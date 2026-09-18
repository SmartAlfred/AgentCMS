"""Tag endpoints (#19).

GET  /v1/sites/{site}/tags     — list tags with counts
POST /v1/sites/{site}/tags/{tag}/merge — merge one tag into another (admin)
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth import AuthContext, require_auth
from app.db.session import get_db
from app.services.tags import list_tags, merge_tags

from .schemas import TagListResponse, TagMergeRequest, TagMergeResponse, TagRead

router = APIRouter()

DbSession = Annotated[Session, Depends(get_db)]


@router.get(
    "/sites/{site_slug}/tags",
    summary="List tags with post counts",
    tags=["tags"],
    response_model=TagListResponse,
)
def list_tags_endpoint(
    site_slug: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
) -> TagListResponse:
    """List all tags for a site, with post counts."""
    items = list_tags(db, site_slug)
    return TagListResponse(
        items=[TagRead(**item) for item in items],
        count=len(items),
    )


@router.post(
    "/sites/{site_slug}/tags/{tag_slug}/merge",
    summary="Merge one tag into another",
    tags=["tags"],
    response_model=TagMergeResponse,
)
def merge_tags_endpoint(
    site_slug: str,
    tag_slug: str,
    body: TagMergeRequest,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
) -> TagMergeResponse:
    """Merge source tag into target tag.

    Rewrites post_tags for all affected posts. Tag changes do NOT create
    content revisions but emit audit events per affected post.
    """
    result = merge_tags(
        db,
        site_slug,
        source_tag_slug=tag_slug,
        target_tag_slug=body.target_tag,
        actor_id=auth.actor_id,
        audit_ctx={
            "actor_label": auth.label,
            "actor_kind": "machine",
            "source": "api",
            "request_id": getattr(request.state, "request_id", None),
            "ip": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent"),
        },
    )
    return TagMergeResponse(**result)
