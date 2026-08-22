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
CREATE TABLE IF NOT EXISTS player_cell_ping (
    player_id  INTEGER NOT NULL,
    protocol   TEXT NOT NULL,
    cell_id    TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    seen_at    INTEGER NOT NULL,
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
"""



MIGRATIONS = [
    # Nullable on purpose, both of them: every award written before these
    # columns existed genuinely has no value to backfill. message_ts is
    # not recoverable at all -- the check-in feed only serves its newest
    # 100 messages, so a net that passed without this column can never
    # have its posting times reconstructed. streak COULD be derived from
    # net_date history, but is left null rather than invented so a row
    # always says whether the value was recorded or inferred.
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
        self.conn = connect()
        self.conn.execute("BEGIN IMMEDIATE")
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
