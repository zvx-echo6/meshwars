"""Weekly rotation for "Places Worth Going" (docs/features/places.md).

Summits and boundary-backed parks (place.rotates=0) are always active.
Landmarks and small "city" parks (place.rotates=1) rotate weekly: which
of them are live is a DETERMINISTIC draw, seeded from the week identifier
alone (never per-player, never wall-clock random), so every player sees
the same set and it is reproducible from week_start on its own -- see
_compute_week()'s docstring for the algorithm.

The week clock is the same one app/checkin.py already uses for net
dates: Wednesday, local to settings.checkin_net_timezone -- "one clock
for the whole game" (docs/features/places.md). Rotation flips at local
midnight on that Wednesday, which is "immediately before the net" (the
net itself starts at settings.checkin_net_start_hour that evening).

REGION CELL SIZE -- the brief asked for a chosen size and its yield,
stated plainly:

  ROTATION_CELL_MILES = 18. One allowance slot per cell
  (ROTATION_QUOTA_PER_CELL = 1), so a cell that has any rotating
  candidate at all contributes exactly one live place per week, and an
  18-mile cell filled edge-to-edge puts that place roughly 18 miles
  (about a 20-minute drive) from the next one -- inside the brief's
  15-20 mile target band. A town with a single candidate gets it
  automatically (its cell has nothing else competing for the slot); a
  dense area like Boise has many candidates inside a small number of
  18-mile cells, so it does not get proportionally more live places
  than a rural cell that also has at least one -- that IS the density
  levelling the brief asked for, not a bug in it.

  Cell degrees are computed from the configured play area's own
  latitude band (not a hardcoded reference point), the same way
  app/places.py documents its own bucket sizing depends on where degrees
  are being measured -- see _region_cell_degrees().

MIN_SPACING_MILES = 3: no two live rotating places within 3 miles of
each other, enforced globally (not just within one region cell -- two
candidates a mile apart but in adjacent cells would otherwise both
slip through).
"""
from __future__ import annotations

import hashlib
import math
import random
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import settings
from .grid import distance_m

MILE_M = 1609.344

ROTATION_CELL_MILES = 18.0
ROTATION_QUOTA_PER_CELL = 1
MIN_SPACING_MILES = 3.0

_METERS_PER_DEG_LAT = 111_320.0

# Bucket size for the 3-mile spacing check's spatial index -- generous
# on purpose (a 3x3 neighborhood at this size reaches roughly 33 km, well
# past the ~4.8 km / 3-mile threshold being tested), same reasoning
# app/places.py's own bucket-neighborhood sizing uses, just for a much
# shorter distance.
_SPACING_BUCKET_DEG = 0.1


# ---- the week clock -------------------------------------------------


def week_start_for_date(d) -> str:
    """The Wednesday date (YYYY-MM-DD) that calendar date `d`
    (datetime.date) belongs to -- the most recent date on or before `d`
    whose weekday is settings.checkin_net_weekday. Pure date arithmetic,
    no timezone involved -- `d` is assumed to already BE the correct
    local calendar date; see week_start_for_ts for the ts -> local-date
    step. Split out so a caller that already has a local date (e.g. the
    admin preview, parsing an operator-supplied "2026-08-19" string) can
    snap it without a lossy round-trip through a timestamp, where a
    naive date-only string turned into a timestamp picks up the SERVER
    process's own timezone rather than settings.checkin_net_timezone --
    exactly the kind of off-by-one-day bug this avoids.
    """
    days_since = (d.weekday() - settings.checkin_net_weekday) % 7
    return (d - timedelta(days=days_since)).isoformat()


def week_start_for_ts(ts: int) -> str:
    """The Wednesday date (YYYY-MM-DD, local to settings.checkin_net_
    timezone) that `ts` belongs to. This is the same clock
    app/checkin.py's net_date_for_ts() reads (weekday + timezone),
    reused rather than re-declared so the game never holds two opinions
    about when a week starts.
    """
    tz = ZoneInfo(settings.checkin_net_timezone)
    local_date = datetime.fromtimestamp(ts, tz=tz).date()
    return week_start_for_date(local_date)


def current_week_start() -> str:
    import time
    return week_start_for_ts(int(time.time()))


def _prev_week_start(week_start: str) -> str:
    d = datetime.fromisoformat(week_start).date()
    return (d - timedelta(days=7)).isoformat()


# ---- region cells -----------------------------------------------------


def _region_cell_degrees() -> tuple[float, float]:
    """(lat_deg, lon_deg) size of one region cell, sized to
    ROTATION_CELL_MILES using the configured play area's own mid-
    latitude for the longitude compression (cos(lat)) -- longitude
    degrees are narrower the further from the equator the play area
    sits, so this is computed from settings rather than a constant.
    """
    miles_m = ROTATION_CELL_MILES * MILE_M
    lat_deg = miles_m / _METERS_PER_DEG_LAT
    mid_lat = (settings.play_area_north + settings.play_area_south) / 2.0
    lon_scale = max(math.cos(math.radians(mid_lat)), 0.01)
    lon_deg = miles_m / (_METERS_PER_DEG_LAT * lon_scale)
    return lat_deg, lon_deg


def region_cell_key(lat: float, lon: float, lat_deg: float, lon_deg: float) -> tuple[int, int]:
    return (math.floor(lat / lat_deg), math.floor(lon / lon_deg))


# ---- 3-mile spacing index ----------------------------------------------


class _SpacingIndex:
    """Bucketed nearest-neighbour check for "is anything already chosen
    within MIN_SPACING_MILES of this point" -- same 3x3-bucket-
    neighborhood technique as app/places.py's town-distance lookup,
    sized for a few-mile radius instead of a many-mile one.
    """

    def __init__(self):
        self._buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}

    def _key(self, lat: float, lon: float) -> tuple[int, int]:
        return (math.floor(lat / _SPACING_BUCKET_DEG), math.floor(lon / _SPACING_BUCKET_DEG))

    def too_close(self, lat: float, lon: float) -> bool:
        bk = self._key(lat, lon)
        limit_m = MIN_SPACING_MILES * MILE_M
        for dlat in (-1, 0, 1):
            for dlon in (-1, 0, 1):
                for plat, plon in self._buckets.get((bk[0] + dlat, bk[1] + dlon), ()):
                    if distance_m(lat, lon, plat, plon) < limit_m:
                        return True
        return False

    def add(self, lat: float, lon: float) -> None:
        self._buckets.setdefault(self._key(lat, lon), []).append((lat, lon))


# ---- the draw -----------------------------------------------------------


def _seed_for(week_start: str) -> int:
    """Deterministic integer seed from the week identifier alone. Not
    Python's hash() (PYTHONHASHSEED-randomized per process, so the same
    string would seed differently between two servers or two restarts)
    -- sha256 is stable across processes, versions and platforms, which
    is the whole point of a "every player sees the same draw" rotation.
    """
    digest = hashlib.sha256(f"places-rotation:{week_start}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _compute_week(
    conn: sqlite3.Connection,
    week_start: str,
) -> tuple[list[int], dict]:
    """The deterministic rotation draw for `week_start`. Returns
    (chosen place_ids, region_report) where region_report maps a region
    cell key (as "latIdx_lonIdx") to {"candidates": n, "chosen": n} for
    the admin density preview.

    Algorithm: group every rotates=1 place into its region cell,
    process cells in a deterministic-but-shuffled order, and within each
    cell fill up to ROTATION_QUOTA_PER_CELL slots from a deterministically
    shuffled candidate list, skipping any candidate within
    MIN_SPACING_MILES of one already chosen (in ANY cell, not just this
    one -- two candidates a short walk apart but on opposite sides of a
    cell boundary must not both get picked). Last week's picks are
    sorted to the back of their cell's candidate list (not excluded) so
    a repeat only happens when nothing else in that cell clears the
    spacing check.

    Pure with respect to the database: reads `place` and, for the anti-
    repeat preference, last week's persisted `place_week` if present.
    Writes nothing -- resolve_week() below is what persists a result.
    """
    lat_deg, lon_deg = _region_cell_degrees()
    rng = random.Random(_seed_for(week_start))

    prev_week = _prev_week_start(week_start)
    prev_chosen = {
        r[0] for r in conn.execute(
            "SELECT place_id FROM place_week WHERE week_start = ?", (prev_week,)
        )
    }

    by_cell: dict[tuple[int, int], list[tuple[int, float, float]]] = {}
    for row in conn.execute("SELECT id, lat, lon FROM place WHERE rotates = 1"):
        key = region_cell_key(row["lat"], row["lon"], lat_deg, lon_deg)
        by_cell.setdefault(key, []).append((row["id"], row["lat"], row["lon"]))

    cell_keys = sorted(by_cell.keys())
    rng.shuffle(cell_keys)

    spacing = _SpacingIndex()
    chosen: list[int] = []
    region_report: dict[str, dict] = {}

    for key in cell_keys:
        candidates = by_cell[key][:]
        rng.shuffle(candidates)
        # Push last week's picks to the back -- a repeat is a fallback,
        # not a preference, and only happens if every fresher candidate
        # in this cell fails the spacing check.
        candidates.sort(key=lambda c: c[0] in prev_chosen)

        picked = 0
        for place_id, lat, lon in candidates:
            if picked >= ROTATION_QUOTA_PER_CELL:
                break
            if spacing.too_close(lat, lon):
                continue
            spacing.add(lat, lon)
            chosen.append(place_id)
            picked += 1

        region_report[f"{key[0]}_{key[1]}"] = {"candidates": len(candidates), "chosen": picked}

    return chosen, region_report


def resolve_week(conn: sqlite3.Connection, week_start: str) -> list[int]:
    """The live rotating place_ids for `week_start`, computed once and
    cached in place_week. Safe to call from a read path -- if the draw
    is already there, this is a single indexed SELECT; if not, it
    computes and persists it in its own write transaction so every
    later call (this week, any player, any process) sees the same
    stored result rather than a freshly re-rolled one.
    """
    existing = [
        r[0] for r in conn.execute(
            "SELECT place_id FROM place_week WHERE week_start = ?", (week_start,)
        )
    ]
    if existing:
        return existing

    chosen, _report = _compute_week(conn, week_start)

    # credit_places() (app/place_scoring.py) calls this from INSIDE an
    # already-open write transaction (the caller holds app.db's write
    # lock via WriteSession before app/mc_ingest.py or app/ingest.py
    # ever reach the scoring hook); a bare read-path caller (the places
    # API, the admin preview, a first startup) instead hands in a fresh
    # connection with no transaction open. SQLite has no nested
    # transactions, so this only opens its own BEGIN/COMMIT when there
    # is not already one in progress -- inside an outer transaction, the
    # INSERT below just rides along and commits (or rolls back) with
    # whatever the caller is already doing.
    own_transaction = not conn.in_transaction
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-check inside the transaction: two callers racing to resolve
        # the same never-before-seen week could both compute here: the
        # second INSERT would violate place_week's primary key. INSERT
        # OR IGNORE makes that harmless -- both computed the same
        # deterministic answer anyway, so whichever lands first wins and
        # the second is a silent no-op, not an error.
        conn.executemany(
            "INSERT OR IGNORE INTO place_week(week_start, place_id) VALUES (?, ?)",
            [(week_start, pid) for pid in chosen],
        )
        if own_transaction:
            conn.execute("COMMIT")
    except Exception:
        if own_transaction:
            conn.execute("ROLLBACK")
        raise
    return [
        r[0] for r in conn.execute(
            "SELECT place_id FROM place_week WHERE week_start = ?", (week_start,)
        )
    ]


def preview_week(conn: sqlite3.Connection, week_start: str) -> tuple[list[int], dict]:
    """Preview the draw for any week (past, current, or future) WITHOUT
    persisting it -- for the admin operator preview
    (app/admin_ops.py's /api/admin/places/preview). Returns (chosen
    place_ids, region_report), same shape _compute_week returns.

    Deliberately re-runs _compute_week rather than reading place_week:
    an already-resolved week's stored draw must never be silently
    recomputed and shown as if it might differ (it cannot -- the
    algorithm is deterministic -- but reading the stored table for a
    resolved week keeps this function's output honest about what is
    ACTUALLY live, vs. a hypothetical week that has no stored draw yet).
    """
    if week_start != current_week_start():
        # Only the current week's persisted result matters for what
        # actually scores; anything else previewed is by definition
        # hypothetical, so recompute fresh rather than reading a table
        # a past/future week may or may not have a row in yet.
        return _compute_week(conn, week_start)
    existing = [
        r[0] for r in conn.execute(
            "SELECT place_id FROM place_week WHERE week_start = ?", (week_start,)
        )
    ]
    if existing:
        _chosen, report = _compute_week(conn, week_start)
        return existing, report
    return _compute_week(conn, week_start)


def live_place_ids(conn: sqlite3.Connection, week_start: str) -> set[int]:
    """Every place that scores right now: always-active (rotates=0)
    plus this week's resolved rotating set. Resolves (and persists) the
    week's draw if it has not been already -- the first read of a new
    week is what commits it, same as ensure_active_season commits a new
    season on first read past its boundary.
    """
    always = {r[0] for r in conn.execute("SELECT id FROM place WHERE rotates = 0")}
    rotating = set(resolve_week(conn, week_start))
    return always | rotating
