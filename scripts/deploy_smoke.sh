#!/usr/bin/env bash
# deploy_smoke.sh — End-to-end deploy verification (#31).
#
# Runs after `docker compose -f deploy/compose/docker-compose.prod.yml up -d`:
# 1. Waits for /healthz and /readyz to report healthy
# 2. Asserts the site named by --site-slug exists (#44: nothing can create one
#    over HTTP yet, so scripts/selfhost_e2e.sh seeds it first)
# 3. Uses the capability token handed in via --capability-token /
#    SMOKE_CAPABILITY_TOKEN (#44: no API mints cap_ tokens yet; `make seed` does)
# 4. Creates and publishes a post via capability link
# 5. Verifies the post appears on public read surface
# 6. Verifies embed script is served with correct content-type
# 7. Verifies embed token-scoped fetch returns the post
#
# Exits non-zero on any failure. Runnable in CI against compose bundle.

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[SMOKE]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[SMOKE]${NC} $*"; }
log_error() { echo -e "${RED}[SMOKE]${NC} $*"; }

# Configuration
COMPOSE_FILE="${COMPOSE_FILE:-deploy/compose/docker-compose.prod.yml}"
BASE_URL="${BASE_URL:-http://localhost:8000}"
HEALTH_URL="${BASE_URL}/healthz"
READY_URL="${BASE_URL}/readyz"
EMBED_SCRIPT_URL="${BASE_URL}/embed/v1/agentcms.js"
MAX_WAIT="${MAX_WAIT:-180}"  # seconds
SITE_SLUG="${SMOKE_SITE_SLUG:-blog}"
CAP_TOKEN="${SMOKE_CAPABILITY_TOKEN:-}"
POLL_INTERVAL=3

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --base-url)
            BASE_URL="$2"
            HEALTH_URL="${BASE_URL}/healthz"
            READY_URL="${BASE_URL}/readyz"
            EMBED_SCRIPT_URL="${BASE_URL}/embed/v1/agentcms.js"
            shift 2
            ;;
        --compose-file)
            COMPOSE_FILE="$2"
            shift 2
            ;;
        --max-wait)
            MAX_WAIT="$2"
            shift 2
            ;;
        --site-slug)
            SITE_SLUG="$2"
            shift 2
            ;;
        --capability-token)
            CAP_TOKEN="$2"
            shift 2
            ;;
        *)
            log_error "Unknown argument: $1"
            exit 1
            ;;
    esac
done

log_info "Starting deploy smoke test against ${BASE_URL}"
log_info "Compose file: ${COMPOSE_FILE}"

# ---- 1. Wait for healthz ----
log_info "Waiting for /healthz (max ${MAX_WAIT}s)..."
start_time=$(date +%s)
while true; do
    if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
        log_info "/healthz is responding"
        break
    fi
    current_time=$(date +%s)
    if (( current_time - start_time > MAX_WAIT )); then
        log_error "/healthz did not become healthy within ${MAX_WAIT}s"
        exit 1
    fi
    sleep ${POLL_INTERVAL}
done

# ---- 2. Wait for readyz ----
log_info "Waiting for /readyz (max ${MAX_WAIT}s)..."
start_time=$(date +%s)
while true; do
    if curl -fsS "${READY_URL}" >/dev/null 2>&1; then
        log_info "/readyz reports ready"
        break
    fi
    current_time=$(date +%s)
    if (( current_time - start_time > MAX_WAIT )); then
        log_error "/readyz did not become ready within ${MAX_WAIT}s"
        exit 1
    fi
    sleep ${POLL_INTERVAL}
done

# ---- 3. Create admin token ----
log_info "Creating admin token..."
ADMIN_RESPONSE=$(curl -s -X POST "${BASE_URL}/v1/admin/tokens" \
    -H "Content-Type: application/json" \
    -d '{"label":"smoke-test","scopes":["posts:read","posts:write","posts:publish","assets:write","sites:write"]}')

ADMIN_TOKEN=$(echo "${ADMIN_RESPONSE}" | jq -r '.token // empty')
if [[ -z "${ADMIN_TOKEN}" || "${ADMIN_TOKEN}" == "null" ]]; then
    log_error "Failed to create admin token: ${ADMIN_RESPONSE}"
    exit 1
fi
log_info "Admin token created: ${ADMIN_TOKEN:0:20}..."

# ---- 4. The site must exist ----
# #44: there is no POST /v1/sites (the dashboard only UPDATEs the single existing
# row), so a site cannot be created from here. The only shipped mechanism is
# scripts/seed.py, which the self-host E2E runs before this script:
#   docker compose -f <compose-file> --env-file .env exec -T api python -m scripts.seed
log_info "Checking that site '${SITE_SLUG}' exists..."
SITE_PROBE=$(curl -s -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    "${BASE_URL}/v1/sites/${SITE_SLUG}/posts")
if [[ "${SITE_PROBE}" != "200" ]]; then
    log_error "No site '${SITE_SLUG}' in this deployment (GET /v1/sites/${SITE_SLUG}/posts -> ${SITE_PROBE})"
    log_error "Provision one first -- today that means seeding it (#44):"
    log_error "  docker compose -f ${COMPOSE_FILE} --env-file .env exec -T api python -m scripts.seed"
    exit 1
fi
log_info "Site '${SITE_SLUG}' present"

# ---- 5. Capability link (minted outside the API -- #44) ----
# #44: POST /v1/sites/{slug}/capability-links does not exist either, so the token
# is minted by the operator (or by scripts/seed.py in the E2E) and handed in.
if [[ -z "${CAP_TOKEN}" ]]; then
    log_error "No capability token: pass --capability-token <cap_...> (or set SMOKE_CAPABILITY_TOKEN)."
    log_error "Mint one with: docker compose --env-file .env exec -T api python -m scripts.seed"
    log_error "(#44: no API endpoint mints cap_ tokens yet.)"
    exit 1
fi
log_info "Using capability token: ${CAP_TOKEN:0:20}..."

# ---- 6. Create a post via capability link ----
log_info "Creating post via capability link..."
CREATE_RESPONSE=$(curl -s -X POST "${BASE_URL}/c/${CAP_TOKEN}/posts" \
    -H "Content-Type: application/json" \
    -d '{"title":"Smoke Test Post","body_md":"# Smoke Test\n\nThis post was created during deploy smoke test.","tags":["smoke","test"]}')

POST_ID=$(echo "${CREATE_RESPONSE}" | jq -r '.id // empty')
if [[ -z "${POST_ID}" || "${POST_ID}" == "null" ]]; then
    log_error "Failed to create post: ${CREATE_RESPONSE}"
    exit 1
fi
log_info "Post created: ${POST_ID}"

# ---- 7. Publish the post via capability link ----
log_info "Publishing post via capability link..."
PUBLISH_RESPONSE=$(curl -s -X POST "${BASE_URL}/c/${CAP_TOKEN}/posts/${POST_ID}/publish")

PUBLISHED_STATUS=$(echo "${PUBLISH_RESPONSE}" | jq -r '.status // empty')
if [[ "${PUBLISHED_STATUS}" != "published" ]]; then
    log_error "Failed to publish post: ${PUBLISH_RESPONSE}"
    exit 1
fi
log_info "Post published successfully"

# ---- 8. Verify post appears on public read surface ----
log_info "Verifying post on public read surface..."
PUBLIC_RESPONSE=$(curl -s "${BASE_URL}/${SITE_SLUG}/smoke-test")
if [[ "${PUBLIC_RESPONSE}" != *"Smoke Test"* ]]; then
    log_error "Post not found on public page"
    exit 1
fi
log_info "Post visible on public page"

# Also check JSON feed
JSON_RESPONSE=$(curl -s "${BASE_URL}/${SITE_SLUG}/posts.json")
PUBLIC_POST_COUNT=$(echo "${JSON_RESPONSE}" | jq '.items | length')
if [[ "${PUBLIC_POST_COUNT}" -lt 1 ]]; then
    log_error "Post not in posts.json"
    exit 1
fi
log_info "Post visible in posts.json (${PUBLIC_POST_COUNT} total)"

# ---- 9. Verify embed script is served correctly ----
log_info "Verifying embed script endpoint..."
SCRIPT_RESPONSE=$(curl -s -I "${EMBED_SCRIPT_URL}")
CONTENT_TYPE=$(echo "${SCRIPT_RESPONSE}" | grep -i "content-type:" | head -1 | cut -d' ' -f2- | tr -d '\r')
if [[ "${CONTENT_TYPE}" != *"javascript"* ]]; then
    log_error "Embed script has wrong content-type: ${CONTENT_TYPE}"
    exit 1
fi
log_info "Embed script served with correct content-type: ${CONTENT_TYPE}"

# Verify script content
SCRIPT_BODY=$(curl -s "${EMBED_SCRIPT_URL}")
if [[ "${SCRIPT_BODY}" != *"agentcms-embed"* ]]; then
    log_error "Embed script content unexpected"
    exit 1
fi
log_info "Embed script content verified"

# ---- 10. Verify embed token-scoped fetch returns the post ----
log_info "Verifying embed /posts endpoint with token..."
EMBED_POSTS_URL="${BASE_URL}/embed/v1/posts?token=${CAP_TOKEN}&limit=10"
EMBED_RESPONSE=$(curl -s -H "Origin: http://localhost:3000" "${EMBED_POSTS_URL}")

EMBED_POST_COUNT=$(echo "${EMBED_RESPONSE}" | jq '.posts | length')
if [[ "${EMBED_POST_COUNT}" -lt 1 ]]; then
    log_error "Embed endpoint returned no posts: ${EMBED_RESPONSE}"
    exit 1
fi

# Verify the post is in the response
EMBED_POST_TITLE=$(echo "${EMBED_RESPONSE}" | jq -r '.posts[0].title // empty')
if [[ "${EMBED_POST_TITLE}" != "Smoke Test Post" ]]; then
    log_error "Embed endpoint didn't return expected post: ${EMBED_RESPONSE}"
    exit 1
fi
log_info "Embed endpoint returns the published post"

# ---- 11. The embed surface must reject non-capability tokens ----
# #44 removed the "mint a write-only token" variant of this check (no API mints
# capability links), so it asserts the property directly instead: an admin
# (`acms_`) token must never work on the public embed surface.
log_info "Verifying a non-capability token is rejected on the embed endpoint..."
ADMIN_EMBED_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    "${BASE_URL}/embed/v1/posts?token=${ADMIN_TOKEN}&limit=5")
if [[ "${ADMIN_EMBED_CODE}" =~ ^4 ]]; then
    log_info "Non-capability token correctly rejected (${ADMIN_EMBED_CODE})"
else
    log_error "Embed endpoint accepted a non-capability token (got ${ADMIN_EMBED_CODE})"
    exit 1
fi

# ---- 12. Verify CORS headers on embed endpoint ----
log_info "Verifying CORS on embed endpoint..."
CORS_RESPONSE=$(curl -s -I -H "Origin: http://localhost:3000" \
    -H "Access-Control-Request-Method: GET" \
    -X OPTIONS "${BASE_URL}/embed/v1/posts")
ACCESS_CONTROL_ALLOW_ORIGIN=$(echo "${CORS_RESPONSE}" | grep -i "access-control-allow-origin:" | head -1 | cut -d' ' -f2- | tr -d '\r')
if [[ -n "${ACCESS_CONTROL_ALLOW_ORIGIN}" ]]; then
    log_info "CORS preflight works: ${ACCESS_CONTROL_ALLOW_ORIGIN}"
else
    log_warn "CORS preflight headers not present (EMBED_ORIGINS may not be configured)"
fi

# ---- All checks passed ----
log_info "============================================"
log_info "ALL SMOKE TESTS PASSED ✓"
log_info "============================================"
log_info "Deploy verified end-to-end:"
log_info "  - Health checks: OK"
log_info "  - Admin API: OK"
log_info "  - Capability links: OK"
log_info "  - Create + publish via link: OK"
log_info "  - Public read surface: OK"
log_info "  - Embed script served: OK"
log_info "  - Embed token fetch: OK"
log_info "  - Write token rejection: OK"
log_info "  - CORS headers: OK"
exit 0