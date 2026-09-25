#!/usr/bin/env bash
# Self-host AgentCMS in one command (#31 self-host DX, #35 turnkey fix).
#
#   ./scripts/selfhost.sh                              generate .env, validate, build, start
#   ./scripts/selfhost.sh --setup-only                 only write/fill .env (no Docker needed)
#   ./scripts/selfhost.sh --env-file p.env             use a different env file
#   ./scripts/selfhost.sh --tag ghcr.io/owner/agentcms@v0.3.0   pin AGENTCMS_IMAGE_TAG explicitly
#   ./scripts/selfhost.sh --tag ghcr.io/owner/agentcms@sha256:abc123   pin to immutable digest
#   ./scripts/selfhost.sh --no-build                   reuse an already built image
#   DOMAIN=cms.example.com CADDY_EMAIL=me@example.com ./scripts/selfhost.sh
#
# Why the --env-file flag matters: `docker compose -f deploy/compose/...`
# resolves its project directory to deploy/compose/, so a repository-root .env
# is NOT read implicitly.  This script always passes it explicitly, so the
# single .env in the repository root works for the whole stack.
#
# The script is idempotent: an existing env file is kept, and only blank or
# unsafe (development default) values are replaced.  It never deletes data.
#
# For production, use a published GHCR digest (see docs/DEPLOYING.md §9):
#   ./scripts/selfhost.sh --tag ghcr.io/smartalfred/agentcms@sha256:abc123...
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

ENV_FILE="${ENV_FILE:-.env}"
COMPOSE_FILE="${COMPOSE_FILE:-deploy/compose/docker-compose.prod.yml}"
TEMPLATE="deploy/.env.example"
DEV_SECRET="dev-insecure-secret-change-me-please-000000000000"
TAG=""
SETUP_ONLY=0
NO_BUILD=0

log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --env-file)   ENV_FILE="${2:?--env-file needs a path}"; shift 2 ;;
    --env-file=*) ENV_FILE="${1#*=}"; shift ;;
    --tag)        TAG="${2:?--tag needs a value}"; shift 2 ;;
    --tag=*)      TAG="${1#*=}"; shift ;;
    --setup-only) SETUP_ONLY=1; shift ;;
    --no-build)   NO_BUILD=1; shift ;;
    -h|--help)    usage 0 ;;
    *)            die "unknown option: $1 (try --help)" ;;
  esac
done

# --- 1. env file ------------------------------------------------------------

if [ -f "$ENV_FILE" ]; then
  log "keeping existing $ENV_FILE"
else
  [ -f "$TEMPLATE" ] || die "$TEMPLATE not found — run this from a clone of AgentCMS"
  cp "$TEMPLATE" "$ENV_FILE"
  log "created $ENV_FILE from $TEMPLATE"
fi
chmod 600 "$ENV_FILE" 2>/dev/null || true

value_of() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -1; }

set_value() { # name value — replace the line, or append it if absent
  local name="$1" value="$2" tmp
  tmp="$(mktemp)"
  if grep -q "^${name}=" "$ENV_FILE"; then
    awk -v n="$name" -v v="$value" \
      '$0 ~ "^"n"=" { print n"="v; next } { print }' "$ENV_FILE" > "$tmp"
  else
    cat "$ENV_FILE" > "$tmp"
    printf '%s=%s\n' "$name" "$value" >> "$tmp"
  fi
  mv "$tmp" "$ENV_FILE"
  log "set $name"
}

# Secrets: generate whenever blank or still an insecure development value.
secret_key="$(value_of SECRET_KEY)"
if [ -z "$secret_key" ] || [ "$secret_key" = "$DEV_SECRET" ] || [ "${#secret_key}" -lt 32 ]; then
  command -v python3 >/dev/null || die "python3 is required to generate SECRET_KEY"
  set_value SECRET_KEY "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
fi

# hex only: the password is interpolated into a database URL by compose
if [ "$(value_of POSTGRES_PASSWORD)" = "changeme" ] || [ -z "$(value_of POSTGRES_PASSWORD)" ]; then
  command -v openssl >/dev/null || die "openssl is required to generate POSTGRES_PASSWORD"
  set_value POSTGRES_PASSWORD "$(openssl rand -hex 24)"
fi

# DATABASE_URL: the template ships the *development* default, which the app
# rejects when APP_ENV=production.  Point it at the compose service instead —
# the value is only used by tooling that reads .env (compose overrides it for
# the containers anyway).
dev_database_url="postgresql+psycopg://agentcms:agentcms@localhost:5432/agentcms"
database_url="$(value_of DATABASE_URL)"
if [ -z "$database_url" ] || [ "$database_url" = "$dev_database_url" ]; then
  set_value DATABASE_URL "postgresql+psycopg://$(value_of POSTGRES_USER || echo agentcms):$(value_of POSTGRES_PASSWORD)@db:5432/$(value_of POSTGRES_DB || echo agentcms)"
fi

# Version pinning: the app refuses to boot in production without an immutable
# tag (see app/config.py).  A locally built image gets the repository version.
if [ -z "$TAG" ] && [ -z "$(value_of AGENTCMS_IMAGE_TAG)" ]; then
  version="$(sed -n 's/^version *= *"\([^"]*\)".*/\1/p' pyproject.toml 2>/dev/null | head -1)"
  TAG="agentcms:v${version:-0.0.0}"
fi
[ -n "$TAG" ] && set_value AGENTCMS_IMAGE_TAG "$TAG"

[ -n "${DOMAIN:-}" ] && set_value DOMAIN "$DOMAIN"
[ -n "${CADDY_EMAIL:-}" ] && set_value CADDY_EMAIL "$CADDY_EMAIL"

if command -v git >/dev/null 2>&1; then
  # exit 1 means "not ignored"; anything else (2, 127, a broken git binary) means
  # we cannot tell, so stay quiet rather than print a misleading warning.
  git check-ignore -q -- "$ENV_FILE" || {
    [ $? -eq 1 ] && printf 'warning: %s is not ignored by git — do not commit it\n' "$ENV_FILE" >&2
  }
fi

log "environment ready in $ENV_FILE"

if [ "$SETUP_ONLY" -eq 1 ]; then
  log "--setup-only: nothing started"
  exit 0
fi

# --- 2. validate, then start ------------------------------------------------

command -v docker >/dev/null || die "docker is required to start the stack"
docker info >/dev/null 2>&1 || die "the Docker daemon is not running"

log "validating $COMPOSE_FILE"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config >/dev/null \
  || die "compose rejected $ENV_FILE — fix the reported variables and re-run"

build_flag="--build"
[ "$NO_BUILD" -eq 1 ] && build_flag=""
log "starting the stack (this builds the image the first time)"
# shellcheck disable=SC2086
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d $build_flag

# --- 3. wait for health -----------------------------------------------------

domain="$(value_of DOMAIN)"
url="http://127.0.0.1:8000/healthz"
log "waiting for the API on $url"
for _ in $(seq 1 30); do
  if curl -fsS "$url" >/dev/null 2>&1; then
    log "API is healthy"
    log "open http://${domain:-localhost} (HTTPS once DNS + certificates are ready)"
    exit 0
  fi
  sleep 2
done

printf 'error: the API did not become healthy in 60s — recent logs:\n' >&2
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" logs --tail=50 >&2
exit 1
