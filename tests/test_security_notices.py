"""Tests for Stage 2's nine account-security notice emails:
app/email_login.py's send_security_notice()/format_notice_timestamp(),
app/account_api.py's _verified_contact_email()/_notify_security() (the
recipient-policy gate every one of the nine call sites shares), and a
representative sample of the call sites themselves in
app/account_api.py, app/totp_api.py, and app/admin_api.py.

Not every one of the nine call sites gets its own HTTP-level test here
-- that would mostly re-prove the same four things nine times over.
Instead: the shared plumbing (_verified_contact_email, _notify_security,
send_security_notice's own mail construction) is tested directly and
thoroughly, and one self-initiated event (password change,
app/account_api.py), one TOTP event (app/totp_api.py), and one
operator-initiated event (role grant, app/admin_api.py) are driven
through real HTTP requests to prove the wiring at the router level
too -- the same "prove the shared thing well, sample the call sites"
split tests/test_oauth_api.py and tests/test_email_login.py already
draw for resolve_oauth_callback() versus its many callers.

Same fixture shapes as tests/test_account_security.py, tests/test_totp.py,
and tests/test_admin_roles.py: a real file-backed sqlite database
(app/db.py's connect()/WriteSession open a fresh connection per call, so
":memory:" would not share data across TestClient's own thread
boundary) for the HTTP-level tests, and the in-memory `conn` fixture
(tests/conftest.py) for the direct helper-function tests.

Every test that reaches a real send_security_notice() monkeypatches it
to a stub -- no real SMTP connection, no real mail, anywhere in this
file. Same _stub_send()-at-the-module-level pattern
tests/test_account_security.py's own send_magic_link_email stub uses.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.account_api as account_api_module
import app.admin_api as admin_api_module
import app.db as db
import app.email_login as email_login
import app.totp_api as totp_api_module
from app.account_api import router as account_router
from app.admin_api import router as admin_router
from app.auth import http_exception_as_error_body
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.email_login import EmailSendError, format_notice_timestamp, send_security_notice
from app.oauth_api import router as oauth_router
from app.sessions import SESSION_COOKIE_NAME, create_session
from app.totp_api import router as totp_router


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


def _make_account(path: str, *, role: str | None = None, totp_active: bool = False) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute("INSERT INTO account(created_at, role) VALUES (?, ?)", (int(time.time()), role))
    account_id = cur.lastrowid
    if totp_active:
        conn.execute(
            "INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at) "
            "VALUES (?, 'unused', ?, ?)",
            (account_id, int(time.time()), int(time.time())),
        )
    conn.commit()
    conn.close()
    return account_id


def _set_contact_email(path: str, account_id: int, email: str | None, *, verified: bool) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE account SET contact_email = ?, contact_email_verified_at = ? WHERE account_id = ?",
        (email, int(time.time()) if verified else None, account_id),
    )
    conn.commit()
    conn.close()


def _add_identity(path: str, account_id: int, *, provider="email", subject=None,
                   email=None, email_verified=1) -> None:
    subject = subject or email
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (provider, subject, account_id, email, email_verified, int(time.time())),
    )
    conn.commit()
    conn.close()


def _login_as(client: TestClient, account_id: int) -> None:
    raw_token = _run(create_session(account_id, device_label="Firefox on Windows"))
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)


def _stub_notice_send(monkeypatch, module, *, raises: bool = False):
    """Same _stub_send()-at-the-module-level shape
    tests/test_account_security.py's own stub for send_magic_link_email
    uses, aimed at send_security_notice() instead -- patched on whichever
    module actually calls it (account_api_module for every account_api.py
    AND admin_api.py call site, since app/admin_api.py imports
    _notify_security -- and therefore its call to send_security_notice --
    straight from app/account_api.py's own module globals; totp_api_module
    for app/totp_api.py's own duplicate copy).
    """
    calls = []

    async def _fake(to_address, *, subject, heading, lines, cta_label, cta_url, footer):
        calls.append({
            "to_address": to_address, "subject": subject, "heading": heading,
            "lines": tuple(lines), "cta_label": cta_label, "cta_url": cta_url, "footer": footer,
        })
        if raises:
            raise EmailSendError("boom")

    monkeypatch.setattr(module, "send_security_notice", _fake)
    return calls


# =========================================================================
# app/email_login.py -- format_notice_timestamp()
# =========================================================================

def test_format_notice_timestamp_renders_utc_regardless_of_local_tz(monkeypatch):
    # 2026-09-07T01:19:05Z
    assert format_notice_timestamp(1788743945) == "7 September 2026 at 01:19 UTC"


def test_format_notice_timestamp_pads_minutes_not_the_day():
    # 3 January 2026 at 00:05 UTC -- day has no leading zero, minutes do.
    assert format_notice_timestamp(1767398700) == "3 January 2026 at 00:05 UTC"


# =========================================================================
# app/email_login.py -- send_security_notice()
# =========================================================================

class _FakeSMTP:
    instances: list["_FakeSMTP"] = []

    def __init__(self, *a, **k):
        self.sent = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, **kwargs):
        pass

    def login(self, *a):
        pass

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.sent = {"msg": msg, "from_addr": from_addr, "to_addrs": to_addrs}


@pytest.fixture(autouse=True)
def _reset_fake_smtp():
    _FakeSMTP.instances.clear()
    yield
    _FakeSMTP.instances.clear()


@pytest.fixture
def _smtp_settings(monkeypatch):
    monkeypatch.setattr(email_login.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_tls_mode", "starttls")
    monkeypatch.setattr(settings, "smtp_from_address", "admin@example.test")
    monkeypatch.setattr(settings, "smtp_from_name", "MeshWars")


def test_send_security_notice_multipart_headers_and_envelope(_smtp_settings):
    _run(send_security_notice(
        "dev@example.com",
        subject="Your MeshWars password was changed",
        heading="Your password was changed",
        lines=(
            "The password on your MeshWars account was changed on 7 September 2026 at 01:19 UTC.",
            "If that was you, there is nothing to do.",
            "If it wasn't, someone else has access. Sign in, rotate your API key, and "
            "check which sign-in methods are attached to the account.",
        ),
        cta_label="Review your account",
        cta_url="https://mw.test/account",
        footer="You're getting this because this address is confirmed on a MeshWars "
        "account. Security notices can't be turned off.",
    ))

    assert len(_FakeSMTP.instances) == 1
    sent = _FakeSMTP.instances[0].sent
    assert sent is not None

    # Envelope sender/recipient passed explicitly, bare address -- same
    # DKIM-signing reasoning _send_sync()'s own comment gives
    # (app/email_login.py).
    assert sent["from_addr"] == "admin@example.test"
    assert sent["to_addrs"] == ["dev@example.com"]

    msg = sent["msg"]
    assert msg["Subject"] == "Your MeshWars password was changed"
    assert msg["From"] == "MeshWars <admin@example.test>"
    assert msg["To"] == "dev@example.com"
    assert msg["Date"] is not None
    assert msg["Message-ID"].endswith("@example.test>")
    assert msg["X-Mailer"] == "MeshWars"
    # Transactional security mail, not a subscription -- see
    # send_security_notice()'s own docstring.
    assert msg["List-Unsubscribe"] is None

    assert msg.is_multipart()
    text_part = html_part = image_part = None
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype == "text/plain" and text_part is None:
            text_part = part
        elif ctype == "text/html" and html_part is None:
            html_part = part
        elif ctype == "image/png" and image_part is None:
            image_part = part

    assert text_part is not None, "no text/plain part"
    assert html_part is not None, "no text/html part"
    assert image_part is not None, "no image/png part"

    text_body = text_part.get_content()
    assert "Your password was changed" in text_body
    assert "https://mw.test/account" in text_body

    html_body = html_part.get_content()
    assert "Your password was changed" in html_body
    assert "https://mw.test/account" in html_body
    assert "If that was you, there is nothing to do." in html_body
    # No stray placeholder tokens left unsubstituted.
    assert "{heading}" not in html_body
    assert "{line1}" not in html_body
    assert "{cta_url}" not in html_body

    image_content_id = image_part["Content-ID"]
    bare_cid = image_content_id.strip("<>")
    assert f"cid:{bare_cid}" in html_body


def test_send_security_notice_runs_off_the_event_loop(_smtp_settings):
    import threading

    main_thread = threading.current_thread()
    seen_thread = {}
    real_init = _FakeSMTP.__init__

    def _tracking_init(self, *a, **k):
        seen_thread["thread"] = threading.current_thread()
        real_init(self, *a, **k)

    _FakeSMTP.__init__ = _tracking_init
    try:
        _run(send_security_notice(
            "dev@example.com", subject="s", heading="h",
            lines=("a", "b", "c"), cta_label="Go", cta_url="https://mw.test/account",
            footer="f",
        ))
    finally:
        _FakeSMTP.__init__ = real_init

    assert seen_thread["thread"] is not main_thread


def test_send_security_notice_wraps_smtp_failure_in_email_send_error(monkeypatch):
    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(email_login.smtplib, "SMTP", _boom)
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")

    with pytest.raises(EmailSendError):
        _run(send_security_notice(
            "dev@example.com", subject="s", heading="h",
            lines=("a", "b", "c"), cta_label="Go", cta_url="https://mw.test/account",
            footer="f",
        ))


# =========================================================================
# app/account_api.py -- _verified_contact_email()
# =========================================================================

def test_verified_contact_email_none_when_never_set(conn):
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    account_id = cur.lastrowid
    assert account_api_module._verified_contact_email(conn, account_id) is None


def test_verified_contact_email_none_when_unverified(conn):
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    account_id = cur.lastrowid
    conn.execute(
        "UPDATE account SET contact_email = 'me@example.com', contact_email_verified_at = NULL "
        "WHERE account_id = ?",
        (account_id,),
    )
    assert account_api_module._verified_contact_email(conn, account_id) is None


def test_verified_contact_email_returns_address_when_verified(conn):
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    account_id = cur.lastrowid
    conn.execute(
        "UPDATE account SET contact_email = 'me@example.com', contact_email_verified_at = ? "
        "WHERE account_id = ?",
        (int(time.time()), account_id),
    )
    assert account_api_module._verified_contact_email(conn, account_id) == "me@example.com"


# =========================================================================
# app/account_api.py -- _notify_security()
# =========================================================================

def test_notify_security_skips_silently_with_no_verified_address(monkeypatch, caplog):
    calls = _stub_notice_send(monkeypatch, account_api_module)
    with caplog.at_level("INFO", logger="account_api"):
        _run(account_api_module._notify_security(
            1, None, subject="s", heading="h", lines=("a", "b", "c"),
        ))
    assert calls == []
    assert any("no verified contact email" in r.message for r in caplog.records)


def test_notify_security_sends_when_verified_address_present(monkeypatch):
    calls = _stub_notice_send(monkeypatch, account_api_module)
    _run(account_api_module._notify_security(
        1, "me@example.com", subject="s", heading="h", lines=("a", "b", "c"),
    ))
    assert len(calls) == 1
    assert calls[0]["to_address"] == "me@example.com"
    assert calls[0]["cta_label"] == "Review your account"
    assert "security notices can't be turned off" in calls[0]["footer"].lower()


def test_notify_security_swallows_send_failure(monkeypatch, caplog):
    _stub_notice_send(monkeypatch, account_api_module, raises=True)
    with caplog.at_level("ERROR", logger="account_api"):
        # Must not raise -- a notice failure can never propagate.
        _run(account_api_module._notify_security(
            1, "me@example.com", subject="s", heading="h", lines=("a", "b", "c"),
        ))
    assert any("failed to send security notice" in r.message for r in caplog.records)


# =========================================================================
# HTTP-level, event 1: password set/changed -- app/account_api.py
# =========================================================================

@pytest.fixture(autouse=True)
def _cheap_scrypt(monkeypatch):
    monkeypatch.setattr(settings, "account_password_scrypt_n", 2 ** 12)


@pytest.fixture(autouse=True)
def _reset_account_rate_limiters():
    limiters = [
        account_api_module._link_key_addr_limiter,
        account_api_module._rotate_key_addr_limiter,
        account_api_module._contact_email_account_limiter,
    ]
    for lim in limiters:
        lim._hits.clear()
    yield
    for lim in limiters:
        lim._hits.clear()


@pytest.fixture
def account_client(db_path):
    app = FastAPI()
    app.include_router(account_router)
    app.include_router(oauth_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    return TestClient(app)


def test_password_set_notifies_verified_contact_address(account_client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    calls = _stub_notice_send(monkeypatch, account_api_module)
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, email="dev@example.com", email_verified=1)
    _set_contact_email(db_path, account_id, "contact@example.com", verified=True)
    _login_as(account_client, account_id)

    resp = account_client.post("/api/account/password", json={"new_password": "correct horse battery"})

    assert resp.status_code == 200
    assert len(calls) == 1
    call = calls[0]
    assert call["to_address"] == "contact@example.com"
    assert call["subject"] == "Your MeshWars password was set"
    assert call["heading"] == "Your password was set"
    assert "was set on" in call["lines"][0]
    assert call["cta_url"] == "https://mw.test/account"


def test_password_change_uses_changed_wording_not_set(account_client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    calls = _stub_notice_send(monkeypatch, account_api_module)
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, email="dev@example.com", email_verified=1)
    _set_contact_email(db_path, account_id, "contact@example.com", verified=True)
    _login_as(account_client, account_id)
    account_client.post("/api/account/password", json={"new_password": "correct horse battery"})
    calls.clear()

    resp = account_client.post(
        "/api/account/password",
        json={"current_password": "correct horse battery", "new_password": "another horse battery"},
    )

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0]["subject"] == "Your MeshWars password was changed"
    assert "was changed on" in calls[0]["lines"][0]


def test_password_set_sends_no_notice_without_verified_contact_address(account_client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    calls = _stub_notice_send(monkeypatch, account_api_module)
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, email="dev@example.com", email_verified=1)
    # No contact email set at all.
    _login_as(account_client, account_id)

    resp = account_client.post("/api/account/password", json={"new_password": "correct horse battery"})

    assert resp.status_code == 200
    assert calls == []


def test_password_set_ignores_unverified_contact_address(account_client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    calls = _stub_notice_send(monkeypatch, account_api_module)
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, email="dev@example.com", email_verified=1)
    _set_contact_email(db_path, account_id, "contact@example.com", verified=False)
    _login_as(account_client, account_id)

    resp = account_client.post("/api/account/password", json={"new_password": "correct horse battery"})

    assert resp.status_code == 200
    assert calls == []


def test_password_set_succeeds_even_when_notice_send_raises(account_client, db_path, monkeypatch):
    """The core guarantee: a mail failure must never break, delay, or
    roll back the action that triggered it -- see
    _notify_security()'s own docstring. The password write itself must
    still have committed.
    """
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    _stub_notice_send(monkeypatch, account_api_module, raises=True)
    account_id = _make_account(db_path)
    _add_identity(db_path, account_id, email="dev@example.com", email_verified=1)
    _set_contact_email(db_path, account_id, "contact@example.com", verified=True)
    _login_as(account_client, account_id)

    resp = account_client.post("/api/account/password", json={"new_password": "correct horse battery"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT 1 FROM account_password WHERE account_id = ?", (account_id,)).fetchone()
    conn.close()
    assert row is not None


# =========================================================================
# HTTP-level, event 2: TOTP enabled -- app/totp_api.py
# =========================================================================

@pytest.fixture
def totp_client(db_path):
    app = FastAPI()
    app.include_router(account_router)
    app.include_router(oauth_router)
    app.include_router(totp_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _totp_encryption_key(monkeypatch):
    monkeypatch.setattr(settings, "account_totp_encryption_key", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def _reset_totp_rate_limiters():
    limiters = [
        totp_api_module._activate_account_limiter,
        totp_api_module._disable_account_limiter,
    ]
    for lim in limiters:
        lim._hits.clear()
    yield
    for lim in limiters:
        lim._hits.clear()


def _b32_to_bytes(b32: str) -> bytes:
    import base64
    padding = "=" * (-len(b32) % 8)
    return base64.b32decode(b32 + padding)


def test_totp_activate_notifies_verified_contact_address(totp_client, db_path, monkeypatch):
    from app.totp import totp_code_at

    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    calls = _stub_notice_send(monkeypatch, totp_api_module)
    account_id = _make_account(db_path)
    _set_contact_email(db_path, account_id, "contact@example.com", verified=True)
    _login_as(totp_client, account_id)

    r = totp_client.post("/api/account/totp/enroll")
    assert r.status_code == 200, r.text
    secret = _b32_to_bytes(r.json()["secret"])
    code = totp_code_at(secret, when=int(time.time()))
    r2 = totp_client.post("/api/account/totp/activate", json={"code": code})
    assert r2.status_code == 200, r2.text

    assert len(calls) == 1
    assert calls[0]["to_address"] == "contact@example.com"
    assert calls[0]["subject"] == "Two-factor authentication was enabled on your MeshWars account"


def test_totp_activate_sends_no_notice_without_verified_contact(totp_client, db_path, monkeypatch):
    from app.totp import totp_code_at

    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    calls = _stub_notice_send(monkeypatch, totp_api_module)
    account_id = _make_account(db_path)
    _login_as(totp_client, account_id)

    r = totp_client.post("/api/account/totp/enroll")
    secret = _b32_to_bytes(r.json()["secret"])
    code = totp_code_at(secret, when=int(time.time()))
    r2 = totp_client.post("/api/account/totp/activate", json={"code": code})

    assert r2.status_code == 200
    assert calls == []


# =========================================================================
# HTTP-level, event 7: admin role grant (operator-initiated) --
# app/admin_api.py
# =========================================================================

@pytest.fixture
def admin_client(db_path):
    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(account_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    return TestClient(app)


def test_role_grant_notifies_target_with_operator_initiated_wording(admin_client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    calls = _stub_notice_send(monkeypatch, account_api_module)
    operator_id = _make_account(db_path, role="operator", totp_active=True)
    target_id = _make_account(db_path)
    _set_contact_email(db_path, target_id, "target@example.com", verified=True)
    _login_as(admin_client, operator_id)

    resp = admin_client.post("/api/admin/roles/grant", json={"account_id": target_id})

    assert resp.status_code == 200
    assert len(calls) == 1
    call = calls[0]
    assert call["to_address"] == "target@example.com"
    assert call["subject"] == "Your MeshWars role changed"
    assert "granted the admin role" in call["lines"][0]
    # Operator-initiated shape (events 7-9): different line2/line3 than
    # the self-initiated shape events 1-6 use.
    assert call["lines"][1] == "This was done by a MeshWars operator, not by you."
    assert call["lines"][2] == "If you weren't expecting it, reply to this message."


def test_role_grant_sends_no_notice_when_target_has_no_verified_contact(admin_client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    calls = _stub_notice_send(monkeypatch, account_api_module)
    operator_id = _make_account(db_path, role="operator", totp_active=True)
    target_id = _make_account(db_path)
    _login_as(admin_client, operator_id)

    resp = admin_client.post("/api/admin/roles/grant", json={"account_id": target_id})

    assert resp.status_code == 200
    assert calls == []


def test_role_grant_succeeds_even_when_notice_send_raises(admin_client, db_path, monkeypatch):
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    _stub_notice_send(monkeypatch, account_api_module, raises=True)
    operator_id = _make_account(db_path, role="operator", totp_active=True)
    target_id = _make_account(db_path)
    _set_contact_email(db_path, target_id, "target@example.com", verified=True)
    _login_as(admin_client, operator_id)

    resp = admin_client.post("/api/admin/roles/grant", json={"account_id": target_id})

    assert resp.status_code == 200
    assert resp.json()["changed"] is True

    conn = sqlite3.connect(db_path)
    role = conn.execute("SELECT role FROM account WHERE account_id = ?", (target_id,)).fetchone()[0]
    conn.close()
    assert role == "admin"
