"""Standings and top captures of EVERY joined region (group) in one pass.

The per-request builders (mc_scoring.team_tile_counts & co, mc_api.top_for)
answer one region with one SQL pass per figure. Run for fifteen groups on
both boards that is sixty passes over the same few hundred thousand rows --
twelve seconds of a worker's time on production-sized data. Here each figure
is computed by ONE query per board that joins the region membership in (an
index probe on region_cell per row) and a small temporary table mapping
region code -> group number, and is aggregated by the database:

  squares      owned mc_tile rows of the active season
  exploration  Places Worth Going credits in the season's window, through
               the credited place's cells
  check-ins    awards of the season, through where they were placed
  captures     the season's capture log

A row belongs to a group when any of its cells is in any of the group's
regions (a check-in placed by airport code: when that code is one of them),
and counts ONCE per group however many of its regions match -- the same
rules as the per-request queries, and tests hold the two to identical
answers.

Memory: Python only ever sees the aggregated rows (a handful per group and
team, or per group and player), never the rows being counted. The first
version tallied every squares/captures row into dicts and sets in Python
(tens of MB at production size, on top of the worker's other work); the
database's own distinct/group sorters now do that, and temp_store is set to
FILE for the batch so even those spill to disk rather than the process heap.
"""
from __future__ import annotations

import logging
import time

from .db import connect
from .mc_api import MC_PROTOCOL, MT_PROTOCOL, active_season, regional_scores_payload, scores_for

log = logging.getLogger("regional_standings")

TOP_LIMIT = 20   # the same cap top_for() applies


def build_group_standings(targets) -> dict[str, dict | list]:
    """{cache key: payload} for every (slug, resolved) in `targets`: the
    four keys per group the web routes read (mc_scores:region:<slug>,
    mt_scores:..., mc_top:..., mt_top:...)."""
    out: dict[str, dict | list] = {}
    conn = connect()
    try:
        # BEFORE the temp table exists: changing temp_store drops temp objects.
        conn.execute("PRAGMA temp_store=FILE")
        conn.execute("CREATE TEMP TABLE _rg(code TEXT NOT NULL, gi INTEGER NOT NULL, PRIMARY KEY (code, gi)) WITHOUT ROWID")
        conn.executemany("INSERT INTO _rg(code, gi) VALUES (?, ?)",
                         [(code, gi) for gi, (_slug, resolved) in enumerate(targets) for code in resolved.codes])
        players = {r["player_id"]: (r["display_name"], r["team"])
                   for r in conn.execute("SELECT player_id, display_name, team FROM player")}
        n = len(targets)
        for tag, protocol in (("mc", MC_PROTOCOL), ("mt", MT_PROTOCOL)):
            t0 = time.monotonic()
            season = active_season(conn, protocol)
            if not season:
                for slug, resolved in targets:     # the same "no season" answers the per-request path gives
                    out[f"{tag}_scores:region:{slug}"] = scores_for(protocol, resolved)
                    out[f"{tag}_top:region:{slug}"] = []
                continue
            sid = season["id"]
            tiles = _per_group_team(conn, n, _TILES_SQL, (sid,))
            explorer = _per_group_team(conn, n, _EXPLORER_SQL, (protocol, season["started_at"], season["ends_at"]))
            checkins = _per_group_team(conn, n, _CHECKIN_SQL, (sid, sid))
            tops = _tops(conn, n, sid, players)
            for gi, (slug, resolved) in enumerate(targets):
                out[f"{tag}_scores:region:{slug}"] = regional_scores_payload(
                    season, resolved.key, tiles[gi], checkins[gi], explorer[gi])
                out[f"{tag}_top:region:{slug}"] = tops[gi]
            log.debug("group standings, %s board: %d groups in %.2f s", tag, n, time.monotonic() - t0)
    finally:
        try:
            conn.execute("PRAGMA temp_store=MEMORY")      # drops _rg and restores the pool's setting
        finally:
            conn.close()
    return out


# CROSS JOIN fixes the loop order: walk the rows being counted, probe
# region_cell by cell (idx_region_cell_cell), then _rg by region code.
_TILES_SQL = (
    "SELECT g.gi AS gi, t.owner_team AS team, COUNT(DISTINCT t.cell_id) AS v "
    "  FROM mc_tile t CROSS JOIN region_cell rc CROSS JOIN _rg g "
    " WHERE t.season_id = ? AND t.owner_team IS NOT NULL "
    "   AND rc.cell_id = t.cell_id AND g.code = rc.region_code "
    " GROUP BY g.gi, t.owner_team"
)

_EXPLORER_SQL = (
    "SELECT gi, team, SUM(pts) AS v FROM ("
    " SELECT DISTINCT g.gi AS gi, a.id AS aid, p.team AS team, a.points AS pts "
    "   FROM place_activation a CROSS JOIN player p CROSS JOIN place_cell pc CROSS JOIN region_cell rc CROSS JOIN _rg g "
    "  WHERE a.protocol = ? AND a.awarded_at >= ? AND a.awarded_at <= ? "
    "    AND p.player_id = a.player_id AND pc.place_id = a.place_id "
    "    AND rc.cell_id = pc.cell_id AND g.code = rc.region_code"
    ") GROUP BY gi, team"
)

# an award counts for a group through the cell it was placed in, or through
# an airport-code placement; UNION (not ALL) so one award counts once
_CHECKIN_SQL = (
    "SELECT gi, team, SUM(pts) AS v FROM ("
    " SELECT g.gi AS gi, a.player_id AS pid, a.net_date AS nd, p.team AS team, a.points AS pts "
    "   FROM mc_checkin_award a CROSS JOIN player p CROSS JOIN checkin_award_place cp CROSS JOIN _rg g "
    "  WHERE a.season_id = ? AND p.player_id = a.player_id "
    "    AND cp.season_id = a.season_id AND cp.player_id = a.player_id AND cp.net_date = a.net_date "
    "    AND g.code = cp.region_code "
    " UNION "
    " SELECT g.gi, a.player_id, a.net_date, p.team, a.points "
    "   FROM mc_checkin_award a CROSS JOIN player p CROSS JOIN checkin_award_place cp "
    "        CROSS JOIN region_cell rc CROSS JOIN _rg g "
    "  WHERE a.season_id = ? AND p.player_id = a.player_id "
    "    AND cp.season_id = a.season_id AND cp.player_id = a.player_id AND cp.net_date = a.net_date "
    "    AND rc.cell_id = cp.cell_id AND g.code = rc.region_code"
    ") GROUP BY gi, team"
)

_CAPTURES_SQL = (
    "SELECT gi, pid, COUNT(*) AS v FROM ("
    " SELECT DISTINCT g.gi AS gi, l.cell_id AS cell, l.ts AS ts, l.by_player_id AS pid "
    "   FROM mc_tile_capture_log l CROSS JOIN region_cell rc CROSS JOIN _rg g "
    "  WHERE l.season_id = ? AND l.by_player_id IS NOT NULL "
    "    AND rc.cell_id = l.cell_id AND g.code = rc.region_code"
    ") GROUP BY gi, pid"
)


def _per_group_team(conn, n, sql, params) -> list[dict]:
    out: list[dict] = [{} for _ in range(n)]
    for r in conn.execute(sql, params):
        out[r["gi"]][r["team"]] = r["v"] or 0
    return out


def _tops(conn, n, season_id, players) -> list[list[dict]]:
    per_group: list[list[tuple[int, int]]] = [[] for _ in range(n)]
    for r in conn.execute(_CAPTURES_SQL, (season_id,)):
        per_group[r["gi"]].append((r["pid"], r["v"]))
    tops = []
    for rows in per_group:
        out = []
        # most captures first; equal counts in player id order (what the
        # per-request GROUP BY hands back)
        for pid, caps in sorted(rows, key=lambda kv: (-kv[1], kv[0])):
            who = players.get(pid)
            if who is None:
                continue      # the per-request query joins player, so a player row must exist
            out.append({"display_name": who[0], "team": who[1], "captures": caps})
            if len(out) == TOP_LIMIT:
                break
        tops.append(out)
    return tops
