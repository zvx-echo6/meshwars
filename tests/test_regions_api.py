"""Region filtering on the public routes of BOTH boards (board, scores,
top captures), the regions read endpoints, the preselecting page routes
and the admin routes. A region is a view filter only: every unfiltered
response must be exactly what it was before regions existed.

Real file-backed sqlite and a bare FastAPI app around the real routers
(same shape as tests/test_get_nodes_cache.py). Region membership is made
by the real importer fixtures and map_new_cells(), then every expectation
is derived from region_cell itself rather than from hand-placed
coordinates, so a thinned fixture polygon cannot make a test lie.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.api as api_module
import app.db as db
import app.mc_api as mc_api_module
from app import regions
from app.admin_api import router as admin_api_router
from app.auth import http_exception_as_error_body
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.grid import cell_center, cell_id as grid_cell_id, cell_indices
from app.regions_api import router as regions_router
from app.sessions import SESSION_COOKIE_NAME, create_session

NOW = int(time.time())
FIX = Path(__file__).parent / "fixtures" / "meshmapper"

MT, MC = 1, 2  # season ids: the Meshtastic board and the MeshCore board


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


@pytest.fixture(autouse=True)
def _fresh_board_cache(monkeypatch):
    monkeypatch.setattr(settings, "board_cache_seconds", 10)
    mc_api_module._BOARD_CACHE.clear()
    yield
    mc_api_module._BOARD_CACHE.clear()


def _overlap_cell(index) -> str:
    smn = index.shapes["SMN"]
    ida = index.shapes["IDA"]
    lat0, lat1 = max(smn.south, ida.south), min(smn.north, ida.north)
    lon0, lon1 = max(smn.west, ida.west), min(smn.east, ida.east)
    for i in range(1, 60):
        for j in range(1, 60):
            lat = lat0 + (lat1 - lat0) * i / 60
            lon = lon0 + (lon1 - lon0) * j / 60
            cid = grid_cell_id(lat, lon)
            if set(index.codes_for(*cell_center(cid))) >= {"SMN", "IDA"}:
                return cid
    raise AssertionError("no overlapping cell")


@pytest.fixture
def seeded(db_path):
    """Regions BOI/PIH/IDA/SMN; two boards each holding the same four
    squares (Boise, Pocatello, an SMN+IDA overlap, Seattle = no region);
    capture logs, a check-in award and place credits on both boards."""
    c = sqlite3.connect(db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    zones = {z["code"]: z for z in regions.parse_zone_list((FIX / "index_zones.html").read_text())}
    for code in ("boi", "pih", "ida", "smn"):
        z = zones[code.upper()]
        parsed = regions.parse_region_page((FIX / f"{code}.html").read_text(), code, z["lat"], z["lon"])
        regions.store_region(c, code, z["name"], parsed, now=1000)
    index = regions._build_index(c)
    cells = {
        "boise": grid_cell_id(43.61, -116.20),
        "poca": grid_cell_id(42.87, -112.45),
        "overlap": _overlap_cell(index),
        "seattle": grid_cell_id(47.60, -122.30),
    }
    c.execute("INSERT INTO mc_season(id, protocol, started_at, ends_at, status) VALUES (?, 'mt', ?, ?, 'active')",
              (MT, NOW - 1000, NOW + 10**6))
    c.execute("INSERT INTO mc_season(id, protocol, started_at, ends_at, status) VALUES (?, 'mc', ?, ?, 'active')",
              (MC, NOW - 1000, NOW + 10**6))
    c.execute("INSERT INTO player(player_id, display_name, team, created_at) VALUES (1,'ann','RED',?)", (NOW,))
    c.execute("INSERT INTO player(player_id, display_name, team, created_at) VALUES (2,'bob','BLUE',?)", (NOW,))
    owners = {"boise": "RED", "poca": "BLUE", "overlap": "RED", "seattle": "BLUE"}
    for season in (MT, MC):
        for name, cid in cells.items():
            la, lo = cell_indices(cid)
            c.execute(
                "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts, paint_count, "
                "lat_idx, lon_idx) VALUES (?,?,?,1,?,1,?,?)", (season, cid, owners[name], NOW, la, lo))
            c.execute("INSERT INTO mc_tile_score(season_id, cell_id, team, score, last_update) VALUES (?,?,?,5,?)",
                      (season, cid, owners[name], NOW))
        # captures: ann x3 in Boise, bob x2 in Pocatello, bob x5 in Seattle, ann x1 in the overlap
        ts = NOW - 500
        for name, pid, team, n in (("boise", 1, "RED", 3), ("poca", 2, "BLUE", 2),
                                   ("seattle", 2, "BLUE", 5), ("overlap", 1, "RED", 1)):
            for _ in range(n):
                ts += 1
                c.execute("INSERT INTO mc_tile_capture_log(season_id, cell_id, ts, by_player_id, by_team) "
                          "VALUES (?,?,?,?,?)", (season, cells[name], ts, pid, team))
        c.execute("INSERT INTO mc_checkin_award(season_id, player_id, net_date, points, protocol, message_id, "
                  "awarded_at) VALUES (?,?,?,?,?,?,?)",
                  (season, 1, "2026-10-01", 7.0, "mt" if season == MT else "mc", "m1", NOW))
    # places: one in Boise (ann), one in Seattle (bob); credited on both boards
    for pid, (lat, lon) in ((1, (43.61, -116.20)), (2, (47.60, -122.30))):
        c.execute("INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, rotates, active, "
                  "created_at) VALUES (?,?,?,?,?,?,?,?,0,1,?)",
                  (pid, "summit", f"r{pid}", f"p{pid}", lat, lon, 100, "TEST", NOW))
        c.execute("INSERT INTO place_cell(place_id, cell_id) VALUES (?,?)", (pid, grid_cell_id(lat, lon)))
    for proto in ("mt", "mc"):
        c.execute("INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at, protocol) "
                  "VALUES (1, 1, ?, 100, ?, ?)", (f"2026-10-0{1 if proto == 'mt' else 2}", NOW, proto))
        c.execute("INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at, protocol) "
                  "VALUES (2, 2, ?, 100, ?, ?)", (f"2026-10-0{1 if proto == 'mt' else 2}", NOW, proto))
    # node_seen for the Meshtastic payload: one in Boise, one in Seattle
    c.execute("INSERT INTO node_seen(season_id, node_id, name, lat, lon, elev, last_seen) "
              "VALUES (?,?,?,?,?,0,?)", (MT, 1, "boise-node", 43.61, -116.20, NOW))
    c.execute("INSERT INTO node_seen(season_id, node_id, name, lat, lon, elev, last_seen) "
              "VALUES (?,?,?,?,?,0,?)", (MT, 2, "seattle-node", 47.60, -122.30, NOW))
    regions.map_new_cells()
    members = {}
    for r in c.execute("SELECT region_code, cell_id FROM region_cell"):
        members.setdefault(r["region_code"], set()).add(r["cell_id"])
    c.close()
    return {"cells": cells, "members": members, "path": db_path}


@pytest.fixture
def client(db_path):
    app = FastAPI()
    app.include_router(api_module.router)
    app.include_router(mc_api_module.router)
    app.include_router(regions_router)
    return TestClient(app)


def _cell_ids_mc(client, region=None):
    r = client.get("/api/mc/board", params={"region": region} if region else None)
    assert r.status_code == 200
    return {c["cell_id"] for c in r.json()}, r


def _cell_ids_mt(client, region=None):
    r = client.get("/get-nodes", params={"region": region} if region else None)
    assert r.status_code == 200
    return {c["cell_id"] for c in r.json()["coverage"]}, r


# ---- board ----------------------------------------------------------------


def test_memberships_are_what_the_tests_assume(seeded):
    cells, members = seeded["cells"], seeded["members"]
    assert cells["boise"] in members["BOI"]
    assert cells["seattle"] not in set().union(*members.values())
    assert cells["overlap"] in members["SMN"] and cells["overlap"] in members["IDA"]
    assert cells["poca"] in members["PIH"]


@pytest.mark.parametrize("fetch", [_cell_ids_mc, _cell_ids_mt], ids=["mc", "mt"])
def test_board_unfiltered_has_every_square_and_region_has_only_its_own(seeded, client, fetch):
    cells, members = seeded["cells"], seeded["members"]
    everything, _ = fetch(client)
    assert everything == set(cells.values())
    for code in ("BOI", "PIH", "SMN", "IDA"):
        got, _ = fetch(client, code)
        assert got == members[code] & set(cells.values())
    boi, _ = fetch(client, "boi")      # case-insensitive
    assert boi == members["BOI"] & set(cells.values())
    # the overlap square is in both regions' boards
    assert cells["overlap"] in fetch(client, "SMN")[0] and cells["overlap"] in fetch(client, "IDA")[0]
    assert cells["seattle"] not in fetch(client, "BOI")[0]


@pytest.mark.parametrize("path", ["/api/mc/board", "/get-nodes", "/scores", "/api/mc/scores", "/top", "/api/mc/top"])
def test_unknown_region_is_a_json_404(seeded, client, path):
    for bad in ("nope", "../x", "a b"):
        r = client.get(path, params={"region": bad})
        assert r.status_code == 404
        assert r.json() == {"error": "unknown region"}


def test_empty_region_param_means_unfiltered(seeded, client):
    plain = client.get("/api/mc/board").content
    assert client.get("/api/mc/board", params={"region": ""}).content == plain


def test_region_board_is_smaller_than_the_full_board(seeded, client):
    _, full = _cell_ids_mc(client)
    _, one = _cell_ids_mc(client, "BOI")
    assert len(one.content) < len(full.content)
    _, full_mt = _cell_ids_mt(client)
    _, one_mt = _cell_ids_mt(client, "BOI")
    assert len(one_mt.content) < len(full_mt.content)


def test_region_board_has_its_own_cache_entry(seeded, client):
    a = client.get("/api/mc/board", params={"region": "BOI"})
    b = client.get("/api/mc/board", params={"region": "PIH"})
    full = client.get("/api/mc/board")
    assert len({a.headers["etag"], b.headers["etag"], full.headers["etag"]}) == 3
    again = client.get("/api/mc/board", params={"region": "BOI"}, headers={"If-None-Match": a.headers["etag"]})
    assert again.status_code == 304


def test_unfiltered_board_shape_unchanged(seeded, client):
    rows = client.get("/api/mc/board").json()
    assert set(rows[0]) == {"cell_id", "owner_team", "south", "west", "north", "east"}
    cov = client.get("/get-nodes").json()
    assert set(cov) == {"coverage", "repeaters"}
    assert set(cov["coverage"][0]) == {"cell_id", "owner_team", "last_report_ts", "paint_count", "captured_at",
                                       "scores", "south", "west", "north", "east"}
    assert len(cov["repeaters"]) == 2


def test_get_nodes_region_filters_repeaters_to_the_region(seeded, client):
    boi = client.get("/get-nodes", params={"region": "BOI"}).json()
    assert {r["name"] for r in boi["repeaters"]} == {"boise-node"}
    assert {c["cell_id"] for c in boi["coverage"]} == seeded["members"]["BOI"] & set(seeded["cells"].values())


def test_group_board_is_the_union_without_double_counting(seeded, client):
    c = sqlite3.connect(seeded["path"], isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("BEGIN IMMEDIATE")
    regions.create_group(c, "Idaho Mix", None, ["SMN", "IDA"], [])
    inner = c.execute("SELECT id FROM region_group WHERE slug='idaho-mix'").fetchone()["id"]
    regions.create_group(c, "Outer", None, ["BOI"], [inner])
    c.execute("COMMIT")
    c.close()
    expect = set()
    for code in ("SMN", "IDA", "BOI"):
        expect |= seeded["members"][code]
    expect &= set(seeded["cells"].values())
    rows = client.get("/api/mc/board", params={"region": "outer"}).json()
    ids = [r["cell_id"] for r in rows]
    assert set(ids) == expect and len(ids) == len(set(ids))   # the overlap square appears once


# ---- scores ---------------------------------------------------------------

TEAM_KEYS = {"team", "tiles", "checkin_points", "explorer_points", "total"}


@pytest.mark.parametrize("path,season", [("/scores", MT), ("/api/mc/scores", MC)])
def test_scores_unfiltered_unchanged(seeded, client, path, season):
    body = client.get(path).json()
    assert set(body) == {"season_id", "started_at", "ends_at", "teams"}
    teams = {t["team"]: t for t in body["teams"]}
    assert set(teams["RED"]) == TEAM_KEYS
    assert teams["RED"]["tiles"] == 2 and teams["BLUE"]["tiles"] == 2
    assert teams["RED"]["checkin_points"] == 7.0
    assert teams["RED"]["explorer_points"] == 100.0 and teams["BLUE"]["explorer_points"] == 100.0
    assert teams["RED"]["total"] == 2 + 7.0 + 100.0


@pytest.mark.parametrize("path", ["/scores", "/api/mc/scores"])
def test_scores_for_a_region_count_only_inside_and_leave_out_checkins(seeded, client, path):
    body = client.get(path, params={"region": "boi"}).json()
    assert body["region"] == "BOI"
    teams = {t["team"]: t for t in body["teams"]}
    inside = seeded["members"]["BOI"] & set(seeded["cells"].values())
    red_tiles = 1 if seeded["cells"]["boise"] in inside else 0
    assert teams["RED"]["tiles"] == red_tiles
    assert teams["BLUE"]["tiles"] == 0
    # the seeded check-in award has no placement, so it counts only in All regions
    assert teams["RED"]["checkin_points"] == 0.0 and teams["BLUE"]["checkin_points"] == 0.0
    assert teams["RED"]["explorer_points"] == 100.0       # the Boise place only
    assert teams["BLUE"]["explorer_points"] == 0.0        # bob's place is in Seattle
    assert teams["RED"]["total"] == red_tiles + 100.0
    assert set(teams["RED"]) == TEAM_KEYS
    assert len(body["teams"]) == 7                         # still zero-filled for every team


def test_scores_region_with_no_activity_is_all_zero(seeded, client):
    body = client.get("/api/mc/scores", params={"region": "pih"}).json()
    teams = {t["team"]: t for t in body["teams"]}
    assert teams["RED"]["explorer_points"] == 0.0 and teams["RED"]["checkin_points"] == 0.0


def test_exploration_follows_the_place_cell_not_the_player(seeded, client):
    # Seattle belongs to no region: even the biggest group never sees bob's place.
    c = sqlite3.connect(seeded["path"], isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("BEGIN IMMEDIATE")
    regions.create_group(c, "All Four", None, ["BOI", "PIH", "IDA", "SMN"], [])
    c.execute("COMMIT")
    c.close()
    teams = {t["team"]: t for t in client.get("/scores", params={"region": "all-four"}).json()["teams"]}
    assert teams["BLUE"]["explorer_points"] == 0.0
    assert teams["RED"]["explorer_points"] == 100.0


# ---- top captures -----------------------------------------------------------


@pytest.mark.parametrize("path", ["/top", "/api/mc/top"])
def test_top_unfiltered_counts_everything(seeded, client, path):
    rows = client.get(path).json()
    assert [(r["display_name"], r["captures"]) for r in rows] == [("bob", 7), ("ann", 4)]
    assert set(rows[0]) == {"display_name", "team", "captures"}


@pytest.mark.parametrize("path", ["/top", "/api/mc/top"])
def test_top_for_a_region_counts_only_its_squares(seeded, client, path):
    cells, members = seeded["cells"], seeded["members"]
    rows = client.get(path, params={"region": "BOI"}).json()
    got = {r["display_name"]: r["captures"] for r in rows}
    boi = members["BOI"]
    expect_ann = (3 if cells["boise"] in boi else 0) + (1 if cells["overlap"] in boi else 0)
    expect_bob = (2 if cells["poca"] in boi else 0) + (5 if cells["seattle"] in boi else 0)
    assert got.get("ann", 0) == expect_ann and got.get("bob", 0) == expect_bob
    assert "bob" not in got or got["bob"] != 7


def test_top_checkins_endpoints_stay_unfiltered(seeded, client):
    # A region parameter on them is simply ignored.
    assert client.get("/top-checkins", params={"region": "BOI"}).json() == client.get("/top-checkins").json()


# ---- regions endpoints ----------------------------------------------------


def test_regions_list_and_boundary(seeded, client):
    body = client.get("/api/regions").json()
    assert [r["code"] for r in body["regions"]] == ["BOI", "IDA", "PIH", "SMN"]
    r0 = body["regions"][0]
    assert set(r0) == {"code", "name", "lat", "lon", "bbox"} and len(r0["bbox"]) == 4
    assert body["groups"] == []
    # revalidated, never kept blind: a region fetched from the admin panel must show at once
    first = client.get("/api/regions")
    assert first.headers["cache-control"] == "no-cache" and first.headers["etag"]
    assert client.get("/api/regions", headers={"If-None-Match": first.headers["etag"]}).status_code == 304
    b = client.get("/api/regions/boi/boundary")
    assert b.status_code == 200
    fc = b.json()
    assert fc["type"] == "FeatureCollection" and fc["features"][0]["properties"]["code"] == "BOI"
    lon, lat = fc["features"][0]["geometry"]["coordinates"][0][0]
    assert lon < 0 < lat                                   # GeoJSON order
    assert client.get("/api/regions/nope/boundary").status_code == 404


def test_group_boundary_is_a_feature_per_member_region(seeded, client):
    c = sqlite3.connect(seeded["path"], isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("BEGIN IMMEDIATE")
    regions.create_group(c, "Pair", None, ["BOI", "PIH"], [])
    c.execute("COMMIT")
    c.close()
    fc = client.get("/api/regions/pair/boundary").json()
    assert {f["properties"]["code"] for f in fc["features"]} == {"BOI", "PIH"}
    tree = client.get("/api/regions").json()["groups"]
    assert tree[0]["slug"] == "pair" and tree[0]["regions"] == ["BOI", "PIH"] and tree[0]["groups"] == []


# ---- page routes ----------------------------------------------------------


@pytest.fixture
def page_client(seeded):
    app = FastAPI()
    api_module.mount(app)
    return TestClient(app)


def _meta(resp):
    marker = '<meta name="mw-region" content="'
    if marker not in resp.text:
        return None
    return resp.text.split(marker, 1)[1].split('"', 1)[0]


def test_map_region_route_preselects_case_insensitively(page_client):
    assert _meta(page_client.get("/map/boi")) == "BOI"
    assert _meta(page_client.get("/map/BOI")) == "BOI"
    assert page_client.get("/map/boi").status_code == 200


def test_unknown_region_serves_the_plain_map(page_client):
    r = page_client.get("/map/nowhere")
    assert r.status_code == 200 and _meta(r) is None
    assert 'id="mw-' in r.text or "<html" in r.text.lower()


def test_query_param_on_root(page_client):
    assert _meta(page_client.get("/", params={"region": "pih"})) == "PIH"
    assert _meta(page_client.get("/", params={"region": "zzz"})) is None
    assert _meta(page_client.get("/")) is None


def test_group_slug_route(seeded, page_client):
    c = sqlite3.connect(seeded["path"], isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("BEGIN IMMEDIATE")
    regions.create_group(c, "Treasure Valley", None, ["BOI"], [])
    c.execute("COMMIT")
    c.close()
    assert _meta(page_client.get("/map/Treasure-Valley")) == "treasure-valley"


def test_host_based_preselection_is_off_by_default_and_on_with_a_suffix(page_client, monkeypatch):
    assert _meta(page_client.get("/", headers={"host": "boi.meshwars.com"})) is None
    monkeypatch.setattr(settings, "regions_host_suffix", "meshwars.com")
    assert _meta(page_client.get("/", headers={"host": "boi.meshwars.com"})) == "BOI"
    assert _meta(page_client.get("/", headers={"host": "www.meshwars.com"})) is None
    # the path wins over the host
    assert _meta(page_client.get("/map/pih", headers={"host": "boi.meshwars.com"})) == "PIH"


def test_page_etag_differs_per_region(page_client):
    a = page_client.get("/map/boi")
    b = page_client.get("/map/pih")
    assert a.headers["etag"] != b.headers["etag"]
    assert page_client.get("/map/boi", headers={"If-None-Match": a.headers["etag"]}).status_code == 304


# ---- admin ----------------------------------------------------------------


@pytest.fixture
def admin(seeded, monkeypatch):
    app = FastAPI()
    app.include_router(admin_api_router)
    app.include_router(regions_router)
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    client = TestClient(app)
    conn = sqlite3.connect(seeded["path"])
    cur = conn.execute("INSERT INTO account(created_at, role) VALUES (?, 'admin')", (NOW,))
    account_id = cur.lastrowid
    conn.execute("INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at) "
                 "VALUES (?, 'unused', ?, ?)", (account_id, NOW, NOW))
    conn.commit()
    conn.close()
    client.cookies.set(SESSION_COOKIE_NAME, asyncio.run(create_session(account_id, device_label=None)))
    return client


ADMIN_ROUTES = [
    ("get", "/api/admin/regions"), ("get", "/api/admin/regions/sync"), ("post", "/api/admin/regions/sync"),
    ("post", "/api/admin/regions/groups/create"), ("post", "/api/admin/regions/groups/update"),
    ("post", "/api/admin/regions/groups/delete"),
]


@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_admin_routes_need_a_role(seeded, method, path):
    app = FastAPI()
    app.include_router(admin_api_router)
    app.include_router(regions_router)
    anon = TestClient(app)
    r = getattr(anon, method)(path, **({"json": {}} if method == "post" else {}))
    assert r.status_code in (401, 404)


def test_admin_lists_regions_with_fetch_time(admin):
    body = admin.get("/api/admin/regions").json()
    assert [r["code"] for r in body["regions"]] == ["BOI", "IDA", "PIH", "SMN"]
    assert body["regions"][0]["fetched_at"] == 1000
    assert body["sync"]["running"] is False


def test_admin_group_crud_and_validation_errors(admin):
    r = admin.post("/api/admin/regions/groups/create", json={"name": "Eastern", "regions": ["PIH", "ida"], "groups": []})
    assert r.status_code == 201
    g = r.json()
    assert g["slug"] == "eastern" and g["regions"] == ["IDA", "PIH"]
    # duplicate slug
    dup = admin.post("/api/admin/regions/groups/create", json={"name": "Eastern", "regions": [], "groups": []})
    assert dup.status_code == 409 and "slug" in dup.json()["error"]
    # unknown member, bad slug, slug that is a region code
    assert admin.post("/api/admin/regions/groups/create", json={"name": "X", "regions": ["NOPE"]}).status_code == 400
    assert admin.post("/api/admin/regions/groups/create", json={"name": "X", "slug": "Bad Slug"}).status_code == 400
    assert admin.post("/api/admin/regions/groups/create", json={"name": "X", "slug": "boi"}).status_code == 409
    assert admin.post("/api/admin/regions/groups/create", json={"name": ""}).status_code == 400
    # nest + cycle
    top = admin.post("/api/admin/regions/groups/create",
                     json={"name": "Top", "regions": ["BOI"], "groups": [g["id"]]}).json()
    cyc = admin.post("/api/admin/regions/groups/update",
                     json={"id": g["id"], "name": "Eastern", "regions": [], "groups": [top["id"]]})
    assert cyc.status_code == 409 and "itself" in cyc.json()["error"]
    # update members
    up = admin.post("/api/admin/regions/groups/update",
                    json={"id": g["id"], "name": "Eastern Idaho", "slug": "eastern", "regions": ["SMN"], "groups": []})
    assert up.status_code == 200 and up.json()["regions"] == ["SMN"] and up.json()["name"] == "Eastern Idaho"
    assert admin.post("/api/admin/regions/groups/update", json={"id": 999, "name": "x"}).status_code == 404
    # delete needs the exact name
    assert admin.post("/api/admin/regions/groups/delete", json={"id": g["id"], "name": "wrong"}).status_code == 409
    assert admin.post("/api/admin/regions/groups/delete", json={"id": g["id"], "name": "Eastern Idaho"}).status_code == 200
    assert admin.post("/api/admin/regions/groups/delete", json={"id": g["id"], "name": "Eastern Idaho"}).status_code == 404
    groups = admin.get("/api/admin/regions").json()["groups"]
    assert [x["slug"] for x in groups] == ["top"] and groups[0]["groups"] == []


def test_admin_sync_starts_in_the_background_and_refuses_a_second(admin, monkeypatch):
    started = []

    def fake_start(mode="all"):
        started.append(mode)
        return len(started) == 1

    monkeypatch.setattr(regions, "start_import", fake_start)
    assert admin.post("/api/admin/regions/sync").status_code == 202
    assert admin.post("/api/admin/regions/sync").status_code == 409
    assert started == ["all", "all"]


# ---- fetch one region by pasted link --------------------------------------

import httpx

TWF_LINKS = ["twf.meshmapper.net", "https://twf.meshmapper.net/", "http://TWF.meshmapper.net/anything?x=1",
             "twf", "TWF", "  Twf.MeshMapper.net  ", "https://twf.meshmapper.net:443/", "twf.meshmapper.net/a/b#frag"]


@pytest.mark.parametrize("link", TWF_LINKS)
def test_link_normalizes_to_the_code(link):
    assert regions.normalize_region_link(link) == "TWF"


@pytest.mark.parametrize("link", [
    "evil.com", "twf.meshmapper.net.evil.com", "https://evil.com/twf.meshmapper.net", "../", "../etc/passwd",
    "meshmapper.net", "a.b.meshmapper.net", "https://user@twf.meshmapper.net@evil.com/", "ftp://twf.meshmapper.net/",
    "twf.meshmapper.net:8080", "https://twf.meshmapper.net:9999/", "", "   ", None, 7, "twf meshmapper", "tw f",
    "http://169.254.169.254/", "http://localhost/", "x" * 400, "twf.meshmapper.org", "evilmeshmapper.net",
])
def test_link_rejects_anything_else(link):
    assert regions.normalize_region_link(link) is None


def _page_client(monkeypatch, handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"User-Agent": regions.USER_AGENT})
    monkeypatch.setattr(regions, "make_client", lambda: client)


def _twf_site(seen=None):
    page = (FIX / "twf.html").read_text()

    def handler(request):
        if seen is not None:
            seen.append(str(request.url))
        if request.url.host == "twf.meshmapper.net":
            return httpx.Response(200, text=page)
        return httpx.Response(404)

    return handler


def test_fetch_route_adds_a_new_region_then_updates_it(admin, monkeypatch, seeded):
    seen = []
    _page_client(monkeypatch, _twf_site(seen))
    r = admin.post("/api/admin/regions/fetch", json={"link": "https://TWF.meshmapper.net/whatever?x"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] == "TWF" and body["name"].startswith("Twin Falls")
    assert body["points"] > 10 and body["new_or_updated"] == "new"
    assert "squares_in_region" not in body
    assert body["claimed_squares"] == body["claimed_meshcore"] + body["claimed_meshtastic"]
    assert seen == ["https://twf.meshmapper.net/"]          # built from the code, not the pasted URL
    # it is in the admin table and the public list straight away
    assert "TWF" in [x["code"] for x in admin.get("/api/admin/regions").json()["regions"]]
    assert "TWF" in [x["code"] for x in admin.get("/api/regions").json()["regions"]]
    # second time: updated in place, no duplicate row
    _page_client(monkeypatch, _twf_site())
    again = admin.post("/api/admin/regions/fetch", json={"link": "twf"}).json()
    assert again["new_or_updated"] == "updated"
    assert [x["code"] for x in admin.get("/api/regions").json()["regions"]].count("TWF") == 1


def test_fetch_route_maps_existing_squares_into_the_new_region(admin, monkeypatch, seeded):
    from app.grid import cell_center
    c = sqlite3.connect(seeded["path"], isolation_level=None)
    c.row_factory = sqlite3.Row
    page = regions.parse_region_page((FIX / "twf.html").read_text(), "TWF")
    shape = regions.Shape(page["rings"])
    cid = grid_cell_id(*[(shape.south + shape.north) / 2, (shape.west + shape.east) / 2])
    ok = shape.contains(*cell_center(cid))
    la, lo = cell_indices(cid)
    c.execute("INSERT OR IGNORE INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts, "
              "paint_count, lat_idx, lon_idx) VALUES (?,?, 'RED', 1, ?, 1, ?, ?)", (MC, cid, NOW, la, lo))
    # a credited place whose cell holds no claimed square: it becomes a region_cell row but must not be counted
    pcid = f"{la + 1}_{lo}"
    c.execute("INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, rotates, active, created_at) "
              "VALUES (77, 'summit', 'r77', 'p77', 42.7, -114.5, 100, 'TEST', 0, 1, ?)", (NOW,))
    c.execute("INSERT INTO place_cell(place_id, cell_id) VALUES (77, ?)", (pcid,))
    c.execute("INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at, protocol) "
              "VALUES (77, 1, '2026-10-05', 100, ?, 'mc')", (NOW,))
    regions.map_new_cells()
    _page_client(monkeypatch, _twf_site())
    body = admin.post("/api/admin/regions/fetch", json={"link": "twf"}).json()
    # claimed squares only, per board, in ACTIVE seasons: the seeded four squares (both boards) that fall
    # inside TWF's polygon, plus the one MeshCore square added here when its centre is inside
    expect = {MC: 0, MT: 0}
    for row in c.execute("SELECT season_id, cell_id FROM mc_tile WHERE owner_team IS NOT NULL"):
        if shape.contains(*cell_center(row["cell_id"])):
            expect[row["season_id"]] += 1
    assert body["claimed_meshcore"] == expect[MC] and body["claimed_meshtastic"] == expect[MT]
    assert body["claimed_squares"] == expect[MC] + expect[MT]
    assert expect[MC] >= (1 if ok else 0)
    c.close()


def test_fetch_route_errors(admin, monkeypatch, seeded):
    for bad in ("evil.com", "twf.meshmapper.net.evil.com", "../", ""):
        r = admin.post("/api/admin/regions/fetch", json={"link": bad})
        assert r.status_code == 400 and r.json() == {"error": "Not a MeshMapper region link"}
    assert admin.post("/api/admin/regions/fetch", json={}).status_code == 400
    # the page does not exist
    _page_client(monkeypatch, lambda req: httpx.Response(404))
    r = admin.post("/api/admin/regions/fetch", json={"link": "zzz"})
    assert r.status_code == 404 and "No MeshMapper region" in r.json()["error"]
    # the page exists but has no allZones entry for that code
    other = (FIX / "boi.html").read_text()
    _page_client(monkeypatch, lambda req: httpx.Response(200, text=other))
    assert admin.post("/api/admin/regions/fetch", json={"link": "zzz"}).status_code == 404
    # entry without a polygon or radius
    nob = '<script>var allZones = [{"code":"ZZZ","name":"Z, US","lat":1,"lon":2}];</script>'
    _page_client(monkeypatch, lambda req: httpx.Response(200, text=nob))
    r = admin.post("/api/admin/regions/fetch", json={"link": "zzz"})
    assert r.status_code == 422 and "no boundary" in r.json()["error"]
    # MeshMapper down / slow
    _page_client(monkeypatch, lambda req: httpx.Response(503))
    assert admin.post("/api/admin/regions/fetch", json={"link": "zzz"}).status_code == 502

    def slow(req):
        raise httpx.ReadTimeout("slow", request=req)

    _page_client(monkeypatch, slow)
    r = admin.post("/api/admin/regions/fetch", json={"link": "zzz"})
    assert r.status_code == 504 and "in time" in r.json()["error"]
    # nothing was stored by any failure
    assert "ZZZ" not in [x["code"] for x in admin.get("/api/admin/regions").json()["regions"]]


def test_fetch_route_needs_a_role(seeded):
    app = FastAPI()
    app.include_router(admin_api_router)
    app.include_router(regions_router)
    assert TestClient(app).post("/api/admin/regions/fetch", json={"link": "twf"}).status_code in (401, 404)


# ---- group order: by number of regions, largest first ------------------------


def _slugs(client):
    return [g["slug"] for g in client.get("/api/regions").json()["groups"]]


def _mk(admin, name, regs=(), groups=()):
    r = admin.post("/api/admin/regions/groups/create", json={"name": name, "regions": list(regs), "groups": list(groups)})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_groups_are_listed_largest_first_with_a_region_count(admin, client):
    _mk(admin, "Small", ["BOI", "PIH"])
    _mk(admin, "Big", ["BOI", "PIH", "IDA", "SMN"])
    _mk(admin, "Medium", ["BOI", "PIH", "IDA"])
    body = client.get("/api/regions").json()["groups"]
    assert [(g["slug"], g["region_count"]) for g in body] == [("big", 4), ("medium", 3), ("small", 2)]


def test_ties_break_by_name_a_to_z_ignoring_case(admin, client):
    _mk(admin, "zulu", ["BOI", "PIH"])
    _mk(admin, "Alpha", ["IDA", "SMN"])
    _mk(admin, "mike", ["BOI", "SMN"])
    assert _slugs(client) == ["alpha", "mike", "zulu"]


def test_count_is_recursive_and_distinct(admin, client):
    inner = _mk(admin, "Inner", ["BOI", "PIH"])
    mid = _mk(admin, "Mid", ["PIH", "IDA"], [inner])           # PIH is in both: 3 distinct, not 4
    _mk(admin, "Outer", ["SMN"], [mid, inner])                  # BOI PIH IDA SMN = 4, inner counted once
    counts = {g["slug"]: g["region_count"] for g in client.get("/api/regions").json()["groups"]}
    assert counts == {"inner": 2, "mid": 3, "outer": 4}
    assert _slugs(client) == ["outer", "mid", "inner"]
    # the admin list uses the same order
    assert [g["slug"] for g in admin.get("/api/admin/regions").json()["groups"]] == ["outer", "mid", "inner"]


def test_reordering_follows_edits_not_creation_order(admin, client):
    a = _mk(admin, "A", ["BOI", "PIH", "IDA"])
    _mk(admin, "B", ["BOI", "PIH"])
    assert _slugs(client) == ["a", "b"]
    admin.post("/api/admin/regions/groups/update", json={"id": a, "name": "A", "regions": ["BOI"], "groups": []})
    assert _slugs(client) == ["b", "a"]


def test_the_move_route_is_gone(admin):
    gid = _mk(admin, "G", ["BOI", "PIH"])
    assert admin.post("/api/admin/regions/groups/move", json={"id": gid, "direction": "up"}).status_code in (404, 405)


def test_existing_groups_take_creation_order_from_the_migration(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE region_group (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
                       "slug TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);")
    for n in ("b", "a"):
        conn.execute("INSERT INTO region_group(name, slug, created_at, updated_at) VALUES (?,?,0,0)", (n, n))
    for stmt in MIGRATIONS:
        if "region_group" in stmt:
            conn.execute(stmt)
            conn.execute(stmt) if stmt.startswith("UPDATE") else None
    assert [r[0] for r in conn.execute("SELECT slug FROM region_group ORDER BY display_order, id")] == ["b", "a"]
    conn.close()


# ---- regional responses are shared between worker processes ---------------------


def test_regional_scores_are_shared_across_workers_through_board_cache(seeded, client, monkeypatch):
    """The web role runs several uvicorn worker processes, each with its own in-memory cache. A key built
    by one must not be rebuilt by the others, or only some regional keys ever look cached."""
    calls = []
    real = mc_api_module.scores_for
    monkeypatch.setattr(mc_api_module, "scores_for", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    first = client.get("/api/mc/scores", params={"region": "boi"})
    assert calls == [1]
    mc_api_module._BOARD_CACHE.clear()            # another worker process: empty memory, same database
    second = client.get("/api/mc/scores", params={"region": "boi"})
    assert calls == [1] and second.content == first.content
    # a shared row older than its allowed age is not trusted
    c = sqlite3.connect(seeded["path"])
    c.execute("UPDATE board_cache SET built_at = built_at - 1000")
    c.commit()
    c.close()
    mc_api_module._BOARD_CACHE.clear()
    client.get("/api/mc/scores", params={"region": "boi"})
    assert calls == [1, 1]


def test_each_region_has_its_own_shared_row(seeded, client):
    for r in ("boi", "pih", "idaho"):
        client.get("/api/mc/top", params={"region": r})
    c = sqlite3.connect(seeded["path"])
    keys = {row[0] for row in c.execute("SELECT cache_key FROM board_cache")}
    c.close()
    assert {"mc_top:region:BOI", "mc_top:region:PIH"} <= keys
