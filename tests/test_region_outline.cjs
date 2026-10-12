// Run with: node --test tests/test_region_outline.cjs
// The merged outline of a region group (frontend/region-outline.js).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const pc = require(path.join(__dirname, '..', 'frontend', 'vendor', 'polygon-clipping.0.15.7.umd.min.js'));
const ro = require(path.join(__dirname, '..', 'frontend', 'region-outline.js'));

const box = (w, s, e, n) => ({ type: 'Feature', properties: {},
  geometry: { type: 'Polygon', coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]] } });
const holes = (multi) => multi.flatMap((p) => p.slice(1));

test('two neighbours whose borders miss by less than the snap grid merge into one piece', () => {
  // a 0.00004 degree (~4 m) gap: the snap puts both borders on the same grid line
  const out = ro.buildOutline([box(-116, 43, -115, 44), box(-115 + 0.00004, 43, -114, 44)], pc);
  assert.equal(out.length, 1);
  assert.equal(holes(out).length, 0);
});

test('a small hole left between members is dropped as a sliver', () => {
  // four members around a 0.005 x 0.005 degree (~0.3 km2) gap
  const members = [box(-117, 42, -116.0025, 44), box(-115.9975, 42, -115, 44),
                   box(-116.0025, 42, -115.9975, 42.9975), box(-116.0025, 43.0025, -115.9975, 44)];
  const raw = pc.union(...members.map((f) => f.geometry.coordinates));
  assert.equal(holes(raw).length, 1);                 // the library alone keeps it
  const out = ro.buildOutline(members, pc);
  assert.equal(out.length, 1);
  assert.equal(holes(out).length, 0);
});

test('a real gap between members stays as a hole', () => {
  const ring = [box(-117, 42, -113, 43), box(-117, 45, -113, 46), box(-117, 43, -116, 45), box(-114, 43, -113, 45)];
  const out = ro.buildOutline(ring, pc);
  assert.equal(out.length, 1);
  assert.equal(holes(out).length, 1);
  assert.ok(ro.ringAreaKm2(holes(out)[0]) > 1000);
});

test('tiny separate pieces are dropped, big ones kept', () => {
  const out = ro.buildOutline([box(-116, 43, -115, 44), box(-100, 40, -99.99, 40.01)], pc);
  assert.equal(out.length, 1);
});

test('simplifying keeps corners and removes near-collinear points', () => {
  const dense = [[0, 0], [0.5, 0.0001], [1, 0], [1, 1], [0, 1], [0, 0]];
  const ring = ro.cleanRing(dense, ro.SIMPLIFY_DEG);
  assert.equal(ring.length, 5);                                // 4 corners + closing point
  assert.deepEqual(ring[0], ring[ring.length - 1]);
});

test('a ring that collapses is dropped', () => {
  assert.equal(ro.cleanRing([[0, 0], [0.00001, 0], [0, 0.00001], [0, 0]], ro.SIMPLIFY_DEG), null);
});

test('area is in km2', () => {
  const a = ro.ringAreaKm2([[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]);
  assert.ok(Math.abs(a - 111.32 * 111.32) < 1);
});
