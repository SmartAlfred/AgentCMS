"""Public read service (ticket #8).

Efficient queries for the public read surface: published posts, site index,
tag-filtered listings, and machine-readable mirrors.  All queries are scoped
to ``status='published'`` and exclude trashed posts — drafts are never
leaked.

Design principles:
* No N+1: tags are fetched in a single batch query per page.
* ETag is derived from ``content_hash`` + ``updated_at``.
* ``last_modified`` comes from ``updated_at`` (UTC).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models.post import Post
from app.models.redirect import Redirect
from app.models.site import Site
from app.models.tag import PostTag, Tag

PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def _resolve_site(session: Session, site_slug: str) -> Site:
    site = session.query(Site).filter(Site.slug == site_slug).first()
    if site is None:
        return None  # type: ignore[return-value]
    return site


def get_published_post(session: Session, site_slug: str, slug: str) -> Post | None:
    """Return a published, non-trashed post, or None."""
    site = _resolve_site(session, site_slug)
    if site is None:
        return None
    post = (
        session.query(Post)
        .options(joinedload(Post.tag_links).joinedload(PostTag.tag))
        .filter(
            Post.site_id == site.id,
            Post.slug == slug,
            Post.status == "published",
            Post.deleted_at.is_(None),
        )
        .first()
    )
    return post


def check_redirect(session: Session, site_slug: str, slug: str) -> Redirect | None:
    """Check if a slug has been renamed and a redirect exists."""
    site = _resolve_site(session, site_slug)
    if site is None:
        return None
    return session.query(Redirect).filter(Redirect.site_id == site.id, Redirect.old_slug == slug).first()


def list_published_posts(
    session: Session,
    site_slug: str,
    *,
    page: int = 1,
    tag: str | None = None,
    page_size: int = PAGE_SIZE,
) -> tuple[list[Post], int, int]:
    """List published posts for a site.  Returns (posts, total_count, total_pages)."""
    site = _resolve_site(session, site_slug)
    if site is None:
        return [], 0, 0

    page_size = max(1, min(page_size, MAX_PAGE_SIZE))

    query = (
        session.query(Post)
        .options(
            selectinload(Post.tag_links).selectinload(PostTag.tag),
        )
        .filter(
            Post.site_id == site.id,
            Post.status == "published",
            Post.deleted_at.is_(None),
        )
    )

    if tag:
        query = (
            query.join(PostTag, PostTag.post_id == Post.id)
            .join(Tag, Tag.id == PostTag.tag_id)
            .filter(Tag.slug == tag)
        )

    # Count
    count_query = session.query(func.count(Post.id)).filter(
        Post.site_id == site.id,
        Post.status == "published",
        Post.deleted_at.is_(None),
    )
    if tag:
        count_query = (
            count_query.join(PostTag, PostTag.post_id == Post.id)
            .join(Tag, Tag.id == PostTag.tag_id)
            .filter(Tag.slug == tag)
        )
    total = count_query.scalar() or 0

    total_pages = max(1, -(-total // page_size))  # ceil division
    page = max(1, min(page, total_pages))

    offset = (page - 1) * page_size
    posts = (
        query.order_by(Post.published_at.desc().nullslast(), Post.id.desc())
        .offset(offset)
        .limit(page_size)
        .all()
    )

    return posts, total, total_pages


def list_published_posts_for_site(session: Session, site_id: uuid.UUID) -> list[Post]:
    """Return all published posts for a site (for feeds/sitemap)."""
    return (
        session.query(Post)
        .filter(
            Post.site_id == site_id,
            Post.status == "published",
            Post.deleted_at.is_(None),
        )
        .order_by(Post.published_at.desc().nullslast(), Post.id.desc())
        .all()
    )


def get_site(session: Session, site_slug: str) -> Site | None:
    """Return a site by slug, or None."""
    return session.query(Site).filter(Site.slug == site_slug).first()


def get_tags_for_post_ids(session: Session, post_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    """Batch-fetch tags for a list of post IDs.  Returns {post_id: [tag_slugs]}."""
    if not post_ids:
        return {}
    rows = (
        session.query(PostTag.post_id, Tag.slug)
        .join(Tag, Tag.id == PostTag.tag_id)
        .filter(PostTag.post_id.in_(post_ids))
        .all()
    )
    result: dict[uuid.UUID, list[str]] = {}
    for post_id, slug in rows:
        result.setdefault(post_id, []).append(slug)
    return result


def compute_etag(post: Post) -> str:
    """Compute an ETag from content_hash and updated_at."""
    raw = f"{post.content_hash or ''}|{post.updated_at.isoformat() if post.updated_at else ''}"
    import hashlib

    return hashlib.sha256(raw.encode()).hexdigest()


def post_to_public_dict(post: Post, site_slug: str, *, tags: list[str] | None = None) -> dict[str, Any]:
    """Build a public-safe dict for a post (no internal fields)."""
    if tags is None:
        tags = []
    return {
        "slug": post.slug,
        "title": post.title,
        "excerpt": post.excerpt or "",
        "body_md": post.body_md,
        "tags": tags,
        "published_at": post.published_at.isoformat() if post.published_at else None,
        "updated_at": post.updated_at.isoformat() if post.updated_at else None,
        "content_hash": post.content_hash,
        "word_count": post.word_count,
        "reading_time_minutes": post.reading_time_minutes,
        "author_label": post.author_label,
    }
