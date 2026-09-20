#!/usr/bin/env bash
# rollback.sh — Self-hosted rollback lifecycle (#33).
#
#   ./scripts/rollback.sh
#
# Flow: verify rollback is safe -> restore pre-upgrade dump -> restart old image -> smoke
#
# The pre-upgrade backup (created by upgrade.sh) is restored. If the schema has
# migrated past a point where rollback is safe (i.e., the current alembic head
# is not the same as the head at backup time, and no downgrade path exists),
# refuse with a non-zero exit and an explicit message rather than silently
# corrupting data.
#
# Exit codes:
#   0  rollback succeeded
#   1  anything failed (guard, restore, restart, health gate, smoke)
#   2  rollback not possible (schema mismatch, no pre-upgrade state)
#   3  usage / argument error

set -euo pipefail

# --- Configuration ----------------------------------------------------------

COMPOSE_BIN="${COMPOSE_BIN:-docker compose}"
COMPOSE_FILE="${COMPOSE_FILE:-compose.prod.yml}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# Load .env if present (for POSTGRES_PASSWORD etc.)
if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  source .env
fi

# State files (written by upgrade.sh)
MARKER_FILE=".deploy-last-tag"          # current live tag
PRE_UPGRADE_DUMP_FILE=".pre-upgrade-dump"  # path to the pre-upgrade encrypted dump
PRE_UPGRADE_TAG_FILE=".pre-upgrade-tag"    # previous image tag for rollback
PRE_UPGRADE_ALEMBIC_FILE=".pre-upgrade-alembic"  # alembic revision at backup time

# --- Helpers -----------------------------------------------------------------

log_info()  { echo -e "\033[0;32m[ROLLBACK]\033[0m $*"; }
log_warn()  { echo -e "\033[1;33m[ROLLBACK]\033[0m $*"; }
log_error() { echo -e "\033[0;31m[ROLLBACK]\033[0m $*" >&2; }

usage() {
  cat <<EOF
Usage: $(basename "$0")

Rollback AgentCMS to the previous version-pinned image tag and database state.

This restores the pre-upgrade database dump (taken by upgrade.sh before any
migrations ran) and re-points the API to the previous image tag.

Environment:
  COMPOSE_BIN      docker compose command (default: "docker compose")
  COMPOSE_FILE     path to compose file (default: "deploy/compose/docker-compose.prod.yml")
  BACKUP_PASSPHRASE  required for encrypted restore (from .env or env)
  SMOKE_BASE_URL   base URL for smoke tests (default: https://\${DOMAIN:-localhost})

Example:
  BACKUP_PASSPHRASE=xxx ./scripts/rollback.sh
EOF
}

read_marker()  { [[ -f "$MARKER_FILE" ]] && cat "$MARKER_FILE" || echo ""; }
write_marker() { echo "$1" > "$MARKER_FILE"; }

read_pre_upgrade_dump()  { [[ -f "$PRE_UPGRADE_DUMP_FILE" ]] && cat "$PRE_UPGRADE_DUMP_FILE" || echo ""; }
write_pre_upgrade_dump() { echo "$1" > "$PRE_UPGRADE_DUMP_FILE"; }

read_pre_upgrade_tag()  { [[ -f "$PRE_UPGRADE_TAG_FILE" ]] && cat "$PRE_UPGRADE_TAG_FILE" || echo ""; }
write_pre_upgrade_tag() { echo "$1" > "$PRE_UPGRADE_TAG_FILE"; }

read_pre_upgrade_alembic()  { [[ -f "$PRE_UPGRADE_ALEMBIC_FILE" ]] && cat "$PRE_UPGRADE_ALEMBIC_FILE" || echo ""; }
write_pre_upgrade_alembic() { echo "$1" > "$PRE_UPGRADE_ALEMBIC_FILE"; }

# --- Rollback Safety Guard ---------------------------------------------------

# Check if rolling back the database is safe. This compares the alembic revision
# at backup time with the current alembic head. If they differ and the current
# head has no downgrade path (or downgrade would lose data), refuse.
step_guard() {
  local pre_upgrade_rev
  pre_upgrade_rev=$(read_pre_upgrade_alembic)

  if [[ -z "$pre_upgrade_rev" ]]; then
    log_error "No pre-upgrade alembic revision recorded (.pre-upgrade-alembic missing)"
    log_error "Cannot verify rollback safety — refusing to proceed"
    return 2
  fi

  log_info "Checking rollback safety: pre-upgrade revision was ${pre_upgrade_rev}"

  # Get current alembic head from the current image
  local current_tag
  current_tag=$(read_marker)
  if [[ -z "$current_tag" ]]; then
    log_warn "No current deployment marker found; cannot verify alembic revision"
    return 2
  fi

  local current_rev
  current_rev=$($COMPOSE_BIN -f "$COMPOSE_FILE" run --rm --no-deps \
    -e AGENTCMS_IMAGE_TAG="${current_tag}" \
    migrate python -m alembic current --verbose 2>/dev/null | head -1 | awk '{print $1}')

  if [[ -z "$current_rev" ]]; then
    log_warn "Could not determine current alembic revision; assuming unsafe"
    return 2
  fi

  log_info "Current alembic revision: ${current_rev}"

  if [[ "$pre_upgrade_rev" == "$current_rev" ]]; then
    log_info "Schema unchanged since backup — rollback is safe"
    return 0
  fi

  local current_tag
  current_tag=$(read_marker)

  # Schema has changed. Check if a downgrade path exists from current to pre-upgrade.
  # We do this by attempting a dry-run downgrade SQL generation.
  log_info "Schema differs from backup; checking if downgrade to ${pre_upgrade_rev} is possible..."

  local downgrade_sql
  downgrade_sql=$($COMPOSE_BIN -f "$COMPOSE_FILE" run --rm --no-deps \
    -e AGENTCMS_IMAGE_TAG="${current_tag}" \
    migrate python -m alembic downgrade "${pre_upgrade_rev}" --sql 2>&1) || {
    log_error "Downgrade path from ${current_rev} to ${pre_upgrade_rev} does not exist or would fail"
    log_error "Rollback would lose data — refusing to proceed"
    log_error "To force: restore from an external backup manually"
    return 2
  }

  # Check if the downgrade SQL contains DROP TABLE, DROP COLUMN, or other
  # destructive operations that would lose data
  if echo "$downgrade_sql" | grep -qiE "(DROP TABLE|DROP COLUMN|DELETE FROM|TRUNCATE)"; then
    log_error "Downgrade SQL contains destructive operations (DROP/DELETE/TRUNCATE)"
    log_error "Rollback would lose data — refusing to proceed"
    return 2
  fi

  log_info "Downgrade path exists and appears non-destructive — rollback is safe"
  return 0
}

# --- Steps -------------------------------------------------------------------

step_restore_dump() {
  local dump_path
  dump_path=$(read_pre_upgrade_dump)

  if [[ -z "$dump_path" || ! -f "$dump_path" ]]; then
    log_error "Pre-upgrade dump not found: ${dump_path}"
    log_error "Cannot rollback database — dump missing or corrupted"
    return 1
  fi

  if [[ -z "${BACKUP_PASSPHRASE:-}" ]]; then
    log_error "BACKUP_PASSPHRASE is not set; cannot decrypt backup"
    log_error "Set BACKUP_PASSPHRASE in deploy/.env or environment"
    return 1
  fi

  log_info "Restoring pre-upgrade database dump: ${dump_path}"

  # Use the restore.py module to restore directly into the live database
  local db_url="postgresql+psycopg://${POSTGRES_USER:-agentcms}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB:-agentcms}"

  if ! $COMPOSE_BIN -f "$COMPOSE_FILE" run --rm --no-deps \
       -e DATABASE_URL="${db_url}" \
       -e BACKUP_PASSPHRASE="${BACKUP_PASSPHRASE}" \
       api python -m scripts.restore --dump "$dump_path" --target-url "${db_url}" 2>&1; then
    log_error "Database restore failed"
    return 1
  fi

  log_info "Database restored successfully from pre-upgrade dump"
  return 0
}

step_restart_old_image() {
  local prev_tag
  prev_tag=$(read_pre_upgrade_tag)

  if [[ -z "$prev_tag" ]]; then
    log_error "No previous image tag recorded (.pre-upgrade-tag missing)"
    log_error "Cannot rollback — no previous image to restore"
    return 1
  fi

  log_info "Restarting API with previous image: ${prev_tag}"

  # Rolling update: compose will start old container, wait for /readyz, then cut over
  if ! AGENTCMS_IMAGE_TAG="${prev_tag}" $COMPOSE_BIN -f "$COMPOSE_FILE" up -d --no-deps --wait api; then
    log_error "API restart failed — old container did not become healthy"
    return 1
  fi

  log_info "API restarted and healthy on ${prev_tag}"
  return 0
}

step_smoke() {
  local prev_tag
  prev_tag=$(read_pre_upgrade_tag)

  log_info "Running post-rollback smoke tests..."

  local base_url="${SMOKE_BASE_URL:-https://${DOMAIN:-localhost}}"
  local health_url="${base_url}/healthz"
  local ready_url="${base_url}/readyz"

  # Wait for /healthz
  log_info "Waiting for /healthz..."
  local i
  for i in $(seq 1 60); do
    if curl -fsS "$health_url" >/dev/null 2>&1; then
      log_info "/healthz OK"
      break
    fi
    if [[ $i -eq 60 ]]; then
      log_error "/healthz did not respond after 120s"
      return 1
    fi
    sleep 2
  done

  # Wait for /readyz
  log_info "Waiting for /readyz..."
  for i in $(seq 1 60); do
    if curl -fsS "$ready_url" >/dev/null 2>&1; then
      log_info "/readyz OK"
      break
    fi
    if [[ $i -eq 60 ]]; then
      log_error "/readyz did not respond after 120s"
      return 1
    fi
    sleep 2
  done

  # Basic API version check
  log_info "Checking /v1/version..."
  local version_resp
  version_resp=$(curl -fsS "${base_url}/v1/version" 2>/dev/null || echo "failed")
  if [[ "$version_resp" == "failed" ]]; then
    log_error "/v1/version endpoint failed"
    return 1
  fi
  log_info "Version endpoint responded: ${version_resp}"

  log_info "All smoke tests passed"
  return 0
}

# --- Main --------------------------------------------------------------------

main() {
  if [[ $# -ne 0 ]]; then
    usage
    exit 3
  fi

  local prev_tag
  prev_tag=$(read_pre_upgrade_tag)
  local current_tag
  current_tag=$(read_marker)

  log_info "Starting rollback from ${current_tag:-<unknown>} to ${prev_tag:-<unknown>}"

  # Validate we have rollback state
  if [[ -z "$prev_tag" ]]; then
    log_error "No pre-upgrade state found (.pre-upgrade-tag missing)"
    log_error "Cannot rollback — run upgrade.sh first to establish rollback point"
    exit 2
  fi

  # Step 1: Rollback guard (schema safety check)
  step_guard || { exit_code=$?; [[ $exit_code -eq 2 ]] && log_error "Rollback refused: schema past safe downgrade point"; exit $exit_code; }

  # Step 2: Restore pre-upgrade database dump
  step_restore_dump || { log_error "Database restore failed"; exit 1; }

  # Step 3: Restart with old image
  step_restart_old_image || { log_error "API restart failed"; exit 1; }

  # Step 4: Smoke tests
  step_smoke || { log_error "Smoke tests failed"; exit 1; }

  # Success: update the live marker to the rolled-back tag
  write_marker "$prev_tag"
  log_info "Rollback complete: now running ${prev_tag}"
}

main "$@"