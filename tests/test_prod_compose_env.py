"""Docker-free guard for #44: every production compose service must get ADMIN_TOKEN.

The migration container boots the *same* production settings as the API.  When
`ADMIN_TOKEN` became [prod-required], `migrate` was not given it, `Settings`
refused to start in production, and `docker compose up` aborted the whole stack
with `error: docker compose up failed`.  The local gate could not see this: the
self-host E2E needs Docker and only runs in CI, so the wiring is asserted here
from the compose files themselves, where it broke.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

PROD_COMPOSE_FILES = (
    "compose.prod.yml",
    "deploy/compose/docker-compose.prod.yml",
)


def _services(path: Path) -> dict[str, str]:
    """Map service name -> its raw block. No YAML dependency on purpose."""
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in path.read_text().splitlines():
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            current = match.group(1)
            blocks[current] = []
            continue
        if current is not None:
            blocks[current].append(line)
    return {name: "\n".join(lines) for name, lines in blocks.items()}


def test_production_compose_services_get_the_admin_bootstrap_secret() -> None:
    for rel in PROD_COMPOSE_FILES:
        blocks = _services(REPO_ROOT / rel)
        production = {name: block for name, block in blocks.items() if "APP_ENV: production" in block}
        assert production, f"{rel}: expected at least one production service"
        for name, block in production.items():
            assert re.search(r"^\s+ADMIN_TOKEN:", block, re.M), (
                f"{rel}: service '{name}' runs with APP_ENV=production but is not given "
                "ADMIN_TOKEN; Settings refuses to boot in production without it (#44)"
            )
