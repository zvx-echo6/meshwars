"""Tests for app/discord_notify.py -- the exactly-once outbox
(app/db.py's discord_outbox), the fire-and-forget webhook POST, the
month-honors embed builder, and the drain loop.

Every outbound webhook call is intercepted by an httpx.MockTransport
handler, the same pattern tests/test_oauth.py uses for its own
outbound HTTP calls -- there is no real network access anywhere in
this file.

enqueue() tests use the in-memory `conn` fixture (tests/conftest.py):
it is a plain sync function taking an already-open connection, exactly
like app/results.py's freeze_month() calls it. The drain-loop tests
need a real file-backed database instead, same reasoning
tests/test_write_session.py gives for its own db_path fixture:
_drain_once() reads with connect() and records outcomes through
WriteSession, both of which open their own connection, and two
':memory:' connections do not share state at all.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time

import httpx
import pytest

import app.db as db
from app import discord_notify, results
from app.config import settings
from app.db import MIGRATIONS, SCHEMA


def _run(coro):
    return asyncio.run(coro)


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# A generous, deliberately broad emoji range check -- this test only
# needs to catch "did anyone accidentally type an emoji here," not
# classify every Unicode symbol precisely.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FFFF"
    "\U00002600-\U000027BF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "]"
)


def _has_emoji(text: str) -> bool:
    return bool(_EMOJI_RE.search(text))


# ---- announcements_enabled --------------------------------------------


def test_announcements_enabled_false_when_webhook_unset(monkeypatch):
    monkeypatch.setattr(settings, "discord_webhook_announcements", "")
    assert discord_notify.announcements_enabled() is False


def test_announcements_enabled_true_when_webhook_set(monkeypatch):
    monkeypatch.setattr(
        settings, "discord_webhook_announcements",
        "https://discord.test/api/webhooks/1/abc",
    )
    assert discord_notify.announcements_enabled() is True


# ---- enqueue ------------------------------------------------------------


def test_enqueue_is_noop_when_disabled(conn, monkeypatch):
    monkeypatch.setattr(settings, "discord_webhook_announcements", "")
    discord_notify.enqueue(
        conn, kind="month_honors", key="2026-08:mc",
        payload={"embeds": []}, now=int(time.time()),
    )
    rows = conn.execute("SELECT * FROM discord_outbox").fetchall()
    assert rows == []


def test_enqueue_same_key_twice_leaves_exactly_one_row(conn, monkeypatch):
    monkeypatch.setattr(
        settings, "discord_webhook_announcements",
        "https://discord.test/api/webhooks/1/abc",
    )
    now = int(time.time())
    discord_notify.enqueue(
        conn, kind="month_honors", key="2026-08:mc",
        payload={"embeds": [{"title": "first"}]}, now=now,
    )
    discord_notify.enqueue(
        conn, kind="month_honors", key="2026-08:mc",
        payload={"embeds": [{"title": "second"}]}, now=now,
    )
    rows = conn.execute(
        "SELECT payload FROM discord_outbox WHERE kind = 'month_honors' AND key = '2026-08:mc'"
    ).fetchall()
    assert len(rows) == 1
    # INSERT OR IGNORE: the first row written wins, never overwritten by
    # a later duplicate enqueue().
    assert json.loads(rows[0]["payload"])["embeds"][0]["title"] == "first"


def test_enqueue_different_keys_leave_separate_rows(conn, monkeypatch):
    monkeypatch.setattr(
        settings, "discord_webhook_announcements",
        "https://discord.test/api/webhooks/1/abc",
    )
    now = int(time.time())
    discord_notify.enqueue(conn, kind="month_honors", key="2026-08:mc", payload={}, now=now)
    discord_notify.enqueue(conn, kind="month_honors", key="2026-08:mt", payload={}, now=now)
    rows = conn.execute("SELECT key FROM discord_outbox ORDER BY key").fetchall()
    assert [r["key"] for r in rows] == ["2026-08:mc", "2026-08:mt"]


# ---- build_month_honors_embed -------------------------------------------


def _sample_result():
    return {
        "month": "2026-08",
        "protocol": "mc",
        "standings": [
            {"team": "RED", "squares": 120, "checkin_points": 50.0, "explorer_points": 10.0},
            {"team": "BLUE", "squares": 80, "checkin_points": 25.0, "explorer_points": 5.0},
        ],
        "awards": [
            {"award": "largest_territory", "label": "Largest Territory", "scope": "",
             "player_id": None, "player": None, "team": "RED", "value": 120.0,
             "detail": "squares held"},
            {"award": "empire_builder", "label": "Empire Builder", "scope": "",
             "player_id": 7, "player": "zippy", "team": "RED", "value": 90.0,
             "detail": "squares held"},
            {"award": "longest_road", "label": "Longest Road", "scope": "",
             "player_id": None, "player": None, "team": None, "value": None,
             "detail": None},
        ],
    }


def test_build_month_honors_embed_renders_labels_and_standings():
    embed = discord_notify.build_month_honors_embed("2026-08", "mc", _sample_result())
    text = json.dumps(embed)
    assert "MeshCore" in text
    assert "2026-08" in text
    assert "RED: 120 squares held" in text
    assert "BLUE: 80 squares held" in text
    assert "Largest Territory" in text
    assert "Empire Builder" in text
    assert "zippy" in text
    # The unwon Longest Road placeholder (player_id and team both None,
    # see with_placeholders() in app/results.py) has nothing to
    # announce and must not appear anywhere in the embed.
    assert "Longest Road" not in text


def test_build_month_honors_embed_names_meshtastic_protocol():
    embed = discord_notify.build_month_honors_embed("2026-08", "mt", _sample_result())
    assert "Meshtastic" in json.dumps(embed)


def test_build_month_honors_embed_has_no_emoji():
    embed = discord_notify.build_month_honors_embed("2026-08", "mc", _sample_result())
    assert not _has_emoji(json.dumps(embed))


def test_build_month_honors_embed_sets_username():
    embed = discord_notify.build_month_honors_embed("2026-08", "mc", _sample_result())
    assert embed["username"] == "MeshWars"


def test_build_month_honors_embed_username_falls_back_when_setting_blanked(monkeypatch):
    monkeypatch.setattr(settings, "discord_webhook_username", "")
    embed = discord_notify.build_month_honors_embed("2026-08", "mc", _sample_result())
    assert embed["username"] == "MeshWars"


def test_build_month_honors_embed_falls_back_to_award_labels(monkeypatch):
    """A row with no 'label' key still renders a real name, off
    results.AWARD_LABELS -- never the raw award key."""
    result = _sample_result()
    del result["awards"][1]["label"]
    embed = discord_notify.build_month_honors_embed("2026-08", "mc", result)
    assert results.AWARD_LABELS["empire_builder"] in json.dumps(embed)


# ---- drain loop -----------------------------------------------------------


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
    """A fresh temp file-backed database, with a webhook configured --
    see this module's own docstring for why _drain_once needs a real
    file rather than ':memory:'.
    """
    path = str(tmp_path / "game.db")
    _init_schema(path)
    monkeypatch.setattr(db.settings, "db_path", path)
    monkeypatch.setattr(
        settings, "discord_webhook_announcements",
        "https://discord.test/api/webhooks/1/abc",
    )
    return path


def _insert_row(path: str, *, key: str = "2026-08:mc", created_at: int | None = None) -> int:
    now = int(time.time())
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(
        "INSERT INTO discord_outbox(kind, key, payload, created_at) VALUES (?, ?, ?, ?)",
        ("month_honors", key, json.dumps({"embeds": [{"title": "t"}]}),
         created_at if created_at is not None else now),
    )
    row_id = conn.execute("SELECT id FROM discord_outbox WHERE key = ?", (key,)).fetchone()[0]
    conn.close()
    return row_id


def _read_row(path: str, row_id: int) -> tuple:
    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT posted_at, attempts, last_error FROM discord_outbox WHERE id = ?", (row_id,)
    ).fetchone()
    conn.close()
    return row


def test_drain_loop_marks_row_posted_on_2xx(db_path):
    row_id = _insert_row(db_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    async def go():
        async with _mock_client(handler) as client:
            await discord_notify._drain_once(http_client=client)

    _run(go())

    posted_at, attempts, last_error = _read_row(db_path, row_id)
    assert posted_at is not None
    assert attempts == 0
    assert last_error is None


def test_drain_loop_records_failure_and_leaves_posted_at_null(db_path):
    row_id = _insert_row(db_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    async def go():
        async with _mock_client(handler) as client:
            await discord_notify._drain_once(http_client=client)

    _run(go())

    posted_at, attempts, last_error = _read_row(db_path, row_id)
    assert posted_at is None
    assert attempts == 1
    assert last_error is not None and "500" in last_error
    # The webhook URL itself must never end up in the stored error.
    assert "discord.test" not in last_error


def test_drain_loop_never_posts_a_row_past_max_age(db_path, monkeypatch):
    monkeypatch.setattr(settings, "discord_outbox_max_age_hours", 1)
    stale_created_at = int(time.time()) - 2 * 3600
    row_id = _insert_row(db_path, created_at=stale_created_at)

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(204)

    async def go():
        async with _mock_client(handler) as client:
            await discord_notify._drain_once(http_client=client)

    _run(go())

    assert calls == []
    posted_at, attempts, last_error = _read_row(db_path, row_id)
    assert posted_at is None
    assert attempts == 0


def test_drain_loop_gives_up_after_max_attempts(db_path, monkeypatch):
    monkeypatch.setattr(settings, "discord_outbox_max_attempts", 2)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute(
        "INSERT INTO discord_outbox(kind, key, payload, created_at, attempts) "
        "VALUES (?, ?, ?, ?, ?)",
        ("month_honors", "2026-08:mc", json.dumps({"embeds": []}), int(time.time()), 2),
    )
    row_id = conn.execute("SELECT id FROM discord_outbox").fetchone()[0]
    conn.close()

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(204)

    async def go():
        async with _mock_client(handler) as client:
            await discord_notify._drain_once(http_client=client)

    _run(go())

    assert calls == []
    posted_at, attempts, last_error = _read_row(db_path, row_id)
    assert posted_at is None
    assert attempts == 2
