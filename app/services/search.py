"""Search & filtering service (#19).

Postgres-native full-text search with weighted tsvector (A/B/C/D),
ts_rank_cd ordering, ts_headline snippets, pg_trgm for fuzzy matching,
and faceted filter support.

Key design decisions:
* ``tsvector`` lives on the ``posts`` table and is kept in sync by a
  trigger that fires on INSERT/UPDATE of title/body_md/excerpt and on
  any change to ``post_tags``.
* ``ts_rank_cd`` is used for ranking (coverage density).
* ``ts_headline`` snippets are capped at 20 words and use ``<mark>``
  tags for the dashboard (plain text for the JSON API).
* Cursor pagination uses ``published_at DESC, id DESC`` for stability.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session

from app.domain.errors import InvalidCursorError
from app.models.post import Post
from app.models.site import Site
from app.models.tag import PostTag, Tag

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def _strip_html_tags(s: str) -> str:
    """Remove any HTML tags from a string."""
    return re.sub(r"<[^>]+>", "", s)


def _truncate_snippet(text_str: str, max_words: int = 20) -> str:
    """Truncate text to max_words."""
    words = text_str.split()
    if len(words) <= max_words:
        return text_str
    return " ".join(words[:max_words]) + "\u2026"


def _resolve_site(session: Session, site_slug: str) -> Site:
    site = session.query(Site).filter(Site.slug == site_slug).first()
    if site is None:
        from app.domain.errors import SiteNotFoundError

        raise SiteNotFoundError(site_slug)
    return site


def search_posts(
    session: Session,
    *,
    q: str | None = None,
    site_slug: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    author_label: str | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    published_after: datetime | None = None,
    published_before: datetime | None = None,
    updated_since: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    format_ids: bool = False,
) -> dict[str, Any]:
    """Full-text search with faceted filters and cursor pagination.

    Returns dict with keys: results, total_estimate, next_cursor.
    """
    if limit < 1 or limit > MAX_LIMIT:
        limit = max(1, min(limit, MAX_LIMIT))

    # Base query
    query = session.query(Post).filter(Post.deleted_at.is_(None))

    if site_slug:
        site = _resolve_site(session, site_slug)
        query = query.filter(Post.site_id == site.id)

    # Faceted filters
    if status:
        query = query.filter(Post.status == status)

    if tag:
        query = (
            query.join(PostTag, PostTag.post_id == Post.id)
            .join(Tag, Tag.id == PostTag.tag_id)
            .filter(Tag.slug == tag)
        )

    if author_label:
        query = query.filter(Post.author_label == author_label)

    if from_date:
        query = query.filter(Post.created_at >= from_date)

    if to_date:
        query = query.filter(Post.created_at <= to_date)

    if published_after:
        query = query.filter(Post.published_at >= published_after)

    if published_before:
        query = query.filter(Post.published_at <= published_before)

    if updated_since:
        query = query.filter(Post.updated_at >= updated_since)

    # Full-text search
    has_search = bool(q and q.strip())
    ts_query = None
    if has_search:
        search_terms = q.strip()  # type: ignore[union-attr]
        ts_query = func.plainto_tsquery("english", search_terms)
        query = query.filter(Post.search_vector.op("@@")(ts_query))

    # Cursor pagination: stable order by published_at DESC, id DESC
    if cursor:
        try:
            parts = cursor.split("|", 1)
            sort_key_str, cursor_id_str = parts[0], parts[1]
            cursor_id = uuid.UUID(cursor_id_str)
            sort_key = datetime.fromisoformat(sort_key_str) if sort_key_str else None
        except (ValueError, IndexError) as exc:
            raise InvalidCursorError(f"Invalid cursor format: {cursor!r}") from exc

        if sort_key is None:
            query = query.filter(
                or_(
                    Post.published_at.isnot(None),
                    (Post.published_at.is_(None)) & (Post.id < cursor_id),
                )
            )
        else:
            query = query.filter(
                or_(
                    Post.published_at.is_(None),
                    Post.published_at < sort_key,
                    (Post.published_at == sort_key) & (Post.id < cursor_id),
                )
            )

    # Compute total estimate
    total_estimate = query.with_entities(func.count(Post.id)).scalar() or 0

    # Apply ordering
    if has_search and ts_query is not None:
        score = func.ts_rank_cd(Post.search_vector, ts_query).label("score")
        query = query.add_columns(score).order_by(
            score.desc(), Post.published_at.desc().nullslast(), Post.id.desc()
        )
    else:
        query = query.order_by(Post.published_at.desc().nullslast(), Post.id.desc())

    items = query.limit(limit + 1).all()

    next_cursor = None
    if len(items) > limit:
        last_row = items[-2]
        last_post = last_row[0] if (has_search and ts_query is not None) else last_row  # type: ignore[index]
        next_cursor = f"{last_post.published_at.isoformat() if last_post.published_at else ''}|{last_post.id}"
        items = items[:limit]

    # Build results
    results: list[dict[str, Any]] = []
    for row in items:
        if has_search and ts_query is not None:
            post, score_val = row  # type: ignore[misc]
        else:
            post = row
            score_val = None

        # Generate snippet
        snippet = ""
        if has_search and q and q.strip():
            # Use ts_headline on the post's body via parameterized query
            raw_snippet = session.execute(
                text(
                    "SELECT ts_headline('english', coalesce(p.body_md, ''), "
                    "plainto_tsquery('english', :q), "
                    "'StartSel=<mark>, StopSel=</mark>, MaxWords=20, MinWords=10, HighlightAll=true') "
                    "FROM posts p WHERE p.id = :post_id"
                ),
                {"q": q.strip(), "post_id": post.id},
            ).scalar()
            snippet = _strip_html_tags(raw_snippet or "")
            snippet = _truncate_snippet(snippet, 20)
        else:
            # No search query: use excerpt or first 20 words of body
            raw = post.excerpt or post.body_md or ""
            snippet = _truncate_snippet(raw, 20)

        if format_ids:
            results.append(
                {
                    "id": str(post.id),
                    "slug": post.slug,
                }
            )
        else:
            results.append(
                {
                    "id": str(post.id),
                    "title": post.title,
                    "slug": post.slug,
                    "status": post.status,
                    "published_at": str(post.published_at) if post.published_at else None,
                    "snippet": snippet,
                    "score": float(score_val) if score_val is not None else None,
                    "url": f"/posts/{post.slug}",
                    "markdown_url": f"/posts/{post.slug}.md",
                }
            )

    return {
        "results": results,
        "total_estimate": total_estimate,
        "next_cursor": next_cursor,
    }
