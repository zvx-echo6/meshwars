"""MeshCore wardriving ingest: queue and worker.

This module receives batches of position "pings" pushed by the MeshCore
companion app (MeshMapper) during wardriving sessions. The HTTP handler in
app/api.py that accepts these batches must answer in single-digit
milliseconds: MeshMapper gives the request ten seconds and does not retry
on failure or timeout, so the request path only authenticates the caller,
checks that the batch is well formed, and hands it to an in-memory queue.

All real work -- attributing pings to a player, converting coordinates to
a grid cell, deduping, and writing to the database -- happens here in the
background worker, off the request path entirely.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import logging.handlers
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import mc_scoring
from .config import settings
from .db import _WRITE_LOCK, connect
from .grid import cell_center, cell_id, distance_m, valid_coord

log = logging.getLogger("mc_ingest")

PROTOCOL = "mc"

_CONTACT_RE = re.compile(r"^[0-9a-fA-F]{8}$")

_HOUSEKEEPING_INTERVAL_S = 3600  # at most once per hour

# Every distinct key hash looked up gets a cache entry, including hashes
# for keys that do not exist -- and the ingest endpoint is reachable from
# the public internet. Without a limit, a flood of bogus keys grows this
# dict without bound and can exhaust process memory. This caps it.
_KEY_CACHE_MAX = 10000

# Cap on how many comma-separated repeater entries count_repeaters() will
# look at in a "heard_repeats" string. This field is attacker-controlled
# input from the public internet; without a cap, a single crafted batch
# could carry an enormous string and burn CPU parsing it.
_MAX_PARSED_REPEATERS = 64


def count_repeaters(ping: dict) -> int:
    """Return how many distinct repeaters this ping reached, from
    whichever field its ping type uses:

    - type "TX"/"RX": `heard_repeats`, a string like
      "a1b2(3.5),c3d4(-2.0)", or the literal "None" when nothing was
      heard. Each entry is a repeater id followed by an SNR in
      parentheses; distinct ids are counted, SNR is ignored.
    - type "DISC"/"TRACE": `repeater_id`, a single id, or the literal
      "None" when the discovery failed.

    The literal "None", an empty string, and a missing field all count
    as zero. This is attacker-controlled input from the public internet
    -- malformed entries are ignored, never raised on, and parsing is
    capped at _MAX_PARSED_REPEATERS entries.

    SNR is deliberately not used for anything, even though it's right
    there in the string -- signal strength mostly reflects the antenna
    someone is carrying, not the coverage they actually found, and
    scoring on repeater count rather than signal quality was a
    deliberate call.
    """
    if not isinstance(ping, dict):
        return 0
    ping_type = ping.get("type")

    if ping_type in ("TX", "RX"):
        heard = ping.get("heard_repeats")
        if not isinstance(heard, str) or not heard or heard == "None":
            return 0
        ids: set[str] = set()
        for entry in heard.split(",")[:_MAX_PARSED_REPEATERS]:
            entry = entry.strip()
            if not entry:
                continue
            # "<id>(<snr>)" -- an entry with no "(" before an id doesn't
            # match the expected shape and is ignored, not counted.
            paren = entry.find("(")
            if paren <= 0:
                continue
            rid = entry[:paren].strip()
            if not rid:
                continue
            ids.add(rid)
        return len(ids)

    if ping_type in ("DISC", "TRACE"):
        rid = ping.get("repeater_id")
        if not isinstance(rid, str) or not rid or rid == "None":
            return 0
        return 1

    return 0


def hash_secret(raw: str) -> str:
    """SHA-256 hex digest of a raw API key, for storage/lookup by hash."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuthResult:
    """Outcome of authenticate(). status is one of:
    "not_found", "revoked", "disabled", "ok".
    player_id is set for every status except "not_found".
    """
    status: str
    player_id: int | None = None


_AUTH_NOT_FOUND = AuthResult("not_found")


class McIngestor:
    """Bounded queue + background worker for MeshCore ingest batches."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=settings.mc_queue_max)
        self._worker_task: asyncio.Task | None = None
        self._key_cache: dict[str, tuple[float, AuthResult]] = {}
        self._last_housekeeping = 0.0

    async def start(self) -> None:
        self._worker_task = asyncio.create_task(self._run_worker(), name="mc-ingest-worker")
        log.info("mc ingest worker started; queue_max=%d", settings.mc_queue_max)

    async def stop(self) -> None:
        if self._worker_task is None:
            return
        self._worker_task.cancel()
        try:
            await self._worker_task
        except (asyncio.CancelledError, Exception):
            pass
        self._worker_task = None
        log.info("mc ingest worker stopped")

    # ---- authentication ---------------------------------------------------

    async def authenticate(self, raw_key: str) -> AuthResult:
        """Resolve an API key to a player, using a short-TTL cache so a
        flood of bad keys can't force a database read per request.
        """
        key_hash = hash_secret(raw_key)
        now = time.monotonic()
        cached = self._key_cache.get(key_hash)
        if cached is not None and cached[0] > now:
            return cached[1]

        result = await asyncio.to_thread(self._lookup_key_sync, key_hash)
        if len(self._key_cache) >= _KEY_CACHE_MAX:
            # First sweep out anything already expired -- that alone
            # usually frees plenty of room. If the cache is still at
            # the limit after that, clear it outright: it is only a
            # cache, so the worst case is a few extra database reads
            # to repopulate it, and that is always better than letting
            # an unbounded cache take down the process.
            expired = [k for k, (exp, _) in self._key_cache.items() if exp <= now]
            for k in expired:
                del self._key_cache[k]
            if len(self._key_cache) >= _KEY_CACHE_MAX:
                self._key_cache.clear()
        self._key_cache[key_hash] = (now + settings.mc_key_cache_seconds, result)
        return result

    def _lookup_key_sync(self, key_hash: str) -> AuthResult:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT a.player_id, a.revoked_at, p.disabled_at "
                "  FROM api_key a JOIN player p ON p.player_id = a.player_id "
                " WHERE a.key_hash = ?",
                (key_hash,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return _AUTH_NOT_FOUND
        if row["revoked_at"] is not None:
            return AuthResult("revoked", row["player_id"])
        if row["disabled_at"] is not None:
            return AuthResult("disabled", row["player_id"])
        return AuthResult("ok", row["player_id"])

    # ---- submission ---------------------------------------------------

    def submit(self, player_id: int, key_hash: str, pings: list, received_at: int) -> bool:
        """Enqueue one batch for background processing. Non-blocking;
        returns False if the queue is full. Must stay fast -- this runs on
        the request path.
        """
        try:
            self._queue.put_nowait((player_id, key_hash, pings, received_at))
            return True
        except asyncio.QueueFull:
            return False

    # ---- worker ---------------------------------------------------

    async def _run_worker(self) -> None:
        try:
            while True:
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=300)
                except asyncio.TimeoutError:
                    await self._maybe_housekeeping()
                    continue
                player_id, key_hash, pings, received_at = item
                try:
                    await self._process_batch(player_id, key_hash, pings, received_at)
                except Exception:
                    log.exception("mc ingest: batch processing failed for player %s", player_id)
                finally:
                    self._queue.task_done()
                await self._maybe_housekeeping()
        except asyncio.CancelledError:
            raise

    async def _process_batch(self, player_id, key_hash, pings, received_at) -> None:
        # All database work for a batch runs in a single thread call, under
        # the same write lock app/db.py uses for the Meshtastic ingest loop,
        # so a slow batch never blocks the event loop (and the HTTP server
        # with it) and writes stay serialized fleet-wide.
        async with _WRITE_LOCK:
            await asyncio.to_thread(
                self._process_batch_sync, player_id, key_hash, pings, received_at
            )

    def _process_batch_sync(self, player_id, key_hash, pings, received_at) -> None:
        counters = {
            "pings_accepted": 0,
            "pings_no_contact": 0,
            "pings_wrong_owner": 0,
            "pings_duplicate": 0,
            "pings_bad_coord": 0,
            "pings_no_repeaters": 0,
        }
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            # MeshCore season bookkeeping happens at most once per batch,
            # not once per ping.
            mc_scoring.maybe_roll_season(conn, received_at)
            season_id = mc_scoring.ensure_active_season(conn, received_at)

            # The whole batch belongs to one player, so their team is read
            # once here rather than once per ping.
            team_row = conn.execute(
                "SELECT team FROM player WHERE player_id = ?", (player_id,)
            ).fetchone()
            team = team_row["team"] if team_row else None
            if team is None:
                log.warning(
                    "mc ingest: player %d has no player row; MeshCore scoring "
                    "skipped for this batch",
                    player_id,
                )

            for ping in pings:
                self._process_one_ping(conn, player_id, ping, received_at, counters, season_id, team)

            day = int(datetime.fromtimestamp(received_at, tz=timezone.utc).strftime("%Y%m%d"))
            conn.execute(
                "INSERT INTO player_ingest_stat("
                "  player_id, protocol, day, batches, pings_accepted, "
                "  pings_no_contact, pings_wrong_owner, pings_duplicate, pings_bad_coord, "
                "  pings_no_repeaters) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(player_id, protocol, day) DO UPDATE SET "
                "  batches = batches + 1, "
                "  pings_accepted = pings_accepted + excluded.pings_accepted, "
                "  pings_no_contact = pings_no_contact + excluded.pings_no_contact, "
                "  pings_wrong_owner = pings_wrong_owner + excluded.pings_wrong_owner, "
                "  pings_duplicate = pings_duplicate + excluded.pings_duplicate, "
                "  pings_bad_coord = pings_bad_coord + excluded.pings_bad_coord, "
                "  pings_no_repeaters = pings_no_repeaters + excluded.pings_no_repeaters",
                (
                    player_id, PROTOCOL, day,
                    counters["pings_accepted"], counters["pings_no_contact"],
                    counters["pings_wrong_owner"], counters["pings_duplicate"],
                    counters["pings_bad_coord"],
                    counters["pings_no_repeaters"],
                ),
            )

            conn.execute(
                "UPDATE api_key SET last_seen_at = ? WHERE key_hash = ?",
                (received_at, key_hash),
            )

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        log.info(
            "mc ingest: player=%d batch processed accepted=%d no_contact=%d "
            "wrong_owner=%d duplicate=%d bad_coord=%d no_repeaters=%d",
            player_id, counters["pings_accepted"], counters["pings_no_contact"],
            counters["pings_wrong_owner"], counters["pings_duplicate"],
            counters["pings_bad_coord"],
            counters["pings_no_repeaters"],
        )

    def _process_one_ping(self, conn, player_id, ping, received_at, counters, season_id, team) -> None:
        # 1. Coordinates + timestamp
        lat = ping.get("lat") if isinstance(ping, dict) else None
        lon = ping.get("lon") if isinstance(ping, dict) else None
        ts_raw = ping.get("timestamp") if isinstance(ping, dict) else None

        if not isinstance(lat, (int, float)) or isinstance(lat, bool):
            counters["pings_bad_coord"] += 1
            return
        if not isinstance(lon, (int, float)) or isinstance(lon, bool):
            counters["pings_bad_coord"] += 1
            return
        if not valid_coord(lat, lon):
            counters["pings_bad_coord"] += 1
            return
        if ts_raw is None:
            counters["pings_bad_coord"] += 1
            return
        try:
            ts = int(ts_raw)
        except (TypeError, ValueError):
            counters["pings_bad_coord"] += 1
            return

        # 2. Contact key -- "Include Contact Key" toggle off in MeshMapper
        # is a common user setup problem, not an attack; log at debug.
        contact = ping.get("contact")
        if not contact or not isinstance(contact, str) or not _CONTACT_RE.match(contact):
            counters["pings_no_contact"] += 1
            log.debug("mc ingest: ping missing/invalid contact key for player %d", player_id)
            return

        # 3. Binding: this IS registration for the radio, there is no
        # separate flow.
        row = conn.execute(
            "SELECT player_id FROM player_node WHERE protocol = ? AND node_ref = ?",
            (PROTOCOL, contact),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) "
                "VALUES (?, ?, ?, ?)",
                (PROTOCOL, contact, player_id, received_at),
            )
            log.info("mc ingest: bound contact %s to player %d", contact, player_id)
        elif row["player_id"] != player_id:
            counters["pings_wrong_owner"] += 1
            log.warning(
                "mc ingest: contact %s belongs to player %d, not requesting player %d "
                "-- possible key sharing or attack; ping dropped",
                contact, row["player_id"], player_id,
            )
            return

        # 4. Cell. Raw lat/lon are never written to the database anywhere;
        # they are discarded right here, after being reduced to a cell id.
        cell = cell_id(lat, lon)
        del lat, lon

        # 5. Duplicate check
        cur = conn.execute(
            "INSERT OR IGNORE INTO player_cell_ping"
            "(player_id, protocol, cell_id, ts, seen_at) VALUES (?, ?, ?, ?, ?)",
            (player_id, PROTOCOL, cell, ts, received_at),
        )
        if cur.rowcount == 0:
            counters["pings_duplicate"] += 1
            return

        # 6. Sanity gates -- log only, never reject; these need real data
        # to tune.
        last_fix = conn.execute(
            "SELECT cell_id, ts FROM player_last_fix WHERE player_id = ? AND protocol = ?",
            (player_id, PROTOCOL),
        ).fetchone()

        if last_fix is not None and ts > last_fix["ts"]:
            elapsed = ts - last_fix["ts"]
            prev_lat, prev_lon = cell_center(last_fix["cell_id"])
            cur_lat, cur_lon = cell_center(cell)
            speed = distance_m(prev_lat, prev_lon, cur_lat, cur_lon) / elapsed
            if speed > settings.mc_max_speed_mps:
                log.warning(
                    "mc ingest: implausible speed for player %d: %.1f m/s over %ds",
                    player_id, speed, elapsed,
                )

        skew = abs(ts - received_at)
        if skew > settings.mc_max_clock_skew_seconds:
            log.warning(
                "mc ingest: clock skew for player %d: %ds (ping ts=%d, server=%d)",
                player_id, skew, ts, received_at,
            )

        # 7. Update last fix, only if this timestamp is at or after the
        # stored one.
        if last_fix is None:
            conn.execute(
                "INSERT INTO player_last_fix(player_id, protocol, cell_id, ts) "
                "VALUES (?, ?, ?, ?)",
                (player_id, PROTOCOL, cell, ts),
            )
        elif ts >= last_fix["ts"]:
            conn.execute(
                "UPDATE player_last_fix SET cell_id = ?, ts = ? "
                " WHERE player_id = ? AND protocol = ?",
                (cell, ts, player_id, PROTOCOL),
            )

        # 8. Accepted
        counters["pings_accepted"] += 1

        # 9. Repeaters heard -> points. A ping that reached zero repeaters
        # earns zero points -- it is still counted above as accepted (it
        # was a valid ping) but flagged here separately so a player can be
        # told why it isn't scoring.
        points = min(
            count_repeaters(ping) * settings.mc_points_per_repeater,
            settings.mc_max_points_per_ping,
        )
        if points <= 0:
            counters["pings_no_repeaters"] += 1

        # 10. MeshCore scoring, inside the same write transaction as the
        # rest of this batch. A scoring failure must not lose the
        # counters already recorded above or abort the rest of the
        # batch -- log it and keep going.
        if team is not None:
            try:
                mc_scoring.apply_paint(conn, season_id, player_id, team, cell, ts, points)
            except Exception:
                log.exception(
                    "mc scoring: apply_paint failed for player %d cell %s",
                    player_id, cell,
                )

    # ---- housekeeping ---------------------------------------------------

    async def _maybe_housekeeping(self) -> None:
        now = time.monotonic()
        if now - self._last_housekeeping < _HOUSEKEEPING_INTERVAL_S:
            return
        self._last_housekeeping = now
        async with _WRITE_LOCK:
            removed_pings, removed_stats = await asyncio.to_thread(self._housekeeping_sync)
        log.info(
            "mc ingest housekeeping: removed %d stale player_cell_ping rows, "
            "%d stale player_ingest_stat rows",
            removed_pings, removed_stats,
        )

    def _housekeeping_sync(self) -> tuple:
        now_ts = int(time.time())
        ping_cutoff = now_ts - settings.mc_ping_retention_hours * 3600
        stat_cutoff_day = int(
            (datetime.now(timezone.utc) - timedelta(days=settings.mc_stat_retention_days))
            .strftime("%Y%m%d")
        )
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur1 = conn.execute(
                "DELETE FROM player_cell_ping WHERE seen_at < ?", (ping_cutoff,)
            )
            removed_pings = cur1.rowcount
            cur2 = conn.execute(
                "DELETE FROM player_ingest_stat WHERE day < ?", (stat_cutoff_day,)
            )
            removed_stats = cur2.rowcount
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return removed_pings, removed_stats


# ---- raw batch diagnostic log ---------------------------------------------
#
# Opt-in, off by default (settings.mc_raw_log_enabled). Records each
# received batch verbatim -- including real GPS positions -- to a
# dedicated rotating file, so real MeshMapper payloads can be inspected
# before thresholds are tuned. Never touches the database and never the
# normal application log.

_raw_log_logger: logging.Logger | None = None
_raw_log_setup_done = False
_raw_log_broken = False


def _get_raw_logger() -> logging.Logger | None:
    """Lazily set up the dedicated raw-batch logger the first time it's
    needed, and only once per process. Returns None if raw logging is
    off, or if setup already failed once (a broken handler is not
    retried on every request).
    """
    global _raw_log_logger, _raw_log_setup_done, _raw_log_broken

    if _raw_log_broken:
        return None
    if _raw_log_setup_done:
        return _raw_log_logger
    _raw_log_setup_done = True

    try:
        handler = logging.handlers.RotatingFileHandler(
            settings.mc_raw_log_path,
            maxBytes=settings.mc_raw_log_max_bytes,
            backupCount=settings.mc_raw_log_backups,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        raw_logger = logging.getLogger("meshwars.mc_raw")
        raw_logger.setLevel(logging.INFO)
        raw_logger.propagate = False  # never leak into the normal app log
        raw_logger.addHandler(handler)
        _raw_log_logger = raw_logger
    except Exception:
        _raw_log_broken = True
        log.warning(
            "mc ingest: could not set up raw batch log at %s -- raw batch "
            "logging disabled for the rest of this process",
            settings.mc_raw_log_path, exc_info=True,
        )
        return None

    log.warning(
        "mc ingest: raw batch logging is ON -- %s will contain real GPS "
        "positions from wardriving batches",
        settings.mc_raw_log_path,
    )
    return _raw_log_logger


def log_raw_batch(player_id: int, key_hash: str, raw_body: bytes) -> None:
    """Append one JSON line to the raw batch diagnostic log, if enabled.
    This is a diagnostic, not a feature: nothing in here may ever raise
    into the request path, so a logging failure can't fail a request.
    """
    if not settings.mc_raw_log_enabled:
        return  # no file handle, no setup, no cost
    try:
        raw_logger = _get_raw_logger()
        if raw_logger is None:
            return
        try:
            body_text = raw_body.decode("utf-8", errors="replace")
        except Exception:
            body_text = "<undecodable body>"
        record = {
            "received_at": time.time(),
            "player_id": player_id,
            # Only the first 8 characters of the key hash are stored --
            # never the raw API key, and never the full hash.
            "key_hash_prefix": key_hash[:8],
            "body": body_text,
        }
        raw_logger.info(json.dumps(record))
    except Exception:
        log.warning("mc ingest: raw batch logging failed", exc_info=True)
