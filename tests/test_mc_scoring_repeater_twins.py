"""Prefix-twin repeater ids (same repeater at two hash widths) are credited
once per cooldown window (app/mc_scoring.py apply_paint, protocol 'mc')."""
from __future__ import annotations

import time

import pytest

from app import mc_scoring
from app.config import settings

NOW = int(time.time())
CELL = "10000_-10000"


def _paint(conn, sid, ids, ts, player=1):
    return mc_scoring.apply_paint(
        conn, sid, player, "RED", CELL, ts, ids,
        settings.mc_points_per_repeater, settings.mc_max_points_per_ping, "mc", ts,
    )


@pytest.fixture
def sid(conn):
    conn.execute("BEGIN IMMEDIATE")
    s = mc_scoring.ensure_active_season(conn, NOW, "mc")
    conn.execute("COMMIT")
    return s


def _score(conn, sid):
    r = conn.execute("SELECT score FROM mc_tile_score WHERE season_id=? AND cell_id=?", (sid, CELL)).fetchone()
    return r["score"] if r else 0.0


def test_twin_in_window_credited_once(conn, sid):
    _paint(conn, sid, ["f0ab"], NOW)
    s1 = _score(conn, sid)
    res = _paint(conn, sid, ["f0ab31"], NOW + 100)
    assert res.outcome == "cooldown" if hasattr(res, "outcome") else True
    assert _score(conn, sid) == pytest.approx(s1)
    ids = {r["repeater_id"] for r in conn.execute("SELECT repeater_id FROM player_cell_repeater_credit")}
    assert ids == {"f0ab"}


def test_twin_after_window_credited_again(conn, sid):
    _paint(conn, sid, ["f0ab"], NOW)
    s1 = _score(conn, sid)
    _paint(conn, sid, ["f0ab31"], NOW + settings.mc_cooldown_seconds + 5)
    assert _score(conn, sid) == pytest.approx(s1 + settings.mc_points_per_repeater, abs=0.001)


def test_two_hex_ids_are_not_twins(conn, sid):
    _paint(conn, sid, ["6e", "6edf"], NOW)
    ids = {r["repeater_id"] for r in conn.execute("SELECT repeater_id FROM player_cell_repeater_credit")}
    assert ids == {"6e", "6edf"}


def test_twins_within_one_ping_keep_longest(conn, sid):
    _paint(conn, sid, ["f0ab", "f0ab31", "cc3c1a"], NOW)
    ids = {r["repeater_id"] for r in conn.execute("SELECT repeater_id FROM player_cell_repeater_credit")}
    assert ids == {"f0ab31", "cc3c1a"}


def test_unrelated_ids_unaffected_and_cap_holds(conn, sid):
    ids = [f"{i:04x}ab" for i in range(1, 15)]
    _paint(conn, sid, ids, NOW)
    n = conn.execute("SELECT COUNT(*) c FROM player_cell_repeater_credit").fetchone()["c"]
    assert n == 10
    first = _score(conn, sid)
    assert first == pytest.approx(settings.mc_max_points_per_ping + settings.mc_score_per_unique_player, abs=0.001)


def test_twin_helpers():
    assert mc_scoring.repeaters_are_twins("F0AB", "f0ab31")
    assert not mc_scoring.repeaters_are_twins("f0", "f0ab31")
    assert not mc_scoring.repeaters_are_twins("f0ab", "f1ab31")
