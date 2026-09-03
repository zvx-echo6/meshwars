"""Tests for app/oauth.py -- the provider table, PKCE, and the
token-exchange/identity-fetch HTTP flow, entirely independent of
app/oauth_api.py's routes or the callback decision tree (see
tests/test_oauth_api.py for those). Every outbound call to a provider
is intercepted by an httpx.MockTransport handler -- there is no real
network access anywhere in this file.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib

import httpx
import pytest

from app.config import settings
from app.oauth import (
    DISCORD,
    GITHUB,
    GOOGLE,
    PROVIDER_LABELS,
    PROVIDERS,
    OAuthError,
    Provider,
    ProviderIdentity,
    build_authorize_url,
    credentials,
    exchange_code,
    fetch_identity,
    generate_pkce_pair,
    generate_state,
    get_provider,
    provider_enabled,
    redirect_uri_for,
)


def _run(coro):
    return asyncio.run(coro)


def _enable_github(monkeypatch) -> None:
    monkeypatch.setattr(settings, "oauth_github_client_id", "test-client-id")
    monkeypatch.setattr(settings, "oauth_github_client_secret", "test-client-secret")
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")


def _enable_discord(monkeypatch) -> None:
    monkeypatch.setattr(settings, "oauth_discord_client_id", "test-discord-client-id")
    monkeypatch.setattr(settings, "oauth_discord_client_secret", "test-discord-client-secret")
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")


def _enable_google(monkeypatch) -> None:
    monkeypatch.setattr(settings, "oauth_google_client_id", "test-google-client-id")
    monkeypatch.setattr(settings, "oauth_google_client_secret", "test-google-client-secret")
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")


# ---- provider table / enable-disable ------------------------------------


def test_get_provider_returns_github():
    assert get_provider("github") is GITHUB


def test_get_provider_unknown_name_returns_none():
    assert get_provider("not-a-real-provider") is None


def test_get_provider_returns_discord():
    assert get_provider("discord") is DISCORD


def test_get_provider_returns_google():
    assert get_provider("google") is GOOGLE


def test_every_wired_up_provider_has_a_display_label():
    """PROVIDER_LABELS is the single source of truth for how a provider
    name renders in the UI (GET /auth/providers, GET /api/account's
    identities, GET /api/account/pending) -- a provider present in
    PROVIDERS with no entry in PROVIDER_LABELS would silently render as
    its raw lowercase name instead of failing loudly, exactly the bug
    this test exists to catch before it ships. PROVIDER_LABELS is
    allowed to be broader than PROVIDERS (it also covers "email", which
    is never a PROVIDERS entry, and providers commented out in
    PROVIDERS pending their own Provider(...) row) -- only the other
    direction, a PROVIDERS entry missing its label, is a bug.
    """
    missing = [name for name in PROVIDERS if name not in PROVIDER_LABELS]
    assert missing == [], f"PROVIDERS entries missing from PROVIDER_LABELS: {missing}"


def test_provider_disabled_by_default(monkeypatch):
    # conftest.py never sets any oauth_* setting -- a fresh clone/test
    # run must start with every provider off.
    assert provider_enabled(GITHUB) is False
    assert provider_enabled(DISCORD) is False
    assert provider_enabled(GOOGLE) is False


def test_discord_provider_enabled_requires_client_id_and_secret(monkeypatch):
    monkeypatch.setattr(settings, "oauth_discord_client_id", "")
    monkeypatch.setattr(settings, "oauth_discord_client_secret", "")
    assert provider_enabled(DISCORD) is False

    _enable_discord(monkeypatch)
    assert provider_enabled(DISCORD) is True


def test_google_provider_enabled_requires_client_id_and_secret(monkeypatch):
    monkeypatch.setattr(settings, "oauth_google_client_id", "")
    monkeypatch.setattr(settings, "oauth_google_client_secret", "")
    assert provider_enabled(GOOGLE) is False

    _enable_google(monkeypatch)
    assert provider_enabled(GOOGLE) is True


def test_provider_enabled_requires_client_id_and_secret_and_base_url(monkeypatch):
    monkeypatch.setattr(settings, "oauth_github_client_id", "")
    monkeypatch.setattr(settings, "oauth_github_client_secret", "")
    monkeypatch.setattr(settings, "oauth_public_base_url", "")
    assert provider_enabled(GITHUB) is False

    monkeypatch.setattr(settings, "oauth_github_client_id", "id-only")
    assert provider_enabled(GITHUB) is False  # secret and base url still blank

    monkeypatch.setattr(settings, "oauth_github_client_secret", "secret-only")
    assert provider_enabled(GITHUB) is False  # base url still blank -- half configured is still off

    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    assert provider_enabled(GITHUB) is True  # all three set


def test_credentials_reads_settings_live_not_baked_in(monkeypatch):
    """Provider.client_id_setting/client_secret_setting are attribute
    NAMES, read off the live settings object on every call -- proves a
    monkeypatch after the table was built (at import time) still takes
    effect, which is what lets tests flip a provider on/off per test.
    """
    monkeypatch.setattr(settings, "oauth_github_client_id", "first")
    monkeypatch.setattr(settings, "oauth_github_client_secret", "first-secret")
    assert credentials(GITHUB) == ("first", "first-secret")

    monkeypatch.setattr(settings, "oauth_github_client_id", "second")
    assert credentials(GITHUB) == ("second", "first-secret")


def test_redirect_uri_for_uses_public_base_url(monkeypatch):
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test")
    assert redirect_uri_for("github") == "https://mw.test/auth/github/callback"


def test_redirect_uri_for_strips_trailing_slash(monkeypatch):
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mw.test/")
    assert redirect_uri_for("github") == "https://mw.test/auth/github/callback"


# ---- PKCE (RFC 7636, S256) -----------------------------------------------


def test_generate_pkce_pair_challenge_is_s256_of_verifier():
    verifier, challenge = generate_pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    assert challenge == expected
    # No padding, no '+'/'/' -- base64URL, per RFC 7636.
    assert "=" not in challenge
    assert "+" not in challenge
    assert "/" not in challenge


def test_generate_pkce_pair_verifier_length_within_rfc_bounds():
    verifier, _ = generate_pkce_pair()
    assert 43 <= len(verifier) <= 128


def test_generate_pkce_pair_is_unique_per_call():
    v1, c1 = generate_pkce_pair()
    v2, c2 = generate_pkce_pair()
    assert v1 != v2
    assert c1 != c2


def test_generate_state_is_unique_per_call():
    assert generate_state() != generate_state()


# ---- authorize URL --------------------------------------------------------


def test_build_authorize_url_carries_every_required_param(monkeypatch):
    _enable_github(monkeypatch)
    url = build_authorize_url(GITHUB, state="the-state", code_challenge="the-challenge")

    parsed = httpx.URL(url)
    assert str(parsed).startswith(GITHUB.authorize_url)
    params = dict(httpx.QueryParams(parsed.query))
    assert params["client_id"] == "test-client-id"
    assert params["redirect_uri"] == "https://mw.test/auth/github/callback"
    assert params["scope"] == "read:user user:email"
    assert params["state"] == "the-state"
    assert params["code_challenge"] == "the-challenge"
    assert params["code_challenge_method"] == "S256"
    assert params["response_type"] == "code"
    # The client secret is NEVER put on the authorize URL -- it has no
    # business being sent to the browser at all, only in the server-side
    # token exchange below.
    assert "client_secret" not in params
    assert "test-client-secret" not in url


def test_build_authorize_url_discord_matches_registered_app_shape(monkeypatch):
    """Cross-checks the constructed authorize URL against the exact
    shape Matt registered Discord's OAuth2 app with (confirmed via a
    real authorize URL Discord itself generated for both the preview
    and prod redirect URIs): the authorize endpoint is
    https://discord.com/oauth2/authorize (NOT under /api/, unlike
    Discord's token endpoint which IS), response_type=code, and scope
    must serialize to exactly "identify email" -- no extras, no
    "openid". A mismatch on any of these (or on redirect_uri not
    matching the registered value byte for byte) is a silent
    production failure, not a test-only concern -- see
    redirect_uri_for's own docstring for why redirect_uri has to match
    exactly.
    """
    _enable_discord(monkeypatch)
    url = build_authorize_url(DISCORD, state="the-state", code_challenge="the-challenge")

    assert url.startswith("https://discord.com/oauth2/authorize?")
    assert not url.startswith("https://discord.com/api/")

    params = dict(httpx.QueryParams(httpx.URL(url).query))
    assert params["scope"] == "identify email"
    assert params["response_type"] == "code"
    assert params["redirect_uri"] == "https://mw.test/auth/discord/callback"
    assert "client_secret" not in params


def test_redirect_uri_for_discord_matches_registered_preview_and_prod_values(monkeypatch):
    """Directly reproduces the two redirect_uri values Matt registered
    Discord's OAuth2 app with (mwpreview.k7zvx.com for preview,
    meshwars.com for prod) -- proves redirect_uri_for produces exactly
    what Discord's app console expects for each deployment, since
    Discord rejects the entire flow on any mismatch (trailing slash,
    scheme, anything).
    """
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mwpreview.k7zvx.com")
    assert redirect_uri_for("discord") == "https://mwpreview.k7zvx.com/auth/discord/callback"

    monkeypatch.setattr(settings, "oauth_public_base_url", "https://meshwars.com")
    assert redirect_uri_for("discord") == "https://meshwars.com/auth/discord/callback"


def test_build_authorize_url_google_matches_oidc_shape(monkeypatch):
    """Cross-checks the constructed authorize URL against Google's own
    OAuth 2.0 / OIDC endpoint: authorize is
    https://accounts.google.com/o/oauth2/v2/auth, response_type=code,
    and scope must serialize to exactly "openid email profile" -- no
    extras. A mismatch on any of these (or on redirect_uri not matching
    the registered value byte for byte) is a silent production failure,
    not a test-only concern -- see redirect_uri_for's own docstring for
    why redirect_uri has to match exactly.
    """
    _enable_google(monkeypatch)
    url = build_authorize_url(GOOGLE, state="the-state", code_challenge="the-challenge")

    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")

    params = dict(httpx.QueryParams(httpx.URL(url).query))
    assert params["scope"] == "openid email profile"
    assert params["response_type"] == "code"
    assert params["redirect_uri"] == "https://mw.test/auth/google/callback"
    assert "client_secret" not in params


def test_redirect_uri_for_google_matches_registered_preview_and_prod_values(monkeypatch):
    """Directly reproduces the two redirect_uri values Google's OAuth
    client would be registered with (mwpreview.k7zvx.com for preview,
    meshwars.com for prod) -- same reasoning as the Discord version of
    this test above.
    """
    monkeypatch.setattr(settings, "oauth_public_base_url", "https://mwpreview.k7zvx.com")
    assert redirect_uri_for("google") == "https://mwpreview.k7zvx.com/auth/google/callback"

    monkeypatch.setattr(settings, "oauth_public_base_url", "https://meshwars.com")
    assert redirect_uri_for("google") == "https://meshwars.com/auth/google/callback"


# ---- token exchange --------------------------------------------------------


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_exchange_code_happy_path_returns_token_response(monkeypatch):
    _enable_github(monkeypatch)
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        assert request.url == GITHUB.token_url
        assert request.headers["accept"] == "application/json"
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["client_id"] == "test-client-id"
        assert body["client_secret"] == "test-client-secret"
        assert body["code"] == "the-code"
        assert body["redirect_uri"] == "https://mw.test/auth/github/callback"
        assert body["code_verifier"] == "the-verifier"
        assert body["grant_type"] == "authorization_code"
        return httpx.Response(200, json={"access_token": "gh-token-abc", "token_type": "bearer", "scope": "read:user"})

    async def go():
        async with _mock_client(handler) as client:
            return await exchange_code(GITHUB, code="the-code", code_verifier="the-verifier", http_client=client)

    token_response = _run(go())
    assert token_response["access_token"] == "gh-token-abc"
    assert len(seen_requests) == 1


def test_exchange_code_non_200_raises_oauth_error(monkeypatch):
    _enable_github(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    async def go():
        async with _mock_client(handler) as client:
            return await exchange_code(GITHUB, code="c", code_verifier="v", http_client=client)

    with pytest.raises(OAuthError):
        _run(go())


def test_exchange_code_200_with_error_body_raises_oauth_error(monkeypatch):
    """GitHub returns 200 with an error PAYLOAD (not a non-2xx status)
    for an invalid/expired/already-used code -- the HTTP status alone
    can't be trusted here.
    """
    _enable_github(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "bad_verification_code"})

    async def go():
        async with _mock_client(handler) as client:
            return await exchange_code(GITHUB, code="c", code_verifier="v", http_client=client)

    with pytest.raises(OAuthError):
        _run(go())


def test_exchange_code_non_json_body_raises_oauth_error(monkeypatch):
    _enable_github(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all")

    async def go():
        async with _mock_client(handler) as client:
            return await exchange_code(GITHUB, code="c", code_verifier="v", http_client=client)

    with pytest.raises(OAuthError):
        _run(go())


# ---- fetch_identity / Apple seam -------------------------------------------


def test_fetch_identity_missing_access_token_raises():
    async def go():
        async with _mock_client(lambda r: httpx.Response(500)) as client:
            return await fetch_identity(GITHUB, {"token_type": "bearer"}, client)

    with pytest.raises(OAuthError):
        _run(go())


def test_fetch_identity_no_userinfo_url_raises_not_implemented():
    """The Apple seam: a provider with userinfo_url=None (id_token-based
    identity) is a deliberately unimplemented branch -- see app/oauth.py's
    module docstring. Constructs a throwaway Provider rather than adding
    a real Apple entry to the table, since none exists yet.
    """
    async def _never_called(userinfo, http_client, access_token):
        raise AssertionError("extract_identity must never be reached for a None userinfo_url")

    apple_shaped = Provider(
        name="apple-shaped",
        authorize_url="https://appleid.apple.com/auth/authorize",
        token_url="https://appleid.apple.com/auth/token",
        userinfo_url=None,
        scopes=("name", "email"),
        client_id_setting="oauth_github_client_id",  # any real setting name works for this test
        client_secret_setting="oauth_github_client_secret",
        extract_identity=_never_called,
    )

    async def go():
        async with _mock_client(lambda r: httpx.Response(500)) as client:
            return await fetch_identity(apple_shaped, {"access_token": "whatever"}, client)

    with pytest.raises(OAuthError):
        _run(go())


def test_fetch_identity_userinfo_non_200_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    async def go():
        async with _mock_client(handler) as client:
            return await fetch_identity(GITHUB, {"access_token": "tok"}, client)

    with pytest.raises(OAuthError):
        _run(go())


def test_fetch_identity_end_to_end_github(monkeypatch):
    """The full fetch_identity() path for GitHub: GET /user, then GET
    /user/emails, resolved into one ProviderIdentity.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer gh-token-abc"
        if request.url == GITHUB.userinfo_url:
            return httpx.Response(200, json={"id": 4242, "login": "octocat"})
        if request.url == "https://api.github.com/user/emails":
            return httpx.Response(
                200,
                json=[
                    {"email": "old@example.com", "primary": False, "verified": True},
                    {"email": "dev@example.com", "primary": True, "verified": True},
                ],
            )
        return httpx.Response(404)

    async def go():
        async with _mock_client(handler) as client:
            return await fetch_identity(GITHUB, {"access_token": "gh-token-abc"}, client)

    identity = _run(go())
    assert identity == ProviderIdentity(subject="4242", email="dev@example.com", email_verified=True)


# ---- GitHub email selection (_github_extract_identity) --------------------
#
# Directly exercises the function fetch_identity() ultimately calls for
# GitHub, so each of these can assert the exact selection rule without
# also depending on the /user call succeeding.

def _emails_client(emails: list[dict]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=emails)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_github_email_selection_picks_primary_and_verified():
    from app.oauth import _github_extract_identity

    emails = [
        {"email": "secondary@example.com", "primary": False, "verified": True},
        {"email": "unverified-primary@example.com", "primary": True, "verified": False},
        {"email": "correct@example.com", "primary": True, "verified": True},
    ]

    async def go():
        async with _emails_client(emails) as client:
            return await _github_extract_identity({"id": 1}, client, "tok")

    identity = _run(go())
    assert identity.email == "correct@example.com"
    assert identity.email_verified is True
    assert identity.subject == "1"


def test_github_email_selection_rejects_verified_but_not_primary():
    from app.oauth import _github_extract_identity

    # Verified, but not primary -- must NOT be selected, and with no
    # other candidate, email must come back absent rather than falling
    # back to this one.
    emails = [{"email": "verified-not-primary@example.com", "primary": False, "verified": True}]

    async def go():
        async with _emails_client(emails) as client:
            return await _github_extract_identity({"id": 2}, client, "tok")

    identity = _run(go())
    assert identity.email is None
    assert identity.email_verified is False


def test_github_email_selection_rejects_primary_but_unverified():
    from app.oauth import _github_extract_identity

    # Primary, but not verified -- must NOT be selected either.
    emails = [{"email": "primary-not-verified@example.com", "primary": True, "verified": False}]

    async def go():
        async with _emails_client(emails) as client:
            return await _github_extract_identity({"id": 3}, client, "tok")

    identity = _run(go())
    assert identity.email is None
    assert identity.email_verified is False


def test_github_email_selection_no_emails_at_all():
    from app.oauth import _github_extract_identity

    async def go():
        async with _emails_client([]) as client:
            return await _github_extract_identity({"id": 4}, client, "tok")

    identity = _run(go())
    assert identity.email is None
    assert identity.email_verified is False


def test_github_emails_endpoint_non_200_raises():
    from app.oauth import _github_extract_identity

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await _github_extract_identity({"id": 5}, client, "tok")

    with pytest.raises(OAuthError):
        _run(go())


# ---- fetch_identity end-to-end / Discord (_discord_extract_identity) ------
#
# Unlike GitHub, Discord's /users/@me response carries email/verified
# directly -- no second call, so fetch_identity()'s single GET already
# exercises the whole path. These tests hit _discord_extract_identity
# directly (same reasoning as the GitHub email-selection tests above:
# asserting the exact mapping rule without also depending on an HTTP
# round trip succeeding), plus one full fetch_identity() test for the
# end-to-end path GitHub's own test above covers.


def test_fetch_identity_end_to_end_discord(monkeypatch):
    """The full fetch_identity() path for Discord: one GET to
    /users/@me, resolved directly into a ProviderIdentity -- no second
    call, unlike GitHub's /user + /user/emails.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer discord-token-abc"
        assert request.url == DISCORD.userinfo_url
        return httpx.Response(
            200,
            json={
                "id": "123456789012345678",
                "username": "mattjohnson",
                "global_name": "Matt",
                "email": "matt@example.com",
                "verified": True,
            },
        )

    async def go():
        async with _mock_client(handler) as client:
            return await fetch_identity(DISCORD, {"access_token": "discord-token-abc"}, client)

    identity = _run(go())
    assert identity == ProviderIdentity(
        subject="123456789012345678", email="matt@example.com", email_verified=True
    )


def test_discord_extract_identity_maps_representative_payload():
    """A representative Discord /users/@me payload -- id is the
    subject (never username/global_name, both user-editable -- see
    this module's Discord section comment), email/email_verified come
    straight off email/verified.
    """
    from app.oauth import _discord_extract_identity

    userinfo = {
        "id": "999888777666555444",
        "username": "octoduck",
        "discriminator": "0",
        "global_name": "Octo Duck",
        "email": "octoduck@example.com",
        "verified": True,
    }

    async def go():
        async with _mock_client(lambda r: httpx.Response(500)) as client:
            return await _discord_extract_identity(userinfo, client, "tok")

    identity = _run(go())
    assert identity == ProviderIdentity(
        subject="999888777666555444", email="octoduck@example.com", email_verified=True
    )
    # subject must be the id, never the mutable/reusable username.
    assert identity.subject != userinfo["username"]


def test_discord_extract_identity_unverified_email_not_reported_as_verified():
    """Discord's own `verified` flag, false -- must not be reported as
    a verified email even though `email` itself is present. This is
    the exact case the account-requires-password-when-email-verified
    rule depends on getting right: wrongly marking this verified would
    both weaken sign-in resolution (an unverified address auto-linking
    an account) and wrongly compel a password.
    """
    from app.oauth import _discord_extract_identity

    userinfo = {"id": "1", "username": "u", "email": "maybe@example.com", "verified": False}

    async def go():
        async with _mock_client(lambda r: httpx.Response(500)) as client:
            return await _discord_extract_identity(userinfo, client, "tok")

    identity = _run(go())
    assert identity.email is None
    assert identity.email_verified is False
    assert identity.subject == "1"


def test_discord_extract_identity_missing_email_handled_without_raising():
    """The `email` scope can be granted but Discord still omits `email`/
    `verified` from the response entirely (no email on file, or the
    scope was silently dropped by the consent screen) -- must resolve
    to email=None, email_verified=False rather than raising a KeyError
    or crashing on a missing field.
    """
    from app.oauth import _discord_extract_identity

    userinfo = {"id": "2", "username": "u"}  # no email/verified keys at all

    async def go():
        async with _mock_client(lambda r: httpx.Response(500)) as client:
            return await _discord_extract_identity(userinfo, client, "tok")

    identity = _run(go())
    assert identity.email is None
    assert identity.email_verified is False
    assert identity.subject == "2"


def test_discord_extract_identity_verified_but_no_email_value():
    """Belt-and-suspenders on the same "absent, never guessed" contract:
    verified=True with email explicitly null (Discord can return this
    shape) must still resolve to email=None, not treat verified=True
    as license to invent an address.
    """
    from app.oauth import _discord_extract_identity

    userinfo = {"id": "3", "username": "u", "email": None, "verified": True}

    async def go():
        async with _mock_client(lambda r: httpx.Response(500)) as client:
            return await _discord_extract_identity(userinfo, client, "tok")

    identity = _run(go())
    assert identity.email is None
    assert identity.email_verified is False


# ---- fetch_identity end-to-end / Google (_google_extract_identity) --------
#
# Same reasoning as the Discord block above: Google's OIDC userinfo
# response carries email/email_verified directly -- no second call, so
# fetch_identity()'s single GET already exercises the whole path.


def test_fetch_identity_end_to_end_google(monkeypatch):
    """The full fetch_identity() path for Google: one GET to
    /v1/userinfo, resolved directly into a ProviderIdentity -- no
    second call, same shape as Discord's own end-to-end test above.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer google-token-abc"
        assert request.url == GOOGLE.userinfo_url
        return httpx.Response(
            200,
            json={
                "sub": "108234567890123456789",
                "name": "Matt Johnson",
                "picture": "https://example.com/pic.jpg",
                "email": "matt@example.com",
                "email_verified": True,
            },
        )

    async def go():
        async with _mock_client(handler) as client:
            return await fetch_identity(GOOGLE, {"access_token": "google-token-abc"}, client)

    identity = _run(go())
    assert identity == ProviderIdentity(
        subject="108234567890123456789", email="matt@example.com", email_verified=True
    )


def test_google_extract_identity_maps_representative_payload():
    """A representative Google OIDC userinfo payload -- sub is the
    subject (never email, which can be reassigned -- see this module's
    Google section comment), email/email_verified come straight off
    email/email_verified.
    """
    from app.oauth import _google_extract_identity

    userinfo = {
        "sub": "108234567890123456789",
        "name": "Octo Duck",
        "given_name": "Octo",
        "family_name": "Duck",
        "picture": "https://example.com/pic.jpg",
        "email": "octoduck@example.com",
        "email_verified": True,
    }

    async def go():
        async with _mock_client(lambda r: httpx.Response(500)) as client:
            return await _google_extract_identity(userinfo, client, "tok")

    identity = _run(go())
    assert identity == ProviderIdentity(
        subject="108234567890123456789", email="octoduck@example.com", email_verified=True
    )
    # subject must be sub, never the mutable/reassignable email address.
    assert identity.subject != userinfo["email"]


def test_google_extract_identity_unverified_email_not_reported_as_verified():
    """Google's own `email_verified` claim, false -- must not be
    reported as a verified email even though `email` itself is present.
    Same "getting `verified` right" reasoning as Discord's own test
    above -- this is what the account-requires-password-when-email-
    verified rule depends on getting right.
    """
    from app.oauth import _google_extract_identity

    userinfo = {"sub": "1", "email": "maybe@example.com", "email_verified": False}

    async def go():
        async with _mock_client(lambda r: httpx.Response(500)) as client:
            return await _google_extract_identity(userinfo, client, "tok")

    identity = _run(go())
    assert identity.email is None
    assert identity.email_verified is False
    assert identity.subject == "1"


def test_google_extract_identity_missing_email_handled_without_raising():
    """The `email`/`profile` scopes can be granted but Google still
    omits `email`/`email_verified` from the response entirely -- must
    resolve to email=None, email_verified=False rather than raising a
    KeyError or crashing on a missing field.
    """
    from app.oauth import _google_extract_identity

    userinfo = {"sub": "2", "name": "u"}  # no email/email_verified keys at all

    async def go():
        async with _mock_client(lambda r: httpx.Response(500)) as client:
            return await _google_extract_identity(userinfo, client, "tok")

    identity = _run(go())
    assert identity.email is None
    assert identity.email_verified is False
    assert identity.subject == "2"


def test_google_extract_identity_verified_but_no_email_value():
    """Belt-and-suspenders on the same "absent, never guessed" contract:
    email_verified=True with email explicitly null must still resolve
    to email=None, not treat email_verified=True as license to invent
    an address.
    """
    from app.oauth import _google_extract_identity

    userinfo = {"sub": "3", "email": None, "email_verified": True}

    async def go():
        async with _mock_client(lambda r: httpx.Response(500)) as client:
            return await _google_extract_identity(userinfo, client, "tok")

    identity = _run(go())
    assert identity.email is None
    assert identity.email_verified is False
