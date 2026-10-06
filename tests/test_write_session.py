"""Regression tests for app.db.WriteSession's lock-leak bug.

`WriteSession.__aenter__` acquires the module-level `_WRITE_LOCK` and then
opens a connection and issues `BEGIN IMMEDIATE`. Python only calls
`__aexit__` when `__aenter__` returns successfully -- so before the fix,
any failure between the lock acquire and the return (a bad db_path from
`connect()`, or `BEGIN IMMEDIATE` losing the race against another
transaction and raising `sqlite3.OperationalError: database is locked`
once the busy_timeout elapses) left `_WRITE_LOCK` held forever. Every
subsequent write anywhere in the process -- ingest, check-ins, scoring,
admin -- would then hang indefinitely.

These tests need a real file-backed database, not `:memory:`, because
the core regression only reproduces with genuine BEGIN IMMEDIATE
contention between two separate connections; two `:memory:` connections
do not see each other's transactions at all.

Async support is not configured anywhere in this repo (no pytest.ini,
no [tool.pytest.ini_options] in a pyproject.toml, no existing
@pytest.mark.asyncio usage), even though pytest-asyncio happens to be
installed as a dependency of something else -- so rather than lean on
that incidentally-present package, these tests drive `WriteSession`'s
coroutine methods with `asyncio.run(...)` from ordinary synchronous
test functions, the same way any other sync caller would have to.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

import app.db as db
from app.db import MIGRATIONS, SCHEMA, WriteSession


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
    """Point app.db's connect() at a fresh temp file-backed database."""
    path = str(tmp_path / "game.db")
    _init_schema(path)
    monkeypatch.setattr(db.settings, "db_path", path)
    return path


def test_lock_released_when_begin_immediate_fails(db_path, monkeypatch):
    # Cut the busy_timeout pragma down to a few milliseconds so the
    # doomed BEGIN IMMEDIATE below fails fast instead of waiting out the
    # real 5000ms default -- the point being tested is what happens when
    # it *does* fail, not how long that takes.
    fast_pragmas = [p if not p.startswith("PRAGMA busy_timeout") else "PRAGMA busy_timeout=20"
                    for p in db.PRAGMAS]
    monkeypatch.setattr(db, "PRAGMAS", fast_pragmas)

    # A second, plain connection holds its own BEGIN IMMEDIATE open,
    # which is what makes WriteSession's own BEGIN IMMEDIATE lose the
    # race and raise "database is locked".
    blocker = sqlite3.connect(db_path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")

    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            asyncio.run(_open_and_close(WriteSession()))

        # The key assertion: __aenter__ raised, so __aexit__ never ran,
        # but the lock must not be left held.
        assert db._WRITE_LOCK.locked() is False
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    # With the blocker gone, the process must have actually recovered:
    # a fresh WriteSession can acquire the lock and write.
    async def _write():
        async with WriteSession() as conn:
            db.set_cursor(conn, "test-key", "test-value")

    asyncio.run(_write())
    assert db._WRITE_LOCK.locked() is False

    check = sqlite3.connect(db_path)
    check.row_factory = sqlite3.Row
    row = check.execute("SELECT v FROM cursor WHERE k = 'test-key'").fetchone()
    check.close()
    assert row["v"] == "test-value"


async def _open_and_close(session: WriteSession) -> None:
    async with session:
        pass  # pragma: no cover -- __aenter__ is expected to raise first


def test_lock_released_when_connect_fails(db_path, monkeypatch):
    def _boom():
        raise OSError("simulated connect() failure")

    monkeypatch.setattr(db, "connect", _boom)

    async def _use():
        async with WriteSession():
            pass  # pragma: no cover -- connect() is expected to raise first

    with pytest.raises(OSError, match="simulated connect"):
        asyncio.run(_use())

    assert db._WRITE_LOCK.locked() is False


def test_normal_path_still_commits(db_path):
    async def _write():
        async with WriteSession() as conn:
            db.set_cursor(conn, "normal-key", "normal-value")

    asyncio.run(_write())

    assert db._WRITE_LOCK.locked() is False

    check = sqlite3.connect(db_path)
    check.row_factory = sqlite3.Row
    row = check.execute("SELECT v FROM cursor WHERE k = 'normal-key'").fetchone()
    check.close()
    assert row["v"] == "normal-value"


# ---- BEGIN IMMEDIATE / COMMIT wait in a worker thread --------------------

def _fast_busy(monkeypatch, ms: int) -> None:
    monkeypatch.setattr(db, "PRAGMAS", [
        p if not p.startswith("PRAGMA busy_timeout") else f"PRAGMA busy_timeout={ms}"
        for p in db.PRAGMAS
    ])


def test_event_loop_keeps_ticking_while_begin_immediate_waits_for_file_lock(db_path, monkeypatch):
    import time

    _fast_busy(monkeypatch, 5000)
    monkeypatch.setattr(db.settings, "db_write_wait_in_thread", True)

    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")

    async def scenario():
        ticks = 0
        stop = False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0)

        async def writer():
            async with WriteSession() as conn:
                conn.execute("INSERT INTO cursor(k, v) VALUES ('tick', 'x')")

        t = asyncio.create_task(ticker())
        w = asyncio.create_task(writer())
        await asyncio.sleep(0.3)  # writer is now blocked on the file lock
        ticks_before = ticks
        await asyncio.sleep(0.3)
        assert not w.done()
        assert ticks - ticks_before > 10, "event loop was frozen by BEGIN IMMEDIATE"
        holder.execute("ROLLBACK")  # release the file lock
        await asyncio.wait_for(w, timeout=3)
        stop = True
        await t

    asyncio.run(scenario())
    holder.close()
    chk = sqlite3.connect(db_path)
    assert chk.execute("SELECT v FROM cursor WHERE k='tick'").fetchone() == ("x",)
    chk.close()


def test_flag_off_keeps_synchronous_begin_immediate(db_path, monkeypatch):
    monkeypatch.setattr(db.settings, "db_write_wait_in_thread", False)

    def boom(*a, **k):
        raise AssertionError("to_thread must not be used with the flag off")

    monkeypatch.setattr(db.asyncio, "to_thread", boom)

    async def scenario():
        async with WriteSession() as conn:
            conn.execute("INSERT INTO cursor(k, v) VALUES ('sync', 'y')")

    asyncio.run(scenario())
    chk = sqlite3.connect(db_path)
    assert chk.execute("SELECT v FROM cursor WHERE k='sync'").fetchone() == ("y",)
    chk.close()


@pytest.mark.parametrize("flag", [True, False])
def test_begin_immediate_locked_error_propagates_and_releases(db_path, monkeypatch, flag):
    _fast_busy(monkeypatch, 20)
    monkeypatch.setattr(db.settings, "db_write_wait_in_thread", flag)
    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")

    async def scenario():
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            async with WriteSession():
                pytest.fail("body must not run")
        assert not db._WRITE_LOCK.locked()

    try:
        asyncio.run(scenario())
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    # connection was released to the pool, not leaked in a transaction
    conn = db.connect()
    assert not conn.in_transaction
    conn.close()


def test_two_concurrent_write_sessions_serialize_in_thread_mode(db_path, monkeypatch):
    monkeypatch.setattr(db.settings, "db_write_wait_in_thread", True)
    inside = 0
    max_inside = 0

    async def writer(key):
        nonlocal inside, max_inside
        async with WriteSession() as conn:
            inside += 1
            max_inside = max(max_inside, inside)
            await asyncio.sleep(0.02)
            conn.execute("INSERT INTO cursor(k, v) VALUES (?, 'w')", (key,))
            inside -= 1

    async def scenario():
        await asyncio.gather(writer("a"), writer("b"), writer("c"))

    asyncio.run(scenario())
    assert max_inside == 1
    chk = sqlite3.connect(db_path)
    assert {r[0] for r in chk.execute("SELECT k FROM cursor WHERE k IN ('a','b','c')")} == {"a", "b", "c"}
    chk.close()


def test_cancel_during_begin_immediate_wait_does_not_leak_transaction(db_path, monkeypatch):
    _fast_busy(monkeypatch, 800)
    monkeypatch.setattr(db.settings, "db_write_wait_in_thread", True)
    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")

    async def scenario():
        async def writer():
            async with WriteSession():
                pass

        w = asyncio.create_task(writer())
        await asyncio.sleep(0.2)
        w.cancel()
        with pytest.raises(asyncio.CancelledError):
            await w
        assert not db._WRITE_LOCK.locked()

    asyncio.run(scenario())
    holder.execute("ROLLBACK")
    holder.close()
    conn = db.connect()
    assert not conn.in_transaction
    conn.execute("BEGIN IMMEDIATE")  # file lock is free again
    conn.execute("ROLLBACK")
    conn.close()
