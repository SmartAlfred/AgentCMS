"""Token utilities (#5): hash, format, redact, pepper, and scope helpers.

Token format: ``acms_<actor_id_hex>_<secret>``
  - the id prefix makes lookup O(1) without scanning hashes
  - the token is greppable in logs for redaction

Stored as ``sha256(secret + pepper)`` — the pepper is a server-side secret
so rainbow tables are useless even if the DB leaks.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.domain.errors import DomainError
from app.models.actor import Actor
from app.models.capability_link import CapabilityLink

TOKEN_PREFIX = "acms_"
_SECRET_BYTES = 32  # 256-bit random secret

# Scopes the system understands.
ALL_SCOPES = frozenset(
    {
        "posts:read",
        "posts:write",
        "posts:publish",
        "assets:write",
        "sites:read",
        "*:read",
    }
)
DEFAULT_SCOPES: list[str] = ["posts:read", "posts:write"]
DEFAULT_EXPIRY_DAYS = 90


# ---------------------------------------------------------------------------
# Token hashing / generation
# ---------------------------------------------------------------------------


def _pepper() -> str:
    """Return the server-side pepper (from secret_key)."""
    return get_settings().secret_key


def hash_secret(secret: str) -> str:
    """SHA-256 hash of the raw secret + pepper."""
    return hashlib.sha256((secret + _pepper()).encode()).hexdigest()


def generate_token(actor_id: uuid.UUID) -> tuple[str, str]:
    """Generate a new plaintext token and its hash.

    Returns (plaintext, hash).
    """
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    plaintext = f"{TOKEN_PREFIX}{actor_id.hex}_{secret}"
    token_hash = hash_secret(secret)
    return plaintext, token_hash


def split_token(plaintext: str) -> tuple[str, str] | None:
    """Split ``acms_<actor_id>_<secret>`` into (actor_id_hex, secret).

    Returns None if the token does not match the expected format.
    """
    if not plaintext.startswith(TOKEN_PREFIX):
        return None
    remainder = plaintext[len(TOKEN_PREFIX) :]
    parts = remainder.split("_", 1)
    if len(parts) != 2:
        return None
    actor_id_hex, secret = parts
    # Validate hex
    try:
        int(actor_id_hex, 16)
    except ValueError:
        return None
    if len(actor_id_hex) != 32:  # UUID hex is 32 chars
        return None
    return actor_id_hex, secret


def redact_token(plaintext: str) -> str:
    """Redact a token for safe logging: ``acms_abc…***``."""
    if not plaintext.startswith(TOKEN_PREFIX):
        return "***"
    remainder = plaintext[len(TOKEN_PREFIX) :]
    parts = remainder.split("_", 1)
    if len(parts) != 2:
        return "***"
    actor_hex = parts[0]
    prefix = actor_hex[:6] if len(actor_hex) >= 6 else actor_hex
    return f"{TOKEN_PREFIX}{prefix}…***"


# ---------------------------------------------------------------------------
# Auth errors
# ---------------------------------------------------------------------------


class AuthenticationError(DomainError):
    """401 — missing, unknown, expired, or revoked token."""

    status_code = 401
    code = "unauthenticated"
    title = "Unauthenticated"


class AuthorizationError(DomainError):
    """403 — token is valid but lacks the required scope or site binding."""

    status_code = 403
    code = "forbidden"
    title = "Forbidden"

    def __init__(
        self,
        detail: str,
        *,
        required_scope: str | None = None,
    ) -> None:
        extra: dict[str, Any] = {}
        if required_scope:
            extra["required_scope"] = required_scope
        super().__init__(
            detail,
            hint=(
                f"This action requires the `{required_scope}` scope. "
                "Mint a new token with the required scope via POST /v1/admin/tokens."
                if required_scope
                else "The token or link you used does not carry the scope this call needs."
            ),
            extra=extra,
        )


# ---------------------------------------------------------------------------
# Token lookup & verification
# ---------------------------------------------------------------------------


def verify_token(
    session: Session,
    plaintext: str,
    *,
    required_scope: str | None = None,
    required_site_id: uuid.UUID | None = None,
    check_revoked: bool = True,
) -> tuple[Actor, CapabilityLink]:
    """Verify a token and return (actor, capability_link).

    Raises AuthenticationError for 401 cases and AuthorizationError for 403.
    """
    parts = split_token(plaintext)
    if parts is None:
        raise AuthenticationError("Malformed token.")

    actor_id_hex, secret = parts
    try:
        uuid.UUID(actor_id_hex)
    except ValueError as exc:
        raise AuthenticationError("Malformed token.") from exc

    token_hash = hash_secret(secret)

    # Look up the capability link by hash — O(1) via unique index
    link = session.query(CapabilityLink).filter(CapabilityLink.token_hash == token_hash).first()
    if link is None:
        raise AuthenticationError("Unknown token.")

    # Load the actor
    actor = session.query(Actor).filter(Actor.id == link.actor_id).first()
    if actor is None:
        raise AuthenticationError("Token actor not found.")

    # Check revocation (immediate — no grace period)
    if check_revoked:
        if actor.revoked_at is not None:
            raise AuthenticationError("This token has been revoked.")
        if link.revoked_at is not None:
            raise AuthenticationError("This token has been revoked.")

    # Check expiration
    now = datetime.now(UTC)
    if link.expires_at is not None:
        expires = link.expires_at if link.expires_at.tzinfo else link.expires_at.replace(tzinfo=UTC)
        if expires < now:
            raise AuthenticationError(
                "This token has expired.",
                hint=(
                    "Mint a new token with POST /v1/admin/tokens — tokens expire after 90 days by default."
                ),
            )
    if actor.expires_at is not None:
        expires = actor.expires_at if actor.expires_at.tzinfo else actor.expires_at.replace(tzinfo=UTC)
        if expires < now:
            raise AuthenticationError("This token has expired.")

    # Check scope
    if required_scope is not None:
        actor_scopes = set(actor.scopes or [])
        if required_scope not in actor_scopes and "*:read" not in actor_scopes:
            raise AuthorizationError(
                f"The token lacks the `{required_scope}` scope.",
                required_scope=required_scope,
            )

    # Check site binding
    if required_site_id is not None and actor.site_id is not None and actor.site_id != required_site_id:
        raise AuthorizationError(
            "The token is not authorized for this site.",
        )

    # Update usage stats (fire-and-forget style, committed by caller)
    actor.last_used_at = now
    actor.uses_count = (actor.uses_count or 0) + 1
    session.flush()

    return actor, link


# ---------------------------------------------------------------------------
# Scope helpers for endpoints
# ---------------------------------------------------------------------------

_METHOD_SCOPE_MAP: dict[str, dict[str, str]] = {
    "GET": {
        "posts": "posts:read",
        "sites": "sites:read",
        "assets": "posts:read",
        "tags": "posts:read",
        "search": "posts:read",
    },
    "POST": {"posts": "posts:write", "tags": "posts:write", "assets": "assets:write"},
    "PATCH": {"posts": "posts:write"},
    "DELETE": {"posts": "posts:write", "assets": "posts:write"},
}


def scope_for_endpoint(method: str, path: str) -> str | None:
    """Return the required scope for a given HTTP method + path, or None for public."""
    # Public routes — no auth required
    if path in ("/healthz", "/readyz", "/docs", "/redoc", "/openapi.json", "/"):
        return None
    if path == "/v1/info":
        return None
    # Admin routes need special handling
    if path.startswith("/v1/admin/"):
        return None  # handled by admin auth separately
    # Capability link routes — handled by capability auth
    if path.startswith("/c/"):
        return None

    method_upper = method.upper()

    # Publish/unpublish need posts:publish
    if "/publish" in path or "/unpublish" in path:
        return "posts:publish"

    # Determine resource from the *last* meaningful segment in the path.
    # For /v1/sites/{slug}/posts the resource is "posts"; for /v1/posts/{id} it's "posts".
    parts = [p for p in path.strip("/").split("/") if p and p != "v1"]
    resource = "posts"
    for part in reversed(parts):
        # Skip path parameters (UUIDs or slugs that look like IDs)
        if part in ("posts", "sites", "assets", "tags", "search"):
            resource = part
            break

    method_scopes = _METHOD_SCOPE_MAP.get(method_upper, {})
    return method_scopes.get(resource, "posts:write")


def require_scope(session: Session, actor: Actor, scope: str) -> None:
    """Raise AuthorizationError if the actor lacks the given scope."""
    actor_scopes = set(actor.scopes or [])
    if scope not in actor_scopes and "*:read" not in actor_scopes:
        raise AuthorizationError(
            f"The token lacks the `{scope}` scope.",
            required_scope=scope,
        )
