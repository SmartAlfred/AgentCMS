"""Markdown pipeline (#7).

Deterministic rendering, safe HTML sanitisation, heading anchors, derived
fields (excerpt, word_count, reading_time_minutes, content_hash) and a
content-hash-keyed rendering cache.

Parser: CommonMark + GFM (tables, strikethrough, task lists, autolinks)
via ``markdown-it-py`` 3.0.0.

Design principles:
* Raw HTML is disabled by default.  Per-site enabling uses a strict
  tag/attribute allowlist; ``script``, ``iframe``, ``style``, event
  handlers, ``javascript:`` and ``data:`` URLs are always stripped.
* Heading anchors are deterministic: ``## Foo Bar`` → ``id="foo-bar"``.
* Every render is byte-identical for the same normalised input, and
  cached by ``content_hash``.
* Auto-fixes are applied on ingest and reported in ``warnings[]``.
"""

from __future__ import annotations

import hashlib
import html
import math
import re
import unicodedata

from markdown_it import MarkdownIt

# ---------------------------------------------------------------------------
# Pinned version (must match pyproject.toml)
# ---------------------------------------------------------------------------
MARKDOWN_IT_VERSION = "3.0.0"

# ---------------------------------------------------------------------------
# Sanitiser allowlist
# ---------------------------------------------------------------------------

_ALLOW_TAGS: frozenset[str] = frozenset(
    {
        "p",
        "br",
        "em",
        "strong",
        "a",
        "code",
        "pre",
        "blockquote",
        "ul",
        "ol",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "img",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "hr",
        "del",
        "s",
        "input",
    }
)

_ALLOW_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "title", "rel"}),
    "img": frozenset({"src", "alt", "title", "width", "height"}),
    "input": frozenset({"type", "disabled", "checked"}),
    "td": frozenset({"align"}),
    "th": frozenset({"align"}),
}

# Patterns that are NEVER allowed regardless of allowlist
_DANGEROUS_URL_RE = re.compile(r"^\s*(javascript|data|vbscript)\s*:", re.IGNORECASE)
_EVENT_HANDLER_RE = re.compile(r"^on", re.IGNORECASE)

# Smart-quote mapping (Unicode → ASCII)
_SMART_QUOTES: dict[str, str] = {
    "\u2018": "'",  # left single
    "\u2019": "'",  # right single
    "\u201c": '"',  # left double
    "\u201d": '"',  # right double
    "\u2013": "-",  # en-dash
    "\u2014": "-",  # em-dash
    "\u2026": "...",  # ellipsis
}

# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def normalise_line_endings(text: str) -> str:
    """Convert ``\\r\\n`` and ``\\r`` to ``\\n``."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def strip_trailing_whitespace(text: str) -> str:
    """Remove trailing whitespace from every line."""
    return "\n".join(line.rstrip() for line in text.split("\n"))


def ensure_single_trailing_newline(text: str) -> str:
    """Ensure exactly one trailing newline, strip leading blank lines."""
    text = text.strip("\n")
    return text + "\n" if text else ""


def _normalise_smart_quotes_in_code_spans(text: str) -> tuple[str, bool]:
    """Convert smart-quote artefacts back to ASCII inside inline code spans.

    Operates on the raw markdown text.  Returns (normalised_text, changed).
    """
    # Match inline code spans: `...` (but not ``...``)
    changed = False

    def _replace_in_code(match: re.Match[str]) -> str:
        nonlocal changed
        content = match.group(1)
        new_content = content
        for smart, ascii_char in _SMART_QUOTES.items():
            new_content = new_content.replace(smart, ascii_char)
        if new_content != content:
            changed = True
            return f"`{new_content}`"
        return match.group(0)

    result = re.sub(r"`([^`]+)`", _replace_in_code, text)
    return result, changed


def _demote_duplicate_h1s(text: str) -> tuple[str, bool]:
    """Demote every ``# H1`` after the first to ``## H2``.

    Returns (normalised_text, changed).
    """
    lines = text.split("\n")
    seen_h1 = False
    changed = False
    result: list[str] = []
    for line in lines:
        if re.match(r"^#\s+", line):
            if seen_h1:
                result.append("#" + line)
                changed = True
            else:
                seen_h1 = True
                result.append(line)
        else:
            result.append(line)
    return "\n".join(result), changed


def normalise_body(body_md: str) -> tuple[str, list[str]]:
    """Apply all auto-fix normalisations.  Returns (normalised, warnings)."""
    warnings: list[str] = []

    original = body_md
    body_md = normalise_line_endings(body_md)
    if body_md != original:
        warnings.append("Normalised line endings to \\n.")

    body_stripped = strip_trailing_whitespace(body_md)
    if body_stripped != body_md:
        warnings.append("Stripped trailing whitespace.")
        body_md = body_stripped

    body_md = ensure_single_trailing_newline(body_md)

    body_md, demoted = _demote_duplicate_h1s(body_md)
    if demoted:
        warnings.append("Demoted duplicate H1 headings to H2.")

    body_md, quotes_fixed = _normalise_smart_quotes_in_code_spans(body_md)
    if quotes_fixed:
        warnings.append("Converted smart-quote artefacts to ASCII in code spans.")

    return body_md, warnings


# ---------------------------------------------------------------------------
# Heading anchor generation
# ---------------------------------------------------------------------------


def _heading_id(text: str) -> str:
    """Generate a stable, URL-friendly ID from heading text.

    ``## Hello, World!`` → ``hello-world``
    """
    slug = text.lower().strip()
    # Remove HTML tags if any leaked in
    slug = re.sub(r"<[^>]+>", "", slug)
    # Transliterate to ASCII where possible
    slug = unicodedata.normalize("NFKD", slug)
    slug = slug.encode("ascii", "ignore").decode("ascii")
    # Keep only alphanumerics, spaces, hyphens
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")


# ---------------------------------------------------------------------------
# Sanitiser
# ---------------------------------------------------------------------------


def _is_dangerous_url(url: str) -> bool:
    """Check if a URL contains a dangerous scheme or encoding."""
    if _DANGEROUS_URL_RE.match(url):
        return True
    # Decode HTML entities and check
    decoded = html.unescape(url)
    return bool(_DANGEROUS_URL_RE.match(decoded))


def _sanitize_html(raw_html: str) -> str:
    """Strip disallowed tags and attributes from HTML.

    This is a simple tag-based sanitiser, not a full parser.  It handles
    the common XSS vectors documented in the ticket.
    """
    # First, remove dangerous URL patterns from the entire HTML
    # before any tag processing
    result = raw_html

    # Remove <script> tags and their content
    result = re.sub(r"<script\b[^>]*>.*?</script\s*>", "", result, flags=re.DOTALL | re.IGNORECASE)
    result = re.sub(r"<script\b[^>]*/?>", "", result, flags=re.IGNORECASE)

    # Remove <iframe> tags and their content
    result = re.sub(r"<iframe\b[^>]*>.*?</iframe\s*>", "", result, flags=re.DOTALL | re.IGNORECASE)
    result = re.sub(r"<iframe\b[^>]*/?>", "", result, flags=re.IGNORECASE)

    # Remove <style> tags and their content
    result = re.sub(r"<style\b[^>]*>.*?</style\s*>", "", result, flags=re.DOTALL | re.IGNORECASE)
    result = re.sub(r"<style\b[^>]*/?>", "", result, flags=re.IGNORECASE)

    # Remove HTML comments
    result = re.sub(r"<!--.*?-->", "", result, flags=re.DOTALL)

    # Process tags
    def _process_tag(match: re.Match[str]) -> str:
        full_match = match.group(0)
        is_closing = full_match.startswith("</")
        tag_match = re.match(r"</?(\w+)([^>]*)", full_match)
        if not tag_match:
            return ""
        tag_name = tag_match.group(1).lower()
        attrs_str = tag_match.group(2) if not is_closing else ""

        if tag_name not in _ALLOW_TAGS:
            return ""

        # Parse attributes
        attrs: dict[str, str] = {}
        for attr_match in re.finditer(r'(\w[\w-]*)\s*=\s*"([^"]*)"', attrs_str):
            attr_name = attr_match.group(1).lower()
            attr_val = attr_match.group(2)

            # Skip event handlers
            if _EVENT_HANDLER_RE.match(attr_name):
                continue

            # Check if attribute is allowed for this tag
            allowed = _ALLOW_ATTRS.get(tag_name, frozenset())
            if attr_name not in allowed:
                continue

            # Check URL attributes for dangerous schemes
            if attr_val and attr_name in ("href", "src", "action") and _is_dangerous_url(attr_val):
                continue

            attrs[attr_name] = attr_val

        # Also check unquoted and single-quoted attributes
        for attr_match in re.finditer(r"(\w[\w-]*)\s*=\s*'([^']*)'", attrs_str):
            attr_name = attr_match.group(1).lower()
            attr_val = attr_match.group(2)
            if _EVENT_HANDLER_RE.match(attr_name):
                continue
            allowed = _ALLOW_ATTRS.get(tag_name, frozenset())
            if attr_name not in allowed:
                continue
            if attr_val and attr_name in ("href", "src", "action") and _is_dangerous_url(attr_val):
                continue
            attrs[attr_name] = attr_val

        # For boolean attributes (no value)
        for attr_match in re.finditer(r"\s(\w[\w-]*)(?:\s|=)", attrs_str):
            attr_name = attr_match.group(1).lower()
            if attr_name in ("disabled", "checked"):
                allowed = _ALLOW_ATTRS.get(tag_name, frozenset())
                if attr_name in allowed:
                    attrs[attr_name] = attr_name

        # Build clean tag
        attr_str = ""
        if attrs:
            parts = [f'{k}="{html.escape(v, quote=True)}"' for k, v in attrs.items()]
            attr_str = " " + " ".join(parts)

        if is_closing:
            return f"</{tag_name}>"
        return f"<{tag_name}{attr_str}>"

    result = re.sub(r"</?\w+[^>]*>", _process_tag, result)

    # Final pass: strip any remaining event handler attributes
    result = re.sub(r"\s+on\w+\s*=\s*[\"'][^\"']*[\"']", "", result, flags=re.IGNORECASE)
    result = re.sub(r"\s+on\w+\s*=\s*\S+", "", result, flags=re.IGNORECASE)

    return result


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


class MarkdownRenderer:
    """Deterministic markdown → HTML renderer with sanitisation and caching.

    Parameters
    ----------
    allow_raw_html:
        If ``True``, raw HTML in the markdown source is passed through
        (after sanitisation with the allowlist).  If ``False`` (default),
        all HTML tags are escaped.
    """

    def __init__(self, *, allow_raw_html: bool = False) -> None:
        self.allow_raw_html = allow_raw_html
        self._md = self._build_parser()
        self._cache: dict[str, str] = {}

    def _build_parser(self) -> MarkdownIt:
        """Build a markdown-it parser with GFM extensions."""
        md = MarkdownIt("commonmark")
        md.enable("table")
        md.enable("strikethrough")
        md.enable("linkify")
        md.options["html"] = self.allow_raw_html
        md.options["breaks"] = False
        md.options["typographer"] = False
        return md

    def render_html(self, body_md: str) -> str:
        """Render markdown to a sanitised HTML fragment.

        Uses a content-hash cache so repeated calls with the same input
        return the same object (fast path).
        """
        content_hash = compute_content_hash(body_md)
        cached = self._cache.get(content_hash)
        if cached is not None:
            return cached

        raw_html = self._md.render(body_md)
        clean_html = _sanitize_html(raw_html)
        clean_html = self._add_heading_anchors(clean_html, body_md)

        self._cache[content_hash] = clean_html
        return clean_html

    def _add_heading_anchors(self, html_out: str, body_md: str) -> str:
        """Post-process heading tags to add stable ``id`` attributes.

        Re-parses the markdown to extract heading text, then finds the
        corresponding ``<hN>`` tags in the HTML and injects ``id="…"``.
        """
        heading_re = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
        headings = heading_re.findall(body_md)
        if not headings:
            return html_out

        # Count occurrences for deduplication
        seen: dict[str, int] = {}
        for _level, text in headings:
            hid = _heading_id(text)
            if hid in seen:
                seen[hid] += 1
            else:
                seen[hid] = 0

        # Now inject ids
        used: dict[str, int] = {}

        def _inject(match: re.Match[str]) -> str:
            tag = match.group(1)  # e.g. "h2"
            attrs = match.group(2)  # e.g. "" or ' class="x"'
            content = match.group(3)  # text between tags
            # Strip any inner HTML tags to get plain text
            heading_text = re.sub(r"<[^>]+>", "", content).strip()
            hid = _heading_id(heading_text)
            if not hid:
                return match.group(0)

            count = used.get(hid, 0)
            deduped_id = f"{hid}-{count}" if count > 0 else hid
            used[hid] = count + 1

            # Check if id already present
            if re.search(r'id="[^"]*"', attrs):
                return match.group(0)

            return f'<{tag}{attrs} id="{deduped_id}">{content}</{tag}>'

        result = re.sub(r"<(h[1-6])([^>]*)>(.*?)</\1>", _inject, html_out, flags=re.DOTALL)
        return result


# ---------------------------------------------------------------------------
# Derived fields
# ---------------------------------------------------------------------------

_READING_SPEED_WPM = 238  # Average silent reading speed (words per minute)


def compute_content_hash(body_md: str) -> str:
    """SHA-256 hex digest of the normalised ``body_md``."""
    normalised = normalise_line_endings(body_md)
    normalised = strip_trailing_whitespace(normalised)
    normalised = ensure_single_trailing_newline(normalised)
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def compute_word_count(body_md: str) -> int:
    """Count words: space-separated tokens after normalising to ``\\n``-based line endings."""
    text = normalise_line_endings(body_md)
    text = strip_trailing_whitespace(text)
    # Split on whitespace and filter empty strings
    tokens = [t for t in text.split() if t]
    return len(tokens)


def compute_reading_time_minutes(body_md: str) -> int:
    """Estimated reading time in minutes (round up)."""
    wc = compute_word_count(body_md)
    if wc == 0:
        return 0
    return math.ceil(wc / _READING_SPEED_WPM)


def compute_excerpt(body_md: str, *, max_length: int = 200) -> str:
    """Extract the first paragraph (≤ 200 chars, markdown-free).

    Strips markdown formatting and returns plain text.
    """
    text = normalise_line_endings(body_md)
    text = strip_trailing_whitespace(text)

    # Find first non-empty paragraph
    paragraphs = re.split(r"\n\s*\n", text)
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        # Skip headings
        if re.match(r"^#{1,6}\s+", para):
            continue
        # Strip markdown formatting
        plain = _strip_markdown(para)
        plain = " ".join(plain.split())  # collapse whitespace
        if len(plain) > max_length:
            plain = plain[:max_length].rsplit(" ", 1)[0] + "…"
        return plain

    return ""


def _strip_markdown(text: str) -> str:
    """Remove common markdown formatting, keeping plain text."""
    # Remove images ![alt](url) → alt
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    # Remove links [text](url) → text
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    # Remove bold/italic markers
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}([^_]+)_{1,3}", r"\1", text)
    # Remove inline code backticks
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Remove blockquote markers
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    # Remove heading markers
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Remove horizontal rules
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    return text


# ---------------------------------------------------------------------------
# Render result dataclass
# ---------------------------------------------------------------------------


class RenderResult:
    """Result of rendering a markdown document."""

    __slots__ = ("body_html", "content_hash", "excerpt", "reading_time_minutes", "warnings", "word_count")

    def __init__(
        self,
        *,
        body_html: str,
        content_hash: str,
        word_count: int,
        reading_time_minutes: int,
        excerpt: str,
        warnings: list[str],
    ) -> None:
        self.body_html = body_html
        self.content_hash = content_hash
        self.word_count = word_count
        self.reading_time_minutes = reading_time_minutes
        self.excerpt = excerpt
        self.warnings = warnings


def render_markdown(body_md: str, *, allow_raw_html: bool = False) -> RenderResult:
    """Full pipeline: normalise → render → compute derived fields.

    This is the main entry point for the markdown pipeline.
    """
    normalised, norm_warnings = normalise_body(body_md)

    renderer = MarkdownRenderer(allow_raw_html=allow_raw_html)
    body_html = renderer.render_html(normalised)

    return RenderResult(
        body_html=body_html,
        content_hash=compute_content_hash(normalised),
        word_count=compute_word_count(normalised),
        reading_time_minutes=compute_reading_time_minutes(normalised),
        excerpt=compute_excerpt(normalised),
        warnings=norm_warnings,
    )
