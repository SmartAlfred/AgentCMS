"""Auth dependency (#5, extended by #6): extract and verify Bearer tokens on /v1/* routes.

Supports two token families:
* ``acms_*`` — standard API tokens (from #5)
* ``cap_*`` — capability tokens for link-only agents (from #6)

Usage in endpoints::

    from app.auth import AuthContext, require_auth

    @router.get("/posts/{id}")
    def get_post(auth: AuthContext = Depends(require_auth), ...):
        ...
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.actor import Actor
from app.models.capability_link import CapabilityLink
from app.services.tokens import (
    AuthenticationError,
    verify_token,
)

logger = logging.getLogger("app.auth")

_TOKEN_RE = re.compile(r"^Bearer\s+(acms_\S+)$", re.IGNORECASE)
_CAP_TOKEN_RE = re.compile(r"^Bearer\s+(cap_\S+)$", re.IGNORECASE)


@dataclass(frozen=True)
class AuthContext:
    """Verified authentication context attached to the request."""

    actor: Actor
    link: CapabilityLink
    actor_id: uuid.UUID
    label: str
    scopes: list[str]
    site_id: uuid.UUID | None


def _extract_bearer(authorization: str | None) -> str | None:
    """Pull the raw token from an ``Authorization: Bearer <token>`` header."""
    if not authorization:
        return None
    match = _TOKEN_RE.match(authorization)
    return match.group(1) if match else None


def _extract_cap_bearer(authorization: str | None) -> str | None:
    """Pull a capability token from an ``Authorization: Bearer cap_…`` header."""
    if not authorization:
        return None
    match = _CAP_TOKEN_RE.match(authorization)
    return match.group(1) if match else None


async def require_auth(
    request: Request,
    authorization: str | None = Header(None),
    db: Session = Depends(get_db),
) -> AuthContext:
    """FastAPI dependency: verify the Bearer token and return an AuthContext.

    Raises 401 if the token is missing, malformed, unknown, expired or revoked.
    Raises 403 if the token lacks the required scope for this endpoint.
    """
    from app.services.tokens import scope_for_endpoint

    # Try standard acms_ token first
    plaintext = _extract_bearer(authorization)
    if plaintext is not None:
        required_scope = scope_for_endpoint(request.method, request.url.path)
        required_site_id = _extract_site_id(request.url.path, db)

        actor, link = verify_token(
            db,
            plaintext,
            required_scope=required_scope,
            required_site_id=required_site_id,
            check_revoked=True,
        )

        logger.info(
            "authenticated %s (%s) on %s %s",
            actor.label,
            actor.id,
            request.method,
            request.url.path,
        )

        return AuthContext(
            actor=actor,
            link=link,
            actor_id=actor.id,
            label=actor.label,
            scopes=list(actor.scopes or []),
            site_id=actor.site_id,
        )

    # Try capability token (cap_)
    cap_plaintext = _extract_cap_bearer(authorization)
    if cap_plaintext is not None:
        return _verify_capability_auth(cap_plaintext, request, db)

    raise AuthenticationError(
        "Missing or malformed Authorization header.",
        hint="Present a scoped API token as `Authorization: Bearer <token>`, or use "
        "the capability link you were given (see /c/{token}).",
    )


def _verify_capability_auth(
    plaintext: str,
    request: Request,
    db: Session,
) -> AuthContext:
    """Verify a capability token and return an AuthContext."""
    from app.services.capability_tokens import (
        check_rate_limit,
        verify_capability_token,
    )
    from app.services.tokens import scope_for_endpoint

    required_scope = scope_for_endpoint(request.method, request.url.path)

    # Map scope to capability verb
    verb_map = {
        "posts:read": "posts:read",
        "posts:write": "posts:write",
        "posts:publish": "posts:publish",
    }
    required_verb = verb_map.get(required_scope) if required_scope else None

    # Extract site slug from the URL path
    site_slug = _extract_site_slug(request.url.path, db)

    actor, link = verify_capability_token(
        db,
        plaintext,
        required_verb=required_verb,
        required_site_slug=site_slug,
    )

    # Rate limit per link
    check_rate_limit(db, link)

    logger.info(
        "authenticated capability link %s (%s) on %s %s",
        link.label,
        link.id,
        request.method,
        request.url.path,
    )

    return AuthContext(
        actor=actor,
        link=link,
        actor_id=actor.id,
        label=link.label or actor.label,
        scopes=list(actor.scopes or []),
        site_id=actor.site_id,
    )


def _extract_site_id(path: str, db: Session) -> uuid.UUID | None:
    """Extract a site UUID from the path if it contains /v1/sites/{slug}."""
    import re

    match = re.search(r"/v1/sites/([^/]+)", path)
    if not match:
        return None
    from app.models.site import Site

    slug = match.group(1)
    site = db.query(Site).filter(Site.slug == slug).first()
    return site.id if site else None


def _extract_site_slug(path: str, db: Session) -> str | None:
    """Extract a site slug from the path if it contains /v1/sites/{slug}."""
    match = re.search(r"/v1/sites/([^/]+)", path)
    if match:
        return match.group(1)
    return None
