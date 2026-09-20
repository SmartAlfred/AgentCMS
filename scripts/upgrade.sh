#!/usr/bin/env bash
# upgrade.sh — Self-hosted upgrade lifecycle (#33).
#
#   ./scripts/upgrade.sh <NEW_TAG>
#
# Flow: pull -> preflight -> backup -> migrate -> restart -> smoke
#
# A verified pre-upgrade backup of the database is taken BEFORE the first
# migration runs (reusing scripts/backup.py / scripts/pgbackup.py). If the
# backup or preflight fails, abort with a clear error and leave the running
# app untouched.
#
# Exit codes:
#   0  upgrade succeeded
#   1  anything failed (preflight, alembic-record, backup, migrate, health gate, smoke)
#   2  usage / argument error

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

# State files
MARKER_FILE=".deploy-last-tag"           # current live tag (written by deploy.sh / upgrade.sh)
PRE_UPGRADE_DUMP_FILE=".pre-upgrade-dump"    # path to the pre-upgrade encrypted dump
PRE_UPGRADE_TAG_FILE=".pre-upgrade-tag"      # previous image tag for rollback
PRE_UPGRADE_ALEMBIC_FILE=".pre-upgrade-alembic"  # alembic revision at backup time

# --- Helpers -----------------------------------------------------------------

log_info()  { echo -e "\033[0;32m[UPGRADE]\033[0m $*"; }
log_warn()  { echo -e "\033[1;33m[UPGRADE]\033[0m $*"; }
log_error() { echo -e "\033[0;31m[UPGRADE]\033[0m $*" >&2; }

read_pre_upgrade_alembic()  { [[ -f "$PRE_UPGRADE_ALEMBIC_FILE" ]] && cat "$PRE_UPGRADE_ALEMBIC_FILE" || echo ""; }
write_pre_upgrade_alembic() { echo "$1" > "$PRE_UPGRADE_ALEMBIC_FILE"; }

usage() {
  cat <<EOF
Usage: $(basename "$0") <NEW_TAG>

Upgrade AgentCMS to a new version-pinned image tag.

Arguments:
  NEW_TAG    Explicit immutable image tag (e.g. ghcr.io/owner/agentcms:v0.3.1
             or ghcr.io/owner/agentcms@sha256:abc123...)

Environment:
  COMPOSE_BIN      docker compose command (default: "docker compose")
  COMPOSE_FILE     path to compose file (default: "deploy/compose/docker-compose.prod.yml")
  BACKUP_PASSPHRASE  required for encrypted backup (from .env or env)
  SMOKE_BASE_URL   base URL for smoke tests (default: https://\${DOMAIN:-localhost})

Example:
  AGENTCMS_IMAGE_TAG=ghcr.io/owner/agentcms:v0.3.1 ./scripts/upgrade.sh ghcr.io/owner/agentcms:v0.3.1
EOF
}

read_marker()  { [[ -f "$MARKER_FILE" ]] && cat "$MARKER_FILE" || echo ""; }
write_marker() { echo "$1" > "$MARKER_FILE"; }

read_pre_upgrade_dump()  { [[ -f "$PRE_UPGRADE_DUMP_FILE" ]] && cat "$PRE_UPGRADE_DUMP_FILE" || echo ""; }
write_pre_upgrade_dump() { echo "$1" > "$PRE_UPGRADE_DUMP_FILE"; }

read_pre_upgrade_tag()  { [[ -f "$PRE_UPGRADE_TAG_FILE" ]] && cat "$PRE_UPGRADE_TAG_FILE" || echo ""; }
write_pre_upgrade_tag() { echo "$1" > "$PRE_UPGRADE_TAG_FILE"; }

# --- Steps -------------------------------------------------------------------

step_pull() {
  local new_tag="$1"
  log_info "Pulling new image: ${new_tag}"
  AGENTCMS_IMAGE_TAG="${new_tag}" $COMPOSE_BIN -f "$COMPOSE_FILE" pull api migrate
}

step_preflight() {
  log_info "Running preflight checks..."

  # 1. Check that the new image tag is valid (not empty, not "latest")
  local new_tag="$1"
  if [[ -z "$new_tag" ]]; then
    log_error "NEW_TAG is empty"
    return 1
  fi
  if [[ "$new_tag" == *"latest"* ]] || [[ "$new_tag" == *":local"* ]]; then
    log_error "Refusing to upgrade to mutable tag: $new_tag (must be immutable version tag or digest)"
    return 1
  fi

  # 2. Run alembic check against the live database (read-only)
  log_info "Checking for pending migrations (alembic check)..."
  if ! $COMPOSE_BIN -f "$COMPOSE_FILE" run --rm --no-deps \
       -e AGENTCMS_IMAGE_TAG="${new_tag}" \
       migrate python -m alembic check >/dev/null 2>&1; then
    log_error "Preflight failed: pending migrations or schema drift detected by 'alembic check'"
    log_error "Run migrations off-hours with the migrate job, then retry upgrade."
    return 1
  fi
  log_info "Preflight passed: no pending migrations"
}

step_record_alembic() {
  local new_tag="$1"
  log_info "Recording current alembic revision for rollback safety..."

  local current_rev
  current_rev=$($COMPOSE_BIN -f "$COMPOSE_FILE" run --rm --no-deps \
    -e AGENTCMS_IMAGE_TAG="${new_tag}" \
    migrate python -m alembic current 2>/dev/null | head -1 | awk '{print $1}')

  if [[ -z "$current_rev" ]]; then
    log_error "Could not determine current alembic revision"
    return 1
  fi

  write_pre_upgrade_alembic "$current_rev"
  log_info "Recorded pre-upgrade alembic revision: ${current_rev}"
}

step_backup() {
  local new_tag="$1"
  log_info "Taking pre-upgrade backup..."

  # Ensure BACKUP_PASSPHRASE is set
  if [[ -z "${BACKUP_PASSPHRASE:-}" ]]; then
    log_error "BACKUP_PASSPHRASE is not set; cannot create encrypted backup"
    log_error "Set BACKUP_PASSPHRASE in deploy/.env or environment"
    return 1
  fi

  # Run the backup via the backup.py module (uses pg_dump + openssl encryption)
  # The backup is written to the configured BACKUP_DIR/dumps/
  local backup_out
  backup_out=$($COMPOSE_BIN -f "$COMPOSE_FILE" run --rm --no-deps \
    -e AGENTCMS_IMAGE_TAG="${new_tag}" \
    -e BACKUP_PASSPHRASE="${BACKUP_PASSPHRASE}" \
    api python -m scripts.backup 2>&1) || {
    log_error "Pre-upgrade backup failed:"
    echo "$backup_out" >&2
    return 1
  }

  # Extract the dump filename from the backup output
  # Expected output: "backup ok: <filename> (<elapsed>s)"
  local dump_name
  dump_name=$(echo "$backup_out" | grep -oE 'backup ok: [^ ]+' | cut -d' ' -f3)
  if [[ -z "$dump_name" ]]; then
    log_error "Could not determine backup dump filename from output"
    echo "$backup_out" >&2
    return 1
  fi

  # Determine the full path to the dump
  local backup_dir="${BACKUP_DIR:-.backups}"
  local dump_path="${backup_dir}/dumps/${dump_name}"
  if [[ ! -f "$dump_path" ]]; then
    # Try relative to project dir
    dump_path="${PROJECT_DIR}/${backup_dir}/dumps/${dump_name}"
  fi
  if [[ ! -f "$dump_path" ]]; then
    log_error "Backup dump not found at expected path: $dump_path"
    return 1
  fi

  write_pre_upgrade_dump "$dump_path"
  log_info "Pre-upgrade backup verified: ${dump_path}"
}

step_migrate() {
  local new_tag="$1"
  log_info "Running database migrations..."

  # Run the migrate service with the new image
  # This runs: python -m alembic upgrade head
  if ! AGENTCMS_IMAGE_TAG="${new_tag}" $COMPOSE_BIN -f "$COMPOSE_FILE" run --rm --no-deps migrate; then
    log_error "Migration failed — the new schema was NOT applied"
    log_error "The running app (old image) is still serving on the old schema"
    return 1
  fi
  log_info "Migrations applied successfully"
}

step_restart() {
  local new_tag="$1"
  log_info "Restarting API with new image..."

  # Record the previous tag for potential rollback
  local prev_tag
  prev_tag=$(read_marker)
  if [[ -n "$prev_tag" ]]; then
    write_pre_upgrade_tag "$prev_tag"
  fi

  # Rolling update: compose will start new container, wait for /readyz, then cut over
  if ! AGENTCMS_IMAGE_TAG="${new_tag}" $COMPOSE_BIN -f "$COMPOSE_FILE" up -d --no-deps --wait api; then
    log_error "API restart failed — new container did not become healthy"
    return 1
  fi
  log_info "API restarted and healthy"
}

step_smoke() {
  local new_tag="$1"
  log_info "Running post-upgrade smoke tests..."

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
}

# --- Main --------------------------------------------------------------------

main() {
  if [[ $# -ne 1 ]]; then
    usage
    exit 2
  fi

  local new_tag="$1"
  local prev_tag
  prev_tag=$(read_marker)

  log_info "Starting upgrade from ${prev_tag:-<none>} to ${new_tag}"

  # Validate we have a running stack to upgrade
  if [[ -z "$prev_tag" ]]; then
    log_warn "No previous deployment marker found (.deploy-last-tag)"
    log_warn "This may be a fresh deploy; proceeding anyway"
  fi

  # Step 1: Pull new image
  step_pull "$new_tag" || { log_error "Pull failed"; exit 1; }

  # Step 2: Preflight
  step_preflight "$new_tag" || { log_error "Preflight failed — upgrade aborted, app untouched"; exit 1; }

  # Step 3: Record alembic revision (BEFORE backup, for rollback safety)
  step_record_alembic "$new_tag" || { log_error "Failed to record alembic revision"; exit 1; }

  # Step 4: Backup (BEFORE any migration)
  step_backup "$new_tag" || { log_error "Backup failed — upgrade aborted, app untouched"; exit 1; }

  # Step 4: Migrate
  step_migrate "$new_tag" || { log_error "Migration failed — upgrade aborted, app still on old schema"; exit 1; }

  # Step 5: Restart
  step_restart "$new_tag" || { log_error "Restart failed"; exit 1; }

  # Step 6: Smoke
  step_smoke "$new_tag" || { log_error "Smoke tests failed"; exit 1; }

  # Success: record the new live tag
  write_marker "$new_tag"
  log_info "Upgrade complete: now running ${new_tag}"
}

main "$@"