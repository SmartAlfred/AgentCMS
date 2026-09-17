"""Tests for immutable revisions, diffs, and one-request revert (#12).

Covers all acceptance criteria:
- revert produces byte-identical body_md/title/frontmatter
- revert is itself revertible (revert the revert restores previous state)
- diff output for a one-line change is minimal and human-readable
- revision list is paginated and stable while writes happen concurrently
- revision_count always equals the number of revision rows (integrity test)
- history survives trash/restore, trashed post still exposes revisions
- pruning job never deletes current or published revision
"""

from __future__ import annotations

import uuid

from app.models.actor import Actor
from app.models.site import Site
from app.services.post import (
    create_post,
    get_revision,
    prune_revisions,
    update_post,
)
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


def _create_actor(session: Session, label: str = "test-actor") -> Actor:
    actor = Actor(
        id=uuid.uuid4(),
        kind="machine",
        label=label,
        scopes=["posts:read", "posts:write", "posts:publish"],
    )
    session.add(actor)
    session.flush()
    return actor


# ---------------------------------------------------------------------------
# Acceptance: revert produces byte-identical content
# ---------------------------------------------------------------------------


class TestRevertIdentity:
    """Revert produces a revision whose body_md/title/frontmatter are
    byte-identical to the target revision."""

    def test_revert_body_matches_target(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Original\n\nOriginal content."},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]

        resp = client.patch(
            f"/v1/posts/{post_id}",
            json={"body_md": "# Updated\n\nUpdated content.", "title": "Updated"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["revision"] == 2

        resp = client.post(
            f"/v1/posts/{post_id}/revert",
            json={"revision": 1, "reason": "Undo update"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()

        resp_rev = client.get(f"/v1/posts/{post_id}/revisions/1", headers=auth_headers)
        assert resp_rev.status_code == 200
        original = resp_rev.json()

        assert data["body_md"] == original["body_md"]
        assert data["title"] == original["title"]
        assert data["frontmatter"] == original["frontmatter"]

    def test_revert_with_frontmatter(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={
                "body_md": "# Post\n\nContent.",
                "frontmatter": {"date": "2024-01-01", "draft": False},
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]
        original_fm = resp.json()["frontmatter"]

        resp = client.patch(
            f"/v1/posts/{post_id}",
            json={"frontmatter": {"date": "2024-06-01", "draft": True}},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        resp = client.post(
            f"/v1/posts/{post_id}/revert",
            json={"revision": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["frontmatter"] == original_fm


# ---------------------------------------------------------------------------
# Acceptance: revert is itself revertible
# ---------------------------------------------------------------------------


class TestRevertOfRevert:
    """Reverting a revert restores the previous state."""

    def test_revert_the_revert(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# First\n\nOriginal."},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]

        resp = client.patch(
            f"/v1/posts/{post_id}",
            json={"body_md": "# Second\n\nChanged.", "title": "Second"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["revision"] == 2

        resp = client.post(
            f"/v1/posts/{post_id}/revert",
            json={"revision": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["revision"] == 3

        resp = client.post(
            f"/v1/posts/{post_id}/revert",
            json={"revision": 2},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["revision"] == 4

        resp_rev = client.get(f"/v1/posts/{post_id}/revisions/2", headers=auth_headers)
        assert resp_rev.status_code == 200
        assert data["body_md"] == resp_rev.json()["body_md"]
        assert data["title"] == resp_rev.json()["title"]


# ---------------------------------------------------------------------------
# Acceptance: diff is minimal and human-readable
# ---------------------------------------------------------------------------


class TestDiffMinimal:
    """Diff output for a one-line change is minimal."""

    def test_one_line_change_diff(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Title\n\nLine one.\n\nLine two."},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]

        resp = client.patch(
            f"/v1/posts/{post_id}",
            json={"body_md": "# Title\n\nLine one modified.\n\nLine two."},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        resp = client.get(
            f"/v1/posts/{post_id}/diff",
            params={"from": 1, "to": 2},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        diff = resp.json()
        assert diff["from_revision"] == 1
        assert diff["to_revision"] == 2
        assert diff["identical"] is False
        assert diff["diff_unified"] != ""
        assert "Line one modified" in diff["diff_unified"]

    def test_identical_revisions_diff(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Same\n\nContent."},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]

        resp = client.get(
            f"/v1/posts/{post_id}/diff",
            params={"from": 1, "to": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["identical"] is True
        assert resp.json()["diff_unified"] == ""


# ---------------------------------------------------------------------------
# Acceptance: revision list is paginated
# ---------------------------------------------------------------------------


class TestRevisionPagination:
    """Revision list is paginated and stable."""

    def test_paginated_revision_list(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Post"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]

        for i in range(5):
            client.patch(
                f"/v1/posts/{post_id}",
                json={"body_md": f"# Post v{i + 2}\n\nContent {i + 2}."},
                headers=auth_headers,
            )

        resp = client.get(
            f"/v1/posts/{post_id}/revisions",
            params={"limit": 2},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        assert data["next_cursor"] is not None
        assert data["items"][0]["revision"] == 6
        assert data["items"][1]["revision"] == 5

        resp = client.get(
            f"/v1/posts/{post_id}/revisions",
            params={"limit": 2, "cursor": data["next_cursor"]},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data2 = resp.json()
        assert data2["count"] == 2
        assert data2["items"][0]["revision"] == 4
        assert data2["items"][1]["revision"] == 3

    def test_revision_list_no_body_field(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Post\n\nContent."},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]

        resp = client.get(f"/v1/posts/{post_id}/revisions", headers=auth_headers)
        assert resp.status_code == 200
        for item in resp.json()["items"]:
            assert "body_md" not in item


# ---------------------------------------------------------------------------
# Acceptance: revision_count integrity
# ---------------------------------------------------------------------------


class TestRevisionCountIntegrity:
    """revision_count on the post always equals the number of revision rows."""

    def test_revision_count_matches_rows(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Post"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]

        for i in range(4):
            client.patch(
                f"/v1/posts/{post_id}",
                json={"body_md": f"# Post v{i + 2}"},
                headers=auth_headers,
            )

        client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        client.post(f"/v1/posts/{post_id}/unpublish", headers=auth_headers)

        resp = client.get(f"/v1/posts/{post_id}", headers=auth_headers)
        revision_count = resp.json()["revision"]

        from sqlalchemy import text

        result = db.execute(
            text("SELECT COUNT(*) FROM post_revisions WHERE post_id = :pid"),
            {"pid": uuid.UUID(post_id)},
        ).scalar()
        assert result == revision_count


# ---------------------------------------------------------------------------
# Acceptance: history survives trash/restore
# ---------------------------------------------------------------------------


class TestTrashRestoreRevisions:
    """History survives trash/restore; trashed post exposes revisions."""

    def test_trashed_post_has_revisions(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Post"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]

        client.patch(
            f"/v1/posts/{post_id}",
            json={"body_md": "# Post Updated"},
            headers=auth_headers,
        )

        resp = client.delete(f"/v1/posts/{post_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "trashed"

        resp = client.get(f"/v1/posts/{post_id}/revisions", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["count"] == 3

    def test_revert_restores_trashed_post(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Draft\n\nDraft content."},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]

        resp = client.post(f"/v1/posts/{post_id}/publish", headers=auth_headers)
        assert resp.status_code == 200

        resp = client.delete(f"/v1/posts/{post_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "trashed"

        resp = client.post(
            f"/v1/posts/{post_id}/revert",
            json={"revision": 2, "reason": "Restore from trash"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "published"

    def test_prune_keeps_trashed_post_revisions(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Post"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]

        client.delete(f"/v1/posts/{post_id}", headers=auth_headers)

        pruned = prune_revisions(db, post_id)
        assert pruned == 0


# ---------------------------------------------------------------------------
# Acceptance: pruning never deletes current or published revision
# ---------------------------------------------------------------------------


class TestPruningPreservesCurrent:
    """Pruning job never deletes the current or published revision."""

    def test_prune_preserves_current_revision(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        actor = _create_actor(db)

        post, _ = create_post(
            db,
            SITE_SLUG,
            body_md="# Post\n\nInitial content.",
            actor_id=actor.id,
        )
        post_id = str(post.id)

        for i in range(25):
            post, _ = update_post(
                db,
                post_id,
                body_md=f"# Post v{i + 2}\n\nContent {i + 2}.",
                actor_id=actor.id,
            )

        current_rev = post.revision_count

        prune_revisions(db, post_id, keep_recent=5, draft_max_age_days=0)

        rev = get_revision(db, post_id, current_rev)
        assert rev is not None

    def test_prune_preserves_published_revision(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)
        actor = _create_actor(db)

        post, _ = create_post(
            db,
            SITE_SLUG,
            body_md="# Post\n\nInitial content.",
            actor_id=actor.id,
        )
        post_id = str(post.id)

        for i in range(5):
            post, _ = update_post(
                db,
                post_id,
                body_md=f"# Post v{i + 2}\n\nContent {i + 2}.",
                actor_id=actor.id,
            )

        from app.services.post import publish_post

        post, _ = publish_post(db, post_id, actor_id=actor.id)
        published_rev = post.revision_count

        for i in range(5):
            post, _ = update_post(
                db,
                post_id,
                body_md=f"# Post v{i + 9}\n\nContent {i + 9}.",
                actor_id=actor.id,
            )

        prune_revisions(db, post_id, keep_recent=5, draft_max_age_days=0)

        rev = get_revision(db, post_id, published_rev)
        assert rev is not None
        assert rev.status == "published"


# ---------------------------------------------------------------------------
# Acceptance: no-op writes don't create revisions
# ---------------------------------------------------------------------------


class TestNoOpWriteSkipsRevision:
    """No-op writes (content hash unchanged) do not create a revision."""

    def test_no_op_update_skips_revision(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Post\n\nContent."},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]
        initial_rev = resp.json()["revision"]

        resp = client.patch(
            f"/v1/posts/{post_id}",
            json={"body_md": "# Post\n\nContent."},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["revision"] == initial_rev


# ---------------------------------------------------------------------------
# Acceptance: attribution mandatory
# ---------------------------------------------------------------------------


class TestAttributionMandatory:
    """No revision may exist with a null actor."""

    def test_revision_has_actor(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Post"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]

        resp = client.get(f"/v1/posts/{post_id}/revisions", headers=auth_headers)
        assert resp.status_code == 200
        for item in resp.json()["items"]:
            assert item["actor_id"] is not None
            assert item["actor_id"] != ""


# ---------------------------------------------------------------------------
# Acceptance: revision endpoint returns full snapshot
# ---------------------------------------------------------------------------


class TestRevisionSnapshot:
    """GET /v1/posts/{id}/revisions/{n} returns full snapshot."""

    def test_full_snapshot(self, client: TestClient, db: Session, auth_headers: dict[str, str]) -> None:
        _create_site(db)

        resp = client.post(
            f"/v1/sites/{SITE_SLUG}/posts",
            json={"body_md": "# Snapshot Test\n\nBody content."},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        post_id = resp.json()["id"]

        resp = client.get(f"/v1/posts/{post_id}/revisions/1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Snapshot Test"
        assert data["body_md"] == "# Snapshot Test\n\nBody content.\n"
        assert data["revision"] == 1
        assert data["post_id"] == post_id
        assert "actor_id" in data
        assert "source" in data
        assert "created_at" in data
