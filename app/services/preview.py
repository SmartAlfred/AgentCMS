"""Preview token signing (ticket #8).

Signed, time-limited tokens that allow the dashboard to render drafts
without exposing them to the public.  Token format::

    base64url(post_id_hex|expires_epoch|signature)

Signature is HMAC-SHA256(server_secret, "post_id|expires_epoch").
Tokens expire after 15 minutes and are single-use by design (the dashboard
generates a fresh one for each preview).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid

from app.config import get_settings

PREVIEW_TTL_SECONDS = 15 * 60  # 15 minutes


def _signing_key() -> bytes:
    """Derive a signing key from the server secret."""
    return get_settings().secret_key.encode()


def create_preview_token(post_id: uuid.UUID) -> str:
    """Create a signed preview token for a post, valid for 15 minutes."""
    expires = int(time.time()) + PREVIEW_TTL_SECONDS
    payload = f"{post_id.hex}|{expires}"
    sig = hmac.new(_signing_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    token = f"{payload}|{sig}"
    return base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")


def verify_preview_token(token: str) -> uuid.UUID | None:
    """Verify a preview token and return the post ID, or None if invalid/expired."""
    try:
        # Add back padding
        padded = token + "=" * (4 - len(token) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode()
        parts = decoded.split("|")
        if len(parts) != 3:
            return None
        post_hex, expires_str, sig = parts
        expires = int(expires_str)
    except (ValueError, UnicodeDecodeError):
        return None

    if time.time() > expires:
        return None

    # Verify signature
    payload = f"{post_hex}|{expires_str}"
    expected_sig = hmac.new(_signing_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected_sig):
        return None

    try:
        return uuid.UUID(post_hex)
    except ValueError:
        return None
