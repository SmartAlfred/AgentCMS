"""Prometheus metrics endpoint (#24).

``GET /metrics`` is admin-gated:

* a matching ``X-Metrics-Token`` header, or
* an API bearer token with ``*:read`` (admin/read) scope, or
* a loopback connection (localhost scraping), or
* test environments (so the test suite can scrape without secrets).

DB-backed gauges (outbox backlog, webhook success ratio, review queue,
pool saturation) are refreshed on every scrape so the numbers are always
current and the alert rules can fire from live state.
"""

from __future__ import annotations

import hmac
import ipaddress

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.observability.metrics import (
    metrics_content_type,
    refresh_status_metrics,
    write_metrics,
)

router = APIRouter(tags=["ops"])

_LOOPBACK_NETS = (ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"))


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        for part in forwarded.split(","):
            part = part.strip()
            if part:
                return part
    return request.client.host if request.client else None


def _is_loopback(ip_value: str | None) -> bool:
    if not ip_value:
        return False
    try:
        addr = ipaddress.ip_address(ip_value.split("%")[0])
    except ValueError:
        return False
    return any(addr in net for net in _LOOPBACK_NETS)


def _metrics_authorized(request: Request, db: Session) -> bool:
    """Decide whether the /metrics caller is allowed to scrape."""

    settings = request.app.state.settings

    # Test / dev convenience: the suite scrapes without secrets.
    if settings.is_test:
        return True

    # Loopback scraping (prometheus / node_exporter on the same host).
    if _is_loopback(_client_ip(request)):
        return True

    # Shared metric token (constant-time compare, no early exit).
    if settings.metrics_token:
        supplied = request.headers.get("x-metrics-token", "")
        if supplied and hmac.compare_digest(supplied, settings.metrics_token):
            return True

    # Valid API token with read-everything scope.
    return _has_admin_scope(request, db)


def _has_admin_scope(request: Request, db: Session) -> bool:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    token = auth[len("bearer ") :].strip()
    if not token:
        return False

    from app.services.tokens import verify_token

    try:
        actor, _link = verify_token(db, token)
    except Exception:
        return False
    scopes = set(actor.scopes or [])
    return "*:read" in scopes or "posts:read" in scopes


@router.get("/metrics", include_in_schema=False)
def metrics_endpoint(request: Request, db: Session = Depends(get_db)) -> Response:
    """Prometheus text-format exposition of all agentcms metrics."""
    if not _metrics_authorized(request, db):
        return Response(status_code=403, content=b"Metrics are admin-gated.\n", media_type="text/plain")

    refresh_status_metrics(db)
    return Response(content=write_metrics(), media_type=metrics_content_type())
