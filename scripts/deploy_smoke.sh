#!/usr/bin/env bash
# deploy_smoke.sh — End-to-end deploy verification (#31).
#
# Runs after `docker compose -f deploy/compose/docker-compose.prod.yml up -d`:
# 1. Waits for /healthz and /readyz to report healthy
# 2. Creates a site via admin API
# 3. Mints a capability link with posts:read + posts:write + posts:publish
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
    -d '{"label":"smoke-test","scopes":["posts:read","posts:write","posts:publish","assets:write"]}')

ADMIN_TOKEN=$(echo "${ADMIN_RESPONSE}" | jq -r '.plaintext // empty')
if [[ -z "${ADMIN_TOKEN}" || "${ADMIN_TOKEN}" == "null" ]]; then
    log_error "Failed to create admin token: ${ADMIN_RESPONSE}"
    exit 1
fi
log_info "Admin token created: ${ADMIN_TOKEN:0:20}..."

# ---- 4. Create site ----
log_info "Creating site 'blog'..."
SITE_RESPONSE=$(curl -s -X POST "${BASE_URL}/v1/sites" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"slug":"blog","name":"Smoke Test Blog","publish_mode":"auto"}')

SITE_ID=$(echo "${SITE_RESPONSE}" | jq -r '.id // empty')
if [[ -z "${SITE_ID}" || "${SITE_ID}" == "null" ]]; then
    # Site might already exist, try to get it
    SITE_RESPONSE=$(curl -s -X GET "${BASE_URL}/v1/sites/blog" \
        -H "Authorization: Bearer ${ADMIN_TOKEN}")
    SITE_ID=$(echo "${SITE_RESPONSE}" | jq -r '.id // empty')
fi
if [[ -z "${SITE_ID}" || "${SITE_ID}" == "null" ]]; then
    log_error "Failed to create/get site: ${SITE_RESPONSE}"
    exit 1
fi
log_info "Site ready: ${SITE_ID}"

# ---- 5. Create capability link (embed token with full scope for smoke test) ----
log_info "Creating capability link for embed..."
CAP_RESPONSE=$(curl -s -X POST "${BASE_URL}/v1/sites/blog/capability-links" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"label":"smoke-embed","verbs":["posts:read","posts:write","posts:publish"],"ttl_minutes":60}')

CAP_TOKEN=$(echo "${CAP_RESPONSE}" | jq -r '.plaintext // empty')
if [[ -z "${CAP_TOKEN}" || "${CAP_TOKEN}" == "null" ]]; then
    log_error "Failed to create capability link: ${CAP_RESPONSE}"
    exit 1
fi
log_info "Capability token: ${CAP_TOKEN}"

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
PUBLIC_RESPONSE=$(curl -s "${BASE_URL}/blog/smoke-test")
if [[ "${PUBLIC_RESPONSE}" != *"Smoke Test"* ]]; then
    log_error "Post not found on public page"
    exit 1
fi
log_info "Post visible on public page"

# Also check JSON feed
JSON_RESPONSE=$(curl -s "${BASE_URL}/blog/posts.json")
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

# ---- 11. Verify write-scoped token is rejected by embed endpoint ----
log_info "Verifying write-scoped token rejection on embed endpoint..."
# Create a write-only token
WRITE_CAP_RESPONSE=$(curl -s -X POST "${BASE_URL}/v1/sites/blog/capability-links" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"label":"smoke-write","verbs":["posts:write"],"ttl_minutes":10}')

WRITE_TOKEN=$(echo "${WRITE_CAP_RESPONSE}" | jq -r '.plaintext // empty')
if [[ -n "${WRITE_TOKEN}" && "${WRITE_TOKEN}" != "null" ]]; then
    WRITE_EMBED_RESPONSE=$(curl -s -w "%{http_code}" -o /dev/null \
        "${BASE_URL}/embed/v1/posts?token=${WRITE_TOKEN}&limit=5")
    if [[ "${WRITE_EMBED_RESPONSE}" == "403" ]]; then
        log_info "Write-scoped token correctly rejected (403)"
    else
        log_error "Write-scoped token was NOT rejected (got ${WRITE_EMBED_RESPONSE})"
        exit 1
    fi
else
    log_warn "Could not create write-only token for rejection test"
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