# Deploy to a Bare Ubuntu VM (Escape Hatch)

This guide takes you from a fresh Ubuntu 22.04/24.04 VM to a live HTTPS
AgentCMS instance using the same `docker compose` stack. Use this when PaaS
options (Fly, Render, Railway) aren't acceptable.

## Prerequisites

- A VM with **Ubuntu 22.04 or 24.04** (2 GB RAM minimum, 4 GB recommended)
- **Root or sudo access**
- A domain name pointed at the VM's public IP (for Let's Encrypt)
  - For local testing without a domain, use `localhost` (self-signed certs)
- **Docker** and **Docker Compose v2** installed (script below handles this)

## 1. Provision the VM

### Cloud Provider Examples

**DigitalOcean / Linode / Vultr / Hetzner:**
- Create a 2 GB RAM / 1 vCPU droplet/instance
- Ubuntu 24.04 LTS
- Add your SSH key
- Note the public IP

**AWS EC2:**
```bash
# t3.small (2 GB RAM), Ubuntu 24.04 AMI, security group: 22, 80, 443
aws ec2 run-instances --image-id ubuntu-24.04 --instance-type t3.small --key-name your-key --security-group-ids sg-xxx
```

**Local VM (VirtualBox/VMware/UTM):**
- 2+ GB RAM, 20+ GB disk, bridged networking

### DNS Setup

Point your domain at the VM:
```
A     cms.example.com    → <VM_PUBLIC_IP>
AAAA  cms.example.com    → <VM_IPv6_IF_AVAILABLE>
```

Wait for DNS propagation (`dig cms.example.com`).

## 2. One-Command Bootstrap

Run this on the VM as a user with `sudo` access:

```bash
curl -fsSL https://raw.githubusercontent.com/your-org/agentcms/main/scripts/bootstrap_vm.sh | bash -s -- --domain cms.example.com --email admin@example.com
```

Or manually (see script contents below):

```bash
# 1. Install Docker & Compose
sudo apt-get update && sudo apt-get install -y ca-certificates curl gnupg lsb-release
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# 2. Clone repo
git clone https://github.com/SmartAlfred/AgentCMS.git /opt/agentcms
cd /opt/agentcms

# 3. Configure environment
cp deploy/.env.example .env
# Edit .env with your values (see step 4)

# 4. Deploy
docker compose --env-file .env -f deploy/compose/docker-compose.prod.yml up -d --build
```

## 3. Configure Environment (`.env`)

Edit `/opt/agentcms/.env` with your values:

```bash
# Required
APP_ENV=production
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
POSTGRES_PASSWORD=$(openssl rand -base64 32)
DOMAIN=cms.example.com
CADDY_EMAIL=admin@example.com

# Optional: Embed configuration (your frontend origin)
EMBED_ORIGINS=https://www.example.com,https://example.com

# Optional: Backups to local filesystem (or s3://...)
BACKUP_STORE_URL=file:///opt/agentcms/backups
BACKUP_PASSPHRASE=$(openssl rand -base64 32)
BACKUP_DIR=/opt/agentcms/backups

# Optional: Observability
METRICS_TOKEN=$(openssl rand -base64 32)
```

**Generate all secrets at once:**

```bash
cd /opt/agentcms
cat > .env <<'EOF'
APP_ENV=production
APP_NAME=AgentCMS
APP_VERSION=0.3.1
LOG_LEVEL=INFO
LOG_FORMAT=json
DOCS_ENABLED=true
HOST=0.0.0.0
PORT=8000
CORS_ORIGINS=
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
DEBUG=false
DATABASE_URL=postgresql+psycopg://agentcms:${POSTGRES_PASSWORD}@db:5432/agentcms
POSTGRES_USER=agentcms
POSTGRES_PASSWORD=$(openssl rand -base64 32)
POSTGRES_DB=agentcms
DB_POOL_SIZE=5
DB_POOL_TIMEOUT=5
DB_ECHO=false
DEFAULT_SITE_SLUG=blog
DEFAULT_PUBLISH_MODE=auto
IDEMPOTENCY_RETENTION_HOURS=24
CAPABILITY_DEFAULT_TTL_MINUTES=60
CAPABILITY_MAX_TTL_DAYS=30
CAPABILITY_RATE_LIMIT_PER_LINK=30
CAPABILITY_RATE_LIMIT_WINDOW_SECONDS=60
CAPABILITY_MAX_LIVE_LINKS_PER_SITE=100
METRICS_TOKEN=$(openssl rand -base64 32)
OTLP_ENDPOINT=
OTEL_SERVICE_NAME=agentcms
TRACE_SAMPLE_RATIO=0.1
GIT_SHA=
BUILD_TIME=
BACKUP_STORE_URL=file:///opt/agentcms/backups
BACKUP_DIR=/opt/agentcms/backups
BACKUP_PASSPHRASE=$(openssl rand -base64 32)
BACKUP_RETENTION_DAILY=30
BACKUP_RETENTION_MONTHLY=12
BACKUP_SOURCE_URL=
DOMAIN=cms.example.com
CADDY_EMAIL=admin@example.com
EMBED_ORIGINS=https://www.example.com,https://example.com
EMBED_TOKEN_SCOPE=posts:read
EOF
```

## 4. Deploy

```bash
cd /opt/agentcms
docker compose --env-file .env -f deploy/compose/docker-compose.prod.yml up -d --build
```

Watch the logs:

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.prod.yml logs -f
```

Wait for:
```
caddy-1  | {"level":"info","msg":"using provided configuration","config_file":"/etc/caddy/Caddyfile"}
caddy-1  | {"level":"info","msg":"automatic TLS certificate management","domains":["cms.example.com"]}
api-1    | {"level":"info","msg":"Application startup complete"}
```

## 5. Verify Deployment

```bash
# Health check (via Caddy on 443)
curl -k https://cms.example.com/healthz

# Or locally on VM (bypassing Caddy)
curl http://127.0.0.1:8000/healthz
```

Expected:
```json
{"status":"ok","service":"AgentCMS","version":"0.3.1","env":"production"}
```

## 6. Run Migrations (if not auto-run)

The `migrate` service runs automatically on first deploy. If you need to run manually:

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.prod.yml run --rm migrate
```

## 7. Create a Site & Capability Token

### Option A: Via Dashboard (Recommended)

1. Open `https://cms.example.com/dashboard`
2. Create admin token (save it!)
3. Create a site (slug: `blog`)
4. Create capability link with `posts:read` verb for embedding

### Option B: Via API

```bash
# Mint an admin token (one-time).  POST /v1/admin/tokens is authenticated by the
# ADMIN_TOKEN bootstrap secret from .env (export it: set -a; . ./.env; set +a).
ADMIN_TOKEN=$(curl -s -X POST https://cms.example.com/v1/admin/tokens \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"label":"bootstrap","scopes":["posts:read","posts:write","posts:publish","assets:write"]}' | jq -r .token)

# Create site
curl -X POST https://cms.example.com/v1/sites \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"slug":"blog","name":"My Blog","publish_mode":"auto"}'

# Mint the read-only embed token. No HTTP route mints a cap_... capability link
# yet, so run the seeder on the VM -- it prints the embed token for the demo site
# `blog` (the ADMIN_TOKEN token above carries posts:write and is rejected by
# /embed/v1/posts).
docker compose --env-file .env -f deploy/compose/docker-compose.prod.yml \
  exec -T api python -m scripts.seed
#   Embed token (read-only): cap_blog_yyyyyyyy
```

## 8. Embed in Your Site

Add to your frontend HTML:

```html
<script
  src="https://cms.example.com/embed/v1/agentcms.js"
  data-site-token="cap_blog_xxx..."
  data-mount="#cms"
></script>
<div id="cms"></div>
```

## 9. Automate Updates (Systemd)

Create `/etc/systemd/system/agentcms.service`:

```ini
[Unit]
Description=AgentCMS
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/agentcms
ExecStart=/usr/bin/docker compose --env-file .env -f deploy/compose/docker-compose.prod.yml up -d --build
ExecStop=/usr/bin/docker compose --env-file .env -f deploy/compose/docker-compose.prod.yml down
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now agentcms
```

## 10. Scheduled Backups (Cron)

Add to root's crontab (`sudo crontab -e`):

```bash
# Daily backup at 03:00
0 3 * * * cd /opt/agentcms && docker compose --env-file .env -f deploy/compose/docker-compose.prod.yml exec -T api python scripts/backup.py >> /var/log/agentcms-backup.log 2>&1

# Monthly restore drill on 1st at 04:00
0 4 1 * * cd /opt/agentcms && docker compose --env-file .env -f deploy/compose/docker-compose.prod.yml exec -T api python scripts/restore_drill.py >> /var/log/agentcms-restore-drill.log 2>&1
```

## 11. Log Rotation

Create `/etc/logrotate.d/agentcms`:

```
/var/log/agentcms-*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    create 640 root root
}
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `docker compose` fails | Ensure Docker Compose v2: `docker compose version` |
| Caddy fails to get cert | Check DNS propagation; ensure ports 80/443 open on firewall/cloud SG |
| `SECRET_KEY` error | Regenerate: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| Database connection refused | Check `docker compose logs db`; ensure volume permissions |
| Embed script blocked | Verify `EMBED_ORIGINS` in `.env` matches your frontend origin exactly |
| Permission denied on volumes | Run `sudo chown -R 10001:10001 /opt/agentcms` (UID from Dockerfile) |

## Firewall (UFW)

```bash
sudo ufw allow 22/tcp   # SSH
sudo ufw allow 80/tcp   # HTTP (ACME challenge)
sudo ufw allow 443/tcp  # HTTPS
sudo ufw enable
```

## Monitoring

- **Health**: `curl https://cms.example.com/healthz`
- **Readiness**: `curl https://cms.example.com/readyz`
- **Metrics**: `curl -H "X-Metrics-Token: $METRICS_TOKEN" https://cms.example.com/metrics` — behind Caddy the app is *not* loopback, so the token is what opens `/metrics` (the app never trusts a forwarded `X-Forwarded-For`; see [configuration.md](../configuration.md#metrics-behind-a-proxy))
- **Status page**: `https://cms.example.com/status`

## Next Steps

- [Configure backups](../configuration.md#backups--restore-drill) to S3 for durability
- [Set up observability](../configuration.md#observability) with OTLP
- [Read the embed guide](./embed.md) for theming and CSP configuration
- Consider [Watchtower](https://containrrr.dev/watchtower/) for auto-updates

## Security Hardening (Recommended)

- Disable SSH password auth: `PasswordAuthentication no` in `/etc/ssh/sshd_config`
- Use SSH keys only
- Set up fail2ban: `sudo apt install fail2ban`
- Regular `apt update && apt upgrade`
- Monitor `docker compose logs` for anomalies