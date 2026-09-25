# Deploying AgentCMS

What actually runs in production from this repository, what has to happen
before the app starts, and how to undo it. **Every command below was executed
at this revision** (commit `24b6870`) and its output is pasted, trimmed.

## One paragraph

Production is a **Docker image** built from the multi-stage `Dockerfile`
(`python:3.11-slim` runtime) and composed by `compose.prod.yml`: a Postgres 16
`db`, a one-shot `migrate` job, and the `api` service whose rollout is gated on
migrations having completed **and** on `GET /readyz` returning 200. The
deployment can be driven by hand (`docker compose -f compose.prod.yml up`) or
through the migration-gated `scripts/deploy.sh` (`make deploy` / `make rollback`).

## 1. Build the image

```text
$ docker build -t agentcms:local .
…
#21 exporting manifest list …
#21 naming to docker.io/library/agentcms:local done
#21 unpacking to docker.io/library/agentcms:local
#21 DONE 4.9s
```

(`make docker-build` is the same command.) The image entries:

- `ENTRYPOINT` = `docker/entrypoint.sh` — waits for Postgres, runs
  `alembic upgrade head`, then `exec`s the command.
- `CMD` = `uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers`.
- `HEALTHCHECK` = `curl -fsS http://127.0.0.1:8000/healthz`.
- Runs as unprivileged `agentcms` (uid 10001).

## 2. Compose: dev vs prod

| | `compose.yml` (dev) | `compose.prod.yml` (prod) |
| --- | --- | --- |
| Postgres | `postgres:16-alpine`, host port exposed (`POSTGRES_PORT`) | same image, **no** host-exposed port |
| API | runs on the host (`make dev`) | `api` service built from source with a `/readyz` healthcheck |
| Migrate | host `alembic` (`make migrate`) | **`migrate` one-shot service** (`alembic upgrade head`) |
| Storage | opt-in `minio` (`quay.io/minio/minio:latest`, storage profile) | not included (bring your own S3 endpoint) |

The `migrate` service and the `api` ordering are the load-bearing part:

```yaml
migrate:
  command: ["python", "-m", "alembic", "upgrade", "head"]
  depends_on: { db: { condition: service_healthy } }

api:
  depends_on:
    db: { condition: service_healthy }
    migrate: { condition: service_completed_successfully }   # app never races the migration job
```

Both got fixed in this push: the `api` entrypoint also auto-migrates
(defensive), so when the two containers started **simultaneously** on a fresh
volume they raced to `CREATE TABLE alembic_version` and one died with a
`UniqueViolation`. Ordering `api` strictly after `migrate` completed removes
the race — verified by a clean `docker compose -f compose.prod.yml up -d` on an
empty volume (below).

## 3. Required secrets and environment

Nothing is baked into the image; every value comes from the environment
(`compose.prod.yml` interpolation, or your orchestrator). Production refuses to
boot on the development defaults (`app/config.py`).

| Variable | Required in prod | Why |
| --- | --- | --- |
| `DATABASE_URL` | ✅ (not the dev default) | SQLAlchemy DSN for the app and `migrate`; composed as `postgresql+psycopg://…@db:5432/…` |
| `SECRET_KEY` | ✅, ≥ 32 chars | Token/session signing. `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `POSTGRES_PASSWORD` | ✅ (`${POSTGRES_PASSWORD:?…}` fails otherwise) | Postgres superuser password; created on first volume init |
| `POSTGRES_USER` / `POSTGRES_DB` | default `agentcms` | Superuser + database name |
| `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET`, `S3_REGION` | only if media is on | S3-compatible object store credentials (MinIO elsewhere) |
| `S3_PUBLIC_BASE_URL` | only if media is on | Public base URL media URLs are built from — the ticket's "`PUBLIC_BASE_URL`"; there is **no** `PUBLIC_BASE_URL` setting, this is the variable |
| `METRICS_TOKEN` | recommended | Shared secret for `GET /metrics` (empty = authenticated admin tokens + loopback only) |
| `OTLP_ENDPOINT` | optional | OTel/HTTP trace export endpoint (empty = soft-disabled) |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | Structured request logs |
| `BACKUP_STORE_URL`, `BACKUP_PASSPHRASE`, `BACKUP_RETENTION_*` | backups enabled only | Nightly encrypted `pg_dump` (see `docs/ops/runbook.md`) |

Full list, with comments: `.env.example` — copy to `.env` (compose reads it for
interpolation) or export from your secret store.

## 4. Out-of-the-box prod stack (hand-driven)

With `SECRET_KEY` and `POSTGRES_PASSWORD` provided (compose will refuse to even
parse without them):

```text
$ APP_ENV=production SECRET_KEY=prod-test-secret-key-0123456789abcdef0123456789abcdef \
    POSTGRES_PASSWORD='ci-prod-password' AGENTCMS_IMAGE_TAG=agentcms:v0.3.1 \
    docker compose -f compose.prod.yml up -d --build
 Container agentcms-prod-db-1 Waiting
 Container agentcms-prod-db-1 Healthy
 Container agentcms-prod-migrate-1 Starting
 Container agentcms-prod-migrate-1 Started
 Container agentcms-prod-db-1 Waiting
 Container agentcms-prod-migrate-1 Waiting
 Container agentcms-prod-db-1 Healthy
 Container agentcms-prod-migrate-1 Exited            # migrate job completed (exit 0)
 Container agentcms-prod-api-1 Starting             # api only starts after that
 Container agentcms-prod-api-1 Started
```

The result — `api` healthy, `migrate` exit code `0`, `/readyz` green:

```text
$ docker compose -f compose.prod.yml ps --format '{{.Name}}\t{{.Status}}'
agentcms-prod-api-1	Up 12 seconds (healthy)
agentcms-prod-db-1	Up 17 seconds (healthy)

$ docker inspect -f '{{.State.ExitCode}}' agentcms-prod-migrate-1
0

$ curl -fsS http://127.0.0.1:8000/readyz
{"status":"ready","checks":{"database":{"status":"ok","ok":true,"detail":"","latency_ms":1.46},…}}
```

`make prod-up` / `make prod-down` wrap this compose file.

## 5. The migration-gated deploy (`scripts/deploy.sh`)

The script is the recommended rollout path. It was **executed end-to-end at
this revision** in both of its modes (plain rollout and fresh-bootstrap), and
both finished `==> deploy complete` (below).

```text
$ make deploy            # ./scripts/deploy.sh  (env: TAG, IMAGE_REPO, MIGRATE_JOB)
```

What it actually does:

1. **Ensure `db` is up first** — `docker compose -f compose.prod.yml up -d db`.
   Idempotent: a no-op on a running stack, it bootstraps Postgres on a fresh
   environment so everything below (preflight, migrate job, api) can resolve
   the `db` host. (This was a real first-deploy bug, fixed in this push:
   without it the rolling update started `api` against nothing and died with
   `failed to resolve host 'db'`.)
2. **Migration gate** — `./scripts/deploy_preflight.sh` runs `alembic check`.
   Green (`no pending migrations`) → skip to the build. Any failure →
   `MIGRATE_JOB=1` (default) runs the one-shot job as an explicit
   `compose run --rm --no-deps migrate`; `MIGRATE_JOB=0` refuses to rollout.
3. `docker build -t $TAG .`.
4. **Rolling update** — `up -d --no-deps --wait api --force-recreate`; compose
   keeps the old container serving until the new one is healthy.
5. **Health gate** — `read_health_gate` polls `GET /readyz` (default
   `http://127.0.0.1:8000/readyz`) every 2s, up to 240s, before the deploy is
   declared complete.
6. The live image tag is recorded in `.deploy-last-tag` (gitignored
   operational state) as the rollback target for the *next* deploy.

**Mode A — plain rollout (no pending migrations), real output:**

```text
$ make deploy
==> ensuring database service is up...
 Container agentcms-prod-db-1 Started
==> migration gate...
==> no pending migrations
==> building agentcms:deploy
 Container agentcms-prod-api-1 Starting
 Container agentcms-prod-api-1 Waiting
 Container agentcms-prod-api-1 Healthy
==> health gate passed after 1 attempts
==> deploy complete: agentcms:deploy
```

**Mode B — first deploy to an empty volume (migrations pending), real output:**
the preflight cannot satisfy against an empty database, so the one-shot job
applies every revision before the api is allowed up.

```text
$ make deploy
==> ensuring database service is up...
 Container agentcms-prod-db-1 Started
==> migration gate...
pending migrations or schema drift detected by 'alembic check'
==> running migration job (one-shot container)...
==> waiting for the database
==> applying migrations
INFO  [alembic.runtime.migration] Running upgrade  -> 76e194a9b7ba, create_all_tables
INFO  [alembic.runtime.migration] Running upgrade 76e194a9b7ba -> a1b2c3d4e5f6, add_token_fields
INFO  [alembic.runtime.migration] Running upgrade … #19 / #20 / #21 …
INFO  [alembic.runtime.migration] Running upgrade c3d4e5f6a7b8 -> f4e5d6c7b8a9, Add request_id -> webhook_deliveries (#24).
==> building agentcms:deploy
 Container agentcms-prod-api-1 Starting
 Container agentcms-prod-api-1 Waiting
 Container agentcms-prod-api-1 Healthy
==> health gate passed after 1 attempts
==> deploy complete: agentcms:deploy
```

Related targets:

```bash
make preflight-migrations    # dry-run: fails if alembic check reports pending migrations
make gate-migrations         # DESTRUCTIVE clone of the CI migrations job (up/down/up/check)
```

## 6. Health checks that gate the rollout

- **`/healthz`** — liveness only, *never touches the database*. The image
  `HEALTHCHECK` uses it.
- **`/readyz`** — probes every dependency and returns `503 + problem+json`
  (with `Retry-After: 5`) when a **critical** dependency is down. Only the
  database is critical; object store / queue are advisory (see
  `app/services/health.py`). The compose api healthcheck and
  `scripts/deploy.sh` both gate on `/readyz`.

```text
$ curl -fsS http://127.0.0.1:8000/healthz
{"status":"ok","service":"AgentCMS","version":"0.3.1","env":"production"}

$ curl -fsS http://127.0.0.1:8000/v1/version
{"service":"AgentCMS","version":"0.3.1","env":"production","git_sha":"unknown","build_time":"1789754635","migration_head":"f4e5d6c7b8a9","openapi_version":"3.1.0"}
```

(`git_sha` is `unknown` here because no `.git` exists inside the container;
set `GIT_SHA` at build time for baked-in metadata.)

## 7. Rollback

```text
$ make rollback              # ./scripts/deploy.sh --rollback
==> rolling back to <previous image>
```

`--rollback` re-points compose to the image recorded in `.deploy-last-tag` and
re-runs the same `/readyz` gate. Two honest constraints:

- If the failed deploy already applied **additive** migrations, roll back the
  *application* first; **schema downgrades are out of scope by design** — the
  migration policy is forward-only (see `docs/data-model.md` and the runbook's
  deploy drill).
- If no previous image is recorded, the script refuses loudly.

## 8. What the GitHub Pages workflow publishes (and what it does NOT)

There are two workflows in this repository, and they deploy **different
things**. Don't confuse them:

- **`.github/workflows/ci.yml`** — CI gate for the *service*: lint, migrations,
  tests. Red blocks merge into `main`. It deploys nothing.
- **`docs/ci/github-pages.yml`** — the GitHub Pages publisher. It runs on
  `workflow_dispatch` or a `post.published` repository dispatch, builds a
  **static export of the blog** (`agentcms export --site … --out dist`,
  requiring the `DATABASE_URL` secret and the `SITE_SLUG` / `BASE_URL` repo
  variables), and pushes `dist/` to the `gh-pages` branch.

So: **GitHub Pages publishes the static Nocturne preview site (a static export
of the content), not the API.** The live Pages URL
(`https://smartalfred.github.io/AgentCMS/` — 404 on a private repo free plan)
serves the repo-root Nocturne preview files (`index.html`, …) — those files are
hand-maintained and must not be confused with the API deployment. The agent
API runs from the Docker image of §1–§7, never from Pages.

Deterministic export, verbatim (this revision, against the seeded dev DB):

```text
$ agentcms export --site blog --out /tmp/agentcms-export \
    --base-url https://cms.example.com/ --verify
exported blog: 3 posts, 24 files (0 reused, 0 pruned) in 0.194s
verify OK: 23 files matched manifest.json for /private/tmp/agentcms-export
```

## 9. Using published GHCR images (self-host)

Every release from v0.3.1 publishes a **multi-arch image to GHCR**
(`linux/amd64` + `linux/arm64`) with SBOM and provenance attestation. Pin the
**digest** — tags move, digests do not.

```text
# AgentCMS v0.3.1, as deployed and asserted by the release run:
ghcr.io/smartalfred/agentcms@sha256:c25db8216b7a61035980822838b7b89c46e6e07f9a34074694353569cc92dd85

# use the digest in your .env (NOT the tag)
AGENTCMS_IMAGE_TAG=ghcr.io/smartalfred/agentcms@sha256:c25db8216b7a61035980822838b7b89c46e6e07f9a34074694353569cc92dd85
```

The release workflow (`.github/workflows/release.yml`) refuses to publish unless
the tag equals `v` + the version in `pyproject.toml`, and it writes the digest and
the ready-to-paste `AGENTCMS_IMAGE_TAG=…` line into the run summary **and** the
bottom of the GitHub release notes. Its `boot the published image` job then deploys
that exact digest through `scripts/selfhost_e2e.sh`, so the artifact self-hosters
pull is the artifact that was booted and exercised here.

Refreshing this pin at the next release: cut the tag, wait for the run to go green,
copy the new `pin this` line from the release notes into the docs and your `.env`.
Rollback: set `AGENTCMS_IMAGE_TAG` back to the previous digest, run the `migrate`
service, restart (see `docs/ops/runbook.md` §Upgrade).

The production guard (`app/config.py`) accepts digests and full semver tags
(`v0.3.1`), but rejects `latest`, `local`, `dev`, or major.minor-only tags.

If you prefer to build locally, `make docker-build` / `scripts/selfhost.sh`
still work; they tag `agentcms:v0.3.1` from `pyproject.toml` which the guard
also accepts.

> **Package visibility (open)**: the GHCR package is not public yet, so an anonymous
> `docker pull` returns `401 Unauthorized`. Until that is flipped, authenticate first
> (`docker login ghcr.io` with a token carrying `read:packages`). Tracked in #38.

## 10. Known gaps at this revision

- `scripts/deploy.sh` was verified end-to-end at this revision in both modes
  (see §5). Its one real limitation is per-line: `MIGRATE_JOB=0` refuses to
  rollout while migrations are pending — that is intentional (migrate
  off-hours first, then deploy).
- S3/MinIO-backed media uploads and `s3://` backups are exercised only when a
  store is actually running; the dev prep is `make up-storage`.