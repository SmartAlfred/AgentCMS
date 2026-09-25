# Alert-delivery drill — 2026-09-25

Drill #2 of the WS3 production-readiness set (issue #39/#47): the monitoring
stack is only real if a page has been *watched arriving*.  Every timestamp below
was printed by `date -u +%FT%TZ` in the shell that ran the command, and every
number is derived from two of them.

## Environment (pinned)

| Item | Value |
| --- | --- |
| Host | macOS worker, Docker via OrbStack, `date -u` UTC |
| App | AgentCMS self-hosted production stack, compose project `ws3-drill` (api / db / caddy / minio) |
| Ops set | `deploy/compose/docker-compose.observability.yml` — Prometheus 3.1.0, Alertmanager 0.28.0, blackbox-exporter 0.25.0, Grafana 11.4.0, python 3.12-slim (pinned by digest in the compose file) |
| Scrape/eval | 15s / 15s (drill), `for:` as committed per rule |
| Channel | ntfy.sh public topic, no account, free tier — confirmed by hand **before** any rule existed |

Bring the ops set up (all commands as run):

```bash
cd /tmp/ws3                     # scratch clone of origin/main, never the supervised one
docker compose -p ws3-obs --env-file .env \
  -f deploy/compose/docker-compose.observability.yml up -d
```

## Channel check first (a broken channel must not look like a broken rule)

```
2026-09-25T13:47:20Z  curl -d "hand-sent channel test at 2026-09-25T13:47:20Z" \
                        -H "Title: AgentCMS WS3 drill channel test" \
                        https://ntfy.sh/agentcms-ws3-drill-9f2c7a4b
                      -> HTTP 200, id=ptRcb5cDD5fd, server time 2026-09-25T13:47:21Z
2026-09-25T13:47:2xZ  read back from the server: GET /agentcms-ws3-drill-9f2c7a4b/json?poll=1&since=all
                      -> 2026-09-25T13:47:21+00:00 | AgentCMS WS3 drill channel test | tags ['wrench']
```

## The four pages, induced one at a time

Faults and commands as run (`ws3-drill` is named explicitly so a copy-paste
cannot touch anyone else's stack):

| # | Rule proved | Induced failure (command) | T_fail | Rule active | Delivered (sink `received_at`) | Failure → delivery |
| - | --- | --- | --- | --- | --- | ---: |
| 1 | `MetricsScrapeMissing` (dead-man) | `docker stop ws3-drill-db-1` | 13:47:28Z | 13:47:32.305Z | 13:47:42.362Z | **14.4 s** |
| 2 | `ReadyzNotOk` | `docker stop ws3-drill-db-1` (round 2) | 13:48:00Z | 13:48:47.305Z | 13:48:57.418Z | **57.4 s** |
| 3 | `MigrateJobFailed` | `docker run --name ws3-drill-migrate-fault --label com.docker.compose.project=ws3-drill --label com.docker.compose.service=migrate python:3.12-slim python -c "…; sys.exit(1)"` | 13:50:20Z | 13:50:47.305Z | 13:50:57.401Z | **37.4 s** |
| 4 | `ContainerRestart` | `docker run -d --name ws3-drill-restart-fault --restart on-failure:5 --label com.docker.compose.project=ws3-drill --label com.docker.compose.service=restart-fault python:3.12-slim python -c "…time.sleep(3); sys.exit(1)"` (RestartCount reached 2–3) | 13:54:38Z | 13:55:17.305Z | 13:55:27.362Z | **49.4 s** |

Budget was ≤ 300s per page; the worst measured is 57.4s. Recovery notices
(Alertmanager `send_resolved: true`) all arrived on the same channel:

```
13:49:42.373Z resolved MetricsScrapeMissing  endsAt=13:49:17.305Z  ntfy HTTP 200
13:49:57.465Z resolved ReadyzNotOk           endsAt=13:49:32.305Z  ntfy HTTP 200
```

Recovery command for rounds 1–2: `docker start ws3-drill-db-1` at 13:49:11Z →
`GET /readyz` 200 again, both alerts resolved inside a minute.  For the two
synthetic one-shot faults the "recovery" is `docker rm -f
ws3-drill-restart-fault ws3-drill-migrate-fault` at 13:55:35Z (series disappear
→ alert resolves).  **Not captured:** those two resolved notices had not arrived
25 s later when this log was written, so they are reported as unverified rather
than assumed — the resolved path itself is proven by the two database-round
notices above, both delivered with HTTP 200.

### Collateral that is not a drill artifact (honest note)

`BackupMissed` and `OutboxBacklog` also paged at 13:54:17Z.  They were
**pending before** the injected faults and belong to the drill stack's own data
(no backup has ever been recorded for `ws3-drill`, and its outbox has a backlog)
— they are not caused by the restart/migrate injections.  They are, however, a
live reminder that the drill stack still needs the backup job wired in.

## Exact alert payload as delivered (rule 2)

```json
{"received_at": "2026-09-25T13:48:57.418Z",
 "title": "[FIRING] agentcms-ws3-drill: ReadyzNotOk",
 "body": "status: FIRING\nreceived_at: 2026-09-25T13:48:57.418Z\ngroupKey: {}:{alertname=\"ReadyzNotOk\"}\n\nalertname: ReadyzNotOk  [firing]\nseverity: page\nsummary: /readyz is not 200: a critical dependency (database/object store) is down\nstartsAt: 2026-09-25T13:48:47.305Z  endsAt: 0001-01-01T00:00:00Z\nlabels: instance=http://api:8000/readyz, job=agentcms-readiness, probe=readyz, severity=page, stack=agentcms",
 "channel": {"delivered": true, "http_status": 200, "server": "https://ntfy.sh", "topic": "agentcms-ws3-drill-9f2c7a4b"}}
```

## What the drill exposed (the point of running it)

1. **`changes(RestartCount[10m])` can never see a crash that settles.** The first
   version of the ContainerRestart rule did not fire at all: a container that
   restarted 3× in 12s and then stopped leaves the gauge flat, so every scrape
   sees the same value and `changes()` is 0 — a real crash would have been
   silent.  Fixed by having the exporter poll Docker every 5s itself and publish
   `agentcms_container_last_restart_observed_timestamp_seconds`; the rule is now
   `time() - agentcms_container_last_restart_observed_timestamp_seconds{job="docker-exporter"} < 600`
   (fires while the restart is younger than 10 min, resolves after that).  The
   serial order above (rule 4 at 13:55:27Z) is the re-run that proves the fix.
2. **A dead database trips the dead-man switch before the readiness page.**
   `up{job="agentcms-api"} == 0` fires immediately (0s `for:`) while `ReadyzNotOk`
   waits 30s — correct, and worth knowing when reading a page: the first page for
   a DB outage is `MetricsScrapeMissing`, the follow-up is `ReadyzNotOk`.
3. **`/metrics` trusts `X-Forwarded-For` for its loopback exemption.** Any client
   that can reach the port can add `X-Forwarded-For: 127.0.0.1` and read
   `/metrics` without the token.  Low severity for a stack published on
   loopback only, but it should be tightened (see issue comment).
   **Fixed in #49 (2026-09-26):** the exemption is decided by the TCP peer, and
   the header is only read when that peer is a proxy listed in `TRUSTED_PROXIES`
   (empty by default, so the shipped stack trusts nobody). See
   `docs/deploy/configuration.md` § "Metrics behind a proxy".

## Uptime probe — what is proved and what is blocked

* **Proved (in-stack, on-host):** blackbox-exporter probes `/healthz` and
  `/readyz` from outside the app process every 15s;
  `probe_success{job="agentcms-readiness",probe="readyz"} == 1` at baseline and
  `== 0` during the injected database outage, which is what fired rule 2.
* **Not proved (off-host):** a probe that survives *this host* dying needs an
  off-host prober, and this stack has neither a public HTTPS endpoint nor a
  free-tier account.  A self-hosted Uptime Kuma in another container on the same
  host is **not** off-host and was deliberately not passed off as one.

## Numbers

| Claim | Number | Evidence |
| --- | --- | --- |
| Worst failure → human-channel delivery | **57.4 s** (budget 300 s) | table above, sink `received_at` |
| Fastest | 14.4 s | `MetricsScrapeMissing` |
| Alertmanager → ntfy | 200 OK each time | transcript artifact |
| Resolved notifications | delivered for both database rounds | sink transcript |
| Rules firing | 4 of 4 induced | Prometheus `ALERTS` + sink transcript |

## Next drill due

**2026-12-25** (quarterly), and after any change to `prometheus-rules.yml`,
`alertmanager.yml` or the delivery channel.  Off-host probe: re-attempt when a
public endpoint or a free-tier prober account exists.
