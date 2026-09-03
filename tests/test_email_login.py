"""Tests for passwordless email sign-in: app/email_login.py's address
shape/SMTP half, and app/oauth_api.py's POST /auth/email/start +
GET /auth/email/callback routes (which reuse resolve_oauth_callback()
-- the exact same callback decision tree tests/test_oauth_api.py
exercises via GitHub -- unmodified, provider_name="email").

Same two-fixture split tests/test_oauth_api.py already uses:
- `conn` (tests/conftest.py, in-memory) for _sweep_stale_rows() and
  resolve_oauth_callback() directly.
- `db_path` (real file) for the HTTP-level tests (TestClient runs the
  ASGI app in a different OS thread; app/db.py's connect()/WriteSession
  open a fresh connection per call, so ":memory:" would not share data
  across that boundary).

Every test that reaches POST /auth/email/start monkeypatches
app.oauth_api.send_magic_link_email to a stub -- no real SMTP
connection, no real mail, anywhere in this file.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.oauth_api as oauth_api_module
from app import email_login
from app.auth import http_exception_as_error_body
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.mc_ingest import hash_secret
from app.oauth import ProviderIdentity
from app.oauth_api import _sweep_stale_rows, resolve_oauth_callback
from app.oauth_api import router as oauth_router
from app.sessions import SESSION_COOKIE_NAME, create_session


def _run(coro):
    return asyncio.run(coro)


# =========================================================================
# Part 0: app/email_login.py -- shape validation, enablement, SMTP send
# =========================================================================


def test_looks_like_email_accepts_ordinary_addresses():
    assert email_login.looks_like_email("dev@example.com")
    assert email_login.looks_like_email("first.last+tag@sub.example.co")


@pytest.mark.parametrize(
    "bad",
    ["", "not-an-email", "a@b", "a b@example.com", "@example.com", "a@", "a@example.com "],
)
def test_looks_like_email_rejects_garbage(bad):
    assert not email_login.looks_like_email(bad)


def test_looks_like_email_rejects_overlong_address():
    huge = "a" * 250 + "@example.com"
    assert not email_login.looks_like_email(huge)


def test_normalize_email_trims_and_lowercases():
    assert email_login.normalize_email("  User@Example.COM  ") == "user@example.com"


def test_email_login_enabled_requires_both_smtp_host_and_public_base_url(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "oauth_public_base_url", "")
    assert email_login.email_login_enabled() is False

    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "oauth_public_base_url", "")
    assert email_login.email_login_enabled() is False  # half-configured -- still off

    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    assert email_login.email_login_enabled() is False

    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    assert email_login.email_login_enabled() is True


def test_send_magic_link_email_runs_off_the_event_loop(monkeypatch):
    """The actual SMTP conversation is blocking stdlib smtplib -- this
    codebase has previously been bitten by a blocking call starving the
    shared event loop (see app/email_login.py's own module docstring),
    so send_magic_link_email() must run it via asyncio.to_thread, not
    call it directly on the caller's own task. Proven here by recording
    which thread the (fully mocked) SMTP conversation actually runs on.
    """
    main_thread = threading.current_thread()
    seen = {}

    class _FakeSMTP:
        def __init__(self, *a, **k):
            seen["thread"] = threading.current_thread()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, **kwargs):
            pass

        def login(self, *a):
            pass

        def send_message(self, msg):
            pass

    monkeypatch.setattr(email_login.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_tls_mode", "starttls")

    _run(email_login.send_magic_link_email("dev@example.com", "https://mw.test/auth/email/callback?token=x"))

    assert "thread" in seen
    assert seen["thread"] is not main_thread


def test_send_magic_link_email_wraps_smtp_failure_in_email_send_error(monkeypatch):
    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(email_login.smtplib, "SMTP", _boom)
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")

    with pytest.raises(email_login.EmailSendError):
        _run(email_login.send_magic_link_email("dev@example.com", "https://mw.test/x"))


# =========================================================================
# Part 1: _sweep_stale_rows() -- the opportunistic cleanup helper
# =========================================================================


def test_sweep_deletes_expired_and_consumed_past_grace_leaves_live_rows(conn):
    now = 1_000_000
    grace = oauth_api_module._SWEEP_GRACE_SECONDS

    conn.execute(
        "INSERT INTO email_login_token(token_hash, email, created_at, expires_at, consumed_at) "
        "VALUES ('expired-old', 'a@example.com', ?, ?, NULL)",
        (now - 10000, now - grace - 100),
    )
    conn.execute(
        "INSERT INTO email_login_token(token_hash, email, created_at, expires_at, consumed_at) "
        "VALUES ('expired-recent', 'b@example.com', ?, ?, NULL)",
        (now - 100, now - 10),
    )
    conn.execute(
        "INSERT INTO email_login_token(token_hash, email, created_at, expires_at, consumed_at) "
        "VALUES ('consumed-old', 'c@example.com', ?, ?, ?)",
        (now - 10000, now + 10000, now - grace - 100),
    )
    conn.execute(
        "INSERT INTO email_login_token(token_hash, email, created_at, expires_at, consumed_at) "
        "VALUES ('still-live', 'd@example.com', ?, ?, NULL)",
        (now - 100, now + 10000),
    )

    _sweep_stale_rows(conn, "email_login_token", now)

    remaining = {r["token_hash"] for r in conn.execute("SELECT token_hash FROM email_login_token").fetchall()}
    # expired-old (past grace) and consumed-old (past grace) are gone;
    # expired-recent (within grace) and still-live are untouched.
    assert remaining == {"expired-recent", "still-live"}


def test_sweep_rejects_a_table_it_does_not_own(conn):
    with pytest.raises(AssertionError):
        _sweep_stale_rows(conn, "account", 0)


def test_case4_pending_write_sweeps_stale_pending_rows(conn):
    """resolve_oauth_callback's own case 4 write triggers the sweep --
    proves the wiring, not just the helper in isolation.
    """
    now = 1_000_000
    grace = oauth_api_module._SWEEP_GRACE_SECONDS
    conn.execute(
        "INSERT INTO account_pending_identity"
        "(token_hash, provider, subject, email, email_verified, created_at, expires_at, consumed_at) "
        "VALUES ('stale-pending', 'github', 'old-sub', NULL, 0, ?, ?, NULL)",
        (now - 10000, now - grace - 100),
    )

    identity = ProviderIdentity(subject="new-sub", email=None, email_verified=False)
    resolve_oauth_callback(conn, provider_name="github", identity=identity, current_account_id=None, now=now)

    hashes = {r["token_hash"] for r in conn.execute("SELECT token_hash FROM account_pending_identity").fetchall()}
    assert "stale-pending" not in hashes
    assert any(h != "stale-pending" for h in hashes)  # the fresh case-4 row is still there


# =========================================================================
# Part 2: HTTP routes (TestClient) -- POST /auth/email/start,
# GET /auth/email/callback, GET /auth/providers
# =========================================================================


def _init_schema(path: str) -> None:
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            c.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                continue
            raise
    c.commit()
    c.close()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    import app.db as db_module

    path = str(tmp_path / "game.db")
    _init_schema(path)
    monkeypatch.setattr(db_module.settings, "db_path", path)
    return path


@pytest.fixture(autouse=True)
def _reset_email_rate_limiters():
    """oauth_api.py's email-sign-in rate limiters are module-level
    singletons (see app/auth.py's module docstring for why every
    _BoundedHits budget in this codebase is built once, at import time)
    -- cleared before and after every test in this file, same pattern
    tests/test_oauth_api.py's own autouse fixture uses for its limiters.
    """
    oauth_api_module._email_start_ip_limiter._hits.clear()
    oauth_api_module._email_start_addr_limiter._hits.clear()
    yield
    oauth_api_module._email_start_ip_limiter._hits.clear()
    oauth_api_module._email_start_addr_limiter._hits.clear()


def _enable_email(monkeypatch) -> None:
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    # TestClient's default base_url is http://testserver, plain HTTP --
    # see tests/test_oauth_api.py's own _enable_github() for why this
    # has to be false for the session cookie to round-trip in tests.
    monkeypatch.setattr(settings, "account_session_cookie_secure", False)


def _stub_send(monkeypatch, *, raises: bool = False):
    """Replaces oauth_api's own reference to send_magic_link_email with
    an async stub that records every call and sends NO real mail.
    Patched on oauth_api_module (the name that module imported into its
    own namespace), not app.email_login -- same reasoning
    tests/test_oauth_api.py's _patch_provider_http gives for patching
    oauth_api_module.httpx rather than the httpx module itself.
    """
    calls = []

    async def _fake(to_address: str, link_url: str) -> None:
        calls.append((to_address, link_url))
        if raises:
            raise email_login.EmailSendError("boom")

    monkeypatch.setattr(oauth_api_module, "send_magic_link_email", _fake)
    return calls


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(oauth_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    return TestClient(app)


def _make_email_token(db_path: str, *, email: str = "new@example.com", ttl: int = 900, consumed: bool = False) -> str:
    raw_token = "raw-email-token-" + email
    token_hash = hash_secret(raw_token)
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO email_login_token(token_hash, email, created_at, expires_at, consumed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (token_hash, email, now, now + ttl, now if consumed else None),
    )
    conn.commit()
    conn.close()
    return raw_token


def _make_account_and_session(db_path: str) -> tuple[int, str]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    account_id = cur.lastrowid
    conn.commit()
    conn.close()
    raw_token = _run(create_session(account_id, device_label=None))
    return account_id, raw_token


# ---- disabled state ---------------------------------------------------


def test_email_disabled_by_default_omitted_from_providers(db_path):
    client = _client()
    resp = client.get("/auth/providers")
    assert resp.status_code == 200
    assert resp.json() == {"providers": []}


def test_email_disabled_by_default_start_404s(db_path):
    client = _client()
    resp = client.post("/auth/email/start", json={"email": "dev@example.com"})
    assert resp.status_code == 404


def test_email_disabled_by_default_callback_404s(db_path):
    client = _client()
    resp = client.get("/auth/email/callback", params={"token": "whatever"})
    assert resp.status_code == 404


def test_email_half_configured_stays_disabled(db_path, monkeypatch):
    # smtp_host set, oauth_public_base_url blank -- treated as fully
    # off, same "empty means off" contract every other credential pair
    # in this app uses.
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    client = _client()
    resp = client.get("/auth/providers")
    assert resp.status_code == 200
    assert resp.json() == {"providers": []}


def test_email_listed_in_providers_once_enabled(db_path, monkeypatch):
    _enable_email(monkeypatch)
    client = _client()
    resp = client.get("/auth/providers")
    assert resp.status_code == 200
    assert resp.json() == {"providers": [{"name": "email", "label": "Email"}]}


# ---- POST /auth/email/start ------------------------------------------


def test_start_sends_a_link_and_stores_only_the_hash(db_path, monkeypatch):
    _enable_email(monkeypatch)
    calls = _stub_send(monkeypatch)
    client = _client()

    resp = client.post("/auth/email/start", json={"email": "  Dev@Example.com  "})
    assert resp.status_code == 200, resp.text
    assert resp.json() == dict(oauth_api_module._EMAIL_START_RESPONSE_BODY)

    assert len(calls) == 1
    sent_to, link_url = calls[0]
    assert sent_to == "dev@example.com"  # normalized before sending
    assert "/auth/email/callback?token=" in link_url

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT token_hash, email FROM email_login_token").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["email"] == "dev@example.com"
    assert rows[0]["token_hash"] != "dev@example.com"  # never the raw token or address as the hash


def test_start_rejects_malformed_address_without_sending(db_path, monkeypatch):
    _enable_email(monkeypatch)
    calls = _stub_send(monkeypatch)
    client = _client()

    resp = client.post("/auth/email/start", json={"email": "not-an-email"})
    assert resp.status_code == 400
    assert calls == []  # never sent

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM email_login_token").fetchone()[0]
    conn.close()
    assert count == 0


def test_start_missing_body_field_is_400(db_path, monkeypatch):
    _enable_email(monkeypatch)
    _stub_send(monkeypatch)
    client = _client()
    resp = client.post("/auth/email/start", json={})
    assert resp.status_code == 400


def test_start_identical_response_for_known_and_unknown_address(db_path, monkeypatch):
    """No account enumeration: POST /auth/email/start never look up an
    account at all, and returns the exact same body regardless.
    """
    _enable_email(monkeypatch)
    _stub_send(monkeypatch)

    # A "known" address: one that already has an account_identity row.
    conn = sqlite3.connect(db_path)
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    account_id = cur.lastrowid
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('email', 'known@example.com', ?, 'known@example.com', 1, ?)",
        (account_id, int(time.time())),
    )
    conn.commit()
    conn.close()

    client = _client()
    r_known = client.post("/auth/email/start", json={"email": "known@example.com"})
    r_unknown = client.post("/auth/email/start", json={"email": "never-seen-before@example.com"})

    assert r_known.status_code == r_unknown.status_code == 200
    assert r_known.json() == r_unknown.json()


def test_start_send_failure_still_returns_the_generic_success_body(db_path, monkeypatch):
    """A send failure must not leak to the caller as a different
    response than success -- see app/email_login.py's EmailSendError and
    app/oauth_api.py's email_start() docstring.
    """
    _enable_email(monkeypatch)
    _stub_send(monkeypatch, raises=True)
    client = _client()

    resp = client.post("/auth/email/start", json={"email": "dev@example.com"})
    assert resp.status_code == 200
    assert resp.json() == dict(oauth_api_module._EMAIL_START_RESPONSE_BODY)

    # The token is still issued and stored even though the send failed
    # -- a person can still be mailed a working link on a retry, and
    # nothing about the token's own validity depends on the send having
    # succeeded.
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM email_login_token").fetchone()[0]
    conn.close()
    assert count == 1


def test_start_rate_limited_per_ip(db_path, monkeypatch):
    _enable_email(monkeypatch)
    _stub_send(monkeypatch)
    monkeypatch.setattr(settings, "email_login_start_ip_rate_limit_attempts", 1)
    monkeypatch.setattr(settings, "email_login_start_ip_rate_limit_window_seconds", 60)
    monkeypatch.setattr(settings, "email_login_start_address_rate_limit_attempts", 1000)
    monkeypatch.setattr(settings, "email_login_start_address_rate_limit_window_seconds", 60)
    client = _client()

    r1 = client.post("/auth/email/start", json={"email": "one@example.com"})
    assert r1.status_code == 200

    # Different address, same (test) client IP -- still limited, because
    # the IP budget (not the address budget) is exhausted.
    r2 = client.post("/auth/email/start", json={"email": "two@example.com"})
    assert r2.status_code == 429
    assert r2.json() == {"error": "rate limited"}


def test_start_rate_limited_per_address(db_path, monkeypatch):
    _enable_email(monkeypatch)
    _stub_send(monkeypatch)
    monkeypatch.setattr(settings, "email_login_start_ip_rate_limit_attempts", 1000)
    monkeypatch.setattr(settings, "email_login_start_ip_rate_limit_window_seconds", 60)
    monkeypatch.setattr(settings, "email_login_start_address_rate_limit_attempts", 1)
    monkeypatch.setattr(settings, "email_login_start_address_rate_limit_window_seconds", 60)
    client = _client()

    r1 = client.post("/auth/email/start", json={"email": "same@example.com"})
    assert r1.status_code == 200

    r2 = client.post("/auth/email/start", json={"email": "same@example.com"})
    assert r2.status_code == 429
    assert r2.json() == {"error": "rate limited"}

    # A DIFFERENT address is unaffected -- the address budget is keyed
    # per-address, not shared.
    r3 = client.post("/auth/email/start", json={"email": "different@example.com"})
    assert r3.status_code == 200


def test_start_sweeps_stale_email_login_token_rows(db_path, monkeypatch):
    _enable_email(monkeypatch)
    _stub_send(monkeypatch)
    now = int(time.time())
    grace = oauth_api_module._SWEEP_GRACE_SECONDS

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO email_login_token(token_hash, email, created_at, expires_at, consumed_at) "
        "VALUES ('stale-start-token', 'old@example.com', ?, ?, NULL)",
        (now - 10000, now - grace - 100),
    )
    conn.commit()
    conn.close()

    client = _client()
    resp = client.post("/auth/email/start", json={"email": "dev@example.com"})
    assert resp.status_code == 200

    conn = sqlite3.connect(db_path)
    hashes = {r[0] for r in conn.execute("SELECT token_hash FROM email_login_token").fetchall()}
    conn.close()
    assert "stale-start-token" not in hashes


# ---- GET /auth/email/callback: token lifecycle -------------------------


def test_callback_missing_token_param_rejected(db_path, monkeypatch):
    _enable_email(monkeypatch)
    client = _client()
    resp = client.get("/auth/email/callback", params={"format": "json"})
    assert resp.status_code == 400
    assert resp.json() == {"error": "invalid or expired sign-in link"}


def test_callback_unknown_token_rejected(db_path, monkeypatch):
    _enable_email(monkeypatch)
    client = _client()
    resp = client.get("/auth/email/callback", params={"token": "never-issued", "format": "json"})
    assert resp.status_code == 400
    assert resp.json() == {"error": "invalid or expired sign-in link"}


def test_callback_expired_token_rejected(db_path, monkeypatch):
    _enable_email(monkeypatch)
    raw_token = _make_email_token(db_path, email="expired@example.com", ttl=-10)
    client = _client()
    resp = client.get("/auth/email/callback", params={"token": raw_token, "format": "json"})
    assert resp.status_code == 400
    assert resp.json() == {"error": "invalid or expired sign-in link"}


def test_callback_consumes_token_single_use_reuse_rejected(db_path, monkeypatch):
    _enable_email(monkeypatch)
    raw_token = _make_email_token(db_path, email="once@example.com")
    client = _client()

    r1 = client.get("/auth/email/callback", params={"token": raw_token, "format": "json"})
    assert r1.status_code == 200, r1.text

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT consumed_at FROM email_login_token WHERE token_hash = ?", (hash_secret(raw_token),)
    ).fetchone()
    conn.close()
    assert row["consumed_at"] is not None

    # Reusing the exact same link a second time is rejected -- the
    # underlying row is either gone (swept) or marked consumed, same
    # generic failure either way.
    r2 = client.get("/auth/email/callback", params={"token": raw_token, "format": "json"})
    assert r2.status_code == 400
    assert r2.json() == {"error": "invalid or expired sign-in link"}


# ---- GET /auth/email/callback: the decision tree, all four cases -------


def test_callback_case1_existing_email_identity_logs_in(db_path, monkeypatch):
    _enable_email(monkeypatch)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    account_id = cur.lastrowid
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('email', 'returning@example.com', ?, 'returning@example.com', 1, ?)",
        (account_id, int(time.time())),
    )
    conn.commit()
    conn.close()

    raw_token = _make_email_token(db_path, email="returning@example.com")
    client = _client()
    resp = client.get("/auth/email/callback", params={"token": raw_token, "format": "json"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"result": "login", "account_id": account_id}
    assert "mw_session" in resp.cookies


def test_callback_case2_links_new_email_onto_the_logged_in_account(db_path, monkeypatch):
    _enable_email(monkeypatch)
    account_id, raw_session = _make_account_and_session(db_path)
    raw_token = _make_email_token(db_path, email="second-method@example.com")

    client = _client()
    client.cookies.set(SESSION_COOKIE_NAME, raw_session)
    resp = client.get("/auth/email/callback", params={"token": raw_token, "format": "json"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"result": "linked", "account_id": account_id}
    assert "mw_session" not in resp.cookies  # already had one

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT account_id FROM account_identity WHERE provider = 'email' AND subject = 'second-method@example.com'"
    ).fetchone()
    conn.close()
    assert row["account_id"] == account_id


def test_callback_case3_verified_github_email_auto_links_email_login(db_path, monkeypatch):
    """The explicit "verified-email auto-link connecting an email login
    to an existing GitHub account with the same address" scenario: a
    person who already has an account through GitHub, whose GitHub
    identity carries a verified email, signs in later by clicking a
    magic link sent to that SAME address -- must auto-link onto the
    existing account and log in, not create a second one.
    """
    _enable_email(monkeypatch)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    account_id = cur.lastrowid
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('github', 'gh-42', ?, 'shared@example.com', 1, ?)",
        (account_id, int(time.time())),
    )
    conn.commit()
    conn.close()

    raw_token = _make_email_token(db_path, email="shared@example.com")
    client = _client()
    resp = client.get("/auth/email/callback", params={"token": raw_token, "format": "json"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"result": "auto_linked", "account_id": account_id}
    assert "mw_session" in resp.cookies  # this was a login, not just a link

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT account_id FROM account_identity WHERE provider = 'email' AND subject = 'shared@example.com'"
    ).fetchone()
    conn.close()
    assert row["account_id"] == account_id


def test_callback_case4_brand_new_address_returns_pending(db_path, monkeypatch):
    _enable_email(monkeypatch)
    raw_token = _make_email_token(db_path, email="brand-new@example.com")
    client = _client()
    resp = client.get("/auth/email/callback", params={"token": raw_token, "format": "json"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == "pending"
    assert isinstance(body["pending_token"], str) and body["pending_token"]
    assert body["email"] == "brand-new@example.com"
    assert body["email_verified"] is True  # clicking the link IS the proof
    assert "mw_session" not in resp.cookies


# ---- GET /auth/email/callback: the browser (redirect) shape -----------


def test_callback_redirect_pending_goes_to_link_with_pending_cookie(db_path, monkeypatch):
    _enable_email(monkeypatch)
    raw_token = _make_email_token(db_path, email="redirect-pending@example.com")
    client = _client()
    resp = client.get("/auth/email/callback", params={"token": raw_token}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/link"
    assert "mw_pending_token" in resp.cookies


def test_callback_redirect_login_goes_to_account_with_session_cookie(db_path, monkeypatch):
    _enable_email(monkeypatch)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    account_id = cur.lastrowid
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('email', 'redirect-login@example.com', ?, 'redirect-login@example.com', 1, ?)",
        (account_id, int(time.time())),
    )
    conn.commit()
    conn.close()

    raw_token = _make_email_token(db_path, email="redirect-login@example.com")
    client = _client()
    resp = client.get("/auth/email/callback", params={"token": raw_token}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/account"
    assert "mw_session" in resp.cookies


def test_callback_redirect_invalid_token_goes_to_join_with_auth_error(db_path, monkeypatch):
    _enable_email(monkeypatch)
    client = _client()
    resp = client.get("/auth/email/callback", params={"token": "never-issued"}, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/join?auth_error=invalid_session"
