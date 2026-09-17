"""Redirect model (#3).

Maps old slugs to new slugs for 301 redirects.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Redirect(Base):
    __tablename__ = "redirects"
    __table_args__ = (UniqueConstraint("site_id", "old_slug", name="uq_redirects_site_id_old_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sites.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    old_slug: Mapped[str] = mapped_column(String(256), nullable=False)
    new_slug: Mapped[str] = mapped_column(String(256), nullable=False)
    status_code: Mapped[int] = mapped_column(nullable=False, server_default="301")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
