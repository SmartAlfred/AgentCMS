"""Auth dependency (#5): extract and verify Bearer tokens on /v1/* routes.

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

    plaintext = _extract_bearer(authorization)
    if plaintext is None:
        raise AuthenticationError(
            "Missing or malformed Authorization header.",
            hint="Present a scoped API token as `Authorization: Bearer <token>`.",
        )

    required_scope = scope_for_endpoint(request.method, request.url.path)

    # Extract site_id from the URL path if present (e.g. /v1/sites/{slug}/posts)
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
