"""Idempotency key service (#11).

Manages storage, retrieval and replay of idempotent requests.

Design:
* Keys are namespaced per ``(actor_id, key)`` so two tokens with ``key=1``
  don't collide.
* A request fingerprint (SHA-256 of method + path + normalised body) detects
  replay of the same request vs. a different body under the same key.
* Same key + same fingerprint → replay the stored response verbatim, flagged
  with ``Idempotent-Replay: true``.
* Same key + different body → ``409 IDEMPOTENCY_KEY_REUSED``.
* In-flight guard: ``409 REQUEST_IN_PROGRESS`` + ``Retry-After: 1``.
* 24-hour TTL; expired rows are reclaimable by a pruner job.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.domain.errors import IdempotencyKeyReusedError, RequestInProgressError
from app.models.idempotency_key import IdempotencyKey


def _compute_fingerprint(method: str, path: str, body: bytes | None) -> str:
    """Deterministic fingerprint for a request."""
    h = hashlib.sha256()
    h.update(method.upper().encode())
    h.update(b"\x00")
    h.update(path.encode())
    h.update(b"\x00")
    if body:
        h.update(body)
    return h.hexdigest()


def _parse_response_headers(raw: dict[str, str] | None) -> dict[str, str]:
    """Ensure response headers are a flat str→str dict."""
    if not raw:
        return {}
    return {k: str(v) for k, v in raw.items()}


class IdempotencyResult:
    """Result of checking an idempotency key."""

    __slots__ = ("is_replay", "original_request_id", "record")

    def __init__(
        self,
        *,
        is_replay: bool,
        record: IdempotencyKey | None = None,
        original_request_id: str | None = None,
    ) -> None:
        self.is_replay = is_replay
        self.record = record
        self.original_request_id = original_request_id


def check_idempotency(
    session: Session,
    *,
    actor_id: uuid.UUID | None,
    key: str,
    method: str,
    path: str,
    body: bytes | None,
) -> IdempotencyResult:
    """Check if this idempotency key already has a stored or in-flight request.

    Raises ``IdempotencyKeyReusedError`` if the key exists with a different
    fingerprint.  Raises ``RequestInProgressError`` if a request is currently
    in flight with that key.

    Returns an ``IdempotencyResult`` with ``is_replay=True`` and the stored
    record if this is an exact replay.
    """
    settings = get_settings()
    retention = timedelta(hours=settings.idempotency_retention_hours)
    fingerprint = _compute_fingerprint(method, path, body)

    now = datetime.now(UTC)

    # Find existing record (non-expired)
    record = (
        session.query(IdempotencyKey)
        .filter(
            IdempotencyKey.actor_id == actor_id,
            IdempotencyKey.key == key,
            IdempotencyKey.expires_at > now,
        )
        .first()
    )

    if record is not None:
        # Check in-flight guard
        if record.in_flight:
            raise RequestInProgressError()

        # Check fingerprint match
        if record.request_fingerprint == fingerprint:
            return IdempotencyResult(
                is_replay=True,
                record=record,
                original_request_id=record.original_request_id,
            )

        # Different body → conflict
        raise IdempotencyKeyReusedError()

    # No existing record → create one in in-flight state
    new_record = IdempotencyKey(
        id=uuid.uuid4(),
        actor_id=actor_id,
        key=key,
        request_fingerprint=fingerprint,
        in_flight=True,
        expires_at=now + retention,
        created_at=now,
    )
    session.add(new_record)
    session.flush()

    return IdempotencyResult(is_replay=False, record=new_record)


def complete_idempotency(
    session: Session,
    record: IdempotencyKey,
    *,
    request_id: str,
    status_code: int,
    body: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> None:
    """Mark an idempotency record as complete with the stored response."""
    record.in_flight = False
    record.response_status = status_code
    record.response_body = body
    record.response_headers = _parse_response_headers(headers)
    record.original_request_id = request_id
    session.flush()


def get_stored_response(record: IdempotencyKey) -> tuple[int, dict[str, Any], dict[str, str]]:
    """Return (status_code, body, headers) from a stored idempotency record."""
    return (
        record.response_status or 200,
        record.response_body or {},
        _parse_response_headers(record.response_headers),
    )


def prune_expired(session: Session) -> int:
    """Delete expired idempotency key records.  Returns the count deleted."""
    now = datetime.now(UTC)
    deleted = (
        session.query(IdempotencyKey)
        .filter(IdempotencyKey.expires_at <= now)
        .delete(synchronize_session="fetch")
    )
    session.flush()
    return deleted
