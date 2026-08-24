"""Tests for the Explorer/Frontier redefinition in app/results.py
(docs/features/places.md, "What it changes about the honors").

Explorer used to mean "most squares nobody had ever claimed" -- a proxy
for exploring, back when Places Worth Going did not exist to measure it
directly. It now means "most Explorer Score points earned this month"
(points from place_activation). Frontier keeps counting squares beyond
city limits but drops the virgin-ground restriction that used to make
it a strict subset of the old Explorer.
"""
from __future__ import annotations

import time

from app import results
from app.grid import cell_id


NOW = int(time.time())
MONTH = results.month_key(NOW)
START, END = results.month_bounds(MONTH)


def _player(conn, player_id, team, name=None):
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?,?,?,?)",
        (player_id, name or f"player-{player_id}", team, NOW),
    )


def _season(conn, protocol, started_at=0, ends_at=None):
    ends_at = ends_at if ends_at is not None else NOW + 10_000_000
    cur = conn.execute(
        "INSERT INTO mc_season(protocol, started_at, ends_at, status) VALUES (?,?,?,?)",
        (protocol, started_at, ends_at, "active"),
    )
    return cur.lastrowid


def _capture(conn, season_id, cell, ts, player_id, team, from_team=None, by_air=0):
    conn.execute(
        "INSERT INTO mc_tile_capture_log(season_id, cell_id, ts, by_player_id, by_team, "
        "from_team, by_air) VALUES (?,?,?,?,?,?,?)",
        (season_id, cell, ts, player_id, team, from_team, by_air),
    )


def _place_activation(conn, place_id, player_id, points, awarded_at, week_start="2026-01-07"):
    # place_activation has no foreign-key enforcement in the test schema's
    # in-memory connection (see conftest.py), so a bare place_id is fine --
    # nothing here reads app/place.
    conn.execute(
        "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at) "
        "VALUES (?,?,?,?,?)",
        (place_id, player_id, week_start, points, awarded_at),
    )


def _award(awards, key):
    return next((a for a in awards if a["award"] == key), None)


def test_explorer_ranks_by_place_points(conn):
    """Explorer goes to the player with the most place_activation points
    THIS MONTH, not the most captures -- and a player with more captures
    but fewer place points must not win it.
    """
    _player(conn, 1, "RED")
    _player(conn, 2, "BLUE")
    season_id = _season(conn, "mt")

    # Player 1: fewer place points, but more virgin captures (would have
    # won the OLD Explorer).
    _capture(conn, season_id, cell_id(43.0, -116.0), START + 10, 1, "RED", from_team=None)
    _capture(conn, season_id, cell_id(43.1, -116.1), START + 20, 1, "RED", from_team=None)
    _place_activation(conn, 1, player_id=1, points=5, awarded_at=START + 30)

    # Player 2: no captures at all, but more place points.
    _place_activation(conn, 2, player_id=2, points=25, awarded_at=START + 40)
    _place_activation(conn, 3, player_id=2, points=100, awarded_at=START + 50)

    result = results.compute_month(conn, "mt", MONTH)
    explorer = _award(result["awards"], "explorer")
    assert explorer is not None
    assert explorer["player_id"] == 2
    assert explorer["value"] == 125
    assert explorer["detail"] == "points earned from places"


def test_explorer_ignores_place_points_outside_the_month(conn):
    _player(conn, 1, "RED")
    _season(conn, "mt")
    _place_activation(conn, 1, player_id=1, points=100, awarded_at=START - 1)  # before the month
    _place_activation(conn, 2, player_id=1, points=100, awarded_at=END)  # on/after the month ends

    result = results.compute_month(conn, "mt", MONTH)
    assert _award(result["awards"], "explorer") is None


def test_frontier_counts_out_of_town_captures_without_virgin_restriction(conn, monkeypatch):
    """Frontier no longer requires a virgin (from_team is None) claim --
    an attack or a retake out past the towns must count too.
    """
    _player(conn, 1, "RED")
    _player(conn, 2, "BLUE")
    season_id = _season(conn, "mt")

    far_cell = cell_id(45.0, -114.0)
    near_cell = cell_id(45.0, -114.5)

    def fake_distance(lat, lon):
        return 999_999.0 if cell_id(lat, lon) == far_cell else 10.0  # metres

    monkeypatch.setattr(results.places, "distance_to_nearest_town_m", fake_distance)

    # Player 1: an ATTACK (from_team set) on a far-out square -- would NOT
    # have counted under the old virgin-only rule.
    _capture(conn, season_id, far_cell, START + 10, 1, "RED", from_team="BLUE")
    # Player 2: a virgin claim, but inside town -- must not count.
    _capture(conn, season_id, near_cell, START + 20, 2, "BLUE", from_team=None)

    result = results.compute_month(conn, "mt", MONTH)
    frontier = _award(result["awards"], "frontier")
    assert frontier is not None
    assert frontier["player_id"] == 1
    assert frontier["value"] == 1


def test_frozen_month_is_not_recomputed(conn):
    """A month already frozen (month_result/month_award rows exist) must
    keep the numbers it was frozen with, even if new place_activation or
    capture rows land inside that month afterward.
    """
    _player(conn, 1, "RED")
    season_id = _season(conn, "mt")
    _place_activation(conn, 1, player_id=1, points=5, awarded_at=START + 10)

    results.freeze_month(conn, "mt", MONTH, NOW)
    frozen = results.month_results_for(conn, "mt", now=results.month_bounds(MONTH)[1] + 1)
    # Sanity: our frozen month is actually the one returned.
    stored = next(m for m in frozen["months"] if m["month"] == MONTH)
    before = _award(stored["awards"], "explorer")
    assert before is not None and before["value"] == 5

    # New activity lands in the same, already-frozen month.
    _place_activation(conn, 2, player_id=1, points=100, awarded_at=START + 20)
    _player(conn, 2, "BLUE")
    _capture(conn, season_id, cell_id(43.0, -116.0), START + 30, 2, "BLUE", from_team=None)

    # maybe_roll_months must not re-freeze a month that already has a result.
    rolled = results.maybe_roll_months(conn, now=results.month_bounds(MONTH)[1] + 1, protocol="mt")
    assert rolled == 0

    after = results.month_results_for(conn, "mt", now=results.month_bounds(MONTH)[1] + 1)
    stored_after = next(m for m in after["months"] if m["month"] == MONTH)
    still = _award(stored_after["awards"], "explorer")
    assert still is not None and still["value"] == 5  # unchanged -- history is history
