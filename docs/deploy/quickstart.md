# Deploy AgentCMS in 5 Minutes

> **TL;DR**: `git clone → make selfhost → open HTTPS URL → grab capability token → embed in your site.`

This guide gets you a production-ready AgentCMS instance with HTTPS, a database,
automatic migrations, and an embed script — in about 5 minutes.

## Verified Deployment Path

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

---

## Docker Compose on a VM (Recommended Default)

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
#    (publish a post through a capability link -> fetch it from the public URL).
#    It needs an existing site and a capability token; the seed script creates
#    both in one step (the API can too now: POST /v1/sites, and POST /v1/admin/tokens
#    authenticated with the ADMIN_TOKEN bootstrap secret):
CAP=$(docker compose --env-file .env -f deploy/compose/docker-compose.prod.yml \
        exec -T api python -m scripts.seed | sed -n 's/.*Capability token: *//p')
make selfhost-verify CAPABILITY_TOKEN="$CAP"

# By hand instead? cp deploy/.env.example .env, edit SECRET_KEY, POSTGRES_PASSWORD
# and AGENTCMS_IMAGE_TAG (required in production), then:
#   docker compose --env-file .env -f deploy/compose/docker-compose.prod.yml up -d --build
# The explicit --env-file matters: compose reads .env from the directory of the
# compose file it is given, never from your current directory (#35).
#
# CI proves this path on every push by running `make selfhost-e2e` from a clean
# checkout: deploy -> migrate -> /healthz + /readyz -> seed the demo site ->
# publish a post via a capability link -> fetch the public URL. It is destructive (ends with `down -v`, wiping the
# database volume), so run it on a throwaway VM or a fresh clone.
```

### Pin a released image instead of building (optional)

`make selfhost` above builds from source and stays the documented default. Released
images are also published to GHCR (`linux/amd64` + `linux/arm64`), and are best pinned
by digest — a tag can be re-pointed at different bytes:

```bash
# AgentCMS v0.3.1 -- copy the `pin this` line from the release notes of the tag you want
AGENTCMS_IMAGE_TAG=ghcr.io/smartalfred/agentcms@sha256:c25db8216b7a61035980822838b7b89c46e6e07f9a34074694353569cc92dd85
docker compose --env-file .env -f deploy/compose/docker-compose.prod.yml up -d
docker compose --env-file .env -f deploy/compose/docker-compose.prod.yml run --rm migrate
```

Refresh the pin at each release; roll back by pinning the previous digest. See
[docs/DEPLOYING.md §9](../DEPLOYING.md) for the refresh/rollback procedure and the
current package-visibility caveat.

---

**Full guide**: [docs/DEPLOYING.md](../DEPLOYING.md)

---

## After Deploy: Create Content & Embed

### 1. Open the Dashboard

A fresh instance has **no site yet**. You can create the first one in two ways:

**Option A: Via the Dashboard (recommended)**
1. Open `https://your-domain.com/dashboard`
2. Sign in with a magic link (email)
3. Go to **Settings** → the "Create your first site" form appears
4. Fill in the slug, name, and optional base URL/publish mode
5. Click **Create site**

**Option B: Via the API**

```bash
curl -X POST https://your-domain.com/v1/sites \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"slug":"blog","name":"My Blog"}'
# -> {"id":"...","slug":"blog","name":"My Blog","publish_mode":"auto","created_at":"..."}
```

Mint `YOUR_ADMIN_TOKEN` with `POST /v1/admin/tokens`, authenticated with the
`ADMIN_TOKEN` bootstrap secret that `scripts/selfhost.sh` generated into your `.env`:

```bash
curl -X POST https://your-domain.com/v1/admin/tokens \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"label":"admin","scopes":["*:read"]}'
# -> {"token":"acms_...","label":"admin",...}
```

Every `/v1/admin/*` route is closed to anonymous callers (#44). `ADMIN_TOKEN` is a
bootstrap secret, not a stored token — it lives in `.env`, is never shown in the
dashboard, and can be rotated any time from
[configuration.md](configuration.md#admin-surface).

Then open `https://your-domain.com/dashboard`; it now shows your site.

### 2. Create a Capability Link for Embedding

The embed script needs a **read-only** capability token (`posts:read` scope).

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

### 3. Embed in Your Site

Add **one script tag** to any page — no build step, no npm, no framework:

```html
<script
  src="https://your-domain.com/embed/v1/agentcms.js"
  data-site-token="cap_blog_abc123..."
  data-mount="#cms"
></script>
<div id="cms"></div>
```

That's it. Your published posts now render inside `#cms`.

### 4. Customize Appearance (Optional)

The embed uses CSS custom properties — override them to match your site:

```css
:root {
  --agentcms-color-primary: #your-brand-color;
  --agentcms-color-bg: #your-bg;
  --agentcms-color-text: #your-text;
  --agentcms-radius: 0.5rem;
  --agentcms-font-sans: system-ui, sans-serif;
}
```

See [embed.md](deploy/embed.md) for full theming, CSP, iframe fallback, and
framework-specific snippets (WordPress, Next.js, Astro).

---

## What You Get

| Feature | Status |
|---------|--------|
| HTTPS (auto Let's Encrypt or self-signed) | ✅ |
| PostgreSQL 16 with named volume | ✅ |
| Migrations run on boot | ✅ |
| Health/readiness probes | ✅ |
| Fail-fast config validation | ✅ |
| Embed script (vanilla JS, no deps) | ✅ |
| Origin allowlist (CORS) | ✅ |
| Read-only embed tokens | ✅ |
| CSS theming via custom properties | ✅ |
| Scheduled backups + restore drill | ✅ |

---

## Troubleshooting Quick Reference

| Symptom | Check |
|---------|-------|
| `SECRET_KEY` error on boot | Must be ≥32 chars, not dev default |
| `DATABASE_URL` error on boot | Must not be `localhost` in production |
| Caddy: "no certificate" | DNS not propagated, or ports 80/443 blocked |
| Embed: blank/403 | `EMBED_ORIGINS` must include your frontend origin exactly |
| Embed: write token rejected | Embed tokens must be `posts:read` only |
| Migrations not applied | Check `migrate` service logs: `docker compose logs migrate` |

---

## Next Steps

- [Configuration Reference](deploy/configuration.md) — Every env var, secrets, validation
- [Embed Guide](deploy/embed.md) — Theming, CSP, iframe, WordPress/Next.js/Astro snippets
- [Operations Runbook](../ops/runbook.md) — Backups, scaling, incidents
- [API Reference](../api.md) — Full OpenAPI spec at `/openapi.json`

---

## Local Development (No Domain)

For local testing without a domain, the stack works with self-signed certs:

```bash
cp deploy/.env.example .env
# Edit .env: DOMAIN=localhost (default), CADDY_EMAIL= (empty)
docker compose --env-file .env -f deploy/compose/docker-compose.prod.yml up -d --build
# Open https://localhost (browser will warn about self-signed cert)
```

The embed script works on localhost too — just set `EMBED_ORIGINS=http://localhost:3000` (or your dev server origin).