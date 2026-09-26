# MCP Setup Guide — AgentCMS

Complete guide to connecting AgentCMS to any MCP-compatible client (Claude Desktop, Cursor, ChatGPT Desktop, Continue, etc.).

---

## Architecture

```
┌─────────────────┐     MCP Protocol      ┌──────────────────┐
│  MCP Client     │ ◄──────────────────► │  AgentCMS MCP    │
│  (Claude,       │   JSON-RPC 2.0       │  Server          │
│   Cursor, etc.) │   over stdio or      │  (app.mcp)       │
└─────────────────┘   Streamable-HTTP    └────────┬─────────┘
                                                  │
                                    ┌─────────────┴─────────────┐
                                    ▼                           ▼
                          ┌───────────────┐             ┌───────────────┐
                          │  FastAPI App  │             │  PostgreSQL   │
                          │  (same proc)  │             │  Database     │
                          └───────────────┘             └───────────────┘
```

Two transport options:
1. **stdio** — Local process (`npx @agentcms/mcp` / `pipx install agentcms-mcp`)
2. **Streamable-HTTP** — Hosted at `/mcp` on your AgentCMS instance (capability token in URL or header)

---

## Prerequisites

1. **AgentCMS instance** running (self-hosted or cloud)
2. **Capability link** with appropriate verbs (see below)
3. **MCP-compatible client** (Claude Desktop 0.8+, Cursor 0.40+, Continue, etc.)

---

## Step 1: Mint a Token

MCP clients are pre-scoped tokens that work without header management — perfect for MCP.
Mint one with `POST /v1/admin/tokens`; a `cap_...` capability link works the same way
(no HTTP route mints those yet — the seeder does: `python -m scripts.seed`).

```bash
curl -X POST https://your-agentcms.example.com/v1/admin/tokens \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "label": "Claude Desktop MCP",
    "scopes": ["posts:read", "posts:write", "posts:publish"],
    "expires_in_days": 7
  }'
```

**Response:**
```json
{
  "token": "acms_...",
  "id": "0f6c2d5a-...",
  "actor_id": "9b1e...",
  "label": "Claude Desktop MCP",
  "scopes": ["posts:read", "posts:write", "posts:publish"],
  "expires_at": "2026-10-01T00:00:00Z",
  "created_at": "2026-09-24T00:00:00Z"
}
```

Copy the `token` value (starts with `acms_`; `cap_...` capability links work too).

### Verb Reference

| Verb | Allows |
|------|--------|
| `posts:read` | `get_post`, `list_posts`, `search_posts`, `list_revisions`, `list_assets` |
| `posts:write` | `create_post`, `update_post`, `validate_post`, `revert_post`, `upload_asset` |
| `posts:publish` | `publish_post`, `unpublish_post` |
| `assets:write` | `upload_asset` |

**Principle of least privilege:** Only grant verbs you need.

---

## Step 2: Configure Your Client

### Claude Desktop (macOS/Windows/Linux)

**Config file:** `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows)

```json
{
  "mcpServers": {
    "agentcms": {
      "command": "agentcms-mcp",
      "args": [],
      "env": {
        "AGENTCMS_BASE_URL": "https://your-agentcms.example.com",
        "AGENTCMS_TOKEN": "cap_blog_abc123..."
      }
    }
  }
}
```

**Restart Claude Desktop.** Tools appear in the 🔧 tool palette.

### Cursor (VS Code fork)

**Settings → Features → MCP Servers → Add Server:**

```json
{
  "name": "agentcms",
  "command": "agentcms-mcp",
  "args": [],
  "env": {
    "AGENTCMS_BASE_URL": "https://your-agentcms.example.com",
    "AGENTCMS_TOKEN": "cap_blog_abc123..."
  }
}
```

### Continue (VS Code / JetBrains extension)

**`.continue/config.json`:**

```json
{
  "mcpServers": [
    {
      "name": "agentcms",
      "command": "agentcms-mcp",
      "args": [],
      "env": {
        "AGENTCMS_BASE_URL": "https://your-agentcms.example.com",
        "AGENTCMS_TOKEN": "cap_blog_abc123..."
      }
    }
  ]
}
```

### ChatGPT Desktop (with MCP support)

Currently uses **Custom GPTs + Actions** instead of native MCP. See [chatgpt-actions.md](chatgpt-actions.md).

### Generic JSON-RPC Client

If your client speaks raw JSON-RPC over stdio:

```bash
# Start server
AGENTCMS_BASE_URL=https://your-agentcms.example.com \
AGENTCMS_TOKEN=cap_blog_abc123... \
agentcms-mcp

# Send initialize
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' | agentcms-mcp

# List tools
echo '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | agentcms-mcp

# Call tool
echo '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"check_status","arguments":{}}}' | agentcms-mcp
```

---

## Step 3: Streamable-HTTP (Remote/Shared)

For clients that support HTTP transport, use the hosted endpoint:

**Endpoint:** `https://your-agentcms.example.com/mcp`
**Auth:** `Authorization: Bearer cap_...` header

**Cursor (0.42+):**
```json
{
  "name": "agentcms",
  "url": "https://your-agentcms.example.com/mcp",
  "headers": {
    "Authorization": "Bearer cap_blog_abc123..."
  }
}
```

**MCP Inspector (testing):**
```bash
npx @modelcontextprotocol/inspector
# Transport: Streamable-HTTP
# URL: https://your-agentcms.example.com/mcp
# Headers: Authorization: Bearer cap_...
```

---

## Available Tools (Complete)

| Tool | Description | Dry-run | Destructive |
|------|-------------|---------|-------------|
| `check_status` | Permissions, limits, site info | N/A | No |
| `create_post` | Create draft (title/slug auto) | ✅ Yes | No |
| `update_post` | Partial update | ✅ Yes | No |
| `get_post` | Read by ID/slug (JSON/MD) | N/A | No |
| `list_posts` | Paginated, filtered | N/A | No |
| `search_posts` | Full-text search | N/A | No |
| `publish_post` | Publish draft (idempotent) | N/A | No |
| `unpublish_post` | Revert to draft | N/A | No |
| `validate_post` | **Self-correction loop** | ✅ Yes | No |
| `list_revisions` | Revision metadata | N/A | No |
| `revert_post` | Revert to revision (new rev) | ❌ No | ⚠️ Yes |
| `upload_asset` | Presigned upload URL | N/A | No |
| `list_assets` | List assets with markdown | N/A | No |

---

## Error Handling

All errors follow `application/problem+json` (RFC 9457) — **never bare strings**.

```json
{
  "type": "https://agentcms.dev/problems/forbidden",
  "title": "Forbidden",
  "status": 403,
  "detail": "This link lacks the `posts:publish` permission.",
  "code": "forbidden",
  "hint": "Ask the site owner for a link that grants `posts:publish`.",
  "request_id": "req-abc123"
}
```

**Model behavior:** Read `hint` field → fix the issue → retry.

---

## Rate Limits

| Bucket | Limit | Window |
|--------|-------|--------|
| writes | 30 | 1 min |
| reads | 600 | 1 min |
| publishes | 50 | 1 day |
| writes | 500 | 1 day |

Headers on every response:
- `X-RateLimit-Limit`
- `X-RateLimit-Remaining`
- `X-RateLimit-Reset`
- `Retry-After` (on 429)

---

## Testing Your Setup

### 1. Verify MCP handshake
```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' | AGENTCMS_BASE_URL=... AGENTCMS_TOKEN=... agentcms-mcp
```

### 2. List tools
```bash
echo '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | AGENTCMS_BASE_URL=... AGENTCMS_TOKEN=... agentcms-mcp
```

### 3. Call check_status
```bash
echo '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"check_status","arguments":{}}}' | AGENTCMS_BASE_URL=... AGENTCMS_TOKEN=... agentcms-mcp
```

### 4. Create + publish a post
```bash
# Create
echo '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"create_post","arguments":{"site":"blog","body_md":"# Test\n\nHello MCP!"}}}' | AGENTCMS_BASE_URL=... AGENTCMS_TOKEN=... agentcms-mcp

# Publish (use id from create response)
echo '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"publish_post","arguments":{"id_or_slug":"<id-from-create>"}}}' | AGENTCMS_BASE_URL=... AGENTCMS_TOKEN=... agentcms-mcp
```

---

## Troubleshooting Checklist

| Symptom | Check |
|---------|-------|
| Tools don't appear | Restart client; check MCP logs |
| "Connection refused" | `AGENTCMS_BASE_URL` correct? Server running? |
| "Unauthenticated" | Token format `cap_...`? Not expired? |
| "Forbidden: lacks `posts:publish`" | Capability link has the verb? |
| "Site not found" | `site_slug` in capability link matches tool call? |
| Rate limited (429) | Wait `Retry-After`; reduce frequency |
| SSE connection drops (HTTP) | Some proxies buffer SSE; try stdio instead |

---

## Security Notes

- **Capability tokens are bearer secrets** — treat like passwords
- Use `cap_...` tokens (not `acms_...` API tokens) for MCP
- Tokens embed site slug — a token for `blog` cannot access `docs`
- Set `ttl_minutes` and `uses_remaining` for automatic expiry
- Revoke via `DELETE /v1/admin/tokens/{token_id}` if compromised (a capability link
  is a token record; send it with `X-Admin-Token`)
- Never log full tokens — the server redacts automatically

---

## Development: Local Testing

```bash
# 1. Start AgentCMS locally
make dev

# 2. Mint a token with the scopes MCP needs (ADMIN_TOKEN is the bootstrap secret in .env)
curl -X POST http://localhost:8000/v1/admin/tokens \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"label": "mcp-local", "scopes": ["posts:read", "posts:write", "posts:publish"]}'

# 3. Run MCP server
export AGENTCMS_BASE_URL=http://localhost:8000
export AGENTCMS_TOKEN=acms_...  # from step 2
python -m app.mcp.server

# 4. Test with MCP Inspector
npx @modelcontextprotocol/inspector
# Transport: stdio
# Command: python
# Args: -m app.mcp.server
```