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

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .config import settings
from .db import connect
from .grid import cell_bounds
from .mc_scoring import team_tile_counts

router = APIRouter()

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
    return conn.execute(
        "SELECT id, started_at, ends_at, status, winner FROM mc_season "
        " WHERE status = 'active' ORDER BY id DESC LIMIT 1"
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
