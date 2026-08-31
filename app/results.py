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
from .grid import cell_bounds, cell_center
from . import places

log = logging.getLogger("results")

# Awards, in the order the page shows them. The key is what goes in
# month_award.award; the label is what a reader sees.
TEAM_AWARDS = [
    ("largest_territory", "Largest Territory"),
    ("longest_road", "Longest Road"),
]
PLAYER_AWARDS = [
    ("empire_builder", "Empire Builder"),
    ("top_attacker", "Top Attacker"),
    ("top_defender", "Top Defender"),
    ("quick_fingers", "Quick Fingers"),
    ("tourist", "Tourist"),
    ("park_hopper", "Park Hopper"),
    ("peak_tagger", "Peak Tagger"),
    ("frontier", "Frontier"),
]
PER_TEAM_AWARDS = [
    ("team_builder", "Team Builder"),
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

# "Month Winner" was the team with the most points, where points meant
# captures plus check-ins. Scoring moved to ground held (2026-08-31) and
# the award moved with it: Largest Territory, the team holding the most
# squares when the month closes. Label kept so a month frozen under the
# old name still reads as one.
AWARD_LABELS["month_winner"] = "Month Winner"

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


def with_placeholders(awards: list[dict]) -> list[dict]:
    """Every league-wide honor listed, won or not, in display order.

    An award that silently disappears reads as an award that does not
    exist: Peak Tagger was absent for the whole of August 2026 and looked
    broken, when in fact nobody had reached a summit (each of the 4,851
    summits is a single square you have to stand on). A placeholder
    carries no winner -- player_id and team both None -- and the page
    says so rather than hiding the row.

    Placeholders are NEVER stored. month_award.value is NOT NULL, and a
    frozen month should record what was won, not what wasn't; they are
    added on the way out, by both compute_month() and the frozen read
    path, so the two render identically.

    Per-team awards get no placeholder -- the By team table already draws
    a dash for a team missing one.
    """
    won = {a["award"] for a in awards if not a.get("scope")}
    out = list(awards)
    for key, label in TEAM_AWARDS + PLAYER_AWARDS:
        if key not in won:
            out.append({"award": key, "label": label, "scope": "",
                        "player_id": None, "player": None, "team": None,
                        "value": None, "detail": None})
    out.sort(key=_award_sort_key)
    return out


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


def _ownership_at(conn: sqlite3.Connection, protocol: str, at_ts: int) -> list[sqlite3.Row]:
    """Who owns every square at `at_ts`: one row per cell, carrying the
    owning team and the player whose paint put it there.

    Exact, because a cell has no neutral state (see app/mc_scoring.py):
    once captured it always has an owner, so the newest capture at or
    before `at_ts` names both the owner and the last painter then.
    Scoped to the season active at that instant, because a season
    boundary clears the board and counting across one is meaningless.

    Three things read this: the standings (grouped by team -- the same
    figure the scoreboard shows), Empire Builder (grouped by player, so
    the per-player numbers add up to the team's own), and Longest Road
    (the cell ids themselves).
    """
    season = conn.execute(
        "SELECT id FROM mc_season "
        " WHERE protocol = ? AND started_at <= ? "
        " ORDER BY started_at DESC LIMIT 1",
        (protocol, at_ts),
    ).fetchone()
    if season is None:
        return []
    return conn.execute(
        "SELECT cell_id, by_team AS team, by_player_id AS player_id FROM ("
        "  SELECT cell_id, by_team, by_player_id,"
        "         ROW_NUMBER() OVER (PARTITION BY cell_id"
        "                            ORDER BY ts DESC, rowid DESC) AS rn"
        "    FROM mc_tile_capture_log"
        "   WHERE season_id = ? AND ts <= ?"
        ") WHERE rn = 1",
        (season["id"], at_ts),
    ).fetchall()


# The eight neighbours of a square: sides AND corners. Corner contact
# counts because a road crossing the grid diagonally is still one road.
_NEIGHBOURS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def _longest_road_path(cells: set[tuple[int, int]]) -> list[tuple[int, int]]:
    """The longest unbroken chain of touching squares in `cells`, as the
    squares themselves in order along the chain.

    Cell ids are "<latIdx>_<lonIdx>" on a fixed grid (app/grid.py), so
    adjacency is plain integer arithmetic. Two squares are linked if they
    touch on a side or a corner.

    Found by walking to the furthest square of each connected patch, then
    walking to the furthest square from THERE and following parent
    pointers back. That is exact for a chain and can only understate a
    patch with loops in it, which is the safe direction to be wrong in:
    it never claims a road longer than one that exists. It is also what
    makes the award mean something, since a big round blob scores near
    its width while a thin run along a highway scores its whole length --
    in August 2026 RED led with 330 squares while holding half of GREEN's
    ground.

    Returns the path rather than just its length so the award can be
    drawn on the map (app/mc_api's award geometry route); _longest_road()
    below is the length of it, so the number on the results page and the
    line on the map can never disagree.

    Linear in the number of squares: each patch is walked twice and never
    revisited.
    """
    from collections import deque

    def walk(src):
        parent = {src: None}
        q = deque([src])
        far = src
        depth = {src: 0}
        while q:
            cur = q.popleft()
            for dy, dx in _NEIGHBOURS:
                nxt = (cur[0] + dy, cur[1] + dx)
                if nxt in cells and nxt not in parent:
                    parent[nxt] = cur
                    depth[nxt] = depth[cur] + 1
                    q.append(nxt)
                    if depth[nxt] > depth[far]:
                        far = nxt
        return far, parent

    seen: set[tuple[int, int]] = set()
    best: list[tuple[int, int]] = []
    for cell in cells:
        if cell in seen:
            continue
        end_cell, reached = walk(cell)
        seen |= reached.keys()
        far, parent = walk(end_cell)
        path = []
        node = far
        while node is not None:
            path.append(node)
            node = parent[node]
        if len(path) > len(best):
            best = path
    return best


def _longest_road(cells: set[tuple[int, int]]) -> int:
    """Length, in squares, of the longest unbroken chain a team holds --
    see _longest_road_path() for how it is found and why."""
    return len(_longest_road_path(cells))


def _cell_xy(cell_id: str) -> tuple[int, int] | None:
    """"<latIdx>_<lonIdx>" as integers, or None if it is not that shape.
    Nothing else in the codebase parses a cell id this way -- grid.py
    hands back coordinates in degrees, and the road walk needs indices."""
    try:
        lat_str, lon_str = cell_id.split("_")
        return int(lat_str), int(lon_str)
    except (ValueError, AttributeError):
        return None


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


def _place_points(conn: sqlite3.Connection, protocol: str, start: int, end: int) -> dict[str, float]:
    """Places Worth Going points earned inside the month, per team,
    summed by each activation's player's CURRENT team -- the same live
    team choice _checkins() and mc_scoring.team_place_points() make.

    Scoped to the board that earned it, same as the place honors -- both
    boards used to report an identical exploration figure because
    place_activation carried no protocol.
    """
    return {
        r["team"]: r["pts"] or 0.0
        for r in conn.execute(
            "SELECT p.team AS team, SUM(a.points) AS pts "
            "  FROM place_activation a "
            "  JOIN player p ON p.player_id = a.player_id "
            " WHERE a.protocol = ? AND a.awarded_at >= ? AND a.awarded_at < ? "
            " GROUP BY p.team",
            (protocol, start, end),
        )
    }


def _names(conn: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    return {
        r["player_id"]: (r["display_name"], r["team"])
        for r in conn.execute("SELECT player_id, display_name, team FROM player")
    }


# ---- award helpers -----------------------------------------------------


def _top_with_tiebreak(counts: dict, tiebreak: dict, minimum: float = 1):
    """Like _top(), but a tie on the count is settled by `tiebreak`
    (higher wins) instead of being refused.

    Peak Tagger uses it, broken by the tallest summit the player reached:
    two people who each tagged one peak have not done the same thing if
    one of them climbed 3,000 feet higher. Only that award has an
    obvious "harder" axis -- landmarks and parks do not, so Tourist and
    Park Hopper keep refusing ties.

    A tie on BOTH count and tiebreak is still refused, same as _top().
    """
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], -(tiebreak.get(kv[0]) or 0)))
    if not ranked or ranked[0][1] < minimum:
        return None
    if len(ranked) > 1:
        a, b = ranked[0], ranked[1]
        if a[1] == b[1] and (tiebreak.get(a[0]) or 0) == (tiebreak.get(b[0]) or 0):
            return None
    return ranked[0]


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
    ownership = _ownership_at(conn, protocol, at)
    held: dict[str, int] = {}
    held_by_player: dict[int, int] = {}
    cells_by_team: dict[str, set[tuple[int, int]]] = {}
    for row in ownership:
        held[row["team"]] = held.get(row["team"], 0) + 1
        if row["player_id"] is not None:
            held_by_player[row["player_id"]] = held_by_player.get(row["player_id"], 0) + 1
        xy = _cell_xy(row["cell_id"])
        if xy is not None:
            cells_by_team.setdefault(row["team"], set()).add(xy)

    # Three figures side by side, never added together. Territory decides
    # the month (Largest Territory); check-in and exploration points are
    # shown because they are real work, not because they place a team.
    # Adding them would put a team ahead on ground it does not hold.
    explored = _place_points(conn, protocol, start, end)

    def _blank():
        return {"squares": 0, "checkin_points": 0.0, "explorer_points": 0.0}

    teams = {t: _blank() for t in settings.teams_list}
    for t, n in held.items():
        teams.setdefault(t, _blank())["squares"] = n
    for r in chk:
        teams.setdefault(r["team"], _blank())["checkin_points"] += r["points"]
    for t, pts in explored.items():
        teams.setdefault(t, _blank())["explorer_points"] = pts
    standings = sorted(
        ({"team": t, **v} for t, v in teams.items()),
        key=lambda s: (-s["squares"], s["team"]),
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
    add(_award("largest_territory",
               *(_top({s["team"]: s["squares"] for s in standings}, 1) or (None, 0)),
               names=names, detail="squares held"))

    # Longest Road rewards a shape rather than an amount: ground taken in
    # one continuous run, the way a highway paints. Deliberately NOT
    # scaled by team size -- a small team that drove a long road beats a
    # big one that filled in a city.
    roads = {t: _longest_road(cs) for t, cs in cells_by_team.items()}
    add(_award("longest_road",
               *(_top(roads, settings.longest_road_min_squares) or (None, 0)),
               names=names, detail="squares in an unbroken run"))

    # Empire Builder honors whoever holds the most ground they painted
    # themselves. Counted from the same ownership rows as the standings,
    # so every player's figure is a share of their team's own total and
    # the two can be read against each other -- unlike the attack awards,
    # which count only contested captures and so look tiny beside a team
    # score.
    add(_award("empire_builder", *(_top(held_by_player, 1) or (None, 0)),
               names=names, detail="squares held"))

    # ---- attack and defence -------------------------------------------
    attacks: dict[int, int] = {}
    retakes: dict[int, int] = {}
    per_team_attacks: dict[str, dict[int, int]] = {}
    per_team_retakes: dict[str, dict[int, int]] = {}
    # Ground held, split by the player who painted it, per team. Comes
    # from the ownership rows rather than the capture window, so a team's
    # builders add up to the team's own square count exactly.
    per_team_built: dict[str, dict[int, int]] = {}
    for row in ownership:
        if row["player_id"] is not None:
            bucket = per_team_built.setdefault(row["team"], {})
            bucket[row["player_id"]] = bucket.get(row["player_id"], 0) + 1
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
    # falling inside the month AND by the board that earned it.
    #
    # The protocol filter is new (2026-08-31). place_activation had no
    # protocol column, so a trip made on one board won the honor on both:
    # a single Meshtastic drive up Big Cottonwood put the same name on
    # MeshCore's Peak Tagger, and MeshCore activity was deciding the
    # Meshtastic Tourist and Park Hopper. Rows written before the column
    # existed carry '' and match neither board -- see
    # scripts/backfill_activation_protocol.py, which traces the old rows
    # back to the captures that earned them.
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
            " WHERE p.ref_type = ? AND a.protocol = ? "
            "   AND a.awarded_at >= ? AND a.awarded_at < ? "
            " GROUP BY a.player_id",
            (ref_type, protocol, start, end),
        ).fetchall())
        if award == "peak_tagger":
            # Tallest peak reached breaks a tie -- see
            # _top_with_tiebreak(). Elevation is nullable, and a summit
            # missing one loses the tiebreak rather than winning it.
            tallest: dict[int, float] = dict(conn.execute(
                "SELECT a.player_id, MAX(COALESCE(p.elevation_ft, 0)) FROM place_activation a "
                "  JOIN place p ON p.id = a.place_id "
                " WHERE p.ref_type = 'summit' AND a.protocol = ? "
                "   AND a.awarded_at >= ? AND a.awarded_at < ? "
                " GROUP BY a.player_id",
                (protocol, start, end),
            ).fetchall())
            winner = _top_with_tiebreak(visits, tallest)
            detail = label_detail
            if winner is not None and tallest.get(winner[0]):
                detail = "%s, highest %.0f ft" % (label_detail, tallest[winner[0]])
            add(_award(award, *(winner or (None, 0)), names=names, detail=detail))
        else:
            add(_award(award, *(_top(visits) or (None, 0)), names=names, detail=label_detail))

    for team in settings.teams_list:
        add(_award("team_builder", *(_top(per_team_built.get(team, {})) or (None, 0)),
                   names=names, scope=team, team=team, detail="squares held"))
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
        # The automation guard below needs several nets before a low
        # spread means anything, so a player with one or two timed
        # check-ins is never screened by it -- deliberately. Averaging
        # one night is the point of the minimum being 1; see
        # settings.quick_fingers_min_checkins.
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

    return {"month": month, "protocol": protocol, "standings": standings,
            "awards": with_placeholders(awards)}


# ---- freeze ------------------------------------------------------------


def freeze_month(conn: sqlite3.Connection, protocol: str, month: str, now: int) -> None:
    """Write a finished month's result. Caller holds the write lock."""
    result = compute_month(conn, protocol, month, now)
    conn.execute("INSERT OR REPLACE INTO month_result(month, protocol, closed_at) VALUES (?, ?, ?)",
                 (month, protocol, now))
    conn.execute("DELETE FROM month_standing WHERE month = ? AND protocol = ?", (month, protocol))
    for s in result["standings"]:
        conn.execute(
            "INSERT INTO month_standing(month, protocol, team, squares, checkin_points, explorer_points) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (month, protocol, s["team"], s["squares"], s["checkin_points"], s["explorer_points"]),
        )
    conn.execute("DELETE FROM month_award WHERE month = ? AND protocol = ?", (month, protocol))
    for a in result["awards"]:
        if a["player_id"] is None and a["team"] is None:
            continue   # placeholder for an honor nobody won -- see with_placeholders()
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

    # Memoised only AFTER the work, never before. The callers wrap this
    # in a poll that catches and logs (app/checkin.py), and WriteSession
    # rolls the transaction back on exception -- so a freeze that raised
    # leaves no month_result row. Setting the memo first meant that
    # failure was then suppressed for the rest of the calendar month:
    # every later poll returned 0 without retrying, and the month stayed
    # unfrozen until the process happened to restart. Set here, a failed
    # freeze is simply retried on the next poll, which is what the
    # catch-up behaviour described above is supposed to give.
    _LAST_CHECKED[protocol] = current
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
            "SELECT team, squares, checkin_points, explorer_points FROM month_standing "
            " WHERE month = ? AND protocol = ? ORDER BY squares DESC, team",
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
        awards = with_placeholders(awards)
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


# ---- award geometry ----------------------------------------------------
#
# Some honors happen SOMEWHERE. Longest Road is the clearest case: the
# award is a shape, and the results page can only state it as a number.
# These build GeoJSON for the ones that have a real place, so the page
# can link them onto the map.
#
# Deliberately NOT offered for Largest Territory, Empire Builder, or the
# attack awards: those are thousands of squares scattered across the whole
# board, so a link would either re-draw the team colour the map already
# shows or scatter pins with no shape to them.

GEOMETRIC_AWARDS = ("longest_road", "frontier", "tourist", "park_hopper", "peak_tagger")

# The three place honors differ only in which kind of place they count.
_PLACE_AWARD_TYPE = {"tourist": "landmark", "park_hopper": "park", "peak_tagger": "summit"}


def _cell_feature(cell: tuple[int, int], props: dict) -> dict:
    """One grid square as a GeoJSON polygon, in the same units the board
    layer uses so a highlight sits exactly on top of the cells beneath."""
    south, west, north, east = cell_bounds("%d_%d" % cell)
    return {
        "type": "Feature",
        "properties": props,
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[west, south], [east, south],
                             [east, north], [west, north], [west, south]]],
        },
    }


def _point_feature(lat: float, lon: float, props: dict) -> dict:
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "Point", "coordinates": [lon, lat]}}


def _road_geometry(conn, protocol, at, team):
    """The winning team's longest chain, as the squares along it."""
    by_team: dict[str, set[tuple[int, int]]] = {}
    for row in _ownership_at(conn, protocol, at):
        xy = _cell_xy(row["cell_id"])
        if xy is not None:
            by_team.setdefault(row["team"], set()).add(xy)
    path = _longest_road_path(by_team.get(team, set()))
    return [_cell_feature(c, {"team": team}) for c in path]


def _frontier_geometry(conn, protocol, start, end, player_id, team):
    """Every square this player claimed out past the towns in the month,
    with the furthest one marked. Re-walks the same scan the award does
    (see the frontier block in compute_month) rather than storing it."""
    feats = []
    best = None
    for c in _windowed_captures(conn, protocol, start, end):
        if c["by_air"] or c["player_id"] != player_id:
            continue
        lat, lon = cell_center(c["cell_id"])
        d = places.distance_to_nearest_town_m(lat, lon)
        if d is None:
            continue   # place data unavailable -- skip, never guess
        miles = d / places.MILE_M
        if miles > settings.frontier_miles:
            xy = _cell_xy(c["cell_id"])
            if xy is None:
                continue
            feats.append((miles, xy))
            if best is None or miles > best[0]:
                best = (miles, xy)
    return [
        _cell_feature(xy, {"team": team, "miles": round(miles, 1),
                           "furthest": bool(best and xy == best[1])})
        for miles, xy in feats
    ]


def _place_geometry(conn, protocol, start, end, player_id, ref_type):
    """The places of one kind this player reached inside the month."""
    return [
        _point_feature(r["lat"], r["lon"], {"name": r["name"], "kind": ref_type})
        for r in conn.execute(
            "SELECT p.name, p.lat, p.lon FROM place_activation a "
            "  JOIN place p ON p.id = a.place_id "
            " WHERE a.player_id = ? AND p.ref_type = ? AND a.protocol = ? "
            "   AND a.awarded_at >= ? AND a.awarded_at < ? "
            " ORDER BY a.awarded_at",
            (player_id, ref_type, protocol, start, end),
        )
    ]


def award_geometry(conn: sqlite3.Connection, protocol: str, month: str,
                   award: str, now: int | None = None) -> dict | None:
    """GeoJSON for where an award was earned, or None if that award has
    no place on the map (or nobody won it).

    The winner is taken from compute_month() rather than re-derived, so
    the map can never disagree with the page about who won; only the
    geometry itself is looked up afterwards. Recomputed on demand rather
    than stored -- it is linear in squares held, and persisting a
    330-square path per month per board would be a schema change earning
    milliseconds.
    """
    if award not in GEOMETRIC_AWARDS:
        return None

    start, end = month_bounds(month)
    at = min(end - 1, now if now is not None else int(time.time()))

    result = compute_month(conn, protocol, month, now)
    row = next((a for a in result["awards"]
                if a["award"] == award and not a.get("scope")), None)
    if row is None or (row["player_id"] is None and row["team"] is None):
        return None   # listed but unwon -- nothing to draw

    if award == "longest_road":
        features = _road_geometry(conn, protocol, at, row["team"])
    elif award == "frontier":
        features = _frontier_geometry(conn, protocol, start, end,
                                      row["player_id"], row["team"])
    else:
        features = _place_geometry(conn, protocol, start, end, row["player_id"],
                                   _PLACE_AWARD_TYPE[award])
    if not features:
        return None

    return {
        "award": award,
        "label": row["label"],
        "month": month,
        "protocol": protocol,
        "team": row["team"],
        "player": row["player"],
        "value": row["value"],
        "detail": row["detail"],
        "geojson": {"type": "FeatureCollection", "features": features},
    }
