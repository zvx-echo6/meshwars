"""FastAPI router for key-authenticated node (radio) management.

A player registers once at /api/join and gets a key shown exactly one
time (see app/join_api.py). Everything from here on -- listing which
radios are on the player's name, adding another, removing one -- is
authenticated by that same key, never by anything new. There is no
session, no cookie, no login: the key IS the credential, on every
request, same as /api/mc/ingest and /api/mc/status already do.

Why this exists: MeshCore radios self-bind the moment a wardriving
batch arrives carrying a contact key (see app/mc_ingest.py), so those
never need this. Meshtastic radios have no equivalent -- their position
data is pulled from a third-party service that carries no key back to
us -- so a Meshtastic node has always had to be registered explicitly.
Originally that only happened once, at signup, and there was no way to
add a second radio afterward. These routes remove that ceiling: a
player can register with zero radios and add as many as they like,
whenever they like, using the key they already have.

The key travels in the X-API-Key request header on every route here,
including GET and DELETE -- never in the URL and never in a request
body. A key in a URL ends up in server access logs, browser history,
and any Referer header a browser sends onward; a header does not.

Authentication reuses request.app.state.mc_ingestor.authenticate(),
the exact same short-TTL-cached key lookup /api/mc/ingest and
/api/mc/status already use (app/mc_ingest.py: McIngestor.authenticate /
_lookup_key_sync). That object is constructed unconditionally in
app/main.py's lifespan regardless of settings.mc_ingest_enabled --
only its background worker is gated by that flag -- so authenticate()
is always available here even on a deployment that has MeshCore
ingest turned off entirely.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .config import settings
from .db import connect
from .mc_ingest import hash_secret
from .node_ref import normalize_node_ref

router = APIRouter()

_VALID_PROTOCOLS = ("mt", "mc")
_DEFAULT_PROTOCOL = "mt"

# ---- rate limiting ------------------------------------------------------
#
# Same bounded-dictionary pattern as _attempts in app/join_api.py and
# _status_attempts in app/mc_api.py, keyed here on the API key hash
# rather than the caller's address -- every route in this module is
# already authenticated by the time this check runs (see _authenticate
# below), so limiting by key is both simpler and more accurate than
# limiting by address, the same choice McIngestor.rate_limit_ok makes
# for /api/mc/ingest. A distinct dict from that one, though: ingest
# batches and a person clicking "add radio" a few times are different
# usage shapes with different budgets, and settings.node_api_rate_limit_*
# is a separate, much smaller ceiling from settings.mc_ingest_rate_limit_*.
_RATE_LIMIT_MAX_TRACKED = 10000

_rate_limit_hits: dict[str, list[float]] = {}


def _rate_limited(key_hash: str) -> bool:
    now = time.monotonic()
    window = settings.node_api_rate_limit_window_seconds
    limit = settings.node_api_rate_limit_attempts

    if len(_rate_limit_hits) >= _RATE_LIMIT_MAX_TRACKED:
        stale = [
            k for k, hits in _rate_limit_hits.items()
            if not hits or now - hits[-1] >= window
        ]
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


# ---- shared auth + response helpers --------------------------------------

async def _authenticate(request: Request):
    """Resolve the caller's X-API-Key header to a player_id.

    Returns (player_id, None) on success, or (None, JSONResponse) with
    the response to return as-is. Mirrors the exact status codes and
    generic messages /api/mc/ingest and /api/mc/status already use --
    "not_found" and "revoked" collapse to the same 401 "unauthorized"
    so a caller can never learn from the response alone whether a key
    ever existed.
    """
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


def _radios_out(conn, player_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT protocol, node_ref, bound_at FROM player_node "
        " WHERE player_id = ? ORDER BY bound_at",
        (player_id,),
    ).fetchall()
    return [
        {"protocol": r["protocol"], "node_ref": r["node_ref"], "bound_at": r["bound_at"]}
        for r in rows
    ]


def _parse_protocol(raw: object) -> str:
    """Missing (None) means "not supplied" -> the default, "mt". Anything
    else that isn't one of _VALID_PROTOCOLS is invalid -- returned as ""
    rather than None, since None is already spoken for as "use the
    default" and callers below only need a single falsy check either way.
    """
    if raw is None:
        return _DEFAULT_PROTOCOL
    if not isinstance(raw, str):
        return ""
    protocol = raw.strip().lower()
    return protocol if protocol in _VALID_PROTOCOLS else ""


# ---- routes ---------------------------------------------------------------

@router.get("/api/nodes")
async def list_nodes(request: Request) -> JSONResponse:
    player_id, err = await _authenticate(request)
    if err is not None:
        return err

    conn = connect()
    try:
        radios = _radios_out(conn, player_id)
    finally:
        conn.close()

    return JSONResponse({"radios": radios}, status_code=200)


@router.post("/api/nodes")
async def add_node(request: Request) -> JSONResponse:
    player_id, err = await _authenticate(request)
    if err is not None:
        return err

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad request"}, status_code=400)

    protocol = _parse_protocol(body.get("protocol"))
    if not protocol:
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

    now = int(time.time())
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")

        # Check-then-act, same pattern as the already-registered check
        # in app/join_api.py's join(): the (protocol, node_ref) primary
        # key on player_node would raise an IntegrityError on a
        # cross-player conflict too, but going through that as an error
        # path means either leaking a raw sqlite3 exception as a 500 or
        # threading exception-type checks through the transaction --
        # simpler and clearer to just look first.
        existing = conn.execute(
            "SELECT player_id FROM player_node WHERE protocol = ? AND node_ref = ?",
            (protocol, node_ref),
        ).fetchone()

        if existing is not None:
            conn.execute("ROLLBACK")
            if existing["player_id"] == player_id:
                # Already bound to the caller -- this is not an error.
                # A player re-adding a radio they already have (a retried
                # request, a second click, whatever) should just see it
                # succeed, not have to first check whether it's there.
                # ROLLBACK doesn't close the connection, so it's still
                # fine to read from here.
                radios = _radios_out(conn, player_id)
                return JSONResponse({"radios": radios, "added": False}, status_code=200)
            return JSONResponse(
                {"error": "that node is already registered to another player"},
                status_code=409,
            )

        conn.execute(
            "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) "
            "VALUES (?, ?, ?, ?)",
            (protocol, node_ref, player_id, now),
        )
        conn.execute("COMMIT")

        radios = _radios_out(conn, player_id)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return JSONResponse({"radios": radios, "added": True}, status_code=201)


@router.delete("/api/nodes/{node_ref}")
async def remove_node(node_ref: str, request: Request) -> JSONResponse:
    player_id, err = await _authenticate(request)
    if err is not None:
        return err

    protocol = _parse_protocol(request.query_params.get("protocol"))
    if not protocol:
        return JSONResponse(
            {"error": "protocol must be one of: " + ", ".join(_VALID_PROTOCOLS)},
            status_code=400,
        )

    # A malformed reference can't possibly be bound to anyone. Rather
    # than give it a distinct response, it takes the exact same path as
    # a well-formed one that doesn't match any row below (normalize_node_ref
    # already rejects it -- fold it to a value that will simply never
    # match instead of branching), so the response shape never varies
    # by input validity, ownership, or existence.
    normalized = normalize_node_ref(node_ref) or ""

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Scoped to player_id in the WHERE clause itself, not checked
        # separately -- deleting a node that exists but belongs to
        # someone else, and deleting a node_ref that doesn't exist at
        # all, both delete zero rows and produce the exact same
        # response either way. Nothing here lets a caller distinguish
        # "not yours" from "never existed" from "malformed".
        conn.execute(
            "DELETE FROM player_node WHERE protocol = ? AND node_ref = ? AND player_id = ?",
            (protocol, normalized, player_id),
        )
        conn.execute("COMMIT")
        radios = _radios_out(conn, player_id)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return JSONResponse({"radios": radios}, status_code=200)
