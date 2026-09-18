"""Event feed service (#21).

Provides the pollable event feed for agents that cannot receive webhooks.
Events are retained for 7 days and returned with cursor-based pagination.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.domain.errors import InvalidCursorError
from app.models.event_outbox import EventOutbox
from app.services.webhook import EVENT_RETENTION_DAYS

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def list_events(
    session: Session,
    *,
    since: str | None = None,
    event_types: list[str] | None = None,
    site_slug: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[dict[str, Any]], str | None]:
    """List recent events from the outbox.

    ``since`` is either an ISO-8601 timestamp or a cursor from a previous response.
    Returns ``(items, next_cursor)``.
    """
    if limit < 1 or limit > MAX_LIMIT:
        limit = max(1, min(limit, MAX_LIMIT))

    cutoff = datetime.now(UTC) - timedelta(days=EVENT_RETENTION_DAYS)
    cutoff_naive = cutoff.replace(tzinfo=None)

    query = session.query(EventOutbox).filter(
        EventOutbox.created_at >= cutoff_naive,
    )

    # Parse since parameter (cursor or ISO timestamp)
    if since:
        try:
            # Try cursor format first: created_at|id
            parts = since.split("|", 1)
            if len(parts) == 2:
                cursor_ts_str, cursor_id_str = parts
                cursor_id = uuid.UUID(cursor_id_str)
                cursor_ts = datetime.fromisoformat(cursor_ts_str)
                from sqlalchemy import or_

                query = query.filter(
                    or_(
                        EventOutbox.created_at < cursor_ts,
                        (EventOutbox.created_at == cursor_ts) & (EventOutbox.id < cursor_id),
                    )
                )
            else:
                # Try plain ISO timestamp
                since_dt = datetime.fromisoformat(since)
                if since_dt.tzinfo is None:
                    since_dt = since_dt.replace(tzinfo=UTC)
                query = query.filter(EventOutbox.created_at > since_dt.replace(tzinfo=None))
        except (ValueError, IndexError) as exc:
            raise InvalidCursorError(f"Invalid 'since' parameter: {since!r}") from exc

    if event_types:
        query = query.filter(EventOutbox.event_type.in_(event_types))

    if site_slug:
        query = query.filter(EventOutbox.site_slug == site_slug)

    query = query.order_by(EventOutbox.created_at.desc(), EventOutbox.id.desc())
    items = query.limit(limit + 1).all()

    next_cursor: str | None = None
    if len(items) > limit:
        last = items[limit - 1]
        next_cursor = f"{last.created_at.isoformat()}|{last.id}"
        items = items[:limit]

    result = [_event_to_dict(e) for e in items]
    return result, next_cursor


def _event_to_dict(event: EventOutbox) -> dict[str, Any]:
    """Convert an EventOutbox row to a dict for API response."""
    return {
        "id": str(event.id),
        "type": event.event_type,
        "created_at": event.created_at.isoformat() if event.created_at else None,
        "site": event.site_slug,
        "actor": {
            "id": str(event.actor_id) if event.actor_id else None,
            "label": event.actor_label,
            "kind": event.actor_kind,
        },
        "data": event.payload or {},
        "request_id": event.request_id or "",
    }
