# Deploy AgentCMS in 5 Minutes

> **TL;DR**: `git clone → make selfhost → open HTTPS URL → grab capability token → embed in your site.`

This guide gets you a production-ready AgentCMS instance with HTTPS, a database,
automatic migrations, and an embed script — in about 5 minutes.

## Choose Your Path

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

---

## Path 1: Docker Compose on a VM (Recommended Default)

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

**Full guide**: [deploy/vm/quickstart.md](deploy/vm/quickstart.md)

---

## Path 2: Fly.io (Free Tier)

**Prerequisites**: `flyctl` installed, Fly account.

```bash
# 1. Clone
git clone https://github.com/SmartAlfred/AgentCMS.git
cd AgentCMS

# 2. Create apps & volumes
fly volumes create pgdata --size 3 --region ord --app agentcms-db
fly apps create agentcms-db
fly apps create agentcms-api

# 3. Set secrets (API app)
fly secrets set SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" --app agentcms-api
# Required: with APP_ENV=production the app refuses to boot without an immutable tag
fly secrets set AGENTCMS_IMAGE_TAG="agentcms:v0.3.0" --app agentcms-api
fly secrets set POSTGRES_PASSWORD="your-db-password" --app agentcms-api
fly secrets set DATABASE_URL="postgresql+psycopg://agentcms:your-db-password@agentcms-db.internal:5432/agentcms" --app agentcms-api
fly secrets set EMBED_ORIGINS="https://your-frontend.com" --app agentcms-api

# 4. Set secrets (DB app)
fly secrets set POSTGRES_PASSWORD="your-db-password" --app agentcms-db

# 5. Deploy DB
fly deploy -c deploy/fly/fly.db.toml --app agentcms-db

# 6. Run migrations
docker build -t agentcms-migrate -f deploy/docker/Dockerfile .
docker run --rm -e DATABASE_URL="postgresql+psycopg://agentcms:your-db-password@agentcms-db.internal:5432/agentcms" -e SECRET_KEY="..." -e APP_ENV=production agentcms-migrate python -m alembic upgrade head

# 7. Deploy API
fly deploy -c deploy/fly/fly.api.toml --app agentcms-api

# 8. Verify
curl https://agentcms-api.fly.dev/healthz
```

**Full guide**: [deploy/fly/quickstart.md](deploy/fly/quickstart.md)

---

## Path 3: Render (Free Tier)

**Prerequisites**: Render account, GitHub repo.

1. Push code to GitHub
2. Create PostgreSQL database on Render (Free)
3. Create Web Service → Docker → `deploy/docker/Dockerfile`
4. Add environment variables (see guide)
5. Set Pre-Deploy Command: `python -m alembic upgrade head`
6. Deploy → Live at `https://your-app.onrender.com`

**Full guide**: [deploy/render/quickstart.md](deploy/render/quickstart.md)

---

## After Deploy: Create Content & Embed

### 1. Open the Dashboard

Visit `https://your-domain.com/dashboard` — create an admin token, then a site.

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