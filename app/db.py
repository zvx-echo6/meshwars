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

-- Individual position samples (8-char geohash) for the existing /get-samples endpoint.
-- Retained for the current season only; cleared on season transition.
CREATE TABLE IF NOT EXISTS sample (
    season_id       INTEGER NOT NULL,
    sample_hash     TEXT NOT NULL,           -- 8-char geohash
    sender_node_id  INTEGER NOT NULL,
    ts              INTEGER NOT NULL,
    snr             REAL,
    rssi            REAL,
    path_json       TEXT NOT NULL DEFAULT '[]',
    observed        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (season_id, sample_hash, sender_node_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_sample_season ON sample(season_id);

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

-- Explicit MeshCore display-name -> player binding. This is the
-- LAST-RESORT fallback in app/checkin.py's identity resolution for
-- MeshCore check-ins -- the primary, normal-case path resolves a
-- player's already-bound radio contact (player_node, protocol='mc',
-- however it got bound: MeshMapper's wardriving auto-bind, typed into
-- POST /api/nodes by hand, or picked from app/checkin_api.py's
-- directory picker -- all three write the identical row shape and this
-- table plays no part in any of them) through the live.mwmesh.com
-- public-key directory automatically. A row here only matters for a
-- player whose public key has never shown up in that directory at all
-- -- key-based resolution wins wherever it produces an answer, so a
-- binding here is IGNORED, not consulted, the moment the bridge
-- resolves that player some other way. See app/checkin.py's module
-- docstring for the full priority story.
--
-- Same first-claim-wins conflict semantics as player_node's radio
-- binding, but keyed on a free-text display name instead of an 8-hex
-- node reference -- player_node.node_ref is validated as exactly that
-- (app/node_ref.py) and a MeshCore display name has no fixed shape at
-- all, so it cannot live in that table. UNIQUE on player_id (which
-- player_node does NOT have on player_id) because unlike radios -- a
-- player can own several -- a player has at most one checked-in name
-- here; app/checkin_api.py's POST is "set", not "add", enforced by
-- this constraint.
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
    # ALTER, unlike mc_checkin_binding/mc_checkin_award/
    # mc_checkin_seen_message above (those are brand new tables, so
    # CREATE TABLE IF NOT EXISTS in SCHEMA already covers them). Kept as
    # its own column rather than folded into `tiles` so a closed
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
