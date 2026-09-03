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
email address (POST /api/account/contact-email), and disconnect a
sign-in identity (DELETE /api/account/identity/{provider}). See
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

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

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
from .password_login import PasswordHash, hash_password, verify_password
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
        "SELECT provider, email, linked_at, last_login_at "
        "  FROM account_identity WHERE account_id = ? ORDER BY linked_at",
        (account_id,),
    ).fetchall()
    per_provider, has_password = _door_counts(conn, account_id)
    total_doors = sum(per_provider.values()) + (1 if has_password else 0)
    return [
        {
            "provider": r["provider"],
            "email": _mask_email(r["email"]),
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
        has_password = _has_password(conn, session.account_id)
        contact_email = _contact_email_out(conn, session.account_id)
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
        verified = conn.execute(
            "SELECT 1 FROM account_identity WHERE account_id = ? AND email_verified = 1 LIMIT 1",
            (session.account_id,),
        ).fetchone()
        if verified is None:
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
        await send_magic_link_email(email, link_url)
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
