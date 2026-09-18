"""Regression coverage for the missing protocol filter on
place_activation this pass fixes (app/mc_scoring.py's
team_place_points()/team_totals()).

place_activation carries a protocol column (app/db.py's CREATE TABLE
comment), but team_place_points() used to sum every row inside a
season's time window regardless of which board earned it -- so both
'mc' and 'mt' team standings reported an identical, doubled Explorer
figure. team_place_points() now takes `protocol` as a required
argument (no default -- see its own docstring for why) and filters on
it; team_totals() threads the same value through.
"""
from __future__ import annotations

import time

from app import mc_scoring

NOW = int(time.time())


def _player(conn, player_id, team, name=None):
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?,?,?,?)",
        (player_id, name or f"player-{player_id}", team, NOW),
    )


def _season(conn, protocol, started_at=None, ends_at=None, status="active"):
    started_at = NOW - 1000 if started_at is None else started_at
    ends_at = NOW + 1000 if ends_at is None else ends_at
    cur = conn.execute(
        "INSERT INTO mc_season(protocol, started_at, ends_at, status) VALUES (?,?,?,?)",
        (protocol, started_at, ends_at, status),
    )
    return cur.lastrowid


def _place_activation(conn, place_id, player_id, points, awarded_at, protocol,
                       week_start="2026-01-07"):
    conn.execute(
        "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at, protocol) "
        "VALUES (?,?,?,?,?,?)",
        (place_id, player_id, week_start, points, awarded_at, protocol),
    )


def test_team_place_points_for_one_protocol_excludes_the_others_activations(conn):
    _player(conn, 1, "RED")
    mc_season_id = _season(conn, "mc")

    _place_activation(conn, 1, player_id=1, points=10, awarded_at=NOW, protocol="mc")
    _place_activation(conn, 2, player_id=1, points=40, awarded_at=NOW, protocol="mt")

    assert mc_scoring.team_place_points(conn, mc_season_id, "mc") == {"RED": 10.0}


def test_team_totals_combined_figure_is_also_protocol_scoped(conn):
    """team_totals() (squares + check-ins + Explorer) must not fold the
    other board's Explorer points into this one's combined standing --
    see this function's own docstring on why it threads `protocol`
    through to team_place_points() rather than defaulting it.
    """
    _player(conn, 1, "RED")
    mc_season_id = _season(conn, "mc")

    _place_activation(conn, 1, player_id=1, points=10, awarded_at=NOW, protocol="mc")
    _place_activation(conn, 2, player_id=1, points=40, awarded_at=NOW, protocol="mt")

    totals = mc_scoring.team_totals(conn, mc_season_id, "mc")
    assert totals["RED"] == 10.0
