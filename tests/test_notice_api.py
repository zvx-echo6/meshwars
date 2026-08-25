"""Tests for the one-time update notice: the player-facing read route
(app/notice_api.py) and the singleton-row active/inactive semantics the
admin save route (app/admin_ops.py's admin_notice_save) relies on.

Same monkeypatch-the-module's-connect() pattern tests/test_places_api.py
already uses, rather than spinning up a real HTTP client through
app/admin_api.py's token guard.
"""
from __future__ import annotations

import asyncio
import json
import time

import app.notice_api as notice_api_module
from app.notice_api import active_notice

NOW = int(time.time())

# The exact upsert admin_ops.admin_notice_save issues -- exercised
# directly here rather than through the FastAPI route, so these tests
# don't need settings.admin_token configured (pydantic-settings reads it
# at import time in conftest.py, before any per-test override could
# reach it) just to prove the storage model behaves as designed.
_UPSERT_SQL = (
    "INSERT INTO notice(id, version_key, title, body, active, updated_at) "
    "VALUES (1, ?, ?, ?, ?, ?) "
    "ON CONFLICT(id) DO UPDATE SET "
    "  version_key = excluded.version_key, "
    "  title = excluded.title, "
    "  body = excluded.body, "
    "  active = excluded.active, "
    "  updated_at = excluded.updated_at"
)


def _save(conn, version_key, title, body, active):
    conn.execute(_UPSERT_SQL, (version_key, title, body, int(active), NOW))


class _NonClosingConn:
    """active_notice() closes whatever connect() hands it once the
    request finishes -- fine against a real per-request connection, but
    a test that calls it twice against one shared `conn` fixture (to
    prove a second save changes what the first call already saw) would
    have the first call's close() leave the second with a dead handle.
    Same wrapper tests/test_places_api.py uses for the identical reason.
    """

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def close(self):
        pass


def test_no_notice_ever_saved_returns_none(conn, monkeypatch):
    monkeypatch.setattr(notice_api_module, "connect", lambda: conn)
    result = asyncio.run(active_notice())
    assert json.loads(result.body) == {"notice": None}


def test_inactive_notice_returns_none(conn, monkeypatch):
    monkeypatch.setattr(notice_api_module, "connect", lambda: conn)
    _save(conn, "2026-08-25", "Scoring changed", "Read the rules page.", active=False)

    result = asyncio.run(active_notice())
    assert json.loads(result.body) == {"notice": None}


def test_active_notice_returns_version_title_body(conn, monkeypatch):
    monkeypatch.setattr(notice_api_module, "connect", lambda: conn)
    _save(conn, "2026-08-25", "Scoring changed",
          "Points now come from effort, not category.\nSee the rules page.", active=True)

    result = asyncio.run(active_notice())
    data = json.loads(result.body)
    assert data == {
        "notice": {
            "version_key": "2026-08-25",
            "title": "Scoring changed",
            "body": "Points now come from effort, not category.\nSee the rules page.",
        }
    }


def test_saving_again_overwrites_rather_than_accumulating(conn, monkeypatch):
    """Singleton row, not a history table: a second save (a new
    version_key, editing an old draft, or retiring one) must replace
    what was there, never add a second row -- the `notice` table's own
    id CHECK (id = 1) already enforces this at the schema level; this
    proves the upsert the admin route issues actually takes that path
    (ON CONFLICT DO UPDATE) rather than erroring or silently no-op'ing
    on the second insert.
    """
    monkeypatch.setattr(notice_api_module, "connect", lambda: _NonClosingConn(conn))

    _save(conn, "2026-08-25", "Scoring changed", "First cut.", active=True)
    _save(conn, "2026-08-26", "Scoring changed, take two", "Fixed a typo.", active=True)

    rows = conn.execute("SELECT * FROM notice").fetchall()
    assert len(rows) == 1

    result = asyncio.run(active_notice())
    data = json.loads(result.body)
    assert data["notice"]["version_key"] == "2026-08-26"
    assert data["notice"]["title"] == "Scoring changed, take two"


def test_retiring_keeps_the_row_but_stops_serving_it(conn, monkeypatch):
    """Toggling active off (the admin panel's "Stop showing to players")
    must not delete the draft -- turning it back on later should not
    require retyping it -- but it must stop the player-facing route
    from serving it.
    """
    monkeypatch.setattr(notice_api_module, "connect", lambda: _NonClosingConn(conn))

    _save(conn, "2026-08-25", "Scoring changed", "Read the rules page.", active=True)
    assert json.loads(asyncio.run(active_notice()).body)["notice"] is not None

    _save(conn, "2026-08-25", "Scoring changed", "Read the rules page.", active=False)
    assert json.loads(asyncio.run(active_notice()).body) == {"notice": None}

    row = conn.execute("SELECT title, body FROM notice WHERE id = 1").fetchone()
    assert row["title"] == "Scoring changed"
    assert row["body"] == "Read the rules page."
