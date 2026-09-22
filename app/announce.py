"""Decides WHEN a public announcement Content gets built -- the missing
piece between app/announce_content.py (builds transport-neutral Content,
INSERT-OR-IGNOREs it into app/db.py's `announcement` table) and
app/mesh_render.py (renders one Content to a byte-budgeted line).
Neither of those two modules has, or should have, any notion of a
clock; this module is that clock.

Shape and reasoning are deliberately borrowed from app/discord_notify.py's
TIME_DRIVEN_PROVIDERS / check_due_time_driven() -- see that list's own
docstring (~L1414) for the full rationale this module does not repeat:
a PROVIDER FUNCTION, called as provider(conn, now) on every due-check,
returns zero or more Content dicts due right now; dueness is decided
ENTIRELY by "does a row for this (kind, key) already exist" (here,
app/db.py's `announcement` table and its own UNIQUE(kind, key) index,
via app/announce_content.py's store_announcement()'s INSERT OR IGNORE),
never a separate "last run" column; and a provider must stay CHEAP when
nothing is due, never querying a large/growing table (mc_tile,
mc_tile_capture_log, mc_checkin_award, ...) before its own key check has
had a chance to short-circuit.

This module reuses app/discord_notify.py's _weekly_recap_period() and
_due_net_wrapups() rather than recomputing "when did the week end" or
"is this net's wrap-up due" a second, independent way -- the same
cross-module reach app/discord_interactions.py already makes into that
module's private helpers (discord_notify._parse_team_emoji() etc.), so
the announcement feed and the Discord recap can never disagree about
when a period ended.

THE BACKLOG RULE, the one new piece of dueness logic every provider
below adds on top of discord_notify.py's own pattern: an announcement
whose own period ended more than settings.announcement_max_age_hours
ago is never built, full stop, regardless of whether an announcement
row for it exists yet. Discord's own weekly recap and net wrap-up are
deliberately narrow (at most one period, never a backlog -- see
_weekly_recap_period()'s own CRITICAL note) but month_provider() below
is not narrow the same way on its own: month_result accumulates one row
per (month, protocol) forever, so on a FRESH deployment, or one
recovering from a long outage, the newest frozen month with no
announcement row could be months old, and building an accurate Content
for it (which this module otherwise would, happily) would still be
stale news dumped on readers all at once -- precisely the bug this
codebase already shipped once on the Discord side (enabling
announcements on a new deployment immediately posted the most recent
completed weekly recap and every net's most recent wrap-up, days to
weeks stale, and they had to be deleted by hand). The age cutoff is
what makes a first run on an old database silent instead.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import announce_content, discord_notify, results
from .config import settings
from .db import WriteSession

log = logging.getLogger("announce")


def _daily_period(now: int) -> tuple[str, int, int]:
    """(iso_date, start_ts, end_ts) for the most recently COMPLETED
    local day -- the calendar day immediately before `now`'s own local
    date, in settings.checkin_net_timezone (the same app-wide local
    clock app/discord_notify.py's _weekly_recap_period() and
    _due_net_wrapups(), and app/results.py's month_bounds(), all
    already use).

    Simpler than _weekly_recap_period() above it needs to be: "today" is
    never complete while `now` is inside it, so the most recently
    completed day is always exactly local-yesterday -- there is no
    weekday arithmetic to do, and unlike a week or a net's own schedule
    there is only ever one candidate day to ask about. Still SELF-
    HEALING in the same sense that function's own docstring describes:
    this answers "which day most recently finished" on every call, on
    any day, not "is it the trigger moment right now," so it is exactly
    as stable across repeated calls within the same calendar day as
    _weekly_recap_period() is within the same week -- daily_provider()
    below can call this at any hour and get back the same
    (iso_date, start_ts, end_ts) every time until the next local
    midnight passes.

    Built from local calendar midnights (today's, minus one calendar
    day), not a fixed 86400-second offset, so a day that crosses a
    daylight-saving change is still exactly that calendar day, never an
    hour short or long -- the same reasoning app/results.py's
    month_bounds() and _weekly_recap_period()'s own week window apply to
    their own boundaries.

    CRITICAL, same as _weekly_recap_period(): this returns only the
    single most recently completed day, never a backlog of unannounced
    days stretching back through an outage -- daily_provider() below
    combines this with THE BACKLOG RULE (this module's own docstring)
    for the same "never dump stale news" reason.
    """
    tz = ZoneInfo(settings.checkin_net_timezone)
    local = datetime.fromtimestamp(now, tz=tz)
    today_midnight = datetime(local.year, local.month, local.day, tzinfo=tz)
    start_local = today_midnight - timedelta(days=1)
    start_ts = int(start_local.timestamp())
    end_ts = int(today_midnight.timestamp())
    return start_local.date().isoformat(), start_ts, end_ts


def _period_too_old(period_end_ts: int, now: int, max_age_hours: int | None = None) -> bool:
    """THE BACKLOG RULE itself (see this module's own docstring): True
    once `period_end_ts` -- the moment the period under consideration
    ended -- is more than `max_age_hours` in the past, relative to
    `now`. Every provider below calls this BEFORE doing any real work
    for a period, right after computing that period's own bounds and
    well before touching the `announcement` table or any heavier one --
    pure arithmetic, no DB access, so a provider whose most recent
    candidate period is already too old costs nothing beyond computing
    that period's bounds in the first place.

    `max_age_hours` defaults to settings.announcement_max_age_hours --
    the shared window every provider except month_provider() uses.
    month_provider() passes settings.announcement_month_max_age_hours
    instead (see that setting's own comment in app/config.py for why
    the month needs a longer one): this stays the one shared helper
    rather than being duplicated per-provider, with the age itself as
    the only thing that varies by caller.
    """
    if max_age_hours is None:
        max_age_hours = settings.announcement_max_age_hours
    max_age_seconds = max_age_hours * 3600
    return (now - period_end_ts) > max_age_seconds


def _active_season_protocols(conn: sqlite3.Connection) -> list[str]:
    """Every protocol with a currently active mc_season row, cheap
    (mc_season is a tiny table -- one row per season, the same table
    app/discord_notify.py's own _active_recap_protocols() reads for
    exactly this question) and in a stable, deterministic order:
    SQLite's own ORDER BY protocol sorts 'mc' before 'mt' alphabetically,
    which happens to be the same fixed order _active_recap_protocols()
    gets from _PROTOCOL_NAMES's own key order, without this module
    needing that private dict at all. A protocol with no active season
    (never started, or between seasons) is left out entirely -- there is
    no season for a day or week's standing to be a period INSIDE of.
    """
    return [
        row["protocol"] for row in conn.execute(
            "SELECT DISTINCT protocol FROM mc_season WHERE status = 'active' ORDER BY protocol"
        ).fetchall()
    ]


def daily_provider(conn: sqlite3.Connection, now: int) -> list[dict]:
    """One Content per protocol with an active season, for the most
    recently completed local day (_daily_period()) -- None from
    build_daily_content() (a quiet day) is simply left out, same as
    every other provider here.
    """
    iso_date, start_ts, end_ts = _daily_period(now)
    if _period_too_old(end_ts, now):
        return []

    items: list[dict] = []
    for board in _active_season_protocols(conn):
        key = f"{iso_date}:{board}"
        # THE dueness test -- see this module's own docstring. A row
        # already existing for this (kind, key), here or from a prior
        # call, means this day is not due for THIS board; nothing else
        # decides that question. Checked before build_daily_content()
        # (which reads ownership_at() against mc_tile_capture_log, a
        # large table) ever runs.
        already = conn.execute(
            "SELECT 1 FROM announcement WHERE kind = 'daily_recap' AND key = ?", (key,),
        ).fetchone()
        if already is not None:
            continue
        content = announce_content.build_daily_content(conn, board, start_ts, end_ts, now)
        if content is None:
            continue
        items.append(content)
    return items


def weekly_provider(conn: sqlite3.Connection, now: int) -> list[dict]:
    """One Content per protocol with an active season, for the most
    recently completed week (discord_notify._weekly_recap_period()) --
    reusing that function rather than recomputing week boundaries a
    second way, so this feed and the Discord weekly recap can never
    disagree about when a week ended. See this module's own docstring
    for why that cross-module reach is safe here.
    """
    period_key, start_ts, end_ts = discord_notify._weekly_recap_period(now)
    if _period_too_old(end_ts, now):
        return []

    items: list[dict] = []
    for board in _active_season_protocols(conn):
        key = f"{period_key}:{board}"
        already = conn.execute(
            "SELECT 1 FROM announcement WHERE kind = 'weekly_recap' AND key = ?", (key,),
        ).fetchone()
        if already is not None:
            continue
        content = announce_content.build_weekly_content(conn, board, start_ts, end_ts, now)
        if content is None:
            continue
        items.append(content)
    return items


def month_provider(conn: sqlite3.Connection, now: int) -> list[dict]:
    """The most recently FROZEN month per board that has no
    `announcement` row yet -- app/results.py's month_result is the
    freeze marker (the same table freeze_month() writes and
    month_results_for() checks), read here instead of scanning
    month_award/month_standing (or, far worse, mc_tile_capture_log)
    directly: month_result is tiny (one row per month per protocol,
    a handful a year), so reading all of it is cheap even years in.

    "Most recently frozen," not "every frozen month with no row yet":
    only the single latest-closed month per board is ever considered --
    the same no-backlog shape _weekly_recap_period()'s own CRITICAL note
    describes, applied here explicitly because month_result, unlike a
    week, keeps every past month's freeze marker around forever, so
    without this a provider walking it naively would find every
    never-announced month back to the dawn of the season and announce
    them all in one burst. Combined with THE BACKLOG RULE's own age
    cutoff below (checked against THIS single candidate month, before
    build_month_content() runs), this is exactly what keeps a fresh
    deployment, or one resuming after a long outage, from dumping a run
    of old monthly honors into the feed -- see this module's own
    docstring for the Discord-side precedent this is written to avoid
    repeating.
    """
    items: list[dict] = []
    boards = [
        row["protocol"] for row in conn.execute(
            "SELECT DISTINCT protocol FROM month_result ORDER BY protocol"
        ).fetchall()
    ]
    for board in boards:
        latest = conn.execute(
            "SELECT month FROM month_result WHERE protocol = ? ORDER BY closed_at DESC LIMIT 1",
            (board,),
        ).fetchone()
        if latest is None:
            continue
        month = latest["month"]
        key = f"{month}:{board}"
        already = conn.execute(
            "SELECT 1 FROM announcement WHERE kind = 'month_honors' AND key = ?", (key,),
        ).fetchone()
        if already is not None:
            continue
        _start_ts, end_ts = results.month_bounds(month)
        # settings.announcement_month_max_age_hours, NOT the shared
        # announcement_max_age_hours every other provider here uses --
        # see that setting's own comment in app/config.py. A month's
        # announcement is a one-shot: once this month is skipped as too
        # old, or announced, store_announcement()'s exactly-once
        # (kind, key) index means a re-freeze of the SAME month can
        # never create it again -- there is no later chance to catch up
        # the way a fresh day or week naturally provides one next
        # cycle. The longer window is what keeps an ordinary few-day
        # outage from silently losing a month's announcement forever.
        if _period_too_old(end_ts, now, settings.announcement_month_max_age_hours):
            continue
        content = announce_content.build_month_content(conn, board, month, now)
        if content is None:
            continue
        items.append(content)
    return items


def _net_wrapup_period_end_ts(net_row, net_date: str) -> int:
    """The same period_end_ts app/announce_content.py's
    build_net_wrapup_content() computes for this exact (net_row,
    net_date) -- reproduced here, cheaply and with no DB access, so
    net_wrapup_provider() below can apply THE BACKLOG RULE's age cutoff
    BEFORE calling that builder (which does hit mc_checkin_award). Must
    stay in lockstep with that function's own computation -- see its
    docstring for why one hour is added to end_hour (the wrap-up covers
    the net's full closing hour, not just its start).
    """
    tz = ZoneInfo(net_row["timezone"])
    d = datetime.strptime(net_date, "%Y-%m-%d")
    return int(
        (datetime(d.year, d.month, d.day, net_row["end_hour"], tzinfo=tz) + timedelta(hours=1)).timestamp()
    )


def net_wrapup_provider(conn: sqlite3.Connection, now: int) -> list[dict]:
    """One Content per due net wrap-up, via
    discord_notify._due_net_wrapups() -- reusing that function rather
    than recomputing "is this net's wrap-up due" a second way, so this
    feed and Discord's own per-net wrap-up can never disagree about
    which occurrence is due. See this module's own docstring for why
    that cross-module reach is safe here.
    """
    items: list[dict] = []
    for due in discord_notify._due_net_wrapups(conn, now):
        net, net_date = due["net"], due["net_date"]
        key = f"{net['id']}:{net_date}"
        already = conn.execute(
            "SELECT 1 FROM announcement WHERE kind = 'net_wrapup' AND key = ?", (key,),
        ).fetchone()
        if already is not None:
            continue
        if _period_too_old(_net_wrapup_period_end_ts(net, net_date), now):
            continue
        content = announce_content.build_net_wrapup_content(conn, net, net_date, now)
        if content is None:
            continue
        items.append(content)
    return items


# Every provider this module registers -- see this module's own
# docstring for the provider shape (conn, now) -> list[Content]. Order
# has no correctness meaning (each provider's dueness is independent,
# decided entirely by its own (kind, key) checks against `announcement`)
# but is kept in the same rough order the four Content builders appear
# in app/announce_content.py, for a reader comparing the two files.
ANNOUNCEMENT_PROVIDERS: list = [daily_provider, weekly_provider, month_provider, net_wrapup_provider]


def check_due(conn: sqlite3.Connection, now: int) -> int:
    """Call every provider in ANNOUNCEMENT_PROVIDERS once, and
    store_announcement() whatever Content each one says is due right
    now. Returns how many were newly stored (store_announcement()'s own
    non-None return, counted) -- 0 for an idle registry, and 0 again for
    a second call in the same period once every item's (kind, key) is
    already in `announcement`, the same "calling this twice in the same
    period is always exactly as safe as calling it once" guarantee
    app/discord_notify.py's check_due_time_driven() gives, for the same
    reason: dueness here is decided ENTIRELY by that table's own
    UNIQUE(kind, key) index via store_announcement()'s INSERT OR IGNORE.

    SYNC, and takes the CALLER's own connection -- same shape as
    check_due_time_driven(): a provider that needs DB access uses THIS
    connection, inside whatever transaction the caller already has open,
    rather than opening a second one of its own.

    Never raises: a single misbehaving provider is logged and skipped,
    exactly like check_due_time_driven()'s own contract, so one bad
    provider can never stop a later one in the same call, or a later
    call to this function on the next cycle.
    """
    stored = 0
    for provider in ANNOUNCEMENT_PROVIDERS:
        try:
            items = provider(conn, now)
        except Exception:
            log.exception("announce: provider failed, skipping it this cycle")
            continue
        for content in items:
            if announce_content.store_announcement(conn, content) is not None:
                stored += 1
    return stored


# Monotonic timestamp of the last time maybe_run() actually decided to
# run check_due() (whether or not that check_due() call itself found
# anything due) -- managed entirely by maybe_run() itself, module-level,
# same shape app/discord_leaderboard.py's _last_leaderboard_run_at and
# app/discord_bot.py's _last_reconcile_gate_at both use for their own
# interval gates on this identical loop. Reset directly by name in a
# test fixture (monkeypatch.setattr(announce, "_last_announce_check_at",
# 0.0)), the same way those two siblings' own tests reset theirs,
# instead of a caller-supplied object -- three features gating three
# different ways inside one loop would make that loop harder to follow
# than the small cost of shared module state resettable by tests.
_last_announce_check_at = 0.0


async def maybe_run(now: int) -> int:
    """Interval-gated wrapper app/discord_notify.py's run_forever()
    calls once per its own poll cycle -- same "one background loop, its
    own interval gate" shape as that module's maybe_reconcile_roles()/
    maybe_run_leaderboard() calls, gated here by
    settings.announcement_poll_interval_seconds rather than a fixed
    constant, since a due-check's real cost varies with how many boards
    and nets are configured.

    The gate is checked against time.monotonic(), NEVER against `now`
    (wall clock, int(time.time()), still taken as a parameter and passed
    straight through to check_due() below -- the PERIOD math genuinely
    needs wall-clock time: _daily_period(), discord_notify._weekly_
    recap_period(), and discord_notify._due_net_wrapups() all reason
    about real calendar dates). Same distinction app/discord_leaderboard.
    py's maybe_run_leaderboard() and app/discord_bot.py's maybe_
    reconcile_roles() both draw, for the same reason: a wall-clock gate
    breaks the moment NTP steps the system clock backward, since
    `now - last_checked_at` can go negative and block every due-check
    for the entire length of that step, silently stalling the whole
    feed. time.monotonic() never steps backward, so the GATE itself
    cannot be fooled by a clock correction even though the WORK it gates
    still reasons in wall-clock time throughout.

    Updates _last_announce_check_at UNCONDITIONALLY, before check_due()
    ever runs -- deliberately, same reasoning app/discord_leaderboard.py's
    run_leaderboard_pass() and app/discord_bot.py's reconcile_all() both
    give for updating their own gate first: this must never retry every
    single tick just because one due-check failed (WriteSession
    contention, or any exception check_due() itself did not already
    catch and skip) -- the next attempt still waits out a full interval,
    exactly as a successful one would.

    Opens its OWN WriteSession -- same pattern
    _check_due_time_driven_once() uses for check_due_time_driven(): a
    due-check is pure DB work end to end (the cheap reads each provider's
    own docstring requires, plus a handful of store_announcement()'s
    INSERT OR IGNOREs), so holding the write lock for the whole check is
    fine and simpler than juggling two connections.
    """
    global _last_announce_check_at
    interval = settings.announcement_poll_interval_seconds
    if time.monotonic() - _last_announce_check_at < interval:
        return 0
    _last_announce_check_at = time.monotonic()
    async with WriteSession() as conn:
        return check_due(conn, now)
