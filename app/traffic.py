"""Server-side traffic analytics for meshwars.com: page views, unique
visitors, and new-visitor counts for the admin panel (GET
/api/admin/traffic in app/admin_api.py).

This is deliberately NOT a third-party analytics script (no Google
Analytics, no Plausible, nothing that ships a request off this server)
-- it is a single ASGI middleware (TrafficMiddleware, below) that
records a row for a qualifying request, and a handful of aggregate
SQL queries an admin route reads back. See frontend/privacy.html's
"Site traffic" section for the plain-English version of everything
below.

---- identity: a salted, truncated, one-way hash --------------------------

A visitor is never identified by their real IP address or their raw
User-Agent string -- neither is stored anywhere, in any table, in any
form. Instead every qualifying request is reduced to:

    sha256(salt + client_ip + user_agent).hexdigest()[:16]

`salt` is settings.traffic_salt (app/config.py) if an operator set one,
or an automatically generated one persisted in the `cursor` table (see
_get_salt() below) -- see that setting's own comment in app/config.py
for the full reasoning on why an empty value here means "generate and
remember one," not "off," and why the salt is deliberately never
rotated. Without the salt, a 16-hex-character hash space is small
enough that anyone could precompute the hash for every plausible
(ip, user_agent) pair and match a stored hash straight back to a real
person -- the salt is what makes that precomputation infeasible, since
it is never written to the repository or shipped with the code.

---- what gets counted -----------------------------------------------------

Only real page loads: GET requests, a response status under 400, and a
response Content-Type starting with "text/html". That single rule is
what keeps the map's own polling API calls, static assets, tiles, and
fonts out of the numbers, without having to hand-maintain a path
blocklist that would drift out of date the moment a new route is added.

A bot's page load is still recorded (site_visitor.is_bot=1,
site_visit_day still gets a row for it) -- it is not silently dropped --
but it is walled off from every HUMAN-facing count: site_path_daily and
site_referrer_daily are only ever incremented for a non-bot hit, and
every aggregate app/admin_api.py's traffic route reports (today.views,
today.uniques, today.new_visitors, and the same three inside `daily`)
excludes bot hits by construction, joining through site_visitor.is_bot.
Bot traffic surfaces only as its own separate `bot_views` figure. See
_is_bot_user_agent() for the exact classifier.

---- this module must never break a request --------------------------------

TrafficMiddleware.dispatch() always calls call_next() and always
returns whatever it got back, unconditionally -- the try/except lives
entirely around the RECORDING step, after the real response already
exists. A bug in this module (a locked database, a schema mismatch
after a bad migration, anything) must turn into a debug log line and a
silently uncounted page view, never a 500 for a player who did nothing
wrong.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .client_ip import get_client_ip
from .config import settings
from .db import WriteSession, get_cursor

log = logging.getLogger("traffic")

# The key this feature's generated salt is stored under in the generic
# `cursor` key-value table (app/db.py) -- the same table app/ingest.py's
# own polling cursor already uses for "one durable string this app needs
# to remember across restarts." Not a dedicated table: a single string
# does not need one, and reusing `cursor` means no new schema concept is
# needed just to persist it.
_SALT_CURSOR_KEY = "traffic_salt"

# Resolved once per process and cached here -- see _get_salt(). Every
# request after the first reuses this instead of touching the database
# again; only the very first request (or the first after a restart)
# pays for the cursor-table read (and, on a truly fresh install, the
# one-time write).
_salt_cache: str | None = None

# Case-insensitive keywords that mark a User-Agent as a bot/crawler/
# monitor rather than a real browser. Matched as a plain substring
# search (re.search, not a full match) so "Googlebot/2.1",
# "Mozilla/5.0 (compatible; bingbot/2.0...)", and "python-requests/2.31"
# all match on their own distinguishing word without needing a pattern
# per known crawler. Deliberately broad rather than an exhaustive named
# list of every crawler that exists -- new bots show up constantly, and
# nearly all of them advertise themselves honestly in their own
# User-Agent (there is no reputational reason for a well-behaved crawler
# to hide), so a keyword net catches the overwhelming majority without
# needing to be maintained.
_BOT_KEYWORDS = (
    "bot",
    "crawl",
    "spider",
    "slurp",
    "bingpreview",
    "headless",
    "curl",
    "wget",
    "python-requests",
    "facebookexternalhit",
    "uptime",
    "monitor",
)
_BOT_RE = re.compile("|".join(re.escape(k) for k in _BOT_KEYWORDS), re.IGNORECASE)

# At most once per process per this many seconds does a qualifying
# request also run the retention prune -- same "_maybe_housekeeping,
# gated on a monotonic interval, riding along on ordinary traffic
# instead of a dedicated scheduled task" shape app/mc_ingest.py's
# McIngestor already uses for its own retention sweep (see
# _HOUSEKEEPING_INTERVAL_S there). This app has no cron/scheduler of its
# own to hook into for a brand-new periodic job -- see
# TrafficMiddleware's own docstring for why riding the request path was
# the deliberate choice here instead of adding one.
_PRUNE_INTERVAL_S = 24 * 60 * 60  # once a day

# How long a visitor hash (site_visitor) and a day's worth of per-visitor
# rows (site_visit_day) are kept before prune_stale_traffic() deletes
# them -- see that function's own docstring, and frontend/privacy.html's
# "Site traffic" section, which quotes this same number to visitors.
_RETENTION_DAYS = 90


def _utc_today() -> str:
    """Today's UTC calendar date, as the 'YYYY-MM-DD' string every
    traffic table stores in its `day` column. UTC, not
    settings.checkin_net_timezone/local time, the same reasoning
    app/db.py's SCHEMA comment for these tables gives: this is a public
    website with visitors in every timezone, not a single region's
    weekly net.
    """
    return datetime.now(timezone.utc).date().isoformat()


def _is_bot_user_agent(user_agent: str) -> bool:
    """True when `user_agent` matches _BOT_KEYWORDS. An empty/missing
    User-Agent is NOT treated as a bot -- plenty of real, if unusual,
    browser configurations send no UA at all, and there is nothing in
    an absent header that positively identifies automation the way a
    keyword match does.
    """
    return bool(user_agent) and _BOT_RE.search(user_agent) is not None


def _hash_visitor(salt: str, ip: str, user_agent: str) -> str:
    """sha256(salt + ip + user_agent), truncated to 16 hex characters --
    see this module's own docstring for the full reasoning. Truncating
    to 16 characters (64 bits) is a deliberate space/collision trade:
    this is an aggregate visitor counter, not a security credential, and
    64 bits of a cryptographic digest is astronomically collision-safe
    for any traffic volume this app will ever see, while keeping the
    stored key short.
    """
    digest = hashlib.sha256(f"{salt}{ip}{user_agent}".encode("utf-8")).hexdigest()
    return digest[:16]


def _get_salt(conn) -> str:
    """Resolve the salt used by _hash_visitor(): settings.traffic_salt
    if an operator set one, otherwise a generated value persisted in the
    `cursor` table -- see settings.traffic_salt's own comment in
    app/config.py for why empty means "generate and remember," not
    "off."

    Race-safe against two workers/processes starting at once against a
    fresh database: the INSERT below is `ON CONFLICT(k) DO NOTHING`, so
    if two processes both generate a candidate salt and both try to
    write it, exactly one write wins and the loser's own candidate is
    simply discarded -- the immediately following read-back returns
    whichever value actually landed, which is the same value every
    process will keep seeing from then on, regardless of whose
    candidate it was.
    """
    global _salt_cache
    if _salt_cache is not None:
        return _salt_cache

    if settings.traffic_salt:
        _salt_cache = settings.traffic_salt
        return _salt_cache

    existing = get_cursor(conn, _SALT_CURSOR_KEY, "")
    if existing:
        _salt_cache = existing
        return _salt_cache

    candidate = secrets.token_hex(32)
    conn.execute(
        "INSERT INTO cursor(k, v) VALUES (?, ?) ON CONFLICT(k) DO NOTHING",
        (_SALT_CURSOR_KEY, candidate),
    )
    resolved = get_cursor(conn, _SALT_CURSOR_KEY, candidate)
    _salt_cache = resolved
    return resolved


def _normalize_referrer(raw_referer: str, own_host: str | None) -> str | None:
    """The Referer header, reduced to scheme+host only (e.g.
    "https://old-rival-site.com") -- never the full URL, which can carry
    a path and query string that leak what a visitor was doing on the
    SENDING site (somebody else's visitor to protect, not just noise to
    strip here).

    Returns None (nothing recorded) for: no header at all, a header
    that fails to parse as an absolute URL (no scheme or no host -- a
    bare path like "/join", which a misbehaving client can legally
    send, is not a referring SITE), and a referrer whose host matches
    `own_host` -- a self-referral (this deployment linking to itself)
    is not information this feature exists to report.
    """
    if not raw_referer:
        return None
    try:
        parsed = urlsplit(raw_referer)
    except ValueError:
        return None
    if not parsed.scheme or not parsed.hostname:
        return None
    if own_host and parsed.hostname.lower() == own_host.lower():
        return None
    return f"{parsed.scheme}://{parsed.hostname}"


def _record_visit(
    conn,
    *,
    day: str,
    visitor_hash: str,
    is_bot: bool,
    path: str,
    referrer: str | None,
) -> None:
    """The four writes one qualifying page view produces. Called with a
    connection already inside a write transaction (TrafficMiddleware
    opens one via WriteSession per request) -- this function itself
    issues no BEGIN/COMMIT.
    """
    # first_seen is only ever set by the INSERT branch (a brand new
    # visitor_hash); the ON CONFLICT branch deliberately never touches
    # it, so it always reads back as this visitor's actual first day,
    # no matter how many hits follow. is_bot is refreshed on every hit
    # (rather than left as whatever the first hit decided) so a future
    # change to _BOT_KEYWORDS can reclassify an existing hash the next
    # time it is seen, instead of being stuck with a stale classification
    # forever.
    conn.execute(
        "INSERT INTO site_visitor(visitor_hash, first_seen, last_seen, hits, is_bot) "
        "VALUES (?, ?, ?, 1, ?) "
        "ON CONFLICT(visitor_hash) DO UPDATE SET "
        "  last_seen = excluded.last_seen, "
        "  hits = hits + 1, "
        "  is_bot = excluded.is_bot",
        (visitor_hash, day, day, int(is_bot)),
    )

    # One row per (day, visitor), with a per-day hit count riding along
    # in `views` -- see this table's own SCHEMA comment in app/db.py for
    # why that column exists: it is what lets a day's total page views
    # AND total bot page views both be read back from this one table
    # (joined to site_visitor.is_bot), not just its unique-visitor count.
    conn.execute(
        "INSERT INTO site_visit_day(day, visitor_hash, views) VALUES (?, ?, 1) "
        "ON CONFLICT(day, visitor_hash) DO UPDATE SET views = views + 1",
        (day, visitor_hash),
    )

    # Path/referrer breakdowns are human-only -- see their own SCHEMA
    # comments in app/db.py for why a bot hit is deliberately excluded
    # here even though it is fully recorded two statements above.
    if is_bot:
        return

    conn.execute(
        "INSERT INTO site_path_daily(day, path, views) VALUES (?, ?, 1) "
        "ON CONFLICT(day, path) DO UPDATE SET views = views + 1",
        (day, path),
    )
    if referrer:
        conn.execute(
            "INSERT INTO site_referrer_daily(day, referrer, views) VALUES (?, ?, 1) "
            "ON CONFLICT(day, referrer) DO UPDATE SET views = views + 1",
            (day, referrer),
        )


def prune_stale_traffic(conn, today: str | None = None) -> tuple[int, int]:
    """Delete traffic rows older than _RETENTION_DAYS (90 days). Called
    with a connection already inside a write transaction (see
    TrafficMiddleware._maybe_record()'s own opportunistic-once-a-day
    call, and this module's own docstring for why that ride-along shape
    was chosen over a dedicated scheduled task).

    Deletes site_visit_day rows and site_visitor rows exactly as asked
    (see this feature's own spec: "site_visit_day rows older than 90
    days" and "site_visitor rows whose last_seen is older than 90
    days"), and ALSO prunes site_path_daily/site_referrer_daily on the
    same cutoff -- not explicitly called out separately, but without it
    those two tables would grow forever, one row per (day, path) or
    (day, referrer) ever seen, which defeats the entire point of having
    a retention policy on everything else this feature writes.

    Returns (removed_visit_days, removed_visitors) -- rowcount from the
    two deletes the spec named, for the caller to log.
    """
    if today is None:
        today = _utc_today()
    cutoff = (
        datetime.strptime(today, "%Y-%m-%d").date() - timedelta(days=_RETENTION_DAYS)
    ).isoformat()

    cur_days = conn.execute("DELETE FROM site_visit_day WHERE day < ?", (cutoff,))
    removed_visit_days = cur_days.rowcount

    cur_visitors = conn.execute(
        "DELETE FROM site_visitor WHERE last_seen < ?", (cutoff,)
    )
    removed_visitors = cur_visitors.rowcount

    conn.execute("DELETE FROM site_path_daily WHERE day < ?", (cutoff,))
    conn.execute("DELETE FROM site_referrer_daily WHERE day < ?", (cutoff,))

    return removed_visit_days, removed_visitors


def build_traffic_report(conn, days: int = 30) -> dict:
    """The full payload GET /api/admin/traffic (app/admin_api.py)
    returns. `days` is clamped to [1, 365] here (not left to the route)
    so this function is safe to call with any input a test or a future
    caller hands it.

    Shape (see app/admin_api.py's own route docstring for the field-by-
    field contract the frontend depends on):

        {
          "today":  {"views": int, "uniques": int, "new_visitors": int, "bot_views": int},
          "daily":  [{"day": "YYYY-MM-DD", "views": int, "uniques": int,
                      "new_visitors": int, "bot_views": int}, ...],  # ascending, one entry per day in the window, zero-filled
          "top_paths":     [{"path": str, "views": int}, ...],       # up to 10, human hits only
          "top_referrers": [{"referrer": str, "views": int}, ...],   # up to 10, human hits only
          "since": "YYYY-MM-DD",  # earliest day this deployment has ANY recorded data
        }

    `today` is always exactly the `daily` entry for today's date -- it
    is never computed a second, separate way that could drift from it.
    """
    days = max(1, min(365, days))
    today = _utc_today()
    start_day = (
        datetime.strptime(today, "%Y-%m-%d").date() - timedelta(days=days - 1)
    ).isoformat()

    since_row = conn.execute("SELECT MIN(day) AS d FROM site_visit_day").fetchone()
    since = since_row["d"] if since_row and since_row["d"] else today

    # One query for the whole window: for every (day, is_bot) pair that
    # has at least one site_visit_day row in range, the total views that
    # day, how many distinct visitors that day, and how many of those
    # visitors' GLOBAL first-ever day (first_day, not window-limited --
    # a visitor first seen before the window must still not count as
    # "new" on a later day inside it) equals this day.
    rows = conn.execute(
        """
        WITH first_day AS (
            SELECT visitor_hash, MIN(day) AS first_day
              FROM site_visit_day
             GROUP BY visitor_hash
        )
        SELECT svd.day AS day,
               sv.is_bot AS is_bot,
               SUM(svd.views) AS views,
               COUNT(*) AS uniques,
               SUM(CASE WHEN svd.day = fd.first_day THEN 1 ELSE 0 END) AS new_visitors
          FROM site_visit_day svd
          JOIN site_visitor sv ON sv.visitor_hash = svd.visitor_hash
          JOIN first_day fd ON fd.visitor_hash = svd.visitor_hash
         WHERE svd.day >= ? AND svd.day <= ?
         GROUP BY svd.day, sv.is_bot
        """,
        (start_day, today),
    ).fetchall()

    _zero = {"views": 0, "uniques": 0, "new_visitors": 0}
    by_day: dict[str, dict[int, dict[str, int]]] = {}
    for r in rows:
        by_day.setdefault(r["day"], {})[int(r["is_bot"])] = {
            "views": r["views"] or 0,
            "uniques": r["uniques"] or 0,
            "new_visitors": r["new_visitors"] or 0,
        }

    daily = []
    cursor_date = datetime.strptime(start_day, "%Y-%m-%d").date()
    end_date = datetime.strptime(today, "%Y-%m-%d").date()
    while cursor_date <= end_date:
        d = cursor_date.isoformat()
        human = by_day.get(d, {}).get(0, _zero)
        bot = by_day.get(d, {}).get(1, _zero)
        daily.append({
            "day": d,
            "views": human["views"],
            "uniques": human["uniques"],
            "new_visitors": human["new_visitors"],
            "bot_views": bot["views"],
        })
        cursor_date += timedelta(days=1)

    # `today` in the response is always the window's own last entry --
    # the window always ends on `today` by construction above, so this
    # is a lookup, not a second computation.
    today_entry = daily[-1] if daily else {**_zero, "bot_views": 0}

    top_paths = conn.execute(
        "SELECT path, SUM(views) AS views FROM site_path_daily "
        " WHERE day >= ? AND day <= ? GROUP BY path ORDER BY views DESC LIMIT 10",
        (start_day, today),
    ).fetchall()
    top_referrers = conn.execute(
        "SELECT referrer, SUM(views) AS views FROM site_referrer_daily "
        " WHERE day >= ? AND day <= ? GROUP BY referrer ORDER BY views DESC LIMIT 10",
        (start_day, today),
    ).fetchall()

    return {
        "today": {
            "views": today_entry["views"],
            "uniques": today_entry["uniques"],
            "new_visitors": today_entry["new_visitors"],
            "bot_views": today_entry["bot_views"],
        },
        "daily": daily,
        "top_paths": [{"path": p["path"], "views": p["views"]} for p in top_paths],
        "top_referrers": [
            {"referrer": r["referrer"], "views": r["views"]} for r in top_referrers
        ],
        "since": since,
    }


class TrafficMiddleware(BaseHTTPMiddleware):
    """Counts a page view for every qualifying request. Registered in
    app/main.py BEFORE (see that file's own comment) GZipMiddleware and
    CORSMiddleware are added, which -- because Starlette wraps
    middleware in the reverse of add_middleware() call order, last
    added ends up outermost -- makes this the INNERMOST middleware,
    closest to the router. That means it sees the response exactly as
    the route handler produced it: the real status code and the real,
    uncompressed Content-Type, before GZipMiddleware's own wrapping ever
    touches it. Nothing here depends on that ordering for correctness
    (GZip never changes Content-Type, only adds Content-Encoding), but
    it is the more obviously correct place to sit, and it means this
    middleware's own BaseHTTPMiddleware body-buffering happens before
    compression, not after.

    See this module's own docstring for the "must never break a
    request" contract dispatch() below implements.
    """

    def __init__(self, app):
        super().__init__(app)
        # Per-instance, not module-global: Starlette constructs exactly
        # one TrafficMiddleware for the life of the process, the same
        # single-instance-holds-its-own-timer shape McIngestor uses for
        # its own _last_housekeeping (app/mc_ingest.py).
        self._last_prune = 0.0

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        try:
            await self._maybe_record(request, response)
        except Exception:
            # See this module's own docstring: a failure here must
            # never surface to the caller. debug, not exception/error --
            # an occasional missed page view is expected background
            # noise (a locked database under load, say), not an
            # operator-facing incident.
            log.debug("traffic: failed to record page view", exc_info=True)
        return response

    async def _maybe_record(self, request: Request, response: Response) -> None:
        if request.method != "GET":
            return
        if response.status_code >= 400:
            return
        content_type = response.headers.get("content-type", "")
        # This one rule is the entire filter -- see this module's own
        # docstring for why a content-type check was chosen over a
        # maintained path blocklist.
        if not content_type.lower().startswith("text/html"):
            return

        ip = get_client_ip(request)
        user_agent = request.headers.get("user-agent", "")
        is_bot = _is_bot_user_agent(user_agent)
        referrer = _normalize_referrer(
            request.headers.get("referer", ""), request.url.hostname
        )
        path = request.url.path
        day = _utc_today()

        now_mono = time.monotonic()
        do_prune = (now_mono - self._last_prune) >= _PRUNE_INTERVAL_S

        # Same global write lock/transaction shape every other write in
        # this app already goes through (app/db.py's WriteSession) --
        # page loads are low-rate compared to the API polling the
        # content-type rule above already excludes, so a plain
        # synchronous write here, once per qualifying request, is cheap
        # enough with no queue or batching needed.
        async with WriteSession() as conn:
            salt = _get_salt(conn)
            visitor_hash = _hash_visitor(salt, ip, user_agent)
            _record_visit(
                conn,
                day=day,
                visitor_hash=visitor_hash,
                is_bot=is_bot,
                path=path,
                referrer=referrer,
            )
            if do_prune:
                removed_days, removed_visitors = prune_stale_traffic(conn, day)
                log.info(
                    "traffic: pruned %d stale site_visit_day rows, %d stale site_visitor rows",
                    removed_days, removed_visitors,
                )

        if do_prune:
            self._last_prune = now_mono
