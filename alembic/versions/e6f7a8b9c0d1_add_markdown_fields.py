"""Add markdown pipeline fields to posts (#7)

Adds ``body_html``, ``word_count`` and ``reading_time_minutes`` columns
for the markdown rendering pipeline.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-09-17 22:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6f7a8b9c0d1"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("posts", sa.Column("body_html", sa.Text(), nullable=True))
    op.add_column("posts", sa.Column("word_count", sa.Integer(), nullable=True))
    op.add_column("posts", sa.Column("reading_time_minutes", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("posts", "reading_time_minutes")
    op.drop_column("posts", "word_count")
    op.drop_column("posts", "body_html")
