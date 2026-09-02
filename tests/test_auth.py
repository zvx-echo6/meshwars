"""Tests for app/auth.py -- the shared API-key authentication
dependency that replaced five hand-duplicated copies of the same
pattern (app/join_api.py, app/nodes_api.py, app/checkin_api.py's own
_authenticate()s, and inline code in app/api.py's POST /api/mc/ingest
and app/mc_api.py's POST /api/mc/status).

Two layers here, same split as tests/test_client_ip.py uses for the
helper this module builds on:

- Unit tests against require_api_key_principal() directly, using bare
  fastapi.Request objects built from a hand-rolled ASGI scope (no
  TestClient/HTTP, no event loop beyond asyncio.run) and a FakeIngestor
  standing in for request.app.state.mc_ingestor. This is the only way
  to exercise every one of the four AuthResult statuses plus the two
  rate-limit tiers in isolation, and to prove the pre-auth limiter
  really does run before the key is ever read (FakeIngestor.calls stays
  empty).

- A handful of TestClient/HTTP tests, wiring app/nodes_api.py's,
  app/join_api.py's, and app/mc_api.py's REAL routers into a bare
  FastAPI app (same "FastAPI-around-one-router" spirit as
  tests/test_tiles_api.py), to prove the Depends(...) wiring on the
  actual endpoints produces the same status codes and {"error": ...}
  JSON bodies these routes always returned -- behavior preservation is
  the entire point of this refactor. app/checkin_api.py and app/api.py
  cannot be imported in this environment at all (see the two module-
  level skip conditions below) so their routes aren't exercised here;
  their behavior is covered indirectly, since they call the exact same
  require_api_key_principal() this file already tests thoroughly, just
  configured differently -- see app/auth.py's own module docstring for
  each site's configuration.

Deliberately NOT covered here: any request that reaches a router's
database-backed success path (200 OK on GET /api/nodes, POST /api/team,
etc.). starlette's TestClient runs the ASGI app in a different OS
thread than the test itself (confirmed empirically while writing this
file), and the tests/conftest.py `conn` fixture is a sqlite3 connection
opened with the default check_same_thread=True -- sharing it across
that thread boundary would raise, not skip, so it's not attempted. The
"ok" AuthResult status is still fully covered below, just at the unit
level (require_api_key_principal() called directly, no HTTP), which
proves the same thing (a Principal comes back, nothing raises) without
needing a database at all.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.auth import (
    Principal,
    http_exception_as_error_body,
    new_rate_limit_bucket,
    require_api_key_principal,
)
from app.config import settings
from app.mc_ingest import AuthResult

GOOD_KEY = "good-key"
DISABLED_KEY = "disabled-key"
REVOKED_KEY = "revoked-key"
PLAYER_ID = 42


class FakeIngestor:
    """Stands in for request.app.state.mc_ingestor. Records every raw
    key it was asked to authenticate, in order -- tests use this to
    prove the pre-auth rate limiter really does run BEFORE the key is
    ever read (a rejected-by-rate-limit request must never appear
    here).
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def authenticate(self, raw_key: str) -> AuthResult:
        self.calls.append(raw_key)
        if raw_key == GOOD_KEY:
            return AuthResult("ok", PLAYER_ID)
        if raw_key == DISABLED_KEY:
            return AuthResult("disabled", PLAYER_ID)
        if raw_key == REVOKED_KEY:
            return AuthResult("revoked", PLAYER_ID)
        return AuthResult("not_found")


class _FakeApp:
    """Just enough of a FastAPI app for Request.app.state.mc_ingestor
    to resolve -- request.app is `scope["app"]` and nothing else, so a
    bare namespace-with-.state is sufficient without ever constructing
    a real FastAPI() instance.
    """

    class _State:
        pass

    def __init__(self, ingestor: FakeIngestor) -> None:
        self.state = self._State()
        self.state.mc_ingestor = ingestor


def _request(
    ingestor: FakeIngestor,
    *,
    peer: str = "198.51.100.10",
    api_key: str | None = None,
) -> Request:
    headers = []
    if api_key is not None:
        headers.append((b"x-api-key", api_key.encode("latin-1")))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "http_version": "1.1",
        "client": (peer, 51234),
        "headers": headers,
        "app": _FakeApp(ingestor),
    }
    return Request(scope)


def _run(coro):
    return asyncio.run(coro)


# ---- unit tests: the four AuthResult statuses --------------------------

def test_ok_status_returns_a_principal_with_default_source():
    ingestor = FakeIngestor()
    dep = require_api_key_principal()
    principal = _run(dep(_request(ingestor, api_key=GOOD_KEY)))
    assert principal == Principal(player_id=PLAYER_ID, source="api_key")


def test_not_found_key_is_401_unauthorized():
    ingestor = FakeIngestor()
    dep = require_api_key_principal()
    with pytest.raises(HTTPException) as exc_info:
        _run(dep(_request(ingestor, api_key="never-issued")))
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "unauthorized"


def test_revoked_key_is_401_unauthorized_same_as_not_found():
    """not_found and revoked must be indistinguishable from the
    response alone -- see app/auth.py's docstring for why.
    """
    ingestor = FakeIngestor()
    dep = require_api_key_principal()
    with pytest.raises(HTTPException) as exc_info:
        _run(dep(_request(ingestor, api_key=REVOKED_KEY)))
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "unauthorized"


def test_disabled_key_is_403_forbidden():
    ingestor = FakeIngestor()
    dep = require_api_key_principal()
    with pytest.raises(HTTPException) as exc_info:
        _run(dep(_request(ingestor, api_key=DISABLED_KEY)))
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "forbidden"


def test_missing_header_is_401_unauthorized_without_calling_the_ingestor():
    ingestor = FakeIngestor()
    dep = require_api_key_principal()
    with pytest.raises(HTTPException) as exc_info:
        _run(dep(_request(ingestor, api_key=None)))
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "unauthorized"
    assert ingestor.calls == []


def test_empty_header_is_401_unauthorized_without_calling_the_ingestor():
    """An X-API-Key header present but empty is treated exactly like a
    missing one -- `request.headers.get("X-API-Key", "")` can't tell
    the two apart, and neither could any of the five original copies.
    """
    ingestor = FakeIngestor()
    dep = require_api_key_principal()
    with pytest.raises(HTTPException) as exc_info:
        _run(dep(_request(ingestor, api_key="")))
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "unauthorized"
    assert ingestor.calls == []


# ---- unit tests: rate limiting -----------------------------------------

def test_pre_auth_rate_limit_returns_429_before_reading_the_key(monkeypatch):
    # The dependency always consults settings.mc_status_rate_limit_*
    # for the pre-auth tier (see app/auth.py's require_api_key_principal
    # docstring) -- pinned to a budget of 1 so the second call below is
    # guaranteed to be over it.
    monkeypatch.setattr(settings, "mc_status_rate_limit_attempts", 1)
    monkeypatch.setattr(settings, "mc_status_rate_limit_window_seconds", 60)

    ingestor = FakeIngestor()
    bucket = new_rate_limit_bucket()
    dep = require_api_key_principal(pre_auth_limiter=bucket)

    # First request from this address spends the budget of 1.
    principal = _run(dep(_request(ingestor, peer="203.0.113.9", api_key=GOOD_KEY)))
    assert principal.player_id == PLAYER_ID
    assert ingestor.calls == [GOOD_KEY]

    with pytest.raises(HTTPException) as exc_info:
        _run(dep(_request(ingestor, peer="203.0.113.9", api_key=GOOD_KEY)))
    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == "rate limited"
    # The address limiter rejected this before the header was ever
    # read again -- see app/nodes_api.py's module docstring for why
    # that ordering matters (an unauthenticated flood must never reach
    # McIngestor.authenticate at all). ingestor.calls is unchanged from
    # the one successful call above.
    assert ingestor.calls == [GOOD_KEY]

    # A second, distinct address is unaffected -- proves this is a
    # per-address budget, not a global one.
    principal = _run(dep(_request(ingestor, peer="203.0.113.99", api_key=GOOD_KEY)))
    assert principal.player_id == PLAYER_ID


def test_pre_auth_rate_limit_uses_the_configured_detail_message(monkeypatch):
    """app/mc_api.py's POST /api/mc/status has always used a different
    429 message ("too many attempts, try again later") than the other
    four key-authenticated endpoints ("rate limited") -- this must stay
    configurable per call site, not unified.
    """
    monkeypatch.setattr(settings, "mc_status_rate_limit_attempts", 1)
    monkeypatch.setattr(settings, "mc_status_rate_limit_window_seconds", 60)

    ingestor = FakeIngestor()
    bucket = new_rate_limit_bucket()
    dep = require_api_key_principal(
        pre_auth_limiter=bucket,
        pre_auth_rate_limit_detail="too many attempts, try again later",
    )
    _run(dep(_request(ingestor, peer="203.0.113.9", api_key=GOOD_KEY)))  # spends the budget of 1

    with pytest.raises(HTTPException) as exc_info:
        _run(dep(_request(ingestor, peer="203.0.113.9", api_key=GOOD_KEY)))
    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == "too many attempts, try again later"


def test_post_auth_rate_limit_triggers_only_after_a_successful_key(monkeypatch):
    monkeypatch.setattr(settings, "node_api_rate_limit_attempts", 1)
    monkeypatch.setattr(settings, "node_api_rate_limit_window_seconds", 60)

    ingestor = FakeIngestor()
    bucket = new_rate_limit_bucket()
    dep = require_api_key_principal(post_auth_limiter=bucket)

    # A bad key never reaches the post-auth limiter at all -- it's
    # rejected first, on the auth status, and must not consume any
    # budget from the good key below.
    with pytest.raises(HTTPException) as exc_info:
        _run(dep(_request(ingestor, api_key="wrong")))
    assert exc_info.value.status_code == 401

    # The good key's first use spends its budget of 1.
    principal = _run(dep(_request(ingestor, api_key=GOOD_KEY)))
    assert principal.player_id == PLAYER_ID

    # Same key again, still authenticates fine, but is now over its
    # own post-auth budget -- 429, not the (successful) auth status.
    with pytest.raises(HTTPException) as exc_info:
        _run(dep(_request(ingestor, api_key=GOOD_KEY)))
    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == "rate limited"


def test_no_pre_auth_limiter_means_no_address_rate_limiting():
    """app/api.py's POST /api/mc/ingest configuration: pre_auth_limiter
    left at its default of None. Flooding one address must never 429
    here -- that endpoint's only rate limiting is
    McIngestor.rate_limit_ok(), entirely separate from this dependency.
    """
    ingestor = FakeIngestor()
    dep = require_api_key_principal()  # both limiters default to None
    for _ in range(50):
        principal = _run(dep(_request(ingestor, peer="203.0.113.9", api_key=GOOD_KEY)))
        assert principal.player_id == PLAYER_ID


def test_no_post_auth_limiter_means_no_per_key_rate_limiting():
    """app/mc_api.py's POST /api/mc/status configuration: it has a
    pre-auth address limiter but, unlike the other four endpoints,
    never had a post-auth per-key limiter -- flooding one valid key
    from many different addresses must never 429 here.
    """
    ingestor = FakeIngestor()
    dep = require_api_key_principal(pre_auth_limiter=new_rate_limit_bucket())
    for i in range(50):
        principal = _run(dep(_request(ingestor, peer=f"203.0.113.{i % 200 + 1}", api_key=GOOD_KEY)))
        assert principal.player_id == PLAYER_ID


def test_rate_limit_buckets_are_independent_instances():
    """new_rate_limit_bucket() must hand back a fresh, unshared budget
    every time -- app/auth.py's module docstring is explicit that each
    call site keeps its own instance(s) rather than a pool shared
    across modules, since that sharing was never how the original five
    copies of this behaved.
    """
    a = new_rate_limit_bucket()
    b = new_rate_limit_bucket()
    assert a.limited("same-address", limit=1, window=60) is False
    # Bucket b has never seen this address -- it must not be affected
    # by bucket a's own state.
    assert b.limited("same-address", limit=1, window=60) is False


# ---- HTTP-level tests: the real routers, via TestClient -----------------
#
# app/checkin_api.py can't be imported in this environment (a pre-
# existing, unrelated gap: `aiolimiter` is not installed here, and
# app/checkin.py imports it unconditionally) and app/api.py can't
# either (sse_starlette missing, the same gap tests/test_tiles_api.py
# and tests/test_ingest_integrity_gates.py already work around) -- so
# neither module's router is exercised here. Both configure this exact
# same dependency (see app/auth.py's module docstring), already proven
# correct above at the unit level.

import app.join_api as join_api_module  # noqa: E402
import app.mc_api as mc_api_module  # noqa: E402
import app.nodes_api as nodes_api_module  # noqa: E402


def _client_for(router, ingestor: FakeIngestor) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    # Mirrors app/main.py's own registration -- without it, FastAPI's
    # default HTTPException handler would render {"detail": ...}
    # instead of the {"error": ...} shape these routes have always
    # returned. See app/auth.py's http_exception_as_error_body
    # docstring.
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    app.state.mc_ingestor = ingestor
    return TestClient(app)


def _reset(limiter) -> None:
    """Clear a module-level rate-limit bucket's accumulated state
    between tests.

    Each of require_principal/require_team_principal/
    require_status_principal was built ONCE, at import time, by
    calling require_api_key_principal(pre_auth_limiter=<this exact
    object>, ...) -- the returned dependency closure holds a direct
    reference to that _BoundedHits instance, not a live lookup of the
    module attribute by name. Reassigning the module attribute (e.g.
    `monkeypatch.setattr(nodes_api_module, "_addr_rate_limiter", ...)`)
    would therefore change what the NAME points at without touching
    what the already-built dependency actually consults -- the same
    reason Depends(require_principal) on each route also can't be
    swapped this way, since FastAPI captured that exact callable at
    route-definition time too. Reaching in and clearing the real
    object's state (rather than replacing the object) is the only way
    to give each test a clean budget on these particular singletons.
    """
    limiter._hits.clear()


@pytest.mark.parametrize(
    "router_module,path,method,limiter_attr",
    [
        (nodes_api_module, "/api/nodes", "GET", "_addr_rate_limiter"),
        (join_api_module, "/api/team", "GET", "_team_addr_rate_limiter"),
        (mc_api_module, "/api/mc/status", "POST", "_status_addr_rate_limiter"),
    ],
)
def test_missing_key_is_401_over_http(router_module, path, method, limiter_attr):
    _reset(getattr(router_module, limiter_attr))
    client = _client_for(router_module.router, FakeIngestor())
    resp = client.request(method, path)
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}


@pytest.mark.parametrize(
    "router_module,path,method,limiter_attr",
    [
        (nodes_api_module, "/api/nodes", "GET", "_addr_rate_limiter"),
        (join_api_module, "/api/team", "GET", "_team_addr_rate_limiter"),
        (mc_api_module, "/api/mc/status", "POST", "_status_addr_rate_limiter"),
    ],
)
def test_disabled_key_is_403_over_http(router_module, path, method, limiter_attr):
    _reset(getattr(router_module, limiter_attr))
    client = _client_for(router_module.router, FakeIngestor())
    resp = client.request(method, path, headers={"X-API-Key": DISABLED_KEY})
    assert resp.status_code == 403
    assert resp.json() == {"error": "forbidden"}


def test_nodes_api_pre_auth_rate_limit_over_http(monkeypatch):
    _reset(nodes_api_module._addr_rate_limiter)
    monkeypatch.setattr(settings, "mc_status_rate_limit_attempts", 1)
    monkeypatch.setattr(settings, "mc_status_rate_limit_window_seconds", 60)
    ingestor = FakeIngestor()
    client = _client_for(nodes_api_module.router, ingestor)

    r1 = client.get("/api/nodes", headers={"X-API-Key": "whatever"})
    assert r1.status_code == 401  # budget of 1 consumed, key still gets looked up
    assert ingestor.calls == ["whatever"]

    r2 = client.get("/api/nodes", headers={"X-API-Key": "whatever"})
    assert r2.status_code == 429
    assert r2.json() == {"error": "rate limited"}
    # Rejected by the address limiter before the key was read again.
    assert ingestor.calls == ["whatever"]


def test_mc_status_pre_auth_rate_limit_uses_its_own_message_over_http(monkeypatch):
    """The one endpoint whose 429 body has always read differently
    from the other four -- see app/mc_api.py's require_status_principal
    wiring.
    """
    _reset(mc_api_module._status_addr_rate_limiter)
    monkeypatch.setattr(settings, "mc_status_rate_limit_attempts", 1)
    monkeypatch.setattr(settings, "mc_status_rate_limit_window_seconds", 60)
    ingestor = FakeIngestor()
    client = _client_for(mc_api_module.router, ingestor)

    r1 = client.post("/api/mc/status", headers={"X-API-Key": "whatever"})
    assert r1.status_code == 401

    r2 = client.post("/api/mc/status", headers={"X-API-Key": "whatever"})
    assert r2.status_code == 429
    assert r2.json() == {"error": "too many attempts, try again later"}


def test_join_api_team_switch_rate_limit_is_independent_of_nodes_api(monkeypatch):
    """app/join_api.py's team-switch address budget must not be shared
    with app/nodes_api.py's own copy of this same dependency -- see
    app/auth.py's module docstring for why merging them would be an
    observable behavior change. Exhausting nodes_api's budget for an
    address must leave join_api's budget for that same address intact.
    """
    _reset(nodes_api_module._addr_rate_limiter)
    _reset(join_api_module._team_addr_rate_limiter)
    monkeypatch.setattr(settings, "mc_status_rate_limit_attempts", 1)
    monkeypatch.setattr(settings, "mc_status_rate_limit_window_seconds", 60)

    ingestor = FakeIngestor()
    nodes_client = _client_for(nodes_api_module.router, ingestor)
    join_client = _client_for(join_api_module.router, ingestor)

    # Drive nodes_api's address budget (limit 1) to empty for this peer.
    # TestClient reports a fixed synthetic peer ("testclient", 50000)
    # for every request regardless of which TestClient instance sends
    # it, so nodes_client and join_client below are guaranteed to be
    # rate-limited (or not) as the SAME address.
    nodes_client.get("/api/nodes", headers={"X-API-Key": "whatever"})
    r_nodes = nodes_client.get("/api/nodes", headers={"X-API-Key": "whatever"})
    assert r_nodes.status_code == 429

    # join_api's own bucket for the SAME client peer address is
    # untouched -- still 401 (bad key), never 429.
    r_join = join_client.get("/api/team", headers={"X-API-Key": "whatever"})
    assert r_join.status_code == 401


# ---- regression: the app-wide handler must not touch router-level 404/405 -
#
# app/main.py registers http_exception_as_error_body on
# fastapi.exceptions.HTTPException (imported as `from fastapi import
# HTTPException`), keyed to that exact class, with the comment "nothing
# raised HTTPException before this change, so it has zero effect on any
# untouched route." That claim was checked empirically (not just by
# reading fastapi/starlette source) because it looked suspicious:
# Starlette's own router raises HTTPException(404) for an unmatched
# path and HTTPException(405) for a method mismatch, and StaticFiles
# raises HTTPException(404) for a missing file -- all three look like
# exactly the exception type this handler is registered for.
#
# They are not, in the one way that matters here: all three raise
# `starlette.exceptions.HTTPException` (the base class), while this
# handler is registered on `fastapi.exceptions.HTTPException` (a
# *subclass* of it -- confirmed via issubclass() during investigation).
# Starlette's exception middleware looks up a handler by walking the
# raised exception INSTANCE's own class upward through its MRO; a
# base-class instance's MRO never includes a child class, so a handler
# keyed to the subclass never matches a base-class instance. Only code
# that raises `fastapi.HTTPException` specifically (app/auth.py's
# require_api_key_principal(), the only such call site in this app) is
# ever caught by it. This test locks that in with a real TestClient
# request, so a future change (e.g. registering the handler on
# starlette.exceptions.HTTPException instead, which WOULD catch these)
# can't silently flip every 404/405 on the site from {"detail": ...} to
# {"error": ...} without a test noticing.
def test_router_404_and_405_keep_fastapi_default_detail_shape():
    app = FastAPI()

    @app.get("/only-get")
    async def only_get():
        return {"ok": True}

    # Same registration app/main.py performs.
    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    client = TestClient(app, raise_server_exceptions=False)

    r404 = client.get("/does-not-exist")
    assert r404.status_code == 404
    assert r404.json() == {"detail": "Not Found"}

    r405 = client.post("/only-get")
    assert r405.status_code == 405
    assert r405.json() == {"detail": "Method Not Allowed"}


def test_handler_still_translates_a_directly_raised_http_exception():
    """Contrast case for the test above: an HTTPException actually
    raised by application code (the shape app/auth.py raises) IS caught
    by the registered handler and rendered as {"error": ...}, same as
    every hand-rolled JSONResponse in this app already does.
    """
    app = FastAPI()

    @app.get("/raises")
    async def raises():
        raise HTTPException(status_code=401, detail="unauthorized")

    app.add_exception_handler(HTTPException, http_exception_as_error_body)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/raises")
    assert resp.status_code == 401
    assert resp.json() == {"error": "unauthorized"}
