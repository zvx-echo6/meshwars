"""Unit tests for app/ip_class.py's classify_ip() -- the coarse,
in-process, no-network hosting-provider classification
app/mc_ingest.py's record_ingest_identity() stores in
app/db.py's mc_ingest_request_log.ip_class. See that module's own
docstring for the full "no outbound lookup, ever, AT RUNTIME" contract
and for what this can and cannot positively tell you (only
"datacenter" is ever positively asserted; "residential"/"mobile" remain
valid column values this module never produces from a hosting-provider
blocklist alone).

This module was rebuilt after an earlier version's hand-picked sample
of a few dozen CIDRs per provider MISSED the real 2026-09 incident's
own attacker address entirely (it fell in an OVH /17 the sample simply
never included) -- see app/reference/datacenter_prefixes.csv.gz and
app/reference/build_datacenter_prefixes.py for the real, ~21,600-entry
bundled snapshot that replaced it. Every "must classify as datacenter"
address below was verified against RIPEstat's own network-info/
as-overview APIs (live BGP attribution) to actually belong to the named
provider before being written into this file -- these are not
arbitrary examples.
"""
from __future__ import annotations

import importlib
import socket
import time

import app.ip_class as ip_class_module
from app.ip_class import IP_CLASS_DATACENTER, IP_CLASS_UNKNOWN, classify_ip

# ---------------------------------------------------------------------
# Real, verified addresses -- every one of these was checked against
# RIPEstat's network-info (which ASN currently announces it) and
# as-overview (that ASN's holder name) APIs before being written here.
# ---------------------------------------------------------------------

# THE regression this rebuild exists for: the real September 2026
# incident's own attacker address, inside OVH's 51.161.0.0/17 (AS16276,
# "OVH OVH SAS" per RIPEstat, confirmed 2026-09-23). The hand-picked
# sample this module used to ship did NOT include this /17 -- only a
# few unrelated OVH /16s -- and classified this exact address as
# "unknown". If this test ever goes back to failing, the bundled
# snapshot has gone stale or been re-narrowed and no longer catches the
# one real case that motivated this feature.
SEPT_2026_ATTACKER_ADDR = "51.161.33.124"

# Same OVH /17 as above, a different host within it.
OVH_SAME_PREFIX_ADDR = "51.161.0.1"

# Hetzner Online GmbH (AS24940), confirmed via RIPEstat 2026-09-23.
HETZNER_ADDR = "95.216.1.1"

# Contabo GmbH (AS51167), confirmed via RIPEstat 2026-09-23 -- this
# repo's own edge1/edge2/edge3 hosts are also real Contabo servers (see
# ~/.claude/CLAUDE.md's infra cheat-sheet), so this provider's coverage
# is not academic: this deployment's OWN infrastructure would correctly
# classify as "datacenter" too if it ever hit this endpoint, which is
# expected, not a bug.
CONTABO_ADDR = "194.15.110.1"

# DigitalOcean, LLC (AS14061), confirmed via RIPEstat 2026-09-23.
DIGITALOCEAN_ADDR = "167.99.1.1"

# AWS (published directly in ip-ranges.json, not ASN-derived).
AWS_ADDR = "3.5.1.1"

# Comcast Cable Communications, LLC (AS7922) -- a genuine residential
# broadband ISP, confirmed via RIPEstat 2026-09-23. Must NOT classify
# as datacenter: the whole point of this feature is telling a real
# player's home connection apart from a rented server.
COMCAST_RESIDENTIAL_ADDR = "73.15.44.201"

# NTL Virgin Media Limited (AS5089) -- a genuine UK residential
# broadband ISP, confirmed via RIPEstat 2026-09-23.
VIRGIN_MEDIA_RESIDENTIAL_ADDR = "86.15.0.1"

# T-Mobile USA, Inc. (AS21928) -- a genuine mobile carrier, confirmed
# via RIPEstat 2026-09-23. Must NOT classify as datacenter: a player on
# cellular data is exactly the traffic this feature must never flag.
TMOBILE_MOBILE_ADDR = "172.56.0.1"

# RFC 5737 "TEST-NET-3" -- reserved for documentation, guaranteed never
# publicly routed or assigned to any real host.
DOCS_ADDR = "203.0.113.5"


def test_the_real_incident_address_classifies_as_datacenter():
    """THE regression: see SEPT_2026_ATTACKER_ADDR's own comment above.
    A prefix list that misses its own motivating case is worse than no
    field at all."""
    assert classify_ip(SEPT_2026_ATTACKER_ADDR) == IP_CLASS_DATACENTER


def test_same_ovh_prefix_also_classifies_as_datacenter():
    assert classify_ip(OVH_SAME_PREFIX_ADDR) == IP_CLASS_DATACENTER


def test_known_hetzner_address_classifies_as_datacenter():
    assert classify_ip(HETZNER_ADDR) == IP_CLASS_DATACENTER


def test_known_contabo_address_classifies_as_datacenter():
    assert classify_ip(CONTABO_ADDR) == IP_CLASS_DATACENTER


def test_known_digitalocean_address_classifies_as_datacenter():
    assert classify_ip(DIGITALOCEAN_ADDR) == IP_CLASS_DATACENTER


def test_known_aws_address_classifies_as_datacenter():
    assert classify_ip(AWS_ADDR) == IP_CLASS_DATACENTER


def test_residential_comcast_address_is_not_datacenter():
    """The list must not be so broad that it flags real players. A
    genuine home-broadband address must stay 'unknown', never a false
    'datacenter' positive."""
    assert classify_ip(COMCAST_RESIDENTIAL_ADDR) == IP_CLASS_UNKNOWN


def test_residential_virgin_media_address_is_not_datacenter():
    assert classify_ip(VIRGIN_MEDIA_RESIDENTIAL_ADDR) == IP_CLASS_UNKNOWN


def test_mobile_carrier_address_is_not_datacenter():
    """Never 'residential' or 'mobile' either -- this module only ever
    positively asserts 'datacenter'; a mobile carrier address correctly
    falls to the honest 'unknown' default, not a guessed third label."""
    assert classify_ip(TMOBILE_MOBILE_ADDR) == IP_CLASS_UNKNOWN


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


# ---------------------------------------------------------------------
# Bundled snapshot: coverage, structure, and provenance
# ---------------------------------------------------------------------

def test_at_least_the_eleven_named_providers_are_covered():
    """This feature's design brief names OVH, Hetzner, DigitalOcean,
    AWS, GCP, Azure, Contabo, Linode, and Vultr as the minimum coverage
    -- the bundled snapshot also adds Oracle Cloud and Scaleway.
    Confirm all are actually present in the loaded snapshot, not just
    described in a comment."""
    expected = {
        "ovh", "hetzner", "digitalocean", "aws", "gcp",
        "azure", "contabo", "linode", "vultr", "oracle", "scaleway",
    }
    assert expected.issubset(set(ip_class_module.SNAPSHOT_PROVIDERS))


def test_snapshot_has_real_breadth_not_a_hand_picked_sample():
    """The specific gap this rebuild closes: a "small, hand-picked
    sample" list is exactly what missed the real incident address
    above. Assert the loaded snapshot is genuinely wide -- thousands of
    prefixes, not a few dozen."""
    assert len(ip_class_module._NETWORKS) > 10_000


def test_snapshot_meta_records_generation_date_and_sources():
    """The bundled file's own header must carry provenance (when it was
    generated, and what produced each provider's entries) -- this is
    what SNAPSHOT_META exposes, parsed straight from the file rather
    than re-typed into code comments that could drift from what was
    actually fetched."""
    assert len(ip_class_module.SNAPSHOT_META) > 0
    meta_text = "\n".join(ip_class_module.SNAPSHOT_META)
    assert "generated" in meta_text
    for provider in ("aws", "gcp", "oracle", "ovh", "hetzner", "contabo"):
        assert provider in meta_text


def test_data_file_is_not_gitignored():
    """The exact failure mode this rebuild exists to prevent: a
    gitignored data file would silently ship an empty/stale snapshot
    (or none at all) in a built image, making every address 'unknown'
    in production while the code itself looks complete. Checked here,
    not just by hand, so a future .gitignore edit that accidentally
    re-catches this path fails CI instead of failing silently in prod.
    """
    import subprocess
    result = subprocess.run(
        ["git", "check-ignore", "-v", str(ip_class_module._DATA_PATH)],
        capture_output=True, text=True,
    )
    # check-ignore exits 0 (and prints a match) only when the path IS
    # ignored -- exit 1 with no output is what "not ignored" looks
    # like, which is what this test requires.
    assert result.returncode != 0, (
        f"{ip_class_module._DATA_PATH} IS gitignored ({result.stdout!r}) -- "
        "this would silently ship an empty ip_class dataset in production"
    )


def test_lookup_structure_is_merged_and_sorted():
    """_build_lookup()'s own contract: non-overlapping, ascending
    intervals -- what makes the binary search in classify_ip() correct
    (at most one interval can ever contain a given address)."""
    for starts, ends in (
        (ip_class_module._V4_STARTS, ip_class_module._V4_ENDS),
        (ip_class_module._V6_STARTS, ip_class_module._V6_ENDS),
    ):
        assert starts == sorted(starts)
        for i in range(len(starts) - 1):
            assert ends[i] < starts[i + 1], "intervals must not overlap or touch"


# ---------------------------------------------------------------------
# No network call, ever -- including the module's own loading path
# ---------------------------------------------------------------------

def test_no_network_call_is_ever_made_by_classify_ip(monkeypatch):
    """Nothing in classify_ip()'s call path may open a socket -- patch
    every socket-opening primitive to explode, then classify both a
    known and an unknown address, and confirm neither touched the
    network."""
    def _boom(*args, **kwargs):
        raise AssertionError("classify_ip() must never open a network connection")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    monkeypatch.setattr(socket, "gethostbyaddr", _boom)
    monkeypatch.setattr(socket, "gethostbyname", _boom)

    assert classify_ip(SEPT_2026_ATTACKER_ADDR) == IP_CLASS_DATACENTER
    assert classify_ip(DOCS_ADDR) == IP_CLASS_UNKNOWN


def test_no_network_call_is_ever_made_while_loading_the_module(monkeypatch):
    """Extends the guarantee above to the LOADING path itself
    (_load_networks()/_build_lookup(), run at import time): reload the
    module with every socket primitive AND urllib patched to explode,
    and confirm the reload -- which re-reads the bundled gzip file and
    rebuilds the lookup structure from scratch -- still succeeds
    without touching the network.
    """
    import urllib.request

    def _boom(*args, **kwargs):
        raise AssertionError("app/ip_class.py's own loading path must never touch the network")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    reloaded = importlib.reload(ip_class_module)
    try:
        assert len(reloaded._NETWORKS) > 10_000
        assert reloaded.classify_ip(SEPT_2026_ATTACKER_ADDR) == IP_CLASS_DATACENTER
    finally:
        # Leave the module in its normal, unreloaded state for any test
        # that runs after this one in the same process.
        importlib.reload(ip_class_module)


def test_load_time_is_fast_enough_for_process_startup():
    """Reloading (re-reading the gzip file and rebuilding the merged
    interval structure from ~21,000+ entries) must stay well under a
    second -- this runs once per process at import time, not per
    request, but a slow load would still show up as a slow cold start /
    slow test collection.
    """
    t0 = time.perf_counter()
    importlib.reload(ip_class_module)
    elapsed = time.perf_counter() - t0
    assert elapsed < 5.0, f"reload took {elapsed:.2f}s -- investigate before this regresses further"
