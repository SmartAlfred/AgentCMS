"""Validation & dry-run pipeline (#15).

Runs the full validation pipeline against a post payload **without
persisting anything**:

* Frontmatter JSON schema (per-site, sensible default)
* Field limits (title ≤ 512, description ≤ 160, body_md non-empty)
* Markdown parse (verify markdown-it can render the body)
* Link checking (async HEAD, 3 s total timeout, best-effort)
* Image-alt checking (warn on missing alt)
* Slug availability (check against DB)
* Duplicate detection (simhash ≥ 0.65 or token similarity ≥ 0.45 vs recent posts)
* Publish gates (same checks publish would run)
* Stats: word_count, reading_time_minutes, links, images
* Normalisation preview: slug, excerpt, tag normalisation, markdown fixes

Design:
* Side-effect free: no rows, no revisions, no audit writes, no idempotency
  key consumption.
* Errors block; warnings inform.
* Link failures never become errors — they are always warnings.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.post import Post
from app.models.site import Site
from app.services.markdown import (
    MarkdownRenderer,
    compute_excerpt,
    compute_reading_time_minutes,
    compute_word_count,
    normalise_body,
)

# ---------------------------------------------------------------------------
# Limits & thresholds
# ---------------------------------------------------------------------------

MAX_TITLE_LENGTH = 512
MAX_DESCRIPTION_LENGTH = 160
MAX_BODY_SIZE_BYTES = 262_144  # 256 KB
MAX_TAGS = 20
LONG_POST_THRESHOLD = 5000  # words
# near-duplicate detection (char-level rewrites).  Calibrated against the
# *stable* simhash: 64-bit simhash on short texts is noisy, and the old 0.65
# sat inside that noise band (unrelated short posts measured 0.67).
SIMHASH_THRESHOLD = 0.80
TOKEN_SIMILARITY_THRESHOLD = 0.45  # paraphrased reposts (token-level rewrites)
LINK_TIMEOUT_SECONDS = 3.0  # total timeout for all link checks
PER_LINK_TIMEOUT_SECONDS = 2.0  # timeout per individual link
MAX_LINKS_TO_CHECK = 20  # cap the number of links we attempt to check

# ---------------------------------------------------------------------------
# Frontmatter JSON Schema (sensible default)
# ---------------------------------------------------------------------------

DEFAULT_FRONTMATTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": MAX_TITLE_LENGTH},
        "description": {"type": "string", "maxLength": MAX_DESCRIPTION_LENGTH},
        "tags": {"type": "array", "items": {"type": "string"}},
        "date": {"type": "string", "format": "date-time"},
        "canonical_url": {"type": "string", "format": "uri"},
        "draft": {"type": "boolean", "description": "Disallowed — status is separate"},
    },
    "required": [],
    "additionalProperties": True,
}


def _get_frontmatter_schema(site: Site | None) -> dict[str, Any]:
    """Return the frontmatter schema for a site, falling back to default."""
    if site is not None and site.settings:
        custom = site.settings.get("frontmatter_schema")
        if custom and isinstance(custom, dict):
            return custom
    return DEFAULT_FRONTMATTER_SCHEMA


# ---------------------------------------------------------------------------
# Validation result dataclass
# ---------------------------------------------------------------------------


class ValidationResult:
    """Structured result of the validation pipeline."""

    __slots__ = (
        "errors",
        "normalised",
        "stats",
        "valid",
        "warnings",
        "would_create",
    )

    def __init__(
        self,
        *,
        valid: bool,
        normalised: dict[str, Any],
        would_create: dict[str, Any],
        errors: list[dict[str, str]],
        warnings: list[str],
        stats: dict[str, Any],
    ) -> None:
        self.valid = valid
        self.normalised = normalised
        self.would_create = would_create
        self.errors = errors
        self.warnings = warnings
        self.stats = stats

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "normalised": self.normalised,
            "would_create": self.would_create,
            "errors": self.errors,
            "warnings": self.warnings,
            "stats": self.stats,
        }


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _slugify(text: str) -> str:
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-")[:256]


def _derive_title(body_md: str) -> str | None:
    match = _TITLE_RE.search(body_md)
    return match.group(1).strip() if match else None


# ---------------------------------------------------------------------------
# SimHash for duplicate detection
# ---------------------------------------------------------------------------


def _stable_token_hash(token: str) -> int:
    """Deterministic 64-bit hash of a token.

    ``hash()`` is salted per interpreter process (PYTHONHASHSEED), which made
    simhash -- and therefore duplicate detection -- non-deterministic across
    processes, restarts and CI runs.  blake2b is stable everywhere.
    """
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")


def _simhash(text: str) -> int:
    """Compute a 64-bit SimHash fingerprint for near-duplicate detection."""
    tokens = re.findall(r"\w+", text.lower())
    if not tokens:
        return 0

    v = [0] * 64
    for token in tokens:
        h = _stable_token_hash(token)
        for i in range(64):
            if h & (1 << i):
                v[i] += 1
            else:
                v[i] -= 1

    fingerprint = 0
    for i in range(64):
        if v[i] > 0:
            fingerprint |= 1 << i
    return fingerprint


def _hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _similarity(a: int, b: int) -> float:
    """Return similarity in [0, 1] based on hamming distance of 64-bit hashes."""
    dist = _hamming_distance(a, b)
    return 1.0 - (dist / 64.0)


def _sequence_similarity(a: str, b: str) -> float:
    """Token-level similarity using SequenceMatcher for paraphrase detection."""
    import difflib

    tokens_a = re.findall(r"\w+", a.lower())
    tokens_b = re.findall(r"\w+", b.lower())
    return difflib.SequenceMatcher(None, tokens_a, tokens_b).ratio()


# ---------------------------------------------------------------------------
# Link checking (best-effort, async, bounded)
# ---------------------------------------------------------------------------


def _extract_links(body_md: str) -> list[str]:
    """Extract all URLs from markdown links, images and angle-bracket URLs.

    Only URLs are returned (never the link text), so the result is a flat
    ``list[str]`` that every caller can iterate without unpacking.
    """
    urls: list[str] = []
    # Markdown links: [text](url)
    for _text, url in re.findall(r"\[([^\]]*)\]\(([^)]+)\)", body_md):
        urls.append(str(url))
    # Image links: ![alt](url)
    for _alt, url in re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", body_md):
        urls.append(str(url))
    # Raw URLs in angle brackets
    urls.extend(re.findall(r"<(https?://[^>]+)>", body_md))
    # Deduplicate while preserving order, skipping in-page anchors
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        if url not in seen and not url.startswith("#"):
            seen.add(url)
            result.append(url)
    return result


def _extract_images(body_md: str) -> list[tuple[str, str]]:
    """Extract (alt_text, url) pairs from markdown images."""
    return re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", body_md)


def _check_one_link(url: str) -> str | None:
    """HEAD a single URL. Returns a warning string on failure, else ``None``."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None
        resp = httpx.head(
            url,
            timeout=PER_LINK_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": "AgentCMS/0.3 (link-checker)"},
        )
        if resp.status_code >= 400 and resp.status_code != 405:
            # 405 = the server answered, it just dislikes HEAD
            return f"LINK_UNREACHABLE: {url} returned {resp.status_code}"
        return None
    except Exception:
        return f"LINK_UNREACHABLE: {url}"


def _check_links(body_md: str) -> list[str]:
    """Check links in a thread pool, bounded by ``LINK_TIMEOUT_SECONDS``.

    Returns a list of ``LINK_UNREACHABLE`` warning strings. Never raises and
    never blocks for longer than the total timeout.

    Note: callers are synchronous FastAPI handlers running in a worker thread,
    where ``asyncio.get_event_loop()`` raises ``RuntimeError``. Link checking
    therefore uses a plain thread pool and no asyncio at all.
    """
    import concurrent.futures

    links = _extract_links(body_md)[:MAX_LINKS_TO_CHECK]
    if not links:
        return []

    warnings: list[str] = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=5)
    try:
        futures = {pool.submit(_check_one_link, url): url for url in links}
        done, not_done = concurrent.futures.wait(
            futures,
            timeout=LINK_TIMEOUT_SECONDS,
            return_when=concurrent.futures.ALL_COMPLETED,
        )
        for future in not_done:
            future.cancel()
            warnings.append(f"LINK_UNREACHABLE: {futures[future]} (timeout)")
        for future in done:
            url = futures[future]
            try:
                failure = future.result()
            except Exception:  # pragma: no cover - defensive
                failure = f"LINK_UNREACHABLE: {url}"
            if failure:
                warnings.append(failure)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return warnings


# ---------------------------------------------------------------------------
# Frontmatter validation
# ---------------------------------------------------------------------------


def _validate_frontmatter(frontmatter: dict[str, Any] | None, schema: dict[str, Any]) -> list[dict[str, str]]:
    """Validate frontmatter against the schema. Returns field-level errors."""
    errors: list[dict[str, str]] = []

    if frontmatter is None:
        frontmatter = {}

    # Check required fields
    for field in schema.get("required", []):
        if field not in frontmatter:
            errors.append(
                {
                    "field": f"frontmatter.{field}",
                    "code": "missing-field",
                    "message": f"Frontmatter field '{field}' is required.",
                }
            )

    # Check properties
    properties = schema.get("properties", {})
    for field_name, field_schema in properties.items():
        value = frontmatter.get(field_name)
        if value is None:
            continue

        expected_type = field_schema.get("type")
        if expected_type == "string" and not isinstance(value, str):
            errors.append(
                {
                    "field": f"frontmatter.{field_name}",
                    "code": "invalid-type",
                    "message": f"Frontmatter '{field_name}' must be a string.",
                }
            )
        elif expected_type == "array" and not isinstance(value, list):
            errors.append(
                {
                    "field": f"frontmatter.{field_name}",
                    "code": "invalid-type",
                    "message": f"Frontmatter '{field_name}' must be an array.",
                }
            )
        elif expected_type == "boolean" and field_name == "draft":
            # draft is disallowed in frontmatter — status is separate
            errors.append(
                {
                    "field": f"frontmatter.{field_name}",
                    "code": "disallowed-field",
                    "message": (
                        "The 'draft' field is disallowed in frontmatter. "
                        "Use the 'status' field or the publish endpoint."
                    ),
                }
            )

        # maxLength check
        if isinstance(value, str) and "maxLength" in field_schema and len(value) > field_schema["maxLength"]:
            errors.append(
                {
                    "field": f"frontmatter.{field_name}",
                    "code": "value-too-long",
                    "message": (
                        f"Frontmatter '{field_name}' exceeds {field_schema['maxLength']} characters."
                    ),
                }
            )

    return errors


# ---------------------------------------------------------------------------
# Main validation pipeline
# ---------------------------------------------------------------------------


def validate_post(
    session: Session,
    *,
    site_slug: str,
    body_md: str | None,
    title: str | None = None,
    slug: str | None = None,
    tags: list[str] | None = None,
    excerpt: str | None = None,
    frontmatter: dict[str, Any] | None = None,
    existing_post_id: str | None = None,
    check_links: bool = True,
) -> ValidationResult:
    """Run the full validation pipeline without persisting.

    Parameters
    ----------
    session:
        Database session (read-only checks).
    site_slug:
        Target site slug.
    body_md:
        Markdown body. ``None`` is accepted so a caller can validate a payload
        that omits it; it is reported as a ``content-required`` error.
    title:
        Optional title (derived from H1 if omitted).
    slug:
        Optional slug (derived from title if omitted).
    tags:
        Optional tag list.
    excerpt:
        Optional excerpt (derived from body if omitted).
    frontmatter:
        Optional frontmatter dict.
    existing_post_id:
        If set, this is an update — skip slug-availability check for this post.
    check_links:
        Whether to run link checking (default True).

    Returns
    -------
    ValidationResult with valid, normalised, would_create, errors, warnings, stats.
    """
    body_md = body_md or ""
    errors: list[dict[str, str]] = []
    warnings: list[str] = []

    # Resolve the "this is the post being updated" identifier once — used by the
    # slug-availability check (below) and to keep a post from matching itself
    # during duplicate detection.
    existing_uuid: uuid.UUID | None = None
    if existing_post_id:
        try:
            existing_uuid = uuid.UUID(existing_post_id)
        except ValueError:
            existing_uuid = None

    # 1. Resolve site
    site: Site | None = None
    try:
        exists = session.query(func.count(Site.id)).filter(Site.slug == site_slug).scalar()
        if not exists:
            errors.append(
                {
                    "field": "site_slug",
                    "code": "site-not-found",
                    "message": f"No site with slug '{site_slug}' exists.",
                }
            )
        else:
            site = session.query(Site).filter(Site.slug == site_slug).one()
    except Exception:
        errors.append(
            {
                "field": "site_slug",
                "code": "site-not-found",
                "message": f"Could not resolve site '{site_slug}'.",
            }
        )

    # 2. Body validation
    if not body_md or not body_md.strip():
        errors.append(
            {
                "field": "body_md",
                "code": "content-required",
                "message": "Document body required — body_md is the only required field.",
            }
        )

    # 3. Normalise body
    normalised_body = body_md
    norm_warnings: list[str] = []
    if body_md and body_md.strip():
        normalised_body, norm_warnings = normalise_body(body_md)
        warnings.extend(norm_warnings)

    # 4. Markdown parse check
    if body_md and body_md.strip():
        try:
            renderer = MarkdownRenderer()
            renderer.render_html(normalised_body)
        except Exception as exc:
            errors.append(
                {
                    "field": "body_md",
                    "code": "markdown-parse-error",
                    "message": f"Markdown could not be parsed: {exc}",
                }
            )

    # 5. Compute derived fields
    has_content = body_md and body_md.strip()
    word_count = compute_word_count(normalised_body) if has_content else 0
    reading_time = compute_reading_time_minutes(normalised_body) if has_content else 0
    derived_excerpt = compute_excerpt(normalised_body) if has_content else ""

    # 6. Extract links and images for stats
    links_in_body = _extract_links(normalised_body) if body_md and body_md.strip() else []
    images_in_body = _extract_images(normalised_body) if body_md and body_md.strip() else []

    # 7. Title derivation and validation
    final_title = title
    if title is None or not title.strip():
        derived = _derive_title(normalised_body) if body_md and body_md.strip() else None
        if derived is None:
            errors.append(
                {
                    "field": "title",
                    "code": "title-required",
                    "message": "Neither 'title' nor a first-level heading in 'body_md' was found.",
                }
            )
        else:
            final_title = derived
            warnings.append("Title was derived from the first H1 in body_md.")

    if final_title and len(final_title) > MAX_TITLE_LENGTH:
        errors.append(
            {
                "field": "title",
                "code": "value-too-long",
                "message": f"Title exceeds {MAX_TITLE_LENGTH} characters.",
            }
        )

    # 8. Slug derivation and validation
    final_slug = slug
    if slug is None or not slug.strip():
        if final_title:
            final_slug = _slugify(final_title)
            warnings.append("Slug was derived from the title.")
        else:
            final_slug = ""

    if final_slug and not _SLUG_RE.match(final_slug):
        errors.append(
            {
                "field": "slug",
                "code": "slug-invalid",
                "message": (
                    f"Slug '{final_slug}' is not usable: must contain only "
                    "lowercase letters, digits, and single hyphens."
                ),
            }
        )

    # 9. Slug availability
    if final_slug and site and not errors:
        query = session.query(Post).filter(Post.site_id == site.id, Post.slug == final_slug)
        if existing_uuid is not None:
            query = query.filter(Post.id != existing_uuid)
        if query.first() is not None:
            errors.append(
                {
                    "field": "slug",
                    "code": "slug-conflict",
                    "message": f"Slug '{final_slug}' is already taken.",
                }
            )

    # 10. Tags normalisation
    final_tags = tags or []
    if final_tags:
        final_tags = [t.lower().strip() for t in final_tags if t.strip()][:MAX_TAGS]

    # 11. Excerpt
    final_excerpt = excerpt if excerpt is not None else derived_excerpt

    # 12. Frontmatter validation
    if site is not None:
        schema = _get_frontmatter_schema(site)
        fm_errors = _validate_frontmatter(frontmatter, schema)
        errors.extend(fm_errors)

    # 13. Image alt checking
    for alt_text, img_url in images_in_body:
        if not alt_text or not alt_text.strip():
            warnings.append(f"IMG_MISSING_ALT: image '{img_url}' has no alt text.")

    # 14. Link checking (best-effort, never errors)
    if check_links and body_md and body_md.strip():
        link_warnings = _check_links(normalised_body)
        warnings.extend(link_warnings)

    # 15. Long post warning
    if word_count > LONG_POST_THRESHOLD:
        warnings.append(f"LONG_POST: {word_count} words exceeds {LONG_POST_THRESHOLD} threshold.")

    # 16. No description warning (SEO)
    if not frontmatter or not frontmatter.get("description"):
        warnings.append("NO_DESCRIPTION: frontmatter 'description' is missing.")

    # 17. Duplicate detection (simhash + token similarity against recent posts)
    #
    # Two signals, because either alone misses real reposts:
    # * simhash catches character-level rewrites of the same text;
    # * token-level similarity catches paraphrases that reuse the same
    #   vocabulary in a different order. Thresholds were calibrated against a
    #   known paraphrase pair (0.69 simhash / 0.48 tokens) and a known
    #   unrelated pair (0.55 simhash / 0.06 tokens).
    if body_md and body_md.strip() and site:
        recent_query = session.query(Post.id, Post.body_md).filter(
            Post.site_id == site.id, Post.deleted_at.is_(None)
        )
        if existing_uuid is not None:
            # An update must not be reported as a duplicate of itself.
            recent_query = recent_query.filter(Post.id != existing_uuid)
        recent_posts = recent_query.order_by(Post.created_at.desc()).limit(50).all()
        current_hash = _simhash(normalised_body)
        for _recent_id, recent_body in recent_posts:
            if not recent_body:
                continue
            sim = _similarity(current_hash, _simhash(recent_body))
            if sim >= SIMHASH_THRESHOLD:
                warnings.append(f"DUPLICATE_LIKELY: content is {sim:.0%} similar to a recent post.")
                break
            # Also check token-level similarity for paraphrase detection
            tok_sim = _sequence_similarity(normalised_body, recent_body)
            if tok_sim >= TOKEN_SIMILARITY_THRESHOLD:
                warnings.append(f"DUPLICATE_LIKELY: content is {tok_sim:.0%} similar to a recent post.")
                break

    # 18. Build normalised preview
    normalised: dict[str, Any] = {
        "body_md": normalised_body,
        "title": final_title,
        "slug": final_slug,
        "tags": final_tags,
        "excerpt": final_excerpt,
        "frontmatter": frontmatter or {},
    }

    # 19. Build would_create
    would_create: dict[str, Any] = {}
    if not errors:
        would_create = {
            "slug": final_slug,
            "status": "draft",
            "url": f"/posts/{final_slug}" if final_slug else "",
        }

    # 20. Stats
    stats: dict[str, Any] = {
        "word_count": word_count,
        "reading_time_minutes": reading_time,
        "links": len(links_in_body),
        "images": len(images_in_body),
    }

    return ValidationResult(
        valid=len(errors) == 0,
        normalised=normalised,
        would_create=would_create,
        errors=errors,
        warnings=warnings,
        stats=stats,
    )
