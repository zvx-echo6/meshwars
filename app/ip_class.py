"""Coarse hosting-provider classification for a MeshCore ingest batch's
source address -- app/mc_ingest.py's record_ingest_identity() calls
classify_ip() below and stores only the resulting label (mc_ingest_
request_log.ip_class), never the address itself.

---- no outbound lookup, ever -----------------------------------------

This module does exactly one thing: check a single address against a
small, bundled, hand-curated list of CIDR blocks published or
documented by major hosting/cloud providers, entirely in-process. It
makes NO network call of any kind -- no reverse DNS, no WHOIS, no
third-party IP-intelligence API. A DNS lookup was considered and
rejected outright: frontend/privacy.html's closed list of "what leaves
the system" ends with "There is no third-party analytics or telemetry
of any kind in the backend," and a query built from a player's own
source address, sent to an outside resolver on every ingest batch, is
exactly the kind of telemetry that sentence promises does not happen
here, DNS or not.

---- what this can and cannot tell you ---------------------------------

classify_ip() returns exactly one of:

    "datacenter" -- the address falls inside a bundled prefix for one
                    of the providers below. A real signal: a genuine
                    phone running MeshMapper on cellular or home Wi-Fi
                    is never going to originate from Hetzner or OVH.
    "unknown"    -- everything else, INCLUDING an address that is, in
                    reality, residential or mobile. This module has no
                    positive way to confirm either of those from a
                    hosting-provider blocklist alone -- that would need
                    a real IP-intelligence dataset (ISP/ASN attribution
                    for the entire address space) this deployment does
                    not bundle and has no way to keep current without
                    an outbound call this feature exists specifically
                    to avoid. "residential" and "mobile" remain valid
                    values of the ip_class column for a future data
                    source to populate; this module never guesses its
                    way into either and always prefers "unknown" over a
                    wrong positive claim.

---- the bundled list: source, size, staleness --------------------------

Hand-curated from each provider's own well-known, publicly documented
address space (their ASN's registered allocations, as commonly cited in
network-operator blocklists and each provider's own published IP-range
references) as of ~2025-2026 -- NOT machine-generated from any of those
providers' own official range feeds (AWS ip-ranges.json, GCP cloud.json,
Azure's ServiceTags file, etc.), which are each thousands of entries and
change continuously; pulling and refreshing those automatically would
need the outbound network access this module deliberately does not
have. What's below is therefore a small, illustrative SAMPLE -- a few
dozen prefixes per provider, not exhaustive coverage of any of them --
picked to catch a meaningful share of real traffic from each named
provider while staying honest about its own limits. It WILL miss
genuine datacenter traffic from a range not listed here (that traffic
correctly falls back to "unknown", never a false "residential"), and it
WILL go stale as providers grow into new address space. An operator who
wants tighter coverage should periodically refresh this list by hand
against each provider's own published range file -- there is no
automatic update path, by design.

Covers, at minimum, the providers this feature's own design brief named
explicitly: OVH, Hetzner, DigitalOcean, AWS, GCP, Azure, Contabo,
Linode, and Vultr. (The September 2026 incident that prompted this
feature came from an OVH address -- OVH is listed first below for that
reason, not because its coverage is any more or less complete than the
others'.)
"""
from __future__ import annotations

import ipaddress

_IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

IP_CLASS_DATACENTER = "datacenter"
IP_CLASS_UNKNOWN = "unknown"

# provider label -> tuple of CIDR strings. The label itself is never
# stored anywhere (mc_ingest_request_log.ip_class only ever holds
# "datacenter" or "unknown" -- see this module's own docstring); it
# exists here purely so each block below is traceable to the provider
# it was curated for, for whoever next refreshes this list by hand.
_DATACENTER_PREFIXES: dict[str, tuple[str, ...]] = {
    # OVH (AS16276) -- the provider the 2026-09 incident this feature
    # responds to actually came from.
    "ovh": (
        "51.38.0.0/16", "51.68.0.0/16", "51.75.0.0/16", "51.83.0.0/16",
        "54.36.0.0/16", "141.94.0.0/16", "145.239.0.0/16", "146.59.0.0/16",
        "178.32.0.0/15", "188.165.0.0/16", "213.186.32.0/19", "15.235.0.0/16",
    ),
    # Hetzner (AS24940)
    "hetzner": (
        "5.9.0.0/16", "78.46.0.0/15", "88.99.0.0/16", "94.130.0.0/16",
        "116.202.0.0/16", "135.181.0.0/16", "138.201.0.0/16", "148.251.0.0/16",
        "162.55.0.0/16", "168.119.0.0/16", "176.9.0.0/16", "195.201.0.0/16",
        "65.108.0.0/16", "65.109.0.0/16",
    ),
    # DigitalOcean (AS14061)
    "digitalocean": (
        "104.131.0.0/16", "104.236.0.0/16", "138.68.0.0/16", "138.197.0.0/16",
        "139.59.0.0/16", "142.93.0.0/16", "143.110.0.0/16", "146.190.0.0/16",
        "157.245.0.0/16", "159.65.0.0/16", "159.89.0.0/16", "159.203.0.0/16",
        "161.35.0.0/16", "164.90.0.0/16", "165.22.0.0/16", "167.71.0.0/16",
        "167.99.0.0/16", "174.138.0.0/16", "178.62.0.0/17", "188.166.0.0/16",
        "206.189.0.0/16", "209.97.128.0/17",
    ),
    # AWS EC2 -- a small sample of well-known EC2 ranges. AWS's own
    # ip-ranges.json lists thousands of prefixes across every service
    # and region; this is a deliberately small illustrative subset, not
    # an attempt at real coverage of AWS as a whole.
    "aws": (
        "3.0.0.0/9", "13.32.0.0/15", "18.130.0.0/16", "34.192.0.0/10",
        "44.192.0.0/10", "52.0.0.0/11", "54.144.0.0/12", "99.77.0.0/16",
        "100.24.0.0/13", "107.20.0.0/14", "174.129.0.0/16", "184.72.0.0/15",
    ),
    # Google Cloud Platform -- same "small sample" caveat as AWS above.
    "gcp": (
        "34.64.0.0/10", "35.184.0.0/13", "35.192.0.0/14", "104.154.0.0/15",
        "130.211.0.0/16", "146.148.0.0/17", "162.216.148.0/22",
    ),
    # Microsoft Azure -- same "small sample" caveat as AWS above.
    "azure": (
        "13.64.0.0/11", "20.33.0.0/16", "40.64.0.0/10", "52.224.0.0/11",
        "104.40.0.0/13", "137.116.0.0/16", "168.61.0.0/16",
    ),
    # Contabo (AS51167) -- this codebase's own edge1/edge2/edge3 hosts
    # are Contabo servers, so this range correctly classifies THIS
    # deployment's own infrastructure as "datacenter" too, same as any
    # other Contabo customer's traffic -- expected, not a bug.
    "contabo": (
        "5.189.128.0/17", "62.171.128.0/17", "89.163.128.0/17",
        "144.126.128.0/17", "154.53.128.0/17", "173.212.192.0/18",
        "194.163.128.0/17",
    ),
    # Linode / Akamai Connected Cloud (AS63949)
    "linode": (
        "45.33.0.0/16", "45.56.64.0/18", "45.79.0.0/16", "69.164.192.0/18",
        "96.126.96.0/19", "139.144.0.0/16", "172.104.0.0/15", "173.255.192.0/18",
    ),
    # Vultr (AS20473)
    "vultr": (
        "45.32.0.0/16", "45.63.0.0/16", "45.76.0.0/16", "45.77.0.0/16",
        "63.209.0.0/18", "66.42.0.0/17", "104.156.224.0/19", "108.61.0.0/16",
        "149.28.0.0/16", "155.138.128.0/17", "207.246.64.0/18", "208.167.224.0/19",
    ),
}


def _parse_networks() -> list[_IPNetwork]:
    """Every CIDR string above, parsed once. A parse failure here would
    be a bug in this module's own hand-curated data (never attacker
    input -- these strings never come from a request), so it is allowed
    to raise at import time rather than being swallowed: a typo in this
    list should fail loudly in CI/tests, not silently drop a provider's
    entire range in production.
    """
    networks: list[_IPNetwork] = []
    for prefixes in _DATACENTER_PREFIXES.values():
        for cidr in prefixes:
            networks.append(ipaddress.ip_network(cidr))
    return networks


_NETWORKS = _parse_networks()


def classify_ip(raw_ip: str | None) -> str:
    """IP_CLASS_DATACENTER if `raw_ip` falls inside any bundled prefix
    above, else IP_CLASS_UNKNOWN -- including for a missing, empty, or
    unparseable address ("unknown", the string client_ip.py's own
    get_client_ip() returns when Starlette hands back no peer at all,
    included). Never raises on attacker-controlled input: a source
    address off the public ingest endpoint is exactly that, and a
    malformed one must classify as "unknown", not crash the batch that
    carried it.
    """
    if not raw_ip:
        return IP_CLASS_UNKNOWN
    try:
        addr = ipaddress.ip_address(raw_ip)
    except ValueError:
        return IP_CLASS_UNKNOWN
    for network in _NETWORKS:
        if addr in network:
            return IP_CLASS_DATACENTER
    return IP_CLASS_UNKNOWN
