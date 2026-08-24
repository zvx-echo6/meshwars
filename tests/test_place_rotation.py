"""Tests for app/place_rotation.py: the weekly rotation draw is
deterministic from week_start alone, and respects the 3-mile minimum
spacing between chosen places (docs/features/places.md).
"""
from __future__ import annotations

import time

from app.grid import distance_m
from app.place_rotation import (
    MIN_SPACING_MILES,
    _compute_week,
    current_week_start,
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
    """
    for i in range(200):
        _insert_place(conn, i, "landmark", 40.0 + (i % 20) * 0.3, -110.0 + (i // 20) * 0.3)

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


def test_three_mile_spacing_enforced(conn):
    """Two candidates 1 mile apart (well under MIN_SPACING_MILES) in the
    same region cell must never both be chosen -- and a set of many
    tightly-clustered candidates should never yield two live picks
    closer than the minimum spacing to each other, checked pairwise
    over the actual result rather than assumed from the algorithm.
    """
    # ~1 mile apart in latitude (1 degree lat ~= 69 miles).
    _insert_place(conn, 1, "landmark", 43.000, -116.000)
    _insert_place(conn, 2, "landmark", 43.0145, -116.000)

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
