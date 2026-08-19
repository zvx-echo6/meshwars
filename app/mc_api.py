"""FastAPI router for the MeshCore board/scores/players/cell endpoints.

NOT mounted here -- a later change wires this into the app the same way
app/api.py wires its own router (see `mount()` there). This module only
builds the router.

The mc_season / mc_season_team_tally / mc_tile / mc_tile_score /
mc_tile_capture / mc_tile_capture_log tables are being added by a
different change landing in parallel with this one. Until that lands
(or if it lands with a different column set than expected), every
route below is expected to see sqlite3.OperationalError("no such
table: mc_*") -- so every query goes through `_safe_query`, which
catches "no such table" and degrades to an empty result instead of a
500. That is a deliberate landing-order guard, not defensive
programming against a real failure mode.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .config import settings
from .db import connect
from .grid import cell_bounds
from .mc_ingest import PROTOCOL as MC_PROTOCOL
from .mc_scoring import team_tile_counts

router = APIRouter()

# ---- status-check rate limiting ---------------------------------------
#
# Bounded per-address tracking, same pattern as app/join_api.py's
# _attempts/_rate_limited (itself modeled on McIngestor._key_cache in
# app/mc_ingest.py): every distinct client IP that hits this public,
# key-authenticated-but-still-abusable endpoint gets a tracking entry.
# Sweep stale entries first when the cap is hit, and only clear the
# whole dict if that alone doesn't bring it back under the cap.
_STATUS_RATE_LIMIT_MAX_TRACKED = 10000

_status_attempts: dict[str, list[float]] = {}

# Fallback roster used only if `settings` does not (yet) expose a
# MeshCore team list, or exposes it under a name this module doesn't
# know about yet -- keeps these routes usable even if the in-flight
# config change lands after this file does. Matches the seven
# MeshCore team colors (see frontend/mc.js TEAM_COLORS).
_FALLBACK_TEAMS = ["RED", "GREEN", "BLUE", "PURPLE", "YELLOW", "ORANGE", "PINK"]


def _team_list() -> list[str]:
    """Read the MeshCore team roster from settings.

    Tries a few plausible attribute names/shapes so this keeps working
    however the in-flight config change ends up naming the field.
    Falls back to the fixed seven-team roster if none of them exist.
    """
    for attr in ("mc_teams", "mc_team_list", "teams"):
        val = getattr(settings, attr, None)
        if val is None:
            continue
        if isinstance(val, str):
            parsed = [t.strip().upper() for t in val.split(",") if t.strip()]
            if parsed:
                return parsed
        elif isinstance(val, (list, tuple)):
            parsed = [str(t).strip().upper() for t in val if str(t).strip()]
            if parsed:
                return parsed
    return list(_FALLBACK_TEAMS)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _status_rate_limited(ip: str) -> bool:
    """True if `ip` has used up its /api/mc/status budget for the
    current window. Records this attempt (by timestamp) when allowed.
    """
    now = time.monotonic()
    window = settings.mc_status_rate_limit_window_seconds
    limit = settings.mc_status_rate_limit_attempts

    if len(_status_attempts) >= _STATUS_RATE_LIMIT_MAX_TRACKED:
        stale = [
            k for k, times in _status_attempts.items()
            if not times or now - times[-1] >= window
        ]
        for k in stale:
            del _status_attempts[k]
        if len(_status_attempts) >= _STATUS_RATE_LIMIT_MAX_TRACKED:
            _status_attempts.clear()

    times = [t for t in _status_attempts.get(ip, []) if now - t < window]
    if len(times) >= limit:
        _status_attempts[ip] = times
        return True
    times.append(now)
    _status_attempts[ip] = times
    return False


def _relative_time(now_ts: int, then_ts: int) -> str:
    """Human phrase for the diagnosis sentence, e.g. "3 minutes ago"."""
    delta = max(0, now_ts - then_ts)
    if delta < 60:
        return "just now"
    minutes = delta // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def _diagnose(alltime: dict, squares_held: int, last_batch_at: int | None, now_ts: int) -> tuple[str, str]:
    """Work out, server-side, why (if at all) a player isn't scoring.

    Applied in order, first match wins -- see the counters this reads
    in player_ingest_stat, written by app/mc_ingest.py's
    _process_one_ping(). `alltime` is the player's all-time totals
    (not just today), because "never received anything", "only ever
    no-contact", etc. describe a standing setup problem, not a bad day.
    """
    total_pings = (
        alltime["pings_bad_coord"] + alltime["pings_out_of_area"]
        + alltime["pings_no_contact"] + alltime["pings_wrong_owner"]
        + alltime["pings_duplicate"] + alltime["pings_accepted"]
    )

    if alltime["batches"] == 0:
        return "never_received", (
            "MeshWars has never received anything from your app. Check that "
            "Custom API Endpoint is switched on in MeshMapper, that the URL "
            "is right, and that a wardriving session is actually running."
        )

    if total_pings > 0 and alltime["pings_no_contact"] == total_pings:
        return "no_contact_key", (
            "Your batches are arriving but none of the pings can be "
            "attributed to you. Turn on Include Contact Key in MeshMapper."
        )

    if total_pings > 0 and alltime["pings_out_of_area"] == total_pings:
        return "out_of_area", (
            "Your positions are outside the play area, which is Southern "
            "Idaho and Northern Utah."
        )

    if alltime["pings_wrong_owner"] > 0:
        return "wrong_owner", (
            "A radio reporting under your key is registered to someone "
            "else. Your key may have been shared or copied."
        )

    if (
        alltime["pings_accepted"] > 0
        and alltime["pings_no_repeaters"] == alltime["pings_accepted"]
        and squares_held == 0
    ):
        return "no_repeaters", (
            "Everything is working. Your positions are arriving, but none "
            "of them heard a repeater, so no squares were claimed. That "
            "means you are out of range of the mesh, not misconfigured."
        )

    when = _relative_time(now_ts, last_batch_at) if last_batch_at else "never"
    square_word = "square" if squares_held == 1 else "squares"
    return "ok", f"Working. Last heard {when}. You hold {squares_held} {square_word}."


def _safe_query(fn):
    """Run fn(conn) with a fresh read connection.

    Returns fn's result, or None if fn raises sqlite3.OperationalError
    for a missing table (the mc_* schema not landed yet). Any other
    error is a real bug and is re-raised.
    """
    conn = connect()
    try:
        return fn(conn)
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return None
        raise
    finally:
        conn.close()


def _active_mc_season(conn) -> sqlite3.Row | None:
    """The active MeshCore ('mc') season. Every mc_* API route reads
    through this one helper rather than querying mc_season directly, so
    the protocol filter only has to be written in one place -- once the
    Meshtastic board moves onto mc_season too, an 'mt' season being
    active would otherwise look, to an unfiltered query, exactly like an
    'mc' season being active.
    """
    return conn.execute(
        "SELECT id, started_at, ends_at, status, winner FROM mc_season "
        " WHERE protocol = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
        (MC_PROTOCOL,),
    ).fetchone()


@router.get("/api/mc/board")
async def mc_board() -> list[dict]:
    """Every owned cell in the active MeshCore season, with bounds
    computed server-side so the browser never has to reimplement the
    grid maths in app.grid.
    """

    def run(conn):
        season = _active_mc_season(conn)
        if not season:
            return []
        rows = conn.execute(
            "SELECT cell_id, owner_team, last_report_ts, paint_count "
            "  FROM mc_tile WHERE season_id = ? AND owner_team IS NOT NULL",
            (season["id"],),
        ).fetchall()
        out = []
        for r in rows:
            south, west, north, east = cell_bounds(r["cell_id"])
            out.append({
                "cell_id": r["cell_id"],
                "owner_team": r["owner_team"],
                "last_report_ts": r["last_report_ts"],
                "paint_count": r["paint_count"],
                "south": south,
                "west": west,
                "north": north,
                "east": east,
            })
        return out

    result = _safe_query(run)
    return result if result is not None else []


@router.get("/api/mc/scores")
async def mc_scores() -> dict:
    """Active season id/window plus every team's current tile count.

    Always returns all seven teams (zero-filled) so the scoreboard
    doesn't jump around as teams appear on the board.
    """

    def run(conn):
        season = _active_mc_season(conn)
        if not season:
            return None
        # Live counts, not the season-close tally: mc_season_team_tally is
        # only populated by maybe_roll_season() when a season CLOSES, so it
        # stays empty/stale for the entire span of an active season. Count
        # current ownership straight from mc_tile via the same helper
        # season rollover itself uses, so this always matches the live
        # board. mc_season_team_tally is still the correct source for
        # historical (closed) season standings -- leave it alone, don't
        # wire it back in here.
        counts = team_tile_counts(conn, season["id"])
        return {
            "season_id": season["id"],
            "started_at": season["started_at"],
            "ends_at": season["ends_at"],
            "teams": [{"team": t, "tiles": counts.get(t, 0)} for t in _team_list()],
        }

    result = _safe_query(run)
    if result is not None:
        return result
    return {
        "season_id": None,
        "started_at": None,
        "ends_at": None,
        "teams": [{"team": t, "tiles": 0} for t in _team_list()],
    }


@router.get("/api/mc/players")
async def mc_players() -> list[dict]:
    """display_name + team for every non-disabled player.

    Deliberately excludes player_id, key hashes, or anything else --
    display_name/team are the only fields the frontend roster needs.
    """

    def run(conn):
        rows = conn.execute(
            "SELECT display_name, team FROM player WHERE disabled_at IS NULL"
        ).fetchall()
        return [{"display_name": r["display_name"], "team": r["team"]} for r in rows]

    result = _safe_query(run)
    return result if result is not None else []


@router.get("/api/mc/history")
async def mc_history() -> list[dict]:
    """Closed MeshCore seasons, newest first, each with its final
    per-team tile tally.

    mc_season_team_tally, not mc_tile, is the correct source here: it
    is written once at season close (see maybe_roll_season() in
    mc_scoring.py) and is the only place a closed season's standings
    still live, since mc_tile itself moves on to the next season.
    """

    def run(conn):
        # protocol = 'mc' for the same reason as _active_mc_season() above:
        # once mt_season rows exist too, an unfiltered query here would
        # start mixing Meshtastic seasons into the MeshCore history feed.
        seasons = conn.execute(
            "SELECT id, started_at, ends_at, winner FROM mc_season "
            " WHERE protocol = ? AND status = 'closed' ORDER BY id DESC",
            (MC_PROTOCOL,),
        ).fetchall()
        out = []
        for s in seasons:
            tally_rows = conn.execute(
                "SELECT team, tiles FROM mc_season_team_tally WHERE season_id = ?",
                (s["id"],),
            ).fetchall()
            tallies = {r["team"]: r["tiles"] for r in tally_rows}
            out.append({
                "id": s["id"],
                "started_at": s["started_at"],
                "ends_at": s["ends_at"],
                "winner": s["winner"],
                "teams": [{"team": t, "tiles": tallies.get(t, 0)} for t in _team_list()],
            })
        return out

    result = _safe_query(run)
    return result if result is not None else []


@router.get("/api/mc/find")
async def mc_find(name: str):
    """Case-insensitive exact match on a player's display name.

    Returns their team and how many cells they currently hold as last
    painter in the active season, plus the bounding box of those cells
    so the map can zoom to them. 404s if no such player exists.

    The player table itself is not part of the mc_* landing-order race
    described at the top of this module (it predates it), so that
    lookup runs directly; only the season/mc_tile half -- which does
    the actual "holds cells" answer -- is guarded against a missing
    table, degrading to zero cells rather than a 500. A player who
    exists but holds nothing right now (no active season, or simply no
    cells) is a normal 200 with tiles_held=0 and bounds=null, not a 404.
    """
    conn = connect()
    try:
        player = conn.execute(
            "SELECT player_id, display_name, team FROM player "
            " WHERE disabled_at IS NULL AND LOWER(display_name) = LOWER(?)",
            (name,),
        ).fetchone()
        if not player:
            return JSONResponse({"error": "not found"}, status_code=404)

        cell_ids: list[str] = []
        try:
            season = _active_mc_season(conn)
            if season:
                rows = conn.execute(
                    "SELECT cell_id FROM mc_tile WHERE season_id = ? AND last_player_id = ?",
                    (season["id"], player["player_id"]),
                ).fetchall()
                cell_ids = [r["cell_id"] for r in rows]
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower():
                raise
            cell_ids = []

        bounds = None
        if cell_ids:
            souths, wests, norths, easts = zip(*(cell_bounds(c) for c in cell_ids))
            bounds = {
                "south": min(souths),
                "west": min(wests),
                "north": max(norths),
                "east": max(easts),
            }

        return {
            "display_name": player["display_name"],
            "team": player["team"],
            "tiles_held": len(cell_ids),
            "bounds": bounds,
        }
    finally:
        conn.close()


@router.get("/api/mc/top")
async def mc_top() -> list[dict]:
    """Players ranked by capture-event count in the active season,
    from mc_tile_capture_log. Top 20, empty list if there's no data
    (or no active season).
    """

    def run(conn):
        season = _active_mc_season(conn)
        if not season:
            return []
        rows = conn.execute(
            "SELECT p.display_name AS display_name, p.team AS team, "
            "       COUNT(*) AS captures "
            "  FROM mc_tile_capture_log l "
            "  JOIN player p ON p.player_id = l.by_player_id "
            " WHERE l.season_id = ? "
            " GROUP BY l.by_player_id "
            " ORDER BY captures DESC "
            " LIMIT 20",
            (season["id"],),
        ).fetchall()
        return [
            {"display_name": r["display_name"], "team": r["team"], "captures": r["captures"]}
            for r in rows
        ]

    result = _safe_query(run)
    return result if result is not None else []


@router.get("/api/mc/cell/{cell_id}")
async def mc_cell(cell_id: str):
    """Detail for one cell: owner, capture time, per-team current
    scores, and a few recent capture-log entries with display names.

    404s when the cell has no owner -- including when there is no
    active season, or the mc_* schema doesn't exist yet, since in both
    of those cases the cell has no owner either.
    """

    def run(conn):
        season = _active_mc_season(conn)
        if not season:
            return None

        tile = conn.execute(
            "SELECT owner_team, last_report_ts, paint_count "
            "  FROM mc_tile WHERE season_id = ? AND cell_id = ?",
            (season["id"], cell_id),
        ).fetchone()
        if not tile or not tile["owner_team"]:
            return None

        cap = conn.execute(
            "SELECT captured_at, captured_by_team FROM mc_tile_capture "
            " WHERE season_id = ? AND cell_id = ?",
            (season["id"], cell_id),
        ).fetchone()

        score_rows = conn.execute(
            "SELECT team, score FROM mc_tile_score "
            " WHERE season_id = ? AND cell_id = ?",
            (season["id"], cell_id),
        ).fetchall()
        scores = {r["team"]: r["score"] for r in score_rows}

        log_rows = conn.execute(
            "SELECT l.ts, l.by_team, l.from_team, p.display_name "
            "  FROM mc_tile_capture_log l "
            "  LEFT JOIN player p "
            "    ON p.player_id = l.by_player_id "
            " WHERE l.season_id = ? AND l.cell_id = ? "
            " ORDER BY l.ts DESC LIMIT 5",
            (season["id"], cell_id),
        ).fetchall()

        south, west, north, east = cell_bounds(cell_id)
        return {
            "cell_id": cell_id,
            "owner_team": tile["owner_team"],
            "last_report_ts": tile["last_report_ts"],
            "paint_count": tile["paint_count"],
            "captured_at": cap["captured_at"] if cap else None,
            "captured_by_team": cap["captured_by_team"] if cap else None,
            "scores": {t: scores.get(t, 0) for t in _team_list()},
            "south": south,
            "west": west,
            "north": north,
            "east": east,
            "recent_captures": [
                {
                    "ts": r["ts"],
                    "by_team": r["by_team"],
                    "from_team": r["from_team"],
                    "by_display_name": r["display_name"],
                }
                for r in log_rows
            ],
        }

    detail = _safe_query(run)
    if detail is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return detail


_STAT_COLUMNS = (
    "batches, pings_accepted, pings_no_contact, pings_wrong_owner, "
    "pings_duplicate, pings_bad_coord, pings_out_of_area, pings_no_repeaters"
)
_STAT_ZERO_ROW = {
    "batches": 0, "pings_accepted": 0, "pings_no_contact": 0,
    "pings_wrong_owner": 0, "pings_duplicate": 0, "pings_bad_coord": 0,
    "pings_out_of_area": 0, "pings_no_repeaters": 0,
}


def _counters_out(row) -> dict:
    return {
        "batches": row["batches"],
        "accepted": row["pings_accepted"],
        "no_contact": row["pings_no_contact"],
        "wrong_owner": row["pings_wrong_owner"],
        "duplicate": row["pings_duplicate"],
        "bad_coord": row["pings_bad_coord"],
        "out_of_area": row["pings_out_of_area"],
        "no_repeaters": row["pings_no_repeaters"],
    }


@router.post("/api/mc/status")
async def mc_status(request: Request) -> JSONResponse:
    """Lets a player check whether their wardriving app is actually
    reaching us. Every failure mode is already recorded in
    player_ingest_stat and api_key.last_seen_at -- this reads it back
    and works out server-side which one (if any) explains what the
    player is seeing, so the page only has to display it.

    The key arrives in the X-API-Key header on a POST, the same as
    /api/mc/ingest, and for the same reason: a GET would put the key in
    the URL, where it can land in a server access log, browser history,
    or a Referer header. Checking status is deliberately not usage --
    this never touches api_key.last_seen_at.
    """
    ip = _client_ip(request)
    if _status_rate_limited(ip):
        return JSONResponse({"error": "too many attempts, try again later"}, status_code=429)

    raw_key = request.headers.get("X-API-Key", "")
    if not raw_key:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    ingestor = request.app.state.mc_ingestor
    auth = await ingestor.authenticate(raw_key)
    if auth.status in ("not_found", "revoked"):
        # Generic message for both -- don't reveal whether a key exists.
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if auth.status == "disabled":
        return JSONResponse({"error": "forbidden"}, status_code=403)

    player_id = auth.player_id
    now_ts = int(time.time())

    conn = connect()
    try:
        player = conn.execute(
            "SELECT display_name, team FROM player WHERE player_id = ?",
            (player_id,),
        ).fetchone()

        radio_rows = conn.execute(
            "SELECT protocol, node_ref FROM player_node WHERE player_id = ? ORDER BY bound_at",
            (player_id,),
        ).fetchall()

        last_batch_at = conn.execute(
            "SELECT MAX(last_seen_at) AS ts FROM api_key WHERE player_id = ?",
            (player_id,),
        ).fetchone()["ts"]

        now_dt = datetime.now(timezone.utc)
        today = int(now_dt.strftime("%Y%m%d"))
        week_start = int((now_dt.date() - timedelta(days=6)).strftime("%Y%m%d"))

        today_row = conn.execute(
            f"SELECT {_STAT_COLUMNS} FROM player_ingest_stat "
            " WHERE player_id = ? AND protocol = ? AND day = ?",
            (player_id, MC_PROTOCOL, today),
        ).fetchone()

        week_row = conn.execute(
            "SELECT COALESCE(SUM(batches),0) AS batches, "
            "       COALESCE(SUM(pings_accepted),0) AS pings_accepted, "
            "       COALESCE(SUM(pings_no_contact),0) AS pings_no_contact, "
            "       COALESCE(SUM(pings_wrong_owner),0) AS pings_wrong_owner, "
            "       COALESCE(SUM(pings_duplicate),0) AS pings_duplicate, "
            "       COALESCE(SUM(pings_bad_coord),0) AS pings_bad_coord, "
            "       COALESCE(SUM(pings_out_of_area),0) AS pings_out_of_area, "
            "       COALESCE(SUM(pings_no_repeaters),0) AS pings_no_repeaters "
            "  FROM player_ingest_stat WHERE player_id = ? AND protocol = ? "
            "    AND day BETWEEN ? AND ?",
            (player_id, MC_PROTOCOL, week_start, today),
        ).fetchone()

        alltime_row = conn.execute(
            "SELECT COALESCE(SUM(batches),0) AS batches, "
            "       COALESCE(SUM(pings_accepted),0) AS pings_accepted, "
            "       COALESCE(SUM(pings_no_contact),0) AS pings_no_contact, "
            "       COALESCE(SUM(pings_wrong_owner),0) AS pings_wrong_owner, "
            "       COALESCE(SUM(pings_duplicate),0) AS pings_duplicate, "
            "       COALESCE(SUM(pings_bad_coord),0) AS pings_bad_coord, "
            "       COALESCE(SUM(pings_out_of_area),0) AS pings_out_of_area, "
            "       COALESCE(SUM(pings_no_repeaters),0) AS pings_no_repeaters "
            "  FROM player_ingest_stat WHERE player_id = ? AND protocol = ?",
            (player_id, MC_PROTOCOL),
        ).fetchone()

        # squares held: same landing-order guard as mc_find() above -- the
        # mc_tile schema is not part of the always-present core tables.
        squares_held = 0
        try:
            season = _active_mc_season(conn)
            if season:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM mc_tile WHERE season_id = ? AND last_player_id = ?",
                    (season["id"], player_id),
                ).fetchone()
                squares_held = row["n"]
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower():
                raise
            squares_held = 0
    finally:
        conn.close()

    today_out = _counters_out(today_row) if today_row else _counters_out(_STAT_ZERO_ROW)

    alltime = {
        "batches": alltime_row["batches"],
        "pings_accepted": alltime_row["pings_accepted"],
        "pings_no_contact": alltime_row["pings_no_contact"],
        "pings_wrong_owner": alltime_row["pings_wrong_owner"],
        "pings_duplicate": alltime_row["pings_duplicate"],
        "pings_bad_coord": alltime_row["pings_bad_coord"],
        "pings_out_of_area": alltime_row["pings_out_of_area"],
        "pings_no_repeaters": alltime_row["pings_no_repeaters"],
    }
    code, message = _diagnose(alltime, squares_held, last_batch_at, now_ts)

    return {
        "display_name": player["display_name"],
        "team": player["team"],
        "radios": [
            {"protocol": r["protocol"], "node_ref": r["node_ref"]} for r in radio_rows
        ],
        "last_batch_at": last_batch_at,
        "today": today_out,
        "last_7_days": _counters_out(week_row),
        "squares_held": squares_held,
        "diagnosis": {"code": code, "message": message},
    }
