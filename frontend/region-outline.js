/*
 * Merged outline of a region group (loaded by map2.html as a classic script;
 * also require()-able from node for tests: tests/test_region_outline.cjs).
 *
 * The member regions of a joined region are separate MeshMapper polygons
 * whose borders nearly, but not exactly, meet, and which carry hundreds of
 * vertices a screen never shows. Unioned as served, that leaves hairline
 * slivers along the shared borders (drawn as stray lines inside the merged
 * border) and takes seconds. So, before the union, every ring is snapped to
 * a ~11 m grid and simplified (Douglas-Peucker, ~220 m), which closes the
 * hairline gaps and cuts the vertex count by an order of magnitude; after
 * it, the result is reduced to its OUTER EDGE ONLY: every interior ring is
 * dropped (the outline is a border, not a map of the gaps between members),
 * and so is any separate piece lying inside another. Finally narrow inlets --
 * thin channels where a gap between members opens to the outside and would
 * draw a line deep into the shape -- are closed (closeInlets, below).
 *
 * Coordinates are GeoJSON [lon, lat]. Areas use an equal-area
 * approximation (degrees scaled to km at the ring's mean latitude).
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.MWRegionOutline = factory();
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const SNAP_DEG = 1e-4;          // ~11 m
  const SIMPLIFY_DEG = 0.002;     // ~220 m
  const MIN_AREA_KM2 = 100;       // separate pieces smaller than this are dropped
  const CLOSE_DEG = 0.01;         // ~1.1 km: inlets narrower than twice this are closed
  const KM_PER_DEG = 111.32;

  function snapValue(v) {
    return Math.round(v / SNAP_DEG) * SNAP_DEG;
  }

  // Squared distance from p to the segment a-b, in degrees.
  function segDist2(p, a, b) {
    let dx = b[0] - a[0];
    let dy = b[1] - a[1];
    let t = 0;
    const len2 = dx * dx + dy * dy;
    if (len2 > 0) {
      t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2;
      t = Math.max(0, Math.min(1, t));
    }
    dx = a[0] + t * dx - p[0];
    dy = a[1] + t * dy - p[1];
    return dx * dx + dy * dy;
  }

  // Douglas-Peucker on an open polyline; iterative, so a 3,000-vertex ring
  // cannot overflow the stack. Keeps both end points.
  function simplifyLine(pts, tol) {
    const n = pts.length;
    if (n < 3) return pts.slice();
    const keep = new Uint8Array(n);
    keep[0] = keep[n - 1] = 1;
    const tol2 = tol * tol;
    const stack = [[0, n - 1]];
    while (stack.length) {
      const [lo, hi] = stack.pop();
      let worst = -1;
      let at = -1;
      for (let i = lo + 1; i < hi; i++) {
        const d = segDist2(pts[i], pts[lo], pts[hi]);
        if (d > worst) { worst = d; at = i; }
      }
      if (worst > tol2) {
        keep[at] = 1;
        stack.push([lo, at], [at, hi]);
      }
    }
    const out = [];
    for (let i = 0; i < n; i++) if (keep[i]) out.push(pts[i]);
    return out;
  }

  // Snap, drop repeated points, simplify, and re-close. Returns null when
  // fewer than 3 distinct points remain.
  function cleanRing(ring, tol) {
    const pts = [];
    for (const c of ring) {
      const p = [snapValue(c[0]), snapValue(c[1])];
      const last = pts[pts.length - 1];
      if (!last || last[0] !== p[0] || last[1] !== p[1]) pts.push(p);
    }
    if (pts.length > 1) {
      const f = pts[0];
      const l = pts[pts.length - 1];
      if (f[0] === l[0] && f[1] === l[1]) pts.pop();
    }
    if (pts.length < 3) return null;
    // Split the closed ring at the point farthest from the first, simplify
    // each half, so the anchor points are real corners and not arbitrary.
    let far = 1;
    let farD = -1;
    for (let i = 1; i < pts.length; i++) {
      const d = (pts[i][0] - pts[0][0]) ** 2 + (pts[i][1] - pts[0][1]) ** 2;
      if (d > farD) { farD = d; far = i; }
    }
    const a = simplifyLine(pts.slice(0, far + 1), tol);
    const b = simplifyLine(pts.slice(far).concat([pts[0]]), tol);
    const ring2 = a.concat(b.slice(1));          // ends with pts[0] again: closed
    return ring2.length >= 4 ? ring2 : null;
  }

  // Unsigned area of a closed ring in km2.
  function ringAreaKm2(ring) {
    let sum = 0;
    let latSum = 0;
    for (let i = 0; i < ring.length - 1; i++) {
      sum += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1];
      latSum += ring[i][1];
    }
    const lat = latSum / Math.max(1, ring.length - 1);
    return Math.abs(sum) / 2 * KM_PER_DEG * KM_PER_DEG * Math.cos(lat * Math.PI / 180);
  }

  // Every polygon (an array of rings, outer first) of a GeoJSON feature.
  function polygonsOf(feature) {
    const g = feature && feature.geometry;
    if (!g) return [];
    if (g.type === 'Polygon') return [g.coordinates];
    if (g.type === 'MultiPolygon') return g.coordinates;
    return [];
  }

  // Snapped, simplified member polygons ready for the union.
  function prepare(features, tol) {
    const out = [];
    for (const f of features) {
      for (const poly of polygonsOf(f)) {
        const rings = [];
        poly.forEach((ring, i) => {
          const c = cleanRing(ring, tol);
          if (c && (i === 0 || true)) rings.push(c);
        });
        if (rings.length) out.push(rings);
      }
    }
    return out;
  }

  function pointInRing(pt, ring) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
      if ((yi > pt[1]) !== (yj > pt[1]) && pt[0] < (xj - xi) * (pt[1] - yi) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }

  // Outer rings only: no holes at all, no piece smaller than minKm2, and no
  // piece lying entirely inside another piece's outer ring.
  function outerOnly(multi, minKm2) {
    const pieces = multi.map((p) => [p[0]]).filter((p) => ringAreaKm2(p[0]) >= minKm2);
    return pieces.filter((p, i) => !pieces.some((q, j) => j !== i && ringAreaKm2(q[0]) > ringAreaKm2(p[0])
      && p[0].every((pt) => pointInRing(pt, q[0]))));
  }

  // The polygon shifted by `r` degrees in each of eight directions. Their
  // union is the polygon dilated by a small square (a Minkowski sum with the
  // square's corners and edge midpoints, which is all a gap narrower than 2r
  // needs to be filled whatever its direction).
  function shifted(multi, r) {
    const out = [];
    const dirs = [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [1, -1], [-1, 1], [-1, -1]];
    for (const [dx, dy] of dirs) {
      for (const p of multi) out.push([p[0].map((c) => [c[0] + dx * r, c[1] + dy * r])]);
    }
    return out;
  }

  /**
   * Closes narrow inlets: the shape is dilated by `r` (~1 km) -- unioned with
   * eight copies of itself shifted by r -- so two borders facing each other
   * across a gap narrower than 2r merge and the gap fills, while the rest of
   * the border only moves outward by about r, invisible at map scale. Interior
   * rings the closing leaves behind are dropped again. If the union library
   * cannot do it (it can throw on degenerate input), the unclosed outline is
   * returned rather than nothing.
   */
  function closeInlets(multi, polygonClipping, r, minKm2) {
    const parts = multi.concat(shifted(multi, r));
    try {
      return outerOnly(polygonClipping.union(parts[0], ...parts.slice(1)), minKm2);
    } catch (err) {
      return multi;
    }
  }

  /**
   * The merged border of a group's member features (polygonClipping is the
   * vendored library). Returns MultiPolygon coordinates, outer rings only;
   * [] if nothing survives.
   */
  function buildOutline(features, polygonClipping, opts) {
    const o = Object.assign({ tol: SIMPLIFY_DEG, minKm2: MIN_AREA_KM2, close: CLOSE_DEG }, opts || {});
    const polys = prepare(features, o.tol);
    if (!polys.length) return [];
    const merged = polys.length === 1 ? [polys[0]] : polygonClipping.union(polys[0], ...polys.slice(1));
    const outer = outerOnly(merged, o.minKm2);
    return o.close > 0 ? closeInlets(outer, polygonClipping, o.close, o.minKm2) : outer;
  }

  /**
   * Narrowest place where a ring faces itself: the smallest distance, in km,
   * between two of its segments that are far apart ALONG the ring (more than
   * `minArcKm` of border between them). A narrow inlet shows up as a small
   * value; a plain smooth border never does. For verification, not drawing.
   */
  function narrowestGapKm(ring, minArcKm) {
    const kx = KM_PER_DEG * Math.cos(ring[0][1] * Math.PI / 180);
    const P = ring.map((c) => [c[0] * kx, c[1] * KM_PER_DEG]);
    const arc = [0];
    for (let i = 1; i < P.length; i++) arc.push(arc[i - 1] + Math.hypot(P[i][0] - P[i - 1][0], P[i][1] - P[i - 1][1]));
    const total = arc[arc.length - 1];
    let best = Infinity;
    for (let i = 0; i < P.length - 1; i++) {
      for (let j = i + 2; j < P.length - 1; j++) {
        const sep = Math.min(arc[j] - arc[i + 1], total - (arc[j + 1] - arc[i]));
        if (sep < minArcKm) continue;
        const d = Math.sqrt(Math.min(segDist2(P[i], P[j], P[j + 1]), segDist2(P[i + 1], P[j], P[j + 1]),
                                     segDist2(P[j], P[i], P[i + 1]), segDist2(P[j + 1], P[i], P[i + 1])));
        if (d < best) best = d;
      }
    }
    return best;
  }

  return { buildOutline, prepare, cleanRing, simplifyLine, ringAreaKm2, outerOnly, closeInlets, narrowestGapKm, pointInRing,
           SNAP_DEG, SIMPLIFY_DEG, MIN_AREA_KM2, CLOSE_DEG };
}));
