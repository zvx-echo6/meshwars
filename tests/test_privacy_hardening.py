"""Tests for the privacy-hardening pass: "identity can be public,
location can be public, the link between them cannot" (Matt's rule).

Covers the routes that pass touched:

- GET /api/mc/find, GET /find -- exist solely to answer "where is this
  person"; now require a session (app/sessions.py's require_session),
  with no unauthenticated variant, and are rate-limited per address
  even behind the session gate (app/mc_api.py's _find_rate_limited,
  app/api.py's own separate limiter).
- GET /api/mc/cell/{cell_id}, GET /cell/{cell_id} -- stay public at
  team level (owner, scores, capture timestamps); WHO captured a
  square (recent_captures[].by_display_name) is stripped unless a
  session is present (app/sessions.py's optional_session,
  mc_api._redact_cell_detail).
- GET /get-nodes -- stays public with EXACT node coordinates (Matt's
  explicit call: those positions are already public via the mesh and
  upstream feeds, so this endpoint never withheld them); only the
  `team` field, which JOINS a node to a registered player, is stripped
  unauthenticated.
- GET /team/{node_ref} -- a second, follow-up pass: this one was missed
  the first time through even though it answers the exact same
  person-to-place question /find and /api/mc/find do, just keyed by
  node reference instead of display name. Now gated behind
  require_session() the same way, no unauthenticated variant, same 401
  shape.
- GET /api/v1/cells/{cell_id}, GET /api/v1/captures -- checked as part
  of the same follow-up pass and found NOT to be a gap: an
  X-API-Key holder is deliberately treated as accountable rather than
  anonymous for these two routes (see app/public_api.py's module
  docstring, "one deliberate exception" paragraph, and commit 007db35's
  message). The tests below guard that intent -- proving the display
  name still rides along with a valid key -- so a future pass does not
  "fix" this into a parity gap that was never a bug.
- The public roster (GET /api/mc/players) and other unrelated public
  routes must keep working with no session at all -- the failure mode
  this file guards against as much as the gating itself is
  OVER-gating something that was never person-to-place.

Real file-backed sqlite database, same fixture shape and reasoning as
tests/test_account_api.py/tests/test_sessions.py: app/db.py's
connect()/WriteSession open a fresh connection per call, so ":memory:"
would not share data between the setup code here and the route code
under test. A bare FastAPI app around app/mc_api.py's, app/api.py's and
app/public_api.py's real routers (same "FastAPI-around-one-router"
spirit as tests/test_auth.py's _client_for/tests/test_account_api.py's
`client`) exercises the actual Depends(require_session)/
Depends(optional_session)/_guard() wiring end to end over real HTTP,
not just the dependency functions in isolation.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.api as api_module
import app.db as db
import app.mc_api as mc_api_module
import app.public_api as public_api_module
from app.api import _node_hex
from app.auth import http_exception_as_error_body
from app.db import MIGRATIONS, SCHEMA
from app.mc_ingest import hash_secret
from app.sessions import SESSION_COOKIE_NAME, create_session

NOW = int(time.time())


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
    app.include_router(mc_api_module.router)
    app.include_router(api_module.router)
    app.include_router(public_api_module.router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limiters_and_cache():
    """The find-lookup limiters and the board response cache are all
    module-level singletons (see app/auth.py's module docstring on why
    every _BoundedHits budget in this codebase is built once, at import
    time) -- left dirty, they'd accumulate hits/entries across every
    test in this file and this file's own rate-limit test would trip
    (or fail to trip) depending on test order. Same pattern
    tests/test_account_api.py's own _reset_link_key_rate_limiter uses.

    app/public_api.py's own `_key_cache` (raw key -> label, 60s TTL) and
    `_hits` (its per-key/per-address rate bucket) are the same kind of
    module-level singleton: left dirty, a key inserted into one test's
    tmp_path database would still resolve from a previous test's cache
    entry, or a previous test's request count would carry into this
    one's rate-limit budget.
    """
    mc_api_module._find_addr_rate_limiter._hits.clear()
    api_module._find_addr_rate_limiter._hits.clear()
    mc_api_module._BOARD_CACHE.clear()
    public_api_module._key_cache.clear()
    public_api_module._hits.clear()
    yield
    mc_api_module._find_addr_rate_limiter._hits.clear()
    api_module._find_addr_rate_limiter._hits.clear()
    mc_api_module._BOARD_CACHE.clear()
    public_api_module._key_cache.clear()
    public_api_module._hits.clear()


# ---- DB setup helpers -----------------------------------------------------

def _account(path: str) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (NOW,))
    conn.commit()
    account_id = cur.lastrowid
    conn.close()
    return account_id


def _login(client: TestClient, db_path: str) -> str:
    """Create a fresh account + session and cookie it onto `client` for
    subsequent requests. Returns the raw token (unused by most callers,
    kept for symmetry with tests/test_account_api.py's own _login)."""
    account_id = _account(db_path)
    raw_token = _run(create_session(account_id, device_label="Firefox on Windows"))
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return raw_token


def _player(path: str, player_id: int, team: str, name: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?,?,?,?)",
        (player_id, name, team, NOW),
    )
    conn.commit()
    conn.close()


def _mc_season(path: str, protocol: str, season_id: int = 1) -> int:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO mc_season(id, protocol, started_at, ends_at, status) "
        "VALUES (?,?,?,?,'active')",
        (season_id, protocol, NOW - 1000, NOW + 1_000_000),
    )
    conn.commit()
    conn.close()
    return season_id


def _mc_tile(path: str, season_id: int, cell_id: str, owner_team: str, last_player_id: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts) "
        "VALUES (?,?,?,?,?)",
        (season_id, cell_id, owner_team, last_player_id, NOW),
    )
    conn.commit()
    conn.close()


def _mc_capture_log(
    path: str, season_id: int, cell_id: str, by_player_id: int, by_team: str, from_team: str | None
) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO mc_tile_capture_log(season_id, cell_id, ts, by_player_id, by_team, from_team) "
        "VALUES (?,?,?,?,?,?)",
        (season_id, cell_id, NOW, by_player_id, by_team, from_team),
    )
    conn.commit()
    conn.close()


def _node_seen(path: str, season_id: int, node_id: int, name: str, lat: float, lon: float) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO node_seen(season_id, node_id, name, lat, lon, elev, last_seen) "
        "VALUES (?,?,?,?,?,0,?)",
        (season_id, node_id, name, lat, lon, NOW),
    )
    conn.commit()
    conn.close()


def _api_key(path: str, label: str = "test integration") -> str:
    """Insert a valid, unrevoked app/public_api.py key and return the
    raw value -- only its hash is ever stored, same as app/db.py's
    api_client table comment says, so the raw value has to be handed
    back here rather than looked up later."""
    raw_key = "test-key-" + label.replace(" ", "-")
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO api_client(key_hash, label, created_at) VALUES (?,?,?)",
        (hash_secret(raw_key), label, NOW),
    )
    conn.commit()
    conn.close()
    return raw_key


def _bind_node(path: str, protocol: str, node_ref: str, player_id: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) VALUES (?,?,?,?)",
        (protocol, node_ref, player_id, NOW),
    )
    conn.commit()
    conn.close()


# ---- /find, /api/mc/find: session required, no public variant ------------

@pytest.mark.parametrize("path", ["/api/mc/find", "/find"])
def test_find_requires_a_session(client, path):
    resp = client.get(path, params={"name": "anyone"})
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


@pytest.mark.parametrize(
    "path,protocol",
    [("/api/mc/find", "mc"), ("/find", "mt")],
)
def test_find_works_with_a_session(client, db_path, path, protocol):
    _login(client, db_path)
    _player(db_path, 1, "RED", "wanderer")
    season_id = _mc_season(db_path, protocol)
    _mc_tile(db_path, season_id, "100_-200", "RED", 1)

    resp = client.get(path, params={"name": "wanderer"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "wanderer"
    assert body["team"] == "RED"
    assert body["tiles_held"] == 1
    assert body["bounds"] is not None


@pytest.mark.parametrize("path", ["/api/mc/find", "/find"])
def test_find_still_404s_for_an_unregistered_name_when_signed_in(client, db_path, path):
    _login(client, db_path)
    resp = client.get(path, params={"name": "nobody-by-this-name"})
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "path,limiter_attr,module",
    [
        ("/api/mc/find", "_find_addr_rate_limiter", mc_api_module),
        ("/find", "_find_addr_rate_limiter", api_module),
    ],
)
def test_find_is_rate_limited_per_address_even_with_a_session(
    client, db_path, monkeypatch, path, limiter_attr, module
):
    """The audit's other finding about /find: unthrottled. Session-
    gating alone doesn't fix that -- a signed-in account could still
    script through every display name -- so this must 429 well before
    an attacker gets far, budget or not.
    """
    monkeypatch.setattr(db.settings, "find_rate_limit_attempts", 2)
    monkeypatch.setattr(db.settings, "find_rate_limit_window_seconds", 60)
    _login(client, db_path)

    r1 = client.get(path, params={"name": "x"})
    r2 = client.get(path, params={"name": "y"})
    r3 = client.get(path, params={"name": "z"})
    assert r1.status_code == 404  # budget spent, but this attempt still ran
    assert r2.status_code == 404
    assert r3.status_code == 429
    assert r3.json() == {"error": "rate limited"}


# ---- /team/{node_ref}: same person-to-place link as /find, caught later --
#
# player_node.node_ref is stored in app/node_ref.py's canonical bare
# lowercase form (no leading "!") -- _bind_node below stores that form
# directly, while the route itself is queried with the "!"-prefixed
# form _node_hex() returns, the same way a real caller would type it;
# normalize_node_ref() inside the route is what reconciles the two.

def test_team_lookup_requires_a_session(client, db_path):
    node_ref = _node_hex(1)
    _player(db_path, 1, "RED", "wanderer")
    _bind_node(db_path, "mt", node_ref.lstrip("!"), 1)

    resp = client.get(f"/team/{node_ref}")
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_team_lookup_works_with_a_session(client, db_path):
    _login(client, db_path)
    node_ref = _node_hex(1)
    _player(db_path, 1, "RED", "wanderer")
    _bind_node(db_path, "mt", node_ref.lstrip("!"), 1)
    season_id = _mc_season(db_path, "mt")
    _mc_tile(db_path, season_id, "100_-200", "RED", 1)

    resp = client.get(f"/team/{node_ref}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["display_name"] == "wanderer"
    assert body["team"] == "RED"
    assert body["tiles_owned"] == 1


def test_team_lookup_still_requires_a_session_for_an_unregistered_node(client, db_path):
    """A session is required before the route will even say whether a
    node is registered -- refusing has to happen before the "not a
    registered player radio" branch runs, not after, or an
    unauthenticated caller could still use the found/not-found split to
    enumerate which node references are real."""
    resp = client.get(f"/team/{_node_hex(999)}")
    assert resp.status_code == 401


# ---- /api/mc/cell/{id}, /cell/{id}: team-level public, WHO gated ---------

@pytest.mark.parametrize(
    "path,protocol",
    [("/api/mc/cell/{cid}", "mc"), ("/cell/{cid}", "mt")],
)
def test_cell_detail_omits_by_display_name_when_unauthenticated(client, db_path, path, protocol):
    cell_id = "500_-700"
    _player(db_path, 1, "RED", "capturer")
    season_id = _mc_season(db_path, protocol)
    _mc_tile(db_path, season_id, cell_id, "RED", 1)
    _mc_capture_log(db_path, season_id, cell_id, by_player_id=1, by_team="RED", from_team=None)

    resp = client.get(path.format(cid=cell_id))
    assert resp.status_code == 200
    body = resp.json()

    # Team-level history stays public: owner, capture log entries with
    # team/timestamp, all present.
    assert body["owner_team"] == "RED"
    assert len(body["recent_captures"]) == 1
    assert body["recent_captures"][0]["by_team"] == "RED"
    assert body["recent_captures"][0]["ts"] is not None

    # WHO captured it does not ride along unauthenticated.
    assert "by_display_name" not in body["recent_captures"][0]


@pytest.mark.parametrize(
    "path,protocol",
    [("/api/mc/cell/{cid}", "mc"), ("/cell/{cid}", "mt")],
)
def test_cell_detail_includes_by_display_name_when_authenticated(client, db_path, path, protocol):
    cell_id = "500_-700"
    _login(client, db_path)
    _player(db_path, 1, "RED", "capturer")
    season_id = _mc_season(db_path, protocol)
    _mc_tile(db_path, season_id, cell_id, "RED", 1)
    _mc_capture_log(db_path, season_id, cell_id, by_player_id=1, by_team="RED", from_team=None)

    resp = client.get(path.format(cid=cell_id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["recent_captures"][0]["by_display_name"] == "capturer"


# ---- /get-nodes: exact coordinates always public, team attribution gated --

def test_get_nodes_omits_team_attribution_but_keeps_exact_coordinates_when_unauthenticated(
    client, db_path
):
    season_id = _mc_season(db_path, "mt")
    node_id = 1
    node_ref = _node_hex(node_id)
    _player(db_path, 1, "BLUE", "radio-owner")
    _bind_node(db_path, "mt", node_ref, 1)
    _node_seen(db_path, season_id, node_id, "MyNode", 43.6135, -116.2035)

    resp = client.get("/get-nodes")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["repeaters"]) == 1
    node = body["repeaters"][0]

    # Position and the node's own name are public, unchanged, exact --
    # Matt's explicit call: these are already public via the mesh and
    # upstream feeds, so this endpoint never withheld them.
    assert node["lat"] == 43.6135
    assert node["lon"] == -116.2035
    assert node["name"] == "MyNode"

    # The JOIN to a registered player's team is what's gated.
    assert node["team"] is None


def test_get_nodes_includes_team_attribution_when_authenticated(client, db_path):
    _login(client, db_path)
    season_id = _mc_season(db_path, "mt")
    node_id = 1
    node_ref = _node_hex(node_id)
    _player(db_path, 1, "BLUE", "radio-owner")
    _bind_node(db_path, "mt", node_ref, 1)
    _node_seen(db_path, season_id, node_id, "MyNode", 43.6135, -116.2035)

    resp = client.get("/get-nodes")
    assert resp.status_code == 200
    node = resp.json()["repeaters"][0]
    assert node["lat"] == 43.6135
    assert node["team"] == "BLUE"


def test_get_nodes_coverage_is_always_public_and_team_colored(client, db_path):
    """Guard against over-gating: cell-level territory coloring is
    explicitly NOT person-to-place (a team is not a person) and must
    never require a session."""
    season_id = _mc_season(db_path, "mt")
    _player(db_path, 1, "GREEN", "someone")
    _mc_tile(db_path, season_id, "1_1", "GREEN", 1)

    resp = client.get("/get-nodes")
    assert resp.status_code == 200
    coverage = resp.json()["coverage"]
    assert len(coverage) == 1
    assert coverage[0]["owner_team"] == "GREEN"


# ---- /api/v1/cells/{id}, /api/v1/captures: key-holders are NOT anonymous --
#
# Investigated as part of this same pass and found not to be a gap:
# app/public_api.py's module docstring ("one deliberate exception"
# paragraph) and commit 007db35's message both say plainly that an
# X-API-Key is issued personally by the operator and is accountable
# rather than anonymous -- the same trust /find and the cell routes
# above extend to a signed-in session, this surface extends to a valid
# key instead. These two tests guard that intent by proving the
# behaviour a future pass might mistake for the same bug as /team/
# above and "fix" into a parity gap that was never real.

def test_v1_cell_returns_captor_display_name_with_a_valid_key(client, db_path):
    cell_id = "500_-700"
    raw_key = _api_key(db_path)
    _player(db_path, 1, "RED", "capturer")
    season_id = _mc_season(db_path, "mc")
    _mc_tile(db_path, season_id, cell_id, "RED", 1)
    _mc_capture_log(db_path, season_id, cell_id, by_player_id=1, by_team="RED", from_team=None)

    resp = client.get(f"/api/v1/cells/{cell_id}", headers={"X-API-Key": raw_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["cell"]["recent_captures"][0]["by_display_name"] == "capturer"


def test_v1_captures_returns_player_display_name_with_a_valid_key(client, db_path):
    cell_id = "500_-700"
    raw_key = _api_key(db_path)
    _player(db_path, 1, "RED", "capturer")
    season_id = _mc_season(db_path, "mc")
    _mc_tile(db_path, season_id, cell_id, "RED", 1)
    _mc_capture_log(db_path, season_id, cell_id, by_player_id=1, by_team="RED", from_team=None)

    resp = client.get("/api/v1/captures", headers={"X-API-Key": raw_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["captures"][0]["player"] == "capturer"


def test_v1_cell_requires_a_key_at_all(client, db_path):
    """Unrelated to the display-name question above -- this is the
    _guard() gate every /api/v1/* route already had before this pass
    and still has; included so this file's own coverage of these two
    routes doesn't read as if a key were optional."""
    cell_id = "500_-700"
    season_id = _mc_season(db_path, "mc")
    _mc_tile(db_path, season_id, cell_id, "RED", 1)

    resp = client.get(f"/api/v1/cells/{cell_id}")
    assert resp.status_code == 401


# ---- guard against over-gating: the public roster and board stay open ----

def test_public_roster_still_works_unauthenticated(client, db_path):
    _player(db_path, 1, "RED", "alice")
    _player(db_path, 2, "BLUE", "bob")

    resp = client.get("/api/mc/players")
    assert resp.status_code == 200
    names = {p["display_name"] for p in resp.json()}
    assert names == {"alice", "bob"}
    # Only display_name/team -- no player_id, no location, no key material.
    for row in resp.json():
        assert set(row.keys()) == {"display_name", "team"}


@pytest.mark.parametrize("path", ["/api/mc/board", "/api/mc/season", "/api/mc/scores"])
def test_public_board_routes_still_work_unauthenticated(client, db_path, path):
    resp = client.get(path)
    assert resp.status_code == 200


# ---- `sample` table removal: another privacy-audit finding, dropped -----
# entirely rather than merely gated. It held ~19m-precision position
# history keyed to radio identity (sender_node_id), for radios that were
# never registered with MeshWars at all, and had no deletion anywhere in
# the codebase -- see app/db.py's SCHEMA comment, right before
# node_seen, for the full reasoning. Its only route, /get-samples, was
# already dead code (ingest stopped writing `sample` long before this was
# noticed) and has been removed along with the table.

def test_fresh_schema_has_no_sample_table():
    """A database created from the current SCHEMA never has `sample` at
    all -- unlike the fortress-game tables around it (tile/tile_score/
    tile_capture*), which are deliberately kept, unwritten, for their
    completed-season history, `sample` was dropped outright."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "sample" not in tables


def test_migrations_drop_a_pre_existing_sample_table(tmp_path):
    """A database that still carries the old `sample` table -- built by
    hand here, the same way tests/test_sessions.py's _old_shape_db
    builds the pre-migration account_session shape, since SCHEMA itself
    no longer defines `sample` at all -- gets it dropped by the
    MIGRATIONS list's own DROP TABLE IF EXISTS entry: the same one every
    real boot runs through app/db.py's init_db(). A row is inserted
    first specifically to prove this is a real DROP (data and all), not
    just a schema-shape check against an already-empty table.
    """
    path = str(tmp_path / "pre_migration_sample.db")
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "CREATE TABLE sample ("
        "  season_id INTEGER NOT NULL,"
        "  sample_hash TEXT NOT NULL,"
        "  sender_node_id INTEGER NOT NULL,"
        "  ts INTEGER NOT NULL,"
        "  snr REAL, rssi REAL,"
        "  path_json TEXT NOT NULL DEFAULT '[]',"
        "  observed INTEGER NOT NULL DEFAULT 1,"
        "  PRIMARY KEY (season_id, sample_hash, sender_node_id, ts)"
        ")"
    )
    conn.execute(
        "INSERT INTO sample(season_id, sample_hash, sender_node_id, ts) VALUES (1, 'abc12345', 42, 100)"
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(path)
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                continue
            raise
    conn.commit()

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "sample" not in tables


def test_migrations_are_a_no_op_when_sample_is_already_gone(db_path):
    """db_path's fixture already runs the current SCHEMA+MIGRATIONS (see
    this file's own _init_schema), so `sample` is already absent --
    running MIGRATIONS a second time, exactly what every later boot of
    init_db() does, must not raise on the DROP TABLE IF EXISTS entry.
    """
    conn = sqlite3.connect(db_path)
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)  # must not raise
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                continue
            raise
    conn.commit()
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "sample" not in tables


def test_get_samples_route_is_gone(client):
    """/get-samples used to serve a hardcoded empty response for a table
    that ingest had already stopped writing; now that `sample` itself is
    dropped, the route is removed rather than kept as permanent dead
    weight -- confirmed nothing in frontend/ still calls it."""
    resp = client.get("/get-samples")
    assert resp.status_code == 404
