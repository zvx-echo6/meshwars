"""Tests for app/places_api.py's active-flag filtering: an inactive
place (app/places_seed.py's reconcile flag, set when a place leaves the
seed) must never appear in the viewport or "near here" panel response,
even when a stale place_week row still points at it (a rotating place
drawn earlier in the week, then deactivated by a later seed reload).
"""
from __future__ import annotations

import asyncio
import json
import time

import app.places_api as places_api_module
from app.place_rotation import current_week_start
from app.places_api import places_in_viewport, places_near

WEEK = current_week_start()


def _place(conn, place_id, ref_type, lat, lon, points, rotates=0, active=1):
    conn.execute(
        "INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, "
        "rotates, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (place_id, ref_type, f"ref-{place_id}", f"place-{place_id}", lat, lon,
         points, "TEST", rotates, active, int(time.time())),
    )


def test_inactive_place_excluded_from_viewport(conn, monkeypatch):
    monkeypatch.setattr(places_api_module, "connect", lambda: conn)

    _place(conn, 1, "summit", 43.0, -116.0, points=100, rotates=0, active=1)
    _place(conn, 2, "summit", 43.01, -116.01, points=100, rotates=0, active=0)

    result = asyncio.run(places_in_viewport(north=44.0, south=42.0, west=-117.0, east=-115.0))
    ids = {p["id"] for p in json.loads(result.body)["places"]}
    assert ids == {1}


def test_inactive_place_excluded_even_with_a_stale_place_week_row(conn, monkeypatch):
    """A rotating place drawn into this week's place_week, then
    deactivated by a later seed reload, must not still show up just
    because place_week (append-only, never rewritten) still names it.
    """
    monkeypatch.setattr(places_api_module, "connect", lambda: conn)

    _place(conn, 1, "landmark", 43.0, -116.0, points=5, rotates=1, active=0)
    _place(conn, 2, "landmark", 43.02, -116.02, points=5, rotates=1, active=1)
    conn.execute("INSERT INTO place_week(week_start, place_id) VALUES (?, ?)", (WEEK, 1))
    conn.execute("INSERT INTO place_week(week_start, place_id) VALUES (?, ?)", (WEEK, 2))

    result = asyncio.run(places_in_viewport(north=44.0, south=42.0, west=-117.0, east=-115.0))
    ids = {p["id"] for p in json.loads(result.body)["places"]}
    assert ids == {2}


def test_inactive_place_excluded_from_near_panel(conn, monkeypatch):
    monkeypatch.setattr(places_api_module, "connect", lambda: conn)

    _place(conn, 1, "landmark", 43.0, -116.0, points=5, rotates=0, active=1)
    _place(conn, 2, "landmark", 43.001, -116.001, points=5, rotates=0, active=0)

    result = asyncio.run(places_near(lat=43.0, lon=-116.0, limit=20))
    ids = {p["id"] for p in json.loads(result.body)["places"]}
    assert ids == {1}


def test_capped_viewport_thins_evenly_not_by_insertion_order(conn, monkeypatch):
    """The bug this endpoint shipped with: every SOTA summit is worth
    the same 100 points, so `ORDER BY points DESC` alone is not a total
    order and SQLite broke the tie by insertion order -- which, in
    production, follows the seed CSV's SOTA-association sort (W0C
    Colorado ... W7Y Wyoming, see app/places_seed.py). A capped,
    zoomed-out viewport then kept everything up to about Oregon and
    silently dropped the alphabetic tail.

    Reproduced here with two equal-sized, equal-points "regions" --
    ids 1-100 inserted first, ids 101-200 inserted second, all tied on
    points, all in the same viewport -- and a cap below the combined
    total. The old `ORDER BY p.points DESC LIMIT ?` (no further
    tiebreak) would return ids 1-50 only: pure insertion order, one
    region entirely and the other not at all. The fix's tiebreak
    (_stable_tiebreak, a deterministic hash of id) must scatter the
    truncated result across BOTH regions instead.
    """
    monkeypatch.setattr(places_api_module, "connect", lambda: conn)
    monkeypatch.setattr(places_api_module, "MAX_VIEWPORT_RESULTS", 50)

    for i in range(1, 101):
        _place(conn, i, "summit", 43.0, -116.0, points=100)
    for i in range(101, 201):
        _place(conn, i, "summit", 43.0, -116.0, points=100)

    result = asyncio.run(places_in_viewport(north=44.0, south=42.0, west=-117.0, east=-115.0))
    data = json.loads(result.body)
    ids = [p["id"] for p in data["places"]]

    assert data["count"] == 50
    assert data["truncated"] is True
    assert any(i <= 100 for i in ids), "first-inserted region must not be the only one dropped"
    assert any(i > 100 for i in ids), "second-inserted region must not be entirely truncated away"


def test_capped_viewport_is_deterministic_across_repeated_calls(conn, monkeypatch):
    """Same viewport, same tied-points rows -> same truncated subset in
    the same order every time. A capped view that reshuffled on every
    call would make markers flicker as a player pans the map -- the
    tiebreak must be a pure function of `id`, never randomness.
    """
    # places_in_viewport closes whatever connect() hands it when the
    # request finishes -- fine against a real per-request connection,
    # but this test calls it twice against one shared in-memory `conn`
    # fixture, so the close() after call one would leave call two with
    # a dead handle. A thin non-closing wrapper sidesteps that without
    # weakening what's under test: the SQL and its tiebreak are exactly
    # `conn`'s, only lifecycle management differs.
    class _NonClosingConn:
        def __getattr__(self, name):
            return getattr(conn, name)

        def close(self):
            pass

    monkeypatch.setattr(places_api_module, "connect", lambda: _NonClosingConn())
    monkeypatch.setattr(places_api_module, "MAX_VIEWPORT_RESULTS", 50)

    for i in range(1, 201):
        _place(conn, i, "summit", 43.0, -116.0, points=100)

    result_a = asyncio.run(places_in_viewport(north=44.0, south=42.0, west=-117.0, east=-115.0))
    result_b = asyncio.run(places_in_viewport(north=44.0, south=42.0, west=-117.0, east=-115.0))

    ids_a = [p["id"] for p in json.loads(result_a.body)["places"]]
    ids_b = [p["id"] for p in json.loads(result_b.body)["places"]]
    assert ids_a == ids_b


def test_park_boundaries_also_thin_evenly_not_by_insertion_order(conn, monkeypatch):
    """_park_boundaries_in_viewport has the identical points-tie flaw at
    its own MAX_BOUNDARY_RESULTS cap -- same fix, same proof shape as
    the viewport-markers test above, just with boundary-backed parks
    (geom set, rotates=0) instead of summits.
    """
    monkeypatch.setattr(places_api_module, "connect", lambda: conn)
    monkeypatch.setattr(places_api_module, "MAX_BOUNDARY_RESULTS", 20)

    point_wkt = "POLYGON((-116.1 42.9,-116.1 43.1,-115.9 43.1,-115.9 42.9,-116.1 42.9))"
    for i in range(1, 41):
        conn.execute(
            "INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, "
            "geom, rotates, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, "park", f"ref-{i}", f"park-{i}", 43.0, -116.0, 10, "TEST",
             point_wkt, 0, 1, int(time.time())),
        )

    features = places_api_module._park_boundaries_in_viewport(
        conn, north=44.0, south=42.0, west=-117.0, east=-115.0
    )
    ids = [f["properties"]["id"] for f in features]

    assert len(ids) == 20
    assert any(i <= 20 for i in ids), "first-inserted half must not be the only one dropped"
    assert any(i > 20 for i in ids), "second-inserted half must not be entirely truncated away"
