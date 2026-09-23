"""Tests for app/checkin.py's _prune_seen_messages and the retention
invariant it depends on: settings.checkin_seen_retention_hours must
stay strictly greater than settings.mqtt_buffer_retention_hours (see
_prune_seen_messages' own docstring). If that ever inverted, a
checkin_seen_message row could be pruned while its mqtt_message_buffer
row still existed -- CheckinPoller's read-first dedupe (_seen) would
then find no seen row, treat the still-buffered message as never
settled, and re-process (and, for a still-in-window registered sender,
re-award) it.

Real file-backed database, same reasoning as every other db-touching
test in this repo -- app/db.py's connect() (which _prune_seen_messages
and app/mqtt_subscriber.py's _prune_buffer both use internally, taking
no conn argument) opens a fresh connection per call, so ":memory:"
would not persist what this file seeds between the setup step and the
prune call.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from app.checkin import _prune_seen_messages
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.mqtt_subscriber import _prune_buffer

NOW = int(time.time())
CONNECTOR = "mqtt://broker.test:1883"


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


def _seed_seen(path: str, packet_id: str, seen_at: int, connector: str = CONNECTOR) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO checkin_seen_message(connector, packet_id, seen_at) VALUES (?, ?, ?)",
        (connector, packet_id, seen_at),
    )
    conn.commit()
    conn.close()


def _seed_buffer(path: str, packet_id: str, received_at: int, connector: str = CONNECTOR) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO mqtt_message_buffer"
        "(connector, packet_id, from_node, channel_name, text, ts, received_at) "
        "VALUES (?, ?, 42, '', 'hello', ?, ?)",
        (connector, packet_id, received_at, received_at),
    )
    conn.commit()
    conn.close()


def _seen_packet_ids(path: str) -> set[str]:
    conn = sqlite3.connect(path)
    ids = {r[0] for r in conn.execute("SELECT packet_id FROM checkin_seen_message").fetchall()}
    conn.close()
    return ids


def _buffer_packet_ids(path: str) -> set[str]:
    conn = sqlite3.connect(path)
    ids = {r[0] for r in conn.execute("SELECT packet_id FROM mqtt_message_buffer").fetchall()}
    conn.close()
    return ids


# ---------------------------------------------------------------------
# _prune_seen_messages: deletes old, keeps new
# ---------------------------------------------------------------------

def test_prune_seen_messages_deletes_old_keeps_new(db_path):
    cutoff_seconds = settings.checkin_seen_retention_hours * 3600
    old_seen_at = NOW - cutoff_seconds - 3600  # well past the cutoff
    new_seen_at = NOW - 60  # one minute ago -- well within retention

    _seed_seen(db_path, "old-packet", old_seen_at)
    _seed_seen(db_path, "new-packet", new_seen_at)

    removed = _prune_seen_messages()

    assert removed == 1
    remaining = _seen_packet_ids(db_path)
    assert remaining == {"new-packet"}


def test_prune_seen_messages_removes_nothing_when_all_rows_are_recent(db_path):
    _seed_seen(db_path, "recent-a", NOW - 10)
    _seed_seen(db_path, "recent-b", NOW - 20)

    removed = _prune_seen_messages()

    assert removed == 0
    assert _seen_packet_ids(db_path) == {"recent-a", "recent-b"}


# ---------------------------------------------------------------------
# the retention invariant itself
# ---------------------------------------------------------------------

def test_checkin_seen_retention_exceeds_mqtt_buffer_retention():
    """settings.checkin_seen_retention_hours MUST stay strictly greater
    than settings.mqtt_buffer_retention_hours -- see
    app/checkin.py's _prune_seen_messages docstring for exactly what
    goes wrong if this ever inverts. Pinned as its own test so a future
    edit to either default trips a clear, direct assertion instead of
    only showing up as a confusing re-award bug much later.
    """
    assert settings.checkin_seen_retention_hours > settings.mqtt_buffer_retention_hours


# ---------------------------------------------------------------------
# the invariant in practice: a still-buffered message's seen row survives
# ---------------------------------------------------------------------

def test_buffer_row_still_present_means_its_seen_row_is_never_pruned(db_path):
    """A message whose mqtt_message_buffer row is still on file (i.e.
    within settings.mqtt_buffer_retention_hours) must have its
    checkin_seen_message row survive _prune_seen_messages too -- proving
    the retention-ordering invariant actually holds in practice, not
    just as a bare number comparison. Timestamps chosen just inside the
    mqtt buffer's own retention window (so app/mqtt_subscriber.py's own
    housekeeping would not have pruned the buffer row either), which
    must therefore also be inside the checkin_seen_message window given
    checkin_seen_retention_hours > mqtt_buffer_retention_hours.
    """
    # Margin generous enough (10 minutes) to stay inside the window even
    # if this test runs a while after NOW was captured at module import
    # (a full suite run can take minutes) -- a tight one-minute margin
    # flaked here for exactly that reason.
    now = int(time.time())
    received_at = now - (settings.mqtt_buffer_retention_hours * 3600) + 600  # just inside mqtt's own window
    _seed_buffer(db_path, "still-live", received_at)
    _seed_seen(db_path, "still-live", received_at)

    buffer_removed = _prune_buffer()
    seen_removed = _prune_seen_messages()

    assert buffer_removed == 0
    assert seen_removed == 0
    assert "still-live" in _buffer_packet_ids(db_path)
    assert "still-live" in _seen_packet_ids(db_path)


def test_buffer_row_expired_but_seen_row_can_outlive_it_within_its_own_window(db_path):
    """The other half of the invariant: once a buffer row ages out (past
    mqtt_buffer_retention_hours) its seen row is still allowed to
    persist a while longer, up to checkin_seen_retention_hours -- that
    is the whole point of the gap between the two settings (dedupe must
    outlive the buffer, see _prune_seen_messages' docstring), not a bug.
    """
    # Past the mqtt buffer's own retention, but still inside the (longer)
    # checkin_seen retention window. Uses a freshly captured `now`, not
    # the module-level NOW, for the same reason the sibling test above
    # does -- a full suite run can take minutes, and this timestamp must
    # stay correctly ordered relative to time.time() AT PRUNE TIME.
    now = int(time.time())
    received_at = now - (settings.mqtt_buffer_retention_hours * 3600) - 3600
    assert received_at > now - (settings.checkin_seen_retention_hours * 3600)

    _seed_buffer(db_path, "aged-out", received_at)
    _seed_seen(db_path, "aged-out", received_at)

    buffer_removed = _prune_buffer()
    seen_removed = _prune_seen_messages()

    assert buffer_removed == 1
    assert seen_removed == 0
    assert "aged-out" not in _buffer_packet_ids(db_path)
    assert "aged-out" in _seen_packet_ids(db_path)
