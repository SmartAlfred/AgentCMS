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

### Choose Your Path

| Platform | Time | Cost | Best For |
|----------|------|------|----------|
| **Docker Compose (VM/VPS)** | ~5 min | $4-10/mo | Full control, any cloud, bare metal |
| **Fly.io** | ~10 min | Free tier available | Global edge, auto-scaling, custom domains free |
| **Render** | ~10 min | Free tier available | Git-native, zero config, auto-deploys |
| **Railway** | ~10 min | Free trial | Simplicity, built-in Postgres |

> **All paths use the exact same Docker image and compose stack.** The only
> difference is how the container runs and how TLS is terminated.

> **Verified at this revision:** only **Path 1 (Docker Compose)** is proven
> end-to-end. Paths 2–4 are written from intent, not from a run: their platform
> manifests are missing and they never set `AGENTCMS_IMAGE_TAG`, so they cannot
> boot as written — see #36. Compose-path gaps: #35, #37, #38, #39.

### Path 1: Docker Compose on a VM (Recommended Default)

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
# {"status":"ok","service":"AgentCMS","version":"0.3.0","env":"production"}

# By hand instead? cp deploy/.env.example .env, edit SECRET_KEY, POSTGRES_PASSWORD
# and AGENTCMS_IMAGE_TAG (required in production), then:
#   docker compose --env-file .env -f deploy/compose/docker-compose.prod.yml up -d --build --build
# The explicit --env-file matters: compose reads .env from the directory of the
# compose file it is given, never from your current directory (#35).
```

**Full guide**: [docs/deploy/quickstart.md#path-1-docker-compose-on-a-vm](docs/deploy/quickstart.md#path-1-docker-compose-on-a-vm)

### Path 2: Fly.io (Free Tier)

```bash
# Prerequisites: flyctl installed, Fly account
git clone https://github.com/SmartAlfred/AgentCMS.git
cd AgentCMS

# Create apps & volumes
fly volumes create pgdata --size 3 --region ord --app agentcms-db
fly apps create agentcms-db
fly apps create agentcms-api

# Set secrets (API app)
fly secrets set SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" --app agentcms-api
fly secrets set POSTGRES_PASSWORD="your-db-password" --app agentcms-api
fly secrets set DATABASE_URL="postgresql+psycopg://agentcms:your-db-password@agentcms-db.internal:5432/agentcms" --app agentcms-api
fly secrets set EMBED_ORIGINS="https://your-frontend.com" --app agentcms-api

# Deploy DB, run migrations, deploy API
fly deploy -c deploy/fly/fly.db.toml --app agentcms-db
# ... (see full guide for migration step)
fly deploy -c deploy/fly/fly.api.toml --app agentcms-api
```

**Full guide**: [docs/deploy/quickstart.md#path-2-flyio-free-tier](docs/deploy/quickstart.md#path-2-flyio-free-tier)

### Path 3: Render (Free Tier)

1. Push code to GitHub
2. Create PostgreSQL database on Render (Free)
3. Create Web Service → Docker → `deploy/docker/Dockerfile`
4. Add environment variables (see guide)
5. Set Pre-Deploy Command: `python -m alembic upgrade head`
6. Deploy → Live at `https://your-app.onrender.com`

**Full guide**: [docs/deploy/quickstart.md#path-3-render-free-tier](docs/deploy/quickstart.md#path-3-render-free-tier)

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
| `docs/deploy/quickstart.md` | **Start here** — clone to HTTPS in 5 minutes (VM, Fly, Render) |
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