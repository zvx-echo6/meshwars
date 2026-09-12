"""Tests for the admin check-in visibility gap: no screen in the admin
panel ever read mc_checkin_award, so an operator could credit a missed
check-in (POST /api/admin/checkin/award) but never see what had
actually been recorded -- a healthy net and a dead one looked
identical. This adds:

  - GET /api/admin/checkin/awards (app/admin_ops.py) -- history for the
    most recent net dates, joined to player (display name) and
    checkin_net (net label via the net_id column added by the per-net
    streak scoping fix), falling back to a protocol label when net_id
    is NULL (a legacy row, or an ambiguous manual credit).
  - GET /api/admin/checkin/nets' new last_checkin_net_date/
    last_checkin_count fields -- a per-net count of awards for that
    net's own most recent net_date, attributed by net_id only.

Same "FastAPI-around-a-real-file-backed-db, TestClient, session cookie
via app/sessions.create_session" shape tests/test_admin_traffic.py uses
(see that file's own module docstring) -- app/admin_ops.py's routes go
through app/db.py's connect(), a fresh connection per call, so an
in-memory ":memory:" database would not share data between a test's
seed writes and the route's own read. The _net/_player/_award seed
helpers mirror tests/test_checkin_net_scoping.py's own, adapted to
write through a file path instead of the `conn` fixture.
"""
from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.db as db
from app.admin_api import router as admin_api_router
from app.admin_ops import router as admin_ops_router
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


@pytest.fixture
def app_(db_path):
    fastapi_app = FastAPI()
    # admin_ops's routes are the ones under test; admin_api's router
    # supplies _role_guard's actual auth machinery (sessions/accounts),
    # same two-router shape test_admin_ops_checkin.py's own binding-
    # route test uses implicitly via admin_api's imported _role_guard.
    fastapi_app.include_router(admin_ops_router)
    fastapi_app.include_router(admin_api_router)
    fastapi_app.add_exception_handler(HTTPException, http_exception_as_error_body)
    return fastapi_app


@pytest.fixture
def client(app_):
    return TestClient(app_)


def _make_account(path: str, *, role: str = "admin") -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute("INSERT INTO account(created_at, role) VALUES (?, ?)", (NOW, role))
    account_id = cur.lastrowid
    # _role_guard() (app/admin_api.py) requires an ACTIVE TOTP enrollment
    # on every call for any role-holding account, not just at claim time
    # -- see that function's own docstring -- so a role alone here would
    # 403 every route under test.
    conn.execute(
        "INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at) "
        "VALUES (?, 'unused', ?, ?)",
        (account_id, NOW, NOW),
    )
    conn.commit()
    conn.close()
    return account_id


def _login_as(client, account_id: int) -> None:
    import asyncio

    raw_token = asyncio.run(create_session(account_id, device_label=None))
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)


def _net(path, *, weekday, protocol="mc", kind="corescope", label="Test Net") -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO checkin_net(label, protocol, kind, connector_url, channel, hashtag, "
        "weekday, start_hour, end_hour, timezone, start_date, enabled, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (label, protocol, kind, "http://example.test", "general", "",
         weekday, 0, 23, "America/Boise", "2000-01-01", 1, NOW),
    )
    net_id = cur.lastrowid
    conn.commit()
    conn.close()
    return net_id


def _player(path, name="Player") -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO player(display_name, team, created_at) VALUES (?,?,?)",
        (name, "RED", NOW),
    )
    player_id = cur.lastrowid
    conn.commit()
    conn.close()
    return player_id


def _award(path, *, player_id, net_id, net_date, streak=1, points=25.0,
           protocol="mc", message_id="pkt-1") -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO mc_checkin_award"
        "(season_id, player_id, net_date, points, protocol, message_id, awarded_at, streak, net_id) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
        (player_id, net_date, points, protocol, message_id, NOW, streak, net_id),
    )
    conn.commit()
    conn.close()


def _admin_client(client, db_path):
    account_id = _make_account(db_path, role="admin")
    _login_as(client, account_id)
    return client


# ===========================================================================
# GET /api/admin/checkin/awards
# ===========================================================================


def test_awards_grouped_and_ordered_by_net_date_with_net_label(client, db_path):
    _admin_client(client, db_path)
    net_id = _net(db_path, weekday=2, label="Freq51 MC")
    p1 = _player(db_path, "Alice")
    p2 = _player(db_path, "Bob")
    _award(db_path, player_id=p1, net_id=net_id, net_date="2026-08-19", message_id="pkt-a")
    _award(db_path, player_id=p2, net_id=net_id, net_date="2026-08-26", message_id="pkt-b")

    resp = client.get("/api/admin/checkin/awards")
    assert resp.status_code == 200
    awards = resp.json()["awards"]
    assert len(awards) == 2
    # Newest net_date first.
    assert [a["net_date"] for a in awards] == ["2026-08-26", "2026-08-19"]
    assert all(a["net_label"] == "Freq51 MC" for a in awards)
    assert awards[0]["player_name"] == "Bob"
    assert awards[1]["player_name"] == "Alice"


def test_awards_null_net_id_falls_back_to_protocol_label(client, db_path):
    _admin_client(client, db_path)
    p1 = _player(db_path, "LegacyPlayer")
    # No checkin_net row involved at all -- net_id NULL, exactly the
    # "written before that column existed" case mc_checkin_award's own
    # comment describes.
    _award(db_path, player_id=p1, net_id=None, net_date="2026-07-01",
           protocol="mc", message_id="pkt-legacy")

    resp = client.get("/api/admin/checkin/awards")
    assert resp.status_code == 200
    awards = resp.json()["awards"]
    assert len(awards) == 1
    assert awards[0]["net_label"] == "MeshCore"
    assert awards[0]["player_name"] == "LegacyPlayer"


def test_awards_source_admin_vs_poller(client, db_path):
    _admin_client(client, db_path)
    net_id = _net(db_path, weekday=2, label="Freq51 MC")
    p1 = _player(db_path, "Carol")
    p2 = _player(db_path, "Dave")
    _award(db_path, player_id=p1, net_id=net_id, net_date="2026-08-19", message_id="admin")
    _award(db_path, player_id=p2, net_id=net_id, net_date="2026-08-19", message_id="pkt-real-123")

    resp = client.get("/api/admin/checkin/awards")
    assert resp.status_code == 200
    by_name = {a["player_name"]: a for a in resp.json()["awards"]}
    assert by_name["Carol"]["source"] == "admin"
    assert by_name["Dave"]["source"] == "poller"


def test_awards_limits_to_most_recent_net_dates(client, db_path):
    _admin_client(client, db_path)
    net_id = _net(db_path, weekday=2, label="Freq51 MC")
    p1 = _player(db_path, "Solo")
    dates = [f"2026-01-{d:02d}" for d in range(1, 11)]  # 10 distinct dates
    for i, d in enumerate(dates):
        _award(db_path, player_id=p1, net_id=net_id, net_date=d, message_id=f"pkt-{i}")

    resp = client.get("/api/admin/checkin/awards?dates=3")
    assert resp.status_code == 200
    body = resp.json()
    seen_dates = sorted({a["net_date"] for a in body["awards"]})
    assert seen_dates == ["2026-01-08", "2026-01-09", "2026-01-10"]
    assert body["dates"] == 3


# ===========================================================================
# GET /api/admin/checkin/nets -- per-net check-in count
# ===========================================================================


def test_nets_reports_zero_checkins_for_a_net_with_no_awards(client, db_path):
    _admin_client(client, db_path)
    _net(db_path, weekday=3, label="Coloradomesh MC")

    resp = client.get("/api/admin/checkin/nets")
    assert resp.status_code == 200
    nets = resp.json()["nets"]
    assert len(nets) == 1
    assert nets[0]["last_checkin_count"] == 0
    assert nets[0]["last_checkin_net_date"] is None


def test_nets_reports_per_net_checkin_count_for_latest_date_only(client, db_path):
    _admin_client(client, db_path)
    net_id = _net(db_path, weekday=2, label="Freq51 MC")
    other_net_id = _net(db_path, weekday=3, label="Coloradomesh MC")
    p1 = _player(db_path, "Alice")
    p2 = _player(db_path, "Bob")
    p3 = _player(db_path, "Carol")
    # Two players on the latest date, one on an older date -- only the
    # latest date's count should show.
    _award(db_path, player_id=p1, net_id=net_id, net_date="2026-08-19", message_id="pkt-1")
    _award(db_path, player_id=p2, net_id=net_id, net_date="2026-08-26", message_id="pkt-2")
    _award(db_path, player_id=p3, net_id=net_id, net_date="2026-08-26", message_id="pkt-3")

    resp = client.get("/api/admin/checkin/nets")
    assert resp.status_code == 200
    by_id = {n["id"]: n for n in resp.json()["nets"]}
    assert by_id[net_id]["last_checkin_net_date"] == "2026-08-26"
    assert by_id[net_id]["last_checkin_count"] == 2
    assert by_id[other_net_id]["last_checkin_count"] == 0
    assert by_id[other_net_id]["last_checkin_net_date"] is None


def test_nets_does_not_attribute_null_net_id_awards_to_any_net(client, db_path):
    # A legacy/ambiguous award with net_id NULL must not be miscounted
    # into any net's total -- see _checkin_counts_by_net's own docstring
    # in app/admin_ops.py for why this is excluded rather than guessed.
    _admin_client(client, db_path)
    net_id = _net(db_path, weekday=2, label="Freq51 MC")
    p1 = _player(db_path, "Orphan")
    _award(db_path, player_id=p1, net_id=None, net_date="2026-08-19",
           protocol="mc", message_id="pkt-orphan")

    resp = client.get("/api/admin/checkin/nets")
    assert resp.status_code == 200
    nets = resp.json()["nets"]
    assert len(nets) == 1
    assert nets[0]["id"] == net_id
    assert nets[0]["last_checkin_count"] == 0
    assert nets[0]["last_checkin_net_date"] is None
