"""Add request_id -> webhook_deliveries (#24).

Carries the originating request id from the event outbox all the way to the
webhook delivery row so the ``request_id`` trace is end-to-end:
request log line -> audit_event -> event_outbox -> webhook_deliveries.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f4e5d6c7b8a9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "webhook_deliveries",
        sa.Column("request_id", sa.String(64), nullable=True),
    )
    op.create_index("ix_webhook_deliveries_request_id", "webhook_deliveries", ["request_id"])


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_request_id", table_name="webhook_deliveries")
    op.drop_column("webhook_deliveries", "request_id")
