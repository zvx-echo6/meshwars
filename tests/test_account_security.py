"""Tests for the account-security surface added on top of
app/account_api.py and app/oauth_api.py: player-facing API key
rotation (POST /api/account/rotate-key), account passwords
(app/password_login.py, POST/DELETE /api/account/password, POST
/auth/password/start), contact email (POST /api/account/contact-email,
GET /auth/contact-email/verify), and identity unlink with the
last-door guard (DELETE /api/account/identity/{provider}).

Same fixture shapes tests/test_account_api.py and
tests/test_oauth_api.py already use for the same reasons: a real
file-backed sqlite database (app/db.py's connect()/WriteSession open a
fresh connection per call, so ":memory:" would not share data across
TestClient's own thread boundary), and a bare FastAPI app built from
just the two routers this file actually exercises.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.account_api as account_api_module
import app.db as db
import app.oauth_api as oauth_api_module
from app.account_api import router as account_router
from app.auth import http_exception_as_error_body
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.mc_ingest import AuthResult, McIngestor, hash_secret
from app.oauth_api import router as oauth_router
from app.password_login import hash_password, verify_password
from app.sessions import SESSION_COOKIE_NAME, create_session

GOOD_KEY = "good-key"
KEY_PLAYER_ID = 42


class FakeIngestor:
    """Same shape tests/test_account_api.py's own FakeIngestor uses,
    plus recording invalidate_player() calls -- rotate-key's whole
    point is that it calls that, and this is how tests below confirm
    it actually did.
    """

    def __init__(self) -> None:
        self.invalidated_players: list[int] = []

    async def authenticate(self, raw_key: str) -> AuthResult:
        if raw_key == GOOD_KEY:
            return AuthResult("ok", KEY_PLAYER_ID)
        return AuthResult("not_found")

    def invalidate_player(self, player_id: int) -> None:
        self.invalidated_players.append(player_id)


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


@pytest.fixture(autouse=True)
def _reset_rate_limiters_and_backoff():
    """Every limiter this file exercises is a module-level singleton
    (see app/auth.py's module docstring) -- cleared before and after
    every test, same pattern tests/test_account_api.py's and
    tests/test_email_login.py's own autouse fixtures use.
    """
    limiters = [
        account_api_module._link_key_addr_limiter,
        account_api_module._rotate_key_addr_limiter,
        account_api_module._contact_email_account_limiter,
        oauth_api_module._password_start_ip_limiter,
        oauth_api_module._password_start_addr_limiter,
    ]
    for lim in limiters:
        lim._hits.clear()
    oauth_api_module._password_backoff._state.clear()
    yield
    for lim in limiters:
        lim._hits.clear()
    oauth_api_module._password_backoff._state.clear()


@pytest.fixture
def client(db_path):
    app = FastAPI()
    app.include_router(account_router)
    app.include_router(oauth_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    app.state.mc_ingestor = FakeIngestor()
    return TestClient(app)


def _make_account(path: str) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    conn.commit()
    account_id = cur.lastrowid
    conn.close()
    return account_id


def _make_player(path: str, *, account_id: int | None = None, player_id: int | None = None,
                  display_name="Tester", team="RED") -> int:
    conn = sqlite3.connect(path)
    if player_id is not None:
        conn.execute(
            "INSERT INTO player(player_id, display_name, team, created_at, account_id) VALUES (?, ?, ?, ?, ?)",
            (player_id, display_name, team, int(time.time()), account_id),
        )
        conn.commit()
        conn.close()
        return player_id
    cur = conn.execute(
        "INSERT INTO player(display_name, team, created_at, account_id) VALUES (?, ?, ?, ?)",
        (display_name, team, int(time.time()), account_id),
    )
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    return pid


def _login(client: TestClient, db_path: str, *, account_id: int | None = None) -> tuple[int, str]:
    if account_id is None:
        account_id = _make_account(db_path)
    raw_token = _run(create_session(account_id, device_label="Firefox on Windows"))
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return account_id, raw_token


def _add_identity(db_path: str, account_id: int, *, provider="email", subject=None,
                   email=None, email_verified=1, linked_at=None) -> None:
    subject = subject or email
    linked_at = linked_at or int(time.time())
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (provider, subject, account_id, email, email_verified, linked_at),
    )
    conn.commit()
    conn.close()


def _issue_key(db_path: str, player_id: int, raw_key: str, *, revoked: bool = False) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO api_key(key_hash, player_id, issued_at, revoked_at) VALUES (?, ?, ?, ?)",
        (hash_secret(raw_key), player_id, int(time.time()), int(time.time()) if revoked else None),
    )
    conn.commit()
    conn.close()


def _set_password(db_path: str, account_id: int, raw_password: str) -> None:
    hashed = hash_password(raw_password, n=2 ** 12, r=8, p=1, dklen=32)  # low cost, fast tests
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO account_password(account_id, salt, n, r, p, dklen, hash, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (account_id, hashed.salt, hashed.n, hashed.r, hashed.p, hashed.dklen, hashed.derived_key, now, now),
    )
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def _cheap_scrypt(monkeypatch):
    """Every route test in this file goes through real hashlib.scrypt
    -- fine at production cost parameters (n=2**14) for a handful of
    calls, but this file makes many. Lower the cost app-wide for the
    duration of these tests only; app/password_login.py's own unit
    tests below check the real default values are what config.py
    actually ships.
    """
    monkeypatch.setattr(settings, "account_password_scrypt_n", 2 ** 12)


# =========================================================================
# app/password_login.py -- hash/verify unit tests
# =========================================================================

def test_hash_password_verify_round_trip():
    hashed = hash_password("correct horse battery staple", n=2 ** 12, r=8, p=1, dklen=32)
    assert verify_password("correct horse battery staple", hashed) is True


def test_verify_password_rejects_wrong_password():
    hashed = hash_password("correct horse battery staple", n=2 ** 12, r=8, p=1, dklen=32)
    assert verify_password("wrong password entirely", hashed) is False


def test_hash_password_uses_a_fresh_random_salt_each_time():
    a = hash_password("same-password", n=2 ** 12, r=8, p=1, dklen=32)
    b = hash_password("same-password", n=2 ** 12, r=8, p=1, dklen=32)
    assert a.salt != b.salt
    assert a.derived_key != b.derived_key


def test_default_scrypt_parameters_are_rfc7914_interactive_baseline():
    # Reads the field DEFAULTS off the Settings class itself, not the
    # process-wide `settings` singleton -- this file's own autouse
    # _cheap_scrypt fixture above monkeypatches that singleton's own
    # account_password_scrypt_n down for every other test's speed, so
    # asserting against the live singleton here would just be checking
    # this file's own test fixture instead of app/config.py's real
    # shipped default.
    from app.config import Settings
    fields = Settings.model_fields
    assert fields["account_password_scrypt_n"].default == 2 ** 14
    assert fields["account_password_scrypt_r"].default == 8
    assert fields["account_password_scrypt_p"].default == 1
    assert fields["account_password_scrypt_dklen"].default == 32


# =========================================================================
# POST /api/account/rotate-key
# =========================================================================

def test_rotate_key_requires_a_linked_player(client, db_path):
    _login(client, db_path)
    resp = client.post("/api/account/rotate-key")
    assert resp.status_code == 404


def test_rotate_key_revokes_all_old_keys_and_issues_exactly_one_new(client, db_path):
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id, player_id=KEY_PLAYER_ID)
    _issue_key(db_path, KEY_PLAYER_ID, "old-key-1")
    _issue_key(db_path, KEY_PLAYER_ID, "old-key-2")

    resp = client.post("/api/account/rotate-key")

    assert resp.status_code == 200
    body = resp.json()
    assert body["rotated"] is True
    assert body["revoked_count"] == 2
    new_key = body["key"]
    assert new_key not in ("old-key-1", "old-key-2")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT key_hash, revoked_at FROM api_key WHERE player_id = ?", (KEY_PLAYER_ID,)
    ).fetchall()
    conn.close()
    assert len(rows) == 3  # 2 old + 1 new
    revoked = {r["key_hash"]: r["revoked_at"] for r in rows}
    assert revoked[hash_secret("old-key-1")] is not None
    assert revoked[hash_secret("old-key-2")] is not None
    assert revoked[hash_secret(new_key)] is None


def test_rotate_key_old_key_genuinely_stops_authenticating(client, db_path, monkeypatch):
    """Uses a REAL McIngestor (not the FakeIngestor the other tests use)
    so this exercises the actual auth-cache invalidation path the spec
    calls out: without ingestor.invalidate_player(), a just-revoked key
    would keep authenticating until its cache entry's TTL expires.
    """
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id, player_id=KEY_PLAYER_ID)
    old_raw_key = "the-old-key"
    _issue_key(db_path, KEY_PLAYER_ID, old_raw_key)

    real_ingestor = McIngestor()
    client.app.state.mc_ingestor = real_ingestor

    # Populate the auth cache with a positive result for the old key --
    # this is the stale entry rotate-key has to actually invalidate.
    pre = _run(real_ingestor.authenticate(old_raw_key))
    assert pre.status == "ok"

    resp = client.post("/api/account/rotate-key")
    assert resp.status_code == 200

    post = _run(real_ingestor.authenticate(old_raw_key))
    assert post.status == "revoked"


def test_rotate_key_audit_row_written(client, db_path):
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id, player_id=KEY_PLAYER_ID)
    _issue_key(db_path, KEY_PLAYER_ID, "old-key")

    client.post("/api/account/rotate-key")

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT kind, actor FROM account_link_event WHERE account_id = ? AND kind = 'key_rotated'",
        (account_id,),
    ).fetchone()
    conn.close()
    assert row == ("key_rotated", "user")


def test_rotate_key_rate_limited(client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "account_rotate_key_rate_limit_attempts", 1)
    monkeypatch.setattr(settings, "account_rotate_key_rate_limit_window_seconds", 60)
    account_id, _ = _login(client, db_path)
    _make_player(db_path, account_id=account_id, player_id=KEY_PLAYER_ID)

    r1 = client.post("/api/account/rotate-key")
    assert r1.status_code == 200

    r2 = client.post("/api/account/rotate-key")
    assert r2.status_code == 429


def test_rotate_key_requires_a_session(client):
    resp = client.post("/api/account/rotate-key")
    assert resp.status_code == 401


# =========================================================================
# POST/DELETE /api/account/password
# =========================================================================

def test_set_password_refused_without_a_verified_email_identity(client, db_path):
    _login(client, db_path)
    resp = client.post("/api/account/password", json={"new_password": "a-fine-password"})
    assert resp.status_code == 409
    assert "verified" in resp.json()["error"]


def test_set_password_refused_when_identity_email_is_not_verified(client, db_path):
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=0)
    resp = client.post("/api/account/password", json={"new_password": "a-fine-password"})
    assert resp.status_code == 409


def test_set_password_first_time_requires_no_current_password(client, db_path):
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)

    resp = client.post("/api/account/password", json={"new_password": "a-fine-password"})

    assert resp.status_code == 200
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT 1 FROM account_password WHERE account_id = ?", (account_id,)).fetchone()
    conn.close()
    assert row is not None


def test_set_password_too_short_is_rejected(client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "account_password_min_length", 8)
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)

    resp = client.post("/api/account/password", json={"new_password": "short"})

    assert resp.status_code == 400
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT 1 FROM account_password WHERE account_id = ?", (account_id,)).fetchone()
    conn.close()
    assert row is None


def test_change_password_requires_current_password(client, db_path):
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)
    _set_password(db_path, account_id, "original-password")

    resp = client.post("/api/account/password", json={"new_password": "brand-new-password"})

    assert resp.status_code == 400


def test_change_password_wrong_current_password_is_rejected(client, db_path):
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)
    _set_password(db_path, account_id, "original-password")

    resp = client.post(
        "/api/account/password",
        json={"current_password": "totally-wrong", "new_password": "brand-new-password"},
    )

    assert resp.status_code == 401


def test_change_password_success_replaces_the_hash(client, db_path):
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)
    _set_password(db_path, account_id, "original-password")

    resp = client.post(
        "/api/account/password",
        json={"current_password": "original-password", "new_password": "brand-new-password"},
    )
    assert resp.status_code == 200

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT salt, n, r, p, dklen, hash FROM account_password WHERE account_id = ?", (account_id,)
    ).fetchone()
    conn.close()
    from app.password_login import PasswordHash
    stored = PasswordHash(
        salt=row["salt"], n=row["n"], r=row["r"], p=row["p"], dklen=row["dklen"], derived_key=row["hash"]
    )
    assert verify_password("brand-new-password", stored)
    assert not verify_password("original-password", stored)


def test_delete_password_requires_a_password_to_exist(client, db_path):
    _login(client, db_path)
    resp = client.delete("/api/account/password")
    assert resp.status_code == 404


def test_delete_password_refused_at_the_last_door(client, db_path):
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)
    _set_password(db_path, account_id, "only-door")
    # Remove the identity so password is the ONLY door left.
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM account_identity WHERE account_id = ?", (account_id,))
    conn.commit()
    conn.close()

    resp = client.delete("/api/account/password")

    assert resp.status_code == 409
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT 1 FROM account_password WHERE account_id = ?", (account_id,)).fetchone()
    conn.close()
    assert row is not None


def test_delete_password_allowed_when_an_identity_remains(client, db_path):
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)
    _set_password(db_path, account_id, "removable")

    resp = client.delete("/api/account/password")

    assert resp.status_code == 200
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT 1 FROM account_password WHERE account_id = ?", (account_id,)).fetchone()
    conn.close()
    assert row is None


# =========================================================================
# POST /auth/password/start
# =========================================================================

def _identity_and_password(db_path, *, email="dev@example.com", password="a-real-password") -> int:
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, provider="github", email=email, email_verified=1)
    _set_password(db_path, account_id, password)
    return account_id


def test_password_start_success_issues_a_session(client, db_path):
    _identity_and_password(db_path, email="dev@example.com", password="a-real-password")

    resp = client.post("/auth/password/start", json={"email": "dev@example.com", "password": "a-real-password"})

    assert resp.status_code == 200
    assert resp.json()["result"] == "login"
    assert SESSION_COOKIE_NAME in resp.cookies


def test_password_start_never_creates_an_account_or_identity(client, db_path):
    before_accounts = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM account").fetchone()[0]
    before_identities = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM account_identity").fetchone()[0]

    resp = client.post("/auth/password/start", json={"email": "nobody@example.com", "password": "whatever12"})

    assert resp.status_code == 401
    after_accounts = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM account").fetchone()[0]
    after_identities = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM account_identity").fetchone()[0]
    assert after_accounts == before_accounts
    assert after_identities == before_identities
    # And no pending-identity row either -- this must never touch that
    # decision tree at all.
    pending = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM account_pending_identity").fetchone()[0]
    assert pending == 0


def test_password_start_three_failure_cases_return_identical_response(client, db_path):
    # Case A: no such account at all.
    resp_a = client.post("/auth/password/start", json={"email": "ghost@example.com", "password": "whatever12"})

    # Case B: account exists (verified email) but has no password set.
    no_password_account = _make_account(db_path)
    _add_identity(db_path, no_password_account, provider="github", email="nopass@example.com", email_verified=1)
    resp_b = client.post("/auth/password/start", json={"email": "nopass@example.com", "password": "whatever12"})

    # Case C: account exists, has a password, but the password is wrong.
    _identity_and_password(db_path, email="haspass@example.com", password="the-real-one")
    resp_c = client.post("/auth/password/start", json={"email": "haspass@example.com", "password": "the-wrong-one"})

    assert resp_a.status_code == resp_b.status_code == resp_c.status_code == 401
    assert resp_a.json() == resp_b.json() == resp_c.json()


def test_password_start_wrong_password_does_not_create_a_session(client, db_path):
    _identity_and_password(db_path, email="dev@example.com", password="a-real-password")
    resp = client.post("/auth/password/start", json={"email": "dev@example.com", "password": "nope"})
    assert resp.status_code == 401
    assert SESSION_COOKIE_NAME not in resp.cookies


def test_password_start_ip_rate_limited(client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "account_password_start_ip_rate_limit_attempts", 1)
    monkeypatch.setattr(settings, "account_password_start_ip_rate_limit_window_seconds", 60)

    r1 = client.post("/auth/password/start", json={"email": "a@example.com", "password": "whatever12"})
    assert r1.status_code == 401

    r2 = client.post("/auth/password/start", json={"email": "b@example.com", "password": "whatever12"})
    assert r2.status_code == 429


def test_password_start_address_rate_limited(client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "account_password_start_address_rate_limit_attempts", 1)
    monkeypatch.setattr(settings, "account_password_start_address_rate_limit_window_seconds", 60)
    monkeypatch.setattr(settings, "account_password_backoff_base_seconds", 0.0)

    r1 = client.post("/auth/password/start", json={"email": "same@example.com", "password": "whatever12"})
    assert r1.status_code == 401

    r2 = client.post("/auth/password/start", json={"email": "same@example.com", "password": "whatever12"})
    assert r2.status_code == 429


def test_password_start_escalating_backoff_locks_out_repeated_failures(client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "account_password_start_address_rate_limit_attempts", 1000)
    monkeypatch.setattr(settings, "account_password_backoff_base_seconds", 100.0)  # long enough to observe within the test
    _identity_and_password(db_path, email="dev@example.com", password="a-real-password")

    r1 = client.post("/auth/password/start", json={"email": "dev@example.com", "password": "wrong"})
    assert r1.status_code == 401

    r2 = client.post("/auth/password/start", json={"email": "dev@example.com", "password": "a-real-password"})
    assert r2.status_code == 429  # locked out even with the CORRECT password now


def test_password_start_backoff_resets_on_success(client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "account_password_start_address_rate_limit_attempts", 1000)
    monkeypatch.setattr(settings, "account_password_backoff_base_seconds", 0.01)
    monkeypatch.setattr(settings, "account_password_backoff_factor", 1.0)
    _identity_and_password(db_path, email="dev@example.com", password="a-real-password")

    r1 = client.post("/auth/password/start", json={"email": "dev@example.com", "password": "wrong"})
    assert r1.status_code == 401
    time.sleep(0.05)

    r2 = client.post("/auth/password/start", json={"email": "dev@example.com", "password": "a-real-password"})
    assert r2.status_code == 200

    assert "dev@example.com" not in oauth_api_module._password_backoff._state


def test_password_start_works_even_when_no_providers_are_configured(client, db_path):
    """frontend/signin-email.js's PASSWORD_SIGNIN_AVAILABLE is a
    hardcoded `true`, never derived from GET /auth/providers, on the
    strength of exactly this: unlike every OAuth provider (needs a
    client id/secret) or magic-link email (needs SMTP settings), this
    door has no provider_enabled()-style gate at all -- see
    list_providers()'s own docstring in app/oauth_api.py, which never
    appends a "password" entry to that response under any
    configuration. Confirms both halves of that assumption on a bare,
    fully-unconfigured deployment: the list really is empty, and
    password sign-in works anyway.
    """
    providers_resp = client.get("/auth/providers")
    assert providers_resp.status_code == 200
    assert providers_resp.json() == {"providers": []}

    _identity_and_password(db_path, email="dev@example.com", password="a-real-password")
    resp = client.post("/auth/password/start", json={"email": "dev@example.com", "password": "a-real-password"})
    assert resp.status_code == 200
    assert resp.json()["result"] == "login"
    assert SESSION_COOKIE_NAME in resp.cookies


# =========================================================================
# GET /api/account -- extended shape
# =========================================================================

def test_get_account_reports_has_password_and_can_remove(client, db_path):
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)
    _set_password(db_path, account_id, "a-password")

    body = client.get("/api/account").json()

    assert body["has_password"] is True
    assert len(body["identities"]) == 1
    # Two doors total (identity + password) -- removing the one
    # identity still leaves the password, so it CAN be removed.
    assert body["identities"][0]["can_remove"] is True


def test_get_account_identity_cannot_be_removed_at_the_last_door(client, db_path):
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)

    body = client.get("/api/account").json()

    assert body["has_password"] is False
    assert body["identities"][0]["can_remove"] is False


# =========================================================================
# GET /api/account -- owes_password (app/account_api.py's _owes_password())
#
# Matt's rule: "if an account has an email on file, it must have a
# password; if it has no email, nothing is required." Deliberately the
# SAME condition POST /api/account/password's own guard checks --
# _has_verified_identity_email() -- so an account can never be told to
# do something it is refused the ability to do. "Email on file" means
# an account_identity row with email_verified=1 ONLY -- a verified
# account.contact_email deliberately does NOT count (it is never
# usable to sign in at all; see app/db.py's account.contact_email
# MIGRATIONS comment and the CRITICAL/NON-NEGOTIABLE comment on
# resolve_oauth_callback's case 3 in app/oauth_api.py for exactly why
# folding it in here would compel a password nobody could ever use to
# sign in).
# =========================================================================

def test_owes_password_true_with_verified_identity_and_no_password(client, db_path):
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)

    body = client.get("/api/account").json()

    assert body["owes_password"] is True


def test_owes_password_false_with_verified_identity_and_password(client, db_path):
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)
    _set_password(db_path, account_id, "a-fine-password")

    body = client.get("/api/account").json()

    assert body["owes_password"] is False


def test_owes_password_false_with_no_verified_email_at_all(client, db_path):
    # The "no email, nothing required" half of the rule -- an account
    # with no identities whatsoever (e.g. signed up some other way,
    # nothing linked yet) must not be told it owes a password. This is
    # the case that must never regress.
    _login(client, db_path)

    body = client.get("/api/account").json()

    assert body["owes_password"] is False


def test_owes_password_false_with_unverified_identity_email(client, db_path):
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=0)

    body = client.get("/api/account").json()

    assert body["owes_password"] is False


def test_owes_password_false_with_verified_contact_email_and_no_verified_identity(client, db_path, monkeypatch):
    # Pins the deliberate exclusion: contact_email is never usable to
    # sign in, so verifying one must NOT trigger the requirement -- if
    # a later change quietly folds contact_email into "email on file,"
    # this is the test that should catch it.
    _enable_email(monkeypatch)
    calls = _stub_send(monkeypatch)
    account_id, _ = _login(client, db_path)
    client.post("/api/account/contact-email", json={"email": "me@example.com"})
    raw_token = calls[0][1].split("token=", 1)[1]
    verify_resp = client.get("/auth/contact-email/verify", params={"token": raw_token, "format": "json"})
    assert verify_resp.json()["verified"] is True

    body = client.get("/api/account").json()

    assert body["contact_email"]["verified"] is True
    assert body["owes_password"] is False


def test_owes_password_is_a_standing_condition_not_only_at_signup(client, db_path):
    # Matt's corrected example: an account created via a GitHub identity
    # with no verified email owes nothing at signup; if that person
    # LATER links an identity that arrives with a verified email (a
    # second OAuth provider, or email magic-link sign-in), the
    # requirement applies from that moment -- evaluated fresh on every
    # GET /api/account, not stamped once at account creation.
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", subject="gh-sub-1", email=None, email_verified=0)
    assert client.get("/api/account").json()["owes_password"] is False

    _add_identity(db_path, account_id, provider="google", email="dev@example.com", email_verified=1)

    assert client.get("/api/account").json()["owes_password"] is True


def test_owes_password_cleared_by_setting_a_password(client, db_path):
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)
    assert client.get("/api/account").json()["owes_password"] is True

    resp = client.post("/api/account/password", json={"new_password": "a-fine-password"})
    assert resp.status_code == 200

    assert client.get("/api/account").json()["owes_password"] is False


def test_owing_a_password_is_not_an_authorization_gate(client, db_path):
    # This is a required ONBOARDING step, not a block: an account that
    # owes a password must still be able to read its own account, set
    # the password it owes, and sign out -- none of that may be
    # refused while owes_password is True.
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)

    read_resp = client.get("/api/account")
    assert read_resp.status_code == 200
    assert read_resp.json()["owes_password"] is True

    set_resp = client.post("/api/account/password", json={"new_password": "a-fine-password"})
    assert set_resp.status_code == 200

    logout_resp = client.post("/api/account/logout")
    assert logout_resp.status_code == 200


# =========================================================================
# DELETE /api/account/identity/{provider} -- last-door guard
# =========================================================================

def test_unlink_identity_refused_when_it_is_the_only_door(client, db_path):
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)

    resp = client.delete("/api/account/identity/github")

    assert resp.status_code == 409
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT 1 FROM account_identity WHERE account_id = ? AND provider = 'github'", (account_id,)
    ).fetchone()
    conn.close()
    assert row is not None


def test_unlink_identity_allowed_when_another_identity_remains(client, db_path):
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)
    _add_identity(db_path, account_id, provider="google", subject="sub-1", email="dev@gmail.com", email_verified=1)

    resp = client.delete("/api/account/identity/github")

    assert resp.status_code == 200
    conn = sqlite3.connect(db_path)
    remaining = conn.execute(
        "SELECT provider FROM account_identity WHERE account_id = ?", (account_id,)
    ).fetchall()
    conn.close()
    assert [r[0] for r in remaining] == ["google"]


def test_unlink_identity_allowed_when_a_password_is_the_remaining_door(client, db_path):
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)
    _set_password(db_path, account_id, "a-password")

    resp = client.delete("/api/account/identity/github")

    assert resp.status_code == 200
    body = resp.json()
    assert body["warning_last_door"] is True  # password is now the only door


def test_unlink_identity_refused_when_a_password_alone_would_not_be_enough(client, db_path):
    """A password counts as a door too -- but it must still exist. If
    the account has one identity and NO password, removing that
    identity must be refused exactly like the no-password case above,
    proving the guard actually checks account_password rather than
    just assuming one exists.
    """
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)

    resp = client.delete("/api/account/identity/github")

    assert resp.status_code == 409


def test_unlink_unknown_provider_is_404(client, db_path):
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)
    resp = client.delete("/api/account/identity/discord")
    assert resp.status_code == 404


def test_unlink_identity_writes_audit_event(client, db_path):
    account_id, _ = _login(client, db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)
    _add_identity(db_path, account_id, provider="google", subject="sub-1", email="dev@gmail.com", email_verified=1)

    client.delete("/api/account/identity/github")

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT kind, detail FROM account_link_event WHERE account_id = ? AND kind = 'identity_unlinked'",
        (account_id,),
    ).fetchone()
    conn.close()
    assert row[0] == "identity_unlinked"
    assert "github" in row[1]


# =========================================================================
# POST /api/account/contact-email + GET /auth/contact-email/verify
# =========================================================================

def _enable_email(monkeypatch) -> None:
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    monkeypatch.setattr(settings, "account_session_cookie_secure", False)


def _stub_send(monkeypatch, *, raises: bool = False):
    calls = []

    async def _fake(to_address: str, link_url: str) -> None:
        calls.append((to_address, link_url))
        if raises:
            from app.email_login import EmailSendError
            raise EmailSendError("boom")

    monkeypatch.setattr(account_api_module, "send_magic_link_email", _fake)
    return calls


def test_contact_email_disabled_without_smtp_configured(client, db_path):
    _login(client, db_path)
    resp = client.post("/api/account/contact-email", json={"email": "me@example.com"})
    assert resp.status_code == 404


def test_contact_email_set_is_unverified_and_mails_a_link(client, db_path, monkeypatch):
    _enable_email(monkeypatch)
    calls = _stub_send(monkeypatch)
    account_id, _ = _login(client, db_path)

    resp = client.post("/api/account/contact-email", json={"email": "me@example.com"})

    assert resp.status_code == 200
    assert resp.json()["verified"] is False
    assert len(calls) == 1
    assert calls[0][0] == "me@example.com"

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT contact_email, contact_email_verified_at FROM account WHERE account_id = ?", (account_id,)
    ).fetchone()
    conn.close()
    assert row[0] == "me@example.com"
    assert row[1] is None


def test_contact_email_verify_marks_it_verified(client, db_path, monkeypatch):
    _enable_email(monkeypatch)
    calls = _stub_send(monkeypatch)
    account_id, _ = _login(client, db_path)
    client.post("/api/account/contact-email", json={"email": "me@example.com"})
    link_url = calls[0][1]
    raw_token = link_url.split("token=", 1)[1]

    resp = client.get("/auth/contact-email/verify", params={"token": raw_token, "format": "json"})

    assert resp.status_code == 200
    assert resp.json()["verified"] is True
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT contact_email_verified_at FROM account WHERE account_id = ?", (account_id,)
    ).fetchone()
    conn.close()
    assert row[0] is not None


def test_contact_email_verify_stale_link_does_not_verify_a_changed_address(client, db_path, monkeypatch):
    _enable_email(monkeypatch)
    calls = _stub_send(monkeypatch)
    account_id, _ = _login(client, db_path)
    client.post("/api/account/contact-email", json={"email": "old@example.com"})
    stale_link = calls[0][1]
    stale_token = stale_link.split("token=", 1)[1]

    # Address changed again before the first link was ever clicked.
    client.post("/api/account/contact-email", json={"email": "new@example.com"})

    resp = client.get("/auth/contact-email/verify", params={"token": stale_token, "format": "json"})

    assert resp.status_code == 200
    assert resp.json()["verified"] is False
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT contact_email, contact_email_verified_at FROM account WHERE account_id = ?", (account_id,)
    ).fetchone()
    conn.close()
    assert row[0] == "new@example.com"
    assert row[1] is None


def test_contact_email_verify_unknown_token_is_an_error(client, db_path, monkeypatch):
    _enable_email(monkeypatch)
    resp = client.get(
        "/auth/contact-email/verify", params={"token": "never-issued", "format": "json"}
    )
    assert resp.status_code == 400


def test_contact_email_verify_does_not_require_a_session(client, db_path, monkeypatch):
    """The mailed link is the whole proof -- a person may click it from
    a different browser/device than the one they are signed in on."""
    _enable_email(monkeypatch)
    calls = _stub_send(monkeypatch)
    _login(client, db_path)
    client.post("/api/account/contact-email", json={"email": "me@example.com"})
    link_url = calls[0][1]
    raw_token = link_url.split("token=", 1)[1]

    client.cookies.clear()  # no session at all for the verify request
    resp = client.get("/auth/contact-email/verify", params={"token": raw_token, "format": "json"})

    assert resp.status_code == 200
    assert resp.json()["verified"] is True


def test_verified_contact_email_does_not_auto_link_a_new_provider_identity(client, db_path, monkeypatch):
    """The critical non-negotiable rule: a verified contact_email must
    NEVER participate in resolve_oauth_callback's case-3 auto-link
    match. Set and verify a contact email on account A, then have a
    completely different, brand-new OAuth identity report that SAME
    address as ITS OWN provider-verified email -- this must NOT
    auto-link onto account A (case 3), and must instead fall through to
    case 4 (parked as a pending identity) exactly as it would for any
    address no account holds via account_identity.
    """
    from app.oauth import ProviderIdentity
    from app.oauth_api import resolve_oauth_callback

    _enable_email(monkeypatch)
    calls = _stub_send(monkeypatch)
    account_id, _ = _login(client, db_path)
    client.post("/api/account/contact-email", json={"email": "shared@example.com"})
    raw_token = calls[0][1].split("token=", 1)[1]
    verify_resp = client.get(
        "/auth/contact-email/verify", params={"token": raw_token, "format": "json"}
    )
    assert verify_resp.json()["verified"] is True

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    identity = ProviderIdentity(subject="new-sub", email="shared@example.com", email_verified=True)
    outcome = resolve_oauth_callback(
        conn,
        provider_name="github",
        identity=identity,
        current_account_id=None,
        now=int(time.time()),
    )
    conn.commit()
    conn.close()

    assert outcome["case"] == "pending"
    assert outcome.get("account_id") != account_id


# =========================================================================
# GET /auth/contact-email/verify -- the default BROWSER (redirect) shape,
# as opposed to the `?format=json` coverage above. Same fixtures/helpers;
# every request here is made WITHOUT format=json and WITH
# follow_redirects=False, mirroring tests/test_oauth_api.py's own "Part 4"
# redirect coverage of GET /auth/{provider}/callback.
# =========================================================================

def test_contact_email_verify_redirect_on_success(client, db_path, monkeypatch):
    _enable_email(monkeypatch)
    calls = _stub_send(monkeypatch)
    _login(client, db_path)
    client.post("/api/account/contact-email", json={"email": "me@example.com"})
    raw_token = calls[0][1].split("token=", 1)[1]

    resp = client.get(
        "/auth/contact-email/verify", params={"token": raw_token}, follow_redirects=False
    )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/account/verify-email?ok=1"


def test_contact_email_verify_redirect_on_unknown_token(client, db_path, monkeypatch):
    _enable_email(monkeypatch)

    resp = client.get(
        "/auth/contact-email/verify", params={"token": "never-issued"}, follow_redirects=False
    )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/account/verify-email?ok=0"


def test_contact_email_verify_redirect_on_missing_token(client, db_path, monkeypatch):
    _enable_email(monkeypatch)

    resp = client.get("/auth/contact-email/verify", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/account/verify-email?ok=0"
