"""Post CRUD endpoints (#4).

Implements the full create -> update -> publish -> read -> unpublish -> trash
cycle for posts.  All responses carry ``id``, ``slug``, ``status``, ``url``,
``markdown_url``, ``revision``, ``created_at``, ``updated_at``.

Design invariants:
* ``POST …/posts`` always returns a draft — ``status`` in the body is ignored.
* ``POST …/posts/{id}/publish`` is idempotent — double-publish returns 200
  with a ``warnings[]`` entry, not an error.
* ``?dry_run=true`` is accepted on create/update and simply doesn't persist.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.db.session import get_db
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
    dry_run: bool = Query(False, description="Validate but don't persist"),
) -> Response:
    if dry_run:
        return Response(
            content='{"dry_run": true}',
            status_code=201,
            media_type="application/json",
        )

    post = create_post(
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
    data["warnings"] = []
    if body.title is None:
        data["warnings"].append("Title was derived from the first H1 in body_md.")
    if body.slug is None:
        data["warnings"].append("Slug was derived from the title.")

    return Response(
        status_code=201,
        content=PostRead(**data).model_dump_json(),
        media_type="application/json",
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
) -> PostRead:
    post = get_post(db, identifier)
    site_slug = post.site.slug if post.site else "blog"
    data = _post_to_dict(post, site_slug, session=db)
    return PostRead(**data)


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
    dry_run: bool = Query(False, description="Validate but don't persist"),
) -> PostRead:
    if dry_run:
        post = get_post(db, identifier)
        site_slug = post.site.slug if post.site else "blog"
        data = _post_to_dict(post, site_slug, session=db)
        return PostRead(**data)

    post = update_post(
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
    return PostRead(**data)


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
) -> dict[str, Any]:
    post = trash_post(db, identifier)
    site_slug = post.site.slug if post.site else "blog"
    data = _post_to_dict(post, site_slug, session=db)
    return data
