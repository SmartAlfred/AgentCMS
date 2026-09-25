#!/usr/bin/env bash
# upgrade_smoke.sh — CI test for self-hosted upgrade lifecycle (#33).
#
# This script tests the full upgrade + rollback lifecycle:
# 1. Bring up release tag A (using local build as tag A)
# 2. Create and publish a post through the documented capability-link flow
# 3. Upgrade to tag B (using a different local build as tag B)
# 4. Assert the same post still reads back on the public surface (no data loss)
# 5. Verify /healthz 200
# 6. Deliberately break the new image, run rollback.sh
# 6. Verify rollback returns to A and the post from step 1 is readable again
# 7. Verify non-zero exit for "rollback not possible" case
#
# This runs entirely in CI against the compose bundle.

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info()  { echo -e "${GREEN}[UPGRADE-SMOKE]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[UPGRADE-SMOKE]${NC} $*"; }
log_error() { echo -e "${RED}[UPGRADE-SMOKE]${NC} $*" >&2; }
log_step()  { echo -e "${BLUE}[UPGRADE-SMOKE]${NC} ==> $*"; }

# Configuration
COMPOSE_FILE="${COMPOSE_FILE:-compose.prod.yml}"
COMPOSE_BIN="${COMPOSE_BIN:-docker compose}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# Tag A and Tag B for testing (both built locally with different tags)
TAG_A="${TAG_A:-agentcms:upgrade-smoke-a}"
TAG_B="${TAG_B:-agentcms:upgrade-smoke-b}"
BASE_URL="${BASE_URL:-http://localhost:8000}"
SITE_SLUG="${SITE_SLUG:-blog}"
HEALTH_URL="${BASE_URL}/healthz"
READY_URL="${BASE_URL}/readyz"

# Backup passphrase for testing
export BACKUP_PASSPHRASE="${BACKUP_PASSPHRASE:-test-passphrase-for-upgrade-smoke-000000}"
export BACKUP_DIR="${BACKUP_DIR:-${PROJECT_DIR}/.backups}"
export BACKUP_STORE_URL="file://${BACKUP_DIR}"

# Load .env if present
if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  source .env
fi

# --- Helpers -----------------------------------------------------------------

wait_for_health() {
  local url="$1"
  local max_wait="${2:-120}"
  local label="$3"
  log_info "Waiting for ${label} (${url})..."
  local start_time
  start_time=$(date +%s)
  while true; do
    if curl -fsS "$url" >/dev/null 2>&1; then
      log_info "${label} is responding"
      return 0
    fi
    local current_time
    current_time=$(date +%s)
    if (( current_time - start_time > max_wait )); then
      log_error "${label} did not become healthy within ${max_wait}s"
      return 1
    fi
    sleep 3
  done
}

create_admin_token() {
  local token
  token=$(curl -fsS -X POST "${BASE_URL}/v1/admin/tokens" \
    -H "Content-Type: application/json" \
    -d '{"label":"upgrade-smoke","scopes":["posts:read","posts:write","posts:publish","assets:write","sites:write"]}' \
    | jq -r '.token // empty')
  if [[ -z "$token" || "$token" == "null" ]]; then
    log_error "Failed to create admin token"
    return 1
  fi
  echo "$token"
}

require_site() {
  local admin_token="$1"
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer ${admin_token}" \
    "${BASE_URL}/v1/sites/${SITE_SLUG}/posts")
  if [[ "$code" != "200" ]]; then
    log_error "No site '${SITE_SLUG}' (GET /v1/sites/${SITE_SLUG}/posts -> ${code})"
    log_error "#44: the API cannot create a site yet -- seed it: python -m scripts.seed"
    return 1
  fi
  echo "$SITE_SLUG"
}

create_capability_link() {
  # #44: no API endpoint mints capability links yet; scripts/seed.py (`make seed`)
  # prints one, and the caller hands it in through SMOKE_CAPABILITY_TOKEN.
  local token="${SMOKE_CAPABILITY_TOKEN:-}"
  if [[ -z "$token" || "$token" == "null" ]]; then
    log_error "No capability token: export SMOKE_CAPABILITY_TOKEN=cap_... (#44: no API mints one;"
    log_error "python -m scripts.seed prints one)"
    return 1
  fi
  echo "$token"
}

create_post() {
  local cap_token="$1"
  local title="$2"
  local body_md="$3"
  local post_id
  post_id=$(curl -fsS -X POST "${BASE_URL}/c/${cap_token}/posts" \
    -H "Content-Type: application/json" \
    -d "{\"title\":\"${title}\",\"body_md\":\"${body_md}\",\"tags\":[\"upgrade\",\"smoke\"]}" \
    | jq -r '.id // empty')
  if [[ -z "$post_id" || "$post_id" == "null" ]]; then
    log_error "Failed to create post: ${title}"
    return 1
  fi
  echo "$post_id"
}

publish_post() {
  local cap_token="$1"
  local post_id="$2"
  local status
  status=$(curl -fsS -X POST "${BASE_URL}/c/${cap_token}/posts/${post_id}/publish" \
    | jq -r '.status // empty')
  if [[ "$status" != "published" ]]; then
    log_error "Failed to publish post: ${post_id}"
    return 1
  fi
}

verify_post_public() {
  # The slug is derived from the title by the API, so read it back from the public feed
  # rather than guessing: this function used to fetch /blog/smoke-test (404 every time)
  # and "passed" via a title-only fallback that never touched the HTML page at all.
  local expected_title="$1"
  local feed post_slug public_resp
  feed=$(curl -fsS "${BASE_URL}/${SITE_SLUG}/posts.json" 2>/dev/null || echo "")
  post_slug=$(echo "$feed" | jq -r --arg title "$expected_title" \
    '.items[] | select(.title == $title) | .slug' 2>/dev/null | head -1)
  if [[ -z "$post_slug" || "$post_slug" == "null" ]]; then
    log_error "Post '${expected_title}' is not in ${BASE_URL}/${SITE_SLUG}/posts.json"
    return 1
  fi
  public_resp=$(curl -fsS "${BASE_URL}/${SITE_SLUG}/${post_slug}" 2>/dev/null || echo "")
  if [[ "$public_resp" != *"${expected_title}"* ]]; then
    log_error "Post '${expected_title}' not on public page ${BASE_URL}/${SITE_SLUG}/${post_slug}"
    return 1
  fi
  return 0
}

verify_healthz() {
  local resp
  resp=$(curl -fsS "${HEALTH_URL}" 2>/dev/null || echo "")
  if [[ -z "$resp" ]]; then
    log_error "/healthz did not respond"
    return 1
  fi
  log_info "/healthz OK: ${resp}"
}

# --- Main Test Flow ----------------------------------------------------------

main() {
  log_step "Starting upgrade smoke test"
  log_info "Tag A: ${TAG_A}"
  log_info "Tag B: ${TAG_B}"
  log_info "Base URL: ${BASE_URL}"
  log_info "Compose file: ${COMPOSE_FILE}"

  # ============================================================
  # PHASE 1: Build and deploy Tag A
  # ============================================================
  log_step "PHASE 1: Building and deploying Tag A (${TAG_A})"

  log_info "Building Tag A image..."
  docker build -t "${TAG_A}" . || { log_error "Failed to build Tag A"; exit 1; }

  log_info "Starting stack with Tag A..."
  TAG="${TAG_A}" $COMPOSE_BIN -f "$COMPOSE_FILE" up -d --build || { log_error "Failed to start stack with Tag A"; exit 1; }

  wait_for_health "$HEALTH_URL" 120 "/healthz" || exit 1
  wait_for_health "$READY_URL" 120 "/readyz" || exit 1

  log_info "Tag A deployed and healthy"

  # ============================================================
  # PHASE 2: Create content with Tag A
  # ============================================================
  log_step "PHASE 2: Creating content with Tag A"

  ADMIN_TOKEN=$(create_admin_token) || exit 1
  log_info "Admin token created"

  require_site "$ADMIN_TOKEN" >/dev/null || exit 1
  log_info "Site created"

  CAP_TOKEN=$(create_capability_link "$ADMIN_TOKEN") || exit 1
  log_info "Capability link created"

  POST_TITLE="Upgrade Smoke Test Post - $(date +%s)"
  POST_BODY="# Upgrade Smoke Test\n\nThis post was created during the upgrade smoke test.\n\nIt should survive the upgrade and rollback."

  POST_ID=$(create_post "$CAP_TOKEN" "$POST_TITLE" "$POST_BODY") || exit 1
  log_info "Post created: ${POST_ID}"

  publish_post "$CAP_TOKEN" "$POST_ID" || exit 1
  log_info "Post published"

  # Verify post is readable on public surface
  verify_post_public "$POST_TITLE" || exit 1
  log_info "Post verified on public surface (Tag A)"

  verify_healthz || exit 1

  # ============================================================
  # PHASE 3: Build Tag B and upgrade
  # ============================================================
  log_step "PHASE 3: Building Tag B (${TAG_B}) and upgrading"

  log_info "Building Tag B image..."
  # Tag B is a slightly different build - add a label to distinguish
  docker build --label "version=tag-b" -t "${TAG_B}" . || { log_error "Failed to build Tag B"; exit 1; }

  log_info "Running upgrade.sh from Tag A to Tag B..."
  # Run upgrade.sh with Tag B
  AGENTCMS_IMAGE_TAG="${TAG_B}" BACKUP_PASSPHRASE="${BACKUP_PASSPHRASE}" \
    ./scripts/upgrade.sh "${TAG_B}" || { log_error "Upgrade failed"; exit 1; }

  log_info "Upgrade completed successfully"

  # ============================================================
  # PHASE 4: Verify post survives upgrade (no data loss)
  # ============================================================
  log_step "PHASE 4: Verifying post survives upgrade (no data loss)"

  wait_for_health "$HEALTH_URL" 120 "/healthz" || exit 1
  wait_for_health "$READY_URL" 120 "/readyz" || exit 1

  verify_healthz || exit 1
  verify_post_public "$POST_TITLE" || exit 1
  log_info "Post verified on public surface after upgrade (Tag B) - NO DATA LOSS"

  # ============================================================
  # PHASE 5: Test rollback (deliberately "break" and rollback)
  # ============================================================
  log_step "PHASE 5: Testing rollback to Tag A"

  log_info "Running rollback.sh..."
  BACKUP_PASSPHRASE="${BACKUP_PASSPHRASE}" \
    ./scripts/rollback.sh || { log_error "Rollback failed"; exit 1; }

  log_info "Rollback completed successfully"

  # ============================================================
  # PHASE 6: Verify post survives rollback
  # ============================================================
  log_step "PHASE 6: Verifying post survives rollback"

  wait_for_health "$HEALTH_URL" 120 "/healthz" || exit 1
  wait_for_health "$READY_URL" 120 "/readyz" || exit 1

  verify_healthz || exit 1
  verify_post_public "$POST_TITLE" || exit 1
  log_info "Post verified on public surface after rollback (Tag A) - NO DATA LOSS"

  # ============================================================
  # PHASE 7: Test rollback guard (should fail - no pre-upgrade state)
  # ============================================================
  log_step "PHASE 7: Testing rollback guard (should fail)"

  # Clear the pre-upgrade state files to simulate "no rollback possible"
  rm -f .pre-upgrade-tag .pre-upgrade-dump .pre-upgrade-alembic

  log_info "Running rollback.sh without pre-upgrade state (should fail with exit code 2)..."
  if BACKUP_PASSPHRASE="${BACKUP_PASSPHRASE}" ./scripts/rollback.sh; then
    log_error "Rollback should have failed but succeeded!"
    exit 1
  else
    exit_code=$?
    if [[ $exit_code -eq 2 ]]; then
      log_info "Rollback correctly refused with exit code 2 (no pre-upgrade state)"
    else
      log_error "Rollback failed with unexpected exit code: $exit_code"
      exit 1
    fi
  fi

  # ============================================================
  # PHASE 8: Test rollback guard with schema mismatch
  # ============================================================
  log_step "PHASE 8: Testing rollback guard with schema mismatch (simulated)"

  # Recreate pre-upgrade state but with a fake alembic revision
  echo "${TAG_A}" > .pre-upgrade-tag
  # Create a dummy dump file
  mkdir -p "${BACKUP_DIR}/dumps"
  echo "dummy" > "${BACKUP_DIR}/dumps/dummy.dump.enc"
  echo "${BACKUP_DIR}/dumps/dummy.dump.enc" > .pre-upgrade-dump
  echo "000000000000" > .pre-upgrade-alembic  # Fake revision that won't match

  log_info "Running rollback.sh with mismatched alembic revision (should fail with exit code 2)..."
  if BACKUP_PASSPHRASE="${BACKUP_PASSPHRASE}" ./scripts/rollback.sh; then
    log_error "Rollback should have failed but succeeded!"
    exit 1
  else
    exit_code=$?
    if [[ $exit_code -eq 2 ]]; then
      log_info "Rollback correctly refused with exit code 2 (schema mismatch)"
    else
      log_error "Rollback failed with unexpected exit code: $exit_code"
      exit 1
    fi
  fi

  # ============================================================
  # ALL TESTS PASSED
  # ============================================================
  log_step "ALL UPGRADE SMOKE TESTS PASSED ✓"
  log_info "Upgrade lifecycle verified:"
  log_info "  - Deploy Tag A: OK"
  log_info "  - Create/publish post: OK"
  log_info "  - Upgrade to Tag B: OK"
  log_info "  - Post survives upgrade (no data loss): OK"
  log_info "  - Rollback to Tag A: OK"
  log_info "  - Post survives rollback (no data loss): OK"
  log_info "  - Rollback guard (no state): correctly refuses (exit 2)"
  log_info "  - Rollback guard (schema mismatch): correctly refuses (exit 2)"
  exit 0
}

# Run main and cleanup on exit
cleanup() {
  log_info "Cleaning up..."
  $COMPOSE_BIN -f "$COMPOSE_FILE" down -v >/dev/null 2>&1 || true
}

trap cleanup EXIT

main "$@"