"""FastAPI router for the player-facing halves of net check-ins
(app/checkin.py) that need a human on the other end: two PUBLIC node
pickers (MeshCore and Meshtastic) so a person can pick a radio by name
instead of typing an 8-hex reference by hand on either protocol, and
node confirmation, which proves a player actually holds a specific
radio.

(This module used to also carry the last-resort fallback-name routes,
GET/POST/DELETE /api/checkin/name -- a player typing the display name
their radio posts under, as the identity source of last resort when
the public-key directory bridge had nothing for them. Retired: it had
zero rows bound on preview, node confirmation below is strictly
stronger proof for exactly the players who needed it, and a typed name
carried none of the impersonation resistance a key-anchored match
does. mc_checkin_binding, the table it read/wrote, is left in place
per this codebase's no-drop convention -- see its own comment in
app/db.py -- but nothing reads it anymore.)

Two different trust levels in this one module, and they must not be
confused with each other:

- The two node-picker routes (GET /api/checkin/mc/nodes,
  GET /api/checkin/mt/nodes) are deliberately PUBLIC -- no key at all.
  The owner wants the radio chosen INSIDE the join form, at the point
  where a person picks their protocol, which is before /api/join has
  ever issued them a key -- a key-authenticated picker would be unusable
  at exactly the moment it's needed. Both underlying directories are
  already public data (live.mwmesh.com's node list and meshview's node
  roster are both openly readable), so serving them exposes nothing that
  isn't already exposed. They still get address-based rate limiting
  (the same bounded-dictionary pattern app/join_api.py and
  app/mc_api.py's /api/mc/status already use -- see _addr_rate_limited
  below), because "public" does not mean "unmetered." Being public also
  means neither response may disclose which entries are already bound
  to a registered player -- that would leak player-identifying
  information to anyone who asks, with no auth at all standing in the
  way. That check stays exactly where it already lives: POST
  /api/nodes' existing conflict check, which still refuses a taken
  node_ref with a clear error at bind time.

Nothing in this module binds a radio to a player -- that stays
app/nodes_api.py's POST /api/nodes and app/join_api.py's /api/join
(which already accepts an optional Meshtastic node_ref), both
unchanged. Picking an entry from either list here is just a friendlier
way to arrive at the exact same node_ref that typing it in by hand, or
(MeshCore only) MeshMapper's wardriving auto-bind, would have produced
-- app/checkin.py's identity resolution does not know or care which of
the three ever happened -- EXCEPT for node confirmation below, which is
the one case where this module DOES bind a radio itself.

- Node confirmation (POST /api/checkin/confirm/start, GET .../status,
  POST .../accept, DELETE /api/checkin/confirm) is key/session-
  authenticated exactly like the public pickers' address tier plus a
  per-key tier layered on top -- same require_checkin_principal
  dependency the retired fallback-name routes used to share, same
  reasoning (a person proving their own radio is a session-worthy
  action, not a public one). Unlike every other route in this module,
  POST .../accept DOES write to player_node directly, with the SAME
  first-claim-wins conflict check POST /api/nodes uses
  (app/nodes_api.py) -- see that
  route's own docstring below for why this is a deliberate, narrow
  exception rather than a second bind path drifting out of sync with
  the first: confirmation is a stronger proof of ownership than
  anything POST /api/nodes itself can check (a typed node_ref alone
  proves nothing), so it earns the right to bind on its own rather
  than merely handing the player a node_ref to paste into that route
  by hand. The actual proof-of-possession mechanics -- what a
  "confirmation window" is, why its baseline snapshot can never come
  from CheckinPoller's cache, what counts as a fresh advert -- live in
  app/db.py's mc_node_confirmation comment and app/checkin.py's
  confirm_scan_connector/confirm_scan_all_connectors; this module's own
  job is just the HTTP surface: validate input, open/read/close a
  window, and perform the one write once a live re-scan has verified
  the claim.
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .auth import Principal, new_rate_limit_bucket, require_api_key_principal
from .checkin import (
    companion_directory_entries,
    confirm_scan_all_connectors,
    issue_unique_mt_confirm_code,
    mt_confirm_scan_all_connectors,
    mt_roster_entries,
)
from .client_ip import get_client_ip
from .config import settings
from .db import connect
from .node_ref import normalize_node_ref, normalize_public_key, normalize_sender_name

router = APIRouter()

# Independent app/auth.py new_rate_limit_bucket() instances, same
# pattern (and same reasoning) as every other rate limiter in this
# codebase -- see app/auth.py's require_api_key_principal() docstring
# for the two-tier (address, then key) shape the fallback-name routes
# below use, and app/mc_api.py's POST /api/mc/status for the
# address-only shape the public pickers use.
#
# _addr_rate_limiter is deliberately ONE instance shared two ways: as
# the pre-auth tier passed into require_checkin_principal() below, AND
# called directly (see _client_ip usage further down) by the two public
# picker routes, which have no key at all to authenticate. That sharing
# is not new here -- the original hand-rolled _addr_rate_limited() was
# already the same single dict serving both -- so an address that's
# been hammering the public pickers arrives at the fallback-name routes
# with less budget left, same as before this module existed.
_addr_rate_limiter = new_rate_limit_bucket()
_key_rate_limiter = new_rate_limit_bucket()


def _client_ip(request: Request) -> str:
    # See app/client_ip.py's module docstring: this used to be
    # request.client.host directly, which is always the Caddy reverse
    # proxy's own address in every deployment, not the real caller's.
    return get_client_ip(request)


def _addr_rate_limited(ip: str) -> bool:
    """True if `ip` has used up its budget for the current window --
    shared by the two public picker routes below AND (via
    require_checkin_principal()'s pre_auth_limiter) the first tier for
    the key-authenticated fallback-name routes. See _addr_rate_limiter's
    own comment above for why that sharing is intentional.
    """
    return _addr_rate_limiter.limited(
        ip,
        limit=settings.mc_status_rate_limit_attempts,
        window=settings.mc_status_rate_limit_window_seconds,
    )


require_checkin_principal = require_api_key_principal(
    pre_auth_limiter=_addr_rate_limiter,
    post_auth_limiter=_key_rate_limiter,
    # A logged-in browser session is just as good a credential as this
    # player's own key for the node-confirmation routes below -- a
    # person acting through the site, not a machine posting a batch.
    # See app/auth.py's module docstring ("opt-in") for why this is one
    # of the four sites that ask for it and app/api.py's POST
    # /api/mc/ingest is the one that doesn't.
    allow_session_fallback=True,
)


# ---- node pickers (PUBLIC, address rate-limited) ---------------------------


@router.get("/api/checkin/mc/nodes")
async def mc_directory_picker(request: Request) -> JSONResponse:
    """Companion nodes from the cached live.mwmesh.com directory, so a
    player can pick a familiar node instead of hunting for their public
    key's 8-hex prefix in a companion app. PUBLIC -- see the module
    docstring for why (the join form needs this before a key exists)
    and for why that means no bound/available status can appear here.

    What this actually feeds is POST /api/nodes (or, for MeshCore,
    /api/join, which self-binds nothing but can be followed by
    POST /api/nodes) -- picking an entry means "bind THIS node_ref," the
    exact same call as typing the prefix in by hand or letting
    MeshMapper auto-bind it on a wardriving ping. This endpoint changes
    nothing about what identity resolution trusts (see
    app/checkin.py's module docstring); it only makes the node easier to
    find. Served from the cached directory app/checkin.py's
    CheckinPoller already maintains on its own refresh interval -- never
    a fresh upstream fetch per request, since this is a person clicking
    around a form, not a scoring path.

    Each entry: name, short_name (always null here -- the MeshCore
    directory has no short-name concept), node_ref (bare lowercase
    8-hex, NOT "!"-prefixed -- see companion_directory_entries()'s
    docstring for why display formatting is the UI's job, not this
    endpoint's), last_seen, lat, lon. Duplicate names are never
    collapsed -- node_ref is what tells two identically-named entries
    apart.
    """
    ip = _client_ip(request)
    if _addr_rate_limited(ip):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    poller = getattr(request.app.state, "checkin_poller", None)
    directory = poller.directory_snapshot() if poller is not None else []
    nodes = companion_directory_entries(directory)
    return JSONResponse({"nodes": nodes}, status_code=200)


@router.get("/api/checkin/mt/nodes")
async def mt_roster_picker(request: Request) -> JSONResponse:
    """Meshtastic nodes from node_seen, so a player can pick a familiar
    node instead of typing an 8-hex node id by hand. PUBLIC, same
    reasoning and same rate limiting as the MeshCore picker above --
    see this module's docstring.

    No new upstream call: node_seen is already repopulated every poll
    cycle by app/ingest.py's Ingestor._refresh_roster() from meshview's
    /api/nodes for the live board's own map markers -- this just reads
    it back out, shaped and filtered for a picker instead of a map. See
    app/checkin.py's mt_roster_entries() for the exclusion rule
    (settings.excluded_roles_set, the same infrastructure-role list the
    live board itself already uses) and the most-recently-seen-first
    ordering.

    Same entry shape as the MeshCore picker (name, short_name, node_ref,
    last_seen, lat, lon), plus one Meshtastic-only field: public_key,
    filled in from mt_node_key when exactly one distinct key is on
    record for that node_ref, otherwise null (see mt_roster_entries()
    in app/checkin.py) -- so the join page can pre-fill the key field
    the moment someone picks a node we already recognize. short_name
    here is node_seen's real (nullable) column, passed through as-is,
    never invented. node_ref is bare lowercase 8-hex, not "!"-prefixed,
    for the same reason the MeshCore picker's is bare: this endpoint
    returns the canonical storage/binding form, and protocol-specific
    display (Meshtastic gets a leading "!" where it's shown, MeshCore
    doesn't) is the UI's job, already handled elsewhere (the join page's
    radio list, the admin portal) -- not something this endpoint bakes
    in.
    """
    ip = _client_ip(request)
    if _addr_rate_limited(ip):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    conn = connect()
    try:
        nodes = mt_roster_entries(conn)
    finally:
        conn.close()
    return JSONResponse({"nodes": nodes}, status_code=200)


# ---- node confirmation (key-authenticated, binds a radio) -----------------
#
# See this module's own docstring for why this group -- unlike every
# other route above -- is allowed to write to player_node directly, and
# app/db.py's mc_node_confirmation/mt_node_confirmation comments /
# app/checkin.py's confirm_scan_connector/confirm_scan_all_connectors
# (MeshCore) and mt_confirm_scan_connector/mt_confirm_scan_all_connectors
# (Meshtastic) for the actual proof-of-possession mechanics of each
# protocol. What's left here is purely the HTTP surface: validate
# input, open/read/close exactly one window per player -- MeshCore OR
# Meshtastic, never both at once, see confirm_start below -- and
# perform the one write once a live re-scan has verified the claim.
#
# One route group, two protocols: POST .../start takes an optional
# `protocol` ("mc" default, or "mt") and opens the matching table's
# window; GET .../status and POST .../accept never take a `protocol`
# themselves -- they read whichever of mc_node_confirmation/
# mt_node_confirmation currently has a row for this player (mc_row/
# mt_row in confirm_status/confirm_accept below) and act accordingly,
# since a player can only ever be mid-confirmation for one radio, on
# one protocol, at a
# time. DELETE .../confirm cancels whichever is open. This mirrors, at
# the HTTP layer, the exact asymmetry app/checkin.py's module docstring
# already describes between the two protocols' identity models: same
# window/throttle shape, genuinely different proof underneath.

_CONFIRM_WINDOW_SECONDS = 300  # 5 minutes -- long enough to walk to a radio and key it on, short enough that a stale window can't sit open indefinitely
_CONFIRM_SCAN_THROTTLE_SECONDS = 8  # see _scan_cache below

# player_id -> most recent scan result: confirm_scan_all_connectors()'s
# list of {public_key, name, role, last_heard_epoch} dicts for an open
# MeshCore window, or mt_confirm_scan_all_connectors()'s list of
# {node_ref, node_id, name, last_heard_epoch} dicts for an open
# Meshtastic one -- shared by both protocols rather than two separate
# caches, since a player has at most one open window (either table) at
# a time (see this section's header comment above), so there is never
# a moment this dict needs to hold both shapes for the same player_id.
# Populated on every LIVE scan (start, and status/accept when the
# throttle below lets one through) and served back out, unchanged, on a
# status poll that arrives inside the throttle window -- see
# confirm_status.
#
# Deliberately in-memory, not a table -- same reasoning
# CheckinPoller.last_poll_at/last_poll_error give for their own in-
# process state (app/checkin.py): this is a short-lived liveness
# signal (a window lives at most _CONFIRM_WINDOW_SECONDS) with a cheap,
# well-defined fallback if it's ever missing after a process restart --
# the very next scan (never more than _CONFIRM_SCAN_THROTTLE_SECONDS
# away) repopulates it. Popped whenever a player's window closes
# (expires, is cancelled, or is consumed by a successful accept) so
# this dict never grows past the number of players CURRENTLY mid-
# confirmation.
_scan_cache: dict[int, list[dict]] = {}


def _fresh_candidates(raw: list[dict], baseline: dict) -> list[dict]:
    """Filter an already exact-name-matched scan (confirm_scan_
    all_connectors' output) down to the entries that count as PROOF for
    this window -- see app/db.py's mc_node_confirmation comment for why
    a bare name match is not enough on its own. A public key absent
    from `baseline` is a candidate outright (never posted under this
    name before the window opened); one present in it is a candidate
    only if it has been heard MORE RECENTLY since -- a node that was
    already advertising before the window opened, and hasn't been
    heard again since, proves nothing about who is holding it right
    now. `baseline`'s values and a raw entry's last_heard_epoch are
    both treated as 0 ("never heard") wherever they're missing/None, so
    a merely-absent timestamp can never look like it moved forward.
    """
    out = []
    for node in raw:
        current = node["last_heard_epoch"] if isinstance(node["last_heard_epoch"], int) else 0
        base = baseline.get(node["public_key"])
        if base is None or current > base:
            out.append(node)
    return out


def _node_ref_owners(conn, protocol: str, node_refs: set[str]) -> dict[str, int]:
    """Which player, if any, already holds each of `node_refs` in
    player_node under `protocol` ('mc' or 'mt') -- app/checkin_api.py's
    confirm/status uses this to classify each candidate into the three
    states the UI has to tell apart: unclaimed (absent from this
    mapping entirely), already the CALLER's own (mapped to the
    caller's own player_id -- not an error, never something to grey
    out: see already_yours below), or already someone ELSE's (mapped
    to a different player_id -- already_claimed). Returning the owning
    player_id, not just a membership set, is what lets the caller tell
    those last two apart; a plain "is this ref claimed by anyone"
    boolean can't, which was the bug -- a candidate that was the
    caller's OWN radio used to come back flagged already_claimed same
    as a stranger's, and the UI greyed it out as someone else's. This
    route itself never refuses to SHOW any of the three (that refusal
    belongs to accept's own conflict check, below, at the moment
    someone actually tries to claim it). Protocol is a parameter, not
    hardcoded, because this one function now backs both confirm/status
    branches below -- 'mc' node_refs and 'mt' node_refs are never
    comparable (the same 8-hex string can independently exist,
    unrelated, in each protocol's own namespace), so which column value
    to filter on has to come from the caller, not be assumed.
    """
    if not node_refs:
        return {}
    placeholders = ",".join("?" for _ in node_refs)
    rows = conn.execute(
        f"SELECT node_ref, player_id FROM player_node WHERE protocol = ? AND node_ref IN ({placeholders})",
        (protocol, *node_refs),
    ).fetchall()
    return {r["node_ref"]: r["player_id"] for r in rows}


def _clear_confirm_windows(conn, player_id: int) -> None:
    """Delete BOTH mc_node_confirmation and mt_node_confirmation rows
    for `player_id`, if either exists. Called whenever a window closes
    for any reason (a fresh start on either protocol, a successful
    accept, an explicit cancel, an expiry noticed on read) so "at most
    one open window, mc or mt, never both" (see this section's header
    comment) stays true no matter which of those four paths got there
    first. Harmless, cheap no-op on whichever table has no row for this
    player -- DELETE ... WHERE matching nothing is not an error.
    """
    conn.execute("DELETE FROM mc_node_confirmation WHERE player_id = ?", (player_id,))
    conn.execute("DELETE FROM mt_node_confirmation WHERE player_id = ?", (player_id,))


@router.post("/api/checkin/confirm/start")
async def confirm_start(
    request: Request, principal: Principal = Depends(require_checkin_principal)
) -> JSONResponse:
    """Open (or replace) this player's confirmation window.

    `protocol` in the body selects which radio type: "mc" (the
    default, so the original MeshCore-only frontend keeps working
    unchanged against this same endpoint) requires `name`, the display
    name the player's radio currently shows on the mesh; "mt" takes no
    `name` at all and instead generates a fresh, unique broadcast code
    for the player to send. Whichever protocol is NOT selected has its
    OWN window cleared here too (_clear_confirm_windows) -- a player
    has at most one open confirmation window, ever, regardless of
    protocol; starting one kind always retires the other kind's, the
    same way starting a fresh MeshCore window already retired any
    previous MeshCore one.

    MeshCore path: takes the baseline snapshot -- an on-demand,
    uncached scan of every configured MeshCore-family connector
    (app/checkin.py's confirm_scan_all_connectors; see that function
    and app/db.py's mc_node_confirmation comment for why this can
    never be CheckinPoller's cached directory) -- RIGHT NOW, before
    responding, so the window's five minutes start counting from a
    snapshot the player has not yet had a chance to act on.

    Meshtastic path: no baseline needed at all -- see app/checkin.py's
    Meshtastic node-confirmation section header for why a freshly
    generated, unique code is its own proof with nothing to compare it
    against.

    Set, not add, on whichever table gets the new row: PRIMARY KEY
    (player_id) means opening a second window on the SAME protocol (a
    retry, a different node, a typo fixed) silently replaces whatever
    window of that protocol was already open, exactly as before.
    """
    player_id = principal.player_id

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad request"}, status_code=400)

    protocol = body.get("protocol")
    if protocol is None:
        protocol = "mc"  # default -- see docstring: keeps the pre-existing MeshCore-only callers unchanged
    if protocol not in ("mc", "mt"):
        return JSONResponse({"error": "protocol must be 'mc' or 'mt'"}, status_code=400)

    now = int(time.time())
    expires_at = now + _CONFIRM_WINDOW_SECONDS

    if protocol == "mc":
        name = body.get("name")
        if normalize_sender_name(name) is None:
            return JSONResponse({"error": "name is required"}, status_code=400)

        conn = connect()
        try:
            raw = await confirm_scan_all_connectors(conn, name)
            baseline = {
                n["public_key"]: (n["last_heard_epoch"] if isinstance(n["last_heard_epoch"], int) else 0)
                for n in raw
            }

            conn.execute("BEGIN IMMEDIATE")
            try:
                # Clears BOTH tables, not just this one -- see
                # _clear_confirm_windows and this route's own docstring
                # for why starting one protocol's window always retires
                # the other's.
                _clear_confirm_windows(conn, player_id)
                conn.execute(
                    "INSERT INTO mc_node_confirmation"
                    "(player_id, typed_name, opened_at, expires_at, baseline, last_scan_at) "
                    "VALUES (?, ?, ?, ?, ?, 0)",
                    (player_id, name, now, expires_at, json.dumps(baseline)),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

        _scan_cache.pop(player_id, None)  # stale from any previous window -- see _scan_cache's own comment

        return JSONResponse(
            {"expires_at": expires_at, "window_seconds": _CONFIRM_WINDOW_SECONDS, "baseline_count": len(baseline)},
            status_code=200,
        )

    # protocol == "mt"
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # issue_unique_mt_confirm_code() and the INSERT below share
            # this one BEGIN IMMEDIATE transaction -- see that
            # function's own docstring for why that's what actually
            # makes its uniqueness check race-free, not the loop by
            # itself.
            code = issue_unique_mt_confirm_code(conn)
            _clear_confirm_windows(conn, player_id)
            conn.execute(
                "INSERT INTO mt_node_confirmation"
                "(player_id, code, opened_at, expires_at, last_scan_at) "
                "VALUES (?, ?, ?, ?, 0)",
                (player_id, code, now, expires_at),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    _scan_cache.pop(player_id, None)

    return JSONResponse(
        {"protocol": "mt", "code": code, "expires_at": expires_at, "window_seconds": _CONFIRM_WINDOW_SECONDS},
        status_code=200,
    )


@router.get("/api/checkin/confirm/status")
async def confirm_status(
    request: Request, principal: Principal = Depends(require_checkin_principal)
) -> JSONResponse:
    """Poll this player's open confirmation window -- MeshCore for a
    fresh advert, Meshtastic for a message carrying the issued code.
    Checks mc_node_confirmation first, then mt_node_confirmation, and
    the response always names which one it found via `protocol` -- see
    this section's header comment for why there can never be a row in
    both at once, so this is never actually ambiguous, just two tables
    to look in.

    Re-scans on every call the caller isn't throttled on (see
    _scan_cache's own comment for why a THROTTLED call is answered from
    the previous scan instead of skipped outright -- a player mid-
    window still gets an answer every poll, just not always a freshly
    fetched one) -- a browser polling this every couple of seconds for
    up to five minutes must never turn into a request storm against
    every configured connector, MeshCore or Meshtastic alike.

    An open `mt` window's response includes `code`, the same issued
    code confirm_start returned -- the player is about to broadcast it
    in the clear on an open mesh, so returning it back to them here
    costs nothing, and it's what lets a page reload mid-window recover
    the code (and the countdown, and polling) instead of forcing a
    cancel-and-restart. An `mc` window's response never carries a
    `code` key at all -- there is no code on that protocol, only a
    typed_name, which this route already doesn't echo back either.
    Strictly scoped to the caller's OWN window either way, same as
    every other field here -- this route reads mc_node_confirmation/
    mt_node_confirmation by this player's own player_id, never anyone
    else's.
    """
    player_id = principal.player_id
    now = int(time.time())

    conn = connect()
    try:
        mc_row = conn.execute(
            "SELECT typed_name, expires_at, baseline, last_scan_at "
            "  FROM mc_node_confirmation WHERE player_id = ?",
            (player_id,),
        ).fetchone()

        if mc_row is not None:
            if now >= mc_row["expires_at"]:
                conn.execute("DELETE FROM mc_node_confirmation WHERE player_id = ?", (player_id,))
                _scan_cache.pop(player_id, None)
                return JSONResponse({"state": "none"}, status_code=200)

            typed_name = mc_row["typed_name"]
            expires_at = mc_row["expires_at"]
            baseline = json.loads(mc_row["baseline"])

            if now - mc_row["last_scan_at"] < _CONFIRM_SCAN_THROTTLE_SECONDS:
                raw = _scan_cache.get(player_id, [])
            else:
                raw = await confirm_scan_all_connectors(conn, typed_name)
                _scan_cache[player_id] = raw
                conn.execute(
                    "UPDATE mc_node_confirmation SET last_scan_at = ? WHERE player_id = ?", (now, player_id)
                )

            candidates = _fresh_candidates(raw, baseline)
            owners = _node_ref_owners(conn, "mc", {c["public_key"][:8] for c in candidates})

            out_candidates = [
                {
                    "public_key": c["public_key"],
                    "node_ref": c["public_key"][:8],
                    "name": c["name"],
                    "role": c["role"],
                    "last_heard": c["last_heard_epoch"],
                    # already_claimed means claimed by SOMEONE ELSE --
                    # the caller's own radio is never flagged this way
                    # (see _node_ref_owners' own docstring for why).
                    "already_claimed": owners.get(c["public_key"][:8]) not in (None, player_id),
                    # The third state already_claimed alone can't
                    # express: this exact candidate is already bound to
                    # the CALLER themself (a prior accept, days-old data
                    # that came across in a copy, whatever) -- not
                    # available, but not somebody else's either.
                    "already_yours": owners.get(c["public_key"][:8]) == player_id,
                }
                for c in candidates
            ]
            return JSONResponse(
                {
                    "protocol": "mc",
                    "state": "found" if out_candidates else "waiting",
                    "expires_at": expires_at,
                    "candidates": out_candidates,
                },
                status_code=200,
            )

        mt_row = conn.execute(
            "SELECT code, expires_at, last_scan_at FROM mt_node_confirmation WHERE player_id = ?",
            (player_id,),
        ).fetchone()

        if mt_row is None:
            return JSONResponse({"state": "none"}, status_code=200)

        if now >= mt_row["expires_at"]:
            conn.execute("DELETE FROM mt_node_confirmation WHERE player_id = ?", (player_id,))
            _scan_cache.pop(player_id, None)
            return JSONResponse({"state": "none"}, status_code=200)

        code = mt_row["code"]
        expires_at = mt_row["expires_at"]

        if now - mt_row["last_scan_at"] < _CONFIRM_SCAN_THROTTLE_SECONDS:
            raw = _scan_cache.get(player_id, [])
        else:
            raw = await mt_confirm_scan_all_connectors(conn, code)
            _scan_cache[player_id] = raw
            conn.execute(
                "UPDATE mt_node_confirmation SET last_scan_at = ? WHERE player_id = ?", (now, player_id)
            )

        # No _fresh_candidates()-style baseline filter here -- every
        # match mt_confirm_scan_all_connectors returns IS proof, by
        # construction (see app/checkin.py's Meshtastic node-
        # confirmation section header for why).
        owners = _node_ref_owners(conn, "mt", {m["node_ref"] for m in raw})
        out_candidates = [
            {
                "node_ref": m["node_ref"],
                "node_id": m["node_id"],
                "name": m.get("name"),
                "last_heard": m["last_heard_epoch"],
                # Same already_claimed/already_yours split as the mc
                # branch above -- see _node_ref_owners' own docstring.
                "already_claimed": owners.get(m["node_ref"]) not in (None, player_id),
                "already_yours": owners.get(m["node_ref"]) == player_id,
            }
            for m in raw
        ]
    finally:
        conn.close()

    return JSONResponse(
        {
            "protocol": "mt",
            "state": "found" if out_candidates else "waiting",
            "expires_at": expires_at,
            "code": code,
            "candidates": out_candidates,
        },
        status_code=200,
    )


@router.post("/api/checkin/confirm/accept")
async def confirm_accept(
    request: Request, principal: Principal = Depends(require_checkin_principal)
) -> JSONResponse:
    """Bind the radio identified in the body to the caller, IF a live
    re-scan still finds it among the current window's candidates.

    Which protocol is open -- and therefore which body field is read,
    and which upstream re-scan runs -- is read off the database
    (mc_node_confirmation checked first, then mt_node_confirmation),
    never off a client-supplied `protocol`: see this section's header
    comment for why a player can never have both open at once, so
    there is nothing for a client-supplied value to disambiguate that
    the database doesn't already answer on its own. MeshCore: body
    carries `public_key` (64 hex), unchanged from before this feature
    existed. Meshtastic: body carries `node_ref` (bare or
    "!"-prefixed 8-hex, normalize_node_ref accepts either) -- the same
    identifier GET .../status already reports on each mt candidate, so
    a client never has to convert between shapes to go from status to
    accept.

    Never trusts the client's word that a node was offered by GET
    .../status -- an accept request is re-verified against a fresh scan
    here (confirm_scan_all_connectors, under this player's own
    typed_name and baseline, for mc; mt_confirm_scan_all_connectors,
    under this player's own code, for mt) the same way GET .../status
    computes candidates itself. Once verified, the bind honours the
    SAME first-claim-wins conflict check POST /api/nodes uses
    (app/nodes_api.py): a node already claimed by someone else refuses
    with 409 and binds nothing; already bound to the CALLER is treated
    as success, not an error (a retried request, a second click); a
    fresh bind consumes the window (both tables cleared, same as
    confirm_start opening a new one) so it can't be replayed against a
    second node.
    """
    player_id = principal.player_id

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad request"}, status_code=400)

    conn = connect()
    try:
        now = int(time.time())

        mc_row = conn.execute(
            "SELECT typed_name, expires_at, baseline FROM mc_node_confirmation WHERE player_id = ?",
            (player_id,),
        ).fetchone()
        mt_row = None
        if mc_row is None:
            mt_row = conn.execute(
                "SELECT code, expires_at FROM mt_node_confirmation WHERE player_id = ?",
                (player_id,),
            ).fetchone()

        window_row = mc_row if mc_row is not None else mt_row
        if window_row is None or now >= window_row["expires_at"]:
            if window_row is not None:
                _clear_confirm_windows(conn, player_id)
                _scan_cache.pop(player_id, None)
            return JSONResponse(
                {"error": "no open confirmation window -- start one first"}, status_code=409
            )

        if mc_row is not None:
            protocol = "mc"
            public_key = normalize_public_key(body.get("public_key"))
            if public_key is None:
                return JSONResponse(
                    {"error": "public_key is required and must be 64 hex characters"}, status_code=400
                )

            raw = await confirm_scan_all_connectors(conn, mc_row["typed_name"])
            baseline = json.loads(mc_row["baseline"])
            candidates = _fresh_candidates(raw, baseline)

            if not any(c["public_key"] == public_key for c in candidates):
                return JSONResponse(
                    {"error": "that key is not a current confirmation candidate"}, status_code=400
                )

            node_ref = public_key[:8]
            bind_public_key = public_key
        else:
            protocol = "mt"
            node_ref = normalize_node_ref(body.get("node_ref"))
            if node_ref is None:
                return JSONResponse(
                    {"error": "node_ref is required and must be 8 hex characters"}, status_code=400
                )

            candidates = await mt_confirm_scan_all_connectors(conn, mt_row["code"])
            if not any(c["node_ref"] == node_ref for c in candidates):
                return JSONResponse(
                    {"error": "that node is not a current confirmation candidate"}, status_code=400
                )

            bind_public_key = None  # Meshtastic node confirmation proves a node id, never a key

        bound_at = int(time.time())
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Same check-then-act first-claim-wins pattern as POST
            # /api/nodes (app/nodes_api.py, around lines 224-244) --
            # see that route's own comment for why a look-first check
            # beats letting player_node's (protocol, node_ref) PRIMARY
            # KEY raise on a cross-player conflict.
            existing = conn.execute(
                "SELECT player_id FROM player_node WHERE protocol = ? AND node_ref = ?",
                (protocol, node_ref),
            ).fetchone()
            if existing is not None and existing["player_id"] != player_id:
                conn.execute("ROLLBACK")
                return JSONResponse(
                    {"error": "that node is already registered to another player"},
                    status_code=409,
                )
            if existing is None:
                conn.execute(
                    "INSERT INTO player_node(protocol, node_ref, player_id, bound_at, public_key) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (protocol, node_ref, player_id, bound_at, bind_public_key),
                )
            # Already bound to the caller, or freshly bound just now --
            # either way the window is consumed: it did its job. Clears
            # BOTH tables (harmless no-op on whichever has no row for
            # this player), same as confirm_start's own use of
            # _clear_confirm_windows.
            _clear_confirm_windows(conn, player_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    _scan_cache.pop(player_id, None)
    return JSONResponse({"node_ref": node_ref}, status_code=200)


@router.delete("/api/checkin/confirm")
async def confirm_cancel(
    request: Request, principal: Principal = Depends(require_checkin_principal)
) -> JSONResponse:
    """Cancel this player's open confirmation window, if any -- MeshCore
    or Meshtastic, whichever is open (see this section's header
    comment for why a player only ever has one). Always-succeeds:
    calling this with no window open is not an error, just a no-op.
    """
    player_id = principal.player_id
    conn = connect()
    try:
        _clear_confirm_windows(conn, player_id)
    finally:
        conn.close()
    _scan_cache.pop(player_id, None)
    return JSONResponse({"state": "none"}, status_code=200)
