"""Read routes for "Places Worth Going" (docs/features/places.md):
what frontend/map2.js draws on the map and lists in its slide-out panel.

Not part of the keyed /api/v1 surface (app/public_api.py) -- that is a
deliberately separate, stable contract for external integrators, and
every other route the site's OWN pages call (/get-nodes, /live-tracks,
/api/mc/board, ...) lives outside it too. These are exactly that kind
of route: shaped for map2's own rendering needs, free to change with
the page, and requiring no key, same as /get-nodes.

Both routes only ever return LIVE places -- always-active (rotates=0)
plus this week's resolved rotating set (app/place_rotation.
resolve_week) -- never a rotating place that is not currently drawable,
so the map can never show a marker a player cannot actually score.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

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


def _live_where(week_start: str) -> str:
    return (
        "(p.rotates = 0 OR EXISTS ("
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


@router.get("/api/places")
async def places_in_viewport(
    north: float = Query(...),
    south: float = Query(...),
    west: float = Query(...),
    east: float = Query(...),
) -> JSONResponse:
    """Live places inside a map viewport -- always-active plus this
    week's live rotating set, capped at MAX_VIEWPORT_RESULTS and
    ordered by points (highest first) so a capped view drops the least
    valuable markers, not an arbitrary tail.

    `zoom` is deliberately not a parameter here: nothing server-side
    varies by it today (no clustering, no per-zoom thinning) -- the
    brief's "bbox + zoom" is honored on the frontend instead, which
    already gates whether it draws labels and which tiers are visible
    by zoom (frontend/map2.js) without needing the server to know it.
    Kept as a documented non-parameter rather than a silently accepted
    and ignored one.
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
            " ORDER BY p.points DESC LIMIT ?",
            (south, north, west, east, week_start, MAX_VIEWPORT_RESULTS),
        ).fetchall()
    finally:
        conn.close()

    return JSONResponse({
        "week_start": week_start,
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
