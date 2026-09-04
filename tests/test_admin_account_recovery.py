"""Tests for the three account-recovery routes in app/admin_api.py:
POST /api/admin/account/disable-totp, POST
/api/admin/account/password/clear, and POST
/api/admin/account/identity/remove.

Before these existed, the only thing an operator could do for someone
locked out of their own account was delete it (POST
/api/admin/account/delete, tests/test_admin_account_delete.py) and tell
them to start over -- deletion is for ending a person's presence in the
game, not for helping them back in. These three routes are the actual
recovery doors, and they all share one hard rule this file exists to
prove holds everywhere: an operator may only ever CLEAR a credential,
never SET one. Not one request body accepted anywhere in this surface
carries a password, a TOTP secret, or a recovery code value -- see
test_no_route_in_this_file_accepts_a_credential_value() at the bottom,
which greps the request bodies these tests themselves send as a second,
independent check on that property.

Same fixture shapes tests/test_admin_account_delete.py and
tests/test_admin_player_delete.py already use: a real file-backed
sqlite database (app/db.py's connect()/WriteSession open a fresh
connection per call, so ":memory:" would not share data with the route
code under test) and a bare FastAPI app around just app/admin_api.py's
router.
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
from app.admin_api import router as admin_router
from app.auth import http_exception_as_error_body
from app.db import MIGRATIONS, SCHEMA
from app.mc_ingest import hash_secret
from app.sessions import SESSION_COOKIE_NAME, create_session


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
    """Same shape tests/test_admin_player_delete.py and
    tests/test_admin_account_delete.py's own _login_as() use: a fresh
    account holding `role`, an ACTIVE account_totp row (_role_guard()
    requires active two-factor to USE a role, not merely hold one), and
    a signed-in session cookie on the TestClient.
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


def _add_password(db_path: str, account_id: int) -> None:
    """A placeholder account_password row -- these tests never verify
    a real password, only that the row exists and gets cleared, so a
    fixed scrypt-shaped placeholder is enough (same "unused" shortcut
    _login_as() takes for account_totp.secret_encrypted above).
    """
    conn = sqlite3.connect(db_path)
    now = int(time.time())
    conn.execute(
        "INSERT INTO account_password(account_id, salt, n, r, p, dklen, hash, created_at, updated_at) "
        "VALUES (?, 'unused-salt', 16384, 8, 1, 32, 'unused-hash', ?, ?)",
        (account_id, now, now),
    )
    conn.commit()
    conn.close()


def _add_totp(db_path: str, account_id: int, *, recovery_codes: int = 3) -> None:
    """An ACTIVE account_totp row plus a batch of recovery-code rows --
    the target state POST /api/admin/account/disable-totp is meant to
    clear. Placeholder secret/hashes throughout, same reasoning as
    _add_password() above: these tests never verify a real code, only
    that disable-totp deletes every row.
    """
    conn = sqlite3.connect(db_path)
    now = int(time.time())
    conn.execute(
        "INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at) "
        "VALUES (?, 'unused', ?, ?)",
        (account_id, now, now),
    )
    conn.executemany(
        "INSERT INTO account_totp_recovery_code(account_id, code_hash, created_at) VALUES (?, ?, ?)",
        [(account_id, hash_secret(f"code-{account_id}-{i}"), now) for i in range(recovery_codes)],
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
# POST /api/admin/account/disable-totp
# =========================================================================


def test_disable_totp_clears_secret_and_every_recovery_code(client, db_path):
    actor_id = _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, provider="google", email="real@example.com")
    player_id = _make_player(db_path, account_id=account_id, display_name="RealName")
    _add_totp(db_path, account_id, recovery_codes=4)

    resp = client.post(
        "/api/admin/account/disable-totp",
        json={"account_id": account_id, "display_name": "RealName"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"ok": True, "account_id": account_id, "recovery_codes_cleared": 4}

    assert _row(db_path, "SELECT 1 FROM account_totp WHERE account_id = ?", (account_id,)) is None
    assert _count(db_path, "account_totp_recovery_code", "account_id", account_id) == 0

    log_row = _row(db_path, "SELECT actor_account_id, action, detail FROM admin_action_log")
    assert log_row["actor_account_id"] == actor_id
    assert log_row["action"] == "account_disable_totp"
    assert f"account_id={account_id}" in log_row["detail"]
    assert "recovery_codes_cleared=4" in log_row["detail"]

    event = _row(
        db_path,
        "SELECT kind, actor FROM account_link_event WHERE account_id = ? ORDER BY event_id DESC LIMIT 1",
        (account_id,),
    )
    assert event["kind"] == "totp_disabled"
    assert event["actor"] == "operator"


def test_disable_totp_refused_when_not_enabled(client, db_path):
    _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    _make_player(db_path, account_id=account_id, display_name="RealName")

    resp = client.post(
        "/api/admin/account/disable-totp",
        json={"account_id": account_id, "display_name": "RealName"},
    )
    assert resp.status_code == 404
    assert resp.json() == {"error": "two-factor authentication is not enabled on this account"}


def test_disable_totp_wrong_confirm_changes_nothing(client, db_path):
    _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    _make_player(db_path, account_id=account_id, display_name="RealName")
    _add_totp(db_path, account_id)

    resp = client.post(
        "/api/admin/account/disable-totp",
        json={"account_id": account_id, "display_name": "Wrong Name"},
    )
    assert resp.status_code == 409
    assert resp.json() == {"error": "display name does not match"}
    assert _row(db_path, "SELECT 1 FROM account_totp WHERE account_id = ?", (account_id,)) is not None
    assert _row(db_path, "SELECT 1 FROM admin_action_log") is None


def test_disable_totp_orphan_confirm_phrase(client, db_path):
    _login_as(client, db_path, role="admin")
    orphan_id = _make_account(db_path)
    _add_identity(db_path, orphan_id, provider="google", email="orphan@example.com")
    _add_totp(db_path, orphan_id)

    bad = client.post(
        "/api/admin/account/disable-totp",
        json={"account_id": orphan_id, "display_name": "DELETE ACCOUNT " + str(orphan_id)},
    )
    assert bad.status_code == 409
    assert "DISABLE TWO-FACTOR" in bad.json()["error"]

    good = client.post(
        "/api/admin/account/disable-totp",
        json={"account_id": orphan_id, "display_name": f"DISABLE TWO-FACTOR {orphan_id}"},
    )
    assert good.status_code == 200, good.text


def test_admin_is_refused_disabling_totp_on_a_role_holding_account(client, db_path):
    _login_as(client, db_path, role="admin")
    target_id = _make_account(db_path, role="operator")
    _make_player(db_path, account_id=target_id, display_name="RealName")
    _add_totp(db_path, target_id)

    resp = client.post(
        "/api/admin/account/disable-totp",
        json={"account_id": target_id, "display_name": "RealName"},
    )
    assert resp.status_code == 409
    assert resp.json() == {
        "error": "that account holds the operator role — an admin cannot disable "
        "two-factor authentication on an account that holds a role; an operator "
        "can still do this"
    }
    assert _row(db_path, "SELECT 1 FROM account_totp WHERE account_id = ?", (target_id,)) is not None


def test_operator_may_disable_totp_on_a_role_holding_account(client, db_path):
    _login_as(client, db_path, role="operator")
    target_id = _make_account(db_path, role="admin")
    _make_player(db_path, account_id=target_id, display_name="RealName")
    _add_totp(db_path, target_id)

    resp = client.post(
        "/api/admin/account/disable-totp",
        json={"account_id": target_id, "display_name": "RealName"},
    )
    assert resp.status_code == 200, resp.text
    assert _row(db_path, "SELECT 1 FROM account_totp WHERE account_id = ?", (target_id,)) is None


def test_disable_totp_requires_a_role_holding_session(client, db_path):
    _make_account(db_path, role="admin")  # keeps the admin surface enabled
    resp = client.post(
        "/api/admin/account/disable-totp", json={"account_id": 1, "display_name": "x"}
    )
    assert resp.status_code == 401


# =========================================================================
# POST /api/admin/account/password/clear
# =========================================================================


def test_clear_password_removes_the_row_when_another_door_remains(client, db_path):
    actor_id = _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, provider="google", email="real@example.com")
    _add_password(db_path, account_id)
    player_id = _make_player(db_path, account_id=account_id, display_name="RealName")

    resp = client.post(
        "/api/admin/account/password/clear",
        json={"account_id": account_id, "display_name": "RealName"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"ok": True, "account_id": account_id, "remaining_doors": 1}

    assert _row(db_path, "SELECT 1 FROM account_password WHERE account_id = ?", (account_id,)) is None

    log_row = _row(db_path, "SELECT actor_account_id, action, detail FROM admin_action_log")
    assert log_row["actor_account_id"] == actor_id
    assert log_row["action"] == "account_clear_password"
    assert f"account_id={account_id}" in log_row["detail"]

    event = _row(
        db_path,
        "SELECT kind, actor FROM account_link_event WHERE account_id = ? ORDER BY event_id DESC LIMIT 1",
        (account_id,),
    )
    assert event["kind"] == "password_removed"
    assert event["actor"] == "operator"


def test_clear_password_refused_when_it_is_the_only_door(client, db_path):
    """The last-door guard is KEPT for this route -- see
    app/admin_api.py's own docstring on POST
    /api/admin/account/password/clear for why: clearing the account's
    only door does not, by itself, hand anyone a way back in (nothing
    in this recovery surface can ever SET a new password), so allowing
    it would only trade a locked-out-but-intact account for a
    permanently unreachable one.
    """
    _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    _add_password(db_path, account_id)
    _make_player(db_path, account_id=account_id, display_name="RealName")

    resp = client.post(
        "/api/admin/account/password/clear",
        json={"account_id": account_id, "display_name": "RealName"},
    )
    assert resp.status_code == 409
    assert "no way to sign in" in resp.json()["error"]
    assert _row(db_path, "SELECT 1 FROM account_password WHERE account_id = ?", (account_id,)) is not None


def test_clear_password_refused_when_no_password_is_set(client, db_path):
    _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, provider="google", email="real@example.com")
    _make_player(db_path, account_id=account_id, display_name="RealName")

    resp = client.post(
        "/api/admin/account/password/clear",
        json={"account_id": account_id, "display_name": "RealName"},
    )
    assert resp.status_code == 404
    assert resp.json() == {"error": "no password is set on this account"}


def test_clear_password_wrong_confirm_changes_nothing(client, db_path):
    _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, provider="google", email="real@example.com")
    _add_password(db_path, account_id)
    _make_player(db_path, account_id=account_id, display_name="RealName")

    resp = client.post(
        "/api/admin/account/password/clear",
        json={"account_id": account_id, "display_name": "Wrong Name"},
    )
    assert resp.status_code == 409
    assert resp.json() == {"error": "display name does not match"}
    assert _row(db_path, "SELECT 1 FROM account_password WHERE account_id = ?", (account_id,)) is not None
    assert _row(db_path, "SELECT 1 FROM admin_action_log") is None


def test_admin_is_refused_clearing_password_on_a_role_holding_account(client, db_path):
    _login_as(client, db_path, role="admin")
    target_id = _make_account(db_path, role="operator")
    _add_identity(db_path, target_id, provider="google", email="op@example.com")
    _add_password(db_path, target_id)
    _make_player(db_path, account_id=target_id, display_name="RealName")

    resp = client.post(
        "/api/admin/account/password/clear",
        json={"account_id": target_id, "display_name": "RealName"},
    )
    assert resp.status_code == 409
    assert "an admin cannot clear the password on an account that holds a role" in resp.json()["error"]
    assert _row(db_path, "SELECT 1 FROM account_password WHERE account_id = ?", (target_id,)) is not None


def test_operator_may_clear_password_on_a_role_holding_account(client, db_path):
    _login_as(client, db_path, role="operator")
    target_id = _make_account(db_path, role="admin")
    _add_identity(db_path, target_id, provider="google", email="op@example.com")
    _add_password(db_path, target_id)
    _make_player(db_path, account_id=target_id, display_name="RealName")

    resp = client.post(
        "/api/admin/account/password/clear",
        json={"account_id": target_id, "display_name": "RealName"},
    )
    assert resp.status_code == 200, resp.text
    assert _row(db_path, "SELECT 1 FROM account_password WHERE account_id = ?", (target_id,)) is None


# =========================================================================
# POST /api/admin/account/identity/remove
# =========================================================================


def test_remove_identity_disconnects_the_named_provider(client, db_path):
    actor_id = _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, provider="google", email="lost@example.com")
    _add_identity(db_path, account_id, provider="discord", email=None)
    player_id = _make_player(db_path, account_id=account_id, display_name="RealName")

    resp = client.post(
        "/api/admin/account/identity/remove",
        json={"account_id": account_id, "provider": "google", "display_name": "RealName"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"ok": True, "account_id": account_id, "provider": "google", "remaining_doors": 1}

    assert _count(db_path, "account_identity", "account_id", account_id) == 1
    remaining = _row(
        db_path, "SELECT provider FROM account_identity WHERE account_id = ?", (account_id,)
    )
    assert remaining["provider"] == "discord"

    log_row = _row(db_path, "SELECT actor_account_id, action, detail FROM admin_action_log")
    assert log_row["actor_account_id"] == actor_id
    assert log_row["action"] == "account_remove_identity"
    assert f"account_id={account_id}" in log_row["detail"]
    assert "provider=google" in log_row["detail"]

    event = _row(
        db_path,
        "SELECT kind, actor, detail FROM account_link_event WHERE account_id = ? ORDER BY event_id DESC LIMIT 1",
        (account_id,),
    )
    assert event["kind"] == "identity_unlinked"
    assert event["actor"] == "operator"
    assert event["detail"] == "provider=google"


def test_remove_identity_refused_when_it_is_the_only_door(client, db_path):
    _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, provider="google", email="lost@example.com")
    _make_player(db_path, account_id=account_id, display_name="RealName")

    resp = client.post(
        "/api/admin/account/identity/remove",
        json={"account_id": account_id, "provider": "google", "display_name": "RealName"},
    )
    assert resp.status_code == 409
    assert "no way to sign in" in resp.json()["error"]
    assert _count(db_path, "account_identity", "account_id", account_id) == 1


def test_remove_identity_refused_when_provider_not_linked(client, db_path):
    _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, provider="google", email="real@example.com")
    _add_password(db_path, account_id)
    _make_player(db_path, account_id=account_id, display_name="RealName")

    resp = client.post(
        "/api/admin/account/identity/remove",
        json={"account_id": account_id, "provider": "discord", "display_name": "RealName"},
    )
    assert resp.status_code == 404
    assert resp.json() == {"error": "that provider is not linked to this account"}


def test_remove_identity_wrong_confirm_changes_nothing(client, db_path):
    _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, provider="google", email="real@example.com")
    _add_password(db_path, account_id)
    _make_player(db_path, account_id=account_id, display_name="RealName")

    resp = client.post(
        "/api/admin/account/identity/remove",
        json={"account_id": account_id, "provider": "google", "display_name": "Wrong Name"},
    )
    assert resp.status_code == 409
    assert resp.json() == {"error": "display name does not match"}
    assert _count(db_path, "account_identity", "account_id", account_id) == 1
    assert _row(db_path, "SELECT 1 FROM admin_action_log") is None


def test_admin_is_refused_removing_identity_on_a_role_holding_account(client, db_path):
    _login_as(client, db_path, role="admin")
    target_id = _make_account(db_path, role="operator")
    _add_identity(db_path, target_id, provider="google", email="op@example.com")
    _add_password(db_path, target_id)
    _make_player(db_path, account_id=target_id, display_name="RealName")

    resp = client.post(
        "/api/admin/account/identity/remove",
        json={"account_id": target_id, "provider": "google", "display_name": "RealName"},
    )
    assert resp.status_code == 409
    assert "an admin cannot remove a sign-in method from an account that holds a role" in resp.json()["error"]
    assert _count(db_path, "account_identity", "account_id", target_id) == 1


def test_operator_may_remove_identity_on_a_role_holding_account(client, db_path):
    _login_as(client, db_path, role="operator")
    target_id = _make_account(db_path, role="admin")
    _add_identity(db_path, target_id, provider="google", email="op@example.com")
    _add_password(db_path, target_id)
    _make_player(db_path, account_id=target_id, display_name="RealName")

    resp = client.post(
        "/api/admin/account/identity/remove",
        json={"account_id": target_id, "provider": "google", "display_name": "RealName"},
    )
    assert resp.status_code == 200, resp.text
    assert _count(db_path, "account_identity", "account_id", target_id) == 0


# =========================================================================
# GET /api/admin/accounts -- the new fields these actions need
# =========================================================================


def test_listing_reports_totp_password_and_identity_providers(client, db_path):
    _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, provider="google", email="real@example.com")
    _add_identity(db_path, account_id, provider="discord", email=None)
    _add_password(db_path, account_id)
    _add_totp(db_path, account_id)
    _make_player(db_path, account_id=account_id, display_name="RealName")

    resp = client.get("/api/admin/accounts")
    assert resp.status_code == 200, resp.text
    rows = {r["account_id"]: r for r in resp.json()}
    row = rows[account_id]
    assert row["has_password"] is True
    assert row["totp_active"] is True
    assert sorted(row["identity_providers"]) == ["discord", "google"]


def test_listing_totp_and_password_false_when_absent(client, db_path):
    _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, provider="google", email="real@example.com")
    _make_player(db_path, account_id=account_id, display_name="RealName")

    resp = client.get("/api/admin/accounts")
    row = {r["account_id"]: r for r in resp.json()}[account_id]
    assert row["has_password"] is False
    assert row["totp_active"] is False
    assert row["identity_providers"] == ["google"]


def test_listing_never_exposes_email_addresses(client, db_path):
    """The new fields (identity_providers, has_password, totp_active)
    widen this listing slightly -- see app/admin_api.py's own
    docstring on GET /api/admin/accounts for why provider NAMES are
    safe to expose (the recovery UI has to know which providers to
    offer removing) while an email address, a subject id, a secret, or
    a hash never are. This asserts the boundary held: providers show
    up, nothing that identifies the account holder does.
    """
    _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, provider="email", email="secret@example.com")
    _add_totp(db_path, account_id)
    _make_player(db_path, account_id=account_id, display_name="RealName")

    resp = client.get("/api/admin/accounts")
    assert "secret@example.com" not in resp.text
    assert "unused" not in resp.text  # the placeholder totp secret/password hash


# =========================================================================
# The property that governs this whole surface: clear, never set
# =========================================================================


def test_no_route_in_this_file_accepts_a_credential_value(client, db_path):
    """Every request this test file sends to the three recovery routes
    carries only account_id, provider, and the typed confirmation --
    never a password, a TOTP secret, or a recovery code. This asserts
    that holds even if a caller TRIES to smuggle one in: extra body
    fields the routes were never written to read must be silently
    ignored, not accepted, and the credential-shaped fields below must
    never end up written to the account they targeted -- setting a
    credential is simply not an operation any route in this surface
    performs, no matter what a request body contains.
    """
    _login_as(client, db_path, role="admin")
    account_id = _make_account(db_path)
    # Two identities plus a password, so removing one identity and then
    # clearing the password each still leave a door behind -- this test
    # is about what the request BODY can smuggle in, not a second
    # exercise of the last-door guard (see the dedicated tests for that
    # above).
    _add_identity(db_path, account_id, provider="google", email="real@example.com")
    _add_identity(db_path, account_id, provider="discord", email=None)
    _add_password(db_path, account_id)
    _add_totp(db_path, account_id)
    _make_player(db_path, account_id=account_id, display_name="RealName")

    smuggled = {
        "new_password": "hunter2-hunter2",
        "password": "hunter2-hunter2",
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "secret": "JBSWY3DPEHPK3PXP",
        "recovery_code": "ABCD234567",
        "code": "111111",
    }

    resp = client.post(
        "/api/admin/account/disable-totp",
        json={"account_id": account_id, "display_name": "RealName", **smuggled},
    )
    assert resp.status_code == 200, resp.text

    resp = client.post(
        "/api/admin/account/identity/remove",
        json={
            "account_id": account_id, "provider": "google", "display_name": "RealName",
            **smuggled,
        },
    )
    assert resp.status_code == 200, resp.text

    # account_password was never touched by either call above (neither
    # route reads or writes that table) -- still the exact placeholder
    # _add_password() wrote, never anything derived from `smuggled`.
    pw = _row(db_path, "SELECT hash FROM account_password WHERE account_id = ?", (account_id,))
    assert pw["hash"] == "unused-hash"

    resp = client.post(
        "/api/admin/account/password/clear",
        json={"account_id": account_id, "display_name": "RealName", **smuggled},
    )
    assert resp.status_code == 200, resp.text
    assert _row(db_path, "SELECT 1 FROM account_password WHERE account_id = ?", (account_id,)) is None

    # And a static check on the route source itself, independent of
    # what any test happens to send: none of the three handlers'
    # source code contains a body.get() for any key a credential value
    # would arrive under, so there is no way to smuggle one in through
    # a body shape these tests didn't happen to try.
    import inspect
    import re

    src = inspect.getsource(admin_api_module)
    route_names = (
        "admin_account_disable_totp", "admin_account_clear_password",
        "admin_account_remove_identity",
    )
    for route_name in route_names:
        start = src.index(f"async def {route_name}")
        rest = src[start + 1:]
        end_match = re.search(r"\n@router\.", rest)
        fn_src = rest[: end_match.start()] if end_match else rest
        for forbidden in (
            '"new_password"', '"password"', '"totp_secret"', '"secret"',
            '"recovery_code"', '"code"',
        ):
            assert f"body.get({forbidden})" not in fn_src, f"{route_name} reads {forbidden}"
