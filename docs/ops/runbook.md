# AgentCMS Operational Runbook (#24)

Six drills. Every section lists a trigger, the symptom you will actually see,
and a step-by-step recovery that relies only on committed tooling in this
repository.

OPS cheat-sheet:

| Task | Command |
| --- | --- |
| Structured request logs (JSON) | `docker compose logs -f api` |
| Readiness / dependency report | `curl -fsS localhost:8000/readyz` |
| All status checks, 200-always | `curl -fsS localhost:8000/status` |
| Version + migration head | `curl -fsS localhost:8000/v1/version` |
| Metrics scrape | `curl -fsS -H 'X-Metrics-Token: $METRICS_TOKEN' localhost:8000/metrics` |
| Alerts (PromQL, committed) | `docs/ops/prometheus-rules.yml` |
| Grafana dashboard | Import `docs/ops/grafana-dashboard.json` |
| Nightly backup | `make backup` (cron: `30 2 * * *` in the deploy host) |
| Restore drill | `make restore-drill` |
| Migration-gated deploy | `./scripts/deploy.sh` |
| One-command rollback | `./scripts/deploy.sh --rollback` |
| Version-pinned upgrade | `./scripts/upgrade.sh <NEW_TAG>` |
| Tested rollback | `./scripts/rollback.sh` |

---

## 1. Secret-in-logs drill (#6 + #24)

**Trigger:** someone reports a token (or their redacted prefix) in a log stream.

**What you'd see:** `grep -n "acms_"` in a log file returns nothing, because the
request path *never* emits tokens and the redaction filter masks any that reach
`logging`; a correctly working system shows only `acms_abcde…***` markers.

**Drill steps:**

1. Reproduce the spike:
   ```bash
   curl -fsS -H 'Authorization: Bearer acms_0123456789abcdef_abcdefghijklmnopqrstuvwxyz123456' localhost:8000/healthz > /dev/null
   ```
2. Confirm the raw token is absent and a redacted marker is present:
   ```bash
   docker compose logs --since 1m | grep -c 'acms_0123456789abcdef'   # -> 0
   docker compose logs --since 1m | grep 'request complete' | head -1  # actor fields, no token
   ```
3. If a leak is found, it must be a call site that bypassed logging filters
   (e.g. `print`, a raw `stderr` write, or a third-party lib). Grep for those:
   ```bash
   grep -rn "print(" app/ || true
   ```
4. The committed guard is `tests/test_secrets_in_logs.py` (both token families
   through the real JSON formatter + middleware) and the root-logger filter in
   `app/logging.py`. Fix at the source, then re-run:
   ```bash
   make test
   ```

**Recovery:** rotate the leaked token (`capability_links` re-issue / new
`acms_` token via `/v1/admin/tokens`), then follow step 3 to find the bypass.
Escalation: on-call page if the leaked token scopes include `posts:publish`.

---

## 2. Alert / profiling drill (#24)

**Trigger:** pager fires `HighErrorRatio5xx`, `OutboxBacklog`, `BackupMissed`,
`HighLatencyP95`, `AuthFailureSpike`, `RateLimitRejections`, `PublishFailures`,
or `DiskUsageHigh` (see `docs/ops/prometheus-rules.yml`).

**What you'd see:** a Grafana annotation + alert on the operations dashboard;
`/metrics` shows the offending series (e.g. `agentcms_outbox_backlog` > 1000).

**Drill steps:**

1. Profile the offending series on the dashboard:
   ```bash
   curl -fsS -H "X-Metrics-Token: $METRICS_TOKEN" localhost:8000/metrics | grep -E 'agentcms_outbox_backlog|http_requests_total'
   ```
2. Find the hottest route by error rate:
   ```promql
   topk(10, sum by (path_template, status) (rate(http_requests_total{status=~"5.."}[5m])))
   ```
3. Open the trace for one bad request: filter logs by `request_id`, then follow
   the audit → outbox → webhook drill in **section 5** for the same id. In
   OTLP/jaeger, search the `agentcms.request_id` attribute.
4. Confirm a rule fires locally (staging proof, real DB):
   ```bash
   python -m pytest tests/test_alerts.py -q
   ```
5. Re-baseline the dashboard time range to the alert window and note the cause
   in the postmortem.

**Recovery:** address the root cause (restart the dispatcher for outbox
backlog; free disk for `DiskUsageHigh`; scale workers for latency). Re-check
`/readyz`, then the alert resolves after its `for:` window elapses.

---

## 3. Backup + restore-drill (#2 backup, #24)

**Trigger:** `BackupMissed` fires (no successful backup in 24h) or an annual
restore drill is due.

**What you'd see:** `agentcms_backup_last_success_timestamp` frozen at an old
epoch; `scripts/pgbackup.py` failed with `BackupError`.

**Drill steps (run this at least monthly, timed in §7):**

1. Cause a real backup of the current source DB:
   ```bash
   make backup
   ```
   Expect: `pg_dump -Fc | openssl aes-256-cbc` → object store (presigned PUT),
   local retention (30 daily + 12 monthly). Passphrase comes from
   `BACKUP_PASSPHRASE` — **never** a flag.
2. Verify the ciphertext is not a plain dump:
   ```bash
   openssl enc -aes-256-cbc -a -d -pbkdf2 -pass env:BACKUP_PASSPHRASE \
     < "$(ls -t "$BACKUP_DIR"/*.enc | head -1)" | head -c 64
   ```
3. Run the restore drill — restores the newest backup into a fresh scratch DB
   on the same server, then diffs canonical content hashes of
   `posts`, `post_revisions` and `audit_events` (created_at, id ordered):
   ```bash
   make restore-drill
   # restore drill OK: <dump>.enc restored with zero content-hash mismatches
   ```
   Non-zero exit means the backup diverged from the live data — page DBA.
4. Record the timing (round-trip, end-to-end) in §7.

**Recovery for `BackupMissed`:** check the object-store credentials and
`BACKUP_PASSPHRASE` presence; fix, then re-run `make backup`. If local storage
is the fallback, keep `BACKUP_DIR` on a different volume than the data volume.

---

## 4. Migration-gated deploy + rollback (#24)

**Trigger:** you are about to ship a schema-changing release, or a deploy
didn't come up healthy and you must go back to the previous image.

**What you'd see:** `./scripts/deploy.sh` refuses to rollout on schema drift
(`deploy_preflight.sh` → `alembic check` fails), or the health gate times out
after 240s.

**Drill steps (migration first, zero dropped requests):**

1. Pre-flight the DB in a *disposable* database (migrations must never run
   against a live cluster without a gate):
   ```bash
   make gate-migrations        # destructive: wipe a temp DB + upgrade head
   ```
2. Check for drift on the live cluster (read-only):
   ```bash
   ./scripts/deploy_preflight.sh && echo "no pending migrations"
   ```
3. Migrate off-hours with the one-shot job (safe: only after the gate):
   ```bash
   MIGRATE_JOB=1 ./scripts/deploy.sh   # runs `migrate` service, then rollout
   ```
4. If `MIGRATE_JOB=0` and migrations are pending, the deploy **refuses** to run
   — that is the gate working.

5. **Zero-dropped-request proof.** While the rolling update is in progress,
   run the small load test against `/readyz` and a read route and watch for a
   clean handoff:
   ```bash
   python -m scripts.load_test --url http://127.0.0.1:8000 --requests 500 --concurrency 10
   ```
   Expect: `dropped=0`. A healthy new container passes `/readyz` before compose
   cuts over traffic, so there is no window.

**Rollback (one command, timed in §7):**

```bash
./scripts/deploy.sh --rollback
```

This re-points compose to the previously deployed image (`.deploy-last-tag`)
and re-runs the same health gate. If the failed deploy already applied
migrations, roll back the *application* first; schema downgrades are
out-of-scope by design (forward-only migrations — see the migration
policy note in the repo docs).

**Recovery:** after rollback, confirm `/readyz` and `/status` are green, then
re-classify the failed release (config, migrations, health threshold).

---

## 4b. Version-pinned upgrade + tested rollback (#33)

**Trigger:** a new AgentCMS release is published and you want to upgrade your
self-hosted instance without data loss, with a tested rollback path if the
new release is broken.

**What you'd see:** a new immutable image tag is available (e.g.
`ghcr.io/owner/agentcms:v0.3.2` or a digest
`ghcr.io/owner/agentcms@sha256:abc123...`). The current instance runs an older
immutable tag (recorded in `.deploy-last-tag`).

**Prerequisites (one-time):**

- `BACKUP_PASSPHRASE` set in `deploy/.env` (same passphrase used for nightly
  `make backup`).
- The stack is running via `compose.prod.yml` with `AGENTCMS_IMAGE_TAG`
  pinned to the current release.
- `POSTGRES_PASSWORD`, `SECRET_KEY` and other production secrets are in
  `deploy/.env`.

**Drill steps (upgrade):**

1. Verify the current release and that the stack is healthy:
   ```bash
   cat .deploy-last-tag
   curl -fsS https://<your-domain>/healthz
   curl -fsS https://<your-domain>/readyz
   curl -fsS https://<your-domain>/v1/version
   ```
   Expect: `/healthz` and `/readyz` return 200; `/v1/version` shows the
   current image tag.

2. Run the upgrade to the new tag (pull → preflight → backup → migrate → restart → smoke):
   ```bash
   AGENTCMS_IMAGE_TAG=ghcr.io/owner/agentcms:v0.3.2 \
   BACKUP_PASSPHRASE=<your-passphrase> \
   ./scripts/upgrade.sh ghcr.io/owner/agentcms:v0.3.2
   ```
   Expected output (abridged):
   ```
   [UPGRADE] Starting upgrade from ghcr.io/owner/agentcms:v0.3.1 to ghcr.io/owner/agentcms:v0.3.2
   [UPGRADE] Pulling new image: ghcr.io/owner/agentcms:v0.3.2
   [UPGRADE] Running preflight checks...
   [UPGRADE] Preflight passed: no pending migrations
   [UPGRADE] Recording current alembic revision for rollback safety...
   [UPGRADE] Recorded pre-upgrade alembic revision: abc123
   [UPGRADE] Taking pre-upgrade backup...
   [UPGRADE] Pre-upgrade backup verified: .backups/dumps/20260115-023000.dump.enc
   [UPGRADE] Running database migrations...
   [UPGRADE] Migrations applied successfully
   [UPGRADE] Restarting API with new image...
   [UPGRADE] API restarted and healthy
   [UPGRADE] Running post-upgrade smoke tests...
   [UPGRADE] Waiting for /healthz...
   [UPGRADE] /healthz OK
   [UPGRADE] Waiting for /readyz...
   [UPGRADE] /readyz OK
   [UPGRADE] Checking /v1/version...
   [UPGRADE] Version endpoint responded: {"version":"0.3.2","git_sha":"...","migration_head":"def456"}
   [UPGRADE] All smoke tests passed
   [UPGRADE] Upgrade complete: now running ghcr.io/owner/agentcms:v0.3.2
   ```

   **Key guarantees:**
   - The new image is pulled *before* any state change.
   - `alembic check` runs read-only against the live DB; if pending
     migrations or drift are detected, the upgrade aborts and the app is
     untouched.
   - The current alembic revision is recorded (`.pre-upgrade-alembic`).
   - An encrypted `pg_dump` backup is taken **before** any migration runs
     (`.pre-upgrade-dump` + `.pre-upgrade-tag`).
   - Migrations run in a one-shot container (`migrate` service). If they
     fail, the old app continues serving on the old schema.
   - The API is restarted with a rolling update (compose `--wait` polls
     `/readyz`). Traffic is never cut to an unhealthy container.
   - Smoke tests verify `/healthz`, `/readyz`, and `/v1/version`.

3. Verify the upgrade manually:
   ```bash
   curl -fsS https://<your-domain>/healthz
   curl -fsS https://<your-domain>/readyz
   curl -fsS https://<your-domain>/v1/version
   # Spot-check a public page or capability-link endpoint
   curl -fsS https://<your-domain>/blog/posts.json
   ```

**If the upgrade appears stuck:**
- Check `docker compose -f compose.prod.yml logs -f migrate` for migration
  output (the most common stall point).
- Check `docker compose -f compose.prod.yml logs -f api` for container
  startup errors.
- `docker compose -f compose.prod.yml ps` shows container status.
- The pre-upgrade dump (`.pre-upgrade-dump`) and tag (`.pre-upgrade-tag`)
  exist — you can roll back at any point before the smoke tests pass.

**Drill steps (rollback):**

If the new release is broken (errors, regressions, performance), roll back
to the previous image and database state:

```bash
BACKUP_PASSPHRASE=<your-passphrase> \
./scripts/rollback.sh
```

Expected output (abridged):
```
[ROLLBACK] Starting rollback from ghcr.io/owner/agentcms:v0.3.2 to ghcr.io/owner/agentcms:v0.3.1
[ROLLBACK] Checking rollback safety: pre-upgrade revision was abc123
[ROLLBACK] Current alembic revision: def456
[ROLLBACK] Schema differs from backup; checking if downgrade to abc123 is possible...
[ROLLBACK] Downgrade path exists and appears non-destructive — rollback is safe
[ROLLBACK] Restoring pre-upgrade database dump: .backups/dumps/20260115-023000.dump.enc
[ROLLBACK] Database restored successfully from pre-upgrade dump
[ROLLBACK] Restarting API with previous image: ghcr.io/owner/agentcms:v0.3.1
[ROLLBACK] API restarted and healthy on ghcr.io/owner/agentcms:v0.3.1
[ROLLBACK] Running post-rollback smoke tests...
[ROLLBACK] Waiting for /healthz...
[ROLLBACK] /healthz OK
[ROLLBACK] Waiting for /readyz...
[ROLLBACK] /readyz OK
[ROLLBACK] Checking /v1/version...
[ROLLBACK] Version endpoint responded: {"version":"0.3.1","git_sha":"...","migration_head":"abc123"}
[ROLLBACK] All smoke tests passed
[ROLLBACK] Rollback complete: now running ghcr.io/owner/agentcms:v0.3.1
```

**Rollback safety guard:**
The rollback refuses to proceed (exit code 2) when:
- No pre-upgrade state exists (`.pre-upgrade-tag`, `.pre-upgrade-dump`,
  or `.pre-upgrade-alembic` missing).
- The current alembic revision differs from the pre-upgrade revision **and**
  the generated downgrade SQL contains destructive operations (`DROP TABLE`,
  `DROP COLUMN`, `DELETE FROM`, `TRUNCATE`).
- The downgrade SQL generation itself fails (no downgrade path).

In these cases the script prints an explicit message and leaves the app
running on the new image. You must restore from an external backup manually.

**What to check first if rollback fails:**
1. `cat .pre-upgrade-tag .pre-upgrade-dump .pre-upgrade-alembic` — are the
   state files present and readable?
2. `BACKUP_PASSPHRASE` — is it set and correct?
3. `docker compose -f compose.prod.yml logs migrate` — does `alembic downgrade
   <pre-upgrade-rev> --sql` emit errors or destructive statements?
4. If the schema truly cannot be downgraded, you must restore the pre-upgrade
   dump into a fresh database and point a new stack at it (disaster recovery,
   not rollback).

**Measured runtime targets (fill in §7):**

| Drill | Target |
| --- | ---: |
| Upgrade (pull → backup → migrate → restart → smoke) | < 5 min |
| Rollback (guard → restore → restart → smoke) | < 3 min |

---

## 5. Request-id trace drill (#24)

**Trigger:** a user reports "a request failed" and you need every artifact of
that one request: the structured log line, the audit entry, the outbox event,
and the webhook delivery — all sharing one id.

**What you'd see:** every log line carries `request_id`; the middleware echoes
the inbound `X-Request-ID` back on the response. The subscriber receives
`traceparent` whose root trace id can be correlated in the OTLP backend.

**Drill steps:**

1. Find the request id from the failing response (`X-Request-ID` header) or
   from the response `problem+json` `request_id` field.
2. Pull every artifact for that id:
   ```bash
   docker compose logs | grep '<request-id>'
   curl -fsS localhost:8000/v1/audit?request_id=<request-id>   # audit row
   psql "$DATABASE_URL" -c "SELECT * FROM event_outbox WHERE request_id='<request-id>';"
   psql "$DATABASE_URL" -c "SELECT * FROM webhook_deliveries WHERE request_id='<request-id>';"
   ```
3. Prove the propagation in CI (end-to-end, real subscriber server):
   ```bash
   python -m pytest tests/test_request_trace.py -q
   ```
4. In a tracing backend, filter by the `traceparent` from the subscriber's
   headers (or `agentcms.request_id` attribute) to see the full span tree.

---

## 6. Escalation / P-R runbook

**Trigger:** any pager; every on-call should know the default posture.

**Steps (CLOCK when paging):**

- **C**ontain: if `HighErrorRatio5xx`/`OutboxBacklog`, suspend the offending
  feed (`kill_switch`), or `deploy.sh --rollback` if a release is the cause.
- **L**ocate: open the Grafana dashboard → pinpoint the span/route via
  section 5's request-id drill.
- **O**bserve: `/readyz` (DB gate) + `/status`; attach log excerpts with
  `request_id`s.
- **C**onfirm: reproduce on staging; if reproducible, that's the postmortem
  appendix, not a guess.
- **K**eep data safe: backups first — `make backup` before any manual DB
  surgery; `make restore-drill` on the *copy*.

**P-R template:** (a) customer impact in SLO terms (5xx %, p95, dropped
requests), (b) root cause with evidence (trace id, log line, alert), (c)
containment + ETA, (d) permanent fix (commit link), (e) timeline.

---

## 7. Measured runtimes (restore drill + rollback)

Fill this table every drill; if a step crosses the target, open a follow-up.

| Drill | Date | Target | Measured |
| --- | --- | ---: | --- |
| Restore drill (round-trip: dump → decrypt → restore → content diff) | — | < 5 min on 10k rows | |
| Rollback (`deploy.sh --rollback` → `/readyz` green) | — | < 2 min | |
| Deploy with pending migrations (gate + migrate job + rollout) | — | < 5 min | |
| Request-id trace across all four artifacts | — | < 5 min | |

## Migration policy (one line)

Migrations are forward-only; the DB is migrated by the gated `migrate` job
*before* new code cutover, so old code still works against the new schema
(additive DDL only). Downgrades are not part of the rollback path.