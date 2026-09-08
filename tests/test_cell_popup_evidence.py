"""Tests for cell_detail_for()'s reception/verification evidence (app/
mc_api.py): each recent_captures[] entry enriched with the
player_cell_ping row that caused it (evidence_type, watcher_count,
watcher_corroborated, quality), plus a cell-level evidence_summary --
see Matt's original ask ("how do i know its an rx square or rx cap by
the lines in the popup box?").

Same fixture pattern as tests/test_mc_api_cell_park.py: an in-memory
conn with the real schema, monkeypatched onto app.mc_api.connect, and
cell_detail_for() called directly.
"""
from __future__ import annotations

import time

import app.mc_api as mc_api_module
from app.mc_api import cell_detail_for

NOW = int(time.time())
CELL = "10000_-10000"


def _season(conn, protocol="mc"):
    cur = conn.execute(
        "INSERT INTO mc_season(protocol, started_at, ends_at, status) VALUES (?,?,?,?)",
        (protocol, NOW - 1000, NOW + 1_000_000, "active"),
    )
    return cur.lastrowid


def _tile(conn, season_id, cell_id, team="RED"):
    conn.execute(
        "INSERT INTO mc_tile(season_id, cell_id, owner_team, last_player_id, last_report_ts) "
        "VALUES (?,?,?,1,?)",
        (season_id, cell_id, team, NOW),
    )


def _player(conn, player_id, name, team="RED"):
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) "
        "VALUES (?,?,?,?) ON CONFLICT(player_id) DO NOTHING",
        (player_id, name, team, NOW),
    )


def _capture(conn, season_id, cell_id, ts, by_player_id, by_team, from_team=None):
    conn.execute(
        "INSERT INTO mc_tile_capture_log(season_id, cell_id, ts, by_player_id, by_team, from_team) "
        "VALUES (?,?,?,?,?,?)",
        (season_id, cell_id, ts, by_player_id, by_team, from_team),
    )


def _ping(
    conn, protocol, cell_id, ts, player_id, *,
    evidence_type=None, watcher_count=None, watcher_corroborated=None, quality=None,
):
    conn.execute(
        "INSERT INTO player_cell_ping("
        "  player_id, protocol, cell_id, ts, seen_at, evidence_type, "
        "  watcher_count, watcher_corroborated, quality"
        ") VALUES (?,?,?,?,?,?,?,?,?)",
        (player_id, protocol, cell_id, ts, ts, evidence_type, watcher_count,
         watcher_corroborated, quality),
    )


def test_capture_from_passive_rx_paint_reports_its_evidence(conn, monkeypatch):
    monkeypatch.setattr(mc_api_module, "connect", lambda: conn)
    season_id = _season(conn)
    _tile(conn, season_id, CELL)
    _player(conn, 1, "rx-tester")
    _capture(conn, season_id, CELL, NOW, 1, "RED")
    _ping(
        conn, "mc", CELL, NOW, 1,
        evidence_type="passive_rx", watcher_count=4, watcher_corroborated=1, quality="good",
    )

    detail = cell_detail_for("mc", CELL)
    [cap] = detail["recent_captures"]
    assert cap["evidence_type"] == "passive_rx"
    assert cap["watcher_count"] == 4
    assert cap["watcher_corroborated"] is True
    assert cap["quality"] == "good"


def test_capture_from_verified_tx_paint_reports_its_evidence(conn, monkeypatch):
    monkeypatch.setattr(mc_api_module, "connect", lambda: conn)
    season_id = _season(conn)
    _tile(conn, season_id, CELL)
    _player(conn, 1, "tx-tester")
    _capture(conn, season_id, CELL, NOW, 1, "RED")
    _ping(
        conn, "mc", CELL, NOW, 1,
        evidence_type="verified_tx", watcher_count=2, watcher_corroborated=1, quality="good",
    )

    detail = cell_detail_for("mc", CELL)
    [cap] = detail["recent_captures"]
    assert cap["evidence_type"] == "verified_tx"
    assert cap["watcher_count"] == 2


def test_capture_with_no_matching_ping_row_returns_nulls_and_stays_present(conn, monkeypatch):
    """Older data, a pruned ping, or a meshview paint -- none of these
    write a player_cell_ping row, but the capture itself is real and
    must not be dropped just because the LEFT JOIN found nothing."""
    monkeypatch.setattr(mc_api_module, "connect", lambda: conn)
    season_id = _season(conn)
    _tile(conn, season_id, CELL)
    _player(conn, 1, "no-ping-tester")
    _capture(conn, season_id, CELL, NOW, 1, "RED")
    # Deliberately no _ping() call.

    detail = cell_detail_for("mc", CELL)
    [cap] = detail["recent_captures"]
    assert cap["by_team"] == "RED"
    assert cap["evidence_type"] is None
    assert cap["watcher_count"] is None
    assert cap["watcher_corroborated"] is None
    assert cap["quality"] is None


def test_evidence_summary_counts_a_mix_of_evidence_types(conn, monkeypatch):
    monkeypatch.setattr(mc_api_module, "connect", lambda: conn)
    season_id = _season(conn)
    _tile(conn, season_id, CELL)
    _player(conn, 1, "p1")
    _player(conn, 2, "p2")
    _player(conn, 3, "p3")

    # Two passive_rx paints (watcher_count 3 and 7 -- max should be 7),
    # one verified_tx paint, one paint predating evidence_type (NULL).
    _ping(conn, "mc", CELL, NOW - 300, 1, evidence_type="passive_rx", watcher_count=3, watcher_corroborated=1)
    _ping(conn, "mc", CELL, NOW - 200, 2, evidence_type="passive_rx", watcher_count=7, watcher_corroborated=1)
    _ping(conn, "mc", CELL, NOW - 100, 3, evidence_type="verified_tx", watcher_count=1, watcher_corroborated=1)
    _ping(conn, "mc", CELL, NOW, 1, evidence_type=None)

    detail = cell_detail_for("mc", CELL)
    summary = detail["evidence_summary"]
    assert summary["passive_rx_count"] == 2
    assert summary["verified_tx_count"] == 1
    assert summary["other_count"] == 1
    assert summary["max_watcher_count"] == 7


def test_meshcore_cell_with_all_null_evidence_summarizes_to_zeros(conn, monkeypatch):
    """MeshCore paints never populate evidence_type at all -- the
    summary must still come back cleanly (all real paints counted as
    'other', no crash), not omit the field or blow up on empty
    aggregates."""
    monkeypatch.setattr(mc_api_module, "connect", lambda: conn)
    season_id = _season(conn, protocol="mc")
    _tile(conn, season_id, CELL)
    _player(conn, 1, "mc-tester")
    _capture(conn, season_id, CELL, NOW, 1, "RED")
    _ping(conn, "mc", CELL, NOW, 1, evidence_type=None)

    detail = cell_detail_for("mc", CELL)
    summary = detail["evidence_summary"]
    assert summary == {
        "verified_tx_count": 0,
        "passive_rx_count": 0,
        "other_count": 1,
        "max_watcher_count": None,
    }
    [cap] = detail["recent_captures"]
    assert cap["evidence_type"] is None


def test_evidence_summary_all_zero_when_cell_has_no_pings_at_all(conn, monkeypatch):
    monkeypatch.setattr(mc_api_module, "connect", lambda: conn)
    season_id = _season(conn)
    _tile(conn, season_id, CELL)

    detail = cell_detail_for("mc", CELL)
    assert detail["evidence_summary"] == {
        "verified_tx_count": 0,
        "passive_rx_count": 0,
        "other_count": 0,
        "max_watcher_count": None,
    }
