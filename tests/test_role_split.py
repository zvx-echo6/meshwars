"""Tests for the web/worker role split: app/config.py's
run_background_tasks (default True -- a single all-in-one process is
still fully supported) gates two things:

  * app/main.py's lifespan() -- whether this process starts any of its
    seven background loops (ingest, mc_ingest's queue-drain worker,
    freqmapper_ingest, the checkin poller, the mqtt subscriber, the
    Discord outbox drain loop, and app/mc_api.py's board cache
    publisher).
  * app/db.py's init_db() -- whether this process performs its startup
    WRITES (the places-seed background thread, and the checkin/
    freqmapper/discord config env bootstraps). Schema creation and
    migrations are NOT gated -- every process needs a migrated schema
    and that DDL is idempotent -- so this file does not test those
    (already covered by every other test file that calls init_db() or
    builds its own schema from SCHEMA/MIGRATIONS directly).

docker-compose.yml runs `meshwars` (the web role, RUN_BACKGROUND_TASKS=
false, `--workers 3`) and `meshwars-worker` (RUN_BACKGROUND_TASKS=true,
exactly one process) against the SAME game.db. That is only safe if
SQLite's own file-level locking (BEGIN IMMEDIATE + PRAGMA busy_timeout)
actually serializes writers across separate OS processes -- the
in-process `_WRITE_LOCK` (an asyncio.Lock, process-local by
construction) cannot help across processes at all. The last test in
this file (test_concurrent_writers_across_separate_processes...) is
what actually proves that, using real multiprocessing.Process workers,
not threads -- threads would share one asyncio event loop and one
`_WRITE_LOCK` and would prove nothing about cross-process contention.

Same style as tests/test_connection_pool.py and
tests/test_write_session.py: a real file-backed database (WAL/
busy_timeout behaviour does not reproduce against ":memory:"), and
asyncio.run(...) from plain sync test functions -- this repo has no
pytest-asyncio configuration (see tests/test_write_session.py's own
docstring).
"""
from __future__ import annotations

import asyncio
import multiprocessing
import sqlite3
from unittest.mock import MagicMock

import pytest

import app.checkin as checkin_module
import app.db as db
import app.discord_interactions as discord_interactions_module
import app.discord_notify as discord_notify_module
import app.freqmapper_ingest as freqmapper_module
import app.main as main_module
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
    path = str(tmp_path / "game.db")
    _init_schema(path)
    monkeypatch.setattr(db.settings, "db_path", path)
    return path


# =========================================================================
# app/db.py init_db(): startup writes gated on run_background_tasks
# =========================================================================

def test_init_db_skips_startup_writes_when_run_background_tasks_false(db_path, monkeypatch):
    monkeypatch.setattr(db.settings, "run_background_tasks", False)

    fake_thread_cls = MagicMock(name="threading.Thread")
    monkeypatch.setattr(db.threading, "Thread", fake_thread_cls)

    checkin_seed = MagicMock()
    freqmapper_seed = MagicMock()
    discord_seed = MagicMock()
    monkeypatch.setattr(checkin_module, "seed_nets_from_env", checkin_seed)
    monkeypatch.setattr(freqmapper_module, "seed_freqmapper_config_from_env", freqmapper_seed)
    monkeypatch.setattr(discord_notify_module, "seed_discord_config_from_env", discord_seed)

    db.init_db()

    # No places-seed thread constructed at all.
    fake_thread_cls.assert_not_called()
    # None of the one-time config bootstraps ran.
    checkin_seed.assert_not_called()
    freqmapper_seed.assert_not_called()
    discord_seed.assert_not_called()

    # Schema creation itself is UNGATED -- still happened.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("SELECT id FROM season LIMIT 1")  # must not raise: table exists
    finally:
        conn.close()


def test_init_db_runs_startup_writes_when_run_background_tasks_true(db_path, monkeypatch):
    monkeypatch.setattr(db.settings, "run_background_tasks", True)

    fake_thread_cls = MagicMock(name="threading.Thread")
    monkeypatch.setattr(db.threading, "Thread", fake_thread_cls)

    checkin_seed = MagicMock()
    freqmapper_seed = MagicMock()
    discord_seed = MagicMock()
    monkeypatch.setattr(checkin_module, "seed_nets_from_env", checkin_seed)
    monkeypatch.setattr(freqmapper_module, "seed_freqmapper_config_from_env", freqmapper_seed)
    monkeypatch.setattr(discord_notify_module, "seed_discord_config_from_env", discord_seed)

    db.init_db()

    fake_thread_cls.assert_called_once()
    _, kwargs = fake_thread_cls.call_args
    assert kwargs.get("name") == "places-seed-load"
    assert kwargs.get("daemon") is True
    fake_thread_cls.return_value.start.assert_called_once()

    checkin_seed.assert_called_once()
    freqmapper_seed.assert_called_once()
    discord_seed.assert_called_once()


# =========================================================================
# app/main.py lifespan(): background loops gated on run_background_tasks
# =========================================================================

class _FakeMeshviewClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FakeLoopOwner:
    """Stand-in for Ingestor/FreqMapperIngestor: a synchronous .stop()
    plus a run_forever() that blocks (via an Event that's never set)
    until the task wrapping it is cancelled -- exactly like the real
    run_forever loops, without needing a real meshview/FreqMapper
    upstream to poll."""

    def __init__(self, *args, **kwargs) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True

    async def run_forever(self) -> None:
        await asyncio.Event().wait()


class _FakeStartStopOwner:
    """Stand-in for McIngestor/CheckinPoller/MqttSubscriber: async
    .start()/.stop(), both just flag-setting, no real task of their
    own created here (the real classes create their own internal task
    on .start() -- this fake only needs to prove WHETHER lifespan
    called .start() at all, not exercise that task's own body)."""

    def __init__(self, *args, **kwargs) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


async def _fake_discord_run_forever() -> None:
    await asyncio.Event().wait()


async def _fake_board_publisher_run_forever() -> None:
    await asyncio.Event().wait()


async def _fake_cancel_all_claimnode_watches() -> None:
    pass


class _FakeApp:
    """lifespan() only ever does `app.state.x = y` -- no other
    attribute of `app` is read. A bare object with a settable `.state`
    is enough; a real FastAPI app would pull in the whole route table
    for no reason this test needs."""

    def __init__(self) -> None:
        class _State:
            pass

        self.state = _State()


@pytest.fixture
def _fake_loop_owners(monkeypatch):
    """Patch every background-loop-owning class/function app/main.py's
    lifespan() constructs, plus the Discord claimnode-watch canceller
    it always awaits at shutdown. Does NOT patch connect()/init_db()/
    load_freqmapper_config -- those run for real against the db_path
    fixture's real (schema-only) database, which is cheap and more
    honest than mocking the DB layer too.
    """
    monkeypatch.setattr(main_module, "MeshviewClient", _FakeMeshviewClient)
    monkeypatch.setattr(main_module, "Ingestor", _FakeLoopOwner)
    monkeypatch.setattr(main_module, "FreqMapperIngestor", _FakeLoopOwner)
    monkeypatch.setattr(main_module, "McIngestor", _FakeStartStopOwner)
    monkeypatch.setattr(main_module, "CheckinPoller", _FakeStartStopOwner)
    monkeypatch.setattr(main_module, "MqttSubscriber", _FakeStartStopOwner)
    monkeypatch.setattr(main_module.discord_notify, "run_forever", _fake_discord_run_forever)
    monkeypatch.setattr(main_module.mc_api, "run_forever", _fake_board_publisher_run_forever)
    monkeypatch.setattr(
        main_module.discord_interactions,
        "cancel_all_claimnode_watches",
        _fake_cancel_all_claimnode_watches,
    )


def test_lifespan_starts_no_loops_when_run_background_tasks_false(db_path, monkeypatch, _fake_loop_owners):
    monkeypatch.setattr(main_module.settings, "run_background_tasks", False)

    app = _FakeApp()

    async def _drive():
        async with main_module.lifespan(app):
            # Inside the running lifespan: none of the five task-
            # creating loops exist, and the two start()/stop() objects
            # were never started.
            assert app.state.ingest_task is None
            assert app.state.freqmapper_task is None
            assert app.state.discord_task is None
            assert app.state.board_publisher_task is None
            assert app.state.mc_ingestor.started is False
            assert app.state.checkin_poller.started is False
            assert app.state.mqtt_subscriber.started is False

            names = {t.get_name() for t in asyncio.all_tasks()}
            for forbidden in ("ingest", "freqmapper-ingest", "discord-outbox", "board-cache-publisher"):
                assert forbidden not in names

        # Shutdown (the `finally` block inside lifespan) must not raise
        # -- see the `async with` above completing normally as the
        # actual assertion; nothing to add here except confirming the
        # stop()/aexit path really ran with nothing to cancel.
        assert app.state.ingestor.stopped is True
        assert app.state.freqmapper_ingestor.stopped is True
        assert app.state.mc_ingestor.stopped is True
        assert app.state.checkin_poller.stopped is True
        assert app.state.mqtt_subscriber.stopped is True
        assert app.state.client.closed is True

    asyncio.run(_drive())


def test_lifespan_starts_all_loops_when_run_background_tasks_true(db_path, monkeypatch, _fake_loop_owners):
    monkeypatch.setattr(main_module.settings, "run_background_tasks", True)

    app = _FakeApp()

    async def _drive():
        async with main_module.lifespan(app):
            assert isinstance(app.state.ingest_task, asyncio.Task)
            assert isinstance(app.state.freqmapper_task, asyncio.Task)
            assert isinstance(app.state.discord_task, asyncio.Task)
            assert isinstance(app.state.board_publisher_task, asyncio.Task)
            assert app.state.mc_ingestor.started is True
            assert app.state.checkin_poller.started is True
            assert app.state.mqtt_subscriber.started is True

            names = {t.get_name() for t in asyncio.all_tasks()}
            for expected in ("ingest", "freqmapper-ingest", "discord-outbox", "board-cache-publisher"):
                assert expected in names

        # Clean shutdown: every task got cancelled and awaited, every
        # stop() ran, nothing raised out of the `async with` above.
        assert app.state.ingestor.stopped is True
        assert app.state.freqmapper_ingestor.stopped is True
        assert app.state.mc_ingestor.stopped is True
        assert app.state.checkin_poller.stopped is True
        assert app.state.mqtt_subscriber.stopped is True
        assert app.state.ingest_task.cancelled()
        assert app.state.freqmapper_task.cancelled()
        assert app.state.discord_task.cancelled()
        assert app.state.board_publisher_task.cancelled()

    asyncio.run(_drive())


# =========================================================================
# Cross-process write contention -- the new risk a web/worker split
# introduces (item 3 of the task this file was written for): with 3 web
# workers plus 1 worker process, _WRITE_LOCK no longer serializes every
# WriteSession write app-wide -- it only serializes within ONE process.
# Across processes, only SQLite's own file locking (BEGIN IMMEDIATE +
# PRAGMA busy_timeout=15000) is left. This proves that alone is enough.
# =========================================================================

def _cp_writer(db_path: str, worker_id: int, n_writes: int, result_queue) -> None:
    """Run in a genuinely separate OS process (see the test below --
    plain multiprocessing.Process, no custom start method, so this
    relies on the platform default, `fork` on Linux). Re-imports
    app.db fresh in that process (fork gives a copy of the already-
    imported module, but with an EMPTY connection pool -- the autouse
    tests/conftest.py fixture drains it before this test runs -- so
    every connect() call here opens a brand new physical connection,
    exactly like a real separate meshwars-web/meshwars-worker
    container would)."""
    import app.db as _db

    _db.settings.db_path = db_path
    errors: list[str] = []
    for i in range(n_writes):
        try:
            conn = _db.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO cursor(k, v) VALUES (?, ?) "
                    "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                    (f"cp_test:{worker_id}:{i}", str(i)),
                )
                conn.execute("COMMIT")
            except Exception as e:  # noqa: BLE001 -- report every failure mode, not just locks
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                errors.append(f"{type(e).__name__}: {e}")
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001 -- connect() itself failing counts too
            errors.append(f"connect() {type(e).__name__}: {e}")
    result_queue.put((worker_id, errors))


def test_concurrent_writers_across_separate_processes_no_lock_errors(db_path):
    """4 processes (matching docker-compose.yml's 3 web `--workers`
    plus 1 meshwars-worker process) hammering BEGIN IMMEDIATE write
    transactions against the SAME file, through app.db.connect() --
    the exact path every real writer in this codebase uses. Asserts
    zero errors (in particular zero "database is locked", the failure
    mode PRAGMA busy_timeout=15000 exists to avoid) and that every
    single write actually landed.
    """
    n_workers = 4
    n_writes = 25

    ctx = multiprocessing.get_context()  # platform default (fork on Linux)
    result_queue = ctx.Queue()
    procs = [
        ctx.Process(target=_cp_writer, args=(db_path, wid, n_writes, result_queue))
        for wid in range(n_workers)
    ]
    for p in procs:
        p.start()

    seen: dict[int, list[str]] = {}
    for _ in range(n_workers):
        worker_id, errors = result_queue.get(timeout=60)
        seen[worker_id] = errors

    for p in procs:
        p.join(timeout=30)
        assert not p.is_alive(), "writer process hung past join timeout"
        assert p.exitcode == 0, f"writer process crashed (exitcode={p.exitcode})"

    assert set(seen) == set(range(n_workers))
    all_errors = [e for errs in seen.values() for e in errs]
    assert all_errors == [], f"cross-process write contention produced errors: {all_errors}"
    assert not any("locked" in e.lower() for e in all_errors)

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM cursor WHERE k LIKE 'cp_test:%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == n_workers * n_writes
