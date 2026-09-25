"""Full backup/restore drill integration test (#39).

This test exercises the complete backup → destroy volume → restore → verify data
pipeline against a real PostgreSQL server (via Docker or local initdb),
proving the documented runbook steps work end-to-end with actual data.

Run with: pytest tests/test_backup_restore_drill.py -v
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PG_IMAGE = os.environ.get("TEST_POSTGRES_IMAGE", "postgres:16-alpine")
_LOCAL_PG_BIN_GLOBS = (
    "/opt/homebrew/opt/postgresql@16/bin",
    "/opt/homebrew/opt/postgresql@18/bin",
    "/opt/homebrew/opt/postgresql/bin",
    "/usr/lib/postgresql/16/bin",
    "/usr/local/pgsql/bin",
)


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kwargs)


def _locale_env() -> dict[str, str]:
    """Return an environment dict with locale variables pinned for initdb."""
    env = dict(os.environ)
    for var, default in (("LANG", "C.UTF-8"), ("LC_ALL", "C.UTF-8"), ("LC_CTYPE", "C.UTF-8")):
        env.setdefault(var, default)
    return env


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


def docker_available() -> bool:
    if os.environ.get("AGENTCMS_TEST_NO_DOCKER"):
        return False
    if shutil.which("docker") is None:
        return False
    return _run(["docker", "info"]).returncode == 0


def free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_pg(dsn: str, timeout: float = 60) -> bool:
    from sqlalchemy import create_engine, text

    deadline = time.time() + timeout
    engine = create_engine(dsn, pool_pre_ping=True)
    try:
        while time.time() < deadline:
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                return True
            except Exception:
                time.sleep(0.5)
        return False
    finally:
        engine.dispose()


def with_database(url: str, database: str) -> str:
    from sqlalchemy.engine import make_url

    return make_url(url).set(database=database).render_as_string(hide_password=False)


def _run_ddl(url: str, statement: str) -> None:
    """Run one DDL statement in AUTOCOMMIT (CREATE/DROP DATABASE cannot be transactional)."""
    from sqlalchemy import create_engine, text

    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(statement))
    finally:
        engine.dispose()


#: Every database the drill creates is named with this prefix, so a drill database can
#: never be confused with one the drill does not own.
DRILL_DB_PREFIX = "agentcms_drill_"

#: Names the drill must never create, drop or adopt: the app's ambient default
#: (``app.config.DEV_DATABASE_URL``) and the database the whole test suite shares.
SHARED_DB_NAMES = ("agentcms", "agentcms_test")


def _drill_database_name() -> str:
    """A fresh database name this drill owns outright.

    The drill DROPs and recreates the database it works on, so it has to work on a name
    it chose.  A fixed name is not a resource it owns: on CI's external server the
    literal ``agentcms`` is also the app's ambient/dev database, so the drill's
    ``DROP DATABASE IF EXISTS "agentcms" WITH (FORCE)`` deleted data it never created --
    and the three MCP tests that fell back to the ambient binding then died with
    ``FATAL: database "agentcms" does not exist`` (run 36130902330).
    """
    return f"{DRILL_DB_PREFIX}{uuid.uuid4().hex[:12]}"


def _database_names(admin_url: str) -> set[str]:
    """Every database on the server -- used to prove the drill touches only its own."""
    from sqlalchemy import create_engine, text

    engine = create_engine(admin_url)
    try:
        with engine.connect() as conn:
            return {row[0] for row in conn.execute(text("SELECT datname FROM pg_database"))}
    finally:
        engine.dispose()


def _destroy_and_recreate_database(admin_url: str, db_name: str) -> None:
    """The drill's "the volume was destroyed" step -- scoped to a database we own."""
    from sqlalchemy import create_engine, text

    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :name"),
                {"name": db_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        engine.dispose()


@pytest.fixture(scope="function")
def postgres_server():
    """Per-test PostgreSQL server, with DATABASE_URL restored once the test is done."""
    from app.config import reset_settings_cache
    from app.db.session import dispose_engine

    previous = os.environ.get("DATABASE_URL")
    try:
        with _postgres_server() as info:
            yield info
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        reset_settings_cache()
        dispose_engine()


@contextlib.contextmanager
def _postgres_server():
    """One drill's PostgreSQL server, holding exactly one database -- one the drill owns.

    ``DATABASE_URL`` is bound for the drill and released *here*, not only in the
    ``postgres_server`` fixture above: tests may drive this context manager directly, and
    a binding left behind points the whole process at a database the drill has just
    dropped -- the second half of run 36130902330.
    """
    db_name = _drill_database_name()
    previous_database_url = os.environ.get("DATABASE_URL")
    try:
        with _postgres_server_inner(db_name) as info:
            yield info
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url
        from app.config import reset_settings_cache
        from app.db.session import dispose_engine

        reset_settings_cache()
        dispose_engine()


def _reap_orphan_servers(root: Path | None = None, min_age_seconds: int = 600) -> list[str]:
    """Stop and delete throwaway servers left behind by a killed run.

    A pytest process killed mid-run (the harness's 4800 s cap, a watchdog,
    Ctrl-C) never runs its fixture finalizer, so its postmaster survives and
    keeps its SysV shared-memory segment. 28 such orphans from earlier killed
    runs exhausted macOS's `kern.sysv.shmmni = 32` on 2026-09-25; after that
    *every* `initdb` on the machine died with `shmget(...) failed: No space left
    on device` / `could not create shared memory segment`, which reddened the
    local gate that lands tickets for hours. Reaping stale servers (far older
    than any live test, so a concurrent run is never touched) bounds the damage
    of the next kill.
    """
    tmp = root or Path(tempfile.gettempdir())
    now = time.time()
    reaped: list[str] = []
    for datadir in sorted(tmp.glob("agentcms-drill-pgdata-*")):
        pidfile = datadir / "postmaster.pid"
        try:
            fields = pidfile.read_text().split()
            age = now - pidfile.stat().st_mtime
        except OSError:
            continue
        if not fields or age < min_age_seconds:
            continue
        pid = int(fields[0])
        sockdir = Path(fields[4]) if len(fields) > 4 else None
        if str(datadir) in _ps_command(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                time.sleep(0.25)
                if not _ps_command(pid).strip():
                    break
            else:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
        shutil.rmtree(datadir, ignore_errors=True)
        if sockdir is not None and str(sockdir).startswith(str(tmp)):
            shutil.rmtree(sockdir, ignore_errors=True)
        reaped.append(datadir.name)
    return reaped


def _ps_command(pid: int) -> str:
    """`ps -o command=` for one pid, or "" when it is gone (macOS has no /proc)."""
    return subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False
    ).stdout


def _local_server_options(port: int, sockdir: Path) -> str:
    """`pg_ctl -o` options for the throwaway drill server (one place, one truth).

    Measured on 2026-09-25 with a real server: a PostgreSQL 18 server on macOS
    takes exactly **one** `kern.sysv.shmmni` segment whether or not
    `shared_memory_type=mmap` is set, and the limit is 32 -- so the machine is
    wedged by *how many servers are alive*, not by how much memory each maps.
    Keep the count low: see `_reap_orphan_servers` below.
    """
    return (
        f"-p {port} -k {sockdir} -c listen_addresses=127.0.0.1 "
        "-c fsync=off -c full_page_writes=off -c timezone=UTC"
    )


@contextlib.contextmanager
def _postgres_server_inner(db_name: str):
    """Start a throwaway PostgreSQL server (external, Docker, or local initdb) for the drill."""
    # Strategy 1: Use TEST_DATABASE_URL if provided (CI service container)
    external = os.environ.get("TEST_DATABASE_URL")
    if external:
        # An external server (a CI service container, or a dev machine's Postgres) is
        # normally SHARED: the rest of the suite writes to it, so the drill has to own a
        # database nobody else is using -- a name the *drill* picked (`db_name`), never a
        # fixed one. It used to be the literal `agentcms`, which on CI is also the app's
        # ambient database: the drill's DROP deleted a database it never created (run
        # 36130902330). Reusing the suite's own database instead is just as wrong -- the
        # drill seeds into suite-wide state (CI: `assert 5 == 3`, `UniqueViolation
        # ix_sites_slug`) and "destroys" a database it never used, so the destroy and
        # restore half of the drill proves nothing.
        from sqlalchemy.engine import make_url

        admin_url = make_url(external).set(database="postgres").render_as_string(hide_password=False)
        url = make_url(external).set(database=db_name).render_as_string(hide_password=False)

        _run_ddl(admin_url, f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        _run_ddl(admin_url, f'CREATE DATABASE "{db_name}"')

        os.environ["DATABASE_URL"] = url
        from app.config import reset_settings_cache
        from app.db.session import dispose_engine

        reset_settings_cache()
        dispose_engine()

        try:
            yield {
                "url": url,
                "admin_url": admin_url,
                "db_name": db_name,
                "cleanup": lambda: None,
                "mode": "external",
            }
        finally:
            dispose_engine()
            _run_ddl(admin_url, f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        return

    # Strategy 2: Docker
    if docker_available():
        container_name = f"agentcms-drill-pg-{uuid.uuid4().hex[:8]}"
        port = free_port()

        start = _run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                container_name,
                "-e",
                "POSTGRES_USER=postgres",
                "-e",
                "POSTGRES_PASSWORD=postgres",
                "-e",
                f"POSTGRES_DB={db_name}",
                "-p",
                f"{port}:5432",
                PG_IMAGE,
                "postgres",
                "-c",
                "fsync=off",
                "-c",
                "full_page_writes=off",
            ]
        )
        if start.returncode != 0:
            pytest.fail(f"docker run failed: {start.stderr.strip()}")

        url = f"postgresql+psycopg://postgres:postgres@127.0.0.1:{port}/{db_name}"
        admin_url = f"postgresql+psycopg://postgres:postgres@127.0.0.1:{port}/postgres"

        if not wait_for_pg(admin_url, timeout=90):
            _run(["docker", "rm", "-f", container_name])
            pytest.fail("PostgreSQL did not become ready")

        def cleanup():
            _run(["docker", "rm", "-f", container_name])

        os.environ["DATABASE_URL"] = url
        from app.config import reset_settings_cache
        from app.db.session import dispose_engine

        reset_settings_cache()
        dispose_engine()

        yield {
            "url": url,
            "admin_url": admin_url,
            "db_name": db_name,
            "cleanup": cleanup,
            "mode": "docker",
        }
        return

    # Strategy 3: Local initdb/pg_ctl
    bindir = _local_pg_bin()
    if bindir is None:
        pytest.skip("No PostgreSQL available (no Docker, no initdb/pg_ctl)")

    orphans = _reap_orphan_servers()
    if orphans:
        print(f"reaped {len(orphans)} orphaned drill server(s): {', '.join(orphans)}", flush=True)

    datadir = Path(tempfile.mkdtemp(prefix="agentcms-drill-pgdata-"))
    sockdir = Path(tempfile.mkdtemp(prefix="agentcms-drill-pgsock-"))
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
        pytest.fail(f"initdb failed: {init.stderr.strip()}")

    options = _local_server_options(port, sockdir)
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
        pytest.fail(f"pg_ctl start failed: {started.stderr.strip()}")

    version = _run([str(bindir / "postgres"), "--version"]).stdout.strip() or "unknown version"
    url = f"postgresql+psycopg://postgres@127.0.0.1:{port}/{db_name}"
    admin_url = with_database(url, "postgres")

    if not wait_for_pg(admin_url, timeout=30):
        _run([str(bindir / "pg_ctl"), "-D", str(datadir), "-m", "immediate", "stop"])
        shutil.rmtree(datadir, ignore_errors=True)
        shutil.rmtree(sockdir, ignore_errors=True)
        pytest.fail("initdb postgres did not become ready")

    # Create the agentcms database
    created = _run(
        [
            str(bindir / "psql"),
            "-h",
            "127.0.0.1",
            "-p",
            str(port),
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-c",
            f'CREATE DATABASE "{db_name}"',
        ],
        env=_locale_env(),
    )
    if created.returncode != 0:
        # Fallback: use SQLAlchemy
        from sqlalchemy import create_engine, text

        engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        engine.dispose()

    def cleanup() -> None:
        _run([str(bindir / "pg_ctl"), "-D", str(datadir), "-m", "immediate", "stop"])
        shutil.rmtree(datadir, ignore_errors=True)
        shutil.rmtree(sockdir, ignore_errors=True)

    # Set DATABASE_URL in environment for alembic
    os.environ["DATABASE_URL"] = url

    # Reset settings cache and dispose engine so the new DATABASE_URL is picked up
    from app.config import reset_settings_cache
    from app.db.session import dispose_engine

    reset_settings_cache()
    dispose_engine()

    yield {
        "url": url,
        "admin_url": admin_url,
        "db_name": db_name,
        "cleanup": cleanup,
        "mode": f"initdb ({version})",
    }


def run_alembic(*args: str, database_url: str) -> subprocess.CompletedProcess[str]:
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
        pytest.fail(
            f"alembic {' '.join(args)} failed ({result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout


def seed_database(database_url: str) -> dict:
    """Seed the database with test data and return identifiers for verification."""
    import uuid as uuid_lib

    from app.config import Settings
    from app.db.session import session_scope
    from app.models.actor import Actor
    from app.models.capability_link import CapabilityLink
    from app.models.post import Post
    from app.models.post_revision import PostRevision
    from app.models.site import Site
    from app.services.capability_tokens import ALL_VERBS, generate_capability_token

    settings = Settings(
        app_env="test",
        database_url=database_url,
        secret_key="test-secret-key-that-is-long-enough-000000",
        log_level="WARNING",
    )
    assert settings.app_env == "test"

    with session_scope() as session:
        # Create site
        site = Site(
            id=uuid_lib.uuid4(),
            slug="drill-blog",
            name="Drill Blog",
            base_url="https://drill.example.com",
            publish_mode="auto",
            settings={"description": "Backup/restore drill site"},
        )
        session.add(site)
        session.flush()

        # Create actor
        actor = Actor(
            id=uuid_lib.uuid4(),
            kind="machine",
            label="Drill Actor",
            site_id=site.id,
            scopes=["posts:read", "posts:write", "posts:publish"],
        )
        session.add(actor)
        session.flush()

        # Create capability link
        raw_token, token_hash = generate_capability_token("drill-blog")
        link = CapabilityLink(
            id=uuid_lib.uuid4(),
            actor_id=actor.id,
            label="Drill Link",
            token_hash=token_hash,
            site_slug="drill-blog",
            path_scope="/v1/sites/drill-blog/posts",
            verbs=sorted(ALL_VERBS),
            uses_remaining=1000,
        )
        session.add(link)

        # Create posts with revisions
        post_ids = []
        for _i, (slug, title, body, status) in enumerate(
            [
                ("hello-world", "Hello World", "# Hello World\n\nFirst post.", "published"),
                ("getting-started", "Getting Started", "# Getting Started\n\nSecond post.", "draft"),
                ("api-overview", "API Overview", "# API Overview\n\nThird post.", "pending_review"),
            ]
        ):
            post = Post(
                id=uuid_lib.uuid4(),
                site_id=site.id,
                slug=slug,
                title=title,
                body_md=body,
                status=status,
                revision_count=1,
                created_by_actor_id=actor.id,
                author_label="Drill Bot",
            )
            session.add(post)
            session.flush()
            post_ids.append(str(post.id))

            revision = PostRevision(
                id=uuid_lib.uuid4(),
                post_id=post.id,
                revision=1,
                title=title,
                body_md=body,
                status=status,
                editor_label="Drill Bot",
                actor_id=actor.id,
            )
            session.add(revision)

    return {
        "site_slug": "drill-blog",
        "post_ids": post_ids,
        "actor_label": "Drill Actor",
        "token": raw_token,
    }


def count_core_tables(database_url: str) -> dict[str, int]:
    """Count rows in core tables for verification."""
    from sqlalchemy import create_engine, text

    engine = create_engine(database_url)
    counts = {}
    try:
        with engine.connect() as conn:
            for table in ("posts", "post_revisions", "audit_events", "sites", "actors", "capability_links"):
                result = conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
                counts[table] = result.scalar() or 0
    finally:
        engine.dispose()
    return counts


def compute_content_hashes(database_url: str) -> dict[str, str]:
    """Compute canonical content hashes for core tables."""
    import hashlib

    from sqlalchemy import create_engine, text

    engine = create_engine(database_url)
    digests = {}
    try:
        with engine.connect() as conn:
            for table in ("posts", "post_revisions", "audit_events"):
                rows = conn.execute(text(f"SELECT * FROM {table} ORDER BY created_at, id")).fetchall()
                h = hashlib.sha256()
                h.update(str(len(rows)).encode())
                for row in rows:
                    h.update(repr(tuple(str(v) if v is not None else "" for v in row)).encode())
                digests[table] = h.hexdigest()
    finally:
        engine.dispose()
    return digests


class TestFullBackupRestoreDrill:
    """Full backup → destroy volume → restore → verify drill."""

    def test_seed_backup_destroy_restore_verify(self, postgres_server):
        """Complete drill: seed → backup → destroy volume → restore → verify."""
        database_url = postgres_server["url"]
        admin_url = postgres_server["admin_url"]

        # --- STEP 1: Apply migrations ---
        alembic_ok("upgrade", "head", database_url=database_url)

        # --- STEP 2: Seed data ---
        _seed_data = seed_database(database_url)
        original_counts = count_core_tables(database_url)
        original_hashes = compute_content_hashes(database_url)

        print(f"\n[DRILL] Original data counts: {original_counts}")
        print(f"[DRILL] Original content hashes: {original_hashes}")
        assert original_counts["posts"] == 3
        assert original_counts["post_revisions"] == 3
        assert original_counts["sites"] == 1
        assert original_counts["actors"] == 1
        assert original_counts["capability_links"] == 1

        # --- STEP 3: Run backup ---
        backup_dir = REPO_ROOT / ".backups" / f"drill-{uuid.uuid4().hex[:8]}"
        backup_dir.mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        env.update(
            {
                "DATABASE_URL": database_url,
                "BACKUP_PASSPHRASE": "drill-passphrase-000",
                "BACKUP_DIR": str(backup_dir),
                "BACKUP_STORE_URL": f"file://{backup_dir}",
                "BACKUP_RETENTION_DAILY": "30",
                "BACKUP_RETENTION_MONTHLY": "12",
            }
        )

        proc = _run([sys.executable, "-m", "scripts.backup"], env=env)

        assert proc.returncode == 0, f"Backup failed: {proc.stdout}\n{proc.stderr}"
        print(f"[DRILL] Backup output: {proc.stdout.strip()}")

        # Find the backup file
        dump_files = list((backup_dir / "dumps").glob("*.dump.enc"))
        assert len(dump_files) == 1, f"Expected exactly one backup file, found {len(dump_files)}"
        backup_file = dump_files[0]
        print(f"[DRILL] Backup created: {backup_file}")

        # Verify backup is encrypted
        raw = backup_file.read_bytes()
        assert b"CREATE TABLE posts" not in raw
        assert b"PGDMP" not in raw
        print("[DRILL] Backup verified as encrypted")

        # --- STEP 4: Destroy volume (drop and recreate database) ---
        print("[DRILL] Destroying volume (dropping database)...")
        _destroy_and_recreate_database(admin_url, postgres_server["db_name"])

        # --- STEP 5: Restore from backup ---
        print("[DRILL] Restoring from backup...")
        restore_env = dict(env)
        proc = _run(
            [
                sys.executable,
                "-m",
                "scripts.restore",
                "--dump",
                str(backup_file),
                "--target-url",
                database_url,
            ],
            env=restore_env,
        )

        assert proc.returncode == 0, f"Restore failed: {proc.stdout}\n{proc.stderr}"
        print(f"[DRILL] Restore output: {proc.stdout.strip()}")

        # --- STEP 6: Verify restored data ---
        restored_counts = count_core_tables(database_url)
        restored_hashes = compute_content_hashes(database_url)

        print(f"[DRILL] Restored data counts: {restored_counts}")
        print(f"[DRILL] Restored content hashes: {restored_hashes}")

        # Verify counts match
        assert restored_counts == original_counts, f"Count mismatch: {restored_counts} != {original_counts}"

        # Verify content hashes match (zero content-hash mismatches)
        for table in ("posts", "post_revisions", "audit_events"):
            assert restored_hashes[table] == original_hashes[table], (
                f"Content hash mismatch for {table}: {restored_hashes[table]} != {original_hashes[table]}"
            )

        print("[DRILL] ✓ Backup → destroy → restore → verify: ALL CHECKS PASSED")

        # --- STEP 7: Run the official restore_drill.py as final verification ---
        print("[DRILL] Running official restore_drill.py for final verification...")
        drill_env = dict(env)
        drill_env["BACKUP_SOURCE_URL"] = database_url
        proc = _run(
            [
                sys.executable,
                "-m",
                "scripts.restore_drill",
                "--source-url",
                database_url,
            ],
            env=drill_env,
        )

        assert proc.returncode == 0, f"Restore drill failed: {proc.stdout}\n{proc.stderr}"
        assert "restore drill OK" in proc.stdout
        print("[DRILL] Official restore_drill.py: PASSED")


class TestUpgradeRollbackDrill:
    """Full upgrade → rollback drill with data loss verification (#39).

    This test simulates:
    1. Deploy v1 (current)
    2. Seed data
    3. Backup
    4. Upgrade to v2 (simulated by running migrations)
    5. Verify data intact
    6. Rollback to v1 (restore backup + old image)
    7. Verify no data loss
    """

    def test_upgrade_rollback_no_data_loss(self, postgres_server):
        """Complete upgrade/rollback drill verifying zero data loss."""
        database_url = postgres_server["url"]
        admin_url = postgres_server["admin_url"]

        # --- PHASE 1: Initial deployment (v1) ---
        alembic_ok("upgrade", "head", database_url=database_url)

        # Seed initial data
        _seed_data_v1 = seed_database(database_url)
        original_counts = count_core_tables(database_url)
        original_hashes = compute_content_hashes(database_url)

        print(f"\n[UPGRADE-DRILL] v1 data counts: {original_counts}")
        print(f"[UPGRADE-DRILL] v1 content hashes: {original_hashes}")
        assert original_counts["posts"] == 3

        # --- PHASE 2: Pre-upgrade backup (simulating upgrade.sh) ---
        backup_dir = REPO_ROOT / ".backups" / f"upgrade-drill-{uuid.uuid4().hex[:8]}"
        backup_dir.mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        env.update(
            {
                "DATABASE_URL": database_url,
                "BACKUP_PASSPHRASE": "upgrade-drill-passphrase-000",
                "BACKUP_DIR": str(backup_dir),
                "BACKUP_STORE_URL": f"file://{backup_dir}",
                "BACKUP_RETENTION_DAILY": "30",
                "BACKUP_RETENTION_MONTHLY": "12",
            }
        )

        proc = _run([sys.executable, "-m", "scripts.backup"], env=env)
        assert proc.returncode == 0, f"Pre-upgrade backup failed: {proc.stdout}\n{proc.stderr}"

        dump_files = list((backup_dir / "dumps").glob("*.dump.enc"))
        assert len(dump_files) == 1
        pre_upgrade_backup = dump_files[0]
        print(f"[UPGRADE-DRILL] Pre-upgrade backup: {pre_upgrade_backup}")

        # Record alembic revision at backup time
        from sqlalchemy import create_engine, text

        engine = create_engine(database_url)
        with engine.connect() as conn:
            result = conn.execute(text("SELECT version_num FROM alembic_version"))
            pre_upgrade_rev = result.scalar() or "unknown"
        engine.dispose()
        print(f"[UPGRADE-DRILL] Pre-upgrade alembic revision: {pre_upgrade_rev}")

        # --- PHASE 3: Upgrade (apply migrations - simulating new version) ---
        # In a real upgrade, this would be a new image with new migrations.
        # Here we simulate by re-running migrations (idempotent) and noting
        # that in a real scenario there would be new migrations.
        alembic_ok("upgrade", "head", database_url=database_url)

        # Verify data still intact after "upgrade"
        post_upgrade_counts = count_core_tables(database_url)
        post_upgrade_hashes = compute_content_hashes(database_url)
        assert post_upgrade_counts == original_counts
        for table in ("posts", "post_revisions", "audit_events"):
            assert post_upgrade_hashes[table] == original_hashes[table]
        print("[UPGRADE-DRILL] ✓ Data intact after upgrade")

        # --- PHASE 4: Add new data post-upgrade (simulating production use) ---
        import uuid as uuid_lib

        from app.config import Settings
        from app.db.session import session_scope
        from app.models.post import Post
        from app.models.post_revision import PostRevision

        settings = Settings(
            app_env="test",
            database_url=database_url,
            secret_key="test-secret-key-that-is-long-enough-000000",
            log_level="WARNING",
        )
        assert settings.app_env == "test"

        with session_scope() as session:
            site = session.execute(text("SELECT id FROM sites WHERE slug = 'drill-blog'")).scalar_one()
            actor = session.execute(text("SELECT id FROM actors WHERE label = 'Drill Actor'")).scalar_one()

            # Add a post that was created AFTER the upgrade
            new_post = Post(
                id=uuid_lib.uuid4(),
                site_id=site,
                slug="post-upgrade",
                title="Post Upgrade Feature",
                body_md="# Post Upgrade\n\nAdded after upgrade.",
                status="published",
                revision_count=1,
                created_by_actor_id=actor,
                author_label="Drill Bot",
            )
            session.add(new_post)
            session.flush()

            session.add(
                PostRevision(
                    id=uuid_lib.uuid4(),
                    post_id=new_post.id,
                    revision=1,
                    title="Post Upgrade Feature",
                    body_md="# Post Upgrade\n\nAdded after upgrade.",
                    status="published",
                    editor_label="Drill Bot",
                    actor_id=actor,
                )
            )

        # Counts now include the new post
        post_upgrade_with_new_counts = count_core_tables(database_url)
        assert post_upgrade_with_new_counts["posts"] == 4  # 3 original + 1 new
        print(f"[UPGRADE-DRILL] Data after new post: {post_upgrade_with_new_counts}")

        # --- PHASE 5: Rollback (restore pre-upgrade backup) ---
        print("[UPGRADE-DRILL] Rolling back to pre-upgrade state...")

        # Destroy current database (the drill's own -- never a shared name)
        _destroy_and_recreate_database(admin_url, postgres_server["db_name"])

        # Restore from pre-upgrade backup
        restore_env = dict(env)
        proc = _run(
            [
                sys.executable,
                "-m",
                "scripts.restore",
                "--dump",
                str(pre_upgrade_backup),
                "--target-url",
                database_url,
            ],
            env=restore_env,
        )

        assert proc.returncode == 0, f"Rollback restore failed: {proc.stdout}\n{proc.stderr}"

        # --- PHASE 6: Verify rollback state matches pre-upgrade exactly ---
        rolled_back_counts = count_core_tables(database_url)
        rolled_back_hashes = compute_content_hashes(database_url)

        print(f"[UPGRADE-DRILL] Rolled back counts: {rolled_back_counts}")
        print(f"[UPGRADE-DRILL] Rolled back hashes: {rolled_back_hashes}")

        # Core content should match pre-upgrade exactly (zero data loss for pre-upgrade data)
        assert rolled_back_counts == original_counts, (
            f"Rollback count mismatch: {rolled_back_counts} != {original_counts}"
        )
        for table in ("posts", "post_revisions", "audit_events"):
            assert rolled_back_hashes[table] == original_hashes[table], (
                f"Rollback hash mismatch for {table}: {rolled_back_hashes[table]} != {original_hashes[table]}"
            )

        # Verify the post-upgrade post is GONE (this is expected - rollback restores to backup point)
        assert rolled_back_counts["posts"] == 3, "Rollback should restore to pre-upgrade state (3 posts)"

        # --- PHASE 7: Verify alembic revision matches pre-upgrade ---
        engine = create_engine(database_url)
        with engine.connect() as conn:
            result = conn.execute(text("SELECT version_num FROM alembic_version"))
            rolled_back_rev = result.scalar() or "unknown"
        engine.dispose()

        assert rolled_back_rev == pre_upgrade_rev, (
            f"Alembic revision mismatch after rollback: {rolled_back_rev} != {pre_upgrade_rev}"
        )

        print("[UPGRADE-DRILL] ✓ Upgrade → rollback: ZERO DATA LOSS for pre-upgrade data")
        print("[UPGRADE-DRILL] ✓ Alembic revision correctly restored")
        print("[UPGRADE-DRILL] ✓ Post-upgrade data correctly discarded (expected)")


class TestDrillIsolation:
    """Regression tests for the drill's database isolation.

    Before the fix, the external-server strategy reused ``TEST_DATABASE_URL`` verbatim
    (the database the whole test suite shares). Two consequences, both seen on main:

    * the drill seeded into shared state, so the "original" counts were wrong
      (``assert 5 == 3``) and the second drill collided on ``sites.slug``
      (``UniqueViolation ix_sites_slug`` = "drill-blog");
    * the drill's hardcoded ``DROP DATABASE "agentcms"`` dropped a database it never
      used, so the destroy-and-restore half of the drill proved nothing.
    """

    def test_drill_owns_its_database_when_a_server_is_external(self, postgres_server):
        """The drill must never run against the suite's shared database."""
        from sqlalchemy.engine import make_url

        shared = os.environ.get("TEST_DATABASE_URL")
        if not shared:
            pytest.skip("only meaningful with an external server (CI service container)")

        drill_db = make_url(postgres_server["url"]).database
        shared_db = make_url(shared).database

        assert drill_db == postgres_server["db_name"], "the drill's URL is not its own database"
        assert drill_db.startswith(DRILL_DB_PREFIX), (
            f"drill runs against database {drill_db!r}; it DROPs and recreates what it creates, "
            f"so it must own a name it chose ({DRILL_DB_PREFIX}...), never a shared one"
        )
        assert drill_db not in SHARED_DB_NAMES, (
            f"drill claimed the shared name {drill_db!r}: on CI that deleted the database the MCP "
            "tests were using (run 36130902330) and on a dev machine it deletes real data"
        )
        assert drill_db != shared_db, "drill shared the suite database"
        assert postgres_server["url"] != shared, "drill reused TEST_DATABASE_URL verbatim"

    def test_consecutive_drills_each_start_from_an_empty_database(self):
        """Two drills in a row must each start empty -- this is the CI symptom exactly.

        The pre-fix external branch pointed every drill at one shared database, so the
        second drill saw the first drill's rows (``assert 5 == 3``) and collided on
        ``sites.slug = 'drill-blog'`` (``UniqueViolation ix_sites_slug``).
        """
        if not os.environ.get("TEST_DATABASE_URL"):
            pytest.skip("only meaningful with an external server (CI service container)")

        from sqlalchemy.engine import make_url

        names: list[str] = []
        for round_no in (1, 2):
            with _postgres_server() as info:
                names.append(info["db_name"])
                assert make_url(info["url"]).database == info["db_name"]
                alembic_ok("upgrade", "head", database_url=info["url"])
                seed_database(info["url"])
                counts = count_core_tables(info["url"])
                assert counts["sites"] == 1, f"round {round_no} saw {counts['sites']} sites, not 1"
                assert counts["posts"] == 3, f"round {round_no} saw {counts['posts']} posts, not 3"

        assert len(set(names)) == 2, f"both drills worked on the same database name: {names}"
        assert not set(names) & set(SHARED_DB_NAMES), f"a drill claimed a shared name: {names}"

    def test_drill_never_touches_a_database_it_does_not_own(self):
        """Regression (run 36130902330): the drill dropped a database it never created.

        The external branch ran ``DROP DATABASE IF EXISTS "agentcms" WITH (FORCE)`` before
        every drill -- a fixed name, and on CI's service container the database the app
        falls back to.  The three MCP tests that rode on that ambient binding then died
        with ``FATAL: database "agentcms" does not exist``.  This plants a database with
        that exact name (plus a row), runs one drill, and asserts both survived; on a
        developer's machine the same bug deletes their real dev database.
        """
        from sqlalchemy import create_engine, text
        from sqlalchemy.engine import make_url

        shared = os.environ.get("TEST_DATABASE_URL")
        if not shared:
            pytest.skip("only meaningful with an external server (CI service container)")
        assert make_url(shared).database not in SHARED_DB_NAMES, "refusing to plant on the suite's DB"

        admin_url = with_database(shared, "postgres")
        probe = SHARED_DB_NAMES[0]
        planted = probe not in _database_names(admin_url)
        if planted:
            _run_ddl(admin_url, f'CREATE DATABASE "{probe}"')
        try:
            _run_ddl(with_database(shared, probe), "CREATE TABLE IF NOT EXISTS drill_probe(kept int)")
            _run_ddl(with_database(shared, probe), "INSERT INTO drill_probe VALUES (1)")

            with _postgres_server():
                pass  # exactly one drill: create our own database, drop it on the way out

            assert probe in _database_names(admin_url), (
                f"the drill dropped {probe!r}, a database it never created"
            )
            engine = create_engine(with_database(shared, probe))
            try:
                with engine.connect() as conn:
                    kept = conn.execute(text("SELECT count(*) FROM drill_probe")).scalar()
            finally:
                engine.dispose()
            assert kept == 1, "the drill destroyed data in a database it did not own"
        finally:
            if planted:
                _run_ddl(admin_url, f'DROP DATABASE IF EXISTS "{probe}" WITH (FORCE)')


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
