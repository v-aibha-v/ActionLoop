"""Domain exception hierarchy and secret redaction.

Design notes
------------
* Every failure the application can produce has a *stable machine-readable code*
  (``code``) and an HTTP status. The API layer therefore never has to guess how to
  translate an exception; it just calls ``to_dict()``.
* Third-party exceptions (Anthropic, Google) are wrapped at the boundary where
  they occur so that nothing above the integration layer knows about them.
* ``redact`` scrubs credential-looking substrings before an error message is
  logged or returned to a client. Error text is a classic accidental credential
  leak: SDK errors happily quote the ``Authorization`` header they used.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
_REDACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),  # Anthropic API keys
    re.compile(r"AIza[0-9A-Za-z_\-]{10,}"),  # Google API keys
    re.compile(r"GOCSPX-[0-9A-Za-z_\-]+"),  # Google OAuth client secrets
    re.compile(r"ya29\.[0-9A-Za-z_\-\.]+"),  # Google access tokens
    re.compile(r"1//[0-9A-Za-z_\-]{10,}"),  # Google refresh tokens
    re.compile(
        r"(?i)\b(api[_-]?key|client[_-]?secret|refresh[_-]?token|access[_-]?token"
        r"|authorization|bearer)\b\s*[:=]\s*[^\s,;\"']+"
    ),
)

REDACTED = "[REDACTED]"


def redact(message: str) -> str:
    """Return ``message`` with anything that looks like a credential masked."""
    if not message:
        return message
    redacted = message
    for pattern in _REDACTION_PATTERNS:
        redacted = pattern.sub(
            lambda match: f"{match.group(1)}={REDACTED}" if match.groups() else REDACTED,
            redacted,
        )
    return redacted


# ---------------------------------------------------------------------------
# Base error
# ---------------------------------------------------------------------------
class ActionLoopError(Exception):
    """Base class for every error ActionLoop raises deliberately."""

    code: str = "actionloop_error"
    status_code: int = 500

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the response body shape used by the API error handler."""
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class ConfigurationError(ActionLoopError):
    """A required setting is missing or unusable (never contains the value)."""

    code = "configuration_error"
    status_code = 500


class EmptyTranscriptError(ActionLoopError):
    code = "empty_transcript"
    status_code = 422


class TranscriptTooLongError(ActionLoopError):
    code = "transcript_too_long"
    status_code = 422


class TranscriptNotFoundError(ActionLoopError):
    code = "transcript_not_found"
    status_code = 404


# ---------------------------------------------------------------------------
# Extraction (LLM)
# ---------------------------------------------------------------------------
class ExtractionError(ActionLoopError):
    """Base class for anything that goes wrong while extracting action items."""

    code = "extraction_error"
    status_code = 502


class LLMResponseError(ExtractionError):
    """Claude replied, but not in the contracted structured shape."""

    code = "llm_response_error"


class ExtractionValidationError(ExtractionError):
    """Claude produced structured output that failed Pydantic validation."""

    code = "extraction_validation_error"


# ---------------------------------------------------------------------------
# Action item lifecycle
# ---------------------------------------------------------------------------
class ActionItemNotFoundError(ActionLoopError):
    code = "action_item_not_found"
    status_code = 404


class InvalidStateTransitionError(ActionLoopError):
    """The requested status change is not allowed from the current status."""

    code = "invalid_state_transition"
    status_code = 409


class ItemNotEditableError(ActionLoopError):
    code = "action_item_not_editable"
    status_code = 409


class ExecutionNotAllowedError(ActionLoopError):
    """``/execute`` was called on an item that is not in an executable status."""

    code = "execution_not_allowed"
    status_code = 409


class AlreadyExecutedError(ActionLoopError):
    """The item already completed successfully; re-executing would duplicate work."""

    code = "already_executed"
    status_code = 409


# ---------------------------------------------------------------------------
# Google integrations
# ---------------------------------------------------------------------------
class NotAuthenticatedError(ActionLoopError):
    """No usable Google credential cached: the user must complete the OAuth flow."""

    code = "google_not_authenticated"
    status_code = 401


class GoogleAuthError(ActionLoopError):
    """OAuth code exchange or token refresh failed."""

    code = "google_auth_error"
    status_code = 502


class IntegrationError(ActionLoopError):
    """Base class for upstream Google API failures."""

    code = "integration_error"
    status_code = 502


class CalendarIntegrationError(IntegrationError):
    code = "calendar_integration_error"


class GmailIntegrationError(IntegrationError):
    code = "gmail_integration_error"