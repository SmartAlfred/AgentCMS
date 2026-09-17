"""Post CRUD endpoints (#4, extended by #5, #7, #11).

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

Ticket #11 additions:
* ``Idempotency-Key`` header or ``?idempotency_key=`` query param for
  idempotent writes.
* ``ETag`` header on every ``GET`` of a post (derived from ``content_hash``
  + ``revision``).
* ``If-Match`` on ``PATCH`` / ``DELETE`` / ``publish`` / ``unpublish`` for
  optimistic concurrency.
* ``If-None-Match`` on ``GET`` for cheap polling (304).
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from sqlalchemy.orm import Session

from app.auth import AuthContext, require_auth
from app.db.session import get_db
from app.services.concurrency import check_if_match, check_if_none_match, compute_etag
from app.services.idempotency import (
    check_idempotency,
    complete_idempotency,
    get_stored_response,
)
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


def _extract_idempotency_key(request: Request, idempotency_key: str | None) -> str | None:
    """Extract idempotency key from header or query parameter."""
    # Prefer header, fall back to query parameter
    key = request.headers.get("idempotency-key")
    if not key:
        key = idempotency_key
    return key


def _warn_no_idempotency_key(post_data: dict[str, Any]) -> None:
    """Add a warning if no idempotency key was supplied."""
    warnings: list[str] = post_data.get("warnings", [])
    warnings.append("No Idempotency-Key supplied. Duplicate protection is opt-in for this client.")
    post_data["warnings"] = warnings


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
    idempotency_key: str | None = Query(
        None, description="Idempotency key (for clients that can't set headers)"
    ),
) -> Response:
    if dry_run:
        return Response(
            content='{"dry_run": true}',
            status_code=201,
            media_type="application/json",
        )

    key = _extract_idempotency_key(request, idempotency_key)
    # Use Pydantic serialization for consistent fingerprinting
    body_bytes = body.model_dump_json().encode()

    # Idempotency check
    idem_result = None
    if key is not None:
        idem_result = check_idempotency(
            db,
            actor_id=auth.actor_id,
            key=key,
            method="POST",
            path=f"/v1/sites/{site_slug}/posts",
            body=body_bytes,
        )
        if idem_result.is_replay and idem_result.record is not None:
            status, resp_body, resp_headers = get_stored_response(idem_result.record)
            resp_headers["Idempotent-Replay"] = "true"
            return Response(
                content=json.dumps(resp_body),
                status_code=status,
                media_type="application/json",
                headers=resp_headers,
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
    if key is None:
        warnings.append("No Idempotency-Key supplied. Duplicate protection is opt-in for this client.")
    data["warnings"] = warnings

    # Compute ETag
    etag = compute_etag(post.content_hash, post.revision_count)

    # Render HTML for content negotiation
    renderer = MarkdownRenderer()
    body_html = renderer.render_html(post.body_md)

    resp_headers = {"Location": f"/v1/posts/{post.id}", "ETag": etag}

    # Complete idempotency record (must be JSON-serializable)
    if idem_result is not None and idem_result.record is not None:
        serialized = PostRead(**data).model_dump(mode="json")
        complete_idempotency(
            db,
            idem_result.record,
            request_id=getattr(request.state, "request_id", "-"),
            status_code=201,
            body=serialized,
            headers=resp_headers,
        )
        db.commit()

    return _negotiate_response(
        request,
        data,
        body_html=body_html,
        status_code=201,
        headers=resp_headers,
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
    if_none_match: str | None = Header(None, alias="If-None-Match"),
) -> Response:
    post = get_post(db, identifier)
    site_slug = post.site.slug if post.site else "blog"
    data = _post_to_dict(post, site_slug, session=db)

    # ETag
    etag = compute_etag(post.content_hash, post.revision_count)

    # If-None-Match → 304
    if check_if_none_match(if_none_match, post.content_hash, post.revision_count):
        return Response(status_code=304, headers={"ETag": etag})

    renderer = MarkdownRenderer()
    body_html = renderer.render_html(post.body_md)

    return _negotiate_response(request, data, body_html=body_html, headers={"ETag": etag})


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
    idempotency_key: str | None = Query(None, description="Idempotency key"),
    if_match: str | None = Header(None, alias="If-Match"),
) -> Response:
    if dry_run:
        post = get_post(db, identifier)
        site_slug = post.site.slug if post.site else "blog"
        data = _post_to_dict(post, site_slug, session=db)
        renderer = MarkdownRenderer()
        body_html = renderer.render_html(post.body_md)
        return _negotiate_response(request, data, body_html=body_html)

    key = _extract_idempotency_key(request, idempotency_key)
    # Use Pydantic serialization for consistent fingerprinting
    body_bytes = body.model_dump_json().encode()

    # Idempotency check
    idem_result = None
    if key is not None:
        idem_result = check_idempotency(
            db,
            actor_id=auth.actor_id,
            key=key,
            method="PATCH",
            path=f"/v1/posts/{identifier}",
            body=body_bytes,
        )
        if idem_result.is_replay and idem_result.record is not None:
            status, resp_body, resp_headers = get_stored_response(idem_result.record)
            resp_headers["Idempotent-Replay"] = "true"
            return Response(
                content=json.dumps(resp_body),
                status_code=status,
                media_type="application/json",
                headers=resp_headers,
            )

    # If-Match check
    post = get_post(db, identifier)
    audit_note = None
    if if_match is not None:
        _, audit_note = check_if_match(
            if_match,
            post.content_hash,
            post.revision_count,
            audit_note=True,
        )

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
    if audit_note:
        warnings = data.get("warnings", [])
        warnings.append(audit_note)
        data["warnings"] = warnings

    # ETag
    etag = compute_etag(post.content_hash, post.revision_count)

    renderer = MarkdownRenderer()
    body_html = renderer.render_html(post.body_md)

    resp_headers = {"ETag": etag}

    # Complete idempotency record (must be JSON-serializable)
    if idem_result is not None and idem_result.record is not None:
        serialized = PostRead(**data).model_dump(mode="json")
        complete_idempotency(
            db,
            idem_result.record,
            request_id=getattr(request.state, "request_id", "-"),
            status_code=200,
            body=serialized,
            headers=resp_headers,
        )
        db.commit()

    return _negotiate_response(request, data, body_html=body_html, headers=resp_headers)


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
    idempotency_key: str | None = Query(None, description="Idempotency key"),
    if_match: str | None = Header(None, alias="If-Match"),
) -> Response:
    key = _extract_idempotency_key(request, idempotency_key)

    # Idempotency check
    idem_result = None
    if key is not None:
        idem_result = check_idempotency(
            db,
            actor_id=auth.actor_id,
            key=key,
            method="POST",
            path=f"/v1/posts/{identifier}/publish",
            body=None,
        )
        if idem_result.is_replay and idem_result.record is not None:
            status, resp_body, resp_headers = get_stored_response(idem_result.record)
            resp_headers["Idempotent-Replay"] = "true"
            return Response(
                content=json.dumps(resp_body),
                status_code=status,
                media_type="application/json",
                headers=resp_headers,
            )

    # If-Match check
    post_check = get_post(db, identifier)
    if if_match is not None:
        check_if_match(if_match, post_check.content_hash, post_check.revision_count)

    post, warnings = publish_post(db, identifier)
    site_slug = post.site.slug if post.site else "blog"
    data = _post_to_dict(post, site_slug, session=db)
    data["warnings"] = warnings

    # ETag
    etag = compute_etag(post.content_hash, post.revision_count)
    response_headers: dict[str, str] = {"ETag": etag}

    # Serialize response body for idempotency storage
    serialized_body = PostRead(**data).model_dump(mode="json")

    # Complete idempotency record
    if idem_result is not None and idem_result.record is not None:
        complete_idempotency(
            db,
            idem_result.record,
            request_id=getattr(request.state, "request_id", "-"),
            status_code=200,
            body=serialized_body,
            headers=response_headers,
        )
        db.commit()

    return Response(
        content=PostRead(**data).model_dump_json(),
        status_code=200,
        media_type="application/json",
        headers=response_headers,
    )


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
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    # If-Match check
    post_check = get_post(db, identifier)
    if if_match is not None:
        check_if_match(if_match, post_check.content_hash, post_check.revision_count)

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
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    # If-Match check
    post_check = get_post(db, identifier)
    if if_match is not None:
        check_if_match(if_match, post_check.content_hash, post_check.revision_count)

    post = trash_post(db, identifier)
    site_slug = post.site.slug if post.site else "blog"
    data = _post_to_dict(post, site_slug, session=db)
    return data
