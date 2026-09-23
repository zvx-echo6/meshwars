"""Tests for durable per-batch request identity capture (app/mc_ingest.py's
McIngestor.record_ingest_identity(), app/db.py's mc_ingest_request_log,
settings.mc_ingest_identity_enabled/mc_ingest_identity_retention_days/
mc_ingest_salt).

PRIVACY-SAFE BY DESIGN -- this rebuilds branch feat/ingest-identity-capture
(commit 45eadb2), which stored the raw source_ip and raw user_agent
verbatim. A privacy review rejected that shape outright: frontend/
privacy.html tells players "[MeshWars] does not store your IP address:
that column has been dropped from the database entirely," and
app/db.py's _migrate_session_privacy() already physically dropped
account_session's own `ip` column for the identical reason. This file
tests the REPLACEMENT shape instead: mc_ingest_request_log never holds
a raw IP or raw User-Agent, in any column, at any point -- only a
salted one-way ip_hash (app/mc_ingest.py's _hash_source_ip(), same
construction app/traffic.py's own _hash_visitor() uses), a coarse
ip_class ('datacenter'/'unknown', app/ip_class.py, computed in-process
against a bundled hosting-provider prefix list, never a network call),
and a coarse client_family (app/client_family.py, derived from the
User-Agent but never storing it).

Most HTTP-level tests drive the real POST /api/mc/ingest route through
fastapi.testclient.TestClient (same shape
tests/test_mc_ingest_durable_queue.py already uses); the ones that have
to prove get_client_ip() is actually resolving a TRUSTED proxy's
X-Forwarded-For build a raw ASGI Request by hand and call the route
function directly, same technique tests/test_client_ip.py already uses
for exactly this reason (TestClient's own hardcoded "testclient" peer is
not a real IP address, so it can never itself be a trusted_proxies
entry). The retention/disable tests drive McIngestor's own methods
directly, same style as tests/test_mc_ingest_plausibility_guards.py.
"""
from __future__ import annotations

import asyncio
import json as json_module
import logging
import sqlite3
import time

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

import app.api as api_module
import app.mc_ingest as mc_ingest_module
from app.auth import http_exception_as_error_body
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.mc_ingest import McIngestor, _hash_source_ip, hash_secret

NOW = int(time.time())
PROTOCOL = "mc"

# Well within the default play area (settings.play_area_*).
LAT, LON = 43.0, -116.0

# TestClient's own fixed peer address (starlette's default ASGI
# transport) -- verified empirically, not documented API, but stable
# across the fastapi/starlette pins this repo uses.
TESTCLIENT_PEER = "testclient"

# A distinctive, RFC 5737 documentation-range address ("TEST-NET-3") --
# guaranteed never publicly routed, so this is not a real caller's
# address at any point, just a fixture value for the "never persisted"
# tests below.
UNKNOWN_ADDR = "203.0.113.42"

# An address inside app/ip_class.py's bundled OVH prefix (51.68.0.0/16)
# -- a large public hosting-provider range, used here only to prove the
# classifier's own logic wires correctly end to end; not any specific
# machine's real address.
DATACENTER_ADDR = "51.68.1.1"


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
    path = str(tmp_path / "game.db")
    _init_schema(path)
    monkeypatch.setattr(settings, "db_path", path)
    return path


@pytest.fixture(autouse=True)
def _reset_ingest_salt_cache():
    """mc_ingest._ingest_salt_cache is a module-level singleton, resolved
    once per process and reused forever -- exactly the point in
    production (see get_or_create_persistent_salt()'s own docstring for
    why), but poison across tests unless cleared: without this,
    whichever test runs first would pin every later test to its own
    db_path's generated (or overridden) salt.
    """
    mc_ingest_module._ingest_salt_cache = None
    yield
    mc_ingest_module._ingest_salt_cache = None


def _seed_player(db_path, player_id=1, team="RED"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?, ?, ?, ?)",
        (player_id, f"player-{player_id}", team, NOW),
    )
    conn.commit()
    conn.close()


def _seed_api_key(db_path, raw_key, player_id):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO api_key(key_hash, player_id, issued_at) VALUES (?, ?, ?)",
        (hash_secret(raw_key), player_id, NOW),
    )
    conn.commit()
    conn.close()


def _ping(ping_type="TX", lat=LAT, lon=LON, ts=NOW, contact="deadbeef"):
    return {
        "type": ping_type,
        "contact": contact,
        "lat": lat,
        "lon": lon,
        "timestamp": ts,
        "heard_repeats": "cafefeed(3.5)",
    }


def _identity_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM mc_ingest_request_log ORDER BY id"
    )]
    conn.close()
    return rows


def _db_file_bytes(db_path) -> bytes:
    with open(db_path, "rb") as f:
        return f.read()


def _client(db_path):
    app = FastAPI()
    app.include_router(api_module.router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    ingestor = McIngestor()
    app.state.mc_ingestor = ingestor
    return TestClient(app), ingestor


def _build_app(db_path):
    app = FastAPI()
    app.include_router(api_module.router)
    ingestor = McIngestor()
    app.state.mc_ingestor = ingestor
    return app, ingestor


def _direct_post_mc_ingest(app, peer_ip: str, headers: dict[str, str], body: dict):
    """Call app/api.py's mc_ingest() route function directly against a
    hand-built ASGI Request whose `client` is a REAL IP address --
    something fastapi.testclient.TestClient itself cannot produce (its
    peer is always the literal, non-IP string "testclient"; see this
    module's own docstring). This is the only way to exercise the
    TRUSTED half of get_client_ip()'s resolution against the real route.
    """
    body_bytes = json_module.dumps(body).encode("utf-8")
    header_list = [
        (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()
    ]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/mc/ingest",
        "query_string": b"",
        "http_version": "1.1",
        "client": (peer_ip, 51234),
        "headers": header_list,
        "app": app,
    }

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    request = Request(scope, receive=receive)
    return asyncio.run(api_module.mc_ingest(request))


# ---------------------------------------------------------------------
# A batch records ip_hash/ip_class/client_family, resolved correctly
# ---------------------------------------------------------------------

def test_batch_records_hash_class_and_family(db_path, monkeypatch):
    """With the peer that actually connected to us (CADDY_ADDR, standing
    in for Caddy) listed in trusted_proxies, the FORWARDED address --
    not the peer's -- must be what ip_hash is computed from. This is
    the load-bearing proof that get_client_ip() is actually wired into
    this route: a bug that hashed request.client.host directly here
    would silently hash the proxy's own address forever, exactly the
    failure mode every prior investigation hit.
    """
    CADDY_ADDR = "10.10.10.2"
    monkeypatch.setattr(settings, "trusted_proxies", CADDY_ADDR)
    monkeypatch.setattr(settings, "mc_ingest_salt", "test-salt")
    _seed_player(db_path, player_id=1)
    _seed_api_key(db_path, "raw-key-1", player_id=1)
    app, _ = _build_app(db_path)

    resp = _direct_post_mc_ingest(
        app, CADDY_ADDR,
        {
            "X-API-Key": "raw-key-1",
            "X-Forwarded-For": DATACENTER_ADDR,
            "User-Agent": "Dart/3.4 (dart:io)",
            "content-type": "application/json",
        },
        {"data": [_ping()]},
    )
    assert resp.status_code == 202

    rows = _identity_rows(db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["player_id"] == 1
    assert row["ip_hash"] == _hash_source_ip("test-salt", DATACENTER_ADDR)
    assert row["ip_hash"] != _hash_source_ip("test-salt", CADDY_ADDR)
    assert row["ip_class"] == "datacenter"
    assert row["client_family"] == "meshmapper-dart"
    assert row["ping_count"] == 1
    assert row["key_hash_prefix"] == hash_secret("raw-key-1")[:8]
    assert len(row["key_hash_prefix"]) == 8


def test_untrusted_peer_ignores_forwarded_header(db_path, monkeypatch):
    """No trusted_proxies configured (the safe default): an
    X-Forwarded-For header from an untrusted peer must be IGNORED, and
    the raw peer address hashed instead -- otherwise any caller could
    simply claim to be any IP address it likes.
    """
    assert settings.trusted_proxies == ""  # safe default, nothing overridden
    monkeypatch.setattr(settings, "mc_ingest_salt", "test-salt")
    _seed_player(db_path, player_id=1)
    _seed_api_key(db_path, "raw-key-1", player_id=1)
    client, _ = _client(db_path)

    resp = client.post(
        "/api/mc/ingest",
        json={"data": [_ping()]},
        headers={"X-API-Key": "raw-key-1", "X-Forwarded-For": "198.51.100.99"},
    )
    assert resp.status_code == 202

    row = _identity_rows(db_path)[0]
    assert row["ip_hash"] == _hash_source_ip("test-salt", TESTCLIENT_PEER)
    assert row["ip_hash"] != _hash_source_ip("test-salt", "198.51.100.99")


# ---------------------------------------------------------------------
# The raw IP and raw User-Agent are never persisted anywhere
# ---------------------------------------------------------------------

def test_raw_ip_and_user_agent_appear_nowhere_in_the_database(db_path, monkeypatch, caplog):
    """The hard privacy guarantee this whole rebuild exists for: post a
    batch with a distinctive, never-otherwise-used raw IP and raw
    User-Agent, then scan the ENTIRE database FILE's bytes (not just the
    one row/column this feature writes) and the full log capture for
    either substring. Neither may appear anywhere.
    """
    monkeypatch.setattr(settings, "mc_ingest_salt", "test-salt")
    _seed_player(db_path, player_id=1)
    _seed_api_key(db_path, "raw-key-1", player_id=1)
    client, _ = _client(db_path)

    distinctive_ip = "198.51.100.222"
    distinctive_ua = "ThisExactUAStringMustNeverBeStored/9.9.9"

    caplog.set_level(logging.DEBUG)
    resp = client.post(
        "/api/mc/ingest",
        json={"data": [_ping()]},
        headers={"X-API-Key": "raw-key-1", "User-Agent": distinctive_ua},
    )
    assert resp.status_code == 202
    assert len(_identity_rows(db_path)) == 1

    db_bytes = _db_file_bytes(db_path)
    assert distinctive_ua.encode("utf-8") not in db_bytes
    # TestClient's own peer is "testclient", not a real IP, so the
    # distinctive_ip above was never actually the peer here -- the
    # point of this assertion is the UA above; a companion direct-ASGI
    # test below (test_direct_post_raw_ip_appears_nowhere_in_the_database)
    # proves the IP half with a real peer address instead.
    del distinctive_ip

    assert distinctive_ua not in caplog.text


def test_direct_post_raw_ip_appears_nowhere_in_the_database(db_path, monkeypatch, caplog):
    """Companion to the UA test above, using the direct-ASGI helper so a
    REAL, distinctive IP address is actually the connecting peer (see
    this module's own docstring for why TestClient itself cannot
    produce one)."""
    monkeypatch.setattr(settings, "mc_ingest_salt", "test-salt")
    _seed_player(db_path, player_id=1)
    _seed_api_key(db_path, "raw-key-1", player_id=1)
    app, _ = _build_app(db_path)

    distinctive_ip = "198.51.100.222"

    caplog.set_level(logging.DEBUG)
    resp = _direct_post_mc_ingest(
        app, distinctive_ip,
        {"X-API-Key": "raw-key-1", "content-type": "application/json"},
        {"data": [_ping()]},
    )
    assert resp.status_code == 202
    assert len(_identity_rows(db_path)) == 1

    db_bytes = _db_file_bytes(db_path)
    assert distinctive_ip.encode("utf-8") not in db_bytes

    assert distinctive_ip not in caplog.text


def test_capture_failure_log_never_contains_raw_ip_or_ua(db_path, monkeypatch, caplog):
    """record_ingest_identity()'s own except-and-log-warning path (see
    its docstring: "never raises into the request path") must not leak
    the raw IP/UA into the failure log line either, even under a forced
    internal failure.
    """
    monkeypatch.setattr(settings, "mc_ingest_salt", "test-salt")
    ingestor = McIngestor()

    def _boom(*args, **kwargs):
        raise RuntimeError("forced failure for this test")

    monkeypatch.setattr(mc_ingest_module, "classify_ip", _boom)

    distinctive_ip = "198.51.100.222"
    distinctive_ua = "ThisExactUAStringMustNeverBeStored/9.9.9"

    caplog.set_level(logging.DEBUG, logger="mc_ingest")
    _run(ingestor.record_ingest_identity(
        1, "keyhash", distinctive_ip, distinctive_ua, [_ping()], NOW,
    ))

    # caplog.text is pytest's own fully-formatted capture, including the
    # rendered traceback for any record logged with exc_info=True -- a
    # stronger check than inspecting raw LogRecord attributes by hand.
    assert distinctive_ip not in caplog.text
    assert distinctive_ua not in caplog.text
    assert any("request identity capture failed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------
# Stability: the same address always hashes the same way
# ---------------------------------------------------------------------

def test_same_address_produces_same_hash_across_calls(db_path, monkeypatch):
    monkeypatch.setattr(settings, "mc_ingest_salt", "test-salt")
    _seed_player(db_path, player_id=1)
    _seed_api_key(db_path, "raw-key-1", player_id=1)
    client, _ = _client(db_path)

    client.post(
        "/api/mc/ingest",
        json={"data": [_ping(ts=NOW)]},
        headers={"X-API-Key": "raw-key-1"},
    )
    client.post(
        "/api/mc/ingest",
        json={"data": [_ping(ts=NOW + 1)]},
        headers={"X-API-Key": "raw-key-1"},
    )

    rows = _identity_rows(db_path)
    assert len(rows) == 2
    assert rows[0]["ip_hash"] == rows[1]["ip_hash"]


def test_salt_persists_across_a_simulated_restart(db_path):
    """No settings.mc_ingest_salt override -- a fresh install generates
    and persists its own salt in the `cursor` table on first use.
    Clearing the in-process cache (standing in for a process restart)
    and resolving again must land on the EXACT SAME value: if this salt
    ever rotated, every historical ip_hash would silently stop matching
    anything.
    """
    ingestor = McIngestor()
    _run(ingestor.record_ingest_identity(1, "keyhash", "198.51.100.1", "ua", [_ping()], NOW))
    first_hash = _identity_rows(db_path)[0]["ip_hash"]

    # Simulate a process restart: the module-level cache is cleared, the
    # NEXT resolution must re-read the persisted value from `cursor`,
    # not mint a fresh one.
    mc_ingest_module._ingest_salt_cache = None

    _run(ingestor.record_ingest_identity(1, "keyhash", "198.51.100.1", "ua", [_ping()], NOW + 1))
    second_hash = _identity_rows(db_path)[1]["ip_hash"]

    assert first_hash == second_hash


# ---------------------------------------------------------------------
# ip_class: bundled hosting-provider prefix list, no network call
# ---------------------------------------------------------------------

def test_known_datacenter_prefix_classifies_as_datacenter(db_path):
    ingestor = McIngestor()
    _run(ingestor.record_ingest_identity(1, "keyhash", DATACENTER_ADDR, "ua", [_ping()], NOW))
    assert _identity_rows(db_path)[0]["ip_class"] == "datacenter"


def test_unknown_address_classifies_as_unknown(db_path):
    ingestor = McIngestor()
    _run(ingestor.record_ingest_identity(1, "keyhash", UNKNOWN_ADDR, "ua", [_ping()], NOW))
    assert _identity_rows(db_path)[0]["ip_class"] == "unknown"


def test_unresolvable_peer_classifies_as_unknown_not_an_error(db_path):
    """get_client_ip() falls back to the literal string "unknown" when
    Starlette hands back no peer at all -- classify_ip() must treat that
    (and any other unparseable string) as IP_CLASS_UNKNOWN, never raise.
    """
    ingestor = McIngestor()
    _run(ingestor.record_ingest_identity(1, "keyhash", "unknown", "ua", [_ping()], NOW))
    assert _identity_rows(db_path)[0]["ip_class"] == "unknown"


# ---------------------------------------------------------------------
# client_family: coarse label derived from User-Agent, never stored
# ---------------------------------------------------------------------

@pytest.mark.parametrize("ua,expected_family", [
    ("Dart/3.4 (dart:io)", "meshmapper-dart"),
    ("FreqMapper/1.0", "freqmapper"),
    ("python-requests/2.31.0", "unrecognized-python"),
    ("Mozilla/5.0 (compatible)", "unrecognized-other"),
])
def test_user_agent_maps_to_expected_client_family(db_path, ua, expected_family):
    ingestor = McIngestor()
    _run(ingestor.record_ingest_identity(1, "keyhash", "198.51.100.1", ua, [_ping()], NOW))
    assert _identity_rows(db_path)[0]["client_family"] == expected_family


def test_missing_user_agent_does_not_error(db_path):
    """A caller that sends no User-Agent header at all must not error --
    request.headers.get("user-agent") hands record_ingest_identity()
    None in that case (see app/api.py's mc_ingest()), and client_family
    must fall back to "unrecognized-other" rather than raising.
    """
    ingestor = McIngestor()
    _run(ingestor.record_ingest_identity(1, "keyhash", "198.51.100.1", None, [_ping()], NOW))

    row = _identity_rows(db_path)[0]
    assert row["client_family"] == "unrecognized-other"


def test_oversized_user_agent_does_not_error(db_path):
    """A hostile client cannot use an enormous User-Agent to crash or
    meaningfully slow this method down -- client_family_from_user_agent()
    bounds its own input internally, and nothing here ever stores the
    value regardless of length.
    """
    _seed_player(db_path, player_id=1)
    _seed_api_key(db_path, "raw-key-1", player_id=1)
    client, _ = _client(db_path)

    hostile_ua = "A" * 100_000
    resp = client.post(
        "/api/mc/ingest",
        json={"data": [_ping()]},
        headers={"X-API-Key": "raw-key-1", "User-Agent": hostile_ua},
    )
    assert resp.status_code == 202
    row = _identity_rows(db_path)[0]
    assert row["client_family"] == "unrecognized-other"


def test_type_counts_summarize_the_batch(db_path):
    _seed_player(db_path, player_id=1)
    _seed_api_key(db_path, "raw-key-1", player_id=1)
    client, _ = _client(db_path)

    pings = [
        _ping(ping_type="TX", contact="deadbeef"),
        _ping(ping_type="TX", contact="deadbeef"),
        _ping(ping_type="RX", contact="deadbeef"),
    ]
    resp = client.post(
        "/api/mc/ingest", json={"data": pings}, headers={"X-API-Key": "raw-key-1"},
    )
    assert resp.status_code == 202

    row = _identity_rows(db_path)[0]
    assert row["ping_count"] == 3
    counts = json_module.loads(row["type_counts"])
    assert counts == {"TX": 2, "RX": 1}


# ---------------------------------------------------------------------
# Recorded regardless of whether the batch was durably queued
# ---------------------------------------------------------------------

def test_identity_still_recorded_when_queue_is_full(db_path, monkeypatch):
    monkeypatch.setattr(settings, "mc_queue_max", 0)
    _seed_player(db_path, player_id=1)
    _seed_api_key(db_path, "raw-key-1", player_id=1)
    client, _ = _client(db_path)

    resp = client.post(
        "/api/mc/ingest", json={"data": [_ping()]}, headers={"X-API-Key": "raw-key-1"},
    )
    assert resp.status_code == 503

    # The batch itself was refused, but who sent it is still on record.
    assert len(_identity_rows(db_path)) == 1


# ---------------------------------------------------------------------
# Disable flag fully reverts to prior behaviour
# ---------------------------------------------------------------------

def test_disabled_records_nothing(db_path, monkeypatch):
    monkeypatch.setattr(settings, "mc_ingest_identity_enabled", False)
    _seed_player(db_path, player_id=1)
    _seed_api_key(db_path, "raw-key-1", player_id=1)
    client, _ = _client(db_path)

    resp = client.post(
        "/api/mc/ingest", json={"data": [_ping()]}, headers={"X-API-Key": "raw-key-1"},
    )
    assert resp.status_code == 202
    assert _identity_rows(db_path) == []


def test_record_ingest_identity_disabled_never_touches_db(db_path, monkeypatch):
    """Unit-level version of the same guarantee: calling the method
    directly with the flag off must not even open a write session."""
    monkeypatch.setattr(settings, "mc_ingest_identity_enabled", False)
    ingestor = McIngestor()
    _run(ingestor.record_ingest_identity(1, "keyhash", "1.2.3.4", "ua", [_ping()], NOW))
    assert _identity_rows(db_path) == []


# ---------------------------------------------------------------------
# Retention pruning
# ---------------------------------------------------------------------

def _seed_identity_row(db_path, received_at, player_id=1):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO mc_ingest_request_log"
        "(received_at, player_id, key_hash_prefix, ip_hash, ip_class, "
        " client_family, ping_count, type_counts) "
        "VALUES (?, ?, 'deadbeef', 'somehash', 'unknown', 'unrecognized-other', 1, '{}')",
        (received_at, player_id),
    )
    conn.commit()
    conn.close()


def _seed_wire_tag_row(db_path, tag, first_seen_at, player_id=1):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO mc_wire_tag(wire_tag, player_id, first_seen_at) VALUES (?, ?, ?)",
        (tag, player_id, first_seen_at),
    )
    conn.commit()
    conn.close()


def test_housekeeping_prunes_old_identity_rows_keeps_recent(db_path, monkeypatch):
    monkeypatch.setattr(settings, "mc_ingest_identity_retention_days", 90)
    now_ts = int(time.time())
    old_ts = now_ts - (91 * 86400)
    recent_ts = now_ts - (10 * 86400)
    _seed_identity_row(db_path, old_ts)
    _seed_identity_row(db_path, recent_ts)

    ingestor = McIngestor()
    (
        _removed_pings, _removed_stats, _removed_credits,
        removed_identity, _removed_wire_tags,
    ) = ingestor._housekeeping_sync()

    assert removed_identity == 1
    rows = _identity_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["received_at"] == recent_ts


def test_housekeeping_prunes_old_wire_tag_rows_keeps_recent(db_path, monkeypatch):
    monkeypatch.setattr(settings, "mc_wire_tag_retention_days", 365)
    now_ts = int(time.time())
    old_ts = now_ts - (366 * 86400)
    recent_ts = now_ts - (10 * 86400)
    _seed_wire_tag_row(db_path, "MM:0000000001", old_ts)
    _seed_wire_tag_row(db_path, "MM:0000000002", recent_ts)

    ingestor = McIngestor()
    (
        _removed_pings, _removed_stats, _removed_credits,
        _removed_identity, removed_wire_tags,
    ) = ingestor._housekeeping_sync()

    assert removed_wire_tags == 1
    conn = sqlite3.connect(db_path)
    remaining = [r[0] for r in conn.execute("SELECT wire_tag FROM mc_wire_tag")]
    conn.close()
    assert remaining == ["MM:0000000002"]
