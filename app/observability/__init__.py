"""Observability package (#24): request metrics, OpenTelemetry tracing and
alert hooks wired to Prometheus.

Public entrypoint is ``observe_request(obs)`` (called by the request
middleware) and ``configure_observability(settings)`` (called once by the
app factory).  Metrics are exposed on ``GET /metrics`` (see
``app/api/v1/observability.py``).
"""

from __future__ import annotations

from app.config import Settings

from .metrics import observe_request, registry, write_metrics
from .tracing import configure_tracing, start_span

__all__ = [
    "Settings",
    "configure_tracing",
    "observe_request",
    "registry",
    "start_span",
    "write_metrics",
]


def configure_observability(settings: Settings) -> None:
    """Wire tracing at startup.  (Metrics are plain Prometheus symbols; they
    need no setup.)"""
    configure_tracing(settings)
