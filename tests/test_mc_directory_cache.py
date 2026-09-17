"""Tests for app/db.py's mc_directory_cache table and app/checkin.py's
CheckinPoller.directory_snapshot()/_refresh_mc_directory_if_stale --
the DB-backed fallback for a real regression the web/worker role split
(app/config.py's run_background_tasks) introduced.

directory_snapshot() is called from several HTTP routes (the node
picker in app/checkin_api.py, plus app/admin_ops.py, app/account_api.py,
and /claimnode in app/discord_interactions.py), but its cache
(self._mc_directory) is only ever populated by _refresh_mc_directory_
if_stale, which only runs inside CheckinPoller.run_forever()'s own
_poll_mc loop -- a loop that, after the split, runs in exactly ONE
process (the worker, RUN_BACKGROUND_TASKS=true). A web-role process's
own CheckinPoller is constructed but never .start()'d (see
app/main.py's lifespan()), so its in-memory dict is permanently {} --
without this fallback, every one of those routes would serve an
always-empty node picker on the web role, even though the worker has
real, current data one process over.

mc_directory_cache is what lets the worker publish what it fetched
(one row per connector_url, JSON-serialized nodes, wall-clock
fetched_at) for any process to read back. No staleness check on the
read side is deliberate -- see directory_snapshot()'s own docstring: an
out-of-date node picker beats an empty one, and the worker's own
directory_refresh_seconds interval already bounds how stale a row can
realistically get.

Real file-backed database (not ":memory:"), same reasoning as every
other db-touching test file in this repo -- app/db.py's connect() opens
a fresh connection per call, so ":memory:" would not share data between
the "worker publishes" and "web role reads" sides of these tests.
asyncio.run(...) from plain sync test functions -- this repo has no
pytest-asyncio configuration (see tests/test_write_session.py's own
docstring).
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import pytest

import app.checkin as checkin_module
from app.checkin import CheckinPoller
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.meshview_client import MeshviewClient


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


def _seed_cache_row(db_path, connector_url, nodes, fetched_at=None):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO mc_directory_cache(connector_url, nodes, fetched_at) VALUES (?, ?, ?) "
        "ON CONFLICT(connector_url) DO UPDATE SET nodes = excluded.nodes, fetched_at = excluded.fetched_at",
        (connector_url, json.dumps(nodes), fetched_at if fetched_at is not None else int(time.time())),
    )
    conn.commit()
    conn.close()


def _poller() -> CheckinPoller:
    # CheckinPoller's own MeshviewClient argument is never touched by
    # anything in this file (nothing here calls _poll_mt or start()) --
    # a bare, never-.aclose()'d client is fine.
    return CheckinPoller(MeshviewClient())


NODE_A = {"public_key": "aaaaaaaa", "name": "alpha", "role": "REPEATER", "last_heard_epoch": 1}
NODE_B = {"public_key": "bbbbbbbb", "name": "bravo", "role": "CLIENT", "last_heard_epoch": 2}


def _by_key(nodes):
    return sorted(nodes, key=lambda n: n["public_key"])


# ---------------------------------------------------------------------
# Web-role process: in-memory cache is empty, DB fallback answers
# ---------------------------------------------------------------------

def test_web_role_never_started_returns_directory_from_table(db_path):
    """The regression this file exists for: a poller that was
    constructed but never .start()'d (exactly what a web-role process
    does -- see app/main.py's lifespan()) has an empty self._mc_directory,
    yet must still serve whatever the worker last published."""
    _seed_cache_row(db_path, "https://corescope.example", [NODE_A])

    poller = _poller()
    assert poller._mc_directory == {}  # never started, never populated

    assert poller.directory_snapshot("https://corescope.example") == [NODE_A]


def test_empty_table_returns_empty_list_not_raise(db_path):
    poller = _poller()
    assert poller.directory_snapshot("https://nothing-here.example") == []
    assert poller.directory_snapshot() == []


def test_db_fallback_union_across_connectors(db_path):
    _seed_cache_row(db_path, "https://corescope-a.example", [NODE_A])
    _seed_cache_row(db_path, "https://corescope-b.example", [NODE_B])

    poller = _poller()
    assert _by_key(poller.directory_snapshot()) == _by_key([NODE_A, NODE_B])


# ---------------------------------------------------------------------
# In-memory cache is the fast path: no DB read at all once populated
# ---------------------------------------------------------------------

def test_in_memory_fast_path_wins_no_db_read(db_path, monkeypatch):
    # DB deliberately holds something DIFFERENT from the in-memory
    # value, so a wrong answer here would prove the DB was consulted.
    _seed_cache_row(db_path, "https://corescope.example", [NODE_B])

    poller = _poller()
    poller._mc_directory["https://corescope.example"] = [NODE_A]

    calls = []
    real_connect = checkin_module.connect

    def _spy_connect(*a, **kw):
        calls.append((a, kw))
        return real_connect(*a, **kw)

    monkeypatch.setattr(checkin_module, "connect", _spy_connect)

    assert poller.directory_snapshot("https://corescope.example") == [NODE_A]
    assert calls == [], "in-memory hit must never touch connect()/the database"

    assert poller.directory_snapshot() == [NODE_A]
    assert calls == [], "the union form must also skip the database when memory has data"


def test_in_memory_partial_falls_back_per_connector(db_path):
    """In-memory has SOME connector cached but not the one being asked
    for by name -- directory_snapshot(connector_url=...) must still
    fall back to the DB for that specific connector, per-key, not only
    for the all-empty case."""
    _seed_cache_row(db_path, "https://corescope-b.example", [NODE_B])

    poller = _poller()
    poller._mc_directory["https://corescope-a.example"] = [NODE_A]

    assert poller.directory_snapshot("https://corescope-b.example") == [NODE_B]
    assert poller.directory_snapshot("https://corescope-a.example") == [NODE_A]


# ---------------------------------------------------------------------
# End-to-end: a real refresh writes the row; a SEPARATE poller instance
# (standing in for a different process -- its own, unrelated
# CheckinPoller, never told about the first one) reads it back.
# ---------------------------------------------------------------------

class _FakeMcClient:
    def __init__(self, nodes: list[dict]) -> None:
        self._nodes = nodes

    async def fetch_directory(self, limit: int) -> list[dict]:
        return self._nodes


def test_refresh_writes_row_then_a_separate_poller_reads_it_back(db_path, monkeypatch):
    connector_url = "https://corescope.example"

    worker_poller = _poller()
    monkeypatch.setattr(
        worker_poller, "_mc_client_for", lambda kind, url: _FakeMcClient([NODE_A, NODE_B])
    )
    config = {"directory_refresh_seconds": 999, "directory_limit": 50}
    asyncio.run(worker_poller._refresh_mc_directory_if_stale("corescope", connector_url, config))

    # The worker's own in-memory cache is populated, as before this fix.
    assert worker_poller._mc_directory[connector_url] == [NODE_A, NODE_B]

    # A brand new CheckinPoller -- standing in for a wholly separate
    # web-role container that never ran _poll_mc -- reads the SAME row
    # back from the shared database, both call shapes.
    web_role_poller = _poller()
    assert web_role_poller._mc_directory == {}
    assert web_role_poller.directory_snapshot(connector_url) == [NODE_A, NODE_B]
    assert web_role_poller.directory_snapshot() == [NODE_A, NODE_B]

    # And the row really is in the table, wall-clock fetched_at set.
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT nodes, fetched_at FROM mc_directory_cache WHERE connector_url = ?",
        (connector_url,),
    ).fetchone()
    conn.close()
    assert json.loads(row[0]) == [NODE_A, NODE_B]
    assert row[1] > 0
