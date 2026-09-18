"""add review workflow for #16

Revision ID: b2c3d4e5f6a7
Revises: 4e63b315747a
Create Date: 2026-09-18 00:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "4e63b315747a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- posts: review-related columns ---
    op.add_column("posts", sa.Column("publish_at", sa.DateTime(), nullable=True))

    # reviews table must exist before adding FK from posts
    op.create_table(
        "reviews",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "post_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("posts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "site_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sites.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "requested_by_actor_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("actors.id", ondelete="SET NULL"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending_review"),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column(
            "reviewed_by_actor_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("actors.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("preview_token", sa.String(length=256), nullable=True),
        sa.Column("snapshot_body_md", sa.Text(), nullable=True),
        sa.Column("snapshot_title", sa.String(length=512), nullable=True),
        sa.Column("snapshot_diff", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_reviews_post_id", "reviews", ["post_id"], unique=False)
    op.create_index("ix_reviews_site_id", "reviews", ["site_id"], unique=False)
    op.create_index("ix_reviews_status", "reviews", ["status"], unique=False)

    # Now add FK columns to posts that reference reviews
    op.add_column(
        "posts",
        sa.Column(
            "review_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reviews.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("posts", sa.Column("review_status", sa.String(length=20), nullable=True))
    op.add_column("posts", sa.Column("review_comment", sa.Text(), nullable=True))
    op.add_column(
        "posts",
        sa.Column(
            "reviewed_by_actor_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("actors.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("posts", sa.Column("reviewed_at", sa.DateTime(), nullable=True))

    # --- sites: trust mode expiry ---
    op.add_column("sites", sa.Column("trust_mode_expires_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("sites", "trust_mode_expires_at")
    op.drop_column("posts", "reviewed_at")
    op.drop_column("posts", "reviewed_by_actor_id")
    op.drop_column("posts", "review_comment")
    op.drop_column("posts", "review_status")
    op.drop_column("posts", "review_id")
    op.drop_column("posts", "publish_at")
    op.drop_index("ix_reviews_status", table_name="reviews")
    op.drop_index("ix_reviews_site_id", table_name="reviews")
    op.drop_index("ix_reviews_post_id", table_name="reviews")
    op.drop_table("reviews")
