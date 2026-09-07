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


# ---------------------------------------------------------------------
# _compile_exclude_park_name_re(): elementary-and-below schools come off
# the board (2026-09-07, Matt's decision, narrowed the same day to spare
# junior high/middle school once the reachable-ring credit change made a
# school creditable from the public sidewalk outside its fence -- see
# docs/features/places.md and app/places_seed.py's REACHABLE-RING CREDIT
# note). Junior high, middle school, senior high, college, and
# university names all stay; only elementary-and-below comes off.
# ---------------------------------------------------------------------


def test_elementary_school_park_name_is_excluded():
    exre = bps._compile_exclude_park_name_re()
    for name in (
        "Lincoln Elementary School",
        "Roosevelt Elem",
        "Franklin Intermediate School",
        "Sunrise Grade School",
        "Lincoln K-8 School",
        "Lincoln K-6 School",
        "Little Learners Preschool",
        "Sunshine Pre-K Center",
        "Willamette Primary School",
    ):
        assert exre.search(name), name


def test_junior_high_and_middle_school_park_names_are_kept():
    """Reversed same day as the original cut -- see the module comment
    above _SCHOOL_DROP_RE_SRC for why junior high/middle school no
    longer come off."""
    exre = bps._compile_exclude_park_name_re()
    for name in (
        "Whitman Junior High School",
        "Whitman Jr High",
        "Whitman Jr. High School",
        "Lewis & Clark Middle School",
    ):
        assert not exre.search(name), name


def test_west_junior_high_school_survives_the_filter():
    """The confirming case Matt named directly: a real PAD-US row
    (Boise, PADUS-84489, 0.12 km^2, 5 points) that the original wider
    cut would have dropped and the narrowed rule must keep."""
    exre = bps._compile_exclude_park_name_re()
    assert not exre.search("West Junior High School")


def test_senior_high_college_university_park_names_are_kept():
    exre = bps._compile_exclude_park_name_re()
    for name in (
        "Boise Senior High School",
        "Capital High School",
        "Boise State University",
        "North Idaho College",
        "Idaho Fish and Game Institute",
        "Seminary Oaks Park",
    ):
        assert not exre.search(name), name


def test_unclassifiable_school_named_park_defaults_to_kept():
    """Most names this filter cannot resolve either way are real parks
    that merely happen to be NAMED after a school, not a school campus
    itself -- deleting a national park to catch an elementary school
    would be a far worse mistake than the reverse, so these must default
    to kept."""
    exre = bps._compile_exclude_park_name_re()
    for name in (
        "Blackwell School National Park",
        "Elgin School House State Park",
        "Galloway School Park",
    ):
        assert not exre.search(name), name


def test_drop_is_evaluated_before_keep_for_a_combined_campus_name():
    """ORDERING TEST -- the specific bug this must not regress. A real
    combined campus can be named e.g. "Lincoln Elementary and Middle
    School": it matches BOTH the DROP pattern ("elementary") and the
    KEEP pattern ("middle school") as substrings of the very same name.
    If KEEP were (wrongly) checked before DROP, this name would match
    "middle school" and get kept outright, before the elementary-grade
    word ever had a chance to exclude it. DROP must be evaluated first
    so a campus that is even partly elementary-and-below still comes
    off the board.
    """
    exre = bps._compile_exclude_park_name_re()
    name = "Lincoln Elementary and Middle School"
    import re
    assert re.search(bps._SCHOOL_DROP_RE_SRC, name, re.IGNORECASE), \
        "test fixture must actually match the DROP pattern"
    assert re.search(bps._SCHOOL_KEEP_RE_SRC, name, re.IGNORECASE), \
        "test fixture must actually match the KEEP pattern too -- otherwise this isn't testing ordering"
    assert exre.search(name), "DROP must win when a name matches both patterns"


def test_utility_parcel_exclusions_unaffected_by_the_school_rule():
    """The original, unrelated exclusions (community gardens, utility
    parcels) must still work exactly as before."""
    exre = bps._compile_exclude_park_name_re()
    for name in ("Community Garden Park", "Detention Basin Park", "Water Tower Park"):
        assert exre.search(name), name


# ---------------------------------------------------------------------
# PARK-SIZE SCORING (2026-09-07, "Yosemite pays the same as a pocket
# park"): _frac_area_outside_city() replaces the centroid-in-circle test
# for a park with a matched boundary -- see that function's own comment
# and PARK_REMOTE_AREA_FRAC above match_parks() for the reasoning and
# the 0.5 cutoff.
# ---------------------------------------------------------------------

from shapely.geometry import box  # noqa: E402


def _one_anchor_bucket(lat: float, lon: float, radius_m: float) -> dict:
    key = (bps.math.floor(lat / bps._ANCHOR_BUCKET_DEG), bps.math.floor(lon / bps._ANCHOR_BUCKET_DEG))
    return {key: [(lat, lon, radius_m)]}


def test_frac_area_outside_city_large_mostly_remote_park_is_mostly_outside():
    """A big (~55km x 50km) park with only a small town's circle
    brushing one corner of it -- almost all of its own area must fall
    outside that circle, well past PARK_REMOTE_AREA_FRAC."""
    big_park = box(-114.05, 43.75, -113.55, 44.25)
    buckets = _one_anchor_bucket(43.76, -114.04, 3000.0)  # a small 3km-radius town at one corner
    frac = bps._frac_area_outside_city(big_park, buckets)
    assert frac > bps.PARK_REMOTE_AREA_FRAC
    assert frac > 0.95, frac


def test_frac_area_outside_city_large_park_entirely_inside_one_city_is_in_city():
    """A large park (Golden Gate Park/Central Park style -- big, but
    trivially easy to reach) sitting entirely inside one big city's own
    circle must measure close to 0% outside, not remote just for being
    a large polygon -- Matt was explicit that size alone must not read
    as remote."""
    city_park = box(-116.22, 43.59, -116.17, 43.64)
    buckets = _one_anchor_bucket(43.615, -116.195, 20000.0)  # a 20km-radius city containing it
    frac = bps._frac_area_outside_city(city_park, buckets)
    assert frac < bps.PARK_REMOTE_AREA_FRAC
    assert frac < 0.05, frac


def test_frac_area_outside_city_no_nearby_anchor_is_fully_outside():
    """Deep backcountry, nothing in range -- the whole park counts as
    outside, same as _in_city_limits returning False when it finds
    nothing nearby."""
    remote_park = box(-114.05, 43.75, -113.55, 44.25)
    frac = bps._frac_area_outside_city(remote_park, {})
    assert frac == 1.0


def test_score_points_park_with_area_frac_outside_uses_it_over_the_point_test():
    """A park with a matched boundary (area_frac_outside populated) is
    scored from that fraction, not the point-based in-city test -- even
    when the row's own lat/lon sits inside a buckets anchor that would
    otherwise say 'in city'."""
    buckets = _one_anchor_bucket(43.6, -116.2, 50000.0)  # huge anchor covering the point below
    row_remote = {
        "ref_type": "park", "lat": "43.6001", "lon": "-116.2001",
        "area_frac_outside": "0.97",
    }
    assert bps.score_points(row_remote, buckets) == (bps.REMOTE_POINTS["park"], "remote_by_area")

    row_in_city = {
        "ref_type": "park", "lat": "43.6001", "lon": "-116.2001",
        "area_frac_outside": "0.10",
    }
    assert bps.score_points(row_in_city, buckets) == (bps.IN_CITY_POINTS, "in_city_by_area")


def test_score_points_park_without_area_frac_outside_falls_back_to_point_test():
    """An unmatched park (area_frac_outside == "") still uses the plain
    point-based in-city test -- nothing regresses for the parks that
    have no boundary to measure."""
    buckets = _one_anchor_bucket(43.6, -116.2, 5000)
    row = {"ref_type": "park", "lat": "43.6001", "lon": "-116.2001", "area_frac_outside": ""}
    assert bps.score_points(row, buckets) == (bps.IN_CITY_POINTS, "in_city")


# ---------------------------------------------------------------------
# BOUNDARY SANITY CHECK (2026-09-07, "a roadside museum does not have a
# 21,000 km^2 boundary"): _match_passes_sanity_check() rejects a match
# only when BOTH the area is implausible AND the name evidence behind
# it was weak -- see MATCH_AREA_SANITY_CEILING_M2's own comment.
# ---------------------------------------------------------------------


def test_match_sanity_check_rejects_huge_area_with_weak_name_score():
    """The confirmed shipped-seed case: "Cherokee Hills Scenic Byway
    Scenic Site" matched to the same 18,034.7 km^2 polygon as the
    legitimate "Cherokee Wildlife Management Area", scoring only 0.25
    on the name-token test -- must be rejected."""
    assert not bps._match_passes_sanity_check(area_m2=18_034_700_000, name_score=0.25)


def test_match_sanity_check_keeps_legitimate_huge_match_with_strong_name_score():
    """Flathead National Forest's own 13,613.9 km^2 boundary, matched at
    a near-exact name score -- must NOT be rejected just for being
    enormous."""
    assert bps._match_passes_sanity_check(area_m2=13_613_900_000, name_score=1.0)


def test_match_sanity_check_keeps_small_area_even_with_weak_name_score():
    """A weak name match on an ordinarily-sized boundary is exactly what
    the lenient contains-branch is FOR -- the sanity check must never
    touch a match under the area ceiling, however weak its name score."""
    assert bps._match_passes_sanity_check(area_m2=5_000_000, name_score=0.05)


def test_match_sanity_check_keeps_huge_area_right_at_the_score_boundary():
    """MATCH_AREA_SANITY_MIN_SCORE (0.5) is inclusive -- a match scoring
    exactly 0.5 is not weak evidence and must not be rejected."""
    assert bps._match_passes_sanity_check(
        area_m2=bps.MATCH_AREA_SANITY_CEILING_M2 * 2, name_score=bps.MATCH_AREA_SANITY_MIN_SCORE)
