#!/usr/bin/env bash
# deploy_smoke.sh — End-to-end deploy verification (#31).
#
# Runs after `docker compose -f deploy/compose/docker-compose.prod.yml up -d`:
# 1. Waits for /healthz and /readyz to report healthy
# 2. Mints an admin token with the ADMIN_TOKEN bootstrap secret (#44: POST
#    /v1/admin/tokens is no longer anonymous, so --admin-token / SMOKE_ADMIN_TOKEN
#    is required -- there is no fallback)
# 3. Asserts the site named by --site-slug exists (#44: nothing can create one
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
ADMIN_BOOTSTRAP_TOKEN="${SMOKE_ADMIN_TOKEN:-}"   # the ADMIN_TOKEN bootstrap secret (#44); no anonymous fallback
CAP_TOKEN="${SMOKE_CAPABILITY_TOKEN:-}"
EMBED_TOKEN="${SMOKE_EMBED_TOKEN:-}"   # read-only (posts:read) token; the embed surface refuses write tokens
EMBED_ORIGINS="${SMOKE_EMBED_ORIGINS:-}"   # the allowlist this stack was deployed with (empty = deny-all)
CORS_PROBE_ORIGIN="${SMOKE_CORS_PROBE_ORIGIN:-http://localhost:3000}"
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
        --admin-token)
            ADMIN_BOOTSTRAP_TOKEN="$2"
            shift 2
            ;;
        --capability-token)
            CAP_TOKEN="$2"
            shift 2
            ;;
        --embed-token)
            EMBED_TOKEN="$2"
            shift 2
            ;;
        --embed-origins)
            EMBED_ORIGINS="$2"
            shift 2
            ;;
        --cors-probe-origin)
            CORS_PROBE_ORIGIN="$2"
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
# #44: POST /v1/admin/tokens is authenticated by the ADMIN_TOKEN bootstrap secret.
# Fail loudly rather than probing the endpoint unauthenticated: a 401 here means
# "you forgot the secret", not "the product is broken".
if [[ -z "${ADMIN_BOOTSTRAP_TOKEN}" ]]; then
    log_error "No admin bootstrap secret: pass --admin-token or set SMOKE_ADMIN_TOKEN"
    log_error "(it is the ADMIN_TOKEN written into .env by scripts/selfhost.sh; #44 made it mandatory)"
    exit 1
fi
log_info "Creating admin token (authenticated with X-Admin-Token)..."
ADMIN_RESPONSE=$(curl -s -X POST "${BASE_URL}/v1/admin/tokens" \
    -H "X-Admin-Token: ${ADMIN_BOOTSTRAP_TOKEN}" \
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
# The public URL is /{site}/{post-slug}, and the API derives the slug from the title
# ("Smoke Test Post" -> "smoke-test-post"). Never hardcode it: this step used to fetch
# /{site}/smoke-test, 404 on every run, and that is exactly what turned the self-host
# E2E job red. Read the real slug back from the public JSON feed, then fetch the HTML.
log_info "Verifying post on public read surface..."
JSON_RESPONSE=$(curl -s "${BASE_URL}/${SITE_SLUG}/posts.json")
POST_SLUG=$(echo "${JSON_RESPONSE}" | jq -r '.items[] | select(.title == "Smoke Test Post") | .slug' | head -1)
if [[ -z "${POST_SLUG}" || "${POST_SLUG}" == "null" ]]; then
    log_error "Just-published post is missing from ${BASE_URL}/${SITE_SLUG}/posts.json"
    log_error "Feed: ${JSON_RESPONSE}"
    exit 1
fi
log_info "Public slug read back from feed: ${POST_SLUG}"
PUBLIC_RESPONSE=$(curl -s "${BASE_URL}/${SITE_SLUG}/${POST_SLUG}")
if [[ "${PUBLIC_RESPONSE}" != *"Smoke Test"* ]]; then
    log_error "Post not on public page ${BASE_URL}/${SITE_SLUG}/${POST_SLUG}"
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
# A browser fetches this asset with GET, so assert it with GET.  `curl -I` sends
# HEAD, and FastAPI's @router.get does not register HEAD -- the documented surface
# answers 405 and the assertion then reads the *error* body's content-type
# (application/problem+json) as its verdict.  That is how the first CI run of the
# self-host E2E job failed, so the status code is asserted separately.
log_info "Verifying embed script endpoint..."
SCRIPT_HEADERS=$(curl -s -D - -o /dev/null "${EMBED_SCRIPT_URL}")
SCRIPT_CODE=$(printf '%s' "${SCRIPT_HEADERS}" | head -1 | awk '{print $2}')
if [[ "${SCRIPT_CODE}" != "200" ]]; then
    log_error "Embed script endpoint returned HTTP ${SCRIPT_CODE} (expected 200)"
    printf '%s\n' "${SCRIPT_HEADERS}" >&2
    exit 1
fi
CONTENT_TYPE=$(printf '%s' "${SCRIPT_HEADERS}" | { grep -i "^content-type:" || true; } | head -1 | cut -d' ' -f2- | tr -d '\r')
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
# The embed surface only accepts read-only tokens, so a write-capable capability
# token must be refused (403) -- assert that first, then read the feed with the
# read-only token `make seed` prints.  Asserting with the write token is how the
# first CI run of the self-host E2E job got a 403 dressed up as "no posts".
log_info "Verifying the write-capable token is refused on the embed surface..."
WRITE_EMBED_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${BASE_URL}/embed/v1/posts?token=${CAP_TOKEN}&limit=5")
if [[ "${WRITE_EMBED_CODE}" != "403" ]]; then
    log_error "Embed surface accepted a write token (got ${WRITE_EMBED_CODE}, expected 403)"
    exit 1
fi
log_info "Write token refused on the embed surface (403)"

if [[ -z "${EMBED_TOKEN}" ]]; then
    log_error "No read-only embed token: set SMOKE_EMBED_TOKEN (make seed prints one)"
    exit 1
fi
log_info "Verifying embed /posts endpoint with the read-only token..."
EMBED_POSTS_URL="${BASE_URL}/embed/v1/posts?token=${EMBED_TOKEN}&limit=10"
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
# Asserted in both directions, never warned away. The documented default is
# `EMBED_ORIGINS=` (deny-all): a preflight from an origin that was not
# configured must come back with *no* access-control-allow-origin, and when the
# stack was deployed with an allowlist that lists the probe origin, the header
# must echo it. A warn-only branch here is how this check died: the extraction
# read `$(echo ... | grep ... | head -1 | ...)` under `set -euo pipefail`, so
# with no ACAO header `grep` exited 1, pipefail failed the whole substitution
# and bash -e killed the run *before* its own log_warn -- reddening the self-host
# E2E job on a healthy stack (2026-09-25). A missing header is a result to
# assert on, not a shell error, so the grep is guarded.
log_info "Verifying CORS on embed endpoint (probe origin: ${CORS_PROBE_ORIGIN})..."
CORS_RESPONSE=$(curl -s -D - -o /dev/null -H "Origin: ${CORS_PROBE_ORIGIN}" \
    -H "Access-Control-Request-Method: GET" \
    -X OPTIONS "${BASE_URL}/embed/v1/posts")
ACCESS_CONTROL_ALLOW_ORIGIN=$(printf '%s\n' "${CORS_RESPONSE}" \
    | { grep -i "^access-control-allow-origin:" || true; } \
    | head -1 | cut -d' ' -f2- | tr -d '\r')

case ",${EMBED_ORIGINS}," in
    *",${CORS_PROBE_ORIGIN},"*)
        # The stack was deployed with an allowlist that includes this origin.
        if [[ "${ACCESS_CONTROL_ALLOW_ORIGIN}" != "${CORS_PROBE_ORIGIN}" ]]; then
            log_error "EMBED_ORIGINS lists ${CORS_PROBE_ORIGIN} but the preflight returned '${ACCESS_CONTROL_ALLOW_ORIGIN:-no access-control-allow-origin header}'"
            exit 1
        fi
        log_info "CORS preflight allowed the configured origin: ${ACCESS_CONTROL_ALLOW_ORIGIN}"
        ;;
    *)
        # Either deny-all (EMBED_ORIGINS unset) or an allowlist without this
        # origin: both must stay silent.  A header here is an open-embed hole.
        if [[ -n "${ACCESS_CONTROL_ALLOW_ORIGIN}" ]]; then
            log_error "CORS preflight allowed ${CORS_PROBE_ORIGIN}, which is not in EMBED_ORIGINS='${EMBED_ORIGINS}'"
            exit 1
        fi
        if [[ -z "${EMBED_ORIGINS}" ]]; then
            log_info "CORS deny-all confirmed (EMBED_ORIGINS unset): no access-control-allow-origin for ${CORS_PROBE_ORIGIN}"
        else
            log_info "CORS denied ${CORS_PROBE_ORIGIN} (not in EMBED_ORIGINS)"
        fi
        ;;
esac

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