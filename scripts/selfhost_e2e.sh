#!/usr/bin/env bash
# selfhost_e2e.sh — prove the documented self-host path works, from nothing (#37).
#
#   ./scripts/selfhost_e2e.sh                 deploy .env -> stack -> migrate ->
#                                             healthz/readyz -> seed a site ->
#                                             API roundtrip -> down -v
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

for bin in docker curl; do
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

# --- 6. provision a site: the only shipped mechanism is the seed script (#44) ---
# The API exposes no POST /v1/sites and the dashboard only UPDATEs the single
# existing row, so a fresh deployment cannot create a site over HTTP yet (#44).
# `python -m scripts.seed` (the documented `make seed`) ships in the image and is
# idempotent; without it there is no site to publish into and the roundtrip below
# would fail against a perfectly healthy stack.
log "provisioning the demo site: python -m scripts.seed (the documented \`make seed\` target)"
if ! seed_out="$(compose exec -T api python -m scripts.seed)"; then
  die "python -m scripts.seed failed: no API can create a site yet (#44), so the stack has nothing to publish into"
fi
printf '%s\n' "$seed_out" | sed 's/^/    /'
site_slug="$(printf '%s\n' "$seed_out" | sed -n 's|.*Demo site: */v1/sites/\([A-Za-z0-9_-]*\).*|\1|p' | head -1)"
cap_token="$(printf '%s\n' "$seed_out" | sed -n 's/.*Capability token: *//p' | head -1 | tr -d '\r' | awk '{print $1}')"
embed_token="$(printf '%s\n' "$seed_out" | sed -n 's/.*Embed token: *//p' | head -1 | tr -d '\r' | awk '{print $1}')"
[ -n "$site_slug" ] || die "could not read the seeded site slug from scripts/seed.py output"
[ -n "$cap_token" ] || die "scripts/seed.py did not print a capability token (see #44)"
[ -n "$embed_token" ] || die "scripts/seed.py did not print a read-only embed token (GET /embed/v1/posts rejects the write token)"

# --- 7. API roundtrip: capability link -> publish post -> public page ---------

log "API roundtrip (minted capability link -> publish post -> public page -> read-only embed token)"
./scripts/deploy_smoke.sh --base-url "$BASE_URL" --compose-file "$COMPOSE_FILE" --max-wait "$TIMEOUT" \
  --site-slug "$site_slug" --capability-token "$cap_token" --embed-token "$embed_token" \
  || die "the API roundtrip failed (see [SMOKE] output above)"

log "SELF-HOST E2E PASSED — the documented path deploys and serves content"
