"""Capability token service (#6): HMAC-signed tokens for link-only agents.

Token format: ``cap_<site>_<random>``

The token is self-describing — the site slug is embedded in the plaintext.
Verification: hash the random part + pepper, look up the CapabilityLink by hash.
The link record stores site_slug, verbs, expires_at, and uses_remaining.

Rate limiting: per-link, tracked in-memory with a sliding window.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.config import get_settings
from app.domain.errors import DomainError
from app.models.actor import Actor
from app.models.capability_link import CapabilityLink

TOKEN_PREFIX = "cap_"
_SECRET_BYTES = 32
_HMAC_ALGO = hashlib.sha256

# All verbs a capability link can grant.
ALL_VERBS = frozenset({"posts:read", "posts:write", "posts:publish"})


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


class CapabilityTokenError(DomainError):
    """401 — capability token is missing, unknown, expired, or revoked."""

    status_code = 401
    code = "unauthenticated"
    title = "Unauthenticated"


class CapabilityTokenExpired(DomainError):
    """410 — capability token has expired."""

    status_code = 410
    code = "gone"
    title = "Link expired"


class CapabilityTokenRevoked(DomainError):
    """401 — capability token has been revoked."""

    status_code = 401
    code = "unauthenticated"
    title = "Unauthenticated"


class CapabilityTokenRateLimited(DomainError):
    """429 — rate limit exceeded for this capability link."""

    status_code = 429
    code = "rate-limited"
    title = "Rate limit exceeded"


class CapabilityLinkForbidden(DomainError):
    """403 — capability token lacks required verb."""

    status_code = 403
    code = "forbidden"
    title = "Forbidden"


# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------


def _pepper() -> str:
    return get_settings().secret_key


def generate_capability_token(site_slug: str) -> tuple[str, str]:
    """Generate a new capability token and its hash.

    Returns (plaintext, hash).  The hash is what goes into the DB.
    """
    random_part = secrets.token_urlsafe(_SECRET_BYTES)
    plaintext = f"{TOKEN_PREFIX}{site_slug}_{random_part}"
    token_hash = _hash_secret(random_part)
    return plaintext, token_hash


def _hash_secret(random_part: str) -> str:
    return hashlib.sha256((random_part + _pepper()).encode()).hexdigest()


def _hmac_sign(message: str) -> str:
    """HMAC-SHA256 of a message using the server secret."""
    return hmac.new(_pepper().encode(), message.encode(), _HMAC_ALGO).hexdigest()[:16]


def _parse_token(plaintext: str) -> tuple[str, str] | None:
    """Parse ``cap_<site>_<random>`` into (site_slug, random_part).

    Returns None if the token does not match the expected format.
    """
    if not plaintext.startswith(TOKEN_PREFIX):
        return None
    remainder = plaintext[len(TOKEN_PREFIX) :]
    parts = remainder.split("_", 1)
    if len(parts) != 2:
        return None
    site_slug, random_part = parts
    if not site_slug or not random_part:
        return None
    return site_slug, random_part


def redact_capability_token(plaintext: str) -> str:
    """Redact a capability token for safe logging: ``cap_<site>_…***``."""
    parsed = _parse_token(plaintext)
    if parsed is None:
        return "***"
    site_slug, _ = parsed
    return f"cap_{site_slug}_…***"


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------


def verify_capability_token(
    session: Session,
    plaintext: str,
    *,
    required_verb: str | None = None,
    required_site_slug: str | None = None,
) -> tuple[Actor, CapabilityLink]:
    """Verify a capability token and return (actor, link).

    Raises CapabilityTokenError (401) for bad/missing/unknown tokens,
    CapabilityTokenExpired (410) for expired tokens,
    CapabilityTokenRevoked (401) for revoked tokens.
    """
    parsed = _parse_token(plaintext)
    if parsed is None:
        raise CapabilityTokenError("Malformed capability token.")

    _site_slug, random_part = parsed
    token_hash = _hash_secret(random_part)

    # Look up by hash
    link = session.query(CapabilityLink).filter(CapabilityLink.token_hash == token_hash).first()
    if link is None:
        raise CapabilityTokenError("Unknown capability token.")

    # Check revocation
    if link.revoked_at is not None:
        raise CapabilityTokenRevoked(
            "This link was revoked. Ask the site owner for a new one.",
            hint="This link was revoked. Ask the site owner for a new one.",
        )

    # Load the actor
    actor = session.query(Actor).filter(Actor.id == link.actor_id).first()
    if actor is None:
        raise CapabilityTokenError("Token actor not found.")

    if actor.revoked_at is not None:
        raise CapabilityTokenRevoked(
            "This link was revoked. Ask the site owner for a new one.",
            hint="This link was revoked. Ask the site owner for a new one.",
        )

    # Check expiration
    now = datetime.now(UTC)
    if link.expires_at is not None:
        expires = link.expires_at if link.expires_at.tzinfo else link.expires_at.replace(tzinfo=UTC)
        if expires < now:
            raise CapabilityTokenExpired(
                "This link has expired.",
                hint="Ask the site owner for a new link.",
            )

    # Check site binding (token's site must match the link's site_slug)
    link_site_slug = getattr(link, "site_slug", None)
    if link_site_slug and required_site_slug and link_site_slug != required_site_slug:
        raise CapabilityTokenError("This link is not authorized for this site.")

    # Check verb authorization
    if required_verb is not None:
        link_verbs = set(link.verbs or [])
        if required_verb not in link_verbs:
            raise CapabilityLinkForbidden(
                f"This link lacks the `{required_verb}` permission.",
                hint=(
                    f"This link lacks the `{required_verb}` permission. "
                    f"Ask the site owner for a link that grants `{required_verb}`."
                ),
            )

    # Check uses_remaining
    if link.uses_remaining is not None and link.uses_remaining <= 0:
        raise CapabilityTokenError("This link has no remaining uses.")

    # Decrement uses_remaining if set
    if link.uses_remaining is not None:
        link.uses_remaining -= 1

    # Update usage stats
    link.last_used_at = now
    link.uses_count = (link.uses_count or 0) + 1
    actor.last_used_at = now
    actor.uses_count = (actor.uses_count or 0) + 1
    session.flush()

    return actor, link


# ---------------------------------------------------------------------------
# Rate limiting (per-link, in-memory sliding window)
# ---------------------------------------------------------------------------


class _RateLimiter:
    """Per-link sliding window rate limiter."""

    def __init__(self) -> None:
        self._windows: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def is_rate_limited(self, link_id: str, limit: int, window_seconds: int) -> bool:
        """Return True if the link has exceeded the rate limit."""
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            timestamps = self._windows[link_id]
            # Remove old entries
            self._windows[link_id] = [t for t in timestamps if t > cutoff]
            if len(self._windows[link_id]) >= limit:
                return True
            self._windows[link_id].append(now)
            return False

    def reset(self, link_id: str) -> None:
        with self._lock:
            self._windows.pop(link_id, None)


_rate_limiter = _RateLimiter()


def check_rate_limit(session: Session, link: CapabilityLink) -> None:
    """Check per-link rate limit. Raises CapabilityTokenRateLimited if exceeded."""
    settings = get_settings()
    limit = settings.capability_rate_limit_per_link
    window = settings.capability_rate_limit_window_seconds
    link_id = str(link.id)

    if _rate_limiter.is_rate_limited(link_id, limit, window):
        raise CapabilityTokenRateLimited(
            f"Rate limit exceeded: {limit} requests per {window} seconds.",
            hint=f"Wait a moment and retry. Rate limit: {limit} writes per {window} seconds.",
            headers={"Retry-After": str(window)},
        )
