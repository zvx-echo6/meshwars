"""FastAPI router for provider (OAuth) sign-in: GET /auth/{provider}/start
and GET /auth/{provider}/callback drive app/oauth.py's provider table
through an actual authorization-code-with-PKCE round trip, and
implement the callback decision tree that decides whether a provider
identity logs someone in, links onto an account, or has to wait for a
person to choose (resolve_oauth_callback below). POST
/api/account/pending/create and POST /api/account/pending/link are the
two ways a parked ("pending") identity from that last case is ever
redeemed.

---- CSRF/state and PKCE storage: cookies, not a table -------------------

The `state` value and the PKCE `code_verifier` generated on
/auth/{provider}/start have to survive the round trip to the provider
and back to /auth/{provider}/callback. They are carried in two
short-lived, HttpOnly, SameSite=Lax cookies (mw_oauth_state,
mw_oauth_pkce_verifier -- see _set_flow_cookies/_clear_flow_cookies
below), scoped to Path=/auth, rather than a server-side table.

This was considered, not assumed: a server-side "oauth_flow" table
(keyed by state, holding the verifier, with its own expiry) is the
other obvious design, and there is a real case for it -- it would work
even if a browser dropped the cookie mid-flow (e.g. a strict tracking
protection setting) and it would not depend on SameSite=Lax's specific
allowance for top-level GET navigation. It was rejected here because:

1. SameSite=Lax is exactly built for this shape. Per the SameSite
   spec, a Lax cookie IS attached to a cross-site top-level navigation
   using a "safe" method -- and a provider's OAuth redirect back to
   /auth/{provider}/callback is precisely that: the provider's own
   domain sends the browser's TOP-LEVEL location to a URL on THIS
   site, via a plain 302, which the browser treats as a top-level GET
   navigation regardless of which site initiated it. That is the one
   case Lax exists to still allow (unlike Strict, which would drop the
   cookie on exactly this cross-site-initiated navigation and break
   the whole flow). app/account_api.py's own module docstring reasons
   through this same SameSite=Lax contract for a different purpose
   (blocking cross-site POSTs); this is the read of the same contract
   that makes the cookie approach viable for a GET redirect landing.
2. A server-side table buys correctness in exchange for a cleanup
   story: every login attempt -- including one abandoned at the
   provider's consent screen, or one that never returns at all --
   leaves a row that has to be swept on its own schedule, the same
   operational cost app/db.py's join_token/account_pending_identity
   already carry for a genuinely different reason (they are redeemed
   well after the request that created them, potentially minutes
   later, by a DIFFERENT request that can't carry state in a cookie at
   all). Here, the entire lifetime of the state/verifier is one
   redirect round trip through the SAME browser tab -- a cookie is
   exactly the right amount of durability for that, and needs no sweep
   because it expires itself (max_age below) and is deleted outright
   on every callback response, success or failure.
3. The failure mode of a dropped cookie (strict tracking protection,
   or a browser configured to block third-party/reduced cookies) is
   "the login attempt fails and the person tries again," not a
   security gap -- app/oauth_api.py's callback treats a missing state
   or verifier cookie exactly like a state mismatch (see
   oauth_callback below), which is the same "start over" outcome a
   server-side table's own expired-row case would produce anyway.

If this reasoning turns out to be wrong for a real deployment (a
tracking-protection browser turns out to be a meaningful fraction of
this app's users, say), the fix is a server-side table with the exact
shape join_token already demonstrates -- not a rewrite of the flow
above it.
"""
from __future__ import annotations

import logging
import secrets
import sqlite3
import time

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from .auth import new_rate_limit_bucket
from .client_ip import get_client_ip
from .config import settings
from .db import WriteSession
from .mc_ingest import hash_secret
from .oauth import (
    OAuthError,
    ProviderIdentity,
    build_authorize_url,
    exchange_code,
    fetch_identity,
    generate_pkce_pair,
    generate_state,
    get_provider,
    provider_enabled,
)
from .sessions import (
    SESSION_COOKIE_NAME,
    SessionPrincipal,
    create_session,
    require_session,
    set_session_cookie,
    verify_session,
)

log = logging.getLogger("oauth_api")

router = APIRouter()

_STATE_COOKIE_NAME = "mw_oauth_state"
_VERIFIER_COOKIE_NAME = "mw_oauth_pkce_verifier"


def _set_flow_cookies(response: Response, *, state: str, code_verifier: str) -> None:
    # Path=/auth: these cookies have no business being sent anywhere
    # outside this router's two routes -- scoping them narrowly means a
    # completely unrelated request can never even carry them, let alone
    # depend on them. Secure/HttpOnly/SameSite reuse
    # settings.account_session_cookie_secure -- the same "are we behind
    # real TLS or a local dev loop" flag app/sessions.py's own session
    # cookie already reads, rather than a second knob for what is the
    # same underlying deployment fact.
    common = dict(
        path="/auth",
        httponly=True,
        samesite="lax",
        secure=settings.account_session_cookie_secure,
        max_age=settings.oauth_state_cookie_lifetime_seconds,
    )
    response.set_cookie(_STATE_COOKIE_NAME, state, **common)
    response.set_cookie(_VERIFIER_COOKIE_NAME, code_verifier, **common)


def _clear_flow_cookies(response: Response) -> None:
    """Called on EVERY /auth/{provider}/callback response, success or
    failure -- these cookies are single-use by design (see
    resolve_oauth_callback: a `code` can only ever be exchanged once
    regardless), so there is never a reason to let them survive past
    the one callback that consumes them. Attributes must match
    _set_flow_cookies' exactly, same reason app/sessions.py's own
    clear_session_cookie docstring gives for its matching delete_cookie
    call.
    """
    common = dict(path="/auth", httponly=True, samesite="lax", secure=settings.account_session_cookie_secure)
    response.delete_cookie(_STATE_COOKIE_NAME, **common)
    response.delete_cookie(_VERIFIER_COOKIE_NAME, **common)


async def _resolve_current_account_id(request: Request) -> int | None:
    """Case 2 of the callback decision tree needs to know whether THIS
    request already carries a valid session -- but, unlike every route
    in app/account_api.py, a callback with no session (or an
    expired/revoked one) is not an error here, just "not case 2, try
    case 3." So this reads app/sessions.py's verify_session() directly
    rather than going through require_session() (which raises 401 on
    exactly the cases this function is supposed to treat as "None, keep
    going").
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        return None
    result = await verify_session(raw_token)
    if result.status != "ok":
        return None
    return result.account_id


# ---- the callback decision tree -----------------------------------------
#
# Implements Step 3's four cases exactly, in order, each one falling
# through to the next only when it doesn't apply. Takes an
# already-open, caller-owned write-transaction connection (conn, from a
# WriteSession the route below holds for the whole decision) so the
# read that decides which case fires and the write that case performs
# are atomic against a second, concurrent callback for the same
# identity -- the same reasoning app/account_api.py's link_key() gives
# for doing its own conflict checks and writes inside one WriteSession
# rather than as two separate round trips.

def _link_identity(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    provider_name: str,
    identity: ProviderIdentity,
    now: int,
    detail_suffix: str = "",
) -> None:
    """Shared by cases 2, 3, and POST /api/account/pending/create's own
    new-account path below -- "attach this provider identity to this
    account" is the exact same two-row write (account_identity +
    account_link_event) no matter which of those three reached it, so
    it is written once here instead of three times with a chance to
    drift.
    """
    conn.execute(
        "INSERT INTO account_identity(provider, subject, account_id, email, email_verified, linked_at, last_login_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (provider_name, identity.subject, account_id, identity.email, int(identity.email_verified), now, now),
    )
    conn.execute(
        "INSERT INTO account_link_event(account_id, kind, detail, actor, created_at) "
        "VALUES (?, 'identity_linked', ?, 'user', ?)",
        (account_id, f"provider={provider_name} subject={identity.subject}{detail_suffix}", now),
    )


def resolve_oauth_callback(
    conn: sqlite3.Connection,
    *,
    provider_name: str,
    identity: ProviderIdentity,
    current_account_id: int | None,
    now: int,
) -> dict:
    """The decision tree itself, deliberately kept free of any
    HTTP/cookie/session concept -- it only ever reads/writes account,
    account_identity, account_link_event, and account_pending_identity,
    which is what makes it testable directly against a bare `conn`
    fixture (tests/test_oauth_api.py), no FastAPI, no TestClient, no
    provider HTTP at all. Returns one of:

      {"case": "login", "account_id": int}        -- case 1
      {"case": "linked", "account_id": int}        -- case 2
      {"case": "auto_linked", "account_id": int}   -- case 3
      {"case": "pending", "raw_token": str, "expires_at": int}  -- case 4

    The caller (oauth_callback below) is responsible for issuing a
    session for "login"/"auto_linked" and for handing the raw pending
    token back to the caller for "pending" -- this function never
    touches account_session or a cookie.
    """
    # ---- case 1: an identity for (provider, subject) already exists --
    # log in to THAT account, no linking decision to make at all.
    row = conn.execute(
        "SELECT account_id FROM account_identity WHERE provider = ? AND subject = ?",
        (provider_name, identity.subject),
    ).fetchone()
    if row is not None:
        account_id = row["account_id"]
        conn.execute(
            "UPDATE account_identity SET last_login_at = ? WHERE provider = ? AND subject = ?",
            (now, provider_name, identity.subject),
        )
        conn.execute("UPDATE account SET last_login_at = ? WHERE account_id = ?", (now, account_id))
        return {"case": "login", "account_id": account_id}

    # ---- case 2: no existing identity, but the caller is already
    # logged in -- link this new identity onto THAT account, keep the
    # existing session (the route below never touches account_session
    # for this case).
    if current_account_id is not None:
        _link_identity(
            conn, account_id=current_account_id, provider_name=provider_name, identity=identity, now=now
        )
        return {"case": "linked", "account_id": current_account_id}

    # ---- case 3: not logged in, but the identity's own email is
    # provider-verified AND matches exactly one existing account's own
    # verified email -- auto-link and log in. An unverified email never
    # reaches this branch at all (email_verified must be true), and an
    # AMBIGUOUS match (more than one candidate account) deliberately
    # does NOT link -- falls straight through to case 4 instead. Two
    # different accounts holding the same verified email is not
    # supposed to be possible in steady state (see app/db.py's
    # account_identity comment: two identities are never auto-merged),
    # but nothing stops it from happening some other way (an operator
    # merge tool that doesn't exist yet, a provider that changes whose
    # email is verified) -- and when it does, picking one silently would
    # be a guess this code has no business making.
    if identity.email_verified and identity.email:
        matches = conn.execute(
            "SELECT DISTINCT account_id FROM account_identity"
            " WHERE email_verified = 1 AND LOWER(email) = LOWER(?)",
            (identity.email,),
        ).fetchall()
        if len(matches) == 1:
            account_id = matches[0]["account_id"]
            _link_identity(
                conn, account_id=account_id, provider_name=provider_name, identity=identity, now=now
            )
            conn.execute("UPDATE account SET last_login_at = ? WHERE account_id = ?", (now, account_id))
            return {"case": "auto_linked", "account_id": account_id}
        # 0 matches (no account holds this email verified) or >=2
        # (ambiguous) -- both fall through to case 4 below.

    # ---- case 4: nothing above applies -- park the identity rather
    # than create an account speculatively. See account_pending_identity's
    # own comment in app/db.py for the full shape/reasoning; this is the
    # ONLY place that table is ever written to.
    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_secret(raw_token)
    expires_at = now + settings.account_pending_identity_lifetime_seconds
    conn.execute(
        "INSERT INTO account_pending_identity"
        "(token_hash, provider, subject, email, email_verified, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (token_hash, provider_name, identity.subject, identity.email, int(identity.email_verified), now, expires_at),
    )
    return {"case": "pending", "raw_token": raw_token, "expires_at": expires_at}


# ---- routes ---------------------------------------------------------------

@router.get("/auth/{provider}/start")
async def oauth_start(provider: str, request: Request) -> Response:
    """Redirects the browser to `provider`'s own authorize screen.
    404s for an unknown or disabled provider (see get_provider/
    provider_enabled in app/oauth.py) -- indistinguishable from the
    route not existing at all, the same contract app/admin_api.py's
    _api_guard already applies to a disabled admin door.
    """
    prov = get_provider(provider)
    if prov is None or not provider_enabled(prov):
        return JSONResponse({"error": "not found"}, status_code=404)

    state = generate_state()
    code_verifier, code_challenge = generate_pkce_pair()
    authorize_url = build_authorize_url(prov, state=state, code_challenge=code_challenge)

    response = RedirectResponse(authorize_url, status_code=302)
    _set_flow_cookies(response, state=state, code_verifier=code_verifier)
    return response


@router.get("/auth/{provider}/callback")
async def oauth_callback(provider: str, request: Request) -> JSONResponse:
    """Completes the flow /auth/{provider}/start began: verifies state
    and PKCE, exchanges the code, fetches the provider's identity, and
    runs resolve_oauth_callback() above to decide what happens to it.

    Every failure path clears the flow cookies before returning -- see
    _clear_flow_cookies' own docstring for why that happens
    unconditionally rather than only on success.
    """
    prov = get_provider(provider)
    if prov is None or not provider_enabled(prov):
        return JSONResponse({"error": "not found"}, status_code=404)

    if request.query_params.get("error"):
        # The provider itself declined (user hit "cancel" on the consent
        # screen, a misconfigured scope, ...) -- never reaches token
        # exchange at all.
        resp = JSONResponse({"error": "oauth provider returned an error"}, status_code=400)
        _clear_flow_cookies(resp)
        return resp

    code = request.query_params.get("code")
    returned_state = request.query_params.get("state")
    cookie_state = request.cookies.get(_STATE_COOKIE_NAME)
    cookie_verifier = request.cookies.get(_VERIFIER_COOKIE_NAME)

    # One generic failure for every way this callback can fail to check
    # out -- a missing code, a missing state or PKCE-verifier cookie (the
    # flow cookies expired, were dropped, or this simply isn't a real
    # continuation of a /start redirect), or a state value that doesn't
    # match what /start actually set. Collapsed into a single message
    # and status code deliberately, the same "don't reveal which part
    # failed" posture app/auth.py's require_api_key_principal() already
    # applies to not_found vs. revoked API keys -- every one of these
    # means the same thing to a caller regardless: start over at
    # /auth/{provider}/start.
    if (
        not code
        or not returned_state
        or not cookie_state
        or not cookie_verifier
        or not secrets.compare_digest(returned_state, cookie_state)
    ):
        resp = JSONResponse({"error": "invalid or expired oauth login attempt"}, status_code=400)
        _clear_flow_cookies(resp)
        return resp

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        try:
            token_response = await exchange_code(
                prov, code=code, code_verifier=cookie_verifier, http_client=http_client
            )
            identity = await fetch_identity(prov, token_response, http_client)
        except OAuthError:
            log.exception("oauth: %s callback failed talking to the provider", provider)
            resp = JSONResponse({"error": "oauth provider error"}, status_code=502)
            _clear_flow_cookies(resp)
            return resp

    current_account_id = await _resolve_current_account_id(request)
    now = int(time.time())

    async with WriteSession() as conn:
        outcome = resolve_oauth_callback(
            conn,
            provider_name=prov.name,
            identity=identity,
            current_account_id=current_account_id,
            now=now,
        )

    if outcome["case"] == "pending":
        resp = JSONResponse(
            {
                "result": "pending",
                "pending_token": outcome["raw_token"],
                "expires_at": outcome["expires_at"],
                "email": identity.email,
                "email_verified": identity.email_verified,
            },
            status_code=200,
        )
        _clear_flow_cookies(resp)
        return resp

    # "login" (case 1) and "auto_linked" (case 3) both issue a fresh
    # session. "linked" (case 2) deliberately does NOT -- the caller
    # already had a valid session (current_account_id came straight from
    # it), so reissuing one here would be pointless at best and would
    # invite a subtle bug at worst (a stale reference to the OLD token
    # somewhere still expecting it to work).
    account_id = outcome["account_id"]
    resp = JSONResponse({"result": outcome["case"], "account_id": account_id}, status_code=200)
    if outcome["case"] in ("login", "auto_linked"):
        raw_session_token = await create_session(
            account_id, user_agent=request.headers.get("user-agent"), ip=get_client_ip(request)
        )
        set_session_cookie(resp, raw_session_token)
    _clear_flow_cookies(resp)
    return resp


# ---- pending identity redemption ------------------------------------------
#
# Two doors onto the same account_pending_identity row, matching the
# choice case 4's response actually offers a caller ("create a new
# account, or sign in with an existing method to link this one"):
# pending_create makes the NEW account; pending_link is case 2's own
# linking write, reachable for someone who is holding a pending token
# but chose to sign in some OTHER way first (an existing session, a
# different provider's own case 1/3, POST /api/account/link-key, ...)
# rather than replaying the same provider's OAuth flow a second time
# while already logged in -- this is what "make the case-2 path work
# for someone who signs in afterward while holding a pending token"
# (Step 3) means in practice: case 2 itself only ever fires from inside
# a live callback for the identity being resolved RIGHT NOW; this route
# is the equivalent write for an identity that was already resolved
# (and parked) by an EARLIER callback.
#
# Both are rate-limited per address, same reasoning
# app/account_api.py's own link-key rate limiter comment gives: a
# pending_token is an arbitrary bearer value accepted in a request
# body, and reports back (via 403 "invalid token" vs. other outcomes)
# whether it was valid -- with no limit at all, that is a guessing
# oracle, whether or not the guesser is logged in.

_pending_create_addr_limiter = new_rate_limit_bucket()
_pending_link_addr_limiter = new_rate_limit_bucket()


def _load_pending(conn: sqlite3.Connection, raw_token: str, now: int) -> tuple[sqlite3.Row | None, JSONResponse | None]:
    """Shared validation for both redemption routes below: (row, None)
    on success, or (None, error_response) on any of the three ways a
    token can fail to redeem -- unknown, already consumed, or expired.
    Mirrors app/join_api.py's join_redeem() validation of join_token
    almost exactly (same three distinct 403 messages, same shape), a
    different single-use hashed ticket with the same lifecycle.
    """
    token_hash = hash_secret(raw_token)
    row = conn.execute(
        "SELECT token_hash, provider, subject, email, email_verified, expires_at, consumed_at "
        "  FROM account_pending_identity WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None, JSONResponse({"error": "invalid token"}, status_code=403)
    if row["consumed_at"] is not None:
        return None, JSONResponse({"error": "token already used"}, status_code=403)
    if row["expires_at"] <= now:
        return None, JSONResponse({"error": "token expired"}, status_code=403)
    return row, None


async def _read_pending_token_body(request: Request) -> tuple[str | None, JSONResponse | None]:
    try:
        body = await request.json()
    except Exception:
        return None, JSONResponse({"error": "bad request"}, status_code=400)
    if not isinstance(body, dict):
        return None, JSONResponse({"error": "bad request"}, status_code=400)
    raw_token = body.get("pending_token")
    if not isinstance(raw_token, str) or not raw_token:
        return None, JSONResponse({"error": "pending_token is required"}, status_code=400)
    return raw_token, None


@router.post("/api/account/pending/create")
async def pending_create(request: Request) -> JSONResponse:
    """Consumes a pending token to create a BRAND NEW account + its
    first identity, then logs in. This is the "create a new account"
    half of case 4's choice -- the other half is pending_link below.
    """
    ip = get_client_ip(request)
    if _pending_create_addr_limiter.limited(
        ip,
        limit=settings.account_link_key_rate_limit_attempts,
        window=settings.account_link_key_rate_limit_window_seconds,
    ):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    raw_token, err = await _read_pending_token_body(request)
    if err is not None:
        return err

    now = int(time.time())
    async with WriteSession() as conn:
        row, err = _load_pending(conn, raw_token, now)
        if err is not None:
            return err

        cur = conn.execute("INSERT INTO account(created_at, last_login_at) VALUES (?, ?)", (now, now))
        account_id = cur.lastrowid

        _link_identity(
            conn,
            account_id=account_id,
            provider_name=row["provider"],
            identity=ProviderIdentity(
                subject=row["subject"], email=row["email"], email_verified=bool(row["email_verified"])
            ),
            now=now,
            detail_suffix=" (new account)",
        )

        conn.execute(
            "UPDATE account_pending_identity SET consumed_at = ? WHERE token_hash = ?",
            (now, row["token_hash"]),
        )

    raw_session_token = await create_session(
        account_id, user_agent=request.headers.get("user-agent"), ip=ip
    )
    resp = JSONResponse({"result": "created", "account_id": account_id}, status_code=200)
    set_session_cookie(resp, raw_session_token)
    return resp


@router.post("/api/account/pending/link")
async def pending_link(
    request: Request, session: SessionPrincipal = Depends(require_session)
) -> JSONResponse:
    """Consumes a pending token to link its identity onto the CALLER'S
    already-logged-in account -- the "sign in with an existing method
    to link this one" half of case 4's choice. Requires a valid session
    (require_session, same as every other app/account_api.py route);
    see this module's own "pending identity redemption" section comment
    above for why this exists as a separate route from pending_create
    rather than folded into oauth_callback's case 2.
    """
    ip = get_client_ip(request)
    if _pending_link_addr_limiter.limited(
        ip,
        limit=settings.account_link_key_rate_limit_attempts,
        window=settings.account_link_key_rate_limit_window_seconds,
    ):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    raw_token, err = await _read_pending_token_body(request)
    if err is not None:
        return err

    now = int(time.time())
    async with WriteSession() as conn:
        row, err = _load_pending(conn, raw_token, now)
        if err is not None:
            return err

        # Defensive, not expected in normal operation: the identity this
        # token names could only already be linked if something else
        # consumed it since -- e.g. this exact token redeemed twice
        # concurrently would already be caught by consumed_at above, so
        # this instead guards a (provider, subject) that got linked some
        # OTHER way (a second, independent login through the same
        # provider that hit case 1/2/3 on its own) while this token sat
        # unconsumed. Refused rather than silently double-linked or
        # silently overwritten.
        existing = conn.execute(
            "SELECT 1 FROM account_identity WHERE provider = ? AND subject = ?",
            (row["provider"], row["subject"]),
        ).fetchone()
        if existing is not None:
            return JSONResponse(
                {"error": "that identity is already linked to an account"}, status_code=409
            )

        _link_identity(
            conn,
            account_id=session.account_id,
            provider_name=row["provider"],
            identity=ProviderIdentity(
                subject=row["subject"], email=row["email"], email_verified=bool(row["email_verified"])
            ),
            now=now,
        )
        conn.execute(
            "UPDATE account_pending_identity SET consumed_at = ? WHERE token_hash = ?",
            (now, row["token_hash"]),
        )

    return JSONResponse({"result": "linked", "account_id": session.account_id}, status_code=200)
