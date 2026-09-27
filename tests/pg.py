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
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
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


def _locale_env() -> dict[str, str]:
    """Return an environment dict with locale variables pinned for initdb.

    PostgreSQL 18+ refuses to run initdb when LANG, LC_ALL, and LC_CTYPE are
    all unset. This helper ensures they are set to C.UTF-8 (or preserves any
    existing values) so that a throwaway cluster can be created in minimal
    environments (containers, CI, etc.).
    """
    env = dict(os.environ)
    for var, default in (("LANG", "C.UTF-8"), ("LC_ALL", "C.UTF-8"), ("LC_CTYPE", "C.UTF-8")):
        env.setdefault(var, default)
    return env


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


# The *name* is the contract: `agentcms-drill-pg-<hex>` is what
# `test_backup_restore_drill._postgres_server_inner` starts, `agentcms-test-pg-<hex>` what
# `start_docker_postgres` starts for the whole session. Both families leak the same way, so
# both are in one documented tuple, one entry per family. Reaping by *image* would instead
# match a self-hoster's own `postgres:16-alpine` stack, where `docker rm -f` is data loss,
# not cleanup -- so a name outside this contract is never reaped.
DRILL_CONTAINER_PREFIX = "agentcms-drill-pg-"
SUITE_CONTAINER_PREFIX = "agentcms-test-pg-"
TEST_CONTAINER_PREFIXES = (DRILL_CONTAINER_PREFIX, SUITE_CONTAINER_PREFIX)
_TEST_CONTAINER_RE = re.compile(
    "|".join(rf"/?{re.escape(prefix)}[0-9a-f]+$" for prefix in TEST_CONTAINER_PREFIXES)
)


def _is_reapable_test_container(name: str) -> bool:
    """True for a throwaway container this repo started, and nothing else."""
    return _TEST_CONTAINER_RE.match(name.strip()) is not None


def _container_age_seconds(created: str, now: float | None = None) -> float | None:
    """Age in seconds of one `docker inspect .Created` (RFC 3339) timestamp, or None."""
    match = re.match(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})$",
        created.strip(),
    )
    if match is None:
        return None
    stamp, zone = match.groups()
    zone = "+00:00" if zone == "Z" else (zone if ":" in zone else f"{zone[:3]}:{zone[3:]}")
    try:
        started = datetime.fromisoformat(f"{stamp}{zone}")
    except ValueError:
        return None
    return (time.time() if now is None else now) - started.timestamp()


def _test_container_entries() -> list[tuple[str, float]]:
    """(name, age in seconds) for every throwaway test container Docker still knows about.

    One listing per prefix, so each selector stays as narrow as the name contract, and
    only names `_is_reapable_test_container` accepts are kept. `.Created` from
    `docker inspect` is RFC 3339 and needs no locale guess, unlike `docker ps`'s
    `.CreatedAt` (a rendered local-time string). Fail closed: an unreadable listing, a name
    outside the contract or an unparseable timestamp reaps nothing rather than guessing --
    a wrong age could delete a *running* run's server.
    """
    names: list[str] = []
    for prefix in TEST_CONTAINER_PREFIXES:
        listed = _run(["docker", "ps", "-a", "--filter", f"name={prefix}", "--format", "{{.Names}}"])
        if listed.returncode != 0:
            return []
        names.extend(
            line.strip()
            for line in listed.stdout.splitlines()
            if line.strip() and _is_reapable_test_container(line)
        )
    if not names:
        return []
    inspected = _run(["docker", "inspect", "--format", "{{.Name}}\t{{.Created}}", *names])
    if inspected.returncode != 0:
        return []
    entries: list[tuple[str, float]] = []
    for line in inspected.stdout.splitlines():
        name, _, created = line.partition("\t")
        age = _container_age_seconds(created)
        if name.strip() and age is not None:
            entries.append((name.strip().lstrip("/"), age))
    return entries


def _reap_orphan_containers(
    min_age_seconds: int = 600,
    entries: list[tuple[str, float]] | None = None,
    remover: Callable[[str], int] | None = None,
) -> list[str]:
    """Force-remove throwaway *containers* a killed run left behind -- both families.

    `docker run -d --rm --name <prefix><hex>` removes its container only when the run
    *ends*, so a pytest killed mid-run (the harness's 4800 s cap, a watchdog, Ctrl-C --
    the finalizer never runs) leaves it `Up` with its published host port held for good.
    The datadir reaper cannot see either family: it globs `agentcms-drill-pgdata-*` and is
    reached only on the local-initdb path, while a docker-capable machine takes the Docker
    path instead.

    Two families leak this way: the drill's `agentcms-drill-pg-*`, and the suite's own
    session server `agentcms-test-pg-*` started by `start_docker_postgres`. Measured for
    the drill family on 2026-09-27: 30 orphans (19 from 2026-09-25, 9 from 2026-09-26, all
    0.00-0.02% CPU, no compose labels). The suite family had **no** measured orphan on
    2026-09-27 -- the host carried 0 of both families that day -- so its selection was
    proved by leaving a fresh `agentcms-test-pg-*` behind on purpose and watching this
    reaper take it.

    Only containers older than `min_age_seconds` are touched, so a concurrent run's own
    server survives. `entries`/`remover` exist so the guard test can drive the selection
    without Docker; both default to the real thing.
    """
    if entries is None:
        if shutil.which("docker") is None:
            return []
        entries = _test_container_entries()
    remove = remover or (lambda name: _run(["docker", "rm", "-f", name]).returncode)
    reaped: list[str] = []
    for name, age in sorted(entries):
        if not _is_reapable_test_container(name) or age < min_age_seconds:
            continue
        if remove(name) == 0:
            reaped.append(name)
    return reaped


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
        ],
        env=_locale_env(),
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
        ],
        env=_locale_env(),
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

    # Reap before choosing a strategy. `docker run -d --rm` removes a container only when
    # the run *ends*, and a pytest killed mid-run (harness cap, watchdog, Ctrl-C) never
    # reaches the session finalizer in conftest -- so the last killed run's
    # `agentcms-test-pg-*` server is still `Up`, holding its host port, with `--rm` never
    # firing. This sits above every branch so a docker-capable machine reaps too: the
    # datadir reap only runs on the local-initdb path below.
    orphans = _reap_orphan_containers()
    if orphans:
        print(f"reaped {len(orphans)} orphaned test container(s): {', '.join(orphans)}", flush=True)

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


def docker_required() -> bool:
    """True when a missing Docker daemon must fail the suite instead of skipping it.

    CI sets AGENTCMS_REQUIRE_DOCKER=1.  A deploy check that skips is how #35 and
    #36 shipped, so on the runners "no Docker" is a failure, not a green run.
    """
    import os

    return os.environ.get("AGENTCMS_REQUIRE_DOCKER", "").strip().lower() not in {
        "",
        "0",
        "false",
        "no",
    }


def skip_or_fail_without_docker(what: str) -> None:
    """Skip when Docker is genuinely unavailable — unless it is required (#37)."""
    if docker_available():
        return
    import os

    import pytest

    message = (
        f"{what} needs a running Docker daemon (docker CLI present: "
        f"{bool(os.environ.get('PATH')) and bool(__import__('shutil').which('docker'))})."
    )
    if docker_required() or os.environ.get("AGENTCMS_REQUIRE_DOCKER"):
        pytest.fail(message + " AGENTCMS_REQUIRE_DOCKER is set, so this is a FAILURE, not a skip.")
    pytest.skip(message)
