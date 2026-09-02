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

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .auth import new_rate_limit_bucket
from .client_ip import get_client_ip
from .config import settings
from .db import WriteSession, connect
from .sessions import (
    SessionPrincipal,
    clear_session_cookie,
    require_session,
    revoke_all_sessions,
    revoke_session,
)

router = APIRouter()

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
