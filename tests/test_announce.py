"""Tests for app/announce.py -- the missing "when" between
app/announce_content.py's Content builders and app/mesh_render.py's
renderer: _daily_period() (pure date math), the four providers
(daily_provider, weekly_provider, month_provider, net_wrapup_provider),
check_due() (the provider loop, mirroring app/discord_notify.py's
check_due_time_driven()), and maybe_run() (its own interval-gated
wrapper, mirroring app/discord_notify.py's _check_due_time_driven_once()
/ app/discord_leaderboard.py's maybe_run_leaderboard()).

Fixed, known-good instants are used throughout rather than real
time.time(), so a test's outcome never depends on when the suite
happens to run -- same convention tests/test_discord_notify.py's own
weekly-recap and net-wrap-up sections already use.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import app.db as db
from app import announce, results
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.grid import cell_id

TZ = ZoneInfo(settings.checkin_net_timezone)


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
    conn.close()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """A fresh temp file-backed database -- only maybe_run()'s own tests
    need this (it opens a real WriteSession, which reads settings.db_path
    via app/db.py's connect()); every provider/check_due() test below
    uses the in-memory `conn` fixture (tests/conftest.py) instead, same
    split tests/test_discord_leaderboard.py's own db_path fixture and
    tests/test_discord_notify.py's check_due_time_driven() tests make.
    """
    path = str(tmp_path / "game.db")
    _init_schema(path)
    monkeypatch.setattr(db.settings, "db_path", path)
    return path


@pytest.fixture(autouse=True)
def _reset_announce_gate(monkeypatch):
    """announce._last_announce_check_at is module-level, mutated by
    maybe_run() itself -- reset before every test so one test's timing
    can never bleed into the next, same reasoning
    tests/test_discord_leaderboard.py's own _reset_leaderboard_gate
    fixture (for _last_leaderboard_run_at) and
    tests/test_discord_bot.py's own _reset_reconcile_gate fixture (for
    _last_reconcile_gate_at) already give for their own module globals.
    """
    monkeypatch.setattr(announce, "_last_announce_check_at", 0.0)


# ---- seed helpers, self-contained per tests/test_announce_content.py's
# own convention -------------------------------------------------------


def _player(conn, player_id, team, name=None):
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?,?,?,?)",
        (player_id, name or f"player-{player_id}", team, 0),
    )


def _season(conn, protocol, started_at=0, ends_at=None, status="active"):
    ends_at = ends_at if ends_at is not None else started_at + 100_000_000
    cur = conn.execute(
        "INSERT INTO mc_season(protocol, started_at, ends_at, status) VALUES (?,?,?,?)",
        (protocol, started_at, ends_at, status),
    )
    return cur.lastrowid


def _capture(conn, season_id, cell, ts, player_id, team):
    conn.execute(
        "INSERT INTO mc_tile_capture_log(season_id, cell_id, ts, by_player_id, by_team, "
        "from_team, by_air) VALUES (?,?,?,?,?,NULL,0)",
        (season_id, cell, ts, player_id, team),
    )


def _net(conn, net_id, protocol="mc", timezone="America/Boise", start_hour=18, end_hour=20,
         weekday=2, label="Wednesday Net", enabled=1):
    conn.execute(
        "INSERT INTO checkin_net(id, label, protocol, kind, connector_url, weekday, "
        " start_hour, end_hour, timezone, enabled, created_at) "
        "VALUES (?,?,?,'corescope','http://x',?,?,?,?,?,0)",
        (net_id, label, protocol, weekday, start_hour, end_hour, timezone, enabled),
    )
    return conn.execute("SELECT * FROM checkin_net WHERE id = ?", (net_id,)).fetchone()


def _checkin_award(conn, season_id, player_id, net_date, net_id, streak=1, protocol="mc"):
    conn.execute(
        "INSERT INTO mc_checkin_award(season_id, player_id, net_date, points, protocol, "
        "message_id, awarded_at, streak, net_id) VALUES (?,?,?,25,?,?,0,?,?)",
        (season_id, player_id, net_date, protocol, f"msg-{player_id}-{net_date}", streak, net_id),
    )


def _freeze(conn, protocol, month, closed_at):
    """A bare month_result row -- the freeze MARKER only (what every
    provider/builder here actually checks), without app/results.py's
    freeze_month()'s heavier standings/awards/Discord-enqueue side
    effects, which none of these tests need.
    """
    conn.execute(
        "INSERT INTO month_result(month, protocol, closed_at) VALUES (?,?,?)",
        (month, protocol, closed_at),
    )


def _announcement_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM announcement").fetchone()["n"]


# ---- _daily_period ---------------------------------------------------


def test_daily_period_returns_most_recently_completed_day():
    now = int(datetime(2026, 9, 20, 10, 30, tzinfo=TZ).timestamp())
    iso_date, start_ts, end_ts = announce._daily_period(now)
    assert iso_date == "2026-09-19"
    assert end_ts == int(datetime(2026, 9, 20, 0, 0, tzinfo=TZ).timestamp())
    assert start_ts == int(datetime(2026, 9, 19, 0, 0, tzinfo=TZ).timestamp())


def test_daily_period_stable_across_calls_within_the_same_day():
    early = int(datetime(2026, 9, 20, 0, 0, 1, tzinfo=TZ).timestamp())
    late = int(datetime(2026, 9, 20, 23, 59, 59, tzinfo=TZ).timestamp())
    assert announce._daily_period(early) == announce._daily_period(late)


def test_daily_period_different_days_get_different_keys():
    d1 = announce._daily_period(int(datetime(2026, 9, 20, 12, tzinfo=TZ).timestamp()))
    d2 = announce._daily_period(int(datetime(2026, 9, 21, 12, tzinfo=TZ).timestamp()))
    assert d1[0] != d2[0]


# ---- daily_provider ----------------------------------------------------


def test_daily_provider_produces_content_when_a_team_gains_ground(conn):
    now = int(datetime(2026, 9, 20, 10, 0, tzinfo=TZ).timestamp())
    _, start_ts, _end_ts = announce._daily_period(now)
    _player(conn, 1, "RED")
    _season(conn, "mc")
    _capture(conn, 1, cell_id(43.0, -116.0), start_ts + 10, 1, "RED")

    items = announce.daily_provider(conn, now)
    assert len(items) == 1
    assert items[0]["kind"] == "daily_recap"
    assert items[0]["board"] == "mc"


def test_daily_provider_no_active_season_yields_nothing(conn):
    now = int(datetime(2026, 9, 20, 10, 0, tzinfo=TZ).timestamp())
    assert announce.daily_provider(conn, now) == []


def test_daily_provider_active_season_but_quiet_day_yields_nothing(conn):
    now = int(datetime(2026, 9, 20, 10, 0, tzinfo=TZ).timestamp())
    _season(conn, "mc")
    assert announce.daily_provider(conn, now) == []


def test_daily_provider_not_due_once_a_row_for_that_key_exists(conn):
    now = int(datetime(2026, 9, 20, 10, 0, tzinfo=TZ).timestamp())
    iso_date, start_ts, _end_ts = announce._daily_period(now)
    _player(conn, 1, "RED")
    _season(conn, "mc")
    _capture(conn, 1, cell_id(43.0, -116.0), start_ts + 10, 1, "RED")

    first = announce.daily_provider(conn, now)
    assert len(first) == 1
    conn.execute(
        "INSERT INTO announcement(kind, key, board, net_id, content, created_at) "
        "VALUES ('daily_recap', ?, 'mc', NULL, '{}', 0)",
        (f"{iso_date}:mc",),
    )
    assert announce.daily_provider(conn, now) == []


def test_daily_provider_age_cutoff_skips_a_stale_day(conn, monkeypatch):
    """The daily period is always < 24h stale by construction (see
    _daily_period()'s own docstring), so under the real 72h default
    this cutoff never fires for a daily period -- tightened here to
    prove the cutoff logic itself actually applies to this provider,
    not just to month_provider()."""
    monkeypatch.setattr(settings, "announcement_max_age_hours", 1)
    now = int(datetime(2026, 9, 20, 23, 0, tzinfo=TZ).timestamp())  # ~23h since yesterday ended
    _player(conn, 1, "RED")
    _season(conn, "mc")
    _, start_ts, _ = announce._daily_period(now)
    _capture(conn, 1, cell_id(43.0, -116.0), start_ts + 10, 1, "RED")
    assert announce.daily_provider(conn, now) == []


# ---- weekly_provider -----------------------------------------------------


def test_weekly_provider_produces_content_when_due(conn):
    """build_weekly_content() (app/announce_content.py) only reports a
    RANK CHANGE (never a bare gain, unlike the daily builder) -- so
    proving weekly_provider() is due needs an actual rank swap across
    the window: BLUE ahead before the week starts, RED overtaking it
    during the week.
    """
    now = int(datetime(2026, 9, 20, 12, 0, tzinfo=TZ).timestamp())  # a Sunday
    period_key, week_start, _week_end = announce.discord_notify._weekly_recap_period(now)
    _player(conn, 1, "RED")
    _player(conn, 2, "BLUE")
    _season(conn, "mc")
    _capture(conn, 1, cell_id(43.0, -116.0), week_start - 200, 2, "BLUE")
    _capture(conn, 1, cell_id(43.1, -116.0), week_start - 100, 2, "BLUE")
    _capture(conn, 1, cell_id(43.2, -116.0), week_start - 50, 1, "RED")
    _capture(conn, 1, cell_id(43.3, -116.0), week_start + 10, 1, "RED")
    _capture(conn, 1, cell_id(43.4, -116.0), week_start + 20, 1, "RED")
    _capture(conn, 1, cell_id(43.5, -116.0), week_start + 30, 1, "RED")

    items = announce.weekly_provider(conn, now)
    assert len(items) == 1
    assert items[0]["kind"] == "weekly_recap"
    assert items[0]["key"] == f"{period_key}:mc"


def test_weekly_provider_quiet_week_yields_nothing(conn):
    now = int(datetime(2026, 9, 20, 12, 0, tzinfo=TZ).timestamp())
    _season(conn, "mc")
    assert announce.weekly_provider(conn, now) == []


def test_weekly_provider_age_cutoff_skips_a_stale_week(conn):
    """Under the real 72h default, a week checked more than 3 days after
    it ended is stale news and must not be announced -- self-healing
    (discord_notify._weekly_recap_period()) still finds the period, but
    THE BACKLOG RULE stops it here."""
    sunday = datetime(2026, 9, 13, 12, 0, tzinfo=TZ)
    now = int((sunday + timedelta(days=4)).timestamp())  # Thursday, >72h later
    _player(conn, 1, "RED")
    _player(conn, 2, "BLUE")
    _season(conn, "mc")
    _, week_start, _week_end = announce.discord_notify._weekly_recap_period(now)
    # Same rank-swap shape as the "due" test above -- would otherwise be
    # a real, reportable Content, if not for the age cutoff.
    _capture(conn, 1, cell_id(43.0, -116.0), week_start - 200, 2, "BLUE")
    _capture(conn, 1, cell_id(43.1, -116.0), week_start - 100, 2, "BLUE")
    _capture(conn, 1, cell_id(43.2, -116.0), week_start - 50, 1, "RED")
    _capture(conn, 1, cell_id(43.3, -116.0), week_start + 10, 1, "RED")
    _capture(conn, 1, cell_id(43.4, -116.0), week_start + 20, 1, "RED")
    _capture(conn, 1, cell_id(43.5, -116.0), week_start + 30, 1, "RED")
    assert announce.weekly_provider(conn, now) == []


# ---- month_provider --------------------------------------------------


def test_month_provider_produces_content_for_a_recently_frozen_month(conn):
    now = int(datetime(2026, 9, 2, 10, 0, tzinfo=TZ).timestamp())  # 2 days into September
    _freeze(conn, "mc", "2026-08", closed_at=now - 3600)  # closed ~1h ago

    items = announce.month_provider(conn, now)
    assert len(items) == 1
    assert items[0]["kind"] == "month_honors"
    assert items[0]["key"] == "2026-08:mc"


def test_month_provider_no_frozen_month_yields_nothing(conn):
    now = int(datetime(2026, 9, 2, 10, 0, tzinfo=TZ).timestamp())
    assert announce.month_provider(conn, now) == []


def test_month_provider_not_due_once_a_row_for_that_key_exists(conn):
    now = int(datetime(2026, 9, 2, 10, 0, tzinfo=TZ).timestamp())
    _freeze(conn, "mc", "2026-08", closed_at=now - 3600)
    first = announce.month_provider(conn, now)
    assert len(first) == 1
    conn.execute(
        "INSERT INTO announcement(kind, key, board, net_id, content, created_at) "
        "VALUES ('month_honors', '2026-08:mc', 'mc', NULL, '{}', 0)"
    )
    assert announce.month_provider(conn, now) == []


def test_month_provider_age_cutoff_skips_several_old_frozen_months(conn):
    """THE BACKLOG RULE, explicitly, for month_provider(): several old
    frozen months, none ever announced, must never all land at once --
    or at all -- once every one of them ended more than
    settings.announcement_max_age_hours ago. Only the single most
    recently frozen month per board is even considered (see
    month_provider()'s own docstring), and even that one is skipped once
    it is this stale."""
    now = int(time.time())  # whenever this test happens to run, 2020 is always ancient
    for i, month in enumerate(["2020-01", "2020-02", "2020-03", "2020-04"]):
        _freeze(conn, "mc", month, closed_at=1577836800 + i * 86400)  # 2020-01-01 + i days

    assert announce.month_provider(conn, now) == []


def test_month_provider_uses_its_own_longer_backlog_window(conn):
    """settings.announcement_month_max_age_hours (default 168h/7d), NOT
    the shared settings.announcement_max_age_hours (72h/3d) every other
    provider uses -- see that setting's own comment in app/config.py: a
    lost month announcement can never be re-created (a re-freeze of the
    same month is a silent no-op against the `announcement` table's
    exactly-once (kind, key) index), so the month gets real headroom
    over an ordinary outage. A month whose own window ended 5 days ago
    -- well past the shared 72h window, proving that setting is not
    what is actually being applied here -- is still announced.
    """
    month_end = int(datetime(2026, 9, 1, 0, 0, tzinfo=TZ).timestamp())  # August ended here
    _freeze(conn, "mc", "2026-08", closed_at=month_end)

    now_5d = month_end + 5 * 86400
    items = announce.month_provider(conn, now_5d)
    assert len(items) == 1
    assert items[0]["key"] == "2026-08:mc"


def test_month_provider_age_cutoff_still_applies_past_its_own_longer_window(conn):
    """The month's own longer window is still a real cutoff, not
    unlimited: 8 days after the month ended is past its own 168h/7-day
    window, so it is skipped exactly like a too-old day or week is."""
    month_end = int(datetime(2026, 9, 1, 0, 0, tzinfo=TZ).timestamp())
    _freeze(conn, "mc", "2026-08", closed_at=month_end)

    now_8d = month_end + 8 * 86400
    assert announce.month_provider(conn, now_8d) == []


def test_fresh_database_with_old_frozen_months_produces_zero_announcements(conn):
    """The exact scenario this feature must never repeat: a fresh
    deployment (or one resuming after a long outage) against a database
    whose only history is old, never-announced frozen months. check_due()
    across ALL FOUR providers must store nothing at all."""
    now = int(time.time())
    for i, month in enumerate(["2020-01", "2020-02", "2020-03"]):
        _freeze(conn, "mc", month, closed_at=1577836800 + i * 86400)
        _freeze(conn, "mt", month, closed_at=1577836800 + i * 86400)

    stored = announce.check_due(conn, now)
    assert stored == 0
    assert _announcement_count(conn) == 0


# ---- net_wrapup_provider --------------------------------------------------


def test_net_wrapup_provider_produces_content_when_due(conn):
    _net(conn, 1, protocol="mc", weekday=2, start_hour=18, end_hour=20, timezone="America/Boise")
    season_id = _season(conn, "mc")
    _player(conn, 1, "Alice", "RED")
    thu_8am = int(datetime(2026, 9, 10, 8, 0, tzinfo=ZoneInfo("America/Boise")).timestamp())
    _checkin_award(conn, season_id, 1, "2026-09-09", net_id=1)

    items = announce.net_wrapup_provider(conn, thu_8am)
    assert len(items) == 1
    assert items[0]["kind"] == "net_wrapup"
    assert items[0]["key"] == "1:2026-09-09"


def test_net_wrapup_provider_zero_checkins_yields_nothing(conn):
    _net(conn, 1, protocol="mc")
    thu_8am = int(datetime(2026, 9, 10, 8, 0, tzinfo=ZoneInfo("America/Boise")).timestamp())
    assert announce.net_wrapup_provider(conn, thu_8am) == []


def test_net_wrapup_provider_not_due_once_a_row_for_that_key_exists(conn):
    _net(conn, 1, protocol="mc", weekday=2, start_hour=18, end_hour=20, timezone="America/Boise")
    season_id = _season(conn, "mc")
    _player(conn, 1, "Alice", "RED")
    thu_8am = int(datetime(2026, 9, 10, 8, 0, tzinfo=ZoneInfo("America/Boise")).timestamp())
    _checkin_award(conn, season_id, 1, "2026-09-09", net_id=1)

    first = announce.net_wrapup_provider(conn, thu_8am)
    assert len(first) == 1
    conn.execute(
        "INSERT INTO announcement(kind, key, board, net_id, content, created_at) "
        "VALUES ('net_wrapup', '1:2026-09-09', 'mc', 1, '{}', 0)"
    )
    assert announce.net_wrapup_provider(conn, thu_8am) == []


def test_net_wrapup_provider_age_cutoff_skips_a_stale_occurrence(conn):
    _net(conn, 1, protocol="mc", weekday=2, start_hour=18, end_hour=20, timezone="America/Boise")
    season_id = _season(conn, "mc")
    _player(conn, 1, "Alice", "RED")
    _checkin_award(conn, season_id, 1, "2026-09-09", net_id=1)
    tz = ZoneInfo("America/Boise")
    # Still the SAME occurrence (self-healing), but checked 5 days later --
    # well past the 72h default cutoff.
    much_later = int(datetime(2026, 9, 15, 8, 0, tzinfo=tz).timestamp())
    assert announce.net_wrapup_provider(conn, much_later) == []


# ---- check_due -------------------------------------------------------


def test_check_due_empty_registry_stores_nothing(conn, monkeypatch):
    monkeypatch.setattr(announce, "ANNOUNCEMENT_PROVIDERS", [])
    assert announce.check_due(conn, int(time.time())) == 0


def test_check_due_is_idempotent(conn):
    now = int(datetime(2026, 9, 2, 10, 0, tzinfo=TZ).timestamp())
    _freeze(conn, "mc", "2026-08", closed_at=now - 3600)

    first = announce.check_due(conn, now)
    assert first == 1
    assert _announcement_count(conn) == 1

    second = announce.check_due(conn, now)
    assert second == 0
    assert _announcement_count(conn) == 1


def test_check_due_one_provider_raising_does_not_stop_the_others(conn, monkeypatch, caplog):
    def broken_provider(conn, now):
        raise RuntimeError("boom")

    def working_provider(conn, now):
        return [{
            "kind": "daily_recap", "key": "2026-08-19:mc", "board": "mc", "net_id": None,
            "period_label": "19 Aug", "period_start_ts": 0, "period_end_ts": 1,
            "headline": "Test", "sections": [], "url": None, "created_at": 0,
        }]

    monkeypatch.setattr(announce, "ANNOUNCEMENT_PROVIDERS", [broken_provider, working_provider])
    with caplog.at_level("ERROR"):
        stored = announce.check_due(conn, int(time.time()))
    assert stored == 1
    assert _announcement_count(conn) == 1


def test_check_due_provider_returning_empty_list_is_noop(conn, monkeypatch):
    monkeypatch.setattr(announce, "ANNOUNCEMENT_PROVIDERS", [lambda conn, now: []])
    assert announce.check_due(conn, int(time.time())) == 0
    assert _announcement_count(conn) == 0


def test_check_due_quiet_period_builder_returns_none_stores_nothing(conn):
    """A provider whose own builder returned None for every candidate
    (nothing worth saying) never even reaches store_announcement()."""
    now = int(datetime(2026, 9, 20, 10, 0, tzinfo=TZ).timestamp())
    _season(conn, "mc")  # active season, but no movement at all
    assert announce.check_due(conn, now) == 0
    assert _announcement_count(conn) == 0


# ---- maybe_run ---------------------------------------------------------


def test_maybe_run_gate_blocks_before_interval_elapses(monkeypatch):
    """The gate itself runs on time.monotonic(), never on `now` (wall
    clock) -- see maybe_run()'s own docstring for why. Simulate "just
    checked" by setting the module gate to the current monotonic clock
    directly, the same thing a real prior call would have done.
    """
    calls = []
    monkeypatch.setattr(announce, "check_due", lambda conn, now: calls.append(now) or 5)
    monkeypatch.setattr(settings, "announcement_poll_interval_seconds", 60)
    monkeypatch.setattr(announce, "_last_announce_check_at", time.monotonic())

    result = _run(announce.maybe_run(1_000))  # `now` (wall clock) irrelevant to the gate

    assert result == 0
    assert calls == []


def test_maybe_run_gate_allows_and_calls_check_due(db_path, monkeypatch):
    calls = []
    monkeypatch.setattr(announce, "check_due", lambda conn, now: calls.append(now) or 3)
    monkeypatch.setattr(settings, "announcement_poll_interval_seconds", 60)
    # _reset_announce_gate (autouse) already zeroed the module gate, so
    # this first call is due regardless of how long the process has
    # been up (time.monotonic() only ever grows).

    result = _run(announce.maybe_run(10_000))

    assert result == 3
    assert calls == [10_000]
    assert announce._last_announce_check_at > 0.0


def test_maybe_run_second_call_within_interval_is_skipped(db_path, monkeypatch):
    calls = []
    monkeypatch.setattr(announce, "check_due", lambda conn, now: calls.append(now) or 1)
    monkeypatch.setattr(settings, "announcement_poll_interval_seconds", 60)

    first = _run(announce.maybe_run(10_000))
    # Real elapsed wall-clock time between these two calls is a tiny
    # fraction of a second either way -- `now` here is only ever passed
    # through to check_due(), never compared against the gate itself.
    second = _run(announce.maybe_run(10_010))

    assert first == 1
    assert second == 0
    assert calls == [10_000]
