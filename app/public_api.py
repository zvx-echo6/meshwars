"""The public read API: /api/v1.

Everything MeshWars shows on its own pages was already reachable without
a key -- two dozen routes across /api/mc/* and the bare Meshtastic
namespace. What was missing was a CONTRACT. Those routes are shaped for
the pages that call them, split across two naming schemes, and free to
change whenever a page does; /api/mc/results changed from a list to an
object the same week this was written. Nothing anyone else builds should
be resting on that.

So this is a separate, deliberately stable surface, and the rules for it
are:

- The board is a QUERY PARAMETER, not a second namespace. A caller
  writes one integration and points it at either game.
- Response shapes are additive only. Fields may appear; a field that
  exists keeps its name and meaning. A breaking change means /api/v2,
  not an edit here.
- Timestamps are unix seconds, always, named `*_at` or `*_ts`. Dates
  that are calendar dates (net dates, months) are strings, because that
  is what they are -- a net date is a Wednesday in Boise, not an
  instant.
- Every list route documents its own limit and never returns more.
- Read-only. Nothing here writes.
- A key is required, in an `X-API-Key` header. Mostly this is not
  because the data is secret but because an anonymous surface cannot be
  reasoned about: with keys, a misbehaving integration can be
  identified and revoked on its own rather than by blocking an address
  that might be a whole mesh community behind one NAT. Keys are issued
  from the admin panel and only their hash is stored, so a lost key is
  replaced, never recovered.

  The one deliberate exception: /api/v1/cells/{cell_id} and
  /api/v1/captures return a captured square's player display name
  (`recent_captures[]`/`captures[].player`), which the site itself
  withholds from an anonymous visitor -- GET /cell/{cell_id} and
  GET /api/mc/cell/{cell_id} strip `by_display_name` unless
  app/sessions.py's optional_session() resolves a real signed-in
  account (see mc_api._redact_cell_detail()). That is Matt's privacy
  rule, "identity can be public, location can be public, the link
  between them requires a session" -- and an integration key is a
  session in the sense that matters here: it is issued personally by
  the operator, so it is accountable rather than anonymous, and
  revocable per key, the same way a signed-in account is tied to a
  person rather than an address. See commit 007db35 ("stop the public
  API linking a person to a place"), which hardened every other person-
  to-place route on the site and explicitly left this one alone for
  that reason. So these two routes are NOT a parity gap to be closed;
  do not redact them into matching the anonymous site shape without
  checking with Matt first.

It reads through the same *_for() helpers the site's own routes use
rather than issuing its own copies of those queries, so a figure a bot
reports and a figure on the page can never disagree.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response

from . import mc_api, results
from .auth import new_rate_limit_bucket
from .checkin import load_checkin_config
from .client_ip import get_client_ip
from .config import settings
from .db import connect
from .mc_ingest import PROTOCOL as MC_PROTOCOL, hash_secret
from .mc_scoring import team_checkin_points, team_tile_counts
from .mesh_render import render_mesh

log = logging.getLogger("public_api")

router = APIRouter()

MT_PROTOCOL = "mt"
API_VERSION = "1"

# Callers name the board the way a person would; the two-letter codes
# the database uses are accepted too, since anyone reading the site's
# own routes will already have seen them.
_BOARDS = {
    "meshcore": MC_PROTOCOL, "mc": MC_PROTOCOL,
    "meshtastic": MT_PROTOCOL, "mt": MT_PROTOCOL,
}
_BOARD_NAMES = {MC_PROTOCOL: "meshcore", MT_PROTOCOL: "meshtastic"}


def _protocol(board: str) -> str | None:
    return _BOARDS.get((board or "").strip().lower())


# ---- authentication ----------------------------------------------------

_KEY_HEADER = "X-API-Key"

# key_hash -> (checked_at_monotonic, label or None if it is not valid).
# Every read would otherwise be two queries: one to authenticate and one
# to answer. Bounded because the header is attacker-controlled and a
# flood of invented keys would otherwise grow this without limit.
_key_cache: dict[str, tuple[float, str | None]] = {}
_KEY_CACHE_MAX = 10000
_KEY_CACHE_SECONDS = 60


def _authenticate(raw_key: str) -> str | None:
    """The client's label if this key is valid and unrevoked, else None.

    Cached for a minute, which is also how long a revocation takes to
    bite. That is the deliberate trade: an operator revoking a key wants
    it gone, but a minute of grace costs nothing on a read-only surface
    and saves a query on every single request.
    """
    key_hash = hash_secret(raw_key)
    now = time.monotonic()

    hit = _key_cache.get(key_hash)
    if hit is not None and now - hit[0] < _KEY_CACHE_SECONDS:
        return hit[1]

    if len(_key_cache) >= _KEY_CACHE_MAX:
        for k, v in list(_key_cache.items()):
            if now - v[0] >= _KEY_CACHE_SECONDS:
                del _key_cache[k]
        if len(_key_cache) >= _KEY_CACHE_MAX:
            _key_cache.clear()

    conn = connect()
    try:
        row = conn.execute(
            "SELECT label FROM api_client WHERE key_hash = ? AND revoked_at IS NULL",
            (key_hash,),
        ).fetchone()
        label = row["label"] if row else None
        if label is not None:
            # Usage bookkeeping, so an operator can tell a live
            # integration from an abandoned one before revoking it.
            conn.execute(
                "UPDATE api_client SET last_seen_at = ?, request_count = request_count + 1 "
                " WHERE key_hash = ?", (int(time.time()), key_hash))
            conn.commit()
    except sqlite3.OperationalError:
        # Schema not landed yet. Refuse rather than fall open.
        label = None
    finally:
        conn.close()

    _key_cache[key_hash] = (now, label)
    return label


# ---- rate limiting -----------------------------------------------------

_hits: dict[str, list[float]] = {}
_MAX_TRACKED = 10000


def _client_ip(request: Request) -> str:
    # See app/client_ip.py's module docstring: this used to be
    # request.client.host directly, which is always the Caddy reverse
    # proxy's own address in every deployment, not the real caller's.
    return get_client_ip(request)


def _rate_limited(bucket: str) -> bool:
    """True if `bucket` -- a key hash, or an address for the one route
    that needs none -- is over budget for the window, recording this
    call when it is not.

    Same shape as the limiter on /api/mc/status. It exists here because
    this surface invites automation by design -- a bot polling every few
    seconds is the expected caller, not an abusive one -- and a budget
    generous enough for that is still a bound.
    """
    now = time.monotonic()
    window = settings.public_api_rate_limit_window_seconds
    limit = settings.public_api_rate_limit_requests

    if len(_hits) >= _MAX_TRACKED:
        for k in [k for k, t in _hits.items() if not t or now - t[-1] >= window]:
            del _hits[k]
        if len(_hits) >= _MAX_TRACKED:
            _hits.clear()

    times = [t for t in _hits.get(bucket, []) if now - t < window]
    if len(times) >= limit:
        _hits[bucket] = times
        return True
    times.append(now)
    _hits[bucket] = times
    return False


def _guard(request: Request, board: str | None = None, require_key: bool = True):
    """Authenticate, rate limit, and resolve the board if one was asked
    for. Returns (protocol, error_response) -- exactly one of which is
    None.

    The budget is spent per KEY rather than per address, which is the
    main practical reason keys exist here: a mesh community behind one
    NAT is many integrations at one address, and rate limiting the
    address would have them starve each other.
    """
    if require_key:
        raw = request.headers.get(_KEY_HEADER, "")
        if not raw:
            return None, JSONResponse(
                {"error": "unauthorized",
                 "detail": "send your key in an %s header -- see https://meshwars.com/api"
                           % _KEY_HEADER},
                status_code=401,
            )
        if _authenticate(raw) is None:
            return None, JSONResponse(
                {"error": "unauthorized", "detail": "unknown or revoked key"},
                status_code=401,
            )
        bucket = hash_secret(raw)
    else:
        bucket = _client_ip(request)

    if _rate_limited(bucket):
        return None, JSONResponse(
            {"error": "rate limited",
             "detail": "%d requests per %d seconds"
                       % (settings.public_api_rate_limit_requests,
                          settings.public_api_rate_limit_window_seconds)},
            status_code=429,
        )
    if board is None:
        return None, None
    proto = _protocol(board)
    if proto is None:
        return None, JSONResponse(
            {"error": "unknown board",
             "detail": "board must be one of: meshcore, meshtastic"},
            status_code=400,
        )
    return proto, None


# ---- the net -----------------------------------------------------------


def _net_window(now_ts: int) -> dict:
    """When the next net opens and whether one is open right now, across
    every ENABLED row in checkin_net -- not a single settings-based
    window, which is all there was back when a site could only ever run
    one net. See app/db.py's checkin_net table and app/checkin.py's
    net_date_for_net, which decides the same open/closed question for
    an incoming message, one net at a time.

    Display only, same as before this read multiple nets: this never
    decides who gets an award. The award gate lives in app/checkin.py,
    reading checkin_net independently, and stays there -- a caller of
    /api/v1/net must never be able to reason "the API says it's open,
    so my check-in counted."

    The response keeps the single-net field names (weekday,
    opens_hour_local, ...) for backward compatibility with the
    published /api/v1 contract, which promises those names keep their
    meaning. With more than one enabled net they describe whichever
    net is open right now, or -- if none is -- whichever opens
    soonest: one net has to speak for those fields, so it is always
    the one a caller most wants to know about.
    """
    conn = connect()
    try:
        nets = [dict(r) for r in conn.execute(
            "SELECT * FROM checkin_net WHERE enabled = 1 ORDER BY id").fetchall()]
        config = load_checkin_config(conn)
    finally:
        conn.close()

    # points/streak figures now live in checkin_config (admin-editable,
    # read fresh every time -- see load_checkin_config), not settings,
    # which only seeded that table's first row and is never consulted
    # again once it has.
    base = {
        "base_points": config["points"],
        "streak_bonus_per_net": config["streak_bonus"],
        "streak_bonus_max": config["streak_bonus_max"],
    }

    if not nets:
        return dict(base, open=False, weekday=None, opens_hour_local=None,
                    closes_hour_local=None, timezone=None,
                    current_net_date=None, next_opens_at=None)

    open_net = None    # first (lowest id) enabled net that is open right now, if any
    soonest = None      # (next_start_ts, net) for whichever net opens soonest
    for n in nets:
        tz = ZoneInfo(n["timezone"])
        local = datetime.fromtimestamp(now_ts, tz=tz)

        is_open = (local.weekday() == n["weekday"]
                   and n["start_hour"] <= local.hour <= n["end_hour"])
        if is_open and open_net is None:
            open_net = n

        # The next start for THIS net, which is today's if it has not
        # happened yet -- same arithmetic the single-net version used,
        # just run once per net instead of once for the whole site.
        days_ahead = (n["weekday"] - local.weekday()) % 7
        start = local.replace(hour=n["start_hour"], minute=0, second=0, microsecond=0) \
            + timedelta(days=days_ahead)
        if start <= local:
            start += timedelta(days=7)
        start_ts = int(start.timestamp())
        if soonest is None or start_ts < soonest[0]:
            soonest = (start_ts, n)

    chosen = open_net or soonest[1]
    today_local = datetime.fromtimestamp(now_ts, tz=ZoneInfo(chosen["timezone"])).date().isoformat()

    return dict(base,
        open=open_net is not None,
        weekday=chosen["weekday"],
        opens_hour_local=chosen["start_hour"],
        closes_hour_local=chosen["end_hour"],
        timezone=chosen["timezone"],
        current_net_date=today_local if open_net is not None else None,
        next_opens_at=soonest[0],
    )


# ---- shaping -----------------------------------------------------------


def _season_shape(row) -> dict | None:
    if not row:
        return None
    return {
        "id": row["id"],
        "started_at": row["started_at"],
        "ends_at": row["ends_at"],
        "status": row["status"],
        "winner": row["winner"],
        "seconds_remaining": max(0, row["ends_at"] - int(time.time())),
    }


def _standings(conn, season_id: int) -> list[dict]:
    tiles = team_tile_counts(conn, season_id)
    points = team_checkin_points(conn, season_id)
    rows = [
        {"team": t,
         "squares": tiles.get(t, 0),
         "checkin_points": round(points.get(t, 0.0), 2),
         "total": round(tiles.get(t, 0) + points.get(t, 0.0), 2)}
        for t in set(mc_api.team_list()) | set(tiles) | set(points)
    ]
    rows.sort(key=lambda r: (-r["total"], r["team"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def _board_summary(conn, protocol: str) -> dict:
    season = mc_api.active_season(conn, protocol)
    out = {
        "board": _BOARD_NAMES[protocol],
        "season": _season_shape(season),
        "standings": [],
        "players": 0,
        "squares_owned": 0,
    }
    if not season:
        return out
    out["standings"] = _standings(conn, season["id"])
    out["squares_owned"] = sum(r["squares"] for r in out["standings"])
    out["players"] = conn.execute(
        "SELECT count(DISTINCT pn.player_id) FROM player_node pn "
        "  JOIN player p ON p.player_id = pn.player_id "
        " WHERE pn.protocol = ? AND p.disabled_at IS NULL", (protocol,)
    ).fetchone()[0]
    return out


# ---- routes ------------------------------------------------------------


@router.get("/api/v1")
async def v1_index(request: Request) -> JSONResponse:
    """What this API is and what is in it, and the one route that needs
    no key -- so somebody who has just been handed one, or is deciding
    whether to ask for one, can see what they are getting. Rate limited
    by address instead."""
    _, err = _guard(request, require_key=False)
    if err:
        return err
    return JSONResponse({
        "name": "MeshWars public API",
        "version": API_VERSION,
        "docs": "https://meshwars.com/api",
        "boards": list(_BOARD_NAMES.values()),
        "authentication": {
            "header": _KEY_HEADER,
            "required": True,
            "how_to_get_one": "ask the operator; see https://meshwars.com/api",
        },
        "rate_limit": {
            "requests": settings.public_api_rate_limit_requests,
            "window_seconds": settings.public_api_rate_limit_window_seconds,
        },
        "endpoints": {
            "/api/v1/status": "both boards at once -- season, standings, the net",
            "/api/v1/seasons": "the running season and every closed one",
            "/api/v1/teams": "standings for one board",
            "/api/v1/players": "the roster with each player's figures",
            "/api/v1/players/{name}": "one player in detail",
            "/api/v1/top": "rankings; kind=captures or checkins",
            "/api/v1/board": "every owned square",
            "/api/v1/cells/{cell_id}": "one square: owner, scores, history",
            "/api/v1/captures": "recent captures, newest first",
            "/api/v1/results": "monthly standings and honors",
            "/api/v1/net": "the weekly net, and who has checked in",
            "/api/v1/nets": "the enabled check-in nets by id/label/board -- no key required",
            "/api/v1/announcements": "the public announcement feed -- no key required; poll with ?since=",
        },
    })


@router.get("/api/v1/status")
async def v1_status(request: Request) -> JSONResponse:
    """Both boards in one call.

    Exists because a bot answering "how's it going" should not need four
    requests to do it. Everything here is also available separately at
    more detail.
    """
    _, err = _guard(request)
    if err:
        return err
    now = int(time.time())
    conn = connect()
    try:
        return JSONResponse({
            "generated_at": now,
            "boards": [_board_summary(conn, p) for p in (MC_PROTOCOL, MT_PROTOCOL)],
            "net": _net_window(now),
        })
    finally:
        conn.close()


@router.get("/api/v1/seasons")
async def v1_seasons(request: Request, board: str = "meshcore") -> JSONResponse:
    """The running season plus every closed one, newest first, each with
    its final per-team tally."""
    proto, err = _guard(request, board)
    if err:
        return err
    conn = connect()
    try:
        current = _season_shape(mc_api.active_season(conn, proto))
        return JSONResponse({
            "board": _BOARD_NAMES[proto],
            "current": current,
            "past": mc_api.history_for(proto),
        })
    finally:
        conn.close()


@router.get("/api/v1/teams")
async def v1_teams(request: Request, board: str = "meshcore") -> JSONResponse:
    """Standings for one board: squares held, check-in points, and the
    total that decides the season."""
    proto, err = _guard(request, board)
    if err:
        return err
    conn = connect()
    try:
        season = mc_api.active_season(conn, proto)
        if not season:
            return JSONResponse({"board": _BOARD_NAMES[proto], "season": None, "teams": []})
        return JSONResponse({
            "board": _BOARD_NAMES[proto],
            "season": _season_shape(season),
            "teams": _standings(conn, season["id"]),
        })
    finally:
        conn.close()


def _player_rows(conn, protocol: str, season_id: int, name: str | None = None) -> list[dict]:
    """Every registered player on this board with their season figures,
    or one of them by name.

    Disabled players are left out, the same filter every attribution
    path applies at read time -- a disabled account keeps its rows but
    stops being a participant.
    """
    where = "AND lower(p.display_name) = lower(?)" if name else ""
    args: tuple = (protocol,) + ((name,) if name else ())
    players = conn.execute(
        "SELECT DISTINCT p.player_id, p.display_name, p.team, p.created_at "
        "  FROM player p JOIN player_node pn ON pn.player_id = p.player_id "
        " WHERE pn.protocol = ? AND p.disabled_at IS NULL " + where +
        " ORDER BY p.display_name",
        args,
    ).fetchall()
    if not players:
        return []

    ids = [r["player_id"] for r in players]
    marks = ",".join("?" * len(ids))

    captures = dict(conn.execute(
        "SELECT l.by_player_id, count(*) FROM mc_tile_capture_log l "
        " WHERE l.season_id = ? AND l.by_player_id IN (%s) GROUP BY l.by_player_id" % marks,
        (season_id, *ids)).fetchall())
    taken = dict(conn.execute(
        "SELECT l.by_player_id, count(*) FROM mc_tile_capture_log l "
        " WHERE l.season_id = ? AND l.from_team IS NOT NULL AND l.by_player_id IN (%s) "
        " GROUP BY l.by_player_id" % marks, (season_id, *ids)).fetchall())
    checkins = {
        r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            "SELECT a.player_id, count(*), sum(a.points), max(a.net_date) "
            "  FROM mc_checkin_award a WHERE a.season_id = ? AND a.player_id IN (%s) "
            " GROUP BY a.player_id" % marks, (season_id, *ids)).fetchall()
    }
    # The run a player is currently carrying, which is the streak on
    # their most recent award -- not their longest ever.
    streaks = dict(conn.execute(
        "SELECT player_id, streak FROM mc_checkin_award a WHERE a.season_id = ? "
        "  AND a.player_id IN (%s) AND a.net_date = ("
        "      SELECT max(b.net_date) FROM mc_checkin_award b "
        "       WHERE b.season_id = a.season_id AND b.player_id = a.player_id)" % marks,
        (season_id, *ids)).fetchall())
    nodes: dict[int, list[str]] = {}
    for r in conn.execute(
        "SELECT player_id, node_ref FROM player_node WHERE protocol = ? "
        "  AND player_id IN (%s) ORDER BY bound_at" % marks, (protocol, *ids)):
        nodes.setdefault(r["player_id"], []).append(r["node_ref"])
    last_fix = dict(conn.execute(
        "SELECT player_id, ts FROM player_last_fix WHERE protocol = ? AND player_id IN (%s)" % marks,
        (protocol, *ids)).fetchall())

    # Places Worth Going (app/place_scoring.py): a player's personal
    # Explorer Score for this season. place_activation has no season_id
    # of its own -- it is week-scoped, not season-scoped, same reasoning
    # app/mc_scoring.team_place_points() documents -- so this scopes by
    # the season's own started_at/ends_at window instead. A missing
    # season row (should not happen -- season_id is always the caller's
    # own active/queried season) just yields no explorer points rather
    # than an error.
    explorer_points: dict[int, float] = {}
    season = conn.execute(
        "SELECT started_at, ends_at FROM mc_season WHERE id = ?", (season_id,)
    ).fetchone()
    if season is not None:
        explorer_points = dict(conn.execute(
            "SELECT player_id, SUM(points) FROM place_activation "
            " WHERE player_id IN (%s) AND protocol = ? AND awarded_at >= ? AND awarded_at <= ? "
            " GROUP BY player_id" % marks,
            (*ids, protocol, season["started_at"], season["ends_at"])).fetchall())

    out = []
    for p in players:
        pid = p["player_id"]
        ci = checkins.get(pid, (0, 0.0, None))
        out.append({
            "name": p["display_name"],
            "team": p["team"],
            "joined_at": p["created_at"],
            "radios": nodes.get(pid, []),
            "captures": captures.get(pid, 0),
            "captures_from_other_teams": taken.get(pid, 0),
            "checkins": ci[0],
            "checkin_points": round(ci[1] or 0.0, 2),
            "last_checkin_net_date": ci[2],
            "current_streak": streaks.get(pid),
            "last_position_ts": last_fix.get(pid),
            "explorer_points": explorer_points.get(pid, 0),
        })
    return out


@router.get("/api/v1/players")
async def v1_players(request: Request, board: str = "meshcore") -> JSONResponse:
    """The roster for one board, with each player's figures for the
    running season."""
    proto, err = _guard(request, board)
    if err:
        return err
    conn = connect()
    try:
        season = mc_api.active_season(conn, proto)
        if not season:
            return JSONResponse({"board": _BOARD_NAMES[proto], "players": []})
        return JSONResponse({
            "board": _BOARD_NAMES[proto],
            "season_id": season["id"],
            "players": _player_rows(conn, proto, season["id"]),
        })
    finally:
        conn.close()


@router.get("/api/v1/players/{name}")
async def v1_player(request: Request, name: str, board: str = "meshcore") -> JSONResponse:
    """One player by display name, case-insensitively. 404 if they are
    not registered on this board."""
    proto, err = _guard(request, board)
    if err:
        return err
    conn = connect()
    try:
        season = mc_api.active_season(conn, proto)
        rows = _player_rows(conn, proto, season["id"], name) if season else []
        if not rows:
            return JSONResponse({"error": "not found",
                                 "detail": "no player %r on the %s board" % (name, _BOARD_NAMES[proto])},
                                status_code=404)
        return JSONResponse({"board": _BOARD_NAMES[proto],
                             "season_id": season["id"], "player": rows[0]})
    finally:
        conn.close()


@router.get("/api/v1/top")
async def v1_top(request: Request, board: str = "meshcore",
                 kind: str = Query("captures", pattern="^(captures|checkins)$")) -> JSONResponse:
    """Rankings for the running season. Top 20, the same list the site's
    own Season Rankings shows."""
    proto, err = _guard(request, board)
    if err:
        return err
    rows = mc_api.top_for(proto) if kind == "captures" else mc_api.top_checkin_for(proto)
    return JSONResponse({"board": _BOARD_NAMES[proto], "kind": kind, "players": rows})


@router.get("/api/v1/board")
async def v1_board(request: Request, board: str = "meshcore") -> JSONResponse:
    """Every owned square in the running season, with its bounds.

    The heaviest route here by a wide margin -- several thousand squares
    -- so it is the one to fetch on a timer rather than per command.
    """
    proto, err = _guard(request, board)
    if err:
        return err
    cells = mc_api.board_for(proto)
    return JSONResponse({"board": _BOARD_NAMES[proto], "count": len(cells), "cells": cells})


@router.get("/api/v1/cells/{cell_id}")
async def v1_cell(request: Request, cell_id: str, board: str = "meshcore") -> JSONResponse:
    """One square: who holds it, every team's score on it, when it last
    changed hands, and the repeaters heard from it.

    Returns `recent_captures[].by_display_name` unredacted -- this
    route deliberately does NOT call mc_api._redact_cell_detail() the
    way GET /cell/{cell_id} and GET /api/mc/cell/{cell_id} do for an
    anonymous caller. That is not an oversight; see this module's own
    docstring's "one deliberate exception" paragraph for why an
    X-API-Key holder is treated as accountable rather than anonymous
    for this specific field.
    """
    proto, err = _guard(request, board)
    if err:
        return err
    detail = mc_api.cell_detail_for(proto, cell_id)
    if detail is None:
        return JSONResponse({"error": "not found", "detail": "no square %r" % cell_id},
                            status_code=404)
    return JSONResponse({"board": _BOARD_NAMES[proto], "cell": detail})


@router.get("/api/v1/captures")
async def v1_captures(request: Request, board: str = "meshcore",
                      since: int = 0,
                      limit: int = Query(100, ge=1, le=500)) -> JSONResponse:
    """Captures newest first, optionally only those after `since`.

    The event feed -- this is what a bot polls to announce "RED just
    took a square from BLUE". Pass the newest `ts` you have seen back as
    `since` and you get only what is new; the ordering guarantees you
    can.

    `player` (the capturing player's display_name, LEFT JOINed straight
    off mc_tile_capture_log) is returned unconditionally -- deliberately
    not gated the way the site's own cell popup gates the equivalent
    `by_display_name` field for an anonymous visitor. See this module's
    own docstring's "one deliberate exception" paragraph: an X-API-Key
    is accountable rather than anonymous, so it is treated the same as
    a signed-in session for this one field.
    """
    proto, err = _guard(request, board)
    if err:
        return err
    conn = connect()
    try:
        season = mc_api.active_season(conn, proto)
        if not season:
            return JSONResponse({"board": _BOARD_NAMES[proto], "captures": []})
        rows = conn.execute(
            "SELECT l.cell_id, l.ts, l.by_team, l.from_team, l.by_air, p.display_name AS player "
            "  FROM mc_tile_capture_log l "
            "  LEFT JOIN player p ON p.player_id = l.by_player_id "
            " WHERE l.season_id = ? AND l.ts > ? "
            " ORDER BY l.ts DESC LIMIT ?",
            (season["id"], since, limit),
        ).fetchall()
        return JSONResponse({
            "board": _BOARD_NAMES[proto],
            "season_id": season["id"],
            "count": len(rows),
            "captures": [{
                "cell_id": r["cell_id"],
                "ts": r["ts"],
                "player": r["player"],
                "team": r["by_team"],
                # null means it was unclaimed ground, not that the
                # previous owner is unknown.
                "from_team": r["from_team"],
                "by_air": bool(r["by_air"]),
            } for r in rows],
        })
    finally:
        conn.close()


@router.get("/api/v1/results")
async def v1_results(request: Request, board: str = "meshcore",
                     limit: int = Query(12, ge=1, le=60)) -> JSONResponse:
    """Finished months, newest first, with standings and honors, plus
    when the month in progress closes.

    The month in progress is not included -- a month is judged when it
    ends. See the rules page.
    """
    proto, err = _guard(request, board)
    if err:
        return err
    out = mc_api.results_for(proto, limit)
    out["board"] = _BOARD_NAMES[proto]
    return JSONResponse(out)


@router.get("/api/v1/net")
async def v1_net(request: Request, board: str = "meshcore") -> JSONResponse:
    """The weekly net: whether it is open, when the next one is, and who
    has checked in to the most recent one."""
    proto, err = _guard(request, board)
    if err:
        return err
    now = int(time.time())
    window = _net_window(now)
    conn = connect()
    try:
        season = mc_api.active_season(conn, proto)
        checkins = []
        net_date = None
        if season:
            row = conn.execute(
                "SELECT max(net_date) FROM mc_checkin_award WHERE season_id = ?",
                (season["id"],)).fetchone()
            net_date = row[0] if row else None
            if net_date:
                checkins = [{
                    "player": r["display_name"],
                    "team": r["team"],
                    "points": r["points"],
                    "streak": r["streak"],
                } for r in conn.execute(
                    "SELECT p.display_name, p.team, a.points, a.streak "
                    "  FROM mc_checkin_award a JOIN player p ON p.player_id = a.player_id "
                    " WHERE a.season_id = ? AND a.net_date = ? "
                    " ORDER BY a.points DESC, p.display_name",
                    (season["id"], net_date))]
        return JSONResponse({
            "board": _BOARD_NAMES[proto],
            "net": window,
            "latest_net_date": net_date,
            "latest_checkins": checkins,
        })
    finally:
        conn.close()


# ---- GET /api/v1/announcements -----------------------------------------
#
# The first keyless /api/v1 route -- see v1_announcements()'s own
# docstring for why. Everything below this line is specific to that
# shape (keyless-by-default, a valid key upgrades you): its own
# rate-limit tier, its own response cache. Neither is shared with the
# machinery above, deliberately (see each one's own comment). GET
# /api/v1/nets, further down, is keyless for the same reason and
# deliberately reuses this same guard and cache rather than growing a
# second copy of either.

# A fresh, route-local rate-limit budget for the anonymous tier -- built
# once at import time, same as every other _BoundedHits in this codebase
# (see app/auth.py's module docstring for why a call site never shares
# one with another). Deliberately NOT the same dict _rate_limited() above
# reads/writes (`_hits`): a keyless /api/v1/announcements caller must
# never be able to spend, or be starved by, budget any keyed or
# require_key=False route above tracks under the same address.
_announcements_anon_limiter = new_rate_limit_bucket()


def _announcements_guard(request: Request) -> JSONResponse | None:
    """Authenticate and rate limit for GET /api/v1/announcements only --
    NOT a call to _guard() above, because this route's shape is not one
    _guard() supports: every other route either always requires a key or
    always rate limits by address (v1_index, require_key=False); this
    one is keyless-by-default but a valid key upgrades the caller onto
    the normal per-key budget instead. Returns an error JSONResponse, or
    None when the caller may proceed.

    A key that fails to authenticate still 401s exactly like every other
    route -- presenting a bad key is not the same thing as presenting no
    key at all, and must not silently fall back to the (tighter)
    anonymous tier.

    Every 429 here carries a `Retry-After` header (seconds), which
    _rate_limited()'s own 429 (used by every other route above) does
    not -- this route is the one meant to be polled by unattended bots
    with no human watching the response body, so the machine-readable
    header matters more here than it has anywhere else in this module.
    """
    raw = request.headers.get(_KEY_HEADER, "")
    if raw:
        if _authenticate(raw) is None:
            return JSONResponse(
                {"error": "unauthorized", "detail": "unknown or revoked key"},
                status_code=401,
            )
        # The normal per-key /api/v1 budget -- same _hits dict and same
        # settings every other keyed route in this module shares, so a
        # key's spend here counts against, and is counted by, its spend
        # everywhere else. A key upgrades a caller onto this fast lane;
        # it never adds a SECOND budget on top of it.
        if _rate_limited(hash_secret(raw)):
            return JSONResponse(
                {"error": "rate limited",
                 "detail": "%d requests per %d seconds"
                           % (settings.public_api_rate_limit_requests,
                              settings.public_api_rate_limit_window_seconds)},
                status_code=429,
                headers={"Retry-After": str(settings.public_api_rate_limit_window_seconds)},
            )
        return None

    ip = _client_ip(request)
    limit = settings.announcements_anon_rate_limit_requests
    window = settings.announcements_anon_rate_limit_window_seconds
    if _announcements_anon_limiter.limited(ip, limit=limit, window=window):
        return JSONResponse(
            {"error": "rate limited", "detail": "%d requests per %d seconds" % (limit, window)},
            status_code=429,
            headers={"Retry-After": str(window)},
        )
    return None


# ---- response cache, modeled on app/places_api.py's cached_places_
# response/_PLACES_CACHE (itself modeled on app/mc_api.py's
# cached_json_response/_BOARD_CACHE) -- same shape: cache the SERIALIZED
# bytes (so N pollers hitting an unchanged feed don't each pay their own
# json.dumps), key the ETag inside the per-entry object (so a validator
# minted against one entry's bytes can never 304 a request against a
# different entry's), ttl = 0 disables the cache. Deliberately its own,
# small, self-contained copy here rather than an import from either
# sibling module -- same "this module's cache can change without
# touching that one's" reasoning app/places_api.py's own comment gives
# for not sharing app/mc_api.py's. No gzip tier: unlike the board or the
# places viewport, a page of announcements is small (limit caps at 100
# rows of short, budget-capped text), so there is no equivalent of the
# py-spy finding that justified paying for that complexity there.
_ANNOUNCEMENTS_CACHE_MAX = 512


class _CachedAnnouncementsBody:
    __slots__ = ("built_at", "body", "etag")

    def __init__(self, built_at: float, body: bytes, etag: str) -> None:
        self.built_at = built_at
        self.body = body
        self.etag = etag


_ANNOUNCEMENTS_CACHE: "OrderedDict[str, _CachedAnnouncementsBody]" = OrderedDict()


def _cached_announcements_response(key: str, ttl: int, build, request: Request) -> Response:
    """Serve `build()`'s result as JSON, reusing the serialized bytes for
    up to `ttl` seconds under `key` (the full set of query parameters
    that affect the result -- see v1_announcements()'s own cache_key).

    ETag: a strong validator (sha256 of the serialized body) is computed
    and checked against an incoming If-None-Match on EVERY call, cache
    hit or miss, ttl=0 or not -- minting one costs nothing extra on top
    of the serialization this route already pays for. A polling bot
    that has already seen everything currently in the feed sends the
    same cursor and gets the same bytes back every time; honouring
    If-None-Match means that costs this process a hash comparison and a
    304, not a query and a full payload -- the whole point of a bot
    being ABLE to poll this route on a tight interval in the first
    place.
    """
    now = time.monotonic()

    def answer(entry: _CachedAnnouncementsBody) -> Response:
        headers = {"ETag": entry.etag}
        if ttl > 0:
            headers["Cache-Control"] = f"public, max-age={ttl}"
        if request.headers.get("if-none-match") == entry.etag:
            return Response(status_code=304, headers=headers)
        return Response(content=entry.body, media_type="application/json", headers=headers)

    if ttl > 0:
        hit = _ANNOUNCEMENTS_CACHE.get(key)
        if hit is not None and now - hit.built_at < ttl:
            _ANNOUNCEMENTS_CACHE.move_to_end(key)
            return answer(hit)

    # ensure_ascii=False + allow_nan=False, matching JSONResponse.render
    # (starlette.responses) byte-for-byte -- see app/places_api.py's
    # cached_places_response for why: content is ASCII by construction
    # today (app/announce_content.py's own HARD RULE) but this keeps the
    # bytes identical to what a plain JSONResponse would have sent
    # regardless.
    body = json.dumps(
        build(), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    etag = '"%s"' % hashlib.sha256(body).hexdigest()[:32]
    entry = _CachedAnnouncementsBody(now, body, etag)
    if ttl > 0:
        _ANNOUNCEMENTS_CACHE[key] = entry
        _ANNOUNCEMENTS_CACHE.move_to_end(key)
        if len(_ANNOUNCEMENTS_CACHE) > _ANNOUNCEMENTS_CACHE_MAX:
            _ANNOUNCEMENTS_CACHE.popitem(last=False)
    return answer(entry)


# The recommended poll interval this route hands back in every response
# (`poll_interval_seconds`) -- a fixed, documented constant, NOT
# settings.announcement_poll_interval_seconds (that setting is
# app/announce.py's own internal due-check cadence, 60s by default, an
# entirely different concern: how often THIS SERVER checks whether a new
# announcement is due, not how often an outside consumer should ask for
# one). 900s (15 minutes) is generous headroom over how often anything
# genuinely new can appear -- the fastest-moving provider, net_wrapup,
# fires once per net -- so a forked bot that simply reads this number
# picks a sane cadence instead of guessing (or, worse, polling every few
# seconds "to be safe").
_RECOMMENDED_POLL_INTERVAL_SECONDS = 900


@router.get("/api/v1/announcements")
async def v1_announcements(
    request: Request,
    since: int = 0,
    kinds: str | None = None,
    board: str | None = None,
    net_id: int | None = None,
    limit: int = 20,
    text_budget: int = 150,
) -> Response:
    """The public announcement feed -- app/db.py's `announcement` table
    (daily recaps, weekly recaps, month honors, net wrap-ups; see
    app/announce.py for when each is built and app/announce_content.py
    for their shape).

    KEYLESS BY DESIGN -- the one route in this module that does not
    require an X-API-Key (see _announcements_guard(), not _guard()).
    Every other route in this file gates on a key mainly so a
    misbehaving integration can be identified and revoked on its own
    (see this module's own docstring); that reasoning does not apply
    here. An announcement is already public broadcast news about a
    public game -- headline honors and recaps destined for an open radio
    channel anyone can listen to -- so gating it behind a key protects
    nothing, and would only mean every third-party bot author has to
    personally ask the operator for a key before their bot can work at
    all. Instead, a keyless caller gets a tight, address-keyed rate
    limit of its own (announcements_anon_rate_limit_requests per
    announcements_anon_rate_limit_window_seconds); a caller who does
    present a valid key is treated exactly like every other /api/v1
    caller (the normal public_api_rate_limit_requests/window_seconds
    per-key budget) -- a key upgrades you to the fast lane here, it is
    just never required to use this route at all.

    `since` is the poll cursor: only rows with id > since are returned,
    ORDER BY id ASC (oldest first) -- unlike /api/v1/captures (newest
    first), a consumer replaying a cursor across a restart or an outage
    must see events in the order they actually happened, not have to
    sort them itself. `next_since` is the highest id actually returned,
    or the incoming `since` unchanged when nothing was -- so a consumer
    can always poll again with `since=<next_since>` and never has to
    track ids on its own.

    `kinds` filters on `kind` (comma-separated, e.g.
    "daily_recap,month_honors"); `board` accepts the same spellings
    _BOARDS above does; `net_id` filters to one checkin_net's own
    wrap-ups. `limit` (default 20) and `text_budget` (default 150,
    MeshCore's own single-packet budget -- see app/mesh_render.py) are
    both silently CLAMPED to their documented bounds rather than
    rejected -- a caller passing an oversized limit gets the capped
    response it should have asked for, not a 400.

    `text` is each row's stored Content dict, rendered through
    app/mesh_render.py's render_mesh() at the caller's own text_budget
    -- render_mesh()'s own hard guarantee is that the result never
    exceeds that many UTF-8 bytes, so a caller building a radio bot does
    not need to reimplement that renderer just to know what would
    actually fit on the air.

    Served through _cached_announcements_response()
    (settings.announcements_cache_seconds) -- see that function for the
    ETag/304 contract, which applies regardless of the cache TTL.
    """
    err = _announcements_guard(request)
    if err:
        return err

    limit = max(1, min(limit, 100))
    text_budget = max(20, min(text_budget, 1000))

    kind_list = sorted({k.strip() for k in kinds.split(",") if k.strip()}) if kinds else None

    proto = None
    if board is not None:
        proto = _protocol(board)
        if proto is None:
            return JSONResponse(
                {"error": "unknown board",
                 "detail": "board must be one of: meshcore, meshtastic"},
                status_code=400,
            )

    cache_key = "|".join([
        str(since),
        ",".join(kind_list) if kind_list else "",
        proto or "",
        str(net_id) if net_id is not None else "",
        str(limit),
        str(text_budget),
    ])

    def build() -> dict:
        query = ("SELECT id, kind, key, board, net_id, content, created_at "
                  "  FROM announcement WHERE id > ?")
        args: list = [since]
        if kind_list:
            marks = ",".join("?" * len(kind_list))
            query += f" AND kind IN ({marks})"
            args.extend(kind_list)
        if proto is not None:
            query += " AND board = ?"
            args.append(proto)
        if net_id is not None:
            query += " AND net_id = ?"
            args.append(net_id)
        query += " ORDER BY id ASC LIMIT ?"
        args.append(limit)

        conn = connect()
        try:
            rows = conn.execute(query, args).fetchall()
        finally:
            conn.close()

        next_since = since
        items = []
        for r in rows:
            content = json.loads(r["content"])
            items.append({
                "id": r["id"],
                "kind": r["kind"],
                "key": r["key"],
                "board": r["board"],
                "net_id": r["net_id"],
                "created_at": r["created_at"],
                "content": content,
                "text": render_mesh(content, budget_bytes=text_budget),
            })
            next_since = r["id"]

        return {
            "announcements": items,
            "next_since": next_since,
            "poll_interval_seconds": _RECOMMENDED_POLL_INTERVAL_SECONDS,
        }

    return _cached_announcements_response(
        cache_key, settings.announcements_cache_seconds, build, request)


# ---- GET /api/v1/nets ----------------------------------------------------


@router.get("/api/v1/nets")
async def v1_nets(request: Request) -> Response:
    """The enabled check-in nets -- id, label, board, and schedule -- so
    a third-party bot's operator can choose a net BY NAME and pass its
    `id` to /api/v1/announcements?net_id= or elsewhere, instead of
    guessing a numeric id nothing else on this surface hands out.

    KEYLESS, sharing v1_announcements()'s own _announcements_guard()
    rather than a second copy of it -- this route is the same
    keyless-with-key-upgrade shape for the same reason: a net's
    schedule is already shown on the site's own check-in pages, so
    gating it behind a key protects nothing.

    Only rows with enabled = 1, ordered by id -- a disabled net is not
    a choice a caller should be offered.

    NEVER returns `connector_url`, `broker_username`, `broker_password`,
    `channel_key`, `topic_root`, `channel`, or `hashtag`. `broker_
    password` and `channel_key` are outright secrets (see app/db.py's
    checkin_net table comment); the rest name this operator's own
    private upstream infrastructure -- which connector, which broker,
    which channel or hashtag it polls -- that has no bearing on a
    caller picking a net by name and would only leak deployment detail
    nobody asked for. This is intentionally stricter than the admin
    surface's own app/admin_ops.py:_scrub_secrets(), which hides only
    the two secrets because an admin is allowed to see the rest of
    their own config. Do not widen this response to match that shape;
    ask Matt first.
    """
    err = _announcements_guard(request)
    if err:
        return err

    def build() -> dict:
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT id, label, protocol, weekday, start_hour, end_hour, timezone "
                "  FROM checkin_net WHERE enabled = 1 ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        return {
            "nets": [{
                "id": r["id"],
                "label": r["label"],
                "board": r["protocol"],
                "weekday": r["weekday"],
                "start_hour": r["start_hour"],
                "end_hour": r["end_hour"],
                "timezone": r["timezone"],
            } for r in rows],
        }

    return _cached_announcements_response(
        "nets", settings.announcements_cache_seconds, build, request)
