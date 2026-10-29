"""cell_detail_for() and api._cell_score_map() show per-team scores decayed
to now with mc_scoring.decayed_score (display only; stored data untouched)."""
from __future__ import annotations

import time

import pytest

import app.api as api_module
import app.mc_api as mc_api_module
from app.config import settings
from app.mc_api import cell_detail_for

NOW = int(time.time())
CELL = "10000_-10000"
DAY = 86400


def _setup(conn):
    sid = conn.execute(
        "INSERT INTO mc_season(protocol, started_at, ends_at, status) VALUES ('mc',?,?,'active')",
        (NOW - 10 * DAY, NOW + 10 * DAY),
    ).lastrowid
    conn.execute(
        "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts) "
        "VALUES (?,?,?,1,?)", (sid, CELL, "RED", NOW),
    )
    return sid


def _score(conn, sid, team, score, age_days):
    conn.execute(
        "INSERT INTO mc_tile_score(season_id, cell_id, team, score, last_update) VALUES (?,?,?,?,?)",
        (sid, CELL, team, score, int(NOW - age_days * DAY)),
    )


def test_popup_scores_are_decayed(conn, monkeypatch):
    monkeypatch.setattr(mc_api_module, "connect", lambda: conn)
    sid = _setup(conn)
    _score(conn, sid, "RED", 1.0, 1)
    _score(conn, sid, "YELLOW", 0.2595, 4)
    # cell_detail_for closes the connection it is given, so read the
    # stored row first: display must not modify stored data.
    stored_before = conn.execute(
        "SELECT score FROM mc_tile_score WHERE team='YELLOW'").fetchone()["score"]
    assert stored_before == 0.2595
    d = cell_detail_for("mc", CELL)
    per_day = settings.mc_score_decay_per_day
    assert d["scores"]["RED"] == pytest.approx(1.0 - per_day, abs=0.01)
    assert d["scores"]["YELLOW"] == 0.0
    assert d["owner_team"] == "RED"


def test_score_map_is_decayed(conn):
    sid = _setup(conn)
    _score(conn, sid, "RED", 1.0, 1)
    _score(conn, sid, "YELLOW", 0.2595, 4)
    m = api_module._cell_score_map(conn, sid)
    assert m[CELL]["YELLOW"] == 0.0
    assert m[CELL]["RED"] == pytest.approx(1.0 - settings.mc_score_decay_per_day, abs=0.01)
