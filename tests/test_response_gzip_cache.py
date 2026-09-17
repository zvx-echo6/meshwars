"""Tests for the gzip-caching fix shared by app/mc_api.py's
cached_json_response/_BOARD_CACHE and app/places_api.py's
cached_places_response/_PLACES_CACHE.

Before this fix, both caches stored only the PLAINTEXT serialized bytes,
so Starlette's GZipMiddleware (wired in ahead of every route, see
app/main.py) recompressed those same bytes from scratch on every single
gzip-accepting request within the cache's TTL window -- a py-spy profile
of production found 0.57s of own-time in gzip.py:_write_raw from exactly
this. The fix caches the gzip bytes ALONGSIDE the plaintext (computed at
most once per cache generation) and serves whichever representation the
request's Accept-Encoding actually supports.

Both modules got the identical treatment (_CachedBody class, same
_wants_gzip() helper, same "one shared ETag across both encodings"
choice, same reasoning) -- so most cases here are run against BOTH
modules via the `target` fixture below rather than duplicated per file.
"""
from __future__ import annotations

import gzip
import json

import pytest
from fastapi import Request

import app.mc_api as mc_api_module
import app.places_api as places_api_module


def _request(if_none_match: str | None = None, accept_gzip: bool = False) -> Request:
    """A minimal Request carrying just enough of an ASGI scope for
    cached_json_response/cached_places_response to read If-None-Match
    and Accept-Encoding off it -- same approach tests/test_places_api.py's
    own _request() helper uses (a bare Request, no running server)."""
    headers = []
    if if_none_match is not None:
        headers.append((b"if-none-match", if_none_match.encode("latin-1")))
    if accept_gzip:
        headers.append((b"accept-encoding", b"gzip, deflate, br"))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "http_version": "1.1",
        "headers": headers,
    }
    return Request(scope)


class _Target:
    """Wraps one module's (cache dict, call function, ttl-setting) triple
    so the shared test bodies below don't have to branch on which module
    they're exercising."""

    def __init__(self, module, cache_attr, call, set_ttl):
        self.module = module
        self.cache_attr = cache_attr
        self.call = call
        self.set_ttl = set_ttl

    def clear_cache(self):
        getattr(self.module, self.cache_attr).clear()


def _mc_target(monkeypatch, ttl: int) -> _Target:
    monkeypatch.setattr(mc_api_module.settings, "board_cache_seconds", ttl)
    mc_api_module._BOARD_CACHE.clear()

    def call(key, build, request=None):
        return mc_api_module.cached_json_response(key, build, request)

    return _Target(mc_api_module, "_BOARD_CACHE", call, lambda t: None)


def _places_target(monkeypatch, ttl: int) -> _Target:
    places_api_module._PLACES_CACHE.clear()

    def call(key, build, request=None):
        return places_api_module.cached_places_response(key, ttl, build, request)

    return _Target(places_api_module, "_PLACES_CACHE", call, lambda t: None)


@pytest.fixture(params=["mc_api", "places_api"])
def target_factory(request, monkeypatch):
    """Yields a function(ttl) -> _Target for whichever module this
    parametrization covers. Both modules' module-level caches are
    cleared before and after via each _*_target() helper / the autouse
    _reset_caches fixture below."""
    if request.param == "mc_api":
        return lambda ttl=60: _mc_target(monkeypatch, ttl)
    return lambda ttl=60: _places_target(monkeypatch, ttl)


@pytest.fixture(autouse=True)
def _reset_caches():
    mc_api_module._BOARD_CACHE.clear()
    places_api_module._PLACES_CACHE.clear()
    yield
    mc_api_module._BOARD_CACHE.clear()
    places_api_module._PLACES_CACHE.clear()


def _counting_build(payload: dict):
    calls = {"n": 0}

    def build():
        calls["n"] += 1
        return payload

    return build, calls


PAYLOAD = {"hello": "world", "n": [1, 2, 3], "name": "café"}


def test_cache_hit_does_not_recompress(target_factory, monkeypatch):
    """Two gzip-accepting requests for the same key: gzip.compress must
    run exactly once (on the cache-populating request), not twice."""
    target = target_factory(60)
    build, calls = _counting_build(PAYLOAD)

    compress_calls = {"n": 0}
    real_compress = gzip.compress

    def counting_compress(data, *a, **kw):
        compress_calls["n"] += 1
        return real_compress(data, *a, **kw)

    monkeypatch.setattr(target.module.gzip, "compress", counting_compress)

    r1 = target.call("k", build, _request(accept_gzip=True))
    r2 = target.call("k", build, _request(accept_gzip=True))

    assert r1.headers["content-encoding"] == "gzip"
    assert r2.headers["content-encoding"] == "gzip"
    assert compress_calls["n"] == 1
    assert calls["n"] == 1  # build() itself also only ran once, i.e. this really was a cache hit


def test_request_without_gzip_gets_plaintext(target_factory):
    target = target_factory(60)
    build, _ = _counting_build(PAYLOAD)

    resp = target.call("k", build, _request(accept_gzip=False))

    assert "content-encoding" not in resp.headers
    assert json.loads(resp.body) == PAYLOAD


def test_request_with_gzip_gets_content_encoding_vary_and_decompresses_correctly(target_factory):
    target = target_factory(60)
    build, _ = _counting_build(PAYLOAD)

    resp = target.call("k", build, _request(accept_gzip=True))

    assert resp.headers["content-encoding"] == "gzip"
    assert resp.headers["vary"] == "Accept-Encoding"
    assert json.loads(gzip.decompress(resp.body)) == PAYLOAD


def test_gzip_and_plaintext_variants_carry_the_same_etag(target_factory):
    """This module's documented ETag choice: ONE etag covers both
    representations (see cached_json_response/cached_places_response's
    own docstrings for the reasoning) -- not a distinct etag per
    encoding."""
    target = target_factory(60)
    build, _ = _counting_build(PAYLOAD)

    plain = target.call("k", build, _request(accept_gzip=False))
    gz = target.call("k", build, _request(accept_gzip=True))

    assert plain.headers["etag"] == gz.headers["etag"]


def test_if_none_match_yields_304_with_no_body_without_gzip(target_factory):
    target = target_factory(60)
    build, _ = _counting_build(PAYLOAD)

    first = target.call("k", build, _request(accept_gzip=False))
    etag = first.headers["etag"]

    second = target.call("k", build, _request(if_none_match=etag, accept_gzip=False))
    assert second.status_code == 304
    assert second.body == b""


def test_if_none_match_yields_304_with_no_body_with_gzip(target_factory):
    target = target_factory(60)
    build, _ = _counting_build(PAYLOAD)

    first = target.call("k", build, _request(accept_gzip=True))
    etag = first.headers["etag"]

    second = target.call("k", build, _request(if_none_match=etag, accept_gzip=True))
    assert second.status_code == 304
    assert second.body == b""
    # A 304 to a gzip-accepting client should still say the response
    # varies by Accept-Encoding, same as the 200 it is validating.
    assert second.headers.get("vary") == "Accept-Encoding"


def test_ttl_zero_bypasses_cache_entirely(target_factory, monkeypatch):
    target = target_factory(0)
    build, calls = _counting_build(PAYLOAD)

    target.call("k", build, _request(accept_gzip=True))
    target.call("k", build, _request(accept_gzip=True))

    assert calls["n"] == 2  # rebuilt every time -- never cached
    assert len(getattr(target.module, target.cache_attr)) == 0
