"""Composition root: the single place where objects are constructed and wired.

Why a container instead of module-level singletons or scattered ``Depends``
factories? Because external dependencies (the Claude client, the Google clients,
the database) are the things tests must replace. One ``AppContainer`` that takes
overrides for those three seams means a test can build a complete application with
no network access in three lines -- and nothing else in the codebase needs to know
how wiring works.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.agents.extraction_agent import ExtractionAgent
from app.core.config import Settings, get_settings
from app.core.exceptions import ActionLoopError
from app.core.logging import get_logger
from app.db.database import Database
from app.integrations.google_auth import GoogleOAuthManager, build_google_service
from app.integrations.google_calendar import CalendarClient, GoogleCalendarClient
from app.integrations.google_gmail import GmailClient, GoogleGmailClient
from app.services.approval_service import ApprovalService
from app.services.calendar_service import CalendarService
from app.services.execution_service import ExecutionService
from app.services.extraction_service import ExtractionService
from app.services.gmail_service import GmailService


def default_calendar_client_factory(
    oauth: GoogleOAuthManager, settings: Settings
) -> Callable[[], CalendarClient]:
    """Resolve credentials *per call* so a refreshed token is always picked up."""

    def factory() -> CalendarClient:
        service = build_google_service(oauth.get_credentials(), "calendar", "v3")
        return GoogleCalendarClient(service, calendar_id=settings.google_calendar_id)

    return factory


def default_gmail_client_factory(
    oauth: GoogleOAuthManager, settings: Settings
) -> Callable[[], GmailClient]:
    def factory() -> GmailClient:
        service = build_google_service(oauth.get_credentials(), "gmail", "v1")
        return GoogleGmailClient(service)

    return factory


@dataclass
class AppContainer:
    """Every long-lived object the application needs, already wired."""

    settings: Settings
    database: Database
    oauth: GoogleOAuthManager
    extraction_agent: ExtractionAgent
    extraction_service: ExtractionService
    approval_service: ApprovalService
    execution_service: ExecutionService
    # Kept on the container (rather than only inside GmailService) so the status
    # endpoint can ask Google *which account* is connected.
    gmail_client_factory: Callable[[], GmailClient]

    @classmethod
    def build(
        cls,
        *,
        settings: Settings | None = None,
        database: Database | None = None,
        extraction_agent: ExtractionAgent | None = None,
        oauth: GoogleOAuthManager | None = None,
        calendar_client_factory: Callable[[], CalendarClient] | None = None,
        gmail_client_factory: Callable[[], GmailClient] | None = None,
    ) -> AppContainer:
        settings = settings or get_settings()
        database = database or Database.from_settings(settings)
        oauth = oauth or GoogleOAuthManager(settings)
        agent = extraction_agent or ExtractionAgent(settings)

        calendar = CalendarService(
            client_provider=calendar_client_factory
            or default_calendar_client_factory(oauth, settings),
            settings=settings,
        )
        gmail_factory = gmail_client_factory or default_gmail_client_factory(oauth, settings)
        gmail = GmailService(client_provider=gmail_factory, settings=settings)

        return cls(
            settings=settings,
            database=database,
            oauth=oauth,
            extraction_agent=agent,
            extraction_service=ExtractionService(database, agent, settings),
            approval_service=ApprovalService(database),
            execution_service=ExecutionService(database, calendar, gmail),
            gmail_client_factory=gmail_factory,
        )

    def connected_google_account(self) -> str | None:
        """The signed-in Google address, or ``None`` when it cannot be determined.

        Deliberately best-effort. This value decorates a status badge, so an expired
        token or a Gmail hiccup must degrade to "unknown" instead of turning the
        health/status endpoint into a failure.
        """
        if not self.oauth.is_authenticated():
            return None
        try:
            return self.gmail_client_factory().get_user_email()
        except ActionLoopError:
            get_logger(__name__).warning(
                "Could not read the connected Google account",
                extra={"provider": "gmail", "operation": "read_profile"},
            )
            return None

    def initialize_schema(self) -> None:
        """Create tables if they do not exist yet.

        A local single-user tool does not need a migration chain yet; this is the
        one call that would be replaced by Alembic in a multi-environment deploy.
        """
        self.database.create_all()