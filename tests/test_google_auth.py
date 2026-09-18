"""Tests for the Google OAuth surface: status reporting and account identity.

The OAuth *flow* itself talks to Google and cannot be exercised here, but the
contract the review UI depends on can: the status endpoint must report what is
configured, what is authenticated, and which account is signed in -- without ever
leaking a credential, and without failing when Google cannot be reached.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import CALENDAR_EVENTS_SCOPE, GMAIL_COMPOSE_SCOPE
from app.core.container import AppContainer
from app.core.exceptions import GmailIntegrationError
from app.main import create_app


class StubOAuth:
    """A stand-in for ``GoogleOAuthManager`` with a controllable session state."""

    def __init__(self, *, authenticated: bool, configured: bool = True) -> None:
        self._authenticated = authenticated
        self._configured = configured

    @property
    def is_configured(self) -> bool:
        return self._configured

    def is_authenticated(self) -> bool:
        return self._authenticated


class UnreachableGmail:
    """A Gmail client whose profile lookup fails, like an expired token would."""

    def get_user_email(self) -> str | None:
        raise GmailIntegrationError("simulated Gmail outage")


@pytest.fixture
def build_client(settings: Any, database: Any, agent: Any, google_fakes: Any):
    """Build a real app around a stub OAuth session and the fake Google clients."""

    def _build(*, authenticated: bool, gmail: Any = None) -> TestClient:
        container = AppContainer.build(
            settings=settings,
            database=database,
            extraction_agent=agent,
            oauth=StubOAuth(authenticated=authenticated),
            calendar_client_factory=lambda: google_fakes.calendar,
            gmail_client_factory=lambda: gmail or google_fakes.gmail,
        )
        return TestClient(create_app(settings=settings, container=container))

    return _build


def test_status_reports_the_connected_account(build_client: Any) -> None:
    with build_client(authenticated=True) as client:
        body = client.get("/auth/google/status").json()

    assert body["configured"] is True
    assert body["authenticated"] is True
    assert body["connected_email"] == "actionloop.demo@example.com"


def test_status_reports_no_account_before_oauth(build_client: Any) -> None:
    """Not connected yet: the UI needs to know to show the 'Connect' button."""
    with build_client(authenticated=False) as client:
        body = client.get("/auth/google/status").json()

    assert body["authenticated"] is False
    assert body["connected_email"] is None


def test_status_degrades_to_unknown_when_google_is_unreachable(build_client: Any) -> None:
    """A status badge must never take the endpoint down with it."""
    with build_client(authenticated=True, gmail=UnreachableGmail()) as client:
        response = client.get("/auth/google/status")

    assert response.status_code == 200
    assert response.json()["authenticated"] is True
    assert response.json()["connected_email"] is None


def test_status_requests_only_the_scopes_the_feature_needs(build_client: Any) -> None:
    """The consent screen must ask for exactly what is used. No ``gmail.send``."""
    with build_client(authenticated=False) as client:
        scopes = client.get("/auth/google/status").json()["scopes"]

    assert scopes == [CALENDAR_EVENTS_SCOPE, GMAIL_COMPOSE_SCOPE]
    assert not any(scope.endswith("gmail.send") for scope in scopes)


def test_status_never_leaks_a_credential(build_client: Any, settings: Any) -> None:
    """The body is a status report, so the client secret must not appear in it."""
    with build_client(authenticated=True) as client:
        raw = client.get("/auth/google/status").text

    assert settings.google_client_secret not in raw
    assert settings.google_client_id not in raw


def test_health_reports_configuration_without_values(client: Any) -> None:
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["llm_configured"] is True  # the test settings carry a fake key
    assert set(body) >= {"status", "app", "version", "google_configured", "google_authenticated"}


def test_the_token_cache_lives_outside_version_control(settings: Any) -> None:
    """The cached refresh token is the most sensitive file this project writes."""
    repo_root = Path(__file__).resolve().parents[1]
    ignore_rules = (repo_root / ".gitignore").read_text(encoding="utf-8")

    assert ".secrets/" in ignore_rules
    assert "*token*.json" in ignore_rules
    assert ".env" in ignore_rules