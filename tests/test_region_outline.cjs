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

test('the outline is the outer edge only: a hole between members is dropped, small or large', () => {
  const small = [box(-117, 42, -116.0025, 44), box(-115.9975, 42, -115, 44),
                 box(-116.0025, 42, -115.9975, 42.9975), box(-116.0025, 43.0025, -115.9975, 44)];   // ~0.3 km2 gap
  const large = [box(-117, 42, -113, 43), box(-117, 45, -113, 46), box(-117, 43, -116, 45), box(-114, 43, -113, 45)];
  for (const members of [small, large]) {
    assert.equal(holes(pc.union(...members.map((f) => f.geometry.coordinates))).length, 1);   // the library keeps it
    const out = ro.buildOutline(members, pc);
    assert.equal(out.length, 1);
    assert.equal(out[0].length, 1);                                                          // one ring: the outer edge
  }
});

test('a narrow inlet between members is closed, a wide one is left', () => {
  const frame = (gap) => [box(-117, 42, -116 - gap / 2, 44), box(-116 + gap / 2, 42, -115, 44), box(-117, 42, -115, 42.5)];
  const narrow = ro.buildOutline(frame(0.004), pc, { close: 0 });
  assert.ok(!ro.pointInRing([-116, 43], narrow[0][0]));           // without closing, the channel is cut into the outline
  const closed = ro.buildOutline(frame(0.004), pc);
  assert.ok(ro.pointInRing([-116, 43], closed[0][0]));            // closing fills it
  const wide = ro.buildOutline(frame(0.5), pc);
  assert.ok(!ro.pointInRing([-116, 43], wide[0][0]));             // a 50 km channel is a real feature
});

test('a piece lying inside another is dropped', () => {
  const out = ro.outerOnly([[[[-117, 42], [-115, 42], [-115, 44], [-117, 44], [-117, 42]]],
                            [[[-116.5, 42.5], [-116, 42.5], [-116, 43], [-116.5, 43], [-116.5, 42.5]]]], 1);
  assert.equal(out.length, 1);
});

test('if the union library fails while closing, the unclosed outline is returned', () => {
  const flaky = { union: (...a) => { if (a.length > 5) throw new Error('degenerate input'); return pc.union(...a); } };
  const out = ro.buildOutline([box(-116, 43, -115, 44), box(-115, 43, -114, 44)], flaky);
  assert.equal(out.length, 1);
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
