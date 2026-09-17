"""Regression tests for app/db.py's connection pool.

Context: connect() used to open a brand-new physical sqlite3 connection
(full PRAGMA list re-run, including a 1 GiB mmap remap) for every call,
and the caller's own conn.close() would close it -- closing the LAST
connection to a WAL database triggers a checkpoint. py-spy on prod
measured that open+close cycle at ~22% of all real CPU work.

The fix is a free list of idle, already-PRAGMA'd connections with
EXCLUSIVE borrowing: connect() pops one whole connection off the free
list (or opens a fresh one) and hands it to exactly one caller; nobody
else can see it until that caller's close() releases it back. This is
deliberately NOT thread-local reuse -- this app is a single uvicorn
process with no --workers, i.e. one event-loop thread hosts every
concurrently in-flight request coroutine, and several call sites
(app/checkin_api.py's confirm_start/confirm_accept) hold a connection
across a real `await` and then run a manual BEGIN IMMEDIATE on it,
outside app/db.py's _WRITE_LOCK. A thread-local single shared
connection would let two such concurrent callers stomp on each other's
transactions. The free-list design instead preserves this codebase's
actual invariant exactly: every concurrent logical caller holds its own
distinct physical connection, and SQLite's own file-level locking
(BEGIN IMMEDIATE + busy_timeout) serializes writers between them.

These tests use a real file-backed database (not :memory:), same
reasoning as tests/test_write_session.py: the transaction-serialization
tests only reproduce with genuine BEGIN IMMEDIATE contention between
distinct connections, which two :memory: connections don't give you.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

import app.db as db
from app.db import MIGRATIONS, SCHEMA


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


def _real(conn) -> sqlite3.Connection:
    """Unwrap a pooled proxy to the real sqlite3.Connection it wraps
    (or return conn unchanged if it already is one -- e.g. a
    pooled=False borrow)."""
    return getattr(conn, "_real", conn)


# ---- borrow exclusivity -----------------------------------------------

def test_simultaneous_borrows_get_different_connections(db_path):
    """THE core invariant: two callers that both currently hold a
    borrowed connection (neither has released yet) must never see the
    same physical connection. This is what makes it safe for two
    concurrent writers to each run their own BEGIN IMMEDIATE without
    stomping on each other -- see this file's module docstring."""
    conn_a = db.connect()
    conn_b = db.connect()
    try:
        assert _real(conn_a) is not _real(conn_b)
    finally:
        conn_a.close()
        conn_b.close()


def test_released_connection_is_reused(db_path):
    """Proves pooling actually happens: a connection released via
    close() is handed back out by a later connect() call, not
    discarded."""
    conn_a = db.connect()
    real_a = _real(conn_a)
    conn_a.close()

    conn_b = db.connect()
    try:
        assert _real(conn_b) is real_a
    finally:
        conn_b.close()


def test_double_close_does_not_duplicate_in_free_list(db_path):
    """A double close() must not push the same real connection onto the
    free list twice -- that would let two concurrent connect() calls
    hand out the SAME physical connection to two different callers,
    exactly the bug the exclusive-borrow design exists to prevent."""
    conn = db.connect()
    real = _real(conn)
    conn.close()
    conn.close()  # must be a no-op, not a second free-list append

    with db._POOL_LOCK:
        matches = [e for e in db._POOL if e.conn is real]
    assert len(matches) == 1


# ---- transaction leak guard ---------------------------------------------

def test_released_connection_does_not_leak_open_transaction(db_path):
    """The critical correctness property: borrow a connection, write
    WITHOUT committing, close() it (release, not commit), then borrow
    again from the same pool and confirm the uncommitted row is
    invisible and in_transaction is False. A leaked open transaction
    here would poison the NEXT borrower's very first statement."""
    conn = db.connect()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO cursor(k, v) VALUES (?, ?)", ("leak-guard-key", "leak-guard-value")
    )
    assert conn.in_transaction is True
    conn.close()  # release WITHOUT committing

    conn2 = db.connect()
    try:
        assert conn2.in_transaction is False
        row = conn2.execute(
            "SELECT v FROM cursor WHERE k = ?", ("leak-guard-key",)
        ).fetchone()
        assert row is None
    finally:
        conn2.close()


# ---- liveness / resilience ----------------------------------------------

def test_dead_connection_is_transparently_replaced(db_path):
    """A pooled connection must never turn a transient fault into a
    permanent one: if the idle connection in the free list is dead,
    connect() discards it and opens a fresh one instead of raising."""
    conn = db.connect()
    real = _real(conn)
    conn.close()

    # Kill the connection now sitting idle in the free list.
    real.close()
    assert len(db._POOL) == 1

    conn2 = db.connect()
    try:
        # Must be a genuinely new, working connection -- not the dead one.
        assert _real(conn2) is not real
        conn2.execute("SELECT 1")
    finally:
        conn2.close()


# ---- db_path change (the test-suite hazard) -----------------------------

def test_db_path_change_yields_connection_to_new_path(tmp_path, monkeypatch):
    """tests/conftest.py's autouse pool-drain fixture already prevents
    this cross-test, but connect() itself must also handle a db_path
    change WITHIN a test/process: a connection idle in the free list
    that was opened against an old path must never be handed to a
    borrower expecting the CURRENT settings.db_path."""
    path_a = str(tmp_path / "a.db")
    path_b = str(tmp_path / "b.db")
    _init_schema(path_a)
    _init_schema(path_b)

    monkeypatch.setattr(db.settings, "db_path", path_a)
    conn_a = db.connect()
    real_a = _real(conn_a)
    conn_a.close()  # released, tagged with path_a, sitting in the free list

    monkeypatch.setattr(db.settings, "db_path", path_b)
    conn_b = db.connect()
    try:
        assert _real(conn_b) is not real_a
        # Prove it's actually talking to path_b, not path_a.
        conn_b.execute("INSERT INTO cursor(k, v) VALUES ('probe', 'b')")
        check = sqlite3.connect(path_b)
        row = check.execute("SELECT v FROM cursor WHERE k = 'probe'").fetchone()
        check.close()
        assert row == ("b",)
    finally:
        conn_b.close()


# ---- PRAGMAs applied once per real connection ---------------------------

def test_pragmas_applied_once_per_real_connection_not_per_borrow(db_path, monkeypatch):
    """The entire point of the pool: PRAGMAs (including the mmap_size
    remap) run once when a real connection is first created, NOT again
    on every borrow."""
    real_calls = []
    orig = db._make_real_connection

    def _counting(path):
        real_calls.append(path)
        return orig(path)

    monkeypatch.setattr(db, "_make_real_connection", _counting)

    for _ in range(5):
        conn = db.connect()
        conn.execute("SELECT 1")
        conn.close()

    assert len(real_calls) == 1


# ---- concurrency ---------------------------------------------------------

def test_concurrent_readers_and_writers_no_errors(db_path):
    """Drive 50 threads (well past the "10 concurrent passed, 40 failed"
    threshold a prior change shipped and broke on) doing interleaved
    reads and writes through connect(), and assert zero errors, zero
    'database is locked', and the correct final count."""
    N_THREADS = 60
    WRITES_PER_THREAD = 20
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def _worker(i: int) -> None:
        try:
            for j in range(WRITES_PER_THREAD):
                conn = db.connect()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "INSERT INTO cursor(k, v) VALUES (?, ?)",
                        (f"concurrency-{i}-{j}", "x"),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()

                # Interleave a read too.
                conn = db.connect()
                try:
                    conn.execute("SELECT COUNT(*) FROM cursor").fetchone()
                finally:
                    conn.close()
        except BaseException as e:  # noqa: BLE001 - want every failure, incl. asserts
            with errors_lock:
                errors.append(e)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not any(t.is_alive() for t in threads), "a worker thread hung"
    if errors:
        pytest.fail(
            f"{len(errors)} worker error(s), first: {errors[0]!r}\n"
            + "\n".join(f"database is locked" for e in errors if "locked" in str(e))
        )

    check = sqlite3.connect(db_path)
    count = check.execute(
        "SELECT COUNT(*) FROM cursor WHERE k LIKE 'concurrency-%'"
    ).fetchone()[0]
    check.close()
    assert count == N_THREADS * WRITES_PER_THREAD


def test_two_concurrent_writers_each_begin_immediate_still_serialize(db_path):
    """THE regression test for the bug the coordinator caught: this is
    the exact shape of app/checkin_api.py's confirm_start/confirm_accept
    -- a connection is borrowed, held for a moment (simulating the real
    `await` those routes make before their manual BEGIN IMMEDIATE), and
    only then does it open an explicit transaction and write.

    Under the (rejected) thread-local design, two such callers on the
    same thread would share ONE physical connection, and the second's
    BEGIN IMMEDIATE would either raise "cannot start a transaction
    within a transaction" or silently join the first's transaction.
    Under the free-list design, they get two distinct connections, so
    SQLite's own file lock + busy_timeout correctly serializes them:
    both succeed, both writes survive.
    """
    results: dict[str, BaseException | None] = {}

    def _writer(key: str, hold_seconds: float) -> None:
        conn = db.connect()
        try:
            time.sleep(hold_seconds)  # simulate work done before BEGIN IMMEDIATE
            conn.execute("BEGIN IMMEDIATE")
            time.sleep(hold_seconds)  # simulate work done while the txn is open
            conn.execute("INSERT INTO cursor(k, v) VALUES (?, ?)", (key, "written"))
            conn.execute("COMMIT")
            results[key] = None
        except BaseException as e:  # noqa: BLE001
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            results[key] = e
        finally:
            conn.close()

    t1 = threading.Thread(target=_writer, args=("writer-1", 0.05))
    t2 = threading.Thread(target=_writer, args=("writer-2", 0.05))
    t1.start()
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)

    assert results.get("writer-1") is None, results.get("writer-1")
    assert results.get("writer-2") is None, results.get("writer-2")

    check = sqlite3.connect(db_path)
    rows = {
        r[0]
        for r in check.execute(
            "SELECT k FROM cursor WHERE k IN ('writer-1', 'writer-2')"
        )
    }
    check.close()
    assert rows == {"writer-1", "writer-2"}


# ---- WAL growth ----------------------------------------------------------

def test_wal_file_does_not_grow_unboundedly(db_path):
    """Long-lived pooled connections could in principle prevent WAL
    checkpointing, letting the -wal file grow without bound. Drive a
    few thousand writes through the pool and assert the -wal file stays
    under a sane ceiling. If this fails, that is a real finding to
    report (e.g. a periodic PRAGMA wal_checkpoint(TRUNCATE) would be
    the fix), not something to quietly loosen the assertion around.
    """
    N_WRITES = 5000
    CEILING_BYTES = 8 * 1024 * 1024  # a few MiB

    for i in range(N_WRITES):
        conn = db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO cursor(k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                ("wal-growth-key", str(i)),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

    wal_path = db_path + "-wal"
    wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
    assert wal_size < CEILING_BYTES, (
        f"-wal file grew to {wal_size} bytes after {N_WRITES} writes through the "
        f"pool (ceiling {CEILING_BYTES}) -- pooled connections may be preventing "
        f"WAL checkpointing; consider a periodic PRAGMA wal_checkpoint(TRUNCATE)."
    )


def test_connections_are_always_opened_in_wal_mode(db_path):
    """WAL must stay on. Without it SQLite falls back to a rollback
    journal, where a writer takes an exclusive lock on the whole
    database file and readers and writers block each other outright --
    on a ~2 GB database with background pollers writing continuously,
    that serializes the entire application onto one lock. It has bitten
    this project before: the symptom was the site becoming, in the
    operator's words, "super beyond slow", with no single slow endpoint
    to blame, because everything was queued behind the same file lock.

    Nothing errors when WAL is lost -- it just degrades, silently and
    everywhere at once -- so this pins it. journal_mode is persisted in
    the database file rather than per-connection, but the PRAGMA list
    is what establishes it on a fresh database, and this asserts both
    that the PRAGMA is still declared and that a borrowed connection
    actually reports wal.
    """
    assert any(
        "journal_mode" in pragma.lower() and "wal" in pragma.lower()
        for pragma in db.PRAGMAS
    ), "PRAGMA journal_mode=WAL was removed from app/db.py's PRAGMAS"

    conn = db.connect()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()

    unpooled = db.connect(pooled=False)
    try:
        assert unpooled.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        unpooled.close()
