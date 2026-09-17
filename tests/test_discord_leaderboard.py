"""Tests for app/discord_leaderboard.py -- the pinned, self-editing
Discord leaderboard: discord_pinned_message state, change detection
(hash excludes the "as of" line), the five-case update pass
(run_leaderboard_pass()), and its own interval gate
(maybe_run_leaderboard()).

Every outbound call -- webhook POST/PATCH AND bot pin/PUT/DELETE -- goes
through ONE shared httpx.MockTransport handler per test, since
run_leaderboard_pass() threads a single `http_client` through both
app/discord_leaderboard.py's own _webhook_request() and
app/discord_bot.py's pin_message()/unpin_message() -- same pattern
tests/test_discord_notify.py and tests/test_discord_bot.py already use
for their own outbound calls. No real network access anywhere in this
file.

Needs a real file-backed database, not the in-memory `conn` fixture
(tests/conftest.py): app/mc_api.py's top_for()/top_checkin_for()/
top_explorer_for() are self-contained *_for() helpers that always open
their OWN connection via app.db.connect() (settings.db_path), never
accepting a caller's connection -- see those functions' own docstrings
in app/mc_api.py, and app/discord_leaderboard.py's own module docstring
for why they are reused verbatim rather than re-implemented against a
passed-in connection. Same db_path-fixture shape
tests/test_discord_notify.py's own drain-loop tests, and
tests/test_discord_bot.py's own ensure_team_roles() tests, already use.
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import time

import httpx
import pytest

import app.db as db
from app import discord_bot, discord_leaderboard, discord_notify, mc_api
from app.config import settings
from app.db import MIGRATIONS, SCHEMA

_BOT_TOKEN = "test-bot-token-never-leak-me"
_TEST_WEBHOOK = "https://discord.test/api/webhooks/1/original-token"
_TEST_WEBHOOK_2 = "https://discord.test/api/webhooks/2/moved-token"


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
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """A fresh temp file-backed database with the leaderboard turned on
    against a default webhook -- the precondition every
    run_leaderboard_pass() test below needs. `enabled` (the announce_*
    outbox gate) is deliberately left at its bare default (0) --
    leaderboard_enabled is its OWN, independent gate (see
    discord_config's own comment in app/db.py), not a sub-toggle of it.
    """
    path = str(tmp_path / "game.db")
    _init_schema(path)
    monkeypatch.setattr(db.settings, "db_path", path)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(
        "UPDATE discord_config SET leaderboard_enabled = 1, "
        " leaderboard_interval_seconds = 600, leaderboard_top_n = 5, "
        " webhook_url = ? WHERE id = 1",
        (_TEST_WEBHOOK,),
    )
    conn.close()
    return path


@pytest.fixture(autouse=True)
def _enable_bot_token(monkeypatch):
    monkeypatch.setattr(settings, "discord_bot_token", _BOT_TOKEN)


@pytest.fixture(autouse=True)
def _reset_leaderboard_gate(monkeypatch):
    """discord_leaderboard._last_leaderboard_run_at is module-level,
    mutated by run_leaderboard_pass() itself -- reset before every test
    so one test's timing can never bleed into the next (same reasoning
    tests/test_discord_bot.py's own _reset_reconcile_gate fixture gives
    for _last_reconcile_gate_at).
    """
    monkeypatch.setattr(discord_leaderboard, "_last_leaderboard_run_at", 0.0)


def _seed_standings(path: str, *, protocol: str = "mc", team: str = "RED") -> int:
    """One active season for `protocol` plus one squares-held row, so
    build_leaderboard() has at least a Standings field to show (an
    entirely empty board is left out of the embeds -- see that
    function's own docstring) -- the minimum every test in this file
    needs. Returns the player id created.
    """
    conn = sqlite3.connect(path)
    now = int(time.time())
    conn.execute(
        "INSERT INTO mc_season(id, protocol, started_at, ends_at, status) "
        "VALUES (1, ?, 0, ?, 'active')",
        (protocol, now + 1_000_000),
    )
    player_id = conn.execute(
        "INSERT INTO player(display_name, team, created_at) VALUES ('Seed', ?, ?)",
        (team, now),
    ).lastrowid
    conn.execute(
        "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts) "
        "VALUES (1, 'c0', ?, ?, ?)",
        (team, player_id, now),
    )
    conn.commit()
    conn.close()
    return player_id


def _seed_top_lists(path: str, *, protocol: str = "mc") -> None:
    """Capture, check-in, and Explorer data for TWO players so
    top_for()/top_checkin_for()/top_explorer_for() each return more than
    one row -- test_figures_match_the_three_site_helpers below needs
    real, orderable data to compare against, not just a non-empty list.
    Assumes _seed_standings() has already created season id 1 and one
    player; this adds a second player and gives BOTH activity.
    """
    conn = sqlite3.connect(path)
    now = int(time.time())
    p2 = conn.execute(
        "INSERT INTO player(display_name, team, created_at) VALUES ('Runner-Up', 'BLUE', ?)", (now,)
    ).lastrowid
    p1 = conn.execute("SELECT player_id FROM player WHERE display_name = 'Seed'").fetchone()[0]
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) VALUES (?, 'aabbccdd', ?, ?)",
        (protocol, p1, now),
    )
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) VALUES (?, 'eeff0011', ?, ?)",
        (protocol, p2, now),
    )
    # Wardrivers (captures) -- p1 gets 3, p2 gets 1.
    for i, pid in enumerate([p1, p1, p1, p2]):
        conn.execute(
            "INSERT INTO mc_tile_capture_log(season_id, cell_id, ts, by_player_id, by_team) "
            "VALUES (1, ?, ?, ?, ?)",
            (f"cap{i}", now + i, pid, "RED" if pid == p1 else "BLUE"),
        )
    # NetOps (check-in points) -- p2 outranks p1.
    conn.execute(
        "INSERT INTO mc_checkin_award(season_id, player_id, net_date, points, protocol, message_id, "
        " awarded_at, streak) VALUES (1, ?, '2026-09-01', 5, ?, 'm1', ?, 1)",
        (p1, protocol, now),
    )
    conn.execute(
        "INSERT INTO mc_checkin_award(season_id, player_id, net_date, points, protocol, message_id, "
        " awarded_at, streak) VALUES (1, ?, '2026-09-01', 12, ?, 'm2', ?, 2)",
        (p2, protocol, now),
    )
    # Explorer (Places Worth Going points) -- p1 outranks p2.
    conn.execute(
        "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at, protocol) "
        "VALUES (1, ?, '2026-09-01', 20, ?, ?)",
        (p1, now, protocol),
    )
    conn.execute(
        "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at, protocol) "
        "VALUES (2, ?, '2026-09-01', 4, ?, ?)",
        (p2, now, protocol),
    )
    conn.commit()
    conn.close()


def _pinned_row(path: str) -> sqlite3.Row | None:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM discord_pinned_message WHERE kind = 'leaderboard'").fetchone()
    conn.close()
    return row


def _set_leaderboard(path: str, **overrides) -> None:
    cols = ", ".join(f"{k} = ?" for k in overrides)
    conn = sqlite3.connect(path)
    conn.execute(f"UPDATE discord_config SET {cols} WHERE id = 1", tuple(overrides.values()))
    conn.commit()
    conn.close()


def _make_handler(*, post_status=200, patch_status=200, pin_status=200, unpin_status=204,
                   post_ids=("1001", "2002")):
    """One handler covering every request app/discord_leaderboard.py's
    pass can make: a webhook POST (?wait=true, create), a webhook PATCH
    (edit), a bot PUT to pin (new v10 route only -- see
    app/discord_bot.py's pin_message() for the legacy fallback this
    module also supports but no test here specifically exercises), and
    a bot DELETE to unpin. `calls` records every request seen, in order,
    for assertions.
    """
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        url = str(request.url)
        if request.method == "POST" and "/webhooks/" in url:
            return httpx.Response(post_status, json={"id": post_ids[0], "channel_id": post_ids[1]})
        if request.method == "PATCH" and "/webhooks/" in url:
            return httpx.Response(patch_status, json={"id": post_ids[0], "channel_id": post_ids[1]})
        if request.method == "PUT" and "/messages/pins/" in url:
            return httpx.Response(pin_status)
        if request.method == "DELETE" and "/messages/pins/" in url:
            return httpx.Response(unpin_status)
        return httpx.Response(404)

    return handler, calls


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _pass(handler, *, force: bool = False) -> dict:
    async def go():
        async with _mock_client(handler) as client:
            return await discord_leaderboard.run_leaderboard_pass(force=force, http_client=client)
    return _run(go())


# ---- 1: first pass posts, stores ids, pins -------------------------------


def test_first_pass_posts_with_wait_true_stores_ids_and_pins(db_path):
    _seed_standings(db_path)
    handler, calls = _make_handler()

    result = _pass(handler)

    assert result == {"ok": True, "reason": "posted", "channel_id": "2002", "message_id": "1001", "pinned": True}
    posts = [c for c in calls if c.method == "POST"]
    assert len(posts) == 1
    assert "wait=true" in str(posts[0].url)
    pins = [c for c in calls if c.method == "PUT"]
    assert len(pins) == 1

    row = _pinned_row(db_path)
    assert row is not None
    assert row["webhook_id"] == "1"
    assert row["channel_id"] == "2002"
    assert row["message_id"] == "1001"
    assert row["pinned"] == 1
    assert row["content_hash"]


# ---- 2: unchanged content -> no PATCH ------------------------------------


def test_unchanged_content_issues_no_patch(db_path):
    _seed_standings(db_path)
    handler, calls = _make_handler()
    _pass(handler)
    calls.clear()

    result = _pass(handler)

    assert result == {"ok": True, "reason": "unchanged"}
    assert calls == []


# ---- 3: changed content -> exactly one PATCH, no new POST ----------------


def test_changed_content_edits_in_place(db_path):
    player_id = _seed_standings(db_path)
    handler, calls = _make_handler()
    _pass(handler)
    calls.clear()

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts) "
        "VALUES (1, 'c1', 'RED', ?, ?)", (player_id, int(time.time())),
    )
    conn.commit()
    conn.close()

    result = _pass(handler)

    assert result["ok"] is True and result["reason"] == "edited"
    posts = [c for c in calls if c.method == "POST"]
    patches = [c for c in calls if c.method == "PATCH"]
    assert posts == []
    assert len(patches) == 1
    assert "/messages/1001" in str(patches[0].url)

    row = _pinned_row(db_path)
    assert row["message_id"] == "1001"  # unchanged -- same message, just edited


# ---- 4: the "as of" timestamp alone never triggers an edit ---------------


def test_as_of_timestamp_excluded_from_hash(db_path):
    _seed_standings(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cfg = discord_notify.load_discord_config(conn)
    base = discord_leaderboard.build_leaderboard(conn, cfg, 5)
    conn.close()
    assert base is not None

    hash_a = discord_leaderboard._content_hash(base)
    hash_b = discord_leaderboard._content_hash(base)
    assert hash_a == hash_b

    payload_a = discord_leaderboard._finalize_payload(base, 1000)
    payload_b = discord_leaderboard._finalize_payload(base, 2000)
    assert payload_a["content"] != payload_b["content"]
    assert payload_a["embeds"] == payload_b["embeds"]
    # The hash is computed over `base` (username + embeds only) -- never
    # over either finalized payload's own "as of" content line.
    assert "content" not in base


# ---- 5: PATCH 404 -> reposts and re-pins ---------------------------------


def test_patch_404_reposts_and_repins(db_path):
    player_id = _seed_standings(db_path)
    handler, calls = _make_handler()
    _pass(handler)
    calls.clear()

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts) "
        "VALUES (1, 'c1', 'RED', ?, ?)", (player_id, int(time.time())),
    )
    conn.commit()
    conn.close()

    handler2, calls2 = _make_handler(patch_status=404, post_ids=("9999", "8888"))
    result = _pass(handler2)

    assert result["ok"] is True and result["reason"] == "posted"
    assert [c for c in calls2 if c.method == "PATCH"]
    assert [c for c in calls2 if c.method == "POST"]
    assert [c for c in calls2 if c.method == "PUT"]

    row = _pinned_row(db_path)
    assert row["message_id"] == "9999"
    assert row["channel_id"] == "8888"
    assert row["pinned"] == 1


# ---- 6: webhook moved -----------------------------------------------------


def test_webhook_moved_unpins_old_posts_and_pins_new_no_delete_of_message(db_path):
    _seed_standings(db_path)
    handler, calls = _make_handler()
    _pass(handler)
    calls.clear()

    _set_leaderboard(db_path, webhook_url=_TEST_WEBHOOK_2)
    handler2, calls2 = _make_handler(post_ids=("5555", "6666"))
    result = _pass(handler2)

    assert result["ok"] is True and result["reason"] == "posted"
    unpins = [c for c in calls2 if c.method == "DELETE"]
    assert len(unpins) == 1
    assert "/channels/2002/messages/pins/1001" in str(unpins[0].url)
    posts = [c for c in calls2 if c.method == "POST"]
    assert len(posts) == 1 and "webhooks/2/" in str(posts[0].url)
    pins = [c for c in calls2 if c.method == "PUT"]
    assert len(pins) == 1

    # No message ever gets DELETEd -- only the pins sub-resource does.
    for c in calls2:
        if c.method == "DELETE":
            assert "/pins/" in str(c.url)

    row = _pinned_row(db_path)
    assert row["webhook_id"] == "2"
    assert row["channel_id"] == "6666"
    assert row["message_id"] == "5555"


# ---- 7: pin failure never fails the pass ---------------------------------


def test_pin_failure_leaves_message_updating_but_not_pinned(db_path):
    _seed_standings(db_path)
    handler, calls = _make_handler(pin_status=403)

    result = _pass(handler)

    assert result["ok"] is True and result["reason"] == "posted"
    assert result["pinned"] is False
    row = _pinned_row(db_path)
    assert row is not None
    assert row["message_id"] == "1001"
    assert row["pinned"] == 0


# ---- 8: interval gate ------------------------------------------------------


def test_interval_gate_two_calls_run_one_pass_force_bypasses_it(db_path):
    _seed_standings(db_path)
    handler, calls = _make_handler()

    async def maybe():
        async with _mock_client(handler) as client:
            return await discord_leaderboard.maybe_run_leaderboard(http_client=client)

    first = _run(maybe())
    assert first is not None and first["ok"] is True
    calls_after_first = len(calls)
    assert calls_after_first > 0

    second = _run(maybe())
    assert second is None
    assert len(calls) == calls_after_first  # nothing new happened

    forced = _pass(handler, force=True)
    assert forced["ok"] is True
    assert len(calls) > calls_after_first  # force bypassed the gate


# ---- 9: disabled or no webhook -> nothing --------------------------------


def test_disabled_does_nothing(db_path):
    _seed_standings(db_path)
    _set_leaderboard(db_path, leaderboard_enabled=0)
    handler, calls = _make_handler()

    result = _pass(handler)

    assert result == {"ok": False, "reason": "leaderboard disabled"}
    assert calls == []
    assert _pinned_row(db_path) is None


def test_no_webhook_routed_does_nothing(db_path):
    _seed_standings(db_path)
    _set_leaderboard(db_path, webhook_url="")
    handler, calls = _make_handler()

    result = _pass(handler)

    assert result["ok"] is False
    assert "no webhook" in result["reason"]
    assert calls == []
    assert _pinned_row(db_path) is None


# ---- 10: figures match the three site helpers exactly --------------------


def test_figures_match_the_three_site_helpers(db_path):
    _seed_standings(db_path)
    _seed_top_lists(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cfg = discord_notify.load_discord_config(conn)
    base = discord_leaderboard.build_leaderboard(conn, cfg, 5)
    conn.close()
    assert base is not None
    embed = base["embeds"][0]
    fields = {f["name"]: f["value"] for f in embed["fields"]}

    expected_wardrivers = mc_api.top_for("mc")
    expected_netops = mc_api.top_checkin_for("mc")
    expected_explorer = mc_api.top_explorer_for("mc")
    assert expected_wardrivers and expected_netops and expected_explorer

    for row in expected_wardrivers[:5]:
        assert row["display_name"] in fields["Wardrivers"]
        assert discord_notify._fmt_number(row["captures"]) in fields["Wardrivers"]
    for row in expected_netops[:5]:
        assert row["display_name"] in fields["NetOps"]
        assert discord_notify._fmt_number(row["points"]) in fields["NetOps"]
    for row in expected_explorer[:5]:
        assert row["display_name"] in fields["Explorer"]
        assert discord_notify._fmt_number(row["points"]) in fields["Explorer"]

    # Rank order: the best Wardriver line appears before the second's.
    lines = fields["Wardrivers"].splitlines()
    first_name_line = next(i for i, l in enumerate(lines) if expected_wardrivers[0]["display_name"] in l)
    second_name_line = next(i for i, l in enumerate(lines) if expected_wardrivers[1]["display_name"] in l)
    assert first_name_line < second_name_line


# ---- 11: no location/cell/radio/node data; allowed_mentions.parse == [] --


def test_no_location_fields_and_allowed_mentions_locked_down(db_path):
    _seed_standings(db_path)
    _seed_top_lists(db_path)
    handler, calls = _make_handler()

    _pass(handler)

    posts = [c for c in calls if c.method == "POST" and "/webhooks/" in str(c.url)]
    assert len(posts) == 1
    body = posts[0].content.decode("utf-8")
    for forbidden in ("cell_id", "lat_idx", "lon_idx", "aabbccdd", "eeff0011", "node_ref", "\"place\"", "place_id"):
        assert forbidden not in body

    import json as _json
    payload = _json.loads(body)
    assert payload["allowed_mentions"] == {"parse": []}


# ---- 12: stored row never contains a webhook token -----------------------


def test_stored_row_never_contains_a_webhook_token(db_path):
    _seed_standings(db_path)
    handler, calls = _make_handler()

    _pass(handler)

    row = _pinned_row(db_path)
    assert re.fullmatch(r"\d+", row["webhook_id"])
    for value in dict(row).values():
        assert "original-token" not in str(value)


# ---- 13: webhook calls never carry the bot token; pin/unpin always do ---


def test_webhook_calls_never_use_bot_token_pin_unpin_always_do(db_path):
    _seed_standings(db_path)
    handler, calls = _make_handler()
    _pass(handler)  # first pass: POST + PUT

    _set_leaderboard(db_path, webhook_url=_TEST_WEBHOOK_2)
    calls.clear()
    handler2, calls2 = _make_handler(post_ids=("7777", "8888"))
    _pass(handler2)  # second pass: DELETE (unpin) + POST + PUT

    webhook_calls = [c for c in calls2 if "/webhooks/" in str(c.url)]
    assert webhook_calls  # the POST to the new webhook
    for c in webhook_calls:
        assert "Authorization" not in c.headers

    pin_or_unpin_calls = [c for c in calls2 if "/messages/pins/" in str(c.url)]
    assert pin_or_unpin_calls  # the unpin DELETE and the pin PUT
    for c in pin_or_unpin_calls:
        assert c.headers.get("Authorization") == f"Bot {_BOT_TOKEN}"
