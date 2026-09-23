"""Unit tests for app/ip_class.py's classify_ip() -- the coarse,
in-process, no-network hosting-provider classification
app/mc_ingest.py's record_ingest_identity() stores in
app/db.py's mc_ingest_request_log.ip_class. See that module's own
docstring for the full "no outbound lookup, ever" contract and for what
this can and cannot positively tell you (only "datacenter" is ever
positively asserted; "residential"/"mobile" remain valid column values
this module never produces from a hosting-provider blocklist alone).
"""
from __future__ import annotations

from app import ip_class as ip_class_module
from app.ip_class import IP_CLASS_DATACENTER, IP_CLASS_UNKNOWN, classify_ip

# An address inside the bundled OVH prefix (51.68.0.0/16) -- a large
# public hosting-provider range, used here only to prove the
# prefix-match logic; not any specific machine's real address. OVH is
# the provider named in this feature's own design brief (the 2026-09
# incident that prompted it came from an OVH address).
OVH_ADDR = "51.68.1.1"

# Inside the bundled Hetzner prefix (5.9.0.0/16).
HETZNER_ADDR = "5.9.100.1"

# RFC 5737 "TEST-NET-3" -- reserved for documentation, guaranteed never
# publicly routed or assigned to any real host.
DOCS_ADDR = "203.0.113.5"


def test_known_ovh_prefix_classifies_as_datacenter():
    assert classify_ip(OVH_ADDR) == IP_CLASS_DATACENTER


def test_known_hetzner_prefix_classifies_as_datacenter():
    assert classify_ip(HETZNER_ADDR) == IP_CLASS_DATACENTER


def test_documentation_range_address_classifies_as_unknown():
    """An address that is neither in the bundled list nor a real
    assignment anywhere -- the honest default, never a guess."""
    assert classify_ip(DOCS_ADDR) == IP_CLASS_UNKNOWN


def test_missing_address_classifies_as_unknown():
    assert classify_ip(None) == IP_CLASS_UNKNOWN
    assert classify_ip("") == IP_CLASS_UNKNOWN


def test_the_get_client_ip_unknown_fallback_classifies_as_unknown():
    """app/client_ip.py's get_client_ip() returns the literal string
    "unknown" when Starlette hands back no peer at all -- that value
    must classify as IP_CLASS_UNKNOWN, not raise."""
    assert classify_ip("unknown") == IP_CLASS_UNKNOWN


def test_malformed_address_does_not_raise():
    """Source address is attacker-controlled input off a public
    endpoint -- a garbage string must classify as unknown, never crash
    the batch that carried it."""
    assert classify_ip("not-an-ip-address") == IP_CLASS_UNKNOWN
    assert classify_ip("999.999.999.999") == IP_CLASS_UNKNOWN


def test_ipv6_address_does_not_raise():
    assert classify_ip("2001:db8::1") == IP_CLASS_UNKNOWN


def test_at_least_the_nine_named_providers_are_covered():
    """This feature's own design brief names OVH, Hetzner,
    DigitalOcean, AWS, GCP, Azure, Contabo, Linode, and Vultr
    explicitly as the minimum coverage -- confirm all nine are actually
    present in the bundled data, not just described in a comment."""
    expected = {
        "ovh", "hetzner", "digitalocean", "aws", "gcp",
        "azure", "contabo", "linode", "vultr",
    }
    assert expected.issubset(ip_class_module._DATACENTER_PREFIXES.keys())
    for provider in expected:
        assert len(ip_class_module._DATACENTER_PREFIXES[provider]) > 0, provider


def test_bundled_prefixes_all_parse_as_valid_networks():
    """Every hand-curated CIDR string must actually parse -- a typo here
    would silently drop a provider's entire range from coverage."""
    assert len(ip_class_module._NETWORKS) > 0
    for prefixes in ip_class_module._DATACENTER_PREFIXES.values():
        for cidr in prefixes:
            # Raises ValueError on a malformed or non-network-aligned
            # CIDR string -- this call succeeding IS the assertion.
            import ipaddress
            ipaddress.ip_network(cidr)


def test_no_network_call_is_ever_made(monkeypatch):
    """Nothing in classify_ip()'s call path may open a socket -- patch
    every socket-opening primitive to explode, then classify both a
    known and an unknown address, and confirm neither touched the
    network."""
    import socket

    def _boom(*args, **kwargs):
        raise AssertionError("classify_ip() must never open a network connection")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    monkeypatch.setattr(socket, "gethostbyaddr", _boom)
    monkeypatch.setattr(socket, "gethostbyname", _boom)

    assert classify_ip(OVH_ADDR) == IP_CLASS_DATACENTER
    assert classify_ip(DOCS_ADDR) == IP_CLASS_UNKNOWN
