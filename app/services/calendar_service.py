"""Calendar business rules: what an ActionLoop calendar event should contain.

Two clearly separated concerns:

* ``build_event_description`` is pure and decides the *content* of the event.
* ``CalendarService`` decides *whether* to create one, and converts upstream
  failures into ``OperationOutcome`` values instead of exceptions.

That split is why a test can assert on the exact event text without any Google
involvement, and why a calendar outage can never abort the rest of an execution.
"""

from __future__ import annotations

from collections.abc import Callable

from app.core.config import Settings
from app.core.exceptions import IntegrationError
from app.core.logging import get_logger
from app.integrations.google_calendar import CalendarClient
from app.models.action_item import ActionItem
from app.models.execution import OperationOutcome, OperationStatus, OperationTarget

CALENDAR_SKIP_DETAIL = (
    "No deadline was extracted for this item, so no calendar event was created. "
    "Add a deadline during review if the task needs time blocked out."
)


def build_event_description(item: ActionItem) -> str:
    """Compose the calendar event body from the item and its meeting context."""
    sections: list[str] = []

    if item.description:
        sections.append(item.description)

    metadata = [
        f"Owner: {item.owner or 'unassigned'}",
        f"Priority: {item.priority.value}",
        f"Deadline: {item.deadline.isoformat() if item.deadline else 'none'}",
    ]
    sections.append("\n".join(metadata))

    if item.source_context:
        sections.append(f'From the meeting transcript:\n"{item.source_context}"')

    sections.append(
        f"Created by ActionLoop from approved action item {item.id}. "
        "The event was not created until a human approved this item."
    )
    return "\n\n".join(sections)


class CalendarService:
    """Creates calendar events for approved action items.

    ``client_provider`` is a callable, not a client instance, so the credential is
    resolved at execution time. A token refreshed during the session is therefore
    picked up, and tests can inject a fake without touching OAuth.
    """

    def __init__(
        self,
        client_provider: Callable[[], CalendarClient],
        settings: Settings,
    ) -> None:
        self._client_provider = client_provider
        self._settings = settings
        self._logger = get_logger(__name__)

    def create_event_for(self, item: ActionItem) -> OperationOutcome:
        """Create (or discover) the calendar event for ``item``.

        Raises ``NotAuthenticatedError`` unchanged: a missing/expired Google
        credential is a session problem, not a failure of this action item, so the
        caller must not record the item as failed.
        """
        if not item.has_deadline:
            return OperationOutcome(
                target=OperationTarget.CALENDAR,
                status=OperationStatus.SKIPPED,
                detail=CALENDAR_SKIP_DETAIL,
            )

        # Layer 1 idempotency: we already recorded an event id for this item, so
        # there is nothing to do and no API call to make.
        if item.calendar_event_id:
            return OperationOutcome(
                target=OperationTarget.CALENDAR,
                status=OperationStatus.ALREADY_EXISTS,
                external_id=item.calendar_event_id,
                link=item.calendar_event_link,
                detail="A calendar event for this action item already exists.",
            )

        try:
            client = self._client_provider()

            # Layer 2 idempotency: ask Google whether a previous attempt already
            # created this event (covers a crash after the insert but before the
            # local write).
            existing = client.find_event_for_action_item(str(item.id))
            if existing:
                return OperationOutcome(
                    target=OperationTarget.CALENDAR,
                    status=OperationStatus.ALREADY_EXISTS,
                    external_id=existing.get("id"),
                    link=existing.get("htmlLink"),
                    detail="An event for this action item already exists on the calendar.",
                )

            assert item.deadline is not None  # guaranteed by has_deadline above
            event = client.create_event(
                action_item_id=str(item.id),
                summary=item.title,
                description=build_event_description(item),
                start=item.deadline,
                duration_minutes=self._settings.calendar_event_duration_minutes,
                timezone=self._settings.google_calendar_timezone,
            )
        except IntegrationError as exc:
            self._logger.warning(
                "Calendar operation failed",
                extra={"action_item_id": str(item.id), "provider": "google_calendar"},
            )
            return OperationOutcome(
                target=OperationTarget.CALENDAR,
                status=OperationStatus.FAILED,
                detail=exc.message,
            )

        return OperationOutcome(
            target=OperationTarget.CALENDAR,
            status=OperationStatus.CREATED,
            external_id=event.get("id"),
            link=event.get("htmlLink"),
            detail="Calendar event created successfully.",
        )