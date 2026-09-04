"""Tests for POST /api/admin/player/delete (app/admin_api.py) -- the
operator counterpart to DELETE /api/account (app/account_api.py), now
brought onto that route's "delete the person, keep the team" model
instead of its own older hard-delete-everything shape.

There was no dedicated test file for this route before this change --
it predates the privacy-hardening pass this file exercises, and its
old behavior (hard-deleting the player row plus any square it last
painted) was never pinned down anywhere. This file is new, not an
edit of an existing suite.

Table lists (_PLAYER_SCOPED_TABLES, _ACCOUNT_SCOPED_TABLES) and the
tombstone-name helper (_tombstone_display_name) are defined once, in
app/account_api.py, and imported into app/admin_api.py rather than
duplicated -- see that route's own "account deletion" section comment
for the full per-table reasoning, and app/admin_api.py's
admin_player_delete() docstring for what is different on the operator
path. This file imports the same names directly from
app.account_api, exactly the way tests/test_account_delete.py already
reaches into that module's own private table lists to assert against.

Same fixture shapes tests/test_admin_account_release.py already uses:
a real file-backed sqlite database (app/db.py's connect()/WriteSession
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

import app.db as db
from app.account_api import (
    _ACCOUNT_SCOPED_TABLES,
    _PLAYER_SCOPED_TABLES,
    _tombstone_display_name,
)
from app.admin_api import router as admin_router
from app.auth import http_exception_as_error_body
from app.db import MIGRATIONS, SCHEMA
from app.sessions import SESSION_COOKIE_NAME, create_session

# Named explicitly (not just "every entry in _PLAYER_SCOPED_TABLES")
# because these seven are the ones the OLD route left dangling --
# pointing at a player_id that no longer resolved to anything once the
# player row itself was hard-deleted. They are the direct regression
# target for the "leaves dangling rows" bug this change fixes.
_FORMERLY_DANGLING_TABLES = (
    "player_last_fix",
    "player_cell_repeater_credit",
    "join_token",
    "checkin_node_name",
    "mc_checkin_binding",
    "mc_node_confirmation",
    "mt_node_confirmation",
)

# The shared-history tables the OLD route left alone for every player
# EXCEPT the one it deleted a square out from under -- mc_tile,
# mc_tile_capture_log, and mc_tile_unique_painter were hard-deleted
# for any square this player last painted. These must now survive
# completely untouched, the same as DELETE /api/account already
# leaves them.
_SHARED_HISTORY_TABLES_AND_ID_COL = (
    ("mc_tile", "last_player_id"),
    ("mc_tile_capture_log", "by_player_id"),
    ("mc_tile_unique_painter", "player_id"),
    ("mc_checkin_award", "player_id"),
    ("month_award", "player_id"),
    ("place_activation", "player_id"),
    ("player_team_change", "player_id"),
)


class FakeIngestor:
    def __init__(self) -> None:
        self.invalidated: list[int] = []

    def invalidate_player(self, player_id: int) -> None:
        self.invalidated.append(player_id)


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
    """Same shape tests/test_admin_account_release.py's own
    _login_as() uses: a fresh account holding `role`, an ACTIVE
    account_totp row (app/admin_api.py's _role_guard() requires active
    two-factor to USE a role, not merely hold one), and a signed-in
    session cookie on the TestClient.
    """
    account_id = _make_account(db_path, role=role)
    if role is not None:
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


def _add_totp(db_path: str, account_id: int) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at) "
        "VALUES (?, 'unused-target-secret', ?, ?)",
        (account_id, int(time.time()), int(time.time())),
    )
    conn.commit()
    conn.close()


def _full_player_scoped_data(db_path: str, player_id: int) -> None:
    """Writes one row into every table _PLAYER_SCOPED_TABLES deletes --
    same shape tests/test_account_delete.py's own
    _full_player_scoped_data() uses, kept in lockstep with that file's
    schema so a passing "everything is gone" assertion actually proves
    something for both routes.
    """
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO api_key(key_hash, player_id, issued_at) VALUES ('deadbeefcafef00d', ?, ?)",
        (player_id, now),
    )
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


def _shared_history_data(db_path: str, player_id: int, *, season_id=1, cell_id="cellA") -> None:
    """Writes one row into every table this route must leave alone,
    plus mc_tile_score/mc_tile_capture for the same square -- those two
    are not player-keyed at all (PRIMARY KEY is season_id/cell_id[/team]),
    but the OLD route deleted them as a side effect of deleting the
    whole mc_tile row for a square this player last painted, so they
    are included here to prove that side effect is gone too.
    """
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts) "
        "VALUES (?, ?, 'RED', ?, ?)",
        (season_id, cell_id, player_id, now),
    )
    conn.execute(
        "INSERT INTO mc_tile_score(season_id, cell_id, team, score, last_update) "
        "VALUES (?, ?, 'RED', 12.5, ?)",
        (season_id, cell_id, now),
    )
    conn.execute(
        "INSERT INTO mc_tile_capture(season_id, cell_id, captured_at, captured_by_team) "
        "VALUES (?, ?, ?, 'RED')",
        (season_id, cell_id, now),
    )
    conn.execute(
        "INSERT INTO mc_tile_capture_log(season_id, cell_id, ts, by_player_id, by_team) "
        "VALUES (?, ?, ?, ?, 'RED')",
        (season_id, cell_id, now, player_id),
    )
    conn.execute(
        "INSERT INTO mc_tile_unique_painter(season_id, cell_id, team, player_id, first_ts) "
        "VALUES (?, ?, 'RED', ?, ?)",
        (season_id, cell_id, player_id, now),
    )
    conn.execute(
        "INSERT INTO mc_checkin_award(season_id, player_id, net_date, points, protocol, message_id, awarded_at) "
        "VALUES (?, ?, '2026-08-19', 5.0, 'mc', 'msg1', ?)",
        (season_id, player_id, now),
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
# Confirmation guard / not-found -- unchanged from before this pass
# =========================================================================


def test_display_name_mismatch_is_409_and_deletes_nothing(client, db_path):
    _login_as(client, db_path, role="admin")
    player_id = _make_player(db_path, display_name="RealName")

    resp = client.post(
        "/api/admin/player/delete",
        json={"player_id": player_id, "display_name": "Wrong Name"},
    )

    assert resp.status_code == 409
    row = _row(db_path, "SELECT display_name, disabled_at FROM player WHERE player_id = ?", (player_id,))
    assert row["display_name"] == "RealName"
    assert row["disabled_at"] is None


def test_nonexistent_player_is_404(client, db_path):
    _login_as(client, db_path, role="admin")

    resp = client.post(
        "/api/admin/player/delete",
        json={"player_id": 999999, "display_name": "Nobody"},
    )

    assert resp.status_code == 404
    assert resp.json() == {"error": "player not found"}


def test_requires_a_signed_in_session_holding_a_role(client, db_path):
    _make_account(db_path, role="admin")  # keeps the admin surface enabled
    player_id = _make_player(db_path, display_name="Malice")

    resp = client.post(
        "/api/admin/player/delete",
        json={"player_id": player_id, "display_name": "Malice"},
    )

    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}
    row = _row(db_path, "SELECT disabled_at FROM player WHERE player_id = ?", (player_id,))
    assert row["disabled_at"] is None


# =========================================================================
# Tombstone model: player survives, player-scoped tables do not
# =========================================================================


def test_player_is_tombstoned_not_removed(client, db_path):
    actor_id = _login_as(client, db_path, role="admin")
    player_id = _make_player(db_path, display_name="RealName", team="BLUE")

    resp = client.post(
        "/api/admin/player/delete",
        json={"player_id": player_id, "display_name": "RealName"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deleted"] is True
    assert body["player_id"] == player_id
    assert body["account_id"] is None

    # The old route hard-deleted this row -- it must exist now.
    row = _row(
        db_path,
        "SELECT display_name, team, disabled_at, account_id FROM player WHERE player_id = ?",
        (player_id,),
    )
    assert row is not None
    assert row["display_name"] == _tombstone_display_name(player_id)
    assert row["display_name"] != "RealName"
    assert "RealName" not in row["display_name"]
    assert row["team"] == "BLUE"  # not identifying, left alone -- same as self-service
    assert row["disabled_at"] is not None
    assert row["account_id"] is None

    log_row = _row(
        db_path, "SELECT actor_account_id, action, detail FROM admin_action_log"
    )
    assert log_row["actor_account_id"] == actor_id
    assert log_row["action"] == "player_delete"
    assert f"player_id={player_id}" in log_row["detail"]
    assert "RealName" in log_row["detail"]

    assert player_id in client.app.state.mc_ingestor.invalidated


def test_full_delete_clears_every_player_scoped_table(client, db_path):
    _login_as(client, db_path, role="admin")
    player_id = _make_player(db_path, display_name="RealName")
    _full_player_scoped_data(db_path, player_id)

    resp = client.post(
        "/api/admin/player/delete",
        json={"player_id": player_id, "display_name": "RealName"},
    )
    assert resp.status_code == 200, resp.text

    for table in _PLAYER_SCOPED_TABLES:
        assert _count(db_path, table, "player_id", player_id) == 0, table
    body = resp.json()
    for table in _PLAYER_SCOPED_TABLES:
        assert body["counts"].get(table) == 1, table


def test_formerly_dangling_tables_are_now_cleaned(client, db_path):
    """The straightforward bug fix, isolated from the design question
    above: these seven tables were NOT touched by the old route at all,
    left pointing at a player_id that no longer resolved to anything
    once `player` was hard-deleted out from under them. All seven are
    a subset of _PLAYER_SCOPED_TABLES, so this is really the same
    guarantee as test_full_delete_clears_every_player_scoped_table
    above, named explicitly because it is the exact regression this
    change was built to close.
    """
    _login_as(client, db_path, role="admin")
    player_id = _make_player(db_path, display_name="RealName")
    _full_player_scoped_data(db_path, player_id)

    for table in _FORMERLY_DANGLING_TABLES:
        assert _count(db_path, table, "player_id", player_id) == 1, f"setup: {table}"

    resp = client.post(
        "/api/admin/player/delete",
        json={"player_id": player_id, "display_name": "RealName"},
    )
    assert resp.status_code == 200, resp.text

    for table in _FORMERLY_DANGLING_TABLES:
        assert _count(db_path, table, "player_id", player_id) == 0, table


# =========================================================================
# Shared history is no longer rewritten
# =========================================================================


def test_shared_history_survives_an_operator_deletion(client, db_path):
    _login_as(client, db_path, role="admin")
    player_id = _make_player(db_path, display_name="RealName")
    _shared_history_data(db_path, player_id)

    resp = client.post(
        "/api/admin/player/delete",
        json={"player_id": player_id, "display_name": "RealName"},
    )
    assert resp.status_code == 200, resp.text

    for table, id_col in _SHARED_HISTORY_TABLES_AND_ID_COL:
        row = _row(db_path, f"SELECT * FROM {table} WHERE {id_col} = ?", (player_id,))
        assert row is not None, f"{table} row did not survive"

    resolved = _row(
        db_path,
        "SELECT p.display_name FROM mc_tile_capture_log c "
        "JOIN player p ON p.player_id = c.by_player_id WHERE c.cell_id = 'cellA'",
    )
    assert resolved["display_name"] == _tombstone_display_name(player_id)


def test_square_the_player_last_painted_is_no_longer_deleted(client, db_path):
    """Direct regression test for the bug this whole route change
    exists to fix: the OLD route deleted mc_tile (plus mc_tile_score,
    mc_tile_capture, mc_tile_capture_log for that season/cell) for
    every square this player was the last painter of. None of that
    runs any more -- the square, its score, and its capture-window row
    all survive exactly as they were.
    """
    _login_as(client, db_path, role="admin")
    player_id = _make_player(db_path, display_name="RealName")
    _shared_history_data(db_path, player_id, season_id=1, cell_id="cellA")

    resp = client.post(
        "/api/admin/player/delete",
        json={"player_id": player_id, "display_name": "RealName"},
    )
    assert resp.status_code == 200, resp.text

    assert _row(
        db_path, "SELECT 1 FROM mc_tile WHERE season_id = 1 AND cell_id = 'cellA'"
    ) is not None
    assert _row(
        db_path,
        "SELECT 1 FROM mc_tile_score WHERE season_id = 1 AND cell_id = 'cellA' AND team = 'RED'",
    ) is not None
    assert _row(
        db_path, "SELECT 1 FROM mc_tile_capture WHERE season_id = 1 AND cell_id = 'cellA'"
    ) is not None

    # counts in the response body no longer mention any of these --
    # they simply aren't touched at all any more.
    counts = resp.json()["counts"]
    for stale_key in ("mc_tile", "mc_tile_score", "mc_tile_capture", "mc_tile_capture_log", "mc_tile_unique_painter"):
        assert stale_key not in counts


# =========================================================================
# The target's linked account
# =========================================================================


def test_target_with_linked_account_has_that_account_deleted_not_merely_unlinked(client, db_path):
    actor_id = _login_as(client, db_path, role="admin")
    target_account_id = _make_account(db_path)
    _add_identity(db_path, target_account_id, provider="google", email="a@example.com")
    _add_totp(db_path, target_account_id)
    player_id = _make_player(db_path, account_id=target_account_id, display_name="RealName")

    resp = client.post(
        "/api/admin/player/delete",
        json={"player_id": player_id, "display_name": "RealName"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["account_id"] == target_account_id

    # The account row itself, and every account-scoped table: gone --
    # not merely unlinked the way unlink-account leaves it.
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (target_account_id,)) is None
    for table in _ACCOUNT_SCOPED_TABLES:
        assert _count(db_path, table, "account_id", target_account_id) == 0, table

    # player itself: tombstoned, not deleted, account_id cleared.
    row = _row(
        db_path, "SELECT display_name, account_id FROM player WHERE player_id = ?", (player_id,)
    )
    assert row["display_name"] == _tombstone_display_name(player_id)
    assert row["account_id"] is None

    # the audit log names the account too, not just the player.
    log_row = _row(db_path, "SELECT actor_account_id, detail FROM admin_action_log")
    assert log_row["actor_account_id"] == actor_id
    assert f"account_id={target_account_id}" in log_row["detail"]
    assert "also deleted" in log_row["detail"]


def test_target_with_no_linked_account_leaves_account_id_null_in_response(client, db_path):
    _login_as(client, db_path, role="admin")
    player_id = _make_player(db_path, account_id=None, display_name="Loner")

    resp = client.post(
        "/api/admin/player/delete",
        json={"player_id": player_id, "display_name": "Loner"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["account_id"] is None

    log_row = _row(db_path, "SELECT detail FROM admin_action_log")
    assert "account also deleted" not in log_row["detail"]


def test_deleting_a_player_never_touches_an_unrelated_accounts_data(client, db_path):
    """Sanity check on the account-scoped cascade's WHERE clause: a
    second, unrelated account (and its own player) must survive
    untouched -- this is a targeted delete, not a broad one.
    """
    _login_as(client, db_path, role="admin")
    target_account_id = _make_account(db_path)
    player_id = _make_player(db_path, account_id=target_account_id, display_name="RealName")

    other_account_id = _make_account(db_path)
    _add_identity(db_path, other_account_id, provider="google", email="other@example.com")
    other_player_id = _make_player(db_path, account_id=other_account_id, display_name="Bystander")

    resp = client.post(
        "/api/admin/player/delete",
        json={"player_id": player_id, "display_name": "RealName"},
    )
    assert resp.status_code == 200, resp.text

    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (other_account_id,)) is not None
    assert _count(db_path, "account_identity", "account_id", other_account_id) == 1
    other_row = _row(
        db_path, "SELECT display_name, account_id FROM player WHERE player_id = ?", (other_player_id,)
    )
    assert other_row["display_name"] == "Bystander"
    assert other_row["account_id"] == other_account_id


# =========================================================================
# An admin cannot use this route to reach a role-holder
#
# See admin_player_delete()'s own docstring, "an admin cannot use this
# route to reach a role-holder", for the full reasoning: this route now
# carries the same one-directional boundary /api/admin/roles/* already
# enforces (operator-rank required to touch anyone's account.role) --
# an admin refused, an operator allowed, and only when the target's
# linked account actually holds a role.
# =========================================================================


def test_admin_is_refused_when_target_account_holds_admin_role(client, db_path):
    _login_as(client, db_path, role="admin")
    target_account_id = _make_account(db_path, role="admin")
    player_id = _make_player(db_path, account_id=target_account_id, display_name="RealName")

    resp = client.post(
        "/api/admin/player/delete",
        json={"player_id": player_id, "display_name": "RealName"},
    )

    assert resp.status_code == 409
    assert resp.json() == {
        "error": "that account holds the admin role — an admin cannot delete "
        "an account that holds a role; an operator can still do this"
    }
    row = _row(
        db_path, "SELECT display_name, disabled_at FROM player WHERE player_id = ?", (player_id,)
    )
    assert row["display_name"] == "RealName"
    assert row["disabled_at"] is None
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (target_account_id,)) is not None


def test_admin_is_refused_when_target_account_holds_operator_role(client, db_path):
    _login_as(client, db_path, role="admin")
    target_account_id = _make_account(db_path, role="operator")
    player_id = _make_player(db_path, account_id=target_account_id, display_name="RealName")

    resp = client.post(
        "/api/admin/player/delete",
        json={"player_id": player_id, "display_name": "RealName"},
    )

    assert resp.status_code == 409
    assert resp.json() == {
        "error": "that account holds the operator role — an admin cannot delete "
        "an account that holds a role; an operator can still do this"
    }
    row = _row(db_path, "SELECT display_name FROM player WHERE player_id = ?", (player_id,))
    assert row["display_name"] == "RealName"
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (target_account_id,)) is not None


def test_operator_may_delete_a_target_whose_account_holds_admin_role(client, db_path):
    _login_as(client, db_path, role="operator")
    target_account_id = _make_account(db_path, role="admin")
    player_id = _make_player(db_path, account_id=target_account_id, display_name="RealName")

    resp = client.post(
        "/api/admin/player/delete",
        json={"player_id": player_id, "display_name": "RealName"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["account_id"] == target_account_id
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (target_account_id,)) is None


def test_operator_may_delete_a_target_whose_account_holds_operator_role(client, db_path):
    _login_as(client, db_path, role="operator")
    target_account_id = _make_account(db_path, role="operator")
    player_id = _make_player(db_path, account_id=target_account_id, display_name="RealName")

    resp = client.post(
        "/api/admin/player/delete",
        json={"player_id": player_id, "display_name": "RealName"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["account_id"] == target_account_id
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (target_account_id,)) is None


def test_admin_may_still_delete_an_ordinary_player_with_no_linked_account(client, db_path):
    _login_as(client, db_path, role="admin")
    player_id = _make_player(db_path, account_id=None, display_name="Loner")

    resp = client.post(
        "/api/admin/player/delete",
        json={"player_id": player_id, "display_name": "Loner"},
    )

    assert resp.status_code == 200, resp.text


def test_admin_may_still_delete_a_target_whose_account_holds_no_role(client, db_path):
    _login_as(client, db_path, role="admin")
    target_account_id = _make_account(db_path)  # role defaults to None
    player_id = _make_player(db_path, account_id=target_account_id, display_name="RealName")

    resp = client.post(
        "/api/admin/player/delete",
        json={"player_id": player_id, "display_name": "RealName"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["account_id"] == target_account_id
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (target_account_id,)) is None


def test_operator_may_delete_their_own_account_through_this_route(client, db_path):
    """Matt's call, documented in admin_player_delete()'s own docstring:
    the admin-vs-role-holder refusal above only fires when the CALLER's
    own rank is "admin" -- an operator's is not, so an operator naming
    their own player through this route is unaffected by it, the same
    as any other role-holding target an operator may act on. This
    matches DELETE /api/account already letting any signed-in account,
    role or no role, delete itself -- this route staying consistent
    with that rather than adding a second, contradictory door onto the
    same already-permitted act.
    """
    actor_id = _login_as(client, db_path, role="operator")
    player_id = _make_player(db_path, account_id=actor_id, display_name="SelfOp")

    resp = client.post(
        "/api/admin/player/delete",
        json={"player_id": player_id, "display_name": "SelfOp"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["account_id"] == actor_id
    assert _row(db_path, "SELECT 1 FROM account WHERE account_id = ?", (actor_id,)) is None
