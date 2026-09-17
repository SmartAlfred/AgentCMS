"""Capability links router (#6).

Routes:
* ``GET  /c/{token}``           — instruction sheet (text/plain or HTML)
* ``POST /c/{token}/posts``     — create a post (delegates to posts service)
* ``POST /c/{token}/posts/{id}/publish`` — publish a post

The token is the same plaintext that is used for ``Authorization: Bearer cap_…``
or ``?token=cap_…``.  The instruction sheet is generated from the token's actual
scope so it can never over-promise.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.capability_tokens import (
    check_rate_limit,
    verify_capability_token,
)
from app.services.post import (
    _post_to_dict,
    create_post,
    publish_post,
)

logger = logging.getLogger("app.capability_links")

router = APIRouter(prefix="/c", tags=["capability"])


# ---------------------------------------------------------------------------
# Request schemas (reused from v1)
# ---------------------------------------------------------------------------


class CapPostCreate(BaseModel):
    """Request body for POST /c/{token}/posts."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = None
    body_md: str
    slug: str | None = None
    tags: list[str] = Field(default_factory=list)
    excerpt: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _verify_link_auth(
    token: str,
    request: Request,
    db: Session,
    *,
    required_verb: str | None = None,
) -> tuple[Any, Any]:
    """Verify a capability token and enforce rate limits.

    Returns (actor, link) on success, raises on failure.
    """
    # Determine site slug from the token
    from app.services.capability_tokens import _parse_token

    parsed = _parse_token(token)
    if parsed is None:
        from app.services.capability_tokens import CapabilityTokenError

        raise CapabilityTokenError("Malformed capability token.")
    site_slug, _ = parsed

    actor, link = verify_capability_token(
        db,
        token,
        required_verb=required_verb,
        required_site_slug=site_slug,
    )
    check_rate_limit(db, link)
    return actor, link


def _build_instruction_sheet(
    token: str,
    link: Any,
    site_slug: str,
    request: Request,
) -> str:
    """Build the text/plain instruction sheet for agents."""
    verbs = link.verbs or []
    verb_list = ", ".join(verbs)
    base = str(request.base_url).rstrip("/")

    lines = [
        f'You have access to site "{site_slug}" via a capability link.',
        f"Allowed permissions: {verb_list}.",
    ]

    if link.expires_at:
        lines.append(f"Link expires: {link.expires_at.isoformat()}.")

    lines.append("")
    lines.append("---")
    lines.append("")

    # POST /c/{token}/posts
    if "posts:write" in verbs:
        lines.append(f"POST {base}/c/{token}/posts")
        lines.append("Content-Type: application/json")
        lines.append("")
        lines.append('{"title": "...", "body_md": "# ...", "tags": ["..."]}')
        lines.append("")

    # POST /c/{token}/posts/{id}/publish
    if "posts:publish" in verbs:
        lines.append(f"POST {base}/c/{{post_id}}/publish")
        lines.append("Returns the published post.")
        lines.append("")

    # GET /c/{token}/posts
    if "posts:read" in verbs:
        lines.append(f"GET {base}/c/{token}/posts")
        lines.append("List posts on this site.")
        lines.append("")

    lines.append('Error responses are JSON: {"title", "detail", "hint"} — read "hint" and retry.')
    lines.append("Rate limit: 30 writes/min. Max body 256 KB.")
    lines.append("")
    lines.append("Full spec: /llms.txt")

    return "\n".join(lines)


def _build_instruction_html(
    token: str,
    link: Any,
    site_slug: str,
    request: Request,
) -> str:
    """Build the HTML instruction page for browsers."""
    verbs = link.verbs or []
    verb_list = ", ".join(verbs)

    sections: list[str] = []
    if "posts:write" in verbs:
        sections.append(
            "<h2>Create a Post</h2>"
            "<pre>POST /c/{token}/posts\n"
            "Content-Type: application/json\n\n"
            '{"title": "...", "body_md": "# ...", "tags": ["..."]}</pre>'
        )
    if "posts:publish" in verbs:
        sections.append("<h2>Publish a Post</h2><pre>POST /c/{token}/posts/{id}/publish</pre>")
    if "posts:read" in verbs:
        sections.append("<h2>List Posts</h2><pre>GET /c/{token}/posts</pre>")

    expires = ""
    if link.expires_at:
        expires = f"<p>Link expires: {link.expires_at.isoformat()}</p>"

    return (
        "<!DOCTYPE html><html><head><title>Capability Link</title></head><body>"
        f"<h1>Capability Link — Site: {site_slug}</h1>"
        f"<p>Permissions: {verb_list}</p>"
        f"{expires}"
        + "\n".join(sections)
        + "<p>Error responses are JSON with a <code>hint</code> field.</p>"
        + "<p>Rate limit: 30 writes/min. Max body 256 KB.</p>"
        + "</body></html>"
    )


# ---------------------------------------------------------------------------
# GET /c/{token} — instruction sheet
# ---------------------------------------------------------------------------


@router.get(
    "/{token}",
    summary="Capability link instruction sheet",
    include_in_schema=False,
)
def capability_instruction_sheet(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Return an instruction sheet for the capability link.

    Content negotiation: text/plain for agents, HTML for browsers.
    """
    from app.services.capability_tokens import (
        CapabilityTokenError,
        CapabilityTokenExpired,
        CapabilityTokenRevoked,
        _parse_token,
    )

    parsed = _parse_token(token)
    if parsed is None:
        return Response(
            content="Malformed capability token.",
            status_code=401,
            media_type="text/plain",
        )

    site_slug, _ = parsed

    try:
        _actor, link = verify_capability_token(db, token)
    except (CapabilityTokenError, CapabilityTokenExpired, CapabilityTokenRevoked) as exc:
        status = exc.status_code
        return Response(
            content=str(exc.detail),
            status_code=status,
            media_type="text/plain",
        )

    # Content negotiation
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        html = _build_instruction_html(token, link, site_slug, request)
        return Response(content=html, status_code=200, media_type="text/html")

    text = _build_instruction_sheet(token, link, site_slug, request)
    return Response(content=text, status_code=200, media_type="text/plain")


# ---------------------------------------------------------------------------
# POST /c/{token}/posts — create a post
# ---------------------------------------------------------------------------


@router.post(
    "/{token}/posts",
    summary="Create a post via capability link",
    status_code=201,
    tags=["posts"],
)
def create_post_via_link(
    token: str,
    body: CapPostCreate,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Create a post using a capability link.

    The capability token authenticates and authorizes the request.
    Posts are always created as drafts.
    """
    from app.services.capability_tokens import _parse_token

    parsed = _parse_token(token)
    if parsed is None:
        from app.services.capability_tokens import CapabilityTokenError

        raise CapabilityTokenError("Malformed capability token.")

    site_slug, _ = parsed
    _actor, _link = _verify_link_auth(token, request, db, required_verb="posts:write")

    post = create_post(
        db,
        site_slug,
        body_md=body.body_md,
        title=body.title,
        slug=body.slug,
        tags=body.tags,
        excerpt=body.excerpt,
    )

    data = _post_to_dict(post, site_slug, session=db)
    data["warnings"] = []
    if body.title is None:
        data["warnings"].append("Title was derived from the first H1 in body_md.")
    if body.slug is None:
        data["warnings"].append("Slug was derived from the title.")

    from app.services.post import _post_to_dict as _p2d

    data_dict = _p2d(post, site_slug, session=db)
    data_dict["warnings"] = data["warnings"]

    return Response(
        status_code=201,
        content=__import__("json").dumps(data_dict, default=str),
        media_type="application/json",
        headers={"Location": f"/v1/posts/{post.id}"},
    )


# ---------------------------------------------------------------------------
# POST /c/{token}/posts/{post_id}/publish — publish a post
# ---------------------------------------------------------------------------


@router.post(
    "/{token}/posts/{post_id}/publish",
    summary="Publish a post via capability link",
    tags=["posts"],
)
def publish_post_via_link(
    token: str,
    post_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Publish a post using a capability link.

    Requires the ``posts:publish`` verb in the capability token.
    """
    from app.services.capability_tokens import _parse_token

    parsed = _parse_token(token)
    if parsed is None:
        from app.services.capability_tokens import CapabilityTokenError

        raise CapabilityTokenError("Malformed capability token.")

    site_slug, _ = parsed
    _actor, _link = _verify_link_auth(token, request, db, required_verb="posts:publish")

    post, warnings = publish_post(db, post_id)
    data = _post_to_dict(post, site_slug, session=db)
    data["warnings"] = warnings
    return data
