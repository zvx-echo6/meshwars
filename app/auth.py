"""Shared API-key authentication for every key-authenticated route in
this app.

Before this module existed, the same pattern -- read X-API-Key, run a
pre-auth address-keyed rate limit, hand the raw key to
request.app.state.mc_ingestor.authenticate() (app/mc_ingest.py), and
map the result onto an HTTP response -- was hand-copied into five
places: app/join_api.py's _authenticate (team switch), app/nodes_api.py's
_authenticate (radio add/list/remove -- the most-documented copy; read
its module docstring for the two-tier rate-limiting rationale this
module inherits unchanged), app/checkin_api.py's _authenticate (fallback
check-in name), and inline in app/api.py's POST /api/mc/ingest and
app/mc_api.py's POST /api/mc/status. This module is the fix, used by
all five.

Three of those five (join_api, nodes_api, checkin_api) were byte-for-
byte identical: same two-tier rate limiting, same status-code mapping,
same messages. The other two are genuinely different and are NOT
force-fit into that shape here -- see require_api_key_principal()'s own
docstring for exactly how each of the five configures this dependency,
and what stayed different on purpose:

- POST /api/mc/ingest has no pre-auth address-keyed rate limit at all
  (never did) -- its only rate limiting is McIngestor.rate_limit_ok(),
  a per-key, post-auth check with its own settings
  (mc_ingest_rate_limit_batches/window_seconds) sized for a wardriving
  app's batch cadence, not a person clicking a button. That stays where
  it is, in app/api.py, called after this dependency resolves a
  Principal -- it is ingest-specific rate limiting, not authentication,
  and out of scope for this consolidation.
- POST /api/mc/status has a pre-auth address limiter but no post-auth
  one, and its rate-limited response uses a different message
  ("too many attempts, try again later" instead of "rate limited") --
  both preserved via require_api_key_principal()'s parameters rather
  than overwritten to match the other four.

What's genuinely shared, and lives here: the bounded-dictionary rate-
limit counter (_BoundedHits below -- the exact logic that was
retyped, unchanged, in every one of the five modules), the Principal
result type, and the status-code mapping itself
(request.app.state.mc_ingestor.authenticate()'s "not_found"/"revoked"
both collapse to a generic 401 so a caller can never learn from the
response alone whether a key ever existed; "disabled" is 403; "ok"
carries the player_id through).

What's deliberately NOT shared: rate-limit STATE. Each call site below
still gets its own _BoundedHits instance(s), not a pool shared across
modules -- app/join_api.py, app/nodes_api.py, and app/checkin_api.py's
copies of these limiters were independent budgets before this module
existed (their own comments said so explicitly: "Independent bounded
dicts, same pattern as ..."), and merging them into one shared counter
here would silently make each site's limit stricter under combined
load, an observable behavior change nothing about this refactor is
supposed to make. The one exception is intentional and internal to
app/checkin_api.py: its pre-auth address limiter is already shared, in
the ORIGINAL code, between the key-authenticated fallback-name routes
and its two public (unauthenticated) node-picker routes, which call
the limiter directly with no key at all. That module still wires one
_BoundedHits instance into both places -- this module doesn't change
that, it just gives checkin_api.py a reusable class to do it with
instead of a hand-rolled dict.

This is also the seam a session-cookie login plugs into, and now does:
Principal.source is "api_key" (default, and the ONLY value every
existing call site has ever seen) or "session" -- account_id is set
for the latter, always None for the former. require_api_key_principal()'s
built dependency below now falls back to a session cookie (see
_try_session_principal) whenever a request carries no X-API-Key header
at all -- but ONLY then: a request that DOES carry a key is
authenticated exactly as it always has been, on the exact same code
path, with the session fallback never even consulted. This is
deliberately additive, not a replacement: the five existing call sites
(app/join_api.py, app/nodes_api.py, app/checkin_api.py, app/api.py's
POST /api/mc/ingest, app/mc_api.py's POST /api/mc/status) needed zero
code changes to gain this -- they all resolve their Principal through
this one function, so a session-authenticated caller is now ACCEPTED
wherever a key-authenticated one already was, with no per-site wiring.
tests/test_auth.py locks in that a key-bearing request's behavior is
unchanged byte-for-byte by this addition. A session that resolves to
an account with no linked player (see app/db.py's player.account_id)
cannot satisfy this dependency -- every one of these five routes
operates on a specific player's data (their radios, their team, their
key), so a session with nothing to point at falls through to the same
401 a missing credential always produced, never a 500 from a None
player_id reaching a query that assumes an int.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .client_ip import get_client_ip
from .config import settings
from .mc_ingest import hash_secret
from .sessions import SESSION_COOKIE_NAME, _lookup_linked_player_sync, verify_session

# ---- error rendering ---------------------------------------------------
#
# Every hand-rolled error response in this app, everywhere this
# consolidation didn't touch, is a JSONResponse shaped {"error": "..."}.
# Starlette's default HTTPException handler instead renders
# {"detail": "..."}. require_api_key_principal() below is the first
# thing in this codebase to raise HTTPException rather than building a
# JSONResponse by hand -- it has to, to work as a real FastAPI
# dependency (Depends(...) can only short-circuit a request via a
# raised exception, not a returned value) -- but that must not change
# the JSON body a client already depending on {"error": ...} sees. This
# is that translation. app/main.py registers it once, app-wide, via
# app.add_exception_handler(HTTPException, http_exception_as_error_body)
# rather than a decorator here, since building the FastAPI app is
# main.py's job, not this module's -- but it lives here, not there,
# because this is the only code that raises what it renders.


async def http_exception_as_error_body(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        {"error": exc.detail}, status_code=exc.status_code, headers=exc.headers
    )


# ---- result type -----------------------------------------------------

@dataclass(frozen=True)
class Principal:
    """Who a request is authenticated as, and how.

    source is "api_key" (default -- every one of the five existing call
    sites, always) or "session" (app/sessions.py's cookie fallback,
    below). account_id is always None for "api_key" -- a raw API key
    has no account concept at all -- and set for "session". player_id
    is typed nullable for the "session" case in principle (an account
    with no linked player has none), but require_api_key_principal()'s
    own dependency (below) never actually returns a Principal with
    player_id=None: a session that resolves to no linked player fails
    to authenticate here entirely (401), the same as no credential at
    all, since every existing call site unconditionally reads
    principal.player_id as a real player. A session that legitimately
    has no linked player yet is instead handled by
    app/sessions.py's own require_session() dependency
    (SessionPrincipal, a separate type), which app/account_api.py's
    routes use directly -- those routes need account identity, not
    necessarily a player.
    """
    player_id: int | None
    account_id: int | None = None
    source: str = "api_key"


# ---- rate limiting: the shared counting logic -------------------------
#
# This is the bounded-dictionary pattern that was retyped, identically,
# as a module-level dict plus a same-shaped function in every one of
# the five call sites this module replaces. Collapsing the LOGIC here
# does not collapse the STATE -- see the module docstring above for why
# each site still owns its own instance(s) of this class rather than
# sharing one.

_DEFAULT_MAX_TRACKED = 10000


class _BoundedHits:
    """Tracks recent-hit timestamps per key, in a dict capped at
    max_tracked entries so an attacker flooding us with distinct keys
    (IP addresses, key hashes) can't grow it without bound. When the
    cap is hit, stale entries (whose most recent hit has already aged
    out of the window) are swept first; only if that alone doesn't
    bring it back under the cap is the whole dict cleared -- the same
    two-step eviction every duplicated copy of this used.
    """

    def __init__(self, max_tracked: int = _DEFAULT_MAX_TRACKED) -> None:
        self._hits: dict[str, list[float]] = {}
        self._max_tracked = max_tracked

    def limited(self, key: str, *, limit: int, window: float) -> bool:
        """True if `key` has used up its budget for the current
        window. Records this attempt (by timestamp) when allowed,
        exactly like every duplicated copy of this did.
        """
        now = time.monotonic()

        if len(self._hits) >= self._max_tracked:
            stale = [
                k for k, hits in self._hits.items()
                if not hits or now - hits[-1] >= window
            ]
            for k in stale:
                del self._hits[k]
            if len(self._hits) >= self._max_tracked:
                self._hits.clear()

        hits = [t for t in self._hits.get(key, []) if now - t < window]
        if len(hits) >= limit:
            self._hits[key] = hits
            return True
        hits.append(now)
        self._hits[key] = hits
        return False


async def _try_session_principal(request: Request) -> Principal | None:
    """Session-cookie fallback consulted only when a request carries no
    X-API-Key at all (see the call site in require_api_key_principal's
    dependency below) -- a request that DOES carry a key never reaches
    this function, so its existence has zero effect on key-bearing
    requests. Returns None (never raises) on any failure: no cookie, an
    invalid/expired/revoked session, or a session whose account has no
    linked player -- the caller treats None exactly like "no key was
    presented," falling through to the same 401 unauthorized it always
    raised for that case.
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        return None

    result = await verify_session(raw_token)
    if result.status != "ok":
        return None

    player_id = await asyncio.to_thread(_lookup_linked_player_sync, result.account_id)
    if player_id is None:
        return None

    return Principal(player_id=player_id, account_id=result.account_id, source="session")


def new_rate_limit_bucket(max_tracked: int = _DEFAULT_MAX_TRACKED) -> _BoundedHits:
    """One independent rate-limit budget. Call this once per call site
    (or once per limiter a call site needs, e.g. checkin_api.py's
    shared address bucket) at module import time and reuse the same
    instance across requests -- never construct one per-request, or
    the "bounded" cap and the whole point of tracking hits over time
    is lost.
    """
    return _BoundedHits(max_tracked=max_tracked)


# ---- the dependency itself ---------------------------------------------

def require_api_key_principal(
    *,
    pre_auth_limiter: _BoundedHits | None = None,
    pre_auth_rate_limit_detail: str = "rate limited",
    post_auth_limiter: _BoundedHits | None = None,
) -> Callable[[Request], Awaitable[Principal]]:
    """Build a FastAPI dependency that resolves the caller's X-API-Key
    header to a Principal, raising the right HTTPException otherwise.

    Returns a callable of (request) -> Principal, suitable for
    `Depends(...)` (or, equally, a direct `await` -- there is nothing
    Depends-specific about it beyond taking a single Request argument).
    A callable is returned rather than resolving directly, because the
    five call sites are not all configured identically -- see the
    module docstring's list of what differs -- and each therefore
    builds its own dependency, once, at import time:

    - app/join_api.py, app/nodes_api.py, app/checkin_api.py (the three
      byte-for-byte-identical originals): both rate limiters on,
      default "rate limited" message on both. Each module still passes
      its OWN pre_auth_limiter/post_auth_limiter instances (see
      new_rate_limit_bucket()) rather than sharing one across modules.
    - app/mc_api.py's POST /api/mc/status: pre_auth_limiter set (its
      own instance, replacing the old _status_attempts dict),
      pre_auth_rate_limit_detail="too many attempts, try again later"
      (that endpoint's original, different message),
      post_auth_limiter=None -- it never had a post-auth per-key limit.
    - app/api.py's POST /api/mc/ingest: pre_auth_limiter=None and
      post_auth_limiter=None -- it never had either of these dict-based
      limiters; its own McIngestor.rate_limit_ok() stays where it is,
      called separately after this dependency resolves, since that is
      ingest-specific rate limiting on a different budget entirely
      (see the module docstring).

    Pre-auth limiting always uses settings.mc_status_rate_limit_attempts/
    window_seconds, and post-auth limiting always uses
    settings.node_api_rate_limit_attempts/window_seconds -- both
    exactly the settings every original copy of this already reused
    rather than inventing new ones, per app/nodes_api.py's own
    docstring on why.
    """

    async def _dependency(request: Request) -> Principal:
        if pre_auth_limiter is not None:
            ip = get_client_ip(request)
            if pre_auth_limiter.limited(
                ip,
                limit=settings.mc_status_rate_limit_attempts,
                window=settings.mc_status_rate_limit_window_seconds,
            ):
                raise HTTPException(status_code=429, detail=pre_auth_rate_limit_detail)

        raw_key = request.headers.get("X-API-Key", "")
        if not raw_key:
            # No key at all -- try a session cookie before giving up.
            # See _try_session_principal's own docstring and this
            # module's docstring for the full reasoning; this branch is
            # the ENTIRE session-fallback addition; everything below it
            # in this function is the original, untouched key path.
            session_principal = await _try_session_principal(request)
            if session_principal is not None:
                return session_principal
            raise HTTPException(status_code=401, detail="unauthorized")

        ingestor = request.app.state.mc_ingestor
        auth = await ingestor.authenticate(raw_key)
        if auth.status in ("not_found", "revoked"):
            # Generic message for both -- don't reveal whether a key exists.
            raise HTTPException(status_code=401, detail="unauthorized")
        if auth.status == "disabled":
            raise HTTPException(status_code=403, detail="forbidden")

        if post_auth_limiter is not None:
            key_hash = hash_secret(raw_key)
            if post_auth_limiter.limited(
                key_hash,
                limit=settings.node_api_rate_limit_attempts,
                window=settings.node_api_rate_limit_window_seconds,
            ):
                raise HTTPException(status_code=429, detail="rate limited")

        assert auth.player_id is not None  # only "not_found" ever leaves this unset, and that already raised above
        return Principal(player_id=auth.player_id)

    return _dependency
