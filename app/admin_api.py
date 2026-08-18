"""FastAPI router for the minimal admin door: revoke keys, disable
players.

There is no other authentication anywhere in this application -- this
module and its token are the whole of it, so every route here is
deliberately small and disabled outright unless `settings.admin_token`
is configured (empty means off, never open, same reasoning as
`join_invite_code` in app/join_api.py).

`GET /admin` serves the page shell itself unauthenticated (beyond the
enabled/disabled check) -- it is just a login box with no player data
in it, the same way any other login page needs to be reachable before
you're logged in. Every route that actually reads or changes data
(`/api/admin/*`) requires the token in the `X-Admin-Token` header,
compared with `secrets.compare_digest` so a wrong guess can't be timed.
"""
from __future__ import annotations

import logging
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import settings
from .db import connect

log = logging.getLogger("admin_api")

router = APIRouter()

_TOKEN_HEADER = "X-Admin-Token"

# A key-hash prefix shorter than this is too likely to match more than
# one key by chance once there are enough players -- refuse it outright
# rather than resolving an ambiguous match by guessing.
_MIN_PREFIX_LEN = 4


def _api_guard(request: Request) -> JSONResponse | None:
    """Returns a response to short-circuit an /api/admin/* route with,
    or None to let the route continue. 404 when the admin door is off
    entirely (indistinguishable from the route not existing); 401 when
    it's on but the caller's token is missing or wrong.
    """
    if not settings.admin_token:
        return JSONResponse({"error": "not found"}, status_code=404)
    supplied = request.headers.get(_TOKEN_HEADER, "")
    if not supplied or not secrets.compare_digest(supplied, settings.admin_token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


# ---- page ---------------------------------------------------------------


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_page() -> HTMLResponse:
    if not settings.admin_token:
        return HTMLResponse("<h1>meshwars</h1><p>not found</p>", status_code=404)
    path = Path(__file__).resolve().parent.parent / "frontend" / "admin.html"
    if not path.exists():
        return HTMLResponse("<h1>meshwars admin</h1><p>admin page not bundled</p>", status_code=404)
    return HTMLResponse(path.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


# ---- data ---------------------------------------------------------------


@router.get("/api/admin/players")
async def admin_players(request: Request):
    guard = _api_guard(request)
    if guard is not None:
        return guard

    conn = connect()
    try:
        players = conn.execute(
            "SELECT player_id, display_name, team, created_at, disabled_at "
            "  FROM player ORDER BY player_id"
        ).fetchall()
        out = []
        for p in players:
            radios = conn.execute(
                "SELECT protocol, node_ref, bound_at FROM player_node "
                " WHERE player_id = ? ORDER BY bound_at",
                (p["player_id"],),
            ).fetchall()
            keys = conn.execute(
                "SELECT key_hash, issued_at, last_seen_at, revoked_at FROM api_key "
                " WHERE player_id = ? ORDER BY issued_at",
                (p["player_id"],),
            ).fetchall()
            out.append({
                "player_id": p["player_id"],
                "display_name": p["display_name"],
                "team": p["team"],
                "created_at": p["created_at"],
                "disabled": p["disabled_at"] is not None,
                "disabled_at": p["disabled_at"],
                "radios": [
                    {"protocol": r["protocol"], "node_ref": r["node_ref"], "bound_at": r["bound_at"]}
                    for r in radios
                ],
                # Never the key hash itself, let alone the raw key -- only
                # the first 8 hex characters, enough to identify a key in
                # the revoke UI without being useful for anything else.
                "keys": [
                    {
                        "key_hash_prefix": k["key_hash"][:8],
                        "issued_at": k["issued_at"],
                        "last_seen_at": k["last_seen_at"],
                        "revoked": k["revoked_at"] is not None,
                        "revoked_at": k["revoked_at"],
                    }
                    for k in keys
                ],
            })
        return out
    finally:
        conn.close()


@router.post("/api/admin/revoke")
async def admin_revoke(request: Request):
    guard = _api_guard(request)
    if guard is not None:
        return guard

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    prefix = body.get("key_hash_prefix") if isinstance(body, dict) else None
    if not isinstance(prefix, str) or len(prefix) < _MIN_PREFIX_LEN:
        return JSONResponse(
            {"error": f"key_hash_prefix must be at least {_MIN_PREFIX_LEN} characters"},
            status_code=400,
        )

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # The api_key table is small (one row per issued key for a
        # small-mesh game), so matching the prefix in Python rather than
        # a SQL LIKE avoids any need to escape user-controlled `%`/`_`
        # wildcard characters.
        rows = conn.execute("SELECT key_hash, player_id, revoked_at FROM api_key").fetchall()
        matches = [r for r in rows if r["key_hash"].startswith(prefix)]

        if not matches:
            conn.execute("ROLLBACK")
            return JSONResponse({"error": "no matching key"}, status_code=404)
        if len(matches) > 1:
            conn.execute("ROLLBACK")
            return JSONResponse(
                {"error": "ambiguous prefix, matches multiple keys"}, status_code=409
            )

        match = matches[0]
        already_revoked = match["revoked_at"] is not None
        now = int(time.time())
        # Revoking sets a timestamp; it never deletes the row, so the
        # record of every key a player has ever held survives.
        conn.execute(
            "UPDATE api_key SET revoked_at = ? WHERE key_hash = ?", (now, match["key_hash"])
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    # The auth cache means a revoked key could otherwise keep working
    # until its cached entry expires -- drop it now so this takes effect
    # on the very next ingest attempt.
    ingestor = request.app.state.mc_ingestor
    ingestor.invalidate_key(match["key_hash"])

    log.info("admin: revoked key %s... (player %d)", match["key_hash"][:8], match["player_id"])
    return {
        "revoked": True,
        "key_hash_prefix": match["key_hash"][:8],
        "player_id": match["player_id"],
        "revoked_at": now,
        "already_revoked": already_revoked,
    }


async def _set_player_disabled(request: Request, disable: bool):
    guard = _api_guard(request)
    if guard is not None:
        return guard

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    player_id = body.get("player_id") if isinstance(body, dict) else None
    if not isinstance(player_id, int) or isinstance(player_id, bool):
        return JSONResponse({"error": "player_id is required"}, status_code=400)

    now = int(time.time()) if disable else None
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT player_id FROM player WHERE player_id = ?", (player_id,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return JSONResponse({"error": "player not found"}, status_code=404)
        conn.execute(
            "UPDATE player SET disabled_at = ? WHERE player_id = ?", (now, player_id)
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    # Same cache-staleness problem as revoke: drop every cached auth
    # entry for this player so a disable/enable takes effect right away
    # rather than waiting out the cache TTL.
    ingestor = request.app.state.mc_ingestor
    ingestor.invalidate_player(player_id)

    log.info("admin: player %d %s", player_id, "disabled" if disable else "enabled")
    return {"player_id": player_id, "disabled": disable, "disabled_at": now}


@router.post("/api/admin/player/disable")
async def admin_player_disable(request: Request):
    return await _set_player_disabled(request, disable=True)


@router.post("/api/admin/player/enable")
async def admin_player_enable(request: Request):
    return await _set_player_disabled(request, disable=False)


@router.post("/api/admin/player/delete")
async def admin_player_delete(request: Request):
    """Permanently remove a player and everything that refers to them.

    Unlike disable, which only flips a flag and can be reversed, this
    deletes the player row, every key and radio binding they hold, their
    MeshCore ping/stat history, their unique-painter credit, and every
    square where they are the last painter -- along with that square's
    score, capture-window, and capture-log rows, so nothing is left
    pointing at a square that no longer exists.

    The caller must supply the player's current display_name exactly;
    a mismatch (or a player_id that doesn't exist) refuses with 409/404
    rather than deleting on a stale or mistyped name. Everything below
    runs in one transaction so a failure partway through cannot leave a
    partial delete behind.
    """
    guard = _api_guard(request)
    if guard is not None:
        return guard

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    player_id = body.get("player_id") if isinstance(body, dict) else None
    display_name = body.get("display_name") if isinstance(body, dict) else None
    if not isinstance(player_id, int) or isinstance(player_id, bool):
        return JSONResponse({"error": "player_id is required"}, status_code=400)
    if not isinstance(display_name, str) or not display_name:
        return JSONResponse({"error": "display_name is required"}, status_code=400)

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT display_name FROM player WHERE player_id = ?", (player_id,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return JSONResponse({"error": "player not found"}, status_code=404)
        if row["display_name"] != display_name:
            conn.execute("ROLLBACK")
            return JSONResponse(
                {"error": "display name does not match"}, status_code=409
            )

        counts = {
            "mc_tile": 0,
            "mc_tile_score": 0,
            "mc_tile_capture": 0,
            "mc_tile_capture_log": 0,
        }

        # Squares where this player is the last painter. Each one, plus
        # its score/capture/capture-log rows, is removed entirely rather
        # than left behind pointing at nobody.
        squares = conn.execute(
            "SELECT season_id, cell_id FROM mc_tile WHERE last_player_id = ?",
            (player_id,),
        ).fetchall()
        for sq in squares:
            season_id, cell_id = sq["season_id"], sq["cell_id"]
            c = conn.execute(
                "DELETE FROM mc_tile_score WHERE season_id = ? AND cell_id = ?",
                (season_id, cell_id),
            )
            counts["mc_tile_score"] += c.rowcount
            c = conn.execute(
                "DELETE FROM mc_tile_capture WHERE season_id = ? AND cell_id = ?",
                (season_id, cell_id),
            )
            counts["mc_tile_capture"] += c.rowcount
            c = conn.execute(
                "DELETE FROM mc_tile_capture_log WHERE season_id = ? AND cell_id = ?",
                (season_id, cell_id),
            )
            counts["mc_tile_capture_log"] += c.rowcount
            c = conn.execute(
                "DELETE FROM mc_tile WHERE season_id = ? AND cell_id = ?",
                (season_id, cell_id),
            )
            counts["mc_tile"] += c.rowcount

        c = conn.execute(
            "DELETE FROM mc_tile_unique_painter WHERE player_id = ?", (player_id,)
        )
        counts["mc_tile_unique_painter"] = c.rowcount

        c = conn.execute(
            "DELETE FROM player_ingest_stat WHERE player_id = ?", (player_id,)
        )
        counts["player_ingest_stat"] = c.rowcount

        c = conn.execute(
            "DELETE FROM player_cell_ping WHERE player_id = ?", (player_id,)
        )
        counts["player_cell_ping"] = c.rowcount

        c = conn.execute(
            "DELETE FROM player_node WHERE player_id = ?", (player_id,)
        )
        counts["player_node"] = c.rowcount

        c = conn.execute("DELETE FROM api_key WHERE player_id = ?", (player_id,))
        counts["api_key"] = c.rowcount

        c = conn.execute("DELETE FROM player WHERE player_id = ?", (player_id,))
        counts["player"] = c.rowcount

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    # Same reasoning as revoke/disable: a deleted player's keys must stop
    # authenticating immediately, not once the auth cache TTL expires.
    ingestor = request.app.state.mc_ingestor
    ingestor.invalidate_player(player_id)

    log.info("admin: deleted player %d (%s): %s", player_id, display_name, counts)
    return {
        "deleted": True,
        "player_id": player_id,
        "display_name": display_name,
        "counts": counts,
    }
