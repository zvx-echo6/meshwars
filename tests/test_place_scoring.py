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
    must not credit anything -- the cap must never be exceeded, and a
    place whose full value does not fit the remainder is skipped
    entirely rather than partially credited.
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


def test_place_worth_more_than_remaining_cap_is_skipped_not_partial(conn):
    """95 points already earned this week; a 100-point summit does not
    fit (would exceed the cap) and must be skipped entirely -- no
    partial credit -- while a 5-point landmark that DOES fit still
    credits."""
    landmark_cells = [_place(conn, i, "landmark", 43.0 + i * 0.01, -116.0, points=5) for i in range(1, 20)]
    for cid in landmark_cells:
        credit_places(conn, player_id=7, cell_id=cid, ts=NOW, repeater_ids=["r1"])
    total_so_far = conn.execute(
        "SELECT SUM(points) FROM place_activation WHERE player_id = ? AND week_start = ?", (7, WEEK)
    ).fetchone()[0]
    assert total_so_far == 95

    summit_cell = _place(conn, 100, "summit", 50.0, -120.0, points=100)
    credited = credit_places(conn, player_id=7, cell_id=summit_cell, ts=NOW, repeater_ids=["r1"])
    assert credited == []  # 100 does not fit in the remaining 5

    fits_cell = _place(conn, 101, "landmark", 51.0, -121.0, points=5)
    credited2 = credit_places(conn, player_id=7, cell_id=fits_cell, ts=NOW, repeater_ids=["r1"])
    assert credited2 == [(101, 5)]


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
