"""Unit tests for app/client_family.py's client_family_from_user_agent()
-- the coarse "is this the genuine MeshCore ingest client" label
app/mc_ingest.py's record_ingest_identity() stores in
app/db.py's mc_ingest_request_log.client_family INSTEAD of the raw
User-Agent header. See that table's own SCHEMA comment for why the raw
header is never stored at all.
"""
from __future__ import annotations

from app.client_family import (
    FAMILY_FREQMAPPER,
    FAMILY_MESHMAPPER_DART,
    FAMILY_UNRECOGNIZED_OTHER,
    FAMILY_UNRECOGNIZED_PYTHON,
    client_family_from_user_agent,
)


def test_dart_user_agent_maps_to_meshmapper_dart():
    assert client_family_from_user_agent("Dart/3.4 (dart:io)") == FAMILY_MESHMAPPER_DART


def test_dart_match_is_case_insensitive():
    assert client_family_from_user_agent("dart/3.4 (DART:IO)") == FAMILY_MESHMAPPER_DART


def test_freqmapper_user_agent_maps_to_freqmapper():
    assert client_family_from_user_agent("FreqMapper/1.0") == FAMILY_FREQMAPPER


def test_python_requests_user_agent_maps_to_unrecognized_python():
    assert client_family_from_user_agent("python-requests/2.31.0") == FAMILY_UNRECOGNIZED_PYTHON


def test_bare_python_token_maps_to_unrecognized_python():
    assert client_family_from_user_agent("Python-urllib/3.11") == FAMILY_UNRECOGNIZED_PYTHON


def test_ordinary_browser_user_agent_maps_to_unrecognized_other():
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    assert client_family_from_user_agent(ua) == FAMILY_UNRECOGNIZED_OTHER


def test_missing_user_agent_maps_to_unrecognized_other():
    assert client_family_from_user_agent(None) == FAMILY_UNRECOGNIZED_OTHER


def test_empty_user_agent_maps_to_unrecognized_other():
    assert client_family_from_user_agent("") == FAMILY_UNRECOGNIZED_OTHER


def test_oversized_input_does_not_raise():
    """User-Agent is attacker-controlled input from a public endpoint --
    a pathologically large header must not raise or hang, and must
    still resolve to a definite label."""
    huge = "A" * 1_000_000
    assert client_family_from_user_agent(huge) == FAMILY_UNRECOGNIZED_OTHER


def test_control_characters_are_stripped_before_matching():
    """A UA carrying embedded control characters around a real token
    must still match -- control chars are attacker-controlled noise,
    not a reason to fail an otherwise-recognizable client."""
    ua = "Dart\x00/3.4\x1b (dart:io)"
    assert client_family_from_user_agent(ua) == FAMILY_MESHMAPPER_DART


def test_output_never_echoes_the_raw_input():
    """Every possible return value is one of the four fixed FAMILY_*
    constants -- never a slice of the input string. This is what makes
    it safe to store: the return value can never itself become a
    fingerprint."""
    for ua in ("Dart/3.4", "FreqMapper/2.0", "python-requests/2.0", "SomeUnknownClient/1.0"):
        result = client_family_from_user_agent(ua)
        assert result in (
            FAMILY_MESHMAPPER_DART, FAMILY_FREQMAPPER,
            FAMILY_UNRECOGNIZED_PYTHON, FAMILY_UNRECOGNIZED_OTHER,
        )
