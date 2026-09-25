"""Mounted versioned API surface (#2 stub, implemented by #4, #5, #13).

The router lives here so the application factory has a single seam for
``/v1``; the route modules themselves are imported by #4 (posts), #5 (tokens)
and later tickets, which keeps the ``/v1`` prefix in one place.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

router = APIRouter()

from app.api.admin_auth import require_admin  # noqa: E402
from app.api.v1.admin_moderation import router as admin_moderation_router  # noqa: E402
from app.api.v1.admin_reviews import router as admin_reviews_router  # noqa: E402
from app.api.v1.admin_tokens import router as admin_tokens_router  # noqa: E402
from app.api.v1.assets import router as assets_router  # noqa: E402
from app.api.v1.audit import router as audit_router  # noqa: E402
from app.api.v1.events import router as events_router  # noqa: E402
from app.api.v1.export import router as export_router  # noqa: E402
from app.api.v1.posts import router as posts_router  # noqa: E402
from app.api.v1.search import router as search_router  # noqa: E402
from app.api.v1.sites import router as sites_router  # noqa: E402
from app.api.v1.tags import router as tags_router  # noqa: E402
from app.api.v1.webhooks import router as webhooks_router  # noqa: E402

router.include_router(posts_router)
router.include_router(admin_tokens_router, dependencies=[Depends(require_admin)])
router.include_router(audit_router, dependencies=[Depends(require_admin)])
router.include_router(admin_reviews_router, dependencies=[Depends(require_admin)])
router.include_router(admin_moderation_router, dependencies=[Depends(require_admin)])
router.include_router(search_router)
router.include_router(sites_router)
router.include_router(tags_router)
router.include_router(assets_router)
router.include_router(webhooks_router, dependencies=[Depends(require_admin)])
router.include_router(events_router)
router.include_router(export_router)


@router.get(
    "/info",
    summary="API version info",
    tags=["ops"],
    response_description="The API version and status.",
)
def v1_info() -> dict[str, Any]:
    """Return version metadata for the v1 API surface."""
    return {"version": "v1", "status": "active"}
