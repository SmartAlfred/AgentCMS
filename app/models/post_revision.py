"""Post revision model (#3).

Append-only snapshot: never updated, never deleted except by explicit
retention policy.  Each revision records the full state of the post at
the time of the edit.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class PostRevision(Base):
    __tablename__ = "post_revisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    post_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False, server_default="")
    body_md: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    frontmatter: Mapped[dict | None] = mapped_column(JSONB, nullable=True, server_default="{}")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="draft")
    editor_label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.id", ondelete="SET NULL"), nullable=True
    )
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    diff_unified: Mapped[str | None] = mapped_column(Text, nullable=True)

    # relationships
    post = relationship("Post", back_populates="revisions", lazy="noload")
