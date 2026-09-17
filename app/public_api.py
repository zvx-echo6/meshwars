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

import logging
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from . import mc_api, results
from .checkin import load_checkin_config
from .client_ip import get_client_ip
from .config import settings
from .db import connect
from .mc_ingest import PROTOCOL as MC_PROTOCOL, hash_secret
from .mc_scoring import team_checkin_points, team_tile_counts

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
            " WHERE player_id IN (%s) AND awarded_at >= ? AND awarded_at <= ? "
            " GROUP BY player_id" % marks,
            (*ids, season["started_at"], season["ends_at"])).fetchall())

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
