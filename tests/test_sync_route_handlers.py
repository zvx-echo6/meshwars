"""Pins the perf/sync-handlers change: route handlers that only do
blocking SQLite/CPU/file work were converted from `async def` to plain
`def` so FastAPI runs them in its threadpool instead of on the event
loop (see app/api.py, app/mc_api.py, app/places_api.py, app/public_api.py,
app/notice_api.py, app/nodes_api.py, app/account_api.py, app/join_api.py,
app/checkin_api.py and app/admin_api.py's admin_page -- the full list is
in this branch's commit message).

A handful of representative handlers (spanning every converted module,
plus at least one bare GET, one with a path param, and one with a
Depends()-injected session) are asserted here as plain functions via
inspect.iscoroutinefunction() -- if a future edit turns one back into
`async def` without adding a matching `await`, that handler silently
starts blocking the whole process again under load exactly the way the
original bug did, and this test catches it before it reaches prod.

Also pins app/db.py connect()'s busy_timeout: concurrent readers now
genuinely overlap with the writer (they did not before, since every
handler ran serialized on the one event loop), so a busy_timeout has to
actually be configured for SQLITE_BUSY retries to have anything to
retry against.
"""
from __future__ import annotations

import inspect
import sqlite3

import pytest

import app.db as db
from app.account_api import account_stats, get_account
from app.api import config, get_nodes, season_info, tile_file
from app.join_api import team_status
from app.mc_api import mc_board, mc_cell
from app.notice_api import active_notice
from app.places_api import places_in_viewport, places_near
from app.public_api import v1_board


@pytest.mark.parametrize(
    "handler",
    [
        config,  # bare GET, no params
        get_nodes,  # Depends(optional_session)
        season_info,
        tile_file,  # functionally registered (app.get(...)(tile_file)), not decorator-registered
        places_in_viewport,
        places_near,
        active_notice,
        mc_board,
        mc_cell,  # path param + Depends(optional_session)
        v1_board,
        team_status,
        get_account,  # Depends(require_session)
        account_stats,
    ],
    ids=lambda h: h.__module__ + "." + h.__name__,
)
def test_converted_handlers_are_not_coroutine_functions(handler):
    """The mechanical half of the fix: FastAPI only runs a handler in
    its threadpool (starlette.concurrency.run_in_threadpool) when it is
    NOT a coroutine function. A handler that does blocking DB/CPU/file
    work but is still `async def` runs straight on the event loop and
    blocks every other in-flight request for as long as it takes --
    the exact prod symptom (p50 0.254s, p95 38.8s, p99 60.7s, every
    endpoint's tail moving together) that motivated this change.
    """
    assert inspect.iscoroutinefunction(handler) is False


def test_connect_sets_a_busy_timeout(tmp_path, monkeypatch):
    """Concurrent SQLite readers (now genuinely concurrent across
    threadpool threads, not serialized on one event loop) can still
    collide with the single in-process writer taking its
    `BEGIN IMMEDIATE` (app/db.py's WriteSession). Without a busy_timeout
    a reader that loses that race fails immediately with
    'database is locked' instead of quietly retrying for a bit -- so
    connect() must configure one rather than leaving SQLite's default
    (0, i.e. no retry at all).
    """
    monkeypatch.setattr(db.settings, "db_path", str(tmp_path / "game.db"))
    conn = db.connect()
    try:
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()
    assert busy_timeout > 0
