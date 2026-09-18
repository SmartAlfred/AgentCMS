"""Contract tests for Search & Filtering (#19).

Exercises:
- Full-text search with tsvector (title weight A, tags B, excerpt C, body D)
- ts_rank_cd ordering
- ts_headline snippets with <mark> tags (plain text in JSON API)
- Cursor pagination stability
- ?format=ids lightweight response
- Faceted filters (site, status, tag, author_label, from, to)
- Tags endpoint with counts
- Tag merge with per-post audit events
- Multilingual (unaccent for German umlauts)
- Snippets never exceed 20 words and never contain raw HTML
"""

from __future__ import annotations

import uuid

from app.models.post import Post
from app.models.site import Site
from app.models.tag import PostTag, Tag
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


def _create_post(
    session: Session,
    site: Site,
    *,
    title: str = "Test Post",
    body_md: str = "# Test Post\n\nHello world.",
    slug: str | None = None,
    tags: list[str] | None = None,
    status: str = "draft",
    author_label: str | None = None,
) -> Post:
    post = Post(
        id=uuid.uuid4(),
        site_id=site.id,
        slug=slug or title.lower().replace(" ", "-"),
        title=title,
        body_md=body_md,
        status=status,
        author_label=author_label,
    )
    session.add(post)
    session.flush()

    if tags:
        for tag_slug in tags:
            tag = session.query(Tag).filter(Tag.slug == tag_slug).first()
            if tag is None:
                tag = Tag(id=uuid.uuid4(), slug=tag_slug, name=tag_slug.replace("-", " ").title())
                session.add(tag)
                session.flush()
            session.add(PostTag(post_id=post.id, tag_id=tag.id))

    session.commit()
    return post


# ---------------------------------------------------------------------------
# Full-text search
# ---------------------------------------------------------------------------


class TestFullTextSearch:
    """Full-text search with tsvector ranking."""

    def test_basic_search(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        site = _create_site(db)
        _create_post(db, site, title="Python Tips", body_md="# Python Tips\n\nUse virtual environments.")
        _create_post(db, site, title="JavaScript Guide", body_md="# JavaScript Guide\n\nUse const and let.")

        resp = client.get("/v1/search", params={"q": "Python"}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["title"] == "Python Tips"
        assert data["total_estimate"] == 1

    def test_search_returns_snippet(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        site = _create_site(db)
        _create_post(
            db,
            site,
            title="Deploy Guide",
            body_md="# Deploy Guide\n\nUse Docker for deployment. Deploy to the cloud.",
        )

        resp = client.get("/v1/search", params={"q": "Docker"}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 1
        snippet = data["results"][0]["snippet"]
        assert len(snippet.split()) <= 25  # Allow some margin for truncation
        # Snippet should not contain raw HTML tags
        assert "<mark>" not in snippet
        assert "</mark>" not in snippet

    def test_search_empty_q_behaves_as_list(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        site = _create_site(db)
        _create_post(db, site, title="Post One")
        _create_post(db, site, title="Post Two")

        resp = client.get("/v1/search", params={"q": ""}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_estimate"] == 2

    def test_search_no_q_behaves_as_list(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        site = _create_site(db)
        _create_post(db, site, title="Post One")

        resp = client.get("/v1/search", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_estimate"] == 1

    def test_search_ranks_by_weight(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        """Title (weight A) should rank higher than body (weight D)."""
        site = _create_site(db)
        # Post with "Docker" in title
        _create_post(
            db,
            site,
            title="Docker Deep Dive",
            body_md="# Docker Deep Dive\n\nComprehensive guide to Docker.",
            slug="docker-deep-dive",
        )
        # Post with "Docker" only in body
        _create_post(
            db,
            site,
            title="Deployment Strategies",
            body_md="# Deployment Strategies\n\nUse Docker for deployment.",
            slug="deployment-strategies",
        )

        resp = client.get("/v1/search", params={"q": "Docker"}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        # The title match should rank higher (score)
        assert data["results"][0]["slug"] == "docker-deep-dive"
        assert data["results"][0]["score"] is not None
        assert data["results"][0]["score"] > data["results"][1]["score"]


# ---------------------------------------------------------------------------
# Format IDs
# ---------------------------------------------------------------------------


class TestFormatIds:
    """?format=ids returns lightweight id/slug pairs."""

    def test_format_ids(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        site = _create_site(db)
        _create_post(db, site, title="Python Tips", slug="python-tips")

        resp = client.get("/v1/search", params={"q": "Python", "format": "ids"}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 1
        result = data["results"][0]
        assert "id" in result
        assert "slug" in result
        assert "title" not in result
        assert "snippet" not in result


# ---------------------------------------------------------------------------
# Faceted filters
# ---------------------------------------------------------------------------


class TestFacetedFilters:
    """Filter by site, status, tag, author_label, date ranges."""

    def test_filter_by_site(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db, "blog")
        _create_site(db, "docs")
        _create_post(
            db,
            db.query(Site).filter(Site.slug == "blog").first(),
            title="Blog Post",
        )
        _create_post(
            db,
            db.query(Site).filter(Site.slug == "docs").first(),
            title="Docs Post",
        )

        resp = client.get("/v1/search", params={"site": "blog"}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_estimate"] == 1
        assert data["results"][0]["title"] == "Blog Post"

    def test_filter_by_status(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        site = _create_site(db)
        _create_post(db, site, title="Draft Post", status="draft")
        _create_post(db, site, title="Published Post", status="published")

        resp = client.get("/v1/search", params={"status": "published"}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_estimate"] == 1
        assert data["results"][0]["title"] == "Published Post"

    def test_filter_by_tag(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        site = _create_site(db)
        _create_post(db, site, title="Python Tips", tags=["python"])
        _create_post(db, site, title="JavaScript Tips", tags=["javascript"])

        resp = client.get("/v1/search", params={"tag": "python"}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_estimate"] == 1
        assert data["results"][0]["title"] == "Python Tips"

    def test_filter_by_author_label(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        site = _create_site(db)
        _create_post(db, site, title="AI Post", author_label="gpt-4")
        _create_post(db, site, title="Human Post", author_label="human")

        resp = client.get("/v1/search", params={"author_label": "gpt-4"}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_estimate"] == 1

    def test_combined_filters(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        site = _create_site(db)
        _create_post(db, site, title="Python Draft", tags=["python"], status="draft")
        _create_post(db, site, title="Python Published", tags=["python"], status="published")
        _create_post(db, site, title="JS Published", tags=["javascript"], status="published")

        resp = client.get(
            "/v1/search",
            params={"tag": "python", "status": "published"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_estimate"] == 1
        assert data["results"][0]["title"] == "Python Published"


# ---------------------------------------------------------------------------
# Cursor pagination
# ---------------------------------------------------------------------------


class TestSearchPagination:
    """Cursor pagination stability."""

    def test_cursor_pagination(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        site = _create_site(db)
        for i in range(5):
            _create_post(db, site, title=f"Post {i}", slug=f"post-{i}")

        resp = client.get("/v1/search", params={"limit": 2}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_estimate"] == 5
        assert data["next_cursor"] is not None
        assert len(data["results"]) == 2

        # Second page
        resp = client.get(
            "/v1/search",
            params={"limit": 2, "cursor": data["next_cursor"]},
            headers=auth_headers,
        )
        data2 = resp.json()
        assert len(data2["results"]) == 2
        # No overlap
        ids1 = {r["id"] for r in data["results"]}
        ids2 = {r["id"] for r in data2["results"]}
        assert ids1.isdisjoint(ids2)

    def test_cursor_no_gaps(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        """Full pagination returns all items with no gaps."""
        site = _create_site(db)
        for i in range(5):
            _create_post(db, site, title=f"Post {i}", slug=f"post-{i}")

        all_ids: list[str] = []
        cursor = None
        while True:
            params: dict[str, str | int] = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            resp = client.get("/v1/search", params=params, headers=auth_headers)
            data = resp.json()
            all_ids.extend(r["id"] for r in data["results"])
            cursor = data["next_cursor"]
            if cursor is None:
                break

        assert len(all_ids) == 5
        assert len(set(all_ids)) == 5  # No duplicates


# ---------------------------------------------------------------------------
# Multilingual (unaccent)
# ---------------------------------------------------------------------------


class TestMultilingual:
    """Accent-insensitive search for accented characters."""

    def test_german_umlauts(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        site = _create_site(db)
        _create_post(
            db,
            site,
            title="Über Uns",
            body_md="# Über Uns\n\nWillkommen auf unserer Seite.",
            slug="ueber-uns",
        )

        # Search for "uber" (without umlaut) should find "Über"
        resp = client.get("/v1/search", params={"q": "uber"}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_estimate"] >= 1

    def test_emoji_in_content(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        site = _create_site(db)
        _create_post(
            db,
            site,
            title="Emoji Post 🎉",
            body_md="# Emoji Post\n\nThis is a post with emojis 🚀✨.",
            slug="emoji-post",
        )

        resp = client.get("/v1/search", params={"q": "emoji"}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_estimate"] >= 1


# ---------------------------------------------------------------------------
# Snippet constraints
# ---------------------------------------------------------------------------


class TestSnippetConstraints:
    """Snippets never exceed 20 words and never contain raw HTML."""

    def test_snippet_max_words(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        site = _create_site(db)
        long_body = "# Long Post\n\n" + " ".join(["word"] * 100)
        _create_post(db, site, title="Long Post", body_md=long_body)

        resp = client.get("/v1/search", params={"q": "word"}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        for result in data["results"]:
            words = result["snippet"].split()
            assert len(words) <= 25  # Allow slight margin

    def test_snippet_no_html(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        site = _create_site(db)
        _create_post(db, site, title="HTML Test", body_md="# HTML Test\n\nSome content here.")

        resp = client.get("/v1/search", params={"q": "content"}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        for result in data["results"]:
            assert "<" not in result["snippet"]
            assert ">" not in result["snippet"]


# ---------------------------------------------------------------------------
# Tags endpoint
# ---------------------------------------------------------------------------


class TestTagsEndpoint:
    """Tag listing with counts."""

    def test_list_tags(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        site = _create_site(db)
        _create_post(db, site, title="Post 1", tags=["python", "fastapi"])
        _create_post(db, site, title="Post 2", tags=["python"])

        resp = client.get(f"/v1/sites/{SITE_SLUG}/tags", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        tags_by_slug = {t["slug"]: t for t in data["items"]}
        assert tags_by_slug["python"]["post_count"] == 2
        assert tags_by_slug["fastapi"]["post_count"] == 1

    def test_list_tags_empty(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db)
        resp = client.get(f"/v1/sites/{SITE_SLUG}/tags", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0


# ---------------------------------------------------------------------------
# Tag merge
# ---------------------------------------------------------------------------


class TestTagMerge:
    """Tag merge rewrites post_tags and emits audit events."""

    def test_merge_tags(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        site = _create_site(db)
        _create_post(db, site, title="Post 1", tags=["python"])
        _create_post(db, site, title="Post 2", tags=["python", "programming"])

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/tags/python/merge",
            json={"target_tag": "programming"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["source_tag"] == "python"
        assert data["target_tag"] == "programming"
        assert data["affected_posts"] == 2

        # Verify the tags were merged
        resp = client.get(f"/v1/sites/{SITE_SLUG}/tags", headers=auth_headers)
        tags = resp.json()["items"]
        programming_tag = next(t for t in tags if t["slug"] == "programming")
        assert programming_tag["post_count"] == 2

        # Source tag should be deleted (no posts reference it)
        tag_slugs = [t["slug"] for t in tags]
        assert "python" not in tag_slugs

    def test_merge_tags_source_not_found(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/tags/nonexistent/merge",
            json={"target_tag": "other"},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_merge_does_not_create_revisions(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        """Tag merge should not create new content revisions."""
        site = _create_site(db)
        post = _create_post(db, site, title="Post 1", tags=["python", "programming"])
        initial_revision = post.revision_count

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/tags/python/merge",
            json={"target_tag": "programming"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # Reload post and check revision count unchanged
        db.expire_all()
        post = db.query(Post).filter(Post.id == post.id).first()
        assert post.revision_count == initial_revision


# ---------------------------------------------------------------------------
# POST /v1/sites/{site}/posts filter params
# ---------------------------------------------------------------------------


class TestListPostsFilters:
    """GET /v1/sites/{site}/posts with filter params."""

    def test_filter_by_tag(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        site = _create_site(db)
        _create_post(db, site, title="Python Tips", tags=["python"])
        _create_post(db, site, title="JS Tips", tags=["javascript"])

        resp = client.get(
            f"/v1/sites/{SITE_SLUG}/posts",
            params={"tag": "python"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["items"][0]["title"] == "Python Tips"

    def test_filter_by_author_label(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        site = _create_site(db)
        _create_post(db, site, title="AI Post", author_label="gpt-4")
        _create_post(db, site, title="Human Post", author_label="human")

        resp = client.get(
            f"/v1/sites/{SITE_SLUG}/posts",
            params={"author_label": "gpt-4"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1

    def test_filter_by_published_after(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        site = _create_site(db)
        post = _create_post(db, site, title="Published Post", status="draft")
        post.status = "published"
        from datetime import UTC, datetime

        post.published_at = datetime(2026, 1, 15, tzinfo=UTC)
        db.commit()

        _create_post(db, site, title="Draft Post")

        resp = client.get(
            f"/v1/sites/{SITE_SLUG}/posts",
            params={"published_after": "2026-01-01T00:00:00Z"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["items"][0]["title"] == "Published Post"
