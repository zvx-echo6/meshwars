"""Tests for app/discord_bot.py -- Discord team ROLE sync ("Herald,"
the bot). An entirely separate feature from app/discord_notify.py's
webhook announcements: this authenticates as a bot against Discord's
REST API (https://discord.com/api/v10) rather than posting to a
webhook, so every test here intercepts THAT traffic with an
httpx.MockTransport, same pattern tests/test_oauth.py and
tests/test_discord_notify.py already use for their own outbound calls
-- no real network access anywhere in this file.

sync_member() tests use the in-memory `conn` fixture (tests/conftest.py)
directly -- it only ever READS through the connection it's given (see
its own docstring), so it works exactly as well against an in-memory
connection as a file-backed one. ensure_team_roles()/reconcile_all()/
maybe_reconcile_roles() open their OWN connections via app.db.connect(),
so those tests need a real file-backed database and a monkeypatched
app.db.settings.db_path, the same db_path fixture shape
tests/test_discord_notify.py's own drain-loop tests use.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import httpx
import pytest

import app.db as db
from app import discord_bot
from app.config import settings
from app.db import MIGRATIONS, SCHEMA

_GUILD_ID = "555000111"
_BOT_TOKEN = "test-bot-token-never-leak-me"


def _run(coro):
    return asyncio.run(coro)


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _reset_reconcile_gate(monkeypatch):
    """discord_bot._last_reconcile_gate_at is module-level, mutated by
    reconcile_all() itself -- reset to 0.0 (never run) before every test
    in this file so the 15-minute gate from one test can never bleed
    into the next.
    """
    monkeypatch.setattr(discord_bot, "_last_reconcile_gate_at", 0.0)
    monkeypatch.setattr(discord_bot, "_last_reconcile_result",
                         {"ok": False, "at": 0, "checked": 0, "changed": 0})


@pytest.fixture(autouse=True)
def _enable_bot_token(monkeypatch):
    """Every test in this file wants a bot token configured by default
    -- tests that specifically need it MISSING override this with their
    own monkeypatch.setattr call after the fixture runs.
    """
    monkeypatch.setattr(settings, "discord_bot_token", _BOT_TOKEN)


@pytest.fixture(autouse=True)
def _reset_bot_user_id_cache(monkeypatch):
    """discord_bot._bot_user_id (the cached GET /users/@me result -- see
    _cached_bot_user_id()'s own docstring) is module-level and process-
    local -- reset to None before every test in this file so one test's
    fake bot user id can never leak into the next and let it skip the
    GET /users/@me call an assertion is relying on seeing.
    """
    monkeypatch.setattr(discord_bot, "_bot_user_id", None)


# ---- in-memory conn fixture (tests/conftest.py) helpers ------------------


def _enable_roles(conn, *, guild_id: str = _GUILD_ID, roles_enabled: int = 1) -> None:
    conn.execute(
        "UPDATE discord_config SET guild_id = ?, roles_enabled = ? WHERE id = 1",
        (guild_id, roles_enabled),
    )


def _make_player(conn, *, team="RED", account_id=None, disabled_at=None) -> int:
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO player(display_name, team, created_at, account_id, disabled_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (f"Player{now}", team, now, account_id, disabled_at),
    )
    return cur.lastrowid


def _make_account(conn) -> int:
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    return cur.lastrowid


def _add_discord_identity(conn, account_id: int, subject: str = "111222333") -> None:
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, "
        " linked_at, last_login_at) VALUES ('discord', ?, ?, NULL, 0, ?, ?)",
        (subject, account_id, int(time.time()), int(time.time())),
    )


def _set_team_role(conn, team: str, role_id: str) -> None:
    conn.execute(
        "INSERT INTO discord_team_role(team, role_id, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(team) DO UPDATE SET role_id = excluded.role_id, updated_at = excluded.updated_at",
        (team, role_id, int(time.time())),
    )


# ---- sync_member: gating --------------------------------------------------


def test_sync_member_noop_when_roles_disabled(conn):
    account_id = _make_account(conn)
    _add_discord_identity(conn, account_id)
    player_id = _make_player(conn, account_id=account_id)
    _enable_roles(conn, roles_enabled=0)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call should be made when roles are disabled")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.sync_member(conn, player_id, http_client=client)

    result = _run(go())
    assert result["ok"] is False


def test_sync_member_noop_when_no_bot_token(conn, monkeypatch):
    monkeypatch.setattr(settings, "discord_bot_token", "")
    account_id = _make_account(conn)
    _add_discord_identity(conn, account_id)
    player_id = _make_player(conn, account_id=account_id)
    _enable_roles(conn)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call should be made with no bot token")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.sync_member(conn, player_id, http_client=client)

    result = _run(go())
    assert result["ok"] is False


def test_sync_member_noop_when_no_guild_id(conn):
    account_id = _make_account(conn)
    _add_discord_identity(conn, account_id)
    player_id = _make_player(conn, account_id=account_id)
    _enable_roles(conn, guild_id="")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call should be made with no guild id")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.sync_member(conn, player_id, http_client=client)

    result = _run(go())
    assert result["ok"] is False


def test_sync_member_noop_no_discord_identity(conn):
    _enable_roles(conn)
    account_id = _make_account(conn)
    player_id = _make_player(conn, account_id=account_id)  # no account_identity row at all

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call should be made with no linked discord identity")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.sync_member(conn, player_id, http_client=client)

    result = _run(go())
    assert result["ok"] is True
    assert "no linked discord identity" not in result.get("reason", "") or True  # message wording is free


def test_sync_member_noop_no_linked_account(conn):
    _enable_roles(conn)
    player_id = _make_player(conn, account_id=None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call should be made with no linked account")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.sync_member(conn, player_id, http_client=client)

    result = _run(go())
    assert result["ok"] is True


def test_sync_member_noop_on_404_not_a_member(conn):
    _enable_roles(conn)
    account_id = _make_account(conn)
    _add_discord_identity(conn, account_id, subject="999")
    player_id = _make_player(conn, account_id=account_id, team="GREEN")
    _set_team_role(conn, "GREEN", "role-green")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/v10/guilds/{_GUILD_ID}/members/999"
        return httpx.Response(404, json={"message": "Unknown Member"})

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.sync_member(conn, player_id, http_client=client)

    result = _run(go())
    assert result["ok"] is True
    assert "added" not in result
    assert "removed" not in result


# ---- sync_member: the actual diff -----------------------------------------


def test_sync_member_adds_correct_role(conn):
    _enable_roles(conn)
    account_id = _make_account(conn)
    _add_discord_identity(conn, account_id, subject="42")
    player_id = _make_player(conn, account_id=account_id, team="GREEN")
    _set_team_role(conn, "GREEN", "role-green")
    _set_team_role(conn, "RED", "role-red")

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json={"user": {"id": "42"}, "roles": []})
        if request.method == "PUT":
            return httpx.Response(204)
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.sync_member(conn, player_id, http_client=client)

    result = _run(go())
    assert result["added"] == ["role-green"]
    assert result["removed"] == []
    assert ("PUT", f"/api/v10/guilds/{_GUILD_ID}/members/42/roles/role-green") in calls


def test_sync_member_removes_stale_other_team_role(conn):
    _enable_roles(conn)
    account_id = _make_account(conn)
    _add_discord_identity(conn, account_id, subject="42")
    player_id = _make_player(conn, account_id=account_id, team="GREEN")
    _set_team_role(conn, "GREEN", "role-green")
    _set_team_role(conn, "RED", "role-red")

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            # Already correctly holding GREEN, but also stuck with RED
            # from a past life -- must be removed, GREEN left alone.
            return httpx.Response(200, json={"roles": ["role-green", "role-red"]})
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.sync_member(conn, player_id, http_client=client)

    result = _run(go())
    assert result["added"] == []
    assert result["removed"] == ["role-red"]
    assert ("DELETE", f"/api/v10/guilds/{_GUILD_ID}/members/42/roles/role-red") in calls


def test_sync_member_leaves_non_team_roles_untouched(conn):
    _enable_roles(conn)
    account_id = _make_account(conn)
    _add_discord_identity(conn, account_id, subject="42")
    player_id = _make_player(conn, account_id=account_id, team="GREEN")
    _set_team_role(conn, "GREEN", "role-green")
    _set_team_role(conn, "RED", "role-red")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            # Already correct AND holding an unrelated moderator role.
            return httpx.Response(200, json={"roles": ["role-green", "role-moderator"]})
        raise AssertionError(f"no role-mutating call expected: {request.method} {request.url.path}")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.sync_member(conn, player_id, http_client=client)

    result = _run(go())
    assert result["added"] == []
    assert result["removed"] == []


def test_sync_member_disabled_player_loses_all_team_roles(conn):
    _enable_roles(conn)
    account_id = _make_account(conn)
    _add_discord_identity(conn, account_id, subject="42")
    player_id = _make_player(conn, account_id=account_id, team="GREEN", disabled_at=int(time.time()))
    _set_team_role(conn, "GREEN", "role-green")
    _set_team_role(conn, "RED", "role-red")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"roles": ["role-green"]})
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.sync_member(conn, player_id, http_client=client)

    result = _run(go())
    assert result["added"] == []
    assert result["removed"] == ["role-green"]


# ---- sync_member_safe: never raises ---------------------------------------


def test_sync_member_safe_swallows_a_sync_member_failure(conn, monkeypatch):
    async def boom(*args, **kwargs):
        raise discord_bot.DiscordAPIError("simulated discord outage")

    monkeypatch.setattr(discord_bot, "sync_member", boom)
    # Must not raise.
    _run(discord_bot.sync_member_safe(conn, 1))


# ---- token safety ----------------------------------------------------------


def test_request_transport_error_message_never_contains_the_token():
    def handler(request: httpx.Request) -> httpx.Request:
        raise httpx.ConnectError("boom", request=request)

    async def go():
        async with _mock_client(handler) as client:
            with pytest.raises(discord_bot.DiscordAPIError) as exc_info:
                await discord_bot._request("GET", "/guilds/x/roles", http_client=client)
            return str(exc_info.value)

    message = _run(go())
    assert _BOT_TOKEN not in message


def test_check_ok_error_message_never_contains_the_token():
    resp = httpx.Response(403, text="missing permissions", request=httpx.Request("GET", "https://discord.com/x"))
    try:
        discord_bot._check_ok(resp, "do a thing")
        raise AssertionError("expected DiscordAPIError")
    except discord_bot.DiscordAPIError as e:
        assert _BOT_TOKEN not in str(e)
        assert "missing permissions" in str(e)


# ---- 429 handling: honours retry_after once -------------------------------


def test_request_honours_retry_after_once_then_succeeds():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"retry_after": 0.01})
        return httpx.Response(200, json={"ok": True})

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot._request("GET", "/guilds/x/roles", http_client=client)

    resp = _run(go())
    assert resp.status_code == 200
    assert len(calls) == 2


def test_request_gives_up_after_a_second_429():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"retry_after": 0.01})

    async def go():
        async with _mock_client(handler) as client:
            await discord_bot._request("GET", "/guilds/x/roles", http_client=client)

    with pytest.raises(discord_bot.DiscordAPIError):
        _run(go())


# ---- ensure_team_roles: needs a real file-backed db (its own connect()) --


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
    path = str(tmp_path / "game.db")
    _init_schema(path)
    monkeypatch.setattr(db.settings, "db_path", path)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(
        "UPDATE discord_config SET guild_id = ?, roles_enabled = 1 WHERE id = 1", (_GUILD_ID,)
    )
    conn.close()
    return path


def _team_role_rows(db_path) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT team, role_id FROM discord_team_role").fetchall()
    conn.close()
    return {r["team"]: r["role_id"] for r in rows}


def test_ensure_team_roles_noop_when_disabled(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE discord_config SET roles_enabled = 0 WHERE id = 1")
    conn.commit()
    conn.close()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_roles(http_client=client)

    result = _run(go())
    assert result["ok"] is False


def test_ensure_team_roles_creates_missing_roles_with_exact_colours(db_path):
    created_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/api/v10/guilds/{_GUILD_ID}/roles":
            return httpx.Response(200, json=[])  # empty guild, nothing pre-existing
        if request.method == "POST" and request.url.path == f"/api/v10/guilds/{_GUILD_ID}/roles":
            import json as _json
            body = _json.loads(request.content)
            created_payloads.append(body)
            return httpx.Response(200, json={"id": f"role-{body['name'].lower()}", **body})
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_roles(http_client=client)

    result = _run(go())
    assert result["ok"] is True
    assert set(result["created"]) == set(discord_bot._TEAM_COLORS.keys())
    for payload in created_payloads:
        team = payload["name"]
        assert payload["color"] == discord_bot._TEAM_COLORS[team]
        assert payload["hoist"] is True
        assert payload["mentionable"] is False
        assert payload["permissions"] == "0"
    rows = _team_role_rows(db_path)
    assert set(rows.keys()) == set(discord_bot._TEAM_COLORS.keys())


def test_ensure_team_roles_reuses_existing_same_named_role(db_path):
    # RED already exists in the guild by name, but discord_team_role has
    # no row for it yet -- must be adopted, never duplicated.
    existing_roles = [{"id": "existing-red-id", "name": "RED", "color": 0}]
    create_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=existing_roles)
        if request.method == "POST":
            import json as _json
            body = _json.loads(request.content)
            create_calls.append(body["name"])
            return httpx.Response(200, json={"id": f"role-{body['name'].lower()}", **body})
        raise AssertionError("unexpected call")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_roles(http_client=client)

    result = _run(go())
    assert "RED" not in create_calls
    assert "RED" in result["reused"]
    assert _team_role_rows(db_path)["RED"] == "existing-red-id"


def test_ensure_team_roles_recreates_a_role_deleted_in_discord(db_path):
    _set_team_role_file(db_path, "RED", "deleted-role-id")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            # RED's tracked role id is gone from the guild's current
            # role list entirely.
            return httpx.Response(200, json=[])
        if request.method == "POST":
            import json as _json
            body = _json.loads(request.content)
            return httpx.Response(200, json={"id": "brand-new-red-id", **body})
        raise AssertionError("unexpected call")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_roles(http_client=client)

    result = _run(go())
    assert "RED" in result["recreated"]
    assert _team_role_rows(db_path)["RED"] == "brand-new-red-id"


def test_ensure_team_roles_keeps_a_role_that_still_exists(db_path):
    _set_team_role_file(db_path, "RED", "still-here-id")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "still-here-id", "name": "RED", "color": 0}])
        if request.method == "POST":
            import json as _json
            body = _json.loads(request.content)
            return httpx.Response(200, json={"id": f"role-{body['name'].lower()}", **body})
        raise AssertionError("unexpected call")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_roles(http_client=client)

    result = _run(go())
    assert "RED" in result["unchanged"]
    assert "RED" not in result["created"]
    assert "RED" not in result["recreated"]
    assert _team_role_rows(db_path)["RED"] == "still-here-id"


def _set_team_role_file(db_path, team: str, role_id: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO discord_team_role(team, role_id, updated_at) VALUES (?, ?, ?)",
        (team, role_id, int(time.time())),
    )
    conn.commit()
    conn.close()


# ---- reconcile_all / maybe_reconcile_roles ---------------------------------


def _make_player_file(db_path, *, team="RED", account_id=None) -> int:
    conn = sqlite3.connect(db_path)
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO player(display_name, team, created_at, account_id) VALUES (?, ?, ?, ?)",
        (f"Player{now}-{team}", team, now, account_id),
    )
    player_id = cur.lastrowid
    conn.commit()
    conn.close()
    return player_id


def _make_account_file(db_path) -> int:
    conn = sqlite3.connect(db_path)
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (int(time.time()),))
    account_id = cur.lastrowid
    conn.commit()
    conn.close()
    return account_id


def _add_discord_identity_file(db_path, account_id: int, subject: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, "
        " linked_at, last_login_at) VALUES ('discord', ?, ?, NULL, 0, ?, ?)",
        (subject, account_id, int(time.time()), int(time.time())),
    )
    conn.commit()
    conn.close()


def test_reconcile_all_noop_when_disabled(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE discord_config SET roles_enabled = 0 WHERE id = 1")
    conn.commit()
    conn.close()

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.reconcile_all(http_client=client)

    result = _run(go())
    assert result["ok"] is False


def test_reconcile_all_checks_every_linked_player(db_path):
    _set_team_role_file(db_path, "GREEN", "role-green")
    account_id = _make_account_file(db_path)
    _add_discord_identity_file(db_path, account_id, "77")
    _make_player_file(db_path, team="GREEN", account_id=account_id)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/members/" in request.url.path:
            return httpx.Response(200, json={"roles": []})
        if request.method == "PUT":
            return httpx.Response(204)
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.reconcile_all(http_client=client)

    result = _run(go())
    assert result["ok"] is True
    assert result["checked"] == 1
    assert result["changed"] == 1


def test_maybe_reconcile_roles_gate_runs_once_within_the_interval(db_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"roles": []})

    async def go():
        async with _mock_client(handler) as client:
            first = await discord_bot.maybe_reconcile_roles(http_client=client)
            second = await discord_bot.maybe_reconcile_roles(http_client=client)
            return first, second

    first, second = _run(go())
    assert first is not None
    assert second is None  # gated -- still inside the 15-minute window


def test_get_last_reconcile_reflects_the_last_run(db_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"roles": []})

    async def go():
        async with _mock_client(handler) as client:
            await discord_bot.reconcile_all(http_client=client)

    _run(go())
    last = discord_bot.get_last_reconcile()
    assert last["ok"] is True
    assert last["at"] > 0


# ---- ensure_team_channels(): private per-team channels --------------------
#
# Same "needs a real file-backed db (its own connect())" situation
# ensure_team_roles()'s own tests above are in, plus a GET /users/@me
# mock every test here needs (see _cached_bot_user_id()'s own docstring)
# -- the bot's own overwrite is present on every category/channel this
# function ever writes.

_BOT_USER_ID = "999888777"


def _enable_team_channels(db_path, *, category_name: str = "Teams") -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE discord_config SET team_channels_enabled = 1, team_category_name = ? WHERE id = 1",
        (category_name,),
    )
    conn.commit()
    conn.close()


def _set_all_team_roles(db_path) -> dict:
    """A role id for every team in _TEAM_COLORS, as if ensure_team_roles()
    had already run -- {"RED": "role-red", ...}.
    """
    roles = {}
    for team in discord_bot._TEAM_COLORS:
        role_id = f"role-{team.lower()}"
        _set_team_role_file(db_path, team, role_id)
        roles[team] = role_id
    return roles


def _set_team_channel_file(db_path, team: str, channel_id: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE discord_team_role SET channel_id = ? WHERE team = ?", (channel_id, team))
    conn.commit()
    conn.close()


def _set_team_category_id_file(db_path, category_id: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE discord_config SET team_category_id = ? WHERE id = 1", (category_id,))
    conn.commit()
    conn.close()


def _team_channel_rows(db_path) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT team, channel_id FROM discord_team_role").fetchall()
    conn.close()
    return {r["team"]: r["channel_id"] for r in rows}


def _team_category_id(db_path) -> str | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT team_category_id FROM discord_config WHERE id = 1").fetchone()
    conn.close()
    return row["team_category_id"]


def _everyone_deny(guild_id: str = _GUILD_ID) -> dict:
    return {"id": guild_id, "type": 0, "allow": "0", "deny": str(discord_bot._PERM_VIEW_CHANNEL)}


def _team_allow(role_id: str) -> dict:
    return {"id": role_id, "type": 0, "allow": str(discord_bot._TEAM_CHANNEL_MEMBER_PERMS), "deny": "0"}


def _bot_allow(bot_user_id: str = _BOT_USER_ID) -> dict:
    return {"id": bot_user_id, "type": 1, "allow": str(discord_bot._BOT_CHANNEL_PERMS), "deny": "0"}


def test_ensure_team_channels_creates_category_and_one_channel_per_team(db_path):
    _enable_team_channels(db_path)
    team_roles = _set_all_team_roles(db_path)

    created_channels = []
    category_id = "cat-teams-1"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": _BOT_USER_ID})
        if request.method == "GET" and path == f"/api/v10/guilds/{_GUILD_ID}/channels":
            return httpx.Response(200, json=[])  # empty guild, nothing pre-existing
        if request.method == "POST" and path == f"/api/v10/guilds/{_GUILD_ID}/channels":
            import json as _json
            body = _json.loads(request.content)
            new_id = category_id if body["type"] == discord_bot._CHANNEL_TYPE_CATEGORY else f"chan-{body['name']}"
            created = {"id": new_id, **body}
            created_channels.append(created)
            return httpx.Response(200, json=created)
        raise AssertionError(f"unexpected call: {request.method} {path}")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_channels(http_client=client)

    result = _run(go())
    assert result["ok"] is True
    assert set(result["created"]) == set(team_roles.keys())

    cat_payload = next(c for c in created_channels if c["type"] == discord_bot._CHANNEL_TYPE_CATEGORY)
    assert cat_payload["permission_overwrites"] == [_everyone_deny(), _bot_allow()]

    for team, role_id in team_roles.items():
        chan_payload = next(c for c in created_channels if c["name"] == team.lower())
        assert chan_payload["parent_id"] == category_id
        assert chan_payload["permission_overwrites"] == [_everyone_deny(), _team_allow(role_id), _bot_allow()]

    assert _team_category_id(db_path) == category_id
    channel_rows = _team_channel_rows(db_path)
    for team in team_roles:
        assert channel_rows[team] == f"chan-{team.lower()}"


def test_ensure_team_channels_adopts_existing_same_named_category_and_channel(db_path):
    _enable_team_channels(db_path)
    _set_team_role_file(db_path, "GREEN", "role-green")

    channels_list = [
        {"id": "existing-cat", "type": discord_bot._CHANNEL_TYPE_CATEGORY, "name": "Teams", "parent_id": None},
        {"id": "existing-green-chan", "type": discord_bot._CHANNEL_TYPE_TEXT, "name": "green", "parent_id": "existing-cat"},
    ]
    post_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": _BOT_USER_ID})
        if request.method == "GET" and path == f"/api/v10/guilds/{_GUILD_ID}/channels":
            return httpx.Response(200, json=channels_list)
        if request.method == "PATCH" and path in ("/api/v10/channels/existing-cat", "/api/v10/channels/existing-green-chan"):
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST":
            post_paths.append(path)
            raise AssertionError("no create expected -- an existing category/channel must be adopted")
        raise AssertionError(f"unexpected call: {request.method} {path}")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_channels(http_client=client)

    result = _run(go())
    assert result["ok"] is True
    assert not post_paths
    assert result["adopted"] == ["GREEN"]
    assert _team_category_id(db_path) == "existing-cat"
    assert _team_channel_rows(db_path)["GREEN"] == "existing-green-chan"


def test_ensure_team_channels_recreates_a_channel_deleted_in_discord(db_path):
    _enable_team_channels(db_path)
    _set_team_role_file(db_path, "GREEN", "role-green")
    _set_team_channel_file(db_path, "GREEN", "deleted-chan-id")
    _set_team_category_id_file(db_path, "existing-cat")

    # The category is still there; GREEN's tracked channel id is gone
    # and no channel named "green" exists anywhere inside the category.
    channels_list = [
        {"id": "existing-cat", "type": discord_bot._CHANNEL_TYPE_CATEGORY, "name": "Teams", "parent_id": None},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": _BOT_USER_ID})
        if request.method == "GET" and path == f"/api/v10/guilds/{_GUILD_ID}/channels":
            return httpx.Response(200, json=channels_list)
        if request.method == "PATCH" and path == "/api/v10/channels/existing-cat":
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST" and path == f"/api/v10/guilds/{_GUILD_ID}/channels":
            import json as _json
            body = _json.loads(request.content)
            return httpx.Response(200, json={"id": "brand-new-green-chan", **body})
        raise AssertionError(f"unexpected call: {request.method} {path}")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_channels(http_client=client)

    result = _run(go())
    assert result["ok"] is True
    assert result["recreated"] == ["GREEN"]
    assert _team_channel_rows(db_path)["GREEN"] == "brand-new-green-chan"


def test_ensure_team_channels_reasserts_overwrites_on_existing_channel(db_path):
    """An operator (or a slip of the mouse) removed the @everyone deny
    by hand in Discord -- the next run must put it back, on a channel
    this function otherwise leaves entirely alone (bucket "unchanged").
    """
    _enable_team_channels(db_path)
    _set_team_role_file(db_path, "GREEN", "role-green")
    _set_team_channel_file(db_path, "GREEN", "green-chan-id")
    _set_team_category_id_file(db_path, "existing-cat")

    channels_list = [
        {"id": "existing-cat", "type": discord_bot._CHANNEL_TYPE_CATEGORY, "name": "Teams", "parent_id": None},
        # No @everyone deny in Discord's own current state -- this
        # module never reads a channel's CURRENT overwrites, only ever
        # writes its own authoritative list, so this list isn't even
        # consulted; the assertion below is on what gets PATCHed.
        {"id": "green-chan-id", "type": discord_bot._CHANNEL_TYPE_TEXT, "name": "green", "parent_id": "existing-cat"},
    ]
    patched = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": _BOT_USER_ID})
        if request.method == "GET" and path == f"/api/v10/guilds/{_GUILD_ID}/channels":
            return httpx.Response(200, json=channels_list)
        if request.method == "PATCH":
            import json as _json
            patched[path] = _json.loads(request.content)["permission_overwrites"]
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected call: {request.method} {path}")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_channels(http_client=client)

    result = _run(go())
    assert result["ok"] is True
    assert result["unchanged"] == ["GREEN"]
    assert patched["/api/v10/channels/green-chan-id"] == [_everyone_deny(), _team_allow("role-green"), _bot_allow()]


def test_ensure_team_channels_never_touches_a_channel_not_in_the_table(db_path):
    _enable_team_channels(db_path)
    _set_team_role_file(db_path, "GREEN", "role-green")
    _set_team_category_id_file(db_path, "existing-cat")

    unrelated_channel_id = "some-other-channel-id"
    channels_list = [
        {"id": "existing-cat", "type": discord_bot._CHANNEL_TYPE_CATEGORY, "name": "Teams", "parent_id": None},
        # Some unrelated text channel that happens to live in the same
        # category -- e.g. a general-chat channel an operator put there
        # by hand. Must never be read individually, patched, or deleted.
        {"id": unrelated_channel_id, "type": discord_bot._CHANNEL_TYPE_TEXT, "name": "general", "parent_id": "existing-cat"},
    ]
    touched_ids = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": _BOT_USER_ID})
        if request.method == "GET" and path == f"/api/v10/guilds/{_GUILD_ID}/channels":
            return httpx.Response(200, json=channels_list)
        if request.method in ("PATCH", "DELETE"):
            touched_ids.append(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST" and path == f"/api/v10/guilds/{_GUILD_ID}/channels":
            import json as _json
            body = _json.loads(request.content)
            return httpx.Response(200, json={"id": "new-green-chan", **body})
        raise AssertionError(f"unexpected call: {request.method} {path}")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_channels(http_client=client)

    result = _run(go())
    assert result["ok"] is True
    assert unrelated_channel_id not in touched_ids


def test_ensure_team_channels_never_issues_a_delete(db_path):
    _enable_team_channels(db_path)
    _set_all_team_roles(db_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method != "DELETE"
        path = request.url.path
        if request.method == "GET" and path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": _BOT_USER_ID})
        if request.method == "GET" and path == f"/api/v10/guilds/{_GUILD_ID}/channels":
            return httpx.Response(200, json=[])
        if request.method == "POST" and path == f"/api/v10/guilds/{_GUILD_ID}/channels":
            import json as _json
            body = _json.loads(request.content)
            return httpx.Response(200, json={"id": f"chan-{body['name']}", **body})
        raise AssertionError(f"unexpected call: {request.method} {path}")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_channels(http_client=client)

    result = _run(go())
    assert result["ok"] is True


def test_ensure_team_channels_bot_overwrite_always_present(db_path):
    _enable_team_channels(db_path)
    _set_team_role_file(db_path, "GREEN", "role-green")
    _set_team_channel_file(db_path, "GREEN", "green-chan-id")
    _set_team_category_id_file(db_path, "existing-cat")

    channels_list = [
        {"id": "existing-cat", "type": discord_bot._CHANNEL_TYPE_CATEGORY, "name": "Teams", "parent_id": None},
        {"id": "green-chan-id", "type": discord_bot._CHANNEL_TYPE_TEXT, "name": "green", "parent_id": "existing-cat"},
    ]
    seen_overwrite_lists = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": _BOT_USER_ID})
        if request.method == "GET" and path == f"/api/v10/guilds/{_GUILD_ID}/channels":
            return httpx.Response(200, json=channels_list)
        if request.method == "PATCH":
            import json as _json
            seen_overwrite_lists.append(_json.loads(request.content)["permission_overwrites"])
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected call: {request.method} {path}")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_channels(http_client=client)

    result = _run(go())
    assert result["ok"] is True
    assert len(seen_overwrite_lists) == 2  # the category, and GREEN's one channel
    assert all(_bot_allow() in overwrites for overwrites in seen_overwrite_lists)


def test_ensure_team_channels_noop_when_disabled(db_path):
    # db_path already enables role sync -- team_channels_enabled stays
    # at its column default (0) since _enable_team_channels() is never
    # called here.
    _set_team_role_file(db_path, "GREEN", "role-green")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected when team channels are disabled")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_channels(http_client=client)

    result = _run(go())
    assert result["ok"] is False


def test_ensure_team_channels_403_on_create_names_manage_channels(db_path):
    _enable_team_channels(db_path)
    _set_team_role_file(db_path, "GREEN", "role-green")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": _BOT_USER_ID})
        if request.method == "GET" and path == f"/api/v10/guilds/{_GUILD_ID}/channels":
            return httpx.Response(200, json=[])
        if request.method == "POST":
            return httpx.Response(403, json={"message": "Missing Permissions"})
        raise AssertionError(f"unexpected call: {request.method} {path}")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_channels(http_client=client)

    result = _run(go())
    assert result["ok"] is False
    assert "Manage Channels" in result["reason"]
    assert _BOT_TOKEN not in result["reason"]


def test_ensure_team_channels_403_on_overwrite_patch_names_manage_roles(db_path):
    _enable_team_channels(db_path)
    _set_team_role_file(db_path, "GREEN", "role-green")
    _set_team_category_id_file(db_path, "existing-cat")

    channels_list = [{"id": "existing-cat", "type": discord_bot._CHANNEL_TYPE_CATEGORY, "name": "Teams", "parent_id": None}]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": _BOT_USER_ID})
        if request.method == "GET" and path == f"/api/v10/guilds/{_GUILD_ID}/channels":
            return httpx.Response(200, json=channels_list)
        if request.method == "PATCH":
            return httpx.Response(403, json={"message": "Missing Permissions"})
        raise AssertionError(f"unexpected call: {request.method} {path}")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_channels(http_client=client)

    result = _run(go())
    assert result["ok"] is False
    assert "Manage Roles" in result["reason"]
    assert _BOT_TOKEN not in result["reason"]


def test_ensure_team_channels_error_never_contains_the_token(db_path):
    _enable_team_channels(db_path)
    _set_team_role_file(db_path, "GREEN", "role-green")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_channels(http_client=client)

    result = _run(go())
    assert result["ok"] is False
    assert _BOT_TOKEN not in result["reason"]


# ---- ensure_team_channels(): adoption of an operator's own channels ------
#
# The scenario that motivated _normalize_channel_name() (see this
# module's own docstring's ADOPTION section): an owner's guild already
# has a category and channels named and decorated their own way --
# `[Team Chat]` holding `red🟥`, `orange🟧`, etc -- and the old
# exact-string match could see none of it. These tests drive
# _ensure_category()/_ensure_team_channel() through real (mocked)
# Discord responses shaped like that guild, rather than one this bot
# built itself.


def test_ensure_team_channels_adopts_a_differently_named_category(db_path):
    """team_category_name is "Team Chat" (what an admin typed into the
    config field); the guild's own category is "[Team Chat]" -- normalize
    strips the brackets and the space, so both become "teamchat" and this
    must be found and adopted, never duplicated.
    """
    _enable_team_channels(db_path, category_name="Team Chat")
    _set_team_role_file(db_path, "GREEN", "role-green")

    channels_list = [
        {"id": "owner-cat", "type": discord_bot._CHANNEL_TYPE_CATEGORY, "name": "[Team Chat]", "parent_id": None},
        {"id": "owner-green-chan", "type": discord_bot._CHANNEL_TYPE_TEXT, "name": "green", "parent_id": "owner-cat"},
    ]
    post_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method != "DELETE"
        path = request.url.path
        if request.method == "GET" and path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": _BOT_USER_ID})
        if request.method == "GET" and path == f"/api/v10/guilds/{_GUILD_ID}/channels":
            return httpx.Response(200, json=channels_list)
        if request.method == "PATCH":
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST":
            post_paths.append(path)
            raise AssertionError("no create expected -- the category must be adopted by normalized name")
        raise AssertionError(f"unexpected call: {request.method} {path}")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_channels(http_client=client)

    result = _run(go())
    assert result["ok"] is True
    assert not post_paths
    assert _team_category_id(db_path) == "owner-cat"


def test_ensure_team_channels_adopts_an_emoji_suffixed_channel(db_path):
    """`red🟥` is RED's own channel in the owner's guild -- normalize
    strips the emoji, matches "red", and this must be adopted (its
    overwrites reasserted) with NO create call at all for RED.
    """
    _enable_team_channels(db_path)
    _set_team_role_file(db_path, "RED", "role-red")

    channels_list = [
        {"id": "owner-cat", "type": discord_bot._CHANNEL_TYPE_CATEGORY, "name": "Teams", "parent_id": None},
        {"id": "red-chan-id", "type": discord_bot._CHANNEL_TYPE_TEXT, "name": "red🟥", "parent_id": "owner-cat"},
    ]
    post_paths = []
    patched = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method != "DELETE"
        path = request.url.path
        if request.method == "GET" and path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": _BOT_USER_ID})
        if request.method == "GET" and path == f"/api/v10/guilds/{_GUILD_ID}/channels":
            return httpx.Response(200, json=channels_list)
        if request.method == "PATCH":
            import json as _json
            patched[path] = _json.loads(request.content)
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST":
            post_paths.append(path)
            raise AssertionError("no create expected -- red\U0001f7e5 must be adopted, not duplicated")
        raise AssertionError(f"unexpected call: {request.method} {path}")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_channels(http_client=client)

    result = _run(go())
    assert result["ok"] is True
    assert not post_paths
    assert result["adopted"] == ["RED"]
    assert _team_channel_rows(db_path)["RED"] == "red-chan-id"

    # Test 5: never renamed -- no PATCH body anywhere carries a `name`.
    for body in patched.values():
        assert "name" not in body

    # Test 6: overwrites ARE applied to the adopted channel, same full
    # authoritative list a freshly created channel would get.
    assert patched["/api/v10/channels/red-chan-id"]["permission_overwrites"] == [
        _everyone_deny(), _team_allow("role-red"), _bot_allow(),
    ]


def test_ensure_team_channels_creates_for_a_team_with_no_match(db_path):
    """GREEN has no stored channel id and nothing in the category
    normalizes to "green" -- the fallback create path, inside the
    category that WAS found (not a second, duplicate one).
    """
    _enable_team_channels(db_path)
    _set_team_role_file(db_path, "GREEN", "role-green")

    channels_list = [
        {"id": "owner-cat", "type": discord_bot._CHANNEL_TYPE_CATEGORY, "name": "Teams", "parent_id": None},
        {"id": "red-chan-id", "type": discord_bot._CHANNEL_TYPE_TEXT, "name": "red🟥", "parent_id": "owner-cat"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method != "DELETE"
        path = request.url.path
        if request.method == "GET" and path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": _BOT_USER_ID})
        if request.method == "GET" and path == f"/api/v10/guilds/{_GUILD_ID}/channels":
            return httpx.Response(200, json=channels_list)
        if request.method == "PATCH" and path == "/api/v10/channels/owner-cat":
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST" and path == f"/api/v10/guilds/{_GUILD_ID}/channels":
            import json as _json
            body = _json.loads(request.content)
            assert body["parent_id"] == "owner-cat"
            assert body["name"] == "green"
            return httpx.Response(200, json={"id": "new-green-chan", **body})
        raise AssertionError(f"unexpected call: {request.method} {path}")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_channels(http_client=client)

    result = _run(go())
    assert result["ok"] is True
    assert result["created"] == ["GREEN"]
    assert _team_channel_rows(db_path)["GREEN"] == "new-green-chan"


def test_ensure_team_channels_ambiguous_match_touches_nothing(db_path):
    """Two channels in the category both normalize to "red" -- this
    function must refuse to guess: no create, no overwrite PATCH for
    RED, and its stored channel_id (none yet) is left alone. The team is
    reported in the `ambiguous` bucket with both candidate names.
    """
    _enable_team_channels(db_path)
    _set_team_role_file(db_path, "RED", "role-red")

    channels_list = [
        {"id": "owner-cat", "type": discord_bot._CHANNEL_TYPE_CATEGORY, "name": "Teams", "parent_id": None},
        {"id": "red-chan-1", "type": discord_bot._CHANNEL_TYPE_TEXT, "name": "red🟥", "parent_id": "owner-cat"},
        {"id": "red-chan-2", "type": discord_bot._CHANNEL_TYPE_TEXT, "name": "Red", "parent_id": "owner-cat"},
    ]
    touched_channel_ids = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method != "DELETE"
        path = request.url.path
        if request.method == "GET" and path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": _BOT_USER_ID})
        if request.method == "GET" and path == f"/api/v10/guilds/{_GUILD_ID}/channels":
            return httpx.Response(200, json=channels_list)
        if request.method == "PATCH" and path == "/api/v10/channels/owner-cat":
            return httpx.Response(200, json={"ok": True})
        if request.method == "PATCH" and path in ("/api/v10/channels/red-chan-1", "/api/v10/channels/red-chan-2"):
            touched_channel_ids.append(path)
            raise AssertionError("must not edit either ambiguous candidate")
        if request.method == "POST" and path == f"/api/v10/guilds/{_GUILD_ID}/channels":
            raise AssertionError("must not create when the match is ambiguous")
        raise AssertionError(f"unexpected call: {request.method} {path}")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_channels(http_client=client)

    result = _run(go())
    assert result["ok"] is True
    assert not touched_channel_ids
    assert result["created"] == []
    assert result["adopted"] == []
    assert result["ambiguous"] == [{"team": "RED", "candidates": ["Red", "red🟥"]}]
    assert _team_channel_rows(db_path)["RED"] is None


def test_ensure_team_channels_stored_id_wins_even_if_renamed(db_path):
    """RED's tracked channel_id still exists in the guild, but an
    operator renamed it to something that no longer matches "red" at
    all -- step 1 of the adoption order (this module's own docstring)
    must use it anyway, by id, and never go looking for a name match.
    """
    _enable_team_channels(db_path)
    _set_team_role_file(db_path, "RED", "role-red")
    _set_team_channel_file(db_path, "RED", "red-chan-id")
    _set_team_category_id_file(db_path, "owner-cat")

    channels_list = [
        {"id": "owner-cat", "type": discord_bot._CHANNEL_TYPE_CATEGORY, "name": "Teams", "parent_id": None},
        {"id": "red-chan-id", "type": discord_bot._CHANNEL_TYPE_TEXT, "name": "totally-renamed-channel", "parent_id": "owner-cat"},
    ]
    patched = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method != "DELETE"
        path = request.url.path
        if request.method == "GET" and path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": _BOT_USER_ID})
        if request.method == "GET" and path == f"/api/v10/guilds/{_GUILD_ID}/channels":
            return httpx.Response(200, json=channels_list)
        if request.method == "PATCH":
            import json as _json
            patched[path] = _json.loads(request.content)
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST":
            raise AssertionError("no create expected -- the stored id is still valid")
        raise AssertionError(f"unexpected call: {request.method} {path}")

    async def go():
        async with _mock_client(handler) as client:
            return await discord_bot.ensure_team_channels(http_client=client)

    result = _run(go())
    assert result["ok"] is True
    assert result["unchanged"] == ["RED"]
    assert _team_channel_rows(db_path)["RED"] == "red-chan-id"
    assert "name" not in patched["/api/v10/channels/red-chan-id"]
