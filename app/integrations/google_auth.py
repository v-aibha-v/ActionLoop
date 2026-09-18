"""Google OAuth 2.0: consent URL, code exchange, token cache and refresh.

Why this is hand-rolled rather than a managed helper
----------------------------------------------------
ActionLoop is a single-user local tool, so the "installed app" style flow is
enough: build a consent URL, receive ``?code=`` on the redirect URI, exchange it
for a refresh token, cache that token on disk, and refresh it in place when it
expires. Two details are worth calling out:

* The OAuth client config is assembled from environment variables rather than a
  downloaded ``credentials.json``. Fewer secrets on disk, and nothing to
  accidentally commit.
* A ``state`` value is issued per authorization attempt and required back on the
  callback. Without it, any page could hit the callback with an attacker's code
  (login CSRF) and bind the app to the wrong Google account.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from contextlib import suppress
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from app.core.config import Settings
from app.core.exceptions import ActionLoopError, GoogleAuthError, NotAuthenticatedError, redact
from app.core.logging import get_logger

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
_STATE_TTL_SECONDS = 600


def build_google_service(credentials: Credentials, api: str, version: str):  # type: ignore[no-untyped-def]
    """Build a discovery client for ``api``/``version`` with the given credentials."""
    # Imported lazily: the discovery machinery is only needed once we actually
    # call Google, not while importing the application.
    from googleapiclient.discovery import build

    return build(api, version, credentials=credentials, cache_discovery=False)


class GoogleOAuthManager:
    """Owns everything about the Google credential lifecycle."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._logger = get_logger(__name__)
        self._pending_states: dict[str, float] = {}

    # ------------------------------------------------------------------
    # OAuth client configuration
    # ------------------------------------------------------------------
    @property
    def is_configured(self) -> bool:
        return self._settings.is_google_configured

    def _client_config(self) -> dict[str, object]:
        return {
            "web": {
                "client_id": self._settings.google_client_id,
                "client_secret": self._settings.google_client_secret,
                "auth_uri": GOOGLE_AUTH_URI,
                "token_uri": GOOGLE_TOKEN_URI,
                "redirect_uris": [self._settings.google_redirect_uri],
            }
        }

    def _build_flow(self) -> Flow:
        if not self.is_configured:
            raise GoogleAuthError(
                "Google OAuth is not configured. Set GOOGLE_CLIENT_ID and "
                "GOOGLE_CLIENT_SECRET in your .env file."
            )
        return Flow.from_client_config(
            self._client_config(),
            scopes=list(self._settings.google_scopes),
            redirect_uri=self._settings.google_redirect_uri,
        )

    # ------------------------------------------------------------------
    # Authorization
    # ------------------------------------------------------------------
    def issue_state(self) -> str:
        """Create and remember a one-time CSRF token for the consent redirect."""
        self._prune_states()
        state = secrets.token_urlsafe(32)
        self._pending_states[state] = time.monotonic()
        return state

    def consume_state(self, state: str | None) -> None:
        """Validate and invalidate a state value coming back from Google."""
        self._prune_states()
        if not state or state not in self._pending_states:
            raise GoogleAuthError(
                "The OAuth state value is missing or has expired. Start the "
                "connection flow again from /auth/google."
            )
        del self._pending_states[state]

    def _prune_states(self) -> None:
        cutoff = time.monotonic() - _STATE_TTL_SECONDS
        for state in [s for s, issued_at in self._pending_states.items() if issued_at < cutoff]:
            del self._pending_states[state]

    def authorization_url(self, state: str) -> str:
        flow = self._build_flow()
        url, _ = flow.authorization_url(
            access_type="offline",  # required to receive a refresh token
            include_granted_scopes="true",
            prompt="consent",
            state=state,
        )
        return url

    def exchange_code(self, code: str) -> Credentials:
        """Trade the authorization code for credentials and cache them."""
        flow = self._build_flow()
        try:
            flow.fetch_token(code=code)
        except ActionLoopError:
            raise
        except Exception as exc:  # noqa: BLE001 - boundary: normalise any oauthlib error
            self._logger.warning("OAuth code exchange failed", extra={"provider": "google"})
            raise GoogleAuthError(
                f"Failed to exchange the authorization code: {redact(str(exc))}"
            ) from exc
        credentials = flow.credentials
        self.save_credentials(credentials)
        return credentials

    # ------------------------------------------------------------------
    # Token cache
    # ------------------------------------------------------------------
    def save_credentials(self, credentials: Credentials) -> None:
        """Persist credentials, creating the directory with owner-only permissions."""
        token_file = self._settings.google_token_file
        token_file.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            with suppress(OSError):
                os.chmod(token_file.parent, 0o700)
        token_file.write_text(credentials.to_json(), encoding="utf-8")
        if os.name == "posix":
            with suppress(OSError):
                os.chmod(token_file, 0o600)

    def load_credentials(self) -> Credentials | None:
        token_file: Path = self._settings.google_token_file
        if not token_file.exists():
            return None
        try:
            data = json.loads(token_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._logger.warning("Cached Google token is unreadable", extra={"provider": "google"})
            raise GoogleAuthError(
                "The cached Google token could not be read. Delete it and reconnect."
            ) from exc
        return Credentials.from_authorized_user_info(data, scopes=list(self._settings.google_scopes))

    def is_authenticated(self) -> bool:
        """True when a credential exists and is usable (refreshing it if needed)."""
        try:
            self.get_credentials()
        except (NotAuthenticatedError, GoogleAuthError):
            return False
        return True

    def get_credentials(self) -> Credentials:
        """Return a usable credential, refreshing it when it has expired."""
        credentials = self.load_credentials()
        if credentials is None:
            raise NotAuthenticatedError(
                "ActionLoop is not connected to Google. Open /auth/google to grant access."
            )

        if credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except Exception as exc:  # noqa: BLE001 - boundary: refresh errors are opaque
                self._logger.warning("Google token refresh failed", extra={"provider": "google"})
                raise GoogleAuthError(
                    f"Could not refresh the Google credential: {redact(str(exc))}. "
                    "Reconnect at /auth/google."
                ) from exc
            self.save_credentials(credentials)

        if not credentials.valid:
            raise NotAuthenticatedError(
                "The cached Google credential is no longer usable. Reconnect at /auth/google."
            )
        return credentials

    def clear_credentials(self) -> bool:
        """Delete the cached token (used by the 'disconnect' endpoint)."""
        token_file: Path = self._settings.google_token_file
        if token_file.exists():
            token_file.unlink()
            self._logger.info("Cached Google token removed", extra={"provider": "google"})
            return True
        return False