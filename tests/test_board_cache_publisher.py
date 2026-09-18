"""Tests for the worker-published board_cache table (app/db.py) and its
three-tier read path in app/mc_api.py's cached_json_response: this
process's own in-process _BOARD_CACHE (tier 1) -> board_cache (tier 2,
worker-published) -> inline build() (tier 3, cold-start / never-
published-key fallback). See app/mc_api.py's run_forever() and
cached_json_response() docstrings for the full reasoning this file
proves.

Why this exists: after the web/worker split (docker-compose.yml's
`meshwars` (uvicorn --workers 3, RUN_BACKGROUND_TASKS=false) vs.
`meshwars-worker` (RUN_BACKGROUND_TASKS=true)), _BOARD_CACHE became a
per-PROCESS dict -- each web worker rebuilt /api/mc/board's ~4.2MB
payload independently on every board_cache_seconds TTL miss (up to 3
~6.8s rebuilds per window, on processes serving user requests). The
worker now precomputes and publishes that payload to board_cache
instead, so a web process's cache miss reads a finished row rather than
rebuilding.

Real file-backed sqlite (not the in-memory `conn` fixture other
app/mc_api.py tests use, e.g. tests/test_mc_api_cell_park.py):
_publish_board_once() writes through WriteSession(), which goes through
app/db.py's own connect()/db_path plumbing, not a monkeypatched
module-level `connect`. Same style as tests/test_mc_ingest_durable_queue.py
and tests/test_role_split.py -- asyncio.run(...) from plain sync test
functions (no pytest-asyncio configured; see tests/test_write_session.py's
own docstring for why).
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import sqlite3
import time

import pytest
from fastapi import Request

import app.mc_api as mc_api_module
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.mc_api import MC_PROTOCOL, board_for, cached_json_response

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
    monkeypatch.setattr(settings, "db_path", path)
    return path


@pytest.fixture(autouse=True)
def _reset_board_cache():
    mc_api_module._BOARD_CACHE.clear()
    yield
    mc_api_module._BOARD_CACHE.clear()


def _seed_board(db_path, n_cells: int = 3) -> int:
    """A few owned cells for the active MC season -- enough for
    board_for(MC_PROTOCOL, include_meta=False) to return real, non-empty
    data. Same shape tests/test_mc_api_cell_park.py's own _season()/
    _tile() helpers use, inserted directly (bypassing the ingest path,
    which is not what this file is testing)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO mc_season(protocol, started_at, ends_at, status) VALUES (?,?,?,?)",
        (MC_PROTOCOL, NOW - 1000, NOW + 1_000_000, "active"),
    )
    season_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (1, 'tester', 'RED', ?)",
        (NOW,),
    )
    for i in range(n_cells):
        conn.execute(
            "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts) "
            "VALUES (?, ?, 'RED', 1, ?)",
            (season_id, f"{1000 + i}_{-1000 - i}", NOW),
        )
    conn.commit()
    conn.close()
    return season_id


def _request(if_none_match: str | None = None, accept_gzip: bool = False) -> Request:
    """Minimal Request carrying just enough of an ASGI scope for
    cached_json_response to read If-None-Match/Accept-Encoding -- same
    approach tests/test_response_gzip_cache.py's own _request() uses."""
    headers = []
    if if_none_match is not None:
        headers.append((b"if-none-match", if_none_match.encode("latin-1")))
    if accept_gzip:
        headers.append((b"accept-encoding", b"gzip, deflate, br"))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "http_version": "1.1",
        "headers": headers,
    }
    return Request(scope)


def _expected_bytes(db_path) -> tuple[bytes, str]:
    """What an inline build of the 'mc_board' key produces right now --
    computed with the exact same json.dumps/hashlib formula
    cached_json_response's own inline path (and the publisher) use --
    the reference every tier below is checked against."""
    payload = board_for(MC_PROTOCOL, include_meta=False)
    body = json.dumps(payload, separators=(",", ":")).encode()
    etag = '"%s"' % hashlib.sha256(body).hexdigest()[:32]
    return body, etag


def test_web_role_serves_from_table_without_rebuild(db_path, monkeypatch):
    """A web-role process (publisher loop not running in this process,
    in-process cache empty) serves /api/mc/board's payload straight from
    board_cache -- the whole point of this table -- without ever calling
    build()."""
    _seed_board(db_path)
    monkeypatch.setattr(settings, "board_cache_seconds", 10)
    expected_body, expected_etag = _expected_bytes(db_path)

    asyncio.run(mc_api_module._publish_board_once())
    assert "mc_board" not in mc_api_module._BOARD_CACHE  # publishing alone must not warm it

    calls = {"n": 0}

    def build():
        calls["n"] += 1
        return board_for(MC_PROTOCOL, include_meta=False)

    resp = cached_json_response("mc_board", build, _request())
    assert calls["n"] == 0
    assert resp.body == expected_body
    assert resp.headers["ETag"] == expected_etag
    # Reading from the table must warm the in-process cache too.
    assert mc_api_module._BOARD_CACHE["mc_board"].body == expected_body


def test_in_process_cache_wins_when_warm(db_path, monkeypatch):
    """A warm process's own _BOARD_CACHE answers without ever touching
    the database -- unchanged fast path, even with a table row present."""
    _seed_board(db_path)
    monkeypatch.setattr(settings, "board_cache_seconds", 10)
    expected_body, expected_etag = _expected_bytes(db_path)
    asyncio.run(mc_api_module._publish_board_once())  # a table row exists too
    mc_api_module._BOARD_CACHE["mc_board"] = mc_api_module._CachedBody(
        time.monotonic(), expected_body, expected_etag
    )

    def _forbidden_connect(*args, **kwargs):
        raise AssertionError("a warm in-process hit must not touch the database")

    monkeypatch.setattr(mc_api_module, "connect", _forbidden_connect)

    calls = {"n": 0}

    def build():
        calls["n"] += 1
        return board_for(MC_PROTOCOL, include_meta=False)

    resp = cached_json_response("mc_board", build, _request())
    assert calls["n"] == 0
    assert resp.body == expected_body


def test_cold_start_builds_inline_when_table_and_memory_both_empty(db_path, monkeypatch):
    """Fresh deploy, before the worker's first publish pass: empty table
    AND empty in-process cache must still serve correctly, by building
    inline exactly as before this table existed. The web role must
    never hard-depend on the worker having run."""
    _seed_board(db_path)
    monkeypatch.setattr(settings, "board_cache_seconds", 10)
    expected_body, expected_etag = _expected_bytes(db_path)

    calls = {"n": 0}

    def build():
        calls["n"] += 1
        return board_for(MC_PROTOCOL, include_meta=False)

    resp = cached_json_response("mc_board", build, _request())
    assert calls["n"] == 1
    assert resp.body == expected_body
    assert resp.headers["ETag"] == expected_etag


def test_publisher_row_is_byte_identical_to_inline_build(db_path):
    """The publisher's own INSERT must produce EXACTLY what an inline
    build produces for the same DB state -- both derive from the same
    board_for()/json.dumps()/hashlib formula, just run at different
    times/in different processes -- and its gzip bytes must decompress
    back to that same plaintext."""
    _seed_board(db_path)
    expected_body, expected_etag = _expected_bytes(db_path)

    asyncio.run(mc_api_module._publish_board_once())

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT body, gzip_body, etag FROM board_cache WHERE cache_key = 'mc_board'"
    ).fetchone()
    conn.close()

    assert row is not None
    assert bytes(row["body"]) == expected_body
    assert row["etag"] == expected_etag
    assert gzip.decompress(bytes(row["gzip_body"])) == expected_body


def test_etag_and_304_identical_across_all_three_tiers(db_path, monkeypatch):
    """The same If-None-Match must 304 whether the entry came from
    memory, from the table, or from an inline build -- because all three
    are derived from the identical bytes for unchanged underlying data."""
    _seed_board(db_path)
    expected_body, expected_etag = _expected_bytes(db_path)

    def build():
        return board_for(MC_PROTOCOL, include_meta=False)

    # Tier 3: ttl=0 always takes the inline-build path, and a freshly
    # rebuilt entry still honours a matching If-None-Match.
    monkeypatch.setattr(settings, "board_cache_seconds", 0)
    resp3 = cached_json_response("mc_board", build, _request(if_none_match=expected_etag))
    assert resp3.status_code == 304
    assert resp3.body == b""

    # Tier 2: publish the row, clear the in-process cache, same etag.
    monkeypatch.setattr(settings, "board_cache_seconds", 10)
    asyncio.run(mc_api_module._publish_board_once())
    mc_api_module._BOARD_CACHE.clear()
    resp2 = cached_json_response("mc_board", build, _request(if_none_match=expected_etag))
    assert resp2.status_code == 304
    assert resp2.body == b""

    # Tier 1: in-process hit, no DB touched at all.
    def _forbidden_connect(*args, **kwargs):
        raise AssertionError("a tier-1 hit must not touch the database")

    monkeypatch.setattr(mc_api_module, "connect", _forbidden_connect)
    resp1 = cached_json_response("mc_board", build, _request(if_none_match=expected_etag))
    assert resp1.status_code == 304
    assert resp1.body == b""


def test_ttl_zero_bypasses_memory_and_table_entirely(db_path, monkeypatch):
    """ttl=0 must mean NO caching at all -- not just "skip _BOARD_CACHE"
    but also "never read board_cache" -- so a stale row (or a stale
    in-process entry) left over from a different ttl setting is never
    served."""
    _seed_board(db_path)
    stale_body = b'{"stale": true}'
    stale_etag = '"stale-etag-0000000000000000"'

    monkeypatch.setattr(settings, "board_cache_seconds", 10)
    mc_api_module._BOARD_CACHE["mc_board"] = mc_api_module._CachedBody(
        time.monotonic(), stale_body, stale_etag
    )
    asyncio.run(mc_api_module._publish_board_once())
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE board_cache SET body = ?, gzip_body = NULL, etag = ? WHERE cache_key = 'mc_board'",
        (stale_body, stale_etag),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(settings, "board_cache_seconds", 0)
    expected_body, expected_etag = _expected_bytes(db_path)

    calls = {"n": 0}

    def build():
        calls["n"] += 1
        return board_for(MC_PROTOCOL, include_meta=False)

    resp = cached_json_response("mc_board", build, _request())
    assert calls["n"] == 1
    assert resp.body == expected_body
    assert resp.body != stale_body
    assert resp.headers["ETag"] == expected_etag
