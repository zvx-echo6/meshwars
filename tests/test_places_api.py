"""Tests for app/places_api.py's active-flag filtering: an inactive
place (app/places_seed.py's reconcile flag, set when a place leaves the
seed) must never appear in the viewport or "near here" panel response,
even when a stale place_week row still points at it (a rotating place
drawn earlier in the week, then deactivated by a later seed reload).
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi import Request

import app.places_api as places_api_module
from app.place_rotation import current_week_start
from app.places_api import places_in_viewport, places_near

WEEK = current_week_start()


@pytest.fixture(autouse=True)
def _reset_places_cache():
    """_PLACES_CACHE is a module-level singleton (see app/places_api.py's
    response cache). Left dirty, a later test using the same viewport/
    near-point params (several tests below all query the same
    north=44/south=42/west=-117/east=-115 box) would silently get an
    earlier test's cached response instead of running its own query
    against its own rows. Same pattern tests/test_privacy_hardening.py's
    _reset_rate_limiters_and_cache uses for app/mc_api.py's _BOARD_CACHE.
    """
    places_api_module._PLACES_CACHE.clear()
    yield
    places_api_module._PLACES_CACHE.clear()


class _NonClosingConn:
    """Wraps a shared in-memory `conn` fixture so places_api's
    connect()-then-close() lifecycle doesn't leave a dead handle when a
    test calls a handler more than once against the same `conn`. Pulled
    out as a shared helper since both the pre-existing determinism test
    below and the new cache tests need it.
    """

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


def _request(if_none_match: str | None = None) -> Request:
    """A minimal Request carrying just enough of an ASGI scope for
    cached_places_response to read If-None-Match off it -- same idea as
    tests/test_auth.py's own _request() helper, which builds a bare
    Request the same way to test code that reads request headers
    without a running server.
    """
    headers = []
    if if_none_match is not None:
        headers.append((b"if-none-match", if_none_match.encode("latin-1")))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "http_version": "1.1",
        "headers": headers,
    }
    return Request(scope)


def _place(conn, place_id, ref_type, lat, lon, points, rotates=0, active=1):
    conn.execute(
        "INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, "
        "rotates, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (place_id, ref_type, f"ref-{place_id}", f"place-{place_id}", lat, lon,
         points, "TEST", rotates, active, int(time.time())),
    )


def test_inactive_place_excluded_from_viewport(conn, monkeypatch):
    monkeypatch.setattr(places_api_module, "connect", lambda: conn)

    _place(conn, 1, "summit", 43.0, -116.0, points=100, rotates=0, active=1)
    _place(conn, 2, "summit", 43.01, -116.01, points=100, rotates=0, active=0)

    result = asyncio.run(
        places_in_viewport(request=_request(), north=44.0, south=42.0, west=-117.0, east=-115.0)
    )
    ids = {p["id"] for p in json.loads(result.body)["places"]}
    assert ids == {1}


def test_inactive_place_excluded_even_with_a_stale_place_week_row(conn, monkeypatch):
    """A rotating place drawn into this week's place_week, then
    deactivated by a later seed reload, must not still show up just
    because place_week (append-only, never rewritten) still names it.
    """
    monkeypatch.setattr(places_api_module, "connect", lambda: conn)

    _place(conn, 1, "landmark", 43.0, -116.0, points=5, rotates=1, active=0)
    _place(conn, 2, "landmark", 43.02, -116.02, points=5, rotates=1, active=1)
    conn.execute("INSERT INTO place_week(week_start, place_id) VALUES (?, ?)", (WEEK, 1))
    conn.execute("INSERT INTO place_week(week_start, place_id) VALUES (?, ?)", (WEEK, 2))

    result = asyncio.run(
        places_in_viewport(request=_request(), north=44.0, south=42.0, west=-117.0, east=-115.0)
    )
    ids = {p["id"] for p in json.loads(result.body)["places"]}
    assert ids == {2}


def test_inactive_place_excluded_from_near_panel(conn, monkeypatch):
    monkeypatch.setattr(places_api_module, "connect", lambda: conn)

    _place(conn, 1, "landmark", 43.0, -116.0, points=5, rotates=0, active=1)
    _place(conn, 2, "landmark", 43.001, -116.001, points=5, rotates=0, active=0)

    result = asyncio.run(places_near(request=_request(), lat=43.0, lon=-116.0, limit=20))
    ids = {p["id"] for p in json.loads(result.body)["places"]}
    assert ids == {1}


def test_capped_viewport_thins_evenly_not_by_insertion_order(conn, monkeypatch):
    """The bug this endpoint shipped with: every SOTA summit is worth
    the same 100 points, so `ORDER BY points DESC` alone is not a total
    order and SQLite broke the tie by insertion order -- which, in
    production, follows the seed CSV's SOTA-association sort (W0C
    Colorado ... W7Y Wyoming, see app/places_seed.py). A capped,
    zoomed-out viewport then kept everything up to about Oregon and
    silently dropped the alphabetic tail.

    Reproduced here with two equal-sized, equal-points "regions" --
    ids 1-100 inserted first, ids 101-200 inserted second, all tied on
    points, all in the same viewport -- and a cap below the combined
    total. The old `ORDER BY p.points DESC LIMIT ?` (no further
    tiebreak) would return ids 1-50 only: pure insertion order, one
    region entirely and the other not at all. The fix's tiebreak
    (_stable_tiebreak, a deterministic hash of id) must scatter the
    truncated result across BOTH regions instead.
    """
    monkeypatch.setattr(places_api_module, "connect", lambda: conn)
    monkeypatch.setattr(places_api_module, "MAX_VIEWPORT_RESULTS", 50)

    for i in range(1, 101):
        _place(conn, i, "summit", 43.0, -116.0, points=100)
    for i in range(101, 201):
        _place(conn, i, "summit", 43.0, -116.0, points=100)

    result = asyncio.run(
        places_in_viewport(request=_request(), north=44.0, south=42.0, west=-117.0, east=-115.0)
    )
    data = json.loads(result.body)
    ids = [p["id"] for p in data["places"]]

    assert data["count"] == 50
    assert data["truncated"] is True
    assert any(i <= 100 for i in ids), "first-inserted region must not be the only one dropped"
    assert any(i > 100 for i in ids), "second-inserted region must not be entirely truncated away"


def test_capped_viewport_is_deterministic_across_repeated_calls(conn, monkeypatch):
    """Same viewport, same tied-points rows -> same truncated subset in
    the same order every time. A capped view that reshuffled on every
    call would make markers flicker as a player pans the map -- the
    tiebreak must be a pure function of `id`, never randomness.

    Caching disabled here (places_cache_seconds=0): this test is about
    the underlying query's own determinism, not about the response
    cache trivially replaying identical bytes on the second call --
    forcing both calls to actually rebuild keeps it a real proof of
    _stable_tiebreak rather than a proof of the cache.
    """
    monkeypatch.setattr(places_api_module, "connect", lambda: _NonClosingConn(conn))
    monkeypatch.setattr(places_api_module, "MAX_VIEWPORT_RESULTS", 50)
    monkeypatch.setattr(places_api_module.settings, "places_cache_seconds", 0)

    for i in range(1, 201):
        _place(conn, i, "summit", 43.0, -116.0, points=100)

    result_a = asyncio.run(
        places_in_viewport(request=_request(), north=44.0, south=42.0, west=-117.0, east=-115.0)
    )
    result_b = asyncio.run(
        places_in_viewport(request=_request(), north=44.0, south=42.0, west=-117.0, east=-115.0)
    )

    ids_a = [p["id"] for p in json.loads(result_a.body)["places"]]
    ids_b = [p["id"] for p in json.loads(result_b.body)["places"]]
    assert ids_a == ids_b


def test_park_boundaries_also_thin_evenly_not_by_insertion_order(conn, monkeypatch):
    """_park_boundaries_in_viewport has the identical points-tie flaw at
    its own MAX_BOUNDARY_RESULTS cap -- same fix, same proof shape as
    the viewport-markers test above, just with boundary-backed parks
    (geom set, rotates=0) instead of summits.
    """
    monkeypatch.setattr(places_api_module, "connect", lambda: conn)
    monkeypatch.setattr(places_api_module, "MAX_BOUNDARY_RESULTS", 20)

    point_wkt = "POLYGON((-116.1 42.9,-116.1 43.1,-115.9 43.1,-115.9 42.9,-116.1 42.9))"
    for i in range(1, 41):
        conn.execute(
            "INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, "
            "geom, rotates, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, "park", f"ref-{i}", f"park-{i}", 43.0, -116.0, 10, "TEST",
             point_wkt, 0, 1, int(time.time())),
        )

    features = places_api_module._park_boundaries_in_viewport(
        conn, north=44.0, south=42.0, west=-117.0, east=-115.0
    )
    ids = [f["properties"]["id"] for f in features]

    assert len(ids) == 20
    assert any(i <= 20 for i in ids), "first-inserted half must not be the only one dropped"
    assert any(i > 20 for i in ids), "second-inserted half must not be entirely truncated away"


def test_park_boundary_properties_include_type_for_the_shared_popup(conn, monkeypatch):
    """frontend/map2.js's boundary click handler feeds a boundary
    feature's properties straight into the same showPlacePopup() a
    place marker uses -- name/type/points. `type` must come back as
    "park" (there is no ref_type column on the feature itself, since
    the query is already scoped to ref_type = 'park') or that popup
    would render "undefined" where a marker click shows "park".
    """
    monkeypatch.setattr(places_api_module, "connect", lambda: conn)

    point_wkt = "POLYGON((-116.1 42.9,-116.1 43.1,-115.9 43.1,-115.9 42.9,-116.1 42.9))"
    conn.execute(
        "INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, "
        "geom, rotates, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "park", "ref-1", "Test Park", 43.0, -116.0, 50, "TEST",
         point_wkt, 0, 1, int(time.time())),
    )

    features = places_api_module._park_boundaries_in_viewport(
        conn, north=44.0, south=42.0, west=-117.0, east=-115.0
    )

    assert features[0]["properties"] == {"id": 1, "name": "Test Park", "points": 50, "type": "park"}


# ---- response cache (app/places_api.py's _PLACES_CACHE) ------------------


def _counting_connect(monkeypatch, conn):
    """Monkeypatches places_api_module.connect to a non-closing wrapper
    around `conn` that also counts how many times it was called -- i.e.
    how many times build() actually ran a fresh query, as opposed to
    being served straight from _PLACES_CACHE. Returns the counter dict
    (its "n" key holds the running total).
    """
    calls = {"n": 0}

    def _connect():
        calls["n"] += 1
        return _NonClosingConn(conn)

    monkeypatch.setattr(places_api_module, "connect", _connect)
    return calls


def test_repeated_viewport_request_within_ttl_uses_cache_not_db(conn, monkeypatch):
    """A second, identical /api/places request inside places_cache_seconds
    must be served from _PLACES_CACHE -- no second connect(), no second
    query -- and must return the exact same bytes as the first.
    """
    calls = _counting_connect(monkeypatch, conn)
    _place(conn, 1, "summit", 43.0, -116.0, points=100)

    result_a = asyncio.run(
        places_in_viewport(request=_request(), north=44.0, south=42.0, west=-117.0, east=-115.0)
    )
    result_b = asyncio.run(
        places_in_viewport(request=_request(), north=44.0, south=42.0, west=-117.0, east=-115.0)
    )

    assert calls["n"] == 1
    assert result_a.body == result_b.body


def test_near_requests_within_rounding_threshold_share_one_query(conn, monkeypatch):
    """Two /api/places/near requests whose lat/lon differ only in the
    5th decimal place round to the same 3-decimal-place cache key, so
    the second request is served from cache -- one underlying query
    covers both.
    """
    calls = _counting_connect(monkeypatch, conn)
    _place(conn, 1, "summit", 43.0, -116.0, points=100)

    asyncio.run(places_near(request=_request(), lat=43.00001, lon=-116.00001, limit=20))
    asyncio.run(places_near(request=_request(), lat=43.00004, lon=-116.00004, limit=20))

    assert calls["n"] == 1


def test_near_requests_above_rounding_threshold_produce_two_queries(conn, monkeypatch):
    """Two /api/places/near requests whose lat/lon differ enough to
    round to different 3-decimal-place cache keys must each run their
    own query -- the cache must never collapse genuinely different
    points together.
    """
    calls = _counting_connect(monkeypatch, conn)
    _place(conn, 1, "summit", 43.0, -116.0, points=100)

    asyncio.run(places_near(request=_request(), lat=43.000, lon=-116.000, limit=20))
    asyncio.run(places_near(request=_request(), lat=43.010, lon=-116.010, limit=20))

    assert calls["n"] == 2


def test_if_none_match_returns_304(conn, monkeypatch):
    """A repeat request carrying the ETag the first response returned
    gets a 304 with that same ETag, same contract as app/mc_api.py's
    cached_json_response.
    """
    monkeypatch.setattr(places_api_module, "connect", lambda: conn)
    _place(conn, 1, "summit", 43.0, -116.0, points=100)

    result = asyncio.run(
        places_in_viewport(request=_request(), north=44.0, south=42.0, west=-117.0, east=-115.0)
    )
    etag = result.headers["etag"]
    assert etag

    result2 = asyncio.run(
        places_in_viewport(
            request=_request(if_none_match=etag),
            north=44.0, south=42.0, west=-117.0, east=-115.0,
        )
    )
    assert result2.status_code == 304
    assert result2.headers["etag"] == etag


def test_zero_ttl_disables_places_cache(conn, monkeypatch):
    """places_cache_seconds=0 must bypass _PLACES_CACHE entirely: two
    identical requests run two independent queries, neither response
    carries a Cache-Control header, and nothing is left in the cache.
    """
    calls = _counting_connect(monkeypatch, conn)
    monkeypatch.setattr(places_api_module.settings, "places_cache_seconds", 0)
    _place(conn, 1, "summit", 43.0, -116.0, points=100)

    result_a = asyncio.run(
        places_in_viewport(request=_request(), north=44.0, south=42.0, west=-117.0, east=-115.0)
    )
    result_b = asyncio.run(
        places_in_viewport(request=_request(), north=44.0, south=42.0, west=-117.0, east=-115.0)
    )

    assert calls["n"] == 2
    assert "cache-control" not in result_a.headers
    assert "cache-control" not in result_b.headers
    assert len(places_api_module._PLACES_CACHE) == 0


def test_places_cache_eviction_bounded_at_max(conn, monkeypatch):
    """Enough distinct /api/places/near cache keys to exceed
    _PLACES_CACHE_MAX must not grow the cache past that cap -- the
    oldest entries are evicted (OrderedDict.popitem(last=False)), not
    accumulated forever.
    """
    monkeypatch.setattr(places_api_module, "connect", lambda: _NonClosingConn(conn))

    over_cap = places_api_module._PLACES_CACHE_MAX + 50
    for i in range(over_cap):
        asyncio.run(places_near(request=_request(), lat=i * 0.01, lon=-116.0, limit=1))

    assert len(places_api_module._PLACES_CACHE) == places_api_module._PLACES_CACHE_MAX
