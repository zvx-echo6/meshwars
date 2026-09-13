"""How far a square is from the nearest town.

Used only by the Frontier award (app/results.py) and, through the same
Census anchors, by the seed's effort scoring (scripts/build_places_seed.py's
"city limits" test). NOT by Explorer: Explorer is not an award at all --
it is the season-long Places Worth Going points ranking that feeds a
player's total score (app/public_api._player_rows, app/mc_api's
top_explorer_for), the same shape NetOps has, and it never reads this
module. Nothing on the scoring or ingest path reads this --
territory does not care where it is, only whether the radio reached a
repeater.

The data is app/reference/places.csv, derived from the US Census 2024
Gazetteer places file and filtered to the play area plus a degree of
margin (a town just outside the box still matters to a square inside
it). Each row is a place's interior point plus an EFFECTIVE RADIUS:
sqrt(ALAND / pi), the radius of a circle with the same land area the
Census records for that place.

That radius is the whole reason this is a flat file rather than a
polygon library. "Twenty miles beyond city limits" needs the limits,
and real limits are ragged multi-polygons -- thousands of them across
twelve states, a heavy dependency, and a lot of precision that makes no
difference twenty miles out. A circle of equal area puts Boise's edge
about seven miles from its centre and a hamlet's about a thousand feet
from its own, which is the distinction that actually matters here. It
does mean a long thin city reads as rounder than it is; at this
distance that is noise.

Distances are to the place's EDGE, not its centre -- max(0, distance to
centre - radius) -- so "outside city limits" means outside the circle,
and a big city pushes its frontier further out than a village does.
"""
from __future__ import annotations

import logging
import math
import os

from .grid import distance_m

log = logging.getLogger("places")

# Not app/data/: .gitignore excludes "data/" for the runtime Docker
# volume, which silently swallowed this file the first time it lived
# there -- the image built without it and the exploration awards skipped
# themselves with only a log line to say so. "reference" is also the
# truer name: this is static data shipped with the code, not the
# mutable /data the container mounts.
_DATA_PATH = os.path.join(os.path.dirname(__file__), "reference", "places.csv")

# Places are bucketed into whole-degree cells so a lookup scans a small
# neighbourhood of buckets rather than all thirty-four thousand rows.
# That neighbourhood used to be a hardcoded 3x3, on the reasoning that
# one degree of longitude is at worst about 100 km -- true at the south
# edge of the old western-US play area, but a degree of longitude keeps
# shrinking with latitude (111 km * cos(lat): ~73 km at 49N, ~54 km at
# 61N, ~19 km at 80N) and the anchor set is now national, with a global
# set coming. A fixed 3x3 window can sit entirely outside a real
# anchor's bucket at high latitude even though the query point is
# inside that anchor's circle -- see _lon_bucket_span.
#
# So the window is sized per query instead: enough longitude buckets to
# cover REACH metres at the query's own latitude (derived from
# cos(latitude), capped at scanning every longitude once that would
# wrap past the whole globe), and enough latitude buckets to cover
# REACH metres too (a degree of latitude is close enough to constant
# everywhere that this rarely needs to grow past 1, but it is derived
# rather than assumed). REACH itself is derived from the loaded data
# (see _MAX_RADIUS_M) rather than a hardcoded figure, so a future
# anchor set with a larger radius widens the window on its own instead
# of silently reintroducing this bug.
_BUCKETS: dict[tuple[int, int], list[tuple[float, float, float]]] | None = None

# Largest effective radius among the loaded anchors, in metres. Set by
# _load(); 0.0 until then. Drives how far the bucket scan has to reach
# to guarantee it can never miss an anchor whose circle could contain
# the query point -- see _reach_m.
_MAX_RADIUS_M = 0.0

# A degree of latitude is close enough to constant across the globe
# (110.57 km at the equator to 111.69 km at the poles) to treat as one
# figure everywhere. Longitude buckets convert this further by
# cos(latitude) -- see _lon_bucket_span.
_METRES_PER_DEGREE_LAT = 111_320.0

MILE_M = 1609.344


def _load() -> dict[tuple[int, int], list[tuple[float, float, float]]]:
    global _BUCKETS, _MAX_RADIUS_M
    if _BUCKETS is not None:
        return _BUCKETS

    buckets: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    count = 0
    max_radius = 0.0
    try:
        with open(_DATA_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(",")
                if len(parts) != 3:
                    continue
                try:
                    lat = float(parts[0])
                    lon = float(parts[1])
                    radius = float(parts[2])
                except ValueError:
                    continue
                buckets.setdefault((int(math.floor(lat)), int(math.floor(lon))), []).append(
                    (lat, lon, radius)
                )
                count += 1
                if radius > max_radius:
                    max_radius = radius
    except OSError:
        # Missing or unreadable data file: every lookup then reports an
        # unknown distance, and the exploration awards skip themselves
        # rather than handing Frontier to whoever happens to be closest
        # to nothing. See distance_to_nearest_town_m.
        log.exception("places: could not read %s -- exploration awards will be skipped", _DATA_PATH)
        _BUCKETS = {}
        return _BUCKETS

    log.info("places: loaded %d places into %d buckets", count, len(buckets))
    _BUCKETS = buckets
    _MAX_RADIUS_M = max_radius
    return _BUCKETS


def _reach_m() -> float:
    """Metres the bucket scan must guarantee reaching from any query
    point. At least the largest effective radius among loaded anchors,
    so an anchor whose circle could contain the query point is never
    missed regardless of where it sits -- and at least
    _MIN_UNKNOWN_FAR_M, so an empty neighbourhood really does mean "no
    place for at least this far" rather than an artifact of how little
    got scanned."""
    return max(_MAX_RADIUS_M, _MIN_UNKNOWN_FAR_M)


def _lat_bucket_span(reach_m: float) -> int:
    """Latitude buckets to extend above and below the query's own bucket
    to guarantee `reach_m` metres of north-south coverage. A degree of
    latitude is close enough to constant everywhere that this rarely
    needs to exceed 1, but it is derived from the actual reach rather
    than assumed."""
    return max(1, math.ceil(reach_m / _METRES_PER_DEGREE_LAT))


def _lon_bucket_span(lat: float, reach_m: float) -> int:
    """Longitude buckets to extend east and west of the query's own
    bucket to guarantee `reach_m` metres of east-west coverage AT THIS
    LATITUDE. A degree of longitude covers _METRES_PER_DEGREE_LAT *
    cos(latitude) metres -- about 111 km at the equator, shrinking
    toward zero at the poles -- so the same metre reach needs more
    buckets the further from the equator the query sits. Capped at 180:
    past that the maths is asking for more width than a lap of the
    globe at this latitude, and the caller scans every longitude bucket
    instead (see distance_to_nearest_town_m)."""
    cos_lat = math.cos(math.radians(lat))
    if cos_lat < 1e-9:
        # Within a hair of a pole: a degree of longitude is essentially
        # a point, so no finite span would do -- scan every bucket.
        return 180
    return min(180, math.ceil(reach_m / (_METRES_PER_DEGREE_LAT * cos_lat)))


def _wrap_lon_bucket(bucket: int) -> int:
    """Normalise a longitude bucket index to the [-180, 179] range
    _load() actually keys buckets with (floor() of a longitude in
    [-180, 180)), so a scan that walks past +179 or below -180 finds
    the antimeridian-wrapped bucket instead of an empty one."""
    return ((bucket + 180) % 360) - 180


def loaded_count() -> int:
    """How many places are available, for a caller that wants to say so
    (or to decide the data is missing and skip an award)."""
    return sum(len(v) for v in _load().values())


def distance_to_nearest_town_m(lat: float, lon: float) -> float | None:
    """Metres from this point to the nearest town's EDGE, or None if the
    place data is unavailable.

    Zero means inside a town's circle. None is not "very far" and must
    never be treated as such -- it means we do not know, and a caller
    deciding an exploration award should skip rather than guess.
    """
    buckets = _load()
    if not buckets:
        return None

    reach_m = _reach_m()
    blat = int(math.floor(lat))
    blon = int(math.floor(lon))
    lat_span = _lat_bucket_span(reach_m)
    lon_span = _lon_bucket_span(lat, reach_m)
    full_lon_sweep = lon_span >= 180
    lon_offsets = range(-180, 180) if full_lon_sweep else range(-lon_span, lon_span + 1)

    best: float | None = None
    for dlat in range(-lat_span, lat_span + 1):
        plat = blat + dlat
        for dlon in lon_offsets:
            # In a full sweep dlon IS already a bucket key (-180..179);
            # otherwise it is an offset from the query's own bucket that
            # may need wrapping at the antimeridian.
            plon = dlon if full_lon_sweep else _wrap_lon_bucket(blon + dlon)
            for plat2, plon2, radius in buckets.get((plat, plon), ()):
                edge = distance_m(lat, lon, plat2, plon2) - radius
                if best is None or edge < best:
                    best = edge
                    if best <= 0:
                        return 0.0

    if best is None:
        # No place within the scanned neighbourhood at all. That is
        # real remoteness -- open terrain, or off the edge of the
        # padded data -- and the honest answer is "at least as far as
        # this neighbourhood reaches" (see _reach_m), not an exact
        # figure.
        return _MIN_UNKNOWN_FAR_M
    return max(best, 0.0)


# Floor reported when the bucket neighbourhood holds no place at all.
# Deliberately a real distance rather than None: the point IS genuinely
# remote, and reporting "unknown" would exclude exactly the squares
# Frontier exists to reward. 100 km is also the minimum reach _reach_m
# guarantees the scan actually covers, so this floor is always honest
# regardless of how far the query's own latitude stretches a longitude
# bucket.
_MIN_UNKNOWN_FAR_M = 100_000.0


def is_outside_town(lat: float, lon: float) -> bool | None:
    """True if this point lies beyond every town's circle. None if the
    place data is unavailable."""
    d = distance_to_nearest_town_m(lat, lon)
    return None if d is None else d > 0.0


def is_frontier(lat: float, lon: float, miles: float) -> bool | None:
    """True if this point is more than `miles` beyond the nearest town's
    edge. None if the place data is unavailable."""
    d = distance_to_nearest_town_m(lat, lon)
    return None if d is None else d > miles * MILE_M
