"""Polling loop: fetch new position packets from meshview, qualify them
(hops <= 1), encode sender location to geohash, and update tile ownership.

Tile color for the current season is determined by the team of the most
recent qualifying sender in that tile. Unassigned senders paint GREEN.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from typing import Iterable

import pygeohash

from .config import settings
from .db import WriteSession, connect, get_cursor, set_cursor
from .meshview_client import (
    MeshviewClient,
    extract_feeder_id,
    extract_node_id,
    extract_packet_id,
    extract_position,
    extract_timestamp,
    hop_count,
    is_via_mqtt,
)
from .draft import assign_new_node
from .scoring import (
    decayed_score,
    get_team_score,
    in_defense_window,
    is_first_paint_for_node,
    record_capture,
    upsert_team_score,
)
from .seasons import (
    ensure_initial_season,
    get_active_season,
    get_team_assignments,
    maybe_close_and_roll,
)

log = logging.getLogger("ingest")

GEOHASH_TILE_PRECISION = 6
GEOHASH_SAMPLE_PRECISION = 8

CURSOR_LAST_IMPORT_US = "last_position_import_us"  # microseconds, monotonic


class Ingestor:
    def __init__(self, client: MeshviewClient):
        self.client = client
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        log.info("ingest loop starting; poll=%ds", settings.poll_interval_seconds)
        await ensure_initial_season()
        # Snapshot roster on startup so we have something to render
        await self._refresh_roster()
        try:
            await self._backfill()
        except Exception as e:
            log.warning("backfill failed: %s", e)

        while not self._stop.is_set():
            try:
                # Always check if the active season has rolled over first.
                rolled = await maybe_close_and_roll()
                if rolled:
                    await self._refresh_roster()

                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("ingest cycle failed: %s", e)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=settings.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass
        log.info("ingest loop stopped")

    async def _refresh_roster(self) -> None:
        """Snapshot active nodes from meshview into node_seen for this season."""
        try:
            nodes = await self.client.nodes(days_active=settings.season_days)
        except Exception as e:
            log.warning("nodes() refresh failed: %s", e)
            return
        if not nodes:
            return

        conn = connect()
        try:
            active = get_active_season(conn)
            if not active:
                return
            season_id = active["id"]
        finally:
            conn.close()

        ts = int(time.time())
        async with WriteSession() as conn:
            for n in nodes:
                node_id = extract_node_id(n) or _node_id_from_dict(n)
                if node_id is None:
                    continue
                lat = n.get("last_lat") or n.get("lat")
                lon = n.get("last_long") or n.get("lon") or n.get("longitude")
                if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                    if abs(lat) > 1000:
                        lat = lat / 1e7
                        lon = lon / 1e7
                else:
                    lat = lon = None

                long_name = (n.get("long_name") or n.get("name") or f"!{node_id:08x}") if node_id else "?"
                short_name = n.get("short_name")
                role = n.get("role")

                conn.execute(
                    "INSERT INTO node_seen(season_id, node_id, name, short_name, lat, lon, last_seen, role) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(season_id, node_id) DO UPDATE SET "
                    "  name=excluded.name, short_name=excluded.short_name, "
                    "  lat=COALESCE(excluded.lat, node_seen.lat), "
                    "  lon=COALESCE(excluded.lon, node_seen.lon), "
                    "  last_seen=excluded.last_seen, role=excluded.role",
                    (season_id, node_id, long_name, short_name, lat, lon, ts, role),
                )
        log.info("roster refreshed: %d nodes", len(nodes))


    async def _backfill(self) -> None:
        """On startup, pull position packets from the last N hours so tiles
        fill in immediately. Pages backwards by import_time_us.
        """
        import time as _time
        cutoff_s = int(_time.time()) - (settings.backfill_hours * 3600)
        cutoff_us = cutoff_s * 1_000_000
        log.info("backfilling last %d hours (cutoff_s=%d)", settings.backfill_hours, cutoff_s)

        conn = connect()
        try:
            active = get_active_season(conn)
            if not active:
                return
            season_id = active["id"]
            teams = get_team_assignments(conn, season_id)
        finally:
            conn.close()

        from .meshview_client import _unwrap_list

        total_processed = 0
        oldest_import_us: int | None = None
        oldest_packet_id: int | None = None
        last_cursor: int | None = None

        for page in range(50):
            try:
                params: dict = {
                    "portnum": settings.position_app_portnum,
                    "limit": 100,
                }
                if oldest_packet_id is not None:
                    # freq51 meshview pagination: before_id walks backwards
                    # in import-time order using a packet id as the cursor.
                    params["before_id"] = oldest_packet_id
                data = await self.client._get("/api/packets", params)
                packets = _unwrap_list(data, ("packets", "data", "results"))
            except Exception as e:
                log.warning("backfill page %d failed: %s", page, e)
                break
            if not packets:
                break

            # Track oldest by import_time_us; capture that packet's id for next cursor
            iu_values = [(p.get("import_time_us") or 0, p.get("id")) for p in packets]
            iu_values.sort()
            page_min_us = iu_values[0][0] if iu_values else 0
            page_max_us = iu_values[-1][0] if iu_values else 0
            page_oldest_pid = iu_values[0][1] if iu_values else None

            # Filter packets to those within our 24h window
            in_window = [p for p in packets if (p.get("import_time_us") or 0) >= cutoff_us]

            if in_window:
                tasks = []
                for pkt in in_window:
                    pid = extract_packet_id(pkt)
                    if pid is None:
                        continue
                    tasks.append(self._classify_and_process(pkt, pid, season_id, teams))
                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    total_processed += sum(1 for r in results if r is True)

            log.info("backfill page %d: got=%d in_window=%d min_us=%d max_us=%d",
                     page, len(packets), len(in_window), page_min_us, page_max_us)

            # Stop conditions
            if page_min_us < cutoff_us:
                break  # walked past the 24h window
            if last_cursor is not None and page_oldest_pid == last_cursor:
                log.info("backfill: pagination not advancing (same cursor), stopping")
                break

            last_cursor = page_oldest_pid
            oldest_packet_id = page_oldest_pid
            oldest_import_us = page_min_us

        # Set forward cursor to the newest packet we saw
        if oldest_import_us is not None:
            async with WriteSession() as conn:
                # Use current time as the forward cursor; we'll dedup via processed_packet
                set_cursor(conn, CURSOR_LAST_IMPORT_US, str(int(_time.time() * 1_000_000)))
        log.info("backfill done: processed=%d", total_processed)

    async def _poll_once(self) -> None:
        """Fetch the newest 100 position packets and process anything we
        haven't seen yet. Dedup is via the processed_packet table; we
        don't need a server-side cursor because the API always returns
        newest-first and 100 packets is plenty of overlap between polls.
        """
        conn = connect()
        try:
            active = get_active_season(conn)
            if not active:
                log.warning("no active season; skipping poll")
                return
            season_id = active["id"]
            teams = get_team_assignments(conn, season_id)
        finally:
            conn.close()

        try:
            packets = await self.client.packets(
                portnum=settings.position_app_portnum,
                since_id=None,
                limit=100,
            )
        except Exception as e:
            log.warning("packets() failed: %s", e)
            return

        if not packets:
            return

        # Process oldest->newest for clean last_report_ts comparisons
        packets.sort(key=lambda p: extract_timestamp(p))
        max_import_us = 0

        tasks = []
        for pkt in packets:
            pid = extract_packet_id(pkt)
            if pid is None:
                continue
            iu = pkt.get("import_time_us") or 0
            if iu > max_import_us:
                max_import_us = iu
            tasks.append(self._classify_and_process(pkt, pid, season_id, teams))

        if not tasks:
            return

        results = await asyncio.gather(*tasks, return_exceptions=True)
        n_processed = sum(1 for r in results if r is True)
        n_skipped = sum(1 for r in results if r is False)

        if max_import_us:
            async with WriteSession() as conn:
                set_cursor(conn, CURSOR_LAST_IMPORT_US, str(max_import_us))

        log.info(
            "poll: packets=%d processed=%d skipped=%d max_import_us=%d",
            len(packets), n_processed, n_skipped, max_import_us,
        )

    async def _classify_and_process(
        self,
        packet: dict,
        packet_id: int,
        season_id: int,
        teams: dict[int, str],
    ) -> bool:
        # Skip already-processed packets
        conn = connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM processed_packet WHERE packet_id = ?", (packet_id,)
            ).fetchone()
            if row:
                return False
        finally:
            conn.close()

        sender_id = extract_node_id(packet)
        pos = extract_position(packet)
        ts = extract_timestamp(packet)
        if sender_id is None or pos is None:
            await self._mark_processed(packet_id)
            return False
        lat, lon = pos

        # Fetch reception rows
        seen = await self.client.packets_seen(packet_id)
        if not seen:
            await self._mark_processed(packet_id)
            return False

        # Filter qualifying receptions: at least one with hops <= settings.max_hops
        # AND not MQTT-only (we want real RF reach)
        feeders: list[int] = []
        best_snr: float | None = None
        best_rssi: float | None = None
        qualified = False
        for row in seen:
            hops = hop_count(row)
            if hops is None:
                continue
            if hops > settings.max_hops:
                continue
            if is_via_mqtt(row):
                # If the only receptions are MQTT-injected, this isn't real RF reach.
                continue
            qualified = True
            fid = extract_feeder_id(row)
            if fid is not None and fid not in feeders:
                feeders.append(fid)
            snr = row.get("rx_snr") or row.get("snr")
            rssi = row.get("rx_rssi") or row.get("rssi")
            if isinstance(snr, (int, float)):
                best_snr = max(best_snr, float(snr)) if best_snr is not None else float(snr)
            if isinstance(rssi, (int, float)):
                best_rssi = max(best_rssi, float(rssi)) if best_rssi is not None else float(rssi)

        if not qualified:
            await self._mark_processed(packet_id)
            return False

        # Determine owning team. If sender is unknown, auto-assign at the
        # moment they send a qualifying position packet. We've already
        # verified this packet has valid lat/lon and passed hop filtering
        # above, so the sender is by definition position-broadcasting.
        # Excluded roles (routers, bases) are still filtered out.
        team = teams.get(sender_id)
        if team not in ("RED", "BLUE"):
            # Look up the sender's role from node_seen (if known) to honor
            # the role exclusion. Unknown role -> treat as player.
            conn_r = connect()
            try:
                row = conn_r.execute(
                    "SELECT role FROM node_seen WHERE season_id = ? AND node_id = ?",
                    (season_id, sender_id),
                ).fetchone()
                role = row["role"] if row else None
                is_excluded = role is not None and role.upper() in settings.excluded_roles_set

                if is_excluded:
                    team = "GREEN"
                else:
                    counts = conn_r.execute(
                        "SELECT team, COUNT(*) AS c FROM team_assignment "
                        " WHERE season_id = ? GROUP BY team",
                        (season_id,),
                    ).fetchall()
                    cmap = {r["team"]: r["c"] for r in counts}
                    red_c = cmap.get("RED", 0)
                    blue_c = cmap.get("BLUE", 0)
            finally:
                conn_r.close()

            if team != "GREEN":
                team = assign_new_node(red_c, blue_c, sender_id)
                try:
                    async with WriteSession() as conn_w:
                        conn_w.execute(
                            "INSERT INTO team_assignment(season_id, node_id, team, activity_score) "
                            "VALUES (?, ?, ?, 0) "
                            "ON CONFLICT(season_id, node_id) DO NOTHING",
                            (season_id, sender_id, team),
                        )
                    teams[sender_id] = team
                    log.info("auto-assigned new node %d -> %s (red=%d blue=%d)",
                             sender_id, team, red_c, blue_c)
                except Exception as e:
                    log.warning("auto-assign persist failed for %d: %s", sender_id, e)
                    team = "GREEN"

        # Encode geohash
        tile_hash = pygeohash.encode(lat, lon, precision=GEOHASH_TILE_PRECISION)
        sample_hash = pygeohash.encode(lat, lon, precision=GEOHASH_SAMPLE_PRECISION)

        async with WriteSession() as conn:
            # Dedup check inside the lock (race-safe)
            row = conn.execute(
                "SELECT 1 FROM processed_packet WHERE packet_id = ?", (packet_id,)
            ).fetchone()
            if row:
                return False

            # Per-node-per-tile cooldown: ignore paints from the same node
            # on the same tile within COOLDOWN_SECONDS of the last paint.
            COOLDOWN_SECONDS = 300  # 5 minutes
            last_paint = conn.execute(
                "SELECT MAX(ts) AS last_ts FROM sample "
                " WHERE season_id = ? "
                "   AND sample_hash LIKE ? || '%' "
                "   AND sender_node_id = ? "
                "   AND ts <= ?",
                (season_id, tile_hash, sender_id, ts),
            ).fetchone()
            if last_paint and last_paint["last_ts"] and (ts - last_paint["last_ts"]) < COOLDOWN_SECONDS:
                # Cooldown active — record the packet as processed but skip
                # tile, sample, and activity updates.
                conn.execute(
                    "INSERT OR IGNORE INTO processed_packet(packet_id, processed_at) VALUES (?, ?)",
                    (packet_id, int(time.time())),
                )
                return False

            # ----- Fortress scoring -----
            # Compute this paint's point contribution for the painter's team.
            # +0.5 per packet, +1.0 if this is the first time this node has
            # painted this tile for this team this season.
            if team in ("RED", "BLUE"):
                first_time = is_first_paint_for_node(
                    conn, season_id, tile_hash, team, sender_id, ts
                )
                points = settings.score_per_packet + (
                    settings.score_per_unique_node if first_time else 0.0
                )

                # Update the painter team's score (with decay applied)
                current_painter_score = get_team_score(
                    conn, season_id, tile_hash, team, ts
                )
                new_painter_score = current_painter_score + points
                upsert_team_score(
                    conn, season_id, tile_hash, team, new_painter_score, ts
                )

                # Decide if a capture/flip happens.
                existing = conn.execute(
                    "SELECT owner_team, last_report_ts, rcv, rptr_json FROM tile "
                    " WHERE season_id = ? AND geohash = ?",
                    (season_id, tile_hash),
                ).fetchone()
                owner = existing["owner_team"] if existing else None
                opponent = "BLUE" if team == "RED" else "RED"

                capture = False
                if existing is None:
                    # Brand new tile - painter captures immediately
                    capture = True
                elif owner == team:
                    # Same team reinforcing - no capture, just reinforce
                    capture = False
                else:
                    # Attacker's team paints opponent's tile - check window + score
                    if in_defense_window(conn, season_id, tile_hash, ts):
                        capture = False  # cannot flip during defense window
                    else:
                        defender_score = get_team_score(
                            conn, season_id, tile_hash, opponent, ts
                        )
                        if new_painter_score >= defender_score:
                            capture = True

                # Apply tile row update
                merged = _merge_unique(
                    existing["rptr_json"] if existing else "[]", feeders
                )
                new_owner = team if capture else (owner if existing else team)

                if existing is None:
                    conn.execute(
                        "INSERT INTO tile(season_id, geohash, rcv, lost, "
                        " last_sender_node_id, last_report_ts, last_snr, last_rssi, "
                        " owner_team, rptr_json, last_packet_id) "
                        "VALUES (?, ?, 1, 0, ?, ?, ?, ?, ?, ?, ?)",
                        (season_id, tile_hash, sender_id, ts,
                         best_snr, best_rssi, new_owner, json.dumps(merged), packet_id),
                    )
                else:
                    conn.execute(
                        "UPDATE tile SET "
                        "  rcv = rcv + 1, "
                        "  last_sender_node_id = ?, "
                        "  last_report_ts = ?, "
                        "  last_snr = ?, "
                        "  last_rssi = ?, "
                        "  owner_team = ?, "
                        "  rptr_json = ?, "
                        "  last_packet_id = ? "
                        " WHERE season_id = ? AND geohash = ?",
                        (sender_id, max(ts, existing["last_report_ts"]),
                         best_snr, best_rssi, new_owner,
                         json.dumps(merged), packet_id, season_id, tile_hash),
                    )

                if capture:
                    if existing is not None and owner in ("RED", "BLUE"):
                        upsert_team_score(
                            conn, season_id, tile_hash, owner, 0.0, ts
                        )
                    record_capture(conn, season_id, tile_hash, team, ts)
                    conn.execute(
                        "INSERT OR IGNORE INTO tile_capture_log"
                        "(season_id, geohash, ts, by_node_id, by_team, from_team, packet_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (season_id, tile_hash, ts, sender_id, team, owner, packet_id),
                    )
                    log.info(
                        "capture: %s tile=%s by node=%d score=%.2f (was %s)",
                        team, tile_hash, sender_id, new_painter_score, owner or "NEW",
                    )

            else:
                # team == 'GREEN' (excluded role or unassigned). Don't paint
                # color, but still bump packet counters so the tile shows
                # activity.
                existing = conn.execute(
                    "SELECT last_report_ts, rcv, rptr_json, owner_team FROM tile "
                    " WHERE season_id = ? AND geohash = ?",
                    (season_id, tile_hash),
                ).fetchone()
                merged = _merge_unique(
                    existing["rptr_json"] if existing else "[]", feeders
                )
                if existing is None:
                    conn.execute(
                        "INSERT INTO tile(season_id, geohash, rcv, lost, "
                        " last_sender_node_id, last_report_ts, last_snr, last_rssi, "
                        " owner_team, rptr_json, last_packet_id) "
                        "VALUES (?, ?, 1, 0, ?, ?, ?, ?, 'GREEN', ?, ?)",
                        (season_id, tile_hash, sender_id, ts,
                         best_snr, best_rssi, json.dumps(merged), packet_id),
                    )
                else:
                    conn.execute(
                        "UPDATE tile SET rcv = rcv + 1, rptr_json = ?, last_packet_id = ? "
                        " WHERE season_id = ? AND geohash = ?",
                        (json.dumps(merged), packet_id, season_id, tile_hash),
                    )

            # Sample
            conn.execute(
                "INSERT OR IGNORE INTO sample(season_id, sample_hash, sender_node_id, ts, snr, rssi, path_json, observed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    season_id, sample_hash, sender_id, ts,
                    best_snr, best_rssi, json.dumps(feeders),
                ),
            )

            # Activity bookkeeping
            conn.execute(
                "INSERT INTO activity(node_id, window_id, packet_count, last_seen) "
                "VALUES (?, ?, 1, ?) "
                "ON CONFLICT(node_id, window_id) DO UPDATE SET "
                "  packet_count = packet_count + 1, last_seen = excluded.last_seen",
                (sender_id, season_id, ts),
            )

            conn.execute(
                "INSERT OR IGNORE INTO processed_packet(packet_id, processed_at) VALUES (?, ?)",
                (packet_id, int(time.time())),
            )
        return True

    async def _mark_processed(self, packet_id: int) -> None:
        async with WriteSession() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO processed_packet(packet_id, processed_at) VALUES (?, ?)",
                (packet_id, int(time.time())),
            )


def _merge_unique(existing_json: str, additions: list[int]) -> list[int]:
    try:
        existing = json.loads(existing_json)
        if not isinstance(existing, list):
            existing = []
    except json.JSONDecodeError:
        existing = []
    seen = set(existing)
    for f in additions:
        if f not in seen:
            existing.append(f)
            seen.add(f)
    return existing


def _node_id_from_dict(n: dict) -> int | None:
    for k in ("node_id", "id"):
        v = n.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            s = v.lstrip("!")
            try:
                return int(s, 16) if any(c in "abcdefABCDEF" for c in s) else int(s)
            except ValueError:
                continue
    return None
