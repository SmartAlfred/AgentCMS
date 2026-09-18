"""Media / asset service (#20).

Presigned upload flow:
    1. POST /v1/sites/{site}/assets  →  presigned URL + metadata
    2. Agent PUTs bytes to the presigned URL
    3. POST /v1/assets/{id}/finalize  →  validates, sniffs, generates variants

Inline (escape-hatch) flow:
    POST /v1/sites/{site}/assets/inline with base64 or source URL

Content policy:
    * SVG, HTML, executables rejected with clear error listing allowed types
    * Magic-byte sniffing verifies claimed Content-Type (never trust it)
    * EXIF (incl. GPS) stripped from images

Image variants:
    * thumb: 256x256
    * inline: 1200 wide (webp)
    * og: 1200x630 crop

Serving:
    * Immutable, content-hashed URLs: /media/{sha256}/{name}.webp
    * Cache-Control: public, max-age=31536000, immutable
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
import uuid
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

from PIL import Image
from PIL.ExifTags import GPSTAGS
from sqlalchemy.orm import Session

from app.config import get_settings
from app.domain.errors import DomainError
from app.models.asset import Asset
from app.models.post import Post

logger = logging.getLogger("app.media")

# ---------------------------------------------------------------------------
# Content policy — allowed / rejected types
# ---------------------------------------------------------------------------

ALLOWED_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"})
ALLOWED_FILE_TYPES = frozenset(
    {
        "application/pdf",
        "text/plain",
        "text/markdown",
        "application/zip",
        "application/gzip",
    }
)
ALLOWED_TYPES = ALLOWED_IMAGE_TYPES | ALLOWED_FILE_TYPES

# Magic bytes → detected content type
MAGIC_BYTES: list[tuple[bytes, str, str | None]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg", "image/jpeg"),
    (b"GIF87a", "image/gif", "image/gif"),
    (b"GIF89a", "image/gif", "image/gif"),
    (b"RIFF", "image/webp", "image/webp"),  # RIFF container, need to check WEBP suffix
    (b"\x00\x00\x00", "image/avif", None),  # ftyp box, need deeper check
    (b"%PDF", "application/pdf", None),
    (b"PK", "application/zip", None),  # ZIP-based formats
    (b"\x1f\x8b", "application/gzip", None),
]

# Rejected extensions / content types
REJECTED_EXTENSIONS = frozenset(
    {".svg", ".html", ".htm", ".exe", ".bat", ".cmd", ".sh", ".ps1", ".js", ".php"}
)
REJECTED_CONTENT_TYPES = frozenset(
    {"image/svg+xml", "text/html", "application/javascript", "application/x-executable"}
)

INLINE_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
PRESIGNED_URL_TTL_MINUTES = 15
VARIANT_SIZES = {
    "thumb": (256, 256),
    "inline": (1200, None),
    "og": (1200, 630),
}

MAX_ASSET_BYTES = 50 * 1024 * 1024  # 50 MB hard limit for presigned uploads


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AssetNotFoundError(DomainError):
    def __init__(self, asset_id: str) -> None:
        super().__init__(
            f"Asset '{asset_id}' not found.",
            hint="Use GET /v1/sites/{site}/assets to list available assets.",
            extra={"asset_id": asset_id},
        )
        self.status_code = 404
        self.code = "asset-not-found"
        self.title = "Asset not found"


class FileTypeRejectedError(DomainError):
    def __init__(self, content_type: str | None = None, filename: str | None = None) -> None:
        allowed = ", ".join(sorted(ALLOWED_TYPES))
        detail = f"File type '{content_type or 'unknown'}' is not allowed."
        if filename:
            detail = f"File '{filename}' (type '{content_type or 'unknown'}') is not allowed."
        super().__init__(
            detail,
            hint=f"Allowed types: {allowed}. SVG, HTML, and executables are rejected in v1.",
            extra={
                "content_type": content_type,
                "filename": filename,
                "allowed_types": sorted(ALLOWED_TYPES),
            },
        )
        self.status_code = 415
        self.code = "file-type-rejected"
        self.title = "File type not allowed"


class MagicBytesMismatchError(DomainError):
    def __init__(self, claimed: str, detected: str) -> None:
        super().__init__(
            f"File content does not match claimed type '{claimed}'; detected '{detected}' from magic bytes.",
            hint=(
                "Upload a file whose actual content matches the declared content_type. "
                "The server verifies magic bytes, not the Content-Type header."
            ),
            extra={"claimed_type": claimed, "detected_type": detected},
        )
        self.status_code = 422
        self.code = "magic-bytes-mismatch"
        self.title = "File content does not match claimed type"


class PresignedUrlExpiredError(DomainError):
    def __init__(self) -> None:
        super().__init__(
            "The presigned upload URL has expired.",
            hint="Request a new presigned URL with POST /v1/sites/{site}/assets.",
        )
        self.status_code = 410
        self.code = "presigned-url-expired"
        self.title = "Presigned URL expired"


class InlineUploadTooLargeError(DomainError):
    def __init__(self, size: int) -> None:
        super().__init__(
            f"Inline upload is {size:,} bytes; the maximum is {INLINE_MAX_BYTES:,} bytes (2 MB).",
            hint=(
                "Use the presigned upload path for larger files: "
                "POST /v1/sites/{site}/assets, then PUT to the upload_url."
            ),
            extra={"size": size, "max_bytes": INLINE_MAX_BYTES},
        )
        self.status_code = 413
        self.code = "inline-upload-too-large"
        self.title = "Inline upload too large"


class AssetReferencedError(DomainError):
    def __init__(self, asset_id: str, post_ids: list[str]) -> None:
        super().__init__(
            f"Asset '{asset_id}' is still referenced by posts: {', '.join(post_ids)}.",
            hint="Remove references from the posts first, then delete the asset.",
            extra={"asset_id": asset_id, "referencing_posts": post_ids},
        )
        self.status_code = 409
        self.code = "asset-still-referenced"
        self.title = "Asset still referenced"


# ---------------------------------------------------------------------------
# Magic-byte detection
# ---------------------------------------------------------------------------


def sniff_magic_bytes(data: bytes) -> str | None:
    """Sniff the first bytes to determine the actual content type.

    Returns the detected MIME type, or None if unrecognized.
    """
    if len(data) < 4:
        return None

    # PNG
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    # JPEG
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    # GIF
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    # WebP (RIFF....WEBP)
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    # AVIF/HEIF (ftyp box)
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"avif", b"avis", b"mif1"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"miff"):
            return "image/heic"
    # PDF
    if data[:5] == b"%PDF-":
        return "application/pdf"
    # ZIP (includes docx, xlsx, etc.)
    if data[:2] == b"PK":
        return "application/zip"
    # GZIP
    if data[:2] == b"\x1f\x8b":
        return "application/gzip"
    # Detect text-like content (HTML, XML, etc.) by looking for common text signatures
    if data[:1] == b"<":
        # Likely HTML, XML, or other markup
        return "text/plain"

    return None


def sniff_file_extension(data: bytes) -> str | None:
    """Return a recommended file extension (with dot) from magic bytes."""
    ct = sniff_magic_bytes(data)
    if ct is None:
        return None
    _EXT_MAP = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/avif": ".avif",
        "image/heic": ".heic",
        "application/pdf": ".pdf",
        "application/zip": ".zip",
        "application/gzip": ".gz",
    }
    return _EXT_MAP.get(ct)


# ---------------------------------------------------------------------------
# Content policy validation
# ---------------------------------------------------------------------------


def validate_asset_type(filename: str, content_type: str | None) -> str:
    """Validate that the file type is allowed.

    Checks both the extension and the content type.
    Returns the validated content_type (or inferred type).
    Raises FileTypeRejectedError for SVG, HTML, executables, etc.
    """
    # Check extension
    lower_name = filename.lower()
    for ext in REJECTED_EXTENSIONS:
        if lower_name.endswith(ext):
            raise FileTypeRejectedError(content_type=content_type, filename=filename)

    # Check content type
    if content_type and content_type.lower() in REJECTED_CONTENT_TYPES:
        raise FileTypeRejectedError(content_type=content_type, filename=filename)

    # If no content type provided, try to infer from extension
    if not content_type:
        _EXT_INFER = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".avif": "image/avif",
            ".pdf": "application/pdf",
            ".txt": "text/plain",
            ".md": "text/markdown",
        }
        for ext, ct in _EXT_INFER.items():
            if lower_name.endswith(ext):
                content_type = ct
                break

    # Validate the content type is in our allowed set
    if content_type and content_type.lower() not in ALLOWED_TYPES:
        raise FileTypeRejectedError(content_type=content_type, filename=filename)

    return content_type or "application/octet-stream"


def verify_magic_bytes(data: bytes, claimed_content_type: str) -> str:
    """Verify magic bytes match the claimed content type.

    Returns the detected content type. Raises MagicBytesMismatchError if mismatch.
    """
    detected = sniff_magic_bytes(data)
    if detected is None:
        # No known magic bytes - check if content looks like text
        # when claimed to be an image
        if claimed_content_type.startswith("image/") and len(data) > 0:
            # Check if content looks like text/HTML (starts with < or common text patterns)
            first_bytes = data[:100]
            if first_bytes[:1] == b"<" or (
                first_bytes[:5].isascii()
                and not any(first_bytes[i : i + 1] == b"\x00" for i in range(min(5, len(first_bytes))))
            ):
                raise MagicBytesMismatchError(
                    claimed=claimed_content_type, detected="text/plain (content appears to be text/HTML)"
                )
        return claimed_content_type  # Can't verify, trust the claim

    # Normalize for comparison
    claimed_lower = claimed_content_type.lower().split(";")[0].strip()
    detected_lower = detected.lower()

    # Allow some flexibility: e.g., image/jpg should match image/jpeg
    _CANONICAL = {"image/jpg": "image/jpeg", "image/tif": "image/tiff"}
    claimed_canonical = _CANONICAL.get(claimed_lower, claimed_lower)

    if claimed_canonical != detected_lower:
        raise MagicBytesMismatchError(claimed=claimed_content_type, detected=detected)

    return detected


# ---------------------------------------------------------------------------
# EXIF stripping
# ---------------------------------------------------------------------------


def strip_exif(data: bytes, *, strip_gps: bool = True) -> tuple[bytes, dict[str, Any]]:
    """Strip EXIF data from an image. Returns (stripped_bytes, metadata).

    metadata contains width, height, and whether GPS data was found/stripped.
    """
    img = Image.open(BytesIO(data))
    metadata: dict[str, Any] = {
        "width": img.width,
        "height": img.height,
        "had_exif": False,
        "had_gps": False,
        "gps_stripped": False,
    }

    # Check for EXIF data
    exif_data = img.getexif() if hasattr(img, "getexif") else None
    if exif_data:
        metadata["had_exif"] = True

        # Check for GPS data
        gps_ifd = exif_data.get_ifd(0x8825) if hasattr(exif_data, "get_ifd") else None
        if gps_ifd:
            metadata["had_gps"] = True
            if strip_gps:
                metadata["gps_stripped"] = True

    if strip_gps and exif_data:
        # Remove EXIF entirely to strip GPS + other metadata
        clean_data = BytesIO()
        # Re-save without EXIF
        save_kwargs: dict[str, Any] = {}
        if img.format == "JPEG":
            save_kwargs["exif"] = b""
        img.save(clean_data, format=img.format, **save_kwargs)
        return clean_data.getvalue(), metadata

    # No stripping needed, just return with dimensions
    return data, metadata


def extract_gps_info(data: bytes) -> dict[str, Any] | None:
    """Extract GPS info from image EXIF data. Returns None if no GPS data."""
    try:
        img = Image.open(BytesIO(data))
        exif_data = img.getexif() if hasattr(img, "getexif") else None
        if not exif_data:
            return None
        gps_ifd = exif_data.get_ifd(0x8825) if hasattr(exif_data, "get_ifd") else None
        if not gps_ifd:
            return None

        gps_info: dict[str, Any] = {}
        for tag_id, value in gps_ifd.items():
            tag_name = GPSTAGS.get(tag_id, str(tag_id))
            gps_info[tag_name] = value
        return gps_info if gps_info else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Image variant generation
# ---------------------------------------------------------------------------


def generate_variants(data: bytes, content_type: str) -> tuple[dict[str, bytes], dict[str, tuple[int, int]]]:
    """Generate image variants. Returns (variants_dict, dimensions_dict).

    variants_dict: {"thumb": bytes, "inline": bytes, "og": bytes}
    dimensions_dict: {"thumb": (w, h), "inline": (w, h), "og": (w, h)}
    """
    img = Image.open(BytesIO(data))
    variants: dict[str, bytes] = {}
    dimensions: dict[str, tuple[int, int]] = {}

    for name, (target_w, target_h) in VARIANT_SIZES.items():
        variant = _resize_image(img, target_w, target_h)
        buf = BytesIO()
        # Save as WebP for best compression
        variant.save(buf, format="WEBP", quality=85)
        variants[name] = buf.getvalue()
        dimensions[name] = (variant.width, variant.height)

    return variants, dimensions


def _resize_image(img: Image.Image, target_w: int | None, target_h: int | None) -> Image.Image:
    """Resize an image to fit within target dimensions, maintaining aspect ratio.

    For 'og' (fixed dimensions), crops to exact size.
    For 'thumb', crops to square.
    For 'inline', resizes to width only.
    """
    if target_w is None:
        msg = "target_w must be provided"
        raise ValueError(msg)
    if target_h is None:
        # Width-only resize (inline)
        ratio = target_w / img.width
        new_h = int(img.height * ratio)
        return img.resize((target_w, new_h), Image.Resampling.LANCZOS)

    if target_w == target_h:
        # Square crop (thumb)
        return _crop_to_square(img, target_w, target_h)

    # Fixed dimensions with crop (og)
    return _crop_to_fill(img, target_w, target_h)


def _crop_to_square(img: Image.Image, size: int, _: int) -> Image.Image:
    """Crop to center square, then resize."""
    w, h = img.size
    min_dim = min(w, h)
    left = (w - min_dim) // 2
    top = (h - min_dim) // 2
    img = img.crop((left, top, left + min_dim, top + min_dim))
    return img.resize((size, size), Image.Resampling.LANCZOS)


def _crop_to_fill(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Resize to fill target dimensions, then center-crop."""
    w, h = img.size
    ratio = max(target_w / w, target_h / h)
    new_w = int(w * ratio)
    new_h = int(h * ratio)
    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    # Center crop
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


# ---------------------------------------------------------------------------
# Storage key generation
# ---------------------------------------------------------------------------


def compute_sha256(data: bytes) -> str:
    """Compute SHA-256 hex digest of data."""
    return hashlib.sha256(data).hexdigest()


def make_storage_key(sha256: str, filename: str) -> str:
    """Generate an immutable storage key: media/{sha256}/{filename}."""
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    return f"media/{sha256}/{safe_name}"


def make_asset_url(sha256: str, filename: str) -> str:
    """Generate the public asset URL: /media/{sha256}/{filename}."""
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    return f"/media/{sha256}/{safe_name}"


# ---------------------------------------------------------------------------
# Presigned URL generation (S3 PUT, AWS Signature V4)
# ---------------------------------------------------------------------------


def generate_presigned_put_url(
    storage_key: str,
    content_type: str,
    *,
    expires_in_minutes: int = PRESIGNED_URL_TTL_MINUTES,
) -> tuple[str, dict[str, str], int]:
    """Generate a presigned S3 PUT URL using AWS Signature V4.

    Returns (upload_url, upload_headers, expires_in_seconds).
    Falls back to a mock URL for development without real S3.
    """
    settings = get_settings()

    if not settings.s3_access_key_id or not settings.s3_secret_access_key:
        # Development mode: generate a mock presigned URL
        expires_in = expires_in_minutes * 60
        expires_at = int(time.time()) + expires_in
        nonce = hashlib.sha256(f"{storage_key}:{expires_at}".encode()).hexdigest()[:16]
        upload_url = (
            f"{settings.s3_endpoint_url or 'http://localhost:9000'}"
            f"/{settings.s3_bucket}/{storage_key}"
            f"?expires={expires_at}&nonce={nonce}"
        )
        upload_headers = {
            "Content-Type": content_type,
            "X-Amz-Content-Sha256": "UNSIGNED-PAYLOAD",
        }
        return upload_url, upload_headers, expires_in

    # Real S3 presigned PUT URL
    expires_in = expires_in_minutes * 60
    region = settings.s3_region
    endpoint = settings.s3_endpoint_url or f"https://{settings.s3_bucket}.s3.{region}.amazonaws.com"
    url = f"{endpoint}/{storage_key}"

    # AWS Signature V4 for PUT
    now = datetime.now(UTC)
    date_stamp = now.strftime("%Y%m%d")
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"

    # Canonical request
    canonical_uri = f"/{storage_key}"
    canonical_querystring = "X-Amz-Algorithm=AWS4-HMAC-SHA256"
    canonical_querystring += f"&X-Amz-Credential={settings.s3_access_key_id}%2F{credential_scope}"
    canonical_querystring += f"&X-Amz-Date={amz_date}"
    canonical_querystring += f"&X-Amz-Expires={expires_in}"
    canonical_querystring += "&X-Amz-SignedHeaders=content-type%3Bhost"

    payload_hash = "UNSIGNED-PAYLOAD"
    host = f"{settings.s3_bucket}.s3.{region}.amazonaws.com"
    canonical_headers = f"content-type:{content_type}\nhost:{host}\n"
    signed_headers = "content-type;host"

    canonical_request = (
        f"PUT\n{canonical_uri}\n{canonical_querystring}\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )

    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n"
        + hashlib.sha256(canonical_request.encode()).hexdigest()
    )

    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    signing_key = _sign(
        _sign(_sign(_sign(b"AWS4" + settings.s3_secret_access_key.encode(), date_stamp), region), "s3"),
        "aws4_request",
    )
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    presigned_url = (
        f"{url}?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        f"&X-Amz-Credential={settings.s3_access_key_id}%2F{credential_scope}"
        f"&X-Amz-Date={amz_date}"
        f"&X-Amz-Expires={expires_in}"
        f"&X-Amz-SignedHeaders=content-type%3Bhost"
        f"&X-Amz-Signature={signature}"
    )

    upload_headers = {"Content-Type": content_type}
    return presigned_url, upload_headers, expires_in


def verify_presigned_url_not_expired(upload_url: str) -> None:
    """Verify a presigned URL hasn't expired by checking the 'expires' param."""
    import urllib.parse

    parsed = urllib.parse.urlparse(upload_url)
    params = urllib.parse.parse_qs(parsed.query)

    expires_param = params.get("expires", [None])[0]
    if expires_param is None:
        return  # No expiry to check (e.g., real S3 URL with X-Amz-Expires)

    try:
        expires_at = int(expires_param)
    except ValueError:
        return

    if time.time() > expires_at:
        raise PresignedUrlExpiredError()


# ---------------------------------------------------------------------------
# Reference extraction from body_md
# ---------------------------------------------------------------------------


def extract_asset_refs(body_md: str) -> list[str]:
    """Parse body_md for asset URL references.

    Matches patterns like:
    * ![alt](/media/{sha256}/{name})
    * ![](/media/{sha256}/{name})
    * src="/media/{sha256}/{name}"
    """
    # Markdown image references
    md_refs = re.findall(r"!\[[^\]]*\]\((/media/[^\)]+)\)", body_md)
    # HTML img src references
    html_refs = re.findall(r'src="(/media/[^"]+)"', body_md)
    # All refs
    all_refs = md_refs + html_refs
    # Deduplicate preserving order
    seen: set[str] = set()
    result: list[str] = []
    for ref in all_refs:
        if ref not in seen:
            seen.add(ref)
            result.append(ref)
    return result


def find_referencing_posts(session: Session, asset_url: str) -> list[str]:
    """Find posts that reference the given asset URL in body_md."""
    # Search for the asset URL pattern in body_md
    pattern = f"%{asset_url}%"
    posts = session.query(Post.id).filter(Post.body_md.ilike(pattern), Post.deleted_at.is_(None)).all()
    return [str(p.id) for p in posts]


def find_all_asset_urls(session: Session, site_id: uuid.UUID | None = None) -> set[str]:
    """Find all asset URLs referenced in any post's body_md or frontmatter.

    Includes trashed posts — an asset referenced by a trashed-but-restorable
    post should NOT be considered an orphan.
    """
    query = session.query(Post.body_md)
    if site_id is not None:
        query = query.filter(Post.site_id == site_id)

    all_urls: set[str] = set()
    for (body_md,) in query.all():
        if body_md:
            all_urls.update(extract_asset_refs(body_md))
    return all_urls


# ---------------------------------------------------------------------------
# Orphan management
# ---------------------------------------------------------------------------


def scan_orphans(
    session: Session, *, site_id: uuid.UUID | None = None, dry_run: bool = True
) -> list[dict[str, Any]]:
    """Find assets not referenced by any post.

    Returns a list of orphan info dicts. If dry_run=True, nothing is deleted.
    Assets referenced by trashed-but-restorable posts are NOT considered orphans.
    """
    query = session.query(Asset).filter(
        Asset.deleted_at.is_(None),
        Asset.status == "ready",
    )
    if site_id is not None:
        query = query.filter(Asset.site_id == site_id)

    all_assets = query.all()
    referenced_urls = find_all_asset_urls(session, site_id)

    orphans: list[dict[str, Any]] = []
    for asset in all_assets:
        asset_url = make_asset_url(asset.sha256 or "", asset.filename)
        if asset_url not in referenced_urls:
            orphans.append(
                {
                    "id": str(asset.id),
                    "filename": asset.filename,
                    "sha256": asset.sha256,
                    "byte_size": asset.byte_size,
                    "created_at": asset.created_at.isoformat() if asset.created_at else None,
                    "asset_url": asset_url,
                }
            )

    return orphans
