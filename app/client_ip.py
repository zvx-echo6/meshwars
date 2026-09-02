"""Resolve the real client IP for a request reaching this app through a
reverse proxy.

Every deployment of this app that has been checked runs behind a Caddy
reverse proxy that terminates TLS and forwards to this container over
plain HTTP -- see docker-compose.yml's `ports` mapping, which is a
normal `host:container` publish, not `network_mode: host`. That means
`request.client.host` is always Caddy's own address, never the address
of whoever is actually playing the game. Every per-address rate limiter
in this codebase (app/join_api.py, app/nodes_api.py, app/checkin_api.py,
app/clientlog_api.py, app/mc_api.py, app/public_api.py) keyed its
tracking dict on that value, so in production every caller on the
internet has been sharing ONE rate-limit bucket -- the proxy's -- since
the day each limiter shipped. This module is the fix, used by all six.

Caddy carries the real chain in X-Forwarded-For, but a header is just
text a client can put anything into. It can only be trusted once we
know the peer that actually opened the TCP connection to us is a proxy
we ourselves run -- settings.trusted_proxies (app/config.py) is that
allowlist. It defaults to empty, the same "empty means off, never open"
contract every other gate in app/config.py already uses (join_invite_code,
admin_token, ...): a fresh clone with nothing configured here must not
let a client's own header pick its own rate-limit bucket. See
trusted_proxies' own comment in app/config.py for the value this
deployment actually needs and where it is set.

uvicorn is also started with --proxy-headers (see the Dockerfile CMD),
which does its own, very similar, X-Forwarded-For resolution and can
already overwrite request.client.host before this module ever sees it.
That is deliberate defense in depth, not redundant: uvicorn's
--forwarded-allow-ips is a process-startup flag with its own safe
default (127.0.0.1), so a deployment that only ever configures
settings.trusted_proxies (an ordinary env var, not a container command
line) still gets a correct answer here regardless of whether uvicorn's
flag was also wired up to match. If uvicorn HAS already resolved things
correctly, request.client.host is simply already the real client by the
time get_client_ip() runs, that address will not be in trusted_proxies
(it's a player's IP, not our proxy's), and this function returns it
unchanged on the first line below -- no harm done either way.
"""
from __future__ import annotations

import ipaddress

from fastapi import Request

from .config import settings

_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
_IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _trusted_networks() -> list[_IPNetwork]:
    """settings.trusted_proxies_set, parsed into networks.

    Accepts bare IPs ("192.168.1.101") and CIDR ranges ("10.0.0.0/8")
    in the same comma-separated field -- strict=False so a bare IP is
    treated as its own /32 (or /128) network rather than requiring the
    caller to spell that out. Read fresh on every call instead of
    cached: settings.trusted_proxies is an ordinary attribute tests
    monkeypatch directly (see tests/test_client_ip.py), and re-parsing
    a handful of comma-separated entries per request is not worth
    caching against that.
    """
    networks: list[_IPNetwork] = []
    for entry in settings.trusted_proxies_set:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            # A typo in an operator's own TRUSTED_PROXIES value must
            # never take rate limiting down with it -- it just never
            # matches anything, same as leaving the entry out entirely.
            continue
    return networks


def _is_trusted(addr: str, networks: list[_IPNetwork]) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in networks)


def get_client_ip(request: Request) -> str:
    """The real caller's address for rate-limiting and logging purposes.

    Only reads X-Forwarded-For when the peer that actually connected to
    us (request.client.host) is a configured trusted proxy; otherwise
    the peer address itself is returned as-is, exactly as every
    duplicated `_client_ip()` in this codebase already did before this
    helper existed -- including the "unknown" fallback when Starlette
    hands back no client at all (a unix socket, or a test harness that
    never set one).
    """
    peer = request.client.host if request.client else None
    if peer is None:
        return "unknown"

    networks = _trusted_networks()
    if not networks or not _is_trusted(peer, networks):
        return peer

    xff = request.headers.get("x-forwarded-for")
    if not xff:
        return peer

    # X-Forwarded-For reads left-to-right, oldest hop first: the
    # original caller writes (or omits) element zero, and each proxy
    # the request passes through afterwards APPENDS its own view of who
    # it heard from, to the right. Two ways to read this go wrong:
    #
    #  - Taking the FIRST entry trusts whatever the original caller
    #    put there themselves. A client that talks to a trusted proxy
    #    directly can still write "1.2.3.4" as element zero and have
    #    nothing else in the chain to contradict it.
    #  - Taking the LAST entry is wrong the moment there is more than
    #    one hop: in a chain of two trusted proxies forwarding for one
    #    real caller, the last entry is the SECOND proxy's own address,
    #    not the caller's.
    #
    # The correct read is a walk from the right -- the hop closest to
    # us, which we already know is trusted, since it's `peer` -- popping
    # off entries that are themselves trusted proxies, and stopping at
    # the first one that isn't. That is the closest-to-us hop that
    # neither we nor any proxy we run vouches for: the real caller,
    # however many trusted hops separate it from us. If every entry
    # claims to be one of our own proxies, there is no untrusted address
    # left to point to, so we fall back to `peer` -- still correct, just
    # not more specific than "one of our proxies talked to us."
    chain = [hop.strip() for hop in xff.split(",") if hop.strip()]
    for hop in reversed(chain):
        if not _is_trusted(hop, networks):
            return hop
    return peer
