"""Pydantic request/response schemas for the v1 Post API (#4)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class PostCreate(BaseModel):
    """Request body for ``POST /v1/sites/{site}/posts``.

    Every field is optional except ``body_md``.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = None
    body_md: str
    slug: str | None = None
    tags: list[str] = Field(default_factory=list)
    excerpt: str | None = None
    frontmatter: dict[str, Any] | None = None


class PostUpdate(BaseModel):
    """Request body for ``PATCH /v1/posts/{id}``.

    All fields are optional; only supplied fields are applied.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = None
    body_md: str | None = None
    slug: str | None = None
    tags: list[str] | None = None
    excerpt: str | None = None
    frontmatter: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class PostRead(BaseModel):
    """Standard JSON representation of a post returned by every endpoint."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    site_id: str
    slug: str
    title: str
    body_md: str
    excerpt: str | None = None
    status: str
    frontmatter: dict[str, Any] | None = None
    tags: list[str] = Field(default_factory=list)
    url: str
    markdown_url: str
    revision: int
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None = None


class PostListResponse(BaseModel):
    """Cursor-paginated list of posts."""

    model_config = ConfigDict(from_attributes=True)

    items: list[PostRead]
    next_cursor: str | None = None
    count: int


class PublishResponse(BaseModel):
    """Response for publish/unpublish actions."""

    id: str
    slug: str
    status: str
    url: str
    published_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list)


class DryRunResponse(BaseModel):
    """Response when ``?dry_run=true`` is supplied."""

    dry_run: bool = True
    would_create: PostRead | None = None
