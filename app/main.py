"""FastAPI application factory.

Three responsibilities live here, and nowhere else:

* **Wiring** -- build the container, attach it to ``app.state``, create the schema.
* **Request context** -- assign a request id, log one structured line per request
  with a duration.
* **Error translation** -- a single handler turns any ``ActionLoopError`` into the
  documented error envelope, and an unexpected exception into a generic 500 that
  leaks nothing about internals or credentials.

Run it with the factory form so that merely importing this module has no side
effects (no database file is created until the server actually starts):

    uvicorn app.main:create_app --factory --reload
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import auth_routes, routes
from app.core.config import Settings, get_settings
from app.core.container import AppContainer
from app.core.exceptions import ActionLoopError
from app.core.logging import configure_logging, get_logger, set_request_id

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _error_response(status_code: int, code: str, message: str, details=None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


def register_exception_handlers(app: FastAPI) -> None:
    logger = get_logger("actionloop.errors")

    @app.exception_handler(ActionLoopError)
    async def _handle_actionloop_error(_request: Request, exc: ActionLoopError) -> JSONResponse:
        # Domain errors carry their own status code and stable error code, so the
        # handler stays a one-liner no matter how many error types are added.
        if exc.status_code >= 500:
            logger.warning("Request failed", extra={"status": exc.code})
        return _error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            "The request body or parameters failed validation.",
            {"errors": [{"location": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
                        for e in exc.errors()]},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        # Log the detail, return none of it: an unexpected traceback can contain
        # configuration values or request payloads.
        logger.error("Unhandled exception", exc_info=exc)
        return _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "An unexpected internal error occurred.",
        )


def register_middleware(app: FastAPI) -> None:
    logger = get_logger("actionloop.request")

    @app.middleware("http")
    async def _request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        set_request_id(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Let the exception handlers build the response, but still log timing.
            logger.warning(
                "Request raised",
                extra={"operation": "http", "status": "error",
                       "duration_ms": int((time.perf_counter() - started) * 1000)},
            )
            raise
        duration_ms = int((time.perf_counter() - started) * 1000)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "Request handled",
            extra={"operation": "http", "status": response.status_code, "duration_ms": duration_ms},
        )
        return response


def create_app(
    *,
    settings: Settings | None = None,
    container: AppContainer | None = None,
) -> FastAPI:
    """Build the application.

    ``container`` is injectable so the test suite can substitute fake Claude and
    Google clients and exercise the real routes end to end.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    resolved = container or AppContainer.build(settings=settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        resolved.initialize_schema()
        yield
        resolved.database.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Extract structured action items from meeting transcripts, review and "
            "approve them, then create Google Calendar events and Gmail drafts. "
            "No external side effect happens before an item is approved."
        ),
        lifespan=lifespan,
    )
    app.state.container = resolved
    app.state.settings = settings

    register_middleware(app)
    register_exception_handlers(app)

    app.include_router(routes.system_router)
    app.include_router(routes.router)
    app.include_router(auth_routes.router)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def review_interface() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app