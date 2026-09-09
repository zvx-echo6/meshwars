"""Tests for app/places_seed.py's classification logic: the
named-summit filter and the larger/smaller-than-a-cell park split.
Does not load the real seed CSV (a full load takes on the order of a
minute, mostly park boundary geometry work) -- these exercise the pure
functions directly.

The country filter these tests used to cover (a SOTA association
allowlist, a POTA ref_code prefix check) was REMOVED 2026-09-07 --
"Places Worth Going" went worldwide, Matt approved -- see
app/places_seed.py's module docstring "COUNTRY FILTER". The tests below
that used to assert non-US rows were EXCLUDED now assert the opposite:
worldwide, everything that clears its own quality bar (a real name,
POTA's active flag upstream) is kept regardless of country.
"""
from __future__ import annotations

from app.places_seed import (
    _cell_area_m2,
    _classify_row,
    _park_band,
    _park_cells,
)
from app.grid import cell_indices
from shapely.geometry import box


def test_us_sota_association_kept():
    keep, rotates = _classify_row(
        {"ref_type": "summit", "ref_code": "W7I/SW-001", "name": "Steel Mountain"}
    )
    assert keep is True
    assert rotates is False


def test_mexico_sota_association_kept_worldwide():
    keep, _ = _classify_row(
        {"ref_type": "summit", "ref_code": "XE2/BC-001", "name": "Cerro Grande"}
    )
    assert keep is True


def test_canada_sota_association_kept_worldwide():
    for assoc in ("VE5", "VE6", "VE7"):
        keep, _ = _classify_row(
            {"ref_type": "summit", "ref_code": f"{assoc}/AB-001", "name": "Test Peak"}
        )
        assert keep is True, assoc


def test_minnesota_k0m_still_kept():
    """K0M (USA - Minnesota) was the one association code that looked
    like an odd one out next to the W-prefixed codes back when this was
    an allowlist -- no longer a distinction that matters now that every
    association is kept, but a real named summit under it should still
    pass the (now country-blind) classifier."""
    keep, _ = _classify_row(
        {"ref_type": "summit", "ref_code": "K0M/MN-001", "name": "Eagle Mountain"}
    )
    assert keep is True


def test_numeric_named_summit_below_thirteener_threshold_excluded():
    """SOTA records a summit's elevation as its name when it has none --
    a summit named "9740" (below the 13,000ft thirteener-exception
    threshold) must still be excluded regardless of country."""
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


def test_non_us_pota_park_kept_worldwide():
    for prefix in ("CA", "MX", "DL", "G", "VK"):
        keep, rotates = _classify_row({"ref_type": "park", "ref_code": f"{prefix}-1234"})
        assert keep is True, prefix
        assert rotates is None  # decided later once area is known


def test_landmark_always_kept_and_rotates():
    keep, rotates = _classify_row({"ref_type": "landmark", "ref_code": "n123"})
    assert keep is True
    assert rotates is True


def test_cell_area_shrinks_toward_the_poles():
    # Longitude degrees compress by cos(lat); a cell at 49N is smaller
    # in m^2 than the same-shaped cell at 26N.
    assert _cell_area_m2(49.0) < _cell_area_m2(26.0)


def test_park_cells_finds_the_only_cell_it_touches():
    """A park polygon covering most of one cell and none of its
    neighbour must select only the touched cell."""
    from app.grid import cell_bounds, cell_id

    lat, lon = 43.0, -116.0
    cid = cell_id(lat, lon)
    south, west, north, east = cell_bounds(cid)
    # A polygon covering 90% of this one cell's box, nothing else.
    poly = box(west, south, west + (east - west) * 0.9, north)

    cells = _park_cells(poly)
    assert cid in cells
    assert len(cells) == 1


def test_park_cells_includes_a_sliver_now_any_intersection_counts():
    """Changed 2026-09-07 ("reward the trip, not the trespass"): the
    old rule required a cell to be more than 50% inside the boundary --
    a park covering only 10% of a cell would NOT have selected it. The
    new rule is any intersection at all, so the same 10%-covered cell
    must now be included. (The reachable ring this cell then gets
    expanded into is a separate step, _ring_expand, tested below --
    this test is only about _park_cells' own base set.)
    """
    from app.grid import cell_bounds, cell_id

    lat, lon = 43.0, -116.0
    cid = cell_id(lat, lon)
    south, west, north, east = cell_bounds(cid)
    poly = box(west, south, west + (east - west) * 0.1, north)

    cells = _park_cells(poly)
    assert cid in cells


def test_park_cells_finds_no_cells_when_geometry_does_not_touch_the_grid():
    """A tiny polygon selects only the cell(s) it actually touches --
    any-intersection still means SOME intersection, not "always include
    something"; here it's exactly one cell, not the whole grid."""
    tiny = box(-116.0005, 43.0005, -116.0002, 43.0008)
    cells = _park_cells(tiny)
    from app.grid import cell_id
    assert cells == {cell_id(43.0005, -116.0005)}


def test_park_band_drops_the_deep_interior_of_a_square():
    """A clean 7x7 block of cells (indices 0..6 on both axes): at
    width=1, only cells with an outside neighbour survive as "band" --
    the deep interior (the inner 5x5, indices 1..5) has none and is
    dropped. At width=2, the next ring in (indices 1..5's own edge)
    drops too, leaving only the inner 3x3 (indices 2..4) as deep
    interior."""
    cells = {f"{y}_{x}" for y in range(7) for x in range(7)}

    band1 = _park_band(cells, width=1)
    assert "3_3" not in band1  # dead center: no outside neighbour at all
    assert "0_0" in band1 and "6_6" in band1  # corners are edge cells
    assert "0_3" in band1  # an edge midpoint
    assert len(band1) == 49 - 25  # everything except the inner 5x5

    band2 = _park_band(cells, width=2)
    assert "3_3" not in band2  # depth 4 from the edge, still well past width=2
    assert "2_2" not in band2  # depth 3 from the edge -- the new, deeper cutoff
    assert "1_1" in band2  # depth exactly 2: within width=2, still band
    assert "1_0" in band2  # depth 1 from the edge (column 0 is missing)
    assert len(band2) == 49 - 9  # everything except the inner 3x3


def test_park_band_keeps_a_footprint_narrower_than_the_band_whole():
    """A single-row strip: every cell in it is missing a neighbour
    above and below (outside the strip), so nothing survives even one
    erosion. There is no deeper interior to drop -- the whole footprint
    already IS the band, at any width."""
    strip = {f"5_{x}" for x in range(10)}
    assert _park_band(strip, width=1) == strip
    assert _park_band(strip, width=3) == strip


def test_park_band_matches_the_direct_chebyshev_definition_on_a_ragged_shape():
    """_park_band()'s iterative erosion (cheap: linear in the cell
    count per width step, reusing the previous pass) must agree with
    the direct, expensive definition -- a cell is banded iff some cell
    within Chebyshev distance `width` of it is NOT in the footprint --
    on an irregular shape (an L-notch plus a one-cell hole), not just a
    clean square, since a real park's boundary and any enclosed gap
    exercise the cumulative-erosion logic a plain square cannot."""
    cells = {f"{y}_{x}" for y in range(9) for x in range(9)}
    cells -= {f"{y}_{x}" for y in range(5, 9) for x in range(5, 9)}  # notch out a corner
    cells.discard("4_4")  # a one-cell hole in the remaining body

    def direct_band(cells: set[str], width: int) -> set[str]:
        out = set()
        for c in cells:
            y, x = cell_indices(c)
            hit = False
            for dy in range(-width, width + 1):
                for dx in range(-width, width + 1):
                    if f"{y+dy}_{x+dx}" not in cells:
                        hit = True
                        break
                if hit:
                    break
            if hit:
                out.add(c)
        return out

    for width in (1, 2, 3):
        assert _park_band(cells, width) == direct_band(cells, width), width


# ring expansion (_ring_expand, as it was called here) MOVED to
# app/grid.ring_expand() 2026-09-09 -- see tests/test_grid.py. It is no
# longer called from this module at all; the end-to-end tests further
# down (test_landmark_credits_from_an_adjacent_cell and friends) prove
# the credit-time call site app/place_scoring.py now uses is exactly
# equivalent to the seed-time one this module used to have.


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


def test_unchanged_seed_skips_the_reload(conn, tmp_path, monkeypatch):
    """A second call against a byte-identical seed must not re-run the
    reconcile pass at all -- proven the same way the codebase already
    proves a full pass ran (test_upgrading_to_the_reconcile_fix_forces_
    one_full_reload above): a manually-inserted stale row would be
    deactivated by a real reconcile pass, so if it survives untouched,
    the pass was skipped, not just cheap.
    """
    csv_path = tmp_path / "places.csv"
    monkeypatch.setattr(places_seed_module, "_DATA_PATH", str(csv_path))
    _write_seed_csv(csv_path, [_seed_row("landmark", "n1", lat=44.0, lon=-117.0)])

    first = load_places_seed(conn)
    assert first["kept"]["landmark"] == 1

    # A row a real reconcile pass would deactivate (not in the CSV).
    conn.execute(
        "INSERT INTO place(ref_type, ref_code, name, lat, lon, points, source, "
        "rotates, active, created_at) VALUES ('summit', 'sneaky', 'sneaky', 43.0, -116.0, "
        "100, 'TEST', 0, 1, ?)",
        (int(time.time()),),
    )

    second = load_places_seed(conn)
    assert second["deactivated"] == 0, "unchanged seed must skip the reconcile pass entirely"
    assert conn.execute(
        "SELECT active FROM place WHERE ref_code = 'sneaky'"
    ).fetchone()[0] == 1, "a skipped pass must not touch rows a real pass would deactivate"


def test_places_force_reseed_forces_reload_despite_matching_fingerprint(conn, tmp_path, monkeypatch):
    """PLACES_FORCE_RESEED (app/config.py's places_force_reseed) is the
    operator escape hatch -- it must force the full reconcile pass even
    when the fingerprint matches, without anyone hand-editing `cursor`.
    """
    csv_path = tmp_path / "places.csv"
    monkeypatch.setattr(places_seed_module, "_DATA_PATH", str(csv_path))
    _write_seed_csv(csv_path, [_seed_row("landmark", "n1", lat=44.0, lon=-117.0)])
    load_places_seed(conn)

    conn.execute(
        "INSERT INTO place(ref_type, ref_code, name, lat, lon, points, source, "
        "rotates, active, created_at) VALUES ('summit', 'sneaky', 'sneaky', 43.0, -116.0, "
        "100, 'TEST', 0, 1, ?)",
        (int(time.time()),),
    )

    monkeypatch.setattr(places_seed_module.settings, "places_force_reseed", True)
    stats = load_places_seed(conn)
    assert stats["deactivated"] == 1, "PLACES_FORCE_RESEED must force a real pass, not skip"
    assert conn.execute(
        "SELECT active FROM place WHERE ref_code = 'sneaky'"
    ).fetchone()[0] == 0


def test_emptied_place_table_forces_reload_despite_matching_fingerprint(conn, tmp_path, monkeypatch):
    """Belt-and-suspenders guard: a `cursor` fingerprint row recording a
    completed load should never coexist with an empty `place` table
    (they are written/populated in the same transaction), but if that
    invariant is ever violated from outside this module -- a manual
    wipe, a bad migration -- the loader must notice and reload rather
    than trusting the fingerprint into a permanent, silent "no places
    data" state.
    """
    csv_path = tmp_path / "places.csv"
    monkeypatch.setattr(places_seed_module, "_DATA_PATH", str(csv_path))
    _write_seed_csv(csv_path, [_seed_row("landmark", "n1", lat=44.0, lon=-117.0)])
    load_places_seed(conn)
    assert conn.execute("SELECT COUNT(*) FROM place").fetchone()[0] == 1

    conn.execute("DELETE FROM place")  # fingerprint row in `cursor` is left untouched
    assert conn.execute("SELECT COUNT(*) FROM place").fetchone()[0] == 0

    stats = load_places_seed(conn)
    assert stats["kept"]["landmark"] == 1
    assert conn.execute("SELECT COUNT(*) FROM place").fetchone()[0] == 1


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


# ---------------------------------------------------------------------
# REACHABLE-RING CREDIT, end to end: load_places_seed()'s cell mapping
# combined with app/place_scoring.credit_places() -- proving the ring
# actually pays out from app/place_scoring.py's point of view, not just
# that _park_cells() produces the right base cell set in isolation (see
# the unit tests above) or that app.grid.ring_expand() itself is
# correct in isolation (tests/test_grid.py).
#
# MOVED 2026-09-09 ("move the reachable ring from storage time to
# lookup time"): place_cell now stores ONLY a place's own occupied
# cell(s) -- no ring -- and credit_places() expands the ping's cell by
# one ring at query time instead. These tests now prove EQUIVALENCE
# directly: place_cell holds the narrower, un-ringed set (asserted
# below), while credit_places() still pays out from every cell the OLD
# storage-side ring would have included, because ring adjacency is
# symmetric -- and, for summits, still does NOT pay out from a
# neighbouring cell, because credit_places() gates its query-side ring
# on ref_type, not just on what happens to be stored.
# ---------------------------------------------------------------------

from app.grid import cell_bounds, cell_id, cell_indices, ring_expand
from app.place_rotation import week_start_for_ts
from app.place_scoring import credit_places


def test_landmark_credits_from_its_own_cell_and_all_8_neighbours(conn, tmp_path, monkeypatch):
    """A landmark's point can sit inside a fence with nothing to stop
    it -- the reachable ring is what lets a player standing on the
    sidewalk outside still score it, from ANY of the 8 directions, not
    just one arbitrarily-picked neighbour."""
    csv_path = tmp_path / "places.csv"
    monkeypatch.setattr(places_seed_module, "_DATA_PATH", str(csv_path))

    lat, lon = 43.0, -116.0
    _write_seed_csv(csv_path, [_seed_row("landmark", "n1", lat=lat, lon=lon, points=5)])
    load_places_seed(conn)

    place_id = conn.execute("SELECT id FROM place WHERE ref_code = 'n1'").fetchone()[0]
    own_cid = cell_id(lat, lon)

    # STORAGE: place_cell now holds ONLY the landmark's own cell -- the
    # ring is no longer stored at all (see app/places_seed.py's
    # REACHABLE-RING CREDIT note).
    stored = {r[0] for r in conn.execute(
        "SELECT cell_id FROM place_cell WHERE place_id = ?", (place_id,)
    )}
    assert stored == {own_cid}, "place_cell must store only the landmark's own cell, no ring"

    # Landmarks rotate weekly (app/places_seed.py's _classify_row) --
    # irrelevant to what this test is proving (the ring credit), so
    # force it always-active rather than pulling in place_rotation's
    # weekly-draw machinery just to make it live this week.
    conn.execute("UPDATE place SET rotates = 0 WHERE id = ?", (place_id,))

    # CREDIT: every one of the 9 cells in the ring -- the landmark's
    # own plus all 8 neighbours -- must still credit, each for a fresh
    # player so the weekly cap/one-per-week rule can't be why one fails.
    now = int(time.time())
    ring = sorted(ring_expand({own_cid}))
    assert len(ring) == 9
    for i, cid in enumerate(ring, start=100):
        credited = credit_places(conn, player_id=i, cell_id=cid, ts=now, paint_outcome="captured")
        assert credited == [(place_id, 5)], f"cell {cid} (own={cid == own_cid}) must credit"


def test_summit_does_not_credit_from_an_adjacent_cell(conn, tmp_path, monkeypatch):
    """Deliberate exception to the reachable-ring rule: a SOTA
    activation requires physically reaching the summit, so unlike every
    other place type a summit's place_cell mapping is NOT ring-expanded
    at storage time, AND credit_places() must not ring-expand the
    ping's cell into crediting it either -- an adjacent cell must not
    credit a summit under either scheme."""
    csv_path = tmp_path / "places.csv"
    monkeypatch.setattr(places_seed_module, "_DATA_PATH", str(csv_path))
    # No terrain-qualified squares for this ref_code -- force the
    # fallback to the summit's own single cell (see
    # _load_summit_cells' docstring) so this test is not at the mercy
    # of the real shipped summit_cells.csv's terrain data. Monkeypatches
    # _load_summit_cells() ITSELF, not just _SUMMIT_CELLS_PATH: that
    # function's `path` parameter defaults to _SUMMIT_CELLS_PATH at
    # DEF time (module import), so patching the module-level constant
    # afterward never reaches load_places_seed()'s own no-argument call
    # -- a real footgun this test used to fall into silently (it still
    # passed, but only because "W7I/SW-001" also happens to be absent
    # from the real shipped summit_cells.csv, not because the patch
    # below did anything).
    monkeypatch.setattr(places_seed_module, "_load_summit_cells", lambda *a, **k: {})

    lat, lon = 43.0, -116.0
    _write_seed_csv(csv_path, [
        {**_seed_row("summit", "W7I/SW-001", lat=lat, lon=lon, points=100), "name": "Steel Mountain"},
    ])
    load_places_seed(conn)

    place_id = conn.execute("SELECT id FROM place WHERE ref_code = 'W7I/SW-001'").fetchone()[0]
    own_cid = cell_id(lat, lon)
    lat_idx, lon_idx = cell_indices(own_cid)
    adjacent_cid = f"{lat_idx}_{lon_idx + 1}"

    cells = {r[0] for r in conn.execute(
        "SELECT cell_id FROM place_cell WHERE place_id = ?", (place_id,)
    )}
    assert cells == {own_cid}, "a summit must map to its own cell only, no ring"

    now = int(time.time())
    # Positive control: the summit's own cell still credits normally.
    credited = credit_places(conn, player_id=1, cell_id=own_cid, ts=now, paint_outcome="captured")
    assert credited == [(place_id, 100)]

    # The actual assertion: an adjacent cell -- which credit_places()'s
    # query-side ring WOULD include for a landmark or boundary-matched
    # park -- credits nothing for a summit, for a different player so
    # the weekly cap can't be why. This is THE regression this whole
    # change could get wrong: a query that ring-matched every ref_type
    # alike would silently pass every other test in this file while
    # breaking SOTA's core rule right here.
    credited = credit_places(conn, player_id=2, cell_id=adjacent_cid, ts=now, paint_outcome="captured")
    assert credited == []


def test_summit_credits_from_its_terrain_qualified_set_but_not_a_grid_adjacent_cell_outside_it(
    conn, tmp_path, monkeypatch,
):
    """CORRECTION (2026-09-09): summits are NOT "no ring" -- SOTA's
    activation zone genuinely extends beyond the peak's own square (near
    AND below it counts, not only standing exactly on the summit). That
    zone already exists as summit_cells.csv's terrain-qualified set
    (elevation-based, built against the planet DEM on navi), which IS
    the summit's ring -- just shaped by the mountain instead of by grid
    adjacency, and a strictly BETTER one for that reason: a flat 3x3
    would sweep in squares that sit hundreds of metres below the summit
    and outside SOTA's real activation zone.

    So the rule is: store the terrain-qualified set exactly as-is
    (already true, untouched by this change -- see the test above's
    fallback case for the single-cell case when no terrain data exists)
    and match it EXACTLY at credit time, with NO additional query-side
    8-neighbour expansion layered on top. This test proves both halves
    with a set that is deliberately NOT ring-shaped: the peak's own
    cell, plus one immediate neighbour, plus one cell that is FAR from
    the peak in grid terms (three rows away) -- standing in for a
    terrain-following square a flat ring could never reach on its own.
    A cell grid-adjacent to the peak but OUTSIDE this set must not
    credit, proving credit_places() is not silently unioning in a flat
    ring around a summit's own cell alongside its terrain set.
    """
    csv_path = tmp_path / "places.csv"
    monkeypatch.setattr(places_seed_module, "_DATA_PATH", str(csv_path))

    lat, lon = 43.0, -116.0
    own_cid = cell_id(lat, lon)
    lat_idx, lon_idx = cell_indices(own_cid)

    # Terrain-qualified set: own cell, one immediate (east) neighbour,
    # and one cell three rows south -- NOT grid-adjacent to anything
    # else in the set, so a flat ring could never produce it.
    east_cid = f"{lat_idx}_{lon_idx + 1}"
    terrain_cid = f"{lat_idx - 3}_{lon_idx + 2}"
    summit_cells_path = tmp_path / "summit_cells.csv"
    summit_cells_path.write_text(
        "ref_code,base_y,base_x,offsets\n"
        f"W7I/SW-001,{lat_idx},{lon_idx},0:0 0:1 -3:2\n"
    )
    # Parse via the REAL _load_summit_cells() (so this test also proves
    # the fixture CSV parses to what it claims), then monkeypatch
    # _load_summit_cells ITSELF to return that result -- patching just
    # _SUMMIT_CELLS_PATH does not work here: that function's `path`
    # parameter defaults to _SUMMIT_CELLS_PATH at DEF time (module
    # import), so load_places_seed()'s own no-argument call
    # (`_load_summit_cells()`) never sees a module-level patch made
    # after import. See test_summit_does_not_credit_from_an_adjacent_
    # cell's own comment on this same footgun.
    parsed = places_seed_module._load_summit_cells(str(summit_cells_path))
    assert parsed == {"W7I/SW-001": {own_cid, east_cid, terrain_cid}}, "fixture CSV parsed unexpectedly"
    monkeypatch.setattr(places_seed_module, "_load_summit_cells", lambda *a, **k: parsed)

    _write_seed_csv(csv_path, [
        {**_seed_row("summit", "W7I/SW-001", lat=lat, lon=lon, points=100), "name": "Steel Mountain"},
    ])
    load_places_seed(conn)

    place_id = conn.execute("SELECT id FROM place WHERE ref_code = 'W7I/SW-001'").fetchone()[0]

    stored = {r[0] for r in conn.execute(
        "SELECT cell_id FROM place_cell WHERE place_id = ?", (place_id,)
    )}
    assert stored == {own_cid, east_cid, terrain_cid}, (
        "the terrain-qualified set must be stored exactly as-is, not reduced to one cell"
    )

    now = int(time.time())
    # Positive: every cell actually IN the terrain-qualified set credits,
    # including the far-away terrain cell no flat ring would ever reach.
    for i, cid in enumerate((own_cid, east_cid, terrain_cid), start=1):
        credited = credit_places(conn, player_id=i, cell_id=cid, ts=now, paint_outcome="captured")
        assert credited == [(place_id, 100)], f"cell {cid} is in the terrain set and must credit"

    # Negative: a cell grid-adjacent to the peak's own square but NOT
    # part of the terrain-qualified set must NOT credit -- proving
    # credit_places() applies no flat 8-neighbour ring to a summit on
    # top of its terrain set.
    north_of_own = f"{lat_idx + 1}_{lon_idx}"
    assert north_of_own not in stored
    credited = credit_places(conn, player_id=10, cell_id=north_of_own, ts=now, paint_outcome="captured")
    assert credited == [], "a grid-adjacent-but-not-terrain-qualified cell must not credit a summit"

    # Also grid-adjacent to the FAR terrain cell, not just the peak's
    # own square -- the same guarantee has to hold everywhere in the set,
    # not only around the summit's own cell.
    adjacent_to_terrain_cell = f"{lat_idx - 3}_{lon_idx + 3}"
    assert adjacent_to_terrain_cell not in stored
    credited = credit_places(
        conn, player_id=11, cell_id=adjacent_to_terrain_cell, ts=now, paint_outcome="captured",
    )
    assert credited == [], "no ring around any terrain-qualified cell either, not just the peak's own"


def test_big_boundary_matched_park_credits_from_a_perimeter_cell_outside_the_boundary(conn, tmp_path, monkeypatch):
    """Rocky Mountain Arsenal NWR's actual case, in miniature: a park
    whose boundary covers a 3x3 block of cells must also credit from a
    perimeter cell one square further out -- a cell the polygon itself
    never touches at all, standing in for "the reachable perimeter just
    outside a mostly-closed boundary". place_cell no longer stores that
    perimeter cell at all (see the STORAGE assertions below); the
    credit comes entirely from credit_places()'s query-side ring
    matching the perimeter cell's OWN ring against the park's stored
    (un-ringed) boundary cells."""
    csv_path = tmp_path / "places.csv"
    monkeypatch.setattr(places_seed_module, "_DATA_PATH", str(csv_path))

    lat, lon = 43.0, -116.0
    center_cid = cell_id(lat, lon)
    clat, clon = cell_indices(center_cid)
    sw_south, sw_west, _, _ = cell_bounds(f"{clat - 1}_{clon - 1}")
    _, _, ne_north, ne_east = cell_bounds(f"{clat + 1}_{clon + 1}")
    eps = 1e-7
    from shapely.geometry import box as shapely_box
    poly = shapely_box(sw_west + eps, sw_south + eps, ne_east - eps, ne_north - eps)

    from app.places_seed import _cell_area_m2
    area_m2 = _cell_area_m2(lat) * 20  # comfortably at/above one cell -- the matched-larger branch

    row = _seed_row("park", "US-9999", lat=lat, lon=lon, points=25)
    row["area_m2"] = f"{area_m2:.0f}"
    row["geom"] = poly.wkt
    _write_seed_csv(csv_path, [row])
    load_places_seed(conn)

    place_id = conn.execute("SELECT id FROM place WHERE ref_code = 'US-9999'").fetchone()[0]

    # The polygon covers exactly the 3x3 block (clat-1..clat+1,
    # clon-1..clon+1) -- this IS the full stored place_cell set now,
    # with no ring added on top of it.
    perimeter_cid = f"{clat + 2}_{clon}"     # one square past the boundary -- reachable perimeter
    inside_cid = f"{clat + 1}_{clon}"        # inside the actual boundary, for contrast
    two_out_cid = f"{clat + 3}_{clon}"       # two squares past -- must stay unreachable

    cells = {r[0] for r in conn.execute(
        "SELECT cell_id FROM place_cell WHERE place_id = ?", (place_id,)
    )}
    assert cells == {f"{y}_{x}" for y in (clat - 1, clat, clat + 1) for x in (clon - 1, clon, clon + 1)}, (
        "place_cell must store exactly the boundary's own 3x3 block, no ring"
    )
    assert perimeter_cid not in cells, "the perimeter cell must NOT be stored -- the ring is query-side now"

    now = int(time.time())
    credited = credit_places(conn, player_id=1, cell_id=perimeter_cid, ts=now, paint_outcome="captured")
    assert credited == [(place_id, 25)], "the perimeter cell must still credit via the query-side ring"

    # Two squares out must still be unreachable -- only ONE ring, not two.
    credited = credit_places(conn, player_id=2, cell_id=two_out_cid, ts=now, paint_outcome="captured")
    assert credited == []


def test_large_park_stores_a_band_not_the_filled_interior(conn, tmp_path, monkeypatch):
    """A park at or above _PARK_BAND_THRESHOLD_CELLS must store only
    its boundary band (see the PARK BAND STORAGE note in
    app/places_seed.py), not every cell its boundary intersects --
    proven here on a clean 15x15 block (225 cells, comfortably over the
    100-cell threshold) so "deep interior" and "boundary" are
    unambiguous. Three positions get checked against BOTH storage
    (place_cell) and live credit_places():

      - dead center (Chebyshev distance 7 from every edge): far outside
        even a width-2 band -- must NOT be stored, and a ping there
        must NOT credit. This is the intended rule from the module
        docstring's PARK BAND STORAGE note, demonstrated directly:
        credit is for reaching the park, not for how far into its
        interior someone gets, so the deep interior scores the same as
        never having come at all -- nothing.
      - the outermost boundary row: must BE stored (it's the band's
        own edge) and must credit directly, same as any place_cell hit.
      - one square past the boundary, touching no stored cell at all:
        must still credit, via credit_places()'s query-side ring --
        exactly the same reachable-perimeter mechanism the small-park
        test above proves, now shown to survive banding too.
    """
    csv_path = tmp_path / "places.csv"
    monkeypatch.setattr(places_seed_module, "_DATA_PATH", str(csv_path))

    lat, lon = 43.0, -116.0
    center_cid = cell_id(lat, lon)
    clat, clon = cell_indices(center_cid)
    half = 7  # 15x15 block: clat-7..clat+7, clon-7..clon+7
    sw_south, sw_west, _, _ = cell_bounds(f"{clat - half}_{clon - half}")
    _, _, ne_north, ne_east = cell_bounds(f"{clat + half}_{clon + half}")
    eps = 1e-7
    from shapely.geometry import box as shapely_box
    poly = shapely_box(sw_west + eps, sw_south + eps, ne_east - eps, ne_north - eps)

    area_m2 = _cell_area_m2(lat) * 300  # comfortably at/above one cell

    row = _seed_row("park", "US-BIGPARK", lat=lat, lon=lon, points=25)
    row["area_m2"] = f"{area_m2:.0f}"
    row["geom"] = poly.wkt
    _write_seed_csv(csv_path, [row])
    load_places_seed(conn)

    place_id = conn.execute("SELECT id FROM place WHERE ref_code = 'US-BIGPARK'").fetchone()[0]
    stored = {r[0] for r in conn.execute(
        "SELECT cell_id FROM place_cell WHERE place_id = ?", (place_id,)
    )}

    # 225 cells filled, but nowhere near 225 stored -- it was banded.
    assert 0 < len(stored) < 225, f"expected a band well under the full 225-cell fill, got {len(stored)}"

    deep_center_cid = f"{clat}_{clon}"
    boundary_cid = f"{clat + half}_{clon}"        # outermost row -- part of the band
    just_outside_cid = f"{clat + half + 1}_{clon}"  # one square past the boundary

    assert deep_center_cid not in stored, "the dead center must NOT survive banding"
    assert boundary_cid in stored, "the outermost boundary row must be stored"
    assert just_outside_cid not in stored, "outside reach comes from the query-side ring, not storage"

    now = int(time.time())
    credited = credit_places(conn, player_id=1, cell_id=deep_center_cid, ts=now, paint_outcome="captured")
    assert credited == [], "deep interior must NOT credit -- reaching the park is the credit, not going further in"

    credited = credit_places(conn, player_id=2, cell_id=boundary_cid, ts=now, paint_outcome="captured")
    assert credited == [(place_id, 25)], "a stored boundary cell must credit directly"

    credited = credit_places(conn, player_id=3, cell_id=just_outside_cid, ts=now, paint_outcome="captured")
    assert credited == [(place_id, 25)], "one square past the boundary must still credit via the query-side ring"


def test_small_park_below_band_threshold_still_stores_filled_interior(conn, tmp_path, monkeypatch):
    """A park just under _PARK_BAND_THRESHOLD_CELLS (81 cells, a clean
    9x9 block) must store every one of its filled cells, unchanged --
    including its own dead center -- and a ping there must credit
    exactly like it always has. Banding only starts at the threshold;
    below it, "interior" isn't a meaningful distinction (see the PARK
    BAND STORAGE note in app/places_seed.py)."""
    csv_path = tmp_path / "places.csv"
    monkeypatch.setattr(places_seed_module, "_DATA_PATH", str(csv_path))

    lat, lon = 43.0, -116.0
    center_cid = cell_id(lat, lon)
    clat, clon = cell_indices(center_cid)
    half = 4  # 9x9 block: 81 cells, under the 100-cell threshold
    sw_south, sw_west, _, _ = cell_bounds(f"{clat - half}_{clon - half}")
    _, _, ne_north, ne_east = cell_bounds(f"{clat + half}_{clon + half}")
    eps = 1e-7
    from shapely.geometry import box as shapely_box
    poly = shapely_box(sw_west + eps, sw_south + eps, ne_east - eps, ne_north - eps)

    area_m2 = _cell_area_m2(lat) * 300

    row = _seed_row("park", "US-SMALLPARK", lat=lat, lon=lon, points=25)
    row["area_m2"] = f"{area_m2:.0f}"
    row["geom"] = poly.wkt
    _write_seed_csv(csv_path, [row])
    load_places_seed(conn)

    place_id = conn.execute("SELECT id FROM place WHERE ref_code = 'US-SMALLPARK'").fetchone()[0]
    stored = {r[0] for r in conn.execute(
        "SELECT cell_id FROM place_cell WHERE place_id = ?", (place_id,)
    )}
    expected = {f"{y}_{x}" for y in range(clat - half, clat + half + 1) for x in range(clon - half, clon + half + 1)}
    assert stored == expected, "a park below the band threshold must store its full filled footprint"

    deep_center_cid = f"{clat}_{clon}"
    now = int(time.time())
    credited = credit_places(conn, player_id=1, cell_id=deep_center_cid, ts=now, paint_outcome="captured")
    assert credited == [(place_id, 25)], "below the threshold, the interior still credits exactly as before"
