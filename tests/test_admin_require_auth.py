"""Tests for settings.admin_require_auth (app/config.py) -- the
preview/dev escape hatch that turns app/admin_api.py's _role_guard() off
entirely, rather than merely relaxing its TOTP requirement.

Background: preview (CT 113) is a network-isolated, disposable clone of
production, with /admin and /api/admin/* already 404'd at its public
host. Its database is periodically re-cloned from production, which
wipes every account, session, and account_totp row on every clone -- so
a hand-built credential would have to be re-provisioned after each one
just to look at the panel. admin_require_auth lets that one deployment
shape skip session/role/TOTP altogether; the isolation is the access
control there. It defaults true and must stay true anywhere reachable
from the internet -- see the flag's own comment in app/config.py.

Same "FastAPI-around-one-router" TestClient shape
tests/test_admin_roles.py already uses, with a real file-backed sqlite
database (app/admin_api.py's routes go through app/db.py's
connect()/WriteSession, a fresh connection per call, so ":memory:"
would not share data between them).
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.db as db
from app.admin_api import router as admin_router
from app.auth import http_exception_as_error_body
from app.config import Settings
from app.db import MIGRATIONS, SCHEMA
from app.sessions import SESSION_COOKIE_NAME, create_session

NOW = int(time.time())


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
    return TestClient(app)


def _run(coro):
    return asyncio.run(coro)


def _make_account(path: str, *, role: str | None = None, totp_active: bool = False) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO account(created_at, role) VALUES (?, ?)", (NOW, role)
    )
    account_id = cur.lastrowid
    if totp_active:
        conn.execute(
            "INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at) "
            "VALUES (?, 'unused', ?, ?)",
            (account_id, NOW, NOW),
        )
    conn.commit()
    conn.close()
    return account_id


def _login_as(client, account_id: int) -> None:
    raw_token = _run(create_session(account_id, device_label=None))
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)


# ===========================================================================
# The setting itself
# ===========================================================================


def test_admin_require_auth_defaults_true():
    assert Settings(meshview_base_url="https://example.invalid").admin_require_auth is True


# ===========================================================================
# admin_require_auth TRUE (the default) -- pins existing _role_guard()
# behaviour, unchanged by this flag's addition.
# ===========================================================================


def test_true_no_session_is_401(client, db_path, monkeypatch):
    import app.admin_api as admin_api_module
    monkeypatch.setattr(admin_api_module.settings, "admin_token", "the-token")

    resp = client.get("/api/admin/players")

    assert resp.status_code == 401


def test_true_admin_role_without_totp_is_403(client, db_path, monkeypatch):
    import app.admin_api as admin_api_module
    monkeypatch.setattr(admin_api_module.settings, "admin_token", "the-token")

    admin_id = _make_account(db_path, role="admin", totp_active=False)
    _login_as(client, admin_id)

    resp = client.get("/api/admin/players")

    assert resp.status_code == 403


# ===========================================================================
# admin_require_auth FALSE -- the escape hatch itself
# ===========================================================================


def test_false_no_session_at_all_reaches_the_admin_route(client, db_path, monkeypatch):
    import app.admin_api as admin_api_module
    # _admin_surface_enabled() must still pass -- give it a real role
    # holder rather than relying on admin_token, so this test is only
    # ever exercising the admin_require_auth branch, not the surface
    # check (that gets its own test below).
    _make_account(db_path, role="operator", totp_active=True)
    monkeypatch.setattr(admin_api_module.settings, "admin_require_auth", False)

    # No _login_as() call anywhere in this test -- no session cookie is
    # ever set. With admin_require_auth on, this would be a bare 401
    # (see test_true_no_session_is_401 above).
    resp = client.get("/api/admin/players")

    assert resp.status_code == 200


def test_false_still_404s_when_admin_token_is_empty_and_nobody_holds_a_role(
    client, db_path, monkeypatch
):
    """_admin_surface_enabled()'s own "empty means off, never open"
    contract must keep winning even with admin_require_auth off --
    that flag skips the SESSION/role/TOTP checks inside _role_guard(),
    never the surface-exists check that runs before them. Otherwise a
    fresh install with nothing configured at all would suddenly have an
    open admin panel the moment this flag is (mis)set, rather than
    needing an admin_token or an existing role holder first.
    """
    import app.admin_api as admin_api_module
    monkeypatch.setattr(admin_api_module.settings, "admin_token", "")
    monkeypatch.setattr(admin_api_module.settings, "admin_require_auth", False)

    resp = client.get("/api/admin/players")

    assert resp.status_code == 404
    assert resp.json() == {"error": "not found"}
