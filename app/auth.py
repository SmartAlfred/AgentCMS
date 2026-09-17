"""Auth dependency (#5, extended by #6, #14): extract and verify Bearer tokens on /v1/* routes.

Supports two token families:
* ``acms_*`` — standard API tokens (from #5)
* ``cap_*`` — capability tokens for link-only agents (from #6)

Rate limiting (#14):
After successful auth, the dependency checks per-token rate limits
and stores the result on ``request.state.rate_limit_result`` so the
``RateLimitHeadersMiddleware`` can attach ``X-RateLimit-*`` headers.

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
    Raises 429 if rate-limited (#14).
    Raises 423 if writes are paused by a kill switch (#14).
    """
    from app.services.kill_switch import check_write_allowed
    from app.services.rate_limiter import check_token_rate_limit
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

        auth_ctx = AuthContext(
            actor=actor,
            link=link,
            actor_id=actor.id,
            label=actor.label,
            scopes=list(actor.scopes or []),
            site_id=actor.site_id,
        )

        # --- Rate limiting (#14) ---
        bucket = _bucket_for_request(request.method, request.url.path)
        rl_result = check_token_rate_limit(db, actor.id.hex, bucket=bucket)
        request.state.rate_limit_result = rl_result
        if not rl_result.allowed:
            from app.domain.errors import DomainError

            class _RateLimited(DomainError):
                status_code = 429
                code = "rate-limited"
                title = "Rate limit exceeded"

            raise _RateLimited(
                f"Rate limit exceeded for bucket '{bucket}'.",
                hint=(
                    f"Wait {rl_result.retry_after}s then retry the same request; "
                    f"your Idempotency-Key is preserved."
                ),
                headers={
                    "Retry-After": str(rl_result.retry_after or 60),
                    "X-RateLimit-Bucket": bucket,
                },
                extra={
                    "bucket": bucket,
                    "limit": rl_result.limit,
                    "remaining": rl_result.remaining,
                    "reset": rl_result.reset_epoch,
                },
            )

        # --- Kill switch (#14) ---
        if bucket in ("writes", "publish"):
            site_slug = _extract_site_slug(request.url.path, db)
            check_write_allowed(actor_id=actor.id.hex, site_slug=site_slug)

        return auth_ctx

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
    from app.services.kill_switch import check_write_allowed
    from app.services.rate_limiter import check_capability_link_rate_limit
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

    # Rate limit per link (legacy in-memory limiter from capability_tokens)
    check_rate_limit(db, link)

    # --- Rate limiting (#14) — capability link bucket ---
    bucket = _bucket_for_request(request.method, request.url.path)
    if bucket in ("writes", "publish"):
        rl_result = check_capability_link_rate_limit(db, link.id.hex)
        request.state.rate_limit_result = rl_result
        if not rl_result.allowed:
            from app.domain.errors import DomainError

            class _RateLimited(DomainError):
                status_code = 429
                code = "rate-limited"
                title = "Rate limit exceeded"

            raise _RateLimited(
                f"Rate limit exceeded for capability link in bucket '{bucket}'.",
                hint=(
                    f"Wait {rl_result.retry_after}s then retry the same request; "
                    f"your Idempotency-Key is preserved."
                ),
                headers={
                    "Retry-After": str(rl_result.retry_after or 60),
                    "X-RateLimit-Bucket": bucket,
                },
                extra={
                    "bucket": bucket,
                    "limit": rl_result.limit,
                    "remaining": rl_result.remaining,
                    "reset": rl_result.reset_epoch,
                },
            )
    else:
        # Reads bucket for capability links — use token-level read limit
        from app.services.rate_limiter import check_token_rate_limit

        rl_result = check_token_rate_limit(db, actor.id.hex, bucket=bucket)
        request.state.rate_limit_result = rl_result
        if not rl_result.allowed:
            from app.domain.errors import DomainError

            class _RateLimitedRead(DomainError):
                status_code = 429
                code = "rate-limited"
                title = "Rate limit exceeded"

            raise _RateLimitedRead(
                f"Rate limit exceeded for bucket '{bucket}'.",
                hint=(
                    f"Wait {rl_result.retry_after}s then retry the same request; "
                    f"your Idempotency-Key is preserved."
                ),
                headers={
                    "Retry-After": str(rl_result.retry_after or 60),
                    "X-RateLimit-Bucket": bucket,
                },
                extra={
                    "bucket": bucket,
                    "limit": rl_result.limit,
                    "remaining": rl_result.remaining,
                    "reset": rl_result.reset_epoch,
                },
            )

    # --- Kill switch (#14) ---
    if bucket in ("writes", "publish"):
        check_write_allowed(link_id=link.id.hex, site_slug=site_slug)

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


def _bucket_for_request(method: str, path: str) -> str:
    """Determine the rate-limit bucket for a request.

    Returns 'reads', 'writes', or 'publish'.
    """
    method_upper = method.upper()
    if method_upper == "GET":
        return "reads"
    if method_upper == "HEAD" or method_upper == "OPTIONS":
        return "reads"
    # Publish/unpublish are special writes
    if "/publish" in path or "/unpublish" in path:
        return "publish"
    # All other mutating methods are writes
    return "writes"
