"""Transcript entity: the raw input a set of action items was extracted from."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.action_item import normalise_timestamp, utc_now


class Transcript(BaseModel):
    """A stored meeting transcript.

    Kept as its own entity (rather than a text column on ActionItem) so that
    re-extracting one transcript produces a fresh, comparable batch of items and so
    the extracted items can always be traced back to the text that produced them.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: UUID = Field(default_factory=uuid4)
    title: str | None = Field(default=None, max_length=200)
    content: str = Field(min_length=1)
    meeting_date: datetime | None = Field(
        default=None,
        description="When the meeting happened; used to resolve relative deadlines.",
    )
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("meeting_date", mode="before")
    @classmethod
    def _normalise_meeting_date(cls, value: Any) -> Any:
        return normalise_timestamp(value)

    @field_validator("title", mode="before")
    @classmethod
    def _blank_title_is_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def char_count(self) -> int:
        return len(self.content)