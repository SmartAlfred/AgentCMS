"""Post CRUD endpoints (#4, extended by #5, #7).

Implements the full create -> update -> publish -> read -> unpublish -> trash
cycle for posts.  All responses carry ``id``, ``slug``, ``status``, ``url``,
``markdown_url``, ``revision``, ``created_at``, ``updated_at``.

Design invariants:
* ``POST …/posts`` always returns a draft — ``status`` in the body is ignored.
* ``POST …/posts/{id}/publish`` is idempotent — double-publish returns 200
  with a ``warnings[]`` entry, not an error.
* ``?dry_run=true`` is accepted on create/update and simply doesn't persist.

Content negotiation (Accept header):
* ``application/json`` (default) — full post object
* ``text/markdown`` — raw ``body_md`` only
* ``text/html`` — rendered HTML fragment
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.auth import AuthContext, require_auth
from app.db.session import get_db
from app.services.markdown import MarkdownRenderer
from app.services.post import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    _post_to_dict,
    create_post,
    get_post,
    list_posts,
    publish_post,
    trash_post,
    unpublish_post,
    update_post,
)

from .schemas import PostCreate, PostListResponse, PostRead, PostUpdate

router = APIRouter()

DbSession = Annotated[Session, Depends(get_db)]


def _negotiate_response(
    request: Request,
    post_data: dict[str, Any],
    *,
    body_html: str | None = None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    """Return the appropriate representation based on the Accept header."""
    accept = request.headers.get("accept", "application/json")
    resp_headers = dict(headers) if headers else {}

    if "text/markdown" in accept:
        return Response(
            content=post_data.get("body_md", ""),
            status_code=status_code,
            media_type="text/markdown; charset=utf-8",
            headers=resp_headers,
        )
    if "text/html" in accept:
        html_content = body_html or ""
        if not html_content and post_data.get("body_md"):
            renderer = MarkdownRenderer()
            html_content = renderer.render_html(post_data["body_md"])
        return Response(
            content=html_content,
            status_code=status_code,
            media_type="text/html; charset=utf-8",
            headers=resp_headers,
        )
    # Default: application/json
    return Response(
        content=PostRead(**post_data).model_dump_json(),
        status_code=status_code,
        media_type="application/json",
        headers=resp_headers,
    )


# ---------------------------------------------------------------------------
# POST /v1/sites/{site}/posts — create
# ---------------------------------------------------------------------------


@router.post(
    "/sites/{site_slug}/posts",
    summary="Create a post",
    status_code=201,
    tags=["posts"],
    response_model=PostRead,
)
def create_post_endpoint(
    site_slug: str,
    body: PostCreate,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
    dry_run: bool = Query(False, description="Validate but don't persist"),
) -> Response:
    if dry_run:
        return Response(
            content='{"dry_run": true}',
            status_code=201,
            media_type="application/json",
        )

    post, norm_warnings = create_post(
        db,
        site_slug,
        body_md=body.body_md,
        title=body.title,
        slug=body.slug,
        tags=body.tags,
        excerpt=body.excerpt,
        frontmatter=body.frontmatter,
    )

    data = _post_to_dict(post, site_slug, session=db)
    warnings: list[str] = list(norm_warnings)
    if body.title is None:
        warnings.append("Title was derived from the first H1 in body_md.")
    if body.slug is None:
        warnings.append("Slug was derived from the title.")
    data["warnings"] = warnings

    # Render HTML for content negotiation
    renderer = MarkdownRenderer()
    body_html = renderer.render_html(post.body_md)

    return _negotiate_response(
        request,
        data,
        body_html=body_html,
        status_code=201,
        headers={"Location": f"/v1/posts/{post.id}"},
    )


# ---------------------------------------------------------------------------
# GET /v1/sites/{site}/posts — list
# ---------------------------------------------------------------------------


@router.get(
    "/sites/{site_slug}/posts",
    summary="List posts",
    tags=["posts"],
    response_model=PostListResponse,
)
def list_posts_endpoint(
    site_slug: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> PostListResponse:
    items, next_cursor = list_posts(
        db,
        site_slug,
        status=status,
        limit=limit,
        cursor=cursor,
    )

    post_items = [PostRead(**_post_to_dict(p, site_slug, session=db)) for p in items]
    return PostListResponse(
        items=post_items,
        next_cursor=next_cursor,
        count=len(post_items),
    )


# ---------------------------------------------------------------------------
# GET /v1/posts/{id_or_slug} — read one
# ---------------------------------------------------------------------------


@router.get(
    "/posts/{identifier}",
    summary="Read a post",
    tags=["posts"],
    response_model=PostRead,
)
def get_post_endpoint(
    identifier: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
) -> Response:
    post = get_post(db, identifier)
    site_slug = post.site.slug if post.site else "blog"
    data = _post_to_dict(post, site_slug, session=db)

    renderer = MarkdownRenderer()
    body_html = renderer.render_html(post.body_md)

    return _negotiate_response(request, data, body_html=body_html)


# ---------------------------------------------------------------------------
# PATCH /v1/posts/{id} — update
# ---------------------------------------------------------------------------


@router.patch(
    "/posts/{identifier}",
    summary="Update a post",
    tags=["posts"],
    response_model=PostRead,
)
def update_post_endpoint(
    identifier: str,
    body: PostUpdate,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
    dry_run: bool = Query(False, description="Validate but don't persist"),
) -> Response:
    if dry_run:
        post = get_post(db, identifier)
        site_slug = post.site.slug if post.site else "blog"
        data = _post_to_dict(post, site_slug, session=db)
        renderer = MarkdownRenderer()
        body_html = renderer.render_html(post.body_md)
        return _negotiate_response(request, data, body_html=body_html)

    post, norm_warnings = update_post(
        db,
        identifier,
        title=body.title,
        body_md=body.body_md,
        slug=body.slug,
        tags=body.tags,
        excerpt=body.excerpt,
        frontmatter=body.frontmatter,
    )
    site_slug = post.site.slug if post.site else "blog"
    data = _post_to_dict(post, site_slug, session=db)
    if norm_warnings:
        data["warnings"] = norm_warnings

    renderer = MarkdownRenderer()
    body_html = renderer.render_html(post.body_md)

    return _negotiate_response(request, data, body_html=body_html)


# ---------------------------------------------------------------------------
# POST /v1/posts/{id}/publish
# ---------------------------------------------------------------------------


@router.post(
    "/posts/{identifier}/publish",
    summary="Publish a post",
    tags=["posts"],
)
def publish_post_endpoint(
    identifier: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
) -> dict[str, Any]:
    post, warnings = publish_post(db, identifier)
    site_slug = post.site.slug if post.site else "blog"
    data = _post_to_dict(post, site_slug, session=db)
    data["warnings"] = warnings
    return data


# ---------------------------------------------------------------------------
# POST /v1/posts/{id}/unpublish
# ---------------------------------------------------------------------------


@router.post(
    "/posts/{identifier}/unpublish",
    summary="Unpublish a post",
    tags=["posts"],
)
def unpublish_post_endpoint(
    identifier: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
) -> dict[str, Any]:
    post, warnings = unpublish_post(db, identifier)
    site_slug = post.site.slug if post.site else "blog"
    data = _post_to_dict(post, site_slug, session=db)
    data["warnings"] = warnings
    return data


# ---------------------------------------------------------------------------
# DELETE /v1/posts/{id} — trash
# ---------------------------------------------------------------------------


@router.delete(
    "/posts/{identifier}",
    summary="Trash a post",
    tags=["posts"],
)
def trash_post_endpoint(
    identifier: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_auth),
) -> dict[str, Any]:
    post = trash_post(db, identifier)
    site_slug = post.site.slug if post.site else "blog"
    data = _post_to_dict(post, site_slug, session=db)
    return data
