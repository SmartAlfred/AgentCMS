"""Prometheus metrics endpoint (#24).

``GET /metrics`` is admin-gated:

* a matching ``X-Metrics-Token`` header, or
* an API bearer token with ``*:read`` (admin/read) scope, or
* a loopback connection (localhost scraping), or
* test environments (so the test suite can scrape without secrets).

The loopback exemption is decided by :func:`app.api.client_ip.client_is_loopback`,
the same helper ``require_admin`` uses: ``X-Forwarded-For`` is client-supplied and
is ignored unless the peer is a proxy listed in ``TRUSTED_PROXIES`` (#49).  Behind
a reverse proxy, set ``METRICS_TOKEN`` (what the shipped Prometheus config and the
Caddyfile's ``header_up X-Forwarded-For {remote}`` are built for) -- do not widen
``TRUSTED_PROXIES`` to make a header work.

DB-backed gauges (outbox backlog, webhook success ratio, review queue,
pool saturation) are refreshed on every scrape so the numbers are always
current and the alert rules can fire from live state.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.client_ip import client_is_loopback
from app.db.session import get_db
from app.observability.metrics import (
    metrics_content_type,
    refresh_status_metrics,
    write_metrics,
)

router = APIRouter(tags=["ops"])


def _metrics_authorized(request: Request, db: Session) -> bool:
    """Decide whether the /metrics caller is allowed to scrape."""

    settings = request.app.state.settings

    # Test / dev convenience: the suite scrapes without secrets.
    if settings.is_test:
        return True

    # Loopback scraping (prometheus / node_exporter on the same host).  The peer
    # address decides this; `X-Forwarded-For` only counts behind a proxy the
    # operator has declared in TRUSTED_PROXIES (#49).
    if client_is_loopback(request, settings.trusted_proxies):
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
