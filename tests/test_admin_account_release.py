"""Tests for POST /api/admin/player/unlink-account (app/admin_api.py).

This is the admin/operator-only door that clears player.account_id --
the only place in the whole app that can, since app/account_api.py's
link_key() (POST /api/account/link-key) can only ever SET it, never
clear it, by design. See app/admin_api.py's own docstring on the new
route for the full reasoning.

Auth model (privacy-hardening pass): every /api/admin/* route,
including this one, now requires a signed-in account holding a role
(account.role -- 'admin' or 'operator') resolved from the session
cookie by app/admin_api.py's _role_guard(), never the retired
X-Admin-Token header. This file exercises that guard directly (401 with
no session, 401 with a session that holds no role, 404 when the whole
admin surface does not exist on this deployment) alongside the route's
own behavior, which is otherwise unchanged from before this pass.

Same "FastAPI-around-one-router" TestClient shape tests/test_account_api.py
uses, with a real file-backed sqlite database (app/admin_api.py's routes
go through app/db.py's connect()/WriteSession, a fresh connection per
call, so ":memory:" would not share data between them -- same reasoning
tests/test_account_api.py's own db_path fixture gives). Both the admin
router and the account router are mounted on the same app so this file
can also prove the two things the task explicitly called out:
  - a fresh account can link-key a player this route just released
    (the 409 that used to block it is gone)
  - there is no player-facing unlink anywhere (DELETE /api/account/link
    was proposed once, rejected, and must never come back)
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.db as db
from app.account_api import router as account_router
from app.admin_api import router as admin_router
from app.auth import http_exception_as_error_body
from app.db import MIGRATIONS, SCHEMA
from app.mc_ingest import AuthResult
from app.sessions import SESSION_COOKIE_NAME, create_session

GOOD_KEY = "good-key"
KEY_PLAYER_ID = 77  # the player FakeIngestor resolves GOOD_KEY to


class FakeIngestor:
    """Same shape tests/test_account_api.py's own FakeIngestor uses --
    link_key() calls request.app.state.mc_ingestor.authenticate() and
    nothing else on it.
    """

    async def authenticate(self, raw_key: str) -> AuthResult:
        if raw_key == GOOD_KEY:
            return AuthResult("ok", KEY_PLAYER_ID)
        return AuthResult("not_found")

    def invalidate_key(self, key_hash: str) -> None:
        pass

    def invalidate_player(self, player_id: int) -> None:
        pass


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
def client(db_path):
    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(account_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    app.state.mc_ingestor = FakeIngestor()
    return TestClient(app)


def _run(coro):
    return asyncio.run(coro)


def _make_account(path: str, *, role: str | None = None) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO account(created_at, role) VALUES (?, ?)", (int(time.time()), role)
    )
    conn.commit()
    account_id = cur.lastrowid
    conn.close()
    return account_id


def _login_as(client, db_path, *, role: str) -> int:
    """Creates a fresh account holding `role`, signs the TestClient's
    cookie jar into it, and returns the account_id -- the session-based
    replacement for the old `_headers()` helper's X-Admin-Token header.
    """
    account_id = _make_account(db_path, role=role)
    raw_token = _run(create_session(account_id, device_label=None))
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return account_id


def _make_player(
    path: str, *, player_id: int | None = None, account_id: int | None = None,
    display_name="Tester", team="RED",
) -> int:
    conn = sqlite3.connect(path)
    if player_id is None:
        cur = conn.execute(
            "INSERT INTO player(display_name, team, created_at, account_id) VALUES (?, ?, ?, ?)",
            (display_name, team, int(time.time()), account_id),
        )
        player_id = cur.lastrowid
    else:
        conn.execute(
            "INSERT INTO player(player_id, display_name, team, created_at, account_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (player_id, display_name, team, int(time.time()), account_id),
        )
    conn.commit()
    conn.close()
    return player_id


def _add_node(path: str, player_id: int, protocol="mc", node_ref="a1b2c3d4") -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) VALUES (?, ?, ?, ?)",
        (protocol, node_ref, player_id, int(time.time())),
    )
    conn.commit()
    conn.close()


def _add_api_key(path: str, player_id: int, key_hash="deadbeefcafef00d") -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO api_key(key_hash, player_id, issued_at) VALUES (?, ?, ?)",
        (key_hash, player_id, int(time.time())),
    )
    conn.commit()
    conn.close()


# ---- release clears the link, writes the audit event, and the actor's
#      account_id lands in admin_action_log -----------------------------


def test_release_clears_account_id_and_writes_operator_event(client, db_path):
    actor_id = _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    player_id = _make_player(db_path, account_id=account_id, display_name="Malice")

    resp = client.post(
        "/api/admin/player/unlink-account",
        json={"player_id": player_id, "display_name": "Malice"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "player_id": player_id,
        "display_name": "Malice",
        "account_id": account_id,
        "unlinked": True,
    }

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT account_id FROM player WHERE player_id = ?", (player_id,)
    ).fetchone()
    event = conn.execute(
        "SELECT kind, actor, detail FROM account_link_event WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    # The whole point of the roles pass: this action is no longer
    # anonymous -- admin_action_log names the SIGNED-IN account that
    # performed it (see app/db.py's own comment on that table).
    log_row = conn.execute(
        "SELECT actor_account_id, action, detail FROM admin_action_log"
    ).fetchone()
    conn.close()

    assert row[0] is None
    assert event == ("player_unlinked", "operator", f"player_id={player_id}")
    assert log_row[0] == actor_id
    assert log_row[1] == "unlink_account"
    assert str(player_id) in log_row[2]


def test_release_display_name_mismatch_is_409_and_does_not_unlink(client, db_path):
    _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    player_id = _make_player(db_path, account_id=account_id, display_name="Malice")

    resp = client.post(
        "/api/admin/player/unlink-account",
        json={"player_id": player_id, "display_name": "Wrong Name"},
    )

    assert resp.status_code == 409
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT account_id FROM player WHERE player_id = ?", (player_id,)
    ).fetchone()
    conn.close()
    assert row[0] == account_id  # untouched


# ---- releasing a player with no link ------------------------------------


def test_release_not_linked_returns_clear_error_and_writes_no_event(client, db_path):
    _login_as(client, db_path, role="admin")
    player_id = _make_player(db_path, account_id=None, display_name="Loner")

    resp = client.post(
        "/api/admin/player/unlink-account",
        json={"player_id": player_id, "display_name": "Loner"},
    )

    assert resp.status_code == 409
    assert resp.json() == {"error": "player is not linked to any account"}

    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT count(*) FROM account_link_event").fetchone()[0]
    conn.close()
    assert n == 0


# ---- nonexistent player ---------------------------------------------------


def test_release_nonexistent_player_is_404(client, db_path):
    _login_as(client, db_path, role="admin")

    resp = client.post(
        "/api/admin/player/unlink-account",
        json={"player_id": 999999, "display_name": "Nobody"},
    )

    assert resp.status_code == 404
    assert resp.json() == {"error": "player not found"}


# ---- guard: unreachable without a session holding a role -----------------


def test_release_requires_a_signed_in_session(client, db_path):
    # An account exists holding a role (so the admin surface itself is
    # enabled -- see _admin_surface_enabled()), but THIS request carries
    # no session cookie at all.
    _make_account(db_path, role="admin")
    account_id = _make_account(db_path)
    player_id = _make_player(db_path, account_id=account_id, display_name="Malice")

    resp = client.post(
        "/api/admin/player/unlink-account",
        json={"player_id": player_id, "display_name": "Malice"},
        # no cookie, no X-Admin-Token header -- neither authenticates
        # this route any more.
    )

    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT account_id FROM player WHERE player_id = ?", (player_id,)
    ).fetchone()
    conn.close()
    assert row[0] == account_id  # untouched


def test_release_refused_for_a_signed_in_account_with_no_role(client, db_path):
    # A real, valid session -- just for an account that holds no role.
    # This is the guard's OTHER 401 case, and it must read identically
    # to "no session at all" (see _role_guard's own docstring on why).
    _make_account(db_path, role="admin")  # keeps the surface enabled
    _login_as(client, db_path, role=None)
    account_id = _make_account(db_path)
    player_id = _make_player(db_path, account_id=account_id, display_name="Malice")

    resp = client.post(
        "/api/admin/player/unlink-account",
        json={"player_id": player_id, "display_name": "Malice"},
    )

    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_the_retired_header_no_longer_authenticates_anything(client, db_path, monkeypatch):
    """The bypass this whole pass exists to close: holding the old
    admin_token is no longer enough on its own. Even WITH the header
    set to a real configured token, a request carrying no session (or a
    session with no role) still gets refused.
    """
    import app.admin_api as admin_api_module

    monkeypatch.setattr(admin_api_module.settings, "admin_token", "still-configured-for-claiming")
    account_id = _make_account(db_path)
    player_id = _make_player(db_path, account_id=account_id, display_name="Malice")

    resp = client.post(
        "/api/admin/player/unlink-account",
        json={"player_id": player_id, "display_name": "Malice"},
        headers={"X-Admin-Token": "still-configured-for-claiming"},
    )

    # 401, not 200 -- the header is compared nowhere in this route any
    # more (see app/admin_api.py's _role_guard()).
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT account_id FROM player WHERE player_id = ?", (player_id,)
    ).fetchone()
    conn.close()
    assert row[0] == account_id  # untouched


def test_release_404s_when_the_admin_surface_does_not_exist_at_all(client, db_path):
    # Neither settings.admin_token nor any account holding a role --
    # the one state in which the whole admin surface must be
    # indistinguishable from not existing (_admin_surface_enabled()).
    account_id = _make_account(db_path)
    player_id = _make_player(db_path, account_id=account_id, display_name="Malice")

    resp = client.post(
        "/api/admin/player/unlink-account",
        json={"player_id": player_id, "display_name": "Malice"},
    )

    assert resp.status_code == 404
    assert resp.json() == {"error": "not found"}


# ---- the point of the whole feature: the player becomes claimable again --


def test_after_release_link_key_succeeds_for_a_fresh_account(client, db_path):
    _login_as(client, db_path, role="admin")
    stuck_account_id = _make_account(db_path)
    _make_player(
        db_path, player_id=KEY_PLAYER_ID, account_id=stuck_account_id,
        display_name="KeyHolder", team="GREEN",
    )

    # Before release, the 409 link_key() itself is documented to raise
    # for a player already owned by a different account. Uses a
    # SEPARATE TestClient (its own cookie jar) so the admin session set
    # by _login_as() above isn't the one attempting to link-key.
    fresh_account_id = _make_account(db_path)
    player_client = TestClient(client.app)
    raw_token = _run(create_session(fresh_account_id, device_label=None))
    player_client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    blocked = player_client.post("/api/account/link-key", json={"api_key": GOOD_KEY})
    assert blocked.status_code == 409

    release = client.post(
        "/api/admin/player/unlink-account",
        json={"player_id": KEY_PLAYER_ID, "display_name": "KeyHolder"},
    )
    assert release.status_code == 200

    resp = player_client.post("/api/account/link-key", json={"api_key": GOOD_KEY})

    assert resp.status_code == 200
    assert resp.json()["player"] == {
        "player_id": KEY_PLAYER_ID,
        "display_name": "KeyHolder",
        "team": "GREEN",
    }

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT account_id FROM player WHERE player_id = ?", (KEY_PLAYER_ID,)
    ).fetchone()
    conn.close()
    assert row[0] == fresh_account_id


# ---- release is narrow: radios and keys survive --------------------------


def test_release_keeps_player_node_rows_and_api_keys(client, db_path):
    _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    player_id = _make_player(db_path, account_id=account_id, display_name="Malice")
    _add_node(db_path, player_id, protocol="mc", node_ref="a1b2c3d4")
    _add_api_key(db_path, player_id, key_hash="deadbeefcafef00d")

    resp = client.post(
        "/api/admin/player/unlink-account",
        json={"player_id": player_id, "display_name": "Malice"},
    )
    assert resp.status_code == 200

    conn = sqlite3.connect(db_path)
    nodes = conn.execute(
        "SELECT protocol, node_ref FROM player_node WHERE player_id = ?", (player_id,)
    ).fetchall()
    keys = conn.execute(
        "SELECT key_hash FROM api_key WHERE player_id = ?", (player_id,)
    ).fetchall()
    conn.close()

    assert nodes == [("mc", "a1b2c3d4")]
    assert keys == [("deadbeefcafef00d",)]


# ---- there is no player-facing unlink, and there must never be one -------


def test_delete_account_link_does_not_exist(client, db_path):
    """A previous attempt added a player-facing DELETE /api/account/link
    -- session-authenticated self-service unlink. That was rejected and
    reverted: the model is release-by-admin/operator-only (see
    app/admin_api.py's admin_player_unlink_account() docstring). This
    pins the route's absence down so a reintroduction fails a test
    immediately rather than shipping unnoticed.
    """
    account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, device_label=None))
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)

    resp = client.delete("/api/account/link")

    assert resp.status_code == 404
