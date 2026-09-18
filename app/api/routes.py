"""REST API routes.

Status codes are chosen to be informative rather than uniform: 201 for creation,
404 for unknown ids, 409 for an illegal lifecycle transition or a duplicate
execution, 401 for "connect Google first", 422 for validation, 502 when an
upstream provider misbehaves.

Sync endpoints are declared with ``def`` (not ``async def``) so FastAPI runs them
in a threadpool. The database work and the Google API calls in those handlers are
blocking library calls; running them on the event loop would stall every other
request. Only the extraction endpoint is ``async`` because the Anthropic client is
genuinely asynchronous.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from app.api.dependencies import ContainerDep
from app.api.schemas import (
    ActionItemEdit,
    ActionItemListResponse,
    ErrorResponse,
    ExecutionResponse,
    ExtractionRequest,
    ExtractionResponse,
    HealthResponse,
    TranscriptCreateRequest,
    TranscriptResponse,
)
from app.core.examples import load_sample_transcript
from app.models.action_item import ActionItem, ActionItemStatus

system_router = APIRouter(tags=["system"])
router = APIRouter(prefix="/api", tags=["action-items"])

_NOT_APPROVED = {409: {"model": ErrorResponse, "description": "Item is not approved"}}
_NOT_FOUND = {404: {"model": ErrorResponse, "description": "Unknown id"}}


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
@system_router.get("/health", response_model=HealthResponse, summary="Liveness and config check")
def health(container: ContainerDep) -> HealthResponse:
    """Reports which integrations are usable without leaking any secret value."""
    from app import __version__

    settings = container.settings
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        version=__version__,
        environment=settings.environment,
        llm_configured=settings.is_anthropic_configured,
        google_configured=settings.is_google_configured,
        google_authenticated=container.oauth.is_authenticated(),
    )


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------
@router.post(
    "/transcripts",
    response_model=TranscriptResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Store a meeting transcript",
)
def create_transcript(
    payload: TranscriptCreateRequest, container: ContainerDep
) -> TranscriptResponse:
    """Validate and persist a transcript. Does not call Claude."""
    transcript = container.extraction_service.create_transcript(
        content=payload.content,
        title=payload.title,
        meeting_date=payload.meeting_date,
    )
    return TranscriptResponse(
        id=transcript.id,
        title=transcript.title,
        meeting_date=transcript.meeting_date,
        created_at=transcript.created_at,
        char_count=transcript.char_count,
        content=transcript.content,
    )


@router.get(
    "/transcripts/{transcript_id}",
    response_model=TranscriptResponse,
    responses=_NOT_FOUND,
    summary="Fetch a stored transcript",
)
def get_transcript(transcript_id: UUID, container: ContainerDep) -> TranscriptResponse:
    transcript = container.extraction_service.get_transcript(transcript_id)
    return TranscriptResponse(
        id=transcript.id,
        title=transcript.title,
        meeting_date=transcript.meeting_date,
        created_at=transcript.created_at,
        char_count=transcript.char_count,
        content=transcript.content,
    )


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
@router.post(
    "/action-items/extract",
    response_model=ExtractionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        502: {"model": ErrorResponse, "description": "Claude call or structured output failed"},
        422: {"model": ErrorResponse, "description": "Transcript rejected"},
    },
    summary="Extract action items from a transcript with Claude",
)
async def extract_action_items(
    payload: ExtractionRequest, container: ContainerDep
) -> ExtractionResponse:
    """Send the transcript to Claude and persist the validated action items.

    This endpoint has no side effects outside ActionLoop: nothing is created in
    Google Calendar or Gmail until a human approves an item.
    """
    if payload.transcript_id is not None:
        transcript_id = payload.transcript_id
    else:
        transcript = container.extraction_service.create_transcript(
            content=payload.transcript or "",
            title=payload.title,
            meeting_date=payload.meeting_date,
        )
        transcript_id = transcript.id

    items = await container.extraction_service.extract(transcript_id)
    return ExtractionResponse(
        transcript_id=transcript_id, item_count=len(items), items=items
    )


# ---------------------------------------------------------------------------
# Action items
# ---------------------------------------------------------------------------
@router.get(
    "/action-items",
    response_model=ActionItemListResponse,
    summary="List action items, optionally filtered by status",
)
def list_action_items(
    container: ContainerDep,
    item_status: Annotated[
        ActionItemStatus | None, Query(alias="status", description="Filter by lifecycle status")
    ] = None,
    transcript_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ActionItemListResponse:
    items, total = container.approval_service.list(
        status=item_status, transcript_id=transcript_id, limit=limit, offset=offset
    )
    return ActionItemListResponse(items=items, total=total)


@router.get(
    "/action-items/{item_id}",
    response_model=ActionItem,
    responses=_NOT_FOUND,
    summary="Fetch one action item",
)
def get_action_item(item_id: UUID, container: ContainerDep) -> ActionItem:
    return container.approval_service.get(item_id)


@router.patch(
    "/action-items/{item_id}",
    response_model=ActionItem,
    responses={**_NOT_FOUND, 409: {"model": ErrorResponse, "description": "Item is not editable"}},
    summary="Edit an action item during review",
)
def edit_action_item(
    item_id: UUID, edit: ActionItemEdit, container: ContainerDep
) -> ActionItem:
    """Partial update. Only extracted or failed items may be edited."""
    return container.approval_service.edit(item_id, edit)


@router.post(
    "/action-items/{item_id}/approve",
    response_model=ActionItem,
    responses={**_NOT_FOUND, 409: {"model": ErrorResponse, "description": "Illegal transition"}},
    summary="Approve an action item (human-in-the-loop gate)",
)
def approve_action_item(item_id: UUID, container: ContainerDep) -> ActionItem:
    """Marks the item as approved. Still no external side effect."""
    return container.approval_service.approve(item_id)


@router.post(
    "/action-items/{item_id}/reject",
    response_model=ActionItem,
    responses={**_NOT_FOUND, 409: {"model": ErrorResponse, "description": "Illegal transition"}},
    summary="Reject an action item",
)
def reject_action_item(item_id: UUID, container: ContainerDep) -> ActionItem:
    """Terminal state: a rejected item can never be executed."""
    return container.approval_service.reject(item_id)


@router.post(
    "/action-items/{item_id}/execute",
    response_model=ExecutionResponse,
    responses={
        **_NOT_FOUND,
        **_NOT_APPROVED,
        401: {"model": ErrorResponse, "description": "Google is not connected"},
        502: {"model": ErrorResponse, "description": "A Google API call failed"},
    },
    summary="Create the Calendar event and Gmail draft for an approved item",
)
def execute_action_item(item_id: UUID, container: ContainerDep) -> ExecutionResponse:
    """The only endpoint that performs external side effects.

    Requires the item to be approved; the check is enforced in the service layer,
    so it cannot be bypassed by calling the API directly.
    """
    item, result = container.execution_service.execute(item_id)
    return ExecutionResponse(item=item, result=result)


# ---------------------------------------------------------------------------
# Demo helpers
# ---------------------------------------------------------------------------
@router.get("/examples/transcript", summary="Bundled sample transcript for the demo")
def sample_transcript() -> dict[str, str]:
    content = load_sample_transcript()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No sample transcript is bundled."
        )
    return {"content": content}