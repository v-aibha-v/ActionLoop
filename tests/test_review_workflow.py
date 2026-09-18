"""Tests for the human review workflow: extraction -> edit -> approve / reject.

These tests run against the real API, the real services, the real state machine and
a real (in-memory) database. Only Claude and Google are faked, so what is asserted
here is the behaviour a client actually gets over HTTP -- including status codes and
the error envelope -- rather than the shape of an internal helper.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import ActionItemNotFoundError, ItemNotEditableError
from app.models.action_item import (
    ALLOWED_TRANSITIONS,
    ActionItemEdit,
    ActionItemStatus,
    can_transition,
)
from app.services.approval_service import ApprovalService

TRANSCRIPT = "Vaibhav: I will prepare the benchmark report by September 25."


def extract_one(client: TestClient) -> dict[str, Any]:
    """Extract the single item the fake Claude is scripted to return."""
    response = client.post("/api/action-items/extract", json={"transcript": TRANSCRIPT})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["item_count"] == 1
    return body["items"][0]


# ---------------------------------------------------------------------------
# Extraction -> review queue
# ---------------------------------------------------------------------------
def test_extraction_lands_in_the_review_queue_as_extracted(client: TestClient) -> None:
    """The happy path: a transcript becomes persisted, reviewable action items."""
    item = extract_one(client)

    assert item["status"] == ActionItemStatus.EXTRACTED.value
    assert item["priority"] == "high"
    assert item["title"] == "Prepare benchmark report"
    assert item["transcript_id"] is not None

    # It is really persisted, not just echoed back in the response body.
    fetched = client.get(f"/api/action-items/{item['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == item["id"]
    assert fetched.json()["calendar_event_id"] is None
    assert fetched.json()["gmail_draft_id"] is None


def test_listing_can_be_filtered_by_status(client: TestClient) -> None:
    item = extract_one(client)
    client.post(f"/api/action-items/{item['id']}/approve")

    everything = client.get("/api/action-items").json()
    approved = client.get("/api/action-items", params={"status": "approved"}).json()
    rejected = client.get("/api/action-items", params={"status": "rejected"}).json()

    assert everything["total"] == 1
    assert [i["id"] for i in approved["items"]] == [item["id"]]
    assert rejected["total"] == 0


def test_extraction_requires_exactly_one_transcript_source(client: TestClient) -> None:
    """Both sources or neither is a client bug, and the schema says so."""
    both = client.post(
        "/api/action-items/extract",
        json={"transcript": TRANSCRIPT, "transcript_id": str(uuid4())},
    )
    neither = client.post("/api/action-items/extract", json={})

    for response in (both, neither):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"


def test_unknown_action_item_returns_404_for_every_operation(client: TestClient) -> None:
    missing = uuid4()
    responses = [
        client.get(f"/api/action-items/{missing}"),
        client.patch(f"/api/action-items/{missing}", json={"owner": "Someone"}),
        client.post(f"/api/action-items/{missing}/approve"),
        client.post(f"/api/action-items/{missing}/reject"),
        client.post(f"/api/action-items/{missing}/execute"),
    ]

    for response in responses:
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "action_item_not_found"


# ---------------------------------------------------------------------------
# Editing during review
# ---------------------------------------------------------------------------
def test_reviewer_can_edit_an_extracted_item(client: TestClient) -> None:
    item = extract_one(client)

    response = client.patch(
        f"/api/action-items/{item['id']}",
        json={"title": "Prepare the Q3 benchmark report", "owner": "Priya", "priority": "low"},
    )

    assert response.status_code == 200
    updated = response.json()
    assert updated["title"] == "Prepare the Q3 benchmark report"
    assert updated["owner"] == "Priya"
    assert updated["priority"] == "low"
    # Editing is not a lifecycle change: the item still awaits a decision.
    assert updated["status"] == ActionItemStatus.EXTRACTED.value
    assert client.get(f"/api/action-items/{item['id']}").json()["owner"] == "Priya"


def test_editing_can_clear_an_owner_but_cannot_be_empty(client: TestClient) -> None:
    """An explicit null clears a field; an empty body is a mistake and is rejected."""
    item = extract_one(client)

    cleared = client.patch(f"/api/action-items/{item['id']}", json={"owner": None})
    empty = client.patch(f"/api/action-items/{item['id']}", json={})

    assert cleared.status_code == 200
    assert cleared.json()["owner"] is None
    assert empty.status_code == 422


def test_edit_is_validated_before_it_is_stored(client: TestClient) -> None:
    """A reviewer cannot save a title the extraction contract would have rejected."""
    item = extract_one(client)

    response = client.patch(f"/api/action-items/{item['id']}", json={"title": "ab"})

    assert response.status_code == 422
    stored = client.get(f"/api/action-items/{item['id']}").json()
    assert stored["title"] == "Prepare benchmark report"


def test_server_owned_fields_cannot_be_edited_by_a_client(client: TestClient) -> None:
    """Status and the side-effect ids are server-owned; a client cannot set them."""
    item = extract_one(client)

    response = client.patch(
        f"/api/action-items/{item['id']}", json={"status": "executed", "confidence": 1.0}
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# The approval gate
# ---------------------------------------------------------------------------
def test_approve_then_reject_is_an_illegal_transition(client: TestClient) -> None:
    item = extract_one(client)
    assert client.post(f"/api/action-items/{item['id']}/approve").status_code == 200

    second = client.post(f"/api/action-items/{item['id']}/approve")
    flip = client.post(f"/api/action-items/{item['id']}/reject")

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "invalid_state_transition"
    assert flip.status_code == 409
    assert flip.json()["error"]["details"]["current_status"] == "approved"


def test_rejection_is_terminal_and_records_a_timestamp(client: TestClient) -> None:
    item = extract_one(client)

    rejected = client.post(f"/api/action-items/{item['id']}/reject")

    assert rejected.status_code == 200
    assert rejected.json()["status"] == ActionItemStatus.REJECTED.value
    assert rejected.json()["rejected_at"] is not None
    assert client.post(f"/api/action-items/{item['id']}/approve").status_code == 409


def test_approval_closes_the_review_gate(client: TestClient) -> None:
    """Once approved, the reviewed content is frozen until it is executed."""
    item = extract_one(client)
    client.post(f"/api/action-items/{item['id']}/approve")

    response = client.patch(f"/api/action-items/{item['id']}", json={"title": "Changed my mind"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "action_item_not_editable"


def test_approval_is_timestamped_for_the_audit_trail(client: TestClient) -> None:
    item = extract_one(client)

    approved = client.post(f"/api/action-items/{item['id']}/approve").json()

    assert approved["status"] == ActionItemStatus.APPROVED.value
    assert approved["approved_at"] is not None
    assert approved["executed_at"] is None


# ---------------------------------------------------------------------------
# State machine + service-level guards
# ---------------------------------------------------------------------------
def test_terminal_states_cannot_be_escaped() -> None:
    """The lifecycle table is the single source of truth, so pin its edges."""
    assert ALLOWED_TRANSITIONS[ActionItemStatus.REJECTED] == frozenset()
    assert ALLOWED_TRANSITIONS[ActionItemStatus.EXECUTED] == frozenset()
    # The approval gate can be neither skipped nor reversed.
    assert not can_transition(ActionItemStatus.EXTRACTED, ActionItemStatus.EXECUTED)
    assert not can_transition(ActionItemStatus.EXECUTED, ActionItemStatus.APPROVED)
    assert can_transition(ActionItemStatus.EXTRACTED, ActionItemStatus.APPROVED)
    assert can_transition(ActionItemStatus.APPROVED, ActionItemStatus.EXECUTED)


def test_service_refuses_to_edit_a_rejected_item(database: Any, stored_item: Any) -> None:
    """The rule lives in the service, so it holds without the UI or the API."""
    service = ApprovalService(database)
    service.reject(stored_item.id)

    with pytest.raises(ItemNotEditableError):
        service.edit(stored_item.id, ActionItemEdit(owner="Someone"))


def test_service_reports_a_missing_item_as_not_found(database: Any) -> None:
    service = ApprovalService(database)

    with pytest.raises(ActionItemNotFoundError):
        service.approve(uuid4())


