"""Tests for app/place_scoring.py: the weekly 100-point cap, one credit
per reference per person per week, aircraft exclusion, and rotation
gating (docs/features/places.md).
"""
from __future__ import annotations

import time

from app.grid import cell_id
from app.place_rotation import week_start_for_ts
from app.place_scoring import WEEKLY_CAP_POINTS, credit_places

NOW = int(time.time())
WEEK = week_start_for_ts(NOW)


def _place(conn, place_id, ref_type, lat, lon, points, rotates=0, active=1):
    conn.execute(
        "INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, "
        "rotates, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (place_id, ref_type, f"ref-{place_id}", f"place-{place_id}", lat, lon,
         points, "TEST", rotates, active, NOW),
    )
    cid = cell_id(lat, lon)
    conn.execute("INSERT INTO place_cell(place_id, cell_id) VALUES (?, ?)", (place_id, cid))
    return cid


def test_credits_a_summit_and_caps_at_100(conn):
    cid = _place(conn, 1, "summit", 43.0, -116.0, points=100)
    credited = credit_places(conn, player_id=1, cell_id=cid, ts=NOW, repeater_ids=["r1"])
    assert credited == [(1, 100)]

    total = conn.execute(
        "SELECT SUM(points) FROM place_activation WHERE player_id = ? AND week_start = ?",
        (1, WEEK),
    ).fetchone()[0]
    assert total == WEEKLY_CAP_POINTS


def test_landmarks_do_not_exceed_weekly_cap(conn):
    """20 landmarks at 5 points each sums exactly to the cap; a 21st
    must not credit anything and must not create a row -- landing
    exactly on zero remaining budget is the one case with nothing left
    to clamp down to.
    """
    cell_ids = [_place(conn, i, "landmark", 43.0 + i * 0.01, -116.0 + i * 0.01, points=5)
                for i in range(1, 22)]

    total_credited = 0
    for i, cid in enumerate(cell_ids, start=1):
        credited = credit_places(conn, player_id=2, cell_id=cid, ts=NOW, repeater_ids=["r1"])
        total_credited += sum(pts for _, pts in credited)

    assert total_credited == WEEKLY_CAP_POINTS
    total = conn.execute(
        "SELECT SUM(points) FROM place_activation WHERE player_id = ? AND week_start = ?",
        (2, WEEK),
    ).fetchone()[0]
    assert total == WEEKLY_CAP_POINTS
    count = conn.execute(
        "SELECT COUNT(*) FROM place_activation WHERE player_id = ? AND week_start = ?",
        (2, WEEK),
    ).fetchone()[0]
    assert count == 20  # the 21st place created no row for its zero-point non-credit


def test_one_credit_per_reference_per_week(conn):
    """Painting the same place's cell twice in the same week must only
    credit it once."""
    cid = _place(conn, 1, "landmark", 43.0, -116.0, points=5)
    first = credit_places(conn, player_id=3, cell_id=cid, ts=NOW, repeater_ids=["r1"])
    second = credit_places(conn, player_id=3, cell_id=cid, ts=NOW + 60, repeater_ids=["r1"])

    assert first == [(1, 5)]
    assert second == []
    count = conn.execute(
        "SELECT COUNT(*) FROM place_activation WHERE player_id = ? AND place_id = ?", (3, 1)
    ).fetchone()[0]
    assert count == 1


def test_no_signal_ping_credits_nothing(conn):
    cid = _place(conn, 1, "landmark", 43.0, -116.0, points=5)
    credited = credit_places(conn, player_id=4, cell_id=cid, ts=NOW, repeater_ids=[])
    assert credited == []


def test_aircraft_excluded(conn):
    cid = _place(conn, 1, "landmark", 43.0, -116.0, points=5)
    credited = credit_places(conn, player_id=5, cell_id=cid, ts=NOW, repeater_ids=["r1"], by_air=True)
    assert credited == []


def test_rotating_place_only_credits_when_live(conn):
    """A rotates=1 place not chosen for the current week's draw must not
    credit, even though its cell maps to it."""
    cid = _place(conn, 1, "landmark", 43.0, -116.0, points=5, rotates=1)
    # A second, far-away rotating candidate ensures the draw has more
    # than one option in play -- not load-bearing for this test, just
    # realistic.
    _place(conn, 2, "landmark", 10.0, -50.0, points=5, rotates=1)

    # Force place_week for this week to NOT include place 1, simulating
    # "this landmark exists but did not win this week's slot".
    conn.execute("INSERT INTO place_week(week_start, place_id) VALUES (?, ?)", (WEEK, 2))

    credited = credit_places(conn, player_id=6, cell_id=cid, ts=NOW, repeater_ids=["r1"])
    assert credited == []


def test_place_worth_more_than_remaining_cap_is_clamped_to_the_remainder(conn):
    """95 points already earned this week; a 100-point summit does not
    fit whole, so it is credited for the 5 points still left rather
    than refused -- "if you are at 50 points and snag a 100 point peak,
    just cap it out." The activation row itself must record the 5
    actually awarded, not the summit's full 100, so the weekly SUM
    matches what the player received. A further place afterward, with
    the budget now exactly spent, credits nothing and creates no row.
    """
    landmark_cells = [_place(conn, i, "landmark", 43.0 + i * 0.01, -116.0, points=5) for i in range(1, 20)]
    for cid in landmark_cells:
        credit_places(conn, player_id=7, cell_id=cid, ts=NOW, repeater_ids=["r1"])
    total_so_far = conn.execute(
        "SELECT SUM(points) FROM place_activation WHERE player_id = ? AND week_start = ?", (7, WEEK)
    ).fetchone()[0]
    assert total_so_far == 95

    summit_cell = _place(conn, 100, "summit", 50.0, -120.0, points=100)
    credited = credit_places(conn, player_id=7, cell_id=summit_cell, ts=NOW, repeater_ids=["r1"])
    assert credited == [(100, 5)]  # clamped to the remaining 5, not the full 100

    row_points = conn.execute(
        "SELECT points FROM place_activation WHERE player_id = ? AND place_id = ?", (7, 100)
    ).fetchone()[0]
    assert row_points == 5  # the row records what was awarded, not the place's full value

    total_after = conn.execute(
        "SELECT SUM(points) FROM place_activation WHERE player_id = ? AND week_start = ?", (7, WEEK)
    ).fetchone()[0]
    assert total_after == WEEKLY_CAP_POINTS

    # Budget is now exactly spent -- a further place credits nothing
    # and creates no row (it is not consumed for a zero-point activation).
    another_cell = _place(conn, 101, "landmark", 51.0, -121.0, points=5)
    credited2 = credit_places(conn, player_id=7, cell_id=another_cell, ts=NOW, repeater_ids=["r1"])
    assert credited2 == []
    count = conn.execute(
        "SELECT COUNT(*) FROM place_activation WHERE player_id = ? AND place_id = ?", (7, 101)
    ).fetchone()[0]
    assert count == 0


def test_place_larger_than_the_whole_cap_is_clamped_to_100(conn):
    """A single place worth more than the entire weekly cap (a
    synthetic 150-point value -- nothing in the real seed scores that
    high, but credit_places() must not assume points <= 100), hit by a
    player with the full budget still open, is clamped to exactly
    WEEKLY_CAP_POINTS in one activation."""
    cid = _place(conn, 1, "summit", 43.0, -116.0, points=150)
    credited = credit_places(conn, player_id=30, cell_id=cid, ts=NOW, repeater_ids=["r1"])
    assert credited == [(1, WEEKLY_CAP_POINTS)]

    row_points = conn.execute(
        "SELECT points FROM place_activation WHERE player_id = ? AND place_id = ?", (30, 1)
    ).fetchone()[0]
    assert row_points == WEEKLY_CAP_POINTS


def test_partially_credited_place_does_not_pay_again_same_week(conn):
    """A place clamped down this week (only part of its value paid) is
    still fully spent for the week -- the place_activation UNIQUE
    constraint gates on (place_id, player_id, week_start), not on
    whether a prior credit was full or partial, so revisiting it again
    the same week earns nothing more. (Whether it should be reclaimable
    for the remainder in a LATER week is Matt's call, not decided by
    this test -- see docs/features/places.md and the module docstring;
    this test only pins down same-week behaviour, which the simpler
    reading already makes unambiguous.)
    """
    landmark_cells = [_place(conn, i, "landmark", 43.0 + i * 0.01, -116.0, points=5) for i in range(1, 20)]
    for cid in landmark_cells:
        credit_places(conn, player_id=31, cell_id=cid, ts=NOW, repeater_ids=["r1"])
    assert conn.execute(
        "SELECT SUM(points) FROM place_activation WHERE player_id = ? AND week_start = ?",
        (31, WEEK)).fetchone()[0] == 95

    summit_cell = _place(conn, 100, "summit", 50.0, -120.0, points=100)
    first = credit_places(conn, player_id=31, cell_id=summit_cell, ts=NOW, repeater_ids=["r1"])
    assert first == [(100, 5)]

    second = credit_places(conn, player_id=31, cell_id=summit_cell, ts=NOW + 3600, repeater_ids=["r1"])
    assert second == []
    count = conn.execute(
        "SELECT COUNT(*) FROM place_activation WHERE player_id = ? AND place_id = ?", (31, 100)
    ).fetchone()[0]
    assert count == 1


def test_inactive_place_cannot_be_scored(conn):
    """A place that has left the seed (app/places_seed.py's reconcile
    pass sets active=0, never deletes) must not credit even though its
    place_cell row still maps the painted cell to it -- the same stale-
    row situation a real seed reload leaves behind.
    """
    cid = _place(conn, 1, "summit", 43.0, -116.0, points=100, active=0)
    credited = credit_places(conn, player_id=8, cell_id=cid, ts=NOW, repeater_ids=["r1"])
    assert credited == []
    count = conn.execute(
        "SELECT COUNT(*) FROM place_activation WHERE place_id = ?", (1,)
    ).fetchone()[0]
    assert count == 0


# ---------------------------------------------------------------------
# Non-stacking (2026-08-27): a cell that maps to several live places
# credits ONLY the highest-value one. The lesser places are dropped
# outright -- not paid alongside it, and not a fallback when the winner
# cannot be paid. See app/place_scoring.py's module docstring.
# ---------------------------------------------------------------------

def _place_on(conn, place_id, ref_type, cid_lat, cid_lon, points, rotates=0, active=1):
    """Same as _place(), but the caller supplies coordinates directly so
    two places can be planted on the SAME grid cell (identical lat/lon
    is the simplest way to guarantee that)."""
    return _place(conn, place_id, ref_type, cid_lat, cid_lon, points,
                  rotates=rotates, active=active)


def test_overlapping_places_credit_only_the_highest(conn):
    """A landmark standing inside a big park: one ping, one credit, the
    park's. Under the old stacking behaviour this returned BOTH."""
    cid = _place_on(conn, 1, "park", 43.0, -116.0, points=25)
    cid2 = _place_on(conn, 2, "landmark", 43.0, -116.0, points=10)
    assert cid == cid2, "both places must land on the same cell for this test"

    credited = credit_places(conn, player_id=20, cell_id=cid, ts=NOW, repeater_ids=["r1"])
    assert credited == [(1, 25)]

    rows = conn.execute(
        "SELECT place_id, points FROM place_activation WHERE player_id = ?", (20,)
    ).fetchall()
    assert [tuple(r) for r in rows] == [(1, 25)]


def test_equal_points_tiebreak_is_stable_and_not_insertion_order(conn):
    """Two places of EQUAL value on one cell: exactly one credits, and
    which one is decided by _stable_tiebreak's hash of the id -- not by
    the id itself and not by insertion order.

    The ids are chosen so those three answers disagree: place 1 is
    inserted first and has the lower id, but hashes HIGHER
    ((1*2654435761) % 1000000007 = 654435747 vs 308871487 for id 2), so
    the hash orders them 2-then-1. A pass here means the tiebreak really
    is the hash.
    """
    assert (1 * 2654435761) % 1000000007 > (2 * 2654435761) % 1000000007

    cid = _place_on(conn, 1, "landmark", 43.5, -116.5, points=10)
    cid2 = _place_on(conn, 2, "landmark", 43.5, -116.5, points=10)
    assert cid == cid2

    credited = credit_places(conn, player_id=21, cell_id=cid, ts=NOW, repeater_ids=["r1"])
    assert credited == [(2, 10)]

    # Deterministic across runs: a fresh player on the same cell, and a
    # repeat call, must resolve to the same winner every time.
    for pid in (22, 23, 24):
        assert credit_places(conn, player_id=pid, cell_id=cid, ts=NOW,
                             repeater_ids=["r1"]) == [(2, 10)]


def test_lesser_place_is_not_a_fallback_when_the_winner_is_clamped(conn):
    """95 points already spent this week. The cell's winner is a
    100-point summit, which is clamped to the remaining 5 -- and the
    5-point landmark sharing the cell must NOT ALSO credit its own 5
    on top of that. The winner alone is on the table; the lesser place
    is never queried once non-stacking has picked a winner, clamped or
    not.
    """
    for i in range(1, 20):
        cid = _place(conn, i, "landmark", 43.0 + i * 0.01, -116.0, points=5)
        credit_places(conn, player_id=25, cell_id=cid, ts=NOW, repeater_ids=["r1"])
    assert conn.execute(
        "SELECT SUM(points) FROM place_activation WHERE player_id = ? AND week_start = ?",
        (25, WEEK)).fetchone()[0] == 95

    shared = _place_on(conn, 100, "summit", 45.0, -114.0, points=100)
    _place_on(conn, 101, "landmark", 45.0, -114.0, points=5)

    assert credit_places(conn, player_id=25, cell_id=shared, ts=NOW,
                         repeater_ids=["r1"]) == [(100, 5)]
    # Only the winner (100) credited, clamped to 5 -- the lesser place
    # (101) never gets a row of its own.
    assert [tuple(r) for r in conn.execute(
        "SELECT place_id, points FROM place_activation WHERE player_id = ? AND place_id IN (100, 101)",
        (25,)).fetchall()] == [(100, 5)]
    assert conn.execute(
        "SELECT SUM(points) FROM place_activation WHERE player_id = ? AND week_start = ?",
        (25, WEEK)).fetchone()[0] == WEEKLY_CAP_POINTS


def test_revisiting_a_cell_does_not_fall_through_to_the_lesser_place(conn):
    """The winner was already credited this week, so the cell is spent
    for the week -- the cheaper place on it does not step in for a
    second payout."""
    cid = _place_on(conn, 1, "park", 44.0, -115.0, points=25)
    _place_on(conn, 2, "landmark", 44.0, -115.0, points=10)

    first = credit_places(conn, player_id=26, cell_id=cid, ts=NOW, repeater_ids=["r1"])
    second = credit_places(conn, player_id=26, cell_id=cid, ts=NOW + 3600, repeater_ids=["r1"])
    assert first == [(1, 25)]
    assert second == []
    assert conn.execute(
        "SELECT COUNT(*) FROM place_activation WHERE player_id = ?", (26,)
    ).fetchone()[0] == 1


def test_existing_stacked_history_is_never_rewritten(conn):
    """Rows written under the old stacking behaviour -- both places on
    one cell credited in the same past week -- stay exactly as they are.
    This change is forward-only: credit_places never updates or deletes
    a place_activation row, so past scores and frozen months cannot
    move under a player.
    """
    cid = _place_on(conn, 1, "park", 44.5, -115.5, points=25)
    _place_on(conn, 2, "landmark", 44.5, -115.5, points=10)

    old_ts = NOW - 7 * 86400
    old_week = week_start_for_ts(old_ts)
    assert old_week != WEEK
    for place_id, points in ((1, 25), (2, 10)):
        conn.execute(
            "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at) "
            "VALUES (?, ?, ?, ?, ?)", (place_id, 27, old_week, points, old_ts))
    before = [tuple(r) for r in conn.execute(
        "SELECT place_id, player_id, week_start, points, awarded_at FROM place_activation "
        " WHERE week_start = ? ORDER BY place_id", (old_week,)).fetchall()]
    assert before == [(1, 27, old_week, 25, old_ts), (2, 27, old_week, 10, old_ts)]

    # A fresh visit under the new rule, this week.
    assert credit_places(conn, player_id=27, cell_id=cid, ts=NOW, repeater_ids=["r1"]) == [(1, 25)]

    after = [tuple(r) for r in conn.execute(
        "SELECT place_id, player_id, week_start, points, awarded_at FROM place_activation "
        " WHERE week_start = ? ORDER BY place_id", (old_week,)).fetchall()]
    assert after == before, "historic activation rows must not change"
