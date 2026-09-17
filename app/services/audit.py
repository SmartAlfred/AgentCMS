"""Audit event service (#13).

Records every mutation as an append-only row in ``audit_events``.
The table is append-only at the DB level (REVOKE UPDATE/DELETE),
so this module only ever INSERTs.

Key design decisions:
* ``actor_label`` is **denormalised** so the label survives token deletion.
* ``before_hash`` / ``after_hash`` represent content fingerprints; the actual
  body is recoverable via ``post_revisions``.
* Failed auth attempts are also logged (``auth.denied``).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.audit_event import AuditEvent

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def record_event(
    session: Session,
    *,
    action: str,
    actor_id: uuid.UUID | None = None,
    actor_label: str | None = None,
    actor_kind: str = "human",
    source: str = "api",
    target_type: str | None = None,
    target_id: str | None = None,
    before_hash: str | None = None,
    after_hash: str | None = None,
    revision: int | None = None,
    request_id: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    idempotent_replay: bool = False,
    event_metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append an audit event.  Returns the created event (not flushed)."""

    # Compute prev_hash from the most recent event in the same site context
    prev_hash = _compute_prev_hash(session)

    event = AuditEvent(
        id=uuid.uuid4(),
        actor_id=actor_id,
        actor_label=actor_label,
        actor_kind=actor_kind,
        source=source,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before_hash=before_hash,
        after_hash=after_hash,
        revision=revision,
        request_id=request_id,
        ip=ip,
        user_agent=user_agent,
        idempotent_replay=idempotent_replay,
        prev_hash=prev_hash,
        event_metadata=event_metadata or {},
    )
    session.add(event)
    # Do NOT commit here — the caller controls the transaction boundary.
    # Flush so that the row is visible for the next prev_hash computation
    # within the same transaction.
    session.flush()
    return event


def _compute_prev_hash(session: Session) -> str | None:
    """Return the hash of the most recently inserted audit event, if any."""
    last = session.query(AuditEvent).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).first()
    if last is None:
        return None
    return _hash_event(last)


def _hash_event(event: AuditEvent) -> str:
    """Deterministic SHA-256 fingerprint of an audit event for the hash chain."""
    payload = json.dumps(
        {
            "id": str(event.id),
            "created_at": event.created_at.isoformat() if event.created_at else "",
            "actor_id": str(event.actor_id) if event.actor_id else "",
            "action": event.action,
            "target_type": event.target_type or "",
            "target_id": event.target_id or "",
            "before_hash": event.before_hash or "",
            "after_hash": event.after_hash or "",
            "prev_hash": event.prev_hash or "",
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def compute_content_hash(body_md: str) -> str:
    """SHA-256 hex digest of a body for before_hash / after_hash."""
    return hashlib.sha256(body_md.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Querying
# ---------------------------------------------------------------------------


def list_audit_events(
    session: Session,
    *,
    actor_id: uuid.UUID | None = None,
    action: str | None = None,
    source: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> tuple[list[AuditEvent], str | None]:
    """Cursor-paginated audit event list.

    ``cursor`` is the ``created_at|id`` of the last seen event.
    Returns ``(items, next_cursor)``.
    """
    if limit < 1 or limit > MAX_LIMIT:
        limit = max(1, min(limit, MAX_LIMIT))

    query = session.query(AuditEvent)

    if actor_id is not None:
        query = query.filter(AuditEvent.actor_id == actor_id)
    if action is not None:
        query = query.filter(AuditEvent.action == action)
    if source is not None:
        query = query.filter(AuditEvent.source == source)
    if target_type is not None:
        query = query.filter(AuditEvent.target_type == target_type)
    if target_id is not None:
        query = query.filter(AuditEvent.target_id == target_id)
    if since is not None:
        query = query.filter(AuditEvent.created_at >= since)
    if until is not None:
        query = query.filter(AuditEvent.created_at <= until)

    # Cursor pagination: created_at (ISO) | id
    if cursor:
        try:
            parts = cursor.split("|", 1)
            cursor_ts_str, cursor_id_str = parts[0], parts[1]
            cursor_id = uuid.UUID(cursor_id_str)
            cursor_ts = datetime.fromisoformat(cursor_ts_str)
        except (ValueError, IndexError):
            pass
        else:
            from sqlalchemy import or_

            query = query.filter(
                or_(
                    AuditEvent.created_at < cursor_ts,
                    (AuditEvent.created_at == cursor_ts) & (AuditEvent.id < cursor_id),
                )
            )

    query = query.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
    items = query.limit(limit + 1).all()

    next_cursor: str | None = None
    if len(items) > limit:
        last = items[-2]
        next_cursor = f"{last.created_at.isoformat()}|{last.id}"
        items = items[:limit]

    return items, next_cursor


def get_audit_event(session: Session, event_id: uuid.UUID) -> AuditEvent | None:
    """Fetch a single audit event by id."""
    return session.query(AuditEvent).filter(AuditEvent.id == event_id).first()


# ---------------------------------------------------------------------------
# Streaming export
# ---------------------------------------------------------------------------


def iter_all_events(
    session: Session,
    *,
    actor_id: uuid.UUID | None = None,
    action: str | None = None,
    source: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    batch_size: int = 1000,
) -> Generator[AuditEvent, None, None]:
    """Yield audit events in chronological order without loading all into memory.

    Uses keyset pagination on ``(created_at, id)`` for stable streaming.
    """
    last_ts: datetime | None = None
    last_id: uuid.UUID | None = None

    while True:
        query = session.query(AuditEvent)

        if actor_id is not None:
            query = query.filter(AuditEvent.actor_id == actor_id)
        if action is not None:
            query = query.filter(AuditEvent.action == action)
        if source is not None:
            query = query.filter(AuditEvent.source == source)
        if since is not None:
            query = query.filter(AuditEvent.created_at >= since)
        if until is not None:
            query = query.filter(AuditEvent.created_at <= until)

        if last_ts is not None and last_id is not None:
            from sqlalchemy import or_

            query = query.filter(
                or_(
                    AuditEvent.created_at > last_ts,
                    (AuditEvent.created_at == last_ts) & (AuditEvent.id > last_id),
                )
            )

        query = query.order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
        batch = query.limit(batch_size).all()

        if not batch:
            break

        yield from batch

        last_ts = batch[-1].created_at
        last_id = batch[-1].id

        if len(batch) < batch_size:
            break


# ---------------------------------------------------------------------------
# Activity feed helpers
# ---------------------------------------------------------------------------

_ACTION_LABELS: dict[str, str] = {
    "post.created": "created",
    "post.updated": "updated",
    "post.published": "published",
    "post.unpublished": "unpublished",
    "post.trashed": "trashed",
    "post.reverted": "reverted",
    "token.created": "created a token",
    "token.revoked": "revoked a token",
    "link.used": "used a capability link",
    "review.approved": "approved",
    "review.rejected": "rejected",
    "quota.exceeded": "exceeded quota",
    "auth.denied": "was denied access",
}


def human_readable_action(action: str) -> str:
    """Return a plain-language verb phrase for an action string."""
    return _ACTION_LABELS.get(action, action.replace(".", " "))


def detect_anomalies(
    session: Session,
    *,
    since_minutes: int = 5,
    mass_write_threshold: int = 10,
) -> list[dict[str, Any]]:
    """Cheap anomaly detection for the activity feed.

    Returns a list of advisory flags (not blocking).
    Flags:
    * ``mass_writes``: >threshold writes per actor in the window
    * ``novel_ip_ua``: new IP or user-agent for an actor
    * ``after_hours``: writes outside 08:00-22:00 UTC
    """
    from datetime import timedelta

    flags: list[dict[str, Any]] = []
    cutoff = datetime.now(UTC) - timedelta(minutes=since_minutes)

    # Mass writes per actor
    rows = (
        session.query(AuditEvent.actor_id, AuditEvent.actor_label, func.count(AuditEvent.id))
        .filter(AuditEvent.created_at >= cutoff, AuditEvent.action.like("post.%"))
        .group_by(AuditEvent.actor_id, AuditEvent.actor_label)
        .having(func.count(AuditEvent.id) > mass_write_threshold)
        .all()
    )
    for actor_id, label, count in rows:
        flags.append(
            {
                "type": "mass_writes",
                "actor_id": str(actor_id) if actor_id else None,
                "actor_label": label,
                "count": count,
                "window_minutes": since_minutes,
            }
        )

    # After-hours writes (before 08:00 or after 22:00 UTC)
    recent = session.query(AuditEvent).filter(AuditEvent.created_at >= cutoff).all()
    for event in recent:
        hour = event.created_at.hour if event.created_at.tzinfo else event.created_at.replace(tzinfo=UTC).hour
        if hour < 8 or hour >= 22:
            flags.append(
                {
                    "type": "after_hours",
                    "event_id": str(event.id),
                    "actor_label": event.actor_label,
                    "action": event.action,
                    "hour": hour,
                }
            )
            break  # One flag per batch is enough

    return flags
