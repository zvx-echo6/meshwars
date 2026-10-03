"""Tests for MeshMapper "DEFER" items posted to POST /api/mc/ingest.

A DEFER ({"type": "DEFER", lat, lon, timestamp, held, radio_freq, contact,
iata}) carries no heard_repeats / repeater_id. It is a recognized type and
scores what the most recent scored REAL ping in the same cell (any player)
earned, read from mc_cell_last_score (never pruned), via apply_paint's flat
mode; a cell with no such ping gives 0 / no paint / "no_signal". It has the
same per-cell cooldown (mc_cooldown_seconds) real pings have, the same
cell-claim cap, and place credit. It records no repeater observation and
bumps neither pings_no_repeaters nor pings_unknown_type.

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


def _heard(n, prefix=0):
    return ",".join(f"{prefix * 100 + i:08x}(3.5)" for i in range(n))


def _real(n, ts=NOW, lat=LAT, lon=LON, contact="deadbeef", prefix=0):
    return {
        "type": "TX", "contact": contact, "lat": lat, "lon": lon,
        "timestamp": ts, "heard_repeats": _heard(n, prefix),
    }


P1, P2, P3, P4 = ("aaaaaaa1", "aaaaaaa2", "aaaaaaa3", "aaaaaaa4")
BONUS = settings.mc_score_per_unique_player


def _team_score(db_path, team, cell=CELL):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT score FROM mc_tile_score WHERE cell_id = ? AND team = ?", (cell, team)).fetchone()
    conn.close()
    return None if row is None else row[0]


def _last_score(db_path, cell=CELL):
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT points, ts, player_id FROM mc_cell_last_score WHERE protocol = 'mc' AND cell_id = ?",
        (cell,),
    ).fetchone()
    conn.close()
    return row


def _spy_outcomes(monkeypatch):
    from app import mc_ingest
    seen = []
    real = mc_ingest.credit_places

    def spy(conn, player_id, cell, ts, outcome, *a, **k):
        seen.append(outcome)
        return real(conn, player_id, cell, ts, outcome, *a, **k)

    monkeypatch.setattr(mc_ingest, "credit_places", spy)
    return seen


@pytest.fixture
def three(db_path):
    """RED p1, BLUE p2, GREEN p3, YELLOW p4 and a shared ingestor."""
    for pid, team in ((1, "RED"), (2, "BLUE"), (3, "GREEN"), (4, "YELLOW")):
        _seed_player(db_path, pid, team)
    return McIngestor()


def _ing_as(ing, pid, pings, received_at):
    _ingest(ing, pid, pings, received_at)


@pytest.mark.parametrize("n,expected", [(1, 0.1), (3, 0.3), (12, 1.0)])
def test_defer_scores_what_another_players_last_real_ping_in_cell_earned(db_path, three, n, expected):
    _ing_as(three, 1, [_real(n, contact=P1)], NOW)
    t = NOW + 30
    _ing_as(three, 2, [_defer(ts=t, contact=P2)], t)
    assert _team_score(db_path, "BLUE") == pytest.approx(expected + BONUS)


def test_defer_ignores_own_history_elsewhere(db_path, three):
    other = grid_cell_id(LAT + CELL_STEP, LON)
    _ing_as(three, 1, [_real(1, contact=P1)], NOW)  # cell A: 0.1
    # Player 2's own real ping, in another cell B, earned 0.8 -- irrelevant.
    _ing_as(three, 2, [_real(8, ts=NOW + 10, lat=LAT + CELL_STEP, contact=P2, prefix=2)], NOW + 10)
    t = NOW + 400
    _ing_as(three, 2, [_defer(ts=t, contact=P2)], t)
    assert _team_score(db_path, "BLUE", CELL) == pytest.approx(0.1 + BONUS)
    assert _tile(db_path, other)["owner_team"] == "BLUE"


def test_newer_real_ping_replaces_value_and_older_ts_does_not(db_path, three):
    _ing_as(three, 1, [_real(1, ts=NOW, contact=P1)], NOW + 500)
    assert _last_score(db_path)[0] == pytest.approx(0.1)
    _ing_as(three, 3, [_real(3, ts=NOW + 400, contact=P3, prefix=3)], NOW + 500)
    assert _last_score(db_path) == (pytest.approx(0.3), NOW + 400, 3)
    # Out-of-order offline upload: older ts must not overwrite.
    _ing_as(three, 4, [_real(2, ts=NOW + 100, contact=P4, prefix=4)], NOW + 500)
    assert _last_score(db_path) == (pytest.approx(0.3), NOW + 400, 3)
    _ing_as(three, 2, [_defer(ts=NOW + 450, contact=P2)], NOW + 500)
    assert _team_score(db_path, "BLUE") == pytest.approx(0.3 + BONUS)


def test_defers_and_zero_point_pings_never_write_last_score(db_path, three):
    # DEFER alone in an empty cell
    _ing_as(three, 2, [_defer(contact=P2)], NOW)
    assert _last_score(db_path) is None
    # no_signal real ping
    _ing_as(three, 1, [dict(_real(1, contact=P1), type="RX", heard_repeats="None")], NOW + 5)
    assert _last_score(db_path) is None
    # real ping, then a cooldown repeat (same repeater) and a DEFER: row unchanged
    _ing_as(three, 1, [_real(2, ts=NOW + 10, contact=P1)], NOW + 10)
    row = _last_score(db_path)
    assert row == (pytest.approx(0.2), NOW + 10, 1)
    _ing_as(three, 1, [_real(2, ts=NOW + 40, contact=P1)], NOW + 40)  # all repeated: cooldown
    _ing_as(three, 2, [_defer(ts=NOW + 50, contact=P2)], NOW + 50)
    assert _last_score(db_path) == row


def test_last_score_survives_housekeeping_pruning_credit_rows(db_path, three):
    _ing_as(three, 1, [_real(3, contact=P1)], NOW)
    assert _count(db_path, "SELECT COUNT(*) FROM player_cell_repeater_credit WHERE repeater_id != 'DEFER'") == 3
    three._housekeeping_sync()  # real clock is far past NOW + 48h
    assert _count(db_path, "SELECT COUNT(*) FROM player_cell_repeater_credit") == 0
    assert _last_score(db_path) == (pytest.approx(0.3), NOW, 1)
    t = NOW + 60
    _ing_as(three, 2, [_defer(ts=t, contact=P2)], t)
    assert _team_score(db_path, "BLUE") == pytest.approx(0.3 + BONUS)


def test_defer_in_cell_with_no_scored_ping_earns_nothing(db_path, three, monkeypatch):
    seen = _spy_outcomes(monkeypatch)
    _ing_as(three, 2, [_defer(contact=P2)], NOW)
    assert _tile(db_path) is None
    assert seen == ["no_signal"]
    s = _stats(db_path, 2)
    assert s["pings_accepted"] == 1
    assert s["pings_no_repeaters"] == 0
    assert s["pings_unknown_type"] == 0
    assert _count(db_path, "SELECT COUNT(*) FROM player_cell_repeater_credit") == 0  # no sentinel
    assert _count(db_path, "SELECT COUNT(*) FROM player_cell_claim") == 0  # no cap slot used
    assert _count(db_path, "SELECT COUNT(*) FROM place_activation") == 0


def test_real_ping_in_a_different_cell_is_ignored(db_path, three):
    _ing_as(three, 1, [_real(5, lat=LAT + CELL_STEP, contact=P1)], NOW)
    _ing_as(three, 2, [_defer(ts=NOW + 30, contact=P2)], NOW + 30)
    assert _tile(db_path) is None


def test_defer_flip_follows_the_borrowed_points(db_path, three):
    _ing_as(three, 1, [_real(1, contact=P1)], NOW)  # RED: 0.1 + 0.5 bonus
    t1 = NOW + 60
    _ing_as(three, 2, [_defer(ts=t1, contact=P2)], t1)  # BLUE 0.6: inside defense window
    assert _tile(db_path)["owner_team"] == "RED"
    t2 = NOW + settings.mc_defense_window_seconds + 400
    _ing_as(three, 2, [_defer(ts=t2, contact=P2)], t2)  # BLUE 0.7 > RED decayed (<= 0.6)
    assert _tile(db_path)["owner_team"] == "BLUE"


def test_defer_credits_places(db_path, three):
    _seed_place(db_path, 1, points=10)
    _ing_as(three, 1, [_real(2, contact=P1)], NOW)
    _ing_as(three, 2, [_defer(ts=NOW + 30, contact=P2)], NOW + 30)
    assert _count(db_path, "SELECT COUNT(*) FROM place_activation WHERE player_id = 2") == 1


def test_defer_does_not_bump_no_repeaters_or_unknown_type(db_path, three):
    _ing_as(three, 1, [_real(2, contact=P1)], NOW)
    _ing_as(three, 2, [_defer(ts=NOW + 30, contact=P2)], NOW + 30)
    s = _stats(db_path, 2)
    assert s["pings_accepted"] == 1
    assert s["pings_no_repeaters"] == 0
    assert s["pings_unknown_type"] == 0


def test_defer_writes_no_repeater_observations(db_path, three):
    _ing_as(three, 1, [_real(2, contact=P1)], NOW)
    before = _count(db_path, "SELECT COUNT(*) FROM repeater_observation")
    _ing_as(three, 2, [_defer(ts=NOW + 30, contact=P2)], NOW + 30)
    assert _count(db_path, "SELECT COUNT(*) FROM repeater_observation") == before


def test_defer_cooldown_then_scores_again(db_path, three, monkeypatch):
    _ing_as(three, 1, [_real(2, contact=P1)], NOW)
    seen = _spy_outcomes(monkeypatch)
    _ing_as(three, 2, [_defer(ts=NOW + 30, contact=P2)], NOW + 30)
    first = _team_score(db_path, "BLUE")
    assert first == pytest.approx(0.2 + BONUS)
    t = NOW + 60
    _ing_as(three, 2, [_defer(ts=t, contact=P2)], t)  # inside cooldown
    assert seen[-1] == "cooldown"
    assert _team_score(db_path, "BLUE") == pytest.approx(first)
    t = NOW + 30 + settings.mc_cooldown_seconds + 5
    _ing_as(three, 2, [_defer(ts=t, contact=P2)], t)
    assert seen[-1] != "cooldown"
    assert _team_score(db_path, "BLUE") == pytest.approx(first + 0.2, abs=0.05)  # allow tiny decay


def test_defer_respects_cell_claim_cap(db_path, three, monkeypatch):
    cell_b = grid_cell_id(LAT + CELL_STEP, LON)
    _ing_as(three, 1, [_real(2, contact=P1), _real(2, ts=NOW + 120, lat=LAT + CELL_STEP, contact=P1, prefix=5)], NOW + 120)
    monkeypatch.setattr(settings, "mc_cell_claim_cap", 1)
    monkeypatch.setattr(settings, "mc_cell_claim_cap_window_seconds", 3600)
    pings = [_defer(ts=NOW + 200, contact=P2), _defer(ts=NOW + 400, lat=LAT + CELL_STEP, contact=P2)]
    _ing_as(three, 2, pings, NOW + 400)
    assert _team_score(db_path, "BLUE", CELL) is not None
    assert _team_score(db_path, "BLUE", cell_b) is None
    assert _stats(db_path, 2)["pings_cell_cap_exceeded"] == 1


def test_seed_populates_from_existing_credit_rows_and_is_idempotent(db_path):
    from app import db as appdb
    seed = appdb.MIGRATIONS[-1]
    assert "mc_cell_last_score" in seed
    conn = sqlite3.connect(db_path)
    rows = (
        # cell X: p1 older group; p2 and p3 tie at ts 200 (lowest player_id wins)
        [(1, "mc", "X", r, 100, 100) for r in "ab"]
        + [(2, "mc", "X", r, 200, 200) for r in "cde"]
        + [(3, "mc", "X", r, 200, 200) for r in "fghi"]
        + [(1, "mc", "X", "DEFER", 300, 300)]      # sentinel ignored
        + [(1, "mt", "X", r, 400, 400) for r in "jk"]  # other protocol ignored
        # cell Y: 15 repeaters -> capped at max
        + [(1, "mc", "Y", f"r{i:02d}", 50, 50) for i in range(15)]
    )
    conn.executemany("INSERT INTO player_cell_repeater_credit VALUES (?,?,?,?,?,?)", rows)
    conn.execute(seed)
    got = {r[0]: r[1:] for r in conn.execute(
        "SELECT cell_id, points, ts, player_id FROM mc_cell_last_score WHERE protocol='mc'")}
    assert got["X"] == (pytest.approx(0.3), 200, 2)
    assert got["Y"] == (pytest.approx(settings.mc_max_points_per_ping), 50, 1)
    assert set(got) == {"X", "Y"}
    # Re-running never overwrites a live row.
    conn.execute("UPDATE mc_cell_last_score SET points = 9, ts = 999 WHERE cell_id = 'X'")
    conn.execute(seed)
    assert conn.execute("SELECT points, ts FROM mc_cell_last_score WHERE cell_id='X'").fetchone() == (9, 999)
    assert conn.execute("SELECT COUNT(*) FROM mc_cell_last_score").fetchone()[0] == 2
    conn.close()


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
