"""Extend idempotency_keys for ticket #11

Adds columns for response headers, in-flight tracking, expiry, and
a composite unique index on (actor_id, key).  The old single-column
PK ``key`` is replaced by a UUID PK.

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-09-18 10:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7a8b9c0d1e2"
down_revision: str | None = "e6f7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop old PK on key (constraint named pk_idempotency_keys from initial migration)
    op.execute("ALTER TABLE idempotency_keys DROP CONSTRAINT IF EXISTS pk_idempotency_keys")

    # Add new UUID column
    op.add_column(
        "idempotency_keys",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    # Backfill existing rows with generated UUIDs
    op.execute("UPDATE idempotency_keys SET id = gen_random_uuid() WHERE id IS NULL")
    # Set NOT NULL
    op.alter_column("idempotency_keys", "id", nullable=False)
    # Add as primary key
    op.create_primary_key("pk_idempotency_keys_v2", "idempotency_keys", ["id"])

    # Add new columns
    op.add_column(
        "idempotency_keys",
        sa.Column("response_headers", sa.dialects.postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "idempotency_keys",
        sa.Column("in_flight", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("idempotency_keys", sa.Column("original_request_id", sa.String(64), nullable=True))
    op.add_column(
        "idempotency_keys",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Add composite unique index on (actor_id, key)
    op.create_index(
        "ix_idempotency_keys_actor_key",
        "idempotency_keys",
        ["actor_id", "key"],
        unique=True,
    )
    # Plain index on actor_id (model declares actor_id with index=True)
    op.create_index("ix_idempotency_keys_actor_id", "idempotency_keys", ["actor_id"])


def downgrade() -> None:
    op.drop_index("ix_idempotency_keys_actor_id", table_name="idempotency_keys")
    op.drop_index("ix_idempotency_keys_actor_key", table_name="idempotency_keys")
    op.drop_column("idempotency_keys", "expires_at")
    op.drop_column("idempotency_keys", "original_request_id")
    op.drop_column("idempotency_keys", "in_flight")
    op.drop_column("idempotency_keys", "response_headers")

    # Drop PK on id, then drop the column
    op.execute("ALTER TABLE idempotency_keys DROP CONSTRAINT IF EXISTS pk_idempotency_keys_v2")
    op.drop_column("idempotency_keys", "id")
    # Restore PK on key
    op.execute("ALTER TABLE idempotency_keys ADD CONSTRAINT pk_idempotency_keys PRIMARY KEY (key)")
