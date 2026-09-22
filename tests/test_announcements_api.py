"""Tests for GET /api/v1/announcements (app/public_api.py) -- the one
keyless /api/v1 route. Covers:

- no key required, and a valid key is NOT subject to the tighter anon
  budget (app/public_api.py's _announcements_guard())
- the keyless per-address rate limit trips with a Retry-After header and
  the actual limit numbers in the body (settings.announcements_anon_
  rate_limit_requests/window_seconds)
- the `since` poll cursor and `next_since` advancing, including the
  empty-result case
- `kinds`, `board` (both spellings), and `net_id` filters
- `limit` and `text_budget` clamping rather than erroring
- ETag / If-None-Match -> 304
- ordering (id ASC, oldest first)

Real file-backed sqlite database, same fixture shape and reasoning as
tests/test_privacy_hardening.py: app/db.py's connect()/WriteSession open
a fresh connection per call, so ":memory:" would not share data between
the setup code here and the route code under test. A bare FastAPI app
around app/public_api.py's real router exercises _announcements_guard()
and _cached_announcements_response() end to end over real HTTP, not just
the functions in isolation.
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.public_api as public_api_module
from app.db import MIGRATIONS, SCHEMA
from app.mc_ingest import hash_secret

NOW = int(time.time())


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
    monkeypatch.setattr(public_api_module.settings, "db_path", path)
    # Also patch app.db.settings.db_path -- app/public_api.py's connect()
    # is app.db.connect, which reads settings.db_path off the SAME
    # imported `settings` object app.config.settings is, so one
    # monkeypatch covers both; kept explicit here for clarity/robustness.
    return path


@pytest.fixture
def client(db_path):
    app = FastAPI()
    app.include_router(public_api_module.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limiters_and_cache():
    """Every limiter and cache this route touches is a module-level
    singleton -- see tests/test_privacy_hardening.py's own
    _reset_rate_limiters_and_cache fixture for the identical reasoning.
    Left dirty, one test's hits/cached bytes would bleed into the next.
    """
    public_api_module._key_cache.clear()
    public_api_module._hits.clear()
    public_api_module._announcements_anon_limiter._hits.clear()
    public_api_module._ANNOUNCEMENTS_CACHE.clear()
    yield
    public_api_module._key_cache.clear()
    public_api_module._hits.clear()
    public_api_module._announcements_anon_limiter._hits.clear()
    public_api_module._ANNOUNCEMENTS_CACHE.clear()


# ---- DB setup helpers ------------------------------------------------

def _api_key(path: str, label: str = "test integration") -> str:
    """Insert a valid, unrevoked app/public_api.py key and return the
    raw value -- same helper as tests/test_privacy_hardening.py's."""
    raw_key = "test-key-" + label.replace(" ", "-")
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO api_client(key_hash, label, created_at) VALUES (?,?,?)",
        (hash_secret(raw_key), label, NOW),
    )
    conn.commit()
    conn.close()
    return raw_key


def _content(
    kind: str = "daily_recap",
    key: str = "2026-09-20:mc",
    board: str = "mc",
    net_id: int | None = None,
    headline: str = "RED climbed to 1st place, extending a comfortable lead over BLUE",
    created_at: int = NOW,
) -> dict:
    return {
        "kind": kind, "key": key, "board": board, "net_id": net_id,
        "period_label": "20 Sep", "period_start_ts": 0, "period_end_ts": 1,
        "headline": headline, "sections": [], "url": None, "created_at": created_at,
    }


def _insert_announcement(path: str, content: dict) -> int:
    """INSERT the given Content dict as an `announcement` row, returning
    its new (AUTOINCREMENT) id -- so tests can assert on exact
    since/next_since values without hardcoding ids."""
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO announcement(kind, key, board, net_id, content, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (content["kind"], content["key"], content["board"], content.get("net_id"),
         json.dumps(content), content["created_at"]),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def _insert_net(
    path: str,
    *,
    label: str = "Weekly Net (Freq51 MC)",
    protocol: str = "mc",
    kind: str = "corescope",
    weekday: int = 2,
    start_hour: int = 17,
    end_hour: int = 23,
    timezone: str = "America/Boise",
    enabled: int = 1,
    connector_url: str = "https://SECRET-UPSTREAM.example.test",
    channel: str = "SECRET-CHANNEL-NAME",
    hashtag: str = "#SECRET-HASHTAG",
    broker_username: str = "SECRET-BROKER-USER",
    broker_password: str = "SECRET-BROKER-PASSWORD",
    channel_key: str = "SECRET-CHANNEL-KEY==",
    topic_root: str = "SECRET/TOPIC/ROOT",
) -> int:
    """INSERT a checkin_net row, defaulting every infrastructure/secret
    column to a distinctive SECRET-* marker value, so a test can assert
    none of those markers ever appear in a route's response body."""
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO checkin_net(label, protocol, kind, connector_url, channel, hashtag, "
        " weekday, start_hour, end_hour, timezone, start_date, enabled, created_at, "
        " broker_username, broker_password, channel_key, topic_root) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (label, protocol, kind, connector_url, channel, hashtag,
         weekday, start_hour, end_hour, timezone, "2000-01-01", enabled, NOW,
         broker_username, broker_password, channel_key, topic_root),
    )
    conn.commit()
    net_id = cur.lastrowid
    conn.close()
    return net_id


# ---- no key required ---------------------------------------------------


def test_works_with_no_key(client, db_path):
    _insert_announcement(db_path, _content())
    resp = client.get("/api/v1/announcements")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["announcements"]) == 1
    assert body["announcements"][0]["kind"] == "daily_recap"
    assert body["poll_interval_seconds"] == 900


# ---- anonymous rate limit -----------------------------------------------


def test_seventh_keyless_request_in_window_is_rate_limited(client, db_path, monkeypatch):
    monkeypatch.setattr(public_api_module.settings, "announcements_anon_rate_limit_requests", 6)
    monkeypatch.setattr(public_api_module.settings, "announcements_anon_rate_limit_window_seconds", 3600)

    for _ in range(6):
        resp = client.get("/api/v1/announcements")
        assert resp.status_code == 200

    resp = client.get("/api/v1/announcements")
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") == "3600"
    body = resp.json()
    assert body["error"] == "rate limited"
    assert "6" in body["detail"]
    assert "3600" in body["detail"]


def test_valid_key_is_not_subject_to_the_anon_limit(client, db_path, monkeypatch):
    monkeypatch.setattr(public_api_module.settings, "announcements_anon_rate_limit_requests", 6)
    monkeypatch.setattr(public_api_module.settings, "announcements_anon_rate_limit_window_seconds", 3600)
    raw_key = _api_key(db_path)

    # More than the anon budget (6), all with a valid key -- none of
    # these count against the anon per-address limiter at all.
    for _ in range(10):
        resp = client.get("/api/v1/announcements", headers={"X-API-Key": raw_key})
        assert resp.status_code == 200

    # The anon limiter's own bucket is untouched -- a keyless request
    # right after still gets its full budget.
    for _ in range(6):
        resp = client.get("/api/v1/announcements")
        assert resp.status_code == 200
    resp = client.get("/api/v1/announcements")
    assert resp.status_code == 429


# ---- since / next_since --------------------------------------------------


def test_since_cursor_returns_only_newer_rows(client, db_path):
    id1 = _insert_announcement(db_path, _content(key="2026-09-18:mc", created_at=NOW - 300))
    id2 = _insert_announcement(db_path, _content(key="2026-09-19:mc", created_at=NOW - 200))
    id3 = _insert_announcement(db_path, _content(key="2026-09-20:mc", created_at=NOW - 100))

    resp = client.get(f"/api/v1/announcements?since={id1}")
    body = resp.json()
    ids = [a["id"] for a in body["announcements"]]
    assert ids == [id2, id3]
    assert body["next_since"] == id3


def test_since_cursor_empty_result_keeps_next_since_unchanged(client, db_path):
    id1 = _insert_announcement(db_path, _content())
    resp = client.get(f"/api/v1/announcements?since={id1}")
    body = resp.json()
    assert body["announcements"] == []
    assert body["next_since"] == id1


# ---- filters -------------------------------------------------------------


def test_kinds_filter(client, db_path):
    _insert_announcement(db_path, _content(kind="daily_recap", key="2026-09-20:mc"))
    _insert_announcement(db_path, _content(kind="month_honors", key="2026-08:mc"))

    resp = client.get("/api/v1/announcements?kinds=month_honors")
    body = resp.json()
    assert len(body["announcements"]) == 1
    assert body["announcements"][0]["kind"] == "month_honors"


def test_board_filter_accepts_both_spellings(client, db_path):
    _insert_announcement(db_path, _content(board="mc", key="2026-09-20:mc"))
    _insert_announcement(db_path, _content(board="mt", key="2026-09-20:mt"))

    for spelling in ("mc", "meshcore"):
        resp = client.get(f"/api/v1/announcements?board={spelling}")
        body = resp.json()
        assert len(body["announcements"]) == 1
        assert body["announcements"][0]["board"] == "mc"

    for spelling in ("mt", "meshtastic"):
        resp = client.get(f"/api/v1/announcements?board={spelling}")
        body = resp.json()
        assert len(body["announcements"]) == 1
        assert body["announcements"][0]["board"] == "mt"


def test_net_id_filter(client, db_path):
    _insert_announcement(db_path, _content(kind="net_wrapup", key="1:2026-09-16", net_id=1))
    _insert_announcement(db_path, _content(kind="net_wrapup", key="2:2026-09-16", net_id=2))

    resp = client.get("/api/v1/announcements?net_id=2")
    body = resp.json()
    assert len(body["announcements"]) == 1
    assert body["announcements"][0]["net_id"] == 2


# ---- limit / text_budget clamping ----------------------------------------


def test_limit_clamps_at_100_rather_than_erroring(client, db_path):
    for i in range(105):
        _insert_announcement(db_path, _content(key=f"2026-{i:04d}:mc", created_at=NOW - i))

    resp = client.get("/api/v1/announcements?limit=99999")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["announcements"]) == 100


def test_text_budget_is_honoured_in_bytes(client, db_path):
    _insert_announcement(db_path, _content(
        headline="RED climbed all the way to 1st place, extending a very comfortable lead over BLUE and GREEN"
    ))

    resp = client.get("/api/v1/announcements?text_budget=25")
    body = resp.json()
    assert len(body["announcements"]) == 1
    text = body["announcements"][0]["text"]
    assert len(text.encode("utf-8")) <= 25


# ---- ETag / If-None-Match -------------------------------------------------


def test_if_none_match_returns_304(client, db_path):
    _insert_announcement(db_path, _content())
    first = client.get("/api/v1/announcements")
    assert first.status_code == 200
    etag = first.headers["etag"]

    second = client.get("/api/v1/announcements", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""


# ---- ordering --------------------------------------------------------------


def test_ordering_is_id_ascending(client, db_path):
    id1 = _insert_announcement(db_path, _content(key="2026-09-18:mc", created_at=NOW - 300))
    id2 = _insert_announcement(db_path, _content(key="2026-09-19:mc", created_at=NOW - 200))
    id3 = _insert_announcement(db_path, _content(key="2026-09-20:mc", created_at=NOW - 100))

    resp = client.get("/api/v1/announcements")
    ids = [a["id"] for a in resp.json()["announcements"]]
    assert ids == [id1, id2, id3]


# ---- GET /api/v1/nets -----------------------------------------------------


def test_nets_returns_only_enabled_nets_ordered_by_id(client, db_path):
    id1 = _insert_net(db_path, label="First", enabled=1)
    _insert_net(db_path, label="Disabled", enabled=0)
    id3 = _insert_net(db_path, label="Third", enabled=1)

    resp = client.get("/api/v1/nets")
    assert resp.status_code == 200
    body = resp.json()
    ids = [n["id"] for n in body["nets"]]
    assert ids == [id1, id3]
    assert body["nets"][0]["label"] == "First"
    assert body["nets"][1]["label"] == "Third"


def test_nets_works_with_no_key(client, db_path):
    _insert_net(db_path, label="Weekly Net (Freq51 MC)", protocol="mc",
                weekday=2, start_hour=17, end_hour=23, timezone="America/Boise")

    resp = client.get("/api/v1/nets")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "nets": [{
            "id": body["nets"][0]["id"],
            "label": "Weekly Net (Freq51 MC)",
            "board": "mc",
            "weekday": 2,
            "start_hour": 17,
            "end_hour": 23,
            "timezone": "America/Boise",
        }]
    }


def test_nets_never_leaks_secret_or_upstream_infrastructure_columns(client, db_path):
    _insert_net(
        db_path,
        connector_url="https://SECRET-UPSTREAM.example.test",
        channel="SECRET-CHANNEL-NAME",
        hashtag="#SECRET-HASHTAG",
        broker_username="SECRET-BROKER-USER",
        broker_password="SECRET-BROKER-PASSWORD",
        channel_key="SECRET-CHANNEL-KEY==",
        topic_root="SECRET/TOPIC/ROOT",
    )

    resp = client.get("/api/v1/nets")
    assert resp.status_code == 200
    raw = resp.text

    forbidden_values = [
        "SECRET-UPSTREAM.example.test",
        "SECRET-CHANNEL-NAME",
        "SECRET-HASHTAG",
        "SECRET-BROKER-USER",
        "SECRET-BROKER-PASSWORD",
        "SECRET-CHANNEL-KEY",
        "SECRET/TOPIC/ROOT",
    ]
    for value in forbidden_values:
        assert value not in raw

    forbidden_keys = [
        "connector_url", "broker_username", "broker_password",
        "channel_key", "topic_root", "channel", "hashtag",
    ]
    body = resp.json()
    net = body["nets"][0]
    for key in forbidden_keys:
        assert key not in net


def test_nets_shares_keyless_rate_limit_with_announcements(client, db_path, monkeypatch):
    monkeypatch.setattr(public_api_module.settings, "announcements_anon_rate_limit_requests", 6)
    monkeypatch.setattr(public_api_module.settings, "announcements_anon_rate_limit_window_seconds", 3600)
    _insert_net(db_path)

    # Split the shared budget across both routes -- it is one bucket
    # per address, not one per route.
    for _ in range(3):
        assert client.get("/api/v1/nets").status_code == 200
    for _ in range(3):
        assert client.get("/api/v1/announcements").status_code == 200

    resp = client.get("/api/v1/nets")
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") == "3600"


def test_nets_if_none_match_returns_304(client, db_path):
    _insert_net(db_path)
    first = client.get("/api/v1/nets")
    assert first.status_code == 200
    etag = first.headers["etag"]

    second = client.get("/api/v1/nets", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""
