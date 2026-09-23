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
import logging
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import settings
from . import mc_scoring
from . import results

log = logging.getLogger("announce_content")

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


def _team_ranks(conn: sqlite3.Connection, board: str, before_ts: int, after_ts: int
                ) -> tuple[list[str], dict[str, int], dict[str, int], dict[str, int]]:
    """Shared ranking arithmetic for the two row builders below --
    computed ONCE per window since both the movers-only diff
    (_placement_rows) and the full-roster standings list
    (_standings_rows) need the exact same before/after ownership
    snapshot, and re-deriving it a second, independent way would be
    wasted work and a second chance for the two to disagree.

    Returns (after_order, before_rank, before_counts, after_counts):
    after_order is team names in current rank order (index 0 = 1st);
    before_rank maps a team that held at least one square BEFORE the
    window to its rank then (1-indexed); before_counts/after_counts are
    the raw square counts either side, which _biggest_gain_row() also
    needs and would otherwise have to recompute a second time.
    """
    before = _team_counts(results.ownership_at(conn, board, before_ts))
    after = _team_counts(results.ownership_at(conn, board, after_ts))
    after_order = _ranked_teams(after)
    before_rank = {t: i + 1 for i, t in enumerate(_ranked_teams(before))}
    return after_order, before_rank, before, after


def _movers_rows(after_order: list[str], before_rank: dict[str, int]) -> list[dict]:
    """Rank-change rows -- one row per team whose current rank differs
    from its rank before the window. A team absent from the "before"
    snapshot has no rank to compare and is left out entirely (no "new to
    the board" row here -- unlike Discord's weekly recap, this module
    only ever reports a MOVE, never an appearance/disappearance, since a
    terse LoRa-budget line has no room for "new to the board" and still
    naming the team and a rank).

    MOVERS ONLY, deliberately kept this way even though _standings_rows()
    below now also exists: build_daily_content()/build_weekly_content()'s
    own "nothing happened" quiet check, and this module's one-line
    dramatic-headline logic (_placement_headline()), both depend on an
    EMPTY list meaning "no team's rank changed" -- the full always-non-
    empty roster _standings_rows() returns would silently break that
    check if used here instead.

    Pure (no DB access): both this and _standings_rows() are derived from
    the SAME _team_ranks() call a caller makes once, so the two row lists
    can never disagree about what the ranking actually was.
    """
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
    return rows


def _standings_rows(after_order: list[str], before_rank: dict[str, int]) -> list[dict]:
    """The FULL roster's placement rows -- every team currently ranked,
    unchanged teams included (rank == rank_was), not just the movers
    _placement_rows() above reports. This is the "MORE than mesh
    renders" data Part 1 of this module's own contract asks for: the
    JSON API's consumers (and app/mesh_render.py's block renderer, which
    needs a fixed Top-N board regardless of whether anyone moved) get
    every team's row, never just the ones that changed.

    A team with no square before the window (absent from before_rank)
    has no real prior rank to report -- rather than invent a "new to the
    board" case here (the terse one-line renderer's own reason for
    leaving such a team out entirely does not apply to a full-roster
    list, which cannot leave a currently-ranked team out), it is treated
    as unchanged: rank_was defaults to the team's own current rank.
    """
    rows: list[dict] = []
    for i, team in enumerate(after_order):
        current_rank = i + 1
        prior_rank = before_rank.get(team, current_rank)
        if prior_rank == current_rank:
            text = f"{team}: {_ordinal(current_rank)} (unchanged)"
        else:
            text = f"{team}: {_ordinal(current_rank)} (was {_ordinal(prior_rank)})"
        rows.append({
            "text": _clip(text, _ROW_TEXT_LIMIT),
            "team": team, "player": None, "value": None, "unit": None,
            "delta": prior_rank - current_rank,
            "rank": current_rank, "rank_was": prior_rank,
        })
    return rows


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
    after_order, before_rank, before, after = _team_ranks(conn, board, start_ts - 1, end_ts - 1)
    rows = _movers_rows(after_order, before_rank)
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

    # "Standings" carries EVERY currently-ranked team (unchanged included)
    # -- see _standings_rows()'s own docstring for why this is additive,
    # never a replacement for the movers-only "Placement" section above:
    # app/mesh_render.py's block renderer needs a fixed Top-N board even
    # on a day where nobody's rank moved but a gain row still fired.
    sections = [{"heading": "Placement", "rows": section_rows}]
    sections.append({"heading": "Standings", "rows": _standings_rows(after_order, before_rank)})

    return {
        "kind": "daily_recap",
        "key": f"{day.isoformat()}:{board}",
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


def build_weekly_content(conn: sqlite3.Connection, board: str, start_ts: int, end_ts: int,
                          now: int) -> dict | None:
    """Same placement-rank diff as build_daily_content(), across the
    week [start_ts, end_ts), plus a short Exploration section counting
    DISTINCT places activated in the window (place_activation.awarded_at
    inside [start_ts, end_ts), filtered to this board). Returns None
    when nothing moved and nothing was explored.
    """
    after_order, before_rank, _before, _after = _team_ranks(conn, board, start_ts - 1, end_ts - 1)
    rows = _movers_rows(after_order, before_rank)

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
    # "Standings" carries EVERY currently-ranked team, unchanged included
    # -- see _standings_rows()'s own docstring; additive, alongside the
    # movers-only "Placement" section above, for the same reason
    # build_daily_content() adds it.
    sections.append({"heading": "Standings", "rows": _standings_rows(after_order, before_rank)})

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
        # Clearly-named field, alongside (not instead of) the Exploration
        # section's own row above -- app/mesh_render.py's weekly block
        # reads THIS field directly for its "<n> new places · <domain>"
        # line rather than digging a row's `value` back out of
        # `sections`. Always an int (0 when nothing was explored but the
        # week still fired on a rank move alone), never None, so the
        # renderer never has to special-case a missing field.
        "new_places": explored,
        # No `/results` path -- unlike build_month_content()'s own url
        # (a specific frozen month's results page), the weekly recap
        # links to the site itself, and app/mesh_render.py's compact
        # weekly block renders only the bare host anyway (see that
        # module's own _domain_only()).
        "url": settings.oauth_public_base_url.rstrip("/") if settings.oauth_public_base_url else None,
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
        # one-line fallback renderer's own "{prefix} {period_label}:
        # {headline}" line puts it right alongside), so restating it in
        # the headline would just be spending bytes on the same fact
        # twice. The sentence stays complete without it -- "RED wins
        # with 3 squares" -- for a JSON consumer reading `headline`
        # alone.
        headline = _clip(
            f"{winner['team']} wins with {_fmt_number(winner['squares'])} squares",
            _HEADLINE_LIMIT,
        )
    else:
        headline = _clip(f"{period_label} is over", _HEADLINE_LIMIT)

    url = f"{settings.oauth_public_base_url.rstrip('/')}/results" if settings.oauth_public_base_url else None

    # "Standings" -- every team that scored at least one square this
    # month, ranked (month_standing's own ORDER BY squares DESC, team is
    # already the correct order), zero-square teams excluded entirely.
    # app/mesh_render.py's block reads THIS for its "MW <Month> Top 5"
    # rows -- NOT the Honors section above (kept for JSON/API consumers
    # and the one-line fallback only; see that module's own comment).
    standings_rows = [
        {
            "text": _clip(f"{r['team']}: {_fmt_number(r['squares'])} squares", _ROW_TEXT_LIMIT),
            "team": r["team"], "player": None, "value": r["squares"], "unit": "squares",
            "delta": None, "rank": i + 1, "rank_was": None,
        }
        for i, r in enumerate(r for r in standings if r["squares"] > 0)
    ]

    sections = []
    if section_rows:
        sections.append({"heading": "Honors", "rows": section_rows})
    if standings_rows:
        sections.append({"heading": "Standings", "rows": standings_rows})

    return {
        "kind": "month_honors",
        "key": f"{month}:{board}",
        "board": board,
        "net_id": None,
        "period_label": period_label,
        "period_start_ts": start_ts,
        "period_end_ts": end_ts,
        "headline": headline,
        "sections": sections,
        "url": url,
        "created_at": now,
    }


def build_season_close_content(conn: sqlite3.Connection, board: str, season_id: int,
                                now: int) -> dict | None:
    """A closed MeshCore/Meshtastic season's final standings.

    ORDERING: the podium is ordered by the SAME combined total that
    decided `mc_season.winner` -- app/mc_scoring.py's team_totals()
    (squares held, plus check-in points, plus Places Worth Going
    points), the same figure Discord's own season-close embed
    (app/discord_notify.py's build_season_close_embed()) is built from.
    This module used to rank by mc_season_team_tally.tiles alone, which
    could theoretically crown a DIFFERENT team the winner on the mesh
    than Discord and the website just named -- the worst failure this
    feature could have. Fixed here deliberately.

    The combined total is reconstructed from what a closed season keeps:
    mc_season_team_tally's own tiles and checkin_points columns (frozen
    once, at close, by maybe_roll_season(), and stable afterward -- a
    closed season's mc_tile/mc_checkin_award rows are never touched
    again; only a NEW season's rows ever carry a new season_id), plus
    app/mc_scoring.py's team_place_points() read live. Places Worth
    Going points are never stored per-season anywhere -- place_activation
    is scoped by week_start, not season_id, see that function's own
    docstring -- so a live, time-windowed read is the only way to fold
    them in at all, exactly the read maybe_roll_season() itself used to
    decide the winner in the first place.

    DISPLAY: app/mesh_render.py's default season_close render shows NO
    number beside a podium team at all -- just the medal, the team
    emoji, and the name (see that module's _medal_line()). The podium
    is ORDERED by the combined total above, but a genuinely close
    season can make a row look numerically "out of order" if a raw
    figure were printed next to it (a team with fewer squares placed
    above one with more) -- correct, since the total decided it, but it
    reads as a bug to anyone hearing it on a radio, and this is the
    single most-read message the system sends. Removing the number
    removes the contradiction. This function still puts each team's
    squares (`value`/`unit` below) AND its combined total (`total`
    below) on every standings row as structured fields -- this is a
    RENDERING choice, not a data one: JSON/API consumers, and any
    future richer destination, still get both numbers.

    INVARIANT: if this season has a stored `mc_season.winner` (not NULL
    and not 'TIE'), the podium's first place always equals it. Should
    the stored winner and the total computed here ever disagree (they
    should not, but a closed season predating some scoring change, or
    any other drift, must never announce a different winner than
    mc_season.winner / Discord), the stored winner wins outright and a
    warning is logged naming both -- this never silently announces its
    own answer instead.

    Returns None when this season has no tally rows at all yet (should
    never happen for a season maybe_roll_season() actually closed, but
    a season_id that does not exist, or one closed by a path that
    skipped the tally, must not crash a due-check).

    A team with 0 tiles is excluded entirely -- same "not worth
    announcing" rule build_month_content()'s own Standings section
    above applies.
    """
    season_row = conn.execute(
        "SELECT started_at, ends_at, winner FROM mc_season WHERE id = ?", (season_id,),
    ).fetchone()
    if season_row is None:
        return None

    tally_rows = conn.execute(
        "SELECT team, tiles, checkin_points FROM mc_season_team_tally "
        " WHERE season_id = ? AND tiles > 0 ORDER BY tiles DESC, team",
        (season_id,),
    ).fetchall()
    if not tally_rows:
        return None

    tiles_by_team = {r["team"]: r["tiles"] for r in tally_rows}

    # Places Worth Going points aren't in the tally row at all (see the
    # docstring above) -- the only place they exist for a closed season
    # is this same live, time-windowed read maybe_roll_season() used.
    place_points = mc_scoring.team_place_points(conn, season_id, board)
    totals_by_team = {
        r["team"]: r["tiles"] + r["checkin_points"] + place_points.get(r["team"], 0.0)
        for r in tally_rows
    }

    # Ordered by the COMBINED total, ties broken alphabetically (the
    # same deterministic tiebreak the tally query's own ORDER BY uses)
    # -- deliberately NOT tiles_by_team, so a row below can print a
    # smaller squares figure ABOVE a row with a larger one. That is the
    # whole point: the number shown is squares, the order is the total
    # that actually decided the season, and those two are allowed to
    # disagree.
    ranked_teams = sorted(tiles_by_team, key=lambda t: (-totals_by_team[t], t))

    computed_winner = ranked_teams[0]
    stored_winner = season_row["winner"]
    if stored_winner and stored_winner != "TIE" and stored_winner != computed_winner:
        log.warning(
            "season %d close content: computed podium winner %s disagrees with stored "
            "mc_season.winner %s -- using the stored winner",
            season_id, computed_winner, stored_winner,
        )
        if stored_winner in tiles_by_team:
            ranked_teams = [stored_winner] + [t for t in ranked_teams if t != stored_winner]
            winner = stored_winner
        else:
            # The stored winner holds no tally row with tiles > 0, so it
            # cannot be placed on a podium that only lists teams with
            # squares -- fall back to the computed order; the warning
            # above already flags this (very unlikely) drift.
            winner = computed_winner
    else:
        winner = stored_winner if stored_winner and stored_winner != "TIE" else computed_winner

    standings_rows = [
        {
            "text": _clip(f"{team}: {_fmt_number(tiles_by_team[team])} squares", _ROW_TEXT_LIMIT),
            "team": team, "player": None, "value": tiles_by_team[team], "unit": "squares",
            # The combined total that actually decided the podium ORDER
            # (see this function's own ORDERING docstring above) -- kept
            # here as its own structured field, distinct from `value`
            # (squares), for JSON/API consumers and any future richer
            # destination. app/mesh_render.py's default render uses
            # neither field -- see its DISPLAY docstring above.
            "total": totals_by_team[team],
            "delta": None, "rank": i + 1, "rank_was": None,
        }
        for i, team in enumerate(ranked_teams)
    ]

    url = f"{settings.oauth_public_base_url.rstrip('/')}/results" if settings.oauth_public_base_url else None

    return {
        "kind": "season_close",
        "key": str(season_id),
        "board": board,
        "net_id": None,
        # No period_label the way a recurring day/week/month has one --
        # a season close is a one-time event, not a period on a
        # repeating clock, and app/mesh_render.py's fixed
        # "MW SEASON OVER" title carries no date at all either way.
        "period_label": None,
        "period_start_ts": season_row["started_at"],
        "period_end_ts": season_row["ends_at"],
        "headline": _clip(
            f"{winner} wins the season with {_fmt_number(tiles_by_team[winner])} squares",
            _HEADLINE_LIMIT,
        ),
        "sections": [{"heading": "Standings", "rows": standings_rows}],
        # app/mesh_render.py's "Congratulations <winner>!" line reads
        # this directly rather than re-deriving it from sections[0]'s
        # first row.
        "winner": winner,
        "url": url,
        "created_at": now,
    }


def _short_net_name(label: str) -> str:
    """checkin_net.label -> a short name for app/mesh_render.py's net
    wrap-up title ("MW <short name> Net") -- "Weekly Net (Freq51 MC)"
    becomes "Freq51", "Weekly Net (Coloradomesh MC)" becomes
    "Coloradomesh".

    Rule, in order: if `label` has a parenthesised part, take its text
    and strip a trailing " MC"/" MT" (the board suffix an operator's own
    label convention already carries -- redundant here since board is
    already a separate Content field). Otherwise, strip a leading
    "Weekly Net" and use what remains. If either path would leave
    nothing usable, fall back to the whole label rather than an empty
    title.
    """
    label = (label or "").strip()
    if "(" in label and ")" in label:
        start = label.index("(")
        end = label.index(")", start)
        inner = label[start + 1:end].strip()
        for suffix in (" MC", " MT"):
            if inner.endswith(suffix):
                inner = inner[: -len(suffix)].strip()
                break
        return inner or label

    prefix = "Weekly Net"
    if label.startswith(prefix):
        remainder = label[len(prefix):].strip()
        return remainder or label

    return label


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
        "SELECT a.player_id, a.streak, p.display_name, p.team FROM mc_checkin_award a "
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
            # `team` (added for app/mesh_render.py's "EMOJI NAME STREAK"
            # streak rows -- the same TEAM_EMOJI map placement rows use)
            # is the player's CURRENT team, same known caveat this
            # module's own docstring already documents for check-in/
            # exploration points: a player who switches teams re-
            # attributes their historical check-in row to the new team.
            "team": r["team"], "player": r["display_name"], "value": r["streak"],
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
        # app/mesh_render.py's net wrap-up title ("MW <net_name> Net") --
        # NOT the date (period_label already carries that, for JSON/API
        # consumers and the other Content kinds' own titles, but the
        # operator's own final spec for this block leaves the date out
        # of the title entirely, same as "MW Weekly Top 5"/"MW Daily Top
        # 5" carry no date either). See _short_net_name()'s own docstring.
        "net_name": _short_net_name(net_row["label"]),
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
