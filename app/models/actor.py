"""Actor model (#3).

The single attribution table for anything that writes: humans (dashboard
users) and machines (tokens, capability links).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Actor(Base):
    __tablename__ = "actors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default="human")
    label: Mapped[str] = mapped_column(String(256), nullable=False)
    site_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sites.id", ondelete="SET NULL"), nullable=True
    )
    scopes: Mapped[list | None] = mapped_column(JSONB, nullable=True, server_default="[]")
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_used_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    uses_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    # relationships
    site = relationship("Site", back_populates="actors", lazy="raise")
    capability_links = relationship("CapabilityLink", back_populates="actor", lazy="raise")
