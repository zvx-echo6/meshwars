"""Tests for app/regions.py: polygon parsing, point-in-polygon (incl. the
SMN/IDA overlap), membership, groups, and the importer with the network
mocked. Fixtures in tests/fixtures/meshmapper are trimmed copies of the
real MeshMapper pages (the `var zones` / `var allZones` lines, polygons
thinned to ~37 vertices); they keep the real structure: polygon is a JSON
STRING of [lat, lon] pairs.
"""
from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import time
from pathlib import Path

import httpx
import pytest

import app.db as db
from app import regions
from app.db import MIGRATIONS, SCHEMA
from app.grid import cell_id as grid_cell_id

FIX = Path(__file__).parent / "fixtures" / "meshmapper"


def _fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


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
    monkeypatch.setattr(regions, "_INDEX_CACHE", None)
    return path


def _conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def load_fixture_regions(path: str, codes=("boi", "pih", "ida", "smn")) -> None:
    c = _conn(path)
    zones = {z["code"]: z for z in regions.parse_zone_list(_fx("index_zones.html"))}
    for code in codes:
        z = zones[code.upper()]
        parsed = regions.parse_region_page(_fx(f"{code}.html"), code, z["lat"], z["lon"])
        c.execute("BEGIN IMMEDIATE")
        regions.store_region(c, code, z["name"], parsed, now=1000)
        c.execute("COMMIT")
    c.close()


# ---- parsing ------------------------------------------------------------


def test_normalize_polygon_single_ring_from_json_string():
    rings = regions.normalize_polygon("[[44.0,-116.0],[44.0,-115.0],[43.0,-115.0],[43.0,-116.0]]")
    assert rings == [[(44.0, -116.0), (44.0, -115.0), (43.0, -115.0), (43.0, -116.0)]]


def test_normalize_polygon_single_ring_already_decoded():
    ring = [[44, -116], [44, -115], [43, -115]]
    assert regions.normalize_polygon(ring) == [[(44.0, -116.0), (44.0, -115.0), (43.0, -115.0)]]


def test_normalize_polygon_multi_ring_by_nesting_depth():
    outer = [[44, -116], [44, -114], [42, -114], [42, -116]]
    inner = [[43.5, -115.5], [43.5, -114.5], [42.5, -114.5]]
    rings = regions.normalize_polygon(json.dumps([outer, inner]))
    assert len(rings) == 2
    assert rings[0][0] == (44.0, -116.0)
    assert rings[1][0] == (43.5, -115.5)


@pytest.mark.parametrize("bad", [None, "", "not json", "[]", "[[1,2],[3,4]]", [[None, None]] * 5, 7, {"a": 1}])
def test_normalize_polygon_rejects_unusable(bad):
    assert regions.normalize_polygon(bad) is None


def test_normalize_polygon_drops_bad_points_and_bad_rings():
    ring = [[44, -116], ["x", 1], [44, -115], [95, 0], [43, -115]]
    assert regions.normalize_polygon(ring) == [[(44.0, -116.0), (44.0, -115.0), (43.0, -115.0)]]


def test_parse_zone_list_reads_homepage_fixture():
    zones = regions.parse_zone_list(_fx("index_zones.html"))
    assert [z["code"] for z in zones] == ["BOI", "IDA", "PIH", "SMN", "TWF"]
    boi = zones[0]
    assert boi["name"] == "Boise, US"
    assert boi["lat"] == pytest.approx(43.596306)


def test_parse_zone_list_drops_unsafe_codes_and_bad_entries():
    html = ('<script>var zones = [{"code":"A.B","name":"x","lat":1,"lon":2},'
            '{"code":"OK","name":"Fine","lat":1,"lon":2},{"code":"NN","name":"","lat":1,"lon":2},'
            '{"code":"XX","name":"n","lat":"1","lon":2}];</script>')
    assert [z["code"] for z in regions.parse_zone_list(html)] == ["OK"]
    assert regions.parse_zone_list("<html>nothing here</html>") == []


def test_parse_region_page_polygon_is_lat_first():
    parsed = regions.parse_region_page(_fx("boi.html"), "BOI")
    assert parsed["shape"] == "polygon"
    first = parsed["rings"][0][0]
    assert 40 < first[0] < 50 and -120 < first[1] < -110  # (lat, lon), not (lon, lat)
    assert parsed["radius_km"] == pytest.approx(233.4)
    assert parsed["center_lat"] == pytest.approx(43.596306)


def test_parse_region_page_missing_polygon_falls_back_to_circle():
    html = ('<script>var allZones = [{"code":"ZZZ","name":"Z, US","lat":43.0,"lon":-116.0,'
            '"radius_km":50}];</script>')
    parsed = regions.parse_region_page(html, "zzz")
    assert parsed["shape"] == "circle"
    shape = regions.Shape(parsed["rings"])
    assert shape.contains(43.0, -116.0)
    assert shape.contains(43.3, -116.0)        # ~33 km north, inside 50 km
    assert not shape.contains(43.6, -116.0)    # ~67 km north, outside
    assert not shape.contains(43.0, -115.2)    # ~65 km east, outside


def test_parse_region_page_unusable_polygon_also_falls_back():
    html = ('<script>var allZones = [{"code":"ZZZ","lat":43.0,"lon":-116.0,"radius_km":10,'
            '"polygon":"[[1,2]]"}];</script>')
    assert regions.parse_region_page(html, "ZZZ")["shape"] == "circle"


def test_parse_region_page_no_polygon_no_radius_is_none():
    html = '<script>var allZones = [{"code":"ZZZ","lat":43.0,"lon":-116.0}];</script>'
    assert regions.parse_region_page(html, "ZZZ") is None


def test_parse_region_page_other_code_or_garbage_is_none():
    assert regions.parse_region_page(_fx("boi.html"), "PIH") is None
    assert regions.parse_region_page("<html></html>", "BOI") is None
    assert regions.parse_region_page("var allZones = [{broken", "BOI") is None


def test_parse_region_page_multi_ring_polygon():
    outer = [[44, -116], [44, -114], [42, -114], [42, -116]]
    inner = [[43.4, -115.4], [43.4, -114.6], [42.6, -114.6], [42.6, -115.4]]
    html = ('<script>var allZones = [{"code":"ZZZ","lat":43,"lon":-115,"radius_km":5,"polygon":'
            + json.dumps(json.dumps([outer, inner])) + '}];</script>')
    parsed = regions.parse_region_page(html, "ZZZ")
    assert len(parsed["rings"]) == 2
    shape = regions.Shape(parsed["rings"])
    assert shape.contains(43.8, -115.0)       # in the outer ring only
    assert not shape.contains(43.0, -115.0)   # inside the inner ring: a hole (even-odd)


# ---- point in polygon ----------------------------------------------------


def _naive_even_odd(rings, lat, lon):
    inside = False
    for ring in rings:
        m = len(ring)
        for i in range(m):
            la1, lo1 = ring[i]
            la2, lo2 = ring[(i + 1) % m]
            if (la1 > lat) != (la2 > lat):
                x = lo1 + (lat - la1) * (lo2 - lo1) / (la2 - la1)
                if x > lon:
                    inside = not inside
    return inside


def test_shape_matches_naive_even_odd_on_real_boundary():
    parsed = regions.parse_region_page(_fx("smn.html"), "SMN")
    shape = regions.Shape(parsed["rings"])
    rng = random.Random(7)
    inside_seen = outside_seen = 0
    for _ in range(3000):
        lat = rng.uniform(shape.south - 0.2, shape.north + 0.2)
        lon = rng.uniform(shape.west - 0.2, shape.east + 0.2)
        got = shape.contains(lat, lon)
        assert got == _naive_even_odd(parsed["rings"], lat, lon)
        inside_seen += got
        outside_seen += not got
    assert inside_seen > 100 and outside_seen > 100


def _overlap_point():
    smn = regions.Shape(regions.parse_region_page(_fx("smn.html"), "SMN")["rings"])
    ida = regions.Shape(regions.parse_region_page(_fx("ida.html"), "IDA")["rings"])
    lat0, lat1 = max(smn.south, ida.south), min(smn.north, ida.north)
    lon0, lon1 = max(smn.west, ida.west), min(smn.east, ida.east)
    for i in range(1, 40):
        for j in range(1, 40):
            lat = lat0 + (lat1 - lat0) * i / 40
            lon = lon0 + (lon1 - lon0) * j / 40
            if smn.contains(lat, lon) and ida.contains(lat, lon):
                return lat, lon
    raise AssertionError("SMN and IDA fixtures do not overlap")


def test_overlap_point_maps_to_both_smn_and_ida(db_path):
    load_fixture_regions(db_path)
    lat, lon = _overlap_point()
    c = _conn(db_path)
    index = regions._build_index(c)
    assert set(index.codes_for(lat, lon)) >= {"SMN", "IDA"}
    c.close()


def test_point_in_one_region_only_and_none():
    index = regions.RegionIndex({
        "A": regions.Shape([[(0, 0), (0, 2), (2, 2), (2, 0)]]),
        "B": regions.Shape([[(0, 2), (0, 4), (2, 4), (2, 2)]]),  # shares the edge lon=2
    })
    assert index.codes_for(1, 1) == ["A"]
    assert index.codes_for(1, 3) == ["B"]
    assert index.codes_for(5, 5) == []


def test_concave_polygon():
    # An L shape: the notch is outside.
    ring = [(0, 0), (0, 4), (2, 4), (2, 2), (4, 2), (4, 0)]
    shape = regions.Shape([ring])
    assert shape.contains(1, 1)
    assert shape.contains(1, 3)
    assert shape.contains(3, 1)
    assert not shape.contains(3, 3)


# ---- membership -----------------------------------------------------------


def _insert_tile(c, season, cid, team="RED"):
    from app.grid import cell_indices
    la, lo = cell_indices(cid)
    c.execute(
        "INSERT OR IGNORE INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts, "
        "paint_count, lat_idx, lon_idx) VALUES (?,?,?,?,?,1,?,?)",
        (season, cid, team, 1, 1, la, lo))


def test_map_new_cells_fills_membership_once_and_is_incremental(db_path):
    load_fixture_regions(db_path)
    boise = grid_cell_id(43.61, -116.20)
    seattle = grid_cell_id(47.6, -122.3)
    c = _conn(db_path)
    _insert_tile(c, 1, boise)
    _insert_tile(c, 2, boise)          # the other board's season: same cell, mapped once
    _insert_tile(c, 1, seattle)
    assert regions.map_new_cells() == 2
    members = {(r[0], r[1]) for r in c.execute("SELECT cell_id, region_code FROM region_cell")}
    assert members == {(boise, "BOI")}
    # a cell in no region is still marked as tested
    assert c.execute("SELECT COUNT(*) FROM region_cell_mapped").fetchone()[0] == 2
    assert regions.map_new_cells() == 0
    # a new square is picked up on the next pass
    pocatello = grid_cell_id(42.87, -112.45)
    _insert_tile(c, 1, pocatello)
    assert regions.map_new_cells() == 1
    assert (pocatello, "PIH") in {(r[0], r[1]) for r in c.execute("SELECT cell_id, region_code FROM region_cell")}
    c.close()


def test_map_new_cells_without_regions_is_a_noop(db_path):
    c = _conn(db_path)
    _insert_tile(c, 1, grid_cell_id(43.61, -116.20))
    assert regions.map_new_cells() == 0
    assert c.execute("SELECT COUNT(*) FROM region_cell_mapped").fetchone()[0] == 0
    c.close()


def _overlap_cell(c) -> str:
    """A grid cell whose CENTRE is inside both SMN and IDA (membership is
    decided on the centre)."""
    from app.grid import cell_center
    index = regions._build_index(c, ["SMN", "IDA"])
    lat, lon = _overlap_point()
    base = grid_cell_id(lat, lon)
    from app.grid import cell_indices
    la, lo = cell_indices(base)
    for dla in range(-3, 4):
        for dlo in range(-3, 4):
            cid = f"{la + dla}_{lo + dlo}"
            if set(index.codes_for(*cell_center(cid))) == {"SMN", "IDA"}:
                return cid
    raise AssertionError("no cell centre inside both fixtures")


def test_overlap_square_is_a_member_of_both_regions(db_path):
    load_fixture_regions(db_path)
    c = _conn(db_path)
    cid = _overlap_cell(c)
    _insert_tile(c, 1, cid)
    regions.map_new_cells()
    codes = {r[0] for r in c.execute("SELECT region_code FROM region_cell WHERE cell_id = ?", (cid,))}
    assert codes == {"SMN", "IDA"}
    c.close()


def test_remap_regions_recomputes_after_a_boundary_changes(db_path):
    load_fixture_regions(db_path, codes=("boi",))
    boise = grid_cell_id(43.61, -116.20)
    c = _conn(db_path)
    _insert_tile(c, 1, boise)
    regions.map_new_cells()
    assert c.execute("SELECT COUNT(*) FROM region_cell WHERE region_code='BOI'").fetchone()[0] == 1
    # BOI's boundary moves far away: the old membership must go
    far = {"rings": [[(10, 10), (10, 11), (11, 11), (11, 10)]], "radius_km": None,
           "center_lat": 10.5, "center_lon": 10.5, "shape": "polygon"}
    c.execute("BEGIN IMMEDIATE")
    regions.store_region(c, "BOI", "Boise, US", far, now=2000)
    c.execute("COMMIT")
    regions.remap_regions(["boi"])
    assert c.execute("SELECT COUNT(*) FROM region_cell WHERE region_code='BOI'").fetchone()[0] == 0
    c.close()


# ---- groups -------------------------------------------------------------


def _new_group(c, name, regs=(), groups=(), slug=None):
    c.execute("BEGIN IMMEDIATE")
    try:
        gid = regions.create_group(c, name, slug, list(regs), list(groups))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    return gid


def test_group_expansion_nests_to_any_depth(db_path):
    load_fixture_regions(db_path)
    c = _conn(db_path)
    leaf = _new_group(c, "Eastern Idaho", ["PIH", "IDA"])
    mid = _new_group(c, "South", ["BOI"], [leaf])
    top = _new_group(c, "Everything", ["SMN"], [mid])
    r = regions.resolve(c, "everything")
    assert r.kind == "group" and r.key == "everything"
    assert set(r.codes) == {"SMN", "BOI", "PIH", "IDA"}
    assert set(regions.resolve(c, "EASTERN-IDAHO").codes) == {"PIH", "IDA"}
    assert top > mid > leaf
    c.close()


def test_resolve_region_is_case_insensitive_and_unknown_is_none(db_path):
    load_fixture_regions(db_path)
    c = _conn(db_path)
    for v in ("boi", "BOI", "Boi"):
        r = regions.resolve(c, v)
        assert r.kind == "region" and r.key == "BOI" and r.codes == ("BOI",)
    for v in ("nope", "", "  ", "../etc", "a b", None):
        assert regions.resolve(c, v) is None
    c.close()


def test_cycle_is_rejected_on_write(db_path):
    load_fixture_regions(db_path)
    c = _conn(db_path)
    a = _new_group(c, "A", ["BOI"])
    b = _new_group(c, "B", ["PIH"], [a])
    d = _new_group(c, "D", [], [b])
    for gid, members in ((a, [b]), (a, [a]), (a, [d]), (b, [d])):
        c.execute("BEGIN IMMEDIATE")
        with pytest.raises(regions.RegionError) as exc:
            regions.update_group(c, gid, "x", None, [], members)
        c.execute("ROLLBACK")
        assert exc.value.status == 409
        assert "itself" in exc.value.message
    # a non-cyclic edit is fine
    c.execute("BEGIN IMMEDIATE")
    regions.update_group(c, a, "A", "a", ["BOI", "SMN"], [])
    c.execute("COMMIT")
    assert set(regions.resolve(c, "d").codes) == {"BOI", "SMN", "PIH"}
    c.close()


def test_slug_rules(db_path):
    load_fixture_regions(db_path)
    c = _conn(db_path)
    gid = _new_group(c, "Treasure Valley")
    assert regions.resolve(c, "treasure-valley") is not None
    for slug, status in (("treasure-valley", 409), ("boi", 409), ("Bad Slug!", 400)):
        c.execute("BEGIN IMMEDIATE")
        with pytest.raises(regions.RegionError) as exc:
            regions.create_group(c, "Other", slug, [], [])
        c.execute("ROLLBACK")
        assert exc.value.status == status
    # a group may keep its own slug on update
    c.execute("BEGIN IMMEDIATE")
    regions.update_group(c, gid, "Treasure Valley 2", "treasure-valley", ["BOI"], [])
    c.execute("COMMIT")
    c.close()


def test_member_validation_and_delete_cascade(db_path):
    load_fixture_regions(db_path)
    c = _conn(db_path)
    c.execute("BEGIN IMMEDIATE")
    with pytest.raises(regions.RegionError, match="unknown region"):
        regions.create_group(c, "X", None, ["NOPE"], [])
    c.execute("ROLLBACK")
    c.execute("BEGIN IMMEDIATE")
    with pytest.raises(regions.RegionError, match="unknown group"):
        regions.create_group(c, "X", None, [], [999])
    c.execute("ROLLBACK")
    child = _new_group(c, "Child", ["BOI"])
    parent = _new_group(c, "Parent", ["PIH"], [child])
    c.execute("BEGIN IMMEDIATE")
    assert regions.delete_group(c, child)
    c.execute("COMMIT")
    assert set(regions.resolve(c, "parent").codes) == {"PIH"}
    assert regions.resolve(c, "child") is None
    assert c.execute("SELECT COUNT(*) FROM region_group_member WHERE child_group_id IS NOT NULL").fetchone()[0] == 0
    assert parent
    c.close()


def test_empty_group_resolves_to_no_regions(db_path):
    load_fixture_regions(db_path)
    c = _conn(db_path)
    _new_group(c, "Empty")
    assert regions.resolve(c, "empty").codes == ()
    cond, params = regions.cell_filter_sql(())
    assert cond == "0" and params == []
    c.close()


# ---- geojson -------------------------------------------------------------


def test_geojson_flips_to_lon_lat_and_closes_rings(db_path):
    load_fixture_regions(db_path, codes=("boi",))
    c = _conn(db_path)
    fc = regions.geojson_for_regions(c, ["BOI"])
    assert fc["type"] == "FeatureCollection" and len(fc["features"]) == 1
    f = fc["features"][0]
    assert f["properties"]["code"] == "BOI"
    ring = f["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1]
    lon, lat = ring[0]
    assert -120 < lon < -110 and 40 < lat < 50
    c.close()


# ---- importer (network mocked) ------------------------------------------


def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"User-Agent": regions.USER_AGENT})


def _site(failing=(), seen=None):
    pages = {f"{c}.meshmapper.net": _fx(f"{c}.html") for c in ("boi", "ida", "pih", "smn", "twf")}

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((request.url.host, request.headers.get("user-agent")))
        host = request.url.host
        if host == "meshmapper.net":
            return httpx.Response(200, text=_fx("index_zones.html"))
        if host.split(".")[0].upper() in failing:
            return httpx.Response(500, text="boom")
        if host in pages:
            return httpx.Response(200, text=pages[host])
        return httpx.Response(404)

    return handler


def test_import_stores_regions_and_maps_membership(db_path, monkeypatch):
    monkeypatch.setattr(regions.settings, "regions_fetch_delay_seconds", 0.0)
    c = _conn(db_path)
    _insert_tile(c, 1, grid_cell_id(43.61, -116.20))
    seen: list = []
    state = asyncio.run(regions.run_import("all", client=_mock_client(_site(seen=seen))))
    assert state["total"] == 5 and state["done"] == 5 and state["failures"] == 0
    assert c.execute("SELECT COUNT(*) FROM region").fetchone()[0] == 5
    assert {h for h, _ in seen} >= {"meshmapper.net", "boi.meshmapper.net", "smn.meshmapper.net"}
    assert {ua for _, ua in seen} == {regions.USER_AGENT}
    # square membership was recomputed for the affected regions
    regions.map_new_cells()
    assert c.execute("SELECT region_code FROM region_cell").fetchall()[0][0] == "BOI"
    st = regions.sync_status()
    assert st["running"] is False and st["done"] == 5 and st["finished_at"]
    c.close()


def test_import_failure_keeps_previous_boundary_and_carries_on(db_path, monkeypatch):
    monkeypatch.setattr(regions.settings, "regions_fetch_delay_seconds", 0.0)
    load_fixture_regions(db_path, codes=("boi", "pih"))
    c = _conn(db_path)
    before = c.execute("SELECT polygon_json, fetched_at FROM region WHERE code='BOI'").fetchone()
    state = asyncio.run(regions.run_import("all", client=_mock_client(_site(failing={"BOI"}))))
    assert state["failures"] == 1 and "BOI" in state["last_error"]
    assert state["done"] == 5
    after = c.execute("SELECT polygon_json, fetched_at FROM region WHERE code='BOI'").fetchone()
    assert tuple(after) == tuple(before)                       # kept
    assert c.execute("SELECT fetched_at FROM region WHERE code='SMN'").fetchone()[0] > 1000  # others went on
    assert c.execute("SELECT COUNT(*) FROM region").fetchone()[0] == 5
    c.close()


def test_import_new_mode_fetches_only_unstored_codes(db_path, monkeypatch):
    monkeypatch.setattr(regions.settings, "regions_fetch_delay_seconds", 0.0)
    load_fixture_regions(db_path, codes=("boi", "pih", "ida"))
    seen: list = []
    state = asyncio.run(regions.run_import("new", client=_mock_client(_site(seen=seen))))
    pages = sorted(h for h, _ in seen if h != "meshmapper.net")
    assert pages == ["smn.meshmapper.net", "twf.meshmapper.net"]
    assert state["total"] == 2


def test_import_list_failure_is_reported_and_stores_nothing(db_path, monkeypatch):
    monkeypatch.setattr(regions.settings, "regions_fetch_delay_seconds", 0.0)
    monkeypatch.setattr(regions, "_next_try_at", 0.0)
    client = _mock_client(lambda req: httpx.Response(503))
    state = asyncio.run(regions.run_import("all", client=client))
    assert state["failures"] == 1 and "region list" in state["last_error"]
    c = _conn(db_path)
    assert c.execute("SELECT COUNT(*) FROM region").fetchone()[0] == 0
    assert regions.sync_status()["running"] is False
    c.close()


def test_import_pauses_between_requests(db_path, monkeypatch):
    monkeypatch.setattr(regions.settings, "regions_fetch_delay_seconds", 2.0)
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(regions.asyncio, "sleep", fake_sleep)
    asyncio.run(regions.run_import("all", client=_mock_client(_site())))
    assert sleeps == [2.0] * 4      # between the 5 region pages, none before the first


def test_make_client_identifies_meshwars():
    async def go():
        client = regions.make_client()
        try:
            return client.headers["user-agent"], client.timeout
        finally:
            await client.aclose()

    ua, timeout = asyncio.run(go())
    assert "MeshWars" in ua and timeout.read is not None


def test_sync_status_running_goes_stale(db_path):
    regions._write_state({"state": "running", "mode": "all", "started_at": int(time.time())})
    assert regions.sync_status()["running"] is True
    c = _conn(db_path)
    row = json.loads(c.execute("SELECT v FROM cursor WHERE k = ?", (regions._STATE_KEY,)).fetchone()[0])
    row["updated_at"] = int(time.time()) - 3600
    c.execute("UPDATE cursor SET v = ? WHERE k = ?", (json.dumps(row), regions._STATE_KEY))
    assert regions.sync_status()["running"] is False
    c.close()


def test_host_region_key(monkeypatch):
    monkeypatch.setattr(regions.settings, "regions_host_suffix", "")
    assert regions.host_region_key("boi.meshwars.com") is None
    monkeypatch.setattr(regions.settings, "regions_host_suffix", "meshwars.com")
    assert regions.host_region_key("boi.meshwars.com") == "boi"
    assert regions.host_region_key("BOI.MeshWars.com:8443") == "boi"
    assert regions.host_region_key("meshwars.com") is None
    assert regions.host_region_key("a.b.meshwars.com") is None
    assert regions.host_region_key("boi.example.com") is None
    assert regions.host_region_key(None) is None


# ---- write-lock behaviour -------------------------------------------------


def test_retry_busy_retries_locked_then_succeeds(monkeypatch):
    monkeypatch.setattr(regions.time, "sleep", lambda s: None)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert regions._retry_busy(flaky) == "ok" and len(calls) == 3


def test_retry_busy_gives_up_and_ignores_other_errors(monkeypatch):
    monkeypatch.setattr(regions.time, "sleep", lambda s: None)
    n = []

    def always_locked():
        n.append(1)
        raise sqlite3.OperationalError("database table is locked")

    with pytest.raises(sqlite3.OperationalError):
        regions._retry_busy(always_locked)
    assert len(n) == regions._BUSY_ATTEMPTS

    m = []

    def other():
        m.append(1)
        raise sqlite3.OperationalError("no such table: x")

    with pytest.raises(sqlite3.OperationalError):
        regions._retry_busy(other)
    assert len(m) == 1


def _write_lock_is_free(path: str) -> bool:
    other = sqlite3.connect(path, timeout=0.2, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        other.close()


def test_membership_work_runs_with_no_write_lock_held(db_path, monkeypatch):
    """The polygon testing is the slow part; it must happen with no write
    transaction open so ingest writers are never kept waiting behind it."""
    load_fixture_regions(db_path)
    c = _conn(db_path)
    _insert_tile(c, 1, grid_cell_id(43.61, -116.20))
    seen = []
    real = regions._membership

    def spy(index, cells):
        if cells:
            seen.append(_write_lock_is_free(db_path))
        return real(index, cells)

    monkeypatch.setattr(regions, "_membership", spy)
    assert regions.map_new_cells() == 1
    regions.remap_regions(["BOI"])
    assert seen and all(seen)
    c.close()


def test_map_batch_redone_when_a_region_changes_mid_batch(db_path, monkeypatch):
    load_fixture_regions(db_path, codes=("boi",))
    c = _conn(db_path)
    boise = grid_cell_id(43.61, -116.20)
    _insert_tile(c, 1, boise)
    real = regions._membership
    fired = []

    def change_then_compute(index, cells):
        if not fired:
            fired.append(1)
            far = {"rings": [[(10, 10), (10, 11), (11, 11), (11, 10)]], "radius_km": None,
                   "center_lat": 10.5, "center_lon": 10.5, "shape": "polygon"}
            c.execute("BEGIN IMMEDIATE")
            regions.store_region(c, "BOI", "Boise, US", far, now=5000)
            c.execute("COMMIT")
        return real(index, cells)

    monkeypatch.setattr(regions, "_membership", change_then_compute)
    regions.map_new_cells()
    # the stale first pass wrote nothing; the redo used the moved boundary
    assert c.execute("SELECT COUNT(*) FROM region_cell WHERE region_code='BOI'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM region_cell_mapped").fetchone()[0] == 1
    c.close()


# ---- remap writes in small transactions ------------------------------------


def _box(lat0, lat1, lon0, lon1):
    return {"rings": [[(lat0, lon0), (lat0, lon1), (lat1, lon1), (lat1, lon0)]], "radius_km": None,
            "center_lat": (lat0 + lat1) / 2, "center_lon": (lon0 + lon1) / 2, "shape": "polygon"}


def _seed_box_cells(c, lat0, lat1, lon0, lon1):
    from app.grid import cell_indices
    la0, lo0 = cell_indices(grid_cell_id(lat0, lon0))
    la1, lo1 = cell_indices(grid_cell_id(lat1, lon1))
    n = 0
    for la in range(la0 - 2, la1 + 3):
        for lo in range(lo0 - 2, lo1 + 3):
            _insert_tile(c, 1, f"{la}_{lo}")
            n += 1
    return n


def test_remap_writes_in_bounded_transactions_and_yields_between_them(db_path, monkeypatch, caplog):
    import logging
    monkeypatch.setattr(regions, "_TXN_ROWS", 5)
    c = _conn(db_path)
    c.execute("BEGIN IMMEDIATE")
    regions.store_region(c, "BOX", "Box", _box(43.00, 43.02, -116.00, -115.97), now=1000)
    regions.store_region(c, "TWO", "Two", _box(43.00, 43.02, -116.00, -115.97), now=1000)
    c.execute("COMMIT")
    _seed_box_cells(c, 43.00, 43.02, -116.00, -115.97)
    regions.map_new_cells()
    before = {r[0] for r in c.execute("SELECT cell_id FROM region_cell WHERE region_code='BOX'")}
    assert len(before) > 20

    # the BOX region moves east, so many rows change; TWO stays put
    c.execute("BEGIN IMMEDIATE")
    regions.store_region(c, "BOX", "Box", _box(43.00, 43.02, -115.985, -115.955), now=2000)
    c.execute("COMMIT")

    sizes, free_between = [], []
    real_apply = regions._apply_chunk

    def counting_apply(conn, ops):
        sizes.append(len(ops))
        return real_apply(conn, ops)

    monkeypatch.setattr(regions, "_apply_chunk", counting_apply)
    monkeypatch.setattr(regions.time, "sleep", lambda s: free_between.append(_write_lock_is_free(db_path)))
    with caplog.at_level(logging.INFO, logger="regions"):
        written = regions.remap_regions(["BOX", "TWO"])

    assert sizes and max(sizes) <= 5 and len(sizes) >= 3          # bounded, and split into several txns
    assert sum(sizes) == written
    assert free_between and all(free_between)                      # an ingest writer can get in between txns
    # the result is exactly the new geometry's membership
    index = regions._build_index(c, ["BOX"])
    expect = {cid for (cid,) in c.execute("SELECT cell_id FROM region_cell_mapped")
              if "BOX" in index.codes_for_cell(cid)}
    assert {r[0] for r in c.execute("SELECT cell_id FROM region_cell WHERE region_code='BOX'")} == expect
    assert expect != before
    # TWO's boundary did not change: nothing of it was rewritten
    assert all(r.getMessage().count("longest lock hold") for r in caplog.records if "remapped" in r.getMessage())
    assert any("remapped 2 region(s)" in r.getMessage() and "longest lock hold" in r.getMessage()
               for r in caplog.records)
    c.close()


def test_remap_of_an_unchanged_boundary_writes_nothing(db_path, monkeypatch):
    c = _conn(db_path)
    c.execute("BEGIN IMMEDIATE")
    regions.store_region(c, "BOX", "Box", _box(43.00, 43.02, -116.00, -115.97), now=1000)
    c.execute("COMMIT")
    _seed_box_cells(c, 43.00, 43.02, -116.00, -115.97)
    regions.map_new_cells()
    calls = []
    monkeypatch.setattr(regions, "_apply_chunk", lambda conn, ops: calls.append(len(ops)))
    assert regions.remap_regions(["BOX"]) == 0 and calls == []
    c.close()


def test_remap_skips_a_region_refetched_since_the_work_was_computed(db_path, monkeypatch):
    c = _conn(db_path)
    c.execute("BEGIN IMMEDIATE")
    regions.store_region(c, "BOX", "Box", _box(43.00, 43.02, -116.00, -115.97), now=1000)
    c.execute("COMMIT")
    _seed_box_cells(c, 43.00, 43.02, -116.00, -115.97)
    regions.map_new_cells()
    c.execute("BEGIN IMMEDIATE")
    regions.store_region(c, "BOX", "Box", _box(43.00, 43.02, -115.985, -115.955), now=2000)  # moved: remap has work
    c.execute("COMMIT")
    real = regions._membership

    def meanwhile(index, cells):
        out = real(index, cells)
        c.execute("BEGIN IMMEDIATE")
        regions.store_region(c, "BOX", "Box", _box(10, 11, 10, 11), now=9000)   # re-fetched mid-remap
        c.execute("COMMIT")
        return out

    monkeypatch.setattr(regions, "_membership", meanwhile)
    n_before = c.execute("SELECT COUNT(*) FROM region_cell WHERE region_code='BOX'").fetchone()[0]
    assert regions.remap_regions(["BOX"]) == 0     # the newer fetch owns it; nothing written from stale work
    assert c.execute("SELECT COUNT(*) FROM region_cell WHERE region_code='BOX'").fetchone()[0] == n_before
    c.close()


def test_map_new_cells_batches_are_bounded_and_yield(db_path, monkeypatch):
    c = _conn(db_path)
    c.execute("BEGIN IMMEDIATE")
    regions.store_region(c, "BOX", "Box", _box(43.00, 43.02, -116.00, -115.97), now=1000)
    c.execute("COMMIT")
    n = _seed_box_cells(c, 43.00, 43.02, -116.00, -115.97)
    sizes, free = [], []
    real_apply = regions._apply_membership

    def counting(conn, rows, *, mark_mapped):
        sizes.append(len(rows))
        return real_apply(conn, rows, mark_mapped=mark_mapped)

    monkeypatch.setattr(regions, "_apply_membership", counting)
    monkeypatch.setattr(regions.time, "sleep", lambda s: free.append(_write_lock_is_free(db_path)))
    assert regions.map_new_cells(batch=10) == n
    assert max(sizes) <= 10 and len(sizes) == -(-n // 10)
    assert free and all(free)
    c.close()


def test_vendored_polygon_clipping_is_pinned_licensed_and_loaded_before_the_map():
    root = Path(__file__).parent.parent / "frontend"
    lib = (root / "vendor" / "polygon-clipping.0.15.7.umd.min.js").read_text(encoding="utf-8")
    head = lib[:3000]
    assert lib.startswith("/*!") and "0.15.7" in head and "The MIT License" in head
    assert "polygonClipping" in lib
    html = (root / "map2.html").read_text(encoding="utf-8")
    assert html.index("vendor/polygon-clipping.0.15.7.umd.min.js") < html.index("/static/map2.js")
    assert "cdn" not in html[html.index("polygon-clipping"):html.index("polygon-clipping") + 200].lower().replace("cdn.jsdelivr", "")
