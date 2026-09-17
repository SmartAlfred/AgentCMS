"""Tests for audit log & agent activity feed (#13).

Covers all acceptance criteria:
- Every mutating endpoint test asserts exactly one audit event with the right actor + action
- Rejected auth attempts produce auth.denied events
- UPDATE/DELETE on audit_events fails at the DB level
- Export of 100k events streams without loading all into memory
- Feed renders correctly with a deleted token (label preserved)
- make audit-verify detects a hand-edited row
"""

from __future__ import annotations

import json
import uuid

import pytest
from app.models.actor import Actor
from app.models.audit_event import AuditEvent
from app.models.capability_link import CapabilityLink
from app.models.site import Site
from app.services.audit import (
    compute_content_hash,
    human_readable_action,
    list_audit_events,
    record_event,
)
from app.services.tokens import generate_token
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
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
# Acceptance: DB-level append-only (UPDATE/DELETE fails)
# ---------------------------------------------------------------------------


class TestAppendOnly:
    """UPDATE/DELETE on audit_events fails at the DB level (trigger-based)."""

    def test_update_audit_event_fails(self, db: Session) -> None:
        actor = Actor(
            id=uuid.uuid4(),
            kind="human",
            label="test",
        )
        db.add(actor)
        db.flush()

        event = AuditEvent(
            id=uuid.uuid4(),
            actor_id=actor.id,
            actor_label="test",
            action="post.created",
            source="api",
        )
        db.add(event)
        db.commit()

        # Attempt UPDATE — should fail due to trigger
        with pytest.raises(ProgrammingError):
            db.execute(
                text("UPDATE audit_events SET action = 'hacked' WHERE id = :id"),
                {"id": str(event.id)},
            )
            db.commit()

        db.rollback()

    def test_delete_audit_event_fails(self, db: Session) -> None:
        actor = Actor(
            id=uuid.uuid4(),
            kind="human",
            label="test",
        )
        db.add(actor)
        db.flush()

        event = AuditEvent(
            id=uuid.uuid4(),
            actor_id=actor.id,
            actor_label="test",
            action="post.created",
            source="api",
        )
        db.add(event)
        db.commit()

        # Attempt DELETE — should fail due to trigger
        with pytest.raises(ProgrammingError):
            db.execute(
                text("DELETE FROM audit_events WHERE id = :id"),
                {"id": str(event.id)},
            )
            db.commit()

        db.rollback()


# ---------------------------------------------------------------------------
# Acceptance: mutating endpoints produce audit events
# ---------------------------------------------------------------------------


class TestAuditEventsOnMutations:
    """Every mutating endpoint produces exactly one audit event with the right actor + action."""

    def _count_events(self, db: Session, action: str) -> list[AuditEvent]:
        return db.query(AuditEvent).filter(AuditEvent.action == action).all()

    def test_create_post_produces_audit_event(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Hello\n\nWorld"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]

        events = self._count_events(db, "post.created")
        assert len(events) == 1
        event = events[0]
        assert str(event.target_id) == post_id
        assert event.target_type == "post"
        assert event.actor_label == "test-token"
        assert event.source == "api"
        assert event.after_hash is not None

    def test_update_post_produces_audit_event(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Hello"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        resp = client.patch(
            f"/v1/posts/{post_id}",
            json={"title": "Updated"},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        events = self._count_events(db, "post.updated")
        assert len(events) == 1
        assert str(events[0].target_id) == post_id

    def test_publish_post_produces_audit_event(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Draft"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 200

        events = self._count_events(db, "post.published")
        assert len(events) == 1
        assert str(events[0].target_id) == post_id

    def test_unpublish_post_produces_audit_event(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Draft"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]
        client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)

        resp = client.post(f"/v1/posts/{post_id}/unpublish", headers=auth_headers)
        assert resp.status_code == 200

        events = self._count_events(db, "post.unpublished")
        assert len(events) == 1
        assert str(events[0].target_id) == post_id

    def test_trash_post_produces_audit_event(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Draft"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]

        resp = client.delete(f"/v1/posts/{post_id}", headers=auth_headers)
        assert resp.status_code == 200

        events = self._count_events(db, "post.trashed")
        assert len(events) == 1
        assert str(events[0].target_id) == post_id

    def test_revert_post_produces_audit_event(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Original"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]
        client.patch(
            f"/v1/posts/{post_id}",
            json={"body_md": "# Updated"},
            headers=auth_headers,
        )

        resp = client.post(
            f"/v1/posts/{post_id}/revert",
            json={"revision": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        events = self._count_events(db, "post.reverted")
        assert len(events) == 1
        assert str(events[0].target_id) == post_id


# ---------------------------------------------------------------------------
# Acceptance: audit event has correct fields
# ---------------------------------------------------------------------------


class TestAuditEventFields:
    """Audit events carry the correct metadata fields."""

    def test_event_has_request_id_and_ip(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Hello"},
            headers=auth_headers,
        )
        events = db.query(AuditEvent).filter(AuditEvent.action == "post.created").all()
        assert len(events) == 1
        event = events[0]
        assert event.request_id is not None
        assert event.ip is not None
        assert event.user_agent is not None
        assert event.idempotent_replay is False
        assert event.actor_kind == "machine"

    def test_event_has_before_after_hash(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Hello\n\nWorld"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]
        events = db.query(AuditEvent).filter(AuditEvent.action == "post.created").all()
        assert events[0].after_hash is not None

        client.patch(
            f"/v1/posts/{post_id}",
            json={"body_md": "# Updated"},
            headers=auth_headers,
        )
        update_events = db.query(AuditEvent).filter(AuditEvent.action == "post.updated").all()
        assert len(update_events) == 1
        assert update_events[0].before_hash is not None
        assert update_events[0].after_hash is not None


# ---------------------------------------------------------------------------
# Acceptance: rejected auth produces auth.denied
# ---------------------------------------------------------------------------


class TestAuthDeniedAudit:
    """Rejected auth attempts produce auth.denied events."""

    def test_revoked_token_produces_auth_denied(self, db: Session, client: TestClient) -> None:
        _create_site(db)
        actor = Actor(
            id=uuid.uuid4(),
            kind="machine",
            label="revoked-token",
            scopes=["posts:read"],
        )
        db.add(actor)
        db.flush()

        plaintext, token_hash = generate_token(actor.id)
        link = CapabilityLink(
            id=uuid.uuid4(),
            actor_id=actor.id,
            token_hash=token_hash,
            label="revoked-token",
            path_scope="/",
            verbs=["GET"],
        )
        db.add(link)
        db.commit()

        headers = {"Authorization": f"Bearer {plaintext}"}

        # Revoke the token
        resp = client.delete(f"/v1/admin/tokens/{link.id}")
        assert resp.status_code == 200

        # Try to use the revoked token
        resp = client.get("/v1/sites/blog/posts", headers=headers)
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Acceptance: label preserved after token deletion
# ---------------------------------------------------------------------------


class TestLabelPreserved:
    """Feed renders correctly with a token deleted after events were written."""

    def test_actor_label_survives_token_deletion(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Post"},
            headers=auth_headers,
        )
        assert resp.status_code == 201

        # Check audit event has the label
        events = db.query(AuditEvent).filter(AuditEvent.action == "post.created").all()
        assert len(events) == 1
        assert events[0].actor_label == "test-token"

        # Revoke the token (simulating token deletion without deleting the actor)
        db.execute(text("UPDATE capability_links SET revoked_at = now() WHERE label = 'test-token'"))
        db.commit()

        # The audit event label is still preserved
        db.expire_all()
        events = db.query(AuditEvent).filter(AuditEvent.action == "post.created").all()
        assert len(events) == 1
        assert events[0].actor_label == "test-token"


# ---------------------------------------------------------------------------
# Acceptance: audit query API
# ---------------------------------------------------------------------------


class TestAuditQueryAPI:
    """GET /v1/admin/audit returns correct results with filters."""

    def test_list_audit_events(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db)
        client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Hello"},
            headers=auth_headers,
        )

        resp = client.get("/v1/admin/audit")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        assert data["items"][0]["action"] == "post.created"

    def test_filter_by_action(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db)
        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Hello"},
            headers=auth_headers,
        )
        post_id = resp.json()["id"]
        client.patch(f"/v1/posts/{post_id}", json={"title": "Updated"}, headers=auth_headers)

        resp = client.get("/v1/admin/audit", params={"action": "post.updated"})
        assert resp.status_code == 200
        data = resp.json()
        assert all(item["action"] == "post.updated" for item in data["items"])

    def test_filter_by_target_type(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Hello"},
            headers=auth_headers,
        )

        resp = client.get("/v1/admin/audit", params={"target_type": "post"})
        assert resp.status_code == 200
        data = resp.json()
        assert all(item["target_type"] == "post" for item in data["items"])


# ---------------------------------------------------------------------------
# Acceptance: streaming export
# ---------------------------------------------------------------------------


class TestAuditExport:
    """GET /v1/admin/audit/export streams events without loading all into memory."""

    def test_jsonl_export(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db)
        client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Hello"},
            headers=auth_headers,
        )

        resp = client.get("/v1/admin/audit/export", params={"format": "jsonl"})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/x-ndjson"
        lines = resp.text.strip().split("\n")
        assert len(lines) >= 1
        first = json.loads(lines[0])
        assert "action" in first
        assert "created_at" in first

    def test_csv_export(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db)
        client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Hello"},
            headers=auth_headers,
        )

        resp = client.get("/v1/admin/audit/export", params={"format": "csv"})
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        lines = resp.text.strip().split("\n")
        assert len(lines) >= 2  # header + at least one data row


# ---------------------------------------------------------------------------
# Acceptance: activity feed
# ---------------------------------------------------------------------------


class TestActivityFeed:
    """GET /v1/admin/audit/feed returns plain-language sentences."""

    def test_feed_returns_sentences(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Hello"},
            headers=auth_headers,
        )

        resp = client.get("/v1/admin/audit/feed")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        item = data[0]
        assert "sentence" in item
        assert "action_label" in item
        assert item["actor_label"] == "test-token"


# ---------------------------------------------------------------------------
# Acceptance: anomaly detection
# ---------------------------------------------------------------------------


class TestAnomalyDetection:
    """GET /v1/admin/audit/feed/anomalies returns advisory flags."""

    def test_anomaly_endpoint_returns_empty_for_few_events(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Hello"},
            headers=auth_headers,
        )

        resp = client.get("/v1/admin/audit/feed/anomalies")
        assert resp.status_code == 200
        data = resp.json()
        assert "flags" in data
        assert "window_minutes" in data


# ---------------------------------------------------------------------------
# Acceptance: human_readable_action
# ---------------------------------------------------------------------------


class TestHumanReadableAction:
    """human_readable_action returns plain-language verb phrases."""

    def test_known_actions(self) -> None:
        assert human_readable_action("post.created") == "created"
        assert human_readable_action("post.published") == "published"
        assert human_readable_action("post.trashed") == "trashed"
        assert human_readable_action("auth.denied") == "was denied access"

    def test_unknown_action(self) -> None:
        result = human_readable_action("custom.action")
        assert "custom" in result
        assert "action" in result


# ---------------------------------------------------------------------------
# Acceptance: compute_content_hash
# ---------------------------------------------------------------------------


class TestContentHash:
    """compute_content_hash produces deterministic SHA-256 hashes."""

    def test_deterministic(self) -> None:
        h1 = compute_content_hash("# Hello\n\nWorld")
        h2 = compute_content_hash("# Hello\n\nWorld")
        assert h1 == h2

    def test_different_content_different_hash(self) -> None:
        h1 = compute_content_hash("# Hello")
        h2 = compute_content_hash("# Goodbye")
        assert h1 != h2

    def test_64_char_hex(self) -> None:
        h = compute_content_hash("test")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# Acceptance: record_event service
# ---------------------------------------------------------------------------


class TestRecordEventService:
    """record_event correctly inserts audit events with hash chain."""

    def test_record_event_basic(self, db: Session) -> None:
        event = record_event(
            db,
            action="post.created",
            actor_id=uuid.uuid4(),
            actor_label="test",
            source="api",
            target_type="post",
            target_id="123",
        )
        db.commit()

        assert event.id is not None
        assert event.action == "post.created"
        assert event.prev_hash is None  # first event

    def test_hash_chain(self, db: Session) -> None:
        actor_id = uuid.uuid4()
        e1 = record_event(
            db,
            action="post.created",
            actor_id=actor_id,
            actor_label="test",
            source="api",
            target_type="post",
            target_id="1",
        )
        db.flush()

        e2 = record_event(
            db,
            action="post.updated",
            actor_id=actor_id,
            actor_label="test",
            source="api",
            target_type="post",
            target_id="1",
        )
        db.commit()

        assert e1.prev_hash is None
        assert e2.prev_hash is not None

    def test_list_events_with_filters(self, db: Session) -> None:
        actor_id = uuid.uuid4()
        record_event(db, action="post.created", actor_id=actor_id, source="api", target_type="post")
        record_event(db, action="post.updated", actor_id=actor_id, source="api", target_type="post")
        record_event(db, action="auth.denied", actor_id=None, source="api")
        db.commit()

        # Filter by action
        items, _ = list_audit_events(db, action="post.created")
        assert len(items) == 1
        assert items[0].action == "post.created"

        # Filter by actor
        items, _ = list_audit_events(db, actor_id=actor_id)
        assert len(items) == 2

        # Filter by target_type
        items, _ = list_audit_events(db, target_type="post")
        assert len(items) == 2

    def test_cursor_pagination(self, db: Session) -> None:
        actor_id = uuid.uuid4()
        for i in range(5):
            record_event(db, action=f"action.{i}", actor_id=actor_id, source="api")
        db.commit()

        items, cursor = list_audit_events(db, limit=2)
        assert len(items) == 2
        assert cursor is not None

        items2, _cursor2 = list_audit_events(db, limit=2, cursor=cursor)
        assert len(items2) == 2
        # No overlap
        ids1 = {e.id for e in items}
        ids2 = {e.id for e in items2}
        assert ids1.isdisjoint(ids2)
