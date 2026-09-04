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

from .account_api import (
    _ACCOUNT_SCOPED_TABLES,
    _PLAYER_SCOPED_TABLES,
    _door_counts,
    _has_password,
    _tombstone_display_name,
)
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


async def _role_guard(
    request: Request, *, need: str = "admin", return_role: bool = False
) -> SessionPrincipal | tuple[SessionPrincipal, str] | JSONResponse:
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

    `return_role`, when true, hands back `(session, role)` instead of
    bare `session` on success -- `role` being the exact string this
    function already read out of `account.role` to decide the `need`
    check above ("admin" or "operator"; never None, since a None role
    would already have failed that check). Every existing route only
    ever needed a yes/no answer to "does this caller meet `need`", so
    the bare-session return stays the default and every call site
    keeps working unchanged. POST /api/admin/player/delete is the one
    exception: it has to tell an admin apart from an operator, not
    just confirm the caller is at least one of them, because the two
    ranks get different answers there (see that route's own
    docstring). Reading `account.role` a second time in that route,
    separately from this function's own read of it two lines below,
    would be the same fact fetched twice through two different code
    paths that could in principle drift -- this parameter exists so
    there is exactly one read of "what role does this caller hold",
    reused by both the pass/fail decision here and that route's own
    finer-grained one.
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
        return (session, role) if return_role else session
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


@router.get("/api/admin/accounts")
async def admin_accounts(request: Request):
    """Every account on this deployment -- the account-shaped
    counterpart GET /api/admin/players just above has never had. That
    route lists every PLAYER, and only ever reaches an account BY WAY
    OF the player it happens to be linked to (`account_id` on each row,
    there purely so this file's own unlink-account route has something
    to pass back) -- an account with no linked player at all is
    invisible there, and reachable nowhere else in this admin surface.
    That state is real and already reachable two ways: POST
    /api/admin/player/unlink-account deliberately creates it, and any
    account that signs in but never finishes POST /api/join or POST
    /api/account/link-key starts there and can sit there indefinitely.
    This route is the fix: list every row in `account` directly, not
    every row reachable through `player`.

    Never the account's email/OAuth identities, password hash, TOTP
    secret, or session tokens -- none of that is safe to hand to the
    browser, the same "never the key hash itself, only enough to
    identify it" rule GET /api/admin/players already applies to
    api_key.key_hash just above. What IS safe and useful for an
    operator deciding what to do with a row:

      - account_id, so this can be passed straight to POST
        /api/admin/account/delete below.
      - The linked player's id and CURRENT display_name, if any --
        None when the account is orphaned, which is the one fact this
        whole route exists to surface. This is the SAME display_name
        column POST /api/admin/account/delete's own confirmation check
        reads, never a name captured or cached anywhere else, so what
        this listing shows an operator to type can never go stale
        against what that route will actually check it against.
      - role (account.role) -- None for an ordinary account, 'admin'
        or 'operator' for a role holder; the same column
        /api/admin/roles already reads and the same value
        POST /api/admin/account/delete's own role guard checks below,
        surfaced here purely so an operator can see at a glance which
        rows that guard will refuse to an admin.
      - sign_in_methods -- how many DOORS this account can currently be
        reached through (every account_identity provider it holds,
        plus one more if it has a password set), via
        app/account_api.py's own _door_counts() -- the same helper GET
        /api/account's own Security panel already builds its
        per-identity "can this be removed" answer from, reused here
        rather than a second count that could drift from it.
      - identity_providers -- the PROVIDER NAMES themselves (e.g.
        ["google", "email"]), not merely how many. Added alongside the
        three account-recovery routes below (disable-totp,
        password/clear, identity/remove): "remove a sign-in method"
        has to offer a button per provider actually present, and a
        bare count cannot drive that. This is still a narrower
        exposure than it looks -- a provider NAME is which service was
        used, not who the account belongs to (no subject id, no email
        address, nothing that identifies the person) -- which is the
        same line _mask_email() already draws for a full address
        versus a masked one in GET /api/account's own view. What is
        still deliberately never exposed HERE is the finer detail
        those routes never surface either: subject, email, or which
        specific row within a provider (unlink/remove has always acted
        on an entire provider at once, never a single row -- see
        DELETE /api/account/identity/{provider}'s own docstring).
      - has_password -- whether POST /api/admin/account/password/clear
        below has anything to act on; redundant with sign_in_methods
        in spirit but that field cannot be decomposed back into "is
        one of these doors a password" without exposing
        identity_providers' own count too, so this is its own boolean
        rather than asking the frontend to infer it.
      - totp_active -- whether POST /api/admin/account/disable-totp
        below has anything to act on (an account_totp row with
        activated_at set -- a pending, never-proven enrollment does
        not count, same "unproven secret guards nothing" rule
        _role_guard() itself applies). Never the secret, never a
        recovery-code count -- just the one bit the UI needs to decide
        whether to show the button at all.
      - created_at / last_login_at (account's own columns) -- when it
        first appeared and when it was last actually used, so a
        long-dead orphan reads differently on sight from one created
        five minutes ago by someone mid-join.
    """
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard

    conn = connect()
    try:
        accounts = conn.execute(
            "SELECT account_id, created_at, last_login_at, role FROM account "
            " ORDER BY account_id"
        ).fetchall()
        out = []
        for a in accounts:
            player_row = conn.execute(
                "SELECT player_id, display_name FROM player WHERE account_id = ?",
                (a["account_id"],),
            ).fetchone()
            per_provider, has_password = _door_counts(conn, a["account_id"])
            totp_active = conn.execute(
                "SELECT 1 FROM account_totp WHERE account_id = ? AND activated_at IS NOT NULL",
                (a["account_id"],),
            ).fetchone() is not None
            out.append({
                "account_id": a["account_id"],
                "player": (
                    {
                        "player_id": player_row["player_id"],
                        "display_name": player_row["display_name"],
                    }
                    if player_row is not None else None
                ),
                "role": a["role"],
                "sign_in_methods": sum(per_provider.values()) + (1 if has_password else 0),
                "identity_providers": sorted(per_provider.keys()),
                "has_password": has_password,
                "totp_active": totp_active,
                "created_at": a["created_at"],
                "last_login_at": a["last_login_at"],
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
    """Permanently remove a player -- the operator counterpart to
    DELETE /api/account (app/account_api.py), brought onto that
    route's "delete the person, keep the team" model rather than the
    hard-delete-everything shape this route used to have. See
    app/account_api.py's "account deletion" section comment for the
    full table-by-table reasoning, which this route now shares
    directly (`_PLAYER_SCOPED_TABLES`, `_ACCOUNT_SCOPED_TABLES`, and
    `_tombstone_display_name()` are all imported from there, not
    redefined here -- one definition of "what deletion means", used by
    both the self-service and the operator door). Only what genuinely
    differs on the operator path is documented below.

    This used to hard-delete the `player` row outright, plus every
    square where this player was the last painter (mc_tile,
    mc_tile_score, mc_tile_capture, mc_tile_capture_log) and
    mc_tile_unique_painter -- rewriting a shared, possibly already-
    published record (a square's capture history, a month's
    standings) to serve the removal of one player, exactly the harm
    app/account_api.py's own docstring explains at length. None of
    that runs any more: `player` is tombstoned (display_name
    overwritten with an unmistakable, uncollidable placeholder,
    disabled_at set, account_id cleared) instead of deleted, and
    mc_tile / mc_tile_capture_log / mc_tile_unique_painter and every
    other shared-history table are left completely untouched. Do not
    "restore" the square-deletion behavior -- it is not a missing
    feature, it is the exact harm this change removes. (mc_tile_score
    and mc_tile_capture were never player-keyed in the first place --
    they key on (season_id, cell_id[, team]) alone -- so they were only
    ever touched as a side effect of deleting the whole mc_tile row for
    a square this player last painted; with that row no longer deleted,
    neither is ever touched here again.)

    This also used to hard-delete only four of the eleven tables
    _PLAYER_SCOPED_TABLES now covers (player_ingest_stat,
    player_cell_ping, player_node, api_key), while deleting the
    `player` row itself out from under the rest -- player_last_fix,
    player_cell_repeater_credit, join_token, checkin_node_name,
    mc_checkin_binding, mc_node_confirmation, mt_node_confirmation --
    leaving every one of those pointing at a player_id that no longer
    resolved to anything. That was a plain bug, independent of the
    design question above, and is fixed the same way: every table in
    _PLAYER_SCOPED_TABLES is hard-deleted, unconditionally, before
    `player` is tombstoned.

    ---- what is different from DELETE /api/account -----------------

    - This acts on a TARGET player named in the request body
      (`player_id`), not on the caller. There is no session.player_id
      here at all -- see _role_guard() below for who is allowed to
      make this call, which is an entirely separate question from
      whose data it acts on.
    - Authorization is this file's own role guard (admin or operator,
      with active TOTP -- see _role_guard()'s own docstring),
      unchanged from before this pass. This is not the caller
      re-authenticating themselves the way DELETE /api/account
      requires a fresh password/TOTP check on top of the session --
      that check exists there because a browser's session cookie alone
      doesn't prove who is currently at the keyboard; here, the
      _role_guard() check on the OPERATOR's own account already covers
      that, and there is no equivalent credential of the TARGET's to
      ask for (an operator acting on someone else's account has no
      reason to hold their password or TOTP secret).
    - Confirmation is still the operator typing the target's current
      display_name exactly (case-sensitive, no trimming) -- unchanged.
      There is no _NO_PLAYER_CONFIRM_PHRASE equivalent here: every
      target of this route, by definition, already has a player row to
      read a display_name off of (the request identifies the player,
      not an account), so that self-service-only fallback does not
      apply.
    - The target's linked account, if any (player.account_id), is
      DELETED along with the player -- every _ACCOUNT_SCOPED_TABLES
      row for it, then the account row itself, the exact same cascade
      DELETE /api/account runs against the caller's own account.
      Deliberately not just unlinked (clearing player.account_id the
      way POST /api/admin/player/unlink-account does): unlinking is
      the right call when the LINK itself is wrong (a misclick, a
      shared radio claimed by the wrong side -- see that route's own
      docstring) and both sides should go on existing, just not
      attached to each other. Deleting a player is a different act
      entirely -- it says this person's presence in the game is being
      permanently ended -- and leaving their login (email/OAuth
      identity, password hash, active sessions) sitting around,
      unlinked but otherwise intact, would not "keep the team" the way
      tombstoning `player` does; it would just leave a working set of
      credentials for a person the operator has just erased, free to
      link-key straight onto a fresh player and undo the point of this
      call. So: tombstone the player, delete the account underneath it,
      same as if that account holder had run DELETE /api/account
      themselves -- except an operator initiated it instead of them.
      Recorded in admin_action_log either way (see below), including
      the account_id when one was deleted, so this is never a silent
      side effect.

      ---- an admin cannot use this route to reach a role-holder --------

      Deleting the target's account is exactly the kind of act
      /api/admin/roles/* deliberately keeps out of an admin's reach --
      that section's own routes require the OPERATOR rank specifically
      so an admin can never grant, revoke, or otherwise touch anyone's
      account.role, including its own (see this module's own
      docstring). Left alone, this route would have been a second door
      into the same act: an admin who names a player linked to an
      operator's (or another admin's) account would delete that
      account, role and all, on the strength of the plain admin role
      guard, with no role check of its own. So this route carries the
      same boundary directly: once the target is resolved (below), if
      the target has a linked account AND that account holds a role
      (admin or operator, checked via the same account.role column
      /api/admin/roles/* itself gates on) AND the caller's own rank is
      exactly "admin" (not "operator"), the deletion is refused before
      anything is written. An operator may still delete a role-holding
      account through this route -- that matches the roles model
      exactly, where operator is the one rank permitted to act on
      accounts that hold roles at all.

      The caller's rank for this check is read via
      `_role_guard(request, return_role=True)`, not by a second,
      separate query against `account.role` -- see that parameter's
      own docstring for why: there is exactly one place this route
      reads "what role does the caller hold", reused for both the
      pass/fail decision inside the guard and this finer-grained one.

      Refused with 409, not the generic 401 _role_guard() itself uses
      for an insufficient rank. That generic 401 exists so a caller
      who has not yet proven they hold ANY role learns nothing about
      what exists above them -- see _role_guard()'s own docstring. That
      reasoning does not transfer here: by this point the caller has
      already cleared the admin role guard (a real, TOTP-active admin
      account, not a probe), and has already named this exact target
      by its exact, currently-correct display_name -- the same
      confirmation this route has always required, checked above
      before this section ever runs, so a wrong guess never reaches
      this far. Nothing about which role the target's account holds is
      handed to an admin who does not already know precisely who they
      are naming. Trade-off accepted deliberately: an admin who
      deliberately probes player_ids they can already see via GET
      /api/admin/players (which lists every player's account_id
      linkage already) still learns one additional bit here -- that a
      specific, correctly-named target's account holds a role at all --
      that route does not expose. Given the caller already has to
      supply the target's exact current name to get this far, and the
      refusal message follows the same "say what happened and why"
      shape /api/admin/roles/grant's own 409s already use for their
      own conflicts (see admin_roles_grant() above), a 409 with a real
      explanation was judged more useful to a legitimate admin -- who
      otherwise has no way to tell "refused because of the roles
      boundary" from "player not found" from any other failure -- than
      a bare 401 would be. A 403 was considered and rejected: nothing
      about the caller's own eligibility is in question here (unlike
      _role_guard()'s own TOTP 403), it is the TARGET's state that
      blocks the action, which is exactly the shape admin_roles_grant()
      already answers with 409, not 403.

      Self-deletion: an operator naming their OWN player through this
      route is unaffected by the check above -- the refusal only fires
      when the CALLER's rank is "admin", and an operator's is not, so
      an operator deleting themselves this way is allowed, the same as
      every other role-holding target an operator may act on. This is
      deliberate, not an oversight: DELETE /api/account (the
      self-service route) already lets any signed-in account, role or
      no role, delete itself, so refusing it here would only add a
      second, inconsistent door onto the same already-permitted act.
      An admin naming their OWN player is, by contrast, refused by
      this same check -- their own account holds "admin", which is a
      role -- with no special case needed: an admin who wants to
      delete their own account already has the fitting door for that,
      DELETE /api/account, which re-authenticates them as themselves
      rather than routing it through the admin surface's confirm-by-
      name flow.

    ---- atomicity ----------------------------------------------------

    Runs inside one WriteSession (app/db.py) -- the same primitive
    DELETE /api/account uses, and the current-generation replacement
    for this file's older manual connect()/BEGIN IMMEDIATE pairing
    (see POST /api/admin/player/unlink-account's own comment on why it
    already made this switch). Every player-scoped delete, the
    tombstone UPDATE, and the account-scoped cascade (if any) commit
    together or not at all.
    """
    guard = await _role_guard(request, return_role=True)
    if isinstance(guard, JSONResponse):
        return guard
    session, caller_role = guard

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

        target_account_id = row["account_id"]

        # See this route's own docstring, "an admin cannot use this
        # route to reach a role-holder", for the full reasoning. Only
        # an admin (never an operator) is refused here, and only when
        # the target actually has a linked account holding a role --
        # an ordinary player, or a linked account with no role, is
        # unaffected.
        if caller_role == "admin" and target_account_id is not None:
            target_role = conn.execute(
                "SELECT role FROM account WHERE account_id = ?", (target_account_id,)
            ).fetchone()["role"]
            if target_role is not None:
                return JSONResponse(
                    {
                        "error": f"that account holds the {target_role} role — "
                        "an admin cannot delete an account that holds a role; "
                        "an operator can still do this"
                    },
                    status_code=409,
                )

        # See this module's "account deletion" section comment in
        # app/account_api.py for why each table below is here and why
        # `player` survives, tombstoned, instead of being deleted too.
        counts: dict[str, int] = {}
        for table in _PLAYER_SCOPED_TABLES:
            c = conn.execute(
                f"DELETE FROM {table} WHERE player_id = ?", (player_id,)
            )
            counts[table] = c.rowcount

        conn.execute(
            "UPDATE player SET display_name = ?, disabled_at = ?, account_id = NULL "
            "WHERE player_id = ?",
            (_tombstone_display_name(player_id), now, player_id),
        )

        if target_account_id is not None:
            for table in _ACCOUNT_SCOPED_TABLES:
                c = conn.execute(
                    f"DELETE FROM {table} WHERE account_id = ?", (target_account_id,)
                )
                counts[table] = c.rowcount
            conn.execute(
                "DELETE FROM account WHERE account_id = ?", (target_account_id,)
            )

        detail = f"player_id={player_id} ({display_name})"
        if target_account_id is not None:
            detail += f" account_id={target_account_id} (account also deleted)"
        _log_admin_action(
            conn, actor_account_id=session.account_id, action="player_delete",
            detail=detail, now=now,
        )

    # Same reasoning as revoke/disable: a deleted player's keys must stop
    # authenticating immediately, not once the auth cache TTL expires.
    ingestor = request.app.state.mc_ingestor
    ingestor.invalidate_player(player_id)

    log.info(
        "admin: deleted player %d (%s)%s: %s",
        player_id, display_name,
        f", also deleted account {target_account_id}" if target_account_id is not None else "",
        counts,
    )
    return {
        "deleted": True,
        "player_id": player_id,
        "display_name": display_name,
        "account_id": target_account_id,
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


def _admin_account_no_player_confirm(account_id: int) -> str:
    """The confirmation text POST /api/admin/account/delete below
    requires when its target account has no linked player to name --
    the account-shaped counterpart to app/account_api.py's own
    _NO_PLAYER_CONFIRM_PHRASE (DELETE /api/account's fixed fallback for
    the identical "nothing to name" situation, reached only ever
    against the caller's OWN account).

    Deliberately NOT that same fixed literal, and not a fixed literal
    at all: DELETE /api/account is safe with one constant phrase
    because a signed-in caller only ever has ONE account to delete --
    their own -- so there is nothing for the phrase to disambiguate
    between. This route is reached from a LIST (GET /api/admin/accounts
    above) that can show several orphaned accounts side by side, which
    is exactly the situation a fixed phrase is dangerous in: an
    operator working down that list could paste the same
    "DELETE MY ACCOUNT"-shaped literal against every row in turn
    without the text itself ever forcing them to look at which row
    they are confirming. Folding the target's own account_id into the
    required text closes that gap the same way the display_name check
    already does for a linked account -- producing a correct answer
    for THIS row requires having actually read THIS row -- at the
    small, deliberate cost of an operator typing an id instead of a
    fixed phrase for the no-player case specifically.
    """
    return f"DELETE ACCOUNT {account_id}"


@router.post("/api/admin/account/delete")
async def admin_account_delete(request: Request):
    """Permanently remove an ACCOUNT -- the account-shaped door onto
    the exact same act POST /api/admin/player/delete already performs
    from the player side. See that route's own docstring for the full
    "delete the person, keep the team" model and the table-by-table
    reasoning (_PLAYER_SCOPED_TABLES, _ACCOUNT_SCOPED_TABLES, and
    _tombstone_display_name() -- all imported from
    app/account_api.py, same as that route, not redefined here, so
    there remains exactly one definition of what "deletion" means in
    this codebase); only what genuinely differs from reaching this
    through a player_id is documented below.

    ---- why this route exists at all ----------------------------------

    Every admin/operator action before this one is PLAYER-shaped:
    every route lives under /api/admin/player/*, reached by naming a
    player_id. An account with no linked player -- released by POST
    /api/admin/player/unlink-account above, or simply never claimed by
    a finished join -- has no player_id to name, so it was reachable by
    NOTHING: not listed (see GET /api/admin/accounts above, its own fix
    for the same gap), not actionable, its sign-in identities and
    sessions left sitting there indefinitely. This route closes that
    door the same way the listing route opens it: by acting on
    `account` directly, never by way of `player`.

    ---- what is different from POST /api/admin/player/delete ----------

    - The target is `account_id`, not `player_id` -- the caller names
      the account, not a player. The target's LINKED player, if any
      (looked up here, never passed in), is tombstoned exactly the way
      player/delete tombstones its own target: every
      _PLAYER_SCOPED_TABLES row for it hard-deleted, then `player`
      itself overwritten via _tombstone_display_name() and disabled.
      An orphaned account simply has no player row to run that half
      against -- the account-scoped half below is the entire operation
      for it, same as DELETE /api/account's own "no linked player"
      case.
    - Confirmation is still typing something that proves the operator
      means THIS target specifically, not a generic "yes, delete" --
      the same purpose the display_name check already serves
      everywhere else in this file. When a player is linked, that is
      exactly the same check, unchanged: the player's CURRENT
      display_name, case-sensitive, no trimming. When the account is
      orphaned, there is no display_name to check, so the confirmation
      is instead "DELETE ACCOUNT <account_id>" -- see
      _admin_account_no_player_confirm() just above for why this is a
      per-target literal rather than reusing app/account_api.py's own
      fixed _NO_PLAYER_CONFIRM_PHRASE.

    ---- the same role guard, carried across ----------------------------

    Identical rule to POST /api/admin/player/delete's own "an admin
    cannot use this route to reach a role-holder" (see that route's
    docstring for the full reasoning, reused here verbatim, right down
    to the 409-not-401-not-403 shape): once the target account is
    resolved, if it holds a role (admin or operator) AND the caller's
    own rank is exactly "admin" (never "operator"), the deletion is
    refused before anything is written. This route is a second door
    onto the same role-holding accounts /api/admin/roles/* already
    keeps out of an admin's reach -- skipping this check here, on the
    theory that an account-shaped route is somehow a different act
    from a player-shaped one, would silently reopen exactly the
    escalation that guard exists to close, just reached by account_id
    instead of player_id. An operator may still delete a role-holding
    account through this route, the same as through player/delete.

    The caller's rank is read via `_role_guard(request,
    return_role=True)`, the same single read player/delete's own
    docstring explains at length -- reused here rather than a second,
    separate query against `account.role`.

    ---- atomicity -------------------------------------------------------

    One WriteSession, same primitive player/delete uses: every
    player-scoped delete (if a player is linked), the tombstone UPDATE
    (if any), every account-scoped delete, and the account row itself
    commit together in one COMMIT, or -- on any exception -- none of
    them happen at all.
    """
    guard = await _role_guard(request, return_role=True)
    if isinstance(guard, JSONResponse):
        return guard
    session, caller_role = guard

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    account_id = body.get("account_id") if isinstance(body, dict) else None
    confirm = body.get("display_name") if isinstance(body, dict) else None
    if not isinstance(account_id, int) or isinstance(account_id, bool):
        return JSONResponse({"error": "account_id is required"}, status_code=400)
    if not isinstance(confirm, str) or not confirm:
        return JSONResponse({"error": "display_name is required"}, status_code=400)

    now = int(time.time())
    async with WriteSession() as conn:
        account_row = conn.execute(
            "SELECT role FROM account WHERE account_id = ?", (account_id,)
        ).fetchone()
        if account_row is None:
            return JSONResponse({"error": "account not found"}, status_code=404)

        player_row = conn.execute(
            "SELECT player_id, display_name FROM player WHERE account_id = ?",
            (account_id,),
        ).fetchone()

        if player_row is not None:
            if confirm != player_row["display_name"]:
                return JSONResponse(
                    {"error": "display name does not match"}, status_code=409
                )
        else:
            expected = _admin_account_no_player_confirm(account_id)
            if confirm != expected:
                return JSONResponse(
                    {
                        "error": f'type "{expected}" in display_name to confirm -- '
                        "this account has no linked player to name"
                    },
                    status_code=409,
                )

        # See this route's own docstring, "the same role guard, carried
        # across", and POST /api/admin/player/delete's "an admin cannot
        # use this route to reach a role-holder" for the full
        # reasoning -- identical rule, just checked directly against
        # the target account this route was already given, rather than
        # one resolved from a player row first.
        if caller_role == "admin" and account_row["role"] is not None:
            return JSONResponse(
                {
                    "error": f"that account holds the {account_row['role']} role — "
                    "an admin cannot delete an account that holds a role; "
                    "an operator can still do this"
                },
                status_code=409,
            )

        # See app/account_api.py's "account deletion" section comment
        # for why each table below is here and why `player` survives,
        # tombstoned, instead of being deleted too.
        counts: dict[str, int] = {}
        player_id = player_row["player_id"] if player_row is not None else None
        display_name = player_row["display_name"] if player_row is not None else None

        if player_id is not None:
            for table in _PLAYER_SCOPED_TABLES:
                c = conn.execute(
                    f"DELETE FROM {table} WHERE player_id = ?", (player_id,)
                )
                counts[table] = c.rowcount
            conn.execute(
                "UPDATE player SET display_name = ?, disabled_at = ?, account_id = NULL "
                "WHERE player_id = ?",
                (_tombstone_display_name(player_id), now, player_id),
            )

        for table in _ACCOUNT_SCOPED_TABLES:
            c = conn.execute(
                f"DELETE FROM {table} WHERE account_id = ?", (account_id,)
            )
            counts[table] = c.rowcount
        conn.execute("DELETE FROM account WHERE account_id = ?", (account_id,))

        detail = f"account_id={account_id}"
        if player_id is not None:
            detail += f" player_id={player_id} ({display_name}, also tombstoned)"
        _log_admin_action(
            conn, actor_account_id=session.account_id, action="account_delete",
            detail=detail, now=now,
        )

    # Same reasoning as player/delete: a deleted account's player, if
    # any, may still hold a key cached as live at the ingest endpoint --
    # invalidate it immediately rather than waiting out the cache TTL.
    if player_id is not None:
        ingestor = request.app.state.mc_ingestor
        ingestor.invalidate_player(player_id)

    log.info(
        "admin: deleted account %d%s: %s",
        account_id,
        f" (also tombstoned player {player_id})" if player_id is not None else "",
        counts,
    )
    return {
        "deleted": True,
        "account_id": account_id,
        "player_id": player_id,
        "display_name": display_name,
        "counts": counts,
    }


# ---- account recovery: clear a credential, never set one ----------------
#
# The gap this section closes: before these three routes, the ONLY thing
# an operator could do for someone locked out of their own account was
# delete it (POST /api/admin/account/delete above) and tell them to start
# over. That is backwards -- deletion is for ending a person's presence
# in the game, not for helping them back in. These three routes are the
# actual recovery doors: disable-totp (lost phone, recovery codes gone --
# the genuine, unrecoverable-any-other-way lockout, and the most
# important of the three), password/clear (holder still has some OTHER
# way in and wants a fresh password), and identity/remove (a provider the
# holder has lost access to, or one linked in error).
#
# ---- the property that governs every route below: CLEAR, never SET -----
#
# An operator may only ever take a credential AWAY. Not one route here
# accepts a password, a TOTP secret, a recovery code, or any other
# credential VALUE in its request body -- each one only ever names WHICH
# credential to remove (an account_id, and for identity/remove, a
# provider string). This is not an incidental implementation detail, it
# is the entire security model of operator-assisted recovery:
#
#   An operator who could SET a password could sign in as the account
#   holder, silently, at any time, forever -- the exact shared-secret
#   failure mode this whole role/audit-log pass (see this module's own
#   docstring) replaced. An operator who can only CLEAR one hands the
#   door back to whoever can next prove they hold the account's OTHER
#   surviving credentials (see _role_guard() itself, and every existing
#   self-service route in app/account_api.py and app/totp_api.py) --
#   they restore access without ever being able to grant themselves
#   access. The same asymmetry a locksmith who can cut you a NEW key
#   would be a very different, much more dangerous kind of trusted party
#   than one who can only drill out a jammed lock and hand it back empty.
#
# If a future route here ever needs to accept `new_password`,
# `totp_secret`, or anything shaped like a credential value: stop. That
# is the fence this section exists to keep intact, not a gap to fill in.
#
# ---- the role guard matters MORE here than on delete --------------------
#
# Every route below carries the identical "an admin cannot act on an
# account holding a role" guard POST /api/admin/player/delete and POST
# /api/admin/account/delete already enforce (see player/delete's own
# docstring, "an admin cannot use this route to reach a role-holder", for
# the full reasoning -- reused verbatim here). It matters MORE on these
# three than on delete: an admin who could disable an operator's
# two-factor authentication would not need to delete anything to
# escalate -- they would just have turned the operator's account into a
# single-factor one, then gone back through whatever door survives
# (a password, a recovery email) to sign in AS them, with every
# operator-only power that account holds. Clearing a credential is a
# strictly smaller act than deleting an account, but "smaller" is not
# "safer" when the credential being cleared is someone else's second
# factor -- so this guard is not weakened or skipped for any of the
# three, even though none of them destroys anything the way delete does.
#
# ---- typed confirmation, on all three ------------------------------------
#
# Same shape POST /api/admin/account/delete already uses (see that
# route's own docstring and _admin_account_no_player_confirm() above):
# the target's linked player's CURRENT display_name when one exists, or
# an account_id-bound phrase (_admin_account_recovery_confirm() below)
# when the account is orphaned. All three warrant it, not just some:
# every one of them acts on someone ELSE's account from a list that can
# show many rows side by side (GET /api/admin/accounts), same shape that
# makes account/delete's own confirmation necessary, and every one of
# them takes something away that the account holder cannot simply put
# back themselves (a cleared password cannot be un-cleared back to what
# it was, a disabled second factor is not still there to re-check, a
# removed identity is not still linked) -- the same "stale or mistyped
# target must not be able to do this to the wrong person" reasoning
# node/remove, reissue, unlink-account, player/delete, and account/delete
# already apply to every route in this file that acts on someone else's
# behalf. The one thing NOT required is proof of the CALLER's own
# credentials the way DELETE /api/account or POST /api/account/totp
# re-authenticate the person acting on their OWN account -- see
# player/delete's own docstring for why: _role_guard() on the operator's
# account already covers "is this really the operator", and there is no
# equivalent credential of the TARGET's an operator acting on someone
# else's account has any reason to hold.


def _admin_account_recovery_confirm(account_id: int, action_phrase: str) -> str:
    """The orphan-account confirmation phrase for one of the three
    recovery routes below -- the same purpose
    _admin_account_no_player_confirm() (above, for account/delete)
    serves, generalized to a caller-supplied action phrase instead of
    always saying "DELETE". Not a rename of that function nor a shared
    helper with it: account/delete's own phrase is deliberately frozen
    (changing it would break any operator tooling or muscle memory
    already built around "DELETE ACCOUNT <id>"), and each of these three
    routes needs its OWN verb so an operator confirming "disable
    two-factor" is never looking at text that reads like a password
    clear or a delete. See that function's own docstring for why the
    account_id is folded into the phrase at all: a fixed literal would
    let a phrase copied for one orphan row get pasted onto another
    without the text itself ever forcing a look at which row is being
    confirmed.
    """
    return f"{action_phrase} {account_id}"


def _resolve_recovery_target(
    conn, *, account_id: object, confirm: object, caller_role: str,
    action_phrase: str, guard_verb: str,
) -> tuple[int | None, str | None, JSONResponse | None]:
    """Shared target-resolution for all three recovery routes below:
    validates `account_id`/`confirm` shape, loads the target account,
    checks the typed-confirmation guard (display_name when a player is
    linked, `_admin_account_recovery_confirm()` when the account is
    orphaned), and enforces the same "an admin cannot reach a
    role-holder" boundary POST /api/admin/player/delete and POST
    /api/admin/account/delete already carry (see this section's own
    comment above, "the role guard matters MORE here than on delete").

    Pulled out once here rather than copied three times: all three
    routes below share this exact sequence of checks before they ever
    reach their own action-specific logic (does TOTP exist to disable,
    does a password exist to clear, is this provider linked to
    remove) -- three copies of the same four checks would be three
    places for the role-guard wording or the confirmation logic to
    drift out of sync with each other, which is exactly the risk this
    module's docstring already warns against for a role check this
    security-sensitive.

    Returns (account_id, target_role, None) on success -- target_role
    is `account.role` for the resolved account, handed back so a
    caller that wants to log or reason about it does not read the row
    a second time. Returns (None, None, JSONResponse(...)) on any
    failure, the response to hand back as-is.

    `action_phrase` (e.g. "DISABLE TWO-FACTOR") is passed straight
    through to `_admin_account_recovery_confirm()` for the orphan
    confirmation text. `guard_verb` is a separate, already-grammatical
    verb phrase for the role-guard 409's own message (e.g. "disable
    two-factor authentication on", "clear the password on", "remove a
    sign-in method from") -- kept apart from `action_phrase` rather
    than deriving one from the other, because a shouted confirmation
    literal ("CLEAR PASSWORD") and a sentence fragment that has to read
    correctly inside "an admin cannot ___ an account that holds a
    role" are two different pieces of text with two different jobs;
    mechanically lowercasing the former for the latter produced
    grammatically broken messages for two of the three routes below
    (missing "the"/"a" and the trailing preposition) before this
    parameter was split out.
    """
    if not isinstance(account_id, int) or isinstance(account_id, bool):
        return None, None, JSONResponse({"error": "account_id is required"}, status_code=400)
    if not isinstance(confirm, str) or not confirm:
        return None, None, JSONResponse({"error": "display_name is required"}, status_code=400)

    account_row = conn.execute(
        "SELECT role FROM account WHERE account_id = ?", (account_id,)
    ).fetchone()
    if account_row is None:
        return None, None, JSONResponse({"error": "account not found"}, status_code=404)

    player_row = conn.execute(
        "SELECT display_name FROM player WHERE account_id = ?", (account_id,)
    ).fetchone()
    if player_row is not None:
        if confirm != player_row["display_name"]:
            return None, None, JSONResponse(
                {"error": "display name does not match"}, status_code=409
            )
    else:
        expected = _admin_account_recovery_confirm(account_id, action_phrase)
        if confirm != expected:
            return None, None, JSONResponse(
                {
                    "error": f'type "{expected}" in display_name to confirm — '
                    "this account has no linked player to name"
                },
                status_code=409,
            )

    # See this section's own "the role guard matters MORE here than on
    # delete" comment, and POST /api/admin/player/delete's "an admin
    # cannot use this route to reach a role-holder" docstring for the
    # full reasoning -- identical rule, worded for whichever recovery
    # action is calling in.
    if caller_role == "admin" and account_row["role"] is not None:
        return None, None, JSONResponse(
            {
                "error": f"that account holds the {account_row['role']} role — "
                f"an admin cannot {guard_verb} an account that holds a role; "
                "an operator can still do this"
            },
            status_code=409,
        )

    return account_id, account_row["role"], None


@router.post("/api/admin/account/disable-totp")
async def admin_account_disable_totp(request: Request):
    """Turn off two-factor authentication on someone ELSE's account --
    the recovery door for the genuine lockout case (lost phone,
    recovery codes gone, nothing else gets back in) and the most
    important of the three routes in this section: see this section's
    own module-level comment above for why deletion was never the
    right answer to this, and for the clear-never-set property this
    route (like the other two) is built around.

    Reuses app/totp_api.py's own DELETE /api/account/totp deletion
    logic -- both account_totp and every account_totp_recovery_code
    row for the account are removed outright, same as that route (see
    its own docstring for why: turning TOTP off makes the secret and
    every remaining recovery code moot at once, nothing is worth
    keeping). Duplicated here rather than imported: that route's
    deletion logic is inline in an HTTP handler, not a standalone
    function, so there is nothing to import -- the same "duplicated
    rather than imported" reasoning this file already gives for
    _validate_team, _VALID_PROTOCOLS, and _player_radios above.

    Deliberately does NOT require the target's current TOTP code or a
    recovery code the way DELETE /api/account/totp requires from the
    account holder themselves -- requiring either would defeat the
    entire point of this route: an account holder who could still
    produce a live code or an unused recovery code would not need an
    operator's help, they would just call that route themselves. The
    _role_guard() check on the OPERATOR's own account (real,
    TOTP-active, role-holding) plus the typed confirmation below are
    what stand in place of that proof here -- the same substitution
    every other operator-acting-on-someone-else's-behalf route in this
    file already makes (see POST /api/admin/player/delete's own
    docstring on why re-authenticating the CALLER is what covers this,
    not a credential of the TARGET's).

    ---- last-door: does not apply -------------------------------------

    TOTP is a GATE in front of an account's doors (see
    app/totp_api.py's own module docstring, "which doors this
    guards"), not a door itself -- an account cannot be signed into
    with a TOTP code alone, only a password or magic-link sign-in that
    TOTP then challenges. Disabling it never reduces how many doors an
    account has; it only removes a lock sitting in front of whichever
    doors already exist. There is nothing here for a last-door
    refusal to protect against, unlike the two routes below.
    """
    guard = await _role_guard(request, return_role=True)
    if isinstance(guard, JSONResponse):
        return guard
    session, caller_role = guard

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    raw_account_id = body.get("account_id") if isinstance(body, dict) else None
    confirm = body.get("display_name") if isinstance(body, dict) else None

    now = int(time.time())
    async with WriteSession() as conn:
        account_id, _target_role, err = _resolve_recovery_target(
            conn, account_id=raw_account_id, confirm=confirm, caller_role=caller_role,
            action_phrase="DISABLE TWO-FACTOR",
            guard_verb="disable two-factor authentication on",
        )
        if err is not None:
            return err

        totp_row = conn.execute(
            "SELECT 1 FROM account_totp WHERE account_id = ? AND activated_at IS NOT NULL",
            (account_id,),
        ).fetchone()
        if totp_row is None:
            return JSONResponse(
                {"error": "two-factor authentication is not enabled on this account"},
                status_code=404,
            )

        recovery_codes_cleared = conn.execute(
            "DELETE FROM account_totp_recovery_code WHERE account_id = ?", (account_id,)
        ).rowcount
        conn.execute("DELETE FROM account_totp WHERE account_id = ?", (account_id,))
        conn.execute(
            "INSERT INTO account_link_event(account_id, kind, detail, actor, created_at) "
            "VALUES (?, 'totp_disabled', 'operator recovery', 'operator', ?)",
            (account_id, now),
        )
        _log_admin_action(
            conn, actor_account_id=session.account_id, action="account_disable_totp",
            detail=f"account_id={account_id} recovery_codes_cleared={recovery_codes_cleared}",
            now=now,
        )

    log.info(
        "admin: disabled two-factor authentication on account %d (%d recovery code(s) cleared)",
        account_id, recovery_codes_cleared,
    )
    return {"ok": True, "account_id": account_id, "recovery_codes_cleared": recovery_codes_cleared}


@router.post("/api/admin/account/password/clear")
async def admin_account_clear_password(request: Request):
    """Remove the password on someone ELSE's account -- so the holder
    can set a fresh one themselves the next time they sign in through
    some OTHER door (see this section's own module-level comment).
    This does not, by itself, sign anyone in: it only clears
    account_password, the exact same row DELETE /api/account/password
    removes for the caller's own account (see that route's own
    docstring). Once cleared, POST /api/account/password's own "set
    the FIRST password" branch applies the next time the account holder
    is signed in (through whichever door still works) and visits their
    account page -- no current_password is asked for, because there is
    none any more.

    ---- last-door: KEPT, same guard DELETE /api/account/password uses -

    _door_counts() decides this the identical way that route already
    does: refuse if removing the password would leave the account with
    zero doors. This is NOT a case for inverting the self-service
    guard the way the prompt asks each route to consider on its own
    merits -- clearing a password does not, by itself, hand the
    account holder anything to sign in WITH; the whole recovery
    depends on some OTHER door already working, which is exactly what
    this guard is checking for. An account whose ONLY door is its
    password cannot be helped by this route at all: clearing that
    password would not restore access (nothing here can ever SET a new
    one -- see this section's own "clear, never set" comment), it
    would only trade "locked out, but the row still exists" for
    "permanently unreachable by anyone, including a future operator,
    since nothing in this recovery surface can ever add a door back."
    That is worse than doing nothing, so it stays refused -- the
    self-service guard's protection and this route's own reasoning for
    keeping it happen to reach the same number, but for different
    reasons: self-service refuses to protect the CALLER from locking
    themselves out by mistake; this route refuses because there is
    nothing on the other side of the door being closed for the
    operator to hand back.
    """
    guard = await _role_guard(request, return_role=True)
    if isinstance(guard, JSONResponse):
        return guard
    session, caller_role = guard

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    raw_account_id = body.get("account_id") if isinstance(body, dict) else None
    confirm = body.get("display_name") if isinstance(body, dict) else None

    now = int(time.time())
    async with WriteSession() as conn:
        account_id, _target_role, err = _resolve_recovery_target(
            conn, account_id=raw_account_id, confirm=confirm, caller_role=caller_role,
            action_phrase="CLEAR PASSWORD",
            guard_verb="clear the password on",
        )
        if err is not None:
            return err

        if not _has_password(conn, account_id):
            return JSONResponse({"error": "no password is set on this account"}, status_code=404)

        per_provider, _ = _door_counts(conn, account_id)
        remaining = sum(per_provider.values())  # the password itself is the door being removed
        if remaining < 1:
            return JSONResponse(
                {
                    "error": "clearing this password would leave the account with no way "
                    "to sign in, and nothing here can set a new one — see this "
                    "surface's own clear-never-set rule"
                },
                status_code=409,
            )

        conn.execute("DELETE FROM account_password WHERE account_id = ?", (account_id,))
        conn.execute(
            "INSERT INTO account_link_event(account_id, kind, detail, actor, created_at) "
            "VALUES (?, 'password_removed', 'operator recovery', 'operator', ?)",
            (account_id, now),
        )
        _log_admin_action(
            conn, actor_account_id=session.account_id, action="account_clear_password",
            detail=f"account_id={account_id} remaining_doors={remaining}", now=now,
        )

    log.info("admin: cleared password on account %d (%d door(s) remain)", account_id, remaining)
    return {"ok": True, "account_id": account_id, "remaining_doors": remaining}


@router.post("/api/admin/account/identity/remove")
async def admin_account_remove_identity(request: Request):
    """Disconnect one sign-in provider from someone ELSE's account --
    for a provider the holder has lost access to (an old Google account
    that no longer exists), or one linked in error (a misclick, a
    shared device signed into the wrong account). Reuses the exact
    removal DELETE /api/account/identity/{provider} performs for the
    caller's own account (see that route's own docstring): every
    account_identity row for (account_id, provider) at once, never a
    single (provider, subject) row -- "disconnect google" is the only
    granularity this data model supports, same reasoning there.

    `provider` is read from the request body (not a path parameter,
    unlike the self-service route) -- every other target-identifying
    field in this file's admin routes lives in the JSON body, not the
    URL, and this route follows that rather than being the one
    exception.

    ---- last-door: KEPT, same guard DELETE /api/account/identity uses -

    Same reasoning as POST /api/admin/account/password/clear just
    above, applied to an identity instead of a password: an identity
    IS a door (unlike TOTP), so removing the account's LAST one would
    leave it with zero ways to ever sign in again, and nothing in this
    recovery surface can add a replacement door -- only clear existing
    ones. Refusing that is not the self-service guard copied out of
    caution, it is the only choice that leaves the account recoverable
    by a LATER action (linking a fresh provider once signed in through
    whatever door remains) instead of permanently orphaning it.
    """
    guard = await _role_guard(request, return_role=True)
    if isinstance(guard, JSONResponse):
        return guard
    session, caller_role = guard

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    raw_account_id = body.get("account_id") if isinstance(body, dict) else None
    confirm = body.get("display_name") if isinstance(body, dict) else None
    provider = body.get("provider") if isinstance(body, dict) else None
    if not isinstance(provider, str) or not provider:
        return JSONResponse({"error": "provider is required"}, status_code=400)

    now = int(time.time())
    async with WriteSession() as conn:
        account_id, _target_role, err = _resolve_recovery_target(
            conn, account_id=raw_account_id, confirm=confirm, caller_role=caller_role,
            action_phrase="REMOVE SIGN-IN",
            guard_verb="remove a sign-in method from",
        )
        if err is not None:
            return err

        per_provider, has_password = _door_counts(conn, account_id)
        removing = per_provider.get(provider, 0)
        if removing == 0:
            return JSONResponse(
                {"error": "that provider is not linked to this account"}, status_code=404
            )

        total_doors = sum(per_provider.values()) + (1 if has_password else 0)
        remaining = total_doors - removing
        if remaining < 1:
            return JSONResponse(
                {
                    "error": "removing this would leave the account with no way to sign "
                    "in, and nothing here can add a replacement — see this surface's "
                    "own clear-never-set rule"
                },
                status_code=409,
            )

        conn.execute(
            "DELETE FROM account_identity WHERE account_id = ? AND provider = ?",
            (account_id, provider),
        )
        conn.execute(
            "INSERT INTO account_link_event(account_id, kind, detail, actor, created_at) "
            "VALUES (?, 'identity_unlinked', ?, 'operator', ?)",
            (account_id, f"provider={provider}", now),
        )
        _log_admin_action(
            conn, actor_account_id=session.account_id, action="account_remove_identity",
            detail=f"account_id={account_id} provider={provider} remaining_doors={remaining}",
            now=now,
        )

    log.info(
        "admin: removed %s identity from account %d (%d door(s) remain)",
        provider, account_id, remaining,
    )
    return {"ok": True, "account_id": account_id, "provider": provider, "remaining_doors": remaining}


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
