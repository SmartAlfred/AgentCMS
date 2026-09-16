#!/usr/bin/env bash
# Wait for Postgres, apply migrations, then exec the server (#2).
set -euo pipefail

if [[ -n "${DATABASE_URL:-}" ]]; then
  echo "==> waiting for the database"
  for _ in $(seq 1 60); do
    if alembic current >/dev/null 2>&1 || alembic upgrade head --sql >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  echo "==> applying migrations"
  alembic upgrade head
fi

exec "$@"
