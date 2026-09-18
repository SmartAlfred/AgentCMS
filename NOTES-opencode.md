# NOTES-opencode.md — run/test/deploy docs (#26) on top of export/observability (#23/#24)

## #26 in one paragraph

Ticket #26 ("README — how to run, test and deploy the service; make the push
gate match CI") is about **making the docs match what the service actually
does** — every command in them executed at this revision, real output pasted.
The docs are `README.md` (rewritten), `docs/RUNNING.md`, `docs/TESTING.md`,
`docs/DEPLOYING.md` (new). While executing every documented command, five real
defects that only appear when you *actually run* the flows were found and
fixed (below). The full gate is green and the live GitHub Pages site is HTTP
200. No off-limits repo-root files were touched.

## Real bugs found & fixed while verifying the docs (#26)

1. **`scripts/restore_drill.py` — masked password.** The scratch-DB URL was
   built with `str(URL)`, which SQLAlchemy renders with the password `***` —
   so the restore drill failed with `password authentication failed` against
   any password-authed server (docker/CI's `postgres:16`). Only the
   `initdb`/trust path looked green. Fixed with
   `parsed.set(database=scratch).render_as_string(hide_password=False)`.
2. **`scripts/pgbackup.py` — `SET transaction_timeout = 0;` skew.** Homebrew's
   pg_restore 18 rebroadcasts that PG17+ housekeeping statement into PG16
   servers, which reject it (`unrecognized configuration parameter`); the old
   `pg_restore -d` path failed on the very target version. `restore_dump` now
   runs `pg_restore --no-owner --no-privileges --exit-on-error -f -`, strips
   the one `SET transaction_timeout = 0;` line, and replays the SQL through
   `psql --no-psqlrc -q -v ON_ERROR_STOP=1`; `psql` was added to
   `_require_binaries()`.
3. **`pyproject.toml` — `httpx` was a dev-only extra.** Runtime modules
   (`app/services/validation.py`, `content_policy.py`, `webhook.py`,
   `health.py`, `app/api/v1/assets.py`) import it, so the production image
   crashed on boot (`ModuleNotFoundError: No module named 'httpx'`). Moved
   `httpx>=0.27` into `[project].dependencies`.
4. **`compose.prod.yml` — `api` raced the migrate job on a fresh volume.**
   The api entrypoint defensively auto-migrates, so simultaneous start on an
   empty volume hit `UniqueViolation` on `alembic_version`. `api` now
   `depends_on: migrate: condition: service_completed_successfully`.
5. **`scripts/deploy.sh` — fresh-environment bootstrap was broken.** The
   rolling update ran `compose -f compose.prod.yml up -d --no-deps api`; on a
   fresh env `db` was never started, so `api` died with
   `failed to resolve host 'db'` (also broke the migrate-job path, which needs
   db up). Fixed: `up -d db` first (idempotent no-op on a running stack).
   Verified end-to-end in **both** modes — plain rollout (preflight green,
   no pending) and first deploy to an empty volume (preflight fails → one-shot
   `migrate` job applies all 14 revisions → rolling update → `/readyz` gate →
   `deploy complete`). `.deploy-last-tag` is now gitignored.

## Deliberate divergences from the ticket's assumptions (#26)

- **"SQLite fallback" does not exist and will not be added.** The suite
  (migrations, content API, FTS `tsvector`/trigram, conftest truncation) is
  Postgres-only by design; `docs/TESTING.md` says so explicitly rather than
  hand-wave it.
- **`PUBLIC_BASE_URL` is not a setting.** The real variables are
  `S3_PUBLIC_BASE_URL` (`app/config.py`, media URLs) and `BASE_URL` (the
  Pages-export workflow's repo variable). `docs/DEPLOYING.md` maps them
  instead of inventing a variable.
- **Ticket's "main is red" no longer holds:** in this checkout the two #25
  failures (formatter drift + alembic drift) were already fixed and the gate
  was green before any doc work; docs show the green gate and reference #25
  as resolved.

## What was delivered (#24)

- `app/observability_types.py` — `RequestObservation` (request_id, method,
  path_template, status, duration_ms, actor id/label/kind, source, ip,
  user_agent, bytes in/out, `is_error`, `as_log_record()`).
- `app/observability/` — `metrics.py` (prometheus-client registry: request
  rate/latency, writes by actor, publish, rate-limit/auth/moderation/webhook,
  backup counters + status gauges incl. outbox backlog, oldest age, webhook
  delivery ratio, review queue, pool saturation, disk usage; 5-minute recent
  window for the error-ratio alert), `tracing.py` (OTel providers, ratio
  sampler + always-on error tracer, `trace()`/`start_http_span`/
  `end_http_span`, W3C traceparent inject/extract, `reset_for_test`),
  `alerts.py` (8 committed rules with PromQL + python predicates that fire in
  staging tests), `__init__.py` (`configure_observability`, re-exports).
- `app/logging.py` rewrite — `JsonFormatter` + `StandardFormatter`,
  `LOG_FORMAT=json|text`, `_REQUEST_FIELDS_ATTR` merged top-level, token +
  capability redaction preserved (root-logger filter always runs).
- `app/middleware.py` — `RequestContextMiddleware` is now raw-ASGI: echoes
  `X-Request-ID`, counts bytes in/out, emits one structured `"request complete"`
  line, feeds `observe_request` and `end_http_span` in a never-raise block.
- `app/api/observability.py` — `GET /metrics` gated (test env / loopback /
  `X-Metrics-Token` constant-time / bearer scopes), refreshes DB gauges per
  scrape; `app/api/health.py` + `app/services/health.py` — `/healthz`,
  per-dependency `/readyz` (DB critical; object-store/queue advisory),
  `/status`, `/v1/version` (git_sha, build_time, migration head) + legacy
  `/version`.
- `app/auth.py` — `_attach_actor_state` (actor id/label/kind/source on
  `request.state`).
- `app/services/webhook.py` — deliveries store `request_id`, dispatch/replay
  propagate it, `trace("webhook.deliver")` wraps the POST, W3C `traceparent`
  injected into outbound headers, `metrics.observe_webhook_delivery`.
- Migration `alembic/versions/f4e5d6c7b8a9_add_webhook_delivery_request_id.py`
  (new head) — indexed `webhook_deliveries.request_id`.
- `scripts/pgbackup.py` — `pg_dump -Fc` + OpenSSL AES-256-CBC, file:// or
  presigned S3 PUT, local retention (N daily + M monthly), `restore_dump`,
  `to_libpq_url` (pg binaries need libpq URLs, not `+psycopg`).
- `scripts/backup.py` (nightly job + `--prune-only`), `scripts/restore_drill.py`
  (scratch DB on the same server + canonical sha256 content diff over
  posts/post_revisions/audit_events), `scripts/load_test.py`
  (zero-dropped-requests load test for the deploy window),
  `scripts/deploy.sh` + `scripts/deploy_preflight.sh` (migration gate, one-shot
  `migrate` job, `/readyz` health gate, `--rollback`).
- `compose.prod.yml` — `migrate` service + api healthcheck + observability env
  passthrough.
- `docs/ops/` — `runbook.md` (six drills + measured-runtime table),
  `grafana-dashboard.json`, `prometheus-rules.yml` (generated by
  `make alert-rules`).
- Tests: `test_alerts.py` (parity + real firing), `test_request_trace.py`
  (request_id through log/audit/revision/outbox/delivery + real subscriber
  traceparent), `test_secrets_in_logs.py` (token families through the JSON
  formatter + middleware + a no-credential-callsite grep), `test_backup_restore.py`
  (encrypted dump round-trip, retention, full CLI restore drill),
  `test_ops_docs.py` (runbook/dashboard/rules deploy tooling).
- Makefile targets: `backup`, `restore-drill`, `alert-rules`,
  `preflight-migrations`, `deploy`, `rollback`, `migrate-gate` (all in
  `make help`; REQUIRED targets from `test_infra_files.py` untouched).

## Key decisions (#24)

- **DB-only readiness gate.** MinIO isn't running locally and S3 dev creds
  are wired via `.env`, so the object-store probe (and queue, advisory) can
  never gate `/readyz`. Only the DB is critical; `db_failure()` re-raises
  `DatabaseUnavailableError` so the pre-existing problem+json contract
  (`503 + retry-after: 2 + checks.database.status`) is preserved and its tests
  pass unchanged.
- **Two OTel providers**: a `ParentBased(TraceIdRatioBased)` provider for
  root spans and an always-on provider for `http.request.error` — 5xx/error
  traces are always recorded even when the root wasn't sampled. The global
  tracer provider is set at most once (`_global_provider_set`) to avoid SDK
  warnings.
- **Alerts live twice.** PromQL rendered to `docs/ops/prometheus-rules.yml`
  and python predicates over live in-process metrics; a parity test keeps the
  file byte-regenerable from code, and staging tests prove
  `HighErrorRatio5xx` and `OutboxBacklog` fire against real state/DB.
- **Redaction runs at the root logger**, not only on handlers, because
  `Handler.filter` short-circuits on the first filter returning `True`
  (`any(...)`), which would otherwise skip the token filter.
- **request_id flows everywhere**: middleware seeds `scope.state.request_id`
  from inbound `X-Request-ID`; posts service threads it into
  audit/revisions/outbox; `_create_revision` now receives it; dispatcher and
  redeliver propagate it to `webhook_deliveries.request_id`; outbound
  `traceparent` is injected *inside* the `webhook.deliver` span.
- **pg tools libpq URLs**: `pg_dump`/`pg_restore` reject `postgresql+psycopg://`;
  `to_libpq_url()` strips the driver. `pg_restore` also refuses `-` as a stdin
  filename, so the decrypted dump is staged in a temp file.
- **Migration-gated deploy**: `deploy_preflight.sh` fails the rollout on
  `alembic check` drift; `deploy.sh` runs the compose `migrate` one-shot job
  first, then rolling-updates with a `/readyz` gate and records
  `.deploy-last-tag` for `--rollback`. Migrations are forward-only.

## Honest limitations (not verified / not possible here)

1. **Prometheus/Grafana runtime**: rules + dashboard are committed and
   validated as data (JSON/parity tests), but no live Prometheus/Grafana is
   run in this repo; alert `for:` windows and dashboard RPS are unmeasured.
2. **OTLP export**: `configure_tracing` installs an OTLP exporter only when
   `OTLP_ENDPOINT` is set; no collector exists here, so exported spans are
   verified via `InMemorySpanExporter`-style assertions in tests (request
   middleware + `end_http_span`), not against a real backend.
3. **Object store backups**: `s3://` upload uses the media stack's SigV4
   presigned PUT; without MinIO/S3 credentials locally only `file://` stores
   are exercised by tests. S3 retention is delegated to bucket lifecycle
   rules (documented in the runbook).
4. **Docker deploy/rollback**: `deploy.sh`/`compose.prod.yml` are now
   **executed end-to-end** in this checkout (docker running via OrbStack):
   image build, prod stack up (migrate exit 0, api healthy, `/readyz` ready),
   and `make deploy` in both plain and fresh-bootstrap modes (§ above). The
   load-test helper (`scripts/load_test.py`) is the zero-dropped-request proof
   tool; rollback after a failed deploy was not forced here (no failed deploy
   to recover from).
5. **Real GitHub Pages push / CI secrets**: still out of scope, unchanged from
   #23's note.
6. **`disk_usage_ratio` callback**: refreshed only when a scraper-side
   callback is provided (never in tests); production wiring (e.g. psutil or
   `du`) is documented, not implemented.

## Gate

`source .venv/bin/activate && python -m ruff format . && python -m pytest -q && python -m ruff check . && python -m ruff format --check . && python -m mypy`

Result at handoff: **706 passed** (docker-backed ephemeral `postgres:16-alpine`,
docker running via OrbStack), ruff format/check and mypy all clean. The CI
mirror gate was also green verbatim:
`ruff format --check . && ruff check . && mypy && pytest -q` → 148 files
already formatted / All checks passed! / no issues in 92 source files / 706
passed. Migrations gate (`upgrade head → downgrade base → upgrade head →
alembic check`) green against Postgres 16 → "No new upgrade operations
detected."

## Hoisting notes for the next agent

- `test_infra_files.py` REQUIRED Makefile targets are fixed; extend `make help`
  descriptions if more targets are added or the infra test fails.
- Redaction/spike tests expect `acms_<hex>_<secret>` and `cap_<site>_<secret>`
  shapes; keep `app/logging._TOKEN_RE`/`_CAP_TOKEN_RE` the source of truth and
  add both token families if the shape ever changes.
- Recent-window metrics (`_recent_statuses`/`_recent_durations`/
  `_recent_events`) are in-process only; restart resets them. Alert predicates
  that need history *before* a restart should read the prometheus counters
  instead (`increase(...)` semantics) — see `refresh_status_metrics`.
- `reset_recent_windows_for_test()` exists for deterministic test seeding.
- The restore drill spawns a subprocess in tests via `sys.executable` +
  `scripts.restore_drill` with `DATABASE_URL`/`BACKUP_PASSPHRASE`/`BACKUP_DIR`
  env; keep that contract if the CLI flags change.
- `RequestObservation.as_log_record()` returns a flat dict merged into JSON
  logs; the seed of the structured-log test relies on `path_template` exactly
  matching the FastAPI route template (e.g. `/sites/{site_slug}/posts`).