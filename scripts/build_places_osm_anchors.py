#!/usr/bin/env python3
"""Builds the worldwide (non-US) side of app/reference/places.csv from an
OpenStreetMap planet extract, and merges it with the existing US Census
side into the final hybrid file. This is the replacement
scripts/build_global_anchors.py's own docstring said was "in progress" --
it lands directly in app/reference/places.csv (not a separate
places_global.csv) so app/places.py's Frontier award and
build_places_seed.py's score_points() in-city test both pick it up with
no code changes, matching that file's existing "one file the loader
reads" shape.

DECIDED APPROACH (Matt, worldwide-expansion task, 2026-09-07): Census
inside the US, OpenStreetMap outside it. Not open for revisiting -- OSM
alone is far more accurate per-match where it exists (R^2 0.726, median
error 2.1%, measured against the same 11-state sample used to validate
the classification rule below) but covers only ~40% of US places and has
NOTHING for cases like the Las Vegas Strip (Paradise, NV exists in OSM
only as a bare point, not a boundary, so it cannot become an anchor under
this script's rule either). A straight OSM swap inside the US measured a
35% disagreement rate against Census -- a regression. So: Census stays
authoritative inside the US; OSM fills in literally everywhere else.

This is a FOUR-STAGE pipeline, same shape as build_places_csv.py:

  1. classify           -- on navi (zvx@100.64.0.27), which holds the
                            planet extraction (already run once, ~77
                            minutes -- see extract-boundaries below; DO
                            NOT re-run it for an ordinary rebuild).
                            Reads admin_boundaries.geojsonseq (polygons)
                            + place_points.csv (points) and classifies
                            each polygon as a populated place or not.
  2. dedup               -- anywhere. Collapses duplicate/fragment
                            objects sharing a name.
  3. extract-us-boundary -- on navi (needs admin_boundaries.geojsonseq
                            again, but only greps one line out of it --
                            seconds, not minutes).
  4. merge               -- anywhere. Drops the OSM anchors that fall
                            inside that US boundary (Census stays sole
                            authority there) and appends the rest to the
                            existing Census rows.

PREREQUISITE (not part of this script, already run once): a separate
planet-wide osmium export + classify.py pass on navi produced the two
inputs `classify` reads --
  admin_boundaries.geojsonseq -- every boundary=administrative OR
      place=city|town|village polygon/multipolygon worldwide (907,244
      rows, ~10GB), raw admin_level passed through unmodified.
  place_points.csv -- every place=city|town|village|hamlet POINT
      (3,695,265 rows): osm_type,osm_id,name,place,lat,lon,population.
Both sat at /tmp/places_global/ on navi for this build; regenerating
them is a ~77-minute osmium/python pass over a planet PBF and is out of
this script's scope.

CLASSIFICATION RULE (validated on an 11-state US sample): a polygon is a
populated place if EITHER
  (a) it carries place=city|town|village directly, OR
  (b) it is boundary=administrative AND a place=city|town|village|hamlet
      NODE with the same normalized name (normalize(): NFKD-strip
      diacritics, lowercase, Unicode-aware -- NOT ascii-only, which
      silently dropped ~1M non-Latin-script points -- e.g. Cyrillic/CJK/
      Arabic/Devanagari -- from the match index the first time this was
      written) lies inside it.
NOT filtered on admin_level as the primary test: it is inconsistent
worldwide (non-numeric values exist in the wild -- "Village", "suburb",
"Desa", "RW/8" -- and San Francisco is level 6 rather than 8 because it
is a consolidated city-county).

ONE ADAPTATION was needed beyond that rule, found by inspecting the
result rather than assumed up front: a bare admin_level<=4 exclusion for
the name-match path was tried first (country/state/province objects
should never equal one settlement) and it worked for the failure mode it
targeted -- Sao Paulo STATE matching via Sao Paulo city (radius would
have been 293km), Mexico and Panama the COUNTRIES matching via a
village/their own capital sharing that name (847km, 230km), New York
STATE matching via New York city (212km) -- measured as EVERY ONE of 150
rows >100km radius at level<=4 in the full planet run. But it also
silently dropped Berlin and Hamburg entirely: German federal city-states
have no OSM boundary below country/state level at all, so admin_level=4
is their ONLY polygon, and a blanket level cut has no way to tell a real
city-state from a state that merely contains a same-named city. Size is
the actual signal, not level: swapped to "level<=4 AND radius>100km",
which keeps every real city-state (Berlin's ~17km radius is nowhere near
the cutoff) while still rejecting every measured false positive (all 150
were >100km; no real single settlement reaches that size at any
admin_level -- the genuinely huge-but-real level 5+ jurisdictions this
data also has, Amazon municipalities like Altamira, Chinese prefecture-
level cities like Harbin, Australian outback shires like Birdsville, are
all named after and governed from their own seat settlement, not a
different, larger, coincidentally-same-named entity).

DEDUP IS MANDATORY at planet scale: 47 objects named exactly "Commerce
City" (46 parking-lot-sized fragments, 0.004-0.05 km2, plus one real
94.7 km2 relation, all independently tagged boundary=administrative
place=city) and 785 objects tagged as countries against ~195 real ones.
dedup_anchors() groups by normalized name and keeps only the largest by
area among candidates within ~50km of each other -- a GREEDY largest-
first pass (sort by area descending; a candidate is absorbed only if it
falls within 50km of an ALREADY-KEPT, hence larger-or-equal, anchor),
not a transitive union-find, specifically so real same-named cities on
different continents thousands of km apart (Springfield, San Jose,
Victoria, Newcastle) never chain together into one incorrectly-merged
anchor. BLANK NAMES (no `name` tag at all -- direct place=village
polygons are common with no name worldwide, ~5% of the pre-dedup total)
are excluded from name-grouping entirely and kept one-for-one: grouping
them would collapse every unnamed place on Earth into a single bucket
and silently delete thousands of real villages across Africa, South
Asia, and anywhere else OSM naming is thinner.

US EXCLUSION: real point-in-polygon containment against OSM's own
"United States" country relation (osm relation 148838, admin_level=2,
name=="United States" exactly -- not any of the "United States of
America (...)" parenthetical territory relations alongside it in the
same extraction, which are Puerto Rico/Guam/CNMI/American Samoa/Minor
Outlying Islands and are correctly OUTSIDE this polygon, so OSM anchors
there are KEPT, matching Census's own 50-states-plus-DC scope). A first
attempt used three lat/lon bounding boxes (CONUS/Alaska/Hawaii) instead
and measured wrong: Toronto (43.65N, -79.38W) sits inside a CONUS-shaped
box spanning lat 24.4-49.4 / lon -125..-66.93, because the US-Canada
border is not a rectangle -- it follows the 49th parallel only west of
Minnesota, and Canada's populated strip east of that (Toronto, Ottawa,
Montreal) sits well south of 49N. The bbox silently dropped Toronto's
real OSM anchor. Real polygon containment has no such failure mode.

KNOWN BIAS, ACCEPTED NOT FIXED: OSM city polygons include water and
exclaves, so coastal/island cities read larger than their land area
justifies -- Istanbul Province's OSM polygon (radius 59.9km, implying
~11,270 km2) against its official land area (~5,461 km2) is about 2x
inflated by the Bosphorus/Sea of Marmara; Auckland's supercity anchor
(radius 71.7km, ~16,150 km2) against its official council land area
(~4,941 km2) is about 3x, from harbour and Hauraki Gulf islands folded
into one regional authority. San Francisco's Census-side equivalent
problem (its interior point sits on the Farallon Islands exclave) is
the reason app/places.py needed supplementary Census urban-area anchors
in the first place -- see that module's docstring and
scripts/build_places_csv.py's "WHY URBAN AREAS". No land/water mask
exists to correct this for OSM at planet scale; it is accepted the same
way the existing Census file already accepts "a long thin city reads
rounder than it is" (app/places.py's own docstring).

Radius: sqrt(area/pi) on a Lambert cylindrical equal-area projection
(standard parallel at the equator, IUGG mean radius 6,371,008.8m) --
same formula/constant as classify.py's own park-area calculation,
validated there against Yellowstone to within 1%; exact everywhere on
the sphere, not an approximation valid only near one parallel. Anchor
point: shapely representative_point() (guaranteed inside the polygon,
unlike a plain centroid which can land outside for concave/exclave
shapes).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time
import unicodedata

_HERE = os.path.dirname(os.path.abspath(__file__))
CENSUS_PLACES_CSV = os.path.join(_HERE, "..", "app", "reference", "places.csv")

# navi-side default paths (the planet extraction lives only there).
DEFAULT_ADMIN_BOUNDARIES = "/tmp/places_global/admin_boundaries.geojsonseq"
DEFAULT_PLACE_POINTS = "/tmp/places_global/place_points.csv"

R_EARTH = 6371008.8  # IUGG mean/authalic radius, metres.

PLACE_DIRECT = {"city", "town", "village"}
PLACE_NODE_VALUES = {"city", "town", "village", "hamlet"}
LOW_ADMIN_LEVEL_MAX = 4
OVERSIZED_RADIUS_M = 100_000.0
DEDUP_RADIUS_KM = 50.0
US_RELATION_OSM_ID = 148838
US_RELATION_NAME = "United States"

RAW_FIELDS = ["norm_name", "name", "lat", "lon", "radius_m", "area_m2",
              "osm_type", "osm_id", "place_tag", "boundary", "admin_level", "classify_reason"]


def normalize(name):
    """NFKD-strip diacritics, lowercase, then keep any Unicode word
    character (not ascii a-z0-9 only -- an ascii-only cut silently
    dropped ~1M non-Latin-script place-node names, Cyrillic/CJK/Arabic/
    Devanagari among them, from the match index the first time this was
    written, which would have made the name-match rule blind to most of
    Russia/China/Japan/Korea/the Middle East/South Asia)."""
    if not name:
        return None
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower()
    n = re.sub(r"[^\w\s]", " ", n, flags=re.UNICODE)
    n = re.sub(r"\s+", " ", n).strip()
    return n or None


def equal_area_m2(geom):
    def proj(lon, lat):
        return (R_EARTH * math.radians(lon), R_EARTH * math.sin(math.radians(lat)))

    def ring_area(ring):
        pts = [proj(lon, lat) for lon, lat in ring]
        s = 0.0
        for i in range(len(pts) - 1):
            x1, y1 = pts[i]
            x2, y2 = pts[i + 1]
            s += x1 * y2 - x2 * y1
        return abs(s) / 2.0

    gtype = geom["type"]
    coords = geom["coordinates"]
    if gtype == "Polygon":
        polys = [coords]
    elif gtype == "MultiPolygon":
        polys = coords
    else:
        return None
    total = 0.0
    for poly in polys:
        if not poly:
            continue
        total += ring_area(poly[0])
        for hole in poly[1:]:
            total -= ring_area(hole)
    return total


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a)))


# --------------------------------------------------------------------
# Stage 1: classify -- run on navi (needs the planet extraction).
# --------------------------------------------------------------------
def classify(admin_path: str, points_path: str, out_path: str) -> None:
    from shapely.geometry import shape, Point
    from shapely.prepared import prep

    t0 = time.time()
    print("loading place_points.csv name index...", file=sys.stderr)
    name_index: dict[str, list[tuple[float, float]]] = {}
    n_pts = 0
    with open(points_path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        next(r)  # header
        for row in r:
            if len(row) != 7:
                continue
            osm_type, osm_id, name, place, lat, lon, population = row
            if place not in PLACE_NODE_VALUES:
                continue
            norm = normalize(name)
            if not norm:
                continue
            try:
                name_index.setdefault(norm, []).append((float(lat), float(lon)))
                n_pts += 1
            except ValueError:
                continue
    print(f"  {n_pts:,} place-node points indexed, {len(name_index):,} distinct "
          f"normalized names ({time.time()-t0:.0f}s)", file=sys.stderr)

    counts = dict(n_in=0, n_props_only_skip=0, n_full_parse=0, n_direct=0, n_name_match=0,
                  n_name_match_fail=0, n_area_fail=0, n_no_name=0, n_oversized_blocked=0)

    marker = '"properties":'
    with open(out_path, "w", newline="", encoding="utf-8") as fout, \
            open(admin_path, "r", encoding="utf-8") as fh:
        w = csv.writer(fout)
        w.writerow(RAW_FIELDS)
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            counts["n_in"] += 1
            idx = line.rfind(marker)
            if idx == -1:
                continue
            props_str = line[idx + len(marker):].strip()
            if props_str.endswith("}"):
                props_str = props_str[:-1]
            try:
                props = json.loads(props_str)
            except json.JSONDecodeError:
                continue

            name = props.get("name") or ""
            place = props.get("place")
            boundary = props.get("boundary")
            osm_type = props.get("osm_type")
            osm_id = props.get("osm_id")
            admin_level = props.get("admin_level")

            direct = place in PLACE_DIRECT
            norm = normalize(name)
            if not name:
                counts["n_no_name"] += 1

            try:
                admin_level_num = int(admin_level) if admin_level is not None else None
            except (TypeError, ValueError):
                admin_level_num = None
            is_low_level = admin_level_num is not None and admin_level_num <= LOW_ADMIN_LEVEL_MAX

            candidates = name_index.get(norm) if norm else None
            name_match_possible = (not direct) and boundary == "administrative" and candidates
            if not direct and not name_match_possible:
                counts["n_props_only_skip"] += 1
                continue

            try:
                feat = json.loads(line)
            except json.JSONDecodeError:
                continue
            counts["n_full_parse"] += 1
            geom = feat.get("geometry")
            if geom is None:
                continue

            if direct:
                reason = "direct"
            else:
                try:
                    poly = shape(geom)
                    if not poly.is_valid:
                        poly = poly.buffer(0)
                    prepared = prep(poly)
                except Exception:
                    counts["n_name_match_fail"] += 1
                    continue
                if not any(prepared.contains(Point(clon, clat)) for clat, clon in candidates):
                    counts["n_name_match_fail"] += 1
                    continue
                reason = "name_match"

            area = equal_area_m2(geom)
            if area is None or area <= 0:
                counts["n_area_fail"] += 1
                continue
            radius = math.sqrt(area / math.pi)

            # See module docstring's "ONE ADAPTATION" for why this is
            # level+size, not level alone.
            if reason == "name_match" and is_low_level and radius > OVERSIZED_RADIUS_M:
                counts["n_oversized_blocked"] += 1
                continue

            try:
                poly2 = shape(geom)
                if not poly2.is_valid:
                    poly2 = poly2.buffer(0)
                rp = poly2.representative_point()
                out_lat, out_lon = rp.y, rp.x
            except Exception:
                counts["n_area_fail"] += 1
                continue

            counts["n_direct" if reason == "direct" else "n_name_match"] += 1
            w.writerow([norm, name, f"{out_lat:.6f}", f"{out_lon:.6f}", f"{radius:.1f}",
                        f"{area:.1f}", osm_type, osm_id, place or "", boundary or "",
                        admin_level if admin_level is not None else "", reason])

            if counts["n_full_parse"] % 20000 == 0:
                print(f"  ...{counts['n_in']:,} read, {counts['n_full_parse']:,} full-parsed, "
                      f"{counts['n_direct']:,} direct + {counts['n_name_match']:,} name_match kept "
                      f"({time.time()-t0:.0f}s)", file=sys.stderr)

    print("=== classify DONE ===", file=sys.stderr)
    for k, v in counts.items():
        print(f"  {k}: {v:,}", file=sys.stderr)
    print(f"  TOTAL KEPT (pre-dedup): {counts['n_direct'] + counts['n_name_match']:,}", file=sys.stderr)
    print(f"  elapsed: {time.time()-t0:.0f}s", file=sys.stderr)


# --------------------------------------------------------------------
# Stage 2: dedup -- anywhere.
# --------------------------------------------------------------------
def _dedup_group(rows):
    """Greedy largest-first, NOT transitive union-find over the 50km
    threshold -- see module docstring for why (real same-named cities on
    different continents must never chain together)."""
    ordered = sorted(rows, key=lambda r: -r["area_m2"])
    kept = []
    for r in ordered:
        if not any(haversine_km(r["lat"], r["lon"], k["lat"], k["lon"]) <= DEDUP_RADIUS_KM
                   for k in kept):
            kept.append(r)
    return kept


def dedup(raw_path: str, out_path: str) -> None:
    groups: dict[str, list[dict]] = {}
    blank = []
    n_in = 0
    with open(raw_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            n_in += 1
            row["lat"] = float(row["lat"])
            row["lon"] = float(row["lon"])
            row["area_m2"] = float(row["area_m2"])
            row["radius_m"] = float(row["radius_m"])
            if not row["norm_name"]:
                blank.append(row)
                continue
            groups.setdefault(row["norm_name"], []).append(row)

    kept_named = []
    n_removed = 0
    for rows in groups.values():
        k = _dedup_group(rows)
        n_removed += len(rows) - len(k)
        kept_named.extend(k)
    final_rows = kept_named + blank

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(RAW_FIELDS[:-1] + ["classify_reason"])  # admin_level not needed downstream
        for row in final_rows:
            w.writerow([row["norm_name"], row["name"], f"{row['lat']:.6f}", f"{row['lon']:.6f}",
                        f"{row['radius_m']:.1f}", f"{row['area_m2']:.1f}", row["osm_type"],
                        row["osm_id"], row["place_tag"], row["boundary"], row["classify_reason"]])

    print(f"input rows: {n_in:,}", file=sys.stderr)
    print(f"blank-name rows (excluded from grouping, kept one-for-one): {len(blank):,}", file=sys.stderr)
    print(f"named rows: {n_in - len(blank):,} across {len(groups):,} distinct normalized names",
          file=sys.stderr)
    print(f"named rows removed as duplicates/fragments: {n_removed:,}", file=sys.stderr)
    print(f"final anchor count: {len(final_rows):,}", file=sys.stderr)


# --------------------------------------------------------------------
# Stage 3: extract-us-boundary -- on navi (one grep over the 10GB file).
# --------------------------------------------------------------------
def extract_us_boundary(admin_path: str, out_path: str) -> None:
    marker = f'"osm_id": {US_RELATION_OSM_ID},'
    with open(admin_path, encoding="utf-8") as fh:
        for line in fh:
            if marker in line:
                feat = json.loads(line)
                if feat["properties"].get("name") == US_RELATION_NAME:
                    with open(out_path, "w", encoding="utf-8") as out:
                        out.write(line if line.endswith("\n") else line + "\n")
                    print(f"wrote {out_path} ({len(line):,} bytes)", file=sys.stderr)
                    return
    raise SystemExit(f"could not find relation {US_RELATION_OSM_ID} named {US_RELATION_NAME!r}")


# --------------------------------------------------------------------
# Stage 4: merge -- anywhere.
# --------------------------------------------------------------------
def in_us(lat, lon, prepared, bounds):
    from shapely.geometry import Point
    minlon, minlat, maxlon, maxlat = bounds
    if not (minlat <= lat <= maxlat and minlon <= lon <= maxlon):
        return False
    return prepared.contains(Point(lon, lat))


def merge(dedup_path: str, us_boundary_path: str, census_path: str, out_path: str) -> None:
    from shapely.geometry import shape
    from shapely.prepared import prep

    with open(us_boundary_path, encoding="utf-8") as f:
        feat = json.load(f)
    assert feat["properties"]["osm_id"] == US_RELATION_OSM_ID
    assert feat["properties"]["name"] == US_RELATION_NAME
    us_poly = shape(feat["geometry"])
    if not us_poly.is_valid:
        us_poly = us_poly.buffer(0)
    us_prepared = prep(us_poly)
    us_bounds = us_poly.bounds

    n_osm_total = 0
    n_osm_us_dropped = 0
    osm_rows = []
    with open(dedup_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            n_osm_total += 1
            lat, lon, radius = float(row["lat"]), float(row["lon"]), float(row["radius_m"])
            if in_us(lat, lon, us_prepared, us_bounds):
                n_osm_us_dropped += 1
                continue
            osm_rows.append((round(lat, 4), round(lon, 4), round(radius)))

    census_lines = []
    with open(census_path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("#") or not line.strip():
                continue
            census_lines.append(line)
    n_census = len(census_lines)

    header = (
        "# lat,lon,effective_radius_m -- worldwide populated-place anchors,\n"
        "# HYBRID provenance (2026-09-07): the 50 US states + DC keep the\n"
        "# existing US Census 2024 Gazetteer places + urban-area rows\n"
        f"# unchanged ({n_census:,} rows -- see scripts/build_places_csv.py),\n"
        "# because a straight OSM swap inside the US measured a 35%\n"
        "# disagreement rate against Census there and OSM alone has NOTHING\n"
        "# for cases like the Las Vegas Strip (Paradise, NV exists in OSM\n"
        "# only as a bare point, not a boundary). Everywhere else in the\n"
        f"# world ({len(osm_rows):,} rows) is derived from OpenStreetMap planet\n"
        "# boundaries + place nodes instead -- far more accurate per-match\n"
        "# where it exists (R^2 0.726, median error 2.1%) but with worse US\n"
        "# coverage (~40% of US places) than Census, which is why the US\n"
        "# stays Census-sourced. A polygon counts as a populated place if it\n"
        "# carries place=city|town|village directly, OR is boundary=\n"
        "# administrative with a place=city|town|village|hamlet NODE of the\n"
        "# same normalized name inside it. A country/state/province-level\n"
        "# object (admin_level<=4) matched that second way is dropped ONLY if\n"
        "# its radius also exceeds 100km -- measured false positives like Sao\n"
        "# Paulo STATE (matched via Sao Paulo city) or Mexico/Panama the\n"
        "# COUNTRIES (matched via a village/the capital sharing that name)\n"
        "# were all >100km; smaller admin_level<=4 matches are kept because\n"
        "# some real cities (Berlin, Hamburg -- German federal city-states)\n"
        "# have no OSM boundary below country/state level at all. Duplicate/\n"
        "# fragment objects sharing a name within ~50km of each other keep\n"
        "# only the largest by area. Radius is sqrt(area/pi) on a Lambert\n"
        "# cylindrical equal-area projection (validated against Yellowstone\n"
        "# to within 1%) -- same formula as the Census side, just a\n"
        "# different area source. OSM anchors falling inside OSM's own\n"
        "# \"United States\" country polygon (point-in-polygon against the\n"
        "# real shape, not a bbox -- see this script's module docstring for\n"
        "# why a bbox measured wrong) are dropped so Census stays sole\n"
        "# authority there. KNOWN BIAS: OSM city polygons include water and\n"
        "# exclaves, so coastal/island cities read larger than their land\n"
        "# area justifies (Istanbul ~2x, Auckland ~3x measured against their\n"
        "# official land areas; accepted, not corrected -- see this script's\n"
        "# module docstring for the full derivation).\n"
        f"# {n_census + len(osm_rows):,} rows total.\n"
    )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header)
        for line in census_lines:
            f.write(line + "\n")
        for lat, lon, radius in osm_rows:
            f.write(f"{lat},{lon},{int(radius)}\n")

    print(f"OSM dedup rows in: {n_osm_total:,}", file=sys.stderr)
    print(f"OSM rows dropped as inside US (real polygon containment): {n_osm_us_dropped:,}", file=sys.stderr)
    print(f"OSM rows kept (non-US): {len(osm_rows):,}", file=sys.stderr)
    print(f"Census rows (unchanged): {n_census:,}", file=sys.stderr)
    print(f"TOTAL final anchors: {n_census + len(osm_rows):,} -> {out_path}", file=sys.stderr)


FALLBACK_PLACE_VALUES = {"city", "town"}
# Fitted 2026-09-07 against 6,378 US Census cities with known land area
# (see this script's `fallback` docstring below for the full rationale
# and the accuracy caveat -- this same relation was measured and
# REJECTED as the primary anchor method).
FALLBACK_RADIUS_A = 2.0524
FALLBACK_RADIUS_B = 0.3138


def _fallback_radius_m(population: float) -> float:
    return 10.0 ** (FALLBACK_RADIUS_A + FALLBACK_RADIUS_B * math.log10(population))


# --------------------------------------------------------------------
# Stage 5: fallback -- anywhere (needs app/ importable, i.e. run from a
# checkout of this repo; the place_points.csv it reads is the same
# planet extraction classify() uses, so at planet scale it wants
# navi's copy the way classify/extract-us-boundary do).
# --------------------------------------------------------------------
def fallback(points_path: str, census_path: str, out_path: str) -> None:
    """Fills the gap the four stages above leave: a real, well-populated
    settlement whose containing OSM administrative boundary is named
    differently from the settlement itself (Mumbai's boundary is
    "Greater Mumbai"; Johannesburg's, Cairo's, and Stockholm's likewise
    fail the exact-normalized-name-match rule classify() uses) gets NO
    anchor at all under the boundary-only approach, even though OSM
    knows exactly where the settlement is via its place=city/town POINT
    -- just not as a boundary. Every landmark in an affected city was
    reading as remote wilderness (25 points) before this stage existed.

    DECIDED APPROACH (Matt, 2026-09-07): where no boundary anchor
    already covers a settlement, fall back to the settlement's own OSM
    point with a radius ESTIMATED from population, rather than leaving
    it unanchored. Restricted to place=city or place=town nodes/ways
    carrying a usable (parses as a positive number) `population` tag --
    place=village and place=hamlet are excluded even with a population
    tag, and city/town WITHOUT one are excluded too, so this adds one
    fallback circle per substantial, population-attested settlement
    instead of millions of hamlet-sized dots blowing up the file.

    COVERAGE CHECK: a candidate is skipped if it is already inside some
    EXISTING anchor's circle (checked via app.places.
    distance_to_nearest_town_m against `census_path` as it stands
    BEFORE this stage runs -- a real boundary anchor, Census or OSM, is
    always strictly authoritative and this stage never overrides or
    duplicates one, only fills a gap). This is the same bucket-scan
    lookup the game itself uses at runtime, not a reimplementation, so
    there is no risk of the build-time check and the runtime answer
    disagreeing.

    RADIUS: log10(radius_m) = 2.0524 + 0.3138 * log10(population) --
    fitted against 6,378 US Census cities with known Census land area
    (~1.6km at 5,000 people, ~4.2km at 100,000, ~10.7km at 2,000,000).
    ACCURACY CAVEAT, IMPORTANT: this exact relation was measured and
    REJECTED as this file's primary anchor method elsewhere in this
    pipeline -- R^2 = 0.214, median relative error 27%, p90 79%.
    Population is a poor predictor of a city's physical extent. It is
    used here anyway, deliberately, because the alternative for these
    settlements is no anchor at all, and "roughly the right city" beats
    "reads as wilderness." Do not read this stage's existence as a
    reversal of that earlier rejection.

    MARKING: fallback rows are appended after a dedicated comment
    header (this function's own, distinct from the HYBRID header above
    it) so they stay visually and programmatically distinguishable from
    the boundary-derived rows above -- grep for "FALLBACK" or take
    every data row from that header onward.
    """
    import sys as _sys
    _sys.path.insert(0, os.path.join(_HERE, ".."))
    from app import places as places_mod

    # Point the runtime loader at the file as it stands right now (pre-
    # fallback) and force a fresh load -- this process may have already
    # imported/loaded app.places for an unrelated reason, and a stale
    # cached bucket set would silently miss the boundary rows just
    # written by an earlier stage in the same run.
    places_mod._DATA_PATH = census_path
    places_mod._BUCKETS = None
    places_mod._MAX_RADIUS_M = 0.0
    places_mod._load()

    n_points = 0
    n_wrong_place = 0
    n_no_population = 0
    n_candidates = 0
    n_covered = 0
    fallback_rows = []
    with open(points_path, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        next(r)  # header
        for row in r:
            if len(row) != 7:
                continue
            n_points += 1
            osm_type, osm_id, name, place, lat, lon, population = row
            if place not in FALLBACK_PLACE_VALUES:
                n_wrong_place += 1
                continue
            population = population.strip()
            if not population:
                n_no_population += 1
                continue
            try:
                pop = float(population)
            except ValueError:
                n_no_population += 1
                continue
            if pop <= 0:
                n_no_population += 1
                continue
            n_candidates += 1
            lat_f, lon_f = float(lat), float(lon)
            if places_mod.distance_to_nearest_town_m(lat_f, lon_f) == 0.0:
                n_covered += 1
                continue
            radius_m = _fallback_radius_m(pop)
            fallback_rows.append((round(lat_f, 4), round(lon_f, 4), round(radius_m)))

    with open(census_path, encoding="utf-8") as f:
        existing = f.read()
    n_existing = sum(
        1 for line in existing.splitlines()
        if line.strip() and not line.startswith("#")
    )
    new_total = n_existing + len(fallback_rows)
    existing = re.sub(
        r"# [\d,]+ rows total\.\n",
        f"# {new_total:,} rows total (includes the FALLBACK section below).\n",
        existing,
        count=1,
    )

    fallback_header = (
        "# ---------------------------------------------------------------------\n"
        "# FALLBACK: place-point anchors (2026-09-07, this script's `fallback`\n"
        "# stage). Some real, well-populated settlements -- Mumbai, Cairo,\n"
        "# Johannesburg, Stockholm among them -- have NO anchor above because\n"
        "# OSM names the containing administrative boundary differently from\n"
        "# the settlement (Mumbai's boundary is \"Greater Mumbai\"), so the\n"
        "# exact-normalized-name-match rule above never connects them, and\n"
        "# every landmark in an affected city would otherwise score as remote\n"
        "# wilderness. These rows fall back to the settlement's own OSM POINT\n"
        f"# instead of a boundary ({n_points:,} place points read; {n_wrong_place:,}\n"
        "# were neither place=city nor place=town and skipped outright;\n"
        f"# {n_no_population:,} of the remainder had no usable population tag and\n"
        f"# were skipped; {n_candidates:,} qualified as candidates; {n_covered:,} of\n"
        "# those were already inside some existing boundary anchor's circle\n"
        "# -- Census or OSM, always authoritative over this section -- and\n"
        f"# were skipped so nothing here ever overrides or duplicates one;\n"
        f"# {len(fallback_rows):,} were genuinely uncovered and are the rows below).\n"
        "# Radius is ESTIMATED from population:\n"
        "#     log10(radius_m) = 2.0524 + 0.3138 * log10(population)\n"
        "# fitted against 6,378 US Census cities with known land area (~1.6km\n"
        "# at 5,000 people, ~4.2km at 100,000, ~10.7km at 2,000,000).\n"
        "# ACCURACY CAVEAT: this same relation was measured and REJECTED as\n"
        "# this file's primary anchor method (R^2 = 0.214, median relative\n"
        "# error 27%, p90 79%) -- population is a poor predictor of a city's\n"
        "# physical extent. Used here anyway because the alternative for\n"
        "# these settlements is no anchor at all, and \"roughly the right\n"
        "# city\" beats \"reads as wilderness.\"\n"
        f"# {len(fallback_rows):,} fallback rows below.\n"
        "# ---------------------------------------------------------------------\n"
    )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(existing)
        if not existing.endswith("\n"):
            f.write("\n")
        f.write(fallback_header)
        for lat, lon, radius in fallback_rows:
            f.write(f"{lat},{lon},{int(radius)}\n")

    print(f"place points read: {n_points:,}", file=sys.stderr)
    print(f"skipped, not city/town: {n_wrong_place:,}", file=sys.stderr)
    print(f"skipped, no usable population: {n_no_population:,}", file=sys.stderr)
    print(f"candidates (city/town with population): {n_candidates:,}", file=sys.stderr)
    print(f"skipped, already covered by an existing anchor: {n_covered:,}", file=sys.stderr)
    print(f"fallback rows added: {len(fallback_rows):,}", file=sys.stderr)
    print(f"pre-fallback anchor rows: {n_existing:,}", file=sys.stderr)
    print(f"TOTAL final anchors: {new_total:,} -> {out_path}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="stage", required=True)

    p = sub.add_parser("classify")
    p.add_argument("--admin", default=DEFAULT_ADMIN_BOUNDARIES)
    p.add_argument("--points", default=DEFAULT_PLACE_POINTS)
    p.add_argument("--out", required=True)

    p = sub.add_parser("dedup")
    p.add_argument("--raw", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("extract-us-boundary")
    p.add_argument("--admin", default=DEFAULT_ADMIN_BOUNDARIES)
    p.add_argument("--out", required=True)

    p = sub.add_parser("merge")
    p.add_argument("--dedup", required=True)
    p.add_argument("--us-boundary", required=True)
    p.add_argument("--census-csv", default=CENSUS_PLACES_CSV)
    p.add_argument("--out", default=CENSUS_PLACES_CSV)

    p = sub.add_parser("fallback")
    p.add_argument("--points", default=DEFAULT_PLACE_POINTS)
    p.add_argument("--census-csv", default=CENSUS_PLACES_CSV)
    p.add_argument("--out", default=CENSUS_PLACES_CSV)

    args = ap.parse_args()
    if args.stage == "classify":
        classify(args.admin, args.points, args.out)
    elif args.stage == "dedup":
        dedup(args.raw, args.out)
    elif args.stage == "extract-us-boundary":
        extract_us_boundary(args.admin, args.out)
    elif args.stage == "merge":
        merge(args.dedup, args.us_boundary, args.census_csv, args.out)
    elif args.stage == "fallback":
        fallback(args.points, args.census_csv, args.out)


if __name__ == "__main__":
    main()
