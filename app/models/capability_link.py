"""Capability link model (#3).

Scoped, time-limited tokens for agent access.  The token itself is never
stored -- only a SHA-256 hash.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class CapabilityLink(Base):
    __tablename__ = "capability_links"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("actors.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    path_scope: Mapped[str] = mapped_column(String(512), nullable=False, server_default="/")
    verbs: Mapped[list | None] = mapped_column(JSONB, nullable=True, server_default='["GET"]')
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    uses_remaining: Mapped[int | None] = mapped_column(nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    actor = relationship("Actor", back_populates="capability_links", lazy="noload")
