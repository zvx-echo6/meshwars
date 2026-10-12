"""Player requests to group neighbouring regions ("Join neighboring
regions" on the map) and the admin side that approves or declines them.

A request is stored first and mailed second (to settings.
regions_request_email through app/email_login.py's shell); a missing SMTP
configuration or a failed send is logged and never fails the request.
Approving creates the group through app/regions.py's create_group(), so it
gets exactly the validation groups made by hand get.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from . import email_login, regions
from .admin_api import _log_admin_action, _role_guard
from .config import settings
from .db import connect
from .sessions import SessionPrincipal, require_session

log = logging.getLogger("region_requests")

router = APIRouter()

DAILY_LIMIT = 3
WINDOW_SECONDS = 86400
MAX_NAME = 80
MAX_NOTE = 1000
MAX_REGIONS = 50
_EMAIL_TIMEOUT_S = 8.0
_BOISE = ZoneInfo("America/Boise")


def _clean_request(body) -> tuple[str, list[str], str]:
    if not isinstance(body, dict):
        raise regions.RegionError("Bad request")
    name = body.get("group_name")
    if not isinstance(name, str) or not name.strip():
        raise regions.RegionError("Name the group.")
    name = name.strip()
    if len(name) > MAX_NAME:
        raise regions.RegionError(f"The group name can be at most {MAX_NAME} characters.")
    raw = body.get("regions")
    if not isinstance(raw, list) or any(not isinstance(c, str) for c in raw):
        raise regions.RegionError("Pick at least 2 regions.")
    codes = sorted({c.strip().upper() for c in raw if c.strip()})
    if len(codes) < 2:
        raise regions.RegionError("Pick at least 2 different regions.")
    if len(codes) > MAX_REGIONS:
        raise regions.RegionError(f"Pick at most {MAX_REGIONS} regions.")
    note = body.get("note") or ""
    if not isinstance(note, str):
        raise regions.RegionError("Bad note.")
    note = note.strip()
    if len(note) > MAX_NOTE:
        raise regions.RegionError(f"The note can be at most {MAX_NOTE} characters.")
    return name, codes, note


def _boise_time(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=_BOISE).strftime("%-d %B %Y, %-I:%M %p %Z")


def _admin_url() -> str:
    base = (settings.oauth_public_base_url or "https://meshwars.com").rstrip("/")
    return f"{base}/admin#regions"


def request_details(conn, row) -> dict:
    """Everything an operator needs to judge a request, from a
    region_join_request row."""
    codes = json.loads(row["region_codes"])
    names = {r["code"]: r["name"] for r in conn.execute(
        "SELECT code, name FROM region WHERE code IN (%s)" % ",".join("?" * len(codes)), codes)} if codes else {}
    player = conn.execute("SELECT display_name, team FROM player WHERE player_id = ?",
                          (row["player_id"],)).fetchone()
    email = conn.execute("SELECT contact_email FROM account WHERE account_id = ?",
                         (row["account_id"],)).fetchone()
    addr = email["contact_email"] if email and email["contact_email"] else None
    if not addr:
        ident = conn.execute(
            "SELECT email FROM account_identity WHERE account_id = ? AND email IS NOT NULL "
            "ORDER BY email_verified DESC, linked_at LIMIT 1", (row["account_id"],)).fetchone()
        addr = ident["email"] if ident else None
    return {
        "id": row["id"],
        "group_name": row["group_name"],
        "regions": [{"code": c, "name": names.get(c, "")} for c in codes],
        "note": row["note"],
        "player": player["display_name"] if player else None,
        "team": player["team"] if player else None,
        "email": addr,
        "status": row["status"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
        "decided_by": row["decided_by"],
        "group_id": row["group_id"],
    }


def _mail_parts(d: dict) -> tuple[str, str, str]:
    rows = [
        ("Group name", d["group_name"]),
        ("Regions", ", ".join(f"{r['code']} {r['name']}".strip() for r in d["regions"])),
        ("Requested by", f"{d['player'] or '?'} ({d['team'] or 'no team'})"),
        ("Account email", d["email"] or "none on file"),
        ("Note", d["note"] or "-"),
        ("Submitted", _boise_time(d["created_at"])),
    ]
    text = "\n".join(f"{k}: {v}" for k, v in rows) + f"\n\nReview it: {_admin_url()}\n"
    cells = "".join(
        f'<tr><td style="padding:4px 12px 4px 0; vertical-align:top; color:#888888; white-space:nowrap;">'
        f'{html.escape(k)}</td><td style="padding:4px 0; vertical-align:top;">'
        f'{html.escape(v).replace(chr(10), "<br>")}</td></tr>'
        for k, v in rows)
    details_html = f'<table role="presentation" cellpadding="0" cellspacing="0" border="0">{cells}</table>'
    return f"Region group request: {d['group_name']}", text, details_html


async def _notify(d: dict) -> None:
    """Mail the request to the operator. Never raises."""
    to = (settings.regions_request_email or "").strip()
    subject, text, details_html = _mail_parts(d)
    if not to or not settings.smtp_host:
        log.info("region request %s stored; email not configured, not sent. %s", d["id"], text.replace("\n", " | "))
        return
    try:
        await asyncio.wait_for(
            email_login.send_region_request_email(to, subject=subject, text=text,
                                                  details_html=details_html, cta_url=_admin_url()),
            _EMAIL_TIMEOUT_S)
    except Exception:
        log.exception("region request %s stored; the notification mail failed", d["id"])


@router.post("/api/regions/join-request")
async def join_request(request: Request, session: SessionPrincipal = Depends(require_session)):
    """Body: {group_name, regions: [codes], note?}. Any signed-in player;
    3 per player per rolling 24 hours."""
    if session.player_id is None:
        return JSONResponse({"error": "Link a player to your account to send a request."}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = None
    try:
        name, codes, note = _clean_request(body)
    except regions.RegionError as exc:
        return JSONResponse({"error": exc.message}, status_code=exc.status)
    now = int(time.time())
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            player = conn.execute("SELECT disabled_at FROM player WHERE player_id = ?",
                                  (session.player_id,)).fetchone()
            if player is None or player["disabled_at"] is not None:
                conn.execute("ROLLBACK")
                return JSONResponse({"error": "Your player cannot send requests."}, status_code=403)
            found = {r[0] for r in conn.execute(
                "SELECT code FROM region WHERE code IN (%s)" % ",".join("?" * len(codes)), codes)}
            missing = [c for c in codes if c not in found]
            if missing:
                conn.execute("ROLLBACK")
                return JSONResponse({"error": "Unknown region: " + ", ".join(missing)}, status_code=400)
            used = conn.execute(
                "SELECT COUNT(*) FROM region_join_request WHERE player_id = ? AND created_at > ?",
                (session.player_id, now - WINDOW_SECONDS)).fetchone()[0]
            if used >= DAILY_LIMIT:
                conn.execute("ROLLBACK")
                return JSONResponse({"error": f"You can send {DAILY_LIMIT} requests a day."}, status_code=429)
            cur = conn.execute(
                "INSERT INTO region_join_request(player_id, account_id, group_name, region_codes, note, "
                "status, created_at) VALUES (?,?,?,?,?, 'pending', ?)",
                (session.player_id, session.account_id, name, json.dumps(codes), note, now))
            row = conn.execute("SELECT * FROM region_join_request WHERE id = ?", (cur.lastrowid,)).fetchone()
            details = request_details(conn, row)
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
    finally:
        conn.close()
    await _notify(details)
    return JSONResponse({"ok": True, "id": details["id"]}, status_code=201)


# ---- admin ----------------------------------------------------------------


@router.get("/api/admin/regions/requests")
async def admin_requests(request: Request):
    """Pending requests first (oldest first), then decided ones, newest
    first, capped."""
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM region_join_request ORDER BY (status = 'pending') DESC, "
            "CASE WHEN status = 'pending' THEN created_at END ASC, created_at DESC LIMIT 100").fetchall()
        out = [request_details(conn, r) for r in rows]
        pending = conn.execute("SELECT COUNT(*) FROM region_join_request WHERE status = 'pending'").fetchone()[0]
    finally:
        conn.close()
    return JSONResponse({"requests": out, "pending": pending})


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


@router.post("/api/admin/regions/requests/approve")
async def admin_request_approve(request: Request):
    """Body: {id, group_name?}. Creates the group (the requested name, or
    group_name when the admin edited it after a slug collision) with the
    requested regions and marks the request approved. A validation error
    (slug taken, ...) leaves the request pending."""
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard
    body = await _body(request)
    rid = body.get("id")
    if not isinstance(rid, int) or isinstance(rid, bool):
        return JSONResponse({"error": "id is required"}, status_code=400)
    now = int(time.time())
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM region_join_request WHERE id = ?", (rid,)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return JSONResponse({"error": "request not found"}, status_code=404)
            if row["status"] != "pending":
                conn.execute("ROLLBACK")
                return JSONResponse({"error": "That request was already decided."}, status_code=409)
            name = body.get("group_name") if isinstance(body.get("group_name"), str) and body["group_name"].strip() \
                else row["group_name"]
            try:
                gid = regions.create_group(conn, name, None, json.loads(row["region_codes"]), [], now=now)
            except regions.RegionError as exc:
                conn.execute("ROLLBACK")
                return JSONResponse({"error": exc.message}, status_code=exc.status)
            conn.execute(
                "UPDATE region_join_request SET status = 'approved', decided_at = ?, decided_by = ?, group_id = ? "
                "WHERE id = ?", (now, session.account_id, gid, rid))
            _log_admin_action(conn, actor_account_id=session.account_id, action="region_request_approve",
                              detail=f"request_id={rid} group_id={gid}", now=now)
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
    finally:
        conn.close()
    return JSONResponse({"id": rid, "status": "approved", "group_id": gid})


@router.post("/api/admin/regions/requests/decline")
async def admin_request_decline(request: Request):
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard
    body = await _body(request)
    rid = body.get("id")
    if not isinstance(rid, int) or isinstance(rid, bool):
        return JSONResponse({"error": "id is required"}, status_code=400)
    now = int(time.time())
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT status FROM region_join_request WHERE id = ?", (rid,)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return JSONResponse({"error": "request not found"}, status_code=404)
            if row["status"] != "pending":
                conn.execute("ROLLBACK")
                return JSONResponse({"error": "That request was already decided."}, status_code=409)
            conn.execute("UPDATE region_join_request SET status = 'declined', decided_at = ?, decided_by = ? "
                         "WHERE id = ?", (now, session.account_id, rid))
            _log_admin_action(conn, actor_account_id=session.account_id, action="region_request_decline",
                              detail=f"request_id={rid}", now=now)
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
    finally:
        conn.close()
    return JSONResponse({"id": rid, "status": "declined"})
