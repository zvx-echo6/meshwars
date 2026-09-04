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

    raw_token = _run(create_session(account_id, device_label="Firefox on Windows"))

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT token_hash, account_id, device_label FROM account_session").fetchall()
    conn.close()
    assert len(rows) == 1
    token_hash, stored_account_id, device_label = rows[0]
    assert stored_account_id == account_id
    assert device_label == "Firefox on Windows"
    # The raw token is never stored verbatim.
    assert token_hash != raw_token


def test_account_session_table_has_no_ip_column(db_path):
    """Privacy hardening (see account_session's own SCHEMA comment in
    app/db.py): the table must not have an `ip` column at all anymore
    -- not present-but-unused, not nullable-and-blank, physically
    gone. Guards against a future change accidentally reintroducing it
    (e.g. a careless ALTER TABLE ADD COLUMN ip during unrelated work).
    """
    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(account_session)")}
    conn.close()
    assert "ip" not in cols
    assert "device_label" in cols


def test_create_session_with_no_device_label_stores_null(db_path):
    """A caller with nothing to label (no User-Agent header at all)
    passes device_label=None -- this must store NULL, not the string
    "None" or an empty string, so app/account_api.py's _sessions_out()
    and the frontend can tell "nothing to show" apart from an actual
    (if degraded) label like "Unknown device".
    """
    account_id = _make_account(db_path)

    raw_token = _run(create_session(account_id, device_label=None))

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT device_label FROM account_session WHERE token_hash = (SELECT token_hash FROM account_session)"
    ).fetchone()
    conn.close()
    assert row[0] is None
    assert raw_token  # sanity: a token was still minted


def test_verify_session_ok_for_a_freshly_created_session(db_path):
    account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, device_label=None))

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
    raw_token = _run(create_session(account_id, device_label=None))
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
    raw_token = _run(create_session(account_id, device_label=None))
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
    raw_token = _run(create_session(account_id, device_label=None))
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
    raw_token = _run(create_session(account_id, device_label=None))
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
    raw_token = _run(create_session(account_id, device_label=None))
    token_hash = _run(verify_session(raw_token)).token_hash

    _run(revoke_session(token_hash))

    result = _run(verify_session(raw_token))
    assert result.status == "revoked"


def test_revoke_session_is_idempotent(db_path):
    account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, device_label=None))
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
    token_a1 = _run(create_session(account_a, device_label=None))
    token_a2 = _run(create_session(account_a, device_label=None))
    token_b = _run(create_session(account_b, device_label=None))

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
    raw_token = _run(create_session(account_id, device_label="Firefox on Windows"))

    principal = _run(require_session(_request_with_cookie(raw_token)))

    assert principal.account_id == account_id
    assert principal.player_id == player_id


def test_require_session_player_id_none_when_no_linked_player(db_path):
    account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, device_label=None))

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
    raw_token = _run(create_session(account_id, device_label=None))
    token_hash = _run(verify_session(raw_token)).token_hash
    _run(revoke_session(token_hash))

    with pytest.raises(HTTPException) as exc_info:
        _run(require_session(_request_with_cookie(raw_token)))
    assert exc_info.value.status_code == 401


# ---- stale-row sweep (app/sessions.py's _sweep_stale_sessions) -----------
#
# The sweep only ever runs inline inside create_session() (see that
# function's own docstring for why login is the trigger point, not a
# timer) -- so every test below drives it the same way: force a
# target row into whatever dead-and-old shape it wants to test, then
# call create_session() again (for an unrelated account, so its own
# fresh row can never be the one under test) to fire the sweep, and
# only then assert on the target row.

def test_sweep_removes_a_long_expired_session(db_path):
    account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, device_label=None))
    token_hash = _run(verify_session(raw_token)).token_hash

    # Expired well past the grace period -- definitively dead.
    conn = sqlite3.connect(db_path)
    stale_cutoff = int(time.time()) - 7200  # 2 hours ago, grace is 1 hour
    conn.execute(
        "UPDATE account_session SET expires_at = ? WHERE token_hash = ?",
        (stale_cutoff, token_hash),
    )
    conn.commit()
    conn.close()

    other_account = _make_account(db_path)
    _run(create_session(other_account, device_label=None))  # fires the sweep

    assert _row_for_token(db_path, token_hash) is None


def test_sweep_removes_a_revoked_session_once_past_grace(db_path):
    account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, device_label=None))
    token_hash = _run(verify_session(raw_token)).token_hash
    _run(revoke_session(token_hash))

    # Push revoked_at back past the grace period by hand -- a real
    # logout only just happened, but the sweep should treat a logout
    # from long ago the same as one from just now, once grace elapses.
    conn = sqlite3.connect(db_path)
    old_revoke = int(time.time()) - 7200  # 2 hours ago, grace is 1 hour
    conn.execute(
        "UPDATE account_session SET revoked_at = ? WHERE token_hash = ?",
        (old_revoke, token_hash),
    )
    conn.commit()
    conn.close()

    other_account = _make_account(db_path)
    _run(create_session(other_account, device_label=None))  # fires the sweep

    assert _row_for_token(db_path, token_hash) is None


def test_sweep_leaves_a_revoked_session_inside_grace(db_path):
    account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, device_label=None))
    token_hash = _run(verify_session(raw_token)).token_hash
    _run(revoke_session(token_hash))  # revoked_at = now, well inside grace

    other_account = _make_account(db_path)
    _run(create_session(other_account, device_label=None))  # fires the sweep

    # Still present -- the grace period exists precisely so a session
    # that just went dead is not yanked out from under a concurrent
    # reader.
    assert _row_for_token(db_path, token_hash) is not None


def test_sweep_never_removes_a_live_session(db_path, monkeypatch):
    """A live session (expires_at in the future, revoked_at NULL) must
    survive the sweep no matter what -- checked here even with the
    grace period forced to zero, since the WHERE clause itself, not
    the grace constant, is what protects a live row (see
    _sweep_stale_sessions()'s own comment on why no value of grace can
    make it match).
    """
    import app.sessions as sessions_module
    monkeypatch.setattr(sessions_module, "_SWEEP_GRACE_SECONDS", 0)

    account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, device_label=None))
    token_hash = _run(verify_session(raw_token)).token_hash

    other_account = _make_account(db_path)
    _run(create_session(other_account, device_label=None))  # fires the sweep

    assert _row_for_token(db_path, token_hash) is not None
    assert _run(verify_session(raw_token)).status == "ok"


def test_swept_session_fails_authentication_the_same_way_a_revoked_one_does(db_path):
    """Once a dead row is actually deleted, presenting its old cookie
    must be indistinguishable, to the caller, from that session having
    been revoked -- both go through require_session()'s single
    `result.status != "ok"` check into the exact same 401
    {"error": "unauthorized"} response (see require_session()'s own
    docstring: it never reveals which failure mode applied). This test
    proves that holds for "not_found because the row was swept," not
    just "not_found because the token was never real."
    """
    account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, device_label=None))
    token_hash = _run(verify_session(raw_token)).token_hash

    conn = sqlite3.connect(db_path)
    stale_cutoff = int(time.time()) - 7200
    conn.execute(
        "UPDATE account_session SET expires_at = ? WHERE token_hash = ?",
        (stale_cutoff, token_hash),
    )
    conn.commit()
    conn.close()

    other_account = _make_account(db_path)
    _run(create_session(other_account, device_label=None))  # fires the sweep
    assert _row_for_token(db_path, token_hash) is None  # row is really gone

    # A revoked session hits 401 via SessionResult("revoked", ...).
    revoked_account = _make_account(db_path)
    revoked_raw = _run(create_session(revoked_account, device_label=None))
    revoked_hash = _run(verify_session(revoked_raw)).token_hash
    _run(revoke_session(revoked_hash))

    swept_result = _run(verify_session(raw_token))
    assert swept_result.status == "not_found"

    with pytest.raises(HTTPException) as swept_exc:
        _run(require_session(_request_with_cookie(raw_token)))
    with pytest.raises(HTTPException) as revoked_exc:
        _run(require_session(_request_with_cookie(revoked_raw)))

    assert swept_exc.value.status_code == revoked_exc.value.status_code == 401
    assert swept_exc.value.detail == revoked_exc.value.detail == "unauthorized"


# ---- privacy-hardening migration (db._migrate_session_privacy) -----------
#
# These build the OLD pre-migration table shape by hand (SCHEMA itself
# no longer defines `user_agent`/`ip` at all -- see account_session's
# own comment in app/db.py) to prove existing rows, not just future
# ones, get cleaned: Matt's explicit requirement was that an IP address
# already sitting in the database does not get to linger just because
# it predates this change.

def _old_shape_db(tmp_path) -> str:
    """A standalone sqlite file with account_session in its PRE-
    migration shape (user_agent/ip both present), populated with rows
    exactly the way a real, already-deployed database would have them
    before this privacy-hardening pass ever ran. Deliberately does NOT
    reuse SCHEMA (it already reflects the post-migration shape) --
    this is the one place in this file that needs the OLD one.
    """
    path = str(tmp_path / "pre_migration.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE account_session ("
        "  token_hash TEXT PRIMARY KEY,"
        "  account_id INTEGER NOT NULL,"
        "  created_at INTEGER NOT NULL,"
        "  expires_at INTEGER NOT NULL,"
        "  last_seen_at INTEGER NOT NULL,"
        "  revoked_at INTEGER,"
        "  user_agent TEXT,"
        "  ip TEXT"
        ")"
    )
    now = int(time.time())
    rows = [
        # A real raw Chrome/Windows UA with an IP -- the ordinary case
        # this migration exists for.
        ("hash-a", 1, now, now + 1000, now, None,
         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
         "203.0.113.5"),
        # A UA this parser cannot recognise -- must reduce to "Unknown
        # device", never stay raw and never silently vanish.
        ("hash-b", 1, now, now + 1000, now, None, "curl/8.1.2", "198.51.100.20"),
        # A row with NULL user_agent -- e.g. a request that never sent
        # the header at all. device_label_from_user_agent(None) itself
        # already resolves to "Unknown device" (its own documented
        # contract: missing input degrades the same as unparseable
        # input), so the migration reduces this the same way it
        # reduces hash-b's unparseable curl UA, not to NULL.
        ("hash-c", 2, now, now + 1000, now, None, None, None),
    ]
    conn.executemany(
        "INSERT INTO account_session"
        "  (token_hash, account_id, created_at, expires_at, last_seen_at, revoked_at, user_agent, ip)"
        "  VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return path


def test_migrate_session_privacy_reduces_raw_uas_and_clears_ip(tmp_path):
    path = _old_shape_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    db._migrate_session_privacy(conn)
    conn.commit()

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(account_session)")}
    # ip is gone entirely -- physically dropped, not blanked (see
    # _migrate_session_privacy's own comment for why).
    assert "ip" not in cols
    assert "user_agent" not in cols
    assert "device_label" in cols

    by_hash = {
        row["token_hash"]: row["device_label"]
        for row in conn.execute("SELECT token_hash, device_label FROM account_session")
    }
    conn.close()
    assert by_hash["hash-a"] == "Chrome on Windows"
    assert by_hash["hash-b"] == "Unknown device"  # curl UA, unrecognisable -- degrades honestly
    assert by_hash["hash-c"] == "Unknown device"  # NULL UA -- same honest degradation, not left NULL


def test_migrate_session_privacy_is_a_no_op_on_an_already_migrated_db(db_path):
    """db_path's fixture already runs the current SCHEMA (no `ip`
    column at all) -- calling the migration again must do nothing and
    must not raise, since app/db.py's init_db() calls this
    unconditionally on every boot, migrated database or not.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    db._migrate_session_privacy(conn)  # must not raise

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(account_session)")}
    conn.close()
    assert "ip" not in cols
    assert "device_label" in cols
