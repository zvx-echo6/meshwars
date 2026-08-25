"""Tests for the two Meshtastic game-integrity gates added 2026-08-25
(see app/config.py's mt_min_precision_bits/mt_max_speed_mps):

  A. Position precision: a packet whose Meshtastic precision_bits is
     missing or too coarse for a ~300m grid cell must be refused, not
     scored.
  B. Speed: a jump between a player's own consecutive fixes implying a
     physically impossible speed must be refused, mirroring the gate
     app/mc_ingest.py already has for MeshCore.

Like tests/test_place_scoring_ingest_hook.py, these drive the REAL
ingest path -- Ingestor._process_one_packet(), the exact call a live
meshview poll makes -- rather than testing app.grid/app.config in
isolation, because the risk this investigation is closing is exactly
"the gate exists somewhere but was never actually wired into ingest."
A FakeMeshviewClient stands in for the real HTTP client (only
packets_seen() is ever called on it here); everything else -- the
database, app.mc_scoring, app.place_scoring -- is the real, unmocked
code.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from app.config import settings
from app.db import MIGRATIONS, SCHEMA
from app.grid import cell_center, cell_id as grid_cell_id, distance_m
from app.ingest import Ingestor
from app import mc_scoring
from app.meshview_client import extract_position

NOW = int(time.time())
PROTOCOL = "mt"

# Well within the default play area (settings.play_area_*).
LAT, LON = 43.0, -116.0


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Same fixture as tests/test_place_scoring_ingest_hook.py -- a fresh
    on-disk sqlite file with the real schema, settings.db_path pointed at
    it. Ingestor._process_one_packet() calls app.db.connect()/WriteSession,
    both of which always open settings.db_path -- there is no way to hand
    it a connection directly, so this is the only way to exercise the
    real path.
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


def _seed_player_and_node(db_path, player_id=1, node_ref="0a0a0a0a", team="RED"):
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


def _season_id(db_path) -> int:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN IMMEDIATE")
    mc_scoring.maybe_roll_season(conn, NOW, PROTOCOL)
    sid = mc_scoring.ensure_active_season(conn, NOW, PROTOCOL)
    conn.execute("COMMIT")
    conn.close()
    return sid


def _packet(packet_id: int, node_id: int, lat: float, lon: float, ts: int,
            precision_bits: int | None) -> dict:
    """A packet shaped exactly like this deployment's live meshview
    payload (confirmed live 2026-08-25 against meshview.freq51.net):
    latitude_i/longitude_i/precision_bits packed into a text-format
    `payload` string, not top-level decoded fields.
    """
    precision_line = f"\nprecision_bits: {precision_bits}" if precision_bits is not None else ""
    payload = (
        f"latitude_i: {round(lat * 1e7)}\n"
        f"longitude_i: {round(lon * 1e7)}\n"
        f"altitude: 1000\n"
        f"time: {ts}\n"
        f"location_source: LOC_MANUAL\n"
        f"ground_speed: 0\n"
        f"ground_track: 0"
        f"{precision_line}"
    )
    return {
        "id": packet_id,
        "from_node_id": node_id,
        "to_node_id": 4294967295,
        "portnum": 3,
        "import_time_us": ts * 1_000_000,
        "payload": payload,
    }


class FakeMeshviewClient:
    """Stands in for app.meshview_client.MeshviewClient. Only
    packets_seen() is ever called by Ingestor._process_one_packet();
    everything else about position ingest (extract_position, the
    play-area/precision/speed gates, scoring) is the real code.
    """

    def __init__(self, feeders: list[dict] | None = None):
        self._feeders = feeders if feeders is not None else []

    async def packets_seen(self, packet_id: int) -> list[dict]:
        return self._feeders


def _one_feeder(node_id: int = 0xFEEDFACE) -> list[dict]:
    # hop_start == hop_limit -> hops == 0 -> counted as a "direct" feeder.
    return [{"node_id": node_id, "hop_start": 1, "hop_limit": 1, "rx_snr": 5.0}]


def _run(coro):
    return asyncio.run(coro)


def _player_cell_ping_rows(db_path, player_id=1):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM player_cell_ping WHERE player_id = ? AND protocol = ? ORDER BY ts",
        (player_id, PROTOCOL),
    )]
    conn.close()
    return rows


def _last_fix(db_path, player_id=1):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM player_last_fix WHERE player_id = ? AND protocol = ?",
        (player_id, PROTOCOL),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _ingest_stat(db_path, player_id=1):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM player_ingest_stat WHERE player_id = ? AND protocol = ?",
        (player_id, PROTOCOL),
    )]
    conn.close()
    totals = {}
    for r in rows:
        for k, v in r.items():
            if isinstance(v, int) and k not in ("player_id", "day"):
                totals[k] = totals.get(k, 0) + v
    return totals


def _mc_tile_cells(db_path, season_id):
    conn = sqlite3.connect(db_path)
    rows = [r[0] for r in conn.execute(
        "SELECT cell_id FROM mc_tile WHERE season_id = ?", (season_id,)
    )]
    conn.close()
    return set(rows)


# ---------------------------------------------------------------------
# A. Position precision gate
# ---------------------------------------------------------------------

def test_extract_position_reads_precision_bits_from_live_shaped_payload():
    """Unit-level: the exact text-payload shape confirmed live against
    this deployment's meshview (2026-08-25) parses precision_bits
    correctly, alongside lat/lon.
    """
    packet = {
        "id": 1, "from_node_id": 169334476, "portnum": 3,
        "import_time_us": 1787687482030770,
        "payload": ("latitude_i: 435945472\nlongitude_i: -1164181504\n"
                    "altitude: 502\ntime: 1766715809\n"
                    "location_source: LOC_MANUAL\nground_speed: 0\n"
                    "ground_track: 0\nprecision_bits: 13"),
    }
    lat, lon, precision_bits = extract_position(packet)
    assert round(lat, 4) == 43.5945
    assert round(lon, 4) == -116.4182
    assert precision_bits == 13


def test_extract_position_missing_precision_bits_is_none():
    """A position packet with no precision_bits field at all (older
    firmware, or a meshview fork that omits it) must parse to precision
    None, not a silently-assumed value.
    """
    packet = {
        "id": 2, "from_node_id": 1, "portnum": 3,
        "import_time_us": 0,
        "payload": "latitude_i: 430000000\nlongitude_i: -1160000000\n",
    }
    lat, lon, precision_bits = extract_position(packet)
    assert precision_bits is None


def test_real_ingest_rejects_low_precision_position(db_path):
    """A packet whose radio truncated its position to a box far bigger
    than the ~300m cell must be refused: no square painted, no
    player_last_fix recorded, and the rejection counted."""
    _seed_player_and_node(db_path, player_id=1, node_ref="0a0a0a0a")
    season_id = _season_id(db_path)

    too_coarse = settings.mt_min_precision_bits - 1  # e.g. 17: ~365m box
    packet = _packet(1001, 0x0A0A0A0A, LAT, LON, NOW, too_coarse)

    ingestor = Ingestor(FakeMeshviewClient(_one_feeder()))
    result = _run(ingestor._process_one_packet(packet, 1001, season_id, {"0a0a0a0a": (1, "RED")}))

    assert result is False
    assert _last_fix(db_path) is None
    assert _player_cell_ping_rows(db_path) == []
    assert _ingest_stat(db_path).get("pings_low_precision", 0) == 1
    assert _mc_tile_cells(db_path, season_id) == set()


def test_real_ingest_rejects_missing_precision_bits(db_path):
    """No precision_bits field at all must fail closed -- the same
    outcome as a too-coarse value, not a silent pass."""
    _seed_player_and_node(db_path, player_id=1, node_ref="0a0a0a0a")
    season_id = _season_id(db_path)

    packet = _packet(1002, 0x0A0A0A0A, LAT, LON, NOW, None)

    ingestor = Ingestor(FakeMeshviewClient(_one_feeder()))
    result = _run(ingestor._process_one_packet(packet, 1002, season_id, {"0a0a0a0a": (1, "RED")}))

    assert result is False
    assert _last_fix(db_path) is None
    assert _ingest_stat(db_path).get("pings_low_precision", 0) == 1


def test_real_ingest_accepts_high_precision_position(db_path):
    """A packet at or above the minimum precision must be accepted,
    scored, and its precision_bits recorded on the ping row for audit."""
    _seed_player_and_node(db_path, player_id=1, node_ref="0a0a0a0a")
    season_id = _season_id(db_path)

    good = settings.mt_min_precision_bits  # exactly at the floor: ~182m, well under 300m
    packet = _packet(1003, 0x0A0A0A0A, LAT, LON, NOW, good)

    ingestor = Ingestor(FakeMeshviewClient(_one_feeder()))
    result = _run(ingestor._process_one_packet(packet, 1003, season_id, {"0a0a0a0a": (1, "RED")}))

    assert result is True
    lf = _last_fix(db_path)
    assert lf is not None and lf["ts"] == NOW
    rows = _player_cell_ping_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["precision_bits"] == good
    assert _ingest_stat(db_path).get("pings_accepted", 0) == 1
    assert _ingest_stat(db_path).get("pings_low_precision", 0) == 0
    assert len(_mc_tile_cells(db_path, season_id)) == 1


# ---------------------------------------------------------------------
# B. Speed gate
# ---------------------------------------------------------------------

def _cell_speed_mps(lat1, lon1, lat2, lon2, elapsed_s: int) -> float:
    """The exact speed app/ingest.py's speed gate would compute for a
    jump between the CELLS these two points land in (cell-center to
    cell-center, matching the real code, not raw point-to-point)."""
    c1 = grid_cell_id(lat1, lon1)
    c2 = grid_cell_id(lat2, lon2)
    la1, lo1 = cell_center(c1)
    la2, lo2 = cell_center(c2)
    return distance_m(la1, lo1, la2, lo2) / elapsed_s


def test_real_ingest_rejects_impossible_speed_jump(db_path):
    """Two fixes for the same node, far apart and close together in
    time, implying ~1000 mph: the second must be refused, no square
    credited for it, and player_last_fix left exactly where the first,
    legitimate fix put it."""
    _seed_player_and_node(db_path, player_id=1, node_ref="0a0a0a0a")
    season_id = _season_id(db_path)
    good = settings.mt_min_precision_bits
    ingestor = Ingestor(FakeMeshviewClient(_one_feeder()))

    # First, legitimate fix.
    p1 = _packet(2001, 0x0A0A0A0A, LAT, LON, NOW, good)
    assert _run(ingestor._process_one_packet(p1, 2001, season_id, {"0a0a0a0a": (1, "RED")})) is True
    first_fix = _last_fix(db_path)
    assert first_fix["ts"] == NOW

    # Second fix, 0.5 degrees away (~55km), 5 seconds later --
    # implies on the order of 10,000+ m/s (>20,000 mph), far beyond any
    # real vehicle, and comfortably beyond the 220-1300 mph range this
    # investigation actually found live.
    lat2, lon2, ts2 = LAT + 0.5, LON, NOW + 5
    implied_speed = _cell_speed_mps(LAT, LON, lat2, lon2, 5)
    assert implied_speed > settings.mt_max_speed_mps * 10  # sanity: test itself is picking an extreme jump

    p2 = _packet(2002, 0x0A0A0A0A, lat2, lon2, ts2, good)
    result = _run(ingestor._process_one_packet(p2, 2002, season_id, {"0a0a0a0a": (1, "RED")}))

    assert result is False
    # last_fix must be untouched -- still the FIRST, legitimate fix.
    assert _last_fix(db_path) == first_fix
    assert _ingest_stat(db_path).get("pings_implausible_speed", 0) == 1
    # No square painted for the impossible jump's cell.
    jumped_cell = grid_cell_id(lat2, lon2)
    assert jumped_cell not in _mc_tile_cells(db_path, season_id)


def test_real_ingest_accepts_fast_but_plausible_car_speed(db_path):
    """A driver genuinely covering ground fast -- comfortably above any
    real highway speed limit here but still well under the gate -- must
    still be able to paint a new square."""
    _seed_player_and_node(db_path, player_id=1, node_ref="0a0a0a0a")
    season_id = _season_id(db_path)
    good = settings.mt_min_precision_bits
    ingestor = Ingestor(FakeMeshviewClient(_one_feeder()))

    p1 = _packet(3001, 0x0A0A0A0A, LAT, LON, NOW, good)
    assert _run(ingestor._process_one_packet(p1, 3001, season_id, {"0a0a0a0a": (1, "RED")})) is True

    # Solve for a lat offset that lands ~60 seconds later at roughly
    # 36 m/s (~80 mph) -- a fast, genuinely plausible highway speed for
    # this play area, well under mt_max_speed_mps.
    elapsed = 60
    target_speed = 36.0  # ~80 mph
    target_distance_m = target_speed * elapsed
    lat_offset = target_distance_m / 111_320.0  # degrees of latitude
    lat2, lon2, ts2 = LAT + lat_offset, LON, NOW + elapsed

    implied_speed = _cell_speed_mps(LAT, LON, lat2, lon2, elapsed)
    assert implied_speed < settings.mt_max_speed_mps, (
        f"test setup produced {implied_speed} m/s, not safely under the gate"
    )

    p2 = _packet(3002, 0x0A0A0A0A, lat2, lon2, ts2, good)
    result = _run(ingestor._process_one_packet(p2, 3002, season_id, {"0a0a0a0a": (1, "RED")}))

    assert result is True
    lf = _last_fix(db_path)
    assert lf["ts"] == ts2
    assert lf["cell_id"] == grid_cell_id(lat2, lon2)
    assert _ingest_stat(db_path).get("pings_implausible_speed", 0) == 0
    assert _ingest_stat(db_path).get("pings_accepted", 0) == 2
    assert len(_mc_tile_cells(db_path, season_id)) == 2


def test_real_ingest_accepts_stationary_sequence(db_path):
    """Repeated fixes from the same spot (near-zero implied speed) must
    never trip the speed gate."""
    _seed_player_and_node(db_path, player_id=1, node_ref="0a0a0a0a")
    season_id = _season_id(db_path)
    good = settings.mt_min_precision_bits
    ingestor = Ingestor(FakeMeshviewClient(_one_feeder()))

    p1 = _packet(4001, 0x0A0A0A0A, LAT, LON, NOW, good)
    assert _run(ingestor._process_one_packet(p1, 4001, season_id, {"0a0a0a0a": (1, "RED")})) is True

    # Same position, 10 minutes later.
    p2 = _packet(4002, 0x0A0A0A0A, LAT, LON, NOW + 600, good)
    result = _run(ingestor._process_one_packet(p2, 4002, season_id, {"0a0a0a0a": (1, "RED")}))

    assert result is True
    assert _ingest_stat(db_path).get("pings_implausible_speed", 0) == 0
    assert _last_fix(db_path)["ts"] == NOW + 600


# ---------------------------------------------------------------------
# Nothing already credited is altered
# ---------------------------------------------------------------------

def test_gates_do_not_touch_pre_existing_rows(db_path):
    """Both gates only ever run on a NEWLY-arriving packet -- they must
    never rewrite a player_cell_ping/player_last_fix/mc_tile row that
    already existed before this packet arrived. Seed a pre-existing
    accepted ping (as if scored by code before these gates existed, with
    no precision_bits at all -- the pre-migration shape) and confirm a
    later, unrelated low-precision rejection leaves it byte-for-byte
    alone."""
    _seed_player_and_node(db_path, player_id=1, node_ref="0a0a0a0a")
    season_id = _season_id(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO player_cell_ping(player_id, protocol, cell_id, ts, seen_at, precision_bits) "
        "VALUES (?, ?, ?, ?, ?, NULL)",
        (1, PROTOCOL, grid_cell_id(LAT, LON), NOW - 3600, NOW - 3600),
    )
    conn.execute(
        "INSERT INTO player_last_fix(player_id, protocol, cell_id, ts) VALUES (?, ?, ?, ?)",
        (1, PROTOCOL, grid_cell_id(LAT, LON), NOW - 3600),
    )
    conn.commit()
    conn.close()
    pre_existing_ping = _player_cell_ping_rows(db_path)
    pre_existing_fix = _last_fix(db_path)

    # A brand-new, unrelated low-precision packet from far away -- must
    # be refused without touching the pre-existing rows above.
    ingestor = Ingestor(FakeMeshviewClient(_one_feeder()))
    packet = _packet(5001, 0x0A0A0A0A, LAT + 1.0, LON, NOW, settings.mt_min_precision_bits - 1)
    result = _run(ingestor._process_one_packet(packet, 5001, season_id, {"0a0a0a0a": (1, "RED")}))

    assert result is False
    assert _player_cell_ping_rows(db_path) == pre_existing_ping
    assert _last_fix(db_path) == pre_existing_fix
