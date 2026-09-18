"""Add event_outbox for webhook delivery (#21).

Revision ID: c3d4e5f6a7b8
Revises: a1b2c3d4e5f8
Create Date: 2026-09-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "a1b2c3d4e5f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_outbox",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("site_slug", sa.String(128), nullable=True),
        sa.Column("payload", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("actor_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_label", sa.String(256), nullable=True),
        sa.Column("actor_kind", sa.String(20), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("dispatched", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("dispatched_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_event_outbox_event_type", "event_outbox", ["event_type"])
    op.create_index("ix_event_outbox_dispatched", "event_outbox", ["dispatched"])
    op.create_index("ix_event_outbox_created_at", "event_outbox", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_event_outbox_created_at", table_name="event_outbox")
    op.drop_index("ix_event_outbox_dispatched", table_name="event_outbox")
    op.drop_index("ix_event_outbox_event_type", table_name="event_outbox")
    op.drop_table("event_outbox")
