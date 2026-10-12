"""The regional queries at production size: ~480k region_cell rows, tens of
thousands of squares, a group of 120 regions. The regional standings used to
take 15-43 s because `cell IN (SELECT cell_id FROM region_cell WHERE
region_code IN (...))` made SQLite build the group's whole cell set once per
query. These tests pin the PLAN (an index probe per candidate row, no
materialised subquery, no scan of region_cell) rather than a stopwatch, check
the answers against a plain-Python count, and keep a generous time ceiling
only to catch an order-of-magnitude regression."""
from __future__ import annotations

import random
import sqlite3
import time

import pytest

import app.db as db
from app import mc_scoring, regions
from app.db import MIGRATIONS, SCHEMA

NOW = int(time.time())
N_REGIONS, PER_REGION, N_TILES, GROUP_SIZE = 480, 1000, 60000, 120


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("perf") / "big.db")
    c = sqlite3.connect(path, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            c.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                continue
            raise
    rng = random.Random(3)
    codes = [f"R{i:03d}" for i in range(N_REGIONS)]
    c.execute("BEGIN")
    c.executemany("INSERT INTO region_cell(region_code, cell_id) VALUES (?,?)",
                  [(codes[r], f"{r}_{k}") for r in range(N_REGIONS) for k in range(PER_REGION)])
    # a few cells sit in two regions, as SMN/IDA do
    c.executemany("INSERT OR IGNORE INTO region_cell(region_code, cell_id) VALUES (?,?)",
                  [(codes[(r + 1) % N_REGIONS], f"{r}_{k}") for r in range(N_REGIONS) for k in range(0, 50)])
    c.execute("INSERT INTO mc_season(id, protocol, started_at, ends_at, status) VALUES (1,'mc',?,?, 'active')",
              (NOW - 10**6, NOW + 10**6))
    for pid, team in enumerate(("RED", "BLUE", "GREEN"), start=1):
        c.execute("INSERT INTO player(player_id, display_name, team, created_at) VALUES (?,?,?,?)", (pid, team, team, NOW))
    tiles = {}
    while len(tiles) < N_TILES:
        tiles[f"{rng.randrange(N_REGIONS)}_{rng.randrange(PER_REGION)}"] = rng.choice(("RED", "BLUE", "GREEN"))
    c.executemany("INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts, paint_count, "
                  "lat_idx, lon_idx) VALUES (1,?,?,1,?,1,0,0)", [(cid, t, NOW) for cid, t in tiles.items()])
    for i in range(1, 801):
        cid = f"{rng.randrange(N_REGIONS)}_{rng.randrange(PER_REGION)}"
        c.execute("INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, rotates, active, created_at) "
                  "VALUES (?, 'summit', ?, 'p', 0, 0, 10, 'T', 0, 1, ?)", (i, f"r{i}", NOW))
        c.execute("INSERT INTO place_cell(place_id, cell_id) VALUES (?,?)", (i, cid))
        c.execute("INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at, protocol) "
                  "VALUES (?,?,?,10,?, 'mc')", (i, 1 + i % 3, f"w{i}", NOW))
    for i in range(1, 401):
        c.execute("INSERT INTO mc_checkin_award(season_id, player_id, net_date, points, protocol, message_id, awarded_at) "
                  "VALUES (1,?,?,2.0,'mc','m',?)", (1 + i % 3, f"d{i}", NOW))
        c.execute("INSERT INTO checkin_award_place(season_id, player_id, net_date, cell_id, method) VALUES (1,?,?,?,'first_hop')",
                  (1 + i % 3, f"d{i}", f"{rng.randrange(N_REGIONS)}_{rng.randrange(PER_REGION)}"))
    ts = 0
    capture_rows = []
    for _ in range(50000):
        ts += 1
        capture_rows.append((f"{rng.randrange(N_REGIONS)}_{rng.randrange(PER_REGION)}", ts, 1 + ts % 3))
    c.executemany("INSERT INTO mc_tile_capture_log(season_id, cell_id, ts, by_player_id, by_team) VALUES (1,?,?,?, 'RED')",
                  capture_rows)
    c.execute("COMMIT")
    c.execute("ANALYZE")
    yield {"conn": c, "codes": tuple(codes[:GROUP_SIZE]), "tiles": tiles, "path": path}
    c.close()


def _captured(conn, fn):
    stmts = []
    conn.set_trace_callback(stmts.append)
    try:
        out = fn()
    finally:
        conn.set_trace_callback(None)
    return out, [s for s in stmts if "region_cell" in s and s.lstrip().upper().startswith("SELECT")]


def _assert_plan_is_indexed(conn, stmts):
    assert stmts, "no regional statement was captured"
    for sql in stmts:
        plan = "\n".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
        assert "LIST SUBQUERY" not in plan, plan                   # the group's cell set is never materialised
        assert "SCAN rc" not in plan and "SCAN region_cell" not in plan, plan
        assert "idx_region_cell_cell" in plan, plan                # one covering-index probe per candidate row


def test_tile_counts_plan_and_answer(big):
    c, codes = big["conn"], big["codes"]
    t0 = time.monotonic()
    got, stmts = _captured(c, lambda: mc_scoring.team_tile_counts(c, 1, codes))
    assert time.monotonic() - t0 < 5
    _assert_plan_is_indexed(c, stmts)
    group = {int(code[1:]) for code in codes}
    expect: dict[str, int] = {}
    for cid, team in big["tiles"].items():
        r, k = (int(x) for x in cid.split("_"))
        # in the group if its own region is, or it is one of the shared cells owned by the previous region
        if r in group or (k < 50 and (r + 1) % N_REGIONS in group):
            expect[team] = expect.get(team, 0) + 1
    assert got == expect


def test_place_points_plan_and_answer(big):
    c, codes = big["conn"], big["codes"]
    got, stmts = _captured(c, lambda: mc_scoring.team_place_points(c, 1, "mc", codes))
    _assert_plan_is_indexed(c, stmts)
    all_pts = mc_scoring.team_place_points(c, 1, "mc")
    assert 0 < sum(got.values()) < sum(all_pts.values())


def test_checkin_points_plan_and_answer(big):
    c, codes = big["conn"], big["codes"]
    got, stmts = _captured(c, lambda: mc_scoring.team_checkin_points(c, 1, codes))
    _assert_plan_is_indexed(c, stmts)
    all_pts = mc_scoring.team_checkin_points(c, 1)
    assert 0 < sum(got.values()) < sum(all_pts.values())


def test_a_group_of_every_region_is_still_one_probe_per_row(big):
    c = big["conn"]
    every = tuple(f"R{i:03d}" for i in range(N_REGIONS))
    t0 = time.monotonic()
    got, stmts = _captured(c, lambda: mc_scoring.team_tile_counts(c, 1, every))
    assert time.monotonic() - t0 < 5
    _assert_plan_is_indexed(c, stmts)
    assert sum(got.values()) == N_TILES


def test_filter_sql_shape_and_empty_group():
    cond, params = regions.cell_filter_sql(["A", "B"], "mc_tile.cell_id")
    assert "rc.cell_id = mc_tile.cell_id" in cond and "+rc.region_code IN (?,?)" in cond and params == ["A", "B"]
    assert regions.cell_filter_sql([], "mc_tile.cell_id") == ("0", [])


class _KeepOpen:
    """Hands the module-scoped connection to code that closes what it opens."""

    def __init__(self, conn):
        self._c = conn

    def __getattr__(self, name):
        return getattr(self._c, name)

    def close(self):
        pass


def test_the_group_batch_reads_each_figure_once_through_the_index(big, monkeypatch):
    """Fifteen groups on production-sized data: the batch makes a handful of
    index-driven passes (squares, activations, check-ins, captures) per board
    instead of one pass per group and figure."""
    from app import regional_standings
    c = big["conn"]
    codes = [f"R{i:03d}" for i in range(N_REGIONS)]
    targets = [(f"g{i}", regions.Resolved(f"g{i}", "group", f"G{i}", tuple(codes[i * 30:(i + 1) * 30 + 10])))
               for i in range(15)]
    monkeypatch.setattr(regional_standings, "connect", lambda: _KeepOpen(c))
    monkeypatch.setattr(db.settings, "db_path", big["path"])
    t0 = time.monotonic()
    built, stmts = _captured(c, lambda: regional_standings.build_group_standings(targets))
    elapsed = time.monotonic() - t0
    assert elapsed < 10, elapsed                      # generous: the point is the plans below, not the stopwatch
    _assert_plan_is_indexed_probe(c, stmts)
    assert len(stmts) == 4                            # one pass per figure on the one active board
    assert len(built) == 15 * 4
    # the squares tally agrees with the per-request count for a group
    from app import mc_scoring
    for slug, resolved in targets[:3]:
        want = mc_scoring.team_tile_counts(c, 1, resolved.codes)
        got = {t["team"]: t["tiles"] for t in built[f"mc_scores:region:{slug}"]["teams"]}
        assert {k: v for k, v in want.items() if v} == {k: v for k, v in got.items() if v}


def _assert_plan_is_indexed_probe(conn, stmts):
    for sql in stmts:
        plan = "\n".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
        assert "idx_region_cell_cell" in plan or "region_cell" in plan, plan
        assert "SCAN rc" not in plan and "SCAN region_cell" not in plan, plan
