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
    its detail at all any more -- just "TEAM **<number>**" -- so the
    old per-line 169/169 stutter this test used to guard against cannot
    recur structurally. What remains to guard: the award's `detail`
    still surfaces exactly once, as the field's own trailing italic
    unit line (_join_team_field()), and the per-team line itself stays
    just the bold number with no "--" and no restated detail text."""
    result = _sample_result()
    result["awards"] = [{
        "award": "quick_fingers", "label": "Quick Fingers", "scope": "TEAM0",
        "player_id": None, "player": None, "team": "TEAM0",
        "value": 169.0, "detail": "169 s after the net opened",
    }]
    embed = discord_notify.build_month_honors_embed(conn, "2026-08", "mc", result)
    by_team = next(e for e in embed["embeds"] if e["title"] == "By team")
    value = by_team["fields"][0]["value"]
    assert value == "TEAM0 **169**\n\n*169 s after the net opened*"
    assert "169 169" not in value
    assert "--" not in value


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
