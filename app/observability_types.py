"""Shared observability data structures (#24).

These live in a dedicated module (not inside the middleware package) so the
metrics/tracing/alert modules can import them without circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class RequestObservation:
    """One request's structured log payload (#24).

    Collected by the outermost middleware and emitted as a single JSON line,
    recorded into Prometheus, and attached to the OpenTelemetry span.

    ``path_template`` is always the route's format string (never the raw path,
    because capability tokens live in paths — #6).
    """

    request_id: str = ""
    method: str = ""
    path_template: str = ""
    status: int = 0
    duration_ms: float = 0.0
    actor_id: str = ""
    actor_label: str = ""
    actor_kind: str = ""
    source: str = ""
    ip: str = ""
    user_agent: str = ""
    bytes_in: int = 0
    bytes_out: int = 0
    bytes_err: str = ""

    @property
    def is_error(self) -> bool:
        return self.status >= 500

    def as_log_record(self) -> dict[str, object]:
        """The structured field set emitted to the request log stream."""
        return {
            "ts": datetime.now().isoformat(sep=" ", timespec="milliseconds"),
            "level": "INFO",
            "request_id": self.request_id,
            "method": self.method,
            "path_template": self.path_template,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 2),
            "actor_id": self.actor_id,
            "actor_label": self.actor_label,
            "actor_kind": self.actor_kind,
            "source": self.source,
            "ip": self.ip,
            "user_agent": self.user_agent,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
        }
