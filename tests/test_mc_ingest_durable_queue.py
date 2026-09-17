"""Tests for the durable MeshCore ingest queue (app/db.py's
mc_ingest_queue table, app/mc_ingest.py's McIngestor).

Before this, McIngestor.submit() did put_nowait() on an in-process
asyncio.Queue: a batch accepted by POST /api/mc/ingest lived only in
that process's heap until app/main.py's lifespan-started worker task
drained it. Every deploy restarts the process, so every pending batch
-- real MeshCore wardriving data -- was silently lost on every deploy,
and the design would not have worked at all for a web/worker process
split (a batch accepted on one process's queue is invisible to a
worker running as a different process). This durable queue survives a
restart and would be visible to any process reading the same database,
without implementing that split here.

Per-ping processing is idempotent -- app/db.py's player_cell_ping has
PRIMARY KEY (player_id, protocol, cell_id, ts), and _process_one_ping's
"INSERT OR IGNORE INTO player_cell_ping" dedup check gates every
downstream effect (scoring, place credit, repeater-observation
recording, last-fix update) -- so the delivery semantics chosen here
are at-least-once: claim, process, DELETE on success; a row left over
from a crash between "batch committed" and "row deleted" is simply
reprocessed, and reprocessing an already-processed ping is a safe
no-op, not a double score. test_reprocessing_the_same_batch_does_not_
double_score below proves that directly, since it is the load-bearing
assumption the whole at-least-once design rests on.

Same style as tests/test_mc_ingest_unknown_type.py and
tests/test_write_session.py: real file-backed sqlite (a durable queue
is meaningless against ":memory:", which a restart would wipe anyway),
McIngestor's real methods driven directly rather than through the
30-second worker loop, and asyncio.run(...) from plain sync test
functions -- this repo has no pytest-asyncio configuration (see
tests/test_write_session.py's own docstring).
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.auth import http_exception_as_error_body
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.mc_ingest import McIngestor, hash_secret

import app.api as api_module

NOW = int(time.time())

# Well within the default play area (settings.play_area_*).
LAT, LON = 43.0, -116.0

PROTOCOL = "mc"


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
    monkeypatch.setattr(settings, "db_path", path)
    return path


def _seed_player(db_path, player_id=1, team="RED"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?, ?, ?, ?)",
        (player_id, f"player-{player_id}", team, NOW),
    )
    conn.commit()
    conn.close()


def _seed_api_key(db_path, raw_key, player_id):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO api_key(key_hash, player_id, issued_at) VALUES (?, ?, ?)",
        (hash_secret(raw_key), player_id, NOW),
    )
    conn.commit()
    conn.close()


def _ping(ping_type="TX", lat=LAT, lon=LON, ts=NOW, contact="deadbeef", **extra):
    ping = {
        "type": ping_type,
        "contact": contact,
        "lat": lat,
        "lon": lon,
        "timestamp": ts,
        "heard_repeats": "cafefeed(3.5)",
    }
    ping.update(extra)
    return ping


def _queue_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM mc_ingest_queue ORDER BY id")]
    conn.close()
    return rows


def _ingest_stat(db_path, player_id=1):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM player_ingest_stat WHERE player_id = ? AND protocol = ?",
        (player_id, PROTOCOL),
    )]
    conn.close()
    totals = {}
    for r in rows:
        for k, v in r.items():
            if isinstance(v, int) and k not in ("player_id", "day"):
                totals[k] = totals.get(k, 0) + v
    return totals


def _mc_tile_paint_count(db_path, cell_id):
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT paint_count FROM mc_tile WHERE cell_id = ?", (cell_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else 0


from app.grid import cell_id as grid_cell_id  # noqa: E402


# ---------------------------------------------------------------------
# 13. Persistence survives a simulated restart
# ---------------------------------------------------------------------

def test_submit_persists_row_and_survives_restart(db_path):
    _seed_player(db_path, player_id=1)

    ingestor = McIngestor()
    accepted = _run(ingestor.submit(1, "keyhash-1", [_ping()], NOW))
    assert accepted is True

    rows = _queue_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["player_id"] == 1
    assert rows[0]["key_hash"] == "keyhash-1"
    assert rows[0]["attempts"] == 0
    assert rows[0]["claimed_at"] is None

    # Simulate a process restart: drop the in-memory McIngestor entirely
    # (no stop(), no graceful anything -- a real restart doesn't get
    # one either) and build a brand new one, the way app/main.py's
    # lifespan does on every boot.
    del ingestor
    fresh = McIngestor()

    # The row must still be on disk, untouched by the "restart".
    rows = _queue_rows(db_path)
    assert len(rows) == 1

    async def _drain_once():
        await fresh._reset_stale_claims()
        claimed = await fresh._claim_batch()
        for row in claimed:
            await fresh._process_queued_row(row)

    _run(_drain_once())

    # Processed and removed from the queue.
    assert _queue_rows(db_path) == []
    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_accepted"] == 1


# ---------------------------------------------------------------------
# 14. Oldest-first claim, delete on success
# ---------------------------------------------------------------------

def test_claim_batch_is_oldest_first_and_deletes_on_success(db_path):
    _seed_player(db_path, player_id=1)
    ingestor = McIngestor()

    for i in range(3):
        _run(ingestor.submit(1, f"keyhash-{i}", [_ping(ts=NOW + i)], NOW))

    rows = _queue_rows(db_path)
    ids_in_insert_order = [r["id"] for r in rows]
    assert ids_in_insert_order == sorted(ids_in_insert_order)

    async def _drain():
        claimed = await ingestor._claim_batch()
        assert [r["id"] for r in claimed] == ids_in_insert_order
        for row in claimed:
            await ingestor._process_queued_row(row)

    _run(_drain())

    assert _queue_rows(db_path) == []
    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_accepted"] == 3


# ---------------------------------------------------------------------
# 15. Queue-full: submit() and the HTTP handler both refuse identically
# ---------------------------------------------------------------------

def test_submit_returns_false_at_queue_max(db_path, monkeypatch):
    monkeypatch.setattr(settings, "mc_queue_max", 2)
    _seed_player(db_path, player_id=1)
    ingestor = McIngestor()

    assert _run(ingestor.submit(1, "k1", [_ping()], NOW)) is True
    assert _run(ingestor.submit(1, "k2", [_ping()], NOW)) is True
    # At capacity: the third submit must be refused, and must not have
    # inserted a row.
    assert _run(ingestor.submit(1, "k3", [_ping()], NOW)) is False
    assert len(_queue_rows(db_path)) == 2


def test_http_queue_full_response_is_unchanged(db_path, monkeypatch):
    """Same behavior the old in-memory queue's QueueFull path produced:
    503 {"error": "queue full"}, and the batch is never accepted."""
    monkeypatch.setattr(settings, "mc_queue_max", 1)
    _seed_player(db_path, player_id=1)
    raw_key = "test-raw-key"
    _seed_api_key(db_path, raw_key, player_id=1)

    app = FastAPI()
    app.include_router(api_module.router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    ingestor = McIngestor()
    app.state.mc_ingestor = ingestor
    client = TestClient(app)

    headers = {"X-API-Key": raw_key}
    resp1 = client.post("/api/mc/ingest", json={"data": [_ping()]}, headers=headers)
    assert resp1.status_code == 202
    assert resp1.json() == {"accepted": 1}

    resp2 = client.post("/api/mc/ingest", json={"data": [_ping()]}, headers=headers)
    assert resp2.status_code == 503
    assert resp2.json() == {"error": "queue full"}
    # The refused batch must not have been persisted.
    assert len(_queue_rows(db_path)) == 1


# ---------------------------------------------------------------------
# 16. A failing row is retried, not lost
# ---------------------------------------------------------------------

def test_failed_row_increments_attempts_and_is_retried_not_lost(db_path, monkeypatch):
    _seed_player(db_path, player_id=1)
    ingestor = McIngestor()
    _run(ingestor.submit(1, "keyhash-1", [_ping()], NOW))

    async def _boom(self, player_id, key_hash, pings, received_at):
        raise RuntimeError("simulated processing failure")

    monkeypatch.setattr(McIngestor, "_process_batch", _boom)

    async def _one_pass():
        claimed = await ingestor._claim_batch()
        assert len(claimed) == 1
        await ingestor._process_queued_row(claimed[0])

    _run(_one_pass())

    rows = _queue_rows(db_path)
    assert len(rows) == 1, "a failed row must never be dropped"
    assert rows[0]["attempts"] == 1
    assert rows[0]["claimed_at"] is None, "claim must be released so it can be retried"
    assert rows[0]["last_error"]


# ---------------------------------------------------------------------
# 17. A poison row is dead-lettered after the threshold, and does not
#     block later rows from draining.
# ---------------------------------------------------------------------

def test_poison_row_is_dead_lettered_and_does_not_block_later_rows(db_path, monkeypatch):
    monkeypatch.setattr(settings, "mc_queue_max_attempts", 2)
    _seed_player(db_path, player_id=1)
    _seed_player(db_path, player_id=2, team="BLUE")
    ingestor = McIngestor()

    # Row 1: poison -- will always fail. Row 2: perfectly normal.
    _run(ingestor.submit(1, "poison-key", [{"poison": True}], NOW))
    _run(ingestor.submit(2, "good-key", [_ping()], NOW))

    original_process_batch = McIngestor._process_batch

    async def _maybe_boom(self, player_id, key_hash, pings, received_at):
        if pings and isinstance(pings[0], dict) and pings[0].get("poison"):
            raise RuntimeError("simulated poison batch")
        return await original_process_batch(self, player_id, key_hash, pings, received_at)

    monkeypatch.setattr(McIngestor, "_process_batch", _maybe_boom)

    async def _drain_pass():
        claimed = await ingestor._claim_batch()
        for row in claimed:
            await ingestor._process_queued_row(row)
        return claimed

    # Pass 1: both rows claimed. Row 1 fails (attempts -> 1), row 2
    # succeeds and is deleted.
    claimed1 = _run(_drain_pass())
    assert {r["player_id"] for r in claimed1} == {1, 2}
    rows = _queue_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["player_id"] == 1
    assert rows[0]["attempts"] == 1

    # Row 2's effect actually happened -- proves row 1's continued
    # failure never blocked it.
    stats2 = _ingest_stat(db_path, player_id=2)
    assert stats2["pings_accepted"] == 1

    # Pass 2: row 1 fails again -> attempts reaches mc_queue_max_attempts
    # (2) -> dead-lettered.
    claimed2 = _run(_drain_pass())
    assert len(claimed2) == 1
    rows = _queue_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["attempts"] == 2

    # Pass 3: the dead-lettered row must no longer be claimed at all --
    # it stops being retried, but is still visible in the table (not
    # silently dropped) for an operator to find.
    claimed3 = _run(_drain_pass())
    assert claimed3 == []
    rows = _queue_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["attempts"] == 2
    assert rows[0]["last_error"]


# ---------------------------------------------------------------------
# 18. End-to-end: POST -> row lands -> drain -> effect happened once,
#     row gone.
# ---------------------------------------------------------------------

def test_end_to_end_post_then_drain_scores_exactly_once(db_path):
    _seed_player(db_path, player_id=1, team="RED")
    raw_key = "e2e-raw-key"
    _seed_api_key(db_path, raw_key, player_id=1)

    app = FastAPI()
    app.include_router(api_module.router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    ingestor = McIngestor()
    app.state.mc_ingestor = ingestor
    client = TestClient(app)

    ping = _ping()
    resp = client.post("/api/mc/ingest", json={"data": [ping]}, headers={"X-API-Key": raw_key})
    assert resp.status_code == 202
    assert resp.json() == {"accepted": 1}

    rows = _queue_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["player_id"] == 1

    async def _drain():
        claimed = await ingestor._claim_batch()
        for row in claimed:
            await ingestor._process_queued_row(row)

    _run(_drain())

    assert _queue_rows(db_path) == []

    cell = grid_cell_id(LAT, LON)
    assert _mc_tile_paint_count(db_path, cell) == 1
    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_accepted"] == 1


# ---------------------------------------------------------------------
# 19. Delivery semantics: at-least-once is safe because processing is
#     idempotent -- reprocessing the same batch must not double-score.
# ---------------------------------------------------------------------

def test_reprocessing_the_same_batch_does_not_double_score(db_path):
    """Simulates exactly the crash window at-least-once delivery
    accepts: a batch's scoring transaction commits, but the process
    dies before the queue row is deleted, so the SAME batch gets
    processed again after restart. This must be a safe no-op, not a
    double score -- see app/db.py's player_cell_ping PRIMARY KEY and
    _process_one_ping's INSERT OR IGNORE dedup, which is what makes
    this true.
    """
    _seed_player(db_path, player_id=1, team="RED")
    ingestor = McIngestor()
    ping = _ping()
    cell = grid_cell_id(LAT, LON)

    _run(ingestor._process_batch(1, "keyhash-1", [ping], NOW))
    assert _mc_tile_paint_count(db_path, cell) == 1
    stats_after_first = _ingest_stat(db_path, player_id=1)
    assert stats_after_first["pings_accepted"] == 1
    assert stats_after_first["pings_duplicate"] == 0

    # Reprocess the identical batch -- as would happen if this row were
    # re-claimed after a crash that hit between commit and DELETE.
    _run(ingestor._process_batch(1, "keyhash-1", [ping], NOW))

    # No second paint, no second accepted ping -- the dedup on
    # player_cell_ping caught it and the ping counted as a duplicate
    # instead.
    assert _mc_tile_paint_count(db_path, cell) == 1
    stats_after_second = _ingest_stat(db_path, player_id=1)
    assert stats_after_second["pings_accepted"] == 1
    assert stats_after_second["pings_duplicate"] == 1
