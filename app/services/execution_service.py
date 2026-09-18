"""Execution service: the only code path that produces external side effects.

The safety story in four layers
-------------------------------
1. **Status gate.** ``execute`` refuses anything that is not APPROVED or FAILED.
   A REJECTED or EXTRACTED item can never reach Google, no matter what the UI does.
2. **Local idempotency.** If the item already carries a calendar event id or a
   draft id, that operation is reported as already done without an API call.
3. **Remote idempotency.** The Google clients look for the resource they would have
   created (extended property / body marker) before creating it.
4. **Compare-and-set write.** The final status update only applies if the row is
   still in the status we read, so a double-click cannot record two executions.

Layer 4 is the interesting one: external calls happen outside a database
transaction, so "read status, call Google, write status" is inherently racy.
Making the write conditional moves the correctness guarantee into the single
atomic statement that finishes the operation.
"""

from __future__ import annotations

from uuid import UUID

from app.core.exceptions import (
    ActionItemNotFoundError,
    AlreadyExecutedError,
    ExecutionNotAllowedError,
)
from app.core.logging import get_logger
from app.db.database import Database
from app.db.repository import ActionItemRepository
from app.models.action_item import ActionItem, ActionItemStatus
from app.models.execution import ExecutionResult, OperationOutcome, OperationStatus
from app.services.calendar_service import CalendarService
from app.services.gmail_service import GmailService


def summarise_failures(outcomes: list[OperationOutcome]) -> str | None:
    """One human-readable line describing every failed external operation."""
    failures = [o for o in outcomes if o.status is OperationStatus.FAILED]
    if not failures:
        return None
    return "; ".join(f"{o.target.value}: {o.detail}" for o in failures)


class ExecutionService:
    """Turns an approved action item into Calendar/Gmail side effects."""

    def __init__(
        self,
        database: Database,
        calendar: CalendarService,
        gmail: GmailService,
    ) -> None:
        self._db = database
        self._calendar = calendar
        self._gmail = gmail
        self._logger = get_logger(__name__)

    # ------------------------------------------------------------------
    def execute(self, item_id: UUID | str) -> tuple[ActionItem, ExecutionResult]:
        """Execute one action item and return its new state plus a result report."""
        item = self._load(item_id)
        self._ensure_executable(item)

        # --- external calls (no transaction held open) --------------------
        outcomes = [
            self._calendar.create_event_for(item),
            self._gmail.create_draft_for(item),
        ]

        failure_summary = summarise_failures(outcomes)
        updated = self._apply_outcome(item, outcomes, failure_summary)

        result = ExecutionResult(
            action_item_id=str(updated.id),
            status=updated.status.value,
            outcomes=outcomes,
            error=failure_summary,
        )
        self._logger.info(
            "Action item executed",
            extra={
                "action_item_id": str(updated.id),
                "status": updated.status.value,
                "calendar_status": self._status_of(outcomes, "calendar"),
                "gmail_status": self._status_of(outcomes, "gmail"),
            },
        )
        return updated, result

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------
    def _load(self, item_id: UUID | str) -> ActionItem:
        with self._db.session() as session:
            item = ActionItemRepository(session).get(item_id)
        if item is None:
            raise ActionItemNotFoundError(f"Action item {item_id} was not found.")
        return item

    @staticmethod
    def _ensure_executable(item: ActionItem) -> None:
        """The approval gate. Rejects anything that has not been approved."""
        if item.status is ActionItemStatus.EXECUTED:
            raise AlreadyExecutedError(
                "This action item was already executed successfully. Re-running it would "
                "create duplicate external resources.",
                details={"status": item.status.value},
            )
        if not item.is_executable:
            raise ExecutionNotAllowedError(
                f"Cannot execute a {item.status.value} action item. "
                "Only approved items may create calendar events or Gmail drafts.",
                details={"status": item.status.value},
            )

    # ------------------------------------------------------------------
    # Persistence of the result
    # ------------------------------------------------------------------
    def _apply_outcome(
        self,
        item: ActionItem,
        outcomes: list[OperationOutcome],
        failure_summary: str | None,
    ) -> ActionItem:
        updates: dict[str, object] = {}
        for outcome in outcomes:
            if outcome.status not in {OperationStatus.CREATED, OperationStatus.ALREADY_EXISTS}:
                continue
            if outcome.target.value == "calendar":
                updates["calendar_event_id"] = outcome.external_id
                updates["calendar_event_link"] = outcome.link
            else:
                updates["gmail_draft_id"] = outcome.external_id

        target_status = (
            ActionItemStatus.FAILED if failure_summary else ActionItemStatus.EXECUTED
        )
        updated = item.with_status(target_status).model_copy(
            update={**updates, "last_error": failure_summary}
        )

        with self._db.session() as session:
            written = ActionItemRepository(session).save_if_status(
                updated, expected_status=item.status
            )
        if not written:
            # Another request changed the row while we were talking to Google.
            # Layers 2 and 3 mean the external resources were deduplicated, so
            # failing loudly here is both correct and safe.
            raise AlreadyExecutedError(
                "This action item was modified by another request while it was executing. "
                "Reload it to see the current state.",
                details={"status": item.status.value},
            )
        return updated

    @staticmethod
    def _status_of(outcomes: list[OperationOutcome], target: str) -> str | None:
        outcome = next((o for o in outcomes if o.target.value == target), None)
        return outcome.status.value if outcome else None