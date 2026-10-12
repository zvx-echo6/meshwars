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
 * it, holes and separate pieces under MIN_AREA_KM2 are dropped, so only
 * real gaps between members survive as holes.
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
  const MIN_AREA_KM2 = 100;       // holes and pieces smaller than this are dropped
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

  // Drops holes and whole pieces smaller than minKm2 from a MultiPolygon.
  function dropSlivers(multi, minKm2) {
    const out = [];
    for (const poly of multi) {
      if (ringAreaKm2(poly[0]) < minKm2) continue;
      out.push([poly[0]].concat(poly.slice(1).filter((hole) => ringAreaKm2(hole) >= minKm2)));
    }
    return out;
  }

  /**
   * The merged border of a group's member features (polygonClipping is the
   * vendored library). Returns MultiPolygon coordinates; [] if nothing
   * survives.
   */
  function buildOutline(features, polygonClipping, opts) {
    const o = Object.assign({ tol: SIMPLIFY_DEG, minKm2: MIN_AREA_KM2 }, opts || {});
    const polys = prepare(features, o.tol);
    if (!polys.length) return [];
    const merged = polys.length === 1 ? [polys[0]] : polygonClipping.union(polys[0], ...polys.slice(1));
    return dropSlivers(merged, o.minKm2);
  }

  return { buildOutline, prepare, cleanRing, simplifyLine, ringAreaKm2, dropSlivers,
           SNAP_DEG, SIMPLIFY_DEG, MIN_AREA_KM2 };
}));
