"""Agent-facing surfaces (#9): /llms.txt, / (content-negotiated), /v1/discover, /changelog.

These routes are the product promise: one URL is enough for an agent to
learn how to publish a post.  All content is generated from
:mod:`app.docs_content` — the single source of truth.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from starlette.responses import HTMLResponse, JSONResponse

from app.docs_content import (
    LLMS_TXT,
    build_changelog,
    build_discover_json,
    build_instruction_sheet,
    build_landing_page_html,
)

router = APIRouter(tags=["agent-docs"])


# ---------------------------------------------------------------------------
# GET /llms.txt — machine-readable instruction sheet
# ---------------------------------------------------------------------------


@router.get(
    "/llms.txt",
    summary="Machine-readable instruction sheet (llms.txt convention)",
    include_in_schema=False,
)
def llms_txt(request: Request) -> Response:
    """Return the llms.txt instruction sheet.

    Under ~4 KB so a model can read the entire thing in one pass.
    """
    return Response(
        content=LLMS_TXT,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


# ---------------------------------------------------------------------------
# GET / — content-negotiated root
# ---------------------------------------------------------------------------


@router.get(
    "/",
    summary="Entry point — content-negotiated (text/plain, application/json, text/html)",
    include_in_schema=False,
)
def root(request: Request) -> Response:
    """Content-negotiated entry point.

    * ``Accept: text/plain`` or ``application/json`` → compact instruction sheet
    * ``Accept: text/html`` (default for browsers) → human landing page
    """
    accept = request.headers.get("accept", "")
    base_url = str(request.base_url).rstrip("/")

    if "text/html" in accept:
        html = build_landing_page_html(base_url)
        return HTMLResponse(content=html)

    # text/plain or application/json — return the instruction sheet as JSON
    sheet = build_instruction_sheet(base_url)
    return JSONResponse(
        content={"instructions": sheet, "llms_txt": "/llms.txt", "openapi": "/openapi.json"},
    )


# ---------------------------------------------------------------------------
# GET /v1/discover — orientation JSON
# ---------------------------------------------------------------------------


@router.get(
    "/v1/discover",
    summary="Orientation JSON for agents already holding a token",
    response_description="Service metadata, auth modes, and key URLs.",
)
def discover(request: Request) -> dict[str, Any]:
    """Tiny JSON so an agent can orient itself in one call."""
    base_url = str(request.base_url).rstrip("/")
    return build_discover_json(base_url)


# ---------------------------------------------------------------------------
# GET /changelog — append-only API change log
# ---------------------------------------------------------------------------


@router.get(
    "/changelog",
    summary="Append-only API change log",
    response_description="List of changes with dates and summaries.",
)
def changelog() -> list[dict[str, str]]:
    """Append-only API change log with breaking changes named explicitly."""
    return build_changelog()
