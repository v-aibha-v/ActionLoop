"""Tests for execution safety: the approval gate, idempotency, provider failures.

The property under test is the one that matters most in this project: nothing
reaches Google Calendar or Gmail unless a human approved the item, and a repeated
execution never duplicates an external resource.

Everything runs against the fake Google clients, so the assertions are about
*behaviour* ("was an event ever created?") rather than about HTTP payloads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import app
from app.core.config import GMAIL_COMPOSE_SCOPE, GOOGLE_SCOPES
from app.core.exceptions import AlreadyExecutedError, ExecutionNotAllowedError
from app.db.repository import ActionItemRepository
from app.integrations.google_gmail import REFERENCE_PREFIX
from app.models.action_item import ActionItem, ActionItemStatus
from app.models.execution import OperationStatus, OperationTarget
from app.services.calendar_service import CALENDAR_SKIP_DETAIL
from app.services.gmail_service import DRAFT_NOT_SENT_NOTE
from tests.conftest import make_item


def persist(database: Any, item: ActionItem) -> ActionItem:
    """Store an item directly, for cases a fixture does not already provide."""
    with database.session() as session:
        ActionItemRepository(session).add(item)
    return item


def outcomes_by_target(result: Any) -> dict[OperationTarget, Any]:
    return {outcome.target: outcome for outcome in result.outcomes}


# ---------------------------------------------------------------------------
# The approval gate: no side effect without an approval
# ---------------------------------------------------------------------------
def test_extracted_item_cannot_execute(container: Any, google_fakes: Any, stored_item: Any) -> None:
    """The gate is enforced in the service, not by hiding a button in the UI."""
    with pytest.raises(ExecutionNotAllowedError) as excinfo:
        container.execution_service.execute(stored_item.id)

    assert excinfo.value.details["status"] == "extracted"
    assert google_fakes.total_creates == 0, "an unapproved item must not reach Google"


def test_rejected_item_can_never_execute(container: Any, google_fakes: Any, stored_item: Any) -> None:
    """Rejection is terminal, so a rejected item is permanently inert."""
    container.approval_service.reject(stored_item.id)

    with pytest.raises(ExecutionNotAllowedError):
        container.execution_service.execute(stored_item.id)

    assert google_fakes.total_creates == 0


def test_approved_item_executes_both_integrations(
    container: Any, google_fakes: Any, approved_item: Any
) -> None:
    item, result = container.execution_service.execute(approved_item.id)

    assert item.status is ActionItemStatus.EXECUTED
    assert item.executed_at is not None
    assert result.all_succeeded is True
    assert result.error is None

    assert len(google_fakes.calendar.created) == 1
    assert len(google_fakes.gmail.created) == 1
    assert item.calendar_event_id == "cal-event-1"
    assert item.gmail_draft_id == "draft-1"


def test_execution_over_http_is_refused_for_an_unapproved_item(client: Any) -> None:
    """The same gate holds for a direct API call, which is the real attack path."""
    extracted = client.post(
        "/api/action-items/extract",
        json={"transcript": "Vaibhav: I will prepare the benchmark report by September 25."},
    ).json()["items"][0]

    refused = client.post(f"/api/action-items/{extracted['id']}/execute")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "execution_not_allowed"

    client.post(f"/api/action-items/{extracted['id']}/approve")
    executed = client.post(f"/api/action-items/{extracted['id']}/execute")

    assert executed.status_code == 200
    assert executed.json()["item"]["status"] == ActionItemStatus.EXECUTED.value


# ---------------------------------------------------------------------------
# Idempotency: a repeated execution must not duplicate external resources
# ---------------------------------------------------------------------------
def test_re_executing_an_executed_item_is_refused(
    container: Any, google_fakes: Any, approved_item: Any
) -> None:
    container.execution_service.execute(approved_item.id)

    with pytest.raises(AlreadyExecutedError):
        container.execution_service.execute(approved_item.id)

    assert google_fakes.total_creates == 2, "a duplicate execution must create nothing"


def test_a_resource_already_on_google_is_detected_instead_of_recreated(
    container: Any, google_fakes: Any, approved_item: Any
) -> None:
    """Remote idempotency: the crash happened after Google, before our own write."""
    google_fakes.calendar.existing_event = {
        "id": "cal-event-existing",
        "htmlLink": "https://calendar.example.invalid/e",
    }
    google_fakes.gmail.existing_draft = {"id": "draft-existing"}

    item, result = container.execution_service.execute(approved_item.id)
    outcomes = outcomes_by_target(result)

    assert google_fakes.calendar.created == []
    assert google_fakes.gmail.created == []
    assert outcomes[OperationTarget.CALENDAR].status is OperationStatus.ALREADY_EXISTS
    assert outcomes[OperationTarget.GMAIL].status is OperationStatus.ALREADY_EXISTS
    assert item.calendar_event_id == "cal-event-existing"
    assert item.gmail_draft_id == "draft-existing"
    assert item.status is ActionItemStatus.EXECUTED


# ---------------------------------------------------------------------------
# Provider failures
# ---------------------------------------------------------------------------
def test_a_calendar_outage_is_recorded_and_the_item_can_be_retried(
    container: Any, google_fakes: Any, approved_item: Any
) -> None:
    """A Calendar failure must not discard the draft that already succeeded."""
    google_fakes.calendar.fail_times = 1

    item, result = container.execution_service.execute(approved_item.id)

    assert item.status is ActionItemStatus.FAILED
    assert item.last_error is not None and "calendar" in item.last_error
    assert result.all_succeeded is False
    assert outcomes_by_target(result)[OperationTarget.GMAIL].status is OperationStatus.CREATED
    assert item.calendar_event_id is None
    assert item.gmail_draft_id == "draft-1"

    retried, retry_result = container.execution_service.execute(approved_item.id)

    assert retried.status is ActionItemStatus.EXECUTED
    assert retried.last_error is None
    assert len(google_fakes.calendar.created) == 1
    assert len(google_fakes.gmail.created) == 1, "the draft must not be duplicated on retry"
    assert (
        outcomes_by_target(retry_result)[OperationTarget.GMAIL].status
        is OperationStatus.ALREADY_EXISTS
    )


def test_a_gmail_outage_keeps_the_calendar_event(
    container: Any, google_fakes: Any, approved_item: Any
) -> None:
    google_fakes.gmail.fail_times = 1

    item, result = container.execution_service.execute(approved_item.id)

    assert item.status is ActionItemStatus.FAILED
    assert item.calendar_event_id == "cal-event-1"
    assert item.gmail_draft_id is None
    assert "gmail" in (item.last_error or "")
    assert outcomes_by_target(result)[OperationTarget.CALENDAR].status is OperationStatus.CREATED


def test_an_item_without_a_deadline_skips_calendar_but_still_drafts(
    container: Any, google_fakes: Any, database: Any
) -> None:
    item = persist(database, make_item(deadline=None))
    container.approval_service.approve(item.id)

    executed, result = container.execution_service.execute(item.id)
    calendar_outcome = outcomes_by_target(result)[OperationTarget.CALENDAR]

    assert calendar_outcome.status is OperationStatus.SKIPPED
    assert calendar_outcome.detail == CALENDAR_SKIP_DETAIL
    assert google_fakes.calendar.created == []
    assert len(google_fakes.gmail.created) == 1
    assert executed.status is ActionItemStatus.EXECUTED
    assert result.all_succeeded is True


# ---------------------------------------------------------------------------
# Gmail is drafts-only, structurally rather than by convention
# ---------------------------------------------------------------------------
def test_the_draft_says_it_was_not_sent_and_carries_a_reference_marker(
    container: Any, google_fakes: Any, approved_item: Any
) -> None:
    item, result = container.execution_service.execute(approved_item.id)

    draft = google_fakes.gmail.created[0]
    assert draft["subject"] == "Follow-up: Prepare benchmark report"
    # The marker is how a retry finds this draft again instead of duplicating it.
    assert f"{REFERENCE_PREFIX}{item.id}" in draft["body"]
    assert "prepared as a draft" in draft["body"]

    gmail_outcome = outcomes_by_target(result)[OperationTarget.GMAIL]
    assert DRAFT_NOT_SENT_NOTE in gmail_outcome.detail


def test_no_code_path_in_the_application_can_send_mail() -> None:
    """Drafts-only is structural: there is no send call anywhere to reach."""
    root = Path(app.__file__).resolve().parent
    forbidden = ("drafts().send", "messages().send")
    offenders = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if any(marker in path.read_text(encoding="utf-8") for marker in forbidden)
    ]

    assert offenders == [], "ActionLoop must only ever create Gmail drafts"


def test_only_the_narrowest_google_scopes_are_requested() -> None:
    """A leaked token cannot send mail, because that scope was never granted."""
    assert GMAIL_COMPOSE_SCOPE in GOOGLE_SCOPES
    assert not any("gmail.send" in scope for scope in GOOGLE_SCOPES)
    assert len(GOOGLE_SCOPES) == 2, "request the Calendar and draft scopes, nothing else"


