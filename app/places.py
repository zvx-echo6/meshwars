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

# Places are bucketed into whole-degree cells so a lookup scans its own
# bucket and the eight around it rather than all thirteen thousand rows.
# One degree of latitude is about 111 km and one of longitude is at
# worst about 100 km (at the south edge of the play area), so a 3x3
# neighbourhood always reaches at least 100 km from the query point.
# The furthest a place can sit and still disqualify a square is the
# frontier distance (32 km) plus the largest effective radius in the
# file (about 25 km) -- comfortably inside that, with room for the
# threshold to be raised later.
_BUCKETS: dict[tuple[int, int], list[tuple[float, float, float]]] | None = None

MILE_M = 1609.344


def _load() -> dict[tuple[int, int], list[tuple[float, float, float]]]:
    global _BUCKETS
    if _BUCKETS is not None:
        return _BUCKETS

    buckets: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    count = 0
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
    return _BUCKETS


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

    blat = int(math.floor(lat))
    blon = int(math.floor(lon))

    best: float | None = None
    for dlat in (-1, 0, 1):
        for dlon in (-1, 0, 1):
            for plat, plon, radius in buckets.get((blat + dlat, blon + dlon), ()):
                edge = distance_m(lat, lon, plat, plon) - radius
                if best is None or edge < best:
                    best = edge
                    if best <= 0:
                        return 0.0

    if best is None:
        # No place within the 3x3 neighbourhood at all. That is real
        # remoteness -- open desert, or off the edge of the padded data
        # -- and the honest answer is "at least as far as this
        # neighbourhood reaches", not an exact figure.
        return _MIN_UNKNOWN_FAR_M
    return max(best, 0.0)


# Floor reported when the 3x3 bucket neighbourhood holds no place at
# all. Deliberately a real distance rather than None: the point IS
# genuinely remote, and reporting "unknown" would exclude exactly the
# squares Frontier exists to reward. 100 km is the smallest distance the
# neighbourhood can guarantee.
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
