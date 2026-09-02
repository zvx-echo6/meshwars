"""FastAPI router for public player self-registration.

`/api/join` and `/api/join/redeem` are the only unauthenticated,
state-changing endpoints in this app -- reachable from the public
internet, so this module is defensive about it:

- Registration is entirely OFF unless `settings.join_invite_code` is
  configured. Empty means off, never open (see the check at the top of
  `join()`).
- The invite code is compared with `secrets.compare_digest`, and a wrong
  code still costs a small fixed delay, so the endpoint can't be used as
  a fast oracle to brute-force the code.
- A simple in-process rate limiter (`_attempts`, keyed on client IP)
  caps attempts per address; the tracking dict is bounded the same way
  `McIngestor._key_cache` is in app/mc_ingest.py -- sweep expired
  entries first, and only clear the whole structure if that alone
  doesn't bring it back under the cap.

`/api/join/redeem` exists now so a later mesh join command can turn a
pre-issued token into a key without any change here. Nothing in this
module ever transmits on a radio.
"""
from __future__ import annotations

import asyncio
import secrets
import time
import unicodedata

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .auth import Principal, new_rate_limit_bucket, require_api_key_principal
from .client_ip import get_client_ip
from .config import settings
from .db import connect
from .mc_ingest import hash_secret
from .node_ref import normalize_node_ref
from .results import month_bounds, month_key

router = APIRouter()

# Small fixed delay added before returning "wrong invite code", so a
# flood of guesses can't be timed to distinguish a wrong code from a
# rejected-for-other-reasons request.
_WRONG_CODE_DELAY_S = 0.3

# ---- rate limiting ---------------------------------------------------
#
# Every distinct client IP that hits these public endpoints gets a
# tracking entry, whether it ever succeeds or not. Without a bound this
# grows without limit under a flood -- the same failure mode
# `_KEY_CACHE_MAX` in app/mc_ingest.py guards against -- so this caps it
# the same way: sweep stale entries, and only clear the whole dict if
# that alone doesn't bring it back under the cap.
_RATE_LIMIT_MAX_TRACKED = 10000

_attempts: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    # See app/client_ip.py's module docstring: this used to be
    # request.client.host directly, which is always the Caddy reverse
    # proxy's own address in every deployment, not the real caller's.
    return get_client_ip(request)


def _rate_limited(ip: str) -> bool:
    """True if `ip` has used up its attempt budget for the current
    window. Records this attempt (by timestamp) when allowed.
    """
    now = time.monotonic()
    window = settings.join_rate_limit_window_seconds
    limit = settings.join_rate_limit_attempts

    if len(_attempts) >= _RATE_LIMIT_MAX_TRACKED:
        stale = [
            k for k, times in _attempts.items()
            if not times or now - times[-1] >= window
        ]
        for k in stale:
            del _attempts[k]
        if len(_attempts) >= _RATE_LIMIT_MAX_TRACKED:
            _attempts.clear()

    times = [t for t in _attempts.get(ip, []) if now - t < window]
    if len(times) >= limit:
        _attempts[ip] = times
        return True
    times.append(now)
    _attempts[ip] = times
    return False


# ---- validation helpers -----------------------------------------------
#
# Node-reference normalization (_NODE_REF_RE / normalize_node_ref) lives
# in app/node_ref.py now -- app/nodes_api.py needs the exact same
# definition, and a table whose primary key is a literal string compare
# on node_ref must never have two independent ideas of what "valid"
# means.


def _validate_display_name(raw: object) -> tuple[str | None, str | None]:
    """Returns (name, error). name is None if invalid."""
    if not isinstance(raw, str):
        return None, "display name is required"
    name = raw.strip()
    if not (1 <= len(name) <= 32):
        return None, "display name must be 1-32 characters"
    if any(unicodedata.category(c) == "Cc" for c in name):
        return None, "display name contains invalid characters"
    return name, None


def _validate_team(raw: object) -> tuple[str | None, str | None]:
    """Same rule join()'s own inline team check applies at step 4 below
    (strip, uppercase, must be in settings.teams_list) -- factored out
    so switch_team() below shares one definition of "valid team"
    instead of drifting from it. join()'s own check is left as it is;
    this only saves the new route from carrying a second copy.
    Returns (team, error); team is None if invalid.
    """
    team = raw.strip().upper() if isinstance(raw, str) else ""
    if team not in settings.teams_list:
        return None, "invalid team"
    return team, None


def _config_link(raw_key: str) -> str:
    # No https:// prefix on the url value -- the app adds it.
    return f"meshmapper://custom-api?url={settings.public_host}/api/mc/ingest&key={raw_key}"


def _registration_response(display_name: str, team: str, protocol: str | None, raw_key: str) -> dict:
    resp: dict = {
        "display_name": display_name,
        "team": team,
        "protocol": protocol,
        "key": raw_key,
    }
    if protocol == "mc":
        resp["config_link"] = _config_link(raw_key)
    return resp


# ---- routes -------------------------------------------------------------

@router.post("/api/join")
async def join(request: Request) -> JSONResponse:
    # 1. Registration is disabled unless an invite code is configured.
    # Empty must mean off, never open.
    if not settings.join_invite_code:
        return JSONResponse(
            {"error": "registration is currently closed"}, status_code=503
        )

    ip = _client_ip(request)
    if _rate_limited(ip):
        return JSONResponse(
            {"error": "too many attempts, try again later"}, status_code=429
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad request"}, status_code=400)

    # 2. Invite code, constant-time compare, fixed delay on mismatch so
    # this can't be timed as a fast oracle. asyncio.sleep, not
    # time.sleep -- this must not block the event loop.
    supplied_code = body.get("invite_code")
    if not isinstance(supplied_code, str) or not secrets.compare_digest(
        supplied_code, settings.join_invite_code
    ):
        await asyncio.sleep(_WRONG_CODE_DELAY_S)
        return JSONResponse({"error": "invalid invite code"}, status_code=403)

    # 3. Display name.
    display_name, err = _validate_display_name(body.get("display_name"))
    if err:
        return JSONResponse({"error": err}, status_code=400)

    # 4. Team.
    team_raw = body.get("team")
    team = team_raw.strip().upper() if isinstance(team_raw, str) else ""
    if team not in settings.teams_list:
        return JSONResponse({"error": "invalid team"}, status_code=400)

    # 5. Protocol.
    protocol = body.get("protocol")
    if protocol not in ("mc", "mt"):
        return JSONResponse({"error": "invalid protocol"}, status_code=400)

    # settings.join_meshtastic_enabled opens or closes this path for a
    # deployment -- a registered node is what puts a Meshtastic player on
    # the board at all, so a deployment that hasn't decided to run
    # Meshtastic yet leaves this off. Enforced here regardless of what any
    # form shows; a disabled control in a page is not a restriction.
    if protocol == "mt" and not settings.join_meshtastic_enabled:
        return JSONResponse(
            {"error": "Meshtastic registration is not open yet"}, status_code=503
        )

    # 6. node_ref: OPTIONAL, even for meshtastic. A player can register
    # with no radio at all and add one (or several) afterward through
    # the key-authenticated routes in app/nodes_api.py -- that is the
    # whole point of that module. MeshCore radios still self-bind on
    # their own from a position batch's contact key and never take a
    # node_ref here, so this only ever applies to protocol == "mt". If
    # a node_ref IS supplied at signup, though, it is validated and
    # bound exactly as before, including the already-registered check
    # below -- an empty/missing value is the only thing that now means
    # "skip it", not a relaxed version of the format check.
    node_ref = None
    if protocol == "mt":
        raw_node_ref = body.get("node_ref")
        supplied = isinstance(raw_node_ref, str) and raw_node_ref.strip() != ""
        if supplied:
            node_ref = normalize_node_ref(raw_node_ref)
            if node_ref is None:
                return JSONResponse(
                    {"error": "node_ref must be 8 hex characters, with or "
                              "without a leading !"},
                    status_code=400,
                )

    now = int(time.time())
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        dup = conn.execute(
            "SELECT 1 FROM player WHERE LOWER(display_name) = LOWER(?)",
            (display_name,),
        ).fetchone()
        if dup:
            conn.execute("ROLLBACK")
            return JSONResponse({"error": "that name is taken"}, status_code=409)

        if node_ref is not None:
            bound = conn.execute(
                "SELECT player_id FROM player_node WHERE protocol = 'mt' AND node_ref = ?",
                (node_ref,),
            ).fetchone()
            if bound:
                conn.execute("ROLLBACK")
                return JSONResponse(
                    {"error": "that node is already registered to another player"},
                    status_code=409,
                )

        # 7. Create the player + key.
        cur = conn.execute(
            "INSERT INTO player(display_name, team, created_at) VALUES (?, ?, ?)",
            (display_name, team, now),
        )
        player_id = cur.lastrowid

        raw_key = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO api_key(key_hash, player_id, issued_at) VALUES (?, ?, ?)",
            (hash_secret(raw_key), player_id, now),
        )

        if node_ref is not None:
            conn.execute(
                "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) "
                "VALUES ('mt', ?, ?, ?)",
                (node_ref, player_id, now),
            )

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    # 8. Plaintext key shown once, plus the config link for MeshCore.
    return JSONResponse(
        _registration_response(display_name, team, protocol, raw_key),
        status_code=200,
    )


@router.post("/api/join/redeem")
async def join_redeem(request: Request) -> JSONResponse:
    """Turn a pre-issued, single-use join_token into a key. This is the
    path a future mesh join command will use -- it does not itself
    accept or transmit anything over a radio.
    """
    ip = _client_ip(request)
    if _rate_limited(ip):
        return JSONResponse(
            {"error": "too many attempts, try again later"}, status_code=429
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad request"}, status_code=400)

    token = body.get("token")
    if not isinstance(token, str) or not token:
        return JSONResponse({"error": "token is required"}, status_code=400)

    token_hash = hash_secret(token)
    now = int(time.time())

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            "SELECT token_hash, player_id, expires_at, consumed_at "
            "  FROM join_token WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return JSONResponse({"error": "invalid token"}, status_code=403)
        if row["consumed_at"] is not None:
            conn.execute("ROLLBACK")
            return JSONResponse({"error": "token already used"}, status_code=403)
        if row["expires_at"] <= now:
            conn.execute("ROLLBACK")
            return JSONResponse({"error": "token expired"}, status_code=403)

        player = conn.execute(
            "SELECT display_name, team FROM player WHERE player_id = ?",
            (row["player_id"],),
        ).fetchone()
        if player is None:
            conn.execute("ROLLBACK")
            return JSONResponse({"error": "player not found"}, status_code=404)

        # join_token doesn't carry its own protocol column -- by the time
        # a token is redeemed the player already has whichever radio
        # binding triggered the join command, so that binding's protocol
        # is what determines whether a MeshCore config link is included.
        node = conn.execute(
            "SELECT protocol FROM player_node WHERE player_id = ? LIMIT 1",
            (row["player_id"],),
        ).fetchone()
        protocol = node["protocol"] if node else None

        raw_key = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO api_key(key_hash, player_id, issued_at) VALUES (?, ?, ?)",
            (hash_secret(raw_key), row["player_id"], now),
        )
        conn.execute(
            "UPDATE join_token SET consumed_at = ? WHERE token_hash = ?",
            (now, token_hash),
        )

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return JSONResponse(
        _registration_response(player["display_name"], player["team"], protocol, raw_key),
        status_code=200,
    )


# ---- team switching (key-authenticated) ----------------------------------
#
# Everything below is a player changing their OWN team, capped at once
# per calendar month -- app/admin_api.py's admin_set_team() is the
# unlimited operator override. Ground stays exactly where it was
# (mc_tile.owner_team is frozen at paint time and never re-derived from
# player.team); check-in points, exploration points, and streaks all
# join live on player.team already (app/checkin.py's
# team_checkin_points()/team_place_points()), so they follow the player
# to the new team for free. Nothing about scoring changes here -- this
# only ever writes player.team and an audit row in player_team_change.
#
# Same two-tier (address, then key) rate limiting app/nodes_api.py and
# app/checkin_api.py's key-authenticated routes already use, and the
# same settings app/checkin_api.py's own copy reuses rather than
# inventing a third rate-limit mechanism for -- see app/auth.py's
# require_api_key_principal() docstring. Independent
# new_rate_limit_bucket() instances, same pattern as _attempts above --
# never shared with app/nodes_api.py's or app/checkin_api.py's own
# copies of this same dependency (see app/auth.py's module docstring
# for why merging those pools would be an observable behavior change).

_team_addr_rate_limiter = new_rate_limit_bucket()
_team_key_rate_limiter = new_rate_limit_bucket()

require_team_principal = require_api_key_principal(
    pre_auth_limiter=_team_addr_rate_limiter,
    post_auth_limiter=_team_key_rate_limiter,
)


def _current_month_window(now: int) -> tuple[int, int]:
    """[start, end) unix timestamps for the calendar month `now` falls
    in, in settings.checkin_net_timezone -- the exact convention
    app/results.py scores a month on (month_key() + month_bounds(),
    both imported unchanged from there), so a player's switch allowance
    resets in lockstep with the scoring month rather than holding a
    second opinion about when a month starts.
    """
    return month_bounds(month_key(now))


def _switch_used_this_month(conn, player_id: int, start: int, end: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM player_team_change"
        " WHERE player_id = ? AND actor = 'player'"
        "   AND changed_at >= ? AND changed_at < ?",
        (player_id, start, end),
    ).fetchone()
    return row is not None


@router.get("/api/team")
async def team_status(
    request: Request, principal: Principal = Depends(require_team_principal)
) -> JSONResponse:
    """Read-only status for the switch-team UI: the player's current
    team, whether a self-switch is available right now, and
    next_switch_at -- always the end of the current month window (when
    the switch allowance next resets), regardless of switch_available.
    That value is true and useful in both states, so it is never null.
    """
    player_id = principal.player_id

    now = int(time.time())
    start, end = _current_month_window(now)

    conn = connect()
    try:
        player = conn.execute(
            "SELECT team FROM player WHERE player_id = ?", (player_id,)
        ).fetchone()
        used = _switch_used_this_month(conn, player_id, start, end)
    finally:
        conn.close()

    if player is None:
        return JSONResponse({"error": "player not found"}, status_code=404)

    return JSONResponse(
        {
            "team": player["team"],
            "switch_available": not used,
            "next_switch_at": end,
        },
        status_code=200,
    )


@router.post("/api/team")
async def switch_team(
    request: Request, principal: Principal = Depends(require_team_principal)
) -> JSONResponse:
    """Change the caller's own team, once per calendar month. See the
    module-level comment above this section for what does and does not
    move with the player.
    """
    player_id = principal.player_id

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad request"}, status_code=400)

    team, terr = _validate_team(body.get("team"))
    if terr:
        return JSONResponse({"error": terr}, status_code=400)

    now = int(time.time())
    start, end = _current_month_window(now)

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
            conn.execute("ROLLBACK")
            return JSONResponse({"error": "you are already on that team"}, status_code=400)

        if _switch_used_this_month(conn, player_id, start, end):
            conn.execute("ROLLBACK")
            return JSONResponse(
                {"error": "you can only switch teams once per month",
                 "next_switch_at": end},
                status_code=409,
            )

        conn.execute(
            "UPDATE player SET team = ? WHERE player_id = ?",
            (team, player_id),
        )
        conn.execute(
            "INSERT INTO player_team_change"
            "(player_id, from_team, to_team, changed_at, actor) "
            "VALUES (?, ?, ?, ?, 'player')",
            (player_id, player["team"], team, now),
        )

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return JSONResponse(
        {"team": team, "next_switch_at": end},
        status_code=200,
    )
