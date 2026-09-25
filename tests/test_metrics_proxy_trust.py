"""#49: the ``/metrics`` loopback exemption must not believe ``X-Forwarded-For``.

Before the fix, ``app/api/observability.py::_client_ip()`` returned the *first*
``X-Forwarded-For`` value whenever the header was present, and that value
decided the ``127.0.0.0/8`` / ``::1`` exemption.  The header is client-supplied,
so any caller that could reach the app with the header intact read the metrics
surface unauthenticated -- while ``app/api/admin_auth.py`` deliberately ignored
the very same header.  The two surfaces now ask one helper
(:mod:`app.api.client_ip`), which only believes the header when the *peer* is a
configured trusted proxy (``TRUSTED_PROXIES``) and then takes the right-most hop
that is not itself a trusted proxy.

The tests are behavioural first: a production-configured app behind a
``TestClient`` whose peer address is chosen per test, which is the only way to
show what a remote caller with a spoofed header actually gets.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from app.api.client_ip import client_is_loopback, effective_client_address, parse_trusted_proxies
from app.config import Settings
from fastapi.testclient import TestClient
from starlette.requests import Request

PROD_SECRET = "production-secret-key-long-enough-00000000"
PROD_ADMIN_TOKEN = "production-admin-bootstrap-token-000001"
PROD_IMAGE_TAG = "ghcr.io/smartalfred/agentcms:v0.3.1"
PROD_METRICS_TOKEN = "production-metrics-token-000000000000"

#: A remote caller (documentation range, so it can never be a real peer).
REMOTE_PEER = ("203.0.113.7", 51000)
#: Prometheus / node_exporter on the same host.
LOOPBACK_PEER = ("127.0.0.1", 51000)
#: A reverse proxy on the same private network (Caddy in compose, a load balancer).
PROXY_PEER = ("10.1.0.7", 51000)

Peer = tuple[str, int]


def _production_env(database_url: str, monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    values = {
        "APP_ENV": "production",
        "DATABASE_URL": database_url,
        "SECRET_KEY": PROD_SECRET,
        "AGENTCMS_IMAGE_TAG": PROD_IMAGE_TAG,
        "ADMIN_TOKEN": PROD_ADMIN_TOKEN,
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _settings(database_url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "production",
        "database_url": database_url,
        "secret_key": PROD_SECRET,
        "agentcms_image_tag": PROD_IMAGE_TAG,
        "admin_token": PROD_ADMIN_TOKEN,
        "log_level": "WARNING",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@contextmanager
def _client(settings: Settings, peer: Peer) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app(settings), client=peer) as client:
        yield client


@pytest.fixture()
def prod_client(
    database_url: str, db, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., Iterator[TestClient]]:
    """A production-configured app on the throwaway database, at a chosen peer.

    Only production shows the defect: ``APP_ENV=test`` is exempt (so the rest of
    the suite can scrape without secrets) and ``app.state.settings`` decides the
    exemption, exactly as it does in a real deployment.
    """
    _production_env(database_url, monkeypatch)

    def factory(peer: Peer = REMOTE_PEER, **overrides: object) -> Iterator[TestClient]:
        return _client(_settings(database_url, **overrides), peer)

    return factory


def _request(peer: str | None, forwarded: str | None = None) -> Request:
    """A bare request scope: no app, no settings, just peer + headers."""
    headers: list[tuple[bytes, bytes]] = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/metrics",
            "query_string": b"",
            "headers": headers,
            "client": (peer, 51000) if peer else None,
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )


# ---------------------------------------------------------------------------
# The regression: a client-supplied header must not buy the exemption
# ---------------------------------------------------------------------------


class TestSpoofedForwardedFor:
    def test_forwarded_for_from_a_remote_peer_is_refused(self, prod_client) -> None:
        """#49: the header is attacker-controlled, so it must not decide trust.

        This is the whole ticket: with a non-loopback peer, sending
        ``X-Forwarded-For: 127.0.0.1`` used to return the metrics body.
        """
        with prod_client() as client:
            response = client.get("/metrics", headers={"X-Forwarded-For": "127.0.0.1"})

        assert response.status_code == 403, response.text
        assert "admin-gated" in response.text

    def test_a_chained_header_from_a_remote_peer_is_refused(self, prod_client) -> None:
        """Padding the header with a real-looking hop does not help either."""
        with prod_client() as client:
            response = client.get(
                "/metrics",
                headers={"X-Forwarded-For": "127.0.0.1, 10.0.0.1, 203.0.113.7"},
            )

        assert response.status_code == 403, response.text

    def test_a_remote_peer_without_credentials_is_still_refused(self, prod_client) -> None:
        """The control: the exemption is not simply gone."""
        with prod_client() as client:
            response = client.get("/metrics")

        assert response.status_code == 403, response.text

    def test_a_loopback_peer_may_still_scrape(self, prod_client) -> None:
        """The documented exemption survives: same host, no proxy, no secret."""
        with prod_client(peer=LOOPBACK_PEER) as client:
            response = client.get("/metrics")

        assert response.status_code == 200, response.text
        assert "http_requests_total" in response.text

    def test_a_loopback_peer_is_exempt_even_while_spoofing(self, prod_client) -> None:
        """The header can neither buy nor revoke trust for a real loopback peer."""
        with prod_client(peer=LOOPBACK_PEER) as client:
            response = client.get("/metrics", headers={"X-Forwarded-For": "203.0.113.7"})

        assert response.status_code == 200, response.text

    def test_the_metrics_token_still_unlocks_a_remote_scrape(self, prod_client) -> None:
        """Positive control: the gate still opens, it is the header that stopped working."""
        with prod_client(metrics_token=PROD_METRICS_TOKEN) as client:
            response = client.get("/metrics", headers={"X-Metrics-Token": PROD_METRICS_TOKEN})

        assert response.status_code == 200, response.text
        assert "http_requests_total" in response.text


# ---------------------------------------------------------------------------
# The opt-in escape hatch: TRUSTED_PROXIES
# ---------------------------------------------------------------------------


class TestTrustedProxies:
    def test_a_trusted_proxy_may_report_a_loopback_client(self, prod_client) -> None:
        """A proxy that *replaces* the header (like the shipped Caddyfile) is believed."""
        with prod_client(peer=PROXY_PEER, trusted_proxies=["10.0.0.0/8"]) as client:
            response = client.get("/metrics", headers={"X-Forwarded-For": "127.0.0.1"})

        assert response.status_code == 200, response.text
        assert "http_requests_total" in response.text

    def test_a_proxy_that_appends_cannot_be_spoofed_through(self, prod_client) -> None:
        """The appending-proxy shape the ticket calls out: still refused.

        The attacker sends ``X-Forwarded-For: 127.0.0.1``; the proxy appends the
        address it saw, so the right-most hop is the attacker, not 127.0.0.1.
        """
        with prod_client(peer=PROXY_PEER, trusted_proxies=["10.0.0.0/8"]) as client:
            response = client.get("/metrics", headers={"X-Forwarded-For": "127.0.0.1, 203.0.113.7"})

        assert response.status_code == 403, response.text

    def test_a_whole_chain_of_trusted_hops_is_not_exempt(self, prod_client) -> None:
        """Fail closed: a chain made only of proxies proves nothing about loopback."""
        with prod_client(peer=PROXY_PEER, trusted_proxies=["10.0.0.0/8"]) as client:
            response = client.get("/metrics", headers={"X-Forwarded-For": "10.0.0.1, 10.0.0.2"})

        assert response.status_code == 403, response.text

    def test_an_untrusted_peer_cannot_vouch_for_itself(self, prod_client) -> None:
        """Being outside TRUSTED_PROXIES is what the default deployment relies on."""
        with prod_client(peer=REMOTE_PEER, trusted_proxies=["10.0.0.0/8"]) as client:
            response = client.get("/metrics", headers={"X-Forwarded-For": "127.0.0.1"})

        assert response.status_code == 403, response.text

    def test_a_trusted_proxy_does_not_open_up_a_remote_caller(self, prod_client) -> None:
        """A header *absent* behind a trusted proxy is not an exemption either."""
        with prod_client(peer=PROXY_PEER, trusted_proxies=["10.0.0.0/8"]) as client:
            response = client.get("/metrics")

        assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# The two surfaces agree
# ---------------------------------------------------------------------------


class TestAdminSurfaceAgrees:
    def test_a_spoofed_header_does_not_open_the_admin_surface(self, prod_client) -> None:
        """``require_admin`` already ignored XFF; the surfaces must still agree."""
        with prod_client() as client:
            response = client.post(
                "/v1/admin/tokens",
                json={"label": "spoofed", "scopes": ["posts:write"]},
                headers={"X-Forwarded-For": "127.0.0.1"},
            )

        assert response.status_code == 401, response.text

    def test_a_spoofed_header_does_not_open_it_behind_a_trusted_proxy(self, prod_client) -> None:
        """The appending-proxy shape, on the admin surface: still refused.

        The peer is the proxy, but the right-most hop is the remote caller, so
        the attacker-controlled ``127.0.0.1`` never gets a vote.
        """
        with prod_client(peer=PROXY_PEER, trusted_proxies=["10.0.0.0/8"]) as client:
            response = client.post(
                "/v1/admin/tokens",
                json={"label": "spoofed", "scopes": ["posts:write"]},
                headers={"X-Forwarded-For": "127.0.0.1, 203.0.113.7"},
            )

        assert response.status_code == 401, response.text

    def test_a_local_caller_through_a_declared_proxy_is_exempt_from_both(self, prod_client) -> None:
        """The opt-in, stated positively: trusting your own proxy is a choice.

        Same rule, same outcome on both surfaces -- and the only way a header
        participates in the decision at all.
        """
        with prod_client(peer=PROXY_PEER, trusted_proxies=["10.0.0.0/8"]) as client:
            metrics = client.get("/metrics", headers={"X-Forwarded-For": "127.0.0.1"})
            tokens = client.post(
                "/v1/admin/tokens",
                json={"label": "local-op", "scopes": ["posts:read"]},
                headers={"X-Forwarded-For": "127.0.0.1"},
            )

        assert metrics.status_code == 200, metrics.text
        assert tokens.status_code in {200, 201}, tokens.text

    def test_a_loopback_peer_is_exempt_from_both_surfaces(self, prod_client) -> None:
        """One rule, two call sites: loopback means loopback for /v1/admin too."""
        with prod_client(peer=LOOPBACK_PEER) as client:
            metrics = client.get("/metrics")
            tokens = client.post("/v1/admin/tokens", json={"label": "local-op", "scopes": ["posts:read"]})

        assert metrics.status_code == 200, metrics.text
        assert tokens.status_code in {200, 201}, tokens.text


# ---------------------------------------------------------------------------
# The rule itself, without the app around it
# ---------------------------------------------------------------------------


class TestClientIpHelper:
    def test_the_header_is_ignored_when_nothing_is_configured(self) -> None:
        assert client_is_loopback(_request("203.0.113.7", "127.0.0.1"), []) is False

    def test_the_header_is_ignored_when_the_peer_is_not_trusted(self) -> None:
        assert client_is_loopback(_request("203.0.113.7", "127.0.0.1"), ["10.0.0.0/8"]) is False

    def test_the_header_is_honoured_when_the_peer_is_trusted(self) -> None:
        assert client_is_loopback(_request("10.1.0.7", "127.0.0.1"), ["10.0.0.0/8"]) is True

    def test_the_right_most_untrusted_hop_wins(self) -> None:
        request = _request("10.1.0.7", "127.0.0.1, 203.0.113.7, 10.1.0.2")
        assert client_is_loopback(request, ["10.0.0.0/8"]) is False

    def test_a_literal_loopback_peer_never_needs_the_header(self) -> None:
        assert client_is_loopback(_request("127.0.0.1"), []) is True
        assert client_is_loopback(_request("::1"), []) is True
        # IPv4-mapped IPv6 loopback (a dual-stack listener) is still loopback.
        assert client_is_loopback(_request("::ffff:127.0.0.1"), []) is True
        assert client_is_loopback(_request("127.0.0.53"), []) is True

    def test_a_name_is_never_loopback(self) -> None:
        # The default TestClient peer is the string "testclient".
        assert client_is_loopback(_request("testclient"), ["127.0.0.0/8"]) is False

    def test_a_missing_peer_is_never_loopback(self) -> None:
        assert client_is_loopback(_request(None, "127.0.0.1"), ["127.0.0.0/8"]) is False

    def test_an_unparsable_hop_is_not_loopback(self) -> None:
        request = _request("10.1.0.7", "127.0.0.1, not-an-ip")
        assert client_is_loopback(request, ["10.0.0.0/8"]) is False

    def test_a_zone_id_does_not_hide_a_loopback_peer(self) -> None:
        assert client_is_loopback(_request("::1%lo0"), []) is True

    def test_effective_client_address_is_the_peer_for_a_remote_caller(self) -> None:
        address = effective_client_address(_request("203.0.113.7", "127.0.0.1"), ["10.0.0.0/8"])
        assert address == ipaddress.ip_address("203.0.113.7")

    def test_effective_client_address_walks_a_trusted_chain(self) -> None:
        request = _request("10.1.0.7", "127.0.0.1, 10.1.0.2")
        assert effective_client_address(request, ["10.0.0.0/8"]) == ipaddress.ip_address("127.0.0.1")


class TestTrustedProxyParsing:
    def test_a_bare_address_becomes_a_host_network(self) -> None:
        assert parse_trusted_proxies(["10.1.0.7"]) == (ipaddress.ip_network("10.1.0.7/32"),)
        assert parse_trusted_proxies(["::1"]) == (ipaddress.ip_network("::1/128"),)

    def test_cidrs_and_whitespace_are_accepted(self) -> None:
        parsed = parse_trusted_proxies([" 10.0.0.0/8 ", "fd00::/8"])
        assert parsed == (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("fd00::/8"))

    def test_unparsable_entries_are_dropped_rather_than_guessed(self) -> None:
        """A typo must fail closed (peer-only), never open the exemption."""
        assert parse_trusted_proxies(["10.0.0.0/8", "not-a-network", ""]) == (
            ipaddress.ip_network("10.0.0.0/8"),
        )

    def test_an_ipv4_range_matches_an_ipv4_mapped_peer(self) -> None:
        assert parse_trusted_proxies(["10.0.0.0/8"]) == (ipaddress.ip_network("10.0.0.0/8"),)
        request = _request("::ffff:10.1.0.7", "127.0.0.1")

        assert client_is_loopback(request, ["10.0.0.0/8"]) is True


class TestTrustedProxiesSetting:
    def test_it_defaults_to_empty_which_fails_closed(self) -> None:
        assert Settings(_env_file=None).trusted_proxies == []

    def test_it_parses_a_comma_separated_string(self) -> None:
        settings = Settings(_env_file=None, trusted_proxies="10.0.0.0/8, 10.1.0.7")  # type: ignore[arg-type]
        assert settings.trusted_proxies == ["10.0.0.0/8", "10.1.0.7"]

    def test_an_empty_value_stays_empty(self) -> None:
        settings = Settings(_env_file=None, trusted_proxies="")  # type: ignore[arg-type]
        assert settings.trusted_proxies == []


# ---------------------------------------------------------------------------
# The shipped stack keeps the conservative default
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
PROD_COMPOSE_FILES = ("compose.prod.yml", "deploy/compose/docker-compose.prod.yml")


class TestShippedStack:
    def test_caddy_replaces_the_forwarded_header_instead_of_appending(self) -> None:
        """Why the shipped path was never exploitable: the edge overwrites XFF.

        ``{remote}`` (not ``+ {remote}``) means the app only ever sees the
        address Caddy saw, so a spoofed header never reaches the app at all.
        """
        caddyfile = (ROOT / "deploy/compose/Caddyfile").read_text()
        assert "header_up X-Forwarded-For {remote}" in caddyfile
        assert "header_up X-Forwarded-For + {remote}" not in caddyfile

    def test_production_compose_passes_the_setting_through_with_an_empty_default(self) -> None:
        """Operators can opt in, and the default is still "trust nobody"."""
        for relative in PROD_COMPOSE_FILES:
            body = (ROOT / relative).read_text()
            assert "TRUSTED_PROXIES: ${TRUSTED_PROXIES:-}" in body, relative
