"""Human dashboard (Nocturne) — supervise, verify, revoke (#18).

Server-rendered HTML + htmx calling the same /v1 and /v1/admin endpoints.
Cookie session with SameSite=Strict, CSRF token on all mutations.
No dashboard-only behaviour that isn't reachable via documented HTTP.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

from app.dashboard.routes import (  # noqa: E402
    auth_router,
    dashboard_router,
)

router.include_router(auth_router)
router.include_router(dashboard_router)
