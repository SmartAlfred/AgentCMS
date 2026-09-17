"""Kill switches (#14): per-token, per-capability-link, per-site, and global.

Effective < 1 s: while paused, writes return 423 ``AGENT_WRITES_PAUSED``
with a hint; reads keep working so the blog stays up.

Switches are in-memory (fast to flip, no DB round-trip).  A dashboard
would call the admin endpoints to toggle them.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from app.domain.errors import DomainError

logger = logging.getLogger("app.kill_switch")


# ---------------------------------------------------------------------------
# Domain error
# ---------------------------------------------------------------------------


class AgentWritesPausedError(DomainError):
    """423 — agent writes are paused by a kill switch."""

    status_code = 423
    code = "agent-writes-paused"
    title = "Agent writes paused"


# ---------------------------------------------------------------------------
# Kill switch store
# ---------------------------------------------------------------------------


@dataclass
class KillSwitchState:
    """Snapshot of all active kill switches."""

    global_paused: bool = False
    site_paused: dict[str, bool] = field(default_factory=dict)
    token_paused: dict[str, bool] = field(default_factory=dict)
    capability_link_paused: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "global": self.global_paused,
            "sites": dict(self.site_paused),
            "tokens": dict(self.token_paused),
            "capability_links": dict(self.capability_link_paused),
        }


class _KillSwitchStore:
    """In-memory kill switch store.

    Thread-safe.  All toggles are effective < 1 s (next request).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._global: bool = False
        self._sites: dict[str, bool] = {}
        self._tokens: dict[str, bool] = {}
        self._cap_links: dict[str, bool] = {}
        # Stats: when each switch was last toggled
        self._last_toggled: dict[str, float] = {}

    # -- Global ---------------------------------------------------------------

    def pause_global(self) -> None:
        with self._lock:
            self._global = True
            self._last_toggled["global"] = time.time()
        logger.warning("kill switch: global writes PAUSED")

    def resume_global(self) -> None:
        with self._lock:
            self._global = False
            self._last_toggled["global"] = time.time()
        logger.info("kill switch: global writes RESUMED")

    def is_global_paused(self) -> bool:
        with self._lock:
            return self._global

    # -- Per-site -------------------------------------------------------------

    def pause_site(self, site_slug: str) -> None:
        with self._lock:
            self._sites[site_slug] = True
            self._last_toggled[f"site:{site_slug}"] = time.time()
        logger.warning("kill switch: site '%s' writes PAUSED", site_slug)

    def resume_site(self, site_slug: str) -> None:
        with self._lock:
            self._sites[site_slug] = False
            self._last_toggled[f"site:{site_slug}"] = time.time()
        logger.info("kill switch: site '%s' writes RESUMED", site_slug)

    def is_site_paused(self, site_slug: str) -> bool:
        with self._lock:
            return self._sites.get(site_slug, False)

    # -- Per-token ------------------------------------------------------------

    def pause_token(self, actor_id: str) -> None:
        with self._lock:
            self._tokens[actor_id] = True
            self._last_toggled[f"token:{actor_id}"] = time.time()
        logger.warning("kill switch: token '%s' writes PAUSED", actor_id)

    def resume_token(self, actor_id: str) -> None:
        with self._lock:
            self._tokens[actor_id] = False
            self._last_toggled[f"token:{actor_id}"] = time.time()
        logger.info("kill switch: token '%s' writes RESUMED", actor_id)

    def is_token_paused(self, actor_id: str) -> bool:
        with self._lock:
            return self._tokens.get(actor_id, False)

    # -- Per-capability-link --------------------------------------------------

    def pause_capability_link(self, link_id: str) -> None:
        with self._lock:
            self._cap_links[link_id] = True
            self._last_toggled[f"caplink:{link_id}"] = time.time()
        logger.warning("kill switch: capability link '%s' writes PAUSED", link_id)

    def resume_capability_link(self, link_id: str) -> None:
        with self._lock:
            self._cap_links[link_id] = False
            self._last_toggled[f"caplink:{link_id}"] = time.time()
        logger.info("kill switch: capability link '%s' writes RESUMED", link_id)

    def is_capability_link_paused(self, link_id: str) -> bool:
        with self._lock:
            return self._cap_links.get(link_id, False)

    # -- Combined check -------------------------------------------------------

    def is_write_paused(
        self,
        *,
        actor_id: str | None = None,
        link_id: str | None = None,
        site_slug: str | None = None,
    ) -> bool:
        """Check if writes are paused at any level.

        Returns True if any applicable kill switch is active.
        """
        with self._lock:
            if self._global:
                return True
            if site_slug and self._sites.get(site_slug, False):
                return True
            if actor_id and self._tokens.get(actor_id, False):
                return True
            if link_id and self._cap_links.get(link_id, False):
                return True
        return False

    # -- State snapshot -------------------------------------------------------

    def get_state(self) -> KillSwitchState:
        """Return a snapshot of all kill switch states."""
        with self._lock:
            return KillSwitchState(
                global_paused=self._global,
                site_paused={k: v for k, v in self._sites.items() if v},
                token_paused={k: v for k, v in self._tokens.items() if v},
                capability_link_paused={k: v for k, v in self._cap_links.items() if v},
            )

    # -- Reset (testing) ------------------------------------------------------

    def reset_all(self) -> None:
        """Reset all kill switches (for testing)."""
        with self._lock:
            self._global = False
            self._sites.clear()
            self._tokens.clear()
            self._cap_links.clear()
            self._last_toggled.clear()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_store = _KillSwitchStore()


def get_kill_switch_store() -> _KillSwitchStore:
    """Return the process-wide kill switch store."""
    return _store


def reset_kill_switches() -> None:
    """Reset all kill switches (for testing)."""
    _store.reset_all()


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------


def check_write_allowed(
    *,
    actor_id: str | None = None,
    link_id: str | None = None,
    site_slug: str | None = None,
) -> None:
    """Raise AgentWritesPausedError if writes are paused.

    Call this at the top of any write endpoint.
    """
    if _store.is_write_paused(actor_id=actor_id, link_id=link_id, site_slug=site_slug):
        raise AgentWritesPausedError(
            "Agent writes are currently paused by an administrator.",
            hint=(
                "Writes are paused via a kill switch. "
                "Check the dashboard or contact the site administrator to resume."
            ),
        )
