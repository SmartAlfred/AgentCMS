"""Optimistic concurrency helpers (#11).

ETag generation, If-Match / If-None-Match validation for post resources.

Design:
* ``ETag`` is derived from ``content_hash`` + ``revision`` — cheap to compute
  and immutable once written.
* ``If-Match`` on ``PATCH`` / ``DELETE`` / ``publish`` / ``unpublish`` ensures
  the agent is not acting on a stale read.
* ``If-None-Match`` on ``GET`` enables cheap polling.
* ``If-Match: *`` means "whatever is current" — allowed, but emits an audit
  note that no concurrency check happened.
"""

from __future__ import annotations

from app.domain.errors import PreconditionFailedError


def compute_etag(content_hash: str | None, revision: int) -> str:
    """Compute a strong ETag from the post's content_hash and revision."""
    raw = f"{content_hash or 'none'}:{revision}"
    import hashlib

    return f'"{hashlib.sha256(raw.encode()).hexdigest()[:32]}"'


def check_if_match(
    if_match: str | None,
    content_hash: str | None,
    revision: int,
    *,
    audit_note: bool = False,
) -> tuple[bool, str | None]:
    """Validate an If-Match header against the current post state.

    Returns ``(passed, audit_note_or_none)``.
    Raises ``PreconditionFailedError`` if the ETag doesn't match.

    Parameters
    ----------
    if_match:
        The raw ``If-Match`` header value.
    content_hash:
        The post's current ``content_hash``.
    revision:
        The post's current ``revision``.
    audit_note:
        If True, and if_match is ``*``, an audit note is returned.
    """
    if if_match is None:
        # No If-Match header — no precondition check
        return False, None

    current_etag = compute_etag(content_hash, revision)

    if if_match.strip() == "*":
        # Wildcard — always passes, but audit note
        return False, "No concurrency check performed (If-Match: *)."

    if if_match == current_etag:
        return True, None

    raise PreconditionFailedError(current_etag=current_etag, current_revision=revision)


def check_if_none_match(if_none_match: str | None, content_hash: str | None, revision: int) -> bool:
    """Validate an If-None-Match header.

    Returns ``True`` if the response should be 304 Not Modified.
    """
    if if_none_match is None:
        return False

    current_etag = compute_etag(content_hash, revision)

    if if_none_match.strip() == "*":
        # Any resource exists → 304
        return True

    return if_none_match == current_etag
