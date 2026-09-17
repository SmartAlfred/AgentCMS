"""Engine and session management (#2).

One lazily-created engine per process, so importing the app never touches the
database (``GET /healthz`` must keep working while Postgres is down, and tests
can point ``DATABASE_URL`` at an ephemeral cluster before the first connect).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.domain.errors import DatabaseUnavailableError

logger = logging.getLogger("app.db")

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
# Reentrant on purpose: ``get_session_factory`` used to acquire this lock and then
# call ``get_engine``, which acquires the same lock -- a guaranteed self-deadlock in
# any fresh process on its first DB request (fixed in #27).
_lock = threading.RLock()


def get_engine() -> Engine:
    """Return the process-wide engine, creating it on first use."""

    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                settings = get_settings()
                logger.info("creating engine for %s", settings.safe_database_url())
                _engine = create_engine(
                    settings.database_url,
                    pool_pre_ping=True,
                    pool_size=settings.db_pool_size,
                    max_overflow=settings.db_pool_size,
                    pool_timeout=settings.db_pool_timeout,
                    echo=settings.db_echo,
                    future=True,
                )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the process-wide session factory."""

    global _session_factory
    if _session_factory is None:
        # Resolve the engine *before* taking the lock so the two lazy singletons never
        # nest an acquisition (see the RLock note above).
        engine = get_engine()
        with _lock:
            if _session_factory is None:
                _session_factory = sessionmaker(
                    bind=engine,
                    autoflush=False,
                    expire_on_commit=False,
                    future=True,
                )
    return _session_factory


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, rolled back on error."""

    factory = get_session_factory()
    with factory() as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for scripts (commits on success, rolls back on error)."""

    factory = get_session_factory()
    with factory() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def check_database() -> float:
    """Return round-trip latency in milliseconds, or raise if unreachable."""

    started = time.perf_counter()
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:  # pragma: no cover - exercised via /readyz with DB down
        raise DatabaseUnavailableError(f"The database is not reachable: {type(exc).__name__}.") from exc
    return (time.perf_counter() - started) * 1000


def dispose_engine() -> None:
    """Close pooled connections (shutdown, or between test clusters)."""

    global _engine, _session_factory
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _session_factory = None
