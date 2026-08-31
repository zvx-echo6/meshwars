"""Tests for app/places_seed.py's classification logic: the non-US
filter (2026-08-24 correction -- see that module's docstring) and the
larger/smaller-than-a-cell park split. Does not load the real 65k-row
CSV (a full load takes on the order of a minute, mostly park boundary
geometry work) -- these exercise the pure functions directly.
"""
from __future__ import annotations

from app.places_seed import US_SOTA_ASSOCIATIONS, _cell_area_m2, _classify_row, _park_cells
from shapely.geometry import box


def test_us_sota_association_kept():
    keep, rotates = _classify_row(
        {"ref_type": "summit", "ref_code": "W7I/SW-001", "name": "Steel Mountain"}
    )
    assert keep is True
    assert rotates is False


def test_mexico_sota_association_excluded():
    keep, _ = _classify_row({"ref_type": "summit", "ref_code": "XE2/BC-001"})
    assert keep is False


def test_canada_sota_association_excluded():
    for assoc in ("VE5", "VE6", "VE7"):
        keep, _ = _classify_row({"ref_type": "summit", "ref_code": f"{assoc}/AB-001"})
        assert keep is False, assoc


def test_minnesota_k0m_is_us_not_a_typo():
    keep, _ = _classify_row(
        {"ref_type": "summit", "ref_code": "K0M/MN-001", "name": "Eagle Mountain"}
    )
    assert keep is True


def test_numeric_named_summit_below_thirteener_threshold_excluded():
    """SOTA records a summit's elevation as its name when it has none --
    a US-association summit named "9740" (below the 13,000ft
    thirteener-exception threshold) must still be excluded even though
    it clears the country filter."""
    keep, _ = _classify_row(
        {"ref_type": "summit", "ref_code": "W7M/SW-001", "name": "9740"}
    )
    assert keep is False


def test_numeric_named_summit_at_thirteener_threshold_kept():
    """Colorado's 13,000ft+ peaks are genuinely known BY their elevation
    -- "13546" (a real example pulled from the seed CSV, W0C/LG-007) is
    kept, not treated as a missing name."""
    keep, _ = _classify_row(
        {"ref_type": "summit", "ref_code": "W0C/LG-007", "name": "13546"}
    )
    assert keep is True


def test_numeric_named_summit_just_below_thirteener_threshold_excluded():
    """The threshold is a hard 13,000ft floor, not a rounded-up
    approximation -- 12,999 does not qualify."""
    keep, _ = _classify_row(
        {"ref_type": "summit", "ref_code": "W0C/LG-999", "name": "12999"}
    )
    assert keep is False


def test_legitimately_named_summit_with_a_digit_kept():
    """A real name that happens to contain a digit must NOT be treated
    as an elevation stand-in -- only a name with no letters at all, or
    a bare generic-placeholder-plus-number, is excluded."""
    keep, _ = _classify_row(
        {"ref_type": "summit", "ref_code": "W7I/SW-002", "name": "Ten Mile Peak"}
    )
    assert keep is True


def test_us_pota_park_kept():
    keep, rotates = _classify_row({"ref_type": "park", "ref_code": "US-1234"})
    assert keep is True
    assert rotates is None  # decided later once area is known


def test_non_us_pota_park_excluded():
    for prefix in ("CA", "MX"):
        keep, _ = _classify_row({"ref_type": "park", "ref_code": f"{prefix}-1234"})
        assert keep is False, prefix


def test_landmark_always_kept_and_rotates():
    keep, rotates = _classify_row({"ref_type": "landmark", "ref_code": "n123"})
    assert keep is True
    assert rotates is True


def test_cell_area_shrinks_toward_the_poles():
    # Longitude degrees compress by cos(lat); a cell at 49N is smaller
    # in m^2 than the same-shaped cell at 26N.
    assert _cell_area_m2(49.0) < _cell_area_m2(26.0)


def test_park_cells_finds_only_majority_covered_cells():
    """A park polygon covering most of one cell and none of its
    neighbour must select only the covered cell."""
    from app.grid import CELL_LAT_DEG, CELL_LON_DEG, cell_bounds, cell_id

    lat, lon = 43.0, -116.0
    cid = cell_id(lat, lon)
    south, west, north, east = cell_bounds(cid)
    # A polygon covering 90% of this one cell's box, nothing else.
    poly = box(west, south, west + (east - west) * 0.9, north)

    cells = _park_cells(poly, lat)
    assert cid in cells
    assert len(cells) == 1


def test_park_cells_excludes_a_sliver():
    """A polygon covering only 10% of a cell must not select it."""
    from app.grid import cell_bounds, cell_id

    lat, lon = 43.0, -116.0
    cid = cell_id(lat, lon)
    south, west, north, east = cell_bounds(cid)
    poly = box(west, south, west + (east - west) * 0.1, north)

    cells = _park_cells(poly, lat)
    assert cid not in cells


# ---------------------------------------------------------------------
# Reconcile: load_places_seed() must make `place` match the CSV exactly
# -- a place pruned from a later seed rebuild goes inactive, never
# deleted, so place_activation rows that already point at it keep
# resolving (docs/features/places.md's Explorer Score must not change
# just because the seed got re-tuned).
# ---------------------------------------------------------------------

import csv as _csv
import os
import time

import app.places_seed as places_seed_module
from app.places_seed import load_places_seed

_CSV_FIELDS = ["ref_type", "ref_code", "name", "lat", "lon", "points", "source", "area_m2", "geom"]


def _seed_row(ref_type, ref_code, lat=43.0, lon=-116.0, points=None):
    if points is None:
        points = {"summit": 100, "park": 25, "landmark": 5}[ref_type]
    return {
        "ref_type": ref_type, "ref_code": ref_code, "name": ref_code,
        "lat": lat, "lon": lon, "points": points, "source": "TEST",
        "area_m2": "", "geom": "",
    }


def _write_seed_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=_CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def test_reconcile_deactivates_a_place_pruned_from_the_seed(conn, tmp_path, monkeypatch):
    csv_path = tmp_path / "places.csv"
    monkeypatch.setattr(places_seed_module, "_DATA_PATH", str(csv_path))

    _write_seed_csv(csv_path, [
        _seed_row("summit", "W7I/SW-001"),
        _seed_row("landmark", "n1", lat=44.0, lon=-117.0),
    ])
    load_places_seed(conn)
    before = conn.execute(
        "SELECT id, active FROM place WHERE ref_type='summit' AND ref_code='W7I/SW-001'"
    ).fetchone()
    assert before["active"] == 1
    summit_id = before["id"]

    # Re-tuned seed: the summit is pruned out, only the landmark remains.
    _write_seed_csv(csv_path, [
        _seed_row("landmark", "n1", lat=44.0, lon=-117.0),
    ])
    stats = load_places_seed(conn)

    after = conn.execute("SELECT active FROM place WHERE id = ?", (summit_id,)).fetchone()
    assert after is not None, "pruned place must survive the reload, not be deleted"
    assert after["active"] == 0
    assert stats["deactivated"] == 1

    still_landmark = conn.execute(
        "SELECT active FROM place WHERE ref_type='landmark' AND ref_code='n1'"
    ).fetchone()
    assert still_landmark["active"] == 1


def test_reconcile_reports_active_counts_matching_the_csv_exactly(conn, tmp_path, monkeypatch):
    csv_path = tmp_path / "places.csv"
    monkeypatch.setattr(places_seed_module, "_DATA_PATH", str(csv_path))

    _write_seed_csv(csv_path, [
        _seed_row("summit", "W7I/SW-001"),
        _seed_row("summit", "W7I/SW-002", lat=43.1, lon=-116.1),
        _seed_row("landmark", "n1", lat=44.0, lon=-117.0),
    ])
    load_places_seed(conn)

    # Prune both summits out; the seed rebuild that motivated this fix
    # pruned ~24k summits in one pass -- two is enough to prove the
    # table ends up with EXACTLY what the CSV contains, no extras.
    _write_seed_csv(csv_path, [
        _seed_row("landmark", "n1", lat=44.0, lon=-117.0),
    ])
    load_places_seed(conn)

    counts = dict(conn.execute(
        "SELECT ref_type, COUNT(*) FROM place WHERE active = 1 GROUP BY ref_type"
    ).fetchall())
    assert counts == {"landmark": 1}
    total_rows = conn.execute("SELECT COUNT(*) FROM place").fetchone()[0]
    assert total_rows == 3  # both pruned summits still exist, just inactive


def test_place_returning_to_the_seed_is_reactivated(conn, tmp_path, monkeypatch):
    csv_path = tmp_path / "places.csv"
    monkeypatch.setattr(places_seed_module, "_DATA_PATH", str(csv_path))

    _write_seed_csv(csv_path, [_seed_row("summit", "W7I/SW-001")])
    load_places_seed(conn)
    place_id = conn.execute(
        "SELECT id FROM place WHERE ref_code = 'W7I/SW-001'"
    ).fetchone()[0]

    _write_seed_csv(csv_path, [])  # pruned out
    load_places_seed(conn)
    assert conn.execute(
        "SELECT active FROM place WHERE id = ?", (place_id,)
    ).fetchone()[0] == 0

    _write_seed_csv(csv_path, [_seed_row("summit", "W7I/SW-001")])  # back in a later rebuild
    load_places_seed(conn)
    row = conn.execute("SELECT id, active FROM place WHERE ref_code = 'W7I/SW-001'").fetchone()
    assert row["id"] == place_id, "same ref_type/ref_code must reuse the same row, not duplicate"
    assert row["active"] == 1


def test_past_activation_against_a_pruned_place_still_resolves_and_counts(conn, tmp_path, monkeypatch):
    """A player who legitimately scored a summit that later left the
    seed must keep the points and the name -- Explorer Score must not
    change just because the seed was re-tuned.
    """
    csv_path = tmp_path / "places.csv"
    monkeypatch.setattr(places_seed_module, "_DATA_PATH", str(csv_path))

    _write_seed_csv(csv_path, [_seed_row("summit", "W7I/SW-001", points=100)])
    load_places_seed(conn)
    place_id = conn.execute(
        "SELECT id FROM place WHERE ref_code = 'W7I/SW-001'"
    ).fetchone()[0]

    now = int(time.time())
    conn.execute(
        "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (place_id, 42, "2026-08-19", 100, now),
    )

    # Seed rebuild prunes the summit out.
    _write_seed_csv(csv_path, [])
    load_places_seed(conn)
    assert conn.execute(
        "SELECT active FROM place WHERE id = ?", (place_id,)
    ).fetchone()[0] == 0

    # Explorer Score sum (app/public_api.py's own query shape) is
    # untouched -- it never joins back to `place` at all.
    explorer_total = conn.execute(
        "SELECT SUM(points) FROM place_activation WHERE player_id = ?", (42,)
    ).fetchone()[0]
    assert explorer_total == 100

    # The name still resolves via a join, for any UI that wants to show
    # "what did I score" history against a now-inactive place.
    name = conn.execute(
        "SELECT p.name FROM place_activation a JOIN place p ON p.id = a.place_id "
        "WHERE a.player_id = ?",
        (42,),
    ).fetchone()[0]
    assert name == "W7I/SW-001"


def test_upgrading_to_the_reconcile_fix_forces_one_full_reload(conn, tmp_path, monkeypatch):
    """A DB that already recorded this exact CSV's fingerprint under the
    OLD insert-only loader (no _RECONCILE_VERSION in the fingerprint)
    must not skip its first load after upgrading -- otherwise a stale
    row from before this fix landed would never actually get
    reconciled, since the CSV file itself never changes again.
    """
    csv_path = tmp_path / "places.csv"
    monkeypatch.setattr(places_seed_module, "_DATA_PATH", str(csv_path))
    _write_seed_csv(csv_path, [_seed_row("landmark", "n1", lat=44.0, lon=-117.0)])

    # Simulate the pre-fix fingerprint already recorded for this exact
    # file (size:mtime, no version suffix) and a stale summit row an
    # old insert-only loader left behind.
    st = os.stat(csv_path)
    old_style_fingerprint = f"{st.st_size}:{int(st.st_mtime)}"
    conn.execute(
        "INSERT INTO cursor(k, v) VALUES ('places_seed_csv_fingerprint', ?)",
        (old_style_fingerprint,),
    )
    conn.execute(
        "INSERT INTO place(ref_type, ref_code, name, lat, lon, points, source, "
        "rotates, active, created_at) VALUES ('summit', 'stale', 'stale', 43.0, -116.0, "
        "100, 'STALE', 0, 1, ?)",
        (int(time.time()),),
    )

    stats = load_places_seed(conn)

    assert stats["deactivated"] == 1
    assert conn.execute(
        "SELECT active FROM place WHERE ref_code = 'stale'"
    ).fetchone()[0] == 0


# --- summits are a terrain-qualified set of squares, not one square -----


def test_load_summit_cells_expands_offsets(tmp_path):
    from app import places_seed
    p = tmp_path / "summit_cells.csv"
    p.write_text("ref_code,base_y,base_x,offsets\nW7U/SL-001,15000,-29000,0:0 1:0 -1:2\n")
    out = places_seed._load_summit_cells(str(p))
    assert out == {"W7U/SL-001": {"15000_-29000", "15001_-29000", "14999_-28998"}}


def test_load_summit_cells_survives_a_missing_file(tmp_path):
    from app import places_seed
    # Not fatal: summits fall back to their own square, which is exactly
    # what they had before the artifact existed.
    assert places_seed._load_summit_cells(str(tmp_path / "nope.csv")) == {}


def test_load_summit_cells_skips_malformed_rows(tmp_path):
    from app import places_seed
    p = tmp_path / "summit_cells.csv"
    p.write_text(
        "ref_code,base_y,base_x,offsets\n"
        "GOOD/1,10,20,0:0\n"
        "BAD/NOINT,x,20,0:0\n"
        "BAD/SHORT,10,20\n"
        "GOOD/2,30,40,1:1 bogus 2:2\n"
    )
    out = places_seed._load_summit_cells(str(p))
    assert set(out) == {"GOOD/1", "GOOD/2"}
    assert out["GOOD/1"] == {"10_20"}
    assert out["GOOD/2"] == {"31_41", "32_42"}   # "bogus" dropped, rest kept


def test_shipped_summit_cells_artifact_is_loadable_and_exclusive():
    """The real file: every square belongs to exactly one summit."""
    from app import places_seed
    cells = places_seed._load_summit_cells()
    assert len(cells) > 4500, "shipped summit_cells.csv looks truncated"
    seen = {}
    for ref_code, squares in cells.items():
        for sq in squares:
            assert sq not in seen, f"{sq} claimed by {seen[sq]} and {ref_code}"
            seen[sq] = ref_code
    assert len(seen) > 80_000
