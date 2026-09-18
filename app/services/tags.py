"""Tag management service (#19).

Tag listing with post counts, and tag merge (admin operation).

Key design decisions:
* Tag merge rewrites ``post_tags`` for all affected posts.
* Tag changes do NOT create content revisions (tag changes don't bump
  content revisions per the ticket).
* Each affected post emits one audit event (not one giant event).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.domain.errors import NotFoundError
from app.models.post import Post
from app.models.site import Site
from app.models.tag import PostTag, Tag


class TagNotFoundError(NotFoundError):
    code = "tag-not-found"
    title = "Tag not found"

    def __init__(self, tag_slug: str) -> None:
        super().__init__(
            f"No tag with slug '{tag_slug}' exists.",
            hint="Use GET /v1/sites/{site}/tags to list available tags.",
            extra={"tag": tag_slug},
        )


def list_tags(session: Session, site_slug: str) -> list[dict[str, Any]]:
    """List all tags for a site with post counts."""
    _resolve_site(session, site_slug)  # validate site exists

    rows = (
        session.query(
            Tag.id,
            Tag.slug,
            Tag.name,
            Tag.created_at,
            func.count(PostTag.post_id).label("post_count"),
        )
        .outerjoin(PostTag, PostTag.tag_id == Tag.id)
        .outerjoin(Post, Post.id == PostTag.post_id)
        .filter(Post.deleted_at.is_(None))
        .group_by(Tag.id, Tag.slug, Tag.name, Tag.created_at)
        .order_by(Tag.slug)
        .all()
    )

    return [
        {
            "id": str(row.id),
            "slug": row.slug,
            "name": row.name,
            "post_count": row.post_count,
            "created_at": row.created_at,
        }
        for row in rows
    ]


def merge_tags(
    session: Session,
    site_slug: str,
    source_tag_slug: str,
    target_tag_slug: str,
    *,
    actor_id: uuid.UUID | None = None,
    audit_ctx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge source_tag into target_tag.

    Rewrites post_tags for all affected posts, deletes the source tag,
    and emits one audit event per affected post.
    """
    site = _resolve_site(session, site_slug)

    source_tag = session.query(Tag).filter(Tag.slug == source_tag_slug).first()
    if source_tag is None:
        raise TagNotFoundError(source_tag_slug)

    target_tag = session.query(Tag).filter(Tag.slug == target_tag_slug).first()
    if target_tag is None:
        raise TagNotFoundError(target_tag_slug)

    # Find posts that have the source tag (scoped to this site)
    affected_post_ids = [
        row[0]
        for row in (
            session.query(PostTag.post_id)
            .join(Post, Post.id == PostTag.post_id)
            .filter(PostTag.tag_id == source_tag.id, Post.site_id == site.id, Post.deleted_at.is_(None))
            .all()
        )
    ]

    affected_count = 0
    for post_id in affected_post_ids:
        # Check if the post already has the target tag
        already_has_target = (
            session.query(PostTag).filter(PostTag.post_id == post_id, PostTag.tag_id == target_tag.id).first()
        )

        if already_has_target is None:
            # Add the target tag
            session.add(PostTag(post_id=post_id, tag_id=target_tag.id))

        # Remove the source tag
        session.query(PostTag).filter(PostTag.post_id == post_id, PostTag.tag_id == source_tag.id).delete(
            synchronize_session=False
        )

        affected_count += 1

        # Emit audit event per affected post
        from app.services.audit import record_event

        ctx = audit_ctx or {}
        record_event(
            session,
            action="tag.merged",
            actor_id=actor_id,
            actor_label=ctx.get("actor_label"),
            actor_kind=ctx.get("actor_kind", "human"),
            source=ctx.get("source", "api"),
            target_type="post",
            target_id=str(post_id),
            event_metadata={
                "source_tag": source_tag_slug,
                "target_tag": target_tag_slug,
            },
            request_id=ctx.get("request_id"),
            ip=ctx.get("ip"),
            user_agent=ctx.get("user_agent"),
        )

    # Delete the source tag if no posts reference it anymore
    remaining = session.query(func.count(PostTag.post_id)).filter(PostTag.tag_id == source_tag.id).scalar()
    if remaining == 0:
        session.delete(source_tag)

    session.commit()

    return {
        "source_tag": source_tag_slug,
        "target_tag": target_tag_slug,
        "affected_posts": affected_count,
    }


def _resolve_site(session: Session, site_slug: str) -> Site:
    site = session.query(Site).filter(Site.slug == site_slug).first()
    if site is None:
        from app.domain.errors import SiteNotFoundError

        raise SiteNotFoundError(site_slug)
    return site
