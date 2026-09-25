"""Review service (#16).

Manages the human approval gate for posts in ``require_review`` sites.
When a post is published in ``require_review`` mode, it enters
``pending_review`` status instead of being immediately published.

Key operations:
* ``create_review`` — create a review request for a post
* ``approve_review`` — approve and publish, idempotent
* ``reject_review`` — reject and return to draft with reason
* ``list_pending_reviews`` — list all pending reviews for a site
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.domain.errors import (
    DomainError,
    PostNotFoundError,
    SiteNotFoundError,
)
from app.models.post import Post
from app.models.review import Review
from app.models.site import Site


class ReviewNotFoundError(DomainError):
    code = "review-not-found"
    title = "Review not found"
    status_code = 404

    def __init__(self, review_id: str) -> None:
        super().__init__(
            f"No pending review with id '{review_id}'.",
            hint="Use GET /v1/admin/reviews to list pending reviews.",
            extra={"review_id": review_id},
        )


class ReviewAlreadyDecidedError(DomainError):
    code = "review-already-decided"
    title = "Review already decided"
    status_code = 409

    def __init__(self, review_id: str, current_status: str) -> None:
        super().__init__(
            f"Review '{review_id}' has already been {current_status}.",
            hint="Reviews can only be decided once.",
            extra={"review_id": review_id, "current_status": current_status},
        )


def _resolve_site(session: Session, site_slug: str) -> Site:
    site = session.query(Site).filter(Site.slug == site_slug).first()
    if site is None:
        raise SiteNotFoundError(site_slug)
    return site


def _resolve_post(session: Session, post_id: str) -> Post:
    try:
        post_uuid = uuid.UUID(post_id)
        post = session.query(Post).filter(Post.id == post_uuid).first()
    except ValueError:
        post = session.query(Post).filter(Post.slug == post_id).first()
    if post is None:
        raise PostNotFoundError(post_id)
    return post


def is_trust_mode_active(site: Site) -> bool:
    """Check if a site is currently in trust mode (bypasses review)."""
    if site.trust_mode_expires_at is None:
        return False
    now = datetime.now(UTC)
    expires = site.trust_mode_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return now < expires


def site_requires_review(site: Site) -> bool:
    """Check if the site requires human review before publishing."""
    if site.publish_mode == "auto" and not is_trust_mode_active(site):
        return False
    if site.publish_mode == "require_review":
        return not is_trust_mode_active(site)
    return False


def create_review(
    session: Session,
    *,
    post: Post,
    site: Site,
    actor_id: uuid.UUID,
    source: str = "api",
    audit_ctx: dict[str, Any] | None = None,
) -> Review:
    """Create a review request for a post.

    The post is moved to ``pending_review`` status and a Review row is created
    with a signed preview token.  Returns the Review object.
    """
    from app.services.preview import create_preview_token

    review = Review(
        id=uuid.uuid4(),
        post_id=post.id,
        site_id=site.id,
        requested_by_actor_id=actor_id,
        status="pending_review",
        preview_token=create_preview_token(post.id),
        snapshot_body_md=post.body_md,
        snapshot_title=post.title,
    )
    session.add(review)
    session.flush()

    # Update post status
    post.status = "pending_review"
    post.review_status = "pending_review"
    post.review_id = review.id
    session.flush()

    # Audit
    from app.services.audit import compute_content_hash as audit_hash
    from app.services.audit import record_event

    ctx = audit_ctx or {}
    record_event(
        session,
        action="review.requested",
        actor_id=actor_id,
        actor_label=ctx.get("actor_label"),
        actor_kind=ctx.get("actor_kind", "human"),
        source=source,
        target_type="review",
        target_id=str(review.id),
        after_hash=audit_hash(post.body_md),
        revision=post.revision_count,
        request_id=ctx.get("request_id"),
        ip=ctx.get("ip"),
        user_agent=ctx.get("user_agent"),
        event_metadata={"post_id": str(post.id), "site_slug": site.slug},
    )

    return review


def approve_review(
    session: Session,
    review_id: str,
    *,
    reviewer_actor_id: uuid.UUID,
    comment: str | None = None,
    source: str = "dashboard",
    audit_ctx: dict[str, Any] | None = None,
) -> tuple[Review, Post]:
    """Approve a review request and publish the post.

    Idempotent: if the review is already approved, returns the existing state.
    Returns ``(review, post)``.
    """
    review = (
        session.query(Review)
        .filter(Review.id == uuid.UUID(review_id) if _is_uuid(review_id) else Review.id.is_(None))
        .first()
    )
    if review is None:
        # Try by finding through post
        review = session.query(Review).filter(Review.id == uuid.UUID(review_id)).first()
    if review is None:
        raise ReviewNotFoundError(review_id)

    if review.status == "approved":
        # Idempotent — already approved
        post = session.query(Post).options(joinedload(Post.site)).filter(Post.id == review.post_id).first()
        assert post is not None
        return review, post

    if review.status != "pending_review":
        raise ReviewAlreadyDecidedError(review_id, review.status)

    # Approve
    review.status = "approved"
    review.reviewed_by_actor_id = reviewer_actor_id
    review.decided_at = datetime.now(UTC)
    if comment:
        review.comment = comment
    session.flush()

    # Publish the post
    post = session.query(Post).options(joinedload(Post.site)).filter(Post.id == review.post_id).first()
    assert post is not None

    post.status = "published"
    post.published_at = datetime.now(UTC)
    post.review_status = "approved"
    post.review_comment = comment
    post.reviewed_by_actor_id = reviewer_actor_id
    post.reviewed_at = datetime.now(UTC)
    post.revision_count += 1

    # Create a revision
    from app.services.post import _create_revision

    _create_revision(session, post, actor_id=reviewer_actor_id, source=source)
    session.flush()

    # Audit
    from app.services.audit import compute_content_hash as audit_hash
    from app.services.audit import record_event

    ctx = audit_ctx or {}
    record_event(
        session,
        action="review.approved",
        actor_id=reviewer_actor_id,
        actor_label=ctx.get("actor_label"),
        actor_kind=ctx.get("actor_kind", "human"),
        source=source,
        target_type="review",
        target_id=str(review.id),
        after_hash=audit_hash(post.body_md),
        revision=post.revision_count,
        request_id=ctx.get("request_id"),
        ip=ctx.get("ip"),
        user_agent=ctx.get("user_agent"),
        event_metadata={
            "post_id": str(post.id),
            "reviewer_actor_id": str(reviewer_actor_id),
        },
    )

    return review, post


def reject_review(
    session: Session,
    review_id: str,
    *,
    reviewer_actor_id: uuid.UUID,
    reason: str,
    source: str = "dashboard",
    audit_ctx: dict[str, Any] | None = None,
) -> Review:
    """Reject a review request.  The post returns to draft with the reason.

    The reject reason is surfaced to the agent verbatim via
    ``GET /v1/posts/{id}``.
    """
    review = session.query(Review).filter(Review.id == uuid.UUID(review_id)).first()
    if review is None:
        raise ReviewNotFoundError(review_id)

    if review.status != "pending_review":
        raise ReviewAlreadyDecidedError(review_id, review.status)

    review.status = "rejected"
    review.reviewed_by_actor_id = reviewer_actor_id
    review.decided_at = datetime.now(UTC)
    review.reject_reason = reason
    session.flush()

    # Move post back to draft
    post = session.query(Post).filter(Post.id == review.post_id).first()
    assert post is not None

    post.status = "draft"
    post.review_status = "rejected"
    post.review_comment = reason
    post.reviewed_by_actor_id = reviewer_actor_id
    post.reviewed_at = datetime.now(UTC)
    session.flush()

    # Audit
    from app.services.audit import compute_content_hash as audit_hash
    from app.services.audit import record_event

    ctx = audit_ctx or {}
    record_event(
        session,
        action="review.rejected",
        actor_id=reviewer_actor_id,
        actor_label=ctx.get("actor_label"),
        actor_kind=ctx.get("actor_kind", "human"),
        source=source,
        target_type="review",
        target_id=str(review.id),
        after_hash=audit_hash(post.body_md),
        revision=post.revision_count,
        request_id=ctx.get("request_id"),
        ip=ctx.get("ip"),
        user_agent=ctx.get("user_agent"),
        event_metadata={
            "post_id": str(post.id),
            "reject_reason": reason,
        },
    )

    return review


def list_pending_reviews(
    session: Session,
    site_slug: str,
    *,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[Review], str | None]:
    """List pending reviews for a site.

    Returns ``(items, next_cursor)``.
    """
    site = _resolve_site(session, site_slug)

    query = (
        session.query(Review)
        .filter(Review.site_id == site.id, Review.status == "pending_review")
        .order_by(Review.created_at.desc(), Review.id.desc())
    )

    if cursor:
        try:
            parts = cursor.split("|", 1)
            cursor_ts = datetime.fromisoformat(parts[0])
            cursor_id = uuid.UUID(parts[1])
            from sqlalchemy import or_

            query = query.filter(
                or_(
                    Review.created_at < cursor_ts,
                    (Review.created_at == cursor_ts) & (Review.id < cursor_id),
                )
            )
        except (ValueError, IndexError):
            pass

    items = query.limit(limit + 1).all()
    next_cursor: str | None = None
    if len(items) > limit:
        last = items[-2]
        next_cursor = f"{last.created_at.isoformat()}|{last.id}"
        items = items[:limit]

    return items, next_cursor


def get_review(session: Session, review_id: str) -> Review:
    """Get a single review by ID."""
    review = session.query(Review).filter(Review.id == uuid.UUID(review_id)).first()
    if review is None:
        raise ReviewNotFoundError(review_id)
    return review


def _is_uuid(value: str) -> bool:
    """Check if a string looks like a UUID."""
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False
