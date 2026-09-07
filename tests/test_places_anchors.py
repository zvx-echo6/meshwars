"""Regression coverage for app/reference/places.csv's anchor set --
specifically the 2026-09-07 fix (scripts/build_places_csv.py) that
added Census urban-area rows alongside place rows.

Before that fix, San Francisco and Las Vegas both read as "remote"
(is_outside_town() True) despite sitting in the middle of a metro area
-- not because either city's Census place row was missing (both were
already anchors), but because the single-interior-point-plus-equal-
-area-circle model broke down for two unrelated reasons: San
Francisco's corporate limits include the non-contiguous Farallon
Islands exclave, which pulls its one Census interior point out to sea,
and the Las Vegas Strip sits in unincorporated Clark County (Winchester
/Paradise CDPs), outside the City of Las Vegas's own boundary and short
of the neighboring CDPs' small circles too. See
scripts/build_places_csv.py's module docstring for the full mechanism.

This test loads the REAL shipped app/reference/places.csv (not a
fixture) against a handful of major western US cities, so a future
regeneration that drops the urban-area stage -- or otherwise shrinks
coverage -- fails loudly here instead of silently shipping "remote"
landmarks in the middle of downtown San Francisco again.
"""
from __future__ import annotations

from app import places

# (name, lat, lon) -- a mix of the two originally-broken cities plus a
# spread of other major metros, all inside the play area.
COVERED_CITIES = [
    ("San Francisco, CA", 37.7749, -122.4194),
    ("Las Vegas, NV", 36.1699, -115.1398),
    ("Los Angeles, CA", 34.0522, -118.2437),
    ("Seattle, WA", 47.6062, -122.3321),
    ("Denver, CO", 39.7392, -104.9903),
    ("Boise, ID", 43.6150, -116.2023),
]

# Anchorage is outside the play area bbox (-125.0 to -93.5 W) even with
# the 1-degree margin -- failing here is correct, not a miss. Kept as a
# negative control so this test would notice if the bbox filter ever
# grew wide enough to accidentally start covering it.
OUT_OF_AREA_CITY = ("Anchorage, AK", 61.2181, -149.9003)


def test_places_data_loaded():
    # Sanity check the real file is actually present and non-trivial --
    # if this is ever near-zero, every test below would pass vacuously
    # for the wrong reason (no anchors at all near the query point is
    # indistinguishable from "far from every anchor").
    assert places.loaded_count() > 10_000


def test_major_cities_are_covered():
    failures = []
    for name, lat, lon in COVERED_CITIES:
        if places.is_outside_town(lat, lon):
            dist = places.distance_to_nearest_town_m(lat, lon)
            failures.append(f"{name}: distance to nearest anchor edge = {dist:.0f}m")
    assert not failures, "these cities read as outside every anchor circle:\n" + "\n".join(failures)


def test_san_francisco_specifically_inside_city_limits():
    """The Farallon-exclave case: San Francisco city's own Census
    interior point sits far out in the ocean, so this must be covered
    by some OTHER anchor (Daly City, or the SF-Oakland urban area) --
    not by San Francisco's own row."""
    lat, lon = 37.7749, -122.4194
    assert places.distance_to_nearest_town_m(lat, lon) == 0.0


def test_las_vegas_specifically_inside_city_limits():
    """The Strip-in-unincorporated-county case: the Las Vegas city
    anchor and the neighboring Winchester/Paradise CDP anchors each
    individually fall short of this point; it takes the Las Vegas
    urban-area anchor to close the gap."""
    lat, lon = 36.1699, -115.1398
    assert places.distance_to_nearest_town_m(lat, lon) == 0.0


def test_out_of_area_city_correctly_reads_remote():
    name, lat, lon = OUT_OF_AREA_CITY
    assert places.is_outside_town(lat, lon) is True
