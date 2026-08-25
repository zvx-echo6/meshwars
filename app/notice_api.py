"""Player-facing read route for the one-time update notice authored in
the admin panel (app/admin_ops.py's admin_notice/admin_notice_save).

See app/db.py's `notice` table docstring for the storage model: a
single row (id fixed to 1), upserted in place -- there is only ever one
current notice, not a history of past ones. This route is that row's
entire public surface: a single, cheap point read against a one-row
table, with no join and no season/protocol scoping.

Deliberately its own tiny route rather than folded into GET /config:
/config already runs several queries (active season, map center, team
tallies, winner banner) on every open map tab's polling cycle, and a
notice most players will fetch exactly once (dismiss, and never fetch
again for that version_key) does not belong bundled into that repeating
payload. frontend/map2.js fires this off without gating first paint on
it -- see that file's main(), which calls it without awaiting.

Not authenticated, same as /get-nodes, /scores, and every other route
the site's own pages call: this is public content, not player data.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .db import connect

router = APIRouter()


@router.get("/api/notice")
async def active_notice() -> JSONResponse:
    """The current notice, if the operator has published one.

    `{"notice": null}` when there is none (no row yet, or the row's
    `active` flag is off) -- the frontend's whole contract is "render
    nothing and don't ask again this load" for that shape, so an absent
    notice costs a visitor one small always-empty response, never a
    missing-content error.
    """
    conn = connect()
    try:
        row = conn.execute(
            "SELECT version_key, title, body FROM notice WHERE id = 1 AND active = 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return JSONResponse({"notice": None})
    return JSONResponse({"notice": dict(row)})
