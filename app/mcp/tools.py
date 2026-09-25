"""MCP tools for AgentCMS - generated from OpenAPI spec.

Tools:
- create_post(site, title?, body_md, tags?, slug?, dry_run?)
- update_post(id_or_slug, title?, body_md?, tags?, if_match?)
- get_post(id_or_slug, format: json|markdown)
- list_posts(site, status?, tag?, limit?, cursor?)
- search_posts(site, q)
- publish_post(id_or_slug) / unpublish_post(id_or_slug)
- validate_post(...) (self-correction loop)
- list_revisions(id) / revert_post(id, revision)
- upload_asset(...), list_assets(...)
- check_status() -> site, token scopes, remaining quota
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from mcp.types import CallToolResult, TextContent, Tool

from app.auth import AuthContext
from app.db.session import session_scope
from app.domain.errors import SiteNotFoundError
from app.models.actor import Actor
from app.models.site import Site
from app.services.capability_tokens import verify_capability_token
from app.services.concurrency import check_if_match
from app.services.post import (
    _post_to_dict,
)
from app.services.post import (
    create_post as svc_create_post,
)
from app.services.post import (
    get_post as svc_get_post,
)
from app.services.post import (
    list_posts as svc_list_posts,
)
from app.services.post import (
    list_revisions as svc_list_revisions,
)
from app.services.post import (
    publish_post as svc_publish_post,
)
from app.services.post import (
    revert_post as svc_revert_post,
)
from app.services.post import (
    unpublish_post as svc_unpublish_post,
)
from app.services.post import (
    update_post as svc_update_post,
)
from app.services.search import search_posts as svc_search_posts
from app.services.tokens import verify_token
from app.services.validation import validate_post as svc_validate_post

logger = logging.getLogger("app.mcp.tools")


def _extract_auth_context(token: str) -> AuthContext | None:
    """Extract auth context from a bearer token (acms_ or cap_)."""

    with session_scope() as db:
        # Try standard token first
        if token.startswith("acms_"):
            try:
                actor, link = verify_token(
                    db,
                    token,
                    required_scope=None,
                    required_site_id=None,
                    check_revoked=True,
                )
                return AuthContext(
                    actor=actor,
                    link=link,
                    actor_id=actor.id,
                    label=actor.label,
                    scopes=list(actor.scopes or []),
                    site_id=actor.site_id,
                )
            except Exception:
                pass

        # Try capability token
        if token.startswith("cap_"):
            try:
                # Parse site slug from token
                from app.services.capability_tokens import _parse_token

                parsed = _parse_token(token)
                if parsed:
                    site_slug, _ = parsed
                    actor, link = verify_capability_token(
                        db,
                        token,
                        required_verb=None,
                        required_site_slug=site_slug,
                    )
                    return AuthContext(
                        actor=actor,
                        link=link,
                        actor_id=actor.id,
                        label=link.label or actor.label,
                        scopes=list(actor.scopes or []),
                        site_id=actor.site_id,
                    )
            except Exception:
                pass

    return None


MCP_AGENT_LABEL = "mcp-agent"


def _mcp_agent_actor_id(db: Any) -> uuid.UUID:
    """The actor MCP-originated writes are attributed to (get, create if needed).

    Tool calls arrive over the authenticated ``/mcp`` endpoint, but the SDK's
    request handlers do not see the caller's request, so a write cannot yet be
    attributed to the token's own actor. It therefore lands on a single machine
    actor — unlike the first draft, which passed the *site's* id as ``actor_id``:
    a site is not an actor, so every create/update/publish/revert died on the
    ``post_revisions.actor_id -> actors.id`` foreign key as soon as a real site
    existed in the database.
    """
    actor = db.query(Actor).filter(Actor.kind == "machine", Actor.label == MCP_AGENT_LABEL).first()
    if actor is None:
        actor = Actor(id=uuid.uuid4(), kind="machine", label=MCP_AGENT_LABEL, scopes=[])
        db.add(actor)
        db.flush()
    return actor.id


def _require_site(db: Any, site_slug: str) -> Site:
    """Get site by slug or raise."""
    site = db.query(Site).filter(Site.slug == site_slug).first()
    if site is None:
        raise SiteNotFoundError(site_slug)
    return site


def _require_post(db: Any, identifier: str, site_id: Any = None) -> Any:
    """Get post by ID or slug, optionally scoped to site."""
    from app.services.post import _resolve_post

    return _resolve_post(db, identifier, site_id=site_id)


def _format_post_response(post: Any, site_slug: str, db: Any, format: str = "json") -> dict[str, Any]:
    """Format a post for MCP response."""
    data = _post_to_dict(post, site_slug, session=db)

    if format == "markdown":
        return {
            "type": "text",
            "text": data.get("body_md", ""),
        }

    return {
        "type": "text",
        "text": json.dumps(data, default=str, indent=2),
    }


def _format_error(error: Exception) -> CallToolResult:
    """Format an error as MCP tool result."""
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps({"error": str(error), "type": type(error).__name__}))
        ],
        is_error=True,
    )


# Tool definitions
TOOLS = [
    Tool(
        name="check_status",
        description=(
            "Check the current connection status, token scopes, and remaining quota. "
            "This is the 'what am I allowed to do' tool — CALL THIS FIRST before any other tool "
            "to understand your permissions and limits."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="create_post",
        description=(
            "Create a new post as a draft. Creates a post in draft status — publishing is a separate step. "
            "Title is derived from the first H1 in body_md if omitted. Slug is derived from the "
            "title if omitted. "
            "Use dry_run=true to validate without persisting. "
            "Side effects: Creates a draft post, emits audit event, creates revision 1. "
            "Idempotency: Not idempotent by default; supply Idempotency-Key header for duplicate protection. "
            "Does NOT publish — the post remains in draft until publish_post is called."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "site": {"type": "string", "description": "Site slug (e.g., 'blog')"},
                "body_md": {"type": "string", "description": "Markdown content (required)"},
                "title": {"type": "string", "description": "Optional title (derived from H1 if omitted)"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of tag slugs",
                },
                "slug": {
                    "type": "string",
                    "description": "Optional custom slug (derived from title if omitted)",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, validate only — don't persist (default: false)",
                },
            },
            "required": ["site", "body_md"],
        },
    ),
    Tool(
        name="update_post",
        description=(
            "Update an existing post. Partial update — only supplied fields are changed. "
            "Creates a new revision on any content change. "
            "Use dry_run=true to validate without persisting. "
            "Use if_match with ETag for optimistic concurrency. "
            "Side effects: Creates new revision, emits audit event. "
            "Idempotency: Not idempotent by default; supply Idempotency-Key header. "
            "Does NOT change publish status — use publish_post/unpublish_post."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id_or_slug": {"type": "string", "description": "Post UUID or slug"},
                "title": {"type": "string", "description": "New title"},
                "body_md": {"type": "string", "description": "New markdown content"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "New tag list (replaces existing)",
                },
                "slug": {"type": "string", "description": "New slug (must be unique)"},
                "excerpt": {"type": "string", "description": "New excerpt"},
                "frontmatter": {"type": "object", "description": "New frontmatter dict"},
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, validate only — don't persist (default: false)",
                },
                "if_match": {
                    "type": "string",
                    "description": "ETag for optimistic concurrency (from previous GET)",
                },
            },
            "required": ["id_or_slug"],
        },
    ),
    Tool(
        name="get_post",
        description=(
            "Read a post by ID or slug. "
            "Side effects: None (read-only). "
            "Use format='markdown' to get raw markdown body."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id_or_slug": {"type": "string", "description": "Post UUID or slug"},
                "format": {
                    "type": "string",
                    "enum": ["json", "markdown"],
                    "description": "Response format (default: json)",
                },
            },
            "required": ["id_or_slug"],
        },
    ),
    Tool(
        name="list_posts",
        description=("List posts for a site with cursor pagination. Side effects: None (read-only)."),
        input_schema={
            "type": "object",
            "properties": {
                "site": {"type": "string", "description": "Site slug"},
                "status": {
                    "type": "string",
                    "description": "Filter by status (draft, published, pending_review, trashed)",
                },
                "tag": {"type": "string", "description": "Filter by tag slug"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                    "description": "Number of posts per page",
                },
                "cursor": {"type": "string", "description": "Pagination cursor from previous response"},
            },
            "required": ["site"],
        },
    ),
    Tool(
        name="search_posts",
        description=(
            "Full-text search across posts. Use this FIRST to check for duplicates before writing. "
            "Side effects: None (read-only)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "site": {"type": "string", "description": "Site slug"},
                "q": {"type": "string", "description": "Search query string"},
            },
            "required": ["site", "q"],
        },
    ),
    Tool(
        name="publish_post",
        description=(
            "Publish a draft post. Idempotent — publishing twice returns the same post with warnings[]. "
            "If site requires review, enters pending_review status instead. "
            "Side effects: Changes status to published, sets published_at, creates revision. "
            "Idempotent: Yes — repeat calls are safe."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id_or_slug": {"type": "string", "description": "Post UUID or slug"},
            },
            "required": ["id_or_slug"],
        },
    ),
    Tool(
        name="unpublish_post",
        description=(
            "Unpublish a published post (back to draft). "
            "Side effects: Changes status to draft, creates revision. "
            "Idempotent: No — unpublishing a draft returns an error."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id_or_slug": {"type": "string", "description": "Post UUID or slug"},
            },
            "required": ["id_or_slug"],
        },
    ),
    Tool(
        name="validate_post",
        description=(
            "Validate a post payload without persisting — the self-correction loop for free. "
            "Runs the full validation pipeline: frontmatter schema, field limits, markdown parse, "
            "link checking, image alt checking, slug availability, duplicate detection, publish gates, "
            "stats, and normalisation preview. Side-effect free: no rows, no revisions, no audit writes. "
            "Errors block; warnings inform. Link failures are always warnings."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "site": {"type": "string", "description": "Site slug to validate against"},
                "body_md": {"type": "string", "description": "Markdown content"},
                "title": {"type": "string", "description": "Optional title"},
                "slug": {"type": "string", "description": "Optional slug"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tag list"},
                "excerpt": {"type": "string", "description": "Optional excerpt"},
                "frontmatter": {"type": "object", "description": "Optional frontmatter dict"},
                "existing_post_id": {
                    "type": "string",
                    "description": "If set, treat as update (skip slug check for this post)",
                },
                "check_links": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether to check links (default: true)",
                },
            },
            "required": ["site"],
        },
    ),
    Tool(
        name="list_revisions",
        description=(
            "List revision metadata for a post (no body content, cheap for agents). "
            "Side effects: None (read-only)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id_or_slug": {"type": "string", "description": "Post UUID or slug"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                    "description": "Number of revisions per page",
                },
                "cursor": {"type": "integer", "description": "Revision number to start after"},
            },
            "required": ["id_or_slug"],
        },
    ),
    Tool(
        name="revert_post",
        description=(
            "Revert a post to a previous revision. Creates a NEW revision whose content equals the "
            "target revision. "
            "History is NEVER rewritten. "
            "⚠️ DESTRUCTIVE / IRREVERSIBLE: This changes the current content. "
            "Consider using validate_post with existing_post_id first to preview. "
            "Dry-run is NOT available on this operation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id_or_slug": {"type": "string", "description": "Post UUID or slug"},
                "revision": {"type": "integer", "description": "Target revision number to revert to"},
                "reason": {
                    "type": "string",
                    "description": "Optional reason for the revert (recorded in audit)",
                },
            },
            "required": ["id_or_slug", "revision"],
        },
    ),
    Tool(
        name="upload_asset",
        description=(
            "Create an asset and return a presigned upload URL. "
            "The agent PUTs the file bytes to the returned URL, then calls finalize_asset "
            "(not yet exposed as a tool) to complete the upload. "
            "This is the first step of a 3-step upload: "
            "1. upload_asset → presigned URL "
            "2. PUT bytes to upload_url "
            "3. finalize_asset (POST /v1/assets/{id}/finalize) → validate, variants"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "site": {"type": "string", "description": "Site slug"},
                "filename": {"type": "string", "description": "Original filename"},
                "content_type": {
                    "type": "string",
                    "description": "MIME type (optional, inferred from filename)",
                },
                "bytes": {"type": "integer", "description": "Expected file size in bytes"},
                "alt": {"type": "string", "description": "Alt text for images"},
                "kind": {
                    "type": "string",
                    "enum": ["image", "file"],
                    "default": "image",
                    "description": "Asset kind (default: image)",
                },
            },
            "required": ["site", "filename", "bytes"],
        },
    ),
    Tool(
        name="list_assets",
        description=(
            "List assets for a site. "
            "Side effects: None (read-only). Returns assets with markdown embed codes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "site": {"type": "string", "description": "Site slug"},
                "cursor": {"type": "string", "description": "Pagination cursor (asset ID)"},
                "kind": {
                    "type": "string",
                    "enum": ["image", "file"],
                    "description": "Filter by image or file",
                },
                "q": {"type": "string", "description": "Search query for filename"},
            },
            "required": ["site"],
        },
    ),
]


async def handle_check_status(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle check_status tool call."""
    return {
        "service": "AgentCMS",
        "version": "0.3.1",
        "available_tools": [t.name for t in TOOLS],
        "auth_note": "Provide Authorization header with acms_* or cap_* token",
        "rate_limits": {
            "writes_per_minute": 30,
            "reads_per_minute": 600,
            "daily_writes": 500,
            "daily_publishes": 50,
        },
        "body_limit_kb": 256,
    }


async def handle_create_post(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle create_post tool call."""
    site = arguments["site"]
    body_md = arguments["body_md"]
    title = arguments.get("title")
    tags = arguments.get("tags")
    slug = arguments.get("slug")
    dry_run = arguments.get("dry_run", False)

    with session_scope() as db:
        _require_site(db, site)

        if dry_run:
            result = svc_validate_post(
                db,
                site_slug=site,
                body_md=body_md,
                title=title,
                slug=slug,
                tags=tags,
                check_links=True,
            )
            return result.to_dict()

        post, warnings = svc_create_post(
            db,
            site,
            body_md=body_md,
            title=title,
            slug=slug,
            tags=tags,
            actor_id=_mcp_agent_actor_id(db),
            source="mcp",
            audit_ctx={"actor_label": "mcp-agent", "actor_kind": "machine"},
            is_agent_actor=True,
        )
        data = _post_to_dict(post, site, session=db)
        data["warnings"] = warnings
        return data


async def handle_update_post(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle update_post tool call."""
    id_or_slug = arguments["id_or_slug"]
    title = arguments.get("title")
    body_md = arguments.get("body_md")
    tags = arguments.get("tags")
    slug = arguments.get("slug")
    excerpt = arguments.get("excerpt")
    frontmatter = arguments.get("frontmatter")
    dry_run = arguments.get("dry_run", False)
    if_match = arguments.get("if_match")

    with session_scope() as db:
        post = _require_post(db, id_or_slug)
        # Honour the declared optimistic-concurrency parameter exactly like the
        # HTTP API's If-Match: reject a stale write instead of clobbering it.
        if if_match is not None:
            check_if_match(if_match, post.content_hash, post.revision_count)
        site_slug = post.site.slug if post.site else "blog"

        if dry_run:
            merged_body = body_md if body_md is not None else post.body_md
            merged_title = title if title is not None else post.title
            merged_slug = slug if slug is not None else post.slug
            merged_tags = tags if tags is not None else None
            merged_excerpt = excerpt if excerpt is not None else post.excerpt
            merged_frontmatter = frontmatter if frontmatter is not None else post.frontmatter

            result = svc_validate_post(
                db,
                site_slug=site_slug,
                body_md=merged_body,
                title=merged_title,
                slug=merged_slug,
                tags=merged_tags,
                excerpt=merged_excerpt,
                frontmatter=merged_frontmatter,
                existing_post_id=id_or_slug,
                check_links=True,
            )
            return result.to_dict()

        post, warnings = svc_update_post(
            db,
            id_or_slug,
            title=title,
            body_md=body_md,
            slug=slug,
            tags=tags,
            excerpt=excerpt,
            frontmatter=frontmatter,
            actor_id=_mcp_agent_actor_id(db),
            source="mcp",
            audit_ctx={"actor_label": "mcp-agent", "actor_kind": "machine"},
            is_agent_actor=True,
        )
        data = _post_to_dict(post, site_slug, session=db)
        data["warnings"] = warnings
        return data


async def handle_get_post(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle get_post tool call."""
    id_or_slug = arguments["id_or_slug"]
    format = arguments.get("format", "json")

    with session_scope() as db:
        post = svc_get_post(db, id_or_slug)
        site_slug = post.site.slug if post.site else "blog"
        return _format_post_response(post, site_slug, db, format)


async def handle_list_posts(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle list_posts tool call."""
    site = arguments["site"]
    status = arguments.get("status")
    tag = arguments.get("tag")
    limit = arguments.get("limit", 20)
    cursor = arguments.get("cursor")

    with session_scope() as db:
        items, next_cursor = svc_list_posts(
            db,
            site,
            status=status,
            tag=tag,
            limit=limit,
            cursor=cursor,
        )
        post_items = [_post_to_dict(p, site, session=db) for p in items]
        return {"items": post_items, "next_cursor": next_cursor, "count": len(post_items)}


async def handle_search_posts(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle search_posts tool call."""
    site = arguments["site"]
    q = arguments["q"]

    with session_scope() as db:
        result = svc_search_posts(
            db,
            q=q,
            site_slug=site,
            limit=20,
        )
        return result


async def handle_publish_post(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle publish_post tool call."""
    id_or_slug = arguments["id_or_slug"]

    with session_scope() as db:
        post = _require_post(db, id_or_slug)
        site_slug = post.site.slug if post.site else "blog"

        post, warnings = svc_publish_post(
            db,
            id_or_slug,
            actor_id=_mcp_agent_actor_id(db),
            source="mcp",
            audit_ctx={"actor_label": "mcp-agent", "actor_kind": "machine"},
            is_agent_actor=True,
        )
        data = _post_to_dict(post, site_slug, session=db)
        data["warnings"] = warnings
        return data


async def handle_unpublish_post(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle unpublish_post tool call."""
    id_or_slug = arguments["id_or_slug"]

    with session_scope() as db:
        post = _require_post(db, id_or_slug)
        site_slug = post.site.slug if post.site else "blog"

        post, warnings = svc_unpublish_post(
            db,
            id_or_slug,
            actor_id=_mcp_agent_actor_id(db),
            source="mcp",
            audit_ctx={"actor_label": "mcp-agent", "actor_kind": "machine"},
        )
        data = _post_to_dict(post, site_slug, session=db)
        data["warnings"] = warnings
        return data


async def handle_validate_post(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle validate_post tool call."""
    site = arguments["site"]
    body_md = arguments.get("body_md")
    title = arguments.get("title")
    slug = arguments.get("slug")
    tags = arguments.get("tags")
    excerpt = arguments.get("excerpt")
    frontmatter = arguments.get("frontmatter")
    existing_post_id = arguments.get("existing_post_id")
    check_links = arguments.get("check_links", True)

    with session_scope() as db:
        result = svc_validate_post(
            db,
            site_slug=site,
            body_md=body_md,
            title=title,
            slug=slug,
            tags=tags,
            excerpt=excerpt,
            frontmatter=frontmatter,
            existing_post_id=existing_post_id,
            check_links=check_links,
        )
        return result.to_dict()


async def handle_list_revisions(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle list_revisions tool call."""
    id_or_slug = arguments["id_or_slug"]
    limit = arguments.get("limit", 20)
    cursor = arguments.get("cursor")

    with session_scope() as db:
        items, next_cursor = svc_list_revisions(db, id_or_slug, limit=limit, cursor=cursor)
        metadata = [
            {
                "revision": r.revision,
                "title": r.title,
                "status": r.status,
                "editor_label": r.editor_label,
                "actor_id": str(r.actor_id),
                "source": r.source,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "diff_unified": r.diff_unified,
            }
            for r in items
        ]
        return {"items": metadata, "next_cursor": next_cursor, "count": len(metadata)}


async def handle_revert_post(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle revert_post tool call."""
    id_or_slug = arguments["id_or_slug"]
    revision = arguments["revision"]
    reason = arguments.get("reason")

    with session_scope() as db:
        post = _require_post(db, id_or_slug)
        site_slug = post.site.slug if post.site else "blog"

        post = svc_revert_post(
            db,
            id_or_slug,
            target_revision=revision,
            actor_id=_mcp_agent_actor_id(db),
            reason=reason,
            source="mcp",
            audit_ctx={"actor_label": "mcp-agent", "actor_kind": "machine"},
        )
        data = _post_to_dict(post, site_slug, session=db)
        data["warnings"] = [f"Reverted to revision {revision}" + (f": {reason}" if reason else "")]
        return data


async def handle_upload_asset(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle upload_asset tool call."""
    site = arguments["site"]
    filename = arguments["filename"]
    content_type = arguments.get("content_type")
    bytes = arguments["bytes"]
    alt = arguments.get("alt")
    kind = arguments.get("kind", "image")

    with session_scope() as db:
        site_obj = _require_site(db, site)
        import uuid

        from app.models.asset import Asset
        from app.services import media

        validated_ct = media.validate_asset_type(filename, content_type)

        if bytes > media.MAX_ASSET_BYTES:
            raise ValueError(f"File too large: {bytes} bytes, max {media.MAX_ASSET_BYTES}")

        temp_id = uuid.uuid4()
        storage_key = f"temp/{site}/{temp_id}/{filename}"

        upload_url, upload_headers, expires_in = media.generate_presigned_put_url(storage_key, validated_ct)

        asset = Asset(
            id=temp_id,
            site_id=site_obj.id,
            filename=filename,
            content_type=validated_ct,
            byte_size=bytes,
            storage_key=storage_key,
            alt_text=alt,
            kind=kind,
            status="pending",
        )
        db.add(asset)
        db.commit()
        db.refresh(asset)

        asset_url = media.make_asset_url(str(temp_id), filename)
        alt_text = alt or filename
        markdown = f"![{alt_text}]({asset_url})" if kind == "image" else f"[{filename}]({asset_url})"

        return {
            "id": str(asset.id),
            "upload_url": upload_url,
            "upload_method": "PUT",
            "upload_headers": upload_headers,
            "expires_in": expires_in,
            "asset_url": asset_url,
            "markdown": markdown,
            "variants": {},
            "max_bytes": media.MAX_ASSET_BYTES,
        }


async def handle_list_assets(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle list_assets tool call."""
    site = arguments["site"]
    cursor = arguments.get("cursor")
    kind = arguments.get("kind")
    q = arguments.get("q")

    with session_scope() as db:
        site_obj = _require_site(db, site)
        import uuid

        from app.models.asset import Asset

        query = db.query(Asset).filter(Asset.site_id == site_obj.id, Asset.deleted_at.is_(None))

        if kind:
            query = query.filter(Asset.kind == kind)
        if q:
            query = query.filter(Asset.filename.ilike(f"%{q}%"))

        if cursor:
            try:
                cursor_id = uuid.UUID(cursor)
                query = query.filter(Asset.id < cursor_id)
            except ValueError:
                pass

        items = query.order_by(Asset.created_at.desc(), Asset.id.desc()).limit(51).all()

        next_cursor = None
        if len(items) > 50:
            next_cursor = str(items[-2].id)
            items = items[:50]

        from app.api.v1.assets import _asset_to_read

        asset_list = [_asset_to_read(a) for a in items]
        return {
            "items": [a.model_dump() for a in asset_list],
            "next_cursor": next_cursor,
            "count": len(asset_list),
        }


# Tool handler mapping
TOOL_HANDLERS = {
    "check_status": handle_check_status,
    "create_post": handle_create_post,
    "update_post": handle_update_post,
    "get_post": handle_get_post,
    "list_posts": handle_list_posts,
    "search_posts": handle_search_posts,
    "publish_post": handle_publish_post,
    "unpublish_post": handle_unpublish_post,
    "validate_post": handle_validate_post,
    "list_revisions": handle_list_revisions,
    "revert_post": handle_revert_post,
    "upload_asset": handle_upload_asset,
    "list_assets": handle_list_assets,
}


def get_all_tools() -> list[Tool]:
    """Return all registered tools."""
    return TOOLS


async def handle_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a tool by name with arguments."""
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        raise ValueError(f"Unknown tool: {name}")
    return await handler(arguments)
