"""The dependency contract CI relies on (#40).

Two properties were silently false on `main`, and both are cheap to assert:

1. Every mypy plugin `pyproject.toml` declares must be importable. When one is
   not, mypy aborts with "errors prevented further checking" after checking no
   files at all — the type gate becomes a no-op.
2. `requirements.lock.txt` must agree with `pyproject.toml`, because CI installs
   from the lock: a dependency can then only change what CI tests through an
   explicit, reviewed commit.
3. The gate must *check files*. Declaring plugins and strictness proves nothing if
   mypy aborts at startup — see `test_the_type_gate_actually_checks_files`.

Since #41 the SQLAlchemy mypy plugin is no longer declared at all: SQLAlchemy 2.0+
types the ORM inline via `Mapped[...]`/`mapped_column()`, and 2.1 deleted the
`sqlalchemy.ext.mypy.plugin` shim outright.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]


def _load_pyproject() -> dict[str, Any]:
    loaded: dict[str, Any] = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    return loaded


def _mypy_plugins() -> list[str]:
    """`[tool.mypy].plugins`, or `[]` when the key is absent.

    Absent is the normal case since #41: SQLAlchemy 2.0+ types the ORM inline, and
    2.1 removed the `sqlalchemy.ext.mypy.plugin` shim this key used to require.
    """
    section: dict[str, Any] = _load_pyproject()["tool"]["mypy"]
    return [str(plugin) for plugin in section.get("plugins", [])]


def _declared_sqlalchemy_requirement() -> Requirement:
    from scripts.check_lock import declared_requirements

    for requirement in declared_requirements():
        if canonicalize_name(requirement.name) == "sqlalchemy":
            return requirement
    raise AssertionError("pyproject.toml no longer declares sqlalchemy at all")


def test_declared_mypy_plugins_are_importable() -> None:
    """Regression (#40): the gate must be able to start, not only to be declared.

    Every plugin named here has to import in the environment CI builds from the
    lock. When one does not, mypy aborts with "errors prevented further checking"
    after checking no files at all, and the type gate is silently a no-op.
    """
    for plugin in _mypy_plugins():
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


def test_no_mypy_plugin_depends_on_a_removed_module() -> None:
    """#41: the SQLAlchemy mypy plugin was deleted, and the config must not need it.

    `sqlalchemy.ext.mypy.plugin` was removed in SQLAlchemy 2.1 (and is redundant on
    2.0, where the ORM is typed inline). Re-declaring it puts the whole type gate
    behind a module a routine dependency bump can delete (#40, #41).
    """
    assert "sqlalchemy.ext.mypy.plugin" not in _mypy_plugins(), (
        "sqlalchemy.ext.mypy.plugin is obsolete: SQLAlchemy 2.0 types the ORM inline "
        "and 2.1 removed the module. Declaring it makes `mypy` abort before it checks "
        "a single file (#40, #41)."
    )


def test_sqlalchemy_upper_bound_allows_the_2_1_typing_story() -> None:
    """#41: the `<2.1` cap only ever existed to protect the plugin, so it is gone.

    A cap that outlives its reason blocks 2.1, including security fixes backported
    only there. The bound may be relaxed again in future — but not back to one that
    excludes the version we now depend on typing-wise.
    """
    requirement = _declared_sqlalchemy_requirement()
    assert Version("2.1.0") in requirement.specifier, (
        f"pyproject.toml declares sqlalchemy{requirement.specifier}, which excludes 2.1. "
        "The `<2.1` stopgap from #40 exists only to protect the deleted mypy plugin and "
        "must not come back (#41)."
    )


def test_the_type_gate_actually_checks_files(tmp_path: Path) -> None:
    """#40/#41: a mypy that dies at startup is not a gate.

    On 2026-09-25 `lint` exited "errors prevented further checking" after checking
    zero files: `main` was red and the type gate enforced nothing while every
    declared setting still claimed it did. So don't assert the config — point mypy
    at a file with a deliberate type error and require the error back.
    """
    canary = tmp_path / "mypy_canary.py"
    canary.write_text("def f() -> int:\n    return 'not an int'\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "--no-incremental", str(canary)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    output = proc.stdout + proc.stderr
    assert "mypy_canary.py" in output, (
        "mypy never reported on the canary file, so the type gate is a no-op:\n" + output
    )
    assert proc.returncode != 0 and "error:" in output, (
        "mypy accepted a file containing a real type error, so the gate is not checking anything:\n" + output
    )


def test_lockfile_agrees_with_pyproject() -> None:
    """CI installs from the lock, so a drifted lock is a gate change nobody reviewed."""
    from scripts.check_lock import LOCK_PATH, check

    assert LOCK_PATH.exists(), "requirements.lock.txt is missing — CI installs from it"
    problems = check()
    assert not problems, "lockfile drift detected:\n  - " + "\n  - ".join(problems)


def test_ci_installs_from_the_lock_and_checks_it() -> None:
    """Enforcement, not just a pin.

    The pin only helps if CI is the thing that installs it. This asserts the
    workflow keeps installing the locked set and keeps running the drift check,
    so `pyproject.toml` alone can never change what CI tests again (#40).
    """
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    assert workflow.count("-r requirements.lock.txt") >= 4, (
        "a CI job no longer installs from requirements.lock.txt, so dependency "
        "resolution would again be whatever pip picks on the day (#40)"
    )
    assert "scripts/check_lock.py" in workflow, (
        "CI no longer runs the lockfile drift check, so pyproject.toml and "
        "requirements.lock.txt could silently disagree (#40)"
    )
    assert 'pip install -e ".[dev]"' not in workflow, (
        'CI has an unpinned `pip install -e ".[dev]"` step again — that is exactly '
        "how `main` went red on 2026-09-25 (#40)"
    )
