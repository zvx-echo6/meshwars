"""Tests for the actual wiring between the MeshCore ingest pipeline
(app/mc_ingest.py) and Places Worth Going (app/place_scoring.py) --
not app/place_scoring.credit_places() in isolation (tests/
test_place_scoring.py already covers that exhaustively), but the hook
itself: McIngestor._process_batch_sync() -> _process_one_ping() ->
mc_scoring.apply_paint() -> credit_places(), exactly the call chain a
real MeshMapper batch posted to POST /api/mc/ingest goes through.

This exists because that wiring can be correct in isolation
(credit_places() alone, apply_paint() alone) and still never fire in
production if the hook between them is missing, mis-called, or its
try/except silently eats a real bug -- which is exactly what "zero
rows in place_activation despite tens of thousands of live places and
an actively-ingesting board" could mean, and what a controlled ingest
test against a running preview (CT 113) ruled out in practice: a real
POST to /api/mc/ingest for a live, in-bounds place produced a
place_activation row with the right player/place/week/points, a repeat
did not double-award, and painting past the 100-point weekly cap
stopped exactly at 100 with no partial credit. These tests reproduce
that same real-path proof against an isolated in-process database, so
it runs in CI rather than depending on a live server.

Talks to McIngestor directly (not through FastAPI/HTTP) since the
queue and HTTP handler in app/api.py are thin, untested-here plumbing
around the same call -- see McIngestor.submit()/_run_worker(), which
do nothing but hand off to _process_batch_sync(), the method these
tests call.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.grid import cell_id as grid_cell_id
from app.mc_ingest import McIngestor
from app.place_rotation import week_start_for_ts

NOW = int(time.time())
WEEK = week_start_for_ts(NOW)

# Well within the default play area (settings.play_area_*).
LAT, LON = 43.0, -116.0


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Point app.config.settings.db_path at a fresh on-disk sqlite file
    with the real schema, and restore it afterward. McIngestor's
    _process_batch_sync() calls app.db.connect(), which always opens
    settings.db_path -- there is no way to hand it a connection object
    directly, so the hook can only be exercised this way, matching how
    the real worker opens its own connection per batch.
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


def _seed_place(db_path, place_id, points, lat=LAT, lon=LON, rotates=0, active=1, live=True):
    """Mirrors tests/test_place_scoring.py's _place() helper, plus the
    place_week row a rotating place needs to be this week's live draw.
    """
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, "
        "rotates, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (place_id, "landmark", f"ref-{place_id}", f"place-{place_id}", lat, lon,
         points, "TEST", rotates, active, NOW),
    )
    cid = grid_cell_id(lat, lon)
    conn.execute("INSERT INTO place_cell(place_id, cell_id) VALUES (?, ?)", (place_id, cid))
    if rotates and live:
        conn.execute(
            "INSERT INTO place_week(week_start, place_id) VALUES (?, ?)",
            (WEEK, place_id),
        )
    conn.commit()
    conn.close()


def _ping(lat, lon, ts, contact="deadbeef", repeater="cafefeed"):
    return {
        "type": "RX",
        "contact": contact,
        "lat": lat,
        "lon": lon,
        "timestamp": ts,
        "heard_repeats": f"{repeater}(3.5)",
    }


def _activations(db_path, player_id=1):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM place_activation WHERE player_id = ? ORDER BY id", (player_id,)
    )]
    conn.close()
    return rows


def test_real_ingest_batch_credits_a_live_place(db_path):
    """A batch posted through the real MeshCore ingest pipeline, landing
    on a live, always-active place, must produce a place_activation row
    -- proving the hook (not just credit_places() alone) actually
    fires.
    """
    _seed_player(db_path, player_id=1)
    _seed_place(db_path, place_id=100, points=5, active=1, rotates=0)

    ingestor = McIngestor()
    ingestor._process_batch_sync(1, "keyhash-1", [_ping(LAT, LON, NOW)], NOW)

    rows = _activations(db_path, player_id=1)
    assert len(rows) == 1
    assert rows[0]["place_id"] == 100
    assert rows[0]["player_id"] == 1
    assert rows[0]["week_start"] == WEEK
    assert rows[0]["points"] == 5


def test_real_ingest_batch_skips_a_rotating_place_not_live(db_path):
    """The mirror image: a rotating place that did NOT draw this week
    must credit nothing through the real path either -- this is the
    "correctly no award" case the original investigation also found
    (Western Springs Mini Park, rotating, not live), told apart here
    from a hook that simply never fires.
    """
    _seed_player(db_path, player_id=1)
    _seed_place(db_path, place_id=101, points=5, active=1, rotates=1, live=False)

    # Pre-resolve this week's draw to a placeholder that is NOT place
    # 101, before any ingest runs. Without this, place 101 would be the
    # only rotating candidate in the whole (test) database, and
    # credit_places()'s own resolve_week() call would draw it into
    # this week's live set by default (nothing to lose a spacing check
    # against) -- masking exactly the bug this test exists to catch.
    # A real deployment never has this problem (thousands of rotating
    # candidates to draw from); this only matters because the test
    # fixture is otherwise empty.
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO place_week(week_start, place_id) VALUES (?, ?)", (WEEK, -1))
    conn.commit()
    conn.close()

    ingestor = McIngestor()
    ingestor._process_batch_sync(1, "keyhash-1", [_ping(LAT, LON, NOW)], NOW)

    assert _activations(db_path, player_id=1) == []


def test_real_ingest_batch_does_not_double_award_same_place_same_week(db_path):
    """Two batches painting the same live place in the same week --
    exactly what a second real visit produces -- must credit it once."""
    _seed_player(db_path, player_id=1)
    _seed_place(db_path, place_id=102, points=5, active=1, rotates=0)

    ingestor = McIngestor()
    ingestor._process_batch_sync(1, "keyhash-1", [_ping(LAT, LON, NOW)], NOW)
    ingestor._process_batch_sync(1, "keyhash-1", [_ping(LAT, LON, NOW + 60)], NOW + 60)

    rows = _activations(db_path, player_id=1)
    assert len(rows) == 1
    assert rows[0]["points"] == 5


def test_real_ingest_batch_stops_at_weekly_cap(db_path):
    """Five 25-point always-active places, each at a distinct cell,
    painted through five separate real batches for the same player in
    the same week: the first four sum to exactly 100 (the cap), so the
    fifth must be fully blocked -- no partial credit, no overflow. This
    exercises the cap through the real ingest path end to end, matching
    the manual test run against the CT 113 preview (which used a mix
    of 25- and 10-point places to the same effect: 85, then a 25-point
    place correctly skipped as not fitting the 15 remaining, then a
    10-point place credited to reach 95, then another 10-point place
    correctly skipped, then a final 5-point place reached exactly 100).
    """
    _seed_player(db_path, player_id=1)
    # Distinct lat/lon -> distinct cells -> distinct places, each 25 pts.
    coords = [(43.0, -116.0), (43.01, -116.0), (43.02, -116.0),
              (43.03, -116.0), (43.04, -116.0)]
    for i, (lat, lon) in enumerate(coords, start=200):
        _seed_place(db_path, place_id=i, points=25, lat=lat, lon=lon, active=1, rotates=0)

    ingestor = McIngestor()
    for i, (lat, lon) in enumerate(coords, start=200):
        ingestor._process_batch_sync(1, "keyhash-1", [_ping(lat, lon, NOW + i)], NOW + i)

    rows = _activations(db_path, player_id=1)
    total = sum(r["points"] for r in rows)
    assert total == 100
    assert len(rows) == 4  # the fifth place credited nothing -- cap already full
    credited_ids = {r["place_id"] for r in rows}
    assert 204 not in credited_ids  # the fifth (last-seeded) place never gets in
