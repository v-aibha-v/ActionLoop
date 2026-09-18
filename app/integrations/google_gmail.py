"""Gmail client (Gmail API v1) -- drafts only.

Scope: ``gmail.compose``. That is deliberately the narrowest scope that permits
``users.drafts.create``; ``gmail.send`` is never requested, so this application
*structurally cannot* send mail. There is no ``send`` method anywhere in the
codebase, and a test asserts that.

Idempotency
-----------
Gmail search does not index arbitrary headers, so the draft body carries a marker
line ``AL-REF-<action item id>``. ``find_draft_for_action_item`` searches drafts
for that quoted phrase before creating a new one. Custom headers are still set on
the message so a human inspecting the draft can see where it came from.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any, Protocol

from googleapiclient.errors import HttpError

from app.core.exceptions import GmailIntegrationError
from app.integrations.http_errors import translate_http_error

REFERENCE_PREFIX = "AL-REF-"
REFERENCE_HEADER = "X-ActionLoop-Item-Id"


def reference_marker(action_item_id: str) -> str:
    return f"{REFERENCE_PREFIX}{action_item_id}"


def build_raw_message(
    *, subject: str, body: str, action_item_id: str, to: str | None = None
) -> str:
    """Encode an RFC 5322 message as a Gmail ``raw`` payload (base64url).

    ``To`` is optional on purpose: a draft without a recipient is a perfectly
    normal Gmail draft and keeps ActionLoop from guessing someone's address.
    """
    message = EmailMessage()
    if to:
        message["To"] = to
    message["Subject"] = subject
    message[REFERENCE_HEADER] = action_item_id
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


class GmailClient(Protocol):
    """What the Gmail service needs from a Gmail client."""

    def get_user_email(self) -> str | None: ...

    def find_draft_for_action_item(self, action_item_id: str) -> dict[str, Any] | None: ...

    def create_draft(
        self, *, action_item_id: str, subject: str, body: str, to: str | None = None
    ) -> dict[str, Any]: ...


class GoogleGmailClient:
    """Adapter over the Gmail v1 discovery resource. Drafts only."""

    def __init__(self, service: Any, *, user_id: str = "me") -> None:
        self._service = service
        self._user_id = user_id

    # ------------------------------------------------------------------
    def get_user_email(self) -> str | None:
        """The authenticated address, used as the default draft recipient."""
        request = self._service.users().getProfile(userId=self._user_id)
        profile = self._execute(request, context="reading the Gmail profile")
        return profile.get("emailAddress")

    def find_draft_for_action_item(self, action_item_id: str) -> dict[str, Any] | None:
        """Return a draft previously created for this action item, if any."""
        request = self._service.users().drafts().list(
            userId=self._user_id, q=f'"{reference_marker(action_item_id)}"', maxResults=1
        )
        response = self._execute(request, context="looking up an existing draft")
        drafts = response.get("drafts") or []
        return drafts[0] if drafts else None

    def create_draft(
        self, *, action_item_id: str, subject: str, body: str, to: str | None = None
    ) -> dict[str, Any]:
        """Create a Gmail draft. Never sends."""
        raw = build_raw_message(
            subject=subject, body=body, action_item_id=action_item_id, to=to
        )
        request = self._service.users().drafts().create(
            userId=self._user_id, body={"message": {"raw": raw}}
        )
        return self._execute(request, context="creating a Gmail draft")

    # ------------------------------------------------------------------
    def _execute(self, request: Any, *, context: str) -> dict[str, Any]:
        try:
            return request.execute()
        except HttpError as exc:
            raise translate_http_error(exc, context=context, error_cls=GmailIntegrationError) from exc