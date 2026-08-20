"""MeshCore fortress scoring per grid cell.

This is the MeshCore counterpart to app/scoring.py: same fortress-scoring
shape, but working on flat grid cells and players instead of geohashes and
radios, and supporting any number of teams instead of a fixed two.

Each team has an independent score per cell that:
- Increases, per qualifying paint, by the number of distinct repeaters
  that ping named which this player has NOT already been credited for on
  this cell within settings.mc_cooldown_seconds, times
  settings.mc_points_per_repeater, capped at settings.mc_max_points_per_ping
  for the whole cooldown window -- a ping that named no repeaters reached
  no one and earns nothing, and a ping naming only repeaters already
  credited in the window earns nothing either (see apply_paint()'s
  docstring below for why the cooldown is scoped to the repeater, not the
  ping)
- Increases by settings.mc_score_per_unique_player the first time a given
  PLAYER from that team paints there (once per player, not per radio --
  a player may own more than one MeshCore radio), but only on a paint
  that itself scored points
- Decays at settings.mc_score_decay_per_day per day toward zero (floor=0)

Ownership rules:
- A cell has NO neutral state. It either has an owner_team or it does not
  exist as a row yet. The first team to paint an empty cell takes it.
- A held cell cannot flip at all for settings.mc_defense_window_seconds
  after it was last captured, regardless of score -- this protects a
  freshly taken cell from being immediately re-taken. The attacker's
  score still accumulates during the window.
- After the window: the cell flips to the attacking team only if the
  attacker's decayed score is >= the CURRENT OWNER's decayed score. The
  comparison is against the current owner alone, never against every
  other team -- with up to seven teams in play there is no single rival,
  so a team is only measured against whoever is holding the cell.
- On a flip: the losing team's score for that cell is reset to zero, and
  the flip is written to the capture log with who took it and from whom.

Scores are never stored pre-decayed. Every read decays the stored value
up to "now" on the fly; every write first re-reads the decayed value,
adds to it, and stores that as the new baseline with last_update reset to
"now" -- decay then resumes counting down from there.

A team's SEASON standing is a separate concept from a cell's score
above, and is no longer squares alone: app/checkin.py adds net check-in
points on top of squares held. team_totals() near the bottom of this
file is the combined figure and the one every standing/winner decision
reads -- see its docstring.

All functions here are synchronous and take an already-open connection
that the caller is expected to hold the write lock for. Nothing in this
module opens its own connection or acquires app.db's write lock -- the
worker in app/mc_ingest.py already does both before calling in.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from .config import settings

log = logging.getLogger("mc_scoring")


SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class PaintResult:
    """Outcome of apply_paint(). outcome is one of:
    "cooldown"   -- every repeater this ping named had already been credited
                    to this player on this cell within the cooldown window,
                    or the visit's point cap was already used up by earlier
                    credits in that window; nothing changed. This is the
                    spam case mc_cooldown_seconds exists for -- a player
                    parked in one spot re-pinging the same repeater(s) over
                    and over. A ping naming a repeater not yet credited on
                    this cell still scores even if it lands a second after
                    the last one -- see apply_paint()'s body for why.
    "no_signal"  -- the ping named zero repeaters, so it earned zero points;
                    nothing painted, nothing captured, no scores touched.
    "reinforced" -- the painting team already owned the cell; ownership unchanged.
    "captured"   -- the cell had no owner yet and this paint took it.
    "attacked"   -- a different team owns the cell and this paint did not flip it
                    (still inside the defense window, or the score wasn't enough).
    "flipped"    -- a different team owned the cell and this paint took it over.
    from_team is set for "attacked" and "flipped" (who the cell was contested
    with / taken from). score is the attacking team's decayed score for the
    cell after this paint, where meaningful.
    """
    outcome: str
    cell_id: str
    team: str
    score: float | None = None
    from_team: str | None = None


def decayed_score(stored_score: float, last_update_ts: int, now_ts: int) -> float:
    """Apply linear decay from last update to now. Floor at 0."""
    if last_update_ts >= now_ts:
        return stored_score
    elapsed_days = (now_ts - last_update_ts) / SECONDS_PER_DAY
    decayed = stored_score - (settings.mc_score_decay_per_day * elapsed_days)
    return max(0.0, decayed)


def get_team_score(
    conn: sqlite3.Connection,
    season_id: int,
    cell_id: str,
    team: str,
    now_ts: int,
) -> float:
    """Return the decayed score for (cell, team) as of now_ts."""
    row = conn.execute(
        "SELECT score, last_update FROM mc_tile_score "
        " WHERE season_id = ? AND cell_id = ? AND team = ?",
        (season_id, cell_id, team),
    ).fetchone()
    if not row:
        return 0.0
    return decayed_score(row["score"], row["last_update"], now_ts)


def upsert_team_score(
    conn: sqlite3.Connection,
    season_id: int,
    cell_id: str,
    team: str,
    new_score: float,
    now_ts: int,
) -> None:
    """Persist a team's score and last_update for a cell."""
    conn.execute(
        "INSERT INTO mc_tile_score(season_id, cell_id, team, score, last_update) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(season_id, cell_id, team) DO UPDATE SET "
        "  score = excluded.score, last_update = excluded.last_update",
        (season_id, cell_id, team, max(0.0, new_score), now_ts),
    )


def is_first_paint_for_player(
    conn: sqlite3.Connection,
    season_id: int,
    cell_id: str,
    team: str,
    player_id: int,
    ts: int,
) -> bool:
    """Check (and atomically record) whether this player has painted this
    cell for this team before. Returns True the FIRST time, False
    afterwards. Always bumps the paint_count.
    """
    row = conn.execute(
        "SELECT 1 FROM mc_tile_unique_painter "
        " WHERE season_id = ? AND cell_id = ? AND team = ? AND player_id = ?",
        (season_id, cell_id, team, player_id),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE mc_tile_unique_painter "
            "   SET paint_count = paint_count + 1 "
            " WHERE season_id = ? AND cell_id = ? AND team = ? AND player_id = ?",
            (season_id, cell_id, team, player_id),
        )
        return False
    conn.execute(
        "INSERT INTO mc_tile_unique_painter(season_id, cell_id, team, player_id, first_ts, paint_count) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (season_id, cell_id, team, player_id, ts),
    )
    return True


def _credited_repeaters(
    conn: sqlite3.Connection,
    player_id: int,
    protocol: str,
    cell_id: str,
    ts: int,
    window: int,
) -> set[str]:
    """Repeater ids this player has already been credited scoring points
    for, on this cell, within `window` seconds before ts. Read from
    player_cell_repeater_credit (see app/db.py) -- a row's `ts` is bumped
    forward every time that repeater earns fresh credit there, so a row
    older than `window` means this repeater's credit has lapsed and it is
    free to score again.

    `protocol` scopes this the same way it scoped player_cell_ping before
    it -- a cooldown is per-board, so a Meshtastic paint on a cell must
    never suppress a MeshCore paint on the cell that happens to share the
    same cell_id, and vice versa.
    """
    rows = conn.execute(
        "SELECT repeater_id FROM player_cell_repeater_credit"
        " WHERE player_id = ? AND protocol = ? AND cell_id = ? AND ts >= ?",
        (player_id, protocol, cell_id, ts - window),
    ).fetchall()
    return {r["repeater_id"] for r in rows}


def _record_repeater_credit(
    conn: sqlite3.Connection,
    player_id: int,
    protocol: str,
    cell_id: str,
    repeater_id: str,
    ts: int,
    seen_at: int,
) -> None:
    """Stamp `repeater_id` as credited to this player on this cell right
    now. Upsert, not insert -- a repeater that scores again after its
    previous credit has aged out of the cooldown window reuses the same
    row (matching the natural key), just with `ts`/`seen_at` pushed
    forward, rather than accumulating a full history of every credit ever
    earned here.
    """
    conn.execute(
        "INSERT INTO player_cell_repeater_credit"
        "(player_id, protocol, cell_id, repeater_id, ts, seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(player_id, protocol, cell_id, repeater_id) DO UPDATE SET "
        "  ts = excluded.ts, seen_at = excluded.seen_at",
        (player_id, protocol, cell_id, repeater_id, ts, seen_at),
    )


def in_defense_window(
    conn: sqlite3.Connection,
    season_id: int,
    cell_id: str,
    now_ts: int,
) -> bool:
    """True if the cell is inside its post-capture defense window."""
    row = conn.execute(
        "SELECT captured_at FROM mc_tile_capture "
        " WHERE season_id = ? AND cell_id = ?",
        (season_id, cell_id),
    ).fetchone()
    if not row:
        return False
    return (now_ts - row["captured_at"]) < settings.mc_defense_window_seconds


def record_capture(
    conn: sqlite3.Connection,
    season_id: int,
    cell_id: str,
    team: str,
    ts: int,
) -> None:
    """Stamp the cell as captured by this team at this time."""
    conn.execute(
        "INSERT INTO mc_tile_capture(season_id, cell_id, captured_at, captured_by_team) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(season_id, cell_id) DO UPDATE SET "
        "  captured_at = excluded.captured_at, "
        "  captured_by_team = excluded.captured_by_team",
        (season_id, cell_id, ts, team),
    )


def apply_paint(
    conn: sqlite3.Connection,
    season_id: int,
    player_id: int,
    team: str,
    cell_id: str,
    ts: int,
    repeater_ids: list[str],
    points_per_repeater: float,
    max_points_per_ping: float,
    protocol: str,
    received_at: int,
) -> PaintResult:
    """Score one accepted ping against a cell and resolve ownership.

    `repeater_ids` is the distinct repeaters (MeshCore) / feeders
    (Meshtastic) this specific ping named -- app/mc_ingest.py and
    app/ingest.py both parse this from the same RepeaterEntry list that
    record_repeater_observations() logs as evidence, so the two can never
    drift apart. `points_per_repeater` and `max_points_per_ping` are the
    caller's board-specific settings (settings.mc_points_per_repeater /
    settings.mc_max_points_per_ping for MeshCore, the mt_ equivalents for
    Meshtastic) -- passed in rather than looked up here so this function
    stays board-agnostic, same as everything else in this module.

    Scoring is gated per REPEATER, not per ping. mc_cooldown_seconds
    exists to stop a player parked in one spot from spamming pings to run
    up a score -- it does not exist to stop a player being credited for
    genuinely different repeaters heard on the same pass. MeshMapper
    sends one ping per repeater contact, often a second apart, so a
    single visit to a square routinely produces several pings in a row,
    each naming a different repeater; gating the whole ping on "was this
    cell painted recently" (the original reading of this rule) discarded
    every one of those pings after the first, crediting a player for one
    repeater when they had actually heard several. Here, a repeater
    already credited to this player on this cell within the cooldown
    window earns nothing again (the spam case); a repeater not yet
    credited still scores, however soon it arrives after the last ping.
    The per-visit cap (`max_points_per_ping`) still holds across the
    whole window -- it counts what has already been credited before
    adding more, so no number of pings or repeaters can push one cell
    past it in one visit. A ping naming zero repeaters reached no one and
    always earns nothing, cooldown aside.

    `protocol` and `received_at` reach the repeater-credit bookkeeping
    (player_cell_repeater_credit, see app/db.py) below -- everything else
    in this function (ownership, scoring, decay, the unique-player bonus)
    is scoped by season_id alone, and season_id is already
    protocol-specific by construction (see ensure_active_season).
    `received_at` is the server's own receipt time, distinct from `ts`
    (the ping's own, attacker-controlled clock) for the same reason
    player_cell_ping keeps the two separate: it is what retention
    housekeeping keys off, not the cooldown-window comparison itself.

    Caller must already hold app.db's write lock and have an open write
    transaction on `conn` -- this function does neither itself.
    """
    if not repeater_ids:
        return PaintResult("no_signal", cell_id, team)

    already_credited = _credited_repeaters(conn, player_id, protocol, cell_id, ts, settings.mc_cooldown_seconds)
    new_ids = [r for r in dict.fromkeys(repeater_ids) if r not in already_credited]
    if not new_ids:
        # Every repeater this ping named has already been credited to
        # this player on this cell within the window -- the spam case
        # the cooldown exists to block.
        return PaintResult("cooldown", cell_id, team)

    # The per-visit cap applies across the whole cooldown window, not
    # just this one ping -- count what earlier pings in the window have
    # already been credited before deciding how much of this ping fits.
    already_points = min(len(already_credited) * points_per_repeater, max_points_per_ping)
    remaining = max_points_per_ping - already_points
    if remaining <= 0:
        # New repeaters were named, but this visit already hit the cap
        # from earlier credits in the window -- same spam protection,
        # just triggered by the cap rather than an all-repeated ping.
        return PaintResult("cooldown", cell_id, team)

    slots = int(remaining / points_per_repeater + 1e-9) if points_per_repeater > 0 else 0
    credit_ids = new_ids[:slots]
    if not credit_ids:
        return PaintResult("cooldown", cell_id, team)

    for repeater_id in credit_ids:
        _record_repeater_credit(conn, player_id, protocol, cell_id, repeater_id, ts, received_at)
    points = min(len(credit_ids) * points_per_repeater, remaining)

    # Effort score for this paint, plus the one-time unique-player bonus
    # the first time this player has painted this cell for this team.
    current = get_team_score(conn, season_id, cell_id, team, ts)
    new_score = current + points
    if is_first_paint_for_player(conn, season_id, cell_id, team, player_id, ts):
        new_score += settings.mc_score_per_unique_player
    upsert_team_score(conn, season_id, cell_id, team, new_score, ts)

    tile = conn.execute(
        "SELECT owner_team FROM mc_tile WHERE season_id = ? AND cell_id = ?",
        (season_id, cell_id),
    ).fetchone()

    if tile is None:
        # No owner row yet -- there is no neutral state, so the first
        # team to paint an empty cell takes it immediately.
        conn.execute(
            "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, "
            "last_report_ts, paint_count) VALUES (?, ?, ?, ?, ?, 1)",
            (season_id, cell_id, team, player_id, ts),
        )
        record_capture(conn, season_id, cell_id, team, ts)
        # This is a real capture -- unowned ground just became owned --
        # so it belongs in the same audit log a flip writes to. There is
        # no previous owner, so from_team is null here.
        conn.execute(
            "INSERT INTO mc_tile_capture_log(season_id, cell_id, ts, by_player_id, by_team, from_team) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (season_id, cell_id, ts, player_id, team, None),
        )
        log.info("mc scoring: cell %s captured by %s (first paint)", cell_id, team)
        return PaintResult("captured", cell_id, team, score=new_score)

    owner_team = tile["owner_team"]

    if owner_team == team:
        # Reinforcement: the painting team already owns this cell.
        # Ownership does not change; just record who last touched it.
        conn.execute(
            "UPDATE mc_tile SET last_player_id = ?, last_report_ts = ?, "
            "paint_count = paint_count + 1 WHERE season_id = ? AND cell_id = ?",
            (player_id, ts, season_id, cell_id),
        )
        return PaintResult("reinforced", cell_id, team, score=new_score)

    # A different team owns the cell. It cannot flip inside the defense
    # window no matter the score -- this protects a freshly captured cell
    # from an immediate counter-attack. Outside the window, it flips only
    # if the attacker's decayed score is >= the CURRENT OWNER's decayed
    # score -- compared against that one team only, never the highest
    # scorer across all teams. With up to seven teams there is no single
    # rival, so each attacker is only measured against whoever is holding
    # the cell right now.
    if in_defense_window(conn, season_id, cell_id, ts):
        return PaintResult("attacked", cell_id, team, score=new_score, from_team=owner_team)

    owner_score = get_team_score(conn, season_id, cell_id, owner_team, ts)
    if new_score < owner_score:
        return PaintResult("attacked", cell_id, team, score=new_score, from_team=owner_team)

    # Flip. The losing team's score for this cell is discarded, the
    # capture clock restarts, and the change is written to the audit log.
    upsert_team_score(conn, season_id, cell_id, owner_team, 0.0, ts)
    conn.execute(
        "UPDATE mc_tile SET owner_team = ?, last_player_id = ?, last_report_ts = ?, "
        "paint_count = paint_count + 1 WHERE season_id = ? AND cell_id = ?",
        (team, player_id, ts, season_id, cell_id),
    )
    record_capture(conn, season_id, cell_id, team, ts)
    conn.execute(
        "INSERT INTO mc_tile_capture_log(season_id, cell_id, ts, by_player_id, by_team, from_team) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (season_id, cell_id, ts, player_id, team, owner_team),
    )
    log.info(
        "mc scoring: cell %s flipped %s -> %s by player %d",
        cell_id, owner_team, team, player_id,
    )
    return PaintResult("flipped", cell_id, team, score=new_score, from_team=owner_team)


def team_tile_counts(conn: sqlite3.Connection, season_id: int) -> dict[str, int]:
    """Tile count per team for a season -- squares held only, no
    check-in points folded in. Used by the API wherever the raw square
    count is shown on its own, and internally by team_totals() below.
    Not "the" team score by itself any more -- see team_totals().
    """
    rows = conn.execute(
        "SELECT owner_team, COUNT(*) AS c FROM mc_tile WHERE season_id = ? GROUP BY owner_team",
        (season_id,),
    ).fetchall()
    return {r["owner_team"]: r["c"] for r in rows}


def team_checkin_points(conn: sqlite3.Connection, season_id: int) -> dict[str, float]:
    """Net check-in points (app/checkin.py) earned per team for a
    season, summed by each awarded player's CURRENT team -- player.team
    is a permanent attribute (see app/join_api.py), not season-scoped,
    so this always reflects who a player plays for now, the same choice
    team_tile_counts() makes by reading mc_tile.owner_team live rather
    than a frozen roster.

    Never "the" team score on its own -- see team_totals() below, which
    is what every caller that needs "how is this team doing" should
    read instead. Note: mc_checkin_award has no protocol filter in this
    query -- season_id already IS protocol-specific by construction
    (see ensure_active_season), so a MeshCore season's id and a
    Meshtastic season's id never collide and never need a second filter
    here, matching every other season-scoped query in this module.
    """
    rows = conn.execute(
        "SELECT p.team AS team, SUM(a.points) AS pts "
        "  FROM mc_checkin_award a JOIN player p ON p.player_id = a.player_id "
        " WHERE a.season_id = ? GROUP BY p.team",
        (season_id,),
    ).fetchall()
    return {r["team"]: (r["pts"] or 0.0) for r in rows}


def team_totals(conn: sqlite3.Connection, season_id: int) -> dict[str, float]:
    """A team's full standing for a season: squares held
    (team_tile_counts) plus net check-in points earned
    (team_checkin_points). This is THE number for "how is this team
    doing" -- every place that decides a season standing or a season
    winner (maybe_roll_season below, and the live scoreboard routes in
    app/mc_api.py / app/api.py) reads this, not team_tile_counts()
    alone, so squares and check-ins are never silently only half
    counted somewhere -- a team's number has to mean the same thing
    everywhere it appears. The two component functions still exist and
    are still read on their own wherever the two figures need to be
    shown or stored separately (see mc_season_team_tally's
    tiles/checkin_points columns); this is their sum, not a replacement
    for either.
    """
    tiles = team_tile_counts(conn, season_id)
    points = team_checkin_points(conn, season_id)
    teams = set(tiles) | set(points)
    return {t: tiles.get(t, 0) + points.get(t, 0.0) for t in teams}


def ensure_active_season(conn: sqlite3.Connection, now: int, protocol: str) -> int:
    """Return the id of the active season for `protocol`, creating one
    running settings.mc_season_days from now if none exists yet.

    `protocol` scopes both the lookup and the INSERT below -- 'mc' and
    'mt' each run their own season independently, on their own clock,
    with their own winner. Filtering only the SELECT and letting the
    INSERT default the column would still be correct today (the column
    default is 'mc'), but it would silently do the wrong thing the
    moment this is ever called for 'mt', so the value is written
    explicitly here instead of relying on the schema default.
    """
    row = conn.execute(
        "SELECT id FROM mc_season WHERE protocol = ? AND status = 'active' "
        "ORDER BY id DESC LIMIT 1",
        (protocol,),
    ).fetchone()
    if row:
        return row["id"]
    ends_at = now + settings.mc_season_days * 86400
    conn.execute(
        "INSERT INTO mc_season(protocol, started_at, ends_at, status) "
        "VALUES (?, ?, ?, 'active')",
        (protocol, now, ends_at),
    )
    season_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    log.info(
        "mc scoring: created initial %s season id=%d ends_at=%d",
        protocol, season_id, ends_at,
    )
    return season_id


def maybe_roll_season(conn: sqlite3.Connection, now: int, protocol: str) -> bool:
    """If the active season for `protocol` has expired, close it and open
    a fresh one for the same protocol. Returns True if a roll happened.

    On close: tally each team's tile count AND check-in points into
    mc_season_team_tally and set the closing season's winner to
    whichever team has the highest COMBINED total (team_totals() --
    squares plus check-in points, see that function's docstring for
    why), or 'TIE' if there is no unique leader (including the case
    where nobody holds any tiles or points at all).

    Pruning of old MeshCore season data (tile/score/capture rows) is
    deferred for now -- nothing is deleted on rollover, matching how the
    Meshtastic side only prunes once a season count threshold is hit.
    """
    row = conn.execute(
        "SELECT id, ends_at FROM mc_season WHERE protocol = ? AND status = 'active' "
        "ORDER BY id DESC LIMIT 1",
        (protocol,),
    ).fetchone()
    if not row or now < row["ends_at"]:
        return False

    season_id = row["id"]
    tile_counts = team_tile_counts(conn, season_id)
    checkin_points = team_checkin_points(conn, season_id)
    totals = team_totals(conn, season_id)

    all_teams = set(settings.teams_list) | set(tile_counts.keys()) | set(checkin_points.keys())
    for team in all_teams:
        tiles = tile_counts.get(team, 0)
        pts = checkin_points.get(team, 0.0)
        conn.execute(
            "INSERT INTO mc_season_team_tally(season_id, team, tiles, checkin_points) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(season_id, team) DO UPDATE SET "
            "  tiles = excluded.tiles, checkin_points = excluded.checkin_points",
            (season_id, team, tiles, pts),
        )

    # Winner is decided on the COMBINED figure, not tiles alone -- see
    # team_totals()'s docstring. tiles/checkin_points are still stored
    # split above so a closed season's history can show where a team's
    # total came from.
    max_total = max(totals.values()) if totals else 0.0
    leaders = [t for t, v in totals.items() if v == max_total and max_total > 0]
    winner = leaders[0] if len(leaders) == 1 else "TIE"

    conn.execute(
        "UPDATE mc_season SET status = 'closed', winner = ? WHERE id = ?",
        (winner, season_id),
    )
    log.info(
        "mc scoring: closed season %d winner=%s totals=%s",
        season_id, winner, totals,
    )

    ends_at = now + settings.mc_season_days * 86400
    conn.execute(
        "INSERT INTO mc_season(protocol, started_at, ends_at, status) "
        "VALUES (?, ?, ?, 'active')",
        (protocol, now, ends_at),
    )
    new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    log.info("mc scoring: opened %s season %d ends_at=%d", protocol, new_id, ends_at)
    return True
