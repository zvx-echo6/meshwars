"""Tests for TOTP two-factor authentication: app/totp.py's pure RFC
6238 math/encryption/recovery-code generation, app/totp_api.py's
enroll/activate/disable/verify routes and replay guard, and the
integration points in app/oauth_api.py (POST /auth/password/start,
GET /auth/email/callback) that hand off to a second-factor challenge
instead of a session -- and, just as important, confirm OAuth sign-in
(app/oauth_api.py's oauth_callback()) never does.

Same fixture shapes tests/test_account_security.py and
tests/test_email_login.py already use, for the same reasons: a real
file-backed sqlite database (TestClient runs the ASGI app in a
different OS thread; app/db.py's connect()/WriteSession open a fresh
connection per call, so ":memory:" would not share data across that
boundary) for HTTP-level tests, and the in-memory `conn` fixture
(tests/conftest.py) for tests that only need a bare connection.
"""
from __future__ import annotations

import asyncio
import base64
import sqlite3
import time

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.db as db
import app.oauth_api as oauth_api_module
import app.totp_api as totp_api_module
from app.account_api import router as account_router
from app.auth import http_exception_as_error_body
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.mc_ingest import hash_secret
from app.oauth import ProviderIdentity
from app.oauth_api import router as oauth_router, _respond_to_callback_outcome, resolve_oauth_callback
from app.password_login import hash_password
from app.sessions import SESSION_COOKIE_NAME, create_session
from app.totp import (
    DEFAULT_SKEW_STEPS,
    TotpEncryptionUnavailable,
    decrypt_secret,
    encrypt_secret,
    generate_recovery_code,
    generate_recovery_codes,
    generate_secret,
    provisioning_uri,
    secret_to_base32,
    totp_encryption_available,
    totp_code_at,
    verify_totp_code,
)
from app.totp_api import (
    router as totp_router,
    verify_and_consume_recovery_code,
    verify_and_consume_totp_code,
)


def _run(coro):
    return asyncio.run(coro)


# =========================================================================
# Part 0: app/totp.py -- pure RFC 6238 math, encryption, recovery codes
# =========================================================================

# RFC 6238 Appendix B's own published test vectors (SHA1 mode), using
# that appendix's own 20-byte ASCII seed "12345678901234567890". The
# appendix's vectors are 8-digit codes; this app always produces 6
# (app/totp.py's _DIGITS) -- RFC 4226's own truncation
# (`truncated_value MOD 10^Digits`) guarantees the last 6 digits of an
# 8-digit vector equal what a 6-digit truncation of the SAME truncated
# integer would produce, so each vector below is compared against the
# last 6 digits of its published 8-digit code.
_RFC6238_SECRET = b"12345678901234567890"
_RFC6238_VECTORS = [
    (59, "287082"),
    (1111111109, "081804"),
    (1111111111, "050471"),
    (1234567890, "005924"),
    (2000000000, "279037"),
    (20000000000, "353130"),
]


@pytest.mark.parametrize("when,expected", _RFC6238_VECTORS)
def test_totp_code_matches_rfc6238_appendix_b_vectors(when, expected):
    assert totp_code_at(_RFC6238_SECRET, when=when) == expected


@pytest.mark.parametrize("when,expected", _RFC6238_VECTORS)
def test_verify_totp_code_accepts_the_exact_rfc6238_vector(when, expected):
    assert verify_totp_code(_RFC6238_SECRET, expected, now=when, skew_steps=0) is True


def test_verify_totp_code_rejects_a_wrong_code():
    secret = generate_secret()
    now = 1_700_000_000
    real = totp_code_at(secret, when=now)
    wrong = "000000" if real != "000000" else "111111"
    assert verify_totp_code(secret, wrong, now=now) is False


def test_verify_totp_code_rejects_malformed_input():
    secret = generate_secret()
    now = 1_700_000_000
    for bad in ["", "12345", "1234567", "abcdef", None]:
        assert verify_totp_code(secret, bad, now=now) is False


# ---- skew window: adjacent steps accepted, distant steps rejected -----

def test_skew_window_accepts_the_immediately_adjacent_step_either_side():
    """DEFAULT_SKEW_STEPS = 1 -- a code generated for the step just
    before or just after "now" must still verify, covering ordinary
    clock drift and the few seconds it takes a person to type a code
    in. See app/totp.py's own "clock skew" docstring section.
    """
    secret = generate_secret()
    # Snapped to an exact 30s step boundary so +/-30 lands cleanly on
    # the adjacent steps, not a fraction of one.
    now = (1_700_000_000 // 30) * 30

    code_prev = totp_code_at(secret, when=now - 30)
    code_now = totp_code_at(secret, when=now)
    code_next = totp_code_at(secret, when=now + 30)

    assert verify_totp_code(secret, code_prev, now=now) is True
    assert verify_totp_code(secret, code_now, now=now) is True
    assert verify_totp_code(secret, code_next, now=now) is True


def test_skew_window_rejects_a_distant_step():
    """A code from two steps away (60s) is already outside
    DEFAULT_SKEW_STEPS = 1, and a code from far in the past/future must
    never verify -- unbounded skew would turn a 6-digit code into a
    much larger effective guessing window.
    """
    secret = generate_secret()
    now = 1_700_000_000
    far_future_code = totp_code_at(secret, when=now + 300)  # 10 steps away
    far_past_code = totp_code_at(secret, when=now - 300)
    assert verify_totp_code(secret, far_future_code, now=now) is False
    assert verify_totp_code(secret, far_past_code, now=now) is False

    # Exactly two steps away (just past the default window) is also
    # rejected -- confirms the boundary is where DEFAULT_SKEW_STEPS
    # says it is, not accidentally wider.
    two_steps_code = totp_code_at(secret, when=now + 2 * 30)
    assert verify_totp_code(secret, two_steps_code, now=now, skew_steps=DEFAULT_SKEW_STEPS) is False


# ---- secret-at-rest encryption -----------------------------------------

def test_encrypt_decrypt_round_trip(monkeypatch):
    monkeypatch.setattr(settings, "account_totp_encryption_key", Fernet.generate_key().decode())
    secret = generate_secret()
    token = encrypt_secret(secret)
    assert decrypt_secret(token) == secret


def test_encryption_unavailable_when_key_unset(monkeypatch):
    monkeypatch.setattr(settings, "account_totp_encryption_key", "")
    assert totp_encryption_available() is False
    with pytest.raises(TotpEncryptionUnavailable):
        encrypt_secret(generate_secret())


def test_encryption_unavailable_when_key_is_malformed(monkeypatch):
    monkeypatch.setattr(settings, "account_totp_encryption_key", "not-a-valid-fernet-key")
    assert totp_encryption_available() is False
    with pytest.raises(TotpEncryptionUnavailable):
        encrypt_secret(generate_secret())


def test_decrypt_fails_closed_on_wrong_key(monkeypatch):
    monkeypatch.setattr(settings, "account_totp_encryption_key", Fernet.generate_key().decode())
    token = encrypt_secret(generate_secret())
    monkeypatch.setattr(settings, "account_totp_encryption_key", Fernet.generate_key().decode())
    with pytest.raises(TotpEncryptionUnavailable):
        decrypt_secret(token)


# ---- provisioning URI ---------------------------------------------------

def test_provisioning_uri_shape():
    secret = generate_secret()
    uri = provisioning_uri(secret=secret, account_label="dev@example.com", issuer="MeshWars")
    assert uri.startswith("otpauth://totp/")
    assert "issuer=MeshWars" in uri
    assert "algorithm=SHA1" in uri
    assert "digits=6" in uri
    assert "period=30" in uri
    assert secret_to_base32(secret) in uri


def test_provisioning_uri_label_separator_is_a_literal_colon():
    """Ente Auth refused a QR whose label carried a percent-encoded
    separator. Google's Key URI grammar permits both ":" and "%3A", but
    every canonical example and every mainstream authenticator emits the
    literal form, and a parser that recovers the issuer by splitting on
    ":" reads a %3A-encoded label as one long account name with no
    issuer at all. The original shape test asserted every query
    parameter and never the separator, which is exactly how this
    shipped -- so it is pinned here.
    """
    secret = generate_secret()
    uri = provisioning_uri(secret=secret, account_label="dev@example.com", issuer="MeshWars")
    assert uri.startswith("otpauth://totp/MeshWars:dev@example.com?"), uri
    assert "%3A" not in uri
    assert "%40" not in uri


def test_provisioning_uri_still_escapes_what_genuinely_needs_it():
    """Keeping ":" and "@" literal must not turn off escaping wholesale
    -- a space in an issuer or account still has to be encoded, or the
    URI breaks at the first whitespace.
    """
    secret = generate_secret()
    uri = provisioning_uri(secret=secret, account_label="a b@c.co", issuer="Mesh Wars")
    label = uri.split("otpauth://totp/", 1)[1].split("?", 1)[0]
    assert " " not in label
    assert label == "Mesh%20Wars:a%20b@c.co", label


# ---- recovery codes -------------------------------------------------------

def test_generate_recovery_codes_count_and_shape():
    codes = generate_recovery_codes(10)
    assert len(codes) == 10
    assert len(set(codes)) == 10  # no accidental duplicates
    for c in codes:
        assert len(c) == 10
        assert c == c.upper()
        # No visually-ambiguous characters (0/O, 1/I) -- see
        # app/totp.py's own _RECOVERY_CODE_ALPHABET comment (L IS in
        # the real alphabet; only 0/1/I/O are dropped).
        assert not any(ch in c for ch in "01IO")


def test_generate_recovery_code_uses_only_the_documented_alphabet():
    from app.totp import _RECOVERY_CODE_ALPHABET
    for _ in range(50):
        code = generate_recovery_code()
        assert all(ch in _RECOVERY_CODE_ALPHABET for ch in code)


# =========================================================================
# Part 1: app/totp_api.py's verify/consume helpers against a bare `conn`
# (tests/conftest.py's in-memory fixture)
# =========================================================================


def _make_account(conn) -> int:
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    return cur.lastrowid


def _make_pending_totp(conn, account_id: int, *, secret: bytes | None = None) -> bytes:
    secret = secret or generate_secret()
    now = int(time.time())
    conn.execute(
        "INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at, last_used_step) "
        "VALUES (?, ?, ?, NULL, NULL)",
        (account_id, encrypt_secret(secret), now),
    )
    return secret


def _activate_totp(conn, account_id: int, *, secret: bytes | None = None) -> bytes:
    secret = secret or _make_pending_totp(conn, account_id)
    conn.execute(
        "UPDATE account_totp SET activated_at = ? WHERE account_id = ?", (int(time.time()), account_id)
    )
    return secret


@pytest.fixture(autouse=True)
def _totp_encryption_key(monkeypatch):
    """Every test in this file that touches an encrypted secret needs a
    real Fernet key configured -- set once, autouse, so individual
    tests don't have to remember it. Tests that specifically exercise
    the UNSET case (fail-closed) override it back to "" themselves.
    """
    monkeypatch.setattr(settings, "account_totp_encryption_key", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def _plain_http_cookies(monkeypatch):
    """TestClient's default base_url is http://testserver -- plain
    HTTP, not HTTPS -- so a cookie set with Secure=True (the default,
    settings.account_session_cookie_secure) is silently dropped by the
    client's own cookie jar the moment it arrives via a Set-Cookie
    response header, never mind resent on a later request. This bites
    BOTH cookies this file exercises (mw_session and this feature's own
    mw_totp_challenge) whenever a test relies on the jar carrying one
    across two separate requests, rather than manually seeding it
    (tests/test_account_security.py's own _login() helper sidesteps
    this the same way, by injecting the session cookie directly into
    the jar instead of receiving it over a response). Same fix
    tests/test_email_login.py's own _enable_email() applies for the
    identical reason.
    """
    monkeypatch.setattr(settings, "account_session_cookie_secure", False)


def test_verify_and_consume_totp_code_accepts_a_pending_secret_code(conn):
    """POST /api/account/totp/activate's own use case: a code must
    verify against a still-PENDING (activated_at IS NULL) row, since
    proving the code is exactly what promotes it to active -- see
    verify_and_consume_totp_code's own docstring for why this function
    does not itself gate on activation state.
    """
    account_id = _make_account(conn)
    secret = _make_pending_totp(conn, account_id)
    now = int(time.time())
    code = totp_code_at(secret, when=now)
    assert verify_and_consume_totp_code(conn, account_id=account_id, code=code, now=now) is True


def test_verify_and_consume_totp_code_rejects_wrong_code(conn):
    account_id = _make_account(conn)
    secret = _activate_totp(conn, account_id)
    now = int(time.time())
    real = totp_code_at(secret, when=now)
    wrong = "000000" if real != "000000" else "111111"
    assert verify_and_consume_totp_code(conn, account_id=account_id, code=wrong, now=now) is False


def test_verify_and_consume_totp_code_rejects_replay_within_the_same_step(conn):
    """The design decision this task calls for: a code, once accepted,
    can never verify again -- not just at the identical step, but at
    any step at or before the one it was accepted at (see
    account_totp.last_used_step's own comment in app/db.py). This is
    deliberately STRICTER than plain RFC 6238 (which has no opinion on
    reuse at all): a stolen-in-transit code is a real threat this
    closes off at zero cost to a legitimate user, since steps only
    move forward in normal use.
    """
    account_id = _make_account(conn)
    secret = _activate_totp(conn, account_id)
    now = int(time.time())
    code = totp_code_at(secret, when=now)

    assert verify_and_consume_totp_code(conn, account_id=account_id, code=code, now=now) is True
    # Same code, same instant, submitted again -- rejected.
    assert verify_and_consume_totp_code(conn, account_id=account_id, code=code, now=now) is False
    # Same code, submitted moments later but still the identical step
    # (well within 30s) -- still rejected.
    assert verify_and_consume_totp_code(conn, account_id=account_id, code=code, now=now + 5) is False


def test_verify_and_consume_totp_code_accepts_a_later_step_after_a_replay_rejection(conn):
    account_id = _make_account(conn)
    secret = _activate_totp(conn, account_id)
    now = int(time.time())
    code_step0 = totp_code_at(secret, when=now)
    assert verify_and_consume_totp_code(conn, account_id=account_id, code=code_step0, now=now) is True

    later = now + 90  # several steps forward, comfortably past last_used_step
    code_later = totp_code_at(secret, when=later)
    assert verify_and_consume_totp_code(conn, account_id=account_id, code=code_later, now=later) is True


def test_verify_and_consume_totp_code_no_row_returns_false(conn):
    account_id = _make_account(conn)
    assert verify_and_consume_totp_code(conn, account_id=account_id, code="123456", now=int(time.time())) is False


def test_verify_and_consume_recovery_code_works_once_then_fails(conn):
    account_id = _make_account(conn)
    now = int(time.time())
    conn.execute(
        "INSERT INTO account_totp_recovery_code(account_id, code_hash, created_at) VALUES (?, ?, ?)",
        (account_id, hash_secret("ABCD234567"), now),
    )
    assert verify_and_consume_recovery_code(conn, account_id=account_id, raw_code="ABCD234567", now=now) is True
    # Second use of the SAME code fails -- single-use.
    assert verify_and_consume_recovery_code(conn, account_id=account_id, raw_code="ABCD234567", now=now) is False


def test_verify_and_consume_recovery_code_is_case_insensitive(conn):
    account_id = _make_account(conn)
    now = int(time.time())
    conn.execute(
        "INSERT INTO account_totp_recovery_code(account_id, code_hash, created_at) VALUES (?, ?, ?)",
        (account_id, hash_secret("ABCD234567"), now),
    )
    assert verify_and_consume_recovery_code(conn, account_id=account_id, raw_code="abcd234567", now=now) is True


def test_verify_and_consume_recovery_code_unknown_code_fails(conn):
    account_id = _make_account(conn)
    assert verify_and_consume_recovery_code(
        conn, account_id=account_id, raw_code="NOSUCHCODE", now=int(time.time())
    ) is False


# =========================================================================
# Part 2: HTTP routes -- enroll / activate / disable / status
# =========================================================================


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
def _reset_rate_limiters():
    """Every limiter this file exercises is a module-level singleton --
    see app/auth.py's own module docstring for why -- cleared before
    and after every test, same pattern every other test file in this
    suite already uses.
    """
    limiters = [
        totp_api_module._activate_account_limiter,
        totp_api_module._disable_account_limiter,
        totp_api_module._verify_ip_limiter,
        totp_api_module._verify_challenge_limiter,
        oauth_api_module._password_start_ip_limiter,
        oauth_api_module._password_start_addr_limiter,
        oauth_api_module._email_start_ip_limiter,
        oauth_api_module._email_start_addr_limiter,
    ]
    for lim in limiters:
        lim._hits.clear()
    oauth_api_module._password_backoff._state.clear()
    yield
    for lim in limiters:
        lim._hits.clear()
    oauth_api_module._password_backoff._state.clear()


@pytest.fixture(autouse=True)
def _cheap_scrypt(monkeypatch):
    monkeypatch.setattr(settings, "account_password_scrypt_n", 2 ** 12)


@pytest.fixture
def client(db_path):
    app = FastAPI()
    app.include_router(account_router)
    app.include_router(oauth_router)
    app.include_router(totp_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    return TestClient(app)


def _make_account_http(path: str) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    conn.commit()
    account_id = cur.lastrowid
    conn.close()
    return account_id


def _add_identity(path: str, account_id: int, *, provider="github", subject=None,
                   email=None, email_verified=1) -> None:
    subject = subject or email or f"{provider}-{account_id}"
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (provider, subject, account_id, email, email_verified, int(time.time())),
    )
    conn.commit()
    conn.close()


def _set_password(path: str, account_id: int, raw_password: str) -> None:
    hashed = hash_password(raw_password, n=2 ** 12, r=8, p=1, dklen=32)
    now = int(time.time())
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO account_password(account_id, salt, n, r, p, dklen, hash, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (account_id, hashed.salt, hashed.n, hashed.r, hashed.p, hashed.dklen, hashed.derived_key, now, now),
    )
    conn.commit()
    conn.close()


def _login(client: TestClient, account_id: int) -> str:
    raw_token = _run(create_session(account_id, device_label="Firefox on Windows"))
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return raw_token


def _b32_to_bytes(b32: str) -> bytes:
    padding = "=" * (-len(b32) % 8)
    return base64.b32decode(b32 + padding)


def _enroll_and_activate(client: TestClient) -> tuple[bytes, list[str]]:
    """Drives the real HTTP enroll -> activate flow and returns
    (raw_secret_bytes, recovery_codes) -- the shared setup every route
    test below that needs an already-ACTIVE second factor uses, so
    each test exercises the real activation path rather than seeding
    account_totp by hand.
    """
    r = client.post("/api/account/totp/enroll")
    assert r.status_code == 200, r.text
    secret = _b32_to_bytes(r.json()["secret"])
    code = totp_code_at(secret, when=int(time.time()))
    r2 = client.post("/api/account/totp/activate", json={"code": code})
    assert r2.status_code == 200, r2.text
    return secret, r2.json()["recovery_codes"]


def _seed_active_totp(db_path: str, account_id: int, *, recovery_code_count: int | None = None) -> tuple[bytes, list[str]]:
    """Writes an already-ACTIVE account_totp row (and a fresh batch of
    recovery codes) directly, bypassing the real POST .../enroll +
    .../activate HTTP round trip -- for tests in Part 3/4 below that
    need a working second factor as SETUP, not as the thing under
    test (that HTTP flow has its own dedicated tests in Part 2 above).
    last_used_step is left NULL, so a code computed immediately after
    calling this is guaranteed fresh -- no replay-guard collision with
    whatever step a real activate() call would have just consumed, and
    no real time.sleep() needed to dodge one either.
    """
    recovery_code_count = recovery_code_count or settings.account_totp_recovery_code_count
    secret = generate_secret()
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at, last_used_step) "
        "VALUES (?, ?, ?, ?, NULL)",
        (account_id, encrypt_secret(secret), now, now),
    )
    plain_codes = generate_recovery_codes(recovery_code_count)
    conn.executemany(
        "INSERT INTO account_totp_recovery_code(account_id, code_hash, created_at) VALUES (?, ?, ?)",
        [(account_id, hash_secret(c), now) for c in plain_codes],
    )
    conn.commit()
    conn.close()
    return secret, plain_codes


# ---- enrollment -----------------------------------------------------------


def test_enroll_requires_a_session(client):
    resp = client.post("/api/account/totp/enroll")
    assert resp.status_code == 401


def test_enroll_unavailable_when_encryption_key_is_unset(client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "account_totp_encryption_key", "")
    account_id = _make_account_http(db_path)
    _login(client, account_id)
    resp = client.post("/api/account/totp/enroll")
    assert resp.status_code == 404


def test_enroll_returns_secret_uri_and_inline_svg(client, db_path):
    account_id = _make_account_http(db_path)
    _login(client, account_id)
    resp = client.post("/api/account/totp/enroll")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["secret"], str) and len(body["secret"]) > 0
    assert body["otpauth_uri"].startswith("otpauth://totp/")
    assert body["qr_svg"].startswith("<svg")


def test_enroll_stores_a_pending_not_yet_active_row(client, db_path):
    account_id = _make_account_http(db_path)
    _login(client, account_id)
    client.post("/api/account/totp/enroll")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT activated_at FROM account_totp WHERE account_id = ?", (account_id,)).fetchone()
    conn.close()
    assert row["activated_at"] is None

    # A pending secret must not yet guard sign-in.
    status = client.get("/api/account").json()["totp"]
    assert status["enabled"] is False


def test_re_enrolling_while_pending_replaces_the_secret(client, db_path):
    account_id = _make_account_http(db_path)
    _login(client, account_id)
    r1 = client.post("/api/account/totp/enroll")
    r2 = client.post("/api/account/totp/enroll")
    assert r1.json()["secret"] != r2.json()["secret"]
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM account_totp WHERE account_id = ?", (account_id,)).fetchone()[0]
    conn.close()
    assert n == 1  # still exactly one row, not two


def test_enroll_refused_when_already_active(client, db_path):
    account_id = _make_account_http(db_path)
    _login(client, account_id)
    _enroll_and_activate(client)
    resp = client.post("/api/account/totp/enroll")
    assert resp.status_code == 409


# ---- activation -------------------------------------------------------


def test_activate_requires_a_pending_enrollment(client, db_path):
    account_id = _make_account_http(db_path)
    _login(client, account_id)
    resp = client.post("/api/account/totp/activate", json={"code": "123456"})
    assert resp.status_code == 404


def test_activate_rejects_a_wrong_code_and_does_not_activate(client, db_path):
    account_id = _make_account_http(db_path)
    _login(client, account_id)
    client.post("/api/account/totp/enroll")
    resp = client.post("/api/account/totp/activate", json={"code": "000000"})
    assert resp.status_code in (400, 401)
    status = client.get("/api/account").json()["totp"]
    assert status["enabled"] is False


def test_activate_success_returns_the_configured_recovery_code_count(client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "account_totp_recovery_code_count", 10)
    account_id = _make_account_http(db_path)
    _login(client, account_id)
    _, recovery_codes = _enroll_and_activate(client)
    assert len(recovery_codes) == 10
    assert len(set(recovery_codes)) == 10


def test_activate_flips_enabled_true_in_get_account(client, db_path):
    account_id = _make_account_http(db_path)
    _login(client, account_id)
    _enroll_and_activate(client)
    status = client.get("/api/account").json()["totp"]
    assert status["enabled"] is True
    assert status["recovery_codes_remaining"] == settings.account_totp_recovery_code_count


def test_activate_refused_when_already_active(client, db_path):
    account_id = _make_account_http(db_path)
    _login(client, account_id)
    secret, _ = _enroll_and_activate(client)
    code = totp_code_at(secret, when=int(time.time()))
    resp = client.post("/api/account/totp/activate", json={"code": code})
    assert resp.status_code == 409


def test_activate_rate_limited(client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "account_totp_activate_rate_limit_attempts", 1)
    monkeypatch.setattr(settings, "account_totp_activate_rate_limit_window_seconds", 60)
    account_id = _make_account_http(db_path)
    _login(client, account_id)
    client.post("/api/account/totp/enroll")
    r1 = client.post("/api/account/totp/activate", json={"code": "000000"})
    assert r1.status_code in (400, 401)
    r2 = client.post("/api/account/totp/activate", json={"code": "000000"})
    assert r2.status_code == 429


# ---- disabling -----------------------------------------------------------


def test_disable_requires_a_code_or_recovery_code(client, db_path):
    account_id = _make_account_http(db_path)
    _login(client, account_id)
    _enroll_and_activate(client)
    resp = client.request("DELETE", "/api/account/totp", json={})
    assert resp.status_code == 400


def test_disable_rejects_a_wrong_code(client, db_path):
    account_id = _make_account_http(db_path)
    _login(client, account_id)
    _enroll_and_activate(client)
    resp = client.request("DELETE", "/api/account/totp", json={"code": "000000"})
    assert resp.status_code == 401
    assert client.get("/api/account").json()["totp"]["enabled"] is True


def test_disable_with_a_valid_code_removes_totp_and_its_recovery_codes(client, db_path):
    account_id = _make_account_http(db_path)
    _login(client, account_id)
    secret, _ = _seed_active_totp(db_path, account_id)
    code = totp_code_at(secret, when=int(time.time()))
    resp = client.request("DELETE", "/api/account/totp", json={"code": code})
    assert resp.status_code == 200
    assert client.get("/api/account").json()["totp"]["enabled"] is False

    conn = sqlite3.connect(db_path)
    totp_rows = conn.execute("SELECT COUNT(*) FROM account_totp WHERE account_id = ?", (account_id,)).fetchone()[0]
    recovery_rows = conn.execute(
        "SELECT COUNT(*) FROM account_totp_recovery_code WHERE account_id = ?", (account_id,)
    ).fetchone()[0]
    conn.close()
    assert totp_rows == 0
    assert recovery_rows == 0


def test_disable_with_a_valid_recovery_code_also_works(client, db_path):
    account_id = _make_account_http(db_path)
    _login(client, account_id)
    _, recovery_codes = _enroll_and_activate(client)
    resp = client.request("DELETE", "/api/account/totp", json={"recovery_code": recovery_codes[0]})
    assert resp.status_code == 200
    assert client.get("/api/account").json()["totp"]["enabled"] is False


def test_disable_requires_totp_to_be_enabled(client, db_path):
    account_id = _make_account_http(db_path)
    _login(client, account_id)
    resp = client.request("DELETE", "/api/account/totp", json={"code": "123456"})
    assert resp.status_code == 404


def test_disable_rate_limited(client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "account_totp_disable_rate_limit_attempts", 1)
    monkeypatch.setattr(settings, "account_totp_disable_rate_limit_window_seconds", 60)
    account_id = _make_account_http(db_path)
    _login(client, account_id)
    _enroll_and_activate(client)
    r1 = client.request("DELETE", "/api/account/totp", json={"code": "000000"})
    assert r1.status_code == 401
    r2 = client.request("DELETE", "/api/account/totp", json={"code": "000000"})
    assert r2.status_code == 429


# =========================================================================
# Part 3: the intermediate challenge state cannot itself be a session
# =========================================================================


def test_password_start_totp_required_never_sets_a_session_cookie(client, db_path):
    account_id = _make_account_http(db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)
    _set_password(db_path, account_id, "a-real-password")
    _login(client, account_id)
    _enroll_and_activate(client)
    client.cookies.delete(SESSION_COOKIE_NAME)

    resp = client.post("/auth/password/start", json={"email": "dev@example.com", "password": "a-real-password"})
    assert resp.status_code == 200
    assert resp.json()["result"] == "totp_required"
    assert SESSION_COOKIE_NAME not in resp.cookies
    assert "mw_totp_challenge" in resp.cookies


def test_challenge_cookie_alone_does_not_authenticate_get_account(client, db_path):
    """The core "intermediate state must not itself be usable as a
    session" property: holding ONLY the totp challenge cookie (no
    mw_session) must never satisfy require_session() -- GET
    /api/account, or any other session-gated route, must still 401.
    """
    account_id = _make_account_http(db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)
    _set_password(db_path, account_id, "a-real-password")
    _login(client, account_id)
    _enroll_and_activate(client)
    client.cookies.delete(SESSION_COOKIE_NAME)

    resp = client.post("/auth/password/start", json={"email": "dev@example.com", "password": "a-real-password"})
    assert "mw_totp_challenge" in resp.cookies
    # The client now carries ONLY the challenge cookie -- no session.
    assert SESSION_COOKIE_NAME not in client.cookies

    account_resp = client.get("/api/account")
    assert account_resp.status_code == 401


def test_verify_completes_sign_in_with_a_valid_code(client, db_path):
    account_id = _make_account_http(db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)
    _set_password(db_path, account_id, "a-real-password")
    secret, _ = _seed_active_totp(db_path, account_id)

    client.post("/auth/password/start", json={"email": "dev@example.com", "password": "a-real-password"})

    code = totp_code_at(secret, when=int(time.time()))
    resp = client.post("/auth/totp/verify", json={"code": code})
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == "login"
    assert SESSION_COOKIE_NAME in resp.cookies


def test_verify_rejects_a_wrong_code(client, db_path):
    account_id = _make_account_http(db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)
    _set_password(db_path, account_id, "a-real-password")
    _seed_active_totp(db_path, account_id)

    client.post("/auth/password/start", json={"email": "dev@example.com", "password": "a-real-password"})
    resp = client.post("/auth/totp/verify", json={"code": "000000"})
    assert resp.status_code == 401
    assert SESSION_COOKIE_NAME not in resp.cookies


def test_verify_with_no_pending_challenge_is_rejected(client, db_path):
    resp = client.post("/auth/totp/verify", json={"code": "123456"})
    assert resp.status_code == 400


def test_verify_recovery_code_completes_sign_in_and_decrements_count(client, db_path):
    account_id = _make_account_http(db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)
    _set_password(db_path, account_id, "a-real-password")
    _, recovery_codes = _seed_active_totp(db_path, account_id)

    client.post("/auth/password/start", json={"email": "dev@example.com", "password": "a-real-password"})
    resp = client.post("/auth/totp/verify", json={"recovery_code": recovery_codes[0]})
    assert resp.status_code == 200
    assert SESSION_COOKIE_NAME in resp.cookies

    status = client.get("/api/account").json()["totp"]
    assert status["recovery_codes_remaining"] == len(recovery_codes) - 1


def test_verify_ip_rate_limited(client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "account_totp_verify_ip_rate_limit_attempts", 1)
    monkeypatch.setattr(settings, "account_totp_verify_ip_rate_limit_window_seconds", 60)
    account_id = _make_account_http(db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)
    _set_password(db_path, account_id, "a-real-password")
    _seed_active_totp(db_path, account_id)
    client.post("/auth/password/start", json={"email": "dev@example.com", "password": "a-real-password"})

    r1 = client.post("/auth/totp/verify", json={"code": "000000"})
    assert r1.status_code == 401
    r2 = client.post("/auth/totp/verify", json={"code": "000000"})
    assert r2.status_code == 429


def test_verify_challenge_rate_limited(client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "account_totp_verify_challenge_rate_limit_attempts", 1)
    monkeypatch.setattr(settings, "account_totp_verify_challenge_rate_limit_window_seconds", 60)
    account_id = _make_account_http(db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)
    _set_password(db_path, account_id, "a-real-password")
    _seed_active_totp(db_path, account_id)
    client.post("/auth/password/start", json={"email": "dev@example.com", "password": "a-real-password"})

    r1 = client.post("/auth/totp/verify", json={"code": "000000"})
    assert r1.status_code == 401
    r2 = client.post("/auth/totp/verify", json={"code": "000000"})
    assert r2.status_code == 429


# =========================================================================
# Part 4: which doors TOTP guards -- password + magic link yes, OAuth no
# =========================================================================


def test_password_start_without_totp_still_issues_a_session_directly(client, db_path):
    """Confirms the existing (pre-TOTP) behavior is unchanged for an
    account that never enrolled -- this is the same assertion
    tests/test_account_security.py's own
    test_password_start_success_issues_a_session makes; repeated here
    so this file stands on its own for exactly the boundary it tests.
    """
    account_id = _make_account_http(db_path)
    _add_identity(db_path, account_id, provider="github", email="dev@example.com", email_verified=1)
    _set_password(db_path, account_id, "a-real-password")
    resp = client.post("/auth/password/start", json={"email": "dev@example.com", "password": "a-real-password"})
    assert resp.status_code == 200
    assert resp.json()["result"] == "login"
    assert SESSION_COOKIE_NAME in resp.cookies


def test_email_callback_requires_totp_when_active(client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    monkeypatch.setattr(settings, "account_session_cookie_secure", False)

    account_id = _make_account_http(db_path)
    _login(client, account_id)
    _enroll_and_activate(client)
    client.cookies.delete(SESSION_COOKIE_NAME)

    # Give the account a verified email identity of provider='email',
    # so a fresh magic-link token for that same address resolves as
    # case 1 (login) -- not a brand-new pending identity.
    _add_identity(db_path, account_id, provider="email", email="dev@example.com", email_verified=1)

    raw_token = "totp-email-token"
    token_hash = hash_secret(raw_token)
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO email_login_token(token_hash, email, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token_hash, "dev@example.com", now, now + 900),
    )
    conn.commit()
    conn.close()

    resp = client.get("/auth/email/callback", params={"token": raw_token, "format": "json"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == "totp_required"
    assert "challenge_token" in body
    assert SESSION_COOKIE_NAME not in resp.cookies


def test_email_callback_without_totp_logs_in_directly(client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    monkeypatch.setattr(settings, "account_session_cookie_secure", False)

    account_id = _make_account_http(db_path)
    _add_identity(db_path, account_id, provider="email", email="dev@example.com", email_verified=1)

    raw_token = "plain-email-token"
    token_hash = hash_secret(raw_token)
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO email_login_token(token_hash, email, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token_hash, "dev@example.com", now, now + 900),
    )
    conn.commit()
    conn.close()

    resp = client.get("/auth/email/callback", params={"token": raw_token, "format": "json"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == "login"
    assert "mw_session" in resp.cookies


def test_oauth_sign_in_is_never_totp_gated(db_path, monkeypatch):
    """The settled design boundary: a resolve_oauth_callback() outcome
    routed through _respond_to_callback_outcome() with its DEFAULT
    enforce_totp (False -- exactly what oauth_callback() itself always
    passes, since it never sets the keyword at all) must issue a
    session immediately, even for an account that has TOTP fully
    active. This exercises the exact function oauth_callback() calls,
    with the exact argument shape it calls it with (no enforce_totp
    keyword), which is the enforcement of the boundary described in
    this module's own "which doors this guards" docstring section.
    """
    monkeypatch.setattr(settings, "account_session_cookie_secure", False)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    account_id = cur.lastrowid
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES ('github', 'gh-42', ?, 'dev@example.com', 1, ?)",
        (account_id, int(time.time())),
    )
    # A fully active TOTP secret on this account -- if OAuth were ever
    # gated, this would force a totp_required response below.
    secret = generate_secret()
    now = int(time.time())
    conn.execute(
        "INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at, last_used_step) "
        "VALUES (?, ?, ?, ?, NULL)",
        (account_id, encrypt_secret(secret), now, now),
    )
    conn.commit()
    conn.close()

    app = FastAPI()
    app.include_router(oauth_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    client = TestClient(app)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    identity = ProviderIdentity(subject="gh-42", email="dev@example.com", email_verified=True)
    outcome = resolve_oauth_callback(
        conn, provider_name="github", identity=identity, current_account_id=None, now=int(time.time())
    )
    conn.commit()
    conn.close()
    assert outcome["case"] == "login"

    class _FakeRequest:
        query_params = {"format": "json"}
        headers = {}

    resp = _run(
        _respond_to_callback_outcome(
            _FakeRequest(), outcome=outcome, identity=identity, now=int(time.time())
        )
    )
    import json
    body = json.loads(resp.body)
    assert body == {"result": "login", "account_id": account_id}
    assert any(h[0].lower() == b"set-cookie" and b"mw_session" in h[1] for h in resp.raw_headers)
