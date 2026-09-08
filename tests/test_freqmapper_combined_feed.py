"""Tests for the 2026-09-08 FreqMapper combined-feed cutover
(app/freqmapper_ingest.py, app/db.py's freqmapper_verification/
freqmapper_config schema changes), AS AMENDED by the same-day incident
fix (see app/freqmapper_ingest.py's module docstring for the full
story): the schema migration this file originally tested
(_migrate_freqmapper_verification_event_id, prefixing verification_id
into a combined event_id) turned out to be unnecessary and has been
reverted (app/db.py's _migrate_freqmapper_verification_verification_id);
what actually protects the live board is the separate high-water-mark
backfill guard, tested in tests/test_freqmapper_backfill_guard.py, not
this file.

FreqMapper replaced the old TX-only /verified-coverage feed this module
used to poll with a combined /coverage-events feed carrying both
verified_tx and passive_rx events under one cursor. This is a
scoring-affecting migration, so these tests are organized around the
specific correctness properties Matt's brief called out:

  A. event_type branching -- verified_tx scores exactly as before,
     passive_rx and any unrecognized event_type are counted but never
     painted, and neither can crash the poll loop (test_event_type_*).
  B. The dedupe-key REVERT -- app/db.py's
     _migrate_freqmapper_verification_verification_id, which converges
     every deployment (including preview, and any operator who ran
     d114a5a even briefly) back onto a single `verification_id` column:
     a database already migrated to the prefixed `event_id` shape is
     converted back (verified_tx: rows unprefixed and kept, passive_rx:
     rows dropped), idempotently, and dedup itself now reads each
     event's own verification_id/reception_id field directly -- no
     prefixed string is ever constructed or parsed (test_migrate_*,
     test_already_stored_verification_id_is_duplicate_no_prefix).
  C. occurred_at vs. published_at -- occurred_at drives the paint
     timestamp and the paint_from date gate; published_at must never be
     read as an event time even when it would produce a very different
     answer (test_event_time_*, test_process_one_event_paint_from_gate_*).
  D. watcher_count weighting -- OFF by default (neutral: identical score
     to before this feature existed, regardless of watcher_count),
     correct scaling when explicitly enabled, and null/missing
     watcher_count always falls back to flat, never zero
     (test_verified_tx_points_*, test_process_one_event_watcher_*).
  E. Rate-limit and error hygiene -- 429 honours Retry-After and never
     advances the cursor, 5xx/network failures retry the same
     request+cursor with increasing backoff, 401 does not retry in a
     tight loop, and a page that fails partway through processing never
     gets its cursor persisted (test_poll_once_429_*,
     test_fetch_page_*, test_poll_once_leaves_cursor_untouched_*).

Uses a real file-backed sqlite database (db_path fixture) for anything
that drives FreqMapperIngestor's async poll methods (they always go
through app.db.connect()/WriteSession, which always open
settings.db_path -- see tests/test_paint_source_both.py's own db_path
fixture, duplicated here for this file's independence) and the shared
in-memory `conn` fixture (tests/conftest.py) for direct
_process_one_event calls that don't need the HTTP layer at all.

HTTP is mocked via httpx.MockTransport, patched into
app.freqmapper_ingest.httpx.AsyncClient -- the same pattern
tests/test_checkin_confirm.py's _patch_checkin_http and
tests/test_oauth_api.py's _patch_provider_http already use for their
own outbound connectors.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime, timezone

import httpx
import pytest

import app.db as db
from app import mc_scoring
from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.freqmapper_ingest import (
    COMBINED_CURSOR_KEY,
    COMBINED_EVENTS_PATH,
    FreqMapperIngestor,
    _event_time,
    _verified_tx_points,
)
from app.grid import cell_id as grid_cell_id
from app.db import get_cursor

NOW = int(time.time())
PROTOCOL = "mt"
LAT, LON = 43.0, -116.0  # well within settings.play_area_* (see app/config.py)


# ---------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """A fresh on-disk sqlite file with the real schema, settings.db_path
    pointed at it -- FreqMapperIngestor's async methods always go
    through app.db.connect()/WriteSession, both of which always open
    settings.db_path, so there is no way to hand them a connection
    directly. Same fixture shape as tests/test_paint_source_both.py's
    own db_path.
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


def _configure(db_path: str, **overrides) -> None:
    """Set freqmapper_config's singleton row for a db_path-backed test.
    Defaults describe an enabled, fully-configured connector with
    watcher weighting OFF (the shipped default) and passive RX painting
    ON at its own shipped defaults -- override only what a given test
    actually needs to differ.
    """
    cols = {
        "mt_paint_source": "both",
        "enabled": 1,
        "base_url": "https://fm.example.invalid",
        "api_key": "fm_live_testkey",
        "poll_interval_seconds": 60,
        "page_limit": 500,
        "points_per_event": 1.0,
        "unique_painter_bonus": 0.5,
        "paint_from": "2020-01-01",
        "watcher_weight_enabled": 0,
        "watcher_weight_base": 0.5,
        "watcher_weight_increment": 0.1,
        "watcher_weight_cap": 1.0,
        "passive_rx_enabled": 1,
        "passive_rx_points_per_event": 0.5,
        "passive_rx_unique_painter_bonus": 0.5,
        "updated_at": int(time.time()),
    }
    cols.update(overrides)
    conn = sqlite3.connect(db_path)
    set_clause = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE freqmapper_config SET {set_clause} WHERE id = 1", tuple(cols.values()))
    conn.commit()
    conn.close()


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


def _tx_event(raw_id: str, node_ref: str = "0a0a0a0a", occurred_at: str | None = None,
              watcher_count=None, published_at: str | None = None) -> dict:
    event = {
        "event_id": f"verified_tx:{raw_id}",
        "event_type": "verified_tx",
        # verification_id is its OWN field on the live feed, not derived
        # from event_id -- see app/freqmapper_ingest.py's module
        # docstring ("Dedupe keys") and _process_one_event, which reads
        # this field directly and never parses event_id's prefix.
        "verification_id": raw_id,
        "radio_node_id": "!" + node_ref,
        "latitude": LAT,
        "longitude": LON,
        "occurred_at": occurred_at or datetime.now(timezone.utc).isoformat(),
    }
    if watcher_count is not None:
        event["watcher_count"] = watcher_count
    if published_at is not None:
        event["published_at"] = published_at
    return event


def _rx_event(raw_id: str, node_ref: str = "0a0a0a0a") -> dict:
    return {
        "event_id": f"passive_rx:{raw_id}",
        # reception_id is passive_rx's own dedupe field, its own UUID
        # space, independent of verification_id -- see _tx_event above.
        "reception_id": raw_id,
        "event_type": "passive_rx",
        "radio_node_id": "!" + node_ref,
        "latitude": LAT,
        "longitude": LON,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }


def _patch_freqmapper_http(monkeypatch, handler) -> None:
    """Redirects every httpx.AsyncClient FreqMapperIngestor._ensure_client
    constructs through an httpx.MockTransport running `handler`."""
    import app.freqmapper_ingest as freqmapper_ingest

    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(freqmapper_ingest.httpx, "AsyncClient", _MockAsyncClient)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------
# A. event_type branching
# ---------------------------------------------------------------------

def test_process_one_event_verified_tx_paints_exactly_as_before(conn):
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    outcome = ingestor._process_one_event(
        conn, _tx_event("tx-1", node_ref), season_id, registered, NOW,
        "both", 1.0, 0.5, "2020-01-01",
    )
    assert outcome == "painted"
    rows = conn.execute("SELECT count(*) FROM player_cell_ping").fetchone()[0]
    assert rows == 1


def test_process_one_event_passive_rx_paints_by_default(conn):
    """AS OF the passive-RX-painting feature (see
    tests/test_freqmapper_passive_rx.py for the full behavior this
    module now has): passive_rx_enabled defaults to True (matching
    freqmapper_config's own shipped default -- see app/db.py), so a
    passive_rx event now paints when the caller does not explicitly
    pass passive_rx_enabled=False. This supersedes this test's own
    former assertion (skipped_passive_rx, never painted) from before RX
    could score at all.
    """
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    outcome = ingestor._process_one_event(
        conn, _rx_event("rx-1", node_ref), season_id, registered, NOW,
        "both", 1.0, 0.5, "2020-01-01",
    )
    assert outcome == "painted_rx"
    rows = conn.execute("SELECT count(*) FROM player_cell_ping").fetchone()[0]
    assert rows == 1

    # Deduped -- a second look at the exact same passive_rx event_id is
    # a duplicate, not scored twice.
    outcome2 = ingestor._process_one_event(
        conn, _rx_event("rx-1", node_ref), season_id, registered, NOW,
        "both", 1.0, 0.5, "2020-01-01",
    )
    assert outcome2 == "skipped_rx_duplicate"


def test_process_one_event_passive_rx_disabled_counts_never_paints(conn):
    """passive_rx_enabled=False (an operator opt-out, not the shipped
    default) still counts/dedupes the event exactly like an active
    verified_tx would, but never paints -- see full coverage of this in
    tests/test_freqmapper_passive_rx.py.
    """
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    outcome = ingestor._process_one_event(
        conn, _rx_event("rx-disabled-1", node_ref), season_id, registered, NOW,
        "both", 1.0, 0.5, "2020-01-01",
        passive_rx_enabled=False,
    )
    assert outcome == "skipped_rx_disabled"
    rows = conn.execute("SELECT count(*) FROM player_cell_ping").fetchone()[0]
    assert rows == 0


def test_process_one_event_unknown_event_type_counted_never_crashes(conn):
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    event = {
        "event_id": "mystery:evt-1",
        "event_type": "some_future_event_type",
        "radio_node_id": "!" + node_ref,
        "latitude": LAT,
        "longitude": LON,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    outcome = ingestor._process_one_event(
        conn, event, season_id, registered, NOW, "both", 1.0, 0.5, "2020-01-01",
    )
    assert outcome == "skipped_unknown_event_type"
    rows = conn.execute("SELECT count(*) FROM player_cell_ping").fetchone()[0]
    assert rows == 0
    seen = conn.execute(
        "SELECT count(*) FROM freqmapper_verification WHERE verification_id = ?", ("mystery:evt-1",)
    ).fetchone()[0]
    assert seen == 1


def test_process_one_event_ordinary_dedupe(conn):
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()
    event = _tx_event("dup-1", node_ref)

    o1 = ingestor._process_one_event(conn, event, season_id, registered, NOW, "both", 1.0, 0.5, "2020-01-01")
    o2 = ingestor._process_one_event(conn, event, season_id, registered, NOW, "both", 1.0, 0.5, "2020-01-01")
    assert o1 == "painted"
    assert o2 == "skipped_duplicate"


# ---------------------------------------------------------------------
# B. the dedupe-key revert (app/db.py) -- undoing d114a5a's unnecessary
#    verification_id -> event_id migration, converging every deployment
#    (including one that ran d114a5a briefly) back onto one shape.
# ---------------------------------------------------------------------

def _d114a5a_shape_freqmapper_db(tmp_path) -> str:
    """A standalone sqlite file with freqmapper_verification in the
    shape d114a5a's now-removed migration left it in -- `event_id`
    holding a mix of prefixed verified_tx: and passive_rx: rows, plus
    one row matching neither prefix (an unrecognized-event-type row,
    dedup'd on the feed's own generic event_id -- see
    _process_one_event's own fallback) -- exactly what preview, or any
    operator who deployed d114a5a even briefly, actually has on disk.
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
            ("verified_tx:old-uuid-1", now),
            ("verified_tx:old-uuid-2", now),
            ("passive_rx:old-rx-uuid-1", now),
            ("mystery:untouched-1", now),
        ],
    )
    conn.commit()
    return path


def test_migrate_reverts_event_id_strips_prefixes_drops_passive_rx(tmp_path):
    path = _d114a5a_shape_freqmapper_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    db._migrate_freqmapper_verification_verification_id(conn)
    conn.commit()

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(freqmapper_verification)")}
    assert "event_id" not in cols
    assert "verification_id" in cols

    ids = {row["verification_id"] for row in conn.execute("SELECT verification_id FROM freqmapper_verification")}
    conn.close()
    # verified_tx: rows -> unprefixed and kept; passive_rx: row -> DELETED
    # (see the migration's own docstring for why: passive RX never
    # painted anything, so there is no dedup history worth protecting,
    # and keeping it would mix reception_id's UUID space into a column
    # now reasoned about as pure verification_id space); an unrecognized
    # row matching neither prefix is left untouched.
    assert ids == {"old-uuid-1", "old-uuid-2", "mystery:untouched-1"}


def test_migrate_reverts_is_a_no_op_on_a_never_migrated_or_already_reverted_db(conn):
    # The shared `conn` fixture already runs the current SCHEMA, which
    # defines freqmapper_verification with verification_id from the
    # start -- calling the migration against it must do nothing and
    # must not raise, since app/db.py's init_db() calls this
    # unconditionally on every boot.
    db._migrate_freqmapper_verification_verification_id(conn)  # must not raise

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(freqmapper_verification)")}
    assert "verification_id" in cols
    assert "event_id" not in cols


def test_migrate_reverts_is_idempotent_across_two_runs(tmp_path):
    path = _d114a5a_shape_freqmapper_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    db._migrate_freqmapper_verification_verification_id(conn)
    conn.commit()
    db._migrate_freqmapper_verification_verification_id(conn)  # must not raise or re-touch rows
    conn.commit()

    ids = {row["verification_id"] for row in conn.execute("SELECT verification_id FROM freqmapper_verification")}
    conn.close()
    assert ids == {"old-uuid-1", "old-uuid-2", "mystery:untouched-1"}


def test_already_stored_verification_id_is_duplicate_no_prefix(tmp_path):
    """THE correctness-critical proof for the revert: a verification_id
    already on disk (from before d114a5a, or converged back by the
    revert migration above) must be recognised as a duplicate when the
    combined feed hands the SAME bare verification_id back again --
    with no event_id, no prefix, and no string construction/parsing
    anywhere in the comparison. This is what makes the now-removed
    forward migration provably unnecessary: dedup never needed the
    prefixed form to begin with.
    """
    path = _d114a5a_shape_freqmapper_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    db._migrate_freqmapper_verification_verification_id(conn)
    conn.commit()

    # The combined feed hands back the exact same historical event,
    # keyed on its own bare verification_id field -- exactly what
    # _process_one_event's dedup INSERT does.
    cur = conn.execute(
        "INSERT OR IGNORE INTO freqmapper_verification(verification_id, seen_at) VALUES (?, ?)",
        ("old-uuid-1", int(time.time())),
    )
    conn.commit()
    conn.close()
    assert cur.rowcount == 0  # already present -- recognised as a duplicate, not inserted again


# ---------------------------------------------------------------------
# C. occurred_at vs. published_at
# ---------------------------------------------------------------------

def test_event_time_uses_occurred_at_and_ignores_published_at():
    event = {
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "published_at": "2030-01-01T00:00:00+00:00",  # if this were used, the answer would differ wildly
    }
    ts = _event_time(event)
    assert ts == int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())


def test_event_time_falls_back_to_verified_at_then_mapping_test_sent_at():
    assert _event_time({"verified_at": "2025-06-01T00:00:00+00:00"}) == int(
        datetime(2025, 6, 1, tzinfo=timezone.utc).timestamp()
    )
    assert _event_time({"mapping_test_sent_at": "2025-07-01T00:00:00+00:00"}) == int(
        datetime(2025, 7, 1, tzinfo=timezone.utc).timestamp()
    )
    assert _event_time({}) is None
    assert _event_time({"published_at": "2025-07-01T00:00:00+00:00"}) is None  # never a fallback


def test_process_one_event_paint_from_gate_uses_occurred_at_not_published_at(conn):
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    # occurred_at is BEFORE paint_from -- must be skipped, even though
    # published_at is well AFTER paint_from (would wrongly pass the
    # gate if published_at were ever used as the event time).
    early = _tx_event(
        "gate-early", node_ref,
        occurred_at="2020-01-01T00:00:00+00:00",
        published_at="2026-06-01T00:00:00+00:00",
    )
    outcome = ingestor._process_one_event(
        conn, early, season_id, registered, NOW, "both", 1.0, 0.5, "2025-01-01",
    )
    assert outcome == "skipped_before_paint_from"
    # Date-skipped events are deliberately left OUT of the dedup table
    # -- see this function's own comment -- so it stays recoverable.
    seen = conn.execute(
        "SELECT count(*) FROM freqmapper_verification WHERE verification_id = ?",
        ("gate-early",),
    ).fetchone()[0]
    assert seen == 0

    # occurred_at AFTER paint_from -- processed and painted normally.
    late = _tx_event("gate-late", node_ref, occurred_at="2026-06-01T00:00:00+00:00")
    outcome2 = ingestor._process_one_event(
        conn, late, season_id, registered, NOW, "both", 1.0, 0.5, "2025-01-01",
    )
    assert outcome2 == "painted"


# ---------------------------------------------------------------------
# D. watcher_count weighting
# ---------------------------------------------------------------------

def test_verified_tx_points_neutral_when_disabled_regardless_of_watcher_count():
    for watcher_count in (None, 1, 5, 500, 0, "not-a-number", True):
        assert _verified_tx_points(watcher_count, 1.0, False, 0.5, 0.1, 1.0) == 1.0


def test_verified_tx_points_flat_when_enabled_but_watcher_count_missing_or_null():
    # Missing/null must fall back to the exact flat value, never to
    # zero and never to watcher_weight_base as if there were exactly
    # one confirmed watcher.
    assert _verified_tx_points(None, 1.0, True, 0.5, 0.1, 5.0) == 1.0


def test_verified_tx_points_scales_with_watcher_count_when_enabled():
    # base=0.5, +0.1 per watcher beyond the first, 5 watchers -> 0.9
    assert _verified_tx_points(5, 1.0, True, 0.5, 0.1, 5.0) == pytest.approx(0.9)
    # 1 watcher -> exactly base
    assert _verified_tx_points(1, 1.0, True, 0.5, 0.1, 5.0) == pytest.approx(0.5)


def test_verified_tx_points_respects_the_cap():
    assert _verified_tx_points(500, 1.0, True, 0.5, 0.1, 2.0) == pytest.approx(2.0)


def test_verified_tx_points_zero_cap_disables_capping():
    assert _verified_tx_points(500, 1.0, True, 0.5, 0.1, 0.0) == pytest.approx(0.5 + 499 * 0.1)


def test_verified_tx_points_non_positive_watcher_count_falls_back_to_flat():
    assert _verified_tx_points(0, 1.0, True, 0.5, 0.1, 5.0) == 1.0
    assert _verified_tx_points(-3, 1.0, True, 0.5, 0.1, 5.0) == 1.0


def test_process_one_event_watcher_weighting_neutral_by_default():
    """Two independent databases, identical except watcher_count -- with
    weighting disabled (the default, no watcher_weight_* kwargs passed),
    the resulting team score must be IDENTICAL regardless of
    watcher_count. This is the deploy-changes-nothing proof at the
    _process_one_event level.
    """
    def _fresh_conn():
        c = sqlite3.connect(":memory:", isolation_level=None)
        c.row_factory = sqlite3.Row
        c.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                c.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                    continue
                raise
        return c

    node_ref = "0a0a0a0a"
    cell = grid_cell_id(LAT, LON)
    scores = {}
    for watcher_count in (1, 500):
        c = _fresh_conn()
        _seed_player_and_node(c, node_ref=node_ref)
        season_id = _season_id(c)
        registered = {node_ref: (1, "RED")}
        ingestor = FreqMapperIngestor()
        event = _tx_event(f"neutral-{watcher_count}", node_ref, watcher_count=watcher_count)
        outcome = ingestor._process_one_event(
            c, event, season_id, registered, NOW, "both", 1.0, 0.5, "2020-01-01",
        )
        assert outcome == "painted"
        row = c.execute(
            "SELECT score FROM mc_tile_score WHERE season_id = ? AND cell_id = ? AND team = 'RED'",
            (season_id, cell),
        ).fetchone()
        scores[watcher_count] = row["score"]
        c.close()

    assert scores[1] == scores[500] == pytest.approx(1.0 + 0.5)  # flat points + unique-painter bonus


def test_process_one_event_watcher_weighting_enabled_scales_score(conn):
    node_ref = "0a0a0a0a"
    cell = grid_cell_id(LAT, LON)
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    event = _tx_event("weighted-1", node_ref, watcher_count=5)
    outcome = ingestor._process_one_event(
        conn, event, season_id, registered, NOW, "both", 1.0, 0.5, "2020-01-01",
        watcher_weight_enabled=True, watcher_weight_base=0.5,
        watcher_weight_increment=0.1, watcher_weight_cap=5.0,
    )
    assert outcome == "painted"
    row = conn.execute(
        "SELECT score FROM mc_tile_score WHERE season_id = ? AND cell_id = ? AND team = 'RED'",
        (season_id, cell),
    ).fetchone()
    # tx_points = 0.5 + 4*0.1 = 0.9, plus the first-paint unique bonus (0.5) = 1.4
    assert row["score"] == pytest.approx(1.4)


# ---------------------------------------------------------------------
# E. rate-limit and error hygiene
# ---------------------------------------------------------------------

def test_poll_once_processes_combined_page_and_branches_by_event_type(db_path, monkeypatch):
    node_ref = "0a0a0a0a"
    _seed_player_and_node_file(db_path, node_ref)
    _configure(db_path)

    page = {
        "schema_version": 1,
        "has_more": False,
        "next_cursor": "combined-cursor-1",
        "events": [
            _tx_event("ev-1", node_ref, occurred_at="2026-06-01T00:00:00+00:00", watcher_count=3),
            _rx_event("rx-1", node_ref),
            {
                "event_id": "mystery:xyz-1",
                "event_type": "future_thing",
                "radio_node_id": "!" + node_ref,
                "latitude": LAT, "longitude": LON,
                "occurred_at": "2026-06-01T00:00:00+00:00",
            },
        ],
    }
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200, json=page,
            headers={"X-RateLimit-Limit": "120", "X-RateLimit-Remaining": "119"},
        )

    _patch_freqmapper_http(monkeypatch, handler)

    ingestor = FreqMapperIngestor()
    _run(ingestor._poll_once())

    assert calls == [COMBINED_EVENTS_PATH]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    assert get_cursor(conn, COMBINED_CURSOR_KEY, "") == "combined-cursor-1"
    painted = conn.execute("SELECT count(*) FROM player_cell_ping").fetchone()[0]
    # BOTH the verified_tx and the passive_rx event painted --
    # passive_rx_enabled defaults to 1 (ON), see app/db.py's
    # freqmapper_config comment -- the unrecognized "future_thing" type
    # is the only one of the three that never scores.
    assert painted == 2
    rows = conn.execute("SELECT evidence_type FROM player_cell_ping ORDER BY evidence_type").fetchall()
    assert [r["evidence_type"] for r in rows] == ["passive_rx", "verified_tx"]
    # Each event type deduped on its own bare id field -- verification_id
    # for verified_tx, reception_id for passive_rx, the feed's generic
    # event_id only as the fallback for the unrecognized type -- no
    # prefixed compound key anywhere (see this module's docstring).
    seen_ids = {r["verification_id"] for r in conn.execute("SELECT verification_id FROM freqmapper_verification")}
    assert seen_ids == {"ev-1", "rx-1", "mystery:xyz-1"}


def test_poll_once_429_honours_retry_after_and_does_not_advance_cursor(db_path, monkeypatch):
    _configure(db_path)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "7"}, json={"error": "slow down"})

    _patch_freqmapper_http(monkeypatch, handler)

    ingestor = FreqMapperIngestor()
    before = time.monotonic()
    _run(ingestor._poll_once())
    after = time.monotonic()

    assert len(calls) == 1
    assert before + 7 <= ingestor._retry_after <= after + 7 + 0.5

    conn = sqlite3.connect(db_path)
    assert get_cursor(conn, COMBINED_CURSOR_KEY, "") == ""
    row = conn.execute("SELECT last_poll_error FROM freqmapper_config WHERE id = 1").fetchone()
    assert row[0] is None  # not recorded as a broken connector -- rate limiting is expected

    # The cooldown blocks the very next cycle from attempting a request
    # at all.
    _run(ingestor._poll_once())
    assert len(calls) == 1


def test_fetch_page_retries_5xx_with_increasing_backoff_then_succeeds(db_path, monkeypatch):
    node_ref = "0a0a0a0a"
    _seed_player_and_node_file(db_path, node_ref)
    _configure(db_path)

    attempts = {"n": 0}
    page = {
        "schema_version": 1, "has_more": False, "next_cursor": "after-5xx",
        "events": [_tx_event("after-5xx-1", node_ref)],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            return httpx.Response(503, json={"error": "temporarily unavailable"})
        return httpx.Response(200, json=page)

    _patch_freqmapper_http(monkeypatch, handler)

    ingestor = FreqMapperIngestor()
    sleeps = []

    async def fast_sleep(seconds):
        sleeps.append(seconds)

    ingestor._sleep = fast_sleep
    _run(ingestor._poll_once())

    assert attempts["n"] == 3
    assert sleeps == [5, 10]  # increasing backoff, FreqMapper's own recommended schedule

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    assert get_cursor(conn, COMBINED_CURSOR_KEY, "") == "after-5xx"


def test_fetch_page_gives_up_after_exhausting_5xx_retries_without_advancing_cursor(db_path, monkeypatch):
    _configure(db_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    _patch_freqmapper_http(monkeypatch, handler)

    ingestor = FreqMapperIngestor()
    sleeps = []

    async def fast_sleep(seconds):
        sleeps.append(seconds)

    ingestor._sleep = fast_sleep
    _run(ingestor._poll_once())

    assert sleeps == [5, 10, 20, 60]  # the whole schedule, exhausted

    conn = sqlite3.connect(db_path)
    assert get_cursor(conn, COMBINED_CURSOR_KEY, "") == ""
    row = conn.execute("SELECT last_poll_error FROM freqmapper_config WHERE id = 1").fetchone()
    assert row[0] is not None


def test_fetch_page_401_does_not_retry_in_a_tight_loop_and_backs_off(db_path, monkeypatch):
    _configure(db_path, poll_interval_seconds=10)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, json={"error": "bad key"})

    _patch_freqmapper_http(monkeypatch, handler)

    ingestor = FreqMapperIngestor()
    _run(ingestor._poll_once())

    assert len(calls) == 1  # no tight retry loop within this cycle
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT last_poll_error FROM freqmapper_config WHERE id = 1").fetchone()
    assert row[0] is not None and "401" in row[0]
    assert get_cursor(conn, COMBINED_CURSOR_KEY, "") == ""

    # Backed off -- the very next cycle does not attempt another request.
    _run(ingestor._poll_once())
    assert len(calls) == 1


def test_poll_once_leaves_cursor_untouched_when_a_page_fails_mid_processing(db_path, monkeypatch):
    node_ref = "0a0a0a0a"
    _seed_player_and_node_file(db_path, node_ref)
    _configure(db_path)

    page = {
        "schema_version": 1, "has_more": False, "next_cursor": "should-never-be-saved",
        "events": [_tx_event("mid-1", node_ref), _tx_event("mid-2", node_ref)],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page)

    _patch_freqmapper_http(monkeypatch, handler)

    ingestor = FreqMapperIngestor()
    original = ingestor._process_one_event
    state = {"n": 0}

    def flaky(*args, **kwargs):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("simulated mid-page failure")
        return original(*args, **kwargs)

    ingestor._process_one_event = flaky

    with pytest.raises(RuntimeError):
        _run(ingestor._poll_once())

    conn = sqlite3.connect(db_path)
    assert get_cursor(conn, COMBINED_CURSOR_KEY, "") == ""
    # The whole page's transaction rolled back -- even the first event's
    # own dedup write is gone, proving the page failed atomically rather
    # than partially committing.
    seen = conn.execute("SELECT count(*) FROM freqmapper_verification").fetchone()[0]
    assert seen == 0
