"""Approval service: the human-in-the-loop gate.

This module is deliberately the *only* place that decides whether an action item
may move forward. Enforcing the rule here -- rather than by hiding a button --
means the same guarantee holds for the API, a script, or a future mobile client.

The state machine lives in ``app.models.action_item`` as data, and every status
change is validated against it before anything is written.
"""

from __future__ import annotations

from uuid import UUID

from app.core.exceptions import (
    ActionItemNotFoundError,
    InvalidStateTransitionError,
    ItemNotEditableError,
)
from app.core.logging import get_logger
from app.db.database import Database
from app.db.repository import ActionItemRepository
from app.models.action_item import (
    ALLOWED_TRANSITIONS,
    ActionItem,
    ActionItemEdit,
    ActionItemStatus,
    can_transition,
)


class ApprovalService:
    """Read access plus the review operations: edit, approve, reject."""

    def __init__(self, database: Database) -> None:
        self._db = database
        self._logger = get_logger(__name__)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get(self, item_id: UUID | str) -> ActionItem:
        with self._db.session() as session:
            item = ActionItemRepository(session).get(item_id)
        if item is None:
            raise ActionItemNotFoundError(f"Action item {item_id} was not found.")
        return item

    def list(
        self,
        *,
        status: ActionItemStatus | None = None,
        transcript_id: UUID | str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[ActionItem], int]:
        with self._db.session() as session:
            repo = ActionItemRepository(session)
            items = repo.list(
                status=status, transcript_id=transcript_id, limit=limit, offset=offset
            )
            total = repo.count(status=status)
        return items, total

    # ------------------------------------------------------------------
    # Review operations
    # ------------------------------------------------------------------
    def edit(self, item_id: UUID | str, edit: ActionItemEdit) -> ActionItem:
        """Apply reviewer edits. Only reviewable items may be edited."""
        changes = edit.as_changes()
        with self._db.session() as session:
            repo = ActionItemRepository(session)
            item = self._require(repo, item_id)
            if not item.is_editable:
                raise ItemNotEditableError(
                    f"A {item.status.value} action item cannot be edited. "
                    "Only extracted or failed items can be changed.",
                    details={"status": item.status.value},
                )
            updated = item.with_edits(changes)
            repo.save(updated)

        self._logger.info(
            "Action item edited",
            extra={"action_item_id": str(item_id), "fields": sorted(changes)},
        )
        return updated

    def approve(self, item_id: UUID | str) -> ActionItem:
        return self._transition(item_id, ActionItemStatus.APPROVED)

    def reject(self, item_id: UUID | str) -> ActionItem:
        return self._transition(item_id, ActionItemStatus.REJECTED)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _transition(self, item_id: UUID | str, target: ActionItemStatus) -> ActionItem:
        with self._db.session() as session:
            repo = ActionItemRepository(session)
            item = self._require(repo, item_id)
            self._ensure_transition_allowed(item, target)
            updated = item.with_status(target)
            if target is ActionItemStatus.APPROVED:
                # A fresh approval clears the stale error from a previous attempt.
                updated = updated.model_copy(update={"last_error": None})
            repo.save(updated)

        self._logger.info(
            "Action item status changed",
            extra={
                "action_item_id": str(item_id),
                "status": target.value,
                "operation": "approve" if target is ActionItemStatus.APPROVED else "reject",
            },
        )
        return updated

    def _ensure_transition_allowed(self, item: ActionItem, target: ActionItemStatus) -> None:
        if can_transition(item.status, target):
            return

        hint = ""
        if item.status is ActionItemStatus.FAILED and target is ActionItemStatus.APPROVED:
            hint = " This item already passed review; retry execution instead."
        elif item.status is target:
            hint = f" It is already {target.value}."
        raise InvalidStateTransitionError(
            f"Cannot move a {item.status.value} action item to {target.value}.{hint}",
            details={
                "current_status": item.status.value,
                "requested_status": target.value,
                "allowed": sorted(s.value for s in ALLOWED_TRANSITIONS[item.status]),
            },
        )

    @staticmethod
    def _require(repo: ActionItemRepository, item_id: UUID | str) -> ActionItem:
        item = repo.get(item_id)
        if item is None:
            raise ActionItemNotFoundError(f"Action item {item_id} was not found.")
        return item