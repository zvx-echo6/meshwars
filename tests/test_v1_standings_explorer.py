"""/api/v1/teams and /api/v1/status standings must rank on the full
season score: squares + check-in points + Explorer (Places Worth Going)
points, the same total mc_scoring.team_totals() and mc_api.scores_for()
(the site's scoreboard) use. The API used to leave Explorer out, so it
could rank teams differently from the site on both boards.

Fixture shape follows tests/test_announcements_api.py: a real
file-backed sqlite database and a bare FastAPI app around the real
public_api router.
"""
from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.public_api as public_api_module
from app import mc_api, mc_scoring
from app.db import MIGRATIONS, SCHEMA, connect
from app.mc_ingest import hash_secret

NOW = int(time.time())

BOARDS = [("meshcore", "mc", "mt"), ("meshtastic", "mt", "mc")]


def _init_schema(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                continue
            raise
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "game.db")
    _init_schema(path)
    monkeypatch.setattr(public_api_module.settings, "db_path", path)
    return path


@pytest.fixture
def client(db_path):
    app = FastAPI()
    app.include_router(public_api_module.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_limiters():
    public_api_module._key_cache.clear()
    public_api_module._hits.clear()
    yield
    public_api_module._key_cache.clear()
    public_api_module._hits.clear()


def _api_key(path: str) -> dict:
    raw_key = "test-key-standings"
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO api_client(key_hash, label, created_at) VALUES (?,?,?)",
        (hash_secret(raw_key), "test integration", NOW),
    )
    conn.commit()
    conn.close()
    return {"X-API-Key": raw_key}


def _season(conn, protocol) -> int:
    return conn.execute(
        "INSERT INTO mc_season(protocol, started_at, ends_at, status) VALUES (?,?,?,?)",
        (protocol, NOW - 1000, NOW + 100000, "active"),
    ).lastrowid


def _player(conn, team) -> int:
    return conn.execute(
        "INSERT INTO player(display_name, team, created_at) VALUES (?,?,?)",
        (f"p-{team}-{time.monotonic_ns()}", team, NOW),
    ).lastrowid


def _squares(conn, season_id, team, player_id, n):
    for i in range(n):
        conn.execute(
            "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts) "
            "VALUES (?,?,?,?,?)",
            (season_id, f"{team}-{season_id}-{i}", team, player_id, NOW),
        )


def _checkin(conn, season_id, player_id, protocol, points):
    conn.execute(
        "INSERT INTO mc_checkin_award(season_id, player_id, net_date, points, protocol, "
        "message_id, awarded_at) VALUES (?,?,?,?,?,?,?)",
        (season_id, player_id, "2026-08-19", points, protocol, "m1", NOW),
    )


def _place(conn, player_id, protocol, points, place_id=1):
    conn.execute(
        "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at, protocol) "
        "VALUES (?,?,?,?,?,?)",
        (place_id, player_id, "2026-01-07", points, NOW, protocol),
    )


def _seed(path, explorer: bool, decoy_proto: str | None = None) -> None:
    """Both boards: RED 10 squares + 5 check-in points = 15, BLUE 8
    squares + 2 check-in points = 10. With `explorer`, BLUE also has 20
    Explorer points on its own board (30 total, ahead of RED). With
    `decoy_proto`, a RED player additionally has 500 Explorer points on
    that one protocol only; the tested board must be the OTHER one, so
    the decoy must not count there."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for _, proto, _other in BOARDS:
        sid = _season(conn, proto)
        red, blue = _player(conn, "RED"), _player(conn, "BLUE")
        _squares(conn, sid, "RED", red, 10)
        _squares(conn, sid, "BLUE", blue, 8)
        _checkin(conn, sid, red, proto, 5.0)
        _checkin(conn, sid, blue, proto, 2.0)
        if explorer:
            _place(conn, blue, proto, 20.0, place_id=1)
    if decoy_proto is not None:
        _place(conn, _player(conn, "RED"), decoy_proto, 500.0, place_id=2)
    conn.commit()
    conn.close()


def _site_totals(protocol: str) -> dict:
    return {t["team"]: t for t in mc_api.scores_for(protocol)["teams"]}


@pytest.mark.parametrize("board,proto,other", BOARDS)
def test_explorer_points_change_the_ranking(db_path, client, board, proto, other):
    _seed(db_path, explorer=True, decoy_proto=other)
    resp = client.get(f"/api/v1/teams?board={board}", headers=_api_key(db_path))
    assert resp.status_code == 200
    teams = resp.json()["teams"]
    by_team = {t["team"]: t for t in teams}

    # Squares + check-ins alone put RED (15) ahead of BLUE (10); the
    # Explorer points put BLUE (30) in front.
    assert [t["team"] for t in teams[:2]] == ["BLUE", "RED"]
    assert by_team["BLUE"]["rank"] == 1 and by_team["RED"]["rank"] == 2
    assert by_team["BLUE"]["squares"] == 8
    assert by_team["BLUE"]["checkin_points"] == 2.0
    assert by_team["BLUE"]["explorer_points"] == 20.0
    assert by_team["BLUE"]["total"] == 30.0
    # The other board's 500 Explorer points do not leak in.
    assert by_team["RED"]["explorer_points"] == 0.0
    assert by_team["RED"]["total"] == 15.0

    # Same total as the site's scoreboard and the season-winner sum.
    site = _site_totals(proto)
    conn = connect()
    try:
        season = mc_api.active_season(conn, proto)
        winner_totals = mc_scoring.team_totals(conn, season["id"], proto)
    finally:
        conn.close()
    for t in teams:
        assert t["total"] == pytest.approx(site[t["team"]]["total"])
        assert t["explorer_points"] == pytest.approx(site[t["team"]]["explorer_points"])
        assert t["total"] == pytest.approx(winner_totals.get(t["team"], 0.0))


@pytest.mark.parametrize("board,proto,_other", BOARDS)
def test_zero_explorer_total_is_squares_plus_checkins(db_path, client, board, proto, _other):
    _seed(db_path, explorer=False)
    resp = client.get(f"/api/v1/teams?board={board}", headers=_api_key(db_path))
    teams = resp.json()["teams"]
    by_team = {t["team"]: t for t in teams}

    assert [t["team"] for t in teams[:2]] == ["RED", "BLUE"]
    assert by_team["RED"]["rank"] == 1 and by_team["BLUE"]["rank"] == 2
    for t in teams:
        assert t["explorer_points"] == 0.0
        assert t["total"] == t["squares"] + t["checkin_points"]
    assert by_team["RED"]["total"] == 15.0
    assert by_team["BLUE"]["total"] == 10.0


def test_status_standings_include_explorer_on_both_boards(db_path, client):
    _seed(db_path, explorer=True)
    resp = client.get("/api/v1/status", headers=_api_key(db_path))
    assert resp.status_code == 200
    boards = {b["board"]: b for b in resp.json()["boards"]}
    for board, proto, _ in BOARDS:
        standings = boards[board]["standings"]
        by_team = {t["team"]: t for t in standings}
        assert [t["team"] for t in standings[:2]] == ["BLUE", "RED"]
        assert by_team["BLUE"]["explorer_points"] == 20.0
        assert by_team["BLUE"]["total"] == 30.0
        assert by_team["BLUE"]["rank"] == 1
        assert by_team["RED"]["total"] == 15.0
        site = _site_totals(proto)
        for t in standings:
            assert t["total"] == pytest.approx(site[t["team"]]["total"])
