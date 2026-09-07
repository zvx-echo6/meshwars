"""Regression coverage for app/reference/places.csv's anchor set --
specifically the 2026-09-07 fix (scripts/build_places_csv.py) that
added Census urban-area rows alongside place rows, and the same-day
scope widening from the western-US play area to the full 50 states +
DC (the play-area check is disabled in production; the board is
world-open).

Before the urban-area fix, San Francisco and Las Vegas both read as
"remote" (is_outside_town() True) despite sitting in the middle of a
metro area -- not because either city's Census place row was missing
(both were already anchors), but because the single-interior-point-
-plus-equal-area-circle model broke down. Checking the same failure
mode nationwide (not just in the original western play area) turned up
two more real instances, New Orleans and Orlando, via two further
variants of the same underlying problem:

  - San Francisco: corporate limits include the non-contiguous
    Farallon Islands exclave, pulling its one Census interior point
    out to sea.
  - Las Vegas: the Strip sits in unincorporated Clark County
    (Winchester/Paradise CDPs), outside the City of Las Vegas's own
    boundary, and short of the neighboring CDPs' small circles too.
  - New Orleans: coterminous with Orleans Parish, whose limits reach
    across a huge swath of Lake Pontchartrain (nearly half the
    jurisdiction is water) -- a contiguous version of San Francisco's
    problem, dragging the interior point well away from the French
    Quarter/CBD.
  - Orlando: decades of annexation, including large parcels far
    southeast of downtown, pull the interior point toward the
    annexed area rather than the historic core.

All four are jurisdiction-boundary artifacts, not population
artifacts, and all four are closed the same way: a Census urban-area
anchor, drawn from population density rather than legal lines. See
scripts/build_places_csv.py's module docstring for the full mechanism
and root-cause detail on each.

This test loads the REAL shipped app/reference/places.csv (not a
fixture) against a spread of major US cities coast to coast, so a
future regeneration that drops the urban-area stage, reintroduces a
bbox/play-area filter, or otherwise shrinks coverage fails loudly here
instead of silently shipping "remote" landmarks in the middle of
downtown San Francisco (or New Orleans, or Orlando) again.

WORLDWIDE EXPANSION (2026-09-07, scripts/build_places_osm_anchors.py):
the anchor set is no longer US-only -- every non-US place used to read
"remote" by default (Census is a US-only source), which was wrong once
the game went worldwide. The US side above is completely unchanged
(same Census rows, same tests); the new WORLD_CITIES coverage below
guards the added OpenStreetMap side the same way -- a future
regeneration that drops the OSM stage, reintroduces a US-only scope, or
otherwise loses non-US coverage fails loudly here instead of silently
shipping "remote" for London or Tokyo again. NON_US_CITY (the negative
control) changed from Toronto to Point Nemo for exactly this reason:
Toronto is now correctly covered by a real OSM anchor, so it stopped
being a valid "nothing anchors here" control.
"""
from __future__ import annotations

from app import places

# (name, lat, lon) -- the two originally-broken western cities plus a
# spread of other major western metros.
WESTERN_CITIES = [
    ("San Francisco, CA", 37.7749, -122.4194),
    ("Las Vegas, NV", 36.1699, -115.1398),
    ("Los Angeles, CA", 34.0522, -118.2437),
    ("Seattle, WA", 47.6062, -122.3321),
    ("Denver, CO", 39.7392, -104.9903),
    ("Boise, ID", 43.6150, -116.2023),
]

# Added 2026-09-07 when the anchor set went national: a spread of major
# eastern/central US cities, including the two (New Orleans, Orlando)
# that turned out to need the urban-area fix just like SF/Las Vegas.
EASTERN_CITIES = [
    ("New York, NY", 40.7128, -74.0060),
    ("Chicago, IL", 41.8781, -87.6298),
    ("Atlanta, GA", 33.7490, -84.3880),
    ("Miami, FL", 25.7617, -80.1918),
    ("Boston, MA", 42.3601, -71.0589),
    ("Washington, DC", 38.9072, -77.0369),
    ("New Orleans, LA", 29.9511, -90.0715),
    ("Orlando, FL", 28.5383, -81.3792),
    ("Minneapolis, MN", 44.9778, -93.2650),
]

COVERED_CITIES = WESTERN_CITIES + EASTERN_CITIES

# Added 2026-09-07, worldwide expansion (scripts/build_places_osm_anchors.py):
# a spread of major non-US cities across every inhabited continent,
# confirmed covered by the real shipped file. Deliberately excludes a
# handful of major cities (Mumbai, Cairo, Johannesburg, Stockholm,
# Reykjavik) that are KNOWN, understood misses under the current OSM
# classification rule -- in each case a real, well-populated place=city
# node exists, but the containing administrative boundary in OSM is
# named differently from the city's common name (e.g. Mumbai's node
# sits inside a boundary named "Greater Mumbai"/"Brihanmumbai", not
# "Mumbai"), so the exact-normalized-name-match rule never connects
# them. That is a real, reported limitation of the classification rule,
# not something this test should paper over by asserting coverage that
# does not exist.
WORLD_CITIES = [
    ("London, UK", 51.5074, -0.1278),
    ("Paris, France", 48.8566, 2.3522),
    ("Tokyo, Japan", 35.6762, 139.6503),
    ("Berlin, Germany", 52.5200, 13.4050),
    ("Madrid, Spain", 40.4168, -3.7038),
    ("Rome, Italy", 41.9028, 12.4964),
    ("Sydney, Australia", -33.8688, 151.2093),
    ("Toronto, Canada", 43.6532, -79.3832),
    ("Mexico City, Mexico", 19.4326, -99.1332),
    ("Sao Paulo, Brazil", -23.5505, -46.6333),
    ("Buenos Aires, Argentina", -34.6037, -58.3816),
    ("Delhi, India", 28.7041, 77.1025),
    ("Shanghai, China", 31.2304, 121.4737),
    ("Seoul, South Korea", 37.5665, 126.9780),
    ("Nairobi, Kenya", -1.2921, 36.8219),
    ("Istanbul, Turkey", 41.0082, 28.9784),
    ("Moscow, Russia", 55.7558, 37.6173),
    ("Auckland, New Zealand", -36.8485, 174.7633),
]

# A genuine negative control: literally the farthest point on Earth
# from any land -- "Point Nemo", the oceanic pole of inaccessibility in
# the South Pacific, ~2,700km from the nearest coastline in any
# direction. Reading as remote here is correct under any anchor set
# this file could ever hold, worldwide expansion included -- unlike the
# old control (Toronto), which stopped being valid the moment the OSM
# expansion correctly gave Toronto a real anchor (see
# test_toronto_specifically_covered_by_worldwide_expansion below).
NON_US_CITY = ("Point Nemo (oceanic pole of inaccessibility)", -48.876, -123.393)


def test_places_data_loaded():
    # Sanity check the real file is actually present and non-trivial --
    # if this is ever near-zero, every test below would pass vacuously
    # for the wrong reason (no anchors at all near the query point is
    # indistinguishable from "far from every anchor").
    assert places.loaded_count() > 30_000


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


def test_new_orleans_specifically_inside_city_limits():
    """The lake-boundary case: New Orleans city's interior point is
    dragged ~17km off the French Quarter by Orleans Parish's limits
    reaching across Lake Pontchartrain; the New Orleans urban area
    closes the gap."""
    lat, lon = 29.9511, -90.0715
    assert places.distance_to_nearest_town_m(lat, lon) == 0.0


def test_orlando_specifically_inside_city_limits():
    """The annexation case: Orlando city's interior point is pulled
    ~19km southeast of downtown toward its own large annexed parcels;
    the Orlando urban area closes the gap."""
    lat, lon = 28.5383, -81.3792
    assert places.distance_to_nearest_town_m(lat, lon) == 0.0


def test_anchorage_now_covered_nationally():
    """Alaska is one of the 50 states -- now that the anchor set is
    national rather than play-area-bboxed, Anchorage must be covered
    like any other state capital-sized city, not treated as remote."""
    lat, lon = 61.2181, -149.9003
    assert places.is_outside_town(lat, lon) is False


def test_non_us_city_correctly_reads_remote():
    name, lat, lon = NON_US_CITY
    assert places.is_outside_town(lat, lon) is True


def test_world_cities_are_covered():
    """Guards the OSM (non-US) side of the 2026-09-07 worldwide
    expansion the same way test_major_cities_are_covered guards the US
    Census side -- a regeneration that drops the OSM stage, reverts to
    a US-only scope, or otherwise loses non-US coverage fails loudly
    here instead of silently shipping "remote" for London or Tokyo."""
    failures = []
    for name, lat, lon in WORLD_CITIES:
        if places.is_outside_town(lat, lon):
            dist = places.distance_to_nearest_town_m(lat, lon)
            failures.append(f"{name}: distance to nearest anchor edge = {dist:.0f}m")
    assert not failures, "these world cities read as outside every anchor circle:\n" + "\n".join(failures)


def test_toronto_specifically_covered_by_worldwide_expansion():
    """A first attempt at the US-exclusion test (three lat/lon bounding
    boxes for CONUS/Alaska/Hawaii) measured wrong here: Toronto sits
    inside a CONUS-shaped box because the US-Canada border is not a
    rectangle -- Canada's populated strip east of Minnesota (Toronto,
    Ottawa, Montreal) sits well south of the 49th parallel, squarely in
    that box's latitude band. The bbox silently dropped Toronto's real
    OSM anchor entirely. Real point-in-polygon containment against
    OSM's own "United States" country boundary (see
    scripts/build_places_osm_anchors.py) has no such failure mode --
    this pins Toronto specifically so that regression cannot recur
    silently."""
    lat, lon = 43.6532, -79.3832
    assert places.distance_to_nearest_town_m(lat, lon) == 0.0


# ---------------------------------------------------------------------
# Seed-level regression: the anchor tests above cover app/places.py's
# runtime lookup, but the actual scored value a player sees lives in
# app/reference/places_worth_going.csv's `points` column, baked in at
# seed-build time (see docs/features/places.md's "Values and the cap").
# That column does not automatically follow an anchor-set change --
# it took an explicit re-rate pass (2026-09-07) to bring the shipped
# seed in line with the widened anchor set this file tests, and
# nothing stops a future anchor change from shipping without a
# matching re-rate. This reads the real seed CSV directly (a plain
# csv.DictReader over ~77k rows, not a places_seed.load_places_seed()
# call -- that loader's own tests avoid the real file because a full
# load's park-boundary geometry work takes on the order of a minute;
# this only needs one row's two columns) so a future regeneration that
# reverts to the old anchor set, or otherwise forgets to re-rate,
# fails here instead of silently shipping Golden Gate Park as "remote"
# again.
# ---------------------------------------------------------------------

import csv
import gzip
import os

# .gz (2026-09-07, the worldwide expansion -- see app/places_seed.py's
# _open_csv) -- this test predates that change and used to open the
# plain CSV directly; gzip.open(path, "rt") is the same gunzip-
# transparently-or-not helper that module uses, duplicated here rather
# than imported so this test file does not need app/ on its path for a
# two-line file open.
_SEED_CSV_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "reference", "places_worth_going.csv.gz"
)


def _seed_row(ref_code: str) -> dict:
    with gzip.open(_SEED_CSV_PATH, "rt", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["ref_code"] == ref_code:
                return row
    raise AssertionError(f"{ref_code} not found in {_SEED_CSV_PATH}")


def test_golden_gate_park_seed_rates_in_city():
    """Golden Gate Park (PADUS-181249) is a large PAD-US-matched park
    that used to score the flat remote rate (25) under the old
    place-only anchor set -- its own centre point/whole shape sits
    comfortably inside San Francisco, but the pre-urban-area anchor
    model missed it the same way it missed San Francisco generally
    (see this file's module docstring). Post re-rate it must land at
    the in-city rate (5) via the whole-park area-fraction test."""
    row = _seed_row("PADUS-181249")
    assert row["name"] == "Golden Gate Park"
    assert row["points"] == "5"
    assert row["points_reason"] == "in_city_by_area"


def test_alcatraz_seed_rates_in_city():
    """Alcatraz Island National Historic Site (US-7888) has no matched
    PAD-US boundary, so it is scored by the plain point test rather
    than the area-fraction one -- a different code path than Golden
    Gate Park above, worth covering separately."""
    row = _seed_row("US-7888")
    assert row["name"] == "Alcatraz Island National Historic Site"
    assert row["points"] == "5"
    assert row["points_reason"] == "in_city"
