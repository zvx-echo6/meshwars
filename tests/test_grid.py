"""Tests for app/grid.py's pure cell-index math.

ring_expand() lived in app/places_seed.py (private, `_ring_expand`)
until 2026-09-09, applied at seed-build time to every place's stored
`place_cell` rows. Moved here as part of "move the reachable ring from
storage time to lookup time" (docs/features/places.md's reachable-ring
section, app/place_scoring.credit_places()) -- it is now called at
CREDIT time, against the ping's own cell, not at seed time against a
place's cell. The function itself is unchanged; only where it is
called from moved. See tests/test_places_seed.py's
test_landmark_credits_from_an_adjacent_cell and
test_summit_does_not_credit_from_an_adjacent_cell for the end-to-end
proof that moving the call site changes nothing about who gets
credited.
"""
from __future__ import annotations

from app.grid import ring_expand


def test_ring_expand_of_a_single_cell_is_a_3x3_block():
    expanded = ring_expand({"10_20"})
    assert expanded == {
        "9_19", "9_20", "9_21",
        "10_19", "10_20", "10_21",
        "11_19", "11_20", "11_21",
    }


def test_ring_expand_of_two_adjacent_cells_merges_their_rings():
    # Two touching cells' 3x3 blocks overlap; the result is their union,
    # not two disjoint 3x3 blocks (9+9=18) -- de-duplicated by the set.
    expanded = ring_expand({"10_20", "10_21"})
    assert len(expanded) < 18
    assert "10_20" in expanded and "10_21" in expanded
    assert "9_19" in expanded and "11_22" in expanded  # outer corners of the combined block


def test_ring_expand_adds_exactly_one_ring_not_two():
    """A cell two squares away from the base set must NOT be included --
    only one ring outward, not a second pass."""
    expanded = ring_expand({"10_20"})
    assert "12_20" not in expanded
    assert "10_22" not in expanded
