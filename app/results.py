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
import time
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
]
PLAYER_AWARDS = [
    ("top_attacker", "Top Attacker"),
    ("top_defender", "Top Defender"),
    ("quick_fingers", "Quick Fingers"),
    ("tourist", "Tourist"),
    ("park_hopper", "Park Hopper"),
    ("peak_tagger", "Peak Tagger"),
    ("frontier", "Frontier"),
]
PER_TEAM_AWARDS = [
    ("team_attacker", "Team Attacker"),
    ("team_defender", "Team Defender"),
]
AWARD_LABELS = dict(TEAM_AWARDS + PLAYER_AWARDS + PER_TEAM_AWARDS)

# Retired: no longer computed for new months, but a month frozen while it
# still existed keeps its month_award row forever (frozen months are
# never rewritten), so its label stays here rather than falling back to
# the raw award key on the page.
#
# most_consistent (longest run of consecutive nets) came down 2026-08-25:
# a month is about four nets, and nearly everyone who shows up hits all
# four, so it was a tie among most of the field and told you nothing.
# Check-in streaks already pay points for showing up every week (5 per
# consecutive week, capped at 25) -- the same thing measured somewhere it
# can actually vary.
AWARD_LABELS["most_consistent"] = "Most Consistent"

# top_netop (most points from weekly net check-ins) came down 2026-08-25:
# the streak bonus pays 5 points per consecutive week, capped at 25, so
# whoever started their streak earliest pulls ahead by an amount a
# newcomer can never close in a single month. That makes the award a
# record of seniority rather than a contest -- players still earn streak
# points, only the award for topping them is gone.
AWARD_LABELS["top_netop"] = "Top NetOp"

# explorer (most place_activation points earned this month) came down
# 2026-08-25: points are capped at 100 per person per week, so everyone
# who plays seriously ends a month within the same narrow band and the
# award separates nobody -- the same ceiling that retired Most
# Consistent. Replaced by three awards that count VISITS instead of
# points -- Tourist, Park Hopper, Peak Tagger, one per place type --
# because the weekly cap makes them mutually exclusive in practice (a
# remote summit alone spends the whole week) and so each one now
# describes a real, distinct playstyle instead of three views of the
# same points grind.
#
# The NAME survives the award, exactly as NetOps did: Explorer is still
# the season-long Places Worth Going points ranking (the Explorer tab
# under Top Operators, `explorer_points` in app/public_api._player_rows,
# folded into a player's total_points in app/mc_api) -- and BECAUSE it
# feeds the season total score it cannot be month-scoped or reset the
# way an honour here is. The label below exists purely so months frozen
# while the award still ran display a real name.
AWARD_LABELS["explorer"] = "Explorer"

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


def _held_at(conn: sqlite3.Connection, protocol: str, at_ts: int) -> dict[str, int]:
    """Squares each team OWNS at `at_ts` -- the same quantity the live
    scoreboard shows, reconstructed at a chosen instant.

    A month is scored on ground HELD when it closes, not on capture
    events. A square that changed hands five times is one square, and
    ground taken and then lost is ground you do not have. Counting
    events instead put this page in different units from the scoreboard
    and inflated every figure (RED read 58% high in August 2026), which
    is the whole reason this helper exists -- see compute_month().

    The reconstruction is exact because a cell has no neutral state
    (see app/mc_scoring.py): once captured it always has an owner, so
    the newest capture at or before `at_ts` names the owner then. Scoped
    to the season active at that instant, because a season boundary
    clears the board and counting across one would be meaningless.
    """
    season = conn.execute(
        "SELECT id FROM mc_season "
        " WHERE protocol = ? AND started_at <= ? "
        " ORDER BY started_at DESC LIMIT 1",
        (protocol, at_ts),
    ).fetchone()
    if season is None:
        return {}
    return {
        r["team"]: r["n"]
        for r in conn.execute(
            "SELECT by_team AS team, COUNT(*) AS n FROM ("
            "  SELECT cell_id, by_team,"
            "         ROW_NUMBER() OVER (PARTITION BY cell_id"
            "                            ORDER BY ts DESC, rowid DESC) AS rn"
            "    FROM mc_tile_capture_log"
            "   WHERE season_id = ? AND ts <= ?"
            ") WHERE rn = 1 GROUP BY by_team",
            (season["id"], at_ts),
        )
    }


def _checkins(conn: sqlite3.Connection, protocol: str, month: str) -> list[sqlite3.Row]:
    """Check-in awards earned in this month, with the player's current
    team -- the same live-team choice mc_scoring.team_checkin_points()
    makes, so a month's figures and a season's always agree."""
    return conn.execute(
        "SELECT a.player_id, a.net_date, a.points, a.message_ts, "
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
    """One honor row. `detail` says what the number counts -- every award
    carries one, because "Top NetOp 130" is a figure with no unit and
    the awards whose unit is not guessable from the name are exactly the
    ones a reader has to guess at."""
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


def compute_month(conn: sqlite3.Connection, protocol: str, month: str,
                  now: int | None = None) -> dict:
    """Standings and honors for one month, derived from scratch.

    `now` only matters for a month still being played, where ground
    held "at the close" can only mean as of now; it defaults to the
    clock. A finished month always reads its own end.
    """
    start, end = month_bounds(month)
    caps = _windowed_captures(conn, protocol, start, end)
    chk = _checkins(conn, protocol, month)
    names = _names(conn)

    # ---- standings: ground HELD at the close, plus check-in points ----
    # Squares held, not captures made. These are the units the scoreboard
    # is in (mc_scoring.team_tile_counts), so the two pages finally agree;
    # counting capture events instead let one square score many times and
    # kept crediting ground a team had already lost.
    #
    # For a month still being played "the close" can only mean now, which
    # is what the preview on a preview host shows.
    # end is the first instant of the NEXT month, so the close is end-1.
    at = min(end - 1, now if now is not None else int(time.time()))
    held = _held_at(conn, protocol, at)

    teams = {t: {"squares": 0, "checkin_points": 0.0} for t in settings.teams_list}
    for t, n in held.items():
        teams.setdefault(t, {"squares": 0, "checkin_points": 0.0})["squares"] = n
    for r in chk:
        teams.setdefault(r["team"], {"squares": 0, "checkin_points": 0.0})["checkin_points"] += r["points"]
    standings = sorted(
        ({"team": t, "squares": v["squares"], "checkin_points": v["checkin_points"],
          "points": v["squares"] + v["checkin_points"]} for t, v in teams.items()),
        key=lambda s: (-s["points"], s["team"]),
    )

    awards: list[dict] = []

    def add(a):
        if a is not None:
            awards.append(a)

    # ---- team awards --------------------------------------------------
    # One team award, not two. A "most captures" award alongside this one
    # names the same team almost every month -- captures dominate the
    # points total -- and two team titles that usually agree is a thing to
    # explain rather than a thing to win.
    add(_award("month_winner", *(_top({s["team"]: s["points"] for s in standings}, 0.001) or (None, 0)),
               names=names, detail="points this month"))

    # ---- attack and defence -------------------------------------------
    attacks: dict[int, int] = {}
    retakes: dict[int, int] = {}
    per_team_attacks: dict[str, dict[int, int]] = {}
    per_team_retakes: dict[str, dict[int, int]] = {}
    for c in caps:
        pid, tm = c["player_id"], c["team"]
        if c["from_team"] is None:
            # Claimed from nobody -- not an attack or a retake, either.
            continue
        attacks[pid] = attacks.get(pid, 0) + 1
        per_team_attacks.setdefault(tm, {})[pid] = per_team_attacks.setdefault(tm, {}).get(pid, 0) + 1
        if c["retake"]:
            retakes[pid] = retakes.get(pid, 0) + 1
            per_team_retakes.setdefault(tm, {})[pid] = per_team_retakes.setdefault(tm, {}).get(pid, 0) + 1

    add(_award("top_attacker", *(_top(attacks) or (None, 0)), names=names,
               detail="squares taken from other teams"))
    add(_award("top_defender", *(_top(retakes) or (None, 0)), names=names,
               detail="squares taken back"))

    # ---- tourist / park hopper / peak tagger: most VISITS this month ---
    # One award per place type (docs/features/places.md), counting rows
    # in place_activation joined to place.ref_type, scoped by awarded_at
    # falling inside the month -- the same time-only scoping every other
    # award here uses. place_activation has no protocol column (a
    # scoring ping credits places the same way regardless of which
    # board sent it -- see app/place_scoring.py), so this does not
    # filter by protocol either, matching that existing convention.
    #
    # A VISIT, not a point total: place.points is never read here.
    # Replaced "explorer" (most place points earned that month) because
    # the 100-point weekly cap meant everyone who played seriously ended
    # the month within the same narrow band -- the award separated
    # nobody, the same flaw that retired Most Consistent. Counting raw
    # visits instead, split by type, works because the same weekly cap
    # makes the three mutually exclusive in practice: one remote summit
    # is worth the whole week on its own, so a summit-chaser gets about
    # one a week, while a landmark-hunter needs twenty. Each award now
    # describes a real, distinct playstyle instead of three views of the
    # same points grind.
    for award, ref_type, label_detail in (
        ("tourist", "landmark", "landmarks visited"),
        ("park_hopper", "park", "parks visited"),
        ("peak_tagger", "summit", "summits visited"),
    ):
        visits: dict[int, int] = dict(conn.execute(
            "SELECT a.player_id, COUNT(*) FROM place_activation a "
            "  JOIN place p ON p.id = a.place_id "
            " WHERE p.ref_type = ? AND a.awarded_at >= ? AND a.awarded_at < ? "
            " GROUP BY a.player_id",
            (ref_type, start, end),
        ).fetchall())
        add(_award(award, *(_top(visits) or (None, 0)), names=names, detail=label_detail))

    for team in settings.teams_list:
        add(_award("team_attacker", *(_top(per_team_attacks.get(team, {})) or (None, 0)),
                   names=names, scope=team, team=team, detail="squares taken from other teams"))
        add(_award("team_defender", *(_top(per_team_retakes.get(team, {})) or (None, 0)),
                   names=names, scope=team, team=team, detail="squares taken back"))

    # ---- frontier: how much ground you claimed out past the towns -----
    # Counted, not measured. The furthest single square rewarded one
    # lucky turn-off; a count rewards actually working the back country.
    #
    # No virgin-ground restriction: that only ever existed to keep
    # Frontier a strict subset of the old squares-nobody-had-claimed
    # Explorer. Explorer has since been retired outright (2026-08-25,
    # replaced by Tourist/Park Hopper/Peak Tagger above) and never
    # measured squares in the first place, but Frontier's own rule --
    # every out-of-town capture counts here, attack or retake or virgin
    # claim alike -- was unaffected by either change and stays as it
    # is. Aircraft are still excluded, same as everywhere else that
    # measures reach and effort.
    #
    # Still expect empty months: twenty miles past a town is where mesh
    # coverage runs out, and a square out there has to hear a repeater
    # to have been claimed at all.
    frontier: dict[int, int] = {}
    furthest: dict[int, float] = {}
    for c in caps:
        if c["by_air"]:
            continue
        lat, lon = cell_center(c["cell_id"])
        d = places.distance_to_nearest_town_m(lat, lon)
        if d is None:
            continue  # place data unavailable -- skip, never guess
        miles = d / places.MILE_M
        if miles > settings.frontier_miles:
            pid = c["player_id"]
            frontier[pid] = frontier.get(pid, 0) + 1
            furthest[pid] = max(furthest.get(pid, 0.0), miles)
    won = _top(frontier)
    if won is not None:
        add(_award("frontier", won[0], won[1], names,
                   detail="squares past the towns, furthest %.0f mi out" % furthest[won[0]]))

    # ---- check-in awards ----------------------------------------------
    offsets: dict[int, list[float]] = {}
    for r in chk:
        if r["message_ts"] is not None:
            offsets.setdefault(r["player_id"], []).append(
                r["message_ts"] - _net_window_open(r["net_date"])
            )

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
    result = compute_month(conn, protocol, month, now)
    conn.execute("INSERT OR REPLACE INTO month_result(month, protocol, closed_at) VALUES (?, ?, ?)",
                 (month, protocol, now))
    conn.execute("DELETE FROM month_standing WHERE month = ? AND protocol = ?", (month, protocol))
    for s in result["standings"]:
        conn.execute(
            "INSERT INTO month_standing(month, protocol, team, squares, checkin_points, points) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (month, protocol, s["team"], s["squares"], s["checkin_points"], s["points"]),
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
            "SELECT team, squares, checkin_points, points FROM month_standing "
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

    # Preview hosts only (settings.results_preview_current_month, off by
    # default -- see app/config.py). The month in progress is computed
    # live and prepended, marked so the frontend can label it
    # provisional. compute_month() is pure SELECTs, so this writes
    # nothing and never freezes the open month; the frozen months above
    # are returned exactly as they were, without a "preview" key.
    if settings.results_preview_current_month:
        live = compute_month(conn, protocol, current)
        live["awards"] = sorted(live.get("awards") or [], key=_award_sort_key)
        live["preview"] = True
        out.insert(0, live)

    return {
        "protocol": protocol,
        "open_month": current,
        "open_month_closes_at": closes_at,
        "months": out,
    }
