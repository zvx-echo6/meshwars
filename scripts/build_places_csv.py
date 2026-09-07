#!/usr/bin/env python3
"""Builds app/reference/places.csv -- the Census anchors app/places.py
tests "how far to the nearest town" against (and, through that same
file, scripts/build_places_seed.py's in-city-limits scoring). See
app/places.py's own module docstring for what the file is used for and
why a flat circle-of-equal-area file exists instead of real polygons.

This is a two-stage pipeline, same shape as build_places_seed.py:

  1. fetch-place -- anywhere with internet. Pulls the US Census 2024
                     Gazetteer PLACE file (incorporated cities/towns and
                     Census Designated Places) and filters it to the 50
                     states + DC.
  2. fetch-ua    -- anywhere with internet. Pulls the same year's
                     Gazetteer URBAN AREA file and filters it the same
                     way -- see "WHY URBAN AREAS" below for why place
                     rows alone are not enough.
  3. build       -- anywhere. Combines both stage outputs into the
                     final app/reference/places.csv, in its existing
                     lat,lon,effective_radius_m shape.

SOURCES (2024 vintage, matching the file's existing header; pulled
2026-09-07):
  Places       -- https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_place_national.zip
  Urban Areas  -- https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_ua_national.zip
  Both are tab-delimited, UTF-8, one national file each -- no per-state
  splitting needed (the per-state *.txt links on the same directory
  listing are a convenience the national zip already contains).

SCOPE (changed 2026-09-07, "the play-area check is DISABLED in
production... build the anchor set over the ENTIRE UNITED STATES"):
this used to filter both files to the MeshWars play area bbox
(-125.0 to -93.5 W) plus a 1-degree margin, matching a play-area check
that existed elsewhere in the app at the time. That check is gone and
the board is world-open, so the bbox is no longer the right boundary --
this now keeps every place/urban-area row in the 50 states + DC and
nothing else. The PLACE file's USPS column makes this direct (keep the
row iff USPS is a state or "DC"); the UA file has no USPS column, so
US_STATE_FILTER extracts the state code(s) already embedded in each
Urban Area's own NAME (e.g. "Washington--Arlington, DC--VA--MD Urban
Area") and keeps the row if ANY of them is one of the 50 states/DC --
see _ua_state_codes.

OUT OF SCOPE, DELIBERATELY: the Gazetteer PLACE file also carries
Puerto Rico (USPS "PR"); the UA file separately carries PR, Guam (GU),
the US Virgin Islands (VI), American Samoa (AS), and the Northern
Mariana Islands (MP). All are excluded here -- "50 states plus DC" was
the explicit ask, and territories/insular areas are a separate scope
decision nobody has made. Non-US anchors are further out of scope
still: the Census Gazetteer is a US-only source, so going international
would need an entirely different data source, not a wider filter on
this one.

FILTER: US_STATE_FILTER (the 50 states + DC) applied to every row's
own state code(s), as above. No FUNCSTAT or place-type filter is
applied to the PLACE file: statistical entities (Census Designated
Places -- unincorporated towns with no government of their own) are
kept right alongside incorporated cities, because they are frequently
the anchor that actually matters (see Winchester/Paradise below).

RADIUS: sqrt(ALAND / pi) for every row, from whichever file it came
from -- a circle with the same land area the Census records for that
place or urban area. Same formula the file has always used; this
pipeline only adds a second row source, not a second formula.

WHY THIS SCRIPT EXISTS: app/reference/places.csv was committed ad hoc
(7cd1eb1, 2026-08-23) with no generator in the repo -- the pipeline
below is a from-scratch reconstruction. At its original, play-area-
-bbox-filtered PLACE-only stage it reproduced that commit's 13,024
rows byte-for-byte (verified as a set: zero added, zero removed) --
there was no missing-row bug in the original generation, and going
national did not change that conclusion (see below).

WHY URBAN AREAS -- THE ACTUAL BUG THIS FIXES: Matt found San Francisco
and Las Vegas both reading as "remote" despite sitting in the middle of
a metro area. The place-only file was NOT missing any Census row for
either city -- "San Francisco city" and "Las Vegas city" (and every
small town around Las Vegas: Winchester, Paradise, Enterprise, Sunrise
Manor, Whitney CDPs) were already anchors in it. The failure is a
limitation of the single-interior-point-plus-equal-area-circle model
itself, and it has two distinct, unrelated causes:

  - San Francisco is a consolidated city-county whose corporate limits
    legally include the Farallon Islands, a non-contiguous exclave
    about 48km/30mi offshore. Census computes ONE interior point for
    that whole multi-part legal shape, and it lands at
    (37.727239, -123.032229) -- on/near the Farallones, not the
    populated mainland. A circle drawn there (radius ~6.2km from
    ALAND=120,913,549 sqm, which includes the mainland's land area)
    is centred over open ocean and reaches nowhere near downtown SF.
    The nearest anchor that actually helps is Daly City, 9km away with
    only a 2.5km radius -- nowhere close enough.
  - Las Vegas has no exclave problem, but the Strip/downtown tourist
    core that most people mean by "Las Vegas" is NOT inside the City
    of Las Vegas at all -- it is unincorporated Clark County, split
    across several small Census Designated Places (Winchester,
    Paradise, ...). "Las Vegas city"'s own interior point
    (36.233499, -115.264037) sits northwest of the Strip and its
    circle (radius ~10.8km, one of the largest place-level radii in
    the country) still comes up ~2.4km short of it. The nearest CDP,
    Winchester, is only 3.7km from the Strip but its own land area is
    small (radius ~2.0km) -- also short. Every relevant place-level
    anchor exists; none of their individual circles happens to bridge
    the gap over that specific point.

Neither cause is specific to the western play area, and checking the
22 additional eastern cities added to the coverage test alongside the
original 25 (New York, Chicago, Atlanta, Miami, Boston, Philadelphia,
Detroit, Washington DC, Nashville, Charlotte, Indianapolis, Columbus,
Milwaukee, Memphis, New Orleans, Tampa, Orlando, Cleveland, Pittsburgh,
St. Louis, Cincinnati, Minneapolis) against the place-only file (no
urban-area rows) found it recurs, twice more, in two further variants:

  - New Orleans city is coterminous with Orleans Parish, whose legal
    limits reach north across a wide swath of Lake Pontchartrain --
    AWATER (467,102,619 sqm) is nearly as large as ALAND
    (439,001,672 sqm), roughly half water. That drags the one Census
    interior point (30.053420, -89.934502) about 17km northeast of
    the French Quarter/CBD, with a radius (11.8km) that still comes
    up ~3.0km short. Same mechanism as San Francisco -- a huge
    in-boundary water body skewing the interior point away from the
    populated core -- just contiguous water instead of a
    non-contiguous exclave.
  - Orlando city has annexed aggressively for decades, including large
    parcels far southeast of downtown (toward the airport and Lake
    Nona) that are still part of one legal shape connected by thin
    corridor annexations. Its interior point (28.408650, -81.254805)
    sits about 19km southeast of downtown -- roughly at those
    annexations, not the historic core -- and its 9.6km radius still
    comes up ~9.3km short of downtown. A third variant of the same
    underlying problem: an irregular, non-compact jurisdiction shape
    whose Census interior point tracks its full legal extent rather
    than "the city" a person means by the name.

The other 20 eastern cities are already covered by a place-level
anchor (own city, or a same-metro neighbour) with no urban-area row
needed. That an unrelated fourth case (Orlando) turned up in a list of
only 47 spot-checked cities, via a third distinct mechanism, is reason
to expect more of these exist among the thousands of incorporated
places this file was never able to individually check -- which is
exactly why the fix is a second, general row source (urban areas) and
not a per-city patch list.

Both failures share one property: they are jurisdiction-boundary
artifacts, not population artifacts -- the built-up area is
continuous, only the legal lines through it are ragged. The Census
Gazetteer URBAN AREA file describes exactly that: a contiguous
built-up footprint drawn from population density, with no regard for
city, county, or incorporation lines. Adding one urban-area row per
metro (in addition to, never instead of, the place rows -- small towns
with no urban area of their own still need their own anchor) closes
every one of these gaps with a single general rule instead of a
per-city patch list: "San Francisco--Oakland, CA Urban Area" is
centred at (37.784954, -122.270088) with a ~20.6km radius (comfortably
reaching downtown SF, 13.2km away); "Las Vegas--Henderson--Paradise, NV
Urban Area" at (36.134165, -115.159181) with a ~18.9km radius
(reaching the Strip, 4.3km away); "New Orleans, LA Urban Area" and
"Orlando, FL Urban Area" close their respective gaps the same way.
None of the four are special-cased anywhere in this file -- the fix is
the row source, not the city. Verified all 25 western + 22 eastern
cities in test_places_anchors.py pass, most by a wide margin.
"""
from __future__ import annotations

import argparse
import csv
import io
import math
import os
import re
import sys
import urllib.request
import zipfile

# The 50 states + DC -- see module docstring "SCOPE" and "OUT OF SCOPE,
# DELIBERATELY" for why territories (PR, GU, VI, AS, MP) are excluded
# even though the source files carry them.
US_STATE_FILTER = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
})

PLACE_URL = ("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
             "2024_Gazetteer/2024_Gaz_place_national.zip")
UA_URL = ("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
          "2024_Gazetteer/2024_Gaz_ua_national.zip")

_DEFAULT_PLACES_CSV = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "app", "reference", "places.csv"
)

# Matches the state-code list an Urban Area's own NAME already carries,
# e.g. "Washington--Arlington, DC--VA--MD Urban Area" -> "DC--VA--MD",
# or "Las Vegas--Henderson--Paradise, NV Urban Area" -> "NV". The UA
# Gazetteer file has no USPS column of its own (unlike PLACE), so this
# is the only place that information lives.
_UA_STATE_CODES_RE = re.compile(r",\s*([A-Z]{2}(?:--[A-Z]{2})*)\s+Urban Area\s*$")


def _ua_state_codes(name: str) -> list[str]:
    m = _UA_STATE_CODES_RE.search(name)
    if not m:
        raise ValueError(f"could not find a state-code list in UA name: {name!r}")
    return m.group(1).split("--")


def _download_zip_member(url: str) -> str:
    """Downloads a Census Gazetteer zip and returns the decoded text of
    its one member (both PLACE_URL and UA_URL zips contain exactly one
    file). Gazetteer files are UTF-8 despite the .gov host not always
    saying so in headers -- confirmed by decoding the full PLACE file
    (which contains Puerto Rico place names with accented characters)
    without error before this was written."""
    req = urllib.request.Request(url, headers={"User-Agent": "meshwars-places-anchors/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = zf.namelist()
        if len(names) != 1:
            raise ValueError(f"expected exactly one file in {url}, got {names}")
        return zf.read(names[0]).decode("utf-8")


def _gazetteer_rows(text: str) -> list[dict]:
    """Parses a tab-delimited Gazetteer file (PLACE or UA shape both
    work -- both are tab-separated with a header row) into dicts,
    stripping the fixed-width trailing padding every column and the
    header carry."""
    lines = text.split("\n")
    header = [h.strip() for h in lines[0].rstrip("\r").split("\t")]
    rows = []
    for line in lines[1:]:
        line = line.rstrip("\r\n")
        if not line:
            continue
        parts = [p.strip() for p in line.split("\t")]
        rows.append(dict(zip(header, parts)))
    return rows


def _effective_radius_m(aland_sqm: float) -> float:
    return math.sqrt(aland_sqm / math.pi)


# --------------------------------------------------------------------
# Stage 1: PLACE file -- incorporated cities/towns and CDPs
# --------------------------------------------------------------------
def fetch_place(out_path: str) -> None:
    text = _download_zip_member(PLACE_URL)
    rows = _gazetteer_rows(text)
    kept = 0
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["lat", "lon", "radius_m", "name", "usps", "funcstat"])
        for r in rows:
            if r["USPS"] not in US_STATE_FILTER:
                continue
            lat = float(r["INTPTLAT"])
            lon = float(r["INTPTLONG"])
            radius = _effective_radius_m(float(r["ALAND"]))
            w.writerow([f"{lat:.6f}", f"{lon:.6f}", f"{radius:.4f}",
                        r["NAME"], r["USPS"], r["FUNCSTAT"]])
            kept += 1
    print(f"fetch-place: {len(rows)} places nationwide (all US states + "
          f"territories), {kept} in the 50 states + DC -> {out_path}", file=sys.stderr)


# --------------------------------------------------------------------
# Stage 2: URBAN AREA file -- contiguous built-up footprints, no
# regard for jurisdiction lines. See module docstring "WHY URBAN
# AREAS" for why place rows alone leave gaps this closes.
# --------------------------------------------------------------------
def fetch_ua(out_path: str) -> None:
    text = _download_zip_member(UA_URL)
    rows = _gazetteer_rows(text)
    kept = 0
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["lat", "lon", "radius_m", "name"])
        for r in rows:
            if not any(code in US_STATE_FILTER for code in _ua_state_codes(r["NAME"])):
                continue
            lat = float(r["INTPTLAT"])
            lon = float(r["INTPTLONG"])
            radius = _effective_radius_m(float(r["ALAND"]))
            w.writerow([f"{lat:.6f}", f"{lon:.6f}", f"{radius:.4f}", r["NAME"]])
            kept += 1
    print(f"fetch-ua: {len(rows)} urban areas nationwide (all US states + "
          f"territories), {kept} touching the 50 states + DC -> {out_path}", file=sys.stderr)


# --------------------------------------------------------------------
# Stage 3: build -- combine both stage outputs into the final file
# --------------------------------------------------------------------
def build(place_csv: str, ua_csv: str, out_path: str) -> None:
    anchors: list[tuple[float, float, float]] = []
    for path in (place_csv, ua_csv):
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                anchors.append((float(row["lat"]), float(row["lon"]), float(row["radius_m"])))

    anchors.sort(key=lambda a: (a[0], a[1]))

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        fh.write(
            "# lat,lon,effective_radius_m -- US Census 2024 Gazetteer places and\n"
            "# urban areas, covering the 50 states plus DC (no play-area or bbox\n"
            "# filter -- that check is disabled in production; see the 'SCOPE'\n"
            "# section of scripts/build_places_csv.py's module docstring). Radius\n"
            "# is sqrt(ALAND/pi): a circle of the same land area as the place or\n"
            f"# urban area, which stands in for its limits. {len(anchors):,} rows, built by\n"
            "# scripts/build_places_csv.py (fetch-place + fetch-ua + build).\n"
        )
        for lat, lon, radius in anchors:
            fh.write(f"{round(lat, 4)},{round(lon, 4)},{round(radius)}\n")

    print(f"build: {len(anchors)} anchors ({place_csv} + {ua_csv}) -> {out_path}",
          file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch-place")
    p.add_argument("out")

    p = sub.add_parser("fetch-ua")
    p.add_argument("out")

    p = sub.add_parser("build")
    p.add_argument("place_csv")
    p.add_argument("ua_csv")
    p.add_argument("--out", default=_DEFAULT_PLACES_CSV)

    args = ap.parse_args()
    if args.cmd == "fetch-place":
        fetch_place(args.out)
    elif args.cmd == "fetch-ua":
        fetch_ua(args.out)
    elif args.cmd == "build":
        build(args.place_csv, args.ua_csv, args.out)


if __name__ == "__main__":
    main()
