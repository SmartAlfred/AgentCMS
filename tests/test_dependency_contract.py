"""The dependency contract CI relies on (#40).

Two properties were silently false on `main`, and both are cheap to assert:

1. Every mypy plugin `pyproject.toml` declares must be importable. When one is
   not, mypy aborts with "errors prevented further checking" after checking no
   files at all — the type gate becomes a no-op.
2. `requirements.lock.txt` must agree with `pyproject.toml`, because CI installs
   from the lock: a dependency can then only change what CI tests through an
   explicit, reviewed commit.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import Any

import pytest
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]


def _load_pyproject() -> dict[str, Any]:
    loaded: dict[str, Any] = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    return loaded


def _mypy_plugins() -> list[str]:
    plugins: list[str] = _load_pyproject()["tool"]["mypy"]["plugins"]
    return plugins


def test_declared_mypy_plugins_are_importable() -> None:
    """Regression (#40): the gate must be able to start, not only to be declared."""
    plugins = _mypy_plugins()
    assert plugins, "[tool.mypy].plugins is empty — did the type gate get gutted?"
    for plugin in plugins:
        module_name = plugin.split(":", 1)[0]
        try:
            importlib.import_module(module_name)
        except ImportError as exc:  # pragma: no cover - only on drift
            pytest.fail(
                f"mypy would abort before checking any file: plugin {plugin!r} from "
                f"[tool.mypy].plugins cannot be imported ({exc}). Restore the dependency "
                "that provides it (pin it in pyproject.toml + regenerate the lock), or "
                "update the plugin list in the same commit."
            )


def test_installed_sqlalchemy_still_provides_the_declared_mypy_plugin() -> None:
    """#40: SQLAlchemy 2.1 removed `sqlalchemy.ext.mypy.plugin`, and the unpinned
    `sqlalchemy>=2.0.30` floated straight onto it. Keep the `<2.1` pin until the
    2.1 typing migration lands (tracked separately)."""
    if "sqlalchemy.ext.mypy.plugin" not in _mypy_plugins():
        pytest.skip("the SQLAlchemy mypy plugin is no longer declared — nothing to protect")
    import sqlalchemy

    assert Version(sqlalchemy.__version__) < Version("2.1"), (
        f"installed SQLAlchemy {sqlalchemy.__version__} no longer ships "
        "sqlalchemy.ext.mypy.plugin, which [tool.mypy].plugins requires, so the mypy "
        "gate would abort. Keep the `sqlalchemy>=2.0.30,<2.1` pin until the 2.1 "
        "migration lands."
    )


def test_lockfile_agrees_with_pyproject() -> None:
    """CI installs from the lock, so a drifted lock is a gate change nobody reviewed."""
    from scripts.check_lock import LOCK_PATH, check

    assert LOCK_PATH.exists(), "requirements.lock.txt is missing — CI installs from it"
    problems = check()
    assert not problems, "lockfile drift detected:\n  - " + "\n  - ".join(problems)
