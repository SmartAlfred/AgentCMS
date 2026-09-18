"""Static export tests (ticket #23).

Covers every acceptance criterion:
- Layout: posts/{slug}/index.{html,md,json}, index.html, feeds, manifest
- Parity: exported post/index/tag HTML matches the public API byte-for-byte
- Determinism: two runs are byte-identical, archives included
- Incremental / since / verify / round-trip after revert
- No private/loopback host leaks into the export
- API: GET /v1/sites/{site}/export with tar.gz|zip, since, 400/401/403/404
- CLI: python -m scripts.cli export + standalone verify
- Deploy helper: plan_github_pages_deploy / validate_repo
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from app.models import Post, PostTag, Site, Tag
from app.models.actor import Actor
from app.models.capability_link import CapabilityLink
from app.services.export import (
    ExportBaseUrlPrivateError,
    ExportBaseUrlRequiredError,
    ExportError,
    archive_bytes_for_export,
    export_site,
    format_iso,
    is_private_host,
    plan_github_pages_deploy,
    read_manifest,
    validate_repo,
    verify_export,
)
from app.services.tokens import generate_token
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

SITE_SLUG = "blog"
PUBLIC_BASE = "https://blog.example.com"


# ---------------------------------------------------------------------------
# Seed helpers (direct ORM writes keep 100-post benchmarks fast)
# ---------------------------------------------------------------------------


def _create_site(session: Session, slug: str = SITE_SLUG, *, base_url: str | None = None) -> Site:
    site = Site(
        id=uuid.uuid4(),
        slug=slug,
        name=f"Test Site {slug}",
        publish_mode="auto",
        base_url=base_url,
    )
    session.add(site)
    session.commit()
    return site


def _seed_post(
    session: Session,
    site: Site,
    *,
    slug: str,
    title: str = "Test Post",
    body_md: str = "# Test Post\n\nHello world.\n",
    tags: list[str] | None = None,
    updated_at: datetime | None = None,
) -> Post:
    post = Post(
        id=uuid.uuid4(),
        site_id=site.id,
        slug=slug,
        title=title,
        body_md=body_md,
        excerpt=body_md.splitlines()[0] if body_md.strip() else "",
        status="published",
        published_at=updated_at or datetime(2026, 9, 1, 12, 0),
        updated_at=updated_at or datetime(2026, 9, 1, 12, 0),
        content_hash=hashlib.sha256(body_md.encode("utf-8")).hexdigest(),
        word_count=len(body_md.split()),
        reading_time_minutes=max(1, len(body_md.split()) // 200),
    )
    session.add(post)
    session.flush()
    for tag_slug in tags or []:
        tag = session.query(Tag).filter(Tag.slug == tag_slug).first()
        if tag is None:
            tag = Tag(id=uuid.uuid4(), slug=tag_slug, name=tag_slug)
            session.add(tag)
            session.flush()
        session.add(PostTag(post_id=post.id, tag_id=tag.id))
    session.commit()
    return post


def _make_auth_headers(session: Session, scopes: list[str]) -> dict[str, str]:
    """Create a token with the given scopes and return Authorization headers."""
    actor = Actor(id=uuid.uuid4(), kind="machine", label="typed-token", scopes=scopes)
    session.add(actor)
    session.flush()
    plaintext, token_hash = generate_token(actor.id)
    link = CapabilityLink(
        id=uuid.uuid4(),
        actor_id=actor.id,
        token_hash=token_hash,
        label="typed-token",
        path_scope="/",
        verbs=["GET"],
    )
    session.add(link)
    session.commit()
    return {"Authorization": f"Bearer {plaintext}"}


def _tree_digest(out_dir: Path) -> str:
    """Recursive SHA-256 over every file's relative path + bytes (sorted)."""
    digest = hashlib.sha256()
    files = sorted(p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file())
    for rel in files:
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        digest.update((out_dir / rel).read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


def _tar_names(content: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tf:
        return tf.getnames()


def _zip_names(content: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return zf.namelist()


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


class TestLayout:
    def test_full_tree_layout(self, db: Session, tmp_path: Path) -> None:
        site = _create_site(db, base_url=PUBLIC_BASE)
        _seed_post(db, site, slug="hello-world", tags=["news"])
        out = tmp_path / "dist"
        export_site(db, SITE_SLUG, out)

        expected = {
            "index.html",
            "posts/hello-world/index.html",
            "posts/hello-world/index.md",
            "posts/hello-world/index.json",
            "tags/news/index.html",
            "rss.xml",
            "atom.xml",
            "feed.json",
            "sitemap.xml",
            "robots.txt",
            "404.html",
            "manifest.json",
            ".nojekyll",
        }
        present = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
        assert expected <= present

        manifest = read_manifest(out)
        assert manifest is not None
        assert manifest["schema"] == "agentcms-export/v1"
        assert manifest["site"]["slug"] == SITE_SLUG
        assert manifest["site"]["base_url"] == PUBLIC_BASE
        assert manifest["stats"]["posts"] == 1
        assert "hello-world" in manifest["content"]
        assert manifest["content"]["hello-world"]["files"] == [
            "posts/hello-world/index.html",
            "posts/hello-world/index.md",
            "posts/hello-world/index.json",
        ]
        assert "posts/hello-world/index.html" in manifest["files"]
        assert "manifest.json" not in manifest["files"]

        # tags go into JSON, never into the HTML render
        post_json = json.loads((out / "posts/hello-world/index.json").read_text())
        assert post_json["tags"] == ["news"]
        html = (out / "posts/hello-world/index.html").read_text()
        assert "news" not in html

    def test_paginated_index_pages(self, db: Session, tmp_path: Path) -> None:
        site = _create_site(db, base_url=PUBLIC_BASE)
        for i in range(PAGE_COUNT):
            _seed_post(db, site, slug=f"post-{i}")
        out = tmp_path / "dist"
        export_site(db, SITE_SLUG, out)
        from app.services.public import PAGE_SIZE

        assert PAGE_SIZE > 0
        assert (out / "index.html").is_file()
        page_count = (PAGE_COUNT + PAGE_SIZE - 1) // PAGE_SIZE
        for pg in range(2, page_count + 1):
            assert (out / f"page/{pg}/index.html").is_file(), f"missing page/{pg}"


PAGE_COUNT = 25


# ---------------------------------------------------------------------------
# Parity with the live public API
# ---------------------------------------------------------------------------


class TestParity:
    def test_post_html_parity(self, client: TestClient, db: Session, tmp_path: Path) -> None:
        site = _create_site(db)
        _seed_post(db, site, slug="parity-post", body_md="# Parity\n\nBody.\n")
        out = tmp_path / "dist"
        export_site(db, SITE_SLUG, out, base_url="http://testserver")

        live = client.get(f"/{SITE_SLUG}/parity-post")
        assert live.status_code == 200
        exported = (out / "posts/parity-post/index.html").read_bytes()
        assert exported == live.content

    def test_index_html_parity(self, client: TestClient, db: Session, tmp_path: Path) -> None:
        site = _create_site(db)
        _seed_post(db, site, slug="parity-post")
        out = tmp_path / "dist"
        export_site(db, SITE_SLUG, out, base_url="http://testserver")

        live = client.get(f"/{SITE_SLUG}")
        assert live.status_code == 200
        exported = (out / "index.html").read_bytes()
        assert exported == live.content

    def test_tag_index_html_parity(self, client: TestClient, db: Session, tmp_path: Path) -> None:
        site = _create_site(db)
        _seed_post(db, site, slug="parity-post", tags=["news"])
        out = tmp_path / "dist"
        export_site(db, SITE_SLUG, out, base_url="http://testserver")

        live = client.get(f"/{SITE_SLUG}", params={"tag": "news"})
        assert live.status_code == 200
        exported = (out / "tags/news/index.html").read_bytes()
        assert exported == live.content

    def test_post_json_has_expected_fields(self, db: Session, tmp_path: Path) -> None:
        from app.services.public import post_to_public_dict

        site = _create_site(db)
        post = _seed_post(db, site, slug="json-check", tags=["news"])
        out = tmp_path / "dist"
        export_site(db, SITE_SLUG, out, base_url="http://testserver")

        exported = json.loads((out / "posts/json-check/index.json").read_text())
        expected = post_to_public_dict(post, SITE_SLUG, tags=["news"])
        assert exported == expected
        assert exported["tags"] == ["news"]
        assert exported["slug"] == "json-check"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_two_exports_byte_identical(self, db: Session, tmp_path: Path) -> None:
        site = _create_site(db, base_url=PUBLIC_BASE)
        _seed_post(db, site, slug="first", tags=["news"])
        _seed_post(db, site, slug="second")

        out1 = tmp_path / "dist1"
        out2 = tmp_path / "dist2"
        export_site(db, SITE_SLUG, out1)
        export_site(db, SITE_SLUG, out2)

        assert _tree_digest(out1) == _tree_digest(out2)
        assert archive_bytes_for_export(out1, "tar.gz") == archive_bytes_for_export(out2, "tar.gz")
        assert archive_bytes_for_export(out1, "zip") == archive_bytes_for_export(out2, "zip")


# ---------------------------------------------------------------------------
# Incremental + since + round-trip
# ---------------------------------------------------------------------------


class TestIncremental:
    def test_incremental_reuses_and_updates(self, db: Session, tmp_path: Path) -> None:
        site = _create_site(db, base_url=PUBLIC_BASE)
        _seed_post(db, site, slug="alpha")
        _seed_post(db, site, slug="beta")

        out = tmp_path / "dist"
        full = export_site(db, SITE_SLUG, out)
        assert full.reused_files == 0

        beta = db.query(Post).filter(Post.slug == "beta").first()
        assert beta is not None
        beta.body_md = "# Beta v2\n\nChanged.\n"
        beta.excerpt = "# Beta v2"
        beta.content_hash = hashlib.sha256(beta.body_md.encode("utf-8")).hexdigest()
        beta.updated_at = datetime(2026, 9, 2, 12, 0)
        db.commit()

        inc = export_site(db, SITE_SLUG, out, incremental=True)
        assert inc.reused_files >= 3  # alpha's three files reused untouched
        assert inc.post_count == 2
        beta_html = (out / "posts/beta/index.html").read_text()
        assert "Beta v2" in beta_html

    def test_round_trip_after_revert(self, db: Session, tmp_path: Path) -> None:
        site = _create_site(db, base_url=PUBLIC_BASE)
        _seed_post(db, site, slug="revert", body_md="# Original\n\nBody.\n")

        out = tmp_path / "dist"
        export_site(db, SITE_SLUG, out)
        original_md = (out / "posts/revert/index.md").read_bytes()

        post = db.query(Post).filter(Post.slug == "revert").first()
        assert post is not None
        post.body_md = "# Broken\n\nWIP.\n"
        post.excerpt = "# Broken"
        post.content_hash = hashlib.sha256(post.body_md.encode("utf-8")).hexdigest()
        post.updated_at = datetime(2026, 9, 3, 12, 0)
        db.commit()
        export_site(db, SITE_SLUG, out, incremental=True)

        # Revert the content to the original (only timestamps move forward).
        post.body_md = "# Original\n\nBody.\n"
        post.excerpt = "# Original"
        post.content_hash = hashlib.sha256(post.body_md.encode("utf-8")).hexdigest()
        post.updated_at = datetime(2026, 9, 4, 12, 0)
        db.commit()
        export_site(db, SITE_SLUG, out, incremental=True)

        # Reader-facing content round-trips exactly.
        assert (out / "posts/revert/index.md").read_bytes() == original_md
        assert "Body." in (out / "posts/revert/index.html").read_text()

        # The reverted tree is byte-deterministic: a fresh full export of the
        # reverted state produces the exact same tree.
        fresh = tmp_path / "fresh"
        export_site(db, SITE_SLUG, fresh)
        assert _tree_digest(out) == _tree_digest(fresh)


class TestPerformance:
    def test_full_100_posts_under_10s(self, db: Session, tmp_path: Path) -> None:
        site = _create_site(db, base_url=PUBLIC_BASE)
        base = datetime(2026, 1, 1, 0, 0)
        for i in range(100):
            _seed_post(
                db,
                site,
                slug=f"perf-full-{i:03d}",
                title=f"Post {i}",
                body_md=f"# Post {i}\n\nBody number {i}.\n",
                updated_at=base + timedelta(minutes=i),
            )
        out = tmp_path / "dist"
        started_at = _now()
        result = export_site(db, SITE_SLUG, out)
        elapsed = _now() - started_at
        assert result.post_count == 100
        assert elapsed < 10.0, f"full export took {elapsed:.3f}s"

    def test_incremental_one_changed_under_1s(self, db: Session, tmp_path: Path) -> None:
        site = _create_site(db, base_url=PUBLIC_BASE)
        base = datetime(2026, 1, 1, 0, 0)
        for i in range(100):
            _seed_post(
                db,
                site,
                slug=f"perf-inc-{i:03d}",
                title=f"Post {i}",
                body_md=f"# Post {i}\n\nBody number {i}.\n",
                updated_at=base + timedelta(minutes=i),
            )
        out = tmp_path / "dist"
        export_site(db, SITE_SLUG, out)

        post = db.query(Post).filter(Post.slug == "perf-inc-099").first()
        assert post is not None
        post.body_md = "# Post 99 v2\n\nChanged.\n"
        post.content_hash = hashlib.sha256(post.body_md.encode("utf-8")).hexdigest()
        post.updated_at = datetime(2026, 9, 1, 9, 0)
        db.commit()

        started_at = _now()
        result = export_site(db, SITE_SLUG, out, incremental=True)
        elapsed = _now() - started_at
        assert result.reused_files > 0
        assert elapsed < 1.0, f"incremental export took {elapsed:.3f}s"


def _now() -> float:
    import time

    return time.perf_counter()


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


class TestVerify:
    def test_verify_ok(self, db: Session, tmp_path: Path) -> None:
        site = _create_site(db, base_url=PUBLIC_BASE)
        _seed_post(db, site, slug="ok")
        out = tmp_path / "dist"
        export_site(db, SITE_SLUG, out)

        result = verify_export(out)
        assert result.ok
        assert result.checked_files > 0

    def test_verify_detects_corruption(self, db: Session, tmp_path: Path) -> None:
        site = _create_site(db, base_url=PUBLIC_BASE)
        _seed_post(db, site, slug="corrupt")
        out = tmp_path / "dist"
        export_site(db, SITE_SLUG, out)

        target = out / "posts/corrupt/index.html"
        original = target.read_bytes()
        target.write_bytes(original + b"\n<!-- tampered -->\n")

        result = verify_export(out)
        assert not result.ok
        assert any("corrupted" in i and "index.html" in i for i in result.issues)


# ---------------------------------------------------------------------------
# Private / loopback URL guard
# ---------------------------------------------------------------------------


class TestUrlGuard:
    def test_is_private_host(self) -> None:
        assert is_private_host("localhost")
        assert is_private_host("api.localhost")
        assert is_private_host("mysite.local")
        assert is_private_host("127.0.0.1")
        assert is_private_host("10.0.0.5")
        assert is_private_host("192.168.1.1")
        assert is_private_host("0.0.0.0")
        assert is_private_host("::1")
        assert not is_private_host("blog.example.com")
        assert not is_private_host("you.github.io")
        assert not is_private_host("testserver")

    def test_private_base_url_rejected(self, db: Session, tmp_path: Path) -> None:
        _create_site(db, base_url="http://localhost:8000")
        out = tmp_path / "dist"
        with pytest.raises(ExportBaseUrlPrivateError):
            export_site(db, SITE_SLUG, out)

    def test_missing_base_url_required(self, db: Session, tmp_path: Path) -> None:
        _create_site(db)
        out = tmp_path / "dist"
        with pytest.raises(ExportBaseUrlRequiredError):
            export_site(db, SITE_SLUG, out)

    def test_no_private_hosts_leak_into_export(self, db: Session, tmp_path: Path) -> None:
        site = _create_site(db, base_url=PUBLIC_BASE)
        _seed_post(db, site, slug="leak-check", tags=["news"])
        out = tmp_path / "dist"
        export_site(db, SITE_SLUG, out)

        forbidden = ("localhost", "127.", "192.168.", "10.0.", "0.0.0.0", "::1", ".local", "testserver")
        for path in out.rglob("*"):
            if not path.is_file():
                continue
            data = path.read_bytes().decode("utf-8", errors="replace").lower()
            assert not any(needle in data for needle in forbidden), f"private host leaked into {path}"


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


class TestApiExport:
    def test_export_tar_gz(self, client: TestClient, db: Session, tmp_path: Path) -> None:
        site = _create_site(db, base_url=PUBLIC_BASE)
        _seed_post(db, site, slug="api-post")
        headers = _make_auth_headers(db, ["sites:read"])

        resp = client.get(f"/v1/sites/{SITE_SLUG}/export", headers=headers)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/gzip"
        assert "blog-export.tar.gz" in resp.headers["content-disposition"]
        assert resp.headers["cache-control"] == "no-store"

        names = _tar_names(resp.content)
        assert "manifest.json" in names
        assert "posts/api-post/index.html" in names
        assert "posts/api-post/index.json" in names
        assert "index.html" in names
        assert ".nojekyll" in names

    def test_export_zip(self, client: TestClient, db: Session) -> None:
        site = _create_site(db, base_url=PUBLIC_BASE)
        _seed_post(db, site, slug="zip-post")
        headers = _make_auth_headers(db, ["sites:read"])

        resp = client.get(f"/v1/sites/{SITE_SLUG}/export", params={"format": "zip"}, headers=headers)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert "blog-export.zip" in resp.headers["content-disposition"]
        assert "posts/zip-post/index.md" in _zip_names(resp.content)

    def test_export_with_since_is_delta(self, client: TestClient, db: Session) -> None:
        site = _create_site(db, base_url=PUBLIC_BASE)
        earlier = datetime(2026, 8, 1, 12, 0)
        later = datetime(2026, 8, 1, 12, 3)
        _seed_post(db, site, slug="older-post", updated_at=earlier)
        _seed_post(db, site, slug="newer-post", updated_at=later)
        headers = _make_auth_headers(db, ["sites:read"])

        since = format_iso(earlier + timedelta(seconds=1))
        resp = client.get(f"/v1/sites/{SITE_SLUG}/export", params={"since": since}, headers=headers)
        assert resp.status_code == 200
        names = _tar_names(resp.content)
        assert "posts/newer-post/index.html" in names
        assert "posts/older-post/index.html" not in names
        assert "index.html" not in names

    def test_export_unknown_site_404(self, client: TestClient, db: Session) -> None:
        headers = _make_auth_headers(db, ["sites:read"])
        resp = client.get("/v1/sites/nope/export", headers=headers)
        assert resp.status_code == 404

    def test_export_bad_since_400(self, client: TestClient, db: Session) -> None:
        site = _create_site(db, base_url=PUBLIC_BASE)
        _seed_post(db, site, slug="bad-since")
        headers = _make_auth_headers(db, ["sites:read"])
        resp = client.get(f"/v1/sites/{SITE_SLUG}/export", params={"since": "not-a-date"}, headers=headers)
        assert resp.status_code == 400

    def test_export_requires_auth_401(self, client: TestClient, db: Session) -> None:
        site = _create_site(db, base_url=PUBLIC_BASE)
        _seed_post(db, site, slug="unauth")
        resp = client.get(f"/v1/sites/{SITE_SLUG}/export")
        assert resp.status_code == 401

    def test_export_forbidden_without_sites_read(self, client: TestClient, db: Session) -> None:
        site = _create_site(db, base_url=PUBLIC_BASE)
        _seed_post(db, site, slug="scope")
        headers = _make_auth_headers(db, ["posts:read"])
        resp = client.get(f"/v1/sites/{SITE_SLUG}/export", headers=headers)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_cli_export_and_verify(self, db: Session, tmp_path: Path) -> None:
        site = _create_site(db, base_url=PUBLIC_BASE)
        _seed_post(db, site, slug="cli-post")
        out = tmp_path / "dist"

        result = _run_cli(["export", "--site", SITE_SLUG, "--out", str(out), "--base-url", PUBLIC_BASE])
        assert result.returncode == 0, result.stderr
        assert (out / "posts/cli-post/index.html").is_file()
        assert read_manifest(out) is not None

        verify = _run_cli(["export", "--out", str(out), "--verify"])
        assert verify.returncode == 0, verify.stderr
        assert "verify OK" in verify.stdout

    def test_cli_unknown_site_fails(self, db: Session, tmp_path: Path) -> None:
        out = tmp_path / "dist"
        result = _run_cli(["export", "--site", "absent", "--out", str(out), "--base-url", PUBLIC_BASE])
        assert result.returncode != 0
        assert "No site with slug" in result.stderr

    def test_deploy_plan(self, db: Session, tmp_path: Path) -> None:
        site = _create_site(db, base_url=PUBLIC_BASE)
        _seed_post(db, site, slug="deploy")
        out = tmp_path / "dist"
        export_site(db, SITE_SLUG, out)

        commands = plan_github_pages_deploy(out, "acme/static-blog", "gh-pages")
        assert commands[-1].endswith("git push -f origin gh-pages")
        assert "https://github.com/acme/static-blog.git" in commands[-2]

        with pytest.raises(ExportError):
            validate_repo("not-a-repo")

        empty = tmp_path / "empty"
        with pytest.raises(ExportError):
            plan_github_pages_deploy(empty, "acme/static-blog")


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    repo_root = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [sys.executable, "-m", "scripts.cli", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=120,
    )
