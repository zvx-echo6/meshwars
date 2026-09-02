"""Tests for app/sessions.py -- account session create/verify/touch/
revoke, the last_seen_at throttling that keeps a verify from writing on
every request, cookie flags, and the require_session() FastAPI
dependency.

Uses a real file-backed sqlite database (not :memory:) via a `db_path`
fixture, same reasoning tests/test_write_session.py's own fixture
documents: app/db.py's connect() opens a FRESH connection every call,
and two independent connections to ":memory:" do not see each other's
data at all -- a real temp file is the only way WriteSession's
BEGIN IMMEDIATE and a plain read-only connect() actually share state,
the way they do in production.

No pytest-asyncio is configured anywhere in this repo (see that same
file's docstring) -- coroutines here are driven with asyncio.run(...)
from ordinary sync test functions, matching every other async test in
this codebase.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from fastapi import HTTPException, Request
from fastapi.responses import Response

import app.db as db
from app.db import MIGRATIONS, SCHEMA, WriteSession, connect
from app.sessions import (
    SESSION_COOKIE_NAME,
    clear_session_cookie,
    create_session,
    require_session,
    revoke_all_sessions,
    revoke_session,
    set_session_cookie,
    verify_session,
)


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


def _run(coro):
    return asyncio.run(coro)


def _make_account(path: str) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    conn.commit()
    account_id = cur.lastrowid
    conn.close()
    return account_id


def _make_player(path: str, *, account_id: int | None = None) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO player(display_name, team, created_at, account_id) VALUES (?, ?, ?, ?)",
        ("Tester", "RED", int(time.time()), account_id),
    )
    conn.commit()
    player_id = cur.lastrowid
    conn.close()
    return player_id


def _row_for_token(path: str, token_hash: str) -> sqlite3.Row:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM account_session WHERE token_hash = ?", (token_hash,)
    ).fetchone()
    conn.close()
    return row


# ---- create / verify ----------------------------------------------------

def test_create_session_returns_a_raw_token_and_stores_only_its_hash(db_path):
    account_id = _make_account(db_path)

    raw_token = _run(create_session(account_id, user_agent="pytest", ip="203.0.113.5"))

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT token_hash, account_id, user_agent, ip FROM account_session").fetchall()
    conn.close()
    assert len(rows) == 1
    token_hash, stored_account_id, user_agent, ip = rows[0]
    assert stored_account_id == account_id
    assert user_agent == "pytest"
    assert ip == "203.0.113.5"
    # The raw token is never stored verbatim.
    assert token_hash != raw_token


def test_verify_session_ok_for_a_freshly_created_session(db_path):
    account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, user_agent=None, ip=None))

    result = _run(verify_session(raw_token))

    assert result.status == "ok"
    assert result.account_id == account_id


def test_verify_session_not_found_for_unknown_token(db_path):
    result = _run(verify_session("this-token-was-never-issued"))
    assert result.status == "not_found"
    assert result.account_id is None


def test_verify_session_not_found_for_empty_token(db_path):
    result = _run(verify_session(""))
    assert result.status == "not_found"


def test_verify_session_expired(db_path):
    account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, user_agent=None, ip=None))
    token_hash = _run(verify_session(raw_token)).token_hash

    # Force the row into the past.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE account_session SET expires_at = ? WHERE token_hash = ?",
        (int(time.time()) - 10, token_hash),
    )
    conn.commit()
    conn.close()

    result = _run(verify_session(raw_token))
    assert result.status == "expired"
    assert result.account_id == account_id


def test_verify_session_revoked_takes_priority_over_expiry(db_path):
    """A session that is BOTH revoked and past its expiry must read as
    revoked, not expired -- app/sessions.py checks revoked_at first.
    """
    account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, user_agent=None, ip=None))
    token_hash = _run(verify_session(raw_token)).token_hash

    conn = sqlite3.connect(db_path)
    now = int(time.time())
    conn.execute(
        "UPDATE account_session SET expires_at = ?, revoked_at = ? WHERE token_hash = ?",
        (now - 10, now - 5, token_hash),
    )
    conn.commit()
    conn.close()

    result = _run(verify_session(raw_token))
    assert result.status == "revoked"


# ---- last_seen_at throttling ---------------------------------------------

def test_verify_within_touch_threshold_does_not_write_last_seen_at(db_path, monkeypatch):
    monkeypatch.setattr(db.settings, "account_session_touch_threshold_seconds", 300)
    account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, user_agent=None, ip=None))
    first = _run(verify_session(raw_token))
    original_row = _row_for_token(db_path, first.token_hash)

    # Move last_seen_at to just inside the threshold (not stale enough
    # to trigger a write).
    conn = sqlite3.connect(db_path)
    fresh_last_seen = int(time.time()) - 60  # well under the 300s threshold
    conn.execute(
        "UPDATE account_session SET last_seen_at = ? WHERE token_hash = ?",
        (fresh_last_seen, first.token_hash),
    )
    conn.commit()
    conn.close()

    _run(verify_session(raw_token))

    row_after = _row_for_token(db_path, first.token_hash)
    assert row_after["last_seen_at"] == fresh_last_seen  # unchanged
    assert row_after["expires_at"] == original_row["expires_at"]  # unchanged


def test_verify_past_touch_threshold_writes_a_fresh_last_seen_at_and_slides_expiry(db_path, monkeypatch):
    monkeypatch.setattr(db.settings, "account_session_touch_threshold_seconds", 300)
    monkeypatch.setattr(db.settings, "account_session_lifetime_seconds", 1000)
    account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, user_agent=None, ip=None))
    first = _run(verify_session(raw_token))

    stale_last_seen = int(time.time()) - 400  # older than the 300s threshold
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE account_session SET last_seen_at = ? WHERE token_hash = ?",
        (stale_last_seen, first.token_hash),
    )
    conn.commit()
    conn.close()

    before = int(time.time())
    _run(verify_session(raw_token))
    after = int(time.time())

    row = _row_for_token(db_path, first.token_hash)
    assert before <= row["last_seen_at"] <= after
    # expires_at slides forward from the fresh last_seen_at, not the
    # original created_at.
    assert row["expires_at"] >= before + 1000


# ---- revoke ---------------------------------------------------------------

def test_revoke_session_makes_it_unverifiable(db_path):
    account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, user_agent=None, ip=None))
    token_hash = _run(verify_session(raw_token)).token_hash

    _run(revoke_session(token_hash))

    result = _run(verify_session(raw_token))
    assert result.status == "revoked"


def test_revoke_session_is_idempotent(db_path):
    account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, user_agent=None, ip=None))
    token_hash = _run(verify_session(raw_token)).token_hash

    _run(revoke_session(token_hash))
    _run(revoke_session(token_hash))  # must not raise

    row = _row_for_token(db_path, token_hash)
    assert row["revoked_at"] is not None


def test_revoke_session_on_unknown_hash_is_a_silent_no_op(db_path):
    _run(revoke_session("never-existed"))  # must not raise


def test_revoke_all_sessions_revokes_every_session_on_the_account_only(db_path):
    account_a = _make_account(db_path)
    account_b = _make_account(db_path)
    token_a1 = _run(create_session(account_a, user_agent=None, ip=None))
    token_a2 = _run(create_session(account_a, user_agent=None, ip=None))
    token_b = _run(create_session(account_b, user_agent=None, ip=None))

    revoked_count = _run(revoke_all_sessions(account_a))

    assert revoked_count == 2
    assert _run(verify_session(token_a1)).status == "revoked"
    assert _run(verify_session(token_a2)).status == "revoked"
    # Account B's session is untouched.
    assert _run(verify_session(token_b)).status == "ok"


def test_revoke_all_sessions_returns_zero_when_none_are_active(db_path):
    account_id = _make_account(db_path)
    assert _run(revoke_all_sessions(account_id)) == 0


# ---- cookie flags -----------------------------------------------------

def test_set_session_cookie_sets_expected_flags(monkeypatch):
    monkeypatch.setattr(db.settings, "account_session_cookie_secure", True)
    response = Response()
    set_session_cookie(response, "raw-token-value")

    header = response.headers["set-cookie"]
    assert f"{SESSION_COOKIE_NAME}=raw-token-value" in header
    assert "HttpOnly" in header
    assert "SameSite=lax" in header or "samesite=lax" in header.lower()
    assert "Secure" in header
    assert "Path=/" in header


def test_set_session_cookie_omits_secure_when_configured_off(monkeypatch):
    monkeypatch.setattr(db.settings, "account_session_cookie_secure", False)
    response = Response()
    set_session_cookie(response, "raw-token-value")

    header = response.headers["set-cookie"]
    assert "Secure" not in header


def test_clear_session_cookie_expires_it(monkeypatch):
    monkeypatch.setattr(db.settings, "account_session_cookie_secure", True)
    response = Response()
    clear_session_cookie(response)

    header = response.headers["set-cookie"]
    assert SESSION_COOKIE_NAME in header
    # Starlette expires a deleted cookie via Max-Age=0 / an epoch date.
    assert "Max-Age=0" in header or "1970" in header


# ---- require_session() dependency ----------------------------------------

def _request_with_cookie(token: str | None) -> Request:
    headers = []
    if token is not None:
        headers.append((b"cookie", f"{SESSION_COOKIE_NAME}={token}".encode("latin-1")))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/account",
        "query_string": b"",
        "http_version": "1.1",
        "client": ("198.51.100.10", 51234),
        "headers": headers,
    }
    return Request(scope)


def test_require_session_returns_principal_with_linked_player(db_path):
    account_id = _make_account(db_path)
    player_id = _make_player(db_path, account_id=account_id)
    raw_token = _run(create_session(account_id, user_agent="pytest", ip="198.51.100.10"))

    principal = _run(require_session(_request_with_cookie(raw_token)))

    assert principal.account_id == account_id
    assert principal.player_id == player_id


def test_require_session_player_id_none_when_no_linked_player(db_path):
    account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, user_agent=None, ip=None))

    principal = _run(require_session(_request_with_cookie(raw_token)))

    assert principal.account_id == account_id
    assert principal.player_id is None


def test_require_session_missing_cookie_is_401(db_path):
    with pytest.raises(HTTPException) as exc_info:
        _run(require_session(_request_with_cookie(None)))
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "unauthorized"


def test_require_session_bad_token_is_401(db_path):
    with pytest.raises(HTTPException) as exc_info:
        _run(require_session(_request_with_cookie("garbage-token")))
    assert exc_info.value.status_code == 401


def test_require_session_revoked_token_is_401(db_path):
    account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, user_agent=None, ip=None))
    token_hash = _run(verify_session(raw_token)).token_hash
    _run(revoke_session(token_hash))

    with pytest.raises(HTTPException) as exc_info:
        _run(require_session(_request_with_cookie(raw_token)))
    assert exc_info.value.status_code == 401
