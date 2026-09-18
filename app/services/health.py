"""Shared dependency checks for readiness/status (#24).

One implementation powers ``GET /readyz``, ``GET /status`` and the status
page so the orchestrator and the public status page always agree.

Dependencies probed:

* ``database``   — ``SELECT 1`` round-trip (critical).
* ``object_store`` — writable S3-compatible bucket / local media directory
  (critical when object storage is configured).
* ``queue``      — the webhook event outbox: table exists and the dispatcher
  is not falling behind (critical when webhooks are in use).

Each check is fault-isolated: one failing check never prevents the others
from reporting their own status.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.config import Settings

_CHECK_TIMEOUT_S = 2.0


@dataclass
class DependencyStatus:
    """Result of one dependency probe."""

    name: str
    ok: bool
    detail: str = ""
    latency_ms: float = 0.0
    critical: bool = True
    exc: Exception | None = None


@dataclass
class DependencyReport:
    """Aggregate result plus the winning overall status."""

    checks: list[DependencyStatus] = field(default_factory=list)
    ready: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "ready" if self.ready else "unavailable",
            "checks": {
                c.name: {"ok": c.ok, "detail": c.detail, "latency_ms": round(c.latency_ms, 2)}
                for c in self.checks
            },
        }


def check_dependencies(
    settings: Settings, *, db_probe: Callable[[], float] | None = None, require_queue: bool = True
) -> DependencyReport:
    """Probe every configured dependency and assemble the report.

    ``db_probe`` is injectable so API layers can route it through their own
    reference (which tests may monkeypatch).
    """
    checks: list[DependencyStatus] = []

    checks.append(_check_db(db_probe))

    # The object store and queue are advisory in the readiness sense: a down
    # object store only affects uploads and a stale queue only delays webhook
    # delivery — reads/writes keep working.  They surface with full detail on
    # /readyz and /status, but only the database gates "ready" (matching the
    # pre-#24 contract that /readyz 503s exactly when the DB is unreachable).
    if require_queue:
        queue = _check_queue()
        queue.critical = False
        checks.append(queue)
    object_store = _check_object_store(settings)
    object_store.critical = False
    checks.append(object_store)

    hard = [c for c in checks if c.critical]
    ready = all(c.ok for c in hard)
    return DependencyReport(checks=checks, ready=ready)


def db_failure(report: DependencyReport) -> Exception | None:
    """Return the database check's exception if the database is down."""
    for check in report.checks:
        if check.name == "database" and not check.ok and check.exc is not None:
            return check.exc
    return None


def _check_db(db_probe: Callable[[], float] | None) -> DependencyStatus:
    if db_probe is None:
        from app.db.session import check_database

        db_probe = check_database

    try:
        latency = db_probe()
        return DependencyStatus("database", True, latency_ms=latency)
    except Exception as exc:
        return DependencyStatus("database", False, detail=str(exc)[:200], exc=exc)


def _check_object_store(settings: Settings) -> DependencyStatus:
    """Verify the media object store is reachable.

    The media stack uses S3-compatible presigned PUTs; with no credentials it
    runs in dev/mock mode, which is a valid (non-critical) state.
    """
    if not settings.s3_access_key_id or not settings.s3_secret_access_key:
        return DependencyStatus(
            "object_store",
            True,
            detail="dev mode: no S3 credentials configured",
        )
    return _check_s3(settings)


def _check_s3(settings: Settings) -> DependencyStatus:
    """Issue a HEAD request against the S3 bucket root (auth optional)."""
    import httpx

    endpoint = settings.s3_endpoint_url or (
        f"https://{settings.s3_bucket}.s3.{settings.s3_region}.amazonaws.com"
    )
    url = f"{endpoint}/{settings.s3_bucket}"
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=_CHECK_TIMEOUT_S) as client:
            resp = client.head(url)
        ok = resp.status_code < 500
        return DependencyStatus(
            "object_store",
            ok,
            detail=f"s3 {url} -> {resp.status_code}",
            latency_ms=(time.perf_counter() - started) * 1000,
        )
    except Exception as exc:
        return DependencyStatus("object_store", False, detail=str(exc)[:200])


def _check_queue() -> DependencyStatus:
    """Outbox queue backlog + dispatcher liveness."""
    from sqlalchemy import text

    from app.db.session import get_engine

    started = time.perf_counter()
    try:
        with get_engine().connect() as connection:
            conn = connection.execute(
                text(
                    "SELECT count(*) AS pending, "
                    "       COALESCE(EXTRACT(EPOCH FROM (now() - min(created_at))), 0) AS oldest_s "
                    "FROM event_outbox WHERE dispatched = false"
                )
            )
            row = conn.fetchone()
        pending = int(row[0]) if row else 0
        oldest_s = float(row[1]) if row and row[1] is not None else 0.0
        ok = pending <= 1000 and oldest_s <= 15 * 60
        detail = f"pending={pending}, oldest={int(oldest_s)}s"
        return DependencyStatus(
            "queue",
            ok,
            detail=detail,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
    except Exception as exc:
        return DependencyStatus("queue", False, detail=str(exc)[:200])


def _local_media_dir() -> str:
    """Best-effort local media root for the filesystem store probe."""
    for candidate in ("media", "static/media", "public/media"):
        if os.path.isdir(candidate):
            return candidate
    return "."
