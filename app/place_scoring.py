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
    The cap CLAMPS rather than refuses (changed 2026-08-27, "no just
    drop the lower value. if you are at 50 points and snag a 100 point
    peak, just cap it out."): a place credits for whatever is left of
    the cap, min(place.points, remaining), not its full value. A player
    at 50 of 100 who activates a 100-point peak is credited 50, finishes
    the week at the cap, and that place_activation row records 50 --
    not the peak's real 100 -- because the row is the source every SUM
    (Explorer Score, team totals) reads, and that sum must equal what
    the player actually received. Only when remaining is already zero
    does the place credit nothing and get no row at all -- it is not
    consumed for a zero-point activation, and it is not partially
    creditable either way: whatever fraction it did pay counts as its
    one credit for the week (place_activation is still unique per
    place/player/week), so a place capped down to a partial payout does
    NOT reappear later in the same week to pay out its remainder.
    (Until 2026-08-27 a place whose full value didn't fit the remaining
    budget was skipped entirely, paying zero rather than the partial
    amount; that was the rule Matt corrected.)
  - Point values are NOT flat by ref_type. They are scored by effort at
    seed-build time (scripts/build_places_seed.py's score_points(),
    baked into place.points): anything inside a Census place's own
    radius is 5, and outside it a landmark is 10, a park is 25, and a
    summit scales linearly 50->100 from 6,000ft to 9,000ft of
    elevation. Nothing in the CREDITING path (credit_places() below)
    branches on ref_type or points_reason -- place.points is read as an
    opaque number, exactly as it was under the old flat model, which is
    why the rescore needed no change there. (qualifying_place_firsts()
    much further below is the one exception: a separate, read-only
    REPORTING helper for the weekly Discord recap, not part of crediting
    at all, that does branch on ref_type and points_reason -- see its
    own module-level comment for why.)
  - NON-STACKING: at most ONE place credits per cell. A cell routinely
    maps to more than one place -- a landmark standing inside a big
    park is the ordinary case -- and only the HIGHEST-VALUE eligible
    one pays out. The lesser ones are not on the table at all: they do
    not credit alongside the winner, and they are not a fallback when
    the winner's full value exceeds this week's remaining budget (the
    winner itself is simply capped there, not swapped out -- see the
    weekly-cap rule above) or the winner was already credited this
    week. Equal point values are broken by _stable_tiebreak() below,
    never by insertion order.
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

from .grid import ring_expand
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
    paint_outcome: str,
    by_air: bool = False,
    protocol: str = "",
) -> list[tuple[int, int]]:
    """Credit the single highest-value live place this cell activates,
    for this player, this week -- subject to the once-per-reference and
    weekly-cap rules above. Returns [(place_id, points_awarded)] for
    what was actually credited, or [] if nothing was; the list shape is
    kept for the callers, but it now never holds more than one entry.

    Gates on `paint_outcome` -- the `outcome` field of the PaintResult
    mc_scoring.apply_paint() just returned for this same ping -- rather
    than on the caller's repeater/feeder list. "a scoring ping" is
    apply_paint()'s own "no_signal" outcome, negated: apply_paint's
    other outcomes (cooldown, reinforced, captured, attacked, flipped)
    are about SQUARE ownership dynamics that place-crediting does not
    share. A "cooldown" ping (this player's square score is throttled
    because these exact repeaters were already credited to them on this
    cell recently) still represents a real, current visit to this cell
    with a working radio -- and place credit is gated weekly, not
    per-visit, so there is nothing to protect against by also blocking
    it here. Only "no_signal" reached no one and must not credit
    anything, on a square or on a place.

    Until 2026-09, this gated on the caller's `repeater_ids` list being
    non-empty instead -- a proxy for "no_signal" that happened to be
    exact for MeshCore and meshview, because both name the repeaters
    they reject on: apply_paint() returns "no_signal" precisely when
    that list is empty, so testing the list directly and testing the
    outcome it produces were the same question asked two different
    ways. FreqMapper (app/freqmapper_ingest.py) broke the equivalence:
    it calls apply_paint() in `flat_points` mode, where the repeater
    list is always empty by construction (there is no repeater/feeder
    concept to report) and the "named zero repeaters" check that
    produces "no_signal" is skipped entirely -- flat-scored mode simply
    cannot return "no_signal". So an empty list meant two different
    things depending on the source: for MeshCore/meshview, "this ping
    reached nobody"; for FreqMapper, "this source doesn't report that
    dimension" on an event that is independently-verified coverage and
    always scores. Gating on the list literally could not tell those
    apart, and silently read every FreqMapper event as the former --
    crediting nothing, for an entire board, with no error anywhere.
    Gating on the outcome instead asks the question apply_paint() itself
    already answered, so it is exact for every source by construction:
    identical to the old test wherever the proxy held (MeshCore,
    meshview), and correct where it didn't (FreqMapper).

    Caller must already hold app.db's write lock and have an open write
    transaction on `conn` -- same contract as apply_paint().
    """
    if by_air or paint_outcome == "no_signal":
        return []

    # REACHABLE-RING CREDIT, query side (moved here 2026-09-09 from
    # app/places_seed.py's seed-build-time _ring_expand() -- see
    # docs/features/places.md's reachable-ring section and this
    # module's own docstring). `place_cell` stores only a place's own
    # occupied cell(s); the ring that makes "the fence line, the
    # trailhead, the reachable perimeter" credit is expanded HERE
    # instead, against the ping's own cell, and matched against
    # whatever place_cell already has on file. This is exactly
    # equivalent to the old storage-side expansion for every non-summit
    # place: "is the ping's cell inside the place's 3x3?" and "is the
    # place's cell inside the ping's 3x3?" are the same question, by
    # simple symmetry of the ring itself (both sides are the identical
    # King's-move adjacency test) -- moving which side does the
    # expanding cannot change who gets credited, only where the
    # temporarily-9x-larger set exists (a handful of query parameters
    # per ping, not a permanent row per place per ring cell). This is
    # also WHY the old change ballooned `place_cell`: 1,281,030
    # landmarks alone at 9 stored rows each pushed the table past 79M
    # rows and a full seed load past an hour; storing only the base
    # cell and expanding the far smaller number of PINGS instead fixes
    # that without touching who qualifies for a ring at all.
    #
    # SUMMITS ARE THE ONE EXCEPTION, and the entire reason this cannot
    # be a blind "match cell_id IN (ring)" query: a SOTA activation
    # requires physically reaching the summit (see the REACHABLE-RING
    # CREDIT note in app/places_seed.py), so a summit's stored cell(s)
    # must credit ONLY on an EXACT match against the ping's own cell,
    # never via a neighbouring cell -- exactly as they always have,
    # ring or no ring, since a summit's place_cell rows were never
    # ring-expanded even under the old storage-side scheme. The WHERE
    # clause below encodes precisely that: `pc.cell_id = ?` (the ping's
    # own cell) always qualifies, for any ref_type, including summit;
    # a neighbouring cell only qualifies when the place backing it is
    # NOT a summit. Getting this gate wrong -- e.g. matching the whole
    # ring for every ref_type -- would silently hand every summit a
    # ring and break SOTA's core rule without a single test failing
    # anywhere else in this module (see tests/test_places_seed.py's
    # test_summit_does_not_credit_from_an_adjacent_cell for the
    # regression coverage).
    #
    # Still a plain indexed lookup on `place_cell.cell_id`
    # (idx_place_cell_cell) -- 9 index probes via the IN(...) list
    # instead of 1, never a table scan -- joined to `place` by its
    # primary key `id` to read ref_type, which costs nothing extra per
    # matched row. DISTINCT because a place spanning more than one of
    # the ping's 9 cells (a boundary-matched park whose footprint
    # touches several of them) would otherwise list the same place_id
    # more than once.
    ring_cells = list(ring_expand({cell_id}))
    marks = ",".join("?" * len(ring_cells))
    place_ids = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT pc.place_id FROM place_cell pc "
            "  JOIN place p ON p.id = pc.place_id "
            f" WHERE pc.cell_id IN ({marks}) "
            "   AND (pc.cell_id = ? OR p.ref_type != 'summit')",
            (*ring_cells, cell_id),
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

    # Clamp, don't refuse (changed 2026-08-27): a place worth more than
    # what's left of the week still credits, just for the remainder --
    # the row records `awarded`, the amount actually paid, not
    # `points`, the place's full value, so every reader that SUMs this
    # table (Explorer Score, team totals) agrees with what the player
    # received. A place capped down this way still fully consumes its
    # one credit for the week (the UNIQUE constraint above), so it does
    # NOT come back later in the same week to pay out the difference --
    # see the module docstring.
    awarded = min(points, remaining)
    conn.execute(
        "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at, protocol) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (place_id, player_id, week_start, awarded, ts, protocol),
    )
    credited = [(place_id, awarded)]

    # A per-activation Discord announcement used to be enqueued right
    # here (build_place_activation_embed()/place_activation_notability(),
    # app/discord_notify.py) -- removed 2026-09-16: at ~373 activations a
    # month it was too frequent to be worth reading, and posting each one
    # in near-real-time announced a player's location within minutes of
    # them reaching it, which is exactly the pairing app/public_api.py's
    # privacy rule ("identity can be public, location can be public, the
    # link between them requires a session") exists to keep off of an
    # unauthenticated surface. Notable activations are now folded,
    # anonymised to a per-player COUNT with no place name attached, into
    # the Sunday "weekly recap" (this module's own qualifying_place_
    # firsts() below, read by app/discord_notify.py's time-driven
    # weekly_recap_provider(), a TIME_DRIVEN_PROVIDERS entry) instead of
    # announced one at a time.
    if awarded < points:
        log.info(
            "place_scoring: player %d credited %s at cell %s (week %s) "
            "-- capped from the place's full %d points by the remaining "
            "%d-point budget",
            player_id, credited, cell_id, week_start, points, remaining,
        )
    else:
        log.info(
            "place_scoring: player %d credited %s at cell %s (week %s)",
            player_id, credited, cell_id, week_start,
        )
    return credited


# ---------------------------------------------------------------------
# Weekly exploration reel (app/discord_notify.py's Sunday weekly_recap
# provider) -- reads place_activation for the "notable firsts" the old
# per-event announcement used to post one at a time (see credit_places()'s
# own comment on why that was removed).
#
# *** HARD PRIVACY WARNING ***
# Every row this returns carries a player's display name AND team
# alongside a SPECIFIC place's own name/ref_type/elevation_ft. That
# pairing is exactly what app/public_api.py's module docstring (line 38)
# forbids on any unauthenticated surface: "identity can be public,
# location can be public, the link between them requires a session" --
# the rule commit 007db35 ("stop the public API linking a person to a
# place") hardened across the rest of the site. Discord has NO session
# concept at all, so a caller building an announcement from these rows
# MUST NOT ever render a player name and a place name on the same line,
# or anywhere in the same message where a reader could connect the two.
# The only sanctioned rendering (app/discord_notify.py's weekly_recap
# provider) reduces this to an aggregate COUNT of firsts per player and
# one unattributed elevation figure -- never a place name next to a
# person. Do not add a second caller of this function that renders a
# place name and a player name together, on Discord or anywhere else
# unauthenticated, without checking with Matt first (the same standing
# instruction app/public_api.py's own docstring already carries for its
# two grandfathered person-to-place routes).
#
# QUALIFYING RULE: which place_activation rows count as a "notable
# first" at all. Matched on place.points_reason's PREFIX, never on
# place.points itself -- points are a rating the seed-build script can
# retune at any time (score_points() in scripts/build_places_seed.py),
# but points_reason states the FACT being selected on (in a Census
# place's radius, or not), which does not change if the number attached
# to it does. Live values, for reference (2026-09):
#   landmark  in_city / remote                   -> never qualifies
#   park      in_city, in_city_by_area   5 pts   -> city parks, excluded
#   park      remote, remote_by_area    25 pts   -> qualifies
#   summit    remote_scaled          50-100 pts  -> qualifies
# i.e. a summit always qualifies (every summit on file is scored
# 'remote_scaled' -- see place.points_reason's own CREATE TABLE comment,
# no summit has ever been scored 'in_city'), a park qualifies unless its
# points_reason begins with 'in_city', and a landmark never qualifies at
# all, regardless of points_reason. Measured against production data
# (2026-09): 483 total place_activation rows, of which 275 were
# in-city parks and 107 were landmarks -- excluded by this rule -- leaving
# 82 qualifying firsts (76 parks, 6 summits) for the month, about 2 a
# day. That volume is exactly why these are collected into one weekly
# reel instead of announced as they happen (see credit_places()'s own
# comment on the per-event announcement this replaced).
#
# "FIRST" IS SEASON-WIDE, NOT WINDOW-WIDE: a place_activation row only
# counts here if it is that (place, player)'s EARLIEST activation
# anywhere in the whole season, not merely the earliest inside
# [start_ts, end_ts) -- so a player who first activated a summit three
# weeks ago and returns to it this week must NOT reappear in this week's
# reel. The reporting window only decides which week's message reports a
# first that already happened; it never redefines what "first" means.
def qualifying_place_firsts(conn: sqlite3.Connection, *, protocol: str, season_id: int,
                             start_ts: int, end_ts: int) -> list[dict]:
    """Qualifying first-time place activations whose activation falls in
    [start_ts, end_ts) -- see this section's own module-level comment
    above for the qualifying rule, the season-wide "first" semantics, and
    the HARD PRIVACY WARNING on what these rows may never be rendered
    into. Read-only: enqueues nothing, writes nothing, calls
    discord_notify for nothing -- a pure query a caller (currently only
    discord_notify.py's weekly_recap_provider()) turns into an
    announcement itself.

    `protocol` scopes both which board's activations are read
    (place_activation.protocol, same 'mc'/'mt' split every other place
    honour already filters on -- see this module's own docstring) and
    which board's "earlier activation" rows count against the
    season-wide first-ever check: a first on one board does not consume
    a player's first on the other, mirroring how a place credit itself
    is scoped (place_activation.protocol's own CREATE TABLE comment).

    `season_id` names an mc_season row (already resolved by the caller --
    place_activation has no season_id column of its own to join on, see
    app/mc_scoring.py's team_place_points() for the same situation and
    the same fix: scope by TIME against mc_season.started_at/ends_at
    instead). A season_id that does not belong to `protocol`, or does
    not exist at all, yields an empty list rather than raising -- a
    caller passing a stale or mismatched id should see "nothing to
    report," not a crash in a background poll loop.

    Each returned dict carries place_id, player_id, awarded_at, points,
    place_name, ref_type, elevation_ft, player_name, team -- read the
    HARD PRIVACY WARNING above before doing anything with place_name and
    player_name together.
    """
    season = conn.execute(
        "SELECT started_at, ends_at FROM mc_season WHERE id = ? AND protocol = ?",
        (season_id, protocol),
    ).fetchone()
    if season is None:
        return []
    season_start, season_end = season["started_at"], season["ends_at"]

    rows = conn.execute(
        "SELECT pa.place_id, pa.player_id, pa.awarded_at, pa.points, "
        "       p.name AS place_name, p.ref_type, p.elevation_ft, "
        "       pl.display_name AS player_name, pl.team AS team "
        "  FROM place_activation pa "
        "  JOIN place p ON p.id = pa.place_id "
        "  JOIN player pl ON pl.player_id = pa.player_id "
        " WHERE pa.protocol = ? "
        "   AND pa.awarded_at >= ? AND pa.awarded_at < ? "
        "   AND ("
        "        p.ref_type = 'summit' "
        "        OR (p.ref_type = 'park' AND "
        "            (p.points_reason IS NULL OR p.points_reason NOT LIKE 'in_city%'))"
        "   )"
        # Season-wide "first" check: exclude this row if this exact
        # (place, player) already has an EARLIER activation anywhere in
        # the same season (not just inside this window) -- see this
        # section's own module-level comment for why the window must
        # never redefine "first". Bounded to the season's own
        # [started_at, ends_at) so an activation from a PRIOR season at
        # the same place never falsely suppresses a genuine first in
        # this one (place_id/player pairs are not reset between
        # seasons -- place.id is permanent, see that table's own
        # comment).
        "   AND NOT EXISTS ("
        "        SELECT 1 FROM place_activation earlier "
        "         WHERE earlier.place_id = pa.place_id "
        "           AND earlier.player_id = pa.player_id "
        "           AND earlier.protocol = pa.protocol "
        "           AND earlier.awarded_at < pa.awarded_at "
        "           AND earlier.awarded_at >= ? "
        "           AND earlier.awarded_at < ?"
        "   )"
        " ORDER BY pa.awarded_at",
        (protocol, start_ts, end_ts, season_start, season_end),
    ).fetchall()
    return [dict(r) for r in rows]
