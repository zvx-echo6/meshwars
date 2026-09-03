"""Tests for app/oauth_api.py: resolve_oauth_callback() (the callback
decision tree) at the unit level against a bare `conn`, and the full
GET /auth/{provider}/start + GET /auth/{provider}/callback +
POST /api/account/pending/{create,link} routes over real HTTP
(TestClient). Every outbound call this router makes to a provider is
intercepted by an httpx.MockTransport handler patched in for
oauth_api.httpx.AsyncClient -- there is no real network access
anywhere in this file, same posture as tests/test_oauth.py.

Two fixture shapes, same split tests/test_auth.py and
tests/test_account_api.py already use for the same reason:

- `conn` (tests/conftest.py, in-memory) for resolve_oauth_callback()
  directly -- it takes an already-open connection and is otherwise
  free of HTTP/cookies/sessions, so it needs neither TestClient nor a
  file-backed database.
- `db_path` (real file, same shape as tests/test_account_api.py's own)
  for the HTTP-level tests, since TestClient runs the ASGI app in a
  different OS thread and app/db.py's connect()/WriteSession open a
  fresh connection per call -- ":memory:" would not share data across
  that boundary, a real file does.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.oauth_api as oauth_api_module
from app.auth import http_exception_as_error_body
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.mc_ingest import hash_secret
from app.oauth import DISCORD, GITHUB, ProviderIdentity
from app.oauth_api import router as oauth_router
from app.oauth_api import resolve_oauth_callback
from app.sessions import SESSION_COOKIE_NAME, create_session


def _run(coro):
    return asyncio.run(coro)


# =========================================================================
# Part 1: resolve_oauth_callback() against a bare `conn` (conftest.py)
# =========================================================================


def _make_account(conn, *, created_at: int | None = None) -> int:
    now = created_at if created_at is not None else int(time.time())
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (now,))
    return cur.lastrowid


def _identity_row(conn, provider: str, subject: str):
    return conn.execute(
        "SELECT * FROM account_identity WHERE provider = ? AND subject = ?", (provider, subject)
    ).fetchone()


def test_case1_existing_identity_logs_in(conn):
    account_id = _make_account(conn)
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('github', 'sub-1', ?, 'old@example.com', 1, ?)",
        (account_id, 1000),
    )

    identity = ProviderIdentity(subject="sub-1", email="old@example.com", email_verified=True)
    outcome = resolve_oauth_callback(
        conn, provider_name="github", identity=identity, current_account_id=None, now=5000
    )

    assert outcome == {"case": "login", "account_id": account_id}
    row = _identity_row(conn, "github", "sub-1")
    assert row["last_login_at"] == 5000
    account_row = conn.execute("SELECT last_login_at FROM account WHERE account_id = ?", (account_id,)).fetchone()
    assert account_row["last_login_at"] == 5000


def test_case1_takes_priority_even_when_a_session_is_also_present(conn):
    """An existing (provider, subject) identity always logs in to ITS
    OWN account -- case 1 is checked before case 2, so a currently
    logged-in caller re-authenticating with an identity that already
    belongs to a DIFFERENT account must never silently re-link it onto
    whatever they happen to be logged in as right now.
    """
    owner_account = _make_account(conn)
    other_logged_in_account = _make_account(conn)
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('github', 'sub-1', ?, NULL, 0, 1000)",
        (owner_account,),
    )

    identity = ProviderIdentity(subject="sub-1", email=None, email_verified=False)
    outcome = resolve_oauth_callback(
        conn,
        provider_name="github",
        identity=identity,
        current_account_id=other_logged_in_account,
        now=5000,
    )

    assert outcome == {"case": "login", "account_id": owner_account}


def test_case2_links_new_identity_onto_the_logged_in_account(conn):
    account_id = _make_account(conn)
    identity = ProviderIdentity(subject="sub-2", email="new@example.com", email_verified=False)

    outcome = resolve_oauth_callback(
        conn, provider_name="github", identity=identity, current_account_id=account_id, now=5000
    )

    assert outcome == {"case": "linked", "account_id": account_id}
    row = _identity_row(conn, "github", "sub-2")
    assert row is not None
    assert row["account_id"] == account_id
    assert row["email"] == "new@example.com"
    assert row["email_verified"] == 0
    event = conn.execute(
        "SELECT kind, actor FROM account_link_event WHERE account_id = ?", (account_id,)
    ).fetchone()
    assert event["kind"] == "identity_linked"
    assert event["actor"] == "user"


def test_case3_verified_email_matches_exactly_one_account_auto_links_and_logs_in(conn):
    existing_account = _make_account(conn)
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('google', 'g-sub', ?, 'shared@example.com', 1, 1000)",
        (existing_account,),
    )

    identity = ProviderIdentity(subject="gh-sub", email="Shared@Example.com", email_verified=True)
    outcome = resolve_oauth_callback(
        conn, provider_name="github", identity=identity, current_account_id=None, now=5000
    )

    assert outcome == {"case": "auto_linked", "account_id": existing_account}
    row = _identity_row(conn, "github", "gh-sub")
    assert row is not None
    assert row["account_id"] == existing_account
    account_row = conn.execute(
        "SELECT last_login_at FROM account WHERE account_id = ?", (existing_account,)
    ).fetchone()
    assert account_row["last_login_at"] == 5000


def test_case3_ambiguous_match_does_not_link_falls_to_pending(conn):
    """Two different accounts each holding their own verified identity
    with the SAME email -- must NOT link to either. Falls through to
    case 4 (pending) instead.
    """
    account_a = _make_account(conn)
    account_b = _make_account(conn)
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('google', 'g-sub-a', ?, 'shared@example.com', 1, 1000)",
        (account_a,),
    )
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('discord', 'd-sub-b', ?, 'shared@example.com', 1, 1000)",
        (account_b,),
    )

    identity = ProviderIdentity(subject="gh-sub", email="shared@example.com", email_verified=True)
    outcome = resolve_oauth_callback(
        conn, provider_name="github", identity=identity, current_account_id=None, now=5000
    )

    assert outcome["case"] == "pending"
    # Neither account gained the new identity.
    assert _identity_row(conn, "github", "gh-sub") is None
    pending = conn.execute(
        "SELECT provider, subject FROM account_pending_identity WHERE token_hash = ?",
        (hash_secret(outcome["raw_token"]),),
    ).fetchone()
    assert pending["provider"] == "github"
    assert pending["subject"] == "gh-sub"


def test_case3_unverified_email_never_auto_links_even_with_a_real_match(conn):
    existing_account = _make_account(conn)
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('google', 'g-sub', ?, 'shared@example.com', 1, 1000)",
        (existing_account,),
    )

    # The NEW identity's own email_verified is False -- must not match,
    # even though an account with a verified identity at this exact
    # address exists.
    identity = ProviderIdentity(subject="gh-sub", email="shared@example.com", email_verified=False)
    outcome = resolve_oauth_callback(
        conn, provider_name="github", identity=identity, current_account_id=None, now=5000
    )

    assert outcome["case"] == "pending"
    assert _identity_row(conn, "github", "gh-sub") is None


def test_case3_ignores_an_existing_identity_whose_own_email_is_unverified(conn):
    """The EXISTING candidate account's identity must also be verified
    -- an unverified existing email at the same address is not a
    candidate match either.
    """
    existing_account = _make_account(conn)
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('google', 'g-sub', ?, 'shared@example.com', 0, 1000)",
        (existing_account,),
    )

    identity = ProviderIdentity(subject="gh-sub", email="shared@example.com", email_verified=True)
    outcome = resolve_oauth_callback(
        conn, provider_name="github", identity=identity, current_account_id=None, now=5000
    )

    assert outcome["case"] == "pending"


def test_case4_pending_identity_stored_hashed_with_correct_expiry(conn, monkeypatch):
    monkeypatch.setattr(settings, "account_pending_identity_lifetime_seconds", 900)
    identity = ProviderIdentity(subject="brand-new", email=None, email_verified=False)

    outcome = resolve_oauth_callback(
        conn, provider_name="github", identity=identity, current_account_id=None, now=5000
    )

    assert outcome["case"] == "pending"
    assert outcome["expires_at"] == 5900
    row = conn.execute(
        "SELECT token_hash, provider, subject, email, email_verified, created_at, expires_at, consumed_at "
        "  FROM account_pending_identity"
    ).fetchone()
    assert row["token_hash"] == hash_secret(outcome["raw_token"])
    assert row["token_hash"] != outcome["raw_token"]  # never stored in the clear
    assert row["provider"] == "github"
    assert row["subject"] == "brand-new"
    assert row["email"] is None
    assert row["email_verified"] == 0
    assert row["consumed_at"] is None


# =========================================================================
# Part 2: full HTTP routes (TestClient), provider HTTP mocked
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
def _reset_pending_rate_limiters():
    """oauth_api.py's two pending-redemption rate limiters are module-
    level singletons (see app/auth.py's module docstring for why every
    _BoundedHits budget in this codebase is built once, at import time)
    -- cleared before and after every test in this file, same pattern
    tests/test_account_api.py's own autouse fixture uses for its single
    limiter.
    """
    oauth_api_module._pending_create_addr_limiter._hits.clear()
    oauth_api_module._pending_link_addr_limiter._hits.clear()
    yield
    oauth_api_module._pending_create_addr_limiter._hits.clear()
    oauth_api_module._pending_link_addr_limiter._hits.clear()


def _enable_github(monkeypatch) -> None:
    monkeypatch.setattr(settings, "oauth_github_client_id", "test-client-id")
    monkeypatch.setattr(settings, "oauth_github_client_secret", "test-client-secret")
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    # TestClient's default base_url is http://testserver, plain HTTP --
    # a cookie set with the Secure attribute (the default,
    # account_session_cookie_secure=True, correct for every real
    # deployment which is always behind TLS) is never sent back by any
    # real cookie jar, httpx's included, on a plain-http request. Flip
    # it off for these tests the same way a developer running the
    # server bare over http locally would in their own .env (see that
    # setting's own comment in app/config.py) -- without this, the
    # state/PKCE-verifier cookies app/oauth_api.py sets would never
    # round-trip back to the callback route in ANY test here, and every
    # one of them would incorrectly read as "invalid or expired oauth
    # login attempt" regardless of what it's actually trying to prove.
    monkeypatch.setattr(settings, "account_session_cookie_secure", False)


def _enable_discord(monkeypatch) -> None:
    monkeypatch.setattr(settings, "oauth_discord_client_id", "test-discord-client-id")
    monkeypatch.setattr(settings, "oauth_discord_client_secret", "test-discord-client-secret")
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    # Same cookie-jar/TestClient reasoning as _enable_github above.
    monkeypatch.setattr(settings, "account_session_cookie_secure", False)


def _discord_handler(*, discord_user_id: str = "999888777666555444", email: str | None = "dev@example.com", verified: bool = True):
    """A MockTransport handler standing in for discord.com across the
    whole /start -> /callback round trip: the token exchange and the
    single userinfo call (/users/@me) -- Discord needs no second call,
    unlike GitHub's /user + /user/emails (see app/oauth.py's Discord
    section comment).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == DISCORD.token_url:
            return httpx.Response(200, json={"access_token": "discord-access-token", "token_type": "Bearer"})
        if request.url == DISCORD.userinfo_url:
            body = {"id": discord_user_id, "username": "octoduck", "global_name": "Octo Duck"}
            if email is not None:
                body["email"] = email
                body["verified"] = verified
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    return handler


def _github_handler(*, github_user_id: int = 999, primary_email: str | None = "dev@example.com", verified: bool = True):
    """A MockTransport handler standing in for github.com/api.github.com
    across the whole /start -> /callback round trip: the token exchange
    and both userinfo calls (/user, /user/emails).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == GITHUB.token_url:
            return httpx.Response(200, json={"access_token": "gh-access-token", "token_type": "bearer"})
        if request.url == GITHUB.userinfo_url:
            return httpx.Response(200, json={"id": github_user_id, "login": "octocat"})
        if request.url == "https://api.github.com/user/emails":
            if primary_email is None:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200, json=[{"email": primary_email, "primary": True, "verified": verified}]
            )
        return httpx.Response(404)

    return handler


def _patch_provider_http(monkeypatch, handler) -> None:
    """Redirects every httpx.AsyncClient the router constructs inside
    oauth_callback() through an httpx.MockTransport running `handler` --
    monkeypatches the class on oauth_api's own `httpx` module reference
    (the same module object every other importer of httpx shares, but
    monkeypatch reverts this automatically at the end of the test).
    """

    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(oauth_api_module.httpx, "AsyncClient", _MockAsyncClient)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(oauth_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    return TestClient(app)


def _start_and_get_state(client: TestClient, provider: str = "github") -> str:
    resp = client.get(f"/auth/{provider}/start", follow_redirects=False)
    assert resp.status_code == 302, resp.text
    location = httpx.URL(resp.headers["location"])
    return dict(httpx.QueryParams(location.query))["state"]


def _make_account_and_session(db_path: str) -> tuple[int, str]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    account_id = cur.lastrowid
    conn.commit()
    conn.close()
    raw_token = _run(create_session(account_id, user_agent=None, ip=None))
    return account_id, raw_token


# ---- /auth/{provider}/start -----------------------------------------------


def test_start_404_when_provider_disabled(db_path):
    client = _client()
    resp = client.get("/auth/github/start", follow_redirects=False)
    assert resp.status_code == 404
    assert resp.json() == {"error": "not found"}


def test_start_404_for_unknown_provider(db_path, monkeypatch):
    _enable_github(monkeypatch)
    client = _client()
    resp = client.get("/auth/not-a-real-provider/start", follow_redirects=False)
    assert resp.status_code == 404


def test_start_redirects_and_sets_flow_cookies(db_path, monkeypatch):
    _enable_github(monkeypatch)
    client = _client()
    resp = client.get("/auth/github/start", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"].startswith(GITHUB.authorize_url)
    assert "mw_oauth_state" in resp.cookies
    assert "mw_oauth_pkce_verifier" in resp.cookies


# ---- /auth/{provider}/callback: rejection paths ----------------------------


def test_callback_404_when_provider_disabled(db_path):
    client = _client()
    resp = client.get("/auth/github/callback", params={"code": "x", "state": "y"})
    assert resp.status_code == 404


def test_callback_state_mismatch_rejected(db_path, monkeypatch):
    _enable_github(monkeypatch)
    client = _client()
    _start_and_get_state(client)  # sets real cookies

    resp = client.get(
        "/auth/github/callback",
        params={"code": "the-code", "state": "tampered-state-value", "format": "json"},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "invalid or expired oauth login attempt"}
    # Single-use: the flow cookies are cleared even on rejection.
    assert client.cookies.get("mw_oauth_state") is None
    assert client.cookies.get("mw_oauth_pkce_verifier") is None


def test_callback_missing_pkce_verifier_cookie_rejected(db_path, monkeypatch):
    _enable_github(monkeypatch)
    client = _client()
    state = _start_and_get_state(client)
    client.cookies.delete("mw_oauth_pkce_verifier")

    resp = client.get("/auth/github/callback", params={"code": "the-code", "state": state, "format": "json"})
    assert resp.status_code == 400
    assert resp.json() == {"error": "invalid or expired oauth login attempt"}


def test_callback_missing_state_cookie_rejected(db_path, monkeypatch):
    _enable_github(monkeypatch)
    client = _client()
    state = _start_and_get_state(client)
    client.cookies.delete("mw_oauth_state")

    resp = client.get("/auth/github/callback", params={"code": "the-code", "state": state, "format": "json"})
    assert resp.status_code == 400


def test_callback_provider_error_param_rejected(db_path, monkeypatch):
    _enable_github(monkeypatch)
    client = _client()
    _start_and_get_state(client)

    resp = client.get("/auth/github/callback", params={"error": "access_denied", "format": "json"})
    assert resp.status_code == 400


def test_callback_provider_http_failure_returns_502(db_path, monkeypatch):
    _enable_github(monkeypatch)
    _patch_provider_http(monkeypatch, lambda r: httpx.Response(500))
    client = _client()
    state = _start_and_get_state(client)

    resp = client.get("/auth/github/callback", params={"code": "the-code", "state": state, "format": "json"})
    assert resp.status_code == 502


# ---- /auth/{provider}/callback: the happy paths ----------------------------


def test_callback_brand_new_identity_returns_pending(db_path, monkeypatch):
    _enable_github(monkeypatch)
    _patch_provider_http(monkeypatch, _github_handler(github_user_id=111, primary_email=None))
    client = _client()
    state = _start_and_get_state(client)

    resp = client.get("/auth/github/callback", params={"code": "the-code", "state": state, "format": "json"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == "pending"
    assert isinstance(body["pending_token"], str) and body["pending_token"]
    assert "mw_session" not in resp.cookies  # no account exists yet -- no session issued


def test_callback_existing_identity_logs_in(db_path, monkeypatch):
    _enable_github(monkeypatch)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    account_id = cur.lastrowid
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('github', '111', ?, 'dev@example.com', 1, ?)",
        (account_id, int(time.time())),
    )
    conn.commit()
    conn.close()

    _patch_provider_http(monkeypatch, _github_handler(github_user_id=111))
    client = _client()
    state = _start_and_get_state(client)

    resp = client.get("/auth/github/callback", params={"code": "the-code", "state": state, "format": "json"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"result": "login", "account_id": account_id}
    assert "mw_session" in resp.cookies


def test_callback_links_while_logged_in(db_path, monkeypatch):
    _enable_github(monkeypatch)
    account_id, raw_session = _make_account_and_session(db_path)

    _patch_provider_http(monkeypatch, _github_handler(github_user_id=222, primary_email=None))
    client = _client()
    client.cookies.set(SESSION_COOKIE_NAME, raw_session)
    state = _start_and_get_state(client)

    resp = client.get("/auth/github/callback", params={"code": "the-code", "state": state, "format": "json"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"result": "linked", "account_id": account_id}
    # No NEW session issued -- the caller already had one.
    assert "mw_session" not in resp.cookies

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT account_id FROM account_identity WHERE provider = 'github' AND subject = '222'"
    ).fetchone()
    conn.close()
    assert row["account_id"] == account_id


def test_callback_verified_email_auto_links_and_logs_in(db_path, monkeypatch):
    _enable_github(monkeypatch)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    account_id = cur.lastrowid
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('google', 'g-1', ?, 'shared@example.com', 1, ?)",
        (account_id, int(time.time())),
    )
    conn.commit()
    conn.close()

    _patch_provider_http(monkeypatch, _github_handler(github_user_id=333, primary_email="shared@example.com"))
    client = _client()
    state = _start_and_get_state(client)

    resp = client.get("/auth/github/callback", params={"code": "the-code", "state": state, "format": "json"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"result": "auto_linked", "account_id": account_id}
    assert "mw_session" in resp.cookies  # this one WAS a login, not just a link


def test_callback_ambiguous_email_match_does_not_link(db_path, monkeypatch):
    _enable_github(monkeypatch)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur_a = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    account_a = cur_a.lastrowid
    cur_b = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    account_b = cur_b.lastrowid
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('google', 'g-a', ?, 'shared@example.com', 1, ?)",
        (account_a, int(time.time())),
    )
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('discord', 'd-b', ?, 'shared@example.com', 1, ?)",
        (account_b, int(time.time())),
    )
    conn.commit()
    conn.close()

    _patch_provider_http(monkeypatch, _github_handler(github_user_id=444, primary_email="shared@example.com"))
    client = _client()
    state = _start_and_get_state(client)

    resp = client.get("/auth/github/callback", params={"code": "the-code", "state": state, "format": "json"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == "pending"


def test_callback_unverified_email_never_auto_links(db_path, monkeypatch):
    _enable_github(monkeypatch)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    account_id = cur.lastrowid
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('google', 'g-1', ?, 'shared@example.com', 1, ?)",
        (account_id, int(time.time())),
    )
    conn.commit()
    conn.close()

    # GitHub reports the email but NOT verified -- must never auto-link.
    _patch_provider_http(
        monkeypatch, _github_handler(github_user_id=555, primary_email="shared@example.com", verified=False)
    )
    client = _client()
    state = _start_and_get_state(client)

    resp = client.get("/auth/github/callback", params={"code": "the-code", "state": state, "format": "json"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == "pending"


# ---- /auth/discord/callback: same decision tree, a different provider -----
#
# The callback decision tree (resolve_oauth_callback) is provider-
# agnostic -- Part 1 above already proves that against a bare `conn`.
# These mirror the GitHub full-HTTP-round-trip tests above (brand-new
# identity -> pending, existing identity -> login) for Discord
# specifically, proving the whole /start -> /callback path (state/PKCE
# cookies, token exchange, the single userinfo call, no second call
# needed) reaches the same outcomes GitHub's does.


def test_callback_brand_new_identity_returns_pending_discord(db_path, monkeypatch):
    _enable_discord(monkeypatch)
    _patch_provider_http(monkeypatch, _discord_handler(discord_user_id="111", email=None))
    client = _client()
    state = _start_and_get_state(client, provider="discord")

    resp = client.get("/auth/discord/callback", params={"code": "the-code", "state": state, "format": "json"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == "pending"
    assert isinstance(body["pending_token"], str) and body["pending_token"]
    assert "mw_session" not in resp.cookies  # no account exists yet -- no session issued


def test_callback_existing_identity_logs_in_discord(db_path, monkeypatch):
    _enable_discord(monkeypatch)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    account_id = cur.lastrowid
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('discord', '111', ?, 'dev@example.com', 1, ?)",
        (account_id, int(time.time())),
    )
    conn.commit()
    conn.close()

    _patch_provider_http(monkeypatch, _discord_handler(discord_user_id="111"))
    client = _client()
    state = _start_and_get_state(client, provider="discord")

    resp = client.get("/auth/discord/callback", params={"code": "the-code", "state": state, "format": "json"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"result": "login", "account_id": account_id}
    assert "mw_session" in resp.cookies


def test_callback_unverified_discord_email_never_auto_links(db_path, monkeypatch):
    """Mirrors test_callback_unverified_email_never_auto_links above,
    for Discord: an account already exists with a verified email at
    the same address, but Discord itself reports verified=False for
    this sign-in -- must fall to pending, never auto-link. This is the
    exact case Step 4 of the task calls out: getting `verified` right
    here is what the account-requires-password-on-verified-email rule
    depends on.
    """
    _enable_discord(monkeypatch)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    account_id = cur.lastrowid
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('google', 'g-1', ?, 'shared@example.com', 1, ?)",
        (account_id, int(time.time())),
    )
    conn.commit()
    conn.close()

    _patch_provider_http(
        monkeypatch, _discord_handler(discord_user_id="555", email="shared@example.com", verified=False)
    )
    client = _client()
    state = _start_and_get_state(client, provider="discord")

    resp = client.get("/auth/discord/callback", params={"code": "the-code", "state": state, "format": "json"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == "pending"


# ---- POST /api/account/pending/create --------------------------------------


def _make_pending(db_path: str, *, provider="github", subject="p-1", email=None, email_verified=False, ttl=900, consumed=False):
    raw_token = "raw-pending-token-" + subject
    token_hash = hash_secret(raw_token)
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO account_pending_identity"
        "(token_hash, provider, subject, email, email_verified, created_at, expires_at, consumed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (token_hash, provider, subject, email, int(email_verified), now, now + ttl, now if consumed else None),
    )
    conn.commit()
    conn.close()
    return raw_token


def test_pending_create_happy_path(db_path):
    raw_token = _make_pending(db_path, subject="new-1", email="new1@example.com", email_verified=True)
    client = _client()

    resp = client.post("/api/account/pending/create", json={"pending_token": raw_token})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == "created"
    assert isinstance(body["account_id"], int)
    assert "mw_session" in resp.cookies

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    identity = conn.execute(
        "SELECT account_id, email, email_verified FROM account_identity WHERE provider='github' AND subject='new-1'"
    ).fetchone()
    consumed = conn.execute(
        "SELECT consumed_at FROM account_pending_identity WHERE token_hash = ?", (hash_secret(raw_token),)
    ).fetchone()
    conn.close()
    assert identity["account_id"] == body["account_id"]
    assert identity["email"] == "new1@example.com"
    assert consumed["consumed_at"] is not None


def test_pending_create_invalid_token(db_path):
    client = _client()
    resp = client.post("/api/account/pending/create", json={"pending_token": "never-issued"})
    assert resp.status_code == 403
    assert resp.json() == {"error": "invalid token"}


def test_pending_create_expired_token(db_path):
    raw_token = _make_pending(db_path, subject="expired-1", ttl=-10)
    client = _client()
    resp = client.post("/api/account/pending/create", json={"pending_token": raw_token})
    assert resp.status_code == 403
    assert resp.json() == {"error": "token expired"}


def test_pending_create_already_consumed_token(db_path):
    raw_token = _make_pending(db_path, subject="consumed-1", consumed=True)
    client = _client()
    resp = client.post("/api/account/pending/create", json={"pending_token": raw_token})
    assert resp.status_code == 403
    assert resp.json() == {"error": "token already used"}


# ---- POST /api/account/pending/link ----------------------------------------


def test_pending_link_requires_a_session(db_path):
    raw_token = _make_pending(db_path, subject="link-1")
    client = _client()
    resp = client.post("/api/account/pending/link", json={"pending_token": raw_token})
    assert resp.status_code == 401


def test_pending_link_happy_path(db_path):
    account_id, raw_session = _make_account_and_session(db_path)
    raw_token = _make_pending(db_path, subject="link-2", email="link2@example.com", email_verified=True)

    client = _client()
    client.cookies.set(SESSION_COOKIE_NAME, raw_session)
    resp = client.post("/api/account/pending/link", json={"pending_token": raw_token})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"result": "linked", "account_id": account_id}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    identity = conn.execute(
        "SELECT account_id FROM account_identity WHERE provider='github' AND subject='link-2'"
    ).fetchone()
    conn.close()
    assert identity["account_id"] == account_id


def test_pending_link_already_linked_identity_is_conflict(db_path):
    account_id, raw_session = _make_account_and_session(db_path)
    other_account_id, _ = _make_account_and_session(db_path)
    raw_token = _make_pending(db_path, subject="link-3")

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('github', 'link-3', ?, NULL, 0, ?)",
        (other_account_id, int(time.time())),
    )
    conn.commit()
    conn.close()

    client = _client()
    client.cookies.set(SESSION_COOKIE_NAME, raw_session)
    resp = client.post("/api/account/pending/link", json={"pending_token": raw_token})
    assert resp.status_code == 409


def test_pending_link_expired_token(db_path):
    _, raw_session = _make_account_and_session(db_path)
    raw_token = _make_pending(db_path, subject="link-4", ttl=-10)

    client = _client()
    client.cookies.set(SESSION_COOKIE_NAME, raw_session)
    resp = client.post("/api/account/pending/link", json={"pending_token": raw_token})
    assert resp.status_code == 403
    assert resp.json() == {"error": "token expired"}


# =========================================================================
# Part 3: GET /auth/providers
# =========================================================================


def test_list_providers_empty_when_none_configured(db_path):
    client = _client()
    resp = client.get("/auth/providers")
    assert resp.status_code == 200
    assert resp.json() == {"providers": []}


def test_list_providers_includes_github_once_enabled(db_path, monkeypatch):
    _enable_github(monkeypatch)
    client = _client()
    resp = client.get("/auth/providers")
    assert resp.status_code == 200
    assert resp.json() == {"providers": [{"name": "github", "label": "GitHub"}]}


def test_list_providers_omits_a_half_configured_provider(db_path, monkeypatch):
    # client_id set, secret blank -- provider_enabled() treats this as
    # fully off (app/oauth.py), never as "trust an empty secret."
    monkeypatch.setattr(settings, "oauth_github_client_id", "test-client-id")
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    client = _client()
    resp = client.get("/auth/providers")
    assert resp.status_code == 200
    assert resp.json() == {"providers": []}


def test_list_providers_includes_discord_once_enabled(db_path, monkeypatch):
    _enable_discord(monkeypatch)
    client = _client()
    resp = client.get("/auth/providers")
    assert resp.status_code == 200
    assert resp.json() == {"providers": [{"name": "discord", "label": "Discord"}]}


def test_list_providers_omits_discord_when_not_configured(db_path, monkeypatch):
    # Only GitHub configured -- Discord must not appear until its own
    # client id/secret are set, mirroring the half-configured test
    # above but for a provider that is entirely untouched.
    _enable_github(monkeypatch)
    client = _client()
    resp = client.get("/auth/providers")
    assert resp.status_code == 200
    assert resp.json() == {"providers": [{"name": "github", "label": "GitHub"}]}


# =========================================================================
# Part 4: GET /auth/{provider}/callback -- the default BROWSER (redirect)
# shape, as opposed to Part 2's `?format=json` coverage of the same
# decision tree above. Same fixtures/helpers as Part 2; every request
# here is made WITHOUT format=json and WITH follow_redirects=False, so
# each assertion is against the raw 302 this route now sends a real
# browser by default -- see oauth_callback's own docstring for the full
# case -> destination mapping.
# =========================================================================


def test_callback_redirect_login_goes_to_account_with_session_cookie(db_path, monkeypatch):
    _enable_github(monkeypatch)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    account_id = cur.lastrowid
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('github', '111', ?, 'dev@example.com', 1, ?)",
        (account_id, int(time.time())),
    )
    conn.commit()
    conn.close()

    _patch_provider_http(monkeypatch, _github_handler(github_user_id=111))
    client = _client()
    state = _start_and_get_state(client)

    resp = client.get(
        "/auth/github/callback", params={"code": "the-code", "state": state}, follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/account"
    assert "mw_session" in resp.cookies
    # Single-use flow cookies are still cleared on this path.
    assert client.cookies.get("mw_oauth_state") is None


def test_callback_redirect_linked_goes_to_account_no_new_session(db_path, monkeypatch):
    _enable_github(monkeypatch)
    account_id, raw_session = _make_account_and_session(db_path)

    _patch_provider_http(monkeypatch, _github_handler(github_user_id=222, primary_email=None))
    client = _client()
    client.cookies.set(SESSION_COOKIE_NAME, raw_session)
    state = _start_and_get_state(client)

    resp = client.get(
        "/auth/github/callback", params={"code": "the-code", "state": state}, follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/account"
    assert "mw_session" not in resp.cookies  # already had one -- see oauth_callback's own comment


def test_callback_redirect_pending_goes_to_link_with_pending_cookie(db_path, monkeypatch):
    _enable_github(monkeypatch)
    _patch_provider_http(monkeypatch, _github_handler(github_user_id=999, primary_email=None))
    client = _client()
    state = _start_and_get_state(client)

    resp = client.get(
        "/auth/github/callback", params={"code": "the-code", "state": state}, follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/link"
    assert "mw_session" not in resp.cookies  # no account exists yet
    assert "mw_pending_token" in resp.cookies
    # The raw token is never on the URL or in a JSON body here -- only in
    # the cookie. Confirm it actually redeems (proves it's a real,
    # freshly-issued pending token, not a placeholder).
    raw_token = resp.cookies["mw_pending_token"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT provider, subject FROM account_pending_identity WHERE token_hash = ?",
        (hash_secret(raw_token),),
    ).fetchone()
    conn.close()
    assert row["provider"] == "github"
    assert row["subject"] == "999"


def test_callback_redirect_error_goes_to_join_with_auth_error(db_path, monkeypatch):
    _enable_github(monkeypatch)
    client = _client()
    _start_and_get_state(client)

    resp = client.get(
        "/auth/github/callback", params={"error": "access_denied"}, follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/join?auth_error=provider_declined"


def test_callback_redirect_state_mismatch_goes_to_join_with_auth_error(db_path, monkeypatch):
    _enable_github(monkeypatch)
    client = _client()
    _start_and_get_state(client)

    resp = client.get(
        "/auth/github/callback",
        params={"code": "the-code", "state": "tampered-state-value"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/join?auth_error=invalid_session"


def test_callback_redirect_provider_http_failure_goes_to_join_with_auth_error(db_path, monkeypatch):
    _enable_github(monkeypatch)
    _patch_provider_http(monkeypatch, lambda r: httpx.Response(500))
    client = _client()
    state = _start_and_get_state(client)

    resp = client.get(
        "/auth/github/callback", params={"code": "the-code", "state": state}, follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/join?auth_error=provider_error"


# =========================================================================
# Part 5: the pending token as a cookie -- GET /api/account/pending, and
# POST /api/account/pending/{create,link} redeeming a COOKIE-carried
# token rather than a JSON body (Part 2 above already covers the body
# fallback exhaustively).
# =========================================================================


def test_pending_get_describes_a_valid_pending_cookie(db_path):
    raw_token = _make_pending(db_path, subject="peek-1", email="peek1@example.com", email_verified=True)
    client = _client()
    client.cookies.set("mw_pending_token", raw_token)

    resp = client.get("/api/account/pending")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "github"
    assert body["provider_label"] == "GitHub"
    assert body["email"] == "p***@example.com"  # masked, never the raw address
    assert body["email_verified"] is True


def test_pending_get_no_cookie_is_404(db_path):
    client = _client()
    resp = client.get("/api/account/pending")
    assert resp.status_code == 404


def test_pending_get_expired_cookie_is_404(db_path):
    raw_token = _make_pending(db_path, subject="peek-2", ttl=-10)
    client = _client()
    client.cookies.set("mw_pending_token", raw_token)
    resp = client.get("/api/account/pending")
    assert resp.status_code == 404


def test_pending_create_via_cookie_redeems_and_clears_cookie(db_path):
    raw_token = _make_pending(db_path, subject="cookie-create-1", email="cc1@example.com", email_verified=True)
    client = _client()
    client.cookies.set("mw_pending_token", raw_token)

    # No JSON body at all -- a real browser POST from frontend/link.js
    # relies entirely on the cookie.
    resp = client.post("/api/account/pending/create")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == "created"
    assert "mw_session" in resp.cookies

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    identity = conn.execute(
        "SELECT account_id FROM account_identity WHERE provider='github' AND subject='cookie-create-1'"
    ).fetchone()
    conn.close()
    assert identity["account_id"] == body["account_id"]


def test_pending_link_via_cookie_redeems(db_path):
    account_id, raw_session = _make_account_and_session(db_path)
    raw_token = _make_pending(db_path, subject="cookie-link-1", email="cl1@example.com", email_verified=True)

    client = _client()
    client.cookies.set(SESSION_COOKIE_NAME, raw_session)
    client.cookies.set("mw_pending_token", raw_token)

    resp = client.post("/api/account/pending/link")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"result": "linked", "account_id": account_id}
