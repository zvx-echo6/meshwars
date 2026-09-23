"""Tests for app/checkin.py's _distinct_connectors -- specifically the
enabled=1 filter added to its observation_source half only.

Before this fix, _distinct_connectors UNIONed checkin_net and
observation_source with no `enabled` filter on either side, which
matched checkin_net's own long-standing confirm-scan behaviour (a net's
own schedule never gated confirmation/discovery) but made an
observation_source's admin "enabled" toggle a lie: switching a source
off left it scanned and merged into the directory anyway. See
_distinct_connectors' own updated docstring in app/checkin.py for why
the two tables deliberately disagree on this now.

Uses tests/conftest.py's `conn` fixture (in-memory db, real SCHEMA +
MIGRATIONS) -- these are plain conn-in, conn-out calls against
_distinct_connectors itself, no HTTP surface under test. Modeled on
tests/test_checkin_confirm.py's own test_connector_present_in_both_tables_is_scanned_once_not_twice,
which already calls _distinct_connectors directly the same way.
"""
from __future__ import annotations

import time

from app import checkin as checkin_module

NOW = int(time.time())


def _checkin_net(conn, *, connector_url, kind="mqtt", protocol="mt", enabled=1) -> None:
    conn.execute(
        "INSERT INTO checkin_net(label, protocol, kind, connector_url, channel, hashtag, "
        "weekday, start_hour, end_hour, timezone, start_date, enabled, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("Test Net", protocol, kind, connector_url, "", "#test", 2, 18, 20,
         "America/Boise", "2026-01-01", enabled, NOW),
    )


def _observation_source(conn, *, connector_url, kind="mqtt", protocol="mt", enabled=1) -> None:
    conn.execute(
        "INSERT INTO observation_source(label, protocol, kind, connector_url, channel, enabled, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("Test Source", protocol, kind, connector_url, "", enabled, NOW),
    )


def test_disabled_observation_source_is_not_returned(conn):
    """A disabled observation_source row must NOT come back from
    _distinct_connectors -- the admin "enabled" toggle is the only
    switch a source has (see app/db.py's observation_source comment:
    there is no scoring window to disable-by-proxy the way a
    checkin_net row has), so ignoring it here would make that toggle a
    lie.
    """
    _observation_source(conn, connector_url="http://disabled.example", enabled=0)

    rows = checkin_module._distinct_connectors(conn, (checkin_module.KIND_MQTT,))

    assert rows == []


def test_enabled_observation_source_is_still_returned(conn):
    """Sanity counterpart -- an enabled=1 observation_source row is
    unaffected by the new filter and still comes back exactly as
    before.
    """
    _observation_source(conn, connector_url="http://enabled.example", enabled=1)

    rows = checkin_module._distinct_connectors(conn, (checkin_module.KIND_MQTT,))

    assert rows == [{"kind": "mqtt", "connector_url": "http://enabled.example"}]


def test_disabled_checkin_net_is_still_returned_unchanged_behavior(conn):
    """checkin_net's half is DELIBERATELY left alone -- a disabled net
    still has to be visible to confirmation/discovery, which run
    regardless of a net's own schedule (see this function's own
    docstring). This must keep working exactly as it always has.
    """
    _checkin_net(conn, connector_url="http://disabled-net.example", enabled=0)

    rows = checkin_module._distinct_connectors(conn, (checkin_module.KIND_MQTT,))

    assert rows == [{"kind": "mqtt", "connector_url": "http://disabled-net.example"}]


def test_disabled_source_and_disabled_net_together_only_net_returned(conn):
    """Combined sanity check exercising both halves of the UNION at
    once, on two different connectors, to prove the asymmetry holds
    when both tables have rows in the same query.
    """
    _checkin_net(conn, connector_url="http://disabled-net.example", enabled=0)
    _observation_source(conn, connector_url="http://disabled-source.example", enabled=0)

    rows = checkin_module._distinct_connectors(conn, (checkin_module.KIND_MQTT,))

    assert rows == [{"kind": "mqtt", "connector_url": "http://disabled-net.example"}]
