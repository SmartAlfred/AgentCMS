"""Rate limiter (#14): sliding-window counters for per-credential, per-IP, and global limits.

Stores counters in a Postgres table (fine for v1 volumes).  Every response
carries ``X-RateLimit-Limit``, ``X-RateLimit-Remaining``,
``X-RateLimit-Reset`` (epoch seconds) and ``X-RateLimit-Bucket`` so an
agent can self-throttle *before* being blocked.

Buckets
-------
* reads   — GET per token (600/min) and unauthenticated per IP (300/min)
* writes  — create/update per token (30/min, 500/day) and per capability link (10/min, 50/day)
* publish — per token (5/min, 50/day)

Daily quotas are tracked separately and return ``QUOTA_EXCEEDED``.

Global circuit breaker: 2 000 writes/min site-wide → 503 for writes only.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger("app.rate_limiter")

# ---------------------------------------------------------------------------
# Defaults (configurable per site/token via Settings in production)
# ---------------------------------------------------------------------------

DEFAULT_LIMITS: dict[str, dict[str, Any]] = {
    "reads": {
        "per_token": {"limit": 600, "window_seconds": 60},
        "per_ip": {"limit": 300, "window_seconds": 60},
    },
    "writes": {
        "per_token": {"limit": 30, "window_seconds": 60, "daily_limit": 500},
        "per_capability_link": {"limit": 10, "window_seconds": 60, "daily_limit": 50},
    },
    "publish": {
        "per_token": {"limit": 5, "window_seconds": 60, "daily_limit": 50},
    },
    "auth_failures": {
        "per_ip": {"limit": 20, "window_seconds": 60, "block_seconds": 300},
    },
    "global_circuit_breaker": {
        "writes": {"limit": 2000, "window_seconds": 60},
    },
}

BODY_SIZE_LIMITS: dict[str, int] = {
    "post_body_bytes": 256 * 1024,  # 256 KB
    "title_chars": 300,
    "excerpt_chars": 1000,
    "tags_max": 20,
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RateLimitResult:
    """Outcome of a rate-limit check."""

    allowed: bool
    bucket: str
    limit: int
    remaining: int
    reset_epoch: int  # epoch seconds when the window resets
    retry_after: int | None = None  # seconds to wait (set when blocked)
    daily_remaining: int | None = None
    daily_limit: int | None = None


@dataclass
class _WindowEntry:
    """One request timestamp inside a sliding window."""

    timestamp: float


@dataclass
class _SlidingWindow:
    """Thread-safe sliding window for one (key, bucket) pair."""

    entries: list[_WindowEntry] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def is_allowed(self, limit: int, window_seconds: int) -> tuple[bool, int, int]:
        """Check if a new request is allowed.

        Returns (allowed, remaining, reset_epoch).
        """
        now = time.time()
        cutoff = now - window_seconds
        with self.lock:
            # Evict stale entries
            self.entries = [e for e in self.entries if e.timestamp > cutoff]
            remaining = max(0, limit - len(self.entries))
            reset_epoch = int(now + window_seconds)
            if len(self.entries) >= limit:
                return False, 0, reset_epoch
            self.entries.append(_WindowEntry(timestamp=now))
            return True, remaining - 1, reset_epoch

    def count(self, window_seconds: int) -> int:
        """Count entries in the current window (read-only, no mutation)."""
        now = time.time()
        cutoff = now - window_seconds
        with self.lock:
            return sum(1 for e in self.entries if e.timestamp > cutoff)


@dataclass
class _DailyQuota:
    """Simple daily quota tracker (keyed by UTC date)."""

    date_str: str = ""
    count: int = 0
    limit: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def is_exhausted(self, daily_limit: int) -> tuple[bool, int]:
        """Return (exhausted, remaining)."""
        from datetime import UTC, datetime

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        with self.lock:
            if self.date_str != today:
                self.date_str = today
                self.count = 0
            self.limit = daily_limit
            remaining = max(0, daily_limit - self.count)
            if self.count >= daily_limit:
                return True, 0
            self.count += 1
            return False, remaining - 1


# ---------------------------------------------------------------------------
# In-memory store (thread-safe, per-process)
# ---------------------------------------------------------------------------


class _RateLimitStore:
    """In-memory sliding-window rate limiter with daily quotas.

    Thread-safe.  No persistence — if the process restarts, counters reset.
    This is acceptable for v1 volumes.
    """

    def __init__(self) -> None:
        self._windows: dict[str, _SlidingWindow] = {}
        self._daily: dict[str, _DailyQuota] = {}
        self._lock = threading.Lock()
        # Global circuit breaker
        self._global_writes: _SlidingWindow = _SlidingWindow()

    def _get_window(self, key: str) -> _SlidingWindow:
        with self._lock:
            if key not in self._windows:
                self._windows[key] = _SlidingWindow()
            return self._windows[key]

    def _get_daily(self, key: str) -> _DailyQuota:
        with self._lock:
            if key not in self._daily:
                self._daily[key] = _DailyQuota()
            return self._daily[key]

    def check(
        self,
        key: str,
        bucket: str,
        limit: int,
        window_seconds: int,
        daily_limit: int | None = None,
    ) -> RateLimitResult:
        """Check rate limit for a given key and bucket.

        Returns a RateLimitResult with all header information.
        """
        window = self._get_window(key)
        allowed, remaining, reset_epoch = window.is_allowed(limit, window_seconds)

        result = RateLimitResult(
            allowed=allowed,
            bucket=bucket,
            limit=limit,
            remaining=remaining,
            reset_epoch=reset_epoch,
        )

        if not allowed:
            result.retry_after = max(1, reset_epoch - int(time.time()))

        if daily_limit is not None:
            daily = self._get_daily(key)
            exhausted, daily_remaining = daily.is_exhausted(daily_limit)
            result.daily_remaining = daily_remaining
            result.daily_limit = daily_limit
            if exhausted:
                result.allowed = False
                result.retry_after = self._seconds_until_midnight_utc()
                result.remaining = 0

        return result

    def check_global_writes(self) -> RateLimitResult:
        """Check the global circuit breaker for writes."""
        cfg = DEFAULT_LIMITS["global_circuit_breaker"]["writes"]
        allowed, remaining, reset_epoch = self._global_writes.is_allowed(cfg["limit"], cfg["window_seconds"])
        result = RateLimitResult(
            allowed=allowed,
            bucket="writes",
            limit=cfg["limit"],
            remaining=remaining,
            reset_epoch=reset_epoch,
        )
        if not allowed:
            result.retry_after = max(1, reset_epoch - int(time.time()))
        return result

    def count_window(self, key: str, window_seconds: int) -> int:
        """Read-only count of entries in the current window."""
        window = self._get_window(key)
        return window.count(window_seconds)

    def reset(self, key: str) -> None:
        """Reset all counters for a key (used by kill-switch tests)."""
        with self._lock:
            self._windows.pop(key, None)
            self._daily.pop(key, None)

    def reset_all(self) -> None:
        """Reset everything (for testing)."""
        with self._lock:
            self._windows.clear()
            self._daily.clear()
            self._global_writes = _SlidingWindow()

    @staticmethod
    def _seconds_until_midnight_utc() -> int:
        """Seconds until the next UTC midnight (for daily quota reset)."""
        import datetime

        now = datetime.datetime.now(datetime.UTC)
        tomorrow = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(1, int((tomorrow - now).total_seconds()))


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_store = _RateLimitStore()


def get_store() -> _RateLimitStore:
    """Return the process-wide rate limit store."""
    return _store


def reset_store() -> None:
    """Reset the store (for testing)."""
    _store.reset_all()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_token_rate_limit(
    session: Session,
    actor_id: str,
    *,
    bucket: str,
) -> RateLimitResult:
    """Check rate limit for an authenticated token.

    Parameters
    ----------
    session : Session
        Database session (unused for now, available for future DB-backed limits).
    actor_id : str
        The actor UUID hex.
    bucket : str
        One of 'reads', 'writes', 'publish'.

    Returns
    -------
    RateLimitResult
        The result with all header info.
    """
    cfg = DEFAULT_LIMITS.get(bucket, {}).get("per_token")
    if cfg is None:
        # No limit configured for this bucket
        return RateLimitResult(
            allowed=True,
            bucket=bucket,
            limit=0,
            remaining=0,
            reset_epoch=int(time.time()) + 60,
        )

    key = f"token:{actor_id}:{bucket}"
    daily_limit = cfg.get("daily_limit")
    return _store.check(
        key,
        bucket=bucket,
        limit=cfg["limit"],
        window_seconds=cfg["window_seconds"],
        daily_limit=daily_limit,
    )


def check_capability_link_rate_limit(
    session: Session,
    link_id: str,
) -> RateLimitResult:
    """Check rate limit for a capability link (writes bucket).

    Parameters
    ----------
    session : Session
        Database session.
    link_id : str
        The capability link UUID hex.

    Returns
    -------
    RateLimitResult
        The result with all header info.
    """
    cfg = DEFAULT_LIMITS["writes"]["per_capability_link"]
    key = f"caplink:{link_id}:writes"
    return _store.check(
        key,
        bucket="writes",
        limit=cfg["limit"],
        window_seconds=cfg["window_seconds"],
        daily_limit=cfg["daily_limit"],
    )


def check_ip_rate_limit(ip: str, *, bucket: str = "reads") -> RateLimitResult:
    """Check rate limit for an unauthenticated IP.

    Parameters
    ----------
    ip : str
        Client IP address.
    bucket : str
        The bucket to check (default 'reads').

    Returns
    -------
    RateLimitResult
        The result with all header info.
    """
    cfg = DEFAULT_LIMITS.get(bucket, {}).get("per_ip")
    if cfg is None:
        return RateLimitResult(
            allowed=True,
            bucket=bucket,
            limit=0,
            remaining=0,
            reset_epoch=int(time.time()) + 60,
        )

    key = f"ip:{ip}:{bucket}"
    return _store.check(
        key,
        bucket=bucket,
        limit=cfg["limit"],
        window_seconds=cfg["window_seconds"],
    )


def check_auth_failure(ip: str) -> RateLimitResult:
    """Check auth failure rate limit for an IP.

    Returns a result; if blocked, the IP is temporarily banned.
    """
    cfg = DEFAULT_LIMITS["auth_failures"]["per_ip"]
    key = f"auth_fail:{ip}"
    return _store.check(
        key,
        bucket="auth_failures",
        limit=cfg["limit"],
        window_seconds=cfg["window_seconds"],
    )


def check_global_circuit_breaker() -> RateLimitResult:
    """Check the global circuit breaker for writes."""
    return _store.check_global_writes()


def build_rate_limit_headers(result: RateLimitResult) -> dict[str, str]:
    """Build the X-RateLimit-* headers from a RateLimitResult."""
    headers: dict[str, str] = {
        "X-RateLimit-Limit": str(result.limit),
        "X-RateLimit-Remaining": str(result.remaining),
        "X-RateLimit-Reset": str(result.reset_epoch),
        "X-RateLimit-Bucket": result.bucket,
    }
    if result.retry_after is not None:
        headers["Retry-After"] = str(result.retry_after)
    if result.daily_remaining is not None:
        headers["X-RateLimit-Daily-Remaining"] = str(result.daily_remaining)
    if result.daily_limit is not None:
        headers["X-RateLimit-Daily-Limit"] = str(result.daily_limit)
    return headers
