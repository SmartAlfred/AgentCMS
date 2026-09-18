# Post API v1

Endpoints for creating, reading, updating, publishing, unpublishing, and trashing posts.

## Endpoints

```
POST   /v1/sites/{site}/posts                 create (always draft)
GET    /v1/sites/{site}/posts                 list (cursor pagination)
GET    /v1/posts/{id_or_slug}                 read one
PATCH  /v1/posts/{id}                         update (partial)
POST   /v1/posts/{id}/publish                 publish
POST   /v1/posts/{id}/unpublish               take down
DELETE /v1/posts/{id}                         trash (recoverable)
GET    /v1/search                             full-text search with filters
GET    /v1/sites/{site}/tags                  list tags with counts
POST   /v1/sites/{site}/tags/{tag}/merge      merge tags (admin)
POST   /v1/sites/{site}/assets                create asset (presigned URL)
POST   /v1/sites/{site}/assets/inline         inline upload (base64, max 2MB)
POST   /v1/assets/{id}/finalize               validate, sniff, generate variants
GET    /v1/sites/{site}/assets                list assets
GET    /v1/assets/{id}                        get asset details
DELETE /v1/assets/{id}                        soft-delete asset
```

## Create a post

```bash
curl -X POST http://localhost:8000/v1/sites/blog/posts \
  -H "Content-Type: application/json" \
  -d '{"body_md": "# Why agents need a CMS\n\nAgents need structured content."}'
```

Response (`201`):

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "site_id": "...",
  "slug": "why-agents-need-a-cms",
  "title": "Why agents need a CMS",
  "body_md": "# Why agents need a CMS\n\nAgents need structured content.",
  "excerpt": null,
  "status": "draft",
  "frontmatter": {},
  "tags": [],
  "url": "/posts/why-agents-need-a-cms",
  "markdown_url": "/posts/why-agents-need-a-cms.md",
  "revision": 1,
  "created_at": "2026-01-01T00:00:00Z",
  "updated_at": "2026-01-01T00:00:00Z",
  "published_at": null
}
```

The `Location` header contains `/v1/posts/{id}`.

### Request body

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `body_md` | string | **yes** | Markdown body |
| `title` | string | no | Derived from first `# H1` in `body_md` if omitted |
| `slug` | string | no | Derived from title if omitted; 409 if taken |
| `tags` | string[] | no | Max 20 tags |
| `excerpt` | string | no | Auto-derived when omitted |
| `frontmatter` | object | no | Arbitrary JSON metadata |

**Important:** `status` in the request body is **ignored**. Create always returns a draft.

### Dry run

Append `?dry_run=true` to validate without persisting:

```bash
curl -X POST "http://localhost:8000/v1/sites/blog/posts?dry_run=true" \
  -H "Content-Type: application/json" \
  -d '{"body_md": "# Test"}'
```

## List posts

```bash
curl http://localhost:8000/v1/sites/blog/posts
curl "http://localhost:8000/v1/sites/blog/posts?status=draft&limit=10"
```

Response:

```json
{
  "items": [...],
  "next_cursor": "2026-01-01T00:00:00|550e8400-...",
  "count": 20
}
```

### Query parameters

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `status` | string | null | Filter by `draft`, `published`, etc. |
| `limit` | int | 20 | 1–100 |
| `cursor` | string | null | Pass `next_cursor` from previous response |

## Read a post

```bash
curl http://localhost:8000/v1/posts/why-agents-need-a-cms
curl http://localhost:8000/v1/posts/550e8400-e29b-41d4-a716-446655440000
```

## Update a post

```bash
curl -X PATCH http://localhost:8000/v1/posts/550e8400-... \
  -H "Content-Type: application/json" \
  -d '{"title": "New title", "tags": ["ai"]}'
```

Supports `?dry_run=true`.

## Publish a post

```bash
curl -X POST http://localhost:8000/v1/posts/550e8400-.../publish
```

Idempotent — publishing twice returns `200` with `warnings[]`.

## Unpublish a post

```bash
curl -X POST http://localhost:8000/v1/posts/550e8400-.../unpublish
```

## Trash a post

```bash
curl -X DELETE http://localhost:8000/v1/posts/550e8400-...
```

Soft delete — status becomes `trashed`, `deleted_at` is set. Revisions are preserved.

## Error responses

All errors are `application/problem+json` (RFC 9457):

```json
{
  "type": "https://agentcms.dev/problems/slug-conflict",
  "title": "Slug already in use",
  "status": 409,
  "detail": "Slug 'hello' is already taken in site 'blog'.",
  "instance": "/v1/sites/blog/posts",
  "code": "slug-conflict",
  "hint": "Retry the same request with the free slug in `suggested_slug`.",
  "suggested_slug": "hello-2",
  "request_id": "req-abc-123"
}
```

## Full-text search

```bash
curl "http://localhost:8000/v1/search?q=python+deployment&site=blog&status=published"
```

Response:

```json
{
  "results": [
    {
      "id": "...",
      "title": "Python Deployment Guide",
      "slug": "python-deployment-guide",
      "status": "published",
      "published_at": "2026-01-15T00:00:00Z",
      "snippet": "Use Docker for Python deployment...",
      "score": 0.95,
      "url": "/posts/python-deployment-guide",
      "markdown_url": "/posts/python-deployment-guide.md"
    }
  ],
  "total_estimate": 1,
  "next_cursor": null,
  "query": "python deployment"
}
```

### Query parameters

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `q` | string | null | Full-text search query (empty = list-with-filters) |
| `site` | string | null | Filter by site slug |
| `status` | string | null | Filter by status |
| `tag` | string | null | Filter by tag slug |
| `author_label` | string | null | Filter by author label |
| `from` | string | null | Created after (ISO-8601) |
| `to` | string | null | Created before (ISO-8601) |
| `published_after` | string | null | Published after (ISO-8601) |
| `published_before` | string | null | Published before (ISO-8601) |
| `updated_since` | string | null | Updated since (ISO-8601) |
| `limit` | int | 20 | 1–100 |
| `cursor` | string | null | Pass `next_cursor` from previous response |
| `format` | string | null | `ids` for lightweight `{id, slug}` results |

### Cheap duplicate check

Use `?format=ids` for the cheapest "have I written this?" call:

```bash
curl "http://localhost:8000/v1/search?q=my+topic&format=ids"
```

Response:

```json
{
  "results": [{"id": "...", "slug": "my-topic"}],
  "total_estimate": 1,
  "next_cursor": null,
  "query": "my topic"
}
```

## Tags

### List tags with counts

```bash
curl http://localhost:8000/v1/sites/blog/tags
```

Response:

```json
{
  "items": [
    {"id": "...", "slug": "python", "name": "Python", "post_count": 5, "created_at": "..."},
    {"id": "...", "slug": "fastapi", "name": "Fastapi", "post_count": 2, "created_at": "..."}
  ],
  "count": 2
}
```

### Merge tags (admin)

```bash
curl -X POST http://localhost:8000/v1/sites/blog/tags/python/merge \
  -H "Content-Type: application/json" \
  -d '{"target_tag": "programming"}'
```

Response:

```json
{
  "source_tag": "python",
  "target_tag": "programming",
  "affected_posts": 5
}
```

Tag merge rewrites `post_tags` for all affected posts. Tag changes do NOT create content revisions but emit audit events per affected post.

## Media uploads

### Create an asset (presigned URL)

```bash
curl -X POST http://localhost:8000/v1/sites/blog/assets \
  -H "Content-Type: application/json" \
  -d '{"filename": "hero.png", "content_type": "image/png", "bytes": 102400, "alt": "Hero image", "kind": "image"}'
```

Response (`201`):

```json
{
  "id": "...",
  "upload_url": "http://localhost:9000/agentcms-media/media/.../hero.png?...",
  "upload_method": "PUT",
  "upload_headers": {"Content-Type": "image/png"},
  "expires_in": 900,
  "asset_url": "/media/{sha256}/hero.png",
  "markdown": "![Hero image](/media/{sha256}/hero.png)",
  "variants": {},
  "max_bytes": 52428800
}
```

### Upload bytes

```bash
curl -X PUT <upload_url> -H "Content-Type: image/png" --data-binary @hero.png
```

### Finalize

```bash
curl -X POST http://localhost:8000/v1/assets/{id}/finalize
```

Response:

```json
{
  "id": "...",
  "status": "ready",
  "filename": "hero.png",
  "content_type": "image/png",
  "sha256": "...",
  "width": 800,
  "height": 600,
  "asset_url": "/media/{sha256}/hero.png",
  "markdown": "![Hero image](/media/{sha256}/hero.png)",
  "variants": {
    "thumb": {"url": "/media/{hash}/hero.thumb.webp", "width": 256, "height": 256},
    "inline": {"url": "/media/{hash}/hero.inline.webp", "width": 1200, "height": 900},
    "og": {"url": "/media/{hash}/hero.og.webp", "width": 1200, "height": 630}
  }
}
```

### Inline upload (escape hatch, max 2 MB)

```bash
curl -X POST http://localhost:8000/v1/sites/blog/assets/inline \
  -H "Content-Type: application/json" \
  -d '{"filename": "small.png", "data_base64": "<base64>", "kind": "image"}'
```

### List assets

```bash
curl http://localhost:8000/v1/sites/blog/assets
curl "http://localhost:8000/v1/sites/blog/assets?kind=image&q=hero"
```

### Content policy

SVG, HTML, executables are rejected with a clear error listing allowed types.
Magic bytes are verified -- a `.png` with HTML content is rejected.

### Image variants

Generated on finalize:
- `thumb`: 256x256 center crop
- `inline`: 1200 wide, proportional height
- `og`: 1200x630 center crop

EXIF (including GPS) is stripped automatically.

### Serving

Immutable, content-hashed URLs: `/media/{sha256}/{filename}`.
Cache-Control: `public, max-age=31536000, immutable`.
