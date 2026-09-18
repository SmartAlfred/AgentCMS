"""Mounted versioned API surface (#2 stub, implemented by #4, #5, #13).

The router lives here so the application factory has a single seam for
``/v1``; the route modules themselves are imported by #4 (posts), #5 (tokens)
and later tickets, which keeps the ``/v1`` prefix in one place.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

router = APIRouter()

from app.api.v1.admin_reviews import router as admin_reviews_router  # noqa: E402
from app.api.v1.admin_tokens import router as admin_tokens_router  # noqa: E402
from app.api.v1.audit import router as audit_router  # noqa: E402
from app.api.v1.posts import router as posts_router  # noqa: E402

router.include_router(posts_router)
router.include_router(admin_tokens_router)
router.include_router(audit_router)
router.include_router(admin_reviews_router)


@router.get(
    "/info",
    summary="API version info",
    tags=["ops"],
    response_description="The API version and status.",
)
def v1_info() -> dict[str, Any]:
    """Return version metadata for the v1 API surface."""
    return {"version": "v1", "status": "active"}
