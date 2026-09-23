"""Tests for db._migrate_mc_tile_capture_log_nullable_actor() -- the
table-rebuild migration that relaxes mc_tile_capture_log.by_player_id/
by_team from NOT NULL to nullable, so a territory-release event
(app/mc_scoring.py's release_tile(), event_type='release') can be
logged with no actor.

Same idiom as tests/test_sessions.py's own privacy-hardening migration
section (see that file's own comment): build the OLD pre-migration
table shape by hand rather than through SCHEMA (which already reflects
the post-migration shape), to prove EXISTING rows -- not just future
ones -- survive the rebuild, and that nothing (not the data, not the
index) is lost.

REAL_TABLE_SQL / REAL_INDEX_SQL below are not a guess at the old shape:
they are the literal `sql` column app/db.py's own migration reads from
sqlite_master, copied byte for byte from preview (CT 113 on utility,
`ssh root@192.168.1.241` -> `pct exec 113` -> `docker exec
meshwars-meshwars-1 python3`, read-only `mode=ro` connection) on
2026-09-23, which per that host's own docs holds a frozen snapshot of
prod taken the same day -- so this IS prod's schema text, not a
simulation of it. Notice from_team/by_air share one line with no
newline between them: that's what an earlier `ALTER TABLE ... ADD
COLUMN by_air` actually left behind on a real, already-deployed
database -- SQLite does not reformat a table's stored CREATE TABLE text
to match this file's aligned style when a column is appended, so a
migration that assumed the aligned style (as an early draft of this one
did, via a hand-typed string search-and-replace) was trusting a
formatting invariant that had already been broken once in this exact
table's history.
"""
from __future__ import annotations

import sqlite3
import time

import app.db as db

NOW = int(time.time())

REAL_TABLE_SQL = (
    "CREATE TABLE mc_tile_capture_log (\n"
    "    season_id    INTEGER NOT NULL,\n"
    "    cell_id      TEXT NOT NULL,\n"
    "    ts           INTEGER NOT NULL,\n"
    "    by_player_id INTEGER NOT NULL,\n"
    "    by_team      TEXT NOT NULL,\n"
    "    from_team    TEXT, by_air INTEGER NOT NULL DEFAULT 0,\n"
    "    PRIMARY KEY (season_id, cell_id, ts)\n"
    ")"
)
REAL_INDEX_SQL = "CREATE INDEX idx_mc_capture_log_cell ON mc_tile_capture_log(season_id, cell_id)"


def _old_shape_db(tmp_path, with_event_type=False) -> str:
    """A standalone sqlite file with mc_tile_capture_log in its REAL,
    pre-migration, already-deployed shape (see this module's own
    docstring for where REAL_TABLE_SQL/REAL_INDEX_SQL came from),
    populated with a handful of real-looking rows.

    with_event_type=True additionally applies the MIGRATIONS loop's own
    plain "ADD COLUMN event_type ... DEFAULT 'capture'" ALTER by hand,
    matching the order init_db() actually runs things in (MIGRATIONS
    loop, including that ALTER, always completes before
    _migrate_mc_tile_capture_log_nullable_actor() is ever called) --
    used by the tests below that call the migration function directly,
    in isolation, rather than through db.init_db() end to end.
    """
    path = str(tmp_path / "pre_migration.db")
    conn = sqlite3.connect(path)
    conn.execute(REAL_TABLE_SQL)
    conn.execute(REAL_INDEX_SQL)
    if with_event_type:
        conn.execute("ALTER TABLE mc_tile_capture_log ADD COLUMN event_type TEXT NOT NULL DEFAULT 'capture'")
    rows = [
        (1, "10_20", NOW - 300, 1, "RED", None, 0),
        (1, "10_21", NOW - 200, 2, "BLUE", "RED", 0),  # a flip
        (1, "10_22", NOW - 100, 3, "RED", None, 1),  # by_air
    ]
    conn.executemany(
        "INSERT INTO mc_tile_capture_log"
        "  (season_id, cell_id, ts, by_player_id, by_team, from_team, by_air)"
        "  VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return path


def test_pre_migration_shape_really_rejects_a_null_actor(tmp_path):
    """Sanity check on the fixture itself: the real old shape must
    reject exactly the kind of row this migration exists to allow,
    proving the "before" state is genuine."""
    path = _old_shape_db(tmp_path, with_event_type=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO mc_tile_capture_log(season_id, cell_id, ts, by_player_id, by_team, from_team, event_type) "
            "VALUES (1, '99_99', 9999, NULL, NULL, 'RED', 'release')"
        )
        assert False, "pre-migration schema accepted a null actor"
    except sqlite3.IntegrityError:
        pass
    conn.close()


def test_migration_relaxes_constraint_and_preserves_every_row(tmp_path):
    path = _old_shape_db(tmp_path, with_event_type=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    db._migrate_mc_tile_capture_log_nullable_actor(conn)

    cols = {row["name"]: row for row in conn.execute("PRAGMA table_info(mc_tile_capture_log)")}
    assert cols["by_player_id"]["notnull"] == 0
    assert cols["by_team"]["notnull"] == 0
    assert "event_type" in cols

    rows = {
        r["cell_id"]: dict(r)
        for r in conn.execute("SELECT * FROM mc_tile_capture_log ORDER BY ts")
    }
    assert len(rows) == 3
    assert rows["10_20"]["by_team"] == "RED"
    assert rows["10_20"]["event_type"] == "capture"  # backfilled default, not lost
    assert rows["10_21"]["from_team"] == "RED"  # the flip's from_team survives
    assert rows["10_22"]["by_air"] == 1

    # A release-shaped row -- the entire point -- now inserts cleanly.
    conn.execute(
        "INSERT INTO mc_tile_capture_log(season_id, cell_id, ts, by_player_id, by_team, from_team, by_air, event_type) "
        "VALUES (1, '10_20', ?, NULL, NULL, 'RED', 0, 'release')",
        (NOW,),
    )
    conn.commit()
    released = conn.execute(
        "SELECT * FROM mc_tile_capture_log WHERE cell_id = '10_20' AND ts = ?", (NOW,)
    ).fetchone()
    assert released["by_player_id"] is None
    assert released["by_team"] is None
    assert released["event_type"] == "release"
    conn.close()


def test_migration_recreates_the_index_and_primary_key(tmp_path):
    """Named explicitly because a silently dropped index on a
    never-pruned table that ownership_at() queries with a window
    function (app/results.py) would be a real, easy-to-miss performance
    regression -- this must fail loudly if idx_mc_capture_log_cell
    doesn't come back."""
    path = _old_shape_db(tmp_path, with_event_type=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    db._migrate_mc_tile_capture_log_nullable_actor(conn)

    indexes = {
        r["name"]: r["sql"] for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'mc_tile_capture_log'"
        )
    }
    assert indexes["idx_mc_capture_log_cell"] == REAL_INDEX_SQL
    assert "sqlite_autoindex_mc_tile_capture_log_1" in indexes  # the PK's own automatic index

    pk_cols = [
        c["name"] for c in conn.execute("PRAGMA table_info(mc_tile_capture_log)")
        if c["pk"] > 0
    ]
    assert pk_cols == ["season_id", "cell_id", "ts"]

    # A duplicate (season_id, cell_id, ts) is still rejected -- the PK
    # constraint itself, not just its automatic index, made it through
    # the rebuild.
    try:
        conn.execute(
            "INSERT INTO mc_tile_capture_log(season_id, cell_id, ts, by_player_id, by_team, from_team) "
            "VALUES (1, '10_20', ?, 9, 'RED', NULL)",
            (NOW - 300,),  # same PK as the seeded '10_20' row
        )
        assert False, "duplicate primary key was accepted after the rebuild"
    except sqlite3.IntegrityError:
        pass
    conn.close()


def test_migration_is_idempotent(tmp_path):
    """init_db() calls this on every boot -- a second run against an
    already-migrated database must be a true no-op, not an error and
    not a second rebuild that could lose or duplicate data."""
    path = _old_shape_db(tmp_path, with_event_type=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    db._migrate_mc_tile_capture_log_nullable_actor(conn)
    before = [dict(r) for r in conn.execute("SELECT * FROM mc_tile_capture_log ORDER BY ts")]

    db._migrate_mc_tile_capture_log_nullable_actor(conn)  # must not raise
    after = [dict(r) for r in conn.execute("SELECT * FROM mc_tile_capture_log ORDER BY ts")]

    assert before == after
    assert len(after) == 3
    conn.close()


def test_init_db_end_to_end_against_the_real_prod_schema_text(tmp_path, monkeypatch):
    """The full boot path (db.init_db()), not just the migration
    function in isolation: MIGRATIONS' own event_type ALTER runs first,
    exactly as it does on a real boot, then
    _migrate_mc_tile_capture_log_nullable_actor() rebuilds the table --
    against a database seeded from the REAL schema text read off
    preview, with no event_type column pre-applied by the test (that is
    init_db()'s job here, not the fixture's). Also proves init_db()
    itself is safe to call twice, matching every process booting
    against an already-migrated database.
    """
    path = _old_shape_db(tmp_path, with_event_type=False)
    monkeypatch.setattr(db.settings, "db_path", path)

    db.init_db()
    db.init_db()

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(mc_tile_capture_log)")}
    assert cols["by_player_id"]["notnull"] == 0
    assert cols["by_team"]["notnull"] == 0
    assert dict(cols["event_type"])["dflt_value"] == "'capture'"

    rows = conn.execute("SELECT COUNT(*) FROM mc_tile_capture_log").fetchone()[0]
    assert rows == 3

    indexes = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'mc_tile_capture_log'"
        )
    }
    assert "idx_mc_capture_log_cell" in indexes
    conn.close()
