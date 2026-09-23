"""Tests for app/mqtt_subscriber.py -- this module had ZERO test
coverage before task 5 of the observation-sources feature. Covers:

  - _expand_channel_key: the Meshtastic PSK-shorthand expansion
    (Channels::getKey() mirror -- see that function's own docstring for
    the firmware source this is verified against).
  - topic_filters_for_row / topic_filter_union: the subscribe-filter
    construction that feeds _BrokerConnection.fingerprint/_on_connect,
    including the regression this task exists to catch -- two rows
    sharing ONE connector_url with different (topic_root, channel) must
    produce a union carrying BOTH rows' filters, or every row past the
    first is silently starved of messages (see topic_filter_union's own
    docstring for the incident this guards).
  - _BrokerConnection.fingerprint: proving a second row (a second
    region/channel added to an already-connected broker) changes the
    fingerprint, which is what forces MqttSubscriber._reconcile_once to
    actually resubscribe rather than leaving the new row's messages
    unheard forever.

No real network access anywhere in this file: _BrokerConnection's
constructor only builds a local paho-mqtt Client object and never calls
.connect()/.loop_start(), so constructing one is safe in a unit test
(the same reasoning tests/test_mc_directory_cache.py's own
"never .aclose()'d client is fine" comment applies to a client that is
simply never connected).
"""
from __future__ import annotations

import base64

from app.mqtt_subscriber import (
    _DEFAULT_PSK,
    _BrokerConnection,
    _expand_channel_key,
    topic_filter_union,
    topic_filters_for_row,
)


# ---------------------------------------------------------------------
# _expand_channel_key
# ---------------------------------------------------------------------

def test_expand_channel_key_blank_is_the_default_longfast_psk():
    assert _expand_channel_key("") == _DEFAULT_PSK


def test_expand_channel_key_single_byte_index_0_means_encryption_off():
    raw_b64 = base64.b64encode(bytes([0])).decode()
    assert _expand_channel_key(raw_b64) is None


def test_expand_channel_key_single_byte_index_1_is_unmodified_default_psk():
    # Index 1 is the common "AQ==" shorthand -- reproduces the default
    # PSK completely unmodified (index - 1 == 0).
    raw_b64 = base64.b64encode(bytes([1])).decode()
    assert _expand_channel_key(raw_b64) == _DEFAULT_PSK
    assert base64.b64encode(bytes([1])).decode() == "AQ=="


def test_expand_channel_key_single_byte_index_n_increments_last_byte():
    index = 5
    raw_b64 = base64.b64encode(bytes([index])).decode()
    expected = bytearray(_DEFAULT_PSK)
    expected[-1] = (expected[-1] + index - 1) & 0xFF
    assert _expand_channel_key(raw_b64) == bytes(expected)


def test_expand_channel_key_single_byte_index_wraps_last_byte_mod_256():
    # Pick an index that forces the last byte to wrap past 0xFF, proving
    # the (& 0xFF) truncation, not just a plain addition.
    index = 0xFF
    raw_b64 = base64.b64encode(bytes([index])).decode()
    expected = bytearray(_DEFAULT_PSK)
    expected[-1] = (expected[-1] + index - 1) & 0xFF
    got = _expand_channel_key(raw_b64)
    assert got == bytes(expected)
    assert got[-1] <= 0xFF


def test_expand_channel_key_literal_16_byte_key_passes_through():
    raw = bytes(range(16))
    raw_b64 = base64.b64encode(raw).decode()
    assert _expand_channel_key(raw_b64) == raw


def test_expand_channel_key_literal_32_byte_key_passes_through():
    raw = bytes(range(32))
    raw_b64 = base64.b64encode(raw).decode()
    assert _expand_channel_key(raw_b64) == raw


def test_expand_channel_key_bad_base64_returns_none():
    assert _expand_channel_key("not valid base64!!") is None


def test_expand_channel_key_wrong_decoded_length_returns_none():
    # 2 bytes, 8 bytes, 24 bytes -- none of the three meaningful lengths
    # (1, 16, 32).
    for n in (2, 8, 24):
        raw_b64 = base64.b64encode(bytes(range(n))).decode()
        assert _expand_channel_key(raw_b64) is None, f"length {n} should be rejected"


# ---------------------------------------------------------------------
# topic_filters_for_row
# ---------------------------------------------------------------------

def test_topic_filters_for_row_root_and_channel_gives_both_e_and_json_forms():
    row = {"topic_root": "msh/US", "channel": "LongFast"}
    assert topic_filters_for_row(row) == [
        "msh/US/2/e/LongFast/#",
        "msh/US/2/json/LongFast/#",
    ]


def test_topic_filters_for_row_root_only_is_broad_region_subscription():
    row = {"topic_root": "msh/EU_868", "channel": ""}
    assert topic_filters_for_row(row) == ["msh/EU_868/#"]


def test_topic_filters_for_row_neither_subscribes_to_everything():
    row = {"topic_root": "", "channel": ""}
    assert topic_filters_for_row(row) == ["#"]


def test_topic_filters_for_row_normalizes_trailing_slash_on_topic_root():
    row = {"topic_root": "msh/US/", "channel": "LongFast"}
    assert topic_filters_for_row(row) == [
        "msh/US/2/e/LongFast/#",
        "msh/US/2/json/LongFast/#",
    ]

    row_no_channel = {"topic_root": "msh/EU_868/", "channel": ""}
    assert topic_filters_for_row(row_no_channel) == ["msh/EU_868/#"]


# ---------------------------------------------------------------------
# topic_filter_union
# ---------------------------------------------------------------------

def test_topic_filter_union_dedupes_identical_rows():
    row = {"topic_root": "msh/US", "channel": "LongFast"}
    union = topic_filter_union([row, dict(row), dict(row)])
    assert union == ("msh/US/2/e/LongFast/#", "msh/US/2/json/LongFast/#")


def test_topic_filter_union_is_a_stable_sorted_order_regardless_of_input_order():
    row_a = {"topic_root": "msh/US", "channel": "LongFast"}
    row_b = {"topic_root": "msh/EU_868", "channel": ""}
    row_c = {"topic_root": "", "channel": ""}

    union_1 = topic_filter_union([row_a, row_b, row_c])
    union_2 = topic_filter_union([row_c, row_b, row_a])
    assert union_1 == union_2
    assert union_1 == tuple(sorted(union_1))


def test_topic_filter_union_two_rows_sharing_one_connector_carries_both_filters():
    """The regression that matters most: two rows sharing ONE
    connector_url with different (topic_root, channel) must produce a
    union containing BOTH rows' filters -- this is the bug that
    silently starved every row past the first (see
    topic_filter_union's own docstring in app/mqtt_subscriber.py).
    """
    row_us = {
        "connector_url": "mqtt://mqtt.meshtastic.org:1883",
        "topic_root": "msh/US", "channel": "LongFast",
    }
    row_eu = {
        "connector_url": "mqtt://mqtt.meshtastic.org:1883",
        "topic_root": "msh/EU_868", "channel": "MediumSlow",
    }

    union = topic_filter_union([row_us, row_eu])

    for f in topic_filters_for_row(row_us):
        assert f in union, f"missing US filter {f!r} -- row_us was starved"
    for f in topic_filters_for_row(row_eu):
        assert f in union, f"missing EU filter {f!r} -- row_eu was starved"
    assert len(union) == 4


# ---------------------------------------------------------------------
# _BrokerConnection.fingerprint
# ---------------------------------------------------------------------

def _net_row(*, id=1, connector_url="mqtt://broker.test:1883", broker_username="",
             broker_password="", topic_root="", channel="", channel_key="",
             source_table="checkin_net"):
    return {
        "id": id, "connector_url": connector_url, "broker_username": broker_username,
        "broker_password": broker_password, "topic_root": topic_root, "channel": channel,
        "channel_key": channel_key, "source_table": source_table,
    }


def test_fingerprint_differs_between_one_row_and_two_row_case():
    """Adding a second region/channel on an already-connected broker
    must change fingerprint -- that's what forces
    MqttSubscriber._reconcile_once to tear down and resubscribe (see
    that method's own comment and _BrokerConnection.fingerprint's
    docstring for exactly why a fingerprint built from only the primary
    row would silently never notice the new row).
    """
    connector_url = "mqtt://mqtt.meshtastic.org:1883"
    row_us = _net_row(id=1, connector_url=connector_url, topic_root="msh/US", channel="LongFast")
    row_eu = _net_row(id=2, connector_url=connector_url, topic_root="msh/EU_868", channel="MediumSlow")

    one_row_bc = _BrokerConnection(connector_url, [row_us])
    two_row_bc = _BrokerConnection(connector_url, [row_us, row_eu])

    assert one_row_bc.fingerprint != two_row_bc.fingerprint


def test_fingerprint_unchanged_when_only_channel_key_differs():
    """channel_key is deliberately NOT part of fingerprint -- it only
    affects decryption of already-flowing messages (re-read live from
    self._nets on every message), so a channel_key-only edit must never
    force a reconnect. Pinned here as the counterpart to the "adding a
    row changes it" test above.
    """
    connector_url = "mqtt://broker.test:1883"
    row_a = _net_row(id=1, connector_url=connector_url, channel_key="")
    row_b = _net_row(id=1, connector_url=connector_url, channel_key=base64.b64encode(bytes(range(16))).decode())

    bc_a = _BrokerConnection(connector_url, [row_a])
    bc_b = _BrokerConnection(connector_url, [row_b])

    assert bc_a.fingerprint == bc_b.fingerprint
