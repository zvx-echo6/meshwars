#!/usr/bin/env python3
"""Builds app/reference/places_worth_going.csv -- the seed for the
"Places Worth Going" feature (docs/features/places.md). Summits, parks,
and landmarks that make a captured grid square worth more than an
ordinary one.

This is a PIPELINE, not a single pass, because its sources live in
several different places and some of them need tools this repo's own
environment does not have (osmium + GDAL). Run each stage where its
inputs live, then `merge` wherever it is convenient:

  1. fetch-sota       -- anywhere with internet. Pulls and bbox-filters
                          the SOTA summits list.
  2. fetch-pota       -- anywhere with internet. Pulls and bbox+active-
                          filters the POTA parks list.
  3. extract-landmarks -- on navi (zvx@100.64.0.27), which has osmium-tool
                          and read access to pi-nas's OSM extract. Needs
                          pyosmium (python3 -c "import osmium").
  4. match-parks      -- on navi, which has GDAL/OGR and the local PAD-US
                          File Geodatabase. Takes fetch-pota's output and
                          finds each park's boundary polygon.
  4b. fetch-padus-parks -- on navi, same GDAL/PAD-US dependency as
                          match-parks. ADDED 2026-08-24: pulls PAD-US's
                          own local/city/county park units directly, as
                          a second park source alongside POTA -- see
                          "PARK SOURCES" below. Also takes fetch-pota's
                          output, but only to dedup against, not to seed
                          from.
  5. merge            -- anywhere. Combines the stage outputs into the
                          final seed CSV in the `place` table's shape.

SOURCES (pulled 2026-08-24; PAD-US local-parks addition and the SOTA
threshold change below re-pulled the same day):
  SOTA summits  -- https://storage.sota.org.uk/summitslist.csv
                   NOTE: this file has a non-CSV title line before the
                   real header ("SOTA Summits List (Date=...)") -- skip
                   line 1, DictReader from line 2.
  POTA parks    -- https://pota.app/all_parks_ext.csv
                   Centre points only -- POTA publishes no boundaries.
                   Lists only what hams activate (state/national parks
                   POTA has itself designated a reference for) -- see
                   "PARK SOURCES" below for why this is not the only
                   park source any more.
  OSM landmarks -- /mnt/nas/nav/western-us-11states.osm.pbf on pi-nas
                   (read-only source storage -- never write there),
                   reachable from navi.
  PAD-US        -- /data/nav/padus/PADUS4_0_Geodatabase.gdb on navi,
                   layer PADUS4_0Combined_Proclamation_Marine_Fee_
                   Designation_Easement (all protected-area types in one
                   layer, so a park does not go unmatched just because
                   it happens to be an easement rather than a fee title).
                   Used twice: match_parks() attaches a boundary to a
                   POTA park; fetch_padus_parks() pulls this layer's own
                   local/city/county park units as parks in their own
                   right, whether or not POTA ever heard of them.

PARK SOURCES (fetch_padus_parks() added 2026-08-24, "Twin Falls has no
parks"): POTA lists only what hams activate for POTA credit -- almost
entirely state and national parks -- so a town with real, ordinary
municipal parks and nothing POTA-worthy showed zero parks at all, which
was the actual complaint (Twin Falls: one landmark, no parks, despite
having several). PAD-US already supplied park boundaries via
match_parks(); it also carries municipal parks in their own right,
tagged with a designation (Des_Tp) and a managing-agency code
(Mang_Type/Mang_Name), so fetch_padus_parks() pulls those directly as a
second, independent park source. Kept: designation LP (local park) or
LREC (local recreation area), managed by Mang_Type LOC or DIST
(city/county/regional-district, not state/federal/private/NGO -- those
are POTA's or nobody's), Pub_Access "OA" (open access -- RA/XA/UK
excluded). A 1-acre floor (MIN_PARK_ACRES, raised from an initial 0.1
after the first pull came back at 45,932 nationwide -- see that
constant's own comment) and a name-pattern exclusion drop slivers and
non-destinations (community gardens, detention basins, utility
parcels) PAD-US's LP tag also sweeps in. Matched against the POTA pull
by name + proximity (same technique match_parks() itself uses) so a
park listed in both does not get written twice -- see
fetch_padus_parks()'s own docstring for the exact rule. Result: 38,346
PAD-US local parks nationwide (8,107 at or above one grid cell --
permanent, scored by the >50% rule like a POTA-matched large park;
30,239 below -- rotate weekly like a landmark, same as a small
POTA-matched park). These carry source "PAD-US" rather than "POTA" or
"POTA/PAD-US"; app/places_seed.py's country filter treats that source
value as already US-only (see that module's docstring) rather than
running it through POTA's "US-"-prefix check, which their "PADUS-<fid>"
ref_code would fail.

PLAY AREA (from the running service's /config, NOT app/config.py's
narrower Idaho-only defaults -- production overrides those via .env):
  north 49.29  south 25.8  west -125.0  east -93.5

OSM TAG LIST -- the approved narrowed list (docs/features/places.md),
BROADENED 2026-08-24 with outdoor/natural destinations. fire_station
and post_office were cut by Matt and must NOT be restored:
  amenity=townhall, amenity=courthouse, amenity=library
  tourism=museum, tourism=viewpoint, tourism=attraction
  tourism=information WHERE information=visitor_centre
  historic=memorial, historic=monument, historic=marker
  -- added 2026-08-24, "places worth going" rebalance:
  natural=hot_spring, natural=arch, natural=cave_entrance, natural=waterfall
  historic=mine, historic=ruins, historic=fort, historic=battlefield, historic=wreck
  man_made=lighthouse
  tourism=alpine_hut, tourism=wilderness_hut
  leisure=nature_reserve WHERE geometry is a node or a small way (skipped
    if it would duplicate the parks tier -- see _landmark_match's area gate)
A landmark also needs a `name` tag -- an unnamed node matching one of
these tags is not a "named destination" and is skipped.

REMOVED 2026-08-25 ("trailheads shouldn't be marked as landmarks";
"lookouts in the mountains -- not a landmark"): highway=trailhead
(4,166 matches in the western-us extract) and man_made=tower WHERE
tower:type=observation, i.e. fire lookouts (312 matches). A trailhead
is where you start going somewhere, not a destination itself; a fire
lookout sitting on a peak is already scored as that peak's summit (see
_SUMMIT_COLOCATION_RADIUS_M in app/places_seed.py, which used to exist
purely to de-dup exactly this pair -- the tower is gone now, but the
colocation filter is left in place since a summit can still carry some
OTHER landmark-tagged structure worth de-duping against). Neither tag
is filtered at load time -- app/places_seed.py's loader has no OSM tag
to filter on, only ref_type=landmark -- so this had to be an extraction-
time change, which is why the seed needed a full rebuild rather than a
loader patch.

POINTS -- see "SCORING BY EFFORT, NOT CATEGORY" below. No longer flat
by ref_type; computed per row at merge time from distance to the
nearest Census place anchor in app/reference/places.csv.

SCORING BY EFFORT, NOT CATEGORY (added 2026-08-25, "everything ...
if they're easy to get to inside city limits should only be worth 5
points. REAL parks and places that are actual trips should be 25 or
100", then "remote landmark is 10"): the old model scored 5/25/100 by
ref_type alone, which meant a city park across a parking lot and a
wilderness park an hour up a dirt road both paid the same 25 -- the
category said nothing about the trip. The new model scores effort
instead:

  inside city limits     any type      5
  outside city limits    landmark     10
                          park         25
                          summit    50-100 (elevation-scaled, see below)

"City limits" is computed against app/reference/places.csv, the same
Census place anchors app/places.py already uses for "how far is the
nearest town" -- each row is (lat, lon, effective_radius_m), where the
radius is sqrt(ALAND/pi), a circle of the same land area as the place,
standing in for its limits (see that file's own header). A place is
IN CITY LIMITS if it falls within that radius of ANY anchor. This is
computed once, here, at seed-build time (merge()'s score_points()) and
baked into the `points` column, so app/places_seed.py's loader and the
scoring path need no new logic at all -- they already just read
`points` off the row. A second column, `points_reason`
("in_city"/"remote"), is written alongside it purely for visibility
(the admin preview, and any future re-tuning) -- nothing reads it to
make a scoring decision; `points` is still the only number that
matters at runtime.

The weekly per-person cap is unchanged at 100 -- see
docs/features/places.md.

SUMMIT THRESHOLD (added 2026-08-24, "places worth going" rebalance;
LOWERED again 2026-08-24, "too few summits"):
SOTA's own Points column is elevation-derived (a 1-10 scale keyed to a
summit's prominence within its region) and is exactly the "is this a
real mountain, not a bump" signal the earlier unfiltered pull lacked --
every SOTA summit became a marker regardless of size, and summits render
as the largest symbol, so 26,600 of them buried the map. Matt's first
brief was "high SOTA value, no easy picks" -- SUMMIT_MIN_POINTS was set
to 10 (SOTA's own scale only takes even values 2/4/6/8/10 plus 1, so 10
is literally the top of the scale, not an arbitrary round number), which
kept 1,865 US in-bbox summits nationwide, 141 in Idaho at the play
area's then-narrower bbox.

That turned out to be one stop too sparse once it was actually in play
("too few summits"). Dropped to the next threshold down, SOTA Points
>= 8 -- still the top half of the scale, not a wide-open floor.
Verified directly against a fresh pull rather than trusted from the
note that flagged it as the fallback option: **6,487 US in-bbox summits
nationwide, 472 in Idaho (W7I)**. Per-state counts for the states this
game's neighborhood actually covers (association code in parens):
Idaho 472 (W7I), Utah 420 (W7U), Nevada 635 (W7N), Montana 533 (W7M),
Wyoming 571 (W7Y), Colorado 539 (W0C), Washington 687 (W7W), Oregon 104
(W7O), California 1,002 (W6). This filter runs in fetch_sota() below,
on SOTA's Points column, BEFORE the bbox/name checks -- not a separate
stage, since it only needs the one column already being read. The
`points` column written to the seed CSV is unrelated and unchanged by
this: it is always a placeholder (POINTS["summit"], overwritten by
score_points() at merge time -- see ELEVATION SCALING above), never
SOTA's own points value.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import io
import math
import os
import sys
import urllib.request

NORTH, SOUTH, WEST, EAST = 49.29, 25.8, -125.0, -93.5

SOTA_URL = "https://storage.sota.org.uk/summitslist.csv"
POTA_URL = "https://pota.app/all_parks_ext.csv"

# Provisional only -- each pipeline stage below writes a row with a
# placeholder points value from this dict so the CSV shape is valid at
# every intermediate stage, but the REAL points value (effort-scored,
# not flat by type) is computed once, for every row regardless of which
# stage produced it, by merge()'s score_points() -- see that function
# and the module docstring's "SCORING BY EFFORT, NOT CATEGORY".
POINTS = {"summit": 100, "park": 25, "landmark": 10}

# Real scoring, computed at merge time (score_points()) --
# IN_CITY_POINTS applies to every ref_type; REMOTE_POINTS is keyed by
# ref_type for everything outside city limits EXCEPT summit, which
# scales by elevation instead of a flat value -- see ELEVATION SCALING
# below and the module docstring.
IN_CITY_POINTS = 5
REMOTE_POINTS = {"park": 25, "landmark": 10}

# ELEVATION SCALING (added 2026-08-25, "lets make the points for peaks
# scaling. 50 for low elevation peaks up to 100 for 9000ft +"): a remote
# summit's value now scales linearly with elevation instead of paying
# the same flat 100 a modest hill and a 12,000ft climb both used to.
# The in-city rule still wins outright -- a summit inside a town's
# limits is worth IN_CITY_POINTS (5) regardless of height, because you
# can park at it; scaling only ever applies to a summit that failed the
# in-city check. See score_points() and _summit_points() below for the
# order that enforces that.
#
# Floor measured, not assumed, against the 7,987 currently-active
# REMOTE summits in the seed at the time this was added (SOTA AltFt
# joined onto app/reference/places_worth_going.csv by ref_code): min
# 2,110ft, p10 6,761ft, p25 7,611ft, median 8,992ft, p75 10,569ft, p90
# 12,540ft, max 14,494ft. The bottom of that range is not a smooth
# taper -- a genuine gap sits between a small low-elevation cluster (80
# summits, 2,110-3,999ft; these clear the SUMMIT_MIN_SOTA_POINTS
# prominence bar despite low absolute elevation, e.g. relief above flat
# surrounding terrain) and where the real body of the distribution
# begins: only 16 summits fall in 4,000-5,999ft at all, then 1,099 land
# in 6,000-6,999ft alone. p1 is 6,002ft, right at that seam. 6,000ft is
# the floor for exactly that reason -- it is where "low elevation peak"
# actually starts in this data, not a round number picked from nowhere,
# and it costs only the 80 outlier summits below it (1.0% of remote
# summits) a fixed 50 rather than a slightly-lower scaled value they
# were never going to reach anyway.
SUMMIT_ELEV_FLOOR_FT = 6000
# 9,000ft is Matt's own ceiling ("up to 100 for 9000ft+"), not derived
# -- it happens to land almost exactly on the measured median (8,992ft),
# so very close to half of today's remote summits cap out at 100.
SUMMIT_ELEV_CEIL_FT = 9000
SUMMIT_MIN_REMOTE_POINTS = 50
SUMMIT_MAX_REMOTE_POINTS = 100


def _summit_points(elevation_ft: float | None) -> int:
    """Linear 50->100 scale from SUMMIT_ELEV_FLOOR_FT to
    SUMMIT_ELEV_CEIL_FT, clamped at both ends, rounded to a whole point
    -- see ELEVATION SCALING above for where the floor and ceiling come
    from. elevation_ft is None only if a row somehow reached here
    without one (should not happen -- fetch_sota() skips any SOTA row
    missing AltFt outright); treated as the floor rather than raising,
    so one malformed upstream row degrades a single summit's score
    instead of failing the whole merge."""
    if elevation_ft is None:
        return SUMMIT_MIN_REMOTE_POINTS
    if elevation_ft <= SUMMIT_ELEV_FLOOR_FT:
        return SUMMIT_MIN_REMOTE_POINTS
    if elevation_ft >= SUMMIT_ELEV_CEIL_FT:
        return SUMMIT_MAX_REMOTE_POINTS
    frac = (elevation_ft - SUMMIT_ELEV_FLOOR_FT) / (SUMMIT_ELEV_CEIL_FT - SUMMIT_ELEV_FLOOR_FT)
    return round(SUMMIT_MIN_REMOTE_POINTS + frac * (SUMMIT_MAX_REMOTE_POINTS - SUMMIT_MIN_REMOTE_POINTS))


# SOTA's own elevation-derived Points column (1-10, effectively
# 1/2/4/6/8/10 -- see module docstring "SUMMIT THRESHOLD"). Only
# summits at or above this SOTA points value become a `place` row at
# all; this is a PROMINENCE filter (relative relief within SOTA's own
# region), distinct from the summit's own absolute elevation in feet
# that ELEVATION SCALING above scores by -- a summit can clear this bar
# with a low AltFt (see the 80-summit low cluster noted there) and
# still only score the floor.
#
# LOWERED 2026-08-24, "too few summits": >=10 (1,865 nationwide / 141
# Idaho at the time) was one stop too sparse in play. Dropped to the
# next threshold down, >=8, per docs/features/places.md and Matt's
# feedback -- see module docstring "SUMMIT THRESHOLD" for the measured
# counts at this threshold.
SUMMIT_MIN_SOTA_POINTS = 8

SEED_FIELDS = [
    "ref_type", "ref_code", "name", "lat", "lon", "points", "source",
    "area_m2", "geom", "elevation_ft",
]

# elevation_ft (added 2026-08-25, "scaling summit points by elevation")
# is only ever populated by fetch_sota(), from SOTA's own AltFt column
# -- summit is the only ref_type score_points() scales by anything, so
# it is the only ref_type that needs the number. Every other stage
# (fetch_pota via match_parks/fetch_padus_parks, extract_landmarks)
# writes "" for this field so the CSV stays SEED_FIELDS-shaped at every
# stage; merge() carries it straight through from whichever row it
# came from, same as area_m2/geom.
#
# The merged, final seed adds one column beyond SEED_FIELDS:
# points_reason ("in_city" / "remote" / "remote_scaled") records WHY a
# row got the points value it did -- not read by app/places_seed.py's
# loader (which only ever reads `points` itself), but carried through
# to the `place` table for the admin panel and future re-tuning to see.
# Only merge()'s output (the actual app/reference/places_worth_going.csv)
# carries this column; the intermediate per-stage CSVs (fetch-sota,
# fetch-pota, extract-landmarks, match-parks, fetch-padus-parks) stay
# SEED_FIELDS-shaped, since none of them can compute the real value on
# its own -- that needs the full merged row set plus
# app/reference/places.csv, and is the reason score_points() has to
# live in merge(), not in each individual stage.
FINAL_SEED_FIELDS = SEED_FIELDS + ["points_reason"]


def in_bbox(lat: float, lon: float) -> bool:
    return SOUTH <= lat <= NORTH and WEST <= lon <= EAST


# --------------------------------------------------------------------
# Stage 1: SOTA summits
# --------------------------------------------------------------------
def fetch_sota(out_path: str) -> None:
    req = urllib.request.Request(SOTA_URL, headers={"User-Agent": "meshwars-places-seed/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8-sig", errors="replace")

    lines = raw.splitlines()
    # First line is a title ("SOTA Summits List (Date=...)"), not CSV --
    # confirmed by inspection before writing this, not assumed. The real
    # header is line 2.
    if not lines[0].lstrip().startswith("SummitCode"):
        lines = lines[1:]
    reader = csv.DictReader(lines)

    kept = 0
    skipped_low_points = 0
    skipped_no_altft = 0
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(SEED_FIELDS)
        for row in reader:
            try:
                lat = float(row["Latitude"])
                lon = float(row["Longitude"])
                sota_points = int(row["Points"])
            except (KeyError, ValueError):
                continue
            if sota_points < SUMMIT_MIN_SOTA_POINTS:
                skipped_low_points += 1
                continue
            if not in_bbox(lat, lon):
                continue
            code = row["SummitCode"].strip()
            name = row["SummitName"].strip()
            if not code or not name:
                continue
            # AltFt is SOTA's own feet figure (AltM is the metres source
            # it derives from) -- read directly rather than converting
            # AltM ourselves, so this always agrees with what SOTA
            # itself publishes. Feeds score_points()'s elevation
            # scaling at merge time (see ELEVATION SCALING in this
            # module's docstring); a row missing/unparseable AltFt is
            # skipped outright rather than kept with a blank -- an
            # unscored summit sitting in the seed with no elevation to
            # scale from is worse than one fewer summit, and this has
            # not happened against a real SOTA pull so far.
            try:
                elevation_ft = round(float(row["AltFt"]))
            except (KeyError, ValueError):
                skipped_no_altft += 1
                continue
            w.writerow(["summit", code, name, f"{lat:.6f}", f"{lon:.6f}",
                        POINTS["summit"], "SOTA", "", "", elevation_ft])
            kept += 1
    print(f"sota: wrote {kept} summits (SOTA Points >= {SUMMIT_MIN_SOTA_POINTS}; "
          f"{skipped_low_points} below threshold worldwide, {skipped_no_altft} "
          f"missing AltFt) -> {out_path}", file=sys.stderr)


# --------------------------------------------------------------------
# Stage 2: POTA parks (centre points, no boundary yet)
# --------------------------------------------------------------------
def fetch_pota(out_path: str) -> None:
    req = urllib.request.Request(POTA_URL, headers={"User-Agent": "meshwars-places-seed/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8-sig", errors="replace")

    reader = csv.DictReader(io.StringIO(raw))
    kept = 0
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["reference", "name", "lat", "lon"])
        for row in reader:
            if row.get("active") != "1":
                continue
            try:
                lat = float(row["latitude"])
                lon = float(row["longitude"])
            except (KeyError, ValueError):
                continue
            if not in_bbox(lat, lon):
                continue
            ref = row["reference"].strip()
            name = row["name"].strip()
            if not ref or not name:
                continue
            w.writerow([ref, name, f"{lat:.6f}", f"{lon:.6f}"])
            kept += 1
    print(f"pota: wrote {kept} active in-bbox parks -> {out_path}", file=sys.stderr)


# --------------------------------------------------------------------
# Stage 3: OSM landmarks -- run on navi
# --------------------------------------------------------------------
# A 300 m game-grid square is 90,000 m^2 -- same cell app/places_seed.py
# scores a park against. Used below only as the "small area" gate for
# leisure=nature_reserve ways, so a big reserve that would duplicate the
# parks tier is skipped rather than double-counted as a landmark too.
SQUARE_AREA_M2 = 300.0 * 300.0

LANDMARK_TAGS = {
    ("amenity", "townhall"), ("amenity", "courthouse"), ("amenity", "library"),
    ("tourism", "museum"), ("tourism", "viewpoint"), ("tourism", "attraction"),
    ("historic", "memorial"), ("historic", "monument"), ("historic", "marker"),
    # REMOVED 2026-08-25, "trailheads shouldn't be marked as landmarks":
    # ("highway", "trailhead") -- 4,166 matches in the western-us
    # extract. A trailhead is where you start going somewhere, not a
    # destination itself.
    # added 2026-08-24, "places worth going" rebalance -- outdoor/natural
    # destinations, to counter the civic-heavy original list:
    ("natural", "hot_spring"), ("natural", "arch"),
    ("natural", "cave_entrance"), ("natural", "waterfall"),
    ("historic", "mine"), ("historic", "ruins"), ("historic", "fort"),
    ("historic", "battlefield"), ("historic", "wreck"),
    ("man_made", "lighthouse"),
    ("tourism", "alpine_hut"), ("tourism", "wilderness_hut"),
}


def _matched_tag(tags) -> str | None:
    """Returns 'key=value' for the specific tag that made this object a
    landmark, or None if it does not match -- used both by
    _landmark_match (boolean gate) and by extract_landmarks' per-tag
    reporting Counter."""
    for k, v in LANDMARK_TAGS:
        if tags.get(k) == v:
            return f"{k}={v}"
    if tags.get("tourism") == "information" and tags.get("information") == "visitor_centre":
        return "tourism=information+visitor_centre"
    # Fire lookouts (man_made=tower WHERE tower:type=observation) were
    # REMOVED 2026-08-25, "lookouts in the mountains -- not a landmark"
    # -- 312 matches in the western-us extract. A lookout on a peak is
    # the summit someone already climbed, not a separate destination;
    # app/places_seed.py's summit/landmark colocation filter used to
    # exist mostly to de-dup exactly this pair.
    # leisure=nature_reserve matches here on tags alone; the node-vs-way
    # "is it small" gate (skip if it would duplicate the parks tier) is
    # applied in the way() handler below, where the geometry is known.
    if tags.get("leisure") == "nature_reserve":
        return "leisure=nature_reserve"
    return None


def _landmark_match(tags) -> bool:
    return _matched_tag(tags) is not None


def _bbox_area_m2(lats: list, lons: list) -> float:
    """Rough bounding-box area for a way's node coordinates -- not a
    true polygon area, but enough to tell a pocket nature reserve from
    one that is PAD-US-scale and belongs in the parks tier instead."""
    mean_lat = sum(lats) / len(lats)
    lat_m = (max(lats) - min(lats)) * 111_320.0
    lon_m = (max(lons) - min(lons)) * 111_320.0 * math.cos(math.radians(mean_lat))
    return lat_m * lon_m


def extract_landmarks(pbf_path: str, out_path: str) -> None:
    """Run on navi against the tags-filter output, e.g.:

        osmium tags-filter -o filtered.pbf --overwrite \\
            /mnt/nas/nav/western-us-11states.osm.pbf \\
            amenity=townhall,courthouse,library \\
            tourism=museum,viewpoint,attraction,information,alpine_hut,wilderness_hut \\
            historic=memorial,monument,marker,mine,ruins,fort,battlefield,wreck \\
            natural=hot_spring,arch,cave_entrance,waterfall \\
            man_made=lighthouse \\
            leisure=nature_reserve

    then: python3 build_places_seed.py extract-landmarks filtered.pbf landmarks.csv

    Nodes are used as-is. Ways are reduced to the plain average of their
    node coordinates -- not a true area centroid, but these are point-of-
    interest buildings and small grounds (museums, trailheads, town
    halls), not large irregular polygons, so the difference is noise at
    game-grid (300 m) scale. leisure=nature_reserve ways are the one
    exception where a real bounding-box area is computed (_bbox_area_m2),
    since reserves genuinely do span from a pocket wetland to a national-
    forest-scale unit that already belongs in the parks tier -- see the
    SQUARE_AREA_M2 gate in way() below. Relations are skipped:
    multipolygon assembly for ~359 objects out of ~19,000 was not worth
    the added dependency surface, and none of these tags commonly appear
    on relations.
    """
    import osmium
    from collections import Counter

    class Handler(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.rows = []
            self.seen_names_skipped = 0
            self.large_reserves_skipped = 0
            self.tag_counts = Counter()

        def node(self, n):
            if not n.location.valid():
                return
            tags = n.tags
            matched = _matched_tag(tags)
            if matched is None:
                return
            name = tags.get("name")
            if not name:
                self.seen_names_skipped += 1
                return
            lat, lon = n.location.lat, n.location.lon
            if not in_bbox(lat, lon):
                return
            self.rows.append(("n", n.id, name, lat, lon))
            self.tag_counts[matched] += 1

        def way(self, w):
            tags = w.tags
            matched = _matched_tag(tags)
            if matched is None:
                return
            name = tags.get("name")
            if not name:
                self.seen_names_skipped += 1
                return
            lats, lons = [], []
            for nd in w.nodes:
                if nd.location.valid():
                    lats.append(nd.location.lat)
                    lons.append(nd.location.lon)
            if not lats:
                return
            if tags.get("leisure") == "nature_reserve":
                if _bbox_area_m2(lats, lons) > SQUARE_AREA_M2:
                    self.large_reserves_skipped += 1
                    return
            lat = sum(lats) / len(lats)
            lon = sum(lons) / len(lons)
            if not in_bbox(lat, lon):
                return
            self.rows.append(("w", w.id, name, lat, lon))
            self.tag_counts[matched] += 1

    h = Handler()
    # locations=True resolves way node coordinates against the file's own
    # node data (tags-filter's default keeps referenced nodes for exactly
    # this reason).
    h.apply_file(pbf_path, locations=True)

    seen = set()
    kept = 0
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(SEED_FIELDS)
        for kind, osm_id, name, lat, lon in h.rows:
            code = f"{kind}{osm_id}"
            if code in seen:
                continue
            seen.add(code)
            w.writerow(["landmark", code, name, f"{lat:.6f}", f"{lon:.6f}",
                        POINTS["landmark"], "OSM", "", "", ""])
            kept += 1
    print(f"landmarks: wrote {kept} named landmarks ({h.seen_names_skipped} "
          f"unnamed matches skipped, {h.large_reserves_skipped} large nature "
          f"reserves skipped as parks-tier duplicates) -> {out_path}", file=sys.stderr)
    print("landmarks: per-tag match counts (pre-dedup, a node/way can only "
          "match one tag):", file=sys.stderr)
    for tag, n in h.tag_counts.most_common():
        print(f"  {tag}: {n}", file=sys.stderr)


# --------------------------------------------------------------------
# Stage 4: match POTA parks to PAD-US boundaries -- run on navi
# --------------------------------------------------------------------
PADUS_GDB = "/data/nav/padus/PADUS4_0_Geodatabase.gdb"
PADUS_LAYER = "PADUS4_0Combined_Proclamation_Marine_Fee_Designation_Easement"

# A 300 m game-grid square is 90,000 m^2.
SQUARE_AREA_M2 = 300.0 * 300.0

_STOPWORDS = {
    "the", "of", "and", "at", "area", "site", "park", "state", "national",
    "county", "city", "recreation", "historic", "historical", "natural",
    "forest", "monument", "preserve", "reserve", "wildlife", "management",
    "wma", "nwr", "nrp", "srp", "unit", "district", "trail", "trailhead",
}


def _norm_name(s: str) -> set:
    s = s.lower()
    for ch in "-_,.'\"()/":
        s = s.replace(ch, " ")
    return {w for w in s.split() if w and w not in _STOPWORDS}


def _name_score(a: str, b: str) -> float:
    wa, wb = _norm_name(a), _norm_name(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def match_parks(pota_csv: str, out_path: str) -> None:
    """Run on navi:  python3 build_places_seed.py match-parks pota.csv parks_matched.csv

    Matching rule: among PAD-US polygons whose bounding box comes within
    ~2 km of the POTA centre point, keep the one with the best normalized
    name-word overlap (Jaccard on stopword-stripped tokens), and only
    accept it if that polygon actually CONTAINS the point, OR the name
    overlap is very strong (>=0.5) and the point is within 500 m of the
    polygon -- POTA centre points are hand-entered and sometimes fall
    just outside their own park's mapped boundary. Anything short of
    that is left unmatched rather than guessed at.
    """
    from osgeo import ogr, osr
    import shapely
    from shapely import wkb as shapely_wkb
    from shapely.strtree import STRtree

    ds = ogr.Open(PADUS_GDB)
    layer = ds.GetLayerByName(PADUS_LAYER)
    src_srs = layer.GetSpatialRef()
    dst_srs = osr.SpatialReference()
    dst_srs.ImportFromEPSG(4326)
    dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    xform = osr.CoordinateTransformation(src_srs, dst_srs)

    # Bbox filter in the layer's native SRS -- transform the play-area
    # corners into it first.
    corners = [(WEST, SOUTH), (EAST, SOUTH), (EAST, NORTH), (WEST, NORTH)]
    xs, ys = [], []
    inv = osr.CoordinateTransformation(dst_srs, src_srs)
    for lon, lat in corners:
        x, y, _ = inv.TransformPoint(lon, lat)
        xs.append(x)
        ys.append(y)
    layer.SetSpatialFilterRect(min(xs), min(ys), max(xs), max(ys))

    print(f"padus: layer has {layer.GetFeatureCount()} features in bbox", file=sys.stderr)

    geoms = []
    names = []
    areas = []
    for feat in layer:
        g = feat.GetGeometryRef()
        if g is None:
            continue
        g2 = g.Clone()
        g2.Transform(xform)
        try:
            geom = shapely_wkb.loads(bytes(g2.ExportToWkb()))
        except Exception:
            continue
        if geom.is_empty:
            continue
        name = feat.GetField("Unit_Nm") or feat.GetField("Loc_Nm") or ""
        geoms.append(geom)
        names.append(name)
        # GIS_Acres is PAD-US's own area figure (acres); convert to m^2.
        acres = feat.GetField("GIS_Acres")
        areas.append((acres or 0) * 4046.8564224)
    layer.ResetReading()

    tree = STRtree(geoms)
    print(f"padus: {len(geoms)} candidate polygons loaded for matching", file=sys.stderr)

    with open(pota_csv, encoding="utf-8") as fh, \
         open(out_path, "w", newline="", encoding="utf-8") as out:
        reader = csv.DictReader(fh)
        w = csv.writer(out)
        w.writerow(SEED_FIELDS)
        matched = 0
        total = 0
        for row in reader:
            total += 1
            lat = float(row["lat"])
            lon = float(row["lon"])
            name = row["name"]
            pt = shapely.Point(lon, lat)
            # ~2km in degrees, generous at this latitude range.
            buf = pt.buffer(0.02)
            idxs = tree.query(buf)
            best = None
            best_score = 0.0
            for i in idxs:
                g = geoms[i]
                score = _name_score(name, names[i])
                contains = g.contains(pt)
                near = g.distance(pt) < 0.0045  # ~500 m
                if contains and score > best_score:
                    best, best_score = i, score
                elif best is None and score >= 0.5 and near:
                    best, best_score = i, score
                elif score >= 0.5 and near and score > best_score:
                    best, best_score = i, score
            area_m2 = ""
            geom_wkt = ""
            if best is not None:
                matched += 1
                g = geoms[best]
                area_m2 = areas[best] if areas[best] else g.area * (111320.0 ** 2) * abs(
                    __import__("math").cos(__import__("math").radians(lat)))
                # PAD-US units are frequently multi-part -- a Wetland
                # Management District or a Refuge can bundle dozens of
                # parcels scattered across a whole region under one
                # polygon. The 50%-of-a-square rule only ever gets
                # evaluated for a square near where someone actually
                # stood, so the geometry only needs to be right THERE:
                # clip to a ~6 km buffer around the POTA point before
                # simplifying. area_m2 above is untouched by this --
                # it still reflects PAD-US's own whole-unit acreage
                # (GIS_Acres), so the larger/smaller-than-a-square
                # classification stays correct even for a park whose
                # boundary got clipped for storage. A handful of huge,
                # genuinely single-blob parks (Grand Canyon, Yosemite)
                # lose their far side this way too, but nobody is
                # claiming a square several km from where they
                # activated and calling it that park either -- and the
                # 6 km radius is still twenty grid squares deep in
                # every direction.
                try:
                    clipped = g.intersection(pt.buffer(0.06))
                except Exception:
                    # PAD-US ships a handful of self-intersecting
                    # polygons; buffer(0) is the standard shapely fixup.
                    g_fixed = g.buffer(0)
                    try:
                        clipped = g_fixed.intersection(pt.buffer(0.06))
                    except Exception:
                        clipped = g_fixed
                if clipped.is_empty:
                    clipped = g
                simplified = clipped.simplify(0.0008, preserve_topology=True)
                geom_wkt = simplified.wkt
            w.writerow(["park", row["reference"], name, f"{lat:.6f}", f"{lon:.6f}",
                        POINTS["park"], "POTA/PAD-US" if best is not None else "POTA",
                        f"{area_m2:.0f}" if area_m2 != "" else "", geom_wkt, ""])
        print(f"parks: {matched}/{total} matched a PAD-US boundary "
              f"({total - matched} unmatched, kept as points)", file=sys.stderr)


# --------------------------------------------------------------------
# Stage 4b: PAD-US local/city/county parks -- run on navi
# --------------------------------------------------------------------
# ADDED 2026-08-24, "too few parks" -- POTA lists only what hams
# activate (state and national parks), so a town with real municipal
# parks and nothing POTA-worthy showed zero (Twin Falls: one landmark,
# no parks, despite having parks). PAD-US already supplies park
# boundaries (match_parks above); it also carries municipal parks in
# their own right, tagged with a designation and a managing agency --
# this stage pulls those directly as a second park source, alongside
# POTA rather than instead of it.
#
# Kept: Des_Tp (designation) LP (local park) or LREC (local recreation
# area) -- PAD-US's own "this is a park, not an easement or a wildlife
# refuge" signal -- AND Mang_Type (manager type) LOC or DIST (city/
# county/regional-district managed, not state/federal/private/NGO,
# which POTA or the summit/landmark sources already cover). Also
# requires Pub_Access "OA" (open access) -- RA (restricted), XA
# (closed) and UK (unknown) are excluded, since a place worth going
# needs to actually be reachable, not merely believed to be.
#
# A GIS acreage floor (MIN_ACRES) drops slivers PAD-US tags LP/LREC
# for being public land but that are not a destination anyone would
# drive to: traffic islands, "Stairway & Pedestrian Way", a detention
# pond's mowed edge. A name-pattern exclusion catches the other kind of
# false positive the acreage floor cannot: community gardens and
# single-purpose utility parcels (detention/retention basins, water
# towers, substations, lift/pump stations, rights-of-way) that PAD-US's
# LP designation also sweeps in even at a normal park's size.
LOCAL_PARK_DESIGNATIONS = {"LP", "LREC"}
LOCAL_PARK_MANAGER_TYPES = {"LOC", "DIST"}
# RAISED 2026-08-24 from an initial 0.1 acre: the 0.1 floor kept every
# LP/LREC feature down to a traffic island (45,932 nationwide, over the
# "flag it" line) because a huge share of PAD-US's local-park layer is
# genuinely sub-acre -- tot lots, pocket parks, mini-parks -- and a
# name-pattern alone cannot separate "small real park" from "mowed
# strip PAD-US also tagged LP". 1.0 acre (roughly a football field) is
# the cut that brings the nationwide count back under 40,000 (38,346)
# while keeping every park big enough to plausibly be a destination,
# not just publicly-owned ground.
MIN_PARK_ACRES = 1.0  # ~43,560 sq ft -- below this is a sliver, not a park


def _compile_exclude_park_name_re():
    import re
    return re.compile(
        r"community\s+garden|detention|retention\s*(basin|pond)?|stormwater|"
        r"water\s+tower|\btank\b|substation|lift\s+station|pump\s+station|"
        r"right.?of.?way|\beasement\b|parking\s+(lot|structure|garage)|"
        r"comfort\s+station|maintenance\s+(yard|facility|shop)",
        re.IGNORECASE,
    )


# ~500 m -- same "hand-entered centre point can land just outside its
# own park" tolerance match_parks() uses for its near-match fallback,
# reused here as the dedup radius against a POTA point.
_DEDUP_NEAR_DEG = 0.0045
# ~2 km -- generous outer query radius before the name/near checks
# narrow it down, same as match_parks()'s own STRtree query buffer.
_DEDUP_QUERY_DEG = 0.02


def fetch_padus_parks(pota_csv: str, out_path: str) -> None:
    """Run on navi:  python3 build_places_seed.py fetch-padus-parks pota.csv padus_parks.csv

    pota_csv is fetch_pota()'s raw output (reference,name,lat,lon), used
    only to deduplicate against -- a PAD-US candidate whose name is a
    strong match (Jaccard >=0.5, same _name_score as match_parks) to a
    nearby (within ~500 m) POTA park is dropped so a park listed in both
    programmes is not written twice. This is deliberately looser than
    match_parks()'s own accept rule (which also accepts on `contains`
    alone) because the goal here is the opposite: match_parks decides
    whether to attach a boundary to a POTA row; this decides whether to
    SKIP a PAD-US row, so a false-negative dedup (a real duplicate slips
    through) is the safer failure than a false-positive one (a real
    Twin-Falls-style city park gets dropped because it happens to share
    a word or two with some unrelated POTA park 2 km away).
    """
    from osgeo import ogr, osr
    import shapely
    from shapely import wkb as shapely_wkb
    from shapely.strtree import STRtree

    exclude_re = _compile_exclude_park_name_re()

    ds = ogr.Open(PADUS_GDB)
    layer = ds.GetLayerByName(PADUS_LAYER)
    src_srs = layer.GetSpatialRef()
    dst_srs = osr.SpatialReference()
    dst_srs.ImportFromEPSG(4326)
    dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    xform = osr.CoordinateTransformation(src_srs, dst_srs)

    corners = [(WEST, SOUTH), (EAST, SOUTH), (EAST, NORTH), (WEST, NORTH)]
    xs, ys = [], []
    inv = osr.CoordinateTransformation(dst_srs, src_srs)
    for lon, lat in corners:
        x, y, _ = inv.TransformPoint(lon, lat)
        xs.append(x)
        ys.append(y)
    layer.SetSpatialFilterRect(min(xs), min(ys), max(xs), max(ys))
    print(f"padus-parks: layer has {layer.GetFeatureCount()} features in bbox", file=sys.stderr)

    # POTA points, for dedup only -- see docstring.
    pota_pts = []
    pota_names = []
    with open(pota_csv, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pota_pts.append(shapely.Point(float(row["lon"]), float(row["lat"])))
            pota_names.append(row["name"])
    pota_tree = STRtree(pota_pts) if pota_pts else None

    seen_fids = set()
    kept = 0
    larger = 0
    smaller = 0
    counts = {
        "wrong_designation": 0, "wrong_manager": 0, "not_open_access": 0,
        "no_name": 0, "name_excluded": 0, "too_small": 0, "dup_of_pota": 0,
        "no_geom": 0, "out_of_bbox": 0,
    }

    with open(out_path, "w", newline="", encoding="utf-8") as out:
        w = csv.writer(out)
        w.writerow(SEED_FIELDS)
        for feat in layer:
            if feat.GetField("Des_Tp") not in LOCAL_PARK_DESIGNATIONS:
                counts["wrong_designation"] += 1
                continue
            if feat.GetField("Mang_Type") not in LOCAL_PARK_MANAGER_TYPES:
                counts["wrong_manager"] += 1
                continue
            if feat.GetField("Pub_Access") != "OA":
                counts["not_open_access"] += 1
                continue
            name = (feat.GetField("Unit_Nm") or feat.GetField("Loc_Nm") or "").strip()
            if not name:
                counts["no_name"] += 1
                continue
            if exclude_re.search(name):
                counts["name_excluded"] += 1
                continue

            g = feat.GetGeometryRef()
            if g is None:
                counts["no_geom"] += 1
                continue
            g2 = g.Clone()
            g2.Transform(xform)
            try:
                geom = shapely_wkb.loads(bytes(g2.ExportToWkb()))
            except Exception:
                counts["no_geom"] += 1
                continue
            if geom.is_empty:
                counts["no_geom"] += 1
                continue

            centroid = geom.centroid
            lat, lon = centroid.y, centroid.x
            if not in_bbox(lat, lon):
                counts["out_of_bbox"] += 1
                continue

            # GIS_Acres is PAD-US's own figure but is stored as an
            # Integer (rounds to the nearest whole acre), which would
            # bucket every park under half an acre into the same "0"
            # and make MIN_PARK_ACRES unable to tell a real pocket park
            # from a true sliver. Compute area from the transformed
            # geometry instead -- same formula match_parks() falls back
            # to when GIS_Acres is missing, used here as the primary
            # figure rather than a fallback for exactly that precision
            # reason.
            area_m2 = geom.area * (111_320.0 ** 2) * math.cos(math.radians(lat))
            acres = area_m2 / 4046.8564224
            if acres < MIN_PARK_ACRES:
                counts["too_small"] += 1
                continue

            if pota_tree is not None:
                pt = shapely.Point(lon, lat)
                buf = pt.buffer(_DEDUP_QUERY_DEG)
                is_dup = False
                for i in pota_tree.query(buf):
                    if _name_score(name, pota_names[i]) >= 0.5 and pt.distance(pota_pts[i]) < _DEDUP_NEAR_DEG:
                        is_dup = True
                        break
                if is_dup:
                    counts["dup_of_pota"] += 1
                    continue

            fid = feat.GetFID()
            if fid in seen_fids:
                continue
            seen_fids.add(fid)

            # NOTE: geom_wkt is written for every park regardless of
            # size, even though app/places_seed.py's loader only ever
            # parses it for a park at or above one grid cell -- a
            # SMALLER matched park still needs geom_wkt to be non-empty,
            # because the loader uses "geom_wkt present" (not its
            # content) as the signal that this is a genuinely matched-
            # but-small park (rotates=True, like a landmark) rather than
            # an unmatched one (rotates=False, permanent -- see
            # app/places_seed.py's _classify_row/load_places_seed
            # docstrings). Blanking it to save space would silently flip
            # every small city park to non-rotating, which contradicts
            # docs/features/places.md's rotation rule -- so the real
            # (simplified) boundary is kept for every matched park, and
            # the seed CSV is larger for it.
            simplified = geom.simplify(0.0008, preserve_topology=True)
            w.writerow(["park", f"PADUS-{fid}", name, f"{lat:.6f}", f"{lon:.6f}",
                        POINTS["park"], "PAD-US", f"{area_m2:.0f}", simplified.wkt, ""])
            kept += 1
            if area_m2 >= SQUARE_AREA_M2:
                larger += 1
            else:
                smaller += 1

    print(f"padus-parks: wrote {kept} local/city/county parks "
          f"(larger-than-cell={larger} permanent, smaller-than-cell={smaller} rotating) "
          f"-> {out_path}", file=sys.stderr)
    print(f"padus-parks: excluded {counts}", file=sys.stderr)


# --------------------------------------------------------------------
# Stage 5: merge -- also where the real, effort-based points value is
# computed (score_points below), since that is the first point in the
# pipeline where the full row set exists.
# --------------------------------------------------------------------

# Default location of the Census place anchors (lat, lon,
# effective_radius_m) this scores city-limits containment against --
# same file app/places.py already uses for "how far is the nearest
# town". Resolved relative to this script's own location (not cwd), so
# `merge` still finds it when run from some other directory, the same
# way the rest of this pipeline is meant to run "anywhere".
_DEFAULT_PLACES_CSV = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "app", "reference", "places.csv"
)

# Anchor bucket size for the coarse spatial index _load_city_anchors
# builds below, in degrees. Must be bigger than the largest possible
# search radius in EITHER direction so a 3x3-bucket neighbourhood around
# a place's own bucket can never miss a real match. The largest anchor
# radius in app/reference/places.csv is ~25.3km; converting that to
# degrees of longitude (the more demanding direction, since a degree of
# longitude covers fewer metres than a degree of latitude everywhere
# except the equator) at this play area's northernmost latitude (49.29N,
# cos ~0.652) gives ~0.35 degrees -- 1.0 degree buckets leave a wide,
# deliberate margin over that, not a tight fit.
_ANCHOR_BUCKET_DEG = 1.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Same formula as app/grid.py's distance_m, duplicated rather than
    imported: this script is meant to run standalone ("anywhere", per
    the module docstring, including on navi with no PYTHONPATH pointed
    at the app package), so it carries no dependency on the app/ tree."""
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2.0) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2)
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _load_city_anchors(path: str) -> dict[tuple[int, int], list[tuple[float, float, float]]]:
    """Reads app/reference/places.csv (lat,lon,effective_radius_m,
    comment lines starting with '#') and buckets each anchor into a
    coarse (lat_bucket, lon_bucket) grid at _ANCHOR_BUCKET_DEG
    resolution, so _in_city_limits below only has to haversine-check
    anchors in a place's own 3x3-bucket neighbourhood instead of every
    anchor in the country."""
    buckets: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    with open(path, encoding="utf-8", newline="") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            lat_s, lon_s, radius_s = line.split(",")
            lat, lon, radius_m = float(lat_s), float(lon_s), float(radius_s)
            key = (math.floor(lat / _ANCHOR_BUCKET_DEG), math.floor(lon / _ANCHOR_BUCKET_DEG))
            buckets.setdefault(key, []).append((lat, lon, radius_m))
    return buckets


def _in_city_limits(lat: float, lon: float, buckets: dict) -> bool:
    """True if (lat, lon) falls within ANY anchor's effective_radius_m
    -- see _load_city_anchors and the module docstring's "SCORING BY
    EFFORT, NOT CATEGORY" for what this radius means."""
    lat_b = math.floor(lat / _ANCHOR_BUCKET_DEG)
    lon_b = math.floor(lon / _ANCHOR_BUCKET_DEG)
    for d_lat in (-1, 0, 1):
        for d_lon in (-1, 0, 1):
            for a_lat, a_lon, radius_m in buckets.get((lat_b + d_lat, lon_b + d_lon), ()):
                if _haversine_m(lat, lon, a_lat, a_lon) <= radius_m:
                    return True
    return False


def score_points(row: dict, buckets: dict) -> tuple[int, str]:
    """(points, points_reason) for one merged row -- the real,
    effort-based value, replacing whatever placeholder points value the
    row's originating stage wrote. See module docstring.

    In-city checked FIRST, for every ref_type including summit: a
    summit inside a town's limits is worth IN_CITY_POINTS regardless of
    elevation, because you can park at it -- see ELEVATION SCALING
    above. Only a summit that fails that check goes on to
    _summit_points() for its elevation-scaled value; park/landmark keep
    the flat REMOTE_POINTS value they always had."""
    lat, lon = float(row["lat"]), float(row["lon"])
    if _in_city_limits(lat, lon, buckets):
        return IN_CITY_POINTS, "in_city"
    if row["ref_type"] == "summit":
        elev_s = row.get("elevation_ft")
        elevation_ft = float(elev_s) if elev_s not in (None, "") else None
        return _summit_points(elevation_ft), "remote_scaled"
    return REMOTE_POINTS[row["ref_type"]], "remote"


def merge(inputs: list, out_path: str, places_csv: str = _DEFAULT_PLACES_CSV) -> None:
    buckets = _load_city_anchors(places_csv)
    seen = set()
    total = 0
    counts = {}
    dist = {}  # (ref_type, points, reason) -> count, for the distribution report
    with open(out_path, "w", newline="", encoding="utf-8") as out:
        w = csv.writer(out)
        w.writerow(FINAL_SEED_FIELDS)
        for path in inputs:
            with open(path, encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    key = (row["ref_type"], row["ref_code"])
                    if key in seen:
                        continue
                    seen.add(key)
                    points, reason = score_points(row, buckets)
                    row["points"] = points
                    out_row = [row[f] for f in SEED_FIELDS] + [reason]
                    w.writerow(out_row)
                    total += 1
                    counts[row["ref_type"]] = counts.get(row["ref_type"], 0) + 1
                    dist_key = (row["ref_type"], points, reason)
                    dist[dist_key] = dist.get(dist_key, 0) + 1
    print(f"merge: {total} rows -> {out_path}", file=sys.stderr)
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}", file=sys.stderr)
    print("merge: points distribution (ref_type, points, reason):", file=sys.stderr)
    for (ref_type, points, reason), n in sorted(dist.items()):
        print(f"  {ref_type} {points} ({reason}): {n}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch-sota")
    p.add_argument("out")

    p = sub.add_parser("fetch-pota")
    p.add_argument("out")

    p = sub.add_parser("extract-landmarks")
    p.add_argument("pbf")
    p.add_argument("out")

    p = sub.add_parser("match-parks")
    p.add_argument("pota_csv")
    p.add_argument("out")

    p = sub.add_parser("fetch-padus-parks")
    p.add_argument("pota_csv")
    p.add_argument("out")

    p = sub.add_parser("merge")
    p.add_argument("inputs", nargs="+")
    p.add_argument("--out", required=True)
    p.add_argument("--places-csv", default=_DEFAULT_PLACES_CSV,
                    help="Census place anchors (lat,lon,effective_radius_m) "
                         "for city-limits scoring; default app/reference/places.csv")

    args = ap.parse_args()
    if args.cmd == "fetch-sota":
        fetch_sota(args.out)
    elif args.cmd == "fetch-pota":
        fetch_pota(args.out)
    elif args.cmd == "extract-landmarks":
        extract_landmarks(args.pbf, args.out)
    elif args.cmd == "match-parks":
        match_parks(args.pota_csv, args.out)
    elif args.cmd == "fetch-padus-parks":
        fetch_padus_parks(args.pota_csv, args.out)
    elif args.cmd == "merge":
        merge(args.inputs, args.out, args.places_csv)


if __name__ == "__main__":
    main()
