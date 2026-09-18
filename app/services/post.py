"""Post CRUD service (#4).

All business rules for creating, reading, updating, publishing, unpublishing
and trashing posts live here.  The HTTP layer is a thin adapter that maps
these operations to endpoints; this module owns the invariants.

Design principles (from the ticket):
* Create **never** publishes; ``status`` in the request body is ignored.
* Title may be omitted — derived from the first ``# H1`` in ``body_md``.
* Slug may be omitted — slugified from title; duplicates return 409 with
  ``suggested_slug``.
* Listing uses cursor pagination.
* Publish is idempotent (no-op when already published).
"""

from __future__ import annotations

import difflib
import hashlib
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.domain.errors import (
    ContentPolicyBlockedError,
    ContentRequiredError,
    InvalidCursorError,
    InvalidTransitionError,
    PostNotFoundError,
    RevisionNotFoundError,
    SiteNotFoundError,
    SlugConflictError,
    SlugInvalidError,
    TitleRequiredError,
)
from app.models.post import Post
from app.models.post_revision import PostRevision
from app.models.site import Site
from app.models.tag import PostTag, Tag
from app.services.markdown import (
    compute_content_hash,
    compute_excerpt,
    compute_reading_time_minutes,
    compute_word_count,
    normalise_body,
)

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Turn a title into a URL-friendly slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")[:256]


def _derive_title(body_md: str) -> str | None:
    """Extract the first ``# Heading`` from body_md."""
    match = _TITLE_RE.search(body_md)
    return match.group(1).strip() if match else None


def _slug_conflict_slug(base_slug: str, session: Session, site_id: uuid.UUID) -> str:
    """Find a free slug by appending -2, -3, ... suffixes."""
    candidate = base_slug
    n = 2
    while session.query(func.count(Post.id)).filter(Post.site_id == site_id, Post.slug == candidate).scalar():
        candidate = f"{base_slug}-{n}"
        n += 1
    return candidate


def _make_content_hash(body_md: str) -> str:
    return hashlib.sha256(body_md.encode()).hexdigest()


def _compute_diff(prev_body: str, new_body: str, prev_title: str = "", new_title: str = "") -> str | None:
    """Compute a unified diff between two revisions' content."""
    old_lines = (prev_title + "\n" + prev_body).splitlines(keepends=True)
    new_lines = (new_title + "\n" + new_body).splitlines(keepends=True)
    diff = list(
        difflib.unified_diff(old_lines, new_lines, fromfile="a/revision", tofile="b/revision", lineterm="")
    )
    if not diff:
        return None
    return "\n".join(diff)


def _create_revision(
    session: Session,
    post: Post,
    *,
    actor_id: uuid.UUID,
    editor_label: str | None = None,
    request_id: str | None = None,
    source: str = "api",
) -> PostRevision:
    """Append a revision snapshot for the current state of the post.

    Computes a unified diff against the previous revision and stores it.
    The caller must have already set ``post.revision_count`` to the correct value.
    """
    prev_rev = (
        session.query(PostRevision)
        .filter(PostRevision.post_id == post.id)
        .order_by(PostRevision.revision.desc())
        .first()
    )

    diff_unified: str | None = None
    if prev_rev is not None:
        diff_unified = _compute_diff(
            prev_rev.body_md,
            post.body_md,
            prev_rev.title,
            post.title,
        )

    rev = PostRevision(
        id=uuid.uuid4(),
        post_id=post.id,
        revision=post.revision_count,
        title=post.title,
        body_md=post.body_md,
        frontmatter=post.frontmatter,
        status=post.status,
        actor_id=actor_id,
        editor_label=editor_label,
        request_id=request_id,
        source=source,
        diff_unified=diff_unified,
    )
    session.add(rev)
    return rev


def _resolve_site(session: Session, site_slug: str) -> Site:
    exists = session.query(func.count(Site.id)).filter(Site.slug == site_slug).scalar()
    if not exists:
        raise SiteNotFoundError(site_slug)
    return session.query(Site).filter(Site.slug == site_slug).one()


def _resolve_post(session: Session, identifier: str, *, site_id: uuid.UUID | None = None) -> Post:
    """Look up a post by UUID or slug, optionally scoped to a site."""
    try:
        post_id = uuid.UUID(identifier)
        query = session.query(Post).filter(Post.id == post_id)
    except ValueError:
        query = session.query(Post).filter(Post.slug == identifier)

    if site_id is not None:
        query = query.filter(Post.site_id == site_id)

    post = query.first()
    if post is None:
        raise PostNotFoundError(identifier)
    return post


def _post_to_dict(post: Post, site_slug: str, *, session: Session | None = None) -> dict[str, Any]:
    """Build the JSON dict for a PostRead response."""
    tags: list[str] = []
    if session is not None:
        tag_rows = (
            session.query(Tag.slug)
            .join(PostTag, PostTag.tag_id == Tag.id)
            .filter(PostTag.post_id == post.id)
            .all()
        )
        tags = [row[0] for row in tag_rows]
    elif post.tag_links:
        tags = [pt.tag.slug for pt in post.tag_links if pt.tag]

    return {
        "id": str(post.id),
        "site_id": str(post.site_id),
        "slug": post.slug,
        "title": post.title,
        "body_md": post.body_md,
        "excerpt": post.excerpt,
        "status": post.status,
        "frontmatter": post.frontmatter or {},
        "tags": tags,
        "url": f"/posts/{post.slug}",
        "markdown_url": f"/posts/{post.slug}.md",
        "revision": post.revision_count,
        "created_at": post.created_at,
        "updated_at": post.updated_at,
        "published_at": post.published_at,
        "publish_at": post.publish_at,
        "unpublish_at": post.unpublish_at,
        "word_count": post.word_count,
        "reading_time_minutes": post.reading_time_minutes,
        "content_hash": post.content_hash,
        "review": _review_summary(post) if post.review_status else None,
    }


def _review_summary(post: Post) -> dict[str, Any] | None:
    """Build a review summary dict for the post response."""
    if not post.review_status:
        return None
    summary: dict[str, Any] = {"status": post.review_status}
    if post.review_comment is not None:
        summary["comment"] = post.review_comment
    if post.reviewed_at:
        summary["decided_at"] = post.reviewed_at
    if post.reviewed_by_actor_id:
        summary["decided_by"] = str(post.reviewed_by_actor_id)
    if post.review_id:
        summary["review_id"] = str(post.review_id)
    return summary


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def create_post(
    session: Session,
    site_slug: str,
    *,
    body_md: str,
    title: str | None = None,
    slug: str | None = None,
    tags: list[str] | None = None,
    excerpt: str | None = None,
    frontmatter: dict[str, Any] | None = None,
    actor_id: uuid.UUID,
    source: str = "api",
    audit_ctx: dict[str, Any] | None = None,
    is_agent_actor: bool = True,
) -> tuple[Post, list[str]]:
    """Create a new post in draft status.

    ``status`` is **never** read from user input — the post is always draft.
    Returns ``(post, warnings)`` where warnings include markdown normalisation notes.
    """
    if not body_md or not body_md.strip():
        raise ContentRequiredError("Document body required — body_md is the only required field.")

    # Normalise body_md (line endings, whitespace, trailing newline, H1 demotion, smart quotes)
    normalised_body, norm_warnings = normalise_body(body_md)

    # Compute derived fields
    content_hash = compute_content_hash(normalised_body)
    word_count = compute_word_count(normalised_body)
    reading_time_minutes = compute_reading_time_minutes(normalised_body)
    derived_excerpt = compute_excerpt(normalised_body)

    # Use caller-provided excerpt if given, otherwise use derived
    final_excerpt = excerpt if excerpt is not None else derived_excerpt

    site = _resolve_site(session, site_slug)

    # Derive title from first H1 if not supplied
    if title is None or not title.strip():
        derived = _derive_title(normalised_body)
        if derived is None:
            raise TitleRequiredError()
        title = derived

    # Derive slug from title if not supplied
    if slug is None or not slug.strip():
        slug = _slugify(title)

    # Validate slug format
    if not _SLUG_RE.match(slug):
        raise SlugInvalidError(slug, "must contain only lowercase letters, digits, and single hyphens")

    # Check for slug collision
    existing = (
        session.query(Post)
        .filter(
            Post.site_id == site.id,
            Post.slug == slug,
        )
        .first()
    )
    if existing is not None:
        suggested = _slug_conflict_slug(slug, session, site.id)
        raise SlugConflictError(slug, suggested, site=site_slug)

    post = Post(
        id=uuid.uuid4(),
        site_id=site.id,
        slug=slug,
        title=title,
        body_md=normalised_body,
        excerpt=final_excerpt,
        status="draft",
        frontmatter=frontmatter or {},
        content_hash=content_hash,
        word_count=word_count,
        reading_time_minutes=reading_time_minutes,
        revision_count=0,
    )
    session.add(post)
    session.flush()

    # Create initial revision (revision 1)
    post.revision_count = 1
    _create_revision(session, post, actor_id=actor_id, source=source)
    session.flush()

    # Handle tags
    if tags:
        _sync_tags(session, post, tags)

    # --- Content policy check (#17) ---
    from app.services.content_policy import (
        evaluate_content_policy,
        record_moderation_decision,
    )

    # Policy detection runs on the body as submitted (before invisible-character
    # stripping) so hidden-text payloads are still caught; storage keeps the
    # normalised body.
    policy_eval = evaluate_content_policy(
        session,
        site_id=site.id,
        body_md=body_md,
        is_agent_actor=is_agent_actor,
        exclude_post_id=post.id,
    )

    if not policy_eval.allowed and policy_eval.blocking_check:
        session.rollback()
        check = policy_eval.blocking_check
        raise ContentPolicyBlockedError(check.rule_name, check.evidence)

    # Record flag decisions (content stored but not publishable)
    flagged = False
    for check in policy_eval.checks:
        if check.outcome == "flag":
            record_moderation_decision(session, post_id=post.id, check=check)
            flagged = True

    if flagged:
        post.status = "pending_review"

    # Audit: record the creation
    from app.services.audit import compute_content_hash as audit_hash
    from app.services.audit import record_event

    ctx = audit_ctx or {}
    record_event(
        session,
        action="post.created",
        actor_id=actor_id,
        actor_label=ctx.get("actor_label"),
        actor_kind=ctx.get("actor_kind", "human"),
        source=source,
        target_type="post",
        target_id=str(post.id),
        after_hash=audit_hash(normalised_body),
        revision=1,
        request_id=ctx.get("request_id"),
        ip=ctx.get("ip"),
        user_agent=ctx.get("user_agent"),
    )

    # Outbox: emit post.created event (#21)
    from app.services.webhook import write_event_to_outbox

    event_payload = {
        "id": str(post.id),
        "slug": post.slug,
        "title": post.title,
        "status": post.status,
        "url": f"/posts/{post.slug}",
        "markdown_url": f"/posts/{post.slug}.md",
        "revision": post.revision_count,
    }
    write_event_to_outbox(
        session,
        event_type="post.created",
        site_slug=site_slug,
        payload=event_payload,
        actor_id=actor_id,
        actor_label=ctx.get("actor_label"),
        actor_kind=ctx.get("actor_kind", "machine"),
        request_id=ctx.get("request_id"),
    )

    session.commit()
    return post, norm_warnings


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def get_post(session: Session, identifier: str, *, site_slug: str | None = None) -> Post:
    """Read a post by id or slug."""
    site_id = None
    if site_slug:
        site = _resolve_site(session, site_slug)
        site_id = site.id
    return _resolve_post(session, identifier, site_id=site_id)


def list_posts(
    session: Session,
    site_slug: str,
    *,
    status: str | None = None,
    tag: str | None = None,
    author_label: str | None = None,
    updated_since: datetime | None = None,
    published_after: datetime | None = None,
    published_before: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> tuple[list[Post], str | None]:
    """List posts for a site with cursor pagination.

    Returns (items, next_cursor).
    """
    if limit < 1 or limit > MAX_LIMIT:
        limit = max(1, min(limit, MAX_LIMIT))

    site = _resolve_site(session, site_slug)

    query = session.query(Post).filter(
        Post.site_id == site.id,
        Post.deleted_at.is_(None),
    )

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

    if updated_since:
        query = query.filter(Post.updated_at >= updated_since)

    if published_after:
        query = query.filter(Post.published_at >= published_after)

    if published_before:
        query = query.filter(Post.published_at <= published_before)

    # Cursor pagination: cursor encodes (sort_key, id) where sort_key is
    # published_at ISO format (or empty for nulls) and id is the post UUID.
    if cursor:
        try:
            parts = cursor.split("|", 1)
            sort_key_str, cursor_id_str = parts[0], parts[1]
            cursor_id = uuid.UUID(cursor_id_str)
            sort_key = datetime.fromisoformat(sort_key_str) if sort_key_str else None
        except (ValueError, IndexError) as exc:
            raise InvalidCursorError(f"Invalid cursor format: {cursor!r}") from exc

        if sort_key is None:
            # Current position is in the NULL published_at group
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

    query = query.order_by(Post.published_at.desc().nullslast(), Post.id.desc())
    items = query.limit(limit + 1).all()

    next_cursor = None
    if len(items) > limit:
        last = items[-2]
        next_cursor = f"{last.published_at.isoformat() if last.published_at else ''}|{last.id}"
        items = items[:limit]

    return items, next_cursor


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def update_post(
    session: Session,
    post_id: str,
    *,
    title: str | None = None,
    body_md: str | None = None,
    slug: str | None = None,
    tags: list[str] | None = None,
    excerpt: str | None = None,
    frontmatter: dict[str, Any] | None = None,
    actor_id: uuid.UUID,
    source: str = "api",
    audit_ctx: dict[str, Any] | None = None,
    is_agent_actor: bool = True,
) -> tuple[Post, list[str]]:
    """Partially update a post.  Creates a revision snapshot.
    Returns ``(post, warnings)`` where warnings include markdown normalisation notes.
    """
    post = _resolve_post(session, post_id)
    warnings: list[str] = []

    if post.status == "trashed":
        raise InvalidTransitionError(
            "update",
            "trashed",
            hint="Restore the post first (not yet supported), or create a new one.",
        )

    changed = False

    if body_md is not None:
        if not body_md.strip():
            raise ContentRequiredError("Document body required — body_md is the only required field.")
        # Normalise body_md
        normalised_body, norm_warnings = normalise_body(body_md)
        warnings.extend(norm_warnings)

        # Recompute derived fields
        new_hash = compute_content_hash(normalised_body)
        if new_hash != post.content_hash:
            post.body_md = normalised_body
            post.content_hash = new_hash
            post.word_count = compute_word_count(normalised_body)
            post.reading_time_minutes = compute_reading_time_minutes(normalised_body)
            # Update excerpt if not explicitly provided
            if excerpt is None:
                post.excerpt = compute_excerpt(normalised_body)
            changed = True

    if title is not None:
        post.title = title
        changed = True

    if slug is not None and slug != post.slug:
        if not _SLUG_RE.match(slug):
            raise SlugInvalidError(slug, "must contain only lowercase letters, digits, and single hyphens")
        existing = (
            session.query(Post)
            .filter(
                Post.site_id == post.site_id,
                Post.slug == slug,
                Post.id != post.id,
            )
            .first()
        )
        if existing is not None:
            suggested = _slug_conflict_slug(slug, session, post.site_id)
            raise SlugConflictError(slug, suggested)
        post.slug = slug
        changed = True

    if excerpt is not None:
        post.excerpt = excerpt
        changed = True

    if frontmatter is not None:
        post.frontmatter = frontmatter
        changed = True

    if tags is not None:
        _sync_tags(session, post, tags)
        changed = True

    if changed:
        post.revision_count += 1
        _create_revision(session, post, actor_id=actor_id, source=source)
        session.flush()

        # --- Content policy check (#17) ---
        from app.services.content_policy import (
            evaluate_content_policy,
            record_moderation_decision,
        )

        if body_md is not None:
            # Detect on the body as submitted; the stored body is normalised.
            policy_eval = evaluate_content_policy(
                session,
                site_id=post.site_id,
                body_md=body_md,
                is_agent_actor=is_agent_actor,
                existing_post_id=post.id,
                exclude_post_id=post.id,
            )

            if not policy_eval.allowed and policy_eval.blocking_check:
                session.rollback()
                check = policy_eval.blocking_check
                raise ContentPolicyBlockedError(check.rule_name, check.evidence)

            # Record flag decisions
            flagged = False
            for check in policy_eval.checks:
                if check.outcome == "flag":
                    record_moderation_decision(session, post_id=post.id, check=check)
                    flagged = True

            if flagged:
                post.status = "pending_review"

        # Audit: record the update
        from app.services.audit import compute_content_hash as audit_hash
        from app.services.audit import record_event

        ctx = audit_ctx or {}
        record_event(
            session,
            action="post.updated",
            actor_id=actor_id,
            actor_label=ctx.get("actor_label"),
            actor_kind=ctx.get("actor_kind", "human"),
            source=source,
            target_type="post",
            target_id=str(post.id),
            before_hash=audit_hash(post.body_md),
            after_hash=audit_hash(post.body_md),
            revision=post.revision_count,
            request_id=ctx.get("request_id"),
            ip=ctx.get("ip"),
            user_agent=ctx.get("user_agent"),
        )

        # Outbox: emit post.updated event (#21)
        from app.services.webhook import write_event_to_outbox

        _site_slug = post.site.slug if post.site else None
        event_payload = {
            "id": str(post.id),
            "slug": post.slug,
            "title": post.title,
            "status": post.status,
            "url": f"/posts/{post.slug}",
            "markdown_url": f"/posts/{post.slug}.md",
            "revision": post.revision_count,
        }
        write_event_to_outbox(
            session,
            event_type="post.updated",
            site_slug=_site_slug,
            payload=event_payload,
            actor_id=actor_id,
            actor_label=ctx.get("actor_label"),
            actor_kind=ctx.get("actor_kind", "machine"),
            request_id=ctx.get("request_id"),
        )

        session.commit()

    return post, warnings


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


def publish_post(
    session: Session,
    post_id: str,
    *,
    actor_id: uuid.UUID,
    source: str = "api",
    audit_ctx: dict[str, Any] | None = None,
    is_agent_actor: bool = True,
) -> tuple[Post, list[str]]:
    """Publish a draft post.

    If the site requires review (``publish_mode == "require_review"`` and
    trust mode is not active), the post enters ``pending_review`` instead
    of being immediately published.

    If the post has been flagged by content policy, agent actors cannot
    publish it even with ``posts:publish`` scope.

    Idempotent — publishing twice returns the same post with warnings.
    """
    post = _resolve_post(session, post_id)
    warnings: list[str] = []

    if post.status == "published":
        warnings.append(f"Post '{post.slug}' is already published; no change made.")
        return post, warnings

    if post.status == "pending_review":
        warnings.append(f"Post '{post.slug}' is pending review; publish request queued.")
        return post, warnings

    if post.status not in ("draft",):
        raise InvalidTransitionError(
            "publish",
            post.status,
            allowed_from=["draft"],
        )

    # --- Content policy: blocked publish for flagged content (#17) ---
    if is_agent_actor:
        from app.services.content_policy import is_post_publishable

        if not is_post_publishable(session, post.id):
            from app.domain.errors import ContentPolicyPublishBlockedError

            raise ContentPolicyPublishBlockedError()

    # Check if site requires review
    from app.models.site import Site as SiteModel

    site = session.query(SiteModel).filter(SiteModel.id == post.site_id).first()
    if site is not None:
        from app.services.review import site_requires_review

        if site_requires_review(site):
            from app.services.review import create_review

            review = create_review(
                session,
                post=post,
                site=site,
                actor_id=actor_id,
                source=source,
                audit_ctx=audit_ctx,
            )
            warnings.append(
                f"Post '{post.slug}' submitted for review (review_id: {review.id}). "
                "The post is not publicly visible until approved."
            )
            return post, warnings

    post.status = "published"
    post.published_at = datetime.now(UTC)
    post.revision_count += 1
    _create_revision(session, post, actor_id=actor_id, source=source)
    session.flush()

    # Audit: record the publish
    from app.services.audit import compute_content_hash as audit_hash
    from app.services.audit import record_event

    ctx = audit_ctx or {}
    record_event(
        session,
        action="post.published",
        actor_id=actor_id,
        actor_label=ctx.get("actor_label"),
        actor_kind=ctx.get("actor_kind", "human"),
        source=source,
        target_type="post",
        target_id=str(post.id),
        after_hash=audit_hash(post.body_md),
        revision=post.revision_count,
        request_id=ctx.get("request_id"),
        ip=ctx.get("ip"),
        user_agent=ctx.get("user_agent"),
    )

    # Outbox: emit post.published event (#21)
    from app.models.site import Site as SiteModel
    from app.services.webhook import write_event_to_outbox

    _site = session.query(SiteModel).filter(SiteModel.id == post.site_id).first()
    _site_slug = _site.slug if _site else None
    event_payload = {
        "id": str(post.id),
        "slug": post.slug,
        "title": post.title,
        "status": post.status,
        "url": f"/posts/{post.slug}",
        "markdown_url": f"/posts/{post.slug}.md",
        "revision": post.revision_count,
        "published_at": post.published_at.isoformat() if post.published_at else None,
    }
    write_event_to_outbox(
        session,
        event_type="post.published",
        site_slug=_site_slug,
        payload=event_payload,
        actor_id=actor_id,
        actor_label=ctx.get("actor_label"),
        actor_kind=ctx.get("actor_kind", "machine"),
        request_id=ctx.get("request_id"),
    )

    session.commit()
    return post, warnings


# ---------------------------------------------------------------------------
# Unpublish
# ---------------------------------------------------------------------------


def unpublish_post(
    session: Session,
    post_id: str,
    *,
    actor_id: uuid.UUID,
    source: str = "api",
    audit_ctx: dict[str, Any] | None = None,
) -> tuple[Post, list[str]]:
    """Unpublish a published post (back to draft)."""
    post = _resolve_post(session, post_id)
    warnings: list[str] = []

    if post.status != "published":
        raise InvalidTransitionError(
            "unpublish",
            post.status,
            allowed_from=["published"],
        )

    post.status = "draft"
    post.revision_count += 1
    _create_revision(session, post, actor_id=actor_id, source=source)
    session.flush()

    # Audit: record the unpublish
    from app.services.audit import compute_content_hash as audit_hash
    from app.services.audit import record_event

    ctx = audit_ctx or {}
    record_event(
        session,
        action="post.unpublished",
        actor_id=actor_id,
        actor_label=ctx.get("actor_label"),
        actor_kind=ctx.get("actor_kind", "human"),
        source=source,
        target_type="post",
        target_id=str(post.id),
        before_hash=audit_hash(post.body_md),
        revision=post.revision_count,
        request_id=ctx.get("request_id"),
        ip=ctx.get("ip"),
        user_agent=ctx.get("user_agent"),
    )

    # Outbox: emit post.unpublished event (#21)
    from app.services.webhook import write_event_to_outbox

    _site_slug = post.site.slug if post.site else None
    event_payload = {
        "id": str(post.id),
        "slug": post.slug,
        "title": post.title,
        "status": post.status,
        "url": f"/posts/{post.slug}",
        "markdown_url": f"/posts/{post.slug}.md",
        "revision": post.revision_count,
    }
    write_event_to_outbox(
        session,
        event_type="post.unpublished",
        site_slug=_site_slug,
        payload=event_payload,
        actor_id=actor_id,
        actor_label=ctx.get("actor_label"),
        actor_kind=ctx.get("actor_kind", "machine"),
        request_id=ctx.get("request_id"),
    )

    session.commit()
    return post, warnings


# ---------------------------------------------------------------------------
# Trash
# ---------------------------------------------------------------------------


def trash_post(
    session: Session,
    post_id: str,
    *,
    actor_id: uuid.UUID,
    source: str = "api",
    audit_ctx: dict[str, Any] | None = None,
) -> Post:
    """Soft-delete a post (status=trashed, deleted_at=now)."""
    post = _resolve_post(session, post_id)

    if post.status == "trashed":
        return post  # already trashed

    post.status = "trashed"
    post.deleted_at = datetime.now(UTC)
    post.revision_count += 1
    _create_revision(session, post, actor_id=actor_id, source=source)
    session.flush()

    # Audit: record the trash
    from app.services.audit import compute_content_hash as audit_hash
    from app.services.audit import record_event

    ctx = audit_ctx or {}
    record_event(
        session,
        action="post.trashed",
        actor_id=actor_id,
        actor_label=ctx.get("actor_label"),
        actor_kind=ctx.get("actor_kind", "human"),
        source=source,
        target_type="post",
        target_id=str(post.id),
        before_hash=audit_hash(post.body_md),
        revision=post.revision_count,
        request_id=ctx.get("request_id"),
        ip=ctx.get("ip"),
        user_agent=ctx.get("user_agent"),
    )

    # Outbox: emit post.trashed event (#21)
    from app.services.webhook import write_event_to_outbox

    _site_slug = post.site.slug if post.site else None
    event_payload = {
        "id": str(post.id),
        "slug": post.slug,
        "title": post.title,
        "status": post.status,
        "url": f"/posts/{post.slug}",
        "markdown_url": f"/posts/{post.slug}.md",
        "revision": post.revision_count,
    }
    write_event_to_outbox(
        session,
        event_type="post.trashed",
        site_slug=_site_slug,
        payload=event_payload,
        actor_id=actor_id,
        actor_label=ctx.get("actor_label"),
        actor_kind=ctx.get("actor_kind", "machine"),
        request_id=ctx.get("request_id"),
    )

    session.commit()
    return post


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


def restore_post(
    session: Session,
    post_id: str,
    *,
    actor_id: uuid.UUID,
    source: str = "api",
    audit_ctx: dict[str, Any] | None = None,
) -> tuple[Post, list[str]]:
    """Restore a trashed post back to draft status."""
    post = _resolve_post(session, post_id)
    warnings: list[str] = []

    if post.status != "trashed":
        warnings.append(f"Post '{post.slug}' is not trashed; no change made.")
        return post, warnings

    post.status = "draft"
    post.deleted_at = None
    post.revision_count += 1
    _create_revision(session, post, actor_id=actor_id, source=source)
    session.flush()

    # Audit: record the restore
    from app.services.audit import compute_content_hash as audit_hash
    from app.services.audit import record_event

    ctx = audit_ctx or {}
    record_event(
        session,
        action="post.reverted",
        actor_id=actor_id,
        actor_label=ctx.get("actor_label"),
        actor_kind=ctx.get("actor_kind", "human"),
        source=source,
        target_type="post",
        target_id=str(post.id),
        after_hash=audit_hash(post.body_md),
        revision=post.revision_count,
        request_id=ctx.get("request_id"),
        ip=ctx.get("ip"),
        user_agent=ctx.get("user_agent"),
    )

    # Outbox: emit post.restored event (#21)
    from app.services.webhook import write_event_to_outbox

    _site_slug = post.site.slug if post.site else None
    event_payload = {
        "id": str(post.id),
        "slug": post.slug,
        "title": post.title,
        "status": post.status,
        "url": f"/posts/{post.slug}",
        "markdown_url": f"/posts/{post.slug}.md",
        "revision": post.revision_count,
    }
    write_event_to_outbox(
        session,
        event_type="post.restored",
        site_slug=_site_slug,
        payload=event_payload,
        actor_id=actor_id,
        actor_label=ctx.get("actor_label"),
        actor_kind=ctx.get("actor_kind", "machine"),
        request_id=ctx.get("request_id"),
    )

    session.commit()
    return post, warnings


# ---------------------------------------------------------------------------
# Revisions — list, get, diff, revert, prune
# ---------------------------------------------------------------------------

REV_DEFAULT_LIMIT = 20
REV_MAX_LIMIT = 100


def list_revisions(
    session: Session,
    post_id: str,
    *,
    limit: int = REV_DEFAULT_LIMIT,
    cursor: int | None = None,
) -> tuple[list[PostRevision], int | None]:
    """List revision metadata for a post (no body content, cheap for agents).

    Returns ``(items, next_cursor)`` where next_cursor is the next revision
    number to fetch, or None if there are no more.
    """
    if limit < 1 or limit > REV_MAX_LIMIT:
        limit = max(1, min(limit, REV_MAX_LIMIT))

    post = _resolve_post(session, post_id)

    query = session.query(PostRevision).filter(PostRevision.post_id == post.id)

    if cursor is not None:
        query = query.filter(PostRevision.revision < cursor)

    query = query.order_by(PostRevision.revision.desc())
    items = query.limit(limit + 1).all()

    next_cursor: int | None = None
    if len(items) > limit:
        last = items[limit - 1]
        next_cursor = last.revision
        items = items[:limit]

    return items, next_cursor


def get_revision(session: Session, post_id: str, revision: int) -> PostRevision:
    """Get a full revision snapshot by post id and revision number."""
    post = _resolve_post(session, post_id)
    rev = (
        session.query(PostRevision)
        .filter(PostRevision.post_id == post.id, PostRevision.revision == revision)
        .first()
    )
    if rev is None:
        raise RevisionNotFoundError(post_id, revision)
    return rev


def get_diff(session: Session, post_id: str, from_rev: int, to_rev: int) -> dict[str, Any]:
    """Get a unified diff between two revisions.

    Returns a dict with ``from_revision``, ``to_revision``, ``diff_unified``,
    and ``identical`` (bool).
    """
    if from_rev == to_rev:
        return {"from_revision": from_rev, "to_revision": to_rev, "diff_unified": "", "identical": True}

    post = _resolve_post(session, post_id)

    rev_from = (
        session.query(PostRevision)
        .filter(PostRevision.post_id == post.id, PostRevision.revision == from_rev)
        .first()
    )
    rev_to = (
        session.query(PostRevision)
        .filter(PostRevision.post_id == post.id, PostRevision.revision == to_rev)
        .first()
    )

    if rev_from is None:
        raise RevisionNotFoundError(post_id, from_rev)
    if rev_to is None:
        raise RevisionNotFoundError(post_id, to_rev)

    diff_text = _compute_diff(rev_from.body_md, rev_to.body_md, rev_from.title, rev_to.title) or ""
    return {
        "from_revision": from_rev,
        "to_revision": to_rev,
        "diff_unified": diff_text,
        "identical": diff_text == "",
    }


def revert_post(
    session: Session,
    post_id: str,
    *,
    target_revision: int,
    actor_id: uuid.UUID,
    reason: str | None = None,
    source: str = "api",
    audit_ctx: dict[str, Any] | None = None,
) -> Post:
    """Revert a post to a previous revision.

    Creates a **new** revision whose content equals the target revision.
    History is never rewritten.
    """
    post = _resolve_post(session, post_id)

    target = (
        session.query(PostRevision)
        .filter(PostRevision.post_id == post.id, PostRevision.revision == target_revision)
        .first()
    )
    if target is None:
        raise RevisionNotFoundError(post_id, target_revision)

    # Apply the target revision's content to the post
    post.title = target.title
    post.body_md = target.body_md
    post.frontmatter = target.frontmatter
    post.status = target.status
    post.content_hash = compute_content_hash(target.body_md)
    post.word_count = compute_word_count(target.body_md)
    post.reading_time_minutes = compute_reading_time_minutes(target.body_md)
    post.excerpt = compute_excerpt(target.body_md)

    if target.status == "trashed":
        post.deleted_at = datetime.now(UTC)
    else:
        post.deleted_at = None

    if target.status == "published":
        post.published_at = post.published_at or datetime.now(UTC)

    # Create a new revision (history is never rewritten)
    post.revision_count += 1
    _create_revision(session, post, actor_id=actor_id, source=source)
    session.flush()

    # Audit: record the revert
    from app.services.audit import compute_content_hash as audit_hash
    from app.services.audit import record_event

    ctx = audit_ctx or {}
    record_event(
        session,
        action="post.reverted",
        actor_id=actor_id,
        actor_label=ctx.get("actor_label"),
        actor_kind=ctx.get("actor_kind", "human"),
        source=source,
        target_type="post",
        target_id=str(post.id),
        before_hash=audit_hash(post.body_md),
        after_hash=audit_hash(target.body_md),
        revision=post.revision_count,
        request_id=ctx.get("request_id"),
        ip=ctx.get("ip"),
        user_agent=ctx.get("user_agent"),
        event_metadata={"target_revision": target_revision, "reason": reason},
    )

    # Outbox: emit post.reverted event (#21)
    from app.services.webhook import write_event_to_outbox

    _site_slug = post.site.slug if post.site else None
    event_payload = {
        "id": str(post.id),
        "slug": post.slug,
        "title": post.title,
        "status": post.status,
        "url": f"/posts/{post.slug}",
        "markdown_url": f"/posts/{post.slug}.md",
        "revision": post.revision_count,
        "target_revision": target_revision,
    }
    write_event_to_outbox(
        session,
        event_type="post.reverted",
        site_slug=_site_slug,
        payload=event_payload,
        actor_id=actor_id,
        actor_label=ctx.get("actor_label"),
        actor_kind=ctx.get("actor_kind", "machine"),
        request_id=ctx.get("request_id"),
    )

    session.commit()
    return post


def prune_revisions(
    session: Session,
    post_id: str,
    *,
    keep_recent: int = 20,
    draft_max_age_days: int = 30,
) -> int:
    """Prune old draft revisions beyond the retention policy.

    Rules:
    * Published posts keep all revisions forever.
    * Draft/trashed posts: keep the most recent ``keep_recent`` revisions,
      and prune any draft-status revisions older than ``draft_max_age_days``
      beyond those.
    * Never delete the current revision or the latest published revision.

    Returns the number of revisions deleted.
    """
    post = _resolve_post(session, post_id)

    # Never prune published posts
    if post.status == "published":
        return 0

    all_revisions = (
        session.query(PostRevision)
        .filter(PostRevision.post_id == post.id)
        .order_by(PostRevision.revision.desc())
        .all()
    )

    if len(all_revisions) <= keep_recent:
        return 0

    # Identify revisions that must never be deleted
    current_rev_num = post.revision_count
    published_rev: PostRevision | None = (
        session.query(PostRevision)
        .filter(PostRevision.post_id == post.id, PostRevision.status == "published")
        .order_by(PostRevision.revision.desc())
        .first()
    )
    protected_revisions: set[int] = {current_rev_num}
    if published_rev is not None:
        protected_revisions.add(published_rev.revision)

    from datetime import timedelta

    cutoff = datetime.now(UTC) - timedelta(days=draft_max_age_days)
    # Make naive for comparison with DB timestamps (which may be naive)
    cutoff_naive = cutoff.replace(tzinfo=None)

    # Candidates: revisions beyond keep_recent that are draft and old enough
    deletable: list[PostRevision] = []
    for rev in all_revisions[keep_recent:]:
        if rev.revision in protected_revisions:
            continue
        if rev.status != "draft":
            continue
        rev_created = rev.created_at.replace(tzinfo=None) if rev.created_at.tzinfo else rev.created_at
        if rev_created < cutoff_naive:
            deletable.append(rev)

    for rev in deletable:
        session.delete(rev)

    if deletable:
        session.flush()

    return len(deletable)


# ---------------------------------------------------------------------------
# Tags helper
# ---------------------------------------------------------------------------


def _sync_tags(session: Session, post: Post, tag_slugs: list[str]) -> None:
    """Replace the post's tags with the given list of slug strings."""
    # Remove existing
    session.query(PostTag).filter(PostTag.post_id == post.id).delete(synchronize_session=False)

    for slug in tag_slugs[:20]:
        slug = slug.lower().strip()
        if not slug:
            continue
        tag = session.query(Tag).filter(Tag.slug == slug).first()
        if tag is None:
            tag = Tag(id=uuid.uuid4(), slug=slug, name=slug.replace("-", " ").title())
            session.add(tag)
            session.flush()
        session.add(PostTag(post_id=post.id, tag_id=tag.id))
