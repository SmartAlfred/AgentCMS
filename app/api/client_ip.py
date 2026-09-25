"""Which address counts as *the client* for a loopback exemption (#49).

Two surfaces exempt a local caller: ``GET /metrics`` and ``/v1/admin/*``.  They
ask this module, so they cannot drift apart again -- ``/metrics`` used to believe
``X-Forwarded-For`` while ``require_admin`` deliberately did not, which meant any
remote caller could claim to be 127.0.0.1 and read the metrics body with no
credentials at all.

``X-Forwarded-For`` is client-supplied, so the rule is:

1. only the *peer* (``request.client.host``) can vouch for the header, and only
   when the peer falls inside ``TRUSTED_PROXIES``;
2. with a trusted peer, the client is the right-most ``X-Forwarded-For`` hop that
   is **not** itself a trusted proxy -- so a proxy that appends the address it
   saw cannot be talked out of naming the real caller;
3. a chain made only of trusted proxies proves nothing and is not exempt;
4. a peer that is literally loopback is exempt on its own: no header can buy
   trust, and none can take it away (unless the operator has declared a same-host
   proxy in ``TRUSTED_PROXIES``, in which case the chain above is walked and a
   forwarded remote caller is *not* exempt);
5. anything else is not loopback, so the caller has to present a credential.

``TRUSTED_PROXIES`` empty -- the default -- means the header is never believed.
That is the conservative behaviour ``require_admin`` shipped with in #44, and it
is what every deployment gets without opting in.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from functools import lru_cache

from fastapi import Request

#: The header a reverse proxy uses to report the original client.
FORWARDED_FOR_HEADER = "X-Forwarded-For"

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_address(value: str | None) -> IPAddress | None:
    """Parse one address, tolerating a zone id (``fe80::1%eth0``).

    A hostname (``testclient``) or garbage is never an address, so it is never
    loopback either.
    """
    if not value:
        return None
    candidate = value.strip().split("%", 1)[0]
    if not candidate:
        return None
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def is_loopback_address(address: IPAddress | None) -> bool:
    """True for 127.0.0.0/8, ::1 and their IPv4-mapped spellings."""
    if address is None:
        return False
    if address.is_loopback:
        return True
    # `::ffff:127.0.0.1` is loopback in practice but is not *in* 127.0.0.0/8.
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped is not None and mapped.is_loopback)


@lru_cache(maxsize=16)
def _parse_networks(values: tuple[str, ...]) -> tuple[IPNetwork, ...]:
    networks: list[IPNetwork] = []
    for value in values:
        candidate = value.strip()
        if not candidate:
            continue
        try:
            # strict=False so `10.1.0.7` and `10.1.0.0/16`-style operator
            # shorthand both work.
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            # An unparsable entry is dropped rather than guessed: the caller's
            # proxy is then simply not trusted, which fails closed.
            continue
    return tuple(networks)


def parse_trusted_proxies(values: Iterable[str]) -> tuple[IPNetwork, ...]:
    """Parse the ``TRUSTED_PROXIES`` setting into networks (cached)."""
    return _parse_networks(tuple(values))


def peer_address(request: Request) -> IPAddress | None:
    """The TCP peer, or None when the transport does not expose one."""
    return parse_address(getattr(request.client, "host", None))


def forwarded_for_chain(request: Request) -> tuple[str, ...]:
    """The ``X-Forwarded-For`` hops, left-most (original client) first."""
    raw = request.headers.get(FORWARDED_FOR_HEADER, "")
    return tuple(part for part in (hop.strip() for hop in raw.split(",")) if part)


def _in_networks(address: IPAddress, networks: Iterable[IPNetwork]) -> bool:
    # An IPv4 range matches the IPv4-mapped spelling of the same address, so a
    # dual-stack proxy on 10.1.0.7 is recognised from either notation.
    for candidate in (address, getattr(address, "ipv4_mapped", None)):
        if candidate is None:
            continue
        if any(candidate in network for network in networks):
            return True
    return False


def effective_client_address(request: Request, trusted_proxies: Iterable[str] = ()) -> IPAddress | None:
    """The address that best represents the caller, or None if unknowable."""
    peer = peer_address(request)
    if peer is None:
        return None

    networks = parse_trusted_proxies(trusted_proxies)
    if not networks or not _in_networks(peer, networks):
        # The header is worth nothing: either nothing is configured (the default)
        # or this peer has not earned the right to speak for anybody else.
        return peer

    for hop in reversed(forwarded_for_chain(request)):
        address = parse_address(hop)
        if address is None or not _in_networks(address, networks):
            return address
    # Every hop is a trusted proxy, so the chain names no client of its own.
    return None


def client_is_loopback(request: Request, trusted_proxies: Iterable[str] = ()) -> bool:
    """True only when the request really came from this machine."""
    return is_loopback_address(effective_client_address(request, trusted_proxies))
