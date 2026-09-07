#!/usr/bin/env python3
"""SUPERSEDED (2026-09-07) -- DO NOT WIRE IN, DO NOT DELETE YET.

This script's population-based radius formula (see FORMULA below) was
measured against 6,378 US cities with known Census areas (comparing
each city's formula-derived radius, fed that city's real population,
against its actual Census-derived radius in app/reference/places.csv):
R^2 = 0.214, median relative error 27%, p90 79%. Population alone does
not predict a city's physical extent well enough for this to be more
than a stopgap -- the Boise/SLC/Denver 3-city sanity check below looked
defensible, but 6,378 cities tell a different story than 3 do.

REPLACEMENT IN PROGRESS: a hybrid approach -- Census areas inside the
US (app/reference/places.csv, unchanged and unaffected by any of this),
OpenStreetMap administrative boundaries outside the US -- is being
built now to replace this file's non-US rows. It is not on disk yet.
Until it lands: this script and app/reference/places_global.csv stay
exactly as they are (neither is wired into merge()'s default -- see
"NOT wired in" below, still true), kept only as the last-resort
fallback, not deleted, not upgraded to a default.

Builds app/reference/places_global.csv -- worldwide city anchors for
scripts/build_places_seed.py's merge stage (score_points()'s in-city
test), keeping app/reference/places.csv's US Census anchors untouched
and filling in the rest of the world from GeoNames.

ADDED 2026-09-07 as part of the "Places Worth Going" worldwide
expansion (Matt approved) -- see build_places_seed.py's module
docstring "WORLDWIDE EXPANSION". Run standalone, then pass the result
to merge() explicitly:

    python3 scripts/build_global_anchors.py
    python3 scripts/build_places_seed.py merge <inputs...> \\
        --out app/reference/places_worth_going.csv.gz \\
        --places-csv app/reference/places_global.csv

NOT wired in as merge()'s new default -- app/reference/places.csv (US
Census only) is left untouched and is still the default, because it is
also read by app/places.py for the Frontier award, a feature this task
was not scoped to touch. Widening the Frontier anchor set worldwide is
a real, separate decision (more anchors globally would change how
Frontier scores everywhere, not just outside the US) that needs its
own sign-off, not a side effect of this script existing.

SOURCE: https://download.geonames.org/export/dump/cities500.zip
(settlements with population > 500, or an administrative seat
regardless of population -- GeoNames' own inclusion rule). Free, no
API key, tab-separated, documented at
https://download.geonames.org/export/dump/readme.txt.

FORMULA: GeoNames carries no land-area figure the way the Census
Gazetteer does (ALAND, which app/reference/places.csv's radius is
sqrt(.../pi) of). A population-based radius stands in instead:

    area_km2 = population / GLOBAL_DENSITY_PER_KM2
    radius_m = sqrt(area_km2 * 1e6 / pi), clamped to [MIN_RADIUS_M, MAX_RADIUS_M]

GLOBAL_DENSITY_PER_KM2 = 1500 was picked by checking it against the
three US cities that exist in both files -- Boise, Salt Lake City,
Denver -- comparing the formula's output (fed each city's own GeoNames
population) against that same city's REAL Census-derived radius in
app/reference/places.csv:

    Boise           pop 235,684   formula 7,072m   census 8,379m   -15.6%
    Salt Lake City  pop 215,548   formula 6,763m   census 9,561m   -29.3%
    Denver          pop 729,019   formula 12,438m  census 11,234m  +10.7%

(Denver's Census radius was matched by land-area cross-check -- its
official land area, 153.3 sq mi, gives sqrt(397 km^2/pi) = 11,239m,
confirming the file's own 11,234m anchor near it really is Denver's,
not a mismatch from picking the nearest anchor to the wrong point.
Salt Lake City's Census circle is unusually large for its population
because its real municipal limits annex a lot of non-residential
watershed/airport land -- looks like a formula miss but is a genuine
feature of that one city's boundary, not a bad density constant.)

A population heuristic cannot reproduce an actual municipal boundary
exactly and does not need to: it only feeds a binary in-city/remote
test (score_points() in build_places_seed.py), not a precise limit.
-15% to +11% against three real, if unrepresentative, comparisons is
treated as defensible rather than adjusted further -- picking a
density that fits Denver better would only widen the miss on Boise and
Salt Lake City, since the three real cities disagree with each other
on people-per-km^2 by 2.4x (738-1,805/km^2) more than any single
constant can track.

MIN_RADIUS_M is also the floor used for the ~13% of GeoNames rows with
population 0 or blank (GeoNames still lists these as real named
P-class settlements; a real place worth going should not become
un-anchored (radius 0) purely because GeoNames dropped that field).

MAX_RADIUS_M matches the largest radius already present in the US
Census file (rounded down slightly, from 25,316m) so a single
megacity's population figure cannot produce a circle bigger than
anything the existing file has ever needed -- swallowing several
neighbouring towns into one "in city limits" blob would be a worse
error than truncating one supercity's radius. Only cities above about
2.9M population reach this cap.
"""
from __future__ import annotations

import csv
import io
import math
import os
import sys
import urllib.request
import zipfile

GEONAMES_URL = "https://download.geonames.org/export/dump/cities500.zip"

_HERE = os.path.dirname(os.path.abspath(__file__))
US_PLACES_CSV = os.path.join(_HERE, "..", "app", "reference", "places.csv")
OUT_CSV = os.path.join(_HERE, "..", "app", "reference", "places_global.csv")

GLOBAL_DENSITY_PER_KM2 = 1500.0
MIN_RADIUS_M = 300.0
MAX_RADIUS_M = 25000.0


def radius_for_population(pop: float) -> float:
    if pop <= 0:
        return MIN_RADIUS_M
    area_km2 = pop / GLOBAL_DENSITY_PER_KM2
    r = math.sqrt(area_km2 * 1_000_000.0 / math.pi)
    return max(MIN_RADIUS_M, min(MAX_RADIUS_M, r))


def _fetch_geonames_rows():
    req = urllib.request.Request(GEONAMES_URL, headers={"User-Agent": "meshwars-places-seed/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        with zf.open("cities500.txt") as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8")
            yield from csv.reader(text, delimiter="\t")


def main(us_places_csv: str = US_PLACES_CSV, out_path: str = OUT_CSV) -> None:
    us_lines = []
    us_rows = 0
    with open(us_places_csv, encoding="utf-8") as f:
        for line in f:
            us_lines.append(line.rstrip("\n"))
            s = line.strip()
            if s and not s.startswith("#"):
                us_rows += 1

    geo_rows = []
    total_geo = 0
    us_skipped = 0
    non_p_skipped = 0
    zero_pop = 0
    for row in _fetch_geonames_rows():
        total_geo += 1
        if len(row) < 15:
            continue
        feature_class = row[6]
        country = row[8]
        if feature_class != "P":
            non_p_skipped += 1
            continue
        if country == "US":
            us_skipped += 1
            continue
        try:
            lat = float(row[4])
            lon = float(row[5])
        except ValueError:
            continue
        try:
            pop = float(row[14]) if row[14] else 0.0
        except ValueError:
            pop = 0.0
        if pop <= 0:
            zero_pop += 1
        geo_rows.append((lat, lon, radius_for_population(pop)))

    with open(out_path, "w", encoding="utf-8", newline="") as out:
        out.write("# SUPERSEDED (2026-09-07) -- non-US rows below are a population-based\n")
        out.write("# estimate measured at R^2=0.214, median relative error 27%, p90 79%\n")
        out.write("# against 6,378 real US Census city areas. Being replaced by a hybrid\n")
        out.write("# Census (US) + OpenStreetMap administrative boundary (non-US) file,\n")
        out.write("# not yet built. Not wired into merge()'s default. See this script's\n")
        out.write("# own module docstring.\n")
        out.write("# lat,lon,effective_radius_m -- worldwide city anchors for\n")
        out.write("# score_points()'s in-city test (docs/features/places.md). US rows are\n")
        out.write("# the untouched original app/reference/places.csv (US Census 2024\n")
        out.write("# Gazetteer places, radius = sqrt(ALAND/pi)). Non-US rows are derived\n")
        out.write("# from GeoNames cities500 (download.geonames.org), radius estimated from\n")
        out.write("# population (GeoNames carries no land-area figure) at an assumed\n")
        out.write(f"# {GLOBAL_DENSITY_PER_KM2:.0f} people/km^2, clamped to "
                   f"[{MIN_RADIUS_M:.0f}m, {MAX_RADIUS_M:.0f}m] -- see\n")
        out.write("# scripts/build_global_anchors.py for the derivation and the\n")
        out.write("# Boise/SLC/Denver sanity check against the real US Census circles.\n")
        for line in us_lines:
            if not line.startswith("#"):
                out.write(line + "\n")
        for lat, lon, radius in geo_rows:
            out.write(f"{lat},{lon},{radius:.0f}\n")

    print(f"anchors: US Census anchors kept verbatim: {us_rows}", file=sys.stderr)
    print(f"anchors: GeoNames rows read: {total_geo} "
          f"(non-P skipped {non_p_skipped}, US skipped {us_skipped} -- "
          f"Census anchor used instead, zero/blank population floored to "
          f"{MIN_RADIUS_M:.0f}m: {zero_pop})", file=sys.stderr)
    print(f"anchors: non-US GeoNames anchors added: {len(geo_rows)}", file=sys.stderr)
    print(f"anchors: TOTAL before={us_rows} after={us_rows + len(geo_rows)} -> {out_path}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
