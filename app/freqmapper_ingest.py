"""Polling loop: fetch Meshtastic coverage events from FreqMapper and
paint the sender's grid cell through the shared MeshCore-model scoring
path (app/mc_scoring.py) -- for REGISTERED Meshtastic players only, same
registration gate app/ingest.py enforces for meshview.

FreqMapper is a third-party, independently-operated Meshtastic
coverage-mapping service, entirely separate from meshview. As of the
2026-09-08 API revision it exposes three read-only endpoints, all under
the same key and the same rate limit:

    GET /api/v1/integrations/coverage-events    combined, PREFERRED (this module)
    GET /api/v1/integrations/verified-coverage  TX-only (the old feed this module used before)
    GET /api/v1/integrations/received-coverage  RX-only

This module polls ONLY the combined feed (COMBINED_EVENTS_PATH below).
It carries both evidence types in one paginated stream, ordered by
`published_at`, with one opaque `next_cursor` covering both:

    Header: Authorization: Bearer <key>
    Params: limit (1-1000, default 500), cursor (opaque)

...returning a page of events oldest first. Each event's `event_type` is
either "verified_tx" (an independently-watcher-verified transmission --
exactly what the old TX-only feed reported, one event per verification)
or "passive_rx" (a wardriving radio hearing a packet a Watcher also
reported nearby in time, with no live equivalent on the old feed). This
module scores verified_tx exactly as it always has. passive_rx is
counted in the poll-cycle stats (skipped_passive_rx) and its event_id is
recorded in freqmapper_verification for future-proofing, but nothing
about it is ever painted -- passive RX scoring is a separate, not-yet-
made decision (see _process_one_event below). Any event_type this module
does not recognize (a future addition to the feed) is likewise counted
(skipped_unknown_event_type) and deduped, never painted, and never
allowed to crash the poll loop -- FreqMapper's own migration guidance
for this feed explicitly anticipates new evidence types arriving over
time.

watcher_count: a verified_tx event now reports how many independent
Watchers verified it (same_region_watcher_count / cross_region_watcher_count
break it down further). The old TX-only feed never reported this at all
("coverage_rule": "independent_watcher_verified" was as specific as it
got) -- that used to be true, and is the reason every verified event was
worth a flat points_per_event with no way to weight it. It is no longer
true. See _verified_tx_points() below for how this module now optionally
scales points by watcher_count -- OFF by default, see that function's
own docstring for why the default has to be exactly flat, unchanged
scoring.

occurred_at vs. published_at: every event carries both. occurred_at is
the RF event's own timestamp (a mapping-test send, or a radio
reception) -- this is what this module uses as the paint timestamp and
what feeds the paint_from date gate below. published_at is the feed's
own server-side ordering time, which the opaque cursor is built around;
FreqMapper's docs are explicit that it must never be read as when the
RF event itself happened, and this module never does -- see _event_time()
below, which does not even look at published_at. The old feed's
verified_at/mapping_test_sent_at field names remain a fallback for an
event that somehow still carries only those (never expected against the
live combined feed today, but cheap insurance against a schema
regression), tried strictly after occurred_at.

event_id and the dedupe migration: the combined feed's dedupe key is
`event_id`, itself prefixed by event type -- "verified_tx:<uuid>" or
"passive_rx:<uuid>" -- and FreqMapper's docs confirm that for a
verified_tx event the UUID half is the exact same value the old feed
called `verification_id`. This module used to dedupe on that bare
verification_id; freqmapper_verification's primary key is now `event_id`
and holds the prefixed form for every event type. app/db.py's
_migrate_freqmapper_verification_event_id() is the one-time migration
that rewrites every row already on disk from the old bare form to
"verified_tx:" + itself (the only event type the old feed could ever
have written) before renaming the column -- see that function's own
docstring in app/db.py for the full reasoning. Without it, the combined
feed replaying this deployment's entire history (which a cleared cursor
does deliberately -- see clear-cursor's own docstring below) would see
every historical event as brand new under its newly-prefixed key and
re-paint/re-score all of it.

Why no auto-bind: app/mc_ingest.py auto-registers a MeshCore radio's
first wardriving ping, because that ping was pushed BY the radio's owner
using their own API key -- the act of submitting it already proves
consent. FreqMapper is the opposite: it reports on ANY radio it happens
to observe on the network, radios this deployment's players may or may
not actually own. Auto-binding an observed radio to whoever happens to
be watching would let someone else's hardware silently start scoring
points for a stranger. A node has to already be registered through the
ordinary join flow (app/join_api.py) before FreqMapper evidence about it
counts -- an unregistered radio's events are skipped and counted, never
used to register anything.

Rate limiting and error handling (see _fetch_page below): the combined
feed documents its rate limit via X-RateLimit-Limit/-Remaining/-Reset
headers on every response (default 120 requests per 60s window, but this
module reads the actual limit off the response rather than assuming
that), a 429 with a Retry-After header when exceeded, and a fixed set of
client errors (401/403/404/422) that must never be retried in a tight
loop. None of that existed against the old feed, which this module
handled with an ad hoc "back off on undocumented 403" rule. That rule is
gone; _fetch_page below implements the documented contract instead:
429 backs off for at least Retry-After seconds without advancing the
cursor, 5xx/network failures retry the SAME request and cursor with
increasing backoff (5s/10s/20s/60s, FreqMapper's own recommended
schedule), and 401/403/404/422 are surfaced to freqmapper_config's
last_poll_error and back off without a tight retry loop. The cursor is
only ever persisted after every event on a page has been fully
processed and written -- see _poll_once below -- so a failure partway
through a page, at any layer, leaves the next poll resuming that exact
page rather than skipping or double-processing it.

Config is DB-backed (app/db.py's freqmapper_config singleton), not
settings.py -- see load_freqmapper_config below, read FRESH on every
poll cycle so an admin edit through app/admin_ops.py's /api/admin/paint
takes effect on the very next cycle, no restart. settings.py's
freqmapper_*/mt_paint_source fields still exist (app/config.py) and are
never deleted: they are the seed source seed_freqmapper_config_from_env
uses to populate this table's row on first boot, and the fallback
load_freqmapper_config returns if that row is somehow missing.

mt_paint_source is the single switch that decides which source(s) are
currently allowed to paint the Meshtastic board -- see that column's
own comment in app/db.py, and app/ingest.py, whose position-packet poll
and backfill read the same DB value (via load_freqmapper_config, not
settings.mt_paint_source) to gate themselves off when it is
"freqmapper" alone. Three values: "meshview" (only app/ingest.py
scores), "freqmapper" (only this module scores), or "both" (both
score, each exactly as if it were the sole selected source -- this is
the default; see that column's comment in app/config.py for why two
sources touching the same cell needs no arbitration here, since
app/mc_scoring.py's cooldown/capture-window machinery already absorbs
it). This module's poll loop keeps running (and keeps deduping on
event_id) whenever freqmapper_config.enabled is true REGARDLESS
of mt_paint_source -- only the final score/write (mc_scoring.apply_paint
+ the player_cell_ping insert) is gated on it being "freqmapper" or
"both". That means an operator can watch FreqMapper's own poll-cycle
log lines (painted vs. skipped_inactive_source) before ever flipping
the switch, and flipping it later never replays history: every event
this loop has already seen is already recorded in
freqmapper_verification by then.

Unlike before this table existed, run_forever() is started
UNCONDITIONALLY by app/main.py and never exits early just because
enabled is currently off -- the loop itself checks
freqmapper_config.enabled fresh on every cycle and simply does nothing
when it is off, the same shape app/checkin.py's CheckinPoller already
uses for checkin_config.enabled. The loop has to always be running for
`enabled` to be a true runtime toggle: a task that already returned at
startup would never notice an admin flipping it back on later.

paint_from is checkin_net.start_date's exact contract, one level up:
a local YYYY-MM-DD lower bound on a verified_tx event's occurred_at,
blank meaning BLOCK EVERY EVENT rather than "no lower bound" -- see that
column's comment in app/db.py and _process_one_event's date-gate below
for the full reasoning. Unlike every other skip reason this loop tracks,
a date-skipped event is deliberately left OUT of freqmapper_verification,
so moving the date earlier and clearing the cursor can still recover
it; the cursor itself still advances past it regardless, or the poller
would never get past its own too-early backlog. This gate only ever
applies to verified_tx events -- passive_rx and unknown event types
never score regardless of date, so there is nothing for the gate to
protect there.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from . import mc_scoring
from .config import settings
from .db import WriteSession, connect, get_cursor, set_cursor
from .grid import cell_id, in_play_area, valid_coord
from .node_ref import normalize_node_ref
from .place_scoring import credit_places

log = logging.getLogger("freqmapper_ingest")

PROTOCOL = "mt"

# The combined feed this module polls -- see this module's docstring.
# Named here rather than inlined at the one call site so it is never
# typed twice (and never drifts) across the request and any log/error
# message that mentions it.
COMBINED_EVENTS_PATH = "/api/v1/integrations/coverage-events"

EVENT_TYPE_VERIFIED_TX = "verified_tx"
EVENT_TYPE_PASSIVE_RX = "passive_rx"

# The OLD TX-only feed's cursor key. No longer written by this module --
# see COMBINED_CURSOR_KEY below -- but deliberately never deleted or
# reused for the new cursor either: a cursor issued by one feed is not
# valid on another (this module's own docstring, and FreqMapper's docs),
# so overwriting this value would make "roll back to the code that reads
# app/db.py's old CURSOR_KEY" silently resume from the wrong place, or
# error outright. Kept purely as a rollback safety net.
CURSOR_KEY = "freqmapper_next_cursor"

# The combined feed's own cursor -- a distinct key/value row in
# app/db.py's generic `cursor` table, independent of CURSOR_KEY above.
COMBINED_CURSOR_KEY = "freqmapper_combined_next_cursor"

_MIN_LIMIT = 1
_MAX_LIMIT = 1000

# FreqMapper's own recommended retry schedule for a network failure or a
# 5xx response: retry the identical request (same params, same cursor)
# after waiting this many seconds, increasing each time, up to this many
# attempts. Never touches the cursor -- see _fetch_page below -- so a
# page that fails even after exhausting this schedule is simply not
# fetched this cycle, and the very same request is tried again next
# cycle (or sooner, if a 429 cooldown does not also apply).
_NETWORK_BACKOFF_SCHEDULE = (5, 10, 20, 60)

# Retry-After is documented as seconds-to-wait, never an HTTP-date, but
# this is the fallback if a 429 response is somehow missing the header
# or carries something this can't parse as a plain integer -- staying
# defensive here costs nothing and keeps a malformed header from ever
# turning into "retry immediately."
_DEFAULT_RETRY_AFTER_S = 30

# How long a non-retryable client error (401/403/404/422 -- see
# _fetch_page) suppresses further requests before trying again. A flat
# multiple of the poll interval rather than a fixed number: a deployment
# that has tuned its poll interval up or down already has an opinion
# about how "polite" polling should be, and this backoff should scale
# with that opinion rather than fight it. (Formerly used for the old
# feed's undocumented 403 throttling; repurposed here for the new feed's
# documented hard-error codes, which need the exact same "don't hammer
# it" treatment.)
_THROTTLE_BACKOFF_MULTIPLE = 2

# HTTP statuses FreqMapper's docs say must not be retried without first
# correcting the key, host, or region -- see _fetch_page below.
_NO_RETRY_STATUS_CODES = frozenset({401, 403, 404, 422})
_STATUS_HINTS = {
    401: "key is absent, expired, or revoked",
    403: "active region is outside this key's assigned scope",
    404: "unknown or inactive region code",
    422: "invalid cursor or parameter",
}

# X-RateLimit-Remaining is worth a log line once it gets low, so an
# operator notices a connector about to get throttled before it
# actually happens. Read against the response's own X-RateLimit-Limit
# rather than a hard-coded number -- FreqMapper's docs describe the
# default window as 120 requests/60s, but this module must not assume
# that never changes.
_RATE_LIMIT_LOW_FRACTION = 0.1

# How often verification-id housekeeping runs, at most -- same
# "at most once an hour" cadence app/mc_ingest.py's McIngestor uses for
# its own retention sweep, for the same reason: cheap enough to run
# often, but there is no benefit to running it every single poll cycle.
_HOUSEKEEPING_INTERVAL_S = 3600

# freqmapper_verification only has to survive long enough to dedupe
# across a restart or an overlapping page -- FreqMapper's own cursor
# already makes re-fetching the same event on a later, ordinary poll
# unlikely, so this window only has to cover "the process was down for a
# while and resumes from a slightly stale cursor," not weeks of history.
# Generous margin over that, in the same spirit as app/mc_ingest.py's
# mc_ping_retention_hours default.
_VERIFICATION_RETENTION_HOURS = 72


def _parse_iso_ts(raw: object) -> int | None:
    """An ISO 8601 timestamp string -> epoch seconds, or None if it
    isn't one. FreqMapper's own examples carry an explicit UTC offset
    ("...+00:00"), not a bare "Z" -- datetime.fromisoformat handles that
    natively -- but a defensive "Z" -> "+00:00" swap is kept anyway
    since app/meshview_client.py's own timestamp parsing does the same,
    and it costs nothing when the suffix is already an offset.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _event_time(event: dict) -> int | None:
    """The RF event time for one combined-feed event: occurred_at first
    -- the feed's own RF-event timestamp, guaranteed on every event type
    -- falling back to the old TX-only feed's verified_at, then its
    mapping_test_sent_at, purely as insurance against an event that
    somehow still carries only those legacy field names (never expected
    against the live combined feed). published_at is the feed's own
    cursor-ordering time, not an RF event time, and is deliberately never
    consulted here under any fallback -- see this module's docstring.
    """
    for key in ("occurred_at", "verified_at", "mapping_test_sent_at"):
        ts = _parse_iso_ts(event.get(key))
        if ts is not None:
            return ts
    return None


def _clamped_limit(page_limit: int) -> int:
    return max(_MIN_LIMIT, min(int(page_limit), _MAX_LIMIT))


def _local_date(ts: int) -> str:
    """Local calendar date (YYYY-MM-DD) for an epoch-seconds timestamp,
    for comparing against freqmapper_config.paint_from -- see that
    column's comment in app/db.py and _process_one_event below for the
    gate this feeds. Uses settings.checkin_net_timezone, the same
    app-wide local zone app/results.py's month rolls (_tz()) and
    app/place_rotation.py's day rollover already reuse rather than
    anything checkin-specific: FreqMapper is one connector, not many
    nets, so there is no more specific zone to key off, and reusing this
    one keeps "today" meaning the same calendar day everywhere in the
    app rather than drifting between UTC and local depending on which
    module you're reading.
    """
    return datetime.fromtimestamp(ts, tz=ZoneInfo(settings.checkin_net_timezone)).date().isoformat()


def _parse_retry_after(raw: object) -> int:
    """A 429 response's Retry-After header -> whole seconds to wait,
    defaulting to _DEFAULT_RETRY_AFTER_S when the header is missing or
    not a plain integer -- see that constant's own comment.
    """
    if raw is None:
        return _DEFAULT_RETRY_AFTER_S
    try:
        seconds = int(str(raw).strip())
    except ValueError:
        return _DEFAULT_RETRY_AFTER_S
    return max(seconds, 0)


def _verified_tx_points(
    watcher_count: object,
    points_per_event: float,
    watcher_weight_enabled: bool,
    watcher_weight_base: float,
    watcher_weight_increment: float,
    watcher_weight_cap: float,
) -> float:
    """How many points one verified_tx event is worth.

    NEUTRAL BY DEFAULT -- this is the load-bearing property of this
    function, not an incidental one: when watcher_weight_enabled is
    false (freqmapper_config's default, set by both its CREATE TABLE and
    its MIGRATIONS ADD COLUMN entry in app/db.py -- see that column
    group's own comment), this ALWAYS returns points_per_event
    unchanged, regardless of what watcher_count says, so deploying this
    feature changes NO player's score until an operator explicitly turns
    it on. The exact same flat points_per_event is returned when
    weighting IS enabled but watcher_count is missing, null, or not a
    usable positive integer -- "we don't know how many watchers verified
    this" must fall back to the same flat value every event got before
    this feature existed, never to zero points (a missing count is not
    evidence of zero watchers) and never to watcher_weight_base either
    (that would silently assume exactly one watcher we can't actually
    confirm).

    When enabled and watcher_count is a usable positive integer: the
    first watcher is worth watcher_weight_base, each additional watcher
    adds watcher_weight_increment, and the total is capped at
    watcher_weight_cap (a cap of 0 or less disables the cap) so one
    very-watched transmission cannot dominate a whole season's scoring
    the way an uncapped linear scale could.
    """
    if not watcher_weight_enabled:
        return points_per_event
    if isinstance(watcher_count, bool) or not isinstance(watcher_count, (int, float)):
        return points_per_event
    watchers = int(watcher_count)
    if watchers < 1:
        return points_per_event
    points = watcher_weight_base + (watchers - 1) * watcher_weight_increment
    if watcher_weight_cap > 0:
        points = min(points, watcher_weight_cap)
    return points


def load_freqmapper_config(conn) -> dict:
    """Fresh, uncached read of the freqmapper_config singleton --
    connector settings, scoring knobs, mt_paint_source, and the poller's
    own last-poll status. Read on every poll cycle (FreqMapperIngestor's
    run_forever/_poll_once) and by app/ingest.py's meshview position/
    backfill gate (which only needs mt_paint_source out of this), and by
    every admin route that needs the current numbers (app/admin_ops.py)
    -- never cached anywhere in the process. Exactly the pattern
    app/checkin.py's load_checkin_config uses for checkin_config, for
    the same reason: an admin edit through /api/admin/paint must take
    effect on the very next poll, not after a restart.

    Falls back to config.py's original settings if the row is somehow
    missing (a database whose migrations have not run yet) rather than
    raising -- defensive, since app/db.py's MIGRATIONS seeds this row
    unconditionally and it should always be there in practice, but a
    poll cycle failing outright over a missing config row would be a
    worse failure mode than briefly falling back to the settings this
    row was itself seeded from. watcher_weight_* has no settings.py
    counterpart (it is a brand new feature with no prior env-var
    configuration to fall back to -- see that column group's own
    comment in app/db.py), so this fallback hardcodes the same neutral
    values the real column defaults already give a fresh or freshly
    migrated database.
    """
    row = conn.execute(
        "SELECT mt_paint_source, enabled, base_url, api_key, poll_interval_seconds, "
        "       page_limit, points_per_event, unique_painter_bonus, paint_from, "
        "       last_poll_at, last_poll_error, updated_at, "
        "       watcher_weight_enabled, watcher_weight_base, "
        "       watcher_weight_increment, watcher_weight_cap "
        "  FROM freqmapper_config WHERE id = 1"
    ).fetchone()
    if row is None:
        return {
            "mt_paint_source": settings.mt_paint_source,
            "enabled": settings.freqmapper_enabled,
            "base_url": settings.freqmapper_base_url,
            "api_key": settings.freqmapper_api_key,
            "poll_interval_seconds": settings.freqmapper_poll_interval_seconds,
            "page_limit": settings.freqmapper_page_limit,
            "points_per_event": settings.freqmapper_points_per_event,
            "unique_painter_bonus": settings.freqmapper_unique_painter_bonus,
            "paint_from": settings.freqmapper_paint_from,
            "last_poll_at": None,
            "last_poll_error": None,
            "updated_at": 0,
            "watcher_weight_enabled": False,
            "watcher_weight_base": 0.5,
            "watcher_weight_increment": 0.1,
            "watcher_weight_cap": 1.0,
        }
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    d["watcher_weight_enabled"] = bool(d["watcher_weight_enabled"])
    return d


def seed_freqmapper_config_from_env(conn) -> None:
    """One-time bootstrap, called from app/db.py's init_db() on every
    startup: populates the freqmapper_config singleton with exactly what
    settings.py already describes, the same guarded-by-updated_at
    pattern app/checkin.py's seed_nets_from_env uses for checkin_config
    (see that function's docstring for the full reasoning). Only fires
    while updated_at is still 0 -- app/db.py's MIGRATIONS already
    guarantees the row exists (bare column defaults) by the time this
    ever runs, so this is an UPDATE, not an INSERT, and an operator's
    later edit through /api/admin/paint (which always sets updated_at to
    the current time) can never be silently overwritten by a later boot.

    Deliberately reads settings rather than anything already in the
    database: those env vars are the only place today's production
    values exist before this function ever runs, and after it runs once
    they are never consulted again for FreqMapper configuration -- see
    load_freqmapper_config above, which reads the database fresh on
    every cycle, never settings. The net effect is that deploying this
    changes NO behavior: same source, same connector, same scoring, just
    moved from env-var-and-restart to database-and-admin-API.

    Does not touch watcher_weight_* -- there is no settings.py
    counterpart to seed from (see that column group's own comment in
    app/db.py), and its schema/MIGRATIONS default (disabled) is already
    exactly the neutral value this bootstrap would otherwise be trying
    to reproduce.
    """
    row = conn.execute("SELECT updated_at FROM freqmapper_config WHERE id = 1").fetchone()
    if row is None or row["updated_at"] != 0:
        return
    conn.execute(
        "UPDATE freqmapper_config SET mt_paint_source = ?, enabled = ?, base_url = ?, "
        " api_key = ?, poll_interval_seconds = ?, page_limit = ?, points_per_event = ?, "
        " unique_painter_bonus = ?, paint_from = ?, updated_at = ? WHERE id = 1",
        (
            settings.mt_paint_source,
            int(settings.freqmapper_enabled),
            settings.freqmapper_base_url,
            settings.freqmapper_api_key,
            settings.freqmapper_poll_interval_seconds,
            settings.freqmapper_page_limit,
            settings.freqmapper_points_per_event,
            settings.freqmapper_unique_painter_bonus,
            settings.freqmapper_paint_from,
            int(time.time()),
        ),
    )
    log.info(
        "freqmapper: seeded config from settings (enabled=%s, paint_source=%s)",
        settings.freqmapper_enabled, settings.mt_paint_source,
    )


def _load_registered_players(conn) -> dict[str, tuple[int, str]]:
    """node_ref (bare lowercase 8-hex) -> (player_id, team) for every
    active Meshtastic player. Identical query and reasoning to
    app/ingest.py's own _load_registered_players -- loaded once per poll
    cycle rather than once per event, and disabled players are excluded
    by the JOIN so a disabled player's radio reads as unregistered here
    too. Kept as its own copy rather than imported from app/ingest.py's
    Ingestor: that method is bound to an Ingestor instance and this
    module has no dependency on that class otherwise.
    """
    rows = conn.execute(
        "SELECT pn.node_ref, pn.player_id, p.team "
        "  FROM player_node pn "
        "  JOIN player p ON p.player_id = pn.player_id "
        " WHERE pn.protocol = ? AND p.disabled_at IS NULL",
        (PROTOCOL,),
    ).fetchall()
    return {r["node_ref"]: (r["player_id"], r["team"]) for r in rows}


class FreqMapperIngestor:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._client: httpx.AsyncClient | None = None
        # The base_url/api_key the current self._client was actually
        # built with -- compared against the freshly loaded config on
        # every cycle so an admin editing the connector rebuilds the
        # client instead of quietly continuing to talk to the old host
        # or key. See _ensure_client below.
        self._client_base_url: str | None = None
        self._client_api_key: str | None = None
        # Cooldown state shared by the 429 and hard-error (401/403/404/
        # 422) paths in _fetch_page: a monotonic deadline before which
        # _poll_once skips fetching entirely, and a flag so the warning
        # is logged once per cooldown episode rather than every time it
        # is (re-)checked. Reset to False on every successful response.
        self._retry_after = 0.0
        self._throttle_warned = False
        self._last_housekeeping = 0.0
        # Logged only the first time, and again only when the value
        # actually flips -- an admin toggling FreqMapper on/off or
        # switching mt_paint_source is worth a log line every cycle
        # finding the same value again is not. None until the first
        # cycle ever looks, so the initial state is always logged once.
        self._last_logged_enabled: bool | None = None
        self._last_logged_paint_source: str | None = None
        # Injectable so tests can replace real waiting with an instant,
        # call-recording stand-in rather than actually sleeping through
        # FreqMapper's own backoff schedule (up to 95 real seconds) --
        # see _fetch_page's retry loop below, the only place this is
        # used.
        self._sleep = asyncio.sleep

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        # Started UNCONDITIONALLY by app/main.py and never exits early
        # just because config is currently off -- see this module's
        # docstring. `enabled` now lives in freqmapper_config and has to
        # be a true runtime toggle, which only works if this loop stays
        # alive to notice a later flip; _poll_once reloads the config
        # fresh every cycle and simply does nothing while enabled is
        # off.
        log.info("freqmapper ingest loop starting (config is DB-backed, read fresh every cycle)")
        try:
            while not self._stop.is_set():
                # Fallback only for the pathological case _poll_once
                # can't even reach a config read (e.g. the database
                # itself is unavailable) -- ordinarily replaced by the
                # freshly loaded poll_interval_seconds it returns.
                interval = settings.freqmapper_poll_interval_seconds
                try:
                    interval = await self._poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("freqmapper ingest cycle failed")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=max(interval, 1))
                except asyncio.TimeoutError:
                    pass
        finally:
            if self._client is not None:
                await self._client.aclose()
                self._client = None
        log.info("freqmapper ingest loop stopped")

    async def _ensure_client(self, cfg: dict) -> None:
        """(Re)build the pooled httpx client when the connector settings
        it was built with have drifted from the freshly loaded config --
        an admin editing base_url or api_key through /api/admin/paint
        must reach the very NEXT poll, not require a restart, the same
        no-restart contract every other DB-backed setting here gets. A
        no-op the overwhelmingly common case where nothing has changed
        since the last cycle (config is read every cycle regardless of
        whether anyone actually touched it).
        """
        if (self._client is not None
                and self._client_base_url == cfg["base_url"]
                and self._client_api_key == cfg["api_key"]):
            return
        if self._client is not None:
            await self._client.aclose()
        self._client = httpx.AsyncClient(
            base_url=cfg["base_url"].rstrip("/"),
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={
                "Accept": "application/json",
                "User-Agent": "meshwars/1.0",
                "Authorization": f"Bearer {cfg['api_key']}",
            },
        )
        self._client_base_url = cfg["base_url"]
        self._client_api_key = cfg["api_key"]

    def _log_rate_limit_headers(self, r: httpx.Response) -> None:
        """Log a warning once a response reports the rate-limit budget
        is running low -- read against that SAME response's own
        X-RateLimit-Limit, never a hard-coded 120, since FreqMapper's
        docs only describe that as today's default, not a guarantee.
        Silently does nothing if either header is absent or unparseable
        -- this is diagnostic logging, not a correctness requirement.
        """
        try:
            remaining = int(r.headers.get("X-RateLimit-Remaining", ""))
            limit = int(r.headers.get("X-RateLimit-Limit", ""))
        except ValueError:
            return
        if limit <= 0:
            return
        if remaining <= max(1, int(limit * _RATE_LIMIT_LOW_FRACTION)):
            log.warning(
                "freqmapper: rate limit budget low (%d/%d remaining this window)",
                remaining, limit,
            )

    async def _fetch_page(self, cfg: dict, cursor: str | None) -> dict | None:
        """Fetch one page of COMBINED_EVENTS_PATH, handling the
        documented rate-limit and error contract (see this module's
        docstring) so _poll_once never has to think about retries or
        cooldowns itself.

        Returns the parsed JSON response body on success. Returns None
        if this cycle has nothing to process -- a 429 or hard-error
        cooldown just started, or the network/5xx retry budget was
        exhausted -- in every None case self._retry_after and/or
        freqmapper_config.last_poll_error have already been updated as
        appropriate, and the caller never sees a next_cursor to persist,
        so the cursor is left exactly where it was.
        """
        params: dict = {"limit": _clamped_limit(cfg["page_limit"])}
        if cursor:
            params["cursor"] = cursor

        for attempt in range(len(_NETWORK_BACKOFF_SCHEDULE) + 1):
            try:
                r = await self._client.get(COMBINED_EVENTS_PATH, params=params)
            except (httpx.HTTPError, httpx.TimeoutException) as e:
                if attempt < len(_NETWORK_BACKOFF_SCHEDULE):
                    delay = _NETWORK_BACKOFF_SCHEDULE[attempt]
                    log.warning(
                        "freqmapper: request failed (%s) -- retrying same request in %ds "
                        "(attempt %d/%d)",
                        e, delay, attempt + 1, len(_NETWORK_BACKOFF_SCHEDULE),
                    )
                    await self._sleep(delay)
                    continue
                log.warning("freqmapper: request failed after exhausting retries: %s", e)
                await self._record_error(str(e))
                return None

            self._log_rate_limit_headers(r)

            if r.status_code == 429:
                retry_after = _parse_retry_after(r.headers.get("Retry-After"))
                if not self._throttle_warned:
                    log.warning(
                        "freqmapper: rate limited (429) -- backing off for at least %ds",
                        retry_after,
                    )
                    self._throttle_warned = True
                self._retry_after = time.monotonic() + retry_after
                # Deliberately NOT recorded as last_poll_error: being
                # rate limited under normal polling is documented,
                # expected upstream behavior, not a broken connector --
                # an operator reading the admin status panel should not
                # see this as something broken.
                return None

            if r.status_code in _NO_RETRY_STATUS_CODES:
                msg = f"HTTP {r.status_code} from FreqMapper ({_STATUS_HINTS[r.status_code]})"
                if not self._throttle_warned:
                    log.warning("freqmapper: %s -- backing off, not retrying in a tight loop", msg)
                    self._throttle_warned = True
                self._retry_after = (
                    time.monotonic() + cfg["poll_interval_seconds"] * _THROTTLE_BACKOFF_MULTIPLE
                )
                await self._record_error(msg)
                return None

            self._throttle_warned = False

            if r.status_code >= 500:
                if attempt < len(_NETWORK_BACKOFF_SCHEDULE):
                    delay = _NETWORK_BACKOFF_SCHEDULE[attempt]
                    log.warning(
                        "freqmapper: upstream error %d -- retrying same request in %ds "
                        "(attempt %d/%d)",
                        r.status_code, delay, attempt + 1, len(_NETWORK_BACKOFF_SCHEDULE),
                    )
                    await self._sleep(delay)
                    continue
                msg = f"HTTP {r.status_code} from FreqMapper after exhausting retries"
                log.warning("freqmapper: %s", msg)
                await self._record_error(msg)
                return None

            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as e:
                log.warning("freqmapper: unexpected upstream error: %s", e)
                await self._record_error(str(e))
                return None

            try:
                data = r.json()
            except ValueError:
                log.warning("freqmapper: response was not valid JSON")
                await self._record_error("response was not valid JSON")
                return None

            if not isinstance(data, dict):
                log.warning("freqmapper: response body was not a JSON object")
                await self._record_error("response body was not a JSON object")
                return None

            schema_version = data.get("schema_version")
            if schema_version != 1:
                # Not fatal -- logged so a future FreqMapper schema bump
                # is noticed quickly, but the response is still
                # processed on the assumption its shape is close enough.
                log.warning(
                    "freqmapper: unexpected schema_version=%r (expected 1)", schema_version
                )

            return data

        return None  # unreachable -- the loop above always returns

    async def _poll_once(self) -> int:
        """One ingest cycle. Returns the poll interval (seconds) to
        sleep before the next one -- read fresh from the config every
        time (app/checkin.py's CheckinPoller._poll_once follows the same
        shape for the same reason), so tightening or loosening it from
        the admin panel takes effect on the very next sleep, not after a
        restart.
        """
        conn = connect()
        try:
            cfg = load_freqmapper_config(conn)
            cursor = get_cursor(conn, COMBINED_CURSOR_KEY, "") or None
        finally:
            conn.close()

        if cfg["enabled"] != self._last_logged_enabled:
            log.info(
                "freqmapper: enabled=%s (key configured=%s)",
                cfg["enabled"], bool(cfg["api_key"]),
            )
            self._last_logged_enabled = cfg["enabled"]
        if cfg["mt_paint_source"] != self._last_logged_paint_source:
            log.info(
                "freqmapper: mt_paint_source=%s (%s)",
                cfg["mt_paint_source"],
                "FreqMapper is painting" if cfg["mt_paint_source"] != "meshview"
                else "FreqMapper is NOT painting -- events are still processed and deduped",
            )
            self._last_logged_paint_source = cfg["mt_paint_source"]

        # Empty means off, same contract every other secret setting in
        # this app uses (admin_token, mc_checkin_base_url) -- a blank key
        # must never be read as "authenticate with nothing."
        if not cfg["enabled"] or not cfg["api_key"]:
            return cfg["poll_interval_seconds"]

        await self._ensure_client(cfg)

        now_mono = time.monotonic()
        if now_mono < self._retry_after:
            # Still cooling down from a recent 429 or hard error -- see
            # _fetch_page. Nothing to fetch this cycle; no request is
            # even attempted, so there is nothing new to log either.
            return cfg["poll_interval_seconds"]

        data = await self._fetch_page(cfg, cursor)
        if data is None:
            return cfg["poll_interval_seconds"]

        events = data.get("events")
        if not isinstance(events, list):
            events = []
        next_cursor = data.get("next_cursor")
        has_more = bool(data.get("has_more"))

        if not events:
            # An empty page can still hand back a next_cursor at the live
            # edge -- worth persisting so the next poll starts from there
            # rather than re-requesting the same empty tail -- but never
            # overwrite an already-persisted cursor with a blank one.
            if next_cursor:
                async with WriteSession() as wconn:
                    set_cursor(wconn, COMBINED_CURSOR_KEY, next_cursor)
            await self._maybe_housekeeping()
            await self._record_ok()
            return cfg["poll_interval_seconds"]

        now_ts = int(time.time())
        counts = {
            "painted": 0, "skipped_duplicate": 0, "skipped_unregistered": 0,
            "skipped_bad_coord": 0, "skipped_out_of_area": 0,
            "skipped_malformed": 0, "skipped_inactive_source": 0,
            "skipped_before_paint_from": 0, "skipped_passive_rx": 0,
            "skipped_unknown_event_type": 0, "error": 0,
        }

        async with WriteSession() as wconn:
            # Season bookkeeping, same reasoning as app/ingest.py's own
            # poll/backfill: WRITES mc_season (a fresh row or a roll),
            # so it has to run inside WriteSession, not a plain read.
            # Run every cycle that has events to process, regardless of
            # mt_paint_source -- when meshview is gated off (the
            # "freqmapper" case), this loop is the only thing left
            # rolling the shared 'mt' season forward.
            mc_scoring.maybe_roll_season(wconn, now_ts, PROTOCOL)
            season_id = mc_scoring.ensure_active_season(wconn, now_ts, PROTOCOL)
            registered = _load_registered_players(wconn)

            for event in events:
                outcome = self._process_one_event(
                    wconn, event, season_id, registered, now_ts,
                    cfg["mt_paint_source"], cfg["points_per_event"], cfg["unique_painter_bonus"],
                    cfg["paint_from"],
                    watcher_weight_enabled=cfg["watcher_weight_enabled"],
                    watcher_weight_base=cfg["watcher_weight_base"],
                    watcher_weight_increment=cfg["watcher_weight_increment"],
                    watcher_weight_cap=cfg["watcher_weight_cap"],
                )
                counts[outcome] = counts.get(outcome, 0) + 1

            # The cursor advances ONLY here, after every event on this
            # page has been fully processed and written above -- never
            # before (see this module's docstring: "never save a cursor
            # before its page is fully processed"). If anything above
            # raises, this line is never reached, next_cursor is never
            # persisted, and the WriteSession context manager rolls the
            # whole transaction back (including any per-event writes
            # already made this cycle), so a restart or the next cycle
            # resumes this exact page from scratch rather than skipping
            # or double-scoring part of it.
            #
            # paint_from skips advance the cursor too, same as every
            # other per-event outcome -- otherwise a connector gated
            # entirely behind a not-yet-reached paint_from would never
            # progress past its own backlog, re-fetching the same
            # too-early page forever. paint_from only decides whether a
            # verified_tx event SCORES and is recorded in
            # freqmapper_verification (see _process_one_event), never
            # whether the poller moves forward.
            if next_cursor:
                set_cursor(wconn, COMBINED_CURSOR_KEY, next_cursor)

        log.info(
            "freqmapper poll: events=%d painted=%d duplicate=%d unregistered=%d "
            "bad_coord=%d out_of_area=%d malformed=%d inactive_source=%d "
            "before_paint_from=%d passive_rx=%d unknown_event_type=%d error=%d has_more=%s",
            len(events), counts["painted"], counts["skipped_duplicate"],
            counts["skipped_unregistered"], counts["skipped_bad_coord"],
            counts["skipped_out_of_area"], counts["skipped_malformed"],
            counts["skipped_inactive_source"], counts["skipped_before_paint_from"],
            counts["skipped_passive_rx"], counts["skipped_unknown_event_type"],
            counts["error"], has_more,
        )

        await self._maybe_housekeeping()
        await self._record_ok()
        return cfg["poll_interval_seconds"]

    def _process_one_event(
        self, conn, event: object, season_id: int,
        registered: dict[str, tuple[int, str]], now_ts: int,
        mt_paint_source: str, points_per_event: float, unique_painter_bonus: float,
        paint_from: str,
        *,
        watcher_weight_enabled: bool = False,
        watcher_weight_base: float = 0.5,
        watcher_weight_increment: float = 0.1,
        watcher_weight_cap: float = 1.0,
    ) -> str:
        """Process one combined-feed event inside the caller's already-
        open write transaction. Returns an outcome key matching one of
        the counters `_poll_once` tallies.

        The four watcher_weight_* parameters are keyword-only with
        neutral defaults (weighting OFF, matching freqmapper_config's
        own default) so every existing call site that predates
        watcher-count weighting keeps behaving exactly as before without
        having to be updated just to pass them.
        """
        if not isinstance(event, dict):
            return "skipped_malformed"

        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            return "skipped_malformed"

        event_type = event.get("event_type")

        if event_type == EVENT_TYPE_PASSIVE_RX:
            # Passive RX scoring is Phase 3 -- a separate, not-yet-made
            # decision (see this module's docstring). Deduped now (its
            # own prefixed event_id, never colliding with a verified_tx
            # row) purely so a future RX-scoring rollout does not have
            # to treat this deployment's entire RX history as unseen;
            # nothing about it is painted, scored, or otherwise acted on
            # here.
            cur = conn.execute(
                "INSERT OR IGNORE INTO freqmapper_verification(event_id, seen_at) VALUES (?, ?)",
                (event_id, now_ts),
            )
            if cur.rowcount == 0:
                return "skipped_duplicate"
            return "skipped_passive_rx"

        if event_type != EVENT_TYPE_VERIFIED_TX:
            # Any event_type this module does not recognize -- a future
            # addition to the feed FreqMapper's own migration guidance
            # explicitly anticipates. Counted and deduped exactly like
            # passive_rx, never painted, and never allowed to crash the
            # poll loop over an event shape this code predates.
            cur = conn.execute(
                "INSERT OR IGNORE INTO freqmapper_verification(event_id, seen_at) VALUES (?, ?)",
                (event_id, now_ts),
            )
            if cur.rowcount == 0:
                return "skipped_duplicate"
            return "skipped_unknown_event_type"

        # ----- event_type == "verified_tx": the scoring pipeline -----

        # ----- Paint-from date gate -----
        # Deliberately runs BEFORE the freqmapper_verification dedup
        # insert just below, unlike every other skip reason in this
        # function, which is recorded there regardless of outcome (see
        # that table's own comment in app/db.py). A date-skipped event
        # must NOT be recorded -- it needs to stay retrievable, the same
        # read-first-not-claim-first discipline app/checkin.py's
        # _seen()/_mark_seen() split enforces (see
        # checkin._record_unresolved_sender's docstring for the
        # production incident this mirrors: two players' real
        # 2026-08-19 award was lost because a message got marked seen
        # before its outcome was actually settled, so no later poll
        # ever looked at it again). Recording a date-skip here would be
        # exactly that mistake: an admin who moves paint_from earlier
        # and clears the cursor (POST /api/admin/paint/clear-cursor)
        # needs FreqMapper to hand this same event back and needs it to
        # reach this function with a clean slate -- the dedup INSERT OR
        # IGNORE below would otherwise silently treat it as already
        # handled forever.
        #
        # Blank paint_from means BLOCK EVERY EVENT, never "no lower
        # bound" -- the same contract app/checkin.py's net_date_for_net
        # enforces for checkin_net.start_date (see that function's own
        # comment): a freshly enabled connector must never silently
        # backfill an entire feed just because nobody has set a date
        # yet. This is the safe default, and deliberate.
        #
        # An unparseable event time does NOT trigger this gate -- it
        # falls through unchanged to the ordinary malformed handling
        # further down (still recorded in freqmapper_verification
        # exactly as every other malformed event already is): "we can't
        # tell when this happened" is a data problem, not a date-window
        # decision, and must not silently dodge the dedup table the way
        # a genuine date-skip does.
        ts = _event_time(event)
        if ts is not None and (not paint_from or _local_date(ts) < paint_from):
            return "skipped_before_paint_from"

        # Dedup on event_id FIRST, before anything else touches this
        # event -- see freqmapper_verification's comment in app/db.py.
        # Recorded regardless of registration, coordinate validity, or
        # which source is currently painting, so a later retry (a
        # restart, a switch of mt_paint_source) never reprocesses the
        # same verified observation twice.
        cur = conn.execute(
            "INSERT OR IGNORE INTO freqmapper_verification(event_id, seen_at) VALUES (?, ?)",
            (event_id, now_ts),
        )
        if cur.rowcount == 0:
            return "skipped_duplicate"

        # Normalize via the shared helper (app/node_ref.py), not a
        # hand-rolled strip -- it accepts both "!43211234" and bare form,
        # in any case, and is the single definition of "valid node
        # reference" the whole app already agrees on.
        node_ref = normalize_node_ref(event.get("radio_node_id"))
        if node_ref is None:
            return "skipped_malformed"

        # ----- Registration gate -----
        # REGISTERED PLAYERS ONLY -- see this module's docstring for why
        # this deliberately does NOT auto-bind the way app/mc_ingest.py's
        # MeshCore path does: FreqMapper reports on any radio it
        # observes, not just ones this deployment's own players carry.
        entry = registered.get(node_ref)
        if entry is None:
            return "skipped_unregistered"
        player_id, team = entry

        lat = event.get("latitude")
        lon = event.get("longitude")
        if not valid_coord(lat, lon):
            return "skipped_bad_coord"

        if not in_play_area(
            lat, lon,
            settings.play_area_north, settings.play_area_south,
            settings.play_area_west, settings.play_area_east,
        ):
            return "skipped_out_of_area"

        # ts was already parsed above (for the paint_from gate) -- a
        # None here means it was unparseable and the gate above
        # deliberately let it fall through to here instead of skipping
        # it for-date.
        if ts is None:
            return "skipped_malformed"

        # Cell. Raw lat/lon are never written to the database anywhere;
        # they are discarded right here, after being reduced to a cell
        # id -- same rule every other ingest path in this app follows.
        cell = cell_id(lat, lon)

        if mt_paint_source == "meshview":
            # Fully processed and deduped above (this exact event will
            # never be reprocessed, even after a later switch), but
            # nothing is scored or written to the board while meshview is
            # the sole active paint source. Runs for "freqmapper" and
            # "both" alike -- see the freqmapper_config.mt_paint_source
            # comment in app/db.py. The transition itself is logged once
            # per change in _poll_once above, not here -- this runs once
            # per event, and would otherwise spam the log on a page full
            # of events while gated off.
            return "skipped_inactive_source"

        seen_at = int(time.time())
        cur = conn.execute(
            "INSERT OR IGNORE INTO player_cell_ping"
            "(player_id, protocol, cell_id, ts, seen_at, precision_bits) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            (player_id, PROTOCOL, cell, ts, seen_at),
        )
        if cur.rowcount == 0:
            # Same player/cell/second already recorded -- two distinct
            # verified events landing in the same cell within the same
            # second is possible even though event_id itself never
            # repeats. Treated as a duplicate ping, same as every other
            # ingest path here (app/ingest.py, app/mc_ingest.py both
            # reject rather than double-score a coincidental collision).
            return "skipped_duplicate"

        tx_points = _verified_tx_points(
            event.get("watcher_count"), points_per_event,
            watcher_weight_enabled, watcher_weight_base,
            watcher_weight_increment, watcher_weight_cap,
        )

        try:
            paint_result = mc_scoring.apply_paint(
                conn, season_id, player_id, team, cell, ts,
                [], 0.0, 0.0, PROTOCOL, seen_at,
                flat_points=tx_points,
                unique_player_bonus=unique_painter_bonus,
            )
        except Exception:
            log.exception(
                "freqmapper scoring: apply_paint failed for player %d cell %s",
                player_id, cell,
            )
            return "error"

        # Places Worth Going (app/place_scoring.py). A FreqMapper event
        # carries no repeater/feeder list at all -- the API deliberately
        # does not report how many stations heard a transmission, only
        # (as of the combined feed) how many independently verified it
        # -- but every event reaching this point is independently-
        # verified coverage, never a ping that reached nobody.
        # credit_places() used to gate on a non-empty repeater list as a
        # stand-in for "did this ping reach anyone", which read
        # FreqMapper's always-empty list as exactly that and silently
        # credited nothing for this whole board. It now gates on
        # apply_paint()'s outcome instead (see its docstring):
        # flat_points mode never returns "no_signal" -- there is no
        # "named zero repeaters" check to fail when there's no repeater
        # list to check -- so every accepted event here is eligible to
        # credit a place, same as any scoring MeshCore or meshview ping.
        # by_air is not a FreqMapper concept (no aircraft-speed detection
        # on this path), so it is always False.
        try:
            credit_places(conn, player_id, cell, ts, paint_result.outcome, False, PROTOCOL)
        except Exception:
            log.exception(
                "place scoring: credit_places failed for player %d cell %s",
                player_id, cell,
            )

        return "painted"

    # ---- poll status (app/admin_ops.py's GET /api/admin/paint) ----------
    #
    # Mirrors app/checkin.py's CheckinPoller._record_net_ok/
    # _record_net_error exactly, one level up: those write per-NET
    # status onto checkin_net, these write this connector's one status
    # onto the freqmapper_config singleton (there being only one
    # FreqMapper connector, not many). last_poll_at advances on every
    # completed request, success or failure, the same way checkin's
    # does -- it answers "is this connector still being reached at all,"
    # which a failed request still demonstrates. last_poll_error is
    # cleared on the next success so a transient failure doesn't sit in
    # the admin panel forever looking current.

    async def _record_ok(self) -> None:
        async with WriteSession() as conn:
            conn.execute(
                "UPDATE freqmapper_config SET last_poll_at = ?, last_poll_error = NULL WHERE id = 1",
                (int(time.time()),),
            )

    async def _record_error(self, error: str) -> None:
        # Truncated to the same 500 chars app/checkin.py's
        # _record_net_error keeps: an upstream client library's
        # exception text can run arbitrarily long, and this only has to
        # be enough for an operator to recognize what broke -- the full
        # traceback already went to the log above.
        async with WriteSession() as conn:
            conn.execute(
                "UPDATE freqmapper_config SET last_poll_at = ?, last_poll_error = ? WHERE id = 1",
                (int(time.time()), error[:500]),
            )

    # ---- housekeeping ---------------------------------------------------

    async def _maybe_housekeeping(self) -> None:
        now = time.monotonic()
        if now - self._last_housekeeping < _HOUSEKEEPING_INTERVAL_S:
            return
        self._last_housekeeping = now
        cutoff = int(time.time()) - _VERIFICATION_RETENTION_HOURS * 3600
        async with WriteSession() as conn:
            cur = conn.execute(
                "DELETE FROM freqmapper_verification WHERE seen_at < ?", (cutoff,)
            )
            removed = cur.rowcount
        if removed:
            log.info("freqmapper housekeeping: removed %d stale verification rows", removed)
