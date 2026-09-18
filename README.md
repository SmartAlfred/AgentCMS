# AgentCMS

An API-first CMS whose first user is an AI agent — FastAPI + PostgreSQL 16,
with a S3-compatible object store (MinIO) for media. Agents get a stable,
machine-readable contract: OpenAPI 3.1, `application/problem+json` errors,
tokens + capability links, webhooks, a pollable event feed, full-text search,
HTML→Markdown content, and a versioned human dashboard for supervision.

Two things live in this repository:

1. **The AgentCMS service** (`app/`, `alembic/`, `tests/`, `scripts/`) — the API
   you run, test and deploy. `Dockerfile`, `compose.yml` (dev) and
   `compose.prod.yml` (prod) are at the repo root.
2. **The Nocturne preview site** (`index.html`, `nocturne.css`,
   `nocturne.tokens.json`, `tailwind.config.js`, `DESIGN-SYSTEM.md`) — a
   self-contained dark-mode design-system preview. That is the *static* site
   GitHub Pages publishes (the Pages workflow builds a static export — it does
   **not** deploy the API; see `docs/DEPLOYING.md`).

## 60-second quickstart

Prerequisites: **Python 3.11+** and **Docker** (with `docker compose`).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # make install does this for you if .env is absent
docker compose up -d db       # Postgres 16 on localhost:5432
make migrate                  # alembic upgrade head
make seed                     # demo site "blog" + 3 posts + one capability token
make dev                      # serve the API on http://127.0.0.1:8000 (with reload)
```

Then open:

- http://127.0.0.1:8000/docs — interactive API reference (Scalar, generated
  from the OpenAPI 3.1 document)
- http://127.0.0.1:8000/healthz — liveness probe (no DB touch)
- http://127.0.0.1:8000/readyz — readiness probe (DB + optional deps)
- http://127.0.0.1:8000/openapi.json — the machine contract (must report openapi `3.1.0`)

## The docs

| File | What it covers |
| --- | --- |
| `docs/RUNNING.md` | Local dev end to end: prerequisites, `make` targets, env vars, migrations, the endpoints above |
| `docs/TESTING.md` | How the test suite runs, the ephemeral-Postgres strategy, and the CI-mirroring gate |
| `docs/DEPLOYING.md` | The real deployment: image build, compose (dev + prod), secrets, the pre-app migration step, health-gated rollout, rollback, and the GitHub Pages workflow |
| `docs/api.md` | Core content API v1 (posts, publish, revisions) |
| `docs/data-model.md` | PostgreSQL data model and migration policy |
| `docs/errors.md` | The `application/problem+json` error contract |
| `docs/ops/runbook.md` | Operational drills: backups/restore, deploy/rollback, request-id traces, alerts |
| `docs/ci/github-pages.yml` | The Pages publish workflow (static export → `gh-pages` branch) |

## Development loop

```bash
make test     # full pytest suite against a throwaway Postgres (docker → initdb fallback)
make lint     # ruff + mypy
make gate     # the CI gate: lint + test
make gate-migrations   # CI migrations gate — DESTRUCTIVE (wipes $DATABASE_URL)
make backup   # nightly encrypted pg_dump
make restore-drill     # restore the newest backup and diff content hashes
make deploy   # migration-gated rollout (compose.prod.yml); make rollback to undo
```

Every `make` target is documented in `make help`.

## Repository layout

```
app/          FastAPI application (api, db, domain, models, services, dashboard, observability)
alembic/      migrations (upgrade/downgrade both directions, alembic check clean)
scripts/      seed, backup/restore drill, static export, deploy, load test, CLI
tests/        pytest suite (Postgres-backed, ephemeral server per run)
compose.yml         dev stack: Postgres 16 (+ opt-in MinIO behind the storage profile)
compose.prod.yml    prod stack: db + one-shot migrate job + health-gated api
Dockerfile          multi-stage production image (python:3.11-slim runtime)
.github/workflows/ci.yml   CI: lint, migrations, test (red blocks merge)
docs/               this documentation set
```

---

### Nocturne — Design System Preview

"Nocturne" v1.0.0 — dark-mode design system for the chat app. Open `index.html`
(served at the Pages URL) for the self-contained living preview: full app mock
+ token swatches. No build step required.

Files:

- `index.html` — self-contained living preview (the Pages entry point)
- `nocturne.css` — drop-in CSS variables + `.btn/.bubble/.card/.avatar/.badge/.skeleton`
- `nocturne.tokens.json` — machine-readable tokens (W3C shape)
- `tailwind.config.js` — Tailwind v3 preset, 1:1 token mapping
- `DESIGN-SYSTEM.md` — full spec, rationale + component recipes