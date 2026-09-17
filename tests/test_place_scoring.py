"""Tests for app/place_scoring.py: the weekly 100-point cap, one credit
per reference per person per week, aircraft exclusion, and rotation
gating (docs/features/places.md).
"""
from __future__ import annotations

import time

from app.grid import cell_id
from app.place_rotation import week_start_for_ts
from app.place_scoring import WEEKLY_CAP_POINTS, credit_places, qualifying_place_firsts

NOW = int(time.time())
WEEK = week_start_for_ts(NOW)


def _player(conn, player_id, team="RED"):
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?, ?, ?, ?)",
        (player_id, f"player-{player_id}", team, NOW),
    )


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
    credited = credit_places(conn, player_id=1, cell_id=cid, ts=NOW, paint_outcome="captured")
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
        credited = credit_places(conn, player_id=2, cell_id=cid, ts=NOW, paint_outcome="captured")
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
    first = credit_places(conn, player_id=3, cell_id=cid, ts=NOW, paint_outcome="captured")
    second = credit_places(conn, player_id=3, cell_id=cid, ts=NOW + 60, paint_outcome="captured")

    assert first == [(1, 5)]
    assert second == []
    count = conn.execute(
        "SELECT COUNT(*) FROM place_activation WHERE player_id = ? AND place_id = ?", (3, 1)
    ).fetchone()[0]
    assert count == 1


def test_no_signal_ping_credits_nothing(conn):
    cid = _place(conn, 1, "landmark", 43.0, -116.0, points=5)
    credited = credit_places(conn, player_id=4, cell_id=cid, ts=NOW, paint_outcome="no_signal")
    assert credited == []


def test_aircraft_excluded(conn):
    cid = _place(conn, 1, "landmark", 43.0, -116.0, points=5)
    credited = credit_places(conn, player_id=5, cell_id=cid, ts=NOW, paint_outcome="captured", by_air=True)
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

    credited = credit_places(conn, player_id=6, cell_id=cid, ts=NOW, paint_outcome="captured")
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
        credit_places(conn, player_id=7, cell_id=cid, ts=NOW, paint_outcome="captured")
    total_so_far = conn.execute(
        "SELECT SUM(points) FROM place_activation WHERE player_id = ? AND week_start = ?", (7, WEEK)
    ).fetchone()[0]
    assert total_so_far == 95

    summit_cell = _place(conn, 100, "summit", 50.0, -120.0, points=100)
    credited = credit_places(conn, player_id=7, cell_id=summit_cell, ts=NOW, paint_outcome="captured")
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
    credited2 = credit_places(conn, player_id=7, cell_id=another_cell, ts=NOW, paint_outcome="captured")
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
    credited = credit_places(conn, player_id=30, cell_id=cid, ts=NOW, paint_outcome="captured")
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
        credit_places(conn, player_id=31, cell_id=cid, ts=NOW, paint_outcome="captured")
    assert conn.execute(
        "SELECT SUM(points) FROM place_activation WHERE player_id = ? AND week_start = ?",
        (31, WEEK)).fetchone()[0] == 95

    summit_cell = _place(conn, 100, "summit", 50.0, -120.0, points=100)
    first = credit_places(conn, player_id=31, cell_id=summit_cell, ts=NOW, paint_outcome="captured")
    assert first == [(100, 5)]

    second = credit_places(conn, player_id=31, cell_id=summit_cell, ts=NOW + 3600, paint_outcome="captured")
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
    credited = credit_places(conn, player_id=8, cell_id=cid, ts=NOW, paint_outcome="captured")
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

    credited = credit_places(conn, player_id=20, cell_id=cid, ts=NOW, paint_outcome="captured")
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

    credited = credit_places(conn, player_id=21, cell_id=cid, ts=NOW, paint_outcome="captured")
    assert credited == [(2, 10)]

    # Deterministic across runs: a fresh player on the same cell, and a
    # repeat call, must resolve to the same winner every time.
    for pid in (22, 23, 24):
        assert credit_places(conn, player_id=pid, cell_id=cid, ts=NOW,
                              paint_outcome="captured") == [(2, 10)]


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
        credit_places(conn, player_id=25, cell_id=cid, ts=NOW, paint_outcome="captured")
    assert conn.execute(
        "SELECT SUM(points) FROM place_activation WHERE player_id = ? AND week_start = ?",
        (25, WEEK)).fetchone()[0] == 95

    shared = _place_on(conn, 100, "summit", 45.0, -114.0, points=100)
    _place_on(conn, 101, "landmark", 45.0, -114.0, points=5)

    assert credit_places(conn, player_id=25, cell_id=shared, ts=NOW,
                          paint_outcome="captured") == [(100, 5)]
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

    first = credit_places(conn, player_id=26, cell_id=cid, ts=NOW, paint_outcome="captured")
    second = credit_places(conn, player_id=26, cell_id=cid, ts=NOW + 3600, paint_outcome="captured")
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
    assert credit_places(conn, player_id=27, cell_id=cid, ts=NOW, paint_outcome="captured") == [(1, 25)]

    after = [tuple(r) for r in conn.execute(
        "SELECT place_id, player_id, week_start, points, awarded_at FROM place_activation "
        " WHERE week_start = ? ORDER BY place_id", (old_week,)).fetchall()]
    assert after == before, "historic activation rows must not change"


# ---- Discord: credit_places() never announces anything itself ------------
#
# credit_places() used to enqueue a per-activation "notable activation"
# Discord announcement directly (build_place_activation_embed()/
# place_activation_notability(), app/discord_notify.py) -- removed
# 2026-09-16: too frequent (~373/month) and it announced a player's
# location within minutes of them reaching it. Notable activations are
# now folded into the Sunday weekly recap instead (see this module's own
# qualifying_place_firsts(), tested further below in this file, and
# app/discord_notify.py's weekly_recap_provider(), tested in
# tests/test_discord_notify.py). This is a regression guard, not a
# feature test:
# it proves credit_places() stays silent on discord_outbox even with a
# webhook fully configured and enabled, for exactly the notable shapes
# (first-ever summit) the old per-event announcement used to fire on.


def test_credit_places_never_touches_discord_outbox(conn):
    """A summit activation -- the old per-event announcement's own
    "notable by ref_type alone" case -- must credit the points exactly
    as before, and discord_outbox must stay completely empty regardless
    of whether a webhook is even configured."""
    conn.execute(
        "UPDATE discord_config SET enabled = 1, "
        " webhook_url = 'https://discord.test/api/webhooks/1/x' WHERE id = 1"
    )
    _player(conn, 40)
    cid = _place_on(conn, 1, "summit", 43.5, -116.5, points=100)

    credited = credit_places(conn, player_id=40, cell_id=cid, ts=NOW, paint_outcome="captured")
    assert credited == [(1, 100)]
    assert conn.execute("SELECT * FROM discord_outbox").fetchall() == []


# ---- qualifying_place_firsts() --------------------------------------------
#
# Read-only query, no relation to credit_places() above -- see this
# function's own docstring (HARD PRIVACY WARNING included) for the
# qualifying rule and the season-wide "first" semantics these tests
# exercise directly against place/place_activation/mc_season rows,
# rather than through a real scoring ping.


def _season(conn, season_id, *, protocol="mc", started_at, ends_at):
    conn.execute(
        "INSERT INTO mc_season(id, protocol, started_at, ends_at, status) "
        "VALUES (?, ?, ?, ?, 'active')",
        (season_id, protocol, started_at, ends_at),
    )


def _place_reason(conn, place_id, ref_type, points_reason, points=25):
    """Same shape as this file's own _place() above, but also sets
    points_reason -- qualifying_place_firsts() matches on that column's
    PREFIX, not on ref_type or points alone, so the tests below need to
    control it directly."""
    conn.execute(
        "INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, "
        "points_reason, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,1,?)",
        (place_id, ref_type, f"ref-{place_id}", f"place-{place_id}",
         43.0 + place_id * 0.01, -116.0 + place_id * 0.01, points, "TEST", points_reason, NOW),
    )


def _activation(conn, *, place_id, player_id, awarded_at, week_start=None, points=25, protocol="mc"):
    # week_start defaults to the REAL week awarded_at falls in (not a
    # fixed constant) -- place_activation's own UNIQUE(place_id,
    # player_id, week_start) means two activations of the same place by
    # the same player in the tests below (an "earlier" one and a
    # "this window" one) need two different week_start values whenever
    # they are more than a week apart, exactly like a real credit_places()
    # insert would produce.
    if week_start is None:
        week_start = week_start_for_ts(awarded_at)
    conn.execute(
        "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at, protocol) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (place_id, player_id, week_start, points, awarded_at, protocol),
    )


_SEASON_START = NOW - 1_000_000
_SEASON_END = NOW + 1_000_000
_WINDOW_START = NOW
_WINDOW_END = NOW + 7 * 86400


def _base_season_and_players(conn):
    _season(conn, 1, started_at=_SEASON_START, ends_at=_SEASON_END)
    _player(conn, 50, team="RED")
    _player(conn, 51, team="GREEN")


def test_qualifying_place_firsts_summit_qualifies(conn):
    _base_season_and_players(conn)
    _place_reason(conn, 10, "summit", "remote_scaled", points=80)
    _activation(conn, place_id=10, player_id=50, awarded_at=_WINDOW_START + 10)

    rows = qualifying_place_firsts(conn, protocol="mc", season_id=1,
                                    start_ts=_WINDOW_START, end_ts=_WINDOW_END)
    assert [r["place_id"] for r in rows] == [10]


def test_qualifying_place_firsts_remote_park_qualifies(conn):
    _base_season_and_players(conn)
    _place_reason(conn, 11, "park", "remote", points=25)
    _activation(conn, place_id=11, player_id=50, awarded_at=_WINDOW_START + 10)

    rows = qualifying_place_firsts(conn, protocol="mc", season_id=1,
                                    start_ts=_WINDOW_START, end_ts=_WINDOW_END)
    assert [r["place_id"] for r in rows] == [11]


def test_qualifying_place_firsts_city_park_excluded(conn):
    _base_season_and_players(conn)
    _place_reason(conn, 12, "park", "in_city", points=5)
    _place_reason(conn, 13, "park", "in_city_by_area", points=5)
    _activation(conn, place_id=12, player_id=50, awarded_at=_WINDOW_START + 10)
    _activation(conn, place_id=13, player_id=50, awarded_at=_WINDOW_START + 20)

    rows = qualifying_place_firsts(conn, protocol="mc", season_id=1,
                                    start_ts=_WINDOW_START, end_ts=_WINDOW_END)
    assert rows == []


def test_qualifying_place_firsts_landmark_never_qualifies(conn):
    """Landmarks never qualify regardless of points_reason -- unlike
    park, ref_type == 'landmark' is excluded outright."""
    _base_season_and_players(conn)
    _place_reason(conn, 14, "landmark", "remote", points=10)
    _activation(conn, place_id=14, player_id=50, awarded_at=_WINDOW_START + 10)

    rows = qualifying_place_firsts(conn, protocol="mc", season_id=1,
                                    start_ts=_WINDOW_START, end_ts=_WINDOW_END)
    assert rows == []


def test_qualifying_place_firsts_repeat_same_place_player_season_excluded(conn):
    """A player returning to a place they already first-activated
    earlier in the SAME season must not reappear as a first, even though
    that earlier activation falls outside this window."""
    _base_season_and_players(conn)
    _place_reason(conn, 11, "park", "remote", points=25)
    # Just after the season started -- within season 1's own boundary
    # (unlike a fixed "14 days before the window" offset, which can fall
    # BEFORE the season even started and so never count as "earlier in
    # season" at all) and, at ~11.5 days before the window, a different
    # week_start than the "this window" activation below (place_
    # activation's own UNIQUE(place_id, player_id, week_start) forbids
    # two rows in the SAME week anyway, so a real repeat-in-season case
    # is always at least a week apart).
    _activation(conn, place_id=11, player_id=50, awarded_at=_SEASON_START + 100)  # earlier, same season
    _activation(conn, place_id=11, player_id=50, awarded_at=_WINDOW_START + 10)   # this window

    rows = qualifying_place_firsts(conn, protocol="mc", season_id=1,
                                    start_ts=_WINDOW_START, end_ts=_WINDOW_END)
    assert rows == []


def test_qualifying_place_firsts_different_place_same_player_qualifies(conn):
    """A player who already used up their 'first' on one place this
    season still qualifies for a genuinely NEW place."""
    _base_season_and_players(conn)
    _place_reason(conn, 11, "park", "remote", points=25)
    _place_reason(conn, 15, "park", "remote", points=25)
    _activation(conn, place_id=11, player_id=50, awarded_at=_SEASON_START + 100)  # old first, place 11
    _activation(conn, place_id=15, player_id=50, awarded_at=_WINDOW_START + 10)   # new first, place 15

    rows = qualifying_place_firsts(conn, protocol="mc", season_id=1,
                                    start_ts=_WINDOW_START, end_ts=_WINDOW_END)
    assert [r["place_id"] for r in rows] == [15]


def test_qualifying_place_firsts_same_place_different_season_qualifies(conn):
    """A first from a PRIOR season does not suppress a genuine first in
    the season this call is scoped to -- season boundaries reset the
    "first" check, place ids do not."""
    season2_start = _SEASON_END
    season2_end = season2_start + 1_000_000
    _season(conn, 1, started_at=_SEASON_START, ends_at=_SEASON_END)
    _season(conn, 2, started_at=season2_start, ends_at=season2_end)
    _player(conn, 50, team="RED")
    _place_reason(conn, 11, "park", "remote", points=25)
    _activation(conn, place_id=11, player_id=50, awarded_at=_SEASON_START + 10)  # first, season 1

    window_start = season2_start + 100
    window_end = window_start + 7 * 86400
    _activation(conn, place_id=11, player_id=50, awarded_at=window_start + 10)  # first, season 2

    rows = qualifying_place_firsts(conn, protocol="mc", season_id=2,
                                    start_ts=window_start, end_ts=window_end)
    assert [r["place_id"] for r in rows] == [11]


def test_qualifying_place_firsts_outside_window_excluded(conn):
    _base_season_and_players(conn)
    _place_reason(conn, 11, "park", "remote", points=25)
    _activation(conn, place_id=11, player_id=50, awarded_at=_WINDOW_END + 10)  # after the window

    rows = qualifying_place_firsts(conn, protocol="mc", season_id=1,
                                    start_ts=_WINDOW_START, end_ts=_WINDOW_END)
    assert rows == []


def test_qualifying_place_firsts_enqueues_nothing(conn):
    """Purely a read -- must never write to discord_outbox, or any other
    table."""
    _base_season_and_players(conn)
    _place_reason(conn, 10, "summit", "remote_scaled", points=80)
    _activation(conn, place_id=10, player_id=50, awarded_at=_WINDOW_START + 10)

    qualifying_place_firsts(conn, protocol="mc", season_id=1,
                             start_ts=_WINDOW_START, end_ts=_WINDOW_END)
    assert conn.execute("SELECT * FROM discord_outbox").fetchall() == []
