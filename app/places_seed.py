"""Loads app/reference/places_worth_going.csv into the `place` and
`place_cell` tables. Companion to scripts/build_places_seed.py, which
builds the CSV from SOTA/POTA/OSM+PAD-US -- this module never touches
the network or the CSV's own contents, only what gets written to the
database from it.

Called from app/db.init_db() on every startup, same as SCHEMA/MIGRATIONS
-- idempotent, so a re-run (a restart, a redeploy with an unchanged CSV)
is a cheap no-op per row via the UPSERT below, not a duplicate load. Not
app/places.py's in-memory-bucket pattern: that module answers "how far
is the nearest town" from a flat file with no database involved at all,
because nothing else needs a `town` row to exist. This feature needs
`place` rows other tables can foreign-key against (place_activation,
place_cell), so it loads into SQLite instead -- same CSV-shipped-with-
the-code precedent (app/reference/, not the gitignored data/ volume),
different destination.

COUNTRY FILTER -- the CSV is not pre-filtered to the US. It is built
from a single bounding box (see scripts/build_places_seed.py's module
docstring) that, being a rectangle, also sweeps in northern Mexico and
southern Canada. MeshWars is a US game, so this loader excludes:

  - SOTA summits: association code (the part of ref_code before the
    first "/") not in US_SOTA_ASSOCIATIONS below. Confirmed against
    SOTA's own /api/associations/ endpoint on 2026-08-24: every code in
    the CSV maps to dxcc "291" (USA) except XE2 (Mexico - North) and
    VE5/VE6/VE7 (Saskatchewan/Alberta/British Columbia). K0M looks like
    an odd one out next to the W-prefixed codes but is genuinely
    USA - Minnesota, confirmed the same way, not a typo.
  - POTA parks: reference prefix (the part of ref_code before the
    first "-") not "US". POTA's own reference scheme puts the country
    right there -- "US-1234" / "CA-1234" / "MX-0001" -- no lookup
    needed. ADDED 2026-08-24: parks with source "PAD-US" (ref_code
    "PADUS-<fid>", from build_places_seed.py's fetch_padus_parks() --
    local/city/county parks POTA never lists at all, since POTA only
    covers what hams activate) skip this prefix check and are kept
    unconditionally instead -- they are pulled from a single US-
    territory PAD-US layer already scoped to the play area's bbox, so
    there is no CA-/MX- equivalent to filter, and their ref_code does
    not start with "US-" for the prefix check to even parse correctly.
  - OSM landmarks: NOT filtered. Verified rather than assumed: every
    landmark row's lat/lon falls inside 31.33-49.01N, -124.72 to
    -102.04W -- exactly the western US states extract
    (western-us-11states.osm.pbf) build_places_seed.py's
    extract_landmarks() reads from, bounded by the real AZ/CA-Mexico
    border (~31.33N) and the real US-Canada border (49.00N) rather than
    the bbox. There is nothing non-US in this file to filter.

PARK BOUNDARY COVERAGE IS PARTIAL -- of the parks kept after the country
filter, POTA-to-PAD-US matching (scripts/build_places_seed.py's
match_parks()) found a boundary for roughly 61%; the rest have
area_m2/geom NULL. This loader does not treat "unmatched" as "small":
an unmatched park is loaded with rotates=0 (always active, never
rotating) and scores its point's own cell like a landmark, exactly the
same containment rule a genuinely-smaller-than-a-cell matched park gets
-- but for a different reason. A matched park scores by the >50%
boundary rule instead, but ONLY if its boundary is at least one grid
cell in area; a matched park smaller than a cell also falls back to
scoring its point's own cell, same as an unmatched one.

Why unmatched parks are permanent rather than rotating: rotates=1 is
supposed to mean "this is a small, town-scale destination", and a
missing boundary is a data gap, not evidence of size -- POTA's larger
wilderness parks are exactly the ones a coarse boundary match can miss.
Rotating a park because the seed pipeline could not find its shape would
punish it for that gap, not for anything about the place itself.
"""
from __future__ import annotations

import csv
import logging
import math
import os
import re
import sqlite3
import time

from shapely import wkt as shapely_wkt
from shapely.geometry import MultiPolygon, Polygon, box as shapely_box

from .grid import CELL_LAT_DEG, CELL_LON_DEG, cell_bounds, cell_id, distance_m

log = logging.getLogger("places_seed")

_DATA_PATH = os.path.join(os.path.dirname(__file__), "reference", "places_worth_going.csv")
# Summit -> squares, built by scripts/build_summit_cells.py against the
# planet DEM on navi. A summit's squares cannot be derived here the way a
# park's are from its boundary: the test is terrain (within 1.5km AND
# within 200m of the summit's own elevation, plus the peak's own square), and the app host has no
# elevation data. So it ships precomputed, same as the seed itself.
_SUMMIT_CELLS_PATH = os.path.join(os.path.dirname(__file__), "reference", "summit_cells.csv")


def _load_summit_cells(path: str = _SUMMIT_CELLS_PATH) -> dict[str, set[str]]:
    """ref_code -> the squares that credit that summit.

    Stored as offsets from each summit's own square rather than absolute
    ids -- 0.55MB for 92k squares instead of several times that.

    Every square is already assigned to exactly ONE summit (its nearest)
    by the build script. That is the whole reason this file exists rather
    than a radius computed at load time: summits cluster, so without the exclusivity pass one hike
    would credit several peaks at once. It cannot be redone here, because
    "nearest" is global across all summits, not a property of one row.

    A missing or unreadable file is not fatal: summits fall back to their
    own single square, which is what they had before this existed.
    """
    out: dict[str, set[str]] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            header = fh.readline()
            if not header.startswith("ref_code"):
                return {}
            for line in fh:
                parts = line.rstrip("\n").split(",", 3)
                if len(parts) != 4:
                    continue
                ref_code, by, bx, offs = parts
                try:
                    base_y, base_x = int(by), int(bx)
                except ValueError:
                    continue
                cells = set()
                for off in offs.split():
                    dy, _, dx = off.partition(":")
                    try:
                        cells.add(f"{base_y + int(dy)}_{base_x + int(dx)}")
                    except ValueError:
                        continue
                if cells:
                    out[ref_code] = cells
    except OSError as e:
        log.warning("places_seed: summit cells unavailable (%s) -- "
                    "summits fall back to their own square", e)
        return {}
    return out

_METERS_PER_DEG_LAT = 111_320.0

# Pulled from https://api-db2.sota.org.uk/api/associations/ on
# 2026-08-24 and filtered to dxcc "291" (USA) -- the full US SOTA
# association list, not just the ones this CSV happens to contain,
# so a future re-pull with a wider bbox (e.g. reaching Alaska or the
# Atlantic seaboard) is still classified correctly without touching
# this file again.
US_SOTA_ASSOCIATIONS = frozenset({
    "K0M", "KH6",
    "W0C", "W0D", "W0I", "W0M", "W0N",
    "W1", "W2", "W3",
    "W4A", "W4C", "W4G", "W4K", "W4T", "W4V",
    "W5A", "W5M", "W5N", "W5O", "W5T",
    "W6",
    "W7A", "W7I", "W7M", "W7N", "W7O", "W7U", "W7W", "W7Y",
    "W8M", "W8O", "W8V",
    "W9",
})

_MIN_OVERLAP_FRACTION = 0.5  # ">50% inside the boundary"

# SUMMIT/LANDMARK DOUBLE-DIP FILTER (added 2026-08-25) -- some SOTA
# summits carry a fire lookout, and OSM separately maps that lookout as
# its own landmark (man_made=tower / tower:type=observation, or similar
# -- extract_landmarks() in scripts/build_places_seed.py does not
# distinguish landmark subtypes, so these come through like any other
# landmark). Both rows then sit at essentially the same coordinates: the
# 100-point summit and a 5-point landmark for the SAME physical peak,
# meaning one visit could score twice. The summit is the higher tier and
# the real destination, so the landmark loses -- dropped here rather
# than kept and left to double-score.
#
# 100m, not miles: a lookout tower sits ON the summit it serves, so the
# two points are essentially coincident modulo GPS/OSM-mapping noise,
# not "nearby". 100m is generous enough to absorb that noise (SOTA's
# summit point and OSM's tower point are rarely surveyed to the same
# few meters) while still being far too tight to catch a genuinely
# separate landmark (a trailhead sign, a monument) that merely happens
# to sit on the same mountain a summit does -- those are real, distinct
# destinations and must survive.
_SUMMIT_COLOCATION_RADIUS_M = 100.0


def _kept_summit_buckets(path: str) -> dict[str, list[tuple[float, float]]]:
    """First pass over the CSV: (lat, lon) of every summit that will
    actually be KEPT (passes the same US/named-summit test
    _classify_row applies in the real load), bucketed by grid cell id
    for a cheap proximity lookup in the main load loop below. A summit
    that _classify_row would exclude (non-US, or an unnamed placeholder
    peak) never got the game's 100 points in the first place, so a
    landmark near IT must not be excluded either -- there would be
    nothing left at that spot to double-dip against.
    """
    buckets: dict[str, list[tuple[float, float]]] = {}
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if row["ref_type"] != "summit":
                continue
            keep, _ = _classify_row(row)
            if not keep:
                continue
            lat, lon = float(row["lat"]), float(row["lon"])
            buckets.setdefault(cell_id(lat, lon), []).append((lat, lon))
    return buckets


def _near_kept_summit(lat: float, lon: float, buckets: dict[str, list[tuple[float, float]]]) -> bool:
    """True if (lat, lon) is within _SUMMIT_COLOCATION_RADIUS_M of any
    bucketed summit. Checks the containing cell PLUS its 8 neighbours
    (grid cells are ~300x420m -- see CELL_LAT_DEG/CELL_LON_DEG -- well
    over 3x the 100m radius, so a summit just across a cell boundary
    from the landmark being tested is still caught) rather than trusting
    a single cell lookup, same reasoning _park_cells already applies to
    boundary-cell membership above.
    """
    lat_idx = math.floor(lat / CELL_LAT_DEG)
    lon_idx = math.floor(lon / CELL_LON_DEG)
    for d_lat in (-1, 0, 1):
        for d_lon in (-1, 0, 1):
            cid = f"{lat_idx + d_lat}_{lon_idx + d_lon}"
            for s_lat, s_lon in buckets.get(cid, ()):
                if distance_m(lat, lon, s_lat, s_lon) <= _SUMMIT_COLOCATION_RADIUS_M:
                    return True
    return False

# NAMED-SUMMITS-ONLY FILTER (added 2026-08-24) -- SOTA does not require
# a summit to have a real name. Where one is missing, SOTA's own
# summitslist.csv records the peak's elevation as its "name" instead
# (measured directly against this CSV: 1,688 of 8,082 summit rows, ~a
# fifth, mostly unnamed Colorado/California/Wyoming thirteeners/high
# points -- e.g. W0C/LG-007 is named "13546"). A small second group uses
# a generic placeholder word plus a bare designator instead of an
# elevation -- "Peak 15-46", "Peak 8" -- same problem, different shape.
# A game about places worth going should not send someone to go stand
# on a number. This applies to SUMMITS ONLY -- parks and landmarks are
# untouched, see _classify_row below.
#
# THIRTEENER EXCEPTION (Matt, 2026-08-24): in Colorado specifically,
# peaks at 13,000ft+ are genuinely known BY their elevation -- climbers
# say "13546" the way they would say a name, and there is a whole
# "peak-bagging the thirteeners" pursuit built around exactly that
# numbering. A bare number there is a real identifier, not missing
# data, unlike a low unnamed bump whose number tells you nothing. So a
# purely-numeric name is kept when it parses as an elevation of 13,000
# ft or more -- expressed as an elevation threshold, not a hardcoded
# Colorado check, because the reasoning is the convention, not the
# state line, and it is nearly all Colorado in practice but not
# exclusively (California and Wyoming both contribute a handful too).
#
# This CSV does not carry SOTA's own AltFt/AltM elevation columns (see
# module docstring -- build_places_seed.py's fetch_sota() never writes
# them), so there is no separate elevation field in this file to check
# a numeric name against at load time. Verified as a one-time check
# instead, against a fresh pull of SOTA's live summitslist.csv on
# 2026-08-24 (25MB, 181,658 rows with an AltFt value): every one of
# the 1,688 numeric-named US summits in this seed has an AltFt within
# 50ft of its own name parsed as a number -- ZERO mismatches. SOTA's
# elevation-as-name behavior is exact, not approximate, so parsing the
# name itself as feet and comparing it to 13,000 is equivalent to
# checking the real AltFt column for every row this filter has ever
# seen, not a guess dressed up as a threshold.
_THIRTEENER_MIN_FT = 13000.0

# Matches a name that is ONLY a generic placeholder word followed by
# nothing but digits/dashes/dots/whitespace -- "Peak 15-46", "Pt.
# 1234", "BM 6187" -- so it can be dropped the same way a bare
# elevation is. Anchored start-to-end (not just a prefix match) so a
# placeholder word followed by any real word survives: "Peak Mountain",
# "Twin Peaks", "Summit Creek Peak", "Peak of the Clouds" all keep a
# non-digit, non-placeholder word after (or before) the trigger word and
# so never match this pattern at all.
_GENERIC_SUMMIT_NAME_RE = re.compile(
    r"^(unnamed|peak|pt|point|summit|hill|bm|benchmark)[\s\-.\d]*$",
    re.IGNORECASE,
)


def _summit_has_real_name(name: str) -> bool:
    """True if `name` is an actual summit name, not SOTA's elevation-
    as-name stand-in or a bare "Peak <number>"-style placeholder.

    Drops (in order):
      1. Empty or whitespace-only.
      2. No alphabetic character anywhere -- catches a bare elevation
         like "13546" -- UNLESS it parses as 13,000 or more, the
         thirteener exception above ("12,883" with a comma, or "2308 m"
         with a unit, do not parse as a plain float and so fall through
         to dropped, same as any other unparseable numeric-shaped
         string below the threshold).
      3. A generic placeholder word (unnamed/peak/pt/point/summit/hill/
         bm/benchmark) followed only by digits/dashes/dots/whitespace,
         case-insensitively -- "Peak 15-46" is exactly SOTA's summit
         Points 8+ pull result for an unnamed peak in a range that
         numbers its unnamed summits instead of using an elevation.
         NOT eligible for the thirteener exception even at a
         13,000+-sounding number -- that exception is specifically for
         a bare elevation standing in as a name, not a placeholder word
         plus a designator.

    Deliberately does NOT drop a name merely because it contains a
    digit -- "Mount 7", "Ten Mile Peak", "Highway 12 Summit", "Peak C"
    (a real single-letter designation used on some ridgelines) all have
    a letter-shaped name a human actually uses and must survive.
    """
    name = name.strip()
    if not name:
        return False
    if not any(ch.isalpha() for ch in name):
        try:
            elevation_ft = float(name)
        except ValueError:
            return False
        return elevation_ft >= _THIRTEENER_MIN_FT
    if _GENERIC_SUMMIT_NAME_RE.match(name):
        return False
    return True


def _cell_area_m2(lat: float) -> float:
    """Area in m^2 of the grid cell containing latitude `lat`. Longitude
    degrees compress toward the poles (cos(lat)); latitude degrees do
    not -- same model app/grid.py's fixed-degree cell already uses, just
    converted to an area for the >50%-of-a-cell size test."""
    lat_m = CELL_LAT_DEG * _METERS_PER_DEG_LAT
    lon_m = CELL_LON_DEG * _METERS_PER_DEG_LAT * math.cos(math.radians(lat))
    return lat_m * lon_m


def _park_cells(geom: Polygon | MultiPolygon, lat: float) -> set[str]:
    """Cell ids where more than half the CELL's own area lies inside
    `geom`. Walked per polygon part of a MultiPolygon (a national forest
    made of scattered units, say) rather than over the union's bounding
    box, so the empty ground between distant parts is never iterated.

    The fraction compares CELL area to CELL area (both in raw degree^2
    units, never converted to meters) -- a ratio of two areas that share
    the same local longitude compression cancels it out, so no metric
    conversion is needed here the way _cell_area_m2 needs one to compare
    against an absolute size in meters.
    """
    parts = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
    cells: set[str] = set()
    for part in parts:
        minx, miny, maxx, maxy = part.bounds
        lat_idx0, lat_idx1 = math.floor(miny / CELL_LAT_DEG), math.floor(maxy / CELL_LAT_DEG)
        lon_idx0, lon_idx1 = math.floor(minx / CELL_LON_DEG), math.floor(maxx / CELL_LON_DEG)
        for lat_idx in range(lat_idx0, lat_idx1 + 1):
            for lon_idx in range(lon_idx0, lon_idx1 + 1):
                cid = f"{lat_idx}_{lon_idx}"
                south, west, north, east = cell_bounds(cid)
                cell_poly = shapely_box(west, south, east, north)
                inter = cell_poly.intersection(part)
                if inter.is_empty:
                    continue
                if (inter.area / cell_poly.area) > _MIN_OVERLAP_FRACTION:
                    cells.add(cid)
    return cells


def _classify_row(row: dict) -> tuple[bool, bool]:
    """(keep, rotates) for one CSV row, before any geometry work."""
    ref_type = row["ref_type"]
    if ref_type == "summit":
        assoc = row["ref_code"].split("/")[0]
        if assoc not in US_SOTA_ASSOCIATIONS:
            return (False, False)
        # Country filter passed -- now require an actual name (see
        # _summit_has_real_name's docstring for what this catches and
        # what it deliberately lets through).
        return (_summit_has_real_name(row.get("name", "")), False)
    if ref_type == "park":
        # ADDED 2026-08-24: PAD-US-sourced parks (ref_code "PADUS-<fid>")
        # are pulled directly from a single US-territory PAD-US layer
        # (scripts/build_places_seed.py's fetch_padus_parks(), run
        # against the play area's own bbox) -- there is no CA-/MX-
        # equivalent to filter the way POTA's own reference prefix
        # requires below, so these are kept unconditionally rather than
        # run through the POTA-shaped prefix check, which would reject
        # every one of them (their ref_code does not start with "US-").
        if row.get("source") == "PAD-US":
            return (True, None)  # rotates decided later, once area is known
        prefix = row["ref_code"].split("-")[0]
        if prefix != "US":
            return (False, False)
        return (True, None)  # rotates decided later, once area is known
    # landmark: verified US-only at CSV build time, see module docstring
    return (True, True)


def load_places_seed(conn: sqlite3.Connection) -> dict:
    """Upsert every kept row of the CSV into `place` (and `place_cell`
    for the cells it scores), inside its own transaction. Returns a
    stats dict for the caller to log -- see app/db.init_db().

    Safe to call every startup: existing rows are updated in place by
    the UNIQUE(ref_type, ref_code) upsert (name/lat/lon/points/source/
    area_m2/geom/rotates/points_reason/elevation_ft/active refreshed, created_at and
    id preserved), and place_cell for a place is fully replaced each time
    so a boundary that changed on re-seed cannot leave stale cells
    behind.

    RECONCILE, NOT JUST INSERT -- every row this pass upserts is also
    remembered by id, and once the CSV is fully read, any place row
    that is currently active but was NOT touched this pass (i.e. it
    left the seed -- pruned, or reclassified out of the US filter) is
    marked active=0 rather than deleted. Deleting it would either
    cascade-orphan place_activation/place_week (losing a player's
    already-earned points and the name their history points at) or
    require a non-destructive FK just to avoid that -- inactive is
    simpler and keeps the row's name resolvable forever. A place that
    LEFT and later RETURNS to the seed (id preserved via the ref_type/
    ref_code upsert) is reactivated by the same upsert that touches it.
    Every read path that draws or scores a place (app/places_api.py's
    two routes, app/place_scoring.credit_places, app/place_rotation's
    draw and always-active set) filters WHERE active = 1; nothing that
    only sums place_activation.points (Explorer score, team totals)
    needs to, since those rows already carry their own frozen points
    and never join back to `place` for eligibility.
    """
    # Read once, not per row: 4,823 summits and 655k squares.
    summit_cells = _load_summit_cells()
    stats = {
        "summit_cells_loaded": len(summit_cells),
        "excluded": {"summit": 0, "park": 0, "landmark": 0},
        "kept": {"summit": 0, "park": 0, "landmark": 0},
        # Of kept parks: matched a PAD-US boundary at all, vs not.
        "park_matched": 0, "park_unmatched": 0,
        # Of the MATCHED ones only: at/above one grid cell (scores by
        # the >50% rule, always active) vs below one cell (scores its
        # point, rotates like a landmark).
        "park_matched_larger": 0, "park_matched_smaller": 0,
        # Reconcile outcome: rows flipped active->inactive this pass
        # because they were not present in this load at all (never
        # deleted -- see the reconcile note above).
        "deactivated": 0,
        # Landmarks dropped by the summit/landmark double-dip filter
        # (see _SUMMIT_COLOCATION_RADIUS_M) -- a distinct counter from
        # excluded.landmark (the non-US filter) so this specific,
        # data-quality-sensitive exclusion is visible on its own rather
        # than folded into a bucket that would hide a wrong radius
        # behind a normal-looking total.
        "landmark_colocated_with_summit": 0,
    }

    if not os.path.exists(_DATA_PATH):
        log.warning("places_seed: %s not found -- places feature will have no data", _DATA_PATH)
        return stats

    # Cheap fingerprint (size + mtime, not a content hash -- this file
    # is 9+MB and hashing it is not what makes a re-run slow; the park
    # boundary geometry work below is, at roughly a minute for ~3,500
    # matched parks) so an unchanged CSV across a routine restart skips
    # the whole pass rather than repeating a minute of shapely work on
    # every boot. `cursor` is app/db.py's existing generic key/value
    # table (get_cursor/set_cursor) -- read/written directly here rather
    # than imported, since importing app.db from a module app.db itself
    # imports would be circular.
    #
    # _RECONCILE_VERSION rides along in the fingerprint string so a code
    # upgrade alone -- CSV byte-for-byte unchanged -- still forces one
    # full pass. Without it, a DB that already recorded this exact CSV's
    # fingerprint under the OLD insert-only loader (no reconcile at all)
    # would skip forever after upgrading to this fix: the file never
    # changes again, so "unchanged since last load" would stay true
    # indefinitely and the stale rows this fix exists to clean up would
    # never actually get cleaned up. Bump this whenever the reconcile
    # mechanics OR the per-row _classify_row rules change in a way that
    # requires re-running against an already-fingerprinted CSV -- the
    # named-summits-only filter (2026-08-24) is exactly that case: the
    # CSV's bytes are unchanged, only which rows get kept changed, so a
    # DB fingerprinted before this filter landed needs the version bump
    # to actually deactivate the newly-excluded summits rather than
    # trusting a fingerprint recorded under the old, looser rule.
    #
    # v3 (2026-08-25): the summit/landmark double-dip filter
    # (_SUMMIT_COLOCATION_RADIUS_M) is new -- same "CSV bytes unchanged,
    # which rows get kept changed" situation, so a DB fingerprinted
    # under v2 needs this bump to actually deactivate the newly-excluded
    # co-located landmarks.
    #
    # v4 (2026-08-31): summits stopped being a single square and became
    # their terrain-qualified set (reference/summit_cells.csv, see
    # _load_summit_cells). The seed CSV's own bytes did not move, but
    # every summit's place_cell rows did, so a DB fingerprinted under v3
    # would keep the old one-square-per-summit mapping forever without
    # this bump.
    _RECONCILE_VERSION = 4
    st = os.stat(_DATA_PATH)
    # summit_cells.csv rides along in the fingerprint too. It decides
    # every summit's place_cell rows but is a SEPARATE file from the seed
    # CSV, so a change to it alone would leave the fingerprint untouched
    # and the old mapping loaded forever. (Tightening the radius from 5km
    # to 1.5km only reloaded because the deploy happened to rewrite the
    # seed CSV and move its mtime -- luck, not design.) Missing file
    # contributes a constant, so its absence is stable rather than
    # re-triggering a load every startup.
    try:
        sc = os.stat(_SUMMIT_CELLS_PATH)
        summit_fp = f"{sc.st_size}:{int(sc.st_mtime)}"
    except OSError:
        summit_fp = "none"
    fingerprint = f"{st.st_size}:{int(st.st_mtime)}:v{_RECONCILE_VERSION}:s{summit_fp}"
    row = conn.execute("SELECT v FROM cursor WHERE k = 'places_seed_csv_fingerprint'").fetchone()
    if row is not None and row[0] == fingerprint:
        log.info("places_seed: CSV unchanged since last load (%s), skipping", fingerprint)
        counts = conn.execute(
            "SELECT ref_type, COUNT(*) FROM place WHERE active = 1 GROUP BY ref_type"
        ).fetchall()
        stats["kept"] = {r[0]: r[1] for r in counts}
        return stats

    now = int(time.time())
    t0 = time.monotonic()
    # Built up front, once, from its own pass over the file -- see
    # _kept_summit_buckets's docstring for why this has to be a
    # separate pass rather than accumulated inline as the main loop
    # below reads landmark rows (a landmark can appear before the
    # summit it double-dips with in file order; SOTA/PADUS/OSM rows are
    # not sorted by proximity to each other).
    summit_buckets = _kept_summit_buckets(_DATA_PATH)
    seen_ids: set[int] = set()
    conn.execute("BEGIN IMMEDIATE")
    try:
        with open(_DATA_PATH, encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                ref_type = row["ref_type"]
                keep, rotates = _classify_row(row)
                if not keep:
                    stats["excluded"][ref_type] += 1
                    continue

                lat = float(row["lat"])
                lon = float(row["lon"])

                if ref_type == "landmark" and _near_kept_summit(lat, lon, summit_buckets):
                    stats["landmark_colocated_with_summit"] += 1
                    continue
                points = int(row["points"])
                area_m2 = float(row["area_m2"]) if row.get("area_m2") else None
                geom_wkt = row.get("geom") or None
                # 'in_city' / 'remote' / 'remote_scaled' -- see
                # app/db.py's `place` table comment and
                # scripts/build_places_seed.py's score_points().
                # row.get(), not row[...]: a CSV written before this
                # column existed (an old fixture, or a stale
                # cursor-fingerprinted file mid-upgrade) still loads.
                points_reason = row.get("points_reason") or None
                # summit only; "" for park/landmark (see build_places_
                # seed.py's SEED_FIELDS comment) and for a CSV written
                # before this column existed -- both read as NULL here,
                # same row.get() fallback reasoning as points_reason
                # just above.
                elevation_ft = float(row["elevation_ft"]) if row.get("elevation_ft") else None

                cells: set[str] = set()
                if ref_type == "park":
                    if geom_wkt and area_m2 is not None and area_m2 >= _cell_area_m2(lat):
                        # Matched boundary at or above one grid cell:
                        # score by the >50%-of-cell rule, always active.
                        geom = shapely_wkt.loads(geom_wkt)
                        cells = _park_cells(geom, lat)
                        rotates = False
                        stats["park_matched"] += 1
                        stats["park_matched_larger"] += 1
                        if not cells:
                            # Boundary matched but no cell clears 50% (a
                            # sliver, or a simplification artifact) --
                            # fall back to the point so the park is not
                            # silently unscoreable.
                            cells = {cell_id(lat, lon)}
                    else:
                        # Either unmatched (no boundary at all) or
                        # matched but smaller than one cell -- both
                        # score their point's own cell. Unmatched stays
                        # permanent (rotates=False); a genuinely small
                        # matched park rotates like a landmark.
                        cells = {cell_id(lat, lon)}
                        if geom_wkt and area_m2 is not None:
                            stats["park_matched"] += 1
                            stats["park_matched_smaller"] += 1
                            rotates = True
                        else:
                            stats["park_unmatched"] += 1
                            rotates = False
                elif ref_type == "summit":
                    # Terrain-qualified squares (see _load_summit_cells).
                    # Falls back to the summit's own square when the file
                    # is absent or has no row for this summit -- which is
                    # exactly the behaviour summits had before, so a
                    # missing artifact degrades rather than breaks.
                    cells = summit_cells.get(row["ref_code"]) or {cell_id(lat, lon)}
                else:
                    cells = {cell_id(lat, lon)}

                stats["kept"][ref_type] += 1

                cur = conn.execute(
                    "INSERT INTO place(ref_type, ref_code, name, lat, lon, points, source, "
                    "area_m2, geom, rotates, points_reason, elevation_ft, active, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?) "
                    "ON CONFLICT(ref_type, ref_code) DO UPDATE SET "
                    "name=excluded.name, lat=excluded.lat, lon=excluded.lon, "
                    "points=excluded.points, source=excluded.source, "
                    "area_m2=excluded.area_m2, geom=excluded.geom, rotates=excluded.rotates, "
                    "points_reason=excluded.points_reason, elevation_ft=excluded.elevation_ft, "
                    # Reactivate on the spot: a place that left the seed
                    # and later comes back (a re-pull picks it up again)
                    # is live again the moment this upsert touches it,
                    # same id, same UNIQUE(ref_type, ref_code) row.
                    "active=1 "
                    "RETURNING id",
                    (ref_type, row["ref_code"], row["name"], lat, lon, points, row["source"],
                     area_m2, geom_wkt, 1 if rotates else 0, points_reason, elevation_ft, now),
                )
                place_id = cur.fetchone()[0]
                seen_ids.add(place_id)

                conn.execute("DELETE FROM place_cell WHERE place_id = ?", (place_id,))
                conn.executemany(
                    "INSERT OR IGNORE INTO place_cell(place_id, cell_id) VALUES (?, ?)",
                    [(place_id, c) for c in cells],
                )

        # Reconcile: any place that is currently active but was not
        # touched by this pass has left the seed. Deactivate rather
        # than delete -- see load_places_seed()'s docstring. A temp
        # table (rather than one giant "id NOT IN (?,?,?...)") because
        # seen_ids can run past SQLite's default bound-parameter limit
        # (~32k rows kept here, well over the ~999 default) -- inserted
        # one row per statement via executemany, so no single statement
        # is ever parameter-bound by the size of the seed.
        conn.execute("CREATE TEMP TABLE _places_seed_seen (id INTEGER PRIMARY KEY)")
        conn.executemany(
            "INSERT INTO _places_seed_seen(id) VALUES (?)", [(pid,) for pid in seen_ids]
        )
        cur = conn.execute(
            "UPDATE place SET active = 0 "
            "WHERE active = 1 AND id NOT IN (SELECT id FROM _places_seed_seen)"
        )
        stats["deactivated"] = cur.rowcount
        conn.execute("DROP TABLE _places_seed_seen")

        conn.execute(
            "INSERT INTO cursor(k, v) VALUES ('places_seed_csv_fingerprint', ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (fingerprint,),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    elapsed = time.monotonic() - t0
    log.info(
        "places_seed: loaded summit=%d park=%d landmark=%d (excluded non-US: summit=%d park=%d landmark=%d) "
        "park boundary matched=%d unmatched=%d (of matched: larger-than-cell=%d smaller-than-cell=%d) "
        "landmark colocated with summit=%d (dropped, within %.0fm) "
        "deactivated=%d (left the seed, kept as history) in %.1fs",
        stats["kept"]["summit"], stats["kept"]["park"], stats["kept"]["landmark"],
        stats["excluded"]["summit"], stats["excluded"]["park"], stats["excluded"]["landmark"],
        stats["park_matched"], stats["park_unmatched"],
        stats["park_matched_larger"], stats["park_matched_smaller"],
        stats["landmark_colocated_with_summit"], _SUMMIT_COLOCATION_RADIUS_M,
        stats["deactivated"], elapsed,
    )
    return stats
