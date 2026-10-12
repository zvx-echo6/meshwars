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
 * draw a line deep into the shape -- are closed (closeInlets, below), and the
 * opposite, thin protrusions (spikes and needles of the source borders) are
 * removed by a morphological opening (openShape) with a vertex-level needle
 * filter as a backstop.
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
  const CLOSE_DEG = 0.02;         // ~2.2 km: inlets narrower than twice this (~4.4 km) are closed
  const OPEN_DEG = 0.015;         // ~1.7 km: protrusions narrower than twice this (~3.3 km) are removed
  const MAX_OPEN_LOSS = 0.02;     // an opening that costs a piece more than 2% of its area is not used for it (rounding the corners of a ragged border costs 0.2-0.8%)
  const FINAL_SIMPLIFY_DEG = 0.004; // ~440 m, the last simplification
  const NEEDLE_DEG = 10;          // vertices sharper than this, between short edges, are removed
  const NEEDLE_EDGE_R = 6;        // 'short': under this many OPEN_DEG
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

  // ---- morphology -----------------------------------------------------------
  //
  // Dilation and erosion by a small square, done with the union library alone
  // (it has no buffer): the shape is combined with eight copies of itself
  // shifted by r in each direction -- the square's corners and edge midpoints,
  // which is all a feature narrower than 2r needs to be reached whatever its
  // direction. Union of the copies is the dilation, intersection the erosion.
  // The copies are shifted by r times a slightly different factor each, and the
  // input is snapped first: eight exact copies make many edges coincide
  // exactly, which the library's sweep line cannot always handle (it throws
  // "Unable to find segment"), whereas slightly staggered ones it can.
  const DIRS = [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [1, -1], [-1, 1], [-1, -1]];
  const STAGGER = [[1, 1.0137, 0.9871, 1.0093, 0.9929, 1.0171, 0.9833, 1.0059],
                   [1.0211, 0.9809, 1.0067, 0.9913, 1.0149, 0.9877, 1.0103, 0.9961]];

  function shiftMulti(multi, dx, dy) {
    return multi.map((p) => p.map((ring) => ring.map((c) => [c[0] + dx, c[1] + dy])));
  }

  function snapMulti(multi) {
    return multi.map((p) => p.map((ring) => ring.map((c) => [snapValue(c[0]), snapValue(c[1])])));
  }

  // 'dilate' or 'erode' by r. null if the library fails on every attempt.
  function morph(multi, polygonClipping, r, mode) {
    const base = snapMulti(multi);
    for (const stagger of STAGGER) {
      try {
        const copies = DIRS.map(([dx, dy], i) => shiftMulti(base, dx * r * stagger[i], dy * r * stagger[i]));
        const res = mode === 'dilate'
          ? polygonClipping.union(base, ...copies)
          : polygonClipping.intersection(base, ...copies);
        return res;
      } catch (err) { /* try the other stagger */ }
    }
    return null;
  }

  function multiAreaKm2(multi) {
    return multi.reduce((n, p) => n + ringAreaKm2(p[0]), 0);
  }

  function insideOf(o, piece) {
    return pointInRing(centroidOf(o[0]), piece[0]) || o[0].some((pt) => pointInRing(pt, piece[0]));
  }

  function centroidOf(ring) {
    let x = 0, y = 0;
    for (let i = 0; i < ring.length - 1; i++) { x += ring[i][0]; y += ring[i][1]; }
    return [x / (ring.length - 1), y / (ring.length - 1)];
  }

  /**
   * Morphological closing: dilate by r, then erode by r. A gap between
   * members narrower than 2r that opens to the outside (an inlet) is filled
   * by the dilation, and the erosion takes the shape back to its own extent,
   * so the border does not creep outward. If the library fails, the shape is
   * returned unchanged.
   */
  function closeInlets(multi, polygonClipping, r, minKm2) {
    const grown = morph(multi, polygonClipping, r, 'dilate');
    if (!grown) return multi;
    const back = morph(outerOnly(grown, minKm2), polygonClipping, r, 'erode');
    return back && back.length ? outerOnly(back, minKm2) : outerOnly(grown, minKm2);
  }

  /**
   * Morphological opening: erode by r, then dilate by r. Anything narrower
   * than about 2r -- a spike or a needle in the borders -- does not survive
   * the erosion and so is not brought back; broad shapes keep their extent.
   * Guarded per piece: if the opening costs a piece more than MAX_OPEN_LOSS of
   * its area, or removes it altogether, that piece is kept as it was.
   */
  function openShape(multi, polygonClipping, r) {
    const eroded = morph(multi, polygonClipping, r, 'erode');
    if (!eroded || !eroded.length) return multi;
    const opened = morph(eroded, polygonClipping, r, 'dilate');
    if (!opened) return multi;
    const outer = opened.map((p) => [p[0]]);
    const result = [];
    for (const piece of multi) {
      const mine = outer.filter((o) => insideOf(o, piece));
      const kept = mine.reduce((n, o) => n + ringAreaKm2(o[0]), 0);
      if (kept < ringAreaKm2(piece[0]) * (1 - MAX_OPEN_LOSS)) result.push(piece);   // lost too much: keep it as it was
      else result.push(...mine);
    }
    return result;
  }

  // Backstop for what the opening leaves: a vertex whose interior angle is
  // under NEEDLE_DEG, between two edges both shorter than NEEDLE_EDGE_R * r,
  // is a needle tip and is removed. Repeats until none is left.
  function removeNeedles(ring, r) {
    const maxEdge = NEEDLE_EDGE_R * r;
    let pts = ring.slice(0, -1);
    let changed = true;
    while (changed && pts.length > 4) {
      changed = false;
      for (let i = 0; i < pts.length && pts.length > 4; i++) {
        const a = pts[(i + pts.length - 1) % pts.length], b = pts[i], c = pts[(i + 1) % pts.length];
        const ux = a[0] - b[0], uy = a[1] - b[1], vx = c[0] - b[0], vy = c[1] - b[1];
        const lu = Math.hypot(ux, uy), lv = Math.hypot(vx, vy);
        if (!lu || !lv || lu > maxEdge || lv > maxEdge) continue;
        const ang = Math.acos(Math.max(-1, Math.min(1, (ux * vx + uy * vy) / (lu * lv)))) * 180 / Math.PI;
        if (ang < NEEDLE_DEG) { pts.splice(i, 1); i--; changed = true; }
      }
    }
    return pts.concat([pts[0]]);
  }

  /**
   * The merged border of a group's member features (polygonClipping is the
   * vendored library). Returns MultiPolygon coordinates, outer rings only;
   * [] if nothing survives.
   */
  function buildOutline(features, polygonClipping, opts) {
    const o = Object.assign({ tol: SIMPLIFY_DEG, minKm2: MIN_AREA_KM2, close: CLOSE_DEG, open: OPEN_DEG }, opts || {});
    const polys = prepare(features, o.tol);
    if (!polys.length) return [];
    const merged = polys.length === 1 ? [polys[0]] : polygonClipping.union(polys[0], ...polys.slice(1));
    let out = outerOnly(merged, o.minKm2);
    // Each dilation/erosion combines nine copies of the shape, so its cost grows with the vertex count:
    // simplify a little more before them (the finer detail is gone after them anyway).
    out = out.map((p) => [cleanRing(p[0], o.tol * 1.5) || p[0]]);
    if (o.close > 0) out = closeInlets(out, polygonClipping, o.close, o.minKm2);
    if (o.open > 0) {
      // fewer vertices for the two dilations/erosions: the closing's stair steps carry no information
      out = out.map((p) => [cleanRing(p[0], o.tol * 2) || p[0]]);
      out = outerOnly(openShape(out, polygonClipping, o.open), o.minKm2);
      // The last pass: one more simplification (~440 m) takes the sub-kilometre teeth the
      // dilations leave, then the needle filter removes any tip still sharp and short.
      out = out.map((p) => [removeNeedles(cleanRing(p[0], FINAL_SIMPLIFY_DEG) || p[0], o.open)]);
    }
    return out;
  }

  /**
   * Longest spike, in km: the greatest distance from a tip along which the
   * two sides of the border stay within `widthKm` of each other (a sliver
   * narrower than that). 0 for a border without such protrusions. For
   * verification, not drawing.
   */
  function maxSpikeKm(ring, widthKm, where, list) {
    const kx = KM_PER_DEG * Math.cos(ring[0][1] * Math.PI / 180);
    const P = ring.slice(0, -1).map((c) => [c[0] * kx, c[1] * KM_PER_DEG]);
    const n = P.length;
    const cum = [0];
    for (let i = 1; i <= n; i++) cum.push(cum[i - 1] + Math.hypot(P[i % n][0] - P[i - 1][0], P[i % n][1] - P[i - 1][1]));
    const total = cum[n];
    const at = (s) => {
      s = ((s % total) + total) % total;
      let lo = 0, hi = n;
      while (hi - lo > 1) { const m = (lo + hi) >> 1; if (cum[m] <= s) lo = m; else hi = m; }
      const f = (s - cum[lo]) / ((cum[lo + 1] - cum[lo]) || 1);
      const a = P[lo], b = P[(lo + 1) % n];
      return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
    };
    const STEP = 0.5;
    let best = 0;
    for (let i = 0; i < n; i++) {
      // only sharp-ish tips can start a spike: skip vertices on a straight or blunt border
      const a = P[(i + n - 1) % n], b = P[i], c = P[(i + 1) % n];
      const ux = a[0] - b[0], uy = a[1] - b[1], vx = c[0] - b[0], vy = c[1] - b[1];
      const lu = Math.hypot(ux, uy), lv = Math.hypot(vx, vy);
      if (!lu || !lv) continue;
      if (Math.acos(Math.max(-1, Math.min(1, (ux * vx + uy * vy) / (lu * lv)))) * 180 / Math.PI > 60) continue;
      let len = 0;
      for (let d = STEP; d < 200 && d < total / 2; d += STEP) {
        const p = at(cum[i] - d), q = at(cum[i] + d);
        if (Math.hypot(p[0] - q[0], p[1] - q[1]) >= widthKm) break;
        len = d;
      }
      if (list && len > 0) list.push([len, ring[i]]);
      if (len > best) { best = len; if (where) where.at = ring[i]; }
    }
    return best;
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

  return { buildOutline, prepare, cleanRing, simplifyLine, ringAreaKm2, outerOnly, closeInlets, openShape, removeNeedles, narrowestGapKm, maxSpikeKm, pointInRing,
           SNAP_DEG, SIMPLIFY_DEG, MIN_AREA_KM2, CLOSE_DEG, OPEN_DEG };
}));
