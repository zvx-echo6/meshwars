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
  4c. extract-osm-parks -- on navi, needs the OSM planet's leisure=park
                          and boundary=protected_area ways/relations
                          already extracted to a GeoJSONSeq (osmium
                          export), plus extract-landmarks' and
                          fetch-padus-parks'/fetch-pota's own outputs to
                          dedup against. ADDED 2026-09-07, worldwide
                          expansion: PAD-US (4b) is deliberately US-only
                          (a US government dataset with no global
                          equivalent), so outside the US "local park"
                          coverage was zero -- this is OSM's answer to
                          the same "Twin Falls has no parks" complaint
                          that motivated 4b, everywhere PAD-US cannot
                          reach. See "PARK SOURCES" below.
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
  OSM landmarks -- /mnt/nas/nav/planet-latest.osm.pbf on pi-nas
                   (read-only source storage -- never write there),
                   reachable from navi. CHANGED 2026-09-07: was
                   western-us-11states.osm.pbf until the worldwide
                   expansion below switched the source to the full
                   planet extract.
  OSM parks     -- ADDED 2026-09-07, same planet extract as OSM
                   landmarks: every named leisure=park or
                   boundary=protected_area way/relation on Earth
                   (osmium tags-filter `wr/leisure=park`,
                   `wr/boundary=protected_area`), pre-exported to a
                   GeoJSONSeq with equal-area area_m2 already computed
                   per feature -- extract_osm_parks() reads that
                   extract directly rather than the planet PBF itself.
                   See "PARK SOURCES" below for why this exists
                   alongside PAD-US rather than instead of it, and how
                   it avoids double-counting a park PAD-US or POTA
                   already carries.
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

OSM PARKS (extract_osm_parks() added 2026-09-07, worldwide expansion):
PAD-US is deliberately US-only (a US government dataset with no global
equivalent -- see WORLDWIDE EXPANSION below), so fetch_padus_parks()'s
local/city/county park density above never reached anywhere outside
the US: the exact "Twin Falls has no parks" complaint, unsolved for
every non-US town. OpenStreetMap tags ordinary municipal parks
(leisure=park) and protected areas (boundary=protected_area) worldwide,
so extract_osm_parks() pulls those as a third, independent park source
-- the global answer to what fetch_padus_parks() already is for the
US. Three things keep it from double-counting a park one of the other
two sources -- or itself -- already carries:

  - leisure=nature_reserve overlap with the landmark tier: a nature
    reserve small enough to clear extract_landmarks()'s own
    SQUARE_AREA_M2 gate is already written there (see LANDMARK_TAGS'
    own comment on that tag); the OSM tags-filter that produced this
    stage's GeoJSONSeq input has no size gate at all, so the identical
    way/relation (same osm_type+osm_id) shows up in both extracts for
    every small reserve. extract_osm_parks() reads extract_landmarks()'s
    own output CSV first and skips any osm_type+osm_id already claimed
    there, so a small reserve stays a landmark (its correct tier) and
    only a reserve too large for that tier ever becomes a park.
  - Overlap with POTA/PAD-US: every candidate is checked by name +
    proximity against both fetch_pota()'s and fetch_padus_parks()'s own
    output (the same Jaccard name-overlap technique fetch_padus_parks()
    already uses to dedup against POTA, extended to test containment
    against the candidate's own full geometry rather than a fixed
    distance -- see the "BUG FOUND AND FIXED" comment at this stage's
    own dedup call site for why) before being kept -- "Ann Morrison
    Park" mapped in both OSM and PAD-US must not become two rows. RUN
    GLOBALLY, not gated to the US play-area bbox: PAD-US really is
    US-only, so a bbox gate is harmless for that half, but POTA is
    WORLDWIDE (see this module's own "WORLDWIDE EXPANSION" note) --
    fetch_pota() lists national parks and reserves on every continent.
    A first cut of this stage gated the whole check to the US bbox on
    the mistaken assumption that "no POTA or PAD-US row exists outside
    the US" -- true for PAD-US, false for POTA -- and shipped 10,752
    same-name-within-50km OSM/POTA collisions worldwide (Serengeti,
    Kakadu, Fiordland, and thousands more) before this was caught and
    fixed to run everywhere.
  - OSM-against-ITSELF (added 2026-09-08): neither rule above ever
    compares one OSM candidate to another. A same-exact-name-within-2km
    proximity measurement over the finished worldwide park set, run
    after the worldwide rebuild above had already passed every
    correctness gate, found 32,085 such pairs total -- 31,052 of them
    OSM-against-OSM (541 PAD-US-against-itself, 369 cross-source,
    the last of those being the "Overlap with POTA/PAD-US" rule above
    doing its job, not a defect). OSM commonly maps the same real park
    twice: once as a way and once as a relation, or as two overlapping
    way fragments of one boundary. _osm_self_dedup_key()/
    _dedup_osm_self_group() (defined just above extract_osm_parks())
    group surviving candidates by EXACT normalized name -- deliberately
    not fuzzy/substring matching; see _osm_self_dedup_key()'s own
    comment for why -- and within each name group keep only the
    largest-by-area candidate(s) more than 2 km apart, greedy
    largest-first, the same strategy build_places_osm_anchors.py already
    uses to dedup city anchors. A feature with no name never reaches
    this pass at all (dropped earlier, at the no_name filter below), so
    unlike the bug caught mid-flight on the anchor build, there is no
    blank-name bucket for it to wrongly collapse.

Kept a named destination the same way OSM landmarks are (a feature with
no `name` tag is not a place to send anyone to). No acreage floor and
no separate utility-parcel/school exclusion list of its own --
_compile_exclude_park_name_re() (the same predicate
fetch_padus_parks() uses) is reused as-is, since OSM's own leisure=park
tagging sweeps in the identical false positives PAD-US's LP designation
does (school playgrounds tagged as a park, community gardens) and the
fix for one dataset is the fix for the other.

A geometry this large a source can range from a city block to
Papahānaumokuākea Marine National Monument (1.5 million km^2,
confirmed the single largest feature in the raw extract) --
app/places_seed.py's _park_cells() walks a stored geometry's own
bounding box at 300m grid resolution with no size guard of its own, so
shipping that boundary unclipped would try to materialize on the order
of ten billion grid cells for one row. extract_osm_parks() clips every
matched boundary to a ~6km buffer around its own centroid before
storage, exactly like match_parks()'s own clip and for the identical
reason (see that clip's own comment) -- computed AFTER
_frac_area_outside_city() has already measured the real, full,
pre-clip shape, so the clip cannot affect which rate the park scores.

PLAY AREA (from the running service's /config, NOT app/config.py's
narrower Idaho-only defaults -- production overrides those via .env):
  north 49.29  south 25.8  west -125.0  east -93.5
  NORTH/SOUTH/WEST/EAST below are this rectangle -- still used to scope
  the PAD-US stages (match_parks, fetch_padus_parks), which stay
  US-only on purpose (see WORLDWIDE EXPANSION just below). No longer
  used to gate SOTA, POTA, or OSM landmarks.

WORLDWIDE EXPANSION (2026-09-07, Matt approved): fetch_sota(),
fetch_pota(), and extract_landmarks() no longer bbox-filter to the play
area above -- each source's own quality filter (SUMMIT_MIN_SOTA_POINTS,
POTA's active flag, LANDMARK_TAGS + the name requirement) is
unchanged and is the only gate left. app/places_seed.py's loader lost
its matching country filters (the US_SOTA_ASSOCIATIONS allowlist for
summits, the "ref_code prefix must be US-" check for POTA parks) in the
same change -- see that module's docstring. PAD-US (match_parks,
fetch_padus_parks) is deliberately UNCHANGED and stays US-only: it is a
US government dataset with no global equivalent, so it keeps
contributing US parks exactly as before, just alongside POTA/OSM
sources that are worldwide now rather than being the only park source
outside the US. "City limits" scoring (score_points() below) needed its
own worldwide fix on the anchors side -- see app/reference/places.csv
and the GeoNames-derived global anchors file assembled alongside it,
not part of this script.

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

A PARK WITH A MATCHED BOUNDARY does not use that point test at all
(added 2026-09-07, "Yosemite pays the same as a pocket park" -- see
PARK_REMOTE_AREA_FRAC's own comment above match_parks()): one point
cannot speak for a polygon that can be thousands of times the area of
the circle it happens to land in or out of. match_parks() and
fetch_padus_parks() instead measure what fraction of the park's own
FULL boundary (before match_parks()'s 6 km storage clip) lies outside
every nearby anchor's circle, and score_points() reads that fraction
straight off the row (`area_frac_outside`) -- more than half outside is
`remote_by_area`, otherwise `in_city_by_area`. A park match_parks() left
unmatched has no boundary to measure and falls back to the point test
above, same as every landmark (which never has a boundary at all).

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
import gzip
import io
import json
import math
import os
import re
import sys
import unicodedata
import urllib.request

# Python's csv module defaults to a 131072-byte field-size limit --
# fine for every column here except `geom` (a simplified WKT string),
# which can still exceed that for a large, multi-part OSM relation
# (a marine protected area's MultiPolygon spanning many islands) even
# after extract_osm_parks()'s own storage clip and simplify(). merge()
# is the only stage that ever reads a previously-written `geom` column
# back in (every other stage only writes one), and it is the first
# stage to read the real, worldwide OSM-parks output -- this raised
# _csv.Error: field larger than field limit (131072) partway through a
# real merge() run against it. 10,000,000 chars is generously above
# anything this pipeline produces (a fixed value, not sys.maxsize,
# which can raise OverflowError against the csv module's underlying C
# long on some platforms).
csv.field_size_limit(10_000_000)

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
    "area_m2", "geom", "elevation_ft", "area_frac_outside",
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
# area_frac_outside (added 2026-09-07, see PARK_REMOTE_AREA_FRAC above
# match_parks()) is the fraction (0.0-1.0) of a matched park's own true,
# pre-clip boundary area that lies outside every nearby Census place's
# circle -- populated only by match_parks() and fetch_padus_parks(),
# the two stages that ever attach a boundary to a park, both computed
# BEFORE any storage clip so the number reflects the park's real shape.
# score_points() uses it in place of the point-based _in_city_limits
# test for any park that has one; every other stage (fetch_sota,
# fetch_pota, extract_landmarks) and any park match_parks() left
# unmatched write "" for it, same convention as elevation_ft, and
# score_points() falls back to the point test for those.
#
# The merged, final seed adds one column beyond SEED_FIELDS:
# points_reason ("in_city" / "remote" / "remote_scaled" / "in_city_by_
# area" / "remote_by_area") records WHY a row got the points value it
# did -- not read by app/places_seed.py's
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
            # WORLDWIDE (2026-09-07): the bbox check that used to sit here
            # (in_bbox(lat, lon)) restricted this pull to the play area's
            # old North-America-only rectangle. Matt approved expanding
            # "Places Worth Going" worldwide -- SUMMIT_MIN_SOTA_POINTS
            # (the quality bar) stays exactly as-is; only the geography
            # gate is gone. app/places_seed.py's loader dropped the
            # matching US_SOTA_ASSOCIATIONS allowlist in the same change,
            # so a summit kept here is no longer re-filtered by country
            # at load time either.
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
                        POINTS["summit"], "SOTA", "", "", elevation_ft, ""])
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
            # WORLDWIDE (2026-09-07): bbox gate removed, same reasoning as
            # fetch_sota() above -- the active-parks filter (the check
            # just above) is the quality bar and is unchanged; only
            # geography opened up. app/places_seed.py's loader dropped
            # its POTA "prefix must be US-" check in the same change.
            ref = row["reference"].strip()
            name = row["name"].strip()
            if not ref or not name:
                continue
            w.writerow([ref, name, f"{lat:.6f}", f"{lon:.6f}"])
            kept += 1
    print(f"pota: wrote {kept} active parks worldwide -> {out_path}", file=sys.stderr)


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
            /mnt/nas/nav/planet-latest.osm.pbf \\
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
            # WORLDWIDE (2026-09-07): in_bbox(lat, lon) removed here --
            # this handler now runs against the planet PBF, not the
            # western-US extract, and every named match is kept
            # regardless of where on Earth it falls. See the module
            # docstring's fetch_sota/fetch_pota notes for the same
            # change on the other two sources.
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
            # WORLDWIDE (2026-09-07): same in_bbox removal as node() above.
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
                        POINTS["landmark"], "OSM", "", "", "", ""])
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


# --------------------------------------------------------------------
# PARK-SIZE SCORING (added 2026-09-07, "Yosemite pays the same as a
# pocket park"): a park's matched boundary can be enormous (a national
# park or forest) or tiny (a city block), and the in-city test used to
# ask only whether ONE point -- the row's own lat/lon -- fell inside a
# nearby Census place's circle (see _in_city_limits below). For a
# point-sized landmark that is the whole test there is to make. For a
# large polygon it is meaningless: one arbitrary interior point decided
# the rate for the WHOLE park, so a park whose centre point happened to
# land inside some small nearby town's circle scored the flat in-city
# rate no matter how much of the rest of it was nowhere near a town at
# all. Real damage this did in the shipped seed: Yosemite National Park
# (3,006.6 km^2), Flathead National Forest (13,613.9 km^2), Humboldt-
# Toiyabe National Forest (12,976.0 km^2), Payette National Forest
# (9,256.2 km^2), and Sequoia National Forest (4,512.0 km^2) all scored
# the in-city rate this way, while Rocky Flats National Wildlife Refuge
# next door to Rocky Mountain Arsenal scored the full remote rate only
# because its centre point happened to miss a circle.
#
# The fix below asks about the WHOLE park instead of one point: for a
# park with a matched boundary, compute what fraction of its own true
# area (the full PAD-US polygon, BEFORE match_parks()'s 6 km storage
# clip further down -- see that clip's own comment for why the stored
# `geom` cannot be used for this) lies outside every nearby Census
# place's circle. This is deliberately NOT a size threshold -- Matt was
# explicit that a park is not worth more for being big, only for being
# hard to reach, which is exactly why a large park that sits entirely
# inside a city (Golden Gate Park, Central Park) must still score the
# in-city rate. It is a truer version of the same in-city test the
# point version was already trying to do, extended to ask it of the
# park's whole shape rather than one arbitrary point on it.
#
# PARK_REMOTE_AREA_FRAC = 0.5: "more than half of the park's own area
# lies outside every nearby town's circle" is the plain reading of
# "substantially outside", and the obvious starting point. It was not
# tuned against the named cases above to force a particular answer --
# none of them needed tuning: the five national forests/park are all
# measured well past 90% outside (each is many times the area of the
# small town whose circle happened to brush their old centre point),
# a large park entirely inside a city measures close to 0% outside,
# and Rocky Mountain Arsenal National Wildlife Refuge -- genuinely
# up against the edge of Denver's circle -- is reported with its own
# measured fraction wherever that lands, not adjusted to land anywhere
# in particular. See this change's own commit message for that number.
PARK_REMOTE_AREA_FRAC = 0.5

# Diagnostic counters only -- never read by scoring logic itself, just
# by callers (the recompute pass, tests) that want to know how often
# _frac_area_outside_city had to repair an invalid geometry or, worse,
# still hit a GEOSException after repairing -- see that function's own
# "INVALID GEOMETRY" comment. Single-element lists so a caller that
# imports these by name still sees future mutations (a plain int would
# be re-bound, not shared).
GEOS_REPAIR_COUNT = [0]
GEOS_FALLBACK_COUNT = [0]


# CLIPPED-GEOMETRY GUARD (added 2026-09-07, "Humboldt-Toiyabe National
# Forest went 25 -> 5 on a re-rate pass"): match_parks() stores geom
# clipped to a ~6km buffer around the park's own point (see that clip's
# own comment) -- fine for the geometry's original purpose (map
# display near where someone actually stood), fatal if fed back into
# _frac_area_outside_city as if it were the whole park. A park's own
# `area_m2` column is untouched by the clip (it comes straight from
# PAD-US's GIS_Acres, not from the stored geometry), so a clipped row
# is caught by comparing the two: if the row's real area is far bigger
# than what its stored geometry's own bounding box could possibly
# contain, that geometry cannot be the whole park and must not be
# trusted for this test. Ratio 1.5x is deliberately loose (a real,
# unclipped, irregularly-shaped park's bbox is often 1.2-1.4x its own
# area already, from the bbox padding around a non-rectangular shape)
# -- this is a clipped/not-clipped gate, not a shape-tightness
# measurement. The 20 km^2 floor exists so an honestly small, honestly
# irregular park's bbox padding alone can't trip it.
CLIPPED_GEOM_AREA_RATIO = 1.5
CLIPPED_GEOM_MIN_AREA_M2 = 20e6  # 20 km^2


class ClippedGeometryError(ValueError):
    """Raised by _frac_area_outside_city when true_area_m2 says the
    geometry it was handed cannot be the whole park -- see
    CLIPPED_GEOM_AREA_RATIO's own comment above."""


def _anchors_near_bbox(minlon: float, minlat: float, maxlon: float, maxlat: float,
                        buckets: dict) -> list:
    """All (lat, lon, radius_m) anchors from _load_city_anchors's bucket
    index whose bucket could possibly reach into [minlon,minlat,maxlon,
    maxlat] -- every bucket the bbox touches, expanded by one bucket in
    each direction (the same margin _in_city_limits's 3x3 neighbourhood
    uses around a single point, generalized to a bbox that can itself
    span several buckets for a large park). _ANCHOR_BUCKET_DEG (1.0
    degree) is sized bigger than the largest anchor radius (~0.35
    degrees at this play area's latitudes -- see that constant's own
    comment), so this one-bucket margin cannot miss a real anchor whose
    circle reaches the bbox."""
    lat_lo = math.floor(minlat / _ANCHOR_BUCKET_DEG) - 1
    lat_hi = math.floor(maxlat / _ANCHOR_BUCKET_DEG) + 1
    lon_lo = math.floor(minlon / _ANCHOR_BUCKET_DEG) - 1
    lon_hi = math.floor(maxlon / _ANCHOR_BUCKET_DEG) + 1
    out = []
    for la in range(lat_lo, lat_hi + 1):
        for lo in range(lon_lo, lon_hi + 1):
            out.extend(buckets.get((la, lo), ()))
    return out


def _local_aeqd_transform(lon0: float, lat0: float):
    """An azimuthal-equidistant projection centred on (lon0, lat0),
    transforming WGS84 lon/lat into local metres -- true circles in
    real metres around a town anchor, not the fixed-degree-buffer
    approximation the rest of this pipeline uses for short distances
    (e.g. match_parks()'s own 6 km clip below). A park's area can span
    tens of kilometres across latitudes from the Pacific coast to the
    Rockies, so a single flat degrees-to-metres factor is not accurate
    enough here the way it is for a small fixed buffer."""
    from osgeo import osr

    src = osr.SpatialReference()
    src.ImportFromEPSG(4326)
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    local = osr.SpatialReference()
    local.ImportFromProj4(
        f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +datum=WGS84 +units=m +no_defs"
    )
    return osr.CoordinateTransformation(src, local)


def _bbox_anchor_shortcut(minlon: float, minlat: float, maxlon: float, maxlat: float,
                           anchors: list) -> float | None:
    """Cheap pre-check against a park's plain lon/lat bounding box,
    before _frac_area_outside_city pays for a real AEQD projection plus
    a shapely union/intersection over its (sometimes very large,
    sometimes just plain invalid -- see that function's own comment)
    polygon. Two cases need no geometry work at all to answer
    correctly, using nothing but haversine distance:

      - The bbox is entirely within a SINGLE anchor's circle (every
        corner within its radius) -- the polygon it bounds is a subset
        of the bbox, so it is entirely inside that circle too. Fully
        covered, fraction 0.0.
      - EVERY anchor's circle falls short of the bbox entirely (not
        even the nearest point on the bbox to that anchor is within
        its radius) -- the polygon cannot touch any of them either.
        Fully outside, fraction 1.0.

    Anything else -- some anchor's circle clips the bbox but does not
    swallow it whole -- is genuinely ambiguous without looking at the
    real shape, so this returns None and the caller falls through to
    the full computation. Corner/nearest-point distances use plain
    haversine on the lon/lat box, not a geodesically exact rectangle
    distance -- an approximation, like the rest of this pipeline's
    short-range distance math, but a safe one here: it only ever
    short-circuits the two unambiguous cases above, never invents a
    fraction in between."""
    corners = [(minlat, minlon), (minlat, maxlon), (maxlat, minlon), (maxlat, maxlon)]
    any_anchor_reaches = False
    for a_lat, a_lon, radius_m in anchors:
        farthest = max(_haversine_m(a_lat, a_lon, clat, clon) for clat, clon in corners)
        if farthest <= radius_m:
            return 0.0
        near_lat = min(max(a_lat, minlat), maxlat)
        near_lon = min(max(a_lon, minlon), maxlon)
        if _haversine_m(a_lat, a_lon, near_lat, near_lon) <= radius_m:
            any_anchor_reaches = True
    return None if any_anchor_reaches else 1.0


def _frac_area_outside_city(geom, buckets: dict, true_area_m2: float | None = None) -> float:
    """Fraction (0.0-1.0) of geom's own area that lies outside every
    nearby Census-place circle from app/reference/places.csv -- see
    PARK_REMOTE_AREA_FRAC above for what this feeds into. geom must be
    the park's real, full boundary (pre-clip) -- see match_parks()'s
    own 6 km clip comment for why a clipped geometry cannot be used
    here.

    true_area_m2 (optional -- match_parks()/fetch_padus_parks() never
    need to pass it, since they call this on the full polygon before
    it is ever clipped) is the row's own independently-known real area
    (PAD-US's GIS_Acres, unaffected by any storage clip). When given,
    this refuses to answer at all -- raising ClippedGeometryError
    rather than a confident, wrong fraction -- if geom's own bounding
    box could not possibly contain an area that large: see
    CLIPPED_GEOM_AREA_RATIO's own comment for why and the exact test.
    This is what stands between a caller that (against this function's
    own advice above) feeds it a clipped `geom` column read back out of
    the shipped CSV, and a repeat of the Humboldt-Toiyabe National
    Forest failure -- a 12,976 km^2 forest whose clipped ~80 km^2
    storage window happened to sit inside a nearby city's circle,
    scoring the whole forest in-city on nothing but that fragment.

    _bbox_anchor_shortcut above answers most rows for the cost of a
    few haversine calls -- deep backcountry (no anchor in reach at
    all) and a park that sits nowhere near a circle's edge both need
    no real geometry work. Only a park whose bbox genuinely straddles
    an anchor circle's boundary falls through to the rest of this
    function: project geom and every candidate anchor into a local
    azimuthal-equidistant plane centred on geom's own centroid (see
    _local_aeqd_transform) so each anchor's circle is a true circle in
    real metres, then measure how much of geom's area that union of
    circles fails to cover. No nearby anchor at all (deep backcountry,
    nothing in reach) counts as the whole park being outside -- same as
    _in_city_limits returning False when it finds nothing in range.

    INVALID GEOMETRY (added 2026-09-07, "one malformed PAD-US polygon
    killed a 77,000-row re-rate pass at row 30,000"): a park's stored
    boundary can be self-intersecting -- either inherited from PAD-US
    itself, or introduced by match_parks()'s own simplify() despite
    preserve_topology=True, which does not guarantee validity, only
    that it tries to preserve it. The reprojection into local AEQD
    coordinates can carry that invalidity (or introduce fresh
    self-intersections of its own from floating-point drift) into
    geom_local, and shapely's intersection() raises GEOSException
    ("side location conflict") rather than returning a wrong answer
    when it hits one. shapely.make_valid() repairs both operands
    before the intersection runs; on the rare case where GEOS still
    can't resolve it, this falls back to the same point-in-circle test
    _in_city_limits uses everywhere else, applied to geom's own
    centroid -- fully in (0.0) or fully out (1.0), never a real
    fraction, since there is no reliable area left to measure. That
    fallback is logged by score_points()'s own caller in the recompute
    pass, keyed by ref_code -- this function has no ref_code to name."""
    import shapely
    from shapely.errors import GEOSException
    from shapely.ops import transform as shapely_transform

    minlon, minlat, maxlon, maxlat = geom.bounds

    if true_area_m2 is not None and true_area_m2 > CLIPPED_GEOM_MIN_AREA_M2:
        bbox_area_m2 = _bbox_area_m2([minlat, maxlat], [minlon, maxlon])
        if true_area_m2 > CLIPPED_GEOM_AREA_RATIO * bbox_area_m2:
            raise ClippedGeometryError(
                f"geom bbox ({bbox_area_m2/1e6:.1f} km^2) cannot hold a "
                f"{true_area_m2/1e6:.1f} km^2 park -- this geometry is a "
                "storage clip, not the whole boundary"
            )

    anchors = _anchors_near_bbox(minlon, minlat, maxlon, maxlat, buckets)
    if not anchors:
        return 1.0

    shortcut = _bbox_anchor_shortcut(minlon, minlat, maxlon, maxlat, anchors)
    if shortcut is not None:
        return shortcut

    centroid = geom.centroid
    xform = _local_aeqd_transform(centroid.x, centroid.y)

    def _proj(x, y, z=None):
        px, py, _ = xform.TransformPoint(x, y)
        return (px, py)

    geom_local = shapely_transform(_proj, geom)
    if not geom_local.is_valid:
        geom_local = shapely.make_valid(geom_local)
        GEOS_REPAIR_COUNT[0] += 1
    area = geom_local.area
    if area <= 0:
        return 0.0

    circles = [shapely.Point(_proj(lon, lat)).buffer(radius_m, quad_segs=32)
               for lat, lon, radius_m in anchors]
    covered = shapely.unary_union(circles)
    if not covered.is_valid:
        covered = shapely.make_valid(covered)
        GEOS_REPAIR_COUNT[0] += 1

    try:
        inside = geom_local.intersection(covered).area
    except GEOSException:
        GEOS_FALLBACK_COUNT[0] += 1
        for a_lat, a_lon, radius_m in anchors:
            if _haversine_m(centroid.y, centroid.x, a_lat, a_lon) <= radius_m:
                return 0.0
        return 1.0
    return max(0.0, min(1.0, 1.0 - inside / area))


# --------------------------------------------------------------------
# BOUNDARY SANITY CHECK (added 2026-09-07, "a roadside museum does not
# have a 21,000 km^2 boundary"): match_parks()'s accept rule below
# takes ANY shared name token as enough to win, as long as the
# candidate polygon CONTAINS the POTA point -- deliberately lenient, so
# a park would rather get an approximate boundary than none at all.
# That lets a park's centre point, purely by chance, land inside some
# huge, genuinely-named PAD-US polygon (a big multi-county wildlife
# management area, say) that happens to share one common word with the
# POTA park's own name and nothing more. Confirmed against the shipped
# seed: "Oklahoma Route 66 Museum State Historic Site" (US-8644) and
# "Cherokee Hills Scenic Byway Scenic Site" (US-11864) both picked up
# 18,000-21,000 km^2 boundaries this way -- the latter matched to the
# exact same polygon as a real, correctly-matched "Cherokee Wildlife
# Management Area" (US-6344, identical area_m2), and scores only 0.25
# on the very same name-token test match_parks() itself uses right
# below -- "cherokee" is the only word its own four tokens
# (hills/scenic/byway are not stopwords) share with that polygon's.
#
# The gate below rejects a match only when BOTH the area is implausible
# for a real boundary AND the name evidence behind it was weak -- never
# area alone, which would risk rejecting a genuinely enormous,
# well-named match (Flathead National Forest's own 13,613.9 km^2
# boundary, or a Wetland Management District's real multi-parcel bundle
# covering an entire state, scores 1.0 on this same test and clears
# MATCH_AREA_SANITY_MIN_SCORE by a wide margin regardless of size).
# MATCH_AREA_SANITY_CEILING_M2 (15,000 km^2) sits with headroom above
# the largest legitimate strongly-named match measured in the shipped
# seed (Flathead, 13,613.9 km^2) and below both confirmed bad matches
# (18,034.7 / 21,176.2 km^2) -- a backstop, not the primary signal,
# since the score requirement alone already protects every legitimate
# large match seen in this data.
MATCH_AREA_SANITY_CEILING_M2 = 1.5e10  # 15,000 km^2
MATCH_AREA_SANITY_MIN_SCORE = 0.5


def _match_passes_sanity_check(area_m2: float, name_score: float) -> bool:
    """False if a match should be rejected as wildly out of scale for
    how weak its name evidence was -- see MATCH_AREA_SANITY_CEILING_M2's
    own comment above. A pulled-out function so this gate is testable on
    its own, without needing PAD-US/GDAL to reach it."""
    return not (area_m2 > MATCH_AREA_SANITY_CEILING_M2 and name_score < MATCH_AREA_SANITY_MIN_SCORE)


# --------------------------------------------------------------------
# BOUNDARY CLEANUP, PASS 2 (added 2026-09-07, continuing the sanity
# check above): _match_passes_sanity_check only ever sees ONE candidate
# at a time, so it cannot catch the dominant failure mode -- a small,
# specifically-named feature (a fish hatchery, a natural bridge) whose
# accepted match is actually the CONTAINING unit's own boundary
# (a national forest, a wildlife refuge complex), byte-identical to
# whatever legitimate row separately matched the very same polygon
# under its own name. Confirmed shipped-seed case: "Tonto State Fish
# Hatchery" and "Tonto Natural Bridge State Park" both carry Tonto
# National Forest's own 11,601.6 km^2 -- neither is a national forest,
# and neither is anywhere near that size in reality (a hatchery is a
# building complex; the natural bridge park is well under 10 km^2).
#
# Three checks run over the WHOLE set of matched rows, after
# match_parks()'s per-candidate loop and per-candidate sanity check --
# see _clean_matched_park_boundaries() below for how they combine:
#
#   1. DUPLICATE GEOMETRY (_resolve_duplicate_boundary_group): when two
#      or more matched parks carry the identical PAD-US area (to the
#      nearest m^2 -- area_m2 comes straight from PAD-US's own
#      GIS_Acres field, so an exact match this precise is not
#      coincidence -- it is the same polygon), at most one of them can
#      really own it. Ranked by _is_big_scale_designation(): a name
#      carrying an unmistakably large-format designation (National
#      Forest/Park/Monument/Grassland, Wilderness, National Conservation
#      or Recreation Area) outranks everything else, and a point-scale
#      name (_is_point_scale_designation() below) always loses. Exactly
#      one top-tier member wins the boundary; the rest are stripped.
#      Zero, or more than one, top-tier member (e.g. Teton Wilderness
#      Area and Jedediah Smith Wilderness Area sharing one 2,366.3 km^2
#      polygon -- both are a "Wilderness Area", so neither outranks the
#      other) -- there is no name evidence here strong enough to award
#      the boundary to any one of them, so EVERY member of the group
#      keeps the boundary. That is a deliberate fail-safe, not a guess:
#      point-in-polygon cannot break these ties either (a matched
#      park's point IS its boundary's centroid, so it is trivially
#      inside its own polygon no matter which member truly owns the
#      ground), and the two failure costs are not symmetric. Keeping
#      every tied member leaves co-located real places sharing one
#      credit zone -- minor, and reversible later with a PAD-US-backed
#      pass. Stripping every tied member would delete real, named
#      wilderness (or a real national forest, or both units of a real
#      national monument) from the board outright.
#
#   2. DESIGNATION-VS-SCALE (_is_point_scale_designation): reinforces
#      (1) and also catches a point-scale name that happens to be the
#      ONLY match on its polygon, with no duplicate to compare against
#      (nothing else in this seed shares San Bernard National Wildlife
#      Refuge's bogus 8,323.8 km^2, for instance, but that one is not a
#      point-scale designation either -- see
#      _KNOWN_BOGUS_BOUNDARY_MATCHES below for why it needs a named
#      exception instead). A museum, fish hatchery, natural bridge,
#      scenic byway/site, historic site, visitor/interpretive center,
#      picnic area, trailhead, or campground should never own a
#      boundary in the hundreds of km^2, let alone thousands --
#      POINT_SCALE_AREA_CEILING_M2 (500 km^2) sits comfortably above
#      Fort Sill National Historic Site's own legitimate 379.3 km^2
#      (an entire Army post, not a point at all) and below every
#      confirmed-bogus case measured in the shipped seed (816 km^2 and
#      up). This is a keyword-GATED ceiling, not a blanket one -- see
#      MATCH_AREA_SANITY_CEILING_M2's own comment above for why a
#      blanket ceiling cannot work in this data (a bogus museum match
#      can outsize a legitimate national forest).
#
#   3. ADMINISTRATIVE ENVELOPE (_is_administrative_envelope_designation):
#      a "Wetland Management District" boundary is genuine, not
#      inherited -- but it is a multi-county, sometimes multi-state
#      administrative footprint of scattered easement parcels, not
#      contiguous ground anyone walks onto. Crediting it under the
#      reachable-ring credit model (see app/places_seed.py) would credit
#      an entire district from one grid square. Stripped unconditionally
#      regardless of size or duplicate status. Individual "National
#      Waterfowl Production Area" units are NOT included here even
#      though the same federal program administers them -- every one of
#      those in this seed is already parcel-sized (under 30 km^2), so
#      there is nothing here to strip; the district-level name is what
#      bundles a whole state's scattered parcels into one polygon.
#
# All three passes are scoped to matched boundaries >=
# DUPLICATE_CLEANUP_MIN_AREA_M2 (100 km^2): below that, a mismatched or
# duplicate boundary's own credit-zone footprint is not the
# multi-county-scale problem this cleanup targets, and the dataset has
# thousands of small, harmless coincidental duplicates (two named
# features sharing one tiny parking-lot-sized polygon) that are not
# this failure mode at all -- rewriting all of them is out of scope for
# this pass. Every example named above already sits three to four
# orders of magnitude past this floor.
DUPLICATE_CLEANUP_MIN_AREA_M2 = 1e8  # 100 km^2
POINT_SCALE_AREA_CEILING_M2 = 5e8  # 500 km^2

_POINT_SCALE_DESIGNATION_RE = re.compile(
    r"museum|fish hatchery|natural bridge|scenic byway|scenic site|"
    r"historic site|visitor'?s? center|interpretive site|picnic area|"
    r"trailhead|campground",
    re.IGNORECASE,
)

_BIG_SCALE_DESIGNATION_RE = re.compile(
    r"national forest|national grassland|national historical park|"
    r"national park|national monument|wilderness area|\bwilderness\b|"
    r"national conservation area|national recreation area",
    re.IGNORECASE,
)

_ADMIN_ENVELOPE_DESIGNATION_RE = re.compile(
    r"wetland management district",
    re.IGNORECASE,
)


def _is_point_scale_designation(name: str) -> bool:
    """True if `name` carries a designation that should never own a
    multi-hundred-km^2 boundary -- see POINT_SCALE_AREA_CEILING_M2's own
    comment above for the "or similar" list and why 500 km^2 is the
    cutoff."""
    return bool(_POINT_SCALE_DESIGNATION_RE.search(name))


def _is_big_scale_designation(name: str) -> bool:
    """True if `name` carries a designation that legitimately can own a
    multi-thousand-km^2 boundary -- see _resolve_duplicate_boundary_group()'s
    own comment above for how this ranks a duplicate-geometry group."""
    return bool(_BIG_SCALE_DESIGNATION_RE.search(name))


def _is_administrative_envelope_designation(name: str) -> bool:
    """True if `name` is a multi-county/multi-state administrative
    footprint (a Wetland Management District) rather than contiguous
    ground -- see ADMINISTRATIVE ENVELOPE's own comment above."""
    return bool(_ADMIN_ENVELOPE_DESIGNATION_RE.search(name))


def _duplicate_boundary_tier(name: str) -> int:
    """Ranks one name's plausibility as the true owner of a
    duplicate-geometry group -- see _resolve_duplicate_boundary_group()'s
    own comment. 2: an unmistakably large-format designation (National
    Forest/Park/Monument/Grassland, Wilderness, National Conservation or
    Recreation Area) that legitimately can own a multi-thousand-km^2
    boundary. 0: a point-scale designation that never can. 1: everything
    else (a Wildlife Management Area, a National Wildlife Refuge, a BLM
    Recreation/Herd Management Area, ...) -- these vary enormously in
    real size and are neither confirmed-plausible nor confirmed-implausible
    from the name alone."""
    if _is_point_scale_designation(name):
        return 0
    if _is_big_scale_designation(name):
        return 2
    return 1


def _resolve_duplicate_boundary_group(names: list) -> list:
    """Given the names of every matched park row that shares one exact
    matched-boundary area, return a same-length list of which of them
    keeps the boundary -- see DUPLICATE GEOMETRY's own comment above for
    the ranking rule this implements. A pulled-out function so the rule
    is testable without needing PAD-US/GDAL to reach it.

    The winner is whichever member sits at the highest
    _duplicate_boundary_tier() in the group, but ONLY if it is alone
    there -- a tie at any tier (including two point-scale names tied at
    the bottom) means no member's own name gives enough evidence to
    award the boundary to any one of them, so EVERY member keeps it.
    Ties cannot be resolved without ground truth this seed does not
    carry (point-in-polygon can't help either -- a matched park's point
    IS its boundary's centroid, so it is trivially inside its own
    polygon regardless of which member actually owns the ground), and
    the two failure costs here are not symmetric: keeping every member
    leaves co-located real places sharing one credit zone, which is
    minor and reversible later with a PAD-US-backed pass, while
    stripping every member deletes real, named wilderness (or a real
    national forest, or both units of a real national monument) from
    the board outright. This is what keeps a legitimate ambiguous-tier
    match (a real, correctly-sized "Ruby Lake National Wildlife Refuge")
    from being dragged down just because ITS duplicate partner is a
    clearly bogus point-scale name ("Fort Ruby National Historic Site")
    that isn't itself tier 1 -- the refuge is the only tier-1 name in
    that group, so it wins outright even though "National Wildlife
    Refuge" alone is never treated as automatically big-scale."""
    if len(names) < 2:
        return [True] * len(names)
    tiers = [_duplicate_boundary_tier(n) for n in names]
    top = max(tiers)
    winners = [t == top for t in tiers]
    if sum(winners) == 1:
        return winners
    return [True] * len(names)


# Known-bad matches that neither generic check above can catch: no
# duplicate partner exists anywhere in this seed to compare against
# (DUPLICATE GEOMETRY has nothing to rank), and the designation itself
# is not inherently point-scale (DESIGNATION-VS-SCALE has no keyword to
# key on -- a "National Wildlife Refuge" legitimately spans thousands of
# km^2 for Desert, Cabeza Prieta, Charles M. Russell, and Sheldon NWRs,
# all confirmed present and legitimate in this same seed). Confirmed bad
# by direct, out-of-band verification against the real refuge, not by
# any rule this script can run on its own -- keyed by ref_code (POTA
# reference) rather than name, since that is the stable, unique key
# match_parks() writes to `ref_code`.
_KNOWN_BOGUS_BOUNDARY_MATCHES = {
    "US-0553": "San Bernard National Wildlife Refuge matched an 8,323.8 "
               "km^2 polygon; the real refuge is ~110 km^2.",
}


def _clean_matched_park_boundaries(matched_rows: list) -> dict:
    """Mutates `matched_rows` in place, clearing the boundary
    (area_m2/geom_wkt/area_frac_outside, all reset to "") on any row
    whose match fails DUPLICATE GEOMETRY, DESIGNATION-VS-SCALE,
    ADMINISTRATIVE ENVELOPE, or the named-exception list above -- see
    this section's own module comment for all four. Each row in
    `matched_rows` must be a dict with "name", "ref_code", "area_m2"
    (a float in m^2, or "" if unmatched), "geom_wkt", and
    "area_frac_outside" keys -- this is called both by match_parks()
    (a full rebuild) and by a standalone patch against the already-built
    seed (no GDAL/PAD-US needed, since every input here is a plain
    Python value already sitting in the CSV).

    Returns {ref_code: reason} for every row stripped, for reporting."""
    from collections import defaultdict

    groups = defaultdict(list)
    for i, row in enumerate(matched_rows):
        area = row["area_m2"]
        if area != "" and area >= DUPLICATE_CLEANUP_MIN_AREA_M2:
            groups[round(float(area))].append(i)

    stripped = {}
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        keep_flags = _resolve_duplicate_boundary_group([matched_rows[i]["name"] for i in idxs])
        for i, keep in zip(idxs, keep_flags):
            if not keep:
                stripped[matched_rows[i]["ref_code"]] = "duplicate geometry"

    for row in matched_rows:
        rc = row["ref_code"]
        area = row["area_m2"]
        if rc in stripped or area == "":
            continue
        area = float(area)
        if area >= POINT_SCALE_AREA_CEILING_M2 and _is_point_scale_designation(row["name"]):
            stripped[rc] = "point-scale designation"
        elif _is_administrative_envelope_designation(row["name"]):
            stripped[rc] = "administrative envelope"
        elif rc in _KNOWN_BOGUS_BOUNDARY_MATCHES:
            stripped[rc] = "known bad match: " + _KNOWN_BOGUS_BOUNDARY_MATCHES[rc]

    for row in matched_rows:
        if row["ref_code"] in stripped:
            row["area_m2"] = ""
            row["geom_wkt"] = ""
            row["area_frac_outside"] = ""

    return stripped


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

    Two checks run on top of that accept rule, both against the FULL
    matched polygon, before it gets clipped down for storage below:
    MATCH_AREA_SANITY_CEILING_M2/MATCH_AREA_SANITY_MIN_SCORE reject a
    match that is wildly out of scale for how weak its name evidence
    was (see that constant's own comment), and PARK_REMOTE_AREA_FRAC
    decides in-city vs. remote from the matched polygon's whole area
    rather than the POTA centre point alone (see that constant's own
    comment).
    """
    from osgeo import ogr, osr
    import shapely
    from shapely import wkb as shapely_wkb
    from shapely.strtree import STRtree

    buckets = _load_city_anchors(_DEFAULT_PLACES_CSV)

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

    with open(pota_csv, encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        matched = 0
        total = 0
        rejected_sanity = []
        # Buffered here (rather than written straight out row-by-row, as
        # every other stage does) so _clean_matched_park_boundaries() can
        # see the WHOLE set of matches at once -- its duplicate-geometry
        # check needs every row that might share one boundary before it
        # can tell which of them, if any, really owns it. See that
        # function's own module comment above _match_passes_sanity_check.
        out_rows = []
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
            frac_outside = ""
            if best is not None:
                g = geoms[best]
                area_m2 = areas[best] if areas[best] else g.area * (111320.0 ** 2) * abs(
                    __import__("math").cos(__import__("math").radians(lat)))
                # BOUNDARY SANITY CHECK -- see MATCH_AREA_SANITY_CEILING_M2's
                # own comment above. Reject the match (fall back to a
                # point place, same as never having matched at all)
                # rather than drop the place from the seed.
                if not _match_passes_sanity_check(area_m2, best_score):
                    rejected_sanity.append((name, area_m2 / 1e6, best_score))
                    best = None
                    area_m2 = ""
            if best is not None:
                matched += 1
                g = geoms[best]
                # PARK-SIZE SCORING -- see PARK_REMOTE_AREA_FRAC's own
                # comment above. Computed against the FULL matched
                # polygon `g`, before the clip just below shrinks it
                # down for storage -- a clipped geometry cannot answer
                # this test (see that clip's own comment).
                frac_outside = _frac_area_outside_city(g, buckets)
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
            out_rows.append({
                "ref_code": row["reference"], "name": name, "lat": lat, "lon": lon,
                "source": "POTA/PAD-US" if best is not None else "POTA",
                "area_m2": area_m2, "geom_wkt": geom_wkt, "area_frac_outside": frac_outside,
            })
        print(f"parks: {matched}/{total} matched a PAD-US boundary "
              f"({total - matched} unmatched, kept as points)", file=sys.stderr)
        if rejected_sanity:
            print(f"parks: rejected {len(rejected_sanity)} boundary match(es) as "
                  f"wildly out of scale (area > {MATCH_AREA_SANITY_CEILING_M2/1e6:.0f} km^2, "
                  f"name score < {MATCH_AREA_SANITY_MIN_SCORE}):", file=sys.stderr)
            for rname, rarea_km2, rscore in rejected_sanity:
                print(f"    {rarea_km2:12.1f} km^2  score={rscore:.3f}  {rname}", file=sys.stderr)

        cleaned = _clean_matched_park_boundaries(out_rows)
        if cleaned:
            print(f"parks: stripped {len(cleaned)} boundary match(es) in the "
                  "post-match cleanup pass (duplicate geometry / point-scale "
                  "designation / administrative envelope / known bad match):",
                  file=sys.stderr)
            by_ref = {r["ref_code"]: r for r in out_rows}
            for ref_code, reason in sorted(cleaned.items()):
                print(f"    {by_ref[ref_code]['name']} ({ref_code}): {reason}", file=sys.stderr)

    with open(out_path, "w", newline="", encoding="utf-8") as out:
        w = csv.writer(out)
        w.writerow(SEED_FIELDS)
        for r in out_rows:
            w.writerow(["park", r["ref_code"], r["name"], f"{r['lat']:.6f}", f"{r['lon']:.6f}",
                        POINTS["park"], r["source"],
                        f"{r['area_m2']:.0f}" if r["area_m2"] != "" else "", r["geom_wkt"], "",
                        f"{r['area_frac_outside']:.4f}" if r["area_frac_outside"] != "" else ""])


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
# LP designation also sweeps in even at a normal park's size -- and
# (added 2026-09-07) elementary-and-below campuses school districts
# register as their own local parks; see _ExcludeParkName below for the
# details and the DROP-before-KEEP ordering that keeps a junior high,
# senior high, or college on the board while an elementary comes off.
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


_UTILITY_EXCLUDE_RE_SRC = (
    r"community\s+garden|detention|retention\s*(basin|pond)?|stormwater|"
    r"water\s+tower|\btank\b|substation|lift\s+station|pump\s+station|"
    r"right.?of.?way|\beasement\b|parking\s+(lot|structure|garage)|"
    r"comfort\s+station|maintenance\s+(yard|facility|shop)"
)

# ELEMENTARY SCHOOL EXCLUSION (added 2026-09-07, Matt's decision;
# NARROWED same day after the reachable-ring credit change landed --
# see below) -- school districts register plenty of their own campuses
# as PAD-US "local parks" (Des_Tp LP/LREC; see the comment above
# MIN_PARK_ACRES). An elementary-and-below campus is not a destination
# worth sending a player to stand outside of with an antenna, so
# elementary, primary, grade-school, K-6/K-8, and pre-K/preschool
# campuses come off the board.
#
# Junior high, middle school, senior high, college, and university
# campuses STAY -- the original 2026-09-07 cut also dropped junior
# high/middle school, but Matt reversed that part the same day once
# the reachable-ring credit change (see app/places_seed.py's
# REACHABLE-RING CREDIT note) meant a school now credits from the
# public sidewalk outside its fence, not just from standing on campus.
# That removed the actual objection (sending a player onto school
# grounds) for every grade EXCEPT the youngest, where a lone kid-height
# fence line still isn't a place worth routing a game to. Confirmed
# case: "West Junior High School" (Boise, PADUS-84489) must survive
# this filter.
_SCHOOL_DROP_RE_SRC = (
    r"\belem(?:entary)?\b|primary\s+school|grade\s+school|"
    r"\bk-?[68]\b|preschool|pre-?k\b|intermediate\s+school"
)
_SCHOOL_KEEP_RE_SRC = (
    r"junior\s+high|jr\.?\s*high|\bjh\b|middle\s+school|"
    r"senior\s+high|high\s+school|\buniversity\b|\bcollege\b|"
    r"\binstitute\b|\bseminary\b"
)


class _ExcludeParkName:
    """Callable predicate, `.search(name)` (same interface as a
    compiled `re.Pattern`, so the one call site below does not need to
    know this isn't a single regex) -- True if a PAD-US local-park name
    should be dropped from the seed.

    Three buckets, checked in this exact ORDER (order matters -- see
    below):

      1. DROP: an elementary-and-below school name (_SCHOOL_DROP_RE).
      2. KEEP: a junior high, middle school, senior high, college,
         university, institute, or seminary (_SCHOOL_KEEP_RE) --
         explicit rather than relying on the default below, so the
         classification is legible and testable as three buckets, not
         two.
      3. Everything else falls through to the ORIGINAL utility-parcel
         exclusion (community garden, detention basin, water tower,
         substation, lift/pump station, right-of-way, easement, parking
         structure, maintenance yard) -- unrelated to schools, unchanged
         since before this addition.

    Anything none of the above classifies -- the common case -- is
    KEPT by default. That default matters: most names this cannot
    classify are real parks that merely happen to be NAMED after a
    school ("Blackwell School National Park", "Elgin School House State
    Park", "Galloway School Park") rather than being a school campus
    itself. Deleting a national park to remove an elementary school is a
    much worse error than the reverse.

    ORDER MATTERS between (1) and (2), even though the DROP and KEEP
    word lists no longer share an obvious substring the way the
    original (wider) cut did: a real combined campus can still be named
    something like "Lincoln Elementary and Middle School", which
    matches BOTH "elementary" (DROP) and "middle school" (KEEP) as
    substrings of the same name. DROP is tested first so a campus that
    is even PARTLY elementary-and-below still comes off the board,
    rather than a later-grade word in the same name accidentally
    rescuing it. (tests/test_build_places_seed.py pins this ordering
    down directly.)
    """

    def __init__(self):
        import re
        self._school_drop_re = re.compile(_SCHOOL_DROP_RE_SRC, re.IGNORECASE)
        self._school_keep_re = re.compile(_SCHOOL_KEEP_RE_SRC, re.IGNORECASE)
        self._utility_exclude_re = re.compile(_UTILITY_EXCLUDE_RE_SRC, re.IGNORECASE)

    def search(self, name: str) -> bool:
        if self._school_drop_re.search(name):
            return True
        if self._school_keep_re.search(name):
            return False
        return bool(self._utility_exclude_re.search(name))


def _compile_exclude_park_name_re():
    return _ExcludeParkName()


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
    buckets = _load_city_anchors(_DEFAULT_PLACES_CSV)

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
            # NOT removed by the 2026-09-07 worldwide change -- PAD-US is
            # a US-government dataset with no global equivalent (see
            # module docstring "PARK SOURCES"/task notes), so this bbox
            # check stays: it is what keeps this stage US-only on
            # purpose, not a leftover of the old play-area restriction.
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
            # PARK-SIZE SCORING -- see PARK_REMOTE_AREA_FRAC's own
            # comment above match_parks(). This stage never clips its
            # geometry (unlike match_parks()'s 6 km storage clip), so
            # `geom` here already is the full boundary the test needs.
            frac_outside = _frac_area_outside_city(geom, buckets)
            simplified = geom.simplify(0.0008, preserve_topology=True)
            w.writerow(["park", f"PADUS-{fid}", name, f"{lat:.6f}", f"{lon:.6f}",
                        POINTS["park"], "PAD-US", f"{area_m2:.0f}", simplified.wkt, "",
                        f"{frac_outside:.4f}"])
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
# Stage 4c: OSM parks (leisure=park, boundary=protected_area) --
# worldwide, run on navi -- see module docstring's "OSM PARKS" section
# for the full rationale, including the three dedup rules below (the
# self-dedup pass, added 2026-09-08, is the newest of the three).
# --------------------------------------------------------------------

# Storage clip radius for a matched boundary -- identical value and
# identical reasoning to match_parks()'s own 6 km clip (see that
# clip's own comment): _frac_area_outside_city() has already measured
# the real, full, pre-clip shape by the time this runs, so shrinking
# the STORED geometry afterward cannot change which rate the park
# scored.
_OSM_PARK_CLIP_DEG = 0.06


def _osm_self_dedup_key(name: str) -> str:
    """Exact-name grouping key for the OSM-vs-OSM self-dedup pass in
    extract_osm_parks() below (added 2026-09-08: a proximity measurement
    over the finished worldwide park set found 31,052 same-exact-name
    park pairs within 2 km, and 31,052 of those -- essentially all of
    them -- were OSM-against-OSM, not against POTA/PAD-US, which
    extract_osm_parks() already dedups against above. OSM frequently
    maps the same park twice: once as a way and once as a relation, or
    as two overlapping way fragments of one boundary; nothing before
    this pass ever compared one OSM candidate against another).

    NFKD-strip diacritics, lowercase, collapse punctuation/whitespace --
    the same normalization build_places_osm_anchors.py's normalize()
    uses for city anchors (see that function's own comment on why
    ascii-only would be wrong here too). Deliberately NOT _norm_name()'s
    token-set/Jaccard scorer a few hundred lines above -- that is a
    fuzzy "these are probably the same place" test, built for
    cross-source variants. This pass targets a narrower, higher-
    confidence case (the literal same object, mapped twice) and stays
    exact on purpose: the measurement also found 13,579 pairs where one
    name merely CONTAINS the other ("Kakadu Park" vs "Kakadu National
    Park World Heritage Site") -- some of those are genuinely distinct
    sub-units ("Monocacy National Battlefield - Best Farm"), so fuzzy or
    substring matching would delete real, distinct parks. That is a
    separate, harder problem, deliberately left alone here.
    """
    if not name:
        return ""
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower()
    n = re.sub(r"[^\w\s]", " ", n, flags=re.UNICODE)
    n = re.sub(r"\s+", " ", n).strip()
    return n


# Proximity threshold for the self-dedup pass -- 2 km, the same radius
# the measurement that found this defect used (31,052 exact-name pairs
# within 2 km, essentially all OSM-against-OSM). Deliberately much
# tighter than build_places_osm_anchors.py's 50 km DEDUP_RADIUS_KM for
# city anchors: a city name can legitimately repeat every few hundred
# km (a country's admin structure reuses "Springfield"/"Georgetown"
# sparsely), but two mappings of the SAME park -- one way, one
# relation, or two overlapping boundary fragments -- sit at the same
# coordinates or at most a few hundred metres apart (their centroids
# can drift a little if the fragments don't overlap exactly). A radius
# anywhere near 50 km would start merging distinct same-named parks in
# neighbouring towns -- there is more than one real "City Park" or
# "Riverside Park" in the US alone. 2 km comfortably covers the
# same-object case without reaching a second town.
_OSM_SELF_DEDUP_M = 2000.0


def _dedup_osm_self_group(rows):
    """Greedy largest-first within one exact-name group -- identical
    strategy to build_places_osm_anchors.py's own _dedup_group() (see
    that function's comment for why), not a transitive union-find over
    the proximity threshold: two same-named parks far enough apart to
    both legitimately survive can never get chained together through a
    third same-named park that happens to sit between them. "Largest"
    is the untouched props.area_m2 already computed for each candidate
    (pre storage-clip) -- a relation covering the whole park outranks a
    way covering one corner of it.
    """
    ordered = sorted(rows, key=lambda r: -r["area_m2"])
    kept = []
    for r in ordered:
        if not any(_haversine_m(r["lat"], r["lon"], k["lat"], k["lon"]) <= _OSM_SELF_DEDUP_M
                   for k in kept):
            kept.append(r)
    return kept


def extract_osm_parks(geojsonseq_path: str, landmarks_csv: str, pota_csv: str,
                       padus_csv: str, out_path: str) -> None:
    """Run on navi:  python3 build_places_seed.py extract-osm-parks \\
        parks.geojsonseq landmarks.csv pota.csv padus_parks.csv out.csv

    geojsonseq_path is a pre-filtered, pre-exported GeoJSONSeq (osmium
    tags-filter `wr/leisure=park`,`wr/boundary=protected_area` then
    `osmium export`) -- this function reads that extract directly, not
    the planet PBF itself (see module docstring's "OSM parks" SOURCES
    entry). Each feature's `properties.area_m2` is trusted as given
    (computed by the export in an equal-area projection) rather than
    re-derived from the lon/lat geometry the way fetch_padus_parks()
    has to for PAD-US's integer-acre GIS_Acres column -- there is no
    equivalent precision problem here to work around.

    landmarks_csv is extract_landmarks()'s own output, read ONLY to
    build a set of osm_type+osm_id keys already claimed by the landmark
    tier -- see "leisure=nature_reserve overlap" in the module
    docstring for why the exact same object can appear in both extracts
    and why the landmark tier always wins that overlap.

    pota_csv and padus_csv are fetch_pota()'s and fetch_padus_parks()'s
    own outputs, read ONLY to dedup against -- globally, not gated to
    the US play-area bbox, since fetch_pota() is worldwide -- see
    "Overlap with POTA/PAD-US" in the module docstring. Neither is
    read for its geometry (POTA carries none; PAD-US's own
    boundary is not needed here) -- name + point is all the dedup check
    uses, same as fetch_padus_parks()'s own dedup against POTA.
    """
    import shapely
    from shapely.geometry import shape as shapely_shape
    from shapely.strtree import STRtree

    exclude_re = _compile_exclude_park_name_re()
    buckets = _load_city_anchors(_DEFAULT_PLACES_CSV)

    landmark_osm_keys = set()
    with open(landmarks_csv, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            landmark_osm_keys.add(row["ref_code"])  # "n<id>" / "w<id>"

    dedup_pts = []
    dedup_names = []
    for src_csv in (pota_csv, padus_csv):
        with open(src_csv, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    lat = float(row["lat"])
                    lon = float(row["lon"])
                except (KeyError, ValueError):
                    continue
                dedup_pts.append(shapely.Point(lon, lat))
                dedup_names.append(row["name"])
    dedup_tree = STRtree(dedup_pts) if dedup_pts else None

    kept = 0
    larger = 0
    smaller = 0
    total = 0
    counts = {
        "no_name": 0, "dup_of_landmark": 0, "name_excluded": 0,
        "dup_of_us_park": 0, "bad_geom": 0, "dup_of_osm_self": 0,
    }

    # Every candidate that clears the per-row filters below is buffered
    # here rather than written immediately -- the OSM-vs-OSM self-dedup
    # pass after this loop (see module docstring's "OSM-against-ITSELF"
    # section and _dedup_osm_self_group()'s own comment) needs every
    # surviving candidate's name/location/area before it can decide
    # which of a same-named cluster to keep, so nothing can be written
    # until that pass has run. `idx` preserves the geojsonseq's own
    # ordering so the final CSV comes out in the same relative order it
    # would have without this pass, not grouped by name. 594,487
    # candidates survived the per-row filters on the full planet extract
    # (2026-09 run) -- comfortably bufferable in memory (the written CSV
    # itself is ~156 MB; navi has tens of GB free).
    candidates = []

    with open(geojsonseq_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            total += 1
            rec = json.loads(line)
            props = rec["properties"]
            name = (props.get("name") or "").strip()
            if not name:
                counts["no_name"] += 1
                continue

            osm_type = props.get("osm_type") or ""
            osm_id = props.get("osm_id")
            code = f"{osm_type[:1]}{osm_id}"
            if code in landmark_osm_keys:
                counts["dup_of_landmark"] += 1
                continue

            if exclude_re.search(name):
                counts["name_excluded"] += 1
                continue

            try:
                geom = shapely_shape(rec["geometry"])
                if not geom.is_valid:
                    geom = geom.buffer(0)
            except Exception:
                counts["bad_geom"] += 1
                continue
            if geom.is_empty:
                counts["bad_geom"] += 1
                continue

            centroid = geom.centroid
            lat, lon = centroid.y, centroid.x
            area_m2 = props.get("area_m2")
            if not area_m2:
                area_m2 = geom.area * (111_320.0 ** 2) * math.cos(math.radians(lat))

            # DEDUP AGAINST POTA/PAD-US -- see module docstring's "US
            # overlap" section. Run GLOBALLY, not bbox-gated: PAD-US
            # really is US-only, so gating on the US play-area bbox was
            # harmless for that half, but POTA is WORLDWIDE (see this
            # module's own "WORLDWIDE EXPANSION" note) -- fetch_pota()
            # lists national parks and reserves on every continent, not
            # just the US. Gating this check to the US bbox (the first
            # cut of this stage did exactly that) silently let every
            # non-US POTA park duplicate its OSM twin freely: confirmed
            # 10,752 same-name-within-50km collisions across the raw
            # extract, the overwhelming majority of them outside the
            # US -- Serengeti National Park, Kakadu National Park,
            # Fiordland National Park, and thousands more, none of which
            # the bbox-gated version ever had a chance to catch.
            #
            # BUG FOUND AND FIXED before this ran for real (2026-09-07):
            # the first cut of this check also queried and distance-
            # tested against this candidate's own CENTROID only, the
            # same technique fetch_padus_parks() uses against POTA --
            # correct for an ordinary city park, where centroid and POTA
            # point sit metres apart, but wrong for a National Forest-
            # scale relation: a POTA point is often placed at one
            # specific activation spot (a trailhead, a visitor centre),
            # which can be tens of km from the geometric centroid of a
            # shape that size. Confirmed duplicating Yosemite National
            # Park, Flathead National Forest, and Sequoia National
            # Forest -- each shipped as BOTH a POTA/PAD-US row and a
            # separate OSM row, exactly the double-counting this stage
            # exists to avoid -- before the fix below caught it. Now
            # queries and tests containment against the candidate's
            # real, full, pre-clip GEOMETRY (bbox padded by
            # _DEDUP_NEAR_DEG so a POTA point sitting just outside its
            # own park's mapped boundary -- the same hand-entered-point
            # slop match_parks() itself tolerates -- still counts as a
            # dup), not a small fixed buffer around one point: a dup is
            # a strong name match (score >= 0.5) whose US/POTA point
            # either falls INSIDE this geometry (match_parks()'s own
            # "contains" test, valid at any size) or within
            # _DEDUP_NEAR_DEG of the centroid (the original small-park
            # case, kept for a park whose own centroid sits near its
            # POTA point but whose true shape is thin/irregular enough
            # that containment alone might miss it at the boundary).
            if dedup_tree is not None:
                pt = shapely.Point(lon, lat)
                minlon, minlat, maxlon, maxlat = geom.bounds
                query_box = shapely.box(minlon - _DEDUP_NEAR_DEG, minlat - _DEDUP_NEAR_DEG,
                                         maxlon + _DEDUP_NEAR_DEG, maxlat + _DEDUP_NEAR_DEG)
                is_dup = False
                for i in dedup_tree.query(query_box):
                    if _name_score(name, dedup_names[i]) < 0.5:
                        continue
                    if geom.contains(dedup_pts[i]) or pt.distance(dedup_pts[i]) < _DEDUP_NEAR_DEG:
                        is_dup = True
                        break
                if is_dup:
                    counts["dup_of_us_park"] += 1
                    continue

            # PARK-SIZE SCORING, against the real, full, pre-clip shape
            # -- see PARK_REMOTE_AREA_FRAC's own comment above
            # match_parks(). true_area_m2 is passed so a self-
            # intersecting OSM relation that make_valid() cannot fully
            # resolve is measured, not silently mis-clipped -- see the
            # CLIPPED-GEOMETRY GUARD comment above _frac_area_outside_city.
            frac_outside = _frac_area_outside_city(geom, buckets, true_area_m2=area_m2)

            # STORAGE CLIP -- see module docstring and _OSM_PARK_CLIP_DEG's
            # own comment: this dataset's boundary=protected_area side
            # ranges up to Papahānaumokuākea's 1.5 million km^2, and
            # app/places_seed.py's _park_cells() has no size guard of
            # its own against a stored geometry that large.
            pt = shapely.Point(lon, lat)
            try:
                clipped = geom.intersection(pt.buffer(_OSM_PARK_CLIP_DEG))
            except Exception:
                g_fixed = geom.buffer(0)
                try:
                    clipped = g_fixed.intersection(pt.buffer(_OSM_PARK_CLIP_DEG))
                except Exception:
                    clipped = g_fixed
            if clipped.is_empty:
                clipped = geom
            simplified = clipped.simplify(0.0008, preserve_topology=True)

            candidates.append({
                "idx": total,
                "row": ["park", f"OSM-{code}", name, f"{lat:.6f}", f"{lon:.6f}",
                        POINTS["park"], "OSM", f"{area_m2:.0f}", simplified.wkt, "",
                        f"{frac_outside:.4f}"],
                "name_key": _osm_self_dedup_key(name),
                "lat": lat, "lon": lon, "area_m2": area_m2,
            })

    # OSM-VS-OSM SELF-DEDUP -- see module docstring's "OSM-against-
    # ITSELF" section, _osm_self_dedup_key()'s comment (why exact-name
    # only, not fuzzy/substring), and _dedup_osm_self_group()'s comment
    # (why greedy-largest-first, not transitive union-find). Every
    # candidate reaching this point already has a non-empty name --
    # blank-name features were dropped above at the "no_name" filter,
    # never buffered at all -- so, unlike the bug caught mid-flight on
    # the anchor build, there is no blank-name bucket for this grouping
    # to wrongly collapse into one giant group.
    name_groups: dict[str, list[dict]] = {}
    for c in candidates:
        name_groups.setdefault(c["name_key"], []).append(c)

    survivors = []
    for group in name_groups.values():
        if len(group) == 1:
            survivors.extend(group)
            continue
        deduped = _dedup_osm_self_group(group)
        counts["dup_of_osm_self"] += len(group) - len(deduped)
        survivors.extend(deduped)
    survivors.sort(key=lambda c: c["idx"])

    with open(out_path, "w", newline="", encoding="utf-8") as out:
        w = csv.writer(out)
        w.writerow(SEED_FIELDS)
        for c in survivors:
            w.writerow(c["row"])
            kept += 1
            if c["area_m2"] >= SQUARE_AREA_M2:
                larger += 1
            else:
                smaller += 1

    print(f"osm-parks: {total} candidates, wrote {kept} OSM parks "
          f"(larger-than-cell={larger} permanent, smaller-than-cell={smaller} rotating) "
          f"-> {out_path}", file=sys.stderr)
    print(f"osm-parks: excluded {counts}", file=sys.stderr)


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
# builds below, in degrees -- purely an INDEXING granularity (how far
# apart two buckets are), not a search-window guarantee (see
# _in_city_limits below for that, which used to conflate the two). Must
# be bigger than the largest anchor's own radius converted to degrees of
# longitude AT THAT ANCHOR'S OWN LATITUDE, so no anchor's circle can
# reach past its immediate neighbouring bucket. Converting a radius to
# degrees of longitude depends on latitude (a degree of longitude covers
# fewer metres than a degree of latitude everywhere except the equator,
# and covers less the further from the equator you are), so this was
# checked against every anchor in the actual file rather than assuming
# the largest radius is also the worst case: 0.81 degrees, from a
# ~47.2km-radius Alaska anchor at 58.4N (cos ~0.526) -- New York's own
# anchor is larger in absolute terms (~51.7km) but sits at a low enough
# latitude (40.7N, cos ~0.758) that it converts to a smaller 0.61
# degrees. Both, and every other anchor checked, land comfortably under
# the 1.0 degree bucket size.
#
# STALE ASSUMPTION REMOVED (2026-09-07): this comment used to also lean
# on "this script's own candidate queries never leave the western play
# area" to justify scanning a fixed 3x3 neighbourhood of buckets around
# the QUERY point. That is a claim about the QUERY's own latitude --
# a different question from the anchor-indexing one above -- and it
# went false the moment fetch_sota()/fetch_pota()/extract_landmarks()
# stopped bbox-filtering to that play area (see the module's "WORLDWIDE
# EXPANSION" note): a landmark or park can now be scored anywhere on
# Earth, including genuinely high-latitude places (Alaska, Scandinavia,
# northern Canada, Patagonia) where a degree of longitude shrinks well
# below the ~80km/degree the old fixed +-1-bucket window assumed.
# _in_city_limits below no longer leans on that assumption at all: it
# derives its own search window per query from the query's OWN latitude
# and the largest anchor radius actually loaded (_anchor_reach_m /
# _anchor_lat_bucket_span / _anchor_lon_bucket_span, plus
# _wrap_anchor_lon_bucket for the antimeridian) -- the same fix
# app/places.py already applies to its own, independent copy of this
# pattern (see that module's _lat_bucket_span/_lon_bucket_span/
# _wrap_lon_bucket). The real, derived guarantee is: for any query point
# anywhere on Earth, every anchor whose circle could contain it is
# found -- not "queries stay where we already checked."
_ANCHOR_BUCKET_DEG = 1.0

# Metres per degree of latitude -- close enough to constant across the
# globe (110.57km at the equator to 111.69km at the poles) to treat as
# one figure everywhere, same reasoning and value as app/places.py's own
# _METRES_PER_DEGREE_LAT. Longitude buckets convert this further by
# cos(latitude) -- see _anchor_lon_bucket_span.
_METRES_PER_DEGREE_LAT = 111_320.0


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


class _AnchorBuckets(dict):
    """dict subclass returned by _load_city_anchors, identical in every
    way to a plain {(lat_bucket, lon_bucket): [(lat, lon, radius_m)]}
    dict (every existing reader -- _anchors_near_bbox, _in_city_limits --
    just calls .get()/.values() on it, unaware of the difference) except
    that it also carries the largest anchor radius among its own entries
    as `max_radius_m`, computed once at load time rather than re-scanned
    on every _in_city_limits call. See _anchor_reach_m for the fallback
    that keeps a plain dict (a test fixture built by hand, bypassing
    _load_city_anchors entirely) working too."""
    max_radius_m: float = 0.0


def _load_city_anchors(path: str) -> dict[tuple[int, int], list[tuple[float, float, float]]]:
    """Reads app/reference/places.csv (lat,lon,effective_radius_m,
    comment lines starting with '#') and buckets each anchor into a
    coarse (lat_bucket, lon_bucket) grid at _ANCHOR_BUCKET_DEG
    resolution, so _in_city_limits below only has to haversine-check
    anchors in a place's own latitude-sized neighbourhood instead of
    every anchor in the country (see _anchor_lat_bucket_span /
    _anchor_lon_bucket_span -- no longer a fixed 3x3)."""
    buckets = _AnchorBuckets()
    max_radius_m = 0.0
    with open(path, encoding="utf-8", newline="") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            lat_s, lon_s, radius_s = line.split(",")
            lat, lon, radius_m = float(lat_s), float(lon_s), float(radius_s)
            key = (math.floor(lat / _ANCHOR_BUCKET_DEG), math.floor(lon / _ANCHOR_BUCKET_DEG))
            buckets.setdefault(key, []).append((lat, lon, radius_m))
            if radius_m > max_radius_m:
                max_radius_m = radius_m
    buckets.max_radius_m = max_radius_m
    return buckets


def _anchor_reach_m(buckets: dict) -> float:
    """Metres the bucket scan below must guarantee reaching from any
    query point: the largest anchor radius among `buckets`' own anchors,
    so an anchor whose circle could contain the query point is never
    missed regardless of where it sits. Reads the precomputed
    max_radius_m off a real _AnchorBuckets table (the ~35k-anchor
    production case, where rescanning every call would be the expensive
    part); falls back to scanning `buckets` itself for a plain dict
    handed in directly (test fixtures with a handful of anchors, where
    that scan costs nothing)."""
    cached = getattr(buckets, "max_radius_m", None)
    if cached is not None:
        return cached
    return max((r for anchors in buckets.values() for _, _, r in anchors), default=0.0)


def _anchor_lat_bucket_span(reach_m: float) -> int:
    """Latitude buckets to extend above and below the query's own bucket
    to guarantee `reach_m` metres of north-south coverage -- same
    derivation as app/places.py's _lat_bucket_span."""
    return max(1, math.ceil(reach_m / _METRES_PER_DEGREE_LAT))


def _anchor_lon_bucket_span(lat: float, reach_m: float) -> int:
    """Longitude buckets to extend east and west of the query's own
    bucket to guarantee `reach_m` metres of east-west coverage AT THIS
    LATITUDE -- same derivation as app/places.py's _lon_bucket_span.
    Capped at 180: past that the caller scans every longitude bucket
    instead (see _in_city_limits)."""
    cos_lat = math.cos(math.radians(lat))
    if cos_lat < 1e-9:
        # Within a hair of a pole: a degree of longitude is essentially
        # a point, so no finite span would do -- scan every bucket.
        return 180
    return min(180, math.ceil(reach_m / (_METRES_PER_DEGREE_LAT * cos_lat)))


def _wrap_anchor_lon_bucket(bucket: int) -> int:
    """Normalise a longitude bucket index to the [-180, 179] range
    _load_city_anchors actually keys buckets with (floor() of a
    longitude in [-180, 180)), so a scan that walks past +179 or below
    -180 finds the antimeridian-wrapped bucket instead of an empty one
    -- same as app/places.py's _wrap_lon_bucket."""
    return ((bucket + 180) % 360) - 180


def _in_city_limits(lat: float, lon: float, buckets: dict) -> bool:
    """True if (lat, lon) falls within ANY anchor's effective_radius_m
    -- see _load_city_anchors and the module docstring's "SCORING BY
    EFFORT, NOT CATEGORY" for what this radius means.

    The bucket neighbourhood scanned is sized per query (2026-09-07),
    not a fixed 3x3: enough longitude buckets to cover the largest
    loaded anchor radius at the QUERY's own latitude, and enough
    latitude buckets to cover it too. A fixed +-1-bucket window used to
    rest on "queries never leave the western play area" -- true once,
    false now that SOTA/POTA/OSM landmarks are worldwide (see
    _ANCHOR_BUCKET_DEG's own comment). Mirrors app/places.py's
    distance_to_nearest_town_m exactly, minus the "return the distance"
    part -- this only needs a yes/no."""
    reach_m = _anchor_reach_m(buckets)
    lat_b = math.floor(lat / _ANCHOR_BUCKET_DEG)
    lon_b = math.floor(lon / _ANCHOR_BUCKET_DEG)
    lat_span = _anchor_lat_bucket_span(reach_m)
    lon_span = _anchor_lon_bucket_span(lat, reach_m)
    full_lon_sweep = lon_span >= 180
    lon_offsets = range(-180, 180) if full_lon_sweep else range(-lon_span, lon_span + 1)
    for d_lat in range(-lat_span, lat_span + 1):
        plat = lat_b + d_lat
        for dlon in lon_offsets:
            # In a full sweep dlon IS already a bucket key (-180..179);
            # otherwise it is an offset from the query's own bucket that
            # may need wrapping at the antimeridian.
            plon = dlon if full_lon_sweep else _wrap_anchor_lon_bucket(lon_b + dlon)
            for a_lat, a_lon, radius_m in buckets.get((plat, plon), ()):
                if _haversine_m(lat, lon, a_lat, a_lon) <= radius_m:
                    return True
    return False


def score_points(row: dict, buckets: dict) -> tuple[int, str]:
    """(points, points_reason) for one merged row -- the real,
    effort-based value, replacing whatever placeholder points value the
    row's originating stage wrote. See module docstring.

    A summit is ALWAYS scored on its elevation, and never by the in-city
    rule. That order was the other way round until 2026-08-31, on the
    reasoning that a summit inside a town's limits is worth
    IN_CITY_POINTS because you can park at it. It could not survive
    contact with the data: 95 summits were being flattened to 5 points,
    among them Humphreys Peak (12,633ft, the highest point in Arizona)
    and four Wasatch peaks over 10,000ft. A town anchor's radius is a
    flat circle and takes no notice of the 6,000ft of relief inside it,
    so "inside a town" said nothing at all about the effort to reach a
    summit. A peak is a peak.

    Park and landmark still check in-city first and keep the flat
    REMOTE_POINTS value otherwise -- that rule was only ever wrong for
    summits, which are the one ref_type with an elevation to score on.

    A park with a matched boundary (row["area_frac_outside"] populated
    by match_parks()/fetch_padus_parks() -- see PARK_REMOTE_AREA_FRAC's
    own comment) is scored from that fraction instead of the point-
    based _in_city_limits test: the centre point alone cannot speak for
    a polygon that can be orders of magnitude bigger than the circle it
    happens to sit inside or outside of. A park match_parks() left
    unmatched has no boundary to ask that question of, so it (and every
    landmark, which never has one either) still uses the point test."""
    lat, lon = float(row["lat"]), float(row["lon"])
    if row["ref_type"] == "summit":
        elev_s = row.get("elevation_ft")
        elevation_ft = float(elev_s) if elev_s not in (None, "") else None
        return _summit_points(elevation_ft), "remote_scaled"
    if row["ref_type"] == "park":
        frac_s = row.get("area_frac_outside")
        if frac_s not in (None, ""):
            if float(frac_s) > PARK_REMOTE_AREA_FRAC:
                return REMOTE_POINTS["park"], "remote_by_area"
            return IN_CITY_POINTS, "in_city_by_area"
    if _in_city_limits(lat, lon, buckets):
        return IN_CITY_POINTS, "in_city"
    return REMOTE_POINTS[row["ref_type"]], "remote"


def _open_out_csv(path: str, **kwargs):
    """Opens path for text writing, gzip-compressing when the name
    ends in .gz -- so a rebuild that passes --out ...places_worth_going.csv.gz
    (the shipped default) writes compressed directly instead of
    silently recreating the 136MB plain CSV that broke GitHub's push
    limit (2026-09-07). Plain open() otherwise.
    """
    if path.endswith(".gz"):
        return gzip.open(path, "wt", **kwargs)
    return open(path, "w", **kwargs)


def merge(inputs: list, out_path: str, places_csv: str = _DEFAULT_PLACES_CSV) -> None:
    buckets = _load_city_anchors(places_csv)
    seen = set()
    total = 0
    counts = {}
    dist = {}  # (ref_type, points, reason) -> count, for the distribution report
    with _open_out_csv(out_path, newline="", encoding="utf-8") as out:
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

    p = sub.add_parser("extract-osm-parks")
    p.add_argument("geojsonseq")
    p.add_argument("landmarks_csv")
    p.add_argument("pota_csv")
    p.add_argument("padus_csv")
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
    elif args.cmd == "extract-osm-parks":
        extract_osm_parks(args.geojsonseq, args.landmarks_csv, args.pota_csv,
                           args.padus_csv, args.out)
    elif args.cmd == "merge":
        merge(args.inputs, args.out, args.places_csv)


if __name__ == "__main__":
    main()
