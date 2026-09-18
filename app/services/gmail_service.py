"""Gmail business rules: follow-up drafts, never sent mail.

The safety property of this module is structural, not a matter of discipline:
``GmailClient`` exposes only ``create_draft``, the requested OAuth scope is
``gmail.compose``, and no code path anywhere calls ``drafts.send`` or
``messages.send``. A test asserts that the whole package contains no send call.

Drafts are idempotent by the same two-layer scheme as calendar events: the locally
recorded draft id first, then a Gmail search for the item's reference marker.
"""

from __future__ import annotations

from collections.abc import Callable

from app.core.config import Settings
from app.core.exceptions import IntegrationError
from app.core.logging import get_logger
from app.integrations.google_gmail import REFERENCE_PREFIX, GmailClient
from app.models.action_item import ActionItem
from app.models.execution import OperationOutcome, OperationStatus, OperationTarget

DRAFT_NOT_SENT_NOTE = "Draft created - not sent"


def build_follow_up_subject(item: ActionItem) -> str:
    return f"Follow-up: {item.title}"


def build_follow_up_body(item: ActionItem) -> str:
    """Compose the draft body: task, owner, deadline and meeting context.

    The trailing reference line is intentional. Gmail does not index custom
    headers for search, so a body marker is the only way to find a previously
    created draft for the same action item. The trade-off (a small traceability
    line in the message) is worth it for retry safety, and the line looks like an
    ordinary ticket reference to a human reader.
    """
    lines = ["Following up on the action item we agreed on.", ""]
    lines.append(f"Task: {item.title}")
    if item.description:
        lines.append(f"Details: {item.description}")
    lines.append(f"Owner: {item.owner or 'to be confirmed'}")
    lines.append(
        f"Deadline: {item.deadline.isoformat() if item.deadline else 'not specified'}"
    )
    lines.append(f"Priority: {item.priority.value}")

    if item.source_context:
        lines.extend(["", "Context from the meeting:", f'"{item.source_context}"'])

    lines.extend(
        [
            "",
            "This message was prepared as a draft for review. Please check it before sending.",
            "",
            "--",
            f"ActionLoop reference: {REFERENCE_PREFIX}{item.id}",
        ]
    )
    return "\n".join(lines)


class GmailService:
    """Creates Gmail drafts for approved action items."""

    def __init__(self, client_provider: Callable[[], GmailClient], settings: Settings) -> None:
        self._client_provider = client_provider
        self._settings = settings
        self._logger = get_logger(__name__)

    def create_draft_for(self, item: ActionItem) -> OperationOutcome:
        """Create (or discover) the follow-up draft for ``item``.

        Raises ``NotAuthenticatedError`` unchanged, for the same reason as the
        calendar service.
        """
        # Layer 1 idempotency: a draft id is already recorded locally.
        if item.gmail_draft_id:
            return OperationOutcome(
                target=OperationTarget.GMAIL,
                status=OperationStatus.ALREADY_EXISTS,
                external_id=item.gmail_draft_id,
                detail=f"{DRAFT_NOT_SENT_NOTE}. A draft for this action item already exists.",
            )

        try:
            client = self._client_provider()

            # Layer 2 idempotency: search for a draft carrying this item's marker.
            existing = client.find_draft_for_action_item(str(item.id))
            if existing:
                return OperationOutcome(
                    target=OperationTarget.GMAIL,
                    status=OperationStatus.ALREADY_EXISTS,
                    external_id=existing.get("id"),
                    detail=(
                        f"{DRAFT_NOT_SENT_NOTE}. A draft for this action item already exists "
                        "in Gmail."
                    ),
                )

            recipient = self._settings.gmail_follow_up_recipient or None
            draft = client.create_draft(
                action_item_id=str(item.id),
                subject=build_follow_up_subject(item),
                body=build_follow_up_body(item),
                to=recipient,
            )
        except IntegrationError as exc:
            self._logger.warning(
                "Gmail operation failed",
                extra={"action_item_id": str(item.id), "provider": "gmail"},
            )
            return OperationOutcome(
                target=OperationTarget.GMAIL,
                status=OperationStatus.FAILED,
                detail=exc.message,
            )

        return OperationOutcome(
            target=OperationTarget.GMAIL,
            status=OperationStatus.CREATED,
            external_id=draft.get("id"),
            detail=f"{DRAFT_NOT_SENT_NOTE}. Review it in Gmail when you are ready.",
        )