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
  URL or body. Reuses the same
  request.app.state.mc_ingestor.authenticate() short-TTL-cached key
  lookup and the same two-tier (address, then key) rate limiting
  app/nodes_api.py already established.

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

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .checkin import companion_directory_entries, mt_roster_entries
from .client_ip import get_client_ip
from .config import settings
from .db import connect
from .mc_ingest import hash_secret
from .node_ref import normalize_sender_name

router = APIRouter()

# Independent bounded dicts, same pattern (and same reasoning) as every
# other rate limiter in this codebase -- see app/nodes_api.py's module
# docstring for the two-tier (address, then key) shape the fallback-name
# routes below copy, and app/join_api.py / app/mc_api.py's
# _status_rate_limited for the address-only shape the public pickers use.
_RATE_LIMIT_MAX_TRACKED = 10000
_rate_limit_hits: dict[str, list[float]] = {}
_addr_rate_limit_hits: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    # See app/client_ip.py's module docstring: this used to be
    # request.client.host directly, which is always the Caddy reverse
    # proxy's own address in every deployment, not the real caller's.
    return get_client_ip(request)


def _addr_rate_limited(ip: str) -> bool:
    """True if `ip` has used up its budget for the current window --
    shared by the two public picker routes below AND as the first
    (pre-auth) tier for the key-authenticated fallback-name routes,
    same as app/nodes_api.py's own address tier. Reuses
    settings.mc_status_rate_limit_attempts/window_seconds rather than
    adding a new pair of settings: this is the same "occasional,
    human-driven request from one address" usage shape
    /api/mc/status was already sized for, and the coordinator was
    explicit not to invent a third rate-limit mechanism.
    """
    now = time.monotonic()
    window = settings.mc_status_rate_limit_window_seconds
    limit = settings.mc_status_rate_limit_attempts
    if len(_addr_rate_limit_hits) >= _RATE_LIMIT_MAX_TRACKED:
        stale = [k for k, hits in _addr_rate_limit_hits.items() if not hits or now - hits[-1] >= window]
        for k in stale:
            del _addr_rate_limit_hits[k]
        if len(_addr_rate_limit_hits) >= _RATE_LIMIT_MAX_TRACKED:
            _addr_rate_limit_hits.clear()
    hits = [t for t in _addr_rate_limit_hits.get(ip, []) if now - t < window]
    if len(hits) >= limit:
        _addr_rate_limit_hits[ip] = hits
        return True
    hits.append(now)
    _addr_rate_limit_hits[ip] = hits
    return False


def _rate_limited(key_hash: str) -> bool:
    now = time.monotonic()
    window = settings.node_api_rate_limit_window_seconds
    limit = settings.node_api_rate_limit_attempts
    if len(_rate_limit_hits) >= _RATE_LIMIT_MAX_TRACKED:
        stale = [k for k, hits in _rate_limit_hits.items() if not hits or now - hits[-1] >= window]
        for k in stale:
            del _rate_limit_hits[k]
        if len(_rate_limit_hits) >= _RATE_LIMIT_MAX_TRACKED:
            _rate_limit_hits.clear()
    hits = [t for t in _rate_limit_hits.get(key_hash, []) if now - t < window]
    if len(hits) >= limit:
        _rate_limit_hits[key_hash] = hits
        return True
    hits.append(now)
    _rate_limit_hits[key_hash] = hits
    return False


async def _authenticate(request: Request):
    """Resolve the caller's X-API-Key header to a player_id. Returns
    (player_id, None) on success, or (None, JSONResponse) to return
    as-is -- identical contract and status codes to
    app/nodes_api.py's _authenticate. Only the fallback-name routes
    below use this -- the two node-picker routes are public and never
    call it.
    """
    ip = _client_ip(request)
    if _addr_rate_limited(ip):
        return None, JSONResponse({"error": "rate limited"}, status_code=429)

    raw_key = request.headers.get("X-API-Key", "")
    if not raw_key:
        return None, JSONResponse({"error": "unauthorized"}, status_code=401)

    ingestor = request.app.state.mc_ingestor
    auth = await ingestor.authenticate(raw_key)
    if auth.status in ("not_found", "revoked"):
        return None, JSONResponse({"error": "unauthorized"}, status_code=401)
    if auth.status == "disabled":
        return None, JSONResponse({"error": "forbidden"}, status_code=403)

    key_hash = hash_secret(raw_key)
    if _rate_limited(key_hash):
        return None, JSONResponse({"error": "rate limited"}, status_code=429)

    return auth.player_id, None


# ---- last-resort fallback name (key-authenticated) ------------------------


@router.get("/api/checkin/name")
async def get_checkin_name(request: Request) -> JSONResponse:
    player_id, err = await _authenticate(request)
    if err is not None:
        return err
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
async def set_checkin_name(request: Request) -> JSONResponse:
    """Set (not add) the caller's last-resort fallback check-in name.

    This binding is only ever consulted by app/checkin.py when the
    public-key directory bridge has nothing for this player -- see that
    module's docstring. Registering one does not affect a player the
    bridge already resolves; it exists for the player it doesn't.
    """
    player_id, err = await _authenticate(request)
    if err is not None:
        return err

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
async def delete_checkin_name(request: Request) -> JSONResponse:
    player_id, err = await _authenticate(request)
    if err is not None:
        return err
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
