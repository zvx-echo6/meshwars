"""Monthly results: standings and honors for one calendar month.

A season now runs six months (settings.season_days), which leaves five
months with nothing to show. Each calendar month closes with its own
result instead, served by the /results page.

Two things make this work, and both are easy to get wrong:

- A month is scored ON THE MONTH. Not a snapshot of season standings --
  that would name the same leader every month and stop meaning anything
  by October. Every figure here is ground taken or points earned between
  the month's own boundaries, so each month is a fresh contest inside
  the long season.
- A month is a CALENDAR month in settings.checkin_net_timezone, the
  same local clock net dates already use. Not an offset from a season
  start: the two boards began on different days, and one site must not
  hold two opinions about when August ended.

Everything here is derived from mc_tile_capture_log and
mc_checkin_award. Nothing new is written on the scoring path to support
it. app/db.py's month_* tables are a freeze of a finished month, not a
source of truth -- the month in progress is computed live from the same
rows, so the page is never stale, and a finished month is written once
so a later correction to history cannot silently rewrite a result
somebody already won.
"""
from __future__ import annotations

import logging
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import settings
from .grid import cell_center
from . import places

log = logging.getLogger("results")

# Awards, in the order the page shows them. The key is what goes in
# month_award.award; the label is what a reader sees.
TEAM_AWARDS = [
    ("month_winner", "Month Winner"),
    ("team_of_month", "Team of the Month"),
]
PLAYER_AWARDS = [
    ("top_attacker", "Top Attacker"),
    ("top_defender", "Top Defender"),
    ("top_phreak", "Top Phreak"),
    ("most_consistent", "Most Consistent"),
    ("quick_fingers", "Quick Fingers"),
    ("explorer", "Explorer"),
    ("frontier", "Frontier"),
]
PER_TEAM_AWARDS = [
    ("team_attacker", "Team Attacker"),
    ("team_defender", "Team Defender"),
]
AWARD_LABELS = dict(TEAM_AWARDS + PLAYER_AWARDS + PER_TEAM_AWARDS)

# Display order. compute_month() emits awards in this order naturally,
# but a frozen month is read back out of a table with no inherent order,
# and SQLite returned them alphabetised -- Explorer above Month Winner.
# Both paths sort through this so a finished month reads the same as the
# month it was.
_AWARD_RANK = {key: i for i, (key, _) in enumerate(TEAM_AWARDS + PLAYER_AWARDS + PER_TEAM_AWARDS)}


def _award_sort_key(a: dict) -> tuple:
    teams = settings.teams_list
    scope = a.get("scope") or ""
    return (
        _AWARD_RANK.get(a["award"], len(_AWARD_RANK)),
        teams.index(scope) if scope in teams else len(teams),
    )


# ---- month arithmetic --------------------------------------------------


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.checkin_net_timezone)


def month_key(ts: int) -> str:
    """The 'YYYY-MM' a unix timestamp falls in, locally."""
    return datetime.fromtimestamp(ts, tz=_tz()).strftime("%Y-%m")


def month_bounds(month: str) -> tuple[int, int]:
    """[start, end) unix timestamps for a local calendar month.

    Built from local midnight on the first of this month and local
    midnight on the first of the next, so a month that contains a
    daylight-saving change is still exactly the month -- an offset of
    30 or 31 fixed days would be an hour out on either side of it.
    """
    tz = _tz()
    year, mon = int(month[:4]), int(month[5:7])
    start = datetime(year, mon, 1, tzinfo=tz)
    nyear, nmon = (year + 1, 1) if mon == 12 else (year, mon + 1)
    end = datetime(nyear, nmon, 1, tzinfo=tz)
    return int(start.timestamp()), int(end.timestamp())


def previous_month(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    return "%04d-%02d" % ((year - 1, 12) if mon == 1 else (year, mon - 1))


def _net_window_open(net_date: str) -> int:
    """Unix time the net opened on this local date -- the moment a
    Quick Fingers offset is measured from."""
    tz = _tz()
    d = datetime.strptime(net_date, "%Y-%m-%d")
    return int(datetime(d.year, d.month, d.day, settings.checkin_net_start_hour, tzinfo=tz).timestamp())


# ---- raw material ------------------------------------------------------


def _captures(conn: sqlite3.Connection, protocol: str) -> list[sqlite3.Row]:
    """Every capture this protocol has ever recorded, oldest first.

    Deliberately unwindowed: Top Defender has to know what happened to a
    square BEFORE the month began to tell a retake from an ordinary
    attack, so the window is applied after the previous-owner lookup
    below, never in this query.
    """
    return conn.execute(
        "SELECT l.cell_id, l.ts, l.by_player_id, l.by_team, l.from_team, l.by_air "
        "  FROM mc_tile_capture_log l "
        "  JOIN mc_season s ON s.id = l.season_id "
        " WHERE s.protocol = ? "
        " ORDER BY l.cell_id, l.ts",
        (protocol,),
    ).fetchall()


def _windowed_captures(conn: sqlite3.Connection, protocol: str, start: int, end: int) -> list[dict]:
    """Captures inside the month, each tagged with whether it was a
    RETAKE: the same square changing hands back to the team that lost it
    last time.

    `retake` is what separates defending from attacking. Both are
    captures with a previous owner; a retake is one where the previous
    capture of that square took it FROM the team now taking it back.
    Walking the full per-cell history is why _captures() is unwindowed.
    """
    rows = _captures(conn, protocol)
    prev_from_by_cell: dict[str, str | None] = {}
    out: list[dict] = []
    for r in rows:
        prev_from = prev_from_by_cell.get(r["cell_id"])
        if start <= r["ts"] < end:
            out.append({
                "cell_id": r["cell_id"],
                "ts": r["ts"],
                "player_id": r["by_player_id"],
                "team": r["by_team"],
                "from_team": r["from_team"],
                "by_air": bool(r["by_air"]),
                "retake": prev_from is not None and prev_from == r["by_team"],
            })
        prev_from_by_cell[r["cell_id"]] = r["from_team"]
    return out


def _checkins(conn: sqlite3.Connection, protocol: str, month: str) -> list[sqlite3.Row]:
    """Check-in awards earned in this month, with the player's current
    team -- the same live-team choice mc_scoring.team_checkin_points()
    makes, so a month's figures and a season's always agree."""
    return conn.execute(
        "SELECT a.player_id, a.net_date, a.points, a.streak, a.message_ts, "
        "       p.team AS team, p.display_name AS display_name "
        "  FROM mc_checkin_award a "
        "  JOIN player p ON p.player_id = a.player_id "
        " WHERE a.protocol = ? AND substr(a.net_date, 1, 7) = ?",
        (protocol, month),
    ).fetchall()


def _names(conn: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    return {
        r["player_id"]: (r["display_name"], r["team"])
        for r in conn.execute("SELECT player_id, display_name, team FROM player")
    }


# ---- award helpers -----------------------------------------------------


def _top(counts: dict, minimum: float = 1):
    """The single highest entry, or None if nothing reached `minimum` or
    the lead is shared.

    Refusing a tie rather than picking one is the same rule the check-in
    identity bridge follows: a shared lead is not a winner, and an award
    nobody won is a fine outcome for a month. It also keeps the honors
    honest in a small league, where two people on one capture each would
    otherwise hand somebody a title for nothing.
    """
    if not counts:
        return None
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    if ranked[0][1] < minimum:
        return None
    if len(ranked) > 1 and ranked[1][1] == ranked[0][1]:
        return None
    return ranked[0]


def _award(award, key, value, names, detail=None, scope="", team=None):
    if key is None:
        return None
    if team is None and isinstance(key, int):
        team = names.get(key, (None, None))[1]
    return {
        "award": award,
        "label": AWARD_LABELS[award],
        "scope": scope,
        "player_id": key if isinstance(key, int) else None,
        "player": names.get(key, (None, None))[0] if isinstance(key, int) else None,
        "team": key if isinstance(key, str) else team,
        "value": float(value),
        "detail": detail,
    }


# ---- the month ---------------------------------------------------------


def compute_month(conn: sqlite3.Connection, protocol: str, month: str) -> dict:
    """Standings and honors for one month, derived from scratch."""
    start, end = month_bounds(month)
    caps = _windowed_captures(conn, protocol, start, end)
    chk = _checkins(conn, protocol, month)
    names = _names(conn)

    # ---- standings: captures made plus check-in points earned --------
    # Captures, not squares held: held is a snapshot and would just
    # re-report the season. A capture is worth one point, the same as a
    # held square is worth one in the season total, so the two numbers
    # stay in the same units.
    teams = {t: {"captures": 0, "checkin_points": 0.0} for t in settings.teams_list}
    for c in caps:
        teams.setdefault(c["team"], {"captures": 0, "checkin_points": 0.0})["captures"] += 1
    for r in chk:
        teams.setdefault(r["team"], {"captures": 0, "checkin_points": 0.0})["checkin_points"] += r["points"]
    standings = sorted(
        ({"team": t, "captures": v["captures"], "checkin_points": v["checkin_points"],
          "points": v["captures"] + v["checkin_points"]} for t, v in teams.items()),
        key=lambda s: (-s["points"], s["team"]),
    )

    awards: list[dict] = []

    def add(a):
        if a is not None:
            awards.append(a)

    # ---- team awards --------------------------------------------------
    add(_award("month_winner", *(_top({s["team"]: s["points"] for s in standings}, 0.001) or (None, 0)), names=names))
    add(_award("team_of_month", *(_top({s["team"]: s["captures"] for s in standings}) or (None, 0)), names=names))

    # ---- attack and defence -------------------------------------------
    attacks: dict[int, int] = {}
    retakes: dict[int, int] = {}
    per_team_attacks: dict[str, dict[int, int]] = {}
    per_team_retakes: dict[str, dict[int, int]] = {}
    virgin: dict[int, int] = {}
    for c in caps:
        pid, tm = c["player_id"], c["team"]
        if c["from_team"] is None:
            # Claimed from nobody. Aircraft excluded: Explorer is about
            # reach and effort, and a plane trivialises both.
            if not c["by_air"]:
                virgin[pid] = virgin.get(pid, 0) + 1
            continue
        attacks[pid] = attacks.get(pid, 0) + 1
        per_team_attacks.setdefault(tm, {})[pid] = per_team_attacks.setdefault(tm, {}).get(pid, 0) + 1
        if c["retake"]:
            retakes[pid] = retakes.get(pid, 0) + 1
            per_team_retakes.setdefault(tm, {})[pid] = per_team_retakes.setdefault(tm, {}).get(pid, 0) + 1

    add(_award("top_attacker", *(_top(attacks) or (None, 0)), names=names))
    add(_award("top_defender", *(_top(retakes) or (None, 0)), names=names))
    add(_award("explorer", *(_top(virgin) or (None, 0)), names=names))

    for team in settings.teams_list:
        add(_award("team_attacker", *(_top(per_team_attacks.get(team, {})) or (None, 0)),
                   names=names, scope=team, team=team))
        add(_award("team_defender", *(_top(per_team_retakes.get(team, {})) or (None, 0)),
                   names=names, scope=team, team=team))

    # ---- frontier: one square, the furthest that qualifies ------------
    # A single winner rather than a count: twenty miles past any town is
    # where mesh coverage runs out, so this is a trip somebody makes on
    # purpose, not something to grind. Expect months with no winner.
    best_cell, best_miles = None, 0.0
    for c in caps:
        if c["from_team"] is not None or c["by_air"]:
            continue
        lat, lon = cell_center(c["cell_id"])
        d = places.distance_to_nearest_town_m(lat, lon)
        if d is None:
            continue  # place data unavailable -- skip, never guess
        miles = d / places.MILE_M
        if miles > settings.frontier_miles and miles > best_miles:
            best_cell, best_miles = c, miles
    if best_cell is not None:
        add(_award("frontier", best_cell["player_id"], round(best_miles, 1), names,
                   detail="%s, %.0f mi out" % (best_cell["cell_id"], best_miles)))

    # ---- check-in awards ----------------------------------------------
    points: dict[int, float] = {}
    streaks: dict[int, int] = {}
    offsets: dict[int, list[float]] = {}
    for r in chk:
        points[r["player_id"]] = points.get(r["player_id"], 0.0) + r["points"]
        if r["streak"] is not None:
            streaks[r["player_id"]] = max(streaks.get(r["player_id"], 0), r["streak"])
        if r["message_ts"] is not None:
            offsets.setdefault(r["player_id"], []).append(
                r["message_ts"] - _net_window_open(r["net_date"])
            )

    add(_award("top_phreak", *(_top(points, 0.001) or (None, 0)), names=names))
    add(_award("most_consistent", *(_top(streaks) or (None, 0)), names=names))

    # Quick Fingers, and the guard that makes it survivable. An award for
    # being fastest on a scheduled event invites a cron job, so a player
    # whose check-in lands within a hair of the same offset every week is
    # skipped for this one award -- silently, and for this award only.
    # A false positive costs somebody a novelty prize; punishing on a
    # statistical guess would make the operator referee an argument
    # nobody can prove.
    speeds: dict[int, float] = {}
    for pid, offs in offsets.items():
        offs = [o for o in offs if o >= 0]
        if len(offs) < settings.quick_fingers_min_checkins:
            continue
        if (len(offs) >= settings.automation_min_samples
                and statistics.pstdev(offs) < settings.automation_stdev_seconds):
            log.info("results: player %d skipped for quick_fingers (offset stdev %.2fs over %d nets)",
                     pid, statistics.pstdev(offs), len(offs))
            continue
        speeds[pid] = -sum(offs) / len(offs)   # negated: fastest is the largest
    fastest = _top(speeds, float("-inf"))
    if fastest is not None:
        add(_award("quick_fingers", fastest[0], round(-fastest[1], 1), names,
                   detail="%.0f s after the net opened" % (-fastest[1])))

    return {"month": month, "protocol": protocol, "standings": standings, "awards": awards}


# ---- freeze ------------------------------------------------------------


def freeze_month(conn: sqlite3.Connection, protocol: str, month: str, now: int) -> None:
    """Write a finished month's result. Caller holds the write lock."""
    result = compute_month(conn, protocol, month)
    conn.execute("INSERT OR REPLACE INTO month_result(month, protocol, closed_at) VALUES (?, ?, ?)",
                 (month, protocol, now))
    conn.execute("DELETE FROM month_standing WHERE month = ? AND protocol = ?", (month, protocol))
    for s in result["standings"]:
        conn.execute(
            "INSERT INTO month_standing(month, protocol, team, captures, checkin_points, points) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (month, protocol, s["team"], s["captures"], s["checkin_points"], s["points"]),
        )
    conn.execute("DELETE FROM month_award WHERE month = ? AND protocol = ?", (month, protocol))
    for a in result["awards"]:
        conn.execute(
            "INSERT INTO month_award(month, protocol, award, scope, player_id, team, value, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (month, protocol, a["award"], a["scope"], a["player_id"], a["team"], a["value"], a["detail"]),
        )
    log.info("results: froze %s for %s (%d awards)", month, protocol, len(result["awards"]))


# Last local month each protocol was checked for pending freezes. The
# check below scans mc_tile_capture_log, and this is called from the
# ingest batch worker -- at a busy moment that is dozens of full scans a
# second over a table that only grows. Nothing it looks for can change
# except at a month boundary, so one check per protocol per month is
# exactly enough. Reset on restart, which costs one extra scan.
_LAST_CHECKED: dict[str, str] = {}


def maybe_roll_months(conn: sqlite3.Connection, now: int, protocol: str) -> int:
    """Freeze every finished month that has activity and no result yet.

    Called from the same places maybe_roll_season() is, so a month
    closes on whatever traffic arrives after the boundary rather than
    needing a scheduler of its own -- but at most once per protocol per
    month (see _LAST_CHECKED), since the ingest path calls this on every
    batch and the scan below only has new work to find at a boundary. Written to catch up rather than to
    fire exactly at midnight: if the service was down over a month end,
    or several months pass quietly, the next call still freezes each of
    them. Only months strictly before the current one are eligible, so
    the month in progress is never frozen early.
    """
    current = month_key(now)
    if _LAST_CHECKED.get(protocol) == current:
        return 0
    _LAST_CHECKED[protocol] = current

    have = {
        r["month"] for r in conn.execute(
            "SELECT month FROM month_result WHERE protocol = ?", (protocol,))
    }
    active = {
        r["m"] for r in conn.execute(
            "SELECT DISTINCT strftime('%Y-%m', l.ts, 'unixepoch') AS m "
            "  FROM mc_tile_capture_log l JOIN mc_season s ON s.id = l.season_id "
            " WHERE s.protocol = ?", (protocol,))
    } | {
        r["m"] for r in conn.execute(
            "SELECT DISTINCT substr(net_date, 1, 7) AS m FROM mc_checkin_award WHERE protocol = ?",
            (protocol,))
    }
    # strftime above works in UTC, which can name a neighbouring month for
    # a capture near a boundary. That only ever ADDS a candidate month --
    # every month with real activity is still in the set -- and an empty
    # month freezes to an empty result, so the imprecision is harmless
    # here and is not used for any figure.
    pending = sorted(m for m in active if m and m < current and m not in have)
    for month in pending:
        freeze_month(conn, protocol, month, now)
    return len(pending)


# ---- read --------------------------------------------------------------


def month_results_for(conn: sqlite3.Connection, protocol: str, now: int, limit: int = 12) -> dict:
    """FINISHED months only, most recent first, plus when the month in
    progress closes.

    The month in progress is deliberately absent. It can be computed --
    compute_month() will happily do it for any month, and the freeze
    below uses exactly that -- but showing it would turn every honor
    into a running total that changes daily, and a title you can watch
    slipping between two people all month is not a title. A month is
    judged once, when it is over.

    That does mean the page carries only the closing date until the
    first month ends. An empty page for a few weeks is the cost of an
    award that lands as an event.
    """
    current = month_key(now)
    _, closes_at = month_bounds(current)
    out: list[dict] = []

    rows = conn.execute(
        "SELECT month FROM month_result WHERE protocol = ? AND month < ? "
        " ORDER BY month DESC LIMIT ?",
        (protocol, current, max(limit, 1)),
    ).fetchall()
    for r in rows:
        month = r["month"]
        standings = [dict(x) for x in conn.execute(
            "SELECT team, captures, checkin_points, points FROM month_standing "
            " WHERE month = ? AND protocol = ? ORDER BY points DESC, team",
            (month, protocol))]
        awards = []
        for a in conn.execute(
            "SELECT award, scope, player_id, team, value, detail FROM month_award "
            " WHERE month = ? AND protocol = ?", (month, protocol)):
            d = dict(a)
            d["label"] = AWARD_LABELS.get(d["award"], d["award"])
            awards.append(d)
        names = _names(conn)
        for a in awards:
            a["player"] = names.get(a["player_id"], (None, None))[0] if a["player_id"] else None
        awards.sort(key=_award_sort_key)
        out.append({"month": month, "protocol": protocol, "standings": standings,
                    "awards": awards})
    return {
        "protocol": protocol,
        "open_month": current,
        "open_month_closes_at": closes_at,
        "months": out,
    }
