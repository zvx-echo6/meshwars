"""FastAPI router for the account layer: who am I, linking an existing
player's API key to an account, and session logout.

Every route here requires a session cookie (app/sessions.py's
require_session() dependency) -- there is no key-authenticated route in
this module, and no OAuth provider callback either; both are separate,
later work. What exists today is what a session, once it exists, can
DO: read its own account (GET /api/account), retrofit an existing
key-only player onto it (POST /api/account/link-key), and log out (POST
/api/account/logout[-all]). See app/sessions.py's own module docstring
for how a session comes to exist in the first place -- nothing in this
router creates one.

---- CSRF -----------------------------------------------------------

Every route below is state-changing except GET /api/account, and every
one of them is authenticated by a cookie a browser attaches
automatically -- the classic CSRF shape: a page on another origin could
try to make a logged-in visitor's browser submit a request here on
their behalf. This was considered, not left implicit, and the
conclusion is that the existing cookie/CORS setup already closes it,
so no CSRF token is added:

1. The session cookie is set SameSite=Lax (app/sessions.py's
   set_session_cookie). Per the SameSite spec, a Lax cookie is
   attached to a cross-site request only for a top-level navigation
   using a "safe" method (GET/HEAD/etc.) -- a cross-site POST is
   EXCLUDED from that allowance, whether it originates from a plain
   HTML <form method="post"> submission or from script-driven
   fetch/XHR. (The "Lax+POST" two-minute grace period some browsers
   apply is a compatibility shim for cookies that never specified
   SameSite at all, defaulting to Lax implicitly -- it does not apply
   here, since this cookie sets SameSite=Lax explicitly.) Every route
   in this module that changes anything is POST-only, so this alone
   already stops a cross-site attacker's request from ever carrying a
   valid session cookie in a modern browser.

2. app/main.py's CORSMiddleware is configured allow_methods=["GET",
   "HEAD"], allow_credentials=False, for the whole app (it exists for
   app/public_api.py's cross-origin read routes, not for this one).
   Any cross-origin POST that isn't a CORS-exempt "simple request"
   (e.g. a fetch with Content-Type: application/json, which
   link-key's body requires) triggers a CORS preflight OPTIONS first;
   since POST is not in allow_methods, the browser refuses to send the
   real request at all. This is defense in depth on top of (1), not
   the primary control -- a "simple" cross-site form POST (allowed
   Content-Type, no custom headers) never triggers a preflight and
   would reach the server if the cookie were attached, which is
   exactly why (1) -- not CORS -- is what has to hold on its own.

Both of these are properties of the cookie and the app-wide CORS
policy, not of this router specifically, so there is nothing here
guarding these routes beyond require_session() itself -- guarding here
too would be redundant with (1)/(2), not additional protection.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from . import mc_api, results
from .auth import new_rate_limit_bucket
from .client_ip import get_client_ip
from .config import settings
from .db import WriteSession, connect
# Reaching into app/checkin.py's underscore-prefixed helpers here is
# deliberate, not a style slip: the four read-only routes at the bottom
# of this module (my stats/honors/checkins/checkin-health additions)
# exist specifically to surface what those functions already compute
# for a player, without re-deriving any of it -- checkin_streak() for a
# player's current streak, and _build_directory_bridge() /
# _resolve_mc_identities() / mc_contact_status() for exactly the
# MeshCore identity resolution a real check-in goes through, so "why
# isn't this crediting me" can never drift from what actually happens
# at award time.
# mc_contact_status() and its own index builder are the one piece of
# genuinely NEW logic in app/checkin.py this pass adds -- they exist so
# a single bound contact's resolution outcome can be reported instead
# of only the bridge's final winners; see that function's own docstring.
#
# This is deliberately NOT imported here at module level. app/checkin.py
# pulls in the full ingest/meshview/MQTT chain (app/meshview_client.py
# -> aiolimiter, httpx, etc) to do its own job, and this is a light,
# session-scoped HTTP router that FastAPI's app wiring imports eagerly
# on every process start -- it should not drag that whole chain in just
# to have the names available. Each function below that needs one of
# these helpers imports it locally, right where it is used.
from .mc_ingest import PROTOCOL as MC_PROTOCOL
from .sessions import (
    SessionPrincipal,
    clear_session_cookie,
    require_session,
    revoke_all_sessions,
    revoke_session,
)

router = APIRouter()

# 'mt' is exactly as fixed a value as MC_PROTOCOL stands in for 'mc' --
# see app/mc_api.py's own MT_PROTOCOL and app/admin_ops.py's identical
# local constant for why this is a bare literal rather than an import:
# app/ingest.py (the module that would otherwise export it) imports
# app/api.py, which imports app/mc_api.py, so importing it from here
# risks the same cycle those two modules already avoid.
MT_PROTOCOL = "mt"

# How far back a "recent unresolved sender name" is still worth
# surfacing to a player -- see account_checkin_health()'s own docstring
# for why these can only ever be shown as "may or may not be yours."
# Same order of magnitude as app/admin_ops.py's _STALE_DAYS, for the
# same reason: a name from months ago is noise, not a diagnosis.
_UNRESOLVED_LOOKBACK_DAYS = 14

# Address-keyed rate limit on link-key -- see
# settings.account_link_key_rate_limit_attempts/window_seconds' own
# comment in app/config.py for why this endpoint needs one at all (it's
# a key-guessing oracle without it). This module's own instance, per
# app/auth.py's module-docstring convention: every _BoundedHits budget
# in this codebase is private to the one call site that owns it, never
# shared across modules.
_link_key_addr_limiter = new_rate_limit_bucket()


# ---- read helpers -----------------------------------------------------

def _mask_email(email: str | None) -> str | None:
    """'jdoe@example.com' -> 'j***@example.com'. Never expose a linked
    identity's full address back through the API it was supplied to --
    the account holder already knows their own email, this view exists
    so they can tell WHICH identity is which (a Google login from a
    GitHub one) without every response leaking the raw address to
    anything that can read a session cookie (a browser extension, a
    proxy log, a screen someone is sharing).
    """
    if not email or "@" not in email:
        return None
    local, _, domain = email.partition("@")
    masked_local = local[0] + "***" if local else "***"
    return f"{masked_local}@{domain}"


def _identities_out(conn, account_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT provider, email, linked_at, last_login_at "
        "  FROM account_identity WHERE account_id = ? ORDER BY linked_at",
        (account_id,),
    ).fetchall()
    return [
        {
            "provider": r["provider"],
            "email": _mask_email(r["email"]),
            "linked_at": r["linked_at"],
            "last_login_at": r["last_login_at"],
        }
        for r in rows
    ]


def _player_out(conn, player_id: int) -> dict | None:
    row = conn.execute(
        "SELECT player_id, display_name, team FROM player WHERE player_id = ?",
        (player_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "player_id": row["player_id"],
        "display_name": row["display_name"],
        "team": row["team"],
    }


def _sessions_out(conn, account_id: int, *, current_token_hash: str) -> list[dict]:
    """Active (not revoked, not expired) sessions on this account --
    never returns token_hash itself, only enough for a person to
    recognise which of their own sessions is which (see
    account_session's own comment in app/db.py for why user_agent/ip
    exist at all: recognition, not a security control).
    """
    now = int(time.time())
    rows = conn.execute(
        "SELECT token_hash, created_at, last_seen_at, user_agent, ip "
        "  FROM account_session "
        " WHERE account_id = ? AND revoked_at IS NULL AND expires_at > ? "
        " ORDER BY last_seen_at DESC",
        (account_id, now),
    ).fetchall()
    return [
        {
            "created_at": r["created_at"],
            "last_seen_at": r["last_seen_at"],
            "user_agent": r["user_agent"],
            "ip": r["ip"],
            "current": r["token_hash"] == current_token_hash,
        }
        for r in rows
    ]


# ---- routes ---------------------------------------------------------------

@router.get("/api/account")
async def get_account(session: SessionPrincipal = Depends(require_session)) -> JSONResponse:
    conn = connect()
    try:
        identities = _identities_out(conn, session.account_id)
        player = _player_out(conn, session.player_id) if session.player_id is not None else None
        sessions_out = _sessions_out(conn, session.account_id, current_token_hash=session.token_hash)
    finally:
        conn.close()

    return JSONResponse(
        {
            "account_id": session.account_id,
            "identities": identities,
            "player": player,
            "sessions": sessions_out,
        },
        status_code=200,
    )


@router.post("/api/account/link-key")
async def link_key(
    request: Request, session: SessionPrincipal = Depends(require_session)
) -> JSONResponse:
    """Retrofit flow: an already-logged-in account posts an existing
    player's API key to claim that player. Authenticates the key
    through the exact same request.app.state.mc_ingestor.authenticate()
    path every key-authenticated route already uses (app/auth.py), so
    this endpoint can never treat a key as valid that the rest of the
    app would reject, or vice versa.

    Refused with a distinct, specific error in each of two conflict
    cases -- never a generic "can't link" -- so a real person stuck
    here (most likely: they meant to use a different account, or
    someone else already claimed their key) can actually tell what
    happened:
      - this account already has a linked player (one account, one
        player, at most -- see app/db.py's player.account_id and its
        UNIQUE index)
      - that key's player already belongs to a DIFFERENT account
    """
    ip = get_client_ip(request)
    if _link_key_addr_limiter.limited(
        ip,
        limit=settings.account_link_key_rate_limit_attempts,
        window=settings.account_link_key_rate_limit_window_seconds,
    ):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad request"}, status_code=400)

    raw_key = body.get("api_key")
    if not isinstance(raw_key, str) or not raw_key:
        return JSONResponse({"error": "api_key is required"}, status_code=400)

    ingestor = request.app.state.mc_ingestor
    auth = await ingestor.authenticate(raw_key)
    if auth.status in ("not_found", "revoked"):
        # Same generic 401 every other key-authenticated route in this
        # app returns for both statuses -- see app/auth.py's own
        # comment on why not_found/revoked must stay indistinguishable
        # from the response alone.
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if auth.status == "disabled":
        return JSONResponse({"error": "forbidden"}, status_code=403)

    player_id = auth.player_id
    now = int(time.time())

    # Both conflict checks and the write happen inside ONE WriteSession
    # -- that context manager's BEGIN IMMEDIATE (app/db.py) holds the
    # single global write lock for the whole block, so there is no
    # window between "checked, looked clear" and "wrote it" for a
    # second concurrent link-key call (same account, or targeting the
    # same player) to race into. A `return` from inside the block still
    # runs __aexit__ and commits -- harmless here, since the only
    # statements that ran before a conflict is detected are the two
    # read-only SELECTs below.
    async with WriteSession() as conn:
        existing = conn.execute(
            "SELECT player_id FROM player WHERE account_id = ?", (session.account_id,)
        ).fetchone()
        if existing is not None:
            return JSONResponse(
                {"error": "this account already has a linked player"}, status_code=409
            )

        owner = conn.execute(
            "SELECT account_id FROM player WHERE player_id = ?", (player_id,)
        ).fetchone()
        if owner is not None and owner["account_id"] is not None:
            return JSONResponse(
                {"error": "that key's player is already linked to a different account"},
                status_code=409,
            )

        conn.execute(
            "UPDATE player SET account_id = ? WHERE player_id = ?",
            (session.account_id, player_id),
        )
        conn.execute(
            "INSERT INTO account_link_event(account_id, kind, detail, actor, created_at) "
            "VALUES (?, 'player_linked', ?, 'user', ?)",
            (session.account_id, f"player_id={player_id}", now),
        )

    conn = connect()
    try:
        player = _player_out(conn, player_id)
    finally:
        conn.close()
    return JSONResponse({"player": player}, status_code=200)


@router.post("/api/account/logout")
async def logout(session: SessionPrincipal = Depends(require_session)) -> JSONResponse:
    await revoke_session(session.token_hash)
    response = JSONResponse({"ok": True}, status_code=200)
    clear_session_cookie(response)
    return response


@router.post("/api/account/logout-all")
async def logout_all(session: SessionPrincipal = Depends(require_session)) -> JSONResponse:
    await revoke_all_sessions(session.account_id)
    response = JSONResponse({"ok": True}, status_code=200)
    clear_session_cookie(response)
    return response


# ---- player-facing data: stats / honors / checkins / checkin-health ------
#
# Everything below is scoped to the signed-in account's OWN linked
# player -- session.player_id, resolved fresh on every request by
# require_session() (see app/sessions.py). None of these routes accept
# a player_id or a name; there is no way to ask this router about
# anyone but yourself. A session with no linked player yet (the account
# exists, POST /api/account/link-key was never called) gets the same
# 404 from all four, rather than an empty-shaped 200 that would read as
# "you have zero of everything" instead of "you have no player."

def _no_linked_player_error() -> JSONResponse:
    return JSONResponse({"error": "no linked player"}, status_code=404)


@router.get("/api/account/stats")
async def account_stats(session: SessionPrincipal = Depends(require_session)) -> JSONResponse:
    """My current standing, per board.

    app/mc_api.py's find_for() already answers "what does this player
    hold and score right now" for anyone, by name -- it is the same
    query GET /api/mc/find runs for a public lookup. This route calls
    it directly (once per protocol the player's single player row could
    be active on) rather than copying its SQL, and adds only the two
    things find_for() cannot give a player about themselves today:

    - checkin_streak: currently only visible via app/mc_api.py's
      top_checkin_for() (GET /api/mc/top-checkins), and only for the
      top 20. Computed here with checkin.checkin_streak() itself,
      seeded from the player's own most recent net_date in
      mc_checkin_award for that protocol -- the same call the poller
      makes when it writes a new award row, so this is never a second,
      possibly-drifting streak implementation, and it does not depend
      on mc_checkin_award.streak (null on any row written before that
      column existed).
    - nets_checked_in: a plain COUNT over mc_checkin_award, which no
      existing endpoint exposes at all.

    Per-protocol, not merged: mc_checkin_award, mc_tile, and
    place_activation are all keyed (or windowed) by protocol, and
    find_for()'s own docstring is explicit that one player row can hold
    both an 'mc' and an 'mt' radio. A protocol with no active season and
    no history for this player still gets an entry (find_for() degrades
    to zeros rather than omitting it), so "boards" always lists both --
    a player who has only ever played one board sees the other as an
    honest zero, not a missing key to special-case in a client.
    """
    if session.player_id is None:
        return _no_linked_player_error()

    from .checkin import checkin_streak

    conn = connect()
    try:
        player = conn.execute(
            "SELECT display_name, team FROM player WHERE player_id = ?",
            (session.player_id,),
        ).fetchone()
        if player is None:
            return _no_linked_player_error()
        name = player["display_name"]
        team = player["team"]

        boards: dict[str, dict] = {}
        for protocol in (MC_PROTOCOL, MT_PROTOCOL):
            board = mc_api.find_for(protocol, name)   # its own connection -- see module import comment
            if board is None:
                continue
            row = conn.execute(
                "SELECT COUNT(*), MAX(net_date) FROM mc_checkin_award "
                " WHERE protocol = ? AND player_id = ?",
                (protocol, session.player_id),
            ).fetchone()
            nets_checked_in = row[0] or 0
            latest_net_date = row[1]
            # No checked-in nets on this protocol at all: nothing for
            # checkin_streak() to walk backward from, and "0" is the
            # honest answer, not "1" (which is what passing a made-up
            # net_date would produce -- see that function's own
            # docstring on why it always returns at least 1 once called).
            streak = (
                checkin_streak(conn, session.player_id, protocol, latest_net_date)
                if latest_net_date else 0
            )
            board["nets_checked_in"] = nets_checked_in
            board["checkin_streak"] = streak
            boards[protocol] = board
    finally:
        conn.close()

    return JSONResponse(
        {"display_name": name, "team": team, "boards": boards}, status_code=200
    )


@router.get("/api/account/honors")
async def account_honors(session: SessionPrincipal = Depends(require_session)) -> JSONResponse:
    """My past honors: this player's own rows out of month_award
    (app/db.py:954), across every FINISHED month, newest first.

    Read-only in the strongest sense -- a frozen month is immutable
    history by design (see app/results.py:19-25's own docstring), and
    nothing here recomputes or rewrites a single figure; this is a
    plain SELECT with a player_id filter that app/results.py's own
    site-wide /results endpoint (month_results_for()) does not offer.

    month_award.player_id is NULL for a team-scoped award (Largest
    Territory, Longest Road) -- filtering on it already leaves only
    this player's own honors (Empire Builder, Top Attacker, Tourist,
    and so on), never a team's, with no separate award-type check
    needed. `label` is the same results.AWARD_LABELS lookup
    month_results_for() applies, so a retired award a player won while
    it still existed (see AWARD_LABELS' own comments on
    most_consistent/top_netop/explorer/month_winner) still reads with
    its real name instead of a raw key.
    """
    if session.player_id is None:
        return _no_linked_player_error()

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT month, protocol, award, scope, value, detail FROM month_award "
            " WHERE player_id = ? ORDER BY month DESC, protocol, award",
            (session.player_id,),
        ).fetchall()
    finally:
        conn.close()

    honors = [
        {
            "month": r["month"],
            "protocol": r["protocol"],
            "award": r["award"],
            "label": results.AWARD_LABELS.get(r["award"], r["award"]),
            "scope": r["scope"],
            "value": r["value"],
            "detail": r["detail"],
        }
        for r in rows
    ]
    return JSONResponse({"honors": honors}, status_code=200)


@router.get("/api/account/checkins")
async def account_checkins(
    session: SessionPrincipal = Depends(require_session),
    limit: int = Query(50, ge=1, le=200),
) -> JSONResponse:
    """My check-in history: this player's own credited rows out of
    mc_checkin_award, newest net first. No existing endpoint lists a
    single player's check-ins today -- app/mc_api.py's top_checkin_for()
    (GET /api/mc/top-checkins) only ranks the top 20 players' current
    point TOTAL, never any one player's row-by-row history.

    `streak` on each row is the value that was actually stored on it at
    award time (null for anything written before the streak column
    existed -- see mc_checkin_award's own comment in app/db.py), so
    reading this list top to bottom is watching the same streak
    GET /api/account/stats reports get built one net at a time.
    """
    if session.player_id is None:
        return _no_linked_player_error()

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT season_id, net_date, points, protocol, streak, awarded_at, message_ts "
            "  FROM mc_checkin_award WHERE player_id = ? "
            " ORDER BY net_date DESC, protocol LIMIT ?",
            (session.player_id, limit),
        ).fetchall()
    finally:
        conn.close()

    return JSONResponse({"checkins": [dict(r) for r in rows]}, status_code=200)


# ---- checkin-health --------------------------------------------------
#
# The plain-English fix for each contact status below is deliberately
# actionable, not just descriptive -- this is the endpoint the spec
# calls "the one to get right," because today the only person who can
# see WHY a MeshCore check-in isn't landing is an operator reading
# app/admin_ops.py's overview, and roughly four in ten bound contacts
# fail to resolve. Every case a player can hit is covered here; there
# is no catch-all "unknown" bucket to hide behind.
_CONTACT_EXPLANATIONS = {
    "resolved": (
        "This contact resolves automatically. Check-ins it posts under "
        "this name are credited with nothing further needed from you."
    ),
    "not_in_directory": (
        "This contact's key has never shown up in the check-in directory, "
        "so it cannot be matched to a name yet. In MeshMapper, check "
        "Settings, API Endpoints, Include Contact Key is turned on, and "
        "wardrive with it a little -- the directory picks new radios up "
        "on its own once they've been heard. Registering a fallback name "
        "below will credit you in the meantime."
    ),
    "key_ambiguous": (
        "This contact's key prefix currently matches more than one entry "
        "in the directory, so it is refused rather than guessed at -- a "
        "wrong credit is worse than a missed one. This is usually "
        "temporary and clears as the directory updates; if it does not "
        "clear, this is worth flagging to an operator."
    ),
    "name_ambiguous": (
        "This contact resolves to a display name that more than one "
        "radio in the directory is currently using, so it is refused "
        "rather than risk crediting the wrong person. Rename this "
        "companion node to something nobody else's radio is using."
    ),
}


def _checkin_contacts_status(conn, player_id: int, directory: list[dict]) -> list[dict]:
    """Every one of this player's bound MeshCore contacts (player_node,
    protocol='mc'), each classified against `directory` by
    checkin.mc_contact_status() -- the exact same per-contact decision
    checkin._build_directory_bridge() makes at check-in time, just not
    thrown away for the cases that don't cleanly resolve.
    """
    from .checkin import _index_mc_directory, mc_contact_status

    by_prefix, ambiguous_names = _index_mc_directory(directory)
    rows = conn.execute(
        "SELECT node_ref, bound_at FROM player_node "
        " WHERE protocol = ? AND player_id = ? ORDER BY bound_at",
        (MC_PROTOCOL, player_id),
    ).fetchall()

    out = []
    for r in rows:
        contact = r["node_ref"]
        status = mc_contact_status(by_prefix, ambiguous_names, contact)
        out.append({
            "node_ref": contact,
            "bound_at": r["bound_at"],
            "status": status["status"],
            "resolved_name": (
                status["name"] if status["status"] in ("resolved", "name_ambiguous") else None
            ),
            "match_count": status["match_count"],
            "explanation": _CONTACT_EXPLANATIONS[status["status"]],
        })
    return out


def _checkin_binding_status(conn, player_id: int, directory: list[dict]) -> dict:
    """Whether this player has a self-declared check-in name
    (mc_checkin_binding) registered, and whether it is actually the
    thing crediting them right now, or sitting inert behind a key
    match. Reuses _build_directory_bridge() (the real key-based pass)
    and _resolve_mc_identities() (the real key-wins-over-fallback
    priority rule -- see that function's own docstring) instead of
    re-deriving either. Neither call has any side effect here: both
    only write to checkin_node_name when given a `connector`, which
    this never passes.
    """
    from .checkin import _build_directory_bridge, _resolve_mc_identities

    row = conn.execute(
        "SELECT sender_name FROM mc_checkin_binding WHERE player_id = ?",
        (player_id,),
    ).fetchone()
    if row is None:
        return {
            "registered": False,
            "sender_name": None,
            "active": False,
            "explanation": (
                "You have not registered a fallback check-in name. If none of "
                "your bound contacts resolve automatically (see above), "
                "register the exact name your check-ins post under so you "
                "keep earning credit while the directory catches up."
            ),
        }

    sender_name = row["sender_name"]
    bridge = _build_directory_bridge(conn, directory)
    if player_id in bridge.values():
        return {
            "registered": True,
            "sender_name": sender_name,
            "active": False,
            "explanation": (
                "Not currently in effect -- one of your bound contacts already "
                "resolves through the directory, so this fallback name isn't needed."
            ),
        }

    resolved = _resolve_mc_identities(conn, directory, other_directories=[])
    if resolved.get(sender_name) == player_id:
        return {
            "registered": True,
            "sender_name": sender_name,
            "active": True,
            "explanation": "This fallback name is what is currently crediting your check-ins.",
        }

    return {
        "registered": True,
        "sender_name": sender_name,
        "active": False,
        "explanation": (
            "Not currently in effect -- this name collides with a name the "
            "directory already resolves for someone else, so it is being "
            "refused rather than risk crediting the wrong person. Register a "
            "more distinctive fallback name instead."
        ),
    }


@router.get("/api/account/checkin-health")
async def account_checkin_health(
    request: Request, session: SessionPrincipal = Depends(require_session),
) -> JSONResponse:
    """Why my check-ins may not be counting.

    Gives a player the same diagnosis app/admin_ops.py's overview
    already gives an operator about them (checkin_unreachable,
    checkin_name_changed) -- but self-serve, and per-contact rather
    than a single flag, since a player can have more than one bound
    MeshCore radio in more than one resolution state at once.

    Reads the check-in poller's own cached directory
    (request.app.state.checkin_poller.directory_snapshot(), the
    UNION across every configured connector) -- the same source
    app/admin_ops.py's _attention() and app/checkin_api.py's node
    picker already read from, and never a fresh upstream fetch for a
    page load. With no poller running (or nothing cached yet), the
    directory is empty and every contact reports "not_in_directory" --
    an honest answer, not a 500: there is genuinely nothing to resolve
    against right now.

    `recent_unresolved_names` is checkin_unresolved_sender read back
    almost as-is, with one deliberate limitation stated up front:
    that table is keyed by NAME, not by player_id (a message that
    resolved to nobody has no player to attribute it to), so entries
    here can never be asserted as "yours" -- only offered as names a
    player can eyeball for a typo or a stale binding of their own.
    """
    if session.player_id is None:
        return _no_linked_player_error()

    poller = getattr(request.app.state, "checkin_poller", None)
    directory = poller.directory_snapshot() if poller is not None else []

    conn = connect()
    try:
        contacts = _checkin_contacts_status(conn, session.player_id, directory)
        binding = _checkin_binding_status(conn, session.player_id, directory)

        cutoff = int(time.time()) - _UNRESOLVED_LOOKBACK_DAYS * 86400
        unresolved_rows = conn.execute(
            "SELECT sender_name, net_date, first_seen, last_seen, message_count "
            "  FROM checkin_unresolved_sender WHERE last_seen > ? "
            " ORDER BY last_seen DESC LIMIT 25",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    resolved_overall = any(c["status"] == "resolved" for c in contacts) or binding["active"]
    if resolved_overall:
        summary = (
            "At least one of your bound contacts or your fallback name is "
            "currently crediting your check-ins."
        )
    elif not contacts and not binding["registered"]:
        summary = (
            "You have no MeshCore contact bound and no fallback name "
            "registered, so you cannot earn MeshCore net check-ins yet. "
            "Binding a radio usually happens on its own the first time you "
            "wardrive with MeshMapper's Include Contact Key setting on, or "
            "you can register the exact name your check-ins post under below."
        )
    else:
        summary = (
            "Nothing is currently crediting your MeshCore check-ins. See the "
            "detail on each contact below, or register a fallback name with "
            "the exact name your radio posts under."
        )

    return JSONResponse(
        {
            "resolved": resolved_overall,
            "summary": summary,
            "contacts": contacts,
            "binding": binding,
            "recent_unresolved_names": {
                "note": (
                    "Recent check-in messages nobody could be matched to. "
                    "Keyed by name, not by player -- these may or may not be "
                    "yours. Never assume one is you just because the name "
                    "looks close."
                ),
                "entries": [dict(r) for r in unresolved_rows],
            },
        },
        status_code=200,
    )
