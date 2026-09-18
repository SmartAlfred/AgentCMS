"""Ephemeral PostgreSQL for the test suite (#2).

``make test`` must never depend on a database someone else left running, and it
must never leave state behind.  Strategy, in order of preference:

1. ``TEST_DATABASE_URL`` — use a server provided by the caller (CI service
   container).  The suite still creates and drops its own databases there.
2. Docker — start a throwaway ``postgres:16-alpine`` container on a free port,
   which is the version the project targets.
3. ``initdb``/``pg_ctl`` — a throwaway cluster from the local Postgres install,
   so the suite works on a laptop with no Docker daemon.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parents[1]
PG_IMAGE = os.environ.get("TEST_POSTGRES_IMAGE", "postgres:16-alpine")
TEST_DB_NAME = "agentcms_test"
_LOCAL_PG_BIN_GLOBS = (
    "/opt/homebrew/opt/postgresql@16/bin",
    "/opt/homebrew/opt/postgresql@18/bin",
    "/opt/homebrew/opt/postgresql/bin",
    "/usr/lib/postgresql/16/bin",
    "/usr/local/pgsql/bin",
)


def _run(cmd: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(cmd), capture_output=True, text=True, check=False, **kwargs)  # type: ignore[arg-type]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def docker_available() -> bool:
    if os.environ.get("AGENTCMS_TEST_NO_DOCKER"):
        return False
    if shutil.which("docker") is None:
        return False
    return _run(["docker", "info"]).returncode == 0


def _wait_for_dsn(dsn: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    engine = create_engine(dsn, pool_pre_ping=True)
    try:
        while time.time() < deadline:
            try:
                with engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
                return True
            except Exception:
                time.sleep(0.5)
        return False
    finally:
        engine.dispose()


def make_url_safe(url: str) -> str:
    """URL with the password masked, for log lines."""

    return make_url(url).render_as_string(hide_password=True)


def with_database(url: str, database: str) -> str:
    return make_url(url).set(database=database).render_as_string(hide_password=False)


@dataclass
class EphemeralPostgres:
    """A throwaway server plus the cleanup needed to remove it."""

    url: str
    mode: str
    admin_url: str
    cleanup: Callable[[], None] = field(repr=False)
    log: str = ""

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<EphemeralPostgres mode={self.mode} url={self.url}>"


def _cleanup_docker(container: str) -> None:
    _run(["docker", "rm", "-f", container])


def start_docker_postgres() -> EphemeralPostgres:
    container = f"agentcms-test-pg-{uuid.uuid4().hex[:8]}"
    port = free_port()
    start = _run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            container,
            "-e",
            "POSTGRES_USER=postgres",
            "-e",
            "POSTGRES_PASSWORD=postgres",
            "-e",
            f"POSTGRES_DB={TEST_DB_NAME}",
            "-e",
            "POSTGRES_INITDB_ARGS=--no-sync",
            "-p",
            f"{port}:5432",
            "--tmpfs",
            "/tmp:rw",
            PG_IMAGE,
            "postgres",
            "-c",
            "fsync=off",
            "-c",
            "full_page_writes=off",
        ]
    )
    if start.returncode != 0:
        raise RuntimeError(f"docker run failed: {start.stderr.strip()}")

    url = f"postgresql+psycopg://postgres:postgres@127.0.0.1:{port}/{TEST_DB_NAME}"
    if not _wait_for_dsn(with_database(url, "postgres"), timeout=90):
        _cleanup_docker(container)
        raise RuntimeError("docker postgres did not become ready within 90s")

    logs = _run(["docker", "logs", "--tail", "20", container])
    return EphemeralPostgres(
        url=url,
        mode=f"docker ({PG_IMAGE})",
        admin_url=with_database(url, "postgres"),
        cleanup=lambda: _cleanup_docker(container),
        log=logs.stdout + logs.stderr,
    )


def _local_pg_bin() -> Path | None:
    for candidate in _LOCAL_PG_BIN_GLOBS:
        path = Path(candidate)
        if (path / "initdb").exists():
            return path
    for name in ("initdb", "pg_ctl"):
        found = shutil.which(name)
        if found:
            return Path(found).parent
    return None


def start_initdb_postgres() -> EphemeralPostgres:
    bindir = _local_pg_bin()
    if bindir is None:
        raise RuntimeError("no initdb/pg_ctl on this machine")

    datadir = Path(tempfile.mkdtemp(prefix="agentcms-pgdata-"))
    sockdir = Path(tempfile.mkdtemp(prefix="agentcms-pgsock-"))
    logfile = datadir / "server.log"
    port = free_port()

    init = _run(
        [
            str(bindir / "initdb"),
            "-D",
            str(datadir),
            "-U",
            "postgres",
            "-A",
            "trust",
            "--no-sync",
            "-E",
            "UTF8",
        ]
    )
    if init.returncode != 0:
        shutil.rmtree(datadir, ignore_errors=True)
        shutil.rmtree(sockdir, ignore_errors=True)
        raise RuntimeError(f"initdb failed: {init.stderr.strip()}")

    options = (
        f"-p {port} -k {sockdir} -c listen_addresses=127.0.0.1 "
        "-c fsync=off -c full_page_writes=off -c timezone=UTC"
    )
    started = _run(
        [
            str(bindir / "pg_ctl"),
            "-D",
            str(datadir),
            "-l",
            str(logfile),
            "-o",
            options,
            "-w",
            "-t",
            "60",
            "start",
        ]
    )
    if started.returncode != 0:
        shutil.rmtree(datadir, ignore_errors=True)
        shutil.rmtree(sockdir, ignore_errors=True)
        raise RuntimeError(f"pg_ctl start failed: {started.stderr.strip()}")

    version = _run([str(bindir / "postgres"), "--version"]).stdout.strip() or "unknown version"
    url = f"postgresql+psycopg://postgres@127.0.0.1:{port}/{TEST_DB_NAME}"
    admin_url = with_database(url, "postgres")
    if not _wait_for_dsn(admin_url, timeout=30):
        _run([str(bindir / "pg_ctl"), "-D", str(datadir), "-m", "immediate", "stop"])
        raise RuntimeError("initdb postgres did not become ready within 30s")

    created = _run(
        [
            "psql",
            "-h",
            "127.0.0.1",
            "-p",
            str(port),
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-c",
            f"CREATE DATABASE {TEST_DB_NAME}",
        ]
    )
    if created.returncode != 0:
        # No psql on PATH: fall back to psycopg via SQLAlchemy.
        engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
        engine.dispose()

    def cleanup() -> None:
        _run([str(bindir / "pg_ctl"), "-D", str(datadir), "-m", "immediate", "stop"])
        shutil.rmtree(datadir, ignore_errors=True)
        shutil.rmtree(sockdir, ignore_errors=True)

    return EphemeralPostgres(
        url=url,
        mode=f"initdb ({version})",
        admin_url=admin_url,
        cleanup=cleanup,
        log=str(logfile),
    )


def start_ephemeral_postgres() -> EphemeralPostgres:
    """Return a fresh server, using whichever strategy this machine supports."""

    external = os.environ.get("TEST_DATABASE_URL")
    if external:
        return EphemeralPostgres(
            url=external,
            mode="TEST_DATABASE_URL",
            admin_url=with_database(external, "postgres"),
            cleanup=lambda: None,
        )

    errors: list[str] = []
    if docker_available():
        try:
            return start_docker_postgres()
        except Exception as exc:
            errors.append(f"docker: {exc}")
    try:
        return start_initdb_postgres()
    except Exception as exc:
        errors.append(f"initdb: {exc}")
    raise RuntimeError("no ephemeral PostgreSQL available (" + "; ".join(errors) + ")")


# --- databases on an existing server ---------------------------------------


def create_database(admin_url: str, name: str) -> None:
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine.dispose()


def drop_database(admin_url: str, name: str) -> None:
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        connection.execute(
            text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :name"),
            {"name": name},
        )
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    engine.dispose()


def list_tables(database_url: str) -> list[str]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
            ).all()
        return [row[0] for row in rows]
    finally:
        engine.dispose()


# --- migrations -------------------------------------------------------------


def run_alembic(*args: str, database_url: str) -> subprocess.CompletedProcess[str]:
    """Invoke Alembic in a subprocess against a specific database."""

    env = dict(os.environ, DATABASE_URL=database_url, APP_ENV="test")
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def alembic_ok(*args: str, database_url: str) -> str:
    result = run_alembic(*args, database_url=database_url)
    if result.returncode != 0:
        raise AssertionError(
            f"alembic {' '.join(args)} failed ({result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout
