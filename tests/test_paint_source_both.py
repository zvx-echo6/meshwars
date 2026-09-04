"""Tests for the third mt_paint_source value, "both" (2026-09-04): the
site owner asked for meshview position-packet painting to come back on
WITHOUT turning FreqMapper off. Before this, freqmapper_config's
mt_paint_source was a single exclusive choice between "meshview" and
"freqmapper" -- exactly one source ever painted the Meshtastic board.
"both" makes each painter run exactly as if its own value were selected,
concurrently. There is deliberately no dedupe, priority, or "which
source wins" logic anywhere in either path -- app/mc_scoring.py's
existing capture/defense window and per-repeater cooldown already
absorb the same cell being touched from two directions, the same as
they would for two different players painting it -- so these tests only
prove each gate opens/closes correctly per value, not any interaction
between the two painters.

Three groups:

  A. app/ingest.py's _poll_once gate (the per-cycle switch for the
     meshview position poll) -- drives Ingestor._poll_once() with
     _poll_positions/_poll_nodeinfo stubbed out, so only the gate
     boolean itself is under test.
  B. app/ingest.py's _backfill gate (the startup switch) -- same idea,
     proven by whether the fake meshview client's _get() is ever called.
  C. app/freqmapper_ingest.py's _process_one_event gate (the per-event
     switch) -- drives the real scoring path (mc_scoring.apply_paint,
     player_cell_ping) exactly as app/freqmapper_ingest.py's poll loop
     would, using the tests/conftest.py `conn` fixture.
  D. app/admin_ops.py's POST /api/admin/paint validation -- proves
     "both" is accepted and round-trips through GET, the same HTTP-round-
     trip shape tests/test_admin_ops_checkin.py already uses for its one
     real route test.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import mc_scoring
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.freqmapper_ingest import FreqMapperIngestor
from app.ingest import Ingestor
from app.node_ref import normalize_node_ref

NOW = int(time.time())
PROTOCOL = "mt"
LAT, LON = 43.0, -116.0  # well within settings.play_area_* (see app/config.py)


# ---------------------------------------------------------------------
# A + B. app/ingest.py's Ingestor gates
# ---------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Same fixture as tests/test_ingest_integrity_gates.py -- a fresh
    on-disk sqlite file with the real schema, settings.db_path pointed
    at it. Ingestor's gate methods call app.db.connect()/WriteSession,
    both of which always open settings.db_path -- there is no way to
    hand them a connection directly.
    """
    path = tmp_path / "game.db"
    monkeypatch.setattr(settings, "db_path", str(path))
    conn = sqlite3.connect(str(path))
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
    return str(path)


def _set_paint_source(db_path, value):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE freqmapper_config SET mt_paint_source = ?, updated_at = ? WHERE id = 1",
                 (value, NOW))
    conn.commit()
    conn.close()


class _NoPacketsClient:
    """Stands in for app.meshview_client.MeshviewClient. _get() is the
    only method _backfill ever calls before hitting its own
    "no packets on this page" stop condition -- returning an empty list
    immediately means a source that IS allowed to backfill still does
    real work (proven by call_count) without needing a full fake feed.
    """

    def __init__(self):
        self.get_calls = 0

    async def _get(self, path, params):
        self.get_calls += 1
        return {"packets": []}


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("source,expect_positions_polled", [
    ("meshview", True),
    ("freqmapper", False),
    ("both", True),
])
def test_poll_once_gate_by_source(db_path, source, expect_positions_polled):
    """Ingestor._poll_once's position-poll gate: meshview and both call
    _poll_positions(), freqmapper alone does not. _poll_nodeinfo is
    stubbed too (identity/roster, unrelated to this gate, and would
    otherwise hit the network) but is expected to run every time --
    that half of the cycle is unconditional regardless of paint source.
    """
    _set_paint_source(db_path, source)
    ingestor = Ingestor(_NoPacketsClient())

    positions_calls = []
    nodeinfo_calls = []

    async def fake_positions():
        positions_calls.append(True)

    async def fake_nodeinfo():
        nodeinfo_calls.append(True)

    ingestor._poll_positions = fake_positions
    ingestor._poll_nodeinfo = fake_nodeinfo

    _run(ingestor._poll_once())

    assert bool(positions_calls) == expect_positions_polled
    assert nodeinfo_calls  # always runs, regardless of paint source


@pytest.mark.parametrize("source,expect_backfill_runs", [
    ("meshview", True),
    ("freqmapper", False),
    ("both", True),
])
def test_backfill_gate_by_source(db_path, source, expect_backfill_runs):
    """Ingestor._backfill's startup gate: meshview and both actually
    fetch (client._get is called at least once); freqmapper alone skips
    entirely without touching the client.
    """
    _set_paint_source(db_path, source)
    client = _NoPacketsClient()
    ingestor = Ingestor(client)

    _run(ingestor._backfill())

    assert (client.get_calls > 0) == expect_backfill_runs


# ---------------------------------------------------------------------
# C. app/freqmapper_ingest.py's _process_one_event gate
# ---------------------------------------------------------------------

def _seed_player_and_node(conn, player_id=1, node_ref="0a0a0a0a", team="RED"):
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?, ?, ?, ?)",
        (player_id, f"player-{player_id}", team, NOW),
    )
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) VALUES (?, ?, ?, ?)",
        (PROTOCOL, node_ref, player_id, NOW),
    )


def _season_id(conn) -> int:
    conn.execute("BEGIN IMMEDIATE")
    mc_scoring.maybe_roll_season(conn, NOW, PROTOCOL)
    sid = mc_scoring.ensure_active_season(conn, NOW, PROTOCOL)
    conn.execute("COMMIT")
    return sid


def _event(verification_id: str, node_ref: str = "0a0a0a0a") -> dict:
    from datetime import datetime, timezone
    return {
        "verification_id": verification_id,
        "radio_node_id": "!" + node_ref,
        "latitude": LAT,
        "longitude": LON,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }


@pytest.mark.parametrize("source,expect_painted", [
    ("meshview", False),
    ("freqmapper", True),
    ("both", True),
])
def test_process_one_event_gate_by_source(conn, source, expect_painted):
    """The actual score/write gate: only "freqmapper" and "both" paint
    and write player_cell_ping; "meshview" alone processes and dedupes
    the event (freqmapper_verification still gets the row -- see that
    table's comment in app/db.py) but never scores it.
    """
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, player_id=1, node_ref=node_ref, team="RED")
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}

    ingestor = FreqMapperIngestor()
    event = _event(f"verif-{source}", node_ref=node_ref)
    outcome = ingestor._process_one_event(
        conn, event, season_id, registered, NOW,
        source, 1.0, 0.5, "2020-01-01",
    )

    if expect_painted:
        assert outcome == "painted"
    else:
        assert outcome == "skipped_inactive_source"

    rows = conn.execute(
        "SELECT count(*) FROM player_cell_ping WHERE player_id = 1 AND protocol = ?",
        (PROTOCOL,),
    ).fetchone()[0]
    assert (rows > 0) == expect_painted

    # Regardless of outcome, the event is always deduped -- see this
    # gate's own comment in app/freqmapper_ingest.py.
    seen = conn.execute(
        "SELECT count(*) FROM freqmapper_verification WHERE verification_id = ?",
        (f"verif-{source}",),
    ).fetchone()[0]
    assert seen == 1


def test_process_one_event_both_matches_freqmapper_exactly():
    """"both" must score identically to "freqmapper" alone for the same
    event -- same outcome, same resulting team tile score -- since under
    "both" this module runs exactly as if "freqmapper" were selected
    (see this module's docstring). Two independent in-memory databases,
    seeded identically, diverging only in which mt_paint_source value is
    passed in.
    """
    from app.grid import cell_id as grid_cell_id

    def _fresh_conn():
        c = sqlite3.connect(":memory:", isolation_level=None)
        c.row_factory = sqlite3.Row
        c.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                c.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                    continue
                raise
        return c

    node_ref = "0a0a0a0a"
    cell = grid_cell_id(LAT, LON)
    results = {}
    for source in ("freqmapper", "both"):
        c = _fresh_conn()
        _seed_player_and_node(c, player_id=1, node_ref=node_ref, team="RED")
        season_id = _season_id(c)
        registered = {node_ref: (1, "RED")}
        ingestor = FreqMapperIngestor()
        event = _event(f"verif-{source}-match", node_ref=node_ref)
        outcome = ingestor._process_one_event(
            c, event, season_id, registered, NOW,
            source, 1.0, 0.5, "2020-01-01",
        )
        score_row = c.execute(
            "SELECT score FROM mc_tile_score WHERE season_id = ? AND cell_id = ? AND team = 'RED'",
            (season_id, cell),
        ).fetchone()
        results[source] = (outcome, score_row["score"] if score_row else None)
        c.close()

    assert results["freqmapper"][0] == results["both"][0] == "painted"
    assert results["freqmapper"][1] is not None
    assert results["freqmapper"][1] == results["both"][1]


# ---------------------------------------------------------------------
# D. app/admin_ops.py's POST /api/admin/paint validation
# ---------------------------------------------------------------------

def _make_admin_client(tmp_path, monkeypatch):
    import app.db as db
    from fastapi import HTTPException
    from app.admin_ops import router as admin_router
    from app.auth import http_exception_as_error_body
    from app.sessions import SESSION_COOKIE_NAME, create_session

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
    # app/admin_api.py's _role_guard requires an ACTIVATED TOTP row for
    # any admin/operator role at use-time (see that function's own
    # docstring) -- a dummy secret is fine, nothing here decrypts it,
    # same pattern tests/test_admin_roles.py's _make_account uses.
    conn.execute(
        "INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at) "
        "VALUES (?, 'unused', ?, ?)",
        (account_id, NOW, NOW),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(db.settings, "db_path", db_path)

    app = FastAPI()
    app.include_router(admin_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    client = TestClient(app)

    raw_token = asyncio.run(create_session(account_id, device_label=None))
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return client


def _base_payload(mt_paint_source: str) -> dict:
    return {
        "mt_paint_source": mt_paint_source,
        "enabled": False,
        "base_url": "",
        "poll_interval_seconds": 60,
        "page_limit": 200,
        "paint_from": "",
        "points_per_event": 0.5,
        "unique_painter_bonus": 0.5,
    }


def test_admin_paint_update_accepts_both_and_round_trips(tmp_path, monkeypatch):
    client = _make_admin_client(tmp_path, monkeypatch)

    resp = client.post("/api/admin/paint", json=_base_payload("both"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["mt_paint_source"] == "both"

    resp2 = client.get("/api/admin/paint")
    assert resp2.status_code == 200
    assert resp2.json()["config"]["mt_paint_source"] == "both"


def test_admin_paint_update_still_rejects_unknown_value(tmp_path, monkeypatch):
    client = _make_admin_client(tmp_path, monkeypatch)
    resp = client.post("/api/admin/paint", json=_base_payload("nonsense"))
    assert resp.status_code == 400
    assert "meshview" in resp.json()["error"]
    assert "both" in resp.json()["error"]
