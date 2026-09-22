"""Transport-neutral announcement Content -- the foundation of the
public announcement feed (app/db.py's `announcement` table).

Each builder in this module turns MeshWars' existing scoring helpers
(app/results.py's ownership_at()/compute_month(), the frozen month_*
tables, mc_checkin_award) into a plain JSON-serializable dict -- a
Content -- with a fixed shape (see each builder's own docstring for the
exact fields). Nothing here renders that Content into text for any
particular destination (Discord, a radio packet, a web page): that is
deliberately a later, separate concern. Nothing here decides WHEN to
build one, either -- no background clock, no API route. This module
only answers "is there anything worth saying about this period, and if
so, what is it" -- a pure, read-only question against the database.

A quiet period is not an error, it is the expected common case -- every
builder returns None rather than a Content with nothing in it, mirroring
the existing rule app/results.py's month_results_for() already follows
(a month's own honors are only ever shown once it closes, never as a
running total) and the Discord weekly recap's own placement/exploration
sections (both return None on a quiet window rather than rendering
"nothing happened").

KNOWN CAVEAT, deliberately not fixed here: mc_checkin_award and
place_activation both attribute their points to a player's CURRENT team
(player.team), not the team the player was on when the row was written.
A player who switches teams mid-window re-attributes their historical
check-in/exploration points to their new team the moment they switch --
exactly the same caveat app/mc_scoring.py's team_checkin_points() and
team_place_points() already carry for the live scoreboard. Ground
actually captured is unaffected: mc_tile_capture_log.by_team is frozen
at paint time, which is why the placement/rank sections below (built
from ownership_at(), which reads by_team off that same frozen log) do
not share this problem -- only the check-in and exploration counts do.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import settings
from . import results

# Award keys in the order the results page already shows them (see
# results.py's own comment on TEAM_AWARDS/PLAYER_AWARDS/PER_TEAM_AWARDS
# and _AWARD_RANK) -- reused here, rather than re-guessing an order, so
# "most notable" in build_month_content() below means the same thing
# this module and the results page already agree on: team honors first,
# then player honors, then per-team honors.
_AWARD_ORDER = {
    key: i for i, (key, _) in enumerate(
        results.TEAM_AWARDS + results.PLAYER_AWARDS + results.PER_TEAM_AWARDS
    )
}

# How many award rows build_month_content() emits at most, and how many
# check-in rows build_net_wrapup_content() emits at most -- both "a
# short list of the most notable," not a full dump. Named once rather
# than a bare literal in two places.
_MAX_SECTION_ROWS = 5

_HEADLINE_LIMIT = 80
_ROW_TEXT_LIMIT = 60


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.checkin_net_timezone)


def _clip(text: str, limit: int) -> str:
    """Defensively enforce the HARD RULE length budget (headline <= 80,
    row text <= 60) no matter what a team/player name turns out to be --
    every builder below composes short, deliberately-terse strings that
    should never actually reach this limit, but a name is
    operator/player-supplied text this module does not control, so the
    guarantee is enforced here rather than merely hoped for.
    """
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    # ASCII "..." only -- not U+2026 HORIZONTAL ELLIPSIS -- these strings
    # are headed for a LoRa packet with a plain-ASCII budget (see this
    # module's own HARD RULES), and a single non-ASCII character here
    # would be the only place that rule was ever broken.
    return text[: limit - 3].rstrip() + "..."


def _fmt_number(n: int | float) -> str:
    """Thousands-separated for display text ONLY -- the HARD RULE in
    the Content contract is explicit that the raw `value` field never
    gets this treatment, only the pre-rendered `text` a row carries.
    """
    if isinstance(n, float) and not n.is_integer():
        return f"{n:,.1f}"
    return f"{int(n):,}"


def _ordinal(n: int) -> str:
    """1 -> "1st", 2 -> "2nd", 11 -> "11th" -- same English ordinal
    rule app/discord_notify.py's own _ordinal() encodes (the "teens"
    always take "th" regardless of last digit); written fresh here
    rather than imported so this module stays independent of Discord's
    own formatting helpers, per this task's own instruction not to
    reuse discord_notify's rendering.
    """
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _team_counts(rows) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["team"]] = counts.get(r["team"], 0) + 1
    return counts


def _ranked_teams(counts: dict[str, int]) -> list[str]:
    """Team names in rank order (index 0 = 1st): squares held
    descending, team name ascending as a deterministic tiebreak -- the
    same ranking app/discord_notify.py's weekly placement section
    already uses (ownership_at() is the shared source of truth for
    "who holds what right now" either way; only the ranking rule needs
    to agree, and this is that same rule, not an import of it).
    """
    return [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _placement_rows(conn: sqlite3.Connection, board: str, before_ts: int, after_ts: int
                     ) -> tuple[list[dict], dict[str, int], dict[str, int]]:
    """Rank-change rows for the window (before_ts, after_ts] -- one row
    per team whose current rank (at after_ts) differs from its rank at
    before_ts. A team absent from either snapshot has no rank to compare
    and is left out entirely (no "new to the board" row here -- unlike
    Discord's weekly recap, this module only ever reports a MOVE, never
    an appearance/disappearance, since a terse LoRa-budget line has no
    room for "new to the board" and still naming the team and a rank).

    Returns (rows, before_counts, after_counts) -- the caller also needs
    the raw counts to compute a biggest-gain row, and re-deriving them a
    second time would be wasted work and a second chance to disagree.
    """
    before = _team_counts(results.ownership_at(conn, board, before_ts))
    after = _team_counts(results.ownership_at(conn, board, after_ts))
    after_order = _ranked_teams(after)
    before_rank = {t: i + 1 for i, t in enumerate(_ranked_teams(before))}

    rows: list[dict] = []
    for i, team in enumerate(after_order):
        current_rank = i + 1
        prior_rank = before_rank.get(team)
        if prior_rank is None or prior_rank == current_rank:
            continue
        rows.append({
            "text": _clip(f"{team}: {_ordinal(current_rank)} (was {_ordinal(prior_rank)})", _ROW_TEXT_LIMIT),
            "team": team, "player": None, "value": None, "unit": None,
            "delta": prior_rank - current_rank,  # positive = improved (moved to a better/lower-numbered rank)
            "rank": current_rank, "rank_was": prior_rank,
        })
    return rows, before, after


def _biggest_gain_row(before: dict[str, int], after: dict[str, int]) -> dict | None:
    """At most one row: the single team with the largest positive
    square gain across the window, or None if no team gained ground.
    Ties broken by team name ascending, the same deterministic tiebreak
    every other ranking in this module uses.
    """
    teams = set(before) | set(after)
    gains = {t: after.get(t, 0) - before.get(t, 0) for t in teams}
    positive = {t: g for t, g in gains.items() if g > 0}
    if not positive:
        return None
    team = min(positive, key=lambda t: (-positive[t], t))
    gain = positive[team]
    return {
        "text": _clip(f"{team} gained {_fmt_number(gain)} squares", _ROW_TEXT_LIMIT),
        "team": team, "player": None, "value": gain, "unit": "squares",
        "delta": gain, "rank": None, "rank_was": None,
    }


def _placement_headline(rows: list[dict], fallback: str | None) -> str | None:
    """The single most dramatic rank change in `rows` (largest absolute
    rank delta; ties broken by team name ascending, same convention
    every other ranking in this module uses -- min() on a
    (-abs(delta), team) key, not max() on a plain one, so a tie picks
    the alphabetically FIRST team rather than the last), or `fallback`
    when there are no rank changes to lead with.
    """
    if not rows:
        return fallback
    top = min(rows, key=lambda r: (-abs(r["delta"]), r["team"]))
    verb = "climbed to" if top["delta"] > 0 else "dropped to"
    return _clip(f"{top['team']} {verb} {_ordinal(top['rank'])}", _HEADLINE_LIMIT)


def build_daily_content(conn: sqlite3.Connection, board: str, start_ts: int, end_ts: int,
                         now: int) -> dict | None:
    """MOVERS ONLY: which teams' rank changed over the day [start_ts,
    end_ts), plus at most one row for the day's single biggest square
    gain. Returns None when nothing moved and nobody gained ground.

    `key` is the local date of the day that just ended -- the local
    date of start_ts, since [start_ts, end_ts) is that whole day.
    """
    rows, before, after = _placement_rows(conn, board, start_ts - 1, end_ts - 1)
    gain_row = _biggest_gain_row(before, after)
    if not rows and gain_row is None:
        return None

    section_rows = list(rows)
    if gain_row is not None:
        section_rows.append(gain_row)

    # No " today" suffix here: this recap is posted AFTER the day it
    # describes has already closed (period_label already carries which
    # day it was), so "today" would be factually wrong.
    headline = _placement_headline(
        rows, fallback=gain_row["text"] if gain_row else None,
    )
    headline = _clip(headline, _HEADLINE_LIMIT)

    day = datetime.fromtimestamp(start_ts, tz=_tz()).date()
    period_label = f"{day.day} {day.strftime('%b')}"

    return {
        "kind": "daily_recap",
        "key": f"{day.isoformat()}:{board}",
        "board": board,
        "net_id": None,
        "period_label": period_label,
        "period_start_ts": start_ts,
        "period_end_ts": end_ts,
        "headline": headline,
        "sections": [{"heading": "Placement", "rows": section_rows}],
        "url": None,
        "created_at": now,
    }


def build_weekly_content(conn: sqlite3.Connection, board: str, start_ts: int, end_ts: int,
                          now: int) -> dict | None:
    """Same placement-rank diff as build_daily_content(), across the
    week [start_ts, end_ts), plus a short Exploration section counting
    DISTINCT places activated in the window (place_activation.awarded_at
    inside [start_ts, end_ts), filtered to this board). Returns None
    when nothing moved and nothing was explored.
    """
    rows, _before, _after = _placement_rows(conn, board, start_ts - 1, end_ts - 1)

    explored = conn.execute(
        "SELECT COUNT(DISTINCT place_id) AS n FROM place_activation "
        " WHERE protocol = ? AND awarded_at >= ? AND awarded_at < ?",
        (board, start_ts, end_ts),
    ).fetchone()["n"]

    if not rows and not explored:
        return None

    sections = []
    if rows:
        sections.append({"heading": "Placement", "rows": rows})
    if explored:
        noun = "place" if explored == 1 else "places"
        sections.append({"heading": "Exploration", "rows": [{
            "text": _clip(f"{_fmt_number(explored)} new {noun} explored", _ROW_TEXT_LIMIT),
            "team": None, "player": None, "value": explored, "unit": "places",
            "delta": None, "rank": None, "rank_was": None,
        }]})

    explore_fallback = None
    if explored:
        noun = "place" if explored == 1 else "places"
        explore_fallback = f"{_fmt_number(explored)} new {noun} explored this week"
    headline = _placement_headline(rows, fallback=explore_fallback)
    headline = _clip(headline, _HEADLINE_LIMIT)

    tz = _tz()
    start_date = datetime.fromtimestamp(start_ts, tz=tz).date()
    end_date = datetime.fromtimestamp(end_ts - 1, tz=tz).date()
    iso_year, iso_week, _ = end_date.isocalendar()
    if start_date.month == end_date.month:
        period_label = f"{start_date.day}-{end_date.day} {end_date.strftime('%b')}"
    else:
        period_label = f"{start_date.strftime('%d %b')}-{end_date.strftime('%d %b')}"

    return {
        "kind": "weekly_recap",
        "key": f"{iso_year}-W{iso_week:02d}:{board}",
        "board": board,
        "net_id": None,
        "period_label": period_label,
        "period_start_ts": start_ts,
        "period_end_ts": end_ts,
        "headline": headline,
        "sections": sections,
        "url": None,
        "created_at": now,
    }


def build_month_content(conn: sqlite3.Connection, board: str, month: str, now: int) -> dict | None:
    """A frozen month's headline standing plus up to _MAX_SECTION_ROWS
    of its most notable (headline-scope, i.e. not per-team) awards.
    Returns None when this (month, board) has not been frozen yet --
    app/results.py's month_result is the freeze marker, same table
    freeze_month() writes and the same one month_results_for() checks.
    """
    frozen = conn.execute(
        "SELECT 1 FROM month_result WHERE month = ? AND protocol = ?", (month, board),
    ).fetchone()
    if frozen is None:
        return None

    standings = conn.execute(
        "SELECT team, squares FROM month_standing WHERE month = ? AND protocol = ? "
        " ORDER BY squares DESC, team", (month, board),
    ).fetchall()

    award_rows = conn.execute(
        "SELECT ma.award, ma.player_id, ma.team, ma.value, ma.detail, p.display_name "
        "  FROM month_award ma LEFT JOIN player p ON p.player_id = ma.player_id "
        " WHERE ma.month = ? AND ma.protocol = ? AND ma.scope = ''",
        (month, board),
    ).fetchall()
    award_rows = sorted(award_rows, key=lambda r: _AWARD_ORDER.get(r["award"], len(_AWARD_ORDER)))[:_MAX_SECTION_ROWS]

    section_rows = []
    for r in award_rows:
        label = results.AWARD_LABELS.get(r["award"], r["award"])
        name = r["display_name"] if r["player_id"] is not None else r["team"]
        value_text = _fmt_number(r["value"]) if r["value"] is not None else ""
        text = f"{label}: {name} {value_text}".strip() if name else f"{label}: {value_text}".strip()
        section_rows.append({
            "text": _clip(text, _ROW_TEXT_LIMIT),
            "team": r["team"], "player": name if r["player_id"] is not None else None,
            "value": r["value"], "unit": r["detail"], "delta": None, "rank": None, "rank_was": None,
        })

    year, mon = int(month[:4]), int(month[5:7])
    period_label = datetime(year, mon, 1).strftime("%B")
    start_ts, end_ts = results.month_bounds(month)

    winner = standings[0] if standings else None
    if winner is not None:
        # No month name here: period_label already carries it (and the
        # renderer's own "{prefix} {period_label}: {headline}" line puts
        # it right alongside), so restating it in the headline would
        # just be spending bytes on the same fact twice. The sentence
        # stays complete without it -- "RED wins with 3 squares" -- for
        # a JSON consumer reading `headline` alone.
        headline = _clip(
            f"{winner['team']} wins with {_fmt_number(winner['squares'])} squares",
            _HEADLINE_LIMIT,
        )
    else:
        headline = _clip(f"{period_label} is over", _HEADLINE_LIMIT)

    url = f"{settings.oauth_public_base_url.rstrip('/')}/results" if settings.oauth_public_base_url else None

    return {
        "kind": "month_honors",
        "key": f"{month}:{board}",
        "board": board,
        "net_id": None,
        "period_label": period_label,
        "period_start_ts": start_ts,
        "period_end_ts": end_ts,
        "headline": headline,
        "sections": [{"heading": "Honors", "rows": section_rows}] if section_rows else [],
        "url": url,
        "created_at": now,
    }


def build_net_wrapup_content(conn: sqlite3.Connection, net_row, net_date: str, now: int) -> dict | None:
    """One net's check-in wrap-up: how many checked in, and the top few
    by their already-stored streak (never recomputed -- see
    mc_checkin_award.streak's own SCHEMA comment). Returns None when
    nobody checked in for this (net_id, net_date).

    `net_row` is a checkin_net row (or any mapping with its columns --
    id, protocol, timezone, start_hour, end_hour). Local-time formatting
    uses THIS net's own timezone column, never a global clock -- two
    nets can run in different zones, and mixing them up would put a
    wrap-up's own local date a day off for whichever net is not on the
    process's assumed clock.
    """
    net_id = net_row["id"]
    board = net_row["protocol"]

    rows = conn.execute(
        "SELECT a.player_id, a.streak, p.display_name FROM mc_checkin_award a "
        "  JOIN player p ON p.player_id = a.player_id "
        " WHERE a.net_id = ? AND a.net_date = ?",
        (net_id, net_date),
    ).fetchall()
    if not rows:
        return None

    count = len(rows)
    top = sorted(rows, key=lambda r: (-(r["streak"] or 0), r["display_name"]))[:_MAX_SECTION_ROWS]
    section_rows = []
    for r in top:
        if r["streak"] is not None:
            text = f"{r['display_name']}: streak {r['streak']}"
        else:
            text = f"{r['display_name']}: checked in"
        section_rows.append({
            "text": _clip(text, _ROW_TEXT_LIMIT),
            "team": None, "player": r["display_name"], "value": r["streak"],
            "unit": "streak" if r["streak"] is not None else None,
            "delta": None, "rank": None, "rank_was": None,
        })

    tz = ZoneInfo(net_row["timezone"])
    d = datetime.strptime(net_date, "%Y-%m-%d")
    period_start_ts = int(datetime(d.year, d.month, d.day, net_row["start_hour"], tzinfo=tz).timestamp())
    period_end_ts = int((datetime(d.year, d.month, d.day, net_row["end_hour"], tzinfo=tz)
                          + timedelta(hours=1)).timestamp())
    period_label = f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"

    return {
        "kind": "net_wrapup",
        "key": f"{net_id}:{net_date}",
        "board": board,
        "net_id": net_id,
        "period_label": period_label,
        "period_start_ts": period_start_ts,
        "period_end_ts": period_end_ts,
        "headline": _clip(f"{count} checked in", _HEADLINE_LIMIT),
        "sections": [{"heading": "Check-ins", "rows": section_rows}],
        "url": None,
        "created_at": now,
    }


def store_announcement(conn: sqlite3.Connection, content: dict) -> int | None:
    """INSERT OR IGNORE `content` into app/db.py's `announcement` table,
    keyed on (kind, key) -- returns the new row's id, or None when that
    (kind, key) was already stored (the whole point of the UNIQUE index:
    a builder can be re-run freely, e.g. by a retried poll, without ever
    writing the same announcement twice).

    Takes the CALLER's connection and issues no BEGIN/COMMIT of its own
    -- same contract as app/results.py's freeze_month() and
    app/discord_notify.py's enqueue(), so a caller can fold this insert
    into a larger transaction (a future poller writing several
    announcements, or one alongside other work) and have the whole
    thing roll back together on failure.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO announcement(kind, key, board, net_id, content, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (content["kind"], content["key"], content["board"], content.get("net_id"),
         json.dumps(content), content["created_at"]),
    )
    return cur.lastrowid if cur.rowcount else None
