"""Tests for the tile-release admin surface: making automatic release
of long-abandoned MeshCore territory configurable from the admin panel
instead of env-only.

Covers three layers:
  - app/mc_ingest.py's load_tile_release_config/
    seed_tile_release_config_from_env -- the DB-backed singleton
    (app/db.py's tile_release_config), seeded once from
    settings.mc_tile_release_*, the database winning thereafter, same
    shape app/checkin.py's load_checkin_config/seed_nets_from_env
    already establish for checkin_config.
  - McIngestor._release_expired_tiles_sync reading that row fresh every
    sweep, with no restart needed for an edit to take effect.
  - app/admin_ops.py's three tile-release routes (GET config, POST
    projection, POST config) -- the floor/ceiling enforcement, the
    read-only projection, the dry_run-off confirmation gate, and the
    audit log.

Same "FastAPI-around-one-router" TestClient shape
tests/test_admin_player_delete.py already uses for app/admin_ops.py's
sibling module app/admin_api.py, including its _login_as() helper
(role + an ACTIVE account_totp row + a signed-in session cookie --
app/admin_api.py's _role_guard() requires active two-factor to USE a
role, not merely hold one).
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.db as db
from app.admin_ops import router as admin_router
from app.auth import http_exception_as_error_body
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.grid import cell_id as grid_cell_id, cell_indices
from app import mc_scoring
from app.mc_ingest import PROTOCOL, McIngestor, load_tile_release_config, seed_tile_release_config_from_env
from app.sessions import SESSION_COOKIE_NAME, create_session

NOW = int(time.time())
LAT, LON = 43.0, -116.0


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


@pytest.fixture
def client(db_path):
    app = FastAPI()
    app.include_router(admin_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    return TestClient(app)


def _make_account(path: str, *, role: str | None = None) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO account(created_at, role) VALUES (?, ?)", (NOW, role)
    )
    conn.commit()
    account_id = cur.lastrowid
    conn.close()
    return account_id


def _login_as(client, db_path, *, role: str) -> int:
    account_id = _make_account(db_path, role=role)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at) "
        "VALUES (?, 'unused', ?, ?)",
        (account_id, NOW, NOW),
    )
    conn.commit()
    conn.close()
    raw_token = _run(create_session(account_id, device_label=None))
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return account_id


def _season_on_disk(db_path, protocol=PROTOCOL):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN IMMEDIATE")
    season_id = mc_scoring.ensure_active_season(conn, NOW, protocol)
    conn.execute("COMMIT")
    conn.close()
    return season_id


def _seed_cell_on_disk(db_path, season_id, cell_id, team, score, last_update, player_id=1):
    """A cell owned by `team`, with the mc_tile_score row
    find_expired_tiles()'s join requires -- same shape
    tests/test_mc_tile_release.py's own _seed_owned_cell uses."""
    conn = sqlite3.connect(db_path)
    lat_idx, lon_idx = cell_indices(cell_id)
    conn.execute(
        "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, "
        "last_report_ts, paint_count, lat_idx, lon_idx) VALUES (?,?,?,?,?,1,?,?)",
        (season_id, cell_id, team, player_id, last_update, lat_idx, lon_idx),
    )
    conn.execute(
        "INSERT INTO mc_tile_score(season_id, cell_id, team, score, last_update) "
        "VALUES (?,?,?,?,?)",
        (season_id, cell_id, team, score, last_update),
    )
    conn.commit()
    conn.close()


def _set_tile_release_config_on_disk(db_path, *, enabled=None, zero_hours=None,
                                      dry_run=None, max_per_sweep=None):
    conn = sqlite3.connect(db_path)
    if enabled is not None:
        conn.execute("UPDATE tile_release_config SET enabled = ? WHERE id = 1", (int(enabled),))
    if zero_hours is not None:
        conn.execute("UPDATE tile_release_config SET zero_hours = ? WHERE id = 1", (zero_hours,))
    if dry_run is not None:
        conn.execute("UPDATE tile_release_config SET dry_run = ? WHERE id = 1", (int(dry_run),))
    if max_per_sweep is not None:
        conn.execute("UPDATE tile_release_config SET max_per_sweep = ? WHERE id = 1", (max_per_sweep,))
    conn.commit()
    conn.close()


def _config_row(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT enabled, zero_hours, dry_run, max_per_sweep, updated_at "
        "FROM tile_release_config WHERE id = 1"
    ).fetchone()
    conn.close()
    return dict(row)


def _admin_action_rows(db_path, action="tile_release_config_update"):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT actor_account_id, action, detail FROM admin_action_log WHERE action = ? "
        "ORDER BY log_id", (action,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------
# Seeding: exactly once, DB wins thereafter
# ---------------------------------------------------------------------

def test_seed_populates_from_settings_exactly_once(db_path, monkeypatch):
    monkeypatch.setattr(settings, "mc_tile_release_enabled", True)
    monkeypatch.setattr(settings, "mc_tile_release_zero_hours", 333)
    monkeypatch.setattr(settings, "mc_tile_release_dry_run", False)
    monkeypatch.setattr(settings, "mc_tile_release_max_per_sweep", 55)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    seed_tile_release_config_from_env(conn)
    conn.commit()
    conn.close()

    row = _config_row(db_path)
    assert row["enabled"] == 1
    assert row["zero_hours"] == 333
    assert row["dry_run"] == 0
    assert row["max_per_sweep"] == 55
    assert row["updated_at"] != 0

    # A second boot with DIFFERENT settings must never clobber what the
    # first seed (or an operator, indistinguishable to this function)
    # already wrote -- the database wins from here on.
    monkeypatch.setattr(settings, "mc_tile_release_enabled", False)
    monkeypatch.setattr(settings, "mc_tile_release_zero_hours", 9999)
    monkeypatch.setattr(settings, "mc_tile_release_dry_run", True)
    monkeypatch.setattr(settings, "mc_tile_release_max_per_sweep", 1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    seed_tile_release_config_from_env(conn)
    conn.commit()
    conn.close()

    row_after = _config_row(db_path)
    assert row_after == row


def test_load_falls_back_to_settings_when_row_missing(db_path, monkeypatch):
    """Defensive fallback -- should not happen in practice (MIGRATIONS
    always seeds the row), but load_tile_release_config must not raise
    if it somehow is missing."""
    monkeypatch.setattr(settings, "mc_tile_release_enabled", True)
    monkeypatch.setattr(settings, "mc_tile_release_zero_hours", 500)
    monkeypatch.setattr(settings, "mc_tile_release_dry_run", False)
    monkeypatch.setattr(settings, "mc_tile_release_max_per_sweep", 42)

    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM tile_release_config")
    conn.commit()
    conn.row_factory = sqlite3.Row

    cfg = load_tile_release_config(conn)
    conn.close()
    assert cfg == {"enabled": True, "zero_hours": 500, "dry_run": False, "max_per_sweep": 42}


# ---------------------------------------------------------------------
# Sweep reads config fresh -- an edit takes effect with no restart
# ---------------------------------------------------------------------

def test_sweep_picks_up_edited_zero_hours_with_no_restart(db_path):
    season_id = _season_on_disk(db_path)
    # 200 hours abandoned: not expired at the shipped 720h default,
    # but past a 168h (the floor) threshold.
    _seed_cell_on_disk(db_path, season_id, grid_cell_id(LAT, LON), "RED",
                        score=0.0, last_update=NOW - 200 * 3600)
    _set_tile_release_config_on_disk(db_path, enabled=True, dry_run=True, zero_hours=720)

    ingestor = McIngestor()
    summary_before = ingestor._release_expired_tiles_sync()
    assert summary_before["count"] == 0

    # Edit the row directly -- an admin panel save, in effect -- without
    # ever touching `ingestor` or restarting anything.
    _set_tile_release_config_on_disk(db_path, zero_hours=168)

    summary_after = ingestor._release_expired_tiles_sync()
    assert summary_after["count"] == 1


# ---------------------------------------------------------------------
# GET /api/admin/tile_release/config
# ---------------------------------------------------------------------

def test_get_config_returns_seeded_defaults_and_bounds(client, db_path):
    _login_as(client, db_path, role="admin")
    resp = client.get("/api/admin/tile_release/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["zero_hours"] == 720
    assert body["dry_run"] is True
    assert body["max_per_sweep"] == 200
    assert body["zero_hours_floor"] == mc_scoring.TILE_RELEASE_ZERO_HOURS_FLOOR
    assert body["max_per_sweep_floor"] == mc_scoring.TILE_RELEASE_MAX_PER_SWEEP_FLOOR
    assert body["max_per_sweep_ceiling"] == mc_scoring.TILE_RELEASE_MAX_PER_SWEEP_CEILING


# ---------------------------------------------------------------------
# POST /api/admin/tile_release/config -- floor/ceiling enforcement
# ---------------------------------------------------------------------

def test_zero_hours_below_floor_is_rejected(client, db_path):
    _login_as(client, db_path, role="admin")
    resp = client.post("/api/admin/tile_release/config", json={
        "enabled": False, "zero_hours": mc_scoring.TILE_RELEASE_ZERO_HOURS_FLOOR - 1,
        "dry_run": True, "max_per_sweep": 200,
    })
    assert resp.status_code == 400
    assert "zero_hours" in resp.json()["error"]
    # Refused -- the stored row must be untouched.
    assert _config_row(db_path)["zero_hours"] == 720


def test_zero_hours_at_floor_is_accepted(client, db_path):
    _login_as(client, db_path, role="admin")
    resp = client.post("/api/admin/tile_release/config", json={
        "enabled": False, "zero_hours": mc_scoring.TILE_RELEASE_ZERO_HOURS_FLOOR,
        "dry_run": True, "max_per_sweep": 200,
    })
    assert resp.status_code == 200
    assert resp.json()["zero_hours"] == mc_scoring.TILE_RELEASE_ZERO_HOURS_FLOOR


@pytest.mark.parametrize("max_per_sweep", [
    mc_scoring.TILE_RELEASE_MAX_PER_SWEEP_FLOOR - 1,
    mc_scoring.TILE_RELEASE_MAX_PER_SWEEP_CEILING + 1,
    0,
])
def test_max_per_sweep_outside_bounds_is_rejected(client, db_path, max_per_sweep):
    _login_as(client, db_path, role="admin")
    resp = client.post("/api/admin/tile_release/config", json={
        "enabled": False, "zero_hours": 720, "dry_run": True,
        "max_per_sweep": max_per_sweep,
    })
    assert resp.status_code == 400
    assert "max_per_sweep" in resp.json()["error"]
    assert _config_row(db_path)["max_per_sweep"] == 200


# ---------------------------------------------------------------------
# POST /api/admin/tile_release/projection -- read-only preview
# ---------------------------------------------------------------------

def test_projection_counts_and_percentages_are_correct(client, db_path):
    _login_as(client, db_path, role="admin")
    season_id = _season_on_disk(db_path)

    # RED: 2 of 3 expired at a 168h candidate.
    _seed_cell_on_disk(db_path, season_id, grid_cell_id(LAT + 0.01, LON), "RED",
                        score=0.0, last_update=NOW - 200 * 3600)
    _seed_cell_on_disk(db_path, season_id, grid_cell_id(LAT + 0.02, LON), "RED",
                        score=0.0, last_update=NOW - 300 * 3600)
    _seed_cell_on_disk(db_path, season_id, grid_cell_id(LAT + 0.03, LON), "RED",
                        score=0.0, last_update=NOW - 10 * 3600)
    # BLUE: 1 of 2 expired.
    _seed_cell_on_disk(db_path, season_id, grid_cell_id(LAT + 0.04, LON), "BLUE",
                        score=0.0, last_update=NOW - 250 * 3600)
    _seed_cell_on_disk(db_path, season_id, grid_cell_id(LAT + 0.05, LON), "BLUE",
                        score=0.0, last_update=NOW - 5 * 3600)

    resp = client.post("/api/admin/tile_release/projection", json={"zero_hours": 168})
    assert resp.status_code == 200
    body = resp.json()

    assert body["season_id"] == season_id
    assert body["zero_hours"] == 168
    assert body["count"] == 3
    assert body["board_total"] == 5
    assert body["board_pct"] == 60.0

    by_team = {t["team"]: t for t in body["by_team"]}
    assert by_team["RED"] == {"team": "RED", "count": 2, "team_total": 3, "pct": pytest.approx(66.7, abs=0.05)}
    assert by_team["BLUE"] == {"team": "BLUE", "count": 1, "team_total": 2, "pct": 50.0}


def test_projection_is_read_only(client, db_path):
    _login_as(client, db_path, role="admin")
    season_id = _season_on_disk(db_path)
    _seed_cell_on_disk(db_path, season_id, grid_cell_id(LAT, LON), "RED",
                        score=0.0, last_update=NOW - 200 * 3600)

    conn = sqlite3.connect(db_path)
    before_tiles = conn.execute("SELECT COUNT(*) FROM mc_tile").fetchone()[0]
    before_log = conn.execute("SELECT COUNT(*) FROM mc_tile_capture_log").fetchone()[0]
    conn.close()

    resp = client.post("/api/admin/tile_release/projection", json={"zero_hours": 168})
    assert resp.status_code == 200
    assert resp.json()["count"] == 1  # sanity: it did see the expired cell

    conn = sqlite3.connect(db_path)
    after_tiles = conn.execute("SELECT COUNT(*) FROM mc_tile").fetchone()[0]
    after_log = conn.execute("SELECT COUNT(*) FROM mc_tile_capture_log").fetchone()[0]
    conn.close()
    assert after_tiles == before_tiles
    assert after_log == before_log
    # Never touched the config singleton either.
    assert _config_row(db_path)["updated_at"] == 0


def test_projection_with_no_active_season_is_all_zero(client, db_path):
    _login_as(client, db_path, role="admin")
    resp = client.post("/api/admin/tile_release/projection", json={"zero_hours": 720})
    assert resp.status_code == 200
    body = resp.json()
    assert body["season_id"] is None
    assert body["count"] == 0
    assert body["by_team"] == []


def test_projection_rejects_non_positive_zero_hours(client, db_path):
    _login_as(client, db_path, role="admin")
    resp = client.post("/api/admin/tile_release/projection", json={"zero_hours": 0})
    assert resp.status_code == 400


# ---------------------------------------------------------------------
# dry_run OFF -- the destructive transition -- requires confirmation
# ---------------------------------------------------------------------

def test_turning_dry_run_off_without_confirmation_is_409(client, db_path):
    _login_as(client, db_path, role="admin")
    season_id = _season_on_disk(db_path)
    _seed_cell_on_disk(db_path, season_id, grid_cell_id(LAT, LON), "RED",
                        score=0.0, last_update=NOW - 721 * 3600)  # expired at the 720h default

    resp = client.post("/api/admin/tile_release/config", json={
        "enabled": True, "zero_hours": 720, "dry_run": False, "max_per_sweep": 200,
    })
    assert resp.status_code == 409
    assert _config_row(db_path)["dry_run"] == 1  # untouched


def test_turning_dry_run_off_with_wrong_count_is_409(client, db_path):
    _login_as(client, db_path, role="admin")
    season_id = _season_on_disk(db_path)
    _seed_cell_on_disk(db_path, season_id, grid_cell_id(LAT, LON), "RED",
                        score=0.0, last_update=NOW - 721 * 3600)

    resp = client.post("/api/admin/tile_release/config", json={
        "enabled": True, "zero_hours": 720, "dry_run": False, "max_per_sweep": 200,
        "confirm_release_count": 0,  # actual is 1
    })
    assert resp.status_code == 409
    assert _config_row(db_path)["dry_run"] == 1


def test_turning_dry_run_off_with_matching_count_succeeds_and_logs(client, db_path):
    account_id = _login_as(client, db_path, role="admin")
    season_id = _season_on_disk(db_path)
    _seed_cell_on_disk(db_path, season_id, grid_cell_id(LAT, LON), "RED",
                        score=0.0, last_update=NOW - 721 * 3600)

    projection = client.post(
        "/api/admin/tile_release/projection", json={"zero_hours": 720}
    ).json()
    assert projection["count"] == 1

    resp = client.post("/api/admin/tile_release/config", json={
        "enabled": True, "zero_hours": 720, "dry_run": False, "max_per_sweep": 200,
        "confirm_release_count": projection["count"],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is False
    assert body["enabled"] is True

    row = _config_row(db_path)
    assert row["dry_run"] == 0
    assert row["enabled"] == 1
    assert row["updated_at"] != 0

    logs = _admin_action_rows(db_path)
    assert len(logs) == 1
    assert logs[0]["actor_account_id"] == account_id
    detail = logs[0]["detail"]
    assert "dry_run True->False" in detail
    assert "enabled False->True" in detail
    assert "zero_hours 720->720" in detail
    assert "max_per_sweep 200->200" in detail


def test_dry_run_stays_off_requires_no_reconfirmation(client, db_path):
    """Only the True->False transition is gated -- saving other fields
    while dry_run is already False must not demand a fresh confirmation
    every time."""
    _login_as(client, db_path, role="admin")
    _set_tile_release_config_on_disk(db_path, dry_run=False)

    resp = client.post("/api/admin/tile_release/config", json={
        "enabled": True, "zero_hours": 300, "dry_run": False, "max_per_sweep": 50,
    })
    assert resp.status_code == 200
    assert _config_row(db_path)["dry_run"] == 0


def test_turning_dry_run_back_on_requires_no_confirmation(client, db_path):
    _login_as(client, db_path, role="admin")
    _set_tile_release_config_on_disk(db_path, dry_run=False)

    resp = client.post("/api/admin/tile_release/config", json={
        "enabled": True, "zero_hours": 720, "dry_run": True, "max_per_sweep": 200,
    })
    assert resp.status_code == 200
    assert _config_row(db_path)["dry_run"] == 1


def test_every_save_writes_an_audit_log_row_with_before_and_after(client, db_path):
    account_id = _login_as(client, db_path, role="admin")
    resp = client.post("/api/admin/tile_release/config", json={
        "enabled": True, "zero_hours": 200, "dry_run": True, "max_per_sweep": 50,
    })
    assert resp.status_code == 200

    logs = _admin_action_rows(db_path)
    assert len(logs) == 1
    assert logs[0]["actor_account_id"] == account_id
    detail = logs[0]["detail"]
    assert "enabled False->True" in detail
    assert "zero_hours 720->200" in detail
    assert "dry_run True->True" in detail
    assert "max_per_sweep 200->50" in detail


# ---------------------------------------------------------------------
# Auth: reject unauthenticated access when admin_require_auth is on
# ---------------------------------------------------------------------

def test_get_config_rejects_unauthenticated(db_path):
    assert settings.admin_require_auth is True
    # An account holds a role so the admin surface itself is enabled
    # (see _admin_surface_enabled) -- otherwise this would 404 rather
    # than exercise the auth check this test targets.
    _make_account(db_path, role="admin")

    app = FastAPI()
    app.include_router(admin_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    client = TestClient(app)  # no session cookie set

    assert client.get("/api/admin/tile_release/config").status_code == 401
    assert client.post("/api/admin/tile_release/projection", json={"zero_hours": 720}).status_code == 401
    assert client.post("/api/admin/tile_release/config", json={
        "enabled": True, "zero_hours": 720, "dry_run": True, "max_per_sweep": 200,
    }).status_code == 401


def test_tile_release_routes_404_with_no_admin_surface_configured(db_path):
    """No account holds a role and no admin_token is set -- this repo's
    fresh-install default -- so the admin surface is off entirely."""
    app = FastAPI()
    app.include_router(admin_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    client = TestClient(app)

    resp = client.get("/api/admin/tile_release/config")
    assert resp.status_code == 404
