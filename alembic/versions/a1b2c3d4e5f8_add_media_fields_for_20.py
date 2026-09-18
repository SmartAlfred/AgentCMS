"""Add media API fields to assets (#20).

Revision ID: a1b2c3d4e5f8
Revises: f7a8b9c0d1e2
Create Date: 2026-09-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f8"
down_revision = "f3a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("kind", sa.String(20), nullable=False, server_default="file"))
    op.add_column("assets", sa.Column("sha256", sa.String(64), nullable=True))
    op.add_column("assets", sa.Column("width", sa.Integer(), nullable=True))
    op.add_column("assets", sa.Column("height", sa.Integer(), nullable=True))
    op.add_column(
        "assets",
        sa.Column("variant_paths", sa.dialects.postgresql.JSONB(), nullable=True, server_default="{}"),
    )
    op.add_column("assets", sa.Column("status", sa.String(20), nullable=False, server_default="pending"))
    op.add_column("assets", sa.Column("magic_content_type", sa.String(128), nullable=True))
    op.add_column("assets", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    op.create_index("ix_assets_sha256", "assets", ["sha256"])


def downgrade() -> None:
    op.drop_index("ix_assets_sha256", table_name="assets")
    op.drop_column("assets", "deleted_at")
    op.drop_column("assets", "magic_content_type")
    op.drop_column("assets", "status")
    op.drop_column("assets", "variant_paths")
    op.drop_column("assets", "height")
    op.drop_column("assets", "width")
    op.drop_column("assets", "sha256")
    op.drop_column("assets", "kind")
