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
from .mc_ingest import hash_secret
from .node_ref import normalize_node_ref, normalize_public_key

log = logging.getLogger("admin_api")

router = APIRouter()

_TOKEN_HEADER = "X-Admin-Token"

# A key-hash prefix shorter than this is too likely to match more than
# one key by chance once there are enough players -- refuse it outright
# rather than resolving an ambiguous match by guessing.
_MIN_PREFIX_LEN = 4

# Same two protocol values app/nodes_api.py and app/join_api.py accept --
# duplicated here rather than imported, since nodes_api's is a private
# (leading-underscore) module constant, not meant for cross-module reuse.
_VALID_PROTOCOLS = ("mt", "mc")


def _validate_team(raw: object) -> tuple[str | None, str | None]:
    """Same rule app/join_api.py applies at registration (strip,
    uppercase, must be in settings.teams_list) and reuses for its own
    player-facing switch_team() -- duplicated here rather than
    imported, same reasoning as _VALID_PROTOCOLS above: that one is a
    private helper in a different module, not meant for cross-module
    reuse. Returns (team, error); team is None if invalid.
    """
    team = raw.strip().upper() if isinstance(raw, str) else ""
    if team not in settings.teams_list:
        return None, "invalid team"
    return team, None


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


def _player_radios(conn, player_id: int) -> list[dict]:
    """Same shape app/nodes_api.py's _radios_out() returns. Duplicated
    rather than imported for the same reason _VALID_PROTOCOLS above is:
    that one is a private helper in a different module, not meant to be
    shared across files.
    """
    rows = conn.execute(
        "SELECT protocol, node_ref, bound_at FROM player_node "
        " WHERE player_id = ? ORDER BY bound_at",
        (player_id,),
    ).fetchall()
    return [
        {"protocol": r["protocol"], "node_ref": r["node_ref"], "bound_at": r["bound_at"]}
        for r in rows
    ]


def _resolve_public_key(conn, protocol: str, node_ref: str, raw_public_key: object) -> tuple[str | None, JSONResponse | None]:
    """Same rule as app/nodes_api.py's _resolve_public_key(). Duplicated
    rather than imported for the same reason _player_radios above is:
    that one is a private helper in a different module. Supplied and
    invalid -> 400. Supplied and valid -> use it. Not supplied -> for a
    Meshtastic node, auto-fill from mt_node_key only when exactly one
    distinct key is on record for it; zero means never heard yet, more
    than one is the drift/collision case that table exists to catch, and
    guessing which key is current would be inventing an answer -- both
    store NULL. MeshCore's node_ref is already a key prefix, so it is
    left alone entirely.
    """
    if raw_public_key is not None:
        normalized = normalize_public_key(raw_public_key)
        if normalized is None:
            return None, JSONResponse(
                {"error": "public_key must be 64 hex characters"},
                status_code=400,
            )
        return normalized, None

    if protocol != "mt":
        return None, None

    rows = conn.execute(
        "SELECT DISTINCT public_key FROM mt_node_key WHERE node_ref = ?",
        (node_ref,),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["public_key"], None
    return None, None


@router.post("/api/admin/node/add")
async def admin_node_add(request: Request):
    """Bind a radio to a player with no key involved at all -- the
    reason this branch exists. A MeshCore player's key already lives in
    their MeshMapper config; a Meshtastic player has no such fallback.
    Either way, the owner should be able to fix "this radio isn't
    registered" without touching keys or asking the player for
    anything. This does exactly what the player-facing POST /api/nodes
    does (app/nodes_api.py's add_node()) -- same normalize_node_ref(),
    same check-then-act conflict check -- just authenticated by the
    admin token instead of a player's key.

    NOT destructive, unlike remove right below: it only ever creates a
    binding nobody held before, or confirms one this same player
    already has. A wrong player_id here binds a real radio to the
    wrong (but real) player -- visible immediately in the admin list
    and reversible with the remove route below -- it never takes
    anything away from anyone. So this route gets only _api_guard(),
    not the player_id + display_name confirmation guard remove/delete/
    reissue require.
    """
    guard = _api_guard(request)
    if guard is not None:
        return guard

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad request"}, status_code=400)

    player_id = body.get("player_id")
    if not isinstance(player_id, int) or isinstance(player_id, bool):
        return JSONResponse({"error": "player_id is required"}, status_code=400)

    protocol = body.get("protocol")
    if protocol not in _VALID_PROTOCOLS:
        return JSONResponse(
            {"error": "protocol must be one of: " + ", ".join(_VALID_PROTOCOLS)},
            status_code=400,
        )

    # normalize_node_ref() is the one place both protocols' writers and
    # readers funnel through (see app/node_ref.py's module docstring) --
    # this branch exists partly because a second, hand-rolled
    # normalization once wrote a different format here and silently
    # broke binding. Do not reimplement it.
    node_ref = normalize_node_ref(body.get("node_ref"))
    if node_ref is None:
        return JSONResponse(
            {"error": "node_ref is required and must be 8 hex characters, "
                      "with or without a leading !"},
            status_code=400,
        )

    now = int(time.time())
    conn = connect()
    try:
        public_key, err = _resolve_public_key(conn, protocol, node_ref, body.get("public_key"))
        if err is not None:
            return err

        conn.execute("BEGIN IMMEDIATE")
        player = conn.execute(
            "SELECT player_id FROM player WHERE player_id = ?", (player_id,)
        ).fetchone()
        if player is None:
            conn.execute("ROLLBACK")
            return JSONResponse({"error": "player not found"}, status_code=404)

        # Check-then-act, same shape as app/nodes_api.py's add_node():
        # the (protocol, node_ref) primary key on player_node would
        # raise an IntegrityError on a cross-player conflict too, but
        # looking first means a clear 409 instead of a raw sqlite3
        # exception surfaced as a 500.
        existing = conn.execute(
            "SELECT player_id FROM player_node WHERE protocol = ? AND node_ref = ?",
            (protocol, node_ref),
        ).fetchone()
        if existing is not None:
            conn.execute("ROLLBACK")
            if existing["player_id"] == player_id:
                # Already bound to this same player -- not an error,
                # same reasoning as add_node(): a retried request should
                # just succeed.
                radios = _player_radios(conn, player_id)
                return JSONResponse({"radios": radios, "added": False}, status_code=200)
            return JSONResponse(
                {"error": "that node is already registered to another player"},
                status_code=409,
            )

        conn.execute(
            "INSERT INTO player_node(protocol, node_ref, player_id, bound_at, public_key) "
            "VALUES (?, ?, ?, ?, ?)",
            (protocol, node_ref, player_id, now, public_key),
        )
        conn.execute("COMMIT")
        radios = _player_radios(conn, player_id)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    # No ingestor.invalidate_*() call: those exist only to flush a
    # cached API-key lookup, and this route never touches api_key at
    # all -- player_node lookups (see app/mc_ingest.py's ingest path
    # and app/ingest.py's registered-node map) are never cached, so
    # there is nothing stale for this to fix.
    log.info("admin: bound %s:%s to player %d", protocol, node_ref, player_id)
    return JSONResponse({"radios": radios, "added": True}, status_code=201)


@router.post("/api/admin/node/remove")
async def admin_node_remove(request: Request):
    """Unbind a radio from a player.

    Destructive -- it silently takes away MeshWars' ability to
    recognize this specific radio as this player's, exactly the kind
    of consequence delete/reissue already guard against. Same
    player_id + matching display_name confirmation guard as those two,
    for the same reason: a stale or mistyped player_id here would take
    a radio away from someone who never asked for that.
    """
    guard = _api_guard(request)
    if guard is not None:
        return guard

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad request"}, status_code=400)

    player_id = body.get("player_id")
    display_name = body.get("display_name")
    if not isinstance(player_id, int) or isinstance(player_id, bool):
        return JSONResponse({"error": "player_id is required"}, status_code=400)
    if not isinstance(display_name, str) or not display_name:
        return JSONResponse({"error": "display_name is required"}, status_code=400)

    protocol = body.get("protocol")
    if protocol not in _VALID_PROTOCOLS:
        return JSONResponse(
            {"error": "protocol must be one of: " + ", ".join(_VALID_PROTOCOLS)},
            status_code=400,
        )

    node_ref = normalize_node_ref(body.get("node_ref"))
    if node_ref is None:
        return JSONResponse(
            {"error": "node_ref is required and must be 8 hex characters, "
                      "with or without a leading !"},
            status_code=400,
        )

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

        # Scoped to player_id in the WHERE clause itself, same as the
        # player-facing DELETE /api/nodes/{node_ref} -- a node_ref that
        # exists but belongs to someone else and one that doesn't exist
        # at all both delete zero rows here.
        cur = conn.execute(
            "DELETE FROM player_node WHERE protocol = ? AND node_ref = ? AND player_id = ?",
            (protocol, node_ref, player_id),
        )
        removed = cur.rowcount > 0
        conn.execute("COMMIT")
        radios = _player_radios(conn, player_id)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    # Same reasoning as add above: nothing about player_node is ever
    # cached, so there is no ingestor.invalidate_*() call to make here.
    log.info(
        "admin: unbound %s:%s from player %d (%s), removed=%s",
        protocol, node_ref, player_id, display_name, removed,
    )
    return JSONResponse({"radios": radios, "removed": removed}, status_code=200)


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


@router.post("/api/admin/player/issue_key")
async def admin_player_issue_key(request: Request):
    """Mint an ADDITIONAL key for a player. Does not touch any key they
    already hold -- api_key has never enforced one-key-per-player (see
    /api/admin/player/reissue's docstring just below, "nothing here has
    ever prevented that"), so this simply exercises that: insert a new
    row, leave every existing row exactly as it was.

    This is the fix for "I lost my key" -- as distinct from "someone
    else has my key", which is what /api/admin/player/reissue right
    below is for. Use THIS route when the player's own setup (their
    MeshMapper config, in particular) is still fine and must keep
    working untouched; reach for reissue only when the old key has to
    stop working immediately. Reaching for reissue here instead would
    silently break a MeshCore player's MeshMapper the next time it
    sends a batch with the now-revoked key -- exactly the outage this
    branch exists to stop causing. The admin UI labels the two
    differently and keeps this one visually lighter for the same
    reason: a tired operator reaching for the wrong one at the wrong
    moment causes an outage for that player.

    No display_name confirmation guard, unlike delete/reissue: those
    guard against a stale/mistyped player_id taking something away
    from the wrong person. This route can't do that -- the worst case
    for a wrong player_id is a real player getting an extra working
    key they didn't ask for, nobody loses access -- so it gets the
    same light guard disable/enable use (player_id only), not the
    heavier one delete/reissue need.
    """
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

    now = int(time.time())
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT display_name FROM player WHERE player_id = ?", (player_id,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return JSONResponse({"error": "player not found"}, status_code=404)

        raw_key = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO api_key(key_hash, player_id, issued_at) VALUES (?, ?, ?)",
            (hash_secret(raw_key), player_id, now),
        )
        conn.execute("COMMIT")
        display_name = row["display_name"]
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    # Deliberately no ingestor.invalidate_*() call here, unlike revoke/
    # disable/delete/reissue below. Those all exist to force an auth
    # cache entry for a key that just became invalid to stop being
    # honored before its TTL expires -- this route never invalidates
    # anything, so there is no stale cache entry for it to fix. A brand
    # new key was never looked up before (nothing has cached a result
    # for it, positive or negative), so it authenticates correctly the
    # very first time it's used with no help needed here. Do not add an
    # invalidate call to "match" the other routes below -- it would be
    # a no-op dressed up as symmetry, and its absence is intentional.
    log.info("admin: issued additional key for player %d (%s)", player_id, display_name)
    return {
        "issued": True,
        "player_id": player_id,
        "display_name": display_name,
        "key": raw_key,
        "issued_at": now,
    }


@router.post("/api/admin/player/reissue")
async def admin_player_reissue(request: Request):
    """Mint a fresh key for a player and revoke every key they currently
    hold, in one operation.

    api_key stores only key_hash (a SHA-256 digest) -- recovering a lost
    raw key is impossible by design, and this route does not try. What
    it does instead is give the player a working key again: a new one,
    returned here exactly once, in this response body only, the same
    one-time treatment app/join_api.py's join() gives a key at signup.

    The old key(s) are revoked as part of the same operation, not left
    alone for the operator to decide about separately. "I lost my key"
    and "someone else has my key" look identical from here -- there is
    no way to tell which one this is -- so the safe default in both
    cases is that whatever key the player had before stops working the
    moment a new one is issued, exactly like a password reset that
    doesn't leave the old password valid.

    Same confirmation guard as /api/admin/player/delete: the caller
    must supply the player's current display_name exactly. This is
    just as disruptive to the player's current setup as a delete
    would be -- their MeshMapper config, or anything else holding the
    old key, stops working the instant this runs -- so it earns the
    same protection against a stale/mistyped player_id doing this to
    the wrong person.
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

    now = int(time.time())
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

        # Revoke every key this player currently holds that isn't
        # already revoked -- not just the newest one. A player can hold
        # more than one active key (nothing here has ever prevented
        # that), and leaving an older one live would defeat the point:
        # "someone else has my key" doesn't tell us WHICH key they have.
        revoked = conn.execute(
            "UPDATE api_key SET revoked_at = ? WHERE player_id = ? AND revoked_at IS NULL",
            (now, player_id),
        )
        revoked_count = revoked.rowcount

        raw_key = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO api_key(key_hash, player_id, issued_at) VALUES (?, ?, ?)",
            (hash_secret(raw_key), player_id, now),
        )

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    # Same cache-staleness problem revoke/disable/delete already solve --
    # without this, a just-revoked key could keep authenticating at the
    # ingest endpoint until its cached entry expires
    # (settings.mc_key_cache_seconds). invalidate_player drops every
    # cached entry for this player_id in one call, covering all of the
    # keys just revoked above, not only the newest one -- the same
    # reason the admin door's disable/delete routes use invalidate_player
    # instead of invalidate_key here.
    ingestor = request.app.state.mc_ingestor
    ingestor.invalidate_player(player_id)

    log.info(
        "admin: reissued key for player %d (%s), revoked %d prior key(s)",
        player_id, display_name, revoked_count,
    )
    return {
        "reissued": True,
        "player_id": player_id,
        "display_name": display_name,
        "key": raw_key,
        "issued_at": now,
        "revoked_count": revoked_count,
    }


# ---- public API clients ------------------------------------------------
#
# Keys for app/public_api.py. Deliberately not the same table as a
# player's api_key: that one authorises writing wardriving data for one
# person, this one authorises reading the public surface for one
# integration. A shared table would let a read key post pings.


@router.get("/api/admin/api-clients")
async def admin_api_clients(request: Request):
    """Every issued read-API key, newest first. Only the first twelve
    characters of the hash are shown -- enough to tell two rows apart
    and to name one in a revoke, and useless to anyone who sees the
    screen."""
    guard = _api_guard(request)
    if guard is not None:
        return guard

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT key_hash, label, created_at, revoked_at, last_seen_at, request_count "
            "  FROM api_client ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return JSONResponse([{
        "key_hash_prefix": r["key_hash"][:12],
        "label": r["label"],
        "created_at": r["created_at"],
        "revoked_at": r["revoked_at"],
        "last_seen_at": r["last_seen_at"],
        # Authentications rather than requests -- see the column's own
        # comment in app/db.py. Returned for completeness; the admin UI
        # shows last_seen_at instead, which is the honest signal.
        "auth_count": r["request_count"],
        "revoked": r["revoked_at"] is not None,
    } for r in rows])


@router.post("/api/admin/api-clients/create")
async def admin_api_client_create(request: Request):
    """Mint a read-API key for one integration.

    The raw key is returned HERE AND NOWHERE ELSE. Only its hash is
    stored, the same contract a player's key has, so there is no route
    that can show it again and no amount of database access recovers
    it. A lost key is replaced by issuing another and revoking the old
    one.

    The label is what makes a list of hashes usable a year later --
    "freq51 discord bot" rather than a twelve-character prefix nobody
    can place. It is required for that reason.
    """
    guard = _api_guard(request)
    if guard is not None:
        return guard

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    label = (body.get("label") or "").strip() if isinstance(body, dict) else ""
    if not label:
        return JSONResponse({"error": "label is required"}, status_code=400)
    if len(label) > 80:
        return JSONResponse({"error": "label is too long (80 characters max)"}, status_code=400)

    raw = secrets.token_urlsafe(32)
    now = int(time.time())
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO api_client(key_hash, label, created_at) VALUES (?, ?, ?)",
            (hash_secret(raw), label, now),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    log.info("admin: issued read-API key for %r", label)
    return JSONResponse({"label": label, "key": raw, "created_at": now})


@router.post("/api/admin/api-clients/revoke")
async def admin_api_client_revoke(request: Request):
    """Revoke one key by its hash prefix. Takes effect within a minute --
    app/public_api.py caches authentication for that long, which is the
    price of not querying on every read.

    The row is kept rather than deleted so the label, when it was
    issued and how much it was used stay visible afterwards; a revoked
    key that vanishes leaves an operator unable to answer "what was
    that and did I already deal with it".
    """
    guard = _api_guard(request)
    if guard is not None:
        return guard

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    prefix = (body.get("key_hash_prefix") or "").strip() if isinstance(body, dict) else ""
    if not prefix or len(prefix) < 8:
        return JSONResponse({"error": "key_hash_prefix is required"}, status_code=400)

    now = int(time.time())
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE api_client SET revoked_at = ? "
            " WHERE key_hash LIKE ? AND revoked_at IS NULL", (now, prefix + "%"))
        conn.execute("COMMIT")
    finally:
        conn.close()
    if not cur.rowcount:
        return JSONResponse({"error": "no active key with that prefix"}, status_code=404)
    log.info("admin: revoked read-API key %s", prefix)
    return JSONResponse({"revoked": cur.rowcount, "revoked_at": now})


@router.post("/api/admin/player/team")
async def admin_set_team(request: Request):
    """Set any player's team, unlimited -- the operator counterpart to
    app/join_api.py's switch_team(), which caps a player to one
    self-service change per calendar month. No such limit applies here.

    Ground stays with whichever team held it at paint time
    (mc_tile.owner_team is frozen and never re-derived from
    player.team); check-in points, exploration points, and streaks all
    travel to the new team for free, because they're already computed
    live off player.team (app/checkin.py's
    team_checkin_points()/team_place_points()). This route changes
    nothing about scoring -- only player.team itself and the audit
    trail in player_team_change.

    Light guard (player_id only, like /api/admin/node/add above), not
    the typed-name confirmation the destructive routes below require --
    a team change is fully reversible by switching back.
    """
    guard = _api_guard(request)
    if guard is not None:
        return guard

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad request"}, status_code=400)

    player_id = body.get("player_id")
    if not isinstance(player_id, int) or isinstance(player_id, bool):
        return JSONResponse({"error": "player_id is required"}, status_code=400)

    team, terr = _validate_team(body.get("team"))
    if terr:
        return JSONResponse({"error": terr}, status_code=400)

    now = int(time.time())
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        player = conn.execute(
            "SELECT team FROM player WHERE player_id = ?", (player_id,)
        ).fetchone()
        if player is None:
            conn.execute("ROLLBACK")
            return JSONResponse({"error": "player not found"}, status_code=404)

        if team == player["team"]:
            # Already on that team -- not an error, same reasoning as
            # admin_node_add()'s "already bound to this same player"
            # case: a retried request should just succeed.
            conn.execute("ROLLBACK")
            return JSONResponse({"player_id": player_id, "team": team, "changed": False}, status_code=200)

        conn.execute(
            "UPDATE player SET team = ? WHERE player_id = ?",
            (team, player_id),
        )
        conn.execute(
            "INSERT INTO player_team_change"
            "(player_id, from_team, to_team, changed_at, actor) "
            "VALUES (?, ?, ?, ?, 'operator')",
            (player_id, player["team"], team, now),
        )

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    log.info("admin: set player %d team to %s", player_id, team)
    return JSONResponse({"player_id": player_id, "team": team, "changed": True}, status_code=200)
