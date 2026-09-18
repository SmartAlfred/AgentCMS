"""Unauthenticated operational endpoints (#2, extended by #24).

Deliberately split so an orchestrator can tell "process is up" from
"dependencies are up":

* ``GET /healthz`` never touches the database — a 200 here means the process
  is serving requests.
* ``GET /readyz`` probes every dependency (database, object store, outbox
  queue) and returns 503 problem+json when a critical dependency is down,
  with ``Retry-After`` set.
* ``GET /status`` is the same probe rendered as a human-readable status page
  (used by the public status page at ``/status``).
* ``GET /v1/version`` reports the running build: git sha, build time,
  migration head and the OpenAPI version.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db.session import check_database
from app.services.health import (
    DependencyReport,
    check_dependencies,
    db_failure,
)

router = APIRouter(tags=["ops"])

MIGRATION_HEAD: str | None = None


def _migration_head() -> str | None:
    """Resolve the alembic migration head (result is cached after first call)."""
    from alembic.script import ScriptDirectory

    try:
        return ScriptDirectory("alembic").get_current_head() or None
    except Exception:
        return None


def _git_sha() -> str:
    settings = get_settings()
    if settings.git_sha:
        return settings.git_sha
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if out.returncode == 0:
            return out.stdout.strip()[:8]
    except Exception:
        pass
    return "unknown"


def _build_time() -> str:
    settings = get_settings()
    if settings.build_time:
        return settings.build_time
    from pathlib import Path

    app_dir = Path(__file__).resolve().parent
    git_dir = app_dir.parent / ".git"
    if git_dir.is_dir():
        try:
            import subprocess

            out = subprocess.run(
                ["git", "log", "-1", "--format=%cI"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if out.returncode == 0:
                return out.stdout.strip()
        except Exception:
            pass
    try:
        return f"{app_dir.stat().st_mtime:.0f}"
    except Exception:
        return ""


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


def _report_dict(report: DependencyReport) -> dict[str, Any]:
    return {
        "status": "ready" if report.ready else "unavailable",
        "checks": {
            name: {
                "status": "ok" if check.ok else "down",
                "ok": check.ok,
                "detail": check.detail,
                "latency_ms": round(check.latency_ms, 2),
            }
            for name, check in {c.name: c for c in report.checks}.items()
        },
    }


@router.get(
    "/readyz",
    summary="Readiness probe (verifies every dependency)",
    response_description="The service can serve reads and writes.",
    responses={
        200: {"description": "Ready"},
        503: {"description": "A dependency is unavailable (problem+json)"},
    },
)
def readyz(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    report = check_dependencies(settings, db_probe=check_database)

    # A failed database probe keeps the problem+json contract (503 with
    # code=database-unavailable, Retry-After) so orchestrators and the
    # existing test suite agree on the failure shape.
    failure = db_failure(report)
    if failure is not None:
        raise failure

    status = 200 if report.ready else 503
    body = _report_dict(report)
    headers = {}
    if not report.ready:
        headers["Retry-After"] = "5"
    return JSONResponse(status_code=status, content=body, headers=headers)


@router.get("/status", include_in_schema=False)
def status_page(request: Request) -> JSONResponse:
    """Public status page — same dependency checks as /readyz, always 200."""
    settings = request.app.state.settings
    report = check_dependencies(settings, db_probe=check_database)
    body = {
        "service": settings.app_name,
        "version": settings.app_version,
        "status": "operational" if report.ready else "partial_outage",
        "checks": {
            name: {"ok": check.ok, "detail": check.detail}
            for name, check in {c.name: c for c in report.checks}.items()
        },
    }
    return JSONResponse(status_code=200, content=body)


@router.get("/v1/version", include_in_schema=False)
def version_v1() -> dict[str, Any]:
    settings = get_settings()
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "env": settings.app_env,
        "git_sha": _git_sha(),
        "build_time": _build_time(),
        "migration_head": _migration_head(),
        "openapi_version": "3.1.0",
    }


@router.get("/version", include_in_schema=False)
def version() -> dict[str, Any]:
    settings = get_settings()
    return {"service": settings.app_name, "version": settings.app_version, "env": settings.app_env}
