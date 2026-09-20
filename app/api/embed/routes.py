"""Embed API routes (#31).

Versioned embed surface for drop-in integration into existing websites.

Routes:
* ``GET /embed/v1/agentcms.js`` — Self-contained vanilla JS embed script
* ``GET /embed/v1/posts`` — JSON feed of published posts for a site (token-scoped)
* ``GET /embed/v1/config`` — Embed configuration (CORS, theming info)

Token safety: The embed token is read-only (``posts:read`` scope). A write-scoped
token presented to the embed endpoint is rejected with 403.

CORS: The embed endpoint respects ``EMBED_ORIGINS`` from settings (comma-separated
list of allowed embedding origins). Default is deny-all.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.session import get_db
from app.models.capability_link import CapabilityLink
from app.models.site import Site
from app.services.capability_tokens import (
    CapabilityLinkForbidden,
    CapabilityTokenError,
    CapabilityTokenExpired,
    CapabilityTokenRevoked,
    _parse_token,
    verify_capability_token,
)
from app.services.public import (
    PAGE_SIZE,
    compute_etag,
    get_site,
    list_published_posts,
)

router = APIRouter(prefix="/embed/v1", tags=["embed"], include_in_schema=False)

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class EmbedPost(BaseModel):
    """Published post for embed consumption."""

    model_config = ConfigDict(str_strip_whitespace=True)

    id: str
    title: str
    slug: str
    url: str
    excerpt: str | None = None
    published_at: str
    site_slug: str
    tags: list[EmbedTag] = []


class EmbedTag(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str
    slug: str
    url: str


class EmbedPostsResponse(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    site_name: str
    site_slug: str
    posts: list[EmbedPost]


class EmbedConfigResponse(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    version: str
    allowed_origins: list[str]
    token_scope: str
    theming: dict[str, str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_base_url(request: Request) -> str:
    settings: Settings = request.app.state.settings
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    return f"{request.url.scheme}://{request.url.netloc}"


def _verify_embed_token(
    token: str,
    db: Session,
    required_verb: str = "posts:read",
) -> tuple[CapabilityLink, Site]:
    """Verify an embed token and return (link, site).

    Raises HTTPException for invalid/expired/revoked/wrong-scope tokens.
    Embed tokens must be read-only (no write verbs).
    """
    if not token or not token.startswith("cap_"):
        raise HTTPException(status_code=401, detail="Invalid embed token format")

    parsed = _parse_token(token)
    if parsed is None:
        raise HTTPException(status_code=401, detail="Malformed embed token")

    site_slug, _ = parsed

    try:
        _actor, _link = verify_capability_token(
            db,
            token,
            required_verb=required_verb,
            required_site_slug=site_slug,
        )
    except CapabilityTokenError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    except CapabilityTokenExpired as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    except CapabilityTokenRevoked as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    except CapabilityLinkForbidden as exc:
        # This is the key enforcement: write tokens rejected for embed
        raise HTTPException(
            status_code=403,
            detail=exc.detail,
            headers={"X-Embed-Error": "write-token-rejected"},
        ) from None

    # Embed tokens must be read-only: reject any token with write verbs
    write_verbs = {"posts:write", "posts:publish"}
    link_verbs = set(_link.verbs or [])
    if link_verbs & write_verbs:
        raise HTTPException(
            status_code=403,
            detail="Embed tokens must be read-only (no write or publish verbs).",
            headers={"X-Embed-Error": "write-token-rejected"},
        )

    # Load the site
    site = get_site(db, site_slug)
    if site is None:
        raise HTTPException(status_code=404, detail=f"Site '{site_slug}' not found")

    return _link, site


def _apply_cors_headers(response: Response, request: Request) -> None:
    """Apply CORS headers based on EMBED_ORIGINS setting."""
    settings: Settings = request.app.state.settings
    if not settings.embed_origins:
        return  # Deny all by default

    origin = request.headers.get("origin")
    if not origin:
        return

    if origin in settings.embed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Credentials"] = "true"


# ---------------------------------------------------------------------------
# GET /embed/v1/agentcms.js — Serve the embed script
# ---------------------------------------------------------------------------


@router.get(
    "/agentcms.js",
    summary="AgentCMS embed script (vanilla JS, no dependencies)",
    response_class=FileResponse,
    include_in_schema=False,
)
async def serve_embed_script(request: Request) -> FileResponse:
    """Serve the self-contained embed script.

    The script is static but we serve it through FastAPI to:
    1. Apply CORS headers based on EMBED_ORIGINS
    2. Set proper caching headers
    3. Allow versioning via URL path (/embed/v1/)
    """
    script_path = Path(__file__).parent / "agentcms_embed.js"

    response = FileResponse(
        path=script_path,
        media_type="application/javascript; charset=utf-8",
        filename="agentcms.js",
    )

    # Caching: 1 hour, revalidate
    response.headers["Cache-Control"] = "public, max-age=3600, stale-while-revalidate=86400"
    response.headers["X-Content-Type-Options"] = "nosniff"

    # CORS for embedding origins
    _apply_cors_headers(response, request)

    return response


# ---------------------------------------------------------------------------
# GET /embed/v1/posts — Token-scoped published posts feed
# ---------------------------------------------------------------------------


@router.get(
    "/posts",
    summary="Published posts for embed (token-scoped, read-only)",
    response_model=EmbedPostsResponse,
)
async def embed_posts(
    request: Request,
    token: str = Query(..., description="Capability token (cap_...) with posts:read scope"),
    limit: int = Query(10, ge=1, le=50, description="Max posts to return"),
    page: int = Query(1, ge=1, description="Page number"),
    tag: str | None = Query(None, description="Filter by tag slug"),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Return published posts for the site associated with the embed token.

    - Token must have ``posts:read`` scope (enforced)
    - Write-scoped tokens are rejected with 403
    - Only published posts are returned
    - Respects EMBED_ORIGINS CORS allowlist
    """
    _link, site = _verify_embed_token(token, db, required_verb="posts:read")

    # Fetch published posts
    posts, _total, _total_pages = list_published_posts(
        db,
        site.slug,
        page=page,
        tag=tag,
        page_size=min(limit, PAGE_SIZE),
    )

    # Build response
    embed_posts = []
    for post in posts:
        tags = []
        for tag_link in post.tag_links:
            tag_obj = tag_link.tag
            if tag_obj is not None:
                tags.append(
                    EmbedTag(
                        name=tag_obj.name,
                        slug=tag_obj.slug,
                        url=f"{_get_base_url(request)}/{site.slug}/tags/{tag_obj.slug}",
                    )
                )
        embed_posts.append(
            EmbedPost(
                id=str(post.id),
                title=post.title,
                slug=post.slug,
                url=f"{_get_base_url(request)}/{site.slug}/{post.slug}",
                excerpt=post.excerpt,
                published_at=post.published_at.isoformat() if post.published_at else "",
                site_slug=site.slug,
                tags=tags,
            )
        )

    response = JSONResponse(
        content=EmbedPostsResponse(
            site_name=site.name,
            site_slug=site.slug,
            posts=embed_posts,
        ).model_dump(mode="json"),
    )

    # ETag for caching
    etag = compute_etag(posts[0]) if posts else f"empty-{site.slug}-embed"
    response.headers["ETag"] = f'"{etag}"'
    if posts and posts[0].updated_at:
        response.headers["Last-Modified"] = posts[0].updated_at.strftime("%a, %d %b %Y %H:%M:%S GMT")
    response.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=600"

    # CORS
    _apply_cors_headers(response, request)

    return response


# ---------------------------------------------------------------------------
# GET /embed/v1/config — Embed configuration (for debugging)
# ---------------------------------------------------------------------------


@router.get(
    "/config",
    summary="Embed configuration",
    response_model=EmbedConfigResponse,
)
async def embed_config(request: Request) -> JSONResponse:
    """Return embed configuration for debugging/verification."""
    settings: Settings = request.app.state.settings

    response = JSONResponse(
        content=EmbedConfigResponse(
            version="1",
            allowed_origins=settings.embed_origins,
            token_scope=settings.embed_token_scope,
            theming={
                "css_variables": "See :root --agentcms-* in embed script",
                "color_scheme": "auto (respects prefers-color-scheme)",
            },
        ).model_dump(mode="json"),
    )

    _apply_cors_headers(response, request)
    return response


# ---------------------------------------------------------------------------
# GET /embed/v1/iframe — Iframe fallback for hosts blocking third-party scripts
# ---------------------------------------------------------------------------


@router.get(
    "/iframe",
    summary="Iframe fallback embed (for hosts blocking third-party scripts)",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def serve_embed_iframe(request: Request) -> HTMLResponse:
    """Serve the iframe fallback embed page.

    The iframe loads the embed script internally and renders posts.
    Used when the host site blocks third-party scripts but allows iframes.

    Query parameters:
    - token: Capability token (cap_...) with posts:read scope
    - limit: Max posts to show (default 10)
    - theme: light, dark, or auto (default auto)
    """
    iframe_path = Path(__file__).parent / "agentcms_embed_iframe.html"

    response = HTMLResponse(
        content=iframe_path.read_text(encoding="utf-8"),
        status_code=200,
    )

    # Caching: 1 hour, revalidate
    response.headers["Cache-Control"] = "public, max-age=3600, stale-while-revalidate=86400"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"

    # CORS for embedding origins (iframe needs to be allowed in frame-ancestors)
    _apply_cors_headers(response, request)

    return response


# ---------------------------------------------------------------------------
# OPTIONS handlers for CORS preflight
# ---------------------------------------------------------------------------


@router.options("/posts", include_in_schema=False)
async def embed_posts_options(request: Request) -> Response:
    response = Response(status_code=204)
    _apply_cors_preflight(response, request)
    return response


@router.options("/config", include_in_schema=False)
async def embed_config_options(request: Request) -> Response:
    response = Response(status_code=204)
    _apply_cors_preflight(response, request)
    return response


@router.options("/iframe", include_in_schema=False)
async def embed_iframe_options(request: Request) -> Response:
    response = Response(status_code=204)
    _apply_cors_preflight(response, request)
    return response


def _apply_cors_preflight(response: Response, request: Request) -> None:
    settings: Settings = request.app.state.settings
    if not settings.embed_origins:
        return

    origin = request.headers.get("origin")
    if not origin:
        return

    if origin in settings.embed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Max-Age"] = "86400"
