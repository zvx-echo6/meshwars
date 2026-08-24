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
    needed.
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
import sqlite3
import time

from shapely import wkt as shapely_wkt
from shapely.geometry import MultiPolygon, Polygon, box as shapely_box

from .grid import CELL_LAT_DEG, CELL_LON_DEG, cell_bounds, cell_id

log = logging.getLogger("places_seed")

_DATA_PATH = os.path.join(os.path.dirname(__file__), "reference", "places_worth_going.csv")

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
        return (assoc in US_SOTA_ASSOCIATIONS, False)
    if ref_type == "park":
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
    area_m2/geom/rotates/active refreshed, created_at and id
    preserved), and place_cell for a place is fully replaced each time
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
    stats = {
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
    st = os.stat(_DATA_PATH)
    fingerprint = f"{st.st_size}:{int(st.st_mtime)}"
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
                points = int(row["points"])
                area_m2 = float(row["area_m2"]) if row.get("area_m2") else None
                geom_wkt = row.get("geom") or None

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
                else:
                    cells = {cell_id(lat, lon)}

                stats["kept"][ref_type] += 1

                cur = conn.execute(
                    "INSERT INTO place(ref_type, ref_code, name, lat, lon, points, source, "
                    "area_m2, geom, rotates, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,?) "
                    "ON CONFLICT(ref_type, ref_code) DO UPDATE SET "
                    "name=excluded.name, lat=excluded.lat, lon=excluded.lon, "
                    "points=excluded.points, source=excluded.source, "
                    "area_m2=excluded.area_m2, geom=excluded.geom, rotates=excluded.rotates, "
                    # Reactivate on the spot: a place that left the seed
                    # and later comes back (a re-pull picks it up again)
                    # is live again the moment this upsert touches it,
                    # same id, same UNIQUE(ref_type, ref_code) row.
                    "active=1 "
                    "RETURNING id",
                    (ref_type, row["ref_code"], row["name"], lat, lon, points, row["source"],
                     area_m2, geom_wkt, 1 if rotates else 0, now),
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
        "deactivated=%d (left the seed, kept as history) in %.1fs",
        stats["kept"]["summit"], stats["kept"]["park"], stats["kept"]["landmark"],
        stats["excluded"]["summit"], stats["excluded"]["park"], stats["excluded"]["landmark"],
        stats["park_matched"], stats["park_unmatched"],
        stats["park_matched_larger"], stats["park_matched_smaller"],
        stats["deactivated"], elapsed,
    )
    return stats
