"""The point of the whole observation-sources feature: an ENABLED
observation_source can NEVER award a check-in. See app/db.py's
observation_source comment ("Anything that reads this table for scoring
purposes is a bug, not a missing column") and app/checkin.py's
_poll_once docstring, which reads checkin_net ONLY to build `nets` (the
scoring path) and only widens to observation_source for
directory/connector DISCOVERY (mc_connectors) -- never for `nets`
itself.

Same real, file-backed database + CheckinPoller shape
tests/test_mc_directory_cache.py already uses to exercise
CheckinPoller methods end to end (app/db.py's connect() opens a fresh
connection per call, so ":memory:" would not share data between what
this test seeds and what _poll_once reads back).
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from app.checkin import CheckinPoller
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.meshview_client import MeshviewClient

NOW = int(time.time())
MQTT_CONNECTOR_URL = "mqtt://broker.test:1883"


def _init_schema(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                continue
            raise
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "game.db")
    _init_schema(path)
    monkeypatch.setattr(settings, "db_path", path)
    return path


def _make_mqtt_source(path: str, connector_url: str = MQTT_CONNECTOR_URL, kind: str = "mqtt") -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO observation_source"
        "(label, protocol, kind, connector_url, channel, topic_root, enabled, created_at) "
        "VALUES (?, 'mt', ?, ?, '', 'msh/US', 1, ?)",
        ("Test Observation Source", kind, connector_url, NOW),
    )
    conn.commit()
    conn.close()


def _make_player(path: str) -> int:
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO player(display_name, team, created_at) VALUES (?, ?, ?)",
        ("Test Player", "RED", NOW),
    )
    player_id = cur.lastrowid
    conn.commit()
    conn.close()
    return player_id


def _seed_buffered_message(path: str, connector_url: str, from_node: int, text: str, packet_id: str = "99") -> None:
    """A message already sitting in mqtt_message_buffer, carrying the
    hashtag a checkin_net would ordinarily match on -- present so that
    IF the scoring path somehow saw this observation_source's connector
    (the bug this test exists to catch), it would have something
    scoreable to act on. It never does, by construction -- see this
    file's own docstring.
    """
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO mqtt_message_buffer"
        "(connector, packet_id, from_node, channel_name, text, ts, received_at) "
        "VALUES (?, ?, ?, '', ?, ?, ?)",
        (connector_url, packet_id, from_node, text, NOW, NOW),
    )
    conn.commit()
    conn.close()


def _award_count(path: str) -> int:
    conn = sqlite3.connect(path)
    n = conn.execute("SELECT COUNT(*) FROM mc_checkin_award").fetchone()[0]
    conn.close()
    return n


def test_observation_source_only_never_awards_a_checkin(db_path, monkeypatch):
    """With only observation sources configured -- no enabled
    checkin_net rows at all -- a full CheckinPoller._poll_once() cycle
    must never write an mc_checkin_award row, even though a message
    matching a plausible check-in hashtag is sitting right there in
    mqtt_message_buffer for this exact connector.
    """
    # checkin must be enabled for _poll_once to do any work at all.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE checkin_config SET enabled = 1, updated_at = ? WHERE id = 1", (NOW,)
    )
    conn.commit()
    conn.close()

    _make_player(db_path)
    _make_mqtt_source(db_path)
    _seed_buffered_message(db_path, MQTT_CONNECTOR_URL, 0xA1B2C3D4, "checking in #freq51")

    assert _award_count(db_path) == 0

    poller = CheckinPoller(MeshviewClient(base_url="https://example.invalid"))
    asyncio.run(poller._poll_once())

    assert _award_count(db_path) == 0, (
        "an observation_source-only deployment must never award a check-in -- "
        "the scoring path (checkin_net-only `nets`) must never have seen this connector"
    )


def test_observation_source_alongside_a_real_net_still_isolates_scoring(db_path):
    """Stronger version: a real, enabled checkin_net exists (so the
    scoring path genuinely runs this cycle) for a DIFFERENT connector,
    while an observation_source on ITS OWN connector carries a
    plausible check-in message. Only the checkin_net's own connector may
    ever be scored; the observation_source's buffered message must
    still never turn into an award.
    """
    net_connector = "mqtt://other-broker.test:1883"
    source_connector = MQTT_CONNECTOR_URL

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE checkin_config SET enabled = 1, updated_at = ? WHERE id = 1", (NOW,)
    )
    conn.execute(
        "INSERT INTO checkin_net"
        "(label, protocol, kind, connector_url, channel, hashtag, weekday, start_hour, "
        " end_hour, timezone, start_date, enabled, created_at) "
        "VALUES (?, 'mt', 'mqtt', ?, '', '#freq51', 2, 0, 23, 'America/Boise', '2000-01-01', 1, ?)",
        ("Real Net", net_connector, NOW),
    )
    conn.commit()
    conn.close()

    _make_mqtt_source(db_path, connector_url=source_connector)
    # A message on the OBSERVATION SOURCE's connector only -- never
    # buffered against the real net's connector, so the real net has
    # nothing to award for this cycle either; this isolates the proof to
    # "the observation_source's own message was never scored."
    _seed_buffered_message(db_path, source_connector, 0xA1B2C3D4, "checking in #freq51")

    poller = CheckinPoller(MeshviewClient(base_url="https://example.invalid"))
    asyncio.run(poller._poll_once())

    assert _award_count(db_path) == 0
