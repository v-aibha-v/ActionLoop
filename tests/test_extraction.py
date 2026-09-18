"""Tests for the Claude extraction agent: prompt contract, parsing, failures.

Everything here runs against ``FakeAsyncAnthropic``, so no test touches the real
Claude API. The tests are organised around the four things that can go wrong:

1. the model is not configured or the request fails,
2. the model answers without the tool call,
3. the tool call payload fails Pydantic validation,
4. the input itself is unusable.
"""

from __future__ import annotations

from typing import Any

import anthropic
import httpx
import pytest

from app.agents.extraction_agent import ExtractionAgent, summarise_validation_errors
from app.agents.prompts import SYSTEM_PROMPT, TOOL_NAME, build_tool_schema
from app.core.config import Settings
from app.core.exceptions import (
    ConfigurationError,
    EmptyTranscriptError,
    ExtractionError,
    ExtractionValidationError,
    LLMResponseError,
    TranscriptTooLongError,
)
from app.models.action_item import ExtractedActionItem, Priority
from tests.conftest import (
    FakeAsyncAnthropic,
    FakeResponse,
    FakeTextBlock,
    FakeToolUseBlock,
    sample_extraction,
    tool_response,
)

# ---------------------------------------------------------------------------
# Prompt and tool schema
# ---------------------------------------------------------------------------


def test_tool_schema_requires_every_contracted_field() -> None:
    """The JSON Schema sent to Claude is the contract; assert it stays complete."""
    schema = build_tool_schema(max_items=10)
    item_schema = schema["input_schema"]["properties"]["action_items"]["items"]

    assert schema["name"] == TOOL_NAME
    assert set(item_schema["required"]) == {
        "title",
        "description",
        "owner",
        "deadline",
        "priority",
        "source_context",
        "confidence",
    }
    assert item_schema["properties"]["priority"]["enum"] == ["low", "medium", "high", "urgent"]
    assert schema["input_schema"]["properties"]["action_items"]["maxItems"] == 10


def test_system_prompt_covers_the_extraction_rules() -> None:
    """A prompt regression is a behaviour regression, so pin the key instructions."""
    for phrase in [
        "NOT action items",
        "Use null for `owner`",
        "Never invent a plausible-looking date",
        "verbatim excerpt",
        "untrusted data",
    ]:
        assert phrase in SYSTEM_PROMPT


def test_request_forces_the_tool_and_includes_meeting_metadata(agent: ExtractionAgent) -> None:
    """Structured output is guaranteed by forcing the tool call, not by parsing text."""
    from datetime import datetime, timezone

    request = agent.build_request(
        "Vaibhav: I will prepare the benchmark report by September 25.",
        meeting_title="Q3 Review",
        meeting_date=datetime(2026, 9, 18, tzinfo=timezone.utc),
    )

    assert request["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    assert request["tools"][0]["name"] == TOOL_NAME
    assert request["system"] == SYSTEM_PROMPT
    content = request["messages"][0]["content"]
    assert "Q3 Review" in content
    assert "2026-09-18" in content
    assert "<transcript>" in content


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_valid_extraction_is_accepted(
    agent: ExtractionAgent, fake_anthropic: FakeAsyncAnthropic
) -> None:
    items = await agent.extract("Vaibhav: I will prepare the benchmark report by September 25.")

    assert len(items) == 1
    item = items[0]
    assert item.title == "Prepare benchmark report"
    assert item.owner == "Vaibhav"
    assert item.priority is Priority.HIGH
    assert item.confidence == pytest.approx(0.95)
    assert item.deadline is not None and item.deadline.year == 2026
    assert fake_anthropic.messages.requests


async def test_empty_action_item_list_is_a_valid_extraction(settings: Settings) -> None:
    """A meeting with no tasks must not be an error; it is a real outcome."""
    agent = ExtractionAgent(settings, client=FakeAsyncAnthropic([tool_response([])]))
    assert await agent.extract("Marcus: The weather was nice today.") == []


async def test_optional_fields_may_be_missing(settings: Settings) -> None:
    payload = sample_extraction(owner=None, deadline=None)
    agent = ExtractionAgent(settings, client=FakeAsyncAnthropic([tool_response([payload])]))

    items = await agent.extract("Dana: I will share the dashboard mockups when I can.")

    assert items[0].owner is None
    assert items[0].deadline is None


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


async def test_unconfigured_api_key_raises_configuration_error() -> None:
    unconfigured = Settings(_env_file=None, anthropic_api_key="")
    agent = ExtractionAgent(unconfigured, client=None)

    with pytest.raises(ConfigurationError):
        await agent.extract("Vaibhav: prepare the report.")


async def test_missing_tool_call_is_rejected(settings: Settings) -> None:
    """'Claude answered in prose' must fail loudly, not be regex-scraped."""
    prose = FakeResponse(content=[FakeTextBlock(text="Here are the action items: ...")])
    agent = ExtractionAgent(settings, client=FakeAsyncAnthropic([prose]))

    with pytest.raises(LLMResponseError):
        await agent.extract("Vaibhav: prepare the report.")


async def test_tool_call_under_a_different_name_is_rejected(settings: Settings) -> None:
    wrong_tool = FakeResponse(
        content=[FakeToolUseBlock(name="something_else", input={"action_items": []})]
    )
    agent = ExtractionAgent(settings, client=FakeAsyncAnthropic([wrong_tool]))

    with pytest.raises(LLMResponseError):
        await agent.extract("Vaibhav: prepare the report.")


async def test_non_object_tool_input_is_rejected(settings: Settings) -> None:
    malformed = FakeResponse(content=[FakeToolUseBlock(input="not-an-object")])  # type: ignore[arg-type]
    agent = ExtractionAgent(settings, client=FakeAsyncAnthropic([malformed]))

    with pytest.raises(LLMResponseError):
        await agent.extract("Vaibhav: prepare the report.")


@pytest.mark.parametrize(
    "bad_item",
    [
        {"title": "A valid looking title"},  # missing confidence
        sample_extraction(confidence=5.0),  # confidence out of range
        sample_extraction(confidence=90),  # bare number: ambiguous scale, not a ratio
        sample_extraction(confidence="90"),  # same ambiguity written as a string
        sample_extraction(priority="whenever"),  # unknown priority
        sample_extraction(deadline="some time in Q4"),  # unparseable deadline
        sample_extraction(title="a"),  # title too short
    ],
)
async def test_invalid_structured_output_is_rejected(
    settings: Settings, bad_item: dict[str, Any]
) -> None:
    """Malformed model output becomes ExtractionValidationError, never a bad ActionItem."""
    agent = ExtractionAgent(settings, client=FakeAsyncAnthropic([tool_response([bad_item])]))

    with pytest.raises(ExtractionValidationError):
        await agent.extract("Vaibhav: prepare the report.")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0.9, 0.9),
        ("0.9", 0.9),
        ("90%", 0.9),  # an explicit percent marker is the only unambiguous signal
        (" 95 % ", 0.95),
        ("100%", 1.0),
        (0, 0.0),
        ("-", 0.0),  # a nullish placeholder means "no confidence reported"
    ],
)
def test_confidence_is_normalised_only_when_the_scale_is_unambiguous(
    raw: Any, expected: float
) -> None:
    """A declared percentage is rescaled; a bare out-of-range number is not guessed at."""
    item = ExtractedActionItem(
        title="Prepare benchmark report", source_context="context", confidence=raw
    )

    assert item.confidence == pytest.approx(expected)


async def test_invalid_envelope_is_rejected(settings: Settings) -> None:
    """A tool call whose payload is not the contracted object shape fails too."""
    malformed = FakeResponse(
        content=[FakeToolUseBlock(input={"action_items": "not a list"})]
    )
    agent = ExtractionAgent(settings, client=FakeAsyncAnthropic([malformed]))

    with pytest.raises(ExtractionValidationError):
        await agent.extract("Vaibhav: prepare the report.")


async def test_validation_error_details_are_json_safe() -> None:
    """Error details must be serialisable for logging and for the API response body."""
    import json

    from pydantic import ValidationError

    from app.models.action_item import ExtractionPayload

    try:
        ExtractionPayload.model_validate({"action_items": [{"title": "x"}]})
    except ValidationError as error:
        details = summarise_validation_errors(error)
    else:  # pragma: no cover - the payload above is invalid by construction
        pytest.fail("expected a ValidationError")

    assert details and all(set(d) == {"location", "message"} for d in details)
    json.dumps(details)  # must not raise


async def test_too_many_items_is_rejected(settings: Settings) -> None:
    limited = settings.model_copy(update={"max_extracted_items": 2})
    payload = [sample_extraction(title=f"Task number {i}") for i in range(3)]
    agent = ExtractionAgent(limited, client=FakeAsyncAnthropic([tool_response(payload)]))

    with pytest.raises(ExtractionValidationError):
        await agent.extract("Vaibhav: prepare the report.")


async def test_anthropic_api_error_becomes_extraction_error(settings: Settings) -> None:
    failure = anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com")
    )
    agent = ExtractionAgent(settings, client=FakeAsyncAnthropic([failure]))

    with pytest.raises(ExtractionError):
        await agent.extract("Vaibhav: prepare the report.")


async def test_network_error_is_wrapped_without_leaking_the_key(settings: Settings) -> None:
    """A transport error message is redacted before it can reach a log or a client."""
    failure = httpx.ConnectError("failed; authorization: Bearer sk-ant-secret-value-1234567890")
    agent = ExtractionAgent(settings, client=FakeAsyncAnthropic([failure]))

    with pytest.raises(ExtractionError) as excinfo:
        await agent.extract("Vaibhav: prepare the report.")

    assert "sk-ant-secret-value" not in str(excinfo.value)
    assert "[REDACTED]" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
async def test_blank_transcript_is_rejected(settings: Settings, text: str) -> None:
    agent = ExtractionAgent(settings, client=FakeAsyncAnthropic([]))

    with pytest.raises(EmptyTranscriptError):
        await agent.extract(text)


async def test_oversized_transcript_is_rejected_before_calling_claude(
    settings: Settings,
) -> None:
    limited = settings.model_copy(update={"max_transcript_chars": 500})
    fake = FakeAsyncAnthropic([tool_response([])])
    agent = ExtractionAgent(limited, client=fake)

    with pytest.raises(TranscriptTooLongError):
        await agent.extract("x" * 501)

    assert fake.messages.requests == [], "Claude must not be called for rejected input"