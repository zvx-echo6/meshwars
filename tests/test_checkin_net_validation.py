"""Regression coverage for app/admin_ops.py's _validate_net_fields --
this function had ZERO test coverage before task 5 of the
observation-sources feature, and was just refactored to extract
_validate_label/_validate_kind_and_protocol/_validate_connector_url/
_validate_mqtt_fields as helpers shared with _validate_source_fields
(see tests/test_observation_sources.py for that sibling's own
coverage). These tests pin down EXISTING checkin_net behaviour so the
refactor is proven not to have changed it.

Calls _validate_net_fields directly against plain dicts -- no HTTP, no
database -- the same "test the pure validator function" style this
module's routes already delegate every rule to (see
app/admin_ops.py's admin_checkin_net_create/update, which are both
thin wrappers around this call).
"""
from __future__ import annotations

from app.admin_ops import _validate_net_fields


def _corescope_net(**overrides) -> dict:
    body = {
        "label": "Freq51 Weekly Net",
        "kind": "corescope",
        "connector_url": "https://cs.example",
        "channel": "general",
        "weekday": 2,
        "start_hour": 18,
        "end_hour": 20,
        "timezone": "America/Boise",
        "start_date": "2026-01-01",
    }
    body.update(overrides)
    return body


def _meshview_net(**overrides) -> dict:
    body = {
        "label": "Weekly Net (Meshtastic)",
        "kind": "meshview",
        "connector_url": "https://meshview.example",
        "hashtag": "#freq51",
        "weekday": 2,
        "start_hour": 18,
        "end_hour": 20,
        "timezone": "America/Boise",
        "start_date": "2026-01-01",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------
# valid nets still validate
# ---------------------------------------------------------------------

def test_valid_corescope_net_validates():
    fields, err = _validate_net_fields(_corescope_net())
    assert err is None
    assert fields["kind"] == "corescope"
    assert fields["protocol"] == "mc"
    assert fields["channel"] == "general"
    assert fields["hashtag"] == ""


def test_valid_meshview_net_validates():
    fields, err = _validate_net_fields(_meshview_net())
    assert err is None
    assert fields["kind"] == "meshview"
    assert fields["protocol"] == "mt"
    assert fields["hashtag"] == "#freq51"
    assert fields["channel"] == ""


# ---------------------------------------------------------------------
# channel/hashtag requirements per kind
# ---------------------------------------------------------------------

def test_corescope_without_channel_is_400():
    fields, err = _validate_net_fields(_corescope_net(channel=""))
    assert err is not None
    assert err.status_code == 400


def test_meshview_without_hashtag_is_400():
    fields, err = _validate_net_fields(_meshview_net(hashtag=""))
    assert err is not None
    assert err.status_code == 400


def test_meshview_forces_channel_to_empty_string():
    fields, err = _validate_net_fields(_meshview_net(channel="some-channel-that-should-be-dropped"))
    assert err is None
    assert fields["channel"] == ""


def test_corescope_forces_hashtag_to_empty_string():
    fields, err = _validate_net_fields(_corescope_net(hashtag="#should-be-dropped"))
    assert err is None
    assert fields["hashtag"] == ""


# ---------------------------------------------------------------------
# weekday / hour / timezone / start_date validation
# ---------------------------------------------------------------------

def test_weekday_out_of_range_is_400():
    fields, err = _validate_net_fields(_corescope_net(weekday=7))
    assert err is not None
    assert err.status_code == 400

    fields, err = _validate_net_fields(_corescope_net(weekday=-1))
    assert err is not None
    assert err.status_code == 400


def test_start_hour_greater_than_end_hour_is_400():
    fields, err = _validate_net_fields(_corescope_net(start_hour=20, end_hour=18))
    assert err is not None
    assert err.status_code == 400


def test_bad_timezone_is_400():
    fields, err = _validate_net_fields(_corescope_net(timezone="Not/A_Real_Zone"))
    assert err is not None
    assert err.status_code == 400


def test_bad_start_date_is_400():
    fields, err = _validate_net_fields(_corescope_net(start_date="not-a-date"))
    assert err is not None
    assert err.status_code == 400


def test_blank_start_date_is_accepted():
    """'' is meaningful (blocks awards, see checkin.py's
    net_date_for_net docstring), not an error -- must be accepted, not
    rejected.
    """
    fields, err = _validate_net_fields(_corescope_net(start_date=""))
    assert err is None
    assert fields["start_date"] == ""


# ---------------------------------------------------------------------
# switching an existing net's kind preserves stored secrets
# ---------------------------------------------------------------------

def test_switching_kind_preserves_stored_secrets():
    """Long-standing behaviour: editing an existing mqtt net's kind
    (e.g. mqtt -> mqtt_meshtastic) with a blank broker_password/
    channel_key submission must not wipe the secrets already on file --
    _validate_mqtt_fields' "blank means keep" rule reads `current`
    regardless of which kind the row is being changed TO.
    """
    current = {
        "broker_password": "s3cret-broker-pw",
        "channel_key": "AQ==",
    }
    body = {
        "label": "Public Meshtastic Broker",
        "kind": "mqtt_meshtastic",
        "connector_url": "mqtt://mqtt.meshtastic.org:1883",
        "channel": "LongFast",
        "hashtag": "#freq51",
        "topic_root": "msh/US",
        "weekday": 2, "start_hour": 18, "end_hour": 20,
        "timezone": "America/Boise", "start_date": "2026-01-01",
        # broker_password/channel_key deliberately omitted -- blank submission
    }
    fields, err = _validate_net_fields(body, current=current)
    assert err is None
    assert fields["broker_password"] == "s3cret-broker-pw"
    assert fields["channel_key"] == "AQ=="
