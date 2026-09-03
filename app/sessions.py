"""Account login sessions: create, verify, revoke, and the cookie
contract that carries a session between requests.

This is the runtime half of the account layer whose schema lives in
app/db.py (read that module's "Account layer" SCHEMA comment first --
this module is built entirely on account_session, and the design
reasoning for sliding expiry, the touch-threshold write throttle, and
why only a hash is ever stored lives there, not repeated here).

What this module deliberately does NOT do: it does not authenticate
anyone. There is no login route here, and none is added by this
change -- creating a session requires proving control of an identity
(an OAuth provider callback, a verified email/password), and no
provider integration exists yet (that is separate follow-up work, out
of scope for this change). What exists here is the machinery a future
login route calls once it has already decided WHICH account a request
is for: create_session() to mint one, set_session_cookie() to hand it
to the browser. Everything else (verify, touch, revoke) is exercised
today by app/account_api.py, which is reachable only once a session
already exists -- tests in tests/test_sessions.py and
tests/test_account_api.py create sessions directly, bypassing the
not-yet-built login step entirely, which is exactly what a real login
route will do once it exists.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, Response

from .config import settings
from .db import WriteSession, connect
from .mc_ingest import hash_secret

log = logging.getLogger("sessions")

# Cookie name. Deliberately not "session" or "sid" -- generic enough to
# collide with something else on a shared parent domain (e.g. another
# app cookied under the same TLD) is exactly the kind of surprise a
# project-scoped name avoids for free.
SESSION_COOKIE_NAME = "mw_session"

# secrets.token_urlsafe(32) -> 256 bits of entropy, ~43 base64url
# characters -- the same budget app/join_api.py's own key generation
# already uses for a similar "must not be guessable" credential, so
# this app has one answer to "how many random bytes is enough," not
# two.
_TOKEN_BYTES = 32


# ---- result types -------------------------------------------------------

@dataclass(frozen=True)
class SessionResult:
    """Outcome of verify_session(). status is one of:
    "not_found", "revoked", "expired", "ok".
    account_id and token_hash are set for every status except
    "not_found" (there is no row to read either from).
    """
    status: str
    account_id: int | None = None
    token_hash: str | None = None


@dataclass(frozen=True)
class SessionPrincipal:
    """Who a session-cookie-authenticated request is, resolved by
    require_session() below.

    Deliberately a separate type from app/auth.py's Principal, not a
    reuse of it, even though the two overlap: app/account_api.py's
    routes need to ACT on the specific session a request came in on
    (logout revokes exactly this token, never a different one on the
    same account) which means carrying token_hash around, and a raw
    API key credential has no equivalent "this exact credential, act on
    it" handle -- app/auth.py's Principal was deliberately kept to the
    shape every existing key-authenticated call site already reads
    (see that module's own comment on why account_id was added there
    without a matching token_hash). player_id is nullable here in a way
    Principal.player_id is not required to be for a session that
    resolves fine: an account with no linked player yet (the entire
    point of POST /api/account/link-key) still has to be able to load
    GET /api/account and attempt to link one.
    """
    account_id: int
    player_id: int | None
    token_hash: str


# ---- create / verify / touch --------------------------------------------

async def create_session(
    account_id: int, *, device_label: str | None
) -> str:
    """Mint a new session for account_id. Returns the RAW token -- the
    only time it ever exists outside the caller's own response; only
    its hash is stored (see account_session's own comment in
    app/db.py). Caller is responsible for handing it to the browser via
    set_session_cookie() before this value goes out of scope.

    Takes a device_label, not a raw User-Agent and IP -- every caller
    (all three live in app/oauth_api.py: the OAuth callback, the
    password sign-in route, and the pending-identity "create account"
    route) is expected to have already reduced the request's own
    User-Agent header through app/device_label.py's
    device_label_from_user_agent() before calling this. This module
    deliberately never sees the raw header at all,
    let alone the caller's IP address: account_session privacy-hardening
    (see that table's own comment in app/db.py) means there is no path
    left, anywhere, that stores an address, so create_session() simply
    has no parameter for one to be passed through.
    """
    raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
    token_hash = hash_secret(raw_token)
    now = int(time.time())
    expires_at = now + settings.account_session_lifetime_seconds
    async with WriteSession() as conn:
        conn.execute(
            "INSERT INTO account_session("
            "  token_hash, account_id, created_at, expires_at, last_seen_at, device_label"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (token_hash, account_id, now, expires_at, now, device_label),
        )
    return raw_token


def _lookup_session_sync(token_hash: str) -> sqlite3.Row | None:
    conn = connect()
    try:
        return conn.execute(
            "SELECT account_id, expires_at, last_seen_at, revoked_at "
            "  FROM account_session WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
    finally:
        conn.close()


async def _maybe_touch(token_hash: str, last_seen_at: int, now: int) -> None:
    """Sliding expiry's write half.

    Bumps last_seen_at AND slides expires_at forward with it, but ONLY
    when the stored last_seen_at is already older than
    settings.account_session_touch_threshold_seconds -- see that
    setting's own comment in app/config.py for why a write on every
    single verify would be a real cost (SQLite write-lock contention,
    through this same app/db.py WriteSession lock, with the check-in
    poller's own periodic writes) for no observable benefit. A session
    seen 4 seconds ago and one seen 3 minutes ago look identical to a
    human, so skipping the write for anything inside the threshold
    loses nothing anyone would notice.
    """
    if now - last_seen_at < settings.account_session_touch_threshold_seconds:
        return
    new_expires_at = now + settings.account_session_lifetime_seconds
    async with WriteSession() as conn:
        conn.execute(
            "UPDATE account_session SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
            (now, new_expires_at, token_hash),
        )


async def verify_session(raw_token: str) -> SessionResult:
    """Resolve a raw session token to a SessionResult, touching
    last_seen_at/expires_at when due (see _maybe_touch).

    Checked in this order, same reasoning app/mc_ingest.py's
    AuthResult status ordering already documents for api_key: revoked
    is checked before expiry so an explicitly-logged-out session that
    happens to not have aged out yet still reads as "revoked," not
    "ok" -- there is no status that could let a revoked token keep
    authenticating.
    """
    if not raw_token:
        return SessionResult("not_found")

    token_hash = hash_secret(raw_token)
    row = await asyncio.to_thread(_lookup_session_sync, token_hash)
    if row is None:
        return SessionResult("not_found")

    if row["revoked_at"] is not None:
        return SessionResult("revoked", row["account_id"], token_hash)

    now = int(time.time())
    if row["expires_at"] < now:
        return SessionResult("expired", row["account_id"], token_hash)

    await _maybe_touch(token_hash, row["last_seen_at"], now)
    return SessionResult("ok", row["account_id"], token_hash)


# ---- revoke ---------------------------------------------------------------

async def revoke_session(token_hash: str) -> None:
    """Revoke exactly one session by its hash. A no-op (not an error)
    if it's already revoked or does not exist -- logout is idempotent,
    the same way app/mc_ingest.py's key revocation and every other
    "turn this off" action in this codebase already is.
    """
    now = int(time.time())
    async with WriteSession() as conn:
        conn.execute(
            "UPDATE account_session SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
            (now, token_hash),
        )


async def revoke_all_sessions(account_id: int) -> int:
    """Revoke every currently-active session on account_id (logout
    everywhere). Returns the number of sessions actually revoked, so a
    caller can report "signed out of N sessions" if it wants to.
    """
    now = int(time.time())
    async with WriteSession() as conn:
        cur = conn.execute(
            "UPDATE account_session SET revoked_at = ? WHERE account_id = ? AND revoked_at IS NULL",
            (now, account_id),
        )
        return cur.rowcount


# ---- linked player lookup -------------------------------------------------
#
# Shared by require_session() below and by app/auth.py's session
# fallback in require_api_key_principal() -- both need "does this
# account have a linked player, and if so which one," the exact
# question player.account_id's UNIQUE index (app/db.py) exists to
# answer in one indexed lookup. app/auth.py imports this function
# directly (module-level import is safe: this module never imports
# app/auth.py back, at module scope or otherwise, so there is no cycle
# to break).

def _lookup_linked_player_sync(account_id: int) -> int | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT player_id FROM player WHERE account_id = ?", (account_id,)
        ).fetchone()
    finally:
        conn.close()
    return row["player_id"] if row else None


# ---- FastAPI dependency: "this route requires a logged-in account" -------

async def require_session(request: Request) -> SessionPrincipal:
    """FastAPI dependency for a route that requires an account session
    specifically (app/account_api.py's whole router) -- as opposed to
    app/auth.py's require_api_key_principal(), which accepts a session
    as an ADDITIONAL way to reach a route that primarily expects an API
    key. Raises the same {"error": "unauthorized"} 401 shape
    (app/auth.py's http_exception_as_error_body, registered app-wide in
    app/main.py) for every failure mode -- missing cookie, unknown
    token, revoked, expired -- so a caller can never learn from the
    response alone which of those applies, the same "don't reveal
    which part was wrong" reasoning app/auth.py's require_api_key_principal
    already applies to not_found vs. revoked API keys.
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(status_code=401, detail="unauthorized")

    result = await verify_session(raw_token)
    if result.status != "ok":
        raise HTTPException(status_code=401, detail="unauthorized")

    player_id = await asyncio.to_thread(_lookup_linked_player_sync, result.account_id)
    return SessionPrincipal(
        account_id=result.account_id, player_id=player_id, token_hash=result.token_hash
    )


# ---- FastAPI dependency: "this route's response varies by whether the
# caller is signed in, but a missing/expired/revoked session is not an
# error" -----------------------------------------------------------------
#
# Added for the privacy-hardening pass that gates person-to-place data
# (app/mc_api.py's /api/mc/cell/{cell_id}, app/api.py's /get-nodes):
# those routes must keep working for a logged-out visitor -- the map and
# a square's team-level history are the "shop window" and stay public --
# while adding fields that JOIN a place to a specific registered player
# (who captured a square; which registered player's radio a node
# belongs to) only once a real session is present. Position itself
# (a node's exact lat/lon) is NOT one of those fields -- see
# app/api.py's _build_get_nodes() docstring for Matt's explicit call on
# why coordinates already public via the mesh and upstream feeds are
# not re-withheld here; only the identity JOIN on top of them is. This
# is a strictly weaker check than require_session() above: never raise,
# just resolve to None when there is nothing to authenticate.
#
# Two ad hoc versions of exactly this "peek at the session, don't
# enforce it" read already existed before this helper: app/oauth_api.py's
# _resolve_current_account_id() (account id only, for the OAuth callback
# decision tree) and app/auth.py's _try_session_principal() (an
# api_key-shaped Principal, and only consulted when a request carries no
# X-API-Key at all). Neither returns a SessionPrincipal, and both are
# private to their own module -- this is the one general-purpose version,
# so a third hand-rolled copy doesn't show up the next time a route needs
# "more detail if you're signed in, same route either way."
async def optional_session(request: Request) -> SessionPrincipal | None:
    """Like require_session(), but resolves to None instead of raising
    on any failure -- no cookie, unknown token, revoked, expired. A
    route using this as its dependency must treat None exactly like
    require_session() raising 401 would: fall back to the public shape
    of the response, never a 500 from code that assumed a principal.
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        return None

    result = await verify_session(raw_token)
    if result.status != "ok":
        return None

    player_id = await asyncio.to_thread(_lookup_linked_player_sync, result.account_id)
    return SessionPrincipal(
        account_id=result.account_id, player_id=player_id, token_hash=result.token_hash
    )


# ---- cookie handling --------------------------------------------------
#
# HttpOnly: never readable from page JavaScript -- a session token has
# no reason to ever touch client-side script, and keeping it out of
# `document.cookie` closes off an entire class of XSS-driven theft.
# SameSite=Lax + Path=/: see app/account_api.py's module docstring for
# the CSRF analysis this pairs with -- the two are designed together
# and that reasoning lives there, next to the state-changing routes it
# actually protects, not duplicated here.
# Secure: settings.account_session_cookie_secure, defaulting True --
# see that setting's own comment in app/config.py.

def set_session_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=settings.account_session_lifetime_seconds,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.account_session_cookie_secure,
    )


def clear_session_cookie(response: Response) -> None:
    """Used on logout/logout-all. The attribute set on delete_cookie
    (path/httponly/samesite/secure) must match what set_cookie used to
    create it -- browsers key a cookie's identity on name+domain+path,
    and mismatched attributes on the deleting Set-Cookie can leave the
    original in place instead of clearing it.
    """
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.account_session_cookie_secure,
    )
