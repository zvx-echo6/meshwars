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

import base64
import logging
import os
import sqlite3
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import mc_api, results
from .admin_api import _api_guard
from .checkin import (
    checkin_streak, load_checkin_config, streak_points, MC_PROTOCOL as CHK_MC,
    KIND_CORESCOPE, KIND_BEACON, KIND_MESHVIEW, KIND_MQTT, KIND_PROTOCOL,
)
from .config import settings
from .db import connect
from .mc_ingest import PROTOCOL as MC_PROTOCOL
from .node_ref import normalize_sender_name
from .place_rotation import preview_week, week_start_for_date, week_start_for_ts

log = logging.getLogger("admin_ops")

router = APIRouter()

MT_PROTOCOL = "mt"
_STALE_DAYS = 14
# The admin-chosen field a net's connector actually is -- protocol
# ('mc'/'mt') is derived FROM this on every write (see
# _validate_net_fields and app/checkin.py's KIND_PROTOCOL), never
# accepted as an independent choice, so the two can never disagree.
_NET_KINDS = (KIND_CORESCOPE, KIND_BEACON, KIND_MESHVIEW, KIND_MQTT)


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
        # Reads checkin_seen_message, not the now-superseded
        # mc_checkin_seen_message (see app/db.py) -- every protocol's
        # settle-decisions land there since nets moved into the
        # database, and mc_checkin_seen_message stops getting new rows
        # entirely from that point on, which would freeze this figure
        # at deploy time forever if it kept reading the old table.
        "last_settled_checkin_at": count("SELECT max(seen_at) FROM checkin_seen_message"),
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
                "SELECT ref_type, name, points, points_reason, lat, lon "
                f"FROM place WHERE id IN ({marks})",
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
        config = load_checkin_config(conn)
        streak = checkin_streak(conn, player_id, protocol, net_date)
        points = streak_points(config, streak)
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


# ---- check-in nets (app/checkin.py's CheckinPoller, app/db.py's checkin_net) --


def _validate_net_fields(body, current: dict | None = None) -> tuple[dict, JSONResponse | None]:
    """Validate and normalize a net's editable fields, shared by
    create and update below -- the same shape both routes need, so the
    rules can only ever say one thing about what a valid net looks
    like.

    `current` is the existing checkin_net row being edited (a dict, as
    admin_checkin_net_update fetches before calling this), or None when
    creating -- it is consulted ONLY for the two mqtt secret fields
    (broker_password/channel_key): see below for why those need the
    existing row and every other field does not.

    Returns (fields, None) on success, where `fields` is ready to bind
    straight into an INSERT/UPDATE; on the first thing wrong, returns
    ({}, response) with the 400 to hand back as-is.
    """
    if not isinstance(body, dict):
        return {}, JSONResponse({"error": "bad request"}, status_code=400)

    label = (body.get("label") or "").strip()
    if not label:
        return {}, JSONResponse({"error": "label is required"}, status_code=400)

    # `kind` is the admin's actual choice -- which connector
    # implementation this net's connector_url speaks (see
    # app/checkin.py's CoreScopeClient/BeaconClient/MeshviewClient).
    # `protocol` is the scoring-board discriminator and is DERIVED from
    # kind here, never accepted as an independent field: a caller MAY
    # send `protocol` (existing behavior, and the admin panel's own GET
    # /api/admin/checkin/nets response echoes it back), but if they do
    # it must agree with what this kind derives to, or be rejected --
    # otherwise a client could persist a net whose protocol (and
    # therefore which scoring board/season/streak it feeds) disagrees
    # with which connector it is actually polled through.
    kind = body.get("kind")
    if kind not in _NET_KINDS:
        return {}, JSONResponse(
            {"error": "kind must be one of: " + ", ".join(_NET_KINDS)},
            status_code=400)
    protocol = KIND_PROTOCOL[kind]
    explicit_protocol = body.get("protocol")
    if explicit_protocol is not None and explicit_protocol != protocol:
        return {}, JSONResponse(
            {"error": "protocol %r does not match kind %r (expected %r)" % (
                explicit_protocol, kind, protocol)},
            status_code=400)

    connector_url = (body.get("connector_url") or "").strip().rstrip("/")
    if kind == KIND_MQTT:
        # A broker, not an HTTP API -- see app/db.py's checkin_net
        # comment on connector_url.
        if not (connector_url.startswith("mqtt://") or connector_url.startswith("mqtts://")):
            return {}, JSONResponse(
                {"error": "connector_url must be an mqtt:// or mqtts:// URL for a mqtt net"},
                status_code=400)
    elif not (connector_url.startswith("http://") or connector_url.startswith("https://")):
        return {}, JSONResponse(
            {"error": "connector_url must be an http:// or https:// URL"}, status_code=400)

    weekday = body.get("weekday")
    if not isinstance(weekday, int) or isinstance(weekday, bool) or not (0 <= weekday <= 6):
        return {}, JSONResponse(
            {"error": "weekday must be 0 (Monday) through 6 (Sunday)"}, status_code=400)

    start_hour = body.get("start_hour")
    end_hour = body.get("end_hour")
    for name, v in (("start_hour", start_hour), ("end_hour", end_hour)):
        if not isinstance(v, int) or isinstance(v, bool) or not (0 <= v <= 23):
            return {}, JSONResponse({"error": "%s must be 0 through 23" % name}, status_code=400)
    if start_hour > end_hour:
        return {}, JSONResponse({"error": "start_hour must be <= end_hour"}, status_code=400)

    timezone = (body.get("timezone") or "").strip()
    try:
        ZoneInfo(timezone)
    except Exception:
        return {}, JSONResponse(
            {"error": "timezone must be a valid IANA zone name, e.g. America/Boise"},
            status_code=400)

    start_date = (body.get("start_date") or "").strip()
    if start_date:
        try:
            date.fromisoformat(start_date)
        except ValueError:
            return {}, JSONResponse(
                {"error": "start_date must be YYYY-MM-DD or empty"}, status_code=400)

    # channel/hashtag: required for the kind that uses it, forced blank
    # for the one that doesn't -- see app/db.py's checkin_net schema
    # comment for why storage keeps the unused field '' rather than
    # whatever a caller happened to send for it. corescope and beacon
    # are both channel-scoped (see app/checkin.py's module docstring
    # for why that's the right model for both); for beacon this stores
    # the channel NAME, never the instance-local numeric id BeaconClient
    # resolves it to at poll time (see that class -- the id means
    # nothing outside one Beacon instance and would silently go stale).
    channel = (body.get("channel") or "").strip()
    hashtag = (body.get("hashtag") or "").strip()
    if kind in (KIND_CORESCOPE, KIND_BEACON):
        if not channel:
            return {}, JSONResponse(
                {"error": "channel is required for a %s net" % kind}, status_code=400)
        hashtag = ""
    else:
        if not hashtag:
            return {}, JSONResponse(
                {"error": "hashtag is required for a Meshtastic net"}, status_code=400)
        channel = ""

    # mqtt-only connector config -- blank/unused for every other kind,
    # the same convention channel/hashtag above already use for the
    # kind that doesn't need them. broker_username/topic_root are plain
    # config, taken straight from the request every time. broker_password/
    # channel_key are SECRETS (see app/db.py's checkin_net comment): an
    # empty submitted value means KEEP THE EXISTING one, not "clear it"
    # -- a config screen that always echoes '' back into these inputs
    # (since GET /api/admin/checkin/nets never returns the real value,
    # see _scrub_secrets below) would otherwise silently wipe a
    # password/key on every unrelated edit to that net. `current` (the
    # existing row, None on create) is what lets a blank submission mean
    # "unchanged" -- on create there is nothing to keep, so blank stays
    # blank there regardless. clear_broker_password/clear_channel_key
    # are the explicit way to actually blank one out, since a plain
    # empty string can no longer mean that.
    broker_username = ""
    topic_root = ""
    broker_password = (current.get("broker_password", "") if current else "")
    channel_key = (current.get("channel_key", "") if current else "")
    if kind == KIND_MQTT:
        broker_username = (body.get("broker_username") or "").strip()
        topic_root = (body.get("topic_root") or "").strip().strip("/")

        if body.get("clear_broker_password") is True:
            broker_password = ""
        else:
            submitted = body.get("broker_password")
            if isinstance(submitted, str) and submitted:
                broker_password = submitted

        if body.get("clear_channel_key") is True:
            channel_key = ""
        else:
            submitted = body.get("channel_key")
            if isinstance(submitted, str) and submitted:
                channel_key = submitted.strip()

        if channel_key:
            try:
                base64.b64decode(channel_key, validate=True)
            except Exception:
                return {}, JSONResponse(
                    {"error": "channel_key must be valid base64"}, status_code=400)

    enabled = bool(body.get("enabled", True))

    return {
        "label": label, "kind": kind, "protocol": protocol, "connector_url": connector_url,
        "channel": channel, "hashtag": hashtag, "weekday": weekday,
        "start_hour": start_hour, "end_hour": end_hour, "timezone": timezone,
        "start_date": start_date, "enabled": int(enabled),
        "broker_username": broker_username, "broker_password": broker_password,
        "channel_key": channel_key, "topic_root": topic_root,
    }, None


def _scrub_secrets(net: dict) -> dict:
    """Never let a net's broker_password/channel_key leave this process
    in a JSON response, in either direction -- not GET /api/admin/checkin/nets'
    listing, and not a create/update route's own echo of what it just
    wrote (that echo used to be `**fields`, which would otherwise leak
    a password right back at the admin who just typed it, same problem,
    same fix). Replaces each with a has_* boolean; every other field
    passes through unchanged. See app/db.py's checkin_net comment and
    _validate_net_fields' docstring above for why these two columns get
    this treatment and no others do.
    """
    out = dict(net)
    out["has_broker_password"] = bool(out.pop("broker_password", ""))
    out["has_channel_key"] = bool(out.pop("channel_key", ""))
    return out


@router.get("/api/admin/checkin/nets")
async def admin_checkin_nets(request: Request):
    """Every net (enabled or not) plus the global config singleton, for
    the admin panel's check-in section. last_poll_at/last_poll_error
    come straight off each checkin_net row -- see app/checkin.py's
    CheckinPoller, which writes them after every cycle -- so a net
    that's silently failing shows up here without anyone reading logs.
    """
    guard = _api_guard(request)
    if guard is not None:
        return guard
    conn = connect()
    try:
        nets = [dict(r) for r in conn.execute(
            "SELECT * FROM checkin_net ORDER BY id").fetchall()]
        for n in nets:
            n["enabled"] = bool(n["enabled"])
        row = conn.execute("SELECT * FROM checkin_config WHERE id = 1").fetchone()
        config = dict(row) if row is not None else {}
        if config:
            config["enabled"] = bool(config["enabled"])
    finally:
        conn.close()
    # Never the raw broker_password/channel_key -- see _scrub_secrets.
    nets = [_scrub_secrets(n) for n in nets]
    return JSONResponse({"nets": nets, "config": config})


@router.post("/api/admin/checkin/nets/create")
async def admin_checkin_net_create(request: Request):
    """Add a net. See app/db.py's checkin_net table for the model: one
    row carries a connector KIND (which upstream API it speaks), a
    window, and a channel or hashtag depending on that kind -- see that
    table's own comment for the corescope/beacon/meshview asymmetry
    this deliberately preserves rather than unifies.
    """
    guard = _api_guard(request)
    if guard is not None:
        return guard
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    fields, err = _validate_net_fields(body)
    if err is not None:
        return err

    now = int(time.time())
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO checkin_net(label, kind, protocol, connector_url, channel, hashtag, "
            " weekday, start_hour, end_hour, timezone, start_date, enabled, created_at, "
            " broker_username, broker_password, channel_key, topic_root) "
            "VALUES (:label, :kind, :protocol, :connector_url, :channel, :hashtag, :weekday, "
            " :start_hour, :end_hour, :timezone, :start_date, :enabled, :created_at, "
            " :broker_username, :broker_password, :channel_key, :topic_root)",
            {**fields, "created_at": now},
        )
        net_id = cur.lastrowid
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    log.info("admin: created checkin net %d (%s, %s, %s)",
              net_id, fields["label"], fields["kind"], fields["connector_url"])
    return JSONResponse(_scrub_secrets({"id": net_id, "created_at": now, **fields}), status_code=201)


@router.post("/api/admin/checkin/nets/update")
async def admin_checkin_net_update(request: Request):
    """Update every editable field of an existing net -- same validation
    as create. Does not touch last_poll_at/last_poll_error (those are
    CheckinPoller's to write, on its own next cycle against whatever
    this update just changed).
    """
    guard = _api_guard(request)
    if guard is not None:
        return guard
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad request"}, status_code=400)
    net_id = body.get("id")
    if not isinstance(net_id, int) or isinstance(net_id, bool):
        return JSONResponse({"error": "id is required"}, status_code=400)

    conn = connect()
    try:
        # Fetched BEFORE validation, not after: _validate_net_fields
        # needs the net's EXISTING broker_password/channel_key to
        # implement "blank submission means keep the existing secret"
        # (see that function's docstring) -- there is nothing to keep
        # if this net doesn't exist, but that's caught below the same
        # way it always was, just slightly later than net_id's own type
        # check.
        existing = conn.execute("SELECT * FROM checkin_net WHERE id = ?", (net_id,)).fetchone()
        if existing is None:
            return JSONResponse({"error": "net not found"}, status_code=404)

        fields, err = _validate_net_fields(body, current=dict(existing))
        if err is not None:
            return err

        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE checkin_net SET label=:label, kind=:kind, protocol=:protocol, "
            " connector_url=:connector_url, channel=:channel, hashtag=:hashtag, "
            " weekday=:weekday, start_hour=:start_hour, end_hour=:end_hour, "
            " timezone=:timezone, start_date=:start_date, enabled=:enabled, "
            " broker_username=:broker_username, broker_password=:broker_password, "
            " channel_key=:channel_key, topic_root=:topic_root "
            " WHERE id=:id",
            {**fields, "id": net_id},
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    if not cur.rowcount:
        return JSONResponse({"error": "net not found"}, status_code=404)
    log.info("admin: updated checkin net %d", net_id)
    return JSONResponse(_scrub_secrets({"id": net_id, **fields}))


@router.post("/api/admin/checkin/nets/delete")
async def admin_checkin_net_delete(request: Request):
    """Remove a net. The caller must supply the net's exact `label` as
    confirmation -- the same player_id + display_name guard
    /api/admin/node/remove and /api/admin/player/delete already use,
    for the same reason: a stale or mistyped id must not silently
    delete the wrong net.

    Deletes only the checkin_net row. mc_checkin_award has no net_id at
    all (see app/db.py) -- a net's historical awards are keyed on
    (season, player, net_date, protocol), not on which net row produced
    them, so removing a net can never touch anyone's earned history.
    """
    guard = _api_guard(request)
    if guard is not None:
        return guard
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    net_id = body.get("id") if isinstance(body, dict) else None
    label = body.get("label") if isinstance(body, dict) else None
    if not isinstance(net_id, int) or isinstance(net_id, bool):
        return JSONResponse({"error": "id is required"}, status_code=400)
    if not isinstance(label, str) or not label:
        return JSONResponse({"error": "label is required"}, status_code=400)

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT label FROM checkin_net WHERE id = ?", (net_id,)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return JSONResponse({"error": "net not found"}, status_code=404)
        if row["label"] != label:
            conn.execute("ROLLBACK")
            return JSONResponse({"error": "label does not match"}, status_code=409)
        conn.execute("DELETE FROM checkin_net WHERE id = ?", (net_id,))
        conn.execute("COMMIT")
    finally:
        conn.close()
    log.info("admin: deleted checkin net %d (%s)", net_id, label)
    return JSONResponse({"id": net_id, "deleted": True})


@router.post("/api/admin/checkin/config")
async def admin_checkin_config_update(request: Request):
    """Update the global check-in config singleton -- whether the
    poller runs at all, points, streak bonus, and the poller's own
    timing knobs. Takes effect on the very next poll cycle, no restart:
    app/checkin.py's poller reads this table fresh every cycle, never
    settings.py, by design (see load_checkin_config).
    """
    guard = _api_guard(request)
    if guard is not None:
        return guard
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad request"}, status_code=400)

    def _num(key, kind, min_v):
        v = body.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        v = kind(v)
        if v < min_v:
            return None
        return v

    enabled = bool(body.get("enabled"))
    points = _num("points", float, 0)
    streak_bonus = _num("streak_bonus", float, 0)
    streak_bonus_max = _num("streak_bonus_max", float, 0)
    poll_interval_seconds = _num("poll_interval_seconds", int, 1)
    directory_limit = _num("directory_limit", int, 1)
    directory_refresh_seconds = _num("directory_refresh_seconds", int, 1)

    if None in (points, streak_bonus, streak_bonus_max, poll_interval_seconds,
                directory_limit, directory_refresh_seconds):
        return JSONResponse(
            {"error": "points, streak_bonus, streak_bonus_max, poll_interval_seconds, "
                      "directory_limit, and directory_refresh_seconds are all required "
                      "and must be non-negative numbers (poll_interval_seconds, "
                      "directory_limit, and directory_refresh_seconds must be at least 1)"},
            status_code=400)

    now = int(time.time())
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO checkin_config(id, enabled, points, streak_bonus, streak_bonus_max, "
            " poll_interval_seconds, directory_limit, directory_refresh_seconds, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  enabled = excluded.enabled, points = excluded.points, "
            "  streak_bonus = excluded.streak_bonus, "
            "  streak_bonus_max = excluded.streak_bonus_max, "
            "  poll_interval_seconds = excluded.poll_interval_seconds, "
            "  directory_limit = excluded.directory_limit, "
            "  directory_refresh_seconds = excluded.directory_refresh_seconds, "
            "  updated_at = excluded.updated_at",
            (int(enabled), points, streak_bonus, streak_bonus_max, poll_interval_seconds,
             directory_limit, directory_refresh_seconds, now),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    log.info("admin: checkin config updated (enabled=%s points=%s)", enabled, points)
    return JSONResponse({
        "enabled": enabled, "points": points, "streak_bonus": streak_bonus,
        "streak_bonus_max": streak_bonus_max, "poll_interval_seconds": poll_interval_seconds,
        "directory_limit": directory_limit, "directory_refresh_seconds": directory_refresh_seconds,
        "updated_at": now,
    })


def _channel_names(items: list) -> list[str]:
    """Normalize a raw channel listing (a list of bare name strings, or
    a list of objects each carrying a `name`) down to one simple list
    of names -- what the admin panel's dropdown actually wants,
    regardless of which kind's shape it came from. Anything that isn't
    one of those two shapes per-entry is silently skipped rather than
    guessed at.
    """
    names: list[str] = []
    for item in items:
        if isinstance(item, str) and item:
            names.append(item)
        elif isinstance(item, dict):
            name = item.get("name")
            if isinstance(name, str) and name:
                names.append(name)
    return names


@router.get("/api/admin/checkin/channels")
async def admin_checkin_channels(request: Request):
    """Proxy one connector's channel list, so the admin panel can offer
    a dropdown instead of a free-text channel name -- a typo here is
    invisible until the net silently never matches a message. Kind-
    aware: corescope and beacon each expose channels through a
    completely different endpoint and shape (see app/checkin.py's
    CoreScopeClient/BeaconClient), and meshview/mqtt have no channel
    concept at all (both are hashtag-scoped -- see the module
    docstring), so this always returns a clean, explicit response for
    that kind rather than an error the admin panel would have to
    special-case.

    Read-only and short-timeout: this is a person filling out a form,
    not anything the poller depends on, so a slow or dead connector
    must fail fast with a clean JSON error rather than hang the request
    or bubble up a 500.
    """
    guard = _api_guard(request)
    if guard is not None:
        return guard

    kind = (request.query_params.get("kind") or "").strip()
    if kind not in _NET_KINDS:
        return JSONResponse(
            {"error": "kind must be one of: " + ", ".join(_NET_KINDS)}, status_code=400)

    if kind in (KIND_MESHVIEW, KIND_MQTT):
        # Not an error -- both Meshtastic-family kinds are hashtag-
        # scoped, on every channel, so there is genuinely nothing to
        # list here. See the docstring above. Checked BEFORE the
        # connector_url validation below (unlike the http(s)-only kinds)
        # since an mqtt connector is an mqtt(s):// broker URL, not an
        # http(s):// one, and there is no fetch to make for either kind
        # anyway -- no reason to require a connector param at all just
        # to learn that.
        return JSONResponse({"channels": [], "applicable": False})

    connector = (request.query_params.get("connector") or "").strip().rstrip("/")
    if not (connector.startswith("http://") or connector.startswith("https://")):
        return JSONResponse(
            {"error": "connector must be an http:// or https:// URL"}, status_code=400)

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(5.0, connect=5.0),
            headers={"Accept": "application/json", "User-Agent": "meshwars/1.0"},
        ) as client:
            if kind == KIND_BEACON:
                r = await client.get("%s/api/v1/channels" % connector, params={"limit": 1000})
            else:
                r = await client.get("%s/api/channels" % connector)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return JSONResponse({"error": "could not reach connector: %s" % e}, status_code=502)

    if kind == KIND_BEACON:
        # Only keyKnown channels carry a `name` at all -- the rest are
        # unnamed and can never be typed into a net's `channel`, so
        # they are excluded here rather than offered as a dead-end
        # dropdown entry -- see BeaconClient's own comment on this.
        items = data.get("items") if isinstance(data, dict) else None
        channels: list[str] = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict) or item.get("keyKnown") is not True:
                    continue
                name = item.get("name")
                if isinstance(name, str) and name:
                    channels.append(name)
        return JSONResponse({"channels": channels, "applicable": True})

    # corescope: tolerant of shape, same reasoning
    # app/meshview_client.py's _unwrap_list applies to meshview's own
    # envelopes -- CoreScope's exact /api/channels shape is not
    # otherwise documented here, so this accepts a bare list or any of
    # the common wrapper keys rather than assuming one and returning
    # empty for the others. Normalized down to plain names either way
    # (see _channel_names) so the admin panel gets one simple shape
    # regardless of which kind it asked for.
    raw: list = []
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        for key in ("channels", "data", "results"):
            v = data.get(key)
            if isinstance(v, list):
                raw = v
                break
    return JSONResponse({"channels": _channel_names(raw), "applicable": True})


@router.get("/api/admin/notice")
async def admin_notice(request: Request):
    """The one-time update notice's current saved state, for the admin
    panel's Notice section to load into its form. Singleton row (see
    app/db.py's `notice` table) -- there is only ever one to fetch, and
    a DB that has never had one saved gets all-empty/inactive defaults
    rather than a 404, so the form just opens blank.
    """
    guard = _api_guard(request)
    if guard is not None:
        return guard

    conn = connect()
    try:
        row = conn.execute(
            "SELECT version_key, title, body, active, updated_at FROM notice WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return JSONResponse({
            "version_key": "", "title": "", "body": "", "active": False, "updated_at": None,
        })
    return JSONResponse({
        "version_key": row["version_key"],
        "title": row["title"],
        "body": row["body"],
        "active": bool(row["active"]),
        "updated_at": row["updated_at"],
    })


@router.post("/api/admin/notice")
async def admin_notice_save(request: Request):
    """Save the one-time update notice.

    Singleton row, upserted by the fixed id -- re-saving the same
    version_key updates what is already published (fixing a typo, say)
    without re-showing it to anyone who already dismissed it; only a
    NEW version_key does that, because the version_key IS the
    localStorage dismissal key the player-facing map checks (see
    app/notice_api.py's GET /api/notice and frontend/map2.js).

    All three text fields are required even to save a draft -- there is
    one form, not a partial/full distinction, and `active` is what
    actually controls whether it's live. Setting active=false is the
    "make it easy to clear" path the admin UI's quick button uses: it
    resends whatever is already saved with active flipped off, so
    retiring a notice never requires retyping it.
    """
    guard = _api_guard(request)
    if guard is not None:
        return guard
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad request"}, status_code=400)

    version_key = (body.get("version_key") or "").strip()
    title = (body.get("title") or "").strip()
    notice_body = (body.get("body") or "").strip()
    active = bool(body.get("active"))

    if not version_key or len(version_key) > 40:
        return JSONResponse(
            {"error": "version_key is required, 40 characters max"}, status_code=400)
    if not title or len(title) > 200:
        return JSONResponse({"error": "title is required, 200 characters max"}, status_code=400)
    if not notice_body or len(notice_body) > 4000:
        return JSONResponse({"error": "body is required, 4000 characters max"}, status_code=400)

    now = int(time.time())
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO notice(id, version_key, title, body, active, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  version_key = excluded.version_key, "
            "  title = excluded.title, "
            "  body = excluded.body, "
            "  active = excluded.active, "
            "  updated_at = excluded.updated_at",
            (version_key, title, notice_body, int(active), now),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    log.info("admin: notice saved (version_key=%r, active=%s)", version_key, active)
    return JSONResponse({
        "version_key": version_key,
        "title": title,
        "body": notice_body,
        "active": active,
        "updated_at": now,
    })


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
