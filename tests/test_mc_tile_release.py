"""Tests for automatic release of long-abandoned MeshCore territory
(app/config.py's mc_tile_release_* settings; app/mc_scoring.py's
find_expired_tiles()/release_tile(); app/mc_ingest.py's
_release_expired_tiles_sync(), hooked into the existing hourly
_maybe_housekeeping()).

Driven off the existing linear decay clock (mc_scoring.decayed_score()),
not a new presence record: a cell whose owning team's score reached
exactly 0 and stayed there for longer than settings.mc_tile_release_zero_hours
becomes unclaimed -- its mc_tile row is deleted (a cell has no neutral/
zero-owner state) and the release is logged to mc_tile_capture_log as
its own event_type='release' row so app/results.py's ownership_at() (the
sole source of truth for month standings) sees "no owner" from that
instant on.

Two fixture styles, matching this repo's existing convention (see
tests/test_mc_ingest_plausibility_guards.py's own docstring):
  - `conn`: the shared in-memory fixture (tests/conftest.py) for
    mc_scoring/results-level tests that don't need a real db_path.
  - `db_path`: a fresh on-disk sqlite file for anything that goes
    through app/mc_ingest.py's McIngestor, which opens its own
    connections via app/db.connect() (keyed on settings.db_path) rather
    than taking one as an argument.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from app import mc_scoring, results
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.grid import cell_id as grid_cell_id, cell_indices
from app.mc_ingest import PROTOCOL, McIngestor

NOW = int(time.time())
LAT, LON = 43.0, -116.0
CELL = grid_cell_id(LAT, LON)


# ---------------------------------------------------------------------
# in-memory (`conn`) helpers
# ---------------------------------------------------------------------

def _season(conn, protocol="mc", started_at=None, ends_at=None, status="active"):
    started_at = NOW - 1_000_000 if started_at is None else started_at
    ends_at = NOW + 1_000_000 if ends_at is None else ends_at
    cur = conn.execute(
        "INSERT INTO mc_season(protocol, started_at, ends_at, status) VALUES (?,?,?,?)",
        (protocol, started_at, ends_at, status),
    )
    return cur.lastrowid


def _player(conn, player_id, team, name=None):
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?,?,?,?)",
        (player_id, name or f"player-{player_id}", team, NOW),
    )


def _seed_owned_cell(conn, season_id, cell_id, team, score, last_update,
                      player_id=1, paint_count=1):
    """A cell owned by `team`, with a mc_tile_score row backing it --
    the shape apply_paint() always leaves behind (see
    find_expired_tiles()'s own docstring on why the join it uses never
    has to handle a missing score row in practice)."""
    lat_idx, lon_idx = cell_indices(cell_id)
    conn.execute(
        "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, "
        "last_report_ts, paint_count, lat_idx, lon_idx) VALUES (?,?,?,?,?,?,?,?)",
        (season_id, cell_id, team, player_id, last_update, paint_count, lat_idx, lon_idx),
    )
    conn.execute(
        "INSERT INTO mc_tile_score(season_id, cell_id, team, score, last_update) "
        "VALUES (?,?,?,?,?)",
        (season_id, cell_id, team, score, last_update),
    )
    conn.execute(
        "INSERT INTO mc_tile_capture(season_id, cell_id, captured_at, captured_by_team) "
        "VALUES (?,?,?,?)",
        (season_id, cell_id, last_update, team),
    )
    conn.execute(
        "INSERT INTO mc_tile_capture_log(season_id, cell_id, ts, by_player_id, by_team, from_team) "
        "VALUES (?,?,?,?,?,NULL)",
        (season_id, cell_id, last_update, player_id, team),
    )


ZERO_HOURS = 720  # the feature's own default (30 days)
THRESHOLD_S = ZERO_HOURS * 3600


# ---------------------------------------------------------------------
# find_expired_tiles() -- the core decay-clock math
# ---------------------------------------------------------------------

def test_cell_at_zero_beyond_threshold_is_released(conn):
    season_id = _season(conn)
    _seed_owned_cell(conn, season_id, CELL, "RED", score=0.0,
                      last_update=NOW - THRESHOLD_S - 100)

    expired = mc_scoring.find_expired_tiles(conn, season_id, NOW, ZERO_HOURS, limit=None)

    assert [e.cell_id for e in expired] == [CELL]
    assert expired[0].team == "RED"


def test_cell_at_zero_inside_threshold_is_not_released(conn):
    season_id = _season(conn)
    _seed_owned_cell(conn, season_id, CELL, "RED", score=0.0,
                      last_update=NOW - THRESHOLD_S + 100)

    expired = mc_scoring.find_expired_tiles(conn, season_id, NOW, ZERO_HOURS, limit=None)

    assert expired == []


def test_cell_with_nonzero_score_is_not_released(conn):
    """A high score, decaying from long ago, that has not reached 0 YET
    (settings.mc_score_decay_per_day defaults to 0.25/day, so 1000
    points takes over a decade to floor out) must never be released no
    matter how old last_update is -- only the instant the score
    actually reaches 0 starts this feature's clock, not last_update
    itself."""
    season_id = _season(conn)
    _seed_owned_cell(conn, season_id, CELL, "RED", score=1000.0,
                      last_update=NOW - THRESHOLD_S * 10)

    expired = mc_scoring.find_expired_tiles(conn, season_id, NOW, ZERO_HOURS, limit=None)

    assert expired == []


def test_zero_ts_is_computed_not_just_last_update(conn):
    """A score that reaches 0 partway between last_update and now is
    released based on WHEN it actually hit 0 (zero_ts), not naively off
    last_update -- proves the math in mc_scoring._zero_ts(), not just
    the already-zero case the other tests use."""
    decay = settings.mc_score_decay_per_day
    season_id = _season(conn)
    # Score of 10*decay takes exactly 10 days to reach 0. Seed
    # last_update 40 days ago, so zero_ts lands 30 days ago -- exactly
    # at the 720h/30d threshold's edge. Push it one more day past to be
    # unambiguously beyond the threshold.
    last_update = NOW - 41 * 86400
    score = 10.0 * decay
    _seed_owned_cell(conn, season_id, CELL, "RED", score=score, last_update=last_update)

    expected_zero_ts = last_update + 10 * 86400
    assert NOW - expected_zero_ts > THRESHOLD_S  # sanity: past the threshold

    expired = mc_scoring.find_expired_tiles(conn, season_id, NOW, ZERO_HOURS, limit=None)

    assert len(expired) == 1
    assert expired[0].zero_ts == expected_zero_ts


# ---------------------------------------------------------------------
# release_tile() -- what a release actually does to the tables
# ---------------------------------------------------------------------

def test_release_deletes_tile_and_capture_but_keeps_score_and_unique_painter(conn):
    season_id = _season(conn)
    _seed_owned_cell(conn, season_id, CELL, "RED", score=0.0, last_update=NOW - THRESHOLD_S - 1)
    conn.execute(
        "INSERT INTO mc_tile_unique_painter(season_id, cell_id, team, player_id, first_ts, paint_count) "
        "VALUES (?,?,?,?,?,1)",
        (season_id, CELL, "RED", 1, NOW - THRESHOLD_S - 1),
    )

    mc_scoring.release_tile(conn, season_id, CELL, "RED", NOW)

    assert conn.execute(
        "SELECT 1 FROM mc_tile WHERE season_id = ? AND cell_id = ?", (season_id, CELL)
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM mc_tile_capture WHERE season_id = ? AND cell_id = ?", (season_id, CELL)
    ).fetchone() is None
    # mc_tile_score and mc_tile_unique_painter are deliberately untouched.
    assert conn.execute(
        "SELECT score FROM mc_tile_score WHERE season_id = ? AND cell_id = ? AND team = ?",
        (season_id, CELL, "RED"),
    ).fetchone()[0] == 0.0
    assert conn.execute(
        "SELECT 1 FROM mc_tile_unique_painter WHERE season_id = ? AND cell_id = ? "
        "AND team = ? AND player_id = ?",
        (season_id, CELL, "RED", 1),
    ).fetchone() is not None


def test_release_logs_a_release_event(conn):
    season_id = _season(conn)
    _seed_owned_cell(conn, season_id, CELL, "RED", score=0.0, last_update=NOW - THRESHOLD_S - 1)

    mc_scoring.release_tile(conn, season_id, CELL, "RED", NOW)

    row = conn.execute(
        "SELECT by_player_id, by_team, from_team, event_type FROM mc_tile_capture_log "
        " WHERE season_id = ? AND cell_id = ? AND ts = ?",
        (season_id, CELL, NOW),
    ).fetchone()
    assert row is not None
    assert row["by_player_id"] is None
    assert row["by_team"] is None
    assert row["from_team"] == "RED"
    assert row["event_type"] == "release"


def test_released_cell_is_recapturable_and_reinserted_row_has_grid_indices(conn):
    season_id = _season(conn)
    _player(conn, 2, "BLUE")
    _seed_owned_cell(conn, season_id, CELL, "RED", score=0.0, last_update=NOW - THRESHOLD_S - 1)

    mc_scoring.release_tile(conn, season_id, CELL, "RED", NOW)

    result = mc_scoring.apply_paint(
        conn, season_id, player_id=2, team="BLUE", cell_id=CELL, ts=NOW + 10,
        repeater_ids=["repeaterA"],
        points_per_repeater=settings.mc_points_per_repeater,
        max_points_per_ping=settings.mc_max_points_per_ping,
        protocol="mc", received_at=NOW + 10,
    )

    # A fresh capture, not a flip -- the old row is genuinely gone.
    assert result.outcome == "captured"

    tile = conn.execute(
        "SELECT owner_team, lat_idx, lon_idx FROM mc_tile WHERE season_id = ? AND cell_id = ?",
        (season_id, CELL),
    ).fetchone()
    assert tile["owner_team"] == "BLUE"
    expected_lat, expected_lon = cell_indices(CELL)
    assert tile["lat_idx"] == expected_lat
    assert tile["lon_idx"] == expected_lon


# ---------------------------------------------------------------------
# ownership_at() / month standings
# ---------------------------------------------------------------------

def test_ownership_at_shows_owner_before_release_and_nobody_after(conn):
    season_id = _season(conn, started_at=NOW - 1_000_000)
    capture_ts = NOW - THRESHOLD_S - 1000
    _seed_owned_cell(conn, season_id, CELL, "RED", score=0.0, last_update=capture_ts)

    release_ts = NOW - 10
    mc_scoring.release_tile(conn, season_id, CELL, "RED", release_ts)

    before = results.ownership_at(conn, "mc", release_ts - 1)
    assert [r["cell_id"] for r in before] == [CELL]
    assert before[0]["team"] == "RED"

    after = results.ownership_at(conn, "mc", release_ts + 1)
    assert after == []


def test_month_standings_do_not_count_a_released_cell(conn):
    month = results.month_key(NOW)
    start, end = results.month_bounds(month)
    season_id = _season(conn, started_at=start - 10_000_000, ends_at=end + 10_000_000)

    capture_ts = start + 10
    _seed_owned_cell(conn, season_id, CELL, "RED", score=0.0, last_update=capture_ts)
    release_ts = start + 20
    mc_scoring.release_tile(conn, season_id, CELL, "RED", release_ts)

    data = results.compute_month(conn, "mc", month, now=end - 1)
    red = next(s for s in data["standings"] if s["team"] == "RED")
    assert red["squares"] == 0


# ---------------------------------------------------------------------
# McIngestor._release_expired_tiles_sync() -- feature gating, dry-run,
# the per-sweep ceiling
# ---------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """A fresh on-disk sqlite database, same shape as
    tests/test_mc_ingest_plausibility_guards.py's own fixture --
    McIngestor's release sweep opens its own connections via
    app/db.connect(), keyed on settings.db_path, rather than taking one
    as an argument."""
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


def _seed_owned_cell_on_disk(db_path, season_id, cell_id, team, score, last_update, player_id=1):
    conn = sqlite3.connect(db_path)
    _seed_owned_cell(conn, season_id, cell_id, team, score, last_update, player_id)
    conn.commit()
    conn.close()


def _season_on_disk(db_path, protocol="mc"):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN IMMEDIATE")
    season_id = mc_scoring.ensure_active_season(conn, NOW, protocol)
    conn.execute("COMMIT")
    conn.close()
    return season_id


def _tile_count(db_path, season_id):
    conn = sqlite3.connect(db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM mc_tile WHERE season_id = ?", (season_id,)
    ).fetchone()[0]
    conn.close()
    return n


def _release_log_count(db_path, season_id):
    conn = sqlite3.connect(db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM mc_tile_capture_log WHERE season_id = ? AND event_type = 'release'",
        (season_id,),
    ).fetchone()[0]
    conn.close()
    return n


def test_feature_disabled_does_nothing(db_path, monkeypatch):
    monkeypatch.setattr(settings, "mc_tile_release_enabled", False)
    season_id = _season_on_disk(db_path)
    _seed_owned_cell_on_disk(db_path, season_id, CELL, "RED", score=0.0,
                              last_update=NOW - THRESHOLD_S - 1)

    ingestor = McIngestor()
    summary = ingestor._release_expired_tiles_sync()

    assert summary is None
    assert _tile_count(db_path, season_id) == 1
    assert _release_log_count(db_path, season_id) == 0


def test_dry_run_reports_but_changes_nothing(db_path, monkeypatch):
    monkeypatch.setattr(settings, "mc_tile_release_enabled", True)
    monkeypatch.setattr(settings, "mc_tile_release_dry_run", True)
    season_id = _season_on_disk(db_path)
    _seed_owned_cell_on_disk(db_path, season_id, CELL, "RED", score=0.0,
                              last_update=NOW - THRESHOLD_S - 1)

    ingestor = McIngestor()
    summary = ingestor._release_expired_tiles_sync()

    assert summary["dry_run"] is True
    assert summary["count"] == 1
    assert summary["by_team"] == {"RED": 1}
    assert summary["sample_cell_ids"] == [CELL]
    assert _tile_count(db_path, season_id) == 1  # untouched
    assert _release_log_count(db_path, season_id) == 0  # untouched


def test_live_run_releases_and_logs(db_path, monkeypatch):
    monkeypatch.setattr(settings, "mc_tile_release_enabled", True)
    monkeypatch.setattr(settings, "mc_tile_release_dry_run", False)
    season_id = _season_on_disk(db_path)
    _seed_owned_cell_on_disk(db_path, season_id, CELL, "RED", score=0.0,
                              last_update=NOW - THRESHOLD_S - 1)

    ingestor = McIngestor()
    summary = ingestor._release_expired_tiles_sync()

    assert summary["dry_run"] is False
    assert summary["count"] == 1
    assert _tile_count(db_path, season_id) == 0
    assert _release_log_count(db_path, season_id) == 1


def test_per_sweep_ceiling_is_honoured(db_path, monkeypatch):
    monkeypatch.setattr(settings, "mc_tile_release_enabled", True)
    monkeypatch.setattr(settings, "mc_tile_release_dry_run", False)
    monkeypatch.setattr(settings, "mc_tile_release_max_per_sweep", 2)
    season_id = _season_on_disk(db_path)
    for i in range(5):
        cell = grid_cell_id(LAT + i * 0.05, LON)
        _seed_owned_cell_on_disk(db_path, season_id, cell, "RED", score=0.0,
                                  last_update=NOW - THRESHOLD_S - 1000 + i)

    ingestor = McIngestor()
    summary = ingestor._release_expired_tiles_sync()

    assert summary["count"] == 2
    assert _tile_count(db_path, season_id) == 3  # 5 seeded, 2 released
    assert _release_log_count(db_path, season_id) == 2


# ---------------------------------------------------------------------
# Clock-clamp interaction: a crafted future client ts cannot postpone
# release beyond mc_clock_clamp_seconds
# ---------------------------------------------------------------------

def _ping(lat, lon, ts, contact="deadbeef", repeaters=("cafefeed",)):
    heard = ",".join(f"{r}(3.5)" for r in repeaters)
    return {
        "type": "RX", "contact": contact, "lat": lat, "lon": lon,
        "timestamp": ts, "heard_repeats": heard,
    }


def test_crafted_future_ts_cannot_postpone_release(db_path, monkeypatch):
    """Without app/mc_ingest.py's scoring-clock clamp (the branch this
    feature is stacked on), a player could keep sending a ping with a
    client `timestamp` far in the future to push mc_tile_score.last_update
    arbitrarily forward, postponing the release clock indefinitely. With
    the clamp on (its default), last_update can be pushed at most
    settings.mc_clock_clamp_seconds ahead of the server's own receipt
    time -- nowhere near enough to matter against a 30-day threshold.
    """
    monkeypatch.setattr(settings, "mc_clock_clamp_enabled", True)
    monkeypatch.setattr(settings, "mc_clock_clamp_seconds", 3600)
    monkeypatch.setattr(settings, "mc_tile_release_enabled", True)
    monkeypatch.setattr(settings, "mc_tile_release_dry_run", False)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (1, 'p1', 'RED', ?)",
        (NOW,),
    )
    season_id = mc_scoring.ensure_active_season(conn, NOW, PROTOCOL)
    conn.execute("COMMIT")
    conn.close()

    received_at = NOW
    crafted_future_ts = NOW + 10_000_000  # ~116 days in the "future"

    ingestor = McIngestor()
    ingestor._process_batch_sync(1, "keyhash-1", [_ping(LAT, LON, crafted_future_ts)], received_at)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT score, last_update FROM mc_tile_score WHERE season_id = ? AND cell_id = ? AND team = 'RED'",
        (season_id, CELL),
    ).fetchone()
    conn.close()
    assert row is not None
    # Clamped to within mc_clock_clamp_seconds of received_at -- never
    # anywhere near the crafted future value.
    assert row["last_update"] == received_at + settings.mc_clock_clamp_seconds
    assert row["last_update"] != crafted_future_ts

    # Simulate "much later": far enough past this score's OWN zero_ts
    # (computed from the CLAMPED last_update, exactly as
    # find_expired_tiles() does) to clear the release threshold. The
    # score is a small first-paint amount, so it decays to 0 in a
    # couple of days from the clamped anchor -- nowhere near the ~116
    # days the crafted ts tried to claim.
    zero_ts = mc_scoring._zero_ts(row["score"], row["last_update"])
    far_future = zero_ts + THRESHOLD_S + 10_000

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN IMMEDIATE")
    expired = mc_scoring.find_expired_tiles(conn, season_id, far_future, ZERO_HOURS, limit=None)
    conn.execute("COMMIT")
    conn.close()

    assert [e.cell_id for e in expired] == [CELL]
