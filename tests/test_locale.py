"""Regression test for locale-independent initdb (#34).

PostgreSQL 18+ refuses to run initdb when LANG, LC_ALL, and LC_CTYPE are all
unset — which is exactly the environment a supervisor/launchd/docker exec/minimal
container gives you. This test verifies that our initdb wrapper works even when
those variables are scrubbed from the environment.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.pg import _local_pg_bin, _locale_env, start_initdb_postgres


def _has_initdb() -> bool:
    return _local_pg_bin() is not None


def _scrubbed_env() -> dict[str, str]:
    """Return a copy of os.environ with locale vars removed."""
    env = dict(os.environ)
    for var in ("LANG", "LC_ALL", "LC_CTYPE", "LANGUAGE"):
        env.pop(var, None)
    return env


@pytest.mark.skipif(not _has_initdb(), reason="no local initdb/pg_ctl available")
def test_initdb_works_with_scrubbed_locale() -> None:
    """start_initdb_postgres must succeed even when LANG/LC_* are unset.

    This reproduces the exact failure mode from the ticket: initdb aborts with
    "invalid locale settings; check LANG and LC_* environment variables" when
    all three are missing. Our _locale_env() helper pins C.UTF-8 so the cluster
    creation is deterministic and independent of the ambient environment.
    """
    # Verify that initdb would fail with a scrubbed env if we didn't pin locale
    bindir = _local_pg_bin()
    assert bindir is not None
    initdb_bin = bindir / "initdb"

    with tempfile.TemporaryDirectory(prefix="agentcms-locale-test-") as tmp:
        datadir = Path(tmp) / "pgdata"
        # This call should fail without our locale pinning
        result = subprocess.run(
            [str(initdb_bin), "-D", str(datadir), "-U", "postgres", "-A", "trust", "--no-sync", "-E", "UTF8"],
            capture_output=True,
            text=True,
            env=_scrubbed_env(),
        )
        # On PG 18+, this fails with locale error; on older PG it may succeed.
        # Either way, our wrapper should work.

    # Now verify our wrapper works with the scrubbed env
    # We monkey-patch os.environ for the duration of this test
    old_environ = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(_scrubbed_env())
        # Ensure PATH is preserved so we can find initdb/pg_ctl/psql
        os.environ["PATH"] = old_environ.get("PATH", "/usr/bin:/bin")
        os.environ["HOME"] = old_environ.get("HOME", "/tmp")

        server = start_initdb_postgres()
        try:
            # Verify the server is actually usable
            from sqlalchemy import create_engine, text

            engine = create_engine(server.admin_url)
            with engine.connect() as conn:
                result = conn.execute(text("SELECT 1"))
                assert result.scalar() == 1
        finally:
            server.cleanup()
    finally:
        os.environ.clear()
        os.environ.update(old_environ)


@pytest.mark.skipif(not _has_initdb(), reason="no local initdb/pg_ctl available")
def test_locale_env_contains_required_vars() -> None:
    """_locale_env() must include LANG, LC_ALL, LC_CTYPE (defaults to C.UTF-8 if unset)."""
    # Test with scrubbed env to verify defaults are applied
    env = _locale_env()
    # Should preserve other env vars
    assert "PATH" in env
    # The function uses setdefault so it preserves existing values
    # In a scrubbed env it would set C.UTF-8
    import os

    old_environ = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update({"PATH": "/usr/bin:/bin"})
        env = _locale_env()
        assert env.get("LANG") == "C.UTF-8"
        assert env.get("LC_ALL") == "C.UTF-8"
        assert env.get("LC_CTYPE") == "C.UTF-8"
    finally:
        os.environ.clear()
        os.environ.update(old_environ)
