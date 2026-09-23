"""Coarse hosting-provider classification for a MeshCore ingest batch's
source address -- app/mc_ingest.py's record_ingest_identity() calls
classify_ip() below and stores only the resulting label (mc_ingest_
request_log.ip_class), never the address itself.

---- no outbound lookup, ever, AT RUNTIME ------------------------------

This module makes NO network call of any kind, ever -- no reverse DNS,
no WHOIS, no third-party IP-intelligence API. A DNS lookup was
considered and rejected outright: frontend/privacy.html's closed list
of "what leaves the system" ends with "There is no third-party
analytics or telemetry of any kind in the backend," and a query built
from a player's own source address, sent to an outside resolver on
every ingest batch, is exactly the kind of telemetry that sentence
promises does not happen here, DNS or not.

Instead, at IMPORT time (module load, once per process), this module
reads a bundled, pre-built snapshot file --
app/reference/datacenter_prefixes.csv.gz, committed to this repository,
not gitignored (verify with `git check-ignore -v` after touching either
this module or that file -- a silently-ignored data file here would
make every address classify as "unknown" in production, exactly the
failure this feature exists to avoid) -- and builds an in-memory
lookup structure from it. See _load_networks() below for the file
format and _build_lookup() for the structure. Regenerating that
snapshot (app/reference/build_datacenter_prefixes.py) DOES make
outbound calls, but that script is a development-time tool an operator
runs by hand; it is never imported or invoked by the running
application.

---- what this can and cannot tell you ---------------------------------

classify_ip() returns exactly one of:

    "datacenter" -- the address falls inside a bundled prefix for one
                    of the providers in app/reference/datacenter_prefixes.csv.gz.
                    A real signal: a genuine phone running MeshMapper on
                    cellular or home Wi-Fi is never going to originate
                    from Hetzner or OVH space.
    "unknown"    -- everything else, INCLUDING an address that is, in
                    reality, residential or mobile. This module has no
                    positive way to confirm either of those from a
                    hosting-provider blocklist alone -- that would need
                    a real IP-intelligence dataset (ISP/ASN attribution
                    for the entire address space) this deployment does
                    not bundle. "residential" and "mobile" remain valid
                    values of the ip_class column for a future data
                    source to populate; this module never guesses its
                    way into either and always prefers "unknown" over a
                    wrong positive claim.

---- the bundled snapshot: source, size, staleness -----------------------

app/reference/datacenter_prefixes.csv.gz -- see that file's own
`#`-prefixed header (read it directly: `zcat` it, or see
_load_networks() below, which parses those same lines back out into
SNAPSHOT_DATE/SNAPSHOT_SOURCES at import time) for the generation date
and, per provider, exactly which source produced its entries and how
many. In short: AWS, GCP, and Oracle Cloud publish their own
authoritative machine-readable range files, pulled in full; OVH,
Hetzner, Contabo, DigitalOcean, Linode, Vultr, Scaleway, and Azure do
not publish an equivalent self-service file, so their entries are every
prefix RIPEstat's announced-prefixes API currently sees BGP-announced
by that provider's own ASN(s) instead -- see
app/reference/build_datacenter_prefixes.py's own module docstring for
the exact ASN list and why. As of the snapshot this repository ships,
that is ~21,600 prefixes across all eleven providers combined -- real
coverage, not a hand-picked sample of a few dozen -- and it is what
catches the real 2026-09 incident's own address (51.161.33.124, inside
OVH's 51.161.0.0/17 under AS16276): an earlier, hand-picked-sample
version of this module's bundled list did NOT include that specific
/17 and missed it entirely, which is the whole reason this module now
loads a real, wide, regeneratable snapshot instead of a few examples
per provider.

This is still a point-in-time snapshot, not a live feed, and WILL go
stale as providers grow into new address space -- there is no
automatic refresh path, by design (see this module's own "no network
call, ever" section above). Refresh it by re-running
app/reference/build_datacenter_prefixes.py when this app is updated;
provider allocations do not churn quickly enough to need anything more
automated than that.
"""
from __future__ import annotations

import bisect
import csv
import gzip
import ipaddress
from pathlib import Path

_DATA_PATH = Path(__file__).parent / "reference" / "datacenter_prefixes.csv.gz"

IP_CLASS_DATACENTER = "datacenter"
IP_CLASS_UNKNOWN = "unknown"


def _load_networks() -> tuple[list[str], list[str], list[ipaddress.IPv4Network | ipaddress.IPv6Network]]:
    """Reads app/reference/datacenter_prefixes.csv.gz once, at import
    time. Returns (snapshot_meta_lines, providers_seen, networks).

    File format: `#`-prefixed metadata lines (generation date, one line
    per source with its own entry count -- see
    build_datacenter_prefixes.py's own docstring for what each line
    means), then a `provider,cidr` header, then one `provider,cidr` data
    row per prefix. This function keeps every `#` line verbatim (as
    SNAPSHOT_META below) purely so classify_ip()'s own module -- and
    anything introspecting it, like a future admin panel -- can report
    provenance without re-parsing the file a second time or re-deriving
    it from code comments that could drift from what was actually
    fetched.

    Raises (at import time, loudly) if the file is missing or empty --
    NOT swallowed into "every address is unknown", which would be a
    silent, production-only failure mode indistinguishable from the
    feature simply working correctly on traffic that happens to be
    residential. A missing bundled file is a packaging bug and must
    fail the same way a missing required Python module would.
    """
    if not _DATA_PATH.exists():
        raise FileNotFoundError(
            f"{_DATA_PATH} is missing -- app/ip_class.py cannot classify any address "
            "without it. Regenerate with app/reference/build_datacenter_prefixes.py, "
            "and confirm it is actually committed (`git check-ignore -v` on this exact "
            "path must report it is NOT ignored) -- a gitignored copy of this file would "
            "silently make every address classify as 'unknown' in a built image."
        )

    meta_lines: list[str] = []
    providers_seen: set[str] = set()
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []

    with gzip.open(_DATA_PATH, "rt", encoding="utf-8", newline="") as f:
        data_lines = []
        for line in f:
            if line.startswith("#"):
                meta_lines.append(line[1:].strip())
                continue
            data_lines.append(line)

        reader = csv.reader(data_lines)
        header = next(reader, None)
        if header != ["provider", "cidr"]:
            raise ValueError(f"{_DATA_PATH}: unexpected header {header!r}, expected ['provider', 'cidr']")

        for row in reader:
            if not row:
                continue
            provider, cidr = row[0], row[1]
            providers_seen.add(provider)
            # strict=True: every entry here came either from a
            # provider's own official range file or a live BGP
            # announcement, via build_datacenter_prefixes.py's own
            # _valid_cidrs() filter -- both already network-aligned by
            # construction. A non-aligned entry reaching this point
            # would mean the bundled file itself is corrupt, which
            # should fail loudly (an ImportError at process start), not
            # be silently coerced.
            networks.append(ipaddress.ip_network(cidr, strict=True))

    if not networks:
        raise ValueError(f"{_DATA_PATH}: parsed zero usable prefixes -- refusing to load an empty dataset")

    return meta_lines, sorted(providers_seen), networks


def _build_lookup(
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> tuple[list[int], list[int], list[int], list[int]]:
    """Turns `networks` into two sorted, NON-OVERLAPPING interval lists
    (one for IPv4, one for IPv6) that classify_ip() below binary-searches
    -- O(log n) per lookup instead of the O(n) "check every network in
    a flat list" approach a small hand-picked sample could get away
    with, but ~21,600 entries genuinely cannot: this deployment answers
    one lookup per accepted ingest batch, so lookup cost matters.

    Returns (v4_starts, v4_ends, v6_starts, v6_ends) -- parallel sorted
    lists (v4_starts[i] and v4_ends[i] together describe the i'th
    merged interval's [start, end] range, as plain ints, IPv4 and IPv6
    address spaces kept entirely separate since they're never
    comparable to each other). classify_ip() finds the rightmost
    interval whose start is <= the target address (bisect_right on
    *_starts) and then just checks whether the target falls at or
    before that one interval's own end -- correct specifically because
    the intervals here are merged and non-overlapping: at most one
    interval can ever contain a given address, so there is no second
    candidate to also check.

    Merging (rather than just sorting the raw networks and binary
    searching those) also shrinks the search space for free when
    providers' own ranges overlap or sit adjacent to each other (a
    provider's range file commonly lists both an aggregate and some of
    its own sub-blocks) -- fewer, wider intervals, same coverage.
    """
    v4_ranges: list[tuple[int, int]] = []
    v6_ranges: list[tuple[int, int]] = []
    for net in networks:
        target = v4_ranges if net.version == 4 else v6_ranges
        target.append((int(net.network_address), int(net.broadcast_address)))

    def _merge(ranges: list[tuple[int, int]]) -> tuple[list[int], list[int]]:
        if not ranges:
            return [], []
        ranges.sort()
        merged: list[list[int]] = [list(ranges[0])]
        for start, end in ranges[1:]:
            last = merged[-1]
            if start <= last[1] + 1:  # overlapping or immediately adjacent
                last[1] = max(last[1], end)
            else:
                merged.append([start, end])
        starts = [m[0] for m in merged]
        ends = [m[1] for m in merged]
        return starts, ends

    v4_starts, v4_ends = _merge(v4_ranges)
    v6_starts, v6_ends = _merge(v6_ranges)
    return v4_starts, v4_ends, v6_starts, v6_ends


# ---- module-level state, resolved once at import time --------------------

_SNAPSHOT_META, _SNAPSHOT_PROVIDERS, _NETWORKS = _load_networks()
_V4_STARTS, _V4_ENDS, _V6_STARTS, _V6_ENDS = _build_lookup(_NETWORKS)

# Exposed for introspection/tests/a future admin panel -- see
# _load_networks()'s own docstring for what each element is.
SNAPSHOT_META: tuple[str, ...] = tuple(_SNAPSHOT_META)
SNAPSHOT_PROVIDERS: tuple[str, ...] = tuple(_SNAPSHOT_PROVIDERS)


def _contains(addr_int: int, starts: list[int], ends: list[int]) -> bool:
    if not starts:
        return False
    i = bisect.bisect_right(starts, addr_int) - 1
    return i >= 0 and addr_int <= ends[i]


def classify_ip(raw_ip: str | None) -> str:
    """IP_CLASS_DATACENTER if `raw_ip` falls inside any bundled prefix
    (app/reference/datacenter_prefixes.csv.gz), else IP_CLASS_UNKNOWN --
    including for a missing, empty, or unparseable address ("unknown",
    the string client_ip.py's own get_client_ip() returns when
    Starlette hands back no peer at all, included). Never raises on
    attacker-controlled input: a source address off the public ingest
    endpoint is exactly that, and a malformed one must classify as
    "unknown", not crash the batch that carried it.
    """
    if not raw_ip:
        return IP_CLASS_UNKNOWN
    try:
        addr = ipaddress.ip_address(raw_ip)
    except ValueError:
        return IP_CLASS_UNKNOWN

    if addr.version == 4:
        hit = _contains(int(addr), _V4_STARTS, _V4_ENDS)
    else:
        hit = _contains(int(addr), _V6_STARTS, _V6_ENDS)
    return IP_CLASS_DATACENTER if hit else IP_CLASS_UNKNOWN
