"""Admin review queue API (#16).

Routes:
* ``GET  /v1/admin/reviews``              — list pending reviews
* ``GET  /v1/admin/reviews/{id}`'         — get a single review
* ``POST /v1/admin/reviews/{id}/approve`` — approve and publish
* ``POST /v1/admin/reviews/{id}/reject``  — reject with reason
* ``POST /v1/admin/sites/{slug}/trust-mode`` — enable trust mode
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import AuthContext, require_auth
from app.db.session import get_db

router = APIRouter(prefix="/admin", tags=["admin", "reviews"])

DbSession = Annotated[Session, Depends(get_db)]


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class ApproveBody(BaseModel):
    comment: str | None = None


class RejectBody(BaseModel):
    reason: str = ""


class TrustModeBody(BaseModel):
    expires_in_minutes: int = 60


# ---------------------------------------------------------------------------
# GET /v1/admin/reviews — list pending reviews
# ---------------------------------------------------------------------------


@router.get(
    "/reviews",
    summary="List pending reviews",
)
def list_reviews_endpoint(
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
    site_slug: str = Query(..., description="Site slug to list reviews for"),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
) -> Response:
    from app.services.review import list_pending_reviews

    items, next_cursor = list_pending_reviews(db, site_slug, limit=limit, cursor=cursor)

    items_data = []
    for r in items:
        items_data.append(
            {
                "id": str(r.id),
                "post_id": str(r.post_id),
                "site_id": str(r.site_id),
                "requested_by_actor_id": str(r.requested_by_actor_id),
                "status": r.status,
                "comment": r.comment,
                "reject_reason": r.reject_reason,
                "reviewed_by_actor_id": str(r.reviewed_by_actor_id) if r.reviewed_by_actor_id else None,
                "decided_at": r.decided_at.isoformat() if r.decided_at else None,
                "preview_token": r.preview_token,
                "snapshot_title": r.snapshot_title,
                "snapshot_diff": r.snapshot_diff,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
        )

    return Response(
        content=json.dumps(
            {
                "items": items_data,
                "next_cursor": next_cursor,
                "count": len(items_data),
            },
            default=str,
        ),
        media_type="application/json",
    )


# ---------------------------------------------------------------------------
# GET /v1/admin/reviews/{id} — get a single review
# ---------------------------------------------------------------------------


@router.get(
    "/reviews/{review_id}",
    summary="Get a single review",
)
def get_review_endpoint(
    review_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
) -> Response:
    from app.services.review import get_review

    review = get_review(db, review_id)
    data = {
        "id": str(review.id),
        "post_id": str(review.post_id),
        "site_id": str(review.site_id),
        "requested_by_actor_id": str(review.requested_by_actor_id),
        "status": review.status,
        "comment": review.comment,
        "reject_reason": review.reject_reason,
        "reviewed_by_actor_id": str(review.reviewed_by_actor_id) if review.reviewed_by_actor_id else None,
        "decided_at": review.decided_at.isoformat() if review.decided_at else None,
        "preview_token": review.preview_token,
        "snapshot_title": review.snapshot_title,
        "snapshot_diff": review.snapshot_diff,
        "created_at": review.created_at.isoformat() if review.created_at else None,
    }
    return Response(content=json.dumps(data, default=str), media_type="application/json")


# ---------------------------------------------------------------------------
# POST /v1/admin/reviews/{id}/approve — approve and publish
# ---------------------------------------------------------------------------


@router.post(
    "/reviews/{review_id}/approve",
    summary="Approve a review and publish the post",
)
def approve_review_endpoint(
    review_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
    body: ApproveBody = Body(default=ApproveBody()),
) -> Response:
    comment = body.comment

    from app.services.post import _post_to_dict
    from app.services.review import approve_review

    _review, post = approve_review(
        db,
        review_id,
        reviewer_actor_id=auth.actor_id,
        comment=comment,
        source="dashboard",
        audit_ctx={
            "actor_label": auth.label,
            "actor_kind": "human",
            "request_id": getattr(request.state, "request_id", None),
            "ip": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent"),
        },
    )
    db.commit()

    site_slug = post.site.slug if post.site else "blog"
    data = _post_to_dict(post, site_slug, session=db)
    return Response(
        content=json.dumps(data, default=str),
        status_code=200,
        media_type="application/json",
    )


# ---------------------------------------------------------------------------
# POST /v1/admin/reviews/{id}/reject — reject with reason
# ---------------------------------------------------------------------------


@router.post(
    "/reviews/{review_id}/reject",
    summary="Reject a review with a reason",
)
def reject_review_endpoint(
    review_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
    body: RejectBody = Body(default=RejectBody()),
) -> Response:
    reason = body.reason

    from app.services.review import reject_review

    review = reject_review(
        db,
        review_id,
        reviewer_actor_id=auth.actor_id,
        reason=reason,
        source="dashboard",
        audit_ctx={
            "actor_label": auth.label,
            "actor_kind": "human",
            "request_id": getattr(request.state, "request_id", None),
            "ip": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent"),
        },
    )
    db.commit()

    data = {
        "id": str(review.id),
        "status": review.status,
        "reject_reason": review.reject_reason,
        "post_id": str(review.post_id),
    }
    return Response(
        content=json.dumps(data, default=str),
        status_code=200,
        media_type="application/json",
    )


# ---------------------------------------------------------------------------
# POST /v1/admin/sites/{slug}/trust-mode — enable trust mode
# ---------------------------------------------------------------------------


@router.post(
    "/sites/{site_slug}/trust-mode",
    summary="Enable trust mode (bypass review) for a fixed period",
)
def enable_trust_mode_endpoint(
    site_slug: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
    body: TrustModeBody = Body(default=TrustModeBody()),
) -> Response:
    from datetime import UTC, datetime, timedelta

    from app.models.site import Site

    expires_in_minutes = body.expires_in_minutes

    site = db.query(Site).filter(Site.slug == site_slug).first()
    if site is None:
        return Response(
            content=json.dumps({"error": f"Site '{site_slug}' not found"}),
            status_code=404,
            media_type="application/json",
        )

    site.trust_mode_expires_at = datetime.now(UTC) + timedelta(minutes=expires_in_minutes)
    db.commit()

    from app.services.audit import record_event

    record_event(
        db,
        action="trust_mode.activated",
        actor_id=auth.actor_id,
        actor_label=auth.label,
        actor_kind="human",
        source="dashboard",
        target_type="site",
        target_id=str(site.id),
        request_id=getattr(request.state, "request_id", None),
        event_metadata={
            "expires_in_minutes": expires_in_minutes,
            "expires_at": site.trust_mode_expires_at.isoformat(),
        },
    )
    db.commit()

    return Response(
        content=json.dumps(
            {
                "status": "trust_mode_activated",
                "site_slug": site_slug,
                "expires_at": site.trust_mode_expires_at.isoformat(),
                "expires_in_minutes": expires_in_minutes,
            },
            default=str,
        ),
        status_code=200,
        media_type="application/json",
    )
