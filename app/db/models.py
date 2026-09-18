"""SQLAlchemy table definitions.

These records are deliberately *not* the domain models. The domain model is
Pydantic, has no idea a database exists, and can be constructed in a test with no
engine at all. Mapping happens in ``app/db/repository.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Float, ForeignKey, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.models.action_item import ActionItemStatus, Priority


class Base(DeclarativeBase):
    """Declarative base for all ActionLoop tables."""


class UTCDateTime(TypeDecorator):
    """Store timezone-aware datetimes as ISO-8601 UTC text.

    SQLite has no native timezone-aware datetime column, so SQLAlchemy would hand
    back a naive ``datetime``. Mixing naive and aware datetimes later raises
    ``TypeError`` at comparison time -- a bug that shows up far from its cause.
    Round-tripping through ISO-8601 text keeps the UTC offset explicit and
    lossless, and reads fine in a sqlite3 shell.
    """

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, _dialect) -> str | None:  # type: ignore[no-untyped-def]
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    def process_result_value(self, value: str | None, _dialect) -> datetime | None:  # type: ignore[no-untyped-def]
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)


def enum_column(enum_cls: type[PyEnum], *, length: int = 24):  # type: ignore[no-untyped-def]
    """VARCHAR-backed enum that stores the *value* (``approved``), not the name.

    ``values_callable`` matters: by default SQLAlchemy persists ``Enum.name``
    (``APPROVED``), which surprises anyone querying the table by hand or writing
    raw SQL.
    """
    return SAEnum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda cls: [member.value for member in cls],
        validate_strings=True,
    )


class TranscriptRecord(Base):
    __tablename__ = "transcripts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    meeting_date: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class ActionItemRecord(Base):
    __tablename__ = "action_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    transcript_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("transcripts.id", ondelete="CASCADE"), nullable=True, index=True
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    priority: Mapped[Priority] = mapped_column(
        enum_column(Priority), nullable=False, default=Priority.MEDIUM
    )
    source_context: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[ActionItemStatus] = mapped_column(
        enum_column(ActionItemStatus), nullable=False, index=True, default=ActionItemStatus.EXTRACTED
    )

    calendar_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    calendar_event_link: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    gmail_draft_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)