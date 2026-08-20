"""FastAPI router for the player-facing halves of net check-ins
(app/checkin.py) that need a human on the other end: registering the
last-resort fallback name, and browsing the MeshCore node directory to
pick a contact instead of hunting for a public-key prefix by hand.

Both routes are key-authenticated exactly like app/nodes_api.py's radio
management routes: no session, no cookie, the player's existing API key
(from /api/join) is the only credential, in the X-API-Key header, never
the URL or body. Reuses the same
request.app.state.mc_ingestor.authenticate() short-TTL-cached key lookup
and the same two-tier (address, then key) rate limiting app/nodes_api.py
already established -- see that module's docstring for why both tiers
are needed; this module copies the shape rather than importing it
because those dicts are private, per-module rate budgets, not something
meant to be shared across endpoints with different usage shapes.

Nothing in this module binds a MeshCore radio contact -- that stays
app/nodes_api.py's POST /api/nodes, unchanged. There are now three ways
a contact ends up in player_node for protocol='mc' (MeshMapper's
wardriving auto-bind, typing it into POST /api/nodes, or picking it from
GET /api/checkin/mc/nodes below and then POSTing that contact the exact
same way), and app/checkin.py's identity resolution does not know or
care which one happened -- see that module's docstring.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .checkin import companion_directory_entries
from .config import settings
from .db import connect
from .mc_ingest import hash_secret
from .node_ref import normalize_sender_name

router = APIRouter()

# Independent bounded dicts, same pattern (and same reasoning) as every
# other rate limiter in this codebase -- see app/nodes_api.py's module
# docstring for the two-tier (address, then key) shape this copies.
_RATE_LIMIT_MAX_TRACKED = 10000
_rate_limit_hits: dict[str, list[float]] = {}
_addr_rate_limit_hits: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _addr_rate_limited(ip: str) -> bool:
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
    app/nodes_api.py's _authenticate.
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


# ---- last-resort fallback name -------------------------------------------


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


# ---- MeshCore node picker -------------------------------------------------


@router.get("/api/checkin/mc/nodes")
async def mc_directory_picker(request: Request) -> JSONResponse:
    """Companion nodes from the cached live.mwmesh.com directory, so a
    player can pick a familiar node name instead of hunting for their
    public key's 8-hex prefix in a companion app.

    What this actually feeds is POST /api/nodes -- picking an entry here
    means "bind THIS contact," the exact same call as typing the prefix
    in by hand or letting MeshMapper auto-bind it on a wardriving ping.
    This endpoint changes nothing about what identity resolution trusts
    (see app/checkin.py's module docstring); it only makes the contact
    easier to find. Served from the cached directory
    app/checkin.py's CheckinPoller already maintains on its own refresh
    interval -- never a fresh upstream fetch per request, since this is
    a person clicking around a form, not a scoring path.

    `available` marks whether the CALLING player could actually bind
    this contact right now: true if it is unbound or already bound to
    them, false if a different player already holds it. This is a
    courtesy for the picker UI, not enforcement -- POST /api/nodes still
    runs its own conflict check regardless of what this says, and a
    stale cache (or a bind that happened seconds ago) could make this
    briefly wrong in either direction.
    """
    player_id, err = await _authenticate(request)
    if err is not None:
        return err

    poller = getattr(request.app.state, "checkin_poller", None)
    directory = poller.directory_snapshot() if poller is not None else []
    entries = companion_directory_entries(directory)

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT node_ref, player_id FROM player_node WHERE protocol = 'mc'"
        ).fetchall()
    finally:
        conn.close()
    bound_by = {r["node_ref"]: r["player_id"] for r in rows}

    nodes = [
        {
            **entry,
            "available": bound_by.get(entry["contact"]) in (None, player_id),
        }
        for entry in entries
    ]
    return JSONResponse({"nodes": nodes}, status_code=200)
