"""EventOutbox model (#21).

Outbox table for webhook event delivery. Events are written in the same
transaction as the content change, ensuring delivery failures never roll
back or delay the publish response.  The dispatcher reads from this table
asynchronously.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime


class EventOutbox(Base):
    __tablename__ = "event_outbox"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    site_slug: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    actor_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dispatched: Mapped[bool] = mapped_column(nullable=False, server_default="false", index=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now(), index=True)
