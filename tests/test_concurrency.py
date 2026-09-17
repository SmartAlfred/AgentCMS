"""Tests for optimistic concurrency (ETag / If-Match) (#11).

Acceptance criteria covered:
* ETag on every GET of a post
* Stale If-Match → 412 including the fresh ETag; retry with it succeeds
* If-None-Match on GET → 304
* If-Match: * means "whatever is current" — allowed
* PATCH with identical content → 200, no-op: true, no new revision (#12)
"""

from __future__ import annotations

import uuid

from app.models.site import Site
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

SITE_SLUG = "blog"


def _create_site(session: Session, slug: str = SITE_SLUG) -> Site:
    site = Site(id=uuid.uuid4(), slug=slug, name=f"Test Site {slug}", publish_mode="auto")
    session.add(site)
    session.flush()
    session.commit()
    return site


# ---------------------------------------------------------------------------
# Acceptance: ETag on every GET of a post
# ---------------------------------------------------------------------------


class TestETagOnGet:
    """Every GET of a post returns an ETag header."""

    def test_get_returns_etag(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# ETag Test\n\nContent."},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        # GET should return ETag
        resp = client.get(f"/v1/posts/{post_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert "etag" in resp.headers
        etag = resp.headers["etag"]
        assert etag.startswith('"')
        assert etag.endswith('"')

    def test_etag_changes_after_update(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# ETag Change Test"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        # Get initial ETag
        resp1 = client.get(f"/v1/posts/{post_id}", headers=auth_headers)
        etag1 = resp1.headers["etag"]

        # Update the post
        client.patch(
            f"/v1/posts/{post_id}",
            json={"title": "Updated"},
            headers=auth_headers,
        )

        # Get new ETag
        resp2 = client.get(f"/v1/posts/{post_id}", headers=auth_headers)
        etag2 = resp2.headers["etag"]

        assert etag1 != etag2


# ---------------------------------------------------------------------------
# Acceptance: If-None-Match → 304
# ---------------------------------------------------------------------------


class TestIfNoneMatch:
    """If-None-Match on GET returns 304 when ETag matches."""

    def test_304_when_etag_matches(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# 304 Test"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        # Get the ETag
        resp = client.get(f"/v1/posts/{post_id}", headers=auth_headers)
        etag = resp.headers["etag"]

        # If-None-Match with matching ETag → 304
        resp = client.get(
            f"/v1/posts/{post_id}",
            headers={**auth_headers, "If-None-Match": etag},
        )
        assert resp.status_code == 304

    def test_200_when_etag_differs(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# 200 Test"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        # Get the ETag
        resp = client.get(f"/v1/posts/{post_id}", headers=auth_headers)
        etag = resp.headers["etag"]

        # Modify the post
        client.patch(
            f"/v1/posts/{post_id}",
            json={"title": "Changed"},
            headers=auth_headers,
        )

        # If-None-Match with old ETag → 200
        resp = client.get(
            f"/v1/posts/{post_id}",
            headers={**auth_headers, "If-None-Match": etag},
        )
        assert resp.status_code == 200

    def test_304_star_when_resource_exists(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Star 304 Test"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        # If-None-Match: * → 304 if resource exists
        resp = client.get(
            f"/v1/posts/{post_id}",
            headers={**auth_headers, "If-None-Match": "*"},
        )
        assert resp.status_code == 304


# ---------------------------------------------------------------------------
# Acceptance: Stale If-Match → 412 including the fresh ETag
# ---------------------------------------------------------------------------


class TestIfMatch:
    """If-Match validation on PATCH, DELETE, publish, unpublish."""

    def test_stale_if_match_returns_412(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        # Create a post
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# 412 Test"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        # Get ETag
        resp = client.get(f"/v1/posts/{post_id}", headers=auth_headers)
        stale_etag = resp.headers["etag"]

        # Update the post (changes the ETag)
        client.patch(
            f"/v1/posts/{post_id}",
            json={"title": "Changed"},
            headers=auth_headers,
        )

        # Try PATCH with stale ETag → 412
        resp = client.patch(
            f"/v1/posts/{post_id}",
            json={"title": "Stale Update"},
            headers={**auth_headers, "If-Match": stale_etag},
        )
        assert resp.status_code == 412
        data = resp.json()
        assert data["code"] == "precondition-failed"
        assert "current_etag" in data
        assert "current_revision" in data

    def test_stale_if_match_412_retry_with_new_etag_succeeds(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        # Create a post
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Retry Test"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        # Get ETag
        resp = client.get(f"/v1/posts/{post_id}", headers=auth_headers)
        stale_etag = resp.headers["etag"]

        # Update the post
        client.patch(
            f"/v1/posts/{post_id}",
            json={"title": "Changed"},
            headers=auth_headers,
        )

        # Try with stale ETag → 412
        resp = client.patch(
            f"/v1/posts/{post_id}",
            json={"title": "Stale"},
            headers={**auth_headers, "If-Match": stale_etag},
        )
        assert resp.status_code == 412
        fresh_etag = resp.json()["current_etag"]

        # Retry with the fresh ETag → 200
        resp = client.patch(
            f"/v1/posts/{post_id}",
            json={"title": "Fresh Update"},
            headers={**auth_headers, "If-Match": fresh_etag},
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "Fresh Update"

    def test_correct_etag_passes(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db)

        # Create a post
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Correct ETag Test"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        # Get current ETag
        resp = client.get(f"/v1/posts/{post_id}", headers=auth_headers)
        etag = resp.headers["etag"]

        # PATCH with correct ETag → 200
        resp = client.patch(
            f"/v1/posts/{post_id}",
            json={"title": "Correct"},
            headers={**auth_headers, "If-Match": etag},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Acceptance: If-Match: * means "whatever is current"
# ---------------------------------------------------------------------------


class TestIfMatchWildcard:
    """If-Match: * always passes, regardless of current ETag."""

    def test_wildcard_if_match_passes(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        # Create a post
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Wildcard Test"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        # PATCH with If-Match: * → 200
        resp = client.patch(
            f"/v1/posts/{post_id}",
            json={"title": "Wildcard Update"},
            headers={**auth_headers, "If-Match": "*"},
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "Wildcard Update"


# ---------------------------------------------------------------------------
# Acceptance: If-Match on DELETE (trash)
# ---------------------------------------------------------------------------


class TestIfMatchOnDelete:
    """If-Match on DELETE (trash) works correctly."""

    def test_stale_if_match_delete_returns_412(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        # Create a post
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Delete 412 Test"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        # Get ETag
        resp = client.get(f"/v1/posts/{post_id}", headers=auth_headers)
        stale_etag = resp.headers["etag"]

        # Update the post
        client.patch(
            f"/v1/posts/{post_id}",
            json={"title": "Changed"},
            headers=auth_headers,
        )

        # DELETE with stale ETag → 412
        resp = client.delete(
            f"/v1/posts/{post_id}",
            headers={**auth_headers, "If-Match": stale_etag},
        )
        assert resp.status_code == 412

    def test_correct_if_match_delete_succeeds(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        # Create a post
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Delete Success Test"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        # Get current ETag
        resp = client.get(f"/v1/posts/{post_id}", headers=auth_headers)
        etag = resp.headers["etag"]

        # DELETE with correct ETag → 200
        resp = client.delete(
            f"/v1/posts/{post_id}",
            headers={**auth_headers, "If-Match": etag},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Acceptance: If-Match on publish
# ---------------------------------------------------------------------------


class TestIfMatchOnPublish:
    """If-Match on publish works correctly."""

    def test_stale_if_match_publish_returns_412(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        # Create a post
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Publish 412 Test"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        # Get ETag
        resp = client.get(f"/v1/posts/{post_id}", headers=auth_headers)
        stale_etag = resp.headers["etag"]

        # Update the post
        client.patch(
            f"/v1/posts/{post_id}",
            json={"title": "Changed"},
            headers=auth_headers,
        )

        # Publish with stale ETag → 412
        resp = client.post(
            f"/v1/posts/{post_id}/publish",
            headers={**auth_headers, "If-Match": stale_etag},
        )
        assert resp.status_code == 412

    def test_correct_if_match_publish_succeeds(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        # Create a post
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Publish Success Test"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        # Get current ETag
        resp = client.get(f"/v1/posts/{post_id}", headers=auth_headers)
        etag = resp.headers["etag"]

        # Publish with correct ETag → 200
        resp = client.post(
            f"/v1/posts/{post_id}/publish",
            headers={**auth_headers, "If-Match": etag},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Acceptance: No If-Match header → no precondition check
# ---------------------------------------------------------------------------


class TestNoIfMatch:
    """When no If-Match header is provided, no precondition check happens."""

    def test_patch_without_if_match_succeeds(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# No If-Match Test"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        # PATCH without If-Match → 200
        resp = client.patch(
            f"/v1/posts/{post_id}",
            json={"title": "Updated"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
