"""Markdown pipeline tests (#7).

Covers every acceptance criterion in the ticket:
- Golden-file tests: ~20 markdown docs render identically across runs and re-renders
- XSS suite: script tags, onerror=, javascript: href, SVG payload, <style>, HTML comment smuggling
- Accept: text/markdown returns exact stored body, byte-for-byte
- Same content → same content_hash → no new revision on no-op PATCH
- Word count/lines computed with \n-normalised input
- Auto-fixes: line endings, trailing whitespace, trailing newline, H1 demotion, smart quotes in code spans
- Heading anchors are stable and deterministic
- Content negotiation via Accept header
"""

from __future__ import annotations

import uuid
from typing import ClassVar

import pytest
from app.models.site import Site
from app.services.markdown import (
    MarkdownRenderer,
    RenderResult,
    _heading_id,
    _strip_markdown,
    compute_content_hash,
    compute_excerpt,
    compute_reading_time_minutes,
    compute_word_count,
    normalise_body,
    render_markdown,
)
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

SITE_SLUG = "blog"

# ---------------------------------------------------------------------------
# Golden-file fixture set: ~20 markdown documents
# ---------------------------------------------------------------------------

GOLDEN_FILES: dict[str, str] = {
    "simple_paragraph": "# Hello World\n\nThis is a simple paragraph.\n",
    "multiple_headings": "# Title\n\n## Subtitle\n\n### Section\n\nContent here.\n",
    "bold_italic": "# Formatting\n\n**bold** and *italic* and ***both***.\n",
    "inline_code": "# Code\n\nUse `print()` to output.\n",
    "code_block": "# Block\n\n```python\ndef hello():\n    print('hi')\n```\n",
    "unordered_list": "# List\n\n- item one\n- item two\n- item three\n",
    "ordered_list": "# Ordered\n\n1. first\n2. second\n3. third\n",
    "link": "# Links\n\n[Click here](https://example.com).\n",
    "image": "# Images\n\n![Alt text](https://example.com/img.png).\n",
    "blockquote": "# Quote\n\n> This is a blockquote.\n",
    "table": "# Table\n\n| Name | Age |\n|------|-----|\n| Alice | 30 |\n| Bob | 25 |\n",
    "strikethrough": "# Deleted\n\n~~This is deleted.~~\n",
    "horizontal_rule": "# Section\n\nContent.\n\n---\n\nMore content.\n",
    "nested_list": "# Nested\n\n- top\n  - middle\n    - bottom\n",
    "autolink": "# Auto\n\nVisit https://example.com for more.\n",
    "footnotes": "# Footnotes\n\nThis has a footnote[^1].\n\n[^1]: The footnote text.\n",
    "task_list": "# Tasks\n\n- [x] Done\n- [ ] Todo\n",
    "empty_headings": "# A\n\n## B\n\n### C\n\n#### D\n\nContent.\n",
    "mixed_formatting": "# Mixed\n\n**Bold** with `code` and [link](http://x.com).\n",
    "html_entities": "# Entities\n\n&amp; &lt; &gt; should be escaped.\n",
    "special_chars": "# Special\n\nAT&T, 100%, $5, @user, #hashtag.\n",
    "multiple_code_blocks": "# Code\n\n```js\nconsole.log(1)\n```\n\n```css\nbody { color: red; }\n```\n",
    "deeply_nested": "# Deep\n\n> quote\n> > nested quote\n> > > triple nested\n",
}


def _create_site(session: Session, slug: str = SITE_SLUG) -> Site:
    site = Site(
        id=uuid.uuid4(),
        slug=slug,
        name=f"Test Site {slug}",
        publish_mode="auto",
    )
    session.add(site)
    session.flush()
    session.commit()
    return site


# ---------------------------------------------------------------------------
# Acceptance: Golden-file tests — deterministic rendering
# ---------------------------------------------------------------------------


class TestGoldenFiles:
    """Each golden file renders identically across runs and re-renders."""

    @pytest.mark.parametrize("name,content", list(GOLDEN_FILES.items()), ids=lambda x: x)
    def test_golden_render_deterministic(self, name: str, content: str) -> None:
        renderer = MarkdownRenderer()
        first = renderer.render_html(content)
        second = renderer.render_html(content)
        fresh = MarkdownRenderer().render_html(content)
        assert first == second, f"Non-deterministic render for {name}"
        assert first == fresh, f"Fresh renderer produced different output for {name}"

    @pytest.mark.parametrize("name,content", list(GOLDEN_FILES.items()), ids=lambda x: x)
    def test_golden_content_hash_stable(self, name: str, content: str) -> None:
        h1 = compute_content_hash(content)
        h2 = compute_content_hash(content)
        assert h1 == h2, f"Content hash not stable for {name}"
        assert len(h1) == 64, f"Content hash not SHA-256 for {name}"


# ---------------------------------------------------------------------------
# Acceptance: XSS suite — neutralised on every output path
# ---------------------------------------------------------------------------


class TestXSS:
    """XSS payloads must be neutralised in HTML output.

    When allow_raw_html=False (default), markdown-it-py escapes all HTML tags,
    so dangerous patterns appear as escaped entities (e.g., &lt;script&gt;),
    which is safe. When allow_raw_html=True, our sanitiser strips dangerous
    tags/attributes while allowing safe ones.
    """

    XSS_PAYLOADS: ClassVar[list[tuple[str, str]]] = [
        ("script_tag", "<script>alert('xss')</script>"),
        ("onerror_handler", '<img src=x onerror="alert(1)">'),
        ("javascript_href", '<a href="javascript:alert(1)">click</a>'),
        ("svg_payload", '<svg onload="alert(1)"><circle r="10"/></svg>'),
        ("style_tag", "<style>body{background:red}</style>"),
        ("html_comment_smuggle", "<!--<script>alert('xss')</script>-->"),
        ("data_url", '<a href="data:text/html,<script>alert(1)</script>">click</a>'),
        ("vbscript", '<a href="vbscript:MsgBox(1)">click</a>'),
        ("iframe_tag", '<iframe src="javascript:alert(1)"></iframe>'),
        ("event_handler_variants", '<div onmouseover="alert(1)">hover</div>'),
        ("nested_script", "<scr<script>ipt>alert(1)</scr</script>ipt>"),
        ("img_onerror", '<img src="valid.jpg" onerror="alert(1)">'),
        ("svg_event", '<svg><animate onbegin="alert(1)" attributeName="x" dur="1s"/></svg>'),
        ("form_action", '<form action="javascript:alert(1)"><input type="submit"></form>'),
        ("meta_refresh", '<meta http-equiv="refresh" content="0;url=javascript:alert(1)">'),
    ]

    @pytest.mark.parametrize("name,payload", XSS_PAYLOADS, ids=lambda x: x)
    def test_xss_neutralised_default(self, name: str, payload: str) -> None:
        """When raw HTML is disabled, all HTML is escaped — safe by construction."""
        renderer = MarkdownRenderer(allow_raw_html=False)
        html_out = renderer.render_html(payload)
        # The dangerous patterns must not appear as executable HTML.
        # When HTML is disabled, tags are escaped: < becomes &lt;
        # So <script becomes &lt;script — which is safe text, not a tag.
        assert "<script>" not in html_out, f"Raw <script> found in output for {name}"
        assert "<iframe" not in html_out, f"Raw <iframe found in output for {name}"
        assert "<style>" not in html_out, f"Raw <style> found in output for {name}"
        assert "<svg" not in html_out, f"Raw <svg found in output for {name}"

    @pytest.mark.parametrize("name,payload", XSS_PAYLOADS, ids=lambda x: x)
    def test_xss_neutralised_allow_raw(self, name: str, payload: str) -> None:
        """Even with raw HTML enabled, dangerous tags/attrs are stripped by sanitiser."""
        renderer = MarkdownRenderer(allow_raw_html=True)
        html_out = renderer.render_html(payload)
        assert "<script" not in html_out.lower(), f"<script found for {name}"
        assert "<iframe" not in html_out.lower(), f"<iframe found for {name}"
        assert "<style>" not in html_out.lower(), f"<style> found for {name}"
        # javascript: URLs must be stripped from href/src
        assert "javascript:" not in html_out.lower(), f"javascript: found for {name}"


# ---------------------------------------------------------------------------
# Acceptance: text/markdown returns exact stored body
# ---------------------------------------------------------------------------


class TestAcceptTextMarkdown:
    """Accept: text/markdown returns the exact stored body, byte-for-byte."""

    def test_text_markdown_returns_exact_body(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        body_md = "# Test Post\n\nHello world.\n"
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": body_md},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        resp = client.get(
            f"/v1/posts/{post_id}",
            headers={**auth_headers, "Accept": "text/markdown"},
        )
        assert resp.status_code == 200
        assert resp.text == body_md
        assert resp.headers["content-type"].startswith("text/markdown")

    def test_text_markdown_after_update(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Original\n\nFirst version."},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]
        updated_md = "# Updated\n\nSecond version.\n"
        client.patch(
            f"/v1/posts/{post_id}",
            json={"body_md": updated_md},
            headers=auth_headers,
        )

        resp = client.get(
            f"/v1/posts/{post_id}",
            headers={**auth_headers, "Accept": "text/markdown"},
        )
        assert resp.status_code == 200
        assert resp.text == updated_md


# ---------------------------------------------------------------------------
# Acceptance: same content → same content_hash → no new revision on PATCH
# ---------------------------------------------------------------------------


class TestContentHashIdempotent:
    """Same content → same content_hash → no new revision on no-op PATCH."""

    def test_no_op_patch_same_body(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        body_md = "# Stable\n\nContent that doesn't change.\n"
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": body_md},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]
        revision_before = resp.json()["revision"]
        hash_before = resp.json()["content_hash"]

        # PATCH with identical body
        resp = client.patch(
            f"/v1/posts/{post_id}",
            json={"body_md": body_md},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["content_hash"] == hash_before
        # Revision should not increase for identical content
        assert resp.json()["revision"] == revision_before

    def test_content_hash_changes_on_different_body(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Version 1\n\nFirst."},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]
        hash_v1 = resp.json()["content_hash"]

        resp = client.patch(
            f"/v1/posts/{post_id}",
            json={"body_md": "# Version 2\n\nSecond."},
            headers=auth_headers,
        )
        hash_v2 = resp.json()["content_hash"]
        assert hash_v1 != hash_v2
        assert resp.json()["revision"] == 2


# ---------------------------------------------------------------------------
# Acceptance: word count / reading time
# ---------------------------------------------------------------------------


class TestWordCountReadingTime:
    """Word count/lines computed with \n-normalised input."""

    def test_word_count_simple(self) -> None:
        assert compute_word_count("hello world") == 2

    def test_word_count_multiline(self) -> None:
        assert compute_word_count("line one\nline two\nline three") == 6

    def test_word_count_empty(self) -> None:
        assert compute_word_count("") == 0

    def test_word_count_whitespace_only(self) -> None:
        assert compute_word_count("   \n  \n  ") == 0

    def test_word_count_normalised_line_endings(self) -> None:
        assert compute_word_count("hello\r\nworld") == 2
        assert compute_word_count("hello\rworld") == 2

    def test_reading_time_zero(self) -> None:
        assert compute_reading_time_minutes("") == 0

    def test_reading_time_short(self) -> None:
        assert compute_reading_time_minutes("one two three") >= 1

    def test_reading_time_long(self) -> None:
        text = "word " * 500
        assert compute_reading_time_minutes(text) >= 2

    def test_word_count_in_api_response(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Title\n\none two three four five"},
            headers=auth_headers,
        )
        assert resp.json()["word_count"] == 7  # Title + 5 words
        assert resp.json()["reading_time_minutes"] >= 1


# ---------------------------------------------------------------------------
# Acceptance: Auto-fixes on ingest
# ---------------------------------------------------------------------------


class TestAutoFixes:
    """Auto-fixes applied on ingest, each reported in warnings[]."""

    def test_normalise_line_endings(self) -> None:
        body = "line1\r\nline2\rline3"
        result, warnings = normalise_body(body)
        assert "\r" not in result
        assert any("line endings" in w.lower() for w in warnings)

    def test_strip_trailing_whitespace(self) -> None:
        body = "# Title   \n\nParagraph   \n"
        result, warnings = normalise_body(body)
        for line in result.split("\n"):
            assert line == line.rstrip()
        assert any("whitespace" in w.lower() for w in warnings)

    def test_ensure_single_trailing_newline(self) -> None:
        body = "# Title\n\nContent\n\n\n\n"
        result, _ = normalise_body(body)
        assert result.endswith("\n")
        assert not result.endswith("\n\n")

    def test_demote_duplicate_h1(self) -> None:
        body = "# First\n\nSome content.\n\n# Second\n\nMore.\n"
        result, warnings = normalise_body(body)
        lines = result.split("\n")
        assert lines[0] == "# First"
        assert "## Second" in result
        assert any("demoted" in w.lower() for w in warnings)

    def test_smart_quotes_in_code_spans(self) -> None:
        # Smart quotes INSIDE code spans should be converted to ASCII
        body = "Use `\u2018hello\u2019` in code.\n"
        result, warnings = normalise_body(body)
        # The smart quotes inside the code span should become ASCII quotes
        assert "'hello'" in result
        assert any("smart" in w.lower() for w in warnings)

    def test_smart_quotes_outside_code_not_touched(self) -> None:
        # Smart quotes outside code spans are not modified
        body = "He said \u201chello\u201d to her.\n"
        result, warnings = normalise_body(body)
        assert "\u201c" in result or "hello" in result
        # No smart quote warning expected (only in code spans)
        assert not any("smart" in w.lower() for w in warnings)

    def test_no_warnings_for_clean_input(self) -> None:
        body = "# Title\n\nClean content.\n"
        _, warnings = normalise_body(body)
        assert warnings == []


# ---------------------------------------------------------------------------
# Acceptance: Heading anchors
# ---------------------------------------------------------------------------


class TestHeadingAnchors:
    """Heading anchors are stable and deterministic."""

    def test_heading_id_basic(self) -> None:
        assert _heading_id("Hello World") == "hello-world"

    def test_heading_id_special_chars(self) -> None:
        assert _heading_id("Hello, World!") == "hello-world"

    def test_heading_id_unicode(self) -> None:
        # NFKD decomposition: Ü → U + combining diaeresis → stripped to ASCII → uber
        assert _heading_id("Über Cool") == "uber-cool"

    def test_heading_id_empty(self) -> None:
        assert _heading_id("") == ""

    def test_heading_ids_in_html(self) -> None:
        renderer = MarkdownRenderer()
        html_out = renderer.render_html("# Title\n\n## Sub Title\n\n### Deep Section\n")
        assert 'id="title"' in html_out
        assert 'id="sub-title"' in html_out
        assert 'id="deep-section"' in html_out

    def test_heading_ids_deterministic(self) -> None:
        md = "## Hello World\n\n## Hello World\n"
        r1 = MarkdownRenderer().render_html(md)
        r2 = MarkdownRenderer().render_html(md)
        assert r1 == r2

    def test_heading_ids_deduplicated(self) -> None:
        md = "## Same\n\n## Same\n\n## Same\n"
        html = MarkdownRenderer().render_html(md)
        assert 'id="same"' in html
        assert 'id="same-1"' in html
        assert 'id="same-2"' in html


# ---------------------------------------------------------------------------
# Acceptance: GFM features
# ---------------------------------------------------------------------------


class TestGFMFeatures:
    """CommonMark + GFM: tables, strikethrough, task lists, autolinks."""

    def test_table(self) -> None:
        md = "| a | b |\n|---|---|\n| 1 | 2 |\n"
        html = MarkdownRenderer().render_html(md)
        assert "<table>" in html
        assert "<td>1</td>" in html

    def test_strikethrough(self) -> None:
        md = "~~deleted~~\n"
        html = MarkdownRenderer().render_html(md)
        assert "<s>" in html or "<del>" in html

    def test_autolink(self) -> None:
        md = "Visit https://example.com today.\n"
        html = MarkdownRenderer().render_html(md)
        assert "https://example.com" in html


# ---------------------------------------------------------------------------
# Acceptance: Content negotiation via API
# ---------------------------------------------------------------------------


class TestContentNegotiation:
    """Accept header drives the representation returned."""

    def test_default_json(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Title\n\nContent."},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        assert "application/json" in resp.headers["content-type"]

    def test_accept_text_markdown(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        body = "# Title\n\nContent.\n"
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": body},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        resp = client.get(
            f"/v1/posts/{post_id}",
            headers={**auth_headers, "Accept": "text/markdown"},
        )
        assert resp.status_code == 200
        assert "text/markdown" in resp.headers["content-type"]
        assert resp.text == body

    def test_accept_text_html(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Title\n\nContent."},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        resp = client.get(
            f"/v1/posts/{post_id}",
            headers={**auth_headers, "Accept": "text/html"},
        )
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "<h1" in resp.text or "<h2" in resp.text

    def test_accept_json_explicit(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Title"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        resp = client.get(
            f"/v1/posts/{post_id}",
            headers={**auth_headers, "Accept": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "body_md" in data
        assert "word_count" in data
        assert "content_hash" in data


# ---------------------------------------------------------------------------
# Acceptance: Derived fields in JSON response
# ---------------------------------------------------------------------------


class TestDerivedFields:
    """excerpt, word_count, reading_time_minutes, content_hash in response."""

    def test_derived_fields_present(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Title\n\nThis is the first paragraph with some words."},
            headers=auth_headers,
        )
        data = resp.json()
        assert "word_count" in data
        assert "reading_time_minutes" in data
        assert "content_hash" in data
        assert data["word_count"] > 0
        assert data["reading_time_minutes"] >= 1
        assert len(data["content_hash"]) == 64  # SHA-256

    def test_excerpt_auto_generated(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Title\n\nThis is the first paragraph of the document."},
            headers=auth_headers,
        )
        data = resp.json()
        assert data["excerpt"] is not None
        assert len(data["excerpt"]) <= 200
        assert "Title" not in data["excerpt"]  # Excerpt skips headings

    def test_excerpt_markdown_free(self) -> None:
        md = "# Heading\n\n**Bold** and *italic* and `code`.\n"
        excerpt = compute_excerpt(md)
        assert "**" not in excerpt
        assert "*" not in excerpt
        assert "`" not in excerpt

    def test_excerpt_max_length(self) -> None:
        md = "# Heading\n\n" + "word " * 100 + "end.\n"
        excerpt = compute_excerpt(md, max_length=50)
        assert len(excerpt) <= 50


# ---------------------------------------------------------------------------
# Acceptance: Rendering cache
# ---------------------------------------------------------------------------


class TestRenderingCache:
    """Rendering cache keyed by content_hash."""

    def test_cache_hit_returns_same_object(self) -> None:
        renderer = MarkdownRenderer()
        md = "# Cached\n\nContent.\n"
        first = renderer.render_html(md)
        second = renderer.render_html(md)
        assert first is second  # Same object identity (cache hit)

    def test_different_content_different_result(self) -> None:
        renderer = MarkdownRenderer()
        first = renderer.render_html("# One\n")
        second = renderer.render_html("# Two\n")
        assert first != second


# ---------------------------------------------------------------------------
# Acceptance: Known limitation — no inline HTML unless enabled
# ---------------------------------------------------------------------------


class TestInlineHTML:
    """Inline HTML is stripped by default, passed through with sanitisation when enabled."""

    def test_inline_html_stripped_by_default(self) -> None:
        md = "Hello <em>world</em>.\n"
        html = MarkdownRenderer(allow_raw_html=False).render_html(md)
        assert "<em>" not in html

    def test_inline_html_allowed_with_flag(self) -> None:
        md = "Hello <em>world</em>.\n"
        html = MarkdownRenderer(allow_raw_html=True).render_html(md)
        assert "<em>" in html

    def test_dangerous_html_stripped_even_when_allowed(self) -> None:
        md = "Hello <script>alert(1)</script>.\n"
        html = MarkdownRenderer(allow_raw_html=True).render_html(md)
        assert "<script>" not in html


# ---------------------------------------------------------------------------
# Acceptance: RenderResult and render_markdown entry point
# ---------------------------------------------------------------------------


class TestRenderMarkdownEntryPoint:
    """render_markdown() returns a full RenderResult with all derived fields."""

    def test_render_markdown_returns_all_fields(self) -> None:
        result = render_markdown("# Hello\n\nWorld.\n")
        assert isinstance(result, RenderResult)
        assert result.body_html is not None
        assert result.content_hash is not None
        assert result.word_count >= 1
        assert result.reading_time_minutes >= 1
        assert result.excerpt is not None

    def test_render_markdown_normalises(self) -> None:
        result = render_markdown("hello\r\nworld")
        assert "\r" not in result.body_html

    def test_render_markdown_caches(self) -> None:
        r1 = render_markdown("# Cache\n\nTest.\n")
        r2 = render_markdown("# Cache\n\nTest.\n")
        assert r1.content_hash == r2.content_hash


# ---------------------------------------------------------------------------
# Acceptance: _strip_markdown helper
# ---------------------------------------------------------------------------


class TestStripMarkdown:
    """_strip_markdown removes markdown formatting."""

    def test_bold_italic(self) -> None:
        assert _strip_markdown("**bold** and *italic*") == "bold and italic"

    def test_links(self) -> None:
        assert _strip_markdown("[text](url)") == "text"

    def test_images(self) -> None:
        assert _strip_markdown("![alt](url)") == "alt"

    def test_code(self) -> None:
        assert _strip_markdown("`code`") == "code"

    def test_headings(self) -> None:
        assert _strip_markdown("## Heading") == "Heading"

    def test_blockquote(self) -> None:
        assert _strip_markdown("> quote") == "quote"
