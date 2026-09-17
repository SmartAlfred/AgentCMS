"""add capability link fields for #6

Revision ID: c4d5e6f7a8b9
Revises: a1b2c3d4e5f6
Create Date: 2026-09-17 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c4d5e6f7a8b9"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("capability_links", sa.Column("site_slug", sa.String(128), nullable=True))
    op.create_index("ix_capability_links_site_slug", "capability_links", ["site_slug"])
    op.add_column("capability_links", sa.Column("last_used_at", sa.DateTime(), nullable=True))
    op.add_column(
        "capability_links",
        sa.Column("uses_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("capability_links", "uses_count")
    op.drop_column("capability_links", "last_used_at")
    op.drop_index("ix_capability_links_site_slug", table_name="capability_links")
    op.drop_column("capability_links", "site_slug")
