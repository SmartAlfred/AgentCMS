"""Post model (#3).

Deleting a post is a **soft delete**: ``status`` is set to ``trashed`` and
``deleted_at`` is populated.  The row (and all its ``post_revisions``) is
never physically removed.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("site_id", "slug", name="uq_posts_site_id_slug"),
        Index(
            "ix_posts_site_status_pub_id",
            "site_id",
            "status",
            text("published_at DESC"),
            "id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sites.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    slug: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False, server_default="")
    body_md: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="draft", index=True)
    frontmatter: Mapped[dict | None] = mapped_column(JSONB, nullable=True, server_default="{}")
    author_label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_by_actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actors.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    unpublish_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revision_count: Mapped[int] = mapped_column(nullable=False, server_default="0")
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    word_count: Mapped[int | None] = mapped_column(nullable=True)
    reading_time_minutes: Mapped[int | None] = mapped_column(nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # relationships
    site = relationship("Site", back_populates="posts", lazy="noload")
    revisions = relationship(
        "PostRevision", back_populates="post", lazy="selectin", order_by="PostRevision.revision.desc()"
    )
    tag_links = relationship("PostTag", back_populates="post", lazy="noload")
