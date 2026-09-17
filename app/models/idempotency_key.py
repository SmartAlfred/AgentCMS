"""Idempotency key model (#3, extended by #11).

Stores the response for a given idempotency key so that retries return the
same result.  24-hour retention is enforced via ``expires_at``; a pruner job
reclaims expired rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (Index("ix_idempotency_keys_actor_key", "actor_id", "key", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    key: Mapped[str] = mapped_column(String(256), nullable=False)
    request_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    response_status: Mapped[int | None] = mapped_column(nullable=True)
    response_body: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    response_headers: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    in_flight: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    original_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
