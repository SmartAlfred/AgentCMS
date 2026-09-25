# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.1] - 2026-09-25

### Fixed
- `scripts/deploy_smoke.sh` and `scripts/upgrade_smoke.sh` no longer call
  `POST /v1/sites` or `POST /v1/sites/{slug}/capability-links` -- neither route
  exists, so `make selfhost-verify` could never pass. The self-host E2E now
  provisions the demo site with the shipped `python -m scripts.seed` (the
  documented `make seed`) and hands the minted capability token to the smoke
  script (`--capability-token`). Creating a site / minting a link over the API is
  tracked in #44.
- `docs/deploy/quickstart.md` and `docs/deploy/embed.md` now document the path
  that actually exists (and quickstart's broken `deploy/vm/quickstart.md` link is
  fixed).

### Added
- `POST /v1/sites` (+ `GET /v1/sites`, `GET /v1/sites/{slug}`) — create a site over the API:
  the documented first-run step that never existed, and what turned the #37 self-host E2E job red (#45)
- Comprehensive test suite for version pinning and rollback logic
- Production deployment guard for AGENTCMS_IMAGE_TAG (immutable tags required)
- Self-host turnkey script (`scripts/selfhost.sh`) with env generation and validation
- Migration-gated deploy script (`scripts/deploy.sh`) with preflight and health checks
- Rollback capability (`scripts/rollback.sh`, `make rollback`)
- Nightly encrypted backup system (`scripts/backup.py`, `make backup`)
- Restore drill for backup verification (`scripts/restore_drill.py`, `make restore-drill`)
- OpenTelemetry tracing with OTLP export
- Prometheus metrics endpoint (`/metrics`)
- Capability links for agent-driven content creation
- Embed tokens for cross-origin content embedding
- S3-compatible object storage support (MinIO locally, S3/R2 in prod)
- Database audit event hash chain verification
- Docker Compose production stack with Caddy reverse proxy and automatic TLS
- GitHub Actions CI pipeline (lint, migrations, tests on Python 3.11/3.12)

### Changed
- Production compose stack now uses one-shot `migrate` job before API starts
- Health checks split: `/healthz` (liveness, no DB) and `/readyz` (readiness, full deps)
- Dependency lockfile (`requirements.lock.txt`) pinned and verified in CI
- Alembic migration policy: forward-only, no automatic downgrades
- Structured JSON logging default in production

### Fixed
- Race condition between simultaneous API and migrate container startup
- First-deploy failure when database service wasn't ensured before migration gate
- MinIO image pull from Docker Hub (switched to quay.io/minio/minio:latest)
- Locale-sensitive test failures in CI (scrubbed LANG/LC_ALL vars)

### Security
- Production refuses to boot with development default SECRET_KEY or DATABASE_URL
- AGENTCMS_IMAGE_TAG must be an immutable tag or digest in production
- CORS and embed origins require explicit configuration
- Metrics endpoint gated by shared secret (METRICS_TOKEN)

## [0.3.0] - 2026-09-25

### Added
- Initial public release of AgentCMS
- FastAPI-based REST API with OpenAPI 3.1 documentation
- Multi-site content management (sites, posts, tags, media)
- PostgreSQL 16 with Alembic migrations
- JWT-based authentication and capability tokens
- Agent-first design: capability links, embed tokens, static export, llms.txt

[Unreleased]: https://github.com/smartalfred/agentcms/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/smartalfred/agentcms/releases/tag/v0.3.1
[0.3.0]: https://github.com/smartalfred/agentcms/releases/tag/v0.3.0