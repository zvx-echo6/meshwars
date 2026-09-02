"""FastAPI router for the player-facing halves of net check-ins
(app/checkin.py) that need a human on the other end: registering the
last-resort fallback name, and two PUBLIC node pickers (MeshCore and
Meshtastic) so a person can pick a radio by name instead of typing an
8-hex reference by hand on either protocol.

Two different trust levels in this one module, and they must not be
confused with each other:

- The fallback-name routes (GET/POST/DELETE /api/checkin/name) are
  key-authenticated exactly like app/nodes_api.py's radio management
  routes: no session, no cookie, the player's existing API key (from
  /api/join) is the only credential, in the X-API-Key header, never the
  URL or body. Authenticated through app/auth.py's
  require_api_key_principal() -- the same short-TTL-cached
  request.app.state.mc_ingestor.authenticate() lookup and the same
  two-tier (address, then key) rate limiting app/nodes_api.py already
  established, wired up as require_checkin_principal below. Its
  pre-auth address limiter is the one exception to "independent budget
  per site" app/auth.py's own docstring calls out: it's the SAME
  _addr_rate_limiter instance the two public pickers below call
  directly, exactly as this module's original hand-rolled version
  already shared one dict between both.

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
the three ever happened.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .auth import Principal, new_rate_limit_bucket, require_api_key_principal
from .checkin import companion_directory_entries, mt_roster_entries
from .client_ip import get_client_ip
from .config import settings
from .db import connect
from .node_ref import normalize_sender_name

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
    # player's own key for managing their own fallback check-in name --
    # a person acting through the site, not a machine posting a batch.
    # See app/auth.py's module docstring ("opt-in") for why this is one
    # of the four sites that ask for it and app/api.py's POST
    # /api/mc/ingest is the one that doesn't.
    allow_session_fallback=True,
)


# ---- last-resort fallback name (key-authenticated) ------------------------


@router.get("/api/checkin/name")
async def get_checkin_name(
    request: Request, principal: Principal = Depends(require_checkin_principal)
) -> JSONResponse:
    player_id = principal.player_id
    conn = connect()
    try:
        row = conn.execute(
            "SELECT sender_name, bound_at FROM mc_checkin_binding WHERE player_id = ?",
            (player_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return JSONResponse({"sender_name": None}, status_code=200)
    return JSONResponse(
        {"sender_name": row["sender_name"], "bound_at": row["bound_at"]}, status_code=200
    )


@router.post("/api/checkin/name")
async def set_checkin_name(
    request: Request, principal: Principal = Depends(require_checkin_principal)
) -> JSONResponse:
    """Set (not add) the caller's last-resort fallback check-in name.

    This binding is only ever consulted by app/checkin.py when the
    public-key directory bridge has nothing for this player -- see that
    module's docstring. Registering one does not affect a player the
    bridge already resolves; it exists for the player it doesn't.
    """
    player_id = principal.player_id

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad request"}, status_code=400)

    name = normalize_sender_name(body.get("sender_name"))
    if name is None:
        return JSONResponse({"error": "sender_name is required"}, status_code=400)

    now = int(time.time())
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT player_id FROM mc_checkin_binding WHERE sender_name = ?", (name,)
        ).fetchone()
        if existing is not None and existing["player_id"] != player_id:
            conn.execute("ROLLBACK")
            return JSONResponse(
                {"error": "that name is already registered to another player"},
                status_code=409,
            )
        # Set semantics, not add: mc_checkin_binding.player_id is
        # UNIQUE, so a player has at most one fallback name. Re-posting
        # a different name moves the caller's own binding rather than
        # creating a second one -- delete-then-insert, since the
        # PRIMARY KEY (sender_name) can change between calls for the
        # same player and there is nothing to ON CONFLICT against by
        # player_id alone.
        conn.execute("DELETE FROM mc_checkin_binding WHERE player_id = ?", (player_id,))
        conn.execute(
            "INSERT INTO mc_checkin_binding(sender_name, player_id, bound_at) VALUES (?, ?, ?)",
            (name, player_id, now),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return JSONResponse({"sender_name": name, "bound_at": now}, status_code=200)


@router.delete("/api/checkin/name")
async def delete_checkin_name(
    request: Request, principal: Principal = Depends(require_checkin_principal)
) -> JSONResponse:
    player_id = principal.player_id
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM mc_checkin_binding WHERE player_id = ?", (player_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return JSONResponse({"sender_name": None}, status_code=200)


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
