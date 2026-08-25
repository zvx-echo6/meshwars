"""Tests for the two additions to app/mc_api.py this pass makes:

- top_explorer_for(): the Explorer ranking behind Top Operators' new
  third tab, ranked over the active season's window the same way
  top_for()/top_checkin_for() already are, and scoped to one board's
  registered players via player_node.protocol the same way
  public_api._player_rows() scopes its own explorer_points.
- find_for()'s new point breakdown (capture/check-in/Explorer points
  and their total): the only lookup on the site that works for a
  player who is not on any Top Operators ranking, so it must return a
  real breakdown for a player who holds zero cells too, not just for
  one who is currently winning a ranking.
"""
from __future__ import annotations

import time

import app.mc_api as mc_api_module
from app.grid import cell_id
from app.mc_api import find_for, top_explorer_for

NOW = int(time.time())


def _player(conn, player_id, team, name=None):
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?,?,?,?)",
        (player_id, name or f"player-{player_id}", team, NOW),
    )


def _bind_node(conn, player_id, protocol, node_ref):
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) VALUES (?,?,?,?)",
        (protocol, node_ref, player_id, NOW),
    )


def _season(conn, protocol, started_at=0, ends_at=None, status="active"):
    ends_at = ends_at if ends_at is not None else NOW + 10_000_000
    cur = conn.execute(
        "INSERT INTO mc_season(protocol, started_at, ends_at, status) VALUES (?,?,?,?)",
        (protocol, started_at, ends_at, status),
    )
    return cur.lastrowid


def _tile(conn, season_id, cell, team, player_id, ts=None):
    conn.execute(
        "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts) "
        "VALUES (?,?,?,?,?)",
        (season_id, cell, team, player_id, ts or NOW),
    )


def _checkin(conn, season_id, player_id, net_date, points, protocol="mc", streak=1):
    conn.execute(
        "INSERT INTO mc_checkin_award(season_id, player_id, net_date, points, protocol, "
        "message_id, awarded_at, streak) VALUES (?,?,?,?,?,?,?,?)",
        (season_id, player_id, net_date, points, protocol, f"msg-{player_id}-{net_date}", NOW, streak),
    )


def _place_activation(conn, place_id, player_id, points, awarded_at, week_start="2026-01-07"):
    conn.execute(
        "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at) "
        "VALUES (?,?,?,?,?)",
        (place_id, player_id, week_start, points, awarded_at),
    )


# ---- top_explorer_for ----------------------------------------------------

def test_top_explorer_ranks_by_place_points_in_season_window(conn, monkeypatch):
    monkeypatch.setattr(mc_api_module, "connect", lambda: conn)

    _player(conn, 1, "RED", "alice")
    _player(conn, 2, "BLUE", "bob")
    _bind_node(conn, 1, "mc", "!alice")
    _bind_node(conn, 2, "mc", "!bob")
    season_id = _season(conn, "mc", started_at=NOW - 1000, ends_at=NOW + 1000)

    _place_activation(conn, 1, player_id=1, points=10, awarded_at=NOW)
    _place_activation(conn, 2, player_id=2, points=40, awarded_at=NOW)
    _place_activation(conn, 3, player_id=2, points=5, awarded_at=NOW)

    rows = top_explorer_for("mc")
    assert [r["display_name"] for r in rows] == ["bob", "alice"]
    assert rows[0]["points"] == 45
    assert rows[1]["points"] == 10


def test_top_explorer_excludes_activity_outside_the_season_window(conn, monkeypatch):
    monkeypatch.setattr(mc_api_module, "connect", lambda: conn)

    _player(conn, 1, "RED", "alice")
    _bind_node(conn, 1, "mc", "!alice")
    season_id = _season(conn, "mc", started_at=NOW - 100, ends_at=NOW + 100)

    _place_activation(conn, 1, player_id=1, points=99, awarded_at=NOW - 5000, week_start="2026-01-01")

    assert top_explorer_for("mc") == []


def test_top_explorer_is_scoped_to_the_boards_own_players(conn, monkeypatch):
    """A player bound only to a Meshtastic radio must not show up in the
    MeshCore Explorer ranking even though place_activation itself has
    no protocol column -- the isolation has to come from player_node.
    """
    monkeypatch.setattr(mc_api_module, "connect", lambda: conn)

    _player(conn, 1, "RED", "mt-only")
    _bind_node(conn, 1, "mt", "!mt1")
    _season(conn, "mc", started_at=NOW - 100, ends_at=NOW + 100)

    _place_activation(conn, 1, player_id=1, points=50, awarded_at=NOW)

    assert top_explorer_for("mc") == []


def test_top_explorer_empty_with_no_active_season(conn, monkeypatch):
    monkeypatch.setattr(mc_api_module, "connect", lambda: conn)
    assert top_explorer_for("mc") == []


# ---- find_for's point breakdown ------------------------------------------

def test_find_for_breaks_down_points_for_a_player_outside_the_top_rankings(conn, monkeypatch):
    """The entire point of this addition: a player who holds no cells
    (so would never appear in /top, and never in /top-explorer or
    /top-checkins with these small numbers either) must still get a
    real breakdown back from find_for(), not the old all-or-nothing
    "holds no cells right now" with no numbers at all.
    """
    monkeypatch.setattr(mc_api_module, "connect", lambda: conn)

    _player(conn, 1, "RED", "wanderer")
    _bind_node(conn, 1, "mc", "!wanderer")
    season_id = _season(conn, "mc", started_at=NOW - 1000, ends_at=NOW + 1000)

    _checkin(conn, season_id, 1, "2026-08-19", points=5)
    _checkin(conn, season_id, 1, "2026-08-20", points=6, streak=2)
    _place_activation(conn, 1, player_id=1, points=25, awarded_at=NOW)

    result = find_for("mc", "wanderer")
    assert result is not None
    assert result["tiles_held"] == 0
    assert result["bounds"] is None
    assert result["checkin_points"] == 11
    assert result["explorer_points"] == 25
    assert result["total_points"] == 36
    assert result["last_checkin_net_date"] == "2026-08-20"


def test_find_for_includes_capture_points_when_holding_cells(conn, monkeypatch):
    monkeypatch.setattr(mc_api_module, "connect", lambda: conn)

    _player(conn, 1, "RED", "holder")
    _bind_node(conn, 1, "mc", "!holder")
    season_id = _season(conn, "mc", started_at=NOW - 1000, ends_at=NOW + 1000)

    _tile(conn, season_id, cell_id(43.0, -116.0), "RED", 1)
    _tile(conn, season_id, cell_id(43.01, -116.0), "RED", 1)
    _checkin(conn, season_id, 1, "2026-08-19", points=5)
    _place_activation(conn, 1, player_id=1, points=10, awarded_at=NOW)

    result = find_for("mc", "holder")
    assert result["tiles_held"] == 2
    assert result["bounds"] is not None
    assert result["checkin_points"] == 5
    assert result["explorer_points"] == 10
    assert result["total_points"] == 17


def test_find_for_returns_none_for_unregistered_name(conn, monkeypatch):
    monkeypatch.setattr(mc_api_module, "connect", lambda: conn)
    assert find_for("mc", "nobody-by-this-name") is None


def test_find_for_zero_breakdown_with_no_activity(conn, monkeypatch):
    monkeypatch.setattr(mc_api_module, "connect", lambda: conn)

    _player(conn, 1, "RED", "quiet")
    _season(conn, "mc", started_at=NOW - 1000, ends_at=NOW + 1000)

    result = find_for("mc", "quiet")
    assert result["tiles_held"] == 0
    assert result["checkin_points"] == 0
    assert result["explorer_points"] == 0
    assert result["total_points"] == 0
    assert result["last_checkin_net_date"] is None
