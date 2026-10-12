"""Regions: MeshMapper-derived boundaries used as a VIEW and STANDINGS
filter. Nothing in here changes scoring -- a region only narrows which
squares, exploration awards and captures a read endpoint counts.

What lives here:

- geometry (Shape, RegionIndex): pure-Python point-in-polygon with a
  banded edge index, because shapely is not part of the runtime for this
  feature and a 2,660-vertex boundary (SMN) tested naively against tens
  of thousands of squares is minutes of work.
- parsing of the two MeshMapper pages the importer reads.
- the DB side: region storage, the precomputed square -> region
  membership (region_cell), groups, and resolving "a code or a slug" to
  the set of region codes a request should filter on.
- the importer and its background loop.

Membership is NEVER computed per request. A request only ever does an
indexed lookup in region_cell. The table is filled by map_new_cells()
(a periodic job, below) for new squares, and by remap_regions() after a
boundary import for the regions that changed.

Coordinates: MeshMapper stores [lat, lon] (LAT FIRST). Everything inside
this module keeps that order, rings are lists of (lat, lon). The only
place the order flips is geojson_for_region(), because GeoJSON is lon,lat.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit
from typing import Iterable, NamedTuple

import httpx

from .config import settings
from .db import connect, get_cursor, set_cursor
from .grid import cell_center

log = logging.getLogger("regions")

MESHMAPPER_HOME = "https://meshmapper.net/"
USER_AGENT = "MeshWars region sync (+https://meshwars.com)"

# A MeshMapper region code is a short alphanumeric. It is spliced into a
# hostname (<code>.meshmapper.net) from data we fetched, so anything else
# is refused rather than turned into a request.
_CODE_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")
# What a request may pass as a code-or-slug. Anything else cannot match a
# stored region or group, so it is "unknown" without touching the DB.
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

_CIRCLE_POINTS = 72


def region_page_url(code: str) -> str:
    return f"https://{code.lower()}.meshmapper.net/"


# ---- geometry -----------------------------------------------------------


def circle_ring(lat: float, lon: float, radius_km: float, points: int = _CIRCLE_POINTS) -> list[tuple[float, float]]:
    """A ring approximating a circle of radius_km around (lat, lon), as
    (lat, lon) pairs. Used when MeshMapper gives a region a radius but no
    polygon."""
    dlat_per_km = 1.0 / 111.32
    coslat = max(math.cos(math.radians(lat)), 0.01)
    dlon_per_km = 1.0 / (111.32 * coslat)
    ring = []
    for i in range(points):
        a = 2.0 * math.pi * i / points
        ring.append((lat + radius_km * dlat_per_km * math.sin(a), lon + radius_km * dlon_per_km * math.cos(a)))
    return ring


def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _clean_ring(raw) -> list[tuple[float, float]] | None:
    if not isinstance(raw, (list, tuple)):
        return None
    ring = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            continue
        lat, lon = _num(p[0]), _num(p[1])
        if lat is None or lon is None or abs(lat) > 90.0 or abs(lon) > 180.0:
            continue
        ring.append((lat, lon))
    return ring if len(ring) >= 3 else None


def normalize_polygon(value) -> list[list[tuple[float, float]]] | None:
    """MeshMapper's `polygon` as a list of rings of (lat, lon), or None
    when it is missing or unusable.

    The value is a JSON STRING of [[lat,lon],...] (one ring), but a list
    of rings ([[[lat,lon],...],...]) is accepted too, and so is an
    already-decoded list. Told apart by nesting depth: a ring's first
    element is a pair of numbers, a list of rings' first element is a list
    of pairs.
    """
    if isinstance(value, (str, bytes)):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
    if not isinstance(value, (list, tuple)) or not value:
        return None
    first = value[0]
    if not isinstance(first, (list, tuple)) or not first:
        return None
    if isinstance(first[0], (list, tuple)):
        candidates = list(value)  # list of rings
    else:
        candidates = [value]  # a single ring
    rings = [r for r in (_clean_ring(c) for c in candidates) if r is not None]
    return rings or None


def bbox_of(rings: list[list[tuple[float, float]]]) -> tuple[float, float, float, float]:
    """(south, west, north, east)."""
    lats = [p[0] for r in rings for p in r]
    lons = [p[1] for r in rings for p in r]
    return (min(lats), min(lons), max(lats), max(lons))


class Shape:
    """A region boundary ready for fast point-in-polygon tests.

    Even-odd rule across all rings, so a list of rings reads as separate
    parts (or an outer ring with holes) without having to say which.

    Edges are bucketed into horizontal bands once, so contains() walks only
    the edges whose latitude span reaches the point's band instead of every
    edge of the boundary.
    """

    def __init__(self, rings: list[list[tuple[float, float]]]):
        self.rings = rings
        self.south, self.west, self.north, self.east = bbox_of(rings)
        n = sum(len(r) for r in rings)
        self._nb = max(1, min(2048, n // 4))
        span = self.north - self.south
        self._h = span / self._nb if span > 0 else 1.0
        self._bands: list[list[tuple[float, float, float, float]]] = [[] for _ in range(self._nb)]
        for ring in rings:
            m = len(ring)
            for i in range(m):
                la1, lo1 = ring[i]
                la2, lo2 = ring[(i + 1) % m]
                if la1 == la2:
                    continue  # a horizontal edge never crosses the ray
                lo_b = self._band(min(la1, la2))
                hi_b = self._band(max(la1, la2))
                edge = (la1, lo1, la2, lo2)
                for k in range(lo_b, hi_b + 1):
                    self._bands[k].append(edge)

    def _band(self, lat: float) -> int:
        return min(self._nb - 1, max(0, int((lat - self.south) / self._h)))

    def contains(self, lat: float, lon: float) -> bool:
        if lat < self.south or lat > self.north or lon < self.west or lon > self.east:
            return False
        inside = False
        for la1, lo1, la2, lo2 in self._bands[self._band(lat)]:
            if (la1 > lat) != (la2 > lat):
                x = lo1 + (lat - la1) * (lo2 - lo1) / (la2 - la1)
                if x > lon:
                    inside = not inside
        return inside


def point_in_rings(lat: float, lon: float, rings: list[list[tuple[float, float]]]) -> bool:
    """One-off test; build a Shape when testing many points."""
    return Shape(rings).contains(lat, lon)


class RegionIndex:
    """Which regions contain a point. A coarse one-degree bucket prefilter
    picks the few candidate shapes; each runs its own bbox and banded
    test."""

    def __init__(self, shapes: dict[str, Shape]):
        self.shapes = shapes
        self._buckets: dict[tuple[int, int], list[tuple[str, Shape]]] = {}
        for code, sh in shapes.items():
            for la in range(math.floor(sh.south), math.floor(sh.north) + 1):
                for lo in range(math.floor(sh.west), math.floor(sh.east) + 1):
                    self._buckets.setdefault((la, lo), []).append((code, sh))

    def codes_for(self, lat: float, lon: float) -> list[str]:
        out = []
        for code, sh in self._buckets.get((math.floor(lat), math.floor(lon)), ()):
            if sh.contains(lat, lon):
                out.append(code)
        return out

    def codes_for_cell(self, cell_id: str) -> list[str]:
        lat, lon = cell_center(cell_id)
        return self.codes_for(lat, lon)


# ---- parsing MeshMapper pages ------------------------------------------


def _json_after(html: str, var_name: str):
    """The JSON value assigned to `var <var_name> = ...;` in a page, or
    None. raw_decode stops at the end of the value, so the trailing `;`
    and the rest of the script are ignored."""
    m = re.search(r"var\s+" + re.escape(var_name) + r"\s*=\s*", html)
    if not m:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(html, m.end())
    except ValueError:
        return None
    return value


def parse_zone_list(html: str) -> list[dict]:
    """The homepage's region list: [{code, name, lat, lon}, ...]. Entries
    without a safe code, a name or a numeric centre are dropped."""
    zones = _json_after(html, "zones")
    if not isinstance(zones, list):
        return []
    out = []
    for z in zones:
        if not isinstance(z, dict):
            continue
        code = z.get("code")
        name = z.get("name")
        lat, lon = _num(z.get("lat")), _num(z.get("lon"))
        if not isinstance(code, str) or not _CODE_RE.match(code):
            continue
        if not isinstance(name, str) or not name.strip() or lat is None or lon is None:
            continue
        out.append({"code": code.upper(), "name": name.strip(), "lat": lat, "lon": lon})
    return out


def parse_region_page(html: str, code: str, fallback_lat: float | None = None,
                      fallback_lon: float | None = None) -> dict | None:
    """The boundary of one region from its page: {rings, radius_km,
    center_lat, center_lon, shape} where shape is 'polygon' or 'circle'.

    The page's `allZones` array holds an entry for this code with `polygon`
    (a JSON string, lat first) and `radius_km`. A missing or unusable
    polygon falls back to a circle of radius_km around the centre. None
    when neither exists (no entry, or no polygon and no usable radius).
    """
    zones = _json_after(html, "allZones")
    if not isinstance(zones, list):
        return None
    entry = None
    for z in zones:
        if isinstance(z, dict) and str(z.get("code", "")).upper() == code.upper():
            entry = z
            break
    if entry is None:
        return None
    lat = _num(entry.get("lat"))
    lon = _num(entry.get("lon"))
    if lat is None or lon is None:
        lat, lon = fallback_lat, fallback_lon
    radius = _num(entry.get("radius_km"))
    rings = normalize_polygon(entry.get("polygon"))
    shape = "polygon"
    if rings is None:
        if lat is None or lon is None or radius is None or radius <= 0:
            return None
        rings = [circle_ring(lat, lon, radius)]
        shape = "circle"
    if lat is None or lon is None:
        s, w, n, e = bbox_of(rings)
        lat, lon = (s + n) / 2.0, (w + e) / 2.0
    name = entry.get("name")
    return {"rings": rings, "radius_km": radius, "center_lat": lat, "center_lon": lon, "shape": shape,
            "name": name.strip() if isinstance(name, str) and name.strip() else None}


# ---- storage ------------------------------------------------------------


def store_region(conn, code: str, name: str, parsed: dict, now: int | None = None) -> None:
    """Insert or replace one region's row. Caller owns the transaction."""
    now = int(time.time()) if now is None else now
    rings = parsed["rings"]
    s, w, n, e = bbox_of(rings)
    conn.execute(
        "INSERT INTO region(code, name, center_lat, center_lon, polygon_json, radius_km, "
        " south, west, north, east, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(code) DO UPDATE SET name=excluded.name, center_lat=excluded.center_lat, "
        " center_lon=excluded.center_lon, polygon_json=excluded.polygon_json, "
        " radius_km=excluded.radius_km, south=excluded.south, west=excluded.west, "
        " north=excluded.north, east=excluded.east, fetched_at=excluded.fetched_at",
        (code.upper(), name, parsed["center_lat"], parsed["center_lon"],
         json.dumps([[list(p) for p in r] for r in rings], separators=(",", ":")),
         parsed.get("radius_km"), s, w, n, e, now),
    )


# One process-wide lock serialising the membership jobs against each other
# (map_new_cells, remap_regions). Across processes (the web role's admin
# button vs the worker's loop) the write transaction's own staleness check
# (below) keeps them correct.
#
# WRITE TRANSACTIONS ARE SHORT BY DESIGN. Both jobs do all their reading and
# all their Python work (building the polygon index, testing thousands of
# squares) with NO transaction open -- WAL readers never block the ingest
# writers -- and then take BEGIN IMMEDIATE only to apply a precomputed
# batch of INSERT/DELETE rows. If a region changed while the work was done
# (its fetched_at moved), the transaction notices, rolls back and the job
# redoes that batch from fresh data. Nothing here ever holds the write lock
# across a network fetch either: every write below is its own transaction,
# opened and closed inside one helper, and run_import() calls them between
# fetches.
_MAP_LOCK = threading.Lock()
_INDEX_CACHE: tuple[tuple, RegionIndex] | None = None
_BATCH = 5000

# app/db.py gives every connection PRAGMA busy_timeout=15000, and no other
# background writer in the app retries beyond that. A import that runs for
# twenty minutes should survive one unlucky stretch of write-lock
# contention though (the preview import lost a region to "database is
# locked" next to mc_ingest's own claim failing), so each write below is
# retried a few times, with a growing pause, when SQLite reports the
# database busy or locked. Nothing else is retried.
_BUSY_ATTEMPTS = 6


def _retry_busy(fn):
    for attempt in range(1, _BUSY_ATTEMPTS + 1):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if ("locked" not in msg and "busy" not in msg) or attempt == _BUSY_ATTEMPTS:
                raise
            log.warning("regions: database busy (%s), retry %d/%d", exc, attempt, _BUSY_ATTEMPTS - 1)
            time.sleep(min(5.0, 0.5 * attempt))


def _region_signature(conn) -> tuple:
    r = conn.execute("SELECT COUNT(*), COALESCE(MAX(fetched_at),0), COALESCE(SUM(fetched_at),0) FROM region").fetchone()
    return (r[0], r[1], r[2])


def _codes_signature(conn, codes: list[str]) -> tuple:
    return tuple(tuple(r) for r in conn.execute(
        "SELECT code, fetched_at FROM region WHERE code IN (%s) ORDER BY code" % ",".join("?" * len(codes)), codes))


def _build_index(conn, codes: Iterable[str] | None = None) -> RegionIndex:
    sql = "SELECT code, polygon_json FROM region"
    params: tuple = ()
    if codes is not None:
        codes = list(codes)
        sql += " WHERE code IN (%s)" % ",".join("?" * len(codes))
        params = tuple(codes)
    shapes = {}
    for r in conn.execute(sql, params):
        rings = normalize_polygon(r["polygon_json"])
        if rings:
            shapes[r["code"]] = Shape(rings)
    return RegionIndex(shapes)


def _full_index(conn) -> tuple[tuple, RegionIndex]:
    """(signature, index over every region). Rebuilt only when the region
    set changed -- parsing ~550 polygons is seconds of work, which is why
    it must never run inside a write transaction."""
    global _INDEX_CACHE
    sig = _region_signature(conn)
    cached = _INDEX_CACHE
    if cached is not None and cached[0] == sig:
        return sig, cached[1]
    idx = _build_index(conn)
    _INDEX_CACHE = (sig, idx)
    return sig, idx


def _unmapped_cells(conn, limit: int) -> list[str]:
    cells: set[str] = set()
    for r in conn.execute(
        "SELECT t.cell_id FROM mc_tile t "
        " WHERE NOT EXISTS (SELECT 1 FROM region_cell_mapped m WHERE m.cell_id = t.cell_id) "
        " GROUP BY t.cell_id LIMIT ?", (limit,)
    ):
        cells.add(r[0])
    if len(cells) < limit:
        # Cells of places somebody has been credited for: Explorer points
        # are scoped by region through these.
        for r in conn.execute(
            "SELECT pc.cell_id FROM place_activation a JOIN place_cell pc ON pc.place_id = a.place_id "
            " WHERE NOT EXISTS (SELECT 1 FROM region_cell_mapped m WHERE m.cell_id = pc.cell_id) "
            " GROUP BY pc.cell_id LIMIT ?", (limit - len(cells),)
        ):
            cells.add(r[0])
    return list(cells)


def _membership(index: RegionIndex, cells: Iterable[str]) -> list[tuple[str, list[str]]]:
    """[(cell_id, [region codes])] -- pure computation, no database."""
    out = []
    for cid in cells:
        try:
            codes = index.codes_for_cell(cid)
        except ValueError:
            codes = []  # not a grid id: in no region, but still marked tested
        out.append((cid, codes))
    return out


def _apply_membership(conn, rows: list[tuple[str, list[str]]], *, mark_mapped: bool) -> None:
    if mark_mapped:
        conn.executemany("INSERT OR IGNORE INTO region_cell_mapped(cell_id) VALUES (?)", [(c,) for c, _ in rows])
    conn.executemany(
        "INSERT OR IGNORE INTO region_cell(region_code, cell_id) VALUES (?,?)",
        [(code, cid) for cid, codes in rows for code in codes])


def _map_one_batch(batch: int) -> int | None:
    """Map one batch of untested cells. None: nothing to do (no regions or
    no untested cells). -1: a region changed meanwhile, nothing written, call
    again. Otherwise the number of cells mapped."""
    conn = connect()
    try:
        if conn.execute("SELECT 1 FROM region LIMIT 1").fetchone() is None:
            return None
        sig, index = _full_index(conn)
        cells = _unmapped_cells(conn, batch)
    finally:
        conn.close()
    if not cells:
        return None
    rows = _membership(index, cells)

    def write() -> int:
        wc = connect()
        try:
            wc.execute("BEGIN IMMEDIATE")
            try:
                if _region_signature(wc) != sig:
                    wc.execute("ROLLBACK")
                    return -1
                _apply_membership(wc, rows, mark_mapped=True)
                wc.execute("COMMIT")
            except BaseException:
                try:
                    wc.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        finally:
            wc.close()
        return len(rows)

    return _retry_busy(write)


def map_new_cells(batch: int = _BATCH) -> int:
    """Map every square (and credited place cell) not yet tested against
    the region set. Returns how many cells were mapped. A no-op when no
    region is stored. Batches of `batch` cells; the polygon work for each
    batch happens before its write transaction opens, which then only
    inserts the computed rows.

    A periodic job rather than an ingest hook: the squares are written by
    several writers (MeshCore ingest, the Meshtastic ingestors, tile
    release) and touching each is far riskier than a cheap sweep that
    only reads mc_tile. The cost of that choice is that a brand-new
    square is missing from a regional view for up to one loop interval.
    """
    total = 0
    with _MAP_LOCK:
        while True:
            n = _map_one_batch(batch)
            if n is None:
                return total
            if n < 0:
                continue
            total += n
            if n < batch:
                return total


def remap_regions(codes: Iterable[str]) -> int:
    """Recompute membership of the given regions over every cell already
    mapped, replacing their old rows in ONE short transaction (so a reader
    sees the old membership or the new one, never a half-built region).
    The testing of every cell happens first, with no transaction open; the
    transaction deletes the regions' old rows, inserts the new ones, and
    also tests any cell that was mapped in the meantime. Returns the number
    of membership rows written."""
    codes = sorted({c.upper() for c in codes})
    if not codes:
        return 0
    marks = ",".join("?" * len(codes))
    with _MAP_LOCK:
        for _ in range(5):
            conn = connect()
            try:
                sig = _codes_signature(conn, codes)
                index = _build_index(conn, codes)
                cells = [r[0] for r in conn.execute("SELECT cell_id FROM region_cell_mapped")]
            finally:
                conn.close()
            computed = _membership(index, cells)
            seen = set(cells)

            def write() -> int | None:
                wc = connect()
                try:
                    wc.execute("BEGIN IMMEDIATE")
                    try:
                        if _codes_signature(wc, codes) != sig:
                            wc.execute("ROLLBACK")
                            return None
                        late = [r[0] for r in wc.execute("SELECT cell_id FROM region_cell_mapped")
                                if r[0] not in seen]
                        rows = computed + (_membership(index, late) if late else [])
                        wc.execute(f"DELETE FROM region_cell WHERE region_code IN ({marks})", codes)
                        _apply_membership(wc, rows, mark_mapped=False)
                        wc.execute("COMMIT")
                    except BaseException:
                        try:
                            wc.execute("ROLLBACK")
                        except Exception:
                            pass
                        raise
                finally:
                    wc.close()
                return sum(len(c) for _, c in rows)

            n = _retry_busy(write)
            if n is not None:
                return n
        log.warning("regions: remap gave up, the boundaries kept changing")
        return 0


# ---- groups -------------------------------------------------------------


class RegionError(Exception):
    """A validation failure on a region/group write; `status` is the HTTP
    status the API layer should answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:63]


def _member_rows(conn, group_id: int) -> tuple[list[str], list[int]]:
    regions, groups = [], []
    for r in conn.execute(
        "SELECT region_code, child_group_id FROM region_group_member WHERE group_id = ?", (group_id,)
    ):
        if r["region_code"] is not None:
            regions.append(r["region_code"])
        else:
            groups.append(r["child_group_id"])
    return sorted(regions), sorted(groups)


def _would_cycle(conn, group_id: int, child_ids: Iterable[int]) -> bool:
    adj: dict[int, list[int]] = {}
    for r in conn.execute(
        "SELECT group_id, child_group_id FROM region_group_member WHERE child_group_id IS NOT NULL"
    ):
        adj.setdefault(r["group_id"], []).append(r["child_group_id"])
    stack = list(child_ids)
    seen: set[int] = set()
    while stack:
        g = stack.pop()
        if g == group_id:
            return True
        if g in seen:
            continue
        seen.add(g)
        stack.extend(adj.get(g, ()))
    return False


def _clean_members(conn, group_id: int | None, regions, groups) -> tuple[list[str], list[int]]:
    if regions is None:
        regions = []
    if groups is None:
        groups = []
    if not isinstance(regions, list) or not isinstance(groups, list):
        raise RegionError("members must be lists")
    codes = []
    for c in regions:
        if not isinstance(c, str) or not _CODE_RE.match(c.strip()):
            raise RegionError("bad region code")
        codes.append(c.strip().upper())
    codes = sorted(set(codes))
    if codes:
        found = {r[0] for r in conn.execute(
            "SELECT code FROM region WHERE code IN (%s)" % ",".join("?" * len(codes)), codes)}
        missing = [c for c in codes if c not in found]
        if missing:
            raise RegionError("unknown region: " + ", ".join(missing))
    ids = []
    for g in groups:
        if not isinstance(g, int) or isinstance(g, bool):
            raise RegionError("bad group id")
        ids.append(g)
    ids = sorted(set(ids))
    if ids:
        found_ids = {r[0] for r in conn.execute(
            "SELECT id FROM region_group WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)}
        missing_ids = [g for g in ids if g not in found_ids]
        if missing_ids:
            raise RegionError("unknown group: " + ", ".join(str(g) for g in missing_ids))
    if group_id is not None and _would_cycle(conn, group_id, ids):
        raise RegionError("a group cannot contain itself, directly or through other groups", 409)
    return codes, ids


def _check_name_slug(conn, group_id: int | None, name, slug) -> tuple[str, str]:
    if not isinstance(name, str) or not name.strip():
        raise RegionError("name is required")
    name = name.strip()
    if len(name) > 80:
        raise RegionError("name is too long")
    if slug is None or (isinstance(slug, str) and not slug.strip()):
        slug = slugify(name)
    if not isinstance(slug, str):
        raise RegionError("bad slug")
    slug = slug.strip().lower()
    if not SLUG_RE.match(slug):
        raise RegionError("slug may only use lowercase letters, digits and dashes")
    if conn.execute("SELECT 1 FROM region WHERE lower(code) = ?", (slug,)).fetchone():
        raise RegionError("slug matches a region code", 409)
    other = conn.execute("SELECT id FROM region_group WHERE slug = ?", (slug,)).fetchone()
    if other is not None and other["id"] != group_id:
        raise RegionError("slug is already used by another group", 409)
    return name, slug


def create_group(conn, name, slug, regions, groups, now: int | None = None) -> int:
    """Caller owns the (BEGIN IMMEDIATE) transaction."""
    now = int(time.time()) if now is None else now
    name, slug = _check_name_slug(conn, None, name, slug)
    codes, ids = _clean_members(conn, None, regions, groups)
    cur = conn.execute(
        "INSERT INTO region_group(name, slug, created_at, updated_at) VALUES (?,?,?,?)",
        (name, slug, now, now))
    gid = cur.lastrowid
    _set_members(conn, gid, codes, ids)
    return gid


def update_group(conn, group_id: int, name, slug, regions, groups, now: int | None = None) -> None:
    now = int(time.time()) if now is None else now
    if conn.execute("SELECT 1 FROM region_group WHERE id = ?", (group_id,)).fetchone() is None:
        raise RegionError("group not found", 404)
    name, slug = _check_name_slug(conn, group_id, name, slug)
    codes, ids = _clean_members(conn, group_id, regions, groups)
    conn.execute("UPDATE region_group SET name = ?, slug = ?, updated_at = ? WHERE id = ?",
                 (name, slug, now, group_id))
    _set_members(conn, group_id, codes, ids)


def _set_members(conn, group_id: int, codes: list[str], ids: list[int]) -> None:
    conn.execute("DELETE FROM region_group_member WHERE group_id = ?", (group_id,))
    for c in codes:
        conn.execute("INSERT INTO region_group_member(group_id, region_code) VALUES (?,?)", (group_id, c))
    for g in ids:
        conn.execute("INSERT INTO region_group_member(group_id, child_group_id) VALUES (?,?)", (group_id, g))


def delete_group(conn, group_id: int) -> bool:
    cur = conn.execute("DELETE FROM region_group WHERE id = ?", (group_id,))
    return cur.rowcount > 0


def list_groups(conn) -> list[dict]:
    out = []
    for g in conn.execute("SELECT id, name, slug FROM region_group ORDER BY name COLLATE NOCASE, id"):
        regions, groups = _member_rows(conn, g["id"])
        out.append({"id": g["id"], "name": g["name"], "slug": g["slug"], "regions": regions, "groups": groups})
    return out


def list_regions(conn) -> list[dict]:
    return [
        {"code": r["code"], "name": r["name"], "lat": r["center_lat"], "lon": r["center_lon"],
         "bbox": [r["south"], r["west"], r["north"], r["east"]]}
        for r in conn.execute(
            "SELECT code, name, center_lat, center_lon, south, west, north, east FROM region ORDER BY code")
    ]


# ---- resolving a request's region parameter ----------------------------


class Resolved(NamedTuple):
    key: str                 # canonical form: the region code, or the group's slug
    kind: str                # 'region' | 'group'
    name: str
    codes: tuple[str, ...]   # every region code the request filters on


def resolve(conn, value: str | None) -> Resolved | None:
    """A region code (case-insensitive) or a group slug to the region
    codes it stands for; a group expands through its child groups to any
    depth. None when it names nothing. A region code wins over a slug
    (group slugs are refused if they equal a code, so this only matters
    if a region code appears later that an old slug already used)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not _KEY_RE.match(value):
        return None
    row = conn.execute("SELECT code, name FROM region WHERE code = ?", (value.upper(),)).fetchone()
    if row is not None:
        return Resolved(row["code"], "region", row["name"], (row["code"],))
    grp = conn.execute("SELECT id, name, slug FROM region_group WHERE slug = ?", (value.lower(),)).fetchone()
    if grp is None:
        return None
    codes: set[str] = set()
    seen: set[int] = set()
    stack = [grp["id"]]
    while stack:  # iterative, and tolerant of a cycle that bypassed the write check
        gid = stack.pop()
        if gid in seen:
            continue
        seen.add(gid)
        regions, children = _member_rows(conn, gid)
        codes.update(regions)
        stack.extend(children)
    return Resolved(grp["slug"], "group", grp["name"], tuple(sorted(codes)))


def cell_filter_sql(codes: Iterable[str], column: str = "cell_id") -> tuple[str, list]:
    """An SQL condition: `<column>` is a cell in any of the given
    regions. A cell in two of the regions still counts once (it is an IN
    test, not a join). `column` is a trusted identifier from the caller."""
    codes = list(codes)
    if not codes:
        return "0", []
    return (
        f"{column} IN (SELECT cell_id FROM region_cell WHERE region_code IN ({','.join('?' * len(codes))}))",
        codes,
    )


def region_cells(conn, codes: Iterable[str]) -> set[str]:
    codes = list(codes)
    if not codes:
        return set()
    return {r[0] for r in conn.execute(
        f"SELECT cell_id FROM region_cell WHERE region_code IN ({','.join('?' * len(codes))})", codes)}


def host_region_key(host: str | None) -> str | None:
    """`<key>.<suffix>` -> key, when regions_host_suffix is configured
    and the host has exactly one label in front of it."""
    suffix = (settings.regions_host_suffix or "").strip().strip(".").lower()
    if not suffix or not host:
        return None
    host = host.split(":", 1)[0].strip().lower().rstrip(".")
    tail = "." + suffix
    if not host.endswith(tail):
        return None
    label = host[: -len(tail)]
    if not label or "." in label:
        return None
    return label


# ---- GeoJSON ------------------------------------------------------------


def _geo_ring(ring) -> list[list[float]]:
    pts = [[p[1], p[0]] for p in ring]  # MeshMapper is lat,lon; GeoJSON is lon,lat
    if pts and pts[0] != pts[-1]:
        pts.append(list(pts[0]))
    return pts


def geojson_for_regions(conn, codes: Iterable[str]) -> dict:
    codes = list(codes)
    feats = []
    if codes:
        rows = conn.execute(
            "SELECT code, name, polygon_json FROM region WHERE code IN (%s) ORDER BY code"
            % ",".join("?" * len(codes)), codes).fetchall()
        for r in rows:
            rings = normalize_polygon(r["polygon_json"])
            if not rings:
                continue
            if len(rings) == 1:
                geom = {"type": "Polygon", "coordinates": [_geo_ring(rings[0])]}
            else:
                geom = {"type": "MultiPolygon", "coordinates": [[_geo_ring(x)] for x in rings]}
            feats.append({"type": "Feature", "properties": {"code": r["code"], "name": r["name"]},
                          "geometry": geom})
    return {"type": "FeatureCollection", "features": feats}


# ---- importer -----------------------------------------------------------

_STATE_KEY = "regions_sync_state"
_LAST_NEW_KEY = "regions_last_new_at"
_LAST_FULL_KEY = "regions_last_full_at"
_RUNNING_STALE_S = 600
_DAY = 86400
_WEEK = 7 * _DAY
_RETRY_AFTER_LIST_FAILURE = 3600

_task: asyncio.Task | None = None
_next_try_at = 0.0


def _read_state() -> dict:
    conn = connect()
    try:
        raw = get_cursor(conn, _STATE_KEY, "")
    finally:
        conn.close()
    try:
        state = json.loads(raw) if raw else {}
    except ValueError:
        state = {}
    return state if isinstance(state, dict) else {}


def _write_state(state: dict) -> None:
    state = dict(state, updated_at=int(time.time()))

    def write() -> None:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                set_cursor(conn, _STATE_KEY, json.dumps(state))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    _retry_busy(write)


def sync_status() -> dict:
    """What the admin panel shows. `running` is true only while a run has
    reported progress recently, so a process killed mid-import does not
    leave the button stuck."""
    st = _read_state()
    running = st.get("state") == "running" and (time.time() - st.get("updated_at", 0)) < _RUNNING_STALE_S
    out = {
        "running": running,
        "mode": st.get("mode"),
        "started_at": st.get("started_at"),
        "finished_at": st.get("finished_at"),
        "total": st.get("total", 0),
        "done": st.get("done", 0),
        "failures": st.get("failures", 0),
        "last_error": st.get("last_error"),
    }
    return out


def _store_one(code: str, name: str, parsed: dict) -> None:
    def write() -> None:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                store_region(conn, code, name, parsed)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    _retry_busy(write)


def _stored_codes() -> dict[str, int]:
    conn = connect()
    try:
        return {r["code"]: r["fetched_at"] for r in conn.execute("SELECT code, fetched_at FROM region")}
    finally:
        conn.close()


def _stamp(keys: list[str]) -> None:
    def write() -> None:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                now = str(int(time.time()))
                for k in keys:
                    set_cursor(conn, k, now)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    _retry_busy(write)


async def _get(client: httpx.AsyncClient, url: str) -> str:
    r = await client.get(url)
    r.raise_for_status()
    return r.text


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(settings.regions_fetch_timeout_seconds, connect=10.0),
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        follow_redirects=True,
    )


async def run_import(mode: str = "all", client: httpx.AsyncClient | None = None) -> dict:
    """Fetch MeshMapper's region list, then region pages one at a time
    (mode 'all': every listed region; 'new': only codes not stored yet),
    store each boundary, and recompute square membership for the regions
    that changed. A failed page is logged and counted, and the region
    keeps its previous boundary; the run carries on. Progress is written
    to the cursor table after every region.

    Never blocks the event loop: every DB step runs in a thread and the
    pause between requests is an asyncio sleep.
    """
    global _next_try_at
    own_client = client is None
    if own_client:
        client = make_client()
    delay = max(0.0, settings.regions_fetch_delay_seconds)
    state = {"state": "running", "mode": mode, "started_at": int(time.time()), "finished_at": None,
             "total": 0, "done": 0, "failures": 0, "last_error": None}
    await asyncio.to_thread(_write_state, state)
    affected: list[str] = []
    list_ok = False
    try:
        try:
            zones = parse_zone_list(await _get(client, MESHMAPPER_HOME))
            if not zones:
                raise ValueError("region list was empty or unreadable")
            list_ok = True
        except Exception as exc:
            log.warning("regions: could not read the MeshMapper region list: %s", exc)
            state["failures"] += 1
            state["last_error"] = f"region list: {type(exc).__name__}"
            _next_try_at = time.time() + _RETRY_AFTER_LIST_FAILURE
            return state
        stored = await asyncio.to_thread(_stored_codes)
        targets = zones if mode == "all" else [z for z in zones if z["code"] not in stored]
        state["total"] = len(targets)
        await asyncio.to_thread(_write_state, state)
        for i, z in enumerate(targets):
            if i:
                await asyncio.sleep(delay)
            code = z["code"]
            try:
                parsed = parse_region_page(await _get(client, region_page_url(code)), code, z["lat"], z["lon"])
                if parsed is None:
                    raise ValueError("no boundary on the page")
                await asyncio.to_thread(_store_one, code, z["name"], parsed)
                affected.append(code)
            except Exception as exc:
                state["failures"] += 1
                state["last_error"] = f"{code}: {type(exc).__name__}"
                log.warning("regions: %s skipped: %s", code, exc)
            state["done"] = i + 1
            await asyncio.to_thread(_write_state, state)
        if affected:
            await asyncio.to_thread(_retry_busy, lambda: remap_regions(affected))
        return state
    finally:
        state["state"] = "idle"
        state["finished_at"] = int(time.time())
        try:
            await asyncio.shield(asyncio.to_thread(_write_state, state))
            if list_ok:
                await asyncio.shield(asyncio.to_thread(
                    _stamp, [_LAST_NEW_KEY, _LAST_FULL_KEY] if mode == "all" else [_LAST_NEW_KEY]))
        except Exception:
            log.exception("regions: could not record the end of the import")
        if own_client:
            await client.aclose()


_HOST_RE = re.compile(r"^([a-z0-9]{1,10})\.meshmapper\.net$")


def normalize_region_link(text) -> str | None:
    """A pasted MeshMapper region link or code -> the uppercase code, or
    None when it is neither. Accepts `twf.meshmapper.net`, any http(s) URL
    of that host (path, query and case ignored), and a bare code (`twf`).
    The host must be exactly <code>.meshmapper.net: nothing else is ever
    turned into a request, so a link to another host (or one that merely
    starts with a MeshMapper name) is refused, not fetched."""
    if not isinstance(text, str):
        return None
    t = text.strip()
    if not t or len(t) > 300:
        return None
    if _CODE_RE.match(t):
        return t.upper()
    if "://" not in t:
        t = "https://" + t
    try:
        parts = urlsplit(t)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or parts.username or parts.password or port not in (None, 80, 443):
        return None
    m = _HOST_RE.match(host or "")
    return m.group(1).upper() if m else None


def _page_failure(code: str, html: str) -> RegionError:
    zones = _json_after(html, "allZones")
    has_entry = isinstance(zones, list) and any(
        isinstance(z, dict) and str(z.get("code", "")).upper() == code for z in zones)
    if not has_entry:
        return RegionError("No MeshMapper region found for that link", 404)
    return RegionError("That region has no boundary or radius to fetch", 422)


async def fetch_region(link: str, client: httpx.AsyncClient | None = None) -> dict:
    """Fetch ONE region page (from a pasted link or code), store its
    boundary, recompute that region's square membership, and report what
    happened. Only https://<code>.meshmapper.net/ is ever requested, built
    from the validated code. Safe alongside a running full import: both
    are upserts of the same row and the membership jobs serialise on
    _MAP_LOCK, so the later write simply wins. Raises RegionError with a
    status the API layer passes on (400 not a link, 404 no such region,
    422 no boundary, 502/504 MeshMapper trouble)."""
    code = normalize_region_link(link)
    if code is None:
        raise RegionError("Not a MeshMapper region link", 400)
    own = client is None
    if own:
        client = make_client()
    try:
        try:
            html = await _get(client, region_page_url(code))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise RegionError("No MeshMapper region found for that link", 404)
            raise RegionError(f"MeshMapper answered {exc.response.status_code}", 502)
        except httpx.TimeoutException:
            raise RegionError("MeshMapper did not answer in time", 504)
        except httpx.HTTPError:
            raise RegionError("Could not reach MeshMapper", 502)
    finally:
        if own:
            await client.aclose()
    parsed = parse_region_page(html, code)
    if parsed is None:
        raise _page_failure(code, html)
    name = parsed.get("name") or code
    existed = code in await asyncio.to_thread(_stored_codes)
    await asyncio.to_thread(_store_one, code, name, parsed)
    await asyncio.to_thread(_retry_busy, lambda: remap_regions([code]))

    def count() -> int:
        conn = connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM region_cell WHERE region_code = ?", (code,)).fetchone()[0]
        finally:
            conn.close()

    return {"code": code, "name": name, "points": sum(len(r) for r in parsed["rings"]),
            "squares_in_region": await asyncio.to_thread(count),
            "new_or_updated": "updated" if existed else "new"}


def start_import(mode: str = "all") -> bool:
    """Start an import in the background of the running event loop.
    False when one is already running (here or, by its recent progress,
    in another process)."""
    global _task
    if (_task is not None and not _task.done()) or sync_status()["running"]:
        return False
    _task = asyncio.get_running_loop().create_task(_guarded_import(mode), name="regions-import")
    return True


async def _guarded_import(mode: str) -> None:
    try:
        await run_import(mode)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("regions: import failed")


async def run_forever() -> None:
    """Worker-role loop. Every cycle: map squares that have no membership
    yet (cheap, DB only). When regions_sync_enabled: a daily fetch of the
    list plus boundaries for codes not stored yet, and a weekly full
    re-fetch of every boundary."""
    log.info("regions loop starting (sync %s)", "on" if settings.regions_sync_enabled else "off")
    while True:
        try:
            await asyncio.to_thread(map_new_cells)
            if settings.regions_sync_enabled and time.time() >= _next_try_at \
                    and not sync_status()["running"] and (_task is None or _task.done()):
                conn = await asyncio.to_thread(connect)
                try:
                    last_new = int(get_cursor(conn, _LAST_NEW_KEY, "0") or 0)
                    last_full = int(get_cursor(conn, _LAST_FULL_KEY, "0") or 0)
                finally:
                    conn.close()
                now = time.time()
                if now - last_full >= _WEEK:
                    await run_import("all")
                elif now - last_new >= _DAY:
                    await run_import("new")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("regions loop: cycle failed")
        await asyncio.sleep(max(settings.regions_loop_seconds, 5))
