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
from app.checkin import (
    OFFICIAL_MESHTASTIC_MQTT_PASSWORD, OFFICIAL_MESHTASTIC_MQTT_URL, OFFICIAL_MESHTASTIC_MQTT_USERNAME,
)


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


def _mqtt_net(**overrides) -> dict:
    body = {
        "label": "Private Broker Net",
        "kind": "mqtt",
        "connector_url": "mqtt://broker.private:1883",
        "broker_username": "brokeruser",
        "broker_password": "brokerpw",
        "hashtag": "#freq51",
        "weekday": 2,
        "start_hour": 18,
        "end_hour": 20,
        "timezone": "America/Boise",
        "start_date": "2026-01-01",
    }
    body.update(overrides)
    return body


def _mqtt_meshtastic_net(**overrides) -> dict:
    body = {
        "label": "Public Meshtastic Broker",
        "kind": "mqtt_meshtastic",
        "topic_root": "msh/US",
        "channel": "LongFast",
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

def test_switching_kind_to_mqtt_preserves_stored_broker_password():
    """Long-standing behaviour, still true for plain mqtt: editing an
    existing net's kind to 'mqtt' with a blank broker_password/
    channel_key submission must not wipe the secrets already on file --
    _validate_mqtt_fields' "blank means keep" rule reads `current`
    regardless of which kind the row is being changed TO.
    """
    current = {
        "broker_password": "s3cret-broker-pw",
        "channel_key": "AQ==",
    }
    body = _mqtt_net(broker_password="", channel_key="")
    fields, err = _validate_net_fields(body, current=current)
    assert err is None
    assert fields["broker_password"] == "s3cret-broker-pw"
    assert fields["channel_key"] == "AQ=="


def test_switching_kind_to_mqtt_meshtastic_forces_broker_credentials_but_keeps_channel_key():
    """Switching an existing net's kind TO mqtt_meshtastic is different
    from switching to plain mqtt: broker_username/broker_password are
    forced to the official constants (there is nothing stored worth
    "keeping" -- see _validate_mqtt_fields), regardless of what
    `current` carries. channel_key is NOT forced -- it stays real,
    operator-supplied secret config for both mqtt kinds, so the
    existing stored value is still preserved on a blank submission,
    same as before.
    """
    current = {
        "broker_password": "s3cret-broker-pw",
        "channel_key": "AQ==",
    }
    body = _mqtt_meshtastic_net()  # broker_password/channel_key omitted -- blank submission
    fields, err = _validate_net_fields(body, current=current)
    assert err is None
    assert fields["connector_url"] == OFFICIAL_MESHTASTIC_MQTT_URL
    assert fields["broker_username"] == OFFICIAL_MESHTASTIC_MQTT_USERNAME
    assert fields["broker_password"] == OFFICIAL_MESHTASTIC_MQTT_PASSWORD
    assert fields["channel_key"] == "AQ=="


# ---------------------------------------------------------------------
# mqtt_meshtastic: connector_url/broker_username/broker_password are
# forced to the official constants, never accepted from the caller
# ---------------------------------------------------------------------

def test_mqtt_meshtastic_net_with_no_connector_or_credentials_succeeds():
    """The whole point of this feature: an operator adding an
    mqtt_meshtastic net supplies only topic_root and channel -- no
    connector_url, no broker_username, no broker_password -- and the
    net still validates, with the official values filled in.
    """
    body = _mqtt_meshtastic_net()
    assert "connector_url" not in body
    assert "broker_username" not in body
    assert "broker_password" not in body
    fields, err = _validate_net_fields(body)
    assert err is None
    assert fields["connector_url"] == OFFICIAL_MESHTASTIC_MQTT_URL
    assert fields["broker_username"] == OFFICIAL_MESHTASTIC_MQTT_USERNAME
    assert fields["broker_password"] == OFFICIAL_MESHTASTIC_MQTT_PASSWORD
    assert fields["topic_root"] == "msh/US"
    assert fields["channel"] == "LongFast"


def test_mqtt_meshtastic_net_submitted_credentials_are_overridden_not_persisted():
    """A caller submitting a DIFFERENT connector_url/broker_username/
    broker_password for this kind has them silently overridden with the
    official values -- never persisted, never even validated as a URL.
    """
    body = _mqtt_meshtastic_net(
        connector_url="mqtts://some-other-broker.example:8883",
        broker_username="not-meshdev",
        broker_password="not-large4cats",
    )
    fields, err = _validate_net_fields(body)
    assert err is None
    assert fields["connector_url"] == OFFICIAL_MESHTASTIC_MQTT_URL
    assert fields["broker_username"] == OFFICIAL_MESHTASTIC_MQTT_USERNAME
    assert fields["broker_password"] == OFFICIAL_MESHTASTIC_MQTT_PASSWORD


def test_mqtt_meshtastic_net_blank_topic_root_is_400():
    fields, err = _validate_net_fields(_mqtt_meshtastic_net(topic_root=""))
    assert err is not None
    assert err.status_code == 400


def test_mqtt_meshtastic_net_blank_channel_is_400():
    fields, err = _validate_net_fields(_mqtt_meshtastic_net(channel=""))
    assert err is not None
    assert err.status_code == 400


def test_plain_mqtt_net_keeps_submitted_connector_and_credentials():
    """Plain mqtt is unchanged: whatever the caller submits for
    connector_url/broker_username/broker_password is validated and
    persisted as-is -- no forcing to any official value.
    """
    body = _mqtt_net(
        connector_url="mqtt://broker.private:1883",
        broker_username="brokeruser",
        broker_password="brokerpw",
    )
    fields, err = _validate_net_fields(body)
    assert err is None
    assert fields["connector_url"] == "mqtt://broker.private:1883"
    assert fields["broker_username"] == "brokeruser"
    assert fields["broker_password"] == "brokerpw"


def test_plain_mqtt_net_requires_mqtt_scheme_connector_url():
    fields, err = _validate_net_fields(_mqtt_net(connector_url="https://not-a-broker.example"))
    assert err is not None
    assert err.status_code == 400
