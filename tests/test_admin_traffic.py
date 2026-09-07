"""Tests for server-side traffic counting: app/traffic.py's
TrafficMiddleware and build_traffic_report(), and the admin-gated
GET /api/admin/traffic route in app/admin_api.py.

Same "FastAPI-around-a-real-file-backed-db, TestClient, session cookie
via app/sessions.create_session" shape tests/test_admin_roles.py already
uses (see that file's own module docstring) -- app/traffic.py's writes
go through app/db.py's WriteSession/connect(), a fresh connection per
call, so an in-memory ":memory:" database would not share data between
the middleware's write and a later assertion's read.
"""
from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient

import app.db as db
import app.traffic as traffic_module
from app.admin_api import router as admin_router
from app.auth import http_exception_as_error_body
from app.db import MIGRATIONS, SCHEMA
from app.sessions import SESSION_COOKIE_NAME, create_session
from app.traffic import (
    TrafficMiddleware,
    _hash_visitor,
    _is_bot_user_agent,
    _normalize_referrer,
    build_traffic_report,
)

NOW = int(time.time())

BOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
HUMAN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


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
    monkeypatch.setattr(traffic_module.settings, "db_path", path)
    return path


@pytest.fixture(autouse=True)
def _reset_traffic_salt_cache():
    """_salt_cache (app/traffic.py) is a module-level singleton, resolved
    once per process and reused forever -- exactly the point in
    production, but poison across tests unless cleared: without this,
    whichever test runs first would pin every later test to its own
    db_path's generated salt, even after that database no longer
    exists.
    """
    traffic_module._salt_cache = None
    yield
    traffic_module._salt_cache = None


@pytest.fixture
def app_(db_path):
    """A small FastAPI app carrying TrafficMiddleware plus two synthetic
    routes standing in for "a real page" (text/html) and "a real JSON
    API poll" (application/json) -- exactly the two shapes the
    content-type rule in TrafficMiddleware._maybe_record() is meant to
    tell apart -- alongside the real admin router, for the endpoint
    tests below.
    """
    fastapi_app = FastAPI()
    fastapi_app.add_middleware(TrafficMiddleware)
    fastapi_app.include_router(admin_router)
    fastapi_app.add_exception_handler(HTTPException, http_exception_as_error_body)

    @fastapi_app.get("/page", response_class=HTMLResponse)
    async def page():
        return HTMLResponse("<html><body>hi</body></html>")

    @fastapi_app.get("/broken", response_class=HTMLResponse)
    async def broken():
        return HTMLResponse("<html><body>error page</body></html>", status_code=500)

    @fastapi_app.get("/api/data")
    async def api_data():
        return JSONResponse({"ok": True})

    return fastapi_app


@pytest.fixture
def client(app_):
    return TestClient(app_)


def _visitor_rows(db_path: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM site_visitor").fetchall()
    conn.close()
    return rows


def _visit_day_rows(db_path: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM site_visit_day ORDER BY day, visitor_hash").fetchall()
    conn.close()
    return rows


def _report(db_path: str, days: int = 30) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return build_traffic_report(conn, days)
    finally:
        conn.close()


# ===========================================================================
# unit tests for the small helpers
# ===========================================================================


def test_is_bot_user_agent_matches_known_keywords():
    assert _is_bot_user_agent(BOT_UA) is True
    assert _is_bot_user_agent("curl/8.4.0") is True
    assert _is_bot_user_agent("python-requests/2.31.0") is True


def test_is_bot_user_agent_false_for_a_real_browser():
    assert _is_bot_user_agent(HUMAN_UA) is False


def test_is_bot_user_agent_false_for_empty_string():
    # An absent User-Agent is not, by itself, evidence of automation --
    # see _is_bot_user_agent's own docstring.
    assert _is_bot_user_agent("") is False


def test_hash_visitor_is_stable_for_the_same_inputs():
    a = _hash_visitor("salt", "198.51.100.10", HUMAN_UA)
    b = _hash_visitor("salt", "198.51.100.10", HUMAN_UA)
    assert a == b
    assert len(a) == 16


def test_hash_visitor_changes_with_any_input():
    base = _hash_visitor("salt", "198.51.100.10", HUMAN_UA)
    assert _hash_visitor("other-salt", "198.51.100.10", HUMAN_UA) != base
    assert _hash_visitor("salt", "198.51.100.11", HUMAN_UA) != base
    assert _hash_visitor("salt", "198.51.100.10", BOT_UA) != base


def test_normalize_referrer_reduces_to_scheme_and_host():
    assert (
        _normalize_referrer("https://old-rival-site.com/page?utm=1", "meshwars.com")
        == "https://old-rival-site.com"
    )


def test_normalize_referrer_drops_self_referrals():
    assert _normalize_referrer("https://meshwars.com/join", "meshwars.com") is None


def test_normalize_referrer_drops_missing_or_unparsable_headers():
    assert _normalize_referrer("", "meshwars.com") is None
    assert _normalize_referrer("/join", "meshwars.com") is None


# ===========================================================================
# middleware: what gets counted
# ===========================================================================


def test_a_page_view_is_counted(client, db_path):
    resp = client.get("/page", headers={"User-Agent": HUMAN_UA})
    assert resp.status_code == 200

    rows = _visitor_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["is_bot"] == 0
    assert rows[0]["hits"] == 1

    day_rows = _visit_day_rows(db_path)
    assert len(day_rows) == 1
    assert day_rows[0]["views"] == 1


def test_an_api_json_request_is_not_counted(client, db_path):
    """The content-type rule, not a path blocklist, is what excludes
    this -- /api/data returns application/json, so it must never reach
    site_visitor at all.
    """
    resp = client.get("/api/data", headers={"User-Agent": HUMAN_UA})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")

    assert _visitor_rows(db_path) == []
    assert _visit_day_rows(db_path) == []


def test_non_get_requests_are_not_counted(client, db_path):
    # No POST route exists to hit legitimately, but a 405 to a GET-only
    # route still exercises the "method must be GET" gate the same way:
    # nothing about a POST -- successful or not -- should ever be
    # recorded as a page view.
    resp = client.post("/page", headers={"User-Agent": HUMAN_UA})
    assert resp.status_code == 405
    assert _visitor_rows(db_path) == []


def test_error_responses_are_not_counted(client, db_path):
    resp = client.get("/broken", headers={"User-Agent": HUMAN_UA})
    assert resp.status_code == 500
    assert _visitor_rows(db_path) == []


# ===========================================================================
# identity: same-day repeat vs. a later day
# ===========================================================================


def test_same_visitor_twice_in_one_day_is_one_unique_two_views(client, db_path, monkeypatch):
    monkeypatch.setattr(traffic_module, "_utc_today", lambda: "2026-09-07")

    client.get("/page", headers={"User-Agent": HUMAN_UA})
    client.get("/page", headers={"User-Agent": HUMAN_UA})

    rows = _visitor_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["hits"] == 2

    day_rows = _visit_day_rows(db_path)
    assert len(day_rows) == 1
    assert day_rows[0]["views"] == 2

    report = _report(db_path)
    today_entry = next(d for d in report["daily"] if d["day"] == "2026-09-07")
    assert today_entry == {
        "day": "2026-09-07",
        "views": 2,
        "uniques": 1,
        "new_visitors": 1,
        "bot_views": 0,
    }


def test_a_visitor_seen_on_a_later_day_is_returning_not_new(client, db_path, monkeypatch):
    monkeypatch.setattr(traffic_module, "_utc_today", lambda: "2026-09-05")
    client.get("/page", headers={"User-Agent": HUMAN_UA})

    monkeypatch.setattr(traffic_module, "_utc_today", lambda: "2026-09-07")
    client.get("/page", headers={"User-Agent": HUMAN_UA})

    # Same visitor_hash both times (same IP/UA via TestClient), so only
    # one site_visitor row exists, but two site_visit_day rows -- one
    # per day.
    assert len(_visitor_rows(db_path)) == 1
    day_rows = _visit_day_rows(db_path)
    assert [r["day"] for r in day_rows] == ["2026-09-05", "2026-09-07"]

    report = _report(db_path, days=10)
    day_05 = next(d for d in report["daily"] if d["day"] == "2026-09-05")
    day_07 = next(d for d in report["daily"] if d["day"] == "2026-09-07")

    assert day_05["uniques"] == 1
    assert day_05["new_visitors"] == 1  # first-ever day for this visitor

    assert day_07["uniques"] == 1
    assert day_07["new_visitors"] == 0  # same visitor returning, not new
    assert report["since"] == "2026-09-05"


# ===========================================================================
# bots: recorded, but walled off from human counts
# ===========================================================================


def test_bot_user_agent_excluded_from_human_counts_and_shown_in_bot_views(client, db_path, monkeypatch):
    monkeypatch.setattr(traffic_module, "_utc_today", lambda: "2026-09-07")

    client.get("/page", headers={"User-Agent": HUMAN_UA})
    client.get("/page", headers={"User-Agent": BOT_UA})

    rows = {r["is_bot"]: r for r in _visitor_rows(db_path)}
    assert set(rows.keys()) == {0, 1}

    report = _report(db_path)
    today_entry = next(d for d in report["daily"] if d["day"] == "2026-09-07")
    assert today_entry["views"] == 1        # the human hit only
    assert today_entry["uniques"] == 1
    assert today_entry["new_visitors"] == 1
    assert today_entry["bot_views"] == 1    # the bot hit, reported separately

    # site_path_daily/site_referrer_daily are human-only -- the bot hit
    # must not appear in the path breakdown at all.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    path_rows = conn.execute("SELECT * FROM site_path_daily").fetchall()
    conn.close()
    assert len(path_rows) == 1
    assert path_rows[0]["views"] == 1


# ===========================================================================
# the middleware must never break a request
# ===========================================================================


def test_a_recording_failure_never_500s_the_real_response(client, db_path, monkeypatch):
    def _boom(conn):
        raise RuntimeError("simulated recording failure")

    monkeypatch.setattr(traffic_module, "_get_salt", _boom)

    resp = client.get("/page", headers={"User-Agent": HUMAN_UA})

    assert resp.status_code == 200
    assert resp.text == "<html><body>hi</body></html>"
    # And, as a consequence of the failure, nothing was recorded --
    # proving the exception really did happen inside the recording path,
    # not that the monkeypatch silently did nothing.
    assert _visitor_rows(db_path) == []


# ===========================================================================
# GET /api/admin/traffic
# ===========================================================================


def _make_account(path: str, *, role: str | None = None, totp_active: bool = False) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute("INSERT INTO account(created_at, role) VALUES (?, ?)", (NOW, role))
    account_id = cur.lastrowid
    if totp_active:
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


def test_traffic_endpoint_rejects_an_unauthenticated_caller(client, db_path, monkeypatch):
    monkeypatch.setattr(traffic_module.settings, "admin_token", "irrelevant")
    import app.admin_api as admin_api_module
    monkeypatch.setattr(admin_api_module.settings, "admin_token", "irrelevant")

    resp = client.get("/api/admin/traffic")
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_traffic_endpoint_rejects_a_signed_in_caller_with_no_role(client, db_path, monkeypatch):
    import app.admin_api as admin_api_module
    monkeypatch.setattr(admin_api_module.settings, "admin_token", "irrelevant")

    account_id = _make_account(db_path, role=None, totp_active=True)
    _login_as(client, account_id)

    resp = client.get("/api/admin/traffic")
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_traffic_endpoint_serves_an_admin_with_active_totp(client, db_path, monkeypatch):
    import app.admin_api as admin_api_module
    monkeypatch.setattr(admin_api_module.settings, "admin_token", "irrelevant")
    monkeypatch.setattr(traffic_module, "_utc_today", lambda: "2026-09-07")

    account_id = _make_account(db_path, role="admin", totp_active=True)
    _login_as(client, account_id)

    # Generate one recorded human page view before asking the admin
    # route to report on it.
    client.get("/page", headers={"User-Agent": HUMAN_UA})

    resp = client.get("/api/admin/traffic?days=7")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) == {"today", "daily", "top_paths", "top_referrers", "since"}
    assert data["today"]["views"] == 1
    assert data["today"]["uniques"] == 1
    assert len(data["daily"]) == 7
    assert data["daily"][-1]["day"] == "2026-09-07"


def test_traffic_endpoint_clamps_days_to_a_sane_range(client, db_path, monkeypatch):
    import app.admin_api as admin_api_module
    monkeypatch.setattr(admin_api_module.settings, "admin_token", "irrelevant")

    account_id = _make_account(db_path, role="admin", totp_active=True)
    _login_as(client, account_id)

    resp = client.get("/api/admin/traffic?days=99999")
    assert resp.status_code == 200
    assert len(resp.json()["daily"]) == 365
