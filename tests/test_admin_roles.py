"""Tests for the roles layer added by the privacy-hardening pass:
account.role, app/admin_api.py's _role_guard()/_admin_surface_enabled(),
the claim/grant/revoke routes, and the admin_action_log audit trail.

Before this pass, every /api/admin/* action was authenticated by one
shared X-Admin-Token header -- anonymous by construction, since a
header either matches the one configured secret or it doesn't; there
was no "which admin" to record. This file is deliberately adversarial:
it tries to escalate as an admin account in every direction the code
might allow (grant, revoke, demote, delete, self-escalate) and asserts
each one is refused, alongside the ordinary "this works as designed"
cases for an operator and for the claim flow itself.

Same "FastAPI-around-one-router" TestClient shape
tests/test_admin_account_release.py uses, with a real file-backed
sqlite database (app/admin_api.py's routes go through app/db.py's
connect()/WriteSession, a fresh connection per call, so ":memory:"
would not share data between them).
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.db as db
import app.totp_api as totp_api_module
from app.account_api import router as account_router
from app.admin_api import router as admin_router
from app.admin_ops import router as admin_ops_router
from app.auth import http_exception_as_error_body
from app.db import MIGRATIONS, SCHEMA
from app.sessions import SESSION_COOKIE_NAME, create_session
from app.totp_api import router as totp_router

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
    app.include_router(admin_ops_router)
    app.include_router(account_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_claim_operator_rate_limiter():
    """app/admin_api.py's _claim_operator_addr_limiter is a module-level
    singleton (see app/auth.py's module docstring on why every
    _BoundedHits budget in this codebase is built once, at import time,
    rather than per-request) -- it accumulates hits across every test in
    this file, keyed on TestClient's fixed synthetic peer address,
    unless cleared between tests. Same pattern
    tests/test_account_api.py's own _reset_link_key_rate_limiter() uses
    for the identical reason.
    """
    import app.admin_api as admin_api_module

    admin_api_module._claim_operator_addr_limiter._hits.clear()
    yield
    admin_api_module._claim_operator_addr_limiter._hits.clear()


def _run(coro):
    return asyncio.run(coro)


def _make_account(path: str, *, role: str | None = None, totp_active: bool = False) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO account(created_at, role) VALUES (?, ?)", (NOW, role)
    )
    account_id = cur.lastrowid
    if totp_active:
        # account_totp.secret_encrypted is NOT NULL but the claim route
        # (and every other TOTP-gated call site) only ever checks
        # activated_at -- a dummy value is fine, nothing here decrypts
        # it.
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


def _new_client(app) -> TestClient:
    """A second TestClient sharing the same app (and therefore the same
    settings/db_path) but its OWN cookie jar -- used whenever a test
    needs two different signed-in accounts at once.
    """
    return TestClient(app)


def _role_of(db_path: str, account_id: int) -> str | None:
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT role FROM account WHERE account_id = ?", (account_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def _admin_action_log_rows(db_path: str) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT actor_account_id, action, detail FROM admin_action_log ORDER BY log_id"
    ).fetchall()
    conn.close()
    return rows


ADMIN_TOKEN = "the-shared-bootstrap-token"


@pytest.fixture(autouse=True)
def _admin_token(monkeypatch):
    import app.admin_api as admin_api_module
    monkeypatch.setattr(admin_api_module.settings, "admin_token", ADMIN_TOKEN)


# ===========================================================================
# POST /api/admin/roles/claim
# ===========================================================================


def test_claim_404s_when_token_not_configured(client, db_path, monkeypatch):
    import app.admin_api as admin_api_module
    monkeypatch.setattr(admin_api_module.settings, "admin_token", "")

    account_id = _make_account(db_path, totp_active=True)
    _login_as(client, account_id)

    resp = client.post("/api/admin/roles/claim", json={"token": ADMIN_TOKEN})

    assert resp.status_code == 404
    assert _role_of(db_path, account_id) is None


def test_claim_requires_a_session(client, db_path):
    resp = client.post("/api/admin/roles/claim", json={"token": ADMIN_TOKEN})
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_claim_refuses_the_wrong_token(client, db_path):
    account_id = _make_account(db_path, totp_active=True)
    _login_as(client, account_id)

    resp = client.post("/api/admin/roles/claim", json={"token": "not-the-token"})

    assert resp.status_code == 401
    assert _role_of(db_path, account_id) is None


def test_claim_refuses_without_active_totp(client, db_path):
    account_id = _make_account(db_path, totp_active=False)
    _login_as(client, account_id)

    resp = client.post("/api/admin/roles/claim", json={"token": ADMIN_TOKEN})

    assert resp.status_code == 403
    assert _role_of(db_path, account_id) is None


def test_claim_refuses_with_a_pending_unactivated_totp_enrollment(client, db_path):
    # A row exists in account_totp but activated_at is still NULL -- an
    # enrollment that was started but never proven. Must count the same
    # as no TOTP at all (see account_totp.activated_at's own comment in
    # app/db.py).
    account_id = _make_account(db_path, totp_active=False)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at) "
        "VALUES (?, 'unused', ?, NULL)",
        (account_id, NOW),
    )
    conn.commit()
    conn.close()
    _login_as(client, account_id)

    resp = client.post("/api/admin/roles/claim", json={"token": ADMIN_TOKEN})

    assert resp.status_code == 403
    assert _role_of(db_path, account_id) is None


def test_claim_success_grants_operator_and_is_fully_audited(client, db_path):
    account_id = _make_account(db_path, totp_active=True)
    _login_as(client, account_id)

    resp = client.post("/api/admin/roles/claim", json={"token": ADMIN_TOKEN})

    assert resp.status_code == 200
    assert resp.json() == {"account_id": account_id, "role": "operator"}
    assert _role_of(db_path, account_id) == "operator"

    conn = sqlite3.connect(db_path)
    event = conn.execute(
        "SELECT kind, actor FROM account_link_event WHERE account_id = ?", (account_id,)
    ).fetchone()
    conn.close()
    assert event == ("operator_claimed", "user")

    rows = _admin_action_log_rows(db_path)
    assert len(rows) == 1
    assert rows[0][0] == account_id
    assert rows[0][1] == "claim_operator"


def test_second_account_can_claim_with_the_same_token(client, db_path):
    """Matt's explicit call: the token is not single-use. A second
    account claiming with it is how a second operator gets added, and
    how recovery works if every existing operator becomes unreachable.
    """
    app = client.app
    first_id = _make_account(db_path, totp_active=True)
    second_id = _make_account(db_path, totp_active=True)

    client_a = _new_client(app)
    _login_as(client_a, first_id)
    resp_a = client_a.post("/api/admin/roles/claim", json={"token": ADMIN_TOKEN})
    assert resp_a.status_code == 200

    client_b = _new_client(app)
    _login_as(client_b, second_id)
    resp_b = client_b.post("/api/admin/roles/claim", json={"token": ADMIN_TOKEN})
    assert resp_b.status_code == 200

    assert _role_of(db_path, first_id) == "operator"
    assert _role_of(db_path, second_id) == "operator"


def test_claim_upgrades_an_existing_admin_to_operator(client, db_path):
    account_id = _make_account(db_path, role="admin", totp_active=True)
    _login_as(client, account_id)

    resp = client.post("/api/admin/roles/claim", json={"token": ADMIN_TOKEN})

    assert resp.status_code == 200
    assert _role_of(db_path, account_id) == "operator"


# ===========================================================================
# Adversarial: an admin account escalating in every direction it might try
# ===========================================================================


def test_admin_cannot_list_roles(client, db_path):
    admin_id = _make_account(db_path, role="admin")
    _login_as(client, admin_id)

    resp = client.get("/api/admin/roles")

    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_admin_cannot_grant_admin_to_anyone(client, db_path):
    admin_id = _make_account(db_path, role="admin")
    target_id = _make_account(db_path)
    _login_as(client, admin_id)

    resp = client.post("/api/admin/roles/grant", json={"account_id": target_id})

    assert resp.status_code == 401
    assert _role_of(db_path, target_id) is None


def test_admin_cannot_grant_admin_to_itself(client, db_path):
    admin_id = _make_account(db_path, role="admin")
    _login_as(client, admin_id)

    resp = client.post("/api/admin/roles/grant", json={"account_id": admin_id})

    assert resp.status_code == 401
    assert _role_of(db_path, admin_id) == "admin"  # unchanged


def test_admin_cannot_revoke_another_admins_role(client, db_path):
    actor_id = _make_account(db_path, role="admin")
    victim_id = _make_account(db_path, role="admin")
    _login_as(client, actor_id)

    resp = client.post("/api/admin/roles/revoke", json={"account_id": victim_id})

    assert resp.status_code == 401
    assert _role_of(db_path, victim_id) == "admin"  # unchanged


def test_admin_cannot_revoke_an_operators_role(client, db_path):
    admin_id = _make_account(db_path, role="admin")
    operator_id = _make_account(db_path, role="operator")
    _login_as(client, admin_id)

    resp = client.post("/api/admin/roles/revoke", json={"account_id": operator_id})

    assert resp.status_code == 401
    assert _role_of(db_path, operator_id) == "operator"  # unchanged


def test_admin_cannot_revoke_its_own_role(client, db_path):
    admin_id = _make_account(db_path, role="admin")
    _login_as(client, admin_id)

    resp = client.post("/api/admin/roles/revoke", json={"account_id": admin_id})

    assert resp.status_code == 401
    assert _role_of(db_path, admin_id) == "admin"  # unchanged, cannot self-demote either


def test_admin_cannot_delete_an_operator_player_via_role_routes(client, db_path):
    # There is no route that lets an admin act on account.role AT ALL --
    # confirms the boundary is enforced by _role_guard's need="operator"
    # requirement on every roles/* route, not by per-route logic that
    # could be individually wrong.
    admin_id = _make_account(db_path, role="admin")
    operator_id = _make_account(db_path, role="operator")
    _login_as(client, admin_id)

    for path, body in (
        ("/api/admin/roles/grant", {"account_id": operator_id}),
        ("/api/admin/roles/revoke", {"account_id": operator_id}),
    ):
        resp = client.post(path, json=body)
        assert resp.status_code == 401, path

    assert client.get("/api/admin/roles").status_code == 401
    assert _role_of(db_path, operator_id) == "operator"


# ===========================================================================
# An operator can do everything an admin cannot, above
# ===========================================================================


def test_operator_can_grant_admin_and_it_is_audited(client, db_path):
    # totp_active=True on every operator/admin account below that is
    # actually EXPECTED to reach a route's own logic (as opposed to
    # being turned away by _role_guard's rank check before TOTP is ever
    # consulted) -- _role_guard() now also requires active TOTP to USE
    # a role, not merely to hold one (see that function's own docstring
    # for the new use-time requirement). These tests are exercising
    # rank/ownership logic, not TOTP, so the fixture accounts are given
    # TOTP up front rather than incidentally re-testing the new gate
    # here too.
    operator_id = _make_account(db_path, role="operator", totp_active=True)
    target_id = _make_account(db_path)
    _login_as(client, operator_id)

    resp = client.post("/api/admin/roles/grant", json={"account_id": target_id})

    assert resp.status_code == 200
    assert resp.json() == {"account_id": target_id, "role": "admin", "changed": True}
    assert _role_of(db_path, target_id) == "admin"

    rows = _admin_action_log_rows(db_path)
    assert rows[-1] == (operator_id, "role_granted", f"account_id={target_id} role=admin")


def test_operator_can_revoke_an_admin(client, db_path):
    operator_id = _make_account(db_path, role="operator", totp_active=True)
    admin_id = _make_account(db_path, role="admin")
    _login_as(client, operator_id)

    resp = client.post("/api/admin/roles/revoke", json={"account_id": admin_id})

    assert resp.status_code == 200
    assert resp.json() == {"account_id": admin_id, "role": None, "revoked": True}
    assert _role_of(db_path, admin_id) is None

    rows = _admin_action_log_rows(db_path)
    assert rows[-1] == (operator_id, "role_revoked", f"account_id={admin_id} role=admin")


def test_operator_can_revoke_another_operator(client, db_path):
    operator_id = _make_account(db_path, role="operator", totp_active=True)
    other_operator_id = _make_account(db_path, role="operator")
    _login_as(client, operator_id)

    resp = client.post("/api/admin/roles/revoke", json={"account_id": other_operator_id})

    assert resp.status_code == 200
    assert _role_of(db_path, other_operator_id) is None


def test_operator_can_list_roles(client, db_path):
    operator_id = _make_account(db_path, role="operator", totp_active=True)
    admin_id = _make_account(db_path, role="admin")
    _login_as(client, operator_id)

    resp = client.get("/api/admin/roles")

    assert resp.status_code == 200
    ids = {r["account_id"]: r["role"] for r in resp.json()["roles"]}
    assert ids == {operator_id: "operator", admin_id: "admin"}


def _link_player(db_path: str, account_id: int, display_name: str) -> None:
    """Insert a player row and link it to account_id via the same
    player.account_id column /api/admin/roles now LEFT JOINs on (see
    that column's UNIQUE index, idx_player_account, in app/db.py's
    MIGRATIONS).
    """
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO player(display_name, team, created_at, account_id) "
        "VALUES (?, 'RED', ?, ?)",
        (display_name, NOW, account_id),
    )
    conn.commit()
    conn.close()


def test_list_roles_includes_display_name_for_a_linked_player(client, db_path):
    operator_id = _make_account(db_path, role="operator", totp_active=True)
    _link_player(db_path, operator_id, "Malice")
    _login_as(client, operator_id)

    resp = client.get("/api/admin/roles")

    assert resp.status_code == 200
    rows = {r["account_id"]: r["display_name"] for r in resp.json()["roles"]}
    assert rows == {operator_id: "Malice"}


def test_list_roles_still_includes_a_role_holder_with_no_linked_player(client, db_path):
    # The LEFT JOIN must not drop an account holding a role just
    # because it has no player row -- that account still needs to show
    # up here so its role can be seen and revoked.
    operator_id = _make_account(db_path, role="operator", totp_active=True)
    admin_id = _make_account(db_path, role="admin")  # deliberately unlinked
    _login_as(client, operator_id)

    resp = client.get("/api/admin/roles")

    assert resp.status_code == 200
    rows = {r["account_id"]: r["display_name"] for r in resp.json()["roles"]}
    assert rows == {operator_id: None, admin_id: None}


def test_grant_refuses_to_silently_demote_an_operator(client, db_path):
    operator_id = _make_account(db_path, role="operator", totp_active=True)
    other_operator_id = _make_account(db_path, role="operator")
    _login_as(client, operator_id)

    resp = client.post("/api/admin/roles/grant", json={"account_id": other_operator_id})

    assert resp.status_code == 409
    assert _role_of(db_path, other_operator_id) == "operator"  # unchanged, not demoted


def test_grant_is_a_no_op_for_an_account_already_admin(client, db_path):
    operator_id = _make_account(db_path, role="operator", totp_active=True)
    already_admin_id = _make_account(db_path, role="admin")
    _login_as(client, operator_id)

    resp = client.post("/api/admin/roles/grant", json={"account_id": already_admin_id})

    assert resp.status_code == 200
    assert resp.json()["changed"] is False


def test_revoke_404s_for_an_account_holding_no_role(client, db_path):
    operator_id = _make_account(db_path, role="operator", totp_active=True)
    plain_id = _make_account(db_path)
    _login_as(client, operator_id)

    resp = client.post("/api/admin/roles/revoke", json={"account_id": plain_id})

    assert resp.status_code == 409
    assert resp.json() == {"error": "that account holds no role"}


def test_grant_404s_for_a_nonexistent_account(client, db_path):
    operator_id = _make_account(db_path, role="operator", totp_active=True)
    _login_as(client, operator_id)

    resp = client.post("/api/admin/roles/grant", json={"account_id": 999999})

    assert resp.status_code == 404


# ===========================================================================
# TOTP is required to USE a role, not merely to hold one -- see
# _role_guard's own docstring on why the requirement lives at use-time
# rather than grant-time, and why the refusal below is 403 (a distinct,
# actionable shape) rather than folding into the generic 401 every
# other failure in this guard uses.
# ===========================================================================


_TOTP_REQUIRED_BODY = {
    "error": "two-factor authentication must be enabled on this account "
    "to use the admin panel"
}


def test_admin_role_without_active_totp_is_refused(client, db_path):
    admin_id = _make_account(db_path, role="admin", totp_active=False)
    _login_as(client, admin_id)

    resp = client.get("/api/admin/players")

    assert resp.status_code == 403
    assert resp.json() == _TOTP_REQUIRED_BODY


def test_admin_role_with_active_totp_is_allowed(client, db_path):
    admin_id = _make_account(db_path, role="admin", totp_active=True)
    _login_as(client, admin_id)

    resp = client.get("/api/admin/players")

    assert resp.status_code == 200


def test_operator_without_active_totp_is_refused_too(client, db_path):
    # The requirement is not admin-specific -- it applies at the
    # operator rank too, on both the ordinary need="admin" surface and
    # the operator-only roles routes.
    operator_id = _make_account(db_path, role="operator", totp_active=False)
    _login_as(client, operator_id)

    assert client.get("/api/admin/players").status_code == 403
    assert client.get("/api/admin/roles").status_code == 403


def test_pending_totp_enrollment_does_not_count_for_role_use(client, db_path):
    # A row exists in account_totp but activated_at is still NULL --
    # enrollment was started but never proven with a real code. Must be
    # refused the same as no row at all -- mirrors POST
    # /api/admin/roles/claim's own identical pending-row check (see
    # test_claim_refuses_with_a_pending_unactivated_totp_enrollment
    # above).
    admin_id = _make_account(db_path, role="admin", totp_active=False)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at) "
        "VALUES (?, 'unused', ?, NULL)",
        (admin_id, NOW),
    )
    conn.commit()
    conn.close()
    _login_as(client, admin_id)

    resp = client.get("/api/admin/players")

    assert resp.status_code == 403
    assert resp.json() == _TOTP_REQUIRED_BODY


def test_role_without_totp_can_still_reach_account_page_and_totp_enrollment(
    client, db_path, monkeypatch
):
    """The deadlock guard: an account that holds a role but has not
    enrolled TOTP yet must still be able to reach the ordinary account
    page and the TOTP enrollment routes themselves -- otherwise it
    would have no way to ever clear the refusal above. GET /api/account
    (app/account_api.py) and POST /api/account/totp/enroll
    (app/totp_api.py) both depend on require_session() only, never
    _role_guard() -- true by construction (neither module imports the
    other's guard), but proven here directly with a real request rather
    than left as an inference from reading two files side by side.

    Builds its OWN app/client (rather than reusing the `client` fixture
    above) because this is the one test in this file that needs
    app/totp_api.py's router mounted alongside the admin and account
    routers.
    """
    monkeypatch.setattr(
        totp_api_module.settings, "account_totp_encryption_key", Fernet.generate_key().decode()
    )

    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(account_router)
    app.include_router(totp_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    local_client = TestClient(app)

    admin_id = _make_account(db_path, role="admin", totp_active=False)
    raw_token = _run(create_session(admin_id, device_label=None))
    local_client.cookies.set(SESSION_COOKIE_NAME, raw_token)

    # Confirms there is something to escape from: the admin route
    # itself is refused without active TOTP.
    assert local_client.get("/api/admin/players").status_code == 403

    account_resp = local_client.get("/api/account")
    assert account_resp.status_code == 200
    assert account_resp.json()["role"] == "admin"
    assert account_resp.json()["totp"]["enabled"] is False

    enroll_resp = local_client.post("/api/account/totp/enroll")
    assert enroll_resp.status_code == 200


# ===========================================================================
# A player (no role) gets nothing
# ===========================================================================


def test_player_with_no_role_is_refused_everywhere(client, db_path):
    plain_id = _make_account(db_path)
    _login_as(client, plain_id)

    assert client.get("/api/admin/players").status_code == 401
    assert client.get("/api/admin/roles").status_code == 401
    assert client.post("/api/admin/roles/grant", json={"account_id": plain_id}).status_code == 401
    assert client.post("/api/admin/roles/revoke", json={"account_id": plain_id}).status_code == 401
    assert client.post("/api/admin/player/team", json={"player_id": 1, "team": "RED"}).status_code == 401


def test_anonymous_caller_with_no_session_gets_nothing(client, db_path):
    # An account holding a role exists (so the surface itself is
    # enabled), but this request carries no session cookie at all.
    _make_account(db_path, role="operator")

    assert client.get("/api/admin/players").status_code == 401
    assert client.get("/api/admin/roles").status_code == 401


# ===========================================================================
# The retired header authenticates nothing, anywhere
# ===========================================================================


def test_header_does_not_authenticate_admin_api_routes(client, db_path):
    resp = client.get("/api/admin/players", headers={"X-Admin-Token": ADMIN_TOKEN})
    assert resp.status_code == 401


def test_header_does_not_authenticate_admin_ops_routes(client, db_path):
    resp = client.get("/api/admin/overview", headers={"X-Admin-Token": ADMIN_TOKEN})
    assert resp.status_code == 401


def test_header_does_not_authenticate_roles_routes(client, db_path):
    resp = client.get("/api/admin/roles", headers={"X-Admin-Token": ADMIN_TOKEN})
    assert resp.status_code == 401


# ===========================================================================
# GET /api/account exposes `role` -- what the panel button (account.js's
# renderAdminSection()) gates on. This is display-only; the assertions
# above are what actually enforce the boundary server-side.
# ===========================================================================


def test_get_account_role_is_null_for_a_plain_account(client, db_path):
    plain_id = _make_account(db_path)
    _login_as(client, plain_id)

    resp = client.get("/api/account")

    assert resp.status_code == 200
    assert resp.json()["role"] is None


def test_get_account_role_reflects_admin(client, db_path):
    admin_id = _make_account(db_path, role="admin")
    _login_as(client, admin_id)

    assert client.get("/api/account").json()["role"] == "admin"


def test_get_account_role_reflects_operator_after_claim(client, db_path):
    account_id = _make_account(db_path, totp_active=True)
    _login_as(client, account_id)

    claim = client.post("/api/admin/roles/claim", json={"token": ADMIN_TOKEN})
    assert claim.status_code == 200

    assert client.get("/api/account").json()["role"] == "operator"


# ===========================================================================
# _admin_surface_enabled(): the ordering that avoids ever locking
# everyone out
# ===========================================================================


def test_surface_stays_enabled_after_token_cleared_once_an_operator_exists(
    client, db_path, monkeypatch
):
    operator_id = _make_account(db_path, role="operator", totp_active=True)
    _login_as(client, operator_id)

    import app.admin_api as admin_api_module
    monkeypatch.setattr(admin_api_module.settings, "admin_token", "")

    # The operator's own ordinary admin routes keep working -- the
    # surface does not vanish just because bootstrap is over and the
    # token was cleared.
    resp = client.get("/api/admin/players")
    assert resp.status_code == 200


def test_surface_404s_with_no_token_and_no_role_anywhere(client, db_path, monkeypatch):
    import app.admin_api as admin_api_module
    monkeypatch.setattr(admin_api_module.settings, "admin_token", "")

    resp = client.get("/api/admin/players")
    assert resp.status_code == 404
    assert resp.json() == {"error": "not found"}
