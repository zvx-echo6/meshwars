"""Public endpoint for client-side failure reports.

frontend/map2.js's map never rendered on real mobile hardware (see that
file's module docstring near sendClientLog/showMapErrorBanner) and had
no error handling at all, so every failure mode collapsed into the same
silent "Loading map..." hang. That file now catches everything it can
(a thrown map constructor, map.on('error'), a load timeout,
webglcontextlost) plus the page's own window.onerror/unhandledrejection,
and reports each one here instead of just logging to a console nobody
on a phone can see.

This is a public, unauthenticated, state-nothing endpoint reachable from
the open internet -- same threat model as app/join_api.py's /api/join --
so it is defensive the same way:

- Rate-limited per client IP (settings.clientlog_rate_limit_attempts/
  _window_seconds), same in-process bounded-dict limiter shape as
  app/join_api.py's _rate_limited / app/checkin_api.py's, so a flood
  from one address can't be used to fill the log.
- The raw request body is capped (_MAX_BODY_BYTES) before it is ever
  parsed, so an oversized POST can't bloat memory or the log.
- Every field is treated as hostile: coerced to a string, stripped of
  newlines/control characters (so a payload can't forge additional log
  lines -- CWE-117), and length-capped (_MAX_FIELD_CHARS) before it
  reaches logging.warning().
- Nothing here ever raises past the route -- worst case is a 4xx
  response. There is no persistence beyond the log line: no table, no
  admin view, nothing that could double as a way to store player data
  under a different name. Only what the server already has (the
  request's own client address and User-Agent header) plus whatever
  short, sanitized strings the page sent -- no coordinates, no tokens,
  nothing that identifies a player beyond the address every other route
  already sees.
"""
from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .client_ip import get_client_ip
from .config import settings

router = APIRouter()
log = logging.getLogger("clientlog")

# ---- rate limiting -------------------------------------------------
#
# Same bounded-dict shape as app/join_api.py's _attempts / _rate_limited:
# sweep stale entries once the tracked-address count gets large, and
# only clear the whole structure if that alone doesn't bring it back
# under the cap.
_RATE_LIMIT_MAX_TRACKED = 10000
_attempts: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    # See app/client_ip.py's module docstring: this used to be
    # request.client.host directly, which is always the Caddy reverse
    # proxy's own address in every deployment, not the real caller's.
    return get_client_ip(request)


def _rate_limited(ip: str) -> bool:
    """True if `ip` has used up its report budget for the current
    window. Records this attempt (by timestamp) when allowed."""
    now = time.monotonic()
    window = settings.clientlog_rate_limit_window_seconds
    limit = settings.clientlog_rate_limit_attempts

    if len(_attempts) >= _RATE_LIMIT_MAX_TRACKED:
        stale = [
            k for k, times in _attempts.items()
            if not times or now - times[-1] >= window
        ]
        for k in stale:
            del _attempts[k]
        if len(_attempts) >= _RATE_LIMIT_MAX_TRACKED:
            _attempts.clear()

    times = [t for t in _attempts.get(ip, []) if now - t < window]
    if len(times) >= limit:
        _attempts[ip] = times
        return True
    times.append(now)
    _attempts[ip] = times
    return False


# ---- body / field caps -----------------------------------------------
#
# The whole payload is a handful of short strings; there is no
# legitimate reason for it to be large. Checked against the raw body
# before any JSON parsing happens.
_MAX_BODY_BYTES = 4096
_MAX_FIELD_CHARS = 500
_MAX_KIND_CHARS = 64
_MAX_UA_CHARS = 200


def _clean(value: object, max_len: int) -> str:
    """Coerce to a short, single-line, printable string. Every field
    here is attacker-controlled input headed straight into a log file:
    control characters (including \\r/\\n, which could otherwise forge
    additional log lines -- CWE-117) are stripped, then the result is
    capped to `max_len` characters."""
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)
    cleaned = "".join(ch for ch in text if ch == " " or ch >= "\x20")
    return cleaned.strip()[:max_len]


@router.post("/api/clientlog")
async def clientlog(request: Request) -> JSONResponse:
    """Best-effort client failure report -- logged at WARNING, never
    persisted. Always returns quickly and never raises; the frontend's
    own reporter (map2.js's sendClientLog) is written to match: it must
    never let a failed report become a second error either."""
    ip = _client_ip(request)
    if _rate_limited(ip):
        return JSONResponse({"ok": False}, status_code=429)

    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        return JSONResponse({"ok": False, "error": "payload too large"}, status_code=413)

    data: object = {}
    if body:
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            data = {}
    if not isinstance(data, dict):
        data = {}

    kind = _clean(data.get("kind"), _MAX_KIND_CHARS) or "unknown"
    message = _clean(data.get("message"), _MAX_FIELD_CHARS)
    href = _clean(data.get("href"), _MAX_FIELD_CHARS)
    ua = _clean(request.headers.get("user-agent"), _MAX_UA_CHARS)

    log.warning("client report kind=%r href=%r ua=%r message=%r", kind, href, ua, message)
    return JSONResponse({"ok": True})
