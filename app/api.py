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

Most of the legacy tables above are NOT dropped and NOT written to any
more -- three completed seasons of history live in them and the owner
wants that data kept, just no longer surfaced anywhere in this API. The
one exception is `sample`: a privacy audit found it stored ~19m-precision
position history keyed to radio identity, including for radios that
never registered with MeshWars at all, with no deletion anywhere in the
codebase -- so unlike its siblings it was dropped outright (see
app/db.py's SCHEMA comment, right before node_seen, and its MIGRATIONS
DROP TABLE entry). Its only route, /get-samples, was already dead code
(ingest stopped writing `sample` well before this was noticed, so the
route only ever returned a hardcoded empty list) and has been removed
along with it. A couple of the routes below (/teams, /team/{node_ref})
used to read the other legacy tables; they have been re-pointed at the
new player/mc_tile model instead, since there is no legacy-shaped
equivalent left to fall back to.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from email.utils import formatdate
from pathlib import Path

import anyio
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from . import mc_api
from .account_api import router as account_router
from .admin_api import router as admin_router
from .admin_ops import router as admin_ops_router
from .auth import new_rate_limit_bucket, require_api_key_principal
from .checkin_api import router as checkin_router
from .client_ip import get_client_ip
from .clientlog_api import router as clientlog_router
from .config import settings
from .db import connect
from .join_api import router as join_router
from .mc_api import router as mc_router
from .mc_ingest import hash_secret, log_raw_batch
from .mc_scoring import team_checkin_points, team_tile_counts
from .node_ref import normalize_node_ref
from .nodes_api import router as nodes_router
from .notice_api import router as notice_router
from .oauth_api import router as oauth_router
from .totp_api import router as totp_router
from .places_api import router as places_router
from .public_api import router as public_router
from .sessions import SessionPrincipal, optional_session, require_session

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
        checkin_points = team_checkin_points(conn, active["id"]) if active else {}

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
        # Basemap key for map2.js's CARTO raster source. Served to the
        # browser deliberately -- a basemap key is inherently public to
        # anyone who loads the map -- but it lives in the environment,
        # not the repo. Empty string when unset, which map2.js treats as
        # "request tiles without a key" (watermarked but working).
        "carto_api_key": settings.carto_api_key,
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
        # Seven-team shaped, same list-of-{team,tiles,...} shape
        # /api/mc/scores and /scores below use -- not the old fixed
        # red/blue/green keys, which only ever made sense for the
        # retired two-team geohash game. `total` (tiles + check-in
        # points, app/mc_scoring.py's team_totals()) is the combined
        # figure that actually decides a season's winner -- see that
        # function's docstring.
        "scoreboard": {
            "teams": [
                {
                    "team": t,
                    "tiles": counts.get(t, 0),
                    "checkin_points": checkin_points.get(t, 0.0),
                    "total": counts.get(t, 0) + checkin_points.get(t, 0.0),
                }
                for t in mc_api.team_list()
            ],
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


def _build_get_nodes(*, include_attribution: bool) -> dict:
    """Build the map's main data payload.

    Split from the route below so it can be served through
    mc_api.cached_json_response -- every open Meshtastic map tab
    re-fetches this on a timer, and without the cache each viewer pays
    for its own query, enrichment pass and serialization of identical
    bytes. See that helper for why the cache is time-based only.

    `coverage` is grid cells straight from mc_tile/mc_tile_score/
    mc_tile_capture (owner team, per-team scores, capture time) --
    replaces the old geohash `tile`/`tile_score`/`tile_capture` reads.
    Team-colored, no identity attached to a cell here -- exactly what
    the privacy rule this pass implements keeps public regardless of
    who is asking. There is no more `samples` key -- the old `sample`
    table and its /get-samples route are gone entirely now (see this
    module's own docstring and app/db.py's SCHEMA comment for the
    privacy reasoning); this `coverage` list is the cell-level
    replacement, and was already the replacement in practice before the
    route was removed.

    `repeaters` is node_seen's per-node marker data. Per Matt's explicit
    call on this endpoint (privacy-hardening pass, 2026-09): the EXACT
    lat/lon/elev/name/time here stay in the response unauthenticated,
    same as always -- these positions are already broadcast in the
    clear on the mesh and already republished by the upstream feeds
    (meshview, CoreScope, mwmesh) this app itself reads from, so
    withholding or coarsening them here would not un-publish anything,
    only make this map worse at the one thing a map is for. What DOES
    get withheld unauthenticated is `team` -- the one field that binds
    a node to a registered MeshWars player rather than just describing
    the node itself (its name/position, which the mesh already
    broadcasts). That is the actual person-to-place JOIN this whole
    pass exists to gate: identity and location are each independently
    public; team attribution is what proves "this specific player was
    right here," and that is exactly the link app/sessions.py's session
    gate keeps behind a login. `include_attribution=False` skips the
    _mt_node_teams() query entirely (not just the field in the
    response) so the common logged-out case doesn't pay for a lookup
    whose result it isn't allowed to see.

    Checked against frontend/mc.js and frontend/map2.js: neither
    fetchBoardCells() (mc.js) nor fetchBoard() (map2.js) reads anything
    but `data.coverage` from this route's response today -- `repeaters`
    (this field) has had no on-page consumer since the map moved onto
    the unified cell-based renderer, team attribution included. So
    dropping `team` for a logged-out request changes zero pixels on the
    current map; it only matters the day a caller starts reading
    `repeaters` again, at which point it will already degrade to an
    unattributed marker instead of a wrong one.
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

        node_teams = _mt_node_teams(conn) if include_attribution else {}
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
                # None unauthenticated (see docstring) -- never the
                # registered player's team unless include_attribution.
                "team": node_teams.get(r["node_id"]) if include_attribution else None,
            })
    finally:
        conn.close()

    return {"coverage": coverage, "repeaters": repeaters}


@router.get("/get-nodes")
async def get_nodes(session: SessionPrincipal | None = Depends(optional_session)) -> Response:
    """The map's main data route. See _build_get_nodes() above.

    Cached separately per auth state (`mt_board_public` vs.
    `mt_board_authed`, not one shared `mt_board` key) -- sharing one
    cache slot across both would mean whichever request happens to
    build it first decides what EVERY caller sees for the next
    board_cache_seconds, logged in or not: a public request could serve
    a cached authenticated build (the leak this whole pass exists to
    close) just as easily as an authenticated request could serve a
    stale public one. Two independent cache entries -- one per shape --
    is the only way this stays both cached and correct.
    """
    cache_key = "mt_board_authed" if session is not None else "mt_board_public"
    return mc_api.cached_json_response(
        cache_key, lambda: _build_get_nodes(include_attribution=session is not None)
    )


# Deliberately NOT the bare "/results" the other Meshtastic data routes
# would suggest (/scores, /top, /top-checkins): that path belongs to the
# results PAGE. A data route and a page cannot share it, and the page is
# the one a person types.
@router.get("/api/results")
async def mt_results() -> dict:
    """Monthly results for the Meshtastic board. See
    mc_api.results_for()."""
    return mc_api.results_for(MT_PROTOCOL)


@router.get("/api/results/{month}/{award}/geo")
async def mt_award_geometry(month: str, award: str) -> JSONResponse:
    """Where a Meshtastic honor was earned, as GeoJSON. Meshtastic
    counterpart of mc_api's route; see mc_api.award_geometry_for()."""
    geo = mc_api.award_geometry_for(MT_PROTOCOL, month, award)
    if geo is None:
        return JSONResponse({"error": "no geometry for that award"}, status_code=404)
    return JSONResponse(geo)


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
    lives on the player row itself and changes only through
    app/join_api.py's switch_team() or app/admin_api.py's
    admin_set_team() -- there is no seasonal reassignment any more, so
    this reads the player roster directly and is not scoped to a season
    at all. Returns one entry per player, not per radio -- a player may
    hold several nodes, and the roster is a list of people, not of
    radios.
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT p.player_id, p.display_name, p.team "
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
        })
    return {"teams": teams}


@router.get("/team/{node_ref}")
async def team_lookup(
    node_ref: str, session: SessionPrincipal = Depends(require_session)
) -> dict:
    """Look up a single Meshtastic radio's registered player and team.

    Accepts the same input shapes app/node_ref.py already defines for
    every other route that takes a node reference (`!a1b2c3d4` or bare
    `a1b2c3d4`, any case) -- replaces the old _parse_node_ref, which also
    accepted a decimal node id or a short_name/name lookup against
    node_seen. Both of those only ever made sense against the old
    auto-enrolling roster; player_node.node_ref is the canonical identity
    now, and it is always stored in exactly this one normalized form.

    Privacy-hardening: this route had NO auth at all until this pass --
    unlike /find just below (session-gated from the start of this pass)
    and /api/mc/find in app/mc_api.py, it was missed the first time
    through even though it answers the exact same question those two
    do, just keyed by node reference instead of by display name: given
    a node, who is this and what team are they on. That is precisely
    the person-to-place link /get-nodes' own docstring in this file
    describes as "kept behind a login" -- Matt's rule is "identity can
    be public, location can be public, the link between them cannot,"
    and node references are broadcast in the clear over the mesh AND
    only 8 hex characters, so this route was both fully enumerable and
    reachable by anyone with a radio, with no session and no rate limit
    standing in the way. Gated behind app/sessions.py's require_session()
    now, same dependency and same 401 failure shape as /find and
    /api/mc/find -- there is deliberately no unauthenticated variant,
    same reasoning as those two. `session` is unused beyond proving one
    exists. Checked frontend/mc.js (the one file that names this route)
    and app/api.py's own docs page before making this change: neither
    calls it, mc.js only mentions it in a comment, so gating it changes
    no page's behavior.
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


# Address-keyed rate limit on GET /find -- its own independent
# _BoundedHits instance, not shared with app/mc_api.py's identically-
# shaped one for /api/mc/find (see that module's own comment on why
# call sites never share rate-limit state, even ones reading the same
# settings). See find_rate_limit_attempts/window_seconds' own comment
# in app/config.py.
_find_addr_rate_limiter = new_rate_limit_bucket()


@router.get("/find")
async def find_player(
    request: Request, name: str, session: SessionPrincipal = Depends(require_session)
):
    """Case-insensitive exact match on a player's display name, scoped
    to the active Meshtastic season -- the Meshtastic counterpart of
    /api/mc/find, and (unlike /team/{node_ref} above) a lookup by NAME
    that returns a bounds box the map can zoom to, same as MeshCore's.
    See app/mc_api.py's find_for(); response shape is identical to
    /api/mc/find's.

    Privacy-hardening: gated behind app/sessions.py's require_session()
    for the exact same reason as /api/mc/find -- see that route's own
    docstring in app/mc_api.py for the full reasoning, which applies
    here unchanged. `session` is unused beyond proving one exists.
    """
    if _find_addr_rate_limiter.limited(
        get_client_ip(request),
        limit=settings.find_rate_limit_attempts,
        window=settings.find_rate_limit_window_seconds,
    ):
        return JSONResponse({"error": "rate limited"}, status_code=429)
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


@router.get("/top-checkins")
async def top_checkin_players() -> list[dict]:
    """Players ranked by check-in points in the active Meshtastic
    season -- the Meshtastic counterpart of /api/mc/top-checkins. See
    app/mc_api.py's top_checkin_for(); response shape is identical to
    /api/mc/top-checkins'.
    """
    return mc_api.top_checkin_for(MT_PROTOCOL)


@router.get("/top-explorer")
async def top_explorer_players() -> list[dict]:
    """Players ranked by Explorer Score in the active Meshtastic
    season -- the Meshtastic counterpart of /api/mc/top-explorer. See
    app/mc_api.py's top_explorer_for(); response shape is identical to
    /api/mc/top-explorer's.
    """
    return mc_api.top_explorer_for(MT_PROTOCOL)


@router.get("/cell/{cell_id}")
async def cell_detail(
    cell_id: str, session: SessionPrincipal | None = Depends(optional_session)
):
    """Rich popup data for a single grid cell -- the cell-keyed
    replacement for the old geohash-keyed /tile/{geohash}. See
    app/mc_api.py's cell_detail_for(), which this calls directly with
    protocol='mt' rather than duplicating its query logic; the response
    shape (once redacted the same way -- see mc_api._redact_cell_detail)
    is identical to /api/mc/cell/{cell_id}'s.
    """
    detail = mc_api.cell_detail_for(MT_PROTOCOL, cell_id)
    if detail is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return mc_api._redact_cell_detail(detail, authenticated=session is not None)


def _node_hex(node_id: int | None) -> str:
    if node_id is None:
        return ""
    return f"!{node_id:08x}"


# app/auth.py's require_api_key_principal(), with BOTH of its optional
# rate limiters left off -- this endpoint never had a dict-based
# pre-auth address limiter or post-auth per-key limiter (unlike
# app/join_api.py, app/nodes_api.py, app/checkin_api.py's routes, or
# even app/mc_api.py's POST /api/mc/status). Its only rate limiting is
# McIngestor.rate_limit_ok() below, a per-key, post-auth check on its
# own settings (mc_ingest_rate_limit_batches/window_seconds) sized for
# a wardriving app's batch cadence -- that stays exactly where it was,
# separate from authentication, since it's ingest-specific throttling
# out of scope for this consolidation. See app/auth.py's module
# docstring for the full rundown of what differs at each of the five
# key-authenticated call sites and why.
#
# allow_session_fallback is ALSO left at its default (False) here, and
# must stay that way: this is the machine ingest path MeshMapper's own
# app posts wardriving batches to over X-API-Key, never a browser --
# accepting a session cookie in its place would let a stolen or shared
# session authenticate a device-only credential's endpoint. The other
# four key-authenticated call sites all pass allow_session_fallback=True
# explicitly; this is the one that doesn't. See app/auth.py's module
# docstring's "opt-in" section and tests/test_auth.py's session-cookie
# tests, which prove both halves of that split.
#
# Called directly below (awaited, not wired up as a FastAPI
# Depends(...)) rather than added as a route parameter: it has to run
# AFTER the settings.mc_ingest_enabled check, so a caller reaches "mc
# ingest disabled" (503) before this ever inspects their key, exactly
# as before this dependency existed -- a Depends(...) parameter would
# resolve before the route body runs at all, which would silently
# reverse that order.
_ingest_principal = require_api_key_principal()


@router.post("/api/mc/ingest")
async def mc_ingest(request: Request) -> JSONResponse:
    """Accepts a batch of wardriving pings pushed by the MeshCore
    MeshMapper app. Must answer fast: authenticate, validate shape, hand
    off to the queue. No scoring or tile work here -- see mc_ingest.py.
    """
    if not settings.mc_ingest_enabled:
        return JSONResponse({"error": "mc ingest disabled"}, status_code=503)

    principal = await _ingest_principal(request)

    # _ingest_principal only ever validates the header (see above), so
    # re-reading it here for the hash is safe -- it's already known
    # non-empty and valid, this just needs the raw bytes again for
    # hash_secret, exactly like the pre-app/auth.py version of this
    # function computed key_hash after its own equivalent checks.
    raw_key = request.headers.get("X-API-Key", "")
    key_hash = hash_secret(raw_key)
    ingestor = request.app.state.mc_ingestor

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
    log_raw_batch(principal.player_id, key_hash, await request.body())

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)

    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return JSONResponse({"error": "bad request"}, status_code=400)

    data = body["data"]
    if not data or len(data) > settings.mc_max_batch_pings:
        return JSONResponse({"error": "bad request"}, status_code=400)

    accepted = ingestor.submit(principal.player_id, key_hash, data, int(time.time()))
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


# Terrain/overlay PMTiles archives (/tiles/<name>.pmtiles, see mount()
# below). NOT served by Starlette's StaticFiles: the pinned
# starlette==0.38.6 (pulled in by fastapi==0.115.0) does not forward an
# incoming Range header through StaticFiles at all -- a PMTiles client
# asking for 127 bytes gets back 200 and the entire multi-hundred-
# megabyte file. starlette==0.41.3 fixes it, but bumping the framework
# to chase one route's behaviour, on a branch heading for production,
# changes everything else in the app along with it. This endpoint
# implements the byte-range contract itself instead.

_TILE_RANGE_RE = re.compile(r"^bytes=(\d+)-(\d*)$")
_TILE_CHUNK_SIZE = 256 * 1024  # streamed read size; keeps a hundreds-of-MB file off the heap


class _UnsatisfiableRange(Exception):
    """The requested Range cannot be honoured -- caller should answer 416."""


def _resolve_tile_path(filename: str) -> Path | None:
    """Map a requested /tiles/<filename> onto a real file inside
    settings.tiles_dir, or return None if it should be refused.

    This endpoint answers to the open internet, so the traversal check
    here is load-bearing: a bug that let a request read outside
    tiles_dir would serve arbitrary files off the container. Three
    separate guards, all required:

    - only a bare *.pmtiles name is servable at all (no .db, no .env,
      no source we didn't mean to publish);
    - an absolute filename is rejected outright before it ever reaches
      the join below -- Path("/tiles-data") / "/etc/passwd" evaluates
      to plain "/etc/passwd" in pathlib (joining an absolute path
      replaces, rather than extends, what came before it), so without
      this check a leading slash would walk straight past tiles_dir;
    - the joined path is resolved (following any symlink) and then
      required to sit inside the resolved tiles_dir via relative_to --
      this is what actually catches "../../etc/passwd" and friends,
      since {filename:path} captures ".." as a literal path segment
      rather than normalizing it away.
    """
    if not filename.endswith(".pmtiles"):
        return None
    if filename.startswith("/") or Path(filename).is_absolute():
        return None

    base = Path(settings.tiles_dir).resolve()
    try:
        candidate = (base / filename).resolve()
        candidate.relative_to(base)
    except (ValueError, OSError):
        return None

    if not candidate.is_file():
        return None
    return candidate


def _parse_tile_range(range_header: str, total: int) -> tuple[int, int]:
    """Parse a Range header into an inclusive (start, end) byte pair.

    PMTiles only ever issues a single range per request: a closed
    'bytes=START-END' for its usual directory/leaf-chunk fetches, or an
    open-ended 'bytes=START-' occasionally. A comma-separated list is a
    multi-range request (RFC 7233); we don't support true
    multipart/byteranges responses, and the correct thing for a server
    that can't is to refuse rather than silently answer with only the
    first range and let a caller that needed all of them get quietly
    wrong data -- so any request naming more than one range is treated
    as unsatisfiable and gets a 416, same as a genuinely out-of-bounds
    one. A leading-less suffix form ('bytes=-500', "last 500 bytes") is
    likewise not something PMTiles asks for and isn't accepted here.
    """
    if "," in range_header:
        raise _UnsatisfiableRange()

    m = _TILE_RANGE_RE.match(range_header.strip())
    if not m:
        raise _UnsatisfiableRange()

    start_s, end_s = m.groups()
    if start_s == "":
        raise _UnsatisfiableRange()

    start = int(start_s)
    end = int(end_s) if end_s else total - 1
    if start >= total or start > end:
        raise _UnsatisfiableRange()

    return start, min(end, total - 1)


async def _stream_tile_range(path: Path, start: int, end: int):
    """Yield the inclusive byte range [start, end] from path, one
    _TILE_CHUNK_SIZE slice at a time.

    Uses anyio's threaded file I/O (the same mechanism Starlette's own
    FileResponse uses internally) rather than a plain open()/read()
    inside this async function -- these files are hundreds of megabytes
    and a blocking read would stall the whole event loop, every other
    request included, for as long as that read takes.
    """
    remaining = end - start + 1
    async with await anyio.open_file(path, "rb") as f:
        await f.seek(start)
        while remaining > 0:
            chunk = await f.read(min(_TILE_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


async def tile_file(filename: str, request: Request) -> Response:
    """Serve one PMTiles archive from settings.tiles_dir with real
    byte-range support -- see the module comment above this section for
    why this exists instead of Starlette's StaticFiles.

    Cache-Control is "public, max-age=31536000, immutable": these
    archives are large and static, and the frontend already appends
    TILE_REV as a cache-busting query param whenever it serves a
    genuinely different file (see frontend/map2.js), so there is no
    "stale until revalidated" case to protect against the way
    _html_page's no-cache exists to protect the HTML shell -- a cached
    copy under one TILE_REV is simply correct forever. ETag/
    If-None-Match still uses _file_etag's mtime+size scheme (matching
    every other file route in this module) so a client that skips the
    cache (or a revalidating proxy) gets a cheap 304 instead of a
    re-download.
    """
    path = _resolve_tile_path(filename)
    if path is None:
        return Response(status_code=404)

    total = path.stat().st_size
    etag, last_modified = _file_etag(path)
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Last-Modified": last_modified,
        "Cache-Control": "public, max-age=31536000, immutable",
    }

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)

    range_header = request.headers.get("range")
    if range_header:
        try:
            start, end = _parse_tile_range(range_header, total)
        except _UnsatisfiableRange:
            return Response(
                status_code=416,
                headers={**headers, "Content-Range": f"bytes */{total}"},
            )
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        headers["Content-Length"] = str(end - start + 1)
        return StreamingResponse(
            _stream_tile_range(path, start, end),
            status_code=206,
            headers=headers,
            media_type="application/octet-stream",
        )

    headers["Content-Length"] = str(total)
    return StreamingResponse(
        _stream_tile_range(path, 0, total - 1),
        status_code=200,
        headers=headers,
        media_type="application/octet-stream",
    )


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


def _inject_head(html: str) -> str:
    """Insert settings-driven <head> content shared by every public page.

    Currently just the Search Console ownership tag
    (settings.google_site_verification) -- present only when the setting
    is non-empty. An empty content="" tag is not the same thing to
    Search Console as no tag at all, so "unset" has to mean the tag is
    absent from the markup, not present with nothing in it. Any future
    settings-driven head content belongs here too, rather than
    duplicated across index.html/about.html/join.html.
    """
    if not settings.google_site_verification:
        return html
    tag = f'  <meta name="google-site-verification" content="{settings.google_site_verification}">\n'
    return html.replace("</head>", tag + "</head>", 1)


def _templated_html_page(request: Request, path: Path, missing_message: str) -> Response:
    """Like _html_page, for a top-level page that also needs _inject_head()
    run over it (the map, /results, /rules, /join, /about -- everywhere the verification
    tag can appear).

    Reads and transforms the file instead of handing it to FileResponse,
    which means the ETag can no longer be _file_etag's mtime+size
    shortcut: flipping GOOGLE_SITE_VERIFICATION in .env and restarting
    changes what this returns without touching the HTML file on disk at
    all, and an mtime-based ETag would then hand a returning browser a
    304 for a page whose actual content changed. Hashing the rendered
    bytes instead keeps the ETag honest about what was actually sent,
    at the cost of reading the (small) file on every request rather than
    streaming it -- the same no-cache/If-None-Match/304 contract as
    _html_page otherwise.
    """
    if not path.exists():
        return HTMLResponse(f"<h1>meshwars</h1><p>{missing_message}</p>", status_code=404)

    content = _inject_head(path.read_text(encoding="utf-8")).encode("utf-8")
    last_modified = formatdate(path.stat().st_mtime, usegmt=True)
    etag = f'"{hashlib.md5(content, usedforsecurity=False).hexdigest()}"'

    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304,
            headers={
                "ETag": etag,
                "Last-Modified": last_modified,
                "Cache-Control": "no-cache",
            },
        )

    return HTMLResponse(
        content,
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
    app.include_router(account_router)
    app.include_router(oauth_router)
    app.include_router(totp_router)
    app.include_router(admin_router)
    app.include_router(admin_ops_router)
    app.include_router(nodes_router)
    app.include_router(checkin_router)
    app.include_router(public_router)
    app.include_router(places_router)
    app.include_router(notice_router)
    app.include_router(clientlog_router)

    # Static frontend
    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    if frontend_dir.exists():
        app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

        # Terrain/overlay PMTiles archives (USFS roads+trails, public
        # lands) -- previously fetched cross-origin from navi at runtime,
        # which broke every time navi's archives were rebuilt in place: a
        # browser holding byte ranges of the old file kept serving them
        # against a file that had since changed shape underneath it.
        # Same-origin now, so no CORS is needed. NOT a StaticFiles mount
        # -- see the module comment above tile_file() for why: the
        # pinned starlette==0.38.6 doesn't forward Range through
        # StaticFiles, so PMTiles' range-based reads need this explicit
        # route instead. Skipped entirely when the directory isn't there
        # (e.g. a dev checkout that hasn't set up TILES_DIR) -- same
        # pattern as /static above.
        tiles_dir = Path(settings.tiles_dir)
        if tiles_dir.exists():
            app.get("/tiles/{filename:path}", include_in_schema=False)(tile_file)

        # The MapLibre map (frontend/map2.html + map2.js/.css) -- built
        # alongside the original Leaflet map at /map-legacy below, and
        # now the front page in its own right rather than a staging-only
        # proof off to the side. Carries its own copy of this page's SEO
        # markup (title/description/canonical/OG/JSON-LD/favicon) plus
        # the nav, territory panel, and winner banner ported over from
        # frontend/index.html + mc.js -- see map2.html/map2.js for what
        # that port did and did not carry forward.
        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def index(request: Request):
            return _templated_html_page(request, frontend_dir / "map2.html", "map page not bundled")

        # The original Leaflet map, kept reachable for side-by-side
        # comparison now that / serves the MapLibre map instead. noindex
        # in its own <head> (frontend/index.html) so it never competes
        # with / for the site's own search identity.
        @app.get("/map-legacy", response_class=HTMLResponse, include_in_schema=False)
        async def map_legacy_page(request: Request):
            return _templated_html_page(request, frontend_dir / "index.html", "legacy map page not bundled")

        @app.get("/join", response_class=HTMLResponse, include_in_schema=False)
        async def join_page(request: Request):
            return _templated_html_page(request, frontend_dir / "join.html", "join page not bundled")

        # The account layer's own page (app/account_api.py) -- which
        # sign-in methods are connected, the linked player (or the
        # connect-by-key flow to claim one), and active sessions. Served
        # the same way as every other top-level page here; the route
        # itself carries no session check of its own -- GET /api/account
        # (session-cookie-authenticated) is what actually decides
        # signed-in vs. signed-out, and frontend/account.js renders
        # either state from that response.
        @app.get("/account", response_class=HTMLResponse, include_in_schema=False)
        async def account_page(request: Request):
            return _templated_html_page(request, frontend_dir / "account.html", "account page not bundled")

        # The case-4 sign-in decision screen (frontend/link.js) -- reached
        # only by redirect from GET /auth/{provider}/callback when a
        # provider identity has never been seen before. Not in the nav;
        # a person only ever arrives here via that redirect.
        @app.get("/link", response_class=HTMLResponse, include_in_schema=False)
        async def link_page(request: Request):
            return _templated_html_page(request, frontend_dir / "link.html", "link page not bundled")

        # The confirmation screen GET /auth/contact-email/verify
        # (app/oauth_api.py) redirects a real browser to once it has
        # redeemed (or failed to redeem) a mailed verification link --
        # ?ok=1/?ok=0 on the query string, read client-side by
        # frontend/verify-email.js. Not in the nav; reached only by that
        # redirect, same as /link above.
        @app.get("/account/verify-email", response_class=HTMLResponse, include_in_schema=False)
        async def verify_email_page(request: Request):
            return _templated_html_page(request, frontend_dir / "verify-email.html", "verify-email page not bundled")

        # Where a real browser lands to type in a second factor after a
        # password or magic-link sign-in verifies on an account that
        # has TOTP active (app/oauth_api.py's password_start()/
        # email_callback(), which set the mw_totp_challenge cookie --
        # see app/totp_api.py's own module docstring for the full
        # mechanism). Not in the nav, and reached no other way -- same
        # "small standalone screen, only ever arrived at by redirect"
        # shape as /link and /account/verify-email just above.
        @app.get("/verify-totp", response_class=HTMLResponse, include_in_schema=False)
        async def verify_totp_page(request: Request):
            return _templated_html_page(request, frontend_dir / "verify-totp.html", "verify-totp page not bundled")

        @app.get("/about", response_class=HTMLResponse, include_in_schema=False)
        async def about_page(request: Request):
            return _templated_html_page(request, frontend_dir / "about.html", "about page not bundled")

        @app.get("/results", response_class=HTMLResponse, include_in_schema=False)
        async def results_page(request: Request):
            return _templated_html_page(request, frontend_dir / "results.html", "results page not bundled")

        @app.get("/rules", response_class=HTMLResponse, include_in_schema=False)
        async def rules_page(request: Request):
            return _templated_html_page(request, frontend_dir / "rules.html", "rules page not bundled")

        # Player-facing how-to reference: setup, account management,
        # troubleshooting, reading the interface. Deliberately NOT where
        # the mechanics live -- those stay on /rules and this page links
        # to them rather than restating them, same separation of concerns
        # /rules and /about already keep (rules = the numbers, about =
        # the pitch, docs = the how-to). Admin/operator procedures are
        # not documented here; they stay in the repo's own docs/ tree.
        @app.get("/docs", response_class=HTMLResponse, include_in_schema=False)
        async def docs_page(request: Request):
            return _templated_html_page(request, frontend_dir / "docs.html", "docs page not bundled")

        # Not in the top nav (no sign-off for a new nav entry) -- linked
        # instead from the site footer. Same top-level-page pattern as
        # every other route in this block.
        @app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
        async def privacy_page(request: Request):
            return _templated_html_page(request, frontend_dir / "privacy.html", "privacy page not bundled")

        # Alias for / (frontend/map2.html, same handler target as index()
        # above) -- kept working for anyone who already has this URL
        # open or bookmarked from before the MapLibre map became the
        # front page.
        @app.get("/map2", response_class=HTMLResponse, include_in_schema=False)
        async def map2_page(request: Request):
            return _templated_html_page(request, frontend_dir / "map2.html", "map2 page not bundled")

        # Not in the nav bar on purpose -- this is a reference for the
        # handful of people building against the API, linked from the
        # foot of /about rather than offered to every visitor.
        @app.get("/api", response_class=HTMLResponse, include_in_schema=False)
        async def api_docs_page(request: Request):
            return _templated_html_page(request, frontend_dir / "api.html", "api docs not bundled")

        # robots.txt / sitemap.xml: plain static files, same explicit
        # top-level-route pattern as the three pages above (not folded
        # into the /static mount, which is cache-friendly but lives
        # under a /static/ prefix -- both of these have to answer at the
        # bare site root for a crawler to find them at all) and the same
        # _html_page no-cache/ETag handling. Neither needs _inject_head
        # -- there's no settings-driven content in either one.
        @app.get("/robots.txt", include_in_schema=False)
        async def robots_txt(request: Request):
            return _html_page(request, frontend_dir / "robots.txt", "robots.txt not bundled")

        @app.get("/sitemap.xml", include_in_schema=False)
        async def sitemap_xml(request: Request):
            return _html_page(request, frontend_dir / "sitemap.xml", "sitemap.xml not bundled")
