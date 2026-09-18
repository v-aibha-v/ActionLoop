"""Translate ``googleapiclient`` HTTP errors into ActionLoop errors.

Both Google clients need exactly this mapping, so it lives in one place. The
important decisions:

* A 401 means the stored credential is no longer accepted. That is a *session*
  problem, not a per-item failure, so it becomes ``NotAuthenticatedError`` and the
  API answers 401 with "reconnect" instead of marking the action item failed.
* Everything else becomes the caller's integration error, with the upstream
  message redacted and truncated before it reaches a log line or a response body.
"""

from __future__ import annotations

from googleapiclient.errors import HttpError

from app.core.exceptions import ActionLoopError, IntegrationError, NotAuthenticatedError, redact

_MAX_DETAIL_LENGTH = 400


def translate_http_error(
    error: HttpError, *, context: str, error_cls: type[IntegrationError]
) -> ActionLoopError:
    """Map an upstream HTTP error onto the ActionLoop error hierarchy."""
    status = getattr(getattr(error, "resp", None), "status", None)
    if status == 401:
        return NotAuthenticatedError(
            f"Google rejected the stored credential while {context}. "
            "Reconnect at /auth/google.",
            details={"status": status},
        )
    detail = redact(str(error))[:_MAX_DETAIL_LENGTH]
    return error_cls(
        f"Google API error while {context}.",
        details={"status": status, "detail": detail},
    )