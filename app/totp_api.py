"""FastAPI router for TOTP two-factor authentication: enrollment,
activation, disabling, and the second-factor challenge every guarded
sign-in has to pass through. Mirrors the split app/oauth_api.py already
draws between account-management routes (app/account_api.py) and the
unauthenticated `/auth/*` doors (app/oauth_api.py itself) -- this
module owns BOTH halves of TOTP for the same reason app/oauth_api.py
owns both the pending-identity redemption routes and the OAuth
callbacks themselves: the challenge mechanism below only makes sense
read alongside the enrollment it protects, the same way
resolve_oauth_callback() only makes sense read alongside
account_pending_identity's own redemption routes.

---- which doors this guards, and why ------------------------------------

An account may hold up to FIVE sign-in doors (see app/account_api.py's
own module docstring): GitHub, Google, and Discord OAuth, magic-link
email, and a password. Once an account has TOTP active, a second factor
is required on the two LOCAL doors -- POST /auth/password/start and
GET /auth/email/callback -- and NOT on any OAuth provider. This is a
settled design decision, not a gap:

  - Password and magic-link email are both secrets THIS app alone
    issues and verifies -- nothing external stands between "the person
    typed the right thing" and "this app is now convinced." Guarding
    only one of the two would leave the other as a trivial bypass (an
    attacker who can guess/phish a password would simply use the
    magic-link door instead, or vice versa), which would make TOTP
    decorative rather than a real second factor -- see
    app/oauth_api.py's password_start() and email_callback() for
    exactly where each one calls into this module's own
    totp_active_for_account()/issue_totp_challenge() before ever
    issuing a session.
  - GitHub/Google/Discord each already enforce whatever second factor
    the ACCOUNT HOLDER configured with THAT provider -- Google's own
    2-Step Verification, GitHub's own 2FA requirement, Discord's own
    MFA. Layering this app's own TOTP check on top of an OAuth
    provider's completed consent screen would be friction with no
    security gain: the provider has already done the second-factor
    check this app would otherwise be trying to redo, worse, with
    less context than the provider itself has. app/oauth_api.py's
    oauth_callback() -- the OAuth-specific callback route -- never
    imports anything from this module at all, which is the enforcement
    of that boundary: there is no code path by which an OAuth sign-in
    can reach either function.

---- the intermediate state: a short-lived, single-use challenge ---------

The pending-identity mechanism app/oauth_api.py already built for case
4 of its own callback decision tree (account_pending_identity: a
hashed, single-use, TTL'd token, handed to the browser as an HttpOnly
cookie OR returned in a JSON body for a non-browser caller, redeemed
exactly once) is the direct precedent this module follows for its own
"credential verified, second factor not yet supplied" state
(app/db.py's account_totp_challenge) -- see that table's own comment
for the exact shape. The reasoning for building a NEW table rather than
reusing account_pending_identity itself: that table's own redemption
routes (POST /api/account/pending/{create,link}) are wired
specifically into resolve_oauth_callback()'s account-linking decision
tree, and a row there is read by GET /api/account/pending as
"a brand-new identity waiting for a choice" -- reusing it here would
either have to teach that whole machinery a second, unrelated meaning,
or risk a totp challenge accidentally satisfying an identity-linking
redemption (or vice versa). A dedicated table with the identical shape
costs three lines of schema and keeps the two concerns from ever being
confusable.

CRITICAL, the same way app/oauth_api.py's account_pending_identity
comment states its own critical property: a row in account_totp_challenge
is NEVER, by itself, sufficient to authenticate as its account_id. It
proves only that a FIRST factor already succeeded -- the token exists
purely to survive the round trip from "password/magic-link verified"
to "TOTP code submitted," and POST /auth/totp/verify below is the ONLY
route that may ever turn one into a real session (via the exact same
create_session()/set_session_cookie() every other door already uses).
This is why the challenge is its own table and cookie
(_TOTP_CHALLENGE_COOKIE_NAME), never account_session itself or anything
shaped like it: a session cookie IS a credential the moment it exists,
and this must not be one.

---- rate limiting ---------------------------------------------------------

Three independent budgets, all reusing app/auth.py's
new_rate_limit_bucket()/_BoundedHits -- the same bounded-dictionary
counter every other rate limit in this app already uses, per that
module's own "every call site owns its own budget" convention:

  - POST /api/account/totp/activate and DELETE /api/account/totp are
    both ALREADY session-authenticated, so their limiter is keyed on
    account_id (like app/account_api.py's own
    _contact_email_account_limiter), not source IP -- there is no
    anonymous-caller enumeration risk, only "how many code guesses can
    one session throw."
  - POST /auth/totp/verify is reached with NO session at all (only the
    challenge cookie/token), which makes it the single highest-value
    guessing target this feature adds -- a 6-digit code is only
    1,000,000 possibilities. Two independent budgets there, same
    two-tier shape POST /auth/password/start already uses: per source
    IP, and per the specific challenge being attacked (the "address"
    analog here, since there is no email in this request at all).
"""
from __future__ import annotations

import asyncio
import io
import logging
import secrets
import time

import segno
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from .auth import new_rate_limit_bucket
from .client_ip import get_client_ip
from .config import settings
from .db import WriteSession, connect
from .device_label import device_label_from_user_agent
from .mc_ingest import hash_secret
from .sessions import SessionPrincipal, create_session, require_session, set_session_cookie
from .totp import (
    DEFAULT_SKEW_STEPS,
    TotpEncryptionUnavailable,
    decrypt_secret,
    encrypt_secret,
    generate_recovery_codes,
    generate_secret,
    provisioning_uri,
    secret_to_base32,
    step_for,
    totp_encryption_available,
    verify_totp_code,
)

log = logging.getLogger("totp_api")

router = APIRouter()

# HttpOnly, short-lived, single-use -- carries the account_totp_challenge
# token from a successful password/magic-link sign-in to POST
# /auth/totp/verify, the same way app/oauth_api.py's own
# _PENDING_COOKIE_NAME carries a pending-identity token from a case-4
# callback to POST /api/account/pending/*. Path="/auth": this cookie's
# only consumer is this module's own /auth/totp/* routes (unlike
# app/oauth_api.py's pending cookie, which has to reach both /link's
# page and /api/account/pending/*, this one never has to leave /auth).
_TOTP_CHALLENGE_COOKIE_NAME = "mw_totp_challenge"

# Where a real browser lands to type in a second-factor code -- set by
# app/oauth_api.py's email_callback() when the outcome would otherwise
# be a "login"/"auto_linked" redirect straight to /account (see that
# route's own docstring). POST /auth/password/start never redirects at
# all (it is, and always was, a JSON-only endpoint -- see that route's
# own docstring in app/oauth_api.py), so this constant is consulted by
# app/oauth_api.py alone, never by this module's own routes.
TOTP_VERIFY_PAGE_PATH = "/verify-totp"


def _set_totp_challenge_cookie(response: Response, *, raw_token: str, expires_at: int, now: int) -> None:
    """Same reasoning app/oauth_api.py's _set_pending_cookie() gives for
    its own cookie, word for word: an HttpOnly cookie rather than a
    query-string parameter, because a query string ends up in Referer
    headers, browser history, and access logs, none of which are a safe
    place for a bearer-shaped secret. max_age is capped at the token's
    own remaining lifetime so a browser can never hold a cookie
    pointing at an already-expired challenge.
    """
    response.set_cookie(
        _TOTP_CHALLENGE_COOKIE_NAME,
        raw_token,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.account_session_cookie_secure,
        max_age=max(0, expires_at - now),
    )


def _clear_totp_challenge_cookie(response: Response) -> None:
    response.delete_cookie(
        _TOTP_CHALLENGE_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.account_session_cookie_secure,
    )


# ---- "does this account need a second factor, and issuing one" -----------
#
# Both of these are called from app/oauth_api.py (password_start(),
# and _respond_to_callback_outcome() on email_callback()'s behalf only
# -- see this module's own docstring for why oauth_callback() never
# calls either) -- the one place outside this module TOTP enforcement
# actually happens. Deliberately free of any request/cookie concept
# themselves (they take/return plain values), the same "business logic
# has no HTTP in it" split every other *_login.py module in this
# codebase already draws.

def _totp_active_sync(account_id: int) -> bool:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM account_totp WHERE account_id = ? AND activated_at IS NOT NULL",
            (account_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


async def totp_active_for_account(account_id: int) -> bool:
    """True iff `account_id` has a fully ACTIVATED account_totp row
    (activated_at IS NOT NULL) -- a pending, never-proven row (see
    account_totp's own comment in app/db.py) never guards anything.
    Synchronous sqlite3 call wrapped the same way every other
    request-path db read in this app already is (see
    app/sessions.py's verify_session() for the identical pattern).
    """
    return await asyncio.to_thread(_totp_active_sync, account_id)


# Grace period + sweep shape identical to app/oauth_api.py's own
# _sweep_stale_rows() -- a separate, inline copy here rather than an
# import of that private function, the same "each module keeps its own
# housekeeping for the tables it owns" precedent
# app/account_api.py's set_contact_email() already sets for
# account_contact_email_token.
_SWEEP_GRACE_SECONDS = 3600  # 1 hour


def _sweep_stale_challenges(conn, now: int) -> None:
    cutoff = now - _SWEEP_GRACE_SECONDS
    conn.execute(
        "DELETE FROM account_totp_challenge WHERE expires_at < ? OR (consumed_at IS NOT NULL AND consumed_at < ?)",
        (cutoff, cutoff),
    )


async def issue_totp_challenge(account_id: int) -> tuple[str, int]:
    """Writes a fresh account_totp_challenge row and returns
    (raw_token, expires_at) -- called by app/oauth_api.py the instant a
    password or magic-link sign-in verifies for an account that has
    TOTP active, in place of issuing a real session. Same
    hashed-single-use-ticket minting shape resolve_oauth_callback()'s
    own case 4 uses for account_pending_identity: a fresh
    secrets.token_urlsafe(32), only its hash ever written to disk, the
    raw value returned exactly once for the caller to hand off (as a
    cookie or a JSON field -- see this module's own docstring on why
    both exist).
    """
    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_secret(raw_token)
    now = int(time.time())
    expires_at = now + settings.account_totp_challenge_lifetime_seconds
    async with WriteSession() as conn:
        conn.execute(
            "INSERT INTO account_totp_challenge(token_hash, account_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (token_hash, account_id, now, expires_at),
        )
        _sweep_stale_challenges(conn, now)
    return raw_token, expires_at


# ---- code / recovery-code verification, with the replay guard ------------

def _load_totp_row(conn, account_id: int):
    return conn.execute(
        "SELECT secret_encrypted, activated_at, last_used_step FROM account_totp WHERE account_id = ?",
        (account_id,),
    ).fetchone()


def verify_and_consume_totp_code(conn, *, account_id: int, code: str, now: int | None = None) -> bool:
    """Checks `code` against account_id's own stored secret and, on a
    match, advances account_totp.last_used_step so the identical code
    (or any code from an equal-or-earlier step) can never verify again
    -- see app/totp.py's own "replayed codes" docstring section and
    account_totp.last_used_step's own comment in app/db.py for the full
    reasoning.

    Deliberately does NOT itself require activated_at IS NOT NULL --
    POST /api/account/totp/activate calls this exact function to check
    the code that PROVES a still-pending enrollment, which is only
    possible if this accepts a pending (activated_at IS NULL) row too.
    Every call site that must NOT accept a pending secret (sign-in via
    POST /auth/totp/verify, DELETE /api/account/totp) already checks
    activated_at IS NOT NULL itself before ever reaching this function
    (see each route's own body) -- duplicating that check here would
    make activation impossible rather than adding real defense in
    depth, since every current caller already gates it correctly.

    Takes a caller-owned connection (never opens its own transaction)
    because every call site needs this check and the write it makes
    to be atomic against a second, concurrent guess against the SAME
    code -- the same reasoning app/oauth_api.py's resolve_oauth_callback()
    gives for taking a caller-owned conn rather than managing its own
    WriteSession.

    Returns False (never raises) for every "no" outcome this module
    can produce on its own -- no row at all, secret cannot be
    decrypted (TotpEncryptionUnavailable -- e.g. the encryption key was
    rotated out from under an already-enrolled account), or no
    candidate step in the skew window beats last_used_step. Callers
    never need to distinguish any of these; they are all just "this
    code is not currently valid."
    """
    if now is None:
        now = int(time.time())
    row = _load_totp_row(conn, account_id)
    if row is None:
        return False

    try:
        secret = decrypt_secret(row["secret_encrypted"])
    except TotpEncryptionUnavailable:
        log.warning("totp: could not decrypt secret for account_id=%s -- key rotated?", account_id)
        return False

    if not code or len(code) != 6 or not code.isdigit():
        return False

    last_used_step = row["last_used_step"]
    current_step = step_for(now)
    for delta in range(-DEFAULT_SKEW_STEPS, DEFAULT_SKEW_STEPS + 1):
        candidate_step = current_step + delta
        if last_used_step is not None and candidate_step <= last_used_step:
            # Already used (or older than the most recent accepted
            # step) -- see this function's own docstring and
            # app/totp.py's "replayed codes" section for why this is
            # checked BEFORE doing the HMAC work for this candidate,
            # not just before accepting it: a step that could never be
            # accepted has no business being compared at all.
            continue
        if verify_totp_code(secret, code, now=candidate_step * 30, skew_steps=0):
            conn.execute(
                "UPDATE account_totp SET last_used_step = ? WHERE account_id = ?",
                (candidate_step, account_id),
            )
            return True
    return False


def _recovery_codes_remaining_sync(conn, account_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM account_totp_recovery_code WHERE account_id = ? AND used_at IS NULL",
        (account_id,),
    ).fetchone()
    return row["n"] if row else 0


def verify_and_consume_recovery_code(conn, *, account_id: int, raw_code: str, now: int | None = None) -> bool:
    """Checks `raw_code` against account_id's own UNUSED recovery-code
    hashes and, on a match, marks that one row used -- single-use, the
    same "consumed_at makes redemption idempotently single-use" shape
    every hashed-single-use ticket table in this app already follows.
    Normalizes the same way the codes are generated (uppercase --
    app/totp.py's _RECOVERY_CODE_ALPHABET is uppercase-only), so a
    person retyping one from a note is not tripped up by case alone.
    """
    if now is None:
        now = int(time.time())
    if not raw_code:
        return False
    normalized = raw_code.strip().upper()
    code_hash = hash_secret(normalized)
    row = conn.execute(
        "SELECT code_id FROM account_totp_recovery_code"
        " WHERE account_id = ? AND code_hash = ? AND used_at IS NULL",
        (account_id, code_hash),
    ).fetchone()
    if row is None:
        return False
    conn.execute(
        "UPDATE account_totp_recovery_code SET used_at = ? WHERE code_id = ?",
        (now, row["code_id"]),
    )
    return True


# ---- account label for the otpauth:// URI ---------------------------------

def _account_label(conn, account_id: int) -> str:
    """The "account" half of the otpauth://totp/{issuer}:{account}
    label an authenticator app shows (app/totp.py's provisioning_uri())
    -- prefers an address the account holder would actually recognise:
    a verified sign-in identity email first (the same
    account_identity.email_verified = 1 condition
    app/account_api.py's _has_verified_identity_email() already
    checks, first row by linked_at so a re-enrollment is stable rather
    than picking a different one each time), then the account's own
    contact_email (unverified is fine here -- this is a DISPLAY label,
    not a security decision), and only falls back to a bare
    "account-{id}" when neither exists (an account reached purely
    through an OAuth identity with no verified email at all, e.g. a
    GitHub account with a private email setting).
    """
    row = conn.execute(
        "SELECT email FROM account_identity"
        " WHERE account_id = ? AND email_verified = 1 AND email IS NOT NULL"
        " ORDER BY linked_at LIMIT 1",
        (account_id,),
    ).fetchone()
    if row is not None and row["email"]:
        return row["email"]
    row = conn.execute(
        "SELECT contact_email FROM account WHERE account_id = ?", (account_id,)
    ).fetchone()
    if row is not None and row["contact_email"]:
        return row["contact_email"]
    return f"account-{account_id}"


# ---- rate limiters (see this module's own docstring, "rate limiting") ----

_activate_account_limiter = new_rate_limit_bucket()
_disable_account_limiter = new_rate_limit_bucket()
_verify_ip_limiter = new_rate_limit_bucket()
_verify_challenge_limiter = new_rate_limit_bucket()


# ---- enrollment / activation / disable (session-authenticated) -----------

def _render_qr_svg(uri: str) -> str:
    """Renders `uri` (an otpauth:// URI) to a self-contained inline SVG
    string via segno -- pure Python, no dependencies of its own (see
    requirements.txt's own comment on why it was added for exactly
    this), so nothing here runs client-side QR generation or fetches
    anything from a CDN. error="m" (segno's default) is a reasonable
    middle ground for a code with no logo/overlay to protect against;
    dark/light are both fully opaque (#000000/#ffffff, not
    transparent) because a QR code needs real contrast to scan
    reliably regardless of the page's own light/dark theme -- see the
    enrollment route's own docstring for why an inline <svg>, not a
    data: URI or a rendered PNG.
    """
    qr = segno.make(uri, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="svg", xmldecl=False, svgns=True, scale=5, border=2, dark="#000000", light="#ffffff")
    return buf.getvalue().decode("utf-8")


@router.post("/api/account/totp/enroll")
async def totp_enroll(session: SessionPrincipal = Depends(require_session)) -> JSONResponse:
    """Starts (or restarts) enrollment: generates a brand-new secret,
    stores it PENDING (account_totp.activated_at left NULL -- it does
    not guard sign-in yet, see that column's own comment in app/db.py),
    and returns everything an authenticator app needs to add it --
    the base32 secret as text (for manual entry) and a QR code
    (segno-rendered inline SVG -- see this route's own "why an SVG
    string" note below) of the same otpauth:// URI.

    404s -- the same "indistinguishable from not existing" contract
    every other unconfigured optional feature in this app already uses
    (app/email_login.py's email_login_enabled(), app/oauth.py's
    provider_enabled()) -- when settings.account_totp_encryption_key is
    unset: see app/totp.py's own "secret at rest" docstring section for
    why this fails closed rather than ever generating a secret it has
    no safe way to store.

    409s if this account already has an ACTIVE (not merely pending)
    secret -- re-enrolling over a working second factor without first
    disabling it (which requires proving a CURRENT code -- see DELETE
    below) would let a hijacked session silently swap in an attacker's
    own secret. Re-running enroll while still PENDING (activation was
    never completed) is fine and expected -- it simply replaces the
    unproven secret with a fresh one, same reasoning account_totp's own
    comment in app/db.py gives for why a pending row is never a
    conflict.

    ---- why an inline SVG string, not a data: URI or a rendered PNG ------

    segno renders directly to SVG XML text (kind="svg") -- returned
    here as a plain string for the frontend to insert with innerHTML
    into a container element, never an <img src="data:..."> data URI.
    An inline <svg> is real DOM (inspectable, no separate image decode,
    crisp at any zoom since it is vector, and -- unlike a data URI --
    never counted as "fetched from a CDN" by anything auditing this
    page's network requests, because it makes no request at all). No
    dependency beyond segno itself (pure Python, already added to
    requirements.txt for exactly this) reaches the browser; nothing
    here runs client-side QR generation or touches a third-party host.
    """
    if not totp_encryption_available():
        return JSONResponse({"error": "not found"}, status_code=404)

    now = int(time.time())
    async with WriteSession() as conn:
        existing = _load_totp_row(conn, session.account_id)
        if existing is not None and existing["activated_at"] is not None:
            return JSONResponse(
                {"error": "two-factor authentication is already enabled -- disable it first"},
                status_code=409,
            )

        secret = generate_secret()
        secret_encrypted = encrypt_secret(secret)
        conn.execute(
            "INSERT INTO account_totp(account_id, secret_encrypted, created_at, activated_at, last_used_step) "
            "VALUES (?, ?, ?, NULL, NULL) "
            "ON CONFLICT(account_id) DO UPDATE SET "
            "  secret_encrypted = excluded.secret_encrypted, created_at = excluded.created_at, "
            "  activated_at = NULL, last_used_step = NULL",
            (session.account_id, secret_encrypted, now),
        )
        label = _account_label(conn, session.account_id)

    uri = provisioning_uri(secret=secret, account_label=label, issuer=settings.account_totp_issuer)
    qr_svg = _render_qr_svg(uri)

    return JSONResponse(
        {
            "secret": secret_to_base32(secret),
            "otpauth_uri": uri,
            "qr_svg": qr_svg,
        },
        status_code=200,
    )


@router.post("/api/account/totp/activate")
async def totp_activate(request: Request, session: SessionPrincipal = Depends(require_session)) -> JSONResponse:
    """Proves the authenticator app added by POST .../enroll actually
    works: takes one 6-digit `code`, and -- ONLY on a valid match --
    flips account_totp.activated_at (making the secret real for
    sign-in from this moment on) and mints
    settings.account_totp_recovery_code_count fresh recovery codes,
    returned in the response body EXACTLY ONCE (see
    account_totp_recovery_code's own comment in app/db.py -- only the
    hash is ever stored, this is the one and only time the plaintext
    list exists anywhere outside the caller's own screen).

    Requires a PENDING row to exist at all (404 if enroll was never
    called) and rejects an already-active account (409 -- activation is
    a one-time step, not a re-verification; DELETE .../totp + a fresh
    enroll is the only way to rotate the secret, so re-running this
    can never do so silently).

    Rate limited per-account (see this module's own docstring, "rate
    limiting") -- this endpoint is reached by an ALREADY-authenticated
    session, but still takes a guessable 6-digit code, so it is not
    exempt from a guessing budget just because a cookie is present.
    """
    if _activate_account_limiter.limited(
        str(session.account_id),
        limit=settings.account_totp_activate_rate_limit_attempts,
        window=settings.account_totp_activate_rate_limit_window_seconds,
    ):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        body = None
    code = body.get("code") if isinstance(body, dict) else None
    if not isinstance(code, str) or not code:
        return JSONResponse({"error": "code is required"}, status_code=400)

    now = int(time.time())
    async with WriteSession() as conn:
        existing = _load_totp_row(conn, session.account_id)
        if existing is None:
            return JSONResponse({"error": "no pending two-factor enrollment"}, status_code=404)
        if existing["activated_at"] is not None:
            return JSONResponse({"error": "two-factor authentication is already enabled"}, status_code=409)

        if not verify_and_consume_totp_code(conn, account_id=session.account_id, code=code, now=now):
            return JSONResponse({"error": "invalid code"}, status_code=401)

        conn.execute(
            "UPDATE account_totp SET activated_at = ? WHERE account_id = ?", (now, session.account_id)
        )
        conn.execute(
            "DELETE FROM account_totp_recovery_code WHERE account_id = ?", (session.account_id,)
        )
        plain_codes = generate_recovery_codes(settings.account_totp_recovery_code_count)
        conn.executemany(
            "INSERT INTO account_totp_recovery_code(account_id, code_hash, created_at) VALUES (?, ?, ?)",
            [(session.account_id, hash_secret(c), now) for c in plain_codes],
        )
        conn.execute(
            "INSERT INTO account_link_event(account_id, kind, detail, actor, created_at) "
            "VALUES (?, 'totp_enabled', NULL, 'user', ?)",
            (session.account_id, now),
        )

    return JSONResponse({"ok": True, "recovery_codes": plain_codes}, status_code=200)


@router.delete("/api/account/totp")
async def totp_disable(request: Request, session: SessionPrincipal = Depends(require_session)) -> JSONResponse:
    """Turns TOTP off -- requires a valid CURRENT code (`code`, a live
    6-digit TOTP) or an unused `recovery_code` in the request body,
    exactly one of the two. This is deliberate friction, not an
    oversight: without it, anyone who hijacks a session (a stolen
    cookie, an unattended logged-in browser) could simply turn off the
    account's second factor and remove the protection it exists to add
    -- proving a CURRENT factor first closes that off the same way
    app/account_api.py's own password-change route requires the
    current password before accepting a new one.

    Deletes the account_totp row and every account_totp_recovery_code
    row outright (not a soft "disabled" flag) -- see those tables' own
    comments in app/db.py for why: turning TOTP off makes the secret
    and every remaining recovery code moot at once, so there is nothing
    left worth keeping around. A future re-enrollment starts completely
    fresh (POST .../enroll), never resurrecting anything disabled here.
    """
    if _disable_account_limiter.limited(
        str(session.account_id),
        limit=settings.account_totp_disable_rate_limit_attempts,
        window=settings.account_totp_disable_rate_limit_window_seconds,
    ):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        body = None
    code = body.get("code") if isinstance(body, dict) else None
    recovery_code = body.get("recovery_code") if isinstance(body, dict) else None
    if not code and not recovery_code:
        return JSONResponse({"error": "code or recovery_code is required"}, status_code=400)

    now = int(time.time())
    async with WriteSession() as conn:
        existing = _load_totp_row(conn, session.account_id)
        if existing is None or existing["activated_at"] is None:
            return JSONResponse({"error": "two-factor authentication is not enabled"}, status_code=404)

        ok = False
        if isinstance(code, str) and code:
            ok = verify_and_consume_totp_code(conn, account_id=session.account_id, code=code, now=now)
        if not ok and isinstance(recovery_code, str) and recovery_code:
            ok = verify_and_consume_recovery_code(
                conn, account_id=session.account_id, raw_code=recovery_code, now=now
            )
        if not ok:
            return JSONResponse({"error": "invalid code"}, status_code=401)

        conn.execute("DELETE FROM account_totp WHERE account_id = ?", (session.account_id,))
        conn.execute("DELETE FROM account_totp_recovery_code WHERE account_id = ?", (session.account_id,))
        conn.execute(
            "INSERT INTO account_link_event(account_id, kind, detail, actor, created_at) "
            "VALUES (?, 'totp_disabled', NULL, 'user', ?)",
            (session.account_id, now),
        )

    return JSONResponse({"ok": True}, status_code=200)


# ---- the second-factor challenge (unauthenticated -- see module docstring)
#
# Both routes below are JSON-only, unconditionally -- unlike
# app/oauth_api.py's browser-redirect callbacks, there is no
# `?format=json` escape hatch here to choose between a redirect and a
# JSON body (see this module's own docstring on why POST
# /auth/password/start's totp gate never redirects at all; GET
# /auth/totp/challenge and POST /auth/totp/verify inherit that same
# shape for consistency). A non-browser caller (a test, a future
# script) still has a way in with no cookie jar at all: see
# _resolve_totp_challenge_token's own `challenge_token` body fallback
# just below.

async def _resolve_totp_challenge_token(request: Request) -> str | None:
    """Same cookie-preferred, JSON-body-fallback resolution
    app/oauth_api.py's own _resolve_pending_token() uses for its
    pending-identity cookie -- see that function's own docstring for
    the full reasoning (a real browser flow never has to know the raw
    token at all; a test or a future non-browser client can still pass
    `challenge_token` explicitly).
    """
    cookie_token = request.cookies.get(_TOTP_CHALLENGE_COOKIE_NAME)
    if cookie_token:
        return cookie_token
    try:
        body = await request.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        token = body.get("challenge_token")
        if isinstance(token, str) and token:
            return token
    return None


def _load_challenge(conn, raw_token: str, now: int):
    token_hash = hash_secret(raw_token)
    row = conn.execute(
        "SELECT token_hash, account_id, expires_at, consumed_at"
        "  FROM account_totp_challenge WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()
    if row is None or row["consumed_at"] is not None or row["expires_at"] <= now:
        return None
    return row


@router.get("/auth/totp/challenge")
async def totp_challenge_status(request: Request) -> JSONResponse:
    """Describes whether a second-factor prompt is actually pending --
    frontend/verify-totp.js calls this on page load (the same way
    frontend/link.js's loadPending() calls GET /api/account/pending) to
    tell an expired/abandoned/already-used challenge apart from a live
    one, without ever needing the raw token itself (it travels only in
    the HttpOnly cookie -- see this module's own docstring). Reveals
    nothing about WHICH account (no email, no account_id) -- unlike
    GET /api/account/pending (which shows a masked email so a person
    can confirm "yes, that's me"), there is nothing useful to show here
    beyond "yes, keep going" or "no, start over": the person reaching
    this page already typed a real password or clicked a real magic
    link moments ago, so there is no "which identity is this" question
    left to answer, only "is my window to answer still open."
    """
    raw_token = await _resolve_totp_challenge_token(request)
    if not raw_token:
        return JSONResponse({"error": "no pending sign-in"}, status_code=404)

    now = int(time.time())
    conn = connect()
    try:
        row = _load_challenge(conn, raw_token, now)
    finally:
        conn.close()
    if row is None:
        return JSONResponse({"error": "no pending sign-in"}, status_code=404)

    return JSONResponse({"pending": True}, status_code=200)


@router.post("/auth/totp/verify")
async def totp_verify(request: Request) -> JSONResponse:
    """Completes a guarded sign-in: consumes the second-factor
    challenge (single-use, same as every other hashed ticket in this
    app) and checks the submitted `code` (a live TOTP) or
    `recovery_code` (an unused one-time code) against the challenge's
    own account_id. On success, issues a REAL session exactly like
    every other door (create_session + set_session_cookie) -- this is
    the ONLY route in this module (or anywhere else) that ever turns an
    account_totp_challenge row into a session; see this module's own
    docstring for why that boundary matters.

    Rate limited twice over (see this module's own docstring, "rate
    limiting") -- per source IP and per the specific challenge being
    guessed against -- since this route is reached with NO session at
    all, making it the highest-value 6-digit-guessing target this
    feature adds.

    A missing/expired/already-consumed challenge and an invalid code
    both return generically-worded errors (never "your challenge
    expired" vs. "your code was wrong" as genuinely distinct copy)
    for the same "don't reveal which part failed" posture
    app/oauth_api.py's oauth_callback() already applies to its own
    state/PKCE check -- though here it is less about enumeration and
    more about not helping an attacker holding a stolen challenge
    cookie tell "keep guessing" from "give up, it's dead" any faster
    than they'd learn it anyway from the 401/400 split itself.
    """
    ip = get_client_ip(request)
    if _verify_ip_limiter.limited(
        ip,
        limit=settings.account_totp_verify_ip_rate_limit_attempts,
        window=settings.account_totp_verify_ip_rate_limit_window_seconds,
    ):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    raw_token = await _resolve_totp_challenge_token(request)
    if not raw_token:
        return JSONResponse({"error": "no pending sign-in"}, status_code=400)

    if _verify_challenge_limiter.limited(
        hash_secret(raw_token),
        limit=settings.account_totp_verify_challenge_rate_limit_attempts,
        window=settings.account_totp_verify_challenge_rate_limit_window_seconds,
    ):
        return JSONResponse({"error": "rate limited"}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        body = None
    code = body.get("code") if isinstance(body, dict) else None
    recovery_code = body.get("recovery_code") if isinstance(body, dict) else None

    now = int(time.time())
    async with WriteSession() as conn:
        row = _load_challenge(conn, raw_token, now)
        if row is None:
            return JSONResponse({"error": "no pending sign-in"}, status_code=400)
        account_id = row["account_id"]

        ok = False
        if isinstance(code, str) and code:
            ok = verify_and_consume_totp_code(conn, account_id=account_id, code=code, now=now)
        if not ok and isinstance(recovery_code, str) and recovery_code:
            ok = verify_and_consume_recovery_code(conn, account_id=account_id, raw_code=recovery_code, now=now)
        if not ok:
            return JSONResponse({"error": "invalid code"}, status_code=401)

        # Consume the challenge -- single-use, same as every ticket
        # table in this app's family (see account_totp_challenge's own
        # comment in app/db.py). Marked used BEFORE the session is
        # created below, inside the same transaction, so a second
        # concurrent request racing this one can never redeem the same
        # challenge twice even if both read it as still-valid.
        conn.execute(
            "UPDATE account_totp_challenge SET consumed_at = ? WHERE token_hash = ?",
            (now, row["token_hash"]),
        )
        conn.execute("UPDATE account SET last_login_at = ? WHERE account_id = ?", (now, account_id))

    raw_session_token = await create_session(
        account_id, device_label=device_label_from_user_agent(request.headers.get("user-agent"))
    )
    resp = JSONResponse({"result": "login", "account_id": account_id}, status_code=200)
    set_session_cookie(resp, raw_session_token)
    _clear_totp_challenge_cookie(resp)
    return resp
