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

Config (enabled, webhook_url, username, team_emoji,
announce_month_honors) now lives in the DB (app/db.py's discord_config
singleton, app/discord_notify.py's load_discord_config()), not
settings.py directly -- so tests below that used to monkeypatch
settings.discord_webhook_announcements/discord_webhook_username/
discord_team_emoji instead write straight to that row (_enable_discord()
below), the same way tests/test_paint_source_both.py's _set_paint_source()
writes to freqmapper_config rather than monkeypatching settings.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

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


_TEST_WEBHOOK = "https://discord.test/api/webhooks/1/abc"


def _enable_discord(conn, **overrides) -> None:
    """Flip discord_config to enabled with a webhook configured -- the
    precondition every enqueue()/build_month_honors_embed() test below
    needs. Config now lives in the DB (discord_config), not settings,
    so tests write directly to that singleton row rather than
    monkeypatching settings.discord_webhook_announcements/
    discord_webhook_username/discord_team_emoji. `conn` fixture rows
    already exist (app/db.py's MIGRATIONS seeds id=1 with bare column
    defaults), so this is always an UPDATE.
    """
    cfg = {
        "enabled": 1,
        "webhook_url": _TEST_WEBHOOK,
        "username": "",
        "team_emoji": "",
        "announce_month_honors": 1,
    }
    cfg.update(overrides)
    conn.execute(
        "UPDATE discord_config SET enabled = :enabled, webhook_url = :webhook_url, "
        " username = :username, team_emoji = :team_emoji, "
        " announce_month_honors = :announce_month_honors WHERE id = 1",
        cfg,
    )


# ---- announcements_enabled --------------------------------------------


def test_announcements_enabled_false_when_webhook_unset():
    assert discord_notify.announcements_enabled({"enabled": True, "webhook_url": ""}) is False


def test_announcements_enabled_true_when_webhook_set():
    assert discord_notify.announcements_enabled(
        {"enabled": True, "webhook_url": _TEST_WEBHOOK}
    ) is True


def test_announcements_enabled_false_when_disabled_even_with_webhook_set():
    """`enabled` and `webhook_url` are both required -- a stored webhook
    with the toggle off must never read as on."""
    assert discord_notify.announcements_enabled(
        {"enabled": False, "webhook_url": _TEST_WEBHOOK}
    ) is False


# ---- enqueue ------------------------------------------------------------


def test_enqueue_is_noop_when_disabled(conn):
    # discord_config's own column defaults (enabled=0, webhook_url='')
    # -- the `conn` fixture never seeds or enables it.
    discord_notify.enqueue(
        conn, kind="month_honors", key="2026-08:mc",
        payload={"embeds": []}, now=int(time.time()),
    )
    rows = conn.execute("SELECT * FROM discord_outbox").fetchall()
    assert rows == []


def test_enqueue_is_noop_when_announce_month_honors_off(conn):
    """announce_month_honors is a SEPARATE gate from `enabled`, checked
    only for kind="month_honors" -- an operator can leave the webhook
    enabled while turning off the automatic monthly post on its own."""
    _enable_discord(conn, announce_month_honors=0)
    discord_notify.enqueue(
        conn, kind="month_honors", key="2026-08:mc",
        payload={"embeds": []}, now=int(time.time()),
    )
    rows = conn.execute("SELECT * FROM discord_outbox").fetchall()
    assert rows == []


def test_enqueue_kind_test_not_gated_by_announce_month_honors(conn):
    """kind="test" (the admin panel's manual test button) must still go
    out even when announce_month_honors is off -- only the automatic
    month_honors kind is gated by it."""
    _enable_discord(conn, announce_month_honors=0)
    discord_notify.enqueue(
        conn, kind="test", key="1234567890",
        payload={"embeds": []}, now=int(time.time()),
    )
    rows = conn.execute("SELECT * FROM discord_outbox WHERE kind = 'test'").fetchall()
    assert len(rows) == 1


def test_enqueue_same_key_twice_leaves_exactly_one_row(conn):
    _enable_discord(conn)
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


def test_enqueue_different_keys_leave_separate_rows(conn):
    _enable_discord(conn)
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


def test_build_month_honors_embed_renders_labels_and_standings(conn):
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", _sample_result())
    text = json.dumps(embed)
    assert "MeshCore" in text
    # The embed title names the month in human form ("August 2026"),
    # not the raw "YYYY-MM" key -- see _month_title().
    assert "August 2026" in text
    assert "2026-08" not in text
    assert "RED **120**" in text
    assert "BLUE **80**" in text
    # The unit is stated once, in the standings embed's footer -- not
    # repeated on every standings line any more.
    assert embed["embeds"][0]["footer"]["text"] == discord_notify._STANDINGS_UNIT
    assert "Largest Territory" in text
    assert "Empire Builder" in text
    assert "zippy" in text
    # The unwon Longest Road placeholder (player_id and team both None,
    # see with_placeholders() in app/results.py) has nothing to
    # announce and must not appear anywhere in the embed.
    assert "Longest Road" not in text


def test_build_month_honors_embed_names_meshtastic_protocol(conn):
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mt", _sample_result())
    assert "Meshtastic" in json.dumps(embed)


def test_build_month_honors_embed_has_no_emoji(conn):
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", _sample_result())
    assert not _has_emoji(json.dumps(embed))


def test_build_month_honors_embed_sets_username(conn):
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", _sample_result())
    assert embed["username"] == "MeshWars"


def test_build_month_honors_embed_username_falls_back_when_config_blank(conn):
    conn.execute("UPDATE discord_config SET username = '' WHERE id = 1")
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", _sample_result())
    assert embed["username"] == "MeshWars"


def test_build_month_honors_embed_falls_back_to_award_labels(conn):
    """A row with no 'label' key still renders a real name, off
    results.AWARD_LABELS -- never the raw award key."""
    result = _sample_result()
    del result["awards"][1]["label"]
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", result)
    assert results.AWARD_LABELS["empire_builder"] in json.dumps(embed)


def _headline_award(i: int) -> dict:
    return {
        "award": f"headline_{i}", "label": f"Headline {i}", "scope": "",
        "player_id": i, "player": f"player{i}", "team": "RED",
        "value": float(i), "detail": "squares held",
    }


def _team_award(award_key: str, label: str, team: str, value: float) -> dict:
    return {
        "award": award_key, "label": label, "scope": team,
        "player_id": None, "player": None, "team": team,
        "value": value, "detail": "squares held",
    }


def _real_world_month_result():
    """Shaped like a real August: 10 headline awards + 20 per-team
    awards (2 per-team award keys x 10 teams) -- the exact 30-field
    shape that a live Discord webhook rejected with an HTTP 400 before
    this fix (Discord's own hard limit is 25 fields per embed)."""
    result = _sample_result()
    teams = [f"TEAM{n}" for n in range(10)]
    result["standings"] = [{"team": t, "squares": 100 - n} for n, t in enumerate(teams)]
    result["awards"] = [_headline_award(i) for i in range(10)]
    for t in teams:
        result["awards"].append(_team_award("team_attacker", "Top Attacker", t, 5.0))
        result["awards"].append(_team_award("team_defender", "Top Defender", t, 3.0))
    return result


def test_build_month_honors_embed_never_exceeds_discord_field_limit(conn):
    """Regression guard for the real 30-field/HTTP-400 incident: no
    matter how many headline + per-team awards a month has, no SINGLE
    embed may carry more than Discord's documented hard limit of 25
    fields (exceeding it fails the whole message, not just the extra
    fields) -- checked per embed, since Honors and By team are now two
    separate embeds rather than one combined `fields` list."""
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", _real_world_month_result())
    for e in embed["embeds"]:
        assert len(e.get("fields") or []) <= 25


def test_build_month_honors_embed_groups_per_team_awards_into_one_field_each(conn):
    """10 headline awards (one field each, in the Honors embed) + 2
    distinct per-team award keys (team_attacker, team_defender) across
    10 teams grouped into 2 fields (in the By team embed) -- never 10
    headline + 20 per-team fields: a per-team award is grouped by award
    key into one field listing every team, not one field per team
    (frontend/results.js's own split for this data)."""
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", _real_world_month_result())
    honors = next(e for e in embed["embeds"] if e["title"] == "Honors")
    by_team = next(e for e in embed["embeds"] if e["title"] == "By team")
    assert len(honors["fields"]) == 10
    assert len(by_team["fields"]) == 2
    attacker_field = next(f for f in by_team["fields"] if f["name"] == "Top Attacker")
    # All ten teams' lines live inside that ONE field's value.
    for n in range(10):
        assert f"TEAM{n}" in attacker_field["value"]


def test_build_month_honors_embed_renders_number_with_thousands_separator(conn):
    """The old code built `value = f"{who} -- {detail}"`, dropping
    a["value"] entirely -- a real announcement read 'GREEN -- squares
    held' with no figure at all. The number must render formatted like
    frontend/results.js's own num() (no trailing .0 on a whole number),
    plus a thousands separator: 6005.0 -> "6,005", never "6005.0"."""
    result = _sample_result()
    result["awards"] = [{
        "award": "largest_territory", "label": "Largest Territory", "scope": "",
        "player_id": None, "player": None, "team": "GREEN",
        "value": 6005.0, "detail": "squares held",
    }]
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", result)
    text = json.dumps(embed)
    assert "6,005" in text
    assert "6005.0" not in text


def test_build_month_honors_embed_deduplicates_number_already_in_detail(conn):
    """A real live announcement rendered 'Quick Fingers:  Littleaton --
    169 169 s after the net opened' -- frontend/results.js's own
    renderHonors() shows value and detail in two separate visual
    columns, so quick_fingers' hand-written detail already restating
    its own number (value=169.0, detail="169 s after the net opened")
    shows no visible duplication there. In the new two-line rendering
    the bold number must not appear a second time in front of a detail
    that already restates it -- the second line is just the italic
    detail, with no bold number and no empty "****"."""
    result = _sample_result()
    result["awards"] = [{
        "award": "quick_fingers", "label": "Quick Fingers", "scope": "",
        "player_id": 3, "player": "Littleaton", "team": "RED",
        "value": 169.0, "detail": "169 s after the net opened",
    }]
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", result)
    honors = next(e for e in embed["embeds"] if e["title"] == "Honors")
    value = honors["fields"][0]["value"]
    assert value == "Littleaton\n*169 s after the net opened*"
    assert "169 169" not in value
    assert "**" not in value
    assert "****" not in value
    assert "--" not in json.dumps(embed)


def test_build_month_honors_embed_number_detail_normal_path_unbroken(conn):
    """The common case -- a detail that does NOT restate the number --
    must still render both value and detail, unchanged by the
    de-duplication added for quick_fingers-shaped awards. Bold number,
    italic unit, on the second of two lines -- no "--" separator."""
    result = _sample_result()
    result["awards"] = [{
        "award": "largest_territory", "label": "Largest Territory", "scope": "",
        "player_id": None, "player": None, "team": "GREEN",
        "value": 6005.0, "detail": "squares held",
    }]
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", result)
    honors = next(e for e in embed["embeds"] if e["title"] == "Honors")
    value = honors["fields"][0]["value"]
    assert value == "GREEN\n**6,005** *squares held*"
    assert "--" not in json.dumps(embed)


def test_build_month_honors_embed_near_miss_number_not_falsely_deduplicated(conn):
    """A detail beginning with a LONGER number than value must not be
    mistaken for a duplicate: "169" is a string-prefix of "1690", but
    "169 " (number-then-space) is not, so both the bold value and the
    full italic detail must still render."""
    result = _sample_result()
    result["awards"] = [{
        "award": "some_award", "label": "Some Award", "scope": "",
        "player_id": None, "player": None, "team": "RED",
        "value": 169.0, "detail": "1690 squares past the towns",
    }]
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", result)
    honors = next(e for e in embed["embeds"] if e["title"] == "Honors")
    value = honors["fields"][0]["value"]
    assert value == "RED\n**169** *1690 squares past the towns*"


def test_build_month_honors_embed_team_award_line_also_deduplicates(conn):
    """A per-team (scoped) award line (_team_award_line()) never prints
    its detail at all any more -- just "TEAM <_SEP> **<number>**" -- so
    the old per-line 169/169 stutter this test used to guard against
    cannot recur structurally. What remains to guard: the award's
    `detail` still surfaces exactly once, as the field's own trailing
    italic unit line (_join_team_field()), and the per-team line itself
    stays just the bold number (behind the middle-dot separator) with
    no "--" and no restated detail text."""
    result = _sample_result()
    result["awards"] = [{
        "award": "quick_fingers", "label": "Quick Fingers", "scope": "TEAM0",
        "player_id": None, "player": None, "team": "TEAM0",
        "value": 169.0, "detail": "169 s after the net opened",
    }]
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", result)
    by_team = next(e for e in embed["embeds"] if e["title"] == "By team")
    value = by_team["fields"][0]["value"]
    assert value == "TEAM0 · **169**\n\n*169 s after the net opened*"
    assert "169 169" not in value
    assert "--" not in value


# ---- by-team middle-dot separator (_SEP) ----------------------------------


def test_team_award_line_distinct_player_gets_separators(conn):
    """A by-team award naming a player distinct from its team scope --
    a real screenshot had team, player, and number running together
    with only plain spaces, hard to read where a handle like "kraroed"
    started. _SEP puts a spaced middle dot between every token."""
    a = {
        "award": "team_top_scorer", "label": "Top Scorer", "scope": "GREEN",
        "player_id": 9, "player": "kraroed", "team": "GREEN",
        "value": 1783.0, "detail": "squares taken",
    }
    assert discord_notify._team_award_line(a, {}) == "GREEN · kraroed · **1,783**"


def test_team_award_line_team_only_names_team_once(conn):
    """A team-only award (who == scope, the existing de-duplication
    case) still gets one separator before the number, with the team
    named exactly once -- never "GREEN GREEN **1,783**"."""
    a = _team_award("team_attacker", "Top Attacker", "GREEN", 1783.0)
    line = discord_notify._team_award_line(a, {})
    assert line == "GREEN · **1,783**"
    assert line.count("GREEN") == 1


def test_team_award_line_no_value_has_no_dangling_separator(conn):
    """When `value` is None there is nothing to put after the trailing
    separator, so none is emitted -- never a dangling "GREEN · "
    with nothing after it."""
    a = {
        "award": "some_award", "label": "Some Award", "scope": "GREEN",
        "player_id": None, "player": None, "team": "GREEN",
        "value": None, "detail": None,
    }
    line = discord_notify._team_award_line(a, {})
    assert not line.rstrip().endswith(discord_notify._SEP.strip())


def test_standings_line_has_no_middle_dot_separator(conn):
    """_SEP is used ONLY by _team_award_line() -- standings lines stay
    "<dot>TEAM **<number>**", already unambiguous with just two tokens,
    and must never grow the by-team block's separator."""
    result = _sample_result()
    result["standings"] = [{"team": "GREEN", "squares": 200}]
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", result)
    description = embed["embeds"][0]["description"]
    assert "·" not in description


def test_build_month_honors_embed_url_absolute_or_omitted(conn, monkeypatch):
    """A Discord embed's "url" must be an ABSOLUTE url -- a relative one
    (the old fallback, "/results") makes Discord reject the WHOLE
    message with an HTTP 400. When OAUTH_PUBLIC_BASE_URL isn't
    configured there is no absolute url to give, so the "url" key must
    be omitted entirely rather than filled with a relative path; when it
    is configured, the url must be absolute."""
    monkeypatch.setattr(settings, "oauth_public_base_url", "")
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", _sample_result())
    assert "url" not in embed["embeds"][0]

    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", _sample_result())
    assert embed["embeds"][0]["url"].startswith("https://")


def test_build_month_honors_embed_standings_use_thousands_separator(conn):
    """The standings line built its number with an f-string directly
    (f"{squares} squares held"), never routing it through _fmt_number()
    the way the award fields do -- so one real message printed "6005"
    in the description right above "6,005" in a field, disagreeing with
    itself. Standings must use the same formatter, bolded, with the
    unit stated once in the embed's footer rather than on the line."""
    result = _sample_result()
    result["standings"] = [{"team": "GREEN", "squares": 6005}]
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", result)
    standings_embed = embed["embeds"][0]
    description = standings_embed["description"]
    assert description == "GREEN **6,005**"
    assert "6005" not in description
    assert "squares held" not in description
    assert standings_embed["footer"]["text"] == "squares held"


# ---- team emoji dots ---------------------------------------------------


def _emoji_setting() -> str:
    """A well-formed 7-entry DISCORD_TEAM_EMOJI covering every team
    _TEAM_COLORS knows, in the exact TEAM=token comma-separated shape
    an operator would paste in from .env.example."""
    return (
        "RED=<:mw_red:111>,GREEN=<:mw_green:222>,BLUE=<:mw_blue:333>,"
        "PURPLE=<:mw_purple:444>,YELLOW=<:mw_yellow:555>,"
        "ORANGE=<:mw_orange:666>,PINK=<:mw_pink:777>"
    )


def test_parse_team_emoji_well_formed_seven_entries():
    parsed = discord_notify._parse_team_emoji(_emoji_setting())
    assert len(parsed) == 7
    assert parsed["GREEN"] == "<:mw_green:222>"
    assert parsed["RED"] == "<:mw_red:111>"


def test_parse_team_emoji_skips_malformed_entry_but_keeps_good_ones(caplog):
    raw = "RED=<:mw_red:111>,GARBAGE_NO_EQUALS,GREEN=<:mw_green:222>"
    with caplog.at_level("WARNING"):
        parsed = discord_notify._parse_team_emoji(raw)
    assert parsed == {"RED": "<:mw_red:111>", "GREEN": "<:mw_green:222>"}
    # Logged once at WARNING naming the count -- never the raw entry text.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "1" in warnings[0].message
    assert "GARBAGE_NO_EQUALS" not in warnings[0].message


def test_parse_team_emoji_empty_string_yields_empty_dict():
    assert discord_notify._parse_team_emoji("") == {}


def test_standings_line_starts_with_team_emoji_dot(conn):
    conn.execute("UPDATE discord_config SET team_emoji = ? WHERE id = 1", (_emoji_setting(),))
    result = _sample_result()
    result["standings"] = [{"team": "GREEN", "squares": 200}]
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", result)
    description = embed["embeds"][0]["description"]
    assert description == "<:mw_green:222> GREEN **200**"


def test_headline_player_award_value_carries_teams_dot(conn):
    """The headline award's own `team` field (not its scope, which is
    empty for a headline award) says which team the winner belongs to
    -- the value is prefixed with THAT team's dot."""
    conn.execute("UPDATE discord_config SET team_emoji = ? WHERE id = 1", (_emoji_setting(),))
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", _sample_result())
    honors = next(e for e in embed["embeds"] if e["title"] == "Honors")
    empire = next(f for f in honors["fields"] if f["name"] == "Empire Builder")
    # _sample_result()'s empire_builder award: player "zippy", team "RED".
    assert empire["value"].startswith("<:mw_red:111> zippy")


def test_per_team_grouped_lines_each_carry_their_own_dot(conn):
    conn.execute("UPDATE discord_config SET team_emoji = ? WHERE id = 1", (_emoji_setting(),))
    result = _sample_result()
    result["standings"] = [{"team": "RED", "squares": 120}, {"team": "GREEN", "squares": 80}]
    result["awards"].append(_team_award("team_attacker", "Top Attacker", "RED", 5.0))
    result["awards"].append(_team_award("team_attacker", "Top Attacker", "GREEN", 3.0))
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", result)
    by_team = next(e for e in embed["embeds"] if e["title"] == "By team")
    attacker = next(f for f in by_team["fields"] if f["name"] == "Top Attacker")
    lines = attacker["value"].split("\n")
    red_line = next(l for l in lines if "RED" in l)
    green_line = next(l for l in lines if "GREEN" in l)
    assert red_line.startswith("<:mw_red:111> RED")
    assert green_line.startswith("<:mw_green:222> GREEN")


def test_no_emoji_configured_renders_identical_to_before(conn):
    """The mandatory fallback: with team_emoji unset (discord_config's
    own default), output must be byte-identical to the no-emoji
    rendering -- no leading space and no "<:" custom-emoji syntax
    anywhere, on a realistic shape with standings, headline awards, and
    per-team awards all present at once."""
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", _real_world_month_result())
    text = json.dumps(embed)
    assert "<:" not in text
    description = embed["embeds"][0]["description"]
    for line in description.split("\n"):
        assert not line.startswith(" ")
    honors = next(e for e in embed["embeds"] if e["title"] == "Honors")
    for f in honors["fields"]:
        assert not f["value"].startswith(" ")
    by_team = next(e for e in embed["embeds"] if e["title"] == "By team")
    for f in by_team["fields"]:
        for line in f["value"].split("\n"):
            assert not line.startswith(" ")


def test_partial_emoji_config_missing_team_renders_plainly(conn):
    """A team absent from a partially-configured emoji map must render
    exactly as if no emoji were configured at all for that team, while
    a team that IS in the map still gets its dot -- a gap in the config
    must not break the teams that ARE configured."""
    conn.execute("UPDATE discord_config SET team_emoji = 'GREEN=<:mw_green:222>' WHERE id = 1")
    result = _sample_result()
    result["standings"] = [{"team": "GREEN", "squares": 200}, {"team": "RED", "squares": 100}]
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", result)
    lines = embed["embeds"][0]["description"].split("\n")
    green_line = next(l for l in lines if "GREEN" in l)
    red_line = next(l for l in lines if "RED" in l)
    assert green_line == "<:mw_green:222> GREEN **200**"
    assert red_line == "RED **100**"


def test_standings_description_no_longer_has_standings_prefix(conn):
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", _sample_result())
    assert "Standings:" not in embed["embeds"][0]["description"]


# ---- three embeds, team colour, density -----------------------------------


def test_build_month_honors_embed_produces_three_embeds_in_order(conn):
    """A normal month with both headline AND per-team awards must
    produce exactly three embeds -- Standings, Honors, By team -- in
    that order, replacing the old single dense embed."""
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", _real_world_month_result())
    titles = [e["title"] for e in embed["embeds"]]
    assert titles == ["MeshCore — August 2026", "Honors", "By team"]


def test_build_month_honors_embed_omits_empty_by_team_embed(conn):
    """A month with no per-team (scoped) awards must produce exactly 2
    embeds -- Standings, Honors -- never a third, empty 'By team'
    embed with no fields in it."""
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", _sample_result())
    embeds = embed["embeds"]
    assert len(embeds) == 2
    assert [e["title"] for e in embeds] == ["MeshCore — August 2026", "Honors"]


def test_build_month_honors_embed_color_matches_leading_team(conn):
    """All three embeds carry the same `color`, the integer value of
    the LEADING team (the first, highest-squares entry of `standings`)
    -- GREEN leading must colour every embed 0x2ecc40, never a mix or a
    guessed default."""
    result = _sample_result()
    result["standings"] = [
        {"team": "GREEN", "squares": 200},
        {"team": "RED", "squares": 100},
    ]
    result["awards"].append(_team_award("team_attacker", "Top Attacker", "GREEN", 5.0))
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", result)
    embeds = embed["embeds"]
    assert len(embeds) == 3
    for e in embeds:
        assert e["color"] == 0x2ecc40


def test_build_month_honors_embed_no_color_when_standings_empty(conn):
    """Empty standings means no leading team to colour by -- `color`
    must be omitted from every embed entirely, never defaulted."""
    result = _sample_result()
    result["standings"] = []
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", result)
    for e in embed["embeds"]:
        assert "color" not in e


def test_build_month_honors_embed_honors_fields_inline_by_team_fields_not(conn):
    """Honors fields are laid out three-across (inline=True) to fix the
    owner's "DENSE" complaint about the old stacked shape; By team
    fields stay full-width (inline=False) because each value is a
    multi-line per-team list that would be unreadable squeezed a third
    as wide."""
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", _real_world_month_result())
    honors = next(e for e in embed["embeds"] if e["title"] == "Honors")
    by_team = next(e for e in embed["embeds"] if e["title"] == "By team")
    assert honors["fields"] and all(f["inline"] is True for f in honors["fields"])
    assert by_team["fields"] and all(f["inline"] is False for f in by_team["fields"])


def test_build_month_honors_embed_drops_by_team_over_char_budget(conn, monkeypatch):
    """When the assembled payload would exceed Discord's 6000-character
    total-embed budget, the 'By team' embed is dropped first and
    entirely -- Standings and Honors must still render in full, never
    truncated, to make the drop reproducible without needing thousands
    of characters of fixture text, the budget itself is monkeypatched
    down for this test."""
    monkeypatch.setattr(discord_notify, "_MAX_TOTAL_EMBED_CHARS", 50)
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", _real_world_month_result())
    titles = [e["title"] for e in embed["embeds"]]
    assert "By team" not in titles
    assert titles == ["MeshCore — August 2026", "Honors"]
    honors = next(e for e in embed["embeds"] if e["title"] == "Honors")
    assert len(honors["fields"]) == 10


# ---- markdown spacing/emphasis (owner: "needs spacing and bold and -----
# ---- italics of some kind to really drive it") -------------------------


def test_build_month_honors_embed_never_contains_double_dash_separator(conn):
    """The old '--' separator is gone everywhere -- standings,
    headline honors, and by-team lines alike -- across a realistic
    payload carrying all three embeds at once."""
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", _real_world_month_result())
    assert "--" not in json.dumps(embed)


def test_build_month_honors_embed_standings_line_bold_number_unit_in_footer(conn):
    """A standings line is exactly '<dot> TEAM **<number>**', and the
    unit ("squares held") appears in the embed's footer, never on the
    line itself."""
    conn.execute("UPDATE discord_config SET team_emoji = ? WHERE id = 1", (_emoji_setting(),))
    result = _sample_result()
    result["standings"] = [{"team": "GREEN", "squares": 6005}]
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", result)
    standings_embed = embed["embeds"][0]
    assert standings_embed["description"] == "<:mw_green:222> GREEN **6,005**"
    assert standings_embed["footer"] == {"text": "squares held"}
    assert "squares held" not in standings_embed["description"]


def test_build_month_honors_embed_headline_value_bold_number_italic_unit(conn):
    """A headline award's field value is two lines: the winner, then a
    bold number and an italic unit."""
    result = _sample_result()
    result["awards"] = [{
        "award": "largest_territory", "label": "Largest Territory", "scope": "",
        "player_id": None, "player": None, "team": "GREEN",
        "value": 6005.0, "detail": "squares held",
    }]
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", result)
    honors = next(e for e in embed["embeds"] if e["title"] == "Honors")
    value = honors["fields"][0]["value"]
    lines = value.split("\n")
    assert lines[0] == "GREEN"
    assert lines[1] == "**6,005** *squares held*"


def test_build_month_honors_embed_quick_fingers_dedup_no_empty_bold(conn):
    """quick_fingers-shaped de-duplication (detail already restates the
    number) still holds under the new two-line rendering, and never
    emits an empty '****' where the bold number would have gone."""
    result = _sample_result()
    result["awards"] = [{
        "award": "quick_fingers", "label": "Quick Fingers", "scope": "",
        "player_id": 3, "player": "Littleaton", "team": "RED",
        "value": 169.0, "detail": "169 s after the net opened",
    }]
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", result)
    honors = next(e for e in embed["embeds"] if e["title"] == "Honors")
    value = honors["fields"][0]["value"]
    assert value == "Littleaton\n*169 s after the net opened*"
    assert "****" not in value
    assert "**" not in value


def test_build_month_honors_embed_by_team_field_ends_with_single_unit_line(conn):
    """A by-team field's value ends with exactly one italic unit line,
    and that unit text does not appear on any of the individual team
    lines above it."""
    result = _sample_result()
    result["standings"] = [{"team": "RED", "squares": 120}, {"team": "GREEN", "squares": 80}]
    result["awards"] = [
        _team_award("team_attacker", "Top Attacker", "RED", 207.0),
        _team_award("team_attacker", "Top Attacker", "GREEN", 40.0),
    ]
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", result)
    by_team = next(e for e in embed["embeds"] if e["title"] == "By team")
    value = by_team["fields"][0]["value"]
    lines = value.split("\n")
    assert lines[-1] == "*squares held*"
    assert lines[-2] == ""
    team_lines = lines[:-2]
    assert len(team_lines) == 2
    for line in team_lines:
        assert "squares held" not in line
    assert value.count("squares held") == 1


def test_build_month_honors_embed_by_team_field_never_exceeds_1024_chars(conn):
    """A by-team field with many long team/player names must never be
    handed to Discord over its hard 1024-character field-value limit --
    trailing lines are dropped and replaced with a plain truncation
    marker instead."""
    result = _sample_result()
    long_teams = [f"TEAM-{n}-{'X' * 40}" for n in range(60)]
    result["standings"] = [{"team": t, "squares": 100 - n} for n, t in enumerate(long_teams)]
    result["awards"] = [
        {
            "award": "team_attacker", "label": "Top Attacker", "scope": t,
            "player_id": None, "player": f"Player-{'Y' * 40}-{n}", "team": t,
            "value": float(1000 + n), "detail": "squares taken from other teams",
        }
        for n, t in enumerate(long_teams)
    ]
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", result)
    by_team = next(e for e in embed["embeds"] if e["title"] == "By team")
    value = by_team["fields"][0]["value"]
    assert len(value) <= 1024
    assert value.endswith(discord_notify._TRUNCATION_MARKER)


# ---- _month_title -----------------------------------------------------


def test_month_title_normal():
    assert discord_notify._month_title("2026-08") == "August 2026"


def test_month_title_invalid_month_returns_unchanged():
    """A month that doesn't parse to 1-12 (out of range, or not
    "YYYY-MM" shaped at all) returns the raw input unchanged -- never
    raises, never produces something like "None 2026"."""
    assert discord_notify._month_title("2026-13") == "2026-13"
    assert discord_notify._month_title("garbage") == "garbage"


def test_build_month_honors_embed_title_uses_month_name_not_key(conn):
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", _sample_result())
    title = embed["embeds"][0]["title"]
    assert title == "MeshCore — August 2026"
    assert "2026-08" not in title


# ---- _post error messages ------------------------------------------------


def test_post_non_2xx_includes_response_body_snippet_in_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text='{"message": "Invalid Form Body", "code": 50035}')

    async def go():
        async with _mock_client(handler) as client:
            await discord_notify._post(_TEST_WEBHOOK, {"embeds": []}, http_client=client)

    with pytest.raises(discord_notify.DiscordSendError) as excinfo:
        _run(go())
    assert "Invalid Form Body" in str(excinfo.value)


def test_post_timeout_error_message_has_no_url():
    webhook_host = "discord.test"
    url = f"https://{webhook_host}/api/webhooks/1/abc"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out", request=request)

    async def go():
        async with _mock_client(handler) as client:
            await discord_notify._post(url, {"embeds": []}, http_client=client)

    with pytest.raises(discord_notify.DiscordSendError) as excinfo:
        _run(go())
    assert webhook_host not in str(excinfo.value)


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
    """A fresh temp file-backed database, with discord_config enabled
    and a webhook configured -- see this module's own docstring for why
    _drain_once needs a real file rather than ':memory:'. Config lives
    in the DB now (discord_config), not settings, so this writes
    directly to that row via its own short-lived connection rather than
    monkeypatching settings.discord_webhook_announcements.
    """
    path = str(tmp_path / "game.db")
    _init_schema(path)
    monkeypatch.setattr(db.settings, "db_path", path)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(
        "UPDATE discord_config SET enabled = 1, webhook_url = ? WHERE id = 1",
        (_TEST_WEBHOOK,),
    )
    conn.close()
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


def _set_channel(path_or_conn, kind: str, *, webhook_url: str = "", enabled: int = 1) -> None:
    """Write one discord_channel row directly, on either a file path
    (drain-loop tests, which need their own short-lived connection like
    _enable_discord()'s file-backed cousins below) or an already-open
    connection (enqueue()-level tests using the in-memory `conn`
    fixture)."""
    owns_conn = isinstance(path_or_conn, str)
    conn = sqlite3.connect(path_or_conn, isolation_level=None) if owns_conn else path_or_conn
    conn.execute(
        "INSERT INTO discord_channel(kind, webhook_url, enabled, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(kind) DO UPDATE SET webhook_url = excluded.webhook_url, "
        " enabled = excluded.enabled, updated_at = excluded.updated_at",
        (kind, webhook_url, enabled, int(time.time())),
    )
    if owns_conn:
        conn.close()


# ---- per-kind channel routing (Piece 1) -----------------------------------


def test_resolve_discord_webhook_no_row_falls_back_to_default():
    cfg = {"webhook_url": _TEST_WEBHOOK}
    assert discord_notify.resolve_discord_webhook(cfg, {}, "month_honors") == _TEST_WEBHOOK


def test_resolve_discord_webhook_own_route_wins_over_default():
    cfg = {"webhook_url": _TEST_WEBHOOK}
    channels = {"month_honors": {"webhook_url": "https://discord.test/own", "enabled": 1}}
    assert discord_notify.resolve_discord_webhook(cfg, channels, "month_honors") == "https://discord.test/own"


def test_resolve_discord_webhook_disabled_route_returns_none_not_default():
    """enabled=0 means 'do not announce' -- NOT a fallback to the
    default webhook. Silently falling back here would post to the main
    channel exactly when an operator tried to switch a kind off."""
    cfg = {"webhook_url": _TEST_WEBHOOK}
    channels = {"month_honors": {"webhook_url": "https://discord.test/own", "enabled": 0}}
    assert discord_notify.resolve_discord_webhook(cfg, channels, "month_honors") is None


def test_resolve_discord_webhook_enabled_but_blank_url_falls_back_to_default():
    cfg = {"webhook_url": _TEST_WEBHOOK}
    channels = {"month_honors": {"webhook_url": "", "enabled": 1}}
    assert discord_notify.resolve_discord_webhook(cfg, channels, "month_honors") == _TEST_WEBHOOK


def test_resolve_discord_webhook_most_specific_first_prefers_instance_kind():
    """A colon-scoped kind ('net_wrapup:12') resolves against its OWN
    per-instance row first, before ever falling back to the generic
    'net_wrapup' prefix -- so an operator CAN split one instance onto
    its own channel without that being required for every instance."""
    cfg = {"webhook_url": _TEST_WEBHOOK}
    channels = {
        "net_wrapup": {"webhook_url": "https://discord.test/generic", "enabled": 1},
        "net_wrapup:12": {"webhook_url": "https://discord.test/net12", "enabled": 1},
    }
    assert discord_notify.resolve_discord_webhook(cfg, channels, "net_wrapup:12") == "https://discord.test/net12"


def test_resolve_discord_webhook_falls_back_to_generic_kind_when_instance_absent():
    """No per-instance override for THIS instance -- falls back to the
    generic prefix shared by every instance with no override of its
    own, before ever reaching the app-wide default."""
    cfg = {"webhook_url": _TEST_WEBHOOK}
    channels = {"net_wrapup": {"webhook_url": "https://discord.test/generic", "enabled": 1}}
    assert discord_notify.resolve_discord_webhook(cfg, channels, "net_wrapup:99") == "https://discord.test/generic"


def test_resolve_discord_webhook_no_route_at_all_falls_back_to_default_for_scoped_kind():
    cfg = {"webhook_url": _TEST_WEBHOOK}
    assert discord_notify.resolve_discord_webhook(cfg, {}, "net_wrapup:99") == _TEST_WEBHOOK


def test_channel_kind_candidates_unscoped_kind_is_single_element():
    assert discord_notify._channel_kind_candidates("month_honors") == ["month_honors"]


def test_channel_kind_candidates_scoped_kind_tries_specific_then_generic():
    assert discord_notify._channel_kind_candidates("net_wrapup:12") == ["net_wrapup:12", "net_wrapup"]


def test_enqueue_skips_kind_with_disabled_route(conn):
    """A kind whose discord_channel row is explicitly enabled=0 is not
    queued at all -- distinct from `enabled` on discord_config itself,
    which is left on here."""
    _enable_discord(conn)
    _set_channel(conn, "month_honors", webhook_url="https://discord.test/own", enabled=0)
    result = discord_notify.enqueue(
        conn, kind="month_honors", key="2026-08:mc", payload={"embeds": []}, now=int(time.time()),
    )
    assert result is False
    rows = conn.execute("SELECT * FROM discord_outbox").fetchall()
    assert rows == []


def test_enqueue_uses_own_route_but_still_queues_when_enabled(conn):
    _enable_discord(conn)
    _set_channel(conn, "month_honors", webhook_url="https://discord.test/own", enabled=1)
    result = discord_notify.enqueue(
        conn, kind="month_honors", key="2026-08:mc", payload={"embeds": []}, now=int(time.time()),
    )
    assert result is True
    rows = conn.execute("SELECT * FROM discord_outbox").fetchall()
    assert len(rows) == 1


def test_drain_loop_posts_to_own_route_not_default(db_path):
    """A kind with its own enabled route posts to THAT route's webhook,
    never the default configured on discord_config."""
    _set_channel(db_path, "month_honors", webhook_url="https://discord.test/own-channel", enabled=1)
    row_id = _insert_row(db_path)

    posted_to = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted_to.append(str(request.url))
        return httpx.Response(204)

    async def go():
        async with _mock_client(handler) as client:
            await discord_notify._drain_once(http_client=client)

    _run(go())

    assert posted_to == ["https://discord.test/own-channel"]
    posted_at, attempts, last_error = _read_row(db_path, row_id)
    assert posted_at is not None


def test_drain_loop_falls_back_to_default_when_no_route(db_path):
    """A kind with no discord_channel row at all falls back to the
    default webhook -- db_path's own fixture already configures that
    default (_TEST_WEBHOOK)."""
    row_id = _insert_row(db_path)

    posted_to = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted_to.append(str(request.url))
        return httpx.Response(204)

    async def go():
        async with _mock_client(handler) as client:
            await discord_notify._drain_once(http_client=client)

    _run(go())

    assert posted_to == [_TEST_WEBHOOK]
    posted_at, attempts, last_error = _read_row(db_path, row_id)
    assert posted_at is not None


def test_drain_loop_disabled_route_leaves_row_pending_no_fallback(db_path):
    """A row queued for a kind that is now enabled=0 must not post at
    all -- not to its own (disabled) route, and not to the default --
    and must be left pending, untouched, rather than marked failed."""
    _set_channel(db_path, "month_honors", webhook_url="https://discord.test/own-channel", enabled=0)
    row_id = _insert_row(db_path)

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
    assert last_error is None


def test_drain_loop_resolves_channel_at_post_time_not_enqueue_time(db_path):
    """Changing a route's webhook AFTER a row is already queued must
    send the PENDING row to the NEW url -- proves the drain loop
    resolves the channel fresh at post time, never storing a target on
    the outbox row itself."""
    _set_channel(db_path, "month_honors", webhook_url="https://discord.test/old-channel", enabled=1)
    row_id = _insert_row(db_path)

    # Operator moves the channel AFTER the row was already queued.
    _set_channel(db_path, "month_honors", webhook_url="https://discord.test/new-channel", enabled=1)

    posted_to = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted_to.append(str(request.url))
        return httpx.Response(204)

    async def go():
        async with _mock_client(handler) as client:
            await discord_notify._drain_once(http_client=client)

    _run(go())

    assert posted_to == ["https://discord.test/new-channel"]
    posted_at, attempts, last_error = _read_row(db_path, row_id)
    assert posted_at is not None


# ---- time-driven announcements (Piece 2) ----------------------------------


def test_check_due_time_driven_empty_registry_enqueues_nothing(conn):
    _enable_discord(conn)
    inserted = discord_notify.check_due_time_driven(conn, int(time.time()))
    assert inserted == 0
    rows = conn.execute("SELECT * FROM discord_outbox").fetchall()
    assert rows == []


def test_check_due_time_driven_empty_registry_safe_to_call_repeatedly(conn):
    _enable_discord(conn)
    now = int(time.time())
    discord_notify.check_due_time_driven(conn, now)
    discord_notify.check_due_time_driven(conn, now)
    rows = conn.execute("SELECT * FROM discord_outbox").fetchall()
    assert rows == []


def test_check_due_time_driven_called_twice_same_period_enqueues_once(conn, monkeypatch):
    """A fake registry entry standing in for a real time-driven kind
    (none shipped in this task) -- the SAME period key on a second call
    must be a no-op, proving discord_outbox's own UNIQUE(kind, key) is
    the entire once-per-period guard, with no separate 'last run' state
    anywhere in this module."""
    _enable_discord(conn)

    def fake_provider(conn, now):
        return [{"kind": "fake_weekly", "key": "2026-W37", "payload": {"embeds": []}}]

    monkeypatch.setattr(discord_notify, "TIME_DRIVEN_PROVIDERS", [fake_provider])

    now = int(time.time())
    first = discord_notify.check_due_time_driven(conn, now)
    second = discord_notify.check_due_time_driven(conn, now)

    assert first == 1
    assert second == 0
    rows = conn.execute("SELECT * FROM discord_outbox WHERE kind = 'fake_weekly'").fetchall()
    assert len(rows) == 1


def test_check_due_time_driven_provider_returning_multiple_items_enqueues_all(conn, monkeypatch):
    """A net-derived-shaped provider can return more than one due item
    in a single check (one per currently-due instance) -- every one of
    them must be enqueued, not just the first."""
    _enable_discord(conn)

    def fake_net_provider(conn, now):
        return [
            {"kind": "fake_net_wrapup:1", "key": "1:2026-09-14", "payload": {"embeds": []}},
            {"kind": "fake_net_wrapup:2", "key": "2:2026-09-15", "payload": {"embeds": []}},
        ]

    monkeypatch.setattr(discord_notify, "TIME_DRIVEN_PROVIDERS", [fake_net_provider])

    inserted = discord_notify.check_due_time_driven(conn, int(time.time()))

    assert inserted == 2
    rows = conn.execute(
        "SELECT kind, key FROM discord_outbox WHERE kind LIKE 'fake_net_wrapup:%' ORDER BY kind"
    ).fetchall()
    assert [(r["kind"], r["key"]) for r in rows] == [
        ("fake_net_wrapup:1", "1:2026-09-14"),
        ("fake_net_wrapup:2", "2:2026-09-15"),
    ]


def test_check_due_time_driven_provider_returning_empty_list_is_noop(conn, monkeypatch):
    _enable_discord(conn)

    def fake_provider_nothing_due(conn, now):
        return []

    monkeypatch.setattr(discord_notify, "TIME_DRIVEN_PROVIDERS", [fake_provider_nothing_due])

    inserted = discord_notify.check_due_time_driven(conn, int(time.time()))

    assert inserted == 0
    rows = conn.execute("SELECT * FROM discord_outbox").fetchall()
    assert rows == []


def test_check_due_time_driven_one_provider_failing_does_not_stop_another(conn, monkeypatch, caplog):
    _enable_discord(conn)

    def broken_provider(conn, now):
        raise RuntimeError("boom")

    def working_provider(conn, now):
        return [{"kind": "fake_weekly", "key": "2026-W37", "payload": {"embeds": []}}]

    monkeypatch.setattr(discord_notify, "TIME_DRIVEN_PROVIDERS", [broken_provider, working_provider])

    with caplog.at_level("ERROR"):
        inserted = discord_notify.check_due_time_driven(conn, int(time.time()))

    assert inserted == 1
    rows = conn.execute("SELECT * FROM discord_outbox WHERE kind = 'fake_weekly'").fetchall()
    assert len(rows) == 1


def test_check_due_time_driven_passes_conn_and_now_to_each_provider(conn, monkeypatch):
    """The provider contract this task changes: provider(conn, now), not
    provider() -- a provider needs database access (check_due_time_driven()
    already holds a connection; a provider must use THIS one, not open a
    second connection of its own inside an in-flight WriteSession), and
    `now` is the SAME clock check_due_time_driven() itself was called
    with, not a fresh time.time() read inside the provider."""
    _enable_discord(conn)
    received = {}

    def recording_provider(passed_conn, passed_now):
        received["conn"] = passed_conn
        received["now"] = passed_now
        return []

    monkeypatch.setattr(discord_notify, "TIME_DRIVEN_PROVIDERS", [recording_provider])

    now = int(time.time())
    discord_notify.check_due_time_driven(conn, now)

    assert received["conn"] is conn
    assert received["now"] == now


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


# ---- build_season_close_embed -------------------------------------------


def test_build_season_close_embed_standings_have_dots_and_bold_totals(conn):
    _enable_discord(conn, team_emoji="RED=<:mw_red:1>,BLUE=<:mw_blue:2>")
    tallies = [{"team": "RED", "total": 6120.0}, {"team": "BLUE", "total": 80.0}]
    embed = discord_notify.build_season_close_embed(
        conn, "mc", {"id": 7, "winner": "RED"}, tallies,
    )
    standings = next(f for f in embed["embeds"][0]["fields"] if f["name"] == "Final standings")
    assert "<:mw_red:1>" in standings["value"]
    assert "<:mw_blue:2>" in standings["value"]
    # Bold, thousands-separated, no trailing ".0".
    assert "**6,120**" in standings["value"]
    assert "**80**" in standings["value"]


def test_build_season_close_embed_title_names_protocol_and_season(conn):
    _enable_discord(conn)
    embed = discord_notify.build_season_close_embed(
        conn, "mc", {"id": 42, "winner": "RED"}, [{"team": "RED", "total": 10.0}],
    )
    assert embed["embeds"][0]["title"] == "MeshCore — Season #42 closed"


def test_build_season_close_embed_meshtastic_protocol_name(conn):
    _enable_discord(conn)
    embed = discord_notify.build_season_close_embed(
        conn, "mt", {"id": 1, "winner": "RED"}, [{"team": "RED", "total": 10.0}],
    )
    assert "Meshtastic" in embed["embeds"][0]["title"]


def test_build_season_close_embed_names_winner(conn):
    _enable_discord(conn)
    embed = discord_notify.build_season_close_embed(
        conn, "mc", {"id": 1, "winner": "BLUE"}, [{"team": "BLUE", "total": 55.0}],
    )
    winner = next(f for f in embed["embeds"][0]["fields"] if f["name"] == "Winner")
    assert "BLUE" in winner["value"]


def test_build_season_close_embed_tie_renders_as_tie_not_a_team_name(conn):
    """winner == 'TIE' must read as a sentence saying it's a tie, never
    as the bare literal 'TIE' printed as if it named a team -- and no
    colour is looked up for it."""
    _enable_discord(conn)
    embed = discord_notify.build_season_close_embed(
        conn, "mc", {"id": 2, "winner": "TIE"},
        [{"team": "RED", "total": 50.0}, {"team": "BLUE", "total": 50.0}],
    )
    winner = next(f for f in embed["embeds"][0]["fields"] if f["name"] == "Winner")
    assert winner["value"] == "It's a tie!"
    assert "color" not in embed["embeds"][0]


def test_build_season_close_embed_colors_by_winning_team(conn):
    _enable_discord(conn)
    embed = discord_notify.build_season_close_embed(
        conn, "mc", {"id": 3, "winner": "GREEN"}, [{"team": "GREEN", "total": 99.0}],
    )
    assert embed["embeds"][0]["color"] == discord_notify._TEAM_COLORS["GREEN"]


def test_build_season_close_embed_no_color_for_unknown_winner(conn):
    _enable_discord(conn)
    embed = discord_notify.build_season_close_embed(
        conn, "mc", {"id": 5, "winner": "NOTATEAM"}, [{"team": "NOTATEAM", "total": 1.0}],
    )
    assert "color" not in embed["embeds"][0]


def test_build_season_close_embed_never_exceeds_discord_limits(conn):
    """Regression guard mirroring build_month_honors_embed()'s own --
    however many teams a season ever ends up with, no single embed may
    carry more than 25 fields, no field value may exceed 1024
    characters, and the whole payload must stay under the 6000-character
    total budget."""
    _enable_discord(conn)
    # 80 long-named teams -- enough that the joined standings field
    # genuinely overflows _MAX_FIELD_VALUE_CHARS and _join_team_field()'s
    # own bottom-truncation actually has to trim something, not just a
    # guard that happens to never fire.
    tallies = [{"team": f"TEAM_WITH_A_LONG_NAME_{n:03d}", "total": 100000 - n} for n in range(80)]
    embed = discord_notify.build_season_close_embed(
        conn, "mc", {"id": 9, "winner": "TEAM_WITH_A_LONG_NAME_000"}, tallies,
    )
    standings = next(f for f in embed["embeds"][0]["fields"] if f["name"] == "Final standings")
    assert discord_notify._TRUNCATION_MARKER in standings["value"]
    for e in embed["embeds"]:
        assert len(e.get("fields") or []) <= discord_notify._MAX_EMBED_FIELDS
        for f in e.get("fields") or []:
            assert len(f["value"]) <= discord_notify._MAX_FIELD_VALUE_CHARS
    assert discord_notify._total_embed_chars(embed["embeds"]) <= discord_notify._MAX_TOTAL_EMBED_CHARS


def test_build_season_close_embed_no_double_dash_separator(conn):
    _enable_discord(conn)
    embed = discord_notify.build_season_close_embed(
        conn, "mc", {"id": 4, "winner": "RED"}, [{"team": "RED", "total": 5.0}],
    )
    assert "--" not in json.dumps(embed)


def test_build_season_close_embed_url_absolute_or_omitted(conn, monkeypatch):
    monkeypatch.setattr(settings, "oauth_public_base_url", "")
    embed = discord_notify.build_season_close_embed(
        conn, "mc", {"id": 1, "winner": "RED"}, [{"team": "RED", "total": 1.0}],
    )
    assert "url" not in embed["embeds"][0]

    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    embed = discord_notify.build_season_close_embed(
        conn, "mc", {"id": 1, "winner": "RED"}, [{"team": "RED", "total": 1.0}],
    )
    assert embed["embeds"][0]["url"].startswith("https://")


def test_build_season_close_embed_no_emoji(conn):
    embed = discord_notify.build_season_close_embed(
        conn, "mc", {"id": 1, "winner": "RED"}, [{"team": "RED", "total": 1.0}],
    )
    assert not _has_emoji(json.dumps(embed))


# ---- per-kind announce_* toggles -----------------------------------------


def test_enqueue_is_noop_when_announce_season_close_off(conn):
    _enable_discord(conn)
    conn.execute("UPDATE discord_config SET announce_season_close = 0 WHERE id = 1")
    inserted = discord_notify.enqueue(conn, kind="season_close", key="7", payload={}, now=int(time.time()))
    assert inserted is False
    assert conn.execute("SELECT * FROM discord_outbox").fetchall() == []


def test_enqueue_is_noop_when_announce_weekly_recap_off(conn):
    _enable_discord(conn)
    conn.execute("UPDATE discord_config SET announce_weekly_recap = 0 WHERE id = 1")
    inserted = discord_notify.enqueue(conn, kind="weekly_recap", key="2026-W37", payload={}, now=int(time.time()))
    assert inserted is False
    assert conn.execute("SELECT * FROM discord_outbox").fetchall() == []


def test_announce_season_close_off_does_not_suppress_other_kinds(conn):
    """Each per-kind toggle gates only its OWN kind -- turning
    announce_season_close off must not touch month_honors or
    weekly_recap."""
    _enable_discord(conn)
    conn.execute("UPDATE discord_config SET announce_season_close = 0 WHERE id = 1")
    now = int(time.time())
    assert discord_notify.enqueue(conn, kind="month_honors", key="2026-08:mc", payload={}, now=now) is True
    assert discord_notify.enqueue(conn, kind="weekly_recap", key="2026-W37", payload={}, now=now) is True
    assert discord_notify.enqueue(conn, kind="season_close", key="7", payload={}, now=now) is False


def test_announce_weekly_recap_off_does_not_suppress_other_kinds(conn):
    _enable_discord(conn)
    conn.execute("UPDATE discord_config SET announce_weekly_recap = 0 WHERE id = 1")
    now = int(time.time())
    assert discord_notify.enqueue(conn, kind="month_honors", key="2026-08:mc", payload={}, now=now) is True
    assert discord_notify.enqueue(conn, kind="season_close", key="7", payload={}, now=now) is True
    assert discord_notify.enqueue(conn, kind="weekly_recap", key="2026-W37", payload={}, now=now) is False


def test_announce_month_honors_off_does_not_suppress_the_two_new_kinds(conn):
    _enable_discord(conn, announce_month_honors=0)
    now = int(time.time())
    assert discord_notify.enqueue(conn, kind="month_honors", key="2026-08:mc", payload={}, now=now) is False
    assert discord_notify.enqueue(conn, kind="season_close", key="7", payload={}, now=now) is True
    assert discord_notify.enqueue(conn, kind="weekly_recap", key="2026-W37", payload={}, now=now) is True


def test_enqueue_season_close_same_key_twice_leaves_exactly_one_row(conn):
    _enable_discord(conn)
    now = int(time.time())
    first = discord_notify.enqueue(conn, kind="season_close", key="7", payload={}, now=now)
    second = discord_notify.enqueue(conn, kind="season_close", key="7", payload={}, now=now)
    assert first is True
    assert second is False
    rows = conn.execute("SELECT * FROM discord_outbox WHERE kind = 'season_close'").fetchall()
    assert len(rows) == 1


def test_enqueue_weekly_recap_same_key_twice_leaves_exactly_one_row(conn):
    _enable_discord(conn)
    now = int(time.time())
    first = discord_notify.enqueue(conn, kind="weekly_recap", key="2026-W37", payload={}, now=now)
    second = discord_notify.enqueue(conn, kind="weekly_recap", key="2026-W37", payload={}, now=now)
    assert first is True
    assert second is False
    rows = conn.execute("SELECT * FROM discord_outbox WHERE kind = 'weekly_recap'").fetchall()
    assert len(rows) == 1


# ---- weekly recap (Sunday, TIME_DRIVEN_PROVIDERS) -------------------------
#
# 2026-09-13 and 2026-09-20 are both real Sundays in
# settings.checkin_net_timezone's default, America/Boise -- confirmed
# below (test_weekly_recap_due_fires_on_sunday_with_correct_window) and
# used throughout as fixed, known-good instants rather than computed
# relative to time.time(), so the exact ISO-week key a test asserts on
# never depends on when the test suite happens to run.


def _wr_season(conn, season_id=1, protocol="mc", started_at=0, ends_at=None):
    if ends_at is None:
        ends_at = started_at + 100_000_000
    conn.execute(
        "INSERT INTO mc_season(id, protocol, started_at, ends_at, status) "
        "VALUES (?, ?, ?, ?, 'active')",
        (season_id, protocol, started_at, ends_at),
    )


def _wr_player(conn, player_id, display_name, team):
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?, ?, ?, ?)",
        (player_id, display_name, team, int(time.time())),
    )


def _wr_capture(conn, season_id, cid, ts, by_player_id, by_team):
    conn.execute(
        "INSERT INTO mc_tile_capture_log(season_id, cell_id, ts, by_player_id, by_team, from_team) "
        "VALUES (?, ?, ?, ?, ?, NULL)",
        (season_id, cid, ts, by_player_id, by_team),
    )


def _wr_net(conn, net_id=1, label="Wednesday MC", protocol="mc", *,
            weekday=2, start_hour=18, end_hour=20, timezone="America/Boise", enabled=1):
    """A checkin_net row -- weekday/start_hour/end_hour/timezone/enabled
    are keyword-only, defaulting to the same Wednesday-evening
    America/Boise net every pre-existing caller in this file relies on,
    so net_wrapup_provider()'s own tests (below) can override just the
    scheduling fields they need to vary without disturbing anything
    else."""
    conn.execute(
        "INSERT INTO checkin_net(id, label, protocol, kind, connector_url, weekday, "
        " start_hour, end_hour, timezone, enabled, created_at) "
        "VALUES (?, ?, ?, 'corescope', 'http://x', ?, ?, ?, ?, ?, 0)",
        (net_id, label, protocol, weekday, start_hour, end_hour, timezone, enabled),
    )


def _wr_checkin(conn, *, season_id, player_id, awarded_at, streak=1, net_id=1, message_id="m", net_date="2026-09-09"):
    conn.execute(
        "INSERT INTO mc_checkin_award(season_id, player_id, net_date, points, protocol, "
        " message_id, awarded_at, streak, net_id) "
        "VALUES (?, ?, ?, 5, 'mc', ?, ?, ?, ?)",
        (season_id, player_id, net_date, message_id, awarded_at, streak, net_id),
    )


def test_weekly_recap_due_none_on_a_non_sunday():
    tz = ZoneInfo(settings.checkin_net_timezone)
    saturday = datetime(2026, 9, 12, 10, 0, tzinfo=tz)
    assert saturday.weekday() == 5
    assert discord_notify._weekly_recap_due(int(saturday.timestamp())) is None


def test_weekly_recap_due_fires_on_sunday_with_correct_window():
    tz = ZoneInfo(settings.checkin_net_timezone)
    sunday = datetime(2026, 9, 13, 15, 30, tzinfo=tz)
    assert sunday.weekday() == 6
    due = discord_notify._weekly_recap_due(int(sunday.timestamp()))
    assert due is not None
    key, start_ts, end_ts = due
    assert key == "2026-W37"
    expected_end = int(datetime(2026, 9, 13, tzinfo=tz).timestamp())
    assert end_ts == expected_end
    assert start_ts == expected_end - 7 * 86400


def test_weekly_recap_due_different_sundays_get_different_keys():
    tz = ZoneInfo(settings.checkin_net_timezone)
    key1, _, _ = discord_notify._weekly_recap_due(int(datetime(2026, 9, 13, 12, tzinfo=tz).timestamp()))
    key2, _, _ = discord_notify._weekly_recap_due(int(datetime(2026, 9, 20, 12, tzinfo=tz).timestamp()))
    assert key1 != key2


def test_build_weekly_recap_embed_empty_data_returns_none(conn):
    _enable_discord(conn)
    now = int(time.time())
    assert discord_notify.build_weekly_recap_embed(conn, "mc", now - 7 * 86400, now) is None


def test_build_weekly_recap_embed_placement_changes_section(conn):
    """Placement changes renders RANK MOVEMENT, not squares gained (the
    owner's own correction -- a squares-gained figure is positive for
    nearly every team nearly every week and never reads as movement):
    GREEN holds 10 squares, untouched all week, and stays 1st ("no
    change"). ORANGE starts with 2 squares (3rd) and captures 4 more
    DURING the window to reach 6, climbing to 2nd ("up from 3rd"). BLUE
    holds 5 squares, also untouched -- but is overtaken by ORANGE's
    gain, dropping from 2nd to 3rd ("down from 2nd") without losing a
    single square of its own. Ordered by CURRENT rank ascending."""
    _enable_discord(conn)
    now = int(time.time())
    start_ts, end_ts = now - 3 * 86400, now
    _wr_season(conn, 1, started_at=start_ts - 1_000_000, ends_at=end_ts + 1_000_000)
    _wr_player(conn, 1, "Gplayer", "GREEN")
    _wr_player(conn, 2, "Bplayer", "BLUE")
    _wr_player(conn, 3, "Oplayer", "ORANGE")
    for i in range(10):
        _wr_capture(conn, 1, f"g{i}_1", start_ts - 2000, 1, "GREEN")
    for i in range(5):
        _wr_capture(conn, 1, f"b{i}_1", start_ts - 2000, 2, "BLUE")
    for i in range(2):
        _wr_capture(conn, 1, f"o{i}_1", start_ts - 2000, 3, "ORANGE")
    for i in range(4):
        _wr_capture(conn, 1, f"o{i}_2", start_ts + 100, 3, "ORANGE")

    embed = discord_notify.build_weekly_recap_embed(conn, "mc", start_ts, end_ts)
    field = next(f for f in embed["embeds"][0]["fields"] if f["name"] == "Placement changes")
    lines = field["value"].splitlines()
    assert lines[0] == "GREEN **1st** *no change*"
    assert lines[1] == "ORANGE **2nd** ▲ *from 3rd*"
    assert lines[2] == "BLUE **3rd** ▼ *from 2nd*"


def test_weekly_placement_section_sorted_descending_by_current_standing(conn):
    """Order is by CURRENT rank ascending -- equivalently, by squares
    held descending -- with a stable tiebreak on team name; NOT by team
    name, and not by any order unrelated to the figure the field
    displays. A real production bug once put the largest gainer fifth in
    a differently-ordered list; this fixture's squares counts are
    deliberately unrelated to alphabetical team-name order, so an
    accidental alphabetical sort would fail this test."""
    _enable_discord(conn)
    now = int(time.time())
    start_ts, end_ts = now - 3 * 86400, now
    _wr_season(conn, 1, started_at=start_ts - 1_000_000, ends_at=end_ts + 1_000_000)
    _wr_player(conn, 1, "P1", "YELLOW")
    _wr_player(conn, 2, "P2", "RED")
    _wr_player(conn, 3, "P3", "BLUE")
    for i in range(3):
        _wr_capture(conn, 1, f"y{i}", start_ts - 2000, 1, "YELLOW")  # 3 squares -- 3rd
    for i in range(9):
        _wr_capture(conn, 1, f"r{i}", start_ts - 2000, 2, "RED")     # 9 squares -- 1st
    for i in range(6):
        _wr_capture(conn, 1, f"b{i}", start_ts - 2000, 3, "BLUE")    # 6 squares -- 2nd

    embed = discord_notify.build_weekly_recap_embed(conn, "mc", start_ts, end_ts)
    field = next(f for f in embed["embeds"][0]["fields"] if f["name"] == "Placement changes")
    teams_in_order = [line.split(" ", 1)[0] for line in field["value"].splitlines()]
    assert teams_in_order == ["RED", "BLUE", "YELLOW"]


def test_weekly_placement_section_team_absent_at_window_start_reads_new_to_the_board(conn):
    """A team with no PRIOR rank (held nothing at the window's open) gets
    a current rank but no up/down verdict -- there is nothing to compare
    against."""
    _enable_discord(conn)
    now = int(time.time())
    start_ts, end_ts = now - 3 * 86400, now
    _wr_season(conn, 1, started_at=start_ts - 1_000_000, ends_at=end_ts + 1_000_000)
    _wr_player(conn, 1, "P1", "PURPLE")
    _wr_capture(conn, 1, "p1_1", start_ts + 100, 1, "PURPLE")  # captured DURING the window only

    embed = discord_notify.build_weekly_recap_embed(conn, "mc", start_ts, end_ts)
    field = next(f for f in embed["embeds"][0]["fields"] if f["name"] == "Placement changes")
    assert field["value"] == "PURPLE **1st** *new to the board*"


def test_build_weekly_recap_embed_exploration_section_matches_exact_shape(conn):
    _enable_discord(conn)
    now = int(time.time())
    start_ts, end_ts = now - 3 * 86400, now + 4 * 86400
    _wr_season(conn, 1, started_at=start_ts - 1_000_000, ends_at=end_ts + 1_000_000)
    _wr_player(conn, 10, "l3@n", "RED")
    _wr_player(conn, 11, "Raptor", "GREEN")
    conn.execute(
        "INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, "
        "elevation_ft, points_reason, active, created_at) VALUES "
        "(1, 'summit', 's1', 'Peak One', 44, -116, 90, 'TEST', 9763, 'remote_scaled', 1, 0)"
    )
    conn.execute(
        "INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, "
        "elevation_ft, points_reason, active, created_at) VALUES "
        "(2, 'summit', 's2', 'Peak Two', 44, -116, 80, 'TEST', 8000, 'remote_scaled', 1, 0)"
    )
    conn.execute(
        "INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, "
        "points_reason, active, created_at) VALUES "
        "(3, 'park', 'p1', 'Park One', 44, -116, 25, 'TEST', 'remote', 1, 0)"
    )
    conn.execute(
        "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at, protocol) "
        "VALUES (1, 10, '2026-09-09', 90, ?, 'mc')", (start_ts + 10,),
    )
    conn.execute(
        "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at, protocol) "
        "VALUES (2, 10, '2026-09-09', 80, ?, 'mc')", (start_ts + 20,),
    )
    conn.execute(
        "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at, protocol) "
        "VALUES (3, 11, '2026-09-09', 25, ?, 'mc')", (start_ts + 30,),
    )

    embed = discord_notify.build_weekly_recap_embed(conn, "mc", start_ts, end_ts)
    field = next(f for f in embed["embeds"][0]["fields"] if f["name"] == "Exploration")
    lines = field["value"].splitlines()
    assert lines[0] == "**2** new summits and **1** new park"
    assert lines[1] == "Highest new summit: **9,763** *ft*"
    assert "l3@n" in field["value"] and "**2** *new*" in field["value"]
    assert "Raptor" in field["value"] and "**1** *new*" in field["value"]
    assert "first" not in field["value"].lower()


def test_weekly_exploration_section_header_says_new_not_first(conn):
    """The owner's own wording call ("labelling firsts is awkward i dont
    like it. use new instead") -- presentation only, the header line."""
    _enable_discord(conn)
    now = int(time.time())
    start_ts, end_ts = now - 3 * 86400, now + 4 * 86400
    _wr_season(conn, 1, started_at=start_ts - 1_000_000, ends_at=end_ts + 1_000_000)
    _wr_player(conn, 50, "Header1", "RED")
    conn.execute(
        "INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, "
        "elevation_ft, points_reason, active, created_at) VALUES "
        "(1, 'summit', 's1', 'Peak One', 44, -116, 90, 'TEST', 9763, 'remote_scaled', 1, 0)"
    )
    conn.execute(
        "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at, protocol) "
        "VALUES (1, 50, '2026-09-09', 90, ?, 'mc')", (start_ts + 10,),
    )

    embed = discord_notify.build_weekly_recap_embed(conn, "mc", start_ts, end_ts)
    field = next(f for f in embed["embeds"][0]["fields"] if f["name"] == "Exploration")
    lines = field["value"].splitlines()
    assert lines[0] == "**1** new summit and **0** new parks"
    assert "first" not in field["value"].lower()


def test_weekly_exploration_section_player_line_uses_new_not_firsts(conn):
    """A player with several qualifying firsts in the window still shows
    a bare count, now labelled "new" -- "firsts"/"first" must not
    appear anywhere in the rendered line."""
    _enable_discord(conn)
    now = int(time.time())
    start_ts, end_ts = now - 3 * 86400, now + 4 * 86400
    _wr_season(conn, 1, started_at=start_ts - 1_000_000, ends_at=end_ts + 1_000_000)
    _wr_player(conn, 60, "Trio", "BLUE")
    for i in range(3):
        conn.execute(
            "INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, "
            "elevation_ft, points_reason, active, created_at) VALUES "
            "(?, 'summit', ?, ?, 44, -116, 90, 'TEST', 9000, 'remote_scaled', 1, 0)",
            (i + 1, f"s{i}", f"Peak {i}"),
        )
        conn.execute(
            "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at, protocol) "
            "VALUES (?, 60, '2026-09-09', 90, ?, 'mc')", (i + 1, start_ts + 10 + i),
        )

    embed = discord_notify.build_weekly_recap_embed(conn, "mc", start_ts, end_ts)
    field = next(f for f in embed["embeds"][0]["fields"] if f["name"] == "Exploration")
    assert "Trio" in field["value"] and "**3** *new*" in field["value"]
    assert "firsts" not in field["value"].lower()


def test_weekly_placement_section_uses_arrows_not_up_down_words(conn):
    """Owner's own call ("use arrows instead of up and down words"): a
    team that climbed shows the up-triangle and no "up from" text; a
    team that dropped shows the down-triangle and no "down from" text."""
    _enable_discord(conn)
    now = int(time.time())
    start_ts, end_ts = now - 3 * 86400, now
    _wr_season(conn, 1, started_at=start_ts - 1_000_000, ends_at=end_ts + 1_000_000)
    _wr_player(conn, 1, "Gplayer", "GREEN")
    _wr_player(conn, 2, "Bplayer", "BLUE")
    _wr_player(conn, 3, "Oplayer", "ORANGE")
    for i in range(10):
        _wr_capture(conn, 1, f"g{i}_1", start_ts - 2000, 1, "GREEN")
    for i in range(5):
        _wr_capture(conn, 1, f"b{i}_1", start_ts - 2000, 2, "BLUE")
    for i in range(2):
        _wr_capture(conn, 1, f"o{i}_1", start_ts - 2000, 3, "ORANGE")
    for i in range(4):
        _wr_capture(conn, 1, f"o{i}_2", start_ts + 100, 3, "ORANGE")

    embed = discord_notify.build_weekly_recap_embed(conn, "mc", start_ts, end_ts)
    field = next(f for f in embed["embeds"][0]["fields"] if f["name"] == "Placement changes")
    value = field["value"]
    orange_line = next(line for line in value.splitlines() if line.startswith("ORANGE"))
    blue_line = next(line for line in value.splitlines() if line.startswith("BLUE"))
    green_line = next(line for line in value.splitlines() if line.startswith("GREEN"))

    assert "▲" in orange_line
    assert "up from" not in value.lower()
    assert "▼" in blue_line
    assert "down from" not in value.lower()

    # The unchanged team gets neither arrow glyph -- an arrow would
    # actively lie about movement that didn't happen.
    assert "▲" not in green_line
    assert "▼" not in green_line


def test_qualifying_place_firsts_name_unchanged(conn):
    """Guard against a well-meaning rename: the "new" wording is a
    PRESENTATION change only (app/discord_notify.py's render site) --
    the underlying rule and its function name in app/place_scoring.py
    must stay qualifying_place_firsts()."""
    from app import place_scoring

    assert hasattr(place_scoring, "qualifying_place_firsts")
    assert callable(place_scoring.qualifying_place_firsts)
    assert place_scoring.qualifying_place_firsts.__name__ == "qualifying_place_firsts"


def test_build_weekly_recap_embed_never_has_a_nets_field(conn):
    """The owner rejected a weekly roll-up of nets -- nets get their own
    per-net wrap-up instead (net_wrapup_provider()) -- so even a week
    with real check-in activity (which used to feed a "Nets" field) AND
    real placement/exploration data must never surface one here."""
    _enable_discord(conn)
    now = int(time.time())
    start_ts, end_ts = now - 3 * 86400, now + 4 * 86400
    _wr_season(conn, 1, started_at=start_ts - 1_000_000, ends_at=end_ts + 1_000_000)
    _wr_player(conn, 20, "l3@n", "RED")
    _wr_player(conn, 21, "Raptor", "GREEN")
    _wr_net(conn, 1, "Wednesday MC")
    _wr_checkin(conn, season_id=1, player_id=20, awarded_at=start_ts + 10, streak=3, message_id="m1")
    _wr_checkin(conn, season_id=1, player_id=21, awarded_at=start_ts + 20, streak=1, message_id="m2")
    _wr_capture(conn, 1, "1_1", start_ts + 5, 20, "RED")

    embed = discord_notify.build_weekly_recap_embed(conn, "mc", start_ts, end_ts)
    assert embed is not None
    names = [f["name"] for f in embed["embeds"][0]["fields"]]
    assert "Nets" not in names
    assert "Placement changes" in names


def test_build_weekly_recap_embed_never_pairs_a_place_name_with_a_player_name(conn):
    """HARD PRIVACY: qualifying_place_firsts() rows (app/place_scoring.py)
    carry a place name right alongside a player name -- see that
    function's own HARD PRIVACY WARNING. The rendered recap must never
    let the two appear together: this fixture gives both a maximally
    distinctive value and asserts the place name never appears in the
    rendered message AT ALL, which is strictly stronger than merely
    "not on the same line.\""""
    _enable_discord(conn)
    now = int(time.time())
    start_ts, end_ts = now - 3 * 86400, now + 4 * 86400
    _wr_season(conn, 1, started_at=start_ts - 1_000_000, ends_at=end_ts + 1_000_000)
    _wr_player(conn, 30, "VeryUniquePlayerName", "RED")
    conn.execute(
        "INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, "
        "elevation_ft, points_reason, active, created_at) VALUES "
        "(1, 'summit', 's1', 'ExtremelyDistinctivePlaceName', 44, -116, 90, 'TEST', "
        "9763, 'remote_scaled', 1, 0)"
    )
    conn.execute(
        "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at, protocol) "
        "VALUES (1, 30, '2026-09-09', 90, ?, 'mc')", (start_ts + 10,),
    )

    embed = discord_notify.build_weekly_recap_embed(conn, "mc", start_ts, end_ts)
    full_text = json.dumps(embed)
    assert "VeryUniquePlayerName" in full_text
    assert "ExtremelyDistinctivePlaceName" not in full_text


def test_weekly_recap_provider_returns_nothing_on_a_non_sunday(conn):
    _enable_discord(conn)
    tz = ZoneInfo(settings.checkin_net_timezone)
    saturday = datetime(2026, 9, 12, 10, 0, tzinfo=tz)
    assert discord_notify.weekly_recap_provider(conn, int(saturday.timestamp())) == []


def test_weekly_recap_provider_empty_week_returns_nothing_and_enqueues_no_message(conn):
    """An entirely empty week -- no captures, no exploration, no
    check-ins -- must not post a message that says so; it must post
    nothing at all."""
    _enable_discord(conn)
    tz = ZoneInfo(settings.checkin_net_timezone)
    sunday = datetime(2026, 9, 13, 15, 0, tzinfo=tz)
    items = discord_notify.weekly_recap_provider(conn, int(sunday.timestamp()))
    assert items == []
    assert conn.execute("SELECT * FROM discord_outbox").fetchall() == []


def test_weekly_recap_provider_respects_its_own_toggle(conn):
    _enable_discord(conn)
    conn.execute("UPDATE discord_config SET announce_weekly_recap = 0 WHERE id = 1")
    tz = ZoneInfo(settings.checkin_net_timezone)
    sunday = datetime(2026, 9, 13, 15, 0, tzinfo=tz)
    # Give it real data so an "empty week" false-negative can't hide a
    # broken toggle check -- a capture, since check-ins alone no longer
    # produce any weekly recap content (the Nets section was removed;
    # see net_wrapup_provider()'s own docstring for what replaced it).
    _wr_season(conn, 1, started_at=0, ends_at=int(sunday.timestamp()) + 100_000_000)
    _wr_player(conn, 1, "Alice", "RED")
    _wr_capture(conn, 1, "1_1", int(sunday.timestamp()) - 3 * 86400, 1, "RED")

    items = discord_notify.weekly_recap_provider(conn, int(sunday.timestamp()))
    assert items == []


def test_check_due_time_driven_weekly_recap_fires_once_per_week_then_again_next_week(conn, monkeypatch):
    """Called twice inside the same ISO week, this enqueues exactly one
    row (discord_outbox's own UNIQUE(kind, key), via enqueue()'s INSERT
    OR IGNORE); called again the following Sunday, a second, distinct
    row."""
    monkeypatch.setattr(discord_notify, "TIME_DRIVEN_PROVIDERS", [discord_notify.weekly_recap_provider])
    _enable_discord(conn)
    tz = ZoneInfo(settings.checkin_net_timezone)
    sunday1 = datetime(2026, 9, 13, 12, 0, tzinfo=tz)
    sunday2 = datetime(2026, 9, 20, 12, 0, tzinfo=tz)
    _wr_season(conn, 1, started_at=0, ends_at=int(sunday2.timestamp()) + 100_000_000)
    _wr_player(conn, 1, "Alice", "RED")
    # A capture, not a check-in -- check-ins alone no longer produce any
    # weekly recap content (the Nets section was removed; see
    # net_wrapup_provider()'s own docstring for what replaced it).
    _wr_capture(conn, 1, "1_1", int(sunday1.timestamp()) - 3 * 86400, 1, "RED")

    now1 = int(sunday1.timestamp())
    first = discord_notify.check_due_time_driven(conn, now1)
    second = discord_notify.check_due_time_driven(conn, now1)
    assert first == 1
    assert second == 0
    rows = conn.execute("SELECT key FROM discord_outbox WHERE kind = 'weekly_recap'").fetchall()
    assert len(rows) == 1
    assert rows[0]["key"] == "2026-W37:mc"

    # A fresh capture inside the SECOND week, on a NEW cell, so its own
    # recap is not itself an empty week (which build_weekly_recap_embed()
    # -- correctly -- posts nothing for, see that function's own test
    # above).
    _wr_capture(conn, 1, "2_2", int(sunday2.timestamp()) - 3 * 86400, 1, "RED")
    third = discord_notify.check_due_time_driven(conn, int(sunday2.timestamp()))
    assert third == 1
    rows = conn.execute("SELECT key FROM discord_outbox WHERE kind = 'weekly_recap'").fetchall()
    assert len(rows) == 2


# ---- weekly recap: per-protocol emission -----------------------------


def test_weekly_recap_provider_emits_one_item_per_protocol_with_active_season(conn):
    """The recap is per-protocol, exactly like month honors (see
    _active_recap_protocols()'s own docstring) -- a week with an active
    season on BOTH boards posts TWO items, one per protocol, each keyed
    by its own protocol so the two dedupe entirely independently."""
    _enable_discord(conn)
    tz = ZoneInfo(settings.checkin_net_timezone)
    sunday = datetime(2026, 9, 13, 12, 0, tzinfo=tz)
    now = int(sunday.timestamp())
    _wr_season(conn, 1, protocol="mc", started_at=0, ends_at=now + 100_000_000)
    _wr_season(conn, 2, protocol="mt", started_at=0, ends_at=now + 100_000_000)
    _wr_player(conn, 1, "McPlayer", "RED")
    _wr_player(conn, 2, "MtPlayer", "BLUE")
    _wr_capture(conn, 1, "1_1", now - 3 * 86400, 1, "RED")
    _wr_capture(conn, 2, "2_2", now - 3 * 86400, 2, "BLUE")

    items = discord_notify.weekly_recap_provider(conn, now)
    assert len(items) == 2
    assert {i["kind"] for i in items} == {"weekly_recap"}
    assert {i["key"] for i in items} == {"2026-W37:mc", "2026-W37:mt"}


def test_weekly_recap_provider_only_protocol_with_active_season_gets_an_item(conn):
    """Only ONE board has an active season this week -- exactly one
    item, for that protocol alone; the other board is silent, not
    posted as an empty recap."""
    _enable_discord(conn)
    tz = ZoneInfo(settings.checkin_net_timezone)
    sunday = datetime(2026, 9, 13, 12, 0, tzinfo=tz)
    now = int(sunday.timestamp())
    _wr_season(conn, 1, protocol="mc", started_at=0, ends_at=now + 100_000_000)
    _wr_player(conn, 1, "McPlayer", "RED")
    _wr_capture(conn, 1, "1_1", now - 3 * 86400, 1, "RED")

    items = discord_notify.weekly_recap_provider(conn, now)
    assert len(items) == 1
    assert items[0]["key"] == "2026-W37:mc"


# ---- per-net wrap-up (net_wrapup_provider, TIME_DRIVEN_PROVIDERS) --------
#
# 2026-09-09 is a real Wednesday and 2026-09-10 the Thursday right after
# it, both in America/Boise -- consistent with the fixed Sundays the
# weekly recap tests above already anchor on (2026-09-13). Used
# throughout as known-good instants for the same reason those are: the
# exact date/key a test asserts on must never depend on when the suite
# happens to run.


def test_due_net_wrapups_fires_at_8am_the_day_after_in_the_nets_own_timezone(conn):
    _wr_net(conn, 1, "Wednesday MC", weekday=2, start_hour=18, end_hour=20, timezone="America/Boise")
    tz = ZoneInfo("America/Boise")

    wed_evening = int(datetime(2026, 9, 9, 19, 0, tzinfo=tz).timestamp())   # the net's own day
    thu_early = int(datetime(2026, 9, 10, 7, 59, tzinfo=tz).timestamp())    # day after, before 08:00
    thu_8am = int(datetime(2026, 9, 10, 8, 0, tzinfo=tz).timestamp())       # day after, exactly 08:00
    thu_late = int(datetime(2026, 9, 10, 22, 0, tzinfo=tz).timestamp())     # day after, well past 08:00
    fri = int(datetime(2026, 9, 11, 9, 0, tzinfo=tz).timestamp())           # two days after

    assert discord_notify._due_net_wrapups(conn, wed_evening) == []
    assert discord_notify._due_net_wrapups(conn, thu_early) == []

    due = discord_notify._due_net_wrapups(conn, thu_8am)
    assert len(due) == 1
    assert due[0]["net"]["id"] == 1
    assert due[0]["net_date"] == "2026-09-09"

    # hour >= 8, not == 8 -- still due later the same local day, so a
    # service that was down at 08:00 sharp still catches this occurrence.
    due_late = discord_notify._due_net_wrapups(conn, thu_late)
    assert len(due_late) == 1
    assert due_late[0]["net_date"] == "2026-09-09"

    assert discord_notify._due_net_wrapups(conn, fri) == []


def test_due_net_wrapups_two_nets_different_timezones_fire_independently(conn):
    """Two nets, different weekdays AND different IANA zones -- each is
    due only on its OWN local schedule, never the other's, from the same
    kind of `now` instant."""
    _wr_net(conn, 1, "Wednesday MC", protocol="mc",
            weekday=2, start_hour=18, end_hour=20, timezone="America/Boise")
    _wr_net(conn, 2, "Friday MT", protocol="mt",
            weekday=4, start_hour=19, end_hour=21, timezone="America/Los_Angeles")

    tz_boise = ZoneInfo("America/Boise")
    thu_8am_boise = int(datetime(2026, 9, 10, 8, 0, tzinfo=tz_boise).timestamp())
    due = discord_notify._due_net_wrapups(conn, thu_8am_boise)
    assert {d["net"]["id"] for d in due} == {1}

    tz_la = ZoneInfo("America/Los_Angeles")
    sat_8am_la = int(datetime(2026, 9, 12, 8, 0, tzinfo=tz_la).timestamp())
    due2 = discord_notify._due_net_wrapups(conn, sat_8am_la)
    assert {d["net"]["id"] for d in due2} == {2}


def test_net_wrapup_provider_zero_checkins_yields_no_item(conn):
    _enable_discord(conn)
    _wr_net(conn, 1, "Wednesday MC")
    tz = ZoneInfo("America/Boise")
    thu_8am = int(datetime(2026, 9, 10, 8, 0, tzinfo=tz).timestamp())

    assert discord_notify.net_wrapup_provider(conn, thu_8am) == []


def test_net_wrapup_provider_renders_checkins_and_notable_streak(conn):
    _enable_discord(conn)
    _wr_season(conn, 1)
    _wr_player(conn, 1, "l3@n", "RED")
    _wr_player(conn, 2, "Raptor", "GREEN")
    _wr_net(conn, 1, "Wednesday MC")
    _wr_checkin(conn, season_id=1, player_id=1, awarded_at=0, streak=3, net_id=1,
                message_id="m1", net_date="2026-09-09")
    _wr_checkin(conn, season_id=1, player_id=2, awarded_at=0, streak=1, net_id=1,
                message_id="m2", net_date="2026-09-09")

    tz = ZoneInfo("America/Boise")
    thu_8am = int(datetime(2026, 9, 10, 8, 0, tzinfo=tz).timestamp())
    items = discord_notify.net_wrapup_provider(conn, thu_8am)

    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "net_wrapup:1"
    assert item["key"] == "1:2026-09-09"
    assert item["payload"]["embeds"][0]["title"] == "Wednesday MC — 2026-09-09"
    field = item["payload"]["embeds"][0]["fields"][0]
    assert field["name"] == "Check-ins"
    assert "**2** checked in" in field["value"]
    assert "l3@n" in field["value"] and "**3**-net streak" in field["value"]
    assert "Raptor" in field["value"]
    assert "-net streak" not in field["value"].split("Raptor")[1].split("\n")[0]  # streak of 1 is not "notable"


def test_net_wrapup_new_net_added_at_runtime_gets_wrapups_with_no_code_change(conn):
    """A checkin_net row added after this process started gets wrap-ups
    on its very next occurrence -- net_wrapup_provider() reads the table
    fresh on every call, so nothing about a new net is hardcoded
    anywhere."""
    _enable_discord(conn)
    _wr_season(conn, 1)
    _wr_player(conn, 1, "NewPlayer", "RED")
    tz = ZoneInfo("America/Boise")
    thu_8am = int(datetime(2026, 9, 10, 8, 0, tzinfo=tz).timestamp())

    assert discord_notify.net_wrapup_provider(conn, thu_8am) == []

    _wr_net(conn, 5, "New Net")
    _wr_checkin(conn, season_id=1, player_id=1, awarded_at=0, net_id=5,
                message_id="m1", net_date="2026-09-09")

    items = discord_notify.net_wrapup_provider(conn, thu_8am)
    assert len(items) == 1
    assert items[0]["kind"] == "net_wrapup:5"


def test_net_wrapup_disabling_a_net_stops_its_wrapups(conn):
    _enable_discord(conn)
    _wr_season(conn, 1)
    _wr_player(conn, 1, "P", "RED")
    _wr_net(conn, 1, "Wednesday MC")
    _wr_checkin(conn, season_id=1, player_id=1, awarded_at=0, net_id=1,
                message_id="m1", net_date="2026-09-09")
    tz = ZoneInfo("America/Boise")
    thu_8am = int(datetime(2026, 9, 10, 8, 0, tzinfo=tz).timestamp())

    assert len(discord_notify.net_wrapup_provider(conn, thu_8am)) == 1

    conn.execute("UPDATE checkin_net SET enabled = 0 WHERE id = 1")
    assert discord_notify.net_wrapup_provider(conn, thu_8am) == []


def test_check_due_time_driven_net_wrapup_fires_once_per_occurrence_then_again_next_time(conn, monkeypatch):
    """Called twice for the same net occurrence, this enqueues exactly
    one row (discord_outbox's own UNIQUE(kind, key), via enqueue()'s
    INSERT OR IGNORE); called again for the NEXT week's occurrence, a
    second, distinct row."""
    monkeypatch.setattr(discord_notify, "TIME_DRIVEN_PROVIDERS", [discord_notify.net_wrapup_provider])
    _enable_discord(conn)
    _wr_season(conn, 1)
    _wr_player(conn, 1, "P", "RED")
    _wr_net(conn, 1, "Wednesday MC")
    _wr_checkin(conn, season_id=1, player_id=1, awarded_at=0, net_id=1,
                message_id="m1", net_date="2026-09-09")
    tz = ZoneInfo("America/Boise")
    thu1_8am = int(datetime(2026, 9, 10, 8, 0, tzinfo=tz).timestamp())

    first = discord_notify.check_due_time_driven(conn, thu1_8am)
    second = discord_notify.check_due_time_driven(conn, thu1_8am)
    assert first == 1
    assert second == 0
    rows = conn.execute("SELECT key FROM discord_outbox WHERE kind = 'net_wrapup:1'").fetchall()
    assert len(rows) == 1
    assert rows[0]["key"] == "1:2026-09-09"

    # The following week's occurrence -- a different net_date, so a
    # different key, on the SAME net.
    _wr_checkin(conn, season_id=1, player_id=1, awarded_at=0, net_id=1,
                message_id="m2", net_date="2026-09-16")
    thu2_8am = int(datetime(2026, 9, 17, 8, 0, tzinfo=tz).timestamp())
    third = discord_notify.check_due_time_driven(conn, thu2_8am)
    assert third == 1
    rows = conn.execute("SELECT key FROM discord_outbox WHERE kind = 'net_wrapup:1'").fetchall()
    assert len(rows) == 2


def test_enqueue_is_noop_when_announce_net_wrapup_off(conn):
    _enable_discord(conn)
    conn.execute("UPDATE discord_config SET announce_net_wrapup = 0 WHERE id = 1")
    inserted = discord_notify.enqueue(
        conn, kind="net_wrapup:1", key="1:2026-09-09", payload={}, now=int(time.time()),
    )
    assert inserted is False


def test_announce_net_wrapup_off_does_not_suppress_other_kinds(conn):
    """The toggle gates only net_wrapup -- an unrelated kind (here,
    weekly_recap) keeps working while it is off."""
    _enable_discord(conn)
    conn.execute("UPDATE discord_config SET announce_net_wrapup = 0 WHERE id = 1")
    now = int(time.time())
    assert discord_notify.enqueue(conn, kind="weekly_recap", key="2026-W37:mc", payload={}, now=now) is True
    assert discord_notify.enqueue(conn, kind="net_wrapup:1", key="1:2026-09-09", payload={}, now=now) is False


def test_announce_net_wrapup_gates_by_generic_prefix_not_the_scoped_kind(conn):
    """The toggle is ONE switch for every net -- checked against the
    generic "net_wrapup" prefix, not the per-instance
    "net_wrapup:<id>" kind -- so it is off for every net at once, not
    per net (an operator wanting only some nets silenced routes those to
    a disabled discord_channel row instead -- see that table's own
    comment in app/db.py)."""
    _enable_discord(conn)
    conn.execute("UPDATE discord_config SET announce_net_wrapup = 0 WHERE id = 1")
    now = int(time.time())
    assert discord_notify.enqueue(conn, kind="net_wrapup:1", key="1:x", payload={}, now=now) is False
    assert discord_notify.enqueue(conn, kind="net_wrapup:2", key="2:x", payload={}, now=now) is False
