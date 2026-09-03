"""Tests for the four player-facing data routes app/account_api.py adds
in this pass: GET /api/account/stats, /honors, /checkins, and
/checkin-health -- each scoped to the signed-in account's own linked
player.

Same "FastAPI-around-one-router" + file-backed sqlite fixture shape
tests/test_account_api.py already uses (see that file's own module
docstring for why file-backed, not ":memory:"): the routes under test
read/write through app/db.py's connect()/WriteSession, a fresh
connection per call, so an in-memory db would not share data between
them. Fixtures are duplicated here rather than imported from that
file, the same way tests/test_mc_api_player_detail.py keeps its own
local helpers rather than reaching into a sibling mc_api test file.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.db as db
from app.account_api import router as account_router
from app.auth import http_exception_as_error_body
from app.db import MIGRATIONS, SCHEMA
from app.sessions import SESSION_COOKIE_NAME, create_session

NOW = int(time.time())


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
    monkeypatch.setattr(db.settings, "db_path", path)
    return path


class FakePoller:
    """Stand-in for request.app.state.checkin_poller -- the
    checkin-health route only ever calls directory_snapshot() on it,
    the same read-only surface app/admin_ops.py's overview and
    app/checkin_api.py's node picker already use.
    """

    def __init__(self, nodes=None):
        self._nodes = nodes or []

    def directory_snapshot(self, connector_url=None):
        return list(self._nodes)


@pytest.fixture
def client(db_path):
    app = FastAPI()
    app.include_router(account_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    app.state.checkin_poller = FakePoller()
    return TestClient(app)


def _run(coro):
    return asyncio.run(coro)


def _make_account(path: str) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    conn.commit()
    account_id = cur.lastrowid
    conn.close()
    return account_id


def _make_player(path: str, *, account_id=None, display_name="Tester", team="RED") -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO player(display_name, team, created_at, account_id) VALUES (?, ?, ?, ?)",
        (display_name, team, NOW, account_id),
    )
    conn.commit()
    player_id = cur.lastrowid
    conn.close()
    return player_id


def _login(client: TestClient, db_path: str, *, account_id: int | None = None) -> tuple[int, str]:
    if account_id is None:
        account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, device_label="Firefox on Windows"))
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return account_id, raw_token


def _bind_node(path: str, player_id: int, node_ref: str, protocol: str = "mc") -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) VALUES (?,?,?,?)",
        (protocol, node_ref, player_id, NOW),
    )
    conn.commit()
    conn.close()


def _season(path: str, protocol: str = "mc", started_at=0, ends_at=None, status="active") -> int:
    ends_at = ends_at if ends_at is not None else NOW + 10_000_000
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO mc_season(protocol, started_at, ends_at, status) VALUES (?,?,?,?)",
        (protocol, started_at, ends_at, status),
    )
    conn.commit()
    season_id = cur.lastrowid
    conn.close()
    return season_id


def _checkin(path: str, season_id: int, player_id: int, net_date: str, *,
             points=10.0, protocol="mc", streak=None) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO mc_checkin_award(season_id, player_id, net_date, points, protocol, "
        "message_id, awarded_at, streak) VALUES (?,?,?,?,?,?,?,?)",
        (season_id, player_id, net_date, points, protocol,
         f"msg-{player_id}-{net_date}-{protocol}", NOW, streak),
    )
    conn.commit()
    conn.close()


def _month_award(path: str, month: str, protocol: str, award: str, *,
                  player_id=None, team=None, scope="", value=1.0, detail=None) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO month_award(month, protocol, award, scope, player_id, team, value, detail) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (month, protocol, award, scope, player_id, team, value, detail),
    )
    conn.commit()
    conn.close()


def _net(path: str, *, weekday: int, start_hour: int = 0, end_hour: int = 23,
          timezone: str = "America/Boise", start_date: str = "2000-01-01",
          protocol: str = "mc", kind: str = "corescope", enabled: int = 1) -> int:
    """A checkin_net row -- see app/db.py's own comment for the columns.
    Defaults to a full-day (0-23) window so checkin.most_recent_mc_net_date()
    always resolves to "today, local to `timezone`" regardless of what
    wall-clock time a test actually runs at -- see _today_mc_net_date()
    below, which computes the exact same date the same way, for how
    tests pair this up deterministically without mocking time.
    """
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO checkin_net(label, protocol, kind, connector_url, channel, hashtag, "
        "weekday, start_hour, end_hour, timezone, start_date, enabled, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("Test Net", protocol, kind, "http://example.test", "general", "",
         weekday, start_hour, end_hour, timezone, start_date, enabled, NOW),
    )
    conn.commit()
    net_id = cur.lastrowid
    conn.close()
    return net_id


def _today_mc_net_date(timezone: str = "America/Boise") -> tuple[int, str]:
    """(weekday, net_date) for "right now," local to `timezone` -- the
    same pair a full-day _net() row above needs to make
    checkin.most_recent_mc_net_date() resolve to today deterministically,
    computed the identical way that function itself does (weekday() and
    .date().isoformat() off a tz-aware "now"), so a test never has to
    mock time to exercise the credited/not-yet-credited states.
    """
    now_local = datetime.now(ZoneInfo(timezone))
    return now_local.weekday(), now_local.date().isoformat()


def _unresolved(path: str, net_date: str, sender_name: str, *,
                 net_id=1, last_seen=None, message_count=1) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO checkin_unresolved_sender"
        "(net_id, net_date, sender_name, first_seen, last_seen, message_count) "
        "VALUES (?,?,?,?,?,?)",
        (net_id, net_date, sender_name, last_seen or NOW, last_seen or NOW, message_count),
    )
    conn.commit()
    conn.close()


def _node(name: str, pubkey: str) -> dict:
    return {"name": name, "public_key": pubkey}


# ---- auth gate: every route requires a session --------------------------

@pytest.mark.parametrize(
    "path",
    [
        "/api/account/stats",
        "/api/account/honors",
        "/api/account/checkins",
        "/api/account/checkin-health",
    ],
)
def test_every_route_requires_a_session(client, path):
    resp = client.get(path)
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


@pytest.mark.parametrize(
    "path",
    [
        "/api/account/stats",
        "/api/account/honors",
        "/api/account/checkins",
        "/api/account/checkin-health",
    ],
)
def test_every_route_404s_with_no_linked_player(client, db_path, path):
    _login(client, db_path)

    resp = client.get(path)

    assert resp.status_code == 404
    assert resp.json() == {"error": "no linked player"}


# ---- GET /api/account/stats -----------------------------------------------

def test_stats_reuses_find_for_and_adds_streak_and_nets(client, db_path):
    account_id, _ = _login(client, db_path)
    player_id = _make_player(db_path, account_id=account_id, display_name="Wardriver", team="BLUE")
    season_id = _season(db_path, "mc")
    # Three consecutive weekly nets -> a real streak to check against
    # checkin.checkin_streak() itself, not a hand-picked number. Imported
    # locally, not at module level -- same reason app/account_api.py
    # itself only imports app.checkin inside the functions that need it
    # (see that module's import comment): app.checkin pulls in the full
    # ingest/meshview/MQTT chain, and this test module should still
    # collect even where that heavy chain's own deps aren't installed.
    from app.checkin import checkin_streak

    _checkin(db_path, season_id, player_id, "2026-08-05", points=10)
    _checkin(db_path, season_id, player_id, "2026-08-12", points=10)
    _checkin(db_path, season_id, player_id, "2026-08-19", points=15)

    resp = client.get("/api/account/stats")

    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "Wardriver"
    assert body["team"] == "BLUE"
    mc = body["boards"]["mc"]
    assert mc["display_name"] == "Wardriver"
    assert mc["tiles_held"] == 0
    assert mc["nets_checked_in"] == 3
    assert mc["checkin_points"] == 35.0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    expected_streak = checkin_streak(conn, player_id, "mc", "2026-08-19")
    conn.close()
    assert expected_streak == 3
    assert mc["checkin_streak"] == expected_streak


def test_stats_gives_a_zero_streak_with_no_checkins_at_all(client, db_path):
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id)

    body = client.get("/api/account/stats").json()

    assert body["boards"]["mc"]["checkin_streak"] == 0
    assert body["boards"]["mc"]["nets_checked_in"] == 0


def test_stats_breaks_down_both_boards_independently(client, db_path):
    account_id, _ = _login(client, db_path)
    player_id = _make_player(db_path, account_id=account_id, display_name="TwoBoards", team="RED")
    mc_season_id = _season(db_path, "mc")
    mt_season_id = _season(db_path, "mt")
    _checkin(db_path, mc_season_id, player_id, "2026-08-05", points=10, protocol="mc")
    _checkin(db_path, mt_season_id, player_id, "2026-08-05", points=10, protocol="mt")
    _checkin(db_path, mt_season_id, player_id, "2026-08-12", points=10, protocol="mt")

    body = client.get("/api/account/stats").json()

    assert body["boards"]["mc"]["nets_checked_in"] == 1
    assert body["boards"]["mt"]["nets_checked_in"] == 2
    assert body["boards"]["mc"]["checkin_streak"] == 1
    assert body["boards"]["mt"]["checkin_streak"] == 2


# ---- GET /api/account/honors -----------------------------------------------

def test_honors_returns_only_this_players_awards_newest_month_first(client, db_path):
    account_id, _ = _login(client, db_path)
    player_id = _make_player(db_path, account_id=account_id)
    other_player_id = _make_player(db_path, display_name="Someone Else", team="GREEN")

    _month_award(db_path, "2026-06", "mc", "empire_builder", player_id=player_id, value=42, detail="42 squares")
    _month_award(db_path, "2026-07", "mc", "top_attacker", player_id=player_id, value=7)
    # Not this player's: a team award (player_id NULL) and another player's award.
    _month_award(db_path, "2026-07", "mc", "largest_territory", team="RED", value=100)
    _month_award(db_path, "2026-07", "mc", "tourist", player_id=other_player_id, value=3)

    resp = client.get("/api/account/honors")

    assert resp.status_code == 200
    honors = resp.json()["honors"]
    assert [h["month"] for h in honors] == ["2026-07", "2026-06"]
    assert honors[0]["award"] == "top_attacker"
    assert honors[0]["label"] == "Top Attacker"
    assert honors[1]["award"] == "empire_builder"
    assert honors[1]["label"] == "Empire Builder"
    assert honors[1]["detail"] == "42 squares"


def test_honors_empty_when_player_has_none(client, db_path):
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id)

    body = client.get("/api/account/honors").json()

    assert body["honors"] == []


# ---- GET /api/account/checkins ---------------------------------------------

def test_checkins_returns_own_history_newest_first(client, db_path):
    account_id, _ = _login(client, db_path)
    player_id = _make_player(db_path, account_id=account_id)
    other_player_id = _make_player(db_path, display_name="Not Me")
    season_id = _season(db_path, "mc")
    _checkin(db_path, season_id, player_id, "2026-08-05", points=10, streak=1)
    _checkin(db_path, season_id, player_id, "2026-08-12", points=15, streak=2)
    _checkin(db_path, season_id, other_player_id, "2026-08-12", points=99, streak=1)

    body = client.get("/api/account/checkins").json()

    assert [c["net_date"] for c in body["checkins"]] == ["2026-08-12", "2026-08-05"]
    assert body["checkins"][0]["points"] == 15
    assert body["checkins"][0]["streak"] == 2


def test_checkins_limit_is_honored(client, db_path):
    account_id, _ = _login(client, db_path)
    player_id = _make_player(db_path, account_id=account_id)
    season_id = _season(db_path, "mc")
    for i in range(5):
        _checkin(db_path, season_id, player_id, f"2026-08-{5 + i:02d}", points=10)

    body = client.get("/api/account/checkins?limit=2").json()

    assert len(body["checkins"]) == 2


def test_checkins_empty_when_player_has_none(client, db_path):
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id)

    body = client.get("/api/account/checkins").json()

    assert body["checkins"] == []


# ---- GET /api/account/checkin-health ---------------------------------------
#
# The headline (`state`/`summary`/`resolved`) is now derived from
# whether this player was actually CREDITED for the most recent
# MeshCore net (mc_checkin_award, matched against
# checkin.most_recent_mc_net_date()'s schedule-derived date) -- not
# from whether a contact merely resolves in the directory. That is
# exactly the distinction test_checkin_health_resolving_but_uncredited_
# reports_state_2 below exists to pin down: it is the regression Matt
# hit (hundreds of check-ins, nobody credited, page said everything was
# fine) and the one state the OLD binding-based "resolved" computation
# could never express. `contacts` (per-contact detail) is unchanged in
# shape from before and still exercised the same way as always.
#
# The retired last-resort fallback-name feature (mc_checkin_binding,
# `binding` in the old response) is gone -- see app/checkin_api.py's
# and app/checkin.py's module docstrings. There is no `_binding()`
# helper or `binding` key anywhere below anymore.

def test_checkin_health_credited_recently_reports_state_1(client, db_path):
    account_id, _ = _login(client, db_path)
    player_id = _make_player(db_path, account_id=account_id)
    _bind_node(db_path, player_id, "aaaa1111")
    weekday, today = _today_mc_net_date()
    _net(db_path, weekday=weekday)
    season_id = _season(db_path, "mc")
    _checkin(db_path, season_id, player_id, today, points=25.0)
    client.app.state.checkin_poller = FakePoller([_node("Clean Radio", "aaaa1111ffffffff")])

    body = client.get("/api/account/checkin-health").json()

    assert body["state"] == "credited"
    assert body["resolved"] is True
    assert body["most_recent_net_date"] == today
    assert today in body["summary"]
    assert "25" in body["summary"]


def test_checkin_health_resolving_but_uncredited_reports_state_2(client, db_path):
    # THE regression: a contact that resolves through the directory
    # bridge used to be enough, on its own, to report "resolved" --
    # even with zero actual awards. This is the case that has to
    # change: resolving is necessary but not sufficient.
    account_id, _ = _login(client, db_path)
    player_id = _make_player(db_path, account_id=account_id)
    _bind_node(db_path, player_id, "aaaa1111")
    weekday, today = _today_mc_net_date()
    _net(db_path, weekday=weekday)
    season_id = _season(db_path, "mc")
    # An OLDER award exists -- proves this isn't merely "never
    # credited," it's specifically "not credited for the MOST RECENT
    # net," which is the state that used to be inexpressible.
    _checkin(db_path, season_id, player_id, "2020-01-01", points=10.0)
    client.app.state.checkin_poller = FakePoller([_node("Clean Radio", "aaaa1111ffffffff")])

    body = client.get("/api/account/checkin-health").json()

    assert body["state"] == "resolving_uncredited"
    assert body["resolved"] is False
    assert "Clean Radio" in body["summary"]
    assert today in body["summary"]
    assert body["contacts"][0]["status"] == "resolved"


def test_checkin_health_contact_absent_reports_state_3_names_node_ref(client, db_path):
    account_id, _ = _login(client, db_path)
    player_id = _make_player(db_path, account_id=account_id)
    _bind_node(db_path, player_id, "deadbeef")
    client.app.state.checkin_poller = FakePoller([])  # empty directory

    body = client.get("/api/account/checkin-health").json()

    contact = body["contacts"][0]
    assert contact["status"] == "not_in_directory"
    assert contact["resolved_name"] is None
    assert body["state"] == "not_in_directory"
    assert body["resolved"] is False
    assert "deadbeef" in body["summary"]


def test_checkin_health_contact_key_ambiguous(client, db_path):
    account_id, _ = _login(client, db_path)
    player_id = _make_player(db_path, account_id=account_id)
    _bind_node(db_path, player_id, "aaaaaaaa")
    # Two different directory entries share the same 8-hex prefix.
    client.app.state.checkin_poller = FakePoller([
        _node("Radio One", "aaaaaaaa11111111"),
        _node("Radio Two", "aaaaaaaa22222222"),
    ])

    body = client.get("/api/account/checkin-health").json()

    contact = body["contacts"][0]
    assert contact["status"] == "key_ambiguous"
    assert contact["match_count"] == 2
    assert contact["resolved_name"] is None
    assert body["state"] == "key_ambiguous"
    assert body["resolved"] is False
    assert "operator" in body["summary"].lower()


def test_checkin_health_contact_name_ambiguous(client, db_path):
    account_id, _ = _login(client, db_path)
    player_id = _make_player(db_path, account_id=account_id)
    _bind_node(db_path, player_id, "bbbbbbbb")
    # This player's key uniquely matches one entry, but that entry's
    # display name is shared by a totally different public key.
    client.app.state.checkin_poller = FakePoller([
        _node("Repeater", "bbbbbbbb11111111"),
        _node("Repeater", "cccccccc22222222"),
    ])

    body = client.get("/api/account/checkin-health").json()

    contact = body["contacts"][0]
    assert contact["status"] == "name_ambiguous"
    assert contact["resolved_name"] == "Repeater"
    assert contact["match_count"] == 1
    assert body["state"] == "name_ambiguous"
    assert body["resolved"] is False


def test_checkin_health_nothing_bound_reports_state_6(client, db_path):
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id)
    client.app.state.checkin_poller = FakePoller([])

    body = client.get("/api/account/checkin-health").json()

    assert body["contacts"] == []
    assert body["state"] == "nothing_bound"
    assert body["resolved"] is False
    assert "no meshcore contact" in body["summary"].lower()
    assert "confirm my node" in body["summary"].lower()
    assert "binding" not in body


def test_checkin_health_never_leaks_another_players_contact_or_name(client, db_path):
    # A previous commit (a39eab3) already removed a leak of this shape
    # (checkin_unresolved_sender, a table keyed by name rather than
    # player). This pins down the equally important, never-regressed
    # half: the response must also never surface a DIFFERENT player's
    # own bound contact or resolved name, even though the poller's
    # directory snapshot legitimately contains both players' radios.
    account_id, _ = _login(client, db_path)
    player_id = _make_player(db_path, account_id=account_id, display_name="Me")
    other_id = _make_player(db_path, display_name="NotMe")
    _bind_node(db_path, player_id, "aaaa1111")
    _bind_node(db_path, other_id, "bbbb2222")
    client.app.state.checkin_poller = FakePoller([
        _node("My Radio", "aaaa1111ffffffff"),
        _node("Someone Else's Radio", "bbbb2222ffffffff"),
    ])

    body = client.get("/api/account/checkin-health").json()

    assert len(body["contacts"]) == 1
    assert body["contacts"][0]["node_ref"] == "aaaa1111"
    raw = json.dumps(body)
    assert "bbbb2222" not in raw
    assert "Someone Else's Radio" not in raw


def test_checkin_health_does_not_leak_recent_unresolved_names(client, db_path):
    # checkin_unresolved_sender is keyed by NAME, not by player -- every
    # live, actively-posting unclaimed name on it is a ready-made target
    # for anyone claiming it. That signal stays admin-only (see
    # app/admin_ops.py); a player's own checkin-health response must
    # never surface it, and the retired POST /api/checkin/name (no
    # proof of possession required) that used to make an unclaimed name
    # exploitable is gone entirely -- see this file's own header.
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id)
    _unresolved(db_path, "2026-08-26", "MysteryPerson", last_seen=NOW - 3600)
    _unresolved(db_path, "2026-01-01", "AncientName", last_seen=NOW - 400 * 86400)

    body = client.get("/api/account/checkin-health").json()

    assert "recent_unresolved_names" not in body
    raw = json.dumps(body)
    assert "MysteryPerson" not in raw
    assert "AncientName" not in raw


def test_checkin_health_with_no_poller_degrades_to_empty_directory(client, db_path):
    account_id, _ = _login(client, db_path)
    player_id = _make_player(db_path, account_id=account_id)
    _bind_node(db_path, player_id, "deadbeef")
    del client.app.state.checkin_poller

    resp = client.get("/api/account/checkin-health")

    assert resp.status_code == 200
    assert resp.json()["contacts"][0]["status"] == "not_in_directory"
