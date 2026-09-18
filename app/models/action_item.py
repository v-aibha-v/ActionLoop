"""The ActionItem domain model, its lifecycle states and Pydantic validation.

Two models, on purpose
----------------------
``ExtractedActionItem`` is the *contract with Claude*. It is deliberately tolerant:
it normalises the small spelling/format variations an LLM inevitably produces
(``"2026-09-25"``, ``null`` owner, a confidence expressed as ``"90%"`` instead of ``0.9``)
so a cosmetic difference does not throw away a genuinely good extraction.

``ActionItem`` is the *domain entity*. It is strict (``extra="forbid"``), owns an
id, a status and the results of external side effects, and is what gets persisted
and served by the API.

Keeping these separate means the LLM contract can evolve without changing the
domain entity, and vice versa. Collapsing them into one model is the usual reason
LLM-facing code becomes untestable.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ISO_HINT = "ISO-8601 (2026-09-25T17:00:00Z) or a calendar date (2026-09-25)"
_NULLISH = frozenset({"", "none", "null", "n/a", "na", "unknown", "tbd", "unspecified", "-", "--"})

# Maps the wording Claude might use onto our four-level enum instead of failing
# validation over a synonym. Unmappable values still raise: silent downgrades hide
# prompt regressions.
_PRIORITY_SYNONYMS: dict[str, str] = {
    "urgent": "urgent",
    "critical": "urgent",
    "blocker": "urgent",
    "p0": "urgent",
    "p1": "urgent",
    "high": "high",
    "important": "high",
    "p2": "high",
    "medium": "medium",
    "normal": "medium",
    "moderate": "medium",
    "p3": "medium",
    "low": "low",
    "minor": "low",
    "p4": "low",
    "p5": "low",
    "nice to have": "low",
}


def utc_now() -> datetime:
    """Timezone-aware 'now'. Always UTC so persisted timestamps compare correctly."""
    return datetime.now(tz=timezone.utc)


class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class ActionItemStatus(str, Enum):
    """Lifecycle of a single action item.

    EXTRACTED -> APPROVED -> EXECUTED is the happy path. REJECTED is terminal and
    FAILED means an external call did not succeed, so the item may be retried.
    Nothing reaches EXECUTED without passing through APPROVED, and that rule is
    enforced by the API/service layer, not by the UI.
    """

    EXTRACTED = "extracted"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Normalisers (shared by the LLM contract and the domain model)
# ---------------------------------------------------------------------------
def normalise_timestamp(value: Any) -> Any:
    """Normalise anything date-ish into a timezone-aware UTC ``datetime`` (or None)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        text = value.strip()
        if text.lower() in _NULLISH:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"Unsupported timestamp format {value!r}; expected {ISO_HINT}."
            ) from exc
    else:
        raise ValueError(f"Unsupported timestamp type {type(value).__name__}; expected a string.")

    # A naive datetime is assumed to be UTC rather than silently mixed with
    # aware datetimes later (which raises TypeError on comparison).
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalise_priority(value: Any) -> Any:
    if value is None:
        return Priority.MEDIUM
    if isinstance(value, Priority):
        return value
    if not isinstance(value, str):
        raise ValueError(f"Unsupported priority type {type(value).__name__}.")
    key = value.strip().lower()
    if key in _NULLISH:
        return Priority.MEDIUM
    if key in _PRIORITY_SYNONYMS:
        return Priority(_PRIORITY_SYNONYMS[key])
    raise ValueError(f"Unknown priority {value!r}; expected one of {[p.value for p in Priority]}.")


def normalise_confidence(value: Any) -> Any:
    """Validate a model-reported confidence and return it on the 0-1 ratio scale.

    A percentage is rescaled **only when the value declares itself one** with a
    trailing ``%``. A bare ``90`` is deliberately *rejected* rather than guessed at:
    "a 0-100 percentage", "a 0-10 score" and "a typo for 0.9" are all equally
    plausible readings, and silently choosing one is exactly the kind of downgrade
    that hides a prompt regression. Requiring the explicit marker keeps the useful
    convenience (``"90%"`` -> ``0.9``) without inventing data.
    """
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly
        raise ValueError("Confidence must be a number between 0 and 1.")

    is_percent = False
    if isinstance(value, str):
        text = value.strip()
        is_percent = text.endswith("%")
        if is_percent:
            text = text[:-1].strip()
        if text.lower() in _NULLISH:
            return 0.0
        try:
            value = float(text)
        except ValueError as exc:
            raise ValueError(f"Confidence must be numeric, got {value!r}.") from exc

    if not isinstance(value, (int, float)):
        raise ValueError(f"Unsupported confidence type {type(value).__name__}.")

    number = float(value)
    if is_percent:
        if not 0.0 <= number <= 100.0:
            raise ValueError(f"Confidence must be between 0% and 100%, got {value!r}.")
        return number / 100.0

    if not 0.0 <= number <= 1.0:
        raise ValueError(
            f"Confidence must be between 0 and 1, got {value!r}. "
            'A percentage must say so explicitly, e.g. "90%".'
        )
    return number


def normalise_optional_text(value: Any) -> Any:
    """Turn LLM placeholders for 'nothing here' into a real ``None``."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return None if text.lower() in _NULLISH else text
    return value


def normalise_context(value: Any) -> Any:
    """Source context stays a string, but loses placeholder noise."""
    normalised = normalise_optional_text(value)
    return "" if normalised is None else normalised


# ---------------------------------------------------------------------------
# LLM contract
# ---------------------------------------------------------------------------
class ExtractedActionItem(BaseModel):
    """One action item as returned by Claude, before it becomes a domain entity.

    ``extra="ignore"`` is intentional: if the model volunteers an extra field it is
    not worth failing the whole extraction over. Required *keys* are enforced by
    the tool schema sent to the API, so a missing key is a genuine contract
    violation and will surface as a validation error.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    title: str = Field(
        min_length=3,
        max_length=200,
        description="Short imperative summary of the task, e.g. 'Prepare benchmark report'.",
    )
    description: str | None = Field(default=None, max_length=2000)
    owner: str | None = Field(default=None, max_length=120)
    deadline: datetime | None = Field(
        default=None, description=f"ISO-8601 timestamp in UTC when known; otherwise null. {ISO_HINT}."
    )
    priority: Priority = Field(default=Priority.MEDIUM)
    source_context: str = Field(
        max_length=1000,
        description="Short verbatim excerpt from the transcript supporting this item.",
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="Model's certainty that this is a real action item."
    )

    @field_validator("title")
    @classmethod
    def _clean_title(cls, value: str) -> str:
        collapsed = " ".join(value.split())
        if len(collapsed) < 3:
            raise ValueError("title must contain at least 3 non-whitespace characters")
        return collapsed

    @field_validator("deadline", mode="before")
    @classmethod
    def _validate_deadline(cls, value: Any) -> Any:
        return normalise_timestamp(value)

    @field_validator("priority", mode="before")
    @classmethod
    def _validate_priority(cls, value: Any) -> Any:
        return normalise_priority(value)

    @field_validator("confidence", mode="before")
    @classmethod
    def _validate_confidence(cls, value: Any) -> Any:
        return normalise_confidence(value)

    @field_validator("description", "owner", mode="before")
    @classmethod
    def _validate_optional_text(cls, value: Any) -> Any:
        return normalise_optional_text(value)

    @field_validator("source_context", mode="before")
    @classmethod
    def _validate_context(cls, value: Any) -> Any:
        return normalise_context(value)


class ExtractionPayload(BaseModel):
    """Envelope around the ``record_action_items`` tool call input.

    Validating the envelope (rather than each item in isolation) means a schema
    violation from the model is reported as one clear error instead of being
    silently swallowed. Anything that is not a valid action item is a prompt or
    model regression we want to see.
    """

    model_config = ConfigDict(extra="ignore")

    action_items: list[ExtractedActionItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Domain entity
# ---------------------------------------------------------------------------
class ActionItem(BaseModel):
    """A persisted, reviewable unit of work.

    ``extra="forbid"`` because this model is the domain contract: an unexpected key
    means a bug in a mapper or a schema, and it should fail loudly in tests rather
    than be silently carried around.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: UUID = Field(default_factory=uuid4)
    transcript_id: UUID | None = None

    title: str = Field(min_length=3, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    owner: str | None = Field(default=None, max_length=120)
    deadline: datetime | None = None
    priority: Priority = Priority.MEDIUM
    source_context: str = Field(default="", max_length=1000)
    confidence: float = Field(ge=0.0, le=1.0)

    status: ActionItemStatus = ActionItemStatus.EXTRACTED

    # --- results of external side effects ---------------------------------
    calendar_event_id: str | None = None
    calendar_event_link: str | None = None
    gmail_draft_id: str | None = None
    last_error: str | None = Field(default=None, max_length=1000)

    # --- audit trail -------------------------------------------------------
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    approved_at: datetime | None = None
    rejected_at: datetime | None = None
    executed_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def _clean_title(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("deadline", mode="before")
    @classmethod
    def _validate_deadline(cls, value: Any) -> Any:
        # Guarantees a timezone-aware UTC datetime (or None).
        return normalise_timestamp(value)

    @field_validator("priority", mode="before")
    @classmethod
    def _validate_priority(cls, value: Any) -> Any:
        return normalise_priority(value)

    @field_validator("confidence", mode="before")
    @classmethod
    def _validate_confidence(cls, value: Any) -> Any:
        return normalise_confidence(value)

    @field_validator("description", "owner", mode="before")
    @classmethod
    def _validate_optional_text(cls, value: Any) -> Any:
        return normalise_optional_text(value)

    @field_validator("created_at", "updated_at", "approved_at", "rejected_at", "executed_at")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    # ------------------------------------------------------------------
    # Construction / derivation
    # ------------------------------------------------------------------
    @classmethod
    def from_extracted(
        cls, extracted: ExtractedActionItem, *, transcript_id: UUID | None = None
    ) -> ActionItem:
        """Promote an LLM extraction into a reviewable domain entity."""
        return cls(
            transcript_id=transcript_id,
            title=extracted.title,
            description=extracted.description,
            owner=extracted.owner,
            deadline=extracted.deadline,
            priority=extracted.priority,
            source_context=extracted.source_context,
            confidence=extracted.confidence,
            status=ActionItemStatus.EXTRACTED,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @property
    def has_deadline(self) -> bool:
        return self.deadline is not None

    @property
    def is_editable(self) -> bool:
        """Editing is allowed while the item is still reviewable or retryable.

        Once an item is approved the review gate closes: the user chose those
        values, and letting them change afterwards would mean the approved content
        is not the content that gets executed. FAILED items stay editable so a bad
        deadline can be corrected and retried.
        """
        return self.status in {ActionItemStatus.EXTRACTED, ActionItemStatus.FAILED}

    @property
    def is_executable(self) -> bool:
        """Only an approved (or previously failed) item may trigger side effects."""
        return self.status in {ActionItemStatus.APPROVED, ActionItemStatus.FAILED}

    def with_status(self, status: ActionItemStatus, *, at: datetime | None = None) -> ActionItem:
        """Return a copy in ``status`` with the matching audit timestamp set."""
        moment = at or utc_now()
        updates: dict[str, Any] = {"status": status, "updated_at": moment}
        if status is ActionItemStatus.APPROVED:
            updates["approved_at"] = moment
        elif status is ActionItemStatus.REJECTED:
            updates["rejected_at"] = moment
        elif status is ActionItemStatus.EXECUTED:
            updates["executed_at"] = moment
        return self.model_copy(update=updates)

    def with_edits(self, changes: dict[str, Any]) -> ActionItem:
        """Apply reviewer edits and re-run validation on the merged values.

        ``model_copy(update=...)`` skips validation, so a merged copy could carry an
        invalid value into the database. Round-tripping through ``model_validate``
        guarantees that anything persisted has passed the same rules as anything
        extracted.
        """
        merged = {**self.model_dump(), **changes, "updated_at": utc_now()}
        return ActionItem.model_validate(merged)


class ActionItemEdit(BaseModel):
    """The fields a reviewer is allowed to change during the approval step.

    This is both the PATCH request body and the service-layer input. Reusing one
    model keeps the API contract and the business rule in sync -- there is no
    second list of editable fields to forget to update.

    Every field is optional, and ``model_dump(exclude_unset=True)`` distinguishes
    "field omitted" from "field explicitly set to null", which is what makes
    ``PATCH {"owner": null}`` able to clear an owner while ``PATCH {}`` is rejected.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=3, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    owner: str | None = Field(default=None, max_length=120)
    deadline: datetime | None = None
    priority: Priority | None = None

    @field_validator("title")
    @classmethod
    def _clean_title(cls, value: str | None) -> str | None:
        return " ".join(value.split()) if value else value

    @field_validator("deadline", mode="before")
    @classmethod
    def _normalise_deadline(cls, value: Any) -> Any:
        return normalise_timestamp(value)

    @field_validator("priority", mode="before")
    @classmethod
    def _normalise_priority(cls, value: Any) -> Any:
        return normalise_priority(value) if value is not None else None

    @field_validator("description", "owner", mode="before")
    @classmethod
    def _normalise_optional_text(cls, value: Any) -> Any:
        return normalise_optional_text(value)

    @model_validator(mode="after")
    def _require_at_least_one_change(self) -> ActionItemEdit:
        if not self.model_fields_set:
            raise ValueError("provide at least one field to update")
        return self

    def as_changes(self) -> dict[str, Any]:
        """Only the fields the client actually sent."""
        return self.model_dump(exclude_unset=True)


# ---------------------------------------------------------------------------
# State machine
#
# Declared once, as data, so the allowed transitions can be unit tested
# exhaustively and no service can invent a shortcut. Every status change in the
# codebase goes through this table.
# ---------------------------------------------------------------------------
ALLOWED_TRANSITIONS: dict[ActionItemStatus, frozenset[ActionItemStatus]] = {
    ActionItemStatus.EXTRACTED: frozenset({ActionItemStatus.APPROVED, ActionItemStatus.REJECTED}),
    ActionItemStatus.APPROVED: frozenset({ActionItemStatus.EXECUTED, ActionItemStatus.FAILED}),
    ActionItemStatus.FAILED: frozenset({ActionItemStatus.EXECUTED, ActionItemStatus.REJECTED}),
    ActionItemStatus.EXECUTED: frozenset(),  # terminal
    ActionItemStatus.REJECTED: frozenset(),  # terminal
}


def can_transition(current: ActionItemStatus, target: ActionItemStatus) -> bool:
    """True when ``current -> target`` is a legal lifecycle move."""
    return target in ALLOWED_TRANSITIONS[current]