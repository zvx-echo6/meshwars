"""Polling loop: fetch new position packets from meshview, qualify them
(hops <= settings.max_hops, real RF reach only), and paint the sender's
grid cell through the shared MeshCore-model scoring path
(app/mc_scoring.py) -- for REGISTERED Meshtastic players only.

This board used to run its own geohash-tile fortress game (see the
now-retired tile/tile_score/tile_capture*/sample/activity/team_assignment
tables, app/scoring.py, and app/draft.py) that auto-enrolled every node
meshview ever saw. That is gone. Meshtastic now works exactly like
MeshCore: a packet only scores if its sender is a registered player
(app/api.py's /api/join flow, bound via player_node with protocol='mt'),
scoring happens on flat grid cells via app/mc_scoring.apply_paint(), and
the two boards share every table that mc_scoring.py and app/mc_ingest.py
already made protocol-aware (player_cell_ping, player_last_fix,
player_ingest_stat, repeater_observation/repeater_identity, and
mc_season).

The one thing that is genuinely different between the boards is what a
single ping's points are computed FROM, because of what meshview can
actually tell us:

- MeshCore (app/mc_ingest.py): the radio's own ping tells us how many
  repeaters the PLAYER heard (parsed straight out of heard_repeats /
  repeater_id in the ping payload).
- Meshtastic (here): meshview never tells us what the sender heard --
  only who heard the SENDER. Each /api/packets_seen/{id} row is one MQTT
  feeder (a meshview gateway node that heard the packet over the air and
  republished it to MQTT for meshview to consume) reporting a reception.
  So Meshtastic scores on the distinct feeder count instead -- the same
  idea as MeshCore, inverted, because that is the only direction meshview
  can observe. See settings.mt_points_per_feeder /
  settings.mt_max_points_per_ping in app/config.py.

Everything upstream of scoring -- the poll loop, the /api/packets fetch,
the /api/packets_seen/{id} reception lookup used to build the feeder
list, and the processed_packet dedup table -- is untouched; that
plumbing lives in app/meshview_client.py and stays exactly as it was.
The one exception is the node_seen roster refresh (_refresh_roster
below): it still records the same nodes the same way, but is now keyed
on the active 'mt' mc_season instead of the retired `season` table,
since that table is frozen history now and no longer rolls forward.

settings.mt_paint_source (app/config.py) decides whether THIS module or
app/freqmapper_ingest.py paints the Meshtastic board. Default is
"meshview" -- unchanged from every line above. When it is "freqmapper"
instead, the two position-painting paths below -- _poll_once's
portnum=3 fetch and _backfill -- are gated off entirely: neither fetches
packets, scores, nor writes player_cell_ping. _refresh_roster (node
names/roster) and _poll_nodeinfo (portnum=4, mt_node_key) are identity/
roster concerns, not scoring, and keep running either way -- see
app/freqmapper_ingest.py's own module docstring for the other half of
this switch.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

from . import mc_scoring
from .api import _node_hex
from .config import settings
from .db import WriteSession, connect, set_cursor
from .grid import cell_center, cell_id, distance_m, in_play_area, valid_coord
from .mc_ingest import RepeaterEntry, record_repeater_observations
from .place_scoring import credit_places
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

log = logging.getLogger("ingest")

PROTOCOL = "mt"

CURSOR_LAST_IMPORT_US = "last_position_import_us"  # microseconds, monotonic

# Per-player, per-day counters that have a Meshtastic meaning. Mirrors the
# column set app/mc_ingest.py writes to player_ingest_stat, minus two that
# do not translate here:
#   - batches: Meshtastic has no batch-submission concept (MeshCore's
#     wardriving app pushes a batch at a time; the poll loop here just
#     processes whatever packets meshview returns), so this stays 0.
#   - pings_wrong_owner: player_node's (protocol, node_ref) primary key
#     already makes node ownership 1:1 at registration time -- there is
#     no way for a Meshtastic packet to arrive "claiming" a node another
#     player already owns, so this never fires.
#   - pings_no_contact would be MeshCore's "ping isn't bound to any
#     player" case, but that table is keyed by player_id -- an
#     unregistered node's packets have no player to attribute a stat row
#     to at all, so this also stays 0 for 'mt' (see the registration gate
#     in _process_one_packet).
#
# pings_low_precision and pings_implausible_speed are new (2026-08-25,
# see app/config.py's mt_min_precision_bits/mt_max_speed_mps): the two
# Meshtastic-only game-integrity gates below. Both have a MeshCore column
# of the same name in player_ingest_stat (app/db.py), permanently 0 for
# protocol='mc' -- MeshCore's own precision/speed handling never rejects
# a ping (see mc_max_speed_mps's comment in app/config.py).
_STAT_COLUMNS = (
    "pings_accepted",
    "pings_no_contact",
    "pings_wrong_owner",
    "pings_duplicate",
    "pings_bad_coord",
    "pings_out_of_area",
    "pings_no_repeaters",
    "pings_low_precision",
    "pings_implausible_speed",
)


def _bump_player_stat(conn, player_id: int, day: int, **counters: int) -> None:
    """Upsert one player_ingest_stat row, incrementing whichever counters
    are passed; anything not passed increments by 0. Same shape as
    app/mc_ingest.py's per-batch upsert, just called once per packet here
    since (unlike MeshCore) a single poll cycle's packets belong to many
    different players, not one.
    """
    values = {c: counters.get(c, 0) for c in _STAT_COLUMNS}
    conn.execute(
        "INSERT INTO player_ingest_stat("
        "  player_id, protocol, day, batches, pings_accepted, pings_no_contact, "
        "  pings_wrong_owner, pings_duplicate, pings_bad_coord, pings_out_of_area, "
        "  pings_no_repeaters, pings_low_precision, pings_implausible_speed) "
        "VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(player_id, protocol, day) DO UPDATE SET "
        "  pings_accepted = pings_accepted + excluded.pings_accepted, "
        "  pings_no_contact = pings_no_contact + excluded.pings_no_contact, "
        "  pings_wrong_owner = pings_wrong_owner + excluded.pings_wrong_owner, "
        "  pings_duplicate = pings_duplicate + excluded.pings_duplicate, "
        "  pings_bad_coord = pings_bad_coord + excluded.pings_bad_coord, "
        "  pings_out_of_area = pings_out_of_area + excluded.pings_out_of_area, "
        "  pings_no_repeaters = pings_no_repeaters + excluded.pings_no_repeaters, "
        "  pings_low_precision = pings_low_precision + excluded.pings_low_precision, "
        "  pings_implausible_speed = pings_implausible_speed + excluded.pings_implausible_speed",
        (
            player_id, PROTOCOL, day,
            values["pings_accepted"], values["pings_no_contact"],
            values["pings_wrong_owner"], values["pings_duplicate"],
            values["pings_bad_coord"], values["pings_out_of_area"],
            values["pings_no_repeaters"], values["pings_low_precision"],
            values["pings_implausible_speed"],
        ),
    )


def _stat_day(ts: int) -> int:
    return int(datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d"))


# Matches a quoted protobuf text-format field body, e.g. the bit between
# the quotes in `public_key: "M\216\013..."`. `(?:[^"\\]|\\.)*` walks
# past an escaped quote or backslash without ending the match early --
# it does not need to understand octal escapes specifically, since a
# `\017`-style escape's backslash is consumed by `\\.` (matching just
# the backslash and the following `0`) and the trailing `17` falls
# through to the plain `[^"\\]` branch. Compiled at import time, same as
# app/node_ref.py's _NODE_REF_RE, since this runs once per packet on
# every NodeInfo poll.
_PB_STRING_FIELD = "{name}:\\s*\"((?:[^\"\\\\]|\\\\.)*)\""
_PUBLIC_KEY_RE = re.compile(_PB_STRING_FIELD.format(name="public_key"))
_LONG_NAME_RE = re.compile(_PB_STRING_FIELD.format(name="long_name"))


def _decode_pb_text_escapes(body: str) -> bytes | None:
    """Turn a protobuf text-format quoted body -- a Python str that
    contains literal backslash escape sequences (octal \\NNN, \\\\, \\")
    rather than actual control bytes -- into the raw bytes it denotes.

    latin-1 round-trips every byte value 0-255 as exactly one character,
    so encoding to latin-1 first is lossless for arbitrary binary data
    smuggled through as text; decoding that through 'unicode_escape'
    then interprets the literal backslash escapes (this is exactly what
    Python's own source parser does for byte-string literals); the final
    encode back to latin-1 recovers the raw bytes.
    """
    try:
        return body.encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return None


def parse_nodeinfo_public_key(payload: str) -> tuple[str | None, str | None]:
    """(public_key_hex, long_name) from a NodeInfo packet payload, or
    (None, None) when it carries neither.

    `payload` is meshview's protobuf text-format string (see
    app/meshview_client.py's parse_meshtastic_payload_text for the same
    format used by position packets) -- a Python str containing literal
    backslash escapes for any non-printable byte, not bytes that have
    already been unescaped. Each field is decoded independently: a
    present, well-formed long_name does not depend on public_key
    decoding cleanly, and vice versa.
    """
    if not isinstance(payload, str):
        return None, None

    public_key: str | None = None
    m = _PUBLIC_KEY_RE.search(payload)
    if m:
        raw = _decode_pb_text_escapes(m.group(1))
        # A real Curve25519 public key is exactly 32 bytes; anything
        # else (truncated match, a future key size, garbage) is not a
        # key we can trust the shape of, so treat it as absent rather
        # than record something that isn't actually a 32-byte key.
        if raw is not None and len(raw) == 32:
            public_key = raw.hex()

    long_name: str | None = None
    m = _LONG_NAME_RE.search(payload)
    if m:
        raw = _decode_pb_text_escapes(m.group(1))
        if raw is not None:
            long_name = raw.decode("utf-8", "replace")

    return public_key, long_name


class Ingestor:
    def __init__(self, client: MeshviewClient):
        self.client = client
        self._stop = asyncio.Event()
        # Logged once, not every poll cycle, the first time a cycle finds
        # painting gated off -- see _poll_once below.
        self._logged_paint_gated_once = False

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        log.info(
            "ingest loop starting; poll=%ds paint_source=%s (%s)",
            settings.poll_interval_seconds, settings.mt_paint_source,
            "meshview is painting" if settings.mt_paint_source == "meshview"
            else "meshview is NOT painting -- position poll and backfill are gated off",
        )
        # Snapshot roster on startup so we have something to render. This
        # also ensures the active 'mt' mc_season exists (see
        # _refresh_roster below) -- there is no separate
        # ensure_initial_season() call any more for the legacy `season`
        # table. That table is frozen history now (see app/api.py's
        # module docstring): node_seen and Meshtastic scoring both key
        # off mc_season instead.
        await self._refresh_roster()
        try:
            await self._backfill()
        except Exception as e:
            log.warning("backfill failed: %s", e)

        while not self._stop.is_set():
            try:
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
        """Snapshot active nodes from meshview into node_seen for this season.

        node_seen is keyed on the active MESHTASTIC ('mt') mc_season now,
        not the frozen legacy `season` table -- that table stopped being
        written the moment this board moved onto the shared player model
        (see app/api.py's module docstring), so pinning node_seen to it
        would freeze the roster at whatever season existed the moment
        this cutover shipped. mc_season is protocol-aware (app/mc_scoring.py)
        and keeps rolling forward on its own schedule; reading through it
        here keeps the roster on the same season Meshtastic scoring
        itself is using. Still records EVERY node meshview reports,
        registered or not -- it feeds the map's node markers, a display
        concern completely separate from who is allowed to score.
        """
        try:
            nodes = await self.client.nodes(days_active=settings.season_days)
        except Exception as e:
            log.warning("nodes() refresh failed: %s", e)
            return
        if not nodes:
            return

        ts = int(time.time())
        async with WriteSession() as conn:
            # Season bookkeeping, same as _poll_once/_backfill above --
            # see the matching comment there for why this has to run
            # inside WriteSession rather than a plain read. Cheap and
            # idempotent to repeat here: this only runs once, at startup,
            # before either of those loops has had a chance to do it.
            mc_scoring.maybe_roll_season(conn, ts, PROTOCOL)
            season_id = mc_scoring.ensure_active_season(conn, ts, PROTOCOL)

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

    def _load_registered_players(self, conn) -> dict[str, tuple[int, str]]:
        """node_ref (bare lowercase 8-hex, player_node's canonical form --
        see app/node_ref.py's module docstring; NOT app.api._node_hex's
        '!'-prefixed display form) -> (player_id, team) for every active
        Meshtastic player.

        Loaded once per poll/backfill cycle, not once per packet: a
        meshview poll returns on the order of a hundred packets from
        whatever nodes happen to be on the air, the overwhelming majority
        of which are not registered players, so a per-packet database
        lookup here would mean a query for nearly every packet on every
        poll for nothing. A dict lookup against this snapshot is enough.

        Disabled players (player.disabled_at set) are filtered out by the
        JOIN below, so a disabled player's node looks exactly like an
        unregistered one to _process_one_packet -- their pings are
        ignored for scoring the same way, and for the same reason
        player_ingest_stat can't record why: there is no per-player
        counter to charge an unattributed packet to.
        """
        rows = conn.execute(
            "SELECT pn.node_ref, pn.player_id, p.team "
            "  FROM player_node pn "
            "  JOIN player p ON p.player_id = pn.player_id "
            " WHERE pn.protocol = ? AND p.disabled_at IS NULL",
            (PROTOCOL,),
        ).fetchall()
        return {r["node_ref"]: (r["player_id"], r["team"]) for r in rows}

    async def _backfill(self) -> None:
        """On startup, pull position packets from the last N hours so the
        board fills in immediately for already-registered players. Pages
        backwards by import_time_us.

        Gated off entirely when settings.mt_paint_source is not
        "meshview" -- see this module's docstring. Backfill exists purely
        to paint the board faster on a cold start; when FreqMapper is the
        active paint source, meshview has nothing to backfill toward.
        """
        if settings.mt_paint_source != "meshview":
            log.info(
                "meshview backfill skipped -- mt_paint_source=%s", settings.mt_paint_source
            )
            return

        import time as _time
        cutoff_s = int(_time.time()) - (settings.backfill_hours * 3600)
        cutoff_us = cutoff_s * 1_000_000
        log.info("backfilling last %d hours (cutoff_s=%d)", settings.backfill_hours, cutoff_s)

        now = int(_time.time())
        # Season bookkeeping WRITES the mc_season table (a fresh row on
        # first run, or a roll on expiry) -- unlike the read-only
        # get_active_season() the old code used here, this has to go
        # through WriteSession so it holds app.db's write lock and an
        # explicit transaction, same as every other write in this module.
        # Without it, two concurrent callers could both see "no active
        # season" and both insert one.
        async with WriteSession() as conn:
            mc_scoring.maybe_roll_season(conn, now, PROTOCOL)
            mt_season_id = mc_scoring.ensure_active_season(conn, now, PROTOCOL)
            registered = self._load_registered_players(conn)

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

            # Filter packets to those within our window
            in_window = [p for p in packets if (p.get("import_time_us") or 0) >= cutoff_us]

            if in_window:
                tasks = []
                for pkt in in_window:
                    pid = extract_packet_id(pkt)
                    if pid is None:
                        continue
                    tasks.append(self._process_one_packet(pkt, pid, mt_season_id, registered))
                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    total_processed += sum(1 for r in results if r is True)

            log.info("backfill page %d: got=%d in_window=%d min_us=%d max_us=%d",
                     page, len(packets), len(in_window), page_min_us, page_max_us)

            # Stop conditions
            if page_min_us < cutoff_us:
                break  # walked past the window
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
        """One ingest cycle: position-packet painting (gated by
        settings.mt_paint_source -- see this module's docstring) followed
        by the NodeInfo identity pass, which always runs regardless of
        which source is currently painting.
        """
        if settings.mt_paint_source == "meshview":
            await self._poll_positions()
        elif not self._logged_paint_gated_once:
            log.info(
                "meshview position poll skipped -- mt_paint_source=%s",
                settings.mt_paint_source,
            )
            self._logged_paint_gated_once = True

        # NodeInfo pass: passive accumulation into mt_node_key only (see
        # that table's comment in app/db.py). Entirely separate from
        # position ingest/scoring above -- nothing reads mt_node_key yet
        # -- so it is isolated in its own try/except: a failure here
        # (upstream hiccup, an unparsable payload) must never take
        # position ingest or scoring down with it. Runs every cycle
        # regardless of mt_paint_source: this is identity/roster data,
        # not scoring, and stays on either way -- see this module's
        # docstring.
        try:
            await self._poll_nodeinfo()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("nodeinfo poll failed")

    async def _poll_positions(self) -> None:
        """Fetch the newest 100 position packets and process anything we
        haven't seen yet. Dedup is via the processed_packet table; we
        don't need a server-side cursor because the API always returns
        newest-first and 100 packets is plenty of overlap between polls.

        Only ever called when settings.mt_paint_source == "meshview" --
        see _poll_once above.
        """
        now = int(time.time())
        # Season bookkeeping WRITES mc_season (see the matching comment in
        # _backfill above for why this has to run inside WriteSession, not
        # a plain autocommit connect()). Happens once per poll cycle, not
        # once per packet -- same reasoning as MeshCore's batch-level call
        # in app/mc_ingest.py.
        async with WriteSession() as conn:
            mc_scoring.maybe_roll_season(conn, now, PROTOCOL)
            mt_season_id = mc_scoring.ensure_active_season(conn, now, PROTOCOL)
            registered = self._load_registered_players(conn)

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

        # Process oldest->newest for clean player_last_fix comparisons
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
            tasks.append(self._process_one_packet(pkt, pid, mt_season_id, registered))

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

    async def _poll_nodeinfo(self) -> None:
        """Fetch the newest NodeInfo (portnum=4) packets and record any
        (node_ref, public_key) pairs they carry into mt_node_key. Purely
        additive evidence -- see that table's comment in app/db.py for
        why the pair, not the node alone, is the key.
        """
        try:
            packets = await self.client.packets(portnum=4, limit=100)
        except Exception as e:
            log.warning("nodeinfo packets() failed: %s", e)
            return
        if not packets:
            return

        now = int(time.time())
        new_count = 0
        async with WriteSession() as conn:
            for pkt in packets:
                node_id = extract_node_id(pkt)
                if node_id is None:
                    continue
                payload = pkt.get("payload")
                if not isinstance(payload, str):
                    continue
                public_key, long_name = parse_nodeinfo_public_key(payload)
                if not public_key:
                    continue
                node_ref = _bare_node_ref(node_id)

                existing = conn.execute(
                    "SELECT 1 FROM mt_node_key WHERE node_ref = ? AND public_key = ?",
                    (node_ref, public_key),
                ).fetchone()
                # first_seen only set on insert; a conflict updates
                # last_seen/long_name and leaves first_seen untouched.
                conn.execute(
                    "INSERT INTO mt_node_key(node_ref, public_key, long_name, first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(node_ref, public_key) DO UPDATE SET "
                    "  last_seen = excluded.last_seen, long_name = excluded.long_name",
                    (node_ref, public_key, long_name, now, now),
                )
                if existing is None:
                    new_count += 1

        if new_count:
            log.info("nodeinfo: %d new node/key pair(s) recorded", new_count)

    async def _process_one_packet(
        self,
        packet: dict,
        packet_id: int,
        mt_season_id: int,
        registered: dict[str, tuple[int, str]],
    ) -> bool:
        # Skip already-processed packets (fast pre-check; race-safe
        # recheck happens again inside the write session below).
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
        if sender_id is None:
            await self._mark_processed(packet_id)
            return False

        # ----- Registration gate -----
        # REGISTERED NODES ONLY. This is the entire behavioral change
        # from the old auto-enrolling geohash board: a packet only scores
        # if its sender is bound to an active player in player_node.
        # Meshview keeps returning an unregistered node's packets on
        # every poll for as long as it's in the newest-100 window, so
        # they still have to be marked processed -- otherwise every
        # unregistered node on the network gets refetched forever -- but
        # nothing about them is ever written anywhere else.
        #
        # _bare_node_ref(), not _node_hex(): this is a player_node lookup
        # key, and player_node.node_ref's canonical form is bare lowercase
        # 8-hex (see app/node_ref.py), not _node_hex()'s '!'-prefixed
        # display form. _node_hex() is still exactly right a few lines
        # down for repeater_id (record_repeater_observations below) --
        # that is a different column, with its own existing data, and is
        # deliberately left alone here.
        node_ref = _bare_node_ref(sender_id)
        entry = registered.get(node_ref)
        if entry is None:
            await self._mark_processed(packet_id)
            return False
        player_id, team = entry

        pos = extract_position(packet)
        if pos is None or not valid_coord(pos[0], pos[1]):
            async with WriteSession() as conn:
                _bump_player_stat(conn, player_id, _stat_day(int(time.time())), pings_bad_coord=1)
                conn.execute(
                    "INSERT OR IGNORE INTO processed_packet(packet_id, processed_at) VALUES (?, ?)",
                    (packet_id, int(time.time())),
                )
            return False
        lat, lon, precision_bits = pos

        # Play-area check comes immediately after coordinate validity and
        # before the cell/receptions work below -- same ordering
        # app/mc_ingest.py uses -- so a ping from outside the configured
        # box never reaches scoring.
        if not in_play_area(
            lat, lon,
            settings.play_area_north, settings.play_area_south,
            settings.play_area_west, settings.play_area_east,
        ):
            async with WriteSession() as conn:
                _bump_player_stat(conn, player_id, _stat_day(int(time.time())), pings_out_of_area=1)
                conn.execute(
                    "INSERT OR IGNORE INTO processed_packet(packet_id, processed_at) VALUES (?, ?)",
                    (packet_id, int(time.time())),
                )
            return False

        # Precision gate (settings.mt_min_precision_bits -- see
        # app/config.py for the box-size arithmetic and why missing
        # precision_bits is rejected, not silently accepted). A radio
        # that has truncated its position to a box the size of this
        # gate's minimum -- or bigger -- cannot honestly paint one
        # specific ~300m cell; letting it through would credit a square
        # the radio may never actually have been near. Checked before the
        # cell is even computed, same as bad_coord/out_of_area above,
        # since a cell derived from an untrustworthy box is itself not
        # trustworthy.
        if precision_bits is None or precision_bits < settings.mt_min_precision_bits:
            async with WriteSession() as conn:
                _bump_player_stat(conn, player_id, _stat_day(int(time.time())), pings_low_precision=1)
                conn.execute(
                    "INSERT OR IGNORE INTO processed_packet(packet_id, processed_at) VALUES (?, ?)",
                    (packet_id, int(time.time())),
                )
            if precision_bits is None:
                log.warning(
                    "mt ingest: rejecting player %d: position carried no precision_bits",
                    player_id,
                )
            else:
                log.warning(
                    "mt ingest: rejecting player %d: precision_bits=%d below minimum %d",
                    player_id, precision_bits, settings.mt_min_precision_bits,
                )
            return False

        ts = extract_timestamp(packet)

        # Cell. Raw lat/lon are never written to the database anywhere;
        # they are discarded right here, after being reduced to a cell id.
        cell = cell_id(lat, lon)
        del lat, lon

        # Fetch reception rows -- one per MQTT feeder (meshview gateway
        # node) that reported hearing this packet.
        seen = await self.client.packets_seen(packet_id)

        # Qualifying receptions: hops <= settings.max_hops, when the hop
        # filter is even on, AND not via_mqtt=true.
        #
        # settings.max_hops <= 0 disables the hop filter entirely --
        # every reception counts no matter how many times the packet was
        # relayed before that feeder heard it -- following the same
        # disable-sentinel convention app/grid.py's in_play_area() uses
        # for north == south. This is the production default (MAX_HOPS=0
        # in docker-compose.yml). When the filter is disabled, a
        # reception with no hop_start/hop_limit data at all (hop_count()
        # returns None) must still count -- silently dropping it for
        # missing hop data would be filtering by hops through the back
        # door, exactly what disabling the filter is supposed to turn
        # off.
        #
        # via_mqtt=true flags a reception meshview learned about only
        # through an internet-side MQTT bridge, one that never crossed
        # the air at all -- not evidence of real RF reach, so it's
        # rejected here regardless of the hop filter. On this deployment
        # the MQTT feeders are uplink-only (they log a packet to MQTT and
        # never re-inject it back into the mesh), so this is expected to
        # filter nothing in practice; it is kept because it costs nothing
        # and stays correct for any deployment whose gateways do
        # re-inject.
        #
        # The distinct feeder ids that pass both filters are exactly what
        # scores this ping (settings.mt_points_per_feeder below) -- this
        # is the same distinct-feeder list the old geohash board used for
        # its rptr_json field, just no longer discarded after use.
        hop_filter_enabled = settings.max_hops > 0
        entries: list[RepeaterEntry] = []
        seen_fids: set[int] = set()
        for row in seen or []:
            hops = hop_count(row)
            if hop_filter_enabled and (hops is None or hops > settings.max_hops):
                continue
            if is_via_mqtt(row):
                continue
            fid = extract_feeder_id(row)
            if fid is None or fid in seen_fids:
                continue
            seen_fids.add(fid)
            snr = row.get("rx_snr") or row.get("snr")
            snr = float(snr) if isinstance(snr, (int, float)) else None
            # hops == 0: this feeder heard the sender directly over RF --
            # a measured relationship between this cell and this one
            # named feeder, same meaning as MeshCore's "direct" DISC/TRACE
            # entries. hops > 0, or unknown (None, only reachable here
            # when the hop filter above is disabled): reach via the mesh
            # or reach of unconfirmed directness, same meaning as
            # MeshCore's "heard" TX/RX entries. See RepeaterEntry's
            # docstring in app/mc_ingest.py for why these are never
            # allowed to merge.
            if hops == 0:
                entries.append(RepeaterEntry(
                    repeater_id=_node_hex(fid), kind="direct", local_snr=snr,
                ))
            else:
                entries.append(RepeaterEntry(
                    repeater_id=_node_hex(fid), kind="heard", heard_snr=snr,
                ))

        day = _stat_day(ts)

        async with WriteSession() as conn:
            # Dedup check inside the lock (race-safe)
            row = conn.execute(
                "SELECT 1 FROM processed_packet WHERE packet_id = ?", (packet_id,)
            ).fetchone()
            if row:
                return False

            # player_cell_ping is written BEFORE scoring, same as
            # app/mc_ingest.py -- it is the exact-duplicate check (this
            # packet_id could theoretically resend an identical
            # sender/cell/ts). The scoring cooldown itself no longer
            # reads this table -- see mc_scoring.apply_paint(), which now
            # gates per feeder credited, not per ping painted.
            # precision_bits is recorded here purely for audit (see
            # app/db.py's player_cell_ping comment) -- by this point it
            # has already passed the precision gate above.
            cur = conn.execute(
                "INSERT OR IGNORE INTO player_cell_ping"
                "(player_id, protocol, cell_id, ts, seen_at, precision_bits) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (player_id, PROTOCOL, cell, ts, int(time.time()), precision_bits),
            )
            if cur.rowcount == 0:
                _bump_player_stat(conn, player_id, day, pings_duplicate=1)
                conn.execute(
                    "INSERT OR IGNORE INTO processed_packet(packet_id, processed_at) VALUES (?, ?)",
                    (packet_id, int(time.time())),
                )
                return False

            # Speed gate (settings.mt_max_speed_mps -- see app/config.py
            # for why this is a materially higher bar than MeshCore's
            # mc_max_speed_mps, and why). Read BEFORE
            # record_repeater_observations/player_last_fix are touched:
            # unlike MeshCore's by_air (a label that never blocks
            # anything -- the radio really did hear that repeater), an
            # implausible jump here means the position itself -- and
            # therefore the cell derived from it -- is not trusted, so
            # this ping must credit nothing and must not become the new
            # baseline other pings' speeds are measured against (letting
            # a bad fix "anchor" player_last_fix would make the very next
            # legitimate ping look like the impossible one instead).
            last_fix = conn.execute(
                "SELECT cell_id, ts FROM player_last_fix WHERE player_id = ? AND protocol = ?",
                (player_id, PROTOCOL),
            ).fetchone()
            if last_fix is not None and ts > last_fix["ts"]:
                elapsed = ts - last_fix["ts"]
                prev_lat, prev_lon = cell_center(last_fix["cell_id"])
                cur_lat, cur_lon = cell_center(cell)
                speed = distance_m(prev_lat, prev_lon, cur_lat, cur_lon) / elapsed
                if speed > settings.mt_max_speed_mps:
                    _bump_player_stat(conn, player_id, day, pings_implausible_speed=1)
                    conn.execute(
                        "INSERT OR IGNORE INTO processed_packet(packet_id, processed_at) VALUES (?, ?)",
                        (packet_id, int(time.time())),
                    )
                    log.warning(
                        "mt ingest: rejecting player %d: implausible speed %.1f m/s "
                        "over %ds (cell %s -> %s)",
                        player_id, speed, elapsed, last_fix["cell_id"], cell,
                    )
                    return False

            # Coverage evidence: which feeders can hear this cell, at
            # cell granularity only -- recorded for every ping that
            # reaches this point, including one whose feeders apply_paint
            # will go on to reject for the cooldown (a cooldown blocks
            # scoring, not what this cell can actually reach).
            record_repeater_observations(conn, PROTOCOL, cell, entries, ts)

            # Last known cell, monotonic on ts, same as MeshCore.
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

            # Distinct feeders heard -> candidate scoring input, mirroring
            # how app/mc_ingest.py turns a distinct-repeater list into
            # scoring input, inverted (see the module docstring). A
            # packet no feeder heard within the hop limit earns zero
            # points here -- still a valid, accepted ping (counted
            # below), just one that paints nothing when it reaches
            # apply_paint. Which feeders actually earn points -- some may
            # already be credited to this player on this cell within the
            # cooldown window -- is decided inside apply_paint(), not
            # here.
            feeder_ids = [e.repeater_id for e in entries]
            no_repeaters = 1 if not feeder_ids else 0
            _bump_player_stat(conn, player_id, day, pings_accepted=1, pings_no_repeaters=no_repeaters)

            # Meshtastic scoring, through the same shared path MeshCore
            # uses. A scoring failure must not lose the stat counters
            # already recorded above or abort the rest of this packet's
            # bookkeeping -- log it and keep going.
            try:
                mc_scoring.apply_paint(
                    conn, mt_season_id, player_id, team, cell, ts, feeder_ids,
                    settings.mt_points_per_feeder, settings.mt_max_points_per_ping,
                    PROTOCOL, int(time.time()),
                )
            except Exception:
                log.exception(
                    "mt scoring: apply_paint failed for player %d cell %s",
                    player_id, cell,
                )

            # Places Worth Going (app/place_scoring.py), same hook as
            # app/mc_ingest.py's MeshCore path. by_air is not tracked on
            # this board (apply_paint above is never passed one either,
            # for the same reason -- see its call above), so it is
            # always False here; nothing on the Meshtastic path detects
            # aircraft speed today.
            try:
                credit_places(conn, player_id, cell, ts, feeder_ids, False, PROTOCOL)
            except Exception:
                log.exception(
                    "place scoring: credit_places failed for player %d cell %s",
                    player_id, cell,
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


def _bare_node_ref(node_id: int) -> str:
    """Bare lowercase 8-hex form of a Meshtastic node id -- the exact
    form player_node.node_ref is canonically stored and looked up in
    (see app/node_ref.py's module docstring). Deliberately NOT
    _node_hex() (app/api.py), which returns the '!'-prefixed form used
    for repeater_id and for the /get-nodes "id" field a human reads;
    those are separate uses with their own existing data/conventions
    that this function must not touch.
    """
    return f"{node_id:08x}"


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
