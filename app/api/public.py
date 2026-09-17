"""Public read surface (ticket #8).

Unauthenticated routes that serve the blog:

* ``GET /{site}/{slug}`` — post page (HTML)
* ``GET /{site}`` — index page (paginated, ``?tag=`` / ``?page=``)
* ``GET /{site}/tags/{tag}`` — tag-filtered index
* ``GET /{site}/posts.json`` — paginated JSON
* ``GET /{site}/posts/{slug}.md`` — raw markdown
* ``GET /{site}/posts/{slug}.json`` — single post JSON
* ``GET /sitemap.xml`` — XML sitemap
* ``GET /robots.txt`` — robots.txt with AI-crawler guidance
* ``GET /{site}/rss.xml`` — RSS 2.0 feed
* ``GET /{site}/atom.xml`` — Atom 1.0 feed
* ``GET /{site}/feed.json`` — JSON Feed 1.1
* ``GET /{site}/posts/{slug}?preview=<token>`` — draft preview

Caching:
* ETag derived from ``content_hash`` + ``updated_at``
* Last-Modified from ``updated_at``
* 304 on conditional GET
* Cache-Control: ``public, max-age=60, stale-while-revalidate=600``

Trashed/unpublished posts return 404 (never 403).
Renamed slugs return 301 via the ``redirects`` table.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any
from xml.dom import minidom

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from app.models.post import Post
from app.services.markdown import MarkdownRenderer
from app.services.preview import verify_preview_token
from app.services.public import (
    PAGE_SIZE,
    check_redirect,
    compute_etag,
    get_published_post,
    get_site,
    list_published_posts,
    list_published_posts_for_site,
    post_to_public_dict,
)
from app.templates import render_index_page, render_post_page

router = APIRouter(tags=["public"], include_in_schema=False)

_NOINDEX = "noindex, nofollow"
_CACHE_PUBLIC = "public, max-age=60, stale-while-revalidate=600"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_base_url(request: Request) -> str:
    """Derive the base URL from the request."""
    return f"{request.url.scheme}://{request.url.netloc}"


def _site_name(session: Session, site_slug: str) -> str:
    """Get a site's display name."""
    site = get_site(session, site_slug)
    return site.name if site else site_slug


def _require_site(session: Session, site_slug: str) -> None:
    """Raise 404 if the site doesn't exist (problem+json)."""
    if get_site(session, site_slug) is None:
        raise HTTPException(status_code=404, detail=f"Site '{site_slug}' not found.")


def _conditional_response(
    request: Request,
    etag: str,
    last_modified: datetime | None,
    *,
    body: bytes,
    media_type: str = "text/html; charset=utf-8",
    cache_control: str = _CACHE_PUBLIC,
    headers: dict[str, str] | None = None,
) -> Response:
    """Return 304 if If-None-Match or If-Modified-Since matches."""
    resp_headers = dict(headers or {})
    resp_headers["ETag"] = f'"{etag}"'
    if last_modified:
        resp_headers["Last-Modified"] = last_modified.strftime("%a, %d %b %Y %H:%M:%S GMT")
    # Only set Cache-Control if not already provided by caller
    if "Cache-Control" not in resp_headers:
        resp_headers["Cache-Control"] = cache_control

    in_none_match = request.headers.get("if-none-match", "").strip('"')
    if in_none_match and in_none_match == etag:
        return Response(status_code=304, headers=resp_headers)

    in_mod_since = request.headers.get("if-modified-since")
    if in_mod_since and last_modified:
        try:
            from email.utils import parsedate_to_datetime

            client_date = parsedate_to_datetime(in_mod_since)
            # Normalize: strip tzinfo and microseconds for naive comparison
            # (SQLAlchemy server_default returns naive UTC datetimes)
            client_naive = client_date.replace(tzinfo=None, microsecond=0)
            server_naive = last_modified.replace(microsecond=0)
            if server_naive <= client_naive:
                return Response(status_code=304, headers=resp_headers)
        except (ValueError, TypeError):
            pass

    return Response(
        content=body,
        status_code=200,
        media_type=media_type,
        headers=resp_headers,
    )


def _get_post_or_404(
    session: Session,
    site_slug: str,
    slug: str,
    *,
    preview_token: str | None = None,
) -> tuple[Post, dict[str, Any], bool] | tuple[None, None, None]:
    """Fetch a published post, handling redirects and preview.

    Returns (post, post_dict, is_preview) or (None, None, None) if 404.
    """
    redirect = check_redirect(session, site_slug, slug)
    if redirect is not None:
        new_post = get_published_post(session, site_slug, redirect.new_slug)
        if new_post is None:
            return None, None, None
        return new_post, post_to_public_dict(new_post, site_slug), False

    post = get_published_post(session, site_slug, slug)

    if post is None and preview_token:
        post_id = verify_preview_token(preview_token)
        if post_id is not None:
            from sqlalchemy.orm import joinedload

            from app.models.post import Post as PostModel

            post = (
                session.query(PostModel)
                .options(joinedload(PostModel.site))
                .filter(PostModel.id == post_id)
                .first()
            )
            if post is not None and post.site is not None and post.site.slug == site_slug:
                return post, post_to_public_dict(post, site_slug), True

    if post is None:
        return None, None, None

    return post, post_to_public_dict(post, site_slug), False


def _open_db() -> Session:
    from app.db.session import get_db as _get_db

    return next(_get_db())


# ---------------------------------------------------------------------------
# FIXED-PATH DISCOVERY: GET /sitemap.xml, GET /robots.txt
# (These must come before any catch-all routes.)
# ---------------------------------------------------------------------------


@router.get("/sitemap.xml")
def sitemap(request: Request) -> Response:
    """Generate an XML sitemap of all published posts across all sites."""
    from app.models.site import Site

    db = _open_db()
    try:
        sites = db.query(Site).all()
        base_url = _get_base_url(request)

        urlset = ET.Element("urlset")
        urlset.set("xmlns", "http://www.sitemaps.org/schemas/sitemap/0.9")

        for site in sites:
            url_el = ET.SubElement(urlset, "url")
            ET.SubElement(url_el, "loc").text = f"{base_url}/{site.slug}"
            ET.SubElement(url_el, "changefreq").text = "daily"
            ET.SubElement(url_el, "priority").text = "0.8"

            posts = list_published_posts_for_site(db, site.id)
            for post in posts:
                url_el = ET.SubElement(urlset, "url")
                ET.SubElement(url_el, "loc").text = f"{base_url}/{site.slug}/{post.slug}"
                if post.updated_at:
                    ET.SubElement(url_el, "lastmod").text = post.updated_at.strftime("%Y-%m-%d")
                ET.SubElement(url_el, "changefreq").text = "weekly"
                ET.SubElement(url_el, "priority").text = "0.6"

        xml_str = minidom.parseString(ET.tostring(urlset, encoding="unicode")).toprettyxml(indent="  ")
        lines = xml_str.split("\n")
        if lines and lines[0].startswith("<?xml"):
            xml_str = "\n".join(lines[1:])
        xml_str = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_str

        return Response(
            content=xml_str,
            media_type="application/xml; charset=utf-8",
            headers={"Cache-Control": _CACHE_PUBLIC},
        )
    finally:
        db.close()


@router.get("/robots.txt")
def robots(request: Request) -> Response:
    """Generate robots.txt with AI-crawler guidance."""
    base_url = _get_base_url(request)
    content = (
        "User-agent: *\n"
        "Allow: /\n"
        "\n"
        "# AI crawlers: you may crawl published content freely.\n"
        "# AgentCMS serves machine-readable formats at every URL:\n"
        "#   .md  -> raw markdown\n"
        "#   .json -> structured JSON\n"
        "#   /posts.json -> paginated listing\n"
        "User-agent: GPTBot\n"
        "Allow: /\n"
        "\n"
        "User-agent: Google-Extended\n"
        "Allow: /\n"
        "\n"
        "User-agent: CCBot\n"
        "Allow: /\n"
        "\n"
        "User-agent: anthropic-ai\n"
        "Allow: /\n"
        "\n"
        "User-agent: Cohere-ai\n"
        "Allow: /\n"
        "\n"
        f"Sitemap: {base_url}/sitemap.xml\n"
    )
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": _CACHE_PUBLIC},
    )


# ---------------------------------------------------------------------------
# SITE-SCOPED SPECIFIC ROUTES (must come before /{site}/{slug} catch-all)
# GET /{site}/posts.json, /{site}/posts/{slug}.md, etc.
# ---------------------------------------------------------------------------


@router.get("/{site_slug}/posts.json")
def posts_json(
    site_slug: str,
    request: Request,
    page: int = Query(1, ge=1),
    tag: str | None = Query(None),
) -> Response:
    """Return published posts as paginated JSON."""
    db = _open_db()
    try:
        _require_site(db, site_slug)
        posts, total, total_pages = list_published_posts(
            db, site_slug, page=page, tag=tag, page_size=PAGE_SIZE
        )

        items = [post_to_public_dict(p, site_slug) for p in posts]
        body = json.dumps(
            {
                "items": items,
                "page": page,
                "total_pages": total_pages,
                "total": total,
            },
            ensure_ascii=False,
            default=str,
        )

        latest_etag = compute_etag(posts[0]) if posts else f"empty-{site_slug}-json"
        return _conditional_response(
            request,
            latest_etag,
            posts[0].updated_at if posts else None,
            body=body.encode(),
            media_type="application/json; charset=utf-8",
        )
    finally:
        db.close()


@router.get("/{site_slug}/posts/{slug}.md")
def post_markdown(site_slug: str, slug: str, request: Request) -> Response:
    """Return the raw markdown of a published post."""
    db = _open_db()
    try:
        _require_site(db, site_slug)
        post = get_published_post(db, site_slug, slug)
        if post is None:
            redirect = check_redirect(db, site_slug, slug)
            if redirect:
                post = get_published_post(db, site_slug, redirect.new_slug)
        if post is None:
            raise HTTPException(status_code=404, detail="Post not found.")

        etag = compute_etag(post)
        return _conditional_response(
            request,
            etag,
            post.updated_at,
            body=post.body_md.encode(),
            media_type="text/markdown; charset=utf-8",
        )
    finally:
        db.close()


@router.get("/{site_slug}/posts/{slug}.json")
def post_json(site_slug: str, slug: str, request: Request) -> Response:
    """Return a published post as JSON."""
    db = _open_db()
    try:
        _require_site(db, site_slug)
        post = get_published_post(db, site_slug, slug)
        if post is None:
            redirect = check_redirect(db, site_slug, slug)
            if redirect:
                post = get_published_post(db, site_slug, redirect.new_slug)
        if post is None:
            raise HTTPException(status_code=404, detail="Post not found.")

        etag = compute_etag(post)
        data = post_to_public_dict(post, site_slug)
        body = json.dumps(data, ensure_ascii=False, default=str)
        return _conditional_response(
            request,
            etag,
            post.updated_at,
            body=body.encode(),
            media_type="application/json; charset=utf-8",
        )
    finally:
        db.close()


@router.get("/{site_slug}/tags/{tag_slug}")
def tag_page(
    site_slug: str,
    tag_slug: str,
    request: Request,
    page: int = Query(1, ge=1),
) -> Response:
    """Render posts filtered by tag."""
    db = _open_db()
    try:
        _require_site(db, site_slug)
        posts, _total, total_pages = list_published_posts(
            db, site_slug, page=page, tag=tag_slug, page_size=PAGE_SIZE
        )

        post_dicts = [post_to_public_dict(p, site_slug) for p in posts]
        site_name_val = _site_name(db, site_slug)
        base_url = _get_base_url(request)

        html_content = render_index_page(
            posts=post_dicts,
            site_name=site_name_val,
            site_slug=site_slug,
            base_url=base_url,
            page=page,
            total_pages=total_pages,
            tag=tag_slug,
        )

        latest_etag = compute_etag(posts[0]) if posts else f"empty-tag-{site_slug}-{tag_slug}"
        return _conditional_response(
            request,
            latest_etag,
            posts[0].updated_at if posts else None,
            body=html_content.encode(),
        )
    finally:
        db.close()


@router.get("/{site_slug}/rss.xml")
def rss_feed(site_slug: str, request: Request) -> Response:
    """Generate an RSS 2.0 feed for a site."""
    db = _open_db()
    try:
        _require_site(db, site_slug)
        site = get_site(db, site_slug)
        posts = list_published_posts_for_site(db, site.id)  # type: ignore[union-attr]
        base_url = _get_base_url(request)

        rss = ET.Element("rss")
        rss.set("version", "2.0")
        rss.set("xmlns:atom", "http://www.w3.org/2005/Atom")
        channel = ET.SubElement(rss, "channel")
        ET.SubElement(channel, "title").text = site.name  # type: ignore[union-attr]
        ET.SubElement(channel, "link").text = f"{base_url}/{site_slug}"
        ET.SubElement(channel, "description").text = f"Posts from {site.name}"  # type: ignore[union-attr]
        ET.SubElement(channel, "language").text = "en"

        atom_link = ET.SubElement(channel, "atom:link")
        atom_link.set("href", f"{base_url}/{site_slug}/rss.xml")
        atom_link.set("rel", "self")
        atom_link.set("type", "application/rss+xml")

        for post in posts[:20]:
            item = ET.SubElement(channel, "item")
            ET.SubElement(item, "title").text = post.title
            ET.SubElement(item, "link").text = f"{base_url}/{site_slug}/{post.slug}"
            ET.SubElement(item, "guid").text = f"{base_url}/{site_slug}/{post.slug}"
            if post.excerpt:
                ET.SubElement(item, "description").text = post.excerpt
            if post.published_at:
                ET.SubElement(item, "pubDate").text = post.published_at.strftime(
                    "%a, %d %b %Y %H:%M:%S +0000"
                )

        xml_str = minidom.parseString(ET.tostring(rss, encoding="unicode")).toprettyxml(indent="  ")
        lines = xml_str.split("\n")
        if lines and lines[0].startswith("<?xml"):
            xml_str = "\n".join(lines[1:])
        xml_str = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_str

        return Response(
            content=xml_str,
            media_type="application/rss+xml; charset=utf-8",
            headers={"Cache-Control": _CACHE_PUBLIC},
        )
    finally:
        db.close()


@router.get("/{site_slug}/atom.xml")
def atom_feed(site_slug: str, request: Request) -> Response:
    """Generate an Atom 1.0 feed for a site."""
    db = _open_db()
    try:
        _require_site(db, site_slug)
        site = get_site(db, site_slug)
        posts = list_published_posts_for_site(db, site.id)  # type: ignore[union-attr]
        base_url = _get_base_url(request)
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        feed = ET.Element("feed")
        feed.set("xmlns", "http://www.w3.org/2005/Atom")
        ET.SubElement(feed, "title").text = site.name  # type: ignore[union-attr]
        ET.SubElement(feed, "link").set("href", f"{base_url}/{site_slug}")
        ET.SubElement(feed, "id").text = f"{base_url}/{site_slug}"
        ET.SubElement(feed, "updated").text = now

        for post in posts[:20]:
            entry = ET.SubElement(feed, "entry")
            ET.SubElement(entry, "title").text = post.title
            ET.SubElement(entry, "link").set("href", f"{base_url}/{site_slug}/{post.slug}")
            ET.SubElement(entry, "id").text = f"{base_url}/{site_slug}/{post.slug}"
            if post.published_at:
                ET.SubElement(entry, "published").text = post.published_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            ET.SubElement(entry, "updated").text = (
                post.updated_at.strftime("%Y-%m-%dT%H:%M:%SZ") if post.updated_at else now
            )
            if post.excerpt:
                summary = ET.SubElement(entry, "summary")
                summary.text = post.excerpt

        xml_str = minidom.parseString(ET.tostring(feed, encoding="unicode")).toprettyxml(indent="  ")
        lines = xml_str.split("\n")
        if lines and lines[0].startswith("<?xml"):
            xml_str = "\n".join(lines[1:])
        xml_str = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_str

        return Response(
            content=xml_str,
            media_type="application/atom+xml; charset=utf-8",
            headers={"Cache-Control": _CACHE_PUBLIC},
        )
    finally:
        db.close()


@router.get("/{site_slug}/feed.json")
def json_feed(site_slug: str, request: Request) -> Response:
    """Generate a JSON Feed 1.1 for a site."""
    db = _open_db()
    try:
        _require_site(db, site_slug)
        site = get_site(db, site_slug)
        posts = list_published_posts_for_site(db, site.id)  # type: ignore[union-attr]
        base_url = _get_base_url(request)

        items = []
        for post in posts[:20]:
            item: dict[str, Any] = {
                "id": f"{base_url}/{site_slug}/{post.slug}",
                "slug": post.slug,
                "title": post.title,
                "url": f"{base_url}/{site_slug}/{post.slug}",
            }
            if post.excerpt:
                item["summary"] = post.excerpt
            if post.body_md:
                item["content_html"] = MarkdownRenderer().render_html(post.body_md)
            if post.published_at:
                item["date_published"] = post.published_at.isoformat()
            if post.updated_at:
                item["date_modified"] = post.updated_at.isoformat()
            items.append(item)

        feed = {
            "version": "https://jsonfeed.org/version/1.1",
            "title": site.name,  # type: ignore[union-attr]
            "home_page_url": f"{base_url}/{site_slug}",
            "feed_url": f"{base_url}/{site_slug}/feed.json",
            "items": items,
        }

        body = json.dumps(feed, ensure_ascii=False, default=str)
        return Response(
            content=body,
            media_type="application/feed+json; charset=utf-8",
            headers={"Cache-Control": _CACHE_PUBLIC},
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# CATCH-ALL ROUTES (must come LAST)
# GET /{site}/{slug}, GET /{site}
# ---------------------------------------------------------------------------


@router.get("/{site_slug}/{slug}")
def post_page(
    site_slug: str,
    slug: str,
    request: Request,
    preview: str | None = Query(None),
) -> Response:
    """Render a published post as a semantic HTML page."""
    db = _open_db()
    try:
        _require_site(db, site_slug)
        result = _get_post_or_404(db, site_slug, slug, preview_token=preview)
        if result[0] is None or result[1] is None:
            raise HTTPException(status_code=404, detail="Post not found.")
        post = result[0]
        post_dict = result[1]
        is_preview = result[2]

        etag = compute_etag(post)
        body_html = MarkdownRenderer().render_html(post.body_md)
        base_url = _get_base_url(request)
        site_name = _site_name(db, site_slug)

        html_content = render_post_page(
            post=post_dict,
            body_html=body_html,
            site_name=site_name,
            site_slug=site_slug,
            base_url=base_url,
            is_preview=is_preview,
        )

        resp_headers: dict[str, str] = {}
        if is_preview:
            resp_headers["X-Robots-Tag"] = "noindex"
            resp_headers["Cache-Control"] = "no-store"

        return _conditional_response(
            request,
            etag,
            post.updated_at,
            body=html_content.encode(),
            headers=resp_headers,
        )
    finally:
        db.close()


@router.get("/{site_slug}")
def site_index(
    site_slug: str,
    request: Request,
    page: int = Query(1, ge=1),
    tag: str | None = Query(None),
) -> Response:
    """Render the site index page with published posts."""
    db = _open_db()
    try:
        _require_site(db, site_slug)
        posts, _total, total_pages = list_published_posts(
            db, site_slug, page=page, tag=tag, page_size=PAGE_SIZE
        )

        post_dicts = [post_to_public_dict(p, site_slug) for p in posts]
        site_name_val = _site_name(db, site_slug)
        base_url = _get_base_url(request)

        html_content = render_index_page(
            posts=post_dicts,
            site_name=site_name_val,
            site_slug=site_slug,
            base_url=base_url,
            page=page,
            total_pages=total_pages,
            tag=tag,
        )

        latest_etag = compute_etag(posts[0]) if posts else f"empty-{site_slug}"
        return _conditional_response(
            request,
            latest_etag,
            posts[0].updated_at if posts else None,
            body=html_content.encode(),
        )
    finally:
        db.close()
