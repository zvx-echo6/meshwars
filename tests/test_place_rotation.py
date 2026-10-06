"""Tests for app/place_rotation.py: the weekly rotation draw is
deterministic from week_start alone, and respects MIN_SPACING_MILES's
minimum spacing between chosen places (docs/features/places.md).
"""
from __future__ import annotations

import time

import app.place_rotation as rot_module
from app.grid import distance_m
from app.place_rotation import (
    MIN_SPACING_MILES,
    ROTATION_QUOTA_CAP,
    ROTATION_QUOTA_FLOOR,
    _compute_week,
    _prev_week_start,
    current_week_start,
    ensure_week_resolved,
    live_place_ids,
    region_quota,
    resolve_week,
    week_start_for_date,
    week_start_for_ts,
)

WEEK = "2026-08-19"  # a real Wednesday, matches settings.checkin_net_weekday


def _insert_place(conn, place_id, ref_type, lat, lon, points=5, rotates=1):
    conn.execute(
        "INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, "
        "rotates, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (place_id, ref_type, f"ref-{place_id}", f"place-{place_id}", lat, lon,
         points, "TEST", rotates, int(time.time())),
    )


def _place_grid_in_cell(conn, first_id, lat_idx, lon_idx, rows, cols):
    """Insert rows*cols landmarks, all inside the single region cell
    (lat_idx, lon_idx), on a grid spanning the middle 70% of the cell
    (same technique test_rotation_differs_by_week uses) -- comfortably
    inside the cell's bounds and, for any rows/cols used in this file,
    comfortably past MIN_SPACING_MILES apart, so every one of them
    clears the spacing check and only the quota decides how many go
    live. Returns the ids inserted.
    """
    lat_deg, lon_deg = rot_module._region_cell_degrees()
    cell_south = lat_idx * lat_deg
    cell_west = lon_idx * lon_deg
    lat_step = (lat_deg * 0.7) / max(rows - 1, 1)
    lon_step = (lon_deg * 0.7) / max(cols - 1, 1)
    lat0 = cell_south + lat_deg * 0.15
    lon0 = cell_west + lon_deg * 0.15
    ids = []
    place_id = first_id
    for r in range(rows):
        for c in range(cols):
            _insert_place(conn, place_id, "landmark", lat0 + r * lat_step, lon0 + c * lon_step)
            ids.append(place_id)
            place_id += 1
    return ids


def test_week_start_snaps_to_wednesday():
    # settings.checkin_net_weekday defaults to 2 (Wednesday). Any date
    # in the week of 2026-08-19 (a Wednesday) through the following
    # Tuesday must snap back to that same Wednesday.
    import datetime
    wed = datetime.date(2026, 8, 19)
    for offset in range(7):
        d = wed + datetime.timedelta(days=offset)
        assert week_start_for_date(d) == "2026-08-19"


def test_rotation_is_deterministic_same_week_twice(conn):
    """Two independent computations for the same week_start (no shared
    state, no persistence) must produce the exact same set of chosen
    places -- the whole point of seeding the RNG from week_start alone.
    """
    for i in range(50):
        _insert_place(conn, i, "landmark", 43.0 + i * 0.05, -116.0 + i * 0.05)

    chosen_a, report_a = _compute_week(conn, WEEK)
    chosen_b, report_b = _compute_week(conn, WEEK)

    assert chosen_a == chosen_b
    assert report_a == report_b
    assert len(chosen_a) > 0


def test_rotation_differs_by_week(conn):
    """Sanity check that the draw actually depends on week_start (not a
    constant regardless of input) -- with enough spread-out candidates
    competing across weeks, two different weeks should not draw the
    identical set every single time.

    Each region cell must hold MORE candidates than ROTATION_QUOTA_PER_
    CELL, or there is no actual choice being made (every candidate that
    clears spacing gets picked regardless of week) and the two weeks'
    draws would be identical by construction, not because the algorithm
    is broken -- exactly what raising ROTATION_QUOTA_PER_CELL from 1 to
    5 (2026-08-24, "a town should have more than one place") did to the
    old flat 20x10-grid version of this test, which put only 1-2
    candidates in most cells. Sixteen candidates per cell, spaced ~4
    miles apart (comfortably past MIN_SPACING_MILES) inside five
    well-separated 18-mile cells, keeps this test meaningful regardless
    of what the quota happens to be tuned to later.
    """
    lat_deg, lon_deg = rot_module._region_cell_degrees()
    place_id = 0
    for lat_idx, lon_idx in [(153, -314), (169, -302), (139, -325), (185, -337), (122, -291)]:
        cell_south = lat_idx * lat_deg
        cell_west = lon_idx * lon_deg
        # Grid spans the middle 70% of the cell on each axis, so no
        # point can land outside it regardless of rounding.
        lat_step = (lat_deg * 0.7) / 3
        lon_step = (lon_deg * 0.7) / 3
        lat0 = cell_south + lat_deg * 0.15
        lon0 = cell_west + lon_deg * 0.15
        for r in range(4):
            for c in range(4):
                _insert_place(conn, place_id, "landmark", lat0 + r * lat_step, lon0 + c * lon_step)
                place_id += 1

    chosen_1, _ = _compute_week(conn, "2026-08-19")
    chosen_2, _ = _compute_week(conn, "2026-08-26")
    assert set(chosen_1) != set(chosen_2)


def test_resolve_week_persists_and_is_stable(conn):
    """resolve_week() computes once and caches in place_week -- a
    second call must return the identical, already-persisted result
    without recomputing (and therefore cannot drift even if it were
    called again after some other, unrelated state changed).
    """
    for i in range(30):
        _insert_place(conn, i, "landmark", 43.0 + i * 0.05, -116.0 + i * 0.05)

    first = resolve_week(conn, WEEK)
    stored = [r[0] for r in conn.execute("SELECT place_id FROM place_week WHERE week_start = ?", (WEEK,))]
    second = resolve_week(conn, WEEK)

    assert sorted(first) == sorted(stored)
    assert sorted(first) == sorted(second)


def test_resolve_week_inside_an_open_transaction(conn):
    """credit_places() (app/place_scoring.py) calls resolve_week() from
    inside an already-open write transaction. resolve_week must not try
    to open a second one (SQLite has no nested transactions) -- this
    reproduces that call shape directly.
    """
    _insert_place(conn, 1, "landmark", 43.0, -116.0)
    conn.execute("BEGIN IMMEDIATE")
    try:
        chosen = resolve_week(conn, WEEK)
        assert chosen == [1]
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def test_resolve_week_returns_an_existing_draw_as_stored_without_recomputing(conn):
    """resolve_week()'s contract is unchanged: with a draw already in
    place_week it returns exactly those ids. No `place` rows exist here,
    so a recompute would draw nothing -- getting the planted ids back
    proves the stored draw was returned as it stands.
    """
    conn.executemany(
        "INSERT INTO place_week(week_start, place_id) VALUES (?, ?)",
        [(WEEK, 9), (WEEK, 3), (WEEK, 7)],
    )

    assert sorted(resolve_week(conn, WEEK)) == [3, 7, 9]


# ---- ensure_week_resolved: the one-row probe (2026-10-05) ----------------
#
# resolve_week() is what the places API routes and credit_places() used to
# call, and it fetched the week's whole id list -- about 500,000 rows, ~0.5
# s -- on every call, for both of them to throw it away. They call
# ensure_week_resolved() now. These pin that it never reads more than one
# row of the week it is resolving, and that everything else about
# resolving a week is as it was: an existing draw is left alone, a missing
# one is computed and persisted, last week's picks still count, and a week
# with nothing to draw persists nothing. `counting_conn` (tests/conftest.py)
# records how many rows each statement pulled back.


def test_ensure_week_resolved_reads_one_row_when_the_draw_exists(conn, counting_conn):
    """The common case, and the one that cost half a second: this week's
    draw is already stored, and it is a big one. Resolving it must read
    one row of it, not the lot -- and must leave it exactly as it was.
    """
    planted = list(range(1, 61))
    conn.executemany(
        "INSERT INTO place_week(week_start, place_id) VALUES (?, ?)",
        [(WEEK, pid) for pid in planted],
    )
    # A rotating candidate the stored draw does not contain: if the draw
    # were recomputed and persisted over the top, this is what would show up.
    _insert_place(conn, 500, "landmark", 43.0, -116.0)

    ensure_week_resolved(counting_conn, WEEK)

    reads = counting_conn.week_reads(WEEK)
    assert len(reads) == 1, "one probe, no second look at the week"
    assert reads[0] == 1, "the probe stops at the first row"
    stored = [r[0] for r in conn.execute(
        "SELECT place_id FROM place_week WHERE week_start = ? ORDER BY place_id", (WEEK,)
    )]
    assert stored == planted


def test_ensure_week_resolved_persists_the_computed_draw_when_missing(conn, counting_conn):
    """No draw yet: it is computed and persisted -- the very draw
    _compute_week() produces for that week -- and still without ever
    reading more than one row of the week being resolved.
    """
    for i in range(30):
        _insert_place(conn, i, "landmark", 43.0 + i * 0.05, -116.0 + i * 0.05)
    expected, _report = _compute_week(conn, WEEK)
    assert expected, "the fixture must actually draw something"

    ensure_week_resolved(counting_conn, WEEK)

    stored = [r[0] for r in conn.execute(
        "SELECT place_id FROM place_week WHERE week_start = ?", (WEEK,)
    )]
    assert sorted(stored) == sorted(expected)
    reads = counting_conn.week_reads(WEEK)
    assert reads and max(reads) <= 1


def test_ensure_week_resolved_still_pushes_last_weeks_picks_to_the_back(conn, counting_conn):
    """The earlier-week logic lives in the draw, and is untouched: a cell
    with 48 candidates earns a quota of 27 (see
    test_p90_density_cell_gets_more_than_15_live), 27 of them were drawn
    last week, so the 21 that were NOT must all be drawn this week and
    the remaining 6 slots go to last week's picks. Worked out by hand --
    it does not depend on what the RNG shuffles to.
    """
    ids = _place_grid_in_cell(conn, 0, lat_idx=101, lon_idx=-201, rows=6, cols=8)
    assert len(ids) == 48
    last_week = _prev_week_start(WEEK)
    drawn_last_week, fresh = ids[:27], ids[27:]
    conn.executemany(
        "INSERT INTO place_week(week_start, place_id) VALUES (?, ?)",
        [(last_week, pid) for pid in drawn_last_week],
    )

    ensure_week_resolved(counting_conn, WEEK)

    stored = [r[0] for r in conn.execute(
        "SELECT place_id FROM place_week WHERE week_start = ?", (WEEK,)
    )]
    assert len(stored) == 27
    assert set(fresh) <= set(stored)
    # Reading LAST week's picks is the draw's business; the week being
    # resolved is still only ever probed.
    assert max(counting_conn.week_reads(WEEK)) <= 1


def test_ensure_week_resolved_with_nothing_to_draw_persists_nothing(conn, counting_conn):
    """A week with no rotating candidates at all (only an always-active
    summit exists): nothing is persisted and nothing raises -- same as
    before -- and resolve_week() still reports an empty draw.
    """
    _insert_place(conn, 1, "summit", 43.0, -116.0, points=100, rotates=0)

    ensure_week_resolved(counting_conn, WEEK)
    ensure_week_resolved(counting_conn, WEEK)

    assert conn.execute("SELECT COUNT(*) FROM place_week").fetchone()[0] == 0
    assert counting_conn.week_reads(WEEK) == [0, 0]  # a probe each time, finding nothing
    assert resolve_week(conn, WEEK) == []


def test_ensure_week_resolved_inside_an_open_transaction_rides_along(conn):
    """credit_places() calls this from inside the scoring write
    transaction. It must neither open a second transaction nor commit
    the caller's: the draw it persists is the caller's to commit or to
    roll back.
    """
    _insert_place(conn, 1, "landmark", 43.0, -116.0)

    conn.execute("BEGIN IMMEDIATE")
    ensure_week_resolved(conn, WEEK)
    assert conn.in_transaction, "the caller's transaction must still be open"
    conn.execute("ROLLBACK")
    assert conn.execute("SELECT COUNT(*) FROM place_week").fetchone()[0] == 0

    conn.execute("BEGIN IMMEDIATE")
    ensure_week_resolved(conn, WEEK)
    conn.execute("COMMIT")
    assert [r[0] for r in conn.execute(
        "SELECT place_id FROM place_week WHERE week_start = ?", (WEEK,)
    )] == [1]


# ---- live_place_ids: the one reader that needs the drawn ids ---------------


def test_live_place_ids_is_the_always_active_set_plus_the_active_drawn_set(conn):
    """Always-active (rotates=0) places plus this week's drawn rotating
    places, both restricted to active = 1. Worked out by hand from the
    fixture below, with this week's draw planted so nothing depends on
    the RNG:

      1  always-active, active                    -> live
      2  always-active, deactivated               -> out (left the seed)
      3  rotating, drawn this week, active        -> live
      4  rotating, drawn this week, deactivated   -> out (stale place_week row)
      5  rotating, not drawn this week            -> out
      6  rotating, drawn only LAST week           -> out
    """
    _insert_place(conn, 1, "summit", 43.0, -116.0, points=100, rotates=0)
    _insert_place(conn, 2, "summit", 43.5, -116.5, points=100, rotates=0)
    _insert_place(conn, 3, "landmark", 44.0, -117.0, rotates=1)
    _insert_place(conn, 4, "landmark", 44.5, -117.5, rotates=1)
    _insert_place(conn, 5, "landmark", 45.0, -118.0, rotates=1)
    _insert_place(conn, 6, "landmark", 45.5, -118.5, rotates=1)
    conn.execute("UPDATE place SET active = 0 WHERE id IN (2, 4)")
    conn.executemany(
        "INSERT INTO place_week(week_start, place_id) VALUES (?, ?)",
        [(WEEK, 3), (WEEK, 4), (_prev_week_start(WEEK), 6)],
    )

    assert live_place_ids(conn, WEEK) == {1, 3}


def test_min_spacing_enforced(conn):
    """Two candidates well under MIN_SPACING_MILES apart in the same
    region cell must never both be chosen -- and a set of many
    tightly-clustered candidates should never yield two live picks
    closer than the minimum spacing to each other, checked pairwise
    over the actual result rather than assumed from the algorithm.
    Offsets are computed from MIN_SPACING_MILES itself (not a hardcoded
    distance) so this stays meaningful regardless of what the constant
    is tuned to later.
    """
    # A third of MIN_SPACING_MILES apart in latitude (1 degree lat ~= 69
    # miles) -- comfortably under the limit whatever it is currently set to.
    close_lat_offset = (MIN_SPACING_MILES / 3.0) / 69.0
    _insert_place(conn, 1, "landmark", 43.000, -116.000)
    _insert_place(conn, 2, "landmark", 43.000 + close_lat_offset, -116.000)

    chosen, _ = _compute_week(conn, WEEK)
    assert len(chosen) == 1  # only one of the two can survive spacing

    # A denser cluster: 20 points within a few hundred meters of each
    # other, all candidates for the same slot(s).
    for i in range(10, 30):
        _insert_place(conn, i, "landmark", 43.500 + (i * 0.0005), -116.500 + (i * 0.0005))
    chosen2, _ = _compute_week(conn, WEEK)

    rows = {r["id"]: (r["lat"], r["lon"]) for r in conn.execute(
        "SELECT id, lat, lon FROM place WHERE id IN (%s)" % ",".join("?" * len(chosen2)), chosen2
    )}
    pts = list(rows.values())
    limit_m = MIN_SPACING_MILES * 1609.344
    for a in range(len(pts)):
        for b in range(a + 1, len(pts)):
            d = distance_m(pts[a][0], pts[a][1], pts[b][0], pts[b][1])
            assert d >= limit_m - 1.0, f"two live places only {d:.0f}m apart"


def test_always_active_places_never_rotate(conn):
    """rotates=0 places (summits, boundary-backed parks) must never
    appear in the rotation draw -- only candidates flagged rotates=1
    are eligible at all.
    """
    _insert_place(conn, 1, "summit", 43.0, -116.0, points=100, rotates=0)
    _insert_place(conn, 2, "landmark", 44.0, -117.0, points=5, rotates=1)

    chosen, _ = _compute_week(conn, WEEK)
    assert 1 not in chosen
    assert 2 in chosen


# ---- density-scaled quota (2026-09-07) ---------------------------------


def test_region_quota_floor_never_drops_below_15():
    """Any cell at or under ROTATION_QUOTA_FLOOR candidates gets exactly
    the floor's worth of quota (or less than the floor's worth of actual
    candidates to fill it with) -- density scaling never produces a
    quota below the pre-scaling flat value, for any candidate count from
    zero up through the floor itself.
    """
    for candidates in (1, 2, 4, 10, 14, ROTATION_QUOTA_FLOOR):
        assert region_quota(candidates) == ROTATION_QUOTA_FLOOR
    assert region_quota(0) == 0  # nothing to place either way


def test_region_quota_p90_density_exceeds_the_old_flat_quota():
    """A p90-density cell (48 candidates, per the worldwide-seed
    measurement) must get a quota above the old flat 15 -- 27, per
    quota(48) = round(15 * sqrt(48/15)).
    """
    assert region_quota(48) == 27
    assert region_quota(48) > ROTATION_QUOTA_FLOOR


def test_region_quota_densest_case_capped_at_60_not_334():
    """The measured worldwide max (7,420 candidates in one cell) must be
    capped at ROTATION_QUOTA_CAP (60), not the ~334 an uncapped sqrt
    would compute -- MIN_SPACING_MILES cannot physically seat anywhere
    near that many in one 18-mile cell.
    """
    uncapped = round(ROTATION_QUOTA_FLOOR * (7420 / ROTATION_QUOTA_FLOOR) ** 0.5)
    assert uncapped == 334  # sanity check on the math the cap is guarding against
    assert region_quota(7420) == ROTATION_QUOTA_CAP == 60


def test_sparse_cell_behaves_exactly_as_today(conn):
    """A sparse cell (4 candidates, well under the floor) gets every
    candidate that clears spacing, same as under the old flat-15 quota
    -- density scaling must not change anything here.
    """
    ids = _place_grid_in_cell(conn, 0, lat_idx=100, lon_idx=-200, rows=2, cols=2)
    assert len(ids) == 4

    chosen, report = _compute_week(conn, WEEK)
    assert sorted(chosen) == sorted(ids)
    assert report["100_-200"]["candidates"] == 4
    assert report["100_-200"]["chosen"] == 4


def test_p90_density_cell_gets_more_than_15_live(conn):
    """A p90-density cell (48 well-spaced candidates) must go live with
    more than the old flat 15 -- exactly region_quota(48) == 27, since
    nothing here fails the spacing check.
    """
    ids = _place_grid_in_cell(conn, 0, lat_idx=101, lon_idx=-201, rows=6, cols=8)
    assert len(ids) == 48

    chosen, report = _compute_week(conn, WEEK)
    assert report["101_-201"]["candidates"] == 48
    assert report["101_-201"]["chosen"] == 27
    assert len(chosen) == 27
    assert set(chosen).issubset(set(ids))


def test_spacing_still_wins_over_a_higher_quota(conn):
    """Quota is a ceiling, never a target to pad out to: a cell with
    enough candidates to earn a quota above 15 (20 candidates ->
    region_quota(20) == 17) but almost all of them clustered within
    MIN_SPACING_MILES of each other must still come away with far fewer
    live places than its quota -- spacing keeps deciding, density
    scaling does not override it.
    """
    assert region_quota(20) == 17
    for i in range(20):
        _insert_place(conn, i, "landmark", 43.500 + (i * 0.0005), -116.500 + (i * 0.0005))

    chosen, report = _compute_week(conn, WEEK)
    key = list(report.keys())[0]
    assert report[key]["candidates"] == 20
    assert report[key]["chosen"] < 17
    assert len(chosen) < 17


def test_density_scaling_is_deterministic_across_runs(conn):
    """The same week, resolved twice from scratch, must produce the
    identical live set and region report for a dense cell -- density
    scaling must not introduce any new source of non-determinism (it is
    a pure function of len(candidates), computed without touching the
    RNG).
    """
    _place_grid_in_cell(conn, 0, lat_idx=102, lon_idx=-202, rows=6, cols=8)

    chosen_a, report_a = _compute_week(conn, WEEK)
    chosen_b, report_b = _compute_week(conn, WEEK)

    assert chosen_a == chosen_b
    assert report_a == report_b

