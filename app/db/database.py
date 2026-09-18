"""Engine, session factory and transaction boundary.

One ``Database`` instance lives for the process lifetime and hands out short-lived
sessions. Every service method that touches storage wraps its work in
``with db.session()``, which commits on success and rolls back on any exception.
That gives each business operation a single, obvious transaction boundary instead
of scattering commits through the codebase.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.db.models import Base

_IN_MEMORY_URLS = frozenset({"sqlite://", "sqlite:///:memory:", "sqlite:///:memory:"})


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
    """SQLite ignores foreign keys unless asked; opt in per connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class Database:
    """Thin wrapper around the SQLAlchemy engine."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        connect_args: dict[str, object] = {}
        engine_kwargs: dict[str, object] = {"echo": echo}

        if url.startswith("sqlite"):
            # FastAPI runs sync endpoints in a threadpool, so connections cross
            # threads. Also, an in-memory SQLite DB lives inside a single
            # connection, which StaticPool keeps alive for the whole test run.
            connect_args["check_same_thread"] = False
            if url in _IN_MEMORY_URLS or ":memory:" in url:
                engine_kwargs["poolclass"] = StaticPool

        self.engine = create_engine(url, connect_args=connect_args, **engine_kwargs)
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", _enable_sqlite_foreign_keys)

        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    @classmethod
    def from_settings(cls, settings: Settings) -> Database:
        return cls(settings.database_url)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    def drop_all(self) -> None:
        Base.metadata.drop_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transaction scope: commit on success, roll back on failure."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()