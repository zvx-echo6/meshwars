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
    of the cap this week; there is no partial credit. Because only ONE
    place per cell is ever eligible (see NON-STACKING below), a cell
    whose place does not fit pays nothing that week -- there is no
    cheaper runner-up on the cell to fall back to. The place itself is
    untouched and is still there next week.
  - Point values are NOT flat by ref_type. They are scored by effort at
    seed-build time (scripts/build_places_seed.py's score_points(),
    baked into place.points): anything inside a Census place's own
    radius is 5, and outside it a landmark is 10, a park is 25, and a
    summit scales linearly 50->100 from 6,000ft to 9,000ft of
    elevation. Nothing here branches on ref_type or points_reason --
    place.points is read as an opaque number, exactly as it was under
    the old flat model, which is why the rescore needed no change in
    this module.
  - NON-STACKING: at most ONE place credits per cell. A cell routinely
    maps to more than one place -- a landmark standing inside a big
    park is the ordinary case -- and only the HIGHEST-VALUE eligible
    one pays out. The lesser ones are not on the table at all: they do
    not credit alongside the winner, and they are not a fallback when
    the winner is over this week's remaining budget or was already
    credited this week. Equal point values are broken by
    _stable_tiebreak() below, never by insertion order.
    (Until 2026-08-27 every eligible place on the cell credited, points
    DESC, until the cap stopped it. That stacking was never intended --
    it paid twice out of one ping for one errand.)
  - A rotating place only credits while it is live this week
    (app/place_rotation.live_place_ids); an always-active place
    (summit, a park at/above one grid cell, or a park with no boundary
    on file at all -- see app/places_seed.py) always qualifies.
  - A place that has left the seed (place.active = 0, set by
    app/places_seed.py's reconcile pass) never credits, even if a
    stale place_cell or place_week row still points at it.
  - Aircraft excluded, same as the exploration awards -- MeshCore only;
    the Meshtastic path never sets by_air (app/ingest.py passes False),
    because it rejects an implausible fix outright instead of labelling
    it.
"""
from __future__ import annotations

import logging
import sqlite3

from .place_rotation import resolve_week, week_start_for_ts

log = logging.getLogger("place_scoring")

WEEKLY_CAP_POINTS = 100


# Deterministic tiebreak for two places on the SAME cell carrying the
# SAME point value -- one of them credits and the other gets nothing, so
# which one wins has to be a stable property of the places themselves,
# not of the query plan or of the order the seed CSV happened to load
# in. Multiplicative hashing (Knuth's constant, reduced mod a large
# prime, evaluated inline by SQLite while it sorts): the same id always
# hashes to the same value, so the same cell resolves to the same winner
# on every run, on every replica, and after any rebuild of the database
# -- while being decorrelated from `id` itself, which on a seeded table
# is just insertion order and carries the source file's own clustering.
# app/places_api.py imports this for a different job (thinning a capped
# map viewport evenly instead of amputating whichever rows sort last);
# see the long comment there for that reasoning.
def _stable_tiebreak(id_column: str) -> str:
    return f"(({id_column} * 2654435761) % 1000000007)"


def credit_places(
    conn: sqlite3.Connection,
    player_id: int,
    cell_id: str,
    ts: int,
    repeater_ids: list,
    by_air: bool = False,
) -> list[tuple[int, int]]:
    """Credit the single highest-value live place this cell activates,
    for this player, this week -- subject to the once-per-reference and
    weekly-cap rules above. Returns [(place_id, points_awarded)] for
    what was actually credited, or [] if nothing was; the list shape is
    kept for the callers, but it now never holds more than one entry.

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
    # LIMIT 1 is the non-stacking rule itself: the dearest eligible
    # place on this cell is the ONLY candidate, and the runners-up are
    # discarded here rather than kept as a fallback further down -- see
    # the NON-STACKING note in the module docstring.
    row = conn.execute(
        "SELECT id, points FROM place "
        f" WHERE id IN ({marks}) "
        "   AND active = 1 "
        "   AND (rotates = 0 OR EXISTS ("
        "         SELECT 1 FROM place_week w WHERE w.week_start = ? AND w.place_id = place.id))"
        f" ORDER BY points DESC, {_stable_tiebreak('id')} ASC "
        " LIMIT 1",
        (*place_ids, week_start),
    ).fetchone()
    if row is None:
        # This cell does map to a place (or places) -- just none of
        # them qualify right now: inactive (left the seed) or a
        # rotating place that isn't this week's draw. Silent otherwise,
        # this is exactly the "why didn't that award" question an
        # operator can't answer by staring at an empty place_activation
        # table -- log it so they don't have to re-derive it by hand.
        log.debug(
            "place_scoring: cell %s maps to place(s) %s but none are "
            "active+live this week (%s)",
            cell_id, place_ids, week_start,
        )
        return []

    place_id, points = row["id"], row["points"]

    already_points = conn.execute(
        "SELECT COALESCE(SUM(points), 0) FROM place_activation "
        "WHERE player_id = ? AND week_start = ?",
        (player_id, week_start),
    ).fetchone()[0]
    remaining = WEEKLY_CAP_POINTS - already_points
    if remaining <= 0:
        log.debug(
            "place_scoring: player %d already at/over the %d weekly cap "
            "(%d) -- cell %s credits nothing (week %s)",
            player_id, WEEKLY_CAP_POINTS, already_points, cell_id, week_start,
        )
        return []

    if points > remaining:
        # Doesn't fit this week's remaining budget -- no partial credit,
        # and (non-stacking) no falling through to a cheaper place on
        # the same cell: the lesser place was never on the table, so
        # this cell simply pays nothing this week. The place is not
        # consumed by the attempt -- it is still worth its full value
        # next week.
        log.debug(
            "place_scoring: place %d (%d pts) doesn't fit player %d's "
            "remaining %d-point budget this week (%s) -- cell %s credits "
            "nothing",
            place_id, points, player_id, remaining, week_start, cell_id,
        )
        return []

    exists = conn.execute(
        "SELECT 1 FROM place_activation WHERE place_id = ? AND player_id = ? AND week_start = ?",
        (place_id, player_id, week_start),
    ).fetchone()
    if exists is not None:
        # Already credited this reference this week. Same rule as the
        # budget case above: no lesser place on the cell steps in for a
        # second payout, so revisiting the cell this week earns nothing.
        log.debug(
            "place_scoring: place %d already credited to player %d this "
            "week (%s) -- cell %s credits nothing",
            place_id, player_id, week_start, cell_id,
        )
        return []

    conn.execute(
        "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (place_id, player_id, week_start, points, ts),
    )
    credited = [(place_id, points)]
    log.info(
        "place_scoring: player %d credited %s at cell %s (week %s)",
        player_id, credited, cell_id, week_start,
    )
    return credited
