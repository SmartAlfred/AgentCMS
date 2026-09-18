# Cursor MCP Integration — AgentCMS

Configure Cursor to use AgentCMS as an MCP server for AI-assisted content publishing.

---

## Quick Setup (Cursor 0.40+)

### 1. Install the MCP Server

```bash
pipx install agentcms-mcp
# or
pip install agentcms-mcp
```

### 2. Create a Capability Link

In your AgentCMS instance:

```bash
curl -X POST https://your-agentcms.example.com/v1/admin/capability-links \
  -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"site_slug": "blog", "verbs": ["posts:read", "posts:write", "posts:publish"], "ttl_minutes": 10080}'
```

Copy the `cap_...` token from the response.

### 3. Add to Cursor Settings

1. Open Cursor Settings (`Cmd/Ctrl + ,`)
2. Search for "MCP" or go to **Features → MCP Servers**
3. Click **Add MCP Server**
4. Fill in:

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

5. Click **Save** → Cursor will restart the MCP connection.

### 4. Verify

Open the AI pane (`Cmd/Ctrl + L`) and type:
> "What AgentCMS tools are available?"

You should see the tool list including `create_post`, `publish_post`, etc.

---

## Usage Examples

### Publish a Blog Post

> **User**: "Create a post about 'Why TypeScript Wins' with tags ['typescript', 'programming'] and publish it."

> **Cursor**: *Calls check_status → create_post → publish_post*
>
> "Done! Published at https://your-agentcms.example.com/posts/why-typescript-wins"

### Search Before Writing

> **User**: "Search for existing posts about 'React Server Components' on the blog."

> **Cursor**: *Calls search_posts*
>
> "Found 3 posts. Most recent: 'RSC Deep Dive' (published 2026-01-15). Want me to read it?"

### Dry-Run Validation

> **User**: "Validate this draft without publishing: [pastes markdown]"

> **Cursor**: *Calls validate_post with dry_run=true*
>
> "Validation passed. Would create slug 'my-draft'. 2 warnings: 1 link unreachable, 1 image missing alt text."

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `AGENTCMS_BASE_URL` | Yes | Your AgentCMS instance URL (e.g., `https://cms.example.com`) |
| `AGENTCMS_TOKEN` | Yes | Capability token (`cap_...`) with needed verbs |
| `AGENTCMS_LOG_LEVEL` | No | `DEBUG`, `INFO`, `WARNING` (default: `INFO`) |

---

## Alternative: Streamable-HTTP (Remote)

If you prefer not to run the MCP server locally, use the hosted `/mcp` endpoint:

```json
{
  "name": "agentcms",
  "url": "https://your-agentcms.example.com/mcp",
  "headers": {
    "Authorization": "Bearer cap_your_capability_token_here"
  }
}
```

> **Note:** This requires Cursor 0.42+ with Streamable-HTTP support. If it doesn't work, fall back to stdio.

---

## Troubleshooting

### "MCP server failed to start"
- Run `agentcms-mcp` manually in terminal to see errors
- Check `AGENTCMS_BASE_URL` is reachable: `curl https://your-agentcms.example.com/healthz`
- Verify token format: must start with `cap_`

### Tools not showing in Cursor
- Restart Cursor completely (Cmd/Ctrl + Shift + P → "Developer: Reload Window")
- Check **Output → MCP** panel for connection logs
- Ensure capability token has at least `posts:read`

### "403 Forbidden" on tool calls
- Token missing required verb (see verb table below)
- Token expired — create a new capability link
- Token bound to different site — ensure `site_slug` matches

### Rate Limited (429)
- Wait for `Retry-After` seconds
- Reduce concurrent requests

---

## Verb Requirements

| Tool | Verb |
|------|------|
| `check_status` | (public) |
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

---

## Advanced: Multiple Sites

Create separate capability links per site and add multiple MCP servers:

```json
{
  "mcpServers": {
    "agentcms-blog": {
      "command": "agentcms-mcp",
      "args": [],
      "env": {
        "AGENTCMS_BASE_URL": "https://cms.example.com",
        "AGENTCMS_TOKEN": "cap_blog_token..."
      }
    },
    "agentcms-docs": {
      "command": "agentcms-mcp",
      "args": [],
      "env": {
        "AGENTCMS_BASE_URL": "https://cms.example.com",
        "AGENTCMS_TOKEN": "cap_docs_token..."
      }
    }
  }
}
```

---

## Development Tip

For local development with the demo:

```bash
# Terminal 1: Start AgentCMS
make dev

# Terminal 2: Create a local capability link
curl -X POST http://localhost:8000/v1/admin/capability-links \
  -H "Authorization: Bearer acms_local_token" \
  -H "Content-Type: application/json" \
  -d '{"site_slug": "blog", "verbs": ["posts:read", "posts:write", "posts:publish"]}'

# Terminal 3: Run MCP server with the returned token
export AGENTCMS_BASE_URL=http://localhost:8000
export AGENTCMS_TOKEN=cap_...
agentcms-mcp
```