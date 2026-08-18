"""MeshCore fortress scoring per grid cell.

This is the MeshCore counterpart to app/scoring.py: same fortress-scoring
shape, but working on flat grid cells and players instead of geohashes and
radios, and supporting any number of teams instead of a fixed two.

Each team has an independent score per cell that:
- Increases, per qualifying paint, by the number of distinct repeaters
  that ping heard times settings.mc_points_per_repeater, capped at
  settings.mc_max_points_per_ping -- a ping that heard no repeaters
  reached no one and earns nothing (see app/mc_ingest.py for where the
  repeater count is parsed and turned into points before it reaches
  apply_paint here)
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
    "cooldown"   -- same player repainted the same cell too soon; nothing changed.
    "no_signal"  -- the ping heard zero repeaters, so it earned zero points;
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


def recently_painted(
    conn: sqlite3.Connection,
    player_id: int,
    cell_id: str,
    ts: int,
    window: int,
) -> bool:
    """True if this player already painted this EXACT cell within `window`
    seconds before ts. Read from player_cell_ping (player_id, protocol='mc',
    cell_id) with an exact cell_id match -- MeshCore cells are flat grid
    ids, not geohash prefixes, so the Meshtastic prefix-matching trick does
    not apply here.
    """
    row = conn.execute(
        "SELECT 1 FROM player_cell_ping"
        " WHERE player_id = ? AND protocol = 'mc' AND cell_id = ?"
        "   AND ts < ? AND ts >= ?"
        " LIMIT 1",
        (player_id, cell_id, ts, ts - window),
    ).fetchone()
    return row is not None


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
    points: float,
) -> PaintResult:
    """Score one accepted ping against a cell and resolve ownership.

    `points` is the score this specific ping earned -- repeaters heard
    times settings.mc_points_per_repeater, capped at
    settings.mc_max_points_per_ping, computed by the caller in
    app/mc_ingest.py from the ping's repeater fields. A ping that heard
    no repeaters reached no one, so points <= 0 here paints nothing,
    captures nothing, and touches no scores at all.

    Caller must already hold app.db's write lock and have an open write
    transaction on `conn` -- this function does neither itself.
    """
    if recently_painted(conn, player_id, cell_id, ts, settings.mc_cooldown_seconds):
        return PaintResult("cooldown", cell_id, team)

    if points <= 0:
        return PaintResult("no_signal", cell_id, team)

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
    """Tile count per team for a season, used by the API and by season
    rollover to compute the winner.
    """
    rows = conn.execute(
        "SELECT owner_team, COUNT(*) AS c FROM mc_tile WHERE season_id = ? GROUP BY owner_team",
        (season_id,),
    ).fetchall()
    return {r["owner_team"]: r["c"] for r in rows}


def ensure_active_season(conn: sqlite3.Connection, now: int) -> int:
    """Return the id of the active MeshCore season, creating one running
    settings.mc_season_days from now if none exists yet.
    """
    row = conn.execute(
        "SELECT id FROM mc_season WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row:
        return row["id"]
    ends_at = now + settings.mc_season_days * 86400
    conn.execute(
        "INSERT INTO mc_season(started_at, ends_at, status) VALUES (?, ?, 'active')",
        (now, ends_at),
    )
    season_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    log.info("mc scoring: created initial season id=%d ends_at=%d", season_id, ends_at)
    return season_id


def maybe_roll_season(conn: sqlite3.Connection, now: int) -> bool:
    """If the active MeshCore season has expired, close it and open a
    fresh one. Returns True if a roll happened.

    On close: tally each team's tile count into mc_season_team_tally and
    set the closing season's winner to whichever team holds the most
    tiles, or 'TIE' if there is no unique leader (including the case
    where nobody holds any tiles at all).

    Pruning of old MeshCore season data (tile/score/capture rows) is
    deferred for now -- nothing is deleted on rollover, matching how the
    Meshtastic side only prunes once a season count threshold is hit.
    """
    row = conn.execute(
        "SELECT id, ends_at FROM mc_season WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row or now < row["ends_at"]:
        return False

    season_id = row["id"]
    counts = team_tile_counts(conn, season_id)

    all_teams = set(settings.teams_list) | set(counts.keys())
    for team in all_teams:
        tiles = counts.get(team, 0)
        conn.execute(
            "INSERT INTO mc_season_team_tally(season_id, team, tiles) VALUES (?, ?, ?) "
            "ON CONFLICT(season_id, team) DO UPDATE SET tiles = excluded.tiles",
            (season_id, team, tiles),
        )

    max_tiles = max(counts.values()) if counts else 0
    leaders = [t for t, c in counts.items() if c == max_tiles and max_tiles > 0]
    winner = leaders[0] if len(leaders) == 1 else "TIE"

    conn.execute(
        "UPDATE mc_season SET status = 'closed', winner = ? WHERE id = ?",
        (winner, season_id),
    )
    log.info(
        "mc scoring: closed season %d winner=%s counts=%s",
        season_id, winner, counts,
    )

    ends_at = now + settings.mc_season_days * 86400
    conn.execute(
        "INSERT INTO mc_season(started_at, ends_at, status) VALUES (?, ?, 'active')",
        (now, ends_at),
    )
    new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    log.info("mc scoring: opened season %d ends_at=%d", new_id, ends_at)
    return True
