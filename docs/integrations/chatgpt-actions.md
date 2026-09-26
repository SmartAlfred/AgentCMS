# ChatGPT Custom GPT Integration — AgentCMS Actions

This document provides the OpenAPI Actions manifest and system prompt for creating a **Custom GPT** that can publish content to AgentCMS via the MCP tools.

> **Note:** If you have an MCP client (Claude Desktop, Cursor, etc.), use the MCP server instead — it's more reliable and doesn't require manual manifest maintenance. This Actions manifest is for ChatGPT users who cannot use MCP.

---

## Quick Setup (3 steps)

1. **Mint a token** in AgentCMS with the `posts:write` and `posts:publish` scopes:
   ```bash
   curl -X POST https://your-agentcms.example.com/v1/admin/tokens \
     -H "X-Admin-Token: $ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"label": "chatgpt-actions", "scopes": ["posts:write", "posts:publish"], "expires_in_days": 7}'
   ```
   Copy the returned `acms_...` token (a `cap_...` capability link works too; the
   seeder mints those).

2. **Create a Custom GPT** in ChatGPT:
   - Go to **Explore GPTs → Create**
   - Name: "AgentCMS Publisher"
   - Paste the **System Prompt** below into **Instructions**
   - Click **Configure → Actions → Create new action**
   - Paste the **OpenAPI Schema** below into the schema editor
   - Save

3. **Add authentication**:
   - In the Actions config, set **Authentication** → **Bearer Token**
   - Paste your `cap_...` token (the full token including `cap_` prefix)

---

## System Prompt (paste into Instructions)

```
You are an AI agent with API access to AgentCMS, an API-first CMS.
Your job: help the user publish content by calling the available tools.

ALWAYS start by calling `check_status` to understand your permissions and limits.
Then use the other tools as needed:

- `create_post` — create a draft (title from H1 if omitted, slug from title)
- `update_post` — modify a draft
- `get_post` — read a post (JSON or markdown)
- `list_posts` — paginated list with filters
- `search_posts` — full-text search (use first to avoid duplicates!)
- `publish_post` — publish a draft (idempotent)
- `unpublish_post` — take down a published post
- `validate_post` — dry-run validation (self-correction loop)
- `list_revisions` / `revert_post` — version control
- `upload_asset` / `list_assets` — media uploads

Key rules:
- Create NEVER publishes. Draft → publish_post → published.
- All errors are structured: read the `hint` field and retry.
- Rate limits: 30 writes/min, 600 reads/min. Respect Retry-After.
- Use `dry_run: true` on create/update to validate before committing.
- For destructive ops (revert_post), confirm with the user first.
```

---

## OpenAPI 3.1 Actions Manifest

Copy this entire JSON into the **Schema** field in the Custom GPT Actions config.

```json
{
  "openapi": "3.1.0",
  "info": {
    "title": "AgentCMS MCP Tools",
    "version": "0.3.1",
    "description": "Typed tool calls for AgentCMS via MCP-over-HTTP. Use with a capability token (cap_...) in the Authorization header."
  },
  "servers": [
    {
      "url": "https://your-agentcms.example.com",
      "description": "Production AgentCMS instance"
    }
  ],
  "components": {
    "securitySchemes": {
      "bearerAuth": {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "Capability token (cap_...)",
        "description": "Bearer token minted by POST /v1/admin/tokens (acms_...) or a cap_... capability link"
      }
    }
  },
  "security": [{"bearerAuth": []}],
  "paths": {
    "/mcp": {
      "post": {
        "operationId": "mcpCallTool",
        "summary": "Call an MCP tool",
        "description": "Single endpoint for all MCP tool calls. The tool name and arguments go in the JSON-RPC request body.",
        "requestBody": {
          "required": true,
          "content": {
            "application/json": {
              "schema": {
                "type": "object",
                "properties": {
                  "jsonrpc": {"type": "string", "const": "2.0"},
                  "id": {"type": ["string", "integer"]},
                  "method": {"type": "string", "enum": ["tools/call"]},
                  "params": {
                    "type": "object",
                    "properties": {
                      "name": {
                        "type": "string",
                        "enum": [
                          "check_status",
                          "create_post",
                          "update_post",
                          "get_post",
                          "list_posts",
                          "search_posts",
                          "publish_post",
                          "unpublish_post",
                          "validate_post",
                          "list_revisions",
                          "revert_post",
                          "upload_asset",
                          "list_assets"
                        ]
                      },
                      "arguments": {"type": "object"}
                    },
                    "required": ["name", "arguments"]
                  }
                },
                "required": ["jsonrpc", "id", "method", "params"]
              }
            }
          }
        },
        "responses": {
          "200": {
            "description": "Tool result",
            "content": {
              "application/json": {
                "schema": {
                  "type": "object",
                  "properties": {
                    "jsonrpc": {"type": "string"},
                    "id": {"type": ["string", "integer"]},
                    "result": {
                      "type": "object",
                      "properties": {
                        "content": {
                          "type": "array",
                          "items": {
                            "type": "object",
                            "properties": {
                              "type": {"type": "string", "enum": ["text"]},
                              "text": {"type": "string"}
                            }
                          }
                        },
                        "isError": {"type": "boolean"}
                      }
                    },
                    "error": {
                      "type": "object",
                      "properties": {
                        "code": {"type": "integer"},
                        "message": {"type": "string"}
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
```

---

## Tool Argument Schemas (for reference)

The `arguments` object in the JSON-RPC call must match these schemas:

### check_status
```json
{}
```

### create_post
```json
{
  "site": "string (site slug, e.g. 'blog')",
  "body_md": "string (required, markdown content)",
  "title": "string? (optional, derived from first H1)",
  "tags": "string[]? (optional, tag slugs)",
  "slug": "string? (optional, derived from title)",
  "dry_run": "boolean? (default false, validate only)"
}
```

### update_post
```json
{
  "id_or_slug": "string (post UUID or slug)",
  "title": "string?",
  "body_md": "string?",
  "tags": "string[]?",
  "slug": "string?",
  "excerpt": "string?",
  "frontmatter": "object?",
  "dry_run": "boolean? (default false)",
  "if_match": "string? (ETag from previous GET)"
}
```

### get_post
```json
{
  "id_or_slug": "string",
  "format": "string? (json|markdown, default json)"
}
```

### list_posts
```json
{
  "site": "string",
  "status": "string? (draft|published|pending_review|trashed)",
  "tag": "string?",
  "limit": "integer? (1-100, default 20)",
  "cursor": "string?"
}
```

### search_posts
```json
{
  "site": "string",
  "q": "string"
}
```

### publish_post
```json
{
  "id_or_slug": "string"
}
```

### unpublish_post
```json
{
  "id_or_slug": "string"
}
```

### validate_post
```json
{
  "site": "string",
  "body_md": "string?",
  "title": "string?",
  "slug": "string?",
  "tags": "string[]?",
  "excerpt": "string?",
  "frontmatter": "object?",
  "existing_post_id": "string? (for update validation)",
  "check_links": "boolean? (default true)"
}
```

### list_revisions
```json
{
  "id_or_slug": "string",
  "limit": "integer? (1-100, default 20)",
  "cursor": "integer?"
}
```

### revert_post
```json
{
  "id_or_slug": "string",
  "revision": "integer",
  "reason": "string?"
}
```

### upload_asset
```json
{
  "site": "string",
  "filename": "string",
  "content_type": "string?",
  "bytes": "integer",
  "alt": "string?",
  "kind": "string? (image|file, default image)"
}
```

### list_assets
```json
{
  "site": "string",
  "cursor": "string?",
  "kind": "string? (image|file)",
  "q": "string?"
}
```

---

## Example: Publish a Post via ChatGPT

Once configured, you can chat with the GPT:

> **User**: "Publish a short post about why agents need a CMS. Title: 'Agents Need Structure'. Tags: ['ai', 'cms']."

> **Assistant**: *calls check_status → create_post → publish_post*

> **Assistant**: "Done! Published at https://your-agentcms.example.com/posts/agents-need-structure"

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| "401 Unauthenticated" | Check the capability token is valid and not expired. Verify it has `posts:write` and `posts:publish` verbs. |
| "403 Forbidden: token lacks `posts:publish`" | Re-create the capability link with the `posts:publish` verb. |
| "429 Rate limited" | Wait for `Retry-After` seconds. The GPT should handle this automatically. |
| "Tool not found" | Ensure the tool name in `arguments.name` matches exactly (snake_case). |
| "Invalid JSON-RPC" | The request must follow JSON-RPC 2.0 format with `jsonrpc`, `id`, `method`, `params`. |

---

## Alternative: Direct REST Calls (no MCP)

If you prefer REST over MCP, use the `/v1/` endpoints directly with the same capability token:

```bash
# Create draft
curl -X POST https://your-agentcms.example.com/v1/sites/blog/posts \
  -H "Authorization: Bearer cap_..." \
  -H "Content-Type: application/json" \
  -d '{"body_md": "# Hello\n\nWorld."}'

# Publish
curl -X POST https://your-agentcms.example.com/v1/posts/{id}/publish \
  -H "Authorization: Bearer cap_..."
```

The OpenAPI spec at `/openapi.json` has the complete REST contract.