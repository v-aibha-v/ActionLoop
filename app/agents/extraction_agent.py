"""The extraction agent: raw transcript in, validated action items out.

Responsibility boundary
-----------------------
The agent does exactly one thing: turn text into structured, validated data.
It does not persist, does not decide what is approved, and never touches Google.
That boundary is why it can be unit tested with a stub client and why the
"no side effects before approval" rule is easy to verify -- there is nothing here
that *could* cause a side effect.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import anthropic
import httpx
from pydantic import ValidationError

from app.agents.prompts import SYSTEM_PROMPT, TOOL_NAME, build_tool_schema, build_user_message
from app.core.config import Settings
from app.core.exceptions import (
    ConfigurationError,
    EmptyTranscriptError,
    ExtractionError,
    ExtractionValidationError,
    LLMResponseError,
    TranscriptTooLongError,
    redact,
)
from app.core.logging import get_logger
from app.models.action_item import ExtractedActionItem, ExtractionPayload


def prepare_transcript(text: str, settings: Settings) -> str:
    """Normalise and bound-check the transcript before it reaches the LLM.

    Called by the service *before* persisting so an over-long transcript does not
    create a row that can never be extracted, and again inside the agent so the
    invariant holds no matter who calls it.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        raise EmptyTranscriptError("The transcript is empty; nothing to extract.")
    if len(cleaned) > settings.max_transcript_chars:
        raise TranscriptTooLongError(
            f"Transcript is {len(cleaned)} characters; the configured limit is "
            f"{settings.max_transcript_chars}.",
            details={"length": len(cleaned), "limit": settings.max_transcript_chars},
        )
    return cleaned


def summarise_validation_errors(error: ValidationError) -> list[dict[str, str]]:
    """Flatten a Pydantic error into JSON-safe, loggable pairs.

    ``ValidationError.errors()`` embeds the original exception object in ``ctx``,
    which is not JSON serialisable; building the summary explicitly avoids a
    serialisation failure while reporting an error.
    """
    return [
        {
            "location": ".".join(str(part) for part in detail["loc"]),
            "message": str(detail["msg"]),
        }
        for detail in error.errors()
    ]


class ExtractionAgent:
    """Converts one meeting transcript into validated action items."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        # Injected so tests can supply a stub; created lazily otherwise so that
        # importing the app never requires an API key.
        self._client = client
        self._logger = get_logger(__name__)

    # ------------------------------------------------------------------
    # Client
    # ------------------------------------------------------------------
    @property
    def client(self) -> Any:
        if self._client is None:
            if not self._settings.is_anthropic_configured:
                raise ConfigurationError(
                    "ANTHROPIC_API_KEY is not configured. Copy .env.example to .env "
                    "and set your Claude API key."
                )
            self._client = anthropic.AsyncAnthropic(
                api_key=self._settings.anthropic_api_key,
                timeout=self._settings.anthropic_timeout_seconds,
                max_retries=2,
            )
        return self._client

    # ------------------------------------------------------------------
    # Request construction
    # ------------------------------------------------------------------
    def build_tool(self) -> dict[str, Any]:
        return build_tool_schema(max_items=self._settings.max_extracted_items)

    def build_request(
        self,
        transcript: str,
        *,
        meeting_title: str | None = None,
        meeting_date: datetime | None = None,
    ) -> dict[str, Any]:
        """Everything sent to Claude, in one testable place."""
        return {
            "model": self._settings.anthropic_model,
            "max_tokens": self._settings.anthropic_max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": build_user_message(
                        transcript,
                        meeting_title=meeting_title,
                        meeting_date=meeting_date.isoformat() if meeting_date else None,
                    ),
                }
            ],
            "tools": [self.build_tool()],
            # Forced tool use: the model cannot answer with prose, so the response
            # is guaranteed to arrive as a parsed object.
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
        }

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------
    def _extract_tool_input(self, response: Any) -> dict[str, Any]:
        """Pull the tool-call payload out of Claude's response."""
        stop_reason = getattr(response, "stop_reason", None)
        blocks = getattr(response, "content", None) or []

        tool_block = next(
            (
                block
                for block in blocks
                if getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == TOOL_NAME
            ),
            None,
        )
        if tool_block is None:
            raise LLMResponseError(
                "Claude did not return the expected structured tool call.",
                details={"stop_reason": str(stop_reason)},
            )

        payload = getattr(tool_block, "input", None)
        if not isinstance(payload, dict):
            raise LLMResponseError(
                "Claude's tool call did not contain a JSON object.",
                details={"payload_type": type(payload).__name__},
            )
        return payload

    def _validate_payload(self, payload: dict[str, Any]) -> list[ExtractedActionItem]:
        try:
            parsed = ExtractionPayload.model_validate(payload)
        except ValidationError as exc:
            details = summarise_validation_errors(exc)
            self._logger.warning(
                "Extraction payload failed validation",
                extra={"operation": "extraction_validate", "error_count": len(details)},
            )
            raise ExtractionValidationError(
                "Claude returned action items that failed schema validation.",
                details={"errors": details},
            ) from exc

        items = parsed.action_items
        if len(items) > self._settings.max_extracted_items:
            raise ExtractionValidationError(
                f"Claude returned {len(items)} action items; the configured maximum is "
                f"{self._settings.max_extracted_items}.",
                details={"count": len(items), "limit": self._settings.max_extracted_items},
            )
        return items

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def extract(
        self,
        transcript_text: str,
        *,
        meeting_title: str | None = None,
        meeting_date: datetime | None = None,
    ) -> list[ExtractedActionItem]:
        """Extract action items from ``transcript_text``.

        Raises ``ExtractionError`` (and subclasses) for every failure mode, so the
        caller never has to handle a third-party exception type.
        """
        transcript = prepare_transcript(transcript_text, self._settings)
        request = self.build_request(
            transcript, meeting_title=meeting_title, meeting_date=meeting_date
        )
        started = datetime.now()

        try:
            response = await self.client.messages.create(**request)
        except anthropic.AnthropicError as exc:
            self._logger.warning(
                "Claude request failed", extra={"operation": "extraction_request"}
            )
            raise ExtractionError(f"Claude request failed: {redact(str(exc))}") from exc
        except httpx.HTTPError as exc:
            self._logger.warning(
                "Network error calling Claude", extra={"operation": "extraction_request"}
            )
            raise ExtractionError(f"Network error calling Claude: {redact(str(exc))}") from exc

        items = self._validate_payload(self._extract_tool_input(response))

        self._logger.info(
            "Extraction completed",
            extra={
                "operation": "extraction",
                "status": "ok",
                "item_count": len(items),
                "duration_ms": int((datetime.now() - started).total_seconds() * 1000),
            },
        )
        return items