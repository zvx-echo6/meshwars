"""Tests for app/join_api.py's POST /api/join -- both the original
anonymous path (invite code required) and the session-based path added
for an authenticated account with no linked player yet (no invite code
required, and the new player is auto-linked to that account).

Builds a bare FastAPI app around just this router (same
"FastAPI-around-one-router" spirit as tests/test_account_api.py and
tests/test_auth.py's own _client_for), with a real file-backed sqlite
database -- join()'s own DB work goes through app/db.py's connect()
directly (not the async WriteSession app/account_api.py's routes use),
so a plain sqlite3 connection per call is exactly what production does
here too; ":memory:" would not share data between them the way a real
file does (same reasoning tests/test_account_api.py's own db_path
fixture documents).

Nothing here exercises /api/join/redeem -- that route is unrelated to
the invite-code/session gate this file is about.
"""
from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.db as db
from app.auth import http_exception_as_error_body
from app.db import MIGRATIONS, SCHEMA
from app.join_api import router as join_router
from app.sessions import SESSION_COOKIE_NAME, create_session

INVITE_CODE = "letmein"


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
    # app/join_api.py imports the SAME settings singleton app/db.py does
    # (both `from .config import settings`), so patching it via either
    # module's own reference mutates the one object both read --
    # db.settings is used here only because tests/test_account_api.py's
    # existing db_path fixture already establishes that pattern.
    monkeypatch.setattr(db.settings, "db_path", path)
    monkeypatch.setattr(db.settings, "join_invite_code", INVITE_CODE)
    return path


@pytest.fixture(autouse=True)
def _reset_join_rate_limiter():
    """app/join_api.py's `_attempts` dict is a module-level singleton,
    keyed on client IP -- TestClient's fixed synthetic peer address
    means every test in this file would otherwise share one budget and
    start 429ing each other out. Same pattern tests/test_account_api.py's
    own _reset_link_key_rate_limiter fixture uses for the identical
    reason, applied to join_api's own tracking dict instead of an
    account_api _BoundedHits bucket.
    """
    import app.join_api as join_api_module

    join_api_module._attempts.clear()
    yield
    join_api_module._attempts.clear()


@pytest.fixture
def client(db_path):
    app = FastAPI()
    app.include_router(join_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    return TestClient(app)


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def _make_account(path: str) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    conn.commit()
    account_id = cur.lastrowid
    conn.close()
    return account_id


def _make_player(path: str, *, account_id: int | None = None, display_name="Existing", team="RED") -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO player(display_name, team, created_at, account_id) VALUES (?, ?, ?, ?)",
        (display_name, team, int(time.time()), account_id),
    )
    conn.commit()
    player_id = cur.lastrowid
    conn.close()
    return player_id


def _login(client: TestClient, db_path: str, *, account_id: int | None = None) -> int:
    """Create an account (unless given) and a session for it, and set
    that session's cookie on `client` for subsequent requests -- same
    helper shape as tests/test_account_api.py's own _login(). Returns
    the account_id.
    """
    if account_id is None:
        account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, device_label="Firefox on Windows"))
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return account_id


def _body(**overrides) -> dict:
    body = {
        "invite_code": INVITE_CODE,
        "display_name": "Newbie",
        "team": "RED",
        "protocol": "mc",
    }
    body.update(overrides)
    return body


def _player_row(path: str, player_id: int):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM player WHERE player_id = ?", (player_id,)).fetchone()
    conn.close()
    return row


# ---- anonymous path: unchanged behavior ------------------------------

def test_anonymous_join_requires_a_valid_invite_code(client):
    resp = client.post("/api/join", json=_body(invite_code="wrong-code"))
    assert resp.status_code == 403
    assert resp.json() == {"error": "invalid invite code"}


def test_anonymous_join_refused_with_no_invite_code_at_all(client):
    body = _body()
    del body["invite_code"]
    resp = client.post("/api/join", json=body)
    assert resp.status_code == 403
    assert resp.json() == {"error": "invalid invite code"}


def test_anonymous_join_succeeds_with_the_right_invite_code(client, db_path):
    resp = client.post("/api/join", json=_body(display_name="AnonPlayer"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["display_name"] == "AnonPlayer"
    assert data["team"] == "RED"
    assert "key" in data and data["key"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT account_id FROM player WHERE display_name = 'AnonPlayer'"
    ).fetchone()
    conn.close()
    # No session on this request -- the new player is not linked to
    # any account, exactly as before this change.
    assert row["account_id"] is None


def test_registration_closed_returns_503_regardless_of_session(client, db_path, monkeypatch):
    monkeypatch.setattr(db.settings, "join_invite_code", "")
    _login(client, db_path)
    resp = client.post("/api/join", json=_body())
    assert resp.status_code == 503
    assert resp.json() == {"error": "registration is currently closed"}


# ---- session-based path: no invite code needed ------------------------

def test_authenticated_caller_can_join_without_an_invite_code(client, db_path):
    account_id = _login(client, db_path)

    body = _body(display_name="AccountJoiner")
    del body["invite_code"]
    resp = client.post("/api/join", json=body)

    assert resp.status_code == 200
    data = resp.json()
    assert data["display_name"] == "AccountJoiner"
    assert data["team"] == "RED"
    assert "key" in data and data["key"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT player_id, account_id FROM player WHERE display_name = 'AccountJoiner'"
    ).fetchone()
    conn.close()
    # Session-based join links the new player to the calling account,
    # in the same request -- no separate POST /api/account/link-key
    # call is needed afterward.
    assert row["account_id"] == account_id


def test_authenticated_caller_with_a_wrong_code_is_not_refused_for_that(client, db_path):
    """The code is simply irrelevant to a signed-in caller -- whatever
    is sent, right or wrong, is never looked at.
    """
    _login(client, db_path)
    resp = client.post("/api/join", json=_body(invite_code="totally-wrong", display_name="StillWorks"))
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "StillWorks"


def test_session_join_writes_a_player_linked_account_link_event(client, db_path):
    account_id = _login(client, db_path)
    body = _body(display_name="EventPlayer")
    del body["invite_code"]
    resp = client.post("/api/join", json=body)
    assert resp.status_code == 200

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT kind, actor FROM account_link_event WHERE account_id = ?", (account_id,)
    ).fetchone()
    conn.close()
    assert row["kind"] == "player_linked"
    assert row["actor"] == "user"


def test_authenticated_caller_already_linked_is_refused(client, db_path):
    account_id = _make_account(db_path)
    _make_player(db_path, account_id=account_id, display_name="AlreadyLinked", team="BLUE")
    _login(client, db_path, account_id=account_id)

    body = _body(display_name="SecondPlayer")
    del body["invite_code"]
    resp = client.post("/api/join", json=body)

    assert resp.status_code == 409
    assert resp.json() == {"error": "this account already has a linked player"}

    # No second player was created for this display name.
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT 1 FROM player WHERE display_name = 'SecondPlayer'"
    ).fetchone()
    conn.close()
    assert row is None


def test_expired_session_falls_back_to_the_anonymous_invite_code_gate(client, db_path):
    # A garbage/expired cookie must never 401 this public route -- it
    # has to behave exactly like no cookie at all (optional_session(),
    # not require_session()).
    client.cookies.set(SESSION_COOKIE_NAME, "not-a-real-token")
    body = _body(display_name="StillAnon")
    del body["invite_code"]
    resp = client.post("/api/join", json=body)
    assert resp.status_code == 403
    assert resp.json() == {"error": "invalid invite code"}
