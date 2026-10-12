"""Where a check-in was heard: a map position for each CoreScope/Beacon
check-in award, so regions can count check-ins.

A check-in message has no location of its own. What the MeshCore feeds do
have is the path the message took: the first repeater that heard the
sender is where the player was, give or take a radio range. This module
finds that repeater (or the observer that heard the sender directly), and
stores one row per award in checkin_award_place:

  first_hop           the first repeater on the path, from the feed's own
                      path data, resolved to a position without ambiguity
  observer            heard directly (zero hops): the observer's position
                      (CoreScope publishes observer coordinates)
  observer_iata       heard directly, Beacon: its observers carry only an
                      airport (IATA) code, kept as region_code
  est_recent_checkin  the message or its path is gone: the position of the
                      player's nearest traced check-in (same net first,
                      later in time preferred)
  est_recent_square   no traced check-in: the player's most recent claimed
                      square on that board
  unplaced            nothing to go on (counts only in All regions)

An ambiguous repeater prefix is never guessed: that observation is skipped
and the next one is tried. Meshtastic (meshview/MQTT) awards are not
placed; they count only in All regions.

Two users share the engine below: run_forever() (worker role) places new
awards in the background, never in the polling path; and
app/checkin_place_backfill.py places history. Both are polite to the feeds:
one request a second per host, a MeshWars User-Agent, bounded timeouts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from typing import Any, NamedTuple
from urllib.parse import quote

import httpx

from .db import connect
from .grid import cell_center, cell_id as grid_cell_id

log = logging.getLogger("checkin_place")

USER_AGENT = "meshwars/1.0 (check-in placement; +https://meshwars.com)"
MIN_INTERVAL_S = 1.0
KINDS = ("corescope", "beacon")
METHODS = ("first_hop", "observer", "observer_iata", "est_recent_checkin", "est_recent_square", "unplaced")
TRACED = ("first_hop", "observer")

# Live loop: a fresh award's trace can take a minute to appear on the feed.
LOOP_SECONDS = 60
FIRST_ATTEMPT_AFTER_S = 90
RETRY_BACKOFF_S = 300          # times the number of attempts so far
MAX_ATTEMPTS = 5               # then fall back to estimation
LIVE_WINDOW_S = 3 * 86400      # older awards are the backfill's job
LOOP_BATCH = 15


class PlaceResult(NamedTuple):
    method: str
    lat: float | None
    lon: float | None
    region_code: str | None
    detail: str
    source_hash: str | None


class Transient(Exception):
    """A feed request failed in a way that says nothing about the award
    (timeout, 5xx): try again later, do not count it as 'not found'."""


def usable_point(lat: Any, lon: Any) -> tuple[float, float] | None:
    """(lat, lon) when both are real numbers, in range, and not the (0, 0)
    some nodes report when they have no position; else None."""
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return float(lat), float(lon)


# ---- polite HTTP -----------------------------------------------------------


class HostClient:
    """One feed host, at most one request per `min_interval` seconds."""

    def __init__(self, base_url: str, *, client: httpx.AsyncClient | None = None,
                 min_interval: float = MIN_INTERVAL_S) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url, timeout=httpx.Timeout(15.0, connect=5.0),
            headers={"Accept": "application/json", "User-Agent": USER_AGENT}, follow_redirects=True)
        self._min_interval = min_interval
        self._next_at = 0.0
        self._lock = asyncio.Lock()
        self.requests = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, path: str, params: dict | None = None) -> Any | None:
        """Parsed JSON, or None when the resource does not exist (404).
        Raises Transient on anything else that went wrong."""
        async with self._lock:
            wait = self._next_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                self.requests += 1
                r = await self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                raise Transient(f"{path}: {type(exc).__name__}") from exc
            finally:
                self._next_at = time.monotonic() + self._min_interval
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise Transient(f"{path}: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise Transient(f"{path}: not JSON") from exc


# ---- CoreScope ---------------------------------------------------------------


def _hex_prefixes(path_json: Any) -> list[str] | None:
    if isinstance(path_json, str):
        try:
            path_json = json.loads(path_json)
        except ValueError:
            return None
    if not isinstance(path_json, list) or not all(isinstance(p, str) and p for p in path_json):
        return None
    return path_json


class CoreScopeSource:
    """One CoreScope channel. Messages are paged once and kept (a channel
    holds a few hundred), then each award is a lookup plus a trace."""

    def __init__(self, host: HostClient, channel: str) -> None:
        self.host = host
        self.channel = channel
        self._messages: dict[int, dict] | None = None
        self._loaded_at = 0.0
        self._observers: list[dict] | None = None
        self._hops: dict[str, tuple[float, float, str] | None] = {}

    async def messages(self, max_age_s: float = 300.0) -> dict[int, dict]:
        if self._messages is not None and time.monotonic() - self._loaded_at < max_age_s:
            return self._messages
        out: dict[int, dict] = {}
        offset = 0
        for _ in range(40):
            data = await self.host.get(f"/api/channels/{quote(self.channel, safe='')}/messages",
                                       {"limit": 100, "offset": offset})
            msgs = data.get("messages") if isinstance(data, dict) else None
            if not isinstance(msgs, list) or not msgs:
                break
            for m in msgs:
                if isinstance(m, dict) and isinstance(m.get("packetId"), int):
                    out[m["packetId"]] = m
            offset += len(msgs)
            total = data.get("total")
            if isinstance(total, int) and offset >= total:
                break
        self._messages, self._loaded_at = out, time.monotonic()
        return out

    async def _observer_list(self) -> list[dict]:
        if self._observers is None:
            data = await self.host.get("/api/observers")
            if isinstance(data, dict):
                data = data.get("observers")
            self._observers = [o for o in data if isinstance(o, dict)] if isinstance(data, list) else []
        return self._observers

    async def _observer_point(self, trace: dict) -> tuple[float, float, str] | None:
        for o in await self._observer_list():
            if o.get("id") == trace.get("observer") or (
                    trace.get("observer_name") and o.get("name") == trace.get("observer_name")):
                pt = usable_point(o.get("lat"), o.get("lon"))
                if pt:
                    return pt[0], pt[1], f"{o.get('name') or trace.get('observer_name')} (observer)"
        return None

    async def _first_hop_point(self, prefix: str) -> tuple[float, float, str] | None:
        key = prefix.upper()
        if key in self._hops:
            return self._hops[key]
        data = await self.host.get("/api/resolve-hops", {"hops": key})
        result = None
        entry = None
        resolved = data.get("resolved") if isinstance(data, dict) else None
        if isinstance(resolved, dict):
            entry = resolved.get(key) or resolved.get(prefix) or resolved.get(key.lower())
        if isinstance(entry, dict):
            cands = entry.get("candidates")
            conflicts = entry.get("conflicts")
            conf = str(entry.get("confidence") or "")
            # Unambiguous only: no conflicting nodes, exactly one candidate,
            # and the feed itself calls the prefix unique.
            if not conflicts and isinstance(cands, list) and len(cands) == 1 and conf.startswith("unique") \
                    and isinstance(cands[0], dict):
                pt = usable_point(cands[0].get("lat"), cands[0].get("lon"))
                if pt:
                    pub = str(entry.get("pubkey") or "")[:8]
                    result = (pt[0], pt[1], f"{entry.get('name') or key} {key} {pub} {conf}".strip())
        self._hops[key] = result
        return result

    async def resolve(self, message_id: str) -> PlaceResult | None:
        """None: the message is gone or has no usable trace."""
        try:
            pid = int(message_id)
        except (TypeError, ValueError):
            return None
        msg = (await self.messages()).get(pid)
        if not msg or not msg.get("packetHash"):
            return None
        phash = str(msg["packetHash"])
        data = await self.host.get(f"/api/traces/{quote(phash, safe='')}")
        traces = data.get("traces") if isinstance(data, dict) else None
        if not isinstance(traces, list):
            return None
        obs = []
        for t in traces:
            path = _hex_prefixes(t.get("path_json")) if isinstance(t, dict) else None
            if path is not None:
                obs.append((len(path), t, path))
        obs.sort(key=lambda x: x[0])  # fewest hops first: closest to the sender
        for hops, t, path in obs:
            if hops == 0:
                got = await self._observer_point(t)
                method = "observer"
            else:
                got = await self._first_hop_point(path[0])
                method = "first_hop"
            if got:
                return PlaceResult(method, got[0], got[1], None, got[2], phash)
        return None


# ---- Beacon ------------------------------------------------------------------


class BeaconSource:
    """One Beacon channel. History is long (back to 2024) and cursor-paged
    newest first, so an index of pages already read is kept and extended
    only as far back as the award being looked up."""

    def __init__(self, host: HostClient, channel: str) -> None:
        self.host = host
        self.channel = channel
        self._channel_id: int | None = None
        self._index: dict[int, dict] = {}
        self._cursor: str | None = None
        self._started = False
        self._exhausted = False
        self._oldest_ms: float | None = None

    async def _channel(self) -> int | None:
        if self._channel_id is None:
            data = await self.host.get("/api/v1/channels", {"limit": 1000})
            for it in (data.get("items") if isinstance(data, dict) else None) or []:
                if isinstance(it, dict) and it.get("keyKnown") is True and it.get("name") == self.channel \
                        and isinstance(it.get("id"), int):
                    self._channel_id = it["id"]
        return self._channel_id

    async def _page(self) -> bool:
        cid = await self._channel()
        if cid is None or self._exhausted:
            return False
        params: dict = {"limit": 100}
        if self._cursor:
            params["cursor"] = self._cursor
        data = await self.host.get(f"/api/v1/channels/{cid}/messages", params)
        items = data.get("items") if isinstance(data, dict) else None
        self._started = True
        if not isinstance(items, list) or not items:
            self._exhausted = True
            return False
        for it in items:
            if isinstance(it, dict) and isinstance(it.get("id"), int):
                self._index[it["id"]] = it
                sent = it.get("sentAt")
                if isinstance(sent, (int, float)):
                    self._oldest_ms = sent if self._oldest_ms is None else min(self._oldest_ms, sent)
        nxt = data.get("nextCursor")
        if not data.get("hasMore") or not nxt:
            self._exhausted = True
        else:
            self._cursor = str(nxt)
        return True

    async def message(self, message_id: str, ts: int | None) -> dict | None:
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            return None
        for _ in range(400):
            if mid in self._index:
                return self._index[mid]
            # Stop once we are well past the award's own time.
            if ts is not None and self._oldest_ms is not None and self._oldest_ms < (ts - 3600) * 1000:
                return None
            if not await self._page():
                return None
        return self._index.get(mid)

    async def resolve(self, message_id: str, ts: int | None = None) -> PlaceResult | None:
        msg = await self.message(message_id, ts)
        if not msg or not msg.get("packetHash"):
            return None
        phash = str(msg["packetHash"])
        data = await self.host.get(f"/api/v1/packets/{quote(phash, safe='')}")
        observations = data.get("observations") if isinstance(data, dict) else None
        if not isinstance(observations, list):
            return None
        obs = []
        for o in observations:
            if not isinstance(o, dict):
                continue
            pl = o.get("pathLength")
            hops = pl.get("hopCount") if isinstance(pl, dict) else None
            if isinstance(hops, int) and hops >= 0:
                obs.append((hops, o))
        obs.sort(key=lambda x: x[0])
        for hops, o in obs:
            if hops == 0:
                iata = str(o.get("iata") or "").strip().upper()
                if iata:
                    return PlaceResult("observer_iata", None, None, iata,
                                       f"{o.get('observerName') or '?'} (observer, {iata})", phash)
                continue
            rp = o.get("resolvedPath")
            first = rp[0] if isinstance(rp, list) and rp and isinstance(rp[0], dict) else None
            if not first or first.get("confidence") != "high":
                continue  # ambiguous or unresolved: never guess
            nodes = first.get("nodes")
            if not isinstance(nodes, list) or len(nodes) != 1 or not isinstance(nodes[0], dict):
                continue
            pt = usable_point(nodes[0].get("latitude"), nodes[0].get("longitude"))
            if pt:
                n = nodes[0]
                return PlaceResult("first_hop", pt[0], pt[1], None,
                                   f"{n.get('name') or '?'} {str(n.get('publicKey') or '')[:8]} high", phash)
        return None


# ---- the engine ---------------------------------------------------------------


class Placer:
    """Sources per (kind, host, channel), created on demand."""

    def __init__(self, client_factory=None) -> None:
        self._factory = client_factory
        self._hosts: dict[str, HostClient] = {}
        self._sources: dict[tuple, CoreScopeSource | BeaconSource] = {}

    def _host(self, url: str) -> HostClient:
        if url not in self._hosts:
            self._hosts[url] = self._factory(url) if self._factory else HostClient(url)
        return self._hosts[url]

    def source(self, kind: str, url: str, channel: str):
        key = (kind, url, channel)
        if key not in self._sources:
            cls = CoreScopeSource if kind == "corescope" else BeaconSource
            self._sources[key] = cls(self._host(url), channel)
        return self._sources[key]

    async def trace(self, award: sqlite3.Row | dict) -> PlaceResult | None:
        src = self.source(award["kind"], award["connector_url"], award["channel"])
        if award["kind"] == "corescope":
            return await src.resolve(award["message_id"])
        return await src.resolve(award["message_id"], award["message_ts"])

    def requests(self) -> int:
        return sum(h.requests for h in self._hosts.values())

    async def aclose(self) -> None:
        for h in self._hosts.values():
            await h.aclose()


# ---- database side ------------------------------------------------------------

_AWARD_SQL = (
    "SELECT a.season_id, a.player_id, a.net_date, a.points, a.message_id, a.awarded_at, a.message_ts, "
    "       a.net_id, a.protocol, n.kind, n.connector_url, n.channel, "
    "       p.method AS p_method, p.attempts AS p_attempts, p.last_attempt_at AS p_last "
    "  FROM mc_checkin_award a "
    "  JOIN checkin_net n ON n.id = a.net_id AND n.kind IN ('corescope','beacon') "
    "  LEFT JOIN checkin_award_place p "
    "    ON p.season_id = a.season_id AND p.player_id = a.player_id AND p.net_date = a.net_date "
)


def select_awards(conn, *, live_now: int | None = None, net_id: int | None = None,
                  season_id: int | None = None, replace: bool = False, limit: int | None = None) -> list[sqlite3.Row]:
    """Awards that still need a placement, oldest first.

    live_now set: the background loop's view -- awards from the last three
    days that are unplaced (no row, or an 'unplaced' row not yet at the
    attempt cap and past its backoff).
    live_now None: the backfill's view -- every award without a placement
    or with an 'unplaced' one; replace=True takes all of them."""
    where, params = [], []
    if net_id is not None:
        where.append("a.net_id = ?")
        params.append(net_id)
    if season_id is not None:
        where.append("a.season_id = ?")
        params.append(season_id)
    if live_now is not None:
        where.append("a.awarded_at <= ? AND a.awarded_at >= ?")
        params += [live_now - FIRST_ATTEMPT_AFTER_S, live_now - LIVE_WINDOW_S]
        where.append("(p.method IS NULL OR (p.method = 'unplaced' AND p.attempts < ?))")
        params.append(MAX_ATTEMPTS)
    elif not replace:
        where.append("(p.method IS NULL OR p.method = 'unplaced')")
    sql = _AWARD_SQL + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY a.awarded_at, a.player_id"
    rows = conn.execute(sql, params).fetchall()
    if live_now is not None:
        rows = [r for r in rows if r["p_last"] is None
                or r["p_last"] + RETRY_BACKOFF_S * max(1, r["p_attempts"] or 1) <= live_now]
    return rows[:limit] if limit else rows


def estimate(conn, award) -> PlaceResult:
    """Placement for an award whose message or path is gone. Only TRACED
    placements are used as the source, never another estimate."""
    # 1. the player's nearest traced check-in: same net first, then any net;
    #    nearest in time, a later one before an earlier one at equal distance.
    for same_net in (True, False):
        sql = ("SELECT p.lat, p.lon, p.cell_id, p.region_code, p.detail, a.net_date, a.awarded_at "
               "  FROM checkin_award_place p JOIN mc_checkin_award a "
               "    ON a.season_id = p.season_id AND a.player_id = p.player_id AND a.net_date = p.net_date "
               " WHERE p.player_id = ? AND a.protocol = ? AND p.method IN ('first_hop','observer','observer_iata') "
               "   AND NOT (p.season_id = ? AND p.net_date = ?)")
        params: list = [award["player_id"], award["protocol"], award["season_id"], award["net_date"]]
        if same_net:
            sql += " AND a.net_id = ?"
            params.append(award["net_id"])
        best = None
        for r in conn.execute(sql, params):
            gap = r["awarded_at"] - award["awarded_at"]
            key = (abs(gap), 0 if gap >= 0 else 1)
            if best is None or key < best[0]:
                best = (key, r)
        if best:
            r = best[1]
            return PlaceResult("est_recent_checkin", r["lat"], r["lon"], r["region_code"],
                               f"like the {r['net_date']} check-in: {r['detail']}", None)
    # 2. the player's most recent claimed square on this board.
    row = conn.execute(
        "SELECT l.cell_id FROM mc_tile_capture_log l JOIN mc_season s ON s.id = l.season_id "
        " WHERE l.by_player_id = ? AND s.protocol = ? AND l.event_type = 'capture' ORDER BY l.ts DESC LIMIT 1",
        (award["player_id"], award["protocol"])).fetchone()
    if row:
        lat, lon = cell_center(row["cell_id"])
        return PlaceResult("est_recent_square", lat, lon, None, f"most recent claimed square {row['cell_id']}", None)
    return PlaceResult("unplaced", None, None, None, "nothing to go on", None)


def write_placement(award, result: PlaceResult, *, attempts: int, now: int | None = None) -> None:
    """One short write transaction (retried when the database is busy)."""
    from .regions import _retry_busy  # the project's one busy-retry helper
    now = int(time.time()) if now is None else now
    cell = grid_cell_id(result.lat, result.lon) if result.lat is not None and result.lon is not None else None

    def write() -> None:
        c = connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute(
                    "INSERT INTO checkin_award_place(season_id, player_id, net_date, lat, lon, cell_id, region_code, "
                    " method, detail, source_hash, attempts, placed_at, last_attempt_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(season_id, player_id, net_date) DO UPDATE SET lat=excluded.lat, lon=excluded.lon, "
                    " cell_id=excluded.cell_id, region_code=excluded.region_code, method=excluded.method, "
                    " detail=excluded.detail, source_hash=excluded.source_hash, attempts=excluded.attempts, "
                    " placed_at=excluded.placed_at, last_attempt_at=excluded.last_attempt_at",
                    (award["season_id"], award["player_id"], award["net_date"], result.lat, result.lon, cell,
                     result.region_code, result.method, result.detail, result.source_hash, attempts,
                     None if result.method == "unplaced" else now, now))
                c.execute("COMMIT")
            except BaseException:
                try:
                    c.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        finally:
            c.close()

    _retry_busy(write)


async def place_one(placer: Placer, award, *, final_after: int | None) -> tuple[str, str | None]:
    """Try to place one award. Returns (outcome method or 'error', detail).
    A traced result is written at once. Not found -> an 'unplaced' row with
    attempts+1; once attempts reach `final_after` (None = immediately), the
    estimate is written instead. A transient feed error writes nothing in
    the backfill (final_after None) and counts as an attempt in the loop."""
    attempts = (award["p_attempts"] or 0)
    try:
        result = await placer.trace(award)
        transient = False
    except Transient as exc:
        log.warning("checkin_place: %s", exc)
        result, transient = None, True
    if result is not None:
        await asyncio.to_thread(write_placement, award, result, attempts=attempts + 1)
        return result.method, result.detail
    if transient and final_after is None:
        return "error", None
    attempts += 1
    if final_after is None or attempts >= final_after:
        est = await asyncio.to_thread(_estimate_sync, award)
        await asyncio.to_thread(write_placement, award, est, attempts=attempts)
        return est.method, est.detail
    await asyncio.to_thread(write_placement, award,
                            PlaceResult("unplaced", None, None, None, "not traced yet", None), attempts=attempts)
    return "retry", None


def _estimate_sync(award) -> PlaceResult:
    c = connect()
    try:
        return estimate(c, award)
    finally:
        c.close()


def _select_sync(**kw):
    c = connect()
    try:
        return select_awards(c, **kw)
    finally:
        c.close()


async def process_due(placer: Placer, now: int | None = None, limit: int = LOOP_BATCH) -> int:
    """One pass of the background loop. Returns awards handled."""
    now = int(time.time()) if now is None else now
    rows = await asyncio.to_thread(lambda: _select_sync(live_now=now, limit=limit))
    for row in rows:
        try:
            await place_one(placer, row, final_after=MAX_ATTEMPTS)
        except Exception:
            log.exception("checkin_place: placing %s/%s/%s failed", row["season_id"], row["player_id"], row["net_date"])
    return len(rows)


async def run_forever() -> None:
    """Worker-role loop: places new CoreScope/Beacon check-ins in the
    background, a few a minute, so the poller itself never waits on it."""
    log.info("check-in placement loop starting")
    placer = Placer()
    try:
        while True:
            # Sleep first: nothing is due in the first minute after a start
            # (awards wait FIRST_ATTEMPT_AFTER_S anyway), and a process that
            # lives only briefly never starts a worker thread.
            await asyncio.sleep(LOOP_SECONDS)
            try:
                await process_due(placer)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("checkin_place: loop cycle failed")
    finally:
        await placer.aclose()
