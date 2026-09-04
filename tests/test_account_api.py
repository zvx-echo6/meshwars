"""Tests for app/account_api.py's router: GET /api/account, POST
/api/account/link-key, POST /api/account/logout[-all].

Builds a bare FastAPI app around just this router (same
"FastAPI-around-one-router" spirit as tests/test_auth.py's own
_client_for and tests/test_tiles_api.py), with a FakeIngestor standing
in for request.app.state.mc_ingestor -- account_api.py's link-key route
calls the exact same .authenticate() method every other key-
authenticated route in this app already does, so a fake with the same
shape tests/test_auth.py already uses is enough.

Real file-backed sqlite database, same fixture shape and reasoning as
tests/test_sessions.py -- account_api.py's routes read/write through
app/db.py's connect()/WriteSession, which is a fresh connection per
call, so ":memory:" would not share data between them.
"""
from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.db as db
from app.account_api import router as account_router
from app.auth import http_exception_as_error_body
from app.db import MIGRATIONS, SCHEMA
from app.mc_ingest import AuthResult
from app.sessions import SESSION_COOKIE_NAME, create_session

GOOD_KEY = "good-key"
DISABLED_KEY = "disabled-key"
REVOKED_KEY = "revoked-key"
KEY_PLAYER_ID = 42  # the player FakeIngestor resolves GOOD_KEY/DISABLED_KEY/REVOKED_KEY to


class FakeIngestor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def authenticate(self, raw_key: str) -> AuthResult:
        self.calls.append(raw_key)
        if raw_key == GOOD_KEY:
            return AuthResult("ok", KEY_PLAYER_ID)
        if raw_key == DISABLED_KEY:
            return AuthResult("disabled", KEY_PLAYER_ID)
        if raw_key == REVOKED_KEY:
            return AuthResult("revoked", KEY_PLAYER_ID)
        return AuthResult("not_found")


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
def _reset_link_key_rate_limiter():
    """account_api.py's _link_key_addr_limiter is a module-level
    singleton (see app/auth.py's module docstring on why every
    _BoundedHits budget in this codebase is built once, at import
    time, rather than per-request) -- it accumulates hits across every
    test in this file, keyed on TestClient's fixed synthetic peer
    address ("testclient"), unless cleared between tests. Same pattern
    tests/test_auth.py's own _reset() helper uses for the same reason.
    """
    import app.account_api as account_api_module

    account_api_module._link_key_addr_limiter._hits.clear()
    yield
    account_api_module._link_key_addr_limiter._hits.clear()


@pytest.fixture
def client(db_path):
    app = FastAPI()
    app.include_router(account_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    app.state.mc_ingestor = FakeIngestor()
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


def _make_player(path: str, *, account_id: int | None = None, display_name="Tester", team="RED") -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO player(display_name, team, created_at, account_id) VALUES (?, ?, ?, ?)",
        (display_name, team, int(time.time()), account_id),
    )
    conn.commit()
    player_id = cur.lastrowid
    conn.close()
    return player_id


def _login(client: TestClient, db_path: str, *, account_id: int | None = None) -> tuple[int, str]:
    """Create an account (unless given) and a session for it, and set
    that session's cookie on `client` for subsequent requests. Returns
    (account_id, raw_token).
    """
    if account_id is None:
        account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, device_label="Firefox on Windows"))
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return account_id, raw_token


# ---- auth gate: every route requires a session --------------------------

@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/account"),
        ("POST", "/api/account/link-key"),
        ("POST", "/api/account/logout"),
        ("POST", "/api/account/logout-all"),
    ],
)
def test_every_route_requires_a_session(client, method, path):
    resp = client.request(method, path)
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


# ---- GET /api/account -----------------------------------------------------

def test_get_account_shape_with_no_player_and_no_identities(client, db_path):
    account_id, _ = _login(client, db_path)

    resp = client.get("/api/account")

    assert resp.status_code == 200
    body = resp.json()
    assert body["account_id"] == account_id
    assert body["identities"] == []
    assert body["player"] is None
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["current"] is True
    assert body["sessions"][0]["device_label"] == "Firefox on Windows"
    # Privacy hardening (app/db.py's account_session comment): no IP
    # address is stored anywhere anymore, so the sessions payload must
    # never carry an "ip" key at all -- not null, not omitted-by-
    # accident, structurally absent.
    assert "ip" not in body["sessions"][0]


def test_get_account_includes_linked_player(client, db_path):
    account_id, _ = _login(client, db_path)
    player_id = _make_player(db_path, account_id=account_id, display_name="Malice", team="BLUE")

    body = client.get("/api/account").json()

    assert body["player"] == {"player_id": player_id, "display_name": "Malice", "team": "BLUE"}


def test_get_account_masks_identity_emails(client, db_path):
    account_id, _ = _login(client, db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('google', 'sub-123', ?, 'jdoe@example.com', 1, ?)",
        (account_id, int(time.time())),
    )
    conn.commit()
    conn.close()

    body = client.get("/api/account").json()

    assert len(body["identities"]) == 1
    identity = body["identities"][0]
    assert identity["provider"] == "google"
    assert identity["label"] == "Google"
    assert identity["email"] == "j***@example.com"
    assert "jdoe" not in identity["email"]
    assert identity["email_verified"] is True


# ---- POST /api/account/link-key -------------------------------------------

def test_link_key_success(client, db_path):
    account_id, _ = _login(client, db_path)

    # FakeIngestor always resolves GOOD_KEY to KEY_PLAYER_ID -- insert a
    # player row with that exact id to be linked and read back.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?, ?, ?, ?)",
        (KEY_PLAYER_ID, "KeyHolder", "GREEN", int(time.time())),
    )
    conn.commit()
    conn.close()

    resp = client.post("/api/account/link-key", json={"api_key": GOOD_KEY})

    assert resp.status_code == 200
    assert resp.json()["player"] == {
        "player_id": KEY_PLAYER_ID,
        "display_name": "KeyHolder",
        "team": "GREEN",
    }

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT account_id FROM player WHERE player_id = ?", (KEY_PLAYER_ID,)).fetchone()
    event = conn.execute(
        "SELECT kind, actor FROM account_link_event WHERE account_id = ?", (account_id,)
    ).fetchone()
    conn.close()
    assert row[0] == account_id
    assert event == ("player_linked", "user")


def test_link_key_relinking_the_same_already_linked_player_is_a_success_noop(client, db_path):
    """The exact bug this session was created to fix: a retried/double
    -clicked link-key call naming the player ALREADY linked to THIS
    account is not a conflict -- the desired end state already holds.
    Must return 200 with the player, same shape a fresh link returns,
    and must NOT double the original account_link_event.
    """
    account_id, _ = _login(client, db_path)

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at, account_id) VALUES (?, ?, ?, ?, ?)",
        (KEY_PLAYER_ID, "KeyHolder", "GREEN", int(time.time()), account_id),
    )
    conn.execute(
        "INSERT INTO account_link_event(account_id, kind, detail, actor, created_at) "
        "VALUES (?, 'player_linked', ?, 'user', ?)",
        (account_id, f"player_id={KEY_PLAYER_ID}", int(time.time())),
    )
    conn.commit()
    conn.close()

    resp = client.post("/api/account/link-key", json={"api_key": GOOD_KEY})

    assert resp.status_code == 200
    assert resp.json()["player"] == {
        "player_id": KEY_PLAYER_ID,
        "display_name": "KeyHolder",
        "team": "GREEN",
    }

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT account_id FROM player WHERE player_id = ?", (KEY_PLAYER_ID,)).fetchone()
    events = conn.execute(
        "SELECT kind FROM account_link_event WHERE account_id = ?", (account_id,)
    ).fetchall()
    conn.close()
    assert row[0] == account_id  # still linked
    assert len(events) == 1  # the original event, not doubled by the retry


def test_link_key_refused_when_account_already_has_a_player(client, db_path):
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id)  # account already linked to a different player

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?, ?, ?, ?)",
        (KEY_PLAYER_ID, "KeyHolder", "GREEN", int(time.time())),
    )
    conn.commit()
    conn.close()

    resp = client.post("/api/account/link-key", json={"api_key": GOOD_KEY})

    assert resp.status_code == 409
    assert resp.json() == {"error": "this account already has a linked player"}

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT account_id FROM player WHERE player_id = ?", (KEY_PLAYER_ID,)).fetchone()
    conn.close()
    assert row[0] is None  # never linked


def test_link_key_refused_when_player_already_owned_by_another_account(client, db_path):
    _login(client, db_path)
    other_account_id = _make_account(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at, account_id) VALUES (?, ?, ?, ?, ?)",
        (KEY_PLAYER_ID, "KeyHolder", "GREEN", int(time.time()), other_account_id),
    )
    conn.commit()
    conn.close()

    resp = client.post("/api/account/link-key", json={"api_key": GOOD_KEY})

    assert resp.status_code == 409
    assert resp.json() == {"error": "that key's player is already linked to a different account"}


def test_link_key_bad_key_is_401(client, db_path):
    _login(client, db_path)

    resp = client.post("/api/account/link-key", json={"api_key": "never-issued"})

    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_link_key_disabled_player_is_403(client, db_path):
    _login(client, db_path)

    resp = client.post("/api/account/link-key", json={"api_key": DISABLED_KEY})

    assert resp.status_code == 403
    assert resp.json() == {"error": "forbidden"}


def test_link_key_missing_body_field_is_400(client, db_path):
    _login(client, db_path)

    resp = client.post("/api/account/link-key", json={})

    assert resp.status_code == 400


def test_link_key_rate_limited(client, db_path, monkeypatch):
    monkeypatch.setattr(db.settings, "account_link_key_rate_limit_attempts", 1)
    monkeypatch.setattr(db.settings, "account_link_key_rate_limit_window_seconds", 60)
    _login(client, db_path)

    r1 = client.post("/api/account/link-key", json={"api_key": "whatever"})
    assert r1.status_code == 401  # bad key, budget of 1 consumed

    r2 = client.post("/api/account/link-key", json={"api_key": "whatever"})
    assert r2.status_code == 429
    assert r2.json() == {"error": "rate limited"}


# ---- logout / logout-all --------------------------------------------------

def test_logout_revokes_the_current_session_and_clears_the_cookie(client, db_path):
    from app.sessions import verify_session

    account_id, raw_token = _login(client, db_path)

    resp = client.post("/api/account/logout")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert "set-cookie" in resp.headers
    result = _run(verify_session(raw_token))
    assert result.status == "revoked"


def test_logout_does_not_touch_other_sessions_on_the_account(client, db_path):
    from app.sessions import verify_session

    account_id, raw_token_a = _login(client, db_path)
    raw_token_b = _run(create_session(account_id, device_label=None))

    client.post("/api/account/logout")

    assert _run(verify_session(raw_token_a)).status == "revoked"
    assert _run(verify_session(raw_token_b)).status == "ok"


def test_logout_all_revokes_every_session_on_the_account(client, db_path):
    from app.sessions import verify_session

    account_id, raw_token_a = _login(client, db_path)
    raw_token_b = _run(create_session(account_id, device_label=None))

    resp = client.post("/api/account/logout-all")

    assert resp.status_code == 200
    assert _run(verify_session(raw_token_a)).status == "revoked"
    assert _run(verify_session(raw_token_b)).status == "revoked"
