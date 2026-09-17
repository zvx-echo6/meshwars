"""MeshCore wardriving ingest: durable queue and worker.

This module receives batches of position "pings" pushed by the MeshCore
companion app (MeshMapper) during wardriving sessions. The HTTP handler in
app/api.py that accepts these batches must answer in single-digit
milliseconds: MeshMapper gives the request ten seconds and does not retry
on failure or timeout, so the request path only authenticates the caller,
checks that the batch is well formed, and hands it to the durable queue
(app/db.py's mc_ingest_queue table) via McIngestor.submit() -- a single
small INSERT, not a batch's worth of processing.

All real work -- attributing pings to a player, converting coordinates to
a grid cell, deduping, and writing to the database -- happens here in the
background worker, off the request path entirely, reading its backlog
from mc_ingest_queue rather than an in-process asyncio.Queue. That used
to be the design: a batch accepted by the HTTP handler went straight into
an in-memory queue that only this process's own worker task ever drained.
It lost every pending batch on restart -- and every deploy restarts --
silently discarding real player scoring data, and would have made a
web/worker process split (accepting batches on one process, draining them
on another) simply not work: a batch accepted by the web process into its
own private queue would sit there forever, since nothing on that process
ever drains it. The durable queue fixes both: a batch survives a restart,
and, once a web/worker split actually exists (a separate change -- this
module doesn't implement it), a batch accepted by ANY process is visible
to and processable by ANY worker, because it lives in the shared database
rather than one process's heap.
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

from . import mc_scoring, results
from .config import settings
from .db import _WRITE_LOCK, WriteSession, connect
from .grid import cell_center, cell_id, distance_m, in_play_area, valid_coord
from .place_scoring import credit_places

log = logging.getLogger("mc_ingest")

PROTOCOL = "mc"

_CONTACT_RE = re.compile(r"^[0-9a-fA-F]{8}$")

_HOUSEKEEPING_INTERVAL_S = 3600  # at most once per hour

# Every distinct key hash looked up gets a cache entry, including hashes
# for keys that do not exist -- and the ingest endpoint is reachable from
# the public internet. Without a limit, a flood of bogus keys grows this
# dict without bound and can exhaust process memory. This caps it.
_KEY_CACHE_MAX = 10000

# Every distinct key that posts a batch gets a rate-limit tracking entry,
# same exposure as the key cache above: the endpoint is public, so this
# needs the same bound for the same reason.
_RATE_LIMIT_MAX_TRACKED = 10000

# Cap on how many comma-separated repeater entries parse_repeaters() will
# look at in a "heard_repeats" string. This field is attacker-controlled
# input from the public internet; without a cap, a single crafted batch
# could carry an enormous string and burn CPU parsing it.
_MAX_PARSED_REPEATERS = 64

# Upper bound on a speed that can still be read as an aircraft. Above
# this (roughly 900 mph) a "speed" between two fixes is a GPS jump, not
# a vehicle -- a stale fix, a cold start, or a phone resolving its
# position from a cell tower in the next county. Marking those by_air
# would quietly disqualify a legitimate remote claim from the very
# awards it should win, so anything this fast is treated as a bad fix
# and left unmarked. settings.mc_max_speed_mps is the LOWER edge of the
# aircraft band; this is the upper one.
_GLITCH_SPEED_MPS = 400.0


@dataclass(frozen=True)
class RepeaterEntry:
    """One repeater identity observed in a single ping, plus whatever
    signal/identity fields that ping type carries for it.

    `kind` is the crucial distinction this whole module is built around,
    and it must never be collapsed:

    - "direct": from a DISC/TRACE ping. A measured relationship between
      this position and this one named repeater -- local_snr,
      local_rssi, and remote_snr describe that link directly. DISC pings
      additionally carry public_key/node_type (TRACE does not).
    - "heard": from a TX/RX ping's `heard_repeats` string. This
      repeater's id came back through the mesh, possibly over multiple
      hops -- it describes the network's reach from this square, not
      necessarily a direct line to the position. heard_snr is whatever
      SNR that string reported for it.

    Conflating "direct" and "heard" observations would make someone
    standing beneath their own well-connected repeater indistinguishable
    from someone on a ridge with genuine multi-hop reach -- so callers
    must always keep direct_count and heard_count (see app/db.py's
    repeater_observation table) separate, never summed into one figure.
    """
    repeater_id: str
    kind: str  # "direct" | "heard"
    local_snr: float | None = None
    local_rssi: float | None = None
    remote_snr: float | None = None
    heard_snr: float | None = None
    public_key: str | None = None
    node_type: str | None = None


def _coerce_float(value: object) -> float | None:
    """Best-effort float, or None for anything that isn't a plain finite
    number. Attacker-controlled input -- never raises.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf guard
        return None
    return f


def _coerce_str(value: object) -> str | None:
    """A non-empty string, or None. Attacker-controlled input -- never
    raises.
    """
    if isinstance(value, str) and value and value != "None":
        return value
    return None


def _parse_heard_snr(text: str) -> float | None:
    """Parse the "<snr>" out of a "<id>(<snr>)" heard_repeats entry, given
    the text after the "(". Malformed numbers are ignored, never raised
    on -- this field is attacker-controlled input.
    """
    text = text.rstrip(")").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


# The two ping-type groups parse_repeaters() below branches on -- "heard"
# entries (from heard_repeats) and "direct" entries (from repeater_id) --
# kept as their own constants and reused BY parse_repeaters() itself
# rather than inlined twice, so is_unknown_ping_type() just below checks
# against exactly the same values parse_repeaters() branches on, with no
# way for the two to drift apart.
_HEARD_PING_TYPES = frozenset({"TX", "RX"})
_DIRECT_PING_TYPES = frozenset({"DISC", "TRACE"})
_RECOGNIZED_PING_TYPES = _HEARD_PING_TYPES | _DIRECT_PING_TYPES


def is_unknown_ping_type(ping: dict) -> bool:
    """True if `ping` names a `type` that parse_repeaters() does not
    recognize -- e.g. a future MeshMapper build's "DEFER". Such a ping
    still falls through parse_repeaters() to an empty repeater list (and
    so still counts toward pings_no_repeaters, same as always); this is
    purely additional observability so an unrecognized protocol type can
    be told apart from a ping that legitimately heard no repeaters.

    A MISSING/None `type` is deliberately NOT unknown: that is an absent
    field (already handled -- and already indistinguishable from "heard
    nothing" -- by every code path that reads ping.get("type") today),
    not a PRESENT value this parser fails to recognize. Only a `type`
    that is actually there, and not one of the four known values, counts.
    """
    if not isinstance(ping, dict):
        return False
    ping_type = ping.get("type")
    return ping_type is not None and ping_type not in _RECOGNIZED_PING_TYPES


def parse_repeaters(ping: dict) -> list[RepeaterEntry]:
    """Return the distinct repeaters this ping reached, with whatever
    signal/identity detail its ping type carries, from whichever field
    its ping type uses:

    - type "TX"/"RX": `heard_repeats`, a string like
      "a1b2(3.5),c3d4(-2.0)", or the literal "None" when nothing was
      heard. Each entry is a repeater id followed by an SNR in
      parentheses; distinct ids are kept as "heard" entries, deduped by
      id (matching the id-count behaviour this replaces exactly).
    - type "DISC"/"TRACE": `repeater_id`, a single id, or the literal
      "None" when the discovery failed. Kept as a single "direct" entry
      carrying local_snr/local_rssi/remote_snr (and public_key/node_type
      for DISC).

    The literal "None", an empty string, and a missing field all yield
    an empty list. This is attacker-controlled input from the public
    internet -- malformed entries are ignored, never raised on, and
    parsing is capped at _MAX_PARSED_REPEATERS entries.

    count_repeaters() below is a thin wrapper -- len(parse_repeaters(...))
    -- so scoring's repeater count and the observations recorded from
    this same call can never drift apart from a second, separately
    maintained parser.
    """
    if not isinstance(ping, dict):
        return []
    ping_type = ping.get("type")

    if ping_type in _HEARD_PING_TYPES:
        heard = ping.get("heard_repeats")
        if not isinstance(heard, str) or not heard or heard == "None":
            return []
        entries: dict[str, RepeaterEntry] = {}
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
            snr = _parse_heard_snr(entry[paren + 1:])
            existing = entries.get(rid)
            if existing is None:
                entries[rid] = RepeaterEntry(repeater_id=rid, kind="heard", heard_snr=snr)
            elif snr is not None and (existing.heard_snr is None or snr > existing.heard_snr):
                # Same id repeated within one ping (e.g. two hops away by
                # two different paths) -- keep the strongest SNR seen for
                # it. Does not change the distinct-id count either way.
                entries[rid] = RepeaterEntry(repeater_id=rid, kind="heard", heard_snr=snr)
        return list(entries.values())

    if ping_type in _DIRECT_PING_TYPES:
        rid = ping.get("repeater_id")
        if not isinstance(rid, str) or not rid or rid == "None":
            return []
        return [RepeaterEntry(
            repeater_id=rid,
            kind="direct",
            local_snr=_coerce_float(ping.get("local_snr")),
            local_rssi=_coerce_float(ping.get("local_rssi")),
            remote_snr=_coerce_float(ping.get("remote_snr")),
            public_key=_coerce_str(ping.get("public_key")),
            node_type=_coerce_str(ping.get("node_type")),
        )]

    return []


def count_repeaters(ping: dict) -> int:
    """Return how many distinct repeaters this ping reached.

    SNR is deliberately not used for anything in the resulting count,
    even though it's right there in the string -- signal strength mostly
    reflects the antenna someone is carrying, not the coverage they
    actually found, and scoring on repeater count rather than signal
    quality was a deliberate call. (The signal values are still recorded
    as observation evidence -- see record_repeater_observations below --
    just never folded into score.)

    This is a thin wrapper over parse_repeaters() rather than a second,
    independent parser -- see that function's docstring for why.
    """
    return len(parse_repeaters(ping))


# Upsert clauses shared by every repeater_observation write below. SQLite's
# MAX(a, b) returns NULL if EITHER argument is NULL, which is wrong for
# "keep the best value seen so far" once either side can be unset -- these
# CASE expressions treat a NULL on either side as "no information," not as
# a value that wins or loses the comparison.
_BEST_SNR_UPDATE = (
    "CASE WHEN excluded.{col} IS NULL THEN {col} "
    "WHEN {col} IS NULL THEN excluded.{col} "
    "ELSE MAX({col}, excluded.{col}) END"
)


def record_repeater_observations(
    conn, protocol: str, cell: str, entries: list[RepeaterEntry], ts: int,
) -> None:
    """Record that these repeaters were audible from this cell, at cell
    granularity only -- no player id, no raw coordinate, ever (see
    app/db.py's repeater_observation/repeater_identity tables).

    Called for every ping that reaches this point in _process_one_ping --
    i.e. every ping that has already passed coordinate validation, the
    play-area check, the contact-key check, and the duplicate check --
    INCLUDING pings whose repeaters mc_scoring.apply_paint() will go on
    to reject for the cooldown. A cooldown means "this repeater doesn't
    score again yet," not "nothing was heard here" -- the square's
    audibility is real evidence either way, so it must not be gated on
    the scoring outcome. Caller already holds app.db's write lock and an
    open write transaction on `conn`, same as mc_scoring.apply_paint --
    this function opens no connection and takes no lock of its own.

    `entries` is whatever parse_repeaters(ping) returned for this ping --
    direct_count/heard_count below come straight from each entry's
    `kind`, never merged, per the module-level note on RepeaterEntry.
    """
    for e in entries:
        direct_inc = 1 if e.kind == "direct" else 0
        heard_inc = 1 if e.kind == "heard" else 0
        conn.execute(
            "INSERT INTO repeater_observation("
            "  protocol, repeater_id, cell_id, first_seen, last_seen,"
            "  direct_count, heard_count, best_local_snr, best_remote_snr, best_heard_snr"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(protocol, repeater_id, cell_id) DO UPDATE SET "
            "  last_seen = MAX(last_seen, excluded.last_seen), "
            "  direct_count = direct_count + excluded.direct_count, "
            "  heard_count = heard_count + excluded.heard_count, "
            f"  best_local_snr = {_BEST_SNR_UPDATE.format(col='best_local_snr')}, "
            f"  best_remote_snr = {_BEST_SNR_UPDATE.format(col='best_remote_snr')}, "
            f"  best_heard_snr = {_BEST_SNR_UPDATE.format(col='best_heard_snr')}",
            (
                protocol, e.repeater_id, cell, ts, ts,
                direct_inc, heard_inc,
                e.local_snr, e.remote_snr, e.heard_snr,
            ),
        )

        # Identity (public_key/node_type) only ever comes from a DISC
        # ping; TRACE also yields a "direct" entry but never carries
        # those two fields (see RepeaterEntry/parse_repeaters), so this
        # still records first_seen/last_seen for a TRACE sighting of a
        # known repeater id without overwriting a real public_key/
        # node_type with nulls -- COALESCE keeps whatever was already
        # stored when the new value is null.
        if e.kind == "direct":
            conn.execute(
                "INSERT INTO repeater_identity("
                "  protocol, repeater_id, public_key, node_type, first_seen, last_seen"
                ") VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(protocol, repeater_id) DO UPDATE SET "
                "  last_seen = MAX(last_seen, excluded.last_seen), "
                "  public_key = COALESCE(excluded.public_key, public_key), "
                "  node_type = COALESCE(excluded.node_type, node_type)",
                (protocol, e.repeater_id, e.public_key, e.node_type, ts, ts),
            )


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
    """Durable queue (app/db.py's mc_ingest_queue) + background worker
    for MeshCore ingest batches."""

    def __init__(self) -> None:
        self._worker_task: asyncio.Task | None = None
        self._key_cache: dict[str, tuple[float, AuthResult]] = {}
        self._rate_limit_hits: dict[str, list[float]] = {}
        self._last_housekeeping = 0.0

    async def start(self) -> None:
        self._worker_task = asyncio.create_task(self._run_worker(), name="mc-ingest-worker")
        log.info(
            "mc ingest worker started; queue_max=%d drain_interval=%.1fs",
            settings.mc_queue_max, settings.mc_queue_drain_interval_seconds,
        )

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

    def invalidate_key(self, key_hash: str) -> None:
        """Drop `key_hash` from the auth cache, if present.

        Called by the admin door on revocation. Without this, a
        just-revoked key could keep authenticating at the ingest
        endpoint until its cached entry expires (`mc_key_cache_seconds`)
        -- this makes revocation take effect on the very next request.
        """
        self._key_cache.pop(key_hash, None)

    def invalidate_player(self, player_id: int) -> None:
        """Drop every cached auth entry belonging to `player_id`.

        Same reasoning as invalidate_key, for the admin door's
        disable/enable: a player can hold more than one key, and the
        cache is keyed by key hash, not player, so this has to scan.
        The cache is bounded to _KEY_CACHE_MAX entries and this is an
        infrequent admin action, not a request-path operation, so the
        scan cost is not a concern here.
        """
        stale = [
            key_hash for key_hash, (_, result) in self._key_cache.items()
            if result.player_id == player_id
        ]
        for key_hash in stale:
            del self._key_cache[key_hash]

    # ---- rate limiting ---------------------------------------------------

    def rate_limit_ok(self, key_hash: str) -> bool:
        """True if this key is still within its per-window batch budget;
        False if it must be rejected with 429. Purely in-process and
        synchronous -- no database read -- so this stays fast on the
        request path. Records this call as a hit when it allows it.

        Bounded the same way `_key_cache` is above: every key hash that
        posts a batch gets an entry here, including keys later found to
        be invalid (this runs after authentication, so that particular
        case doesn't apply, but the endpoint is still public and the
        growth risk is the same) -- sweep expired entries first, and
        only clear the whole dict if that alone doesn't bring it back
        under the cap.
        """
        now = time.monotonic()
        window = settings.mc_ingest_rate_limit_window_seconds
        limit = settings.mc_ingest_rate_limit_batches

        if len(self._rate_limit_hits) >= _RATE_LIMIT_MAX_TRACKED:
            stale = [
                k for k, hits in self._rate_limit_hits.items()
                if not hits or now - hits[-1] >= window
            ]
            for k in stale:
                del self._rate_limit_hits[k]
            if len(self._rate_limit_hits) >= _RATE_LIMIT_MAX_TRACKED:
                self._rate_limit_hits.clear()

        hits = [t for t in self._rate_limit_hits.get(key_hash, []) if now - t < window]
        if len(hits) >= limit:
            self._rate_limit_hits[key_hash] = hits
            return False
        hits.append(now)
        self._rate_limit_hits[key_hash] = hits
        return True

    # ---- submission ---------------------------------------------------

    async def submit(self, player_id: int, key_hash: str, pings: list, received_at: int) -> bool:
        """Durably enqueue one batch: INSERT a row into mc_ingest_queue
        and return True, or return False -- without inserting -- if the
        queue is already at settings.mc_queue_max rows. Same contract as
        the old in-memory queue's put_nowait()/QueueFull: the caller
        (app/api.py's POST /api/mc/ingest) still answers "queue full"
        (503) exactly the same way.

        Must stay fast -- this runs on the request path -- but this is a
        single small write (one COUNT(*) plus one INSERT), so it uses
        the same WriteSession()/_WRITE_LOCK discipline every other quick
        write in this codebase uses (app/account_api.py, app/ingest.py,
        etc. -- see app/db.py's WriteSession docstring) directly in this
        coroutine, rather than asyncio.to_thread: to_thread is reserved
        in this module for _process_batch's genuinely slow, per-ping
        scoring work below, not for a single-row insert.
        """
        payload = json.dumps(pings)
        now = int(time.time())
        async with WriteSession() as conn:
            count = conn.execute("SELECT COUNT(*) FROM mc_ingest_queue").fetchone()[0]
            if count >= settings.mc_queue_max:
                return False
            conn.execute(
                "INSERT INTO mc_ingest_queue"
                "(player_id, key_hash, payload, received_at, enqueued_at, attempts) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (player_id, key_hash, payload, received_at, now),
            )
        return True

    # ---- worker ---------------------------------------------------

    async def _run_worker(self) -> None:
        try:
            await self._reset_stale_claims()
        except Exception:
            log.exception("mc ingest: failed to release stale queue claims at startup")
        try:
            while True:
                try:
                    claimed = await self._claim_batch()
                except Exception:
                    log.exception("mc ingest: failed to claim from durable queue")
                    claimed = []
                for row in claimed:
                    await self._process_queued_row(row)
                await self._maybe_housekeeping()
                if not claimed:
                    # Nothing to do -- wait a short beat before polling
                    # again rather than busy-looping. While there IS a
                    # backlog, the next iteration claims immediately
                    # (no sleep), so a burst drains as fast as the
                    # database allows rather than one drain_interval
                    # chunk at a time.
                    await asyncio.sleep(settings.mc_queue_drain_interval_seconds)
        except asyncio.CancelledError:
            raise

    async def _reset_stale_claims(self) -> None:
        """Release any row a previous run of this process claimed but
        never finished (a crash mid-batch, or an unclean shutdown that
        skipped stop()). Safe to run unconditionally on every worker
        start: this process's OWN worker task has not claimed anything
        yet (this runs before the claim loop starts), so any row still
        showing claimed_at here belongs to a run that is definitely no
        longer alive -- never one genuinely in flight right now.
        """
        async with WriteSession() as conn:
            released = conn.execute(
                "UPDATE mc_ingest_queue SET claimed_at = NULL WHERE claimed_at IS NOT NULL"
            ).rowcount
        if released:
            log.warning(
                "mc ingest: released %d durable-queue row(s) claimed by a previous run",
                released,
            )

    async def _claim_batch(self) -> list[dict]:
        """Atomically claim up to settings.mc_queue_drain_batch_size
        unclaimed, non-dead-lettered rows, oldest id first, and return
        them as plain dicts.

        The UPDATE ... WHERE id IN (SELECT ... LIMIT ?) ... RETURNING
        form is what makes this safe against a second worker: the row
        selection and the claimed_at write happen inside ONE statement,
        inside ONE BEGIN IMMEDIATE/COMMIT (via WriteSession, which holds
        app/db.py's global _WRITE_LOCK for the duration) -- there is no
        window between "decide which rows to take" and "mark them taken"
        for a second claimer to land in. With exactly one worker (today's
        deployment) this is belt-and-braces; it is what makes the same
        code correct if a web/worker split later runs more than one.

        Excludes rows already at settings.mc_queue_max_attempts (the
        dead-letter cutoff -- see _mark_failed below) via the same plain
        WHERE-clause convention discord_notify.py's own drain query uses
        for discord_outbox_max_attempts: a poisoned row simply stops
        matching this query, so it can never wedge the rows behind it,
        without needing a separate "dead" flag.

        RETURNING's row order is not documented SQL semantics to rely
        on, so the result is sorted by id in Python before returning --
        that is what actually guarantees "oldest first" processing, not
        an assumption about statement execution order.
        """
        now = int(time.time())
        async with WriteSession() as conn:
            rows = conn.execute(
                "UPDATE mc_ingest_queue SET claimed_at = ? "
                "WHERE id IN ("
                "  SELECT id FROM mc_ingest_queue"
                "  WHERE claimed_at IS NULL AND attempts < ?"
                "  ORDER BY id LIMIT ?"
                ") "
                "RETURNING id, player_id, key_hash, payload, received_at, attempts",
                (now, settings.mc_queue_max_attempts, settings.mc_queue_drain_batch_size),
            ).fetchall()
        return sorted((dict(r) for r in rows), key=lambda r: r["id"])

    async def _process_queued_row(self, row: dict) -> None:
        """Process one claimed row and either delete it (success) or
        record the failure and release its claim for retry (see
        _mark_failed). Never raises -- a single bad row must not stop
        the rest of this drain pass or take down the worker task.
        """
        row_id = row["id"]
        try:
            pings = json.loads(row["payload"])
        except Exception:
            # Not a transient failure -- this payload can never parse,
            # no matter how many times it's retried. Still routed
            # through the ordinary attempts/last_error path rather than
            # deleted outright: it ages out via the same dead-letter
            # cutoff as a real processing failure, and stays visible to
            # an operator (last_error) instead of vanishing silently.
            log.exception("mc ingest: queue row %d has unparseable payload", row_id)
            await self._mark_failed(row_id, row["attempts"], "payload is not valid JSON")
            return

        try:
            await self._process_batch(row["player_id"], row["key_hash"], pings, row["received_at"])
        except Exception:
            log.exception(
                "mc ingest: batch processing failed for queue row %d (player %s)",
                row_id, row["player_id"],
            )
            await self._mark_failed(row_id, row["attempts"], "batch processing raised -- see server log")
            return

        await self._mark_processed(row_id)

    async def _mark_processed(self, row_id: int) -> None:
        async with WriteSession() as conn:
            conn.execute("DELETE FROM mc_ingest_queue WHERE id = ?", (row_id,))

    async def _mark_failed(self, row_id: int, prior_attempts: int, error: str) -> None:
        """Record a failed attempt and release the row's claim so the
        next drain pass can retry it -- unless this was the attempt that
        crossed settings.mc_queue_max_attempts, in which case it is
        logged loudly (this is the one place an operator finds out a
        batch is stuck) and left in place: _claim_batch's own WHERE
        clause (attempts < mc_queue_max_attempts) never selects it
        again, so it stops being retried without blocking any row behind
        it, but it is never silently dropped -- it stays in
        mc_ingest_queue, attempts and last_error intact, until an
        operator looks at it.
        """
        attempts = prior_attempts + 1
        async with WriteSession() as conn:
            conn.execute(
                "UPDATE mc_ingest_queue SET attempts = ?, last_error = ?, claimed_at = NULL WHERE id = ?",
                (attempts, error, row_id),
            )
        if attempts >= settings.mc_queue_max_attempts:
            log.error(
                "mc ingest: queue row %d dead-lettered after %d attempts -- will not be "
                "retried; last_error=%r",
                row_id, attempts, error,
            )

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
            "pings_out_of_area": 0,
            "pings_no_repeaters": 0,
            "pings_unknown_type": 0,
        }
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            # MeshCore season bookkeeping happens at most once per batch,
            # not once per ping.
            mc_scoring.maybe_roll_season(conn, received_at, PROTOCOL)
            # Same cadence, same reason: a finished month is frozen by
            # whatever traffic arrives after the boundary rather than by a
            # scheduler of its own. See app/results.py.
            results.maybe_roll_months(conn, received_at, PROTOCOL)
            season_id = mc_scoring.ensure_active_season(conn, received_at, PROTOCOL)

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
                "  pings_out_of_area, pings_no_repeaters, pings_unknown_type) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(player_id, protocol, day) DO UPDATE SET "
                "  batches = batches + 1, "
                "  pings_accepted = pings_accepted + excluded.pings_accepted, "
                "  pings_no_contact = pings_no_contact + excluded.pings_no_contact, "
                "  pings_wrong_owner = pings_wrong_owner + excluded.pings_wrong_owner, "
                "  pings_duplicate = pings_duplicate + excluded.pings_duplicate, "
                "  pings_bad_coord = pings_bad_coord + excluded.pings_bad_coord, "
                "  pings_out_of_area = pings_out_of_area + excluded.pings_out_of_area, "
                "  pings_no_repeaters = pings_no_repeaters + excluded.pings_no_repeaters, "
                "  pings_unknown_type = pings_unknown_type + excluded.pings_unknown_type",
                (
                    player_id, PROTOCOL, day,
                    counters["pings_accepted"], counters["pings_no_contact"],
                    counters["pings_wrong_owner"], counters["pings_duplicate"],
                    counters["pings_bad_coord"], counters["pings_out_of_area"],
                    counters["pings_no_repeaters"], counters["pings_unknown_type"],
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
            "wrong_owner=%d duplicate=%d bad_coord=%d out_of_area=%d no_repeaters=%d "
            "unknown_type=%d",
            player_id, counters["pings_accepted"], counters["pings_no_contact"],
            counters["pings_wrong_owner"], counters["pings_duplicate"],
            counters["pings_bad_coord"], counters["pings_out_of_area"],
            counters["pings_no_repeaters"], counters["pings_unknown_type"],
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

        # Play-area check comes immediately after coordinate validity and
        # before everything else -- including the contact key check and
        # binding -- so a ping from outside the configured box never
        # registers a radio.
        if not in_play_area(
            lat, lon,
            settings.play_area_north, settings.play_area_south,
            settings.play_area_west, settings.play_area_east,
        ):
            counters["pings_out_of_area"] += 1
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

        # Real hardware reports this key in uppercase (confirmed from a
        # live MeshMapper payload) while our own test fixtures used
        # lowercase, and it is compared as exact text below to decide
        # which player a ping belongs to. Without normalizing, the same
        # radio reporting in a different case would register as a second
        # radio, and the mismatch check just below could be sidestepped
        # by changing case. Normalize once here; every use of `contact`
        # from this point on -- the binding lookup, the insert, and the
        # mismatch check -- sees the normalized value.
        contact = contact.lower()

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

        # 5b. Parse the repeater fields once here -- both the observation
        # evidence recorded immediately below and the score computed in
        # step 9 come from this same parse, so they can never drift apart.
        #
        # Recorded for every ping that reaches this line, i.e. every ping
        # that has passed coordinate validation, the play-area check, the
        # contact-key check, and the duplicate check above -- INCLUDING a
        # ping whose repeaters mc_scoring.apply_paint() (step 10, below)
        # will go on to reject for the cooldown (every repeater it named
        # already credited to this player on this cell, or the visit cap
        # already reached). A cooldown blocks scoring, not what this
        # square can actually hear -- that's still real evidence, so it
        # is recorded before scoring even runs, and regardless of what
        # scoring later decides.
        entries = parse_repeaters(ping)
        record_repeater_observations(conn, PROTOCOL, cell, entries, ts)

        # 5c. Unknown ping type -- observability only, never a rejection.
        # A `type` that is present but not one of the four parse_repeaters()
        # recognizes (e.g. "DEFER") falls through to an empty repeater
        # list exactly like a legitimate "heard nothing" ping does, so
        # without this it is invisible, silently indistinguishable from
        # real no-coverage evidence in pings_no_repeaters (step 9, below)
        # -- which it still also increments, since parse_repeaters()
        # still returns []. is_unknown_ping_type() deliberately does NOT
        # flag a missing/None `type`; see its own docstring.
        if is_unknown_ping_type(ping):
            counters["pings_unknown_type"] += 1

        # 6. Sanity gates -- these still never REJECT a ping. The speed
        # between consecutive fixes now also decides by_air, which the
        # exploration awards read; it changes nothing about scoring or
        # ownership. See settings.mc_max_speed_mps and _GLITCH_SPEED_MPS.
        by_air = False
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
                by_air = speed <= _GLITCH_SPEED_MPS
                log.warning(
                    "mc ingest: implausible speed for player %d: %.1f m/s over %ds (by_air=%s)",
                    player_id, speed, elapsed, by_air,
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

        # 9. Repeaters heard -> candidate scoring input. A ping that
        # named zero repeaters reached no one -- it is still counted
        # above as accepted (it was a valid ping) but flagged here
        # separately so a player can be told why it isn't scoring.
        # `entries` was already parsed in step 5b (and its observations
        # already recorded there); the repeater ids used for scoring are
        # derived from that same list, never a second parse of the ping.
        # Which of these actually earn points -- some may already be
        # credited to this player on this cell within the cooldown
        # window -- is decided inside apply_paint(), not here.
        repeater_ids = [e.repeater_id for e in entries]
        if not repeater_ids:
            counters["pings_no_repeaters"] += 1

        # 10. MeshCore scoring, inside the same write transaction as the
        # rest of this batch. A scoring failure must not lose the
        # counters already recorded above or abort the rest of the
        # batch -- log it and keep going.
        if team is not None:
            paint_result = None
            try:
                paint_result = mc_scoring.apply_paint(
                    conn, season_id, player_id, team, cell, ts, repeater_ids,
                    settings.mc_points_per_repeater, settings.mc_max_points_per_ping,
                    PROTOCOL, received_at, by_air,
                )
            except Exception:
                log.exception(
                    "mc scoring: apply_paint failed for player %d cell %s",
                    player_id, cell,
                )

            # Places Worth Going (app/place_scoring.py). Same write
            # transaction, own try/except so a places bug can never cost
            # a batch its square scoring above. Gated on apply_paint()'s
            # outcome (paint_result.outcome), not on repeater_ids/by_air
            # directly -- see credit_places()'s docstring for why a
            # square-scoring "cooldown" ping still credits a place. If
            # apply_paint itself raised above, paint_result is still
            # None -- there is no outcome to gate on, so this is skipped
            # rather than guessed at.
            if paint_result is not None:
                try:
                    credit_places(conn, player_id, cell, ts, paint_result.outcome, by_air, PROTOCOL)
                except Exception:
                    log.exception(
                        "place scoring: credit_places failed for player %d cell %s",
                        player_id, cell,
                    )

    # ---- housekeeping ---------------------------------------------------

    async def _maybe_housekeeping(self) -> None:
        now = time.monotonic()
        if now - self._last_housekeeping < _HOUSEKEEPING_INTERVAL_S:
            return
        self._last_housekeeping = now
        async with _WRITE_LOCK:
            removed_pings, removed_stats, removed_credits = await asyncio.to_thread(self._housekeeping_sync)
        log.info(
            "mc ingest housekeeping: removed %d stale player_cell_ping rows, "
            "%d stale player_ingest_stat rows, %d stale player_cell_repeater_credit rows",
            removed_pings, removed_stats, removed_credits,
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
            # Same retention window as player_cell_ping above -- this
            # table is the cooldown's bookkeeping, same role player_cell_ping
            # used to play alone, so a row past the ping retention window
            # is exactly as stale as a player_cell_ping row past it (the
            # cooldown window itself, mc_cooldown_seconds, is minutes; the
            # retention window is days, ample margin either way).
            cur3 = conn.execute(
                "DELETE FROM player_cell_repeater_credit WHERE seen_at < ?", (ping_cutoff,)
            )
            removed_credits = cur3.rowcount
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return removed_pings, removed_stats, removed_credits


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
