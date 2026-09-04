"""Tests for the tail end of the fallback check-in name retirement:

  - POST /api/admin/checkin/binding (app/admin_ops.py) is gone outright,
    not merely guarded off -- a route that used to hand-register a
    mc_checkin_binding row now does nothing at all once app/checkin.py
    stopped reading that table, so leaving it in place would mean an
    operator's "Register" click silently lied about working. Deleted
    rather than left as a no-op; this test proves the route itself no
    longer exists (a 404 from FastAPI routing, not from _api_guard's
    own "admin door is off" 404 -- see the test for how those are told
    apart).

  - The two `_attention()` overview checks that used to point an
    operator at that same dead route -- checkin_unreachable and
    checkin_name_changed -- still fire on the same underlying
    conditions (see app/admin_ops.py's own comments for the detection
    logic, unchanged here), but their remediation text now points at
    node confirmation ("Confirm my node" on the player's account page,
    app/checkin_api.py's /api/checkin/confirm/* routes) instead of the
    retired fallback-name registration.

Same "call the private helper directly against the conn fixture"
pattern tests/test_notice_api.py uses for admin_ops storage logic that
doesn't need a real HTTP round trip, plus the "FastAPI-around-one-
router" TestClient shape tests/test_account_api.py uses for the one
test here that does (proving the route is truly gone, which only a
real route lookup can show).

The one HTTP test in this file (test_admin_checkin_binding_route_is_gone)
predates the privacy-hardening pass's move off the shared X-Admin-Token
header onto session+role auth (app/admin_api.py's _role_guard()) -- it
now signs a real admin-role session in rather than sending that retired
header, for the same reason the original test avoided a bare guard-off
404: this must prove the ROUTE is missing, not merely that some guard
rejected the request.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.admin_ops as admin_ops_module
import app.db as db
from app.admin_ops import _attention, router as admin_router
from app.auth import http_exception_as_error_body
from app.db import MIGRATIONS, SCHEMA
from app.sessions import SESSION_COOKIE_NAME, create_session

NOW = int(time.time())


def _make_player(conn, display_name="Test Player", team="RED"):
    cur = conn.execute(
        "INSERT INTO player(display_name, team, created_at) VALUES (?, ?, ?)",
        (display_name, team, NOW),
    )
    return cur.lastrowid


# ---- POST /api/admin/checkin/binding is gone -----------------------------


def test_admin_checkin_binding_route_is_gone(tmp_path, monkeypatch):
    # A real, valid admin session -- signed in and holding the admin
    # role -- proves the ROUTE is missing rather than just
    # re-demonstrating _role_guard's own "no role" 401: with valid
    # credentials, FastAPI would reach a real handler if one were still
    # registered here.
    db_path = str(tmp_path / "game.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                continue
            raise
    cur = conn.execute("INSERT INTO account(created_at, role) VALUES (?, 'admin')", (NOW,))
    account_id = cur.lastrowid
    conn.commit()
    conn.close()
    monkeypatch.setattr(db.settings, "db_path", db_path)

    app = FastAPI()
    app.include_router(admin_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    client = TestClient(app)
    raw_token = asyncio.run(create_session(account_id, device_label=None))
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)

    resp = client.post(
        "/api/admin/checkin/binding",
        json={"player_id": 1, "sender_name": "somebody"},
    )
    # A plain starlette "no route matched" 404, not admin_ops's own
    # JSONResponse({"error": "not found"}) shape that _role_guard
    # produces when the admin surface itself is disabled -- so this
    # fails loudly if the route is ever reintroduced without a matching
    # test update, and doesn't just coincidentally pass because the
    # guard rejected it instead.
    assert resp.status_code == 404
    assert resp.json() != {"error": "not found"}


def test_admin_checkin_binding_route_is_gone_even_with_no_admin_configured():
    # Sanity check on the other guard path: with no admin_token
    # configured and no account holding a role (this repo's fresh-
    # install default -- see app/config.py), every /api/admin/* route
    # already 404s via _role_guard/_admin_surface_enabled itself. The
    # route being gone doesn't change that, but this pins down that the
    # ordinary "admin surface off" case still behaves as documented.
    app = FastAPI()
    app.include_router(admin_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    client = TestClient(app)

    resp = client.post("/api/admin/checkin/binding", json={"player_id": 1, "sender_name": "x"})
    assert resp.status_code == 404


# ---- checkin_unreachable remediation text ---------------------------------


def test_checkin_unreachable_still_fires_and_points_at_node_confirmation(conn):
    player_id = _make_player(conn)
    # Bound to a MeshCore radio the mwmesh directory has never seen --
    # the exact condition this check exists to catch.
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) "
        "VALUES ('mc', 'aaaa1111', ?, ?)",
        (player_id, NOW),
    )
    # `directory` has to be non-empty for this branch to run at all (see
    # admin_ops.py's `if directory:` guard) -- some OTHER node's public
    # key, so it proves the directory-membership check, not an empty
    # list short-circuiting the whole block.
    directory = [{"public_key": "ffffffffffffffff"}]

    entries = _attention(conn, directory)
    matches = [e for e in entries if e["kind"] == "checkin_unreachable"]
    assert len(matches) == 1
    entry = matches[0]
    assert entry["player_id"] == player_id

    fix = entry["fix"]
    assert "Confirm my node" in fix
    assert "trigger an advert" in fix
    # The retired remediation told the operator to register a name
    # "below" via the now-deleted inline form -- that phrasing must be
    # gone, not just supplemented.
    assert "register" not in fix.lower()
    assert "below" not in fix


def test_checkin_unreachable_does_not_fire_once_the_directory_resolves_it(conn):
    # Same setup as above, but this time the radio's key IS in the
    # directory -- proves the check is still conditioned on directory
    # membership, unchanged, and not just always firing now.
    player_id = _make_player(conn)
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at, public_key) "
        "VALUES ('mc', 'aaaa1111', ?, ?, ?)",
        (player_id, NOW, "aaaa1111ffffffff"),
    )
    directory = [{"public_key": "aaaa1111ffffffff"}]

    entries = _attention(conn, directory)
    assert not [e for e in entries if e["kind"] == "checkin_unreachable"]


# ---- checkin_name_changed remediation text ---------------------------------


def test_checkin_name_changed_still_fires_and_points_at_node_confirmation(conn):
    player_id = _make_player(conn)
    conn.execute(
        "INSERT INTO checkin_node_name"
        "(connector, node_ref, player_id, name, first_seen, changed_at, previous_name) "
        "VALUES ('corescope', 'aaaa1111', ?, 'New Name', ?, ?, 'Old Name')",
        (player_id, NOW - 3600, NOW - 1800),
    )

    entries = _attention(conn, directory=[])
    matches = [e for e in entries if e["kind"] == "checkin_name_changed"]
    assert len(matches) == 1
    fix = matches[0]["fix"]
    assert "Confirm my node" in fix
    assert "fallback name" not in fix.lower()


def test_checkin_name_changed_does_not_fire_outside_the_recency_window(conn):
    # Same shape as above but changed_at is well past _STALE_DAYS (14
    # days) -- a rename an operator can no longer act on usefully
    # shouldn't clutter the list. Unchanged detection logic; pinned down
    # here so a future edit near the remediation text can't accidentally
    # widen or narrow the window without a test noticing.
    player_id = _make_player(conn)
    conn.execute(
        "INSERT INTO checkin_node_name"
        "(connector, node_ref, player_id, name, first_seen, changed_at, previous_name) "
        "VALUES ('corescope', 'aaaa1111', ?, 'New Name', ?, ?, 'Old Name')",
        (player_id, NOW - 90 * 86400, NOW - 30 * 86400),
    )

    entries = _attention(conn, directory=[])
    assert not [e for e in entries if e["kind"] == "checkin_name_changed"]
