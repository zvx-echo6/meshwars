"""Tests for app/mesh_render.py -- rendering a Content dict (see
app/announce_content.py) into the one plain-text line a single LoRa
packet can carry. Covers the hard byte-budget guarantee, the
degradation ladder (headline always present, url reserved up front,
rows dropped whole from the bottom), and determinism.
"""
from __future__ import annotations

import time

from app import announce_content as ac
from app import mesh_render as mr
from app import results
from app.grid import cell_id

NOW = int(time.time())
MONTH = results.month_key(NOW)
START, END = results.month_bounds(MONTH)
DAY_START, DAY_END = START + 86400, START + 2 * 86400
WEEK_START, WEEK_END = START + 7 * 86400, START + 14 * 86400

_ASCII_MARKDOWN_CHARS = set("*_`~#[]")


def _assert_plain_ascii(text: str) -> None:
    assert text == "" or all(ord(c) < 128 for c in text), text
    assert not (_ASCII_MARKDOWN_CHARS & set(text)), text


# ---- seeding helpers, mirroring tests/test_announce_content.py -----------


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


# ---- synthetic Content builder, for tests that need to push past what
# announce_content's own _clip() would ever actually produce -----------


def _content(headline="Test headline", rows=None, url=None, board="mc", period_label="19 Aug"):
    rows = rows or []
    sections = [{"heading": "Test", "rows": [
        {"text": r, "team": None, "player": None, "value": None, "unit": None,
         "delta": None, "rank": None, "rank_was": None} for r in rows
    ]}] if rows else []
    return {
        "kind": "daily_recap", "key": "k", "board": board, "net_id": None,
        "period_label": period_label, "period_start_ts": 0, "period_end_ts": 1,
        "headline": headline, "sections": sections, "url": url, "created_at": 0,
    }


# ---- each of the four Content kinds renders within the MeshCore budget ---


def test_render_daily_content_within_meshcore_budget(conn):
    _player(conn, 1, "RED")
    _player(conn, 2, "BLUE")
    season_id = _season(conn, "mc")
    _capture(conn, season_id, cell_id(43.0, -116.0), DAY_START - 300, 1, "RED")
    _capture(conn, season_id, cell_id(43.1, -116.0), DAY_START - 200, 1, "RED")
    _capture(conn, season_id, cell_id(43.0, -116.0), DAY_START + 10, 2, "BLUE", from_team="RED")
    _capture(conn, season_id, cell_id(43.1, -116.0), DAY_START + 20, 2, "BLUE", from_team="RED")

    content = ac.build_daily_content(conn, "mc", DAY_START, DAY_END, NOW)
    assert content is not None

    line = mr.render_mesh(content)
    assert len(line.encode("utf-8")) <= mr.MESHCORE_BUDGET_BYTES
    assert line.startswith("MW MC")
    _assert_plain_ascii(line)


def test_render_weekly_content_within_meshcore_budget(conn):
    _player(conn, 1, "RED")
    _player(conn, 2, "BLUE")
    season_id = _season(conn, "mc")
    _capture(conn, season_id, cell_id(43.0, -116.0), WEEK_START - 300, 1, "RED")
    _capture(conn, season_id, cell_id(43.1, -116.0), WEEK_START - 200, 1, "RED")
    _capture(conn, season_id, cell_id(43.0, -116.0), WEEK_START + 10, 2, "BLUE", from_team="RED")
    _capture(conn, season_id, cell_id(43.3, -116.0), WEEK_START + 20, 2, "BLUE")

    content = ac.build_weekly_content(conn, "mc", WEEK_START, WEEK_END, NOW)
    assert content is not None

    line = mr.render_mesh(content)
    assert len(line.encode("utf-8")) <= mr.MESHCORE_BUDGET_BYTES
    _assert_plain_ascii(line)


def test_render_month_content_within_meshcore_budget(conn, monkeypatch):
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
    assert content["url"]  # sanity: this fixture actually exercises the url path

    line = mr.render_mesh(content)
    assert len(line.encode("utf-8")) <= mr.MESHCORE_BUDGET_BYTES
    assert content["url"] in line
    assert line.startswith("MW MT")
    _assert_plain_ascii(line)


def test_render_net_wrapup_content_within_meshcore_budget(conn):
    _player(conn, 1, "RED", name="Alice")
    _player(conn, 2, "BLUE", name="Bob")
    season_id = _season(conn, "mc")
    net_row = _net(conn, 1, protocol="mc", timezone="America/Boise")
    net_date = "2026-08-19"
    _checkin_award(conn, season_id, 1, net_date, net_id=1, streak=4)
    _checkin_award(conn, season_id, 2, net_date, net_id=1, streak=1)

    content = ac.build_net_wrapup_content(conn, net_row, net_date, NOW)
    assert content is not None

    line = mr.render_mesh(content)
    assert len(line.encode("utf-8")) <= mr.MESHCORE_BUDGET_BYTES
    _assert_plain_ascii(line)


# ---- the hard byte guarantee, under pathological inputs -------------------


def test_byte_guarantee_holds_with_huge_team_name():
    content = _content(headline="TEAM " + ("X" * 300) + " wins the month with a lot of squares")
    line = mr.render_mesh(content)
    assert len(line.encode("utf-8")) <= mr.MESHCORE_BUDGET_BYTES


def test_byte_guarantee_holds_with_many_rows():
    rows = [f"Row {i}: something happened here worth mentioning" for i in range(20)]
    content = _content(headline="Busy day", rows=rows)
    line = mr.render_mesh(content)
    assert len(line.encode("utf-8")) <= mr.MESHCORE_BUDGET_BYTES
    # Sanity: this is actually exercising the drop -- not every row fits.
    assert not all(r in line for r in rows)


# ---- the degradation ladder ------------------------------------------------


def test_monthly_url_survives_when_rows_dropped_for_space():
    url = "https://example.invalid/results"
    rows = [f"Award number {i}: some team did something notable" for i in range(6)]
    content = _content(headline="March is over", url=url, rows=rows)

    line = mr.render_mesh(content, budget_bytes=90)
    assert len(line.encode("utf-8")) <= 90
    assert line.endswith(url)
    # Sanity: budget 90 cannot fit all six rows plus the url -- prove
    # rows actually got dropped, not that the test is vacuous.
    assert not all(r in line for r in rows)


def test_rows_dropped_whole_never_truncated_mid_row():
    row1 = "AA"
    row2 = "B" * 100  # far too long to fit alongside row1 in a small budget
    content = _content(headline="H", rows=[row1, row2])

    line = mr.render_mesh(content, budget_bytes=30)
    assert row1 in line
    assert row2 not in line
    # No partial prefix of row2 leaked into the output either.
    assert "B" not in line


def test_determinism_same_content_same_budget_is_byte_identical():
    content = _content(
        headline="Deterministic headline",
        rows=["Row one", "Row two", "Row three"],
        url="https://example.invalid/results",
    )
    first = mr.render_mesh(content, budget_bytes=100)
    second = mr.render_mesh(content, budget_bytes=100)
    assert first == second


def test_tiny_budget_still_returns_valid_in_budget_string():
    content = _content(headline="A perfectly ordinary headline that is far too long")
    line = mr.render_mesh(content, budget_bytes=40)
    assert isinstance(line, str)
    assert len(line.encode("utf-8")) <= 40


# ---- redundant-row skipping (Fix 3) ----------------------------------


def test_row_identical_to_headline_is_not_emitted_twice():
    """A row that restates the headline's own fact (e.g. the daily
    recap's fallback headline and its lone biggest-gain row, both built
    from the same text) must appear in the rendered line exactly once --
    but the underlying Content is untouched: `sections` still carries
    the row for JSON/API consumers.
    """
    content = _content(
        headline="BLUE gained 2 squares",
        rows=["BLUE gained 2 squares"],
        board="mc", period_label="2 Sep",
    )
    # The row is still in the Content's own sections.
    assert content["sections"][0]["rows"][0]["text"] == "BLUE gained 2 squares"

    line = mr.render_mesh(content)
    assert line == "MW MC 2 Sep: BLUE gained 2 squares"
    assert line.count("BLUE gained 2 squares") == 1


def test_row_overlapping_headline_with_new_info_is_still_emitted():
    """A row that PARTIALLY overlaps the headline but adds something the
    headline doesn't have (the weekly recap's "was 2nd" case) is not
    redundant -- it earns its bytes and must still be emitted.
    """
    content = _content(
        headline="BLUE climbed to 1st",
        rows=["BLUE: 1st (was 2nd)", "RED: 2nd (was 1st)"],
        board="mc", period_label="8-14 Sep",
    )
    line = mr.render_mesh(content)
    assert "BLUE: 1st (was 2nd)" in line
    assert "RED: 2nd (was 1st)" in line


def test_redundant_row_skip_does_not_stop_later_non_redundant_rows():
    """Skipping a redundant row must not consume a budget slot or break
    the loop -- a later, non-redundant row still gets a chance to fit.
    """
    content = _content(
        headline="BLUE gained 2 squares",
        rows=["BLUE gained 2 squares", "Fresh new place explored"],
        board="mc", period_label="2 Sep",
    )
    line = mr.render_mesh(content)
    assert line.count("BLUE gained 2 squares") == 1
    assert "Fresh new place explored" in line


# ---- url dropped whole rather than truncated (Fix 4) -----------------


def test_budget_smaller_than_url_drops_url_entirely():
    url = "https://example.invalid/results"
    content = _content(headline="Results", url=url, board="mt", period_label="September")

    # A budget smaller than the url itself -- the url must not appear in
    # any form, whole or fragment.
    line = mr.render_mesh(content, budget_bytes=20)
    assert len(line.encode("utf-8")) <= 20
    assert url not in line
    assert "http" not in line
    assert "example.invalid" not in line


def test_url_that_fits_whole_is_never_dropped():
    """Sanity companion to the drop test above -- a url that DOES fit
    whole within budget must still survive, same as before Fix 4.
    """
    url = "https://example.invalid/results"
    content = _content(headline="Results", url=url, board="mt", period_label="September")
    line = mr.render_mesh(content, budget_bytes=100)
    assert line.endswith(url)


def test_non_ascii_team_name_never_produces_invalid_utf8():
    content = _content(headline="日本語" * 40)  # far exceeds any budget in bytes
    line = mr.render_mesh(content, budget_bytes=50)
    assert len(line.encode("utf-8")) <= 50
    # errors="ignore" on the truncated byte slice never leaves a
    # replacement character behind -- a split codepoint is dropped
    # outright, not swapped for U+FFFD.
    assert "�" not in line
    # Round-trips cleanly -- proves no stray/partial byte sequence.
    assert line.encode("utf-8").decode("utf-8") == line
