"""FastAPI router for the account layer: who am I, linking an existing
player's API key to an account, session logout, and the account-
security surface (key rotation, password, contact email, identity
unlink) layered on top of it.

Every route here requires a session cookie (app/sessions.py's
require_session() dependency) -- there is no key-authenticated route in
this module, and no OAuth provider callback either; both are separate,
later work. What exists today is what a session, once it exists, can
DO: read its own account (GET /api/account), retrofit an existing
key-only player onto it (POST /api/account/link-key), log out (POST
/api/account/logout[-all]), mint a fresh player API key while revoking
every old one (POST /api/account/rotate-key), set/change/remove a
sign-in password (POST/DELETE /api/account/password), set a contact-only
email address (POST /api/account/contact-email) or copy the account's
own already-verified identity email onto it server-side instead of
typing one (POST /api/account/contact-email/use-identity), and
disconnect a sign-in identity (DELETE /api/account/identity/{provider}). See
app/sessions.py's own module docstring for how a session comes to exist
in the first place -- nothing in this router creates one. The two
routes a mailed link has to reach WITHOUT a session
(GET /auth/password/... does not exist -- password sign-in is POST
/auth/password/start; GET /auth/contact-email/verify) live in
app/oauth_api.py instead, alongside every other unauthenticated
`/auth/*` door -- see that module's own docstring.

---- the "doors" a person can sign in through, and the last-door guard ----

An account can be reached through any number of linked provider
identities (account_identity rows) plus, optionally, one password
(account_password). DELETE /api/account/identity/{provider} and DELETE
/api/account/password both refuse an action that would leave the
account with ZERO doors -- see _door_counts() below, the one place that
counts them, used by both routes and by GET /api/account's own
per-identity "can this be removed" field so the UI never offers a
button that the backend would then refuse.

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

import secrets
import time

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from . import mc_api, results
from .auth import new_rate_limit_bucket
from .client_ip import get_client_ip
from .config import settings
from .db import WriteSession, connect
from .email_login import (
    EmailSendError,
    PURPOSE_VERIFY_CONTACT,
    email_login_enabled,
    looks_like_email,
    normalize_email,
    send_magic_link_email,
)
from .oauth import PROVIDER_LABELS
from .totp import totp_encryption_available
# Reaching into app/checkin.py's underscore-prefixed helpers here is
# deliberate, not a style slip: the four read-only routes at the bottom
# of this module (stats/honors/checkins/checkin-health) exist
# specifically to surface what those functions already compute for a
# player, without re-deriving any of it -- checkin_streak() for a
# player's current streak, and _build_directory_bridge() /
# _resolve_mc_identities() / mc_contact_status() for exactly the
# MeshCore identity resolution a real check-in goes through, so "why
# isn't this crediting me" can never drift from what actually happens
# at award time.
# mc_contact_status() and its own index builder are the one piece of
# genuinely NEW logic in app/checkin.py this pass adds -- they exist so
# a single bound contact's resolution outcome can be reported instead
# of only the bridge's final winners; see that function's own docstring.
#
# This is deliberately NOT imported here at module level. app/checkin.py
# pulls in the full ingest/meshview/MQTT chain (app/meshview_client.py
# -> aiolimiter, httpx, etc) to do its own job, and this is a light,
# session-scoped HTTP router that FastAPI's app wiring imports eagerly
# on every process start -- it should not drag that whole chain in just
# to have the names available. Each function below that needs one of
# these helpers imports it locally, right where it is used.
from .mc_ingest import PROTOCOL as MC_PROTOCOL, hash_secret
from .password_login import PasswordHash, hash_password, verify_password
from .sessions import (
    SessionPrincipal,
    clear_session_cookie,
    require_session,
    revoke_all_sessions,
    revoke_session,
)
# DELETE /api/account below re-verifies a live TOTP code or recovery
# code before it will act, reusing these two functions rather than
# re-implementing either -- see app/totp_api.py's DELETE /api/account/totp
# (totp_disable()) for the existing route this borrows the "prove a
# CURRENT factor before this destructive action proceeds" shape from.
# Module-level, not a local import inside the route: unlike
# app/checkin.py's helpers (kept local because that module drags in the
# whole ingest/meshview/MQTT chain -- see the comment on that import
# above), app/totp_api.py is exactly as light a session-scoped router as
# this one, and it does not import this module back, so there is no
# cycle to avoid by deferring it.
from .totp_api import verify_and_consume_recovery_code, verify_and_consume_totp_code

router = APIRouter()

# 'mt' is exactly as fixed a value as MC_PROTOCOL stands in for 'mc' --
# see app/mc_api.py's own MT_PROTOCOL and app/admin_ops.py's identical
# local constant for why this is a bare literal rather than an import:
# app/ingest.py (the module that would otherwise export it) imports
# app/api.py, which imports app/mc_api.py, so importing it from here
# risks the same cycle those two modules already avoid.
MT_PROTOCOL = "mt"

# Address-keyed rate limit on link-key -- see
# settings.account_link_key_rate_limit_attempts/window_seconds' own
# comment in app/config.py for why this endpoint needs one at all (it's
# a key-guessing oracle without it). This module's own instance, per
# app/auth.py's module-docstring convention: every _BoundedHits budget
# in this codebase is private to the one call site that owns it, never
# shared across modules.
_link_key_addr_limiter = new_rate_limit_bucket()

# Address-keyed rate limit on rotate-key -- see
# settings.account_rotate_key_rate_limit_attempts/window_seconds' own
# comment in app/config.py. Same independent-instance-per-call-site
# convention as _link_key_addr_limiter above.
_rotate_key_addr_limiter = new_rate_limit_bucket()

# Account-keyed rate limit on POST /api/account/contact-email -- caps
# how many verification mails one account can trigger for itself in a
# window. Keyed on account_id (not source IP) because this route is
# already session-authenticated -- there is no anonymous-caller
# enumeration risk to guard against here, only "an automated script
# repeatedly re-triggering a mail send for the one account it's signed
# into." See settings.account_contact_email_rate_limit_attempts/
# window_seconds' own comment in app/config.py.
_contact_email_account_limiter = new_rate_limit_bucket()


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


def _has_password(conn, account_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM account_password WHERE account_id = ?", (account_id,)
        ).fetchone()
        is not None
    )


def _has_verified_identity_email(conn, account_id: int) -> bool:
    """Exactly the condition POST /api/account/password's own guard
    checks before it will let a password be set -- pulled out here so
    that guard (below, in set_password()) and _owes_password() read
    the SAME query rather than two copies that could drift apart.

    Deliberately account_identity ONLY, never account.contact_email:
    account_identity's email_verified is a PROVIDER's own assertion (an
    OAuth consent screen, or this app's own magic-link proof of
    control) that POST /auth/password/start actually signs a person in
    with; contact_email is a plain, user-typed "where can we reach
    you" address that is deliberately never usable to sign in at all
    (see app/db.py's account.contact_email MIGRATIONS comment, and the
    CRITICAL/NON-NEGOTIABLE comment on resolve_oauth_callback's case 3
    in app/oauth_api.py, for exactly why folding it in here would be an
    account-takeover path). A verified contact_email with no verified
    identity email must NOT satisfy this -- there would be no address
    to actually sign in with, so nothing here may treat it as if there
    were (see tests/test_account_api.py's test for this exact case).
    """
    return (
        conn.execute(
            "SELECT 1 FROM account_identity WHERE account_id = ? AND email_verified = 1 LIMIT 1",
            (account_id,),
        ).fetchone()
        is not None
    )


def _has_non_email_identity(conn, account_id: int) -> bool:
    """True if this account holds an account_identity row from any
    provider OTHER than 'email' -- a working sign-in door that does
    not depend on any mail ever arriving (an OAuth "sign in with
    <provider>" button always works, regardless of the state of this
    account's inbox). Derived from the identity rows themselves
    (provider != 'email') rather than a hardcoded list of OAuth
    provider names, so a provider added later (see app/db.py's
    account_identity comment for the current roster) is picked up here
    with no edit to this function.
    """
    return (
        conn.execute(
            "SELECT 1 FROM account_identity WHERE account_id = ? AND provider != 'email' LIMIT 1",
            (account_id,),
        ).fetchone()
        is not None
    )


def _owes_password(conn, account_id: int) -> bool:
    """Does this account currently OWE a password -- Matt's rule,
    narrowed: an email link must never be an account's ONLY way in.
    True exactly when the account has a verified identity email
    (_has_verified_identity_email above), no password row yet, and no
    OTHER usable sign-in door (_has_non_email_identity above).

    This used to fire on "has a verified email" alone, on the reasoning
    that mail can be delayed or lost, so relying on it is fragile. But
    someone who signed in with, say, GitHub can always get back in
    through GitHub -- the verified email on their identity row merely
    arrived along with their OAuth profile, it was never their way in.
    Asking them for a password buys nothing: they already have a door
    that does not depend on mail arriving. So the condition is now
    about the account's WHOLE set of doors, not just whether an email
    happens to be on file -- _has_non_email_identity backs off the
    moment any other provider identity exists, no matter when it was
    linked relative to the email one.

    This is deliberately a SUBSET of the condition POST
    /api/account/password already requires before it will let a
    password be set (see that route's own docstring): that route only
    checks _has_verified_identity_email, this checks that PLUS two more
    (narrower) clauses. Narrowing which accounts owe a password can
    never widen which accounts are refused the ability to set one --
    so an account can still never be compelled to do something it is
    refused the ability to do. The "no password row yet" clause is what
    keeps this from firing forever once the requirement has been
    satisfied once.

    Evaluated fresh on every read (GET /api/account calls this, not a
    flag stamped at sign-up) because it is a STANDING condition, not a
    one-time sign-up step, and it moves in BOTH directions over an
    account's life: one whose only door is email owes a password, stops
    owing the instant it links an OAuth provider, and owes again if
    that provider is later unlinked (app/account_api.py's own unlink
    route) leaving email alone once more. There is no "first login
    after linking" hook to keep in sync, because nothing is cached --
    the next GET /api/account just sees it.

    An earlier draft of this paragraph gave an example that cannot
    happen: an account created through GitHub that "later links a
    verified email AND has no other provider identity". The GitHub
    identity IS the other provider identity, and _has_non_email_identity
    does not care whether it carries a verified email, so such an
    account can never begin owing while that row exists. Worth stating
    plainly, because the same mistake was sitting in a test that tried
    to prove the standing property with exactly that setup.

    The single authoritative helper for this question -- every caller
    that needs to know "does this account owe a password" (currently
    just GET /api/account) reuses this rather than re-deriving the
    condition inline.
    """
    return (
        _has_verified_identity_email(conn, account_id)
        and not _has_password(conn, account_id)
        and not _has_non_email_identity(conn, account_id)
    )


def _door_counts(conn, account_id: int) -> tuple[dict[str, int], bool]:
    """The one place that counts "ways to sign in" for an account --
    used by both DELETE routes below (identity/{provider}, password) to
    enforce the last-door guard, and by GET /api/account's own
    per-identity "can this be removed" field, so the UI is never
    offered a button the backend would then refuse.

    Returns (per_provider_counts, has_password). per_provider_counts is
    {provider: row_count} over account_identity -- a count per PROVIDER,
    not per row, because DELETE /api/account/identity/{provider}
    disconnects an entire provider at once (every account_identity row
    for it, see that route's own docstring for why a provider, not a
    single (provider, subject) row, is the unit of disconnection here).
    The total door count is sum(per_provider_counts.values()) +
    (1 if has_password else 0).
    """
    rows = conn.execute(
        "SELECT provider, COUNT(*) AS n FROM account_identity"
        " WHERE account_id = ? GROUP BY provider",
        (account_id,),
    ).fetchall()
    per_provider = {r["provider"]: r["n"] for r in rows}
    return per_provider, _has_password(conn, account_id)


def _identities_out(conn, account_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT provider, email, email_verified, linked_at, last_login_at "
        "  FROM account_identity WHERE account_id = ? ORDER BY linked_at",
        (account_id,),
    ).fetchall()
    per_provider, has_password = _door_counts(conn, account_id)
    total_doors = sum(per_provider.values()) + (1 if has_password else 0)
    return [
        {
            "provider": r["provider"],
            # Same PROVIDER_LABELS table GET /auth/providers and GET
            # /api/account/pending read (app/oauth.py) -- the frontend
            # never hardcodes a provider's display capitalization or
            # guesses one for a provider it doesn't recognise.
            "label": PROVIDER_LABELS.get(r["provider"], r["provider"]),
            "email": _mask_email(r["email"]),
            # Whether THIS identity's address is provider-verified --
            # never the raw address, same masking reasoning _mask_email()
            # gives, but the boolean itself is safe to expose as-is. The
            # one thing this is FOR: POST /api/account/password refuses
            # to set a password unless at least one identity on the
            # account has email_verified = 1 (see that route's own
            # docstring) -- without this field there is no way for a
            # client to know in advance whether that form should even be
            # offered, short of submitting it and reading the error.
            "email_verified": bool(r["email_verified"]),
            "linked_at": r["linked_at"],
            "last_login_at": r["last_login_at"],
            # Removing THIS identity means removing every row that
            # shares its provider (see DELETE /api/account/identity/
            # {provider}'s own docstring) -- can_remove is false when
            # doing so would take the account to zero doors.
            "can_remove": (total_doors - per_provider.get(r["provider"], 0)) >= 1,
        }
        for r in rows
    ]


def _contact_email_out(conn, account_id: int) -> dict | None:
    row = conn.execute(
        "SELECT contact_email, contact_email_verified_at FROM account WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    if row is None or row["contact_email"] is None:
        return None
    return {
        # Same masking _mask_email() applies to a linked identity's own
        # address -- see that function's own docstring for why (defense
        # in depth against anything that can read a session cookie, not
        # a secret from the account holder themselves).
        "email": _mask_email(row["contact_email"]),
        "verified": row["contact_email_verified_at"] is not None,
    }


def _totp_out(conn, account_id: int) -> dict:
    """TOTP status for GET /api/account's Security panel -- same
    read-helper shape as _contact_email_out() just above. `enabled`
    only ever reflects an ACTIVATED secret (account_totp.activated_at
    IS NOT NULL -- see that column's own comment in app/db.py for why
    a still-pending, unproven enrollment must never read as enabled).
    `available` is app/totp.py's totp_encryption_available() -- whether
    settings.account_totp_encryption_key is even configured, so the
    frontend can explain an unavailable "Enable two-factor
    authentication" control instead of offering one that would 404 the
    moment it's used (see app/totp_api.py's POST
    /api/account/totp/enroll for that same check on the write side).
    recovery_codes_remaining is only meaningful (non-None) while
    enabled -- an account that has never enrolled, or that disabled
    TOTP (which deletes every recovery-code row -- see that table's
    own comment in app/db.py), has none to count.
    """
    row = conn.execute(
        "SELECT activated_at FROM account_totp WHERE account_id = ?", (account_id,)
    ).fetchone()
    enabled = row is not None and row["activated_at"] is not None
    recovery_codes_remaining = None
    if enabled:
        count_row = conn.execute(
            "SELECT COUNT(*) AS n FROM account_totp_recovery_code"
            " WHERE account_id = ? AND used_at IS NULL",
            (account_id,),
        ).fetchone()
        recovery_codes_remaining = count_row["n"] if count_row else 0
    return {
        "enabled": enabled,
        "available": totp_encryption_available(),
        "recovery_codes_remaining": recovery_codes_remaining,
    }


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


def _has_unrevoked_key(conn, player_id: int) -> bool:
    """Whether this player currently holds at least one live (not
    revoked) API key -- GET /api/account's own has_api_key field below,
    used ONLY so the Security panel's key control (frontend/account.js)
    can tell "generate a first key" from "rotate the one you have"
    apart, without that route ever seeing the key or its hash. Not
    folded into _player_out() above: that helper also backs POST
    /api/account/link-key's response, and this field has no business
    in that route's shape -- a linked-by-key player always already has
    one, by construction of how they got here. A player normally holds
    at most one live key at a time (POST /api/account/rotate-key and
    app/admin_api.py's reissue both revoke every existing key in the
    same transaction that inserts the new one), so this is really a
    yes/no rather than a count -- EXISTS-style LIMIT 1 is used anyway
    rather than assuming that invariant holds everywhere forever.
    """
    row = conn.execute(
        "SELECT 1 FROM api_key WHERE player_id = ? AND revoked_at IS NULL LIMIT 1",
        (player_id,),
    ).fetchone()
    return row is not None


def _sessions_out(conn, account_id: int, *, current_token_hash: str) -> list[dict]:
    """Active (not revoked, not expired) sessions on this account --
    never returns token_hash itself, only enough for a person to
    recognise which of their own sessions is which (see
    account_session's own comment in app/db.py for why device_label
    exists at all -- recognition, not a security control -- and why
    there is no `ip` field here anymore: it is no longer stored at
    all, not just no longer returned).
    """
    now = int(time.time())
    rows = conn.execute(
        "SELECT token_hash, created_at, last_seen_at, device_label "
        "  FROM account_session "
        " WHERE account_id = ? AND revoked_at IS NULL AND expires_at > ? "
        " ORDER BY last_seen_at DESC",
        (account_id, now),
    ).fetchall()
    return [
        {
            "created_at": r["created_at"],
            "last_seen_at": r["last_seen_at"],
            "device_label": r["device_label"],
            "current": r["token_hash"] == current_token_hash,
        }
        for r in rows
    ]


# ---- routes ---------------------------------------------------------------

@router.get("/api/account")
async def get_account(session: SessionPrincipal = Depends(require_session)) -> JSONResponse:
    """The full account read -- identities, linked player, active
    sessions, and account-security state (has_password, contact_email,
    owes_password, role).

    `owes_password` (see _owes_password()'s own docstring for the full
    rule) is carried here, not in SessionPrincipal/require_session():
    every other account route already depends on require_session(),
    and this is a required ONBOARDING step, not an authorization gate
    -- it exists for the account page to render a prompt, not for any
    route to refuse a request over. Adding a query to require_session()
    would put this on the hot path of every session-authenticated
    route (checkin, player, key rotation, ...) for a value only the
    account page reads, and would invite exactly the kind of
    route-gating this feature deliberately does not do.

    `role` (account.role -- see that column's own MIGRATIONS comment in
    app/db.py) is None for an ordinary player and 'admin' or 'operator'
    for a role-holding account. Read here the same reasoning
    owes_password already established: this is display-only, never an
    authorization decision -- frontend/account.js reads it purely to
    decide whether to SHOW the admin panel button (see that file's
    renderAdminSection()), and every real admin/operator route still
    re-checks the session's actual role itself through
    app/admin_api.py's _role_guard() on every request, never trusting
    what this endpoint last reported.

    `player.has_api_key`, when a player is linked, is whether that
    player currently holds a live (not revoked) key -- see
    _has_unrevoked_key()'s own docstring for why this exists at all
    (frontend/account.js's Security panel needs to word its key control
    as "generate a first key" or "rotate the one you have" without ever
    being handed the key or its hash to check for itself) and why it is
    added here rather than inside _player_out() itself.
    """
    conn = connect()
    try:
        identities = _identities_out(conn, session.account_id)
        player = _player_out(conn, session.player_id) if session.player_id is not None else None
        if player is not None:
            player = dict(player)
            player["has_api_key"] = _has_unrevoked_key(conn, session.player_id)
        sessions_out = _sessions_out(conn, session.account_id, current_token_hash=session.token_hash)
        has_password = _has_password(conn, session.account_id)
        contact_email = _contact_email_out(conn, session.account_id)
        owes_password = _owes_password(conn, session.account_id)
        totp = _totp_out(conn, session.account_id)
        role_row = conn.execute(
            "SELECT role FROM account WHERE account_id = ?", (session.account_id,)
        ).fetchone()
        role = role_row["role"] if role_row is not None else None
    finally:
        conn.close()

    return JSONResponse(
        {
            "account_id": session.account_id,
            "identities": identities,
            "player": player,
            "sessions": sessions_out,
            "has_password": has_password,
            "contact_email": contact_email,
            "owes_password": owes_password,
            "totp": totp,
            "role": role,
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
      - this account already has a linked player -- a DIFFERENT one
        than the key names (one account, one player, at most -- see
        app/db.py's player.account_id and its UNIQUE index)
      - that key's player already belongs to a DIFFERENT account

    If the key names the player ALREADY linked to THIS account, that
    is not a conflict at all -- the desired end state already holds
    (a retried request, a second click, the same key submitted twice).
    Same "a retried request should just succeed" reasoning
    app/nodes_api.py's POST /api/nodes and app/checkin_api.py's
    confirm_accept already apply for their own already-bound cases:
    treated as success, no second account_link_event written (the
    original one, from whenever it was first linked, still stands).
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
            if existing["player_id"] == player_id:
                # This key's player is already linked to THIS account --
                # the desired end state already holds (most likely a
                # retried request: see this route's own docstring). Not
                # a conflict, no write, no second account_link_event --
                # just report the same success a fresh link would have.
                player = _player_out(conn, player_id)
                return JSONResponse({"player": player}, status_code=200)
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


# ---- rotate-key -------------------------------------------------------

@router.post("/api/account/rotate-key")
async def rotate_key(
    request: Request, session: SessionPrincipal = Depends(require_session)
) -> JSONResponse:
    """The player-facing twin of app/admin_api.py's POST
    /api/admin/player/reissue -- mints one fresh key for the caller's
    OWN linked player and revokes every key that player currently
    holds, in the same single WriteSession transaction reissue uses,
    for the same reason (see that route's own docstring: "someone else
    has my key" and "I lost my key" look identical from here, so the
    safe default is that whatever key existed before stops working the
    moment a new one is issued).

    Deliberately reuses reissue's exact revoke-then-insert SQL and its
    ingestor.invalidate_player() call afterward, rather than
    reimplementing either -- without that call, a just-revoked key
    could keep authenticating at the ingest endpoint until its cached
    auth entry expires (settings.mc_key_cache_seconds), the same
    staleness problem reissue's own docstring explains.

    Unlike reissue, there is no display_name confirmation guard: an
    operator can typo a player_id and hit the wrong person's account,
    but a signed-in caller can only ever act on session.player_id --
    their OWN linked player, resolved by require_session() from the
    session cookie itself, never from anything the request body
    supplies. There is nothing here for a caller to get wrong the way a
    mistyped player_id could.

    404s if the account has no linked player yet (nothing to rotate) --
    see app/sessions.py's own SessionPrincipal.player_id docstring for
    why that field is nullable at all.
    """
    if session.player_id is None:
        return JSONResponse(
            {"error": "this account has no linked player"}, status_code=404
        )

    ip = get_client_ip(request)
    if _rotate_key_addr_limiter.limited(
        ip,
        limit=settings.account_rotate_key_rate_limit_attempts,
        window=settings.account_rotate_key_rate_limit_window_seconds,
    ):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    now = int(time.time())
    async with WriteSession() as conn:
        row = conn.execute(
            "SELECT display_name FROM player WHERE player_id = ?", (session.player_id,)
        ).fetchone()
        if row is None:
            # Defensive only -- require_session() just resolved this
            # player_id from a live `player` row a moment ago, so this
            # should be unreachable outside a concurrent player delete.
            return JSONResponse({"error": "player not found"}, status_code=404)

        # Same "revoke every currently-active key, not just the newest"
        # reasoning admin_player_reissue gives its own identical UPDATE.
        revoked = conn.execute(
            "UPDATE api_key SET revoked_at = ? WHERE player_id = ? AND revoked_at IS NULL",
            (now, session.player_id),
        )
        revoked_count = revoked.rowcount

        raw_key = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO api_key(key_hash, player_id, issued_at) VALUES (?, ?, ?)",
            (hash_secret(raw_key), session.player_id, now),
        )
        conn.execute(
            "INSERT INTO account_link_event(account_id, kind, detail, actor, created_at) "
            "VALUES (?, 'key_rotated', ?, 'user', ?)",
            (session.account_id, f"player_id={session.player_id} revoked={revoked_count}", now),
        )

    # See this route's own docstring -- same cache-staleness fix
    # admin_player_reissue applies, called the same way (after commit,
    # covering every key just revoked above, not only the newest one).
    ingestor = request.app.state.mc_ingestor
    ingestor.invalidate_player(session.player_id)

    return JSONResponse(
        {
            "rotated": True,
            "player_id": session.player_id,
            "key": raw_key,
            "issued_at": now,
            "revoked_count": revoked_count,
        },
        status_code=200,
    )


# ---- account password ---------------------------------------------------

def _load_password(conn, account_id: int) -> PasswordHash | None:
    row = conn.execute(
        "SELECT salt, n, r, p, dklen, hash FROM account_password WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    if row is None:
        return None
    return PasswordHash(
        salt=row["salt"], n=row["n"], r=row["r"], p=row["p"], dklen=row["dklen"],
        derived_key=row["hash"],
    )


@router.post("/api/account/password")
async def set_password(
    request: Request, session: SessionPrincipal = Depends(require_session)
) -> JSONResponse:
    """Set (first time) or change (already set) the caller's account
    password -- app/password_login.py's hashlib.scrypt, never
    app/mc_ingest.py's hash_secret() (see that module's own docstring
    for why a password can never go through the same hasher as a
    random token).

    Refused outright, before anything else, unless the account already
    holds at least one VERIFIED email identity (account_identity row,
    email_verified = 1, from ANY provider -- Google, GitHub, magic-link
    email, whichever) -- see app/oauth_api.py's POST /auth/password/start
    for why: that route resolves "email + password" to an account by
    matching the email against account_identity's own verified rows,
    the exact same query case 3 of resolve_oauth_callback already runs.
    A password set on an account with no verified email would be a
    door with no address to knock on -- unreachable, not merely
    inconvenient.

    Changing an existing password requires `current_password` and
    checks it before accepting `new_password` -- setting the FIRST
    password on an account that has none yet requires only the session
    itself (there is no prior secret to prove knowledge of).
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad request"}, status_code=400)

    new_password = body.get("new_password")
    if not isinstance(new_password, str) or not new_password:
        return JSONResponse({"error": "new_password is required"}, status_code=400)
    if len(new_password) < settings.account_password_min_length:
        return JSONResponse(
            {
                "error": f"password must be at least "
                f"{settings.account_password_min_length} characters"
            },
            status_code=400,
        )

    now = int(time.time())
    async with WriteSession() as conn:
        # Same query _has_verified_identity_email() runs -- see that
        # helper's own docstring for why this condition must stay
        # account_identity-only (never account.contact_email), and
        # _owes_password()'s docstring for the other half of why this
        # exact clause is shared rather than copied.
        if not _has_verified_identity_email(conn, session.account_id):
            return JSONResponse(
                {
                    "error": "a verified email identity is required before setting a "
                    "password -- link and verify one first"
                },
                status_code=409,
            )

        existing = _load_password(conn, session.account_id)
        if existing is not None:
            current_password = body.get("current_password")
            if not isinstance(current_password, str) or not current_password:
                return JSONResponse(
                    {"error": "current_password is required"}, status_code=400
                )
            if not verify_password(current_password, existing):
                return JSONResponse(
                    {"error": "current password is incorrect"}, status_code=401
                )
            kind_detail = "changed"
        else:
            kind_detail = "set"

        hashed = hash_password(
            new_password,
            n=settings.account_password_scrypt_n,
            r=settings.account_password_scrypt_r,
            p=settings.account_password_scrypt_p,
            dklen=settings.account_password_scrypt_dklen,
        )
        conn.execute(
            "INSERT INTO account_password"
            "(account_id, salt, n, r, p, dklen, hash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET "
            "  salt = excluded.salt, n = excluded.n, r = excluded.r, p = excluded.p, "
            "  dklen = excluded.dklen, hash = excluded.hash, updated_at = excluded.updated_at",
            (
                session.account_id, hashed.salt, hashed.n, hashed.r, hashed.p, hashed.dklen,
                hashed.derived_key, now, now,
            ),
        )
        conn.execute(
            "INSERT INTO account_link_event(account_id, kind, detail, actor, created_at) "
            "VALUES (?, 'password_set', ?, 'user', ?)",
            (session.account_id, kind_detail, now),
        )

    return JSONResponse({"ok": True}, status_code=200)


@router.delete("/api/account/password")
async def delete_password(
    session: SessionPrincipal = Depends(require_session)
) -> JSONResponse:
    """Remove the caller's account password -- subject to the same
    last-door guard DELETE /api/account/identity/{provider} enforces
    (see _door_counts()' own docstring): refused if this account has no
    OTHER way to sign in.
    """
    now = int(time.time())
    async with WriteSession() as conn:
        if not _has_password(conn, session.account_id):
            return JSONResponse({"error": "no password is set"}, status_code=404)

        per_provider, _ = _door_counts(conn, session.account_id)
        remaining = sum(per_provider.values())  # password itself is the door being removed
        if remaining < 1:
            return JSONResponse(
                {
                    "error": "removing your password would leave this account with no "
                    "way to sign in"
                },
                status_code=409,
            )

        conn.execute("DELETE FROM account_password WHERE account_id = ?", (session.account_id,))
        conn.execute(
            "INSERT INTO account_link_event(account_id, kind, detail, actor, created_at) "
            "VALUES (?, 'password_removed', NULL, 'user', ?)",
            (session.account_id, now),
        )

    return JSONResponse(
        {"ok": True, "remaining_doors": remaining, "warning_last_door": remaining == 1},
        status_code=200,
    )


# ---- identity unlink ------------------------------------------------------

@router.delete("/api/account/identity/{provider}")
async def unlink_identity(
    provider: str, session: SessionPrincipal = Depends(require_session)
) -> JSONResponse:
    """Disconnect a sign-in method. Linking an ADDITIONAL provider
    already works today by visiting GET /auth/{provider}/start while
    signed in (case 2 of app/oauth_api.py's callback decision tree) --
    this route is only the reverse of that, not a rebuild of it.

    Removes every account_identity row for (account_id, provider) at
    once, not a single (provider, subject) row -- GET /api/account does
    not expose `subject` to a caller at all (see _identities_out(), and
    app/db.py's account_identity comment on why the (provider, subject)
    pair -- not account_id -- is that table's own primary key: nothing
    stops one account from holding more than one identity under the
    SAME provider), so "disconnect google" is the only granularity this
    API can name. See _door_counts()' own docstring for how that shapes
    the last-door count.

    HARD RULE: never leaves an account with zero doors (see this
    module's own docstring's "doors" section) -- counts every OTHER
    linked identity plus a set password, and refuses whatever removal
    would bring that count to zero. Returns
    warning_last_door: true (not a refusal) when the removal is allowed
    but would leave exactly one door, so a caller's UI can show a
    "this is your only way back in" notice before it happens.
    """
    now = int(time.time())
    async with WriteSession() as conn:
        per_provider, has_password = _door_counts(conn, session.account_id)
        removing = per_provider.get(provider, 0)
        if removing == 0:
            return JSONResponse(
                {"error": "that provider is not linked to this account"}, status_code=404
            )

        total_doors = sum(per_provider.values()) + (1 if has_password else 0)
        remaining = total_doors - removing
        if remaining < 1:
            return JSONResponse(
                {
                    "error": "disconnecting this would leave this account with no way "
                    "to sign in"
                },
                status_code=409,
            )

        conn.execute(
            "DELETE FROM account_identity WHERE account_id = ? AND provider = ?",
            (session.account_id, provider),
        )
        conn.execute(
            "INSERT INTO account_link_event(account_id, kind, detail, actor, created_at) "
            "VALUES (?, 'identity_unlinked', ?, 'user', ?)",
            (session.account_id, f"provider={provider}", now),
        )

    return JSONResponse(
        {"ok": True, "remaining_doors": remaining, "warning_last_door": remaining == 1},
        status_code=200,
    )


# ---- contact email --------------------------------------------------------

@router.post("/api/account/contact-email")
async def set_contact_email(
    request: Request, session: SessionPrincipal = Depends(require_session)
) -> JSONResponse:
    """Set (or change) the account's contact-only address and mail a
    single-use verification link to it -- reuses
    app/email_login.py's own address-shape validation
    (looks_like_email/normalize_email) and mail send
    (send_magic_link_email), the same helpers POST /auth/email/start
    uses, but writes to a completely separate token table
    (account_contact_email_token, never email_login_token) and never
    touches account_identity -- see app/db.py's account.contact_email
    MIGRATIONS comment, and the case-3 matching query's own comment in
    app/oauth_api.py, for exactly why this address must never be able
    to sign anyone in or auto-link a new provider identity.

    Always stored unverified the moment it is set (contact_email_verified_at
    cleared to NULL), even when re-setting the SAME address that was
    already verified -- a new address always needs its own fresh proof
    of control, and there is no cheap way to tell "the same address,
    re-typed" from "a different address that happens to match" without
    trusting the caller's own claim.

    404s (the same "not configured" contract every optional mail-
    sending route in this app already uses) if email sign-in is not
    configured at all (email_login_enabled() -- requires both
    smtp_host and oauth_public_base_url) -- there would be no way to
    ever verify the address, so this refuses to accept it half-broken.
    """
    if not email_login_enabled():
        return JSONResponse({"error": "not found"}, status_code=404)

    if _contact_email_account_limiter.limited(
        str(session.account_id),
        limit=settings.account_contact_email_rate_limit_attempts,
        window=settings.account_contact_email_rate_limit_window_seconds,
    ):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    raw_email = body.get("email") if isinstance(body, dict) else None
    if not isinstance(raw_email, str) or not raw_email:
        return JSONResponse({"error": "email is required"}, status_code=400)

    email = normalize_email(raw_email)
    if not looks_like_email(email):
        return JSONResponse({"error": "invalid email address"}, status_code=400)

    now = int(time.time())
    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_secret(raw_token)
    expires_at = now + settings.account_contact_email_token_lifetime_seconds
    async with WriteSession() as conn:
        conn.execute(
            "UPDATE account SET contact_email = ?, contact_email_verified_at = NULL "
            "WHERE account_id = ?",
            (email, session.account_id),
        )
        conn.execute(
            "INSERT INTO account_contact_email_token"
            "(token_hash, account_id, email, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (token_hash, session.account_id, email, now, expires_at),
        )
        # Opportunistic cleanup, same grace-period shape
        # app/oauth_api.py's _sweep_stale_rows() uses for
        # account_pending_identity/email_login_token -- a separate,
        # inline copy here (not an import of that private function)
        # since this table lives in a different module's own route.
        cutoff = now - 3600
        conn.execute(
            "DELETE FROM account_contact_email_token "
            "WHERE expires_at < ? OR (consumed_at IS NOT NULL AND consumed_at < ?)",
            (cutoff, cutoff),
        )
        conn.execute(
            "INSERT INTO account_link_event(account_id, kind, detail, actor, created_at) "
            "VALUES (?, 'contact_email_set', ?, 'user', ?)",
            (session.account_id, f"email={email}", now),
        )

    link_url = f"{settings.oauth_public_base_url.rstrip('/')}/auth/contact-email/verify?token={raw_token}"
    try:
        # PURPOSE_VERIFY_CONTACT, not the sign-in wording: this link
        # confirms an address is reachable, it does not hand over a
        # session. Saying "click here to sign in" here would be false,
        # and false in the direction phishing goes.
        await send_magic_link_email(email, link_url, PURPOSE_VERIFY_CONTACT)
    except EmailSendError:
        # Logged inside send_magic_link_email() itself. Not surfaced to
        # the caller as a distinct error -- the address is saved either
        # way (unverified until a link is clicked, whenever the next
        # send succeeds or this one is retried), the same "never reveal
        # whether the send itself worked" posture POST /auth/email/start
        # already applies for the same reason (this endpoint IS
        # authenticated, but an outbound-mail outage is not something a
        # caller can act on differently either way).
        pass

    return JSONResponse({"ok": True, "email": _mask_email(email), "verified": False}, status_code=200)


def _pick_identity_email_for_contact(conn, account_id: int) -> str | None:
    """Deterministic choice of WHICH verified identity email
    POST /api/account/contact-email/use-identity (below) copies onto
    account.contact_email, for the case -- uncommon, but not
    impossible -- that an account holds more than one account_identity
    row with email_verified = 1 (a person who has linked both Google
    and GitHub, each with its own provider-verified address). This is
    the ONE place that picks, so "which address did it use" can never
    depend on which route happened to ask, or on SQLite's own row
    order.

    Ordered by last_login_at DESC first: the identity a person most
    recently actually signed IN through is the address most likely to
    still be one they read today, ahead of one linked once, long ago,
    and never used again. This is deliberately not "most recently
    linked" (linked_at) as the primary key -- linking and using are
    different facts, and a stale identity someone linked years ago but
    keeps re-authenticating through should still win over one they
    added last week and have not touched since. linked_at DESC is only
    the first tiebreaker, for two identities that have never been
    logged into again since being linked (both still carry the
    timestamp _link_identity() stamped onto last_login_at at link time
    -- see that function's own comment in app/oauth_api.py) -- whichever
    was linked more recently wins that tie. provider ASC is the final,
    purely mechanical tiebreak so the choice can never depend on
    SQLite's own return order for two rows tied on both timestamps
    (two identities linked in the same second, e.g. by a test or a
    migration backfill).
    """
    row = conn.execute(
        "SELECT email FROM account_identity"
        " WHERE account_id = ? AND email_verified = 1"
        "   AND email IS NOT NULL AND email != ''"
        " ORDER BY last_login_at DESC, linked_at DESC, provider ASC"
        " LIMIT 1",
        (account_id,),
    ).fetchone()
    return row["email"] if row is not None else None


@router.post("/api/account/contact-email/use-identity")
async def use_identity_email_as_contact(
    session: SessionPrincipal = Depends(require_session),
) -> JSONResponse:
    """One-click twin of POST /api/account/contact-email, above, for the
    specific gap that motivated it: an account that signed in through
    GitHub/Google/etc already has a verified email the MOMENT that
    identity is linked (a provider vouched for it at OAuth time -- see
    _has_verified_identity_email()'s own docstring on why that is a
    fundamentally stronger claim than a typed string), but
    account.contact_email is a wholly separate column (see that
    column's own MIGRATIONS comment in app/db.py) that starts NULL and,
    until this route existed, could only ever be filled in by a person
    typing an address the system already held, verified, and then
    proving control of it a second time by mail. This route closes that
    gap by copying the account's own verified identity email onto
    contact_email SERVER-SIDE.

    Deliberately takes NO request body at all -- not Request, not a
    parsed JSON dict, nothing is read off the wire here. The address to
    copy is resolved entirely from this session's OWN account_identity
    rows via _pick_identity_email_for_contact() above; there is no
    field anywhere on this route a caller could set to name a
    different address, including one supplied in a request body --
    FastAPI never parses a body this route declares no parameter for,
    so anything a caller sends is inert. This is the whole point, not
    an oversight: if the client could name the address, this endpoint
    would become a way to set an arbitrary contact_email while skipping
    the manual route's mail-and-click proof entirely -- see that
    route's own docstring for why an unverified, caller-typed address
    is normally never trusted without one.

    Also never hands the real address back to the browser in the
    clear: the response below masks it through the same _mask_email()
    every other read in this module uses (see that function's own
    docstring) -- this route lets the browser trigger the copy without
    ever needing to know the address itself, which is exactly why GET
    /api/account cannot simply prefill the contact-email form client-
    side in the first place.

    Refused with 409 -- the same status set_password() above uses for
    its own "not eligible yet" guard -- when no identity on the account
    carries email_verified = 1: there is nothing to copy, and silently
    doing nothing would leave a caller unsure whether the click did
    anything at all.

    ---- does the copied address count as verified? ----

    Yes: contact_email_verified_at is stamped in the SAME write as
    contact_email, immediately -- never left for a mailed link to
    confirm later. This is a deliberate departure from
    set_contact_email()'s own "always stored unverified, even when
    re-setting the same address" rule just above, and it is not the
    same question with the same answer twice:

    - That route's address is a bare, caller-TYPED string with no proof
      behind it at the moment it is saved. Proof is exactly what
      account_contact_email_token's mailed link supplies, and there is
      no way to skip that step for an address this app has never
      independently confirmed the caller controls.
    - THIS route's address is never caller-supplied at all -- it is
      read straight out of account_identity, where email_verified = 1
      already means an OAuth provider (or this app's own magic-link
      flow, for provider='email') put that exact address through ITS
      OWN proof-of-control step before the row was ever written.
      Mailing a second confirmation link to an address a provider
      already vouched for would not raise assurance about anything --
      this app already trusts that same fact enough to use it as the
      gate for "must set a password" (_has_verified_identity_email(),
      _owes_password(), above) and enough to auto-link a brand-new
      sign-in onto this very account (resolve_oauth_callback's case 3,
      app/oauth_api.py) -- it would only be a redundant round-trip to
      an inbox already proven reachable, for no additional confidence.

    This is NOT a backdoor around the sign-in exclusion those same
    comments guard, and nothing about it changes here: contact_email
    remains, exactly as before, never read by resolve_oauth_callback's
    case-3 matching query or by POST /auth/password/start (this route
    writes account.contact_email/contact_email_verified_at ONLY, never
    account_identity), and the copy runs one-way, FROM the trusted
    table TO the untrusted-by-that-comparison one -- contact_email's
    own read paths (an operator's view, a future contact-only
    notification) are the only things that will ever see the result.

    No account_contact_email_token row is written either -- that table
    exists to bridge the gap between "a person typed an address" and "a
    person can read mail sent to it," and this route has no such gap
    left to bridge once it runs.
    """
    now = int(time.time())
    async with WriteSession() as conn:
        email = _pick_identity_email_for_contact(conn, session.account_id)
        if email is None:
            return JSONResponse(
                {
                    "error": "no verified sign-in email to copy -- link and verify a "
                    "sign-in provider first, or set a contact email manually"
                },
                status_code=409,
            )

        conn.execute(
            "UPDATE account SET contact_email = ?, contact_email_verified_at = ? "
            "WHERE account_id = ?",
            (email, now, session.account_id),
        )
        conn.execute(
            "INSERT INTO account_link_event(account_id, kind, detail, actor, created_at) "
            "VALUES (?, 'contact_email_set', ?, 'user', ?)",
            (session.account_id, f"email={email} source=identity_copy", now),
        )

    return JSONResponse(
        {"ok": True, "email": _mask_email(email), "verified": True}, status_code=200
    )


# ---- account deletion ------------------------------------------------------
#
# DELETE /api/account: self-service, irreversible, "delete the person,
# keep the team" -- Matt's own resolution of the design tension this
# route exists to answer. The alternative considered and rejected was
# deleting the `player` row outright (mirroring what
# app/admin_api.py's POST /api/admin/player/delete already does): a
# player is not an island -- mc_tile.last_player_id, mc_tile_capture_log,
# mc_tile_unique_painter, mc_checkin_award, month_award, and
# place_activation all name a player_id from rows OTHER people are also
# part of (a captured square's history, a month's published standings,
# a check-in streak). Deleting the player row would mean retroactively
# editing a shared, already-published record to serve one person's
# deletion -- rewriting history everyone else who played that month
# still sees. So this route does the opposite of admin delete on
# purpose: every table that is genuinely this ONE person's own data --
# their login, their sessions, their radios, their raw ping history --
# is hard-deleted outright, while `player` itself survives, stripped of
# everything that named who it was. What is left behind is exactly what
# a logged-out visitor already sees looking at that square today: a
# team took it, and nobody can say who. That is this project's own
# standing rule -- "identity can be public, location can be public, the
# link between them cannot" -- applied to the one case where the person
# themselves is asking to be the one who can no longer be linked.
#
# Table-by-table disposition (every table this codebase's schema
# stores a player_id or account_id in -- see app/db.py's SCHEMA --
# falls into exactly one of these three groups, no fourth bucket
# needed):
#
# HARD-DELETED (account-scoped -- _ACCOUNT_SCOPED_TABLES below):
#   account_identity, account_session, account_password, account_totp,
#   account_totp_recovery_code, account_totp_challenge,
#   account_contact_email_token, account_link_event, and finally
#   `account` itself. Every one of these is either a sign-in credential,
#   a login session, or an audit trail of what THIS account did to
#   ITSELF -- nothing here is shared with, or meaningful to, anyone
#   else. account_session going away here is what makes the caller's
#   own cookie stop working (see this route's own return path below --
#   there is no separate "and also log them out" step, deleting the row
#   the cookie names IS the logout).
#
# HARD-DELETED (player-scoped -- _PLAYER_SCOPED_TABLES below):
#   api_key, player_node, checkin_node_name, mc_checkin_binding,
#   mc_node_confirmation, mt_node_confirmation, player_last_fix,
#   player_cell_ping, player_cell_repeater_credit, player_ingest_stat,
#   join_token. Every one of these is keyed on player_id alone, holds
#   nothing anyone but this player could be affected by losing (a
#   radio binding, a credential, a raw location/timing trail kept for
#   anti-cheat and diagnostics), and none of it is read by anything
#   that produces a number someone ELSE'S standing depends on.
#
# TOMBSTONED, not deleted: `player` itself. display_name is overwritten
# with a value that cannot collide and cannot be mistaken for a real
# name (see _tombstone_display_name() below), disabled_at is set (the
# same column app/admin_api.py's disable path already uses to mean
# "not active"), and account_id is cleared (the player is no longer
# claimable by, or attributable to, any account -- the same NULL state
# a never-linked key-only player already sits in). team is left alone
# on purpose: it is not identifying on its own, and every surviving
# table below still needs it to keep meaning anything.
#
# LEFT COMPLETELY UNTOUCHED, now anonymous by construction: mc_tile,
# mc_tile_capture_log, mc_tile_unique_painter, mc_checkin_award,
# month_award, place_activation, player_team_change. Each of these
# still carries this player's (now-stale) player_id, but every one of
# them is a SHARED record -- a square's capture history, a month's
# published standings, a check-in streak, a place activation credit --
# that other players' own numbers are built out of, or that has already
# been shown publicly with a name attached. Deleting or blanking a
# player_id out of these would either break a join for everyone still
# reading that history, or -- worse -- silently reassign a real event
# to nobody, which is not privacy, it is data corruption. Instead, every
# one of these already resolves a player_id to a display name by
# joining against `player` at READ time (never by storing the name
# redundantly) -- see e.g. app/results.py's own month-award/standings
# queries -- so the moment the join above runs, the tombstoned
# display_name is what every one of these tables now shows: the
# capture stays real, the credit stays real, and the name attached to
# it is gone. This is the entire mechanism the "keep the team" half of
# Matt's decision relies on; it costs this route nothing further.
#
# What was verified against app/db.py's SCHEMA and found to need NO
# entry above: account_pending_identity and email_login_token are
# keyed on a raw provider identity / email address, never on account_id
# at all -- they exist to hand a brand-new sign-in a token BEFORE any
# account is chosen, so there is nothing here to attribute to this
# account in the first place (and both already self-expire on their
# own short TTL regardless). admin_action_log.actor_account_id is left
# alone too, and deliberately not folded into the account-scoped list:
# it is a system audit trail of admin/operator ACTIONS TAKEN, the exact
# same "shared record, not personal data at rest" shape this route
# already gives player_team_change/mc_checkin_award/month_award above --
# an operator's account_id surviving in a log line of something THEY
# DID to something or someone else is not a fact about the account
# being deleted here in the way account_link_event's own rows are.
#
# Compare this deliberately against POST /api/admin/player/delete
# (app/admin_api.py), which this route does NOT touch and NEVER should:
# that route is the operator path (moderation/cleanup of a player,
# possibly not this account holder, possibly against their wishes) and
# behaves entirely differently on purpose -- it hard-deletes mc_tile/
# mc_tile_score/mc_tile_capture/mc_tile_capture_log rows where the
# target player is the last painter, and mc_tile_unique_painter, which
# DOES rewrite square/capture history rather than merely anonymizing
# it, and it does NOT touch player_last_fix, player_cell_repeater_credit,
# join_token, checkin_node_name, mc_checkin_binding,
# mc_node_confirmation, or mt_node_confirmation at all, leaving those
# pointing at a player_id that no longer resolves to anything once the
# player row itself is gone. Both of those facts are worth naming
# plainly: next to this route, admin delete's own cascade now reads as
# inconsistent -- it destroys shared history this route goes out of its
# way to preserve, and it leaves orphaned player_id references in seven
# tables this route makes sure never happens. That inconsistency is
# reported, not fixed here -- admin delete is explicitly out of scope
# for this change, and it is a genuinely different operation (a
# moderation action against someone who may not have asked for it) that
# deserves its own deliberate decision, not a drive-by edit riding
# along with this one.

# Every table keyed on account_id whose ENTIRE row belongs to this one
# account and nothing else -- see this section's own comment above for
# why each belongs here. `account` itself is deleted separately, last,
# once every one of these has already been cleared.
_ACCOUNT_SCOPED_TABLES = (
    "account_identity",
    "account_session",
    "account_password",
    "account_totp",
    "account_totp_recovery_code",
    "account_totp_challenge",
    "account_contact_email_token",
    "account_link_event",
)

# Every table keyed on player_id whose entire row belongs to this one
# player and nothing else -- see this section's own comment above for
# why each belongs here, and for the (deliberately longer) list of
# player_id-keyed tables that are NOT here because other players'
# standing is built out of them.
_PLAYER_SCOPED_TABLES = (
    "api_key",
    "player_node",
    "checkin_node_name",
    "mc_checkin_binding",
    "mc_node_confirmation",
    "mt_node_confirmation",
    "player_last_fix",
    "player_cell_ping",
    "player_cell_repeater_credit",
    "player_ingest_stat",
    "join_token",
)

# Fixed confirmation phrase for the one shape this route has no
# display_name to check against: an account with no linked player at
# all (POST /api/join was never finished, or POST /api/account/link-key
# was never called). There is no name to type in that case, but the
# same "prove you meant this, not a misclick" purpose the display_name
# check below serves still applies, so this stands in for it -- an
# exact-match literal, same case-sensitive comparison style as the
# display_name check, chosen to read unambiguously as "yes, delete"
# rather than something a person could type by accident.
_NO_PLAYER_CONFIRM_PHRASE = "DELETE MY ACCOUNT"


def _tombstone_display_name(player_id: int) -> str:
    """The value player.display_name is overwritten with on deletion --
    see this module's own "account deletion" section comment above for
    the full reasoning; this is just the shape.

    Cannot collide with a name a living player holds or could ever
    register, by construction rather than by luck: app/join_api.py's
    _validate_display_name() caps every player-CHOSEN display name at
    32 characters (`1 <= len(name) <= 32`), and that is the ONLY path
    that ever writes a display_name from user input -- there is no
    rename route anywhere in this codebase (display_name is set once,
    at registration, and never again outside this function and the
    admin/join paths that also run through the same validator). The
    fixed text below, BEFORE the player_id digits are even appended, is
    already 35 characters -- past that 32-character ceiling on its own,
    for every possible player_id including a hypothetically empty one.
    No value this function can ever produce is therefore a value
    _validate_display_name() could ever have accepted, for any
    player_id, of any length, ever.
    -- and cannot collide between two DIFFERENT deleted players either:
    player_id is `player`'s own AUTOINCREMENT primary key (see
    app/db.py's SCHEMA), so SQLite never reuses one, even for a row
    this function tombstones rather than deletes -- there is exactly
    one row in this database that will ever produce this exact string
    for a given player_id, forever.
    """
    return f"Deleted player — account removed (#{player_id})"


@router.delete("/api/account")
async def delete_account(
    request: Request, session: SessionPrincipal = Depends(require_session)
) -> JSONResponse:
    """Permanently and irreversibly delete the caller's own account --
    see this module's "account deletion" section comment above for the
    full table-by-table design and why `player` is tombstoned rather
    than deleted. Nothing about this route is reachable on anyone's
    behalf but the caller's own: there is no player_id or account_id in
    the request body, only session.account_id/session.player_id,
    resolved from the cookie itself, the same "nothing here for a
    caller to get wrong the way a mistyped player_id could" reasoning
    POST /api/account/rotate-key's own docstring gives.

    ---- confirmation --------------------------------------------------

    Body must carry `display_name` equal, EXACTLY (case-sensitive, no
    trimming -- the same comparison POST /api/admin/player/delete's own
    guard runs), to the caller's OWN player's current display_name, if
    the account has a linked player. An account with NO linked player
    has no display_name to check instead, so the same field must
    instead exactly equal _NO_PLAYER_CONFIRM_PHRASE. Either way, a
    mismatch is a plain refusal -- nothing is deleted, nothing is
    partially deleted -- not a prompt to try again with a hint about
    what went wrong (see the standard, deliberately unhelpful "display
    name does not match" error every one of admin_api.py's own
    display_name-guarded routes already returns, for the same "an
    attacker fishing for the right value gets nothing back" reasoning).

    ---- re-authentication ----------------------------------------------

    This is account deletion reachable from a possibly-borrowed
    browser: a session cookie alone proves someone was signed in at
    some point, not that the person clicking "delete" right now is the
    account holder and not, say, a housemate or a coworker at an
    unattended desk. So a fresh credential is required on top of the
    session, ONE of two shapes depending on what this specific account
    actually has to offer -- never a credential this account cannot
    possibly supply:

    - If the account has an ACTIVATED TOTP secret (account_totp,
      activated_at IS NOT NULL), a live `totp_code` or an unused
      `totp_recovery_code` is required and verified the exact same way
      DELETE /api/account/totp already does before it will turn TOTP
      OFF (see that route's own docstring for why a proven CURRENT
      factor is the bar for an action this consequential) -- reusing
      verify_and_consume_totp_code()/verify_and_consume_recovery_code()
      directly, not reimplementing either. TOTP is checked first, ahead
      of password, when an account happens to hold both: it is strictly
      the stronger proof (something currently held, not something a
      browser's saved-password autofill could hand a borrower just as
      readily as the account holder).
    - Otherwise, if the account has a password (account_password), the
      current `password` is required and checked with verify_password()
      -- the same "prove the CURRENT one before accepting anything new"
      shape POST /api/account/password's own change-password path
      already uses.
    - Otherwise -- an account reachable only through OAuth-provider
      identities, with neither TOTP nor a password ever set -- nothing
      further is required beyond the session and the confirmation
      above. There is no third credential to ask for: this app has no
      concept of "re-run an OAuth consent screen mid-session" (that is
      a full redirect round trip through a provider that owns it, not a
      value this request could carry), and asking for a secret the
      account genuinely does not hold would not add a real barrier --
      it would only ever be satisfiable by leaving the field blank,
      which is no barrier at all. An OAuth-only account's real
      protection here is the same one every other route in this module
      already leans on: the provider's own session/cookie jar on
      whatever device is signed into it.

    No separate rate limiter guards the password/TOTP checks above --
    same posture POST /api/account/password's own current_password
    check already has today (no limiter there either): scrypt's own
    cost already throttles a password-guessing loop to a handful of
    attempts a second, TOTP guessing is bounded the same way DELETE
    /api/account/totp's own `_disable_account_limiter` already bounds
    it for that route (a fresh limiter instance here would just be a
    second budget for the identical guessing problem, not additional
    protection), and neither of those existing limiters is reused
    directly here either, matching this codebase's own "a rate-limit
    bucket is private to the one call site that owns it" convention
    (see app/auth.py's module docstring).

    ---- atomicity -------------------------------------------------------

    Every delete and the one UPDATE (tombstoning `player`) run inside
    ONE WriteSession -- app/db.py's global single-writer transaction
    every write in this codebase already serializes through. Either
    every row in _ACCOUNT_SCOPED_TABLES/_PLAYER_SCOPED_TABLES is gone,
    the player row (if any) is tombstoned, and the account row itself
    is gone, all in the same COMMIT -- or, on any exception anywhere in
    that block, WriteSession's own __aexit__ rolls the whole transaction
    back and nothing changed at all (see app/db.py's WriteSession
    docstring). There is no window where some tables are cleared and
    others are not: a half-deleted account is worse than a failed
    request that can simply be retried.

    ---- what "signed out afterward" actually is here ---------------------

    account_session is one of the tables _ACCOUNT_SCOPED_TABLES deletes
    -- including the row this very request's own cookie names. There is
    no separate "and also revoke this session" step the way POST
    /api/account/logout calls revoke_session(): by the time this
    transaction commits, the row app/sessions.py's require_session()
    would need to find on the caller's NEXT request no longer exists,
    so the account is signed out as a direct consequence of what this
    route already does to its own data, not an extra action bolted on.
    clear_session_cookie() below only clears the browser's copy of a
    token that already can't authenticate anything -- the same
    belt-and-suspenders shape every other route in this module already
    gives a dead session (see logout()/logout_all() just above).
    """
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        body = {}

    confirm = body.get("display_name")
    now = int(time.time())

    async with WriteSession() as conn:
        player_row = None
        if session.player_id is not None:
            player_row = conn.execute(
                "SELECT player_id, display_name FROM player WHERE player_id = ?",
                (session.player_id,),
            ).fetchone()

        if player_row is not None:
            if not isinstance(confirm, str) or confirm != player_row["display_name"]:
                return JSONResponse(
                    {"error": "display name does not match"}, status_code=409
                )
        else:
            if not isinstance(confirm, str) or confirm != _NO_PLAYER_CONFIRM_PHRASE:
                return JSONResponse(
                    {
                        "error": f'type "{_NO_PLAYER_CONFIRM_PHRASE}" in display_name '
                        f"to confirm -- this account has no linked player to name"
                    },
                    status_code=409,
                )

        totp_row = conn.execute(
            "SELECT activated_at FROM account_totp WHERE account_id = ?",
            (session.account_id,),
        ).fetchone()
        totp_active = totp_row is not None and totp_row["activated_at"] is not None

        if totp_active:
            code = body.get("totp_code")
            recovery_code = body.get("totp_recovery_code")
            ok = False
            if isinstance(code, str) and code:
                ok = verify_and_consume_totp_code(
                    conn, account_id=session.account_id, code=code, now=now
                )
            if not ok and isinstance(recovery_code, str) and recovery_code:
                ok = verify_and_consume_recovery_code(
                    conn, account_id=session.account_id, raw_code=recovery_code, now=now
                )
            if not ok:
                return JSONResponse(
                    {"error": "a current two-factor code or recovery code is required"},
                    status_code=401,
                )
        else:
            existing_password = _load_password(conn, session.account_id)
            if existing_password is not None:
                password = body.get("password")
                if not isinstance(password, str) or not password:
                    return JSONResponse(
                        {"error": "password is required"}, status_code=400
                    )
                if not verify_password(password, existing_password):
                    return JSONResponse(
                        {"error": "password is incorrect"}, status_code=401
                    )

        # ---- past this point, the request is fully authenticated and
        # confirmed -- everything below is the actual, irreversible
        # deletion. See this module's "account deletion" section
        # comment above for why each table is here.
        counts: dict[str, int] = {}

        if player_row is not None:
            player_id = player_row["player_id"]
            for table in _PLAYER_SCOPED_TABLES:
                c = conn.execute(
                    f"DELETE FROM {table} WHERE player_id = ?", (player_id,)
                )
                counts[table] = c.rowcount

            conn.execute(
                "UPDATE player SET display_name = ?, disabled_at = ?, account_id = NULL "
                "WHERE player_id = ?",
                (_tombstone_display_name(player_id), now, player_id),
            )

        for table in _ACCOUNT_SCOPED_TABLES:
            c = conn.execute(
                f"DELETE FROM {table} WHERE account_id = ?", (session.account_id,)
            )
            counts[table] = c.rowcount

        conn.execute("DELETE FROM account WHERE account_id = ?", (session.account_id,))

    # Same cache-staleness fix POST /api/account/rotate-key and
    # app/admin_api.py's player_delete/reissue already apply, after
    # commit, so a key this transaction just revoked-by-deletion cannot
    # keep authenticating at the ingest endpoint until its cache entry
    # naturally expires (settings.mc_key_cache_seconds).
    if player_row is not None:
        ingestor = request.app.state.mc_ingestor
        ingestor.invalidate_player(player_row["player_id"])

    response = JSONResponse({"ok": True}, status_code=200)
    clear_session_cookie(response)
    return response


# ---- player-facing data: stats / honors / checkins / checkin-health ------
#
# Everything below is scoped to the signed-in account's OWN linked
# player -- session.player_id, resolved fresh on every request by
# require_session() (see app/sessions.py). None of these routes accept
# a player_id or a name; there is no way to ask this router about
# anyone but yourself. A session with no linked player yet (the account
# exists, POST /api/account/link-key was never called) gets the same
# 404 from all four, rather than an empty-shaped 200 that would read as
# "you have zero of everything" instead of "you have no player."

def _no_linked_player_error() -> JSONResponse:
    return JSONResponse({"error": "no linked player"}, status_code=404)


@router.get("/api/account/stats")
async def account_stats(session: SessionPrincipal = Depends(require_session)) -> JSONResponse:
    """My current standing, per board.

    app/mc_api.py's find_for() already answers "what does this player
    hold and score right now" for anyone, by name -- it is the same
    query GET /api/mc/find runs for a public lookup. This route calls
    it directly (once per protocol the player's single player row could
    be active on) rather than copying its SQL, and adds only the two
    things find_for() cannot give a player about themselves today:

    - checkin_streak: currently only visible via app/mc_api.py's
      top_checkin_for() (GET /api/mc/top-checkins), and only for the
      top 20. Computed here with checkin.checkin_streak() itself,
      seeded from the player's own most recent net_date in
      mc_checkin_award for that protocol -- the same call the poller
      makes when it writes a new award row, so this is never a second,
      possibly-drifting streak implementation, and it does not depend
      on mc_checkin_award.streak (null on any row written before that
      column existed).
    - nets_checked_in: a plain COUNT over mc_checkin_award, which no
      existing endpoint exposes at all.

    Per-protocol, not merged: mc_checkin_award, mc_tile, and
    place_activation are all keyed (or windowed) by protocol, and
    find_for()'s own docstring is explicit that one player row can hold
    both an 'mc' and an 'mt' radio. A protocol with no active season and
    no history for this player still gets an entry (find_for() degrades
    to zeros rather than omitting it), so "boards" always lists both --
    a player who has only ever played one board sees the other as an
    honest zero, not a missing key to special-case in a client.
    """
    if session.player_id is None:
        return _no_linked_player_error()

    conn = connect()
    try:
        player = conn.execute(
            "SELECT display_name, team FROM player WHERE player_id = ?",
            (session.player_id,),
        ).fetchone()
        if player is None:
            return _no_linked_player_error()
        name = player["display_name"]
        team = player["team"]

        boards: dict[str, dict] = {}
        for protocol in (MC_PROTOCOL, MT_PROTOCOL):
            board = mc_api.find_for(protocol, name)   # its own connection -- see module import comment
            if board is None:
                continue
            row = conn.execute(
                "SELECT COUNT(*), MAX(net_date) FROM mc_checkin_award "
                " WHERE protocol = ? AND player_id = ?",
                (protocol, session.player_id),
            ).fetchone()
            nets_checked_in = row[0] or 0
            latest_net_date = row[1]
            # No checked-in nets on this protocol at all: nothing for
            # checkin_streak() to walk backward from, and "0" is the
            # honest answer, not "1" (which is what passing a made-up
            # net_date would produce -- see that function's own
            # docstring on why it always returns at least 1 once called).
            # Imported here, not at module level (see this module's own
            # import comment) -- and only on this branch, so a player
            # with no check-ins on a protocol never pays for importing
            # app.checkin's heavy chain at all.
            if latest_net_date:
                from .checkin import checkin_streak
                streak = checkin_streak(conn, session.player_id, protocol, latest_net_date)
            else:
                streak = 0
            board["nets_checked_in"] = nets_checked_in
            board["checkin_streak"] = streak
            boards[protocol] = board
    finally:
        conn.close()

    return JSONResponse(
        {"display_name": name, "team": team, "boards": boards}, status_code=200
    )


@router.get("/api/account/honors")
async def account_honors(session: SessionPrincipal = Depends(require_session)) -> JSONResponse:
    """My past honors: this player's own rows out of month_award
    (app/db.py:954), across every FINISHED month, newest first.

    Read-only in the strongest sense -- a frozen month is immutable
    history by design (see app/results.py:19-25's own docstring), and
    nothing here recomputes or rewrites a single figure; this is a
    plain SELECT with a player_id filter that app/results.py's own
    site-wide /results endpoint (month_results_for()) does not offer.

    month_award.player_id is NULL for a team-scoped award (Largest
    Territory, Longest Road) -- filtering on it already leaves only
    this player's own honors (Empire Builder, Top Attacker, Tourist,
    and so on), never a team's, with no separate award-type check
    needed. `label` is the same results.AWARD_LABELS lookup
    month_results_for() applies, so a retired award a player won while
    it still existed (see AWARD_LABELS' own comments on
    most_consistent/top_netop/explorer/month_winner) still reads with
    its real name instead of a raw key.
    """
    if session.player_id is None:
        return _no_linked_player_error()

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT month, protocol, award, scope, value, detail FROM month_award "
            " WHERE player_id = ? ORDER BY month DESC, protocol, award",
            (session.player_id,),
        ).fetchall()
    finally:
        conn.close()

    honors = [
        {
            "month": r["month"],
            "protocol": r["protocol"],
            "award": r["award"],
            "label": results.AWARD_LABELS.get(r["award"], r["award"]),
            "scope": r["scope"],
            "value": r["value"],
            "detail": r["detail"],
        }
        for r in rows
    ]
    return JSONResponse({"honors": honors}, status_code=200)


@router.get("/api/account/checkins")
async def account_checkins(
    session: SessionPrincipal = Depends(require_session),
    limit: int = Query(50, ge=1, le=200),
) -> JSONResponse:
    """My check-in history: this player's own credited rows out of
    mc_checkin_award, newest net first. No existing endpoint lists a
    single player's check-ins today -- app/mc_api.py's top_checkin_for()
    (GET /api/mc/top-checkins) only ranks the top 20 players' current
    point TOTAL, never any one player's row-by-row history.

    `streak` on each row is the value that was actually stored on it at
    award time (null for anything written before the streak column
    existed -- see mc_checkin_award's own comment in app/db.py), so
    reading this list top to bottom is watching the same streak
    GET /api/account/stats reports get built one net at a time.
    """
    if session.player_id is None:
        return _no_linked_player_error()

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT season_id, net_date, points, protocol, streak, awarded_at, message_ts "
            "  FROM mc_checkin_award WHERE player_id = ? "
            " ORDER BY net_date DESC, protocol LIMIT ?",
            (session.player_id, limit),
        ).fetchall()
    finally:
        conn.close()

    return JSONResponse({"checkins": [dict(r) for r in rows]}, status_code=200)


# ---- checkin-health --------------------------------------------------
#
# The plain-English fix for each contact status below is deliberately
# actionable, not just descriptive -- this is the endpoint the spec
# calls "the one to get right," because today the only person who can
# see WHY a MeshCore check-in isn't landing is an operator reading
# app/admin_ops.py's overview, and roughly four in ten bound contacts
# fail to resolve. Every case a player can hit is covered here; there
# is no catch-all "unknown" bucket to hide behind.
_CONTACT_EXPLANATIONS = {
    "resolved": (
        "This contact's key resolves to a name in the check-in "
        "directory, so a message it posts under that exact name is "
        "eligible to be credited. See the headline diagnosis above for "
        "whether it actually has been."
    ),
    "not_in_directory": (
        "This contact's key has never shown up in the check-in directory, "
        "so it cannot be matched to a name yet. In MeshMapper, check "
        "Settings, API Endpoints, Include Contact Key is turned on, and "
        "wardrive with it a little -- the directory picks new radios up "
        "on its own once they've been heard. Or use the Confirm my node "
        "section above to prove this specific radio is yours right now."
    ),
    "key_ambiguous": (
        "This contact's key prefix currently matches more than one entry "
        "in the directory, so it is refused rather than guessed at -- a "
        "wrong credit is worse than a missed one. This is not something "
        "you can fix yourself -- flag it to an operator."
    ),
    "name_ambiguous": (
        "This contact resolves to a display name that more than one "
        "radio in the directory is currently using, so it is refused "
        "rather than risk crediting the wrong person. Rename this "
        "companion node to something nobody else's radio is using."
    ),
}


def _checkin_contacts_status(conn, player_id: int, directory: list[dict]) -> list[dict]:
    """Every one of this player's bound MeshCore contacts (player_node,
    protocol='mc'), each classified against `directory` by
    checkin.mc_contact_status() -- the exact same per-contact decision
    checkin._build_directory_bridge() makes at check-in time, just not
    thrown away for the cases that don't cleanly resolve.
    """
    from .checkin import _index_mc_directory, mc_contact_status

    by_prefix, ambiguous_names = _index_mc_directory(directory)
    rows = conn.execute(
        "SELECT node_ref, bound_at FROM player_node "
        " WHERE protocol = ? AND player_id = ? ORDER BY bound_at",
        (MC_PROTOCOL, player_id),
    ).fetchall()

    out = []
    for r in rows:
        contact = r["node_ref"]
        status = mc_contact_status(by_prefix, ambiguous_names, contact)
        out.append({
            "node_ref": contact,
            "bound_at": r["bound_at"],
            "status": status["status"],
            "resolved_name": (
                status["name"] if status["status"] in ("resolved", "name_ambiguous") else None
            ),
            "match_count": status["match_count"],
            "explanation": _CONTACT_EXPLANATIONS[status["status"]],
        })
    return out


def _fmt_points(points) -> str:
    """`10.0` -> "10", `12.5` -> "12.5" -- mc_checkin_award.points is a
    REAL column so every value round-trips through here as a float,
    but a whole-number award (the overwhelming common case: checkin_
    config.points plus an integer streak bonus) should read as a plain
    integer in player-facing text, not "10.0".
    """
    return f"{points:g}"


def _diagnose_checkin_health(
    contacts: list[dict], most_recent_net_date: str | None, credited_points,
) -> tuple[str, str]:
    """The headline (state, summary) for GET /api/account/checkin-health,
    computed from REALITY -- whether this player was actually credited
    -- not from whether something merely LOOKS bindable. See this
    endpoint's own docstring for why that distinction is the entire
    point of this function existing: the previous version of this
    panel inferred "resolved" from contact/binding status alone and
    could report a player as fine while every one of their check-ins
    went uncredited.

    `credited_points` is the player's mc_checkin_award.points for
    `most_recent_net_date` if that exact row exists, else None -- the
    caller has already done the one query this decision actually turns
    on. `contacts` is _checkin_contacts_status()'s output (this
    player's own bound MeshCore radios only -- never another player's
    name or contact, see this endpoint's own docstring on that).

    Six states, priority order, each ending in one concrete next step
    that only ever points at something this same page renders (My
    radios, Confirm my node, both ABOVE this panel in
    frontend/account.html -- see that file for why):

    1. credited: an award exists for the most recent net date. Nothing
       else matters once this is true -- report it and stop.
    2. resolving_uncredited: no contact is credited yet, at least one
       resolves in the directory. THE state this function exists to be
       able to say -- a resolving contact used to read as "fine" no
       matter what mc_checkin_award said. Directory resolution is
       necessary for a check-in to land but never sufficient (the
       radio still has to actually post under that exact name during
       the window), so this is reported as a real problem, not
       downgraded to "everything's fine."
    3. not_in_directory: no contact resolves or is ambiguous, but at
       least one is bound and simply hasn't shown up in the directory.
    4. name_ambiguous: a bound contact's display name collides with
       another radio in the directory.
    5. key_ambiguous: a bound contact's key prefix collides with
       another directory entry -- not fixable by the player at all.
    6. nothing_bound: no MeshCore contact bound at all.

    A player can be in more than one of 3/4/5 at once with several
    bound radios; priority order picks the single most actionable one
    to lead with, same "refuse rather than guess" spirit as
    checkin._build_directory_bridge()'s own ambiguity handling -- this
    just orders outcomes instead of refusing one.
    """
    if credited_points is not None:
        return "credited", (
            f"You were credited {_fmt_points(credited_points)} point(s) for the "
            f"{most_recent_net_date} net. No further action needed."
        )

    date_text = most_recent_net_date or "the most recent net"

    resolved = [c for c in contacts if c["status"] == "resolved"]
    if resolved:
        names = sorted({c["resolved_name"] for c in resolved if c["resolved_name"]})
        names_text = " and ".join(names) if len(names) <= 1 else (
            ", ".join(names[:-1]) + f", and {names[-1]}"
        )
        return "resolving_uncredited", (
            f"Your radio is in the directory as {names_text}, but you were not "
            f"credited on {date_text}. Check-ins only count when the radio posts "
            "under exactly that name -- so either set the radio back to that "
            "name, or confirm the radio that actually posts, using the Confirm "
            "my node section above."
        )

    not_in_directory = [c for c in contacts if c["status"] == "not_in_directory"]
    if not_in_directory:
        refs = ", ".join(c["node_ref"] for c in not_in_directory)
        return "not_in_directory", (
            f"Your bound radio(s) ({refs}) have not shown up in the check-in "
            "directory yet, so they cannot be matched to a name -- advertising "
            "is what puts a radio in the directory. If one of these isn't the "
            "radio you actually use, remove it in My radios above; otherwise, "
            "wardrive it, or use the Confirm my node section above to prove "
            "which one is yours right now."
        )

    name_ambiguous = [c for c in contacts if c["status"] == "name_ambiguous"]
    if name_ambiguous:
        return "name_ambiguous", (
            "Your radio's display name in the check-in directory is currently "
            "shared by another radio, so check-ins under it are refused rather "
            "than risk crediting the wrong person. Rename the companion node to "
            "something nobody else's radio is using."
        )

    key_ambiguous = [c for c in contacts if c["status"] == "key_ambiguous"]
    if key_ambiguous:
        return "key_ambiguous", (
            "Your radio's key currently matches more than one entry in the "
            "check-in directory. This is not something you can fix yourself -- "
            "flag it to an operator."
        )

    return "nothing_bound", (
        "You have no MeshCore contact bound, so you cannot earn MeshCore net "
        "check-ins yet. Use the Confirm my node section above to get started."
    )


@router.get("/api/account/checkin-health")
async def account_checkin_health(
    request: Request, session: SessionPrincipal = Depends(require_session),
) -> JSONResponse:
    """Why my check-ins may not be counting.

    Gives a player the same diagnosis app/admin_ops.py's overview
    already gives an operator about them (checkin_unreachable,
    checkin_name_changed) -- but self-serve.

    The headline (`state`/`summary`) is derived from whether this
    player was actually CREDITED for the most recent MeshCore net, not
    from whether a contact merely looks bindable -- see
    _diagnose_checkin_health()'s own docstring for exactly why that
    distinction matters: a bound contact resolving in the directory is
    necessary for a check-in to land, but it is not sufficient, and the
    previous version of this endpoint conflated the two, reporting a
    player as fine while every one of their check-ins credited nobody.
    checkin.most_recent_mc_net_date() answers "when did a MeshCore net
    most recently run" straight off checkin_net's own schedule, not off
    mc_checkin_award -- see that function's own docstring for why
    asking the award table "when was the most recent net" would hide
    exactly the failure this endpoint exists to catch (a net that ran
    and credited nobody at all).

    `contacts` (per-contact detail, unchanged shape from before) is
    kept alongside the headline because it's still useful once a player
    knows THAT something's wrong -- it's just no longer what decides
    whether something's wrong. It can never contain anyone else's
    contact or sender name: `_checkin_contacts_status` reads only
    player_node rows already bound to THIS player_id, and nothing here
    reads the operator-only unresolved-sender log a name any bound or
    unbound caller could later claim -- that stays admin-only, see
    app/admin_ops.py.

    Reads the check-in poller's own cached directory
    (request.app.state.checkin_poller.directory_snapshot(), the
    UNION across every configured connector) -- the same source
    app/admin_ops.py's _attention() and app/checkin_api.py's node
    picker already read from, and never a fresh upstream fetch for a
    page load. With no poller running (or nothing cached yet), the
    directory is empty and every contact reports "not_in_directory" --
    an honest answer, not a 500: there is genuinely nothing to resolve
    against right now.
    """
    if session.player_id is None:
        return _no_linked_player_error()

    from .checkin import most_recent_mc_net_date

    poller = getattr(request.app.state, "checkin_poller", None)
    directory = poller.directory_snapshot() if poller is not None else []

    conn = connect()
    try:
        contacts = _checkin_contacts_status(conn, session.player_id, directory)
        most_recent_net_date = most_recent_mc_net_date(conn)
        credited_points = None
        if most_recent_net_date is not None:
            row = conn.execute(
                "SELECT points FROM mc_checkin_award "
                " WHERE player_id = ? AND protocol = ? AND net_date = ?",
                (session.player_id, MC_PROTOCOL, most_recent_net_date),
            ).fetchone()
            credited_points = row["points"] if row is not None else None
    finally:
        conn.close()

    state, summary = _diagnose_checkin_health(contacts, most_recent_net_date, credited_points)

    return JSONResponse(
        {
            "resolved": state == "credited",
            "state": state,
            "summary": summary,
            "most_recent_net_date": most_recent_net_date,
            "contacts": contacts,
        },
        status_code=200,
    )
