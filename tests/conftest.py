"""Shared test fixtures and fake external clients.

What is faked, and why
----------------------
The suite must never call Claude, Google Calendar or Gmail. Three seams are enough
to replace every network dependency:

* ``FakeAsyncAnthropic`` -- stands in for ``anthropic.AsyncAnthropic``. It records
  each request and returns scripted responses, including scripted failures.
* ``FakeCalendarClient`` / ``FakeGmailClient`` -- implement the same Protocols the
  real Google clients implement, so the services under test cannot tell the
  difference. They raise the *domain* error types
  (``CalendarIntegrationError``, ``NotAuthenticatedError``, ...) because that is
  exactly the contract the real clients honour after translating ``HttpError``.
* ``Database("sqlite://")`` -- an in-memory database with ``StaticPool``, so every
  session in a test shares one database and nothing is written to disk.

The container is assembled from these fakes, which means the API tests exercise
the real routes, the real services, the real state machine and the real
repositories -- only the outermost I/O is substituted.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agents.extraction_agent import ExtractionAgent
from app.agents.prompts import TOOL_NAME
from app.core.config import Settings
from app.core.container import AppContainer
from app.core.exceptions import CalendarIntegrationError, GmailIntegrationError
from app.db.database import Database
from app.integrations.google_auth import GoogleOAuthManager
from app.main import create_app
from app.models.action_item import ActionItem, ActionItemStatus

# ---------------------------------------------------------------------------
# Fake Anthropic client
# ---------------------------------------------------------------------------


@dataclass
class FakeToolUseBlock:
    """Mirrors ``anthropic.types.ToolUseBlock`` closely enough for the agent."""

    input: dict[str, Any]
    name: str = TOOL_NAME
    type: str = "tool_use"


@dataclass
class FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeResponse:
    content: list[Any] = field(default_factory=list)
    stop_reason: str = "tool_use"


class FakeMessagesResource:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeAsyncAnthropic ran out of scripted responses.")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeAsyncAnthropic:
    """Drop-in stand-in for the async Anthropic client."""

    def __init__(self, responses: list[Any]) -> None:
        self.messages = FakeMessagesResource(responses)


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


def tool_response(items: list[dict[str, Any]]) -> FakeResponse:
    """A well-formed ``record_action_items`` tool call."""
    return FakeResponse(content=[FakeToolUseBlock(input={"action_items": items})])


def sample_extraction(
    *,
    title: str = "Prepare benchmark report",
    owner: str | None = "Vaibhav",
    deadline: str | None = "2026-09-25T17:00:00Z",
    priority: str = "high",
    confidence: float = 0.95,
    description: str | None = "Write up the Q3 benchmark findings.",
    source_context: str = "I will prepare the benchmark report by September 25.",
) -> dict[str, Any]:
    """One item in the shape Claude is contracted to return."""
    return {
        "title": title,
        "description": description,
        "owner": owner,
        "deadline": deadline,
        "priority": priority,
        "source_context": source_context,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# Fake Google clients
# ---------------------------------------------------------------------------


class FakeCalendarClient:
    """Records what would have been sent to Google Calendar.

    ``fail_times`` simulates a transient upstream outage, which is how the
    "calendar failures are handled" test drives the FAILED path.
    """

    def __init__(
        self, *, fail_times: int = 0, existing_event: dict[str, Any] | None = None
    ) -> None:
        self.created: list[dict[str, Any]] = []
        self.lookups: list[str] = []
        self.fail_times = fail_times
        self.existing_event = existing_event

    def find_event_for_action_item(self, action_item_id: str) -> dict[str, Any] | None:
        self.lookups.append(action_item_id)
        return self.existing_event

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
        if self.fail_times > 0:
            self.fail_times -= 1
            raise CalendarIntegrationError("simulated Calendar outage")
        index = len(self.created) + 1
        event = {
            "id": f"cal-event-{index}",
            "htmlLink": f"https://calendar.google.com/calendar/event?eid=cal-event-{index}",
            "summary": summary,
            "description": description,
            "start": start,
            "duration_minutes": duration_minutes,
            "timezone": timezone,
            "action_item_id": action_item_id,
        }
        self.created.append(event)
        return event


class FakeGmailClient:
    """Records the drafts that would have been created. Never sends anything."""

    def __init__(
        self, *, fail_times: int = 0, existing_draft: dict[str, Any] | None = None
    ) -> None:
        self.created: list[dict[str, Any]] = []
        self.searches: list[str] = []
        self.fail_times = fail_times
        self.existing_draft = existing_draft
        self.user_email = "actionloop.demo@example.com"

    def get_user_email(self) -> str | None:
        return self.user_email

    def find_draft_for_action_item(self, action_item_id: str) -> dict[str, Any] | None:
        self.searches.append(action_item_id)
        return self.existing_draft

    def create_draft(
        self, *, action_item_id: str, subject: str, body: str, to: str | None = None
    ) -> dict[str, Any]:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise GmailIntegrationError("simulated Gmail outage")
        index = len(self.created) + 1
        draft = {
            "id": f"draft-{index}",
            "subject": subject,
            "body": body,
            "to": to,
            "action_item_id": action_item_id,
        }
        self.created.append(draft)
        return draft


@dataclass
class GoogleFakes:
    """Both fake Google clients, so a test can assert on what was attempted."""

    calendar: FakeCalendarClient
    gmail: FakeGmailClient

    @property
    def total_creates(self) -> int:
        return len(self.calendar.created) + len(self.gmail.created)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings with no real secrets and an isolated token path."""
    return Settings(
        _env_file=None,
        environment="test",
        log_level="WARNING",
        database_url="sqlite://",
        anthropic_api_key="sk-ant-test-key-not-real",
        anthropic_model="claude-test-model",
        google_client_id="test-client-id.apps.googleusercontent.com",
        google_client_secret="test-client-secret",
        google_redirect_uri="http://localhost:8000/auth/google/callback",
        google_token_path=str(tmp_path / "google_token.json"),
        gmail_follow_up_recipient="",
    )


@pytest.fixture
def database() -> Iterator[Database]:
    db = Database("sqlite://")
    db.create_all()
    yield db
    db.dispose()


@pytest.fixture
def google_fakes() -> GoogleFakes:
    return GoogleFakes(calendar=FakeCalendarClient(), gmail=FakeGmailClient())


@pytest.fixture
def anthropic_responses() -> list[Any]:
    """Override in a test to script the Claude responses."""
    return [tool_response([sample_extraction()])]


@pytest.fixture
def fake_anthropic(anthropic_responses: list[Any]) -> FakeAsyncAnthropic:
    return FakeAsyncAnthropic(anthropic_responses)


@pytest.fixture
def agent(settings: Settings, fake_anthropic: FakeAsyncAnthropic) -> ExtractionAgent:
    return ExtractionAgent(settings, client=fake_anthropic)


@pytest.fixture
def container(
    settings: Settings,
    database: Database,
    agent: ExtractionAgent,
    google_fakes: GoogleFakes,
) -> AppContainer:
    return AppContainer.build(
        settings=settings,
        database=database,
        extraction_agent=agent,
        oauth=GoogleOAuthManager(settings),
        calendar_client_factory=lambda: google_fakes.calendar,
        gmail_client_factory=lambda: google_fakes.gmail,
    )


@pytest.fixture
def client(container: AppContainer, settings: Settings) -> Iterator[TestClient]:
    """A real HTTP client over the real app, with only the I/O edges faked."""
    with TestClient(create_app(settings=settings, container=container)) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
def make_item(**overrides: Any) -> ActionItem:
    """Build an ActionItem for service-level tests without going through Claude."""
    defaults: dict[str, Any] = {
        "title": "Prepare benchmark report",
        "description": "Write up the Q3 benchmark findings.",
        "owner": "Vaibhav",
        "deadline": datetime(2026, 9, 25, 17, 0, tzinfo=timezone.utc),
        "priority": "high",
        "source_context": "I will prepare the benchmark report by September 25.",
        "confidence": 0.95,
    }
    defaults.update(overrides)
    return ActionItem(**defaults)


@pytest.fixture
def stored_item(database: Database) -> ActionItem:
    """An EXTRACTED item already persisted, ready for review tests."""
    from app.db.repository import ActionItemRepository

    item = make_item()
    with database.session() as session:
        ActionItemRepository(session).add(item)
    return item


@pytest.fixture
def approved_item(database: Database, stored_item: ActionItem) -> ActionItem:
    """The same item, moved to APPROVED through the real state machine."""
    from app.db.repository import ActionItemRepository

    approved = stored_item.with_status(ActionItemStatus.APPROVED)
    with database.session() as session:
        ActionItemRepository(session).save(approved)
    return approved