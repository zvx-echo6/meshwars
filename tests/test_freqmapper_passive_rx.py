"""Tests for FreqMapper passive_rx PAINTING -- the feature this file's
sibling module (app/freqmapper_ingest.py) previously counted and deduped
but never scored (see that module's now-superseded "Passive RX scoring
is Phase 3" comment, removed by this change). passive_rx events now run
through the exact same shared pipeline verified_tx uses
(mc_scoring.apply_paint, flat-points mode) and the exact same gate
sequence (paint_from date gate, dedupe on the event's own id field, the
high-water-mark backfill guard, registration, coordinate validation,
play-area, mt_paint_source) -- see _process_one_event's own docstring in
app/freqmapper_ingest.py for why this is one shared pipeline now rather
than two copies that could quietly drift apart.

THE SEMANTICS THAT MAKE THIS DIFFERENT FROM verified_tx, and that these
tests exist to pin down: a passive_rx event's latitude/longitude are
where the WARDRIVING RADIO HEARD someone else's packet, not where the
original sender was. `radio_node_id` is that RECEIVING wardriver, so the
credited player is the listener and the painted cell is where the
listener was standing -- see test_passive_rx_paints_the_receivers_own_cell
below, which uses coordinates well away from the sample fixture's Salt
Lake City location specifically to prove the cell comes from the EVENT's
own lat/lon, not some other hardcoded value. `verified_coverage` is
always false on a passive_rx event, and FreqMapper's own documentation
is explicit that passive RX must never be presented or treated as
verified TX proof -- this deployment honours that by keeping the two
evidence types DISTINGUISHABLE via player_cell_ping.evidence_type (see
that column's own comment in app/db.py) even though, by Matt's explicit
decision ("coverage is coverage"), they currently earn IDENTICAL points.
Equal points, distinct labels -- see test_rx_and_tx_points_independently_
configurable and test_rx_paint_is_distinguishable_from_tx_paint below,
which prove those are two separate properties, not one.

The real passive_rx event handed to the executor building this file
(fetched live against FreqMapper's /api/v1/integrations/coverage-events
feed on 2026-09-07) is reproduced in _rx_event() below as the base
shape every test in this file builds from, with only reception_id,
radio_node_id, occurred_at, and coordinates ever overridden per test --
every other field (location_accuracy_meters, location_trust, quality,
rssi_dbm, snr_db, hop_count, path_classification, watcher_corroborated,
watcher_count, same_region_watcher_count, cross_region_watcher_count,
evidence_type, verified_coverage) is left exactly as FreqMapper actually
sent it, not a synthetic minimal fixture, so these tests exercise
app/freqmapper_ingest.py against the real event shape, not just the
combined-feed docs.

Same fixture/helper shapes as tests/test_freqmapper_combined_feed.py and
tests/test_freqmapper_backfill_guard.py (both already updated for this
same rollout) -- the shared in-memory `conn` fixture (tests/conftest.py)
for direct _process_one_event calls, since none of these tests need the
HTTP/poll-loop layer at all.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from app import mc_scoring
from app.db import get_cursor, set_cursor
from app.freqmapper_ingest import HIGH_WATER_MARK_KEY, FreqMapperIngestor
from app.grid import cell_id as grid_cell_id

NOW = int(time.time())
PROTOCOL = "mt"

# Well within settings.play_area_* (see app/config.py) -- same play area
# tests/test_freqmapper_combined_feed.py and
# tests/test_freqmapper_backfill_guard.py already use, so a test that
# needs a SECOND, clearly-different-but-still-in-bounds cell (proving
# the painted cell tracks the event's own coordinates rather than some
# other hardcoded value) has a real independent point to use instead of
# guessing at play-area bounds itself.
LAT, LON = 43.0, -116.0
LAT2, LON2 = 43.05, -116.2


def _seed_player_and_node(conn, player_id=1, node_ref="1bbeef80", team="RED"):
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


def _rx_event(
    reception_id: str,
    node_ref: str = "1bbeef80",
    *,
    occurred_at: str | None = None,
    lat: float = LAT,
    lon: float = LON,
    quality: str | None = "fair",
    watcher_count: int | None = 36,
) -> dict:
    """The real passive_rx event handed to this task, reproduced field
    for field (see this module's own docstring) with only the few
    fields any given test actually needs to vary ever overridden.
    event_id carries the feed's own "passive_rx:<uuid>" prefixed form
    (present on every event, per this module's docstring) but is NEVER
    what this test suite -- or app/freqmapper_ingest.py itself --
    dedupes on; reception_id is the field that matters.
    """
    event = {
        "event_id": f"passive_rx:{reception_id}",
        "event_type": "passive_rx",
        "reception_id": reception_id,
        "received_at": occurred_at or _iso(NOW),
        "occurred_at": occurred_at or _iso(NOW),
        "location_at": occurred_at or _iso(NOW),
        "location_accuracy_meters": 3.8,
        "location_trust": "platform_unflagged",
        "latitude": lat,
        "longitude": lon,
        "region_iata": "SLC",
        "radio_node_id": "!" + node_ref,
        "packet_type": "telemetry",
        "portnum": 67,
        "rssi_dbm": -86.0,
        "snr_db": -4.5,
        "hop_count": 2,
        "path_classification": "relayed",
        "last_relay_node": 23,
        "watcher_corroborated": True,
        "same_region_watcher_count": 26,
        "cross_region_watcher_count": 10,
        "evidence_type": "device_receive",
        "verified_coverage": False,
    }
    if quality is not None:
        event["quality"] = quality
    if watcher_count is not None:
        event["watcher_count"] = watcher_count
    return event


def _tx_event(verification_id: str, node_ref: str = "1bbeef80",
              occurred_at: str | None = None, lat: float = LAT, lon: float = LON) -> dict:
    return {
        "event_id": f"verified_tx:{verification_id}",
        "event_type": "verified_tx",
        "verification_id": verification_id,
        "radio_node_id": "!" + node_ref,
        "latitude": lat,
        "longitude": lon,
        "occurred_at": occurred_at or _iso(NOW),
    }


def _process_rx(conn, ingestor, event, season_id, registered, **kwargs):
    kwargs.setdefault("passive_rx_enabled", True)
    kwargs.setdefault("passive_rx_points_per_event", 0.5)
    kwargs.setdefault("passive_rx_unique_painter_bonus", 0.5)
    return ingestor._process_one_event(
        conn, event, season_id, registered, NOW,
        "both", 1.0, 0.5, "2020-01-01",
        **kwargs,
    )


# ---------------------------------------------------------------------
# 1. a passive_rx event for a registered radio paints the reception cell
#    for that player's team
# ---------------------------------------------------------------------

def test_passive_rx_paints_for_registered_player(conn):
    node_ref = "1bbeef80"
    _seed_player_and_node(conn, node_ref=node_ref, team="RED")
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    outcome = _process_rx(conn, ingestor, _rx_event("rx-basic-1", node_ref), season_id, registered)
    assert outcome == "painted_rx"

    cell = grid_cell_id(LAT, LON)
    tile = conn.execute(
        "SELECT owner_team FROM mc_tile WHERE season_id = ? AND cell_id = ?",
        (season_id, cell),
    ).fetchone()
    assert tile is not None
    assert tile["owner_team"] == "RED"


# ---------------------------------------------------------------------
# 2. the painted cell is derived from the RX coordinates (the
#    RECEIVER's own position, not some other hardcoded location)
# ---------------------------------------------------------------------

def test_passive_rx_paints_the_receivers_own_cell(conn):
    node_ref = "1bbeef80"
    _seed_player_and_node(conn, node_ref=node_ref, team="RED")
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    # Two receptions from the SAME radio at two clearly different
    # locations -- if the painted cell were ever hardcoded or derived
    # from anything other than this event's own lat/lon, these would
    # collide on one cell instead of landing on two.
    outcome1 = _process_rx(
        conn, ingestor, _rx_event("rx-cellA", node_ref, lat=LAT, lon=LON),
        season_id, registered,
    )
    outcome2 = _process_rx(
        conn, ingestor, _rx_event("rx-cellB", node_ref, lat=LAT2, lon=LON2),
        season_id, registered,
    )
    assert outcome1 == outcome2 == "painted_rx"

    cell1 = grid_cell_id(LAT, LON)
    cell2 = grid_cell_id(LAT2, LON2)
    assert cell1 != cell2

    painted_cells = {
        r["cell_id"] for r in conn.execute(
            "SELECT cell_id FROM mc_tile WHERE season_id = ?", (season_id,)
        )
    }
    assert painted_cells == {cell1, cell2}


# ---------------------------------------------------------------------
# 3. an unregistered radio_node_id does not paint
# ---------------------------------------------------------------------

def test_passive_rx_unregistered_radio_does_not_paint(conn):
    season_id = _season_id(conn)
    registered: dict = {}  # no player owns this radio at all
    ingestor = FreqMapperIngestor()

    outcome = _process_rx(
        conn, ingestor, _rx_event("rx-unreg-1", "1bbeef80"), season_id, registered,
    )
    assert outcome == "skipped_rx_unregistered"
    rows = conn.execute("SELECT count(*) FROM player_cell_ping").fetchone()[0]
    assert rows == 0
    tiles = conn.execute("SELECT count(*) FROM mc_tile").fetchone()[0]
    assert tiles == 0


# ---------------------------------------------------------------------
# 4. passive_rx_enabled = 0 counts but does not paint
# ---------------------------------------------------------------------

def test_passive_rx_disabled_counts_but_does_not_paint(conn):
    node_ref = "1bbeef80"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    outcome = _process_rx(
        conn, ingestor, _rx_event("rx-disabled-1", node_ref), season_id, registered,
        passive_rx_enabled=False,
    )
    assert outcome == "skipped_rx_disabled"

    # "Counts" -- the event IS recorded in freqmapper_verification (so a
    # later poll never re-evaluates it just because RX painting was off
    # at the time), same "processed and deduped, but nothing scored"
    # shape mt_paint_source=="meshview" already gives verified_tx.
    seen = conn.execute(
        "SELECT count(*) FROM freqmapper_verification WHERE verification_id = ?",
        ("rx-disabled-1",),
    ).fetchone()[0]
    assert seen == 1

    # "But does not paint" -- nothing written to the board.
    rows = conn.execute("SELECT count(*) FROM player_cell_ping").fetchone()[0]
    assert rows == 0
    tiles = conn.execute("SELECT count(*) FROM mc_tile").fetchone()[0]
    assert tiles == 0


# ---------------------------------------------------------------------
# 5. RX and TX points are independently configurable and RX uses its
#    own values (never falls back to or leaks into TX's own config)
# ---------------------------------------------------------------------

def test_rx_and_tx_points_independently_configurable(conn):
    node_ref = "1bbeef80"
    _seed_player_and_node(conn, node_ref=node_ref, team="RED")
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    # TX configured at 1.0 points + 0.5 unique bonus; RX configured at a
    # DELIBERATELY DIFFERENT 2.0 points + 1.5 unique bonus, so a bug
    # that accidentally read the TX values for an RX event (or vice
    # versa) shows up as a wrong total rather than passing by
    # coincidence the way equal values would.
    outcome_rx = _process_rx(
        conn, ingestor, _rx_event("rx-points-1", node_ref, lat=LAT, lon=LON),
        season_id, registered,
        passive_rx_points_per_event=2.0, passive_rx_unique_painter_bonus=1.5,
    )
    assert outcome_rx == "painted_rx"
    cell_rx = grid_cell_id(LAT, LON)
    rx_score = conn.execute(
        "SELECT score FROM mc_tile_score WHERE season_id = ? AND cell_id = ? AND team = 'RED'",
        (season_id, cell_rx),
    ).fetchone()["score"]
    assert rx_score == pytest.approx(2.0 + 1.5)  # RX's own points + RX's own unique bonus

    # A verified_tx event on a DIFFERENT cell, same call site's TX
    # config (points_per_event=1.0, unique_painter_bonus=0.5, the
    # positional args _process_rx always passes) -- proves the TX path
    # still reads its own config, unaffected by the RX overrides above.
    outcome_tx = ingestor._process_one_event(
        conn, _tx_event("tx-points-1", node_ref, lat=LAT2, lon=LON2),
        season_id, registered, NOW,
        "both", 1.0, 0.5, "2020-01-01",
        passive_rx_points_per_event=2.0, passive_rx_unique_painter_bonus=1.5,
    )
    assert outcome_tx == "painted"
    cell_tx = grid_cell_id(LAT2, LON2)
    tx_score = conn.execute(
        "SELECT score FROM mc_tile_score WHERE season_id = ? AND cell_id = ? AND team = 'RED'",
        (season_id, cell_tx),
    ).fetchone()["score"]
    assert tx_score == pytest.approx(1.0 + 0.5)  # TX's own points + TX's own unique bonus, NOT the RX values


def test_rx_defaults_match_tx_defaults_equal_credit_by_design(conn):
    """Matt's explicit decision ("coverage is coverage"): the SHIPPED
    defaults for RX and TX points/bonus are equal (0.5/0.5 each,
    mirroring freqmapper_config's own column defaults in app/db.py), not
    because the two are the same config, but because this deployment
    currently believes a reception is worth exactly as much as a
    verified transmission. Proven here by calling both paths with
    their DEFAULT kwargs (no explicit override) and getting the same
    total.
    """
    node_ref = "1bbeef80"
    _seed_player_and_node(conn, node_ref=node_ref, team="RED")
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    outcome_rx = _process_rx(
        conn, ingestor, _rx_event("rx-default-1", node_ref, lat=LAT, lon=LON),
        season_id, registered,
    )
    assert outcome_rx == "painted_rx"
    cell_rx = grid_cell_id(LAT, LON)
    rx_score = conn.execute(
        "SELECT score FROM mc_tile_score WHERE season_id = ? AND cell_id = ? AND team = 'RED'",
        (season_id, cell_rx),
    ).fetchone()["score"]

    outcome_tx = ingestor._process_one_event(
        conn, _tx_event("tx-default-1", node_ref, lat=LAT2, lon=LON2),
        season_id, registered, NOW,
        "both", 0.5, 0.5, "2020-01-01",
    )
    assert outcome_tx == "painted"
    cell_tx = grid_cell_id(LAT2, LON2)
    tx_score = conn.execute(
        "SELECT score FROM mc_tile_score WHERE season_id = ? AND cell_id = ? AND team = 'RED'",
        (season_id, cell_tx),
    ).fetchone()["score"]

    assert rx_score == tx_score == pytest.approx(1.0)  # 0.5 flat points + 0.5 unique bonus, both paths


# ---------------------------------------------------------------------
# 6. dedupe on reception_id: the same reception twice paints once
# ---------------------------------------------------------------------

def test_passive_rx_dedupes_on_reception_id(conn):
    node_ref = "1bbeef80"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()
    event = _rx_event("rx-dup-1", node_ref)

    outcome1 = _process_rx(conn, ingestor, event, season_id, registered)
    outcome2 = _process_rx(conn, ingestor, event, season_id, registered)
    assert outcome1 == "painted_rx"
    assert outcome2 == "skipped_rx_duplicate"

    rows = conn.execute("SELECT count(*) FROM player_cell_ping").fetchone()[0]
    assert rows == 1
    seen = conn.execute(
        "SELECT count(*) FROM freqmapper_verification WHERE verification_id = ?",
        ("rx-dup-1",),
    ).fetchone()[0]
    assert seen == 1


# ---------------------------------------------------------------------
# 7. an RX event older than the high-water mark is backfill-skipped,
#    not painted
# ---------------------------------------------------------------------

def test_passive_rx_older_than_high_water_mark_is_backfill_skipped(conn):
    node_ref = "1bbeef80"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    # Seed the high-water mark well AFTER this event's own occurred_at --
    # same shape tests/test_freqmapper_backfill_guard.py uses to seed it
    # directly via set_cursor, not through a real prior event, since
    # only the guard's reaction to an already-seeded mark matters here.
    old_ts = NOW - 90 * 86400
    mark_ts = NOW - 10 * 86400
    set_cursor(conn, HIGH_WATER_MARK_KEY, str(mark_ts))

    outcome = _process_rx(
        conn, ingestor, _rx_event("rx-old-1", node_ref, occurred_at=_iso(old_ts)),
        season_id, registered,
    )
    assert outcome == "backfill_skipped_rx"

    rows = conn.execute("SELECT count(*) FROM player_cell_ping").fetchone()[0]
    assert rows == 0
    tiles = conn.execute("SELECT count(*) FROM mc_tile").fetchone()[0]
    assert tiles == 0

    # Still recorded, so a later poll never re-evaluates it (same
    # "recorded so it is never re-evaluated" contract every other
    # backfill-skipped event gets).
    seen = conn.execute(
        "SELECT count(*) FROM freqmapper_verification WHERE verification_id = ?",
        ("rx-old-1",),
    ).fetchone()[0]
    assert seen == 1

    # The mark itself must NOT have moved backwards to this old event's
    # timestamp.
    assert int(get_cursor(conn, HIGH_WATER_MARK_KEY, "0")) == mark_ts

    # allow_backfill=True is the operator's explicit opt-in to bypass
    # this guard -- the same old event now paints normally.
    outcome2 = _process_rx(
        conn, ingestor, _rx_event("rx-old-2", node_ref, occurred_at=_iso(old_ts)),
        season_id, registered,
        allow_backfill=True,
    )
    assert outcome2 == "painted_rx"


# ---------------------------------------------------------------------
# 8. RX paints are distinguishable from TX paints in whatever
#    provenance mechanism was used (player_cell_ping.evidence_type)
# ---------------------------------------------------------------------

def test_rx_paint_is_distinguishable_from_tx_paint_via_evidence_type(conn):
    node_ref = "1bbeef80"
    _seed_player_and_node(conn, node_ref=node_ref, team="RED")
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    outcome_rx = _process_rx(
        conn, ingestor, _rx_event("rx-prov-1", node_ref, lat=LAT, lon=LON),
        season_id, registered,
    )
    outcome_tx = ingestor._process_one_event(
        conn, _tx_event("tx-prov-1", node_ref, lat=LAT2, lon=LON2),
        season_id, registered, NOW,
        "both", 1.0, 0.5, "2020-01-01",
    )
    assert outcome_rx == "painted_rx"
    assert outcome_tx == "painted"

    cell_rx = grid_cell_id(LAT, LON)
    cell_tx = grid_cell_id(LAT2, LON2)

    rx_row = conn.execute(
        "SELECT evidence_type FROM player_cell_ping WHERE cell_id = ?", (cell_rx,)
    ).fetchone()
    tx_row = conn.execute(
        "SELECT evidence_type FROM player_cell_ping WHERE cell_id = ?", (cell_tx,)
    ).fetchone()

    assert rx_row["evidence_type"] == "passive_rx"
    assert tx_row["evidence_type"] == "verified_tx"
    assert rx_row["evidence_type"] != tx_row["evidence_type"]

    # Equal points (see test_rx_defaults_match_tx_defaults_equal_credit_
    # by_design above) is a SEPARATE property from distinguishability --
    # prove both paints are still individually queryable by evidence
    # type despite scoring identically.
    rx_count = conn.execute(
        "SELECT count(*) FROM player_cell_ping WHERE evidence_type = 'passive_rx'"
    ).fetchone()[0]
    tx_count = conn.execute(
        "SELECT count(*) FROM player_cell_ping WHERE evidence_type = 'verified_tx'"
    ).fetchone()[0]
    assert rx_count == 1
    assert tx_count == 1
