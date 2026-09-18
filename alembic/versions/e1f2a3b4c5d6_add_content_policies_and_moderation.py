"""add content policies and moderation decisions (#17)

Revision ID: e1f2a3b4c5d6
Revises: a1b2c3d4e5f6, f7a8b9c0d1e2
Create Date: 2026-09-18 12:00:00.000000+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = ("b2c3d4e5f6a7", "f7a8b9c0d1e2")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "content_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "site_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sites.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False, server_default="flag"),
        sa.Column("config", postgresql.JSONB, nullable=True, server_default="{}"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_content_policies_site_id", "content_policies", ["site_id"])

    op.create_table(
        "moderation_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "post_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("posts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "policy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("content_policies.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("rule_name", sa.String(256), nullable=False),
        sa.Column("rule_kind", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("evidence", sa.Text, nullable=True),
        sa.Column(
            "matched_post_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("posts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("cleared", sa.Boolean, nullable=False, server_default="false"),
        sa.Column(
            "cleared_by_actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("actors.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("cleared_at", sa.DateTime, nullable=True),
        sa.Column("blocked", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_moderation_decisions_post_id", "moderation_decisions", ["post_id"])
    op.create_index("ix_moderation_decisions_cleared", "moderation_decisions", ["cleared"])


def downgrade() -> None:
    op.drop_table("moderation_decisions")
    op.drop_table("content_policies")
