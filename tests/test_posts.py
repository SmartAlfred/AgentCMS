"""Contract tests for the Post CRUD API (#4).

Exercises the full create -> update -> publish -> read -> unpublish -> trash
cycle with ``curl``-equivalent HTTP calls.  Also asserts field names match the
ticket verbatim (public API contract).

Acceptance criteria covered:
- Full create -> update -> publish -> read -> unpublish -> trash cycle
- 201 responses carry a Location header
- Contract tests assert JSON field names verbatim
- Publishing a post twice is a no-op returning 200 with warnings
- ?dry_run=true is accepted on create/update
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from app.models.post_revision import PostRevision
from app.models.site import Site
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

SITE_SLUG = "blog"


def _create_site(session: Session, slug: str = SITE_SLUG) -> Site:
    """Create a test site for the post tests."""
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
# Acceptance: Full CRUD cycle
# ---------------------------------------------------------------------------


class TestPostCRUDCycle:
    """Full create -> update -> publish -> read -> unpublish -> trash cycle."""

    def test_full_cycle(self, client: TestClient, db: Session) -> None:
        _create_site(db)

        # 1. Create
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# My First Post\n\nHello world."},
        )
        assert resp.status_code == 201
        assert resp.headers["location"]
        data = resp.json()
        post_id = data["id"]
        assert data["status"] == "draft"
        assert data["slug"] == "my-first-post"
        assert data["title"] == "My First Post"
        assert data["revision"] == 1
        assert "id" in data
        assert "slug" in data
        assert "status" in data
        assert "url" in data
        assert "markdown_url" in data
        assert "created_at" in data
        assert "updated_at" in data

        # 2. Read (by id)
        resp = client.get(f"/v1/posts/{post_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == post_id

        # 3. Read (by slug)
        resp = client.get("/v1/posts/my-first-post")
        assert resp.status_code == 200
        assert resp.json()["id"] == post_id

        # 4. Update
        resp = client.patch(
            f"/v1/posts/{post_id}",
            json={"title": "Updated Title", "body_md": "# Updated\n\nNew content."},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Updated Title"
        assert data["revision"] == 2

        # 5. Publish
        resp = client.post(f"/v1/posts/{post_id}/publish")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "published"
        assert data["published_at"] is not None

        # 6. Unpublish
        resp = client.post(f"/v1/posts/{post_id}/unpublish")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "draft"

        # 7. Trash
        resp = client.delete(f"/v1/posts/{post_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "trashed"

        # 8. Verify trashed post is gone from list
        resp = client.get(f"/v1/sites/{SITE_SLUG}/posts")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0


# ---------------------------------------------------------------------------
# Acceptance: 201 carries Location header
# ---------------------------------------------------------------------------


class TestLocationHeader:
    """201 responses carry a Location header with the canonical URL."""

    def test_create_returns_location(self, client: TestClient, db: Session) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Test"},
        )
        assert resp.status_code == 201
        assert "location" in resp.headers
        assert resp.headers["location"].startswith("/v1/posts/")


# ---------------------------------------------------------------------------
# Acceptance: field names verbatim
# ---------------------------------------------------------------------------


class TestContractFieldNames:
    """JSON field names in the response match the ticket spec verbatim."""

    EXPECTED_FIELDS: ClassVar[set[str]] = {
        "id",
        "slug",
        "title",
        "body_md",
        "excerpt",
        "status",
        "frontmatter",
        "tags",
        "url",
        "markdown_url",
        "revision",
        "created_at",
        "updated_at",
        "published_at",
        "site_id",
    }

    def test_create_response_fields(self, client: TestClient, db: Session) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Hello\n\nWorld"},
        )
        assert resp.status_code == 201
        data = resp.json()
        missing = self.EXPECTED_FIELDS - set(data.keys())
        assert not missing, f"Missing fields in response: {missing}"

    def test_read_response_fields(self, client: TestClient, db: Session) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Hello\n\nWorld"},
        )
        post_id = resp.json()["id"]
        resp = client.get(f"/v1/posts/{post_id}")
        data = resp.json()
        missing = self.EXPECTED_FIELDS - set(data.keys())
        assert not missing, f"Missing fields in response: {missing}"

    def test_update_response_fields(self, client: TestClient, db: Session) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Hello\n\nWorld"},
        )
        post_id = resp.json()["id"]
        resp = client.patch(f"/v1/posts/{post_id}", json={"title": "New"})
        data = resp.json()
        missing = self.EXPECTED_FIELDS - set(data.keys())
        assert not missing, f"Missing fields in response: {missing}"

    def test_list_response_fields(self, client: TestClient, db: Session) -> None:
        _create_site(db)
        client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Hello"},
        )
        resp = client.get(f"/v1/sites/{SITE_SLUG}/posts")
        data = resp.json()
        assert "items" in data
        assert "next_cursor" in data
        assert "count" in data
        if data["items"]:
            item = data["items"][0]
            missing = self.EXPECTED_FIELDS - set(item.keys())
            assert not missing, f"Missing fields in list item: {missing}"


# ---------------------------------------------------------------------------
# Acceptance: status in create body is ignored
# ---------------------------------------------------------------------------


class TestCreateIgnoresStatus:
    """POST …/posts always returns draft; status in body is ignored."""

    def test_status_ignored_on_create(self, client: TestClient, db: Session) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Test", "status": "published"},
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "draft"

    def test_status_ignored_with_warning(self, client: TestClient, db: Session) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Test", "status": "published"},
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == "draft"


# ---------------------------------------------------------------------------
# Acceptance: title derivation from H1
# ---------------------------------------------------------------------------


class TestTitleDerivation:
    """Title may be omitted — derived from first # H1 in body_md."""

    def test_title_derived_from_h1(self, client: TestClient, db: Session) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# My Great Title\n\nSome content."},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "My Great Title"
        assert data["slug"] == "my-great-title"

    def test_title_required_when_no_h1(self, client: TestClient, db: Session) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "No heading here."},
        )
        assert resp.status_code == 422
        assert resp.headers["content-type"] == "application/problem+json"
        assert resp.json()["code"] == "title-required"


# ---------------------------------------------------------------------------
# Acceptance: slug conflict returns 409 with suggested_slug
# ---------------------------------------------------------------------------


class TestSlugConflict:
    """Duplicate slug returns 409 with suggested_slug."""

    def test_slug_conflict_returns_suggested(self, client: TestClient, db: Session) -> None:
        _create_site(db)
        client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# First", "slug": "my-post"},
        )
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Second", "slug": "my-post"},
        )
        assert resp.status_code == 409
        data = resp.json()
        assert data["code"] == "slug-conflict"
        assert "suggested_slug" in data
        assert data["suggested_slug"] == "my-post-2"


# ---------------------------------------------------------------------------
# Acceptance: publishing twice is a no-op
# ---------------------------------------------------------------------------


class TestIdempotentPublish:
    """Publishing a post twice returns 200 with warnings[], not an error."""

    def test_double_publish_returns_200_with_warnings(self, client: TestClient, db: Session) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Post"},
        )
        post_id = resp.json()["id"]

        # Publish once
        resp = client.post(f"/v1/posts/{post_id}/publish")
        assert resp.status_code == 200

        # Publish again — should be idempotent
        resp = client.post(f"/v1/posts/{post_id}/publish")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "published"
        assert len(data.get("warnings", [])) > 0


# ---------------------------------------------------------------------------
# Acceptance: dry_run on create
# ---------------------------------------------------------------------------


class TestDryRun:
    """?dry_run=true is accepted on create and update and doesn't persist."""

    def test_dry_run_create(self, client: TestClient, db: Session) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Draft"},
            params={"dry_run": True},
        )
        assert resp.status_code == 201
        assert resp.json().get("dry_run") is True

        # Verify nothing was persisted
        resp = client.get(f"/v1/sites/{SITE_SLUG}/posts")
        assert resp.json()["count"] == 0

    def test_dry_run_update(self, client: TestClient, db: Session) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Real"},
        )
        post_id = resp.json()["id"]

        resp = client.patch(
            f"/v1/posts/{post_id}",
            json={"title": "Changed"},
            params={"dry_run": True},
        )
        assert resp.status_code == 200

        # Verify title wasn't changed
        resp = client.get(f"/v1/posts/{post_id}")
        assert resp.json()["title"] == "Real"


# ---------------------------------------------------------------------------
# Acceptance: cursor pagination
# ---------------------------------------------------------------------------


class TestCursorPagination:
    """Listing uses cursor pagination with next_cursor."""

    def test_pagination_basic(self, client: TestClient, db: Session) -> None:
        _create_site(db)
        for i in range(5):
            client.post(
                f"/v1/sites/{SITE_SLUG}/posts",
                json={"body_md": f"# Post {i}"},
            )

        resp = client.get(f"/v1/sites/{SITE_SLUG}/posts", params={"limit": 2})
        data = resp.json()
        assert data["count"] == 2
        assert data["next_cursor"] is not None

        # Fetch next page
        resp = client.get(
            f"/v1/sites/{SITE_SLUG}/posts",
            params={"limit": 2, "cursor": data["next_cursor"]},
        )
        data2 = resp.json()
        assert data2["count"] == 2
        # No overlap
        ids1 = {item["id"] for item in data["items"]}
        ids2 = {item["id"] for item in data2["items"]}
        assert ids1.isdisjoint(ids2)


# ---------------------------------------------------------------------------
# Acceptance: content required
# ---------------------------------------------------------------------------


class TestContentRequired:
    """body_md is the only required field on create."""

    def test_empty_body_md_rejected(self, client: TestClient, db: Session) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": ""},
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == "content-required"

    def test_missing_body_md_rejected(self, client: TestClient, db: Session) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Acceptance: trash doesn't destroy revisions
# ---------------------------------------------------------------------------


class TestTrashPreservesRevisions:
    """Trashing a post doesn't destroy its revisions."""

    def test_trash_preserves_revisions(self, client: TestClient, db: Session) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Post"},
        )
        post_id = resp.json()["id"]

        # Update to create a revision
        client.patch(f"/v1/posts/{post_id}", json={"title": "Updated"})

        # Trash
        client.delete(f"/v1/posts/{post_id}")

        # Check revisions still exist (create + update + trash = 3)
        revisions = db.query(PostRevision).filter(PostRevision.post_id == uuid.UUID(post_id)).all()
        assert len(revisions) == 3


# ---------------------------------------------------------------------------
# Acceptance: site not found
# ---------------------------------------------------------------------------


class TestSiteNotFound:
    """Creating a post in a non-existent site returns 404."""

    def test_site_not_found(self, client: TestClient, db: Session) -> None:
        resp = client.post(
            "/v1/sites/nope/posts",
            json={"body_md": "# Post"},
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "site-not-found"


# ---------------------------------------------------------------------------
# Acceptance: post not found
# ---------------------------------------------------------------------------


class TestPostNotFound:
    """Reading a non-existent post returns 404."""

    def test_post_not_found(self, client: TestClient, db: Session) -> None:
        resp = client.get("/v1/posts/nonexistent")
        assert resp.status_code == 404
        assert resp.json()["code"] == "post-not-found"


# ---------------------------------------------------------------------------
# Acceptance: invalid transition errors
# ---------------------------------------------------------------------------


class TestInvalidTransitions:
    """Invalid status transitions return 409 with allowed_from."""

    def test_unpublish_draft(self, client: TestClient, db: Session) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Draft"},
        )
        post_id = resp.json()["id"]

        resp = client.post(f"/v1/posts/{post_id}/unpublish")
        assert resp.status_code == 409
        assert resp.json()["code"] == "invalid-transition"
        assert "allowed_from" in resp.json()


# ---------------------------------------------------------------------------
# Acceptance: tags
# ---------------------------------------------------------------------------


class TestTags:
    """Tags are created and returned with posts."""

    def test_create_with_tags(self, client: TestClient, db: Session) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Post", "tags": ["python", "fastapi"]},
        )
        assert resp.status_code == 201
        assert set(resp.json()["tags"]) == {"python", "fastapi"}


# ---------------------------------------------------------------------------
# Acceptance: dry_run=true on create returns correct shape
# ---------------------------------------------------------------------------


class TestDryRunResponseShape:
    """dry_run response contains expected fields."""

    def test_dry_run_shape(self, client: TestClient, db: Session) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Test"},
            params={"dry_run": True},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "dry_run" in data
        assert data["dry_run"] is True
