"""Tests for cell_detail_for()'s `park` field (app/mc_api.py's
_containing_park): the cell popup naming the boundary-backed park a
painted square sits inside, per the same >50%-of-cell rule
place_cell already encodes (docs/features/places.md,
app/places_seed.py's _park_cells) -- not a re-derived geometry test.
"""
from __future__ import annotations

import time

import app.mc_api as mc_api_module
from app.mc_api import _containing_park, cell_detail_for

NOW = int(time.time())
CELL = "10000_-10000"


def _season(conn, protocol="mc"):
    cur = conn.execute(
        "INSERT INTO mc_season(protocol, started_at, ends_at, status) VALUES (?,?,?,?)",
        (protocol, NOW - 1000, NOW + 1_000_000, "active"),
    )
    return cur.lastrowid


def _tile(conn, season_id, cell_id, team="RED"):
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) "
        "VALUES (1, 'tester', ?, ?) ON CONFLICT(player_id) DO NOTHING",
        (team, NOW),
    )
    conn.execute(
        "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts) "
        "VALUES (?,?,?,1,?)",
        (season_id, cell_id, team, NOW),
    )


def _place(conn, place_id, ref_type, name, points, geom=None, rotates=0, active=1):
    conn.execute(
        "INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, "
        "geom, rotates, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (place_id, ref_type, f"ref-{place_id}", name, 43.0, -114.0, points, "TEST",
         geom, rotates, active, NOW),
    )


def _place_cell(conn, place_id, cell_id):
    conn.execute(
        "INSERT INTO place_cell(place_id, cell_id) VALUES (?, ?)", (place_id, cell_id),
    )


def test_park_field_none_when_cell_is_not_inside_any_boundary_park(conn, monkeypatch):
    monkeypatch.setattr(mc_api_module, "connect", lambda: conn)
    season_id = _season(conn)
    _tile(conn, season_id, CELL)

    detail = cell_detail_for("mc", CELL)
    assert detail["park"] is None


def test_park_field_names_the_boundary_backed_park_covering_the_cell(conn, monkeypatch):
    monkeypatch.setattr(mc_api_module, "connect", lambda: conn)
    season_id = _season(conn)
    _tile(conn, season_id, CELL)

    _place(conn, 1, "park", "Craters of the Moon National Monument", 25, geom="POLYGON EMPTY", rotates=0)
    _place_cell(conn, 1, CELL)

    detail = cell_detail_for("mc", CELL)
    assert detail["park"] == {"id": 1, "name": "Craters of the Moon National Monument", "points": 25}


def test_park_field_ignores_a_place_cell_row_from_a_non_boundary_place(conn, monkeypatch):
    """A summit or landmark's own point can share a cell_id with a
    place_cell row -- that must never be reported as "inside a park";
    only a place with geom (a real matched boundary) qualifies.
    """
    monkeypatch.setattr(mc_api_module, "connect", lambda: conn)
    season_id = _season(conn)
    _tile(conn, season_id, CELL)

    _place(conn, 1, "summit", "Some Summit", 100, geom=None, rotates=0)
    _place_cell(conn, 1, CELL)

    detail = cell_detail_for("mc", CELL)
    assert detail["park"] is None


def test_park_field_ignores_an_inactive_or_rotating_park(conn, monkeypatch):
    monkeypatch.setattr(mc_api_module, "connect", lambda: conn)
    season_id = _season(conn)
    _tile(conn, season_id, CELL)

    _place(conn, 1, "park", "Left The Seed Park", 25, geom="POLYGON EMPTY", rotates=0, active=0)
    _place_cell(conn, 1, CELL)
    _place(conn, 2, "park", "Small Rotating Park", 25, geom="POLYGON EMPTY", rotates=1, active=1)
    _place_cell(conn, 2, CELL)

    detail = cell_detail_for("mc", CELL)
    assert detail["park"] is None


def test_park_field_is_deterministic_when_two_designations_overlap_the_same_ground(conn, monkeypatch):
    """PAD-US carries near-duplicate designations for the same physical
    area (e.g. a state park and a coincident historic site) -- both can
    independently clear 50% of the same cell. Rather than depending on
    sqlite's unspecified row order, this must pick one consistently
    (points DESC, then the same stable hash tiebreak
    app/places_api.py's viewport queries use).
    """
    _place(conn, 1, "park", "Goliad State Park", 25, geom="POLYGON EMPTY", rotates=0)
    _place_cell(conn, 1, CELL)
    _place(conn, 2, "park", "Goliad State Historic Site", 25, geom="POLYGON EMPTY", rotates=0)
    _place_cell(conn, 2, CELL)

    # _containing_park() takes an already-open connection directly (no
    # _safe_query open/close cycle), so this can call it twice against
    # the same rows without needing a second monkeypatched connect().
    first = _containing_park(conn, CELL)
    second = _containing_park(conn, CELL)
    assert first is not None
    assert first == second
