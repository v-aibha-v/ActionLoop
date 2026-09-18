"""Repositories: the only place that translates between records and domain models.

Services depend on these classes, not on SQLAlchemy. A test can therefore run an
entire workflow against an in-memory SQLite database without mocking anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.exceptions import TranscriptNotFoundError
from app.db.models import ActionItemRecord, TranscriptRecord
from app.models.action_item import ActionItem, ActionItemStatus
from app.models.transcript import Transcript


def _as_uuid(value: str | None) -> UUID | None:
    return UUID(value) if value else None


# ---------------------------------------------------------------------------
# Mappers
# ---------------------------------------------------------------------------
def action_item_to_domain(record: ActionItemRecord) -> ActionItem:
    return ActionItem(
        id=UUID(record.id),
        transcript_id=_as_uuid(record.transcript_id),
        title=record.title,
        description=record.description,
        owner=record.owner,
        deadline=record.deadline,
        priority=record.priority,
        source_context=record.source_context,
        confidence=record.confidence,
        status=record.status,
        calendar_event_id=record.calendar_event_id,
        calendar_event_link=record.calendar_event_link,
        gmail_draft_id=record.gmail_draft_id,
        last_error=record.last_error,
        created_at=record.created_at,
        updated_at=record.updated_at,
        approved_at=record.approved_at,
        rejected_at=record.rejected_at,
        executed_at=record.executed_at,
    )


def record_values(item: ActionItem) -> dict[str, Any]:
    """Every persisted column of an action item, as a plain dict."""
    return {
        "transcript_id": str(item.transcript_id) if item.transcript_id else None,
        "title": item.title,
        "description": item.description,
        "owner": item.owner,
        "deadline": item.deadline,
        "priority": item.priority,
        "source_context": item.source_context,
        "confidence": item.confidence,
        "status": item.status,
        "calendar_event_id": item.calendar_event_id,
        "calendar_event_link": item.calendar_event_link,
        "gmail_draft_id": item.gmail_draft_id,
        "last_error": item.last_error,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "approved_at": item.approved_at,
        "rejected_at": item.rejected_at,
        "executed_at": item.executed_at,
    }


def apply_domain_to_record(record: ActionItemRecord, item: ActionItem) -> ActionItemRecord:
    for field, value in record_values(item).items():
        setattr(record, field, value)
    return record


def transcript_to_domain(record: TranscriptRecord) -> Transcript:
    return Transcript(
        id=UUID(record.id),
        title=record.title,
        content=record.content,
        meeting_date=record.meeting_date,
        created_at=record.created_at,
    )


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------
class TranscriptRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, transcript: Transcript) -> Transcript:
        self._session.add(
            TranscriptRecord(
                id=str(transcript.id),
                title=transcript.title,
                content=transcript.content,
                meeting_date=transcript.meeting_date,
                created_at=transcript.created_at,
            )
        )
        self._session.flush()
        return transcript

    def get(self, transcript_id: UUID | str) -> Transcript | None:
        record = self._session.get(TranscriptRecord, str(transcript_id))
        return transcript_to_domain(record) if record else None

    def require(self, transcript_id: UUID | str) -> Transcript:
        transcript = self.get(transcript_id)
        if transcript is None:
            raise TranscriptNotFoundError(f"Transcript {transcript_id} was not found.")
        return transcript


# ---------------------------------------------------------------------------
# Action items
# ---------------------------------------------------------------------------
class ActionItemRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, item: ActionItem) -> ActionItem:
        record = ActionItemRecord(id=str(item.id))
        self._session.add(apply_domain_to_record(record, item))
        self._session.flush()
        return item

    def add_many(self, items: Sequence[ActionItem]) -> list[ActionItem]:
        return [self.add(item) for item in items]

    def save(self, item: ActionItem) -> ActionItem:
        """Persist every mutable field of ``item``."""
        record = self._session.get(ActionItemRecord, str(item.id))
        if record is None:
            return self.add(item)
        apply_domain_to_record(record, item)
        self._session.flush()
        return item

    def save_if_status(self, item: ActionItem, *, expected_status: ActionItemStatus) -> bool:
        """Compare-and-set write: only save when the stored status is unchanged.

        The status is part of the ``WHERE`` clause, so the check and the write are a
        single atomic statement. That closes the race where a double-clicked
        "Execute" button reads APPROVED twice and performs the external side effects
        twice: the second writer updates zero rows and gets ``False``.

        Returns ``True`` when the row was written.
        """
        statement = (
            update(ActionItemRecord)
            .where(
                ActionItemRecord.id == str(item.id),
                ActionItemRecord.status == expected_status,
            )
            .values(**record_values(item))
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(statement)
        self._session.flush()
        return bool(result.rowcount)

    def get(self, item_id: UUID | str) -> ActionItem | None:
        record = self._session.get(ActionItemRecord, str(item_id))
        return action_item_to_domain(record) if record else None

    def list(
        self,
        *,
        status: ActionItemStatus | None = None,
        transcript_id: UUID | str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ActionItem]:
        statement = select(ActionItemRecord).order_by(
            ActionItemRecord.created_at, ActionItemRecord.id
        )
        if status is not None:
            statement = statement.where(ActionItemRecord.status == status)
        if transcript_id is not None:
            statement = statement.where(ActionItemRecord.transcript_id == str(transcript_id))
        statement = statement.limit(max(1, min(limit, 500))).offset(max(0, offset))
        return [action_item_to_domain(r) for r in self._session.scalars(statement).all()]

    def count(self, *, status: ActionItemStatus | None = None) -> int:
        statement = select(func.count()).select_from(ActionItemRecord)
        if status is not None:
            statement = statement.where(ActionItemRecord.status == status)
        return int(self._session.scalar(statement) or 0)