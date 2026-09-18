"""Structured (JSON) logging.

Why not ``print`` or the default text format? Because when an external API call
fails you want to correlate *which* request, *which* action item and *which* Google
operation it happened in. Emitting one JSON object per log record keeps that
metadata machine-readable, and a request-id context variable means the API layer
does not have to thread it through every function signature.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

_request_id: ContextVar[str | None] = ContextVar("actionloop_request_id", default=None)

# Extra structured fields that are copied from the LogRecord when present.
_EXTRA_FIELDS = (
    "request_id",
    "transcript_id",
    "action_item_id",
    "operation",
    "provider",
    "duration_ms",
    "status",
)


def set_request_id(request_id: str | None) -> None:
    """Bind a request id for the duration of the current context."""
    _request_id.set(request_id)


def get_request_id() -> str | None:
    return _request_id.get()


class RequestIdFilter(logging.Filter):
    """Injects the ambient request id into every record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - stdlib API
        if not getattr(record, "request_id", None):
            record.request_id = _request_id.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with optional structured extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger exactly once."""
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own handlers; let them propagate to ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)