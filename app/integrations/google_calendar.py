"""Google Calendar client (Calendar API v3).

Scope: ``calendar.events`` -- enough to list and insert events on a chosen
calendar, and nothing else.

Idempotency
-----------
Every event ActionLoop creates carries a private extended property
``actionloopItemId=<action item id>``. Before inserting, the client lists events
filtered by that property. If an event already exists, it is returned instead of
creating a second one. This is the layer that protects against duplicates when a
previous attempt created the event but failed (or crashed) before the result could
be recorded locally.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Protocol

from googleapiclient.errors import HttpError

from app.core.exceptions import CalendarIntegrationError
from app.integrations.http_errors import translate_http_error

EXTENDED_PROPERTY_KEY = "actionloopItemId"


class CalendarClient(Protocol):
    """What the calendar service needs from a calendar client."""

    def find_event_for_action_item(self, action_item_id: str) -> dict[str, Any] | None: ...

    def create_event(
        self,
        *,
        action_item_id: str,
        summary: str,
        description: str,
        start: datetime,
        duration_minutes: int,
        timezone: str,
    ) -> dict[str, Any]: ...


class GoogleCalendarClient:
    """Adapter over the Calendar v3 discovery resource."""

    def __init__(self, service: Any, *, calendar_id: str = "primary") -> None:
        self._service = service
        self._calendar_id = calendar_id

    # ------------------------------------------------------------------
    def find_event_for_action_item(self, action_item_id: str) -> dict[str, Any] | None:
        """Return the event previously created for this action item, if any."""
        request = self._service.events().list(
            calendarId=self._calendar_id,
            privateExtendedProperty=f"{EXTENDED_PROPERTY_KEY}={action_item_id}",
            maxResults=1,
            singleEvents=True,
        )
        response = self._execute(request, context="looking up an existing event")
        items = response.get("items") or []
        return items[0] if items else None

    def create_event(
        self,
        *,
        action_item_id: str,
        summary: str,
        description: str,
        start: datetime,
        duration_minutes: int,
        timezone: str,
    ) -> dict[str, Any]:
        """Insert a calendar event and return the created resource."""
        end = start + timedelta(minutes=duration_minutes)
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": timezone},
            "end": {"dateTime": end.isoformat(), "timeZone": timezone},
            # The marker that makes retries safe.
            "extendedProperties": {"private": {EXTENDED_PROPERTY_KEY: action_item_id}},
            "reminders": {"useDefault": True},
        }
        request = self._service.events().insert(calendarId=self._calendar_id, body=body)
        return self._execute(request, context="creating a calendar event")

    # ------------------------------------------------------------------
    def _execute(self, request: Any, *, context: str) -> dict[str, Any]:
        try:
            return request.execute()
        except HttpError as exc:
            raise translate_http_error(
                exc, context=context, error_cls=CalendarIntegrationError
            ) from exc