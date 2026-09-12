"""Tests for the FreqMapper backfill guard -- the actual fix for the
2026-09-08 incident (see app/freqmapper_ingest.py's module docstring,
"THE 2026-09-08 INCIDENT, AND WHAT ACTUALLY CAUSED IT" / "THE ACTUAL
FIX"). Commit d114a5a's combined-feed cutover shipped believing an
unnecessary dedupe-key schema migration would prevent history from
re-painting the live board; it was rolled back after doing exactly
that. The real cause was a cursor cutover legitimately handing this
deployment weeks of coverage it had genuinely never ingested before --
something no amount of dedupe-key correctness could ever have caught,
since dedup only ever answers "have I processed this exact event
before," never "is this event old."

This file is organized around the three independent correctness
properties that make up the actual fix:

  1. The dedupe-key REVERT (app/db.py's
     _migrate_freqmapper_verification_verification_id) -- converging any
     deployment that ran d114a5a's now-removed migration (preview right
     now; any operator who deployed d114a5a even briefly) back onto the
     original, never-actually-necessary verification_id shape, and
     proving dedup itself now reads verification_id directly with no
     prefixed string ever constructed or parsed
     (test_migrate_reverts_*, test_duplicate_verification_id_no_prefix).
  2. The high-water-mark backfill guard itself (_process_one_event in
     app/freqmapper_ingest.py) -- an event older than the stored mark
     and genuinely unseen is recorded (so it is never re-evaluated) but
     never painted; an event at or after the mark paints and advances
     it; a fresh deployment with no mark yet ingests normally and seeds
     one; and an operator can deliberately opt out via
     freqmapper_config.allow_backfill (test_backfill_guard_*,
     test_fresh_install_*, test_allow_backfill_*).
  3. Seeding the mark on an UPGRADING deployment
     (_maybe_seed_high_water_mark) -- the gap in an earlier version of
     this guard: a deployment that already has FreqMapper history (every
     real one, including preview) must have the mark seeded from that
     history, backed off by a grace window, BEFORE the guard ever
     evaluates an event -- not from whatever event happens to arrive
     first, which on a brand-new combined-feed cursor is the OLDEST
     event in FreqMapper's history, reproducing the incident with the
     guard installed and silently doing nothing
     (test_seed_from_history_*, test_seed_grace_window_*,
     test_seed_fresh_install_*, test_seed_then_allow_backfill_*).

Sections 1-2 use the shared in-memory `conn` fixture (tests/conftest.py)
and call FreqMapperIngestor._process_one_event directly, the same shape
tests/test_freqmapper_combined_feed.py already uses for anything that
does not need the HTTP/poll-loop layer. Section 3 needs a real
file-backed database (db_path fixture, same shape as
tests/test_freqmapper_combined_feed.py's own) because
_maybe_seed_high_water_mark always goes through app.db.connect()/
WriteSession, both of which always open settings.db_path -- there is no
way to hand it an in-memory connection directly.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime, timezone

import pytest

import app.db as db
from app import mc_scoring
from app.config import settings
from app.db import MIGRATIONS, SCHEMA, get_cursor, set_cursor
from app.freqmapper_ingest import (
    _BACKFILL_SEED_GRACE_SECONDS,
    FreqMapperIngestor,
    HIGH_WATER_MARK_KEY,
)

NOW = int(time.time())
PROTOCOL = "mt"
LAT, LON = 43.0, -116.0  # well within settings.play_area_* (see app/config.py)


# ---------------------------------------------------------------------
# fixtures / helpers -- same shapes as tests/test_freqmapper_combined_feed.py
# ---------------------------------------------------------------------

def _seed_player_and_node(conn, player_id=1, node_ref="0a0a0a0a", team="RED"):
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?, ?, ?, ?)",
        (player_id, f"player-{player_id}", team, NOW),
    )
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) VALUES (?, ?, ?, ?)",
        (PROTOCOL, node_ref, player_id, NOW),
    )


def _season_id(conn) -> int:
    conn.execute("BEGIN IMMEDIATE")
    mc_scoring.maybe_roll_season(conn, NOW, PROTOCOL)
    sid = mc_scoring.ensure_active_season(conn, NOW, PROTOCOL)
    conn.execute("COMMIT")
    return sid


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _tx_event(raw_id: str, occurred_at_ts: int, node_ref: str = "0a0a0a0a") -> dict:
    return {
        "event_id": f"verified_tx:{raw_id}",
        "event_type": "verified_tx",
        "verification_id": raw_id,
        "radio_node_id": "!" + node_ref,
        "latitude": LAT,
        "longitude": LON,
        "occurred_at": _iso(occurred_at_ts),
    }


def _process(conn, ingestor, event, season_id, registered, *, allow_backfill=False,
             paint_from="2020-01-01"):
    return ingestor._process_one_event(
        conn, event, season_id, registered, NOW,
        "both", 1.0, 0.5, paint_from,
        allow_backfill=allow_backfill,
    )


# ---------------------------------------------------------------------
# 1. The dedupe-key revert (app/db.py)
# ---------------------------------------------------------------------

def _d114a5a_shape_freqmapper_db(tmp_path) -> str:
    """A standalone sqlite file with freqmapper_verification in the
    shape d114a5a's now-removed migration left it in -- `event_id`
    holding prefixed verified_tx: and passive_rx: rows, exactly what
    preview, or any operator who deployed d114a5a even briefly, has on
    disk right now.
    """
    path = str(tmp_path / "d114a5a_shape_fm.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE freqmapper_verification ("
        "  event_id TEXT PRIMARY KEY,"
        "  seen_at INTEGER NOT NULL"
        ")"
    )
    now = int(time.time())
    conn.executemany(
        "INSERT INTO freqmapper_verification(event_id, seen_at) VALUES (?, ?)",
        [
            ("verified_tx:already-verified-1", now),
            ("verified_tx:already-verified-2", now),
            ("passive_rx:already-heard-1", now),
        ],
    )
    conn.commit()
    return path


def test_migrate_reverts_already_migrated_table_strips_prefixes_drops_passive_rx_idempotently(tmp_path):
    path = _d114a5a_shape_freqmapper_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    db._migrate_freqmapper_verification_verification_id(conn)
    conn.commit()

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(freqmapper_verification)")}
    assert "event_id" not in cols
    assert "verification_id" in cols

    ids = {row["verification_id"] for row in conn.execute("SELECT verification_id FROM freqmapper_verification")}
    # verified_tx: rows unprefixed and kept; passive_rx: row deleted
    # outright -- see the migration's own docstring for why (passive RX
    # never painted anything, so there is no dedup history worth the
    # risk of mixing reception_id's UUID space into this column).
    assert ids == {"already-verified-1", "already-verified-2"}

    # Idempotent: running it again against the now-converged table must
    # not raise and must not change anything further.
    db._migrate_freqmapper_verification_verification_id(conn)
    conn.commit()
    ids_again = {row["verification_id"] for row in conn.execute("SELECT verification_id FROM freqmapper_verification")}
    conn.close()
    assert ids_again == ids


def test_duplicate_verification_id_no_prefix(conn):
    """An event whose verification_id is already stored is recognised
    as a duplicate purely by that bare field -- no event_id, no prefix,
    no string construction or parsing anywhere in the comparison. This
    is the direct proof that d114a5a's prefixed-event_id migration was
    never load-bearing for correct dedup.
    """
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    event = _tx_event("dup-no-prefix-1", NOW, node_ref)
    o1 = _process(conn, ingestor, event, season_id, registered)
    o2 = _process(conn, ingestor, event, season_id, registered)

    assert o1 == "painted"
    assert o2 == "skipped_duplicate"

    row = conn.execute(
        "SELECT verification_id FROM freqmapper_verification WHERE verification_id = ?",
        ("dup-no-prefix-1",),
    ).fetchone()
    assert row is not None  # stored bare, exactly as verification_id was on the event


# ---------------------------------------------------------------------
# 2. The high-water-mark backfill guard
# ---------------------------------------------------------------------

def test_event_older_than_mark_and_unseen_is_recorded_as_backfill_skipped_not_painted(conn):
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    # Seed a high-water mark well after this event's occurred_at --
    # simulating a deployment that has already been running and has
    # seen recent coverage, then has its cursor cleared (or a feed
    # cutover) and is handed a genuinely-never-seen HISTORICAL event.
    mark = NOW
    set_cursor(conn, HIGH_WATER_MARK_KEY, str(mark))

    old_event = _tx_event("historical-1", mark - 3600)  # one hour before the mark
    outcome = _process(conn, ingestor, old_event, season_id, registered)

    assert outcome == "backfill_skipped"
    rows = conn.execute("SELECT count(*) FROM player_cell_ping").fetchone()[0]
    assert rows == 0  # never painted

    # Recorded in the dedup table -- so it is never re-evaluated on a
    # later poll just because the mark hasn't caught up to it yet.
    seen = conn.execute(
        "SELECT count(*) FROM freqmapper_verification WHERE verification_id = ?",
        ("historical-1",),
    ).fetchone()[0]
    assert seen == 1

    # The mark itself must not have moved backwards.
    assert get_cursor(conn, HIGH_WATER_MARK_KEY, "") == str(mark)

    # A later poll seeing this exact event again is an ordinary
    # duplicate, not re-evaluated as backfill a second time.
    outcome2 = _process(conn, ingestor, old_event, season_id, registered)
    assert outcome2 == "skipped_duplicate"


def test_event_newer_than_mark_paints_and_advances_mark(conn):
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    mark = NOW - 7200
    set_cursor(conn, HIGH_WATER_MARK_KEY, str(mark))

    new_event = _tx_event("fresh-1", mark + 60)  # one minute after the mark
    outcome = _process(conn, ingestor, new_event, season_id, registered)

    assert outcome == "painted"
    rows = conn.execute("SELECT count(*) FROM player_cell_ping").fetchone()[0]
    assert rows == 1

    assert get_cursor(conn, HIGH_WATER_MARK_KEY, "") == str(mark + 60)


def test_fresh_install_with_no_mark_and_no_history_still_ingests_normally(conn):
    """A brand-new deployment (or a database that has never processed a
    FreqMapper event before) has no high-water-mark cursor row at all --
    there is nothing yet to protect, so the very first event must
    process and paint normally, and the mark is seeded from it rather
    than blocking ingestion outright.
    """
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    assert get_cursor(conn, HIGH_WATER_MARK_KEY, "") == ""  # confirm no mark exists yet

    event_ts = NOW - 999999  # an old-looking timestamp is irrelevant with no mark to compare against
    event = _tx_event("first-ever-1", event_ts)
    outcome = _process(conn, ingestor, event, season_id, registered)

    assert outcome == "painted"
    rows = conn.execute("SELECT count(*) FROM player_cell_ping").fetchone()[0]
    assert rows == 1

    # Seeded from the event this deployment just processed.
    assert get_cursor(conn, HIGH_WATER_MARK_KEY, "") == str(event_ts)


def test_allow_backfill_opt_in_lets_old_events_paint(conn):
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    mark = NOW
    set_cursor(conn, HIGH_WATER_MARK_KEY, str(mark))

    old_event = _tx_event("deliberate-backfill-1", mark - 3600)
    outcome = _process(conn, ingestor, old_event, season_id, registered, allow_backfill=True)

    assert outcome == "painted"
    rows = conn.execute("SELECT count(*) FROM player_cell_ping").fetchone()[0]
    assert rows == 1

    # An intentionally-painted old event must never drag the mark
    # backwards -- it stays exactly where it was.
    assert get_cursor(conn, HIGH_WATER_MARK_KEY, "") == str(mark)


def test_allow_backfill_off_by_default(conn):
    """The guard's default (allow_backfill not passed at all, matching
    freqmapper_config's own column default of 0/off) must be active --
    an operator has to explicitly opt in, never the other way around.
    """
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    mark = NOW
    set_cursor(conn, HIGH_WATER_MARK_KEY, str(mark))

    old_event = _tx_event("default-guard-active-1", mark - 3600)
    # Calling _process_one_event directly without allow_backfill at all
    # (bypassing this file's _process helper's explicit default) proves
    # the function's own keyword default, not just this test file's.
    outcome = ingestor._process_one_event(
        conn, old_event, season_id, registered, NOW,
        "both", 1.0, 0.5, "2020-01-01",
    )
    assert outcome == "backfill_skipped"


# ---------------------------------------------------------------------
# 3. Seeding the mark on an UPGRADING deployment
#    (_maybe_seed_high_water_mark) -- the gap fix
# ---------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """A fresh on-disk sqlite file with the real schema, settings.db_path
    pointed at it -- _maybe_seed_high_water_mark always goes through
    app.db.connect()/WriteSession, both of which always open
    settings.db_path, so there is no way to hand it an in-memory
    connection directly. Same fixture shape as
    tests/test_freqmapper_combined_feed.py's own db_path.
    """
    path = tmp_path / "game.db"
    monkeypatch.setattr(settings, "db_path", str(path))
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                continue
            raise
    conn.commit()
    conn.close()
    return str(path)


def _seed_player_and_node_file(db_path: str, node_ref: str = "0a0a0a0a",
                                player_id: int = 1, team: str = "RED") -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?, ?, ?, ?)",
        (player_id, f"player-{player_id}", team, NOW),
    )
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) VALUES (?, ?, ?, ?)",
        (PROTOCOL, node_ref, player_id, NOW),
    )
    conn.commit()
    conn.close()


def _insert_verification_history(db_path: str, rows: list[tuple[str, int]]) -> None:
    """Directly populate freqmapper_verification with (verification_id,
    seen_at) rows -- simulating a real deployment's existing dedup
    history from before this guard existed, the exact shape
    _maybe_seed_high_water_mark reads max(seen_at) from.
    """
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO freqmapper_verification(verification_id, seen_at) VALUES (?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _open(db_path: str) -> sqlite3.Connection:
    # isolation_level=None (autocommit) -- matches app.db.connect()'s own
    # setting, which _season_id's explicit BEGIN IMMEDIATE/COMMIT relies
    # on, same as the shared in-memory `conn` fixture (tests/conftest.py).
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _run(coro):
    return asyncio.run(coro)


def test_seed_from_history_upgrading_deployment_blocks_older_events(db_path):
    """THE GAP FIX, proven end to end: an upgrading deployment (existing
    freqmapper_verification history, no mark yet) must have the guard
    active from the very first cycle -- not stand aside for the oldest
    event in FreqMapper's history the way an unprotected fresh-install
    fallback would, which was the gap that reproduced the incident with
    the guard installed.
    """
    node_ref = "0a0a0a0a"
    _seed_player_and_node_file(db_path, node_ref)

    newest_seen_at = NOW - 100  # this deployment's last processed event, from before the guard existed
    _insert_verification_history(db_path, [
        ("already-verified-1", NOW - 10000),
        ("already-verified-2", newest_seen_at),
    ])

    conn = _open(db_path)
    assert get_cursor(conn, HIGH_WATER_MARK_KEY, "") == ""  # no mark yet
    conn.close()

    ingestor = FreqMapperIngestor()
    _run(ingestor._maybe_seed_high_water_mark())

    expected_seed = newest_seen_at - _BACKFILL_SEED_GRACE_SECONDS
    conn = _open(db_path)
    assert get_cursor(conn, HIGH_WATER_MARK_KEY, "") == str(expected_seed)

    # A page from FreqMapper's true beginning of history -- exactly what
    # a brand-new combined-feed cursor hands back on an upgrading
    # deployment -- must be recognized as backfill, not painted, now
    # that the mark is seeded ahead of it.
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ancient_event = _tx_event("ancient-history-1", NOW - 50000, node_ref)
    outcome = _process(conn, ingestor, ancient_event, season_id, registered)
    conn.close()

    assert outcome == "backfill_skipped"
    conn2 = _open(db_path)
    rows = conn2.execute("SELECT count(*) FROM player_cell_ping").fetchone()[0]
    conn2.close()
    assert rows == 0


def test_seed_grace_window_admits_event_that_occurred_within_it(db_path):
    """An event that genuinely occurred shortly before the switchover --
    within the grace window -- must NOT be misclassified as backfill
    just because its seen_at (this deployment's processing time) landed
    at or after this deployment's last pre-guard record.
    """
    node_ref = "0a0a0a0a"
    _seed_player_and_node_file(db_path, node_ref)

    newest_seen_at = NOW - 100
    _insert_verification_history(db_path, [("already-verified-1", newest_seen_at)])

    ingestor = FreqMapperIngestor()
    _run(ingestor._maybe_seed_high_water_mark())

    seeded = newest_seen_at - _BACKFILL_SEED_GRACE_SECONDS
    conn = _open(db_path)
    assert get_cursor(conn, HIGH_WATER_MARK_KEY, "") == str(seeded)

    # occurred_at falls INSIDE the grace window (after the seeded mark,
    # before newest_seen_at) -- a legitimate near-boundary event, not
    # backfill.
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    within_grace_event = _tx_event(
        "within-grace-1", seeded + (_BACKFILL_SEED_GRACE_SECONDS // 2), node_ref,
    )
    outcome = _process(conn, ingestor, within_grace_event, season_id, registered)
    conn.close()

    assert outcome == "painted"


def test_seed_fresh_install_empty_table_ingests_normally(db_path):
    """A true fresh install -- freqmapper_verification is empty -- must
    not have a phantom mark seeded from nothing. The bootstrap stands
    aside entirely, exactly as if it did not exist, and the first event
    processed seeds the mark itself (see this file's section 2 for that
    per-event fallback tested directly).
    """
    node_ref = "0a0a0a0a"
    _seed_player_and_node_file(db_path, node_ref)

    ingestor = FreqMapperIngestor()
    _run(ingestor._maybe_seed_high_water_mark())

    conn = _open(db_path)
    assert get_cursor(conn, HIGH_WATER_MARK_KEY, "") == ""  # no phantom mark

    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    old_looking_event = _tx_event("first-ever-1", NOW - 999999, node_ref)
    outcome = _process(conn, ingestor, old_looking_event, season_id, registered)
    conn.close()

    assert outcome == "painted"  # nothing to protect against yet -- normal ingestion


def test_seed_then_allow_backfill_still_paints_old_events(db_path):
    """allow_backfill overrides a mark seeded from history exactly as it
    overrides one seeded any other way -- a deliberate backfill remains
    possible even on an upgrading deployment.
    """
    node_ref = "0a0a0a0a"
    _seed_player_and_node_file(db_path, node_ref)

    newest_seen_at = NOW - 100
    _insert_verification_history(db_path, [("already-verified-1", newest_seen_at)])

    ingestor = FreqMapperIngestor()
    _run(ingestor._maybe_seed_high_water_mark())

    conn = _open(db_path)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ancient_event = _tx_event("deliberate-backfill-after-seed-1", NOW - 50000, node_ref)
    outcome = _process(conn, ingestor, ancient_event, season_id, registered, allow_backfill=True)
    conn.close()

    assert outcome == "painted"
