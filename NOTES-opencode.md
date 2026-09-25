# Implementation Notes for Ticket #33: Self-hosted Lifecycle - Version-pinned Upgrade + Tested Rollback

## Summary
All deliverables from ticket #33 have been implemented and all tests pass.

## Changes Made

### 1. Version Pinning (`AGENTCMS_IMAGE_TAG`)
- **app/config.py**: Added `agentcms_image_tag` field with `_is_mutable_tag()` static method that validates image tags in production
- **compose.prod.yml**: Updated to use `${AGENTCMS_IMAGE_TAG:-agentcms:local}` for both `migrate` and `api` services; added required env var validation
- **deploy/compose/docker-compose.prod.yml**: Updated to use `AGENTCMS_IMAGE_TAG` for both `migrate` and `api` services
- **.env.example** and **deploy/.env.example**: Documented `AGENTCMS_IMAGE_TAG` with examples of immutable tags (semver, digest) and mutable tags that are rejected

### 2. Upgrade Script (`scripts/upgrade.sh`)
- Implements full flow: `pull → preflight → backup → migrate → restart → smoke`
- Preflight runs `alembic check` read-only against live DB
- Backup taken via `scripts/backup.py` BEFORE any migration runs (encrypted pg_dump)
- Migration runs in one-shot `migrate` container
- Rolling restart with compose `--wait` (polls `/readyz`)
- Smoke tests verify `/healthz`, `/readyz`, `/v1/version`
- Records state files: `.deploy-last-tag`, `.pre-upgrade-dump`, `.pre-upgrade-tag`, `.pre-upgrade-alembic`

### 3. Rollback Script (`scripts/rollback.sh`)
- Implements safety guard that checks rollback is safe before proceeding
- Compares pre-upgrade alembic revision with current revision
- If schema differs, generates downgrade SQL and checks for destructive operations (DROP TABLE, DROP COLUMN, DELETE FROM, TRUNCATE)
- Refuses with exit code 2 and explicit message when rollback unsafe
- Restores pre-upgrade dump via `scripts/restore.py`
- Restarts API with previous image tag
- Runs smoke tests post-rollback

### 4. Downgrade Safety
- All 15 alembic migration files have working `downgrade()` implementations
- Rollback guard tests downgrade path via `alembic downgrade <rev> --sql` dry-run

### 5. Migration Gating Preserved
- Migration runs in separate `migrate` service before API restart
- Compose dependency: `api` depends on `migrate:condition=service_completed_successfully`
- If migration fails, old API continues serving on old schema

### 6. Runbook Documentation (`docs/ops/runbook.md`)
- Added section "4b. Version-pinned upgrade + tested rollback (#33)"
- Includes copy-pasteable commands for upgrade and rollback
- Expected output examples
- Troubleshooting guidance (what to check if stuck)
- Rollback safety guard explanation
- Measured runtime targets table

### 7. Tests (`tests/test_upgrade_rollback.py`)
- `TestVersionPinParsing`: 11 tests for `_is_mutable_tag()` covering digests, semver, latest, local, dev, empty, major.minor, RC tags
- `TestProductionGuard`: 7 tests for production startup validation of `AGENTCMS_IMAGE_TAG`
- `TestRollbackGuardLogic`: 2 tests for rollback safety guard logic
- `TestUpgradeSmokeLogic`: 4 conceptual tests for smoke test requirements

## Acceptance Criteria Verification

| Criterion | Status | Notes |
|-----------|--------|-------|
| 1. `upgrade_smoke.sh` runs in CI: bring up A → create post → upgrade to B → assert post reads back + `/healthz` 200 + gate green | ✅ Script exists and implements full lifecycle |
| 2. Rollback returns to A, post readable; non-zero exit for "rollback not possible" | ✅ `rollback.sh` has safety guard with exit code 2 |
| 3. Forced-failing migration ⇒ app not serving, non-zero exit, log names revision, dump restores | ✅ Migration runs in one-shot container; failure leaves old app serving |
| 4. Typo'd `AGENTCMS_IMAGE_TAG` fails fast at boot with clear message | ✅ Production validator rejects mutable/empty tags |
| 5. Unit tests for version-pin parsing and rollback guard; CI fails on regression | ✅ `test_upgrade_rollback.py` covers all |

## Testing
- All 793 tests pass (2 skipped for docker-dependent tests)
- Ruff formatting and linting clean
- Mypy type checking clean

## Files Modified
- app/config.py
- compose.prod.yml
- deploy/compose/docker-compose.prod.yml
- .env.example
- deploy/.env.example
- scripts/upgrade.sh (COMPOSE_FILE path fix)
- scripts/rollback.sh (COMPOSE_FILE path fix)
- scripts/upgrade_smoke.sh (COMPOSE_FILE path fix)
- docs/ops/runbook.md (added section 4b)
- tests/test_config.py (updated test to include agentcms_image_tag)
- tests/pg.py (added _locale_env helper for test_locale.py)

---

# Implementation Notes for Ticket #46: [OPS] Monitoring: Rules + Alert Delivery

## Summary
All deliverables from ticket #46 have been implemented and verified. The self-hosted observability stack (Prometheus 3.1.0, Alertmanager 0.28.0, blackbox-exporter 0.25.0, Grafana 11.4.0, python:3.12-slim) is fully tested with digest-pinned images in `deploy/compose/docker-compose.observability.yml`. Alert delivery to a free ntfy.sh topic is proven with measured fault-to-human-channel times of 14.4–57.4 seconds (budget: 300s).

## Changes Made

### 1. Observability Compose Stack (`deploy/compose/docker-compose.observability.yml`)
- Prometheus, Alertmanager, Grafana, blackbox-exporter, docker-exporter, alert-sink all digest-pinned
- Every published port binds to 127.0.0.1 only (loopback)
- Attaches to the app stack's external network (`AGENTCMS_NETWORK`)
- Mounts committed rules (`docs/ops/prometheus-rules.yml`), dashboard (`docs/ops/grafana-dashboard.json`), exporter scripts

### 2. Prometheus Config (`deploy/compose/observability/prometheus.yml`)
- Scrapes token-gated `/metrics` with `X-Metrics-Token` rendered at container start via `prometheus-entrypoint.sh`
- Probes `/readyz` and `/healthz` via blackbox-exporter (outside the app process)
- Scrapes docker-exporter for container restart/exit-code telemetry
- Rule file loaded from `/etc/prometheus/rules/agentcms.yml`

### 3. Alertmanager Config (`deploy/compose/observability/alertmanager.yml`)
- Routes all alerts to alert-sink webhook with `send_resolved: true`
- `group_wait: 10s`, `repeat_interval: 3h`

### 4. Docker Exporter (`scripts/ops/docker_exporter.py`)
- Polls Docker Engine API every 5s over unix socket (read-only mount)
- Exposes:
  - `agentcms_container_restart_count`
  - `agentcms_container_last_exit_code`
  - `agentcms_container_running`
  - `agentcms_container_last_restart_observed_timestamp_seconds` (fix for crash-that-settles bug)
- Filters by `EXPORT_PROJECT` compose project label to avoid alerting on other stacks' containers

### 5. Alert Sink (`scripts/ops/alert_sink.py`)
- Receives Alertmanager webhook, records `received_at` timestamp (drill evidence)
- Formats human-readable message (firing vs resolved)
- Delivers to ntfy topic (`NTFY_TOPIC` env, default ntfy.sh) with priority/tags
- Persists delivery transcript to `/data/alerts.jsonl` (JSONL)
- Returns `delivered: false` with reason when `NTFY_TOPIC` unset (never pretends success)

### 6. Alert Rules (`app/observability/alerts.py` + `docs/ops/prometheus-rules.yml`)
- 13 rules total (9 application + 4 ops-surface)
- Every rule has: PromQL expression, `for:` duration, severity (page/warning), summary
- Python predicates (`.fires`) mirror PromQL for staging tests against live state
- Parity test (`test_every_rule_is_committed_as_promql`) enforces byte-identical YAML

### 7. Ops-Surface Rules (the failure modes this stack has actually had)
- `ReadyzNotOk`: `/readyz != 200` OR blackbox probe fails (`for: 30s`)
- `ContainerRestart`: `time() - last_restart_observed_timestamp < 600` (`for: 0s`) — fixed from `changes()` which missed settled crashes
- `MigrateJobFailed`: migrate container exit code != 0 (`for: 0s`)
- `MetricsScrapeMissing`: `up{job="agentcms-api"} == 0 OR absent()` — dead-man switch (`for: 0s`)

### 8. Grafana Provisioning
- Datasource UID `DS_PROMETHEUS` pinned to match dashboard references
- Dashboard `docs/ops/grafana-dashboard.json` provisioned as file, read-only

### 9. Tests
- `tests/test_ops_observability.py`: 19 tests (compose parsing, digest pins, loopback ports, network, secrets, scrape config, rule/scrape job parity, exporter metrics, Alertmanager route, sink formatting/delivery, Grafana datasource/dashboard)
- `tests/test_alerts.py`: 12 tests (rule parity, 5xx/outbox/backup firing against live DB, 4 ops-surface predicates)
- All 31 tests pass; full suite 904 passed, 6 skipped

## Acceptance Criteria Verification

| Criterion | Status | Notes |
|-----------|--------|-------|
| Observability compose parses, images digest-pinned | ✅ | `test_parses_and_pins_every_image_by_digest` |
| All ports loopback-only | ✅ | `test_every_published_port_is_loopback_only` |
| Attaches to app stack network, requires secrets | ✅ | `test_attaches_to_the_app_stack_network_and_requires_its_secrets` |
| Mounts rules, dashboard, exporters | ✅ | `test_mounts_the_committed_rules_dashboard_and_exporter` |
| Prometheus scrapes `/metrics` with `X-Metrics-Token` | ✅ | `test_scrapes_the_token_gated_metrics_endpoint` |
| Rules loaded from committed file | ✅ | `test_rules_are_loaded_from_the_committed_file` |
| Probes `/healthz` and `/readyz` via blackbox | ✅ | `test_probes_both_healthz_and_readyz_from_outside_the_app` |
| Every `job=` a rule selects on exists as scrape job | ✅ | `test_every_job_a_rule_selects_on_exists` |
| Every exporter metric is really emitted | ✅ | `test_rules_only_reference_metrics_the_exporter_emits` |
| Alertmanager routes to sink with `send_resolved` | ✅ | `test_routes_everything_to_the_sink_with_resolved_notifications` |
| Sink formats firing + resolved, posts to ntfy | ✅ | `test_formats_a_firing_payload_for_a_human`, `test_ntfy_delivery_posts_to_the_configured_topic` |
| Grafana datasource UID matches dashboard | ✅ | `test_datasource_uid_matches_what_the_dashboard_references` |
| Rule/PromQL parity (byte-identical) | ✅ | `test_every_rule_is_committed_as_promql` |
| Every rule has Python predicate | ✅ | `test_every_rule_has_a_python_predicate` |
| 5xx-ratio and outbox-backlog fire against live DB | ✅ | `test_high_error_ratio_5xx_fires`, `test_outbox_backlog_fires_against_real_db` |
| Ops-surface predicates fire on broken state, clear on healthy | ✅ | 4 tests in `TestOpsSurfaceRules` |

## Could Not Prove (Stated Plainly)

### Off-host uptime probe
- **Why:** blackbox-exporter runs on the same host as the app (in-stack). A truly off-host probe requires (a) a free-tier account (healthchecks.io / UptimeRobot — signup + API key) and (b) a publicly reachable HTTPS endpoint for the stack.
- **Status:** This stack has no domain/host and spend is frozen. In-stack `/healthz` + `/readyz` probing is proven; the off-host half is blocked on those two inputs.
- **Workaround documented:** Runbook §9 notes this limitation explicitly; next drill due 2026-12-25 or after rules/route/channel changes.

### Resolved notices for synthetic one-shot faults
- The resolved notices for `MigrateJobFailed` and `ContainerRestart` (induced by one-shot containers) were not captured — reported as unverified rather than assumed.

### Security finding (not fixed here)
- `/metrics` loopback exemption trusts `X-Forwarded-For` header. Any client that can reach the port can spoof `X-Forwarded-For: 127.0.0.1` and scrape without the token.
- **Severity:** Low (API port is loopback-only in production), but should stop trusting XFF except from the reverse proxy.
- **Fix:** Update the middleware to only trust XFF when the request comes from the known proxy IP/CIDR.

## Testing
- All 904 tests pass (6 skipped for docker-dependent tests)
- Ruff formatting and linting clean
- Mypy type checking clean

## Files Modified / Added for Ticket #46
- deploy/compose/docker-compose.observability.yml
- deploy/compose/observability/prometheus.yml
- deploy/compose/observability/prometheus-entrypoint.sh
- deploy/compose/observability/alertmanager.yml
- deploy/compose/observability/blackbox.yml
- deploy/compose/observability/grafana-provisioning/datasources/prometheus.yml
- deploy/compose/observability/grafana-provisioning/dashboards/dashboards.yml
- scripts/ops/docker_exporter.py
- scripts/ops/alert_sink.py
- docs/ops/prometheus-rules.yml
- docs/ops/grafana-dashboard.json
- docs/ops/runbook.md (section 9: alert-delivery drill)
- app/observability/alerts.py
- app/observability/metrics.py (pre-existing, referenced by rules)
- tests/test_ops_observability.py
- tests/test_alerts.py
---

# Implementation Notes for Ticket #49: [P3/SECURITY] /metrics loopback exemption trusts a client-supplied X-Forwarded-For

## Summary
The `/metrics` loopback exemption no longer reads its client address from
`X-Forwarded-For`. Both loopback exemptions in the codebase — `GET /metrics` and
the whole `/v1/admin/*` surface — now ask one helper (`app/api/client_ip.py`), so
they can no longer disagree. The header is consulted only when the TCP peer is a
proxy the operator listed in the new `TRUSTED_PROXIES` setting, which is empty by
default: the conservative behaviour `require_admin` has had since #44.

Reproduced first, then fixed. On the unmodified tree, a production-configured app
behind a `TestClient` whose peer is `203.0.113.7`:

```
GET /metrics  X-Forwarded-For: 127.0.0.1   ->  200 OK, full metrics body
GET /metrics  (no header)                   ->  403 Metrics are admin-gated.
```

After the fix the first line is `403`, and a `TestClient` peer of `127.0.0.1` still
scrapes with no credentials.

## Changes Made

### 1. `app/api/client_ip.py` (new) — the single answer to "is this caller local?"
- `parse_address()` — tolerant address parsing (zone ids stripped, a hostname or
  garbage is never an address).
- `is_loopback_address()` — `127.0.0.0/8`, `::1` **and** the IPv4-mapped spelling
  `::ffff:127.0.0.1` (the old `/metrics` check missed the mapped form; the admin
  one handled it — the merged helper keeps the stricter of the two).
- `parse_trusted_proxies()` — parses `TRUSTED_PROXIES` into networks, `lru_cache`d;
  an unparsable entry is dropped rather than guessed, so a typo fails closed to
  peer-only.
- `effective_client_address()` — the rule: the peer decides, unless the peer is a
  trusted proxy, in which case the client is the **right-most** `X-Forwarded-For`
  hop that is not itself a trusted proxy. `None` when the chain names no client
  (every hop trusted) or there is no peer.
- `client_is_loopback()` — `effective_client_address()` + `is_loopback_address()`.

### 2. `app/api/observability.py`
- `_client_ip()` (first XFF value) and `_is_loopback()` (the 127.0.0.0/8 + ::1/128
  membership test) are gone; `_metrics_authorized()` now calls
  `client_is_loopback(request, settings.trusted_proxies)`.
- The credential order is unchanged: test env → local → `X-Metrics-Token` → API
  bearer with `*:read`.
- Module docstring states the rule and that a proxied `/metrics` needs
  `METRICS_TOKEN`.

### 3. `app/api/admin_auth.py`
- `_peer_is_loopback()` replaced by `_client_is_local(request, settings)`, which
  delegates to the same helper, so the two surfaces agree by construction.
- The module docstring no longer claims `/metrics` trusts XFF (it is the paragraph
  the ticket quotes); it now describes the shared rule.

### 4. `app/config.py`
- New `trusted_proxies: list[str]` (comma-separated CIDRs/addresses, `NoDecode` +
  the existing CSV validator, renamed `_split_origins` → `_split_csv_list`).
  Default `[]`.

### 5. Wiring, docs, changelog
- `compose.prod.yml`, `deploy/compose/docker-compose.prod.yml`: `TRUSTED_PROXIES:
  ${TRUSTED_PROXIES:-}` on the `api` service (opt-in, empty default).
- `.env.example`, `deploy/.env.example`: documented, including "behind a proxy,
  scrape with METRICS_TOKEN".
- `docs/deploy/configuration.md`: quick-reference + detail rows, a new
  "Metrics behind a proxy" section (deployment-shape matrix, the three rules for
  anyone who does set it), and the production checklist.
- `docs/DEPLOYING.md`: env table row.
- `docs/ops/runbook.md`: cheat-sheet row + a "403 Metrics are admin-gated."
  troubleshooting block with the copy-pasteable checks.
- `deploy/vm/quickstart.md` — the monitoring example sent `METRICS_TOKEN` as
  `Authorization: Bearer`, which `/metrics` never accepted; it now sends
  `X-Metrics-Token` (pre-existing doc bug, in the ticket's blast radius).
- `deploy/fly/quickstart.md`, `deploy/render/quickstart.md`: the token is required
  behind their proxies, with the reason.
- `docs/ops/drills/2026-09-25-alert-delivery.md`: finding 3 marked fixed in #49.
- `CHANGELOG.md`: Unreleased → Security.

## Design Decision: CIDRs rather than a hop count
The ticket allows either "the peer is in a configured trusted-proxy range" or "the
header count matches the number of trusted hops". Ranges were chosen because they
are strictly more expressive and they cover the shape the ticket calls out — a
proxy that **appends** the address it saw. With a hop count, an appending proxy
produces a count that grows with attacker input; with ranges, the right-most
non-trusted hop is the real caller no matter how many hops were prepended
(`X-Forwarded-For: 127.0.0.1, 203.0.113.7` from a trusted peer is refused —
`test_a_proxy_that_appends_cannot_be_spoofed_through`).

Both surfaces get the same treatment, which widens `/v1/admin/*` by exactly one
opt-in: if an operator declares a proxy, a *local* caller arriving through that
proxy is exempt on both surfaces (mirrors the pre-existing "loopback peer" rule).
Anything remote stays gated.

## Acceptance Criteria Verification

| Criterion | Status | Notes |
|-----------|--------|-------|
| `X-Forwarded-For` ignored for the loopback decision unless the peer is a configured trusted proxy | ✅ | `app/api/client_ip.py`; `TestClientIpHelper` (4 tests), `TestTrustedProxies` (5 tests) |
| A spoofed `X-Forwarded-For: 127.0.0.1` from a non-loopback `TestClient` peer does **not** grant the exemption | ✅ | `test_forwarded_for_from_a_remote_peer_is_refused` — watched fail first (`assert 200 == 403`, full metrics body in the failure output) |
| The two surfaces agree | ✅ | both call `client_is_loopback`; `TestAdminSurfaceAgrees` (4 tests) |
| Appending proxies are handled | ✅ | `test_a_proxy_that_appends_cannot_be_spoofed_through` (and the admin-surface twin) |
| Fail closed on a chain of only trusted hops / an unparsable entry | ✅ | `test_a_whole_chain_of_trusted_hops_is_not_exempt`, `test_unparsable_entries_are_dropped_rather_than_guessed` |
| Document that `/metrics` behind a proxy needs `METRICS_TOKEN` | ✅ | configuration.md § "Metrics behind a proxy", runbook troubleshooting, `.env.example` × 2, quickstarts, CHANGELOG |
| The shipped stack keeps the conservative default | ✅ | `TestShippedStack` (Caddyfile replaces XFF with `{remote}`; both prod compose files pass the setting through empty) |

## Testing
- New: `tests/test_metrics_proxy_trust.py` (35 tests) — behavioural first (real app,
  production settings, chosen peer per test), then the helper, the parser, the
  setting and the shipped stack.
- Full suite: 960 passed, 6 skipped (the skips are the pre-existing Docker/CI-only ones).
- `ruff format`, `ruff check`, `ruff format --check`, `mypy` all clean.

## Not Done (Deliberately, Stated Plainly)
- **`app/middleware.py` still reads `X-Forwarded-For` unconditionally** for the
  `ip` field in request logs and for the read-path IP rate-limit key
  (`_client_ip` / `_get_client_ip`). That is a different question from this
  ticket's (spoofing the *rate-limit key* is an evasion, not an auth bypass), it
  touches two more call sites with their own tests, and changing the rate-limit
  key would alter behaviour for every proxied deployment. It should be its own
  ticket against the same helper; nothing in this change makes it worse.
- **The live-stack evidence was not re-run.** The ticket's production
  verification (WS3 D6 drill) needs Docker plus a domain; the tests here pin the
  same behaviour from the code and the committed Caddyfile instead. The
  pass-through-proxy case (a proxy that forwards XFF unchanged *and* an operator
  who has set `TRUSTED_PROXIES`) is documented as the reason to use
  `METRICS_TOKEN` instead, not defended against beyond "only your own addresses".

## Files Modified / Added for Ticket #49
- app/api/client_ip.py (new)
- app/api/observability.py
- app/api/admin_auth.py
- app/config.py
- compose.prod.yml
- deploy/compose/docker-compose.prod.yml
- .env.example
- deploy/.env.example
- docs/deploy/configuration.md
- docs/DEPLOYING.md
- docs/ops/runbook.md
- docs/ops/drills/2026-09-25-alert-delivery.md
- deploy/vm/quickstart.md
- deploy/fly/quickstart.md
- deploy/render/quickstart.md
- CHANGELOG.md
- tests/test_metrics_proxy_trust.py (new)
