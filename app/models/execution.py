"""Result objects for external side effects (Calendar events, Gmail drafts).

These are plain data, which makes the execution service straightforward: it
collects ``OperationOutcome`` values, decides the final item status, and hands the
list back to the API layer to render. No service has to know how an outcome is
displayed, and a test can assert on outcomes without touching HTTP.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.models.action_item import utc_now


class OperationTarget(str, Enum):
    CALENDAR = "calendar"
    GMAIL = "gmail"


class OperationStatus(str, Enum):
    CREATED = "created"  # the external resource was created by this call
    ALREADY_EXISTS = "already_exists"  # a previous attempt already created it
    SKIPPED = "skipped"  # deliberately not attempted (e.g. no deadline)
    FAILED = "failed"  # the external call raised


class OperationOutcome(BaseModel):
    """What happened for a single external target, for a single action item."""

    model_config = ConfigDict(extra="forbid")

    target: OperationTarget
    status: OperationStatus
    external_id: str | None = None
    link: str | None = None
    detail: str = ""
    occurred_at: datetime = Field(default_factory=utc_now)

    @property
    def succeeded(self) -> bool:
        return self.status in {OperationStatus.CREATED, OperationStatus.ALREADY_EXISTS}

    @property
    def failed(self) -> bool:
        return self.status is OperationStatus.FAILED


class ExecutionResult(BaseModel):
    """Aggregate outcome of executing one approved action item."""

    model_config = ConfigDict(extra="forbid")

    action_item_id: str
    status: str
    outcomes: list[OperationOutcome] = Field(default_factory=list)
    error: str | None = None

    @property
    def all_succeeded(self) -> bool:
        """True when nothing failed (skipped operations are not failures)."""
        return all(
            outcome.succeeded or outcome.status is OperationStatus.SKIPPED
            for outcome in self.outcomes
        )

    def for_target(self, target: OperationTarget) -> OperationOutcome | None:
        return next((o for o in self.outcomes if o.target is target), None)