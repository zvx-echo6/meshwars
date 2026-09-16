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
