"""Tests for scripts/build_places_seed.py's score_points()/_summit_points()
-- the elevation-scaling model added 2026-08-25 ("lets make the points
for peaks scaling. 50 for low elevation peaks up to 100 for 9000ft +").

scripts/ is not a package (this pipeline is meant to run standalone --
see that module's own docstring), so it is imported here the same way
the module itself expects to be run: by adding scripts/ to sys.path,
not via a dotted package import.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import build_places_seed as bps  # noqa: E402


def test_summit_points_floor_is_50():
    assert bps._summit_points(bps.SUMMIT_ELEV_FLOOR_FT) == 50
    # Below the floor clamps, does not go lower.
    assert bps._summit_points(2110) == 50


def test_summit_points_ceiling_is_100_at_9000_and_above():
    assert bps._summit_points(bps.SUMMIT_ELEV_CEIL_FT) == 100
    # Idaho's highest, Borah Peak, well above the ceiling -- clamps,
    # does not go higher.
    assert bps._summit_points(12662) == 100


def test_summit_points_mid_value_lands_where_expected():
    # Exact midpoint of the 6000-9000 range scales to the exact
    # midpoint of the 50-100 range.
    assert bps._summit_points(7500) == 75


def test_summit_points_missing_elevation_falls_back_to_floor():
    assert bps._summit_points(None) == 50


def _buckets_with_one_anchor(lat: float, lon: float, radius_m: float) -> dict:
    import math

    key = (math.floor(lat / bps._ANCHOR_BUCKET_DEG), math.floor(lon / bps._ANCHOR_BUCKET_DEG))
    return {key: [(lat, lon, radius_m)]}


def test_score_points_summit_ignores_the_in_city_rule():
    """A peak is a peak. The in-city rule used to win outright here, on
    the reasoning that you can park at a summit inside a town -- which
    flattened 95 summits to 5 points, Humphreys Peak (12,633ft, the
    highest point in Arizona) among them. A town anchor is a flat circle
    and cannot see the relief inside it, so summits are scored on
    elevation whether or not an anchor reaches them (2026-08-31)."""
    buckets = _buckets_with_one_anchor(43.6, -116.2, 5000)
    row = {
        "ref_type": "summit", "lat": "43.6001", "lon": "-116.2001",
        "elevation_ft": "12662",
    }
    points, reason = bps.score_points(row, buckets)
    assert points == bps.SUMMIT_MAX_REMOTE_POINTS == 100
    assert reason == "remote_scaled"


def test_score_points_in_city_still_applies_to_park_and_landmark():
    """Only summits were exempted -- the in-city rule is untouched for
    everything without an elevation to score on."""
    buckets = _buckets_with_one_anchor(43.6, -116.2, 5000)
    for ref_type in ("park", "landmark"):
        points, reason = bps.score_points(
            {"ref_type": ref_type, "lat": "43.6001", "lon": "-116.2001",
             "elevation_ft": ""}, buckets)
        assert (points, reason) == (bps.IN_CITY_POINTS, "in_city"), ref_type


def test_score_points_remote_summit_scales_by_elevation():
    buckets = _buckets_with_one_anchor(43.6, -116.2, 500)  # far from the summit below
    row = {
        "ref_type": "summit", "lat": "44.1", "lon": "-113.8",
        "elevation_ft": "7500",
    }
    points, reason = bps.score_points(row, buckets)
    assert points == 75
    assert reason == "remote_scaled"


def test_score_points_remote_park_and_landmark_unaffected():
    """Only summit scoring changed -- park/landmark keep their flat
    remote values."""
    buckets = _buckets_with_one_anchor(43.6, -116.2, 500)
    park_row = {"ref_type": "park", "lat": "44.1", "lon": "-113.8"}
    landmark_row = {"ref_type": "landmark", "lat": "44.1", "lon": "-113.8"}
    assert bps.score_points(park_row, buckets) == (25, "remote")
    assert bps.score_points(landmark_row, buckets) == (10, "remote")
