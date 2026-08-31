"""Tests for the Tourist/Park Hopper/Peak Tagger awards in
app/results.py (docs/features/places.md, "What it changes about the
honors").

Explorer used to mean "most Explorer Score points earned this month"
(points from place_activation), which came down 2026-08-25: points are
capped at 100 per person per week, so everyone who played seriously
finished a month within the same narrow band and the award separated
nobody. It was replaced by three awards that count VISITS instead --
Tourist (landmarks), Park Hopper (parks), Peak Tagger (summits) -- each
scoped to the month and blind to place.points entirely. A month frozen
while Explorer still existed keeps its stored "explorer" award and
label forever (frozen months are never rewritten). Frontier is
unaffected by any of this and keeps counting squares beyond city
limits, virgin-ground restriction already dropped.
"""
from __future__ import annotations

import time

from app import results
from app.grid import cell_id


def _unawarded(awards, key):
    """An honor nobody won is listed with no winner rather than omitted
    (results.with_placeholders) -- except `explorer`, retired outright,
    which is not in the slate at all."""
    a = _award(awards, key)
    return a is None or (a["player_id"] is None and a["team"] is None)


NOW = int(time.time())
MONTH = results.month_key(NOW)
START, END = results.month_bounds(MONTH)


def _player(conn, player_id, team, name=None):
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?,?,?,?)",
        (player_id, name or f"player-{player_id}", team, NOW),
    )


def _season(conn, protocol, started_at=0, ends_at=None):
    ends_at = ends_at if ends_at is not None else NOW + 10_000_000
    cur = conn.execute(
        "INSERT INTO mc_season(protocol, started_at, ends_at, status) VALUES (?,?,?,?)",
        (protocol, started_at, ends_at, "active"),
    )
    return cur.lastrowid


def _capture(conn, season_id, cell, ts, player_id, team, from_team=None, by_air=0):
    conn.execute(
        "INSERT INTO mc_tile_capture_log(season_id, cell_id, ts, by_player_id, by_team, "
        "from_team, by_air) VALUES (?,?,?,?,?,?,?)",
        (season_id, cell, ts, player_id, team, from_team, by_air),
    )


def _place_activation(conn, place_id, player_id, points, awarded_at, week_start="2026-01-07"):
    # place_activation has no foreign-key enforcement in the test schema's
    # in-memory connection (see conftest.py), so a bare place_id is fine --
    # nothing here reads app/place. Tests that need ref_type (tourist /
    # park_hopper / peak_tagger are joined against place.ref_type) go
    # through _place() below instead, for a real place row to join to.
    conn.execute(
        "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at) "
        "VALUES (?,?,?,?,?)",
        (place_id, player_id, week_start, points, awarded_at),
    )


def _place(conn, place_id, ref_type, points=5):
    conn.execute(
        "INSERT INTO place(id, ref_type, ref_code, name, lat, lon, points, source, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (place_id, ref_type, f"ref-{place_id}", f"place-{place_id}", 43.0, -116.0,
         points, "TEST", NOW),
    )


def _award(awards, key):
    return next((a for a in awards if a["award"] == key), None)


def test_tourist_counts_landmark_visits_not_points(conn):
    """Tourist goes to the most landmark VISITS this month -- a row in
    place_activation joined to a landmark place -- ignoring points
    entirely. A player with fewer, higher-value visits must not beat one
    with more, cheaper ones.
    """
    _player(conn, 1, "RED")
    _player(conn, 2, "BLUE")
    _season(conn, "mt")

    # Player 1: two landmark visits, 5 points each (10 total).
    _place(conn, 1, "landmark", points=5)
    _place(conn, 2, "landmark", points=5)
    _place_activation(conn, 1, player_id=1, points=5, awarded_at=START + 10)
    _place_activation(conn, 2, player_id=1, points=5, awarded_at=START + 20)

    # Player 2: one landmark visit worth far more points (10) -- must
    # still lose on VISIT count, proving points are ignored.
    _place(conn, 3, "landmark", points=10)
    _place_activation(conn, 3, player_id=2, points=10, awarded_at=START + 30)

    result = results.compute_month(conn, "mt", MONTH)
    tourist = _award(result["awards"], "tourist")
    assert tourist is not None
    assert tourist["player_id"] == 1
    assert tourist["value"] == 2
    assert tourist["detail"] == "landmarks visited"
    # Ignored entirely: park_hopper/peak_tagger must not fire on landmark data.
    assert _unawarded(result["awards"], "park_hopper")
    assert _unawarded(result["awards"], "peak_tagger")
    assert _unawarded(result["awards"], "explorer")


def test_park_hopper_counts_park_visits_only(conn):
    _player(conn, 1, "RED")
    _season(conn, "mt")
    _place(conn, 1, "park", points=25)
    _place(conn, 2, "landmark", points=5)  # different type -- must not count here
    _place_activation(conn, 1, player_id=1, points=25, awarded_at=START + 10)
    _place_activation(conn, 2, player_id=1, points=5, awarded_at=START + 20)

    result = results.compute_month(conn, "mt", MONTH)
    park_hopper = _award(result["awards"], "park_hopper")
    assert park_hopper is not None
    assert park_hopper["player_id"] == 1
    assert park_hopper["value"] == 1
    assert park_hopper["detail"] == "parks visited"


def test_peak_tagger_counts_summit_visits_only(conn):
    _player(conn, 1, "RED")
    _season(conn, "mt")
    _place(conn, 1, "summit", points=100)
    _place_activation(conn, 1, player_id=1, points=100, awarded_at=START + 10)

    result = results.compute_month(conn, "mt", MONTH)
    peak_tagger = _award(result["awards"], "peak_tagger")
    assert peak_tagger is not None
    assert peak_tagger["player_id"] == 1
    assert peak_tagger["value"] == 1
    assert peak_tagger["detail"] == "summits visited"


def test_place_visit_awards_ignore_activity_outside_the_month(conn):
    _player(conn, 1, "RED")
    _season(conn, "mt")
    _place(conn, 1, "landmark")
    _place(conn, 2, "park")
    _place(conn, 3, "summit")
    _place_activation(conn, 1, player_id=1, points=5, awarded_at=START - 1)   # before the month
    _place_activation(conn, 2, player_id=1, points=25, awarded_at=END)        # on/after month end
    _place_activation(conn, 3, player_id=1, points=100, awarded_at=END + 1)   # after the month

    result = results.compute_month(conn, "mt", MONTH)
    assert _unawarded(result["awards"], "tourist")
    assert _unawarded(result["awards"], "park_hopper")
    assert _unawarded(result["awards"], "peak_tagger")


def test_place_visit_awards_tie_refuses_a_winner(conn):
    """Matches _top()'s existing rule (shared by top_attacker, frontier,
    etc.): a shared lead is not a winner, an award nobody won is fine.
    """
    _player(conn, 1, "RED")
    _player(conn, 2, "BLUE")
    _season(conn, "mt")
    _place(conn, 1, "landmark")
    _place(conn, 2, "landmark")
    _place_activation(conn, 1, player_id=1, points=5, awarded_at=START + 10)
    _place_activation(conn, 2, player_id=2, points=5, awarded_at=START + 20)

    result = results.compute_month(conn, "mt", MONTH)
    assert _unawarded(result["awards"], "tourist")


def test_frontier_counts_out_of_town_captures_without_virgin_restriction(conn, monkeypatch):
    """Frontier no longer requires a virgin (from_team is None) claim --
    an attack or a retake out past the towns must count too.
    """
    _player(conn, 1, "RED")
    _player(conn, 2, "BLUE")
    season_id = _season(conn, "mt")

    far_cell = cell_id(45.0, -114.0)
    near_cell = cell_id(45.0, -114.5)

    def fake_distance(lat, lon):
        return 999_999.0 if cell_id(lat, lon) == far_cell else 10.0  # metres

    monkeypatch.setattr(results.places, "distance_to_nearest_town_m", fake_distance)

    # Player 1: an ATTACK (from_team set) on a far-out square -- would NOT
    # have counted under the old virgin-only rule.
    _capture(conn, season_id, far_cell, START + 10, 1, "RED", from_team="BLUE")
    # Player 2: a virgin claim, but inside town -- must not count.
    _capture(conn, season_id, near_cell, START + 20, 2, "BLUE", from_team=None)

    result = results.compute_month(conn, "mt", MONTH)
    frontier = _award(result["awards"], "frontier")
    assert frontier is not None
    assert frontier["player_id"] == 1
    assert frontier["value"] == 1


def test_frozen_month_is_not_recomputed(conn):
    """A month already frozen (month_result/month_award rows exist) must
    keep the numbers it was frozen with, even if new place_activation or
    capture rows land inside that month afterward.
    """
    _player(conn, 1, "RED")
    season_id = _season(conn, "mt")
    _place(conn, 1, "landmark")
    _place_activation(conn, 1, player_id=1, points=5, awarded_at=START + 10)

    results.freeze_month(conn, "mt", MONTH, NOW)
    frozen = results.month_results_for(conn, "mt", now=results.month_bounds(MONTH)[1] + 1)
    # Sanity: our frozen month is actually the one returned.
    stored = next(m for m in frozen["months"] if m["month"] == MONTH)
    before = _award(stored["awards"], "tourist")
    assert before is not None and before["value"] == 1

    # New activity lands in the same, already-frozen month.
    _place(conn, 2, "landmark")
    _place_activation(conn, 2, player_id=1, points=5, awarded_at=START + 20)
    _player(conn, 2, "BLUE")
    _capture(conn, season_id, cell_id(43.0, -116.0), START + 30, 2, "BLUE", from_team=None)

    # maybe_roll_months must not re-freeze a month that already has a result.
    rolled = results.maybe_roll_months(conn, now=results.month_bounds(MONTH)[1] + 1, protocol="mt")
    assert rolled == 0

    after = results.month_results_for(conn, "mt", now=results.month_bounds(MONTH)[1] + 1)
    stored_after = next(m for m in after["months"] if m["month"] == MONTH)
    still = _award(stored_after["awards"], "tourist")
    assert still is not None and still["value"] == 1  # unchanged -- history is history


def test_frozen_month_keeps_a_stored_explorer_award_with_its_label(conn):
    """Explorer is retired -- compute_month() never emits it for a month
    computed fresh -- but a month frozen while it still existed keeps
    its month_award row forever (frozen months are never rewritten), and
    AWARD_LABELS still carries a real name for it rather than falling
    back to the raw key. Writes the month_award row directly, the way a
    month frozen before 2026-08-25 would already have it on disk --
    freeze_month() itself is not involved, since a fresh freeze today
    would never produce this row.
    """
    _player(conn, 1, "RED")
    past_month = results.previous_month(results.previous_month(MONTH))
    conn.execute(
        "INSERT INTO month_result(month, protocol, closed_at) VALUES (?, ?, ?)",
        (past_month, "mt", NOW),
    )
    conn.execute(
        "INSERT INTO month_award(month, protocol, award, scope, player_id, team, value, detail) "
        "VALUES (?, 'mt', 'explorer', '', 1, 'RED', 125, 'points earned from places')",
        (past_month,),
    )

    out = results.month_results_for(conn, "mt", now=NOW)
    stored = next(m for m in out["months"] if m["month"] == past_month)
    explorer = _award(stored["awards"], "explorer")
    assert explorer is not None
    assert explorer["label"] == "Explorer"
    assert explorer["value"] == 125
    assert explorer["player"] == "player-1"


# ---- the in-progress month preview (app/config.results_preview_current_month)
# Off in production: month_results_for() reads frozen rows only, and the
# month being played is absent from "months" entirely. A preview host
# turns the flag on to see it early, computed live -- which must stay a
# purely read-side change.


def _preview(monkeypatch, on):
    monkeypatch.setattr(results.settings, "results_preview_current_month", on)


def _seed_open_month(conn):
    """A little real activity inside the month currently being played."""
    _player(conn, 1, "RED")
    season_id = _season(conn, "mt")
    _place(conn, 1, "landmark")
    _place_activation(conn, 1, player_id=1, points=5, awarded_at=START + 10)
    _capture(conn, season_id, cell_id(43.0, -116.0), START + 20, 1, "RED", from_team=None)
    return season_id


def test_preview_off_leaves_the_open_month_out(conn, monkeypatch):
    _preview(monkeypatch, False)
    _seed_open_month(conn)
    # A finished month to prove the frozen path still returns normally.
    past_month = results.previous_month(MONTH)
    conn.execute(
        "INSERT INTO month_result(month, protocol, closed_at) VALUES (?, ?, ?)",
        (past_month, "mt", NOW),
    )

    out = results.month_results_for(conn, "mt", now=NOW)
    assert [m["month"] for m in out["months"]] == [past_month]
    assert MONTH not in [m["month"] for m in out["months"]]
    assert all("preview" not in m for m in out["months"])


def test_preview_on_prepends_the_open_month_marked_provisional(conn, monkeypatch):
    _preview(monkeypatch, True)
    _seed_open_month(conn)
    past_month = results.previous_month(MONTH)
    conn.execute(
        "INSERT INTO month_result(month, protocol, closed_at) VALUES (?, ?, ?)",
        (past_month, "mt", NOW),
    )

    out = results.month_results_for(conn, "mt", now=NOW)
    assert [m["month"] for m in out["months"]] == [MONTH, past_month]

    live = out["months"][0]
    assert live["preview"] is True
    assert live["protocol"] == "mt"
    # It is a real computation, not an empty placeholder: the seeded
    # landmark visit shows up as Tourist.
    tourist = _award(live["awards"], "tourist")
    assert tourist is not None and tourist["value"] == 1
    # Award order matches the frozen path's, which re-sorts on read.
    assert live["awards"] == sorted(live["awards"], key=results._award_sort_key)

    # The frozen month is untouched and stays unmarked.
    assert "preview" not in out["months"][1]
    # The open-month banner still says what it always said.
    assert out["open_month"] == MONTH


def test_preview_on_writes_nothing(conn, monkeypatch):
    """The whole point of the flag is that it is display-only: reading a
    provisional month must never freeze it.
    """
    _preview(monkeypatch, True)
    _seed_open_month(conn)

    def counts():
        return tuple(
            conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("month_result", "month_standing", "month_award")
        )

    before = counts()
    results.month_results_for(conn, "mt", now=NOW)
    results.month_results_for(conn, "mt", now=NOW)
    assert counts() == before == (0, 0, 0)


# --- standings score ground HELD, not captures made ---------------------
#
# The month card used to count rows in mc_tile_capture_log, so a square
# that changed hands five times scored five and ground a team had lost
# still counted for them. That put /results in different units from the
# scoreboard, which has always counted current ownership -- in August
# 2026 RED read 58% above its scoreboard figure. Standings now count the
# owner of each square at the close, the same quantity the scoreboard
# shows (mc_scoring.team_tile_counts).


def test_standings_count_a_repeatedly_captured_square_once(conn):
    _player(conn, 1, "RED")
    sid = _season(conn, "mc")
    cell = cell_id(43.6, -116.2)
    for i in range(5):
        _capture(conn, sid, cell, START + 100 + i, 1, "RED", from_team=None if i == 0 else "RED")

    rows = {s["team"]: s for s in results.compute_month(conn, "mc", MONTH, NOW)["standings"]}
    assert rows["RED"]["squares"] == 1


def test_standings_credit_the_current_owner_not_whoever_took_it_first(conn):
    _player(conn, 1, "RED")
    _player(conn, 2, "BLUE")
    sid = _season(conn, "mc")
    cell = cell_id(43.6, -116.2)
    _capture(conn, sid, cell, START + 100, 1, "RED")
    _capture(conn, sid, cell, START + 200, 2, "BLUE", from_team="RED")

    rows = {s["team"]: s for s in results.compute_month(conn, "mc", MONTH, NOW)["standings"]}
    assert rows["BLUE"]["squares"] == 1
    assert rows["RED"]["squares"] == 0


def test_standings_reconstruct_ownership_as_of_the_month_close(conn):
    # A capture AFTER the month ended must not change that month's result.
    _player(conn, 1, "RED")
    _player(conn, 2, "BLUE")
    sid = _season(conn, "mc")
    cell = cell_id(43.6, -116.2)
    _capture(conn, sid, cell, START + 100, 1, "RED")
    _capture(conn, sid, cell, END + 5_000, 2, "BLUE", from_team="RED")

    rows = {s["team"]: s
            for s in results.compute_month(conn, "mc", MONTH, END + 10_000)["standings"]}
    assert rows["RED"]["squares"] == 1
    assert rows["BLUE"]["squares"] == 0
