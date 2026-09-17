"""Test for app/checkin.py's _checkin_http_limits(): CoreScopeClient and
BeaconClient both build their httpx.AsyncClient with this helper, which
sets keepalive_expiry above settings.checkin_poll_interval_seconds.

httpx's own default keepalive_expiry is 5 seconds. CheckinPoller only
calls back into a given client once per checkin_poll_interval_seconds
(30s by default), so with the default keepalive_expiry every pooled
connection is already dead by the next poll and every poll pays a
fresh TCP+TLS handshake (confirmed via py-spy in production: 0.24s in
ssl.py's do_handshake). These are pure configuration assertions -- no
real network access, and no real AsyncClient is ever used to make a
request; httpx.AsyncClient's constructor is monkeypatched purely to
capture the `limits=` kwarg it was called with.
"""
from __future__ import annotations

import httpx
import pytest

import app.checkin as checkin_module
from app.checkin import CoreScopeClient, BeaconClient, _checkin_http_limits
from app.config import settings


def test_checkin_http_limits_keepalive_exceeds_poll_interval():
    limits = _checkin_http_limits()
    assert isinstance(limits, httpx.Limits)
    assert limits.keepalive_expiry is not None
    assert limits.keepalive_expiry > settings.checkin_poll_interval_seconds


def test_checkin_http_limits_derives_from_configured_poll_interval(monkeypatch):
    """Not a hardcoded constant -- changing the configured poll interval
    changes the resulting keepalive_expiry, and it always stays above
    the (possibly-changed) interval."""
    monkeypatch.setattr(settings, "checkin_poll_interval_seconds", 90)
    limits = _checkin_http_limits()
    assert limits.keepalive_expiry > 90


@pytest.fixture
def captured_client_kwargs(monkeypatch):
    """Monkeypatch httpx.AsyncClient (as imported into app.checkin) to
    record its constructor kwargs instead of opening a real client."""
    calls: list[dict] = []
    real_async_client = httpx.AsyncClient

    class _RecordingAsyncClient:
        def __init__(self, *args, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(checkin_module.httpx, "AsyncClient", _RecordingAsyncClient)
    yield calls
    assert checkin_module.httpx.AsyncClient is _RecordingAsyncClient  # patched throughout
    _ = real_async_client  # unused beyond documenting what was replaced


def test_corescope_client_passes_keepalive_limits_above_poll_interval(captured_client_kwargs):
    CoreScopeClient(base_url="https://example.invalid")
    assert len(captured_client_kwargs) == 1
    limits = captured_client_kwargs[0]["limits"]
    assert isinstance(limits, httpx.Limits)
    assert limits.keepalive_expiry > settings.checkin_poll_interval_seconds


def test_beacon_client_passes_keepalive_limits_above_poll_interval(captured_client_kwargs):
    BeaconClient(base_url="https://example.invalid")
    assert len(captured_client_kwargs) == 1
    limits = captured_client_kwargs[0]["limits"]
    assert isinstance(limits, httpx.Limits)
    assert limits.keepalive_expiry > settings.checkin_poll_interval_seconds


def test_corescope_and_beacon_preserve_existing_timeout_and_headers(captured_client_kwargs):
    """The keepalive fix must not drop the timeout/headers config that
    was already there."""
    CoreScopeClient(base_url="https://example.invalid")
    BeaconClient(base_url="https://example.invalid")
    for kwargs in captured_client_kwargs:
        assert kwargs["timeout"] == httpx.Timeout(15.0, connect=5.0)
        assert kwargs["headers"]["User-Agent"] == "meshwars/1.0"
        assert kwargs["headers"]["Accept"] == "application/json"
