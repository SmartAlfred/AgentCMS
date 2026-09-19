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

from app.config import get_settings

# ---------------------------------------------------------------------------
# llms.txt — under ~4 KB
# ---------------------------------------------------------------------------


def _build_llms_txt(base_url: str) -> str:
    """Build the llms.txt instruction sheet with the given base URL."""
    return f"""\
# AgentCMS

> API-first CMS whose first user is an AI agent.

## Base URL

    {base_url}

## Authentication

Bearer token or capability link (`/c/cap_...`):

    Authorization: Bearer acms_demo_abc123_not_real

Token: `POST /v1/admin/tokens` (human-only).

## Search — check before writing

    GET /v1/search?q=topic&site=blog
    GET /v1/search?q=topic&format=ids  — cheap duplicate check

Filters: q, site, status, tag, author_label, from, to, limit, cursor.

## The 5 canonical calls

### 1. Create a draft

    curl -X POST /v1/sites/blog/posts \\
      -H "Authorization: Bearer TOKEN" \\
      -H "Content-Type: application/json" \\
      -d '{{"title": "Hello", "body_md": "# Hello\\n\\nWorld.", "tags": ["demo"]}}'

### 2. Read

    curl /v1/posts/POST_ID -H "Authorization: Bearer TOKEN"

### 3. Update

    curl -X PATCH /v1/posts/POST_ID \\
      -H "Authorization: Bearer TOKEN" -d '{{"body_md": "# Updated."}}'

### 4. Publish

    curl -X POST /v1/posts/POST_ID/publish -H "Authorization: Bearer TOKEN"

Idempotent — double-publish returns 200.

### 5. Trash

    curl -X DELETE /v1/posts/POST_ID -H "Authorization: Bearer TOKEN"

Soft-deletes. Undo: `POST …/unpublish`.

## Errors

`application/problem+json` — read the `hint` field to fix requests.

Common: `unauthenticated` (401), `slug-conflict` (409), `validation-error` (422).

## Limits

Rate: 30 writes/min, 600 reads/min. Daily: 500 writes, 50 publishes.
Body: 256 KB. Undo: 60s. Backoff on 429 (read `Retry-After`).

## Media uploads

1. `POST /v1/sites/{{site}}/assets` → presigned PUT URL + `markdown`
2. `PUT <upload_url>` with file bytes
3. `POST /v1/assets/{{id}}/finalize` → `sha256`, variants

Inline: `POST …/assets/inline` with `data_base64` (max 2 MB).
SVG/HTML/executables rejected. EXIF stripped. URLs: `/media/{{sha256}}/{{name}}`.

## Undo / Changelog

`POST /v1/posts/ID/unpublish` — revert to draft. `GET /changelog` — changes.

## Full reference

    GET /openapi.json  — OpenAPI 3.1 contract
    GET /docs          — interactive Scalar reference
"""


def _default_base_url() -> str:
    """Return the default base URL for static generation (docs, llms.txt)."""
    settings = get_settings()
    return settings.public_base_url or "https://cms.example.com"


LLMS_TXT = _build_llms_txt(_default_base_url())

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
        "Media uploads (requires Authorization header):\n"
        "  POST   /v1/sites/{site}/assets         — presigned upload URL\n"
        "  POST   /v1/sites/{site}/assets/inline  — inline upload (base64, max 2MB)\n"
        "  POST   /v1/assets/{id}/finalize        — validate, sniff, generate variants\n"
        "  GET    /v1/sites/{site}/assets          — list assets\n"
        "  GET    /v1/assets/{id}                 — get asset details\n"
        "  DELETE /v1/assets/{id}                 — soft-delete asset\n"
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
