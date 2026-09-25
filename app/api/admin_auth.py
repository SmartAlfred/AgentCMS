"""Router-level authentication for every ``/v1/admin/*`` route (#44).

Before this guard the whole admin surface mounted with only ``require_auth``:
``scope_for_endpoint()`` returns ``None`` for ``/v1/admin/`` paths, so *any*
valid token -- even a site-scoped read-only one -- was accepted, and
``POST /v1/admin/tokens`` with no ``Authorization`` header at all was served
without any authentication.  Anyone able to reach the instance could mint a
scoped admin token.  This module closes both.

Accepted credentials, in order:

1. ``X-Admin-Token: <ADMIN_TOKEN>`` -- the bootstrap secret
   (``[prod-required]``, >= 32 characters, constant-time compared) that an
   operator, ``scripts/seed.py`` or ``scripts/deploy_smoke.sh`` uses to mint
   the very first token.
2. A valid dashboard session cookie (``app/dashboard/auth.py``), so the
   server-rendered dashboard and the CSV export links it renders keep working.
   Cookie-authenticated *mutations* additionally require the dashboard CSRF
   token: a bare cookie on POST/DELETE is a CSRF target.
3. A bearer ``acms_`` token carrying the operator wildcard scope ``*:read``.
   A resource-scoped token (``posts:write``, ``sites:read``, ...) is *not*
   enough -- that is the hole this guard closes.
4. A loopback peer, or ``APP_ENV=test`` (mirrors ``GET /metrics``).

``X-Forwarded-For`` is deliberately **not** trusted here, unlike ``/metrics``:
it is client-supplied, so honouring it would let any remote caller send
``X-Forwarded-For: 127.0.0.1`` and walk straight through the guard.  Only the
literal peer address counts.

Usage::

    router.include_router(admin_router, dependencies=[Depends(require_admin)])
"""

from __future__ import annotations

import hmac
import ipaddress
from dataclasses import dataclass

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.auth import AuthContext, require_auth
from app.config import get_settings
from app.dashboard.auth import (
    CSRF_COOKIE,
    CSRF_TOKEN_HEADER,
    SESSION_COOKIE,
    verify_csrf_token,
    verify_session_token,
)
from app.db.session import get_db
from app.domain.errors import DomainError
from app.services.tokens import AuthenticationError, AuthorizationError

#: Header carrying the bootstrap secret.
ADMIN_TOKEN_HEADER = "X-Admin-Token"
#: Minimum length of ``ADMIN_TOKEN`` (enforced at boot in production).
ADMIN_TOKEN_MIN_LENGTH = 32
#: The operator scope a bearer token needs to touch the admin surface.
ADMIN_SCOPE = "*:read"
#: Methods that do not change state (a dashboard cookie is enough for these).
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_MISSING_CREDENTIALS_DETAIL = (
    "The /v1/admin/* surface requires admin credentials: send "
    f"`{ADMIN_TOKEN_HEADER}: <ADMIN_TOKEN>`, present a dashboard session cookie, "
    f"or use a bearer token with the `{ADMIN_SCOPE}` scope."
)
_MISSING_CREDENTIALS_HINT = (
    "ADMIN_TOKEN is generated into .env by scripts/selfhost.sh; see "
    "docs/deploy/configuration.md. Rotate it any time -- it is a bootstrap "
    "secret, not a stored token."
)


class AdminCsrfInvalidError(DomainError):
    """A cookie-authenticated mutation arrived without a valid CSRF token."""

    status_code = 403
    code = "csrf-invalid"
    title = "CSRF validation failed"
    hint = (
        "Cookie-authenticated mutations must send the `X-CSRF-Token` header that "
        "matches the `_accsrf` cookie."
    )


@dataclass(frozen=True)
class AdminContext:
    """Which credential opened the admin surface."""

    source: str
    actor_id: str | None = None


def _peer_is_loopback(request: Request) -> bool:
    """True only when the *literal* peer address is a loopback address.

    ``request.client`` is the TCP peer.  A name (``testclient``), a hostname or
    a missing client is never treated as loopback, and ``X-Forwarded-For`` is
    ignored on purpose -- it is attacker-controlled.
    """
    client = request.client
    host = getattr(client, "host", None)
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped is not None and mapped.is_loopback)


def _admin_token_matches(supplied: str | None, expected: str) -> bool:
    """Constant-time comparison of the bootstrap secret."""
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied, expected)


def _require_csrf(request: Request) -> None:
    """Reject a cookie-authenticated mutation without a valid CSRF token."""
    header_token = request.headers.get(CSRF_TOKEN_HEADER, "")
    cookie_token = request.cookies.get(CSRF_COOKIE, "")
    if not header_token or not cookie_token:
        raise AdminCsrfInvalidError(
            f"Missing CSRF token: send the `{CSRF_TOKEN_HEADER}` header alongside the session cookie."
        )
    parts = cookie_token.split("|")
    if len(parts) != 2:
        raise AdminCsrfInvalidError("Invalid CSRF cookie.")
    raw, signature = parts
    if not hmac.compare_digest(raw, header_token) or not verify_csrf_token(raw, signature):
        raise AdminCsrfInvalidError("Invalid CSRF token.")


async def require_admin(
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> AdminContext:
    """Authenticate a request to the ``/v1/admin/*`` surface (fail closed)."""
    settings = getattr(request.app.state, "settings", None) or get_settings()

    # Test environments have no secrets to send, exactly like /metrics.
    if settings.is_test:
        return AdminContext(source="test")

    if _peer_is_loopback(request):
        return AdminContext(source="loopback")

    if _admin_token_matches(x_admin_token, settings.admin_token):
        return AdminContext(source="bootstrap-token")

    session_cookie = request.cookies.get(SESSION_COOKIE, "")
    if session_cookie:
        user_id = verify_session_token(session_cookie)
        if user_id is not None:
            if request.method.upper() not in SAFE_METHODS:
                _require_csrf(request)
            return AdminContext(source="dashboard-session", actor_id=user_id)

    if authorization:
        # Reuse the standard bearer path: it already handles revocation,
        # expiry, rate limits and the write kill switch.  On top of it the
        # admin surface insists on the operator wildcard scope, which
        # `scope_for_endpoint()` deliberately does not check for /v1/admin/*.
        context: AuthContext = await require_auth(request, authorization, db)
        if ADMIN_SCOPE not in set(context.scopes or ()):
            raise AuthorizationError(
                f"The /v1/admin/* surface requires the `{ADMIN_SCOPE}` operator scope; "
                f"this token carries {sorted(context.scopes or [])}.",
                required_scope=ADMIN_SCOPE,
            )
        return AdminContext(source="api-token", actor_id=str(context.actor_id))

    raise AuthenticationError(
        _MISSING_CREDENTIALS_DETAIL,
        hint=_MISSING_CREDENTIALS_HINT,
        headers={"WWW-Authenticate": "Bearer"},
    )
