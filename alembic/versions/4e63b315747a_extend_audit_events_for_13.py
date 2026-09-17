"""extend audit_events for #13

Revision ID: 4e63b315747a
Revises: a1b2c3d4e5f7
Create Date: 2026-09-17 21:51:38.705818+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4e63b315747a"
down_revision: str | None = "a1b2c3d4e5f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # New columns for #13 audit enrichment
    op.add_column(
        "audit_events",
        sa.Column("actor_kind", sa.String(length=20), server_default="human", nullable=True),
    )
    op.add_column("audit_events", sa.Column("revision", sa.Integer(), nullable=True))
    op.add_column(
        "audit_events",
        sa.Column("idempotent_replay", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column("audit_events", sa.Column("prev_hash", sa.String(length=64), nullable=True))
    op.add_column(
        "audit_events",
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=True),
    )
    op.create_index(op.f("ix_audit_events_action"), "audit_events", ["action"], unique=False)

    # Append-only: revoke UPDATE and DELETE on audit_events so no ORM or
    # application code can mutate rows once written.
    op.execute("""
        DO $$
        BEGIN
            REVOKE UPDATE, DELETE ON audit_events FROM PUBLIC;
        EXCEPTION WHEN insufficient_privilege OR undefined_object THEN
            NULL;
        END
        $$;
    """)
    op.execute("""
        DO $$
        BEGIN
            REVOKE UPDATE, DELETE ON audit_events FROM agentcms;
        EXCEPTION WHEN undefined_object THEN
            NULL;
        END
        $$;
    """)
    # Also create a trigger-based guard as a second line of defence
    op.execute("""
        CREATE OR REPLACE FUNCTION deny_audit_mutations() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only: UPDATE and DELETE are not permitted';
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS trg_deny_update ON audit_events;
        CREATE TRIGGER trg_deny_update
            BEFORE UPDATE OR DELETE ON audit_events
            FOR EACH ROW EXECUTE FUNCTION deny_audit_mutations();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_deny_update ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS deny_audit_mutations()")
    op.execute("""
        DO $$
        BEGIN
            GRANT UPDATE, DELETE ON audit_events TO PUBLIC;
        EXCEPTION WHEN undefined_object THEN
            NULL;
        END
        $$;
    """)
    op.execute("""
        DO $$
        BEGIN
            GRANT UPDATE, DELETE ON audit_events TO agentcms;
        EXCEPTION WHEN undefined_object THEN
            NULL;
        END
        $$;
    """)
    op.drop_index(op.f("ix_audit_events_action"), table_name="audit_events")
    op.drop_column("audit_events", "metadata")
    op.drop_column("audit_events", "prev_hash")
    op.drop_column("audit_events", "idempotent_replay")
    op.drop_column("audit_events", "revision")
    op.drop_column("audit_events", "actor_kind")
