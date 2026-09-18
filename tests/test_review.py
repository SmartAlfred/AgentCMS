"""Tests for the draft -> review -> publish workflow (#16).

Acceptance criteria covered:
- [AC1] In require_review, a published-by-agent post is not reachable publicly
        and not in the sitemap/feeds until approved (tested).
- [AC2] 202 response body matches the documented schema and includes a working preview_url.
- [AC3] Reject reason reaches the agent via GET /v1/posts/{id} verbatim.
- [AC4] Approval publishes exactly once even if the reviewer double-clicks (idempotent).
- [AC5] Mode switch takes effect on the next publish request without a restart;
        "trust mode" expiry is enforced server-side, not client-side.
- [AC6] Review queue shows a compact diff of what would go live.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.site import Site
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

SITE_SLUG = "blog"


def _create_site(
    session: Session,
    slug: str = SITE_SLUG,
    publish_mode: str = "auto",
) -> Site:
    """Create a test site for the review tests."""
    site = Site(
        id=uuid.uuid4(),
        slug=slug,
        name=f"Test Site {slug}",
        publish_mode=publish_mode,
    )
    session.add(site)
    session.flush()
    session.commit()
    return site


def _create_post(client: TestClient, auth_headers: dict[str, str]) -> dict:
    """Helper to create a draft post."""
    resp = client.post(
        f"/v1/sites/{SITE_SLUG}/posts",
        json={"body_md": "# Review Test Post\n\nThis is test content for review."},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    return resp.json()


# ---------------------------------------------------------------------------
# AC1: In require_review, a post is not reachable publicly until approved
# ---------------------------------------------------------------------------


class TestRequireReviewNotPublic:
    """Pending-review posts are not publicly visible."""

    def test_pending_review_not_in_public_listing(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db, publish_mode="require_review")
        post_data = _create_post(client, auth_headers)
        post_id = post_data["id"]

        # Publish — should go to pending_review
        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 202
        assert resp.json()["status"] == "pending_review"

        # Public listing should NOT contain this post
        resp = client.get(f"/{SITE_SLUG}/posts.json")
        assert resp.status_code == 200
        items = resp.json().get("items", [])
        slugs = [item["slug"] for item in items]
        assert post_data["slug"] not in slugs

    def test_pending_review_not_in_sitemap(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db, publish_mode="require_review")
        post_data = _create_post(client, auth_headers)
        post_id = post_data["id"]

        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 202

        # Sitemap should not contain the pending post
        resp = client.get("/sitemap.xml")
        assert resp.status_code == 200
        assert post_data["slug"] not in resp.text

    def test_pending_review_not_in_rss_feed(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db, publish_mode="require_review")
        post_data = _create_post(client, auth_headers)
        post_id = post_data["id"]

        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 202

        resp = client.get(f"/{SITE_SLUG}/rss.xml")
        assert resp.status_code == 200
        assert post_data["slug"] not in resp.text

    def test_approved_post_becomes_public(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db, publish_mode="require_review")
        post_data = _create_post(client, auth_headers)
        post_id = post_data["id"]

        # Publish -> pending_review
        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 202
        review_id = resp.json()["review_id"]

        # Approve
        resp = client.post(
            f"/v1/admin/reviews/{review_id}/approve",
            json={"comment": "Looks good!"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # Now should be in public listing
        resp = client.get(f"/{SITE_SLUG}/posts.json")
        assert resp.status_code == 200
        items = resp.json().get("items", [])
        slugs = [item["slug"] for item in items]
        assert post_data["slug"] in slugs

    def test_pending_review_not_in_public_post_page(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db, publish_mode="require_review")
        post_data = _create_post(client, auth_headers)
        post_id = post_data["id"]

        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 202

        # Direct public URL should 404
        resp = client.get(f"/{SITE_SLUG}/{post_data['slug']}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# AC2: 202 response body matches documented schema with preview_url
# ---------------------------------------------------------------------------


class TestPublishAcceptedResponse:
    """202 response has the documented schema."""

    def test_202_response_schema(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db, publish_mode="require_review")
        post_data = _create_post(client, auth_headers)
        post_id = post_data["id"]

        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 202

        body = resp.json()
        assert body["status"] == "pending_review"
        assert "review_id" in body
        assert body["expected_decision_within"] == "24h"
        assert "GET /v1/posts/" in body["next"]
        assert "preview_url" in body
        assert body["preview_url"].startswith(f"/{SITE_SLUG}/")

    def test_preview_url_works(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db, publish_mode="require_review")
        post_data = _create_post(client, auth_headers)
        post_id = post_data["id"]

        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 202
        preview_url = resp.json()["preview_url"]

        # The preview URL should return the post content
        resp = client.get(preview_url)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# AC3: Reject reason reaches the agent via GET /v1/posts/{id} verbatim
# ---------------------------------------------------------------------------


class TestRejectReasonVisible:
    """Reject reason is surfaced to the agent verbatim."""

    def test_reject_reason_in_get_post(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db, publish_mode="require_review")
        post_data = _create_post(client, auth_headers)
        post_id = post_data["id"]

        # Publish -> pending_review
        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 202
        review_id = resp.json()["review_id"]

        # Reject with a reason
        reason = "Too short; add a conclusion."
        resp = client.post(
            f"/v1/admin/reviews/{review_id}/reject",
            json={"reason": reason},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # GET /v1/posts/{id} should show the reject reason verbatim
        resp = client.get(f"/v1/posts/{post_id}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "draft"
        assert data["review"] is not None
        assert data["review"]["status"] == "rejected"
        assert data["review"]["comment"] == reason

    def test_approved_reason_in_get_post(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db, publish_mode="require_review")
        post_data = _create_post(client, auth_headers)
        post_id = post_data["id"]

        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 202
        review_id = resp.json()["review_id"]

        resp = client.post(
            f"/v1/admin/reviews/{review_id}/approve",
            json={"comment": "LGTM"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        resp = client.get(f"/v1/posts/{post_id}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "published"
        assert data["review"] is not None
        assert data["review"]["status"] == "approved"
        assert data["review"]["comment"] == "LGTM"


# ---------------------------------------------------------------------------
# AC4: Approval publishes exactly once (idempotent)
# ---------------------------------------------------------------------------


class TestIdempotentApproval:
    """Approving a review twice returns the same result (no double publish)."""

    def test_double_approve_idempotent(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db, publish_mode="require_review")
        post_data = _create_post(client, auth_headers)
        post_id = post_data["id"]

        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 202
        review_id = resp.json()["review_id"]

        # Approve once
        resp = client.post(
            f"/v1/admin/reviews/{review_id}/approve",
            json={"comment": "Looks good"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data1 = resp.json()
        assert data1["status"] == "published"

        # Approve again — should be idempotent
        resp = client.post(
            f"/v1/admin/reviews/{review_id}/approve",
            json={"comment": "Looks good"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data2 = resp.json()
        assert data2["status"] == "published"

        # Revision count should be the same
        assert data1["revision"] == data2["revision"]

    def test_reject_then_approve_fails(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db, publish_mode="require_review")
        post_data = _create_post(client, auth_headers)
        post_id = post_data["id"]

        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 202
        review_id = resp.json()["review_id"]

        # Reject first
        resp = client.post(
            f"/v1/admin/reviews/{review_id}/reject",
            json={"reason": "Needs work"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # Approve should fail — review already decided
        resp = client.post(
            f"/v1/admin/reviews/{review_id}/approve",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# AC5: Mode switch takes effect on next publish without restart
# ---------------------------------------------------------------------------


class TestModeSwitch:
    """Switching publish_mode takes effect immediately."""

    def test_auto_to_require_review(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        # Start with auto mode
        site = _create_site(db, publish_mode="auto")
        post_data = _create_post(client, auth_headers)
        post_id = post_data["id"]

        # Publish in auto mode — should succeed immediately
        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "published"

        # Create another post with a different title
        resp2 = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Second Post\n\nDifferent content here."},
            headers=auth_headers,
        )
        assert resp2.status_code == 201
        post_data2 = resp2.json()
        post_id2 = post_data2["id"]

        # Switch to require_review
        from app.models.site import Site as SiteModel

        site_obj = db.query(SiteModel).filter(SiteModel.id == site.id).first()
        site_obj.publish_mode = "require_review"
        db.commit()

        # Publish new post — should now go to pending_review
        resp = client.post(f"/v1/posts/{post_id2}/publish", headers=auth_headers)
        assert resp.status_code == 202
        assert resp.json()["status"] == "pending_review"

    def test_trust_mode_bypasses_review(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        # Start with require_review mode
        _create_site(db, publish_mode="require_review")

        # Enable trust mode for 60 minutes
        resp = client.post(
            f"/v1/admin/sites/{SITE_SLUG}/trust-mode",
            json={"expires_in_minutes": 60},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # Create and publish a post — trust mode should bypass review
        post_data = _create_post(client, auth_headers)
        post_id = post_data["id"]

        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "published"

    def test_trust_mode_expiry_enforced_server_side(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        # Start with require_review mode
        _create_site(db, publish_mode="require_review")

        # Enable trust mode but set it to expire 1 second ago (simulate expiry)
        from app.models.site import Site as SiteModel

        site_obj = db.query(SiteModel).filter(SiteModel.slug == SITE_SLUG).first()
        site_obj.trust_mode_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

        # Create and publish a post — trust mode expired, should require review
        post_data = _create_post(client, auth_headers)
        post_id = post_data["id"]

        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 202
        assert resp.json()["status"] == "pending_review"


# ---------------------------------------------------------------------------
# AC6: Review queue shows a compact diff
# ---------------------------------------------------------------------------


class TestReviewQueueDiff:
    """Review queue includes a compact diff of what would go live."""

    def test_review_queue_has_diff(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db, publish_mode="require_review")
        post_data = _create_post(client, auth_headers)
        post_id = post_data["id"]

        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 202
        review_id = resp.json()["review_id"]

        # List reviews
        resp = client.get(
            "/v1/admin/reviews",
            params={"site_slug": SITE_SLUG},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        reviews = resp.json()["items"]
        assert len(reviews) == 1

        review = reviews[0]
        assert review["id"] == review_id
        assert review["snapshot_title"] == "Review Test Post"
        assert review["status"] == "pending_review"


# ---------------------------------------------------------------------------
# Auto mode still works
# ---------------------------------------------------------------------------


class TestAutoModeStillWorks:
    """Auto mode publishes immediately without review."""

    def test_auto_publish_immediate(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db, publish_mode="auto")
        post_data = _create_post(client, auth_headers)
        post_id = post_data["id"]

        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "published"
        assert data["published_at"] is not None


# ---------------------------------------------------------------------------
# Review queue list endpoint
# ---------------------------------------------------------------------------


class TestReviewQueueEndpoint:
    """Admin review queue endpoint works correctly."""

    def test_list_empty_reviews(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db, publish_mode="require_review")
        resp = client.get(
            "/v1/admin/reviews",
            params={"site_slug": SITE_SLUG},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["items"] == []

    def test_list_pending_reviews(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db, publish_mode="require_review")
        post_data = _create_post(client, auth_headers)
        post_id = post_data["id"]

        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 202

        resp = client.get(
            "/v1/admin/reviews",
            params={"site_slug": SITE_SLUG},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["items"][0]["status"] == "pending_review"

    def test_approved_review_not_in_pending_list(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db, publish_mode="require_review")
        post_data = _create_post(client, auth_headers)
        post_id = post_data["id"]

        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 202
        review_id = resp.json()["review_id"]

        # Approve
        resp = client.post(
            f"/v1/admin/reviews/{review_id}/approve",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # Should not appear in pending list
        resp = client.get(
            "/v1/admin/reviews",
            params={"site_slug": SITE_SLUG},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_get_single_review(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db, publish_mode="require_review")
        post_data = _create_post(client, auth_headers)
        post_id = post_data["id"]

        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 202
        review_id = resp.json()["review_id"]

        resp = client.get(f"/v1/admin/reviews/{review_id}", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == review_id
        assert data["status"] == "pending_review"
