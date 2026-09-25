# Deploy to Fly.io (Free Tier) — **UNVERIFIED**

> ⚠️ **This guide is UNVERIFIED and NOT RECOMMENDED for production use.**
>
> The platform manifests (`fly.db.toml`, `fly.api.toml`) referenced in this guide
> **do not exist** in the repository. The guide also does not set `AGENTCMS_IMAGE_TAG`
> (required in production), so it cannot boot as written.
>
> Only **Docker Compose on a VM/VPS** is verified end-to-end. See
> [docs/deploy/quickstart.md](../../docs/deploy/quickstart.md) for the verified path.
>
> Tracking issue: [#36](https://github.com/SmartAlfred/AgentCMS/issues/36)

This guide takes you from `git clone` to a live HTTPS AgentCMS instance on Fly.io
using the free tier (shared CPU, 256 MB RAM, 3 GB persistent volume).

## Prerequisites

- [flyctl](https://fly.io/docs/hands-on/install-flyctl/) installed and authenticated (`fly auth login`)
- A domain name (optional — Fly provides `*.fly.dev` subdomains free)
- Docker installed locally (for building the image)

## 1. Clone & Prepare

```bash
git clone https://github.com/your-org/agentcms.git
cd agentcms
```

## 2. Create Fly Apps

Create two apps: one for the database, one for the API.

```bash
# Create a volume for Postgres data (3 GB, free tier)
fly volumes create pgdata --size 3 --region ord --app agentcms-db

# Create the database app (Postgres)
fly apps create agentcms-db --org personal

# Create the API app
fly apps create agentcms-api --org personal
```

## 3. Configure Secrets

Generate a strong secret key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Set secrets on the API app:

```bash
# Required
fly secrets set SECRET_KEY="your-generated-secret-key" --app agentcms-api
fly secrets set POSTGRES_PASSWORD="your-db-password" --app agentcms-api
fly secrets set DATABASE_URL="postgresql+psycopg://agentcms:your-db-password@agentcms-db.internal:5432/agentcms" --app agentcms-api

# Optional: embed configuration (set to your frontend domain)
fly secrets set EMBED_ORIGINS="https://your-frontend.com" --app agentcms-api

# Optional: observability
fly secrets set METRICS_TOKEN="your-metrics-token" --app agentcms-api
fly secrets set OTLP_ENDPOINT="https://your-otlp-endpoint" --app agentcms-api

# Optional: S3 media storage
fly secrets set S3_ENDPOINT_URL="https://s3.us-east-1.amazonaws.com" --app agentcms-api
fly secrets set S3_ACCESS_KEY_ID="your-key" --app agentcms-api
fly secrets set S3_SECRET_ACCESS_KEY="your-secret" --app agentcms-api
fly secrets set S3_BUCKET="your-bucket" --app agentcms-api
fly secrets set S3_PUBLIC_BASE_URL="https://cdn.your-domain.com/agentcms-media" --app agentcms-api
```

Set secrets on the DB app:

```bash
fly secrets set POSTGRES_PASSWORD="your-db-password" --app agentcms-db
fly secrets set POSTGRES_USER="agentcms" --app agentcms-db
fly secrets set POSTGRES_DB="agentcms" --app agentcms-db
```

## 4. Deploy Postgres

Create `fly.toml` for the database:

```toml
# fly.db.toml
app = "agentcms-db"
primary_region = "ord"

[build]
  image = "postgres:16-alpine"

[env]
  POSTGRES_USER = "agentcms"
  POSTGRES_DB = "agentcms"

[mounts]
  source = "pgdata"
  destination = "/var/lib/postgresql/data"

[processes]
  app = "postgres"

[http_service]
  internal_port = 5432
  force_https = false
  auto_stop_machines = false
  auto_start_machines = true
  min_machines_running = 1
```

Deploy:

```bash
fly deploy -c fly.db.toml --app agentcms-db
```

Wait for the database to be healthy:

```bash
fly ssh console --app agentcms-db -C "pg_isready -U agentcms -d agentcms"
```

## 5. Run Migrations

Before deploying the API, run migrations against the Fly Postgres:

```bash
# Build the image locally
docker build -t agentcms-migrate -f deploy/docker/Dockerfile .

# Run migrations using the local image against Fly DB
docker run --rm \
  -e DATABASE_URL="postgresql+psycopg://agentcms:your-db-password@agentcms-db.internal:5432/agentcms" \
  -e SECRET_KEY="your-generated-secret-key" \
  -e APP_ENV=production \
  agentcms-migrate python -m alembic upgrade head
```

Or use Fly's remote builder:

```bash
fly ssh console --app agentcms-api -C "python -m alembic upgrade head" --build-only
```

## 6. Deploy API

Create `fly.toml` for the API:

```toml
# fly.api.toml
app = "agentcms-api"
primary_region = "ord"

[build]
  dockerfile = "deploy/docker/Dockerfile"

[env]
  APP_ENV = "production"
  HOST = "0.0.0.0"
  PORT = "8080"
  LOG_LEVEL = "INFO"
  LOG_FORMAT = "json"

[processes]
  app = "uvicorn app.main:app --host 0.0.0.0 --port 8080 --proxy-headers"

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = true
  auto_start_machines = true
  min_machines_running = 0
  max_machines_running = 2

[[http_service.checks]]
  interval = "10s"
  timeout = "3s"
  grace_period = "15s"
  method = "GET"
  path = "/healthz"
```

Deploy:

```bash
fly deploy -c fly.api.toml --app agentcms-api
```

## 7. Configure Custom Domain (Optional)

If you have a domain:

```bash
fly certs create cms.yourdomain.com --app agentcms-api
# Add CNAME record: cms.yourdomain.com -> agentcms-api.fly.dev
fly certs check cms.yourdomain.com --app agentcms-api
```

## 8. Verify Deployment

```bash
# Health check
curl https://agentcms-api.fly.dev/healthz

# Or with custom domain
curl https://cms.yourdomain.com/healthz
```

Expected output:
```json
{
  "status": "ok",
  "service": "AgentCMS",
  "version": "0.3.1",
  "env": "production"
}
```

## 9. Create a Site & Capability Token

```bash
# Create a site via API (requires admin token)
# First, create an admin token via the dashboard or API

# Or use the MCP server / dashboard to create a site and capability link
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

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `fly deploy` fails with "no space left" | Use `--remote-only` to build on Fly's builders |
| Database connection refused | Check `fly status --app agentcms-db` and ensure volume is mounted |
| Migrations fail | Run `fly ssh console --app agentcms-api -C "alembic upgrade head"` |
| HTTPS not working | Check `fly certs list --app agentcms-api` and DNS propagation |
| Embed script blocked | Ensure `EMBED_ORIGINS` includes your frontend origin |

## Free Tier Limits

- 1 shared CPU, 256 MB RAM per app
- 3 GB persistent volume (Postgres)
- 160 GB outbound data/month
- Automatic SSL on `*.fly.dev` or custom domains
- Apps sleep after 1 hour of inactivity (wakes on request)

## Next Steps

- [Configure backups](../configuration.md#backups--restore-drill) to S3/R2
- [Set up observability](../configuration.md#observability) with OTLP
- [Read the embed guide](./embed.md) for theming and CSP configuration