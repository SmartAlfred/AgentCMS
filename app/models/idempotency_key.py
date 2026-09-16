"""Idempotency key model (#3).

Stores the response for a given idempotency key so that retries return the
same result.  24-hour retention is enforced by the application, not a
database-level expiry.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    response_status: Mapped[int | None] = mapped_column(nullable=True)
    response_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
