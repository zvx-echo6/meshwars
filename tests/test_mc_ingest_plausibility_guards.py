"""Tests for the three MeshCore ingest plausibility guards added
2026-09-23 (see app/config.py's mc_clock_clamp_*/mc_glitch_speed_mps/
mc_speed_reject_enabled/mc_cell_claim_cap* settings):

  1. Scoring-clock clamp (app/mc_ingest.py's _clamp_scoring_clock()): a
     ping's own client timestamp is still ACCEPTED and still recorded
     verbatim in player_cell_ping/repeater_observation history, but the
     clock that actually DRIVES scoring (decay, the defense window, the
     repeater-credit cooldown, mc_tile.last_report_ts,
     mc_tile_capture.captured_at, player_last_fix, and Places Worth
     Going's weekly window) is clamped to within
     settings.mc_clock_clamp_seconds of the server's own receipt time.
  2. Speed gate: an implied speed above settings.mc_glitch_speed_mps
     (400 m/s) is REJECTED outright (dropped before binding, the
     player_cell_ping/repeater_observation writes, and scoring) rather
     than merely logged. The 45-400 m/s by_air band is untouched --
     that is a deliberate game-policy decision, not a data-integrity
     one, and is explicitly NOT in scope here.
  3. Per-player cell-claim rate cap (settings.mc_cell_claim_cap, backed
     by app/db.py's player_cell_claim): a backstop on how many distinct
     NEW cells one player can claim inside settings.
     mc_cell_claim_cap_window_seconds, keyed off the server's own
     received_at so it cannot be defeated by a crafted client ts.
     Re-pinging a cell already claimed is never gated by this cap.

Same shape as tests/test_mc_ingest_unknown_type.py and
tests/test_ingest_integrity_gates.py: drives the real ingest path
(McIngestor._process_batch_sync() -> _process_one_ping()) against an
isolated on-disk sqlite database, rather than testing the guards in
total isolation, so these prove each guard is actually wired into
ingest end to end.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.grid import cell_center, cell_id as grid_cell_id, distance_m
from app.mc_ingest import McIngestor, _clamp_scoring_clock
from app import mc_scoring

NOW = int(time.time())
PROTOCOL = "mc"

# Well within the default play area (settings.play_area_*).
LAT, LON = 43.0, -116.0

# ~1.1km apart -- CELL_LAT_DEG is ~0.0027deg (~300m), so this is several
# cells apart and never lands two of these in the same cell.
CELL_STEP = 0.01


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Same fixture as tests/test_mc_ingest_unknown_type.py -- a fresh
    on-disk sqlite file with the real schema (including the
    player_cell_claim table and pings_clock_clamped/pings_cell_cap_exceeded
    columns), settings.db_path pointed at it.
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


def _ping(lat, lon, ts, contact="deadbeef", repeaters=("cafefeed",), ping_type="RX"):
    heard = ",".join(f"{r}(3.5)" for r in repeaters)
    return {
        "type": ping_type,
        "contact": contact,
        "lat": lat,
        "lon": lon,
        "timestamp": ts,
        "heard_repeats": heard,
    }


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


def _player_cell_ping_rows(db_path, player_id=1):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM player_cell_ping WHERE player_id = ? AND protocol = ? ORDER BY ts",
        (player_id, PROTOCOL),
    )]
    conn.close()
    return rows


def _last_fix(db_path, player_id=1):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM player_last_fix WHERE player_id = ? AND protocol = ?",
        (player_id, PROTOCOL),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _mc_tile_cells(db_path, season_id):
    conn = sqlite3.connect(db_path)
    rows = [r[0] for r in conn.execute(
        "SELECT cell_id FROM mc_tile WHERE season_id = ?", (season_id,)
    )]
    conn.close()
    return set(rows)


def _mc_tile_row(db_path, season_id, cell_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM mc_tile WHERE season_id = ? AND cell_id = ?", (season_id, cell_id)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _mc_tile_capture_row(db_path, season_id, cell_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM mc_tile_capture WHERE season_id = ? AND cell_id = ?", (season_id, cell_id)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _capture_log_by_air(db_path, season_id, cell_id):
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT by_air FROM mc_tile_capture_log WHERE season_id = ? AND cell_id = ? ORDER BY ts LIMIT 1",
        (season_id, cell_id),
    ).fetchone()
    conn.close()
    return None if row is None else bool(row[0])


def _season_id(db_path) -> int:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN IMMEDIATE")
    mc_scoring.maybe_roll_season(conn, NOW, PROTOCOL)
    sid = mc_scoring.ensure_active_season(conn, NOW, PROTOCOL)
    conn.execute("COMMIT")
    conn.close()
    return sid


def _cell_speed_mps(lat1, lon1, lat2, lon2, elapsed_s: float) -> float:
    """The exact speed app/mc_ingest.py's speed gate computes for a jump
    between the CELLS these two points land in (cell-center to
    cell-center), matching the real code."""
    c1 = grid_cell_id(lat1, lon1)
    c2 = grid_cell_id(lat2, lon2)
    la1, lo1 = cell_center(c1)
    la2, lo2 = cell_center(c2)
    return distance_m(la1, lo1, la2, lo2) / elapsed_s


# ---------------------------------------------------------------------
# _clamp_scoring_clock() -- pure unit tests
# ---------------------------------------------------------------------

def test_clamp_within_allowance_is_unchanged():
    assert _clamp_scoring_clock(NOW, NOW) == NOW
    assert _clamp_scoring_clock(NOW + 10, NOW) == NOW + 10


def test_clamp_clips_stale_ts_to_lower_bound(monkeypatch):
    monkeypatch.setattr(settings, "mc_clock_clamp_enabled", True)
    monkeypatch.setattr(settings, "mc_clock_clamp_seconds", 100)
    assert _clamp_scoring_clock(NOW - 100000, NOW) == NOW - 100


def test_clamp_clips_future_ts_to_upper_bound(monkeypatch):
    monkeypatch.setattr(settings, "mc_clock_clamp_enabled", True)
    monkeypatch.setattr(settings, "mc_clock_clamp_seconds", 100)
    assert _clamp_scoring_clock(NOW + 100000, NOW) == NOW + 100


def test_clamp_disabled_returns_raw_ts(monkeypatch):
    monkeypatch.setattr(settings, "mc_clock_clamp_enabled", False)
    assert _clamp_scoring_clock(NOW - 100000, NOW) == NOW - 100000


# ---------------------------------------------------------------------
# Change 1: scoring clock clamp, end to end through ingest
# ---------------------------------------------------------------------

def test_stale_ping_preserves_original_ts_but_clamps_scoring_clock(db_path, monkeypatch):
    """A ping arriving with a client timestamp far in the past (a
    legitimate MeshMapper offline-upload case) must still be ACCEPTED
    and its ORIGINAL timestamp preserved in player_cell_ping -- but the
    clock mc_tile/mc_tile_capture record must be clamped near the
    server's real receipt time, not the stale client value.
    """
    monkeypatch.setattr(settings, "mc_clock_clamp_seconds", 100)
    _seed_player(db_path, player_id=1)
    season_id = _season_id(db_path)

    stale_ts = NOW - 100000
    cell = grid_cell_id(LAT, LON)
    pings = [_ping(LAT, LON, stale_ts)]

    ingestor = McIngestor()
    ingestor._process_batch_sync(1, "keyhash-1", pings, NOW)

    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_accepted"] == 1
    assert stats["pings_clock_clamped"] == 1

    # History: the ORIGINAL client timestamp, unclamped, exactly as
    # today -- the dedup PK behavior/shape must not change.
    rows = _player_cell_ping_rows(db_path, player_id=1)
    assert len(rows) == 1
    assert rows[0]["ts"] == stale_ts

    # Scoring clock: player_last_fix and mc_tile_capture.captured_at must
    # be clamped to within mc_clock_clamp_seconds of NOW (the server's
    # received_at), never the raw stale value.
    lf = _last_fix(db_path, player_id=1)
    assert lf["ts"] == NOW - 100
    assert lf["ts"] != stale_ts

    capture = _mc_tile_capture_row(db_path, season_id, cell)
    assert capture["captured_at"] == NOW - 100
    assert capture["captured_at"] != stale_ts

    tile = _mc_tile_row(db_path, season_id, cell)
    assert tile["last_report_ts"] == NOW - 100


def test_stale_backdated_capture_stays_defended_when_clamped(db_path, monkeypatch):
    """The practical consequence of the clamp: a capture backdated by a
    stale client ts must NOT lose its defense window's protection. A
    second team's near-immediate, honestly-timestamped attack must
    still be blocked as "inside the defense window", not allowed to
    flip the cell because the capture LOOKS like it happened long ago.
    """
    monkeypatch.setattr(settings, "mc_clock_clamp_seconds", 60)
    _seed_player(db_path, player_id=1, team="RED")
    _seed_player(db_path, player_id=2, team="BLUE")
    season_id = _season_id(db_path)
    cell = grid_cell_id(LAT, LON)

    ingestor = McIngestor()

    # RED captures with a wildly stale client ts (offline-upload case).
    # Clamped scoring clock: NOW - 60 (the clamp allowance).
    red_received_at = NOW
    ingestor._process_batch_sync(1, "keyhash-1", [_ping(LAT, LON, NOW - 100000)], red_received_at)
    assert _mc_tile_row(db_path, season_id, cell)["owner_team"] == "RED"

    # BLUE attacks moments later in real server time, with a normal,
    # current, honest timestamp and enough repeaters to comfortably beat
    # RED's small first-paint score if the comparison were ever reached.
    blue_received_at = red_received_at + 5
    ingestor._process_batch_sync(
        2, "keyhash-2",
        [_ping(LAT, LON, blue_received_at, contact="beefcafe",
               repeaters=("aaaaaaaa", "bbbbbbbb", "cccccccc"))],
        blue_received_at,
    )

    tile = _mc_tile_row(db_path, season_id, cell)
    assert tile["owner_team"] == "RED", (
        "BLUE flipped a cell RED captured 5 real seconds ago -- the "
        "defense window was defeated by a backdated client timestamp"
    )


def test_stale_backdated_capture_flips_immediately_when_clamp_disabled(db_path, monkeypatch):
    """Same scenario as the test above, with settings.mc_clock_clamp_enabled
    turned off (the explicit escape hatch back to pre-fix behavior):
    proves the clamp above -- not some other change -- is what protects
    the defense window, by showing the exploit succeeds without it.
    """
    monkeypatch.setattr(settings, "mc_clock_clamp_enabled", False)
    _seed_player(db_path, player_id=1, team="RED")
    _seed_player(db_path, player_id=2, team="BLUE")
    season_id = _season_id(db_path)
    cell = grid_cell_id(LAT, LON)

    ingestor = McIngestor()

    red_received_at = NOW
    ingestor._process_batch_sync(1, "keyhash-1", [_ping(LAT, LON, NOW - 100000)], red_received_at)
    assert _mc_tile_row(db_path, season_id, cell)["owner_team"] == "RED"

    blue_received_at = red_received_at + 5
    ingestor._process_batch_sync(
        2, "keyhash-2",
        [_ping(LAT, LON, blue_received_at, contact="beefcafe",
               repeaters=("aaaaaaaa", "bbbbbbbb", "cccccccc"))],
        blue_received_at,
    )

    tile = _mc_tile_row(db_path, season_id, cell)
    assert tile["owner_team"] == "BLUE", (
        "expected the pre-fix vulnerability to reproduce with the clamp "
        "disabled -- if this fails, the clamp is not what was protecting "
        "the defense window in the test above"
    )


# ---------------------------------------------------------------------
# Change 2: physically-impossible-speed rejection
# ---------------------------------------------------------------------

def test_glitch_speed_ping_is_rejected(db_path):
    """A jump implying well over settings.mc_glitch_speed_mps (400 m/s)
    must be dropped entirely -- no player_cell_ping row, no square
    painted, player_last_fix left exactly where the first fix put it,
    and pings_implausible_speed counted."""
    _seed_player(db_path, player_id=1)
    season_id = _season_id(db_path)
    ingestor = McIngestor()

    p1 = _ping(LAT, LON, NOW)
    ingestor._process_batch_sync(1, "keyhash-1", [p1], NOW)
    first_fix = _last_fix(db_path, player_id=1)
    assert first_fix["ts"] == NOW

    # 0.5 degrees away (~55km), 5 seconds later -- implies ~11,000 m/s,
    # far beyond the 400 m/s glitch threshold.
    lat2, lon2, ts2 = LAT + 0.5, LON, NOW + 5
    implied_speed = _cell_speed_mps(LAT, LON, lat2, lon2, 5)
    assert implied_speed > settings.mc_glitch_speed_mps * 10  # sanity on the test's own jump

    p2 = _ping(lat2, lon2, ts2)
    ingestor._process_batch_sync(1, "keyhash-1", [p2], ts2)

    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_implausible_speed"] == 1
    assert stats["pings_accepted"] == 1  # only the first ping

    rows = _player_cell_ping_rows(db_path, player_id=1)
    assert len(rows) == 1  # the jump was never written at all
    assert _last_fix(db_path, player_id=1) == first_fix  # untouched

    jumped_cell = grid_cell_id(lat2, lon2)
    assert jumped_cell not in _mc_tile_cells(db_path, season_id)


def test_by_air_band_scores_exactly_as_before(db_path):
    """Regression guard: a speed between mc_max_speed_mps (45) and
    mc_glitch_speed_mps (400) must still be ACCEPTED, still score
    territory, and still be marked by_air on the capture log -- Change 2
    must not touch this deliberate game-policy band at all."""
    _seed_player(db_path, player_id=1)
    season_id = _season_id(db_path)
    ingestor = McIngestor()

    p1 = _ping(LAT, LON, NOW)
    ingestor._process_batch_sync(1, "keyhash-1", [p1], NOW)

    # Solve for an offset landing ~10 seconds later at ~100 m/s -- inside
    # the 45-400 m/s band.
    elapsed = 10
    target_speed = 100.0
    target_distance_m = target_speed * elapsed
    lat_offset = target_distance_m / 111_320.0
    lat2, lon2, ts2 = LAT + lat_offset, LON, NOW + elapsed

    implied_speed = _cell_speed_mps(LAT, LON, lat2, lon2, elapsed)
    assert settings.mc_max_speed_mps < implied_speed < settings.mc_glitch_speed_mps

    p2 = _ping(lat2, lon2, ts2)
    ingestor._process_batch_sync(1, "keyhash-1", [p2], ts2)

    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_implausible_speed"] == 0
    assert stats["pings_accepted"] == 2

    jumped_cell = grid_cell_id(lat2, lon2)
    assert jumped_cell in _mc_tile_cells(db_path, season_id)
    assert _capture_log_by_air(db_path, season_id, jumped_cell) is True


def test_first_ping_never_speed_rejected(db_path):
    """A player's very first-ever ping has no player_last_fix to compare
    against -- it must never be speed-rejected, no matter where it is."""
    _seed_player(db_path, player_id=1)
    season_id = _season_id(db_path)
    ingestor = McIngestor()

    p1 = _ping(LAT, LON, NOW)
    ingestor._process_batch_sync(1, "keyhash-1", [p1], NOW)

    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_implausible_speed"] == 0
    assert stats["pings_accepted"] == 1
    assert _last_fix(db_path, player_id=1)["ts"] == NOW


def test_speed_reject_disabled_falls_back_to_log_only(db_path, monkeypatch):
    """settings.mc_speed_reject_enabled=False must restore the pre-Change-2
    behavior exactly: a glitch-speed ping is still ACCEPTED (log-only,
    by_air marked only when at or under the glitch threshold -- which an
    over-threshold jump never is, so by_air stays False here)."""
    monkeypatch.setattr(settings, "mc_speed_reject_enabled", False)
    _seed_player(db_path, player_id=1)
    ingestor = McIngestor()

    p1 = _ping(LAT, LON, NOW)
    ingestor._process_batch_sync(1, "keyhash-1", [p1], NOW)

    lat2, lon2, ts2 = LAT + 0.5, LON, NOW + 5
    p2 = _ping(lat2, lon2, ts2)
    ingestor._process_batch_sync(1, "keyhash-1", [p2], ts2)

    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_implausible_speed"] == 0
    assert stats["pings_accepted"] == 2
    rows = _player_cell_ping_rows(db_path, player_id=1)
    assert len(rows) == 2


# ---------------------------------------------------------------------
# Change 3: per-player cell-claim rate cap
# ---------------------------------------------------------------------

def test_cell_cap_drops_new_claims_but_allows_owned_repings(db_path, monkeypatch):
    monkeypatch.setattr(settings, "mc_cell_claim_cap", 2)
    monkeypatch.setattr(settings, "mc_cell_claim_cap_window_seconds", 3600)
    _seed_player(db_path, player_id=1)
    season_id = _season_id(db_path)
    ingestor = McIngestor()

    cell1 = grid_cell_id(LAT, LON)
    cell2 = grid_cell_id(LAT + CELL_STEP, LON)
    cell3 = grid_cell_id(LAT + 2 * CELL_STEP, LON)

    # ts values spaced 60s apart -- CELL_STEP (~1.1km) over 60s is ~18.5
    # m/s, comfortably under even mc_max_speed_mps (45), so this batch
    # tests the cell-claim cap in isolation, not the speed gate.
    pings = [
        _ping(LAT, LON, NOW, repeaters=("aaaaaaaa",)),
        _ping(LAT + CELL_STEP, LON, NOW + 60, repeaters=("aaaaaaaa",)),
        _ping(LAT + 2 * CELL_STEP, LON, NOW + 120, repeaters=("aaaaaaaa",)),
    ]
    ingestor._process_batch_sync(1, "keyhash-1", pings, NOW)

    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_accepted"] == 3          # all three still accepted
    assert stats["pings_cell_cap_exceeded"] == 1  # only the third one's scoring dropped

    claimed_cells = _mc_tile_cells(db_path, season_id)
    assert cell1 in claimed_cells
    assert cell2 in claimed_cells
    assert cell3 not in claimed_cells  # capped -- never painted

    # Re-ping cell1 (already owned/claimed) with a fresh repeater -- must
    # be processed normally, NOT gated by the still-exhausted cap.
    repaint = _ping(LAT, LON, NOW + 180, repeaters=("bbbbbbbb",))
    ingestor._process_batch_sync(1, "keyhash-1", [repaint], NOW + 180)

    stats2 = _ingest_stat(db_path, player_id=1)
    assert stats2["pings_cell_cap_exceeded"] == 1  # unchanged -- not gated again
    tile1 = _mc_tile_row(db_path, season_id, cell1)
    assert tile1["paint_count"] == 2  # the re-ping actually scored


def test_cell_cap_cannot_be_bypassed_with_crafted_timestamps(db_path, monkeypatch):
    """All pings in one batch share a single server received_at -- the
    cap's window is keyed off THAT, not each ping's own client ts. A
    batch spreading its pings' timestamps across wildly different
    apparent "windows" must not be able to claim more new cells than the
    cap allows in this one real moment."""
    monkeypatch.setattr(settings, "mc_cell_claim_cap", 2)
    monkeypatch.setattr(settings, "mc_cell_claim_cap_window_seconds", 3600)
    _seed_player(db_path, player_id=1)
    season_id = _season_id(db_path)
    ingestor = McIngestor()

    pings = [
        _ping(LAT, LON, NOW - 500_000, repeaters=("aaaaaaaa",)),              # "days ago"
        _ping(LAT + CELL_STEP, LON, NOW, repeaters=("aaaaaaaa",)),            # "now"
        _ping(LAT + 2 * CELL_STEP, LON, NOW + 500_000, repeaters=("aaaaaaaa",)),  # "days from now"
    ]
    ingestor._process_batch_sync(1, "keyhash-1", pings, NOW)

    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_cell_cap_exceeded"] == 1

    claimed_cells = _mc_tile_cells(db_path, season_id)
    assert len(claimed_cells) == 2, (
        "crafted per-ping timestamps let more than mc_cell_claim_cap new "
        "cells through in one real server moment"
    )


def test_cell_cap_disabled_has_no_limit(db_path, monkeypatch):
    monkeypatch.setattr(settings, "mc_cell_claim_cap_enabled", False)
    monkeypatch.setattr(settings, "mc_cell_claim_cap", 2)  # would otherwise cap at 2
    _seed_player(db_path, player_id=1)
    season_id = _season_id(db_path)
    ingestor = McIngestor()

    pings = [
        _ping(LAT + i * CELL_STEP, LON, NOW + i * 60, repeaters=("aaaaaaaa",))
        for i in range(5)
    ]
    ingestor._process_batch_sync(1, "keyhash-1", pings, NOW)

    stats = _ingest_stat(db_path, player_id=1)
    assert stats["pings_cell_cap_exceeded"] == 0
    assert len(_mc_tile_cells(db_path, season_id)) == 5
