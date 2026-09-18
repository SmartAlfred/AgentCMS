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


class PostWriteRequest(BaseModel):
    """Lenient request body for ``?dry_run=true`` and ``/v1/posts/validate``.

    Identical to :class:`PostCreate` except that **every** field is optional,
    so a caller can hand over an incomplete or empty payload and get a
    structured validation report back instead of a framework-level 422.
    Real writes are still checked against the strict ``PostCreate`` contract.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = None
    body_md: str | None = None
    slug: str | None = None
    tags: list[str] | None = None
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
    word_count: int | None = None
    reading_time_minutes: int | None = None
    content_hash: str | None = None
    warnings: list[str] = Field(default_factory=list)


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


# ---------------------------------------------------------------------------
# Revision schemas
# ---------------------------------------------------------------------------


class RevisionMetadata(BaseModel):
    """Lightweight revision info (no body) — cheap for agents to list."""

    model_config = ConfigDict(from_attributes=True)

    revision: int
    title: str
    status: str
    editor_label: str | None = None
    actor_id: str
    source: str
    created_at: datetime
    diff_unified: str | None = None


class RevisionSnapshot(BaseModel):
    """Full revision snapshot with content."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    post_id: str
    revision: int
    title: str
    body_md: str
    frontmatter: dict[str, Any] | None = None
    status: str
    editor_label: str | None = None
    actor_id: str
    request_id: str | None = None
    source: str
    created_at: datetime
    diff_unified: str | None = None


class RevisionListResponse(BaseModel):
    """Cursor-paginated list of revision metadata."""

    model_config = ConfigDict(from_attributes=True)

    items: list[RevisionMetadata]
    next_cursor: int | None = None
    count: int


class RevertRequest(BaseModel):
    """Request body for ``POST /v1/posts/{id}/revert``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    revision: int
    reason: str | None = None


class DiffResponse(BaseModel):
    """Unified diff between two revisions."""

    from_revision: int
    to_revision: int
    diff_unified: str
    identical: bool


# ---------------------------------------------------------------------------
# Validation schemas (#15)
# ---------------------------------------------------------------------------


class ValidationError(BaseModel):
    """A single field-level validation error."""

    field: str
    code: str
    message: str


class WouldCreate(BaseModel):
    """Preview of what would be created on a real write."""

    slug: str
    status: str
    url: str


class ValidationStats(BaseModel):
    """Content statistics computed during validation."""

    word_count: int
    reading_time_minutes: int
    links: int
    images: int


class ValidationNormalised(BaseModel):
    """Preview of the normalised payload that would be stored."""

    body_md: str
    title: str | None = None
    slug: str | None = None
    tags: list[str] = Field(default_factory=list)
    excerpt: str | None = None
    frontmatter: dict[str, Any] = Field(default_factory=dict)


class ValidationResponse(BaseModel):
    """Response from POST /v1/posts/validate and ?dry_run=true.

    ``dry_run`` keeps the pre-#15 ``{"dry_run": true}`` contract that callers
    already rely on, alongside the richer validation report.
    """

    dry_run: bool = True
    valid: bool
    normalised: ValidationNormalised
    would_create: WouldCreate | None = None
    errors: list[ValidationError] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    stats: ValidationStats
