# Production Configuration Reference

This document lists every environment variable the AgentCMS service reads, which are
secrets, which have safe defaults, and which will cause the service to refuse to
start in production if misconfigured.

> **Rule**: In `APP_ENV=production` the service **refuses to boot** when a required
> secret is missing or a development default is still in place. The error message
> tells you exactly what to fix.

---

## Quick Reference Table

| Variable | Required in Prod? | Secret? | Safe Default? | Description |
|----------|-------------------|---------|---------------|-------------|
| `APP_ENV` | Yes | No | `development` | Runtime mode: `development`, `test`, `production` |
| `SECRET_KEY` | **Yes** | **Yes** | **No** | Token signing key (≥32 chars) |
| `ADMIN_TOKEN` | **Yes** | **Yes** | **No** | Bootstrap secret for the whole `/v1/admin/*` surface, sent as `X-Admin-Token` (≥32 chars) |
| `DATABASE_URL` | **Yes** | **Yes** | **No** | PostgreSQL connection string |
| `POSTGRES_PASSWORD` | **Yes** | **Yes** | **No** | Postgres password (for compose) |
| `CORS_ORIGINS` | No | No | `""` (empty) | Comma-separated API CORS origins |
| `DEBUG` | Must be `false` | No | `false` | Debug mode (blocks prod boot if `true`) |
| `LOG_LEVEL` | No | No | `INFO` | Log verbosity |
| `LOG_FORMAT` | No | No | `json` | `json` or `text` |
| `DOCS_ENABLED` | No | No | `true` | Enable `/docs`, `/redoc`, `/openapi.json` |
| `S3_*` | Conditional | **Yes** | No | Object storage for media |
| `BACKUP_*` | Conditional | **Yes** | No | Backup encryption & destination |
| `METRICS_TOKEN` | No | **Yes** | `""` (empty) | Shared secret for `/metrics` |
| `TRUSTED_PROXIES` | No | No | `""` (empty) | Proxies whose `X-Forwarded-For` may be believed for the loopback exemption |
| `OTLP_ENDPOINT` | No | No | `""` (empty) | OTel collector endpoint |
| `CAPABILITY_*` | No | No | Various | Link defaults & limits |
| `DOMAIN` | No | No | `localhost` | Caddy domain for auto-TLS |
| `CADDY_EMAIL` | Conditional | **Yes** | `""` | Let's Encrypt email (required if DOMAIN≠localhost) |
| `EMBED_ORIGINS` | No | No | `""` (deny all) | Comma-separated embed allowlist |
| `EMBED_ORIGINS_REGEX` | No | No | `""` (empty = edge silent) | **Derived** from `EMBED_ORIGINS` by `scripts/selfhost.sh`; do not set by hand |
| `EMBED_TOKEN_SCOPE` | No | No | `posts:read` | Scope for embed tokens |

---

## Variable Details

### Admin surface

| Variable | Description |
|----------|-------------|
| `ADMIN_TOKEN` | **Required in production, ≥32 chars.** The bootstrap secret for every `/v1/admin/*` route (`X-Admin-Token: <ADMIN_TOKEN>`). It is a *bootstrap* secret, not a stored token: `scripts/selfhost.sh --setup-only` generates one into `.env`, and rotating it costs nothing (mint your long-lived tokens first, then rotate). A valid dashboard session cookie is also accepted, but only for reads -- cookie-authenticated POST/DELETE additionally require the dashboard CSRF token (`X-CSRF-Token`). Bearer `acms_` tokens need the operator scope `*:read`. |

### Runtime

| Variable | Description |
|----------|-------------|
| `APP_ENV` | **Must be `production` in prod.** Controls validation strictness, log format defaults, and feature flags. |
| `APP_NAME` | Display name in `/healthz`, OpenAPI, and logs. |
| `APP_VERSION` | Version string reported by `/healthz` and `/v1/version`. |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. Affects structured logger. |
| `LOG_FORMAT` | `json` (one machine-readable line per request) or `text` (human-readable). |
| `DOCS_ENABLED` | `true` enables Scalar at `/docs`, ReDoc at `/redoc`, OpenAPI at `/openapi.json`. Keep `true` in prod — the spec is the agent interface. |
| `HOST` / `PORT` | Bind address/port inside container. Default `0.0.0.0:8000`. |
| `CORS_ORIGINS` | Comma-separated list of origins allowed to call the API from browsers. Empty = no CORS middleware (secure default). Example: `https://app.example.com,https://admin.example.com` |
| `DEBUG` | **Must be `false` when `APP_ENV=production`**. Enables debug endpoints and verbose errors. |

### Security

| Variable | Description |
|----------|-------------|
| `SECRET_KEY` | **[prod-required] [secret]** Used to sign capability tokens, API tokens, preview tokens, and HMACs. **Must be ≥32 characters and not the development value.** Generate with: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `DEBUG` | **Blocks prod boot if `true`**. See Runtime. |

### Database

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | **[prod-required] [secret]** SQLAlchemy PostgreSQL URL. Format: `postgresql+psycopg://user:pass@host:port/db`. The dev default (`localhost:5432`) **blocks prod boot**. |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | **[prod-required] [secret]** Used by `docker compose` to bootstrap the Postgres container. `POSTGRES_PASSWORD` has no default in compose (fails fast). |
| `DB_POOL_SIZE` | Connection pool size (default `5`). |
| `DB_POOL_TIMEOUT` | Seconds to wait for a connection before `readyz` fails (default `5`). |
| `DB_ECHO` | Log all SQL (dev only). |

### Object Storage (S3-compatible)

Used by media upload endpoints (`/v1/assets/*`). All optional — media endpoints return 501 if unset.

| Variable | Description |
|----------|-------------|
| `S3_ENDPOINT_URL` | S3-compatible endpoint (e.g., `https://s3.us-east-1.amazonaws.com` or `http://minio:9000`). |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | **[secret]** Credentials. |
| `S3_BUCKET` | Bucket name (default `agentcms-media`). |
| `S3_REGION` | Region (default `us-east-1`). |
| `S3_PUBLIC_BASE_URL` | Public URL prefix for generated asset URLs (e.g., `https://cdn.example.com/agentcms-media`). |

### Capability Links

| Variable | Default | Description |
|----------|---------|-------------|
| `CAPABILITY_DEFAULT_TTL_MINUTES` | `60` | Default link lifetime when not specified. |
| `CAPABILITY_MAX_TTL_DAYS` | `30` | Hard ceiling on requested TTL. |
| `CAPABILITY_RATE_LIMIT_PER_LINK` | `30` | Max requests per link per window. |
| `CAPABILITY_RATE_LIMIT_WINDOW_SECONDS` | `60` | Sliding window duration. |
| `CAPABILITY_MAX_LIVE_LINKS_PER_SITE` | `100` | Refuse to mint more links per site. |

### Observability

| Variable | Description |
|----------|-------------|
| `METRICS_TOKEN` | **[secret]** Shared secret for `GET /metrics`. Empty = metrics still require admin auth but no extra token. Never expose `/metrics` publicly. |
| `TRUSTED_PROXIES` | Comma-separated CIDRs or bare addresses of *your* reverse proxies. Empty (default) = `X-Forwarded-For` is never believed for the `/metrics` and `/v1/admin/*` loopback exemption, so a remote caller cannot claim to be `127.0.0.1`. Unparsable entries are ignored (fail closed to peer-only). **Behind a proxy, use `METRICS_TOKEN`** — see [Metrics behind a proxy](#metrics-behind-a-proxy). |
| `OTLP_ENDPOINT` | OTLP/HTTP endpoint (e.g., `http://jaeger:4318`). Empty = tracing soft-disabled (no export, hot path stays cheap). |
| `OTEL_SERVICE_NAME` | Service name in OTel `service.name` (default `agentcms`). |
| `TRACE_SAMPLE_RATIO` | Sampling rate for normal requests (default `0.1` = 10%). Errors always sampled. |
| `GIT_SHA` / `BUILD_TIME` | Baked-in build metadata for `/v1/version`. Empty = resolved from git/files at runtime. |

### Metrics behind a proxy

`GET /metrics` and the whole `/v1/admin/*` surface exempt a **local** caller from
their credentials. "Local" is decided by the TCP peer address
(`app/api/client_ip.py`), and `X-Forwarded-For` is **not** part of that decision
unless you have listed the peer's network in `TRUSTED_PROXIES`. The header is
client-supplied, so believing it unconditionally would let any remote caller send
`X-Forwarded-For: 127.0.0.1` and walk straight through the gate (#49).

| Deployment shape | What to do |
|------------------|------------|
| Scrape from the same host (`localhost:8000/metrics`) | Nothing. The peer is loopback. |
| Scrape through Caddy (the shipped stack) | Nothing: scrape with `X-Metrics-Token` (Prometheus does this already), or scrape the API port directly. The `Caddyfile` replaces the header with `{remote}` in any case. |
| Scrape through a load balancer you control | Set `METRICS_TOKEN` and send `X-Metrics-Token`. Prefer this over `TRUSTED_PROXIES`. |
| The loopback exemption must work *through* your proxy | Set `TRUSTED_PROXIES` to the proxy's addresses (e.g. `TRUSTED_PROXIES=10.0.0.0/8`). Only the right-most hop that is not itself a trusted proxy counts, so a proxy that *appends* the address it saw still names the real caller. |

Rules to keep in mind when you do set `TRUSTED_PROXIES`:

* never put `0.0.0.0/0` or `::/0` in it — that reintroduces the bug for every caller;
* a chain made only of trusted proxies proves nothing and stays gated;
* a malformed entry is ignored rather than guessed, so the deployment fails closed
  to peer-only (a typo can never *widen* the exemption).

### Backups & Restore Drill

| Variable | Description |
|----------|-------------|
| `BACKUP_STORE_URL` | Destination for nightly `pg_dump`: `file:///absolute/path` or `s3://bucket/prefix`. Empty = backups disabled. |
| `BACKUP_DIR` | Local directory when `BACKUP_STORE_URL` empty (default `.backups`). |
| `BACKUP_PASSPHRASE` | **[secret]** AES-256 passphrase for encrypted dumps. Required if backups enabled. |
| `BACKUP_RETENTION_DAILY` / `BACKUP_RETENTION_MONTHLY` | Keep N daily + M monthly snapshots (defaults `30`/`12`). |
| `BACKUP_SOURCE_URL` | Static DB URL to diff against in restore drill (usually production). Empty = diff against `DATABASE_URL`. |

### Reverse Proxy (Caddy)

| Variable | Description |
|----------|-------------|
| `DOMAIN` | Domain name for automatic HTTPS (e.g., `cms.example.com`). Default `localhost` = self-signed certs. |
| `CADDY_EMAIL` | **[secret]** Email for Let's Encrypt registration. **Required if `DOMAIN` ≠ `localhost`**. |

### Embed Configuration

| Variable | Description |
|----------|-------------|
| `EMBED_ORIGINS` | Comma-separated list of origins allowed to embed the CMS via the embed script. **Empty = deny all (secure default).** Example: `https://example.com,https://blog.example.com` |
| `EMBED_ORIGINS_REGEX` | The same allowlist in the form the Caddy edge can match: pipe-joined, with `.` written as `[.]` (`https://a[.]example[.]com\|https://b[.]example[.]com`). **Derived — never set it yourself.** `scripts/selfhost.sh` recomputes it from `EMBED_ORIGINS` on every run and writes it into `.env`. |
| `EMBED_TOKEN_SCOPE` | Scope granted to embed tokens. **Must be a read-only scope.** Currently only `posts:read` is supported. |

#### Where embed CORS is decided

The **API is the only authority**: `app/api/embed/routes.py` answers preflights on
`/embed/v1/*` and emits `Access-Control-Allow-Origin` only for an origin in
`EMBED_ORIGINS` (nothing at all when the list is empty).

`deploy/compose/Caddyfile` may short-circuit a preflight before the request reaches
the API, so its matchers are gated on the *same* allowlist **and** on the path
prefix `/embed/*`:

* With `EMBED_ORIGINS=` (the default) there is no allowlist, so the edge's matchers
  cannot match any `Origin` and the edge stays silent. The API's deny-all answer is
  what reaches the caller.
* With an allowlist, only those exact origins are reflected, with
  `Access-Control-Allow-Credentials: true`, and only for `/embed/*` — the admin API
  and the capability-link surface are never handed embed CORS by the edge.
* A stack deployed with plain `docker compose` (never through `scripts/selfhost.sh`)
  has no `EMBED_ORIGINS_REGEX`, so the edge stops short-circuiting and the API serves
  every embed CORS response. That is the fail-closed direction, not a break.

---

## Fail-Fast Validation at Boot

When `APP_ENV=production`, the service validates configuration **before** starting
the HTTP server. If validation fails, the process exits with a non-zero code and
logs a clear error message. No half-started app, no confusing runtime errors.

### Validation Rules (from `app/config.py:Settings._guard_production`)

1. **`SECRET_KEY`** must be set, not the dev value, and ≥32 chars.
2. **`DATABASE_URL`** must not be the dev default (`postgresql+psycopg://agentcms:agentcms@localhost:5432/agentcms`).
3. **`DEBUG`** must be `false`.

### Example Failure Output

```
ValueError: refusing to start in production: SECRET_KEY is unset or still the development value; generate one with python -c "import secrets; print(secrets.token_urlsafe(48))"; DATABASE_URL is still the development default
```

The message lists **all** problems at once so you can fix them in one pass.

### Compose-Level Validation

`deploy/compose/docker-compose.prod.yml` uses `${VAR:?message}` syntax for
critical variables (`SECRET_KEY`, `POSTGRES_PASSWORD`). Docker Compose will
refuse to start with a clear message if they are unset:

```
The SECRET_KEY variable is not set. Defaulting to a blank string.
Error: set SECRET_KEY in the environment
```

---

## Secrets Management

| Secret | Where Used | Rotation |
|--------|------------|----------|
| `SECRET_KEY` | Token signing, HMAC | Rotate by generating new key; invalidates all existing tokens |
| `POSTGRES_PASSWORD` | Database auth | Rotate via managed Postgres or `ALTER USER` |
| `S3_SECRET_ACCESS_KEY` | Object storage | Rotate via IAM/MinIO |
| `BACKUP_PASSPHRASE` | Backup encryption | **Cannot rotate without losing restore ability** — keep safe |
| `METRICS_TOKEN` | `/metrics` auth | Rotate freely |
| `CADDY_EMAIL` | Let's Encrypt | Not a secret per se, but treat as sensitive |
| `OTLP_ENDPOINT` | Tracing | May contain auth tokens |

**Recommendation**: Use a secrets manager (AWS Secrets Manager, 1Password CLI,
Docker secrets, GitHub Actions secrets) and inject at deploy time. Do not commit
`.env` to version control.

---

## Configuration Checklist for Production

- [ ] `APP_ENV=production`
- [ ] `SECRET_KEY` generated (`secrets.token_urlsafe(48)`) and ≥32 chars
- [ ] `DATABASE_URL` points to production Postgres (not localhost)
- [ ] `POSTGRES_PASSWORD` set in compose `.env`
- [ ] `DEBUG=false`
- [ ] `CORS_ORIGINS` set to your admin/frontend origins (or empty)
- [ ] `DOMAIN` set to your production domain
- [ ] `CADDY_EMAIL` set for Let's Encrypt
- [ ] `EMBED_ORIGINS` set to your embedding site origins (if using embed)
- [ ] `BACKUP_STORE_URL` and `BACKUP_PASSPHRASE` set (if enabling backups)
- [ ] `METRICS_TOKEN` set (if exposing metrics, or if the app sits behind a proxy)
- [ ] `TRUSTED_PROXIES` left empty unless a loopback scrape *must* work through your own proxy (never `0.0.0.0/0`)
- [ ] `OTLP_ENDPOINT` set (if using tracing)
- [ ] `S3_*` set (if using media uploads)

---

## Local Development Overrides

For local development, copy `deploy/.env.example` to `.env` and adjust:

```bash
cp deploy/.env.example .env
# Edit .env: set SECRET_KEY to dev value, DOMAIN=localhost, etc.
docker compose --env-file .env -f deploy/compose/docker-compose.prod.yml up -d --build
```

The dev defaults in `.env.example` work with the compose stack as-is for a
local HTTPS trial (self-signed certs).

---

## Related Docs

- [Quickstart](./quickstart.md) — Clone to HTTPS in 5 minutes
- [Embed Guide](./embed.md) — Drop-in embedding into existing sites
- [Operations Runbook](../ops/runbook.md) — Backups, restore, scaling, incidents