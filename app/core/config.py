"""Application configuration.

Every environment-dependent value lives here and nowhere else. Modules receive a
``Settings`` instance instead of reading ``os.environ`` directly, which keeps them
trivially testable: a test can build ``Settings(_env_file=None)`` with overrides
and never touch the real environment or a developer's ``.env`` file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Google OAuth scopes
#
# ActionLoop only ever needs to (a) create Calendar events and (b) create Gmail
# drafts. Requesting nothing more keeps the OAuth consent screen honest and the
# blast radius of a leaked token small. Note the deliberate absence of
# `gmail.send`: the application cannot send mail even if it wanted to.
# ---------------------------------------------------------------------------
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GOOGLE_SCOPES: tuple[str, ...] = (CALENDAR_EVENTS_SCOPE, GMAIL_COMPOSE_SCOPE)

_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class Settings(BaseSettings):
    """Typed, validated view over the process environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application -------------------------------------------------------
    app_name: str = "ActionLoop"
    environment: str = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./actionloop.db"

    # --- Claude / Anthropic -------------------------------------------------
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5"
    anthropic_max_tokens: int = Field(default=4096, ge=256, le=64_000)
    anthropic_timeout_seconds: float = Field(default=60.0, gt=0, le=600)

    # --- Guardrails --------------------------------------------------------
    max_transcript_chars: int = Field(default=40_000, ge=500, le=400_000)
    max_extracted_items: int = Field(default=50, ge=1, le=200)

    # --- Google OAuth ------------------------------------------------------
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    google_token_path: str = ".secrets/google_token.json"

    # --- Google Calendar ---------------------------------------------------
    google_calendar_id: str = "primary"
    google_calendar_timezone: str = "UTC"
    calendar_event_duration_minutes: int = Field(default=30, ge=5, le=480)

    # --- Gmail -------------------------------------------------------------
    gmail_follow_up_recipient: str = ""

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------
    @field_validator(
        "anthropic_api_key",
        "anthropic_model",
        "google_client_id",
        "google_client_secret",
        "google_redirect_uri",
        "google_token_path",
        "google_calendar_id",
        "google_calendar_timezone",
        "gmail_follow_up_recipient",
        "database_url",
        mode="before",
    )
    @classmethod
    def _strip_strings(cls, value: object) -> object:
        """Trim whitespace so a copied-pasted secret with a stray space still works."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            level = value.strip().upper()
            if level not in _VALID_LOG_LEVELS:
                raise ValueError(f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}")
            return level
        return value

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------
    @property
    def google_token_file(self) -> Path:
        """Absolute path of the cached OAuth token."""
        return Path(self.google_token_path).expanduser().resolve()

    @property
    def google_scopes(self) -> tuple[str, ...]:
        return GOOGLE_SCOPES

    @property
    def is_anthropic_configured(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def is_google_configured(self) -> bool:
        """OAuth client credentials are present (does not imply a cached token)."""
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (cached so ``.env`` is parsed once)."""
    return Settings()