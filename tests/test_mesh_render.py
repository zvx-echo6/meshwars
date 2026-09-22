"""Tests for app/mesh_render.py -- rendering a Content dict (see
app/announce_content.py) into the one packet payload a single LoRa
packet can carry.

140 BYTES IS THE HARD MAX for every test in this file (per the
operator's own final spec) -- every render_mesh() call below passes
budget_bytes=140 explicitly, never the module's own MESHCORE_BUDGET_BYTES
(150) default and never MESHTASTIC_BUDGET_BYTES (237); this file does
not render, test, or report at those other budgets at all.

Covers: the newline-separated block format for all four Content kinds,
the exact target layout (including the weekly "<n> new places ·
<domain>" tail and daily/net's own lack of a url), the degradation
ladder in its exact documented order, the hard byte-budget guarantee,
never splitting a codepoint, never emitting a truncated url, and
determinism.
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

BUDGET = 140  # the only budget this file ever renders or reports at

_ASCII_MARKDOWN_CHARS = set("*_`~#[]")


def _assert_no_markdown_and_valid_utf8(text: str) -> None:
    """The block format intentionally carries non-ASCII glyphs (team
    emoji, arrows, the middle-dot separator) -- unlike the very first
    version of this renderer, plain ASCII is no longer the bar. What
    still must ALWAYS hold: no stray Markdown-special character, and
    the string round-trips through UTF-8 cleanly (a codepoint was never
    split mid-truncation).
    """
    assert not (_ASCII_MARKDOWN_CHARS & set(text)), text
    assert text.encode("utf-8").decode("utf-8") == text, text


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


# ---- synthetic Content builders -- for tests that need shapes the real
# builders would take many rows of DB setup to produce -------------------


def _standing(team, rank, rank_was=None):
    rank_was = rank if rank_was is None else rank_was
    text = (f"{team}: {rank}th (unchanged)" if rank_was == rank
            else f"{team}: {rank}th (was {rank_was}th)")
    return {
        "text": text, "team": team, "player": None, "value": None, "unit": None,
        "delta": rank_was - rank, "rank": rank, "rank_was": rank_was,
    }


def _weekly_content(standings, new_places=0, url=None, headline="Test headline",
                     period_label="8-14 Sep", board="mc"):
    return {
        "kind": "weekly_recap", "key": "k", "board": board, "net_id": None,
        "period_label": period_label, "period_start_ts": 0, "period_end_ts": 1,
        "headline": headline,
        "sections": [{"heading": "Standings", "rows": standings}],
        "new_places": new_places, "url": url, "created_at": 0,
    }


def _daily_content(standings, headline="Test headline", period_label="19 Aug", board="mc",
                    url=None):
    return {
        "kind": "daily_recap", "key": "k", "board": board, "net_id": None,
        "period_label": period_label, "period_start_ts": 0, "period_end_ts": 1,
        "headline": headline,
        "sections": [{"heading": "Placement", "rows": []},
                     {"heading": "Standings", "rows": standings}],
        # url is never set by build_daily_content() -- accepted here only
        # so a test can defensively prove render_mesh() ignores it anyway.
        "url": url, "created_at": 0,
    }


def _squares_row(team, squares, rank):
    return {
        "text": f"{team}: {squares} squares", "team": team, "player": None, "value": squares,
        "unit": "squares", "delta": None, "rank": rank, "rank_was": None,
    }


def _month_content(standings, headline="RED wins with 3 squares", url=None,
                    period_label="September", board="mt"):
    return {
        "kind": "month_honors", "key": "k", "board": board, "net_id": None,
        "period_label": period_label, "period_start_ts": 0, "period_end_ts": 1,
        "headline": headline, "sections": [{"heading": "Standings", "rows": standings}],
        "url": url, "created_at": 0,
    }


def _season_close_content(standings, winner="GREEN",
                           headline="GREEN wins the season with 6005 squares",
                           url=None, board="mc"):
    return {
        "kind": "season_close", "key": "5", "board": board, "net_id": None,
        "period_label": None, "period_start_ts": 0, "period_end_ts": 1,
        "headline": headline, "sections": [{"heading": "Standings", "rows": standings}],
        "winner": winner, "url": url, "created_at": 0,
    }


def _streak_row(player, team, streak):
    return {
        "text": f"{player}: streak {streak}", "team": team, "player": player, "value": streak,
        "unit": "streak", "delta": None, "rank": None, "rank_was": None,
    }


def _net_content(streak_rows, headline="2 checked in", net_name="Boise Net",
                  period_label="Tue 19 Aug", board="mc", url=None):
    return {
        "kind": "net_wrapup", "key": "k", "board": board, "net_id": 1,
        "period_label": period_label, "period_start_ts": 0, "period_end_ts": 1,
        "headline": headline, "sections": [{"heading": "Check-ins", "rows": streak_rows}],
        "net_name": net_name,
        # url is never set by build_net_wrapup_content() -- accepted here
        # only so a test can defensively prove render_mesh() ignores it.
        "url": url, "created_at": 0,
    }


def _content(headline="Test headline", rows=None, url=None, board="mc", period_label="19 Aug",
             kind="daily_recap"):
    """A Content of a `kind` render_mesh() has NO block builder for --
    always reaches _render_one_line() -- used by the one-line-fallback-
    specific tests below (the redundant-row skip, the pathological huge
    inputs, etc.), same as this file's very first version used for
    every test. Real `daily_recap`/`weekly_recap`/etc. content always
    goes through the block path first now -- see _daily_content() et al.
    above for those.
    """
    rows = rows or []
    sections = [{"heading": "Test", "rows": [
        {"text": r, "team": None, "player": None, "value": None, "unit": None,
         "delta": None, "rank": None, "rank_was": None} for r in rows
    ]}] if rows else []
    return {
        "kind": kind, "key": "k", "board": board, "net_id": None,
        "period_label": period_label, "period_start_ts": 0, "period_end_ts": 1,
        "headline": headline, "sections": sections, "url": url, "created_at": 0,
    }


# ---- the exact target format, byte-for-byte -------------------------------


def test_weekly_block_matches_exact_target_format():
    standings = [
        _standing("GREEN", 1, 1), _standing("ORANGE", 2, 3), _standing("YELLOW", 3, 3),
        _standing("RED", 4, 5), _standing("BLUE", 5, 2),
    ]
    content = _weekly_content(standings, new_places=444, url="https://meshwars.com")
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert line == (
        "MW Weekly Top 5\n"
        "🟢 GREEN = 1\n"
        "🟠 ORANGE ▲ 2\n"
        "🟡 YELLOW = 3\n"
        "🔴 RED ▲ 4\n"
        "🔵 BLUE ▼ 5\n"
        "444 new places · meshwars.com"
    )
    assert len(line.encode("utf-8")) <= BUDGET
    _assert_no_markdown_and_valid_utf8(line)


def test_daily_block_same_shape_no_tail_no_url():
    standings = [
        _standing("GREEN", 1, 1), _standing("ORANGE", 2, 3), _standing("YELLOW", 3, 3),
        _standing("RED", 4, 5), _standing("BLUE", 5, 2),
    ]
    content = _daily_content(standings)
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert line == (
        "MW Daily Top 5\n"
        "🟢 GREEN = 1\n"
        "🟠 ORANGE ▲ 2\n"
        "🟡 YELLOW = 3\n"
        "🔴 RED ▲ 4\n"
        "🔵 BLUE ▼ 5"
    )
    assert "http" not in line
    assert len(line.encode("utf-8")) <= BUDGET


def test_daily_block_ignores_a_url_even_if_content_somehow_carried_one():
    """build_daily_content() never sets `url`, but the renderer itself
    must never emit one for daily_recap regardless -- defensive proof,
    not just an absence-by-construction argument.
    """
    content = _daily_content([_standing("RED", 1, 1)], url="https://meshwars.com")
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert "meshwars.com" not in line
    assert "http" not in line


def test_month_block_matches_exact_target_format():
    standings = [
        _squares_row("GREEN", 6005, 1), _squares_row("RED", 2621, 2),
        _squares_row("YELLOW", 2262, 3), _squares_row("BLUE", 2164, 4),
        _squares_row("PURPLE", 1357, 5),
    ]
    content = _month_content(standings, period_label="August",
                              url="https://meshwars.com/results", board="mc")
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert line == (
        "MW August Top 5\n"
        "🟢 GREEN 6,005\n"
        "🔴 RED 2,621\n"
        "🟡 YELLOW 2,262\n"
        "🔵 BLUE 2,164\n"
        "🟣 PURPLE 1,357\n"
        "meshwars.com/results"
    )
    assert len(line.encode("utf-8")) <= BUDGET
    # No arrows on a monthly standings row -- unlike daily/weekly there
    # is no "before" snapshot to compare against.
    assert mr.ARROW_UP not in line and mr.ARROW_DOWN not in line
    _assert_no_markdown_and_valid_utf8(line)


def test_month_url_is_scheme_stripped_but_keeps_its_path():
    """Unlike weekly's bare-domain tail, month_honors keeps the /results
    path -- it links to a SPECIFIC page, not just the site -- but still
    drops the scheme (never shown in the compact block form)."""
    content = _month_content([_squares_row("RED", 42, 1)],
                              url="https://meshwars.com/results")
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert line.endswith("meshwars.com/results")
    assert "https://" not in line


def test_month_excludes_zero_square_teams():
    standings = [_squares_row("GREEN", 100, 1), _squares_row("RED", 50, 2)]
    content = _month_content(standings)
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert "BLUE" not in line
    assert line.count("\n") == 2  # title + exactly 2 rows, no url configured


def test_net_wrapup_block_matches_exact_target_format():
    """The operator's own final, real-production-derived example:
    Freq51 MT, 2026-09-16, 10 checked in."""
    rows = [
        _streak_row("huntchak", "PURPLE", 5),
        _streak_row("Littleaton", "GREEN", 4),
        _streak_row("dagronslayer", "GREEN", 4),
        _streak_row("Sidpatchy", "ORANGE", 4),
        _streak_row("Eastwood", "PURPLE", 3),
    ]
    content = _net_content(rows, headline="10 checked in", net_name="Freq51", board="mt")
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert line == (
        "MW Freq51 Net\n"
        "10 checked in\n"
        "Top streaks\n"
        "🟣 huntchak 5\n"
        "🟢 Littleaton 4\n"
        "🟢 dagronslayer 4\n"
        "🟠 Sidpatchy 4\n"
        "🟣 Eastwood 3"
    )
    assert len(line.encode("utf-8")) <= BUDGET
    assert "http" not in line
    _assert_no_markdown_and_valid_utf8(line)


def test_net_wrapup_ignores_a_url_even_if_content_somehow_carried_one():
    content = _net_content([_streak_row("Alice", "RED", 4)], url="https://meshwars.com")
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert "meshwars.com" not in line


def test_net_wrapup_non_ascii_display_name_counts_true_utf8_bytes():
    """`37Ω` (U+03A9 GREEK CAPITAL LETTER OMEGA, 2 UTF-8 bytes) is a
    real production display name -- must render correctly and its extra
    byte must actually be counted against the budget, not silently
    dropped or miscounted as 1 byte."""
    rows = [
        _streak_row("zevaryx", "PURPLE", 3),
        _streak_row("JT", "RED", 3),
        _streak_row("KF0KIT", "PINK", 2),
        _streak_row("37Ω", "GREEN", 2),
        _streak_row("schmoseque", "GREEN", 1),
    ]
    content = _net_content(rows, headline="6 checked in", net_name="Coloradomesh", board="mc")
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert "37Ω 2" in line
    assert len(line.encode("utf-8")) <= BUDGET
    _assert_no_markdown_and_valid_utf8(line)


# ---- net_wrapup's own degradation ladder -----------------------------------


def test_net_ladder_drops_streak_rows_before_anything_else():
    rows = [_streak_row(f"Player{i}", "RED", 5 - i) for i in range(5)]
    content = _net_content(rows, headline="5 checked in", net_name="Freq51")
    full = mr.render_mesh(content, budget_bytes=BUDGET)
    assert "Player4" in full

    budget = len(full.encode("utf-8")) - 1
    degraded = mr.render_mesh(content, budget_bytes=budget)
    assert len(degraded.encode("utf-8")) <= budget
    assert "Player4" not in degraded  # rank 5 dropped first
    assert degraded.startswith("MW Freq51 Net\n")  # title/heading untouched
    assert "Top streaks" in degraded


def test_net_ladder_overlong_display_name_forces_a_row_drop():
    long_name = "A" * 60
    rows = [
        _streak_row(long_name, "RED", 9),
        _streak_row("Bob", "BLUE", 5),
        _streak_row("Carl", "GREEN", 4),
        _streak_row("Dee", "ORANGE", 3),
        _streak_row("Eve", "PURPLE", 2),
    ]
    content = _net_content(rows, headline="5 checked in", net_name="Freq51")
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert len(line.encode("utf-8")) <= BUDGET
    # The overlong name alone pushes the full 5-row block over 140 bytes
    # -- the row-cap stage must drop the LAST row(s) (rank 5, then 4;
    # never below 3) rather than ever truncating the long name itself.
    assert long_name in line  # kept whole -- names are never truncated
    assert "Eve" not in line  # rank 5 dropped first to make room
    _assert_no_markdown_and_valid_utf8(line)


def test_net_ladder_drops_emoji_before_heading_and_title():
    rows = [_streak_row(t, t, i + 1) for i, t in enumerate(["GREEN", "ORANGE", "YELLOW"])]
    content = _net_content(rows, headline="3 checked in", net_name="Freq51")
    plain = "MW Freq51 Net\n3 checked in\nTop streaks\nGREEN 1\nORANGE 2\nYELLOW 3"
    budget = len(plain.encode("utf-8"))
    line = mr.render_mesh(content, budget_bytes=budget)
    assert len(line.encode("utf-8")) <= budget
    assert line == plain


def test_net_ladder_drops_heading_before_net_name():
    rows = [_streak_row(t, None, i + 1) for i, t in enumerate(["Ann", "Bo", "Cy"])]
    content = _net_content(rows, headline="3 checked in", net_name="Freq51")
    no_heading = "MW Freq51 Net\n3 checked in\nAnn 1\nBo 2\nCy 3"
    budget = len(no_heading.encode("utf-8"))
    line = mr.render_mesh(content, budget_bytes=budget)
    assert len(line.encode("utf-8")) <= budget
    assert line == no_heading


def test_net_ladder_drops_net_name_last_before_one_line_fallback():
    rows = [_streak_row(t, None, i + 1) for i, t in enumerate(["Ann", "Bo", "Cy"])]
    content = _net_content(rows, headline="3 checked in", net_name="Freq51")
    bare_title = "MW Net\n3 checked in\nAnn 1\nBo 2\nCy 3"
    budget = len(bare_title.encode("utf-8"))
    line = mr.render_mesh(content, budget_bytes=budget)
    assert len(line.encode("utf-8")) <= budget
    assert line == bare_title


# ---- season_close: a new Content kind, exercised only against mocked
# tallies -- no season has ever actually closed in production (see this
# task's own report) ---------------------------------------------------


def test_season_close_block_matches_exact_target_format():
    """The podium is ORDERED by the combined total (squares + check-in +
    place points), not the raw squares this row's `value` field still
    carries for JSON/API consumers -- so the default render shows no
    figure at all beside a podium team, only the medal, team emoji, and
    name. Showing squares next to a total-ordered podium could read as
    a contradiction to anyone on a radio; this is the operator's own
    exact target format."""
    standings = [
        _squares_row("GREEN", 6005, 1), _squares_row("RED", 2621, 2),
        _squares_row("YELLOW", 2262, 3),
    ]
    content = _season_close_content(standings, winner="GREEN",
                                     url="https://meshwars.com/results")
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert line == (
        "\U0001f3c6 MW SEASON OVER \U0001f3c6\n"
        "Congratulations GREEN!\n"
        "\U0001f947 \U0001f7e2 GREEN\n"
        "\U0001f948 \U0001f534 RED\n"
        "\U0001f949 \U0001f7e1 YELLOW\n"
        "meshwars.com/results"
    )
    assert len(line.encode("utf-8")) == 115  # well under the 140-byte target
    _assert_no_markdown_and_valid_utf8(line)


def test_season_close_never_shows_a_squares_figure():
    """No stage of the podium -- full render or any degraded stage --
    ever prints a tally figure, even though the underlying rows still
    carry big, easily-recognisable squares numbers. A stray digit here
    would be exactly the "looks like a bug" contradiction the operator
    flagged (order by total, display disagreeing raw squares)."""
    standings = [
        _squares_row("GREEN", 6005, 1), _squares_row("RED", 2621, 2),
        _squares_row("YELLOW", 2262, 3),
    ]
    content = _season_close_content(standings, winner="GREEN",
                                     url="https://meshwars.com/results")
    full = mr.render_mesh(content, budget_bytes=BUDGET)
    assert not any(ch.isdigit() for ch in full)
    for figure in ("6005", "6,005", "2621", "2,621", "2262", "2,262"):
        assert figure not in full

    # Same holds at every degraded block stage, down to 78 bytes -- the
    # smallest budget that still renders the 2-row-plus-url block form
    # (medals and trophy already dropped) rather than falling back to
    # the one-line sentence form (whose headline, a different code
    # path, legitimately does carry a number -- see the fallback test
    # below).
    for budget in (len(full.encode("utf-8")) - 1, 90, 78):
        degraded = mr.render_mesh(content, budget_bytes=budget)
        assert len(degraded.encode("utf-8")) <= budget
        assert not any(ch.isdigit() for ch in degraded)


def test_season_close_never_shows_more_than_first_second_third():
    """Even if a builder somehow handed it more than 3 standings rows,
    the renderer itself must never show a 4th."""
    standings = [_squares_row(t, 1000 - i, i + 1) for i, t in enumerate(
        ["GREEN", "RED", "YELLOW", "BLUE", "PURPLE"]
    )]
    content = _season_close_content(standings, winner="GREEN")
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert "BLUE" not in line
    assert "PURPLE" not in line


def test_season_close_winner_line_never_repeats_the_emoji():
    content = _season_close_content([_squares_row("GREEN", 6005, 1)], winner="GREEN")
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    congrats_line = line.split("\n")[1]
    assert congrats_line == "Congratulations GREEN!"
    assert mr.TEAM_EMOJI["GREEN"] not in congrats_line


def test_season_close_url_is_scheme_stripped_but_keeps_path():
    content = _season_close_content([_squares_row("GREEN", 6005, 1)], winner="GREEN",
                                     url="https://meshwars.com/results")
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert line.endswith("meshwars.com/results")
    assert "https://" not in line


# ---- season_close's own degradation ladder ---------------------------------


def test_season_close_ladder_drops_medals_first():
    """With no number ever on the row (see the DISPLAY tests above), the
    podium's own ladder has one fewer rung than it used to -- the medal
    emoji is now the FIRST thing dropped when a block does not fit."""
    standings = [
        _squares_row("GREEN", 6005, 1), _squares_row("RED", 2621, 2),
        _squares_row("YELLOW", 2262, 3),
    ]
    content = _season_close_content(standings, winner="GREEN",
                                     url="https://meshwars.com/results")
    full = mr.render_mesh(content, budget_bytes=BUDGET)
    assert mr.MEDALS[0] in full

    budget = len(full.encode("utf-8")) - 1
    degraded = mr.render_mesh(content, budget_bytes=budget)
    assert len(degraded.encode("utf-8")) <= budget
    assert mr.MEDALS[0] not in degraded and mr.MEDALS[1] not in degraded
    assert "GREEN" in degraded and "RED" in degraded and "YELLOW" in degraded
    assert mr.TROPHY in degraded  # trophy still present -- medals go first


def test_season_close_ladder_drops_trophy_before_third_place():
    standings = [_squares_row(t, 100 - i, i + 1) for i, t in enumerate(["GREEN", "RED", "YELLOW"])]
    content = _season_close_content(standings, winner="GREEN")
    no_trophy = (
        "MW SEASON OVER\n"
        "Congratulations GREEN!\n"
        "\U0001f7e2 GREEN\n"
        "\U0001f534 RED\n"
        "\U0001f7e1 YELLOW"
    )
    budget = len(no_trophy.encode("utf-8"))
    line = mr.render_mesh(content, budget_bytes=budget)
    assert len(line.encode("utf-8")) <= budget
    assert line == no_trophy


def test_season_close_ladder_drops_third_place_last_before_fallback():
    standings = [_squares_row(t, 100 - i, i + 1) for i, t in enumerate(["GREEN", "RED", "YELLOW"])]
    content = _season_close_content(standings, winner="GREEN")
    two_rows = "MW SEASON OVER\nCongratulations GREEN!\n\U0001f7e2 GREEN\n\U0001f534 RED"
    budget = len(two_rows.encode("utf-8"))
    line = mr.render_mesh(content, budget_bytes=budget)
    assert len(line.encode("utf-8")) <= budget
    assert line == two_rows
    assert "YELLOW" not in line


def test_season_close_winner_and_congrats_survive_to_one_line_fallback():
    """Per the operator's own spec: the winner, the congratulations
    fact, and the url are the LAST things to go -- proven here by a
    budget too small for even the 2-row block, which must fall back to
    the one-line sentence form and still name the winner."""
    standings = [_squares_row("GREEN", 6005, 1)]
    content = _season_close_content(
        standings, winner="GREEN",
        headline="GREEN wins the season with 6005 squares", board="mc",
    )
    line = mr.render_mesh(content, budget_bytes=45)
    assert len(line.encode("utf-8")) <= 45
    assert "\n" not in line
    assert "GREEN" in line


def test_render_real_season_close_content_within_140(conn, monkeypatch):
    """Exercised against MOCKED mc_season_team_tally rows -- no season
    has ever actually closed in production (see this task's own
    report); this is the one kind never verified against real data."""
    monkeypatch.setattr(ac.settings, "oauth_public_base_url", "https://meshwars.com")
    cur = conn.execute(
        "INSERT INTO mc_season(protocol, started_at, ends_at, status, winner) "
        "VALUES (?,?,?,?,?)",
        ("mc", 0, NOW, "closed", "GREEN"),
    )
    season_id = cur.lastrowid
    for team, tiles in (("GREEN", 6005), ("RED", 2621), ("YELLOW", 2262),
                         ("BLUE", 2164), ("PURPLE", 1357)):
        conn.execute(
            "INSERT INTO mc_season_team_tally(season_id, team, tiles) VALUES (?,?,?)",
            (season_id, team, tiles),
        )

    content = ac.build_season_close_content(conn, "mc", season_id, NOW)
    assert content is not None
    assert content["kind"] == "season_close"
    assert content["key"] == str(season_id)
    assert content["winner"] == "GREEN"

    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert len(line.encode("utf-8")) <= BUDGET
    assert "SEASON OVER" in line
    assert "Congratulations GREEN!" in line
    _assert_no_markdown_and_valid_utf8(line)


# ---- ranks 6/7 never rendered, regardless of budget ------------------------


def test_only_top_5_teams_ever_rendered_never_6_or_7():
    standings = [_standing(t, i + 1) for i, t in enumerate(
        ["GREEN", "ORANGE", "YELLOW", "RED", "BLUE", "PURPLE", "PINK"]
    )]
    content = _weekly_content(standings, new_places=1)
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert "PURPLE" not in line
    assert "PINK" not in line
    assert line.count("\n") == 6  # title + 5 team rows + tail, never a 6th team row


# ---- team emoji / arrow correctness ----------------------------------------


def test_team_missing_from_emoji_map_renders_name_alone():
    content = _weekly_content([_standing("TEAL", 1, 1)], new_places=0)
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert "TEAL = 1" in line
    # No stray emoji leaked in for an unmapped team, and no crash.
    for emoji in mr.TEAM_EMOJI.values():
        assert emoji not in line.split("\n")[1]


def test_arrow_up_for_improved_rank():
    content = _daily_content([_standing("RED", 1, rank_was=3)])
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert f"RED {mr.ARROW_UP} 1" in line


def test_arrow_down_for_dropped_rank():
    content = _daily_content([_standing("RED", 3, rank_was=1)])
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert f"RED {mr.ARROW_DOWN} 3" in line


def test_arrow_same_for_unchanged_rank():
    content = _daily_content([_standing("RED", 2, rank_was=2)])
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert f"RED {mr.ARROW_SAME} 2" in line


def test_arrow_same_when_no_prior_rank_at_all():
    """A team new to the board this window: app/announce_content.py's
    _standings_rows() already folds this into rank_was == rank, but this
    proves the renderer's own fallback (rank_was is None) agrees too."""
    row = _standing("RED", 1, rank_was=1)
    row["rank_was"] = None
    content = _daily_content([row])
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert f"RED {mr.ARROW_SAME} 1" in line


# ---- each real Content kind renders within budget at 140 -----------------


def test_render_real_daily_content_within_140(conn):
    _player(conn, 1, "RED")
    _player(conn, 2, "BLUE")
    season_id = _season(conn, "mc")
    _capture(conn, season_id, cell_id(43.0, -116.0), DAY_START - 300, 1, "RED")
    _capture(conn, season_id, cell_id(43.1, -116.0), DAY_START - 200, 1, "RED")
    _capture(conn, season_id, cell_id(43.0, -116.0), DAY_START + 10, 2, "BLUE", from_team="RED")
    _capture(conn, season_id, cell_id(43.1, -116.0), DAY_START + 20, 2, "BLUE", from_team="RED")

    content = ac.build_daily_content(conn, "mc", DAY_START, DAY_END, NOW)
    assert content is not None

    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert len(line.encode("utf-8")) <= BUDGET
    assert line.startswith("MW Daily Top 5")
    assert "http" not in line
    _assert_no_markdown_and_valid_utf8(line)


def test_render_real_weekly_content_within_140(conn, monkeypatch):
    monkeypatch.setattr(ac.settings, "oauth_public_base_url", "https://meshwars.com")
    _player(conn, 1, "RED")
    _player(conn, 2, "BLUE")
    season_id = _season(conn, "mc")
    _capture(conn, season_id, cell_id(43.0, -116.0), WEEK_START - 300, 1, "RED")
    _capture(conn, season_id, cell_id(43.1, -116.0), WEEK_START - 200, 1, "RED")
    _capture(conn, season_id, cell_id(43.0, -116.0), WEEK_START + 10, 2, "BLUE", from_team="RED")
    _capture(conn, season_id, cell_id(43.3, -116.0), WEEK_START + 20, 2, "BLUE")

    content = ac.build_weekly_content(conn, "mc", WEEK_START, WEEK_END, NOW)
    assert content is not None

    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert len(line.encode("utf-8")) <= BUDGET
    assert line.startswith("MW Weekly Top 5")
    _assert_no_markdown_and_valid_utf8(line)


def test_render_real_month_content_within_140(conn, monkeypatch):
    monkeypatch.setattr(ac.settings, "oauth_public_base_url", "https://meshwars.com")
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

    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert len(line.encode("utf-8")) <= BUDGET
    assert line.startswith(f"MW {content['period_label']} Top 5")
    assert "https://" not in line  # scheme stripped in the compact block form
    _assert_no_markdown_and_valid_utf8(line)


def test_render_real_net_wrapup_content_within_140(conn):
    _player(conn, 1, "RED", name="Alice")
    _player(conn, 2, "BLUE", name="Bob")
    season_id = _season(conn, "mc")
    net_row = _net(conn, 1, protocol="mc", timezone="America/Boise",
                    label="Weekly Net (Freq51 MC)")
    net_date = "2026-08-19"
    _checkin_award(conn, season_id, 1, net_date, net_id=1, streak=4)
    _checkin_award(conn, season_id, 2, net_date, net_id=1, streak=1)

    content = ac.build_net_wrapup_content(conn, net_row, net_date, NOW)
    assert content is not None
    assert content["net_name"] == "Freq51"

    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert len(line.encode("utf-8")) <= BUDGET
    assert line.startswith("MW Freq51 Net\n")
    assert "http" not in line
    _assert_no_markdown_and_valid_utf8(line)


# ---- the degradation ladder, in its exact documented order ----------------


def test_ladder_stage1_drops_count_keeps_url_first():
    standings = [_standing(t, i + 1, rank_was=i + 2) for i, t in enumerate(
        ["GREEN", "ORANGE", "YELLOW", "RED", "BLUE"]
    )]
    content = _weekly_content(standings, new_places=444, url="https://meshwars.com")
    full = mr.render_mesh(content, budget_bytes=BUDGET)
    assert "444 new places" in full

    # A budget too small for the full tail but big enough for all 5 rows
    # plus a bare domain -- must drop the count, NOT a row, first.
    budget = len(full.encode("utf-8")) - 5
    degraded = mr.render_mesh(content, budget_bytes=budget)
    assert len(degraded.encode("utf-8")) <= budget
    assert "new places" not in degraded
    assert degraded.endswith("meshwars.com")
    for team in ("GREEN", "ORANGE", "YELLOW", "RED", "BLUE"):
        assert team in degraded


def test_ladder_stage2_drops_rows_from_rank_5_upward():
    standings = [_standing(t, i + 1) for i, t in enumerate(
        ["GREEN", "ORANGE", "YELLOW", "RED", "BLUE"]
    )]
    content = _daily_content(standings)
    line = mr.render_mesh(content, budget_bytes=60)
    assert len(line.encode("utf-8")) <= 60
    assert "BLUE" not in line  # rank 5 dropped first
    assert "RED" not in line  # rank 4 dropped next -- down to the floor of 3
    assert "GREEN" in line and "ORANGE" in line and "YELLOW" in line


def test_ladder_never_drops_below_3_rows_within_block_form():
    standings = [_standing(t, i + 1) for i, t in enumerate(
        ["GREEN", "ORANGE", "YELLOW", "RED", "BLUE"]
    )]
    content = _daily_content(standings)
    # Sized to exactly fit the LAST block stage (row_cap=3, no emoji) --
    # every earlier stage (more rows, or the same 3 rows WITH emoji) is
    # strictly larger and cannot fit, so this must land exactly here,
    # never degrade past it to the one-line fallback, and never show a
    # 4th row.
    three_rows = "MW Daily Top 5\nGREEN = 1\nORANGE = 2\nYELLOW = 3"
    budget = len(three_rows.encode("utf-8"))
    line = mr.render_mesh(content, budget_bytes=budget)
    assert line == three_rows


def test_ladder_stage3_drops_emoji_keeping_names():
    standings = [_standing(t, i + 1) for i, t in enumerate(["GREEN", "ORANGE", "YELLOW"])]
    content = _daily_content(standings)
    with_emoji = mr.render_mesh(content, budget_bytes=BUDGET)
    assert mr.TEAM_EMOJI["GREEN"] in with_emoji

    # Budget that fits 3 plain-name rows but not 3 emoji rows.
    plain = "MW Daily Top 5\nGREEN = 1\nORANGE = 2\nYELLOW = 3"
    budget = len(plain.encode("utf-8"))
    degraded = mr.render_mesh(content, budget_bytes=budget)
    assert len(degraded.encode("utf-8")) <= budget
    assert degraded == plain


def test_ladder_stage4_falls_back_to_one_line_sentence():
    standings = [_standing(t, i + 1) for i, t in enumerate(["GREEN", "ORANGE", "YELLOW"])]
    content = _daily_content(standings, headline="GREEN leads", period_label="19 Aug")
    # Small enough that even 3 plain-name rows (the block floor) cannot
    # fit -- must fall back to the one-line sentence form ("MW <board>
    # <period>: <headline>"), never a partial/malformed block.
    line = mr.render_mesh(content, budget_bytes=25)
    assert len(line.encode("utf-8")) <= 25
    assert "\n" not in line
    assert line.startswith("MW MC")


def test_ladder_stage5_last_resort_truncates_on_codepoint_boundary():
    content = _daily_content([], headline="日本語" * 40)
    line = mr.render_mesh(content, budget_bytes=50)
    assert len(line.encode("utf-8")) <= 50
    assert "�" not in line
    assert line.encode("utf-8").decode("utf-8") == line


# ---- url guarantees ---------------------------------------------------------


def test_weekly_url_dropped_whole_never_truncated_when_it_cannot_fit_alone():
    standings = [_standing("RED", 1, 1)]
    content = _weekly_content(standings, new_places=5, url="https://meshwars.com")
    # Budget too small even for the bare domain alone (once title+row
    # are accounted for) -- must drop the url entirely rather than cut
    # it, at every stage including the one-line fallback.
    line = mr.render_mesh(content, budget_bytes=12)
    assert len(line.encode("utf-8")) <= 12
    assert "meshwars.com" not in line
    assert "http" not in line


def test_month_url_survives_when_rows_dropped_for_space():
    standings = [_squares_row(t, 1000 - i, i + 1) for i, t in enumerate(
        ["GREEN", "RED", "YELLOW", "BLUE", "PURPLE", "ORANGE"]
    )]
    content = _month_content(standings, url="https://meshwars.com/results")
    line = mr.render_mesh(content, budget_bytes=90)
    assert len(line.encode("utf-8")) <= 90
    assert line.endswith("meshwars.com/results")
    assert "PURPLE" not in line  # something had to give to keep the url


def test_month_url_that_fits_whole_is_never_dropped():
    content = _month_content([_squares_row("RED", 42, 1)], url="https://meshwars.com/results")
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert line.endswith("meshwars.com/results")


# ---- the hard byte guarantee, under pathological inputs (one-line path) --


def test_byte_guarantee_holds_with_huge_team_name():
    content = _content(headline="TEAM " + ("X" * 300) + " wins the month with a lot of squares",
                        kind="freeform_no_block_builder")
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert len(line.encode("utf-8")) <= BUDGET


def test_byte_guarantee_holds_with_many_rows():
    rows = [f"Row {i}: something happened here worth mentioning" for i in range(20)]
    content = _content(headline="Busy day", rows=rows, kind="freeform_no_block_builder")
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert len(line.encode("utf-8")) <= BUDGET
    assert not all(r in line for r in rows)


def test_tiny_budget_still_returns_valid_in_budget_string():
    content = _content(headline="A perfectly ordinary headline that is far too long",
                        kind="freeform_no_block_builder")
    line = mr.render_mesh(content, budget_bytes=40)
    assert isinstance(line, str)
    assert len(line.encode("utf-8")) <= 40


def test_determinism_same_content_same_budget_is_byte_identical():
    standings = [_standing(t, i + 1, rank_was=i + 2) for i, t in enumerate(
        ["GREEN", "ORANGE", "YELLOW", "RED", "BLUE"]
    )]
    content = _weekly_content(standings, new_places=12, url="https://meshwars.com")
    first = mr.render_mesh(content, budget_bytes=BUDGET)
    second = mr.render_mesh(content, budget_bytes=BUDGET)
    assert first == second


# ---- a Content `kind` with no block builder always uses the one-line form -


def test_unknown_kind_uses_one_line_form_directly():
    content = _content(headline="Some other kind of thing", kind="something_new")
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert "\n" not in line
    assert len(line.encode("utf-8")) <= BUDGET


# ---- redundant-row skipping survives in the one-line fallback path -------


def test_row_identical_to_headline_is_not_emitted_twice_in_fallback():
    content = _content(
        headline="BLUE gained 2 squares",
        rows=["BLUE gained 2 squares"],
        board="mc", period_label="2 Sep", kind="freeform_no_block_builder",
    )
    line = mr.render_mesh(content, budget_bytes=BUDGET)
    assert line == "MW MC 2 Sep: BLUE gained 2 squares"
    assert line.count("BLUE gained 2 squares") == 1
