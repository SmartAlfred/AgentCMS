"""add fulltext search and trigram for #19

Revision ID: f3a1b2c3d4e5
Revises: e1f2a3b4c5d6
Create Date: 2026-09-18 01:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a1b2c3d4e5"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Enable extensions
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Add search_vector column (tsvector) to posts
    op.add_column(
        "posts",
        sa.Column("search_vector", sa.dialects.postgresql.TSVECTOR(), nullable=True),
    )

    # Populate search_vector with weighted content using a PL/pgSQL function
    op.execute(
        """
        CREATE OR REPLACE FUNCTION _build_post_search_vector(
            p_title text, p_body text, p_excerpt text, p_post_id uuid
        ) RETURNS tsvector AS $$
        DECLARE
            tag_text text := '';
            t record;
        BEGIN
            SELECT string_agg(t2.slug, ' ') INTO tag_text
            FROM post_tags pt2
            JOIN tags t2 ON t2.id = pt2.tag_id
            WHERE pt2.post_id = p_post_id;

            RETURN
                setweight(to_tsvector('english', coalesce(unaccent(p_title), '')), 'A') ||
                setweight(to_tsvector('english', coalesce(unaccent(coalesce(tag_text, '')), '')), 'B') ||
                setweight(to_tsvector('english', coalesce(unaccent(coalesce(p_excerpt, '')), '')), 'C') ||
                setweight(to_tsvector('english', coalesce(unaccent(p_body), '')), 'D');
        END;
        $$ LANGUAGE plpgsql
        """
    )

    op.execute(
        """
        UPDATE posts SET search_vector = _build_post_search_vector(
            title, body_md, excerpt, posts.id
        )
        """
    )

    op.execute("DROP FUNCTION IF EXISTS _build_post_search_vector(text, text, text, uuid)")

    # GIN index on search_vector
    op.execute("CREATE INDEX ix_posts_search_vector ON posts USING GIN (search_vector)")

    # Trigger to keep search_vector in sync on INSERT/UPDATE of content columns
    op.execute(
        """
        CREATE OR REPLACE FUNCTION posts_search_vector_update() RETURNS trigger AS $$
        DECLARE
            tag_text text := '';
        BEGIN
            SELECT string_agg(t.slug, ' ') INTO tag_text
            FROM post_tags pt
            JOIN tags t ON t.id = pt.tag_id
            WHERE pt.post_id = NEW.id;

            NEW.search_vector :=
                setweight(to_tsvector('english', coalesce(unaccent(NEW.title), '')), 'A') ||
                setweight(to_tsvector('english', coalesce(unaccent(coalesce(tag_text, '')), '')), 'B') ||
                setweight(to_tsvector('english', coalesce(unaccent(coalesce(NEW.excerpt, '')), '')), 'C') ||
                setweight(to_tsvector('english', coalesce(unaccent(NEW.body_md), '')), 'D');
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql
        """
    )

    op.execute(
        """
        CREATE TRIGGER trg_posts_search_vector
        BEFORE INSERT OR UPDATE OF title, body_md, excerpt ON posts
        FOR EACH ROW
        EXECUTE FUNCTION posts_search_vector_update()
        """
    )

    # Also update trigger when post_tags change
    op.execute(
        """
        CREATE OR REPLACE FUNCTION posts_search_vector_tag_update() RETURNS trigger AS $$
        DECLARE
            target_id uuid;
            tag_text text := '';
        BEGIN
            target_id := COALESCE(NEW.post_id, OLD.post_id);

            SELECT string_agg(t.slug, ' ') INTO tag_text
            FROM post_tags pt
            JOIN tags t ON t.id = pt.tag_id
            WHERE pt.post_id = target_id;

            UPDATE posts SET search_vector =
                setweight(to_tsvector('english', coalesce(unaccent(title), '')), 'A') ||
                setweight(to_tsvector('english', coalesce(unaccent(coalesce(tag_text, '')), '')), 'B') ||
                setweight(to_tsvector('english', coalesce(unaccent(coalesce(excerpt, '')), '')), 'C') ||
                setweight(to_tsvector('english', coalesce(unaccent(body_md), '')), 'D')
            WHERE id = target_id;
            RETURN COALESCE(NEW, OLD);
        END
        $$ LANGUAGE plpgsql
        """
    )

    op.execute(
        """
        CREATE TRIGGER trg_posts_search_vector_on_tags
        AFTER INSERT OR DELETE OR UPDATE OF tag_id ON post_tags
        FOR EACH ROW
        EXECUTE FUNCTION posts_search_vector_tag_update()
        """
    )

    # Trigram index on title for fuzzy matching
    op.execute("CREATE INDEX ix_posts_title_trgm ON posts USING GIN (title gin_trgm_ops)")

    # Index on slug for fast slug lookups
    op.execute("CREATE INDEX ix_posts_slug ON posts (slug)")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_posts_search_vector ON posts")
    op.execute("DROP TRIGGER IF EXISTS trg_posts_search_vector_on_tags ON post_tags")
    op.execute("DROP FUNCTION IF EXISTS posts_search_vector_update()")
    op.execute("DROP FUNCTION IF EXISTS posts_search_vector_tag_update()")
    op.drop_index("ix_posts_slug", table_name="posts")
    op.drop_index("ix_posts_title_trgm", table_name="posts")
    op.drop_index("ix_posts_search_vector", table_name="posts")
    op.drop_column("posts", "search_vector")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("DROP EXTENSION IF EXISTS unaccent")
