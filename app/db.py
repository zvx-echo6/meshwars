"""SQLite schema, connection, and migrations.

Uses WAL mode so HTTP read endpoints can serve concurrently with the
single writer (the poll loop / scheduler).
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .config import settings
from .device_label import device_label_from_user_agent

log = logging.getLogger("db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS season (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      INTEGER NOT NULL,        -- epoch seconds
    ends_at         INTEGER NOT NULL,
    status          TEXT NOT NULL,           -- 'active' | 'closed'
    red_tiles       INTEGER,
    blue_tiles      INTEGER,
    green_tiles     INTEGER,
    winner          TEXT                     -- 'RED' | 'BLUE' | 'TIE' | NULL while active
);

CREATE INDEX IF NOT EXISTS idx_season_status ON season(status);

CREATE TABLE IF NOT EXISTS team_assignment (
    season_id       INTEGER NOT NULL,
    node_id         INTEGER NOT NULL,
    team            TEXT NOT NULL,           -- 'RED' | 'BLUE'
    activity_score  REAL NOT NULL,
    PRIMARY KEY (season_id, node_id),
    FOREIGN KEY (season_id) REFERENCES season(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_assignment_node ON team_assignment(node_id, season_id);

-- One row per (season, geohash) that has ever received a qualifying position.
CREATE TABLE IF NOT EXISTS tile (
    season_id               INTEGER NOT NULL,
    geohash                 TEXT NOT NULL,
    rcv                     INTEGER NOT NULL DEFAULT 0,
    lost                    INTEGER NOT NULL DEFAULT 0,
    last_sender_node_id     INTEGER NOT NULL,
    last_report_ts          INTEGER NOT NULL,
    last_snr                REAL,
    last_rssi               REAL,
    owner_team              TEXT NOT NULL,   -- 'RED' | 'BLUE' | 'GREEN'
    rptr_json               TEXT NOT NULL DEFAULT '[]',
    last_packet_id          INTEGER,
    PRIMARY KEY (season_id, geohash)
);

CREATE INDEX IF NOT EXISTS idx_tile_owner ON tile(season_id, owner_team);

-- `sample` (8-char geohash position samples, keyed to sender_node_id --
-- radio identity) used to live here, for the /get-samples endpoint. It
-- is GONE, not just retired-and-kept the way tile/tile_score/tile_capture*
-- above are: a privacy audit found it held the finest-grained position
-- data anywhere in this schema (~19m geohash precision, far tighter than
-- the ~300m grid the live scoring path deliberately uses), tied to radio
-- identity, dead code on both ends (app/ingest.py stopped writing it long
-- before this was noticed; /get-samples has returned a hardcoded empty
-- list ever since -- see that route's own comment), and had no deletion
-- anywhere in the codebase -- no sweep, no retention, nothing ever
-- expired a row. On preview it held movement history for hundreds of
-- radios that were never registered with MeshWars at all -- people who
-- never signed up, being tracked to house-level precision, forever. Matt's
-- call: it serves no purpose and holds the most sensitive data in the
-- system, so it does not get the "kept for history" treatment the
-- fortress-game tables above got -- it is dropped outright. See
-- db.py's MIGRATIONS list below for the DROP TABLE IF EXISTS that
-- removes it from a database that still has it.

-- Repeater/node roster cache: snapshot of nodes seen in this season so the
-- frontend can render them as markers.
CREATE TABLE IF NOT EXISTS node_seen (
    season_id   INTEGER NOT NULL,
    node_id     INTEGER NOT NULL,
    name        TEXT NOT NULL,
    short_name  TEXT,
    lat         REAL,
    lon         REAL,
    elev        REAL DEFAULT 0,
    last_seen   INTEGER NOT NULL,
    role        TEXT,
    PRIMARY KEY (season_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_node_seen_season ON node_seen(season_id);

-- Node id -> public key evidence. Meshtastic 2.8 derives a node's id
-- from its key material rather than from fixed hardware, so the id is
-- no longer a stable identity: it can change under a node, and two
-- nodes can collide on one. The public key is the stable thing, but
-- only NodeInfo packets (portnum 4) carry it -- position packets, which
-- is what scoring reads, do not -- so this accumulates the mapping
-- passively, from app/ingest.py's own NodeInfo poll pass, well ahead of
-- anything needing to read it back. Nothing does yet.
--
-- Primary key is the (node_ref, public_key) PAIR, not node_ref alone,
-- and that is deliberate: keying on node_ref alone would overwrite the
-- old row the instant a node's key changed, destroying exactly the
-- evidence of drift or collision this table exists to catch. A node
-- that has broadcast under two different keys ends up as two rows here,
-- not one row silently rewritten.
CREATE TABLE IF NOT EXISTS mt_node_key (
    node_ref    TEXT NOT NULL,      -- bare lowercase 8-hex, as app/node_ref.py canonicalises it
    public_key  TEXT NOT NULL,      -- full key, lowercase hex, 64 chars for a 32-byte key
    long_name   TEXT,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    PRIMARY KEY (node_ref, public_key)
);
CREATE INDEX IF NOT EXISTS idx_mt_node_key_pub ON mt_node_key(public_key);

-- Fortress score per (tile, team). Decays over time. The owning team's
-- score = current defense. Attacker scores accumulate per attempt.
CREATE TABLE IF NOT EXISTS tile_score (
    season_id   INTEGER NOT NULL,
    geohash     TEXT NOT NULL,
    team        TEXT NOT NULL,           -- 'RED' | 'BLUE'
    score       REAL NOT NULL DEFAULT 0,
    last_update INTEGER NOT NULL,         -- epoch s; used for decay math
    PRIMARY KEY (season_id, geohash, team)
);

-- Unique painters per (tile, team): tracks who's contributed the +1
-- unique-person bonus so we don't double-count.
CREATE TABLE IF NOT EXISTS tile_unique_painter (
    season_id INTEGER NOT NULL,
    geohash   TEXT NOT NULL,
    team      TEXT NOT NULL,
    node_id   INTEGER NOT NULL,
    first_ts  INTEGER NOT NULL,
    paint_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (season_id, geohash, team, node_id)
);

-- Capture audit log for "this tile has been flipped N times" stats
CREATE TABLE IF NOT EXISTS tile_capture_log (
    season_id   INTEGER NOT NULL,
    geohash     TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    by_node_id  INTEGER NOT NULL,
    by_team     TEXT NOT NULL,
    from_team   TEXT,
    packet_id   INTEGER,
    PRIMARY KEY (season_id, geohash, ts)
);
CREATE INDEX IF NOT EXISTS idx_capture_log_tile ON tile_capture_log(season_id, geohash);

-- Capture timestamps for 15-minute defense window.
CREATE TABLE IF NOT EXISTS tile_capture (
    season_id   INTEGER NOT NULL,
    geohash     TEXT NOT NULL,
    captured_at INTEGER NOT NULL,        -- epoch s
    captured_by_team TEXT NOT NULL,
    PRIMARY KEY (season_id, geohash)
);

-- Generic key/value cursor for poll bookmarks etc.
CREATE TABLE IF NOT EXISTS cursor (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

-- Per-node activity in current season window, used for next snake draft.
CREATE TABLE IF NOT EXISTS activity (
    node_id     INTEGER NOT NULL,
    window_id   INTEGER NOT NULL,            -- typically current season_id, but the active window
    packet_count INTEGER NOT NULL DEFAULT 0,
    last_seen   INTEGER NOT NULL,
    PRIMARY KEY (node_id, window_id)
);

CREATE INDEX IF NOT EXISTS idx_activity_window ON activity(window_id);

-- Track which packets we've already processed (de-dup the poll loop).
CREATE TABLE IF NOT EXISTS processed_packet (
    packet_id   INTEGER PRIMARY KEY,
    processed_at INTEGER NOT NULL
);

-- ---------------------------------------------------------------------
-- Player identity and MeshCore ingest tables, added in Phase 2.
-- Nothing reads from these yet.
-- ---------------------------------------------------------------------

-- One row per registered person.
CREATE TABLE IF NOT EXISTS player (
    player_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name  TEXT NOT NULL,
    team          TEXT NOT NULL,           -- 'RED' | 'BLUE'
    created_at    INTEGER NOT NULL,
    disabled_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_player_team ON player(team);

-- History of team changes. Exists so a player's once-per-calendar-month
-- self-switch allowance (app/join_api.py's switch_team()) can be
-- checked without touching player itself, and so an operator override
-- (app/admin_api.py's admin_set_team()) is auditable. Deliberately NOT
-- read by anything on the scoring path: mc_tile.owner_team is frozen
-- at paint time and never re-derived from player.team, and check-in /
-- exploration points and streaks all join live on player.team already
-- -- a team change moves those for free and leaves ground exactly
-- where it was. This table only ever gains a row; nothing deletes from
-- it.
CREATE TABLE IF NOT EXISTS player_team_change (
    change_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id   INTEGER NOT NULL,
    from_team   TEXT NOT NULL,
    to_team     TEXT NOT NULL,
    changed_at  INTEGER NOT NULL,
    actor       TEXT NOT NULL           -- 'player' | 'operator'
);
CREATE INDEX IF NOT EXISTS idx_player_team_change_player ON player_team_change(player_id, changed_at);

-- Which radios belong to which person.
CREATE TABLE IF NOT EXISTS player_node (
    protocol   TEXT NOT NULL,              -- 'meshtastic' | 'meshcore'
    node_ref   TEXT NOT NULL,
    player_id  INTEGER NOT NULL,
    bound_at   INTEGER NOT NULL,
    -- Public key supplied at registration, mirroring mt_node_key above:
    -- the key is the stable identity, node_ref is not (2.8 can change
    -- an id under a node, and two nodes can collide on one). This is
    -- metadata only -- a position packet still carries nothing but a
    -- node id, so attribution still keys on node_ref exactly as before.
    -- Nullable because most bindings predate this column and supplying
    -- one is optional.
    public_key TEXT,
    PRIMARY KEY (protocol, node_ref)
);
CREATE INDEX IF NOT EXISTS idx_player_node_player ON player_node(player_id);

-- Hashed per-player key MeshMapper sends on every batch.
CREATE TABLE IF NOT EXISTS api_key (
    key_hash      TEXT PRIMARY KEY,
    player_id     INTEGER NOT NULL,
    issued_at     INTEGER NOT NULL,
    revoked_at    INTEGER,
    last_seen_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_api_key_player ON api_key(player_id);

-- Hashed single-use 15 minute registration ticket.
CREATE TABLE IF NOT EXISTS join_token (
    token_hash   TEXT PRIMARY KEY,
    player_id    INTEGER NOT NULL,
    team         TEXT NOT NULL,            -- 'RED' | 'BLUE'
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    consumed_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_join_token_player ON join_token(player_id);

-- Last known cell per player, for the implausible-speed check only.
CREATE TABLE IF NOT EXISTS player_last_fix (
    player_id  INTEGER NOT NULL,
    protocol   TEXT NOT NULL,
    cell_id    TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    PRIMARY KEY (player_id, protocol)
);

-- One row per accepted ping; serves exact-duplicate detection (the same
-- player/cell/ts arriving twice). Used to also gate the per-cell scoring
-- cooldown on its own, but that blocked the whole ping rather than just
-- re-earning for a repeater already credited -- see
-- player_cell_repeater_credit below, which is what the cooldown reads
-- now (app/mc_scoring.py's apply_paint()).
-- precision_bits: the Meshtastic position-precision value (see
-- settings.mt_min_precision_bits) this specific ping carried, recorded
-- here purely for audit -- nothing reads it back for scoring, which
-- already happened (or didn't) in app/ingest.py before this row was
-- written. NULL for every MeshCore row (no such concept) and for any
-- Meshtastic row from before this column existed.
-- evidence_type: added for app/freqmapper_ingest.py's passive_rx
-- painting (see that module's module docstring, "Passive RX scoring").
-- The smallest honest addition that makes an RX-sourced paint
-- DISTINGUISHABLE from a verified-TX-sourced one after the fact --
-- FreqMapper's own documentation is explicit that passive RX must never
-- be presented or treated as verified TX proof, and this deployment
-- needs to be able to tell the two apart later even though, per Matt's
-- explicit decision, they currently earn identical points (see
-- freqmapper_config.passive_rx_points_per_event's own comment below for
-- why equal credit and distinguishable provenance are two separate
-- questions, not one). Set to the feed's own event_type string
-- ("verified_tx" / "passive_rx") by app/freqmapper_ingest.py's
-- _process_one_event for every FreqMapper-sourced row; NULL for every
-- row written by app/ingest.py (meshview) or app/mc_ingest.py
-- (MeshCore) -- neither of those paths has more than one evidence
-- source to distinguish, so there is nothing for this column to record
-- there, same reasoning precision_bits above stays NULL for a MeshCore
-- row. Purely an audit/provenance column, same as precision_bits:
-- nothing reads it back for scoring, and it plays no part in the
-- exact-duplicate PRIMARY KEY.
-- watcher_count / same_region_watcher_count / cross_region_watcher_count
-- / watcher_corroborated / quality / rssi_dbm / snr_db / hop_count /
-- path_classification / last_relay_node / packet_type / portnum /
-- location_accuracy_meters: added for FreqMapper's combined-feed
-- "capture signal" fields -- see app/freqmapper_ingest.py's module
-- docstring for the full coverage-mapper reasoning. Matt's own words on
-- what this whole column group is for -- first on watcher_count/
-- quality, then widened to every measurement the feed reports: "we can
-- use the watcher count and the quality. in fact we should enter it in
-- the capture, but what we should NOT do is change the scoring weight.
-- at its core, meshwars is a coverage mapper, so we should honor that."
-- and then, extending the same principle to the rest of the feed's
-- fields: "lets store it all, its not that heavy." So: RECORD, never
-- SCORE, for EVERY measurement FreqMapper hands this deployment, not
-- just the two Matt named first.
--
-- Two of these (watcher_count, and its breakdown
-- same_region_watcher_count/cross_region_watcher_count, plus
-- watcher_corroborated) are reported on BOTH verified_tx and passive_rx
-- events -- how many independent Watchers verified or corroborated the
-- event, split by whether they were in the same FreqMapper region as
-- the reporting radio or a different one, and whether at least one
-- corroborating Watcher observation exists at all. The rest
-- (quality, rssi_dbm, snr_db, hop_count, path_classification,
-- last_relay_node, packet_type, portnum, location_accuracy_meters) are
-- passive_rx-ONLY fields -- the verified_tx feed carries none of them
-- at all, so every verified_tx row's copies of these nine columns are
-- unconditionally NULL, never a guess at what they might have been.
-- quality is "strong"/"fair"/"weak"; rssi_dbm and snr_db are the
-- receiving radio's own signal-strength/noise readings for this
-- specific reception (snr_db can legitimately be null even ON a
-- passive_rx event, per FreqMapper's own API docs -- some hardware
-- cannot report it); hop_count is FreqMapper's own estimate of how many
-- relays the packet crossed before this radio heard it, null when the
-- packet's header gives no basis for an estimate; path_classification
-- is FreqMapper's own "direct"/"relayed"/"unknown" summary of that;
-- last_relay_node is a ONE-BYTE HINT of the last relay's node id
-- fragment (Meshtastic's own on-air packet format only ever carries the
-- low byte of a relaying node's id, not its full identity) -- this is
-- NOT a usable node identity on its own and must never be treated as
-- one; packet_type/portnum describe what kind of Meshtastic packet was
-- overheard (app/config.py's position_app_portnum is the same concept,
-- unrelated numbering space); location_accuracy_meters is FreqMapper's
-- own confidence radius for the receiving radio's own reported
-- position, not the transmitter's.
--
-- All thirteen are nullable and populated verbatim by
-- app/freqmapper_ingest.py's _process_one_event -- a field the payload
-- omits, or sends null, is recorded as NULL here, never 0, never
-- False, and never a placeholder string like "unknown": these are raw
-- measurements being kept for later reference, not scoring inputs that
-- need a safe fallback. NULL for every row written by app/ingest.py
-- (meshview) or app/mc_ingest.py (MeshCore), same reasoning
-- evidence_type stays NULL for those paths above -- neither carries any
-- of these fields at all. Nothing in this codebase reads any of these
-- thirteen columns back for scoring, ever -- see freqmapper_config's
-- own comment below (watcher_weight_*) for the operator-flippable
-- scoring switch that USED to exist for watcher_count and was
-- deliberately removed, not merely left unused, so that "record the
-- measurement" could never quietly become "score the measurement"
-- again by an admin flipping one setting.
CREATE TABLE IF NOT EXISTS player_cell_ping (
    player_id                   INTEGER NOT NULL,
    protocol                    TEXT NOT NULL,
    cell_id                     TEXT NOT NULL,
    ts                          INTEGER NOT NULL,
    seen_at                     INTEGER NOT NULL,
    precision_bits              INTEGER,
    evidence_type               TEXT,
    watcher_count               INTEGER,
    same_region_watcher_count   INTEGER,
    cross_region_watcher_count  INTEGER,
    watcher_corroborated        INTEGER,
    quality                     TEXT,
    rssi_dbm                    REAL,
    snr_db                      REAL,
    hop_count                   INTEGER,
    path_classification         TEXT,
    last_relay_node             INTEGER,
    packet_type                 TEXT,
    portnum                     INTEGER,
    location_accuracy_meters    REAL,
    PRIMARY KEY (player_id, protocol, cell_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_player_cell_ping_seen ON player_cell_ping(seen_at);

-- Which repeaters (MeshCore) / feeders (Meshtastic -- both are just
-- RepeaterEntry.repeater_id, see app/mc_ingest.py) a player has already
-- been credited scoring points for, on a given cell, and when.
--
-- This exists because mc_cooldown_seconds' actual job is stopping
-- someone parked in one spot from spamming pings to run up a score --
-- not stopping a player from being credited for genuinely different
-- repeaters heard on the same pass. MeshMapper sends one ping per
-- repeater contact, often a second apart, so a single visit to a square
-- routinely produces several pings in a row, each naming a different
-- repeater. Gating the cooldown on player_cell_ping (any repaint of the
-- same cell, regardless of which repeater) discarded every one of those
-- pings after the first, crediting a player for one repeater when they
-- had actually heard several -- see apply_paint()'s docstring in
-- app/mc_scoring.py for the full story. This table lets the cooldown
-- block re-earning per REPEATER already credited on this cell instead of
-- per ping: `ts` is bumped forward every time a repeater earns fresh
-- credit here, so a row older than mc_cooldown_seconds means that
-- repeater's credit has lapsed and it is free to score again, while a
-- row still inside the window means it is not.
--
-- Brand new table, no existing deployed shape to ALTER, so CREATE TABLE
-- IF NOT EXISTS here is sufficient on its own -- same reasoning as
-- repeater_observation/repeater_identity above; no MIGRATIONS entry
-- needed.
--
-- `ts` is the credited ping's own (attacker-controlled) timestamp --
-- used for the cooldown-window comparison itself, same field
-- recently_painted() used to read from player_cell_ping. `seen_at` is
-- the server receipt time, kept separate for the same reason
-- player_cell_ping keeps the same two columns distinct: retention
-- housekeeping (_housekeeping_sync in app/mc_ingest.py) needs a time
-- base a client can't manipulate to keep a row alive indefinitely or
-- vanish it early.
CREATE TABLE IF NOT EXISTS player_cell_repeater_credit (
    player_id    INTEGER NOT NULL,
    protocol     TEXT NOT NULL,
    cell_id      TEXT NOT NULL,
    repeater_id  TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    seen_at      INTEGER NOT NULL,
    PRIMARY KEY (player_id, protocol, cell_id, repeater_id)
);
CREATE INDEX IF NOT EXISTS idx_player_cell_repeater_credit_seen ON player_cell_repeater_credit(seen_at);

-- Per-player per-day counters, so we can tell a player why they are not
-- scoring.
CREATE TABLE IF NOT EXISTS player_ingest_stat (
    player_id          INTEGER NOT NULL,
    protocol           TEXT NOT NULL,
    day                INTEGER NOT NULL,
    batches            INTEGER NOT NULL DEFAULT 0,
    pings_accepted     INTEGER NOT NULL DEFAULT 0,
    pings_no_contact   INTEGER NOT NULL DEFAULT 0,
    pings_wrong_owner  INTEGER NOT NULL DEFAULT 0,
    pings_duplicate    INTEGER NOT NULL DEFAULT 0,
    pings_bad_coord    INTEGER NOT NULL DEFAULT 0,
    pings_out_of_area  INTEGER NOT NULL DEFAULT 0,
    pings_no_repeaters INTEGER NOT NULL DEFAULT 0,
    -- Meshtastic-only today (app/ingest.py) -- see settings.mt_min_precision_bits
    -- and settings.mt_max_speed_mps. Always 0 for protocol='mc': MeshCore's
    -- own speed check (app/mc_ingest.py) never rejects a ping, only marks
    -- by_air, and MeshCore has no equivalent precision_bits concept at all.
    pings_low_precision     INTEGER NOT NULL DEFAULT 0,
    pings_implausible_speed INTEGER NOT NULL DEFAULT 0,
    -- MeshCore-only (app/mc_ingest.py's parse_repeaters()): a ping whose
    -- `type` field is PRESENT but not one of the four recognized values
    -- (TX/RX/DISC/TRACE) -- e.g. a future MeshMapper build's "DEFER".
    -- Never rejected: the ping is still accepted and still writes a
    -- position row exactly as before, it just cannot be told apart from
    -- a legitimate ping that heard no repeaters without this counter, so
    -- it also still counts toward pings_no_repeaters (parse_repeaters()
    -- falls through to an empty list either way) -- this is additive
    -- observability, not a new rejection path. A MISSING/None `type` is
    -- deliberately NOT counted here: that's an absent field, not an
    -- unrecognized one, and is already indistinguishable from a
    -- legitimate empty read the same way it always was. Always 0 for
    -- protocol='mt': app/ingest.py's packets carry no `type` field of
    -- this kind at all.
    pings_unknown_type INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (player_id, protocol, day)
);

-- Durable queue for MeshCore wardriving batches accepted by
-- POST /api/mc/ingest, replacing the in-process asyncio.Queue
-- McIngestor (app/mc_ingest.py) used to hold them in before this table
-- existed. That queue lost every pending batch on restart -- and every
-- deploy restarts -- silently discarding real player scoring data. One
-- row per accepted HTTP batch (not one row per ping): `payload` is the
-- JSON-serialized `pings` list exactly as POSTed, so the worker can
-- replay it through the same McIngestor._process_batch_sync() a batch
-- pulled straight off the old in-memory queue always went through.
--
-- Per-ping processing is idempotent (player_cell_ping's own PRIMARY KEY
-- (player_id, protocol, cell_id, ts) makes the INSERT OR IGNORE dedup
-- check in _process_one_ping gate every downstream effect -- scoring,
-- place credit, repeater-observation recording, last-fix update -- so
-- reprocessing an already-processed ping is a safe no-op, not a double
-- score), which is what makes at-least-once delivery (claim, process,
-- DELETE on success) the correct and simplest choice here rather than
-- needing a second dedup layer on top of this table. See
-- McIngestor._process_queued_row()'s own comment for the full
-- reasoning.
--
-- claimed_at marks a row a worker has picked up but not yet finished:
-- the claim itself is a single atomic UPDATE ... RETURNING (see
-- McIngestor._claim_batch()) so two workers can never claim the same
-- row, even though this deployment only ever runs one. A claim that
-- outlives its worker (a crash mid-batch) is released back
-- (claimed_at = NULL) by McIngestor._reset_stale_claims() the next time
-- a worker starts -- safe because a claim surviving into a fresh
-- process start can only be orphaned, never genuinely in flight.
--
-- attempts/last_error are the same "a poison row must not wedge the
-- queue forever" shape discord_outbox already uses (see
-- settings.discord_outbox_max_attempts): a row that keeps failing is
-- left in the table, never deleted, but stops being claimed once
-- attempts reaches settings.mc_queue_max_attempts (a plain WHERE
-- clause in the claim query, not a separate "dead" flag) -- an operator
-- can still see exactly which batch is stuck and why via last_error.
--
-- Brand new table, no existing deployed shape to ALTER, so CREATE TABLE
-- IF NOT EXISTS here is sufficient on its own -- same reasoning as
-- player_cell_repeater_credit/repeater_observation above; no MIGRATIONS
-- entry needed.
CREATE TABLE IF NOT EXISTS mc_ingest_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id     INTEGER NOT NULL,
    key_hash      TEXT NOT NULL,
    payload       TEXT NOT NULL,
    received_at   INTEGER NOT NULL,
    enqueued_at   INTEGER NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    claimed_at    INTEGER
);
-- Drives both McIngestor._claim_batch()'s own query (claimed_at IS NULL
-- AND attempts < ? ORDER BY id) and, indirectly, submit()'s COUNT(*)
-- capacity check.
CREATE INDEX IF NOT EXISTS idx_mc_ingest_queue_claim ON mc_ingest_queue(claimed_at, attempts, id);

-- One row per FreqMapper coverage event ever processed
-- (app/freqmapper_ingest.py). verification_id is that event's whole
-- identity -- for a verified_tx event, the event's own `verification_id`
-- field, a stable UUID FreqMapper itself assigns, one per event, never
-- reused; for a passive_rx event, its own `reception_id` field instead
-- (a separate, independently-assigned UUID space -- see below); for any
-- event_type this code does not recognize, the feed's generic top-level
-- `event_id` field, purely as a best-effort fallback since a future
-- event type carries no field name this code can know in advance. So
-- this is a pure dedup table: INSERT OR IGNORE on the primary key means
-- an event already seen (a page re-fetched after a restart before the
-- cursor was persisted, a retry, an overlapping page) is a no-op rather
-- than a re-processed, re-scored event. Recorded for EVERY event that
-- reaches this check, regardless of whether the radio turns out to be
-- registered or in bounds, or which source is currently painting the
-- Meshtastic board (settings.mt_paint_source) -- this table's only job
-- is "have we ever looked at this specific event before," not "did it
-- score." Pruned well past FreqMapper's own paging window by
-- app/freqmapper_ingest.py's own housekeeping, the same reasoning
-- app/mc_ingest.py's retention windows use, so this cannot grow without
-- bound on a long-running deployment.
--
-- History: briefly renamed to `event_id` and prefixed ("verified_tx:
-- <uuid>" / "passive_rx:<uuid>") by commit d114a5a's combined-feed
-- cutover, which believed the combined feed's own dedup key needed that
-- prefixed form. It did not -- verified against the live API, a
-- verified_tx event's `verification_id` field already carries the exact
-- same UUID as the bare half of its `event_id`, so no schema change was
-- ever required, and the actual production incident that migration was
-- meant to guard against had a different cause entirely (see
-- app/freqmapper_ingest.py's module docstring -- the incident was a
-- cursor-cutover backfill, not a dedupe-key mismatch). See
-- _migrate_freqmapper_verification_verification_id below for the
-- one-time, idempotent migration that converges every deployment --
-- including preview and any operator who ran d114a5a even briefly --
-- back onto this original `verification_id` shape.
CREATE TABLE IF NOT EXISTS freqmapper_verification (
    verification_id TEXT PRIMARY KEY,
    seen_at         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_freqmapper_verification_seen ON freqmapper_verification(seen_at);

-- Singleton, same upsert-by-fixed-id shape as `checkin_config` above --
-- read FRESH by app/freqmapper_ingest.py's poller on every cycle (see
-- that module's load_freqmapper_config, never cached in the process),
-- which is the whole point: an admin edit through
-- app/admin_ops.py's /api/admin/paint takes effect on the very next
-- poll, no restart. mt_paint_source moves here too, off settings.py --
-- it is the same single switch app/ingest.py's meshview poll/backfill
-- and this table's own poller both read, so the two can never disagree
-- about which source(s) are currently allowed to paint the Meshtastic
-- board. Three values: 'meshview', 'freqmapper', or 'both' -- see that
-- column's own comment in config.py, kept as this table's authoritative
-- copy now. Default is 'both': a fresh install runs both painters with
-- no admin action, since the shared cooldown/capture machinery in
-- app/mc_scoring.py already absorbs the same cell being touched by two
-- sources -- see that column's own comment in config.py for why this is
-- not a preference between the two, just the normal starting state.
-- seed_freqmapper_config_from_env (app/freqmapper_ingest.py, called
-- from init_db() below) bootstraps this row from settings.py's
-- freqmapper_*/mt_paint_source values the first time it is ever
-- touched, the same guarded-by-updated_at pattern
-- app/checkin.py's seed_nets_from_env uses for checkin_config, so
-- deploying this table changes NO behavior on its own.
-- api_key is a SECRET (see app/db.py's checkin_net comment on
-- broker_password/channel_key for the general rule this follows):
-- never returned by any route, only a has_api_key boolean
-- (app/admin_ops.py's _scrub_freqmapper_secrets). last_poll_at/
-- last_poll_error mirror checkin_net's own per-net poll-status
-- columns, written by FreqMapperIngestor after every completed cycle
-- (cleared on the next success), so a silently-failing connector shows
-- up here without anyone reading logs.
-- paint_from is checkin_net.start_date's exact contract, one level up:
-- a local YYYY-MM-DD lower bound on an event's verified_at, empty
-- meaning BLOCK EVERY EVENT rather than "no lower bound" -- see that
-- column's own comment and settings.freqmapper_paint_from in
-- app/config.py for the full reasoning, and app/freqmapper_ingest.py's
-- _process_one_event for where it's enforced. A date-skipped event is
-- deliberately left OUT of freqmapper_verification below (unlike every
-- other skip reason, which IS recorded there) so that moving this date
-- earlier and clearing the cursor can still pick the event back up.
-- watcher_weight_* (added in MIGRATIONS below, after this table already
-- shipped): RETAINED, DELIBERATELY UNREAD. These four columns used to
-- back an optional, operator-flippable scaling of a verified_tx event's
-- points by the combined feed's watcher_count field -- OFF by default,
-- but a live switch nonetheless. That scaling path
-- (app/freqmapper_ingest.py's old _verified_tx_points()) has been
-- removed entirely, not just left disabled: Matt's explicit decision is
-- "we can use the watcher count and the quality... but what we should
-- NOT do is change the scoring weight. at its core, meshwars is a
-- coverage mapper, so we should honor that." A neutral-by-default
-- switch that contradicts a stated design principle is a landmine, not
-- a safety net -- leaving it reachable through the admin config only
-- means someone flips it on months from now, with no code review of
-- the decision it re-opens, and MeshWars quietly stops scoring coverage
-- as coverage. Every verified_tx paint is worth points_per_event above,
-- always, with no code path left anywhere that reads watcher_count back
-- for scoring purposes (see player_cell_ping.watcher_count's own
-- comment above for where that field is now actually recorded --
-- capture, not credit). These four columns are kept, unread, purely
-- because dropping a column is a disruptive SQLite migration
-- (CREATE TABLE ... AS SELECT, swap, DROP) for zero benefit once
-- nothing references them -- see this table's own MIGRATIONS entry for
-- the same note at the point they were added.
-- allow_backfill (added in MIGRATIONS below, after this table already
-- shipped, in response to the incident this whole migration file
-- exists to fix -- see app/freqmapper_ingest.py's module docstring):
-- the operator opt-in for the high-water-mark backfill guard in
-- _process_one_event. OFF by default, the safe direction -- a fresh or
-- freshly upgraded deployment keeps the guard active and never paints
-- historical events just because they happen to be new to this
-- deployment (a cleared cursor, a first-ever backfill, a switched
-- feed) -- exactly the failure mode that produced the incident this
-- column exists to let an operator deliberately re-enable, not repeat
-- by accident. When set, the guard is bypassed entirely: an event
-- older than the stored high-water mark paints exactly as if it were
-- current, for an operator who has a real, deliberate reason to want
-- history painted (e.g. onboarding this deployment against a
-- FreqMapper account with pre-existing coverage history).
-- passive_rx_* (added in MIGRATIONS below, after this table already
-- shipped): scoring config for FreqMapper's passive_rx event type --
-- see app/freqmapper_ingest.py's module docstring ("Passive RX
-- painting") for the semantics of what a passive_rx event actually
-- proves (the RECEIVING wardriving radio's own position, not the
-- sender's) and _process_one_event for exactly where these are read.
-- passive_rx_enabled defaults to 1 (ON): this is the feature the
-- deployment was built to ship, not an opt-in an operator has to
-- discover -- unlike allow_backfill and watcher_weight_enabled above,
-- there is no "deploying this must change nothing" constraint here to
-- protect, since passive RX never painted anything before this feature
-- existed at all; ON-by-default is what makes it actually work the
-- moment this migration runs, with no database edit required.
-- passive_rx_points_per_event and passive_rx_unique_painter_bonus
-- default to 0.5 each -- IDENTICAL to points_per_event/
-- unique_painter_bonus's own defaults above, by Matt's explicit
-- decision ("coverage is coverage"): a passive_rx event proves the
-- wardriving radio genuinely heard traffic at that location, which is
-- coverage at that location exactly as much as an independently-
-- Watcher-verified transmission is. These are kept as their OWN
-- columns, never a read of points_per_event/unique_painter_bonus
-- themselves, purely so the two evidence types stay independently
-- tunable later -- the equal starting value is a deliberate choice
-- about what this deployment currently believes RX and TX are worth,
-- not a structural inability to tell them apart; see player_cell_ping.
-- evidence_type's own comment above for the mechanism that keeps the
-- two DISTINGUISHABLE after the fact regardless of what either is
-- currently worth: equal points, distinct labels. Points/bonus are
-- never weighted by quality/rssi/snr/hop_count/path_classification
-- here -- deliberately deferred, an open question still being
-- discussed with FreqMapper, not a decision this deployment makes on
-- its own -- see app/freqmapper_ingest.py's _passive_rx_points() for
-- where that plumbing already exists, unused, ready for that decision
-- once it is made.
CREATE TABLE IF NOT EXISTS freqmapper_config (
    id                     INTEGER PRIMARY KEY CHECK (id = 1),
    mt_paint_source        TEXT NOT NULL DEFAULT 'both',
    enabled                INTEGER NOT NULL DEFAULT 0,
    base_url               TEXT NOT NULL DEFAULT '',
    api_key                TEXT NOT NULL DEFAULT '',
    poll_interval_seconds  INTEGER NOT NULL DEFAULT 60,
    page_limit             INTEGER NOT NULL DEFAULT 200,
    points_per_event       REAL NOT NULL DEFAULT 0.5,
    unique_painter_bonus   REAL NOT NULL DEFAULT 0.5,
    paint_from             TEXT NOT NULL DEFAULT '',
    last_poll_at           INTEGER,
    last_poll_error        TEXT,
    updated_at             INTEGER NOT NULL DEFAULT 0,
    allow_backfill         INTEGER NOT NULL DEFAULT 0,
    watcher_weight_enabled   INTEGER NOT NULL DEFAULT 0,
    watcher_weight_base      REAL NOT NULL DEFAULT 0.5,
    watcher_weight_increment REAL NOT NULL DEFAULT 0.1,
    watcher_weight_cap       REAL NOT NULL DEFAULT 1.0,
    passive_rx_enabled              INTEGER NOT NULL DEFAULT 1,
    passive_rx_points_per_event     REAL NOT NULL DEFAULT 0.5,
    passive_rx_unique_painter_bonus REAL NOT NULL DEFAULT 0.5
);

-- ---------------------------------------------------------------------
-- MeshCore-model scoring tables. Both boards run on this model now,
-- flat grid cells and players instead of the retired geohash tile/
-- tile_score tables above. Those legacy tables are kept, unwritten,
-- purely so their three completed seasons of history stay readable --
-- see app/api.py's module docstring for the full story of the cutover.
-- ---------------------------------------------------------------------

-- One MeshCore season at a time; mirrors `season` above but tallies teams
-- (there can be more than two) instead of fixed red/blue/green columns.
--
-- `protocol` is the ONLY column that separates the MeshCore board from
-- the Meshtastic board on this shared model ('mc' / 'mt'). Deliberately
-- placed here and nowhere else: every other mc_* table keys off
-- season_id, so as long as a season's protocol never changes and every
-- lookup filters on it, the two boards stay fully independent without
-- needing a protocol column on the tile tables too.
CREATE TABLE IF NOT EXISTS mc_season (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol    TEXT NOT NULL DEFAULT 'mc',  -- 'mc' | 'mt'
    started_at  INTEGER NOT NULL,
    ends_at     INTEGER NOT NULL,
    status      TEXT NOT NULL,
    winner      TEXT
);
CREATE INDEX IF NOT EXISTS idx_mc_season_status ON mc_season(protocol, status);

-- Tile count per team, written once at season close.
CREATE TABLE IF NOT EXISTS mc_season_team_tally (
    season_id  INTEGER NOT NULL,
    team       TEXT NOT NULL,
    tiles      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (season_id, team)
);

-- One row per (season, cell) that has ever been captured. No neutral
-- state: a cell either has an owner row or does not exist yet.
-- lat_idx/lon_idx are cell_id's two halves, stored as integers so a
-- rectangular block of the grid can be found by an indexed range scan
-- rather than by parsing every row's id. They are redundant with cell_id
-- by construction and must never disagree with it: everything writing a
-- row here fills them from app/grid.py's cell_indices(), which is the
-- single definition of how an id splits. Nullable only so the migration
-- that added them to existing databases could backfill; a row written by
-- this application always has both.
CREATE TABLE IF NOT EXISTS mc_tile (
    season_id       INTEGER NOT NULL,
    cell_id         TEXT NOT NULL,
    owner_team      TEXT NOT NULL,
    last_player_id  INTEGER NOT NULL,
    last_report_ts  INTEGER NOT NULL,
    paint_count     INTEGER NOT NULL DEFAULT 0,
    lat_idx         INTEGER,
    lon_idx         INTEGER,
    PRIMARY KEY (season_id, cell_id)
);
CREATE INDEX IF NOT EXISTS idx_mc_tile_owner ON mc_tile(season_id, owner_team);

-- Per-(tile, team) score. Decays on read, never stored pre-decayed.
CREATE TABLE IF NOT EXISTS mc_tile_score (
    season_id    INTEGER NOT NULL,
    cell_id      TEXT NOT NULL,
    team         TEXT NOT NULL,
    score        REAL NOT NULL DEFAULT 0,
    last_update  INTEGER NOT NULL,
    PRIMARY KEY (season_id, cell_id, team)
);

-- Unique painters per (tile, team): tracks who's contributed the
-- one-time unique-player bonus so it isn't double-counted.
CREATE TABLE IF NOT EXISTS mc_tile_unique_painter (
    season_id    INTEGER NOT NULL,
    cell_id      TEXT NOT NULL,
    team         TEXT NOT NULL,
    player_id    INTEGER NOT NULL,
    first_ts     INTEGER NOT NULL,
    paint_count  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (season_id, cell_id, team, player_id)
);

-- Capture timestamp per tile, for the post-capture defense window.
CREATE TABLE IF NOT EXISTS mc_tile_capture (
    season_id         INTEGER NOT NULL,
    cell_id           TEXT NOT NULL,
    captured_at       INTEGER NOT NULL,
    captured_by_team  TEXT NOT NULL,
    PRIMARY KEY (season_id, cell_id)
);

-- Capture audit log: every flip, who did it, and who it was taken from.
CREATE TABLE IF NOT EXISTS mc_tile_capture_log (
    season_id    INTEGER NOT NULL,
    cell_id      TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    by_player_id INTEGER NOT NULL,
    by_team      TEXT NOT NULL,
    from_team    TEXT,
    by_air       INTEGER NOT NULL DEFAULT 0,  -- claimed while moving at aircraft speed (see app/mc_ingest.py)
    PRIMARY KEY (season_id, cell_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_mc_capture_log_cell ON mc_tile_capture_log(season_id, cell_id);

-- ---------------------------------------------------------------------
-- Repeater observation evidence. Purely data collection for now -- future
-- work will generate points of interest from what a square can actually
-- hear, instead of guessing, and that needs to know which repeaters are
-- audible from where. Nothing here reads from these tables yet, nothing
-- here changes scoring, and nothing here is new to the schema in the
-- migration sense: both tables are brand new, so CREATE TABLE IF NOT
-- EXISTS in SCHEMA is sufficient on its own -- there is no existing,
-- already-deployed table shape to ALTER, which is the only reason an
-- entry would need to go in MIGRATIONS instead.
--
-- A repeater can appear in a ping two different ways, and they mean
-- different things -- this distinction is the entire point of splitting
-- direct_count from heard_count below, and it must never be collapsed:
--   * DISC/TRACE pings carry `repeater_id` plus local_snr, local_rssi,
--     and remote_snr -- a directly measured relationship between this
--     one position and this one named repeater.
--   * TX/RX pings carry `heard_repeats`, e.g. "5331(12.50),a1b2(-3.0)"
--     -- what came back through the mesh, which may include repeaters
--     reached over multiple hops. This describes the network's reach
--     from this square, not necessarily a direct line to the position.
-- A wardriver standing beneath their own well-connected repeater would
-- be indistinguishable from one on a ridge with genuine multi-hop reach
-- if these were merged into one counter -- so they never are.
-- ---------------------------------------------------------------------

-- One row per (protocol, repeater_id, cell_id) ever observed. Cell-level
-- only: no player id and no raw coordinate are stored here, matching the
-- privacy rule the rest of MeshCore ingest already follows -- this is
-- aggregate evidence about places, not a record of who was where.
CREATE TABLE IF NOT EXISTS repeater_observation (
    protocol        TEXT NOT NULL,
    repeater_id     TEXT NOT NULL,
    cell_id         TEXT NOT NULL,
    first_seen      INTEGER NOT NULL,
    last_seen       INTEGER NOT NULL,
    direct_count    INTEGER NOT NULL DEFAULT 0,
    heard_count     INTEGER NOT NULL DEFAULT 0,
    best_local_snr  REAL,
    best_remote_snr REAL,
    best_heard_snr  REAL,
    PRIMARY KEY (protocol, repeater_id, cell_id)
);
CREATE INDEX IF NOT EXISTS idx_repeater_obs_cell ON repeater_observation(protocol, cell_id);

-- One row per (protocol, repeater_id) ever observed directly. Only
-- DISC/TRACE pings carry public_key and node_type, so those columns are
-- nullable and only ever filled in from those ping types; the most
-- recently seen non-null values are kept.
CREATE TABLE IF NOT EXISTS repeater_identity (
    protocol    TEXT NOT NULL,
    repeater_id TEXT NOT NULL,
    public_key  TEXT,
    node_type   TEXT,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    PRIMARY KEY (protocol, repeater_id)
);

-- ---------------------------------------------------------------------
-- Net check-ins (app/checkin.py): a second way to earn points, alongside
-- squares held. A weekly net runs Wednesday evenings; checking in on
-- either board's feed during that window earns a registered player's
-- team settings.checkin_points once per player per net. Both tables
-- below are brand new, so CREATE TABLE IF NOT EXISTS here is sufficient
-- on its own -- same reasoning as repeater_observation/repeater_identity
-- above: there is no existing, already-deployed shape to ALTER, which
-- is the only reason an entry would need to go in MIGRATIONS instead.
-- ---------------------------------------------------------------------

-- RETIRED, no longer read or written by anything. Used to hold an
-- explicit MeshCore display-name -> player binding, the LAST-RESORT
-- fallback in app/checkin.py's identity resolution for a player whose
-- radio contact had never shown up in the live.mwmesh.com directory --
-- a player typed the name their radio posted under, in place of the
-- key-anchored proof every other path here has. Retired once node
-- confirmation (app/checkin_api.py's POST /api/checkin/confirm/accept)
-- shipped: a live re-advertised proof of possession is strictly
-- stronger than a typed name for exactly the players who needed this
-- table, and it had zero rows bound on preview at retirement. Left in
-- place, empty, per this codebase's no-drop convention (see MIGRATIONS
-- below) rather than dropped -- production's contents were never
-- checked and are out of scope for that decision.
CREATE TABLE IF NOT EXISTS mc_checkin_binding (
    sender_name  TEXT NOT NULL,
    player_id    INTEGER NOT NULL UNIQUE,
    bound_at     INTEGER NOT NULL,
    PRIMARY KEY (sender_name)
);
CREATE INDEX IF NOT EXISTS idx_mc_checkin_binding_player ON mc_checkin_binding(player_id);

-- One row per (season, player, local net date) that has earned a
-- check-in award -- that triple IS the natural key: neither feed has a
-- session concept, and a player who posts several times in one net
-- (MeshCore senders routinely do) must still only be credited once.
-- `points` is copied from settings.checkin_points at award time, not
-- read live at every scoring query, so a later config change can never
-- rewrite the value of a check-in that already happened -- same reason
-- mc_tile_score stores a number instead of a formula.
CREATE TABLE IF NOT EXISTS mc_checkin_award (
    season_id   INTEGER NOT NULL,
    player_id   INTEGER NOT NULL,
    net_date    TEXT NOT NULL,     -- local net date, e.g. "2026-08-19" (see app/checkin.py's net_date_for_ts)
    points      REAL NOT NULL,
    protocol    TEXT NOT NULL,     -- 'mc' | 'mt' -- which feed earned it; informational, season_id already implies it (see mc_season.protocol)
    message_id  TEXT NOT NULL,     -- source message/packet id, audit only
    awarded_at  INTEGER NOT NULL,
    message_ts  INTEGER,            -- when the player actually POSTED, not when the poller saw it; null on rows written before this column existed
    streak      INTEGER,            -- consecutive nets including this one; null on rows written before streaks existed
    net_id      INTEGER,            -- checkin_net.id that produced this award; null on rows written before this column existed (see MIGRATIONS below and tools/backfill_net_id.py) and on rows written by the admin manual-credit endpoint when the net is ambiguous. checkin_streak() scopes by THIS, not protocol -- see that function's own comment for why protocol-only scoping breaks once two nets share a protocol and their dates interleave.
    PRIMARY KEY (season_id, player_id, net_date)
);
CREATE INDEX IF NOT EXISTS idx_mc_checkin_award_season ON mc_checkin_award(season_id);

-- Dedup for the MeshCore weekly-net poller, same role
-- app/db.py's processed_packet table plays for Meshtastic polling (the
-- Meshtastic check-in poller reuses that existing table directly, since
-- meshview packet ids are already globally unique regardless of
-- portnum -- see app/checkin.py). MeshCore's feed has no equivalent
-- shared table to reuse, so this is its own: one row per weekly-net
-- packetId ever seen, so re-fetching the same message on a later poll
-- (the feed returns its newest 100 messages with no pagination) is a
-- no-op rather than a re-processed message.
CREATE TABLE IF NOT EXISTS mc_checkin_seen_message (
    packet_id  INTEGER PRIMARY KEY,
    seen_at    INTEGER NOT NULL
);

-- ---------------------------------------------------------------------
-- Net check-ins, take two: DB-backed nets, admin-editable at runtime
-- with no restart, supporting MULTIPLE nets across MULTIPLE connector
-- instances -- see app/checkin.py's module docstring for the full
-- design. mc_checkin_award above is unchanged and still the one award
-- table (mc_checkin_binding, also above, is retired -- see its own
-- comment); only "what nets exist, on what schedule, against what
-- upstream" and "what settled message ids has the poller already
-- looked at" move into the database here.
--
-- One row per net. Connector + window + channel-or-hashtag together,
-- deliberately: a net without a connector cannot be polled, and a
-- connector without a window cannot ever close, so splitting those
-- into separate tables would only invite one existing without the
-- other. protocol drives which of channel/hashtag actually means
-- anything -- see app/checkin.py's module docstring for why MeshCore
-- (channel-scoped feed) and Meshtastic (hashtag-in-any-channel) are
-- deliberately asymmetric here, not unified into one shared field.
-- last_poll_at/last_poll_error are the per-net counterpart of
-- CheckinPoller's own in-memory last_poll_at/last_poll_error (see that
-- class's docstring) -- those stay in memory as a whole-poller
-- heartbeat; these persist so the admin nets list can show which
-- SPECIFIC net is failing, surviving a process restart.
CREATE TABLE IF NOT EXISTS checkin_net (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    label         TEXT NOT NULL,
    protocol      TEXT NOT NULL,              -- 'mc' | 'mt' -- the SCORING-BOARD
                                                -- discriminator (mc_checkin_award.protocol,
                                                -- checkin_streak, mc_season). DERIVED from
                                                -- `kind` on every admin write (see
                                                -- app/admin_ops.py's _validate_net_fields /
                                                -- app/checkin.py's KIND_PROTOCOL) and stored
                                                -- alongside it rather than computed on every
                                                -- read, so the two columns are validated to
                                                -- agree at write time and every scoring query
                                                -- can keep reading the plain column it always
                                                -- has.
    kind          TEXT NOT NULL DEFAULT '',   -- 'corescope' | 'beacon' | 'meshview' -- the
                                                -- admin-CHOSEN connector implementation:
                                                -- which upstream API this net's connector_url
                                                -- actually speaks. Two kinds ('corescope' and
                                                -- 'beacon') both drive protocol='mc' -- see
                                                -- app/checkin.py's CoreScopeClient/BeaconClient
                                                -- for why both are channel-scoped, directory-
                                                -- backed MeshCore feeds that normalize to the
                                                -- exact same shape and can therefore share
                                                -- every line of identity-resolution code below
                                                -- them, even though their upstream APIs
                                                -- disagree on nearly everything else (field
                                                -- names, timestamp units, whether a channel is
                                                -- addressed by name or by an instance-local
                                                -- numeric id).
    connector_url TEXT NOT NULL,              -- base URL, no trailing slash. mqtt/mqtts:// for
                                                -- kind='mqtt' (a broker), http(s):// for every
                                                -- other kind (an HTTP API) -- see
                                                -- app/admin_ops.py's _validate_net_fields.
    channel       TEXT NOT NULL DEFAULT '',   -- corescope/beacon: channel NAME (never a
                                                -- Beacon instance-local numeric id -- see
                                                -- BeaconClient).  meshview/mqtt: unused, ''
    hashtag       TEXT NOT NULL DEFAULT '',   -- meshview/mqtt: '#freq51'.  corescope/beacon: unused, ''
    weekday       INTEGER NOT NULL,           -- python datetime.weekday(): 0=Mon .. 6=Sun
    start_hour    INTEGER NOT NULL,
    end_hour      INTEGER NOT NULL,           -- inclusive, so 23 means 23:59:59
    timezone      TEXT NOT NULL,              -- IANA, e.g. America/Boise
    start_date    TEXT NOT NULL DEFAULT '',   -- '' means BLOCK ALL (same convention as today)
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    INTEGER NOT NULL,
    last_poll_at  INTEGER,
    last_poll_error TEXT,
    -- mqtt-only connector config (app/mqtt_subscriber.py). Blank/unused
    -- for every other kind, the same convention channel/hashtag above
    -- already use for the kind that doesn't need them. broker_username/
    -- topic_root are plain config; broker_password/channel_key are
    -- SECRETS -- GET /api/admin/checkin/nets NEVER returns these two
    -- columns' values, only has_broker_password/has_channel_key booleans
    -- (see app/admin_ops.py's _scrub_secrets) -- a config screen that
    -- echoes a broker password back in plaintext is how credentials end
    -- up in screenshots. broker_password: NOT NULL DEFAULT '' means "no
    -- password" (many public/test brokers have none), not "unset".
    -- channel_key: base64 PSK; '' means the Meshtastic default channel
    -- key (index-1 shorthand, "AQ==") -- see mqtt_subscriber.py's
    -- _expand_channel_key for the exact expansion this mirrors from
    -- Meshtastic firmware's Channels::getKey(). topic_root: e.g.
    -- 'msh/US'; '' means subscribe broadly ('#') rather than narrowing
    -- to one region -- see MqttSubscriber's per-broker subscription.
    broker_username TEXT NOT NULL DEFAULT '',
    broker_password TEXT NOT NULL DEFAULT '',
    channel_key     TEXT NOT NULL DEFAULT '',
    topic_root      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_checkin_net_enabled ON checkin_net(enabled, protocol);

-- Push-subscription buffer for the mqtt connector kind
-- (app/mqtt_subscriber.py's MqttSubscriber). MQTT is a persistent
-- broker connection, not a 30-second HTTP poll like every other
-- connector kind -- a long-lived connection has no business living
-- inside CheckinPoller's poll loop (it would either block the loop or
-- have to be reopened every cycle, defeating "persistent"). Instead the
-- subscriber writes every matching decoded text message here as it
-- arrives, and CheckinPoller reads this table for a 'mqtt' net exactly
-- the way it reads an HTTP response for every other kind -- see
-- app/checkin.py's _fetch_mqtt_messages -- which is what keeps net
-- rows, windows, the read-first dedupe, identity resolution, and
-- awarding completely unchanged for this fourth kind, and is what lets
-- a broker disconnect survive without losing anything: the buffer is
-- still here on the next poll cycle no matter how long the subscriber
-- took to reconnect.
--
-- connector is the net's connector_url (an mqtt(s):// broker URL, one
-- row per distinct message per broker, shared by every net configured
-- against that broker the same way an HTTP connector_url is already
-- shared -- see app/checkin.py's module docstring). packet_id is the
-- MeshPacket id, decimal string (both the JSON and encrypted-protobuf
-- topics carry the same 32-bit packet id for the same message, so if a
-- broker happens to publish both forms of one message, INSERT OR IGNORE
-- on this primary key harmlessly keeps only the first one seen, rather
-- than double-crediting it -- app/checkin.py's checkin_seen_message
-- dedupe, keyed the same (connector, packet_id) way, is a second,
-- independent reason it could only ever be credited once regardless).
-- Pruned well past any net's window by app/mqtt_subscriber.py's own
-- housekeeping (settings.mqtt_buffer_retention_hours) so this cannot
-- grow without bound on a busy or long-unpolled connector.
CREATE TABLE IF NOT EXISTS mqtt_message_buffer (
    connector    TEXT NOT NULL,
    packet_id    TEXT NOT NULL,
    from_node    INTEGER NOT NULL,
    channel_name TEXT NOT NULL DEFAULT '',
    text         TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    received_at  INTEGER NOT NULL,
    PRIMARY KEY (connector, packet_id)
);
CREATE INDEX IF NOT EXISTS idx_mqtt_buffer_conn_ts ON mqtt_message_buffer(connector, ts);

-- Singleton, same upsert-by-fixed-id shape as `notice` above. Read
-- FRESH by the poller on every cycle (never cached in the process --
-- see app/checkin.py), which is the whole point: an admin edit here
-- takes effect on the next poll, no restart. points/streak_bonus/
-- streak_bonus_max are baked onto each mc_checkin_award row at award
-- time (unchanged from before this table existed), so editing this
-- row never rewrites a check-in someone already earned.
CREATE TABLE IF NOT EXISTS checkin_config (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    enabled               INTEGER NOT NULL DEFAULT 0,
    points                REAL NOT NULL DEFAULT 25.0,
    streak_bonus          REAL NOT NULL DEFAULT 5.0,
    streak_bonus_max      REAL NOT NULL DEFAULT 25.0,
    poll_interval_seconds INTEGER NOT NULL DEFAULT 30,
    directory_limit       INTEGER NOT NULL DEFAULT 5000,
    directory_refresh_seconds INTEGER NOT NULL DEFAULT 900,
    updated_at            INTEGER NOT NULL DEFAULT 0
);

-- Replaces mc_checkin_seen_message (still above, left in place unused
-- per this codebase's no-drop convention) AND the Meshtastic check-in
-- poller's old reuse of processed_packet, for the same reason: both of
-- those key on the UPSTREAM'S OWN packet/message id alone, so two
-- connector instances numbering from their own independent sequences
-- can produce the identical id and collide -- one connector's real
-- check-in silently looking "already seen" because a DIFFERENT
-- connector happened to hand back that same number first. Keying on
-- (connector, packet_id) instead makes every id's namespace exactly as
-- wide as the feed it actually came from. packet_id is TEXT (not
-- INTEGER, unlike mc_checkin_seen_message) so the same table and the
-- same helper in app/checkin.py can dedupe both protocols' ids without
-- a cast either way.
CREATE TABLE IF NOT EXISTS checkin_seen_message (
    connector  TEXT NOT NULL,
    packet_id  TEXT NOT NULL,
    seen_at    INTEGER NOT NULL,
    PRIMARY KEY (connector, packet_id)
);

-- A MeshCore channel message that fell INSIDE a net's window but whose
-- sender name resolved to no registered player -- see
-- app/checkin.py's _process_mc_message, which is the only writer.
-- Existing to make an otherwise completely silent failure visible to an
-- operator: the identity model cannot be strengthened (the packet
-- genuinely carries no public key, only a free-text display name -- see
-- app/checkin.py's module docstring), so the fix here is not resolving
-- more senders, it is showing that a sender went unresolved at all.
--
-- Scoped to (net_id, net_date), not just net_id or a bare timestamp --
-- a busy MeshCore channel carries constant chatter outside net hours,
-- and logging all of it would bury the signal an operator actually
-- wants under noise nobody attends to. Only a message that already
-- passed net_date_for_net for THIS net is ever recorded here (see
-- _process_mc_message), which is also why this table's net_id/net_date
-- pair matches mc_checkin_award's own key shape rather than being an
-- unscoped sender log.
--
-- Recording a row here is NOT the same as settling the message in
-- checkin_seen_message -- it must never be, and must never become,
-- an alternative way to mark a message seen. See _process_mc_message's
-- own comment (and the 2026-08-19 incident it references) for why an
-- unresolved sender has to stay eligible for a later poll to retry once
-- the directory or a binding catches up.
--
-- One row per (net, net_date, sender), upserted -- message_count and
-- last_seen accumulate across repeat posts from the same unresolved
-- name in the same net, the same way a resolved sender would only earn
-- once no matter how many times they posted (mc_checkin_award's own
-- key), so this stays one line per offender per net rather than growing
-- one row per message.
CREATE TABLE IF NOT EXISTS checkin_unresolved_sender (
    net_id        INTEGER NOT NULL,
    net_date      TEXT NOT NULL,
    sender_name   TEXT NOT NULL,
    first_seen    INTEGER NOT NULL,
    last_seen     INTEGER NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (net_id, net_date, sender_name)
);
CREATE INDEX IF NOT EXISTS idx_unresolved_net_date ON checkin_unresolved_sender(net_date);

-- Last-known MeshCore directory display name per (connector, node) --
-- see app/checkin.py's _build_directory_bridge, the only writer. A
-- check-in is credited by resolving a player's bound radio contact to
-- whatever display name its public key currently resolves to in a
-- connector's node directory (see app/checkin.py's module docstring);
-- if a player renames a node, that resolved name changes and any
-- check-in matched against the old name silently stops being credited
-- -- nothing else in this schema records what a node's resolved name
-- USED to be, so there is otherwise no way to notice a rename ever
-- happened. previous_name/changed_at exist so that moment is visible
-- (app/admin_ops.py's _attention surfaces a recent change) rather than
-- only inferrable after check-ins have already gone quiet.
-- changed_at is NULL until the first change is observed -- the initial
-- insert is not itself a "change."
--
-- Keyed on (connector, node_ref), NOT (connector, player_id): a display
-- name belongs to a specific radio, not to the person holding it. A
-- player with two bound MeshCore contacts has the directory resolving
-- two different names at once -- both correct, one per contact -- and
-- that is normal, not a rename. Keying this table on player_id instead
-- (an earlier version did, table name checkin_player_name) made every
-- poll a race between whichever contact's row got processed last, so
-- the table flip-flopped and _attention logged a false "name changed"
-- roughly every poll for any multi-radio player -- 15 of them on
-- preview alone. player_id is still stored (not derivable from
-- node_ref without the join _attention already needs anyway) so a
-- reader can go straight from a row to whose radio it is.
CREATE TABLE IF NOT EXISTS checkin_node_name (
    connector     TEXT NOT NULL,
    node_ref      TEXT NOT NULL,
    player_id     INTEGER NOT NULL,
    name          TEXT NOT NULL,
    first_seen    INTEGER NOT NULL,
    changed_at    INTEGER,
    previous_name TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (connector, node_ref)
);

-- ---------------------------------------------------------------------
-- Node confirmation (app/checkin_api.py's POST/GET/DELETE
-- /api/checkin/confirm/*): a THIRD way to arrive at a player_node
-- binding, alongside typing an 8-hex node_ref by hand and MeshMapper's
-- wardriving auto-bind (see player_node's own comment above) -- but
-- the only one of the three that PROVES possession rather than merely
-- asserting it. A MeshCore channel message carries no per-sender key
-- (see app/checkin.py's module docstring), so a bare display name --
-- typed into a picker, or (the now-retired approach) registered
-- outright as a fallback identity, see mc_checkin_binding's own
-- comment above -- is only ever as trustworthy as whoever typed it:
-- anyone who knows (or guesses) another player's display name can
-- claim their check-ins. Confirmation closes that gap for players
-- willing to do it: make the SPECIFIC radio advertise during a short
-- window, and bind whichever public key showed a FRESH advert under
-- that name, not merely a name match against a directory that could
-- already be stale by up to checkin_config.directory_refresh_seconds.
--
-- One row per player -- PRIMARY KEY (player_id), not a surrogate id --
-- because a player can only ever be mid-confirmation for one radio at
-- a time; opening a second window (a retry, a different node, a typo
-- fixed) is an upsert that silently replaces whatever window was
-- already open -- "set, not add" semantics, expressed as the primary
-- key here since this table has no other natural key.
--
-- typed_name is stored RAW, exactly as the player typed it -- never
-- normalized at write time -- because normalize_sender_name() is a
-- lossy fold (case only, by design -- see that function's own
-- docstring), and every comparison against it happens at READ time
-- instead, so there is only ever one place in the codebase that
-- decides what "the same name" means.
--
-- baseline is a JSON object, public_key -> last-heard epoch seconds,
-- captured by a fresh ON-DEMAND directory scan (app/checkin.py's
-- confirm_scan_all_connectors -- NEVER CheckinPoller's own 15-minute-
-- cached directory, which could not see a fresh advert inside a
-- 5-minute confirmation window at all) the MOMENT the window opens.
-- A node already broadcasting under this name before the window
-- opened is not proof of anything -- it could have been heard hours
-- ago -- so the baseline exists to let GET /api/checkin/confirm/status
-- tell "was already advertising" apart from "just advertised, right
-- now, because the player is holding the button." A public key absent
-- from this snapshot (the common case -- that node has never posted
-- under this exact name before) needs no stored epoch to compare
-- against at all; one that IS present needs its last-heard time to
-- have moved forward since the snapshot was taken. Stored as JSON
-- rather than a second table (one row per baseline entry) because it
-- is read and written as a single unit, once per window and once per
-- status poll, never queried by public_key on its own -- a join for
-- that would cost more than it would ever save.
--
-- last_scan_at throttles GET /api/checkin/confirm/status to at most
-- one upstream re-scan every few seconds (app/checkin_api.py) rather
-- than one per poll -- a browser polling this endpoint for up to five
-- minutes straight must never turn into a request storm against every
-- configured MeshCore connector. DEFAULT 0 so a freshly opened window
-- (whose baseline scan just happened) is immediately due for its
-- first re-scan rather than waiting out the throttle a second time.
--
-- Brand new table, no existing deployed shape to ALTER, so CREATE
-- TABLE IF NOT EXISTS here is sufficient on its own -- same reasoning
-- as repeater_observation/mc_checkin_award above: there is no
-- existing, already-deployed shape, which is the only reason an entry
-- would need to go in MIGRATIONS instead.
CREATE TABLE IF NOT EXISTS mc_node_confirmation (
    player_id     INTEGER PRIMARY KEY,
    typed_name    TEXT NOT NULL,
    opened_at     INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL,
    baseline      TEXT NOT NULL,
    last_scan_at  INTEGER NOT NULL DEFAULT 0
);

-- Meshtastic's counterpart to mc_node_confirmation above -- same job
-- (prove a player is really holding a specific radio, right now, before
-- binding it), same five-minute window/throttle shape, but a
-- deliberately SIMPLER proof than MeshCore's needs, because the two
-- protocols hand this feature opposite problems:
--
-- MeshCore channel messages carry no per-sender key, only a free-text
-- display name that could already be shared or drifted -- so
-- mc_node_confirmation has to snapshot a BASELINE (every node already
-- posting under the typed name, and when it was last heard) and only
-- trust a public key whose last-heard time moves PAST that baseline
-- during the window (app/checkin.py's _fresh_candidates). A bare name
-- match proves nothing on its own; only a fresh advert during the
-- window does.
--
-- Meshtastic packets carry a real sender node id on every message, and
-- the "name" here is not a persistent on-mesh identity at all -- it is
-- a short code (see app/checkin.py's Meshtastic node-confirmation
-- section) generated fresh, with `secrets`, the instant this window
-- opens, and unique among every other currently-open mt confirmation
-- window. Nothing on the mesh could have posted that exact text before
-- this row existed, so there is no baseline to snapshot and no
-- "already advertising before the window opened" case to guard
-- against the way MeshCore's does -- a message containing the code, by
-- construction, can only have been sent by someone who read the code
-- off THIS window after it opened. That is what lets this table skip
-- mc_node_confirmation's `baseline` column entirely rather than
-- storing an empty/unused one: the code IS the proof, not a comparison
-- against a prior snapshot.
--
-- code is UNIQUE across every row (open or not-yet-cleaned-up expired)
-- so that a fresh advert-style collision between two players' windows
-- can never happen -- app/checkin.py's issue_unique_mt_confirm_code()
-- is what enforces that at generation time, retrying on the vanishingly
-- rare chance of a collision; this column-level UNIQUE constraint is
-- the backstop, not the primary mechanism.
--
-- One row per player -- PRIMARY KEY (player_id), same "set, not add"
-- semantics mc_node_confirmation's own comment explains: opening a
-- second window (a retry, a different radio) silently replaces
-- whatever window was already open. app/checkin_api.py additionally
-- clears the OTHER protocol's table (mc_node_confirmation) whenever a
-- window opens here, and vice versa -- a player has at most one open
-- confirmation window, mc or mt, never both at once, which is what
-- lets GET /api/checkin/confirm/status report a single unambiguous
-- `protocol` for whichever window is open.
--
-- last_scan_at is the same per-player throttle mc_node_confirmation's
-- own column is -- see that table's comment -- reused so a browser
-- polling status for up to five minutes straight can never turn into a
-- request storm against every configured Meshtastic connector either.
CREATE TABLE IF NOT EXISTS mt_node_confirmation (
    player_id     INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    opened_at     INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL,
    last_scan_at  INTEGER NOT NULL DEFAULT 0
);

-- DB-backed fallback for app/checkin.py's CheckinPoller._mc_directory
-- (2026-09-17, the web/worker role split -- app/config.py's
-- run_background_tasks, docker-compose.yml's `meshwars`/
-- `meshwars-worker` services). That in-memory dict is what
-- directory_snapshot() serves from on its fast path, and it is ONLY
-- ever populated by _refresh_mc_directory_if_stale, which only runs
-- inside CheckinPoller.run_forever()'s own loop -- a loop that, after
-- the split, runs in exactly ONE process (the worker). Several HTTP
-- routes call directory_snapshot() from a request handler (the node
-- picker in app/checkin_api.py, app/admin_ops.py, app/account_api.py,
-- and /claimnode in app/discord_interactions.py) -- on a web-role
-- process, whose own in-memory dict is permanently empty, that used to
-- mean an always-empty node picker with real upstream data sitting
-- one process over and unreachable. This table is how the worker
-- publishes what it fetched so a web-role process can read it back.
--
-- One row per connector_url (mirroring _mc_directory's own shape: one
-- dict entry per connector, not a single global blob) -- `nodes` is
-- that connector's directory, JSON-serialized (the same node-dict
-- shape CoreScopeClient/BeaconClient's fetch_directory() already
-- returns, unchanged), so directory_snapshot()'s DB fallback needs no
-- reshaping to match what its in-memory fast path already returns.
-- fetched_at is WALL-CLOCK (int(time.time())), unlike
-- CheckinPoller._mc_directory_fetched_at (time.monotonic(), meaningless
-- outside the process that recorded it) -- a cross-process reader needs
-- a clock that means the same thing in both processes. See
-- directory_snapshot()'s own docstring for why a web-role reader never
-- rejects a row for being stale: an out-of-date picker list beats an
-- empty one, and the worker's own refresh interval is already what
-- bounds how stale a row can get.
--
-- The worker is the ONLY writer, through the ordinary WriteSession
-- discipline every other write in this codebase uses -- see
-- _refresh_mc_directory_if_stale. A web-role process only ever reads
-- this table, never writes it.
CREATE TABLE IF NOT EXISTS mc_directory_cache (
    connector_url TEXT PRIMARY KEY,
    nodes         TEXT NOT NULL,
    fetched_at    INTEGER NOT NULL
);

-- Worker-published /api/mc/board response (app/mc_api.py's board_cache
-- publisher run_forever(), cached_json_response()'s DB fallback). Same
-- worker-writes/web-reads split as mc_directory_cache directly above,
-- for the identical reason: after the web/worker split
-- (docker-compose.yml's `meshwars`/`meshwars-worker` services,
-- settings.run_background_tasks), app/mc_api.py's in-process
-- _BOARD_CACHE is a per-PROCESS dict, so each of the `meshwars` web
-- container's uvicorn workers held its own copy and rebuilt it
-- independently on every settings.board_cache_seconds TTL miss -- up to
-- 3 ~6.8s rebuilds per window, on processes that are also serving user
-- requests (observed: web container at 134-187% CPU, worker at 0.3%).
-- This table is how the worker (the ONLY writer, same WriteSession
-- discipline as mc_directory_cache) publishes the finished payload so a
-- web process's cache miss reads a row instead of rebuilding.
--
-- One row per cache_key -- today just 'mc_board' (/api/mc/board has
-- exactly one cache key: the route takes no query parameters and always
-- builds board_for(MC_PROTOCOL, include_meta=False), so the key space
-- is a single, fixed entry, not something that grows with callers the
-- way mc_directory_cache's connector_url or app/places_api.py's
-- viewport-keyed _PLACES_CACHE do). /get-nodes' own cached_json_response
-- keys ('mt_board_authed'/'mt_board_public') deliberately do NOT get a
-- row here: this table opportunistically serves ANY key
-- cached_json_response asks it for, but nothing publishes those two, so
-- that route is unaffected and keeps rebuilding on its own miss exactly
-- as before -- see cached_json_response's own docstring for why only
-- 'mc_board' was worth precomputing.
--
-- body/gzip_body/etag are the FINISHED artifact -- already-serialized
-- JSON bytes, already-gzip-compressed bytes, and the etag hashed from
-- body -- so a web process reading this row does zero serialization and
-- zero compression, only a lookup. Both representations are stored
-- (unlike _CachedBody's own gzip_body, which the in-process cache fills
-- lazily on first gzip-accepting request) because the whole point here
-- is a web process never doing that compression itself either.
--
-- built_at is wall-clock (int(time.time()), like mc_directory_cache's
-- fetched_at, not app/mc_api.py's own _CachedBody.built_at which is
-- time.monotonic() and meaningless outside the process that set it) but
-- is NOT used to reject a stale row on read: see
-- cached_json_response's own docstring for why an out-of-date board
-- beats a 7-second rebuild on a viewer's request, the same reasoning
-- mc_directory_cache's SCHEMA comment gives for its own fetched_at.
-- Kept anyway for operator visibility (how stale is the live row right
-- now) the same way fetched_at is.
CREATE TABLE IF NOT EXISTS board_cache (
    cache_key TEXT PRIMARY KEY,
    body      BLOB NOT NULL,
    gzip_body BLOB,
    etag      TEXT NOT NULL,
    built_at  INTEGER NOT NULL
);

-- ---------------------------------------------------------------------
-- Monthly results (app/results.py). A six-month season leaves five
-- months with nothing to show, so each calendar month closes with its
-- own standings and honors on the /results page.
--
-- A month is scored ON THE MONTH -- ground taken and points earned
-- between its boundaries -- not as a snapshot of season standings. A
-- snapshot would name the same leader every month and mean nothing;
-- "who gained the most in August" is a fresh contest each time.
--
-- Months are CALENDAR months in settings.checkin_net_timezone, the same
-- local clock net dates already use. Not per-season offsets: the two
-- boards started on different days, and one site should not hold two
-- different opinions about when August ended.
--
-- These tables are a freeze, not the source of truth. Everything in
-- them is derived from mc_tile_capture_log and mc_checkin_award, and
-- the current (unfinished) month is computed live from those same rows
-- rather than read from here. A month is written here once it is over,
-- so a result can never change after the fact -- a later correction to
-- history will not silently rewrite a month somebody already won.
CREATE TABLE IF NOT EXISTS month_result (
    month     TEXT NOT NULL,     -- 'YYYY-MM', local
    protocol  TEXT NOT NULL,     -- 'mc' | 'mt'
    closed_at INTEGER NOT NULL,
    PRIMARY KEY (month, protocol)
);

CREATE TABLE IF NOT EXISTS month_standing (
    month          TEXT NOT NULL,
    protocol       TEXT NOT NULL,
    team           TEXT NOT NULL,
    squares        INTEGER NOT NULL DEFAULT 0,  -- ground HELD at the close; this alone places the team
    checkin_points REAL NOT NULL DEFAULT 0,
    explorer_points REAL NOT NULL DEFAULT 0,  -- shown beside squares, never added to them
    PRIMARY KEY (month, protocol, team)
);

CREATE TABLE IF NOT EXISTS month_award (
    month     TEXT NOT NULL,
    protocol  TEXT NOT NULL,
    award     TEXT NOT NULL,     -- see app/results.py AWARDS
    scope     TEXT NOT NULL DEFAULT '',   -- '' for an overall award, else the team it is scoped to
    player_id INTEGER,           -- null for a team award
    team      TEXT,
    value     REAL NOT NULL,
    detail    TEXT,              -- award-specific, e.g. Frontier's cell and distance
    PRIMARY KEY (month, protocol, award, scope)
);

-- ---------------------------------------------------------------------
-- Keys for the public read API (app/public_api.py). Separate from
-- api_key above on purpose: that one belongs to a PLAYER and authorises
-- writing their own wardriving data. This one belongs to an
-- INTEGRATION -- a bot, a dashboard -- authorises reading only, and is
-- issued by the operator rather than earned by joining. Sharing one
-- table would mean a read key could post pings.
--
-- Only the hash is stored, same as api_key, so a key cannot be read
-- back out. Losing one means issuing another.
CREATE TABLE IF NOT EXISTS api_client (
    key_hash     TEXT PRIMARY KEY,
    label        TEXT NOT NULL,      -- what it is for, e.g. "freq51 discord bot"
    created_at   INTEGER NOT NULL,
    revoked_at   INTEGER,
    last_seen_at INTEGER,
    -- Authentications, NOT requests. app/public_api.py caches a key
    -- lookup for a minute, so a client polling every second bumps this
    -- once. It is a coarse "has this been used much" signal and nothing
    -- finer; last_seen_at is the number an operator should actually
    -- read, and that one is accurate to within the same minute.
    request_count INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------
-- "Places Worth Going" -- see docs/features/places.md for the design
-- and scripts/build_places_seed.py for how this table gets its data.
-- Seed only in this migration: no scoring/activation table yet, and
-- nothing reads this one back out until that lands.
-- ---------------------------------------------------------------------

-- One row per named destination: a SOTA summit, a POTA park, or an
-- OpenStreetMap landmark off the narrowed tag list. ref_code is the
-- source's own identifier (SOTA summit code, POTA reference, or an
-- "n<id>"/"w<id>" OSM object reference) -- stable, so re-running the
-- seed script updates a place in place rather than duplicating it.
--
-- area_m2 and geom are NULL for a summit or a landmark (those score
-- the single square their point falls in -- see the design note) and
-- for a park POTA-to-PAD-US matching could not find a boundary for.
-- Where a park DOES have a matched boundary, area_m2 is PAD-US's own
-- whole-unit acreage (converted to m^2) and geom is that boundary as
-- WKT, clipped to a radius around the park's centre point and
-- simplified for storage -- see build_places_seed.py's match_parks()
-- for exactly how much, and why a park's boundary is stored as WKT
-- text rather than a second geometry table: one flat row per place is
-- enough for the scoring stage to test "is this square more than half
-- inside geom" later without a join.
--
-- Brand new table, no existing deployed shape to ALTER, so CREATE
-- TABLE IF NOT EXISTS here is sufficient on its own -- same reasoning
-- as player_cell_repeater_credit above; no MIGRATIONS entry needed for
-- the table itself (see MIGRATIONS below for `rotates`, added after
-- this table's first landing -- a DB that already ran that first
-- migration needs an ALTER, so CREATE TABLE IF NOT EXISTS alone is not
-- enough for `rotates` the way it is for the table as a whole).
--
-- rotates: 1 if this place is in the weekly rotation draw (landmarks,
-- and parks smaller than one grid cell), 0 if it is always active
-- (summits, and parks at or above one grid cell -- including a park
-- PAD-US matched no boundary for, which stays permanent rather than
-- rotating for a data gap; see app/place_rotation.py). Set at load
-- time by app/places_seed.py, never by the rotation engine itself --
-- rotation only ever chooses AMONG rotates=1 rows, never flips the
-- flag.
CREATE TABLE IF NOT EXISTS place (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_type    TEXT NOT NULL CHECK (ref_type IN ('summit', 'park', 'landmark')),
    ref_code    TEXT NOT NULL,      -- source's own id: SOTA code, POTA reference, or n<id>/w<id> from OSM
    name        TEXT NOT NULL,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    -- Effort-scored, not flat by ref_type (changed 2026-08-25 -- see
    -- points_reason just below): 5 inside a Census place's limits for
    -- any ref_type, else 10 (landmark) / 25 (park) / 50-100 (summit,
    -- scaled by elevation_ft below -- changed 2026-08-25, see that
    -- column's own comment). Computed once at seed-build time by
    -- scripts/build_places_seed.py's score_points() against
    -- app/reference/places.csv's town anchors; this column is still
    -- the only thing app/place_scoring.py and the rotation draw read
    -- to award or rank a place.
    points      INTEGER NOT NULL,
    source      TEXT NOT NULL,      -- 'SOTA' | 'POTA' | 'POTA/PAD-US' | 'OSM'
    area_m2     REAL,               -- park only, and only when PAD-US matched a boundary
    geom        TEXT,               -- park only: matched boundary as WKT, NULL otherwise
    -- summit only, NULL for park/landmark. SOTA's own AltFt, carried
    -- through the seed CSV (scripts/build_places_seed.py's fetch_sota())
    -- purely so a remote summit's score can be derived from it
    -- (score_points()'s elevation scaling, 50 at
    -- SUMMIT_ELEV_FLOOR_FT up to 100 at SUMMIT_ELEV_CEIL_FT) and so an
    -- operator can see WHY a summit scored what it did without
    -- re-deriving it -- same purpose points_reason already serves,
    -- just numeric rather than a category. Added 2026-08-25 alongside
    -- that scaling; `place` had no elevation before this, since the
    -- old flat-100 model never needed one.
    elevation_ft REAL,
    rotates     INTEGER NOT NULL DEFAULT 0,  -- 1 = weekly rotation candidate, 0 = always active
    -- WHY `points` got the value it did: 'in_city' (inside a Census
    -- place's effective_radius_m, worth 5 regardless of ref_type),
    -- 'remote' (park/landmark outside every anchor, worth the
    -- ref_type's flat value), or 'remote_scaled' (summit outside every
    -- anchor, worth its elevation_ft-scaled value -- see that column's
    -- comment above).
    -- Written by app/places_seed.py's loader from the seed CSV's own
    -- points_reason column; nothing at runtime branches on it -- it
    -- exists purely so the admin panel and any future re-tuning can see
    -- WHY a place scores what it does without re-deriving it. Nullable
    -- because a DB migrated from before this column existed has no
    -- value to backfill for an already-loaded row; the next places_seed
    -- reconcile pass (which re-upserts every row, not just new ones)
    -- fills it in within one load.
    points_reason TEXT,
    -- 1 = currently in the seed CSV, 0 = pruned from a later seed
    -- rebuild. Never deleted: place_activation (and, once resolved,
    -- place_week) FK/reference place.id, and a player who legitimately
    -- scored a reference that later left the seed keeps those points --
    -- their Explorer score and any frozen month must not change because
    -- the seed got re-tuned. An inactive place just stops being drawn
    -- and stops being scoreable going forward (every read path that
    -- draws or scores a place filters WHERE active = 1); its row and
    -- name survive purely so old place_activation rows still resolve.
    -- Set by app/places_seed.py's reconcile pass at load time, never by
    -- any other code path.
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  INTEGER NOT NULL,
    UNIQUE (ref_type, ref_code)
);
CREATE INDEX IF NOT EXISTS idx_place_latlon ON place(lat, lon);
-- idx_place_active is NOT created here: on a DB that already ran the
-- CREATE TABLE above (before `active` existed), this executescript()
-- runs before MIGRATIONS' ALTER TABLE below adds the column, so an
-- index on it here would fail startup on every existing deployment
-- with "no such column: active". Created by MIGRATIONS instead, after
-- the ALTER that guarantees the column exists first.

-- Which grid cell(s) (app/grid.py cell_id) a place scores when painted.
-- One row for a summit, a landmark, or a park too small to need the
-- boundary test (the cell containing its point) -- several rows for a
-- park whose boundary is bigger than one cell, one row per cell that
-- is more than half inside that boundary (see app/places_seed.py's
-- _park_cells()). This is the pre-computed answer to "does painting
-- this cell activate this place", computed once at load time so the
-- scoring path (app/place_scoring.py) is a single indexed lookup on
-- cell_id rather than a geometry test on every accepted ping.
CREATE TABLE IF NOT EXISTS place_cell (
    place_id    INTEGER NOT NULL,
    cell_id     TEXT NOT NULL,
    PRIMARY KEY (place_id, cell_id),
    FOREIGN KEY (place_id) REFERENCES place(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_place_cell_cell ON place_cell(cell_id);

-- One row per (place, player, week) credit actually awarded -- the
-- UNIQUE constraint IS the "one credit per reference per person per
-- week" rule; app/place_scoring.py relies on a duplicate insert
-- failing rather than checking existence twice. `points` is copied
-- onto the row at award time (same reasoning as mc_checkin_award.points
-- in app/checkin.py) so a later change to a place's point value, or to
-- the weekly cap, never rewrites what someone already earned.
-- week_start is the Wednesday date (YYYY-MM-DD, America/Boise -- see
-- app/place_rotation.week_start_for_ts) the credit belongs to, the
-- same clock app/checkin.py's net_date already uses.
CREATE TABLE IF NOT EXISTS place_activation (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    place_id    INTEGER NOT NULL,
    player_id   INTEGER NOT NULL,
    week_start  TEXT NOT NULL,
    points      INTEGER NOT NULL,
    awarded_at  INTEGER NOT NULL,
    -- Which board earned it, 'mc' or 'mt'. The place honors (Tourist,
    -- Park Hopper, Peak Tagger) and the standings' exploration column
    -- filter on it, so a trip made on one board stops being credited on
    -- the other -- it used to show the same winner on both.
    -- Deliberately NOT part of the UNIQUE below: a place is still one
    -- credit per player per week across both boards, and the weekly cap
    -- is still shared, because those are limits on the person and not on
    -- the radio they were carrying.
    protocol    TEXT NOT NULL DEFAULT '',
    UNIQUE (place_id, player_id, week_start)
);
CREATE INDEX IF NOT EXISTS idx_place_activation_week_player ON place_activation(week_start, player_id);
CREATE INDEX IF NOT EXISTS idx_place_activation_place ON place_activation(place_id);

-- The resolved weekly rotation draw, one row per (week, chosen place).
-- A draw is deterministic (app/place_rotation.py seeds its RNG from
-- week_start alone) so it COULD be recomputed on every read instead of
-- stored -- this table exists so it is computed exactly once and then
-- stable, the same reason mc_tile_capture_log exists alongside the
-- decayed-score model: re-deriving the same answer from the same seed
-- twice is wasted work, not a correctness requirement, but a stored
-- row also means a place already shown to a player this week can never
-- retroactively change because e.g. a new place was added to the seed
-- mid-week. Only rotates=1 places ever appear here -- summits and
-- large parks are always active and never need a row.
CREATE TABLE IF NOT EXISTS place_week (
    week_start  TEXT NOT NULL,
    place_id    INTEGER NOT NULL,
    PRIMARY KEY (week_start, place_id)
);

-- ---------------------------------------------------------------------
-- The one-time update notice: operator-authored, shown to players once
-- per version_key, edited from the admin panel's Notice section (see
-- app/admin_ops.py's admin_notice/admin_notice_save and
-- frontend/admin.js). A player sees it on first map load and never
-- again once dismissed, UNLESS version_key changes -- the dismissal
-- itself lives in the player's own browser (localStorage, keyed on
-- version_key -- see frontend/map2.js), never in this table, so this
-- row only has to say what the CURRENT notice is, not who has seen it.
--
-- Singleton row (id fixed to 1 by the CHECK), upserted the same way
-- app/db.py's own set_cursor() upserts the `cursor` table -- there is
-- only ever one current notice, not a history of past ones. The repo's
-- CHANGELOG.md is where release history actually lives; re-publishing
-- here overwrites what was here before on purpose. Toggling `active`
-- off retires the notice (nothing renders for players) without losing
-- the drafted title/body/version_key, so turning it back on later does
-- not mean retyping it.
CREATE TABLE IF NOT EXISTS notice (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    version_key TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------
-- Account layer (app/sessions.py, app/account_api.py): a login identity
-- sitting ABOVE the existing hashed-API-key player model, not replacing
-- it. Every table above this comment (player, api_key, player_node,
-- ...) is completely unmodified by this -- a player who only ever
-- registers a MeshMapper key and never creates an account behaves
-- exactly as they always have, in every respect. An account is a new,
-- optional handle a person can additionally acquire: it can sign in
-- through more than one identity (Google, GitHub, Discord, Apple,
-- email -- whichever providers actually ship; nothing in this schema
-- or these tables builds an OAuth provider or sends email, that is
-- separate follow-up work), and it can be linked to AT MOST ONE
-- existing player, one-to-one, nullable in both directions -- see
-- player.account_id's own MIGRATIONS entry below for the column that
-- carries that link (an account has no equivalent column pointing back
-- at a player; the FK lives on the "many candidate rows, one true
-- owner" side the same way api_key.player_id already does, and the
-- UNIQUE index on player.account_id is what actually enforces the
-- one-to-one half of the contract).
--
-- Deliberately NOT a replacement identity model, and deliberately NOT
-- auto-merging: an account_identity row is never folded into an
-- existing account just because it happens to share an email address
-- with one -- see that table's own comment for why silent merging
-- would be an account-takeover surface, not a convenience. A future
-- merge TOOL (an operator, or a person proving they control two
-- accounts) is the only way two accounts ever combine, and nothing in
-- this migration builds that tool -- see `account.merged_into` below.
-- ---------------------------------------------------------------------

-- One row per person who has ever created an account. created_at/
-- last_login_at/disabled_at mirror `player`'s own columns above for
-- the same reasons: an operator needs to know when an account was
-- created and last used, and disabling one (spam, abuse, a support
-- request) must not delete anything an audit trail or a later merge
-- still needs to read.
--
-- merged_into: nullable self-reference for a LATER merge tool (two
-- accounts one person somehow created independently -- e.g. signing in
-- with Google once and GitHub another time before ever linking them --
-- discovered to be the same person after the fact). Added now,
-- alongside the rest of this table, specifically so that future tool
-- never needs its own ALTER TABLE migration -- same reasoning
-- `place.rotates`/`active` show what happens when a column like this
-- ISN'T added up front (two extra migrations, each with its own
-- backfill story). Nothing reads or writes this column yet: every
-- account_* query anywhere in this codebase today filters on
-- account_id alone and has no merge concept to account for.
CREATE TABLE IF NOT EXISTS account (
    account_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     INTEGER NOT NULL,
    last_login_at  INTEGER,
    disabled_at    INTEGER,
    merged_into    INTEGER
);

-- One row per (provider, subject) sign-in identity a person can use to
-- reach an account -- provider's own opaque, stable subject id ("sub"
-- in OAuth/OIDC terms; for provider='email' the subject is the address
-- itself, lowercased). PRIMARY KEY on the (provider, subject) pair, not
-- account_id, because that pair IS exactly what a provider's login
-- callback hands back: resolving a callback to an account is a single
-- indexed lookup on the two fields the provider gave us, with no
-- secondary index needed.
--
-- account_id is NOT unique here on purpose -- one account can hold
-- SEVERAL identities (a person who first signed in with Google, later
-- added GitHub, without ever losing access through either) -- see
-- idx_account_identity_account below for the account -> identities
-- direction app/account_api.py's GET /api/account reads.
--
-- email/email_verified travel with the IDENTITY, not with the account:
-- two identities on the same account can legitimately carry two
-- different provider-reported addresses (a work GitHub email, a
-- personal Google one), and a provider's own verified-or-not claim is
-- a fact about that provider's assertion, not a fact about the account
-- as a whole. Both nullable -- not every provider this app might add
-- necessarily returns an email at all.
--
-- Never auto-merged: two DIFFERENT (provider, subject) rows that
-- happen to report the same email are never collapsed into one account
-- just because the addresses match. An email address is exactly the
-- kind of thing a provider lets its own user set to whatever they
-- like, so treating email equality as identity equality would let
-- someone sign in as "the same person" as an account they do not
-- actually control -- a real account-takeover path, not a convenience
-- worth the risk. Every distinct (provider, subject) either binds to
-- an account explicitly (a logged-in user linking a second provider
-- themselves) or creates a brand new account -- there is no automatic
-- path between two rows that merely share an email.
CREATE TABLE IF NOT EXISTS account_identity (
    provider        TEXT NOT NULL,       -- 'google' | 'github' | 'discord' | 'apple' | 'email'
    subject         TEXT NOT NULL,       -- provider's own stable id ("sub"); the address itself for 'email'
    account_id      INTEGER NOT NULL,
    email           TEXT,
    email_verified  INTEGER NOT NULL DEFAULT 0,
    linked_at       INTEGER NOT NULL,
    last_login_at   INTEGER,
    PRIMARY KEY (provider, subject)
);
CREATE INDEX IF NOT EXISTS idx_account_identity_account ON account_identity(account_id);

-- One row per login session. token_hash is a
-- SHA-256 digest of the actual session token (app/sessions.py reuses
-- app/mc_ingest.py's hash_secret() for this -- see that module's own
-- comment for why this app deliberately never grows a second hasher
-- for the same job). The raw token itself is never stored anywhere,
-- mirroring api_key.key_hash above: a stolen database backup must
-- never be enough to impersonate a logged-in session, only to know one
-- existed and when.
--
-- expires_at/last_seen_at together implement SLIDING expiry
-- (app/sessions.py's touch_session()): a session's effective lifetime
-- is measured forward from last_seen_at, not frozen at created_at, so
-- someone actively using the site is never logged out mid-session, but
-- a session nobody has touched in a long time still expires on
-- schedule rather than living forever. last_seen_at is deliberately
-- NOT bumped on every single request -- see touch_session()'s own
-- comment for why (SQLite write-lock contention with the check-in
-- poller's own periodic writes, the same WriteSession lock every write
-- in this codebase now serializes through).
--
-- revoked_at: set by an explicit logout (one session) or logout-all
-- (every session on the account) -- checked ahead of expires_at on
-- every verify, so a revoked-but-not-yet-expired token stops working
-- on the very next request, rather than waiting out its natural
-- sliding expiry. Neither revoked_at nor a passed expires_at leaves
-- the row sitting here forever, either -- app/sessions.py's
-- create_session() sweeps rows that are expired or revoked, past a
-- grace period, in the same transaction as every new login (see that
-- module's _sweep_stale_sessions() for the predicate and why login is
-- the trigger, not a timer).
--
-- device_label is NOT a security control -- nothing here pins a
-- session to it, and nothing rejects a request whose device changed.
-- It exists purely so app/account_api.py's GET /api/account can show
-- a person a recognisable list of their own active sessions ("Chrome
-- on Windows, last seen 3 minutes ago") so they can tell which ones
-- are actually theirs before deciding to revoke one.
--
-- This table used to also carry `ip`, the raw client IP address, and
-- `user_agent`, the full raw User-Agent header -- both stored
-- indefinitely, with no sweep anywhere in this codebase (unlike, say,
-- account_pending_identity's expires_at-driven cleanup below). Matt's
-- privacy-hardening call: an IP address is a real-world tracking
-- identifier with no feature depending on it (see app/sessions.py's
-- create_session() -- nothing here ever pinned a session to an
-- address, and app/client_ip.py's get_client_ip() already serves every
-- actual need for a request's address, in-memory, for rate limiting,
-- entirely separately from this table), so it is not stored at all --
-- not truncated, not hashed, not geolocated. A raw User-Agent string is
-- a fingerprint (exact browser/engine/OS build numbers narrow a device
-- down to a small set of people); app/device_label.py reduces it to a
-- short "<Browser> on <OS>" label instead, which is all the Sessions
-- panel's own purpose (recognise-your-own-session, revoke-a-stranger's)
-- ever needed. See db.py's _migrate_session_privacy() below for how
-- existing rows -- not just future ones -- were cleaned when this
-- landed: an already-stored IP address does not get to linger just
-- because it predates the column's removal.
CREATE TABLE IF NOT EXISTS account_session (
    token_hash    TEXT PRIMARY KEY,
    account_id    INTEGER NOT NULL,
    created_at    INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL,
    last_seen_at  INTEGER NOT NULL,
    revoked_at    INTEGER,
    device_label  TEXT
);
CREATE INDEX IF NOT EXISTS idx_account_session_account ON account_session(account_id);

-- Append-only audit trail for account-affecting events -- the same
-- role `player_team_change` plays for `player.team` above (read that
-- table's own comment first; this mirrors it deliberately, right down
-- to "only ever gains a row; nothing here is ever updated or
-- deleted"). Never read by any scoring or authentication path -- it
-- exists purely so an operator (and, eventually, the account holder's
-- own history view) can see WHAT happened and WHEN, which a row's
-- current live state alone can never answer on its own (a player who
-- was linked and later unlinked leaves no trace anywhere else).
--
-- detail is free-text, not a foreign key to whatever the event
-- happened to -- `kind` alone (identity_linked/identity_unlinked/
-- player_linked/player_unlinked/key_rotated/password_set/
-- password_removed/contact_email_set) already says which OTHER table
-- changed, and forcing every future kind of event through the same
-- fixed set of nullable foreign-key columns would mean adding a new
-- column to this table every time a new kind of account event needs
-- describing. A plain text note (built by whichever code path writes
-- the row) is enough for an audit trail nothing downstream parses back
-- out.
--
-- key_rotated (app/account_api.py's POST /api/account/rotate-key):
-- the player-facing twin of admin_api.py's own reissue -- a player
-- revoking every key they hold and minting one fresh one, without an
-- operator. password_set/password_removed
-- (app/account_api.py's POST/DELETE /api/account/password): the fifth
-- sign-in door being created, changed, or removed -- never carries the
-- password itself in `detail`, only that it changed. contact_email_set
-- (app/account_api.py's POST /api/account/contact-email): the
-- contact-only address being set/changed -- see account.contact_email's
-- own MIGRATIONS comment for why this can never become a sign-in
-- identity.
CREATE TABLE IF NOT EXISTS account_link_event (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  INTEGER NOT NULL,
    kind        TEXT NOT NULL,     -- 'identity_linked' | 'identity_unlinked' | 'player_linked' | 'player_unlinked' | 'key_rotated' | 'password_set' | 'password_removed' | 'contact_email_set'
    detail      TEXT,
    actor       TEXT NOT NULL,     -- 'user' | 'operator'
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_link_event_account ON account_link_event(account_id, created_at);

-- Audit trail for the role-gated admin/operator surface
-- (app/admin_api.py, app/admin_ops.py) -- who did WHAT, to WHAT, when.
-- Added alongside account.role (see that column's own MIGRATIONS
-- comment below) replacing the old shared X-Admin-Token door: every
-- action through that door was anonymous by construction (a header
-- either matched the one configured secret or it didn't -- there was no
-- "which admin" to record), so moving to per-account roles is worthless
-- on its own unless the actions themselves start carrying an identity
-- too. Every mutating route under both admin routers writes exactly one
-- row here (app/admin_api.py's _log_admin_action()) right alongside its
-- own write, on success.
--
-- Same append-only, never-updated-or-deleted shape player_team_change
-- and account_link_event above already establish for this codebase's
-- other audit trails -- read either of those tables' own comments
-- first, this one only differs in whose id it carries:
-- actor_account_id is the SIGNED-IN account that performed the action,
-- resolved from the session by _role_guard() and never from anything a
-- request body supplies, where account_link_event's own `actor` column
-- is just the fixed string 'operator' with no way to say which one.
--
-- `action` names the operation ('player_delete', 'revoke_key',
-- 'role_granted', 'role_revoked', 'operator_claimed', ...) -- see each
-- route's own call to _log_admin_action() for the exact vocabulary in
-- use; nothing enumerates or constrains the value at the database
-- level, the same "free text, not a fixed set" latitude checkin_net's
-- `kind` column was given before it hardened into _NET_KINDS. `detail`
-- is free text too, same "not a foreign key, built by whichever code
-- path writes the row" reasoning account_link_event's own detail column
-- gives -- nothing downstream ever parses it back out, it exists so a
-- person reading this table can tell what happened without cross-
-- referencing five other tables by hand.
CREATE TABLE IF NOT EXISTS admin_action_log (
    log_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_account_id  INTEGER NOT NULL,
    action            TEXT NOT NULL,
    detail            TEXT,
    created_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_action_log_actor ON admin_action_log(actor_account_id, created_at);

-- OAuth provider sign-in (app/oauth.py, app/oauth_api.py): a brand-new
-- provider identity that does not yet belong to any account, waiting
-- for a person to choose what happens to it. This is case 4 of
-- app/oauth_api.py's callback decision tree -- reached only when the
-- identity is not already linked (case 1), the caller is not already
-- logged in (case 2), and there is no unambiguous verified-email match
-- to an existing account to auto-link onto (case 3). Rather than
-- create an account speculatively and hope nobody minds, the callback
-- parks the identity here and hands the caller a token to redeem
-- EITHER through POST /api/account/pending/create (make a new account)
-- OR by signing in through an existing method first and returning with
-- this same token (app/oauth_api.py's callback then takes case 2:
-- link onto whichever account that sign-in resolves to).
--
-- Same hashed-single-use-ticket shape as join_token above (read that
-- table's own comment for the reasoning this mirrors deliberately):
-- token_hash is the only thing ever stored, the raw token exists only
-- in the single redirect response that hands it to the browser,
-- consumed_at makes redemption idempotently single-use rather than
-- deleting the row (an audit trail of "this identity was offered,
-- and was/wasn't ever claimed" survives either way), and expires_at
-- (settings.account_pending_identity_lifetime_seconds, 15 minutes by
-- default) bounds how long an abandoned choice screen leaves a
-- redeemable ticket lying around.
--
-- provider/subject/email/email_verified are exactly the
-- ProviderIdentity app/oauth.py's fetch_identity() produced for this
-- callback -- copied here verbatim (not re-fetched from the provider
-- at redemption time) so that redeeming the token later never has to
-- re-authenticate against the provider or trust a second round of
-- provider claims; whatever was verified at callback time is what gets
-- written to account_identity at redemption time, unchanged.
CREATE TABLE IF NOT EXISTS account_pending_identity (
    token_hash      TEXT PRIMARY KEY,
    provider        TEXT NOT NULL,
    subject         TEXT NOT NULL,
    email           TEXT,
    email_verified  INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    consumed_at     INTEGER
);

-- Hashed single-use magic-link token for passwordless email sign-in
-- (app/oauth_api.py's POST /auth/email/start and GET
-- /auth/email/callback) -- the exact same hashed-single-use-ticket
-- shape as join_token above and account_pending_identity just above
-- this comment: token_hash (SHA-256, app/mc_ingest.py's hash_secret())
-- is the only thing ever stored, the raw token exists only in the one
-- link mailed to the address that requested it, consumed_at makes
-- redemption idempotently single-use rather than deleting the row, and
-- expires_at (settings.email_login_token_lifetime_seconds, 15 minutes
-- by default) bounds how long an unopened link stays valid.
--
-- email is the normalized (lowercased, trimmed) address the link was
-- sent to -- GET /auth/email/callback feeds it straight into the exact
-- same callback decision tree every OAuth provider already uses
-- (resolve_oauth_callback() in app/oauth_api.py), as
-- provider='email' / subject=email / email=email / email_verified=1:
-- clicking a link mailed to that address IS this app's proof of
-- ownership for it, the same role a provider's own consent screen
-- plays for GitHub/Google/etc -- see account_identity's own comment
-- above on why email_verified gates auto-linking.
--
-- No index beyond the token_hash primary key -- like
-- account_pending_identity above, every read of this table is a point
-- lookup by token_hash (the callback redeeming its own link); nothing
-- in this codebase ever looks a row up by email. Rows are opportunistically
-- swept (deleted once expired/consumed past a grace period) by
-- app/oauth_api.py's _sweep_stale_rows(), run inline whenever a fresh
-- row is written to this table or to account_pending_identity -- see
-- that function's own comment for why no cron/scheduled job is needed.
CREATE TABLE IF NOT EXISTS email_login_token (
    token_hash   TEXT PRIMARY KEY,
    email        TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    consumed_at  INTEGER
);

-- A fifth sign-in door: one row per account that has set a password,
-- app/password_login.py's own module docstring explains why this is
-- hashlib.scrypt (stdlib, memory-hard) and NOT app/mc_ingest.py's
-- hash_secret() (a bare, fast SHA-256 -- correct for a long random
-- token like api_key.key_hash/account_session.token_hash above, badly
-- wrong for a human-chosen password an offline attacker could
-- otherwise brute-force at SHA-256 speed). account_id is the PRIMARY
-- KEY, not a surrogate id: exactly one password per account, the same
-- "one row, keyed on the thing it belongs to" shape freqmapper_config's
-- own singleton row uses, just per-account instead of global.
--
-- n/r/p/dklen travel WITH the hash, not as a global constant, so a
-- future change to app/password_login.py's own parameters (raising the
-- cost as hardware gets faster, the standard scrypt-hardening story)
-- never invalidates a password set under the old parameters -- verify
-- reads whatever this row itself recorded, and only a future re-hash
-- (naturally, next time this account signs in and the parameters are
-- bumped, or a dedicated migration) ever changes what is stored here.
-- salt/hash are both stored hex-encoded (TEXT), the same encoding
-- app/mc_ingest.py's hash_secret() already uses for key_hash/
-- token_hash, so every credential digest in this database has one
-- consistent on-disk representation.
CREATE TABLE IF NOT EXISTS account_password (
    account_id   INTEGER PRIMARY KEY,
    salt         TEXT NOT NULL,
    n            INTEGER NOT NULL,
    r            INTEGER NOT NULL,
    p            INTEGER NOT NULL,
    dklen        INTEGER NOT NULL,
    hash         TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);

-- Hashed single-use verification token for account.contact_email (see
-- that column's own MIGRATIONS comment below for what contact_email
-- IS and, just as important, what it is deliberately NOT). Same
-- hashed-single-use-ticket shape as account_pending_identity/
-- email_login_token above -- token_hash only, raw token exists only in
-- the one mailed link, consumed_at makes redemption idempotent,
-- expires_at bounds how long an unopened link stays valid -- but this
-- is its OWN table, not a reuse of email_login_token, on purpose: a
-- row in email_login_token is fed straight into
-- resolve_oauth_callback() as a login-capable, provider='email'
-- identity the instant it is redeemed (see that table's own comment).
-- A contact-email verification token must NEVER be capable of that --
-- see account.contact_email's own comment below -- so giving it a
-- shape that happens to be identical but a table that is NEVER read by
-- that decision tree is the whole point, not an accident of copy-paste.
--
-- email is the normalized address this token was issued for, captured
-- at send time -- if the account's contact_email is changed again
-- before this link is clicked, app/oauth_api.py's verify route checks
-- the token's own email still matches the account's CURRENT
-- contact_email before marking it verified, so an abandoned link for
-- an old address can never verify whatever address happens to be set
-- later.
CREATE TABLE IF NOT EXISTS account_contact_email_token (
    token_hash   TEXT PRIMARY KEY,
    account_id   INTEGER NOT NULL,
    email        TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    consumed_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_account_contact_email_token_account ON account_contact_email_token(account_id);

-- TOTP two-factor authentication (app/totp.py, app/totp_api.py) -- one
-- row per account that has ever started enrollment. account_id is the
-- PRIMARY KEY, same "one row, keyed on the thing it belongs to" shape
-- account_password above uses: at most one TOTP secret per account,
-- ever (a fresh enrollment attempt overwrites a still-PENDING row --
-- see activated_at below -- rather than accumulating a second one).
--
-- secret_encrypted is a cryptography.fernet.Fernet token (base64 TEXT)
-- that decrypts to the raw 160-bit secret -- never the raw secret
-- itself. Unlike account_password's hash or account_session's
-- token_hash, this genuinely cannot be a one-way hash: verifying a
-- submitted code requires recomputing HOTP from the ORIGINAL secret,
-- not comparing to a digest of it. See app/totp.py's own "secret at
-- rest" docstring section for the full reasoning on why this is
-- encrypted with a key held in settings.account_totp_encryption_key
-- (the ENVIRONMENT, never this database) rather than stored plain, and
-- why enrollment refuses to even start when that key is unset.
--
-- activated_at is NULL from the moment enrollment begins (a secret was
-- generated and the QR/text shown) until a person proves their
-- authenticator app actually works by submitting one valid code
-- (app/totp_api.py's POST /api/account/totp/activate) -- the row
-- exists but does NOT yet guard sign-in while NULL: this app never
-- lets an unproven secret become the thing standing between someone
-- and their own account. A pending (activated_at IS NULL) row is
-- silently replaced by the next enrollment attempt, not treated as a
-- conflict -- see that route's own docstring.
--
-- last_used_step is this app's replay guard (app/totp.py's own
-- "replayed codes" docstring section): the counter step (unix_time /
-- 30) the most recently ACCEPTED code was valid at, updated on every
-- successful verification anywhere a code is checked (activation,
-- sign-in, disable) -- app/totp_api.py's verify_and_consume_totp_code()
-- refuses to accept any candidate step at or before this value, which
-- rejects an exact replay of the same code AND a stale code from an
-- earlier step, while still accepting the normal forward march of time
-- through the skew window. NULL until the first ever successful
-- verification.
CREATE TABLE IF NOT EXISTS account_totp (
    account_id        INTEGER PRIMARY KEY,
    secret_encrypted   TEXT NOT NULL,
    created_at         INTEGER NOT NULL,
    activated_at       INTEGER,
    last_used_step     INTEGER
);

-- Ten (settings.account_totp_recovery_code_count) single-use recovery
-- codes minted the moment account_totp.activated_at is first set
-- (app/totp_api.py's POST /api/account/totp/activate) -- see that
-- table's own comment for why account_totp cannot itself carry these
-- (a fresh enrollment attempt replaces the SECRET row before it is
-- proven, but recovery codes are only ever minted once activation
-- actually succeeds, and a person may hold up to
-- account_totp_recovery_code_count of them at once, not one).
--
-- code_hash is app/mc_ingest.py's hash_secret() (a bare SHA-256
-- digest) -- the house convention for a random, server-generated,
-- compared-not-brute-forced secret, the SAME reasoning
-- account_session.token_hash/api_key.key_hash already follow and
-- explicitly NOT app/password_login.py's scrypt (see that module's own
-- docstring on exactly why a human-chosen password needs a slow,
-- memory-hard hash and a 50-bit-entropy, machine-generated code does
-- not: an offline attacker gains nothing from SHA-256 being fast here,
-- because there is nothing short of the full keyspace worth searching).
--
-- used_at marks a code single-use (set the instant it is consumed,
-- never deleted -- same "audit trail survives redemption" reasoning
-- account_pending_identity's own comment gives for consumed_at) --
-- app/totp_api.py's disable/re-enrollment paths DELETE every row for
-- an account outright instead (turning TOTP off makes every one of
-- them moot at once, so there is nothing worth keeping them around
-- for).
CREATE TABLE IF NOT EXISTS account_totp_recovery_code (
    code_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL,
    code_hash    TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    used_at      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_account_totp_recovery_code_account ON account_totp_recovery_code(account_id);

-- The intermediate "credential verified, second factor not yet
-- supplied" state app/totp_api.py's module docstring calls for --
-- carries a single account_id from a successful password or
-- magic-link sign-in (app/oauth_api.py's password_start()/
-- email_callback()) across to POST /auth/totp/verify, which is the
-- ONLY thing that may ever turn a row here into a real session. Same
-- hashed-single-use-ticket shape as account_pending_identity/
-- email_login_token/account_contact_email_token above (token_hash
-- only -- the raw token exists only in the one HttpOnly cookie or
-- JSON field handed to the caller that just proved a first factor;
-- consumed_at makes redemption idempotently single-use; expires_at
-- bounds how long an abandoned second-factor prompt stays live --
-- settings.account_totp_challenge_lifetime_seconds, 5 minutes by
-- default, deliberately shorter than every other ticket in this
-- table's family since there is nothing to decide here, only to type
-- in).
--
-- CRITICAL: a row here is NEVER, by itself, sufficient to authenticate
-- as account_id -- it proves only that a FIRST factor already
-- succeeded, which is exactly why this is its own table and never
-- reuses account_session's own shape (see app/totp_api.py's module
-- docstring for the full "why not just issue a session already" case
-- this guards against: a session cookie IS a credential the instant it
-- exists, and this must not be).
CREATE TABLE IF NOT EXISTS account_totp_challenge (
    token_hash   TEXT PRIMARY KEY,
    account_id   INTEGER NOT NULL,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    consumed_at  INTEGER
);

-- ---------------------------------------------------------------------
-- Server-side traffic analytics (app/traffic.py): page views, unique
-- visitors, and new-visitor counts for the admin panel's traffic tab,
-- counted from inside this app itself rather than a third-party
-- analytics script -- nothing about a visit is ever sent to an outside
-- service, and nothing here can be blocked by a browser extension the
-- way a client-side tracker can be.
--
-- Every table below is keyed on `day`, a UTC calendar date string
-- ('YYYY-MM-DD') -- NOT settings.checkin_net_timezone/local time (the
-- convention app/checkin.py's own net_date_for_ts() uses for a weekly
-- net scoped to one region's clock), and NOT an epoch timestamp. UTC
-- because this is a public website with visitors in every timezone,
-- so there is no single "local" that would mean anything for a global
-- page-view count. A plain string rather than an epoch keeps every
-- query below (BETWEEN, ORDER BY, GROUP BY) a simple string comparison
-- that already sorts and ranges correctly for ISO 8601 dates, with no
-- need to call SQLite's own date() function on every row.
--
-- No visitor is ever identified by their real IP address or their raw
-- User-Agent string -- see settings.traffic_salt's own comment in
-- app/config.py for the full hashing scheme
-- (sha256(salt + ip + user_agent), truncated to 16 hex characters) and
-- why the salt is what makes that irreversible rather than merely
-- obscured.
CREATE TABLE IF NOT EXISTS site_visitor (
    visitor_hash    TEXT PRIMARY KEY,             -- app/traffic.py's _hash_visitor()
    first_seen      TEXT NOT NULL,                -- UTC day string of this visitor's first-ever hit
    last_seen       TEXT NOT NULL,                -- UTC day string of this visitor's most recent hit
    hits            INTEGER NOT NULL DEFAULT 0,   -- lifetime page-view count, all days combined
    is_bot          INTEGER NOT NULL DEFAULT 0    -- app/traffic.py's _is_bot_user_agent()
);

-- Retention (app/traffic.py's prune_stale_traffic(), riding along on
-- ordinary request traffic at most once a day) deletes by last_seen, so
-- this is the one query pattern that benefits from an index beyond the
-- primary key.
CREATE INDEX IF NOT EXISTS idx_site_visitor_first_seen ON site_visitor(first_seen);

-- One row per (day, visitor) -- this is what makes "unique visitors
-- today" and "new visitors today" EXACT, computed directly from this
-- table's own rows, with no nightly rollup job needed to derive them
-- from a raw hit log: uniques-for-a-day is just COUNT(*) of rows for
-- that day, and new-visitors-for-a-day is COUNT(*) of rows for that day
-- whose visitor_hash has no earlier row in this same table (see
-- app/traffic.py's build_traffic_report() for the exact query, joined
-- to site_visitor.is_bot so a bot's presence never counts toward either
-- HUMAN figure).
--
-- `views` carries a per-day, per-visitor hit count -- the one field
-- beyond what a bare (day, visitor_hash) presence table would need,
-- added so a day's TOTAL page views (not just its unique visitor
-- count), split into human and bot totals by joining to
-- site_visitor.is_bot, can both be read straight out of this one table.
-- Without it, this table could only ever answer "how many distinct
-- people," never "how many page loads" -- the same role site_visitor's
-- own `hits` column already plays for a visitor's LIFETIME total,
-- scoped down here to one day.
CREATE TABLE IF NOT EXISTS site_visit_day (
    day             TEXT NOT NULL,
    visitor_hash    TEXT NOT NULL,
    views           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, visitor_hash)
);

-- Per-path daily view counts, for the admin panel's "top pages" list.
-- HUMAN HITS ONLY -- a crawler methodically walking every page on the
-- site would otherwise dominate this ranking and make it useless for
-- the thing it exists to answer ("what are real visitors actually
-- looking at"). Bot traffic is still fully recorded (site_visitor's
-- own is_bot flag, and rolled into site_visit_day.views for the
-- bot_views total), just never broken down by path here.
CREATE TABLE IF NOT EXISTS site_path_daily (
    day             TEXT NOT NULL,
    path            TEXT NOT NULL,
    views           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, path)
);

-- Per-referrer daily view counts, for the admin panel's "where visitors
-- came from" list. Same human-only reasoning as site_path_daily above.
-- Only ever scheme+host (e.g. "https://old-rival-site.com"), never a
-- full URL -- a full referrer URL can carry a path and query string
-- that leak what a visitor was doing on the SENDING site, which is
-- somebody else's visitor to protect, not just noise to strip. A
-- self-referral (this deployment linking to itself) is dropped entirely
-- rather than recorded, since "meshwars.com referred a visitor to
-- meshwars.com" is not information anyone asked this feature for.
CREATE TABLE IF NOT EXISTS site_referrer_daily (
    day             TEXT NOT NULL,
    referrer        TEXT NOT NULL,
    views           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, referrer)
);

-- ---------------------------------------------------------------------
-- Discord outbound announcements (app/discord_notify.py): end-of-month
-- honors posted to a Discord webhook, through a durable outbox rather
-- than a direct HTTP call at freeze time. Two things a direct call
-- cannot give that this table exists for:
--
-- 1. EXACTLY ONCE, across restarts. `kind` names what is being
--    announced ("month_honors" today, room for more later) and `key` is
--    that announcement's own natural key ("2026-08:mc" -- month and
--    protocol) -- the UNIQUE index on (kind, key) is what makes
--    enqueue()'s INSERT OR IGNORE a no-op on a duplicate rather than a
--    second post. Without it, a process restarting mid-drain, or the
--    admin re-freeze route (app/admin_ops.py's POST
--    /api/admin/month/freeze) calling app/results.py's freeze_month()
--    again for a month already announced, would repost the same honors
--    to the Discord channel every time.
--
-- 2. ENQUEUED INSIDE THE FREEZE TRANSACTION, not after it commits.
--    app/results.py's freeze_month() calls discord_notify.enqueue()
--    with the SAME `conn` it just wrote month_result/month_standing/
--    month_award through, before that transaction's caller commits
--    (app/db.py's WriteSession, or the admin route's own BEGIN
--    IMMEDIATE/COMMIT) -- so a freeze that raises and rolls back takes
--    this row with it. A month that never actually froze can never be
--    announced; there is no window where the outbox has a row for a
--    result the database does not.
--
-- posted_at IS NULL means pending -- picked up by run_forever()'s poll
-- loop, which does the actual HTTP POST OUTSIDE any WriteSession (a
-- webhook call is not database work and must never hold the single
-- global write lock while it waits on the network) and only takes the
-- lock afterward, briefly, to record the outcome. attempts/last_error
-- let a permanently-failing row (a revoked webhook, a deleted channel)
-- stop retrying forever rather than spinning every poll interval --
-- see settings.discord_outbox_max_attempts and
-- discord_outbox_max_age_hours in app/config.py for the two independent
-- reasons a pending row is skipped rather than posted.
CREATE TABLE IF NOT EXISTS discord_outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    key         TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    posted_at   INTEGER,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_discord_outbox_key ON discord_outbox(kind, key);

-- Singleton, same upsert-by-fixed-id shape as `checkin_config` and
-- `freqmapper_config` above -- read FRESH by app/discord_notify.py's
-- load_discord_config() every time it is needed (enqueue(), the drain
-- loop, build_month_honors_embed()), never cached in the process, so an
-- admin edit through app/admin_ops.py's /api/admin/discord takes effect
-- on the very next freeze or drain cycle, no restart. Before this table
-- existed, every one of these five values lived only in settings.py
-- (DISCORD_WEBHOOK_ANNOUNCEMENTS, DISCORD_WEBHOOK_USERNAME,
-- DISCORD_TEAM_EMOJI) and changing any of them meant editing .env and
-- redeploying -- the owner's "build everything editable, no black
-- boxes" rule this table exists to satisfy.
-- seed_discord_config_from_env (app/discord_notify.py, called from
-- init_db() below) bootstraps webhook_url/username/team_emoji from
-- those same settings.py values the first time this row is ever
-- touched, the exact same guarded-by-updated_at pattern
-- app/checkin.py's seed_nets_from_env and
-- app/freqmapper_ingest.py's seed_freqmapper_config_from_env already
-- use for their own singletons, so deploying this table changes NO
-- behavior for a deployment that already had a webhook configured via
-- .env: `enabled` is seeded to 1 whenever that seed finds a non-empty
-- webhook (settings.py itself never had a separate enabled/disabled
-- toggle -- "a webhook is configured" WAS the on/off switch, so the
-- seed reconstructs the same on/off state as a real column instead of
-- silently defaulting to off underneath an already-live deployment).
-- webhook_url is a SECRET (see freqmapper_config's own comment on
-- api_key for the general rule this follows -- a Discord webhook URL
-- carries its own bearer auth token in the path): never returned by any
-- route, only a webhook_set boolean plus a last-4-characters hint
-- (app/admin_ops.py's _scrub_discord_secrets). An ABSENT or blank
-- webhook_url in a POST /api/admin/discord body leaves the stored value
-- UNCHANGED rather than wiping it -- the exact same api_key-vs-
-- clear_api_key contract app/admin_ops.py's POST /api/admin/paint
-- already applies to freqmapper_config.api_key (see that route's own
-- docstring); clear_webhook is the explicit way to actually blank it.
-- announce_month_honors is a SEPARATE toggle from `enabled`, checked
-- only for the automatic end-of-month announcement
-- (app/discord_notify.py's enqueue(), kind="month_honors"): an operator
-- can leave the webhook enabled -- so a manual test announcement
-- (kind="test", from POST /api/admin/discord/test) still goes out --
-- while turning off the automatic monthly post on its own. Defaults to
-- 1 (on): this reproduces exactly the always-on behavior every
-- deployment already had before this toggle existed, the same
-- "deploying this changes nothing" contract every other seeded default
-- in this table follows.
CREATE TABLE IF NOT EXISTS discord_config (
    id                         INTEGER PRIMARY KEY CHECK (id = 1),
    enabled                    INTEGER NOT NULL DEFAULT 0,
    webhook_url                TEXT NOT NULL DEFAULT '',
    username                   TEXT NOT NULL DEFAULT '',
    team_emoji                 TEXT NOT NULL DEFAULT '',
    announce_month_honors      INTEGER NOT NULL DEFAULT 1,
    -- Per-kind gate for season closes (app/mc_scoring.py's
    -- maybe_roll_season()) -- same "separate from `enabled`, on by
    -- default" shape announce_month_honors already established: an
    -- operator who never visits /api/admin/discord to turn a new kind
    -- off keeps getting it, and either can be switched off without
    -- touching the webhook or any other kind. See MIGRATIONS below --
    -- discord_config already existed in every deployment before this
    -- column did, so an ALTER is also required there, not just here.
    announce_season_close      INTEGER NOT NULL DEFAULT 1,
    -- SUPERSEDED 2026-09-16 by announce_weekly_recap below: this used to
    -- gate a per-activation "notable place activation" announcement
    -- (app/place_scoring.py's credit_places(), app/discord_notify.py's
    -- since-removed build_place_activation_embed()/
    -- place_activation_notability()), retired for being too frequent
    -- (~373/month) and for announcing a player's location within minutes
    -- of them reaching it -- see credit_places()'s own comment on the
    -- removal. Column LEFT IN PLACE, per this codebase's "never drop a
    -- column" rule (place_activation.points_reason's own comment states
    -- the same rule for a different table) -- but nothing reads it any
    -- more; app/discord_notify.py's load_discord_config() no longer even
    -- selects it, and no admin route accepts or returns it.
    announce_place_activation  INTEGER NOT NULL DEFAULT 1,
    -- The Sunday weekly recap (app/discord_notify.py's
    -- weekly_recap_provider(), a TIME_DRIVEN_PROVIDERS entry) that
    -- replaced announce_place_activation's old per-event announcement
    -- above -- same independent, on-by-default, separate-from-`enabled`
    -- shape as every other per-kind gate in this table.
    announce_weekly_recap      INTEGER NOT NULL DEFAULT 1,
    -- Per-net wrap-up (app/discord_notify.py's net_wrapup_provider(), a
    -- TIME_DRIVEN_PROVIDERS entry, kind="net_wrapup:<checkin_net.id>") --
    -- same independent, on-by-default, separate-from-`enabled` shape as
    -- every other per-kind gate in this table. ONE toggle gates every
    -- net's wrap-up; an operator who wants only SOME nets to post routes
    -- the rest to a disabled discord_channel row instead (see that
    -- table's own comment) rather than this column growing a per-net
    -- flag of its own.
    announce_net_wrapup        INTEGER NOT NULL DEFAULT 1,
    -- Discord ROLE sync (app/discord_bot.py), an entirely separate
    -- feature from every announce_* toggle above (those gate what the
    -- WEBHOOK posts; this gates what the BOT does to guild members'
    -- roles). guild_id is non-secret and seeded from DISCORD_GUILD_ID
    -- the same one-time way webhook_url/username/team_emoji already
    -- are (seed_discord_config_from_env(), app/discord_notify.py).
    -- roles_enabled defaults to 0 (OFF), unlike every announce_*
    -- column's opt-out default -- see the MIGRATIONS entry for this
    -- same pair of columns for why.
    guild_id                   TEXT NOT NULL DEFAULT '',
    roles_enabled              INTEGER NOT NULL DEFAULT 0,
    -- Private per-team text channels (app/discord_bot.py's
    -- ensure_team_channels()), layered on TOP of role sync above --
    -- pointless without a team role to gate a channel's overwrites on,
    -- so this is gated by roles_enabled/guild_id/the bot token as well
    -- as its own toggle (see that function's own docstring). Same
    -- "defaults to 0 (OFF), needs an operator to actually run the
    -- ensure step" reasoning roles_enabled's own comment above gives --
    -- a database gaining this column must never start creating Discord
    -- channels on its own.
    team_channels_enabled      INTEGER NOT NULL DEFAULT 0,
    -- The category (Discord's own "channel type 4," a folder of
    -- channels) ensure_team_channels() creates/finds team channels
    -- under. Non-secret, admin-editable, plain text -- same shape as
    -- guild_id above. Defaults to 'Teams' so a deployment that turns
    -- team_channels_enabled on without first visiting
    -- /api/admin/discord to rename it still gets a sensible category.
    team_category_name         TEXT NOT NULL DEFAULT 'Teams',
    -- The category's discovered Discord id, remembered the exact same
    -- "found or created once, reused forever after" way
    -- discord_team_role.role_id already is for a team role -- see that
    -- table's own comment. Nullable: NULL until ensure_team_channels()
    -- has actually run once. Not admin-editable directly (an operator
    -- edits team_category_name instead; this column is this app's own
    -- bookkeeping, repaired automatically if the category is ever
    -- renamed or deleted by hand).
    team_category_id           TEXT,
    -- Discord slash commands (app/discord_interactions.py), HTTP
    -- Interactions -- a FOURTH, separate Discord integration from the
    -- three above (this table's own webhook/role/channel fields):
    -- Discord POSTs each command straight to POST
    -- /api/discord/interactions and this app answers in the HTTP
    -- response, no gateway connection. Defaults to 0 (OFF), same
    -- "must never turn itself on the moment a database happens to gain
    -- this column" reasoning roles_enabled's own comment above gives --
    -- an operator must deploy the endpoint AND enable it here BEFORE
    -- pasting the interactions URL into Discord's developer portal,
    -- since Discord verifies that URL immediately and refuses to save
    -- it otherwise (see app/discord_interactions.py's own module
    -- docstring). app_id and public_key are both non-secret (shown in
    -- Discord's own developer portal to anyone who can see the
    -- application) and seeded from DISCORD_APP_ID/DISCORD_PUBLIC_KEY
    -- the same one-time way guild_id seeds from DISCORD_GUILD_ID. The
    -- bot token used to REGISTER commands (app/discord_bot.py's
    -- register_commands()) is discord_bot_token, environment-only,
    -- same as every other use of it in this table -- never stored here.
    slash_enabled               INTEGER NOT NULL DEFAULT 0,
    app_id                      TEXT NOT NULL DEFAULT '',
    public_key                  TEXT NOT NULL DEFAULT '',
    -- The pinned, self-editing leaderboard (app/discord_leaderboard.py) --
    -- a FIFTH, separate Discord feature layered on top of the webhook
    -- (posts/edits through it, same as every announce_* kind above) AND
    -- the bot (pins the message it posts, same DISCORD_BOT_TOKEN role
    -- sync already authenticates with -- see discord_team_role's own
    -- comment). Same "must never turn itself on the moment a database
    -- happens to gain this column" reasoning as roles_enabled/
    -- slash_enabled above: defaults to 0 (OFF). leaderboard_interval_
    -- seconds is its OWN gate, separate from
    -- settings.discord_outbox_poll_interval_seconds -- a leaderboard
    -- pass reads every board's live standings and three Top Operators
    -- rankings, real work compared to the outbox's own cheap table scan,
    -- so it rides run_forever()'s existing loop (same "one background
    -- loop, its own interval gate" shape maybe_reconcile_roles() already
    -- uses) rather than running every single 30s cycle. Defaults to 600
    -- (10 minutes) -- frequent enough that a fresh capture shows up
    -- promptly, far below Discord's own per-webhook rate limit for a
    -- single edited message. leaderboard_top_n caps each of the three
    -- Top Operators lists the leaderboard embeds show (Wardrivers/
    -- NetOps/Explorer -- see app/discord_leaderboard.py's own module
    -- docstring), independent of those helpers' own internal top-20 cap
    -- in app/mc_api.py. Defaults to 5 -- short enough that three lists
    -- plus a full standings table comfortably fit one message's field
    -- budget (see app/discord_notify.py's _MAX_EMBED_FIELDS/
    -- _MAX_TOTAL_EMBED_CHARS, reused as-is by the leaderboard).
    leaderboard_enabled          INTEGER NOT NULL DEFAULT 0,
    leaderboard_interval_seconds INTEGER NOT NULL DEFAULT 600,
    leaderboard_top_n            INTEGER NOT NULL DEFAULT 5,
    updated_at                 INTEGER NOT NULL DEFAULT 0
);

-- Per-kind Discord channel routing, on top of discord_config's own
-- single default webhook above. `kind` matches discord_outbox.kind (a
-- plain announcement kind like "month_honors", or a colon-scoped one
-- like a future "net_wrapup:12" -- see app/discord_notify.py's
-- _channel_kind_candidates() for how a scoped kind resolves against
-- both a per-instance row and the generic one before falling back
-- here). No row for a kind at all is the common case and means "use
-- discord_config.webhook_url", exactly the one-channel behavior every
-- deployment already has -- this table only needs a row once an
-- operator actually wants a kind to go somewhere else.
--
-- `enabled` is NOT "fall back to the default when off". It means "do
-- not announce this kind at all" -- a deliberate, explicit distinction
-- from a MISSING row (which does fall back): an operator flipping a
-- kind off is choosing silence for that kind, and silently posting it
-- to the main channel anyway would be exactly the wrong behavior at
-- exactly the moment they asked for the opposite. See
-- app/discord_notify.py's resolve_discord_webhook() for the one place
-- that implements this rule.
--
-- webhook_url is a SECRET, same treatment as discord_config.webhook_url
-- above (never returned by any route, only a webhook_set boolean plus a
-- last-4 hint -- app/admin_ops.py's _scrub_discord_secrets). An ABSENT
-- or blank webhook_url in a POST /api/admin/discord/channel body leaves
-- a row's stored value UNCHANGED, same clear_webhook-to-actually-blank-it
-- contract discord_config's own POST route already uses.
--
-- Resolved FRESH at POST time, in the drain loop, never at enqueue
-- time, and NEVER stored on the discord_outbox row itself -- see
-- app/discord_notify.py's _drain_once() for the reasoning: a pending
-- row must follow an operator's later channel move, not the channel
-- that happened to be configured the moment it was queued.
CREATE TABLE IF NOT EXISTS discord_channel (
    kind        TEXT PRIMARY KEY,
    webhook_url TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    updated_at  INTEGER NOT NULL DEFAULT 0
);

-- Discord ROLE sync (app/discord_bot.py -- "Herald," a separate Discord
-- integration from the webhook announcements above: this one
-- authenticates as a bot, via DISCORD_BOT_TOKEN, and never posts a
-- message at all). One row per MeshWars team, remembering the Discord
-- role id app/discord_bot.py's ensure_team_roles() created (or
-- adopted, if a same-named role already existed) for that team, so a
-- role is discovered ONCE and reused forever after rather than
-- ensure_team_roles() searching the guild's role list by name on every
-- call, and so app/discord_bot.py's sync_member() knows which of a
-- member's current roles are "team roles" at all (any id in this
-- table) versus some other role this bot must never touch (moderator,
-- booster, anything else the guild has). `team` matches the keys of
-- app/discord_notify.py's own _TEAM_COLORS palette (RED, GREEN, ...)
-- -- reused verbatim rather than a second copy, so the two can never
-- name a different set of teams. discord_config.guild_id/roles_enabled
-- (below) are the other two pieces of this feature's config; both live
-- there rather than a third table, following that singleton's own
-- existing "one config row per Discord feature" shape.
--
-- channel_id (nullable): the private team-channel app/discord_bot.py's
-- ensure_team_channels() created (or adopted) for this team, once an
-- operator has also turned on discord_config.team_channels_enabled --
-- the SAME row that already remembers a team's role id remembers its
-- channel id too, rather than a second table, since both are
-- discovered/repaired by the same "find by id, else by name, else
-- create" pass and always travel together. NULL until
-- ensure_team_channels() has actually run for this team (a fresh
-- install, or one that has only ever used role sync, never touches
-- this column).
CREATE TABLE IF NOT EXISTS discord_team_role (
    team        TEXT PRIMARY KEY,
    role_id     TEXT NOT NULL,
    channel_id  TEXT,
    updated_at  INTEGER NOT NULL
);

-- The ONE pinned, self-editing leaderboard message app/discord_leaderboard.py
-- maintains (`kind` is always "leaderboard" today, but the column is a
-- free-form key rather than a fixed value so a future second pinned
-- message -- a per-net board, say -- can share this same table instead
-- of a near-duplicate one). webhook_id is the webhook's own numeric id,
-- PARSED from its URL, never the URL or its token itself: the token
-- already lives in discord_config.webhook_url/discord_channel.webhook_url
-- (both already SECRETS -- see discord_config's own comment), and this
-- table exists purely to remember WHICH message to edit next, which
-- needs no credential at all -- only channel_id/message_id (bot API
-- targets, both non-secret, same reasoning discord_team_role.channel_id
-- already gives) and webhook_id (compared against a freshly resolved
-- webhook's own parsed id on every pass, to detect an operator moving
-- the leaderboard to a different webhook -- see that module's own
-- docstring for what happens then). content_hash is the SHA-256 of the
-- message body MINUS its own "as of" timestamp line (see that module's
-- own docstring for why the timestamp itself is excluded from the hash
-- it gates) -- an unchanged hash means "don't PATCH," the whole point of
-- a pinned message that edits itself instead of spamming a new post
-- every interval. pinned is a plain 0/1 the bot sets after a successful
-- pin attempt, never assumed -- a missing bot token or a missing Pin
-- Messages permission must never fail the whole pass (see that module's
-- own docstring), it only ever leaves this at 0 so the admin panel can
-- say "not pinned" honestly.
CREATE TABLE IF NOT EXISTS discord_pinned_message (
    kind          TEXT PRIMARY KEY,
    webhook_id    TEXT NOT NULL,
    channel_id    TEXT NOT NULL,
    message_id    TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    pinned        INTEGER NOT NULL DEFAULT 0,
    updated_at    INTEGER NOT NULL
);

-- ---------------------------------------------------------------------
-- The public announcement feed (app/announce_content.py): transport-
-- neutral, structured Content -- a daily recap, a weekly recap, a
-- month's honors, or one net's wrap-up -- built from the same scoring
-- helpers app/results.py and Discord's own recaps already read, stored
-- here so it is built exactly once and can be replayed to any consumer
-- (a future public API route, a future radio-broadcast clock -- neither
-- exists yet; this table is only the storage foundation for them).
--
-- `id` is the PUBLIC CURSOR, not an opaque surrogate key: a consumer
-- polls with "?since=<id>" and reads every row with id > since, in
-- insertion order -- a plain AUTOINCREMENT already gives that ordering
-- for free, with no separate sequence column to keep in step. `kind` +
-- `key` is the same dedup shape discord_outbox already uses for its own
-- announcements just above (see that table's own comment) -- `key` is
-- the Content's own natural key (a date, an ISO week, a "YYYY-MM", or a
-- "<net_id>:<net_date>" -- see each builder in announce_content.py),
-- unique only together with `kind`, since two different kinds can
-- legitimately share the same literal key string without colliding.
-- store_announcement()'s INSERT OR IGNORE against the UNIQUE index
-- below is what makes re-building an already-stored Content a no-op
-- rather than a duplicate row -- the same reason a re-freeze of an
-- already-announced month drops its Discord repost silently instead of
-- posting twice.
--
-- `content` is the whole built Content dict, json.dumps()'d whole --
-- no per-field column for headline/sections/etc, the same choice
-- discord_outbox.payload and board_cache.body already make for a blob
-- that only ever needs to be read back out whole, never queried by one
-- of its own fields. `board` and `net_id` are pulled out as real
-- columns anyway (duplicating what is also inside `content`) purely so
-- a consumer can filter/join on them in SQL without parsing the JSON
-- first; net_id is NULL for every kind except net_wrapup, the only one
-- scoped to a single checkin_net row rather than a whole board.
CREATE TABLE IF NOT EXISTS announcement (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    key         TEXT NOT NULL,
    board       TEXT NOT NULL,          -- 'mc' | 'mt'
    net_id      INTEGER,                -- NULL except for net_wrapup
    content     TEXT NOT NULL,          -- json.dumps() of the Content dict
    created_at  INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_announcement_key ON announcement(kind, key);
"""



MIGRATIONS = [
    # Nullable on purpose, both of them: every award written before these
    # columns existed genuinely has no value to backfill. message_ts is
    # not recoverable at all -- the check-in feed only serves its newest
    # 100 messages, so a net that passed without this column can never
    # have its posting times reconstructed. streak IS recoverable, since
    # app/checkin.py's checkin_streak() derives it from net_date history
    # alone; it is added null here and filled by a one-time backfill
    # running that same function, so a backfilled row and an awarded one
    # can never disagree about what a streak means.
    "ALTER TABLE mc_checkin_award ADD COLUMN message_ts INTEGER",
    "ALTER TABLE mc_checkin_award ADD COLUMN streak INTEGER",
    # Defaults to 0 (not aircraft), which is the correct value for every
    # capture recorded before the check existed: nothing was flying that
    # we know of, and treating unknown as "on the ground" keeps old
    # captures eligible for the exploration awards rather than silently
    # disqualifying history.
    "ALTER TABLE mc_tile_capture_log ADD COLUMN by_air INTEGER NOT NULL DEFAULT 0",
    # mc_tile's grid columns (see that table's SCHEMA comment above).
    #
    # The index is PLAIN, not partial. It was specified as
    #   ... WHERE owner_team IS NOT NULL
    # which is always true: owner_team is declared NOT NULL, so the
    # predicate excludes no rows. SQLite folds an always-true term away and
    # is then unable to prove the partial index's own predicate holds, so
    # the index becomes structurally unusable -- EXPLAIN QUERY PLAN skips
    # it and INDEXED BY reports "no query solution". Same rows, same size,
    # never chosen. Verified against a copy of the live board before this
    # went in; the plain form is used, with season_id as an equality seek
    # and lat_idx as a range.
    #
    # The backfill is guarded on lat_idx IS NULL rather than run
    # unconditionally: MIGRATIONS re-runs on every boot, and an unguarded
    # UPDATE would rewrite every row of the table each time the process
    # starts.
    #
    # The index belongs here rather than beside idx_mc_tile_owner in SCHEMA
    # because SCHEMA executes BEFORE this list, so on an existing database
    # it would run against columns that do not exist yet -- the same
    # ordering trap already documented for player.active above.
    "ALTER TABLE mc_tile ADD COLUMN lat_idx INTEGER",
    "ALTER TABLE mc_tile ADD COLUMN lon_idx INTEGER",
    "UPDATE mc_tile SET"
    "  lat_idx = CAST(SUBSTR(cell_id, 1, INSTR(cell_id, '_') - 1) AS INTEGER),"
    "  lon_idx = CAST(SUBSTR(cell_id, INSTR(cell_id, '_') + 1) AS INTEGER)"
    " WHERE lat_idx IS NULL OR lon_idx IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_mc_tile_grid ON mc_tile(season_id, lat_idx, lon_idx)",
    "ALTER TABLE tile ADD COLUMN last_packet_id INTEGER",
    "ALTER TABLE tile_unique_painter ADD COLUMN paint_count INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE player_ingest_stat ADD COLUMN pings_out_of_area INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE player_ingest_stat ADD COLUMN pings_no_repeaters INTEGER NOT NULL DEFAULT 0",
    # Every mc_season row that exists before this migration runs was
    # created by the MeshCore ingest path, so backfilling to 'mc' is not
    # a guess -- it is the only value that has ever been possible. SQLite
    # applies the column default to existing rows on ADD COLUMN, so this
    # single ALTER both adds the column and backfills it in one step.
    "ALTER TABLE mc_season ADD COLUMN protocol TEXT NOT NULL DEFAULT 'mc'",
    # Data fixup, not a schema change: real MeshCore hardware reports the
    # contact key in uppercase (app/mc_ingest.py now lowercases on
    # ingest), but one row was written before that normalization existed.
    # A plain UPDATE is safe to re-run here -- once applied, the WHERE
    # clause matches nothing, so every later run is a no-op rather than
    # an error.
    "UPDATE player_node SET node_ref = lower(node_ref) WHERE node_ref <> lower(node_ref)",
    # Canonical-form fixup, same idea as the lower() entry just above:
    # this branch's app/node_ref.py makes bare lowercase 8-hex the
    # canonical form for BOTH protocols. MeshCore has written bare since
    # before that module existed, so this never touches an 'mc' row in
    # practice -- but every Meshtastic row written by the OLD (pre-branch)
    # join/admin code carries a literal leading "!" (matching what
    # app/ingest.py's old _node_hex()-keyed lookup expected), and that
    # code is exactly what production has been running. Once this branch
    # deploys, app/ingest.py's registered-player lookup switches to
    # _bare_node_ref() (bare, no "!"), so any row still carrying the old
    # "!" prefix would silently stop matching -- an existing, currently-
    # scoring node going dark on deploy day with no error anywhere. This
    # strips that one leading "!" so every row already agrees with the
    # new lookup before the new code ever runs a query.
    #
    # Collision guard: player_node's primary key is (protocol, node_ref),
    # so if a bare row already exists for the same protocol and node --
    # not possible today (MeshCore has only ever written bare, and every
    # live Meshtastic row still carries its original "!"), but not
    # provably impossible on some other deployment's data either -- a
    # blind UPDATE would hit a PRIMARY KEY constraint violation and take
    # the whole migration down with it. The NOT EXISTS guard below skips
    # a row in exactly that situation instead: it is left carrying its
    # "!" for a human to sort out, rather than this migration silently
    # deleting or overwriting somebody's existing binding just to make
    # itself succeed. Same reasoning the capture_log backfill just below
    # uses to leave an ambiguous row alone rather than guess at it.
    #
    # Idempotent: once a row's "!" is stripped, `node_ref LIKE '!%'` no
    # longer matches it, so a second run is a no-op for it -- and a row
    # skipped by the collision guard stays skipped (same NOT EXISTS
    # result) rather than erroring, on every later run too.
    """
    UPDATE player_node
       SET node_ref = substr(node_ref, 2)
     WHERE node_ref LIKE '!%'
       AND NOT EXISTS (
             SELECT 1 FROM player_node AS existing
              WHERE existing.protocol = player_node.protocol
                AND existing.node_ref = substr(player_node.node_ref, 2)
           )
    """,
    # Backfill: mc_tile_capture_log was never written for a square's
    # first claim (only flips were logged) until the fix above, so two
    # real captures already on the board have no log row even though
    # mc_tile_capture and mc_tile both record them happening. Recover a
    # log row for each one, but ONLY where mc_tile.paint_count = 1 --
    # that means the square has been painted exactly once, so the
    # player who painted it (mc_tile.last_player_id) is necessarily the
    # one who captured it. A square painted more than once could have
    # been captured by an earlier, different paint and reinforced since,
    # in which case last_player_id would name the wrong person -- a
    # fabricated record naming the wrong capturer is worse than no
    # record at all, so those are left alone. Safe to re-run: once a
    # (season_id, cell_id) pair has a log row the NOT EXISTS guard below
    # excludes it, so the SELECT finds nothing left to insert.
    """
    INSERT INTO mc_tile_capture_log(season_id, cell_id, ts, by_player_id, by_team, from_team)
    SELECT tc.season_id, tc.cell_id, tc.captured_at, t.last_player_id, tc.captured_by_team, NULL
      FROM mc_tile_capture tc
      JOIN mc_tile t ON t.season_id = tc.season_id AND t.cell_id = tc.cell_id
     WHERE t.paint_count = 1
       AND NOT EXISTS (
             SELECT 1 FROM mc_tile_capture_log l
              WHERE l.season_id = tc.season_id AND l.cell_id = tc.cell_id
           )
    """,
    # Net check-ins (app/checkin.py) add a second, separate figure to a
    # team's season standing -- mc_season_team_tally already exists in
    # production holding only `tiles`, so the new column has to be an
    # ALTER, unlike mc_checkin_award/mc_checkin_seen_message above
    # (those are brand new tables, so CREATE TABLE IF NOT EXISTS in
    # SCHEMA already covers them). Kept as its own column rather than
    # folded into `tiles` so a closed
    # season's history can still show where a team's combined total
    # came from -- see mc_scoring.team_totals() for the combined figure
    # itself, which is what decides the winner.
    "ALTER TABLE mc_season_team_tally ADD COLUMN checkin_points REAL NOT NULL DEFAULT 0",
    # Nullable on purpose: every binding written before this column
    # existed has no key to backfill, and supplying one at registration
    # is optional going forward too. See mt_node_key above -- the public
    # key is the stable identity, node_ref is not, so this rides along
    # as metadata; attribution still keys on node_ref because that is
    # all a position packet carries.
    "ALTER TABLE player_node ADD COLUMN public_key TEXT",
    # `place` first landed (see above) without `rotates` -- any DB that
    # ran that migration before this one needs the column added by
    # hand; SQLite's ADD COLUMN default backfills every existing row to
    # 0 (always active) in the same statement, which is safe: a place
    # loaded before rotation existed gets re-classified correctly the
    # next time app/places_seed.py runs (it upserts `rotates` on every
    # row, not just new ones), so a stale 0 here is corrected within
    # one seed load, never a lasting misclassification.
    "ALTER TABLE place ADD COLUMN rotates INTEGER NOT NULL DEFAULT 0",
    # `active` added after `place` first landed, same situation as
    # `rotates` just above: any DB that already ran this migration set
    # needs the column added by hand. Defaults every existing row to 1
    # (active), which is safe even for a row that should actually be
    # inactive -- the very next places_seed reconcile pass (which now
    # runs on every load, not just a fingerprint-changed one) corrects
    # it within one startup, never a lasting misclassification. See
    # app/places_seed.py's load_places_seed() for the reconcile itself.
    "ALTER TABLE place ADD COLUMN active INTEGER NOT NULL DEFAULT 1",
    "CREATE INDEX IF NOT EXISTS idx_place_active ON place(active)",
    # `points_reason` added 2026-08-25 alongside the effort-based scoring
    # model (see `place`'s own CREATE TABLE comment above) -- any DB that
    # already ran this migration set needs the column added by hand.
    # Nullable, backfilled to NULL for existing rows: the very next
    # places_seed load re-upserts every row (including points_reason)
    # from the CSV, so a stale NULL here never lasts more than one
    # startup for a row still in the seed.
    "ALTER TABLE place ADD COLUMN points_reason TEXT",
    # `elevation_ft` added 2026-08-25 alongside summit elevation scaling
    # (see `place`'s own CREATE TABLE comment above) -- same situation
    # as points_reason just above: any DB that already ran this
    # migration set needs the column added by hand. Nullable, backfilled
    # to NULL for existing rows (correct for every non-summit row
    # permanently, and for a summit row until the next reconcile fills
    # it in from the CSV, same one-startup window points_reason's own
    # migration note describes).
    "ALTER TABLE place ADD COLUMN elevation_ft REAL",
    # A month is scored on ground HELD at the close, not captures made:
    # the old `captures` column counted capture events, so one square
    # could score many times and it read in different units from the
    # scoreboard. Added rather than renamed, because a RENAME raises on
    # a database created from the current schema and only "duplicate
    # column" is tolerated above. On an already-existing database the
    # dead `captures` column stays behind, harmless -- it is NOT NULL
    # DEFAULT 0 and nothing writes it, and month_standing is rewritten
    # wholesale by results.freeze_month() anyway.
    "ALTER TABLE month_standing ADD COLUMN squares INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE month_standing ADD COLUMN explorer_points REAL NOT NULL DEFAULT 0",
    # Which board earned a place credit. Backfilled for existing rows
    # by scripts/backfill_activation_protocol.py, which traces each one
    # to the capture that earned it; rows it cannot place keep '' and
    # are simply invisible to the per-board honors.
    "ALTER TABLE place_activation ADD COLUMN protocol TEXT NOT NULL DEFAULT ''",
    # Game-integrity gates added 2026-08-25 (see app/config.py's
    # mt_min_precision_bits/mt_max_speed_mps and app/ingest.py): every
    # existing player_cell_ping/player_ingest_stat row predates both
    # gates and is left exactly as scored -- these ALTERs only make the
    # columns exist for a DB that already ran CREATE TABLE without them;
    # nothing already credited is touched. precision_bits is nullable
    # (genuinely unknown for a ping written before this column existed --
    # nothing to backfill it from); the two new stat counters default to
    # 0, correct for every day already tallied since neither gate was
    # checking anything yet.
    "ALTER TABLE player_cell_ping ADD COLUMN precision_bits INTEGER",
    # evidence_type added after player_cell_ping already shipped -- see
    # that column's own comment on the CREATE TABLE above. NULL for
    # every existing row: correct for 100% of them, since FreqMapper's
    # passive_rx event type did not paint anything before this column
    # existed, and this column's whole job is distinguishing FreqMapper's
    # two evidence types from each other, not meshview/MeshCore rows from
    # FreqMapper ones.
    "ALTER TABLE player_cell_ping ADD COLUMN evidence_type TEXT",
    # The full FreqMapper "capture signal" column group added after
    # player_cell_ping already shipped -- see that column group's own
    # comment on the CREATE TABLE above. NULL for every existing row:
    # correct for 100% of them, since every one of these thirteen
    # columns is a FreqMapper feed field (how many Watchers verified/
    # corroborated an event, and -- for passive_rx -- how strong the
    # reception was) that no ingest path recorded before this migration
    # -- there is nothing to backfill any of them from. Recorded for
    # reference only; see freqmapper_config's own comment below
    # (watcher_weight_*) for the scoring switch these fields explicitly
    # do NOT drive.
    "ALTER TABLE player_cell_ping ADD COLUMN watcher_count INTEGER",
    "ALTER TABLE player_cell_ping ADD COLUMN same_region_watcher_count INTEGER",
    "ALTER TABLE player_cell_ping ADD COLUMN cross_region_watcher_count INTEGER",
    "ALTER TABLE player_cell_ping ADD COLUMN watcher_corroborated INTEGER",
    "ALTER TABLE player_cell_ping ADD COLUMN quality TEXT",
    "ALTER TABLE player_cell_ping ADD COLUMN rssi_dbm REAL",
    "ALTER TABLE player_cell_ping ADD COLUMN snr_db REAL",
    "ALTER TABLE player_cell_ping ADD COLUMN hop_count INTEGER",
    "ALTER TABLE player_cell_ping ADD COLUMN path_classification TEXT",
    "ALTER TABLE player_cell_ping ADD COLUMN last_relay_node INTEGER",
    "ALTER TABLE player_cell_ping ADD COLUMN packet_type TEXT",
    "ALTER TABLE player_cell_ping ADD COLUMN portnum INTEGER",
    "ALTER TABLE player_cell_ping ADD COLUMN location_accuracy_meters REAL",
    "ALTER TABLE player_ingest_stat ADD COLUMN pings_low_precision INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE player_ingest_stat ADD COLUMN pings_implausible_speed INTEGER NOT NULL DEFAULT 0",
    # Net check-ins move to checkin_seen_message (connector, packet_id),
    # keyed wide enough to cover multiple connector instances -- see
    # that table's own comment in SCHEMA above for why. Every id
    # mc_checkin_seen_message already holds was seen on exactly one
    # connector in practice (production has only ever run one
    # MeshCore feed, live.mwmesh.com), so backfilling all of it under
    # that literal URL is not a guess, it is simply naming the
    # connector that was implicit before this table existed.
    # INSERT OR IGNORE, not INSERT: a database that has already run
    # this backfill (or that somehow already has a matching row from
    # elsewhere) leaves that row alone rather than erroring on the
    # PRIMARY KEY. CAST(packet_id AS TEXT) because
    # mc_checkin_seen_message.packet_id is INTEGER and
    # checkin_seen_message.packet_id is TEXT (so it can hold either
    # protocol's id -- see that table's comment).
    "INSERT OR IGNORE INTO checkin_seen_message(connector, packet_id, seen_at) "
    "SELECT 'https://live.mwmesh.com', CAST(packet_id AS TEXT), seen_at "
    "  FROM mc_checkin_seen_message",
    # Seed the checkin_config singleton with the defaults every fresh
    # column above already carries, so the row exists unconditionally
    # from the first boot after this migration runs -- app/checkin.py's
    # poller and app/admin_ops.py's admin routes both assume it is
    # always there, never optionally-absent, the same way `notice`'s
    # singleton is assumed always-present by its own reader. INSERT OR
    # IGNORE: app/checkin.py's seed_nets_from_env() is what actually
    # populates this row from settings on a truly fresh install (it
    # only overwrites while updated_at is still 0, so it can tell the
    # difference between "still this migration's bare defaults" and
    # "an operator already edited it") -- this migration only has to
    # guarantee the row EXISTS, not what it holds.
    "INSERT OR IGNORE INTO checkin_config(id) VALUES (1)",
    # Connector KIND made first-class (app/checkin.py's CoreScopeClient/
    # BeaconClient/KIND_PROTOCOL): `protocol` alone used to imply exactly
    # one hardcoded connector implementation per value ('mc' meant
    # CoreScope, full stop) -- now that a second MeshCore-family
    # connector (Beacon) exists, `kind` is the admin's actual choice and
    # `protocol` is derived FROM it, so this column has to exist
    # separately rather than being read back out of `protocol`. Added
    # with a blank default (not one of the three real values) so the
    # backfill immediately below can tell "never touched by this
    # migration" apart from "an operator genuinely configured something"
    # on a database that somehow already had a non-empty kind column
    # from a previous partial run of this same migration list.
    "ALTER TABLE checkin_net ADD COLUMN kind TEXT NOT NULL DEFAULT ''",
    # Backfill: every net that exists before this migration ever runs
    # was necessarily hardcoded to the one connector implementation its
    # protocol always meant -- 'mc' rows are all CoreScope (Beacon did
    # not exist as an option yet), 'mt' rows are all meshview (the only
    # Meshtastic connector this app has ever spoken to). Plain UPDATEs,
    # not folded into the ALTER's own DEFAULT, because the default has
    # to stay '' (see above) for the "never touched yet" check to mean
    # anything; idempotent on every later run since the `kind=''` guard
    # matches nothing once a row has already been backfilled or an
    # operator has since edited it through the admin API.
    "UPDATE checkin_net SET kind='corescope' WHERE kind='' AND protocol='mc'",
    "UPDATE checkin_net SET kind='meshview'  WHERE kind='' AND protocol='mt'",
    # Fourth connector kind, 'mqtt' (app/mqtt_subscriber.py): any
    # database that ran the checkin_net CREATE TABLE before these four
    # columns existed needs them added by hand -- see that table's own
    # comment in SCHEMA above for what each one means. All four default
    # to '' for every existing row, which is exactly correct: no net
    # created before this migration could have been kind='mqtt' (the
    # option did not exist yet), so there is nothing real to backfill,
    # the same reasoning the 'kind' column's own backfill above already
    # relies on for corescope/meshview.
    "ALTER TABLE checkin_net ADD COLUMN broker_username TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE checkin_net ADD COLUMN broker_password TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE checkin_net ADD COLUMN channel_key TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE checkin_net ADD COLUMN topic_root TEXT NOT NULL DEFAULT ''",
    # checkin_player_name (player_id-keyed) replaced by checkin_node_name
    # (node_ref-keyed) above -- see that table's own comment for why a
    # player_id key made every multi-radio player's rows flip-flop and
    # false-alarm as a "name changed" on nearly every poll. A DROP
    # rather than an ALTER because SQLite cannot change a table's
    # PRIMARY KEY in place, and there is nothing here worth an in-place
    # migration for: this table is pure observability, holds no
    # historical value check-in resolution or awarding ever reads, and
    # on every database it has existed on so far it is minutes old.
    # CREATE TABLE IF NOT EXISTS above already handles a database that
    # never had checkin_player_name at all (never sees this table name,
    # DROP IF EXISTS is a no-op for it); this line only matters for a
    # database that ran the earlier schema.
    "DROP TABLE IF EXISTS checkin_player_name",
    # Seed the freqmapper_config singleton with the defaults every fresh
    # column above already carries, so the row exists unconditionally
    # from the first boot after this migration runs -- same reasoning as
    # checkin_config's own "INSERT OR IGNORE...VALUES (1)" migration
    # above: app/freqmapper_ingest.py's poller and app/admin_ops.py's
    # paint routes both assume it is always there. INSERT OR IGNORE:
    # seed_freqmapper_config_from_env() (called from init_db() below) is
    # what actually populates this row from settings.py on a truly fresh
    # install; this migration only has to guarantee the row EXISTS, not
    # what it holds.
    "INSERT OR IGNORE INTO freqmapper_config(id) VALUES (1)",
    # paint_from added after freqmapper_config already shipped -- see
    # that column's own comment on the CREATE TABLE above. Defaults to
    # '' (block every event), the same safe-by-default value a fresh
    # install's CREATE TABLE already gives the column, so an existing
    # deployment upgrading into this migration keeps painting exactly
    # nothing extra until an operator explicitly sets a date.
    "ALTER TABLE freqmapper_config ADD COLUMN paint_from TEXT NOT NULL DEFAULT ''",
    # watcher_weight_* added after freqmapper_config already shipped --
    # see that column group's own comment on the CREATE TABLE above,
    # which is now the operative one: the scoring path these columns
    # once fed (app/freqmapper_ingest.py's old _verified_tx_points())
    # has since been removed outright, by Matt's explicit decision that
    # MeshWars scores coverage as coverage and must not be re-weighted
    # by watcher_count or quality. These four ALTERs stay exactly as
    # they always were -- still safe, still a no-op on every existing
    # row's score, now simply the last place in this codebase these
    # columns are ever written at all, kept only so a database that
    # already ran this migration does not need a destructive column
    # drop.
    "ALTER TABLE freqmapper_config ADD COLUMN watcher_weight_enabled INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE freqmapper_config ADD COLUMN watcher_weight_base REAL NOT NULL DEFAULT 0.5",
    "ALTER TABLE freqmapper_config ADD COLUMN watcher_weight_increment REAL NOT NULL DEFAULT 0.1",
    "ALTER TABLE freqmapper_config ADD COLUMN watcher_weight_cap REAL NOT NULL DEFAULT 1.0",
    # allow_backfill added after freqmapper_config already shipped --
    # see that column's own comment on the CREATE TABLE above. Defaults
    # to 0 (guard active), the same safe-by-default value a fresh
    # install's CREATE TABLE already gives it, so an existing deployment
    # upgrading into this migration keeps the backfill guard on and
    # changes NO painting behavior until an operator explicitly opts in.
    "ALTER TABLE freqmapper_config ADD COLUMN allow_backfill INTEGER NOT NULL DEFAULT 0",
    # passive_rx_* added after freqmapper_config already shipped -- see
    # that column group's own comment on the CREATE TABLE above.
    # passive_rx_enabled defaults to 1 (ON) rather than the safe-off
    # default every other feature toggle above uses: passive RX never
    # painted anything before this migration exists to enable it, so
    # there is no "an existing deployment's scores must not change"
    # invariant to protect here the way allow_backfill/
    # watcher_weight_enabled's off-by-default choices protect one --
    # this IS the feature this deployment was built to ship, and it has
    # to work immediately after the migration runs, with no follow-up
    # database edit, for an operator who never touches
    # freqmapper_config by hand to actually get RX-sourced painting.
    # points_per_event/unique_painter_bonus default to 0.5 each,
    # matching verified_tx's own defaults -- Matt's explicit decision:
    # coverage is coverage, an RX reception is not weaker evidence for
    # scoring purposes even though it is provenance-distinct (see
    # player_cell_ping.evidence_type's own comment) and never presented
    # to FreqMapper as verified-TX proof.
    "ALTER TABLE freqmapper_config ADD COLUMN passive_rx_enabled INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE freqmapper_config ADD COLUMN passive_rx_points_per_event REAL NOT NULL DEFAULT 0.5",
    "ALTER TABLE freqmapper_config ADD COLUMN passive_rx_unique_painter_bonus REAL NOT NULL DEFAULT 0.5",
    # The account layer's link to the existing player model (see the
    # "Account layer" section in SCHEMA above for the full story) --
    # `player` is a pre-existing table with rows already in it on every
    # real deployment, so this new column has to be an ALTER, unlike
    # account/account_identity/account_session/account_link_event
    # themselves (brand new tables, CREATE TABLE IF NOT EXISTS in SCHEMA
    # already covers them). NULL for every row until a player links an
    # account through app/account_api.py's POST /api/account/link-key --
    # correct for 100% of existing rows, since the account layer did not
    # exist before this migration and nothing could have set it.
    "ALTER TABLE player ADD COLUMN account_id INTEGER",
    # Enforces the "at most one player per account" half of the
    # one-to-one contract at the database level, not just in
    # application code -- app/account_api.py's link-key handler already
    # checks this itself before writing (see its own comment for why:
    # a friendly, specific error beats a raw IntegrityError leaking out
    # as a 500), but a UNIQUE index means that invariant holds even
    # against a future code path that forgets to check. A UNIQUE index
    # in SQLite treats every NULL as distinct from every other NULL, so
    # any number of players with no linked account (NULL) coexist
    # freely -- only two REAL, non-null account_id values colliding is
    # rejected. Same reason idx_place_active isn't created inside
    # SCHEMA's CREATE TABLE block: on a database that already ran
    # SCHEMA before this ALTER added the column, conn.executescript(SCHEMA)
    # executes before this MIGRATIONS list ever runs, so an index
    # referencing account_id here would fail startup on every existing
    # deployment with "no such column: account_id" -- it has to be
    # created down here, after the ALTER immediately above guarantees
    # the column exists first.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_player_account ON player(account_id)",
    # contact_email: a user-editable address on the ACCOUNT itself, for
    # contact purposes only (an operator or a future notification
    # reaching the person who owns this account) -- a pre-existing
    # table with rows already in it on every real deployment, so this
    # is an ALTER same as player.account_id above, not a CREATE TABLE.
    # Deliberately NOT an account_identity row and NOT usable to sign
    # in: account_identity's own email/email_verified columns are what
    # the callback decision tree (app/oauth_api.py's
    # resolve_oauth_callback, case 3) and password sign-in
    # (app/oauth_api.py's POST /auth/password/start) both read to
    # decide who an address belongs to, and both deliberately read
    # ONLY that table, never this column -- see the case-3 matching
    # query's own comment in app/oauth_api.py for exactly why folding
    # this column into that check would be an account-takeover path.
    # NULL for every row until a person sets one through
    # app/account_api.py's POST /api/account/contact-email; unverified
    # (contact_email_verified_at NULL) the moment it is set, verified
    # only once GET /auth/contact-email/verify redeems a token mailed
    # to it -- OR, since POST /api/account/contact-email/use-identity
    # (app/account_api.py), verified immediately when the address was
    # copied server-side from the account's own already-provider-
    # verified account_identity row instead of typed by hand; see that
    # route's own docstring for why skipping the mailed round-trip is
    # correct in that one case and nowhere else.
    "ALTER TABLE account ADD COLUMN contact_email TEXT",
    "ALTER TABLE account ADD COLUMN contact_email_verified_at INTEGER",
    # The role-gated admin/operator surface (app/admin_api.py's
    # _role_guard) needs somewhere to persist "this account is an
    # operator/admin" that survives a restart -- `account`, like
    # player.account_id and account.contact_email above, is a
    # pre-existing table with rows already in it on every real
    # deployment, so this is an ALTER, not part of the original CREATE
    # TABLE. NULL (no role) for every row until POST
    # /api/admin/roles/claim grants the first operator, or an operator
    # grants admin to someone else through POST /api/admin/roles/grant --
    # correct for 100% of existing rows, since no account could have
    # held a role before this column existed. Deliberately just a plain
    # TEXT column ('admin' | 'operator' | NULL), not a separate
    # account_role join table: one account holds AT MOST one role at a
    # time in this design (see app/admin_api.py's own module docstring
    # for the two-tier model), so there is nothing a join table would
    # let this represent that a single nullable column cannot.
    "ALTER TABLE account ADD COLUMN role TEXT",
    # A partial index -- only the non-NULL rows, which on any real
    # deployment is a small handful of operators/admins out of every
    # account that has ever signed in -- for the two queries that scan
    # role by VALUE rather than by account_id (already covered for free
    # by account's own PRIMARY KEY): _admin_surface_enabled()'s "does
    # any account hold a role at all" check, and GET /api/admin/roles'
    # roster listing. Created down here, after the ALTER immediately
    # above guarantees the column exists, for the same reason
    # idx_player_account is created after player.account_id's own ALTER
    # rather than inside SCHEMA's CREATE TABLE block (see that index's
    # own comment): on a database that already ran SCHEMA before this
    # ALTER added the column, an index referencing `role` inside SCHEMA
    # itself would fail startup with "no such column: role".
    "CREATE INDEX IF NOT EXISTS idx_account_role ON account(role) WHERE role IS NOT NULL",
    # `sample` removed entirely -- see the comment left in its place in
    # SCHEMA above (right before node_seen) for the full privacy
    # reasoning. Unlike checkin_player_name's own DROP TABLE further up
    # this list, there is no in-place migration to consider and nothing
    # to backfill anywhere else first: this table was dead code on both
    # ends (app/ingest.py stopped writing it before this was noticed;
    # /get-samples -- also removed now, see app/api.py -- only ever
    # returned a hardcoded empty list), so dropping it changes no
    # behavior, only removes data at rest. DROP TABLE IF EXISTS is
    # naturally idempotent on its own: a database that already had this
    # migration applied (or was created fresh under the current SCHEMA,
    # which never has `sample` at all) sees a no-op here, same as every
    # other run.
    "DROP TABLE IF EXISTS sample",
    # pings_unknown_type added after player_ingest_stat already shipped --
    # see that column's own comment on the CREATE TABLE above for the
    # full story (a MeshCore ping whose `type` is present but not one of
    # TX/RX/DISC/TRACE, e.g. "DEFER"). ADD COLUMN ... DEFAULT 0 backfills
    # every existing row in the same statement SQLite runs the ALTER in
    # -- correct for 100% of them, since nothing before this column
    # existed could have counted toward it, and it does not change what
    # any of those rows' other counters (in particular pings_no_repeaters,
    # which a ping like this always also incremented, and still does)
    # already mean.
    "ALTER TABLE player_ingest_stat ADD COLUMN pings_unknown_type INTEGER NOT NULL DEFAULT 0",
    # Nullable, same reasoning as message_ts/streak above: every award
    # written before this column existed has no net to backfill from
    # the row alone (protocol + net_date is ambiguous whenever two nets
    # share a protocol and weekday -- see checkin_streak's own comment
    # on why PROTOCOL-only scoping broke multi-net streaks). Recoverable
    # for most rows by matching protocol + net_date's weekday against
    # checkin_net -- see tools/backfill_net_id.py, a one-time,
    # dry-run-by-default script, NOT run automatically here, since it
    # also recomputes streak/points and those need an operator to review
    # before committing. app/checkin.py's _award_checkin now threads the
    # originating checkin_net.id through on every new award, so only
    # historical rows are ever NULL going forward.
    "ALTER TABLE mc_checkin_award ADD COLUMN net_id INTEGER",
    "CREATE INDEX IF NOT EXISTS idx_mc_checkin_award_net ON mc_checkin_award(net_id, player_id, net_date)",
    # Seed the discord_config singleton with the defaults every fresh
    # column above already carries, so the row exists unconditionally
    # from the first boot after this migration runs -- same reasoning as
    # checkin_config's and freqmapper_config's own "INSERT OR
    # IGNORE...VALUES (1)" migrations above: app/discord_notify.py's
    # load_discord_config() and app/admin_ops.py's discord routes both
    # assume it is always there. INSERT OR IGNORE:
    # seed_discord_config_from_env() (called from init_db() below) is
    # what actually populates webhook_url/username/team_emoji from
    # settings.py on a truly fresh install; this migration only has to
    # guarantee the row EXISTS, not what it holds.
    "INSERT OR IGNORE INTO discord_config(id) VALUES (1)",
    # announce_season_close / announce_place_activation added after
    # discord_config already shipped and was live in every deployment
    # (unlike announce_month_honors, which landed in the same CREATE
    # TABLE as the rest of this table's columns and so never needed a
    # migration of its own) -- same situation as place.rotates/active
    # above, an ALTER is required here too. Default to 1 (on), matching
    # announce_month_honors's own default: an operator who never visits
    # /api/admin/discord to turn one of these off keeps getting both new
    # kinds announced, the same "opt-out, not opt-in" behavior every
    # existing announcement kind already has.
    "ALTER TABLE discord_config ADD COLUMN announce_season_close INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE discord_config ADD COLUMN announce_place_activation INTEGER NOT NULL DEFAULT 1",
    # announce_weekly_recap: same "added after discord_config already
    # shipped, so an ALTER is required" situation as the two columns just
    # above -- see this column's own comment on the CREATE TABLE above
    # for what it replaced (announce_place_activation's old per-event
    # announcement) and why. Default 1 (on), same "opt-out, not opt-in"
    # reasoning as every other announcement kind.
    "ALTER TABLE discord_config ADD COLUMN announce_weekly_recap INTEGER NOT NULL DEFAULT 1",
    # announce_net_wrapup: same "added after discord_config already
    # shipped, so an ALTER is required" situation as the three columns
    # above -- see this column's own comment on the CREATE TABLE above.
    # Default 1 (on), same "opt-out, not opt-in" reasoning as every other
    # announcement kind.
    "ALTER TABLE discord_config ADD COLUMN announce_net_wrapup INTEGER NOT NULL DEFAULT 1",
    # guild_id / roles_enabled: app/discord_bot.py's Discord ROLE sync
    # feature, added after discord_config already shipped -- same
    # "an ALTER is required here too" situation as every column above.
    # guild_id is non-secret (a Discord guild id is just a number
    # visible to anyone in the server, same as a channel id) and is
    # seeded from DISCORD_GUILD_ID the same one-time,
    # guarded-by-updated_at way webhook_url/username/team_emoji already
    # are (see app/discord_notify.py's seed_discord_config_from_env()).
    # roles_enabled defaults to 0 (OFF) -- deliberately NOT the
    # "opt-out, not opt-in" default every announce_* column above uses:
    # this feature needs a bot token AND a guild id AND an operator to
    # have actually run "Create / repair team roles" before it can do
    # anything sensible, so it must never turn itself on the moment a
    # database happens to gain this column.
    "ALTER TABLE discord_config ADD COLUMN guild_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE discord_config ADD COLUMN roles_enabled INTEGER NOT NULL DEFAULT 0",
    # team_channels_enabled / team_category_name / team_category_id:
    # app/discord_bot.py's private-team-channels feature, added after
    # discord_config already shipped -- same "an ALTER is required here
    # too" situation as every column above. team_channels_enabled
    # defaults to 0 (OFF), same "must never turn itself on the moment a
    # database happens to gain this column" reasoning as roles_enabled's
    # own migration entry above. team_category_name defaults to the same
    # 'Teams' the CREATE TABLE default above uses. team_category_id is
    # nullable -- see discord_config's own comment on the CREATE TABLE
    # above for why (this app's own discovered-id bookkeeping, not an
    # operator-set value).
    "ALTER TABLE discord_config ADD COLUMN team_channels_enabled INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE discord_config ADD COLUMN team_category_name TEXT NOT NULL DEFAULT 'Teams'",
    "ALTER TABLE discord_config ADD COLUMN team_category_id TEXT",
    # discord_team_role.channel_id: the matching per-team migration for
    # the column discord_team_role's own CREATE TABLE comment above
    # describes -- nullable, same reasoning.
    "ALTER TABLE discord_team_role ADD COLUMN channel_id TEXT",
    # slash_enabled / app_id / public_key: app/discord_interactions.py's
    # HTTP-Interactions slash commands, added after discord_config
    # already shipped -- same "an ALTER is required here too" situation
    # as every column above. slash_enabled defaults to 0 (OFF), same
    # "must never turn itself on the moment a database happens to gain
    # this column" reasoning as roles_enabled's own migration entry
    # above -- see that column's own CREATE TABLE comment for why this
    # one especially must stay an explicit opt-in (Discord verifies the
    # endpoint URL the moment it is pasted into the developer portal).
    # app_id/public_key are non-secret and nullable-as-empty-string,
    # same shape as guild_id.
    "ALTER TABLE discord_config ADD COLUMN slash_enabled INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE discord_config ADD COLUMN app_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE discord_config ADD COLUMN public_key TEXT NOT NULL DEFAULT ''",
    # leaderboard_enabled / leaderboard_interval_seconds / leaderboard_top_n:
    # app/discord_leaderboard.py's pinned leaderboard, added after
    # discord_config already shipped -- same "an ALTER is required here
    # too" situation as every column above. Same defaults as the CREATE
    # TABLE above (see that column's own comment): off by default, a
    # 10-minute pass interval, top 5 per Top Operators list.
    "ALTER TABLE discord_config ADD COLUMN leaderboard_enabled INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE discord_config ADD COLUMN leaderboard_interval_seconds INTEGER NOT NULL DEFAULT 600",
    "ALTER TABLE discord_config ADD COLUMN leaderboard_top_n INTEGER NOT NULL DEFAULT 5",
]

PRAGMAS = [
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=15000",
    "PRAGMA foreign_keys=ON",
    "PRAGMA temp_store=MEMORY",
    # SIZED FOR THE WORLDWIDE SEED (2026-09-12). game.db was ~130 MB
    # until Places Worth Going went worldwide; it is now ~1.8 GB, almost
    # all of it `place` and `place_cell`. SQLite's default cache_size is
    # -2000, i.e. 2 MB per connection, which was invisible against a
    # 130 MB file and is hopeless against 1.8 GB: nearly every query
    # went to disk, and on a shared-CPU VPS reads piled up behind the
    # ingest loop's write transactions until they hit busy_timeout. That
    # is what made a page load take 15 seconds -- five API calls all
    # completing within 150 ms of each other at ~5.2 s, the signature of
    # requests serialized behind a lock rather than five slow queries.
    #
    # mmap_size does the heavy lifting because it is SHARED: the pages
    # live in the OS page cache once, however many connections are open.
    # cache_size is PER CONNECTION and connect() hands every coroutine
    # its own, so it stays deliberately modest -- 64 MB times a dozen
    # live connections is affordable on the 4 GB the container now has,
    # 256 MB times a dozen would not be.
    "PRAGMA mmap_size=1073741824",   # 1 GiB, shared via the OS page cache
    "PRAGMA cache_size=-65536",      # 64 MiB per connection
]

# In-process write lock. SQLite serializes writes at the file level, but
# this lock prevents BEGIN IMMEDIATE collisions across our own coroutines
# that go through WriteSession specifically (see that class below). Most
# of this codebase's OTHER writers (admin_ops.py, admin_api.py,
# nodes_api.py, join_api.py, mc_ingest.py, mqtt_subscriber.py,
# place_rotation.py, places_seed.py -- dozens of call sites) run their
# own manual BEGIN IMMEDIATE / COMMIT / ROLLBACK on a connection from
# connect() WITHOUT going through this lock at all. Those rely entirely
# on SQLite's own file-level locking (BEGIN IMMEDIATE + busy_timeout
# below) to serialize against each other and against WriteSession, which
# only works if every such caller genuinely holds a DISTINCT physical
# connection -- see the pool design below, which preserves that
# invariant on purpose rather than sharing one connection across
# concurrent callers.
_WRITE_LOCK = asyncio.Lock()


def _ensure_parent_dir(path: str) -> None:
    parent = Path(path).parent
    if str(parent):
        os.makedirs(parent, exist_ok=True)


# ---- connection pool --------------------------------------------------
#
# connect() used to open a brand new physical sqlite3 connection (full
# PRAGMA list re-run, including the mmap_size remap and a from-scratch
# 64 MiB page cache) for every single call, then the caller's own
# conn.close() would close it -- and closing the LAST connection to a
# WAL database triggers a checkpoint. Measured on prod with py-spy: that
# open+close cycle was ~22% of all real CPU work, and WriteSession's
# conn.close() alone (app/db.py's write path) was the single most
# expensive leaf function in the whole profile.
#
# The obvious fix -- one connection reused per thread -- is WRONG here:
# this app runs as a single uvicorn process with no --workers, i.e. one
# event-loop thread hosting every concurrently in-flight request
# coroutine. A thread-local connection would be shared by ALL of them,
# which breaks the invariant the write sites above actually depend on
# (distinct physical connections, serialized by SQLite's own file lock).
# Concretely: app/checkin_api.py's confirm_start/confirm_accept hold a
# connect()'d connection across a real `await` (an outbound HTTP fan-out
# to MeshCore-family connectors, via confirm_scan_all_connectors) and
# THEN run a manual BEGIN IMMEDIATE on it, outside _WRITE_LOCK. Under a
# shared connection, a second concurrent caller's BEGIN IMMEDIATE on
# that SAME connection either raises "cannot start a transaction within
# a transaction" instead of blocking/retrying, or -- worse -- silently
# becomes part of the first caller's transaction, so an unrelated
# ROLLBACK can erase writes a completely different request already
# believes succeeded. That is a real, load-bearing hazard, not a
# hypothetical: confirm_status/confirm_accept are player-facing,
# frequently-polled endpoints (its own docstring: "poll status every
# few seconds"), so two players' requests overlapping there is routine.
#
# So instead: a free list of IDLE, already-PRAGMA'd connections, with
# EXCLUSIVE borrowing. connect() pops one whole connection off the free
# list (or makes a fresh one if the list is empty) and hands it to
# exactly one caller; nobody else can see it until that caller's
# .close() releases it back. Two concurrent callers therefore always
# get two distinct physical connections, exactly as today -- the
# invariant above is preserved exactly, not weakened. What's eliminated
# is the repeated PRAGMA-and-remap cost of *opening* a connection, and
# the repeated checkpoint cost of *closing* the last reference to one:
# a connection that goes idle and comes back stays open the whole time.
# PRAGMA cache_size=-65536 is 64 MiB per live connection -- see PRAGMAS'
# own comment on why that number was sized against "a dozen" concurrent
# connections held by a SINGLE process (this was 16 before the web/
# worker role split -- docker-compose.yml's `meshwars`/`meshwars-worker`
# services, app/config.py's run_background_tasks). That single-process
# assumption is gone: a deployment now runs up to 4 processes sharing
# one game.db (3 `--workers` in the web role, plus 1 worker-role
# process), and _POOL_MAX bounds the IDLE free list PER PROCESS, so the
# steady-state ceiling this budget has to respect is now
# (this constant) * 64 MiB * (process count), not * 1. Left at 16, that
# steady-state ceiling would be 4 * 16 * 64 MiB = 4 GiB -- comfortably
# over CT 119's entire 4.0 GiB, before a single byte goes to Python,
# FastAPI, or the OS itself (see docker-compose.yml's mem_limit comment
# for that box's full budget). Lowered to 4 so the FLEET-WIDE steady-
# state ceiling stays exactly what it was before the split --
# 4 processes * 4 = 16 pooled connections total, same 16 this was
# already sized against, just divided across processes instead of piled
# into one. This bounds the IDLE list only, not concurrent usage: a
# burst of concurrent borrows beyond 4 still succeeds (connect() opens
# a fresh connection when the free list is empty -- see connect()
# below), it just isn't retained in the idle list afterward, so a burst
# does not permanently inflate a process's steady-state memory floor.
_POOL_MAX = 4
_POOL: collections.deque = collections.deque()
_POOL_LOCK = threading.Lock()  # plain, not asyncio: borrow/return must
                                # work from any thread (paho's callback
                                # thread, asyncio.to_thread workers, the
                                # event loop thread), and never blocks.


class _PoolEntry:
    """One idle, already-PRAGMA'd connection sitting in the free list,
    tagged with the db_path it was opened against."""

    __slots__ = ("conn", "db_path")

    def __init__(self, conn: sqlite3.Connection, db_path: str) -> None:
        self.conn = conn
        self.db_path = db_path


def _make_real_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,  # autocommit; we manage txns explicitly
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    for pragma in PRAGMAS:
        conn.execute(pragma)
    return conn


def _probe_alive(conn: sqlite3.Connection) -> bool:
    """Cheap liveness check for a connection that has been sitting idle
    in the free list. A pooled connection must never turn a transient
    fault (the underlying file handle going bad, the process having
    fork()'d, whatever) into a permanent one for the rest of the
    process's life -- so a dead connection found here is discarded, not
    handed to the caller."""
    try:
        conn.execute("SELECT 1")
        return True
    except Exception:
        return False


class _PooledConnectionProxy:
    """What connect() actually returns for a pooled borrow: a thin
    wrapper around one real, exclusively-owned sqlite3.Connection.

    Every attribute/method other than close() delegates straight
    through to the real connection via __getattr__, so this is
    source-compatible with plain sqlite3.Connection for every one of
    this module's ~100 call sites (none of which do `with conn:`,
    isinstance(conn, sqlite3.Connection), or touch a dunder on conn
    itself -- confirmed by inspection before this change landed;
    they all just call .execute/.executemany/.executescript/.commit/
    .rollback/.close and read .in_transaction).

    close() does NOT close the underlying connection -- it RELEASES
    it back to the free list for the next borrower. See
    _release_connection().
    """

    __slots__ = ("_real", "_db_path", "_released")

    def __init__(self, real: sqlite3.Connection, db_path: str) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_db_path", db_path)
        object.__setattr__(self, "_released", False)

    def close(self) -> None:
        # Idempotent ON PURPOSE: a double close() must never push the
        # same real connection onto the free list twice -- that would
        # hand one physical connection to two concurrent borrowers,
        # exactly the bug this whole design exists to prevent.
        if self._released:
            return
        object.__setattr__(self, "_released", True)
        _release_connection(self._real, self._db_path)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        setattr(self._real, name, value)


def _release_connection(conn: sqlite3.Connection, db_path: str) -> None:
    try:
        if conn.in_transaction:
            # Non-negotiable: a leaked open transaction would poison
            # the next borrower -- their first statement would silently
            # execute as part of THIS caller's half-finished write.
            conn.rollback()
    except Exception:
        # The connection is unusable for some other reason (dead
        # handle, etc.) -- never hand a poisoned connection back to the
        # free list. Best-effort close and drop it; the next connect()
        # call just opens a fresh one.
        try:
            conn.close()
        except Exception:
            pass
        return

    with _POOL_LOCK:
        if len(_POOL) < _POOL_MAX:
            _POOL.append(_PoolEntry(conn, db_path))
            return
    # Over the cap -- close it for real, outside the lock (closing
    # doesn't need it, and there's no reason to hold the lock while a
    # WAL checkpoint potentially runs).
    conn.close()


def _drain_pool_for_tests() -> None:
    """Test-only: close and clear every idle connection in the free
    list. The pool is process-global state, so without this a
    connection opened (and PRAGMA'd, or tagged with a db_path) under
    one test could be handed to a later test via the free list --
    see tests/conftest.py's autouse fixture, which calls this between
    every test."""
    with _POOL_LOCK:
        entries = list(_POOL)
        _POOL.clear()
    for entry in entries:
        try:
            entry.conn.close()
        except Exception:
            pass


def connect(pooled: bool = True) -> sqlite3.Connection:
    """Borrow a connection. Each concurrent caller gets its own
    EXCLUSIVE physical connection -- see the pool design comment above
    for why that invariant is preserved, not weakened, by pooling.

    pooled=False is the escape hatch for a caller that deliberately
    wants a private, unpooled connection whose .close() really closes
    (and, in WAL mode, really checkpoints) -- e.g.
    app/mqtt_subscriber.py's self._own_conn, which is intentionally
    long-lived on paho's own callback thread and closed on disconnect
    for exactly that checkpoint side effect. Do not add pooled=False
    anywhere else without the same kind of deliberate reasoning: every
    other call site in this codebase is fine (and faster) pooled.
    """
    current_path = settings.db_path

    if not pooled:
        return _make_real_connection(current_path)

    while True:
        with _POOL_LOCK:
            try:
                entry = _POOL.popleft()
            except IndexError:
                entry = None

        if entry is None:
            real = _make_real_connection(current_path)
            break

        if entry.db_path != current_path:
            # Stale: this idle connection points at a different
            # database file than settings.db_path names right now (the
            # test suite monkeypatches db_path per test) -- never hand
            # a borrower a connection to the wrong file. Close it for
            # real and try the next idle entry.
            try:
                entry.conn.close()
            except Exception:
                pass
            continue

        if not _probe_alive(entry.conn):
            try:
                entry.conn.close()
            except Exception:
                pass
            continue

        real = entry.conn
        break

    return _PooledConnectionProxy(real, current_path)


def _migrate_session_privacy(conn: sqlite3.Connection) -> None:
    """One-time cleanup for account_session's privacy-hardening pass --
    see that table's own SCHEMA comment above for the decision this
    implements. Called from init_db() itself (see the call site's own
    comment for why this cannot be a plain MIGRATIONS entry).

    Gate: PRAGMA table_info tells us directly whether this database
    still carries the old `ip` column. If it does not -- either because
    this database was created fresh under the current SCHEMA (which
    never had `ip` at all), or because a previous boot already ran this
    function to completion -- there is nothing to do, and every later
    boot after the first migrated one is a true no-op rather than a
    re-run of the (non-idempotent) label reduction below.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(account_session)")}
    if "ip" not in cols:
        return  # nothing to migrate -- fresh install, or already done

    log.info("account_session privacy migration: starting (reducing user_agent to device labels, clearing ip)")

    # Reduce every existing row's raw User-Agent to its short device
    # label BEFORE the column is renamed below, so the UPDATE below
    # still addresses it by its original name. Matt's intent here was
    # explicit: today's already-stored raw strings must not linger just
    # because they predate this change -- a migration that only alters
    # future INSERTs would leave every historical row exactly as
    # identifying as before.
    rows = conn.execute("SELECT token_hash, user_agent FROM account_session").fetchall()
    for row in rows:
        label = device_label_from_user_agent(row["user_agent"])
        conn.execute(
            "UPDATE account_session SET user_agent = ? WHERE token_hash = ?",
            (label, row["token_hash"]),
        )

    # user_agent -> device_label: a rename, not a new column, since
    # every row above was just rewritten to already hold a label, not a
    # raw UA -- the column's CONTENTS changed meaning, so its name
    # should too, matching this codebase's general preference for
    # naming a column for exactly what it holds. ALTER TABLE ... RENAME
    # COLUMN has been supported since SQLite 3.25 (2018); this
    # deployment runs 3.45.1 (checked via sqlite3.sqlite_version at
    # development time), well past that floor.
    conn.execute("ALTER TABLE account_session RENAME COLUMN user_agent TO device_label")

    # ip: physically DROPPED, not blanked. SQLite's ALTER TABLE ... DROP
    # COLUMN has been supported since 3.35 (2021), and this deployment's
    # 3.45.1 comfortably clears that floor; `ip` is a plain nullable
    # TEXT column with no index, foreign key, or CHECK constraint
    # referencing it (idx_account_session_account is keyed on
    # account_id only), which is exactly the shape SQLite's DROP COLUMN
    # handles as a metadata-only change, no table rebuild required. An
    # UPDATE ... SET ip = NULL was considered instead and rejected:
    # Matt's decision was "not stored," full stop, and a column that
    # still exists -- still enumerated by `SELECT *`, still visible in
    # every future PRAGMA table_info -- is a standing invitation for a
    # future change to start writing to it again "since it's already
    # there." Dropping it removes that temptation along with the data.
    conn.execute("ALTER TABLE account_session DROP COLUMN ip")

    log.info("account_session privacy migration: complete (%d row(s) reduced, ip column dropped)", len(rows))


def _migrate_freqmapper_verification_verification_id(conn: sqlite3.Connection) -> None:
    """One-time, idempotent migration: freqmapper_verification.event_id
    (the prefixed dedupe key -- "verified_tx:<uuid>" / "passive_rx:
    <uuid>" -- commit d114a5a's combined-feed cutover briefly shipped)
    reverts to .verification_id, the table's original shape, holding
    each event's own type-appropriate id UNPREFIXED -- see
    freqmapper_verification's own comment on the CREATE TABLE above and
    app/freqmapper_ingest.py's module docstring for the full incident
    story this undoes.

    WHY THIS EXISTS: d114a5a believed the combined feed's dedup key had
    to change shape, and migrated this table to match. It did not --
    verified against the live API, a verified_tx event's own
    `verification_id` field already holds the exact same UUID as the
    bare half of its `event_id`, so this table never needed to change
    at all, and the incident that migration was meant to prevent had an
    entirely different, unrelated cause (a cursor cutover that
    legitimately started reading FreqMapper history this deployment had
    genuinely never ingested before -- see this module's docstring, not
    a dedupe-key mismatch). d114a5a was rolled back in production
    (eb15040) before ever running this migration there for real, but it
    DID run against preview, and would have run against any operator's
    database that deployed d114a5a even briefly -- so every deployment
    is not guaranteed to be in the same shape, and this migration exists
    purely to converge all of them back onto one, not to fix the
    incident (see app/freqmapper_ingest.py's high-water-mark guard for
    the actual fix).

    Two starting shapes this has to handle:

      1. Never ran d114a5a's migration at all (never deployed that
         commit, or deploys this revert first): already has
         `verification_id`. Nothing to do.
      2. DID run d114a5a's migration (preview right now; any operator
         who deployed d114a5a even briefly): has `event_id`, holding a
         mix of "verified_tx:<uuid>" and "passive_rx:<uuid>" rows.

    For shape 2: every "verified_tx:" row is unprefixed back to its bare
    UUID and KEPT -- per the reasoning above, that bare UUID is exactly
    what `verification_id` would already hold, and losing this dedup
    history would risk re-painting that event the next time the
    combined feed's cursor happens to revisit it, exactly the failure
    this whole table exists to prevent. Every "passive_rx:" row is
    DELETED outright rather than unprefixed and kept -- at the time
    d114a5a ran (and at the time this migration was written), passive
    RX had never painted anything yet (RX painting is a later addition
    -- see app/freqmapper_ingest.py's module docstring, "Passive RX
    painting"), so there was nothing for its dedup history to protect,
    and keeping it would put a value from
    `reception_id`'s own separate UUID space into a column that is once
    again named, and reasoned about everywhere else in this codebase, as
    pure verification_id space -- a latent, silent way for an old RX
    observation to mask a genuinely new verified_tx event that happens
    to land on the same value. Deleting is strictly safer than
    converting here, unlike the verified_tx case just above. A row
    matching neither prefix (an unrecognized-event-type row -- see
    _process_one_event's own fallback dedup for that case) is left
    exactly as it was; this migration has no more specific field name to
    convert it to than the one it already holds.

    Gate: PRAGMA table_info, the same shape-based gate
    _migrate_session_privacy above and d114a5a's own (now-removed)
    migration both used, for the same reason: a plain ALTER TABLE ...
    RENAME COLUMN is not safe to blindly re-run every boot the way the
    plain-SQL MIGRATIONS list below is (a second run would fail with
    "no such column: event_id", which is not one of the "already
    applied" errors that loop knows how to swallow -- see init_db's own
    comment on that loop). A fresh install's SCHEMA above already
    creates the table with `verification_id` directly, so PRAGMA
    table_info never finds `event_id` there and this is a true no-op
    for it too, same as for a database that has already run this
    migration once.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(freqmapper_verification)")}
    if "event_id" not in cols:
        return  # never ran d114a5a's migration, or already reverted

    log.info(
        "freqmapper_verification migration: reverting event_id -> verification_id "
        "(stripping verified_tx: prefixes, dropping passive_rx: rows)"
    )

    # Drop passive_rx rows BEFORE the unprefix/rename below -- see this
    # function's own docstring for why these are deleted rather than
    # converted. WHERE guard (LIKE, not a bare equality) makes this
    # idempotent on its own, belt and braces alongside the column-shape
    # gate above: a hypothetical second pass over a not-yet-renamed
    # table would simply find nothing left to delete.
    conn.execute("DELETE FROM freqmapper_verification WHERE event_id LIKE 'passive_rx:%'")
    # substr(...) rather than a second LIKE-guarded UPDATE loop: every
    # remaining "verified_tx:" row is stripped back to its bare UUID in
    # one pass. WHERE guard makes this idempotent too, same reasoning as
    # the DELETE just above.
    conn.execute(
        "UPDATE freqmapper_verification SET event_id = substr(event_id, length('verified_tx:') + 1)"
        " WHERE event_id LIKE 'verified_tx:%'"
    )
    conn.execute("ALTER TABLE freqmapper_verification RENAME COLUMN event_id TO verification_id")

    log.info("freqmapper_verification migration: revert complete")


def init_db() -> None:
    """Create schema and apply pragmas. Idempotent."""
    _ensure_parent_dir(settings.db_path)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        # Idempotent schema patches for existing DBs. Only "this was
        # already applied" is expected here (re-adding a column that
        # exists already) and is silently skipped -- anything else is a
        # real failure and must be loud, not swallowed, since the
        # application would otherwise start up against a schema the
        # code does not expect it to have.
        for stmt in MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "duplicate column name" in msg or "already exists" in msg:
                    continue  # already applied, nothing to do
                log.error("schema patch failed: %s -- statement: %s", e, stmt)
                raise

        # account_session privacy migration (see that table's own SCHEMA
        # comment above for the full story): not a plain MIGRATIONS
        # entry because it is not a plain SQL statement -- reducing an
        # existing raw User-Agent to a device label requires
        # app/device_label.py's parser, which no ALTER/UPDATE can run
        # for us. Unlike the MIGRATIONS loop above (idempotent by
        # re-running harmlessly every boot) and the places_seed/
        # checkin/freqmapper bootstraps below (idempotent because
        # re-seeding already-present data is a no-op), this operation
        # is NOT safe to blindly re-run: applying the label parser to
        # its own output is not a no-op (a label like "Chrome on
        # Windows" contains no "Chrome/" token, so re-parsing it
        # produces "Unknown device" -- see device_label.py's own
        # comment on why "Version/"+"Safari/" etc. are required
        # together). _migrate_session_privacy() is therefore gated on
        # SCHEMA SHAPE, not re-run unconditionally: it checks whether
        # this database still has the old `ip` column and only does
        # anything if so, making repeated boots against an
        # already-migrated database (or a fresh one, which the SCHEMA
        # above already creates in the new shape) a true no-op. Left
        # unguarded by try/except, unlike the non-fatal seeds below:
        # this changes the table's actual columns, so a failure here
        # must stop boot loudly rather than let the app start up
        # against a schema app/sessions.py does not expect.
        _migrate_session_privacy(conn)

        # freqmapper_verification's event_id -> verification_id revert
        # (see that function's own docstring for the full story of why
        # d114a5a's migration is being undone here) -- unguarded by
        # try/except for the same reason _migrate_session_privacy is
        # just above: this changes the table's actual columns and its
        # dedup keys, and a failure here must stop boot loudly rather
        # than let the app start up against a half-migrated table.
        _migrate_freqmapper_verification_verification_id(conn)

        # Startup WRITES -- gated on settings.run_background_tasks
        # (app/config.py). Schema creation and the MIGRATIONS loop and
        # the two migration functions above are NOT in this block: every
        # process needs a fully migrated schema before it can serve a
        # single request, and that DDL is idempotent, so every process
        # running it redundantly is free. Everything below is different:
        # each of these either spawns a background thread of its own
        # (places-seed) or performs an INSERT/UPDATE bootstrap
        # (checkin/freqmapper/discord config) that is only meant to run
        # ONCE per boot fleet-wide, not once per process. With N web
        # workers plus a worker process all calling init_db() (every
        # process does, unconditionally, just above this block), leaving
        # these ungated would mean N+1 places-seed threads all loading
        # the same ~2M rows concurrently, and N+1 processes racing the
        # same one-time config bootstrap INSERTs -- wasted work at best,
        # a lock-contention pile-up at worst. Exactly one process (the
        # dedicated worker) should have run_background_tasks=True.
        if settings.run_background_tasks:
            # Places Worth Going seed (app/places_seed.py): reference
            # data shipped with the code, same as app/reference/
            # places.csv, but loaded into `place`/`place_cell` rather
            # than kept in memory -- see that module's docstring for
            # why. Imported here rather than at module level to avoid a
            # circular import (places_seed does not import this module
            # back, but keeping the import local keeps db.py's own
            # import graph exactly what it was before this landed).
            #
            # BACKGROUNDED (2026-09-07), not awaited here: a first load
            # of the worldwide seed measured ~200s, and this function
            # runs inside FastAPI's lifespan startup (app/main.py),
            # which blocks uvicorn from accepting ANY connection --
            # including /health -- until it returns. A slow load here
            # meant a slow or, past mw-deploy's 180s health-check
            # timeout, outright FAILED deploy, for a feature that
            # degrades fine without its data for a few minutes
            # (app/places_api.py's routes just return no markers until
            # the load finishes -- nothing crashes on an empty `place`
            # table). Runs against its own fresh connection, not the
            # `conn` this function is using: sqlite3 connections are
            # not safe to hand to another thread while this one keeps
            # using them, and `connect()` is already how every other
            # request-serving codepath gets its own (see that
            # function's docstring, "each coroutine should grab its
            # own"). A failure here must not take the whole app down --
            # the place tables just stay empty (or stale) and the
            # places feature quietly has no data, logged loudly, rather
            # than the server failing to boot (or, now, failing to ever
            # finish this background load) over a reference-data
            # problem.
            def _load_places_seed_background() -> None:
                from .places_seed import load_places_seed
                seed_conn = connect()
                try:
                    load_places_seed(seed_conn)
                except Exception:
                    log.exception("places_seed: background load failed -- places feature will have no/stale data")
                finally:
                    seed_conn.close()

            threading.Thread(
                target=_load_places_seed_background, name="places-seed-load", daemon=True
            ).start()

            # Net check-ins (app/checkin.py): one-time bootstrap of
            # checkin_net/checkin_config from settings.py, so a database
            # that has never had a net row gets exactly today's
            # production behavior reproduced as DB rows, and every
            # later boot is a no-op. Local import, same reason and same
            # pattern as places_seed just above (checkin.py imports
            # WriteSession from this module, so importing it back at
            # module load time here would close a cycle; importing it
            # inside this already-running function does not, since by
            # the time init_db() is called this module has finished
            # executing). Non-fatal for the same reason places_seed's
            # failure is non-fatal: a check-in feature with no nets
            # configured is a quiet, recoverable state (an operator can
            # always add nets through the admin API), not a reason to
            # refuse to serve the rest of the site.
            try:
                from .checkin import seed_nets_from_env
                seed_nets_from_env(conn)
            except Exception:
                log.exception("checkin: seed_nets_from_env failed -- check-in nets may be empty")

            # FreqMapper connector config (app/freqmapper_ingest.py):
            # the same one-time bootstrap shape as seed_nets_from_env
            # just above, migrating settings.py's freqmapper_*/
            # mt_paint_source values onto the freqmapper_config
            # singleton so an operator can edit them through
            # app/admin_ops.py's /api/admin/paint without a restart.
            # Local import, same circular-import reason as checkin.py's
            # own import just above (freqmapper_ingest.py imports
            # WriteSession from this module).
            try:
                from .freqmapper_ingest import seed_freqmapper_config_from_env
                seed_freqmapper_config_from_env(conn)
            except Exception:
                log.exception("freqmapper: seed_freqmapper_config_from_env failed -- config may be unseeded")

            # Discord announcements (app/discord_notify.py): the same
            # one-time bootstrap shape as seed_freqmapper_config_from_env
            # just above, migrating settings.py's discord_webhook_*/
            # discord_team_emoji values onto the discord_config
            # singleton so an operator can edit them through
            # app/admin_ops.py's /api/admin/discord without a restart
            # or an env-var edit. Local import, same circular-import
            # reason as freqmapper_ingest.py's own import just above
            # (discord_notify.py imports WriteSession from this
            # module). Added after this whole block was first written
            # around just places-seed/checkin/freqmapper -- gated here
            # for the identical reason those are: it is the same
            # one-time-per-fleet INSERT/UPDATE bootstrap shape, not a
            # per-process concern.
            try:
                from .discord_notify import seed_discord_config_from_env
                seed_discord_config_from_env(conn)
            except Exception:
                log.exception("discord: seed_discord_config_from_env failed -- config may be unseeded")
    finally:
        conn.close()


@contextmanager
def write_txn(conn: sqlite3.Connection):
    """A short IMMEDIATE write transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


async def with_write_lock():
    """Context manager helper: `async with with_write_lock(): ...`"""
    return _WRITE_LOCK


# Async-compatible wrapper using the global write lock.
class WriteSession:
    """`async with WriteSession() as conn:` -> connection inside the global lock."""

    def __init__(self):
        self.conn: sqlite3.Connection | None = None

    async def __aenter__(self) -> sqlite3.Connection:
        await _WRITE_LOCK.acquire()
        # Invariant: from here to __aexit__, the lock is held only once
        # BEGIN IMMEDIATE has actually succeeded. Python only calls
        # __aexit__ when __aenter__ returns, so if connect() or BEGIN
        # IMMEDIATE raises -- a busy database past the pragma's
        # busy_timeout, a bad db_path, a cancellation, anything -- we
        # must release the lock and close whatever connection we opened
        # ourselves, right here, or the lock is held forever and every
        # write anywhere in the process deadlocks behind it. Catching
        # BaseException (not Exception) matters because this is asyncio
        # code: a task cancellation must not leak the lock either.
        try:
            self.conn = connect()
            self.conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            if self.conn is not None:
                self.conn.close()
                self.conn = None
            _WRITE_LOCK.release()
            raise
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
        finally:
            self.conn.close()
            _WRITE_LOCK.release()


def get_cursor(conn: sqlite3.Connection, k: str, default: str = "") -> str:
    row = conn.execute("SELECT v FROM cursor WHERE k = ?", (k,)).fetchone()
    return row["v"] if row else default


def set_cursor(conn: sqlite3.Connection, k: str, v: str) -> None:
    conn.execute(
        "INSERT INTO cursor(k,v) VALUES(?,?) "
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (k, v),
    )
