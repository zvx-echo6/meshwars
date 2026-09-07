"""Regression coverage for app/places.py's latitude-aware bucket scan
(2026-09-07 fix). The scan used to be a hardcoded 3x3 neighbourhood of
whole-degree buckets, sized against the reasoning "one degree of
longitude is at worst about 100 km" -- true only at the low-latitude
edge of the old western-US play area. A degree of longitude shrinks
with latitude (111 km * cos(lat)), and the anchor set is now national
(largest radius 51.7 km) with a global set coming, with the play-area
gate disabled in production so query points can be anywhere on Earth.
At high latitude a real anchor's own bucket can sit entirely outside a
fixed 3x3 window even though the query point is inside that anchor's
circle -- the lookup then silently reports a point as remote when it
is not.

These tests exercise app.places.distance_to_nearest_town_m directly
against a small synthetic anchor set (monkeypatched module globals,
not the real 34k-row CSV) so each case is exact and independent of
whatever the shipped data happens to contain.
"""
from __future__ import annotations

import math

from app import places
from app.grid import distance_m


def _install_anchors(monkeypatch, anchors):
    """Replace the loaded bucket table with exactly `anchors`
    ((lat, lon, radius_m) tuples), bucketed the same way _load() does,
    and set _MAX_RADIUS_M to match -- both are module globals _load()
    would otherwise compute from the real CSV."""
    buckets: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    max_radius = 0.0
    for lat, lon, radius in anchors:
        buckets.setdefault((math.floor(lat), math.floor(lon)), []).append((lat, lon, radius))
        max_radius = max(max_radius, radius)
    monkeypatch.setattr(places, "_BUCKETS", buckets)
    monkeypatch.setattr(places, "_MAX_RADIUS_M", max_radius)


def test_antimeridian_wrap_finds_the_anchor_on_the_other_side(monkeypatch):
    """A query just east of the antimeridian must still find an anchor
    just west of it -- _load() buckets by floor(lon), so 179.95 and
    -179.95 land in buckets 179 and -180, which are geographic
    neighbours but numeric opposites."""
    query_lat, query_lon = 0.0, 179.95
    anchor_lat, anchor_lon = 0.0, -179.95
    actual = distance_m(query_lat, query_lon, anchor_lat, anchor_lon)
    _install_anchors(monkeypatch, [(anchor_lat, anchor_lon, actual + 5_000)])

    assert places.distance_to_nearest_town_m(query_lat, query_lon) == 0.0


def test_high_latitude_point_inside_circle_whose_anchor_is_outside_old_3x3(monkeypatch):
    """The actual bug: at 80N one degree of longitude is only ~19 km,
    so an anchor three degrees of longitude away (outside the old
    fixed +-1 bucket window) can still have a large enough radius to
    cover the query point. The old hardcoded 3x3 scan would never even
    look at that anchor's bucket and would report this point as
    remote; the fix must size the window from the real reach and find
    it. (Verified against a checkout of the pre-fix 3x3 logic: this
    assertion fails there.)"""
    query_lat, query_lon = 80.0, 10.0
    anchor_lat, anchor_lon = 80.0, 13.0
    actual = distance_m(query_lat, query_lon, anchor_lat, anchor_lon)
    # 3 degrees of longitude at 80N is far outside the old +-1 bucket
    # scan, but comfortably inside this anchor's circle.
    _install_anchors(monkeypatch, [(anchor_lat, anchor_lon, actual + 5_000)])

    assert places.distance_to_nearest_town_m(query_lat, query_lon) == 0.0


def test_equatorial_query_still_scans_a_small_number_of_buckets():
    """Performance is the reason bucketing exists at all -- a low-
    latitude query (where a degree of longitude is close to its full
    ~111 km) must still keep the window small, not silently pay the
    high-latitude cost everywhere."""
    reach_m = places._MIN_UNKNOWN_FAR_M
    lat_span = places._lat_bucket_span(reach_m)
    lon_span = places._lon_bucket_span(0.0, reach_m)
    buckets_scanned = (2 * lat_span + 1) * (2 * lon_span + 1)
    assert buckets_scanned <= 9, f"expected a 3x3-ish window at the equator, got {buckets_scanned}"


def test_high_latitude_query_widens_the_longitude_span():
    """Sanity check on the derivation itself: the same reach needs
    strictly more longitude buckets at 80N than at the equator."""
    reach_m = places._MIN_UNKNOWN_FAR_M
    assert places._lon_bucket_span(80.0, reach_m) > places._lon_bucket_span(0.0, reach_m)


def test_polar_span_is_capped_rather_than_unbounded():
    """Within a hair of a pole, a degree of longitude covers almost no
    distance at all -- the span must be capped at scanning every
    longitude bucket (180) rather than looping toward infinity or
    dividing by zero."""
    assert places._lon_bucket_span(89.9999, places._MIN_UNKNOWN_FAR_M) == 180
    assert places._lon_bucket_span(90.0, places._MIN_UNKNOWN_FAR_M) == 180


def test_none_when_place_data_unavailable(monkeypatch):
    monkeypatch.setattr(places, "_BUCKETS", {})
    monkeypatch.setattr(places, "_MAX_RADIUS_M", 0.0)

    assert places.distance_to_nearest_town_m(45.0, -100.0) is None
    assert places.is_outside_town(45.0, -100.0) is None
    assert places.is_frontier(45.0, -100.0, 20.0) is None


def test_min_unknown_far_m_when_neighbourhood_is_genuinely_empty(monkeypatch):
    """An anchor far outside the scanned neighbourhood (even a wide
    one) must not be found, and the honest floor -- not an invented
    precise figure -- comes back instead."""
    _install_anchors(monkeypatch, [(0.0, 0.0, 1_000.0)])

    d = places.distance_to_nearest_town_m(45.0, 45.0)
    assert d == places._MIN_UNKNOWN_FAR_M


def test_inside_circle_still_returns_exact_zero(monkeypatch):
    _install_anchors(monkeypatch, [(43.6150, -116.2023, 10_000.0)])

    assert places.distance_to_nearest_town_m(43.6150, -116.2023) == 0.0


def test_nonzero_distance_reports_edge_not_centre(monkeypatch):
    lat, lon = 43.0, -116.0
    anchor_lat, anchor_lon, radius = 43.0, -116.2, 5_000.0
    _install_anchors(monkeypatch, [(anchor_lat, anchor_lon, radius)])

    expected = max(distance_m(lat, lon, anchor_lat, anchor_lon) - radius, 0.0)
    got = places.distance_to_nearest_town_m(lat, lon)
    assert got is not None
    assert math.isclose(got, expected, rel_tol=1e-9)
