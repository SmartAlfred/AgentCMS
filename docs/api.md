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
