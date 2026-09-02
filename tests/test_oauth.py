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
    GITHUB,
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


# ---- provider table / enable-disable ------------------------------------


def test_get_provider_returns_github():
    assert get_provider("github") is GITHUB


def test_get_provider_unknown_name_returns_none():
    assert get_provider("not-a-real-provider") is None


def test_provider_disabled_by_default(monkeypatch):
    # conftest.py never sets any oauth_* setting -- a fresh clone/test
    # run must start with every provider off.
    assert provider_enabled(GITHUB) is False


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
