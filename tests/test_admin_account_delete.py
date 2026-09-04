"""Tests for GET /api/admin/accounts and POST /api/admin/account/delete
(app/admin_api.py) -- the account-shaped door this admin surface never
had: every route before these lived under /api/admin/player/*, reached
by naming a player_id, which meant an account with no linked player
(released by POST /api/admin/player/unlink-account, or simply never
claimed by a finished join) was invisible to the console and impossible
to remove.

These two routes reuse the exact same deletion definition
POST /api/admin/player/delete and DELETE /api/account already share --
_ACCOUNT_SCOPED_TABLES, _PLAYER_SCOPED_TABLES, _tombstone_display_name()
(app/account_api.py) -- rather than a third copy of "what deletion
means". This file does not re-prove every entry in either table list;
tests/test_admin_player_delete.py and tests/test_account_delete.py
already do that exhaustively for the shared helpers. What this file
covers is specific to reaching deletion BY ACCOUNT: the listing surfaces
orphans, an orphan can be deleted with no player to tombstone, a linked
account's player is tombstoned exactly like player/delete leaves it,
the role guard carries across unchanged, and the whole write is atomic.

Same fixture shapes tests/test_admin_player_delete.py already uses: a
real file-backed sqlite database (app/db.py's connect()/WriteSession
open a fresh connection per call, so ":memory:" would not share data
with the route code under test) and a bare FastAPI app around just
app/admin_api.py's router.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.admin_api as admin_api_module
import app.db as db
from app.account_api import _ACCOUNT_SCOPED_TABLES, _PLAYER_SCOPED_TABLES, _tombstone_display_name
from app.admin_api import router as admin_router
from app.auth import http_exception_as_error_body
from app.db import MIGRATIONS, SCHEMA
from app.sessions import SESSION_COOKIE_NAME, create_session


class FakeIngestor:
    def __init__(self) -> None:
        self.invalidated: list[int] = []

    def invalidate_player(self, player_id: int) -> None:
        self.invalidated.append(player_id)


def _run(coro):
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


@pytest.fixture
def client(db_path):
    app = FastAPI()
    app.include_router(admin_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    app.state.mc_ingestor = FakeIngestor()
    return TestClient(app)


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
    """Same shape tests/test_admin_player_delete.py's own _login_as()
    uses: a fresh account holding `role`, an ACTIVE account_totp row
    (_role_guard() requires active two-factor to USE a role, not
    merely hold one), and a signed-in session cookie on the TestClient.
    """
    account_id = _make_account(db_path, role=role)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at) "
        "VALUES (?, 'unused', ?, ?)",
        (account_id, int(time.time()), int(time.time())),
    )
    conn.commit()
    conn.close()
    raw_token = _run(create_session(account_id, device_label=None))
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return account_id


def _make_player(
    path: str, *, account_id: int | None = None, display_name="Tester", team="RED",
) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO player(display_name, team, created_at, account_id) VALUES (?, ?, ?, ?)",
        (display_name, team, int(time.time()), account_id),
    )
    conn.commit()
    player_id = cur.lastrowid
    conn.close()
    return player_id


def _add_identity(db_path: str, account_id: int, *, provider="email", subject=None, email=None) -> None:
    subject = subject or email or f"{provider}-sub-{account_id}"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (provider, subject, account_id, email, int(time.time())),
    )
    conn.commit()
    conn.close()


def _issue_key(db_path: str, player_id: int, key_hash: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO api_key(key_hash, player_id, issued_at) VALUES (?, ?, ?)",
        (key_hash, player_id, int(time.time())),
    )
    conn.commit()
    conn.close()


def _shared_history_data(db_path: str, player_id: int, *, season_id=1, cell_id="cellA") -> None:
    """A couple of the shared-history rows this route must leave
    completely untouched -- the same tables tests/test_admin_player_delete.py's
    own test_shared_history_survives_an_operator_deletion already proves
    exhaustively for player/delete. This is a light sample, not a
    repeat of that coverage: enough to prove account/delete's own
    tombstone half runs through the same code path, not a full
    re-audit of every shared table.
    """
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts) "
        "VALUES (?, ?, 'RED', ?, ?)",
        (season_id, cell_id, player_id, now),
    )
    conn.execute(
        "INSERT INTO mc_checkin_award(season_id, player_id, net_date, points, protocol, message_id, awarded_at) "
        "VALUES (?, ?, '2026-08-19', 5.0, 'mc', 'msg1', ?)",
        (season_id, player_id, now),
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
# GET /api/admin/accounts -- orphans must be findable
# =========================================================================


def test_listing_includes_an_account_with_a_linked_player(client, db_path):
    _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, provider="google", email="a@example.com")
    player_id = _make_player(db_path, account_id=account_id, display_name="Linked")

    resp = client.get("/api/admin/accounts")
    assert resp.status_code == 200, resp.text
    rows = {r["account_id"]: r for r in resp.json()}
    row = rows[account_id]
    assert row["player"] == {"player_id": player_id, "display_name": "Linked"}
    assert row["sign_in_methods"] == 1
    assert row["role"] is None


def test_listing_includes_an_orphaned_account_with_player_none(client, db_path):
    """The entire point of this section: an account nobody can reach
    through GET /api/admin/players (which only ever lists players) must
    show up here, clearly marked as having no linked player.
    """
    _login_as(client, db_path, role="admin")
    orphan_id = _make_account(db_path)
    _add_identity(db_path, orphan_id, provider="github", email="orphan@example.com")

    resp = client.get("/api/admin/accounts")
    assert resp.status_code == 200, resp.text
    rows = {r["account_id"]: r for r in resp.json()}
    row = rows[orphan_id]
    assert row["player"] is None
    assert row["sign_in_methods"] == 1


def test_listing_never_exposes_email_or_secrets(client, db_path):
    _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, provider="email", email="secret@example.com")

    resp = client.get("/api/admin/accounts")
    body = resp.text
    assert "secret@example.com" not in body


def test_listing_requires_a_signed_in_session_holding_a_role(client, db_path):
    _make_account(db_path, role="admin")  # keeps the admin surface enabled
    resp = client.get("/api/admin/accounts")
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


# =========================================================================
# Confirmation guard / not-found
# =========================================================================


def test_nonexistent_account_is_404(client, db_path):
    _login_as(client, db_path, role="admin")
    resp = client.post(
        "/api/admin/account/delete",
        json={"account_id": 999999, "display_name": "whatever"},
    )
    assert resp.status_code == 404
    assert resp.json() == {"error": "account not found"}


def test_wrong_display_name_on_linked_account_deletes_nothing(client, db_path):
    account_id = _make_account(db_path)
    _login_as(client, db_path, role="admin")
    player_id = _make_player(db_path, account_id=account_id, display_name="RealName")

    resp = client.post(
        "/api/admin/account/delete",
        json={"account_id": account_id, "display_name": "Wrong Name"},
    )
    assert resp.status_code == 409
    assert resp.json() == {"error": "display name does not match"}
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (account_id,)) is not None
    row = _row(db_path, "SELECT display_name, disabled_at FROM player WHERE player_id = ?", (player_id,))
    assert row["display_name"] == "RealName"
    assert row["disabled_at"] is None


def test_wrong_confirm_phrase_on_orphan_deletes_nothing(client, db_path):
    _login_as(client, db_path, role="admin")
    orphan_id = _make_account(db_path)
    _add_identity(db_path, orphan_id, provider="google", email="orphan@example.com")

    resp = client.post(
        "/api/admin/account/delete",
        json={"account_id": orphan_id, "display_name": "DELETE MY ACCOUNT"},
    )
    assert resp.status_code == 409
    assert "DELETE ACCOUNT" in resp.json()["error"]
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (orphan_id,)) is not None
    assert _count(db_path, "account_identity", "account_id", orphan_id) == 1


def test_orphan_confirm_phrase_is_bound_to_that_accounts_id(client, db_path):
    """Two orphans in the same list: the phrase that deletes one must
    not delete the other -- this is the whole reason
    _admin_account_no_player_confirm() folds account_id into the
    required text rather than reusing a single fixed literal (see that
    function's own docstring).
    """
    _login_as(client, db_path, role="admin")
    orphan_a = _make_account(db_path)
    _add_identity(db_path, orphan_a, provider="google", email="a@example.com")
    orphan_b = _make_account(db_path)
    _add_identity(db_path, orphan_b, provider="google", email="b@example.com")

    resp = client.post(
        "/api/admin/account/delete",
        json={"account_id": orphan_b, "display_name": f"DELETE ACCOUNT {orphan_a}"},
    )
    assert resp.status_code == 409
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (orphan_b,)) is not None


# =========================================================================
# Deleting an orphan
# =========================================================================


def test_deleting_an_orphan_removes_the_account_and_its_doors(client, db_path):
    _login_as(client, db_path, role="admin")
    orphan_id = _make_account(db_path)
    _add_identity(db_path, orphan_id, provider="google", email="orphan@example.com")

    resp = client.post(
        "/api/admin/account/delete",
        json={"account_id": orphan_id, "display_name": f"DELETE ACCOUNT {orphan_id}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] is True
    assert body["account_id"] == orphan_id
    assert body["player_id"] is None
    assert body["display_name"] is None

    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (orphan_id,)) is None
    for table in _ACCOUNT_SCOPED_TABLES:
        assert _count(db_path, table, "account_id", orphan_id) == 0, table


# =========================================================================
# Deleting a linked account tombstones its player, leaves history alone
# =========================================================================


def test_deleting_a_linked_account_tombstones_the_player(client, db_path):
    actor_id = _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, provider="google", email="real@example.com")
    player_id = _make_player(db_path, account_id=account_id, display_name="RealName", team="BLUE")
    _issue_key(db_path, player_id, "deadbeefcafef00d")
    _shared_history_data(db_path, player_id)

    resp = client.post(
        "/api/admin/account/delete",
        json={"account_id": account_id, "display_name": "RealName"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["player_id"] == player_id
    assert body["display_name"] == "RealName"

    # account row and every account-scoped table: gone.
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (account_id,)) is None
    for table in _ACCOUNT_SCOPED_TABLES:
        assert _count(db_path, table, "account_id", account_id) == 0, table

    # player-scoped table (api_key) cleared.
    assert _count(db_path, "api_key", "player_id", player_id) == 0

    # player itself: tombstoned, not deleted, unlinked, disabled.
    row = _row(
        db_path,
        "SELECT display_name, team, disabled_at, account_id FROM player WHERE player_id = ?",
        (player_id,),
    )
    assert row["display_name"] == _tombstone_display_name(player_id)
    assert row["team"] == "BLUE"
    assert row["disabled_at"] is not None
    assert row["account_id"] is None

    # shared history survives untouched and still names this player_id --
    # it will resolve through the tombstoned name at read time.
    tile = _row(db_path, "SELECT last_player_id FROM mc_tile WHERE cell_id = 'cellA'")
    assert tile["last_player_id"] == player_id
    award = _row(db_path, "SELECT player_id FROM mc_checkin_award WHERE season_id = 1")
    assert award["player_id"] == player_id

    # the deleted player's live-key cache entry was invalidated.
    ingestor = client.app.state.mc_ingestor
    assert player_id in ingestor.invalidated

    # audit log names both the account and the tombstoned player.
    log_row = _row(db_path, "SELECT actor_account_id, action, detail FROM admin_action_log")
    assert log_row["actor_account_id"] == actor_id
    assert log_row["action"] == "account_delete"
    assert f"account_id={account_id}" in log_row["detail"]
    assert f"player_id={player_id}" in log_row["detail"]


# =========================================================================
# Role guard, carried across from POST /api/admin/player/delete
# =========================================================================


def test_admin_is_refused_deleting_an_account_that_holds_a_role(client, db_path):
    _login_as(client, db_path, role="admin")
    target_id = _make_account(db_path, role="operator")
    player_id = _make_player(db_path, account_id=target_id, display_name="RealName")

    resp = client.post(
        "/api/admin/account/delete",
        json={"account_id": target_id, "display_name": "RealName"},
    )
    assert resp.status_code == 409
    assert resp.json() == {
        "error": "that account holds the operator role — an admin cannot delete "
        "an account that holds a role; an operator can still do this"
    }
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (target_id,)) is not None
    row = _row(db_path, "SELECT disabled_at FROM player WHERE player_id = ?", (player_id,))
    assert row["disabled_at"] is None


def test_admin_is_refused_deleting_an_orphaned_role_holding_account(client, db_path):
    """The guard must fire for an orphan too -- there is no player row
    to hide behind, and this is exactly the kind of account the guard
    exists to keep an admin from reaching (an operator's own account,
    say, mid-onboarding before it claimed a player).
    """
    _login_as(client, db_path, role="admin")
    target_id = _make_account(db_path, role="admin")

    resp = client.post(
        "/api/admin/account/delete",
        json={"account_id": target_id, "display_name": f"DELETE ACCOUNT {target_id}"},
    )
    assert resp.status_code == 409
    assert "admin cannot delete" in resp.json()["error"]
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (target_id,)) is not None


def test_operator_may_delete_a_role_holding_account(client, db_path):
    _login_as(client, db_path, role="operator")
    target_id = _make_account(db_path, role="admin")
    player_id = _make_player(db_path, account_id=target_id, display_name="RealName")

    resp = client.post(
        "/api/admin/account/delete",
        json={"account_id": target_id, "display_name": "RealName"},
    )
    assert resp.status_code == 200, resp.text
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (target_id,)) is None


def test_admin_may_still_delete_an_account_that_holds_no_role(client, db_path):
    _login_as(client, db_path, role="admin")
    target_id = _make_account(db_path)  # role defaults to None
    player_id = _make_player(db_path, account_id=target_id, display_name="RealName")

    resp = client.post(
        "/api/admin/account/delete",
        json={"account_id": target_id, "display_name": "RealName"},
    )
    assert resp.status_code == 200, resp.text
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (target_id,)) is None


# =========================================================================
# Atomicity
# =========================================================================


def test_forced_failure_partway_through_rolls_back_everything(client, db_path, monkeypatch):
    """Same technique tests/test_account_delete.py's own
    test_forced_failure_partway_through_rolls_back_everything() uses:
    force a real exception partway through the player-scoped delete
    loop (a bogus table name injected after two real deletes have
    already run inside the same transaction) and confirm NOTHING
    committed.
    """
    actor_id = _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, provider="google", email="real@example.com")
    player_id = _make_player(db_path, account_id=account_id, display_name="RealName")
    _issue_key(db_path, player_id, "raw-key-1")

    bad_tables = ("api_key", "player_node", "no_such_table_xyz")
    monkeypatch.setattr(admin_api_module, "_PLAYER_SCOPED_TABLES", bad_tables)

    with pytest.raises(sqlite3.OperationalError):
        client.post(
            "/api/admin/account/delete",
            json={"account_id": account_id, "display_name": "RealName"},
        )

    # Nothing committed: account row survives, its identity row
    # survives, the player row survives UNTOMBSTONED, and the api_key
    # row deleted earlier in the same (rolled-back) transaction is
    # back, and no audit row was written.
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (account_id,)) is not None
    assert _count(db_path, "account_identity", "account_id", account_id) == 1
    row = _row(
        db_path,
        "SELECT display_name, disabled_at, account_id FROM player WHERE player_id = ?",
        (player_id,),
    )
    assert row["display_name"] == "RealName"
    assert row["disabled_at"] is None
    assert row["account_id"] == account_id
    assert _count(db_path, "api_key", "player_id", player_id) == 1
    assert _row(db_path, "SELECT 1 FROM admin_action_log") is None
