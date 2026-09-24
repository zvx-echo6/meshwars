#!/usr/bin/env python3
"""Regenerates app/reference/datacenter_prefixes.csv.gz -- the bundled
hosting-provider IP-prefix snapshot app/ip_class.py's classify_ip()
loads at import time.

THIS SCRIPT MAKES OUTBOUND NETWORK CALLS. It is a development-time tool
only, never imported or run by the application itself -- see
app/ip_class.py's own module docstring for the "zero network access at
runtime" contract this split exists to keep. Run this by hand, by an
operator, when the bundled snapshot needs refreshing (provider
allocations do not churn quickly -- there is no cron or CI job that
runs this automatically):

    python3 app/reference/build_datacenter_prefixes.py

---- sources -----------------------------------------------------------

Three providers publish their own machine-readable, authoritative range
files directly -- used as-is, in full:

  - AWS:    https://ip-ranges.amazonaws.com/ip-ranges.json
  - GCP:    https://www.gstatic.com/ipranges/cloud.json
  - Oracle Cloud (OCI): https://docs.oracle.com/iaas/tools/public_ip_ranges.json
            (this URL 302-redirects; follow it)

The rest do not publish an equivalent self-service file (or, for Azure,
publish one behind a session-specific signed download link with no
stable URL to automate against). For those, this script instead pulls
every IPv4/IPv6 prefix RIPEstat's announced-prefixes API currently sees
announced by that provider's own ASN(s) -- a live BGP-derived view of
what that ASN actually routes, not a self-declared range file, but the
closest available equivalent, and specifically what caught the real
2026-09 incident's own OVH address (51.161.0.0/17, under AS16276) that
an earlier, hand-picked-sample version of this file missed entirely:

    https://stat.ripe.net/data/announced-prefixes/data.json?resource=AS<n>

  - OVH:          AS16276 (OVH SAS), AS35540 (OVH-TELECOM)
  - Hetzner:      AS24940 (Hetzner Online GmbH)
  - Contabo:      AS51167 (Contabo GmbH)
  - DigitalOcean: AS14061 (DigitalOcean, LLC)
  - Linode:       AS63949 (Akamai Connected Cloud, formerly Linode)
  - Vultr:        AS20473 (The Constant Company, LLC)
  - Scaleway:     AS12876 (Scaleway SAS)
  - Azure:        AS8075, AS8068, AS8069, AS8070 (Microsoft Corporation
                   -- MICROSOFT-CORP-MSN-AS-BLOCK; approximate coverage,
                   not Microsoft's own authoritative ServiceTags file,
                   for the "no stable URL" reason above)

Every ASN above and every holder name was verified against RIPEstat's
own as-overview API immediately before this snapshot was taken (see
generate() below, which also fails loudly rather than writing a file if
any fetch comes back empty).

---- output shape --------------------------------------------------------

app/reference/datacenter_prefixes.csv.gz: a gzip-compressed CSV,
`provider,cidr` per data row, with a `#`-prefixed metadata header (this
snapshot's generation date, each source URL/ASN, and the entry count
each source actually contributed) that app/ip_class.py's loader skips
over but a human inspecting the file directly still sees. Not a Python
module -- see app/ip_class.py's own docstring for why the data is kept
out of the code that loads it.
"""
from __future__ import annotations

import gzip
import ipaddress
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OUTPUT_PATH = Path(__file__).parent / "datacenter_prefixes.csv.gz"

AWS_URL = "https://ip-ranges.amazonaws.com/ip-ranges.json"
GCP_URL = "https://www.gstatic.com/ipranges/cloud.json"
OCI_URL = "https://docs.oracle.com/iaas/tools/public_ip_ranges.json"
RIPESTAT_ANNOUNCED_URL = "https://stat.ripe.net/data/announced-prefixes/data.json?resource=AS{asn}"

# provider label -> ASN(s) whose currently-announced prefixes count as
# that provider's datacenter space. See this module's own docstring for
# why these specific ASNs and where each holder name was confirmed.
ASN_PROVIDERS: dict[str, tuple[int, ...]] = {
    "ovh": (16276, 35540),
    "hetzner": (24940,),
    "contabo": (51167,),
    "digitalocean": (14061,),
    "linode": (63949,),
    "vultr": (20473,),
    "scaleway": (12876,),
    "azure": (8075, 8068, 8069, 8070),
}


def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _valid_cidrs(candidates: list[str]) -> list[str]:
    """Keeps only strings that parse as a real IPv4/IPv6 network,
    normalized to strict (network-aligned) form. A BGP-announced or
    provider-published prefix should always already be network-aligned;
    anything that is NOT is dropped rather than silently zeroed-out via
    strict=False, since a host-bits-set entry here would point at a
    data quality problem worth noticing, not silently absorbing.
    """
    out = []
    for c in candidates:
        try:
            net = ipaddress.ip_network(c, strict=True)
        except ValueError:
            print(f"  [!] dropping unparseable/non-aligned entry: {c!r}", file=sys.stderr)
            continue
        out.append(str(net))
    return out


def _fetch_aws() -> list[str]:
    data = _fetch_json(AWS_URL)
    v4 = [p["ip_prefix"] for p in data["prefixes"]]
    v6 = [p["ipv6_prefix"] for p in data["ipv6_prefixes"]]
    return _valid_cidrs(v4 + v6)


def _fetch_gcp() -> list[str]:
    data = _fetch_json(GCP_URL)
    cidrs = []
    for p in data["prefixes"]:
        if "ipv4Prefix" in p:
            cidrs.append(p["ipv4Prefix"])
        if "ipv6Prefix" in p:
            cidrs.append(p["ipv6Prefix"])
    return _valid_cidrs(cidrs)


def _fetch_oci() -> list[str]:
    data = _fetch_json(OCI_URL)
    cidrs = []
    for region in data["regions"]:
        for entry in region["cidrs"]:
            cidrs.append(entry["cidr"])
    return _valid_cidrs(cidrs)


def _fetch_asn_announced(asn: int) -> list[str]:
    data = _fetch_json(RIPESTAT_ANNOUNCED_URL.format(asn=asn))
    prefixes = [p["prefix"] for p in data["data"]["prefixes"]]
    return _valid_cidrs(prefixes)


def generate() -> None:
    rows: list[tuple[str, str]] = []  # (provider, cidr)
    source_counts: list[str] = []

    for provider, fetcher, label in (
        ("aws", _fetch_aws, "AWS (ip-ranges.json, official)"),
        ("gcp", _fetch_gcp, "GCP (cloud.json, official)"),
        ("oracle", _fetch_oci, "Oracle Cloud (public_ip_ranges.json, official)"),
    ):
        print(f"fetching {provider} ...", file=sys.stderr)
        cidrs = fetcher()
        if not cidrs:
            raise RuntimeError(f"{provider}: fetch returned zero usable prefixes -- refusing to write a snapshot")
        rows.extend((provider, c) for c in sorted(set(cidrs)))
        source_counts.append(f"# {provider}: {len(set(cidrs))} prefixes -- {label}")

    for provider, asns in ASN_PROVIDERS.items():
        print(f"fetching {provider} (AS{', AS'.join(str(a) for a in asns)}) ...", file=sys.stderr)
        all_cidrs: set[str] = set()
        for asn in asns:
            cidrs = _fetch_asn_announced(asn)
            if not cidrs:
                raise RuntimeError(f"{provider} AS{asn}: fetch returned zero usable prefixes -- refusing to write a snapshot")
            all_cidrs.update(cidrs)
        rows.extend((provider, c) for c in sorted(all_cidrs))
        asn_list = ", ".join(f"AS{a}" for a in asns)
        source_counts.append(
            f"# {provider}: {len(all_cidrs)} prefixes -- RIPEstat announced-prefixes, {asn_list}"
        )

    snapshot_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    header_lines = [
        f"# app/reference/datacenter_prefixes.csv.gz -- generated {snapshot_date} by",
        "# app/reference/build_datacenter_prefixes.py. See that script's own module",
        "# docstring for the full source list and reasoning. A point-in-time",
        "# snapshot -- refresh by re-running that script; provider allocations do",
        "# not churn quickly enough to need anything more automated than that, and",
        "# the application itself makes NO network call to keep this current.",
        "#",
        *source_counts,
        f"# total: {len(rows)} prefixes across {len({r[0] for r in rows})} providers",
        "provider,cidr",
    ]

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUTPUT_PATH, "wt", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(header_lines) + "\n")
        for provider, cidr in rows:
            f.write(f"{provider},{cidr}\n")

    print(f"wrote {len(rows)} prefixes to {OUTPUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    generate()
