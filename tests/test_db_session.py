"""Regression tests for #27 — a fresh process must not deadlock on its first DB request.

The bug: ``get_session_factory()`` acquired the (non-reentrant) module lock and then
called ``get_engine()``, which acquires the same lock.  In a fresh process the engine
is still ``None``, so the first DB request hung forever instead of returning.

The deadlock can only be reproduced with both singletons still ``None``, which is why
the first test runs in a *subprocess* rather than in-process (pytest has already
imported the app and, in other tests, created the engine).
"""

from __future__ import annotations

import subprocess
import sys

from app.db.session import dispose_engine, get_engine, get_session_factory

FRESH_PROCESS = (
    "from app.db.session import get_session_factory, get_engine;"
    "factory = get_session_factory();"
    "assert factory.kw['bind'] is get_engine();"
    "print('FRESH_OK')"
)


def test_first_db_request_in_a_fresh_process_returns() -> None:
    """A cold process must reach its first session factory within seconds, not hang."""

    proc = subprocess.run(
        [sys.executable, "-c", FRESH_PROCESS],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, f"fresh process failed: {proc.stderr}"
    assert "FRESH_OK" in proc.stdout


def test_session_factory_and_engine_are_the_same_lazily_created_singletons() -> None:
    dispose_engine()
    factory = get_session_factory()
    assert factory.kw["bind"] is get_engine()
    # Second call is cached, not rebuilt.
    assert get_session_factory() is factory
    dispose_engine()


def test_repeated_dispose_and_recreate_does_not_deadlock() -> None:
    """dispose_engine() takes the same lock, so it must stay reentrant-safe too."""

    for _ in range(3):
        dispose_engine()
        assert get_session_factory().kw["bind"] is get_engine()
