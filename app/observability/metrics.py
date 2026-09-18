"""Prometheus metrics (#24).

Symbols exported here are plain Prometheus collectors registered on a
dedicated registry served by ``GET /metrics``.  Request-level metrics are
wired to the request middleware via :func:`observe_request`; status metrics
(gauge-style) are refreshed on each scrape by :func:`refresh_status_metrics`.

Alert rules (committed in :mod:`app.observability.alerts` and rendered to
``docs/ops/prometheus-rules.yml``) read these series.
"""

from __future__ import annotations

import collections
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from app.observability_types import RequestObservation

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

registry = CollectorRegistry(auto_describe=False)

# Request rate / latency / error-ratio by route template.
http_requests_total = Counter(
    "http_requests_total",
    "HTTP requests by method, route template and status code.",
    ("method", "path_template", "status"),
    registry=registry,
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "Request latency by method and route template.",
    ("method", "path_template"),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=registry,
)

# Writes by actor kind/label (top-N via topk in PromQL / Grafana).
writes_total = Counter(
    "agentcms_writes_total",
    "Write operations (POST/PUT/PATCH/DELETE) by actor kind and label.",
    ("actor_kind", "actor_label"),
    registry=registry,
)

# Publish outcomes.
publish_attempts_total = Counter(
    "agentcms_publish_attempts_total",
    "Publish attempts by site.",
    ("site_slug",),
    registry=registry,
)
publish_failures_total = Counter(
    "agentcms_publish_failures_total",
    "Publish failures by site and reason.",
    ("site_slug", "reason"),
    registry=registry,
)

# Rate limiting / auth / moderation.
rate_limit_rejections_total = Counter(
    "agentcms_rate_limit_rejections_total",
    "Requests rejected by rate limiting or quota, by reason.",
    ("reason",),
    registry=registry,
)
auth_failures_total = Counter(
    "agentcms_auth_failures_total",
    "Authentication/authorization failures by reason.",
    ("reason",),
    registry=registry,
)
moderation_blocks_total = Counter(
    "agentcms_moderation_blocks_total",
    "Content moderation blocks by rule name (#18).",
    ("rule",),
    registry=registry,
)

# Webhook delivery outcomes (from webhook_deliveries table).
webhook_deliveries_total = Counter(
    "agentcms_webhook_deliveries_total",
    "Webhook delivery attempts by outcome.",
    ("outcome",),
    registry=registry,
)

# Backups (#24).
backup_results_total = Counter(
    "agentcms_backup_results_total",
    "Backup job outcomes.",
    ("status",),
    registry=registry,
)
backup_last_duration_seconds = Gauge(
    "agentcms_backup_last_duration_seconds",
    "Duration of the last completed backup.",
    registry=registry,
)
backup_last_success_timestamp = Gauge(
    "agentcms_backup_last_success_timestamp",
    "Unix time of the last successful backup (0 if never).",
    registry=registry,
)

# Status gauges refreshed at scrape time by refresh_status_metrics().
outbox_backlog = Gauge(
    "agentcms_outbox_backlog",
    "Pending event_outbox rows (never locked, never sent).",
    registry=registry,
)
outbox_oldest_age_seconds = Gauge(
    "agentcms_outbox_oldest_age_seconds",
    "Age in seconds of the oldest pending outbox row.",
    registry=registry,
)
webhook_delivery_success_ratio = Gauge(
    "agentcms_webhook_delivery_success_ratio",
    "Ratio of successful webhook deliveries over the last 24h.",
    registry=registry,
)
review_queue_depth = Gauge(
    "agentcms_review_queue_depth",
    "Posts in pending_review status.",
    registry=registry,
)
db_pool_saturation_ratio = Gauge(
    "agentcms_db_pool_saturation_ratio",
    "Fraction of the SQLAlchemy write pool in use (0..1).",
    registry=registry,
)
disk_usage_ratio = Gauge(
    "agentcms_disk_usage_ratio",
    "Filesystem usage of the backing data volume (0..1).",
    registry=registry,
)

# ---------------------------------------------------------------------------
# Recent-window tracking for the time-scoped alert rules (5m error ratio).
# ---------------------------------------------------------------------------

_RECENT_WINDOW_SECONDS = 5 * 60
_recent_statuses: dict[str, list[tuple[float, int]]] = collections.defaultdict(list)
_recent_durations: dict[str, list[tuple[float, float]]] = collections.defaultdict(list)
_recent_events: dict[str, list[float]] = collections.defaultdict(list)
_lock = threading.Lock()

_EVENT_WINDOWS = {
    "publish_failure": 15 * 60,
    "auth_failure": 5 * 60,
    "rate_limit": 5 * 60,
}


def count_recent(kind: str, seconds: int | None = None) -> int:
    """Count events of ``kind`` in the trailing window (prunes stale entries)."""
    window = seconds or _EVENT_WINDOWS.get(kind, 5 * 60)
    with _lock:
        now = time.monotonic()
        events = _recent_events[kind]
        while events and now - events[0] > window:
            events.pop(0)
        return len(events)


def record_event(kind: str) -> None:
    with _lock:
        _recent_events[kind].append(time.monotonic())


def reset_recent_windows_for_test() -> None:
    """Clear recent-window state so tests start from a cold, deterministic window."""
    with _lock:
        _recent_statuses.clear()
        _recent_durations.clear()
        _recent_events.clear()


def p95_latency_seconds(window_seconds: int = 10 * 60) -> float:
    """Approximate p95 request latency over the trailing window (seconds)."""
    with _lock:
        now = time.monotonic()
        samples: list[float] = []
        for durations in _recent_durations.values():
            for ts, dur in durations:
                if now - ts <= window_seconds:
                    samples.append(dur)
        # Prune the recorder as well.
        for key, durations in list(_recent_durations.items()):
            while durations and now - durations[0][0] > window_seconds:
                durations.pop(0)
            if not durations:
                _recent_durations.pop(key, None)
    if not samples:
        return 0.0
    samples.sort()
    idx = round(0.95 * (len(samples) - 1))
    return samples[idx]


def _recent_key(obs: RequestObservation) -> str:
    return obs.path_template or "other"


def _is_write_method(method: str) -> bool:
    return method in ("POST", "PUT", "PATCH", "DELETE")


def _metric_path(path_template: str) -> str:
    """Bound label cardinality: only flag templates and a flat allow-list."""
    if path_template and ("{" in path_template or path_template in _FLAT_ALLOW):
        return path_template
    return "other"


_FLAT_ALLOW = frozenset(
    {"/", "/healthz", "/readyz", "/metrics", "/status", "/docs", "/redoc", "/openapi.json", "/v1/version"}
)


# ---------------------------------------------------------------------------
# Request observation (called by the request middleware)
# ---------------------------------------------------------------------------


def observe_request(obs: RequestObservation) -> None:
    """Record one completed HTTP request into the Prometheus registry and the
    recent-window tracker (used by the 5xx-ratio alert)."""
    status_label = str(obs.status)
    route = _metric_path(obs.path_template)

    http_requests_total.labels(obs.method, route, status_label).inc()
    http_request_duration_seconds.labels(obs.method, route).observe(obs.duration_ms / 1000.0)
    if obs.bytes_err:
        http_requests_total.labels(obs.method, route, "error").inc()

    if obs.status == 429:
        rate_limit_rejections_total.labels(reason="rate_limit_or_quota").inc()
    elif obs.status == 401:
        auth_failures_total.labels(reason="unauthenticated").inc()
    elif obs.status == 403:
        auth_failures_total.labels(reason="forbidden").inc()

    if _is_write_method(obs.method) and 200 <= obs.status < 300 and (obs.actor_kind or obs.actor_id):
        writes_total.labels(obs.actor_kind or "unknown", obs.actor_label or "unknown").inc()

    with _lock:
        key = _recent_key(obs)
        now = time.monotonic()
        recent = _recent_statuses[key]
        recent.append((now, obs.status))
        _recent_durations[key].append((now, obs.duration_ms / 1000.0))
        while recent and now - recent[0][0] > _RECENT_WINDOW_SECONDS:
            recent.pop(0)

    if obs.status == 429:
        record_event("rate_limit")
    elif obs.status == 401 or obs.status == 403:
        record_event("auth_failure")


def _snapshot_statuses() -> dict[str, tuple[int, int]]:
    """Return {key: (5xx_count, total)} over the trailing 5-minute window.

    The per-route deques are pruned to the window on insertion, so no
    time-shrinking math is needed — we simply aggregate what is present.
    """
    with _lock:
        now = time.monotonic()
        snap: dict[str, tuple[int, int]] = {}
        for key, recent in _recent_statuses.items():
            total = 0
            errors = 0
            for ts, status in recent:
                if now - ts <= _RECENT_WINDOW_SECONDS:
                    total += 1
                    if status >= 500:
                        errors += 1
            if total:
                snap[key] = (errors, total)
        return snap


def global_5xx_ratio_5m() -> float:
    """Ratio (errors/total) of HTTP responses with status >= 500 over the last
    five minutes, across all routes."""
    errors = total = 0
    for e, t in _snapshot_statuses().values():
        errors += e
        total += t
    return (errors / total) if total else 0.0


def route_5xx_ratio_5m(route: str) -> float:
    """5xx ratio for a single route template over the last five minutes."""
    for key, (e, t) in _snapshot_statuses().items():
        if key == route:
            return (e / t) if t else 0.0
    return 0.0


# ---------------------------------------------------------------------------
# Business-event hooks (called from services)
# ---------------------------------------------------------------------------


def observe_publish_attempt(site_slug: str) -> None:
    publish_attempts_total.labels(site_slug=site_slug).inc()


def observe_publish_failure(site_slug: str, reason: str) -> None:
    publish_failures_total.labels(site_slug=site_slug, reason=reason).inc()
    record_event("publish_failure")


def observe_moderation_block(rule_name: str) -> None:
    moderation_blocks_total.labels(rule=rule_name).inc()


def observe_auth_failure(reason: str) -> None:
    auth_failures_total.labels(reason=reason).inc()
    record_event("auth_failure")


def observe_webhook_delivery(ok: bool) -> None:
    webhook_deliveries_total.labels(outcome="success" if ok else "failure").inc()


def observe_backup_result(ok: bool, duration: float) -> None:
    backup_results_total.labels(status="ok" if ok else "failed").inc()
    backup_last_duration_seconds.set(duration)
    if ok:
        backup_last_success_timestamp.set_to_current_time()


# ---------------------------------------------------------------------------
# Status-gauge refresh (called on every /metrics scrape)
# ---------------------------------------------------------------------------


def refresh_status_metrics(db: Session, *, disk_usage_cb: Callable[[], float] | None = None) -> None:
    """Recompute DB-backed gauges. Cheap enough to run on every scrape.

    ``disk_usage_cb`` is an optional callable returning a 0..1 ratio for the
    data volume (used by the backup-adjacent disk alert); it is never called
    in tests unless provided.
    """
    from sqlalchemy import func

    from app.models.event_outbox import EventOutbox
    from app.models.post import Post
    from app.models.webhook import WebhookDelivery

    # --- Outbox backlog + oldest age ------------------------------------/
    pending_count = db.query(EventOutbox).filter(EventOutbox.dispatched.is_(False)).count()
    outbox_backlog.set(pending_count)

    oldest = (
        db.query(func.min(func.extract("epoch", EventOutbox.created_at)))
        .filter(EventOutbox.dispatched.is_(False))
        .scalar()
    )
    oldest_epoch = float(oldest) if oldest is not None else None
    outbox_oldest_age_seconds.set((time.time() - oldest_epoch) if oldest_epoch else 0.0)

    # --- Webhook delivery success ratio (last 24h) -----------------------/
    since = time.time() - 86400
    total_deliveries = (
        db.query(WebhookDelivery).filter(func.extract("epoch", WebhookDelivery.created_at) >= since).count()
    )
    ok_deliveries = (
        db.query(WebhookDelivery)
        .filter(
            WebhookDelivery.response_status.is_not(None),
            WebhookDelivery.delivered_at.is_not(None),
            WebhookDelivery.response_status >= 200,
            WebhookDelivery.response_status < 300,
            func.extract("epoch", WebhookDelivery.created_at) >= since,
        )
        .count()
    )
    webhook_delivery_success_ratio.set((ok_deliveries / total_deliveries) if total_deliveries else 1.0)

    # --- Review queue depth ---------------------------------------------/
    review_queue_depth.set(db.query(Post).filter(Post.status == "pending_review").count())

    # --- DB pool saturation ------------------------------------------------/
    from app.db.session import get_engine

    def _pool_attr(pool: object, name: str, default: int) -> int:
        value = getattr(pool, name, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                return default
        return int(value) if isinstance(value, (int, float)) else default

    pool = getattr(get_engine(), "pool", None)
    if pool is not None:
        checked = _pool_attr(pool, "checkedout", 0)
        max_overflow = _pool_attr(pool, "max_overflow", 0)
        size = _pool_attr(pool, "size", 5)
        db_pool_saturation_ratio.set(min(1.0, checked / max(1, size + max_overflow)))

    if disk_usage_cb is not None:
        try:
            disk_usage_ratio.set(disk_usage_cb())
        except Exception:  # pragma: no cover
            disk_usage_ratio.set(0.0)


def write_metrics() -> bytes:
    """Serialize all registered metrics (Prometheus text format)."""
    return generate_latest(registry)


def metrics_content_type() -> str:
    return CONTENT_TYPE_LATEST
