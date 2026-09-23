"""Tests for the wire_tag replay/impersonation check (app/mc_ingest.py's
check_wire_tag(), app/db.py's mc_wire_tag,
settings.mc_wire_tag_reject_enabled/mc_wire_tag_retention_days).

wire_tag arrives on effectively 100% of TX pings (measured 2026-09-23:
7,260 of 7,260 distinct, zero duplicates, format "MM:" + 10 base64url
characters) and was read nowhere before this. Stored one row per tag,
keyed on the tag itself as PRIMARY KEY, so a conflicting INSERT IS the
detection of a tag already on file. The rule:

  - The SAME player_id re-presenting a tag already on file for them ->
    ACCEPT, counted (pings_wire_tag_resubmit) -- almost certainly a
    legitimately re-uploaded MeshMapper offline session.
Additionally, the app/db.py player_cell_ping PRIMARY KEY (player_id,
    protocol, cell_id, ts) already prevents the underlying ping from
    being double-scored, so there is nothing else to gate here.
  - A DIFFERENT player_id presenting a tag already claimed by someone
    else -> REJECT that ping, counted (pings_wire_tag_conflict), logged
    at WARNING with both player ids. This is the one guard in this
    module where a hard block is justified: the measured false-positive
    rate against real traffic is zero.
  - A malformed tag (present, but not "MM:" + 10 base64url chars) ->
    counted (pings_wire_tag_malformed), never itself a rejection reason
    -- MeshMapper could change the format.

Same style as tests/test_mc_ingest_plausibility_guards.py: drives the
real ingest path (McIngestor._process_batch_sync() ->
_process_one_ping() -> check_wire_tag()) against an isolated on-disk
sqlite database, so these prove the guard is actually wired into
ingest end to end, not just correct in isolation.
"""
from __future__ import annotations

import logging
import sqlite3
import time

import pytest

from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.mc_ingest import McIngestor

NOW = int(time.time())
PROTOCOL = "mc"

# Well within the default play area (settings.play_area_*).
LAT, LON = 43.0, -116.0

# ~1.1km apart -- several grid cells apart (app/grid.py's CELL_LAT_DEG
# is ~0.0027deg/~300m), same convention
# tests/test_mc_ingest_plausibility_guards.py uses.
CELL_STEP = 0.01

VALID_TAG = "MM:AbCdEfGhIj"


@pytest.fixture
def db_path(tmp_path, monkeypatch):
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


def _seed_player(db_path, player_id, team="RED"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?, ?, ?, ?)",
        (player_id, f"player-{player_id}", team, NOW),
    )
    conn.commit()
    conn.close()


def _ping(lat=LAT, lon=LON, ts=NOW, contact="deadbeef", wire_tag=None, repeaters=("cafefeed",)):
    heard = ",".join(f"{r}(3.5)" for r in repeaters)
    ping = {
        "type": "TX",
        "contact": contact,
        "lat": lat,
        "lon": lon,
        "timestamp": ts,
        "heard_repeats": heard,
    }
    if wire_tag is not None:
        ping["wire_tag"] = wire_tag
    return ping


def _ingest_stat(db_path, player_id):
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


def _player_cell_ping_rows(db_path, player_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM player_cell_ping WHERE player_id = ? AND protocol = ? ORDER BY ts",
        (player_id, PROTOCOL),
    )]
    conn.close()
    return rows


def _wire_tag_row(db_path, tag):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM mc_wire_tag WHERE wire_tag = ?", (tag,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _wire_tag_count(db_path):
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM mc_wire_tag").fetchone()[0]
    conn.close()
    return n


# ---------------------------------------------------------------------
# First sighting
# ---------------------------------------------------------------------

def test_first_seen_wire_tag_is_stored(db_path):
    _seed_player(db_path, player_id=1)
    ingestor = McIngestor()
    ingestor._process_batch_sync(1, "keyhash-1", [_ping(wire_tag=VALID_TAG)], NOW)

    row = _wire_tag_row(db_path, VALID_TAG)
    assert row is not None
    assert row["player_id"] == 1
    assert row["first_seen_at"] == NOW

    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_accepted"] == 1
    assert stats.get("pings_wire_tag_resubmit", 0) == 0
    assert stats.get("pings_wire_tag_conflict", 0) == 0
    assert stats.get("pings_wire_tag_malformed", 0) == 0


def test_no_wire_tag_field_is_a_no_op(db_path):
    """Not every ping type carries wire_tag -- a ping with no field at
    all must be processed exactly as before this feature existed: no
    counter touched, nothing stored."""
    _seed_player(db_path, player_id=1)
    ingestor = McIngestor()
    ingestor._process_batch_sync(1, "keyhash-1", [_ping()], NOW)  # no wire_tag

    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_accepted"] == 1
    assert stats.get("pings_wire_tag_resubmit", 0) == 0
    assert stats.get("pings_wire_tag_conflict", 0) == 0
    assert stats.get("pings_wire_tag_malformed", 0) == 0
    assert _wire_tag_count(db_path) == 0


# ---------------------------------------------------------------------
# Same player re-presenting a tag -> accept
# ---------------------------------------------------------------------

def test_same_player_resubmit_is_accepted_and_counted(db_path):
    _seed_player(db_path, player_id=1)
    ingestor = McIngestor()
    ping = _ping(wire_tag=VALID_TAG)

    ingestor._process_batch_sync(1, "keyhash-1", [ping], NOW)
    # Re-upload of the exact same ping (an offline-session replay) --
    # the realistic shape of a legitimate resubmit.
    ingestor._process_batch_sync(1, "keyhash-1", [ping], NOW + 10)

    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_wire_tag_resubmit"] == 1
    assert stats["pings_wire_tag_conflict"] == 0
    # The re-upload duplicates the same (player_id, protocol, cell_id,
    # ts) row -- player_cell_ping's own PK dedup catches that, exactly
    # as documented: nothing here double-scores it.
    assert stats["pings_duplicate"] == 1
    assert stats["pings_accepted"] == 1

    rows = _player_cell_ping_rows(db_path, player_id=1)
    assert len(rows) == 1

    row = _wire_tag_row(db_path, VALID_TAG)
    assert row["player_id"] == 1
    assert row["first_seen_at"] == NOW  # unchanged by the resubmit


def test_same_player_new_ping_reusing_their_own_tag_is_accepted(db_path):
    """Not a duplicate ping this time (different cell/ts) -- still the
    same player re-presenting their own tag, still accepted."""
    _seed_player(db_path, player_id=1)
    ingestor = McIngestor()
    ingestor._process_batch_sync(
        1, "keyhash-1", [_ping(wire_tag=VALID_TAG)], NOW,
    )
    ingestor._process_batch_sync(
        1, "keyhash-1",
        [_ping(lat=LAT + CELL_STEP, lon=LON + CELL_STEP, ts=NOW + 60, wire_tag=VALID_TAG)],
        NOW + 60,
    )

    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_wire_tag_resubmit"] == 1
    assert stats["pings_accepted"] == 2
    assert stats["pings_duplicate"] == 0


# ---------------------------------------------------------------------
# Different player presenting someone else's tag -> reject
# ---------------------------------------------------------------------

def test_different_player_conflict_is_rejected_counted_and_logged(db_path, caplog):
    _seed_player(db_path, player_id=1, team="RED")
    _seed_player(db_path, player_id=2, team="BLUE")
    ingestor = McIngestor()
    ingestor._process_batch_sync(
        1, "keyhash-1", [_ping(contact="deadbeef", wire_tag=VALID_TAG)], NOW,
    )

    caplog.set_level(logging.WARNING, logger="mc_ingest")
    other_ping = _ping(
        contact="cafebabe", lat=LAT + CELL_STEP, lon=LON + CELL_STEP,
        ts=NOW + 5, wire_tag=VALID_TAG,
    )
    ingestor._process_batch_sync(2, "keyhash-2", [other_ping], NOW + 5)

    stats2 = _ingest_stat(db_path, player_id=2)
    assert stats2["pings_wire_tag_conflict"] == 1
    assert stats2["pings_accepted"] == 0

    # The ping never reached binding/dedup/scoring for player 2.
    assert _player_cell_ping_rows(db_path, player_id=2) == []

    # The tag is still owned by player 1, untouched.
    row = _wire_tag_row(db_path, VALID_TAG)
    assert row["player_id"] == 1

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        "claimed by player 1" in m and "player 2" in m and VALID_TAG in m
        for m in warnings
    ), warnings


def test_conflict_never_touches_first_players_own_stats(db_path):
    _seed_player(db_path, player_id=1, team="RED")
    _seed_player(db_path, player_id=2, team="BLUE")
    ingestor = McIngestor()
    ingestor._process_batch_sync(
        1, "keyhash-1", [_ping(contact="deadbeef", wire_tag=VALID_TAG)], NOW,
    )
    ingestor._process_batch_sync(
        2, "keyhash-2",
        [_ping(contact="cafebabe", lat=LAT + CELL_STEP, lon=LON + CELL_STEP,
               ts=NOW + 5, wire_tag=VALID_TAG)],
        NOW + 5,
    )

    stats1 = _ingest_stat(db_path, player_id=1)
    assert stats1["pings_wire_tag_conflict"] == 0
    assert stats1["pings_accepted"] == 1


# ---------------------------------------------------------------------
# Malformed tag -> counted, never rejected on format alone
# ---------------------------------------------------------------------

@pytest.mark.parametrize("bad_tag", [
    "not-a-valid-tag",
    "MM:short",
    "MM:waytoolongtobevalid",
    "XX:AbCdEfGhIj",
    "MM:AbCdEfGh!j",  # invalid character
])
def test_malformed_tag_is_counted_not_rejected(db_path, bad_tag):
    _seed_player(db_path, player_id=1)
    ingestor = McIngestor()
    ingestor._process_batch_sync(1, "keyhash-1", [_ping(wire_tag=bad_tag)], NOW)

    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_wire_tag_malformed"] == 1
    assert stats["pings_wire_tag_resubmit"] == 0
    assert stats["pings_wire_tag_conflict"] == 0
    assert stats["pings_accepted"] == 1  # never rejected on format alone

    # A malformed tag's unconfirmed shape is never stored -- avoids a
    # garbage value colliding with a real tag later.
    assert _wire_tag_count(db_path) == 0


# ---------------------------------------------------------------------
# Disable flag fully reverts to prior behaviour
# ---------------------------------------------------------------------

def test_reject_disabled_still_detects_but_never_drops_the_ping(db_path, monkeypatch):
    monkeypatch.setattr(settings, "mc_wire_tag_reject_enabled", False)
    _seed_player(db_path, player_id=1, team="RED")
    _seed_player(db_path, player_id=2, team="BLUE")
    ingestor = McIngestor()
    ingestor._process_batch_sync(
        1, "keyhash-1", [_ping(contact="deadbeef", wire_tag=VALID_TAG)], NOW,
    )
    ingestor._process_batch_sync(
        2, "keyhash-2",
        [_ping(contact="cafebabe", lat=LAT + CELL_STEP, lon=LON + CELL_STEP,
               ts=NOW + 5, wire_tag=VALID_TAG)],
        NOW + 5,
    )

    stats2 = _ingest_stat(db_path, player_id=2)
    # Still detected and counted...
    assert stats2["pings_wire_tag_conflict"] == 1
    # ...but NOT rejected -- exactly the behavior before this feature
    # existed, matching settings.mc_speed_reject_enabled's own
    # "detection always runs, only the reject action is gated" contract.
    assert stats2["pings_accepted"] == 1
    assert len(_player_cell_ping_rows(db_path, player_id=2)) == 1


# ---------------------------------------------------------------------
# New counters land in player_ingest_stat
# ---------------------------------------------------------------------

def test_all_three_counters_present_in_one_batch(db_path):
    _seed_player(db_path, player_id=1, team="RED")
    _seed_player(db_path, player_id=2, team="BLUE")
    ingestor = McIngestor()

    # Player 1: first-seen tag A, then resubmits it (2nd batch).
    tag_a = "MM:AAAAAAAAAA"
    ingestor._process_batch_sync(1, "keyhash-1", [_ping(wire_tag=tag_a)], NOW)
    ingestor._process_batch_sync(1, "keyhash-1", [_ping(wire_tag=tag_a)], NOW + 1)

    # Player 1 also sends one malformed tag in the same later batch.
    ingestor._process_batch_sync(
        1, "keyhash-1",
        [_ping(lat=LAT + CELL_STEP, lon=LON + CELL_STEP, ts=NOW + 120, wire_tag="garbage")],
        NOW + 120,
    )

    # Player 2 tries to present player 1's tag -- conflict.
    ingestor._process_batch_sync(
        2, "keyhash-2",
        [_ping(contact="cafebabe", lat=LAT - CELL_STEP, lon=LON - CELL_STEP,
               ts=NOW + 5, wire_tag=tag_a)],
        NOW + 5,
    )

    stats1 = _ingest_stat(db_path, player_id=1)
    assert stats1["pings_wire_tag_resubmit"] == 1
    assert stats1["pings_wire_tag_malformed"] == 1
    assert stats1["pings_wire_tag_conflict"] == 0

    stats2 = _ingest_stat(db_path, player_id=2)
    assert stats2["pings_wire_tag_conflict"] == 1
