"""SQLite schema, connection, and migrations.

Uses WAL mode so HTTP read endpoints can serve concurrently with the
single writer (the poll loop / scheduler).
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import settings

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
"""



MIGRATIONS = [
    "ALTER TABLE tile ADD COLUMN last_packet_id INTEGER",
    "ALTER TABLE tile_unique_painter ADD COLUMN paint_count INTEGER NOT NULL DEFAULT 1",
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
        # Idempotent column-add migrations for existing DBs
        for stmt in MIGRATIONS:
            try:
                conn.execute(stmt)
            except Exception:
                pass  # column already exists
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
