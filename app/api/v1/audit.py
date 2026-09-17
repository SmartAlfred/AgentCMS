"""Admin audit log API (#13).

Routes:
* ``GET  /v1/admin/audit``              — query audit events (dashboard/human auth only)
* ``GET  /v1/admin/audit/export``       — streaming export (CSV or JSONL)
* ``GET  /v1/admin/audit/feed``         — activity feed with plain-language sentences
* ``GET  /v1/admin/audit/feed/anomalies`` — anomaly flags

Query parameters:
  actor, action, source, target_type, target_id, since, until, cursor, limit

Export parameters:
  format=csv|jsonl
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.audit import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    detect_anomalies,
    human_readable_action,
    iter_all_events,
    list_audit_events,
)

router = APIRouter(prefix="/admin/audit", tags=["admin", "audit"])


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class AuditEventRead(BaseModel):
    """Single audit event in API responses."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    actor_id: str | None = None
    actor_label: str | None = None
    actor_kind: str | None = None
    source: str
    action: str
    target_type: str | None = None
    target_id: str | None = None
    before_hash: str | None = None
    after_hash: str | None = None
    revision: int | None = None
    request_id: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    idempotent_replay: bool = False
    prev_hash: str | None = None
    event_metadata: dict[str, Any] | None = None


class AuditListResponse(BaseModel):
    """Cursor-paginated audit event list."""

    model_config = ConfigDict(from_attributes=True)

    items: list[AuditEventRead]
    next_cursor: str | None = None
    count: int


class ActivityFeedItem(BaseModel):
    """Plain-language activity feed entry."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    actor_label: str | None = None
    actor_kind: str | None = None
    action: str
    action_label: str
    target_type: str | None = None
    target_id: str | None = None
    source: str
    sentence: str


class AnomalyFlag(BaseModel):
    """Advisory anomaly flag."""

    model_config = ConfigDict(from_attributes=True)

    type: str
    detail: dict[str, Any]


class AnomalyDigestResponse(BaseModel):
    """Response for the anomaly endpoint."""

    flags: list[AnomalyFlag]
    window_minutes: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event_to_read(event: Any) -> AuditEventRead:
    return AuditEventRead(
        id=str(event.id),
        created_at=event.created_at,
        actor_id=str(event.actor_id) if event.actor_id else None,
        actor_label=event.actor_label,
        actor_kind=event.actor_kind,
        source=event.source,
        action=event.action,
        target_type=event.target_type,
        target_id=event.target_id,
        before_hash=event.before_hash,
        after_hash=event.after_hash,
        revision=event.revision,
        request_id=event.request_id,
        ip=event.ip,
        user_agent=event.user_agent,
        idempotent_replay=event.idempotent_replay,
        prev_hash=event.prev_hash,
        event_metadata=event.event_metadata,
    )


def _build_sentence(event: Any) -> str:
    """Build a plain-language sentence from an audit event."""
    actor = event.actor_label or "Unknown"
    verb = human_readable_action(event.action)

    if event.target_type and event.target_id:
        return f"**{actor}** {verb} *{event.target_type}* {event.target_id}"
    return f"**{actor}** {verb}"


# ---------------------------------------------------------------------------
# GET /v1/admin/audit — list
# ---------------------------------------------------------------------------


@router.get(
    "",
    summary="List audit events",
    response_model=AuditListResponse,
)
def list_audit_events_endpoint(
    request: Request,
    db: Session = Depends(get_db),
    actor: str | None = Query(None, description="Filter by actor UUID"),
    action: str | None = Query(None, description="Filter by action type"),
    source: str | None = Query(None, description="Filter by source"),
    target_type: str | None = Query(None, description="Filter by target type"),
    target_id: str | None = Query(None, description="Filter by target ID"),
    since: str | None = Query(None, description="ISO datetime lower bound"),
    until: str | None = Query(None, description="ISO datetime upper bound"),
    cursor: str | None = Query(None, description="Pagination cursor"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> AuditListResponse:
    actor_id = uuid.UUID(actor) if actor else None
    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    items, next_cursor = list_audit_events(
        db,
        actor_id=actor_id,
        action=action,
        source=source,
        target_type=target_type,
        target_id=target_id,
        since=since_dt,
        until=until_dt,
        limit=limit,
        cursor=cursor,
    )

    return AuditListResponse(
        items=[_event_to_read(e) for e in items],
        next_cursor=next_cursor,
        count=len(items),
    )


# ---------------------------------------------------------------------------
# GET /v1/admin/audit/export — streaming export
# ---------------------------------------------------------------------------


@router.get(
    "/export",
    summary="Export audit events (streaming)",
)
def export_audit_events_endpoint(
    request: Request,
    db: Session = Depends(get_db),
    format: str = Query("jsonl", description="Export format: csv or jsonl"),
    actor: str | None = Query(None),
    action: str | None = Query(None),
    source: str | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
) -> StreamingResponse:
    actor_id = uuid.UUID(actor) if actor else None
    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    if format == "csv":

        def generate_csv() -> Iterator[bytes]:
            header = (
                "id,created_at,actor_id,actor_label,actor_kind,source,"
                "action,target_type,target_id,before_hash,after_hash,"
                "revision,request_id,ip,user_agent,idempotent_replay,"
                "prev_hash\n"
            )
            yield header.encode()

            buf = io.StringIO()
            writer = csv.writer(buf)
            for event in iter_all_events(
                db,
                actor_id=actor_id,
                action=action,
                source=source,
                since=since_dt,
                until=until_dt,
            ):
                writer.writerow(
                    [
                        str(event.id),
                        event.created_at.isoformat() if event.created_at else "",
                        str(event.actor_id) if event.actor_id else "",
                        event.actor_label or "",
                        event.actor_kind or "",
                        event.source,
                        event.action,
                        event.target_type or "",
                        event.target_id or "",
                        event.before_hash or "",
                        event.after_hash or "",
                        event.revision or "",
                        event.request_id or "",
                        event.ip or "",
                        (event.user_agent or "")[:200],
                        event.idempotent_replay,
                        event.prev_hash or "",
                    ]
                )
                yield buf.getvalue().encode()
                buf.seek(0)
                buf.truncate(0)

        return StreamingResponse(
            generate_csv(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=audit_events.csv"},
        )

    # Default: JSONL

    def generate_jsonl() -> Iterator[bytes]:
        for event in iter_all_events(
            db,
            actor_id=actor_id,
            action=action,
            source=source,
            since=since_dt,
            until=until_dt,
        ):
            row = {
                "id": str(event.id),
                "created_at": event.created_at.isoformat() if event.created_at else "",
                "actor_id": str(event.actor_id) if event.actor_id else None,
                "actor_label": event.actor_label,
                "actor_kind": event.actor_kind,
                "source": event.source,
                "action": event.action,
                "target_type": event.target_type,
                "target_id": event.target_id,
                "before_hash": event.before_hash,
                "after_hash": event.after_hash,
                "revision": event.revision,
                "request_id": event.request_id,
                "ip": event.ip,
                "user_agent": event.user_agent,
                "idempotent_replay": event.idempotent_replay,
                "prev_hash": event.prev_hash,
            }
            yield (json.dumps(row, default=str) + "\n").encode()

    return StreamingResponse(
        generate_jsonl(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=audit_events.jsonl"},
    )


# ---------------------------------------------------------------------------
# GET /v1/admin/audit/feed — activity feed
# ---------------------------------------------------------------------------


@router.get(
    "/feed",
    summary="Agent activity feed",
    response_model=list[ActivityFeedItem],
)
def activity_feed_endpoint(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
) -> list[ActivityFeedItem]:
    items, _ = list_audit_events(db, limit=limit)
    return [
        ActivityFeedItem(
            id=str(e.id),
            created_at=e.created_at,
            actor_label=e.actor_label,
            actor_kind=e.actor_kind,
            action=e.action,
            action_label=human_readable_action(e.action),
            target_type=e.target_type,
            target_id=e.target_id,
            source=e.source,
            sentence=_build_sentence(e),
        )
        for e in items
    ]


# ---------------------------------------------------------------------------
# GET /v1/admin/audit/feed/anomalies — anomaly flags
# ---------------------------------------------------------------------------


@router.get(
    "/feed/anomalies",
    summary="Anomaly digest",
    response_model=AnomalyDigestResponse,
)
def anomalies_endpoint(
    request: Request,
    db: Session = Depends(get_db),
    since_minutes: int = Query(5, ge=1, le=1440),
    mass_write_threshold: int = Query(10, ge=2, le=100),
) -> AnomalyDigestResponse:
    flags = detect_anomalies(
        db,
        since_minutes=since_minutes,
        mass_write_threshold=mass_write_threshold,
    )
    return AnomalyDigestResponse(
        flags=[AnomalyFlag(type=f["type"], detail=f) for f in flags],
        window_minutes=since_minutes,
    )
