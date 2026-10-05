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

What the published row holds, and when it is rewritten (also covered
here): the worker stores gzip_body, etag and built_at and leaves `body`
EMPTY, and skips the write entirely when the freshly built board's etag
equals the one already stored -- a 6.16 MB body used to be rewritten
every 10 s whether or not anything had changed. A web process serves the
stored gzip bytes as they are and inflates the plaintext from them only
for a client that cannot take gzip. /get-nodes' two cache keys go through
the same rows and tiers; tests/test_get_nodes_cache.py covers those.

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
import logging
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


def _board_row(db_path, key: str = "mc_board"):
    """The raw board_cache row for `key`, read with plain sqlite (not the
    app's own connect()), or None when there is no such row."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT body, gzip_body, etag, built_at FROM board_cache WHERE cache_key = ?",
        (key,),
    ).fetchone()
    conn.close()
    return row


def _put_row(db_path, key: str, body: bytes, gzip_body: bytes | None, etag: str) -> None:
    """Write a board_cache row by hand, in any shape a row can be found
    in -- including shapes the current publisher never writes."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO board_cache(cache_key, body, gzip_body, etag, built_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(cache_key) DO UPDATE SET"
        " body = excluded.body, gzip_body = excluded.gzip_body,"
        " etag = excluded.etag, built_at = excluded.built_at",
        (key, body, gzip_body, etag, NOW),
    )
    conn.commit()
    conn.close()


def _set_built_at(db_path, key: str, built_at: int) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE board_cache SET built_at = ? WHERE cache_key = ?", (built_at, key))
    conn.commit()
    conn.close()


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
    """The publisher's own INSERT must describe EXACTLY what an inline
    build produces for the same DB state -- both derive from the same
    board_for()/json.dumps()/hashlib formula, just run at different
    times/in different processes. Its etag IS the inline etag, and its
    gzip bytes decompress back to the inline plaintext byte for byte.

    The plaintext itself is deliberately NOT stored any more: `body` is
    empty (b"" -- the column is NOT NULL, so not NULL), because a web
    process serves the gzip bytes and only inflates a plaintext for a
    client that cannot take gzip. This test used to assert the opposite
    (body stored on every publish)."""
    _seed_board(db_path)
    expected_body, expected_etag = _expected_bytes(db_path)

    asyncio.run(mc_api_module._publish_board_once())

    row = _board_row(db_path)
    assert row is not None
    assert bytes(row["body"]) == b""
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


# ---- skipping unchanged publishes -----------------------------------------


def test_publisher_skips_an_unchanged_key_without_writing_and_logs_at_debug(db_path, caplog):
    """A key whose freshly built etag equals the stored one is not
    rewritten: no gzip, no upsert (so no write lock, no WAL traffic).
    This used to rewrite a 6.16 MB body every cycle whether or not one
    cell had changed.

    A rewrite stamps built_at with the current time, so the row is parked
    on a value no real publish can produce -- any upsert shows up at once,
    with no sleeping. The skip is logged at DEBUG, not louder."""
    _seed_board(db_path)
    asyncio.run(mc_api_module._publish_board_once())
    _set_built_at(db_path, "mc_board", 1)
    before = _board_row(db_path)

    with caplog.at_level(logging.DEBUG, logger="mc_api"):
        asyncio.run(mc_api_module._publish_board_once())

    after = _board_row(db_path)
    assert after["built_at"] == 1  # untouched: no upsert happened
    assert after["etag"] == before["etag"]
    assert bytes(after["gzip_body"]) == bytes(before["gzip_body"])
    assert any(
        r.levelno == logging.DEBUG
        and "mc_board" in r.getMessage()
        and "unchanged" in r.getMessage()
        for r in caplog.records
    )


def test_publisher_rewrites_the_row_when_the_board_changes(db_path):
    """The other half of the skip: once the board's content (so its etag)
    changes, the row is rewritten -- new etag, new gzip bytes, fresh
    built_at, and still no plaintext stored."""
    season_id = _seed_board(db_path)
    asyncio.run(mc_api_module._publish_board_once())
    _set_built_at(db_path, "mc_board", 1)
    old_etag = _board_row(db_path)["etag"]

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts) "
        "VALUES (?, '2000_-2000', 'RED', 1, ?)",
        (season_id, NOW),
    )
    conn.commit()
    conn.close()
    new_body, new_etag = _expected_bytes(db_path)
    assert new_etag != old_etag  # the precondition: the board really did change

    asyncio.run(mc_api_module._publish_board_once())

    row = _board_row(db_path)
    assert row["etag"] == new_etag
    assert row["built_at"] > 1
    assert bytes(row["body"]) == b""
    assert gzip.decompress(bytes(row["gzip_body"])) == new_body


def test_stored_board_etag_reads_the_published_row(db_path):
    _seed_board(db_path)
    _, expected_etag = _expected_bytes(db_path)
    assert mc_api_module._stored_board_etag("mc_board") is None  # no row yet

    asyncio.run(mc_api_module._publish_board_once())

    assert mc_api_module._stored_board_etag("mc_board") == expected_etag


def test_stored_board_etag_lookup_failure_reads_as_no_row(monkeypatch):
    """Fails OPEN: a lookup that cannot be answered reads as "nothing
    stored", which is what makes the publisher publish -- a flaky read can
    cost a redundant write, never a skipped one."""

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(mc_api_module, "connect", boom)
    assert mc_api_module._stored_board_etag("mc_board") is None


# ---- serving a row whose plaintext body is empty --------------------------


def test_non_gzip_client_gets_raw_bytes_and_etag_from_a_row_with_empty_body(db_path, monkeypatch):
    """The publisher leaves `body` empty, so a client that cannot take
    gzip is served the plaintext INFLATED from gzip_body: byte-identical
    to an inline build, same ETag, same 304 -- and kept in the in-process
    cache the way a built body would be, so a second plaintext request
    inflates nothing."""
    _seed_board(db_path)
    monkeypatch.setattr(settings, "board_cache_seconds", 10)
    expected_body, expected_etag = _expected_bytes(db_path)
    asyncio.run(mc_api_module._publish_board_once())
    assert bytes(_board_row(db_path)["body"]) == b""  # the precondition under test

    inflates = {"n": 0}
    real_decompress = gzip.decompress

    def counting_decompress(data, *args, **kwargs):
        inflates["n"] += 1
        return real_decompress(data, *args, **kwargs)

    monkeypatch.setattr(mc_api_module.gzip, "decompress", counting_decompress)

    calls = {"n": 0}

    def build():
        calls["n"] += 1
        return board_for(MC_PROTOCOL, include_meta=False)

    first = cached_json_response("mc_board", build, _request())
    assert first.status_code == 200
    assert first.body == expected_body
    assert first.headers["ETag"] == expected_etag
    assert "content-encoding" not in first.headers

    second = cached_json_response("mc_board", build, _request())
    assert second.body == expected_body
    assert mc_api_module._BOARD_CACHE["mc_board"].body == expected_body
    assert inflates["n"] == 1  # inflated once, then served from the in-process entry
    assert calls["n"] == 0  # and never rebuilt

    revalidated = cached_json_response("mc_board", build, _request(if_none_match=expected_etag))
    assert revalidated.status_code == 304
    assert revalidated.body == b""


def test_gzip_client_gets_the_stored_gzip_bytes_from_a_row_with_empty_body(db_path, monkeypatch):
    """A gzip client is served the stored artifact as it is -- not
    recompressed, not rebuilt -- with the same ETag the plaintext client
    gets."""
    _seed_board(db_path)
    monkeypatch.setattr(settings, "board_cache_seconds", 10)
    expected_body, expected_etag = _expected_bytes(db_path)
    asyncio.run(mc_api_module._publish_board_once())
    stored_gzip = bytes(_board_row(db_path)["gzip_body"])

    compressions = {"n": 0}
    real_compress = gzip.compress

    def counting_compress(data, *args, **kwargs):
        compressions["n"] += 1
        return real_compress(data, *args, **kwargs)

    monkeypatch.setattr(mc_api_module.gzip, "compress", counting_compress)

    def build():
        raise AssertionError("served from the published row, never rebuilt")

    resp = cached_json_response("mc_board", build, _request(accept_gzip=True))

    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.headers["vary"] == "Accept-Encoding"
    assert resp.headers["ETag"] == expected_etag
    assert resp.body == stored_gzip
    assert compressions["n"] == 0
    assert gzip.decompress(resp.body) == expected_body


@pytest.mark.parametrize("shape", ["gzip_only", "body_only", "body_and_gzip"])
def test_every_row_shape_serves_both_kinds_of_client_with_one_etag(db_path, monkeypatch, shape):
    """A row can be found in more than one shape: gzip only (what the
    publisher writes now), plaintext only, or both (what it used to
    write -- and one of those stays in place after a deploy for as long as
    the board does not change, since an unchanged board is not rewritten).
    All three must serve a gzip client and a plaintext client correctly,
    under one ETag."""
    _seed_board(db_path)
    monkeypatch.setattr(settings, "board_cache_seconds", 10)
    expected_body, expected_etag = _expected_bytes(db_path)
    _put_row(
        db_path,
        "mc_board",
        body=b"" if shape == "gzip_only" else expected_body,
        gzip_body=None if shape == "body_only" else gzip.compress(expected_body),
        etag=expected_etag,
    )

    def build():
        raise AssertionError("served from the row, never rebuilt")

    plain = cached_json_response("mc_board", build, _request())
    mc_api_module._BOARD_CACHE.clear()  # the gzip client below reads the row afresh
    gz = cached_json_response("mc_board", build, _request(accept_gzip=True))

    assert plain.body == expected_body
    assert "content-encoding" not in plain.headers
    assert gz.headers["content-encoding"] == "gzip"
    assert gzip.decompress(gz.body) == expected_body
    assert plain.headers["ETag"] == gz.headers["ETag"] == expected_etag


def test_row_with_neither_body_nor_gzip_falls_back_to_the_inline_build(db_path, monkeypatch):
    """A row holding nothing servable reads as a miss, so the request
    builds inline exactly as if there were no row at all -- the web role
    never depends on the table being well-formed."""
    _seed_board(db_path)
    monkeypatch.setattr(settings, "board_cache_seconds", 10)
    expected_body, expected_etag = _expected_bytes(db_path)
    _put_row(db_path, "mc_board", body=b"", gzip_body=None, etag='"nothing-to-serve"')

    calls = {"n": 0}

    def build():
        calls["n"] += 1
        return board_for(MC_PROTOCOL, include_meta=False)

    resp = cached_json_response("mc_board", build, _request())

    assert calls["n"] == 1
    assert resp.body == expected_body
    assert resp.headers["ETag"] == expected_etag  # the built one, not the empty row's


# ---- one gzip level --------------------------------------------------------


def test_publisher_and_inline_path_compress_at_the_one_shared_level(db_path, monkeypatch):
    """_GZIP_COMPRESSLEVEL is the single knob for both the worker's
    published artifact and the inline (cold-start) per-entry compression.
    It was 9 (GZipMiddleware's default, a multiple of the CPU of 6 for a
    marginal size gain on a ~6 MB body); it is 6 now."""
    levels = []
    real_compress = gzip.compress

    def recording_compress(data, *args, **kwargs):
        levels.append(kwargs.get("compresslevel"))
        return real_compress(data, *args, **kwargs)

    monkeypatch.setattr(mc_api_module.gzip, "compress", recording_compress)
    _seed_board(db_path)

    # Inline path: ttl=0 skips every tier, so this builds and compresses here.
    monkeypatch.setattr(settings, "board_cache_seconds", 0)
    cached_json_response(
        "mc_board", lambda: board_for(MC_PROTOCOL, include_meta=False), _request(accept_gzip=True)
    )
    inline_levels = list(levels)
    # Worker path.
    asyncio.run(mc_api_module._publish_board_once())

    assert mc_api_module._GZIP_COMPRESSLEVEL == 6
    assert inline_levels == [6]
    assert len(levels) > len(inline_levels)  # the publisher compressed too
    assert set(levels) == {6}
