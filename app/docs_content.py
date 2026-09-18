"""Shared content for agent-facing surfaces (#9).

This module is the single source of truth for:
* ``GET /llms.txt`` — the machine-readable instruction sheet
* ``GET /`` — the content-negotiated root (text/plain and HTML)
* ``GET /v1/discover`` — the orientation JSON

Every string is generated once here.  The routes call into these helpers
so there is zero drift between the three surfaces.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# llms.txt — under ~4 KB
# ---------------------------------------------------------------------------

LLMS_TXT = """\
# AgentCMS

> API-first CMS whose first user is an AI agent.

## Base URL

    https://your-instance.example.com

All endpoints below are relative to this base.

## Authentication

Two ways to authenticate:

### Bearer token

    Authorization: Bearer acms_demo_abc123_not_real

Get a token from the admin: `POST /v1/admin/tokens` (human-only).

### Capability link

A pre-scoped URL: `https://your-instance.example.com/c/cap_...`
Visit the link to see what it can do, or pass it as a Bearer token.

    Authorization: Bearer cap_abc123_not_real

## Search — check before writing

Before creating, search for existing content:

    GET /v1/search?q=your+topic&site=blog
    GET /v1/search?q=your+topic&format=ids  — cheapest duplicate check ({id,slug} only)

Filters: q, site, status, tag, author_label, from, to, limit, cursor.

## The 5 canonical calls

### 1. Create a draft post

    curl -X POST https://your-instance.example.com/v1/sites/blog/posts \\
      -H "Authorization: Bearer acms_demo_abc123_not_real" \\
      -H "Content-Type: application/json" \\
      -d '{"title": "Hello", "body_md": "# Hello\\n\\nWorld.", "tags": ["demo"]}'

Returns 201 with the draft. Always a draft — status in the body is ignored.

### 2. Read a post

    curl https://your-instance.example.com/v1/posts/POST_ID \\
      -H "Authorization: Bearer acms_demo_abc123_not_real"

Returns 200 with the full post object.

### 3. Update a post

    curl -X PATCH https://your-instance.example.com/v1/posts/POST_ID \\
      -H "Authorization: Bearer acms_demo_abc123_not_real" \\
      -H "Content-Type: application/json" \\
      -d '{"body_md": "# Updated\\n\\nNew content."}'

Returns 200 with the updated post.

### 4. Publish a post

    curl -X POST https://your-instance.example.com/v1/posts/POST_ID/publish \\
      -H "Authorization: Bearer acms_demo_abc123_not_real"

Idempotent — double-publish returns 200, not an error.

### 5. Trash a post

    curl -X DELETE https://your-instance.example.com/v1/posts/POST_ID \\
      -H "Authorization: Bearer acms_demo_abc123_not_real"

Soft-deletes the post. Returns 200.

## Error format

All errors are `application/problem+json` (RFC 9457):

    {"title": "...", "detail": "...", "hint": "...", "code": "..."}

**Read `hint`** — it tells you how to fix the request.

### Common errors

* **401** `unauthenticated` — missing or bad token. Get one at `/v1/admin/tokens`.
* **409** `slug-conflict` — slug taken. Use `suggested_slug` from the response.
* **422** `validation-error` — wrong fields. Check `GET /openapi.json`.

## Limits

* Body size: 256 KB max.
* Rate limit: 30 writes/min per token, 5 publishes/min per token.
* Reads: 600/min per token, 300/min per unauthenticated IP.
* Daily quotas: 500 writes/day, 50 publishes/day per token.
* Capability links: 10 writes/min, 50 writes/day (lower because links can leak).
* Posts are soft-deleted; undo within 60 s via `POST …/unpublish`.

## Backoff and retry

Every response includes rate-limit headers:

    X-RateLimit-Limit: 30
    X-RateLimit-Remaining: 0
    X-RateLimit-Reset: 1726650060
    X-RateLimit-Bucket: writes

If you receive a `429` response:

1. Read the `Retry-After` header — wait at least that many seconds.
2. Use **exponential backoff with jitter**: wait 1s, 2s, 4s, 8s + random jitter.
3. Retry the same request (your `Idempotency-Key` is preserved).
4. Never retry `4xx` errors (except `429` and `503`).
5. Always retry `5xx` errors with backoff.

## Undo / revert

    curl -X POST https://your-instance.example.com/v1/posts/POST_ID/unpublish \\
      -H "Authorization: Bearer acms_demo_abc123_not_real"

Moves a published post back to draft.

## Changelog

    GET /changelog

Append-only list of API changes.

## Full reference

    GET /openapi.json  — OpenAPI 3.1 contract
    GET /docs          — interactive Scalar reference
"""

# ---------------------------------------------------------------------------
# Root instruction sheet (text/plain for models)
# ---------------------------------------------------------------------------


def build_instruction_sheet(base_url: str) -> str:
    """Build the compact text/plain instruction sheet for the root URL."""
    return (
        "You are at the AgentCMS entry point.\n"
        "\n"
        f"Base URL: {base_url}\n"
        "\n"
        "Endpoints:\n"
        "  GET  /llms.txt        — full instruction sheet (this content, expanded)\n"
        "  GET  /openapi.json    — OpenAPI 3.1 contract\n"
        "  GET  /docs            — interactive Scalar reference (human)\n"
        "  GET  /v1/discover     — orientation JSON (service, auth modes, limits)\n"
        "  GET  /changelog       — API change log\n"
        "  GET  /healthz         — liveness (no DB)\n"
        "  GET  /readyz          — readiness (DB check)\n"
        "\n"
        "CRUD on posts (requires Authorization header):\n"
        "  POST   /v1/sites/{site}/posts         — create draft\n"
        "  GET    /v1/posts/{id}                 — read\n"
        "  PATCH  /v1/posts/{id}                 — update\n"
        "  POST   /v1/posts/{id}/publish         — publish (idempotent)\n"
        "  DELETE /v1/posts/{id}                 — trash\n"
        "  POST   /v1/posts/{id}/unpublish       — revert to draft\n"
        "\n"
        "Search & filtering:\n"
        "  GET /v1/search?q=...&site=...&status=...  — full-text search\n"
        "  GET /v1/search?q=...&format=ids           — cheap duplicate check\n"
        "  GET /v1/sites/{site}/tags                  — list tags with counts\n"
        "\n"
        "Auth: Bearer token or capability link (see /c/{token}).\n"
        "Errors: application/problem+json — read the `hint` field.\n"
        "Full spec: GET /openapi.json\n"
        "Instructions: GET /llms.txt\n"
    )


# ---------------------------------------------------------------------------
# Root HTML landing page
# ---------------------------------------------------------------------------


def build_landing_page_html(base_url: str) -> str:
    """Build the human-friendly HTML landing page for GET /."""
    suggested_prompt = (
        f"You are an AI agent with API access to a CMS.\n"
        f"Using only the root URL {base_url} and the instructions at /llms.txt,\n"
        "publish a short blog post about the benefits of AI agents managing content."
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>AgentCMS — API for AI agents</title>
<style>
  body {{ font-family: Inter, -apple-system, sans-serif; color: #1a1a2e; background: #fdfdfe;
         max-width: 52rem; margin: 0 auto; padding: 2rem 1.5rem; line-height: 1.6; }}
  h1 {{ margin: 0 0 .5rem; font-size: 1.75rem; }}
  h2 {{ margin: 1.5rem 0 .5rem; font-size: 1.25rem; }}
  code {{ background: #f3f4f6; padding: .125rem .375rem; border-radius: 3px; font-size: .875em; }}
  pre {{ background: #f3f4f6; padding: 1rem; border-radius: 6px; overflow-x: auto; font-size: .875rem; }}
  .hint {{ background: #f0fdf4; border: 1px solid #86efac; padding: 1rem;
           border-radius: 6px; margin: 1rem 0; }}
  a {{ color: #2563eb; }}
</style>
</head>
<body>
<h1>AgentCMS</h1>
<p>API-first CMS for AI agents. One URL is enough.</p>

<h2>Paste this into your AI</h2>
<pre>{suggested_prompt}</pre>

<h2>Quick start</h2>
<ol>
  <li>Get a token: <code>POST /v1/admin/tokens</code> (human auth required)</li>
  <li>Create a draft: <code>POST /v1/sites/blog/posts</code></li>
  <li>Publish it: <code>POST /v1/posts/{{id}}/publish</code></li>
</ol>

<h2>Capability links</h2>
<p>A capability link is a pre-authenticated URL. Share it with an agent and it
can act within the link's scope — no token management needed.</p>
<div class="hint">
  <strong>Just a link:</strong> <code>GET /c/{{token}}</code> returns
  a plain-text instruction sheet scoped to that link's permissions.
</div>

<h2>References</h2>
<ul>
  <li><a href="/llms.txt">/llms.txt</a> — full instruction sheet for models</li>
  <li><a href="/openapi.json">/openapi.json</a> — OpenAPI 3.1 contract</li>
  <li><a href="/docs">/docs</a> — interactive Scalar reference</li>
  <li><a href="/v1/discover">/v1/discover</a> — orientation JSON</li>
  <li><a href="/changelog">/changelog</a> — API change log</li>
</ul>

<h2>Error format</h2>
<p>Every error is <code>application/problem+json</code> with a <code>hint</code>
field that tells you how to fix the request.</p>
</body>
</html>"""


# ---------------------------------------------------------------------------
# /v1/discover — orientation JSON
# ---------------------------------------------------------------------------


def build_discover_json(base_url: str) -> dict[str, Any]:
    """Build the /v1/discover response body."""
    return {
        "service": "AgentCMS",
        "version": "0.3.0",
        "api_base": f"{base_url}/v1",
        "auth_modes": ["bearer", "capability_link"],
        "docs": f"{base_url}/docs",
        "llms_txt": f"{base_url}/llms.txt",
        "openapi": f"{base_url}/openapi.json",
        "limits": {
            "max_body_bytes": 262144,
            "rate_limit_writes_per_minute": 30,
        },
        "changelog": f"{base_url}/changelog",
    }


# ---------------------------------------------------------------------------
# /changelog — append-only change log
# ---------------------------------------------------------------------------

CHANGELOG_ENTRIES: list[dict[str, str]] = [
    {
        "date": "2026-09-17",
        "version": "0.3.0",
        "type": "added",
        "summary": "MVP: post CRUD, publish/unpublish, capability links, agent-facing docs.",
    },
    {
        "date": "2026-09-17",
        "version": "0.3.0",
        "type": "added",
        "summary": "GET /llms.txt, GET / (content-negotiated), GET /v1/discover, GET /changelog.",
    },
]


def build_changelog() -> list[dict[str, str]]:
    """Return the append-only changelog."""
    return list(CHANGELOG_ENTRIES)
