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

Authentication goes through app/auth.py's require_api_key_principal(),
which reuses request.app.state.mc_ingestor.authenticate(), the exact
same short-TTL-cached key lookup /api/mc/ingest and /api/mc/status
already use (app/mc_ingest.py: McIngestor.authenticate /
_lookup_key_sync). That object is constructed unconditionally in
app/main.py's lifespan regardless of settings.mc_ingest_enabled --
only its background worker is gated by that flag -- so authenticate()
is always available here even on a deployment that has MeshCore
ingest turned off entirely. This module used to hand-roll that whole
flow itself (in a private _authenticate()); it's now the canonical
example of the default configuration app/auth.py's dependency
supports -- see that module's docstring for the two sites (POST
/api/mc/ingest, POST /api/mc/status) that configure it differently on
purpose.

Two independent rate limiters guard these routes, same as the two
different limiters already in this codebase answer two different
questions -- each its own app/auth.py new_rate_limit_bucket() instance,
private to this module (never shared with app/join_api.py's or
app/checkin_api.py's own copies of this same shape: see app/auth.py's
module docstring for why merging those pools would be an observable
behavior change):

- An address-keyed limiter runs FIRST, before the X-API-Key header is
  even read -- mirroring exactly how /api/mc/status (app/mc_api.py)
  puts its own pre-auth limiter as the first thing in its handler. A
  caller with no key, or the wrong one, still costs us a request: once
  McIngestor's short-TTL key cache misses (which a flood of DISTINCT
  bad keys does on every single request), _lookup_key_sync opens a
  database connection. That cost has to be bounded before
  authentication runs, not after -- an unauthenticated flood should
  never reach the per-key limiter below, because it never has a valid
  key to be limited by. Reuses settings.mc_status_rate_limit_* rather
  than adding a third pair of settings: these routes see the same
  usage shape /api/mc/status does (an occasional, human-driven check),
  not a bulk/automated one, so the same budget fits.
- A per-key limiter runs after authentication succeeds, same as
  McIngestor.rate_limit_ok does for /api/mc/ingest -- bounds abuse by a
  caller who DOES hold a valid key, which the address limiter above
  cannot do on its own (multiple players can share an address, e.g.
  behind NAT).
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .auth import Principal, new_rate_limit_bucket, require_api_key_principal
from .db import connect
from .node_ref import normalize_node_ref, normalize_public_key

router = APIRouter()

_VALID_PROTOCOLS = ("mt", "mc")
_DEFAULT_PROTOCOL = "mt"

# ---- authentication ---------------------------------------------------
#
# app/auth.py's require_api_key_principal() -- see that module's
# docstring for the full contract (status-code mapping, generic 401 for
# both "not_found" and "revoked", etc.) and for why the two rate
# limiters below are each this module's OWN new_rate_limit_bucket()
# instance rather than shared with app/join_api.py's or
# app/checkin_api.py's identically-configured copies of this same
# dependency: those were independent budgets before this module existed,
# and merging them would be an observable behavior change.
_addr_rate_limiter = new_rate_limit_bucket()
_key_rate_limiter = new_rate_limit_bucket()

require_principal = require_api_key_principal(
    pre_auth_limiter=_addr_rate_limiter,
    post_auth_limiter=_key_rate_limiter,
)


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


def _resolve_public_key(conn, protocol: str, node_ref: str, raw_public_key: object) -> tuple[str | None, JSONResponse | None]:
    """Work out what public_key to store on a new binding.

    Supplied explicitly: it must normalize cleanly or this is a 400.
    Not supplied: for a Meshtastic node, look it up in mt_node_key by
    node_ref. If exactly one distinct key is on record, use it -- that
    is the ordinary case, a node we've already heard NodeInfo from.
    Zero rows means we've simply never heard it yet, so NULL is correct.
    MORE than one distinct key is the drift/collision case mt_node_key
    exists to catch -- the node has broadcast under two different keys
    -- and guessing which one is "current" would be inventing an answer
    this table was built specifically not to invent; NULL leaves it for
    a human. MeshCore is left alone entirely: its node_ref already IS a
    key prefix, so there is nothing to resolve.
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
async def list_nodes(request: Request, principal: Principal = Depends(require_principal)) -> JSONResponse:
    player_id = principal.player_id

    conn = connect()
    try:
        radios = _radios_out(conn, player_id)
    finally:
        conn.close()

    return JSONResponse({"radios": radios}, status_code=200)


@router.post("/api/nodes")
async def add_node(request: Request, principal: Principal = Depends(require_principal)) -> JSONResponse:
    player_id = principal.player_id

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
        public_key, err = _resolve_public_key(conn, protocol, node_ref, body.get("public_key"))
        if err is not None:
            return err

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
            "INSERT INTO player_node(protocol, node_ref, player_id, bound_at, public_key) "
            "VALUES (?, ?, ?, ?, ?)",
            (protocol, node_ref, player_id, now, public_key),
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
async def remove_node(
    node_ref: str, request: Request, principal: Principal = Depends(require_principal)
) -> JSONResponse:
    player_id = principal.player_id

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
