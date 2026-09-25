# Running AgentCMS locally, end to end

This walks a human from a fresh clone to a running API with a migrated,
seeded database. **Every command below was executed at this revision** (commit
`24b6870`) and its output is pasted, trimmed. If a step needs a real value,
the value used here is shown; mask secrets rather than ship them.

## 1. Prerequisites

- **Python 3.11+** (`python3 --version`). The package declares `requires-python = ">=3.11"`.
- **Docker** with the compose plugin (`docker compose version`). The suite also
  falls back to a local `initdb`/`pg_ctl` when no daemon is running — see
  `docs/TESTING.md`.
- ~150 MB disk for Postgres 16 + MinIO images, plus pip wheels.

```text
$ python3 --version
Python 3.11.9

$ docker compose version
Docker Compose version v5.1.2
```

## 2. Environment variables

Everything the service reads is declared in `.env.example` — **that file is the
source of truth**. Copy it to `.env` and edit to taste:

```bash
cp .env.example .env
```

(`make install` does this copy automatically when `.env` does not exist.)

The variables that matter for a plain local run:

| Variable | Default for dev | Purpose |
| --- | --- | --- |
| `APP_ENV` | `development` | `development` \| `test` \| `production`. Production refuses to boot on dev defaults |
| `SECRET_KEY` | dev string | Signs tokens/sessions. Generate a real one with `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `DATABASE_URL` | `postgresql+psycopg://agentcms:agentcms@localhost:5432/agentcms` | SQLAlchemy URL; matches `docker compose up db` |
| `POSTGRES_USER/_PASSWORD/_DB/_PORT` | `agentcms` / `agentcms` / `agentcms` / `5432` | Credentials fed to the compose Postgres container |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Dev server bind address |
| `S3_*` | MinIO dev creds | Media API (optional; unset = media endpoints disabled). Only needed with the `storage` profile |
| `BACKUP_STORE_URL`, `BACKUP_PASSPHRASE`, … | optional | Backups/restore drill (see `docs/ops/runbook.md`) |

An explicit note for this worker: `~/MINIO-IMAGE-FIX.md` explains that Docker
Hub's `minio/minio` namespace is no longer pullable here. The checked-in
`compose.yml` already uses **`quay.io/minio/minio:latest`** (the official MinIO
registry), which pulls fine — see §5.

## 3. Install

Creates the project venv (if needed) and installs the **pinned** dependency set
from `requirements.lock.txt` — the same file CI installs from, so a laptop and a
CI runner resolve identical versions (`pyproject.toml` declares the ranges; the
lock pins them):

```text
$ make install
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet --no-deps -r requirements.lock.txt
.venv/bin/pip install --quiet --no-deps -e .
```

Adding or changing a dependency means editing `pyproject.toml` and regenerating
the lock with `make lock` (or `pip-compile --extra dev --strip-extras
--output-file requirements.lock.txt pyproject.toml`), then committing both.
`make check-lock` — part of `make lint` and of CI's `lint` job — fails when the
two disagree, so the lock cannot silently drift from `pyproject.toml`.

## 4. Start Postgres (dev stack)

`make up` starts the `db` service from `compose.yml` (Postgres 16) and blocks
until `pg_isready`:

```text
$ docker compose up -d db
 Network agentcms_default Creating
 Network agentcms_default Created
 Container agentcms-db Creating
 Container agentcms-db Created
 Container agentcms-db Starting
 Container agentcms-db Started

$ docker compose ps --format '{{.Name}} {{.Status}} {{.Ports}}'
agentcms-db Up 2 seconds (healthy) 0.0.0.0:55433->5432/tcp, [::]:55433->5432/tcp
```

> On a fresh clone compose maps **5432** (`POSTGRES_PORT` default). On this
> machine a Homebrew Postgres already owns 5432, so the repo `.env` pins
> `POSTGRES_PORT=55433` (and `DATABASE_URL` to match). Change `.env`, not
> `compose.yml`, if your machine needs a different port.

`make up` and `make up-storage` are the two entry points:

- `make up` — Postgres only (all you need for the API + tests).
- `make up-storage` — Postgres **and** MinIO (`--profile storage up db minio`),
  required from the media API (#20) onwards.

## 5. Object storage (MinIO) — the storage profile

Compose keeps storage behind an opt-in profile because nothing before the media
API needs it:

```bash
make up-storage          # or: docker compose --profile storage up -d db minio
```

The image is `quay.io/minio/minio:latest` on purpose (Docker Hub's
`minio/minio` is not pullable on this worker — see `~/MINIO-IMAGE-FIX.md`).
It listens on `:9000` (S3 API) and `:9001` (console). Media URLs are built from
`S3_PUBLIC_BASE_URL` (e.g. `http://localhost:9000/agentcms-media`).

## 6. Apply migrations

```text
$ make migrate
.venv/bin/alembic upgrade head
INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
INFO  [alembic.runtime.migration] Will assume transactional DDL.
```

Idempotent: running it against an already-migrated database prints the two INFO
lines and exits 0. To see where you are:

```text
$ python -m alembic current
f4e5d6c7b8a9 (head)
```

Useful migration targets:

```bash
make migration m="change something"   # autogenerate a new revision from the models
make downgrade                        # roll back one revision
make gate-migrations                  # DESTRUCTIVE CI clone: up → down → up → alembic check
```

## 7. Seed a demo site

```text
$ make seed
Seed complete for AgentCMS.
  Demo site:         /v1/sites/blog
  Capability token:  cap_blog_<redacted>
  Instruction sheet: GET  /c/cap_blog_<redacted>
  Write a draft:     POST /c/cap_blog_<redacted>/posts   (no Authorization header needed)
  Public blog:       GET  /blog  ·  /blog/rss.xml  ·  /llms.txt
```

Creates the `blog` site, three posts, and one capability token — enough to
drive the API by hand.

## 8. Serve the API

`make dev` migrates if needed, prints where things are, and serves with `--reload`:

```text
$ make dev
  AgentCMS is starting:
    API         http://127.0.0.1:8000/docs
    OpenAPI     http://127.0.0.1:8000/openapi.json
    health      http://127.0.0.1:8000/healthz   (liveness, no DB)
    readiness   http://127.0.0.1:8000/readyz    (DB reachable)
```

(`make serve` is the non-reload equivalent — bring your own Postgres.)

## 9. Reach the endpoints

```text
$ curl -fsS http://127.0.0.1:8000/healthz
{"status":"ok","service":"AgentCMS","version":"0.3.1","env":"development"}

$ curl -fsS http://127.0.0.1:8000/readyz
{"status":"ready","checks":{"database":{"status":"ok","ok":true,"detail":"","latency_ms":107.76},"queue":{"status":"ok","ok":true,"detail":"pending=0, oldest=0s","latency_ms":2.22},"object_store":{"status":"down","ok":false,"detail":"[Errno 61] Connection refused","latency_ms":0.0}}}

$ curl -sS -o /dev/null -w "%{http_code} %{content_type}\n" http://127.0.0.1:8000/docs
200 text/html; charset=utf-8

$ curl -fsS http://127.0.0.1:8000/openapi.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['openapi'], d['info']['version'], len(d['paths']), 'paths')"
3.1.0 0.3.1 69 paths
```

Notes on what you are actually reading:

- `/healthz` never touches the database — 200 here means "process up".
- `/readyz` probes every dependency and returns `503 + application/problem+json`
  (with `Retry-After`) when a **critical** dependency is down. Only the **database**
  is critical: the object store is advisory, so `/readyz` stays 200 when MinIO is
  down (as in the output above) — see `app/services/health.py`.
- `/docs` is Scalar, rendered against `/openapi.json`, which must report
  OpenAPI **3.1.0** (this revision: `69` documented paths).

## 10. Try the API the way an agent would

Grab the capability token from the seed output, then:

```bash
curl -fsS http://127.0.0.1:8000/c/cap_blog_<token>            # the instruction sheet the agent reads
curl -fsS http://127.0.0.1:8000/blog/rss.xml                  # public feed
curl -fsS http://127.0.0.1:8000/llms.txt                      # links worth showing an LLM
curl -fsS http://127.0.0.1:8000/v1/search?q=agents            # full-text search
```

## 11. Stopping

```bash
make down    # stop the stack, keep volumes
make nuke    # stop and delete the db/minio volumes
```

## Known gaps at this revision

- The test suite needs a real Postgres; there is **no SQLite fallback** (see
  `docs/TESTING.md`). `DATABASE_URL=sqlite://…` is not supported.
- `make dev` binds to the `HOST`/`PORT` in `.env`; if either port is taken the
  server fails to bind with a clear uvicorn error — change `.env`.