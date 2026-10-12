"""Regions routes: the public list and boundary read endpoints the map
uses, and the admin routes for the Regions section (import from
MeshMapper, region groups). See app/regions.py for what a region is and
how square membership is kept; the ?region= filter on the board, scores
and top routes lives with those routes (app/mc_api.py, app/api.py).
"""
from __future__ import annotations

import hashlib
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import regions
from .admin_api import _log_admin_action, _role_guard
from .db import connect

log = logging.getLogger("regions_api")

router = APIRouter()


def _revalidated(request: Request, body: dict) -> Response:
    """JSON that browsers and proxies may keep but must revalidate (ETag
    plus 304), so a region fetched from the admin panel shows up on the very
    next map load while an unchanged list still costs no body."""
    raw = json.dumps(body, separators=(",", ":")).encode()
    etag = '"%s"' % hashlib.sha256(raw).hexdigest()[:32]
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=raw, media_type="application/json", headers=headers)


@router.get("/api/regions")
async def list_regions(request: Request) -> Response:
    """Every region (code, name, centre, bbox) and every group with its
    direct member regions and child groups. No polygons -- those are one
    request each, below. Small and slow-changing, so cacheable."""
    conn = connect()
    try:
        body = {"regions": regions.list_regions(conn), "groups": regions.list_groups(conn)}
    finally:
        conn.close()
    return _revalidated(request, body)


@router.get("/api/regions/{key}/boundary")
async def region_boundary(request: Request, key: str) -> Response:
    """GeoJSON (lon,lat) of one region's boundary, or for a group a
    FeatureCollection of every member region's boundary (children
    included). `key` is a region code or a group slug, case-insensitive."""
    conn = connect()
    try:
        resolved = regions.resolve(conn, key)
        if resolved is None:
            return JSONResponse({"error": "unknown region"}, status_code=404)
        body = regions.geojson_for_regions(conn, resolved.codes)
    finally:
        conn.close()
    return _revalidated(request, body)


# ---- admin --------------------------------------------------------------


@router.get("/api/admin/regions")
async def admin_regions(request: Request):
    """Regions with when each boundary was last fetched, the groups, and
    the import's status."""
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute("SELECT code, name, fetched_at FROM region ORDER BY code")]
        groups = regions.list_groups(conn)
    finally:
        conn.close()
    return JSONResponse({"regions": rows, "groups": groups, "sync": regions.sync_status()})


@router.get("/api/admin/regions/sync")
async def admin_regions_sync_status(request: Request):
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    return JSONResponse(regions.sync_status())


@router.post("/api/admin/regions/sync")
async def admin_regions_sync(request: Request):
    """Start the full MeshMapper import in the background and return at
    once; poll GET /api/admin/regions/sync for progress. 409 if one is
    already running."""
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard
    if not regions.start_import("all"):
        return JSONResponse({"error": "an update is already running"}, status_code=409)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _log_admin_action(conn, actor_account_id=session.account_id, action="regions_sync", detail="mode=all")
        conn.execute("COMMIT")
    finally:
        conn.close()
    return JSONResponse({"started": True}, status_code=202)


@router.post("/api/admin/regions/fetch")
async def admin_region_fetch(request: Request):
    """Body: {link}. Fetches one region from MeshMapper by pasted link or
    code (see regions.fetch_region) and stores it; works for a new region
    and updates an existing one in place."""
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard
    try:
        body = await request.json()
    except Exception:
        body = None
    link = body.get("link") if isinstance(body, dict) else None
    try:
        result = await regions.fetch_region(link)
    except regions.RegionError as exc:
        return JSONResponse({"error": exc.message}, status_code=exc.status)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _log_admin_action(conn, actor_account_id=session.account_id, action="region_fetch",
                          detail=f"code={result['code']} {result['new_or_updated']}")
        conn.execute("COMMIT")
    finally:
        conn.close()
    return JSONResponse(result)


async def _json_body(request: Request):
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


@router.post("/api/admin/regions/groups/create")
async def admin_group_create(request: Request):
    """Body: {name, slug?, regions: [codes], groups: [group ids]}. A blank
    slug is made from the name."""
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard
    body = await _json_body(request)
    if body is None:
        return JSONResponse({"error": "bad request"}, status_code=400)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            gid = regions.create_group(conn, body.get("name"), body.get("slug"),
                                       body.get("regions"), body.get("groups"))
            _log_admin_action(conn, actor_account_id=session.account_id, action="region_group_create",
                              detail=f"group_id={gid} name={str(body.get('name'))!r}")
            conn.execute("COMMIT")
        except regions.RegionError as exc:
            conn.execute("ROLLBACK")
            return JSONResponse({"error": exc.message}, status_code=exc.status)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        group = next(g for g in regions.list_groups(conn) if g["id"] == gid)
    finally:
        conn.close()
    return JSONResponse(group, status_code=201)


@router.post("/api/admin/regions/groups/update")
async def admin_group_update(request: Request):
    """Body: {id, name, slug?, regions, groups}. Replaces the group's
    members with exactly the lists given."""
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard
    body = await _json_body(request)
    gid = body.get("id") if body else None
    if not isinstance(gid, int) or isinstance(gid, bool):
        return JSONResponse({"error": "id is required"}, status_code=400)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            regions.update_group(conn, gid, body.get("name"), body.get("slug"),
                                 body.get("regions"), body.get("groups"))
            _log_admin_action(conn, actor_account_id=session.account_id, action="region_group_update",
                              detail=f"group_id={gid} name={str(body.get('name'))!r}")
            conn.execute("COMMIT")
        except regions.RegionError as exc:
            conn.execute("ROLLBACK")
            return JSONResponse({"error": exc.message}, status_code=exc.status)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        group = next(g for g in regions.list_groups(conn) if g["id"] == gid)
    finally:
        conn.close()
    return JSONResponse(group)


@router.post("/api/admin/regions/groups/move")
async def admin_group_move(request: Request):
    """Body: {id, direction: "up"|"down"}. Moves the group one place in the
    order the map's Region dropdown lists joined regions."""
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard
    body = await _json_body(request)
    gid = body.get("id") if body else None
    if not isinstance(gid, int) or isinstance(gid, bool):
        return JSONResponse({"error": "id is required"}, status_code=400)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            regions.move_group(conn, gid, body.get("direction"))
            _log_admin_action(conn, actor_account_id=session.account_id, action="region_group_move",
                              detail=f"group_id={gid} {body.get('direction')}")
            conn.execute("COMMIT")
        except regions.RegionError as exc:
            conn.execute("ROLLBACK")
            return JSONResponse({"error": exc.message}, status_code=exc.status)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        groups = regions.list_groups(conn)
    finally:
        conn.close()
    return JSONResponse({"groups": groups})


@router.post("/api/admin/regions/groups/delete")
async def admin_group_delete(request: Request):
    """Body: {id, name}; the exact name is the confirmation, like every
    other delete route. A deleted group also disappears from any group
    that contained it."""
    guard = await _role_guard(request)
    if isinstance(guard, JSONResponse):
        return guard
    session = guard
    body = await _json_body(request)
    gid = body.get("id") if body else None
    name = body.get("name") if body else None
    if not isinstance(gid, int) or isinstance(gid, bool):
        return JSONResponse({"error": "id is required"}, status_code=400)
    if not isinstance(name, str) or not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT name FROM region_group WHERE id = ?", (gid,)).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return JSONResponse({"error": "group not found"}, status_code=404)
            if row["name"] != name:
                conn.execute("ROLLBACK")
                return JSONResponse({"error": "name does not match"}, status_code=409)
            regions.delete_group(conn, gid)
            _log_admin_action(conn, actor_account_id=session.account_id, action="region_group_delete",
                              detail=f"group_id={gid} name={name!r}")
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
    finally:
        conn.close()
    return JSONResponse({"id": gid, "deleted": True})
