"""Unauthenticated operational endpoints (#2): liveness and readiness.

Deliberately split so an orchestrator can tell "process is up" from
"dependencies are up":

* ``GET /healthz`` never touches the database — a 200 here means the process
  is serving requests.
* ``GET /readyz`` runs ``SELECT 1`` and returns 503 problem+json when the
  database is unreachable, with ``Retry-After`` set.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db.session import check_database

router = APIRouter(tags=["ops"])


@router.get(
    "/healthz",
    summary="Liveness probe (no database access)",
    response_description="The process is up and serving requests.",
    responses={200: {"description": "Process is alive"}},
)
def healthz(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": settings.app_version,
        "env": settings.app_env,
    }


@router.get(
    "/readyz",
    summary="Readiness probe (verifies the database is reachable)",
    response_description="The service can serve reads and writes.",
    responses={
        200: {"description": "Ready"},
        503: {"description": "A dependency is unavailable (problem+json)"},
    },
)
def readyz(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    latency_ms = check_database()
    body: dict[str, Any] = {
        "status": "ready",
        "service": settings.app_name,
        "version": settings.app_version,
        "checks": {
            "database": {
                "status": "ok",
                "latency_ms": round(latency_ms, 2),
            }
        },
    }
    return JSONResponse(status_code=200, content=body)


@router.get("/version", include_in_schema=False)
def version() -> dict[str, Any]:
    settings = get_settings()
    return {"service": settings.app_name, "version": settings.app_version, "env": settings.app_env}
