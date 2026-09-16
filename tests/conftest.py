"""Shared fixtures (#2).

The suite runs against a throwaway PostgreSQL server: the whole server is
created for the session and destroyed afterwards, and each test starts from an
empty schema (``TRUNCATE … RESTART IDENTITY CASCADE``), so tests cannot leak
state into each other or into the next run.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests import pg as pg_mod

ALEMBIC_TABLE = "alembic_version"
# Inserted by fixtures, kept in FK-dependency order.
TRUNCATE_CANDIDATES = (
    "post_tags",
    "tags",
    "post_revisions",
    "posts",
    "sites",
    "actors",
    "audit_events",
    "capability_links",
    "idempotency_keys",
    "assets",
    "webhooks",
    "webhook_deliveries",
    "redirects",
)


@pytest.fixture(scope="session")
def postgres_server() -> Iterator[pg_mod.EphemeralPostgres]:
    try:
        server = pg_mod.start_ephemeral_postgres()
    except RuntimeError as exc:
        pytest.skip(f"no ephemeral PostgreSQL: {exc}")
    print(f"\n[conftest] ephemeral postgres: {server.mode} -> {pg_mod.make_url_safe(server.url)}")
    yield server
    server.cleanup()


@pytest.fixture(scope="session")
def database_url(postgres_server: pg_mod.EphemeralPostgres) -> Iterator[str]:
    """The migrated test database, wired into settings before the app is built."""

    os.environ["DATABASE_URL"] = postgres_server.url
    os.environ.setdefault("APP_ENV", "test")
    from app.config import get_settings, reset_settings_cache

    reset_settings_cache()
    pg_mod.alembic_ok("upgrade", "head", database_url=postgres_server.url)
    assert get_settings().database_url == postgres_server.url
    yield postgres_server.url


@pytest.fixture(scope="session")
def app(database_url: str):
    """A FastAPI app instance bound to the ephemeral database."""

    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        app_env="test",
        database_url=database_url,
        secret_key="test-secret-key-that-is-long-enough-000000",
        log_level="WARNING",
    )
    return create_app(settings)


@pytest.fixture()
def db(database_url: str, app) -> Iterator[Session]:
    """An empty database and a session for the duration of one test."""

    from app.db.session import session_scope

    _truncate_all(database_url)
    with session_scope() as session:
        yield session


def _truncate_all(database_url: str) -> None:
    from app.db.session import get_engine

    with get_engine().connect() as connection:
        existing = {
            row[0]
            for row in connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        }
    targets = [name for name in TRUNCATE_CANDIDATES if name in existing]
    if not targets:
        return
    statement = "TRUNCATE TABLE " + ", ".join(targets) + " RESTART IDENTITY CASCADE"
    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text(statement))


@pytest.fixture()
def client(db: Session, app) -> Iterator[TestClient]:
    """HTTP client for the app under test (no lifespan, so the engine survives)."""

    with TestClient(app) as test_client:
        test_client.headers.update({"User-Agent": "agentcms-tests/1.0"})
        yield test_client


@pytest.fixture()
def blank_database(postgres_server: pg_mod.EphemeralPostgres) -> Iterator[str]:
    """A brand-new empty database on the same server (used for migration tests)."""

    name = f"agentcms_blank_{uuid.uuid4().hex[:8]}"
    pg_mod.create_database(postgres_server.admin_url, name)
    url = pg_mod.with_database(postgres_server.url, name)
    try:
        yield url
    finally:
        pg_mod.drop_database(postgres_server.admin_url, name)
