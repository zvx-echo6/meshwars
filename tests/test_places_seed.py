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
    keep, rotates = _classify_row({"ref_type": "summit", "ref_code": "W7I/SW-001"})
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
    keep, _ = _classify_row({"ref_type": "summit", "ref_code": "K0M/MN-001"})
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
