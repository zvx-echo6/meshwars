"""Tests for FreqMapper's "capture signal" fields on player_cell_ping --
the thirteen columns (app/db.py) that record every measurement a
verified_tx or passive_rx event carries beyond the bare fact of the
paint itself: how many independent Watchers verified/corroborated it
(watcher_count, same_region_watcher_count, cross_region_watcher_count,
watcher_corroborated -- on BOTH event types), and, for passive_rx only,
how strong the reception was and how it got there (quality, rssi_dbm,
snr_db, hop_count, path_classification, last_relay_node, packet_type,
portnum, location_accuracy_meters).

Matt's decision governs this whole file, in two stages. First, on the
two fields this feature started with: "we can use the watcher count and
the quality. in fact we should enter it in the capture, but what we
should NOT do is change the scoring weight. at its core, meshwars is a
coverage mapper, so we should honor that." Then, once the full shape of
what FreqMapper actually reports was in view: "lets store it all, its
not that heavy" -- which is why this file tests all thirteen columns,
not just the two Matt named first.

This file is organized around the properties that decision implies:

  A. RECORDING -- a verified_tx paint records its four watcher fields
     and leaves every RX-only column NULL (the verified_tx feed has no
     such fields at all); a passive_rx paint records the full set,
     verbatim (test_verified_tx_paint_records_*,
     test_passive_rx_paint_records_*).
  B. NEVER INVENT A VALUE -- a field the payload omits, or sends as an
     explicit JSON null, lands as SQL NULL, never 0, never False, never
     a placeholder string like "unknown" (test_missing_and_null_fields_
     record_as_null_*).
  C. SCORING NEVER MOVES -- points are identical across a sweep of
     watcher_count (both event types), quality, and rssi_dbm/snr_db/
     hop_count, because nothing in app/freqmapper_ingest.py reads any of
     these thirteen columns back for scoring purposes, ever
     (test_points_identical_across_*_sweep).

Uses the shared in-memory `conn` fixture (tests/conftest.py) for section
A/B (single paints, no HTTP layer needed) and independent per-value
in-memory databases for section C (same _fresh_conn shape
tests/test_freqmapper_combined_feed.py's own flat-scoring test uses --
see that test's own comment for why: a SECOND sequential paint in one
database picks up mc_scoring.apply_paint's own score decay over the
elapsed time between paints, which has nothing to do with the field
being swept and would only make the comparison noisier).
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

import pytest

from app import mc_scoring
from app.db import MIGRATIONS, SCHEMA
from app.freqmapper_ingest import FreqMapperIngestor
from app.grid import cell_id as grid_cell_id

NOW = int(time.time())
PROTOCOL = "mt"
LAT, LON = 43.0, -116.0  # well within settings.play_area_* (see app/config.py)

# A sentinel distinct from None, so a test can ask for a field to be
# OMITTED from the event dict entirely (as opposed to sent as an
# explicit JSON null, which Python spells the same as "no value" unless
# the two are kept apart like this) -- both must record as SQL NULL
# (see section B), but they are two different payload shapes and this
# file tests both deliberately, not just one standing in for the other.
_OMIT = object()


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


def _tx_event(
    verification_id: str,
    node_ref: str = "0a0a0a0a",
    *,
    occurred_at: str | None = None,
    lat: float = LAT,
    lon: float = LON,
    watcher_count=12,
    same_region_watcher_count=9,
    cross_region_watcher_count=3,
    watcher_corroborated=True,
) -> dict:
    """A verified_tx event -- realistic default watcher-field values,
    each individually overridable (pass _OMIT to leave the key out of
    the event dict entirely, or None to send it as an explicit JSON
    null). verified_tx has no rx-only fields at all (quality, rssi_dbm,
    etc.) -- there is nothing here to override for them because the
    live feed never sends them on this event type.
    """
    event = {
        "event_id": f"verified_tx:{verification_id}",
        "event_type": "verified_tx",
        "verification_id": verification_id,
        "radio_node_id": "!" + node_ref,
        "latitude": lat,
        "longitude": lon,
        "occurred_at": occurred_at or _iso(NOW),
    }
    for key, value in (
        ("watcher_count", watcher_count),
        ("same_region_watcher_count", same_region_watcher_count),
        ("cross_region_watcher_count", cross_region_watcher_count),
        ("watcher_corroborated", watcher_corroborated),
    ):
        if value is not _OMIT:
            event[key] = value
    return event


def _rx_event(
    reception_id: str,
    node_ref: str = "0a0a0a0a",
    *,
    occurred_at: str | None = None,
    lat: float = LAT,
    lon: float = LON,
    watcher_count=36,
    same_region_watcher_count=26,
    cross_region_watcher_count=10,
    watcher_corroborated=True,
    quality="fair",
    rssi_dbm=-86.0,
    snr_db=-4.5,
    hop_count=2,
    path_classification="relayed",
    last_relay_node=23,
    packet_type="telemetry",
    portnum=67,
    location_accuracy_meters=3.8,
) -> dict:
    """A passive_rx event -- default values matching the real event
    handed to tests/test_freqmapper_passive_rx.py's own _rx_event
    (fetched live against FreqMapper's combined feed on 2026-09-07), so
    this file exercises the real event shape, not a synthetic minimal
    fixture. Every capture-signal field is individually overridable
    (_OMIT to leave the key out of the event dict entirely, None to send
    it as an explicit JSON null).
    """
    event = {
        "event_id": f"passive_rx:{reception_id}",
        "event_type": "passive_rx",
        "reception_id": reception_id,
        "radio_node_id": "!" + node_ref,
        "latitude": lat,
        "longitude": lon,
        "occurred_at": occurred_at or _iso(NOW),
    }
    for key, value in (
        ("watcher_count", watcher_count),
        ("same_region_watcher_count", same_region_watcher_count),
        ("cross_region_watcher_count", cross_region_watcher_count),
        ("watcher_corroborated", watcher_corroborated),
        ("quality", quality),
        ("rssi_dbm", rssi_dbm),
        ("snr_db", snr_db),
        ("hop_count", hop_count),
        ("path_classification", path_classification),
        ("last_relay_node", last_relay_node),
        ("packet_type", packet_type),
        ("portnum", portnum),
        ("location_accuracy_meters", location_accuracy_meters),
    ):
        if value is not _OMIT:
            event[key] = value
    return event


_CAPTURE_COLUMNS = (
    "evidence_type", "watcher_count", "same_region_watcher_count",
    "cross_region_watcher_count", "watcher_corroborated", "quality",
    "rssi_dbm", "snr_db", "hop_count", "path_classification",
    "last_relay_node", "packet_type", "portnum", "location_accuracy_meters",
)


def _ping_row(conn, player_id: int, cell: str, ts: int) -> dict:
    row = conn.execute(
        f"SELECT {', '.join(_CAPTURE_COLUMNS)} FROM player_cell_ping "
        " WHERE player_id = ? AND protocol = ? AND cell_id = ? AND ts = ?",
        (player_id, PROTOCOL, cell, ts),
    ).fetchone()
    assert row is not None, "expected a player_cell_ping row for this paint"
    return dict(row)


# ---------------------------------------------------------------------
# A. Recording
# ---------------------------------------------------------------------

def test_verified_tx_paint_records_watcher_fields_and_null_quality(conn):
    """A verified_tx paint records its four watcher fields and leaves
    every RX-only column NULL -- the verified_tx feed carries no
    quality/rssi_dbm/snr_db/hop_count/path_classification/
    last_relay_node/packet_type/portnum/location_accuracy_meters fields
    at all, so there is nothing for any of those nine columns to record.
    """
    node_ref = "0a0a0a0a"
    cell = grid_cell_id(LAT, LON)
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    event = _tx_event(
        "watcher-fields-1", node_ref,
        watcher_count=12, same_region_watcher_count=9,
        cross_region_watcher_count=3, watcher_corroborated=True,
    )
    outcome = ingestor._process_one_event(
        conn, event, season_id, registered, NOW, "both", 1.0, 0.5, "2020-01-01",
    )
    assert outcome == "painted"

    ts = int(datetime.fromisoformat(event["occurred_at"]).timestamp())
    row = _ping_row(conn, 1, cell, ts)
    assert row["evidence_type"] == "verified_tx"
    assert row["watcher_count"] == 12
    assert row["same_region_watcher_count"] == 9
    assert row["cross_region_watcher_count"] == 3
    assert row["watcher_corroborated"] == 1
    for col in (
        "quality", "rssi_dbm", "snr_db", "hop_count", "path_classification",
        "last_relay_node", "packet_type", "portnum", "location_accuracy_meters",
    ):
        assert row[col] is None, f"{col} must be NULL on a verified_tx row"


def test_passive_rx_paint_records_full_capture_signal(conn):
    """A passive_rx paint records EVERY one of the thirteen
    capture-signal fields, verbatim off the event -- this is the "lets
    store it all" case: nothing on this event type is left unrecorded.
    """
    node_ref = "0a0a0a0a"
    cell = grid_cell_id(LAT, LON)
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    event = _rx_event("full-capture-1", node_ref)
    outcome = ingestor._process_one_event(
        conn, event, season_id, registered, NOW, "both", 1.0, 0.5, "2020-01-01",
    )
    assert outcome == "painted_rx"

    ts = int(datetime.fromisoformat(event["occurred_at"]).timestamp())
    row = _ping_row(conn, 1, cell, ts)
    assert row["evidence_type"] == "passive_rx"
    assert row["watcher_count"] == 36
    assert row["same_region_watcher_count"] == 26
    assert row["cross_region_watcher_count"] == 10
    assert row["watcher_corroborated"] == 1
    assert row["quality"] == "fair"
    assert row["rssi_dbm"] == pytest.approx(-86.0)
    assert row["snr_db"] == pytest.approx(-4.5)
    assert row["hop_count"] == 2
    assert row["path_classification"] == "relayed"
    assert row["last_relay_node"] == 23
    assert row["packet_type"] == "telemetry"
    assert row["portnum"] == 67
    assert row["location_accuracy_meters"] == pytest.approx(3.8)


# ---------------------------------------------------------------------
# B. Never invent a value
# ---------------------------------------------------------------------

def test_missing_and_null_fields_record_as_null_verified_tx(conn):
    """A verified_tx event with watcher_count omitted entirely, and one
    with it sent as an explicit JSON null, both record NULL -- never 0,
    never a guess at "at least one Watcher."
    """
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    for label, watcher_count, lat_offset in (("omitted", _OMIT, 0.01), ("null", None, 0.02)):
        lat = LAT + lat_offset
        cell = grid_cell_id(lat, LON)
        event = _tx_event(
            f"missing-{label}", node_ref, lat=lat,
            watcher_count=watcher_count, same_region_watcher_count=_OMIT,
            cross_region_watcher_count=_OMIT, watcher_corroborated=_OMIT,
        )
        outcome = ingestor._process_one_event(
            conn, event, season_id, registered, NOW, "both", 1.0, 0.5, "2020-01-01",
        )
        assert outcome == "painted"
        ts = int(datetime.fromisoformat(event["occurred_at"]).timestamp())
        row = _ping_row(conn, 1, cell, ts)
        assert row["watcher_count"] is None, label
        assert row["same_region_watcher_count"] is None, label
        assert row["cross_region_watcher_count"] is None, label
        assert row["watcher_corroborated"] is None, label


def test_missing_and_null_fields_record_as_null_passive_rx(conn):
    """A passive_rx event where several fields are omitted and others
    sent as explicit null -- each records NULL independently; fields
    that WERE sent are still recorded normally alongside them (a partial
    payload does not blank out the fields it did include).
    """
    node_ref = "0a0a0a0a"
    cell = grid_cell_id(LAT, LON)
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    event = _rx_event(
        "partial-payload-1", node_ref,
        watcher_count=_OMIT,
        watcher_corroborated=_OMIT,
        quality=None,          # explicit null -- valid per FreqMapper's own docs
        rssi_dbm=None,
        snr_db=None,            # snr_db can legitimately be null even on a real RX event
        hop_count=_OMIT,
        last_relay_node=_OMIT,
        # path_classification, packet_type, portnum, location_accuracy_meters,
        # same_region_watcher_count, cross_region_watcher_count keep their
        # normal default values below -- proving they are NOT blanked out
        # just because sibling fields on the same event are missing/null.
    )
    outcome = ingestor._process_one_event(
        conn, event, season_id, registered, NOW, "both", 1.0, 0.5, "2020-01-01",
    )
    assert outcome == "painted_rx"
    ts = int(datetime.fromisoformat(event["occurred_at"]).timestamp())
    row = _ping_row(conn, 1, cell, ts)

    # Missing/null fields -> NULL, never 0/False/"unknown".
    assert row["watcher_count"] is None
    assert row["watcher_corroborated"] is None
    assert row["quality"] is None
    assert row["rssi_dbm"] is None
    assert row["snr_db"] is None
    assert row["hop_count"] is None
    assert row["last_relay_node"] is None

    # Fields the event DID send are recorded normally regardless.
    assert row["same_region_watcher_count"] == 26
    assert row["cross_region_watcher_count"] == 10
    assert row["path_classification"] == "relayed"
    assert row["packet_type"] == "telemetry"
    assert row["portnum"] == 67
    assert row["location_accuracy_meters"] == pytest.approx(3.8)


def test_wrong_typed_fields_record_as_null_not_coerced(conn):
    """A payload that sends the wrong JSON type for a field (a string
    where a number is expected, a number where a boolean is expected)
    records NULL rather than silently coercing it -- see
    app/freqmapper_ingest.py's _event_int/_event_bool_as_int/etc., which
    reject a type mismatch outright instead of guessing at the sender's
    intent.
    """
    node_ref = "0a0a0a0a"
    cell = grid_cell_id(LAT, LON)
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    event = _rx_event(
        "wrong-types-1", node_ref,
        watcher_count="not-a-number",
        watcher_corroborated=1,  # an int, not a bool -- must not become 1 by luck
        hop_count=True,          # a bool is not an integer count either
    )
    outcome = ingestor._process_one_event(
        conn, event, season_id, registered, NOW, "both", 1.0, 0.5, "2020-01-01",
    )
    assert outcome == "painted_rx"
    ts = int(datetime.fromisoformat(event["occurred_at"]).timestamp())
    row = _ping_row(conn, 1, cell, ts)
    assert row["watcher_count"] is None
    assert row["watcher_corroborated"] is None
    assert row["hop_count"] is None


# ---------------------------------------------------------------------
# C. Scoring never moves
# ---------------------------------------------------------------------

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


def _score_for_tx(watcher_count) -> float:
    """Paint one fresh verified_tx event, in its own fresh database, and
    return the resulting team score -- see this file's own module
    docstring for why a fresh database per value, not a second
    sequential paint in one database (score decay would confound the
    comparison).
    """
    c = _fresh_conn()
    node_ref = "0a0a0a0a"
    cell = grid_cell_id(LAT, LON)
    _seed_player_and_node(c, node_ref=node_ref)
    season_id = _season_id(c)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()
    event = _tx_event(f"sweep-{watcher_count}", node_ref, watcher_count=watcher_count)
    outcome = ingestor._process_one_event(
        c, event, season_id, registered, NOW, "both", 1.0, 0.5, "2020-01-01",
    )
    assert outcome == "painted"
    row = c.execute(
        "SELECT score FROM mc_tile_score WHERE season_id = ? AND cell_id = ? AND team = 'RED'",
        (season_id, cell),
    ).fetchone()
    score = row["score"]
    c.close()
    return score


def _score_for_rx(**overrides) -> float:
    """Same shape as _score_for_tx above, for a passive_rx event."""
    c = _fresh_conn()
    node_ref = "0a0a0a0a"
    cell = grid_cell_id(LAT, LON)
    _seed_player_and_node(c, node_ref=node_ref)
    season_id = _season_id(c)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()
    event = _rx_event("sweep", node_ref, **overrides)
    outcome = ingestor._process_one_event(
        c, event, season_id, registered, NOW, "both", 1.0, 0.5, "2020-01-01",
    )
    assert outcome == "painted_rx"
    row = c.execute(
        "SELECT score FROM mc_tile_score WHERE season_id = ? AND cell_id = ? AND team = 'RED'",
        (season_id, cell),
    ).fetchone()
    score = row["score"]
    c.close()
    return score


def test_points_identical_across_watcher_count_sweep_verified_tx():
    scores = {wc: _score_for_tx(wc) for wc in (1, 5, 50, _OMIT)}
    assert len(set(scores.values())) == 1, scores


def test_points_identical_across_watcher_count_sweep_passive_rx():
    scores = {wc: _score_for_rx(watcher_count=wc) for wc in (1, 5, 50, _OMIT)}
    assert len(set(scores.values())) == 1, scores


def test_points_identical_across_quality_sweep():
    scores = {q: _score_for_rx(quality=q) for q in ("strong", "fair", "weak", _OMIT)}
    assert len(set(scores.values())) == 1, scores


def test_points_identical_across_signal_metrics_sweep():
    """rssi_dbm, snr_db, and hop_count together -- a strong, clean,
    one-hop reception must score exactly the same as a weak, noisy,
    multi-hop one, or one where none of the three was even reported.
    """
    combos = (
        {"rssi_dbm": -40.0, "snr_db": 8.0, "hop_count": 0},
        {"rssi_dbm": -110.0, "snr_db": -18.0, "hop_count": 6},
        {"rssi_dbm": _OMIT, "snr_db": _OMIT, "hop_count": _OMIT},
    )
    scores = [_score_for_rx(**combo) for combo in combos]
    assert len(set(scores)) == 1, scores
