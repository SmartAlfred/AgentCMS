"""HTML templates for the public read surface (ticket #8).

Semantic HTML with ~5 KB inline CSS, OG/Twitter meta, canonical URL,
``<link rel="alternate" type="text/markdown">``, and JSON-LD ``Article``.

The CSS is intentionally boring: readable, accessible, no animations.
"""

from __future__ import annotations

import html
import json
from typing import Any

# ---------------------------------------------------------------------------
# Inline CSS (~2 KB minified)
# ---------------------------------------------------------------------------

INLINE_CSS = """
:root{
  --fg:#1a1a2e;--bg:#fdfdfe;--muted:#6b7280;
  --accent:#2563eb;--border:#e5e7eb;--card:#fff;--radius:6px
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{font-size:16px;-webkit-text-size-adjust:100%}
body{
  font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  color:var(--fg);background:var(--bg);line-height:1.65;
  max-width:42rem;margin:0 auto;padding:1.5rem
}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
h1,h2,h3,h4,h5,h6{line-height:1.25;margin:1.5rem 0 .75rem;font-weight:600}
h1{font-size:1.875rem}h2{font-size:1.5rem}h3{font-size:1.25rem}
p{margin:0 0 1rem}
ul,ol{margin:0 0 1rem;padding-left:1.5rem}
li{margin-bottom:.25rem}
pre{
  background:#f3f4f6;padding:1rem;border-radius:var(--radius);
  overflow-x:auto;margin:0 0 1rem;font-size:.875rem;line-height:1.5
}
code{
  font-family:"JetBrains Mono",Fira Code,monospace;font-size:.875em;
  background:#f3f4f6;padding:.125rem .25rem;border-radius:3px
}
pre code{background:none;padding:0}
blockquote{
  border-left:3px solid var(--accent);padding:.5rem 1rem;
  margin:0 0 1rem;color:var(--muted);font-style:italic
}
table{border-collapse:collapse;width:100%;margin:0 0 1rem}
th,td{border:1px solid var(--border);padding:.5rem .75rem;text-align:left}
th{background:#f9fafb;font-weight:600}
hr{border:none;border-top:1px solid var(--border);margin:2rem 0}
img{max-width:100%;height:auto;border-radius:var(--radius)}
.sr-only{
  position:absolute;width:1px;height:1px;padding:0;margin:-1px;
  overflow:hidden;clip:rect(0,0,0,0);border:0
}
.meta{color:var(--muted);font-size:.875rem;margin-bottom:1.5rem}
.meta time{display:inline}
.tags{
  list-style:none;padding:0;display:flex;flex-wrap:wrap;
  gap:.5rem;margin:0 0 1rem
}
.tags li{
  background:#f3f4f6;padding:.25rem .75rem;
  border-radius:var(--radius);font-size:.8125rem
}
.tags a{color:var(--fg)}
.pagination{
  display:flex;justify-content:center;gap:1rem;margin:2rem 0
}
.pagination a,.pagination span{
  padding:.5rem 1rem;border-radius:var(--radius)
}
.pagination .current{background:var(--accent);color:#fff}
.site-header{
  border-bottom:1px solid var(--border);
  padding-bottom:1rem;margin-bottom:2rem
}
.site-header h1{margin:0 0 .25rem}
.site-header p{margin:0;color:var(--muted);font-size:.875rem}
.site-footer{
  border-top:1px solid var(--border);padding-top:1rem;margin-top:2rem;
  color:var(--muted);font-size:.8125rem;text-align:center
}
.preview-banner{
  background:#fef3c7;border:1px solid #f59e0b;padding:.5rem 1rem;
  border-radius:var(--radius);margin-bottom:1rem;
  font-size:.875rem;color:#92400e
}
.post-list{list-style:none;padding:0}
.post-list li{padding:1rem 0;border-bottom:1px solid var(--border)}
.post-list li:last-child{border-bottom:none}
.post-list .post-title{font-size:1.25rem;font-weight:600;margin-bottom:.25rem}
.post-list .post-excerpt{color:var(--muted);font-size:.875rem;margin-bottom:.5rem}
.post-list .post-meta{font-size:.8125rem;color:var(--muted)}
"""

# ---------------------------------------------------------------------------
# HTML page wrapper
# ---------------------------------------------------------------------------


def render_page(
    *,
    title: str,
    site_name: str,
    site_slug: str,
    canonical_url: str,
    description: str = "",
    body_html: str = "",
    post: dict[str, Any] | None = None,
    posts: list[dict[str, Any]] | None = None,
    page: int = 1,
    total_pages: int = 1,
    tag: str | None = None,
    is_preview: bool = False,
    alternate_markdown_url: str | None = None,
    extra_head: str = "",
) -> str:
    """Render a full HTML page."""
    meta_tags = _og_tags(title, description, canonical_url, post)
    json_ld = _json_ld(post, canonical_url, site_name) if post else ""

    preview_banner = ""
    if is_preview:
        preview_banner = (
            '<div class="preview-banner" role="alert">'
            "Preview mode — this draft is not publicly indexed.</div>"
        )

    alternate_link = ""
    if alternate_markdown_url:
        escaped_url = _esc(alternate_markdown_url)
        alternate_link = f'<link rel="alternate" type="text/markdown" href="{escaped_url}" />'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{_esc(title)}</title>
<meta name="description" content="{_esc(description)}" />
{meta_tags}
<link rel="canonical" href="{_esc(canonical_url)}" />
{alternate_link}
<style>{INLINE_CSS}</style>
{json_ld}
{extra_head}
</head>
<body>
<header class="site-header">
<h1><a href="/{_esc(site_slug)}">{_esc(site_name)}</a></h1>
<p>Published posts</p>
</header>
<main>
{preview_banner}
{body_html}
</main>
<footer class="site-footer">
<p>Powered by AgentCMS</p>
</footer>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Post page
# ---------------------------------------------------------------------------


def render_post_page(
    *,
    post: dict[str, Any],
    body_html: str,
    site_name: str,
    site_slug: str,
    base_url: str,
    is_preview: bool = False,
) -> str:
    """Render a single post page."""
    slug = post["slug"]
    title = post["title"]
    description = post.get("excerpt", "") or title
    canonical_url = f"{base_url.rstrip('/')}/{site_slug}/{slug}"
    alternate_md_url = f"{base_url.rstrip('/')}/{site_slug}/posts/{slug}.md"

    tags_html = ""
    if post.get("tags"):
        items = "".join(
            f'<li><a href="/{_esc(site_slug)}/tags/{_esc(t)}">{_esc(t)}</a></li>' for t in post["tags"]
        )
        tags_html = f'<ul class="tags">{items}</ul>'

    meta_parts: list[str] = []
    if post.get("published_at"):
        pub = post["published_at"]
        meta_parts.append(f'<time datetime="{_esc(pub)}">{_esc(pub[:10])}</time>')
    if post.get("reading_time_minutes"):
        meta_parts.append(f"{post['reading_time_minutes']} min read")
    meta_str = " · ".join(meta_parts)

    body_content = f"""
<article>
<h1>{_esc(title)}</h1>
{tags_html}
<div class="meta">{meta_str}</div>
{body_html}
</article>"""

    return render_page(
        title=f"{title} — {site_name}",
        site_name=site_name,
        site_slug=site_slug,
        canonical_url=canonical_url,
        description=description,
        body_html=body_content,
        post=post,
        is_preview=is_preview,
        alternate_markdown_url=alternate_md_url,
    )


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------


def render_index_page(
    *,
    posts: list[dict[str, Any]],
    site_name: str,
    site_slug: str,
    base_url: str,
    page: int = 1,
    total_pages: int = 1,
    tag: str | None = None,
) -> str:
    """Render the site index page."""
    canonical_url = f"{base_url.rstrip('/')}/{site_slug}"
    if tag:
        canonical_url += f"/tags/{tag}"

    items = []
    for p in posts:
        tag_links = ""
        if p.get("tags"):
            tag_links = " · ".join(
                f'<a href="/{_esc(site_slug)}/tags/{_esc(t)}">{_esc(t)}</a>' for t in p["tags"]
            )
        date_str = p.get("published_at", "")[:10] if p.get("published_at") else ""
        slug_esc = _esc(p["slug"])
        title_esc = _esc(p["title"])
        excerpt_esc = _esc(p.get("excerpt", ""))
        link = f"/{_esc(site_slug)}/{slug_esc}"
        tag_section = f" · {tag_links}" if tag_links else ""
        items.append(
            f"<li>"
            f'<div class="post-title"><a href="{link}">{title_esc}</a></div>'
            f'<div class="post-excerpt">{excerpt_esc}</div>'
            f'<div class="post-meta">{date_str}{tag_section}</div>'
            f"</li>"
        )
    if items:
        joined = "".join(items)
        post_list_html = f'<ul class="post-list">{joined}</ul>'
    else:
        post_list_html = "<p>No published posts yet.</p>"

    pagination = ""
    if total_pages > 1:
        pages: list[str] = []
        if page > 1:
            params = f"page={page - 1}"
            if tag:
                params += f"&tag={tag}"
            pages.append(f'<a href="/{_esc(site_slug)}?{params}">Previous</a>')
        pages.append(f'<span class="current">{page}</span>')
        if page < total_pages:
            params = f"page={page + 1}"
            if tag:
                params += f"&tag={tag}"
            pages.append(f'<a href="/{_esc(site_slug)}?{params}">Next</a>')
        joined_pages = "".join(pages)
        pagination = f'<nav class="pagination" aria-label="Pagination">{joined_pages}</nav>'

    heading = f"Posts tagged \u00ab{tag}\u00bb" if tag else "All posts"
    body_html = f"<h2>{heading}</h2>\n{post_list_html}\n{pagination}"

    page_title = f"{site_name} — {heading}" if tag else site_name
    return render_page(
        title=page_title,
        site_name=site_name,
        site_slug=site_slug,
        canonical_url=canonical_url,
        description=f"Published posts on {site_name}",
        body_html=body_html,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _esc(text: str) -> str:
    """Escape HTML special characters."""
    return html.escape(str(text), quote=True)


def _og_tags(title: str, description: str, url: str, post: dict[str, Any] | None = None) -> str:
    """Generate OG and Twitter meta tags."""
    tags = [
        f'<meta property="og:title" content="{_esc(title)}" />',
        f'<meta property="og:description" content="{_esc(description)}" />',
        f'<meta property="og:url" content="{_esc(url)}" />',
        '<meta property="og:type" content="article" />',
        '<meta name="twitter:card" content="summary" />',
        f'<meta name="twitter:title" content="{_esc(title)}" />',
        f'<meta name="twitter:description" content="{_esc(description)}" />',
    ]
    if post and post.get("published_at"):
        tags.append(f'<meta property="article:published_time" content="{_esc(post["published_at"])}" />')
    if post and post.get("updated_at"):
        tags.append(f'<meta property="article:modified_time" content="{_esc(post["updated_at"])}" />')
    return "\n".join(tags)


def _json_ld(post: dict[str, Any], url: str, site_name: str) -> str:
    """Generate JSON-LD Article structured data."""
    data: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": post.get("title", ""),
        "url": url,
    }
    if post.get("excerpt"):
        data["description"] = post["excerpt"]
    if post.get("published_at"):
        data["datePublished"] = post["published_at"]
    if post.get("updated_at"):
        data["dateModified"] = post["updated_at"]
    if post.get("author_label"):
        data["author"] = {"@type": "Person", "name": post["author_label"]}
    data["publisher"] = {"@type": "Organization", "name": site_name}
    if post.get("word_count"):
        data["wordCount"] = post["word_count"]
    if post.get("reading_time_minutes"):
        data["timeRequired"] = f"PT{post['reading_time_minutes']}M"
    return f'<script type="application/ld+json">{json.dumps(data, ensure_ascii=False)}</script>'
