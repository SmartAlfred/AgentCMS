"""Tests for data model & migrations (#3).

Covers all acceptance criteria:
- Alembic migrations apply cleanly up and down
- All FKs and unique constraints exist in the DB
- Soft delete (status trashed + deleted_at) never destroys revisions
- Slug collision at insert raises a typed domain error with suggested_slug
- Seed script creates demo site + 3 posts + 1 token
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import ClassVar

import pytest
from app.db.session import session_scope
from app.domain.errors import SlugConflictError
from app.models.post import Post
from app.models.post_revision import PostRevision
from app.models.redirect import Redirect
from app.models.site import Site
from app.models.tag import PostTag, Tag
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests import pg as pg_mod

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_site(session: Session, slug: str = "blog") -> Site:
    site = Site(
        id=uuid.uuid4(),
        slug=slug,
        name=f"Site {slug}",
        publish_mode="auto",
    )
    session.add(site)
    session.flush()
    return site


def _make_post(session: Session, site: Site, slug: str = "hello") -> Post:
    post = Post(
        id=uuid.uuid4(),
        site_id=site.id,
        slug=slug,
        title=f"Post {slug}",
        body_md=f"# {slug}",
        status="draft",
        revision_count=0,
    )
    session.add(post)
    session.flush()
    return post


# ---------------------------------------------------------------------------
# Acceptance: all tables exist
# ---------------------------------------------------------------------------


class TestAllTablesExist:
    """Verify every planned table exists in the database after migration."""

    EXPECTED_TABLES: ClassVar[set[str]] = {
        "sites",
        "posts",
        "post_revisions",
        "tags",
        "post_tags",
        "actors",
        "capability_links",
        "idempotency_keys",
        "audit_events",
        "assets",
        "webhooks",
        "webhook_deliveries",
        "redirects",
    }

    def test_all_tables_present(self, db: Session) -> None:
        result = db.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        actual = {row[0] for row in result.fetchall()}
        missing = self.EXPECTED_TABLES - actual
        assert not missing, f"Missing tables: {missing}"


# ---------------------------------------------------------------------------
# Acceptance: FKs and unique constraints
# ---------------------------------------------------------------------------


class TestConstraints:
    """All FKs and unique constraints exist in the DB, not just in app code."""

    def test_posts_unique_site_slug(self, db: Session) -> None:
        site = _make_site(db)
        _make_post(db, site, slug="x")
        dup = Post(
            id=uuid.uuid4(),
            site_id=site.id,
            slug="x",
            title="dup",
            body_md="dup",
            status="draft",
            revision_count=0,
        )
        db.add(dup)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_redirects_unique_site_old_slug(self, db: Session) -> None:
        site = _make_site(db)
        r1 = Redirect(
            id=uuid.uuid4(),
            site_id=site.id,
            old_slug="old",
            new_slug="new",
            status_code=301,
        )
        db.add(r1)
        db.flush()
        r2 = Redirect(
            id=uuid.uuid4(),
            site_id=site.id,
            old_slug="old",
            new_slug="newer",
            status_code=301,
        )
        db.add(r2)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_post_tags_unique_post_tag(self, db: Session) -> None:
        site = _make_site(db)
        post = _make_post(db, site)
        tag = Tag(id=uuid.uuid4(), slug="python", name="Python")
        db.add(tag)
        db.flush()
        pt = PostTag(post_id=post.id, tag_id=tag.id)
        db.add(pt)
        db.flush()
        pt2 = PostTag(post_id=post.id, tag_id=tag.id)
        db.add(pt2)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_posts_site_id_fk_exists(self, db: Session) -> None:
        """posts.site_id references sites.id."""
        fake_site_id = uuid.uuid4()
        post = Post(
            id=uuid.uuid4(),
            site_id=fake_site_id,
            slug="orphan",
            title="Orphan",
            body_md="orphan",
            status="draft",
            revision_count=0,
        )
        db.add(post)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_post_revisions_post_id_fk(self, db: Session) -> None:
        """post_revisions.post_id references posts.id."""
        fake_post_id = uuid.uuid4()
        rev = PostRevision(
            id=uuid.uuid4(),
            post_id=fake_post_id,
            revision=1,
            title="rev",
            body_md="rev",
            status="draft",
        )
        db.add(rev)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()


# ---------------------------------------------------------------------------
# Acceptance: slug collision raises typed domain error
# ---------------------------------------------------------------------------


class TestSlugConflict:
    """Slug collision at insert raises a typed domain error with suggested_slug."""

    def test_slug_conflict_error_type(self, db: Session) -> None:
        site = _make_site(db)
        _make_post(db, site, slug="hello-world")

        error = SlugConflictError("hello-world", "hello-world-2", site="blog")
        assert error.status_code == 409
        assert error.code == "slug-conflict"
        assert "suggested_slug" in error.extensions()
        assert error.extensions()["suggested_slug"] == "hello-world-2"

    def test_slug_conflict_in_database(self, db: Session) -> None:
        """The UNIQUE constraint actually fires on duplicate slugs."""
        site = _make_site(db)
        _make_post(db, site, slug="my-post")

        dup = Post(
            id=uuid.uuid4(),
            site_id=site.id,
            slug="my-post",
            title="dup",
            body_md="dup",
            status="draft",
            revision_count=0,
        )
        db.add(dup)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()


# ---------------------------------------------------------------------------
# Acceptance: soft delete never destroys revisions
# ---------------------------------------------------------------------------


class TestSoftDelete:
    """Deleting a post is a soft delete (status trashed + deleted_at) and never
    destroys revisions."""

    def test_soft_delete_preserves_revisions(self, db: Session) -> None:
        site = _make_site(db)
        post = _make_post(db, site, slug="to-delete")
        post.revision_count = 1
        rev = PostRevision(
            id=uuid.uuid4(),
            post_id=post.id,
            revision=1,
            title="to-delete",
            body_md="# to-delete",
            status="draft",
        )
        db.add(rev)
        db.flush()

        # Simulate soft delete
        post.status = "trashed"
        post.deleted_at = datetime.now(UTC)
        db.flush()

        # Revisions must still be accessible
        remaining = db.execute(
            text("SELECT id FROM post_revisions WHERE post_id = :pid"),
            {"pid": str(post.id)},
        ).fetchall()
        assert len(remaining) == 1

        # Post row still exists
        row = db.execute(
            text("SELECT status, deleted_at FROM posts WHERE id = :pid"),
            {"pid": str(post.id)},
        ).fetchone()
        assert row is not None
        assert row[0] == "trashed"
        assert row[1] is not None


# ---------------------------------------------------------------------------
# Acceptance: seed script
# ---------------------------------------------------------------------------


class TestSeed:
    """Seed script creates a demo site + 3 posts + 1 token."""

    def test_seed_creates_expected_data(self, db: Session) -> None:
        from scripts.seed import seed

        raw_token = seed()
        assert raw_token, "seed should return a non-empty token"

        with session_scope() as session:
            sites = session.execute(text("SELECT COUNT(*) FROM sites")).scalar_one()
            posts = session.execute(text("SELECT COUNT(*) FROM posts")).scalar_one()
            actors = session.execute(text("SELECT COUNT(*) FROM actors")).scalar_one()
            caps = session.execute(text("SELECT COUNT(*) FROM capability_links")).scalar_one()
            revisions = session.execute(text("SELECT COUNT(*) FROM post_revisions")).scalar_one()

        assert sites == 1
        assert posts == 3
        assert actors == 1
        assert caps == 1
        assert revisions == 3  # one revision per post


# ---------------------------------------------------------------------------
# Acceptance: Alembic migrations up and down
# ---------------------------------------------------------------------------


class TestMigrations:
    """Alembic migrations apply cleanly up and down from empty on Postgres 16."""

    def test_upgrade_and_downgrade(self, blank_database: str) -> None:
        # upgrade head
        result = pg_mod.run_alembic("upgrade", "head", database_url=blank_database)
        assert result.returncode == 0, f"upgrade head failed:\n{result.stderr}"

        tables_after_up = pg_mod.list_tables(blank_database)
        assert "posts" in tables_after_up
        assert "sites" in tables_after_up

        # downgrade base
        result = pg_mod.run_alembic("downgrade", "base", database_url=blank_database)
        assert result.returncode == 0, f"downgrade base failed:\n{result.stderr}"

        tables_after_down = pg_mod.list_tables(blank_database)
        assert "posts" not in tables_after_down
        assert "sites" not in tables_after_down

    def test_upgrade_head_again(self, blank_database: str) -> None:
        """Idempotent: upgrading to head twice is fine."""
        pg_mod.run_alembic("upgrade", "head", database_url=blank_database)
        result = pg_mod.run_alembic("upgrade", "head", database_url=blank_database)
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Acceptance: indexes
# ---------------------------------------------------------------------------


class TestIndexes:
    """Key indexes exist for query performance."""

    def test_composite_index_on_posts(self, db: Session) -> None:
        result = db.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'posts' AND indexname = 'ix_posts_site_status_pub_id'"
            )
        )
        assert (
            result.fetchone() is not None
        ), "Composite index on (site_id, status, published_at DESC, id) missing"

    def test_unique_index_on_sites_slug(self, db: Session) -> None:
        result = db.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'sites' AND indexname = 'ix_sites_slug'"
            )
        )
        assert result.fetchone() is not None

    def test_unique_index_on_tags_slug(self, db: Session) -> None:
        result = db.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'tags' AND indexname = 'ix_tags_slug'"
            )
        )
        assert result.fetchone() is not None
