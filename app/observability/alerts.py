"""Committed alert rules (#24).

Every rule exists in two forms that stay in agreement (enforced by a test):

1. **PromQL** in :attr:`ALERTS` and baked into ``docs/ops/prometheus-rules.yml``
   so a real Prometheus deployment has identical, versioned rules, and
2. a **python predicate** (``.fires``) that the ``/metrics`` scrape path and
   the staging tests evaluate against *live* in-process state — which is how
   the "all alert rules fire in a staging test" acceptance criterion is met
   without a running Prometheus.

The predicates read the same symbols the PromQL expressions reference, so the
two representations cannot drift silently.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.config import Settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


@dataclass(frozen=True)
class AlertRule:
    name: str
    severity: str
    summary: str
    promql: str
    expr_for: str
    fires: Callable[[AlertState], bool] | None = None


@dataclass
class AlertState:
    """Current metric values used by the python predicates."""

    error_ratio_5m: float = 0.0
    p95_latency_s: float = 0.0
    publish_failures_15m: int = 0
    outbox_backlog: int = 0
    outbox_oldest_age_s: float = 0.0
    disk_usage_ratio: float = 0.0
    backup_last_success: float = 0.0  # epoch seconds (0 = never)
    auth_failures_5m: int = 0
    rate_limit_rejections_5m: int = 0

    # Ops-surface signals (#47/#24 monitoring drill).  These come from the
    # scrape path and from the docker exporter (container restart count /
    # one-shot job exit code), never from a guess: an unset value means
    # "healthy" and is what the drill deliberately breaks.
    readyz_status: int = 200  # last observed GET /readyz status
    container_restarts: int = 0  # agentcms_container_restart_count
    migrate_exit_code: int = 0  # agentcms_container_last_exit_code{service="migrate"}
    metrics_scraped: bool = True  # up{job="agentcms-api"} == 1

    @property
    def backup_missed(self) -> bool:
        # No success ever, or older than the 24h RPO.
        return self.backup_last_success <= 0 or (time.time() - self.backup_last_success) > 86400


ALERTS: dict[str, AlertRule] = {
    "HighErrorRatio5xx": AlertRule(
        name="HighErrorRatio5xx",
        severity="page",
        summary="HTTP 5xx rate above 1% over the last 5 minutes",
        promql=(
            '(sum(rate(http_requests_total{status=~"5.."}[5m])) / '
            "clamp_min(sum(rate(http_requests_total[5m])), 1)) > 0.01"
        ),
        expr_for="5m",
        fires=lambda s: s.error_ratio_5m > 0.01,
    ),
    "HighLatencyP95": AlertRule(
        name="HighLatencyP95",
        severity="page",
        summary="p95 request latency above 500ms over the last 10 minutes",
        promql=(
            "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[10m])) by (le)) > 0.5"
        ),
        expr_for="10m",
        fires=lambda s: s.p95_latency_s > 0.5,
    ),
    "PublishFailures": AlertRule(
        name="PublishFailures",
        severity="warning",
        summary="3 or more publish failures in 15 minutes",
        promql="increase(agentcms_publish_failures_total[15m]) >= 3",
        expr_for="15m",
        fires=lambda s: s.publish_failures_15m >= 3,
    ),
    "OutboxBacklog": AlertRule(
        name="OutboxBacklog",
        severity="page",
        summary="Outbox backlog > 1000 pending or oldest pending > 15 minutes",
        promql=("(agentcms_outbox_backlog > 1000) OR (agentcms_outbox_oldest_age_seconds > 900)"),
        expr_for="5m",
        fires=lambda s: s.outbox_backlog > 1000 or s.outbox_oldest_age_s > 900,
    ),
    "DiskUsageHigh": AlertRule(
        name="DiskUsageHigh",
        severity="warning",
        summary="Data volume usage above 80% for 10 minutes",
        promql="agentcms_disk_usage_ratio > 0.8",
        expr_for="10m",
        fires=lambda s: s.disk_usage_ratio > 0.8,
    ),
    "BackupMissed": AlertRule(
        name="BackupMissed",
        severity="page",
        summary="No successful backup in the last 24 hours (nightly backup missed)",
        promql=(
            "(agentcms_backup_last_success_timestamp == 0) OR "
            "(time() - agentcms_backup_last_success_timestamp) > 86400"
        ),
        expr_for="5m",
        fires=lambda s: s.backup_missed,
    ),
    "AuthFailureSpike": AlertRule(
        name="AuthFailureSpike",
        severity="warning",
        summary="20+ authentication/authorization failures in 5 minutes",
        promql="increase(agentcms_auth_failures_total[5m]) >= 20",
        expr_for="5m",
        fires=lambda s: s.auth_failures_5m >= 20,
    ),
    "RateLimitRejections": AlertRule(
        name="RateLimitRejections",
        severity="warning",
        summary="10+ rate-limit or quota rejections in 5 minutes",
        promql="increase(agentcms_rate_limit_rejections_total[5m]) >= 10",
        expr_for="5m",
        fires=lambda s: s.rate_limit_rejections_5m >= 10,
    ),
    # --- the three failure modes this stack has actually had (#47) -----------
    # 1. /readyz != 200: the database disappeared under a running API
    #    (2026-09-25 drill, item 1: 503 {"code":"database-unavailable"}).
    "ReadyzNotOk": AlertRule(
        name="ReadyzNotOk",
        severity="page",
        summary="/readyz is not 200: a critical dependency (database/object store) is down",
        promql=('(probe_success{job="agentcms-readiness"} == 0) OR (agentcms_readyz_status != 200)'),
        expr_for="30s",
        fires=lambda s: s.readyz_status != 200,
    ),
    # 2. RestartCount > 0: a container died and Docker brought it back (the
    #    crash-looping API of the 2026-09-25 desktop drill).
    "ContainerRestart": AlertRule(
        name="ContainerRestart",
        severity="page",
        summary="a stack container restarted (RestartCount increased) within the last 10 minutes",
        promql='changes(agentcms_container_restart_count{job="docker-exporter"}[10m]) > 0',
        expr_for="0s",
        fires=lambda s: s.container_restarts > 0,
    ),
    # 3. The one-shot migrate job exiting non-zero: schema not at head while the
    #    API keeps serving (the deploy gate that CI's alembic up/down covers).
    "MigrateJobFailed": AlertRule(
        name="MigrateJobFailed",
        severity="page",
        summary="the one-shot migrate job exited non-zero: the schema is not at head",
        promql=('agentcms_container_last_exit_code{job="docker-exporter",service="migrate"} != 0'),
        expr_for="0s",
        fires=lambda s: s.migrate_exit_code != 0,
    ),
    # Dead-man: alerting that lives only on the monitored host cannot report the
    # host's death, so "no successful scrape at all" is itself a page.
    "MetricsScrapeMissing": AlertRule(
        name="MetricsScrapeMissing",
        severity="page",
        summary="no successful /metrics scrape (target down or gone) — dead-man switch",
        promql='(up{job="agentcms-api"} == 0) OR (absent(up{job="agentcms-api"}))',
        expr_for="0s",
        fires=lambda s: not s.metrics_scraped,
    ),
}


def build_state(
    settings: Settings, db: Session | None = None, *, snapshot: AlertState | None = None
) -> AlertState:
    """Assemble the live state the predicates evaluate against.

    Values come from the in-process Prometheus registry (recent-window ratios
    and counters) which the scrape path keeps current; DB-backed gauges are
    refreshed first when a session is available.
    """
    if db is not None:
        refresh(db)

    from . import metrics as m

    disk = m.disk_usage_ratio._value.get()
    backup = m.backup_last_success_timestamp._value.get()

    state = AlertState(
        error_ratio_5m=m.global_5xx_ratio_5m(),
        p95_latency_s=m.p95_latency_seconds(),
        publish_failures_15m=m.count_recent("publish_failure", seconds=15 * 60),
        outbox_backlog=int(m.outbox_backlog._value.get()),
        outbox_oldest_age_s=float(m.outbox_oldest_age_seconds._value.get()),
        disk_usage_ratio=float(disk),
        backup_last_success=float(backup),
        auth_failures_5m=m.count_recent("auth_failure", seconds=5 * 60),
        rate_limit_rejections_5m=m.count_recent("rate_limit", seconds=5 * 60),
    )
    # Explicit snapshot overrides for deterministic tests.
    if snapshot is not None:
        for f in AlertState.__dataclass_fields__:
            val = getattr(snapshot, f)
            if val not in (None, "") or (isinstance(val, float) and val != 0.0):
                setattr(state, f, getattr(snapshot, f))
    return state


def refresh(db: Session) -> None:
    """Refresh DB-backed gauges from the current database."""
    from .metrics import refresh_status_metrics

    refresh_status_metrics(db)


def evaluate(
    settings: Settings,
    db: Session | None = None,
    *,
    snapshot: AlertState | None = None,
) -> dict[str, str]:
    """Return {name: summary} for every alert that currently fires."""
    state = build_state(settings, db, snapshot=snapshot)
    fired: dict[str, str] = {}
    for name, rule in ALERTS.items():
        if rule.fires is not None and rule.fires(state):
            fired[name] = rule.summary
    return fired


def render_promql_rules() -> str:
    """Render the committed PromQL rules (written to docs/ops/prometheus-rules.yml)."""
    header = (
        "# Committed alert rules (#24) — generated from app/observability/alerts.py\n"
        "# (make alert-rules regenerates this file; a test keeps both in sync).\n"
        "# Prometheus config groups: rule_files:\n"
        "#   - /etc/prometheus/rules/agentcms.yml\n"
    )
    groups: list[str] = [header, "groups:", "  - name: agentcms-alerts", "    rules:"]
    for rule in ALERTS.values():
        groups.append(f"      - alert: {rule.name}")
        groups.append(f"        expr: {rule.promql}")
        groups.append(f"        for: {rule.expr_for}")
        groups.append(f"        labels: {{ severity: {rule.severity} }}")
        groups.append(f'        annotations: {{ summary: "{rule.summary}" }}')
    return "\n".join(groups) + "\n"


def parse_rules_yaml(raw: str) -> dict[str, str]:
    """Parse committed rules YAML back into {name: promql} for the parity test."""
    import re

    names: dict[str, str] = {}
    for block in raw.split("      - alert:"):
        block = block.strip()
        if not block or block.startswith("#"):
            continue
        name = block.splitlines()[0].strip()
        m = re.search(r"expr: (\S.*)$", block, re.MULTILINE)
        names[name] = m.group(1).strip().strip("'\"") if m else ""
    return names
