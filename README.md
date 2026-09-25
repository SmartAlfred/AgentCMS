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

---

## Deploy it in 5 minutes

> **TL;DR**: `git clone → make selfhost → open HTTPS URL → grab capability token → embed in your site.`

### Verified Deployment Path

Only **Docker Compose on a VM/VPS** is verified end-to-end at this revision.

| Platform | Status | Time | Cost | Best For |
|----------|--------|------|------|----------|
| **Docker Compose (VM/VPS)** | ✅ Verified | ~5 min | $4-10/mo | Full control, any cloud, bare metal |
| Fly.io | ❌ Unverified | — | — | Platform manifests missing; see #36 |
| Render | ❌ Unverified | — | — | Platform manifests missing; see #36 |
| Railway | ❌ Unverified | — | — | No documentation; see #36 |

> **Paths 2–4 (Fly.io, Render, Railway) are not verified.** Their platform
> manifests (`fly.toml`, `render.yaml`, `railway.json`) do not exist in this
> repository, and they never set `AGENTCMS_IMAGE_TAG` (required in production),
> so they cannot boot as written. See GitHub issue #36 for tracking.

### Docker Compose on a VM (Recommended Default)

**Prerequisites**: A Linux VM (Ubuntu 22.04+/24.04) with Docker installed, a domain pointed at it.

```bash
# 1. Clone
git clone https://github.com/SmartAlfred/AgentCMS.git
cd AgentCMS

# 2. Configure + deploy in one step: writes .env with generated secrets,
#    validates the stack, builds the image and starts it (safe to re-run)
make selfhost DOMAIN=your-domain.com CADDY_EMAIL=you@example.com

# 3. Verify
curl https://your-domain.com/healthz
# {"status":"ok","service":"AgentCMS","version":"0.3.1","env":"production"}

# 4. Prove it serves content: liveness, readiness and a real roundtrip
#    (create a site -> publish a post -> fetch it from the public URL).
#    Non-destructive: safe to run on a live host.
make selfhost-verify

# By hand instead? cp deploy/.env.example .env, edit SECRET_KEY, POSTGRES_PASSWORD
# and AGENTCMS_IMAGE_TAG (required in production), then:
#   docker compose --env-file .env -f deploy/compose/docker-compose.prod.yml up -d --build
# The explicit --env-file matters: compose reads .env from the directory of the
# compose file it is given, never from your current directory (#35).
#
# CI proves this path on every push by running `make selfhost-e2e` from a clean
# checkout: deploy -> migrate -> /healthz + /readyz -> create a site -> publish a
# post -> fetch the public URL. It is destructive (ends with `down -v`, wiping the
# database volume), so run it on a throwaway VM or a fresh clone.
```

**Full guide**: [docs/deploy/quickstart.md#docker-compose-on-a-vm-recommended-default](docs/deploy/quickstart.md#docker-compose-on-a-vm-recommended-default)

---

### After Deploy: Create Content & Embed

1. **Open the Dashboard** — Visit `https://your-domain.com/dashboard` — create an admin token, then a site.

2. **Create a Capability Link for Embedding** — The embed script needs a **read-only** capability token (`posts:read` scope).

   Via Dashboard:
   1. Go to your site → Capability Links → Create Link
   2. Label: `embed`, Verbs: `posts:read`, TTL: 1 year (525600 minutes)
   3. Copy the `cap_blog_xxx...` token

   Via API:
   ```bash
   curl -X POST https://your-domain.com/v1/sites/blog/capability-links \
     -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"label":"embed","verbs":["posts:read"],"ttl_minutes":525600}'
   ```

3. **Embed in Your Site** — Add **one script tag** to any page — no build step, no npm, no framework:

   ```html
   <script
     src="https://your-domain.com/embed/v1/agentcms.js"
     data-site-token="cap_blog_abc123..."
     data-mount="#cms"
   ></script>
   <div id="cms"></div>
   ```

   That's it. Your published posts now render inside `#cms`.

4. **Customize Appearance (Optional)** — The embed uses CSS custom properties — override them to match your site:

   ```css
   :root {
     --agentcms-color-primary: #your-brand-color;
     --agentcms-color-bg: #your-bg;
     --agentcms-color-text: #your-text;
     --agentcms-radius: 0.5rem;
     --agentcms-font-sans: system-ui, sans-serif;
   }
   ```

   See [docs/deploy/embed.md](docs/deploy/embed.md) for full theming, CSP, iframe fallback, and framework-specific snippets (WordPress, Next.js, Astro).

---

## The docs

| File | What it covers |
| --- | --- |
| `docs/deploy/quickstart.md` | **Start here** — clone to HTTPS in 5 minutes (VM only; Fly/Render unverified) |
| `docs/deploy/configuration.md` | Every env var, secrets, fail-fast validation |
| `docs/deploy/embed.md` | Drop-in embedding: theming, CSP, iframe, WordPress/Next.js/Astro |
| `docs/RUNNING.md` | Local dev end to end: prerequisites, `make` targets, env vars, migrations |
| `docs/TESTING.md` | How the test suite runs, ephemeral-Postgres strategy, CI gate |
| `docs/DEPLOYING.md` | Image build, compose (dev + prod), secrets, migration step, health-gated rollout, rollback |
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