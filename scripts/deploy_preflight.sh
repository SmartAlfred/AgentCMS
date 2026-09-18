#!/usr/bin/env bash
# Migration drift check (#24): fail the rollout when the committed schema and
# the database disagree.  `alembic check` returns non-zero when newer migration
# files have not been applied (or autogen drift exists), which is exactly the
# "migration gate" the deploy must pass before starting a rolling update.
#
# Unlike the destructive `make gate-migrations` (which wipes the DB in CI),
# this is safe against a live database: it only reads metadata and compares
# it (`alembic check` never issues DDL).

set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

VENV_PY="${VENV_PY:-.venv/bin/python}"

if ! "$VENV_PY" -m alembic check >/dev/null 2>&1; then
  echo "pending migrations or schema drift detected by 'alembic check'" >&2
  exit 1
fi
exit 0