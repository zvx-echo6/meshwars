"""Credits "Places Worth Going" (docs/features/places.md) from an
accepted, scoring ping -- hooked into the same write transaction as
app/mc_scoring.apply_paint(), called right after it from both
app/mc_ingest.py and app/ingest.py (see credit_places()'s docstring for
exactly what gates a credit). This module never touches square/tile
ownership, decay, or the defense window -- that is entirely
mc_scoring.apply_paint()'s job, unchanged by anything here.

Rules encoded here, from docs/features/places.md:
  - One credit per reference, per person, per week -- enforced by
    place_activation's UNIQUE(place_id, player_id, week_start), backed
    up by an existence check here so the weekly cap (below) is counted
    correctly rather than discovered via a failed insert.
  - 100 points per person per week, whatever the mix -- WEEKLY_CAP_POINTS.
    A place only credits if its FULL point value fits in what is left
    of the cap this week; there is no partial credit. This is not
    incidental: landmark(5)*20, park(25)*4, and summit(100)*1 all land
    on exactly 100, which is the point of a flat, type-based value in
    the first place -- see docs/features/places.md.
  - A rotating place only credits while it is live this week
    (app/place_rotation.live_place_ids); an always-active place
    (summit, or a park at/above one grid cell) always qualifies.
  - Aircraft excluded, same as the exploration awards.
"""
from __future__ import annotations

import logging
import sqlite3

from .place_rotation import resolve_week, week_start_for_ts

log = logging.getLogger("place_scoring")

WEEKLY_CAP_POINTS = 100


def credit_places(
    conn: sqlite3.Connection,
    player_id: int,
    cell_id: str,
    ts: int,
    repeater_ids: list,
    by_air: bool = False,
) -> list[tuple[int, int]]:
    """Credit every live place this cell activates, for this player,
    this week -- subject to the once-per-reference and weekly-cap
    rules above. Returns [(place_id, points_awarded), ...] for whatever
    was actually credited (may be empty).

    Gating on `repeater_ids` non-empty directly, not on
    mc_scoring.apply_paint()'s PaintResult.outcome: "a scoring ping" is
    the same test apply_paint() itself uses to decide "no_signal" (did
    this ping name at least one repeater/feeder) -- apply_paint's other
    outcomes (cooldown, reinforced, captured, attacked, flipped) are
    about SQUARE ownership dynamics that place-crediting does not share.
    A "cooldown" ping (this player's square score is throttled because
    these exact repeaters were already credited to them on this cell
    recently) still represents a real, current visit to this cell with a
    working radio -- and place credit is gated weekly, not per-visit, so
    there is nothing to protect against by also blocking it here. Only a
    ping that named zero repeaters (no_signal) reached no one and must
    not credit anything, on a square or on a place.

    Caller must already hold app.db's write lock and have an open write
    transaction on `conn` -- same contract as apply_paint().
    """
    if by_air or not repeater_ids:
        return []

    place_ids = [
        r[0] for r in conn.execute(
            "SELECT place_id FROM place_cell WHERE cell_id = ?", (cell_id,)
        )
    ]
    if not place_ids:
        return []

    week_start = week_start_for_ts(ts)
    # Ensures this week's draw is computed and persisted before the
    # liveness check below reads place_week -- idempotent and cheap
    # after the first ping of a new week resolves it (see
    # place_rotation.resolve_week). A cell almost always maps to one or
    # two place_ids, so filtering those few directly against place_week
    # here is far cheaper than materializing the whole always-active set
    # (tens of thousands of rows) on every scoring ping the way
    # live_place_ids() does -- that helper is for the map/admin routes,
    # which need the full set anyway.
    resolve_week(conn, week_start)
    marks = ",".join("?" * len(place_ids))
    rows = conn.execute(
        "SELECT id, points FROM place "
        f" WHERE id IN ({marks}) "
        "   AND (rotates = 0 OR EXISTS ("
        "         SELECT 1 FROM place_week w WHERE w.week_start = ? AND w.place_id = place.id))"
        " ORDER BY points DESC",
        (*place_ids, week_start),
    ).fetchall()
    if not rows:
        return []

    already_points = conn.execute(
        "SELECT COALESCE(SUM(points), 0) FROM place_activation "
        "WHERE player_id = ? AND week_start = ?",
        (player_id, week_start),
    ).fetchone()[0]
    remaining = WEEKLY_CAP_POINTS - already_points
    if remaining <= 0:
        return []

    credited: list[tuple[int, int]] = []
    for row in rows:
        place_id, points = row["id"], row["points"]
        if points > remaining:
            continue  # doesn't fit this week's remaining budget -- no partial credit
        exists = conn.execute(
            "SELECT 1 FROM place_activation WHERE place_id = ? AND player_id = ? AND week_start = ?",
            (place_id, player_id, week_start),
        ).fetchone()
        if exists is not None:
            continue  # already credited this reference this week
        conn.execute(
            "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (place_id, player_id, week_start, points, ts),
        )
        credited.append((place_id, points))
        remaining -= points
        if remaining <= 0:
            break

    if credited:
        log.info(
            "place_scoring: player %d credited %s at cell %s (week %s)",
            player_id, credited, cell_id, week_start,
        )
    return credited
