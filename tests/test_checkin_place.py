"""Check-in placement (app/checkin_place.py) and its backfill, with the
CoreScope and Beacon feeds mocked from recorded-shape fixtures: resolving a
position from a path (lowest hop count first, never an ambiguous prefix,
heard-directly observers), the estimates for awards whose path is gone and
the order they are tried in, which awards are picked up, regional check-in
scoring, and the backfill's dry-run and idempotency."""
from __future__ import annotations

import asyncio
import importlib.util
import io
import sqlite3
import time
from pathlib import Path

import httpx
import pytest

import app.db as db
from app import checkin_place as cp
from app import checkin_place_backfill as backfill
from app import mc_scoring, regions
from app.db import MIGRATIONS, SCHEMA
from app.grid import cell_center, cell_id as grid_cell_id

_spec = importlib.util.spec_from_file_location("checkin_place_fixtures", Path(__file__).parent / "fixtures" / "checkin_place.py")
fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fx)

NOW = int(time.time())
CS_URL = "https://cs.test"
BC_URL = "https://bc.test"


# ---- fake feeds ----------------------------------------------------------------


def _handler(log: list):
    def handler(req: httpx.Request) -> httpx.Response:
        host = f"{req.url.scheme}://{req.url.host}"
        path, q = req.url.path, dict(req.url.params)
        log.append((host, path, q))
        if host == CS_URL:
            if path.startswith("/api/channels/") and path.endswith("/messages"):
                return httpx.Response(200, json=fx.CS_MESSAGES if q.get("offset") == "0" else {"messages": [], "total": 4})
            if path.startswith("/api/traces/"):
                t = fx.CS_TRACES.get(path.rsplit("/", 1)[1])
                return httpx.Response(200, json=t) if t else httpx.Response(404)
            if path == "/api/observers":
                return httpx.Response(200, json=fx.CS_OBSERVERS)
            if path == "/api/resolve-hops":
                return httpx.Response(200, json={"resolved": {h: fx.CS_RESOLVE[h] for h in q["hops"].split(",") if h in fx.CS_RESOLVE}})
        if host == BC_URL:
            if path == "/api/v1/channels":
                return httpx.Response(200, json=fx.BEACON_CHANNELS)
            if path == "/api/v1/channels/7/messages":
                return httpx.Response(200, json=fx.BEACON_PAGE_2 if q.get("cursor") == "c2" else fx.BEACON_PAGE_1)
            if path.startswith("/api/v1/packets/"):
                p = fx.BEACON_PACKETS.get(path.rsplit("/", 1)[1])
                return httpx.Response(200, json=p) if p else httpx.Response(404)
        return httpx.Response(404)

    return handler


def make_placer(log=None, handler=None):
    log = [] if log is None else log

    def factory(url):
        return cp.HostClient(url, client=httpx.AsyncClient(base_url=url, transport=httpx.MockTransport(handler or _handler(log))),
                             min_interval=0)

    return cp.Placer(client_factory=factory), log


def award(kind="corescope", message_id="101", url=None, channel=None, ts=NOW, **extra):
    base = {"kind": kind, "connector_url": url or (CS_URL if kind == "corescope" else BC_URL),
            "channel": channel or ("#weekly-net" if kind == "corescope" else "#central-oregon"),
            "message_id": message_id, "message_ts": ts, "season_id": 1, "player_id": 1, "net_date": "2026-10-07",
            "net_id": 1, "protocol": "mc", "awarded_at": NOW, "p_attempts": 0}
    base.update(extra)
    return base


def trace(a):
    p, _ = make_placer()
    return asyncio.run(p.trace(a))


# ---- CoreScope -------------------------------------------------------------------


def test_corescope_first_hop():
    r = trace(award(message_id="101"))
    assert (r.method, r.lat, r.lon) == ("first_hop", 43.585, -116.795)
    assert "Highland 3" in r.detail and "1FD3" in r.detail and "unique" in r.detail
    assert r.source_hash == "HASH_FIRSTHOP"


def test_corescope_heard_directly_uses_the_observer_position():
    r = trace(award(message_id="102"))
    assert (r.method, r.lat, r.lon) == ("observer", 43.5, -116.2) and "Obs A" in r.detail


def test_corescope_never_guesses_an_ambiguous_prefix():
    assert trace(award(message_id="103")) is None


def test_corescope_takes_the_observation_with_the_fewest_hops():
    log = []
    p, _ = make_placer(log)
    r = asyncio.run(p.trace(award(message_id="104")))
    assert (r.method, r.lat) == ("first_hop", 43.585)
    asked = [q["hops"] for _, path, q in log if path == "/api/resolve-hops"]
    assert asked == ["1FD3"]            # the 3-hop observation was never even resolved


def test_corescope_message_gone_or_trace_gone():
    assert trace(award(message_id="999")) is None
    assert trace(award(message_id="not a number")) is None
    gone = dict(fx.CS_TRACES)
    p, _ = make_placer(handler=lambda req: httpx.Response(404) if "/traces/" in req.url.path else _handler([])(req))
    assert asyncio.run(p.trace(award(message_id="101"))) is None


def test_a_server_error_is_transient_not_not_found():
    p, _ = make_placer(handler=lambda req: httpx.Response(503))
    with pytest.raises(cp.Transient):
        asyncio.run(p.trace(award(message_id="101")))


def test_no_position_is_not_a_position():
    assert cp.usable_point(0, 0) is None and cp.usable_point(None, 1) is None and cp.usable_point(91, 0) is None
    assert cp.usable_point(43.5, -116.2) == (43.5, -116.2)
    # BB22 is unique but publishes no position
    p, _ = make_placer()
    src = p.source("corescope", CS_URL, "#weekly-net")
    assert asyncio.run(src._first_hop_point("BB22")) is None


# ---- Beacon ------------------------------------------------------------------------


def test_beacon_heard_directly_keeps_the_airport_code():
    r = trace(award("beacon", "5003"))
    assert (r.method, r.lat, r.lon, r.region_code) == ("observer_iata", None, None, "BOI")


def test_beacon_first_hop_prefers_fewest_hops_and_high_confidence():
    r = trace(award("beacon", "5002"))
    assert (r.method, r.lat, r.lon) == ("first_hop", 43.6, -116.3) and "Near" in r.detail and "abcd1234" in r.detail


def test_beacon_ambiguous_first_hop_is_refused():
    assert trace(award("beacon", "5001")) is None        # 1-hop is ambiguous; 2-hop first node is ambiguous too (2 nodes)


def test_beacon_pages_back_through_the_cursor_and_stops_past_the_award_time():
    log = []
    p, _ = make_placer(log)
    r = asyncio.run(p.trace(award("beacon", "5001", ts=1_791_000_100)))
    assert r is None
    pages = [q.get("cursor") for _, path, q in log if path.endswith("/messages")]
    assert pages == [None, "c2"]                          # found on page 2 via the cursor
    # an id from before the oldest page is not searched for forever
    log.clear()
    assert asyncio.run(p.trace(award("beacon", "1", ts=1_791_000_000))) is None
    assert [q.get("cursor") for _, path, q in log if path.endswith("/messages")] == []   # index already covers it


def test_beacon_unknown_channel():
    p, _ = make_placer()
    assert asyncio.run(p.trace(award("beacon", "5003", channel="#nope"))) is None


def test_host_client_spaces_requests():
    async def go():
        c = cp.HostClient("https://x.test", client=httpx.AsyncClient(
            base_url="https://x.test", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))), min_interval=0.2)
        t0 = time.monotonic()
        await c.get("/a")
        await c.get("/b")
        return time.monotonic() - t0

    assert asyncio.run(go()) >= 0.19


# ---- database: selection, estimation, scoring ----------------------------------------


def _init(path):
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            c.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                continue
            raise
    c.commit()
    c.close()


@pytest.fixture
def dbp(tmp_path, monkeypatch):
    path = str(tmp_path / "g.db")
    _init(path)
    monkeypatch.setattr(db.settings, "db_path", path)
    monkeypatch.setattr(regions, "_INDEX_CACHE", None)
    c = sqlite3.connect(path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("INSERT INTO mc_season(id, protocol, started_at, ends_at, status) VALUES (1,'mc',?,?, 'active')", (NOW - 10**6, NOW + 10**6))
    for pid, name, team in ((1, "ann", "RED"), (2, "bob", "BLUE")):
        c.execute("INSERT INTO player(player_id, display_name, team, created_at) VALUES (?,?,?,?)", (pid, name, team, NOW))
    for nid, kind, url, ch in ((1, "corescope", CS_URL, "#weekly-net"), (2, "beacon", BC_URL, "#central-oregon"),
                               (3, "corescope", CS_URL, "#other"), (4, "meshview", "https://mv.test", "")):
        c.execute("INSERT INTO checkin_net(id, label, protocol, kind, connector_url, channel, weekday, start_hour, end_hour, "
                  "timezone, created_at) VALUES (?,?,?,?,?,?,2,17,23,'America/Boise',?)",
                  (nid, f"net{nid}", "mt" if kind == "meshview" else "mc", kind, url, ch, NOW))
    yield c
    c.close()


def add_award(c, player=1, net=1, date="2026-10-07", mid="101", at=NOW - 1000, pts=2.0, season=1):
    c.execute("INSERT INTO mc_checkin_award(season_id, player_id, net_date, points, protocol, message_id, awarded_at, "
              "message_ts, net_id) VALUES (?,?,?,?, 'mc', ?, ?, ?, ?)", (season, player, date, pts, mid, at, at, net))


def add_place(c, player=1, date="2026-10-07", method="first_hop", lat=43.6, lon=-116.3, region=None, season=1, attempts=1):
    cell = grid_cell_id(lat, lon) if lat is not None else None
    c.execute("INSERT INTO checkin_award_place(season_id, player_id, net_date, lat, lon, cell_id, region_code, method, detail, "
              "attempts, placed_at, last_attempt_at) VALUES (?,?,?,?,?,?,?,?, 'd', ?, ?, ?)",
              (season, player, date, lat, lon, cell, region, method, attempts, NOW, NOW))


def row_for(c, **kw):
    rows = cp.select_awards(c, replace=True)
    return [r for r in rows if all(r[k] == v for k, v in kw.items())][0]


def test_estimate_prefers_a_traced_checkin_on_the_same_net_over_a_nearer_one_on_another(dbp):
    add_award(dbp, date="2026-10-14", at=NOW)                                   # the one to estimate (net 1)
    add_award(dbp, net=2, date="2026-10-13", mid="5003", at=NOW - 100)          # very near, but net 2
    add_place(dbp, date="2026-10-13", lat=44.0, lon=-115.0)
    add_award(dbp, net=1, date="2026-09-30", at=NOW - 10**5)                    # far, but the same net
    add_place(dbp, date="2026-09-30", lat=43.6, lon=-116.3)
    e = cp.estimate(dbp, row_for(dbp, net_date="2026-10-14"))
    assert e.method == "est_recent_checkin" and (e.lat, e.lon) == (43.6, -116.3)


def test_estimate_nearest_in_time_and_later_wins_a_tie(dbp):
    add_award(dbp, date="2026-10-14", at=NOW)
    add_award(dbp, date="2026-10-07", at=NOW - 500)
    add_place(dbp, date="2026-10-07", lat=43.0, lon=-116.0)
    add_award(dbp, date="2026-10-21", at=NOW + 500)
    add_place(dbp, date="2026-10-21", lat=44.0, lon=-115.0)
    e = cp.estimate(dbp, row_for(dbp, net_date="2026-10-14"))
    assert (e.lat, e.lon) == (44.0, -115.0)                   # equally near: the later one
    dbp.execute("UPDATE mc_checkin_award SET awarded_at = ? WHERE net_date = '2026-10-21'", (NOW + 900,))
    assert cp.estimate(dbp, row_for(dbp, net_date="2026-10-14")).lat == 43.0   # now the earlier one is nearer


def test_estimate_ignores_other_players_and_other_estimates(dbp):
    add_award(dbp, date="2026-10-14", at=NOW)
    add_award(dbp, player=2, date="2026-10-07", at=NOW - 10)
    add_place(dbp, player=2, date="2026-10-07")
    add_award(dbp, date="2026-10-07", at=NOW - 20)
    add_place(dbp, date="2026-10-07", method="est_recent_square")
    e = cp.estimate(dbp, row_for(dbp, player_id=1, net_date="2026-10-14"))
    assert e.method == "unplaced"


def test_estimate_falls_back_to_the_latest_claimed_square_then_unplaced(dbp):
    add_award(dbp, date="2026-10-14", at=NOW)
    a = row_for(dbp, net_date="2026-10-14")
    assert cp.estimate(dbp, a).method == "unplaced"
    old, new = grid_cell_id(42.0, -112.0), grid_cell_id(43.61, -116.2)
    for ts, cid, pid in ((10, old, 1), (20, new, 1), (30, grid_cell_id(47.0, -122.0), 2)):
        dbp.execute("INSERT INTO mc_tile_capture_log(season_id, cell_id, ts, by_player_id, by_team) VALUES (1,?,?,?, 'RED')", (cid, ts, pid))
    e = cp.estimate(dbp, a)
    assert e.method == "est_recent_square" and (e.lat, e.lon) == cell_center(new)


def test_select_awards_only_corescope_and_beacon_and_only_unplaced(dbp):
    add_award(dbp, net=1)
    add_award(dbp, net=4, player=2, mid="x")                                   # meshview: never placed
    add_award(dbp, net=2, player=2, date="2026-10-14", mid="5003")
    add_award(dbp, net=3, player=1, date="2026-10-21", mid="9")
    add_place(dbp, player=1, date="2026-10-21")                                 # already placed
    got = {(r["net_id"], r["player_id"], r["net_date"]) for r in cp.select_awards(dbp)}
    assert got == {(1, 1, "2026-10-07"), (2, 2, "2026-10-14")}
    assert len(cp.select_awards(dbp, replace=True)) == 3
    assert [r["net_id"] for r in cp.select_awards(dbp, net_id=2)] == [2]
    add_place(dbp, player=2, date="2026-10-14", method="unplaced", lat=None, lon=None)
    assert (2, 2, "2026-10-14") in {(r["net_id"], r["player_id"], r["net_date"]) for r in cp.select_awards(dbp)}  # retried


def test_live_selection_waits_then_backs_off_and_stops_at_the_cap(dbp):
    add_award(dbp, at=NOW - 30)
    assert cp.select_awards(dbp, live_now=NOW) == []                           # a fresh award: its trace may not exist yet
    assert len(cp.select_awards(dbp, live_now=NOW + 120)) == 1
    add_place(dbp, method="unplaced", lat=None, lon=None, attempts=1)
    dbp.execute("UPDATE checkin_award_place SET last_attempt_at = ?", (NOW,))
    assert cp.select_awards(dbp, live_now=NOW + 100) == []                     # backoff 300 s x attempts
    assert len(cp.select_awards(dbp, live_now=NOW + 400)) == 1
    dbp.execute("UPDATE checkin_award_place SET attempts = ?", (cp.MAX_ATTEMPTS,))
    assert cp.select_awards(dbp, live_now=NOW + 10**5 - 5000) == []


class FakePlacer:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    async def trace(self, a):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_place_one_writes_a_trace_and_counts_failures_until_it_estimates(dbp):
    add_award(dbp)
    a = row_for(dbp, net_date="2026-10-07")
    hit = cp.PlaceResult("first_hop", 43.6, -116.3, None, "Near abcd", "H")
    assert asyncio.run(cp.place_one(FakePlacer(hit), a, final_after=cp.MAX_ATTEMPTS))[0] == "first_hop"
    r = dbp.execute("SELECT * FROM checkin_award_place").fetchone()
    assert r["method"] == "first_hop" and r["cell_id"] == grid_cell_id(43.6, -116.3) and r["source_hash"] == "H"
    dbp.execute("DELETE FROM checkin_award_place")
    for n in range(1, cp.MAX_ATTEMPTS):
        a = row_for(dbp, net_date="2026-10-07")
        assert asyncio.run(cp.place_one(FakePlacer(None), a, final_after=cp.MAX_ATTEMPTS))[0] == "retry"
        assert dbp.execute("SELECT method, attempts FROM checkin_award_place").fetchone()[:] == ("unplaced", n)
    a = row_for(dbp, net_date="2026-10-07")
    assert asyncio.run(cp.place_one(FakePlacer(None), a, final_after=cp.MAX_ATTEMPTS))[0] == "unplaced"   # nothing to estimate from
    assert dbp.execute("SELECT attempts FROM checkin_award_place").fetchone()[0] == cp.MAX_ATTEMPTS


def test_a_feed_error_in_the_backfill_writes_nothing(dbp):
    add_award(dbp)
    a = row_for(dbp, net_date="2026-10-07")
    assert asyncio.run(cp.place_one(FakePlacer(cp.Transient("boom")), a, final_after=None))[0] == "error"
    assert dbp.execute("SELECT COUNT(*) FROM checkin_award_place").fetchone()[0] == 0


def test_regional_checkin_points_count_only_placed_awards_in_the_region(dbp):
    boise, poca = grid_cell_id(43.6, -116.3), grid_cell_id(42.87, -112.45)
    dbp.execute("INSERT INTO region_cell(region_code, cell_id) VALUES ('BOI', ?), ('PIH', ?)", (boise, poca))
    add_award(dbp, player=1, date="2026-10-07", pts=2.0)
    add_place(dbp, player=1, date="2026-10-07", lat=43.6, lon=-116.3)                       # in BOI
    add_award(dbp, player=1, date="2026-10-14", pts=3.0, mid="b")
    add_place(dbp, player=1, date="2026-10-14", lat=42.87, lon=-112.45)                     # in PIH
    add_award(dbp, player=2, date="2026-10-07", pts=5.0, mid="c")
    add_place(dbp, player=2, date="2026-10-07", method="observer_iata", lat=None, lon=None, region="BOI")   # IATA only
    add_award(dbp, player=2, date="2026-10-14", pts=7.0, mid="d")                           # never placed
    add_award(dbp, player=2, date="2026-10-21", pts=11.0, mid="e")
    add_place(dbp, player=2, date="2026-10-21", method="unplaced", lat=None, lon=None)
    assert mc_scoring.team_checkin_points(dbp, 1, ("BOI",)) == {"RED": 2.0, "BLUE": 5.0}
    assert mc_scoring.team_checkin_points(dbp, 1, ("PIH",)) == {"RED": 3.0}
    assert mc_scoring.team_checkin_points(dbp, 1, ("BOI", "PIH")) == {"RED": 5.0, "BLUE": 5.0}
    assert mc_scoring.team_checkin_points(dbp, 1, ()) == {}
    assert mc_scoring.team_checkin_points(dbp, 1) == {"RED": 5.0, "BLUE": 23.0}             # All regions: everything


def test_placed_cells_are_mapped_into_regions(dbp):
    c = dbp
    c.execute("BEGIN IMMEDIATE")
    box = {"rings": [[(43.5, -116.5), (43.5, -116.0), (43.8, -116.0), (43.8, -116.5)]], "radius_km": None,
           "center_lat": 43.6, "center_lon": -116.2, "shape": "polygon"}
    regions.store_region(c, "BOX", "Box", box, now=1000)
    c.execute("COMMIT")
    add_award(c)
    add_place(c, lat=43.6, lon=-116.3)
    regions.map_new_cells()
    assert c.execute("SELECT region_code FROM region_cell WHERE cell_id = ?", (grid_cell_id(43.6, -116.3),)).fetchone()[0] == "BOX"


# ---- backfill ---------------------------------------------------------------------------


def seed_backfill(c):
    add_award(c, player=1, net=1, date="2026-10-07", mid="101")     # CoreScope, traceable
    add_award(c, player=2, net=1, date="2026-10-07", mid="102")     # CoreScope, heard directly
    add_award(c, player=1, net=1, date="2026-10-14", mid="9999", at=NOW)   # message gone -> estimate from the player's traced check-in
    add_award(c, player=2, net=2, date="2026-10-14", mid="5003")    # Beacon, IATA
    add_award(c, player=1, net=4, date="2026-10-21", mid="mt1")     # Meshtastic: never touched


def run_backfill(**kw):
    out = io.StringIO()
    placer, log = make_placer()
    stats = asyncio.run(backfill.run(placer=placer, out=out, log_every=0, **kw))
    return stats, out.getvalue(), log


def methods(c):
    return {(r["player_id"], r["net_date"]): r["method"] for r in c.execute("SELECT * FROM checkin_award_place")}


def test_backfill_dry_run_writes_nothing_but_reports(dbp):
    seed_backfill(dbp)
    stats, out, log = run_backfill(dry_run=True)
    assert dbp.execute("SELECT COUNT(*) FROM checkin_award_place").fetchone()[0] == 0
    assert "DRY RUN" in out and "first_hop" in out and "TOTAL" in out
    assert sum(st["awards"] for st in stats.values()) == 4


def test_backfill_places_traces_then_estimates_and_is_idempotent(dbp):
    seed_backfill(dbp)
    stats, out, log = run_backfill()
    assert methods(dbp) == {(1, "2026-10-07"): "first_hop", (2, "2026-10-07"): "observer",
                            (1, "2026-10-14"): "est_recent_checkin", (2, "2026-10-14"): "observer_iata"}
    est = dbp.execute("SELECT lat, lon FROM checkin_award_place WHERE player_id = 1 AND net_date = '2026-10-14'").fetchone()
    assert (est["lat"], est["lon"]) == (43.585, -116.795)          # the estimate used the traced one (phase 2 after phase 1)
    for col in ("awards", "first_hop", "observer", "observer_iata", "est_recent_checkin", "est_recent_square", "unplaced", "errors"):
        assert col in out
    first_requests = len(log)
    # second run: nothing left to do, no feed requests
    stats2, out2, log2 = run_backfill()
    assert sum(st["awards"] for st in stats2.values()) == 0 and log2 == []
    assert methods(dbp) == methods(dbp)
    # --replace redoes everything, still without duplicating rows
    stats3, _, _ = run_backfill(replace=True)
    assert sum(st["awards"] for st in stats3.values()) == 4
    assert dbp.execute("SELECT COUNT(*) FROM checkin_award_place").fetchone()[0] == 4
    assert first_requests > 0


def test_backfill_net_filter_and_resume_after_a_feed_error(dbp):
    seed_backfill(dbp)
    stats, _, _ = run_backfill(net_id=2)
    assert methods(dbp) == {(2, "2026-10-14"): "observer_iata"}
    # CoreScope down: its awards are errors, nothing written for them, the next run picks them up
    down = lambda req: httpx.Response(503) if req.url.host == "cs.test" else _handler([])(req)
    out = io.StringIO()
    placer, _ = make_placer(handler=down)
    stats = asyncio.run(backfill.run(placer=placer, out=out, log_every=0, net_id=1))
    assert sum(st["errors"] for st in stats.values()) == 3
    assert (1, "2026-10-07") not in methods(dbp)
    run_backfill(net_id=1)
    assert methods(dbp)[(1, "2026-10-07")] == "first_hop"


def test_region_counts_after_a_backfill(dbp):
    seed_backfill(dbp)
    dbp.execute("INSERT INTO region_cell(region_code, cell_id) VALUES ('BOI', ?)", (grid_cell_id(43.585, -116.795),))
    run_backfill()
    got = dict(backfill.region_counts())
    assert got.get("BOI", 0) >= 2          # the traced and the estimated award at the repeater, plus the IATA-only one
