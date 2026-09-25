"""Fail when the committed lockfile drifts from pyproject.toml (#40).

CI installs its dependencies from ``requirements.lock.txt``, so nothing can
change what CI tests without an explicit commit. This is the counterweight:
it fails when pyproject.toml declares a dependency that the lock does not pin,
or pins one outside the specifier pyproject.toml allows.

Run it from the repo root (CI does)::

    python scripts/check_lock.py

Regenerate the lock with::

    python -m pip install pip-tools
    pip-compile --extra dev --strip-extras --output-file requirements.lock.txt pyproject.toml
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "requirements.lock.txt"
PYPROJECT_PATH = ROOT / "pyproject.toml"
REGENERATE = (
    "Regenerate with: python -m pip install pip-tools && "
    "pip-compile --extra dev --strip-extras --output-file requirements.lock.txt pyproject.toml"
)

# `name==1.2.3`, optionally with extras and/or an environment marker.
_PIN_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==(?P<version>[^;\s]+)(?:\s*;.*)?$"
)


def declared_requirements(pyproject_path: Path = PYPROJECT_PATH) -> list[Requirement]:
    """Every dependency pyproject.toml declares: runtime plus the `dev` extra."""
    data = tomllib.loads(pyproject_path.read_text("utf-8"))
    project = data["project"]
    raw: list[str] = list(project.get("dependencies", []))
    raw += list(project.get("optional-dependencies", {}).get("dev", []))
    return [Requirement(item) for item in raw]


def locked_versions(lock_path: Path = LOCK_PATH) -> dict[str, str]:
    """Map canonicalised package name -> pinned version, from the lockfile."""
    pins: dict[str, str] = {}
    for line in lock_path.read_text("utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or entry.startswith("-"):
            continue
        match = _PIN_RE.match(entry)
        if match is None:
            raise ValueError(f"{lock_path.name}: expected a `name==version` pin, got {entry!r}")
        pins[canonicalize_name(match.group("name"))] = match.group("version")
    return pins


def check(pyproject_path: Path = PYPROJECT_PATH, lock_path: Path = LOCK_PATH) -> list[str]:
    """Return a list of human-readable drift problems (empty means in sync)."""
    pins = locked_versions(lock_path)
    problems: list[str] = []
    for requirement in declared_requirements(pyproject_path):
        name = canonicalize_name(requirement.name)
        pinned = pins.get(name)
        if pinned is None:
            problems.append(
                f"{requirement.name}: declared in pyproject.toml but not pinned in {lock_path.name}"
            )
            continue
        try:
            version = Version(pinned)
        except InvalidVersion:
            problems.append(f"{requirement.name}: {lock_path.name} pins a non-version {pinned!r}")
            continue
        if requirement.specifier and version not in requirement.specifier:
            problems.append(
                f"{requirement.name}: {lock_path.name} pins {pinned}, which does not satisfy "
                f"{requirement.specifier} from pyproject.toml"
            )
    return problems


def main() -> int:
    try:
        problems = check()
    except (OSError, ValueError, KeyError) as exc:
        print(f"check_lock: cannot verify the lock: {exc}", file=sys.stderr)
        return 1
    if problems:
        print(f"{LOCK_PATH.name} has drifted from pyproject.toml:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(REGENERATE, file=sys.stderr)
        return 1
    print(f"{LOCK_PATH.name} agrees with pyproject.toml ({len(locked_versions())} packages pinned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
