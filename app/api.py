"""HTTP endpoints for the Meshtastic board.

Historically this module implemented its own retired geohash/tile
fortress game (the `season` / `tile` / `tile_score` / `tile_capture` /
`tile_capture_log` / `tile_unique_painter` / `sample` / `activity` /
`team_assignment` tables, app/scoring.py, app/draft.py, app/seasons.py).
That game is gone -- see app/ingest.py's module docstring for the full
story of the cutover. Every route below now reads the same
mc_season/mc_tile* model app/mc_api.py already serves for the MeshCore
board, scoped to protocol='mt' via app/mc_api.py's protocol-parameterized
helpers (active_season, board_for, scores_for, history_for,
cell_detail_for, ...) rather than duplicating that query logic here.

The legacy tables above are NOT dropped and NOT written to any more --
three completed seasons of history live in them and the owner wants that
data kept, just no longer surfaced anywhere in this API. A couple of
routes below (/get-samples, /teams, /team/{node_ref}) used to read them
too; they have been re-pointed at the new player/mc_tile model instead,
since there is no legacy-shaped equivalent left to fall back to.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from email.utils import formatdate
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from . import mc_api
from .admin_api import router as admin_router
from .config import settings
from .db import connect
from .join_api import router as join_router
from .mc_api import router as mc_router
from .mc_ingest import hash_secret, log_raw_batch
from .mc_scoring import team_tile_counts
from .node_ref import normalize_node_ref
from .nodes_api import router as nodes_router

log = logging.getLogger("api")

router = APIRouter()

# This board's protocol tag in mc_season/mc_tile* etc. Duplicated as a
# plain literal (matching app/ingest.py's own module-level PROTOCOL
# constant) rather than imported from there: app/ingest.py imports
# _node_hex from this module, so importing app/ingest.py back from here
# would be a circular import. Keep the two in sync by hand if this ever
# changes -- unlikely, since it is also baked into player_node,
# player_cell_ping, and mc_season rows already on disk.
MT_PROTOCOL = "mt"


def _truncate(ts: int) -> int:
    """The frontend uses truncated time (×100000 ms divisor). Convert
    epoch seconds -> ms -> divided by 100000."""
    return int((ts * 1000) / 100000)


@router.get("/config")
async def config() -> dict:
    now_ts = int(time.time())
    conn = connect()
    try:
        active = mc_api.active_season(conn, MT_PROTOCOL)

        # Derive map center from node_seen for the current season.
        center, zoom = _derive_map_center(conn, active["id"] if active else None)
        counts = team_tile_counts(conn, active["id"]) if active else {}

        # See mc_api.winner_banner_for() -- this used to build the banner
        # dict inline here (the only caller that needed it, before /season
        # below and mc_api's own /api/mc/season also needed the identical
        # shape for the MeshCore board's winner banner).
        banner = mc_api.winner_banner_for(conn, MT_PROTOCOL, now_ts)
    finally:
        conn.close()

    resp = {
        "centerPos": center,
        "initialZoom": zoom,
        "maxDistanceMiles": 0,
        "meshview_url": settings.meshview_url,
        "mc_default_view": settings.mc_default_view,
        "join_meshtastic_enabled": settings.join_meshtastic_enabled,
        "play_area": {
            "north": settings.play_area_north,
            "south": settings.play_area_south,
            "west": settings.play_area_west,
            "east": settings.play_area_east,
        },
        "season": {
            "id": active["id"] if active else None,
            "started_at": active["started_at"] if active else None,
            "ends_at": active["ends_at"] if active else None,
        },
        "winner_banner": banner,
        # Seven-team shaped, same list-of-{team,tiles} shape
        # /api/mc/scores and /scores below use -- not the old fixed
        # red/blue/green keys, which only ever made sense for the
        # retired two-team geohash game.
        "scoreboard": {
            "teams": [{"team": t, "tiles": counts.get(t, 0)} for t in mc_api.team_list()],
        },
        "now": now_ts,
    }

    # Only surfaced when the owner has explicitly opted in AND a code is
    # actually configured -- omitted entirely (not empty, not null) in
    # every other case, per join_invite_code_public's contract.
    if settings.join_invite_code_public and settings.join_invite_code:
        resp["join_invite_code"] = settings.join_invite_code

    return resp


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


def _cell_score_map(conn, season_id: int) -> dict[str, dict[str, float]]:
    """cell_id -> {team: score} for every scored cell in a season, in one
    batch query -- enriches /get-nodes' coverage list with the same
    per-team scores mc_api.cell_detail_for() exposes for a single cell,
    without a query per cell. Raw stored scores, not decayed on read,
    matching cell_detail_for's own choice for this field -- that
    function already doesn't decay a single cell's scores, so this batch
    version stays consistent with it rather than "fixing" it here.
    """
    rows = conn.execute(
        "SELECT cell_id, team, score FROM mc_tile_score WHERE season_id = ?",
        (season_id,),
    ).fetchall()
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        out.setdefault(r["cell_id"], {})[r["team"]] = r["score"]
    return out


def _cell_capture_map(conn, season_id: int) -> dict[str, int]:
    """cell_id -> captured_at for every captured cell in a season, in one
    batch query -- same field mc_api.cell_detail_for() reads per-cell.
    """
    rows = conn.execute(
        "SELECT cell_id, captured_at FROM mc_tile_capture WHERE season_id = ?",
        (season_id,),
    ).fetchall()
    return {r["cell_id"]: r["captured_at"] for r in rows}


def _mt_node_teams(conn) -> dict[int, str]:
    """node_id -> team for every registered, non-disabled Meshtastic
    player's radio. Node ids not in this dict are not registered players
    -- unlike the retired snake-draft team_assignment table (which
    auto-enrolled every known node and fell back to a "GREEN" neutral
    team when rendering it), there is no neutral/unregistered team in the
    new model: only a registered player has a team at all.
    """
    rows = conn.execute(
        "SELECT pn.node_ref, p.team "
        "  FROM player_node pn JOIN player p ON p.player_id = pn.player_id "
        " WHERE pn.protocol = ? AND p.disabled_at IS NULL",
        (MT_PROTOCOL,),
    ).fetchall()
    out: dict[int, str] = {}
    for r in rows:
        try:
            out[int(r["node_ref"].lstrip("!"), 16)] = r["team"]
        except (ValueError, AttributeError):
            continue
    return out


@router.get("/get-nodes")
async def get_nodes() -> dict:
    """The map's main data route.

    `coverage` is grid cells straight from mc_tile/mc_tile_score/
    mc_tile_capture (owner team, per-team scores, capture time) --
    replaces the old geohash `tile`/`tile_score`/`tile_capture` reads.
    `repeaters` still comes from node_seen, same as before this cutover;
    that is a map-marker/display concern entirely separate from scoring
    and was never part of the retired game logic. There is no more
    `samples` key -- see /get-samples below for why.
    """
    conn = connect()
    try:
        active = mc_api.active_season(conn, MT_PROTOCOL)
        if not active:
            return {"coverage": [], "repeaters": []}
        season_id = active["id"]

        cells = mc_api.board_for(MT_PROTOCOL)
        score_map = _cell_score_map(conn, season_id)
        cap_map = _cell_capture_map(conn, season_id)
        teams = mc_api.team_list()

        coverage = []
        for c in cells:
            cid = c["cell_id"]
            coverage.append({
                "cell_id": cid,
                "owner_team": c["owner_team"],
                "last_report_ts": c["last_report_ts"],
                "paint_count": c["paint_count"],
                "captured_at": cap_map.get(cid),
                "scores": {t: score_map.get(cid, {}).get(t, 0) for t in teams},
                "south": c["south"],
                "west": c["west"],
                "north": c["north"],
                "east": c["east"],
            })

        node_teams = _mt_node_teams(conn)
        node_rows = conn.execute(
            "SELECT node_id, name, lat, lon, elev, last_seen "
            "  FROM node_seen "
            " WHERE season_id = ? AND lat IS NOT NULL AND lon IS NOT NULL",
            (season_id,),
        ).fetchall()
        repeaters = []
        for r in node_rows:
            repeaters.append({
                "id": _node_hex(r["node_id"]),
                "name": r["name"],
                "lat": r["lat"],
                "lon": r["lon"],
                "elev": r["elev"] or 0,
                "time": _truncate(r["last_seen"]),
                "team": node_teams.get(r["node_id"]),
            })
    finally:
        conn.close()

    return {"coverage": coverage, "repeaters": repeaters}


@router.get("/get-samples")
async def get_samples() -> dict:
    """Per-position sample data from the retired geohash board (the old
    `sample` table) has no equivalent in the new grid-cell model --
    ingest no longer writes anything there (see app/ingest.py), and
    /get-nodes' `coverage` list is the cell-level replacement. This
    route is kept, always empty, so an old caller gets a sane empty
    response instead of a 404 or 500 -- not because it still does
    anything.
    """
    return {"keys": []}


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
    """Seven-team tile counts for the active Meshtastic season. See
    app/mc_api.py's scores_for(), which this calls directly with
    protocol='mt' rather than duplicating its query logic -- this is the
    exact same shape /api/mc/scores returns for the MeshCore board.
    """
    return mc_api.scores_for(MT_PROTOCOL)


@router.get("/history")
async def history() -> dict:
    """Closed Meshtastic seasons under the new player model, newest
    first, each with its final per-team tile tally. See app/mc_api.py's
    history_for().

    Per the owner's explicit decision, this surfaces only 'mt' seasons
    from mc_season -- the three legacy geohash-era seasons remain in the
    `season` table (never dropped, still queryable directly against the
    database) but are not read through here any more.
    """
    return {"seasons": mc_api.history_for(MT_PROTOCOL)}


@router.get("/season")
async def season_info() -> dict:
    """Season status plus the winner banner for the Meshtastic board.
    The counterpart of app/mc_api.py's /api/mc/season -- both call the
    same winner_banner_for(), so the `winner_banner` shape is identical
    on both boards. `winner_banner_active` (a plain bool, kept for any
    existing caller that only wants that) is now redundant with
    `winner_banner is not None`, but is not worth removing over.
    """
    now_ts = int(time.time())
    conn = connect()
    try:
        active = mc_api.active_season(conn, MT_PROTOCOL)
        active = dict(active) if active else None
        closed = mc_api.latest_closed_season(conn, MT_PROTOCOL)
        closed = dict(closed) if closed else None
        banner = mc_api.winner_banner_for(conn, MT_PROTOCOL, now_ts)
    finally:
        conn.close()
    return {
        "active": active,
        "latest_closed": closed,
        "winner_banner_active": banner is not None,
        "winner_banner": banner,
        "now": now_ts,
    }


@router.get("/teams")
async def teams_list() -> dict:
    """Full roster of registered Meshtastic players, grouped by team.

    Unlike the retired snake-draft `team_assignment` table (reassigned
    every season, keyed by season_id), a player's team in the new model
    is a permanent attribute of the player row (see app/join_api.py) --
    there is no seasonal reassignment any more, so this reads the player
    roster directly and is not scoped to a season at all.
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT p.player_id, p.display_name, p.team, pn.node_ref "
            "  FROM player p "
            "  JOIN player_node pn ON pn.player_id = p.player_id AND pn.protocol = ? "
            " WHERE p.disabled_at IS NULL "
            " ORDER BY p.team, p.display_name",
            (MT_PROTOCOL,),
        ).fetchall()
    finally:
        conn.close()

    teams: dict[str, list[dict]] = {t: [] for t in mc_api.team_list()}
    for r in rows:
        teams.setdefault(r["team"], []).append({
            "player_id": r["player_id"],
            "display_name": r["display_name"],
            "node_hex": r["node_ref"],
        })
    return {"teams": teams}


@router.get("/team/{node_ref}")
async def team_lookup(node_ref: str) -> dict:
    """Look up a single Meshtastic radio's registered player and team.

    Accepts the same input shapes app/node_ref.py already defines for
    every other route that takes a node reference (`!a1b2c3d4` or bare
    `a1b2c3d4`, any case) -- replaces the old _parse_node_ref, which also
    accepted a decimal node id or a short_name/name lookup against
    node_seen. Both of those only ever made sense against the old
    auto-enrolling roster; player_node.node_ref is the canonical identity
    now, and it is always stored in exactly this one normalized form.
    """
    ref = normalize_node_ref(node_ref)
    if ref is None:
        return {"found": False, "error": "could not parse node reference"}

    conn = connect()
    try:
        row = conn.execute(
            "SELECT p.player_id, p.display_name, p.team "
            "  FROM player_node pn JOIN player p ON p.player_id = pn.player_id "
            " WHERE pn.protocol = ? AND pn.node_ref = ? AND p.disabled_at IS NULL",
            (MT_PROTOCOL, ref),
        ).fetchone()

        tiles_owned = 0
        if row:
            active = mc_api.active_season(conn, MT_PROTOCOL)
            if active:
                tc = conn.execute(
                    "SELECT COUNT(*) AS c FROM mc_tile "
                    " WHERE season_id = ? AND last_player_id = ?",
                    (active["id"], row["player_id"]),
                ).fetchone()
                tiles_owned = tc["c"] if tc else 0
    finally:
        conn.close()

    if not row:
        return {
            "found": False,
            "node_ref": ref,
            "message": "not a registered player radio",
        }
    return {
        "found": True,
        "node_ref": ref,
        "player_id": row["player_id"],
        "display_name": row["display_name"],
        "team": row["team"],
        "tiles_owned": tiles_owned,
    }


@router.get("/find")
async def find_player(name: str):
    """Case-insensitive exact match on a player's display name, scoped
    to the active Meshtastic season -- the Meshtastic counterpart of
    /api/mc/find, and (unlike /team/{node_ref} above) a lookup by NAME
    that returns a bounds box the map can zoom to, same as MeshCore's.
    See app/mc_api.py's find_for(); response shape is identical to
    /api/mc/find's.
    """
    result = mc_api.find_for(MT_PROTOCOL, name)
    if result is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return result


@router.get("/top")
async def top_players() -> list[dict]:
    """Players ranked by capture-event count in the active Meshtastic
    season -- the Meshtastic counterpart of /api/mc/top. See
    app/mc_api.py's top_for(); response shape is identical to
    /api/mc/top's.
    """
    return mc_api.top_for(MT_PROTOCOL)


@router.get("/cell/{cell_id}")
async def cell_detail(cell_id: str):
    """Rich popup data for a single grid cell -- the cell-keyed
    replacement for the old geohash-keyed /tile/{geohash}. See
    app/mc_api.py's cell_detail_for(), which this calls directly with
    protocol='mt' rather than duplicating its query logic; the response
    shape is identical to /api/mc/cell/{cell_id}'s.
    """
    detail = mc_api.cell_detail_for(MT_PROTOCOL, cell_id)
    if detail is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return detail


def _node_hex(node_id: int | None) -> str:
    if node_id is None:
        return ""
    return f"!{node_id:08x}"


@router.post("/api/mc/ingest")
async def mc_ingest(request: Request) -> JSONResponse:
    """Accepts a batch of wardriving pings pushed by the MeshCore
    MeshMapper app. Must answer fast: authenticate, validate shape, hand
    off to the queue. No scoring or tile work here -- see mc_ingest.py.
    """
    if not settings.mc_ingest_enabled:
        return JSONResponse({"error": "mc ingest disabled"}, status_code=503)

    raw_key = request.headers.get("X-API-Key", "")
    if not raw_key:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    ingestor = request.app.state.mc_ingestor
    auth = await ingestor.authenticate(raw_key)
    if auth.status in ("not_found", "revoked"):
        # Generic message for both cases -- don't reveal whether a key exists.
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if auth.status == "disabled":
        return JSONResponse({"error": "forbidden"}, status_code=403)

    key_hash = hash_secret(raw_key)

    # Per-key rate limit, checked as early as possible on the request
    # path (no database read -- see McIngestor.rate_limit_ok) so a key
    # over budget costs us as little as possible before being rejected.
    if not ingestor.rate_limit_ok(key_hash):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    # Diagnostic only, off by default (mc_raw_log_enabled): logs the raw
    # request body verbatim, which adds file I/O to the request path --
    # meant for short tuning windows, not to run continuously. Placed
    # after auth succeeds, so an unauthenticated caller can never make us
    # write to disk, and before body validation, so malformed payloads
    # are captured too.
    log_raw_batch(auth.player_id, key_hash, await request.body())

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)

    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return JSONResponse({"error": "bad request"}, status_code=400)

    data = body["data"]
    if not data or len(data) > settings.mc_max_batch_pings:
        return JSONResponse({"error": "bad request"}, status_code=400)

    accepted = ingestor.submit(auth.player_id, key_hash, data, int(time.time()))
    if not accepted:
        return JSONResponse({"error": "queue full"}, status_code=503)

    return JSONResponse({"accepted": len(data)}, status_code=202)


def _file_etag(path: Path) -> tuple[str, str]:
    """Reproduce Starlette FileResponse's own ETag/Last-Modified scheme
    (mtime + size, md5) so the value we compare an incoming
    If-None-Match against is exactly the one FileResponse would have
    sent anyway."""
    st = path.stat()
    last_modified = formatdate(st.st_mtime, usegmt=True)
    etag_base = f"{st.st_mtime}-{st.st_size}"
    etag = f'"{hashlib.md5(etag_base.encode(), usedforsecurity=False).hexdigest()}"'
    return etag, last_modified


def _html_page(request: Request, path: Path, missing_message: str) -> Response:
    """Serve a top-level HTML document (the map, /join, /about).

    These are NOT given to the static mount below -- they get an
    explicit Cache-Control: no-cache so a browser always revalidates
    with us before showing a cached copy. Without that directive a
    browser invents its own heuristic expiry and can keep serving a
    stale page after a deploy, invisibly, until a hard reload. The
    /static assets are fine to cache and deliberately keep the
    no-cache directive OFF -- do not "fix" that by moving this header
    onto the StaticFiles mount, that would defeat the point of caching
    them at all.

    "no-cache" means "revalidate every time", not "never store": we
    still honour If-None-Match ourselves (Starlette's FileResponse
    does not do this on its own) and return a bare 304 when the file
    hasn't changed, so revalidation stays cheap.
    """
    if not path.exists():
        return HTMLResponse(f"<h1>meshwars</h1><p>{missing_message}</p>", status_code=404)

    etag, last_modified = _file_etag(path)
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304,
            headers={
                "ETag": etag,
                "Last-Modified": last_modified,
                "Cache-Control": "no-cache",
            },
        )

    return FileResponse(
        path,
        headers={
            "Cache-Control": "no-cache",
            "ETag": etag,
            "Last-Modified": last_modified,
        },
    )


def mount(app: FastAPI) -> None:
    app.include_router(router)
    app.include_router(mc_router)
    app.include_router(join_router)
    app.include_router(admin_router)
    app.include_router(nodes_router)

    # Static frontend
    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    if frontend_dir.exists():
        app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def index(request: Request):
            return _html_page(request, frontend_dir / "index.html", "map page not bundled")

        @app.get("/join", response_class=HTMLResponse, include_in_schema=False)
        async def join_page(request: Request):
            return _html_page(request, frontend_dir / "join.html", "join page not bundled")

        @app.get("/about", response_class=HTMLResponse, include_in_schema=False)
        async def about_page(request: Request):
            return _html_page(request, frontend_dir / "about.html", "about page not bundled")
