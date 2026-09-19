# Deploy to Render (Free Tier)

This guide takes you from `git clone` to a live HTTPS AgentCMS instance on Render
using the free tier (web service + PostgreSQL).

## Prerequisites

- A [Render account](https://render.com/)
- A GitHub/GitLab repository with your AgentCMS code
- A domain name (optional — Render provides `*.onrender.com` subdomains free)

## 1. Push to Git

```bash
git clone https://github.com/your-org/agentcms.git
cd agentcms
# Make any local config changes, then push to your repo
git push origin main
```

## 2. Create PostgreSQL Database

1. Go to Render Dashboard → **New +** → **PostgreSQL**
2. Configure:
   - **Name**: `agentcms-db`
   - **Database**: `agentcms`
   - **User**: `agentcms`
   - **Region**: Choose closest to your users
   - **Plan**: Free (1 GB storage, 90-day expiry on free tier)
3. Click **Create Database**
4. Wait for status "Available"
5. Note the **Internal Database URL** (looks like `postgresql://agentcms:pass@host:port/agentcms`)

## 3. Create Web Service

1. Go to Render Dashboard → **New +** → **Web Service**
2. Connect your Git repository
3. Configure:
   - **Name**: `agentcms-api`
   - **Region**: Same as database
   - **Branch**: `main`
   - **Runtime**: `Docker`
   - **Dockerfile Path**: `deploy/docker/Dockerfile`
   - **Plan**: Free (512 MB RAM, shared CPU, spins down after 15 min inactivity)

## 4. Configure Environment Variables

In the web service **Environment** tab, add:

### Required

| Key | Value |
|-----|-------|
| `APP_ENV` | `production` |
| `SECRET_KEY` | Generate with: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `DATABASE_URL` | Use the **Internal Database URL** from step 2, but change scheme to `postgresql+psycopg://` |
| `POSTGRES_PASSWORD` | Same as database password (for compose compatibility) |

### Optional (Recommended)

| Key | Value |
|-----|-------|
| `LOG_LEVEL` | `INFO` |
| `LOG_FORMAT` | `json` |
| `EMBED_ORIGINS` | `https://your-frontend.com` (your embedding site origin) |
| `METRICS_TOKEN` | Random string for `/metrics` auth |
| `OTLP_ENDPOINT` | Your OTLP collector (e.g., `https://api.honeycomb.io:443`) |

### Optional (Media Storage)

| Key | Value |
|-----|-------|
| `S3_ENDPOINT_URL` | `https://s3.us-east-1.amazonaws.com` |
| `S3_ACCESS_KEY_ID` | Your access key |
| `S3_SECRET_ACCESS_KEY` | Your secret key |
| `S3_BUCKET` | Your bucket name |
| `S3_REGION` | `us-east-1` |
| `S3_PUBLIC_BASE_URL` | `https://cdn.your-domain.com/agentcms-media` |

### Optional (Backups)

| Key | Value |
|-----|-------|
| `BACKUP_STORE_URL` | `s3://your-backup-bucket/agentcms` |
| `BACKUP_PASSPHRASE` | Strong passphrase for encryption |
| `BACKUP_RETENTION_DAILY` | `30` |
| `BACKUP_RETENTION_MONTHLY` | `12` |

## 5. Add a Pre-Deploy Command for Migrations

In the web service **Settings** tab, set **Pre-Deploy Command**:

```bash
python -m alembic upgrade head
```

This runs migrations before each deploy (idempotent — safe to run every time).

## 6. Deploy

Click **Create Web Service**. Render will:
1. Build the Docker image from `deploy/docker/Dockerfile`
2. Run the pre-deploy migration command
3. Start the API on port 8000
4. Provision HTTPS at `https://agentcms-api.onrender.com`

Wait for "Live" status (first deploy takes 5-10 minutes on free tier).

## 7. Verify Deployment

```bash
curl https://agentcms-api.onrender.com/healthz
```

Expected output:
```json
{
  "status": "ok",
  "service": "AgentCMS",
  "version": "0.3.0",
  "env": "production"
}
```

## 8. Configure Custom Domain (Optional)

1. In web service **Settings** → **Custom Domains** → **Add Domain**
2. Enter your domain (e.g., `cms.yourdomain.com`)
3. Add the CNAME record shown by Render to your DNS
4. Render provisions SSL automatically (Let's Encrypt)

## 9. Create a Site & Capability Token

Use the dashboard at `https://agentcms-api.onrender.com/dashboard` or API:

```bash
# Create admin token (one-time, save it!)
curl -X POST https://agentcms-api.onrender.com/v1/admin/tokens \
  -H "Content-Type: application/json" \
  -d '{"label": "admin", "scopes": ["posts:read", "posts:write", "posts:publish", "assets:write"]}'

# Use the returned token to create a site
curl -X POST https://agentcms-api.onrender.com/v1/sites \
  -H "Authorization: Bearer acms_..." \
  -H "Content-Type: application/json" \
  -d '{"slug": "blog", "name": "My Blog", "publish_mode": "auto"}'

# Create a capability link for embedding (read-only)
curl -X POST https://agentcms-api.onrender.com/v1/sites/blog/capability-links \
  -H "Authorization: Bearer acms_..." \
  -H "Content-Type: application/json" \
  -d '{"label": "embed", "verbs": ["posts:read"], "ttl_minutes": 525600}'
```

## 10. Embed in Your Site

Add the embed script to your frontend:

```html
<script
  src="https://cms.yourdomain.com/embed/v1/agentcms.js"
  data-site-token="cap_blog_xxx..."
  data-mount="#cms"
></script>
<div id="cms"></div>
```

## Free Tier Limitations

| Resource | Limit |
|----------|-------|
| Web Service RAM | 512 MB |
| Web Service CPU | Shared |
| Postgres Storage | 1 GB |
| Postgres Lifetime | 90 days (then auto-deleted on free tier) |
| Outbound Bandwidth | 100 GB/month |
| Build Minutes | 500/month |
| Auto-spin-down | After 15 min inactivity |

**Important**: Free PostgreSQL databases are deleted after 90 days. For production,
upgrade to a paid plan or use a managed Postgres (Neon, Supabase, RDS) with
`DATABASE_URL` pointing there.

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Build fails | Check build logs; ensure `deploy/docker/Dockerfile` path is correct |
| Migration fails | Check pre-deploy command logs; run manually via Render Shell |
| "Application not responding" | Free tier spins down; first request wakes it (30-60s cold start) |
| Embed script 403 | Ensure `EMBED_ORIGINS` includes your frontend origin exactly |
| Database connection error | Use **Internal Database URL**, not external; check scheme is `postgresql+psycopg://` |

## Next Steps

- [Configure backups](../configuration.md#backups--restore-drill) to persist beyond 90 days
- [Set up observability](../configuration.md#observability)
- [Read the embed guide](./embed.md) for theming and CSP configuration
- Consider upgrading to paid plan for production workloads