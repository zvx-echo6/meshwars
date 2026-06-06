"""HTTP endpoints.

We implement the exact contract the existing coverage-map frontend
expects (/config, /get-nodes, /get-samples, /live-tracks,
/live-tracks/stream) and add a few game-specific ones (/scores,
/history, /season).

Tiles in /get-nodes carry an `owner_team` field; the frontend uses that
to pick the fill color before falling back to the existing palette.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from .config import settings
from .db import connect
from .seasons import (
    get_active_season,
    get_history,
    get_latest_closed_season,
    get_team_assignments,
    now_s,
    tally_tiles,
    winner_banner_active,
)

log = logging.getLogger("api")

router = APIRouter()


def _truncate(ts: int) -> int:
    """The frontend uses truncated time (×100000 ms divisor). Convert
    epoch seconds -> ms -> divided by 100000."""
    return int((ts * 1000) / 100000)


@router.get("/config")
async def config() -> dict:
    conn = connect()
    try:
        active = get_active_season(conn)
        closed = get_latest_closed_season(conn)

        # Derive map center from node_seen for the current season.
        center, zoom = _derive_map_center(conn, active["id"] if active else None)
        counts = tally_tiles(conn, active["id"]) if active else {"RED": 0, "BLUE": 0, "GREEN": 0}

        banner = None
        if winner_banner_active(closed):
            banner = {
                "season_id": closed["id"],
                "started_at": closed["started_at"],
                "ends_at": closed["ends_at"],
                "winner": closed["winner"],
                "red_tiles": closed["red_tiles"],
                "blue_tiles": closed["blue_tiles"],
                "green_tiles": closed["green_tiles"],
            }
    finally:
        conn.close()

    from .config import settings as _settings
    return {
        "centerPos": center,
        "initialZoom": zoom,
        "maxDistanceMiles": 0,
        "meshview_url": _settings.meshview_url,
        "season": {
            "id": active["id"] if active else None,
            "started_at": active["started_at"] if active else None,
            "ends_at": active["ends_at"] if active else None,
        },
        "winner_banner": banner,
        "scoreboard": {
            "red": counts.get("RED", 0),
            "blue": counts.get("BLUE", 0),
            "green": counts.get("GREEN", 0),
        },
        "now": now_s(),
    }


def _derive_map_center(conn, season_id: int | None) -> tuple[list[float], int]:
    """Median of known node positions, or a global default."""
    if season_id is None:
        return ([0.0, 0.0], 4)
    rows = conn.execute(
        "SELECT lat, lon FROM node_seen "
        " WHERE season_id = ? AND lat IS NOT NULL AND lon IS NOT NULL",
        (season_id,),
    ).fetchall()
    if not rows:
        return ([0.0, 0.0], 4)
    lats = sorted(r["lat"] for r in rows)
    lons = sorted(r["lon"] for r in rows)
    mid = len(lats) // 2
    center_lat = lats[mid]
    center_lon = lons[mid]
    # Zoom heuristic based on lat spread
    span = lats[-1] - lats[0]
    if span < 0.2:
        zoom = 12
    elif span < 1.0:
        zoom = 10
    elif span < 5.0:
        zoom = 8
    else:
        zoom = 6
    return ([center_lat, center_lon], zoom)


@router.get("/get-nodes")
async def get_nodes() -> dict:
    conn = connect()
    try:
        active = get_active_season(conn)
        if not active:
            return {"coverage": [], "samples": [], "repeaters": []}
        season_id = active["id"]
        teams = get_team_assignments(conn, season_id)

        tile_rows = conn.execute(
            "SELECT geohash, rcv, lost, last_sender_node_id, last_report_ts, "
            "       last_snr, last_rssi, owner_team, rptr_json "
            "  FROM tile WHERE season_id = ?",
            (season_id,),
        ).fetchall()

        # Load fortress scores for this season in one batch
        from .scoring import decayed_score
        import time as _time
        now_ts = int(_time.time())
        score_rows = conn.execute(
            "SELECT geohash, team, score, last_update FROM tile_score "
            " WHERE season_id = ?",
            (season_id,),
        ).fetchall()
        # geohash -> {RED: float, BLUE: float}
        score_map: dict[str, dict[str, float]] = {}
        for sr in score_rows:
            score_map.setdefault(sr["geohash"], {})[sr["team"]] = \
                decayed_score(sr["score"], sr["last_update"], now_ts)

        # Capture timestamps for defense-window display
        cap_rows = conn.execute(
            "SELECT geohash, captured_at FROM tile_capture WHERE season_id = ?",
            (season_id,),
        ).fetchall()
        cap_map = {r["geohash"]: r["captured_at"] for r in cap_rows}

        coverage = []
        for r in tile_rows:
            try:
                rptr = json.loads(r["rptr_json"])
            except json.JSONDecodeError:
                rptr = []
            scores = score_map.get(r["geohash"], {})
            owner_score = scores.get(r["owner_team"], 0.0)
            coverage.append({
                "id": r["geohash"],
                "rcv": r["rcv"],
                "lost": r["lost"],
                "rptr": [_node_hex(x) for x in rptr],
                "time": _truncate(r["last_report_ts"]),
                "ut": _truncate(r["last_report_ts"]),
                "lot": _truncate(r["last_report_ts"]),
                "lht": _truncate(r["last_report_ts"]),
                "obs": 1,
                "snr": r["last_snr"],
                "rssi": r["last_rssi"],
                "owner_team": r["owner_team"],
                "last_sender": _node_hex(r["last_sender_node_id"]),
                "score": round(owner_score, 2),
                "red_score": round(scores.get("RED", 0.0), 2),
                "blue_score": round(scores.get("BLUE", 0.0), 2),
                "captured_at": cap_map.get(r["geohash"]),
            })

        # Aggregated samples (the frontend reads /get-nodes.samples too)
        sample_rows = conn.execute(
            "SELECT sample_hash, ts, snr, rssi, path_json, sender_node_id, observed "
            "  FROM sample WHERE season_id = ?",
            (season_id,),
        ).fetchall()
        samples = []
        for r in sample_rows:
            try:
                path = json.loads(r["path_json"])
            except json.JSONDecodeError:
                path = []
            sender_team = teams.get(r["sender_node_id"], "GREEN")
            samples.append({
                "id": r["sample_hash"][:6],  # truncate to coverage-tile precision for aggregation
                "heard": 1,
                "lost": 0,
                "time": _truncate(r["ts"]),
                "path": [_node_hex(x) for x in path],
                "snr": r["snr"],
                "rssi": r["rssi"],
                "obs": True,
                "owner_team": sender_team,
            })

        # Repeater list = nodes with known positions
        node_rows = conn.execute(
            "SELECT node_id, name, lat, lon, elev, last_seen "
            "  FROM node_seen "
            " WHERE season_id = ? AND lat IS NOT NULL AND lon IS NOT NULL",
            (season_id,),
        ).fetchall()
        repeaters = []
        for r in node_rows:
            team = teams.get(r["node_id"], "GREEN")
            repeaters.append({
                "id": _node_hex(r["node_id"]),
                "name": r["name"],
                "lat": r["lat"],
                "lon": r["lon"],
                "elev": r["elev"] or 0,
                "time": _truncate(r["last_seen"]),
                "team": team,
            })
    finally:
        conn.close()

    return {"coverage": coverage, "samples": samples, "repeaters": repeaters}


@router.get("/get-samples")
async def get_samples() -> dict:
    conn = connect()
    try:
        active = get_active_season(conn)
        if not active:
            return {"keys": []}
        season_id = active["id"]
        teams = get_team_assignments(conn, season_id)
        rows = conn.execute(
            "SELECT sample_hash, sender_node_id, ts, snr, rssi, path_json "
            "  FROM sample WHERE season_id = ? "
            " ORDER BY ts DESC LIMIT 5000",
            (season_id,),
        ).fetchall()
    finally:
        conn.close()

    keys = []
    for r in rows:
        try:
            path = json.loads(r["path_json"])
        except json.JSONDecodeError:
            path = []
        sender_team = teams.get(r["sender_node_id"], "GREEN")
        keys.append({
            "name": r["sample_hash"],
            "metadata": {
                "path": [_node_hex(x) for x in path],
                "observed": True,
                "snr": r["snr"],
                "rssi": r["rssi"],
                "time": r["ts"] * 1000,  # frontend expects ms
                "owner_team": sender_team,
                "sender": _node_hex(r["sender_node_id"]),
            },
        })
    return {"keys": keys}


@router.get("/live-tracks")
async def live_tracks() -> dict:
    # v1: empty
    return {"points": []}


@router.get("/live-tracks/stream")
async def live_tracks_stream(request: Request):
    async def gen():
        while True:
            if await request.is_disconnected():
                break
            # Just a keepalive every 30s; v1 doesn't push points.
            yield {"event": "ping", "data": "{}"}
            await asyncio.sleep(30)

    return EventSourceResponse(gen())


@router.get("/scores")
async def scores() -> dict:
    conn = connect()
    try:
        active = get_active_season(conn)
        if not active:
            return {"red": 0, "blue": 0, "green": 0}
        counts = tally_tiles(conn, active["id"])
    finally:
        conn.close()
    return {
        "red": counts.get("RED", 0),
        "blue": counts.get("BLUE", 0),
        "green": counts.get("GREEN", 0),
        "season_id": active["id"],
        "ends_at": active["ends_at"],
    }


@router.get("/history")
async def history() -> dict:
    conn = connect()
    try:
        seasons = get_history(conn, settings.history_max)
    finally:
        conn.close()
    return {"seasons": seasons}


@router.get("/season")
async def season_info() -> dict:
    conn = connect()
    try:
        active = get_active_season(conn)
        closed = get_latest_closed_season(conn)
        banner = winner_banner_active(closed)
    finally:
        conn.close()
    return {
        "active": active,
        "latest_closed": closed,
        "winner_banner_active": banner,
        "now": now_s(),
    }



@router.get("/teams")
async def teams_list() -> dict:
    """Full roster of team assignments for the active season."""
    conn = connect()
    try:
        active = get_active_season(conn)
        if not active:
            return {"season_id": None, "red": [], "blue": []}
        season_id = active["id"]
        rows = conn.execute(
            "SELECT t.node_id, t.team, t.activity_score, "
            "       n.name, n.short_name "
            "  FROM team_assignment t "
            "  LEFT JOIN node_seen n "
            "    ON n.season_id = t.season_id AND n.node_id = t.node_id "
            " WHERE t.season_id = ? "
            " ORDER BY t.team, COALESCE(n.short_name, n.name, CAST(t.node_id AS TEXT))",
            (season_id,),
        ).fetchall()
    finally:
        conn.close()

    red, blue = [], []
    for r in rows:
        item = {
            "node_id": r["node_id"],
            "node_hex": _node_hex(r["node_id"]),
            "name": r["name"],
            "short_name": r["short_name"],
            "activity_score": r["activity_score"],
        }
        (red if r["team"] == "RED" else blue).append(item)
    return {"season_id": season_id, "red": red, "blue": blue}


@router.get("/team/{node_ref}")
async def team_lookup(node_ref: str) -> dict:
    """Look up a single node's team. Accepts decimal id, hex id, or !hex."""
    nid = _parse_node_ref(node_ref)
    if nid is None:
        return {"found": False, "error": "could not parse node reference"}

    conn = connect()
    try:
        active = get_active_season(conn)
        if not active:
            return {"found": False, "error": "no active season"}
        season_id = active["id"]
        row = conn.execute(
            "SELECT t.team, t.activity_score, n.name, n.short_name, n.lat, n.lon "
            "  FROM team_assignment t "
            "  LEFT JOIN node_seen n "
            "    ON n.season_id = t.season_id AND n.node_id = t.node_id "
            " WHERE t.season_id = ? AND t.node_id = ?",
            (season_id, nid),
        ).fetchone()

        # Also check tile count if found
        tile_count = 0
        if row:
            tc = conn.execute(
                "SELECT COUNT(*) AS c FROM tile "
                " WHERE season_id = ? AND last_sender_node_id = ?",
                (season_id, nid),
            ).fetchone()
            tile_count = tc["c"] if tc else 0
    finally:
        conn.close()

    if not row:
        return {
            "found": False,
            "node_id": nid,
            "node_hex": _node_hex(nid),
            "message": "not in current season roster",
        }
    return {
        "found": True,
        "node_id": nid,
        "node_hex": _node_hex(nid),
        "team": row["team"],
        "name": row["name"],
        "short_name": row["short_name"],
        "lat": row["lat"],
        "lon": row["lon"],
        "tiles_owned": tile_count,
    }


def _parse_node_ref(ref: str) -> int | None:
    """Accept decimal int, hex with !, plain hex, or short_name lookup."""
    if not ref:
        return None
    ref = ref.strip()
    # !abcd1234 or abcd1234 → hex
    bare = ref.lstrip("!")
    try:
        if any(c in "abcdefABCDEF" for c in bare):
            return int(bare, 16)
        return int(bare)
    except ValueError:
        pass
    # Maybe it's a short_name — look it up in node_seen
    conn = connect()
    try:
        row = conn.execute(
            "SELECT node_id FROM node_seen "
            " WHERE short_name = ? OR name = ? "
            " ORDER BY last_seen DESC LIMIT 1",
            (ref, ref),
        ).fetchone()
        return row["node_id"] if row else None
    finally:
        conn.close()



@router.get("/tile/{geohash}")
async def tile_detail(geohash: str) -> dict:
    """Rich popup data for a single tile."""
    conn = connect()
    try:
        active = get_active_season(conn)
        if not active:
            return {"found": False}
        season_id = active["id"]

        tile = conn.execute(
            "SELECT geohash, rcv, lost, last_sender_node_id, last_report_ts, "
            "       last_snr, last_rssi, owner_team, last_packet_id "
            "  FROM tile WHERE season_id = ? AND geohash = ?",
            (season_id, geohash),
        ).fetchone()
        if not tile:
            return {"found": False}

        from .scoring import decayed_score
        import time as _time
        now_ts = int(_time.time())

        # Scores per team
        sr = conn.execute(
            "SELECT team, score, last_update FROM tile_score "
            " WHERE season_id = ? AND geohash = ?",
            (season_id, geohash),
        ).fetchall()
        scores = {r["team"]: decayed_score(r["score"], r["last_update"], now_ts) for r in sr}

        # Capture log
        cap_rows = conn.execute(
            "SELECT ts, by_node_id, by_team, from_team, packet_id "
            "  FROM tile_capture_log "
            " WHERE season_id = ? AND geohash = ? "
            " ORDER BY ts DESC LIMIT 5",
            (season_id, geohash),
        ).fetchall()
        cap_count = conn.execute(
            "SELECT COUNT(*) AS c FROM tile_capture_log "
            " WHERE season_id = ? AND geohash = ?",
            (season_id, geohash),
        ).fetchone()["c"]

        # Current defense-window state
        cap_now = conn.execute(
            "SELECT captured_at, captured_by_team FROM tile_capture "
            " WHERE season_id = ? AND geohash = ?",
            (season_id, geohash),
        ).fetchone()

        # Top contributors for owning team (by paint_count)
        top = conn.execute(
            "SELECT p.node_id, p.paint_count, p.first_ts, n.short_name, n.name "
            "  FROM tile_unique_painter p "
            "  LEFT JOIN node_seen n "
            "    ON n.season_id = p.season_id AND n.node_id = p.node_id "
            " WHERE p.season_id = ? AND p.geohash = ? AND p.team = ? "
            " ORDER BY p.paint_count DESC LIMIT 5",
            (season_id, geohash, tile["owner_team"]),
        ).fetchall() if tile["owner_team"] in ("RED", "BLUE") else []

        # Sender label lookup
        sender_row = conn.execute(
            "SELECT short_name, name FROM node_seen "
            " WHERE season_id = ? AND node_id = ?",
            (season_id, tile["last_sender_node_id"]),
        ).fetchone()

        return {
            "found": True,
            "geohash": geohash,
            "owner_team": tile["owner_team"],
            "rcv": tile["rcv"],
            "last_report_ts": tile["last_report_ts"],
            "last_snr": tile["last_snr"],
            "last_rssi": tile["last_rssi"],
            "last_packet_id": tile["last_packet_id"],
            "last_sender": {
                "node_id": tile["last_sender_node_id"],
                "hex": _node_hex(tile["last_sender_node_id"]),
                "short_name": sender_row["short_name"] if sender_row else None,
                "name": sender_row["name"] if sender_row else None,
            },
            "scores": {
                "RED": round(scores.get("RED", 0.0), 2),
                "BLUE": round(scores.get("BLUE", 0.0), 2),
            },
            "captures": {
                "count": cap_count,
                "current_captured_at": cap_now["captured_at"] if cap_now else None,
                "current_captured_by": cap_now["captured_by_team"] if cap_now else None,
                "recent": [
                    {
                        "ts": r["ts"],
                        "node_id": r["by_node_id"],
                        "node_hex": _node_hex(r["by_node_id"]),
                        "team": r["by_team"],
                        "from_team": r["from_team"],
                        "packet_id": r["packet_id"],
                    }
                    for r in cap_rows
                ],
            },
            "top_contributors": [
                {
                    "node_id": r["node_id"],
                    "node_hex": _node_hex(r["node_id"]),
                    "short_name": r["short_name"],
                    "name": r["name"],
                    "paint_count": r["paint_count"],
                    "first_ts": r["first_ts"],
                }
                for r in top
            ],
        }
    finally:
        conn.close()


def _node_hex(node_id: int | None) -> str:
    if node_id is None:
        return ""
    return f"!{node_id:08x}"


def mount(app: FastAPI) -> None:
    app.include_router(router)

    # Static frontend
    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    if frontend_dir.exists():
        app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def index():
            index_path = frontend_dir / "index.html"
            if index_path.exists():
                return FileResponse(index_path)
            return HTMLResponse("<h1>meshwars</h1><p>frontend not bundled</p>")
