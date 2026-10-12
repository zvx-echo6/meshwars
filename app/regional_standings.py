"""Standings and top captures of EVERY joined region (group) in one pass.

The per-request builders (mc_scoring.team_tile_counts & co, mc_api.top_for)
answer one region with one SQL pass per figure. Run for fifteen groups on
both boards that is sixty passes over the same few hundred thousand rows --
twelve seconds of a worker's time on production-sized data. Here each figure
is read ONCE per board, with its region membership joined in (an index probe
on region_cell per row), and tallied per group in Python:

  squares      owned mc_tile rows of the active season
  exploration  Places Worth Going credits in the season's window, through
               the credited place's cells
  check-ins    awards of the season, through where they were placed
  captures     the season's capture log

A row belongs to a group when any of its cells is in any of the group's
regions (a check-in placed by airport code: when that code is one of them),
and counts ONCE per group however many of its regions match -- the same
rules as the per-request queries, and tests/test_regional_standings.py holds
the two to identical answers.
"""
from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict

from .db import connect
from .mc_api import MC_PROTOCOL, MT_PROTOCOL, active_season, regional_scores_payload, scores_for, top_for

log = logging.getLogger("regional_standings")

TOP_LIMIT = 20   # the same cap top_for() applies


def _groups_by_code(targets) -> dict[str, list[int]]:
    """region code -> indexes (into targets) of the groups that contain it."""
    out: dict[str, list[int]] = defaultdict(list)
    for gi, (_slug, resolved) in enumerate(targets):
        for code in resolved.codes:
            out[code].append(gi)
    return out


def build_group_standings(targets) -> dict[str, dict | list]:
    """{cache key: payload} for every (slug, resolved) in `targets`: the
    four keys per group the web routes read (mc_scores:region:<slug>,
    mt_scores:..., mc_top:..., mt_top:...)."""
    out: dict[str, dict | list] = {}
    by_code = _groups_by_code(targets)
    conn = connect()
    try:
        players = {r["player_id"]: (r["display_name"], r["team"])
                   for r in conn.execute("SELECT player_id, display_name, team FROM player")}
        for tag, protocol in (("mc", MC_PROTOCOL), ("mt", MT_PROTOCOL)):
            t0 = time.monotonic()
            season = active_season(conn, protocol)
            if not season:
                for slug, resolved in targets:     # the same "no season" answers the per-request path gives
                    out[f"{tag}_scores:region:{slug}"] = scores_for(protocol, resolved)
                    out[f"{tag}_top:region:{slug}"] = []
                continue
            sid = season["id"]
            tiles = _tile_counts(conn, sid, by_code, len(targets), players)
            explorer = _explorer_points(conn, protocol, season, by_code, len(targets))
            checkins = _checkin_points(conn, sid, by_code, len(targets))
            tops = _top_captures(conn, sid, by_code, len(targets), players)
            for gi, (slug, resolved) in enumerate(targets):
                out[f"{tag}_scores:region:{slug}"] = regional_scores_payload(
                    season, resolved.key, dict(tiles[gi]), dict(checkins[gi]), dict(explorer[gi]))
                out[f"{tag}_top:region:{slug}"] = tops[gi]
            log.debug("group standings, %s board: %d groups in %.2f s", tag, len(targets), time.monotonic() - t0)
    finally:
        conn.close()
    return out


def _tile_counts(conn, season_id, by_code, n, players):
    per_cell: dict[str, tuple[str, set[int]]] = {}
    for r in conn.execute(
        "SELECT t.cell_id AS cell, t.owner_team AS team, rc.region_code AS code "
        "  FROM mc_tile t JOIN region_cell rc ON rc.cell_id = t.cell_id "
        " WHERE t.season_id = ? AND t.owner_team IS NOT NULL", (season_id,)
    ):
        groups = by_code.get(r["code"])
        if not groups:
            continue
        got = per_cell.get(r["cell"])
        if got is None:
            per_cell[r["cell"]] = (r["team"], set(groups))
        else:
            got[1].update(groups)
    counts = [Counter() for _ in range(n)]
    for team, groups in per_cell.values():
        for gi in groups:
            counts[gi][team] += 1
    return counts


def _explorer_points(conn, protocol, season, by_code, n):
    per_act: dict[int, tuple[str, float, set[int]]] = {}
    for r in conn.execute(
        "SELECT a.id AS aid, a.points AS pts, p.team AS team, rc.region_code AS code "
        "  FROM place_activation a JOIN player p ON p.player_id = a.player_id "
        "  JOIN place_cell pc ON pc.place_id = a.place_id "
        "  JOIN region_cell rc ON rc.cell_id = pc.cell_id "
        " WHERE a.protocol = ? AND a.awarded_at >= ? AND a.awarded_at <= ?",
        (protocol, season["started_at"], season["ends_at"]),
    ):
        groups = by_code.get(r["code"])
        if not groups:
            continue
        got = per_act.get(r["aid"])
        if got is None:
            per_act[r["aid"]] = (r["team"], r["pts"] or 0.0, set(groups))
        else:
            got[2].update(groups)
    sums = [Counter() for _ in range(n)]
    for team, pts, groups in per_act.values():
        for gi in groups:
            sums[gi][team] += pts
    return sums


def _checkin_points(conn, season_id, by_code, n):
    per_award: dict[tuple, tuple[str, float, set[int]]] = {}
    for r in conn.execute(
        "SELECT a.player_id AS pid, a.net_date AS nd, a.points AS pts, p.team AS team, "
        "       cp.region_code AS rcode, rc.region_code AS code "
        "  FROM mc_checkin_award a JOIN player p ON p.player_id = a.player_id "
        "  JOIN checkin_award_place cp ON cp.season_id = a.season_id AND cp.player_id = a.player_id "
        "                              AND cp.net_date = a.net_date "
        "  LEFT JOIN region_cell rc ON rc.cell_id = cp.cell_id "
        " WHERE a.season_id = ?", (season_id,)
    ):
        key = (r["pid"], r["nd"])
        got = per_award.get(key)
        if got is None:
            got = per_award[key] = (r["team"], r["pts"] or 0.0, set())
        for code in (r["code"], r["rcode"]):
            if code:
                got[2].update(by_code.get(code, ()))
    sums = [Counter() for _ in range(n)]
    for team, pts, groups in per_award.values():
        for gi in groups:
            sums[gi][team] += pts
    return sums


def _top_captures(conn, season_id, by_code, n, players):
    per_log: dict[tuple, tuple[int, set[int]]] = {}
    for r in conn.execute(
        "SELECT l.cell_id AS cell, l.ts AS ts, l.by_player_id AS pid, rc.region_code AS code "
        "  FROM mc_tile_capture_log l JOIN region_cell rc ON rc.cell_id = l.cell_id "
        " WHERE l.season_id = ? AND l.by_player_id IS NOT NULL", (season_id,)
    ):
        groups = by_code.get(r["code"])
        if not groups:
            continue
        key = (r["cell"], r["ts"])
        got = per_log.get(key)
        if got is None:
            per_log[key] = (r["pid"], set(groups))
        else:
            got[1].update(groups)
    counts: list[Counter] = [Counter() for _ in range(n)]
    for pid, groups in per_log.values():
        for gi in groups:
            counts[gi][pid] += 1
    tops = []
    for c in counts:
        rows = []
        # most captures first; equal counts in player id order (what the
        # per-request GROUP BY hands back)
        for pid, caps in sorted(c.items(), key=lambda kv: (-kv[1], kv[0])):
            who = players.get(pid)
            if who is None:
                continue      # the per-request query joins player, so a player row must exist
            rows.append({"display_name": who[0], "team": who[1], "captures": caps})
            if len(rows) == TOP_LIMIT:
                break
        tops.append(rows)
    return tops
