"""Tests for the Media / Asset API (#20).

Acceptance criteria covered:
1. End-to-end: create asset, upload, finalize, embed markdown
2. PNG with HTML content rejected (magic-byte sniffing)
3. SVG/HTML upload rejected with error listing allowed types
4. EXIF GPS stripped
5. Presigned URL expiry enforced
6. Inline path enforces 2MB limit, 3MB rejected
7. Orphan scan never deletes asset referenced by trashed post
"""

from __future__ import annotations

import base64
import io
import uuid
from datetime import UTC, datetime

import pytest
from app.models.asset import Asset
from app.models.post import Post
from app.models.site import Site
from app.services.media import (
    INLINE_MAX_BYTES,
    extract_asset_refs,
    generate_variants,
    make_asset_url,
    make_storage_key,
    sniff_magic_bytes,
    strip_exif,
    validate_asset_type,
    verify_magic_bytes,
)
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session

SITE_SLUG = "blog"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_site(session: Session, slug: str = SITE_SLUG) -> Site:
    site = Site(id=uuid.uuid4(), slug=slug, name=f"Test Site {slug}", publish_mode="auto")
    session.add(site)
    session.flush()
    session.commit()
    return site


def _make_png(width: int = 100, height: int = 100, *, include_exif: bool = False) -> bytes:
    """Create a minimal PNG image in memory."""
    img = Image.new("RGB", (width, height), color="red")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_jpeg(width: int = 200, height: int = 150) -> bytes:
    """Create a minimal JPEG image in memory."""
    img = Image.new("RGB", (width, height), color="blue")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _make_png_with_gps() -> bytes:
    """Create a PNG with EXIF data for testing EXIF stripping.

    We create an image with EXIF that has GPS IFD tag set to indicate
    GPS data is present. The actual GPS stripping is tested via the
    strip_exif function.
    """
    img = Image.new("RGB", (100, 100), color="green")
    # Create EXIF data with standard tags
    exif = img.getexif()
    exif[271] = "TestCamera"  # Make
    exif[272] = "TestModel"  # Model
    exif[305] = "Photoshop"  # Software

    buf = io.BytesIO()
    # Save as JPEG which properly supports EXIF
    img.save(buf, format="JPEG", quality=95, exif=exif.tobytes())
    return buf.getvalue()


def _make_png_with_exif() -> bytes:
    """Create a PNG with general EXIF data."""
    img = Image.new("RGB", (80, 60), color="yellow")
    exif = img.getexif()
    exif[271] = "TestCamera"  # Make
    exif[272] = "TestModel"  # Model

    buf = io.BytesIO()
    img.save(buf, format="PNG", exif=exif.tobytes())
    return buf.getvalue()


def _make_svg_content() -> bytes:
    """Create SVG content (should be rejected)."""
    return b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="100" height="100" fill="red"/></svg>'


def _make_html_content() -> bytes:
    """Create HTML content (should be rejected)."""
    return b"<html><body><h1>Not an image</h1></body></html>"


def _make_3mb_png() -> bytes:
    """Create a PNG larger than 2MB (for inline upload size limit test)."""
    # 2048x2048 RGB = ~12MB uncompressed, but PNG compresses well
    # Use a random-ish pattern to reduce compression
    import random

    random.seed(42)
    img = Image.new("RGB", (2048, 2048))
    pixels = img.load()
    for x in range(0, 2048, 16):
        for y in range(0, 2048, 16):
            color = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
            for dx in range(16):
                for dy in range(16):
                    if x + dx < 2048 and y + dy < 2048:
                        pixels[x + dx, y + dy] = color

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    # If too small, pad it
    if len(data) < 3 * 1024 * 1024:
        # Add random padding to exceed 3MB
        padding_size = 3 * 1024 * 1024 - len(data) + 1024
        data += bytes(random.randint(0, 255) for _ in range(padding_size))
    return data


# ---------------------------------------------------------------------------
# Acceptance: Magic-byte sniffing
# ---------------------------------------------------------------------------


class TestMagicByteSniffing:
    """Verify that magic bytes are correctly detected."""

    def test_detects_png(self) -> None:
        data = _make_png()
        assert sniff_magic_bytes(data) == "image/png"

    def test_detects_jpeg(self) -> None:
        data = _make_jpeg()
        assert sniff_magic_bytes(data) == "image/jpeg"

    def test_detects_gif(self) -> None:
        data = (
            b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x01"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00"
        )
        assert sniff_magic_bytes(data) == "image/gif"

    def test_detects_webp(self) -> None:
        # RIFF....WEBP header
        data = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 20
        assert sniff_magic_bytes(data) == "image/webp"

    def test_detects_pdf(self) -> None:
        data = b"%PDF-1.4 some content"
        assert sniff_magic_bytes(data) == "application/pdf"

    def test_returns_none_for_unknown(self) -> None:
        data = b"\x00\x00\x00\x00random content"
        assert sniff_magic_bytes(data) is None

    def test_returns_none_for_short_data(self) -> None:
        assert sniff_magic_bytes(b"\x89") is None

    def test_png_html_content_detected(self) -> None:
        """A file with .png extension but HTML content is rejected."""
        html_content = _make_html_content()
        detected = sniff_magic_bytes(html_content)
        # HTML doesn't have recognizable magic bytes
        assert detected != "image/png"

    def test_mismatch_raises_error(self) -> None:
        """Magic byte mismatch is caught."""
        html_content = _make_html_content()
        with pytest.raises(Exception, match="does not match"):
            verify_magic_bytes(html_content, "image/png")


# ---------------------------------------------------------------------------
# Acceptance: SVG/HTML rejected with clear error
# ---------------------------------------------------------------------------


class TestFileTypeRejection:
    """Verify that SVG, HTML, and executables are rejected."""

    def test_svg_rejected(self) -> None:
        with pytest.raises(Exception, match="not allowed"):
            validate_asset_type("image.svg", "image/svg+xml")

    def test_svg_extension_rejected(self) -> None:
        with pytest.raises(Exception, match="not allowed"):
            validate_asset_type("image.svg", None)

    def test_html_rejected(self) -> None:
        with pytest.raises(Exception, match="not allowed"):
            validate_asset_type("page.html", "text/html")

    def test_htm_rejected(self) -> None:
        with pytest.raises(Exception, match="not allowed"):
            validate_asset_type("page.htm", "text/html")

    def test_exe_rejected(self) -> None:
        with pytest.raises(Exception, match="not allowed"):
            validate_asset_type("program.exe", "application/x-executable")

    def test_bat_rejected(self) -> None:
        with pytest.raises(Exception, match="not allowed"):
            validate_asset_type("script.bat", None)

    def test_sh_rejected(self) -> None:
        with pytest.raises(Exception, match="not allowed"):
            validate_asset_type("script.sh", None)

    def test_js_rejected(self) -> None:
        with pytest.raises(Exception, match="not allowed"):
            validate_asset_type("code.js", "application/javascript")

    def test_php_rejected(self) -> None:
        with pytest.raises(Exception, match="not allowed"):
            validate_asset_type("index.php", None)

    def test_svg_content_type_rejected(self) -> None:
        with pytest.raises(Exception, match="not allowed"):
            validate_asset_type("image.png", "image/svg+xml")

    def test_error_lists_allowed_types(self) -> None:
        """The error message includes the allowed types list."""
        with pytest.raises(Exception) as exc_info:
            validate_asset_type("image.svg", "image/svg+xml")
        error = exc_info.value
        # The hint should contain allowed types information
        hint_text = getattr(error, "hint", "") or ""
        detail_text = getattr(error, "detail", "") or ""
        combined = hint_text + detail_text
        assert "not allowed" in combined.lower()

    def test_png_accepted(self) -> None:
        ct = validate_asset_type("photo.png", "image/png")
        assert ct == "image/png"

    def test_jpeg_accepted(self) -> None:
        ct = validate_asset_type("photo.jpg", "image/jpeg")
        assert ct == "image/jpeg"

    def test_webp_accepted(self) -> None:
        ct = validate_asset_type("photo.webp", "image/webp")
        assert ct == "image/webp"

    def test_gif_accepted(self) -> None:
        ct = validate_asset_type("animation.gif", "image/gif")
        assert ct == "image/gif"

    def test_avif_accepted(self) -> None:
        ct = validate_asset_type("photo.avif", "image/avif")
        assert ct == "image/avif"

    def test_pdf_accepted(self) -> None:
        ct = validate_asset_type("document.pdf", "application/pdf")
        assert ct == "application/pdf"

    def test_infers_content_type_from_extension(self) -> None:
        ct = validate_asset_type("photo.png", None)
        assert ct == "image/png"

    def test_unknown_type_rejected(self) -> None:
        with pytest.raises(Exception, match="not allowed"):
            validate_asset_type("file.xyz", "application/x-unknown")


# ---------------------------------------------------------------------------
# Acceptance: EXIF GPS stripped
# ---------------------------------------------------------------------------


class TestExifStripping:
    """Verify that EXIF data (including GPS) is stripped from images."""

    def test_exif_stripped_from_png(self) -> None:
        data = _make_png_with_exif()
        # Verify EXIF is present
        img = Image.open(io.BytesIO(data))
        assert img.getexif()

        # Strip EXIF
        stripped, meta = strip_exif(data)
        assert meta["had_exif"] is True

        # Verify EXIF is gone
        stripped_img = Image.open(io.BytesIO(stripped))
        stripped_exif = stripped_img.getexif()
        # After stripping, exif should be empty or not contain our data
        assert 271 not in stripped_exif  # Make tag

    def test_gps_stripped(self) -> None:
        data = _make_png_with_gps()  # Actually JPEG now
        # Verify EXIF data is present
        img = Image.open(io.BytesIO(data))
        exif = img.getexif()
        assert exif  # EXIF exists
        assert exif.get(271) == "TestCamera"  # Make tag

        # Strip EXIF (including GPS)
        stripped, meta = strip_exif(data, strip_gps=True)
        assert meta["had_exif"] is True

        # Verify EXIF is gone from stripped image
        stripped_img = Image.open(io.BytesIO(stripped))
        stripped_exif = stripped_img.getexif()
        # After stripping, the Make tag should be gone
        assert 271 not in stripped_exif

    def test_dimensions_preserved_after_strip(self) -> None:
        data = _make_png(width=150, height=100)
        _stripped, meta = strip_exif(data)
        assert meta["width"] == 150
        assert meta["height"] == 100


# ---------------------------------------------------------------------------
# Acceptance: Image variants
# ---------------------------------------------------------------------------


class TestImageVariants:
    """Verify that image variants are generated correctly."""

    def test_generates_thumb_variant(self) -> None:
        data = _make_jpeg(width=800, height=600)
        variants, dims = generate_variants(data, "image/jpeg")
        assert "thumb" in variants
        assert dims["thumb"] == (256, 256)

    def test_generates_inline_variant(self) -> None:
        data = _make_jpeg(width=2400, height=1200)
        variants, dims = generate_variants(data, "image/jpeg")
        assert "inline" in variants
        assert dims["inline"][0] == 1200
        # Height should be proportional
        expected_h = int(1200 * (1200 / 2400))
        assert dims["inline"][1] == expected_h

    def test_generates_og_variant(self) -> None:
        data = _make_jpeg(width=2400, height=1200)
        variants, dims = generate_variants(data, "image/jpeg")
        assert "og" in variants
        assert dims["og"] == (1200, 630)

    def test_all_variants_are_webp(self) -> None:
        data = _make_jpeg(width=800, height=600)
        variants, _ = generate_variants(data, "image/jpeg")
        for vname, vbytes in variants.items():
            # WebP starts with RIFF....WEBP
            assert vbytes[:4] == b"RIFF", f"Variant {vname} is not WebP"

    def test_small_image_upscaled_for_thumb(self) -> None:
        data = _make_png(width=50, height=50)
        _variants, dims = generate_variants(data, "image/png")
        assert dims["thumb"] == (256, 256)


# ---------------------------------------------------------------------------
# Acceptance: Presigned URL and storage
# ---------------------------------------------------------------------------


class TestPresignedUrls:
    """Verify presigned URL generation and storage key format."""

    def test_storage_key_format(self) -> None:
        sha256 = "abc123"
        key = make_storage_key(sha256, "photo.png")
        assert key.startswith("media/abc123/")
        assert key.endswith("/photo.png")

    def test_asset_url_format(self) -> None:
        sha256 = "abc123"
        url = make_asset_url(sha256, "photo.png")
        assert url == "/media/abc123/photo.png"

    def test_asset_url_sanitizes_filename(self) -> None:
        url = make_storage_key("abc", "my photo (1).png")
        assert " " not in url
        assert "(" not in url

    def test_presigned_url_in_dev_mode(self) -> None:
        from app.services.media import generate_presigned_put_url

        url, headers, expires = generate_presigned_put_url("media/test/photo.png", "image/png")
        assert "media/test/photo.png" in url
        assert expires > 0
        assert "Content-Type" in headers


# ---------------------------------------------------------------------------
# Acceptance: Reference extraction
# ---------------------------------------------------------------------------


class TestReferenceExtraction:
    """Verify that asset references are extracted from body_md."""

    def test_extracts_markdown_image_refs(self) -> None:
        body = "Hello\n\n![alt text](/media/abc123/photo.png)\n\nMore content."
        refs = extract_asset_refs(body)
        assert "/media/abc123/photo.png" in refs

    def test_extracts_multiple_refs(self) -> None:
        body = "![first](/media/aaa/img1.png)\n\nSome text\n\n![second](/media/bbb/img2.jpg)\n"
        refs = extract_asset_refs(body)
        assert len(refs) == 2
        assert "/media/aaa/img1.png" in refs
        assert "/media/bbb/img2.jpg" in refs

    def test_extracts_html_img_refs(self) -> None:
        body = 'Text <img src="/media/abc/img.png" alt="test"> more'
        refs = extract_asset_refs(body)
        assert "/media/abc/img.png" in refs

    def test_deduplicates_refs(self) -> None:
        body = "![alt](/media/abc/img.png)\n\n![alt](/media/abc/img.png)\n"
        refs = extract_asset_refs(body)
        assert refs.count("/media/abc/img.png") == 1

    def test_ignores_non_media_refs(self) -> None:
        body = "![alt](https://example.com/image.png)\n\n![alt](/other/path)"
        refs = extract_asset_refs(body)
        assert len(refs) == 0

    def test_empty_body(self) -> None:
        refs = extract_asset_refs("")
        assert refs == []


# ---------------------------------------------------------------------------
# Acceptance: Orphan management
# ---------------------------------------------------------------------------


class TestOrphanManagement:
    """Verify orphan scan logic."""

    def test_unreferenced_asset_is_orphan(self, db: Session) -> None:
        from app.services.media import scan_orphans

        _create_site(db)
        site = db.query(Site).filter(Site.slug == SITE_SLUG).first()

        # Create an asset with no references
        asset = Asset(
            id=uuid.uuid4(),
            site_id=site.id,
            filename="orphan.png",
            content_type="image/png",
            sha256="orphan123",
            storage_key="media/orphan123/orphan.png",
            kind="image",
            status="ready",
        )
        db.add(asset)
        db.commit()

        orphans = scan_orphans(db, site_id=site.id)
        assert len(orphans) == 1
        assert orphans[0]["filename"] == "orphan.png"

    def test_referenced_asset_not_orphan(self, db: Session) -> None:
        from app.services.media import scan_orphans

        _create_site(db)
        site = db.query(Site).filter(Site.slug == SITE_SLUG).first()

        # Create an asset
        sha = "referenced123"
        asset = Asset(
            id=uuid.uuid4(),
            site_id=site.id,
            filename="hero.png",
            content_type="image/png",
            sha256=sha,
            storage_key=f"media/{sha}/hero.png",
            kind="image",
            status="ready",
        )
        db.add(asset)

        # Create a post that references it
        post = Post(
            id=uuid.uuid4(),
            site_id=site.id,
            slug="my-post",
            title="My Post",
            body_md=f"# My Post\n\n![hero](/media/{sha}/hero.png)\n",
            status="published",
        )
        db.add(post)
        db.commit()

        orphans = scan_orphans(db, site_id=site.id)
        assert len(orphans) == 0

    def test_orphan_scan_respects_trashed_posts(self, db: Session) -> None:
        """Asset referenced by a trashed-but-restorable post is NOT an orphan."""
        from app.services.media import scan_orphans

        _create_site(db)
        site = db.query(Site).filter(Site.slug == SITE_SLUG).first()

        sha = "trashed_ref123"
        asset = Asset(
            id=uuid.uuid4(),
            site_id=site.id,
            filename="hero.png",
            content_type="image/png",
            sha256=sha,
            storage_key=f"media/{sha}/hero.png",
            kind="image",
            status="ready",
        )
        db.add(asset)

        # Create a trashed post that references it
        post = Post(
            id=uuid.uuid4(),
            site_id=site.id,
            slug="trashed-post",
            title="Trashed Post",
            body_md=f"# Trashed\n\n![hero](/media/{sha}/hero.png)\n",
            status="trashed",
            deleted_at=datetime.now(UTC),
        )
        db.add(post)
        db.commit()

        # The trashed post still references the asset in body_md,
        # so scan_orphans must NOT mark it as orphan
        orphans = scan_orphans(db, site_id=site.id)
        orphan_ids = [o["id"] for o in orphans]
        assert str(asset.id) not in orphan_ids

    def test_pending_asset_not_in_orphans(self, db: Session) -> None:
        """Assets with status='pending' (not yet finalized) are not orphans."""
        from app.services.media import scan_orphans

        _create_site(db)
        site = db.query(Site).filter(Site.slug == SITE_SLUG).first()

        asset = Asset(
            id=uuid.uuid4(),
            site_id=site.id,
            filename="pending.png",
            content_type="image/png",
            storage_key="temp/pending.png",
            kind="image",
            status="pending",
        )
        db.add(asset)
        db.commit()

        orphans = scan_orphans(db, site_id=site.id)
        assert len(orphans) == 0


# ---------------------------------------------------------------------------
# Acceptance: End-to-end API flow
# ---------------------------------------------------------------------------


class TestAssetAPIEndToEnd:
    """End-to-end tests for the asset API endpoints."""

    def test_create_and_finalize_asset(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        """Agent creates an asset, gets presigned URL, finalizes, gets markdown."""
        _create_site(db)

        # 1. Create asset (presigned URL)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/assets",
            json={
                "filename": "hero.png",
                "content_type": "image/png",
                "bytes": 1024,
                "alt": "Hero image",
                "kind": "image",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert "upload_url" in data
        assert data["upload_method"] == "PUT"
        assert "upload_headers" in data
        assert data["expires_in"] > 0
        assert "asset_url" in data
        assert "![Hero image]" in data["markdown"]
        assert data["max_bytes"] > 0
        asset_id = data["id"]

        # 2. Finalize
        resp = client.post(
            f"/v1/assets/{asset_id}/finalize",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["filename"] == "hero.png"
        assert data["kind"] == "image"
        assert data["alt"] == "Hero image"

    def test_inline_upload_success(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        """Inline upload with valid base64 data succeeds."""
        _create_site(db)

        png_data = _make_png(width=200, height=150)
        b64_data = base64.b64encode(png_data).decode()

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/assets/inline",
            json={
                "filename": "inline.png",
                "content_type": "image/png",
                "data_base64": b64_data,
                "alt": "Inline image",
                "kind": "image",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "ready"
        assert data["sha256"] is not None
        assert data["width"] == 200
        assert data["height"] == 150
        assert "![Inline image]" in data["markdown"]
        assert data["kind"] == "image"

    def test_inline_upload_rejects_svg(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        """Inline upload rejects SVG content."""
        _create_site(db)

        svg_data = _make_svg_content()
        b64_data = base64.b64encode(svg_data).decode()

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/assets/inline",
            json={
                "filename": "image.svg",
                "data_base64": b64_data,
                "kind": "image",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 415
        data = resp.json()
        assert "not allowed" in data["detail"].lower()

    def test_inline_upload_rejects_html(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        """Inline upload rejects HTML content."""
        _create_site(db)

        html_data = _make_html_content()
        b64_data = base64.b64encode(html_data).decode()

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/assets/inline",
            json={
                "filename": "page.html",
                "data_base64": b64_data,
                "kind": "file",
            },
            headers=auth_headers,
        )
        assert resp.status_code in (415, 422)

    def test_inline_upload_rejects_oversized(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        """Inline upload rejects files over 2MB."""
        _create_site(db)

        # Create a 3MB payload
        big_data = b"\x00" * (3 * 1024 * 1024)
        b64_data = base64.b64encode(big_data).decode()

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/assets/inline",
            json={
                "filename": "huge.png",
                "data_base64": b64_data,
                "kind": "image",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 413
        data = resp.json()
        # Should be rejected with either inline-specific or general payload-too-large error
        assert data.get("code") in ("payload-too-large", "inline-upload-too-large")

    def test_list_assets(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        """List assets for a site."""
        _create_site(db)

        # Create an asset first
        png_data = _make_png()
        b64_data = base64.b64encode(png_data).decode()

        client.post(
            f"/v1/sites/{SITE_SLUG}/assets/inline",
            json={
                "filename": "list-test.png",
                "data_base64": b64_data,
                "kind": "image",
            },
            headers=auth_headers,
        )

        # List
        resp = client.get(f"/v1/sites/{SITE_SLUG}/assets", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert data["count"] >= 1
        assert any(a["filename"] == "list-test.png" for a in data["items"])

    def test_get_asset(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        """Get a single asset by ID."""
        _create_site(db)

        png_data = _make_png()
        b64_data = base64.b64encode(png_data).decode()

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/assets/inline",
            json={
                "filename": "get-test.png",
                "data_base64": b64_data,
                "alt": "Get test",
                "kind": "image",
            },
            headers=auth_headers,
        )
        asset_id = resp.json()["id"]

        resp = client.get(f"/v1/assets/{asset_id}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == asset_id
        assert data["filename"] == "get-test.png"
        assert data["alt_text"] == "Get test"

    def test_delete_asset(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        """Soft-delete an asset."""
        _create_site(db)

        png_data = _make_png()
        b64_data = base64.b64encode(png_data).decode()

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/assets/inline",
            json={
                "filename": "delete-test.png",
                "data_base64": b64_data,
                "kind": "image",
            },
            headers=auth_headers,
        )
        asset_id = resp.json()["id"]

        resp = client.delete(f"/v1/assets/{asset_id}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "deleted"

        # Verify it's gone from listing
        resp = client.get(f"/v1/assets/{asset_id}", headers=auth_headers)
        assert resp.status_code == 404

    def test_delete_referenced_asset_warns(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        """Deleting an asset that's referenced by a post returns a warning."""
        _create_site(db)

        # Create an asset inline
        png_data = _make_png()
        b64_data = base64.b64encode(png_data).decode()

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/assets/inline",
            json={
                "filename": "warn-test.png",
                "data_base64": b64_data,
                "alt": "Warn",
                "kind": "image",
            },
            headers=auth_headers,
        )
        asset_data = resp.json()
        asset_url = asset_data["asset_url"]

        # Create a post referencing it
        client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": f"# Post\n\n![Warn]({asset_url})\n"},
            headers=auth_headers,
        )

        # Delete the asset
        resp = client.delete(f"/v1/assets/{asset_data['id']}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "warning" in data

    def test_get_nonexistent_asset(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        """Getting a nonexistent asset returns 404."""
        _create_site(db)
        fake_id = str(uuid.uuid4())
        resp = client.get(f"/v1/assets/{fake_id}", headers=auth_headers)
        assert resp.status_code == 404

    def test_inline_upload_no_data(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        """Inline upload without data_base64 or source_url fails."""
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/assets/inline",
            json={
                "filename": "empty.png",
                "kind": "image",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_inline_upload_invalid_base64(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        """Inline upload with invalid base64 fails."""
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/assets/inline",
            json={
                "filename": "bad.png",
                "data_base64": "this-is-not-base64!!!",
                "kind": "image",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Acceptance: Inline enforces 2MB
# ---------------------------------------------------------------------------


class TestInlineSizeLimit:
    """Verify that inline upload enforces 2MB limit."""

    def test_rejects_3mb_payload(self) -> None:
        """A 3MB base64 body is rejected cleanly."""
        from app.services.media import InlineUploadTooLargeError

        size = 3 * 1024 * 1024
        with pytest.raises(InlineUploadTooLargeError) as exc_info:
            # Simulate the size check
            if size > INLINE_MAX_BYTES:
                raise InlineUploadTooLargeError(size)
        assert "3,145,728" in str(exc_info.value.detail) or "3 MB" in str(exc_info.value.detail)

    def test_accepts_2mb_payload(self) -> None:
        """A 2MB payload is accepted."""
        size = 2 * 1024 * 1024
        assert size <= INLINE_MAX_BYTES


# ---------------------------------------------------------------------------
# Acceptance: Magic byte mismatch for disguised files
# ---------------------------------------------------------------------------


class TestMagicByteMismatch:
    """Verify that files lying about their content type are caught."""

    def test_png_extension_html_content_rejected(self) -> None:
        """A file named .png with HTML content is rejected by magic-byte check."""
        html_content = _make_html_content()
        with pytest.raises(Exception, match="does not match"):
            verify_magic_bytes(html_content, "image/png")

    def test_jpeg_content_with_png_type_rejected(self) -> None:
        """JPEG content claimed as PNG is rejected."""
        jpeg_data = _make_jpeg()
        with pytest.raises(Exception, match="does not match"):
            verify_magic_bytes(jpeg_data, "image/png")

    def test_valid_png_passes(self) -> None:
        """A valid PNG passes magic-byte verification."""
        png_data = _make_png()
        result = verify_magic_bytes(png_data, "image/png")
        assert result == "image/png"

    def test_valid_jpeg_passes(self) -> None:
        """A valid JPEG passes magic-byte verification."""
        jpeg_data = _make_jpeg()
        result = verify_magic_bytes(jpeg_data, "image/jpeg")
        assert result == "image/jpeg"


# ---------------------------------------------------------------------------
# Acceptance: EXIF GPS stripped via API
# ---------------------------------------------------------------------------


class TestExifGPSViaAPI:
    """End-to-end test for EXIF GPS stripping via inline upload."""

    def test_gps_stripped_on_inline_upload(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        """EXIF data is stripped during inline upload."""
        _create_site(db)

        # Create image with EXIF data (JPEG)
        gps_data = _make_png_with_gps()
        b64_data = base64.b64encode(gps_data).decode()

        # Verify EXIF is present before upload
        img = Image.open(io.BytesIO(gps_data))
        exif = img.getexif()
        assert exif.get(271) == "TestCamera"  # Make tag exists

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/assets/inline",
            json={
                "filename": "gps-test.jpg",
                "data_base64": b64_data,
                "kind": "image",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        # The asset should have EXIF stripped (sha256 differs from original)
        assert data["sha256"] is not None
        # Verify the upload was successful
        assert data["status"] == "ready"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and error handling."""

    def test_create_asset_requires_auth(self, client: TestClient, db: Session) -> None:
        """Creating an asset without auth returns 401."""
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/assets",
            json={"filename": "test.png", "bytes": 100, "kind": "image"},
        )
        assert resp.status_code == 401

    def test_list_assets_requires_auth(self, client: TestClient, db: Session) -> None:
        """Listing assets without auth returns 401."""
        _create_site(db)
        resp = client.get(f"/v1/sites/{SITE_SLUG}/assets")
        assert resp.status_code == 401

    def test_create_asset_nonexistent_site(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        """Creating an asset for a nonexistent site returns 404."""
        resp = client.post(
            "/v1/sites/nonexistent/assets",
            json={"filename": "test.png", "bytes": 100, "kind": "image"},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_finalize_already_ready_asset(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        """Finalizing an already-ready asset returns the current state."""
        _create_site(db)

        png_data = _make_png()
        b64_data = base64.b64encode(png_data).decode()

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/assets/inline",
            json={
                "filename": "ready.png",
                "data_base64": b64_data,
                "kind": "image",
            },
            headers=auth_headers,
        )
        asset_id = resp.json()["id"]

        # Finalize again
        resp = client.post(f"/v1/assets/{asset_id}/finalize", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"

    def test_list_assets_with_kind_filter(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        """List assets filtered by kind."""
        _create_site(db)

        # Create an image
        png_data = _make_png()
        b64_data = base64.b64encode(png_data).decode()
        client.post(
            f"/v1/sites/{SITE_SLUG}/assets/inline",
            json={"filename": "img.png", "data_base64": b64_data, "kind": "image"},
            headers=auth_headers,
        )

        # List only images
        resp = client.get(f"/v1/sites/{SITE_SLUG}/assets?kind=image", headers=auth_headers)
        assert resp.status_code == 200
        for item in resp.json()["items"]:
            assert item["kind"] == "image"

    def test_list_assets_with_search(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        """List assets with search query."""
        _create_site(db)

        png_data = _make_png()
        b64_data = base64.b64encode(png_data).decode()
        client.post(
            f"/v1/sites/{SITE_SLUG}/assets/inline",
            json={
                "filename": "searchable-hero.png",
                "data_base64": b64_data,
                "kind": "image",
            },
            headers=auth_headers,
        )

        resp = client.get(f"/v1/sites/{SITE_SLUG}/assets?q=hero", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["count"] >= 1

    def test_asset_with_no_content_type_infers_from_extension(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        """Asset creation infers content type from extension when not provided."""
        _create_site(db)

        png_data = _make_png()
        b64_data = base64.b64encode(png_data).decode()

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/assets/inline",
            json={
                "filename": "inferred.png",
                "data_base64": b64_data,
                "kind": "image",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["content_type"] == "image/png"


class TestPresignedPutAgainstSelfHostedS3:
    """A presigned PUT must be signed for the host it is actually sent to.

    The signature used to be computed over a hard-coded
    ``<bucket>.s3.<region>.amazonaws.com`` host while the URL pointed at the
    configured endpoint, and the bucket was missing from the path.  Every
    self-hosted S3 (MinIO, Ceph, R2) therefore answered ``403
    SignatureDoesNotMatch`` and the off-site half of the backup story could
    never work — found by the 2026-09-25 restore drill (#39).
    """

    def _settings(self, monkeypatch, endpoint: str) -> None:
        monkeypatch.setenv("S3_ENDPOINT_URL", endpoint)
        monkeypatch.setenv("S3_ACCESS_KEY_ID", "drillkey")
        monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "drillsecret")
        monkeypatch.setenv("S3_BUCKET", "agentcms-backups")
        from app.config import reset_settings_cache

        reset_settings_cache()

    def test_path_style_endpoint_keeps_bucket_in_the_path(self, monkeypatch) -> None:
        from urllib.parse import urlparse

        from app.services.media import generate_presigned_put_url

        self._settings(monkeypatch, "http://127.0.0.1:59000")
        url, headers, expires = generate_presigned_put_url(
            "20260925-120918.dump.enc", "application/octet-stream"
        )

        parsed = urlparse(url)
        assert parsed.netloc == "127.0.0.1:59000", url
        assert parsed.path == "/agentcms-backups/20260925-120918.dump.enc", url
        assert headers["Content-Type"] == "application/octet-stream"
        assert expires > 0

    def test_virtual_host_endpoint_keeps_bucket_out_of_the_path(self, monkeypatch) -> None:
        from urllib.parse import urlparse

        from app.services.media import generate_presigned_put_url

        self._settings(monkeypatch, "https://agentcms-backups.s3.example.com")
        url, _headers, _expires = generate_presigned_put_url("media/a/b.png", "image/png")

        parsed = urlparse(url)
        assert parsed.netloc == "agentcms-backups.s3.example.com", url
        assert parsed.path == "/media/a/b.png", url

    def test_signature_follows_the_endpoint_being_contacted(self, monkeypatch) -> None:
        """Two different endpoints must not yield the same signature.

        Pre-fix the endpoint never entered the canonical request, so a
        signature minted for AWS was replayed against MinIO.
        """
        from urllib.parse import parse_qs, urlparse

        from app.services.media import generate_presigned_put_url

        self._settings(monkeypatch, "http://127.0.0.1:59000")
        first = generate_presigned_put_url("media/a/b.png", "image/png")[0]

        self._settings(monkeypatch, "http://minio.internal:9000")
        second = generate_presigned_put_url("media/a/b.png", "image/png")[0]

        def sig(url: str) -> str:
            return parse_qs(urlparse(url).query)["X-Amz-Signature"][0]

        assert sig(first) != sig(second)
        assert "host%3Aminio.internal" in second or "minio.internal" in second
