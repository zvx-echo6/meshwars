#!/usr/bin/env python3
"""Build app/reference/summit_cells.csv -- the squares that credit a summit.

RUNS ON navi (100.64.0.27), NOT on an app host: it needs the planet DEM
at /data/nav/dem/planet-dem.pmtiles (705GB, terrarium-encoded WEBP), which
is why the result ships precomputed instead of being derived at load time.
Requires the `pmtiles` and `PIL` packages, both already present there.

    python3 scripts/build_summit_cells.py summits.txt out.csv

`summits.txt` is "ref_code|name|lat|lon|elevation_ft" per line, taken from
the `place` table where ref_type='summit'.

WHY TERRAIN, NOT A RADIUS
A summit used to map to exactly the one square containing its peak, and
nobody ever reached one: 0 of 4,851 summits had ever been tagged. A plain
horizontal radius cannot fix that, because it is the wrong axis. Measured
2026-08-31: at 8km, 11 of the 17 Wasatch summits become claimable from a
valley street 6,000ft below them -- Lone Peak, a serious scramble, handed
out at 100 points to a car in Sandy -- while that same 8km still reaches
only 40 of 4,851 summits statewide. Wide enough to matter in flat country
is wide enough to give away the Wasatch.

So a square credits a summit only if it is within H_RADIUS_M horizontally
AND within V_TOL_M of the summit's own elevation. The vertical test is
what makes it mean "you got up there": downtown SLC reads 4,264ft against
Twin Peaks' 11,473ft, 2,197m apart, so the valley fails on any sane
tolerance while Guardsman Pass or the Willard Peak road passes.

EXCLUSIVITY -- the reason this is a global build, not a per-summit rule
65% of summits (3,152 of 4,851) have another summit within 5km AND 300m;
the worst has 17 neighbours. Without exclusivity one hike would credit 18
peaks at once. Each square is therefore assigned to its NEAREST qualifying
summit and no other. It must be decided by DISTANCE here, not left to the
runtime non-stacking rule, which resolves a square to its DEAREST place --
summit points scale with elevation, so that rule would credit the TALLER
neighbour and make the exploit worse rather than better.

A summit can end up with no squares at all (28 do) when a nearer summit
takes every square it would have qualified for, including its own peak
square. That is exclusivity working, not a bug: two summits sharing one
square can only credit one of them.

Output is offsets from each summit's own square rather than absolute cell
ids -- 3.6MB instead of ~15MB for the same 655k squares.
"""
import collections
import io
import math
import os
import sys

CELL_LAT, CELL_LON = 0.0027, 0.00384   # must match app/grid.py
H_RADIUS_M = 5000.0
V_TOL_M = 300.0
FT = 0.3048
DEM_PATH = os.environ.get("MW_DEM", "/data/nav/dem/planet-dem.pmtiles")


def _open_dem():
    from pmtiles.reader import MmapSource, Reader
    fh = open(DEM_PATH, "rb")
    reader = Reader(MmapSource(fh))
    return reader, reader.header()["max_zoom"]


def make_elevation_reader():
    """(lat, lon) -> metres, or None where the DEM has no tile.

    Terrarium encoding: elevation = (R*256 + G + B/256) - 32768. Tiles are
    cached because a 5km box around one summit hits only a handful of them
    and the same tiles recur across neighbouring summits.
    """
    from PIL import Image
    reader, maxz = _open_dem()
    cache = {}

    def tile(z, x, y):
        key = (z, x, y)
        if key not in cache:
            raw = reader.get(z, x, y)
            cache[key] = Image.open(io.BytesIO(raw)).convert("RGB") if raw else None
        return cache[key]

    def elevation_m(lat, lon):
        n = 2 ** maxz
        xf = (lon + 180.0) / 360.0 * n
        yf = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
        img = tile(maxz, int(xf), int(yf))
        if img is None:
            return None
        w, h = img.size
        r, g, b = img.getpixel((min(int((xf - int(xf)) * w), w - 1),
                                min(int((yf - int(yf)) * h), h - 1)))
        return (r * 256 + g + b / 256.0) - 32768.0

    return elevation_m


def build(summits, elevation_m):
    """[(ref_code, lat, lon, elev_m)] -> {ref_code: {(y, x), ...}}."""
    best = {}
    for i, (ref_code, lat, lon, elev) in enumerate(summits):
        dlat = H_RADIUS_M / 111320.0
        dlon = H_RADIUS_M / (111320.0 * math.cos(math.radians(lat)))
        y0 = math.floor((lat - dlat) / CELL_LAT); y1 = math.floor((lat + dlat) / CELL_LAT)
        x0 = math.floor((lon - dlon) / CELL_LON); x1 = math.floor((lon + dlon) / CELL_LON)
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                cy = (y + 0.5) * CELL_LAT
                cx = (x + 0.5) * CELL_LON
                dy = (cy - lat) * 111320.0
                dx = (cx - lon) * 111320.0 * math.cos(math.radians(lat))
                d = math.hypot(dy, dx)
                if d > H_RADIUS_M:
                    continue
                m = elevation_m(cy, cx)
                if m is None or abs(m - elev) > V_TOL_M:
                    continue
                cur = best.get((y, x))
                if cur is None or d < cur[0]:      # nearest summit wins
                    best[(y, x)] = (d, i)
        if i and i % 500 == 0:
            print(f"  {i}/{len(summits)} summits, {len(best)} squares", file=sys.stderr)

    by_summit = collections.defaultdict(set)
    for (y, x), (_d, i) in best.items():
        by_summit[summits[i][0]].add((y, x))
    return by_summit


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    summits = []
    with open(argv[1], encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            ref_code, _name, lat, lon, ft = line.split("|")
            summits.append((ref_code, float(lat), float(lon), float(ft) * FT))

    by_summit = build(summits, make_elevation_reader())

    with open(argv[2], "w", encoding="utf-8") as out:
        out.write("ref_code,base_y,base_x,offsets\n")
        for ref_code, lat, lon, _elev in summits:
            cells = by_summit.get(ref_code)
            if not cells:
                continue
            by = math.floor(lat / CELL_LAT)
            bx = math.floor(lon / CELL_LON)
            offs = " ".join(f"{y - by}:{x - bx}" for y, x in sorted(cells))
            out.write(f"{ref_code},{by},{bx},{offs}\n")

    total = sum(len(v) for v in by_summit.values())
    print(f"  {total:,} squares across {len(by_summit)} of {len(summits)} summits")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
