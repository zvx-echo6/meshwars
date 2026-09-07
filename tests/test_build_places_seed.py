"""Tests for scripts/build_places_seed.py's score_points()/_summit_points()
-- the elevation-scaling model added 2026-08-25 ("lets make the points
for peaks scaling. 50 for low elevation peaks up to 100 for 9000ft +").

scripts/ is not a package (this pipeline is meant to run standalone --
see that module's own docstring), so it is imported here the same way
the module itself expects to be run: by adding scripts/ to sys.path,
not via a dotted package import.
"""
from __future__ import annotations

import math
import os
import sys

import pytest

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


def test_frac_area_outside_city_invalid_geometry_does_not_raise():
    """A self-intersecting ('bowtie') polygon -- PAD-US ships a handful
    of these, and match_parks()'s own simplify() can introduce fresh
    ones despite preserve_topology=True -- must not blow up a recompute
    pass with a GEOSException (a real 77,000-row re-rate died at row
    30,000 on exactly this before the make_valid()/fallback repair was
    added). Uses the same small anchor circle brushing one corner as
    the mostly-remote case above -- not fully covered, not fully
    missed -- so the bbox shortcut can't answer it and the real
    repair-then-intersect (or centroid-fallback) path actually runs."""
    from shapely.geometry import Polygon

    bowtie = Polygon([(-114.05, 43.75), (-113.55, 44.25), (-114.05, 44.25), (-113.55, 43.75)])
    assert not bowtie.is_valid
    buckets = _one_anchor_bucket(43.76, -114.04, 3000.0)
    frac = bps._frac_area_outside_city(bowtie, buckets)
    assert 0.0 <= frac <= 1.0


# ---------------------------------------------------------------------
# CLIPPED-GEOMETRY GUARD (2026-09-07, "Humboldt-Toiyabe National Forest
# went 25 -> 5 on a re-rate pass"): match_parks() stores geom clipped
# to a ~6km window around the park's own point; feeding that clipped
# window back into _frac_area_outside_city as if it were the whole
# park (as a CSV-patch recompute pass, run without PAD-US access to
# regenerate the true boundary, is tempted to do) silently mis-scores
# any park bigger than that window. true_area_m2 is the guard against
# exactly that -- see CLIPPED_GEOM_AREA_RATIO's own comment.
# ---------------------------------------------------------------------


def test_frac_area_outside_city_refuses_a_clipped_geometry():
    """A small (~8km x 12km) stored geometry standing in for a park
    whose real area is 12,976 km^2 (Humboldt-Toiyabe National Forest's
    actual size) must be refused outright, not scored as if that
    fragment were the whole park."""
    clipped_window = box(-119.913, 39.498, -119.826, 39.606)
    buckets = _one_anchor_bucket(39.55, -119.87, 50000.0)  # a big anchor swallowing the fragment whole
    with pytest.raises(bps.ClippedGeometryError):
        bps._frac_area_outside_city(clipped_window, buckets, true_area_m2=12_976e6)


def test_frac_area_outside_city_accepts_a_genuinely_small_park():
    """A real, honestly small/irregular park (well under the 20 km^2
    floor, and under the 1.5x ratio even above it) must NOT trip the
    guard just for having a bbox somewhat larger than its own area --
    an irregular shape's bbox padding is normal, not a clip artifact."""
    small_park = box(-116.001, 43.599, -115.999, 43.601)  # ~0.05 km^2 bbox
    buckets = _one_anchor_bucket(43.6, -116.0, 5000.0)
    frac = bps._frac_area_outside_city(small_park, buckets, true_area_m2=30_000.0)  # 0.03 km^2
    assert 0.0 <= frac <= 1.0


def test_frac_area_outside_city_no_true_area_skips_the_guard():
    """true_area_m2 defaults to None -- match_parks()/fetch_padus_parks()
    call this on the full, pre-clip polygon and never pass it, so the
    guard must stay off by default rather than requiring every real
    call site to thread an extra argument through."""
    clipped_window = box(-119.913, 39.498, -119.826, 39.606)
    buckets = _one_anchor_bucket(39.55, -119.87, 50000.0)
    frac = bps._frac_area_outside_city(clipped_window, buckets)
    assert 0.0 <= frac <= 1.0


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


# ---------------------------------------------------------------------
# BOUNDARY CLEANUP, PASS 2 (2026-09-07, continuing the sanity check
# above): duplicate geometry / designation-vs-scale / administrative
# envelope -- see _clean_matched_park_boundaries()'s own module comment
# above _match_passes_sanity_check for the full reasoning.
# ---------------------------------------------------------------------


def test_duplicate_geometry_keeps_both_on_a_tie_with_no_top_tier_winner():
    """The confirmed shipped-seed case: "Tonto State Fish Hatchery" and
    "Tonto Natural Bridge State Park" both matched Tonto National
    Forest's own polygon. Neither carries a big-scale designation, so
    the group has no single top-tier winner -- the tie-break fails open
    by keeping the boundary on every member rather than guessing which
    one truly owns it (see _resolve_duplicate_boundary_group()'s own
    docstring for why). This pair still ends up stripped by the
    downstream point-scale-ceiling check in
    _clean_matched_park_boundaries() (see
    test_clean_matched_park_boundaries_full_pipeline_on_the_confirmed_examples)
    -- that is a separate mechanism from this one."""
    keep = bps._resolve_duplicate_boundary_group(
        ["Tonto State Fish Hatchery", "Tonto Natural Bridge State Park"])
    assert keep == [True, True]


def test_duplicate_geometry_keeps_the_legitimate_owner_of_a_shared_boundary():
    """The confirmed shipped-seed case this rule generalizes from:
    "Cherokee Hills Scenic Byway Scenic Site" shared its exact area with
    the legitimately-matched "Cherokee Wildlife Management Area" --
    the scenic site is point-scale, the WMA is not, so the WMA alone
    keeps the boundary."""
    keep = bps._resolve_duplicate_boundary_group(
        ["Cherokee Wildlife Management Area", "Cherokee Hills Scenic Byway Scenic Site"])
    assert keep == [True, False]


def test_duplicate_geometry_does_not_punish_an_ambiguous_name_for_its_partner():
    """A real, correctly-sized "Ruby Lake National Wildlife Refuge" must
    not be dragged down just because its duplicate partner ("Fort Ruby
    National Historic Site") is a clearly bogus point-scale name --
    "National Wildlife Refuge" alone is never treated as automatically
    big-scale (some real refuges are tiny, some are huge), but it is
    still the only non-point-scale name in this group, so it wins."""
    keep = bps._resolve_duplicate_boundary_group(
        ["Ruby Lake National Wildlife Refuge", "Fort Ruby National Historic Site"])
    assert keep == [True, False]


def test_duplicate_geometry_keeps_both_on_a_tie_between_two_legitimate_designations():
    """Teton Wilderness Area and Jedediah Smith Wilderness Area share one
    2,366.3 km^2 polygon in the shipped seed -- both are a "Wilderness
    Area", so neither outranks the other, and there is no name evidence
    left to award the boundary to either one. Fails open to KEEPING the
    boundary on both rather than guessing which one owns it, and rather
    than stripping real, named wilderness from the board outright --
    see _resolve_duplicate_boundary_group()'s own docstring for the
    asymmetric-cost reasoning."""
    keep = bps._resolve_duplicate_boundary_group(
        ["Teton Wilderness Area", "Jedediah Smith Wilderness Area"])
    assert keep == [True, True]


def test_point_scale_designation_flags_the_documented_keywords():
    for name in (
        "Tonto State Fish Hatchery",
        "Tonto Natural Bridge State Park",
        "Oklahoma Route 66 Museum State Historic Site",
        "Cherokee Hills Scenic Byway Scenic Site",
        "South Pass Overlook BLM Interpretive Site",
        "ZOLD - Red Spring Picnic Area BLM Recreation Management Area",
        "Notch Peak Trailhead BLM Recreation Management Area",
    ):
        assert bps._is_point_scale_designation(name), name


def test_big_scale_designation_flags_national_forest_park_monument_wilderness():
    for name in (
        "Flathead National Forest",
        "Yosemite National Park",
        "Grand Staircase-Escalante BLM National Monument",
        "Frank Church-River of No Return Wilderness Area Wilderness Area",
        "Little Missouri National Grassland",
    ):
        assert bps._is_big_scale_designation(name), name
    # An ordinary Wildlife Management Area/Refuge/Recreation Area name
    # is deliberately NOT treated as big-scale -- see the module comment
    # above _duplicate_boundary_tier() for why (real sizes vary too much
    # to trust either way).
    for name in (
        "Cherokee Wildlife Management Area",
        "San Bernard National Wildlife Refuge",
        "Steens Mountain BLM Special Recreation Management Area",
    ):
        assert not bps._is_big_scale_designation(name), name


def test_clean_matched_park_boundaries_rejects_a_national_forest_with_a_genuinely_huge_boundary():
    """A national forest with a legitimate, unique multi-thousand-km^2
    boundary must survive the whole cleanup pass untouched -- this is
    the MUST-SURVIVE regression case (Flathead National Forest, the
    largest legitimate match measured in the shipped seed)."""
    rows = [
        {"ref_code": "US-4502", "name": "Flathead National Forest",
         "area_m2": 13_613_884_004.0, "geom_wkt": "POLYGON(...)", "area_frac_outside": 0.9884},
    ]
    stripped = bps._clean_matched_park_boundaries(rows)
    assert stripped == {}
    assert rows[0]["area_m2"] == 13_613_884_004.0


def test_clean_matched_park_boundaries_strips_an_administrative_envelope():
    """A Wetland Management District is stripped unconditionally, even
    with no duplicate partner and regardless of size."""
    rows = [
        {"ref_code": "US-0270", "name": "Iowa Wetland Management District",
         "area_m2": 50_831_300_000.0, "geom_wkt": "POLYGON(...)", "area_frac_outside": 1.0},
    ]
    stripped = bps._clean_matched_park_boundaries(rows)
    assert stripped == {"US-0270": "administrative envelope"}
    assert rows[0]["area_m2"] == ""
    assert rows[0]["geom_wkt"] == ""
    assert rows[0]["area_frac_outside"] == ""


def test_clean_matched_park_boundaries_full_pipeline_on_the_confirmed_examples():
    """End-to-end over a small mixed batch: the inherited duplicate pair
    is rejected, the legitimate owner of a shared boundary is kept, a
    point-scale designation over the ceiling is rejected on its own, and
    an unrelated national forest is untouched."""
    rows = [
        {"ref_code": "TONTO-FH", "name": "Tonto State Fish Hatchery",
         "area_m2": 11_601_596_788.0, "geom_wkt": "g", "area_frac_outside": ""},
        {"ref_code": "TONTO-NB", "name": "Tonto Natural Bridge State Park",
         "area_m2": 11_601_596_788.0, "geom_wkt": "g", "area_frac_outside": ""},
        {"ref_code": "CHEROKEE-WMA", "name": "Cherokee Wildlife Management Area",
         "area_m2": 18_034_700_000.0, "geom_wkt": "g", "area_frac_outside": ""},
        {"ref_code": "FISH-HATCHERY-LONE", "name": "Willamette State Fish Hatchery",
         "area_m2": 6_803_203_000.0, "geom_wkt": "g", "area_frac_outside": ""},
        {"ref_code": "FLATHEAD", "name": "Flathead National Forest",
         "area_m2": 13_613_884_004.0, "geom_wkt": "g", "area_frac_outside": 0.9884},
    ]
    stripped = bps._clean_matched_park_boundaries(rows)
    assert set(stripped) == {"TONTO-FH", "TONTO-NB", "FISH-HATCHERY-LONE"}
    by_code = {r["ref_code"]: r for r in rows}
    assert by_code["TONTO-FH"]["area_m2"] == ""
    assert by_code["TONTO-NB"]["area_m2"] == ""
    assert by_code["FISH-HATCHERY-LONE"]["area_m2"] == ""
    assert by_code["CHEROKEE-WMA"]["area_m2"] == 18_034_700_000.0
    assert by_code["FLATHEAD"]["area_m2"] == 13_613_884_004.0


def test_clean_matched_park_boundaries_ignores_matches_below_the_materiality_floor():
    """Two small parks coincidentally sharing a tiny area (well under
    DUPLICATE_CLEANUP_MIN_AREA_M2) are out of scope for this cleanup --
    see that constant's own comment for why."""
    rows = [
        {"ref_code": "A", "name": "Some Trailhead", "area_m2": 50_000.0,
         "geom_wkt": "g", "area_frac_outside": ""},
        {"ref_code": "B", "name": "Some Other Park", "area_m2": 50_000.0,
         "geom_wkt": "g", "area_frac_outside": ""},
    ]
    stripped = bps._clean_matched_park_boundaries(rows)
    assert stripped == {}


def test_known_bogus_boundary_match_is_stripped_even_with_no_generic_signal():
    """San Bernard National Wildlife Refuge: no duplicate partner in the
    seed and "National Wildlife Refuge" is not a point-scale designation
    -- only the named-exception list catches this one."""
    rows = [
        {"ref_code": "US-0553", "name": "San Bernard National Wildlife Refuge",
         "area_m2": 8_323_760_445.0, "geom_wkt": "g", "area_frac_outside": ""},
    ]
    stripped = bps._clean_matched_park_boundaries(rows)
    assert list(stripped) == ["US-0553"]
    assert rows[0]["area_m2"] == ""


# ---------------------------------------------------------------------
# _in_city_limits' OWN latitude-aware bucket scan (2026-09-07) -- this
# script's independent copy of the same bug app/places.py's
# distance_to_nearest_town_m was fixed for (see
# tests/test_places_bucketing.py). _in_city_limits used to scan a fixed
# 3x3 neighbourhood of whole-degree buckets around the query point on
# the strength of "this script's own candidate queries never leave the
# western play area" (see the comment that used to sit above
# _ANCHOR_BUCKET_DEG). That assumption went false the moment
# fetch_sota()/fetch_pota()/extract_landmarks() stopped bbox-filtering
# to the play area: a landmark or park can now be scored anywhere on
# Earth, including high-latitude places (Alaska, Scandinavia, northern
# Canada, Patagonia) where a degree of longitude shrinks well below what
# a fixed +-1-bucket window assumed.
# ---------------------------------------------------------------------


def _anchor_buckets_with_one(anchors) -> bps._AnchorBuckets:
    """Build a real _AnchorBuckets table (not a plain dict) from
    `anchors` ((lat, lon, radius_m) tuples), the same way
    _load_city_anchors does -- so these tests exercise the fast,
    precomputed-max_radius_m path _in_city_limits actually takes in
    production, not just the plain-dict scanning fallback the other
    tests in this file happen to use."""
    buckets = bps._AnchorBuckets()
    max_radius_m = 0.0
    for lat, lon, radius_m in anchors:
        key = (math.floor(lat / bps._ANCHOR_BUCKET_DEG), math.floor(lon / bps._ANCHOR_BUCKET_DEG))
        buckets.setdefault(key, []).append((lat, lon, radius_m))
        max_radius_m = max(max_radius_m, radius_m)
    buckets.max_radius_m = max_radius_m
    return buckets


def _in_city_limits_pre_fix(lat: float, lon: float, buckets: dict) -> bool:
    """The OLD _in_city_limits: a hardcoded 3x3 neighbourhood of
    whole-degree buckets around the query's own bucket, no matter the
    query's latitude or the anchor's radius. Reimplemented here (not
    imported -- the real function no longer works this way) purely so
    the high-latitude test below can demonstrate it actually fails
    against this logic, not just pass trivially against both."""
    lat_b = math.floor(lat / bps._ANCHOR_BUCKET_DEG)
    lon_b = math.floor(lon / bps._ANCHOR_BUCKET_DEG)
    for d_lat in (-1, 0, 1):
        for d_lon in (-1, 0, 1):
            for a_lat, a_lon, radius_m in buckets.get((lat_b + d_lat, lon_b + d_lon), ()):
                if bps._haversine_m(lat, lon, a_lat, a_lon) <= radius_m:
                    return True
    return False


def test_in_city_limits_antimeridian_wrap_finds_the_anchor_on_the_other_side():
    """A query just east of the antimeridian must still find an anchor
    just west of it -- _load_city_anchors buckets by floor(lon), so
    179.95 and -179.95 land in buckets 179 and -180, geographic
    neighbours but numeric opposites."""
    query_lat, query_lon = 0.0, 179.95
    anchor_lat, anchor_lon = 0.0, -179.95
    actual = bps._haversine_m(query_lat, query_lon, anchor_lat, anchor_lon)
    buckets = _anchor_buckets_with_one([(anchor_lat, anchor_lon, actual + 5_000)])

    assert bps._in_city_limits(query_lat, query_lon, buckets) is True


def test_in_city_limits_high_latitude_point_the_old_fixed_3x3_would_have_missed():
    """The actual bug: at 80N one degree of longitude is only ~19km, so
    an anchor three degrees of longitude away (outside the old fixed
    +-1-bucket window) can still have a large enough radius to cover the
    query point. Confirms BOTH sides: the fixed 3x3 genuinely fails this
    case (proving the test is not vacuous), and the real, fixed
    _in_city_limits finds it."""
    query_lat, query_lon = 80.0, 10.0
    anchor_lat, anchor_lon = 80.0, 13.0
    actual = bps._haversine_m(query_lat, query_lon, anchor_lat, anchor_lon)
    # 3 degrees of longitude at 80N is far outside the old +-1 bucket
    # scan, but comfortably inside this anchor's circle.
    buckets = _anchor_buckets_with_one([(anchor_lat, anchor_lon, actual + 5_000)])

    assert _in_city_limits_pre_fix(query_lat, query_lon, buckets) is False, (
        "test fixture must actually defeat the old fixed 3x3 scan -- otherwise "
        "this proves nothing about the fix"
    )
    assert bps._in_city_limits(query_lat, query_lon, buckets) is True


def test_in_city_limits_equatorial_query_still_scans_a_small_number_of_buckets():
    """Performance is the reason bucketing exists at all -- a low-
    latitude query (where a degree of longitude is close to its full
    ~111km) must still keep the window small, not silently pay the
    high-latitude cost everywhere."""
    reach_m = 51_700.0  # roughly the largest US anchor radius on file
    lat_span = bps._anchor_lat_bucket_span(reach_m)
    lon_span = bps._anchor_lon_bucket_span(0.0, reach_m)
    buckets_scanned = (2 * lat_span + 1) * (2 * lon_span + 1)
    assert buckets_scanned <= 9, f"expected a 3x3-ish window at the equator, got {buckets_scanned}"


def test_in_city_limits_plain_dict_fixture_still_works():
    """A plain dict (not built via _load_city_anchors, so it has no
    precomputed max_radius_m -- exactly what every OTHER test in this
    file passes to score_points()) must still get a correct, safe
    fallback: _anchor_reach_m scans the dict itself rather than trusting
    a missing attribute."""
    buckets = _buckets_with_one_anchor(80.0, 13.0, 5_000.0 + bps._haversine_m(80.0, 10.0, 80.0, 13.0))
    assert not isinstance(buckets, bps._AnchorBuckets)
    assert bps._in_city_limits(80.0, 10.0, buckets) is True
