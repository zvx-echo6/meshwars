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

OSM TAG LIST -- the approved narrowed list (docs/features/places.md).
fire_station and post_office were cut by Matt and must NOT be restored:
  amenity=townhall, amenity=courthouse, amenity=library
  tourism=museum, tourism=viewpoint, tourism=attraction
  tourism=information WHERE information=visitor_centre
  historic=memorial, historic=monument, historic=marker
  highway=trailhead
A landmark also needs a `name` tag -- an unnamed node matching one of
these tags is not a "named destination" and is skipped.

POINTS (flat, by ref_type -- not derived from SOTA's own points column
or PAD-US acreage): landmark 5, park 25, summit 100.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import io
import sys
import urllib.request

NORTH, SOUTH, WEST, EAST = 49.29, 25.8, -125.0, -93.5

SOTA_URL = "https://storage.sota.org.uk/summitslist.csv"
POTA_URL = "https://pota.app/all_parks_ext.csv"

POINTS = {"summit": 100, "park": 25, "landmark": 5}

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
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(SEED_FIELDS)
        for row in reader:
            try:
                lat = float(row["Latitude"])
                lon = float(row["Longitude"])
            except (KeyError, ValueError):
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
    print(f"sota: wrote {kept} summits -> {out_path}", file=sys.stderr)


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
LANDMARK_TAGS = {
    ("amenity", "townhall"), ("amenity", "courthouse"), ("amenity", "library"),
    ("tourism", "museum"), ("tourism", "viewpoint"), ("tourism", "attraction"),
    ("historic", "memorial"), ("historic", "monument"), ("historic", "marker"),
    ("highway", "trailhead"),
}


def _landmark_match(tags) -> bool:
    for k, v in LANDMARK_TAGS:
        if tags.get(k) == v:
            return True
    if tags.get("tourism") == "information" and tags.get("information") == "visitor_centre":
        return True
    return False


def extract_landmarks(pbf_path: str, out_path: str) -> None:
    """Run on navi against the tags-filter output, e.g.:

        osmium tags-filter -o filtered.pbf --overwrite \\
            /mnt/nas/nav/western-us-11states.osm.pbf \\
            amenity=townhall,courthouse,library \\
            tourism=museum,viewpoint,attraction,information \\
            historic=memorial,monument,marker highway=trailhead

    then: python3 build_places_seed.py extract-landmarks filtered.pbf landmarks.csv

    Nodes are used as-is. Ways are reduced to the plain average of their
    node coordinates -- not a true area centroid, but these are point-of-
    interest buildings and small grounds (museums, trailheads, town
    halls), not large irregular polygons, so the difference is noise at
    game-grid (300 m) scale. Relations are skipped: multipolygon assembly
    for ~359 objects out of ~19,000 was not worth the added dependency
    surface, and none of these tags commonly appear on relations.
    """
    import osmium

    class Handler(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.rows = []
            self.seen_names_skipped = 0

        def node(self, n):
            if not n.location.valid():
                return
            tags = n.tags
            if not _landmark_match(tags):
                return
            name = tags.get("name")
            if not name:
                self.seen_names_skipped += 1
                return
            lat, lon = n.location.lat, n.location.lon
            if not in_bbox(lat, lon):
                return
            self.rows.append(("n", n.id, name, lat, lon))

        def way(self, w):
            tags = w.tags
            if not _landmark_match(tags):
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
            lat = sum(lats) / len(lats)
            lon = sum(lons) / len(lons)
            if not in_bbox(lat, lon):
                return
            self.rows.append(("w", w.id, name, lat, lon))

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
          f"unnamed matches skipped) -> {out_path}", file=sys.stderr)


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
