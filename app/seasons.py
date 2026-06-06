"""Season lifecycle: creation, end-of-season tallying, transitions, history.

Timing model: rolling 30 days from season start. Each season has
`started_at` and `ends_at` stored as epoch seconds (UTC).

End-of-season flow:
  1. Tally tiles per team, write red/blue/green/winner into the closing
     season row, set status='closed'.
  2. Insert a new season row (started_at=now, ends_at=now+30d, status='active').
  3. Run snake draft against the previous-season activity table; persist
     team_assignment rows for the new season.
  4. The new season starts with zero tile rows -> everything green/neutral.
  5. Prune history beyond HISTORY_MAX (12) closed seasons.

A separate concept (winner banner) is purely UI-facing: the most recent
closed season is shown on the map for WINNER_BANNER_HOURS after `ends_at`.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from .config import settings
from .db import WriteSession, connect
from .draft import snake_draft

log = logging.getLogger("seasons")


def now_s() -> int:
    return int(time.time())


def get_active_season(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM season WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def get_latest_closed_season(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM season WHERE status = 'closed' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def get_history(conn: sqlite3.Connection, limit: int = 12) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM season WHERE status = 'closed' "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def tally_tiles(conn: sqlite3.Connection, season_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT owner_team, COUNT(*) AS c FROM tile WHERE season_id = ? GROUP BY owner_team",
        (season_id,),
    ).fetchall()
    counts = {"RED": 0, "BLUE": 0, "GREEN": 0}
    for r in rows:
        counts[r["owner_team"]] = r["c"]
    return counts


async def ensure_initial_season() -> None:
    """If no active season exists, create one."""
    conn = connect()
    try:
        active = get_active_season(conn)
        if active:
            return
    finally:
        conn.close()

    async with WriteSession() as conn:
        # Re-check inside the write lock
        active = get_active_season(conn)
        if active:
            return
        start = now_s()
        end = start + settings.season_days * 86400
        conn.execute(
            "INSERT INTO season(started_at, ends_at, status) VALUES (?, ?, 'active')",
            (start, end),
        )
        season_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        log.info("created initial season id=%d ends_at=%d", season_id, end)


async def maybe_close_and_roll() -> bool:
    """If the active season has expired, close it and start a new one.

    Returns True if a transition happened.
    """
    conn = connect()
    try:
        active = get_active_season(conn)
        if not active:
            return False
        if now_s() < active["ends_at"]:
            return False
    finally:
        conn.close()

    log.info("season %d expired, rolling", active["id"])
    async with WriteSession() as conn:
        # Re-check inside the lock
        active = get_active_season(conn)
        if not active or now_s() < active["ends_at"]:
            return False

        season_id = active["id"]

        # 1. Tally
        counts = tally_tiles(conn, season_id)
        red, blue, green = counts["RED"], counts["BLUE"], counts["GREEN"]
        if red > blue:
            winner = "RED"
        elif blue > red:
            winner = "BLUE"
        else:
            winner = "TIE"

        conn.execute(
            "UPDATE season "
            "   SET status='closed', red_tiles=?, blue_tiles=?, green_tiles=?, winner=? "
            " WHERE id=?",
            (red, blue, green, winner, season_id),
        )
        log.info(
            "closed season %d: red=%d blue=%d green=%d winner=%s",
            season_id, red, blue, green, winner,
        )

        # 2. Pull activity for the snake draft. Eligibility:
        #    - has location data
        #    - is NOT an infrastructure role (routers, bases)
        from .config import settings
        excluded = ",".join(f"'{r}'" for r in settings.excluded_roles_set)
        excl_clause = f"AND (n.role IS NULL OR UPPER(n.role) NOT IN ({excluded}))" if excluded else ""
        activity_rows = conn.execute(
            f"SELECT a.node_id, a.packet_count "
            f"  FROM activity a "
            f"  JOIN node_seen n "
            f"    ON n.season_id = ? AND n.node_id = a.node_id "
            f"   AND n.lat IS NOT NULL AND n.lon IS NOT NULL "
            f"   {excl_clause} "
            f" WHERE a.window_id = ?",
            (season_id, season_id),
        ).fetchall()
        counts_map: dict[int, int] = {
            r["node_id"]: r["packet_count"] for r in activity_rows
        }
        log.info("draft pool: %d nodes with activity + location", len(counts_map))

        # 3. New season row
        start = now_s()
        end = start + settings.season_days * 86400
        conn.execute(
            "INSERT INTO season(started_at, ends_at, status) VALUES (?, ?, 'active')",
            (start, end),
        )
        new_season_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

        # 4. Snake draft
        draft = snake_draft(counts_map)
        for entry in draft:
            conn.execute(
                "INSERT INTO team_assignment(season_id, node_id, team, activity_score) "
                "VALUES (?, ?, ?, ?)",
                (new_season_id, entry.node_id, entry.team, entry.activity_score),
            )
        log.info(
            "drafted %d nodes for season %d", len(draft), new_season_id,
        )

        # Clear samples that belonged to the closed season (we only keep current)
        conn.execute("DELETE FROM sample WHERE season_id = ?", (season_id,))
        # Tile rows for the closed season are kept until history pruning removes them.
        # Activity for the new window starts fresh.
        conn.execute(
            "DELETE FROM activity WHERE window_id != ?",
            (new_season_id,),
        )

        # 5. Prune history beyond HISTORY_MAX closed seasons
        excess = conn.execute(
            "SELECT id FROM season "
            " WHERE status='closed' "
            " ORDER BY id DESC LIMIT -1 OFFSET ?",
            (settings.history_max,),
        ).fetchall()
        for r in excess:
            conn.execute("DELETE FROM tile WHERE season_id = ?", (r["id"],))
            conn.execute("DELETE FROM team_assignment WHERE season_id = ?", (r["id"],))
            conn.execute("DELETE FROM node_seen WHERE season_id = ?", (r["id"],))
            conn.execute("DELETE FROM season WHERE id = ?", (r["id"],))

    return True


def get_team_assignments(conn: sqlite3.Connection, season_id: int) -> dict[int, str]:
    rows = conn.execute(
        "SELECT node_id, team FROM team_assignment WHERE season_id = ?",
        (season_id,),
    ).fetchall()
    return {r["node_id"]: r["team"] for r in rows}


def winner_banner_active(closed_season: dict | None) -> bool:
    """Is the winner banner still inside its 72h display window?"""
    if not closed_season:
        return False
    ends_at = closed_season["ends_at"]
    return now_s() < ends_at + settings.winner_banner_hours * 3600
