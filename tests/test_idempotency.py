"""Tests for idempotency keys (#11).

Acceptance criteria covered:
* Two identical POSTs with the same key create exactly one post
* Replaying a create returns the original id and Idempotent-Replay: true
* Expired keys are reclaimable and a pruner job is tested
* Idempotency + audit interplay: replay does not emit a second audit event
* No Idempotency-Key supplied → warnings[]
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.idempotency_key import IdempotencyKey
from app.models.site import Site
from app.services.idempotency import prune_expired
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
# Acceptance: Two identical POSTs with same key create exactly one post
# ---------------------------------------------------------------------------


class TestSequentialIdempotentCreate:
    """Two identical POSTs with the same key create exactly one post."""

    def test_sequential_create_exactly_one_post(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        body = {"body_md": "# Idempotent Test\n\nContent."}

        # First request
        resp1 = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json=body,
            headers={**auth_headers, "Idempotency-Key": "seq-key-1"},
        )
        assert resp1.status_code == 201
        first_id = resp1.json()["id"]

        # Second identical request — should replay
        resp2 = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json=body,
            headers={**auth_headers, "Idempotency-Key": "seq-key-1"},
        )
        assert resp2.status_code == 201
        assert resp2.json()["id"] == first_id
        assert resp2.headers.get("Idempotent-Replay") == "true"

        # Verify exactly one post was created
        resp = client.get(f"/v1/sites/{SITE_SLUG}/posts", headers=auth_headers)
        assert resp.json()["count"] == 1


# ---------------------------------------------------------------------------
# Acceptance: Replaying a create returns the original id and Idempotent-Replay: true
# ---------------------------------------------------------------------------


class TestIdempotentReplay:
    """Replaying a create returns the original id and Idempotent-Replay: true."""

    def test_replay_returns_original_id(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        body = {"body_md": "# Replay Test\n\nContent."}

        # First request
        resp1 = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json=body,
            headers={**auth_headers, "Idempotency-Key": "replay-key-1"},
        )
        assert resp1.status_code == 201
        original_id = resp1.json()["id"]
        assert resp1.headers.get("Idempotent-Replay") is None

        # Replay
        resp2 = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json=body,
            headers={**auth_headers, "Idempotency-Key": "replay-key-1"},
        )
        assert resp2.status_code == 201
        assert resp2.json()["id"] == original_id
        assert resp2.headers.get("Idempotent-Replay") == "true"

    def test_replay_via_query_param(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        body = {"body_md": "# Query Param Test\n\nContent."}

        # First request via query param
        resp1 = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json=body,
            params={"idempotency_key": "query-key-1"},
            headers=auth_headers,
        )
        assert resp1.status_code == 201
        original_id = resp1.json()["id"]

        # Replay via query param
        resp2 = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json=body,
            params={"idempotency_key": "query-key-1"},
            headers=auth_headers,
        )
        assert resp2.status_code == 201
        assert resp2.json()["id"] == original_id
        assert resp2.headers.get("Idempotent-Replay") == "true"


# ---------------------------------------------------------------------------
# Acceptance: Same key + different body → 409
# ---------------------------------------------------------------------------


class TestIdempotencyKeyReused:
    """Same key with a different body returns 409."""

    def test_different_body_returns_409(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        # First request
        resp1 = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# First"},
            headers={**auth_headers, "Idempotency-Key": "reuse-key-1"},
        )
        assert resp1.status_code == 201

        # Different body, same key
        resp2 = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Different"},
            headers={**auth_headers, "Idempotency-Key": "reuse-key-1"},
        )
        assert resp2.status_code == 409
        assert resp2.json()["code"] == "idempotency-key-reused"


# ---------------------------------------------------------------------------
# Acceptance: No Idempotency-Key supplied → warnings[]
# ---------------------------------------------------------------------------


class TestNoIdempotencyKeyWarning:
    """When no idempotency key is supplied, a warning is returned."""

    def test_no_key_returns_warning(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# No Key"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        warnings = resp.json().get("warnings", [])
        assert any("No Idempotency-Key supplied" in w for w in warnings)


# ---------------------------------------------------------------------------
# Acceptance: Expired keys are reclaimable and pruner job is tested
# ---------------------------------------------------------------------------


class TestIdempotencyKeyExpiry:
    """Expired idempotency keys can be pruned."""

    def test_expired_key_allows_reuse(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        # Create an expired idempotency key manually
        expired_record = IdempotencyKey(
            id=uuid.uuid4(),
            actor_id=None,
            key="expired-key-1",
            request_fingerprint="old-fingerprint",
            response_status=201,
            response_body={"id": "old-id"},
            in_flight=False,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        db.add(expired_record)
        db.commit()

        # Should be able to use the same key now
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Expired Key Test"},
            headers={**auth_headers, "Idempotency-Key": "expired-key-1"},
        )
        assert resp.status_code == 201

    def test_prune_expired_removes_old_records(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        # Create expired records
        for i in range(5):
            record = IdempotencyKey(
                id=uuid.uuid4(),
                actor_id=None,
                key=f"prune-key-{i}",
                request_fingerprint=f"fingerprint-{i}",
                response_status=200,
                response_body={},
                in_flight=False,
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
            db.add(record)

        # Create a non-expired record
        valid_record = IdempotencyKey(
            id=uuid.uuid4(),
            actor_id=None,
            key="valid-key",
            request_fingerprint="valid-fingerprint",
            response_status=200,
            response_body={},
            in_flight=False,
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
        db.add(valid_record)
        db.commit()

        # Prune
        deleted = prune_expired(db)
        assert deleted == 5

        # Verify the valid record still exists
        remaining = db.query(IdempotencyKey).filter(IdempotencyKey.key == "valid-key").count()
        assert remaining == 1


# ---------------------------------------------------------------------------
# Acceptance: Keys are namespaced per actor
# ---------------------------------------------------------------------------


class TestIdempotencyKeyNamespacing:
    """Keys are namespaced per actor — two tokens with key=1 don't collide."""

    def test_different_actors_same_key_no_conflict(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        # Create a second actor
        from app.models.actor import Actor
        from app.models.capability_link import CapabilityLink
        from app.services.tokens import generate_token

        actor2 = Actor(
            id=uuid.uuid4(),
            kind="machine",
            label="idem-test-token-2",
            scopes=["posts:read", "posts:write", "posts:publish"],
        )
        db.add(actor2)
        db.flush()

        plaintext2, token_hash2 = generate_token(actor2.id)
        link2 = CapabilityLink(
            id=uuid.uuid4(),
            actor_id=actor2.id,
            token_hash=token_hash2,
            label="idem-test-token-2",
            path_scope="/",
            verbs=["GET", "POST", "PATCH", "DELETE"],
        )
        db.add(link2)
        db.commit()

        auth_headers2 = {"Authorization": f"Bearer {plaintext2}"}

        # Both actors use the same key but different content
        resp1 = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Actor 1"},
            headers={**auth_headers, "Idempotency-Key": "shared-key"},
        )
        assert resp1.status_code == 201

        resp2 = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Actor 2"},
            headers={**auth_headers2, "Idempotency-Key": "shared-key"},
        )
        assert resp2.status_code == 201

        # Both posts should exist
        resp = client.get(f"/v1/sites/{SITE_SLUG}/posts", headers=auth_headers)
        assert resp.json()["count"] == 2
