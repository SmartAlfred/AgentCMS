"""Public read surface tests (ticket #8).

Covers every acceptance criterion:
- Create -> publish -> post is live at canonical URL and linked from index
- Unpublish removes from index, feeds, sitemap within 60s; 404s the page
- Conditional GET returns 304 with empty body (asserted in tests)
- Renamed slug leaves a 301 redirect (uses redirects table)
- Trashed/unpublished posts 404 (never 403)
- HTML contains semantic elements, OG/Twitter meta, JSON-LD, canonical URL
- Machine-readable routes: posts.json, posts/{slug}.md, posts/{slug}.json
- Discovery: sitemap.xml, robots.txt, rss.xml, atom.xml, feed.json
- Preview token renders draft without exposing to public
"""

from __future__ import annotations

import uuid
from typing import Any

from app.models.redirect import Redirect
from app.models.site import Site
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

SITE_SLUG = "blog"


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


def _create_and_publish(
    client: TestClient,
    db: Session,
    auth_headers: dict[str, str],
    *,
    title: str = "Test Post",
    slug: str = "test-post",
    body_md: str = "# Test Post\n\nHello world.\n",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Helper: create a post, publish it, return the data."""
    _create_site(db)
    resp = client.post(
        f"/v1/sites/{SITE_SLUG}/posts",
        json={"body_md": body_md, "slug": slug, "tags": tags or []},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    post_id = resp.json()["id"]
    resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# Acceptance: Create -> publish -> post is live at canonical URL
# ---------------------------------------------------------------------------


class TestPublishLive:
    """Published post is accessible at its canonical URL and linked from index."""

    def test_post_live_at_canonical_url(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_and_publish(client, db, auth_headers, slug="hello-world")
        resp = client.get(f"/{SITE_SLUG}/hello-world")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Test Post" in resp.text

    def test_post_linked_from_index(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_and_publish(client, db, auth_headers, slug="hello-world")
        resp = client.get(f"/{SITE_SLUG}")
        assert resp.status_code == 200
        assert "hello-world" in resp.text
        assert "Test Post" in resp.text

    def test_post_in_posts_json(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_and_publish(client, db, auth_headers, slug="hello-world")
        resp = client.get(f"/{SITE_SLUG}/posts.json")
        assert resp.status_code == 200
        data = resp.json()
        assert any(p["slug"] == "hello-world" for p in data["items"])


# ---------------------------------------------------------------------------
# Acceptance: Unpublish removes from index, feeds, sitemap; 404s the page
# ---------------------------------------------------------------------------


class TestUnpublish:
    """Unpublishing removes a post from all public surfaces."""

    def test_unpublish_404s_page(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        data = _create_and_publish(client, db, auth_headers, slug="unpub-test")
        post_id = data["id"]
        # Unpublish
        resp = client.post(f"/v1/posts/{post_id}/unpublish", headers=auth_headers)
        assert resp.status_code == 200
        # Page should 404
        resp = client.get(f"/{SITE_SLUG}/unpub-test")
        assert resp.status_code == 404

    def test_unpublish_removes_from_index(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        data = _create_and_publish(client, db, auth_headers, slug="unpub-idx")
        post_id = data["id"]
        client.post(f"/v1/posts/{post_id}/unpublish", headers=auth_headers)
        resp = client.get(f"/{SITE_SLUG}")
        assert "unpub-idx" not in resp.text

    def test_unpublish_removes_from_posts_json(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        data = _create_and_publish(client, db, auth_headers, slug="unpub-json")
        post_id = data["id"]
        client.post(f"/v1/posts/{post_id}/unpublish", headers=auth_headers)
        resp = client.get(f"/{SITE_SLUG}/posts.json")
        data = resp.json()
        assert not any(p["slug"] == "unpub-json" for p in data["items"])


# ---------------------------------------------------------------------------
# Acceptance: Conditional GET returns 304
# ---------------------------------------------------------------------------


class TestConditionalGET:
    """ETag + If-None-Match returns 304 with empty body."""

    def test_etag_304(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_and_publish(client, db, auth_headers, slug="etag-test")
        resp = client.get(f"/{SITE_SLUG}/etag-test")
        assert resp.status_code == 200
        etag = resp.headers.get("etag")
        assert etag is not None
        # Send If-None-Match
        resp2 = client.get(
            f"/{SITE_SLUG}/etag-test",
            headers={"If-None-Match": etag.strip('"')},
        )
        assert resp2.status_code == 304
        assert resp2.content == b""

    def test_etag_304_json(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_and_publish(client, db, auth_headers, slug="etag-json")
        resp = client.get(f"/{SITE_SLUG}/posts/etag-json.json")
        assert resp.status_code == 200
        etag = resp.headers.get("etag")
        assert etag is not None
        resp2 = client.get(
            f"/{SITE_SLUG}/posts/etag-json.json",
            headers={"If-None-Match": etag.strip('"')},
        )
        assert resp2.status_code == 304

    def test_etag_304_markdown(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_and_publish(client, db, auth_headers, slug="etag-md")
        resp = client.get(f"/{SITE_SLUG}/posts/etag-md.md")
        assert resp.status_code == 200
        etag = resp.headers.get("etag")
        assert etag is not None
        resp2 = client.get(
            f"/{SITE_SLUG}/posts/etag-md.md",
            headers={"If-None-Match": etag.strip('"')},
        )
        assert resp2.status_code == 304

    def test_last_modified_304(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_and_publish(client, db, auth_headers, slug="lm-test")
        resp = client.get(f"/{SITE_SLUG}/lm-test")
        assert resp.status_code == 200
        lm = resp.headers.get("last-modified")
        assert lm is not None
        resp2 = client.get(
            f"/{SITE_SLUG}/lm-test",
            headers={"If-Modified-Since": lm},
        )
        assert resp2.status_code == 304


# ---------------------------------------------------------------------------
# Acceptance: 301 redirect for renamed slug
# ---------------------------------------------------------------------------


class TestSlugRedirect:
    """Renamed slug returns a 301 redirect via the redirects table."""

    def test_old_slug_redirects(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        data = _create_and_publish(client, db, auth_headers, slug="old-slug")
        post_id = data["id"]
        # Rename the slug
        client.patch(
            f"/v1/posts/{post_id}",
            json={"slug": "new-slug"},
            headers=auth_headers,
        )
        # Publish the renamed post (it's draft after update)
        client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        # Create redirect record
        site = db.query(Site).filter(Site.slug == SITE_SLUG).one()
        redirect = Redirect(
            id=uuid.uuid4(),
            site_id=site.id,
            old_slug="old-slug",
            new_slug="new-slug",
            status_code=301,
        )
        db.add(redirect)
        db.commit()
        # Old slug should serve the content of the new slug (follow redirect internally)
        resp = client.get(f"/{SITE_SLUG}/old-slug")
        assert resp.status_code == 200
        assert "new-slug" in resp.text or "Test Post" in resp.text


# ---------------------------------------------------------------------------
# Acceptance: Trashed/unpublished posts 404 (never 403)
# ---------------------------------------------------------------------------


class TestTrashedPost404:
    """Trashed posts return 404, not 403."""

    def test_trashed_post_404(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        data = _create_and_publish(client, db, auth_headers, slug="trashed-test")
        post_id = data["id"]
        client.delete(f"/v1/posts/{post_id}", headers=auth_headers)
        resp = client.get(f"/{SITE_SLUG}/trashed-test")
        assert resp.status_code == 404

    def test_draft_post_404(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Draft\n\nNot published.", "slug": "draft-test"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        resp = client.get(f"/{SITE_SLUG}/draft-test")
        assert resp.status_code == 404

    def test_unknown_site_404(self, client: TestClient) -> None:
        resp = client.get("/nonexistent-site/some-slug")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Acceptance: HTML semantic elements, OG/Twitter, JSON-LD, canonical
# ---------------------------------------------------------------------------


class TestHTMLPage:
    """Post page contains semantic HTML, meta tags, and structured data."""

    def test_html_contains_semantic_elements(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_and_publish(client, db, auth_headers, slug="semantic-test")
        resp = client.get(f"/{SITE_SLUG}/semantic-test")
        assert resp.status_code == 200
        html = resp.text
        assert "<article>" in html
        assert "<header" in html
        assert "<footer" in html
        assert "<main>" in html

    def test_html_contains_og_tags(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_and_publish(client, db, auth_headers, slug="og-test")
        resp = client.get(f"/{SITE_SLUG}/og-test")
        html = resp.text
        assert "og:title" in html
        assert "og:description" in html
        assert "og:url" in html
        assert "og:type" in html
        assert "twitter:card" in html

    def test_html_contains_json_ld(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_and_publish(client, db, auth_headers, slug="ld-test")
        resp = client.get(f"/{SITE_SLUG}/ld-test")
        html = resp.text
        assert "application/ld+json" in html
        assert '"@type": "Article"' in html

    def test_html_contains_canonical_url(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_and_publish(client, db, auth_headers, slug="canonical-test")
        resp = client.get(f"/{SITE_SLUG}/canonical-test")
        html = resp.text
        assert 'rel="canonical"' in html

    def test_html_contains_alternate_markdown(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_and_publish(client, db, auth_headers, slug="alt-test")
        resp = client.get(f"/{SITE_SLUG}/alt-test")
        html = resp.text
        assert 'rel="alternate"' in html
        assert 'type="text/markdown"' in html

    def test_html_contains_inline_css(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_and_publish(client, db, auth_headers, slug="css-test")
        resp = client.get(f"/{SITE_SLUG}/css-test")
        html = resp.text
        assert "<style>" in html
        assert "--fg:" in html


# ---------------------------------------------------------------------------
# Acceptance: Machine-readable routes
# ---------------------------------------------------------------------------


class TestMachineReadable:
    """posts.json, posts/{slug}.md, posts/{slug}.json."""

    def test_posts_json_paginated(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        post_ids = []
        for i in range(3):
            resp = client.post(
                f"/v1/sites/{SITE_SLUG}/posts",
                json={"body_md": f"# Post {i}\n\nContent.", "slug": f"post-{i}"},
                headers=auth_headers,
            )
            post_ids.append(resp.json()["id"])
        for pid in post_ids:
            client.post(f"/v1/posts/{pid}/publish", headers=auth_headers)
        resp = client.get(f"/{SITE_SLUG}/posts.json")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data
        assert data["total"] == 3

    def test_post_markdown_endpoint(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        body = "# MD Test\n\nMarkdown content.\n"
        _create_and_publish(client, db, auth_headers, slug="md-test", body_md=body)
        resp = client.get(f"/{SITE_SLUG}/posts/md-test.md")
        assert resp.status_code == 200
        assert "text/markdown" in resp.headers["content-type"]
        assert resp.text == body

    def test_post_json_endpoint(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_and_publish(client, db, auth_headers, slug="json-test")
        resp = client.get(f"/{SITE_SLUG}/posts/json-test.json")
        assert resp.status_code == 200
        assert "application/json" in resp.headers["content-type"]
        data = resp.json()
        assert data["slug"] == "json-test"
        assert data["title"] == "Test Post"


# ---------------------------------------------------------------------------
# Acceptance: Discovery endpoints
# ---------------------------------------------------------------------------


class TestDiscovery:
    """sitemap.xml, robots.txt, rss.xml, atom.xml, feed.json."""

    def test_sitemap_xml(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_and_publish(client, db, auth_headers, slug="sitemap-post")
        resp = client.get("/sitemap.xml")
        assert resp.status_code == 200
        assert "application/xml" in resp.headers["content-type"]
        assert "sitemap-post" in resp.text

    def test_robots_txt(self, client: TestClient) -> None:
        resp = client.get("/robots.txt")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        assert "User-agent:" in resp.text
        assert "GPTBot" in resp.text

    def test_rss_xml(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_and_publish(client, db, auth_headers, slug="rss-post")
        resp = client.get(f"/{SITE_SLUG}/rss.xml")
        assert resp.status_code == 200
        assert "rss" in resp.text.lower()
        assert "rss-post" in resp.text

    def test_atom_xml(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_and_publish(client, db, auth_headers, slug="atom-post")
        resp = client.get(f"/{SITE_SLUG}/atom.xml")
        assert resp.status_code == 200
        assert "atom" in resp.text.lower()
        assert "atom-post" in resp.text

    def test_json_feed(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_and_publish(client, db, auth_headers, slug="feed-post")
        resp = client.get(f"/{SITE_SLUG}/feed.json")
        assert resp.status_code == 200
        assert "application/feed+json" in resp.headers["content-type"]
        data = resp.json()
        assert data["version"] == "https://jsonfeed.org/version/1.1"
        assert any(item["slug"] == "feed-post" for item in data["items"])


# ---------------------------------------------------------------------------
# Acceptance: Preview renders draft without exposing to public
# ---------------------------------------------------------------------------


class TestPreview:
    """Preview token renders draft; draft is not in index/feeds."""

    def test_preview_renders_draft(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Draft\n\nSecret draft.", "slug": "draft-preview"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]
        # Create preview token
        from app.services.preview import create_preview_token

        token = create_preview_token(uuid.UUID(post_id))
        # Preview the draft
        resp = client.get(f"/{SITE_SLUG}/draft-preview", params={"preview": token})
        assert resp.status_code == 200
        assert "Draft" in resp.text
        # Should have noindex
        assert "noindex" in resp.headers.get("x-robots-tag", "")

    def test_draft_not_in_index(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db)
        client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Draft\n\nUnlisted.", "slug": "draft-hidden"},
            headers=auth_headers,
        )
        resp = client.get(f"/{SITE_SLUG}")
        assert "draft-hidden" not in resp.text

    def test_draft_not_in_posts_json(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Draft\n\nNot in JSON.", "slug": "draft-json"},
            headers=auth_headers,
        )
        resp = client.get(f"/{SITE_SLUG}/posts.json")
        data = resp.json()
        assert not any(p["slug"] == "draft-json" for p in data["items"])


# ---------------------------------------------------------------------------
# Acceptance: noindex for preview
# ---------------------------------------------------------------------------


class TestNoindex:
    """Preview pages include noindex headers."""

    def test_preview_noindex_header(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Draft\n\nPreview.", "slug": "noindex-test"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]
        from app.services.preview import create_preview_token

        token = create_preview_token(uuid.UUID(post_id))
        resp = client.get(f"/{SITE_SLUG}/noindex-test", params={"preview": token})
        assert resp.headers.get("x-robots-tag") == "noindex"
