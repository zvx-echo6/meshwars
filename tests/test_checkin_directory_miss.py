"""Tests for CheckinPoller's direct lookup of a registered player's bound
MeshCore node when it is missing from the capped cached directory.

Upstream /api/nodes caps at directory_limit entries, so a player's bound
node can be absent from the directory even though it exists upstream. The
poller then looks the sender up directly (the same single-node client
method the radio-confirmation flow uses), at most once per sender per
directory refresh interval, and resolves through the unchanged bridge so
the same ambiguity refusals apply.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import checkin
from app.checkin import CheckinPoller
from app.meshview_client import MeshviewClient

CONNECTOR = "http://example.test"
NOW = int(time.time())
KEY_A = "02f17f41" + "a" * 56
KEY_B = "02f17f41" + "b" * 56  # same 8-hex prefix as KEY_A
KEY_C = "c0ffee00" + "c" * 56
CONFIG = {
    "points": 25.0, "streak_bonus": 5.0, "streak_bonus_max": 25.0,
    "directory_limit": 2000, "directory_refresh_seconds": 900,
}


class _SharedConn:
    """The test connection with close() disabled, so code that opens and
    closes its own connection keeps using the shared in-memory db."""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _FakeClient:
    def __init__(self, nodes_by_name, fail=False, nodes_by_key=None):
        self.nodes_by_name = nodes_by_name
        self.fail = fail
        self.search_calls: list[str] = []
        self.node_calls: list[str] = []
        self.nodes_by_key = nodes_by_key or {}

    async def fetch_node(self, public_key):
        self.node_calls.append(public_key)
        if self.fail:
            raise RuntimeError("upstream down")
        return self.nodes_by_key.get(public_key)

    async def fetch_directory_search(self, name, limit):
        self.search_calls.append(name)
        if self.fail:
            raise RuntimeError("upstream down")
        return list(self.nodes_by_name.get(name, []))


@pytest.fixture
def env(conn, monkeypatch):
    shared = _SharedConn(conn)

    @contextlib.asynccontextmanager
    async def _write_session():
        yield shared

    monkeypatch.setattr(checkin, "WriteSession", _write_session)
    monkeypatch.setattr(checkin, "connect", lambda: shared)

    weekday = datetime.now(ZoneInfo("America/Boise")).weekday()
    cur = conn.execute(
        "INSERT INTO checkin_net(label, protocol, kind, connector_url, channel, hashtag, "
        "weekday, start_hour, end_hour, timezone, start_date, enabled, created_at) "
        "VALUES ('Net','mc','corescope',?,'general','',?,0,23,'America/Boise','2000-01-01',1,?)",
        (CONNECTOR, weekday, NOW),
    )
    net = conn.execute("SELECT * FROM checkin_net WHERE id = ?", (cur.lastrowid,)).fetchone()
    poller = CheckinPoller(MeshviewClient(base_url="https://example.invalid"))
    return conn, poller, net


def _player(conn, node_ref, public_key=None) -> int:
    pid = conn.execute(
        "INSERT INTO player(display_name, team, created_at) VALUES ('P','RED',?)", (NOW,)
    ).lastrowid
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at, public_key) "
        "VALUES ('mc',?,?,?,?)",
        (node_ref, pid, NOW, public_key),
    )
    return pid


def _msg(packet_id, name="Sender One"):
    return {"packet_id": packet_id, "sender_name": name, "ts": int(time.time())}


def _poll(poller, net, messages, directory=None):
    poller._mc_directory[CONNECTOR] = directory if directory is not None else []
    asyncio.run(poller._poll_mc_feed("corescope", CONNECTOR, "general", [net], [CONNECTOR], 1, int(time.time()), CONFIG))


def _awards(conn):
    return conn.execute("SELECT player_id FROM mc_checkin_award").fetchall()


def _install(poller, client, messages):
    poller._mc_clients[CONNECTOR] = client

    async def _fetch(channel, refresh):
        return messages

    client.fetch_messages = _fetch


def test_missing_node_found_by_direct_lookup_is_awarded(env):
    conn, poller, net = env
    pid = _player(conn, KEY_A[:8])
    client = _FakeClient({"Sender One": [{"name": "Sender One", "public_key": KEY_A}]})
    msgs = [_msg("m1")]
    _install(poller, client, msgs)
    _poll(poller, net, msgs)
    assert [r["player_id"] for r in _awards(conn)] == [pid]
    assert client.search_calls == ["Sender One"]


def test_missing_and_not_found_is_not_awarded_and_warns_once(env, caplog):
    conn, poller, net = env
    pid = _player(conn, KEY_A[:8])
    # History ties the sender name to the missing contact, so the miss is
    # attributable to a player in the warning.
    conn.execute(
        "INSERT INTO checkin_node_name(connector, node_ref, player_id, name, first_seen) "
        "VALUES (?,?,?,?,?)", (CONNECTOR, KEY_A[:8], pid, "Sender One", NOW),
    )
    client = _FakeClient({})
    msgs = [_msg("m1")]
    _install(poller, client, msgs)
    with caplog.at_level(logging.WARNING, logger="checkin"):
        for _ in range(5):
            _poll(poller, net, msgs)
    assert _awards(conn) == []
    warnings = [r for r in caplog.records if "absent from the cached directory" in r.getMessage()]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert f"net {net['id']}" in text and f"player {pid}" in text
    assert KEY_A[:8] in text and "did not find it" in text
    # Still retried, not settled.
    assert conn.execute("SELECT COUNT(*) FROM checkin_seen_message").fetchone()[0] == 0


def test_found_case_warns_once_across_polls(env, caplog):
    conn, poller, net = env
    pid = _player(conn, KEY_A[:8])
    client = _FakeClient({"Sender One": [{"name": "Sender One", "public_key": KEY_A}]})
    msgs = [_msg("m1"), _msg("m2")]
    _install(poller, client, msgs)
    with caplog.at_level(logging.WARNING, logger="checkin"):
        _poll(poller, net, msgs)
        _poll(poller, net, msgs)
    warnings = [r for r in caplog.records if "absent from the cached directory" in r.getMessage()]
    assert len(warnings) == 1
    assert "found it" in warnings[0].getMessage() and f"player {pid}" in warnings[0].getMessage()


def test_direct_lookup_at_most_once_per_refresh_interval(env, monkeypatch):
    conn, poller, net = env
    _player(conn, KEY_A[:8])
    client = _FakeClient({})  # negative result
    msgs = [_msg("m1")]
    _install(poller, client, msgs)
    for _ in range(20):
        _poll(poller, net, msgs)
    assert len(client.search_calls) == 1
    # After the interval has passed, one more lookup is allowed.
    real = time.monotonic()
    monkeypatch.setattr(checkin.time, "monotonic", lambda: real + 901)
    _poll(poller, net, msgs)
    _poll(poller, net, msgs)
    assert len(client.search_calls) == 2


def test_failed_lookup_is_also_rate_limited(env):
    conn, poller, net = env
    _player(conn, KEY_A[:8])
    client = _FakeClient({}, fail=True)
    msgs = [_msg("m1")]
    _install(poller, client, msgs)
    for _ in range(5):
        _poll(poller, net, msgs)
    assert len(client.search_calls) == 1
    assert _awards(conn) == []


def test_no_lookup_when_no_bound_node_is_missing(env):
    conn, poller, net = env
    pid = _player(conn, KEY_C[:8])
    client = _FakeClient({"Sender One": [{"name": "Sender One", "public_key": KEY_C}]})
    msgs = [_msg("m1", "Other Name")]
    _install(poller, client, msgs)
    _poll(poller, net, msgs, directory=[{"name": "Some Name", "public_key": KEY_C}])
    assert client.search_calls == []
    assert pid and _awards(conn) == []


def test_name_shared_by_another_key_still_refuses(env):
    conn, poller, net = env
    _player(conn, KEY_A[:8])
    client = _FakeClient({"Sender One": [
        {"name": "Sender One", "public_key": KEY_A},
        {"name": "Sender One", "public_key": KEY_C},
    ]})
    msgs = [_msg("m1")]
    _install(poller, client, msgs)
    _poll(poller, net, msgs)
    assert _awards(conn) == []


def test_prefix_shared_by_two_keys_still_refuses(env):
    conn, poller, net = env
    _player(conn, KEY_A[:8])
    client = _FakeClient({"Sender One": [
        {"name": "Sender One", "public_key": KEY_A},
        {"name": "Sender One", "public_key": KEY_B},
    ]})
    msgs = [_msg("m1")]
    _install(poller, client, msgs)
    _poll(poller, net, msgs)
    assert _awards(conn) == []


def test_lookup_requires_exact_name_match(env):
    conn, poller, net = env
    _player(conn, KEY_A[:8])
    client = _FakeClient({"Sender One": [{"name": "Sender One Two", "public_key": KEY_A}]})
    msgs = [_msg("m1")]
    _install(poller, client, msgs)
    _poll(poller, net, msgs)
    assert _awards(conn) == []


# ---- bound node with a stored full public key: lookup by key ---------------

def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if "absent from the cached directory" in r.getMessage()]


def test_keyed_node_found_by_key_is_awarded_without_name_search(env, caplog):
    conn, poller, net = env
    pid = _player(conn, KEY_A[:8], KEY_A)
    client = _FakeClient({}, nodes_by_key={KEY_A: {"name": "Sender One", "public_key": KEY_A.upper()}})
    msgs = [_msg("m1")]
    _install(poller, client, msgs)
    with caplog.at_level(logging.WARNING, logger="checkin"):
        _poll(poller, net, msgs)
        _poll(poller, net, msgs)
    assert [r["player_id"] for r in _awards(conn)] == [pid]
    assert client.node_calls == [KEY_A]
    assert client.search_calls == []
    w = _warnings(caplog)
    assert len(w) == 1 and f"player {pid}" in w[0] and "found it" in w[0]


def test_keyed_node_not_found_warns_once_and_lookup_is_rate_limited(env, caplog, monkeypatch):
    conn, poller, net = env
    pid = _player(conn, KEY_A[:8], KEY_A)
    client = _FakeClient({})
    msgs = [_msg("m1")]
    _install(poller, client, msgs)
    with caplog.at_level(logging.WARNING, logger="checkin"):
        for _ in range(20):
            _poll(poller, net, msgs)
    assert _awards(conn) == []
    assert len(client.node_calls) == 1
    w = _warnings(caplog)
    assert len(w) == 1 and f"player {pid}" in w[0] and "did not find it" in w[0]
    real = time.monotonic()
    monkeypatch.setattr(checkin.time, "monotonic", lambda: real + 901)
    _poll(poller, net, msgs)
    assert len(client.node_calls) == 2


def test_keyed_lookup_still_refuses_shared_name(env):
    conn, poller, net = env
    _player(conn, KEY_A[:8], KEY_A)
    client = _FakeClient({}, nodes_by_key={KEY_A: {"name": "Sender One", "public_key": KEY_A}})
    msgs = [_msg("m1")]
    _install(poller, client, msgs)
    _poll(poller, net, msgs, directory=[{"name": "Sender One", "public_key": KEY_C}])
    assert _awards(conn) == []


def test_keyed_lookup_ignores_node_with_a_different_key(env):
    conn, poller, net = env
    _player(conn, KEY_A[:8], KEY_A)
    client = _FakeClient({}, nodes_by_key={KEY_A: {"name": "Sender One", "public_key": KEY_B}})
    msgs = [_msg("m1")]
    _install(poller, client, msgs)
    _poll(poller, net, msgs)
    assert _awards(conn) == []
