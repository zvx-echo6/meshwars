"""FreqMapper ingest records the player's last Meshtastic position
(player_last_fix, protocol 'mt') when an event paints -- the row the
account page's "Last position heard" (mc_api.find_for last_position_ts)
reads. Forward-only on the event's own timestamp; events that do not
paint (duplicate, unregistered, backfill-skipped) never write it.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from app import mc_api, mc_scoring
from app.db import set_cursor
from app.freqmapper_ingest import HIGH_WATER_MARK_KEY, FreqMapperIngestor
from app.grid import cell_id as grid_cell_id

NOW = int(time.time())
PROTOCOL = "mt"
NODE = "1bbeef80"
LAT, LON = 43.0, -116.0
LAT2, LON2 = 43.05, -116.2


def _seed(conn, player_id=1, node_ref=NODE, team="RED"):
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


def _tx(vid, ts, lat=LAT, lon=LON, node=NODE):
    return {
        "event_id": f"verified_tx:{vid}", "event_type": "verified_tx",
        "verification_id": vid, "radio_node_id": "!" + node,
        "latitude": lat, "longitude": lon, "occurred_at": _iso(ts),
    }


def _rx(rid, ts, lat=LAT, lon=LON, node=NODE):
    return {
        "event_id": f"passive_rx:{rid}", "event_type": "passive_rx",
        "reception_id": rid, "radio_node_id": "!" + node,
        "latitude": lat, "longitude": lon,
        "occurred_at": _iso(ts), "received_at": _iso(ts),
        "verified_coverage": False,
    }


def _process(conn, event, sid, registered):
    return FreqMapperIngestor()._process_one_event(
        conn, event, sid, registered, NOW, "freqmapper", 1.0, 0.5, "2020-01-01",
    )


def _fix(conn, pid=1):
    return conn.execute(
        "SELECT cell_id, ts FROM player_last_fix WHERE player_id = ? AND protocol = ?",
        (pid, PROTOCOL),
    ).fetchone()


def test_painted_verified_tx_creates_last_fix(conn):
    _seed(conn)
    sid = _season_id(conn)
    ts = NOW - 60
    assert _process(conn, _tx("a", ts), sid, {NODE: (1, "RED")}) == "painted"
    row = _fix(conn)
    assert row["ts"] == ts
    assert row["cell_id"] == grid_cell_id(LAT, LON)


def test_painted_passive_rx_records_listeners_own_position(conn):
    _seed(conn)
    sid = _season_id(conn)
    ts = NOW - 60
    assert _process(conn, _rx("r1", ts, LAT2, LON2), sid, {NODE: (1, "RED")}) == "painted_rx"
    row = _fix(conn)
    assert row["ts"] == ts
    assert row["cell_id"] == grid_cell_id(LAT2, LON2)


def test_newer_event_moves_fix_forward(conn):
    _seed(conn)
    sid = _season_id(conn)
    reg = {NODE: (1, "RED")}
    _process(conn, _tx("a", NOW - 120), sid, reg)
    _process(conn, _tx("b", NOW - 30, LAT2, LON2), sid, reg)
    row = _fix(conn)
    assert row["ts"] == NOW - 30
    assert row["cell_id"] == grid_cell_id(LAT2, LON2)


def test_older_painted_event_does_not_move_fix_backwards(conn):
    _seed(conn)
    sid = _season_id(conn)
    reg = {NODE: (1, "RED")}
    _process(conn, _tx("new", NOW - 30), sid, reg)
    # Older than the stored fix but not backfill (the mark was advanced
    # only by the first event's own time, so use allow-through by
    # clearing the mark).
    conn.execute("DELETE FROM cursor WHERE k = ?", (HIGH_WATER_MARK_KEY,))
    out = _process(conn, _tx("old", NOW - 600, LAT2, LON2), sid, reg)
    assert out == "painted"
    row = _fix(conn)
    assert row["ts"] == NOW - 30
    assert row["cell_id"] == grid_cell_id(LAT, LON)


def test_duplicate_does_not_write_fix(conn):
    _seed(conn)
    sid = _season_id(conn)
    reg = {NODE: (1, "RED")}
    ev = _tx("dup", NOW - 100)
    assert _process(conn, ev, sid, reg) == "painted"
    conn.execute("UPDATE player_last_fix SET ts = ? WHERE player_id = 1", (NOW - 500,))
    assert _process(conn, ev, sid, reg) == "skipped_duplicate"
    assert _fix(conn)["ts"] == NOW - 500


def test_unregistered_does_not_write_fix(conn):
    _seed(conn)
    sid = _season_id(conn)
    out = _process(conn, _tx("u", NOW - 10, node="deadbeef"), sid, {NODE: (1, "RED")})
    assert out == "skipped_unregistered"
    assert _fix(conn) is None


def test_backfill_skipped_does_not_write_fix(conn):
    _seed(conn)
    sid = _season_id(conn)
    set_cursor(conn, HIGH_WATER_MARK_KEY, str(NOW))
    out = _process(conn, _tx("hist", NOW - 3600), sid, {NODE: (1, "RED")})
    assert out == "backfill_skipped"
    assert _fix(conn) is None


def test_find_for_last_position_ts_after_freqmapper_paint(conn, monkeypatch):
    _seed(conn)
    sid = _season_id(conn)

    class _NoClose:
        def __init__(self, c):
            self._c = c

        def __getattr__(self, n):
            return getattr(self._c, n)

        def close(self):
            pass

    monkeypatch.setattr(mc_api, "connect", lambda: _NoClose(conn))
    assert mc_api.find_for(PROTOCOL, "player-1")["last_position_ts"] is None
    ts = NOW - 45
    assert _process(conn, _tx("f", ts), sid, {NODE: (1, "RED")}) == "painted"
    assert mc_api.find_for(PROTOCOL, "player-1")["last_position_ts"] == ts
