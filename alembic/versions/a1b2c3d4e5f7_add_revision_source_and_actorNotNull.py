"""Add source column to post_revisions, make actor_id NOT NULL

Revision ID: a1b2c3d4e5f7
Revises: f7a8b9c0d1e2
Create Date: 2026-09-19 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f7"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add source column with a server default for existing rows
    op.add_column(
        "post_revisions",
        sa.Column("source", sa.String(20), server_default="api", nullable=False),
    )
    # Make actor_id NOT NULL (backfill any NULLs first — none expected in practice)
    op.execute("UPDATE post_revisions SET actor_id = (SELECT id FROM actors LIMIT 1) WHERE actor_id IS NULL")
    op.alter_column(
        "post_revisions",
        "actor_id",
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "post_revisions",
        "actor_id",
        nullable=True,
    )
    op.drop_column("post_revisions", "source")
