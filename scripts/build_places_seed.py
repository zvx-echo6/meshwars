#!/usr/bin/env python3
"""Builds app/reference/places_worth_going.csv -- the seed for the
"Places Worth Going" feature (docs/features/places.md). Summits, parks,
and landmarks that make a captured grid square worth more than an
ordinary one.

This is a PIPELINE, not a single pass, because its three sources live in
three different places and two of them need tools this repo's own
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
  5. merge            -- anywhere. Combines the three stage outputs into
                          the final seed CSV in the `place` table's shape.

SOURCES (pulled 2026-08-24):
  SOTA summits  -- https://storage.sota.org.uk/summitslist.csv
                   NOTE: this file has a non-CSV title line before the
                   real header ("SOTA Summits List (Date=...)") -- skip
                   line 1, DictReader from line 2.
  POTA parks    -- https://pota.app/all_parks_ext.csv
                   Centre points only -- POTA publishes no boundaries.
  OSM landmarks -- /mnt/nas/nav/western-us-11states.osm.pbf on pi-nas
                   (read-only source storage -- never write there),
                   reachable from navi.
  PAD-US        -- /data/nav/padus/PADUS4_0_Geodatabase.gdb on navi,
                   layer PADUS4_0Combined_Proclamation_Marine_Fee_
                   Designation_Easement (all protected-area types in one
                   layer, so a park does not go unmatched just because
                   it happens to be an easement rather than a fee title).

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
  highway=trailhead
  -- added 2026-08-24, "places worth going" rebalance:
  natural=hot_spring, natural=arch, natural=cave_entrance, natural=waterfall
  historic=mine, historic=ruins, historic=fort, historic=battlefield, historic=wreck
  man_made=lighthouse
  man_made=tower WHERE tower:type=observation (fire lookouts)
  tourism=alpine_hut, tourism=wilderness_hut
  leisure=nature_reserve WHERE geometry is a node or a small way (skipped
    if it would duplicate the parks tier -- see _landmark_match's area gate)
A landmark also needs a `name` tag -- an unnamed node matching one of
these tags is not a "named destination" and is skipped.

POINTS (flat, by ref_type -- not derived from SOTA's own points column
or PAD-US acreage): landmark 5, park 25, summit 100.

SUMMIT THRESHOLD (added 2026-08-24, "places worth going" rebalance):
SOTA's own Points column is elevation-derived (a 1-10 scale keyed to a
summit's prominence within its region) and is exactly the "is this a
real mountain, not a bump" signal the earlier unfiltered pull lacked --
every SOTA summit became a marker regardless of size, and summits render
as the largest symbol, so 26,600 of them buried the map. Matt's brief
was "high SOTA value, no easy picks" -- SUMMIT_MIN_POINTS is set to 10
(SOTA's own scale only takes even values 2/4/6/8/10 plus 1, so 10 is
literally the top of the scale, not an arbitrary round number), which
keeps 1,865 US in-bbox summits nationwide (measured against every
threshold from >=1 to >=10 before picking) -- the true top tier, one a
100-point place should cost an expedition to reach. Spread was checked
before picking, not assumed: at >=10 all 15 SOTA associations that have
ANY 10-point summit in the play area still have at least 6 (the
Dakotas is the thinnest; California the thickest at 353); the only
associations with zero are K0M/W0I/W0M/W0N (Minnesota/Iowa/Missouri/
Nebraska) and those are flat-state associations that already have zero
10-AND-8-point summits -- not something this threshold newly excludes.
Idaho (W7I), where the active players are, keeps 78 at >=10; the next
threshold down, >=8, keeps 472 in Idaho (6,487 nationwide) if that ever
needs revisiting. This filter runs in fetch_sota() below, on SOTA's
Points column, BEFORE the bbox/name checks -- not a separate stage,
since it only needs the one column already being read. The `points`
column written to the seed CSV is unrelated and unchanged by this: it
is always the flat game value (100 for every surviving summit), never
SOTA's own points value.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import io
import math
import sys
import urllib.request

NORTH, SOUTH, WEST, EAST = 49.29, 25.8, -125.0, -93.5

SOTA_URL = "https://storage.sota.org.uk/summitslist.csv"
POTA_URL = "https://pota.app/all_parks_ext.csv"

POINTS = {"summit": 100, "park": 25, "landmark": 5}

# SOTA's own elevation-derived Points column (1-10, effectively
# 1/2/4/6/8/10 -- see module docstring "SUMMIT THRESHOLD"). Only
# summits at or above this SOTA points value become a `place` row at
# all; the ones that survive still carry the flat game value of 100,
# not this number.
SUMMIT_MIN_SOTA_POINTS = 10

SEED_FIELDS = [
    "ref_type", "ref_code", "name", "lat", "lon", "points", "source",
    "area_m2", "geom",
]


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
            w.writerow(["summit", code, name, f"{lat:.6f}", f"{lon:.6f}",
                        POINTS["summit"], "SOTA", "", ""])
            kept += 1
    print(f"sota: wrote {kept} summits (SOTA Points >= {SUMMIT_MIN_SOTA_POINTS}; "
          f"{skipped_low_points} below threshold worldwide) -> {out_path}", file=sys.stderr)


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
    ("highway", "trailhead"),
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
    # Fire lookouts: man_made=tower is far too broad alone (cell towers,
    # water towers), so it only counts with tower:type=observation.
    if tags.get("man_made") == "tower" and tags.get("tower:type") == "observation":
        return "man_made=tower+observation"
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
            highway=trailhead \\
            natural=hot_spring,arch,cave_entrance,waterfall \\
            man_made=lighthouse,tower \\
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
                        POINTS["landmark"], "OSM", "", ""])
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
                        f"{area_m2:.0f}" if area_m2 != "" else "", geom_wkt])
        print(f"parks: {matched}/{total} matched a PAD-US boundary "
              f"({total - matched} unmatched, kept as points)", file=sys.stderr)


# --------------------------------------------------------------------
# Stage 5: merge
# --------------------------------------------------------------------
def merge(inputs: list, out_path: str) -> None:
    seen = set()
    total = 0
    counts = {}
    with open(out_path, "w", newline="", encoding="utf-8") as out:
        w = csv.writer(out)
        w.writerow(SEED_FIELDS)
        for path in inputs:
            with open(path, encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    key = (row["ref_type"], row["ref_code"])
                    if key in seen:
                        continue
                    seen.add(key)
                    w.writerow([row[f] for f in SEED_FIELDS])
                    total += 1
                    counts[row["ref_type"]] = counts.get(row["ref_type"], 0) + 1
    print(f"merge: {total} rows -> {out_path}", file=sys.stderr)
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}", file=sys.stderr)


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

    p = sub.add_parser("merge")
    p.add_argument("inputs", nargs="+")
    p.add_argument("--out", required=True)

    args = ap.parse_args()
    if args.cmd == "fetch-sota":
        fetch_sota(args.out)
    elif args.cmd == "fetch-pota":
        fetch_pota(args.out)
    elif args.cmd == "extract-landmarks":
        extract_landmarks(args.pbf, args.out)
    elif args.cmd == "match-parks":
        match_parks(args.pota_csv, args.out)
    elif args.cmd == "merge":
        merge(args.inputs, args.out)


if __name__ == "__main__":
    main()
