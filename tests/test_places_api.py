"""Tests for app/places_api.py's active-flag filtering: an inactive
place (app/places_seed.py's reconcile flag, set when a place leaves the
seed) must never appear in the viewport or "near here" panel response,
even when a stale place_week row still points at it (a rotating place
drawn earlier in the week, then deactivated by a later seed reload).
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import time

import pytest
from fastapi import Request

import app.places_api as places_api_module
from app.place_rotation import _prev_week_start, current_week_start
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


def _request(if_none_match: str | None = None, accept_gzip: bool = False) -> Request:
    """A minimal Request carrying just enough of an ASGI scope for
    cached_places_response to read If-None-Match (and, when asked,
    Accept-Encoding) off it -- same idea as tests/test_auth.py's own
    _request() helper, which builds a bare Request the same way to test
    code that reads request headers without a running server.
    """
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


# ---- the weekly draw is probed, not fetched (2026-10-05) -------------------
#
# Both routes called resolve_week() on every cache miss, and it fetched the
# week's whole id list -- about 500,000 rows, 94-98% of a ~0.5 s miss --
# only for the routes to throw it away. They call ensure_week_resolved()
# now. `counting_conn` (tests/conftest.py) records how many rows each
# statement pulled back.


def test_viewport_and_near_probe_the_week_instead_of_fetching_it(conn, counting_conn, monkeypatch):
    monkeypatch.setattr(places_api_module, "connect", lambda: counting_conn)
    _place(conn, 1, "summit", 43.0, -116.0, points=100)
    conn.executemany(
        "INSERT INTO place_week(week_start, place_id) VALUES (?, ?)",
        [(WEEK, 9000 + i) for i in range(60)],
    )

    asyncio.run(places_in_viewport(request=_request(), north=44.0, south=42.0, west=-117.0, east=-115.0))
    asyncio.run(places_near(request=_request(), lat=43.0, lon=-116.0, limit=20))

    # One probe per route, each stopping at the first of the draw's 60 rows.
    assert counting_conn.week_reads(WEEK) == [1, 1]


# ---- claimed_by_team is attached AFTER the LIMIT (2026-10-05) ----------------
#
# The viewport used to look up every candidate's newest claim and only then
# cut the list to MAX_VIEWPORT_RESULTS -- ~42,500 lookups at zoom 5 for the
# 2,000 rows kept. It now picks the surviving rows first and looks claims up
# for those alone. What comes back must be exactly what it always was: the
# same rows, in the same order, with the same team. The expectations below
# are written out by hand from _seed_claims_fixture(), not produced by
# running either version of the query.

_BOX = dict(north=44.0, south=42.0, west=-117.0, east=-115.0)

# id -> (type, lat, lon, points, rotates) for the eight live places.
_LIVE_PLACES = {
    1: ("summit", 43.01, -116.01, 100, False),
    2: ("summit", 43.02, -116.02, 100, False),
    3: ("park", 43.03, -116.03, 50, False),
    4: ("park", 43.04, -116.04, 50, False),
    5: ("park", 43.05, -116.05, 50, False),
    6: ("landmark", 43.06, -116.06, 10, False),
    7: ("landmark", 43.07, -116.07, 5, False),
    8: ("landmark", 43.08, -116.08, 50, True),
}


def _expected_places(*id_and_team):
    """The `places` list /api/places should return for these
    (id, claimed_by_team) pairs, in this order -- spelled out from
    _LIVE_PLACES, key order and all, so it can also be compared against
    the response's raw bytes."""
    out = []
    for place_id, team in id_and_team:
        ref_type, lat, lon, points, rotates = _LIVE_PLACES[place_id]
        out.append({
            "id": place_id, "type": ref_type, "name": f"place-{place_id}",
            "lat": lat, "lon": lon, "points": points, "rotates": rotates,
            "claimed_by_team": team,
        })
    return out


def _seed_claims_fixture(conn):
    """Twelve places around the box north=44/south=42/west=-117/east=-115:
    eight live, four decoys that must never come back. Every decoy is worth
    100 points, the top tier, so one that leaked in would show up at the
    front of the list.

    LIVE, ranked by points DESC then _stable_tiebreak(id), i.e.
    (id * 2654435761) % 1000000007 ascending:

      100 points: id 2 (308871487), id 1 (654435747)
       50 points: id 8 (235485941; rotating, in this week's draw),
                  id 5 (272178714), id 4 (617742974), id 3 (963307234)
       10 points: id 6
        5 points: id 7

    so the full order is 2, 1, 8, 5, 4, 3, 6, 7 -- and the tiebreak, not
    id order, is what puts 8 before 5 before 4 before 3.

    DECOYS:
      9   rotating, in no draw at all              -> not live this week
      10  always-active, but active = 0            -> left the seed
      11  always-active and active, but lat 46     -> outside the box
      12  rotating, drawn only LAST week           -> not live this week

    CLAIMS (place_activation), newest awarded_at first, per board
    'mc' (meshcore) / 'mt' (meshtastic); teams are 10 = RED, 11 = BLUE:

      2  mc 2000 BLUE | mc 1000 RED | mt 3000 RED     mc -> BLUE, mt -> RED
      1  mt 4000 BLUE                                  mc -> none, mt -> BLUE
      8  mc 5000 RED                                   mc -> RED, mt -> none
      5  mc 7000 BLUE (inserted FIRST) | mc 6000 RED (inserted second)
         | mc 9000 by player 99, who does not exist
                                                       mc -> BLUE, mt -> none
         (newest by TIME among claimants that still exist: not the last
         inserted, and not player 99's newer row, which the join to
         `player` drops)
      4  mt 8000 RED                                   mc -> none, mt -> RED
      3, 6, 7  never claimed                           none on both boards
    """
    def place(place_id, ref_type, lat, lon, points, rotates=0, active=1):
        _place(conn, place_id, ref_type, lat, lon, points, rotates=rotates, active=active)

    place(1, "summit", 43.01, -116.01, 100)
    place(2, "summit", 43.02, -116.02, 100)
    place(3, "park", 43.03, -116.03, 50)
    place(4, "park", 43.04, -116.04, 50)
    place(5, "park", 43.05, -116.05, 50)
    place(6, "landmark", 43.06, -116.06, 10)
    place(7, "landmark", 43.07, -116.07, 5)
    place(8, "landmark", 43.08, -116.08, 50, rotates=1)
    place(9, "landmark", 43.09, -116.09, 100, rotates=1)
    place(10, "summit", 43.10, -116.10, 100, active=0)
    place(11, "summit", 46.00, -116.00, 100)
    place(12, "landmark", 43.12, -116.12, 100, rotates=1)

    conn.executemany(
        "INSERT INTO place_week(week_start, place_id) VALUES (?, ?)",
        [(WEEK, 8), (_prev_week_start(WEEK), 12)],
    )
    for player_id, team in ((10, "RED"), (11, "BLUE")):
        conn.execute(
            "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?, ?, ?, ?)",
            (player_id, f"player-{player_id}", team, int(time.time())),
        )
    # week_start only has to keep UNIQUE(place_id, player_id, week_start)
    # satisfied, so "an old week" is any string that is not WEEK.
    old_a, old_b = "2020-01-01", "2020-01-08"
    conn.executemany(
        "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at, protocol) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (2, 10, old_a, 100, 1000, "mc"),
            (2, 11, old_b, 100, 2000, "mc"),
            (2, 10, WEEK, 100, 3000, "mt"),
            (1, 11, WEEK, 100, 4000, "mt"),
            (8, 10, WEEK, 50, 5000, "mc"),
            (5, 11, old_b, 50, 7000, "mc"),
            (5, 10, old_a, 50, 6000, "mc"),
            (5, 99, WEEK, 50, 9000, "mc"),
            (4, 10, WEEK, 50, 8000, "mt"),
        ],
    )


def test_viewport_keeps_the_right_rows_and_claims_when_candidates_exceed_the_limit(conn, monkeypatch):
    """Eight live candidates, a limit of four, the meshcore board. The
    cut falls inside the 50-point tier, so which of its four members
    survive is decided by the tiebreak alone: 8 and 5 stay, 4 and 3 go.
    """
    monkeypatch.setattr(places_api_module, "connect", lambda: _NonClosingConn(conn))
    monkeypatch.setattr(places_api_module, "MAX_VIEWPORT_RESULTS", 4)
    _seed_claims_fixture(conn)

    result = asyncio.run(places_in_viewport(request=_request(), **_BOX))
    data = json.loads(result.body)

    expected = _expected_places((2, "BLUE"), (1, None), (8, "RED"), (5, "BLUE"))
    assert data["places"] == expected
    assert data["count"] == 4
    assert data["truncated"] is True
    # ...and byte for byte, key order and number formatting included.
    expected_bytes = json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert expected_bytes in result.body


def test_viewport_claims_follow_the_requested_board(conn, monkeypatch):
    """Same rows, same order -- claims never change which places are kept
    or how they rank -- but each place's team is its newest claim on THE
    BOARD ASKED FOR: place 2's newest claim overall is a meshtastic one
    (RED), its newest meshcore one is BLUE.
    """
    monkeypatch.setattr(places_api_module, "connect", lambda: _NonClosingConn(conn))
    monkeypatch.setattr(places_api_module, "MAX_VIEWPORT_RESULTS", 4)
    _seed_claims_fixture(conn)

    result = asyncio.run(places_in_viewport(request=_request(), board="meshtastic", **_BOX))
    data = json.loads(result.body)

    assert data["places"] == _expected_places((2, "RED"), (1, "BLUE"), (8, None), (5, None))
    assert data["count"] == 4
    assert data["truncated"] is True


def test_viewport_full_order_when_nothing_is_cut(conn, monkeypatch):
    """The default limit (2000) is far above eight places: every live
    place comes back, the decoys do not, and the order is the full
    points-then-tiebreak ranking worked out in _seed_claims_fixture()."""
    monkeypatch.setattr(places_api_module, "connect", lambda: _NonClosingConn(conn))
    _seed_claims_fixture(conn)

    result = asyncio.run(places_in_viewport(request=_request(), **_BOX))
    data = json.loads(result.body)

    assert data["places"] == _expected_places(
        (2, "BLUE"), (1, None), (8, "RED"), (5, "BLUE"), (4, None), (3, None), (6, None), (7, None),
    )
    assert data["count"] == 8
    assert data["truncated"] is False


def test_viewport_claim_lookup_sits_outside_the_limit(conn, counting_conn, monkeypatch):
    """The point of the change, pinned structurally: the select that
    applies ORDER BY ... LIMIT must not mention place_activation at all,
    and the claim lookup must still be in the statement, outside it. A
    flat select (claim subquery beside the LIMIT) would run that lookup
    for every candidate in the box before the LIMIT cut them down.
    """
    monkeypatch.setattr(places_api_module, "connect", lambda: counting_conn)
    _place(conn, 1, "summit", 43.0, -116.0, points=100)

    asyncio.run(places_in_viewport(request=_request(), **_BOX))

    [sql] = [rec["sql"] for rec in counting_conn.statements if "claimed_by_team" in rec["sql"]]
    start = sql.index("FROM (SELECT")
    inner = sql[start:sql.index(") AS t", start)]
    assert "LIMIT ?" in inner
    assert "place_activation" not in inner
    assert "place_activation" in sql


# ---- _PLACES_CACHE is bounded in BYTES as well as entries (2026-10-05) -----
#
# 512 entries of a few hundred KB each (plus a gzip copy of each) is far
# more than one web worker should hold, so _PLACES_CACHE_MAX_BYTES bounds
# what the entries actually weigh, alongside the entry cap, and eviction is
# least-recently-used until the cache is under both.


def _body_len(payload) -> int:
    """How many bytes cached_places_response stores for `payload` as the
    entry's plaintext body."""
    return len(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))


def _serve(key, payload, accept_gzip=False):
    """Run `payload` through cached_places_response under `key`, with a
    60 s ttl, the way a route would."""
    return places_api_module.cached_places_response(
        key, 60, lambda: payload, _request(accept_gzip=accept_gzip)
    )


def test_places_cache_limits_default_to_512_entries_and_24_mib():
    assert places_api_module._PLACES_CACHE_MAX == 512
    assert places_api_module._PLACES_CACHE_MAX_BYTES == 24 * 1024 * 1024


def test_places_cache_byte_bound_evicts_the_oldest_entries_first(monkeypatch):
    payload = {"pad": "x" * 1000}
    size = _body_len(payload)
    # Room for exactly three bodies. Plain requests, so no gzip copies yet.
    monkeypatch.setattr(places_api_module, "_PLACES_CACHE_MAX_BYTES", 3 * size)

    for i in range(5):
        _serve(f"k{i}", payload)

    assert list(places_api_module._PLACES_CACHE) == ["k2", "k3", "k4"]
    assert places_api_module._places_cache_bytes() == 3 * size


def test_places_cache_byte_bound_evicts_the_least_recently_used_entry(monkeypatch):
    """Eviction order is by USE, not by when an entry went in: a hit on
    "a" makes it the newest, so the entry that goes is "b"."""
    payload = {"pad": "x" * 1000}
    monkeypatch.setattr(places_api_module, "_PLACES_CACHE_MAX_BYTES", 3 * _body_len(payload))

    for key in ("a", "b", "c"):
        _serve(key, payload)
    _serve("a", payload)  # a hit, inside the ttl
    _serve("d", payload)  # over the bound: the least recently used entry goes

    assert list(places_api_module._PLACES_CACHE) == ["c", "a", "d"]


def test_places_cache_byte_bound_counts_the_gzip_copy_when_it_is_made(monkeypatch):
    """An entry's gzip copy is made lazily, on the first gzip-accepting
    request -- after the entry is already stored -- so the bound has to be
    enforced again at that point, not only when entries go in. The bound
    here leaves room for one entry with its gzip copy plus one more bare
    body, but not for two gzip copies.
    """
    # Hex digests: they compress, but nowhere near to nothing.
    payload = {"pad": "".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(16))}
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    body_size = len(body)
    gzip_size = len(gzip.compress(body, compresslevel=places_api_module._GZIP_COMPRESSLEVEL))
    assert 0 < gzip_size < body_size
    monkeypatch.setattr(places_api_module, "_PLACES_CACHE_MAX_BYTES", 2 * body_size + gzip_size)

    _serve("a", payload, accept_gzip=True)
    assert places_api_module._places_cache_bytes() == body_size + gzip_size

    resp = _serve("b", payload, accept_gzip=True)
    # b went in fine (a with its gzip copy, plus b's bare body, is under
    # the bound), and then b's own gzip copy took the total over it, so
    # the least recently used entry, a, went.
    assert list(places_api_module._PLACES_CACHE) == ["b"]
    assert places_api_module._places_cache_bytes() == body_size + gzip_size
    assert gzip.decompress(resp.body) == body  # and b was still answered correctly

    # The same two entries asked for WITHOUT gzip hold no gzip copies, so
    # both fit: it really was the gzip bytes that tipped it.
    places_api_module._PLACES_CACHE.clear()
    _serve("a", payload)
    _serve("b", payload)
    assert list(places_api_module._PLACES_CACHE) == ["a", "b"]


def test_places_cache_entry_cap_still_holds_alongside_the_byte_bound(monkeypatch):
    """The byte bound is added to the entry cap, not swapped for it: with
    the byte bound far out of reach, entries are still capped by count."""
    monkeypatch.setattr(places_api_module, "_PLACES_CACHE_MAX", 3)

    for i in range(5):
        _serve(f"k{i}", {"n": i})

    assert list(places_api_module._PLACES_CACHE) == ["k2", "k3", "k4"]


def test_places_cache_entry_bigger_than_the_byte_bound_is_served_but_not_kept(monkeypatch):
    payload = {"pad": "x" * 1000}
    monkeypatch.setattr(places_api_module, "_PLACES_CACHE_MAX_BYTES", _body_len(payload) - 1)

    resp = _serve("big", payload)

    assert json.loads(resp.body) == payload  # answered, and correctly
    assert len(places_api_module._PLACES_CACHE) == 0
    assert places_api_module._places_cache_bytes() == 0


def test_places_cache_expired_entry_is_replaced_not_double_counted():
    """A key whose ttl has lapsed is rebuilt over the old entry. The
    cache's weight is whatever is in it now, never the old and the new
    entry added together."""
    _serve("a", {"pad": "x" * 500})
    places_api_module._PLACES_CACHE["a"].built_at -= 1000  # well past the 60 s ttl

    second = {"pad": "y" * 900}
    _serve("a", second)

    assert list(places_api_module._PLACES_CACHE) == ["a"]
    assert places_api_module._places_cache_bytes() == _body_len(second)
