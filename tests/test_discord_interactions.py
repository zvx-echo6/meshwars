"""Tests for app/discord_interactions.py -- Discord HTTP Interactions
slash commands (POST /api/discord/interactions) and the one piece of
app/discord_bot.py this feature adds, register_commands().

No real network access anywhere in this file: every outbound call
(register_commands()'s PUT, the deferred follow-up's PATCH) goes
through an httpx.MockTransport, the same pattern
tests/test_discord_bot.py and tests/test_discord_notify.py already use
for their own outbound Discord calls.

Signatures are REAL: each test generates its own Ed25519 keypair
(cryptography.hazmat.primitives.asymmetric.ed25519) and signs requests
exactly the way Discord itself does (timestamp.encode() + raw body
bytes) -- see _sign() below. This is what lets test_bad_signature_401
etc. actually exercise _verify_signature() rather than assuming it
works.

Most tests go through the real HTTP surface (a FastAPI app around just
this module's router, TestClient) so the security/gating order in
discord_interactions_endpoint() is exercised end to end. The deferred-
delivery and handler-exception tests instead call _dispatch() directly:
the route itself never threads an injectable http_client through to
_patch_followup(), so there is no way to intercept that outbound PATCH
from the HTTP layer alone -- calling _dispatch() (the same function the
route calls) with http_client=<mock> is the one seam that exists for
it, exactly the way app/discord_bot.py's own tests call _request()'s
callers directly for the same reason.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.db as db
from app import discord_bot
from app import discord_interactions as di
from app.config import settings
from app.db import MIGRATIONS, SCHEMA

_ENDPOINT = "/api/discord/interactions"


# ---- schema / db_path fixture (same shape tests/test_discord_admin.py's
# own db_path fixture uses -- a real file-backed database, since this
# module's route opens its own connection via app.db.connect()) --------


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
    return path


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(di.router)
    return TestClient(app)


def _enable_slash(db_path, *, public_key_hex: str, app_id: str = "app-1000", enabled: int = 1) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE discord_config SET slash_enabled = ?, app_id = ?, public_key = ? WHERE id = 1",
        (enabled, app_id, public_key_hex),
    )
    conn.commit()
    conn.close()


# ---- signing --------------------------------------------------------------


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    priv = Ed25519PrivateKey.generate()
    pub_hex = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    ).hex()
    return priv, pub_hex


def _sign(priv: Ed25519PrivateKey, timestamp: str, body: bytes) -> str:
    return priv.sign(timestamp.encode("utf-8") + body).hex()


def _post_interaction(client: TestClient, priv: Ed25519PrivateKey, payload: dict, *,
                       timestamp: str | None = None, headers: dict | None = None):
    body = json.dumps(payload).encode("utf-8")
    ts = timestamp if timestamp is not None else str(int(time.time()))
    hdrs = {"X-Signature-Ed25519": _sign(priv, ts, body), "X-Signature-Timestamp": ts}
    if headers:
        hdrs.update(headers)
    return client.post(_ENDPOINT, content=body, headers=hdrs)


def _command(name: str, options: list | None = None, *, application_id="app-1000", token="itok-1") -> dict:
    data = {"name": name}
    if options:
        data["options"] = options
    return {"type": 2, "id": "i1", "application_id": application_id, "token": token, "data": data}


# ---- 1: valid PING -> 200 {"type": 1} -------------------------------------


def test_ping_valid_signature_returns_pong(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    client = _client()

    resp = _post_interaction(client, priv, {"type": 1, "id": "1", "application_id": "a", "token": "t"})
    assert resp.status_code == 200
    assert resp.json() == {"type": 1}


# ---- 2: bad signature / missing headers / malformed hex -> 401 -----------


def test_signature_from_wrong_key_is_401(db_path):
    priv, pub_hex = _keypair()
    other_priv, _ = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    client = _client()

    resp = _post_interaction(client, other_priv, {"type": 1})
    assert resp.status_code == 401


def test_missing_signature_headers_is_401(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    client = _client()

    resp = client.post(_ENDPOINT, content=b'{"type": 1}')
    assert resp.status_code == 401


def test_malformed_hex_signature_is_401(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    client = _client()

    resp = client.post(
        _ENDPOINT, content=b'{"type": 1}',
        headers={"X-Signature-Ed25519": "not-valid-hex-zz", "X-Signature-Timestamp": str(int(time.time()))},
    )
    assert resp.status_code == 401


# ---- 3: stale timestamp -> 401 --------------------------------------------


def test_stale_timestamp_is_401(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    client = _client()

    stale = str(int(time.time()) - 400)  # > 5 minutes old
    resp = _post_interaction(client, priv, {"type": 1}, timestamp=stale)
    assert resp.status_code == 401


def test_future_timestamp_is_401(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    client = _client()

    future = str(int(time.time()) + 400)
    resp = _post_interaction(client, priv, {"type": 1}, timestamp=future)
    assert resp.status_code == 401


# ---- 4: disabled or no key -> 404 -----------------------------------------


def test_slash_disabled_is_404(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex, enabled=0)
    client = _client()

    resp = _post_interaction(client, priv, {"type": 1})
    assert resp.status_code == 404


def test_no_public_key_configured_is_404(db_path):
    priv, _ = _keypair()
    _enable_slash(db_path, public_key_hex="", enabled=1)
    client = _client()

    resp = _post_interaction(client, priv, {"type": 1})
    assert resp.status_code == 404


# ---- 5/6: /me -- linked and unlinked --------------------------------------


def test_me_linked_is_ephemeral_and_names_the_right_player(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)

    conn = sqlite3.connect(db_path)
    now = int(time.time())
    account_id = conn.execute("INSERT INTO account(created_at) VALUES (?)", (now,)).lastrowid
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, linked_at) VALUES ('discord', ?, ?, ?)",
        ("999888777", account_id, now),
    )
    player_id = conn.execute(
        "INSERT INTO player(display_name, team, created_at, account_id) VALUES (?, ?, ?, ?)",
        ("Zed", "RED", now, account_id),
    ).lastrowid
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) VALUES ('mc', 'aabbccdd', ?, ?)",
        (player_id, now),
    )
    conn.execute(
        "INSERT INTO mc_season(id, protocol, started_at, ends_at, status) VALUES (1, 'mc', 0, ?, 'active')",
        (now + 1_000_000,),
    )
    conn.execute(
        "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts) "
        "VALUES (1, 'c1', 'RED', ?, ?)",
        (player_id, now),
    )
    conn.commit()
    conn.close()

    client = _client()
    # member.user.id is how a guild invocation identifies the caller.
    payload = _command("me", token="me-tok")
    payload["member"] = {"user": {"id": "999888777"}}
    resp = _post_interaction(client, priv, payload)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == 4
    assert body["data"]["flags"] == di._FLAG_EPHEMERAL

    text = json.dumps(body)
    assert "Zed" in text
    # Never a location, cell, place, or node/radio identifier.
    for forbidden in ("cell_id", "lat_idx", "lon_idx", "aabbccdd", "node_ref", "\"place\""):
        assert forbidden not in text


def test_me_unlinked_tells_caller_to_connect_discord(db_path, monkeypatch):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://meshwars.example")
    client = _client()

    payload = _command("me")
    payload["member"] = {"user": {"id": "000111222"}}
    resp = _post_interaction(client, priv, payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["flags"] == di._FLAG_EPHEMERAL
    assert "https://meshwars.example/account" in body["data"]["content"]


# ---- 7: /standings both boards, ranked order ------------------------------


def test_standings_ranked_order_for_both_boards(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)

    conn = sqlite3.connect(db_path)
    now = int(time.time())
    conn.execute(
        "INSERT INTO mc_season(id, protocol, started_at, ends_at, status) VALUES (1, 'mc', 0, ?, 'active')",
        (now + 1_000_000,),
    )
    pid = conn.execute(
        "INSERT INTO player(display_name, team, created_at) VALUES ('P1', 'RED', ?)", (now,)
    ).lastrowid
    conn.execute(
        "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts) "
        "VALUES (1, 'a', 'RED', ?, ?)", (pid, now),
    )
    for cid in ("b", "c", "d"):
        conn.execute(
            "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts) "
            "VALUES (1, ?, 'BLUE', ?, ?)", (cid, pid, now),
        )
    conn.commit()
    conn.close()

    client = _client()

    resp_mc = _post_interaction(client, priv, _command("standings", [{"name": "board", "value": "mc"}]))
    assert resp_mc.status_code == 200, resp_mc.text
    desc_mc = resp_mc.json()["data"]["embeds"][0]["description"]
    # BLUE (3 squares) outranks RED (1 square).
    assert desc_mc.index("BLUE") < desc_mc.index("RED")

    resp_mt = _post_interaction(client, priv, _command("standings", [{"name": "board", "value": "mt"}]))
    assert resp_mt.status_code == 200, resp_mt.text
    assert "No standings recorded." in resp_mt.json()["data"]["embeds"][0]["description"]

    # Default board (no option given) is MeshCore.
    resp_default = _post_interaction(client, priv, _command("standings"))
    assert resp_default.json()["data"]["embeds"][0] == resp_mc.json()["data"]["embeds"][0]


# ---- 8: /honors default, explicit, unknown month --------------------------


def test_honors_default_explicit_and_unknown_month(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)

    conn = sqlite3.connect(db_path)
    now = int(time.time())
    conn.execute(
        "INSERT INTO month_result(month, protocol, closed_at) VALUES ('2026-07', 'mc', ?)", (now,)
    )
    conn.execute(
        "INSERT INTO month_standing(month, protocol, team, squares, checkin_points, explorer_points) "
        "VALUES ('2026-07', 'mc', 'RED', 10, 0, 0)"
    )
    conn.commit()
    conn.close()

    client = _client()

    resp_default = _post_interaction(client, priv, _command("honors"))
    assert resp_default.status_code == 200, resp_default.text
    assert "July 2026" in resp_default.json()["data"]["embeds"][0]["title"]

    resp_explicit = _post_interaction(
        client, priv, _command("honors", [{"name": "month", "value": "2026-07"}])
    )
    assert "July 2026" in resp_explicit.json()["data"]["embeds"][0]["title"]

    resp_unknown = _post_interaction(
        client, priv, _command("honors", [{"name": "month", "value": "2099-01"}])
    )
    body = resp_unknown.json()["data"]
    assert body["flags"] == di._FLAG_EPHEMERAL
    assert "2099-01" in body["content"]

    resp_bad_shape = _post_interaction(
        client, priv, _command("honors", [{"name": "month", "value": "not-a-month"}])
    )
    body_bad = resp_bad_shape.json()["data"]
    assert body_bad["flags"] == di._FLAG_EPHEMERAL


# ---- 9: /nextnet -- own timezone, live table, enabled-only ---------------


def test_next_net_start_uses_the_nets_own_timezone():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # A known Tuesday, noon, in America/Boise.
    now_ts = int(datetime(2026, 9, 15, 12, 0, tzinfo=ZoneInfo("America/Boise")).timestamp())
    net = {"weekday": 2, "start_hour": 18, "timezone": "America/Boise"}  # Wednesday 18:00

    start_ts = di._next_net_start(net, now_ts)
    local = datetime.fromtimestamp(start_ts, tz=ZoneInfo("America/Boise"))
    assert local.weekday() == 2
    assert local.hour == 18
    assert local.date().isoformat() == "2026-09-16"


def test_nextnet_reflects_enabled_nets_and_picks_up_new_ones(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO checkin_net(label, protocol, kind, connector_url, weekday, start_hour, "
        " end_hour, timezone, enabled, created_at) "
        "VALUES ('Boise Net', 'mc', 'corescope', 'http://x', 2, 18, 20, 'America/Boise', 1, 0)"
    )
    conn.execute(
        "INSERT INTO checkin_net(label, protocol, kind, connector_url, weekday, start_hour, "
        " end_hour, timezone, enabled, created_at) "
        "VALUES ('Disabled Net', 'mt', 'corescope', 'http://x', 3, 18, 20, 'America/Denver', 0, 0)"
    )
    conn.commit()
    conn.close()

    client = _client()
    resp = _post_interaction(client, priv, _command("nextnet"))
    assert resp.status_code == 200, resp.text
    desc = resp.json()["data"]["embeds"][0]["description"]
    assert "Boise Net" in desc
    assert "Disabled Net" not in desc
    assert "<t:" in desc and ":F>" in desc and ":R>" in desc

    # A net added after this test started appears on the very next call --
    # no code change, no restart.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO checkin_net(label, protocol, kind, connector_url, weekday, start_hour, "
        " end_hour, timezone, enabled, created_at) "
        "VALUES ('New Net', 'mc', 'corescope', 'http://x', 5, 10, 12, 'America/New_York', 1, 0)"
    )
    conn.commit()
    conn.close()

    resp2 = _post_interaction(client, priv, _command("nextnet"))
    assert "New Net" in resp2.json()["data"]["embeds"][0]["description"]


def test_nextnet_with_no_nets_configured(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    client = _client()

    resp = _post_interaction(client, priv, _command("nextnet"))
    assert resp.status_code == 200
    assert "No check-in nets" in resp.json()["data"]["content"]


# ---- 10: /player -- exact, prefix, no match -------------------------------


def test_player_exact_match_prefix_list_and_no_match(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)

    conn = sqlite3.connect(db_path)
    now = int(time.time())
    conn.execute("INSERT INTO player(display_name, team, created_at) VALUES ('Alice', 'RED', ?)", (now,))
    conn.execute("INSERT INTO player(display_name, team, created_at) VALUES ('Alicia', 'BLUE', ?)", (now,))
    conn.commit()
    conn.close()

    client = _client()

    resp_exact = _post_interaction(client, priv, _command("player", [{"name": "name", "value": "alice"}]))
    assert resp_exact.status_code == 200, resp_exact.text
    text = json.dumps(resp_exact.json())
    assert "Alice" in text
    assert "Alicia" not in text
    for forbidden in ("cell_id", "node_ref", "radio", "lat_idx", "lon_idx"):
        assert forbidden not in text

    resp_prefix = _post_interaction(client, priv, _command("player", [{"name": "name", "value": "ali"}]))
    content = resp_prefix.json()["data"]["content"]
    assert "Alice" in content and "Alicia" in content

    resp_none = _post_interaction(client, priv, _command("player", [{"name": "name", "value": "zzz"}]))
    assert "No player found" in resp_none.json()["data"]["content"]


# ---- 11: every response carries allowed_mentions.parse == [] -------------


def test_every_response_forbids_mentions(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    client = _client()

    commands = [
        _command("standings"),
        _command("nextnet"),
        _command("player", [{"name": "name", "value": "nobody-here"}]),
        {"type": 2, "application_id": "a", "token": "t", "data": {"name": "not-a-real-command"}},
    ]
    for payload in commands:
        resp = _post_interaction(client, priv, payload)
        assert resp.json()["data"]["allowed_mentions"] == {"parse": []}


# ---- 12: slow handler -> deferred, follow-up uses the interaction token --


def test_slow_handler_defers_and_delivers_via_interaction_token(db_path, monkeypatch):
    """Calls _dispatch() directly (see this module's own docstring for
    why): the route itself never threads an injectable http_client
    through to the deferred PATCH, so this is the one seam that exists
    to intercept it without a real network call.
    """
    monkeypatch.setattr(di, "_HANDLER_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(settings, "discord_bot_token", "super-secret-bot-token")

    def slow_handler(conn, body):
        time.sleep(0.2)
        return di._data(content="slow but done")

    monkeypatch.setitem(
        di._COMMANDS_BY_NAME, "nextnet",
        di.Command(name="nextnet", description="x", options=[], handler=slow_handler, always_ephemeral=False),
    )

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    body = {"type": 2, "application_id": "app-999", "token": "interaction-token-xyz",
            "data": {"name": "nextnet"}}
    cfg = {"app_id": "app-999"}

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
            result = await di._dispatch(body, cfg, http_client=mock_client)
            assert result["type"] == 5
            # Give the background follow-up time to finish (handler
            # sleeps 0.2s; this is generous headroom above that).
            await asyncio.sleep(0.6)
            return result

    result = asyncio.run(run())
    assert result["data"]["allowed_mentions"] == {"parse": []}

    assert captured["url"] == "https://discord.com/api/v10/webhooks/app-999/interaction-token-xyz/messages/@original"
    assert captured["body"]["content"] == "slow but done"
    # The interaction token is embedded in the URL; there is no
    # Authorization header at all, and specifically never the bot token.
    auth = captured["headers"].get("authorization", "")
    assert "super-secret-bot-token" not in auth
    assert "bot" not in auth.lower()
    assert "authorization" not in captured["headers"] or captured["headers"]["authorization"] == ""


def test_slow_always_ephemeral_command_defers_with_ephemeral_flag(db_path, monkeypatch):
    monkeypatch.setattr(di, "_HANDLER_BUDGET_SECONDS", 0.05)

    def slow_me(conn, body):
        time.sleep(0.2)
        return di._ephemeral("done")

    monkeypatch.setitem(
        di._COMMANDS_BY_NAME, "me",
        di.Command(name="me", description="x", options=[], handler=slow_me, always_ephemeral=True),
    )
    body = {"type": 2, "application_id": "app-1", "token": "tok-1", "data": {"name": "me"}}

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"ok": True})
        )) as mock_client:
            result = await di._dispatch(body, {"app_id": "app-1"}, http_client=mock_client)
            await asyncio.sleep(0.6)
            return result

    result = asyncio.run(run())
    assert result["type"] == 5
    assert result["data"]["flags"] == di._FLAG_EPHEMERAL


# ---- 13: handler exception -> generic ephemeral error, no traceback -----


def test_handler_exception_yields_generic_ephemeral_error(db_path, monkeypatch):
    def broken_handler(conn, body):
        raise RuntimeError("sensitive internal detail, never show this")

    monkeypatch.setitem(
        di._COMMANDS_BY_NAME, "nextnet",
        di.Command(name="nextnet", description="x", options=[], handler=broken_handler, always_ephemeral=False),
    )
    body = {"type": 2, "application_id": "a", "token": "t", "data": {"name": "nextnet"}}

    result = asyncio.run(di._dispatch(body, {"app_id": "a"}))
    assert result["type"] == 4
    assert result["data"]["flags"] == di._FLAG_EPHEMERAL
    content = result["data"]["content"]
    assert "sensitive internal detail" not in content
    assert "RuntimeError" not in content
    assert "Traceback" not in content


# ---- 14: register_commands() PUTs the full registry -----------------------


def test_register_commands_puts_full_registry_matching_dispatch_table(db_path, monkeypatch):
    monkeypatch.setattr(settings, "discord_bot_token", "test-bot-token")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE discord_config SET guild_id = 'guild-1', app_id = 'app-1' WHERE id = 1")
    conn.commit()
    conn.close()

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=captured["body"])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
            return await discord_bot.register_commands(http_client=mock_client)

    result = asyncio.run(run())

    assert result["ok"] is True
    assert captured["method"] == "PUT"
    assert captured["url"] == "https://discord.com/api/v10/applications/app-1/guilds/guild-1/commands"
    assert captured["authorization"] == "Bot test-bot-token"

    sent_names = {c["name"] for c in captured["body"]}
    dispatch_names = set(di._COMMANDS_BY_NAME.keys())
    assert sent_names == dispatch_names
    assert set(result["commands"]) == dispatch_names
    # Every command definition sent to Discord round-trips through
    # Command.definition() -- name/description/options only.
    for c in captured["body"]:
        assert set(c.keys()) == {"name", "description", "options"}


def test_register_commands_not_configured_returns_ok_false(db_path):
    # No bot token, no app id, no guild id -- the bare CREATE TABLE
    # defaults.
    result = asyncio.run(discord_bot.register_commands())
    assert result["ok"] is False
    assert "reason" in result


def test_registry_contains_the_new_account_commands():
    for name in ("link", "join", "radios", "setupcheck"):
        assert name in di._COMMANDS_BY_NAME
        # No slash-command OPTIONS at all -- every one of these gathers
        # its input through a modal/component, never a command option
        # (see /link's own docstring on why an API key is never a
        # slash-command option).
        assert di._COMMANDS_BY_NAME[name].options == []
    assert set(di._COMMANDS_BY_NAME.keys()) == {c.name for c in di.COMMANDS}


# ---- 15: /link, /join, /radios, /setupcheck -------------------------------
#
# GOOD_KEY/DISABLED_KEY resolve through FakeIngestor, same shape
# tests/test_account_api.py's own FakeIngestor uses for
# request.app.state.mc_ingestor.


GOOD_KEY = "good-key"
DISABLED_KEY = "disabled-key"
KEY_PLAYER_ID = 9001


class FakeIngestor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def authenticate(self, raw_key: str):
        from app.mc_ingest import AuthResult

        self.calls.append(raw_key)
        if raw_key == GOOD_KEY:
            return AuthResult("ok", KEY_PLAYER_ID)
        if raw_key == DISABLED_KEY:
            return AuthResult("disabled", KEY_PLAYER_ID)
        return AuthResult("not_found")


def _client_with_ingestor(ingestor) -> TestClient:
    app = FastAPI()
    app.include_router(di.router)
    app.state.mc_ingestor = ingestor
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_account_command_rate_limiters():
    """di._link_rate_limiter/_join_rate_limiter/_radios_rate_limiter are
    module-level singletons (see app/auth.py's module docstring on why
    every _BoundedHits budget in this codebase is built once, at import
    time) -- cleared between tests the same way
    tests/test_account_api.py's own autouse fixture clears
    _link_key_addr_limiter.
    """
    di._link_rate_limiter._hits.clear()
    di._join_rate_limiter._hits.clear()
    di._radios_rate_limiter._hits.clear()
    yield


def _component(custom_id: str, *, snowflake: str, values: list[str] | None = None) -> dict:
    data: dict = {"custom_id": custom_id, "component_type": 3 if values is not None else 2}
    if values is not None:
        data["values"] = values
    return {
        "type": 3, "id": "i1", "application_id": "app-1000", "token": "itok-1",
        "data": data, "member": {"user": {"id": snowflake}},
    }


def _modal_submit(custom_id: str, fields: dict[str, str], *, snowflake: str) -> dict:
    components = [
        {"type": 1, "components": [{"type": 4, "custom_id": k, "value": v}]}
        for k, v in fields.items()
    ]
    return {
        "type": 5, "id": "i1", "application_id": "app-1000", "token": "itok-1",
        "data": {"custom_id": custom_id, "components": components},
        "member": {"user": {"id": snowflake}},
    }


def _make_player(db_path, player_id: int, name: str, team: str = "RED", *, key: str | None = None) -> None:
    conn = sqlite3.connect(db_path)
    now = int(time.time())
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?, ?, ?, ?)",
        (player_id, name, team, now),
    )
    if key is not None:
        from app.mc_ingest import hash_secret

        conn.execute(
            "INSERT INTO api_key(key_hash, player_id, issued_at) VALUES (?, ?, ?)",
            (hash_secret(key), player_id, now),
        )
    conn.commit()
    conn.close()


def _make_account_with_discord_identity(db_path, snowflake: str, *, disabled: bool = False) -> int:
    conn = sqlite3.connect(db_path)
    now = int(time.time())
    disabled_at = now if disabled else None
    account_id = conn.execute(
        "INSERT INTO account(created_at, disabled_at) VALUES (?, ?)", (now, disabled_at)
    ).lastrowid
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, linked_at) "
        "VALUES ('discord', ?, ?, ?)",
        (snowflake, account_id, now),
    )
    conn.commit()
    conn.close()
    return account_id


# ---- /link ------------------------------------------------------------


def test_link_opens_modal_never_a_slash_option(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    client = _client()

    resp = _post_interaction(client, priv, _command("link"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == di._RESPONSE_MODAL
    assert body["data"]["custom_id"] == "link:modal"
    field = body["data"]["components"][0]["components"][0]
    assert field["custom_id"] == "api_key"
    assert field["type"] == di._COMPONENT_TEXT_INPUT


def test_link_modal_submit_new_snowflake_creates_account_and_links(db_path, monkeypatch):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    _make_player(db_path, KEY_PLAYER_ID, "KeyHolder", "GREEN")

    synced = []

    async def fake_sync(conn, player_id):
        synced.append(player_id)

    monkeypatch.setattr(di.discord_bot, "sync_member_safe", fake_sync)

    ingestor = FakeIngestor()
    client = _client_with_ingestor(ingestor)

    resp = _post_interaction(
        client, priv, _modal_submit("link:modal", {"api_key": GOOD_KEY}, snowflake="42424242"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == 4
    assert body["data"]["flags"] == di._FLAG_EPHEMERAL
    assert body["data"]["allowed_mentions"] == {"parse": []}
    content = body["data"]["content"]
    assert "KeyHolder" in content and "GREEN" in content
    assert GOOD_KEY not in content

    conn = sqlite3.connect(db_path)
    identity = conn.execute(
        "SELECT account_id FROM account_identity WHERE provider = 'discord' AND subject = ?",
        ("42424242",),
    ).fetchone()
    assert identity is not None
    account_id = identity[0]
    player_row = conn.execute(
        "SELECT account_id FROM player WHERE player_id = ?", (KEY_PLAYER_ID,)
    ).fetchone()
    assert player_row[0] == account_id
    # Two account_link_event rows exist for a brand-new snowflake --
    # 'identity_linked' from _create_account_with_identity() and
    # 'player_linked' from _claim_player() -- assert on the latter.
    event = conn.execute(
        "SELECT actor, detail FROM account_link_event"
        " WHERE account_id = ? AND kind = 'player_linked'", (account_id,)
    ).fetchone()
    assert event is not None
    assert event[0] == "user"
    assert "Discord" in event[1]
    conn.close()

    assert synced == [KEY_PLAYER_ID]


def test_link_modal_submit_existing_account_links_player(db_path, monkeypatch):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    _make_player(db_path, KEY_PLAYER_ID, "Existing", "BLUE")
    account_id = _make_account_with_discord_identity(db_path, "55555555")

    monkeypatch.setattr(di.discord_bot, "sync_member_safe", lambda conn, player_id: _noop())

    ingestor = FakeIngestor()
    client = _client_with_ingestor(ingestor)

    resp = _post_interaction(
        client, priv, _modal_submit("link:modal", {"api_key": GOOD_KEY}, snowflake="55555555"),
    )
    assert resp.status_code == 200, resp.text
    assert "Existing" in resp.json()["data"]["content"]

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT account_id FROM player WHERE player_id = ?", (KEY_PLAYER_ID,)).fetchone()
    conn.close()
    assert row[0] == account_id


async def _noop():
    return None


def test_link_modal_submit_bad_key_is_refused(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    ingestor = FakeIngestor()
    client = _client_with_ingestor(ingestor)

    resp = _post_interaction(
        client, priv, _modal_submit("link:modal", {"api_key": "nonsense"}, snowflake="1"),
    )
    body = resp.json()
    assert body["data"]["flags"] == di._FLAG_EPHEMERAL
    assert "not valid" in body["data"]["content"]

    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM account_identity").fetchone()[0]
    conn.close()
    assert n == 0


def test_link_modal_submit_disabled_player_is_refused(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    ingestor = FakeIngestor()
    client = _client_with_ingestor(ingestor)

    resp = _post_interaction(
        client, priv, _modal_submit("link:modal", {"api_key": DISABLED_KEY}, snowflake="1"),
    )
    body = resp.json()
    assert "disabled" in body["data"]["content"]


def test_link_modal_submit_account_already_has_player_is_refused(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    _make_player(db_path, KEY_PLAYER_ID, "KeyHolder", "GREEN")
    _make_player(db_path, KEY_PLAYER_ID + 1, "AlreadyMine", "RED")
    account_id = _make_account_with_discord_identity(db_path, "77777777")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE player SET account_id = ? WHERE player_id = ?", (account_id, KEY_PLAYER_ID + 1)
    )
    conn.commit()
    conn.close()

    ingestor = FakeIngestor()
    client = _client_with_ingestor(ingestor)
    resp = _post_interaction(
        client, priv, _modal_submit("link:modal", {"api_key": GOOD_KEY}, snowflake="77777777"),
    )
    body = resp.json()
    assert "already has a linked player" in body["data"]["content"]


def test_link_modal_submit_player_claimed_by_different_account_is_refused(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    _make_player(db_path, KEY_PLAYER_ID, "KeyHolder", "GREEN")
    other_account_id = _make_account_with_discord_identity(db_path, "88888888")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE player SET account_id = ? WHERE player_id = ?", (other_account_id, KEY_PLAYER_ID)
    )
    conn.commit()
    conn.close()

    ingestor = FakeIngestor()
    client = _client_with_ingestor(ingestor)
    resp = _post_interaction(
        client, priv, _modal_submit("link:modal", {"api_key": GOOD_KEY}, snowflake="99999999"),
    )
    body = resp.json()
    assert "already linked to a different account" in body["data"]["content"]


def test_link_modal_submit_rate_limited_per_snowflake(db_path, monkeypatch):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    monkeypatch.setattr(settings, "account_link_key_rate_limit_attempts", 1)
    ingestor = FakeIngestor()
    client = _client_with_ingestor(ingestor)

    _post_interaction(client, priv, _modal_submit("link:modal", {"api_key": "x"}, snowflake="1"))
    resp = _post_interaction(client, priv, _modal_submit("link:modal", {"api_key": "x"}, snowflake="1"))
    assert "Too many attempts" in resp.json()["data"]["content"]


def test_disabled_account_refused_on_every_new_command(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    _make_account_with_discord_identity(db_path, "13131313", disabled=True)
    client = _client_with_ingestor(FakeIngestor())

    for payload in (
        _command("link"),
        _command("join"),
        _command("radios"),
        _command("setupcheck"),
    ):
        payload["member"] = {"user": {"id": "13131313"}}
        resp = _post_interaction(client, priv, payload)
        assert "disabled" in resp.json()["data"]["content"], payload["data"]["name"]


# ---- /join --------------------------------------------------------------


def test_join_meshcore_never_mints_or_shows_a_key(db_path, monkeypatch):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://meshwars.example")
    monkeypatch.setattr(di.discord_bot, "sync_member_safe", lambda conn, player_id: _noop())
    client = _client_with_ingestor(FakeIngestor())

    resp = _post_interaction(
        client, priv,
        _modal_submit(
            "join:modal",
            {"display_name": "NewMcPlayer", "team": "RED", "protocol": "mc"},
            snowflake="21212121",
        ),
    )
    assert resp.status_code == 200, resp.text
    content = resp.json()["data"]["content"]
    assert "https://meshwars.example/join" in content
    assert "only issued through the website" in content
    # No key-shaped string anywhere in the reply.
    assert "key\":" not in content

    conn = sqlite3.connect(db_path)
    player_id = conn.execute(
        "SELECT player_id FROM player WHERE display_name = 'NewMcPlayer'"
    ).fetchone()[0]
    n = conn.execute("SELECT COUNT(*) FROM api_key WHERE player_id = ?", (player_id,)).fetchone()[0]
    conn.close()
    assert n == 0


def test_join_meshtastic_never_mints_a_key(db_path, monkeypatch):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    monkeypatch.setattr(settings, "join_meshtastic_enabled", True)
    monkeypatch.setattr(di.discord_bot, "sync_member_safe", lambda conn, player_id: _noop())
    client = _client_with_ingestor(FakeIngestor())

    resp = _post_interaction(
        client, priv,
        _modal_submit(
            "join:modal",
            {"display_name": "NewMtPlayer", "team": "BLUE", "protocol": "mt"},
            snowflake="23232323",
        ),
    )
    assert resp.status_code == 200, resp.text
    content = resp.json()["data"]["content"]
    assert "NewMtPlayer" in content and "BLUE" in content
    assert "/radios" in content

    conn = sqlite3.connect(db_path)
    player_id = conn.execute(
        "SELECT player_id FROM player WHERE display_name = 'NewMtPlayer'"
    ).fetchone()[0]
    n = conn.execute("SELECT COUNT(*) FROM api_key WHERE player_id = ?", (player_id,)).fetchone()[0]
    account_row = conn.execute(
        "SELECT account_id FROM player WHERE player_id = ?", (player_id,)
    ).fetchone()
    identity = conn.execute(
        "SELECT account_id FROM account_identity WHERE provider = 'discord' AND subject = '23232323'"
    ).fetchone()
    conn.close()
    assert n == 0
    assert account_row[0] == identity[0]


def test_join_invalid_team_lists_valid_choices(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    client = _client_with_ingestor(FakeIngestor())

    resp = _post_interaction(
        client, priv,
        _modal_submit(
            "join:modal",
            {"display_name": "Someone", "team": "NOTATEAM", "protocol": "mc"},
            snowflake="31313131",
        ),
    )
    body = resp.json()["data"]
    assert body["flags"] == di._FLAG_EPHEMERAL
    for team in settings.teams_list:
        assert team in body["content"]


def test_join_existing_player_is_refused_and_points_to_me(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    _make_player(db_path, KEY_PLAYER_ID, "AlreadyIn", "RED")
    account_id = _make_account_with_discord_identity(db_path, "41414141")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE player SET account_id = ? WHERE player_id = ?", (account_id, KEY_PLAYER_ID))
    conn.commit()
    conn.close()

    client = _client_with_ingestor(FakeIngestor())
    resp = _post_interaction(client, priv, _command("join", token="jt"))
    payload = _command("join")
    payload["member"] = {"user": {"id": "41414141"}}
    resp = _post_interaction(client, priv, payload)
    body = resp.json()["data"]
    assert "/me" in body["content"]


def test_join_rate_limited_per_snowflake(db_path, monkeypatch):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    monkeypatch.setattr(settings, "join_rate_limit_attempts", 1)
    client = _client_with_ingestor(FakeIngestor())

    fields = {"display_name": "A", "team": "RED", "protocol": "mc"}
    _post_interaction(client, priv, _modal_submit("join:modal", fields, snowflake="1"))
    resp = _post_interaction(
        client, priv,
        _modal_submit("join:modal", {"display_name": "B", "team": "RED", "protocol": "mc"}, snowflake="1"),
    )
    assert "Too many attempts" in resp.json()["data"]["content"]


# ---- /radios --------------------------------------------------------------


def _link_player_to_snowflake(db_path, player_id: int, snowflake: str) -> int:
    account_id = _make_account_with_discord_identity(db_path, snowflake)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE player SET account_id = ? WHERE player_id = ?", (account_id, player_id))
    conn.commit()
    conn.close()
    return account_id


def test_radios_list_shows_add_button_and_remove_select(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    _make_player(db_path, KEY_PLAYER_ID, "Radioed", "RED")
    _link_player_to_snowflake(db_path, KEY_PLAYER_ID, "51515151")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) VALUES ('mc', 'aabbccdd', ?, ?)",
        (KEY_PLAYER_ID, int(time.time())),
    )
    conn.commit()
    conn.close()

    client = _client_with_ingestor(FakeIngestor())
    payload = _command("radios")
    payload["member"] = {"user": {"id": "51515151"}}
    resp = _post_interaction(client, priv, payload)
    body = resp.json()["data"]
    assert body["flags"] == di._FLAG_EPHEMERAL
    assert "aabbccdd" in body["content"]
    components = body["components"]
    add_button = components[0]["components"][0]
    assert add_button["custom_id"] == "radios:add_open"
    select = components[1]["components"][0]
    assert select["custom_id"] == "radios:remove_select"
    assert select["options"][0]["value"] == "mc:aabbccdd"


def test_radios_add_via_modal_normalizes_node_ref(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    _make_player(db_path, KEY_PLAYER_ID, "Radioed", "RED")
    _link_player_to_snowflake(db_path, KEY_PLAYER_ID, "61616161")
    client = _client_with_ingestor(FakeIngestor())

    resp = _post_interaction(
        client, priv,
        _modal_submit(
            "radios:add_submit", {"protocol": "mt", "node_ref": "!AABBCCDD"}, snowflake="61616161",
        ),
    )
    assert resp.status_code == 200, resp.text
    assert "Added" in resp.json()["data"]["content"]

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT node_ref FROM player_node WHERE player_id = ? AND protocol = 'mt'", (KEY_PLAYER_ID,)
    ).fetchone()
    conn.close()
    assert row[0] == "aabbccdd"


def test_radios_remove_requires_confirm(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    _make_player(db_path, KEY_PLAYER_ID, "Radioed", "RED")
    _link_player_to_snowflake(db_path, KEY_PLAYER_ID, "71717171")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) VALUES ('mc', 'aabbccdd', ?, ?)",
        (KEY_PLAYER_ID, int(time.time())),
    )
    conn.commit()
    conn.close()
    client = _client_with_ingestor(FakeIngestor())

    select_resp = _post_interaction(
        client, priv, _component("radios:remove_select", snowflake="71717171", values=["mc:aabbccdd"]),
    )
    assert select_resp.status_code == 200, select_resp.text
    select_body = select_resp.json()
    assert select_body["type"] == di._RESPONSE_UPDATE_MESSAGE
    buttons = select_body["data"]["components"][0]["components"]
    confirm_id = next(b["custom_id"] for b in buttons if b["label"] == "Remove")

    # Still there until confirmed.
    conn = sqlite3.connect(db_path)
    still_there = conn.execute(
        "SELECT 1 FROM player_node WHERE protocol='mc' AND node_ref='aabbccdd'"
    ).fetchone()
    conn.close()
    assert still_there is not None

    confirm_resp = _post_interaction(
        client, priv, _component(confirm_id, snowflake="71717171"),
    )
    assert confirm_resp.json()["type"] == di._RESPONSE_UPDATE_MESSAGE
    assert "Removed" in confirm_resp.json()["data"]["content"]

    conn = sqlite3.connect(db_path)
    gone = conn.execute(
        "SELECT 1 FROM player_node WHERE protocol='mc' AND node_ref='aabbccdd'"
    ).fetchone()
    conn.close()
    assert gone is None


def test_radios_component_from_different_user_is_refused(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    _make_player(db_path, KEY_PLAYER_ID, "Owner", "RED")
    _link_player_to_snowflake(db_path, KEY_PLAYER_ID, "81818181")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) VALUES ('mc', 'aabbccdd', ?, ?)",
        (KEY_PLAYER_ID, int(time.time())),
    )
    conn.commit()
    conn.close()
    client = _client_with_ingestor(FakeIngestor())

    # A DIFFERENT snowflake, with no account of its own, clicks the
    # remove select naming the owner's radio.
    resp = _post_interaction(
        client, priv, _component("radios:remove_select", snowflake="99990000", values=["mc:aabbccdd"]),
    )
    body = resp.json()["data"]
    assert "haven't linked" in body["content"] or "isn't yours" in body["content"]

    conn = sqlite3.connect(db_path)
    still_there = conn.execute(
        "SELECT 1 FROM player_node WHERE protocol='mc' AND node_ref='aabbccdd'"
    ).fetchone()
    conn.close()
    assert still_there is not None


def test_radios_tampered_custom_id_naming_unowned_radio_is_refused(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    _make_player(db_path, KEY_PLAYER_ID, "Owner", "RED")
    _link_player_to_snowflake(db_path, KEY_PLAYER_ID, "82828282")
    _make_player(db_path, KEY_PLAYER_ID + 1, "Other", "BLUE")
    _link_player_to_snowflake(db_path, KEY_PLAYER_ID + 1, "83838383")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) VALUES ('mc', 'deadbeef', ?, ?)",
        (KEY_PLAYER_ID + 1, int(time.time())),
    )
    conn.commit()
    conn.close()
    client = _client_with_ingestor(FakeIngestor())

    # Caller 82828282 owns nothing named 'deadbeef' -- it belongs to a
    # different player. A tampered confirm custom_id naming it anyway
    # must be refused, never deleted.
    resp = _post_interaction(
        client, priv, _component("radios:remove_confirm:mc:deadbeef", snowflake="82828282"),
    )
    assert "isn't yours" in resp.json()["data"]["content"]

    conn = sqlite3.connect(db_path)
    still_there = conn.execute(
        "SELECT 1 FROM player_node WHERE protocol='mc' AND node_ref='deadbeef'"
    ).fetchone()
    conn.close()
    assert still_there is not None


# ---- /setupcheck ------------------------------------------------------


def test_setupcheck_matches_website_helper_content(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    _make_player(db_path, KEY_PLAYER_ID, "Checker", "RED")
    _link_player_to_snowflake(db_path, KEY_PLAYER_ID, "91919191")

    client = _client_with_ingestor(FakeIngestor())
    payload = _command("setupcheck")
    payload["member"] = {"user": {"id": "91919191"}}
    resp = _post_interaction(client, priv, payload)
    content = resp.json()["data"]["content"]

    from app import account_api as account_api_module
    from app.db import connect as app_connect

    conn = app_connect()
    try:
        expected = account_api_module._checkin_health_for_player(conn, KEY_PLAYER_ID, [])
    finally:
        conn.close()

    for protocol, board in expected["boards"].items():
        assert board["summary"] in content


# ---- component/modal responses stay ephemeral and mention-free -----------


def test_component_and_modal_responses_stay_ephemeral(db_path):
    priv, pub_hex = _keypair()
    _enable_slash(db_path, public_key_hex=pub_hex)
    client = _client_with_ingestor(FakeIngestor())

    # The MODAL-opening response (type 9) has no allowed_mentions/flags
    # fields at all -- Discord's own Modal object doesn't carry them
    # (see _modal_response()'s own docstring) -- so only its shape is
    # checked; every OTHER response here (a type-4 message) carries
    # both.
    modal_resp = _post_interaction(client, priv, _command("link"))
    modal_body = modal_resp.json()
    assert modal_body["type"] == di._RESPONSE_MODAL
    assert "allowed_mentions" not in modal_body["data"]

    message_responses = [
        _post_interaction(
            client, priv, _modal_submit("link:modal", {"api_key": "bogus"}, snowflake="1"),
        ),
        _post_interaction(
            client, priv,
            _modal_submit("join:modal", {"display_name": "X", "team": "NOPE", "protocol": "mc"}, snowflake="2"),
        ),
    ]
    for resp in message_responses:
        body = resp.json()
        assert body["type"] in (4, 7)
        data = body["data"]
        assert data["allowed_mentions"] == {"parse": []}
        assert data.get("flags") == di._FLAG_EPHEMERAL
