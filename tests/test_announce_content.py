"""Tests for app/announce_content.py -- the four Content builders and
store_announcement(), the foundation of the public announcement feed
(app/db.py's `announcement` table). No API route, no background clock,
no text rendering exists yet -- these tests only cover the pure,
read-only "is there anything worth saying, and what is it" question
each builder answers.
"""
from __future__ import annotations

import json
import time

from app import announce_content as ac
from app import results
from app.grid import cell_id

NOW = int(time.time())
MONTH = results.month_key(NOW)
START, END = results.month_bounds(MONTH)

# A fixed local day/week window, well inside the frozen MONTH above so
# freeze-month tests and window tests never fight over the same
# timestamps. Deliberately not "today," so DST edges near the real
# current date can never make a test flaky.
DAY_START, DAY_END = START + 86400, START + 2 * 86400
WEEK_START, WEEK_END = START + 7 * 86400, START + 14 * 86400


def _player(conn, player_id, team, name=None):
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?,?,?,?)",
        (player_id, name or f"player-{player_id}", team, NOW),
    )


def _season(conn, protocol, started_at=0, ends_at=None):
    ends_at = ends_at if ends_at is not None else NOW + 10_000_000
    cur = conn.execute(
        "INSERT INTO mc_season(protocol, started_at, ends_at, status) VALUES (?,?,?,?)",
        (protocol, started_at, ends_at, "active"),
    )
    return cur.lastrowid


def _capture(conn, season_id, cell, ts, player_id, team, from_team=None, by_air=0):
    conn.execute(
        "INSERT INTO mc_tile_capture_log(season_id, cell_id, ts, by_player_id, by_team, "
        "from_team, by_air) VALUES (?,?,?,?,?,?,?)",
        (season_id, cell, ts, player_id, team, from_team, by_air),
    )


def _place(conn, place_id, ref_type, points=5):
    conn.execute(
        "INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (place_id, ref_type, f"ref-{place_id}", f"place-{place_id}", 43.0, -116.0,
         points, "TEST", NOW),
    )


def _place_activation(conn, place_id, player_id, points, awarded_at, week_start="2026-01-07",
                       protocol="mc"):
    conn.execute(
        "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at, "
        "protocol) VALUES (?,?,?,?,?,?)",
        (place_id, player_id, week_start, points, awarded_at, protocol),
    )


def _net(conn, net_id, protocol="mc", timezone="America/Boise", start_hour=18, end_hour=20,
         weekday=2, label="Boise Net"):
    conn.execute(
        "INSERT INTO checkin_net(id, label, protocol, kind, connector_url, weekday, "
        " start_hour, end_hour, timezone, enabled, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,1,?)",
        (net_id, label, protocol, "corescope", "http://x", weekday, start_hour, end_hour,
         timezone, NOW),
    )
    return conn.execute("SELECT * FROM checkin_net WHERE id = ?", (net_id,)).fetchone()


def _checkin_award(conn, season_id, player_id, net_date, net_id, points=25, streak=1,
                    protocol="mc"):
    conn.execute(
        "INSERT INTO mc_checkin_award(season_id, player_id, net_date, points, protocol, "
        "message_id, awarded_at, streak, net_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (season_id, player_id, net_date, points, protocol,
         f"msg-{player_id}-{net_date}", NOW, streak, net_id),
    )


_ASCII_MARKDOWN_CHARS = set("*_`~#[]")


def _assert_plain_ascii(text: str) -> None:
    assert text == "" or all(ord(c) < 128 for c in text), text
    assert not (_ASCII_MARKDOWN_CHARS & set(text)), text


def _assert_content_is_plain(content: dict) -> None:
    _assert_plain_ascii(content["headline"])
    for section in content["sections"]:
        for row in section["rows"]:
            _assert_plain_ascii(row["text"])


# ---- daily -----------------------------------------------------------


def test_daily_content_detects_rank_swap_and_biggest_gain(conn):
    _player(conn, 1, "RED")
    _player(conn, 2, "BLUE")
    season_id = _season(conn, "mc")

    # Before the day: RED holds 3 squares, BLUE holds 1 -- RED 1st, BLUE 2nd.
    _capture(conn, season_id, cell_id(43.0, -116.0), DAY_START - 300, 1, "RED")
    _capture(conn, season_id, cell_id(43.1, -116.0), DAY_START - 200, 1, "RED")
    _capture(conn, season_id, cell_id(43.2, -116.0), DAY_START - 100, 1, "RED")
    _capture(conn, season_id, cell_id(43.3, -116.0), DAY_START - 50, 2, "BLUE")

    # During the day: BLUE takes two RED squares and claims two more --
    # BLUE ends at 5, RED ends at 1. Ranks swap; BLUE has the day's
    # biggest gain (+4).
    _capture(conn, season_id, cell_id(43.0, -116.0), DAY_START + 10, 2, "BLUE", from_team="RED")
    _capture(conn, season_id, cell_id(43.1, -116.0), DAY_START + 20, 2, "BLUE", from_team="RED")
    _capture(conn, season_id, cell_id(43.4, -116.0), DAY_START + 30, 2, "BLUE")
    _capture(conn, season_id, cell_id(43.5, -116.0), DAY_START + 40, 2, "BLUE")

    content = ac.build_daily_content(conn, "mc", DAY_START, DAY_END, NOW)
    assert content is not None
    assert content["kind"] == "daily_recap"
    assert content["board"] == "mc"
    assert content["net_id"] is None
    assert content["key"].endswith(":mc")

    rows = content["sections"][0]["rows"]
    by_team = {r["team"]: r for r in rows if r["rank"] is not None}
    assert by_team["BLUE"]["rank"] == 1 and by_team["BLUE"]["rank_was"] == 2
    assert by_team["RED"]["rank"] == 2 and by_team["RED"]["rank_was"] == 1

    gain_rows = [r for r in rows if r["rank"] is None]
    assert len(gain_rows) == 1
    assert gain_rows[0]["team"] == "BLUE"
    assert gain_rows[0]["value"] == 4

    assert "BLUE" in content["headline"]
    _assert_content_is_plain(content)


def test_daily_headline_never_says_today(conn):
    """The daily recap is posted AFTER the day it describes has already
    closed (period_label already carries which day it was), so the
    fallback headline -- used when nothing's rank changed but a team
    still gained ground -- must never claim that gain happened "today."
    """
    _player(conn, 1, "RED")
    _player(conn, 2, "BLUE")
    season_id = _season(conn, "mc")

    # Before the day: RED holds 5, BLUE holds 1 -- RED 1st, BLUE 2nd.
    for i, off in enumerate((-300, -250, -200, -150, -100)):
        _capture(conn, season_id, cell_id(43.0 + i * 0.1, -116.0), DAY_START + off, 1, "RED")
    _capture(conn, season_id, cell_id(43.9, -116.0), DAY_START - 50, 2, "BLUE")

    # During the day: BLUE claims 2 NEW squares (not from RED) -- BLUE
    # ends at 3, RED stays at 5. Ranks do not swap (RED still 1st), so
    # the headline falls back to the biggest-gain row's own text.
    _capture(conn, season_id, cell_id(44.0, -116.0), DAY_START + 10, 2, "BLUE")
    _capture(conn, season_id, cell_id(44.1, -116.0), DAY_START + 20, 2, "BLUE")

    content = ac.build_daily_content(conn, "mc", DAY_START, DAY_END, NOW)
    assert content is not None
    assert "today" not in content["headline"].lower()
    assert content["headline"] == "BLUE gained 2 squares"


def test_daily_content_none_when_ranks_unchanged_and_no_gain(conn):
    _player(conn, 1, "RED")
    _player(conn, 2, "BLUE")
    season_id = _season(conn, "mc")

    # Ownership set once, before the window, and nothing happens during
    # the day at all -- ranks cannot have moved and nobody gained.
    _capture(conn, season_id, cell_id(43.0, -116.0), DAY_START - 300, 1, "RED")
    _capture(conn, season_id, cell_id(43.1, -116.0), DAY_START - 200, 2, "BLUE")

    content = ac.build_daily_content(conn, "mc", DAY_START, DAY_END, NOW)
    assert content is None


def test_daily_content_key_is_local_date_that_just_ended(conn):
    _player(conn, 1, "RED")
    _player(conn, 2, "BLUE")
    season_id = _season(conn, "mc")
    _capture(conn, season_id, cell_id(43.0, -116.0), DAY_START - 300, 1, "RED")
    _capture(conn, season_id, cell_id(43.1, -116.0), DAY_START - 200, 1, "RED")
    _capture(conn, season_id, cell_id(43.0, -116.0), DAY_START + 10, 2, "BLUE", from_team="RED")
    _capture(conn, season_id, cell_id(43.1, -116.0), DAY_START + 20, 2, "BLUE", from_team="RED")

    content = ac.build_daily_content(conn, "mc", DAY_START, DAY_END, NOW)
    assert content is not None
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.config import settings
    expected_date = datetime.fromtimestamp(DAY_START, tz=ZoneInfo(settings.checkin_net_timezone)).date()
    assert content["key"] == f"{expected_date.isoformat()}:mc"


# ---- weekly ------------------------------------------------------------


def test_weekly_content_placement_and_exploration(conn):
    _player(conn, 1, "RED")
    _player(conn, 2, "BLUE")
    season_id = _season(conn, "mc")

    # Before the week: RED holds 2 (1st), BLUE holds 1 (2nd).
    _capture(conn, season_id, cell_id(43.0, -116.0), WEEK_START - 300, 1, "RED")
    _capture(conn, season_id, cell_id(43.1, -116.0), WEEK_START - 200, 1, "RED")
    _capture(conn, season_id, cell_id(43.2, -116.0), WEEK_START - 100, 2, "BLUE")
    # During the week: BLUE takes one RED square and claims a new one --
    # BLUE ends at 3 (1st), RED ends at 1 (2nd). Ranks swap.
    _capture(conn, season_id, cell_id(43.0, -116.0), WEEK_START + 10, 2, "BLUE", from_team="RED")
    _capture(conn, season_id, cell_id(43.3, -116.0), WEEK_START + 20, 2, "BLUE")

    # Two activations of the SAME place inside the window must count as
    # one distinct place, not two.
    _place(conn, 1, "landmark")
    _place(conn, 2, "summit")
    _place_activation(conn, 1, player_id=1, points=5, awarded_at=WEEK_START + 100, protocol="mc")
    _place_activation(conn, 1, player_id=2, points=5, awarded_at=WEEK_START + 200, protocol="mc")
    _place_activation(conn, 2, player_id=1, points=5, awarded_at=WEEK_START + 300, protocol="mc")
    # Wrong board -- must not be counted.
    _place_activation(conn, 2, player_id=1, points=5, awarded_at=WEEK_START + 400,
                       week_start="2026-01-14", protocol="mt")
    # Outside the window -- must not be counted.
    _place_activation(conn, 1, player_id=1, points=5, awarded_at=WEEK_END + 1000,
                       week_start="2026-01-21", protocol="mc")

    content = ac.build_weekly_content(conn, "mc", WEEK_START, WEEK_END, NOW)
    assert content is not None
    assert content["kind"] == "weekly_recap"
    import datetime as _dt
    iso_year, iso_week, _ = _dt.date.fromtimestamp(WEEK_END - 1).isocalendar()
    assert content["key"] == f"{iso_year}-W{iso_week:02d}:mc"

    headings = [s["heading"] for s in content["sections"]]
    assert "Placement" in headings
    assert "Exploration" in headings
    explore = next(s for s in content["sections"] if s["heading"] == "Exploration")
    assert explore["rows"][0]["value"] == 2
    _assert_content_is_plain(content)


def test_weekly_content_none_on_quiet_week(conn):
    _player(conn, 1, "RED")
    _player(conn, 2, "BLUE")
    season_id = _season(conn, "mc")
    # Ownership stable across the whole window, no exploration at all.
    _capture(conn, season_id, cell_id(43.0, -116.0), WEEK_START - 300, 1, "RED")
    _capture(conn, season_id, cell_id(43.1, -116.0), WEEK_START - 200, 2, "BLUE")

    content = ac.build_weekly_content(conn, "mc", WEEK_START, WEEK_END, NOW)
    assert content is None


# ---- month -------------------------------------------------------------


def test_month_content_none_when_not_frozen(conn):
    _player(conn, 1, "RED")
    _season(conn, "mt")
    content = ac.build_month_content(conn, "mt", MONTH, NOW)
    assert content is None


def test_month_content_happy_path_after_freeze(conn, monkeypatch):
    monkeypatch.setattr(ac.settings, "oauth_public_base_url", "https://example.invalid")

    _player(conn, 1, "RED")
    _player(conn, 2, "BLUE")
    season_id = _season(conn, "mt")
    _capture(conn, season_id, cell_id(43.0, -116.0), START + 10, 1, "RED")
    _capture(conn, season_id, cell_id(43.1, -116.0), START + 20, 1, "RED")
    _capture(conn, season_id, cell_id(43.2, -116.0), START + 30, 1, "RED")
    _capture(conn, season_id, cell_id(43.3, -116.0), START + 40, 2, "BLUE")

    results.freeze_month(conn, "mt", MONTH, NOW)

    content = ac.build_month_content(conn, "mt", MONTH, NOW)
    assert content is not None
    assert content["kind"] == "month_honors"
    assert content["key"] == f"{MONTH}:mt"
    assert content["board"] == "mt"
    assert "RED" in content["headline"]
    assert "3" in content["headline"]
    assert content["url"] == "https://example.invalid/results"

    honors = content["sections"][0]["rows"]
    assert honors  # largest_territory at least
    assert any(r["team"] == "RED" for r in honors)
    _assert_content_is_plain(content)


def test_month_headline_does_not_repeat_month_name(conn, monkeypatch):
    """`period_label` already carries the month name (and the renderer
    puts it right alongside the headline in "{prefix} {period_label}:
    {headline}") -- the headline itself must not spend bytes restating
    it, while still reading as a complete sentence on its own.
    """
    monkeypatch.setattr(ac.settings, "oauth_public_base_url", "https://example.invalid")
    _player(conn, 1, "RED")
    season_id = _season(conn, "mt")
    _capture(conn, season_id, cell_id(43.0, -116.0), START + 10, 1, "RED")
    _capture(conn, season_id, cell_id(43.1, -116.0), START + 20, 1, "RED")
    _capture(conn, season_id, cell_id(43.2, -116.0), START + 30, 1, "RED")
    results.freeze_month(conn, "mt", MONTH, NOW)

    content = ac.build_month_content(conn, "mt", MONTH, NOW)
    assert content is not None
    month_name = content["period_label"]
    assert month_name.lower() not in content["headline"].lower()
    # Still a complete, self-contained sentence for a JSON consumer
    # reading `headline` alone, without `period_label`.
    assert content["headline"] == "RED wins with 3 squares"


def test_month_content_url_none_without_base_url(conn, monkeypatch):
    monkeypatch.setattr(ac.settings, "oauth_public_base_url", "")
    _player(conn, 1, "RED")
    season_id = _season(conn, "mt")
    _capture(conn, season_id, cell_id(43.0, -116.0), START + 10, 1, "RED")
    results.freeze_month(conn, "mt", MONTH, NOW)

    content = ac.build_month_content(conn, "mt", MONTH, NOW)
    assert content is not None
    assert content["url"] is None


def test_month_content_caps_awards_at_five(conn):
    """The Honors section never exceeds _MAX_SECTION_ROWS even for a
    month with many headline-scope awards."""
    _player(conn, 1, "RED")
    season_id = _season(conn, "mt")
    _capture(conn, season_id, cell_id(43.0, -116.0), START + 10, 1, "RED")
    results.freeze_month(conn, "mt", MONTH, NOW)

    n_headline = conn.execute(
        "SELECT COUNT(*) AS n FROM month_award WHERE month=? AND protocol=? AND scope=''",
        (MONTH, "mt"),
    ).fetchone()["n"]
    assert n_headline >= 1  # sanity: freeze actually wrote something

    content = ac.build_month_content(conn, "mt", MONTH, NOW)
    assert len(content["sections"][0]["rows"]) <= ac._MAX_SECTION_ROWS


# ---- net wrap-up ---------------------------------------------------------


def test_net_wrapup_happy_path(conn):
    _player(conn, 1, "RED", name="Alice")
    _player(conn, 2, "BLUE", name="Bob")
    season_id = _season(conn, "mc")
    net_row = _net(conn, 1, protocol="mc", timezone="America/Boise")
    net_date = "2026-08-19"
    _checkin_award(conn, season_id, 1, net_date, net_id=1, streak=4)
    _checkin_award(conn, season_id, 2, net_date, net_id=1, streak=1)

    content = ac.build_net_wrapup_content(conn, net_row, net_date, NOW)
    assert content is not None
    assert content["kind"] == "net_wrapup"
    assert content["key"] == f"1:{net_date}"
    assert content["board"] == "mc"
    assert content["net_id"] == 1
    assert "2" in content["headline"]

    rows = content["sections"][0]["rows"]
    assert rows[0]["player"] == "Alice"
    assert rows[0]["value"] == 4
    _assert_content_is_plain(content)


def test_net_wrapup_none_when_nobody_checked_in(conn):
    net_row = _net(conn, 1, protocol="mc")
    content = ac.build_net_wrapup_content(conn, net_row, "2026-08-19", NOW)
    assert content is None


def test_net_wrapup_uses_nets_own_timezone(conn):
    """Two nets on different timezones must never share a clock -- the
    period bounds for each are computed from THAT net's own timezone
    column, not a global one.
    """
    _player(conn, 1, "RED", name="Alice")
    season_id = _season(conn, "mc")
    net_row = _net(conn, 1, protocol="mc", timezone="America/New_York", start_hour=18, end_hour=20)
    net_date = "2026-08-19"
    _checkin_award(conn, season_id, 1, net_date, net_id=1, streak=1)

    content = ac.build_net_wrapup_content(conn, net_row, net_date, NOW)
    assert content is not None
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/New_York")
    expected_start = int(datetime(2026, 8, 19, 18, tzinfo=tz).timestamp())
    assert content["period_start_ts"] == expected_start


# ---- store_announcement --------------------------------------------------


def _fake_content(kind="daily_recap", key="2026-08-19:mc", board="mc"):
    return {
        "kind": kind, "key": key, "board": board, "net_id": None,
        "period_label": "19 Aug", "period_start_ts": 0, "period_end_ts": 1,
        "headline": "Test headline", "sections": [], "url": None,
        "created_at": NOW,
    }


def test_store_announcement_idempotent_on_kind_and_key(conn):
    content = _fake_content()
    first = ac.store_announcement(conn, content)
    assert first is not None

    second = ac.store_announcement(conn, content)
    assert second is None

    rows = conn.execute("SELECT * FROM announcement").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == first
    stored = json.loads(rows[0]["content"])
    assert stored == content


def test_store_announcement_different_key_is_a_new_row(conn):
    ac.store_announcement(conn, _fake_content(key="2026-08-19:mc"))
    second = ac.store_announcement(conn, _fake_content(key="2026-08-20:mc"))
    assert second is not None
    rows = conn.execute("SELECT * FROM announcement").fetchall()
    assert len(rows) == 2


def test_store_announcement_same_key_different_kind_is_a_new_row(conn):
    """(kind, key) together are the dedup key -- a daily recap and a
    weekly recap sharing the same literal key string must not collide.
    """
    ac.store_announcement(conn, _fake_content(kind="daily_recap", key="shared"))
    second = ac.store_announcement(conn, _fake_content(kind="weekly_recap", key="shared"))
    assert second is not None
    rows = conn.execute("SELECT * FROM announcement").fetchall()
    assert len(rows) == 2
