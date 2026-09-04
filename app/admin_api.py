"""FastAPI router for the admin door: revoke keys, disable players, run
the roles layer everything else here now depends on.

---- roles, not a shared secret (privacy-hardening pass) ------------------

Every admin/operator action used to be authenticated by one shared
secret -- an `X-Admin-Token` header, compared with
`secrets.compare_digest` against `settings.admin_token`. That made every
action anonymous: the database could say a key was revoked, never WHO
revoked it. This module now uses three tiers, held on the ACCOUNT (never
on a player, never on an API key -- `account.role`, see that column's
own MIGRATIONS comment in app/db.py):

  - operator: everything an admin can do, plus granting and revoking
    the admin role (POST/DELETE-shaped below as
    /api/admin/roles/grant and /api/admin/roles/revoke).
  - admin: every route in this file and app/admin_ops.py below the
    roles section itself. Admins cannot admin admins -- an admin
    account can never grant, revoke, or otherwise act on ANY account's
    role, including its own, full stop. That boundary is enforced by
    /api/admin/roles/* requiring the OPERATOR rank specifically (see
    _role_guard's `need` parameter) -- there is no route an admin can
    reach that touches account.role at all, so there is nothing to
    turn around.
  - player: no admin surface. Everything in this module and
    app/admin_ops.py 404s or 401s the same as an anonymous caller.

_role_guard() (below) is what every route in both files now opens
with, in place of the old _api_guard() token check -- see its own
docstring for the 404-vs-401 contract, which is unchanged from before:
404 when the admin surface does not exist on this deployment at all,
401 when it exists but this caller cannot use it.

Holding a role is not by itself enough to USE it: _role_guard() also
requires active two-factor authentication on the caller's account, for
admin and operator alike -- see its own docstring for the full
reasoning and for why that one failure mode gets a 403 instead of
folding into the generic 401. Granting a role (POST
/api/admin/roles/grant) still never requires TOTP; the requirement is
enforced at use, not at grant, so an operator can hand the role to
someone before they have enrolled an authenticator.

`settings.admin_token` still exists, but its ONLY remaining power is
POST /api/admin/roles/claim: a signed-in account with active two-factor
authentication submits the token and, on a match, gains the operator
role. See that route's own docstring for the full reasoning (why this
is a real login-shaped event rather than a bypass, why TOTP is
required, why several accounts may claim with the same token, and how
this avoids ever locking every operator out). If the header still
authenticated ordinary requests, none of the above would matter --
anyone holding the token would bypass accounts, roles, and the audit
log entirely, which is exactly the bypass this whole pass exists to
close. There is no other route anywhere that reads
`request.headers.get("X-Admin-Token")` or its equivalent; grep for it
if you are ever unsure.

Every mutating route in this file and app/admin_ops.py calls
_log_admin_action() (below) right alongside its own write -- see
admin_action_log's own comment in app/db.py for the audit shape this
implements and why it is a new table rather than a repurposed
account_link_event.

`GET /admin` serves the page shell itself with only the same
enabled/disabled check every other route here uses (see
_admin_surface_enabled()) -- it is just a shell with no player data in
it, the same way any other app page needs to be reachable before the
browser's own JS has decided whether the signed-in account can see
anything inside it. The panel's own entry point lives on the account
page now (a button, shown only to a role-holding account -- see
frontend/account.js) rather than a token box on this page; the page
itself is unauthenticated the same "reachable, but nothing behind it
without a role" way a login page always is.
"""
from __future__ import annotations

import logging
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .auth import new_rate_limit_bucket
from .client_ip import get_client_ip
from .config import settings
from .db import WriteSession, connect
from .mc_ingest import hash_secret
from .node_ref import normalize_node_ref, normalize_public_key
from .sessions import SessionPrincipal, optional_session

log = logging.getLogger("admin_api")

router = APIRouter()

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



# Rank order for the two roles that can hold anything -- higher can do
# everything lower can, per the module docstring's "operator >
# admin > player" line. Deliberately does not include "player" at all:
# a NULL role is refused outright before this table is ever consulted
# (see _role_guard below), never looked up in it and found absent.
_ROLE_RANK = {"admin": 1, "operator": 2}


def _admin_surface_enabled(conn) -> bool:
    """Whether the admin/operator surface exists AT ALL on this
    deployment -- the same "empty means off, never open" contract
    settings.admin_token's own comment has always described, now
    extended to also stay open once an operator has actually claimed
    the role.

    True when EITHER settings.admin_token is set (so POST
    /api/admin/roles/claim itself is reachable -- see that route's own
    docstring for why it cannot require an existing role) OR at least
    one account already holds a role. This is the ordering that avoids
    ever locking every operator out: a fresh deployment with the token
    unset and nobody holding a role has genuinely nothing here to
    reach, and 404s exactly like before this pass. Setting the token
    turns claiming on. Once the first operator claims, the surface
    keeps working even if the token is later cleared -- a reasonable
    thing to do once bootstrap is done and minting a brand-new operator
    isn't needed for a while -- because an operator already exists to
    keep using it. The one thing that can never happen is the token
    being unset AND nobody holding a role AND the surface still being
    reachable: there would be no way back in at all.
    """
    if settings.admin_token:
        return True
    return conn.execute(
        "SELECT 1 FROM account WHERE role IS NOT NULL LIMIT 1"
    ).fetchone() is not None


async def _role_guard(request: Request, *, need: str = "admin") -> SessionPrincipal | JSONResponse:
    """Replaces the old shared-secret _api_guard() for every route in
    this file and app/admin_ops.py except POST /api/admin/roles/claim
    itself (which cannot require a role -- see its own docstring).
    Returns the caller's SessionPrincipal on success, so a mutating
    route can pass session.account_id straight into
    _log_admin_action(); returns a JSONResponse to hand back as-is on
    any failure. Callers use the same shape the old guard already
    established:

        guard = await _role_guard(request)
        if isinstance(guard, JSONResponse):
            return guard
        session = guard

    404 when _admin_surface_enabled() says the surface does not exist
    on this deployment at all -- indistinguishable from the route not
    existing, same contract the old token guard already used. 401 for
    every other failure that does NOT already prove the caller holds
    `need` (no session cookie, an expired/revoked one, a real account
    with no role, or a role that outranks-fails `need`) -- deliberately
    the SAME status and body for all of those, never a 403 or a
    distinct message for "you have a role but not enough of one":
    telling a caller which part was wrong (missing session vs.
    insufficient role) would hand an attacker free information about
    which accounts exist and what they hold, the same "don't reveal
    which part failed" reasoning app/sessions.py's require_session()
    and app/auth.py's require_api_key_principal() already apply to
    their own failure modes.

    ---- the one failure mode that DOES get its own shape: role held,
    TOTP not active ----------------------------------------------------

    Matt's call: an account granted admin (or operator) may hold that
    role with only a password behind it -- POST /api/admin/roles/grant
    never required TOTP to GRANT the role (an operator can hand it to
    someone before they have set up an authenticator, and it starts
    working the moment they do; refusing the grant would make the
    operator wait on the grantee). But admin is reachable single-factor
    that way, and it can delete players, reissue API keys, release
    account links, and edit nets -- the weakest reachable path into the
    whole admin surface if left alone. So the requirement moves from
    "claim-time" (POST /api/admin/roles/claim, already TOTP-gated, see
    that route's own docstring) to "use-time": EVERY route this guard
    protects, for admin and operator alike.

    This failure is answered with 403, not the generic 401 above, and
    that is a deliberate departure from the "never reveal which part
    failed" rule two paragraphs up -- not an oversight. The reasoning
    that rule rests on is that an anonymous or wrong-role caller learns
    nothing valuable from a generic 401 that they did not already
    suspect. That reasoning does not apply here: this caller's session
    already resolved to an account holding `need`, which is not
    something the response reveals -- it is something the caller
    already knows, because it is their own account. Handing them a
    bare 401 here would not protect any secret; it would just look
    like their access is broken, with no way to tell "your role was
    pulled" from "your session died" from "you need to turn on
    two-factor," and only the last of those has a fix the caller can
    act on themselves. This mirrors POST /api/admin/roles/claim's own
    403-not-401 choice (see that route's docstring) for the identical
    reason: the caller IS who they say they are, they are just not
    eligible to use this yet, which is a distinct condition from
    "unauthorized."

    A PENDING (enrolled but never activated -- account_totp.activated_at
    IS NULL) row does not count, same as claim's own check: an unproven
    secret must never become the thing standing between an account and
    anything it can otherwise reach, including the admin surface.

    `need` is "admin" (the default -- every route below the roles
    section itself) or "operator" (the three roles routes: grant,
    revoke, and the roster listing). optional_session(), not
    require_session(): this function must never RAISE on a bad
    session, it has its own 404-vs-401 decision to make first (whether
    the surface exists at all), which a raised HTTPException would
    short-circuit past.
    """
    conn = connect()
    try:
        if not _admin_surface_enabled(conn):
            return JSONResponse({"error": "not found"}, status_code=404)

        session = await optional_session(request)
        if session is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        row = conn.execute(
            "SELECT role FROM account WHERE account_id = ?", (session.account_id,)
        ).fetchone()
        role = row["role"] if row is not None else None
        if role is None or _ROLE_RANK.get(role, 0) < _ROLE_RANK[need]:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        totp_row = conn.execute(
            "SELECT 1 FROM account_totp WHERE account_id = ? AND activated_at IS NOT NULL",
            (session.account_id,),
        ).fetchone()
        if totp_row is None:
            # See this function's own docstring for why this gets its
            # own 403 shape rather than folding into the generic 401
            # above -- the caller has already proven they hold `need`,
            # so telling them what to do about it reveals nothing an
            # attacker could not already see by holding the role
            # themselves.
            return JSONResponse(
                {
                    "error": "two-factor authentication must be enabled on this account "
                    "to use the admin panel"
                },
                status_code=403,
            )
        return session
    finally:
        conn.close()


def _log_admin_action(
    conn, *, actor_account_id: int, action: str, detail: str | None = None, now: int | None = None
) -> None:
    """Writes one admin_action_log row -- see that table's own comment
    in app/db.py for the full shape and why it exists. Called at the
    end of every mutating route in this file and app/admin_ops.py,
    using the SAME conn/transaction the route's own write already has
    open, so the audit row commits or rolls back atomically with the
    action it describes -- never a separate best-effort write after
    the fact that could succeed or fail independently of what it is
    supposed to be recording.
    """
    if now is None:
        now = int(time.time())
    conn.execute(
        "INSERT INTO admin_action_log(actor_account_id, action, detail, created_at) "
        "VALUES (?, ?, ?, ?)",
        (actor_account_id, action, detail, now),
    )


# ---- page ---------------------------------------------------------------


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_page() -> HTMLResponse:
    conn = connect()
    try:
        enabled = _admin_surface_enabled(conn)
    finally:
        conn.close()
    if not enabled:
        return HTMLResponse("<h1>meshwars</h1><p>not found</p>", status_code=404)
    path = Path(__file__).resolve().parent.parent / "frontend" / "admin.html"
    if not path.exists():
        return HTMLResponse("<h1>meshwars admin</h1><p>admin page not bundled</p>", status_code=404)
    return HTMLResponse(path.read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})


# ---- data ---------------------------------------------------------------


@router.get("/api/admin/players")
async def admin_players(request: Request):
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard

    conn = connect()
    try:
        players = conn.execute(
            "SELECT player_id, display_name, team, created_at, disabled_at, account_id "
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
                # None for a player nobody has claimed yet. Not the
                # account's identities/email -- just enough for the
                # admin panel to show "linked" vs. "not linked" and to
                # pass this same id back to
                # POST /api/admin/player/unlink-account below when an
                # operator needs to release a mis-linked account.
                "account_id": p["account_id"],
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
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard

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
        _log_admin_action(
            conn, actor_account_id=session.account_id, action="revoke_key",
            detail=f"key_hash_prefix={match['key_hash'][:8]} player_id={match['player_id']}",
            now=now,
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
    same check-then-act conflict check -- just authenticated by an
    admin/operator role instead of a player's key.

    NOT destructive, unlike remove right below: it only ever creates a
    binding nobody held before, or confirms one this same player
    already has. A wrong player_id here binds a real radio to the
    wrong (but real) player -- visible immediately in the admin list
    and reversible with the remove route below -- it never takes
    anything away from anyone. So this route gets only _role_guard(),
    not the player_id + display_name confirmation guard remove/delete/
    reissue require.
    """
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard

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
        _log_admin_action(
            conn, actor_account_id=session.account_id, action="node_add",
            detail=f"player_id={player_id} {protocol}:{node_ref}", now=now,
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
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard

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
        _log_admin_action(
            conn, actor_account_id=session.account_id, action="node_remove",
            detail=f"player_id={player_id} ({display_name}) {protocol}:{node_ref} removed={removed}",
        )
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
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard

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
        _log_admin_action(
            conn, actor_account_id=session.account_id,
            action="player_disable" if disable else "player_enable",
            detail=f"player_id={player_id}",
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
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard

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

        _log_admin_action(
            conn, actor_account_id=session.account_id, action="player_delete",
            detail=f"player_id={player_id} ({display_name})",
        )

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
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard

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
        # Never the raw key itself, same reasoning the response body's
        # own "key" field is a one-time-only value never persisted --
        # the audit row only needs to say an extra key was minted and
        # for whom, not what it is.
        _log_admin_action(
            conn, actor_account_id=session.account_id, action="issue_key",
            detail=f"player_id={player_id}", now=now,
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
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard

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

        _log_admin_action(
            conn, actor_account_id=session.account_id, action="reissue_key",
            detail=f"player_id={player_id} ({display_name}) revoked={revoked_count}", now=now,
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


@router.post("/api/admin/player/unlink-account")
async def admin_player_unlink_account(request: Request):
    """Release a player's account link -- an ordinary admin action
    (any account holding the admin role or above), same as every other
    route in this file; "operator" below refers only to
    account_link_event.actor's fixed 'user'|'operator' vocabulary
    (see that column's own comment in app/db.py), not this app's
    admin/operator role names, which predate that column by a long
    way.

    This is the ONLY place in the whole application that can clear
    player.account_id -- see app/account_api.py's link_key() for how it
    gets set, and its own module docstring for why nothing there ever
    offers to unset it: a player can claim a key-only player onto their
    account, but never release one, by design. Once a link is wrong
    (an account holder linked the wrong key, or two people share a
    radio and the key ended up claimed by the wrong side), nothing
    short of an operator clearing it by hand can free that player back
    up -- there is no self-service path and there must not be one.

    Same player_id + matching display_name confirmation guard as
    delete/reissue/node-remove above, for the same reason: this acts on
    a player on someone else's behalf (the account holder isn't the
    one making this request), so a stale or mistyped player_id must not
    be able to silently rip the wrong person's account away from them.

    Deliberately narrow: this clears player.account_id and writes one
    account_link_event row, nothing else. Every one of the player's own
    eighteen player-keyed tables -- radios, keys, check-in awards, month
    awards, points, tile ownership and history -- is untouched, so the
    player keeps everything it ever earned and simply becomes claimable
    again by whoever holds its API key (the same POST
    /api/account/link-key path that claimed it the first time). This is
    a release, not a delete: nobody's progress is at stake here, only
    which account (if any) that progress is currently attached to.

    Mirrors link_key()'s own account_link_event write: same table, same
    detail format ('player_id=<n>'), 'player_unlinked' as the kind
    link_key()'s 'player_linked' pairs with (see app/db.py's
    account_link_event schema comment, which already lists
    'player_unlinked' among the kinds this table expects), and actor
    'operator' -- the value account_link_event's own column comment and
    /api/admin/player/team's player_team_change rows already use for
    every operator-initiated write in this app, not the player-facing
    'user' link_key() itself writes.
    """
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard

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
    # WriteSession (app/db.py), not the manual connect()/BEGIN IMMEDIATE
    # pairing the rest of this file uses -- same primitive
    # app/account_api.py's link_key() holds for its own conflict-check-
    # then-write, and for the same reason: the "is this player linked,
    # and to which account" read below and the UPDATE/INSERT that acts
    # on it must be atomic against a second concurrent request (another
    # release, or a link-key racing in) for the same player, not two
    # separate round trips a race could land between.
    async with WriteSession() as conn:
        row = conn.execute(
            "SELECT display_name, account_id FROM player WHERE player_id = ?",
            (player_id,),
        ).fetchone()
        if row is None:
            return JSONResponse({"error": "player not found"}, status_code=404)
        if row["display_name"] != display_name:
            return JSONResponse(
                {"error": "display name does not match"}, status_code=409
            )

        account_id = row["account_id"]
        if account_id is None:
            # Not a silent no-op success -- an operator who thinks they
            # just released a link should be told when there was none
            # to release, the same way link_key()'s own conflict
            # responses are specific rather than a generic failure.
            return JSONResponse(
                {"error": "player is not linked to any account"}, status_code=409
            )

        conn.execute(
            "UPDATE player SET account_id = NULL WHERE player_id = ?", (player_id,)
        )
        conn.execute(
            "INSERT INTO account_link_event(account_id, kind, detail, actor, created_at) "
            "VALUES (?, 'player_unlinked', ?, 'operator', ?)",
            (account_id, f"player_id={player_id}", now),
        )
        _log_admin_action(
            conn, actor_account_id=session.account_id, action="unlink_account",
            detail=f"player_id={player_id} ({display_name}) account_id={account_id}", now=now,
        )

    log.info(
        "admin: released player %d (%s) from account %d",
        player_id, display_name, account_id,
    )
    return JSONResponse(
        {
            "player_id": player_id,
            "display_name": display_name,
            "account_id": account_id,
            "unlinked": True,
        },
        status_code=200,
    )


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
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard

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
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard

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
        _log_admin_action(
            conn, actor_account_id=session.account_id, action="api_client_create",
            detail=f"label={label!r}", now=now,
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
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard

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
        if cur.rowcount:
            _log_admin_action(
                conn, actor_account_id=session.account_id, action="api_client_revoke",
                detail=f"key_hash_prefix={prefix} revoked={cur.rowcount}", now=now,
            )
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
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard

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
        _log_admin_action(
            conn, actor_account_id=session.account_id, action="player_team_set",
            detail=f"player_id={player_id} {player['team']}->{team}", now=now,
        )

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    log.info("admin: set player %d team to %s", player_id, team)
    return JSONResponse({"player_id": player_id, "team": team, "changed": True}, status_code=200)


# ---- roles: claim / grant / revoke --------------------------------------
#
# The whole point of this pass. Everything above this line is an
# ordinary admin action, reachable by admin or operator alike; every
# route below touches account.role itself, and is either reachable by
# no role at all (claim -- there is nothing to require a role of yet)
# or by operator alone (grant/revoke/list -- see _role_guard's `need`
# parameter, and this module's own docstring for why admin can never
# reach these three no matter what).

# Address-keyed rate limit on the token comparison below -- without one
# this is a straightforward token-guessing oracle, the same reasoning
# app/account_api.py's _link_key_addr_limiter gives its own endpoint.
# Independent instance, per app/auth.py's own "every call site owns its
# budget" convention -- this is the single highest-value guessing
# target this feature adds (a correct guess grants OPERATOR, not merely
# a player), so it does not share link-key's budget.
_claim_operator_addr_limiter = new_rate_limit_bucket()


@router.post("/api/admin/roles/claim")
async def admin_roles_claim(request: Request) -> JSONResponse:
    """Claim the operator role onto the CALLER's own signed-in account.

    This is the only remaining job settings.admin_token has -- see this
    module's own docstring for the full before/after. The shape
    deliberately mirrors app/account_api.py's own POST
    /api/account/link-key: a signed-in account submits a secret, it is
    compared in constant time, and on success something is attached to
    THAT account and nothing else -- never a target account_id supplied
    in the body, the same reasoning link_key() never accepts a
    player_id: the only account a caller can grant something onto here
    is the one whose session cookie is on the request.

    ---- why this solves bootstrap ------------------------------------

    Before this route runs for the first time, no account holds any
    role -- there is no "existing operator" to ask, the same chicken-
    and-egg problem every shared-secret-to-roles migration has. This
    route is deliberately the ONE place in the whole roles surface that
    does not require an existing role (see _role_guard's own docstring
    for why every other roles route does): the token itself is what
    proves the caller is allowed to become the first operator. Once
    that happens, _admin_surface_enabled() (see this module's own
    docstring) keeps the rest of the surface reachable even if the
    token is later cleared.

    ---- why several accounts may claim with the same token -----------

    Nothing here marks the token "used" after a first claim. Matt's own
    call: a second account claiming with the same token is exactly how
    a second operator gets added, and exactly how recovery works if
    every existing operator becomes unreachable (a lost password with
    no other door, a departed volunteer) -- sign into a fresh account,
    claim with the token, you are an operator again. Every claim is its
    own account_link_event + admin_action_log row (below), so "who
    claimed, and when" stays fully auditable even though the token
    itself is reusable.

    ---- why TOTP is required -------------------------------------------

    Matt's explicit call: the highest-privilege role in this
    application must never sit on a single factor. A password or a
    magic-link email is one secret; if that is ever phished, guessed,
    or leaked, this route would otherwise hand the operator role to
    whoever holds it, on top of the admin_token itself already being a
    second secret an attacker would need -- belt AND suspenders, not
    either alone. 403, not 401: the caller IS who their session says
    they are (a real signed-in account, a real correct token) and
    nothing here is asking them to authenticate again -- they are
    simply not eligible to hold this role until they enroll TOTP first
    (app/totp_api.py's POST /api/account/totp/enroll +
    .../activate), which is a distinct condition from "unauthorized."

    ---- upgrading an existing admin -----------------------------------

    An account that already holds the admin role (granted by an
    operator, see POST /api/admin/roles/grant) may claim operator the
    same way any other account does -- this is a legitimate elevation
    path (the token AND active TOTP are still both required), not a
    backdoor: it is exactly as hard to reach as becoming the very first
    operator was.
    """
    if not settings.admin_token:
        # Same "empty means off, never open" contract every other route
        # in this file uses -- checked BEFORE anything about the caller
        # (their session, their TOTP status) is even looked at, so a
        # deployment with claiming turned off reveals nothing about
        # whether the caller is signed in at all.
        return JSONResponse({"error": "not found"}, status_code=404)

    session = await optional_session(request)
    if session is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    ip = get_client_ip(request)
    if _claim_operator_addr_limiter.limited(
        ip,
        limit=settings.admin_claim_operator_rate_limit_attempts,
        window=settings.admin_claim_operator_rate_limit_window_seconds,
    ):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    supplied = body.get("token") if isinstance(body, dict) else None
    if not isinstance(supplied, str) or not supplied:
        return JSONResponse({"error": "token is required"}, status_code=400)
    if not secrets.compare_digest(supplied, settings.admin_token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    now = int(time.time())
    async with WriteSession() as conn:
        totp_row = conn.execute(
            "SELECT 1 FROM account_totp WHERE account_id = ? AND activated_at IS NOT NULL",
            (session.account_id,),
        ).fetchone()
        if totp_row is None:
            return JSONResponse(
                {
                    "error": "two-factor authentication must be enabled on this account "
                    "before it can claim the operator role"
                },
                status_code=403,
            )

        previous_role = conn.execute(
            "SELECT role FROM account WHERE account_id = ?", (session.account_id,)
        ).fetchone()["role"]

        conn.execute(
            "UPDATE account SET role = 'operator' WHERE account_id = ?", (session.account_id,)
        )
        conn.execute(
            "INSERT INTO account_link_event(account_id, kind, detail, actor, created_at) "
            "VALUES (?, 'operator_claimed', ?, 'user', ?)",
            (session.account_id, f"previous_role={previous_role or 'none'}", now),
        )
        _log_admin_action(
            conn, actor_account_id=session.account_id, action="claim_operator",
            detail=f"previous_role={previous_role or 'none'}", now=now,
        )

    log.info("admin: account %d claimed the operator role", session.account_id)
    return JSONResponse({"account_id": session.account_id, "role": "operator"}, status_code=200)


@router.get("/api/admin/roles")
async def admin_roles_list(request: Request) -> JSONResponse:
    """Every account currently holding a role, operator-only -- see this
    module's own docstring for why admin can never reach this (or
    either of the two mutating roles routes below), regardless of what
    else it can do.

    LEFT JOINs to `player` (on player.account_id = account.account_id,
    which app/db.py keeps UNIQUE -- at most one player per account) to
    include the holder's display_name for the UI. Must stay a LEFT
    JOIN, never an inner join: an account can hold a role with no
    player linked at all, and this route is the only place a role can
    be revoked, so an inner join would silently drop that account from
    the list -- a role with no way to see or revoke it.
    """
    guard = await _role_guard(request, need="operator")
    if isinstance(guard, JSONResponse):
        return guard

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT account.account_id, account.role, player.display_name "
            "FROM account LEFT JOIN player ON player.account_id = account.account_id "
            "WHERE account.role IS NOT NULL ORDER BY account.account_id"
        ).fetchall()
    finally:
        conn.close()
    return JSONResponse(
        {
            "roles": [
                {
                    "account_id": r["account_id"],
                    "role": r["role"],
                    "display_name": r["display_name"],
                }
                for r in rows
            ]
        },
        status_code=200,
    )


@router.post("/api/admin/roles/grant")
async def admin_roles_grant(request: Request) -> JSONResponse:
    """Grant the admin role to another account. Operator-only -- see
    this module's own docstring for the one-directional boundary this
    enforces: an admin account can never reach this route at all (a
    plain 401, indistinguishable from having no role), so there is no
    path by which an admin can grant itself or anyone else anything.

    Deliberately grants ONLY 'admin', never 'operator' -- the request
    body has no `role` field to choose one, on purpose. Becoming an
    operator has exactly one door (POST /api/admin/roles/claim, the
    token + active TOTP), never a grant from another operator -- see
    this module's own docstring for why that is the bootstrap/recovery
    path this whole design leans on, and keeping it the ONLY path means
    an operator can never mint a second operator by fiat, only by
    handing out the token (a decision Matt makes deliberately each time,
    not a button in this panel).

    Targets by `display_name`: GET /api/admin/roles shows an operator
    a player's name, not the raw account id underneath it, so the name
    is what the operator actually has in hand. Resolved the exact way
    app/join_api.py's own signup uniqueness check resolves one --
    stripped in Python, then compared case-insensitively in SQL
    (LOWER() on both sides) -- so a name join() would refuse as
    "taken" is the same name this route finds, with no drift between
    the two checks.

    `account_id` is still accepted too, for any caller that already
    has it. When both are given they must resolve to the same
    account, or the request is refused outright (400) rather than one
    silently winning -- a name and an id pointing at two different
    accounts is a caller bug, not something to guess through.

    Two name-specific refusals, both 404, kept distinct from each
    other and from the plain "account not found" below so an operator
    is never left guessing which of three things went wrong:
      - no player carries that name at all, or
      - that player exists but its `account_id` is NULL -- an
        unclaimed, key-only player (see app/join_api.py's own note on
        an anonymous Meshtastic join never linking an account). There
        is no account to grant a role to; the operator is told that
        plainly rather than getting a "not found" indistinguishable
        from a typo.

    Refuses (409) an account that already holds the operator role --
    granting 'admin' onto it would silently DEMOTE an operator to
    admin, which is not what "grant" means and must never happen by
    accident. Revoke it first (POST /api/admin/roles/revoke) if a
    demotion is actually intended. Granting admin to an account that
    is already admin is a no-op success (changed: false), the same
    "a retried request should just succeed" reasoning
    admin_set_team()'s own already-on-that-team case gives above.
    """
    guard = await _role_guard(request, need="operator")
    if isinstance(guard, JSONResponse):
        return guard
    session = guard

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad request"}, status_code=400)

    raw_account_id = body.get("account_id")
    given_account_id: int | None = None
    if raw_account_id is not None:
        if not isinstance(raw_account_id, int) or isinstance(raw_account_id, bool):
            return JSONResponse({"error": "account_id must be an integer"}, status_code=400)
        given_account_id = raw_account_id

    raw_name = body.get("display_name")
    given_name: str | None = None
    if raw_name is not None:
        if not isinstance(raw_name, str):
            return JSONResponse({"error": "display_name must be a string"}, status_code=400)
        stripped = raw_name.strip()
        if stripped:
            given_name = stripped

    if given_account_id is None and given_name is None:
        return JSONResponse(
            {"error": "account_id or display_name is required"}, status_code=400
        )

    now = int(time.time())
    async with WriteSession() as conn:
        target_account_id = given_account_id
        if given_name is not None:
            # Same match app/join_api.py's own dup check uses at its
            # step-3 uniqueness gate: stripped in Python above, then
            # LOWER() on both sides in SQL -- keep the two in lockstep,
            # not two independently-drifting ideas of "same name".
            player_row = conn.execute(
                "SELECT account_id FROM player WHERE LOWER(display_name) = LOWER(?)",
                (given_name,),
            ).fetchone()
            if player_row is None:
                return JSONResponse(
                    {"error": f'no player named "{given_name}"'}, status_code=404
                )
            name_account_id = player_row["account_id"]
            if name_account_id is None:
                return JSONResponse(
                    {
                        "error": f'"{given_name}" is not linked to any account — '
                                 "there is nothing to grant a role to"
                    },
                    status_code=404,
                )
            if given_account_id is not None and given_account_id != name_account_id:
                return JSONResponse(
                    {"error": "account_id and display_name refer to different accounts"},
                    status_code=400,
                )
            target_account_id = name_account_id

        row = conn.execute(
            "SELECT role FROM account WHERE account_id = ?", (target_account_id,)
        ).fetchone()
        if row is None:
            return JSONResponse({"error": "account not found"}, status_code=404)

        if row["role"] == "operator":
            return JSONResponse(
                {"error": "that account already holds the operator role — "
                          "revoke it first if a demotion to admin is intended"},
                status_code=409,
            )

        if row["role"] == "admin":
            return JSONResponse(
                {"account_id": target_account_id, "role": "admin", "changed": False},
                status_code=200,
            )

        conn.execute(
            "UPDATE account SET role = 'admin' WHERE account_id = ?", (target_account_id,)
        )
        _log_admin_action(
            conn, actor_account_id=session.account_id, action="role_granted",
            detail=f"account_id={target_account_id} role=admin", now=now,
        )

    log.info(
        "admin: account %d granted admin to account %d", session.account_id, target_account_id
    )
    return JSONResponse(
        {"account_id": target_account_id, "role": "admin", "changed": True}, status_code=200
    )


@router.post("/api/admin/roles/revoke")
async def admin_roles_revoke(request: Request) -> JSONResponse:
    """Revoke whatever role an account currently holds -- admin OR
    operator, either direction. Operator-only, same one-directional
    boundary as grant above: an admin account cannot reach this route
    at all, so it can never revoke another admin's role, an operator's
    role, or its own.

    Unlike grant, there is no "already holds a higher role" refusal
    here to worry about -- revoke only ever REMOVES a role, it can
    never accidentally promote anyone, so an operator revoking another
    operator (including, if they choose, their own account) is allowed
    outright. Recovery does not depend on preventing that: as long as
    settings.admin_token is still configured, POST
    /api/admin/roles/claim can always mint a fresh operator on any
    signed-in, TOTP-enabled account.
    """
    guard = await _role_guard(request, need="operator")
    if isinstance(guard, JSONResponse):
        return guard
    session = guard

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    target_account_id = body.get("account_id") if isinstance(body, dict) else None
    if not isinstance(target_account_id, int) or isinstance(target_account_id, bool):
        return JSONResponse({"error": "account_id is required"}, status_code=400)

    now = int(time.time())
    async with WriteSession() as conn:
        row = conn.execute(
            "SELECT role FROM account WHERE account_id = ?", (target_account_id,)
        ).fetchone()
        if row is None:
            return JSONResponse({"error": "account not found"}, status_code=404)
        if row["role"] is None:
            return JSONResponse({"error": "that account holds no role"}, status_code=409)

        previous_role = row["role"]
        conn.execute(
            "UPDATE account SET role = NULL WHERE account_id = ?", (target_account_id,)
        )
        _log_admin_action(
            conn, actor_account_id=session.account_id, action="role_revoked",
            detail=f"account_id={target_account_id} role={previous_role}", now=now,
        )

    log.info(
        "admin: account %d revoked %s from account %d",
        session.account_id, previous_role, target_account_id,
    )
    return JSONResponse(
        {"account_id": target_account_id, "role": None, "revoked": True}, status_code=200
    )
