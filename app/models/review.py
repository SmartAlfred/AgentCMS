"""Review model (#16).

Tracks human review requests for posts in ``require_review`` sites.
Each review row represents one publish request that is awaiting a
human decision.  A review can be approved (which publishes the post)
or rejected (which returns the post to ``draft`` with a reason).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    post_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_by_actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.id", ondelete="SET NULL"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending_review", index=True
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by_actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    preview_token: Mapped[str | None] = mapped_column(String(256), nullable=True)
    snapshot_body_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    snapshot_diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # relationships
    post = relationship(
        "Post",
        back_populates="review",
        lazy="raise",
        foreign_keys="[Review.post_id]",
        primaryjoin="Review.post_id == Post.id",
    )
    site = relationship("Site", lazy="raise")
    requested_by = relationship("Actor", foreign_keys=[requested_by_actor_id], lazy="raise")
    reviewed_by = relationship("Actor", foreign_keys=[reviewed_by_actor_id], lazy="raise")
