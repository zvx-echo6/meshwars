"""OAuth 2.0 provider layer: the table of providers this app knows how
to sign in through, and the plumbing (authorize URL, PKCE, token
exchange, identity fetch) shared by every one of them. app/oauth_api.py
is the FastAPI router that drives this table through an actual HTTP
request/response cycle and implements the callback decision tree (who
gets logged in, linked, or asked to choose) -- nothing in THIS module
touches a database, a session, or a cookie. That split exists so the
provider table can be tested (and reasoned about) with nothing but
httpx mocks, no app/db.py, no FastAPI TestClient.

---- table-driven, so a new provider is a config entry ---------------

Every provider this app can ever sign in through is one Provider entry
in PROVIDERS below: its authorize/token/userinfo URLs, its scopes, and
one function (extract_identity) that turns that provider's own
token+userinfo shape into the three facts every provider ultimately
has to produce -- (subject, email, email_verified). GITHUB, DISCORD,
and GOOGLE are implemented; adding apple later means writing that
provider's own extract_identity (every provider's userinfo response is
shaped a little differently -- that variation is irreducible, not
something a shared code path could paper over) and adding one
Provider(...) entry to the table. Nothing in
app/oauth_api.py's routes, app/oauth.py's flow functions
(build_authorize_url/exchange_code/fetch_identity), or the callback
decision tree changes to add one.

Apple is a deliberate exception to "provider = userinfo endpoint,"
called out here rather than built: Apple's authorization code exchange
returns identity claims directly in a signed id_token (a JWT) on the
token response, because Apple has no userinfo endpoint at all -- there
is nothing to GET. Provider.userinfo_url is typed `str | None` for
exactly this reason (None would mean "decode the token response's
id_token instead of calling out to a URL"), and fetch_identity()
below has a clearly marked branch point for it, but that branch is NOT
implemented -- Apple also needs its own JWT verification (fetching and
caching Apple's JWKS, checking iss/aud/exp) and a client_secret that is
itself a short-lived JWT this app would have to mint and rotate, both
real chunks of work out of scope for this change. Wiring Apple in means
implementing that branch, not touching the shape above it.

---- PKCE (RFC 7636) -----------------------------------------------

Every provider here goes through the authorization-code flow WITH
PKCE, S256 only (never "plain") -- generate_pkce_pair() below. This is
required for GitHub's own PKCE support and cheap insurance for every
future provider even where PKCE is optional rather than required: it
binds the authorization code to the specific request that started the
flow (the code_verifier that produced the code_challenge sent on
/start is the only thing that can redeem the code on /callback), which
closes an authorization-code-interception attack even for a public
client with no client-side secret of its own baked into the redirect
handler.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from .config import settings

# ---- result types --------------------------------------------------------


@dataclass(frozen=True)
class ProviderIdentity:
    """What a provider's callback ultimately tells us about who signed
    in -- the only three facts app/oauth_api.py's callback decision
    tree ever reads. subject is the provider's own opaque, stable
    identifier (never a username or email -- see app/db.py's
    account_identity comment for why the (provider, subject) pair, not
    email, is the identity). email/email_verified are BOTH about the
    identity, never about the account -- a provider that cannot
    determine a verified email (GitHub's own /user response, missing a
    primary+verified entry in /user/emails -- see
    _github_extract_identity below) reports email=None,
    email_verified=False, never a guessed or unverified address.
    """

    subject: str
    email: str | None
    email_verified: bool


class OAuthError(Exception):
    """Raised by exchange_code()/fetch_identity() on any failure talking
    to a provider (network error, non-2xx response, unexpected body
    shape). app/oauth_api.py catches this at its callback route and
    turns it into a single generic error response -- a provider's own
    outage or a malformed response is never something a caller should
    be able to distinguish from any other provider-side failure via the
    response body, the same "don't leak which part failed" posture
    app/auth.py's require_api_key_principal() already applies to a bad
    API key.
    """


# ---- provider table -------------------------------------------------------


@dataclass(frozen=True)
class Provider:
    """One row of the provider table. client_id_setting/
    client_secret_setting are the NAMES of the corresponding
    app/config.py Settings attributes (e.g. "oauth_github_client_id"),
    not the values themselves -- credentials() below reads them off the
    live `settings` object by name on every call, rather than this
    dataclass capturing a value once at import time. That matters for
    two reasons: tests monkeypatch settings.oauth_*_client_id/secret to
    flip a provider on/off per test, which only works if this table
    reads through to the live object every time; and an operator can
    (via .env + a restart, the same as every other setting in this
    app) reconfigure a provider without anything here needing to be
    rebuilt.
    """

    name: str
    authorize_url: str
    token_url: str
    userinfo_url: str | None  # None = no userinfo endpoint (Apple's id_token shape -- see module docstring)
    scopes: tuple[str, ...]
    client_id_setting: str
    client_secret_setting: str
    # Turns (userinfo JSON, an httpx client already available for a
    # follow-up call, the raw access token) into a ProviderIdentity.
    # Takes the http client + access token, not just the userinfo dict,
    # because a provider's identity is not always fully answerable from
    # one response -- see _github_extract_identity below, which makes a
    # SECOND authenticated call (GET /user/emails) because GitHub's
    # primary /user response does not reliably carry a usable email.
    extract_identity: Callable[[dict, httpx.AsyncClient, str], Awaitable[ProviderIdentity]]


def credentials(provider: Provider) -> tuple[str, str]:
    """(client_id, client_secret) for `provider`, read live off
    `settings` by attribute name -- see Provider's own docstring for
    why this is a lookup, not a value baked into the table at import
    time.
    """
    return (
        getattr(settings, provider.client_id_setting),
        getattr(settings, provider.client_secret_setting),
    )


def provider_enabled(provider: Provider) -> bool:
    """A provider is reachable only when BOTH its client id and secret
    are set, and this deployment has a public base URL to build a
    redirect_uri from -- empty means off, never open, the same contract
    every other optional feature in app/config.py uses (join_invite_code,
    admin_token, mc_checkin_base_url, ...). A half-configured provider
    (id set, secret blank, or vice versa) is treated as fully off, never
    as "trust it with an empty secret."
    """
    client_id, client_secret = credentials(provider)
    return bool(client_id and client_secret and settings.oauth_public_base_url)


def redirect_uri_for(provider_name: str) -> str:
    """The exact redirect_uri sent on the authorize request and again on
    the token exchange -- must match, to the byte, whatever redirect
    URI is registered in the provider's own OAuth app console, or the
    provider rejects the request outright. Built from
    settings.oauth_public_base_url (see that setting's own comment in
    app/config.py for why this is a distinct setting from
    settings.public_host).
    """
    return f"{settings.oauth_public_base_url.rstrip('/')}/auth/{provider_name}/callback"


# ---- GitHub -----------------------------------------------------------
#
# GitHub is NOT an OIDC provider -- its OAuth Apps flow has no
# userinfo endpoint in the OIDC sense and no id_token; "userinfo" here
# is GitHub's own REST /user endpoint, and critically, /user's own
# `email` field is unreliable: it is null whenever the user's primary
# email is set private (GitHub account setting, on by default for many
# accounts), and even when populated there is no guarantee it is the
# verified, primary address. The only reliable source is a SECOND
# authenticated call, GET /user/emails (requires the user:email scope),
# which lists every email GitHub knows about with its own
# primary/verified flags per entry. This app takes the entry that is
# BOTH primary AND verified; if none exists, email is treated as
# entirely absent (None, email_verified=False) -- never falling back to
# an unverified or non-primary address, which is exactly the account_
# identity.email_verified contract app/db.py's schema comment already
# requires (an unverified email must never be able to link or
# auto-match an account).

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USERINFO_URL = "https://api.github.com/user"
GITHUB_EMAILS_URL = "https://api.github.com/user/emails"

# read:user for the profile /user call; user:email specifically for
# /user/emails -- GitHub scopes these separately, and /user/emails
# returns 404 without user:email even though /user alone would succeed.
GITHUB_SCOPES = ("read:user", "user:email")


async def _github_extract_identity(
    userinfo: dict, http_client: httpx.AsyncClient, access_token: str
) -> ProviderIdentity:
    """userinfo is GitHub's GET /user response. subject is the
    account's numeric id, stringified -- GitHub's own stable, opaque
    identifier (a username/login can be changed by the user at any
    time and must never be treated as a stable subject). Email is
    resolved via the separate /user/emails call described in this
    section's own comment above, never trusted from userinfo['email']
    directly.
    """
    subject = str(userinfo["id"])

    resp = await http_client.get(
        GITHUB_EMAILS_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        },
    )
    if resp.status_code != 200:
        raise OAuthError(f"github: /user/emails returned {resp.status_code}")

    try:
        emails = resp.json()
    except ValueError as e:
        raise OAuthError("github: /user/emails returned non-JSON body") from e
    if not isinstance(emails, list):
        raise OAuthError("github: /user/emails returned an unexpected shape")

    # Take the entry that is BOTH primary AND verified. GitHub guarantees
    # at most one primary entry, so there is no ambiguity to resolve if
    # more than one candidate matched -- there can't be.
    for entry in emails:
        if not isinstance(entry, dict):
            continue
        if entry.get("primary") is True and entry.get("verified") is True:
            email = entry.get("email")
            if isinstance(email, str) and email:
                return ProviderIdentity(subject=subject, email=email, email_verified=True)

    # No primary+verified entry -- absent, not a fallback to whatever
    # userinfo['email'] happened to hold (which may be an unverified or
    # non-primary address, or simply GitHub's own null-when-private
    # value). See this section's own module comment for why.
    return ProviderIdentity(subject=subject, email=None, email_verified=False)


GITHUB = Provider(
    name="github",
    authorize_url=GITHUB_AUTHORIZE_URL,
    token_url=GITHUB_TOKEN_URL,
    userinfo_url=GITHUB_USERINFO_URL,
    scopes=GITHUB_SCOPES,
    client_id_setting="oauth_github_client_id",
    client_secret_setting="oauth_github_client_secret",
    extract_identity=_github_extract_identity,
)


# ---- Discord -----------------------------------------------------------
#
# Discord's OAuth2 app flow is a much closer fit to plain OIDC-style
# "userinfo" than GitHub's: a single authenticated GET to
# https://discord.com/api/users/@me returns id/username/global_name/
# email/verified all in one response, so unlike GitHub there is no
# second call here -- the `identify` scope gets the profile fields,
# `email` gets the email field populated at all (without it, `email`
# is simply absent from the response). Note the authorize endpoint is
# NOT under /api/ (https://discord.com/oauth2/authorize) while the
# token and userinfo endpoints ARE (https://discord.com/api/...) --
# Discord's own inconsistency, not a typo here.
#
# verified reflects whether DISCORD has confirmed the user controls
# that address -- it says nothing about whether the *username* is
# trustworthy, which is why subject below is the id (a permanent
# snowflake), never the username or global_name (both user-editable,
# and username is even reusable after a user changes it -- see
# app/db.py's account_identity comment for why (provider, subject)
# must be a stable, non-reassignable pair).

DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_USERINFO_URL = "https://discord.com/api/users/@me"

# identify for id/username/global_name, email for the email/verified
# fields -- without the email scope, Discord's /users/@me response
# simply omits `email` and `verified` entirely rather than erroring,
# handled below the same way a GitHub account with no primary+verified
# email is handled: email=None, email_verified=False, never guessed.
DISCORD_SCOPES = ("identify", "email")


async def _discord_extract_identity(
    userinfo: dict, http_client: httpx.AsyncClient, access_token: str
) -> ProviderIdentity:
    """userinfo is Discord's GET /users/@me response. subject is the
    account's snowflake id, a permanent numeric string Discord itself
    treats as the stable identifier -- see this section's own comment
    above for why that is required and username/global_name are not
    usable here.

    email/email_verified come straight off userinfo's own `email`/
    `verified` fields, with no second call needed (unlike GitHub --
    see this section's comment above): if `email` is missing (the
    `email` scope was not granted, or Discord simply has none on file)
    or `verified` is not literally True, this reports email=None,
    email_verified=False -- the exact same "absent, never a guessed or
    unverified address" contract _github_extract_identity applies, and
    the one ProviderIdentity's own docstring requires of every
    provider, not a discord-specific policy.
    """
    subject = str(userinfo["id"])

    email = userinfo.get("email")
    verified = userinfo.get("verified") is True
    if isinstance(email, str) and email and verified:
        return ProviderIdentity(subject=subject, email=email, email_verified=True)

    # Either no email on the response at all (email scope refused, or
    # Discord has none on file) or Discord itself has not verified it --
    # absent, never a fallback to an unverified/missing address.
    return ProviderIdentity(subject=subject, email=None, email_verified=False)


DISCORD = Provider(
    name="discord",
    authorize_url=DISCORD_AUTHORIZE_URL,
    token_url=DISCORD_TOKEN_URL,
    userinfo_url=DISCORD_USERINFO_URL,
    scopes=DISCORD_SCOPES,
    client_id_setting="oauth_discord_client_id",
    client_secret_setting="oauth_discord_client_secret",
    extract_identity=_discord_extract_identity,
)


# ---- Google -------------------------------------------------------------
#
# Google's is a genuine OIDC userinfo endpoint (unlike GitHub's REST
# /user and Discord's own /users/@me), which is why this section's
# field names differ slightly from Discord's even though the shape of
# the code is identical: `sub` is OIDC's name for the stable subject
# (never `email` -- a Google address can be released and reassigned to
# a different person after an account closes or an org's domain
# changes hands, exactly the "never a username or email" rule
# ProviderIdentity's own docstring states), and `email_verified` is
# OIDC's name for what Discord calls `verified`. Same single userinfo
# GET as Discord, no second call -- the `openid` scope is required for
# an OIDC-shaped response at all, `email`/`profile` get the
# email/name/picture claims populated.
#
# Google's token endpoint also returns an `id_token` (a JWT carrying
# these same claims, signed) alongside the access token. This
# implementation deliberately does NOT decode it -- doing so would mean
# fetching and caching Google's JWKS and verifying iss/aud/exp/sig, new
# cryptographic surface this app has no other use for, when the plain
# userinfo GET already answers the exact three-fact contract
# fetch_identity() needs. See this module's own docstring for why that
# id_token path is reserved for Apple, which has no userinfo endpoint
# and so has no simpler option.

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

# openid is what makes this an OIDC request at all (and is what makes
# `sub` show up in the userinfo response); email/profile get the
# email/email_verified and name/picture claims populated -- without
# them the userinfo response would omit those fields entirely, the
# same "absent, not guessed" shape as a scope-starved Discord response.
GOOGLE_SCOPES = ("openid", "email", "profile")


async def _google_extract_identity(
    userinfo: dict, http_client: httpx.AsyncClient, access_token: str
) -> ProviderIdentity:
    """userinfo is Google's GET /v1/userinfo (OIDC) response. subject is
    `sub`, OIDC's own stable, opaque identifier for the account -- see
    this section's own comment above for why `email` must never be
    used as the subject.

    email/email_verified come straight off userinfo's own `email`/
    `email_verified` claims, with no second call needed (same shape as
    Discord -- see _discord_extract_identity above): this reports
    email=None, email_verified=False unless `email_verified` is
    LITERALLY True and `email` is a non-empty string -- the same
    "absent, never a guessed or unverified address" contract every
    other provider's extract_identity in this module applies, not a
    google-specific policy.
    """
    subject = str(userinfo["sub"])

    email = userinfo.get("email")
    verified = userinfo.get("email_verified") is True
    if isinstance(email, str) and email and verified:
        return ProviderIdentity(subject=subject, email=email, email_verified=True)

    # Either no email on the response at all (email scope refused, or
    # Google has none on file), email is present but null, or Google
    # itself has not verified it -- absent, never a fallback to an
    # unverified/missing address.
    return ProviderIdentity(subject=subject, email=None, email_verified=False)


GOOGLE = Provider(
    name="google",
    authorize_url=GOOGLE_AUTHORIZE_URL,
    token_url=GOOGLE_TOKEN_URL,
    userinfo_url=GOOGLE_USERINFO_URL,
    scopes=GOOGLE_SCOPES,
    client_id_setting="oauth_google_client_id",
    client_secret_setting="oauth_google_client_secret",
    extract_identity=_google_extract_identity,
)


# ---- the table itself ------------------------------------------------
#
# Adding apple later: write that provider's own
# _<name>_extract_identity() next to GITHUB's/DISCORD's/GOOGLE's above
# (each provider gets its own section, same shape as this one), build a
# Provider(...) for it, and add it to this dict. Nothing else in this
# module or in app/oauth_api.py's routes needs to change -- get_provider()/
# provider_enabled()/build_authorize_url()/exchange_code()/
# fetch_identity() below are all already provider-agnostic.

PROVIDERS: dict[str, Provider] = {
    "github": GITHUB,
    "discord": DISCORD,
    "google": GOOGLE,
    # "apple": APPLE,      # not yet implemented -- see module docstring
}

# Display label for every provider the system knows about -- not just
# the entries actually wired up in PROVIDERS above. GET /auth/providers
# and GET /api/account/pending (app/oauth_api.py) and GET /api/account
# (app/account_api.py) all read this so the frontend never hardcodes
# "GitHub" capitalization or spells a provider's name itself; the
# single source of truth lives here, not duplicated per page script.
#
# Deliberately broader than PROVIDERS: PROVIDERS today has "github",
# "discord", and "google" wired up ("apple" is a commented-out future
# entry -- see that table's own comment above), and "email" is never
# a PROVIDERS entry at all (list_providers() below adds it by hand,
# since it is not an OAuth provider and has no Provider(...) row). But
# every one of those five names is a name this codebase's own routes,
# comments, and pending-identity rows can produce, so every one gets a
# real label now rather than waiting for its Provider(...) entry to
# land -- a provider missing here is a bug, not something a caller
# should have to guard against with a fallback to the raw lowercase
# name.
PROVIDER_LABELS: dict[str, str] = {
    "github": "GitHub",
    "google": "Google",
    "discord": "Discord",
    "apple": "Apple",
    "email": "Email",
}


def get_provider(name: str) -> Provider | None:
    """The table lookup app/oauth_api.py's routes use for the
    {provider} path parameter -- None for an unknown name, exactly like
    an unenabled provider (see provider_enabled above): both render as
    404, the "indistinguishable from not existing" contract this
    module's own docstring and app/config.py's oauth settings describe.
    """
    return PROVIDERS.get(name)


# ---- PKCE (RFC 7636, S256 only) ---------------------------------------


def generate_state() -> str:
    """CSRF state value -- same entropy budget as every other
    must-not-be-guessable token in this app (app/sessions.py's session
    tokens, app/join_api.py's registration keys): 256 bits via
    secrets.token_urlsafe(32).
    """
    return secrets.token_urlsafe(32)


def generate_pkce_pair() -> tuple[str, str]:
    """Returns (code_verifier, code_challenge). code_verifier is 256
    bits of randomness, base64url-encoded (secrets.token_urlsafe(32) --
    about 43 characters, comfortably inside RFC 7636's required 43-128
    character range). code_challenge is BASE64URL(SHA256(code_verifier)),
    unpadded, per RFC 7636 S256 -- the only method this app ever sends
    (code_challenge_method=S256 in build_authorize_url below); "plain"
    is never used.
    """
    code_verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


# ---- the flow itself ---------------------------------------------------


def build_authorize_url(provider: Provider, *, state: str, code_challenge: str) -> str:
    """The URL app/oauth_api.py's /auth/{provider}/start redirects the
    browser to. redirect_uri is rebuilt from settings here (via
    redirect_uri_for) rather than threaded through as a parameter, so
    there is exactly one place in this codebase that knows how a
    redirect_uri is constructed.
    """
    client_id, _ = credentials(provider)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri_for(provider.name),
        "scope": " ".join(provider.scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "response_type": "code",
    }
    return f"{provider.authorize_url}?{httpx.QueryParams(params)}"


async def exchange_code(
    provider: Provider, *, code: str, code_verifier: str, http_client: httpx.AsyncClient
) -> dict:
    """Trades an authorization code (plus the PKCE verifier that
    produced the challenge sent on /start) for a token response.
    Raises OAuthError on any non-2xx response or a body that can't be
    parsed as JSON -- callers never see a provider's raw error body.

    Accept: application/json is set unconditionally -- GitHub's token
    endpoint defaults to a form-urlencoded response body unless asked
    for JSON explicitly; every other provider's token endpoint already
    returns JSON regardless, so this header is harmless there and
    required here.
    """
    client_id, client_secret = credentials(provider)
    resp = await http_client.post(
        provider.token_url,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri_for(provider.name),
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
        },
        headers={"Accept": "application/json"},
    )
    if resp.status_code != 200:
        raise OAuthError(f"{provider.name}: token exchange returned {resp.status_code}")
    try:
        token_response = resp.json()
    except ValueError as e:
        raise OAuthError(f"{provider.name}: token exchange returned non-JSON body") from e
    if not isinstance(token_response, dict) or "access_token" not in token_response:
        # Some providers (GitHub included) return 200 with an error
        # body (e.g. {"error": "bad_verification_code"}) instead of a
        # non-2xx status for an invalid/expired/already-used code --
        # caught here rather than trusting the HTTP status alone.
        raise OAuthError(f"{provider.name}: token exchange response missing access_token")
    return token_response


async def fetch_identity(
    provider: Provider, token_response: dict, http_client: httpx.AsyncClient
) -> ProviderIdentity:
    """Resolves a token response to a ProviderIdentity. For every
    provider currently in PROVIDERS (GitHub only, today), this means a
    userinfo_url is set and gets GET-ed with the access token; the
    resulting JSON, the http client, and the raw access token are
    handed to the provider's own extract_identity (GitHub's
    needs the token again for its own second call -- see
    _github_extract_identity above).

    The `provider.userinfo_url is None` branch below is Apple's future
    seam (see this module's own docstring): a provider with no
    userinfo endpoint would instead decode token_response['id_token']
    (a JWT) directly, with its own signature/claims verification against
    Apple's JWKS. Not implemented -- raises OAuthError so a
    misconfigured future entry fails loudly instead of silently
    skipping identity resolution.
    """
    access_token = token_response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthError(f"{provider.name}: token response has no usable access_token")

    if provider.userinfo_url is None:
        # Apple's shape -- see module docstring. Not implemented.
        raise OAuthError(f"{provider.name}: id_token-based identity is not implemented")

    resp = await http_client.get(
        provider.userinfo_url,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
    if resp.status_code != 200:
        raise OAuthError(f"{provider.name}: userinfo returned {resp.status_code}")
    try:
        userinfo = resp.json()
    except ValueError as e:
        raise OAuthError(f"{provider.name}: userinfo returned non-JSON body") from e
    if not isinstance(userinfo, dict):
        raise OAuthError(f"{provider.name}: userinfo returned an unexpected shape")

    return await provider.extract_identity(userinfo, http_client, access_token)
