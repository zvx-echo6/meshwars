"""Tests for the admin-editable Discord announcement config surface:
app/admin_ops.py's GET/POST /api/admin/discord, POST
/api/admin/discord/test, POST /api/admin/discord/outbox/retry, and the
supporting pieces in app/discord_notify.py (load_discord_config(),
seed_discord_config_from_env(), announcements_enabled()) that make the
whole thing DB-backed rather than settings.py-only.

Same "FastAPI-around-one-router, TestClient, session cookie via
app/sessions.create_session" shape tests/test_paint_source_both.py's
own POST /api/admin/paint tests use (group D there) -- a real
file-backed sqlite database, since app/admin_ops.py's routes go through
app/db.py's connect()/WriteSession, a fresh connection per call, so
":memory:" would not share data between them.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.db as db
from app import admin_ops, discord_bot, discord_notify
from app.admin_ops import router as admin_router
from app.auth import http_exception_as_error_body
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.sessions import SESSION_COOKIE_NAME, create_session

NOW = int(time.time())
_TEST_WEBHOOK = "https://discord.test/api/webhooks/999/supersecrettoken"


def _init_schema(conn_or_path) -> None:
    """Applies SCHEMA + MIGRATIONS -- accepts either a path (opens its
    own connection) or an already-open connection, matching the two
    shapes this file needs it for (a real db_path fixture, and the
    in-memory seed tests below)."""
    owns_conn = isinstance(conn_or_path, str)
    conn = sqlite3.connect(conn_or_path) if owns_conn else conn_or_path
    conn.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                continue
            raise
    conn.commit()
    if owns_conn:
        conn.close()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "game.db")
    _init_schema(path)
    monkeypatch.setattr(db.settings, "db_path", path)
    return path


def _make_account(db_path, *, role: str | None = "admin", with_totp: bool = True) -> int:
    """A real account row, optionally holding `role` and an ACTIVATED
    TOTP secret -- app/admin_api.py's _role_guard() requires both an
    admin/operator role AND an activated TOTP row to use any
    /api/admin/* route at all (see that function's own docstring); a
    dummy secret is fine, nothing here decrypts it, same pattern
    tests/test_paint_source_both.py's own _make_admin_client uses.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.execute("INSERT INTO account(created_at, role) VALUES (?, ?)", (NOW, role))
    account_id = cur.lastrowid
    if with_totp:
        conn.execute(
            "INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at) "
            "VALUES (?, 'unused', ?, ?)",
            (account_id, NOW, NOW),
        )
    conn.commit()
    conn.close()
    return account_id


def _client_for(account_id: int | None) -> TestClient:
    app = FastAPI()
    app.include_router(admin_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    client = TestClient(app)
    if account_id is not None:
        raw_token = asyncio.run(create_session(account_id, device_label=None))
        client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return client


def _configure_discord(db_path, **overrides) -> None:
    """Writes straight to discord_config -- the DB IS the config now,
    same as tests/test_paint_source_both.py's _set_paint_source() for
    freqmapper_config."""
    cfg = {
        "enabled": 1,
        "webhook_url": _TEST_WEBHOOK,
        "username": "",
        "team_emoji": "",
        "announce_month_honors": 1,
    }
    cfg.update(overrides)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE discord_config SET enabled = :enabled, webhook_url = :webhook_url, "
        " username = :username, team_emoji = :team_emoji, "
        " announce_month_honors = :announce_month_honors WHERE id = 1",
        cfg,
    )
    conn.commit()
    conn.close()


def _discord_row(db_path) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM discord_config WHERE id = 1").fetchone()
    conn.close()
    return row


# ---- GET /api/admin/discord never returns the webhook URL ---------------


def test_get_discord_never_returns_webhook_url(db_path):
    account_id = _make_account(db_path)
    _configure_discord(db_path)
    client = _client_for(account_id)

    resp = client.get("/api/admin/discord")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "webhook_url" not in body["config"]
    assert "supersecrettoken" not in resp.text
    assert body["config"]["webhook_set"] is True
    # Last 4 characters only -- enough to recognize, never enough to use.
    assert body["config"]["webhook_hint"] == "oken"


def test_get_discord_reports_webhook_not_set_when_blank(db_path):
    account_id = _make_account(db_path)
    # discord_config's own bare column defaults -- webhook_url = ''.
    client = _client_for(account_id)

    resp = client.get("/api/admin/discord")
    assert resp.status_code == 200
    assert resp.json()["config"]["webhook_set"] is False
    assert resp.json()["config"]["webhook_hint"] == ""


def test_get_discord_reports_outbox_health(db_path):
    account_id = _make_account(db_path)
    _configure_discord(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO discord_outbox(kind, key, payload, created_at, posted_at) "
        "VALUES ('month_honors', '2026-08:mc', '{}', ?, ?)", (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO discord_outbox(kind, key, payload, created_at, attempts, last_error) "
        "VALUES ('month_honors', '2026-07:mc', '{}', ?, 2, 'HTTP 500')", (NOW,),
    )
    conn.commit()
    conn.close()
    client = _client_for(account_id)

    resp = client.get("/api/admin/discord")
    assert resp.status_code == 200
    outbox = resp.json()["outbox"]
    assert outbox["posted"] == 1
    assert outbox["pending"] == 1
    assert outbox["failed"] == 1
    assert len(outbox["recent"]) == 2
    failed_row = next(r for r in outbox["recent"] if r["key"] == "2026-07:mc")
    assert failed_row["last_error"] == "HTTP 500"


# ---- POST leaves the stored webhook unchanged unless told otherwise -----


def test_post_discord_without_webhook_url_key_leaves_stored_value_unchanged(db_path):
    account_id = _make_account(db_path)
    _configure_discord(db_path, webhook_url="https://discord.test/api/webhooks/1/original")
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord", json={
        "enabled": True, "username": "NewName", "team_emoji": "",
        "announce_month_honors": True,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["webhook_set"] is True
    assert _discord_row(db_path)["webhook_url"] == "https://discord.test/api/webhooks/1/original"
    assert _discord_row(db_path)["username"] == "NewName"


def test_post_discord_with_empty_string_webhook_url_leaves_stored_value_unchanged(db_path):
    account_id = _make_account(db_path)
    _configure_discord(db_path, webhook_url="https://discord.test/api/webhooks/1/original")
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord", json={
        "enabled": True, "webhook_url": "", "username": "", "team_emoji": "",
        "announce_month_honors": True,
    })
    assert resp.status_code == 200, resp.text
    assert _discord_row(db_path)["webhook_url"] == "https://discord.test/api/webhooks/1/original"


def test_post_discord_with_new_webhook_url_replaces_stored_value(db_path):
    account_id = _make_account(db_path)
    _configure_discord(db_path, webhook_url="https://discord.test/api/webhooks/1/original")
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord", json={
        "enabled": True, "webhook_url": "https://discord.test/api/webhooks/1/replaced",
        "username": "", "team_emoji": "", "announce_month_honors": True,
    })
    assert resp.status_code == 200, resp.text
    assert _discord_row(db_path)["webhook_url"] == "https://discord.test/api/webhooks/1/replaced"


def test_post_discord_clear_webhook_true_clears_it(db_path):
    account_id = _make_account(db_path)
    _configure_discord(db_path)
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord", json={
        "enabled": True, "username": "", "team_emoji": "",
        "announce_month_honors": True, "clear_webhook": True,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["webhook_set"] is False
    assert _discord_row(db_path)["webhook_url"] == ""


def test_post_discord_round_trips_other_fields(db_path):
    account_id = _make_account(db_path)
    _configure_discord(db_path)
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord", json={
        "enabled": False, "username": "MyBot",
        "team_emoji": "RED=<:mw_red:1>", "announce_month_honors": False,
    })
    assert resp.status_code == 200, resp.text
    cfg = resp.json()["config"]
    assert cfg["enabled"] is False
    assert cfg["username"] == "MyBot"
    assert cfg["team_emoji"] == "RED=<:mw_red:1>"
    assert cfg["announce_month_honors"] is False


def test_get_discord_reports_the_two_new_announce_toggles(db_path):
    """discord_config's own CREATE TABLE/MIGRATIONS default both
    toggles to 1 (on) -- an operator who never visits this route yet
    still gets both announcement kinds. (announce_place_activation is
    NOT one of the two any more -- its own per-event kind was retired
    2026-09-16 in favour of announce_weekly_recap, and GET
    /api/admin/discord no longer even selects the now-inert column --
    see app/discord_notify.py's load_discord_config().)"""
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    resp = client.get("/api/admin/discord")
    assert resp.status_code == 200, resp.text
    cfg = resp.json()["config"]
    assert cfg["announce_season_close"] is True
    assert cfg["announce_weekly_recap"] is True
    assert "announce_place_activation" not in cfg


def test_post_discord_round_trips_season_close_and_weekly_recap_toggles(db_path):
    account_id = _make_account(db_path)
    _configure_discord(db_path)
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord", json={
        "enabled": True, "username": "", "team_emoji": "",
        "announce_month_honors": True,
        "announce_season_close": False,
        "announce_weekly_recap": False,
    })
    assert resp.status_code == 200, resp.text
    cfg = resp.json()["config"]
    assert cfg["announce_season_close"] is False
    assert cfg["announce_weekly_recap"] is False

    resp = client.post("/api/admin/discord", json={
        "enabled": True, "username": "", "team_emoji": "",
        "announce_month_honors": True,
        "announce_season_close": True,
        "announce_weekly_recap": True,
    })
    assert resp.status_code == 200, resp.text
    cfg = resp.json()["config"]
    assert cfg["announce_season_close"] is True
    assert cfg["announce_weekly_recap"] is True


def test_post_discord_season_close_toggle_does_not_affect_weekly_recap(db_path):
    """The two toggles are independent -- flipping one off must leave
    the other exactly where it was."""
    account_id = _make_account(db_path)
    _configure_discord(db_path)
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord", json={
        "enabled": True, "username": "", "team_emoji": "",
        "announce_month_honors": True,
        "announce_season_close": False,
        "announce_weekly_recap": True,
    })
    assert resp.status_code == 200, resp.text
    cfg = resp.json()["config"]
    assert cfg["announce_season_close"] is False
    assert cfg["announce_weekly_recap"] is True


def test_post_discord_no_longer_accepts_announce_place_activation(db_path):
    """The removed toggle is silently ignored, not an error -- an old
    cached admin page, or a stale client, that still submits it must
    not 400; the field is simply not read (app/admin_ops.py's
    admin_discord_update() no longer parses it at all)."""
    account_id = _make_account(db_path)
    _configure_discord(db_path)
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord", json={
        "enabled": True, "username": "", "team_emoji": "",
        "announce_month_honors": True,
        "announce_season_close": True,
        "announce_weekly_recap": True,
        "announce_place_activation": True,
    })
    assert resp.status_code == 200, resp.text
    assert "announce_place_activation" not in resp.json()["config"]


def test_get_discord_reports_the_net_wrapup_toggle(db_path):
    """discord_config's own CREATE TABLE/MIGRATIONS default this toggle
    to 1 (on) -- an operator who never visits this route yet still gets
    per-net wrap-ups."""
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    resp = client.get("/api/admin/discord")
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["announce_net_wrapup"] is True


def test_post_discord_round_trips_net_wrapup_toggle(db_path):
    account_id = _make_account(db_path)
    _configure_discord(db_path)
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord", json={
        "enabled": True, "username": "", "team_emoji": "",
        "announce_month_honors": True, "announce_season_close": True,
        "announce_weekly_recap": True, "announce_net_wrapup": False,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["announce_net_wrapup"] is False

    resp = client.post("/api/admin/discord", json={
        "enabled": True, "username": "", "team_emoji": "",
        "announce_month_honors": True, "announce_season_close": True,
        "announce_weekly_recap": True, "announce_net_wrapup": True,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["announce_net_wrapup"] is True


def test_post_discord_net_wrapup_toggle_off_suppresses_only_net_wrapups(db_path):
    """The toggle is independent -- flipping it off must leave
    announce_season_close/announce_weekly_recap exactly where they
    were."""
    account_id = _make_account(db_path)
    _configure_discord(db_path)
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord", json={
        "enabled": True, "username": "", "team_emoji": "",
        "announce_month_honors": True, "announce_season_close": True,
        "announce_weekly_recap": True, "announce_net_wrapup": False,
    })
    assert resp.status_code == 200, resp.text
    cfg = resp.json()["config"]
    assert cfg["announce_net_wrapup"] is False
    assert cfg["announce_season_close"] is True
    assert cfg["announce_weekly_recap"] is True


# ---- announcements_enabled: both gates required --------------------------


def test_announcements_enabled_false_when_disabled_even_with_webhook_set():
    assert discord_notify.announcements_enabled(
        {"enabled": False, "webhook_url": _TEST_WEBHOOK}
    ) is False


def test_announcements_enabled_false_when_enabled_but_webhook_empty():
    assert discord_notify.announcements_enabled(
        {"enabled": True, "webhook_url": ""}
    ) is False


def test_announcements_enabled_true_when_both_set():
    assert discord_notify.announcements_enabled(
        {"enabled": True, "webhook_url": _TEST_WEBHOOK}
    ) is True


# ---- fresh install seeds discord_config from settings --------------------


def test_seed_discord_config_from_env_seeds_fresh_row(monkeypatch):
    monkeypatch.setattr(settings, "discord_webhook_announcements", _TEST_WEBHOOK)
    monkeypatch.setattr(settings, "discord_webhook_username", "SeedBot")
    monkeypatch.setattr(settings, "discord_team_emoji", "RED=<:mw_red:1>")

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _init_schema(conn)

    discord_notify.seed_discord_config_from_env(conn)

    row = conn.execute(
        "SELECT enabled, webhook_url, username, team_emoji, updated_at "
        "  FROM discord_config WHERE id = 1"
    ).fetchone()
    conn.close()
    assert row["enabled"] == 1
    assert row["webhook_url"] == _TEST_WEBHOOK
    assert row["username"] == "SeedBot"
    assert row["team_emoji"] == "RED=<:mw_red:1>"
    assert row["updated_at"] != 0


def test_seed_discord_config_from_env_disabled_when_no_webhook(monkeypatch):
    monkeypatch.setattr(settings, "discord_webhook_announcements", "")

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _init_schema(conn)

    discord_notify.seed_discord_config_from_env(conn)

    row = conn.execute("SELECT enabled, webhook_url FROM discord_config WHERE id = 1").fetchone()
    conn.close()
    assert row["enabled"] == 0
    assert row["webhook_url"] == ""


def test_seed_discord_config_from_env_never_reseeds_after_an_edit(monkeypatch):
    """Once updated_at is non-zero (an operator has saved through
    POST /api/admin/discord, or a previous boot already seeded it), a
    later boot must never silently overwrite that edit."""
    monkeypatch.setattr(settings, "discord_webhook_announcements", _TEST_WEBHOOK)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _init_schema(conn)
    conn.execute(
        "UPDATE discord_config SET webhook_url = 'https://operator.example/hook', "
        " updated_at = 12345 WHERE id = 1"
    )

    discord_notify.seed_discord_config_from_env(conn)

    row = conn.execute("SELECT webhook_url, updated_at FROM discord_config WHERE id = 1").fetchone()
    conn.close()
    assert row["webhook_url"] == "https://operator.example/hook"
    assert row["updated_at"] == 12345


# ---- discord_channel routing table (Piece 1 admin surface) --------------


def _channel_row(db_path, kind: str) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM discord_channel WHERE kind = ?", (kind,)).fetchone()
    conn.close()
    return row


def test_get_discord_never_returns_full_channel_webhook_url(db_path):
    """A per-kind route's webhook is exactly as much a secret as the
    default one -- GET /api/admin/discord must never leak the real URL
    for ANY row in `channels`, only webhook_set/webhook_hint."""
    account_id = _make_account(db_path)
    _configure_discord(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO discord_channel(kind, webhook_url, enabled, updated_at) "
        "VALUES ('month_honors', 'https://discord.test/api/webhooks/1/reallysecrettoken', 1, ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO discord_channel(kind, webhook_url, enabled, updated_at) "
        "VALUES ('test', 'https://discord.test/api/webhooks/2/anothersecret', 0, ?)",
        (NOW,),
    )
    conn.commit()
    conn.close()
    client = _client_for(account_id)

    resp = client.get("/api/admin/discord")
    assert resp.status_code == 200, resp.text
    assert "reallysecrettoken" not in resp.text
    assert "anothersecret" not in resp.text
    channels = {c["kind"]: c for c in resp.json()["channels"]}
    assert set(channels) == {"month_honors", "test"}
    assert "webhook_url" not in channels["month_honors"]
    assert "webhook_url" not in channels["test"]
    assert channels["month_honors"]["webhook_set"] is True
    assert channels["month_honors"]["webhook_hint"] == "oken"
    assert channels["test"]["enabled"] is False


def test_post_discord_channel_creates_new_route(db_path):
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord/channel", json={
        "kind": "month_honors",
        "webhook_url": "https://discord.test/api/webhooks/3/brandnew",
        "enabled": True,
    })
    assert resp.status_code == 200, resp.text
    assert "brandnew" not in resp.text
    assert resp.json()["channel"]["webhook_set"] is True
    row = _channel_row(db_path, "month_honors")
    assert row["webhook_url"] == "https://discord.test/api/webhooks/3/brandnew"
    assert row["enabled"] == 1


def test_post_discord_channel_without_webhook_url_leaves_stored_value_unchanged(db_path):
    account_id = _make_account(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO discord_channel(kind, webhook_url, enabled, updated_at) "
        "VALUES ('month_honors', 'https://discord.test/api/webhooks/1/original', 1, ?)",
        (NOW,),
    )
    conn.commit()
    conn.close()
    client = _client_for(account_id)

    # Only toggling `enabled` -- no webhook_url in the body at all.
    resp = client.post("/api/admin/discord/channel", json={"kind": "month_honors", "enabled": False})
    assert resp.status_code == 200, resp.text
    row = _channel_row(db_path, "month_honors")
    assert row["webhook_url"] == "https://discord.test/api/webhooks/1/original"
    assert row["enabled"] == 0


def test_post_discord_channel_empty_string_webhook_url_leaves_stored_value_unchanged(db_path):
    account_id = _make_account(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO discord_channel(kind, webhook_url, enabled, updated_at) "
        "VALUES ('month_honors', 'https://discord.test/api/webhooks/1/original', 1, ?)",
        (NOW,),
    )
    conn.commit()
    conn.close()
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord/channel", json={
        "kind": "month_honors", "webhook_url": "", "enabled": True,
    })
    assert resp.status_code == 200, resp.text
    row = _channel_row(db_path, "month_honors")
    assert row["webhook_url"] == "https://discord.test/api/webhooks/1/original"


def test_post_discord_channel_clear_webhook_true_clears_it(db_path):
    account_id = _make_account(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO discord_channel(kind, webhook_url, enabled, updated_at) "
        "VALUES ('month_honors', 'https://discord.test/api/webhooks/1/original', 1, ?)",
        (NOW,),
    )
    conn.commit()
    conn.close()
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord/channel", json={
        "kind": "month_honors", "clear_webhook": True,
    })
    assert resp.status_code == 200, resp.text
    row = _channel_row(db_path, "month_honors")
    assert row["webhook_url"] == ""


def test_post_discord_channel_requires_kind(db_path):
    account_id = _make_account(db_path)
    client = _client_for(account_id)
    resp = client.post("/api/admin/discord/channel", json={"webhook_url": "https://x"})
    assert resp.status_code == 400


def test_post_discord_channel_requires_role_signed_in_but_no_role(db_path):
    _make_account(db_path, role="admin")
    account_id = _make_account(db_path, role=None, with_totp=False)
    client = _client_for(account_id)
    resp = client.post("/api/admin/discord/channel", json={"kind": "month_honors"})
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


# ---- POST /api/admin/discord/test ----------------------------------------


def test_post_discord_test_enqueues_row(db_path):
    account_id = _make_account(db_path)
    _configure_discord(db_path)
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord/test", json={})
    assert resp.status_code == 200, resp.text
    key = resp.json()["key"]

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT key FROM discord_outbox WHERE kind = 'test'").fetchall()
    conn.close()
    assert [r[0] for r in rows] == [key]


def test_post_discord_test_twice_enqueues_two_separate_rows(db_path):
    """Each call must get its own unique key -- discord_outbox's
    UNIQUE(kind, key) exactly-once index must never suppress a second,
    deliberate test click as if it were a duplicate freeze."""
    account_id = _make_account(db_path)
    _configure_discord(db_path)
    client = _client_for(account_id)

    resp1 = client.post("/api/admin/discord/test", json={})
    resp2 = client.post("/api/admin/discord/test", json={})
    assert resp1.status_code == 200, resp1.text
    assert resp2.status_code == 200, resp2.text
    key1, key2 = resp1.json()["key"], resp2.json()["key"]
    assert key1 != key2

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT key FROM discord_outbox WHERE kind = 'test'").fetchall()
    conn.close()
    assert sorted(r[0] for r in rows) == sorted([key1, key2])


def test_post_discord_test_refuses_when_not_enabled(db_path):
    account_id = _make_account(db_path)
    # discord_config's own bare defaults -- enabled=0, webhook_url=''.
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord/test", json={})
    assert resp.status_code == 400
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT count(*) FROM discord_outbox").fetchone()[0]
    conn.close()
    assert count == 0


# ---- POST /api/admin/discord/outbox/retry --------------------------------


def test_post_discord_outbox_retry_resets_attempts_and_error(db_path):
    account_id = _make_account(db_path)
    _configure_discord(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO discord_outbox(kind, key, payload, created_at, attempts, last_error) "
        "VALUES ('month_honors', '2026-08:mc', '{}', ?, 3, 'HTTP 500: boom')", (NOW,),
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord/outbox/retry", json={"id": row_id})
    assert resp.status_code == 200, resp.text

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT attempts, last_error, posted_at FROM discord_outbox WHERE id = ?", (row_id,)
    ).fetchone()
    conn.close()
    assert row[0] == 0
    assert row[1] is None
    assert row[2] is None


def test_post_discord_outbox_retry_unknown_id_404s(db_path):
    account_id = _make_account(db_path)
    client = _client_for(account_id)
    resp = client.post("/api/admin/discord/outbox/retry", json={"id": 999999})
    assert resp.status_code == 404


# ---- both routes require the admin role -----------------------------------


def test_get_discord_requires_role_unauthenticated(db_path):
    # An admin account exists (so _admin_surface_enabled() is true and
    # the guard's own 404-vs-401 distinction resolves to 401), but this
    # client sends no session cookie at all.
    _make_account(db_path)
    client = _client_for(None)
    resp = client.get("/api/admin/discord")
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_post_discord_requires_role_unauthenticated(db_path):
    _make_account(db_path)
    client = _client_for(None)
    resp = client.post("/api/admin/discord", json={"enabled": True})
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_get_discord_requires_role_signed_in_but_no_role(db_path):
    # An admin account has to exist SOMEWHERE for _admin_surface_enabled()
    # to consider the surface reachable at all (otherwise every route
    # 404s regardless of role, proven separately by
    # test_admin_ops_checkin.py) -- this test's own account, signed in
    # below, simply holds no role, the same rejection every other
    # /api/admin/* route gives that case (see app/admin_api.py's
    # _role_guard() docstring: never distinguishable from "no session
    # at all").
    _make_account(db_path, role="admin")
    account_id = _make_account(db_path, role=None, with_totp=False)
    client = _client_for(account_id)
    resp = client.get("/api/admin/discord")
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


def test_post_discord_test_requires_role_signed_in_but_no_role(db_path):
    _make_account(db_path, role="admin")
    account_id = _make_account(db_path, role=None, with_totp=False)
    client = _client_for(account_id)
    resp = client.post("/api/admin/discord/test", json={})
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


# ---- Discord role sync (app/discord_bot.py) admin surface ----------------
#
# The bot token itself (settings.discord_bot_token) never appears
# anywhere this section touches -- GET /api/admin/discord only ever
# reports whether one is configured (`bot_token_set`), never the value.
# ensure_team_roles()/reconcile_all() are monkeypatched to canned async
# stubs throughout: their own real behavior (the guild-role diffing,
# the member role diff) is tests/test_discord_bot.py's job -- this file
# only proves the ROUTES wire up correctly (guard, logging, response
# shape, the 400-on-not-configured precondition).

_TEST_BOT_TOKEN = "totally-secret-bot-token-must-never-leak"


@pytest.fixture(autouse=True)
def _reset_reconcile_state(monkeypatch):
    monkeypatch.setattr(discord_bot, "_last_reconcile_gate_at", 0.0)
    monkeypatch.setattr(discord_bot, "_last_reconcile_result",
                         {"ok": False, "at": 0, "checked": 0, "changed": 0})


def test_get_discord_reports_bot_token_set(db_path, monkeypatch):
    monkeypatch.setattr(settings, "discord_bot_token", _TEST_BOT_TOKEN)
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    resp = client.get("/api/admin/discord")
    assert resp.status_code == 200
    assert resp.json()["config"]["bot_token_set"] is True
    assert _TEST_BOT_TOKEN not in resp.text


def test_get_discord_reports_bot_token_not_set(db_path, monkeypatch):
    monkeypatch.setattr(settings, "discord_bot_token", "")
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    resp = client.get("/api/admin/discord")
    assert resp.status_code == 200
    assert resp.json()["config"]["bot_token_set"] is False


def test_get_discord_lists_team_roles(db_path):
    account_id = _make_account(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO discord_team_role(team, role_id, updated_at) VALUES ('RED', 'role-red', ?)",
        (NOW,),
    )
    conn.commit()
    conn.close()
    client = _client_for(account_id)

    resp = client.get("/api/admin/discord")
    assert resp.status_code == 200
    team_roles = resp.json()["team_roles"]
    # channel_id is None until ensure_team_channels() has run for RED --
    # see that column's own comment in app/db.py.
    assert {"team": "RED", "role_id": "role-red", "channel_id": None, "updated_at": NOW} in team_roles


def test_get_discord_includes_last_reconcile(db_path, monkeypatch):
    monkeypatch.setattr(discord_bot, "_last_reconcile_result",
                         {"ok": True, "at": NOW, "checked": 3, "changed": 1})
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    resp = client.get("/api/admin/discord")
    assert resp.status_code == 200
    assert resp.json()["last_reconcile"] == {"ok": True, "at": NOW, "checked": 3, "changed": 1}


def test_post_discord_saves_roles_enabled_and_guild_id(db_path):
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord", json={
        "enabled": False, "username": "", "team_emoji": "",
        "announce_month_honors": True, "announce_season_close": True,
        "announce_weekly_recap": True, "announce_net_wrapup": True,
        "roles_enabled": True, "guild_id": "123456789",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()["config"]
    assert body["roles_enabled"] is True
    assert body["guild_id"] == "123456789"
    row = _discord_row(db_path)
    assert row["roles_enabled"] == 1
    assert row["guild_id"] == "123456789"


def test_post_discord_roles_ensure_calls_ensure_team_roles(db_path, monkeypatch):
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    async def fake_ensure(*, http_client=None):
        return {"ok": True, "created": ["RED"], "recreated": [], "reused": ["GREEN"], "unchanged": []}

    monkeypatch.setattr(admin_ops.discord_bot, "ensure_team_roles", fake_ensure)

    resp = client.post("/api/admin/discord/roles/ensure", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == ["RED"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    action = conn.execute(
        "SELECT action FROM admin_action_log WHERE action = 'discord_roles_ensure'"
    ).fetchone()
    conn.close()
    assert action is not None


def test_post_discord_roles_ensure_400_when_not_configured(db_path, monkeypatch):
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    async def fake_ensure(*, http_client=None):
        return {"ok": False, "reason": "roles sync disabled or not fully configured"}

    monkeypatch.setattr(admin_ops.discord_bot, "ensure_team_roles", fake_ensure)

    resp = client.post("/api/admin/discord/roles/ensure", json={})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_post_discord_roles_reconcile_calls_reconcile_all(db_path, monkeypatch):
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    async def fake_reconcile(*, http_client=None):
        return {"ok": True, "at": NOW, "checked": 5, "changed": 2}

    monkeypatch.setattr(admin_ops.discord_bot, "reconcile_all", fake_reconcile)

    resp = client.post("/api/admin/discord/roles/reconcile", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["checked"] == 5
    assert resp.json()["changed"] == 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    action = conn.execute(
        "SELECT detail FROM admin_action_log WHERE action = 'discord_roles_reconcile'"
    ).fetchone()
    conn.close()
    assert action is not None
    assert "checked=5" in action["detail"]


def test_post_discord_roles_reconcile_400_when_not_configured(db_path, monkeypatch):
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    async def fake_reconcile(*, http_client=None):
        return {"ok": False, "reason": "roles sync disabled or not fully configured"}

    monkeypatch.setattr(admin_ops.discord_bot, "reconcile_all", fake_reconcile)

    resp = client.post("/api/admin/discord/roles/reconcile", json={})
    assert resp.status_code == 400


def test_post_discord_roles_ensure_requires_role(db_path):
    _make_account(db_path, role="admin")
    account_id = _make_account(db_path, role=None, with_totp=False)
    client = _client_for(account_id)
    resp = client.post("/api/admin/discord/roles/ensure", json={})
    assert resp.status_code == 401


def test_post_discord_roles_reconcile_requires_role(db_path):
    _make_account(db_path, role="admin")
    account_id = _make_account(db_path, role=None, with_totp=False)
    client = _client_for(account_id)
    resp = client.post("/api/admin/discord/roles/reconcile", json={})
    assert resp.status_code == 401


# ---- POST /api/admin/discord/team-channel ---------------------------------
#
# app/discord_bot.py's ensure_team_channels() refuses to guess when more
# than one channel in the configured category normalizes to the same
# team name (its own `ambiguous` bucket -- see that function's own
# docstring) -- this route is the admin's manual way to resolve one:
# pick a channel id by hand, validated against a fresh (mocked) GET of
# the live guild's channel list. discord_bot._list_guild_channels is
# monkeypatched throughout, same "route wiring only, not Discord's own
# behavior" boundary the roles/ensure and roles/reconcile tests above
# already draw for ensure_team_roles()/reconcile_all().

_TEST_GUILD_ID = "555000111"


def _enable_team_channels_for_admin(db_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "discord_bot_token", _TEST_BOT_TOKEN)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE discord_config SET roles_enabled = 1, guild_id = ?, "
        " team_channels_enabled = 1, team_category_name = 'Teams' WHERE id = 1",
        (_TEST_GUILD_ID,),
    )
    conn.commit()
    conn.close()


def test_post_discord_team_channel_sets_a_valid_text_channel(db_path, monkeypatch):
    _enable_team_channels_for_admin(db_path, monkeypatch)
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    async def fake_list_channels(guild_id, *, http_client=None):
        assert guild_id == _TEST_GUILD_ID
        return [{"id": "red-chan-id", "type": discord_bot._CHANNEL_TYPE_TEXT, "name": "red🟥", "parent_id": "cat-1"}]

    monkeypatch.setattr(admin_ops.discord_bot, "_list_guild_channels", fake_list_channels)

    resp = client.post("/api/admin/discord/team-channel", json={"team": "RED", "channel_id": "red-chan-id"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["team_role"]["channel_id"] == "red-chan-id"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT channel_id FROM discord_team_role WHERE team = 'RED'").fetchone()
    action = conn.execute(
        "SELECT detail FROM admin_action_log WHERE action = 'discord_team_channel_set'"
    ).fetchone()
    conn.close()
    assert row["channel_id"] == "red-chan-id"
    assert action is not None and "team=RED" in action["detail"]


def test_post_discord_team_channel_rejects_unknown_channel_id(db_path, monkeypatch):
    _enable_team_channels_for_admin(db_path, monkeypatch)
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    async def fake_list_channels(guild_id, *, http_client=None):
        return []  # nothing in the guild has this id

    monkeypatch.setattr(admin_ops.discord_bot, "_list_guild_channels", fake_list_channels)

    resp = client.post("/api/admin/discord/team-channel", json={"team": "RED", "channel_id": "no-such-id"})
    assert resp.status_code == 400
    assert "error" in resp.json()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT channel_id FROM discord_team_role WHERE team = 'RED'").fetchone()
    conn.close()
    assert row is None  # nothing was ever written


def test_post_discord_team_channel_rejects_non_text_channel(db_path, monkeypatch):
    _enable_team_channels_for_admin(db_path, monkeypatch)
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    async def fake_list_channels(guild_id, *, http_client=None):
        return [{"id": "cat-1", "type": discord_bot._CHANNEL_TYPE_CATEGORY, "name": "Teams", "parent_id": None}]

    monkeypatch.setattr(admin_ops.discord_bot, "_list_guild_channels", fake_list_channels)

    resp = client.post("/api/admin/discord/team-channel", json={"team": "RED", "channel_id": "cat-1"})
    assert resp.status_code == 400
    assert "not a text channel" in resp.json()["error"]


def test_post_discord_team_channel_clears_with_null(db_path, monkeypatch):
    _enable_team_channels_for_admin(db_path, monkeypatch)
    account_id = _make_account(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO discord_team_role(team, role_id, channel_id, updated_at) VALUES ('RED', 'role-red', 'red-chan-id', ?)",
        (NOW,),
    )
    conn.commit()
    conn.close()
    client = _client_for(account_id)

    def fail_list_channels(*a, **k):
        raise AssertionError("clearing must never need a live guild lookup")

    monkeypatch.setattr(admin_ops.discord_bot, "_list_guild_channels", fail_list_channels)

    resp = client.post("/api/admin/discord/team-channel", json={"team": "RED", "channel_id": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["team_role"]["channel_id"] is None

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT channel_id, role_id FROM discord_team_role WHERE team = 'RED'").fetchone()
    conn.close()
    assert row["channel_id"] is None
    assert row["role_id"] == "role-red"  # untouched


def test_post_discord_team_channel_rejects_unknown_team(db_path, monkeypatch):
    _enable_team_channels_for_admin(db_path, monkeypatch)
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord/team-channel", json={"team": "MAGENTA", "channel_id": None})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_post_discord_team_channel_400_when_not_configured(db_path, monkeypatch):
    # team_channels_enabled left at its column default (0).
    monkeypatch.setattr(settings, "discord_bot_token", _TEST_BOT_TOKEN)
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    def fail_list_channels(*a, **k):
        raise AssertionError("must not call Discord when not configured")

    monkeypatch.setattr(admin_ops.discord_bot, "_list_guild_channels", fail_list_channels)

    resp = client.post("/api/admin/discord/team-channel", json={"team": "RED", "channel_id": "red-chan-id"})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_post_discord_team_channel_requires_role(db_path):
    _make_account(db_path, role="admin")
    account_id = _make_account(db_path, role=None, with_totp=False)
    client = _client_for(account_id)
    resp = client.post("/api/admin/discord/team-channel", json={"team": "RED", "channel_id": None})
    assert resp.status_code == 401


# ---- slash commands (app/discord_interactions.py) ------------------------


def test_post_discord_saves_slash_fields(db_path):
    """slash_enabled, app_id, and public_key are neither of them
    secrets -- see discord_config's own comment in app/db.py -- so they
    are saved the same plain, always-explicit way as guild_id, with no
    "omit to keep current" special case."""
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord", json={
        "enabled": True, "username": "", "team_emoji": "",
        "announce_month_honors": True,
        "slash_enabled": True, "app_id": "123456", "public_key": "deadbeef",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["slash_enabled"] is True
    assert resp.json()["config"]["app_id"] == "123456"
    assert resp.json()["config"]["public_key"] == "deadbeef"

    row = _discord_row(db_path)
    assert row["slash_enabled"] == 1
    assert row["app_id"] == "123456"
    assert row["public_key"] == "deadbeef"


def test_get_discord_returns_slash_fields_unscrubbed(db_path):
    """Unlike webhook_url, neither app_id nor public_key is a secret --
    GET /api/admin/discord returns both plainly, no hint/set-flag
    treatment."""
    account_id = _make_account(db_path)
    _configure_discord(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE discord_config SET slash_enabled = 1, app_id = 'app-1', public_key = 'pub-1' WHERE id = 1"
    )
    conn.commit()
    conn.close()
    client = _client_for(account_id)

    resp = client.get("/api/admin/discord")
    assert resp.status_code == 200
    cfg = resp.json()["config"]
    assert cfg["slash_enabled"] is True
    assert cfg["app_id"] == "app-1"
    assert cfg["public_key"] == "pub-1"


def test_get_discord_shows_interactions_endpoint_url(db_path, monkeypatch):
    account_id = _make_account(db_path)
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://meshwars.example")
    client = _client_for(account_id)

    resp = client.get("/api/admin/discord")
    assert resp.status_code == 200
    assert resp.json()["interactions_endpoint_url"] == "https://meshwars.example/api/discord/interactions"


def test_get_discord_interactions_endpoint_url_blank_when_unconfigured(db_path, monkeypatch):
    account_id = _make_account(db_path)
    monkeypatch.setattr(settings, "oauth_public_base_url", "")
    client = _client_for(account_id)

    resp = client.get("/api/admin/discord")
    assert resp.status_code == 200
    assert resp.json()["interactions_endpoint_url"] == ""


def test_post_discord_slash_register_calls_register_commands(db_path, monkeypatch):
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    async def fake_register(*, http_client=None):
        return {"ok": True, "commands": ["me", "standings", "honors", "nextnet", "player"]}

    monkeypatch.setattr(admin_ops.discord_bot, "register_commands", fake_register)

    resp = client.post("/api/admin/discord/slash/register", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["commands"] == ["me", "standings", "honors", "nextnet", "player"]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    action = conn.execute(
        "SELECT detail FROM admin_action_log WHERE action = 'discord_slash_register'"
    ).fetchone()
    conn.close()
    assert action is not None
    assert "me" in action["detail"]


def test_post_discord_slash_register_400_when_not_configured(db_path, monkeypatch):
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    async def fake_register(*, http_client=None):
        return {"ok": False, "reason": "slash commands are not fully configured"}

    monkeypatch.setattr(admin_ops.discord_bot, "register_commands", fake_register)

    resp = client.post("/api/admin/discord/slash/register", json={})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_post_discord_slash_register_requires_role(db_path):
    _make_account(db_path, role="admin")
    account_id = _make_account(db_path, role=None, with_totp=False)
    client = _client_for(account_id)
    resp = client.post("/api/admin/discord/slash/register", json={})
    assert resp.status_code == 401


# ---- Leaderboard (app/discord_leaderboard.py) ----------------------------
#
# Same shape every other Discord admin surface in this file already
# uses: GET /api/admin/discord returns the three plain, non-secret
# leaderboard_* config fields straight through (no scrubbing needed,
# same reasoning as guild_id) plus a separate `leaderboard` status
# block from app/discord_leaderboard.py's own leaderboard_admin_status();
# POST /api/admin/discord saves the three fields the same always-
# explicit way roles_enabled/guild_id are saved; POST
# /api/admin/discord/leaderboard/run is tested with
# run_leaderboard_pass() itself stubbed out (fake_run below) -- the real
# pass's own HTTP behavior is covered end to end in
# tests/test_discord_leaderboard.py, this file only needs to prove the
# route calls it with force=True and logs the action.


def test_get_discord_reports_leaderboard_defaults_and_not_posted(db_path):
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    resp = client.get("/api/admin/discord")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["config"]["leaderboard_enabled"] is False
    assert body["config"]["leaderboard_interval_seconds"] == 600
    assert body["config"]["leaderboard_top_n"] == 5
    assert body["leaderboard"] == {
        "posted": False, "jump_url": "", "pinned": False, "updated_at": 0,
    }


def test_get_discord_reports_leaderboard_status_when_posted(db_path):
    account_id = _make_account(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE discord_config SET guild_id = '555' WHERE id = 1")
    conn.execute(
        "INSERT INTO discord_pinned_message"
        "  (kind, webhook_id, channel_id, message_id, content_hash, pinned, updated_at) "
        "VALUES ('leaderboard', '1', '2', '3', 'deadbeef', 1, ?)", (NOW,),
    )
    conn.commit()
    conn.close()
    client = _client_for(account_id)

    resp = client.get("/api/admin/discord")
    assert resp.status_code == 200, resp.text
    lb = resp.json()["leaderboard"]
    assert lb["posted"] is True
    assert lb["jump_url"] == "https://discord.com/channels/555/2/3"
    assert lb["pinned"] is True
    assert lb["updated_at"] == NOW


def test_post_discord_saves_leaderboard_fields(db_path):
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord", json={
        "enabled": True, "username": "", "team_emoji": "", "announce_month_honors": True,
        "leaderboard_enabled": True,
        "leaderboard_interval_seconds": 120,
        "leaderboard_top_n": 3,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["leaderboard_enabled"] is True
    assert resp.json()["config"]["leaderboard_interval_seconds"] == 120
    assert resp.json()["config"]["leaderboard_top_n"] == 3
    row = _discord_row(db_path)
    assert bool(row["leaderboard_enabled"]) is True
    assert row["leaderboard_interval_seconds"] == 120
    assert row["leaderboard_top_n"] == 3


def test_post_discord_leaderboard_fields_are_clamped_to_a_floor(db_path):
    """0 or negative values must never reach the database -- see
    admin_discord_update()'s own docstring on why these are clamped
    server-side rather than trusted from the form's client-side `min`."""
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    resp = client.post("/api/admin/discord", json={
        "enabled": True, "username": "", "team_emoji": "", "announce_month_honors": True,
        "leaderboard_enabled": True,
        "leaderboard_interval_seconds": 0,
        "leaderboard_top_n": 0,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["leaderboard_interval_seconds"] == 30
    assert resp.json()["config"]["leaderboard_top_n"] == 1


def test_post_discord_leaderboard_run_calls_pass_with_force_true(db_path, monkeypatch):
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    calls = []

    async def fake_run(*, force=False, http_client=None):
        calls.append(force)
        return {"ok": True, "reason": "posted", "pinned": True}

    monkeypatch.setattr(admin_ops.discord_leaderboard, "run_leaderboard_pass", fake_run)

    resp = client.post("/api/admin/discord/leaderboard/run", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "reason": "posted", "pinned": True}
    assert calls == [True]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    action = conn.execute(
        "SELECT detail FROM admin_action_log WHERE action = 'discord_leaderboard_run'"
    ).fetchone()
    conn.close()
    assert action is not None
    assert "ok=True" in action["detail"]


def test_post_discord_leaderboard_run_returns_ok_false_without_erroring(db_path, monkeypatch):
    """Leaderboard disabled, or no webhook routed, is an ordinary
    outcome of clicking this before the feature is turned on -- never a
    400, see admin_discord_leaderboard_run()'s own docstring."""
    account_id = _make_account(db_path)
    client = _client_for(account_id)

    async def fake_run(*, force=False, http_client=None):
        return {"ok": False, "reason": "leaderboard disabled"}

    monkeypatch.setattr(admin_ops.discord_leaderboard, "run_leaderboard_pass", fake_run)

    resp = client.post("/api/admin/discord/leaderboard/run", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": False, "reason": "leaderboard disabled"}


def test_post_discord_leaderboard_run_requires_role(db_path):
    _make_account(db_path, role="admin")
    account_id = _make_account(db_path, role=None, with_totp=False)
    client = _client_for(account_id)
    resp = client.post("/api/admin/discord/leaderboard/run", json={})
    assert resp.status_code == 401
