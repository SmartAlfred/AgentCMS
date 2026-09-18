# Claude Desktop / Cursor / MCP Client Setup — AgentCMS

This guide shows how to connect AgentCMS to any MCP-compatible client using the `agentcms-mcp` server.

---

## Option 1: Local stdio (npx / pipx) — Best for Desktop Apps

### Install

```bash
# Via pipx (recommended for Python environments)
pipx install agentcms-mcp

# Or via npx (if published to npm)
npx @agentcms/mcp
```

### Configure Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "agentcms": {
      "command": "agentcms-mcp",
      "args": [],
      "env": {
        "AGENTCMS_BASE_URL": "https://your-agentcms.example.com",
        "AGENTCMS_TOKEN": "cap_your_capability_token_here"
      }
    }
  }
}
```

Restart Claude Desktop. The AgentCMS tools will appear in the tool palette.

### Configure Cursor

In Cursor Settings → MCP Servers, add:

```json
{
  "name": "agentcms",
  "command": "agentcms-mcp",
  "args": [],
  "env": {
    "AGENTCMS_BASE_URL": "https://your-agentcms.example.com",
    "AGENTCMS_TOKEN": "cap_your_capability_token_here"
  }
}
```

---

## Option 2: Remote Streamable-HTTP — Best for Shared/Remote Instances

If your AgentCMS instance is hosted remotely and you want to use the same MCP endpoint from multiple clients, use the built-in `/mcp` endpoint.

### Get a Capability Link

1. Create a capability link with the needed verbs:
   ```bash
   curl -X POST https://your-agentcms.example.com/v1/admin/capability-links \
     -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"site_slug": "blog", "verbs": ["posts:read", "posts:write", "posts:publish"], "ttl_minutes": 10080}'
   ```

2. Copy the returned `cap_...` token.

### Configure Client with Capability Token URL

**Claude Desktop:**
```json
{
  "mcpServers": {
    "agentcms": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/inspector"],
      "env": {
        "MCP_TRANSPORT": "streamable-http",
        "MCP_ENDPOINT": "https://your-agentcms.example.com/mcp",
        "MCP_AUTH_TOKEN": "cap_your_capability_token_here"
      }
    }
  }
}
```

**Cursor (via MCP proxy):**
```json
{
  "name": "agentcms",
  "url": "https://your-agentcms.example.com/mcp",
  "headers": {
    "Authorization": "Bearer cap_your_capability_token_here"
  }
}
```

> **Note:** Cursor's native MCP support for Streamable-HTTP is evolving. If direct connection fails, use the stdio proxy above.

---

## Required Permissions (Verbs)

| Tool | Required Verb |
|------|---------------|
| `check_status` | (none — public) |
| `create_post` | `posts:write` |
| `update_post` | `posts:write` |
| `get_post` | `posts:read` |
| `list_posts` | `posts:read` |
| `search_posts` | `posts:read` |
| `publish_post` | `posts:publish` |
| `unpublish_post` | `posts:publish` |
| `validate_post` | `posts:write` |
| `list_revisions` | `posts:read` |
| `revert_post` | `posts:write` |
| `upload_asset` | `assets:write` |
| `list_assets` | `posts:read` |

Create your capability link with only the verbs you need (principle of least privilege).

---

## Available Tools (Quick Reference)

| Tool | Description |
|------|-------------|
| `check_status` | **Call this first!** Returns scopes, limits, and site info. |
| `create_post` | Create a draft. Title/slug auto-derived. `dry_run` for validation. |
| `get_post` | Read by ID or slug. `format: "markdown"` returns raw MD. |
| `list_posts` | Paginated list with status/tag filters. |
| `search_posts` | Full-text search. Use `format: "ids"` for cheap duplicate check. |
| `publish_post` | Publish draft (idempotent). |
| `unpublish_post` | Revert to draft. |
| `validate_post` | **Self-correction loop** — dry-run validation with link checking, duplicate detection, stats. |
| `list_revisions` | Revision metadata (no body). |
| `revert_post` | Revert to a revision (creates NEW revision, history preserved). |
| `upload_asset` | Get presigned URL for media upload. |
| `list_assets` | List uploaded assets with markdown embed codes. |

---

## Prompt Pack (for clients without MCP)

If your client doesn't support MCP, copy this system prompt + use the REST API directly:

```
You are an AI agent with API access to AgentCMS via a capability token.
Base URL: https://your-agentcms.example.com
Token: cap_... (in Authorization header)

ALWAYS start with: GET /v1/discover (or check_status equivalent)
Then use these endpoints:
- POST /v1/sites/{site}/posts → create draft
- PATCH /v1/posts/{id} → update
- GET /v1/posts/{id} → read
- POST /v1/posts/{id}/publish → publish
- POST /v1/posts/{id}/unpublish → unpublish
- POST /v1/posts/validate → dry-run validation
- GET /v1/search → search
- POST /v1/sites/{site}/assets → upload media

Errors are application/problem+json — read the `hint` field.
Rate limits: 30 writes/min, 600 reads/min. Back off on 429.
```

---

## Troubleshooting

### "Connection refused" / "Server not found"
- Verify `AGENTCMS_BASE_URL` is correct and reachable.
- For stdio: ensure `agentcms-mcp` is in PATH (`which agentcms-mcp`).

### "Authentication failed"
- Capability token expired? Create a new one.
- Token lacks required verb? Re-create with correct verbs.
- Using wrong token format? Must be `cap_...` not `acms_...`.

### Tools not appearing
- Restart the client after config change.
- Check client logs for MCP handshake errors.
- Try `agentcms-mcp` manually: `echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | agentcms-mcp`

### Rate limited (429)
- The server returns `Retry-After` header. Wait and retry.
- MCP transport should handle this automatically.

---

## Development: Run MCP Server Locally

```bash
# From repo root
source .venv/bin/activate
export AGENTCMS_BASE_URL=http://localhost:8000
export AGENTCMS_TOKEN=cap_your_local_token
python -m app.mcp.server
```

Test with MCP Inspector:
```bash
npx @modelcontextprotocol/inspector
# Connect to stdio: command=python, args=["-m", "app.mcp.server"]
```