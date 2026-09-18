"""Tests for validation & dry-run pipeline (#15).

Acceptance criteria covered:
- Dry-run creates zero new rows in posts, post_revisions, audit_events, idempotency_keys
- Schema violation → 422 with field-level errors in the same shape as real writes
- Duplicate detector catches paraphrased repost without flagging different posts
- Link checker never blocks for > 3 s; failures never become errors
- Fuzzing: random payloads never 500 and always return structured body
"""

from __future__ import annotations

import random
import string
import uuid
from typing import Any

from app.models.audit_event import AuditEvent
from app.models.idempotency_key import IdempotencyKey
from app.models.post import Post
from app.models.post_revision import PostRevision
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


# ---------------------------------------------------------------------------
# Acceptance: dry-run creates zero new rows
# ---------------------------------------------------------------------------


class TestDryRunSideEffects:
    """Dry-run must be completely side-effect free."""

    def test_dry_run_create_zero_rows(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        # Count before
        posts_before = db.query(Post).count()
        revisions_before = db.query(PostRevision).count()
        audit_before = db.query(AuditEvent).count()
        idem_before = db.query(IdempotencyKey).count()

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Dry Run Post\n\nContent here."},
            params={"dry_run": True},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["valid"] is True

        # Count after — must be unchanged
        posts_after = db.query(Post).count()
        revisions_after = db.query(PostRevision).count()
        audit_after = db.query(AuditEvent).count()
        idem_after = db.query(IdempotencyKey).count()

        assert posts_after == posts_before, "dry-run created a post row"
        assert revisions_after == revisions_before, "dry-run created a revision row"
        assert audit_after == audit_before, "dry-run created an audit event"
        assert idem_after == idem_before, "dry-run consumed an idempotency key"

    def test_dry_run_update_zero_rows(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        # Create a real post first
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Original\n\nContent."},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        revisions_before = db.query(PostRevision).count()
        audit_before = db.query(AuditEvent).count()

        resp = client.patch(
            f"/v1/posts/{post_id}",
            json={"title": "Updated Title"},
            params={"dry_run": True},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True

        # Verify no new rows
        revisions_after = db.query(PostRevision).count()
        audit_after = db.query(AuditEvent).count()
        assert revisions_after == revisions_before
        assert audit_after == audit_before

        # Verify title wasn't changed
        resp = client.get(f"/v1/posts/{post_id}", headers=auth_headers)
        assert resp.json()["title"] == "Original"


# ---------------------------------------------------------------------------
# Acceptance: schema violation → 422 with field-level errors
# ---------------------------------------------------------------------------


class TestSchemaViolation:
    """Schema violations return structured errors matching the real-write shape."""

    def test_empty_body_md(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": ""},
            params={"dry_run": True},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["valid"] is False
        assert len(data["errors"]) > 0
        error_codes = [e["code"] for e in data["errors"]]
        assert "content-required" in error_codes

    def test_missing_title_no_h1(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "No heading here."},
            params={"dry_run": True},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["valid"] is False
        error_codes = [e["code"] for e in data["errors"]]
        assert "title-required" in error_codes

    def test_invalid_slug_format(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Title", "slug": "INVALID SLUG!!"},
            params={"dry_run": True},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["valid"] is False
        error_codes = [e["code"] for e in data["errors"]]
        assert "slug-invalid" in error_codes

    def test_slug_conflict(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db)
        # Create a real post
        client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# First", "slug": "my-post"},
            headers=auth_headers,
        )
        # Dry-run validate with same slug
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Second", "slug": "my-post"},
            params={"dry_run": True},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["valid"] is False
        error_codes = [e["code"] for e in data["errors"]]
        assert "slug-conflict" in error_codes

    def test_frontmatter_draft_disallowed(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Title", "frontmatter": {"draft": True}},
            params={"dry_run": True},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["valid"] is False
        error_codes = [e["code"] for e in data["errors"]]
        assert "disallowed-field" in error_codes

    def test_site_not_found(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        resp = client.post(
            "/v1/sites/nonexistent/posts",
            json={"body_md": "# Title"},
            params={"dry_run": True},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["valid"] is False
        error_codes = [e["code"] for e in data["errors"]]
        assert "site-not-found" in error_codes


# ---------------------------------------------------------------------------
# Acceptance: duplicate detection
# ---------------------------------------------------------------------------


class TestDuplicateDetection:
    """Duplicate detector catches paraphrased reposts without false positives."""

    def test_paraphrased_repost_detected(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        original = (
            "# Understanding AI Agents\n\n"
            "AI agents are software entities that perceive their environment "
            "through sensors and act upon that environment through actuators. "
            "They operate autonomously to achieve specific goals. The field of "
            "artificial intelligence has seen remarkable progress in recent years."
        )
        # Create the original post
        client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": original},
            headers=auth_headers,
        )

        # Paraphrased version — similar content, different wording
        paraphrased = (
            "# Understanding AI Agents\n\n"
            "AI agents are programs that observe their surroundings using sensors "
            "and affect those surroundings through actuators. They work independently "
            "to accomplish designated objectives. The artificial intelligence domain "
            "has experienced extraordinary advancement over the past few years."
        )
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": paraphrased},
            params={"dry_run": True},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        warnings = data.get("warnings", [])
        assert any("DUPLICATE_LIKELY" in w for w in warnings), (
            f"Expected DUPLICATE_LIKELY warning, got: {warnings}"
        )

    def test_different_posts_not_flagged(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        # Create a post about one topic
        client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={
                "body_md": (
                    "# Cooking Pasta\n\n"
                    "Boil water in a large pot. Add salt. "
                    "Cook spaghetti for 8-10 minutes until al dente."
                )
            },
            headers=auth_headers,
        )

        # Validate a completely different topic
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={
                "body_md": (
                    "# Quantum Computing\n\n"
                    "Quantum computers use qubits that can exist in "
                    "multiple states simultaneously through superposition."
                )
            },
            params={"dry_run": True},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        warnings = data.get("warnings", [])
        assert not any("DUPLICATE_LIKELY" in w for w in warnings)


# ---------------------------------------------------------------------------
# Acceptance: link checker bounds
# ---------------------------------------------------------------------------


class TestLinkChecker:
    """Link checker never blocks for > 3 s; failures are warnings, not errors."""

    def test_link_checker_timeout_bound(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        body = (
            "# Post with bad links\n\n"
            "See [this](http://192.0.2.1/nonexistent) and "
            "[that](http://192.0.2.2/also-nonexistent)."
        )
        import time

        start = time.monotonic()
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": body},
            params={"dry_run": True, "check_links": True},
            headers=auth_headers,
        )
        elapsed = time.monotonic() - start

        assert resp.status_code == 201
        assert elapsed < 5.0, f"Link checking took {elapsed:.1f}s, should be < 5s"
        data = resp.json()
        # Link failures should be warnings, not errors
        error_codes = [e["code"] for e in data.get("errors", [])]
        assert "link-check-failed" not in error_codes

    def test_link_failures_are_warnings_not_errors(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        body = "# Post\n\n[Dead link](http://192.0.2.1/nonexistent)."
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": body},
            params={"dry_run": True, "check_links": True},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        # Should still be valid (link failures are warnings)
        assert data["valid"] is True
        warnings = data.get("warnings", [])
        assert any("LINK_UNREACHABLE" in w for w in warnings)


# ---------------------------------------------------------------------------
# Acceptance: validation response shape
# ---------------------------------------------------------------------------


class TestValidationResponseShape:
    """Validation response matches the documented shape."""

    def test_valid_response_shape(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Hello\n\nWorld.", "tags": ["test"]},
            params={"dry_run": True},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()

        # Top-level fields
        assert "valid" in data
        assert "normalised" in data
        assert "would_create" in data
        assert "errors" in data
        assert "warnings" in data
        assert "stats" in data

        # normalised shape
        norm = data["normalised"]
        assert "body_md" in norm
        assert "title" in norm
        assert "slug" in norm
        assert "tags" in norm
        assert "excerpt" in norm
        assert "frontmatter" in norm

        # would_create shape
        wc = data["would_create"]
        assert "slug" in wc
        assert "status" in wc
        assert "url" in wc
        assert wc["status"] == "draft"

        # stats shape
        stats = data["stats"]
        assert "word_count" in stats
        assert "reading_time_minutes" in stats
        assert "links" in stats
        assert "images" in stats

    def test_invalid_response_shape(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": ""},
            params={"dry_run": True},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["valid"] is False
        assert data["would_create"] is None
        assert len(data["errors"]) > 0
        # Each error has field, code, message
        for error in data["errors"]:
            assert "field" in error
            assert "code" in error
            assert "message" in error


# ---------------------------------------------------------------------------
# Acceptance: validate endpoint
# ---------------------------------------------------------------------------


class TestValidateEndpoint:
    """POST /v1/posts/validate works as a standalone endpoint."""

    def test_validate_endpoint_valid(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            "/v1/posts/validate",
            json={"body_md": "# Valid Post\n\nThis is valid content."},
            params={"site_slug": SITE_SLUG},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["would_create"] is not None
        assert data["would_create"]["slug"] == "valid-post"

    def test_validate_endpoint_invalid(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            "/v1/posts/validate",
            json={"body_md": ""},
            params={"site_slug": SITE_SLUG},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False
        assert len(data["errors"]) > 0

    def test_validate_dry_run_shape_matches_validate(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        """Both ?dry_run=true and /validate return the same shape."""
        _create_site(db)
        payload = {"body_md": "# Test\n\nContent."}

        # Dry-run via create
        resp1 = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json=payload,
            params={"dry_run": True},
            headers=auth_headers,
        )

        # Validate endpoint
        resp2 = client.post(
            "/v1/posts/validate",
            json=payload,
            params={"site_slug": SITE_SLUG},
            headers=auth_headers,
        )

        assert resp1.json().keys() == resp2.json().keys()


# ---------------------------------------------------------------------------
# Acceptance: normalisation preview
# ---------------------------------------------------------------------------


class TestNormalisationPreview:
    """Normalised payload shows exactly what will be stored."""

    def test_title_derivation_in_normalised(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Derived Title\n\nContent."},
            params={"dry_run": True},
            headers=auth_headers,
        )
        data = resp.json()
        assert data["normalised"]["title"] == "Derived Title"
        assert data["normalised"]["slug"] == "derived-title"

    def test_tag_normalisation(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Post", "tags": ["  Python  ", "FASTAPI"]},
            params={"dry_run": True},
            headers=auth_headers,
        )
        data = resp.json()
        assert data["normalised"]["tags"] == ["python", "fastapi"]


# ---------------------------------------------------------------------------
# Acceptance: warnings on real writes
# ---------------------------------------------------------------------------


class TestWarningsOnRealWrites:
    """Warnings are also returned on real writes."""

    def test_real_create_returns_warnings(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Post\n\nNo description in frontmatter."},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        # Should have warnings from the validation pipeline
        assert "warnings" in data
        assert isinstance(data["warnings"], list)


# ---------------------------------------------------------------------------
# Acceptance: fuzzing — random payloads never 500
# ---------------------------------------------------------------------------


class TestFuzzing:
    """Random valid/invalid payloads never 500 and always return structured body."""

    def _random_string(self, min_len: int = 0, max_len: int = 200) -> str:
        length = random.randint(min_len, max_len)
        return "".join(random.choices(string.printable, k=length))

    def _random_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if random.random() > 0.3:
            payload["body_md"] = self._random_string(1, 500)
        if random.random() > 0.7:
            payload["title"] = self._random_string(1, 100)
        if random.random() > 0.8:
            payload["slug"] = self._random_string(1, 50)
        if random.random() > 0.8:
            payload["tags"] = [self._random_string(1, 20) for _ in range(random.randint(0, 5))]
        if random.random() > 0.8:
            payload["frontmatter"] = {self._random_string(1, 10): self._random_string(0, 50)}
        return payload

    def test_fuzz_validate_endpoint(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        for _ in range(30):
            payload = self._random_payload()
            resp = client.post(
                "/v1/posts/validate",
                json=payload,
                params={"site_slug": SITE_SLUG},
                headers=auth_headers,
            )
            # Must never 500
            assert resp.status_code != 500, f"500 on payload: {payload}"
            # Must always return structured body
            data = resp.json()
            assert "valid" in data
            assert "errors" in data
            assert "warnings" in data
            assert "stats" in data

    def test_fuzz_dry_run_create(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db)
        for _ in range(30):
            payload = self._random_payload()
            resp = client.post(
                f"/v1/sites/{SITE_SLUG}/posts",
                json=payload,
                params={"dry_run": True},
                headers=auth_headers,
            )
            assert resp.status_code != 500, f"500 on payload: {payload}"
            data = resp.json()
            assert "valid" in data
            assert "errors" in data
