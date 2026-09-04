"""Tests for DELETE /api/account (app/account_api.py) -- self-service,
irreversible account deletion. "Delete the person, keep the team":
every account-scoped and player-scoped table listed in that route's own
"account deletion" section comment is hard-deleted; `player` itself is
tombstoned (display_name overwritten, disabled_at set, account_id
cleared) rather than deleted; and the shared-history tables
(mc_tile_capture_log, mc_tile, mc_tile_unique_painter, mc_checkin_award,
month_award, place_activation, player_team_change) survive untouched
and keep resolving through the tombstoned player row.

Same fixture shapes tests/test_account_security.py and
tests/test_totp.py already use, for the same reasons: a real
file-backed sqlite database (app/db.py's connect()/WriteSession open a
fresh connection per call, so ":memory:" would not share data with the
route code under test), and a bare FastAPI app around just
app/account_api.py's router.
"""
from __future__ import annotations

import sqlite3
import time

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.account_api as account_api_module
import app.db as db
from app.account_api import router as account_router
from app.auth import http_exception_as_error_body
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.mc_ingest import hash_secret
from app.password_login import hash_password
from app.sessions import SESSION_COOKIE_NAME, create_session
from app.totp import encrypt_secret, generate_secret, generate_recovery_codes, totp_code_at


def _run(coro):
    import asyncio
    return asyncio.run(coro)


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


class FakeIngestor:
    def __init__(self) -> None:
        self.invalidated: list[int] = []

    def invalidate_player(self, player_id: int) -> None:
        self.invalidated.append(player_id)


@pytest.fixture
def client(db_path):
    app = FastAPI()
    app.include_router(account_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    app.state.mc_ingestor = FakeIngestor()
    return TestClient(app)


@pytest.fixture(autouse=True)
def _cheap_scrypt(monkeypatch):
    monkeypatch.setattr(settings, "account_password_scrypt_n", 2 ** 12)


# ---- setup helpers ---------------------------------------------------------

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
    if account_id is None:
        account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, device_label="Firefox on Windows"))
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return account_id, raw_token


def _add_identity(db_path: str, account_id: int, *, provider="email", subject=None,
                   email=None, email_verified=1) -> None:
    subject = subject or email or f"{provider}-sub-{account_id}"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (provider, subject, account_id, email, email_verified, int(time.time())),
    )
    conn.commit()
    conn.close()


def _set_password(db_path: str, account_id: int, raw_password: str) -> None:
    hashed = hash_password(raw_password, n=2 ** 12, r=8, p=1, dklen=32)
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO account_password(account_id, salt, n, r, p, dklen, hash, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (account_id, hashed.salt, hashed.n, hashed.r, hashed.p, hashed.dklen, hashed.derived_key, now, now),
    )
    conn.commit()
    conn.close()


def _enroll_totp(db_path: str, account_id: int) -> bytes:
    """Inserts an ACTIVATED account_totp row directly (no need to drive
    the real enroll/activate HTTP flow -- this file only needs an
    already-active secret to check DELETE /api/account's own gate
    against).
    """
    secret = generate_secret()
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at) "
        "VALUES (?, ?, ?, ?)",
        (account_id, encrypt_secret(secret), now, now),
    )
    conn.commit()
    conn.close()
    return secret


def _add_recovery_codes(db_path: str, account_id: int, codes: list[str]) -> None:
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    for code in codes:
        conn.execute(
            "INSERT INTO account_totp_recovery_code(account_id, code_hash, created_at) VALUES (?, ?, ?)",
            (account_id, hash_secret(code), now),
        )
    conn.commit()
    conn.close()


def _issue_key(db_path: str, player_id: int, raw_key: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO api_key(key_hash, player_id, issued_at) VALUES (?, ?, ?)",
        (hash_secret(raw_key), player_id, int(time.time())),
    )
    conn.commit()
    conn.close()


def _full_player_scoped_data(db_path: str, player_id: int) -> None:
    """Writes one row into every table _PLAYER_SCOPED_TABLES deletes,
    plus a second account_session/identity for realism -- so a
    passing "everything is gone" assertion actually proves something.
    """
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) VALUES ('mc', 'deadbeef', ?, ?)",
        (player_id, now),
    )
    conn.execute(
        "INSERT INTO checkin_node_name(connector, node_ref, player_id, name, first_seen) "
        "VALUES ('conn1', 'deadbeef', ?, 'Old Name', ?)",
        (player_id, now),
    )
    conn.execute(
        "INSERT INTO mc_checkin_binding(sender_name, player_id, bound_at) VALUES ('Sender1', ?, ?)",
        (player_id, now),
    )
    conn.execute(
        "INSERT INTO mc_node_confirmation(player_id, typed_name, opened_at, expires_at, baseline) "
        "VALUES (?, 'Tester', ?, ?, '{}')",
        (player_id, now, now + 300),
    )
    conn.execute(
        "INSERT INTO mt_node_confirmation(player_id, code, opened_at, expires_at) VALUES (?, 'ABC123', ?, ?)",
        (player_id, now, now + 300),
    )
    conn.execute(
        "INSERT INTO player_last_fix(player_id, protocol, cell_id, ts) VALUES (?, 'mc', 'cell1', ?)",
        (player_id, now),
    )
    conn.execute(
        "INSERT INTO player_cell_ping(player_id, protocol, cell_id, ts, seen_at) VALUES (?, 'mc', 'cell1', ?, ?)",
        (player_id, now, now),
    )
    conn.execute(
        "INSERT INTO player_cell_repeater_credit(player_id, protocol, cell_id, repeater_id, ts, seen_at) "
        "VALUES (?, 'mc', 'cell1', 'rep1', ?, ?)",
        (player_id, now, now),
    )
    conn.execute(
        "INSERT INTO player_ingest_stat(player_id, protocol, day) VALUES (?, 'mc', 20260101)",
        (player_id,),
    )
    conn.execute(
        "INSERT INTO join_token(token_hash, player_id, team, created_at, expires_at) "
        "VALUES ('tokhash1', ?, 'RED', ?, ?)",
        (player_id, now, now + 900),
    )
    conn.commit()
    conn.close()


def _shared_history_data(db_path: str, player_id: int) -> None:
    """Writes one row into every table this route must leave alone
    (mc_tile/mc_tile_capture_log/mc_tile_unique_painter/mc_checkin_award/
    month_award/place_activation/player_team_change)."""
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts) "
        "VALUES (1, 'cellA', 'RED', ?, ?)",
        (player_id, now),
    )
    conn.execute(
        "INSERT INTO mc_tile_capture_log(season_id, cell_id, ts, by_player_id, by_team) "
        "VALUES (1, 'cellA', ?, ?, 'RED')",
        (now, player_id),
    )
    conn.execute(
        "INSERT INTO mc_tile_unique_painter(season_id, cell_id, team, player_id, first_ts) "
        "VALUES (1, 'cellA', 'RED', ?, ?)",
        (player_id, now),
    )
    conn.execute(
        "INSERT INTO mc_checkin_award(season_id, player_id, net_date, points, protocol, message_id, awarded_at) "
        "VALUES (1, ?, '2026-08-19', 5.0, 'mc', 'msg1', ?)",
        (player_id, now),
    )
    conn.execute(
        "INSERT INTO month_award(month, protocol, award, scope, player_id, team, value) "
        "VALUES ('2026-08', 'mc', 'top_scorer', '', ?, 'RED', 100.0)",
        (player_id,),
    )
    conn.execute(
        "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at, protocol) "
        "VALUES (1, ?, '2026-08-19', 10, ?, 'mc')",
        (player_id, now),
    )
    conn.execute(
        "INSERT INTO player_team_change(player_id, from_team, to_team, changed_at, actor) "
        "VALUES (?, 'BLUE', 'RED', ?, 'player')",
        (player_id, now),
    )
    conn.commit()
    conn.close()


def _row(db_path: str, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return row


def _count(db_path: str, table: str, col: str, value) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (value,)).fetchone()[0]
    conn.close()
    return n


# =========================================================================
# Confirmation guard
# =========================================================================

def test_wrong_display_name_deletes_nothing(client, db_path):
    account_id, _ = _login(client, db_path)
    player_id = _make_player(db_path, account_id=account_id, display_name="RealName")

    resp = client.request("DELETE", "/api/account", json={"display_name": "WrongName"})
    assert resp.status_code == 409
    assert "does not match" in resp.json()["error"]

    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (account_id,)) is not None
    row = _row(db_path, "SELECT display_name, disabled_at FROM player WHERE player_id = ?", (player_id,))
    assert row["display_name"] == "RealName"
    assert row["disabled_at"] is None


def test_display_name_match_is_case_sensitive_exact_no_trim(client, db_path):
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id, display_name="RealName")

    # Wrong case and untrimmed whitespace both count as a mismatch --
    # same exact-match behavior POST /api/admin/player/delete's own
    # guard has (app/admin_api.py: `row["display_name"] != display_name`).
    for bad in ("realname", "RealName ", " RealName"):
        resp = client.request("DELETE", "/api/account", json={"display_name": bad})
        assert resp.status_code == 409
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (account_id,)) is not None


def test_no_player_wrong_confirm_phrase_deletes_nothing(client, db_path):
    account_id, _ = _login(client, db_path)

    resp = client.request("DELETE", "/api/account", json={"display_name": "delete my account"})
    assert resp.status_code == 409
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (account_id,)) is not None


def test_no_player_correct_confirm_phrase_deletes_the_account(client, db_path):
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="google", email="a@example.com")

    resp = client.request("DELETE", "/api/account", json={"display_name": "DELETE MY ACCOUNT"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (account_id,)) is None
    assert _count(db_path, "account_identity", "account_id", account_id) == 0


# =========================================================================
# Full deletion + tombstone shape (account with a linked player)
# =========================================================================

def test_full_delete_clears_every_account_and_player_scoped_table_and_tombstones_player(
    client, db_path
):
    account_id, _ = _login(client, db_path)
    player_id = _make_player(db_path, account_id=account_id, display_name="RealName", team="BLUE")
    _add_identity(db_path, account_id, provider="google", email="a@example.com")
    _issue_key(db_path, player_id, "raw-key-1")
    _full_player_scoped_data(db_path, player_id)
    _shared_history_data(db_path, player_id)

    resp = client.request("DELETE", "/api/account", json={"display_name": "RealName"})
    assert resp.status_code == 200, resp.text

    # account row and every account-scoped table: gone.
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (account_id,)) is None
    for table in account_api_module._ACCOUNT_SCOPED_TABLES:
        assert _count(db_path, table, "account_id", account_id) == 0, table

    # every player-scoped table: gone.
    for table in account_api_module._PLAYER_SCOPED_TABLES:
        assert _count(db_path, table, "player_id", player_id) == 0, table

    # player itself: survives, tombstoned.
    row = _row(
        db_path,
        "SELECT display_name, team, disabled_at, account_id FROM player WHERE player_id = ?",
        (player_id,),
    )
    assert row is not None
    assert row["display_name"] == f"Deleted player — account removed (#{player_id})"
    assert row["display_name"] != "RealName"
    assert "RealName" not in row["display_name"]
    assert row["team"] == "BLUE"  # not identifying, left alone
    assert row["disabled_at"] is not None
    assert row["account_id"] is None

    # the ingestor was told to drop its cached auth for this player.
    assert player_id in client.app.state.mc_ingestor.invalidated


def test_shared_history_survives_and_still_resolves_to_the_tombstoned_name(client, db_path):
    account_id, _ = _login(client, db_path)
    player_id = _make_player(db_path, account_id=account_id, display_name="RealName")
    _shared_history_data(db_path, player_id)

    resp = client.request("DELETE", "/api/account", json={"display_name": "RealName"})
    assert resp.status_code == 200

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    for table, id_col in [
        ("mc_tile", "last_player_id"),
        ("mc_tile_capture_log", "by_player_id"),
        ("mc_tile_unique_painter", "player_id"),
        ("mc_checkin_award", "player_id"),
        ("month_award", "player_id"),
        ("place_activation", "player_id"),
        ("player_team_change", "player_id"),
    ]:
        row = conn.execute(f"SELECT * FROM {table} WHERE {id_col} = ?", (player_id,)).fetchone()
        assert row is not None, f"{table} row did not survive"

    resolved = conn.execute(
        "SELECT p.display_name FROM mc_tile_capture_log c "
        "JOIN player p ON p.player_id = c.by_player_id WHERE c.cell_id = 'cellA'"
    ).fetchone()
    assert resolved["display_name"] == f"Deleted player — account removed (#{player_id})"
    conn.close()


# =========================================================================
# Re-authentication
# =========================================================================

def test_password_required_when_account_has_one_and_no_totp(client, db_path):
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id, display_name="RealName")
    _add_identity(db_path, account_id, email="a@example.com")
    _set_password(db_path, account_id, "correct-horse-battery")

    resp = client.request("DELETE", "/api/account", json={"display_name": "RealName"})
    assert resp.status_code == 400
    assert "password is required" in resp.json()["error"]
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (account_id,)) is not None


def test_wrong_password_is_refused_and_deletes_nothing(client, db_path):
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id, display_name="RealName")
    _set_password(db_path, account_id, "correct-horse-battery")

    resp = client.request(
        "DELETE", "/api/account",
        json={"display_name": "RealName", "password": "wrong-password"},
    )
    assert resp.status_code == 401
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (account_id,)) is not None


def test_correct_password_allows_deletion(client, db_path):
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id, display_name="RealName")
    _set_password(db_path, account_id, "correct-horse-battery")

    resp = client.request(
        "DELETE", "/api/account",
        json={"display_name": "RealName", "password": "correct-horse-battery"},
    )
    assert resp.status_code == 200
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (account_id,)) is None


def test_totp_code_required_when_totp_is_active_even_with_a_password_set(client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "account_totp_encryption_key", Fernet.generate_key().decode())
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id, display_name="RealName")
    _set_password(db_path, account_id, "correct-horse-battery")
    _enroll_totp(db_path, account_id)

    # A correct PASSWORD alone must not be enough once TOTP is active --
    # TOTP is the stronger, preferred factor (see the route's own
    # docstring), so this must still be refused.
    resp = client.request(
        "DELETE", "/api/account",
        json={"display_name": "RealName", "password": "correct-horse-battery"},
    )
    assert resp.status_code == 401
    assert "two-factor" in resp.json()["error"]
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (account_id,)) is not None


def test_wrong_totp_code_is_refused_and_deletes_nothing(client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "account_totp_encryption_key", Fernet.generate_key().decode())
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id, display_name="RealName")
    _enroll_totp(db_path, account_id)

    resp = client.request(
        "DELETE", "/api/account",
        json={"display_name": "RealName", "totp_code": "000000"},
    )
    assert resp.status_code == 401
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (account_id,)) is not None


def test_correct_totp_code_allows_deletion(client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "account_totp_encryption_key", Fernet.generate_key().decode())
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id, display_name="RealName")
    secret = _enroll_totp(db_path, account_id)
    now = int(time.time())
    code = totp_code_at(secret, when=now)

    resp = client.request(
        "DELETE", "/api/account",
        json={"display_name": "RealName", "totp_code": code},
    )
    assert resp.status_code == 200
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (account_id,)) is None


def test_totp_recovery_code_allows_deletion(client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "account_totp_encryption_key", Fernet.generate_key().decode())
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id, display_name="RealName")
    _enroll_totp(db_path, account_id)
    _add_recovery_codes(db_path, account_id, ["ABCD1234", "EFGH5678"])

    resp = client.request(
        "DELETE", "/api/account",
        json={"display_name": "RealName", "totp_recovery_code": "abcd1234"},
    )
    assert resp.status_code == 200
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (account_id,)) is None


def test_no_password_no_totp_requires_nothing_beyond_confirmation(client, db_path):
    """An OAuth-only account (e.g. Google sign-in, never set a
    password, never enrolled TOTP) has no third credential to supply --
    the route must not invent one it cannot satisfy.
    """
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id, display_name="RealName")
    _add_identity(db_path, account_id, provider="google", email="a@example.com")

    resp = client.request("DELETE", "/api/account", json={"display_name": "RealName"})
    assert resp.status_code == 200
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (account_id,)) is None


# =========================================================================
# Session death
# =========================================================================

def test_session_cookie_is_dead_after_deletion(client, db_path):
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id, display_name="RealName")

    resp = client.request("DELETE", "/api/account", json={"display_name": "RealName"})
    assert resp.status_code == 200

    # The account_session row this cookie names is gone (hard-deleted
    # as part of _ACCOUNT_SCOPED_TABLES) -- a follow-up request with the
    # SAME cookie against any session-guarded route must now be
    # unauthenticated, not merely "logged out client-side."
    again = client.get("/api/account")
    assert again.status_code == 401


def test_response_clears_the_session_cookie(client, db_path):
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id, display_name="RealName")

    resp = client.request("DELETE", "/api/account", json={"display_name": "RealName"})
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie
    # An expired/cleared cookie header carries Max-Age=0 or an
    # already-past Expires -- either is clear_session_cookie()'s own
    # contract (app/sessions.py); this just checks a clearing header
    # was actually sent, not the exact bytes of that contract.
    assert "Max-Age=0" in set_cookie or "expires=" in set_cookie.lower()


# =========================================================================
# Atomicity
# =========================================================================

def test_forced_failure_partway_through_rolls_back_everything(client, db_path, monkeypatch):
    """Forces a real exception partway through the player-scoped delete
    loop (a bogus table name injected after two real deletes have
    already run inside the same transaction) and confirms NOTHING
    committed: the account row, the player row (untombstoned), and the
    api_key row from before the bogus table are all still exactly as
    they were.
    """
    account_id, _ = _login(client, db_path)
    player_id = _make_player(db_path, account_id=account_id, display_name="RealName")
    _issue_key(db_path, player_id, "raw-key-1")

    bad_tables = ("api_key", "player_node", "no_such_table_xyz")
    monkeypatch.setattr(account_api_module, "_PLAYER_SCOPED_TABLES", bad_tables)

    with pytest.raises(sqlite3.OperationalError):
        client.request("DELETE", "/api/account", json={"display_name": "RealName"})

    # Nothing committed: account row survives, player row survives
    # UNTOMBSTONED, and the api_key row deleted earlier in the same
    # (rolled-back) transaction is back.
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (account_id,)) is not None
    row = _row(
        db_path,
        "SELECT display_name, disabled_at, account_id FROM player WHERE player_id = ?",
        (player_id,),
    )
    assert row["display_name"] == "RealName"
    assert row["disabled_at"] is None
    assert row["account_id"] == account_id
    assert _count(db_path, "api_key", "player_id", player_id) == 1
