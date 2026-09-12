"""Tests for pings_unknown_type (app/mc_ingest.py's is_unknown_ping_type()),
the counter added so a MeshCore ping whose `type` is present but not one
of the four parse_repeaters() recognizes (TX/RX/DISC/TRACE) -- e.g. a
future MeshMapper build's "DEFER" -- can be told apart from a ping that
legitimately heard no repeaters. Before this counter existed, both cases
landed in pings_no_repeaters with no way to distinguish them.

Same shape as tests/test_place_scoring_ingest_hook.py: drives the real
ingest path (McIngestor._process_batch_sync() -> _process_one_ping())
rather than app.mc_ingest.is_unknown_ping_type()/parse_repeaters() in
isolation, so these prove the counter is actually wired into ingest, not
just correct on its own -- plus a couple of direct unit checks of
is_unknown_ping_type() itself for the missing/None-vs-present distinction.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.mc_ingest import McIngestor, is_unknown_ping_type, parse_repeaters

NOW = int(time.time())

# Well within the default play area (settings.play_area_*).
LAT, LON = 43.0, -116.0

PROTOCOL = "mc"


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Same fixture as tests/test_place_scoring_ingest_hook.py -- a fresh
    on-disk sqlite file with the real schema (including the
    pings_unknown_type migration), settings.db_path pointed at it.
    """
    path = tmp_path / "game.db"
    monkeypatch.setattr(settings, "db_path", str(path))
    conn = sqlite3.connect(str(path))
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
    return str(path)


def _seed_player(db_path, player_id=1, team="RED"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?, ?, ?, ?)",
        (player_id, f"player-{player_id}", team, NOW),
    )
    conn.commit()
    conn.close()


def _ping(ping_type, lat=LAT, lon=LON, ts=NOW, contact="deadbeef", **extra):
    ping = {
        "type": ping_type,
        "contact": contact,
        "lat": lat,
        "lon": lon,
        "timestamp": ts,
    }
    ping.update(extra)
    return ping


def _ingest_stat(db_path, player_id=1):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM player_ingest_stat WHERE player_id = ? AND protocol = ?",
        (player_id, PROTOCOL),
    )]
    conn.close()
    totals = {}
    for r in rows:
        for k, v in r.items():
            if isinstance(v, int) and k not in ("player_id", "day"):
                totals[k] = totals.get(k, 0) + v
    return totals


def _position_rows(db_path, player_id=1):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT COUNT(*) FROM player_cell_ping WHERE player_id = ? AND protocol = ?",
        (player_id, PROTOCOL),
    ).fetchone()
    conn.close()
    return rows[0]


# ---------------------------------------------------------------------
# Unit-level: is_unknown_ping_type() itself
# ---------------------------------------------------------------------

@pytest.mark.parametrize("ping_type", ["TX", "RX", "DISC", "TRACE"])
def test_recognized_types_are_not_unknown(ping_type):
    assert is_unknown_ping_type({"type": ping_type}) is False


def test_present_unrecognized_type_is_unknown():
    assert is_unknown_ping_type({"type": "DEFER"}) is True


def test_missing_type_is_not_unknown():
    """A ping with no `type` key at all is NOT counted as unknown -- an
    absent field is different from a present, unrecognized one, and is
    already indistinguishable from "heard nothing" everywhere else this
    field is read.
    """
    assert is_unknown_ping_type({"contact": "deadbeef"}) is False


def test_none_type_is_not_unknown():
    """Same as a missing key -- an explicit `"type": None` reads the same
    as absent, not as an unrecognized value.
    """
    assert is_unknown_ping_type({"type": None}) is False


def test_non_dict_ping_is_not_unknown():
    assert is_unknown_ping_type("not a dict") is False


# ---------------------------------------------------------------------
# End-to-end: through McIngestor._process_batch_sync()
# ---------------------------------------------------------------------

def test_unknown_type_ping_increments_unknown_type_counter(db_path):
    _seed_player(db_path, player_id=1)

    ingestor = McIngestor()
    ingestor._process_batch_sync(1, "keyhash-1", [_ping("DEFER")], NOW)

    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_unknown_type"] == 1


def test_unknown_type_ping_also_increments_no_repeaters(db_path):
    """An unknown-type ping still names zero repeaters (parse_repeaters()
    falls through to an empty list for it, same as it always has), so it
    must still also increment pings_no_repeaters -- pings_unknown_type is
    ADDITIONAL, not a replacement. Both counters go up for the same ping.
    """
    _seed_player(db_path, player_id=1)

    ingestor = McIngestor()
    ingestor._process_batch_sync(1, "keyhash-1", [_ping("DEFER")], NOW)

    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_unknown_type"] == 1
    assert stats["pings_no_repeaters"] == 1


def test_unknown_type_ping_is_still_accepted_and_writes_position_row(db_path):
    """This is observability only, never a new rejection path: the ping
    must still count as accepted and still write a position row exactly
    as before pings_unknown_type existed.
    """
    _seed_player(db_path, player_id=1)

    ingestor = McIngestor()
    ingestor._process_batch_sync(1, "keyhash-1", [_ping("DEFER")], NOW)

    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_accepted"] == 1
    assert _position_rows(db_path, player_id=1) == 1


@pytest.mark.parametrize("ping_type", ["TX", "RX", "DISC", "TRACE"])
def test_recognized_type_does_not_increment_unknown_type_counter(db_path, ping_type):
    _seed_player(db_path, player_id=1)

    ping = _ping(ping_type)
    if ping_type in ("TX", "RX"):
        ping["heard_repeats"] = "cafefeed(3.5)"
    else:
        ping["repeater_id"] = "cafefeed"

    ingestor = McIngestor()
    ingestor._process_batch_sync(1, "keyhash-1", [ping], NOW)

    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_unknown_type"] == 0
    assert stats["pings_accepted"] == 1
    # A recognized type that DID name a repeater must not be miscounted
    # as no_repeaters either -- confirms the new counter didn't disturb
    # existing behavior for a normal, fully-valid ping.
    assert stats["pings_no_repeaters"] == 0


def test_recognized_type_with_no_repeaters_heard_is_unchanged(db_path):
    """A legitimate "heard nothing" ping on a recognized type (heard_repeats
    literally "None") must behave exactly as it did before this counter
    existed: no_repeaters goes up, unknown_type does not.
    """
    _seed_player(db_path, player_id=1)

    ping = _ping("RX", heard_repeats="None")

    ingestor = McIngestor()
    ingestor._process_batch_sync(1, "keyhash-1", [ping], NOW)

    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_accepted"] == 1
    assert stats["pings_no_repeaters"] == 1
    assert stats["pings_unknown_type"] == 0


def test_missing_type_ping_does_not_increment_unknown_type(db_path):
    """A ping missing `type` entirely still falls through parse_repeaters()
    to zero repeaters (and so still increments pings_no_repeaters), but
    must NOT increment pings_unknown_type -- see is_unknown_ping_type()'s
    own docstring for why a missing field is not treated as an
    unrecognized one.
    """
    _seed_player(db_path, player_id=1)

    ping = {
        "contact": "deadbeef",
        "lat": LAT,
        "lon": LON,
        "timestamp": NOW,
    }
    assert parse_repeaters(ping) == []

    ingestor = McIngestor()
    ingestor._process_batch_sync(1, "keyhash-1", [ping], NOW)

    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_accepted"] == 1
    assert stats["pings_no_repeaters"] == 1
    assert stats["pings_unknown_type"] == 0


def test_mixed_batch_counts_each_ping_once(db_path):
    """A batch with one unknown-type ping and one normal, fully-valid
    ping must count exactly one of each -- confirms per-ping accounting,
    not an all-or-nothing batch-level flag.
    """
    _seed_player(db_path, player_id=1)

    pings = [
        _ping("DEFER", ts=NOW),
        _ping("RX", ts=NOW + 1, heard_repeats="cafefeed(3.5)"),
    ]

    ingestor = McIngestor()
    ingestor._process_batch_sync(1, "keyhash-1", pings, NOW)

    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_accepted"] == 2
    assert stats["pings_unknown_type"] == 1
    assert stats["pings_no_repeaters"] == 1
