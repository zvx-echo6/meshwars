"""FastAPI router for provider (OAuth) sign-in: GET /auth/{provider}/start
and GET /auth/{provider}/callback drive app/oauth.py's provider table
through an actual authorization-code-with-PKCE round trip, and
implement the callback decision tree that decides whether a provider
identity logs someone in, links onto an account, or has to wait for a
person to choose (resolve_oauth_callback below). GET /auth/providers
tells the frontend which providers are actually enabled, so it never
renders a dead sign-in button for one that isn't configured. GET
/api/account/pending, POST /api/account/pending/create, and POST
/api/account/pending/link are how a parked ("pending") identity from
case 4 is described to a person and then redeemed.

POST /auth/email/start and GET /auth/email/callback are a second,
independent way to reach an identity: passwordless sign-in by a mailed
single-use link, rather than an OAuth provider's own consent screen.
Independent of app/oauth.py's provider table (see app/email_login.py's
own module docstring for why), but sharing everything past "here is an
identity" -- the account model, and resolve_oauth_callback() itself --
with every provider above. See that pair of routes' own docstrings, and
this module's "email sign-in (magic link)" section comment, for the
full shape.

---- browser redirect vs. JSON: who each response shape is for -----------

GET /auth/{provider}/callback is where a real browser lands straight off
a provider's own consent screen -- by default it now RESPONDS WITH A
REDIRECT (to /account, /link, or /join with an error, depending on the
outcome -- see oauth_callback's own docstring for the full mapping),
because a person completing a sign-in has nowhere to go if the response
is a bare JSON body. The original JSON-bodied response this route
always returned is still available, at `?format=json` -- see
_wants_json's own docstring for exactly who that's for (this module's
own tests, and any non-browser caller) and why a real sign-in never
carries that param itself.

---- case 4's pending token: an HttpOnly cookie, not a query string -------

A case-4 ("pending") outcome hands the browser a bearer-shaped token
that has to survive one more redirect (to /link) and then get spent by
one of the two redemption routes below. See _set_pending_cookie's own
docstring for the full reasoning; the short version is that a query
string ends up in Referer headers, browser history, and access logs,
none of which a bearer token belongs in, while an HttpOnly cookie
avoids all three and is exactly the same trade this app's own session
cookie already makes. GET /api/account/pending reads that cookie
server-side to describe the pending identity (provider, masked email)
for /link to render, without the raw token ever reaching page
JavaScript; POST /api/account/pending/{create,link} read it the same
way to redeem it (see _resolve_pending_token below, which also accepts
a `pending_token` JSON body field as a fallback for a non-browser
caller or this module's own tests).

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
from .db import WriteSession, connect
from .email_login import (
    EmailSendError,
    email_login_enabled,
    looks_like_email,
    normalize_email,
    send_magic_link_email,
)
from .mc_ingest import hash_secret
from .oauth import (
    PROVIDERS,
    PROVIDER_LABELS,
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

# Carries case 4's pending token from the callback redirect to the
# decision page (frontend/link.html) and on to whichever of
# POST /api/account/pending/{create,link} it submits to -- see this
# module's own "case 4: the browser redirect" section below for why a
# cookie, not a query string. Path="/" (not scoped narrower, unlike
# _STATE_COOKIE_NAME/_VERIFIER_COOKIE_NAME above): it has to reach both
# /link and the /api/account/pending/* routes, which do not share a
# path prefix with each other.
_PENDING_COOKIE_NAME = "mw_pending_token"

# Where a browser flow lands for each callback outcome. Plain module-
# level constants, not settings -- these are routes this exact app
# serves (app/api.py's mount()), not deployment-configurable addresses
# the way oauth_public_base_url is.
_ACCOUNT_PAGE_PATH = "/account"
_LINK_PAGE_PATH = "/link"
_JOIN_PAGE_PATH = "/join"


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


def _set_pending_cookie(response: Response, *, raw_token: str, expires_at: int, now: int) -> None:
    """Carries a case-4 pending token to the browser as a short-lived,
    HttpOnly cookie rather than a query-string parameter on the
    redirect to /link -- the choice this change actually had to make,
    called out explicitly since a URL is not a safe place for a
    bearer-shaped secret: a query string is copied verbatim into the
    Referer header of any request /link's own page makes to a
    third-party resource, into browser history, and into any access log
    (this app's own reverse proxy included) that records the request
    line rather than just the path. An HttpOnly cookie hits none of
    those: it is never put in Referer (cookies aren't), it is not part
    of what a "copy link" or a history entry captures, and it is
    unreadable from page JavaScript -- the exact same trade
    SESSION_COOKIE_NAME already makes for the session token itself
    (app/sessions.py's module comment on HttpOnly). The tradeoff, spelled
    out for the same reason _set_flow_cookies' sibling functions spell
    theirs out: /link's own page script cannot read this value directly
    either -- but it never needs to. GET /api/account/pending reads the
    cookie server-side to describe the pending identity (provider,
    masked email) for display, and POST /api/account/pending/{create,link}
    read it server-side too (see _resolve_pending_token below) to
    redeem it -- the raw token itself never has to reach client-side
    script at all.

    max_age is capped at the token's own remaining lifetime (expires_at
    - now) rather than a fixed duration: this cookie must never outlive
    the account_pending_identity row it names, or a browser could hold a
    cookie pointing at an already-expired token indefinitely.
    """
    response.set_cookie(
        _PENDING_COOKIE_NAME,
        raw_token,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.account_session_cookie_secure,
        max_age=max(0, expires_at - now),
    )


def _clear_pending_cookie(response: Response) -> None:
    """Attributes must match _set_pending_cookie's exactly -- same
    reasoning as _clear_flow_cookies above. Called once a pending token
    is redeemed (pending_create/pending_link below) or replaced by a
    fresh callback -- single-use, same as the flow cookies.
    """
    response.delete_cookie(
        _PENDING_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.account_session_cookie_secure,
    )



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


# How long a just-expired or just-consumed row in a hashed-single-use-
# ticket table is kept around before _sweep_stale_rows below deletes it
# -- long enough that an operator glancing at the table mid-incident (or
# this module's own tests) still sees a row that finished a moment ago,
# short enough that the table never grows unbounded in normal operation.
_SWEEP_GRACE_SECONDS = 3600  # 1 hour

# The only two tables this sweep is ever run against -- both hashed,
# single-use, TTL'd tickets with the exact same three lifecycle columns
# (expires_at, consumed_at). Asserted in _sweep_stale_rows below rather
# than trusted, since `table` is interpolated directly into the SQL
# text (there is no parameter placeholder for an identifier) -- every
# call site in this module passes a literal from this tuple, never
# anything derived from a request.
_SWEEPABLE_TABLES = ("account_pending_identity", "email_login_token")


def _sweep_stale_rows(conn: sqlite3.Connection, table: str, now: int) -> None:
    """Opportunistic cleanup for account_pending_identity and
    email_login_token -- both accumulate a row on every OAuth case-4
    callback / every POST /auth/email/start, most of which are either
    redeemed once (consumed_at set) or simply abandoned (left to expire)
    and then never touched again. Nothing before this change ever
    deleted a row from either table.

    No cron, no scheduled job: this is called inline, in the SAME
    transaction, every time a fresh row is written to either table (see
    resolve_oauth_callback's case 4 below, and POST /auth/email/start) --
    the write that is already happening is the trigger, so a deployment
    that sees a login attempt once a week sweeps once a week, and one
    that never sees a single attempt never runs this at all. That keeps
    the cost proportional to actual traffic on the exact table being
    grown, rather than a fixed-interval job that has to exist (and be
    monitored, and survive a restart) even when there is nothing to
    clean.

    Deletes rows that are BOTH past their usefulness (expired, or
    already consumed) AND past _SWEEP_GRACE_SECONDS since that happened
    -- see that constant's own comment for why the grace period exists
    at all. A single indexed-by-nothing DELETE with a WHERE clause is
    cheap here specifically because this sweep keeps the table small in
    the first place; it would not scale the same way against a table
    this mechanism did not already keep bounded.
    """
    assert table in _SWEEPABLE_TABLES
    cutoff = now - _SWEEP_GRACE_SECONDS
    conn.execute(
        f"DELETE FROM {table} WHERE expires_at < ? OR (consumed_at IS NOT NULL AND consumed_at < ?)",
        (cutoff, cutoff),
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
    _sweep_stale_rows(conn, "account_pending_identity", now)
    return {"case": "pending", "raw_token": raw_token, "expires_at": expires_at}


# ---- routes ---------------------------------------------------------------


@router.get("/auth/providers")
async def list_providers() -> JSONResponse:
    """Which providers are actually reachable right now -- the frontend
    (frontend/join.js's sign-in section, frontend/link.js) calls this
    instead of hardcoding a provider list, so an unconfigured provider
    (no client id/secret, or no oauth_public_base_url -- see
    provider_enabled() in app/oauth.py) is never rendered as a dead
    button that 404s the moment someone clicks it. Iterates PROVIDERS in
    table order (github today; google/discord/apple fall in
    automatically once each has its own Provider(...) entry there, with
    no route or frontend change needed here) and keeps only the ones
    provider_enabled() actually approves.

    Email sign-in is appended last, exactly once, when
    email_login_enabled() (app/email_login.py) says the SMTP settings
    are actually configured -- it is not a PROVIDERS table entry (see
    app/email_login.py's own module docstring for why), so it can't fall
    out of the comprehension above the way a future OAuth provider
    would; this is the one place that has to know about it explicitly.
    frontend/join.js's setupSignIn() and frontend/link.js's loadPending()
    both special-case the name "email" in this list to render a form
    (address + submit) instead of the plain `/auth/{name}/start` link
    every OTHER entry here gets -- there is no GET .../start redirect
    for email at all, POST /auth/email/start below is a different shape
    entirely.
    """
    providers = [
        {"name": prov.name, "label": PROVIDER_LABELS.get(prov.name, prov.name)}
        for prov in PROVIDERS.values()
        if provider_enabled(prov)
    ]
    if email_login_enabled():
        providers.append({"name": "email", "label": "Email"})
    return JSONResponse({"providers": providers}, status_code=200)


# ---- email sign-in (magic link) --------------------------------------------
#
# POST /auth/email/start mails a single-use link; GET /auth/email/callback
# redeems it. Independent of the OAuth provider table above (see
# app/email_login.py's own module docstring for why this is not a
# Provider(...) entry), but reaching for the exact same account model
# and the exact same resolve_oauth_callback() decision tree once an
# identity is in hand -- provider="email", subject/email=the token's own
# normalized address, email_verified=True. That last one is not a
# simplification: clicking a link mailed to an address IS this app's
# proof that whoever clicked it controls that address, the same role a
# provider's own consent screen plays for GitHub/Google/etc, so
# email_verified=True here is a genuine fact, not an assumed one.

_email_start_ip_limiter = new_rate_limit_bucket()
_email_start_addr_limiter = new_rate_limit_bucket()

# The one response POST /auth/email/start ever returns once a request
# has passed rate limiting and shape validation -- see that route's own
# docstring for why this is a single constant, never built differently
# depending on whether the address matched an account, or whether the
# mail actually went out.
_EMAIL_START_RESPONSE_BODY = {
    "ok": True,
    "message": "If that address can sign in, a link is on its way — check your inbox.",
}


@router.post("/auth/email/start")
async def email_start(request: Request) -> JSONResponse:
    """Mails a single-use sign-in link to the posted address, if email
    sign-in is configured at all (email_login_enabled() --
    app/email_login.py) -- 404s otherwise, the same "indistinguishable
    from not existing" contract a disabled OAuth provider's routes
    already use (see oauth_start's own docstring).

    ---- no account enumeration ------------------------------------------

    This endpoint takes an arbitrary address from an unauthenticated
    caller and triggers an outbound mail send -- the response it gives
    back must never depend on whether that address belongs to an
    existing account (an account is not even looked up here -- the
    token is issued and mailed unconditionally) or on whether the send
    itself succeeded (see app/email_login.py's EmailSendError -- caught
    below, logged, and otherwise invisible to the caller). Every path
    past rate limiting and shape validation returns the exact same
    _EMAIL_START_RESPONSE_BODY. The two things that DO get a different
    response -- rate limiting (429) and an obviously malformed address
    (400) -- are not enumeration risks: neither one depends on whether
    the address has an account, only on the request's own shape/pace.

    ---- rate limiting -----------------------------------------------------

    Two independent budgets, both must pass -- per source IP
    (settings.email_login_start_ip_rate_limit_*) and per target address
    (settings.email_login_start_address_rate_limit_*) -- see those
    settings' own comment in app/config.py for why both are needed:
    this is the most abusable surface this feature adds, an
    unauthenticated endpoint that triggers a real outbound send.
    """
    if not email_login_enabled():
        return JSONResponse({"error": "not found"}, status_code=404)

    ip = get_client_ip(request)
    if _email_start_ip_limiter.limited(
        ip,
        limit=settings.email_login_start_ip_rate_limit_attempts,
        window=settings.email_login_start_ip_rate_limit_window_seconds,
    ):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        body = None
    raw_email = body.get("email") if isinstance(body, dict) else None
    if not isinstance(raw_email, str) or not raw_email:
        return JSONResponse({"error": "email is required"}, status_code=400)

    email = normalize_email(raw_email)
    if not looks_like_email(email):
        # A shape problem, never an account-existence one -- see this
        # route's own "no account enumeration" section above for why a
        # distinct response here is safe.
        return JSONResponse({"error": "invalid email address"}, status_code=400)

    if _email_start_addr_limiter.limited(
        email,
        limit=settings.email_login_start_address_rate_limit_attempts,
        window=settings.email_login_start_address_rate_limit_window_seconds,
    ):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_secret(raw_token)
    now = int(time.time())
    expires_at = now + settings.email_login_token_lifetime_seconds
    async with WriteSession() as conn:
        conn.execute(
            "INSERT INTO email_login_token(token_hash, email, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token_hash, email, now, expires_at),
        )
        _sweep_stale_rows(conn, "email_login_token", now)

    link_url = f"{settings.oauth_public_base_url.rstrip('/')}/auth/email/callback?token={raw_token}"
    try:
        await send_magic_link_email(email, link_url)
    except EmailSendError:
        # Logged inside send_magic_link_email() itself; never surfaced
        # here -- see this route's own "no account enumeration" section.
        pass

    return JSONResponse(_EMAIL_START_RESPONSE_BODY, status_code=200)


@router.get("/auth/email/callback")
async def email_callback(request: Request) -> Response:
    """Completes the magic-link flow POST /auth/email/start began:
    consumes the token (single-use -- unknown, expired, or already-
    consumed all collapse to the same generic failure, mirroring
    oauth_callback's own "don't reveal which part failed" state/PKCE
    check) and then feeds the token's own normalized address into the
    EXACT SAME callback decision tree resolve_oauth_callback() above
    already implements for every OAuth provider -- see this module's own
    "email sign-in" section comment for the provider="email" identity
    shape and why email_verified=True is a genuine fact here, not an
    assumption.

    Response shape matches oauth_callback exactly -- login/linked/
    auto_linked go to /account with a session cookie, pending goes to
    /link with the pending cookie, any failure goes back to /join with
    a short auth_error code -- because both routes share
    _respond_to_callback_outcome (login/pending/error) and
    _callback_error_response (error only) rather than each building
    these responses by hand. The ?format=json escape hatch
    (_wants_json) is honored the same way too, for this module's own
    tests -- a real mailed link never carries it.

    404s when email sign-in is not configured at all
    (email_login_enabled()), same as every route above.
    """
    if not email_login_enabled():
        return JSONResponse({"error": "not found"}, status_code=404)

    raw_token = request.query_params.get("token")
    if not raw_token:
        return _callback_error_response(
            request,
            message="invalid or expired sign-in link",
            redirect_code="invalid_session",
            status_code=400,
        )

    # Resolved BEFORE the write transaction below opens, same ordering
    # oauth_callback uses and for the same reason (see
    # _resolve_current_account_id's own docstring): verify_session() can
    # itself write (sliding-expiry's touch, app/sessions.py's
    # _maybe_touch) through its own separate WriteSession, and this
    # app's write lock (app/db.py's WriteSession) is not reentrant --
    # calling it from inside an already-open WriteSession block would
    # deadlock the request against itself.
    current_account_id = await _resolve_current_account_id(request)
    now = int(time.time())

    async with WriteSession() as conn:
        token_hash = hash_secret(raw_token)
        row = conn.execute(
            "SELECT email, expires_at, consumed_at FROM email_login_token WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None or row["consumed_at"] is not None or row["expires_at"] <= now:
            return _callback_error_response(
                request,
                message="invalid or expired sign-in link",
                redirect_code="invalid_session",
                status_code=400,
            )

        conn.execute("UPDATE email_login_token SET consumed_at = ? WHERE token_hash = ?", (now, token_hash))
        _sweep_stale_rows(conn, "email_login_token", now)

        identity = ProviderIdentity(subject=row["email"], email=row["email"], email_verified=True)
        outcome = resolve_oauth_callback(
            conn,
            provider_name="email",
            identity=identity,
            current_account_id=current_account_id,
            now=now,
        )

    return await _respond_to_callback_outcome(request, outcome=outcome, identity=identity, now=now)


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


def _wants_json(request: Request) -> bool:
    """Whether this callback request should get the old, machine-shaped
    JSON response instead of the browser redirect the route now sends
    by default -- see oauth_callback's own docstring for the full
    reasoning. `?format=json` on the request itself, never anything
    provider-controlled: this route's real callers are (a) a browser,
    landing here via a 302 from the provider that this app has no say
    over the query string of beyond what it put in redirect_uri in the
    first place (and redirect_uri has to match the provider's own
    registered value to the byte -- see redirect_uri_for()'s own
    comment in app/oauth.py -- so it cannot vary per request), and (b) a
    test or script hitting this route directly, which can put whatever
    it wants on the URL. A real sign-in never carries this param; a test
    or a future bot/CLI client that wants the old machine-readable body
    back sets it explicitly.
    """
    return request.query_params.get("format") == "json"


def _callback_error_response(
    request: Request, *, message: str, redirect_code: str, status_code: int
) -> Response:
    """One error outcome, shaped for whichever caller asked for it --
    see _wants_json's own docstring. The JSON shape and status code are
    exactly what this route always returned for each of these failures;
    the redirect shape is new: send a person back to the sign-in page
    (before this change it never sent them anywhere -- a raw JSON error
    body is fine for a test's assertion, not for a person who just
    clicked "Sign in with GitHub"), with a short, non-sensitive `auth_error`
    code in the query string frontend/join.js reads to show a message.
    That code is deliberately an enum-like word (see the call sites
    below), never the raw exception text or provider response -- a
    query string lands in browser history and any access log that
    records the request line, so nothing sensitive belongs in it; a
    generic reason word is the same class of information a "?error=..."
    query param already carries on countless other sites and carries no
    secret.
    """
    if _wants_json(request):
        resp = JSONResponse({"error": message}, status_code=status_code)
    else:
        resp = RedirectResponse(f"{_JOIN_PAGE_PATH}?auth_error={redirect_code}", status_code=302)
    _clear_flow_cookies(resp)
    return resp


async def _respond_to_callback_outcome(
    request: Request, *, outcome: dict, identity: ProviderIdentity, now: int
) -> Response:
    """Turns resolve_oauth_callback()'s outcome dict into the actual
    response -- factored out of oauth_callback below (which it still
    drives, unchanged in behavior) so GET /auth/email/callback can share
    it byte-for-byte rather than reimplementing this mapping a second
    time. resolve_oauth_callback() itself is already fully
    provider-agnostic (see its own docstring); everything past the
    decision tree -- which case gets which redirect, which cookie,
    whether a session gets issued -- is identical regardless of whether
    the identity came from an OAuth provider's callback or a magic-link
    token, which is exactly what makes sharing this safe.

    Does NOT touch the OAuth flow cookies (mw_oauth_state /
    mw_oauth_pkce_verifier) -- oauth_callback clears those itself, right
    after calling this, via _clear_flow_cookies; the email callback has
    no flow cookie of its own to clear at all, since it never sets one
    (see GET /auth/email/callback's own docstring).
    """
    want_json = _wants_json(request)

    if outcome["case"] == "pending":
        if want_json:
            return JSONResponse(
                {
                    "result": "pending",
                    "pending_token": outcome["raw_token"],
                    "expires_at": outcome["expires_at"],
                    "email": identity.email,
                    "email_verified": identity.email_verified,
                },
                status_code=200,
            )
        resp = RedirectResponse(_LINK_PAGE_PATH, status_code=302)
        _set_pending_cookie(resp, raw_token=outcome["raw_token"], expires_at=outcome["expires_at"], now=now)
        return resp

    # "login" (case 1) and "auto_linked" (case 3) both issue a fresh
    # session. "linked" (case 2) deliberately does NOT -- the caller
    # already had a valid session (current_account_id came straight from
    # it), so reissuing one here would be pointless at best and would
    # invite a subtle bug at worst (a stale reference to the OLD token
    # somewhere still expecting it to work).
    account_id = outcome["account_id"]
    if want_json:
        resp = JSONResponse({"result": outcome["case"], "account_id": account_id}, status_code=200)
    else:
        resp = RedirectResponse(_ACCOUNT_PAGE_PATH, status_code=302)
    if outcome["case"] in ("login", "auto_linked"):
        raw_session_token = await create_session(
            account_id, user_agent=request.headers.get("user-agent"), ip=get_client_ip(request)
        )
        set_session_cookie(resp, raw_session_token)
    return resp


@router.get("/auth/{provider}/callback")
async def oauth_callback(provider: str, request: Request) -> Response:
    """Completes the flow /auth/{provider}/start began: verifies state
    and PKCE, exchanges the code, fetches the provider's identity, and
    runs resolve_oauth_callback() above to decide what happens to it.

    ---- redirect, not a JSON body, by default -------------------------

    This is where a real person's browser lands straight from the
    provider's own consent screen -- returning raw JSON here used to mean
    a person who just finished signing in landed on a blank page of
    `{"result": "login", "account_id": 4}`, which is correct for a
    machine and wrong for a browser. The default response now for every
    outcome is a 302 that takes a real visitor somewhere that renders:

      - login / linked / auto_linked -> the session cookie is set (same
        as before) and the browser is sent to /account, so a completed
        sign-in lands on the page that shows it worked.
      - pending (case 4) -> the pending token is handed to the browser as
        a short-lived HttpOnly cookie (_set_pending_cookie -- see its own
        docstring for why a cookie and not a query string) and the
        browser is sent to /link, the decision screen that offers
        "create a new account" or "sign in with a method you already
        use" for an identity this app has never seen before.
      - any failure (provider declined, state/PKCE mismatch, provider
        HTTP error) -> _callback_error_response above sends the browser
        back to /join with a short `auth_error` code it can display next
        to the sign-in button, rather than a bare JSON error body.

    The original JSON-bodied response (tested exhaustively in
    tests/test_oauth_api.py, which drives this route directly rather
    than through a real browser) is still available at
    `?format=json` -- see _wants_json's own docstring for exactly who
    that is for and why a real sign-in never carries it. Every failure
    path clears the flow cookies before returning either shape -- see
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
        return _callback_error_response(
            request,
            message="oauth provider returned an error",
            redirect_code="provider_declined",
            status_code=400,
        )

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
        return _callback_error_response(
            request,
            message="invalid or expired oauth login attempt",
            redirect_code="invalid_session",
            status_code=400,
        )

    async with httpx.AsyncClient(timeout=10.0) as http_client:
        try:
            token_response = await exchange_code(
                prov, code=code, code_verifier=cookie_verifier, http_client=http_client
            )
            identity = await fetch_identity(prov, token_response, http_client)
        except OAuthError:
            log.exception("oauth: %s callback failed talking to the provider", provider)
            return _callback_error_response(
                request,
                message="oauth provider error",
                redirect_code="provider_error",
                status_code=502,
            )

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

    resp = await _respond_to_callback_outcome(request, outcome=outcome, identity=identity, now=now)
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


async def _resolve_pending_token(request: Request) -> tuple[str | None, JSONResponse | None]:
    """Where GET /api/account/pending and POST /api/account/pending/
    {create,link} all get the raw pending token from: the HttpOnly
    _PENDING_COOKIE_NAME cookie oauth_callback's redirect sets on a real
    browser flow, PREFERRED, falling back to a `pending_token` field in
    a JSON request body when the cookie is absent. The fallback exists
    for two callers, neither of them a browser holding the cookie: this
    module's own tests (tests/test_oauth_api.py, unchanged by this
    fallback -- see this module's module docstring's "keep the JSON
    behavior available for tests" note) and any non-browser caller that
    received the raw token directly from GET
    /auth/{provider}/callback?format=json's response body instead of
    the cookie a browser gets by default. A browser flow never has to
    (and, since the cookie is HttpOnly, cannot) supply the body field
    itself -- see /link's own frontend/link.js for the POST calls this
    makes with no body at all.

    The body is only ever parsed when no cookie is present, so a
    cookie-carrying POST with a malformed or missing JSON body (which
    is every real browser POST from /link) never fails on that account.
    """
    cookie_token = request.cookies.get(_PENDING_COOKIE_NAME)
    if cookie_token:
        return cookie_token, None
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return None, JSONResponse({"error": "pending_token is required"}, status_code=400)
    raw_token = body.get("pending_token")
    if not isinstance(raw_token, str) or not raw_token:
        return None, JSONResponse({"error": "pending_token is required"}, status_code=400)
    return raw_token, None


# Rate limit on the read below too, alongside the two redemption limiters
# -- GET /api/account/pending falls back to the same cookie-or-body
# token resolution as the redemption routes (a caller could still probe
# it with a guessed token in a JSON body, even though the cookie path a
# real browser takes never guesses anything), and it reports back
# (200 vs. 404) whether a token is valid, which is the same "with no
# limit at all this is a guessing oracle" reasoning the module comment
# above already gives for the redemption routes.
_pending_read_addr_limiter = new_rate_limit_bucket()


@router.get("/api/account/pending")
async def pending_get(request: Request) -> JSONResponse:
    """Describes the pending identity a case-4 callback parked, for
    frontend/link.js to render the decision screen ("You signed in with
    GitHub as j***@example.com. We haven't seen this identity before.")
    without the raw pending token ever reaching page JavaScript -- see
    _set_pending_cookie's own docstring for why the token itself is
    HttpOnly. Read-only and does not consume the token; only
    pending_create/pending_link below do that. 404 (not 403, unlike the
    redemption routes below) for a missing/unknown/expired/consumed
    token -- there is nothing to redeem yet at this point, so "not
    found" is the honest status, not "forbidden."
    """
    ip = get_client_ip(request)
    if _pending_read_addr_limiter.limited(
        ip,
        limit=settings.account_link_key_rate_limit_attempts,
        window=settings.account_link_key_rate_limit_window_seconds,
    ):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    raw_token, err = await _resolve_pending_token(request)
    if err is not None:
        return JSONResponse({"error": "no pending sign-in"}, status_code=404)

    now = int(time.time())
    conn = connect()
    try:
        row, load_err = _load_pending(conn, raw_token, now)
    finally:
        conn.close()
    if load_err is not None:
        return JSONResponse({"error": "no pending sign-in"}, status_code=404)

    return JSONResponse(
        {
            "provider": row["provider"],
            "provider_label": PROVIDER_LABELS.get(row["provider"], row["provider"]),
            "email": _mask_pending_email(row["email"]),
            "email_verified": bool(row["email_verified"]),
            "expires_at": row["expires_at"],
        },
        status_code=200,
    )


def _mask_pending_email(email: str | None) -> str | None:
    """Same masking app/account_api.py's own _mask_email() applies to a
    LINKED identity's email -- duplicated rather than imported, since
    that function lives in a module this router does not otherwise
    depend on and the rule is three lines: never show a pending
    identity's full address to the page that is about to ask "is this
    you?" any more than an already-linked one gets shown in full.
    """
    if not email or "@" not in email:
        return None
    local, _, domain = email.partition("@")
    masked_local = local[0] + "***" if local else "***"
    return f"{masked_local}@{domain}"


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

    raw_token, err = await _resolve_pending_token(request)
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
    # Single-use, same as the flow cookies -- harmless to clear even when
    # the token actually arrived via the JSON-body fallback rather than
    # this cookie (there is then nothing to clear, and delete_cookie on
    # an absent cookie is a no-op).
    _clear_pending_cookie(resp)
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

    raw_token, err = await _resolve_pending_token(request)
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

    resp = JSONResponse({"result": "linked", "account_id": session.account_id}, status_code=200)
    _clear_pending_cookie(resp)  # see pending_create's matching comment above
    return resp
