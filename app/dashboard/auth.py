"""Dashboard auth: cookie sessions, magic links, CSRF tokens.

Session: signed cookie ``_acsess`` containing ``user_id|expires|sig``.
CSRF: token in ``_accsrf`` cookie + ``X-CSRF-Token`` header on mutations.
Magic link: ``/dashboard/magic/<token>`` — generates session on GET.

All secrets derived from the app's ``secret_key`` (same as preview tokens).
No new dependencies required.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass

from app.config import get_settings

SESSION_COOKIE = "_acsess"
SESSION_TTL_SECONDS = 24 * 60 * 60  # 24 hours
CSRF_COOKIE = "_accsrf"
CSRF_TOKEN_HEADER = "X-CSRF-Token"
MAGIC_LINK_TTL_SECONDS = 15 * 60  # 15 minutes

_HMAC_ALGO = hashlib.sha256


def _secret() -> bytes:
    return get_settings().secret_key.encode()


def _sign(message: str) -> str:
    return hmac.new(_secret(), message.encode(), _HMAC_ALGO).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


def create_session_token(user_id: str) -> str:
    """Create a signed session token: ``user_id|expires|sig``."""
    expires = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{user_id}|{expires}"
    sig = _sign(payload)
    return f"{payload}|{sig}"


def verify_session_token(token: str) -> str | None:
    """Verify a session token and return user_id, or None if invalid/expired."""
    parts = token.split("|")
    if len(parts) != 3:
        return None
    user_id, expires_str, sig = parts
    try:
        expires = int(expires_str)
    except ValueError:
        return None
    if time.time() > expires:
        return None
    expected = _sign(f"{user_id}|{expires_str}")
    if not hmac.compare_digest(sig, expected):
        return None
    return user_id


# ---------------------------------------------------------------------------
# CSRF tokens
# ---------------------------------------------------------------------------


def generate_csrf_token() -> str:
    """Generate a cryptographically random CSRF token."""
    return secrets.token_urlsafe(32)


def _sign_csrf(token: str) -> str:
    """Sign a CSRF token for embedding in the page."""
    return _sign(f"csrf:{token}")


def verify_csrf_token(token: str, signed: str) -> bool:
    """Verify a CSRF token matches its signature."""
    return hmac.compare_digest(_sign(f"csrf:{token}"), signed)


# ---------------------------------------------------------------------------
# Magic link tokens
# ---------------------------------------------------------------------------


def create_magic_link_token(user_id: str) -> str:
    """Create a signed magic link token valid for 15 minutes."""
    expires = int(time.time()) + MAGIC_LINK_TTL_SECONDS
    payload = f"magic:{user_id}|{expires}"
    sig = _sign(payload)
    token = f"{payload}|{sig}"
    return secrets.token_urlsafe(1) + token.replace("|", ".")


def verify_magic_link_token(token: str) -> str | None:
    """Verify a magic link token and return user_id, or None if invalid/expired."""
    # Decode: we replaced | with . and prefixed with random char
    if len(token) < 2:
        return None
    decoded = token[1:].replace(".", "|")
    parts = decoded.split("|")
    if len(parts) != 4:
        return None
    _prefix, user_id, expires_str, sig = parts
    try:
        expires = int(expires_str)
    except ValueError:
        return None
    if time.time() > expires:
        return None
    expected = _sign(f"magic:{user_id}|{expires_str}")
    if not hmac.compare_digest(sig, expected):
        return None
    return user_id


# ---------------------------------------------------------------------------
# User lookup (lightweight — just for session auth)
# ---------------------------------------------------------------------------


@dataclass
class DashboardUser:
    """Authenticated dashboard user."""

    id: str
    label: str
    kind: str
    scopes: list[str]


def get_dashboard_user(user_id: str) -> DashboardUser | None:
    """Look up a user by ID. Returns None if not found."""
    from app.db.session import get_engine

    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        return None

    with get_engine().connect() as conn:
        from sqlalchemy import text

        result = conn.execute(
            text("SELECT id, label, kind, scopes FROM actors WHERE id = :id AND revoked_at IS NULL"),
            {"id": uid},
        )
        row = result.first()
        if row is None:
            return None
        scopes = row[3] if row[3] else []
        return DashboardUser(
            id=str(row[0]),
            label=row[1],
            kind=row[2],
            scopes=scopes if isinstance(scopes, list) else [],
        )


def get_or_create_dashboard_user(email: str) -> DashboardUser:
    """Get or create a human user for dashboard access."""
    import uuid as _uuid

    from app.db.session import get_engine

    with get_engine().begin() as conn:
        from sqlalchemy import text

        result = conn.execute(
            text("SELECT id, label, kind FROM actors WHERE label = :label AND kind = 'human'"),
            {"label": email},
        )
        row = result.first()
        if row is not None:
            return DashboardUser(id=str(row[0]), label=row[1], kind=row[2], scopes=[])

        new_id = _uuid.uuid4()
        conn.execute(
            text("INSERT INTO actors (id, kind, label, scopes) VALUES (:id, 'human', :label, '[]'::jsonb)"),
            {"id": new_id, "label": email},
        )
        return DashboardUser(id=str(new_id), label=email, kind="human", scopes=[])
