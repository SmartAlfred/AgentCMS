"""Admin endpoints for rate limits and kill switches (#14).

* ``GET  /v1/admin/rate-limits/status`` — view current rate limit state
* ``POST /v1/admin/kill-switches/global/pause`` — pause all agent writes
* ``POST /v1/admin/kill-switches/global/resume`` — resume all agent writes
* ``POST /v1/admin/kill-switches/sites/{slug}/pause`` — pause writes for a site
* ``POST /v1/admin/kill-switches/sites/{slug}/resume`` — resume writes for a site
* ``POST /v1/admin/kill-switches/tokens/{id}/pause`` — pause writes for a token
* ``POST /v1/admin/kill-switches/tokens/{id}/resume`` — resume writes for a token
* ``POST /v1/admin/kill-switches/links/{id}/pause`` — pause writes for a link
* ``POST /v1/admin/kill-switches/links/{id}/resume`` — resume writes for a link
* ``GET  /v1/admin/kill-switches/status`` — view all active kill switches
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.services.kill_switch import get_kill_switch_store
from app.services.rate_limiter import BODY_SIZE_LIMITS, DEFAULT_LIMITS

router = APIRouter(prefix="/v1/admin", tags=["admin"], include_in_schema=False)


# ---------------------------------------------------------------------------
# Rate limit status
# ---------------------------------------------------------------------------


@router.get("/rate-limits/status", summary="View rate limit configuration and state")
def rate_limit_status(request: Request) -> dict[str, Any]:
    """Return the current rate limit configuration and active counters."""
    return {
        "limits": DEFAULT_LIMITS,
        "body_size_limits": BODY_SIZE_LIMITS,
        "active_windows": {
            "note": "In-memory counters — reset on process restart.",
        },
    }


# ---------------------------------------------------------------------------
# Kill switch endpoints
# ---------------------------------------------------------------------------


@router.post("/kill-switches/global/pause", summary="Pause all agent writes site-wide")
def pause_global(request: Request) -> dict[str, str]:
    store = get_kill_switch_store()
    store.pause_global()
    return {"status": "paused", "scope": "global", "message": "All agent writes are paused."}


@router.post("/kill-switches/global/resume", summary="Resume all agent writes site-wide")
def resume_global(request: Request) -> dict[str, str]:
    store = get_kill_switch_store()
    store.resume_global()
    return {"status": "resumed", "scope": "global", "message": "All agent writes are resumed."}


@router.post(
    "/kill-switches/sites/{site_slug}/pause",
    summary="Pause writes for a site",
)
def pause_site(site_slug: str, request: Request) -> dict[str, str]:
    store = get_kill_switch_store()
    store.pause_site(site_slug)
    return {
        "status": "paused",
        "scope": f"site:{site_slug}",
        "message": f"Agent writes for site '{site_slug}' are paused.",
    }


@router.post(
    "/kill-switches/sites/{site_slug}/resume",
    summary="Resume writes for a site",
)
def resume_site(site_slug: str, request: Request) -> dict[str, str]:
    store = get_kill_switch_store()
    store.resume_site(site_slug)
    return {
        "status": "resumed",
        "scope": f"site:{site_slug}",
        "message": f"Agent writes for site '{site_slug}' are resumed.",
    }


@router.post(
    "/kill-switches/tokens/{actor_id}/pause",
    summary="Pause writes for a token",
)
def pause_token(actor_id: str, request: Request) -> dict[str, str]:
    store = get_kill_switch_store()
    store.pause_token(actor_id)
    return {
        "status": "paused",
        "scope": f"token:{actor_id}",
        "message": f"Agent writes for token '{actor_id}' are paused.",
    }


@router.post(
    "/kill-switches/tokens/{actor_id}/resume",
    summary="Resume writes for a token",
)
def resume_token(actor_id: str, request: Request) -> dict[str, str]:
    store = get_kill_switch_store()
    store.resume_token(actor_id)
    return {
        "status": "resumed",
        "scope": f"token:{actor_id}",
        "message": f"Agent writes for token '{actor_id}' are resumed.",
    }


@router.post(
    "/kill-switches/links/{link_id}/pause",
    summary="Pause writes for a capability link",
)
def pause_capability_link(link_id: str, request: Request) -> dict[str, str]:
    store = get_kill_switch_store()
    store.pause_capability_link(link_id)
    return {
        "status": "paused",
        "scope": f"capability_link:{link_id}",
        "message": f"Agent writes for capability link '{link_id}' are paused.",
    }


@router.post(
    "/kill-switches/links/{link_id}/resume",
    summary="Resume writes for a capability link",
)
def resume_capability_link(link_id: str, request: Request) -> dict[str, str]:
    store = get_kill_switch_store()
    store.resume_capability_link(link_id)
    return {
        "status": "resumed",
        "scope": f"capability_link:{link_id}",
        "message": f"Agent writes for capability link '{link_id}' are resumed.",
    }


@router.get("/kill-switches/status", summary="View all active kill switches")
def kill_switch_status(request: Request) -> dict[str, Any]:
    """Return all active kill switches."""
    store = get_kill_switch_store()
    state = store.get_state()
    return {
        "active": state.to_dict(),
        "message": "Only paused switches are shown. Empty dicts mean nothing is paused.",
    }
