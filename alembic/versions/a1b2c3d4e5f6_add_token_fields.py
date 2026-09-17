"""add_token_fields

Revision ID: a1b2c3d4e5f6
Revises: 76e194a9b7ba
Create Date: 2026-09-17 00:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "76e194a9b7ba"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "capability_links", sa.Column("label", sa.String(length=256), server_default="", nullable=False)
    )
    op.add_column("capability_links", sa.Column("ip_allowlist", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("capability_links", "ip_allowlist")
    op.drop_column("capability_links", "label")
