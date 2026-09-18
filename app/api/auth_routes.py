"""Google OAuth 2.0 routes.

Flow
----
1. ``GET /auth/google`` returns the Google consent URL (plus the CSRF state we
   issued). Returning JSON rather than a 302 is what lets the review UI redirect
   the browser itself while keeping this endpoint usable from curl.
2. Google redirects the browser back to ``/auth/google/callback?code=...&state=...``.
3. The callback validates ``state``, exchanges ``code`` for credentials, writes the
   token to the git-ignored cache path, and sends the browser back to the UI with a
   query flag the UI turns into a status message.
4. ``DELETE /auth/google`` forgets the cached token.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, status
from fastapi.responses import RedirectResponse

from app.api.dependencies import ContainerDep
from app.api.schemas import ErrorResponse, GoogleStatusResponse, OAuthStartResponse
from app.core.exceptions import GoogleAuthError

router = APIRouter(prefix="/auth", tags=["google-auth"])


@router.get(
    "/google",
    response_model=OAuthStartResponse,
    responses={502: {"model": ErrorResponse, "description": "OAuth client is not configured"}},
    summary="Start the Google OAuth flow",
)
def start_google_oauth(container: ContainerDep) -> OAuthStartResponse:
    state = container.oauth.issue_state()
    return OAuthStartResponse(
        authorization_url=container.oauth.authorization_url(state),
        state=state,
        scopes=list(container.settings.google_scopes),
    )


@router.get(
    "/google/callback",
    responses={
        400: {"model": ErrorResponse, "description": "Missing code or invalid state"},
        502: {"model": ErrorResponse, "description": "Token exchange failed"},
    },
    summary="OAuth redirect target (registered in Google Cloud Console)",
)
def google_oauth_callback(
    container: ContainerDep,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None, description="Set by Google when consent is refused"),
) -> RedirectResponse:
    if error:
        raise GoogleAuthError(f"Google returned an authorization error: {error}")
    if not code:
        raise GoogleAuthError("Google did not return an authorization code.")

    container.oauth.consume_state(state)
    container.oauth.exchange_code(code)
    return RedirectResponse(url="/?google=connected", status_code=status.HTTP_303_SEE_OTHER)


@router.get(
    "/google/status",
    response_model=GoogleStatusResponse,
    summary="Whether Google credentials are configured and usable",
)
def google_status(container: ContainerDep) -> GoogleStatusResponse:
    return GoogleStatusResponse(
        configured=container.oauth.is_configured,
        authenticated=container.oauth.is_authenticated(),
        scopes=list(container.settings.google_scopes),
    )


@router.delete(
    "/google",
    response_model=GoogleStatusResponse,
    summary="Forget the cached Google token",
)
def disconnect_google(container: ContainerDep) -> GoogleStatusResponse:
    container.oauth.clear_credentials()
    return GoogleStatusResponse(
        configured=container.oauth.is_configured,
        authenticated=False,
        scopes=list(container.settings.google_scopes),
    )