"""Deterministic static export (#23).

Turns one site into a self-contained, byte-reproducible static bundle for
GitHub Pages hosting:

* ``dist/index.html`` — site index (paginated, mirrors ``GET /{site}``)
* ``dist/page/{n}/index.html`` — further index pages
* ``dist/posts/{slug}/index.{html,md,json}`` — post page, raw markdown, JSON
* ``dist/tags/{tag}/index.html`` — tag-filtered index pages
* ``dist/media/{sha256}/{name}`` — content-hashed media (copied from ``media_root``)
* ``dist/rss.xml|atom.xml|feed.json|sitemap.xml|robots.txt|404.html``
* ``dist/manifest.json`` — machine-readable index of everything

Determinism principles:

* rendered HTML/feeds reuse the exact same pipeline as the public API
  (``render_post_page`` / ``render_index_page`` / ``post_to_public_dict``);
* no wall-clock values enter the output: feed timestamps come from the
  site's newest ``updated_at`` unless a ``generated_at`` override is given;
* archives sort members and pin metadata so two identical trees compress
  to identical bytes.

Incremental exports reuse unchanged post files from a previous ``manifest.json``
(compared by a ``render_hash`` over everything that affects rendering), and
prune files that are no longer part of the tree.  ``since=<iso>`` restricts a
run to post files whose ``updated_at`` falls inside the window; every other
file is reused from the prior manifest.
"""

from __future__ import annotations

import gzip
import hashlib
import ipaddress
import json
import shutil
import tarfile
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.domain.errors import DomainError
from app.models.asset import Asset
from app.models.post import Post
from app.models.site import Site
from app.models.tag import PostTag, Tag
from app.services.markdown import MarkdownRenderer
from app.services.media import make_storage_key
from app.services.public import (
    PAGE_SIZE,
    get_tags_for_post_ids,
    list_published_posts,
    list_published_posts_for_site,
    post_to_public_dict,
)
from app.templates import render_index_page, render_post_page

RENDERER_VERSION = "agentcms-export-v1"
MANIFEST_SCHEMA = "agentcms-export/v1"
FEED_LIMIT = 20

_ARCHIVE_MODE = 0o644
_DIR_MODE = 0o755


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ExportError(DomainError):
    status_code = 500
    code = "export-failed"
    title = "Static export failed"


class ExportSiteNotFoundError(ExportError):
    status_code = 404
    code = "site-not-found"
    title = "Site not found"

    def __init__(self, slug: str) -> None:
        super().__init__(
            f"No site with slug '{slug}' exists, so it cannot be exported.",
            hint=(
                "Create the site first (POST /v1/sites), or double-check the --site / site slug you passed."
            ),
            extra={"site": slug},
        )


class ExportBaseUrlRequiredError(ExportError):
    status_code = 422
    code = "export-base-url-required"
    title = "A public base URL is required to export"

    def __init__(self, site_slug: str) -> None:
        super().__init__(
            f"Site '{site_slug}' has no configured base_url, so exported links cannot be resolved.",
            hint=(
                "Set `site.base_url` on the site, or pass --base-url on the command line. "
                "Private/loopback hosts are rejected because they would leak into the export."
            ),
            extra={"site": site_slug},
        )


class ExportBaseUrlPrivateError(ExportError):
    status_code = 422
    code = "export-base-url-private"
    title = "The base URL points at a private host"

    def __init__(self, base_url: str) -> None:
        super().__init__(
            f"Base URL '{base_url}' resolves to a loopback or private address, "
            "which would be embedded in the exported files.",
            hint=(
                "Use a public hostname, e.g. https://blog.example.com or "
                "https://you.github.io. Localhost, 127.x, 10.x, 192.168.x, etc. are rejected."
            ),
            extra={"base_url": base_url},
        )


# ---------------------------------------------------------------------------
# URL safety
# ---------------------------------------------------------------------------


def is_private_host(hostname: str) -> bool:
    """True when a hostname resolves to loopback / private / link-local space."""
    host = hostname.strip().lower().rstrip(".")
    if not host:
        return True
    if host == "localhost" or host.endswith(".localhost"):
        return True
    if host.endswith(".local"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_unspecified
        or addr.is_multicast
        or addr.is_reserved
    )


def is_private_base_url(base_url: str) -> bool:
    """True when the base URL's host is not a public hostname."""
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        return True
    return is_private_host(parsed.hostname or "")


def resolve_base_url(site: Site, override: str | None) -> str:
    """Return the normalized public base URL for a site, or raise."""
    raw = (override or site.base_url or "").strip()
    if not raw:
        raise ExportBaseUrlRequiredError(site.slug)
    if is_private_base_url(raw):
        raise ExportBaseUrlPrivateError(raw)
    return raw.rstrip("/")


# ---------------------------------------------------------------------------
# Deterministic serialization helpers
# ---------------------------------------------------------------------------


def format_iso(dt: datetime) -> str:
    """Render a datetime in UTC ISO-8601 with a trailing Z (no microseconds)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def format_rfc822(dt: datetime) -> str:
    """Render a datetime in RFC 822 GMT form (as feeds expect)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> str:
    """Compact, sorted-key JSON with non-ASCII preserved (deterministic)."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Feed / sitemap builders (same structures as the public API, timestamped)
# ---------------------------------------------------------------------------


def _etree() -> Any:
    import xml.etree.ElementTree as ET

    return ET


def build_rss(
    post_dicts: list[dict[str, Any]],
    site: Site,
    site_slug: str,
    base_url: str,
    generated_at: datetime,
) -> str:
    ET = _etree()

    rss = ET.Element("rss")
    rss.set("version", "2.0")
    rss.set("xmlns:atom", "http://www.w3.org/2005/Atom")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = site.name
    ET.SubElement(channel, "link").text = f"{base_url}/{site_slug}"
    ET.SubElement(channel, "description").text = f"Posts from {site.name}"
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "lastBuildDate").text = format_rfc822(generated_at)

    atom_link = ET.SubElement(channel, "atom:link")
    atom_link.set("href", f"{base_url}/{site_slug}/rss.xml")
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")

    for p in post_dicts[:FEED_LIMIT]:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = p["title"]
        ET.SubElement(item, "link").text = f"{base_url}/{site_slug}/{p['slug']}"
        ET.SubElement(item, "guid").text = f"{base_url}/{site_slug}/{p['slug']}"
        if p.get("excerpt"):
            ET.SubElement(item, "description").text = p["excerpt"]
        if p.get("published_at"):
            published = datetime.fromisoformat(p["published_at"])
            ET.SubElement(item, "pubDate").text = format_rfc822(published)

    return _pretty_xml(rss)


def build_atom(
    post_dicts: list[dict[str, Any]],
    site: Site,
    site_slug: str,
    base_url: str,
    generated_at: datetime,
) -> str:
    ET = _etree()

    feed = ET.Element("feed")
    feed.set("xmlns", "http://www.w3.org/2005/Atom")
    ET.SubElement(feed, "title").text = site.name
    ET.SubElement(feed, "link").set("href", f"{base_url}/{site_slug}")
    ET.SubElement(feed, "id").text = f"{base_url}/{site_slug}"
    ET.SubElement(feed, "updated").text = format_iso(generated_at)

    for p in post_dicts[:FEED_LIMIT]:
        entry = ET.SubElement(feed, "entry")
        ET.SubElement(entry, "title").text = p["title"]
        ET.SubElement(entry, "link").set("href", f"{base_url}/{site_slug}/{p['slug']}")
        ET.SubElement(entry, "id").text = f"{base_url}/{site_slug}/{p['slug']}"
        if p.get("published_at"):
            ET.SubElement(entry, "published").text = p["published_at"]
        updated = p.get("updated_at") or format_iso(generated_at)
        ET.SubElement(entry, "updated").text = updated
        if p.get("excerpt"):
            summary = ET.SubElement(entry, "summary")
            summary.text = p["excerpt"]

    return _pretty_xml(feed)


def build_feed_json(
    post_dicts: list[dict[str, Any]], site: Site, site_slug: str, base_url: str, renderer: MarkdownRenderer
) -> str:
    items: list[dict[str, Any]] = []
    for p in post_dicts[:FEED_LIMIT]:
        item: dict[str, Any] = {
            "id": f"{base_url}/{site_slug}/{p['slug']}",
            "slug": p["slug"],
            "title": p["title"],
            "url": f"{base_url}/{site_slug}/{p['slug']}",
        }
        if p.get("excerpt"):
            item["summary"] = p["excerpt"]
        if p.get("body_md"):
            item["content_html"] = renderer.render_html(p["body_md"])
        if p.get("published_at"):
            item["date_published"] = p["published_at"]
        if p.get("updated_at"):
            item["date_modified"] = p["updated_at"]
        items.append(item)

    return canonical_json(
        {
            "version": "https://jsonfeed.org/version/1.1",
            "title": site.name,
            "home_page_url": f"{base_url}/{site_slug}",
            "feed_url": f"{base_url}/{site_slug}/feed.json",
            "items": items,
        }
    )


def build_sitemap(post_dicts: list[dict[str, Any]], site: Site, site_slug: str, base_url: str) -> str:
    ET = _etree()

    urlset = ET.Element("urlset")
    urlset.set("xmlns", "http://www.sitemaps.org/schemas/sitemap/0.9")
    url_el = ET.SubElement(urlset, "url")
    ET.SubElement(url_el, "loc").text = f"{base_url}/{site_slug}"
    ET.SubElement(url_el, "changefreq").text = "daily"
    ET.SubElement(url_el, "priority").text = "0.8"
    for p in post_dicts:
        post_el = ET.SubElement(urlset, "url")
        ET.SubElement(post_el, "loc").text = f"{base_url}/{site_slug}/{p['slug']}"
        if p.get("updated_at"):
            ET.SubElement(post_el, "lastmod").text = p["updated_at"][:10]
        ET.SubElement(post_el, "changefreq").text = "weekly"
        ET.SubElement(post_el, "priority").text = "0.6"
    return _pretty_xml(urlset)


def _pretty_xml(element: Any) -> str:
    from xml.dom import minidom

    xml_str = minidom.parseString(ET_to_string(element)).toprettyxml(indent="  ")
    lines = xml_str.split("\n")
    if lines and lines[0].startswith("<?xml"):
        xml_str = "\n".join(lines[1:])
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_str


def ET_to_string(element: Any) -> str:
    import xml.etree.ElementTree as ET

    return ET.tostring(element, encoding="unicode")


def build_robots(base_url: str) -> str:
    return (
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


def build_404(site: Site, site_slug: str, base_url: str) -> str:
    home = f"{base_url}/{site_slug}"
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        "<title>Not found</title>\n"
        "</head>\n"
        "<body>\n"
        "<h1>Page not found</h1>\n"
        "<p>The page you were looking for does not exist.</p>\n"
        f'<p><a href="{home}">Back to {home}</a></p>\n'
        "</body>\n"
        "</html>\n"
    )


# ---------------------------------------------------------------------------
# Render hashing (what feeds incremental reuse decisions)
# ---------------------------------------------------------------------------


def compute_render_hash(site: Site, base_url: str, post: Post, tags: list[str]) -> str:
    """Hash everything that affects a post's rendered output."""
    payload = {
        "renderer": RENDERER_VERSION,
        "site_slug": site.slug,
        "site_name": site.name,
        "base_url": base_url,
        "slug": post.slug,
        "title": post.title,
        "excerpt": post.excerpt or "",
        "body_md": post.body_md,
        "tags": sorted(tags),
        "published_at": post.published_at.isoformat() if post.published_at else None,
        "updated_at": post.updated_at.isoformat() if post.updated_at else None,
        "content_hash": post.content_hash,
    }
    return sha256_bytes(canonical_json(payload).encode())


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportResult:
    site_slug: str
    out_dir: Path
    generated_at: datetime
    post_count: int
    file_count: int
    reused_files: int
    generated_files: int
    pruned_files: int
    elapsed_seconds: float
    verify_ok: bool
    verify_issues: list[str]
    manifest: dict[str, Any]


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    issues: list[str]
    checked_files: int


# ---------------------------------------------------------------------------
# Export engine
# ---------------------------------------------------------------------------


def export_site(
    session: Session,
    site_slug: str,
    out_dir: Path,
    *,
    incremental: bool = False,
    since: datetime | None = None,
    base_url: str | None = None,
    generated_at: datetime | None = None,
    media_root: Path | None = None,
    renderer: MarkdownRenderer | None = None,
    delta_only: bool = False,
) -> ExportResult:
    """Render the static tree for one site into ``out_dir``.

    ``incremental`` reuses files whose ``render_hash`` matches the previous
    ``manifest.json``.  ``since`` limits regenerated post files to those whose
    ``updated_at`` is inside the delta window; remaining files are reused from
    the previous manifest when present.

    ``delta_only`` (used by the HTTP endpoint when ``since`` is supplied) omits
    the site-level pages/feeds and returns only the changed post files plus a
    manifest, so a pull-based host can merge them into an existing export.
    """
    started = time.perf_counter()
    renderer = renderer or MarkdownRenderer()
    site = session.query(Site).filter(Site.slug == site_slug).first()
    if site is None:
        raise ExportSiteNotFoundError(site_slug)

    base_url = resolve_base_url(site, base_url)

    posts = list_published_posts_for_site(session, site.id)
    tags_by_post = get_tags_for_post_ids(session, [p.id for p in posts])
    tags_by_post = {pid: sorted(slugs) for pid, slugs in tags_by_post.items()}

    if generated_at is None:
        generated_at = _derive_generated_at(posts, site)

    prev_manifest = read_manifest(out_dir) if (incremental or since is not None) else None

    allow_reuse = prev_manifest is not None
    since_dt = _normalize_utc(since) if since is not None else None

    files: dict[str, bytes] = {}
    reused: set[str] = set()
    content_records: list[dict[str, Any]] = []
    post_count = 0

    for post in posts:
        tags = tags_by_post.get(post.id, [])
        render_hash = compute_render_hash(site, base_url, post, tags)
        files_paths = _post_file_paths(post.slug)
        updated = _normalize_utc(post.updated_at) if post.updated_at else None
        in_window = since_dt is None or (updated is not None and updated >= since_dt)

        if delta_only and not in_window:
            continue

        stored = None
        if allow_reuse:
            assert prev_manifest is not None
            stored = prev_manifest.get("content", {}).get(post.slug)

        reuse = False
        if isinstance(stored, dict) and all((out_dir / f).is_file() for f in files_paths):
            if since_dt is not None and not in_window:
                # Outside the since window: unchanged by definition, reuse the
                # file bytes even if hashes drifted (they should not have).
                reuse = True
            else:
                # Incremental full run: reuse only when nothing changed.
                reuse = stored.get("render_hash") == render_hash

        if reuse:
            for f in files_paths:
                files[f] = (out_dir / f).read_bytes()
                reused.add(f)
            content_records.append(_content_record(post, render_hash, files_paths))
            post_count += 1
            continue

        files.update(_render_post_files(post, site_slug, site.name, base_url, tags))
        content_records.append(_content_record(post, render_hash, files_paths))
        post_count += 1

    if not delta_only:
        for path, data in _render_site_files(
            session, site_slug, site, base_url, generated_at, renderer
        ).items():
            files[path] = data
        media_files = _collect_media(session, site.id, media_root)
        for path, data in media_files.items():
            files[path] = data

    files[".nojekyll"] = b""
    manifest = _build_manifest(
        site=site,
        base_url=base_url,
        generated_at=generated_at,
        post_count=post_count,
        content_records=content_records,
        files=files,
        since=since_dt,
    )
    files["manifest.json"] = canonical_json(manifest).encode() + b"\n"

    pruned = _write_tree(out_dir, files, prev_manifest=prev_manifest)
    verify = verify_export(out_dir)

    return ExportResult(
        site_slug=site_slug,
        out_dir=out_dir,
        generated_at=generated_at,
        post_count=post_count,
        file_count=len(files),
        reused_files=len(reused),
        generated_files=len(files) - len(reused),
        pruned_files=pruned,
        elapsed_seconds=time.perf_counter() - started,
        verify_ok=verify.ok,
        verify_issues=verify.issues,
        manifest=manifest,
    )


def _normalize_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _derive_generated_at(posts: list[Post], site: Site) -> datetime:
    """Feed/lastBuildDate anchor: newest post timestamp, else site timestamp."""
    anchors = [_normalize_utc(p.updated_at) for p in posts if p.updated_at]
    if anchors:
        return max(anchors)
    if site.updated_at:
        return _normalize_utc(site.updated_at)
    return datetime(1970, 1, 1, tzinfo=UTC)


def _content_record(post: Post, render_hash: str, files_paths: list[str]) -> dict[str, Any]:
    return {
        "slug": post.slug,
        "render_hash": render_hash,
        "content_hash": post.content_hash,
        "published_at": format_iso(post.published_at) if post.published_at else None,
        "updated_at": format_iso(post.updated_at) if post.updated_at else None,
        "files": files_paths,
    }


def _post_file_paths(slug: str) -> list[str]:
    return [f"posts/{slug}/index.html", f"posts/{slug}/index.md", f"posts/{slug}/index.json"]


def _post_to_public(post: Post, site_slug: str, tags: list[str]) -> dict[str, Any]:
    return post_to_public_dict(post, site_slug, tags=tags)


def _render_post_files(
    post: Post, site_slug: str, site_name: str, base_url: str, tags: list[str]
) -> dict[str, bytes]:
    renderer = MarkdownRenderer()
    body_html = renderer.render_html(post.body_md)
    # HTML must match what the public API serves: post_to_public_dict defaults
    # to tags=[] so the served page never renders a tag list.
    html_dict = _post_to_public(post, site_slug, [])
    html = render_post_page(
        post=html_dict,
        body_html=body_html,
        site_name=site_name,
        site_slug=site_slug,
        base_url=base_url,
    )
    json_dict = _post_to_public(post, site_slug, tags)
    return {
        f"posts/{post.slug}/index.html": html.encode(),
        f"posts/{post.slug}/index.md": post.body_md.encode(),
        f"posts/{post.slug}/index.json": canonical_json(json_dict).encode(),
    }


def _render_site_files(
    session: Session,
    site_slug: str,
    site: Site,
    base_url: str,
    generated_at: datetime,
    renderer: MarkdownRenderer,
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}

    posts, _total, total_pages = list_published_posts(session, site_slug, page=1, page_size=PAGE_SIZE)
    if not posts:
        total_pages = 1
    post_dicts = [_post_to_public(p, site_slug, []) for p in posts]
    files["index.html"] = render_index_page(
        posts=post_dicts,
        site_name=site.name,
        site_slug=site_slug,
        base_url=base_url,
        page=1,
        total_pages=max(1, total_pages),
    ).encode()
    for pg in range(2, max(1, total_pages) + 1):
        pg_posts, _t, pg_total = list_published_posts(session, site_slug, page=pg, page_size=PAGE_SIZE)
        pg_dicts = [_post_to_public(p, site_slug, []) for p in pg_posts]
        files[f"page/{pg}/index.html"] = render_index_page(
            posts=pg_dicts,
            site_name=site.name,
            site_slug=site_slug,
            base_url=base_url,
            page=pg,
            total_pages=max(1, pg_total),
        ).encode()

    for tag in _site_tags(session, site.id):
        tag_posts, _t, tag_total = list_published_posts(
            session, site_slug, page=1, tag=tag, page_size=PAGE_SIZE
        )
        tag_dicts = [_post_to_public(p, site_slug, []) for p in tag_posts]
        files[f"tags/{tag}/index.html"] = render_index_page(
            posts=tag_dicts,
            site_name=site.name,
            site_slug=site_slug,
            base_url=base_url,
            page=1,
            total_pages=max(1, tag_total),
            tag=tag,
        ).encode()
        for pg in range(2, max(1, tag_total) + 1):
            pg_posts, _t2, _pg_total = list_published_posts(
                session, site_slug, page=pg, tag=tag, page_size=PAGE_SIZE
            )
            pg_dicts = [_post_to_public(p, site_slug, []) for p in pg_posts]
            files[f"tags/{tag}/page/{pg}/index.html"] = render_index_page(
                posts=pg_dicts,
                site_name=site.name,
                site_slug=site_slug,
                base_url=base_url,
                page=pg,
                total_pages=max(1, tag_total),
                tag=tag,
            ).encode()

    all_post_dicts = [
        _post_to_public(p, site_slug, []) for p in list_published_posts_for_site(session, site.id)
    ]
    files["rss.xml"] = build_rss(all_post_dicts, site, site_slug, base_url, generated_at).encode()
    files["atom.xml"] = build_atom(all_post_dicts, site, site_slug, base_url, generated_at).encode()
    files["feed.json"] = build_feed_json(all_post_dicts, site, site_slug, base_url, renderer).encode()
    files["sitemap.xml"] = build_sitemap(all_post_dicts, site, site_slug, base_url).encode()
    files["robots.txt"] = build_robots(base_url).encode()
    files["404.html"] = build_404(site, site_slug, base_url).encode()
    return files


def _site_tags(session: Session, site_id: Any) -> list[str]:
    rows = (
        session.query(Tag.slug)
        .join(PostTag, PostTag.tag_id == Tag.id)
        .join(Post, Post.id == PostTag.post_id)
        .filter(Post.site_id == site_id, Post.status == "published", Post.deleted_at.is_(None))
        .distinct()
        .order_by(Tag.slug)
        .all()
    )
    return [r[0] for r in rows]


def _collect_media(session: Session, site_id: Any, media_root: Path | None) -> dict[str, bytes]:
    if media_root is None:
        return {}
    media_root = media_root.resolve()
    assets = (
        session.query(Asset)
        .filter(Asset.site_id == site_id, Asset.deleted_at.is_(None), Asset.status == "ready")
        .order_by(Asset.sha256, Asset.filename)
        .all()
    )
    files: dict[str, bytes] = {}
    for asset in assets:
        if not asset.sha256:
            continue
        rel = make_storage_key(asset.sha256, asset.filename)
        src = media_root / rel
        if src.is_file():
            files[rel] = src.read_bytes()
    return dict(sorted(files.items()))


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def _build_manifest(
    *,
    site: Site,
    base_url: str,
    generated_at: datetime,
    post_count: int,
    content_records: list[dict[str, Any]],
    files: dict[str, bytes],
    since: datetime | None,
) -> dict[str, Any]:
    return {
        "schema": MANIFEST_SCHEMA,
        "renderer_version": RENDERER_VERSION,
        "generated_at": format_iso(generated_at),
        "site": {
            "slug": site.slug,
            "name": site.name,
            "base_url": base_url,
        },
        "stats": {
            "posts": post_count,
            "files": len(files),
        },
        "since": format_iso(since) if since is not None else None,
        "content": {c["slug"]: c for c in content_records},
        "files": {path: sha256_bytes(data) for path, data in sorted(files.items())},
    }


def read_manifest(out_dir: Path) -> dict[str, Any] | None:
    """Load manifest.json from an export dir, or None when absent/invalid."""
    path = out_dir / "manifest.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return None
    return data


# ---------------------------------------------------------------------------
# Write tree
# ---------------------------------------------------------------------------


def _write_tree(out_dir: Path, files: dict[str, bytes], *, prev_manifest: dict[str, Any] | None) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    new_paths = {str(out_dir / p): p for p in files}

    pruned = 0
    if prev_manifest is not None:
        entries = prev_manifest.get("files", {})
        skip = {"manifest.json"}
        for rel in entries:
            if rel in skip or rel in new_paths:
                continue
            target = out_dir / rel
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
                    _prune_empty_parents(target.parent)
                pruned += 1

    for rel, data in files.items():
        target = out_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return pruned


def _prune_empty_parents(directory: Path) -> None:
    try:
        while directory != directory.parent:
            directory.rmdir()
            directory = directory.parent
    except OSError:
        return


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def verify_export(out_dir: Path) -> VerifyResult:
    """Recompute file hashes from manifest.json and report drift."""
    manifest = read_manifest(out_dir)
    issues: list[str] = []
    if manifest is None:
        return VerifyResult(ok=False, issues=["manifest.json missing or invalid."], checked_files=0)

    expected_files = {str(p) for p in manifest.get("files", {})}
    checked = 0
    for rel in sorted(expected_files):
        target = out_dir / rel
        if not target.is_file():
            issues.append(f"missing: {rel}")
            continue
        data = target.read_bytes()
        actual = sha256_bytes(data)
        expected = manifest["files"][rel]
        if actual != expected:
            issues.append(f"corrupted: {rel}")
        checked += 1

    if not issues:
        issues_sorted: list[str] = []
    else:
        issues_sorted = sorted(set(issues))
    return VerifyResult(ok=len(issues_sorted) == 0, issues=issues_sorted, checked_files=checked)


# ---------------------------------------------------------------------------
# Archives (deterministic tar.gz / zip)
# ---------------------------------------------------------------------------


def render_archive(files: dict[str, bytes], fmt: str) -> bytes:
    """Build a deterministic archive from a ``{path: bytes}`` map."""
    if fmt == "tar.gz":
        return _render_tar_gz(files)
    if fmt == "zip":
        return _render_zip(files)
    msg = f"Unsupported archive format: {fmt}"
    raise ExportError(msg, hint="Pick 'tar.gz' or 'zip'.")


def _render_tar_gz(files: dict[str, bytes]) -> bytes:
    import io

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for rel in sorted(files):
            info = tarfile.TarInfo(name=rel)
            data = files[rel]
            info.size = len(data)
            info.mtime = 0
            info.mode = _ARCHIVE_MODE
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(buf.getvalue(), mtime=0)


def _render_zip(files: dict[str, bytes]) -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(files):
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (_ARCHIVE_MODE & 0xFFFF) << 16
            zf.writestr(info, files[rel])
    return buf.getvalue()


def archive_bytes_for_export(out_dir: Path, fmt: str) -> bytes:
    """Read an already-written export dir back as a deterministic archive."""
    files: dict[str, bytes] = {}
    manifest = read_manifest(out_dir)
    for rel in sorted(manifest.get("files", {})) if manifest else []:
        src = out_dir / rel
        if src.is_file():
            files[rel] = src.read_bytes()
    manifest_path = out_dir / "manifest.json"
    if manifest_path.is_file():
        files["manifest.json"] = manifest_path.read_bytes()
    return render_archive(files, fmt)


# ---------------------------------------------------------------------------
# GitHub Pages deploy helper
# ---------------------------------------------------------------------------


def validate_repo(repo: str) -> tuple[str, str]:
    """Split ``owner/repo``; raise ExportError on malformed input."""
    parts = repo.strip().strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise ExportError(
            f"'{repo}' is not an owner/repo GitHub repository.",
            hint="Pass --repo=owner/repo, e.g. --repo=alice/blog.",
            extra={"repo": repo},
        )
    return parts[0], parts[1]


def plan_github_pages_deploy(out_dir: Path, repo: str, branch: str = "gh-pages") -> list[str]:
    """Return the shell commands that publish an export to GitHub Pages."""
    validate_repo(repo)
    if not (out_dir / "manifest.json").is_file():
        raise ExportError(
            f"{out_dir} has no manifest.json — run an export into it first.",
            hint="Run `agentcms export --site=<slug> --out=<out>` before deploying.",
            extra={"out_dir": str(out_dir)},
        )
    tmp = f"{out_dir}.gh-pages-worktree"
    return [
        f"rm -rf {tmp}",
        f"mkdir -p {tmp}",
        f"cp -R {out_dir}/. {tmp}/",
        f"cd {tmp} && git init -b {branch}",
        f"cd {tmp} && git add -A",
        f'cd {tmp} && git commit -m "agentcms: static export for {repo} [{branch}]" --allow-empty',
        f"cd {tmp} && git remote add origin https://github.com/{repo}.git",
        f"cd {tmp} && git push -f origin {branch}",
    ]
