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
every verified event is worth a flat settings.freqmapper_points_per_event,
plus settings.freqmapper_unique_painter_bonus the first time a given
player paints a given cell for their team (exactly the mc_tile_unique_painter
mechanic MeshCore/meshview already use). See apply_paint()'s flat_points
parameter in app/mc_scoring.py for how this reuses that shared machinery
(ownership, the capture/defense window, decay, and the capture log) while
skipping only the repeater-cooldown/cap logic that has no FreqMapper
analogue.

settings.mt_paint_source is the single switch that decides whether
meshview or FreqMapper is currently allowed to paint the Meshtastic
board -- see that setting's own comment in app/config.py, and
app/ingest.py, whose position-packet poll and backfill read the same
setting to gate themselves off when it is "freqmapper". This module's
poll loop keeps running (and keeps deduping on verification_id) whenever
settings.freqmapper_enabled is true REGARDLESS of mt_paint_source -- only
the final score/write (mc_scoring.apply_paint + the player_cell_ping
insert) is gated on it being "freqmapper" specifically. That means an
operator can watch FreqMapper's own poll-cycle log lines (painted vs.
skipped_inactive_source) before ever flipping the switch, and flipping it
later never replays history: every event this loop has already seen is
already recorded in freqmapper_verification by then.
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


def _clamped_limit() -> int:
    return max(_MIN_LIMIT, min(int(settings.freqmapper_page_limit), _MAX_LIMIT))


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
        # 403 backoff state: a monotonic deadline before which _poll_once
        # skips fetching entirely, and a flag so the warning is logged
        # once per throttling episode rather than once per skipped cycle.
        self._retry_after = 0.0
        self._throttle_warned = False
        self._last_housekeeping = 0.0
        self._logged_inactive_once = False

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        # Empty means off, same contract every other secret setting in
        # this app uses (admin_token, mc_checkin_base_url) -- a blank key
        # must never be read as "authenticate with nothing."
        if not settings.freqmapper_enabled or not settings.freqmapper_api_key:
            log.info(
                "freqmapper ingest disabled (enabled=%s, key configured=%s)",
                settings.freqmapper_enabled, bool(settings.freqmapper_api_key),
            )
            return

        log.info(
            "freqmapper ingest loop starting; poll=%ds limit=%d paint_source=%s (%s)",
            settings.freqmapper_poll_interval_seconds, _clamped_limit(),
            settings.mt_paint_source,
            "FreqMapper is painting" if settings.mt_paint_source == "freqmapper"
            else "FreqMapper is NOT painting -- events are still processed and deduped",
        )

        self._client = httpx.AsyncClient(
            base_url=settings.freqmapper_base_url.rstrip("/"),
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={
                "Accept": "application/json",
                "User-Agent": "meshwars/1.0",
                "Authorization": f"Bearer {settings.freqmapper_api_key}",
            },
        )
        try:
            while not self._stop.is_set():
                try:
                    await self._poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("freqmapper ingest cycle failed")
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=settings.freqmapper_poll_interval_seconds
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._client.aclose()
            self._client = None
        log.info("freqmapper ingest loop stopped")

    async def _poll_once(self) -> None:
        now_mono = time.monotonic()
        if now_mono < self._retry_after:
            # Still cooling down from a recent 403 -- see the throttling
            # handling below. Nothing to fetch this cycle.
            return

        conn = connect()
        try:
            cursor = get_cursor(conn, CURSOR_KEY, "") or None
        finally:
            conn.close()

        params: dict = {"limit": _clamped_limit()}
        if cursor:
            params["cursor"] = cursor

        try:
            r = await self._client.get(
                "/api/v1/integrations/verified-coverage", params=params
            )
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            log.warning("freqmapper: request failed: %s", e)
            return

        if r.status_code == 403:
            # Undocumented throttling (see this module's docstring):
            # sustained fast paging trips it and it recovers on its own
            # after a pause. Expected under normal polling, not a real
            # error -- logged once per episode (not on every cycle spent
            # cooling down) and treated as "try again later," never as a
            # reason to let the poll loop itself die.
            if not self._throttle_warned:
                log.warning(
                    "freqmapper: throttled (403) -- backing off for %ds",
                    settings.freqmapper_poll_interval_seconds * _THROTTLE_BACKOFF_MULTIPLE,
                )
                self._throttle_warned = True
            self._retry_after = now_mono + (
                settings.freqmapper_poll_interval_seconds * _THROTTLE_BACKOFF_MULTIPLE
            )
            return
        self._throttle_warned = False

        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            log.warning("freqmapper: upstream error: %s", e)
            return

        try:
            data = r.json()
        except ValueError:
            log.warning("freqmapper: response was not valid JSON")
            return

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
            return

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
                outcome = self._process_one_event(wconn, event, season_id, registered, now_ts)
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

    def _process_one_event(
        self, conn, event: object, season_id: int,
        registered: dict[str, tuple[int, str]], now_ts: int,
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

        if settings.mt_paint_source != "freqmapper":
            # Fully processed and deduped above (this exact event will
            # never be reprocessed, even after a later switch), but
            # nothing is scored or written to the board while meshview is
            # the active paint source. See settings.mt_paint_source.
            if not self._logged_inactive_once:
                log.info(
                    "freqmapper: mt_paint_source=%s -- events are being "
                    "processed and deduped but NOT painted",
                    settings.mt_paint_source,
                )
                self._logged_inactive_once = True
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
                flat_points=settings.freqmapper_points_per_event,
                unique_player_bonus=settings.freqmapper_unique_painter_bonus,
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
