"""Site model (#3).

A site is a content container (e.g. ``blog``).  Every post belongs to exactly
one site, and the ``UNIQUE (site_id, slug)`` constraint on posts is scoped
per-site.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import UTCDateTime


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    publish_mode: Mapped[str] = mapped_column(String(20), nullable=False, server_default="auto")
    settings: Mapped[dict | None] = mapped_column(JSONB, nullable=True, server_default="{}")
    trust_mode_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # relationships
    posts = relationship("Post", back_populates="site", lazy="selectin")
    actors = relationship("Actor", back_populates="site", lazy="raise")
    content_policies = relationship("ContentPolicy", back_populates="site", lazy="raise")
