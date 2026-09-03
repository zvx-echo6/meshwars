"""SQLite schema, connection, and migrations.

Uses WAL mode so HTTP read endpoints can serve concurrently with the
single writer (the poll loop / scheduler).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
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
CREATE TABLE IF NOT EXISTS player_cell_ping (
    player_id       INTEGER NOT NULL,
    protocol        TEXT NOT NULL,
    cell_id         TEXT NOT NULL,
    ts              INTEGER NOT NULL,
    seen_at         INTEGER NOT NULL,
    precision_bits  INTEGER,
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
    PRIMARY KEY (player_id, protocol, day)
);

-- One row per FreqMapper verified-coverage event ever processed
-- (app/freqmapper_ingest.py). verification_id is that event's whole
-- identity -- a stable UUID FreqMapper itself assigns, one per event,
-- never reused -- so this is a pure dedup table: INSERT OR IGNORE on the
-- primary key means an event already seen (a page re-fetched after a
-- restart before the cursor was persisted, a retry, an overlapping
-- page) is a no-op rather than a re-processed, re-scored event. Recorded
-- for EVERY event that reaches this check, regardless of whether the
-- radio turns out to be registered or in bounds, or which source is
-- currently painting the Meshtastic board (settings.mt_paint_source) --
-- this table's only job is "have we ever looked at this specific event
-- before," not "did it score." Pruned well past FreqMapper's own paging
-- window by app/freqmapper_ingest.py's own housekeeping, the same
-- reasoning app/mc_ingest.py's retention windows use, so this cannot
-- grow without bound on a long-running deployment.
CREATE TABLE IF NOT EXISTS freqmapper_verification (
    verification_id TEXT PRIMARY KEY,
    seen_at          INTEGER NOT NULL
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
-- about which one is currently allowed to paint the Meshtastic board
-- (exactly one at a time -- see that column's own comment in
-- config.py, kept as this table's authoritative copy now).
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
CREATE TABLE IF NOT EXISTS freqmapper_config (
    id                     INTEGER PRIMARY KEY CHECK (id = 1),
    mt_paint_source        TEXT NOT NULL DEFAULT 'meshview',
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
    updated_at             INTEGER NOT NULL DEFAULT 0
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
CREATE TABLE IF NOT EXISTS mc_tile (
    season_id       INTEGER NOT NULL,
    cell_id         TEXT NOT NULL,
    owner_team      TEXT NOT NULL,
    last_player_id  INTEGER NOT NULL,
    last_report_ts  INTEGER NOT NULL,
    paint_count     INTEGER NOT NULL DEFAULT 0,
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

-- One row per login session, active or historical. token_hash is a
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
-- sliding expiry.
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
    # to it.
    "ALTER TABLE account ADD COLUMN contact_email TEXT",
    "ALTER TABLE account ADD COLUMN contact_email_verified_at INTEGER",
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
]

PRAGMAS = [
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
    "PRAGMA temp_store=MEMORY",
]

# In-process write lock. SQLite serializes writes at the file level, but
# this lock prevents BEGIN IMMEDIATE collisions across our own coroutines.
_WRITE_LOCK = asyncio.Lock()


def _ensure_parent_dir(path: str) -> None:
    parent = Path(path).parent
    if str(parent):
        os.makedirs(parent, exist_ok=True)


def connect() -> sqlite3.Connection:
    """Open a fresh connection. Each coroutine should grab its own."""
    conn = sqlite3.connect(
        settings.db_path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,  # autocommit; we manage txns explicitly
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    for pragma in PRAGMAS:
        conn.execute(pragma)
    return conn


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

        # Places Worth Going seed (app/places_seed.py): reference data
        # shipped with the code, same as app/reference/places.csv, but
        # loaded into `place`/`place_cell` rather than kept in memory --
        # see that module's docstring for why. Imported here rather than
        # at module level to avoid a circular import (places_seed does
        # not import this module back, but keeping the import local
        # keeps db.py's own import graph exactly what it was before this
        # landed). A failure here must not take the whole app down --
        # the place tables just stay empty (or stale) and the places
        # feature quietly has no data, logged loudly, rather than the
        # server failing to boot over a reference-data problem.
        try:
            from .places_seed import load_places_seed
            load_places_seed(conn)
        except Exception:
            log.exception("places_seed: load failed -- places feature will have no/stale data")

        # Net check-ins (app/checkin.py): one-time bootstrap of
        # checkin_net/checkin_config from settings.py, so a database that
        # has never had a net row gets exactly today's production
        # behavior reproduced as DB rows, and every later boot is a
        # no-op. Local import, same reason and same pattern as
        # places_seed just above (checkin.py imports WriteSession from
        # this module, so importing it back at module load time here
        # would close a cycle; importing it inside this already-running
        # function does not, since by the time init_db() is called this
        # module has finished executing). Non-fatal for the same reason
        # places_seed's failure is non-fatal: a check-in feature with no
        # nets configured is a quiet, recoverable state (an operator can
        # always add nets through the admin API), not a reason to refuse
        # to serve the rest of the site.
        try:
            from .checkin import seed_nets_from_env
            seed_nets_from_env(conn)
        except Exception:
            log.exception("checkin: seed_nets_from_env failed -- check-in nets may be empty")

        # FreqMapper connector config (app/freqmapper_ingest.py): the
        # same one-time bootstrap shape as seed_nets_from_env just
        # above, migrating settings.py's freqmapper_*/mt_paint_source
        # values onto the freqmapper_config singleton so an operator can
        # edit them through app/admin_ops.py's /api/admin/paint without
        # a restart. Local import, same circular-import reason as
        # checkin.py's own import just above (freqmapper_ingest.py
        # imports WriteSession from this module).
        try:
            from .freqmapper_ingest import seed_freqmapper_config_from_env
            seed_freqmapper_config_from_env(conn)
        except Exception:
            log.exception("freqmapper: seed_freqmapper_config_from_env failed -- config may be unseeded")
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
