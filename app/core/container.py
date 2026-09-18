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
        gmail = GmailService(
            client_provider=gmail_client_factory or default_gmail_client_factory(oauth, settings),
            settings=settings,
        )

        return cls(
            settings=settings,
            database=database,
            oauth=oauth,
            extraction_agent=agent,
            extraction_service=ExtractionService(database, agent, settings),
            approval_service=ApprovalService(database),
            execution_service=ExecutionService(database, calendar, gmail),
        )

    def initialize_schema(self) -> None:
        """Create tables if they do not exist yet.

        A local single-user tool does not need a migration chain yet; this is the
        one call that would be replaced by Alembic in a multi-environment deploy.
        """
        self.database.create_all()