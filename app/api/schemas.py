"""HTTP request/response models.

Read endpoints return the domain ``ActionItem`` directly. That is deliberate: the
domain model is already a validated Pydantic model, and a parallel response schema
would be a second definition of the same fields that drifts the first time somebody
adds a column. Request bodies get their own models because a request is a *different
contract* from an entity -- clients may set far fewer fields than the server owns.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.action_item import ActionItem, ActionItemEdit
from app.models.execution import ExecutionResult

__all__ = [
    "ActionItemEdit",
    "ActionItemListResponse",
    "ErrorDetail",
    "ErrorResponse",
    "ExecutionResponse",
    "ExtractionRequest",
    "ExtractionResponse",
    "GoogleStatusResponse",
    "HealthResponse",
    "OAuthStartResponse",
    "TranscriptCreateRequest",
    "TranscriptResponse",
]


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """The single error envelope every failure uses."""

    error: ErrorDetail

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": {
                    "code": "execution_not_allowed",
                    "message": "Cannot execute an extracted action item.",
                    "details": {"status": "extracted"},
                }
            }
        }
    )


class HealthResponse(BaseModel):
    status: str
    app: str
    version: str
    environment: str
    llm_configured: bool
    google_configured: bool
    google_authenticated: bool


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------
class TranscriptCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    content: str = Field(min_length=1, description="Raw meeting transcript text.")
    title: str | None = Field(default=None, max_length=200)
    meeting_date: datetime | None = Field(
        default=None,
        description="When the meeting happened; used to resolve relative deadlines.",
    )


class TranscriptResponse(BaseModel):
    id: UUID
    title: str | None
    meeting_date: datetime | None
    created_at: datetime
    char_count: int
    content: str


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
class ExtractionRequest(BaseModel):
    """Extract action items from a stored transcript, or from inline text."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    transcript_id: UUID | None = Field(
        default=None, description="Id of a transcript previously POSTed to /api/transcripts."
    )
    transcript: str | None = Field(
        default=None, description="Inline transcript text; stored first, then extracted."
    )
    title: str | None = Field(default=None, max_length=200)
    meeting_date: datetime | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> ExtractionRequest:
        if bool(self.transcript_id) == bool(self.transcript):
            raise ValueError("Provide exactly one of 'transcript_id' or 'transcript'.")
        return self


class ExtractionResponse(BaseModel):
    transcript_id: UUID
    item_count: int
    items: list[ActionItem]


class ActionItemListResponse(BaseModel):
    items: list[ActionItem]
    total: int


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
class ExecutionResponse(BaseModel):
    item: ActionItem
    result: ExecutionResult


# ---------------------------------------------------------------------------
# Google auth
# ---------------------------------------------------------------------------
class OAuthStartResponse(BaseModel):
    authorization_url: str
    state: str
    scopes: list[str]


class GoogleStatusResponse(BaseModel):
    configured: bool
    authenticated: bool
    scopes: list[str]
    connected_email: str | None = None