"""Human- and agent-readable API docs (#2, superseded for agents by #9).

``GET /docs`` renders Scalar, ``GET /redoc`` renders ReDoc — both read the same
``GET /openapi.json`` that the agent-facing instruction sheet (#9) and the
capability-link landing pages (#6) will be generated from, so the contract is
defined exactly once: in the code.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.openapi.docs import get_redoc_html
from starlette.responses import HTMLResponse

router = APIRouter(tags=["docs"], include_in_schema=False)

SCALAR_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{title} — API reference</title>
    <style>
      html, body {{ margin: 0; padding: 0; background: #08090D; color: #F4F6FA;
        font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
      #fallback {{ padding: 2rem; }}
      #fallback a {{ color: #A794FF; }}
    </style>
  </head>
  <body>
    <div id="fallback">
      <p>{title} API reference</p>
      <p>If the viewer below does not load (no CDN access), read the contract directly:
        <a href="{openapi_url}">{openapi_url}</a> — or the plain-text instruction sheet
        at <a href="/llms.txt">/llms.txt</a>.</p>
    </div>
    <script id="api-reference" data-url="{openapi_url}"></script>
    <script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
  </body>
</html>
"""


@router.get("/docs", summary="Interactive API reference (Scalar)", include_in_schema=False)
def scalar_docs() -> HTMLResponse:
    return HTMLResponse(
        SCALAR_HTML.format(title="AgentCMS", openapi_url="/openapi.json"),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/redoc", summary="Interactive API reference (ReDoc)", include_in_schema=False)
def redoc() -> HTMLResponse:
    return get_redoc_html(
        openapi_url="/openapi.json",
        title="AgentCMS API reference",
    )
