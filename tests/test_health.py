"""Tests for GET /health (app/main.py).

Before this file existed, health() was `return {"ok": True}` -- a
handler that touches nothing, so it stayed green (single-digit
milliseconds) through a real outage where every user-facing endpoint
was timing out. Docker's own HEALTHCHECK and mw-deploy's poll both
trusted it and both were wrong for the whole outage.

Same monkeypatch-the-module's-connect() + direct async-call pattern
tests/test_notice_api.py and tests/test_places_api.py already use,
rather than spinning up a real TestClient(app) -- app.main's own
`app` object runs a heavy lifespan (ingestors, mqtt subscriber,
places-seed load) that a health-route test has no business paying for.
"""
from __future__ import annotations

import asyncio

import pytest

import app.main as main_module
from app.db import MIGRATIONS, SCHEMA


class _NonClosingConn:
    """health() closes whatever connect() hands it once the request
    finishes -- fine against a real per-request connection, but not
    against the shared in-memory `conn` fixture, which every test in
    this file (and the fixture's own teardown) still needs open
    afterward. Same wrapper tests/test_notice_api.py and
    tests/test_places_api.py use for the identical reason.
    """

    def __init__(self, real):
        self._real = real
        self.closed = False

    def __getattr__(self, name):
        return getattr(self._real, name)

    def close(self):
        self.closed = True


class _ExplodingConnect:
    """A connect() replacement that raises instead of returning a
    connection, standing in for "the DB is not reachable right now"
    (a wedged file lock, a full disk, a corrupt database -- anything).
    Counts calls so tests can also assert the handler actually invoked
    it, not just that it swallowed some hard-coded failure.
    """

    def __init__(self, exc):
        self._exc = exc
        self.calls = 0

    def __call__(self):
        self.calls += 1
        raise self._exc


def _real_conn():
    import sqlite3

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


def test_health_returns_200_and_ok_true_when_db_is_reachable(monkeypatch):
    conn = _real_conn()
    wrapped = _NonClosingConn(conn)
    calls = {"n": 0}

    def fake_connect():
        calls["n"] += 1
        return wrapped

    monkeypatch.setattr(main_module, "connect", fake_connect)
    try:
        result = asyncio.run(main_module.health())
    finally:
        conn.close()

    # A plain dict return from a FastAPI route means "200, this body" --
    # mw-deploy's poller and any client depending on {"ok": true} must
    # keep seeing exactly that key/value on success.
    assert result == {"ok": True}
    assert calls["n"] >= 1
    assert wrapped.closed


def test_health_returns_503_when_the_db_is_unreachable(monkeypatch):
    boom = _ExplodingConnect(RuntimeError("disk I/O error, seriously, don't leak this string"))
    monkeypatch.setattr(main_module, "connect", boom)

    result = asyncio.run(main_module.health())

    assert result.status_code == 503
    import json
    body = json.loads(result.body)
    assert body["ok"] is False
    # Names the failure class, not the message -- no stack trace, no
    # exception text (which could carry a path, a query, a secret) ever
    # reaches the response body.
    assert body["error"] == "RuntimeError"
    assert "disk I/O error" not in result.body.decode()
    assert boom.calls == 1


def test_health_actually_touches_the_db(monkeypatch):
    """The whole point of this change: a future refactor that quietly
    turns health() back into a static `{"ok": True}` must fail a test,
    not just fail silently in production during the next outage. This
    asserts connect() was really called, not merely that a 200 came
    back (a static handler would pass every other test in this file's
    success case too).
    """
    conn = _real_conn()
    wrapped = _NonClosingConn(conn)
    calls = {"n": 0}

    def fake_connect():
        calls["n"] += 1
        return wrapped

    monkeypatch.setattr(main_module, "connect", fake_connect)
    try:
        asyncio.run(main_module.health())
    finally:
        conn.close()

    assert calls["n"] == 1


def test_health_is_200_even_with_zero_season_rows(monkeypatch):
    """A fresh DB (or one mid places-seed-load) with no season row yet
    is a legitimately servable app -- health() checks "the DB is
    reachable and responsive", not "the data is complete", so this must
    stay 200 rather than treating an empty result as failure.
    """
    conn = _real_conn()
    assert conn.execute("SELECT id FROM season LIMIT 1").fetchone() is None

    wrapped = _NonClosingConn(conn)
    monkeypatch.setattr(main_module, "connect", lambda: wrapped)
    try:
        result = asyncio.run(main_module.health())
    finally:
        conn.close()

    assert result == {"ok": True}
