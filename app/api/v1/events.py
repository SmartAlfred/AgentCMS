"""Event feed API routes (#21).

Pollable event feed for agents that cannot receive webhooks.
Returns recent events with cursor-based pagination (7-day retention).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.db.session import get_db

router = APIRouter()


@router.get(
    "/events",
    summary="Pollable event feed",
    tags=["events"],
    description=(
        "Returns recent events for poll-only clients. "
        "Use `since` cursor or ISO timestamp to paginate. "
        "Events are retained for 7 days."
    ),
)
def list_events_endpoint(
    request: Request,
    db: Session = Depends(get_db),
    since: str | None = Query(None, description="Cursor or ISO-8601 timestamp"),
    types: str | None = Query(None, description="Comma-separated event types to filter"),
    site: str | None = Query(None, description="Filter by site slug"),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    from app.services.event_feed import list_events

    event_types = [t.strip() for t in types.split(",")] if types else None

    items, next_cursor = list_events(
        db,
        since=since,
        event_types=event_types,
        site_slug=site,
        limit=limit,
    )

    return {
        "items": items,
        "next_cursor": next_cursor,
        "count": len(items),
    }
