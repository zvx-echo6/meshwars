"""Tests for app/client_ip.py's get_client_ip() -- the shared helper
that resolves the real caller's address behind the Caddy reverse proxy
every deployment of this app runs behind (see that module's docstring).

Builds bare fastapi.Request objects from a hand-rolled ASGI scope
rather than going through TestClient/an HTTP transport (same "minimal,
direct" spirit as tests/test_tiles_api.py's FastAPI-around-one-function
approach) -- get_client_ip() only ever reads request.client and
request.headers, both already fully populated on scope alone, so there
is nothing an actual connection or event loop would add.
"""
from __future__ import annotations

from fastapi import Request

from app.client_ip import get_client_ip
from app.config import settings

TRUSTED_PROXY = "192.168.1.101"
OTHER_TRUSTED_PROXY = "192.168.1.102"
ATTACKER = "203.0.113.50"
REAL_CLIENT_A = "198.51.100.10"
REAL_CLIENT_B = "198.51.100.20"


def _request(peer: str | None, headers: dict[str, str] | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "http_version": "1.1",
        "client": (peer, 51234) if peer is not None else None,
        "headers": [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in (headers or {}).items()
        ],
    }
    return Request(scope)


def test_no_proxy_header_returns_the_peer_address(monkeypatch):
    """No X-Forwarded-For at all -- the peer is used as-is, whether or
    not it happens to be a trusted proxy. There is nothing to resolve.
    """
    monkeypatch.setattr(settings, "trusted_proxies", TRUSTED_PROXY)
    req = _request(TRUSTED_PROXY)
    assert get_client_ip(req) == TRUSTED_PROXY


def test_no_client_at_all_returns_unknown(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", TRUSTED_PROXY)
    req = _request(None)
    assert get_client_ip(req) == "unknown"


def test_untrusted_peer_with_no_trusted_proxies_configured_returns_peer(monkeypatch):
    """The safe default: an empty TRUSTED_PROXIES means X-Forwarded-For
    is never read at all, even if a caller sends one.
    """
    monkeypatch.setattr(settings, "trusted_proxies", "")
    req = _request(REAL_CLIENT_A, {"X-Forwarded-For": ATTACKER})
    assert get_client_ip(req) == REAL_CLIENT_A


def test_single_entry_from_a_trusted_peer_is_honored(monkeypatch):
    """The common case: one Caddy hop directly in front of the app,
    forwarding for exactly one real caller.
    """
    monkeypatch.setattr(settings, "trusted_proxies", TRUSTED_PROXY)
    req = _request(TRUSTED_PROXY, {"X-Forwarded-For": REAL_CLIENT_A})
    assert get_client_ip(req) == REAL_CLIENT_A


def test_multi_hop_chain_picks_the_first_untrusted_hop_from_the_right(monkeypatch):
    """Two trusted proxies forwarding for one real caller:
    "real_client, trusted_proxy_1" arriving from trusted_proxy_2.
    Neither the leftmost (attacker-writable) entry nor the rightmost
    (a proxy, not the caller) is correct on its own -- walking from the
    right and skipping known-trusted hops is what lands on the real
    caller here.
    """
    monkeypatch.setattr(settings, "trusted_proxies", f"{TRUSTED_PROXY},{OTHER_TRUSTED_PROXY}")
    xff = f"{REAL_CLIENT_A}, {TRUSTED_PROXY}"
    req = _request(OTHER_TRUSTED_PROXY, {"X-Forwarded-For": xff})
    assert get_client_ip(req) == REAL_CLIENT_A


def test_spoofed_header_from_an_untrusted_peer_is_ignored(monkeypatch):
    """A caller that reaches the app directly (or through something we
    don't recognize as our own proxy) can write anything into
    X-Forwarded-For it likes -- none of it may be trusted. The peer
    address itself, which nothing under the caller's control can fake,
    is the only thing returned.
    """
    monkeypatch.setattr(settings, "trusted_proxies", TRUSTED_PROXY)
    req = _request(ATTACKER, {"X-Forwarded-For": REAL_CLIENT_A})
    assert get_client_ip(req) == ATTACKER


def test_cidr_range_in_trusted_proxies_is_honored(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "192.168.1.0/24")
    req = _request(TRUSTED_PROXY, {"X-Forwarded-For": REAL_CLIENT_A})
    assert get_client_ip(req) == REAL_CLIENT_A


def test_chain_of_entirely_trusted_hops_falls_back_to_the_peer(monkeypatch):
    """Every entry in the chain claims to be one of our own proxies --
    there's no untrusted address left to point to, so the immediate
    peer (itself trusted) is the most specific honest answer left.
    """
    monkeypatch.setattr(settings, "trusted_proxies", f"{TRUSTED_PROXY},{OTHER_TRUSTED_PROXY}")
    req = _request(OTHER_TRUSTED_PROXY, {"X-Forwarded-For": TRUSTED_PROXY})
    assert get_client_ip(req) == OTHER_TRUSTED_PROXY


def test_malformed_trusted_proxies_entry_does_not_crash(monkeypatch):
    """A typo in an operator's own config must not take rate limiting
    down with it -- see _trusted_networks()'s own comment.
    """
    monkeypatch.setattr(settings, "trusted_proxies", "not-an-ip, " + TRUSTED_PROXY)
    req = _request(TRUSTED_PROXY, {"X-Forwarded-For": REAL_CLIENT_A})
    assert get_client_ip(req) == REAL_CLIENT_A


def test_rate_limiter_distinguishes_two_real_clients_behind_one_proxy(monkeypatch):
    """The actual bug this whole module exists to fix: before
    get_client_ip() existed, app/join_api.py's _rate_limited() (and
    every other module's identical copy) was keyed on
    request.client.host directly -- the proxy's address -- so two
    different real players both got throttled by each other's
    attempts. With the helper in place, each real caller gets its own
    budget again.
    """
    import app.join_api as join_api

    monkeypatch.setattr(join_api, "_attempts", {})
    monkeypatch.setattr(settings, "trusted_proxies", TRUSTED_PROXY)
    monkeypatch.setattr(settings, "join_rate_limit_attempts", 1)
    monkeypatch.setattr(settings, "join_rate_limit_window_seconds", 600)

    req_a = _request(TRUSTED_PROXY, {"X-Forwarded-For": REAL_CLIENT_A})
    req_b = _request(TRUSTED_PROXY, {"X-Forwarded-For": REAL_CLIENT_B})
    ip_a = join_api._client_ip(req_a)
    ip_b = join_api._client_ip(req_b)
    assert ip_a == REAL_CLIENT_A
    assert ip_b == REAL_CLIENT_B

    # Client A's first attempt is allowed, consuming its budget of one.
    assert join_api._rate_limited(ip_a) is False
    # Client A's second attempt, still within the window, is throttled.
    assert join_api._rate_limited(ip_a) is True
    # Client B, a completely different real caller behind the SAME
    # proxy, has its own untouched budget -- this is the fix: before
    # get_client_ip(), ip_a and ip_b would both have been the proxy's
    # own address, and B would already have been throttled by A's
    # attempt above.
    assert join_api._rate_limited(ip_b) is False
