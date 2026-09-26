#!/usr/bin/env bash
# selfhost_e2e.sh — prove the documented self-host path works, from nothing (#37).
#
#   ./scripts/selfhost_e2e.sh                 deploy .env -> stack -> migrate ->
#                                             healthz/readyz -> seed a site ->
#                                             API roundtrip -> embed CORS at the
#                                             Caddy edge, both ways -> down -v
#   ./scripts/selfhost_e2e.sh --keep          leave the stack running for poking at
#   ./scripts/selfhost_e2e.sh --reuse-env     run against an existing .env (repeat runs)
#   ./scripts/selfhost_e2e.sh --timeout 300   raise the per-assertion wait budget (s)
#   ./scripts/selfhost_e2e.sh --no-build      boot an already-pulled image instead
#   ./scripts/selfhost_e2e.sh --image-tag ghcr.io/owner/app@sha256:...   pin an image
#
# This is the executable form of docs/deploy/quickstart.md: it runs the *same*
# commands, in the same order, from a checkout with no .env and no image cache,
# and it fails (never skips) when the Docker daemon is unreachable.  A skipped
# deploy check is what let #35/#36 ship; the exit code here is the verdict.
#
# Exit codes: 0 verified, 1 broken stack / missing tooling / dirty checkout.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="deploy/compose/docker-compose.prod.yml"
ENV_FILE=".env"
BASE_URL="${SELFHOST_E2E_BASE_URL:-http://127.0.0.1:8000}"
TIMEOUT="${SELFHOST_E2E_TIMEOUT:-180}"
IMAGE_TAG=""
NO_BUILD=0
KEEP=0
REUSE_ENV=0
CREATED_ENV=0
STACK_UP=0

log()  { printf '\033[0;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[0;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --project-dir)  PROJECT_DIR="${2:?--project-dir needs a path}"; shift 2 ;;
    --project-dir=*) PROJECT_DIR="${1#*=}"; shift ;;
    --base-url)     BASE_URL="${2:?--base-url needs a value}"; shift 2 ;;
    --base-url=*)   BASE_URL="${1#*=}"; shift ;;
    --timeout)      TIMEOUT="${2:?--timeout needs seconds}"; shift 2 ;;
    --timeout=*)    TIMEOUT="${1#*=}"; shift ;;
    --image-tag)    IMAGE_TAG="${2:?--image-tag needs a reference}"; shift 2 ;;
    --image-tag=*)  IMAGE_TAG="${1#*=}"; shift ;;
    --no-build)     NO_BUILD=1; shift ;;
    --keep)         KEEP=1; shift ;;
    --reuse-env)    REUSE_ENV=1; shift ;;
    -h|--help)      usage 0 ;;
    *)              die "unknown option: $1 (try --help)" ;;
  esac
done

cd "$PROJECT_DIR"

compose() { docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"; }

dump_logs() {
  printf '\n--- docker compose logs (tail 200) ---\n' >&2
  compose logs --tail=200 >&2 2>&1 || true
  printf '\n--- docker compose ps ---\n' >&2
  compose ps >&2 2>&1 || true
}

cleanup() {
  rc=$?
  if [ "$rc" -ne 0 ]; then
    printf '\033[0;31m==> SELF-HOST E2E FAILED (exit %s)\033[0m\n' "$rc" >&2
    dump_logs
  fi
  if [ "$STACK_UP" -eq 1 ]; then
    if [ "$KEEP" -eq 1 ]; then
      warn "--keep: leaving the stack up (tear it down with: docker compose -f $COMPOSE_FILE --env-file $ENV_FILE down -v)"
    else
      log "tearing the stack down (down -v)"
      compose down -v --remove-orphans >/dev/null 2>&1 || true
    fi
  fi
  if [ "$CREATED_ENV" -eq 1 ] && [ "$KEEP" -eq 0 ]; then
    rm -f "$ENV_FILE"
  fi
  exit "$rc"
}
trap cleanup EXIT

# --- 0. preconditions: tooling, daemon, clean checkout ----------------------

for bin in docker curl jq; do
  command -v "$bin" >/dev/null 2>&1 \
    || die "$bin is not installed — the self-host check cannot run, so this is a FAILURE, not a skip"
done

docker info >/dev/null 2>&1 \
  || die "the Docker daemon is not reachable. Start Docker and re-run — a skipped deploy check is what hid #35/#36, so this exits 1 on purpose."

if [ -f "$ENV_FILE" ] && [ "$REUSE_ENV" -eq 0 ]; then
  die "$PROJECT_DIR/$ENV_FILE already exists: this is not a clean checkout. Remove it, or pass --reuse-env to test an existing environment."
fi
if [ ! -f "$ENV_FILE" ]; then
  CREATED_ENV=1
fi

log "self-host E2E in $PROJECT_DIR (compose: $COMPOSE_FILE, base url: $BASE_URL)"

# --- 1. generate .env exactly the way the quickstart does ------------------

if [ -n "$IMAGE_TAG" ]; then
  # A digest (or a version tag) pinned by the caller, e.g. the image the release
  # workflow just published — the app refuses to boot in production without one.
  ./scripts/selfhost.sh --setup-only --env-file "$ENV_FILE" --tag "$IMAGE_TAG" \
    || die "scripts/selfhost.sh --setup-only failed to generate $ENV_FILE"
else
  ./scripts/selfhost.sh --setup-only --env-file "$ENV_FILE" \
    || die "scripts/selfhost.sh --setup-only failed to generate $ENV_FILE"
fi

compose config >/dev/null || die "docker compose rejected $ENV_FILE"

# --- 2. up -d --build (the documented command, no shortcuts) ---------------

STACK_UP=1  # from here on, always try to tear down
if [ "$NO_BUILD" -eq 1 ]; then
  log "docker compose -f $COMPOSE_FILE --env-file $ENV_FILE up -d (using the pinned image)"
  compose up -d || die "docker compose up failed"
else
  log "docker compose -f $COMPOSE_FILE --env-file $ENV_FILE up -d --build"
  compose up -d --build || die "docker compose up failed"
fi

# --- 3. the migrate service must run, succeed, and be re-runnable ----------

migrate_cid="$(compose ps -a -q migrate || true)"
[ -n "$migrate_cid" ] || die "the migrate service never started"

deadline=$(( $(date +%s) + TIMEOUT ))
while [ "$(docker inspect -f '{{.State.Status}}' "$migrate_cid")" != "exited" ]; do
  [ "$(date +%s)" -lt "$deadline" ] || die "the migrate service did not finish within ${TIMEOUT}s"
  sleep 2
done
migrate_rc="$(docker inspect -f '{{.State.ExitCode}}' "$migrate_cid")"
[ "$migrate_rc" = "0" ] || die "the migrate service exited $migrate_rc (migrations did not reach head)"
log "migrate exited 0 (alembic upgrade head)"

log "re-running the migrate service (migrations must be re-runnable)"
compose run --rm migrate || die "re-running the migrate service failed — upgrades are not idempotent"

# --- 4. assert /healthz and /readyz are 200 -------------------------------

wait_for_http() { # url label
  local url="$1" label="$2" code="" body="" deadline=$(( $(date +%s) + TIMEOUT ))
  while :; do
    code="$(curl -s -o /tmp/code_body.$$ -w '%{http_code}' "$url" || true)"
    body="$(cat /tmp/code_body.$$ 2>/dev/null || true)"
    [ "$code" = "200" ] && { log "$label -> 200 $(printf '%s' "$body" | head -c 200)"; rm -f /tmp/code_body.$$; return 0; }
    if [ "$(date +%s)" -ge "$deadline" ]; then
      rm -f /tmp/code_body.$$
      die "$label returned ${code:-no response} after ${TIMEOUT}s (body: $(printf '%s' "$body" | head -c 300))"
    fi
    sleep 3
  done
}

wait_for_http "$BASE_URL/healthz" "GET /healthz"
wait_for_http "$BASE_URL/readyz" "GET /readyz"
# The human dashboard is a first-class surface (#42 404'd in every deploy);
# a self-host E2E that never loads it is not proving a deploy works.
wait_for_http "$BASE_URL/dashboard/login" "GET /dashboard/login"

# --- 5. no crash loop: restarts must be 0, caddy must be up ---------------

api_cid="$(compose ps -q api)"
[ -n "$api_cid" ] || die "no api container is running"
restarts="$(docker inspect -f '{{.RestartCount}}' "$api_cid")"
[ "$restarts" = "0" ] || die "the api container restarted ${restarts}x — the production config guard is rejecting the generated .env"
api_state="$(docker inspect -f '{{.State.Status}}' "$api_cid")"
[ "$api_state" = "running" ] || die "the api container is $api_state, not running"
log "api: running, 0 restarts"

caddy_cid="$(compose ps -q caddy || true)"
[ -n "$caddy_cid" ] || die "no caddy container is running (the HTTPS front door of the documented path)"
caddy_state="$(docker inspect -f '{{.State.Status}}' "$caddy_cid")"
[ "$caddy_state" = "running" ] || die "the caddy container is $caddy_state, not running"
log "caddy: running"

# --- 6. API roundtrip: create site -> publish post -> fetch public URL -----

# --- 6. provision the demo site through the documented API (#45) --------------
#
# POST /v1/sites is the documented first-run path ("Option B: Via the API",
# docs/deploy/quickstart.md) and the route exists as of #45.  This step used to
# say the API could not create a site and seeded instead -- which meant nothing in
# CI ever called POST /v1/sites, so a regression there shipped green.  The site is
# now created the way a self-hoster is told to create it: mint an acms_ token
# through the admin surface, POST /v1/sites, read the slug out of the response,
# and read the site back before anything is published into it.
#
# `python -m scripts.seed` stays for the one thing the API genuinely cannot do:
# mint the cap_/embed tokens.  No capability-mint route exists -- app.openapi()
# has no /v1/sites/{slug}/capability-links (docs/deploy/embed.md says so;
# docs/deploy/quickstart.md used to hand operators a route that was never served).
site_slug="${SELFHOST_E2E_SITE_SLUG:-blog}"

# #44: the whole /v1/admin/* surface needs the ADMIN_TOKEN bootstrap secret that
# scripts/selfhost.sh wrote into .env.  A curl from this host to the published port
# is NOT a loopback peer (docker forwards it from the bridge gateway), so the smoke
# script has to send it explicitly -- read it from .env the way an operator would.
bootstrap_admin_token="$(sed -n 's/^ADMIN_TOKEN=//p' "${ENV_FILE}" | tail -1 | tr -d '\r"')"
[ -n "$bootstrap_admin_token" ] || die "scripts/selfhost.sh wrote no ADMIN_TOKEN into ${ENV_FILE}: the admin surface (token mint, audit) cannot be exercised (#44)"
export SMOKE_ADMIN_TOKEN="$bootstrap_admin_token"

log "creating the demo site through the documented API: POST /v1/sites"
# The sites API authenticates with an acms_ bearer token, not with the bootstrap
# secret (app/api/v1/sites.py -> require_auth), so mint one first -- the exact
# sequence the quickstart hands an operator.
site_admin_token="$(curl -sS -X POST "${BASE_URL}/v1/admin/tokens" \
  -H "X-Admin-Token: ${bootstrap_admin_token}" \
  -H "Content-Type: application/json" \
  -d '{"label":"e2e-site-provision","scopes":["sites:write","sites:read"]}' \
  | jq -r '.token // empty')"
[ -n "$site_admin_token" ] || die "POST /v1/admin/tokens minted no sites:write token, so POST /v1/sites cannot be exercised"

create_response="$(curl -sS -w '\n%{http_code}' -X POST "${BASE_URL}/v1/sites" \
  -H "Authorization: Bearer ${site_admin_token}" \
  -H "Content-Type: application/json" \
  -d "{\"slug\":\"${site_slug}\",\"name\":\"Smoke Test Blog\"}")"
create_status="$(printf '%s\n' "$create_response" | tail -1)"
create_body="$(printf '%s\n' "$create_response" | sed '$d')"
# 409 = the site already exists (a --reuse-env run against a warm stack); the
# read-back below still proves the row is there.  Anything else non-2xx is the
# documented first-run path being broken, and this job must not pass.
case "$create_status" in
  201|200)
    site_slug="$(printf '%s' "$create_body" | jq -r '.slug // empty')"
    ;;
  409) ;;
  *) die "POST /v1/sites -> ${create_status}: the documented way to create a site is broken (body: ${create_body})" ;;
esac
[ -n "$site_slug" ] || die "POST /v1/sites did not return a slug, so the roundtrip cannot be aimed at the created site"

readback_body="$(curl -sS -H "Authorization: Bearer ${site_admin_token}" "${BASE_URL}/v1/sites/${site_slug}")"
readback_slug="$(printf '%s' "$readback_body" | jq -r '.slug // empty')"
[ "$readback_slug" = "$site_slug" ] || die "GET /v1/sites/${site_slug} returned slug '${readback_slug}' right after the site was created: the provisioning is not readable back (body: ${readback_body})"
log "created ${site_slug} via POST /v1/sites and read it back: GET /v1/sites/${site_slug} -> 200"

# --- 6a. the tokens the API cannot mint: `python -m scripts.seed` --------------
#
# `make seed` ships in the image and is idempotent: it finds the site created
# above by slug and reuses it.  It is the only shipped mint for the cap_/embed
# tokens, and it stays here for that alone.
log "minting the capability/embed tokens: python -m scripts.seed (the documented \`make seed\` target)"
if ! seed_out="$(compose exec -T api python -m scripts.seed)"; then
  die "python -m scripts.seed failed while minting the capability/embed tokens"
fi
printf '%s\n' "$seed_out" | sed 's/^/    /'
seeded_slug="$(printf '%s\n' "$seed_out" | sed -n 's|.*Demo site: */v1/sites/\([A-Za-z0-9_-]*\).*|\1|p' | head -1)"
[ "$seeded_slug" = "$site_slug" ] || die "POST /v1/sites created '${site_slug}' but scripts/seed.py reports '${seeded_slug}': the documented API and the seeder disagree about the demo site"
cap_token="$(printf '%s\n' "$seed_out" | tr -d '\r' | grep -oE 'cap_[A-Za-z0-9_-]+' | head -1)"
[ -n "$cap_token" ] || die "scripts/seed.py did not print a capability token (see #44)"

embed_token="$(printf '%s\n' "$seed_out" | tr -d '\r' | sed -n 's|.*Embed token (read-only): *\(cap_[A-Za-z0-9_-]*\).*|\1|p' | head -1)"
[ -n "$embed_token" ] || die "could not read the read-only embed token from scripts/seed.py output (the embed surface rejects write tokens)"
# The embed surface takes a read-only token; export it so the smoke script uses it
# for /embed/v1/posts and asserts a write token is refused.
export SMOKE_EMBED_TOKEN="$embed_token"

# --- 6b. #44 regression guard: /v1/admin/* refuses an anonymous caller --------
#
# deploy_smoke.sh asserts this too, but this job is the one that boots the
# *published image*, so it has to fail on its own if the router-level guard ever
# goes missing again.  The probe is paired with a positive control because
# app/api/admin_auth.py exempts a loopback peer: a 2xx below is always fatal, and
# the message names that exemption so a vantage change is loud, never silently green.
log "asserting /v1/admin/* refuses an anonymous caller (#44)"
anon_post_code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "${BASE_URL}/v1/admin/tokens" \
  -H "Content-Type: application/json" \
  -d '{"label":"anonymous-probe","scopes":["posts:read"]}' || true)"
anon_get_code="$(curl -s -o /dev/null -w '%{http_code}' "${BASE_URL}/v1/admin/tokens" || true)"
if [ "$anon_post_code" != "401" ] || [ "$anon_get_code" != "401" ]; then
  die "#44 REGRESSION: anonymous POST /v1/admin/tokens -> ${anon_post_code}, GET -> ${anon_get_code}; both must be 401 (this path mints and lists tokens). If this vantage is loopback-exempt -- app/api/admin_auth.py trusts a loopback peer -- the refusal assertion is invalid here, not absent: probe from a non-loopback peer"
fi
mint_code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "${BASE_URL}/v1/admin/tokens" \
  -H "X-Admin-Token: ${bootstrap_admin_token}" \
  -H "Content-Type: application/json" \
  -d '{"label":"e2e-positive-control","scopes":["posts:read"]}' || true)"
[ "$mint_code" = "201" ] || die "#44 POSITIVE CONTROL FAILED: the correct X-Admin-Token got ${mint_code}, want 201 -- a stack that refuses the right secret cannot vouch for the anonymous-refusal assertion above"
log "admin surface: anonymous POST/GET refused (401/401), authenticated mint 201 (positive control)"

# --- 7. API roundtrip: capability link -> publish post -> public page ---------

log "API roundtrip (minted capability link -> publish post -> public page)"
# deploy_smoke.sh asserts CORS in both directions, so hand it the allowlist this
# stack was actually deployed with: deploy/.env.example ships `EMBED_ORIGINS=`
# (deny-all), and an unconfigured origin then gets no ACAO header back.
if [[ -f "${ENV_FILE}" ]]; then
  SMOKE_EMBED_ORIGINS="$(sed -n 's/^EMBED_ORIGINS=//p' "${ENV_FILE}" | tail -1 | tr -d '\r"' || true)"
  export SMOKE_EMBED_ORIGINS
fi
./scripts/deploy_smoke.sh --base-url "$BASE_URL" --compose-file "$COMPOSE_FILE" --max-wait "$TIMEOUT" \
  --site-slug "$site_slug" --capability-token "$cap_token" \
  || die "the API roundtrip failed (see [SMOKE] output above)"

# --- 8. the Caddy edge: embed CORS must equal the operator's allowlist (#48) --
#
# Every assertion above talks to the API's published port, so none of them can
# see the edge. The open-embed hole this ticket closed was *at* the edge: it
# answered every preflight with the caller's Origin plus credentials, so the
# smoke assertion in step 7 was unsatisfiable on the documented default (a
# correct, deny-all deploy looked broken). Probing through Caddy is the only way
# CI can see the matcher that was fixed. DOMAIN=localhost means Caddy's own
# internal CA, hence -k.
EDGE_BASE="${SELFHOST_E2E_EDGE_URL:-https://localhost}"
EMBED_PROBE_ORIGIN="http://localhost:3000"
UNLISTED_PROBE_ORIGIN="https://not-allowlisted.example"

wait_for_edge() { # label
  local label="$1" deadline=$(( $(date +%s) + TIMEOUT ))
  while :; do
    if curl -skf -o /dev/null "${EDGE_BASE}/healthz"; then
      log "$label -> 200 through ${EDGE_BASE}"
      return 0
    fi
    [ "$(date +%s)" -lt "$deadline" ] || die "${EDGE_BASE} never served /healthz after ${TIMEOUT}s"
    sleep 3
  done
}

edge_cors_probe() { # origin allow|deny label
  local origin="$1" verdict="$2" label="$3" headers=""
  headers="$(curl -sk -D - -o /dev/null -X OPTIONS \
      -H "Origin: ${origin}" \
      -H 'Access-Control-Request-Method: GET' \
      "${EDGE_BASE}/embed/v1/posts")" \
    || die "the Caddy edge answered nothing for a ${origin} preflight on ${EDGE_BASE}"
  local acao
  acao="$(printf '%s\n' "$headers" \
    | { grep -i '^access-control-allow-origin:' || true; } \
    | head -1 | cut -d' ' -f2- | tr -d '\r')"
  case "$verdict" in
    allow)
      [ "$acao" = "$origin" ] \
        || die "the edge must allow the configured origin ${origin}, but returned '${acao:-no access-control-allow-origin}' — check EMBED_ORIGINS_REGEX"
      ;;
    deny)
      [ -z "$acao" ] \
        || die "the edge advertised 'Access-Control-Allow-Origin: ${acao}' for the unlisted origin ${origin} — an open-embed hole (#48)"
      ;;
  esac
  log "edge CORS: ${label} (ACAO: ${acao:-none})"
}

wait_for_edge "GET /healthz through Caddy"
log "probing embed CORS at the edge as deployed (EMBED_ORIGINS empty = deny-all)"
edge_cors_probe "$EMBED_PROBE_ORIGIN" deny "denied an unlisted origin"
edge_cors_probe "$UNLISTED_PROBE_ORIGIN" deny "denied an unlisted origin"

# Now the positive direction, through the edge, with the value selfhost.sh
# derives: re-running --setup-only is the same edit an operator makes when they
# set EMBED_ORIGINS, and it is the only thing that recomputes the pattern.
log "re-deploying with EMBED_ORIGINS=${EMBED_PROBE_ORIGIN} to prove the allowlist is honoured at the edge"
sed -i.bak "s|^EMBED_ORIGINS=.*$|EMBED_ORIGINS=${EMBED_PROBE_ORIGIN}|" "$ENV_FILE" \
  || die "could not set EMBED_ORIGINS in ${ENV_FILE}"
rm -f "${ENV_FILE}.bak"
./scripts/selfhost.sh --setup-only --env-file "$ENV_FILE" \
  || die "scripts/selfhost.sh --setup-only failed to re-derive the edge allowlist"
grep -q '^EMBED_ORIGINS_REGEX=http://localhost:3000$' "$ENV_FILE" \
  || die "selfhost.sh did not derive EMBED_ORIGINS_REGEX for ${EMBED_PROBE_ORIGIN}: $(sed -n 's/^EMBED_ORIGINS_REGEX=/…/p' "$ENV_FILE" | tail -1)"
compose up -d || die "docker compose up failed after EMBED_ORIGINS changed"
wait_for_http "$BASE_URL/healthz" "GET /healthz (allowlisted deploy)"
wait_for_edge "GET /healthz through Caddy (allowlisted deploy)"
edge_cors_probe "$EMBED_PROBE_ORIGIN" allow "allowed the configured origin"
edge_cors_probe "$UNLISTED_PROBE_ORIGIN" deny "still denied an unlisted origin"

log "SELF-HOST E2E PASSED — the documented path deploys and serves content"
