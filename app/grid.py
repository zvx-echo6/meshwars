"""Pure grid-cell math for MeshCore ingest. No state, no database access.

Cells are aligned to a fixed lat/lon grid, not geohash, so cell size is
uniform and predictable everywhere (geohash cells warp with latitude).
"""
from __future__ import annotations

import math

CELL_LAT_DEG = 0.0027
CELL_LON_DEG = 0.00384

_EARTH_RADIUS_M = 6371000.0


def cell_id(lat: float, lon: float) -> str:
    """Return the "<latIdx>_<lonIdx>" id of the cell containing (lat, lon).

    Uses math.floor, not int(). int() truncates toward zero, which puts
    negative coordinates in the wrong cell and creates a double-width
    cell straddling the equator and the prime meridian -- do not
    "simplify" this to int().
    """
    lat_idx = math.floor(lat / CELL_LAT_DEG)
    lon_idx = math.floor(lon / CELL_LON_DEG)
    return f"{lat_idx}_{lon_idx}"


def cell_bounds(cid: str) -> tuple[float, float, float, float]:
    """Return (south, west, north, east) for a cell id."""
    lat_str, lon_str = cid.split("_")
    lat_idx = int(lat_str)
    lon_idx = int(lon_str)
    south = lat_idx * CELL_LAT_DEG
    north = (lat_idx + 1) * CELL_LAT_DEG
    west = lon_idx * CELL_LON_DEG
    east = (lon_idx + 1) * CELL_LON_DEG
    return (south, west, north, east)


def cell_center(cid: str) -> tuple[float, float]:
    """Return (lat, lon) of the center of a cell id."""
    south, west, north, east = cell_bounds(cid)
    return ((south + north) / 2.0, (west + east) / 2.0)


def valid_coord(lat: float, lon: float) -> bool:
    """False when lat/lon are not finite numbers, out of range, or the
    (0, 0) "null island" sentinel some broken GPS hardware reports.
    """
    if not isinstance(lat, (int, float)) or isinstance(lat, bool):
        return False
    if not isinstance(lon, (int, float)) or isinstance(lon, bool):
        return False
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return False
    if not (-90.0 <= lat <= 90.0):
        return False
    if not (-180.0 <= lon <= 180.0):
        return False
    if lat == 0 and lon == 0:
        return False
    return True


def in_play_area(
    lat: float, lon: float, north: float, south: float, west: float, east: float,
) -> bool:
    """True if (lat, lon) is inside the box bounded by north/south
    latitude and west/east longitude.

    Setting north == south disables the check entirely (always True) --
    this stays pure grid math with no config dependency, so the caller
    passes the configured box in rather than this module reading
    settings itself.
    """
    if north == south:
        return True
    return south <= lat <= north and west <= lon <= east


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance between two points, in metres."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return _EARTH_RADIUS_M * c
