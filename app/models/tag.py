"""Tag and PostTag models (#3).

Tags are slug-normalised, lowercase.  A post may have at most 20 tags
(enforced in application code, not the database).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    post_links = relationship("PostTag", back_populates="tag", lazy="noload")


class PostTag(Base):
    __tablename__ = "post_tags"
    # NB: no UNIQUE (post_id, tag_id) here.  The composite primary key below
    # already guarantees uniqueness, and PostgreSQL does not create a second
    # constraint on the exact same columns (verified on PG 16), so declaring one
    # only made `alembic check` report drift that could never be applied.

    post_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("posts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    post = relationship("Post", back_populates="tag_links")
    tag = relationship("Tag", back_populates="post_links")
