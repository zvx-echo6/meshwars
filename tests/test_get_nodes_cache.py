"""Tests for /get-nodes (the Meshtastic board's main data route) going
through the same cache path /api/mc/board does: the request is passed to
app/mc_api.py's cached_json_response, so the gzip bytes are cached and
served with an ETag, a matching If-None-Match gets a 304, and both of its
cache keys ('mt_board_public' / 'mt_board_authed') are published into
board_cache by the worker role and read back by the web role -- see
tests/test_board_cache_publisher.py for the publisher/row mechanics
themselves.

Why it needed fixing: the route called cached_json_response WITHOUT the
request, so the gzip bytes were never cached (Starlette's GZipMiddleware
recompressed a ~3.3 MB body on every request), it could never answer
If-None-Match with a 304 (which frontend/map2.js's fetchBoard() already
sends), and every web process rebuilt the board on every cache miss.

The privacy line this route draws (team attribution only with a session,
see app/api.py's _build_get_nodes) is the thing most worth guarding here:
publishing a row per key must not move it. The two registered builds
differ ONLY in include_attribution, the public row must never carry a
`team`, and a validator minted against the signed-in body must never
answer 304 for the signed-out one.

Real file-backed sqlite and a bare FastAPI app around app/api.py's real
router -- same fixture shape and reasoning as
tests/test_privacy_hardening.py. TestClient (httpx) decodes a gzip
response transparently, so `resp.content`/`resp.json()` are the plaintext
and the compressed length is read off the Content-Length header.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import sqlite3
import time

import pytest
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.testclient import TestClient

import app.api as api_module
import app.db as db
import app.mc_api as mc_api_module
from app.api import _node_hex
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.sessions import SESSION_COOKIE_NAME, create_session

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
    monkeypatch.setattr(db.settings, "db_path", path)
    return path


@pytest.fixture
def web_app(db_path):
    app = FastAPI()
    app.include_router(api_module.router)
    return app


@pytest.fixture
def anon(web_app):
    """A signed-out visitor: no session cookie."""
    return TestClient(web_app)


@pytest.fixture
def authed(web_app, db_path):
    """A signed-in visitor, with its own cookie jar and a real session."""
    client = TestClient(web_app)
    conn = sqlite3.connect(db_path)
    cur = conn.execute("INSERT INTO account(created_at) VALUES (?)", (NOW,))
    conn.commit()
    account_id = cur.lastrowid
    conn.close()
    raw_token = asyncio.run(create_session(account_id, device_label="Firefox on Windows"))
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return client


@pytest.fixture(autouse=True)
def _fresh_board_cache(monkeypatch):
    """_BOARD_CACHE is a module-level singleton; left dirty, one test's
    entries would answer another's requests. The tiers only exist at a
    positive TTL, so pin it rather than lean on the default."""
    monkeypatch.setattr(settings, "board_cache_seconds", 10)
    mc_api_module._BOARD_CACHE.clear()
    yield
    mc_api_module._BOARD_CACHE.clear()


def _seed_mt_board(path: str) -> None:
    """An active Meshtastic season with one owned cell and one seen node
    whose radio is bound to a registered BLUE player -- the one thing the
    two /get-nodes shapes differ on is that node's `team`. Same inserts
    tests/test_privacy_hardening.py's own helpers make."""
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO mc_season(id, protocol, started_at, ends_at, status) "
        "VALUES (1, 'mt', ?, ?, 'active')",
        (NOW - 1000, NOW + 1_000_000),
    )
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (1, 'radio-owner', 'BLUE', ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts) "
        "VALUES (1, '1_1', 'BLUE', 1, ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) VALUES ('mt', ?, 1, ?)",
        (_node_hex(1), NOW),
    )
    conn.execute(
        "INSERT INTO node_seen(season_id, node_id, name, lat, lon, elev, last_seen) "
        "VALUES (1, 1, 'MyNode', 43.6135, -116.2035, 0, ?)",
        (NOW,),
    )
    conn.commit()
    conn.close()


def _board_row(path: str, key: str):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT body, gzip_body, etag FROM board_cache WHERE cache_key = ?", (key,)
    ).fetchone()
    conn.close()
    return row


GZIP = {"Accept-Encoding": "gzip"}
PLAIN = {"Accept-Encoding": "identity"}


# ---- the route passes its request: cached gzip bytes, ETag, 304 -----------


def test_get_nodes_serves_the_cached_gzip_bytes_with_an_etag(anon, db_path):
    _seed_mt_board(db_path)

    resp = anon.get("/get-nodes", headers=GZIP)

    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.headers["vary"] == "Accept-Encoding"
    etag = resp.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"')

    entry = mc_api_module._BOARD_CACHE["mt_board_public"]
    assert entry.etag == etag
    # What went over the wire IS the cached gzip artifact, not a fresh
    # compression of the body: same length, and it inflates to exactly
    # what the client decoded.
    assert int(resp.headers["content-length"]) == len(entry.gzip_body)
    assert gzip.decompress(entry.gzip_body) == resp.content
    assert resp.json()["repeaters"][0]["lat"] == 43.6135


def test_get_nodes_gzip_is_not_recompressed_by_the_gzip_middleware(db_path):
    """app/main.py puts GZipMiddleware in front of every route. A
    response that already carries Content-Encoding: gzip must pass
    through it untouched -- otherwise the cached artifact would be
    compressed a second time on every request, which is the very cost
    this route's caching exists to remove. minimum_size=1 here, so the
    middleware WOULD compress this small test body if it were allowed to."""
    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=1)
    app.include_router(api_module.router)
    client = TestClient(app)
    _seed_mt_board(db_path)

    resp = client.get("/get-nodes", headers=GZIP)

    entry = mc_api_module._BOARD_CACHE["mt_board_public"]
    assert resp.headers["content-encoding"] == "gzip"
    assert int(resp.headers["content-length"]) == len(entry.gzip_body)
    assert resp.json()["repeaters"][0]["name"] == "MyNode"


def test_get_nodes_answers_a_matching_if_none_match_with_304(anon, db_path):
    _seed_mt_board(db_path)
    etag = anon.get("/get-nodes", headers=GZIP).headers["etag"]

    gz = anon.get("/get-nodes", headers={**GZIP, "If-None-Match": etag})
    assert gz.status_code == 304
    assert gz.content == b""
    assert gz.headers["etag"] == etag
    assert gz.headers["vary"] == "Accept-Encoding"

    plain = anon.get("/get-nodes", headers={**PLAIN, "If-None-Match": etag})
    assert plain.status_code == 304
    assert plain.content == b""

    stale = anon.get("/get-nodes", headers={**GZIP, "If-None-Match": '"not-the-current-etag"'})
    assert stale.status_code == 200


def test_get_nodes_plain_client_gets_plain_json_and_the_same_etag(anon, db_path):
    _seed_mt_board(db_path)

    gz = anon.get("/get-nodes", headers=GZIP)
    plain = anon.get("/get-nodes", headers=PLAIN)

    assert "content-encoding" not in plain.headers
    assert plain.headers["etag"] == gz.headers["etag"]
    assert plain.json() == gz.json()


def test_get_nodes_does_not_recompress_on_a_cache_hit(anon, db_path, monkeypatch):
    _seed_mt_board(db_path)
    compressions = {"n": 0}
    real_compress = gzip.compress

    def counting_compress(data, *args, **kwargs):
        compressions["n"] += 1
        return real_compress(data, *args, **kwargs)

    monkeypatch.setattr(mc_api_module.gzip, "compress", counting_compress)

    for _ in range(3):
        assert anon.get("/get-nodes", headers=GZIP).status_code == 200

    assert compressions["n"] == 1


# ---- both keys are published, built with the right attribution ------------


def test_both_get_nodes_keys_are_registered_for_publishing():
    builds = mc_api_module._PUBLISHED_BOARD_BUILDS
    assert "mt_board_public" in builds
    assert "mt_board_authed" in builds
    assert "mc_board" in builds  # the original key is still published


def test_registered_builds_differ_only_by_team_attribution(db_path):
    """The two shapes are the same build with ONE boolean flipped -- which
    is what makes it safe for a session-less worker to publish both. If
    the wiring were ever swapped or merged, the public row would carry
    team attribution (the leak this route's whole privacy pass closed)."""
    _seed_mt_board(db_path)

    public = mc_api_module._PUBLISHED_BOARD_BUILDS["mt_board_public"]()
    authed = mc_api_module._PUBLISHED_BOARD_BUILDS["mt_board_authed"]()

    assert [r["team"] for r in public["repeaters"]] == [None]
    assert [r["team"] for r in authed["repeaters"]] == ["BLUE"]
    assert public["coverage"] == authed["coverage"]
    assert [{k: v for k, v in r.items() if k != "team"} for r in public["repeaters"]] == [
        {k: v for k, v in r.items() if k != "team"} for r in authed["repeaters"]
    ]


def test_publisher_writes_both_get_nodes_rows_and_only_the_authed_one_has_teams(db_path):
    _seed_mt_board(db_path)

    asyncio.run(mc_api_module._publish_board_once())

    pub_row = _board_row(db_path, "mt_board_public")
    auth_row = _board_row(db_path, "mt_board_authed")
    assert pub_row is not None and auth_row is not None
    assert bytes(pub_row["body"]) == b"" and bytes(auth_row["body"]) == b""
    public = json.loads(gzip.decompress(bytes(pub_row["gzip_body"])))
    authed = json.loads(gzip.decompress(bytes(auth_row["gzip_body"])))
    assert [r["team"] for r in public["repeaters"]] == [None]
    assert [r["team"] for r in authed["repeaters"]] == ["BLUE"]
    assert pub_row["etag"] != auth_row["etag"]


def test_get_nodes_is_served_from_the_published_rows_without_a_rebuild(
    anon, authed, db_path, monkeypatch
):
    """Same three tiers as /api/mc/board: with the worker's rows in
    place a web process answers from board_cache and never builds. The
    key is still picked by session presence in the route, so the
    signed-out visitor gets the unattributed row and the signed-in one the
    attributed row."""
    _seed_mt_board(db_path)
    asyncio.run(mc_api_module._publish_board_once())
    mc_api_module._BOARD_CACHE.clear()
    pub_row = _board_row(db_path, "mt_board_public")
    auth_row = _board_row(db_path, "mt_board_authed")

    def no_rebuild(*, include_attribution):
        raise AssertionError("served from board_cache, never rebuilt")

    monkeypatch.setattr(api_module, "_build_get_nodes", no_rebuild)

    pub = anon.get("/get-nodes", headers=GZIP)
    auth = authed.get("/get-nodes", headers=GZIP)

    assert pub.status_code == 200 and auth.status_code == 200
    assert pub.headers["etag"] == pub_row["etag"]
    assert auth.headers["etag"] == auth_row["etag"]
    assert pub.json()["repeaters"][0]["team"] is None
    assert auth.json()["repeaters"][0]["team"] == "BLUE"
    # The stored gzip artifact is what went on the wire, as it is.
    assert int(pub.headers["content-length"]) == len(bytes(pub_row["gzip_body"]))
    assert int(auth.headers["content-length"]) == len(bytes(auth_row["gzip_body"]))

    # A client that cannot take gzip is served the same row, inflated.
    plain = anon.get("/get-nodes", headers=PLAIN)
    assert plain.status_code == 200
    assert plain.headers["etag"] == pub_row["etag"]
    assert plain.json() == pub.json()

    # And a matching validator gets its 304 from the row's etag.
    again = anon.get("/get-nodes", headers={**GZIP, "If-None-Match": pub_row["etag"]})
    assert again.status_code == 304


def test_get_nodes_falls_back_to_the_inline_build_when_nothing_was_published(
    anon, authed, db_path
):
    """The web role never hard-depends on the worker: no rows at all (a
    fresh deploy, or the worker down) still serves both shapes, built
    inline."""
    _seed_mt_board(db_path)
    assert _board_row(db_path, "mt_board_public") is None
    assert _board_row(db_path, "mt_board_authed") is None

    pub = anon.get("/get-nodes", headers=GZIP)
    auth = authed.get("/get-nodes", headers=GZIP)

    assert pub.status_code == 200 and auth.status_code == 200
    assert pub.json()["repeaters"][0]["team"] is None
    assert auth.json()["repeaters"][0]["team"] == "BLUE"


@pytest.mark.parametrize("published", [False, True])
def test_signed_in_validator_never_304s_the_signed_out_variant(anon, authed, db_path, published):
    """With 304 now working on this route, a validator minted against the
    signed-in body (which carries team attribution) must not validate the
    signed-out one -- not whether the two shapes come from inline builds
    or from the worker's rows. The etag is a hash of each variant's own
    bytes, so they differ whenever the content does."""
    _seed_mt_board(db_path)
    if published:
        asyncio.run(mc_api_module._publish_board_once())
        mc_api_module._BOARD_CACHE.clear()

    auth = authed.get("/get-nodes", headers=GZIP)
    pub = anon.get("/get-nodes", headers=GZIP)
    assert auth.headers["etag"] != pub.headers["etag"]

    replay = anon.get("/get-nodes", headers={**GZIP, "If-None-Match": auth.headers["etag"]})
    assert replay.status_code == 200
    assert replay.json()["repeaters"][0]["team"] is None
