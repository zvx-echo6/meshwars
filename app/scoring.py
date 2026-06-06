"""Fortress scoring per tile.

Each team has an independent score per tile that:
- Increases by +0.5 per qualifying position packet from that team
- Increases by +1.0 the first time a given node from that team paints there
- Decays at -0.25/day toward zero (floor=0)

Ownership rules:
- A tile's owner_team is whichever team most recently captured it.
- Captures are gated by a 15-minute defense window: during that window,
  the tile cannot be flipped at all (attacker score still accumulates).
- After the window: flip happens when attacker score >= defender score.
- On flip: defender score discarded, attacker score becomes the new
  defender score, captured_at = now.

Tiles never go back to neutral once captured.
"""
from __future__ import annotations

import sqlite3
from .config import settings


SECONDS_PER_DAY = 86400.0


def decayed_score(stored_score: float, last_update_ts: int, now_ts: int) -> float:
    """Apply linear decay from last update to now. Floor at 0."""
    if last_update_ts >= now_ts:
        return stored_score
    elapsed_days = (now_ts - last_update_ts) / SECONDS_PER_DAY
    decayed = stored_score - (settings.score_decay_per_day * elapsed_days)
    return max(0.0, decayed)


def get_team_score(
    conn: sqlite3.Connection,
    season_id: int,
    geohash: str,
    team: str,
    now_ts: int,
) -> float:
    """Return the decayed score for (tile, team) as of now_ts."""
    row = conn.execute(
        "SELECT score, last_update FROM tile_score "
        " WHERE season_id = ? AND geohash = ? AND team = ?",
        (season_id, geohash, team),
    ).fetchone()
    if not row:
        return 0.0
    return decayed_score(row["score"], row["last_update"], now_ts)


def upsert_team_score(
    conn: sqlite3.Connection,
    season_id: int,
    geohash: str,
    team: str,
    new_score: float,
    now_ts: int,
) -> None:
    """Persist a team's score and last_update for a tile."""
    conn.execute(
        "INSERT INTO tile_score(season_id, geohash, team, score, last_update) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(season_id, geohash, team) DO UPDATE SET "
        "  score = excluded.score, last_update = excluded.last_update",
        (season_id, geohash, team, max(0.0, new_score), now_ts),
    )


def is_first_paint_for_node(
    conn: sqlite3.Connection,
    season_id: int,
    geohash: str,
    team: str,
    node_id: int,
    ts: int,
) -> bool:
    """Check (and atomically record) whether this node has painted this
    tile for this team before. Returns True the FIRST time, False
    afterwards. Always bumps the paint_count.
    """
    row = conn.execute(
        "SELECT 1 FROM tile_unique_painter "
        " WHERE season_id = ? AND geohash = ? AND team = ? AND node_id = ?",
        (season_id, geohash, team, node_id),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE tile_unique_painter "
            "   SET paint_count = paint_count + 1 "
            " WHERE season_id = ? AND geohash = ? AND team = ? AND node_id = ?",
            (season_id, geohash, team, node_id),
        )
        return False
    conn.execute(
        "INSERT INTO tile_unique_painter(season_id, geohash, team, node_id, first_ts, paint_count) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (season_id, geohash, team, node_id, ts),
    )
    return True


def in_defense_window(
    conn: sqlite3.Connection,
    season_id: int,
    geohash: str,
    now_ts: int,
) -> bool:
    """True if the tile is inside its 15-minute post-capture defense window."""
    row = conn.execute(
        "SELECT captured_at FROM tile_capture "
        " WHERE season_id = ? AND geohash = ?",
        (season_id, geohash),
    ).fetchone()
    if not row:
        return False
    return (now_ts - row["captured_at"]) < settings.defense_window_seconds


def record_capture(
    conn: sqlite3.Connection,
    season_id: int,
    geohash: str,
    team: str,
    ts: int,
) -> None:
    """Stamp the tile as captured by this team at this time."""
    conn.execute(
        "INSERT INTO tile_capture(season_id, geohash, captured_at, captured_by_team) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(season_id, geohash) DO UPDATE SET "
        "  captured_at = excluded.captured_at, "
        "  captured_by_team = excluded.captured_by_team",
        (season_id, geohash, ts, team),
    )
