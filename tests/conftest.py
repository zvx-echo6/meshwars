"""Shared pytest fixtures.

This repo had no automated test suite before "Places Worth Going"
(README's "Project status" section) -- these fixtures exist to make
that feature's own tests possible without spinning up the full app
(no MESHVIEW_BASE_URL, no admin token, no HTTP server). Config env vars
are set here, before app.config is ever imported by anything, since
pydantic-settings reads the environment at import time.
"""
from __future__ import annotations

import os
import re
import sqlite3

os.environ.setdefault("MESHVIEW_BASE_URL", "https://example.invalid")

import pytest

from app import db
from app.db import MIGRATIONS, SCHEMA


@pytest.fixture(autouse=True)
def _drain_db_connection_pool():
    """app/db.py's connection pool (app.db._POOL) is process-global,
    shared by every test in this run -- without this, a connection
    opened (PRAGMA'd, and tagged with a db_path) under one test could be
    handed to a LATER test via the free list. That would break tests
    like test_write_session.py's test_lock_released_when_begin_immediate_fails,
    which monkeypatches db.PRAGMAS and relies on that actually taking
    effect on the next connect() call. Draining before AND after covers
    a test that leaves connections idle in the pool either way.
    """
    db._drain_pool_for_tests()
    yield
    db._drain_pool_for_tests()


@pytest.fixture(autouse=True)
def _no_stray_legacy_places_seed(monkeypatch, tmp_path):
    """Guards every test against a REAL app/reference/places_worth_going.csv.gz
    that may be sitting on disk in this checkout (app/places_seed.py's
    "SEED LOCATION" section -- the file was `git rm --cached` 2026-09-08
    but deliberately left on disk in an existing checkout so that one
    keeps running). Without this, any test that boots the real app
    (app/db.init_db()'s backgrounded places-seed load, e.g. every
    TestClient(app) use) without itself pointing places_seed_path
    somewhere test-local would silently pick up that real, tens-of-
    megabytes, worldwide seed via places_seed._resolve_seed_path's
    legacy fallback -- turning a fast, isolated test into a real,
    multi-minute data load, times however many such tests run.

    tests/test_places_seed.py's own fallback tests re-point
    _LEGACY_DATA_PATH to their own tmp_path location within the test
    body, which simply overrides this default for the duration of that
    one test (same monkeypatch fixture instance, last setattr wins).
    """
    from app import places_seed
    monkeypatch.setattr(places_seed, "_LEGACY_DATA_PATH", str(tmp_path / "no-stray-legacy-seed.csv.gz"))


@pytest.fixture
def conn():
    """An in-memory database with the real schema (app/db.py's SCHEMA +
    MIGRATIONS), autocommit mode -- matching app/db.connect()'s own
    isolation_level=None so code under test (which issues its own
    explicit BEGIN/COMMIT, e.g. app/place_rotation.resolve_week) behaves
    exactly as it does against a real file-backed connection.
    """
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
    yield c
    c.close()


class _RowCountingCursor:
    """Wraps a cursor and counts the rows its caller actually pulls back
    from it -- by iterating, or by fetchone/fetchmany/fetchall -- into the
    record RowCountingConn.execute() made for the statement. Everything
    else on the cursor passes straight through."""

    def __init__(self, cursor, record):
        self._cursor = cursor
        self._record = record

    def __iter__(self):
        return self

    def __next__(self):
        row = next(self._cursor)
        self._record["rows"] += 1
        return row

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is not None:
            self._record["rows"] += 1
        return row

    def fetchmany(self, *args):
        rows = self._cursor.fetchmany(*args)
        self._record["rows"] += len(rows)
        return rows

    def fetchall(self):
        rows = self._cursor.fetchall()
        self._record["rows"] += len(rows)
        return rows

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class RowCountingConn:
    """A spy around a sqlite3 connection, for asserting that code under
    test never pulls back more rows than it needs. A sqlite3.Connection is
    a C object whose methods cannot be monkeypatched in place, so this
    wraps one instead.

    Every statement run through .execute() is recorded in `statements` as
    {"sql", "params", "rows"}, where "rows" is how many rows the caller
    actually read back from it. Everything else -- executemany,
    in_transaction, row_factory -- passes straight through to the wrapped
    connection, and close() is a no-op so a route's connect()/close()
    cycle leaves the shared fixture connection open.
    """

    def __init__(self, conn):
        self._conn = conn
        self.statements: list[dict] = []

    def execute(self, sql, params=()):
        record = {"sql": sql, "params": tuple(params), "rows": 0}
        self.statements.append(record)
        return _RowCountingCursor(self._conn.execute(sql, params), record)

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def week_reads(self, week_start):
        """Rows read back, one entry per statement, by each statement that
        reads place_week DIRECTLY for `week_start` -- i.e. one that starts
        "SELECT <something> FROM place_week" with that week bound. This is
        how the weekly rotation draw is read back as a list of ids (or
        probed for existence). A bigger statement that merely mentions
        place_week inside an EXISTS subquery is not one of these.
        """
        return [
            rec["rows"] for rec in self.statements
            if week_start in rec["params"]
            and re.match(r"\s*SELECT\s+\w+\s+FROM\s+place_week\b", rec["sql"], re.IGNORECASE)
        ]


@pytest.fixture
def counting_conn(conn):
    """The `conn` database again, behind a RowCountingConn. Hand THIS to
    the code under test and keep using `conn` itself for setup and for
    reading results back, so a test's own queries are not counted as the
    code's.
    """
    return RowCountingConn(conn)
