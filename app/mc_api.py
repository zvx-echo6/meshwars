"""FastAPI router for the MeshCore board/scores/players/cell endpoints.

NOT mounted here -- a later change wires this into the app the same way
app/api.py wires its own router (see `mount()` there). This module only
builds the router.

The mc_season / mc_season_team_tally / mc_tile / mc_tile_score /
mc_tile_capture / mc_tile_capture_log tables are being added by a
different change landing in parallel with this one. Until that lands
(or if it lands with a different column set than expected), every
route below is expected to see sqlite3.OperationalError("no such
table: mc_*") -- so every query goes through `_safe_query`, which
catches "no such table" and degrades to an empty result instead of a
500. That is a deliberate landing-order guard, not defensive
programming against a real failure mode. (The mc_* tables have existed
unconditionally since that parallel change landed, so in practice the
guard no longer triggers -- it is kept because it is still correct and
harmless, not because the race it was written for is still live.)

Several of the functions below (active_season, board_for, scores_for,
history_for, cell_detail_for, find_for, top_for, top_checkin_for,
top_explorer_for, season_team_tally, winner_banner_active, winner_banner_for,
latest_closed_season, team_list) take an explicit `protocol` argument
and are called from here with MC_PROTOCOL -- and, now that the
Meshtastic board runs on this same mc_season/mc_tile* model, also
called from app/api.py with 'mt', so the query logic for each pair of
near-identical mc/mt endpoints lives in exactly one place. None of that
changes what any /api/mc/* route returns; every route here still passes
MC_PROTOCOL explicitly, same as before this module had a second caller.
"""
from __future__ import annotations

import re
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from .auth import Principal, new_rate_limit_bucket, require_api_key_principal
from .client_ip import get_client_ip
from .config import settings
from .db import connect
from .grid import cell_bounds
from .mc_ingest import PROTOCOL as MC_PROTOCOL
from .mc_scoring import team_checkin_points, team_place_points, team_tile_counts
from .places_api import _stable_tiebreak
from .sessions import SessionPrincipal, optional_session, require_session
from . import results

router = APIRouter()

# Bare literal, not `from .ingest import PROTOCOL as MT_PROTOCOL` the way
# MC_PROTOCOL above is imported from app/mc_ingest.py: app/ingest.py
# imports app/api.py (for _node_hex), and app/api.py imports this module
# to mount its router, so importing app/ingest.py from here would close
# a cycle (mc_api -> ingest -> api -> mc_api). 'mt' is exactly as fixed a
# value as MC_PROTOCOL stands in for 'mc' -- app/ingest.py's own PROTOCOL
# constant is this same string.
MT_PROTOCOL = "mt"

# ---- status-check authentication ---------------------------------------
#
# app/auth.py's require_api_key_principal(), configured to match this
# endpoint's own original (and still different from the other four key-
# authenticated endpoints') shape: a pre-auth address limiter using its
# own new_rate_limit_bucket() instance -- same pattern as
# app/join_api.py's join _attempts/_rate_limited (itself modeled on
# McIngestor._key_cache in app/mc_ingest.py) -- but with this endpoint's
# own, different rate-limited message ("too many attempts, try again
# later", not the generic "rate limited" the other four use), and NO
# post-auth per-key limiter at all -- this route never had one. See
# app/auth.py's module docstring for the full list of what stayed
# different on purpose.
_status_addr_rate_limiter = new_rate_limit_bucket()

require_status_principal = require_api_key_principal(
    pre_auth_limiter=_status_addr_rate_limiter,
    pre_auth_rate_limit_detail="too many attempts, try again later",
    # A logged-in browser session is just as good a credential as this
    # player's own key for checking their own status -- a person
    # loading a page, not a machine posting a batch. See app/auth.py's
    # module docstring ("opt-in") for why this is one of the four sites
    # that ask for it and app/api.py's POST /api/mc/ingest is the one
    # that doesn't.
    allow_session_fallback=True,
)

# ---- player-lookup ("find") rate limiting -------------------------------
#
# Address-keyed budget for GET /api/mc/find. app/api.py's GET /find (the
# Meshtastic counterpart, same find_for() helper) keeps its own separate
# _BoundedHits instance rather than sharing this one -- same "state is
# never shared across call sites, even ones that read the same
# settings" reasoning app/auth.py's module docstring already gives for
# every other rate limiter in this app. Session-gated now (see
# mc_find() below and app/sessions.py's require_session), but a session
# only proves WHO is asking, not that they get to ask without limit --
# see find_rate_limit_attempts/window_seconds' own comment in
# app/config.py for why this exists even behind a login.
_find_addr_rate_limiter = new_rate_limit_bucket()


def _find_rate_limited(request: Request) -> bool:
    return _find_addr_rate_limiter.limited(
        get_client_ip(request),
        limit=settings.find_rate_limit_attempts,
        window=settings.find_rate_limit_window_seconds,
    )


# Fallback roster used only if `settings` does not (yet) expose a
# MeshCore team list, or exposes it under a name this module doesn't
# know about yet -- keeps these routes usable even if the in-flight
# config change lands after this file does. Matches the seven
# MeshCore team colors (see frontend/mc.js TEAM_COLORS).
_FALLBACK_TEAMS = ["RED", "GREEN", "BLUE", "PURPLE", "YELLOW", "ORANGE", "PINK"]


def team_list() -> list[str]:
    """Read the MeshCore team roster from settings.

    Tries a few plausible attribute names/shapes so this keeps working
    however the in-flight config change ends up naming the field.
    Falls back to the fixed seven-team roster if none of them exist.
    """
    for attr in ("mc_teams", "mc_team_list", "teams"):
        val = getattr(settings, attr, None)
        if val is None:
            continue
        if isinstance(val, str):
            parsed = [t.strip().upper() for t in val.split(",") if t.strip()]
            if parsed:
                return parsed
        elif isinstance(val, (list, tuple)):
            parsed = [str(t).strip().upper() for t in val if str(t).strip()]
            if parsed:
                return parsed
    return list(_FALLBACK_TEAMS)


def _relative_time(now_ts: int, then_ts: int) -> str:
    """Human phrase for the diagnosis sentence, e.g. "3 minutes ago"."""
    delta = max(0, now_ts - then_ts)
    if delta < 60:
        return "just now"
    minutes = delta // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def _diagnose(alltime: dict, squares_held: int, last_batch_at: int | None, now_ts: int) -> tuple[str, str]:
    """Work out, server-side, why (if at all) a player isn't scoring.

    Applied in order, first match wins -- see the counters this reads
    in player_ingest_stat, written by app/mc_ingest.py's
    _process_one_ping(). `alltime` is the player's all-time totals
    (not just today), because "never received anything", "only ever
    no-contact", etc. describe a standing setup problem, not a bad day.
    """
    total_pings = (
        alltime["pings_bad_coord"] + alltime["pings_out_of_area"]
        + alltime["pings_no_contact"] + alltime["pings_wrong_owner"]
        + alltime["pings_duplicate"] + alltime["pings_accepted"]
    )

    if alltime["batches"] == 0:
        return "never_received", (
            "MeshWars has never received anything from your app. Check that "
            "Custom API Endpoint is switched on in MeshMapper, that the URL "
            "is right, and that a wardriving session is actually running."
        )

    if total_pings > 0 and alltime["pings_no_contact"] == total_pings:
        return "no_contact_key", (
            "Your batches are arriving but none of the pings can be "
            "attributed to you. Turn on Include Contact Key in MeshMapper."
        )

    if total_pings > 0 and alltime["pings_out_of_area"] == total_pings:
        return "out_of_area", (
            "Your positions are outside the play area, which is Southern "
            "Idaho and Northern Utah."
        )

    if alltime["pings_wrong_owner"] > 0:
        return "wrong_owner", (
            "A radio reporting under your key is registered to someone "
            "else. Your key may have been shared or copied."
        )

    if (
        alltime["pings_accepted"] > 0
        and alltime["pings_no_repeaters"] == alltime["pings_accepted"]
        and squares_held == 0
    ):
        return "no_repeaters", (
            "Everything is working. Your positions are arriving, but none "
            "of them heard a repeater, so no squares were claimed. That "
            "means you are out of range of the mesh, not misconfigured."
        )

    when = _relative_time(now_ts, last_batch_at) if last_batch_at else "never"
    square_word = "square" if squares_held == 1 else "squares"
    return "ok", f"Working. Last heard {when}. You hold {squares_held} {square_word}."


def _diagnose_mt(alltime: dict, squares_held: int, last_heard_at: int | None, now_ts: int) -> tuple[str, str]:
    """The Meshtastic equivalent of _diagnose() above -- same
    first-match-wins shape over the player's all-time totals, but
    different failure modes, because Meshtastic is pulled from meshview
    instead of pushed by a wardriving app (see app/ingest.py's module
    docstring). There is no MeshMapper on this side, so none of that
    app's advice belongs in these messages.

    Several genuinely different real-world causes -- the node isn't
    broadcasting a position at all, its OK to MQTT setting is off so no
    gateway ever forwards what it does send, or the node ID registered
    here doesn't match the radio actually transmitting -- are
    indistinguishable from this side: every one of them looks exactly
    like "we have never picked up a single packet for this player", and
    there is no server-side signal that tells them apart from data that,
    by definition, was never received. So the first branch below names
    all of them rather than falsely diagnosing just one.
    """
    # pings_low_precision/pings_implausible_speed (2026-08-25) count
    # here too: both mean a real packet WAS picked up and rejected, not
    # that nothing arrived -- leaving them out of `total` would make
    # mt_never_seen fire for a node whose every fix trips one of those
    # gates, which is actively wrong (see app/ingest.py for the gates
    # themselves).
    total = (
        alltime["pings_accepted"] + alltime["pings_duplicate"]
        + alltime["pings_bad_coord"] + alltime["pings_out_of_area"]
        + alltime["pings_low_precision"] + alltime["pings_implausible_speed"]
    )

    if total == 0:
        return "mt_never_seen", (
            "MeshWars has never picked up a position packet from this node. "
            "Check that the node ID registered here matches the radio "
            "you're actually using, that OK to MQTT is turned on under "
            "Radio Configuration → LoRa, that the node has a GPS fix "
            "and is broadcasting its position, and that you're within "
            "range of a FREQ51 gateway feeder."
        )

    if alltime["pings_out_of_area"] == total:
        return "mt_out_of_area", (
            "Your positions are outside the play area, which is Southern "
            "Idaho and Northern Utah."
        )

    if alltime["pings_bad_coord"] == total:
        return "mt_bad_position", (
            "MeshWars is picking up packets from this node, but without a "
            "usable position. Check that it has a GPS fix, and that "
            "position precision on your channel isn't reduced to the "
            "point coordinates are stripped out."
        )

    if alltime["pings_low_precision"] == total:
        return "mt_low_precision", (
            "MeshWars is picking up packets from this node, but its "
            "position precision is set too coarse to trust for a "
            "300-metre square -- the radio is only reporting roughly "
            "which kilometre it's in, not which square. Raise position "
            "precision on your channel so squares can be credited."
        )

    if (
        alltime["pings_accepted"] > 0
        and alltime["pings_no_repeaters"] == alltime["pings_accepted"]
        and squares_held == 0
    ):
        return "mt_no_feeder", (
            "Everything is working. Your positions are arriving, but no "
            "FREQ51 gateway feeder is hearing this node, so no squares "
            "were claimed. That means you are out of range of a feeder, "
            "not misconfigured."
        )

    when = _relative_time(now_ts, last_heard_at) if last_heard_at else "never"
    square_word = "square" if squares_held == 1 else "squares"
    return "mt_ok", f"Working. Last heard {when}. You hold {squares_held} {square_word}."


def _safe_query(fn):
    """Run fn(conn) with a fresh read connection.

    Returns fn's result, or None if fn raises sqlite3.OperationalError
    for a missing table (the mc_* schema not landed yet). Any other
    error is a real bug and is re-raised.
    """
    conn = connect()
    try:
        return fn(conn)
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return None
        raise
    finally:
        conn.close()


def active_season(conn, protocol: str) -> sqlite3.Row | None:
    """The active season for `protocol` ('mc' or 'mt'). Every mc_* AND
    (now that the Meshtastic board has moved onto mc_season too, see
    app/api.py) mt route reads through this one helper rather than
    querying mc_season directly, so the protocol filter only has to be
    written in one place -- an unfiltered query would otherwise treat an
    'mt' season being active as indistinguishable from an 'mc' one, and
    vice versa. Every call site in this module passes MC_PROTOCOL
    explicitly (never left to a default) so it stays impossible to reuse
    one of these helpers for the wrong board by accident.
    """
    return conn.execute(
        "SELECT id, started_at, ends_at, status, winner FROM mc_season "
        " WHERE protocol = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
        (protocol,),
    ).fetchone()


def latest_closed_season(conn, protocol: str) -> sqlite3.Row | None:
    """The most recently closed season for `protocol`, or None if this
    board has never closed one yet. New helper -- no existing /api/mc/*
    route needed a single "latest closed" season (mc_history returns
    every closed season), this exists for app/api.py's /season and
    /config winner-banner logic, which the old (now-deleted)
    app/seasons.py provided for the legacy `season` table.
    """
    return conn.execute(
        "SELECT id, started_at, ends_at, status, winner FROM mc_season "
        " WHERE protocol = ? AND status = 'closed' ORDER BY id DESC LIMIT 1",
        (protocol,),
    ).fetchone()


def winner_banner_active(closed_season: sqlite3.Row | dict | None, now_ts: int) -> bool:
    """Is the winner banner for `closed_season` still inside its display
    window (settings.winner_banner_hours after ends_at)? Protocol-agnostic
    -- it only looks at the row it's given, same contract the deleted
    app/seasons.py version had for the legacy `season` table.
    """
    if not closed_season:
        return False
    return now_ts < closed_season["ends_at"] + settings.winner_banner_hours * 3600


def winner_banner_for(conn, protocol: str, now_ts: int) -> dict | None:
    """The full seven-team-shaped winner banner for `protocol`'s most
    recently closed season, or None if there is no closed season yet or
    its display window (winner_banner_active, settings.winner_banner_hours
    after ends_at) has elapsed.

    This is the same shape app/api.py's /config used to build inline for
    the Meshtastic board only -- factored out here, and now called with
    both protocols, so /config, this module's /api/mc/season, and
    app/api.py's /season all build the identical banner instead of
    hand-copied near-duplicates. Follows the same *_for() pattern as
    board_for/scores_for/history_for/cell_detail_for/find_for/top_for
    above, except this one also needs `now_ts` -- winner_banner_active()
    already takes it as an explicit argument rather than reading the
    clock itself (so its own callers can pass a stable ts across
    multiple checks in one request), and this helper is layered
    directly on top of it.
    """
    closed = latest_closed_season(conn, protocol)
    if not winner_banner_active(closed, now_ts):
        return None
    tallies = season_team_tally(conn, closed["id"])
    checkin_tallies = season_team_checkin_points(conn, closed["id"])
    return {
        "season_id": closed["id"],
        "started_at": closed["started_at"],
        "ends_at": closed["ends_at"],
        "winner": closed["winner"],
        "teams": [
            {
                "team": t,
                "tiles": tallies.get(t, 0),
                "checkin_points": checkin_tallies.get(t, 0.0),
                "total": tallies.get(t, 0) + checkin_tallies.get(t, 0.0),
            }
            for t in team_list()
        ],
    }


def season_team_tally(conn, season_id: int) -> dict[str, int]:
    """team -> tiles for a season's final tally, from mc_season_team_tally
    (written once at season close, see mc_scoring.maybe_roll_season).
    Squares only -- see season_team_checkin_points() for the check-in
    half of a closed season's standing, and combine the two (as every
    caller below does) for the figure that actually decided the winner.
    Shared by mc_history() below and app/api.py's /config winner banner,
    which both need a closed season's per-team standings.
    """
    rows = conn.execute(
        "SELECT team, tiles FROM mc_season_team_tally WHERE season_id = ?",
        (season_id,),
    ).fetchall()
    return {r["team"]: r["tiles"] for r in rows}


def season_team_checkin_points(conn, season_id: int) -> dict[str, float]:
    """team -> check-in points for a season's final tally, from the
    checkin_points column mc_scoring.maybe_roll_season writes alongside
    tiles at season close. Sibling of season_team_tally() above -- same
    source table, same "written once at close" contract, split out
    because the two figures are shown separately as well as combined.
    """
    rows = conn.execute(
        "SELECT team, checkin_points FROM mc_season_team_tally WHERE season_id = ?",
        (season_id,),
    ).fetchall()
    return {r["team"]: r["checkin_points"] for r in rows}


# ---- board response cache ---------------------------------------------

# key -> (built_at_monotonic, serialized JSON bytes). Small and bounded:
# one entry per cached route, never per request or per caller.
_BOARD_CACHE: dict[str, tuple[float, bytes]] = {}


def cached_json_response(key: str, build) -> Response:
    """Serve `build()`'s result as JSON, reusing the bytes for up to
    settings.board_cache_seconds.

    Caches the SERIALIZED bytes, not the Python object, deliberately:
    the expensive part of a board request is not just the query, it is
    walking several thousand rows through cell_bounds() and then
    serializing the result -- caching the object would still repeat the
    serialization for every viewer. Bytes also cannot be mutated by a
    caller the way a shared list of dicts could.

    Time-based only, with no invalidation hook on the ingest path. The
    board is already up to a full client refresh interval stale in every
    browser showing it, so a few seconds of server-side staleness is
    invisible; an explicit invalidation would couple the scoring path to
    this cache for no gain a reader could ever perceive.

    settings.board_cache_seconds = 0 disables it and rebuilds every time.
    """
    ttl = settings.board_cache_seconds
    now = time.monotonic()
    if ttl > 0:
        hit = _BOARD_CACHE.get(key)
        if hit is not None and now - hit[0] < ttl:
            return Response(content=hit[1], media_type="application/json")

    body = json.dumps(build(), separators=(",", ":")).encode()
    if ttl > 0:
        _BOARD_CACHE[key] = (now, body)
    return Response(content=body, media_type="application/json")


def board_for(protocol: str) -> list[dict]:
    """Every owned cell in the active season for `protocol`, with bounds
    computed server-side so the browser never has to reimplement the
    grid maths in app.grid.

    Factored out of mc_board() below so app/api.py's Meshtastic
    /get-nodes route can build its own (richer) coverage list from the
    same raw ownership rows instead of re-deriving them -- see that
    module for how it enriches these with per-cell scores/capture time.
    """

    def run(conn):
        season = active_season(conn, protocol)
        if not season:
            return []
        rows = conn.execute(
            "SELECT cell_id, owner_team, last_report_ts, paint_count "
            "  FROM mc_tile WHERE season_id = ? AND owner_team IS NOT NULL",
            (season["id"],),
        ).fetchall()
        out = []
        for r in rows:
            south, west, north, east = cell_bounds(r["cell_id"])
            out.append({
                "cell_id": r["cell_id"],
                "owner_team": r["owner_team"],
                "last_report_ts": r["last_report_ts"],
                "paint_count": r["paint_count"],
                "south": south,
                "west": west,
                "north": north,
                "east": east,
            })
        return out

    result = _safe_query(run)
    return result if result is not None else []


@router.get("/api/mc/board")
async def mc_board() -> Response:
    """Every owned cell in the active MeshCore season. See board_for().

    The heaviest route on the site by an order of magnitude, and the one
    every open map tab re-fetches on a timer -- served through
    cached_json_response so viewers share one build.
    """
    return cached_json_response("mc_board", lambda: board_for(MC_PROTOCOL))


def scores_for(protocol: str) -> dict:
    """Active season id/window plus every team's current standing for
    `protocol`. Always returns all seven teams (zero-filled) so the
    scoreboard doesn't jump around as teams appear on the board.

    Each team's "total" here matches team_totals() in mc_scoring.py --
    squares held plus check-in points plus Places Worth Going points --
    so the number the board displays is the same number that decides
    the season and month standings. The three components are also
    returned separately (tiles, checkin_points, explorer_points) so the
    frontend can show a breakdown, not just the sum. "explorer_points"
    matches the field name find_for() already uses above for the same
    Places Worth Going figure on a single player, rather than inventing
    a second name ("place_points") for the identical concept.

    Factored out of mc_scores() below so app/api.py's Meshtastic /scores
    route returns the exact same shape for its own board instead of a
    hand-copied near-duplicate.
    """

    def run(conn):
        season = active_season(conn, protocol)
        if not season:
            return None
        # Live counts, not the season-close tally: mc_season_team_tally is
        # only populated by maybe_roll_season() when a season CLOSES, so it
        # stays empty/stale for the entire span of an active season. Count
        # current ownership straight from mc_tile, current check-in points
        # straight from mc_checkin_award, and current Places Worth Going
        # points straight from place_activation, via the same helpers
        # season rollover itself uses, so this always matches the live
        # board. mc_season_team_tally is still the correct source for
        # historical (closed) season standings -- leave it alone, don't
        # wire it back in here.
        tile_counts = team_tile_counts(conn, season["id"])
        checkin_points = team_checkin_points(conn, season["id"])
        explorer_points = team_place_points(conn, season["id"])
        return {
            "season_id": season["id"],
            "started_at": season["started_at"],
            "ends_at": season["ends_at"],
            "teams": [
                {
                    "team": t,
                    "tiles": tile_counts.get(t, 0),
                    "checkin_points": checkin_points.get(t, 0.0),
                    "explorer_points": explorer_points.get(t, 0.0),
                    "total": (
                        tile_counts.get(t, 0)
                        + checkin_points.get(t, 0.0)
                        + explorer_points.get(t, 0.0)
                    ),
                }
                for t in team_list()
            ],
        }

    result = _safe_query(run)
    if result is not None:
        return result
    return {
        "season_id": None,
        "started_at": None,
        "ends_at": None,
        "teams": [
            {"team": t, "tiles": 0, "checkin_points": 0.0, "explorer_points": 0.0, "total": 0.0}
            for t in team_list()
        ],
    }


@router.get("/api/mc/scores")
async def mc_scores() -> dict:
    """Active season id/window plus every team's current tile count for
    MeshCore. See scores_for()."""
    return scores_for(MC_PROTOCOL)


@router.get("/api/mc/players")
async def mc_players() -> list[dict]:
    """display_name + team for every non-disabled player.

    Deliberately excludes player_id, key hashes, or anything else --
    display_name/team are the only fields the frontend roster needs.
    """

    def run(conn):
        rows = conn.execute(
            "SELECT display_name, team FROM player WHERE disabled_at IS NULL"
        ).fetchall()
        return [{"display_name": r["display_name"], "team": r["team"]} for r in rows]

    result = _safe_query(run)
    return result if result is not None else []


def history_for(protocol: str) -> list[dict]:
    """Closed seasons for `protocol`, newest first, each with its final
    per-team tile tally.

    mc_season_team_tally (see season_team_tally() above), not mc_tile, is
    the correct source here: it is written once at season close (see
    maybe_roll_season() in mc_scoring.py) and is the only place a closed
    season's standings still live, since mc_tile itself moves on to the
    next season.

    Factored out of mc_history() below so app/api.py's Meshtastic
    /history route returns the exact same per-season shape instead of a
    hand-copied near-duplicate. Per the owner's explicit decision, the
    Meshtastic /history feed surfaces only 'mt' seasons under this
    player-model scheme -- the three legacy geohash-era seasons stay in
    the `season` table (never dropped) but are not read through here.
    """

    def run(conn):
        # protocol filter for the same reason as active_season() above:
        # an unfiltered query here would mix the two boards' seasons
        # into one history feed.
        seasons = conn.execute(
            "SELECT id, started_at, ends_at, winner FROM mc_season "
            " WHERE protocol = ? AND status = 'closed' ORDER BY id DESC",
            (protocol,),
        ).fetchall()
        out = []
        for s in seasons:
            tallies = season_team_tally(conn, s["id"])
            checkin_tallies = season_team_checkin_points(conn, s["id"])
            out.append({
                "id": s["id"],
                "started_at": s["started_at"],
                "ends_at": s["ends_at"],
                "winner": s["winner"],
                "teams": [
                    {
                        "team": t,
                        "tiles": tallies.get(t, 0),
                        "checkin_points": checkin_tallies.get(t, 0.0),
                        "total": tallies.get(t, 0) + checkin_tallies.get(t, 0.0),
                    }
                    for t in team_list()
                ],
            })
        return out

    result = _safe_query(run)
    return result if result is not None else []


@router.get("/api/mc/history")
async def mc_history() -> list[dict]:
    """Closed MeshCore seasons, newest first, each with its final
    per-team tile tally. See history_for()."""
    return history_for(MC_PROTOCOL)


@router.get("/api/mc/season")
async def mc_season_info() -> dict:
    """Active/closed season status plus the winner banner for the
    MeshCore board -- the namespaced counterpart to app/api.py's
    /season (which serves the same shape for Meshtastic, protocol='mt').

    Did not exist before this change: the winner banner UI was dropped
    when the map moved onto the unified renderer (see mc.js's module
    docstring) because the old markup expected the retired two-team
    red_tiles/blue_tiles/green_tiles shape. Rebuilding it seven-team
    shaped needed a MeshCore-side season+banner route to sit alongside
    the Meshtastic one that already existed, following the same
    /api/mc/* vs bare-path split every other paired MeshCore/Meshtastic
    route in this file already uses -- see winner_banner_for(), which
    this and /season both call rather than duplicating the banner-build
    logic a third time.
    """
    now_ts = int(time.time())
    conn = connect()
    try:
        active = active_season(conn, MC_PROTOCOL)
        active = dict(active) if active else None
        closed = latest_closed_season(conn, MC_PROTOCOL)
        closed = dict(closed) if closed else None
        banner = winner_banner_for(conn, MC_PROTOCOL, now_ts)
    finally:
        conn.close()
    return {
        "active": active,
        "latest_closed": closed,
        "winner_banner_active": banner is not None,
        "winner_banner": banner,
        "now": now_ts,
    }


def find_for(protocol: str, name: str) -> dict | None:
    """Case-insensitive exact match on a player's display name.

    Returns their team and how many cells they currently hold as last
    painter in `protocol`'s active season, plus the bounding box of
    those cells so the map can zoom to them. None if no such player
    exists at all -- mc_find() below turns that into a 404, same
    contract cell_detail_for() uses for "no such cell". A player who
    exists but holds nothing right now (no active season for this
    protocol, or simply no cells) is not a None here: it's a normal
    result with tiles_held=0 and bounds=None.

    The player table itself is not part of the mc_* landing-order race
    described at the top of this module (it predates it, and a player
    is not protocol-specific -- see app/db.py's player_node table: one
    person can hold both an 'mc' and an 'mt' radio under the same
    player row), so that lookup runs directly; only the season/mc_tile
    half -- which does the actual "holds cells" answer, and which does
    need the protocol filter -- is guarded against a missing table,
    degrading to zero cells rather than a 500.

    Factored out of mc_find() below so app/api.py's Meshtastic /find
    route returns the exact same shape instead of a hand-copied
    near-duplicate, following the same *_for() pattern as
    board_for/scores_for/history_for/cell_detail_for above.

    Also returns the player's own point breakdown -- capture points
    (tiles_held, the same figure team_tile_counts() sums per team),
    check-in points, and Explorer (Places Worth Going) points, plus
    their total -- so a player who is not on any Top Operators ranking
    still has a way to see their own numbers; this is the only lookup
    on the site that works for ANY registered player rather than just
    the top N of one ranking. Check-in points read mc_checkin_award the
    same way top_checkin_for() below does; Explorer points read
    place_activation the same TIME-windowed way
    mc_scoring.team_place_points() and public_api._player_rows() do,
    since that table has no season_id of its own. All three are 0 when
    there is no active season for `protocol` (nothing to attribute
    against), same degrade-to-zero find_for() already used for
    tiles_held/bounds before this pass.

    last_checkin_net_date and last_position_ts are cheap additions from
    tables already read elsewhere for the same player (public_api's
    _player_rows computes both the same way) -- a rough "when were they
    last active" signal. A merged, multi-table recent-activity FEED
    (captures + check-ins + Explorer activations interleaved by time)
    was deliberately left out of this pass: it needs new unioned
    queries this endpoint has never had to run before, not a reuse of
    an existing one.
    """
    conn = connect()
    try:
        player = conn.execute(
            "SELECT player_id, display_name, team FROM player "
            " WHERE disabled_at IS NULL AND LOWER(display_name) = LOWER(?)",
            (name,),
        ).fetchone()
        if not player:
            return None
        pid = player["player_id"]

        cell_ids: list[str] = []
        season = None
        try:
            season = active_season(conn, protocol)
            if season:
                rows = conn.execute(
                    "SELECT cell_id FROM mc_tile WHERE season_id = ? AND last_player_id = ?",
                    (season["id"], pid),
                ).fetchall()
                cell_ids = [r["cell_id"] for r in rows]
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower():
                raise
            cell_ids = []

        bounds = None
        if cell_ids:
            souths, wests, norths, easts = zip(*(cell_bounds(c) for c in cell_ids))
            bounds = {
                "south": min(souths),
                "west": min(wests),
                "north": max(norths),
                "east": max(easts),
            }

        checkin_points = 0.0
        last_checkin_net_date = None
        explorer_points = 0.0
        if season:
            ci = conn.execute(
                "SELECT SUM(points), MAX(net_date) FROM mc_checkin_award "
                " WHERE season_id = ? AND player_id = ?",
                (season["id"], pid),
            ).fetchone()
            checkin_points = ci[0] or 0.0
            last_checkin_net_date = ci[1]
            ep = conn.execute(
                "SELECT SUM(points) FROM place_activation "
                " WHERE player_id = ? AND awarded_at >= ? AND awarded_at <= ?",
                (pid, season["started_at"], season["ends_at"]),
            ).fetchone()
            explorer_points = ep[0] or 0.0

        last_fix = conn.execute(
            "SELECT ts FROM player_last_fix WHERE protocol = ? AND player_id = ?",
            (protocol, pid),
        ).fetchone()

        total_points = round(len(cell_ids) + checkin_points + explorer_points, 2)

        return {
            "display_name": player["display_name"],
            "team": player["team"],
            "tiles_held": len(cell_ids),
            "bounds": bounds,
            "checkin_points": round(checkin_points, 2),
            "explorer_points": round(explorer_points, 2),
            "total_points": total_points,
            "last_checkin_net_date": last_checkin_net_date,
            "last_position_ts": last_fix["ts"] if last_fix else None,
        }
    finally:
        conn.close()


@router.get("/api/mc/find")
async def mc_find(
    request: Request, name: str, session: SessionPrincipal = Depends(require_session)
):
    """Case-insensitive exact match on a player's display name, scoped
    to the active MeshCore season. See find_for(). Request path,
    request shape, and response shape are unchanged by the addition of
    find_for()'s `protocol` argument -- this route still always passes
    MC_PROTOCOL, same as before this module had a second caller.

    Privacy-hardening: this route exists solely to answer "where is
    this person" -- a display name in, a bounding box and a
    last-position timestamp out -- which is exactly the person-to-place
    link app/sessions.py's require_session() dependency now gates. There
    is deliberately no unauthenticated variant; a visitor who wants this
    has to sign in, the same as every other place-tied-to-identity
    lookup this pass gates. `session` itself is unused beyond proving a
    session exists -- this lookup isn't scoped to the caller's own
    account -- but the parameter has to be named to be a real FastAPI
    dependency. Also rate-limited per address (_find_rate_limited)
    now that it costs a real query rather than being free to hammer;
    see that helper's own comment for why a session alone isn't enough.
    """
    if _find_rate_limited(request):
        return JSONResponse({"error": "rate limited"}, status_code=429)
    result = find_for(MC_PROTOCOL, name)
    if result is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return result


def top_for(protocol: str) -> list[dict]:
    """Players ranked by capture-event count in `protocol`'s active
    season, from mc_tile_capture_log. Top 20, empty list if there's no
    data (or no active season).

    Factored out of mc_top() below so app/api.py's Meshtastic /top
    route returns the exact same shape instead of a hand-copied
    near-duplicate, following the same *_for() pattern as
    board_for/scores_for/history_for/cell_detail_for/find_for above.
    """

    def run(conn):
        season = active_season(conn, protocol)
        if not season:
            return []
        rows = conn.execute(
            "SELECT p.display_name AS display_name, p.team AS team, "
            "       COUNT(*) AS captures "
            "  FROM mc_tile_capture_log l "
            "  JOIN player p ON p.player_id = l.by_player_id "
            " WHERE l.season_id = ? "
            " GROUP BY l.by_player_id "
            " ORDER BY captures DESC "
            " LIMIT 20",
            (season["id"],),
        ).fetchall()
        return [
            {"display_name": r["display_name"], "team": r["team"], "captures": r["captures"]}
            for r in rows
        ]

    result = _safe_query(run)
    return result if result is not None else []


@router.get("/api/mc/top")
async def mc_top() -> list[dict]:
    """Players ranked by capture-event count in the active MeshCore
    season. See top_for(). Request path, request shape, and response
    shape are unchanged by the addition of top_for()'s `protocol`
    argument -- this route still always passes MC_PROTOCOL, same as
    before this module had a second caller.
    """
    return top_for(MC_PROTOCOL)


def top_checkin_for(protocol: str) -> list[dict]:
    """Players ranked by check-in points earned in `protocol`'s active
    season, from mc_checkin_award. Top 20, empty list if there's no
    data (or no active season) -- the "NetOps" counterpart of
    top_for() above (see app/checkin.py's module docstring for the
    Wardrivers/NetOps theming; both are ACTIVITIES on the same
    player model, so a player who does both shows up in both rankings).

    Follows the exact same *_for() pattern as
    board_for/scores_for/history_for/cell_detail_for/find_for/top_for,
    so app/api.py's Meshtastic route calls this directly instead of
    duplicating the query. Ranks by SUM(points), not award count, since
    an award's points value is copied from settings.checkin_points at
    the moment it was earned (see app/db.py's mc_checkin_award) rather
    than read live -- summing is also what mc_scoring.team_checkin_points()
    already does against this same table, so a player's rank here and
    their team's total there always agree on what "check-in points"
    means.
    """

    def run(conn):
        season = active_season(conn, protocol)
        if not season:
            return []
        rows = conn.execute(
            "SELECT p.display_name AS display_name, p.team AS team, "
            "       SUM(a.points) AS points, "
            # The streak on the player's MOST RECENT award, not a count
            # or a max: it is the run they are currently carrying, which
            # is the number that tells them what their next check-in is
            # worth. A player whose streak broke shows the shorter run
            # they have rebuilt since, not the longer one they lost.
            "       (SELECT a2.streak FROM mc_checkin_award a2 "
            "         WHERE a2.season_id = a.season_id AND a2.player_id = a.player_id "
            "         ORDER BY a2.net_date DESC LIMIT 1) AS streak "
            "  FROM mc_checkin_award a "
            "  JOIN player p ON p.player_id = a.player_id "
            " WHERE a.season_id = ? "
            " GROUP BY a.player_id "
            " ORDER BY points DESC "
            " LIMIT 20",
            (season["id"],),
        ).fetchall()
        return [
            {"display_name": r["display_name"], "team": r["team"], "points": r["points"],
             # Null for any award written before streaks existed; the
             # frontend shows a dash rather than inventing a 1.
             "streak": r["streak"]}
            for r in rows
        ]

    result = _safe_query(run)
    return result if result is not None else []


# "YYYY-MM" and nothing else. The month reaches award_geometry() from a
# URL path, and month_bounds() would raise on anything else.
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def results_for(protocol: str, limit: int = 12) -> dict:
    """Finished months for `protocol`, newest first, plus when the month
    in progress closes. See app/results.py for why the open month is not
    included.

    Same *_for() pattern as board_for/scores_for/top_for above, so
    app/api.py's Meshtastic route calls this rather than duplicating it.
    """

    def run(conn):
        return results.month_results_for(conn, protocol, int(time.time()), limit)

    out = _safe_query(run)
    return out if out is not None else {"protocol": protocol, "open_month": None,
                                        "open_month_closes_at": None, "months": []}


@router.get("/api/mc/results")
async def mc_results() -> dict:
    """Monthly results for the MeshCore board. See results_for()."""
    return results_for(MC_PROTOCOL)


def award_geometry_for(protocol: str, month: str, award: str) -> dict | None:
    """GeoJSON for where one honor was earned, or None when that honor
    has no place on the map, nobody won it, or the month is malformed.

    Same *_for() pattern as results_for above, so app/api.py's Meshtastic
    route calls this rather than duplicating it.
    """
    if not _MONTH_RE.match(month or ""):
        return None

    def run(conn):
        return results.award_geometry(conn, protocol, month, award, int(time.time()))

    return _safe_query(run)


@router.get("/api/mc/results/{month}/{award}/geo")
async def mc_award_geometry(month: str, award: str) -> JSONResponse:
    """Where a MeshCore honor was earned, as GeoJSON, for the map to
    draw. 404 rather than an empty collection when there is nothing to
    show, so the map can tell "no such thing" from "an empty road"."""
    geo = award_geometry_for(MC_PROTOCOL, month, award)
    if geo is None:
        return JSONResponse({"error": "no geometry for that award"}, status_code=404)
    return JSONResponse(geo)


@router.get("/api/mc/top-checkins")
async def mc_top_checkins() -> list[dict]:
    """Players ranked by check-in points in the active MeshCore season.
    See top_checkin_for(). New route -- every existing /api/mc/* route's
    request path, request shape, and response shape is unchanged by
    this addition.
    """
    return top_checkin_for(MC_PROTOCOL)


def top_explorer_for(protocol: str) -> list[dict]:
    """Players ranked by Explorer Score (Places Worth Going points,
    place_activation) earned in `protocol`'s active season. Top 20,
    empty list if there's no data (or no active season) -- the
    "Explorer" counterpart of top_for()/top_checkin_for() above, ranked
    over the same active-season window those two already use so all
    three Top Operators tabs describe the same period.

    place_activation has no season_id or protocol column of its own --
    a place credit is scoped by week_start, not by season or board (see
    mc_scoring.team_place_points()'s docstring). So this scopes it two
    ways, same as team_place_points() and public_api._player_rows()
    already do: by TIME (awarded_at against the season's own
    started_at/ends_at window) for which activations count, and by
    player_node.protocol (via a subquery, not a direct JOIN, so a
    player bound to more than one node on this protocol is not summed
    twice) for which players belong to THIS board's ranking -- a
    Meshtastic-only player does not show up in the MeshCore Explorer
    tab just because they also hold MeshCore radio bindings, and vice
    versa.

    Follows the exact same *_for() pattern as
    board_for/scores_for/history_for/find_for/top_for/top_checkin_for,
    so app/api.py's Meshtastic route calls this directly instead of
    duplicating the query.
    """

    def run(conn):
        season = active_season(conn, protocol)
        if not season:
            return []
        rows = conn.execute(
            "SELECT p.display_name AS display_name, p.team AS team, "
            "       SUM(a.points) AS points "
            "  FROM place_activation a "
            "  JOIN player p ON p.player_id = a.player_id "
            " WHERE a.awarded_at >= ? AND a.awarded_at <= ? "
            "   AND a.player_id IN (SELECT pn.player_id FROM player_node pn WHERE pn.protocol = ?) "
            " GROUP BY a.player_id "
            " ORDER BY points DESC "
            " LIMIT 20",
            (season["started_at"], season["ends_at"], protocol),
        ).fetchall()
        return [
            {"display_name": r["display_name"], "team": r["team"],
             "points": round(r["points"] or 0.0, 2)}
            for r in rows
        ]

    result = _safe_query(run)
    return result if result is not None else []


@router.get("/api/mc/top-explorer")
async def mc_top_explorer() -> list[dict]:
    """Players ranked by Explorer Score in the active MeshCore season.
    See top_explorer_for(). New route -- every existing /api/mc/* route's
    request path, request shape, and response shape is unchanged by
    this addition.
    """
    return top_explorer_for(MC_PROTOCOL)


# Cap on how many repeater_observation rows cell_detail_for() returns
# for one cell. A well-traveled square can accumulate a long tail of
# barely-heard repeaters; the popup only has room for a handful, and
# "most recently heard" is what a player actually wants to know ("what
# can I reach from here right now"), not a complete historical list.
_REPEATER_OBS_LIMIT = 10


def _repeater_observations(conn, protocol: str, cell_id: str) -> list[dict]:
    """Repeaters (MeshCore) or MQTT feeders (Meshtastic) observed as
    audible from `cell_id`, most-recently-heard first, capped at
    _REPEATER_OBS_LIMIT.

    Reads app/db.py's repeater_observation table (keyed by protocol,
    repeater_id, cell_id), left-joined against repeater_identity for
    node_type where known -- public_key is deliberately not exposed
    here, since this is an anonymous per-cell summary of what a square
    can hear, not an identity lookup. Both ingest paths
    (app/mc_ingest.py's record_repeater_observations(), called from
    both app/mc_ingest.py and app/ingest.py) write these rows for every
    ping that passes validation, independent of season and of whether
    the paint even scores -- so this is not scoped to the active season
    the way owner_team/scores/recent_captures above are; audibility
    evidence outlives a season rollover even though ownership doesn't.

    This is the ONLY source for this data -- it never falls back to
    estimating from distance. A cell with no rows here returns an empty
    list, which the frontend renders as no section at all rather than a
    guess (see mc.js's buildCellPopupHtml/PROTOCOLS' history for why
    that guess is exactly what these tables exist to replace).
    """
    rows = conn.execute(
        "SELECT o.repeater_id, o.first_seen, o.last_seen, o.direct_count, "
        "       o.heard_count, o.best_local_snr, o.best_remote_snr, "
        "       o.best_heard_snr, i.node_type "
        "  FROM repeater_observation o "
        "  LEFT JOIN repeater_identity i "
        "    ON i.protocol = o.protocol AND i.repeater_id = o.repeater_id "
        " WHERE o.protocol = ? AND o.cell_id = ? "
        " ORDER BY o.last_seen DESC LIMIT ?",
        (protocol, cell_id, _REPEATER_OBS_LIMIT),
    ).fetchall()
    return [
        {
            "repeater_id": r["repeater_id"],
            "node_type": r["node_type"],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
            "direct_count": r["direct_count"],
            "heard_count": r["heard_count"],
            "best_local_snr": r["best_local_snr"],
            "best_remote_snr": r["best_remote_snr"],
            "best_heard_snr": r["best_heard_snr"],
        }
        for r in rows
    ]


def _containing_park(conn: sqlite3.Connection, cell_id: str) -> dict | None:
    """The boundary-backed park (if any) that owns this cell under the
    >50%-of-cell rule (docs/features/places.md), for display in the
    cell popup -- see frontend/map2.js's setupCellClickPopup. Reuses
    app/places_seed.py's place_cell table rather than re-deriving
    containment from geometry: place_cell is already exactly this
    relationship, computed once at seed time from the unsimplified
    boundary (app/places_seed.py's _park_cells), so this can never
    disagree with what actually scored the square.

    ref_type = 'park' AND geom IS NOT NULL AND rotates = 0 is the same
    boundary-backed filter app/places_api.py's
    _park_boundaries_in_viewport uses for what draws as an outline --
    a cell mapped only via a summit/landmark's own point, or a small
    matched park that fell back to its point cell (see _park_cells'
    docstring), never surfaces here as "inside a park".

    Two designations for the same physical ground both matched to a
    park boundary (a state park and a coincident historic site, a
    refuge and a coincident WMA -- both real in the PAD-US source data,
    not a hypothetical) can both legitimately clear 50% of one cell at
    once, since the >50% test is run independently per place against
    the cell, not against each other's polygon. Points DESC, then the
    same deterministic hash tiebreak app/places_api.py's viewport
    queries use for an identical "need one stable order out of an
    equally-ranked set" problem, picks one consistently rather than
    letting sqlite's unspecified row order decide.
    """
    row = conn.execute(
        "SELECT p.id, p.name, p.points FROM place_cell pc "
        "  JOIN place p ON p.id = pc.place_id "
        " WHERE pc.cell_id = ? AND p.ref_type = 'park' AND p.geom IS NOT NULL "
        "   AND p.rotates = 0 AND p.active = 1 "
        f" ORDER BY p.points DESC, {_stable_tiebreak('p.id')} LIMIT 1",
        (cell_id,),
    ).fetchone()
    if not row:
        return None
    return {"id": row["id"], "name": row["name"], "points": row["points"]}


def cell_detail_for(protocol: str, cell_id: str) -> dict | None:
    """Detail for one cell in `protocol`'s active season: owner, capture
    time, per-team current scores, and a few recent capture-log entries
    with display names. None when the cell has no owner -- including
    when there is no active season, or the mc_* schema doesn't exist
    yet, since in both of those cases the cell has no owner either.

    Factored out of mc_cell() below so app/api.py's Meshtastic
    /cell/{cell_id} route returns the exact same per-cell shape instead
    of a hand-copied near-duplicate.
    """

    def run(conn):
        season = active_season(conn, protocol)
        if not season:
            return None

        tile = conn.execute(
            "SELECT owner_team, last_report_ts, paint_count "
            "  FROM mc_tile WHERE season_id = ? AND cell_id = ?",
            (season["id"], cell_id),
        ).fetchone()
        if not tile or not tile["owner_team"]:
            return None

        cap = conn.execute(
            "SELECT captured_at, captured_by_team FROM mc_tile_capture "
            " WHERE season_id = ? AND cell_id = ?",
            (season["id"], cell_id),
        ).fetchone()

        score_rows = conn.execute(
            "SELECT team, score FROM mc_tile_score "
            " WHERE season_id = ? AND cell_id = ?",
            (season["id"], cell_id),
        ).fetchall()
        scores = {r["team"]: r["score"] for r in score_rows}

        log_rows = conn.execute(
            "SELECT l.ts, l.by_team, l.from_team, p.display_name "
            "  FROM mc_tile_capture_log l "
            "  LEFT JOIN player p "
            "    ON p.player_id = l.by_player_id "
            " WHERE l.season_id = ? AND l.cell_id = ? "
            " ORDER BY l.ts DESC LIMIT 5",
            (season["id"], cell_id),
        ).fetchall()

        south, west, north, east = cell_bounds(cell_id)
        return {
            "cell_id": cell_id,
            "owner_team": tile["owner_team"],
            "last_report_ts": tile["last_report_ts"],
            "paint_count": tile["paint_count"],
            "captured_at": cap["captured_at"] if cap else None,
            "captured_by_team": cap["captured_by_team"] if cap else None,
            "scores": {t: scores.get(t, 0) for t in team_list()},
            "south": south,
            "west": west,
            "north": north,
            "east": east,
            "recent_captures": [
                {
                    "ts": r["ts"],
                    "by_team": r["by_team"],
                    "from_team": r["from_team"],
                    "by_display_name": r["display_name"],
                }
                for r in log_rows
            ],
            # Additive -- see _repeater_observations()'s docstring. Only
            # ever the real rows ingest wrote for this cell, never a
            # distance-based guess.
            "repeaters": _repeater_observations(conn, protocol, cell_id),
            # Additive -- see _containing_park()'s docstring. None when
            # this cell isn't majority-inside any boundary-backed park.
            "park": _containing_park(conn, cell_id),
        }

    return _safe_query(run)


# Privacy-hardening: the ONLY per-person field cell_detail_for() puts on
# the wire is recent_captures[].by_display_name -- everything else in
# its shape (owner_team, scores, timestamps, repeaters, park) is team-
# or infrastructure-level, which is exactly what the rule this pass
# implements ("a square's history stays public at team level; WHO
# captured it requires a session") says stays public regardless of who
# is asking. cell_detail_for() itself is left returning the full shape
# unchanged -- tests/test_mc_api_cell_park.py and friends call it
# directly and expect that -- so the redaction happens here, once, at
# the boundary where a response actually leaves the server, and is
# shared by both cell routes (this one and app/api.py's /cell/{cell_id})
# rather than duplicated.
def _redact_cell_detail(detail: dict, *, authenticated: bool) -> dict:
    if authenticated:
        return detail
    recent = detail.get("recent_captures")
    if not recent:
        return detail
    return {
        **detail,
        "recent_captures": [
            {k: v for k, v in cap.items() if k != "by_display_name"} for cap in recent
        ],
    }


@router.get("/api/mc/cell/{cell_id}")
async def mc_cell(cell_id: str, session: SessionPrincipal | None = Depends(optional_session)):
    """Detail for one cell on the active MeshCore board. See
    cell_detail_for().

    Public at team level -- owner, scores, capture timestamps, repeater/
    park evidence -- same as before this pass. WHO captured it
    (recent_captures[].by_display_name) is stripped unless `session`
    resolves to a real signed-in account (see optional_session() in
    app/sessions.py and _redact_cell_detail() above) -- that is the
    "who captured a square requires a session" half of the privacy
    rule this endpoint implements.
    """
    detail = cell_detail_for(MC_PROTOCOL, cell_id)
    if detail is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return _redact_cell_detail(detail, authenticated=session is not None)


_STAT_COLUMNS = (
    "batches, pings_accepted, pings_no_contact, pings_wrong_owner, "
    "pings_duplicate, pings_bad_coord, pings_out_of_area, pings_no_repeaters, "
    "pings_low_precision, pings_implausible_speed"
)
_STAT_ZERO_ROW = {
    "batches": 0, "pings_accepted": 0, "pings_no_contact": 0,
    "pings_wrong_owner": 0, "pings_duplicate": 0, "pings_bad_coord": 0,
    "pings_out_of_area": 0, "pings_no_repeaters": 0,
}


def _counters_out(row) -> dict:
    return {
        "batches": row["batches"],
        "accepted": row["pings_accepted"],
        "no_contact": row["pings_no_contact"],
        "wrong_owner": row["pings_wrong_owner"],
        "duplicate": row["pings_duplicate"],
        "bad_coord": row["pings_bad_coord"],
        "out_of_area": row["pings_out_of_area"],
        "no_repeaters": row["pings_no_repeaters"],
    }


# Meshtastic counterpart of _STAT_ZERO_ROW/_counters_out above, missing
# batches/no_contact/wrong_owner on purpose: those three are structurally
# always 0 for protocol='mt' (see app/ingest.py's _STAT_COLUMNS
# docstring -- no batch-submission concept, no way to attribute an
# unregistered packet to a player, and player_node's (protocol, node_ref)
# primary key already makes ownership 1:1 at registration time). Reading
# player_ingest_stat and reporting them anyway would show a Meshtastic
# player a column of counters that can never be anything but zero -- the
# exact thing this endpoint update exists to stop doing -- so they are
# left out of the shape entirely rather than zero-filled.
_STAT_ZERO_ROW_MT = {
    "pings_accepted": 0, "pings_duplicate": 0, "pings_bad_coord": 0,
    "pings_out_of_area": 0, "pings_no_repeaters": 0,
    "pings_low_precision": 0, "pings_implausible_speed": 0,
}


def _counters_out_mt(row) -> dict:
    return {
        "accepted": row["pings_accepted"],
        "duplicate": row["pings_duplicate"],
        "bad_coord": row["pings_bad_coord"],
        "out_of_area": row["pings_out_of_area"],
        "no_repeaters": row["pings_no_repeaters"],
        "low_precision": row["pings_low_precision"],
        "implausible_speed": row["pings_implausible_speed"],
    }


@router.post("/api/mc/status")
async def mc_status(
    request: Request, principal: Principal = Depends(require_status_principal)
) -> JSONResponse:
    """Lets a player check whether their wardriving app is actually
    reaching us. Every failure mode is already recorded in
    player_ingest_stat and api_key.last_seen_at -- this reads it back
    and works out server-side which one (if any) explains what the
    player is seeing, so the page only has to display it.

    The key arrives in the X-API-Key header on a POST, the same as
    /api/mc/ingest, and for the same reason: a GET would put the key in
    the URL, where it can land in a server access log, browser history,
    or a Referer header. Checking status is deliberately not usage --
    this never touches api_key.last_seen_at. Authenticated through
    app/auth.py's require_api_key_principal() (require_status_principal
    above), configured to match this endpoint's own original shape --
    a pre-auth address limiter with its own rate-limited message
    ("too many attempts, try again later") and no post-auth per-key
    limiter, unlike the other four key-authenticated endpoints -- see
    that module's docstring for why this one isn't force-fit to match.

    Despite the /api/mc/ URL, this now reports BOTH protocols a player
    might have radios for -- the top-level fields below (last_batch_at,
    today, last_7_days, squares_held, diagnosis) are unchanged and stay
    MeshCore-scoped, exactly as every existing caller already expects;
    everything Meshtastic-specific is additive, under the new "mt" key.
    They cannot share a shape: MeshCore is pushed to us in batches (see
    api_key.last_seen_at, player_ingest_stat.batches), Meshtastic is
    pulled from meshview one packet at a time with no batch concept at
    all (see app/ingest.py's module docstring) -- and reusing this
    module's own MC_PROTOCOL-scoped helpers (active_season, _diagnose,
    _counters_out, and friends) for a second protocol would silently mix
    the two boards together, the exact bug active_season()'s docstring
    warns an unfiltered query would cause. So "mt" is built from its own
    parallel queries and its own _diagnose_mt()/_counters_out_mt() below,
    filtered to MT_PROTOCOL throughout, never MC_PROTOCOL's helpers
    reused past what they were already filtering for.
    """
    player_id = principal.player_id
    now_ts = int(time.time())

    conn = connect()
    try:
        player = conn.execute(
            "SELECT display_name, team FROM player WHERE player_id = ?",
            (player_id,),
        ).fetchone()

        radio_rows = conn.execute(
            "SELECT protocol, node_ref FROM player_node WHERE player_id = ? ORDER BY bound_at",
            (player_id,),
        ).fetchall()

        last_batch_at = conn.execute(
            "SELECT MAX(last_seen_at) AS ts FROM api_key WHERE player_id = ?",
            (player_id,),
        ).fetchone()["ts"]

        now_dt = datetime.now(timezone.utc)
        today = int(now_dt.strftime("%Y%m%d"))
        week_start = int((now_dt.date() - timedelta(days=6)).strftime("%Y%m%d"))

        today_row = conn.execute(
            f"SELECT {_STAT_COLUMNS} FROM player_ingest_stat "
            " WHERE player_id = ? AND protocol = ? AND day = ?",
            (player_id, MC_PROTOCOL, today),
        ).fetchone()

        week_row = conn.execute(
            "SELECT COALESCE(SUM(batches),0) AS batches, "
            "       COALESCE(SUM(pings_accepted),0) AS pings_accepted, "
            "       COALESCE(SUM(pings_no_contact),0) AS pings_no_contact, "
            "       COALESCE(SUM(pings_wrong_owner),0) AS pings_wrong_owner, "
            "       COALESCE(SUM(pings_duplicate),0) AS pings_duplicate, "
            "       COALESCE(SUM(pings_bad_coord),0) AS pings_bad_coord, "
            "       COALESCE(SUM(pings_out_of_area),0) AS pings_out_of_area, "
            "       COALESCE(SUM(pings_no_repeaters),0) AS pings_no_repeaters "
            "  FROM player_ingest_stat WHERE player_id = ? AND protocol = ? "
            "    AND day BETWEEN ? AND ?",
            (player_id, MC_PROTOCOL, week_start, today),
        ).fetchone()

        alltime_row = conn.execute(
            "SELECT COALESCE(SUM(batches),0) AS batches, "
            "       COALESCE(SUM(pings_accepted),0) AS pings_accepted, "
            "       COALESCE(SUM(pings_no_contact),0) AS pings_no_contact, "
            "       COALESCE(SUM(pings_wrong_owner),0) AS pings_wrong_owner, "
            "       COALESCE(SUM(pings_duplicate),0) AS pings_duplicate, "
            "       COALESCE(SUM(pings_bad_coord),0) AS pings_bad_coord, "
            "       COALESCE(SUM(pings_out_of_area),0) AS pings_out_of_area, "
            "       COALESCE(SUM(pings_no_repeaters),0) AS pings_no_repeaters "
            "  FROM player_ingest_stat WHERE player_id = ? AND protocol = ?",
            (player_id, MC_PROTOCOL),
        ).fetchone()

        # squares held: same landing-order guard as mc_find() above -- the
        # mc_tile schema is not part of the always-present core tables.
        squares_held = 0
        try:
            season = active_season(conn, MC_PROTOCOL)
            if season:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM mc_tile WHERE season_id = ? AND last_player_id = ?",
                    (season["id"], player_id),
                ).fetchone()
                squares_held = row["n"]
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower():
                raise
            squares_held = 0

        # ---- Meshtastic side, additive (see mc_status()'s docstring). ----
        #
        # last_heard_at_mt: MeshCore's last_batch_at reads api_key --
        # there's no equivalent MeshCore concept here, because nothing is
        # ever pushed to us on this side. The pull equivalent is the most
        # recent moment MeshWars picked this player's node up from
        # meshview -- that's player_cell_ping.seen_at (app/ingest.py
        # writes it as int(time.time()) at the moment it processes a
        # packet, not the packet's own reported timestamp), not
        # player_last_fix (which exists only for the implausible-speed
        # check and is keyed on the packet's own ts, a different fact).
        last_heard_at_mt = conn.execute(
            "SELECT MAX(seen_at) AS ts FROM player_cell_ping "
            " WHERE player_id = ? AND protocol = ?",
            (player_id, MT_PROTOCOL),
        ).fetchone()["ts"]

        today_row_mt = conn.execute(
            f"SELECT {_STAT_COLUMNS} FROM player_ingest_stat "
            " WHERE player_id = ? AND protocol = ? AND day = ?",
            (player_id, MT_PROTOCOL, today),
        ).fetchone()

        week_row_mt = conn.execute(
            "SELECT COALESCE(SUM(batches),0) AS batches, "
            "       COALESCE(SUM(pings_accepted),0) AS pings_accepted, "
            "       COALESCE(SUM(pings_no_contact),0) AS pings_no_contact, "
            "       COALESCE(SUM(pings_wrong_owner),0) AS pings_wrong_owner, "
            "       COALESCE(SUM(pings_duplicate),0) AS pings_duplicate, "
            "       COALESCE(SUM(pings_bad_coord),0) AS pings_bad_coord, "
            "       COALESCE(SUM(pings_out_of_area),0) AS pings_out_of_area, "
            "       COALESCE(SUM(pings_no_repeaters),0) AS pings_no_repeaters, "
            "       COALESCE(SUM(pings_low_precision),0) AS pings_low_precision, "
            "       COALESCE(SUM(pings_implausible_speed),0) AS pings_implausible_speed "
            "  FROM player_ingest_stat WHERE player_id = ? AND protocol = ? "
            "    AND day BETWEEN ? AND ?",
            (player_id, MT_PROTOCOL, week_start, today),
        ).fetchone()

        alltime_row_mt = conn.execute(
            "SELECT COALESCE(SUM(batches),0) AS batches, "
            "       COALESCE(SUM(pings_accepted),0) AS pings_accepted, "
            "       COALESCE(SUM(pings_no_contact),0) AS pings_no_contact, "
            "       COALESCE(SUM(pings_wrong_owner),0) AS pings_wrong_owner, "
            "       COALESCE(SUM(pings_duplicate),0) AS pings_duplicate, "
            "       COALESCE(SUM(pings_bad_coord),0) AS pings_bad_coord, "
            "       COALESCE(SUM(pings_out_of_area),0) AS pings_out_of_area, "
            "       COALESCE(SUM(pings_no_repeaters),0) AS pings_no_repeaters, "
            "       COALESCE(SUM(pings_low_precision),0) AS pings_low_precision, "
            "       COALESCE(SUM(pings_implausible_speed),0) AS pings_implausible_speed "
            "  FROM player_ingest_stat WHERE player_id = ? AND protocol = ?",
            (player_id, MT_PROTOCOL),
        ).fetchone()

        # squares held: mirrors the MeshCore block above exactly, just
        # filtered to MT_PROTOCOL -- "held" has to mean the same thing on
        # both boards (same mc_tile.last_player_id ownership check), or
        # the two counts on this one page would quietly disagree about
        # what "holding a square" even means.
        squares_held_mt = 0
        try:
            season_mt = active_season(conn, MT_PROTOCOL)
            if season_mt:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM mc_tile WHERE season_id = ? AND last_player_id = ?",
                    (season_mt["id"], player_id),
                ).fetchone()
                squares_held_mt = row["n"]
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower():
                raise
            squares_held_mt = 0
    finally:
        conn.close()

    today_out = _counters_out(today_row) if today_row else _counters_out(_STAT_ZERO_ROW)

    alltime = {
        "batches": alltime_row["batches"],
        "pings_accepted": alltime_row["pings_accepted"],
        "pings_no_contact": alltime_row["pings_no_contact"],
        "pings_wrong_owner": alltime_row["pings_wrong_owner"],
        "pings_duplicate": alltime_row["pings_duplicate"],
        "pings_bad_coord": alltime_row["pings_bad_coord"],
        "pings_out_of_area": alltime_row["pings_out_of_area"],
        "pings_no_repeaters": alltime_row["pings_no_repeaters"],
    }
    code, message = _diagnose(alltime, squares_held, last_batch_at, now_ts)

    today_out_mt = _counters_out_mt(today_row_mt) if today_row_mt else _counters_out_mt(_STAT_ZERO_ROW_MT)

    alltime_mt = {
        "pings_accepted": alltime_row_mt["pings_accepted"],
        "pings_duplicate": alltime_row_mt["pings_duplicate"],
        "pings_bad_coord": alltime_row_mt["pings_bad_coord"],
        "pings_out_of_area": alltime_row_mt["pings_out_of_area"],
        "pings_no_repeaters": alltime_row_mt["pings_no_repeaters"],
        "pings_low_precision": alltime_row_mt["pings_low_precision"],
        "pings_implausible_speed": alltime_row_mt["pings_implausible_speed"],
    }
    code_mt, message_mt = _diagnose_mt(alltime_mt, squares_held_mt, last_heard_at_mt, now_ts)

    return {
        "display_name": player["display_name"],
        "team": player["team"],
        "radios": [
            {"protocol": r["protocol"], "node_ref": r["node_ref"]} for r in radio_rows
        ],
        "last_batch_at": last_batch_at,
        "today": today_out,
        "last_7_days": _counters_out(week_row),
        "squares_held": squares_held,
        "diagnosis": {"code": code, "message": message},
        # Additive -- see mc_status()'s docstring. Existing callers that
        # only ever read the MeshCore-scoped fields above are unaffected
        # by a new top-level key showing up alongside them.
        "mt": {
            "last_heard_at": last_heard_at_mt,
            "today": today_out_mt,
            "last_7_days": _counters_out_mt(week_row_mt),
            "squares_held": squares_held_mt,
            "diagnosis": {"code": code_mt, "message": message_mt},
        },
    }
