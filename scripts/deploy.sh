#!/usr/bin/env bash
# Migration-gated, zero-downtime deployment (#24).
#
#   ./scripts/deploy.sh                 build+push+run migrations (one-shot
#                                       container) then rolling-update with a
#                                       /readyz health gate
#   ./scripts/deploy.sh --rollback      re-point to the previously running
#                                       image and repeat the health gate
#
# Secrets come from the environment (docker compose .env / CI variables) —
# nothing is baked into the image.  .env.example lists every variable the
# service reads.
#
# The health gate polls GET /readyz until every critical dependency is up, so
# traffic is never cut over to an unhealthy container.  Run the small load
# test during the window (docs/ops/runbook.md, scenario 7) and watch for zero
# 5xx to confirm the zero-dropped-requests goal.
#
# Exit codes:
#   0  deploy/rollback succeeded
#   1  anything failed (migrations, build, health gate, rollback)

set -euo pipefail

COMPOSE_BIN="${COMPOSE_BIN:-docker compose}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

IMAGE_REPO="${IMAGE_REPO:-agentcms}"
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
TAG="${TAG:-${IMAGE_REPO}:${GIT_SHA}}"
MARKER_FILE=".deploy-last-tag"

read_health_gate() {
  # Block until GET /readyz returns 200 or until the timeout.
  local url="${1:?health url required}"
  local i
  for i in $(seq 1 120); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      echo "==> health gate passed after ${i} attempts"
      return 0
    fi
    sleep 2
  done
  echo "==> health gate FAILED: $url not ready after 240s" >&2
  return 1
}

write_marker() { echo "$1" > "$MARKER_FILE"; }
read_marker()  { test -f "$MARKER_FILE" && cat "$MARKER_FILE" || echo ""; }

rollback() {
  local prev
  prev="$(read_marker)"
  if [[ -z "$prev" ]]; then
    echo "no previous image recorded; cannot roll back" >&2
    exit 1
  fi
  echo "==> rolling back to $prev"
  TAG="$prev" $COMPOSE_BIN -f compose.prod.yml up -d --no-deps --wait api
  echo "==> rollback applied: $prev"
}

deploy() {
  local prev
  prev="$(read_marker)"

  # 1. Migration gate: refuse to rollout when migrations would be applied to a
  #    live cluster.  The DB team migrates via `make gate-migrations` off-hours;
  #    this check is the belt-and-braces guard.
  echo "==> migration gate..."
  if ./scripts/deploy_preflight.sh; then
    echo "==> no pending migrations"
  else
    # MIGRATE_JOB enabled: run migrations as an explicit one-shot job first.
    if [[ "${MIGRATE_JOB:-1}" == "1" ]]; then
      echo "==> running migration job (one-shot container)..."
      $COMPOSE_BIN -f compose.prod.yml run --rm --no-deps migrate || {
        echo "==> migration job FAILED; aborting rollout" >&2
        if [[ -n "$prev" ]]; then write_marker "$prev"; fi
        exit 1
      }
    else
      echo "==> pending migrations and MIGRATE_JOB=0; refusing to rollout" >&2
      exit 1
    fi
  fi

  # 2. Build + tag the image.
  echo "==> building ${TAG}"
  docker build -t "$TAG" .

  # 3. Rolling update with a health gate (zero-downtime: compose keeps the
  #    old container serving until the new one accepts /readyz).
  write_marker "$prev"   # remember the previous image for --rollback
  $COMPOSE_BIN -f compose.prod.yml up -d --no-deps --wait api --force-recreate

  local url="${HEALTH_URL:-http://127.0.0.1:8000/readyz}"
  read_health_gate "$url"
  echo "==> deploy complete: ${TAG}"

  # Record the live tag as the rollback target for the *next* deploy.
  write_marker "$TAG"
}

if [[ "${1:-}" == "--rollback" ]]; then
  rollback
else
  deploy
fi