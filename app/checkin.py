"""Net check-ins: a second way to earn points, alongside squares held.

A weekly net runs Wednesday evenings. Checking in during the net window
earns a registered, non-disabled player's team settings.checkin_points,
once per player per net -- on top of, never instead of, the squares
their team holds (see app/mc_scoring.py's team_totals()). Theme only:
square-holders are "Wardrivers," check-in earners are "Netrunners," but
these are two ACTIVITIES on the same player model, not two kinds of
player -- the same person can do both and shows up in both rankings.
There is no class or mode anywhere in this schema.

Two independent feeds, polled the same way app/ingest.py polls meshview
for position packets -- a background task, its own interval, tolerant
of the upstream being down or slow, idempotent under repeated polling:

- MeshCore: GET {mc_checkin_base_url}/api/channels/{channel}/messages
  on live.mwmesh.com returns the newest 100 messages in the weekly-net
  channel, oldest first, no pagination. The only sender identifier ON A
  MESSAGE is a free-text display name (`sender`) -- MeshCore channel
  messages are encrypted to a shared channel key, not signed per
  sender, so there is no public key to read off a message itself.
  Identity resolution for a MeshCore check-in is therefore two paths,
  KEY-BASED FIRST, in this priority order:

    1. The public-key directory bridge (_build_directory_bridge below,
       used by _resolve_mc_identities): resolves a player's ALREADY-
       BOUND MeshCore radio contact -- player_node, protocol='mc', the
       first 8 hex characters of that radio's public key -- through
       live.mwmesh.com's node directory (/api/nodes), which carries
       both a display name and the full public key for every node it
       has seen. If that contact's 8-hex prefix matches exactly one
       directory entry, that entry's name is trusted as the player's
       check-in identity. This is the normal case and needs no separate
       check-in registration step: it works identically whether the
       contact got into player_node via MeshMapper's wardriving
       auto-bind (app/mc_ingest.py) or a player typing it into
       POST /api/nodes by hand (app/nodes_api.py) -- both write the
       exact same row shape, and this bridge reads player_node, not
       whichever path wrote it.

       Why this is trusted automatically, when a bare name match would
       not be: the join is anchored on a public key MeshWars already
       knows independently of anything a check-in message claims.
       Someone who renames their OWN node to impersonate a player's
       display name shows up in the directory as a second, DIFFERENT
       public key under that name -- their contact was never bound to
       the real player, so their messages resolve to nobody. An
       attacker cannot make their own node's contact resolve to someone
       else's public key; they can only ever make their own node's NAME
       collide with someone else's, and resolution here never starts
       from the name. Starting from the name instead of the key is
       exactly what would make that attack work; starting from the key
       is why it doesn't.

    2. A last-resort, self-declared mc_checkin_binding row
       (app/checkin_api.py), used ONLY for a player the bridge above
       found nothing for -- a public key that has never shown up in the
       live.mwmesh.com directory at all (true for roughly 4 in 10 of
       today's real bound contacts) has no way to be resolved key-first,
       so those players have to supply the name directly. Key-based
       resolution wins wherever it produces an answer: if a player's
       contact DOES resolve through the bridge, any fallback name they
       also registered is ignored outright, never consulted, let alone
       allowed to override it -- a self-declared name is strictly less
       trustworthy than a key-anchored match, precisely because it
       carries none of the impersonation resistance described above.

    Both paths refuse rather than guess on ambiguity -- see
    _build_directory_bridge and _resolve_mc_identities below for the
    specific cases (a contact matching more than one public key; a name
    shared by more than one public key anywhere in the directory; a
    fallback name that collides with a name the bridge already produced
    for someone else) and why every one of them is a skip-and-log,
    never a pick-one.

- Meshtastic: GET {meshview_base_url}/api/packets?portnum=1 -- the SAME
  meshview instance app/ingest.py already polls for position packets
  (portnum=3), just text messages instead. A check-in is any message
  whose payload contains settings.mt_checkin_hashtag, case-insensitively.
  Identity is `from_node_id`, resolved through player_node exactly the
  way app/ingest.py's position poller already does -- see
  _bare_node_ref (reused from there, not reimplemented) and
  _load_mt_registered_players below.

Dedup: the natural key for an award is (season, player, local net
date) -- neither feed has a session concept, and a MeshCore sender
posting several times in one net must still be credited once. That key
is the table's PRIMARY KEY (see app/db.py's mc_checkin_award), so
_award_checkin's INSERT OR IGNORE is what actually enforces "at most
one per player per net," not any in-memory bookkeeping here. Separately,
each message/packet's own id is deduplicated too (mc_checkin_seen_message
for MeshCore; the existing processed_packet table, shared with
app/ingest.py's position poller, for Meshtastic -- see
_process_mt_packet's comment for why sharing that table is safe) so a
message already looked at on an earlier poll is never re-examined at
all, whether or not it ended up earning anything.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from . import mc_scoring
from .config import settings
from .db import WriteSession
from .meshview_client import MeshviewClient
from .node_ref import normalize_sender_name

log = logging.getLogger("checkin")

MC_PROTOCOL = "mc"
MT_PROTOCOL = "mt"


def net_date_for_ts(ts: int) -> str | None:
    """Local net date (YYYY-MM-DD) for `ts` if it falls inside the net
    window -- settings.checkin_net_weekday, from
    settings.checkin_net_start_hour through
    settings.checkin_net_end_hour:59:59, local to
    settings.checkin_net_timezone -- otherwise None.

    A real IANA zone via zoneinfo, not a fixed UTC offset: America/Boise
    is UTC-7 in winter and UTC-6 in summer, and a message near either
    boundary of the window must land in the same LOCAL hour either way,
    which only a real zone gets right across the transition. A message
    that spills past local midnight into Thursday has a different
    LOCAL weekday, not just a different hour -- the weekday check above
    is what rejects it, not the hour check.
    """
    local = datetime.fromtimestamp(ts, tz=ZoneInfo(settings.checkin_net_timezone))
    if local.weekday() != settings.checkin_net_weekday:
        return None
    if not (settings.checkin_net_start_hour <= local.hour <= settings.checkin_net_end_hour):
        return None
    return local.date().isoformat()


def _award_checkin(
    conn, season_id: int, player_id: int, net_date: str, protocol: str,
    message_id: str, awarded_at: int,
) -> None:
    """Credit one check-in, if this (season, player, net_date) hasn't
    already been credited -- see the module docstring for why that
    triple, enforced by mc_checkin_award's PRIMARY KEY, is what actually
    makes this idempotent no matter how many qualifying messages a
    player posted in the net or how many times a poll re-examines them.
    `points` is copied from settings.checkin_points now, not read live
    later, so a config change afterward can never rewrite this award.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO mc_checkin_award"
        "(season_id, player_id, net_date, points, protocol, message_id, awarded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (season_id, player_id, net_date, settings.checkin_points, protocol, message_id, awarded_at),
    )
    if cur.rowcount:
        log.info(
            "checkin: awarded %.2f points to player %d for %s net %s (season %d, message %s)",
            settings.checkin_points, player_id, protocol, net_date, season_id, message_id,
        )


# ---- MeshCore: feed client -------------------------------------------


class McLiveClient:
    """HTTP client for live.mwmesh.com's two check-in feeds: the
    weekly-net channel messages, and the public-key node directory used
    by the identity bridge (see the module docstring). Not
    app/meshview_client.py's MeshviewClient -- that is built around a
    different host's /api/nodes, /api/packets, /api/packets_seen
    shapes -- but it follows the same conventions that client does: a
    real User-Agent (this host rejects requests without one, confirmed
    against production) and a bounded timeout. Tolerant of the upstream
    being slow or down by design, not by accident: a failed request logs
    and returns an empty result rather than raising into the poll loop,
    since the poll loop already retries on its own short interval --
    unlike MeshviewClient's own in-request retry/backoff, a second retry
    layer here would just be redundant.
    """

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.mc_checkin_base_url.rstrip("/"),
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={"Accept": "application/json", "User-Agent": "meshwars/1.0"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> dict | None:
        try:
            r = await self._client.get(path, params=params)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            log.warning("checkin: mc request %s failed: %s", path, e)
            return None

    async def messages(self, channel: str) -> list[dict]:
        """Newest 100 messages in `channel`, oldest first. `channel` is
        percent-encoded here (not by the caller) -- it contains a literal
        "#", which is a URL fragment separator and must never reach
        httpx unescaped or everything after it would be silently dropped
        from the request.
        """
        data = await self._get(f"/api/channels/{quote(channel, safe='')}/messages")
        if not isinstance(data, dict):
            return []
        msgs = data.get("messages")
        return msgs if isinstance(msgs, list) else []

    async def nodes(self, limit: int) -> list[dict]:
        """Up to `limit` directory entries. `limit` is the one paging
        parameter this endpoint actually honors -- per-page/page/size
        and similar are silently ignored, so passing anything else would
        look like it worked while quietly returning the default page.
        """
        data = await self._get("/api/nodes", {"limit": limit})
        if not isinstance(data, dict):
            return []
        nodes = data.get("nodes")
        return nodes if isinstance(nodes, list) else []


# ---- MeshCore: identity resolution -------------------------------------


def _load_mc_manual_bindings(conn) -> dict[str, int]:
    """normalized sender_name -> player_id, from the explicit
    mc_checkin_binding table (app/checkin_api.py) -- the LAST-RESORT
    fallback, for a player whose bound contact has never shown up in the
    live.mwmesh.com directory at all, so _build_directory_bridge has
    nothing to resolve them through. See _resolve_mc_identities below
    for how this is merged with the bridge -- key-based resolution wins
    wherever it produces an answer, so a row here is ignored outright
    for any player the bridge already resolved.

    Only non-disabled players are eligible, same filter every other
    attribution path in this app applies at read time (see
    app/ingest.py's _load_registered_players) -- a disabled player's
    binding row still exists (admin disable deletes nothing) but stops
    matching here the moment they're disabled. sender_name is already
    stored normalized (checkin_api.py normalizes before insert), so no
    re-normalization is needed on the way back out.
    """
    rows = conn.execute(
        "SELECT b.sender_name, b.player_id FROM mc_checkin_binding b "
        "JOIN player p ON p.player_id = b.player_id WHERE p.disabled_at IS NULL"
    ).fetchall()
    return {r["sender_name"]: r["player_id"] for r in rows}


def _build_directory_bridge(conn, directory_nodes: list[dict]) -> dict[str, int]:
    """normalized display name -> player_id, resolved by walking each
    non-disabled player's bound MeshCore radio contact (player_node,
    protocol='mc') through to `directory_nodes` (a live.mwmesh.com
    /api/nodes snapshot). See the module docstring for why this join,
    anchored on a public key rather than a name, is safe to trust
    automatically with no separate registration step.

    Refuses rather than guesses on either kind of ambiguity, logging
    and skipping instead of picking one:

    - A bound contact whose 8-hex prefix matches more than one (or
      zero) directory public keys is skipped entirely -- if two
      different real nodes share a prefix, or the radio the player
      bound has never shown up in the directory at all, there is no
      way to tell which (if any) entry is really theirs.
    - A display name shared by more than one public key ANYWHERE in the
      directory -- not just among players who happen to have a bound
      contact -- is refused for every player it would otherwise resolve
      to. A repeater or a room server sharing a player's chosen display
      name makes that name unsafe to attribute to anyone, exactly the
      same as two different companion nodes sharing it would; the
      directory carries 3 such duplicate names in production today.

    A wrong attribution here is worse than a missed point, so every one
    of the skips above is a no-op for that player (they simply don't
    resolve through the bridge this cycle -- an explicit
    mc_checkin_binding registration, or a later unambiguous directory
    state, still works), never a best-effort pick.
    """
    by_name: dict[str, set[str]] = {}
    by_prefix: dict[str, list[dict]] = {}
    for node in directory_nodes:
        name = node.get("name")
        pubkey = node.get("public_key")
        if not isinstance(name, str) or not name or not isinstance(pubkey, str) or len(pubkey) < 8:
            continue
        pubkey = pubkey.lower()
        by_name.setdefault(name, set()).add(pubkey)
        by_prefix.setdefault(pubkey[:8], []).append(node)

    ambiguous_names = {name for name, keys in by_name.items() if len(keys) > 1}

    rows = conn.execute(
        "SELECT pn.node_ref, pn.player_id FROM player_node pn "
        "JOIN player p ON p.player_id = pn.player_id "
        "WHERE pn.protocol = 'mc' AND p.disabled_at IS NULL"
    ).fetchall()

    bridge: dict[str, int] = {}
    for r in rows:
        contact = r["node_ref"]  # already bare lowercase 8-hex (app/node_ref.py)
        matches = by_prefix.get(contact, [])
        if len(matches) != 1:
            if len(matches) > 1:
                log.warning(
                    "checkin: mc contact %s matches %d directory public keys, "
                    "refusing to resolve (ambiguous)", contact, len(matches),
                )
            continue
        name = matches[0].get("name")
        if name in ambiguous_names:
            log.warning(
                "checkin: directory name %r is shared by multiple public keys, "
                "refusing to attribute it to player %d", name, r["player_id"],
            )
            continue
        normalized = normalize_sender_name(name)
        if normalized is None:
            continue
        bridge[normalized] = r["player_id"]
    return bridge


def _resolve_mc_identities(conn, directory_nodes: list[dict]) -> dict[str, int]:
    """normalized sender name -> player_id, the single map
    _process_mc_message actually looks a check-in sender up in --
    combines the key-based directory bridge (_build_directory_bridge)
    with the last-resort fallback (_load_mc_manual_bindings), applying
    the priority the module docstring describes: key-based resolution
    wins wherever it produces an answer.

    Two things follow from that, both enforced here rather than left to
    caller discipline:

    - A fallback row for a player the bridge already resolved is
      dropped outright, never merely low-priority -- that player's
      checked-in identity is whatever the bridge says, full stop, and
      their fallback registration (if any) is inert while that holds.
    - A fallback NAME that happens to equal a name the bridge already
      produced for a DIFFERENT player is also dropped, logged as a
      collision -- the bridge's claim on that exact name wins, and the
      fallback claimant earns nothing under it (the same "refuse rather
      than guess" rule _build_directory_bridge applies to directory-wide
      duplicates, extended to cover a self-declared name colliding with
      a key-anchored one).

    A directory-wide ambiguous name (_build_directory_bridge already
    refuses to put those in the bridge at all) is not separately
    special-cased for the fallback path here: if a fallback name is
    ambiguous in the directory, it was never a candidate to appear as a
    bridge value in the first place, so the collision check above cannot
    catch it -- but nothing stops two DIFFERENT fallback registrations
    from claiming visually distinct names that happen to collide with an
    ambiguous directory name; that is out of scope for a self-declared
    name that was never checked against the directory to begin with, and
    mc_checkin_binding's own PRIMARY KEY already prevents two players
    from registering the identical fallback string.
    """
    bridge = _build_directory_bridge(conn, directory_nodes)
    manual = _load_mc_manual_bindings(conn)
    bridge_player_ids = set(bridge.values())

    resolved: dict[str, int] = {}
    for name, player_id in manual.items():
        if player_id in bridge_player_ids:
            continue  # key-based already answered for this player
        if name in bridge:
            log.warning(
                "checkin: fallback name %r for player %d collides with a "
                "name the directory bridge already resolved for another "
                "player -- refusing the fallback", name, player_id,
            )
            continue
        resolved[name] = player_id

    # Overlay the bridge last so it wins any remaining name collision.
    resolved.update(bridge)
    return resolved


def companion_directory_entries(directory_nodes: list[dict]) -> list[dict]:
    """Shape the cached live.mwmesh.com directory into the "people only"
    picker list app/checkin_api.py's node-picker endpoint serves --
    filters to role == "companion" (repeaters and room servers are
    infrastructure and must never be offered as a person to pick), and
    reduces each entry to exactly what a player needs to recognise their
    own node and what the UI needs to disambiguate two companions
    sharing a name: the display name, the 8-hex public-key prefix that
    would actually be bound (the SAME value MeshMapper auto-binds and
    app/nodes_api.py accepts typed in by hand -- picking from this list
    is a third way to arrive at the exact same contact, never a
    different kind of identity), and whatever presence/location fields
    the directory happens to carry.

    Deliberately does NOT deduplicate by name -- the directory carries
    real duplicate names (3 in production today), and collapsing them
    would let the UI silently offer the wrong node. Every companion
    entry is returned as its own row, distinguished by its prefix;
    picking between duplicates is left entirely to the person looking at
    them. Whether a given entry is already bound, and to whom, is a
    separate per-caller concern the endpoint itself layers on -- this
    function has no database access and does not know who is asking.
    """
    out = []
    for node in directory_nodes:
        if node.get("role") != "companion":
            continue
        name = node.get("name")
        pubkey = node.get("public_key")
        if not isinstance(name, str) or not name or not isinstance(pubkey, str) or len(pubkey) < 8:
            continue
        out.append({
            "name": name,
            "contact": pubkey[:8].lower(),
            "last_seen": node.get("last_seen"),
            "lat": node.get("lat"),
            "lon": node.get("lon"),
        })
    return out


def _parse_iso_ts(raw: object) -> int | None:
    """Parse a "...Z" UTC ISO-8601 timestamp (the shape both weekly-net
    messages and directory entries use) to epoch seconds. None on
    anything malformed -- this is upstream data, never raised on.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


# ---- Meshtastic: identity resolution -------------------------------------


def _load_mt_registered_players(conn) -> dict[str, int]:
    """node_ref (bare lowercase 8-hex) -> player_id, for every active
    Meshtastic player. Same shape and filter as app/ingest.py's
    _load_registered_players (disabled players excluded by the JOIN),
    loaded once per poll rather than once per packet for the same
    reason that function gives: the overwhelming majority of packets in
    a poll are not registered players, so a per-packet query would be a
    query for nearly nothing on every poll.
    """
    rows = conn.execute(
        "SELECT pn.node_ref, pn.player_id "
        "  FROM player_node pn JOIN player p ON p.player_id = pn.player_id "
        " WHERE pn.protocol = 'mt' AND p.disabled_at IS NULL"
    ).fetchall()
    return {r["node_ref"]: r["player_id"] for r in rows}


# ---- Poller ---------------------------------------------------------------


class CheckinPoller:
    """Background task: polls both check-in feeds on
    settings.checkin_poll_interval_seconds and awards points for
    qualifying, in-window, registered senders. Follows the same shape
    app/ingest.py's Ingestor and app/mc_ingest.py's McIngestor already
    use -- a loop task started from app/main.py's lifespan, its own
    interval, exceptions caught and logged per cycle so one bad poll
    never kills the loop.

    Shares the passed-in MeshviewClient (the same one app/ingest.py's
    Ingestor already holds) for the Meshtastic feed rather than opening
    a second connection pool to the same host -- this keeps all
    meshview traffic under that client's own rate limiter
    (settings.upstream_rate_per_sec). The MeshCore feed is a different
    host entirely, so it gets its own small client (McLiveClient above).
    """

    def __init__(self, mt_client: MeshviewClient) -> None:
        self._mt_client = mt_client
        self._mc_client = McLiveClient() if settings.mc_checkin_base_url else None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._directory: list[dict] = []
        self._directory_fetched_at: float = 0.0

    async def start(self) -> None:
        self._task = asyncio.create_task(self.run_forever(), name="checkin-poller")
        log.info(
            "checkin poller started; poll=%ds mc=%s mt_hashtag=%s",
            settings.checkin_poll_interval_seconds,
            "on" if self._mc_client else "off (mc_checkin_base_url not set)",
            settings.mt_checkin_hashtag,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._mc_client is not None:
            await self._mc_client.aclose()
        log.info("checkin poller stopped")

    async def run_forever(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self._poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("checkin: poll cycle failed")
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=settings.checkin_poll_interval_seconds
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise

    async def _poll_once(self) -> None:
        # The two feeds are independent and one going down must never
        # block the other -- each gets its own try/except, mirroring how
        # app/ingest.py's poll cycle already isolates its own failures
        # from the caller (main loop keeps going either way).
        if self._mc_client is not None:
            try:
                await self._poll_mc()
            except Exception:
                log.exception("checkin: mc poll failed")
        try:
            await self._poll_mt()
        except Exception:
            log.exception("checkin: mt poll failed")

    async def _refresh_directory_if_stale(self) -> None:
        now = time.monotonic()
        if self._directory and now - self._directory_fetched_at < settings.mc_checkin_directory_refresh_seconds:
            return
        nodes = await self._mc_client.nodes(settings.mc_checkin_directory_limit)
        if nodes:
            self._directory = nodes
            self._directory_fetched_at = now
        elif not self._directory:
            # No cache to fall back on yet -- the directory bridge is
            # simply unavailable this cycle. An explicit
            # mc_checkin_binding registration still works regardless;
            # this only affects the auto-resolved path.
            log.warning("checkin: mc directory fetch failed and no cached copy yet")

    def directory_snapshot(self) -> list[dict]:
        """Read-only copy of the cached live.mwmesh.com directory, for
        app/checkin_api.py's node-picker endpoint. Reads the SAME cache
        _refresh_directory_if_stale maintains on its own
        settings.mc_checkin_directory_refresh_seconds interval -- that
        endpoint is a person clicking around a form, not something to
        hit the upstream for on every request.
        """
        return list(self._directory)

    async def _poll_mc(self) -> None:
        messages = await self._mc_client.messages(settings.mc_checkin_channel)
        if not messages:
            return
        await self._refresh_directory_if_stale()

        now = int(time.time())
        async with WriteSession() as conn:
            # Season bookkeeping, same call app/mc_ingest.py's batch
            # worker makes -- check-ins must be able to roll the MeshCore
            # season forward on their own; a quiet week of wardriving
            # traffic must never be the only thing keeping mc_season
            # current.
            mc_scoring.maybe_roll_season(conn, now, MC_PROTOCOL)
            season_id = mc_scoring.ensure_active_season(conn, now, MC_PROTOCOL)
            resolved = _resolve_mc_identities(conn, self._directory)

            for m in messages:
                self._process_mc_message(conn, m, season_id, resolved, now)

    def _process_mc_message(self, conn, m: dict, season_id: int, resolved: dict, received_at: int) -> None:
        packet_id = m.get("packetId")
        if not isinstance(packet_id, int):
            return

        # Dedup on the message's own id, unconditionally -- whether or
        # not it turns out to be in-window or attributable, a message
        # already looked at on an earlier poll is never re-examined.
        cur = conn.execute(
            "INSERT OR IGNORE INTO mc_checkin_seen_message(packet_id, seen_at) VALUES (?, ?)",
            (packet_id, received_at),
        )
        if cur.rowcount == 0:
            return

        ts = _parse_iso_ts(m.get("timestamp"))
        if ts is None:
            return
        net_date = net_date_for_ts(ts)
        if net_date is None:
            return

        normalized = normalize_sender_name(m.get("sender"))
        if normalized is None:
            return

        player_id = resolved.get(normalized)
        if player_id is None:
            return  # unregistered (or unresolved) sender -- earns nothing

        _award_checkin(conn, season_id, player_id, net_date, MC_PROTOCOL, str(packet_id), received_at)

    async def _poll_mt(self) -> None:
        packets = await self._mt_client.packets(portnum=1, limit=100)
        if not packets:
            return

        now = int(time.time())
        async with WriteSession() as conn:
            mc_scoring.maybe_roll_season(conn, now, MT_PROTOCOL)
            season_id = mc_scoring.ensure_active_season(conn, now, MT_PROTOCOL)
            registered = _load_mt_registered_players(conn)

            for pkt in packets:
                self._process_mt_packet(conn, pkt, season_id, registered, now)

    def _process_mt_packet(self, conn, pkt: dict, season_id: int, registered: dict, received_at: int) -> None:
        pid = pkt.get("id")
        if not isinstance(pid, int):
            return

        # Dedup via the SAME processed_packet table app/ingest.py's
        # position poller already writes to, not a second table.
        # Meshview assigns a single globally unique "id" per packet
        # regardless of portnum, so a portnum=1 (text) id can never
        # collide with a portnum=3 (position) id -- reusing the table is
        # safe, and simpler than a parallel table that would only ever
        # differ by which portnum wrote each row.
        cur = conn.execute(
            "INSERT OR IGNORE INTO processed_packet(packet_id, processed_at) VALUES (?, ?)",
            (pid, received_at),
        )
        if cur.rowcount == 0:
            return

        payload = pkt.get("payload")
        if not isinstance(payload, str) or settings.mt_checkin_hashtag.lower() not in payload.lower():
            return

        import_us = pkt.get("import_time_us")
        if not isinstance(import_us, (int, float)):
            return
        net_date = net_date_for_ts(int(import_us / 1_000_000))
        if net_date is None:
            return

        sender_id = pkt.get("from_node_id")
        if not isinstance(sender_id, int):
            return
        # Local import: see app/ingest.py's _bare_node_ref docstring for
        # the exact form (bare lowercase 8-hex, matching
        # player_node.node_ref's canonical form) -- reused as-is rather
        # than reimplemented, per the module docstring. Deferred to call
        # time, not imported at module load, to avoid a circular import:
        # app/api.py -> app/checkin_api.py -> app/checkin.py would close
        # a cycle back to app/api.py if this were a top-level import,
        # since app/ingest.py itself imports from app/api.py.
        from .ingest import _bare_node_ref

        node_ref = _bare_node_ref(sender_id)
        player_id = registered.get(node_ref)
        if player_id is None:
            return

        _award_checkin(conn, season_id, player_id, net_date, MT_PROTOCOL, str(pid), received_at)
