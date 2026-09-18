"""MCP tool schemas (#22).

Tool schemas generated from the OpenAPI document — no hand-written drift.
Each tool has a description written for a model: what it does, side effects,
whether it publishes, idempotency behaviour, what "draft" means, and a
one-line example per tool. Destructive/irreversible operations are annotated;
`dry_run` available on everything destructive.
"""

from typing import Any

# Tool input/output schemas as JSON Schema objects
# These are derived from the OpenAPI specification

CREATE_POST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "create_post",
    "description": (
        "Create a new post in draft status. Never publishes — publishing is a separate call.\n"
        "Returns the created post with its slug, URL, and revision number.\n"
        "Side effect: creates a draft post in the database (revision 1).\n"
        "Idempotency: supply an Idempotency-Key header to make this call idempotent.\n"
        "Example: create_post(site='blog', title='Hello', body_md='# Hello\\n\\nWorld')"
    ),
    "properties": {
        "site": {"type": "string", "description": "Site slug (e.g., 'blog')"},
        "title": {
            "type": "string",
            "description": "Optional title; derived from first H1 in body_md if omitted",
        },
        "body_md": {"type": "string", "description": "Markdown body content (required)"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional list of tag slugs"},
        "slug": {"type": "string", "description": "Optional slug; derived from title if omitted"},
        "dry_run": {
            "type": "boolean",
            "default": False,
            "description": "Validate but don't persist; returns validation report",
        },
    },
    "required": ["site", "body_md"],
    "additionalProperties": False,
}

UPDATE_POST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "update_post",
    "description": (
        "Update an existing post. Creates a new revision snapshot.\n"
        "Only supplied fields are changed; omitted fields keep their current values.\n"
        "Side effect: creates a new revision, updates post content.\n"
        "Idempotency: supply an Idempotency-Key header to make this call idempotent.\n"
        "Optimistic concurrency: provide If-Match header with current ETag.\n"
        "Example: update_post(id_or_slug='my-post', title='New Title')"
    ),
    "properties": {
        "id_or_slug": {"type": "string", "description": "Post UUID or slug"},
        "title": {"type": "string", "description": "New title"},
        "body_md": {"type": "string", "description": "New markdown body"},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "New tag list (replaces existing)",
        },
        "if_match": {"type": "string", "description": "ETag for optimistic concurrency (from GET response)"},
        "dry_run": {
            "type": "boolean",
            "default": False,
            "description": "Validate but don't persist; returns validation report",
        },
    },
    "required": ["id_or_slug"],
    "additionalProperties": False,
}

GET_POST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "get_post",
    "description": (
        "Read a post by UUID or slug. Returns full post object by default.\n"
        "Format option: 'json' (default, full object) or 'markdown' (raw body_md only).\n"
        "Safe: no side effects, read-only.\n"
        "Supports If-None-Match for cheap polling (returns 304 if unchanged).\n"
        "Example: get_post(id_or_slug='my-post', format='json')"
    ),
    "properties": {
        "id_or_slug": {"type": "string", "description": "Post UUID or slug"},
        "format": {
            "type": "string",
            "enum": ["json", "markdown"],
            "default": "json",
            "description": "Response format",
        },
    },
    "required": ["id_or_slug"],
    "additionalProperties": False,
}

LIST_POSTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "list_posts",
    "description": (
        "List posts for a site with cursor pagination.\n"
        "Filters: status (draft/published/pending_review/trashed), tag, author.\n"
        "Safe: no side effects, read-only.\n"
        "Example: list_posts(site='blog', status='draft', limit=10)"
    ),
    "properties": {
        "site": {"type": "string", "description": "Site slug"},
        "status": {
            "type": "string",
            "enum": ["draft", "published", "pending_review", "trashed"],
            "description": "Filter by status",
        },
        "tag": {"type": "string", "description": "Filter by tag slug"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "Page size"},
        "cursor": {"type": "string", "description": "Pagination cursor from previous response"},
    },
    "required": ["site"],
    "additionalProperties": False,
}

SEARCH_POSTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "search_posts",
    "description": (
        "Full-text search across posts with faceted filters.\n"
        "Empty query behaves like list-with-filters (no 400).\n"
        "Safe: no side effects, read-only.\n"
        "Example: search_posts(site='blog', q='machine learning')"
    ),
    "properties": {
        "site": {"type": "string", "description": "Site slug"},
        "q": {"type": "string", "description": "Full-text search query (optional)"},
        "status": {
            "type": "string",
            "enum": ["draft", "published", "pending_review", "trashed"],
            "description": "Filter by status",
        },
        "tag": {"type": "string", "description": "Filter by tag slug"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "Page size"},
        "cursor": {"type": "string", "description": "Pagination cursor"},
        "format": {
            "type": "string",
            "enum": ["json", "ids"],
            "default": "json",
            "description": "'ids' returns lightweight id/slug pairs",
        },
    },
    "required": ["site"],
    "additionalProperties": False,
}

PUBLISH_POST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "publish_post",
    "description": (
        "Publish a draft post. Idempotent — publishing twice returns 200 with warnings.\n"
        "Side effect: changes post status to 'published', sets published_at timestamp.\n"
        "If site requires review, enters 'pending_review' instead.\n"
        "Optimistic concurrency: provide If-Match header.\n"
        "Example: publish_post(id_or_slug='my-post')"
    ),
    "properties": {
        "id_or_slug": {"type": "string", "description": "Post UUID or slug"},
        "if_match": {"type": "string", "description": "ETag for optimistic concurrency"},
    },
    "required": ["id_or_slug"],
    "additionalProperties": False,
}

UNPUBLISH_POST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "unpublish_post",
    "description": (
        "Unpublish a published post (back to draft).\n"
        "Side effect: changes status to 'draft', clears published_at.\n"
        "Destructive: unpublishes content from public view.\n"
        "Optimistic concurrency: provide If-Match header.\n"
        "dry_run available to preview without changing state.\n"
        "Example: unpublish_post(id_or_slug='my-post')"
    ),
    "properties": {
        "id_or_slug": {"type": "string", "description": "Post UUID or slug"},
        "if_match": {"type": "string", "description": "ETag for optimistic concurrency"},
        "dry_run": {"type": "boolean", "default": False, "description": "Preview without unpublishing"},
    },
    "required": ["id_or_slug"],
    "additionalProperties": False,
}

VALIDATE_POST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "validate_post",
    "description": (
        "Run the full validation pipeline without persisting (self-correction loop).\n"
        "Checks: frontmatter schema, field limits, markdown parse, link checking,\n"
        "image alt text, slug availability, duplicate detection, publish gates.\n"
        "Returns normalised payload preview, would-create info, errors, warnings, stats.\n"
        "Safe: no side effects, read-only.\n"
        "Example: validate_post(site='blog', body_md='# Test\\n\\nContent', title='Test')"
    ),
    "properties": {
        "site": {"type": "string", "description": "Site slug"},
        "title": {"type": "string", "description": "Optional title"},
        "body_md": {"type": "string", "description": "Markdown body content"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tag list"},
        "slug": {"type": "string", "description": "Optional slug"},
        "excerpt": {"type": "string", "description": "Optional excerpt"},
        "frontmatter": {"type": "object", "description": "Optional frontmatter object"},
        "check_links": {"type": "boolean", "default": True, "description": "Whether to check external links"},
    },
    "required": ["site"],
    "additionalProperties": False,
}

LIST_REVISIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "list_revisions",
    "description": (
        "List revision metadata for a post (no body content, cheap for agents).\n"
        "Returns revision number, title, status, editor, timestamp, and diff summary.\n"
        "Safe: no side effects, read-only.\n"
        "Example: list_revisions(id='post-uuid')"
    ),
    "properties": {
        "id": {"type": "string", "description": "Post UUID"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "Page size"},
        "cursor": {"type": "integer", "description": "Revision number to start after"},
    },
    "required": ["id"],
    "additionalProperties": False,
}

REVERT_POST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "revert_post",
    "description": (
        "Revert a post to a previous revision. Creates a NEW revision whose content\n"
        "equals the target revision — history is never rewritten.\n"
        "Destructive: changes live content. Dry-run available (default off).\n"
        "Warning: this publishes the reverted content if the target revision was published.\n"
        "Example: revert_post(id='post-uuid', revision=2)"
    ),
    "properties": {
        "id": {"type": "string", "description": "Post UUID"},
        "revision": {"type": "integer", "description": "Target revision number to revert to"},
        "reason": {"type": "string", "description": "Optional reason for audit trail"},
        "dry_run": {"type": "boolean", "default": False, "description": "Preview without reverting"},
    },
    "required": ["id", "revision"],
    "additionalProperties": False,
}

UPLOAD_ASSET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "upload_asset",
    "description": (
        "Create an asset and return a presigned upload URL (for images/files up to 50 MB).\n"
        "The agent PUTs the file to the returned URL, then calls finalize_asset.\n"
        "Inline upload (base64) available for small files (<= 2 MB) via upload_asset_inline.\n"
        "Side effect: creates pending asset record.\n"
        "Example: upload_asset(site='blog', filename='image.png', content_type='image/png', bytes=12345)"
    ),
    "properties": {
        "site": {"type": "string", "description": "Site slug"},
        "filename": {"type": "string", "description": "Original filename"},
        "content_type": {"type": "string", "description": "MIME type (e.g., image/png)"},
        "bytes": {"type": "integer", "minimum": 1, "description": "Expected byte size"},
        "alt": {"type": "string", "description": "Alt text for images"},
        "kind": {
            "type": "string",
            "enum": ["image", "file"],
            "default": "image",
            "description": "Asset kind",
        },
    },
    "required": ["site", "filename", "bytes"],
    "additionalProperties": False,
}

LIST_ASSETS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "list_assets",
    "description": (
        "List assets for a site, optionally filtered by kind and search query.\n"
        "Safe: no side effects, read-only.\n"
        "Example: list_assets(site='blog', kind='image')"
    ),
    "properties": {
        "site": {"type": "string", "description": "Site slug"},
        "kind": {"type": "string", "enum": ["image", "file"], "description": "Filter by kind"},
        "q": {"type": "string", "description": "Search query (filename)"},
        "cursor": {"type": "string", "description": "Pagination cursor (asset UUID)"},
    },
    "required": ["site"],
    "additionalProperties": False,
}

CHECK_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "check_status",
    "description": (
        "The 'what am I allowed to do' tool — models should call this first.\n"
        "Returns: site info, token scopes, rate-limit quota remaining, content policy mode.\n"
        "Safe: no side effects, read-only.\n"
        "Example: check_status()"
    ),
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

# All tool schemas in a registry
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "create_post": CREATE_POST_SCHEMA,
    "update_post": UPDATE_POST_SCHEMA,
    "get_post": GET_POST_SCHEMA,
    "list_posts": LIST_POSTS_SCHEMA,
    "search_posts": SEARCH_POSTS_SCHEMA,
    "publish_post": PUBLISH_POST_SCHEMA,
    "unpublish_post": UNPUBLISH_POST_SCHEMA,
    "validate_post": VALIDATE_POST_SCHEMA,
    "list_revisions": LIST_REVISIONS_SCHEMA,
    "revert_post": REVERT_POST_SCHEMA,
    "upload_asset": UPLOAD_ASSET_SCHEMA,
    "list_assets": LIST_ASSETS_SCHEMA,
    "check_status": CHECK_STATUS_SCHEMA,
}

# Resource definitions
RESOURCE_LLMSTXT = "agentcms://llms.txt"
RESOURCE_SITE_POSTS_TEMPLATE = "agentcms://site/{site}/posts"
