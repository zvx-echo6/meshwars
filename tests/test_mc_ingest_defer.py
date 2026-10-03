"""Tests for MeshMapper "DEFER" items posted to POST /api/mc/ingest.

A DEFER ({"type": "DEFER", lat, lon, timestamp, held, radio_freq, contact,
iata}) carries no heard_repeats / repeater_id. It is a recognized type and
scores exactly like a normal ping: flat settings.mc_max_points_per_ping via
apply_paint's flat mode, the same per-cell cooldown (mc_cooldown_seconds)
real pings have, the same cell-claim cap, and place credit. It records no
repeater observation and bumps neither pings_no_repeaters nor
pings_unknown_type.

Drives McIngestor._process_batch_sync() like the neighbouring ingest tests.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.grid import cell_id as grid_cell_id
from app.mc_ingest import McIngestor

# Pinned (Saturday noon America/Boise) so place-week math never straddles a rollover.
NOW = 1768071600
LAT, LON = 43.0, -116.0
CELL = grid_cell_id(LAT, LON)
CELL_STEP = 0.01


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


def _seed_player(db_path, player_id=1, team="RED"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?, ?, ?, ?)",
        (player_id, f"player-{player_id}", team, NOW),
    )
    conn.commit()
    conn.close()


def _seed_place(db_path, place_id=1, points=10, lat=LAT, lon=LON):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, "
        "rotates, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (place_id, "landmark", f"ref-{place_id}", f"place-{place_id}", lat, lon,
         points, "TEST", 0, 1, NOW),
    )
    conn.execute("INSERT INTO place_cell(place_id, cell_id) VALUES (?, ?)",
                 (place_id, grid_cell_id(lat, lon)))
    conn.commit()
    conn.close()


def _defer(ts=NOW, lat=LAT, lon=LON, contact="deadbeef"):
    return {
        "type": "DEFER", "contact": contact, "lat": lat, "lon": lon,
        "timestamp": ts, "held": "tx", "radio_freq": 910.525, "iata": "BOI",
    }


def _tx(ts=NOW, lat=LAT, lon=LON, contact="deadbeef", repeater="cafefeed"):
    return {
        "type": "TX", "contact": contact, "lat": lat, "lon": lon,
        "timestamp": ts, "heard_repeats": f"{repeater}(3.5)",
    }


def _ingest(ingestor, player_id, pings, received_at, contact_hash=None):
    ingestor._process_batch_sync(player_id, contact_hash or f"keyhash-{player_id}", pings, received_at)


def _tile(db_path, cell=CELL):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM mc_tile WHERE cell_id = ?", (cell,)).fetchone()
    conn.close()
    return None if row is None else dict(row)


def _stats(db_path, player_id=1):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    totals = {}
    for r in conn.execute("SELECT * FROM player_ingest_stat WHERE player_id = ?", (player_id,)):
        for k, v in dict(r).items():
            if isinstance(v, int) and k not in ("player_id", "day"):
                totals[k] = totals.get(k, 0) + v
    conn.close()
    return totals


def _count(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    n = conn.execute(sql, args).fetchone()[0]
    conn.close()
    return n


def test_defer_captures_unowned_cell_for_player_team(db_path):
    _seed_player(db_path, 1, "RED")
    _ingest(McIngestor(), 1, [_defer()], NOW)
    tile = _tile(db_path)
    assert tile is not None
    assert tile["owner_team"] == "RED"


def test_defer_can_flip_other_teams_cell_like_a_real_ping(db_path):
    _seed_player(db_path, 1, "RED")
    _seed_player(db_path, 2, "BLUE")
    ing = McIngestor()
    _ingest(ing, 1, [_defer(contact="aaaaaaaa")], NOW)
    assert _tile(db_path)["owner_team"] == "RED"

    # Inside the defense window: cannot flip.
    t1 = NOW + 10
    _ingest(ing, 2, [_defer(ts=t1, contact="bbbbbbbb")], t1)
    assert _tile(db_path)["owner_team"] == "RED"

    # Outside the defense window, BLUE's score (own paints + unique bonus)
    # reaches RED's decayed score: flips, same rule a real ping follows.
    t2 = NOW + settings.mc_defense_window_seconds + 60
    _ingest(ing, 2, [_defer(ts=t2, contact="bbbbbbbb")], t2)
    assert _tile(db_path)["owner_team"] == "BLUE"


def test_defer_flip_matches_real_ping_flip(db_path):
    """Same scenario with TX pings (one repeater each, 0.1 pts) shows the
    rules are shared: a lone weaker real ping does not flip, a DEFER (1.0
    flat) does -- DEFER earns the max a normal ping can. The flip must
    hand the cell to the DEFER player's team."""
    _seed_player(db_path, 1, "RED")
    _seed_player(db_path, 2, "BLUE")
    ing = McIngestor()
    _ingest(ing, 1, [_defer(contact="aaaaaaaa")], NOW)
    t2 = NOW + settings.mc_defense_window_seconds + 60
    _ingest(ing, 2, [_tx(ts=t2, contact="bbbbbbbb")], t2)
    # One real TX (0.1 pts + 0.5 first-paint bonus) is below RED's 1.0 + 0.5: no flip.
    assert _tile(db_path)["owner_team"] == "RED"
    _ingest(ing, 2, [_defer(ts=t2 + 400, contact="bbbbbbbb")], t2 + 400)
    assert _tile(db_path)["owner_team"] == "BLUE"


def test_defer_credits_places(db_path):
    _seed_place(db_path, 1, points=10)
    _seed_player(db_path, 1, "RED")
    _ingest(McIngestor(), 1, [_defer()], NOW)
    assert _count(db_path, "SELECT COUNT(*) FROM place_activation WHERE player_id = 1") == 1


def test_defer_does_not_bump_no_repeaters_or_unknown_type(db_path):
    _seed_player(db_path, 1, "RED")
    _ingest(McIngestor(), 1, [_defer()], NOW)
    s = _stats(db_path)
    assert s["pings_accepted"] == 1
    assert s["pings_no_repeaters"] == 0
    assert s["pings_unknown_type"] == 0


def test_defer_writes_no_repeater_observations(db_path):
    _seed_player(db_path, 1, "RED")
    _ingest(McIngestor(), 1, [_defer()], NOW)
    assert _count(db_path, "SELECT COUNT(*) FROM repeater_observation") == 0


def test_defer_second_in_cell_inside_cooldown_earns_nothing(db_path):
    _seed_player(db_path, 1, "RED")
    ing = McIngestor()
    _ingest(ing, 1, [_defer(ts=NOW)], NOW)
    t = NOW + settings.mc_cooldown_seconds - 10
    _ingest(ing, 1, [_defer(ts=t)], t)
    assert _tile(db_path)["paint_count"] == 1  # no second paint


def test_defer_cooldown_outcome_is_cooldown_and_still_credits_place_like_real_pings(db_path, monkeypatch):
    """Capture apply_paint/credit_places outcomes through the real path."""
    from app import mc_ingest
    seen = []
    real = mc_ingest.credit_places

    def spy(conn, player_id, cell, ts, outcome, *a, **k):
        seen.append(outcome)
        return real(conn, player_id, cell, ts, outcome, *a, **k)

    monkeypatch.setattr(mc_ingest, "credit_places", spy)
    _seed_player(db_path, 1, "RED")
    ing = McIngestor()
    _ingest(ing, 1, [_defer(ts=NOW)], NOW)
    t = NOW + 30
    _ingest(ing, 1, [_defer(ts=t)], t)
    assert seen == ["captured", "cooldown"]


def test_defer_after_cooldown_scores_again(db_path):
    _seed_player(db_path, 1, "RED")
    ing = McIngestor()
    _ingest(ing, 1, [_defer(ts=NOW)], NOW)
    t = NOW + settings.mc_cooldown_seconds + 5
    _ingest(ing, 1, [_defer(ts=t)], t)
    assert _tile(db_path)["paint_count"] == 2


def test_defer_cooldown_is_per_cell(db_path):
    _seed_player(db_path, 1, "RED")
    ing = McIngestor()
    _ingest(ing, 1, [_defer(ts=NOW)], NOW)
    t = NOW + 120
    _ingest(ing, 1, [_defer(ts=t, lat=LAT + CELL_STEP)], t)
    assert _tile(db_path, grid_cell_id(LAT + CELL_STEP, LON)) is not None


def test_defer_respects_cell_claim_cap(db_path, monkeypatch):
    monkeypatch.setattr(settings, "mc_cell_claim_cap", 1)
    monkeypatch.setattr(settings, "mc_cell_claim_cap_window_seconds", 3600)
    _seed_player(db_path, 1, "RED")
    cell2 = grid_cell_id(LAT + CELL_STEP, LON)
    pings = [_defer(ts=NOW), _defer(ts=NOW + 60, lat=LAT + CELL_STEP)]
    _ingest(McIngestor(), 1, pings, NOW)
    assert _tile(db_path) is not None
    assert _tile(db_path, cell2) is None
    assert _stats(db_path)["pings_cell_cap_exceeded"] == 1


def test_real_tx_ping_scores_as_before(db_path):
    _seed_player(db_path, 1, "RED")
    ing = McIngestor()
    _ingest(ing, 1, [_tx(ts=NOW, repeater="cafefeed")], NOW)
    assert _tile(db_path)["owner_team"] == "RED"
    # Same repeater inside cooldown: no second paint (unchanged behaviour).
    _ingest(ing, 1, [_tx(ts=NOW + 30, repeater="cafefeed")], NOW + 30)
    assert _tile(db_path)["paint_count"] == 1
    # A different repeater still scores.
    _ingest(ing, 1, [_tx(ts=NOW + 60, repeater="beefcafe")], NOW + 60)
    assert _tile(db_path)["paint_count"] == 2
    s = _stats(db_path)
    assert s["pings_no_repeaters"] == 0
    assert s["pings_unknown_type"] == 0


def test_real_ping_with_no_repeaters_still_no_signal(db_path):
    _seed_player(db_path, 1, "RED")
    _ingest(McIngestor(), 1, [dict(_tx(), heard_repeats="None", type="RX")], NOW)
    assert _tile(db_path) is None
    assert _stats(db_path)["pings_no_repeaters"] == 1
