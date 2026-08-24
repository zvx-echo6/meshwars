"""Operator routes: what is wrong right now, and the actions to fix it.

The admin panel could already list players and revoke keys. It could not
answer the question an operator is actually asked -- "why is nothing
happening for me" -- because the data that answers it, player_ingest_stat,
is reachable only through the PLAYER-facing /api/mc/status, which needs
that player's own key. Keys cannot be recovered, so the person doing the
supporting was the one person who could not look.

Everything here follows from that. `overview` computes what is wrong
rather than listing what exists, so an operator reads a short list of
problems instead of scrolling a long list of players hoping to spot one.
The actions are the four things that previously meant running Python
inside the container: extending a season, awarding a missed check-in,
freezing a month, and registering a fallback net name for somebody whose
radio the directory has never seen.

Read routes here are diagnostic and safe. The write routes are not, and
each one says in its own docstring what it can break.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import mc_api, results
from .admin_api import _api_guard
from .checkin import checkin_streak, streak_points, MC_PROTOCOL as CHK_MC
from .config import settings
from .db import connect
from .mc_ingest import PROTOCOL as MC_PROTOCOL
from .node_ref import normalize_sender_name
from .place_rotation import preview_week, week_start_for_date, week_start_for_ts

log = logging.getLogger("admin_ops")

router = APIRouter()

MT_PROTOCOL = "mt"
_STALE_DAYS = 14


# ---- needs attention ---------------------------------------------------


def _attention(conn, directory: list[dict]) -> list[dict]:
    """Everything currently wrong that an operator can act on.

    Ordered by how stuck the player is, not alphabetically: somebody who
    has never had a ping accepted needs help before somebody whose
    coverage merely lapsed. Each entry carries the fix in words, because
    an operator reading this at 2am should not have to remember what
    "no_contact" implies about a MeshMapper setting three menus deep.
    """
    now = int(time.time())
    out: list[dict] = []

    def add(player, kind, detail, fix, severity="warn"):
        out.append({"player_id": player["player_id"], "player": player["display_name"],
                    "team": player["team"], "kind": kind, "detail": detail,
                    "fix": fix, "severity": severity})

    players = {r["player_id"]: r for r in conn.execute(
        "SELECT player_id, display_name, team FROM player WHERE disabled_at IS NULL")}

    have_radio = {r[0] for r in conn.execute("SELECT DISTINCT player_id FROM player_node")}
    have_key = {r[0] for r in conn.execute(
        "SELECT DISTINCT player_id FROM api_key WHERE revoked_at IS NULL")}

    stats: dict[int, dict] = {}
    for r in conn.execute(
        "SELECT player_id, sum(pings_accepted) a, sum(pings_no_contact) nc, "
        "       sum(pings_wrong_owner) wo, sum(pings_out_of_area) oa, "
        "       sum(pings_no_repeaters) nr FROM player_ingest_stat GROUP BY player_id"
    ):
        stats[r["player_id"]] = dict(r)

    last_fix = dict(conn.execute("SELECT player_id, max(ts) FROM player_last_fix GROUP BY player_id"))

    for pid, p in players.items():
        st = stats.get(pid, {})
        acc = st.get("a") or 0

        if pid not in have_radio:
            add(p, "no_radio", "registered but has never bound a radio",
                "On MeshCore this happens by itself on the first wardrive if "
                "MeshMapper's Include Contact Key is on. On Meshtastic they have "
                "to add the node ID on the join page.", "bad")
            continue

        if pid not in have_key:
            add(p, "no_key", "has no working key",
                "Issue an extra key from the Players panel -- do not reissue, "
                "that breaks whatever they already have set up.", "bad")

        if acc == 0:
            if (st.get("nc") or 0) > 0:
                add(p, "no_contact_key",
                    "%d pings arrived without a contact key, none accepted" % st["nc"],
                    "MeshMapper: Settings, API Endpoints, turn on Include Contact "
                    "Key. This is the most common setup miss there is.", "bad")
            elif (st.get("oa") or 0) > 0:
                add(p, "out_of_area",
                    "%d pings from outside the play area, none accepted" % st["oa"],
                    "They are outside the box. Nothing to fix unless the play "
                    "area should be wider.", "bad")
            elif (st.get("nr") or 0) > 0:
                add(p, "no_repeaters",
                    "%d pings reached no repeaters, none accepted" % st["nr"],
                    "Their radio is transmitting but nothing is hearing it. "
                    "Antenna, power, or genuinely no coverage where they are.", "bad")
            elif st:
                add(p, "never_accepted", "has sent pings but none were ever accepted",
                    "Open their diagnostics to see which counter is moving.", "bad")
            else:
                add(p, "never_sent", "has a radio bound but has never sent a ping",
                    "They have set up far enough to bind a radio and then stopped. "
                    "Usually worth a nudge.", "warn")

        if (st.get("wo") or 0) > 0:
            add(p, "wrong_owner",
                "%d pings carried a radio registered to someone else" % st["wo"],
                "Either two people are sharing a key, or a radio changed hands "
                "and its old binding is still in place.", "bad")

        seen = last_fix.get(pid)
        if acc > 0 and seen and now - seen > _STALE_DAYS * 86400:
            add(p, "stale", "no position in %d days" % ((now - seen) // 86400),
                "Nothing is broken; they have stopped playing.", "info")

    # ---- check-in reachability, MeshCore only ------------------------
    # A player whose contact has never appeared in the mwmesh directory
    # cannot be resolved from a net message, so they can never earn a
    # check-in no matter how many they post -- and nothing anywhere told
    # anyone. The fallback is a hand-registered sender name.
    if directory:
        keys = {str(n.get("public_key", "")).lower()[:8] for n in directory}
        bound = {}
        for r in conn.execute(
            "SELECT pn.player_id, pn.node_ref FROM player_node pn "
            "  JOIN player p ON p.player_id = pn.player_id "
            " WHERE pn.protocol = 'mc' AND p.disabled_at IS NULL"
        ):
            bound.setdefault(r["player_id"], []).append(r["node_ref"])
        fallbacks = {r[0] for r in conn.execute("SELECT DISTINCT player_id FROM mc_checkin_binding")}
        for pid, refs in bound.items():
            if pid in fallbacks or any(ref in keys for ref in refs):
                continue
            p = players.get(pid)
            if p:
                add(p, "checkin_unreachable",
                    "MeshCore radio has never appeared in the mwmesh directory",
                    "They can wardrive normally but can never earn a net check-in. "
                    "Register the name their check-ins appear under, below.", "warn")

    order = {"bad": 0, "warn": 1, "info": 2}
    out.sort(key=lambda e: (order.get(e["severity"], 3), e["player"].lower()))
    return out


def _health(conn) -> dict:
    """Is the machine doing its job. Every figure here answers a question
    that was previously only answerable by reading logs."""
    now = int(time.time())

    def count(sql, *args):
        try:
            return conn.execute(sql, args).fetchone()[0]
        except sqlite3.OperationalError:
            return None

    db_bytes = None
    try:
        db_bytes = os.path.getsize(settings.db_path)
    except OSError:
        pass
    free_bytes = None
    try:
        st = os.statvfs(os.path.dirname(settings.db_path) or "/")
        free_bytes = st.f_bavail * st.f_frsize
    except OSError:
        pass

    from . import places
    return {
        "now": now,
        "pings_last_hour": count(
            "SELECT count(*) FROM player_cell_ping WHERE seen_at > ?", now - 3600),
        "pings_last_day": count(
            "SELECT count(*) FROM player_cell_ping WHERE seen_at > ?", now - 86400),
        "last_ping_at": count("SELECT max(seen_at) FROM player_cell_ping"),
        "players_active_today": count(
            "SELECT count(DISTINCT player_id) FROM player_cell_ping WHERE seen_at > ?",
            now - 86400),
        # NOT a poller heartbeat, and it was briefly used as one here:
        # a message is only recorded once it is SETTLED, and a message
        # nobody could be matched to is deliberately left unsettled so a
        # later poll can retry it. Between nets the channel is quiet and
        # this stands still while the poller runs perfectly. It is the
        # age of the newest decision, which is a different question.
        "last_settled_checkin_at": count("SELECT max(seen_at) FROM mc_checkin_seen_message"),
        "database_bytes": db_bytes,
        "disk_free_bytes": free_bytes,
        # The exploration awards skip themselves silently when this file
        # is missing, which is exactly how it went unnoticed once.
        "places_loaded": places.loaded_count(),
    }


def _poller_health(request: Request) -> dict:
    """Liveness of the check-in poller, read off the running object.

    In memory, so it resets when the process does -- which is the honest
    answer, since what is being asked is whether the loop is turning
    right now. The database cannot answer that: a poller that runs
    perfectly writes nothing at all between nets.
    """
    poller = getattr(request.app.state, "checkin_poller", None)
    if poller is None:
        return {"running": False, "last_poll_at": None, "last_error": None}
    return {
        "running": True,
        "last_poll_at": getattr(poller, "last_poll_at", 0) or None,
        "last_error": getattr(poller, "last_poll_error", None),
    }


@router.get("/api/admin/overview")
async def admin_overview(request: Request):
    """What is wrong right now, plus the health figures behind it."""
    guard = _api_guard(request)
    if guard is not None:
        return guard

    poller = getattr(request.app.state, "checkin_poller", None)
    directory = poller.directory_snapshot() if poller else []

    conn = connect()
    try:
        boards = []
        for proto in (MC_PROTOCOL, MT_PROTOCOL):
            season = mc_api.active_season(conn, proto)
            boards.append({
                "board": proto,
                "season_id": season["id"] if season else None,
                "ends_at": season["ends_at"] if season else None,
                "squares": conn.execute(
                    "SELECT count(*) FROM mc_tile WHERE season_id = ? AND owner_team IS NOT NULL",
                    (season["id"],)).fetchone()[0] if season else 0,
            })
        return JSONResponse({
            "health": dict(_health(conn), checkin_poller=_poller_health(request)),
            "boards": boards,
            "directory_size": len(directory),
            "attention": _attention(conn, directory),
        })
    finally:
        conn.close()


@router.get("/api/admin/places/preview")
async def admin_places_preview(request: Request, week_start: str | None = None):
    """Preview the Places Worth Going rotation draw for a week WITHOUT
    persisting it (app/place_rotation.preview_week) -- operator only, so
    the density and spacing can be sanity-checked before (or long after)
    a week actually happens. Never writes place_week; the real draw for
    the CURRENT week is still resolved lazily, the first time a scoring
    ping or a places API call needs it (see place_rotation.resolve_week).

    `week_start` is any date string; it is snapped to that date's own
    Wednesday via week_start_for_ts (the same clock the whole feature
    uses), so an operator does not have to already know which Wednesday
    a given day belongs to. Omitted, it previews the current week.
    """
    guard = _api_guard(request)
    if guard is not None:
        return guard

    if week_start:
        try:
            snapped = week_start_for_date(datetime.fromisoformat(week_start).date())
        except ValueError:
            return JSONResponse({"error": "week_start must be an ISO date, e.g. 2026-08-19"}, status_code=400)
    else:
        snapped = week_start_for_ts(int(time.time()))

    conn = connect()
    try:
        chosen_ids, region_report = preview_week(conn, snapped)
        sample_rows = []
        if chosen_ids:
            marks = ",".join("?" * min(len(chosen_ids), 20))
            sample_rows = conn.execute(
                f"SELECT ref_type, name, points, lat, lon FROM place WHERE id IN ({marks})",
                chosen_ids[:20],
            ).fetchall()
        by_type = {}
        if chosen_ids:
            for r in conn.execute(
                "SELECT ref_type, COUNT(*) c FROM place "
                f"WHERE id IN ({','.join('?' * len(chosen_ids))}) GROUP BY ref_type",
                chosen_ids,
            ):
                by_type[r["ref_type"]] = r["c"]
    finally:
        conn.close()

    cells_with_candidates = [v for v in region_report.values() if v["candidates"] > 0]
    candidate_counts = [v["candidates"] for v in cells_with_candidates] or [0]
    top_cells = sorted(
        ({"cell": k, **v} for k, v in region_report.items() if v["candidates"] > 0),
        key=lambda x: -x["candidates"],
    )[:10]

    return JSONResponse({
        "week_start": snapped,
        "live_rotating_count": len(chosen_ids),
        "region_cells_with_candidates": len(cells_with_candidates),
        "region_cells_with_a_live_pick": sum(1 for v in cells_with_candidates if v["chosen"] > 0),
        "candidates_per_cell": {
            "min": min(candidate_counts), "max": max(candidate_counts),
            "mean": round(sum(candidate_counts) / len(candidate_counts), 2),
        },
        "densest_cells": top_cells,
        "by_type": by_type,
        "sample": [dict(r) for r in sample_rows],
    })


@router.get("/api/admin/player/{player_id}/diagnostics")
async def admin_player_diagnostics(request: Request, player_id: int):
    """One player's ingest counters by day, newest first.

    This is /api/mc/status's data without needing that player's key --
    the whole reason this module exists.
    """
    guard = _api_guard(request)
    if guard is not None:
        return guard

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM player_ingest_stat WHERE player_id = ? ORDER BY day DESC LIMIT 30",
            (player_id,)).fetchall()
        return JSONResponse({"player_id": player_id, "days": [dict(r) for r in rows]})
    finally:
        conn.close()


# ---- actions -----------------------------------------------------------


@router.post("/api/admin/season/extend")
async def admin_season_extend(request: Request):
    """Move a running season's end date.

    Changing settings.season_days only affects the NEXT season, because
    ends_at is written onto the row when it opens. This is what actually
    changes the season people are playing, and it is how the current
    six-month seasons were set.

    Shortening one past `now` will end it on the next poll and crown a
    winner, so this refuses to set an end date in the past.
    """
    guard = _api_guard(request)
    if guard is not None:
        return guard
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    season_id = body.get("season_id")
    ends_at = body.get("ends_at")
    if not isinstance(season_id, int) or not isinstance(ends_at, int):
        return JSONResponse({"error": "season_id and ends_at are required"}, status_code=400)
    if ends_at <= int(time.time()):
        return JSONResponse(
            {"error": "that end date is in the past -- the season would close immediately"},
            status_code=400)

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE mc_season SET ends_at = ? WHERE id = ? AND status = 'active'",
            (ends_at, season_id))
        conn.execute("COMMIT")
    finally:
        conn.close()
    if not cur.rowcount:
        return JSONResponse({"error": "no active season with that id"}, status_code=404)
    log.info("admin: season %d now ends at %d", season_id, ends_at)
    return JSONResponse({"season_id": season_id, "ends_at": ends_at})


@router.post("/api/admin/checkin/award")
async def admin_checkin_award(request: Request):
    """Credit a check-in somebody earned but did not receive.

    The streak and the points are computed the same way the poller
    computes them, so a hand-added award is worth exactly what it would
    have been worth on the night -- including its effect on the runs of
    everyone whose streak depends on that net having happened.

    Use it when the feed dropped somebody, not to hand out points: the
    monthly honors read these rows and cannot tell the difference.
    """
    guard = _api_guard(request)
    if guard is not None:
        return guard
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    player_id = body.get("player_id")
    net_date = (body.get("net_date") or "").strip()
    protocol = (body.get("protocol") or CHK_MC).strip()
    if not isinstance(player_id, int) or len(net_date) != 10:
        return JSONResponse({"error": "player_id and net_date (YYYY-MM-DD) are required"},
                            status_code=400)

    now = int(time.time())
    conn = connect()
    try:
        season = mc_api.active_season(conn, protocol)
        if not season:
            return JSONResponse({"error": "no active season for that board"}, status_code=400)
        streak = checkin_streak(conn, player_id, protocol, net_date)
        points = streak_points(streak)
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT OR IGNORE INTO mc_checkin_award"
            "(season_id, player_id, net_date, points, protocol, message_id, awarded_at, streak) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (season["id"], player_id, net_date, points, protocol, "admin", now, streak))
        conn.execute("COMMIT")
    finally:
        conn.close()
    if not cur.rowcount:
        return JSONResponse({"error": "already credited for that net"}, status_code=409)
    log.info("admin: awarded %s net %s to player %d (streak %d)", protocol, net_date, player_id, streak)
    return JSONResponse({"player_id": player_id, "net_date": net_date,
                         "points": points, "streak": streak})


@router.post("/api/admin/checkin/binding")
async def admin_checkin_binding(request: Request):
    """Register the name a player's check-ins appear under.

    Only needed for a MeshCore player whose radio has never shown up in
    the mwmesh directory -- there is no public key to match them on, so
    the name is all there is. The poller ignores this for anyone the
    directory CAN resolve, so adding one for the wrong person achieves
    nothing rather than stealing their check-ins.

    Send an empty name to remove one.
    """
    guard = _api_guard(request)
    if guard is not None:
        return guard
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    player_id = body.get("player_id")
    name = (body.get("sender_name") or "").strip()
    if not isinstance(player_id, int):
        return JSONResponse({"error": "player_id is required"}, status_code=400)

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not name:
            conn.execute("DELETE FROM mc_checkin_binding WHERE player_id = ?", (player_id,))
            conn.execute("COMMIT")
            return JSONResponse({"player_id": player_id, "sender_name": None})
        normalized = normalize_sender_name(name)
        if normalized is None:
            conn.execute("ROLLBACK")
            return JSONResponse({"error": "that name normalizes to nothing"}, status_code=400)
        conn.execute("DELETE FROM mc_checkin_binding WHERE player_id = ?", (player_id,))
        conn.execute(
            "INSERT OR REPLACE INTO mc_checkin_binding(sender_name, player_id) VALUES (?, ?)",
            (normalized, player_id))
        conn.execute("COMMIT")
    finally:
        conn.close()
    log.info("admin: check-in fallback name %r -> player %d", normalized, player_id)
    return JSONResponse({"player_id": player_id, "sender_name": normalized})


@router.post("/api/admin/month/freeze")
async def admin_month_freeze(request: Request):
    """Freeze or re-freeze one month's result.

    Months close on their own from ordinary traffic. This is for the two
    cases that do not: a month that closed while history was wrong and
    needs recomputing, and a month nothing has closed yet because the
    service was down across the boundary.

    Refuses the month in progress. A result that can still change is not
    a result.
    """
    guard = _api_guard(request)
    if guard is not None:
        return guard
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    month = (body.get("month") or "").strip()
    protocol = (body.get("protocol") or MC_PROTOCOL).strip()
    if len(month) != 7 or month[4] != "-":
        return JSONResponse({"error": "month must look like 2026-08"}, status_code=400)

    now = int(time.time())
    if month >= results.month_key(now):
        return JSONResponse({"error": "that month has not finished yet"}, status_code=400)

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        results.freeze_month(conn, protocol, month, now)
        conn.execute("COMMIT")
    finally:
        conn.close()
    log.info("admin: froze %s for %s by hand", month, protocol)
    return JSONResponse({"month": month, "protocol": protocol, "frozen_at": now})
