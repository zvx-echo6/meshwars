"""Read routes for "Places Worth Going" (docs/features/places.md):
what frontend/map2.js draws on the map and lists in its slide-out panel.

Not part of the keyed /api/v1 surface (app/public_api.py) -- that is a
deliberately separate, stable contract for external integrators, and
every other route the site's OWN pages call (/get-nodes, /live-tracks,
/api/mc/board, ...) lives outside it too. These are exactly that kind
of route: shaped for map2's own rendering needs, free to change with
the page, and requiring no key, same as /get-nodes.

Both routes only ever return LIVE places -- active (place.active = 1,
app/places_seed.py's reconcile flag) AND either always-active
(rotates=0) or in this week's resolved rotating set (app/place_rotation.
resolve_week) -- never a place that has left the seed, and never a
rotating place that is not currently drawable, so the map can never show
a marker a player cannot actually score.

/api/places also carries `park_boundaries`: GeoJSON outlines for the
parks large enough that a single circle marker undersells them (a
matched PAD-US boundary at or above one grid cell -- see
_park_boundaries_in_viewport below). Rendering only, gated by the
`zoom` query param -- see that route's docstring; the >50%-of-cell
scoring rule these parks use is computed once at seed time
(app/places_seed.py) and never re-derived from this endpoint.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from shapely import wkt as shapely_wkt
from shapely.geometry import mapping as shapely_mapping

from .db import connect
from .grid import distance_m
from .place_rotation import current_week_start, resolve_week

router = APIRouter()

# A viewport at low zoom over the whole play area could otherwise ask
# for tens of thousands of markers; MapLibre has no server-side
# clustering here (see frontend/map2.js), so this is the ceiling that
# keeps a zoomed-out view from shipping the entire board in one
# response. The frontend is expected to zoom in for the long tail
# rather than this endpoint silently truncating an already-reasonable
# request.
MAX_VIEWPORT_RESULTS = 2000
MAX_NEAR_RESULTS = 100
DEFAULT_NEAR_RESULTS = 20

# Tiebreak for the two capped, points-ordered queries below (viewport
# markers and park boundaries). `points DESC` alone is not a total
# order -- every SOTA summit is worth the same 100 points (docs/features/
# places.md), so SQLite falls back to breaking the tie by insertion
# order, which follows app/reference/places_worth_going.csv, which is
# sorted by SOTA association code (W0C Colorado ... W7Y Wyoming -- see
# app/places_seed.py's US_SOTA_ASSOCIATIONS). A zoomed-out viewport
# capped at MAX_VIEWPORT_RESULTS then silently kept everything up to
# about Oregon and dropped the alphabetic tail -- Utah, Washington,
# Wyoming, most of Nevada -- not because they are less deserving, but
# because of where their state code falls in the alphabet.
#
# The fix: break the tie with a deterministic scramble of `id` instead
# of `id` itself. Multiplicative hashing (Knuth's constant, the odd
# 32-bit-ish multiplier below, reduced mod a large prime) decorrelates
# the tiebreak order from insertion order -- and insertion order is
# exactly what carries the state-code clustering -- without needing a
# registered SQLite UDF or a second query pass: it's one arithmetic
# expression SQLite can evaluate inline while sorting the (already
# viewport-filtered, so already small) row set. Same `id` always hashes
# to the same value, so the same viewport always returns the same
# subset (no flicker while panning) and the same order (stable paging),
# while a capped, zoomed-out view now thins evenly across whatever
# states/regions are actually in it instead of amputating whichever one
# sorts last.
def _stable_tiebreak(id_column: str) -> str:
    return f"(({id_column} * 2654435761) % 1000000007)"


# Park boundaries (docs/features/places.md's "boundary-backed" parks --
# a matched PAD-US polygon at or above one grid cell, app/places_seed.py's
# geom + rotates=0 combination). These are the only places carrying a
# `geom` column at all, and WKT is heavy (the seed generator clips each
# one to roughly 6km around the park's own point, but PAD-US polygons
# can still run to hundreds of vertices) -- so boundary geometry is only
# ever sent when the caller says it is looking at a zoom where an
# outline would actually read as one (frontend/map2.js's
# PLACE_TYPE_MIN_ZOOM already gates the park MARKER the same way, at
# zoom 10; outlines wait one step further in, since a 6km polygon is
# still a speck at a whole-region zoom).
MIN_BOUNDARY_ZOOM = 11

# A capped, points-ordered slice, same reasoning as MAX_VIEWPORT_RESULTS
# above -- a boundary feature costs far more bytes than a point marker,
# so the ceiling is much lower.
MAX_BOUNDARY_RESULTS = 300

# Padding added to the marker viewport query when fetching boundaries:
# a park's `geom` is clipped to ~6km around its own point (see
# docs/features/places.md), so a park whose POINT sits just outside the
# viewport can still have boundary poking into it. ~0.07 degrees is
# generous for that at every latitude this play area covers (comfortably
# more than 6km of longitude even at the box's southernmost edge) --
# better to over-fetch a little than clip a boundary at the map edge.
BOUNDARY_VIEWPORT_MARGIN_DEG = 0.07

# Visual-only simplification of the boundary polygon before it goes out
# over the wire -- roughly 10m, well under anything visible at the
# zooms these draw (MIN_BOUNDARY_ZOOM and up). Scoring never reads this
# endpoint's output: the >50%-of-cell containment test was already
# computed once, at seed time, from the UNSIMPLIFIED geometry
# (app/places_seed.py's _park_cells -> place_cell), so simplifying here
# cannot move a single square's score.
BOUNDARY_SIMPLIFY_TOLERANCE_DEG = 0.0001


def _live_where(week_start: str) -> str:
    return (
        "p.active = 1 AND (p.rotates = 0 OR EXISTS ("
        "  SELECT 1 FROM place_week w WHERE w.week_start = ? AND w.place_id = p.id))"
    )


def _row_to_place(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "type": r["ref_type"],
        "name": r["name"],
        "lat": r["lat"],
        "lon": r["lon"],
        "points": r["points"],
        "rotates": bool(r["rotates"]),
    }


def _park_boundaries_in_viewport(
    conn: sqlite3.Connection, north: float, south: float, west: float, east: float
) -> list[dict]:
    """GeoJSON Feature list for boundary-backed parks touching a padded
    viewport -- `geom IS NOT NULL AND rotates = 0` is exactly the set
    app/places_seed.py wrote a boundary AND classified as at-or-above
    one grid cell (a matched park below one cell, or an unmatched one,
    is either rotates=1 or geom NULL -- see that module's
    load_places_seed). No active/week filter is needed here the way
    _live_where needs one for markers: a boundary-backed park is
    rotates=0 (always active) by definition, so the only liveness gate
    left is place.active itself.
    """
    rows = conn.execute(
        "SELECT id, name, points, geom FROM place "
        " WHERE ref_type = 'park' AND geom IS NOT NULL AND rotates = 0 AND active = 1 "
        "   AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? "
        f" ORDER BY points DESC, {_stable_tiebreak('id')} LIMIT ?",
        (
            south - BOUNDARY_VIEWPORT_MARGIN_DEG, north + BOUNDARY_VIEWPORT_MARGIN_DEG,
            west - BOUNDARY_VIEWPORT_MARGIN_DEG, east + BOUNDARY_VIEWPORT_MARGIN_DEG,
            MAX_BOUNDARY_RESULTS,
        ),
    ).fetchall()

    features = []
    for r in rows:
        try:
            geom = shapely_wkt.loads(r["geom"])
            geom = geom.simplify(BOUNDARY_SIMPLIFY_TOLERANCE_DEG, preserve_topology=True)
        except Exception:
            # A malformed geom must never take the whole viewport
            # response down with it -- skip just this one boundary; the
            # park itself still draws as a marker from the places list.
            continue
        features.append({
            "type": "Feature",
            "properties": {"id": r["id"], "name": r["name"], "points": r["points"]},
            "geometry": shapely_mapping(geom),
        })
    return features


@router.get("/api/places")
async def places_in_viewport(
    north: float = Query(...),
    south: float = Query(...),
    west: float = Query(...),
    east: float = Query(...),
    zoom: float | None = None,
) -> JSONResponse:
    """Live places inside a map viewport -- always-active plus this
    week's live rotating set, capped at MAX_VIEWPORT_RESULTS and ordered
    by points (highest first, so a capped view drops the least valuable
    markers first) then by _stable_tiebreak (see that function) so a
    capped, zoomed-out view thins evenly across the viewport instead of
    keeping whichever places happen to sort first within a points tier.

    `zoom` only ever gates one thing server-side: whether the response
    also carries `park_boundaries`, the GeoJSON outlines for
    boundary-backed parks (docs/features/places.md; see
    _park_boundaries_in_viewport). Everything else about WHICH markers
    come back is still zoom-blind -- no clustering, no per-zoom
    thinning of the points themselves -- the frontend already gates
    which tiers/labels are visible by zoom (frontend/map2.js) without
    needing the server to know it. `zoom` is optional and omitting it
    (any existing caller) just means no boundaries: `park_boundaries`
    comes back as an empty FeatureCollection, never absent, so a
    frontend can always read it the same way.
    """
    if south > north or west > east:
        return JSONResponse({"error": "invalid bbox"}, status_code=400)

    week_start = current_week_start()
    conn = connect()
    try:
        resolve_week(conn, week_start)
        rows = conn.execute(
            "SELECT p.id, p.ref_type, p.name, p.lat, p.lon, p.points, p.rotates "
            "  FROM place p "
            " WHERE p.lat BETWEEN ? AND ? AND p.lon BETWEEN ? AND ? "
            f"  AND {_live_where(week_start)} "
            f" ORDER BY p.points DESC, {_stable_tiebreak('p.id')} LIMIT ?",
            (south, north, west, east, week_start, MAX_VIEWPORT_RESULTS),
        ).fetchall()
        boundary_features = (
            _park_boundaries_in_viewport(conn, north, south, west, east)
            if zoom is not None and zoom >= MIN_BOUNDARY_ZOOM
            else []
        )
    finally:
        conn.close()

    return JSONResponse({
        "week_start": week_start,
        "park_boundaries": {"type": "FeatureCollection", "features": boundary_features},
        "count": len(rows),
        "truncated": len(rows) >= MAX_VIEWPORT_RESULTS,
        "places": [_row_to_place(r) for r in rows],
    })


@router.get("/api/places/near")
async def places_near(
    lat: float = Query(...),
    lon: float = Query(...),
    limit: int = Query(DEFAULT_NEAR_RESULTS, ge=1, le=MAX_NEAR_RESULTS),
) -> JSONResponse:
    """Live places sorted by distance from (lat, lon), for map2's
    slide-out panel -- see that page's module docstring. Distance is
    computed in Python via app/grid.distance_m over every live place
    (tens of thousands of rows, well within what a single request can
    sort in memory) rather than in SQL, matching how the rest of this
    codebase treats distance (app/places.py, app/grid.py) -- no spatial
    extension is assumed to be compiled into this build's SQLite.
    """
    week_start = current_week_start()
    conn = connect()
    try:
        resolve_week(conn, week_start)
        rows = conn.execute(
            "SELECT p.id, p.ref_type, p.name, p.lat, p.lon, p.points, p.rotates "
            "  FROM place p "
            f" WHERE {_live_where(week_start)}",
            (week_start,),
        ).fetchall()
    finally:
        conn.close()

    ranked = sorted(
        ({"place": r, "distance_m": distance_m(lat, lon, r["lat"], r["lon"])} for r in rows),
        key=lambda x: x["distance_m"],
    )[:limit]

    return JSONResponse({
        "week_start": week_start,
        "places": [
            {**_row_to_place(x["place"]), "distance_m": round(x["distance_m"], 1)}
            for x in ranked
        ],
    })
