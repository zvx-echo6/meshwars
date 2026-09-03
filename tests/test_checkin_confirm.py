"""Tests for app/checkin_api.py's node-confirmation routes: POST
/api/checkin/confirm/start, GET .../status, POST .../accept, DELETE
/api/checkin/confirm -- proving possession of a specific MeshCore radio
by making it advert during a short window and binding whichever public
key showed a FRESH advert under the typed name. See app/db.py's
mc_node_confirmation comment and app/checkin.py's
confirm_scan_connector/confirm_scan_all_connectors for the mechanics
under test here; this file exercises the HTTP surface on top of them.

Same "FastAPI-around-one-router" + file-backed sqlite fixture shape
tests/test_account_api.py and tests/test_account_player_data.py already
use (a real file, not ":memory:", since TestClient runs the app in a
different OS thread and app/db.py's connect() opens a fresh connection
per call -- see tests/test_oauth_api.py's own docstring for why), and
the SAME httpx.MockTransport monkeypatch pattern tests/test_oauth_api.py
uses to intercept an outbound call, here standing in for a CoreScope/
Beacon connector's node-directory endpoint
(CoreScopeClient.fetch_directory_search / BeaconClient.
fetch_directory_search in app/checkin.py) instead of an OAuth
provider's token/userinfo endpoints -- there is no real network access
anywhere in this file.

The mock upstream deliberately ignores the `search`/`name` query
parameter and always returns every node in `state["nodes"]` -- the
whole point of several tests below (e.g.
test_node_with_different_name_never_appears) is proving THIS repo's
own exact-normalized-name re-filter (confirm_scan_connector,
app/checkin.py) does the narrowing, not trusting the upstream's
(confirmed-substring, not exact) `search`/`name` filter to have done
it already.

NOTE: app/checkin_api.py cannot be imported in this test environment --
`aiolimiter` is not installed here, and app/checkin.py imports it
unconditionally via app/meshview_client.py (see tests/test_auth.py's
own module docstring for the identical, pre-existing gap already
covering every OTHER route in app/checkin_api.py). This file will run
wherever aiolimiter/sse-starlette are actually installed (this repo's
requirements.txt); on a box missing that dependency it cannot be
COLLECTED at all, the same way app/checkin_api.py's existing routes
already couldn't be exercised via TestClient here.
"""
from __future__ import annotations

import sqlite3
import time

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.checkin as checkin_module
import app.checkin_api as checkin_api_module
import app.db as db
from app.auth import http_exception_as_error_body
from app.checkin_api import router as checkin_router
from app.db import MIGRATIONS, SCHEMA
from app.sessions import SESSION_COOKIE_NAME, create_session

NOW = int(time.time())
CONNECTOR_URL = "https://cs.test"
NAME = "Tester Radio"
PUBKEY = "a1" * 32  # 64 lowercase hex chars -- passes node_ref.py's normalize_public_key
OTHER_PUBKEY = "b2" * 32


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


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """checkin_api.py's two module-level _BoundedHits singletons
    accumulate hits across every test in this file, keyed on
    TestClient's fixed synthetic peer address ("testclient"), unless
    cleared between tests -- same pattern tests/test_account_api.py's
    own _reset_link_key_rate_limiter fixture uses for the identical
    reason.
    """
    checkin_api_module._addr_rate_limiter._hits.clear()
    checkin_api_module._key_rate_limiter._hits.clear()
    yield
    checkin_api_module._addr_rate_limiter._hits.clear()
    checkin_api_module._key_rate_limiter._hits.clear()


@pytest.fixture(autouse=True)
def _reset_scan_cache():
    """app/checkin_api.py's _scan_cache is a module-level dict keyed by
    player_id -- clear it between tests so one test's cached scan can
    never leak into the next (player ids are reused test to test, since
    each test builds its own file-backed db from scratch).
    """
    checkin_api_module._scan_cache.clear()
    yield
    checkin_api_module._scan_cache.clear()


def _patch_checkin_http(monkeypatch, handler) -> None:
    """Redirects every httpx.AsyncClient app/checkin.py's CoreScopeClient/
    BeaconClient construct through an httpx.MockTransport running
    `handler` -- monkeypatches the class on checkin.py's own `httpx`
    module reference, the exact pattern tests/test_oauth_api.py's own
    _patch_provider_http uses for oauth_api.py's outbound calls.
    """

    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(checkin_module.httpx, "AsyncClient", _MockAsyncClient)


def _corescope_handler(state: dict, calls: list[str]):
    """A MockTransport handler standing in for one CoreScope instance's
    GET /api/nodes?search=...&limit=... -- ignores the query params
    entirely and always returns every node currently in
    state["nodes"], so what actually narrows the result down to a
    match is confirm_scan_connector()'s own exact-name re-filter, not
    this mock pretending to implement CoreScope's (substring, not
    exact) `search` semantics. `calls` records every request path, so
    a test can assert the scan throttle actually skipped a re-fetch.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/nodes":
            return httpx.Response(200, json={"nodes": list(state["nodes"])})
        return httpx.Response(404)

    return handler


@pytest.fixture
def client(db_path):
    app = FastAPI()
    app.include_router(checkin_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    return TestClient(app)


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def _make_account(path: str) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (NOW,))
    conn.commit()
    account_id = cur.lastrowid
    conn.close()
    return account_id


def _make_player(path: str, *, account_id: int | None = None, display_name="Tester", team="RED") -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO player(display_name, team, created_at, account_id) VALUES (?, ?, ?, ?)",
        (display_name, team, NOW, account_id),
    )
    conn.commit()
    player_id = cur.lastrowid
    conn.close()
    return player_id


def _login(client: TestClient, db_path: str) -> int:
    """Create an account with a linked player and set that session's
    cookie on `client` for subsequent requests. Returns the player_id.
    """
    account_id = _make_account(db_path)
    player_id = _make_player(db_path, account_id=account_id)
    raw_token = _run(create_session(account_id, user_agent="pytest-agent", ip="203.0.113.5"))
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return player_id


def _make_corescope_net(path: str, connector_url: str = CONNECTOR_URL) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO checkin_net"
        "(label, protocol, kind, connector_url, channel, hashtag, weekday, start_hour, "
        " end_hour, timezone, start_date, enabled, created_at) "
        "VALUES (?, 'mc', 'corescope', ?, 'general', '', 2, 18, 20, 'America/Boise', "
        "        '2026-01-01', 1, ?)",
        ("Test Net", connector_url, NOW),
    )
    conn.commit()
    conn.close()


def _node(name=NAME, public_key=PUBKEY, role="companion", last_heard="2026-09-01T12:00:00Z"):
    return {"name": name, "public_key": public_key, "role": role, "last_heard": last_heard}


def _player_node_row(path: str, node_ref: str):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT protocol, node_ref, player_id, public_key FROM player_node WHERE node_ref = ?",
        (node_ref,),
    ).fetchone()
    conn.close()
    return row


def _expire_window(path: str, player_id: int) -> None:
    """Force this player's open confirmation window into the past, so
    tests don't have to sleep out a real 5-minute window to exercise
    expiry.
    """
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE mc_node_confirmation SET expires_at = ? WHERE player_id = ?",
        (int(time.time()) - 1, player_id),
    )
    conn.commit()
    conn.close()


# ---- start -> status, no fresh advert ------------------------------------

def test_start_then_status_waiting_when_no_fresh_advert(client, db_path, monkeypatch):
    """A node already advertising under the typed name BEFORE the
    window opens is captured into the baseline; with no further advert,
    status must report "waiting" with an empty candidate list -- it is
    not proof of anything by itself (see mc_node_confirmation's own
    comment in app/db.py).
    """
    _make_corescope_net(db_path)
    player_id = _login(client, db_path)

    state = {"nodes": [_node()]}
    calls: list[str] = []
    _patch_checkin_http(monkeypatch, _corescope_handler(state, calls))

    start_resp = client.post("/api/checkin/confirm/start", json={"name": NAME})
    assert start_resp.status_code == 200
    body = start_resp.json()
    assert body["window_seconds"] == 300
    assert body["baseline_count"] == 1
    assert body["expires_at"] > int(time.time())

    status_resp = client.get("/api/checkin/confirm/status")
    assert status_resp.status_code == 200
    status_body = status_resp.json()
    assert status_body["state"] == "waiting"
    assert status_body["candidates"] == []


# ---- a node that advances past baseline becomes a candidate --------------

def test_candidate_appears_when_last_heard_advances_past_baseline(client, db_path, monkeypatch):
    _make_corescope_net(db_path)
    player_id = _login(client, db_path)

    state = {"nodes": [_node(last_heard="2026-09-01T12:00:00Z")]}
    calls: list[str] = []
    _patch_checkin_http(monkeypatch, _corescope_handler(state, calls))

    assert client.post("/api/checkin/confirm/start", json={"name": NAME}).status_code == 200

    # The radio keys on again -- a fresh advert, strictly later than the
    # baseline snapshot just taken.
    state["nodes"][0]["last_heard"] = "2026-09-01T12:05:00Z"

    status_body = client.get("/api/checkin/confirm/status").json()
    assert status_body["state"] == "found"
    assert len(status_body["candidates"]) == 1
    candidate = status_body["candidates"][0]
    assert candidate["public_key"] == PUBKEY
    assert candidate["node_ref"] == PUBKEY[:8]
    assert candidate["name"] == NAME
    assert candidate["already_claimed"] is False


# ---- a baseline node with an unchanged last_heard is not a candidate -----

def test_baseline_node_unchanged_last_heard_not_a_candidate(client, db_path, monkeypatch):
    _make_corescope_net(db_path)
    player_id = _login(client, db_path)

    state = {"nodes": [_node(last_heard="2026-09-01T12:00:00Z")]}
    calls: list[str] = []
    _patch_checkin_http(monkeypatch, _corescope_handler(state, calls))

    assert client.post("/api/checkin/confirm/start", json={"name": NAME}).status_code == 200
    # No mutation of state["nodes"] -- the exact same last_heard is
    # still what a re-scan will find.

    status_body = client.get("/api/checkin/confirm/status").json()
    assert status_body["state"] == "waiting"
    assert status_body["candidates"] == []


# ---- a node with a different name never appears ---------------------------

def test_node_with_different_name_never_appears(client, db_path, monkeypatch):
    _make_corescope_net(db_path)
    player_id = _login(client, db_path)

    state = {
        "nodes": [
            _node(name=NAME, public_key=PUBKEY, last_heard="2026-09-01T12:00:00Z"),
            _node(name="Somebody Else", public_key=OTHER_PUBKEY, last_heard="2026-09-01T12:00:00Z"),
        ]
    }
    calls: list[str] = []
    _patch_checkin_http(monkeypatch, _corescope_handler(state, calls))

    assert client.post("/api/checkin/confirm/start", json={"name": NAME}).status_code == 200

    # Both nodes advance -- only the one matching the typed name may
    # ever become a candidate.
    state["nodes"][0]["last_heard"] = "2026-09-01T12:05:00Z"
    state["nodes"][1]["last_heard"] = "2026-09-01T12:05:00Z"

    status_body = client.get("/api/checkin/confirm/status").json()
    assert status_body["state"] == "found"
    keys = [c["public_key"] for c in status_body["candidates"]]
    assert keys == [PUBKEY]
    assert OTHER_PUBKEY not in keys


# ---- accept binds player_node correctly -----------------------------------

def test_accept_binds_player_node_with_correct_node_ref(client, db_path, monkeypatch):
    _make_corescope_net(db_path)
    player_id = _login(client, db_path)

    state = {"nodes": [_node(last_heard="2026-09-01T12:00:00Z")]}
    calls: list[str] = []
    _patch_checkin_http(monkeypatch, _corescope_handler(state, calls))

    assert client.post("/api/checkin/confirm/start", json={"name": NAME}).status_code == 200
    state["nodes"][0]["last_heard"] = "2026-09-01T12:05:00Z"

    accept_resp = client.post("/api/checkin/confirm/accept", json={"public_key": PUBKEY})
    assert accept_resp.status_code == 200
    assert accept_resp.json() == {"node_ref": PUBKEY[:8]}

    row = _player_node_row(db_path, PUBKEY[:8])
    assert row is not None
    assert row["protocol"] == "mc"
    assert row["player_id"] == player_id
    assert row["public_key"] == PUBKEY

    # Accepting consumes the window.
    assert client.get("/api/checkin/confirm/status").json() == {"state": "none"}


# ---- accept refuses a key that isn't a current candidate ------------------

def test_accept_refuses_key_not_a_current_candidate(client, db_path, monkeypatch):
    _make_corescope_net(db_path)
    player_id = _login(client, db_path)

    state = {"nodes": [_node(last_heard="2026-09-01T12:00:00Z")]}
    calls: list[str] = []
    _patch_checkin_http(monkeypatch, _corescope_handler(state, calls))

    assert client.post("/api/checkin/confirm/start", json={"name": NAME}).status_code == 200
    # Never advances past baseline -- PUBKEY is never a candidate.

    resp = client.post("/api/checkin/confirm/accept", json={"public_key": PUBKEY})
    assert resp.status_code == 400
    assert _player_node_row(db_path, PUBKEY[:8]) is None


# ---- accept refuses a node already claimed by another player -------------

def test_accept_refuses_node_already_claimed_by_another_player(client, db_path, monkeypatch):
    _make_corescope_net(db_path)
    other_player_id = _make_player(db_path, display_name="Other", team="BLUE")

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at, public_key) "
        "VALUES ('mc', ?, ?, ?, ?)",
        (PUBKEY[:8], other_player_id, NOW, PUBKEY),
    )
    conn.commit()
    conn.close()

    player_id = _login(client, db_path)
    assert player_id != other_player_id

    state = {"nodes": [_node(last_heard="2026-09-01T12:00:00Z")]}
    calls: list[str] = []
    _patch_checkin_http(monkeypatch, _corescope_handler(state, calls))

    assert client.post("/api/checkin/confirm/start", json={"name": NAME}).status_code == 200
    state["nodes"][0]["last_heard"] = "2026-09-01T12:05:00Z"

    resp = client.post("/api/checkin/confirm/accept", json={"public_key": PUBKEY})
    assert resp.status_code == 409

    row = _player_node_row(db_path, PUBKEY[:8])
    assert row["player_id"] == other_player_id  # unchanged -- not rebound to the caller


# ---- expired window -------------------------------------------------------

def test_expired_window_status_none_and_accept_409(client, db_path, monkeypatch):
    _make_corescope_net(db_path)
    player_id = _login(client, db_path)

    state = {"nodes": [_node(last_heard="2026-09-01T12:00:00Z")]}
    calls: list[str] = []
    _patch_checkin_http(monkeypatch, _corescope_handler(state, calls))

    assert client.post("/api/checkin/confirm/start", json={"name": NAME}).status_code == 200
    _expire_window(db_path, player_id)

    status_body = client.get("/api/checkin/confirm/status").json()
    assert status_body == {"state": "none"}

    accept_resp = client.post("/api/checkin/confirm/accept", json={"public_key": PUBKEY})
    assert accept_resp.status_code == 409


# ---- the scan throttle does not re-fetch upstream -------------------------

def test_scan_throttle_skips_upstream_refetch_within_window(client, db_path, monkeypatch):
    _make_corescope_net(db_path)
    player_id = _login(client, db_path)

    state = {"nodes": [_node(last_heard="2026-09-01T12:00:00Z")]}
    calls: list[str] = []
    _patch_checkin_http(monkeypatch, _corescope_handler(state, calls))

    # start() always scans (last_scan_at defaults to 0 on insert -- see
    # app/db.py's mc_node_confirmation comment -- so the FIRST status
    # poll after start is never throttled either).
    assert client.post("/api/checkin/confirm/start", json={"name": NAME}).status_code == 200
    assert len(calls) == 1

    first_status = client.get("/api/checkin/confirm/status")
    assert first_status.status_code == 200
    assert len(calls) == 2  # not throttled -- last_scan_at was still 0

    # Immediately polling again lands inside the 8-second throttle
    # window -- no new upstream request should be made.
    second_status = client.get("/api/checkin/confirm/status")
    assert second_status.status_code == 200
    assert len(calls) == 2
    assert second_status.json() == first_status.json()


# ---- retired: GET/POST/DELETE /api/checkin/name --------------------------
#
# The last-resort typed fallback-name routes this module used to carry
# alongside node confirmation above -- see this file's own module
# docstring and app/checkin_api.py's module docstring for why they were
# retired (zero rows bound on preview, node confirmation is strictly
# stronger proof for exactly the players who needed them). No route
# should exist at these paths at all anymore -- not a 401/403 (that
# would mean the route still exists behind auth), a plain 404 from
# FastAPI having nothing registered there.

@pytest.mark.parametrize("method", ["get", "post", "delete"])
def test_retired_checkin_name_routes_are_gone(client, method):
    resp = getattr(client, method)("/api/checkin/name")
    assert resp.status_code == 404
