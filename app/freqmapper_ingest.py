"""Polling loop: fetch verified Meshtastic coverage events from FreqMapper
and paint the sender's grid cell through the shared MeshCore-model
scoring path (app/mc_scoring.py) -- for REGISTERED Meshtastic players
only, same registration gate app/ingest.py enforces for meshview.

FreqMapper is a third-party, independently-operated Meshtastic
coverage-mapping service, entirely separate from meshview. It exposes
one read-only endpoint:

    GET /api/v1/integrations/verified-coverage
    Header: Authorization: Bearer <key>
    Params: limit (1-1000), cursor (opaque, base64 JSON), region_iata

...returning a page of independently-verified reception events, oldest
first, with an opaque next_cursor to resume after the last one returned.

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

Why flat scoring instead of feeder-count: app/ingest.py's meshview path
scores a ping by how many distinct MQTT feeders heard it, because that
is the one thing meshview can actually tell us. FreqMapper deliberately
does not report how many independent watchers verified a given
transmission -- "coverage_rule": "independent_watcher_verified" is as
specific as the API gets -- so there is no count to score on. Instead
every verified event is worth a flat points_per_event, plus
unique_painter_bonus the first time a given player paints a given cell
for their team (exactly the mc_tile_unique_painter mechanic MeshCore/
meshview already use). See apply_paint()'s flat_points parameter in
app/mc_scoring.py for how this reuses that shared machinery (ownership,
the capture/defense window, decay, and the capture log) while skipping
only the repeater-cooldown/cap logic that has no FreqMapper analogue.

Config is DB-backed (app/db.py's freqmapper_config singleton), not
settings.py -- see load_freqmapper_config below, read FRESH on every
poll cycle so an admin edit through app/admin_ops.py's /api/admin/paint
takes effect on the very next cycle, no restart. settings.py's
freqmapper_*/mt_paint_source fields still exist (app/config.py) and are
never deleted: they are the seed source seed_freqmapper_config_from_env
uses to populate this table's row on first boot, and the fallback
load_freqmapper_config returns if that row is somehow missing.

mt_paint_source is the single switch that decides whether meshview or
FreqMapper is currently allowed to paint the Meshtastic board -- see
that column's own comment in app/db.py, and app/ingest.py, whose
position-packet poll and backfill read the same DB value (via
load_freqmapper_config, not settings.mt_paint_source) to gate
themselves off when it is "freqmapper". This module's poll loop keeps
running (and keeps deduping on verification_id) whenever
freqmapper_config.enabled is true REGARDLESS of mt_paint_source -- only
the final score/write (mc_scoring.apply_paint + the player_cell_ping
insert) is gated on it being "freqmapper" specifically. That means an
operator can watch FreqMapper's own poll-cycle log lines (painted vs.
skipped_inactive_source) before ever flipping the switch, and flipping it
later never replays history: every event this loop has already seen is
already recorded in freqmapper_verification by then.

Unlike before this table existed, run_forever() is started
UNCONDITIONALLY by app/main.py and never exits early just because
enabled is currently off -- the loop itself checks
freqmapper_config.enabled fresh on every cycle and simply does nothing
when it is off, the same shape app/checkin.py's CheckinPoller already
uses for checkin_config.enabled. The loop has to always be running for
`enabled` to be a true runtime toggle: a task that already returned at
startup would never notice an admin flipping it back on later.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from . import mc_scoring
from .config import settings
from .db import WriteSession, connect, get_cursor, set_cursor
from .grid import cell_id, in_play_area, valid_coord
from .node_ref import normalize_node_ref

log = logging.getLogger("freqmapper_ingest")

PROTOCOL = "mt"

CURSOR_KEY = "freqmapper_next_cursor"

_MIN_LIMIT = 1
_MAX_LIMIT = 1000

# How long a 403 (undocumented throttling -- see this module's docstring
# and _poll_once below) suppresses further requests before trying again.
# A flat multiple of the poll interval rather than a fixed number: a
# deployment that has tuned its poll interval up or down already has an
# opinion about how "polite" polling should be, and this backoff should
# scale with that opinion rather than fight it.
_THROTTLE_BACKOFF_MULTIPLE = 2

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


def _parse_verified_at(raw: object) -> int | None:
    """verified_at -> epoch seconds, or None if it isn't a parseable ISO
    8601 timestamp. FreqMapper's own example carries an explicit UTC
    offset ("...+00:00"), not a bare "Z" -- datetime.fromisoformat handles
    that natively -- but a defensive "Z" -> "+00:00" swap is kept anyway
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


def _clamped_limit(page_limit: int) -> int:
    return max(_MIN_LIMIT, min(int(page_limit), _MAX_LIMIT))


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
    row was itself seeded from.
    """
    row = conn.execute(
        "SELECT mt_paint_source, enabled, base_url, api_key, poll_interval_seconds, "
        "       page_limit, points_per_event, unique_painter_bonus, last_poll_at, "
        "       last_poll_error, updated_at "
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
            "last_poll_at": None,
            "last_poll_error": None,
            "updated_at": 0,
        }
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
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
    """
    row = conn.execute("SELECT updated_at FROM freqmapper_config WHERE id = 1").fetchone()
    if row is None or row["updated_at"] != 0:
        return
    conn.execute(
        "UPDATE freqmapper_config SET mt_paint_source = ?, enabled = ?, base_url = ?, "
        " api_key = ?, poll_interval_seconds = ?, page_limit = ?, points_per_event = ?, "
        " unique_painter_bonus = ?, updated_at = ? WHERE id = 1",
        (
            settings.mt_paint_source,
            int(settings.freqmapper_enabled),
            settings.freqmapper_base_url,
            settings.freqmapper_api_key,
            settings.freqmapper_poll_interval_seconds,
            settings.freqmapper_page_limit,
            settings.freqmapper_points_per_event,
            settings.freqmapper_unique_painter_bonus,
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
        # 403 backoff state: a monotonic deadline before which _poll_once
        # skips fetching entirely, and a flag so the warning is logged
        # once per throttling episode rather than once per skipped cycle.
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
            cursor = get_cursor(conn, CURSOR_KEY, "") or None
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
                "FreqMapper is painting" if cfg["mt_paint_source"] == "freqmapper"
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
            # Still cooling down from a recent 403 -- see the throttling
            # handling below. Nothing to fetch this cycle.
            return cfg["poll_interval_seconds"]

        params: dict = {"limit": _clamped_limit(cfg["page_limit"])}
        if cursor:
            params["cursor"] = cursor

        try:
            r = await self._client.get(
                "/api/v1/integrations/verified-coverage", params=params
            )
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            log.warning("freqmapper: request failed: %s", e)
            await self._record_error(str(e))
            return cfg["poll_interval_seconds"]

        if r.status_code == 403:
            # Undocumented throttling (see this module's docstring):
            # sustained fast paging trips it and it recovers on its own
            # after a pause. Expected under normal polling, not a real
            # error -- logged once per episode (not on every cycle spent
            # cooling down), treated as "try again later" rather than a
            # reason to let the poll loop itself die, and deliberately
            # NOT recorded as last_poll_error: an operator reading the
            # admin status panel should not see this as something
            # broken.
            backoff = cfg["poll_interval_seconds"] * _THROTTLE_BACKOFF_MULTIPLE
            if not self._throttle_warned:
                log.warning("freqmapper: throttled (403) -- backing off for %ds", backoff)
                self._throttle_warned = True
            self._retry_after = now_mono + backoff
            return cfg["poll_interval_seconds"]
        self._throttle_warned = False

        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            log.warning("freqmapper: upstream error: %s", e)
            await self._record_error(str(e))
            return cfg["poll_interval_seconds"]

        try:
            data = r.json()
        except ValueError:
            log.warning("freqmapper: response was not valid JSON")
            await self._record_error("response was not valid JSON")
            return cfg["poll_interval_seconds"]

        events = data.get("events") if isinstance(data, dict) else None
        if not isinstance(events, list):
            events = []
        next_cursor = data.get("next_cursor") if isinstance(data, dict) else None
        has_more = bool(data.get("has_more")) if isinstance(data, dict) else False

        if not events:
            # An empty page can still hand back a next_cursor at the live
            # edge -- worth persisting so the next poll starts from there
            # rather than re-requesting the same empty tail -- but never
            # overwrite an already-persisted cursor with a blank one.
            if next_cursor:
                async with WriteSession() as wconn:
                    set_cursor(wconn, CURSOR_KEY, next_cursor)
            await self._maybe_housekeeping()
            await self._record_ok()
            return cfg["poll_interval_seconds"]

        now_ts = int(time.time())
        counts = {
            "painted": 0, "skipped_duplicate": 0, "skipped_unregistered": 0,
            "skipped_bad_coord": 0, "skipped_out_of_area": 0,
            "skipped_malformed": 0, "skipped_inactive_source": 0, "error": 0,
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
                )
                counts[outcome] = counts.get(outcome, 0) + 1

            if next_cursor:
                set_cursor(wconn, CURSOR_KEY, next_cursor)

        log.info(
            "freqmapper poll: events=%d painted=%d duplicate=%d unregistered=%d "
            "bad_coord=%d out_of_area=%d malformed=%d inactive_source=%d error=%d has_more=%s",
            len(events), counts["painted"], counts["skipped_duplicate"],
            counts["skipped_unregistered"], counts["skipped_bad_coord"],
            counts["skipped_out_of_area"], counts["skipped_malformed"],
            counts["skipped_inactive_source"], counts["error"], has_more,
        )

        await self._maybe_housekeeping()
        await self._record_ok()
        return cfg["poll_interval_seconds"]

    def _process_one_event(
        self, conn, event: object, season_id: int,
        registered: dict[str, tuple[int, str]], now_ts: int,
        mt_paint_source: str, points_per_event: float, unique_painter_bonus: float,
    ) -> str:
        """Process one verified-coverage event inside the caller's
        already-open write transaction. Returns an outcome key matching
        one of the counters `_poll_once` tallies.
        """
        if not isinstance(event, dict):
            return "skipped_malformed"

        verification_id = event.get("verification_id")
        if not isinstance(verification_id, str) or not verification_id:
            return "skipped_malformed"

        # Dedup on verification_id FIRST, before anything else touches
        # this event -- see freqmapper_verification's comment in
        # app/db.py. Recorded regardless of registration, coordinate
        # validity, or which source is currently painting, so a later
        # retry (a restart, a switch of mt_paint_source) never
        # reprocesses the same verified observation twice.
        cur = conn.execute(
            "INSERT OR IGNORE INTO freqmapper_verification(verification_id, seen_at) VALUES (?, ?)",
            (verification_id, now_ts),
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

        ts = _parse_verified_at(event.get("verified_at"))
        if ts is None:
            return "skipped_malformed"

        # Cell. Raw lat/lon are never written to the database anywhere;
        # they are discarded right here, after being reduced to a cell
        # id -- same rule every other ingest path in this app follows.
        cell = cell_id(lat, lon)

        if mt_paint_source != "freqmapper":
            # Fully processed and deduped above (this exact event will
            # never be reprocessed, even after a later switch), but
            # nothing is scored or written to the board while meshview is
            # the active paint source. See the freqmapper_config.mt_paint_source
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
            # second is possible even though verification_id itself never
            # repeats. Treated as a duplicate ping, same as every other
            # ingest path here (app/ingest.py, app/mc_ingest.py both
            # reject rather than double-score a coincidental collision).
            return "skipped_duplicate"

        try:
            mc_scoring.apply_paint(
                conn, season_id, player_id, team, cell, ts,
                [], 0.0, 0.0, PROTOCOL, seen_at,
                flat_points=points_per_event,
                unique_player_bonus=unique_painter_bonus,
            )
        except Exception:
            log.exception(
                "freqmapper scoring: apply_paint failed for player %d cell %s",
                player_id, cell,
            )
            return "error"

        # Places Worth Going (app/place_scoring.py) is deliberately NOT
        # hooked in here: credit_places() gates on a non-empty
        # repeater_ids list (the same "did this ping reach anyone" test
        # apply_paint's own no_signal case uses), and a FreqMapper event
        # carries no repeater/feeder list at all -- there is nothing for
        # it to credit from, so calling it would only ever be a no-op.

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
