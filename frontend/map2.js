/*
 * MeshWars: front page (/), also reachable at /map2. Boots a MapLibre GL
 * map -- the renderer swap for the original Leaflet page, now kept at
 * /map-legacy (frontend/mc.js) -- and draws it with a self-hosted
 * PMTiles hillshade layer, which Leaflet has no equivalent for.
 *
 * Also carries: the site's theme system (theme.css + theme-toggle.js,
 * same as every other page, but defaulting to the neon/dark theme here
 * specifically -- see the boot snippet in map2.html), a single dark
 * basemap shared by both themes (gold used to sit on a light OSM
 * raster, which washed the baked hillshade out under a dark interface
 * -- gold is now the colour of the chrome and the pins, not of the
 * ground; see BASEMAP_ID), three self-hosted
 * PMTiles overlays (public lands, USFS roads/trails), all behind a small
 * layer-switcher panel (also visibility-toggled, so flipping a checkbox
 * never refetches a source), Places Worth Going markers and a slide-out
 * panel, and the territory scoreboard/roster/history/top-players panel
 * and winner banner ported from frontend/mc.js -- see the "Territory
 * panel" section below for what that port did and did not carry over.
 *
 * Team colours are gameplay, not branding: they are the same constant
 * regardless of theme and never touched by the theme code below.
 */

// ---------------------------------------------------------------------
// Failure reporting + boot checkpoints.
//
// The map has been seen never rendering at all on real mobile hardware
// (GrapheneOS Vanadium and Brave, stock Android Chrome) -- #loading-
// overlay stays up forever, with nothing in the console to say why. It
// works on desktop and in headless Chromium emulating a mobile
// viewport, so whatever fails only shows up on real devices. A first
// pass added a try/catch around `new maplibregl.Map(...)`,
// map.on('error'), a load timeout, and a webglcontextlost handler, but
// a later real-device load still hung with NONE of those firing: no
// /tiles/*.pmtiles requests, no /api/clientlog report, just the
// spinner past the timeout. That means execution stops somewhere
// between /config resolving and the load timeout ever being
// *scheduled* -- and the previous version scheduled that timeout AFTER
// `new maplibregl.Map(...)` returned, so a constructor that hangs
// (rather than throws) would produce exactly this signature, with the
// timeout's own setTimeout call never reached either.
//
// This section is deliberately placed as close to the very top of the
// file as possible, ahead of everything else including TEAM_COLORS --
// every one of the checkpoints below depends on it, including the
// first one (t0), which is meant to fire before almost anything else
// in the module has had a chance to throw. It exposes:
//
//   - sendClientLog/bootCheckpoint: a best-effort report to POST
//     /api/clientlog (app/clientlog_api.py), now via navigator.sendBeacon
//     (falling back to fetch) since sendBeacon is built to survive page
//     teardown -- a fetch, even with keepalive, is not as reliable on
//     mobile Chrome/Safari when the tab is backgrounded or the page is
//     being torn down.
//   - showMapErrorBanner: swaps #loading-overlay for #map-error-banner,
//     unchanged from before.
//   - a handful of boot checkpoints (kind: 'boot') at t0 (module top),
//     t1 (right after the unpkg globals are available to check), t2
//     (immediately before `new maplibregl.Map(...)`), t3 (immediately
//     after it returns), and t4 (inside map.on('load')) -- so a
//     real-device load shows exactly how far execution got instead of
//     nothing at all. These are opt-in only, behind ?debug=1 (see
//     DEBUG_REPORTING below); a normal visitor sends none of them.
//   - a visibilitychange report: if the tab is backgrounded before the
//     map has loaded, that's reported too (also ?debug=1 only, being a
//     kind: 'boot' report), since a visitor who locks their phone or
//     switches apps before any timer fires would otherwise leave no
//     trail either.
//
// Failure reports are NOT gated -- they fire for every visitor,
// always. Only the boot checkpoints are opt-in.
//
// None of this identifies the actual trigger -- that is still unknown
// -- it only narrows down where execution actually stops.

// 12s: comfortably longer than a slow cold TLS+tile fetch on a poor
// mobile connection takes to reach MapLibre's 'load' event (which fires
// once the initial style/sources are ready, well before every tile is
// in), short enough that a visitor whose map really has hung isn't left
// staring at the spinner for anywhere near the 30s periodic-refresh
// cadence before being told it isn't coming.
const MAP_LOAD_TIMEOUT_MS = 12000;

let mapFailureShown = false;

// Set true by map.on('load') (see main()); read by the load-timeout
// watchdog and by the visibilitychange reporter below. Module-scoped
// (not local to main()) so the visibilitychange listener -- registered
// here, before main() has even been called -- can see it.
let mapLoaded = false;

// Furthest boot checkpoint reached so far, read by the visibilitychange
// reporter below so a backgrounded-before-load report says how far
// execution got rather than just that it didn't finish.
let bootStage = 'start';

// Boot checkpoints are a debugging instrument, not production
// telemetry: on a healthy load they are pure noise, and firing 8+
// beacons per visitor would both bury real failures in the clientlog
// and add a request per page view for nothing. So they are opt-in
// behind the SAME ?debug=1 flag that loads eruda in map2.html -- one
// flag, one mental model: "?debug=1 turns diagnostics on".
//
// This gates ONLY `kind: 'boot'` reports. Every failure report
// (map_construct_failed, map_error_event, map_load_timeout,
// webgl_context_lost, register_place_icons_failed, load_handler_threw,
// main_failed, window_onerror, unhandled_rejection) stays
// unconditional for every visitor -- those are the entire reason the
// endpoint exists, and a failure that only reports itself when someone
// happened to append ?debug=1 is a failure nobody hears about.
//
// Evaluated once at module load rather than per call: location.search
// cannot change without a navigation, and a checkpoint must never be
// the thing that throws.
const DEBUG_REPORTING = (() => {
  try {
    return /(?:^|[?&])debug=1(?:&|$)/.test(location.search);
  } catch {
    return false;
  }
})();

// Swaps #loading-overlay for #map-error-banner. Idempotent: only the
// first failure does anything, since several of these paths (e.g. a
// map.on('error') followed by the load timeout it caused) can fire for
// the same underlying problem, and the banner should not flash or
// re-render on the second one.
function showMapErrorBanner() {
  if (mapFailureShown) return;
  mapFailureShown = true;
  const overlay = document.getElementById('loading-overlay');
  if (overlay) overlay.remove();
  const banner = document.getElementById('map-error-banner');
  if (banner) banner.hidden = false;
}

// Best-effort report to the server. Deliberately paranoid about never
// throwing itself -- a reporter that can fail loudly would turn "the
// map broke" into "the map broke AND so did reporting it" -- so
// everything from building the body to the send itself is swallowed.
// Fields are pre-truncated here too (the server enforces its own caps
// independently; this just avoids sending bytes that would only be
// discarded).
//
// navigator.sendBeacon is tried first: it is specifically designed to
// survive page teardown/backgrounding, which a fetch (even with
// keepalive: true) is not as reliably able to do on mobile browsers.
// sendBeacon has no way to set headers, so the JSON body is wrapped in
// a Blob with an explicit type instead -- app/clientlog_api.py parses
// the raw body as JSON regardless of Content-Type, so this changes
// nothing server-side. Falls back to the previous fetch-based send if
// sendBeacon is unavailable (very old browsers) or itself returns
// false (queue already full).
//
// Caps how many reports a single page load will ever send. A flaky
// connection can throw many tile errors in quick succession (see the
// map.on('error') gating in main()); without a cap that becomes a
// burst of requests the server's own 20/min limiter
// (app/clientlog_api.py) was only ever going to reject anyway. Raised
// from 12 to 24 to fit the t4d/t4f/t5/t9 overlay-removal diagnostics
// added alongside the t0-t4 boot checkpoints below.
const CLIENT_LOG_MAX_PER_LOAD = 24;
let clientLogSentCount = 0;

function sendClientLog(kind, message) {
  if (clientLogSentCount >= CLIENT_LOG_MAX_PER_LOAD) return;
  clientLogSentCount += 1;
  try {
    const body = JSON.stringify({
      kind: String(kind == null ? 'unknown' : kind).slice(0, 64),
      message: String(message == null ? '' : message).slice(0, 500),
      href: String((location && location.pathname) || '').slice(0, 200),
    });
    try {
      if (navigator && typeof navigator.sendBeacon === 'function') {
        const blob = new Blob([body], { type: 'application/json' });
        if (navigator.sendBeacon('/api/clientlog', blob)) return;
      }
    } catch {
      // Fall through to the fetch fallback below.
    }
    fetch('/api/clientlog', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
      keepalive: true,
    }).catch(() => {});
  } catch {
    // Never let a reporting failure become a second error.
  }
}

// Cheap, never-throwing boot markers -- `kind: 'boot'`, a short
// `message` unique per call site (see main() and t0/t1 below for where
// each fires). Updates bootStage first so the visibilitychange
// reporter always has the latest value even if sendClientLog itself
// somehow threw.
function bootCheckpoint(stage, message) {
  // bootStage is updated unconditionally even when reporting is off --
  // it costs nothing, and keeping it accurate means a debug-mode
  // session behaves identically to a normal one up to the send.
  bootStage = stage;
  if (!DEBUG_REPORTING) return;
  try {
    sendClientLog('boot', message || stage);
  } catch {
    // Never let a checkpoint become a second error.
  }
}

// Real numbers behind the t2/t3/t4 boot checkpoints below -- confirms
// or refutes, with actual device measurements rather than a guess made
// from a desktop browser where this has never reproduced, whether
// #map (and the WebGL canvas MapLibre draws into) has non-zero size at
// each stage. Compact k=v pairs, single line, to stay well inside the
// clientlog endpoint's 500-char field cap. `canvas` is optional --
// undefined/null before the map is constructed (t2) -- and its
// width/height are the backing-store pixel size (the canvas element's
// attributes), while canvas_cli is its CSS layout size, same
// distinction as #map's clientWidth/clientHeight vs. its
// getBoundingClientRect(). Every call site wraps this in its own
// try/catch and falls back to a plain stage-name checkpoint if
// anything here is missing or throws.
function mapGeomSnapshot(stage, canvas) {
  const el = document.getElementById('map');
  const rect = el && el.getBoundingClientRect ? el.getBoundingClientRect() : null;
  const parts = [
    stage,
    `map=${el ? el.clientWidth : '?'}x${el ? el.clientHeight : '?'}`,
    `rect_h=${rect ? Math.round(rect.height) : '?'}`,
  ];
  if (canvas) {
    parts.push(`canvas=${canvas.width}x${canvas.height}`);
    parts.push(`canvas_cli=${canvas.clientWidth}x${canvas.clientHeight}`);
  }
  parts.push(`win=${window.innerWidth}x${window.innerHeight}`);
  parts.push(`dpr=${window.devicePixelRatio}`);
  if (stage === 't4_load') {
    parts.push(`body_h=${document.body ? document.body.clientHeight : '?'}`);
  }
  return parts.join(' ');
}

// Real device evidence (see the section comment above) showed 'load'
// firing with healthy geometry, every network request succeeding, and
// no console errors -- yet #loading-overlay stayed up forever, because
// its removal previously sat AFTER setBoardMode/loadPlacesViewport/
// loadPlacesPanel inside map.on('load'), gating a startup-only overlay
// behind data calls that can stall or throw. Pulled out into its own
// idempotent function (guarded by loadingOverlayRemoved) so it can be
// called both immediately once 'load' fires (see main() below) AND, as
// a belt-and-braces fallback, from a one-shot map.on('idle') handler in
// case the 'load' path is somehow never reached -- either caller is
// harmless if the other already ran. The fade-out + setTimeout(...,
// 500) removal mechanism itself is unchanged from before.
let loadingOverlayRemoved = false;
function removeLoadingOverlay() {
  if (loadingOverlayRemoved) return;
  loadingOverlayRemoved = true;
  try {
    const loadingOverlay = document.getElementById('loading-overlay');
    bootCheckpoint('t4d_overlay', `t4d_overlay found=${loadingOverlay ? 1 : 0}`);
    if (!loadingOverlay) return;
    loadingOverlay.classList.add('fade-out');
    setTimeout(() => {
      try {
        loadingOverlay.remove();
        bootCheckpoint(
          't4f_removed',
          `t4f_removed gone=${document.getElementById('loading-overlay') ? 0 : 1}`
        );
      } catch {
        bootCheckpoint('t4f_removed', 't4f_removed gone=0');
      }
    }, 500);
  } catch {
    bootCheckpoint('t4d_overlay', 't4d_overlay error');
  }
}

// Page-wide safety net, not map-specific -- addEventListener rather
// than assigning window.onerror/onunhandledrejection so this can never
// clobber (or be clobbered by) some other handler. Reports only; it
// does not show the map error banner itself, since an error anywhere
// else on the page (theme-toggle.js, a browser extension, ...) is not
// evidence the map itself failed -- the map's own paths in main() call
// showMapErrorBanner() directly when they know that's what happened.
window.addEventListener('error', (e) => {
  sendClientLog('window_onerror', e && e.message);
});
window.addEventListener('unhandledrejection', (e) => {
  const reason = e && e.reason;
  const msg = reason && reason.message ? reason.message : String(reason);
  sendClientLog('unhandled_rejection', msg);
});

// Catches a visitor backgrounding the tab (locking the phone, switching
// apps, navigating away) before the map has loaded and before any
// timer has fired -- without this, that load simply vanishes with no
// trail at all instead of showing how far execution got.
document.addEventListener('visibilitychange', () => {
  if (!DEBUG_REPORTING) return;
  if (document.visibilityState === 'hidden' && !mapLoaded) {
    sendClientLog('boot', `hidden_after_${bootStage}`);
  }
});

// t0: the very first executable statement in this module past wiring
// up the reporting helpers directly above it (those are declarations,
// not side effects) -- if a future real-device load never reports even
// this, the module failed before doing anything at all, which rules
// out every checkpoint after it in one shot.
bootCheckpoint('t0_module', 't0_module');

// Same team colors as frontend/mc.js's TEAM_COLORS -- kept as a second
// copy on purpose, same reasoning as frontend/join.js: this page must
// load independently of mc.js.
const TEAM_COLORS = {
  RED: '#ff4136',
  GREEN: '#2ecc40',
  BLUE: '#3d8bfd',
  PURPLE: '#b10dc9',
  YELLOW: '#ffdc00',
  ORANGE: '#ff9020',
  PINK: '#f01ec0',
};

const TEAM_ORDER = Object.keys(TEAM_COLORS);

// Any name that belongs to a team is written in that team's colour --
// team names and player names alike. The colour IS the team, so it is
// never spelled out beside the name; `title` keeps it recoverable for
// anyone who cannot separate seven colours by eye.
function teamName(name, team) {
  const c = TEAM_COLORS[team];
  if (!c) return escapeHtml(name);
  return `<span class="mc-teamed" style="color:${c}" title="${escapeHtml(team)}">${escapeHtml(name)}</span>`;
}


// All three overlay archives -- public lands, USFS roads/trails, and
// the hillshade below -- now ship with the game itself (same-origin
// /tiles/, see app/api.py's tiles_dir mount) rather than being fetched
// from navi at runtime -- navi's archives got rebuilt in place, keeping
// the same filename, and a browser that already held byte ranges of
// the previous file would happily keep serving them against a file
// that had since changed shape underneath it. That showed up as a
// region silently missing rather than as an error. Bump TILE_REV
// whenever a served archive changes, so the URL changes and nothing
// stale can survive.
//
// The hillshade source used to be planet-dem.pmtiles, the one archive
// still on navi: a raw elevation DEM shaded in the browser at ~11.3MB
// per view, ninety-five percent of the page's weight. It is now
// meshwars-hillshade-alpha.pmtiles -- finished imagery, pre-rendered
// once across the play area at the dark theme's exaggeration -- so
// navi is out of the runtime path entirely. This is the second bake:
// the first (meshwars-hillshade.pmtiles, kept on disk as a rollback)
// stored opaque greyscale, which painted flat ground the same opaque
// grey as a shadowed ridge and washed out the whole map. This archive
// carries a real alpha channel -- converted losslessly from the same
// greyscale pixels, no DEM work re-run -- so flat ground is
// transparent again and only the relief itself darkens or lightens.
const TILE_REV = '20260825b';
const DEM_URL = `/tiles/meshwars-hillshade-alpha.pmtiles?r=${TILE_REV}`;
const PUBLIC_LANDS_URL = `/tiles/public-lands.pmtiles?r=${TILE_REV}`;
const USFS_TRAILS_ROADS_URL = `/tiles/usfs-trails-roads.pmtiles?r=${TILE_REV}`;

// Both themes now share ONE dark basemap (CARTO dark_all) -- gold used
// to point at the stock light OSM raster, which put a bright white map
// under a dark interface and washed the baked hillshade out. Gold is
// now the colour of the chrome and the place pins (PLACE_COLORS), not
// of the ground, so there is nothing left for a second basemap source
// to differ on; the old basemap-gold/basemap-neon pair (two sources,
// two layers, toggled by visibility in applyBasemapTheme) collapsed to
// this single always-visible source/layer.
const BASEMAP_ID = 'basemap';
const HILLSHADE_ID = 'hillshade';

// Team territory washes into the dark basemap/hillshade -- both themes
// now need the weight that used to be neon-only when gold still sat on
// a light basemap. Kept as a per-theme map (not a single constant) so
// the two can still be told apart if a future theme needs to.
const BOARD_FILL_OPACITY = { gold: 0.65, neon: 0.65 };
const BOARD_LINE_WIDTH = { gold: 2, neon: 2 };

// The baked hillshade archive carries real alpha -- transparent on flat
// ground, black/white toward shadow/highlight. Both themes sit on the
// same dark basemap now, so both get the full-strength value that used
// to be neon-only; gold's old 0.7 existed only to keep a light basemap
// from washing out, which no longer applies.
const HILLSHADE_OPACITY = { gold: 1.0, neon: 1.0 };

// Each checkbox id -> the style layer id(s) it toggles, and the
// minimum zoom its underlying data starts at (measured from the tile
// archives -- see the minzoom comment in setupOverlayLayers below).
// Layout visibility only -- no source is ever added or removed here,
// so toggling is instant and never refetches. Below its minimum zoom
// a layer has nothing to draw, so the switcher disables that entry
// instead of leaving a ticked box that silently shows nothing; see
// setupLayerSwitcher.
const LAYER_TOGGLES = [
  ['mw-layer-hillshade', [HILLSHADE_ID], 0],
  ['mw-layer-public-lands', ['public-lands-fill', 'public-lands-line'], 4],
  ['mw-layer-usfs-roads', ['usfs-roads-line'], 6],
  ['mw-layer-usfs-trails', ['usfs-trails-line'], 6],
  // park-boundaries-fill/line are included here too -- see
  // setupPlacesLayer -- so unchecking "Places" hides a park's outline
  // along with its glyph instead of leaving the outline drawn with no
  // marker to explain it. One toggle, both layers it actually governs.
  // minZoom 0 (always available): place markers have no hard zoom
  // cutoff any more -- they fade by icon-opacity instead (see
  // PLACE_ICON_OPACITY_ZOOM) -- so there is no zoom below which
  // checking this box would show literally nothing to grey it out for.
  // park-boundaries-fill is itself the click target now (a park's
  // whole box, not just a thin ring around its edge -- see
  // setupPlacesLayer and setupCellClickPopup's precedence comment):
  // unchecking "Places" must take that click handler out of play along
  // with the outline it answers for, the same reasoning as
  // -line/-labels above.
  ['mw-layer-places', ['places-icons-summit', 'places-icons-park', 'places-icons-landmark', 'places-labels', 'park-boundaries-fill', 'park-boundaries-line', 'park-boundaries-labels'], 0],
];

// Places Worth Going (docs/features/places.md). Three flat-colour
// shapes (summit=triangle, park=circle, landmark=diamond) was the
// original design; live feedback was that landmarks were grey and
// nearly invisible while parks, at their old size, dominated the map,
// and that a legend of three shapes in three greys asked a reader to
// learn a code. A circular pin with a glyph inside it was the next
// attempt; the user watching it live on the actual map rejected the
// circle itself -- "i want just the icon instead" -- so this is now a
// bare glyph silhouette (mountain/tree/star -- see PLACE_GLYPHS) with
// no enclosing shape at all, drawn straight onto the basemap and given
// its own outline/halo (PLACE_ICON_OUTLINE) for contrast in place of
// the pin fill that used to provide it. The glyph is now the ONLY
// thing separating the three tiers -- both colour and reveal zoom are
// now shared across all three (see below) precisely so nothing but the
// silhouette itself is left to tell them apart. Colour deliberately
// follows each theme's own accent family (gold on classic, cyan on
// neon) rather than staying constant across themes the way team
// colours do -- a place is a separate scoring layer from square
// ownership (TEAM_COLORS above), so reusing red/green/blue here would
// read as if a place belonged to a team, but there is no reason for it
// to fight the theme the way a team colour would. All three tiers
// share the SAME shade -- the lightest, most saturated end of the
// family (theme.css's --mw-gold-light / neon's cyan-light token) --
// chosen purely for visibility against busy terrain at a tiny size;
// there is no dimmer shade held back for a "less important" tier,
// because the previous attempt at that (a shade ladder across tiers)
// is exactly what made landmarks hard to see in the first place.
const PLACE_COLORS = {
  gold: '#EDD39F',   // theme.css --mw-gold-light (classic)
  neon: '#2BE8E0',   // theme.css --mw-gold (neon's deeper cyan token -- see
                      // PLACE_ICON_OUTLINE below for why not -light)
};

// Base pixel size (not degrees -- this must stay a constant SCREEN
// size across zoom, like any map marker, unlike the board squares
// which are drawn to true ground scale). ONE size, not one per tier --
// an earlier version sized summit largest/park mid/landmark smallest as
// a secondary hierarchy cue, explicitly dropped watching it live: with
// colour already shared (see PLACE_COLORS above) and the glyph the only
// intended differentiator, a size difference on top just made summit
// read as "more important" again through the back door. Equal NOMINAL
// size does not by itself mean equal APPARENT size -- see PLACE_GLYPHS'
// own per-shape normalization below, which is doing the real work of
// making the three look like siblings; this constant is just the
// shared budget they each normalize into. Neon gets a larger baseline
// than gold -- a marker sized to read against gold's light basemap
// washes out against neon's dark hillshade and the orange territory
// squares underneath; same per-theme pattern as BOARD_FILL_OPACITY/
// BOARD_LINE_WIDTH below. Zoom-interpolated on top of this via
// PLACE_ICON_SIZE_ZOOM -- see there for why.
const PLACE_ICON_PX = { gold: 22, neon: 30 };

// Outline/halo stroked around each glyph's own edge in drawPlaceIcon.
// With the enclosing pin gone, this is now the ONLY thing separating a
// bare gold or cyan glyph from a dark hillshade and the orange
// territory squares underneath -- it used to just edge a solid pin
// fill, a lighter job. Both values were strengthened over the pin-era
// numbers (was width 1/1.5, opacity .55/.95) to do that heavier job:
// gold gets a stronger dark outline (still soft, but no longer barely
// there) against its light basemap; neon keeps its light halo, opaque
// now rather than near-opaque, same problem BOARD_FILL_OPACITY/
// BOARD_LINE_WIDTH solved for the board layer, same per-theme pattern.
const PLACE_ICON_OUTLINE = {
  gold: { color: 'rgba(0,0,0,0.65)', width: 1.5 },
  // Was a light/white halo (rgba(244,241,232,1), width 2) -- on the
  // shared dark basemap a pale glyph with a pale outline has nothing
  // to contain it and reads as a glow, not a shape. Same dark-outline
  // treatment as gold now holds the cyan glyph's edge instead.
  neon: { color: 'rgba(0,0,0,0.65)', width: 1.5 },
};

// Park boundaries (app/places_api.py's `park_boundaries` -- a matched
// PAD-US polygon at or above one grid cell, docs/features/places.md).
// A big park still keeps its own glyph, fading in by
// PLACE_ICON_OPACITY_ZOOM the same as every other place (see
// setupPlacesLayer) -- the outline is drawn ON TOP of that, only once
// zoomed in enough for the shape to mean anything, so the park is
// still findable by its glyph before the outline itself has appeared.
// MIN_BOUNDARY_ZOOM must match app/places_api.py's own
// constant of the same name -- it is both the client's own layer
// minzoom AND the zoom this page starts asking the server for boundary
// geometry at all (fetchPlacesInViewport passes map.getZoom() in the
// `zoom` param), so a mismatch here would either fetch boundary data
// that never draws or draw a layer that never has data.
const MIN_BOUNDARY_ZOOM = 11;

// Colour reuses PLACE_COLORS[theme] (the same shade every glyph now
// draws in, park included -- see PLACE_COLORS above) rather than
// inventing a new one -- this is the same park, just drawn two ways at
// once. Fill stays close to nothing
// on purpose: a boundary is a quiet outline, not a second filled area
// competing with the team squares (BOARD_FILL_OPACITY, 0.45/0.65) or
// reading like the public-lands overlay (a flat, unrelated green wash,
// see setupOverlayLayers) -- per-theme values follow the same
// BOARD_FILL_OPACITY/PLACE_ICON_OUTLINE pattern: neon needs a touch
// more of both to hold up against the dark hillshade, gold already
// reads fine at the lower value.
const PARK_BOUNDARY_FILL_OPACITY = { gold: 0.05, neon: 0.08 };
const PARK_BOUNDARY_LINE_WIDTH = { gold: 1, neon: 1.5 };
const PARK_BOUNDARY_LINE_OPACITY = { gold: 0.7, neon: 0.85 };

// Marker screen size is also zoom-interpolated, on top of the
// per-theme base pixel size above: with ~80,000 places now seeded
// (43,639 parks + 30,408 landmarks + 6,487 summits, plus rotating live
// ones), a size tuned to read at zoom 13 close-in tiles into a solid
// mass at a whole-region view if left constant. Same curve for both
// themes -- the per-theme PX value above already carries the theme
// difference.
//
// CAPPED AT 1.0, deliberately never higher -- this is the fix for a
// real blur bug, not a style choice. registerPlaceIcons rasterizes
// each glyph once, at PLACE_ICON_PX * the screen's own devicePixelRatio
// (see there), and icon-size then scales THAT raster for display: at
// icon-size 1.0 the raster is shown at its own native resolution
// (crisp on any dpr, since it was authored at that dpr already), but
// any icon-size ABOVE 1.0 stretches it past native resolution and
// blurs, exactly like blowing up a small image -- an earlier version
// of this curve peaked at 1.15 (a 15% upscale at zoom 13 and, since
// interpolate clamps past its last stop, at every zoom above that too,
// including the map's own 17 maximum), which is precisely why the
// glyphs read as blurry zoomed in. Peaking at 1.0 here means the
// rasterized size (PLACE_ICON_PX) IS the size needed at the closest
// zoom the map allows -- every zoom below that only ever shrinks the
// same raster down, never up, so there is no zoom level, including 15
// and 17, where this can go soft.
const PLACE_ICON_SIZE_ZOOM = ['interpolate', ['linear'], ['zoom'], 3, 0.15, 6, 0.35, 8, 0.55, 9, 0.75, 10, 0.9, 11, 0.95, 13, 1.0];

const PLACE_TYPES = ['summit', 'park', 'landmark'];

// The click layer id for each tier's marker, computed once here rather
// than at every call site -- setupPlacesLayer's own marker click
// handler, its park-boundary click handler, and setupCellClickPopup's
// board handler all need the identical list, to answer the identical
// question ("did this same pixel also hit a place marker, the single
// most specific thing a click can land on?"). Duplicating the
// `.map()` at each site risks the three drifting if PLACE_TYPES ever
// changes.
const PLACE_LAYER_IDS = PLACE_TYPES.map((t) => `places-icons-${t}`);

// All three tiers reveal at the SAME zoom, and there is no hard cutoff
// at all any more: the layer's own minzoom is left at the map's own
// floor (see main()'s `minZoom: 4`), and decluttering a whole-region
// view of ~80,000 places is instead handled entirely by icon-opacity
// (PLACE_ICON_OPACITY_ZOOM below) -- fading out, not disappearing at a
// boundary. Two earlier attempts at the regional-declutter problem both
// got explicitly overridden watching this live: a staggered per-tier
// reveal zoom (summit from 0, park from 10, landmark from 8/11) read as
// three different tiers with three different rules; a single shared
// reveal zoom plus a dot-below/glyph-above size threshold fixed the
// "three different rules" complaint but replaced it with a new one --
// "trees turn to circles when zooming out" -- because a `step`
// expression is a discontinuous jump: one frame is a glyph, the next is
// a completely different shape. icon-opacity is a paint property
// MapLibre interpolates smoothly every frame at zero extra cost (no
// separate image per opacity step the way the dot/glyph swap needed
// one), so nothing ever changes SHAPE, only how visible it is -- there
// is no discontinuity for a "pop" or a "the trees broke" read to happen
// at all.
//
// Stops (0 at the map's own zoom 4 floor, ramped up to full by 10, held
// at 1 above that -- interpolate clamps past the last stop):
//   4 -> 0     (map's own minimum: fully invisible, nothing to declutter)
//   6 -> 0.05  (whole-region view: barely perceptible, reads as empty)
//   8 -> 0.35  (a sub-region has narrowed into view: starting to show)
//   9 -> 0.7
//   10 -> 1    (full strength from here up -- "somewhere you'd go")
// Chosen by looking at the zoom-6/10/13 reference screenshots: zoom 6
// needed to still read as an honest empty regional view (same result
// the old hard zoom-8 cutoff gave), while zoom 10 and 13 both needed to
// be at full strength, matching every earlier round's legibility work
// -- so the curve had to reach 1.0 by 10, not somewhere past it.
const PLACE_ICON_OPACITY_ZOOM = ['interpolate', ['linear'], ['zoom'], 4, 0, 6, 0.05, 8, 0.35, 9, 0.7, 10, 1];

// Below this zoom the map is showing a whole region and place NAMES
// would overlap into noise; icons still draw at every zoom (subject to
// the viewport fetch's own result cap), just unlabeled until the
// reader has zoomed in enough for a name to mean something.
const PLACE_LABEL_MIN_ZOOM = 12;

// Cap on how far the map is ever allowed to zoom in when framing a
// player search result (doPlayerFind) -- same cap and same reasoning
// as frontend/mc.js's MAX_FIT_ZOOM: a single player holding one or two
// 300m cells has a tiny bounds box, and fitting to it with no cap zooms
// in far enough to fill the screen with one giant colored square and no
// context.
const MAX_FIT_ZOOM = 13;

// Matches the breakpoint mc.css's collapsible-header media query uses
// for "phone-width" (see setMcCollapsed below).
const NARROW_BREAKPOINT_PX = 600;

// t1: right after the two unpkg <script> tags in map2.html (maplibregl,
// pmtiles) are the last thing to run before this module, and right
// before the first use of either global below. `typeof` rather than a
// direct reference so this can never itself throw a ReferenceError if
// one of the CDN scripts failed to load -- that outcome is exactly what
// this checkpoint exists to catch, so it has to survive it. Directly
// tests the CDN-blocked hypothesis: if either reads 'undefined' here,
// `new pmtiles.Protocol()` / `maplibregl.addProtocol` immediately below
// throws before main() ever runs, and every checkpoint after this one
// (and every failure path added later, since those are wired up inside
// main()) simply never fires -- which is exactly the silent-hang
// signature seen on real mobile.
bootCheckpoint('t1_libs', `t1_libs maplibregl=${typeof maplibregl} pmtiles=${typeof pmtiles}`);

// pmtiles.js registers no protocol on its own -- this wires the
// pmtiles:// URL scheme into MapLibre's request pipeline so a raster
// source can point straight at a single .pmtiles archive instead of a
// z/x/y tile template. The same protocol also serves the vector
// overlay archives below.
const pmtilesProtocol = new pmtiles.Protocol();
maplibregl.addProtocol('pmtiles', pmtilesProtocol.tile);

function boundsToPolygon(cell) {
  const { south, west, north, east } = cell;
  return {
    type: 'Feature',
    properties: {
      cell_id: cell.cell_id,
      team: cell.owner_team,
    },
    geometry: {
      type: 'Polygon',
      coordinates: [[
        [west, south],
        [east, south],
        [east, north],
        [west, north],
        [west, south],
      ]],
    },
  };
}

// MeshCore's /api/mc/board returns the cell array directly; Meshtastic's
// /get-nodes returns {coverage, repeaters} -- same reasoning and same
// unwrap as mc.js's fetchBoardCells. Both cell shapes (south/west/
// north/east/owner_team/cell_id) match boundsToPolygon either way.
async function fetchBoard(c) {
  const res = await fetch(c.boardEndpoint);
  if (!res.ok) throw new Error(`board fetch failed: ${res.status}`);
  const data = await res.json();
  const cells = Array.isArray(data) ? data : (Array.isArray(data.coverage) ? data.coverage : []);
  return {
    type: 'FeatureCollection',
    features: cells.map(boundsToPolygon),
  };
}

// ===== Territory panel (ported from frontend/mc.js) =====
//
// The scoreboard/roster/history/top-players/player-lookup panel and the
// winner banner, both of which lived only on the Leaflet page (frontend/
// mc.js's buildScoreboardControl et al.) until this page became the
// front page. Same markup, same mc.css/coverage.css classes (loaded by
// map2.html alongside map2.css), same information -- rebuilt here
// because mc.js's version anchors itself via L.control/L.DomUtil/
// L.DomEvent, none of which exist without a Leaflet map. The panel div
// is built once and appended straight to <body>, positioned by
// map2.css (#mc-scoreboard-position) the way Leaflet's own topright
// control corner used to position it for free.
//
// The Meshtastic/MeshCore board SWITCH (.mc-switch-row) at the top of
// mc.js's panel IS ported below (see PROTOCOLS, cfg(), setBoardMode()).
// Both boards read the same board/board-fill MapLibre source -- there
// is no second source, just a re-fetch into the one already created in
// main() -- so switching carries the source's tolerance: 0 setting
// (see main()'s map.addSource('board', ...)) for free on both boards,
// nothing to duplicate. Every string this panel shows (board title,
// button labels, endpoints) comes from whichever half of PROTOCOLS is
// active, same table shape as mc.js's own PROTOCOLS.
//
// Per-cell click popups (bindCellPopup / buildCellPopupHtml in mc.js)
// ARE ported now -- see buildCellPopupHtml/buildRepeaterSectionHtml
// below and setupCellClickPopup, wired to the 'board-fill' layer in
// main(). Correction to an earlier note here that called this "not
// ported" and a gap flagged for later: promoting this map to the front
// page silently dropped a real feature (a square's owner/captures/
// repeaters on click), not a pre-existing gap shared by both pages.
// cellEndpoint/repeaterLabel below are the same two PROTOCOLS entries
// mc.js's table carries for exactly this popup.
const PROTOCOLS = {
  meshcore: {
    protocol: 'mc',
    // "Score" not "Territory" -- this number is squares held PLUS
    // check-in points PLUS exploration points (see scores_for() in
    // app/mc_api.py), never squares alone, so the heading must not
    // imply it is a territory/square count. See the Breakdown modal
    // (openBreakdownModal) for where that split is actually shown.
    boardTitle: 'MeshCore Score',
    topButtonLabel: 'Top Operators',
    topCaptureLabel: 'Wardrivers',
    topCheckinLabel: 'NetOps',
    topExplorerLabel: 'Explorer',
    lookupPlaceholder: 'player name',
    lookupHelp: 'Search by player name.',
    // See mc.js's own PROTOCOLS comment for why "Repeaters" is
    // MeshCore's term for this evidence.
    repeaterLabel: 'Repeaters heard',
    boardEndpoint: '/api/mc/board',
    scoresEndpoint: '/api/mc/scores',
    cellEndpoint: (id) => `/api/mc/cell/${encodeURIComponent(id)}`,
    historyEndpoint: '/api/mc/history',
    rosterEndpoint: '/api/mc/players',
    findEndpoint: (q) => `/api/mc/find?name=${encodeURIComponent(q)}`,
    topEndpoint: '/api/mc/top',
    topCheckinEndpoint: '/api/mc/top-checkins',
    topExplorerEndpoint: '/api/mc/top-explorer',
    seasonEndpoint: '/api/mc/season',
  },
  meshtastic: {
    protocol: 'mt',
    boardTitle: 'Meshtastic Score',
    topButtonLabel: 'Top Operators',
    topCaptureLabel: 'Wardrivers',
    topCheckinLabel: 'NetOps',
    topExplorerLabel: 'Explorer',
    lookupPlaceholder: 'player name',
    lookupHelp: 'Search by player name.',
    // Meshtastic's own vocabulary for the same evidence -- see mc.js's
    // own PROTOCOLS comment.
    repeaterLabel: 'MQTT feeders heard',
    boardEndpoint: '/get-nodes',
    scoresEndpoint: '/scores',
    cellEndpoint: (id) => `/cell/${encodeURIComponent(id)}`,
    historyEndpoint: '/history',
    rosterEndpoint: '/teams',
    findEndpoint: (q) => `/find?name=${encodeURIComponent(q)}`,
    topEndpoint: '/top',
    topCheckinEndpoint: '/top-checkins',
    topExplorerEndpoint: '/top-explorer',
    seasonEndpoint: '/season',
  },
};

const REFRESH_INTERVAL_MS = 30000;

// ---- module state (territory panel) ----
let mode = 'meshcore'; // key into PROTOCOLS -- placeholder only. The
                        // real value is decided in main(), before the
                        // map is even constructed, in priority order:
                        // an explicit ?board= link, then this browser's
                        // remembered choice (BOARD_MODE_KEY, below),
                        // then /config's mc_default_view -- same source
                        // of truth mc.js's loadConfig() reads. Every
                        // time it changes after that (the toggle
                        // buttons, the first-visit board-choice modal)
                        // the new value is written back to
                        // BOARD_MODE_KEY so the next load starts there.
                        // mc.js's own 'mapView' key is unrelated -- that
                        // remembers the camera position, never the
                        // board.
function cfg() {
  return PROTOCOLS[mode];
}

// Remembered per browser, same throw-survives idiom as
// NOTICE_DISMISSED_KEY/LAYERS_COLLAPSED_KEY further down this file:
// private browsing or blocked site data must never break the map, it
// just means the choice is not remembered next time.
const BOARD_MODE_KEY = 'mwBoardMode';

// Only 'meshcore'/'meshtastic' are ever trusted back out of storage --
// anything else (hand-edited, cleared mid-value, a future build's
// value read by an older one) is treated the same as nothing stored,
// never passed through, so a corrupt value can't wedge the map into an
// invalid mode.
function getStoredBoardMode() {
  try {
    const v = localStorage.getItem(BOARD_MODE_KEY);
    return v === 'meshcore' || v === 'meshtastic' ? v : null;
  } catch {
    return null;
  }
}

function rememberBoardMode(newMode) {
  try {
    localStorage.setItem(BOARD_MODE_KEY, newMode);
  } catch {
    // Storage unavailable (private browsing, quota) -- the choice just
    // is not remembered next time, same failure direction as the other
    // localStorage keys on this page.
  }
}
let scoreboardTitleEl = null;
let scoreboardTopBtn = null;
let scoreboardBody = null;
let scoreboardPanelEl = null;
let scoreboardHeaderBtn = null;
let scoreboardSummaryEl = null;
let scoreboardLookupInput = null;
let scoreboardEndsAt = null;

function formatTs(ts) {
  if (!ts) return 'unknown';
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch (e) {
    return 'unknown';
  }
}

function formatCountdown(secondsRemaining) {
  if (secondsRemaining <= 0) return 'closing';
  const days = Math.floor(secondsRemaining / 86400);
  const hours = Math.floor((secondsRemaining % 86400) / 3600);
  const mins = Math.floor((secondsRemaining % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${mins}m`;
  return `${mins}m`;
}

// A team's number, wherever it's shown: squares held PLUS check-in
// points PLUS exploration (Places Worth Going) points (app/mc_scoring.py's
// team_totals()) -- the combined figure that actually decides the season
// now, not squares alone. Falls back to tiles-only for the all-zero seed
// row renderScoreboard(null) hands out before the first real fetch,
// which has no checkin_points/explorer_points/total fields at all yet.
function teamTotal(t) {
  if (typeof t.total === 'number') return t.total;
  return t.tiles ?? 0;
}

// See mc.js's own teamBreakdown for the full rationale -- states all
// three components, even when a component is zero.
function teamBreakdown(t) {
  const tiles = t.tiles ?? 0;
  const pts = t.checkin_points ?? 0;
  const explorer = t.explorer_points ?? 0;
  const squareWord = tiles === 1 ? 'square' : 'squares';
  const pointWord = pts === 1 ? 'point' : 'points';
  const explorerWord = explorer === 1 ? 'point' : 'points';
  return `${tiles} ${squareWord} + ${pts} check-in ${pointWord} + ${explorer} exploration ${explorerWord}`;
}

function teamBreakdownCompact(t) {
  const tiles = t.tiles ?? 0;
  const pts = t.checkin_points ?? 0;
  const explorer = t.explorer_points ?? 0;
  return `${tiles}+${pts}+${explorer}`;
}

function bindBreakdownToggle(el) {
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    const showingBreakdown = el.classList.toggle('mc-showing-breakdown');
    el.textContent = showingBreakdown ? el.dataset.compact : el.dataset.total;
  });
}

function attachBreakdownToggle(el, totalText, compactText) {
  el.dataset.total = totalText;
  el.dataset.compact = compactText;
  el.textContent = totalText;
  bindBreakdownToggle(el);
}

function leadingTeam(teams) {
  let best = null;
  teams.forEach((t) => {
    if (!best || teamTotal(t) > teamTotal(best)) best = t;
  });
  return best;
}

function setMcCollapsed(collapsed) {
  if (!scoreboardPanelEl || !scoreboardHeaderBtn) return;
  scoreboardPanelEl.classList.toggle('mc-collapsed', collapsed);
  scoreboardHeaderBtn.setAttribute('aria-expanded', String(!collapsed));
}

function renderScoreboard(data) {
  if (!scoreboardBody) return;
  scoreboardBody.replaceChildren();

  const teams = (data && Array.isArray(data.teams) && data.teams.length)
    ? data.teams
    : TEAM_ORDER.map((t) => ({ team: t, tiles: 0 }));

  if (scoreboardSummaryEl) {
    const lead = leadingTeam(teams);
    scoreboardSummaryEl.textContent = lead ? `${lead.team} ${teamTotal(lead)}` : '';
  }

  const ordered = teams.slice().sort((a, b) => (
    teamTotal(b) - teamTotal(a) ||
    TEAM_ORDER.indexOf(a.team) - TEAM_ORDER.indexOf(b.team)
  ));

  ordered.forEach((entry) => {
    const row = document.createElement('div');
    row.className = 'mc-row';

    const dot = document.createElement('span');
    dot.className = 'mc-dot';
    dot.style.background = TEAM_COLORS[entry.team] || '#888';
    row.appendChild(dot);

    const label = document.createElement('span');
    label.className = 'mc-team-label';
    label.textContent = `${entry.team}: `;
    row.appendChild(label);

    const count = document.createElement('span');
    count.className = 'mc-team-count';
    count.title = teamBreakdown(entry);
    attachBreakdownToggle(count, String(teamTotal(entry)), teamBreakdownCompact(entry));
    row.appendChild(count);

    scoreboardBody.appendChild(row);
  });
}

function tickCountdown() {
  const el = document.getElementById('mc-countdown');
  if (!el || !scoreboardEndsAt) return;
  const now = Math.floor(Date.now() / 1000);
  el.textContent = formatCountdown(scoreboardEndsAt - now);
}

async function loadScoreboard() {
  try {
    const res = await fetch(cfg().scoresEndpoint);
    if (!res.ok) return;
    const data = await res.json();
    renderScoreboard(data);
    scoreboardEndsAt = data.ends_at || null;
  } catch (err) {
    console.warn('MeshWars map2: scoreboard refresh failed', err);
  }
}

async function fetchHistorySeasons() {
  const res = await fetch(cfg().historyEndpoint);
  if (!res.ok) return [];
  const data = await res.json();
  return Array.isArray(data) ? data : [];
}

// Normalizes both roster shapes into a common Map<team, [{display_name}]>
// -- MeshCore's /api/mc/players is a flat list this groups itself;
// Meshtastic's /teams is already grouped, just keyed differently. Same
// split as mc.js's fetchRosterByTeam.
async function fetchRosterByTeam() {
  const c = cfg();
  const byTeam = new Map();
  const res = await fetch(c.rosterEndpoint);
  if (!res.ok) return byTeam;
  const data = await res.json();
  if (c.protocol !== 'mc') {
    const teams = (data && data.teams) || {};
    Object.keys(teams).forEach((team) => {
      byTeam.set(team, (teams[team] || []).map((p) => ({ display_name: p.display_name })));
    });
    return byTeam;
  }
  const players = data;
  if (!Array.isArray(players)) return byTeam;
  players.forEach((p) => {
    const team = p.team || 'UNKNOWN';
    if (!byTeam.has(team)) byTeam.set(team, []);
    byTeam.get(team).push({ display_name: p.display_name });
  });
  return byTeam;
}

// ===== Player search (Find) =====
//
// Find used to stop here once a player's cells were located -- a player
// holding no cells got a one-line "holds no cells right now" and
// nothing else, which meant Find was really "top-of-board locator", not
// a real player lookup: it never showed points, and it went blank
// entirely for anyone with checkin/Explorer points but zero captures.
// This is now this page's only way for a player OUTSIDE the Top
// Operators rankings to see their own numbers, so it must not go blank
// for them. Point breakdown reuses the exact same "tap to see the
// split" affordance team totals already use (mc-tally-count/
// bindBreakdownToggle, see openHistoryModal above) rather than
// inventing a new interaction or any new CSS.
async function doPlayerFind(value) {
  const resultEl = document.getElementById('mc-lookup-result');
  if (!resultEl) return;
  const query = (value || '').trim();
  if (!query) { resultEl.replaceChildren(); return; }
  resultEl.textContent = 'Searching...';

  try {
    const res = await fetch(cfg().findEndpoint(query));
    if (res.status === 404) {
      resultEl.textContent = `Not found: ${query}`;
      return;
    }
    // Privacy hardening (2026-09): /find and /api/mc/find now require a
    // signed-in session (see app/sessions.py's require_session, wired
    // into both routes) -- looking a named player's location up is
    // exactly the person-to-place link a visitor must sign in to make.
    // A logged-out visitor gets 401 here, not an error; say so plainly
    // rather than falling into the generic "Search failed." below,
    // which would read as broken rather than as "sign in to do this."
    if (res.status === 401 || res.status === 403) {
      resultEl.textContent = 'Sign in to search players by name.';
      return;
    }
    if (res.status === 429) {
      resultEl.textContent = 'Too many searches -- try again in a moment.';
      return;
    }
    if (!res.ok) {
      resultEl.textContent = 'Search failed.';
      return;
    }
    const data = await res.json();

    // Zoom the map only when they currently hold cells -- unchanged
    // from before this pass, just no longer gating whether the point
    // breakdown below renders too.
    if (data.bounds && data.tiles_held && window.__mwMap) {
      const b = data.bounds;
      // MapLibre bounds are [[west, south], [east, north]] -- Leaflet's
      // equivalent call (mc.js's doPlayerFind) uses [lat, lng] order
      // instead; only the coordinate order changes here, not the
      // padding or the MAX_FIT_ZOOM cap.
      window.__mwMap.fitBounds([[b.west, b.south], [b.east, b.north]], { padding: 24, maxZoom: MAX_FIT_ZOOM });
    }

    const plural = data.tiles_held === 1 ? '' : 's';
    const holdsText = data.tiles_held
      ? `${data.display_name} (${data.team}) holds ${data.tiles_held} cell${plural}.`
      : `${data.display_name} (${data.team}) holds no cells right now.`;

    const captures = data.tiles_held || 0; // capture points == squares currently held, same figure team_tile_counts() sums per team
    const checkins = data.checkin_points || 0;
    const explorer = data.explorer_points || 0;
    const total = data.total_points ?? (captures + checkins + explorer);
    const compact = `${captures}+${checkins}+${explorer}`;
    const splitTitle = `${captures} capture + ${checkins} check-in + ${explorer} Explorer points`;

    const activityBits = [];
    if (data.last_checkin_net_date) activityBits.push(`last check-in ${data.last_checkin_net_date}`);
    if (data.last_position_ts) activityBits.push(`last seen ${formatTs(data.last_position_ts)}`);
    // Plain text, no new class -- inherits .mc-lookup-result's own
    // font-size/color like everything else in this box already does.
    const activityText = activityBits.length ? ` (${escapeHtml(activityBits.join(', '))})` : '';

    resultEl.innerHTML =
      `<div>${escapeHtml(holdsText)}</div>` +
      `<div>Points: <span class="mc-tally-count" data-total="${escapeHtml(total)}" data-compact="${escapeHtml(compact)}" title="${escapeHtml(splitTitle)}">${escapeHtml(total)}</span>${activityText}</div>`;
    resultEl.querySelectorAll('.mc-tally-count').forEach(bindBreakdownToggle);
  } catch (err) {
    resultEl.textContent = 'Search failed.';
  }
}

// ===== Modal (History / Roster / Top) =====

let mcModalEl = null;
let mcModalTitleEl = null;
let mcModalBodyEl = null;

function ensureModal() {
  if (mcModalEl) return;
  mcModalEl = document.createElement('div');
  mcModalEl.id = 'mc-modal';
  mcModalEl.className = 'mc-modal';
  mcModalEl.innerHTML = `
    <div class="mc-modal-inner">
      <div class="mc-modal-header">
        <span id="mc-modal-title"></span>
        <button type="button" class="mc-modal-close" id="mc-modal-close">&times;</button>
      </div>
      <div id="mc-modal-body"></div>
    </div>
  `;
  document.body.appendChild(mcModalEl);
  mcModalTitleEl = mcModalEl.querySelector('#mc-modal-title');
  mcModalBodyEl = mcModalEl.querySelector('#mc-modal-body');
  mcModalEl.querySelector('#mc-modal-close').addEventListener('click', closeMcModal);
  mcModalEl.addEventListener('click', (e) => {
    if (e.target === mcModalEl) closeMcModal();
  });
}

function openMcModal(title) {
  ensureModal();
  mcModalTitleEl.textContent = title;
  mcModalBodyEl.replaceChildren();
  const loading = document.createElement('div');
  loading.className = 'mc-modal-loading';
  loading.textContent = 'Loading...';
  mcModalBodyEl.appendChild(loading);
  mcModalEl.style.display = 'flex';
  return mcModalBodyEl;
}

function closeMcModal() {
  if (mcModalEl) mcModalEl.style.display = 'none';
}

function showModalMessage(body, className, text) {
  body.replaceChildren();
  const el = document.createElement('div');
  el.className = className;
  el.textContent = text;
  body.appendChild(el);
}

async function openHistoryModal() {
  const body = openMcModal('Past Seasons');
  try {
    const seasons = await fetchHistorySeasons();
    if (!seasons.length) {
      showModalMessage(body, 'mc-modal-empty', 'No completed seasons yet.');
      return;
    }
    const rows = seasons.map((s) => {
      const started = s.started_at ? new Date(s.started_at * 1000).toLocaleDateString() : '?';
      const ended = s.ends_at ? new Date(s.ends_at * 1000).toLocaleDateString() : '?';
      const teams = Array.isArray(s.teams) ? s.teams : [];
      const tallyText = teams
        .filter((t) => teamTotal(t) > 0)
        .map((t) => `${teamName(t.team, t.team)} <span class="mc-tally-count" data-total="${escapeHtml(teamTotal(t))}" data-compact="${escapeHtml(teamBreakdownCompact(t))}" title="${escapeHtml(teamBreakdown(t))}">${escapeHtml(teamTotal(t))}</span>`)
        .join(', ') || 'no tiles recorded';
      return `<tr>
        <td>#${escapeHtml(s.id)}</td>
        <td>${escapeHtml(started)} &ndash; ${escapeHtml(ended)}</td>
        <td class="mc-winner-cell">${escapeHtml(s.winner || '-')}</td>
        <td>${tallyText}</td>
      </tr>`;
    }).join('');
    body.innerHTML = `<table class="mc-history-table">
      <thead><tr><th>Season</th><th>Dates</th><th>Winner</th><th>Final tallies</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
    body.querySelectorAll('.mc-tally-count').forEach(bindBreakdownToggle);
  } catch (err) {
    showModalMessage(body, 'mc-modal-error', `Failed to load: ${err.message}`);
  }
}

async function openRosterModal() {
  const body = openMcModal('Player Roster');
  try {
    const byTeam = await fetchRosterByTeam();
    if (byTeam.size === 0) {
      showModalMessage(body, 'mc-modal-empty', 'No players yet.');
      return;
    }
    const teamOrder = TEAM_ORDER.filter((t) => byTeam.has(t))
      .concat([...byTeam.keys()].filter((t) => !TEAM_ORDER.includes(t)));
    const sections = teamOrder.map((team) => {
      const list = (byTeam.get(team) || []).slice()
        .sort((a, b) => (a.display_name || '').localeCompare(b.display_name || ''));
      const rows = list.map((p) => `<tr><td>${teamName(p.display_name, team)}</td></tr>`).join('');
      const color = TEAM_COLORS[team] || '#888';
      return `<div class="mc-roster-team">
        <h3 style="color:${color};">${escapeHtml(team)} &mdash; ${list.length}</h3>
        <table class="mc-roster-table"><tbody>${rows}</tbody></table>
      </div>`;
    }).join('');
    body.innerHTML = `<div class="mc-roster-grid">${sections}</div>`;
  } catch (err) {
    showModalMessage(body, 'mc-modal-error', `Failed to load: ${err.message}`);
  }
}

// Points breakdown per team -- squares held, check-in points, exploration
// (Places Worth Going) points, and the total, side by side for every
// team. See mc.js's own openBreakdownModal for the full rationale: same
// c.scoresEndpoint fetch and the same teamTotal/teamBreakdownCompact
// helpers the scoreboard itself uses, so this can never disagree with
// it, and the same modal chrome as History/Roster/Top (openMcModal/
// closeMcModal) -- no separate open/close logic here.
async function openBreakdownModal() {
  const c = cfg();
  const body = openMcModal('Score Breakdown');
  try {
    const res = await fetch(c.scoresEndpoint);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const teams = (data && Array.isArray(data.teams) && data.teams.length)
      ? data.teams
      : TEAM_ORDER.map((t) => ({ team: t, tiles: 0, checkin_points: 0, explorer_points: 0, total: 0 }));

    const ordered = teams.slice().sort((a, b) => (
      teamTotal(b) - teamTotal(a) ||
      TEAM_ORDER.indexOf(a.team) - TEAM_ORDER.indexOf(b.team)
    ));

    const rows = ordered.map((t) => `<tr>
        <td><span class="mc-dot" style="background:${TEAM_COLORS[t.team] || '#888'}"></span>${teamName(t.team, t.team)}</td>
        <td>${escapeHtml(t.tiles ?? 0)}</td>
        <td>${escapeHtml(t.checkin_points ?? 0)}</td>
        <td>${escapeHtml(t.explorer_points ?? 0)}</td>
        <td><strong>${escapeHtml(teamTotal(t))}</strong></td>
      </tr>`).join('');
    body.innerHTML = `<table class="mc-history-table">
      <thead><tr><th>Team</th><th>Squares</th><th>Check-in</th><th>Exploration</th><th>Total</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  } catch (err) {
    showModalMessage(body, 'mc-modal-error', `Failed to load: ${err.message}`);
  }
}

let topModalTab = 'captures';

function topTabSpecs() {
  const c = cfg();
  return {
    captures: {
      label: c.topCaptureLabel,
      endpoint: c.topEndpoint,
      valueKey: 'captures',
      valueHeader: 'Captures',
      emptyText: 'No capture activity yet.',
    },
    checkins: {
      label: c.topCheckinLabel,
      endpoint: c.topCheckinEndpoint,
      valueKey: 'points',
      valueHeader: 'Points',
      emptyText: 'No check-in activity yet.',
      extraKey: 'streak',
      extraHeader: 'Streak',
    },
    explorer: {
      label: c.topExplorerLabel,
      endpoint: c.topExplorerEndpoint,
      valueKey: 'points',
      valueHeader: 'Points',
      emptyText: 'No Explorer activity yet.',
    },
  };
}

async function renderTopModalTab() {
  const tabBody = mcModalBodyEl.querySelector('#mc-top-tab-body');
  if (!tabBody) return;
  const spec = topTabSpecs()[topModalTab];
  tabBody.innerHTML = '<div class="mc-modal-loading">Loading...</div>';

  try {
    const res = await fetch(spec.endpoint);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const rows = await res.json();
    if (!Array.isArray(rows) || rows.length === 0) {
      tabBody.innerHTML = '';
      const el = document.createElement('div');
      el.className = 'mc-modal-empty';
      el.textContent = spec.emptyText;
      tabBody.appendChild(el);
      return;
    }
    const trs = rows.map((r, i) => {
      const color = TEAM_COLORS[r.team] || '#888';
      const extra = spec.extraKey
        ? `<td>${escapeHtml(r[spec.extraKey] == null ? '—' : `${r[spec.extraKey]}x`)}</td>`
        : '';
      return `<tr>
        <td>${i + 1}</td>
        <td>${escapeHtml(r.display_name)}</td>
        <td><span class="mc-dot" style="background:${color}"></span>${escapeHtml(r.team)}</td>
        <td>${escapeHtml(r[spec.valueKey])}</td>
        ${extra}
      </tr>`;
    }).join('');
    const extraHead = spec.extraHeader ? `<th>${escapeHtml(spec.extraHeader)}</th>` : '';
    tabBody.innerHTML = `<table class="mc-history-table">
      <thead><tr><th>#</th><th>Player</th><th>Team</th><th>${escapeHtml(spec.valueHeader)}</th>${extraHead}</tr></thead>
      <tbody>${trs}</tbody>
    </table>`;
  } catch (err) {
    tabBody.innerHTML = '';
    const el = document.createElement('div');
    el.className = 'mc-modal-error';
    el.textContent = `Failed to load: ${err.message}`;
    tabBody.appendChild(el);
  }
}

async function openTopModal() {
  const specs = topTabSpecs();
  topModalTab = 'captures';
  const body = openMcModal('Season Rankings');
  body.innerHTML = `
    <div class="mc-modal-tabs">
      <button type="button" class="mc-modal-tab active" data-tab="captures">${escapeHtml(specs.captures.label)}</button>
      <button type="button" class="mc-modal-tab" data-tab="checkins">${escapeHtml(specs.checkins.label)}</button>
      <button type="button" class="mc-modal-tab" data-tab="explorer">${escapeHtml(specs.explorer.label)}</button>
    </div>
    <div id="mc-top-tab-body"></div>
  `;
  body.querySelectorAll('.mc-modal-tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      if (btn.dataset.tab === topModalTab) return;
      topModalTab = btn.dataset.tab;
      body.querySelectorAll('.mc-modal-tab').forEach((b) => b.classList.toggle('active', b === btn));
      renderTopModalTab();
    });
  });
  await renderTopModalTab();
}

// ===== Winner banner =====
//
// Top-center overlay on the map itself (#mc-winner-banner in
// map2.html) -- same element, same position, same behavior as the
// Leaflet page's. Shows nothing once the season's display window
// elapses, same as the backend already decides via winner_banner_active
// / settings.winner_banner_hours -- this never has its own opinion
// about when to hide it.
function renderWinnerBanner(banner) {
  const el = document.getElementById('mc-winner-banner');
  if (!el) return;
  if (!banner) {
    el.style.display = 'none';
    el.replaceChildren();
    return;
  }

  el.replaceChildren();

  const isTie = !banner.winner || banner.winner === 'TIE';
  const tag = document.createElement('span');
  tag.className = 'mc-winner-tag';
  tag.style.background = isTie ? '#888' : (TEAM_COLORS[banner.winner] || '#888');
  tag.textContent = isTie ? 'TIE' : `${banner.winner} WINS`;
  el.appendChild(tag);

  const counts = document.createElement('span');
  counts.className = 'mc-winner-counts';
  const teams = Array.isArray(banner.teams) ? banner.teams : [];
  teams.forEach((t) => {
    const entry = document.createElement('span');
    entry.className = 'mc-winner-count-entry';
    const dot = document.createElement('span');
    dot.className = 'mc-dot';
    dot.style.background = TEAM_COLORS[t.team] || '#888';
    entry.appendChild(dot);
    const countSpan = document.createElement('span');
    countSpan.className = 'mc-winner-count';
    countSpan.title = teamBreakdown(t);
    attachBreakdownToggle(countSpan, String(teamTotal(t)), teamBreakdownCompact(t));
    entry.appendChild(countSpan);
    counts.appendChild(entry);
  });
  el.appendChild(counts);

  const dates = document.createElement('span');
  dates.className = 'mc-winner-dates';
  dates.textContent = `Season #${banner.season_id}`;
  el.appendChild(dates);

  el.style.display = 'flex';
}

async function refreshWinnerBanner() {
  try {
    const res = await fetch(cfg().seasonEndpoint);
    if (!res.ok) { renderWinnerBanner(null); return; }
    const data = await res.json();
    renderWinnerBanner(data.winner_banner || null);
  } catch (err) {
    console.warn('MeshWars map2: winner banner refresh failed', err);
    renderWinnerBanner(null);
  }
}

// ===== Scoreboard control =====
//
// Board switch, collapsible header with live summary, seven-team
// scoreboard with color dots, season countdown, player lookup, History/
// Roster, Refresh, and a ranked-players button -- everything mc.js's
// buildScoreboardControl builds, including the board-switch row
// (.mc-switch-row, same markup/classes/order as mc.js -- row one of the
// card, outside .mc-panel-content so it survives the phone collapse).
// Built once and appended straight into <body>; map2.css positions it
// (#mc-scoreboard-position) the way Leaflet's own topright control
// corner used to for mc.js. The title span and top-players button start
// empty/unlabeled, same as mc.js's own markup -- setBoardMode (called
// once right after this, with the /config-derived default mode) fills
// them in via applyProtocolChrome(), so there is never a MeshCore-
// labeled flash before a Meshtastic default takes over.
function buildScoreboardControl(map) {
  const div = document.createElement('div');
  div.id = 'mc-scoreboard-position';
  div.className = 'mc-scoreboard';
  div.innerHTML = `
    <div class="mc-switch-row" id="mc-switch-row">
      <button type="button" id="mc-toggle-meshtastic" class="mc-switch-btn">Meshtastic</button>
      <button type="button" id="mc-toggle-meshcore" class="mc-switch-btn">MeshCore</button>
    </div>
    <button type="button" class="mc-row mc-title mc-header-btn" id="mc-header-btn" aria-expanded="true">
      <span class="mc-header-title-text" id="mc-header-title-text"></span>
      <span class="mc-header-right">
        <span class="mc-header-summary" id="mc-header-summary"></span>
        <span class="mc-header-caret" aria-hidden="true">&#9662;</span>
      </span>
    </button>
    <div class="mc-panel-content" id="mc-panel-content">
      <div class="mc-scoreboard-body"></div>
      <div class="mc-row mc-countdown">Ends in&nbsp;<span id="mc-countdown">--</span></div>
      <div class="mc-row mc-lookup-row">
        <input type="text" id="mc-lookup-input" placeholder="player name" />
        <button type="button" id="mc-lookup-btn">Find</button>
      </div>
      <div id="mc-lookup-result" class="mc-lookup-result"></div>
      <div class="mc-row"><a href="#" id="mc-history-link">History</a> &nbsp;|&nbsp; <a href="#" id="mc-roster-link">Roster</a> &nbsp;|&nbsp; <a href="#" id="mc-breakdown-link">Breakdown</a></div>
      <div class="mc-row mc-actions">
        <button type="button" id="mc-refresh-btn">Refresh map</button>
      </div>
      <div class="mc-row mc-actions">
        <button type="button" id="mc-top-btn"></button>
      </div>
      <div class="mc-row mc-actions">
        <button type="button" id="mc-places-btn" aria-expanded="false" aria-controls="mc-places-section">Places</button>
      </div>
      <div id="mc-places-section" class="mc-places-section">
        <div class="mc-row mc-places-section-title">Nearby Places</div>
        <ul id="mw-places-list" class="mw-places-list"></ul>
      </div>
    </div>
  `;
  document.body.appendChild(div);

  scoreboardBody = div.querySelector('.mc-scoreboard-body');
  scoreboardPanelEl = div;
  scoreboardHeaderBtn = div.querySelector('#mc-header-btn');
  scoreboardSummaryEl = div.querySelector('#mc-header-summary');
  scoreboardTitleEl = div.querySelector('#mc-header-title-text');
  scoreboardLookupInput = div.querySelector('#mc-lookup-input');

  // Leaflet's L.DomEvent.disableClickPropagation/disableScrollPropagation
  // stopped clicks/scrolls on a control from reaching the map underneath
  // it -- there is no map-wide equivalent to opt out of on MapLibre, so
  // this stops the same events at the panel's own root instead.
  ['click', 'dblclick', 'mousedown', 'touchstart', 'wheel'].forEach((evt) => {
    div.addEventListener(evt, (e) => e.stopPropagation());
  });

  scoreboardTopBtn = div.querySelector('#mc-top-btn');
  const topBtn = scoreboardTopBtn;

  // Board switch -- row one of the card, always visible, never inside
  // .mc-panel-content, so it survives the phone collapse below. Does
  // NOT touch the camera: setBoardMode only swaps which board's data
  // is fetched into the existing 'board' source and refreshes the
  // panel's numbers, same as the constraint the owner gave for this
  // port -- the view stays exactly where the visitor left it.
  div.querySelector('#mc-toggle-meshtastic').addEventListener('click', (e) => {
    e.stopPropagation();
    setBoardMode('meshtastic', map);
    rememberBoardMode(mode); // read back the normalized value setBoardMode just set
  });
  div.querySelector('#mc-toggle-meshcore').addEventListener('click', (e) => {
    e.stopPropagation();
    setBoardMode('meshcore', map);
    rememberBoardMode(mode);
  });

  div.querySelector('#mc-header-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    setMcCollapsed(!scoreboardPanelEl.classList.contains('mc-collapsed'));
  });

  div.querySelector('#mc-history-link').addEventListener('click', (e) => {
    e.preventDefault();
    openHistoryModal();
  });
  div.querySelector('#mc-roster-link').addEventListener('click', (e) => {
    e.preventDefault();
    openRosterModal();
  });
  div.querySelector('#mc-breakdown-link').addEventListener('click', (e) => {
    e.preventDefault();
    openBreakdownModal();
  });

  const lookupBtn = div.querySelector('#mc-lookup-btn');
  const doLookup = () => doPlayerFind(scoreboardLookupInput.value);
  lookupBtn.addEventListener('click', doLookup);
  scoreboardLookupInput.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') doLookup();
  });
  scoreboardLookupInput.addEventListener('keyup', (e) => e.stopPropagation());
  scoreboardLookupInput.addEventListener('keypress', (e) => e.stopPropagation());

  div.querySelector('#mc-refresh-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    loadBoardData(map);
    loadScoreboard();
  });

  topBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    openTopModal();
  });

  // Places Worth Going (docs/features/places.md): a fourth row in the
  // same .mc-actions stack as Refresh map/Top Operators, expanding the
  // live-places list IN PLACE below it rather than sliding out a
  // separate panel -- the previous floating panel sat in the same
  // top-right corner as this one and, depending on the day, ended up
  // clipped behind it or behind the fixed nav bar. Nested inside this
  // card there is nothing left for it to hide behind. Top Operators
  // (topBtn, above) opens a modal instead of expanding in place, so
  // there is no real "both expanded" state to reconcile -- the modal
  // covers the whole screen regardless of whether this section is open,
  // and closing the modal leaves this section exactly as it was.
  const placesBtn = div.querySelector('#mc-places-btn');
  const placesSection = div.querySelector('#mc-places-section');
  placesBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = !placesSection.classList.contains('open');
    placesSection.classList.toggle('open', open);
    placesBtn.setAttribute('aria-expanded', String(open));
    placesBtn.textContent = open ? 'Hide places' : 'Places';
  });

  return div;
}

// ===== Board switch (mode) =====
//
// Ported from mc.js's updateToggleButtons/applyProtocolChrome/setMode.
// Text-only chrome update -- title, top-players button label, lookup
// placeholder/help, and clearing any stale Find result -- plus a data
// refresh into the panel and the existing 'board' source. Deliberately
// does not touch the camera (no fitBounds/flyTo, unlike mc.js's own
// setMode) per this port's explicit constraint: switching boards must
// never move the map out from under a visitor.
function updateToggleButtons() {
  const btnMeshtastic = document.getElementById('mc-toggle-meshtastic');
  const btnMeshcore = document.getElementById('mc-toggle-meshcore');
  if (btnMeshtastic) btnMeshtastic.classList.toggle('active', mode === 'meshtastic');
  if (btnMeshcore) btnMeshcore.classList.toggle('active', mode === 'meshcore');
}

function applyProtocolChrome() {
  const c = cfg();
  if (scoreboardTitleEl) scoreboardTitleEl.textContent = c.boardTitle;
  if (scoreboardTopBtn) scoreboardTopBtn.textContent = c.topButtonLabel;
  if (scoreboardLookupInput) {
    scoreboardLookupInput.placeholder = c.lookupPlaceholder;
    scoreboardLookupInput.title = c.lookupHelp;
  }
  const resultEl = document.getElementById('mc-lookup-result');
  if (resultEl) resultEl.textContent = '';
}

function setBoardMode(newMode, map) {
  mode = newMode === 'meshtastic' ? 'meshtastic' : 'meshcore';
  applyProtocolChrome();
  updateToggleButtons();
  loadBoardData(map);
  loadScoreboard();
  refreshWinnerBanner();
  // Place claims are per board, so the pins have to be refetched too --
  // otherwise switching boards leaves the other board's colours on them.
  loadPlacesViewport(map);
  loadPlacesPanel(map);
}

// ===== First-visit board choice =====
//
// Shown exactly once per browser: only when main() finds neither a
// linked ?board= nor a remembered BOARD_MODE_KEY (see main()'s own
// comment on that check). Every other modal on this page -- the update
// notice, History, Roster, Top Operators -- closes on an X, a backdrop
// click, or Escape. This one does not: it is a single two-option
// question with no wrong answer to walk away from, and a dismiss that
// left BOARD_MODE_KEY unset would just reopen the identical modal on
// the very next load, which is worse than making the visitor answer it
// once. So there is no close button, wrap has no click listener, and
// Escape is not handled at all (onBoardChoiceKeydown below only ever
// acts on Tab) -- picking Meshtastic or MeshCore is the only way out.
let boardChoiceModalEl = null;

// Keeps Tab cycling between the two options instead of escaping onto
// the page underneath -- there is nothing else focusable inside the
// modal to include in the cycle, and no dismiss control to worry about
// leaving out of it.
function onBoardChoiceKeydown(e) {
  if (e.key !== 'Tab' || !boardChoiceModalEl) return;
  const opts = boardChoiceModalEl.querySelectorAll('.mw-board-choice-option');
  if (opts.length < 2) return;
  const first = opts[0];
  const last = opts[opts.length - 1];
  if (e.shiftKey) {
    if (document.activeElement === first) {
      e.preventDefault();
      last.focus();
    }
  } else if (document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

// Static markup only (no operator- or player-authored text anywhere in
// it), so this is built the same way buildScoreboardControl's card is --
// one innerHTML template, then querySelector to wire it up -- rather
// than showNoticeModal's element-by-element/.textContent construction,
// which exists specifically to keep operator-authored title/body out of
// innerHTML. Nothing here is ever untrusted input.
function showBoardChoiceModal(map) {
  const wrap = document.createElement('div');
  wrap.className = 'mw-board-choice-modal';
  wrap.innerHTML = `
    <div class="mw-board-choice-modal-inner" role="dialog" aria-modal="true" aria-labelledby="mw-board-choice-title">
      <div class="mw-board-choice-modal-header" id="mw-board-choice-title">Which board do you want to see?</div>
      <p class="mw-board-choice-modal-intro">MeshWars runs a separate board for each of two mesh radio protocols. Pick the one that matches the radio you carry; you can switch anytime from the toggle in the corner.</p>
      <div class="mw-board-choice-options">
        <button type="button" class="mw-board-choice-option" id="mw-board-choice-meshtastic">
          <span class="mw-board-choice-option-title">Meshtastic</span>
          <span class="mw-board-choice-option-desc">The board for players running Meshtastic firmware.</span>
        </button>
        <button type="button" class="mw-board-choice-option" id="mw-board-choice-meshcore">
          <span class="mw-board-choice-option-title">MeshCore</span>
          <span class="mw-board-choice-option-desc">The board for players running MeshCore firmware.</span>
        </button>
      </div>
    </div>
  `;
  document.body.appendChild(wrap);
  boardChoiceModalEl = wrap;

  const choose = (chosen) => {
    rememberBoardMode(chosen);
    // mapLoaded (module-scoped, set true by map.on('load') in main())
    // tells whether there is already a board on screen to swap live. If
    // the map has not loaded yet, just set `mode` directly -- main()'s
    // own map.on('load') handler calls setBoardMode(mode, map) once the
    // style finishes loading and picks this up then; calling
    // setBoardMode here first would reach into a 'board' source that
    // does not exist until that handler adds it, for no benefit since
    // that same call is coming regardless.
    if (mapLoaded) {
      setBoardMode(chosen, map);
    } else {
      mode = chosen;
    }
    document.removeEventListener('keydown', onBoardChoiceKeydown);
    wrap.remove();
    boardChoiceModalEl = null;
    // main() held loadNotice() back specifically because this modal was
    // still up -- see its own comment on needsBoardChoice. Now that the
    // question is answered and this modal is gone, the notice is free
    // to run exactly the check it always runs (dismissed-version and
    // hasAwardParams) and show itself if it's still due; nothing here
    // pre-empts or forces that decision.
    loadNotice();
  };

  wrap.querySelector('#mw-board-choice-meshtastic').addEventListener('click', () => choose('meshtastic'));
  wrap.querySelector('#mw-board-choice-meshcore').addEventListener('click', () => choose('meshcore'));
  document.addEventListener('keydown', onBoardChoiceKeydown);

  // Focus moves into the modal on open, same as showNoticeModal's own
  // closeBtn.focus() -- there is no close button here, so the first
  // option is the natural landing spot instead.
  wrap.querySelector('#mw-board-choice-meshtastic').focus();
}

// ---- Places Worth Going (docs/features/places.md) ----------------------

function placeToFeature(p) {
  return {
    type: 'Feature',
    properties: {
      id: p.id, type: p.type, name: p.name, points: p.points,
      // Drives the icon's colour (placeIconExpression). Absent rather
      // than null when unclaimed, so the match expression falls through
      // to the theme glyph.
      ...(p.claimed_by_team ? { claimed_by_team: p.claimed_by_team } : {}),
    },
    geometry: { type: 'Point', coordinates: [p.lon, p.lat] },
  };
}

async function fetchPlacesInViewport(bounds, zoom) {
  const params = new URLSearchParams({
    north: bounds.getNorth(), south: bounds.getSouth(),
    west: bounds.getWest(), east: bounds.getEast(),
    // Server only computes/returns park_boundaries at all once zoom is
    // present and >= its own MIN_BOUNDARY_ZOOM (app/places_api.py) --
    // sent unconditionally rather than only above the threshold so the
    // two constants staying in sync is visible by reading either file,
    // not by remembering to gate the param here too.
    zoom,
    // Claims are scoped to the board that earned them, so the colours
    // have to follow the board the map is showing.
    board: mode,
  });
  const res = await fetch(`/api/places?${params}`);
  if (!res.ok) throw new Error(`places fetch failed: ${res.status}`);
  return res.json();
}

async function fetchPlacesNear(lat, lon) {
  const params = new URLSearchParams({ lat, lon, limit: 30 });
  const res = await fetch(`/api/places/near?${params}`);
  if (!res.ok) throw new Error(`places/near fetch failed: ${res.status}`);
  return res.json();
}

// One canvas path per tier, drawn centered on (cx, cy) and scaled to
// fit within radius g (the glyph budget -- see drawPlaceIcon). Bold,
// simple, single-fill-pass silhouettes on
// purpose: these render as small as ~22-30 CSS px (see PLACE_ICON_PX)
// at low zoom, so any silhouette with fine detail (a snow-capped peak,
// individual pine needles, a five-point star with thin arms) turns to
// mud at that size. Each is picked to be unmistakable from the
// other two even as a blurry thumbnail: the mountain is wide and low
// (twin overlapping peaks), the tree is tall and narrow with a trunk
// stub, the star is the only one with concave points -- three
// different silhouette OUTLINES, not just three different fills of the
// same rough blob, so they stay apart at every zoom the fade
// (PLACE_ICON_OPACITY_ZOOM) leaves them at all visible.
const PLACE_GLYPHS = {
  // Twin overlapping peaks -- wide and flat, both peaks sharing roughly
  // the same baseline. Reads as "range" rather than "single point" even
  // tiny, and its wide/short silhouette is the opposite of the tree's
  // tall/narrow one below.
  summit(ctx, cx, cy, g) {
    const baseY = cy + g * 0.6;
    ctx.moveTo(cx - g, baseY);
    ctx.lineTo(cx - g * 0.32, cy - g * 0.4);
    ctx.lineTo(cx + g * 0.08, baseY);
    ctx.closePath();
    ctx.moveTo(cx - g * 0.22, baseY);
    ctx.lineTo(cx + g * 0.32, cy - g * 0.85);
    ctx.lineTo(cx + g, baseY);
    ctx.closePath();
  },
  // A stacked, three-tier conifer over a short trunk block -- two
  // earlier attempts both failed watching this live: a single wide
  // triangle plus a trunk line read as an up-arrow (a triangle-over-a-
  // stem is exactly that common icon's shape, regardless of the
  // triangle's own proportions), and a rounded 3-circle "bush" canopy
  // read as a face/skull (the gaps between the circles' outlines
  // become eye-shaped holes once stroked). Three flat-bottomed
  // triangles, each apex overlapping down into the tier above so the
  // wider shoulders of every lower tier show as a visible STEP, is the
  // classic layered-Christmas-tree silhouette -- multiple distinct
  // tiers read as "tree" precisely because neither an arrow nor a
  // mountain (two peaks, not three stacked/shrinking ones) has that
  // shape, and there is no smooth rounded outline for it to be
  // mistaken for a face.
  park(ctx, cx, cy, g) {
    const trunkW = g * 0.2, trunkTop = cy + g * 0.55, trunkBottom = cy + g * 0.85;
    ctx.moveTo(cx - trunkW, trunkBottom);
    ctx.lineTo(cx + trunkW, trunkBottom);
    ctx.lineTo(cx + trunkW, trunkTop);
    ctx.lineTo(cx - trunkW, trunkTop);
    ctx.closePath();
    ctx.moveTo(cx, cy - g);
    ctx.lineTo(cx + g * 0.45, cy - g * 0.25);
    ctx.lineTo(cx - g * 0.45, cy - g * 0.25);
    ctx.closePath();
    ctx.moveTo(cx, cy - g * 0.5);
    ctx.lineTo(cx + g * 0.68, cy + g * 0.15);
    ctx.lineTo(cx - g * 0.68, cy + g * 0.15);
    ctx.closePath();
    ctx.moveTo(cx, cy - g * 0.12);
    ctx.lineTo(cx + g * 0.9, trunkTop);
    ctx.lineTo(cx - g * 0.9, trunkTop);
    ctx.closePath();
  },
  // Standard five-point star -- the only glyph with concave points, so
  // it never reads as a rounded blob the way a shrunk mountain or tree
  // can. Normalized against the other two by actual pixel-area, not by
  // eye: measured via an offscreen canvas at a shared reference budget
  // (fill each glyph, count non-transparent pixels), summit and park
  // land within 2% of each other already (their different silhouette
  // shapes happen to use their own bounding box about equally
  // efficiently) -- landmark at outerR=g came in far under both, and at
  // an earlier 0.8g (tuned back when PLACE_ICON_PX still gave landmark
  // its own, larger, per-tier size) came in at little more than half
  // their filled area. 0.95 is the measured match: close to full
  // radius, because a star's concave notches always cost real filled
  // area a solid triangle does not pay for the same reach -- matching
  // envelope size, not raw fill density, is what actually reads as
  // "the same size" for a spiky shape next to a solid one. Confirmed by
  // eye afterward, side by side at equal size, not just by the numbers.
  landmark(ctx, cx, cy, g) {
    const outerR = g * 0.95, innerR = g * 0.95 * 0.42, spikes = 5;
    let rot = -Math.PI / 2;
    const step = Math.PI / spikes;
    ctx.moveTo(cx + Math.cos(rot) * outerR, cy + Math.sin(rot) * outerR);
    for (let i = 0; i < spikes; i++) {
      rot += step;
      ctx.lineTo(cx + Math.cos(rot) * innerR, cy + Math.sin(rot) * innerR);
      rot += step;
      ctx.lineTo(cx + Math.cos(rot) * outerR, cy + Math.sin(rot) * outerR);
    }
    ctx.closePath();
  },
};

// Draws a tiny raster icon for one place tier on an offscreen canvas
// and hands it to MapLibre via map.addImage -- no sprite sheet or
// external asset file, generated at runtime instead, same
// self-contained spirit as everything else this page ships with. A
// symbol layer (not a fill layer of ground polygons the way the board
// squares are drawn) is what keeps these a constant PIXEL size across
// zoom: a place marker is a marker, not a to-scale shape on the ground.
//
// No enclosing pin or circle any more -- the glyph itself (from
// PLACE_GLYPHS, filled with `color`) IS the whole icon, with `outline`
// (a {color, width} pair, PLACE_ICON_OUTLINE) stroked around its own
// edge as a halo for contrast against the basemap, the same job a
// solid pin fill used to do for free. No dot fallback any more either
// (see PLACE_ICON_OPACITY_ZOOM) -- the glyph is the ONLY thing this
// function ever draws now.
//
// dpr is the screen's own window.devicePixelRatio, passed in by
// registerPlaceIcons rather than hardcoded -- THE FIX for a real blur
// bug, not a style choice. This used to hardcode "2" for both the
// canvas resolution (dim = sizePx*2) and the pixelRatio handed to
// map.addImage, regardless of the actual screen: on any display
// reporting a higher devicePixelRatio (most phones report 3, many
// laptops report 2.5+), MapLibre renders its own tiles at that
// screen's real pixel density but this raster was never authored at
// more than 2x, so a real hidpi screen was upscaling a raster that was
// already lower-resolution than the screen needed -- exactly the
// "blurry when zoomed in" symptom, and the worse the screen, the worse
// the blur. Rasterizing at the screen's OWN dpr (floored at 2 so a
// standard 1x display still gets a generously large source image, per
// PLACE_ICON_SIZE_ZOOM's own no-upscale-past-1.0 fix alongside this
// one) means the source image always has at least as many physical
// pixels as the screen can show, so icon-size <= 1.0 never has to
// stretch past native resolution on ANY screen.
function drawPlaceIcon(type, color, sizePx, outline, dpr) {
  const canvas = document.createElement('canvas');
  // Rounded to an integer BEFORE it's used anywhere below -- a
  // fractional dpr (2.625 on many real Android phones, not just a
  // desktop testing artifact) made this fractional too (e.g.
  // 22*2.625=57.75), and canvas.width/height silently truncate a
  // fractional assignment to an integer backing store while dim
  // itself stayed fractional. That mismatch fed forward into
  // getImageData(0,0,dim,dim) (which itself gets truncated back to
  // the canvas's real integer size, so the pixel DATA was always
  // correctly sized) and, separately and fatally, into the `width`/
  // `height` this function reports back to registerPlaceIcons for
  // map.addImage -- MapLibre validates data.length against
  // width*height*4 using the fractional value it was told, computes
  // a different number than the data it actually received, and
  // throws a RangeError from inside its own event dispatch. That
  // throw happens with no console error, no window.onerror, and no
  // map 'error' event (MapLibre's internal listener dispatch swallows
  // it), silently aborting whatever else was running in the same
  // map.on('load') handler -- see the try/catch registerPlaceIcons is
  // now called through below for the belt-and-braces half of this
  // fix. Rounding once, up front, keeps every dimension derived from
  // `dim` (canvas size, getImageData rect, reported width/height)
  // exactly self-consistent for ANY dpr, integer or not.
  const dim = Math.round(sizePx * dpr);
  canvas.width = dim;
  canvas.height = dim;
  const ctx = canvas.getContext('2d');
  const cx = dim / 2, cy = dim / 2;
  // Glyph budget leaves just enough margin for the outline stroke
  // itself not to clip against the canvas edge -- there is no pin fill
  // eating into this any more, so the glyph gets almost the whole
  // canvas footprint. Outline width is scaled by dpr the same way the
  // glyph itself is (CSS px -> device px), not by the old fixed *2.
  const g = dim / 2 - outline.width * dpr - 1;

  ctx.fillStyle = color;
  ctx.strokeStyle = outline.color;
  ctx.lineWidth = outline.width * dpr;
  ctx.lineJoin = 'round';
  ctx.beginPath();
  PLACE_GLYPHS[type](ctx, cx, cy, g);
  ctx.fill();
  ctx.stroke();

  return { data: ctx.getImageData(0, 0, dim, dim).data, width: dim, height: dim };
}

// Registers both themes' icon images up front (place-icon-<type>-gold/
// -neon) so applyBasemapTheme can flip which set each places-icons-
// <type> layer points at with a plain setLayoutProperty, the same
// visibility-swap-not-rebuild pattern the gold/neon basemap rasters
// use -- see applyBasemapTheme. dpr is read ONCE here (not inside
// drawPlaceIcon per call) and handed to both the canvas resolution and
// map.addImage's own pixelRatio option -- these two MUST agree, since
// pixelRatio is what tells MapLibre how many raster pixels correspond
// to one CSS pixel; passing a mismatched pair would reintroduce the
// same blur this whole function exists to fix, just via a different
// mismatch. Floored at 2 rather than passed through raw: a floor of 1
// would make a completely ordinary 1x monitor the WORST-resolution
// source of all three, when it is by far the most common screen this
// page will actually render on.
function registerPlaceIcons(map) {
  const dpr = Math.max(2, window.devicePixelRatio || 1);
  for (const theme of Object.keys(PLACE_ICON_PX)) {
    const color = PLACE_COLORS[theme];
    const sizePx = PLACE_ICON_PX[theme];
    const outline = PLACE_ICON_OUTLINE[theme];
    for (const type of PLACE_TYPES) {
      const icon = drawPlaceIcon(type, color, sizePx, outline, dpr);
      map.addImage(`place-icon-${type}-${theme}`, icon, { pixelRatio: dpr });
      // One tinted glyph per team, so a claimed place can wear the
      // claimer's colour. icon-color would be cheaper but only works on
      // SDF images, and these are drawn as full-colour canvases -- so
      // the variants are drawn up front instead. 3 types x 7 teams x 2
      // themes, once at boot.
      for (const team of TEAM_ORDER) {
        map.addImage(
          `place-icon-${type}-${theme}-${team}`,
          drawPlaceIcon(type, TEAM_COLORS[team], sizePx, outline, dpr),
          { pixelRatio: dpr });
      }
    }
  }
}

// A place wears the colour of the team that most recently CLAIMED it,
// falling back to the theme colour when nobody has. Attribution only --
// the points stay with whoever earned them (see app/places_api.py).
function placeIconExpression(type, theme) {
  const expr = ['match', ['get', 'claimed_by_team']];
  for (const team of TEAM_ORDER) {
    expr.push(team, `place-icon-${type}-${theme}-${team}`);
  }
  expr.push(`place-icon-${type}-${theme}`);
  return expr;
}

function setupPlacesLayer(map) {
  map.addSource('places', {
    type: 'geojson',
    data: { type: 'FeatureCollection', features: [] },
  });

  // Park boundaries (see PARK_BOUNDARY_* above and
  // app/places_api.py's park_boundaries) -- a separate source/pair of
  // layers from the marker source above, added first so 'board-fill'
  // (the team squares) still draws over it, same beforeId pattern
  // setupOverlayLayers uses for public lands/USFS. Fill and line paint
  // are set to the gold values here as a safe initial default, same as
  // places-icons-* above defaulting to currentTheme() before the boot
  // sequence's first applyBasemapTheme call actually themes it.
  map.addSource('park-boundaries', {
    type: 'geojson',
    data: { type: 'FeatureCollection', features: [] },
  });
  // One Point feature per boundary-backed park, rebuilt in
  // loadPlacesViewport (see buildParkLabelPoints) rather than read
  // straight off the park-boundaries polygon source -- see
  // park-boundaries-labels' addLayer call below for why a Point source
  // exists at all here.
  map.addSource('park-boundaries-label-points', {
    type: 'geojson',
    data: { type: 'FeatureCollection', features: [] },
  });
  map.addLayer({
    id: 'park-boundaries-fill',
    type: 'fill',
    source: 'park-boundaries',
    minzoom: MIN_BOUNDARY_ZOOM,
    paint: {
      'fill-color': PLACE_COLORS[currentTheme()],
      'fill-opacity': PARK_BOUNDARY_FILL_OPACITY.gold,
    },
  }, 'board-fill');
  map.addLayer({
    id: 'park-boundaries-line',
    type: 'line',
    source: 'park-boundaries',
    minzoom: MIN_BOUNDARY_ZOOM,
    paint: {
      'line-color': PLACE_COLORS[currentTheme()],
      'line-width': PARK_BOUNDARY_LINE_WIDTH.gold,
      'line-opacity': PARK_BOUNDARY_LINE_OPACITY.gold,
    },
  }, 'board-fill');
  // park-boundaries-fill (and, at the exact edge, park-boundaries-line
  // -- see their shared click handler below) IS the click target for a
  // park's whole box now -- see the click-precedence comment above
  // setupCellClickPopup. An earlier version added a separate invisible
  // line layer as a wide hit ring around just the edge, so a click deep
  // inside a large park (Craters of the Moon, e.g.) would fall through
  // to the board untouched; that traded "can't click the name" for
  // "can't click most of the park", which the user then asked to fix
  // directly -- the board only actually has a square where someone
  // painted one, so the fill can safely answer for every pixel of the
  // park EXCEPT a painted square, which the board-fill handler below
  // still claims. No ring layer needed once the fill itself carries
  // the click.

  // One symbol layer per tier, not one shared layer, so each tier's
  // icon-image can point at its own glyph (PLACE_GLYPHS[type]) while
  // still filtering the same shared `places` source -- a single
  // "Places" toggle still controls all three together (see
  // LAYER_TOGGLES). No minzoom set (defaults to the map's own floor,
  // see main()'s `minZoom: 4`) -- there is no hard cutoff any more,
  // regional decluttering is entirely icon-opacity's job now (see
  // PLACE_ICON_OPACITY_ZOOM), which fades smoothly rather than
  // switching anything on or off at a boundary.
  for (const type of PLACE_TYPES) {
    map.addLayer({
      id: `places-icons-${type}`,
      type: 'symbol',
      source: 'places',
      filter: ['==', ['get', 'type'], type],
      layout: {
        // Theme is filled in properly by applyBasemapTheme right after
        // this layer is added (see boot sequence); this initial value
        // is just a safe default before that first call.
        'icon-image': placeIconExpression(type, currentTheme()),
        'icon-allow-overlap': true,
        'icon-size': PLACE_ICON_SIZE_ZOOM,
      },
      paint: {
        'icon-opacity': PLACE_ICON_OPACITY_ZOOM,
      },
    });
  }

  map.addLayer({
    id: 'places-labels',
    type: 'symbol',
    source: 'places',
    minzoom: PLACE_LABEL_MIN_ZOOM,
    layout: {
      'text-field': ['get', 'name'],
      'text-font': ['Noto Sans Regular'],
      'text-size': 11,
      'text-anchor': 'top',
      'text-offset': [0, 0.9],
      'text-optional': true,
      'text-allow-overlap': false,
    },
  });
  // MapLibre paint properties do not read CSS custom properties, so the
  // theme-aware label colour is set directly from the same tokens
  // theme.css defines -- resolved once here rather than hardcoded,
  // matching applyBasemapTheme()'s own pattern of reading data-theme.
  const textColor = currentTheme() === 'neon' ? '#e8e8e8' : '#1a1a1a';
  map.setPaintProperty('places-labels', 'text-color', textColor);
  map.setPaintProperty('places-labels', 'text-halo-color', currentTheme() === 'neon' ? '#0C0B0A' : '#ffffff');
  map.setPaintProperty('places-labels', 'text-halo-width', 1.2);

  // A boundary-backed park's marker (and the label that hung off it) is
  // suppressed above -- see loadPlacesViewport's boundaryIds filter --
  // so without this the park has no name on the map at all once it's
  // drawn as an outline. This is the same label, text-only on purpose
  // (no icon-image) so an outlined park never regains the dot the user
  // explicitly said must never coexist with the outline; every other
  // layout/paint value below is copied from places-labels rather than
  // re-derived, so the two kinds of label are indistinguishable in
  // font, size, colour, and the zoom they appear at.
  //
  // Sourced from park-boundaries-label-points, NOT park-boundaries --
  // this used to point straight at the polygon source with
  // 'symbol-placement': 'point', which reads as "one label per
  // feature" but is not: MapLibre tiles a GeoJSON source client-side
  // (geojson-vt) purely for rendering performance, and a polygon
  // spanning several of those internal tiles gets its placement point
  // recomputed once per tile it crosses, not once per feature. A park
  // the size of Craters of the Moon crosses several, and printed its
  // own name 3-4 times in one view as a result. park-boundaries-label-
  // points carries exactly one Point feature per park (built in
  // loadPlacesViewport's buildParkLabelPoints, from the same lat/lon
  // app/places_api.py's `place` row already carries for this park,
  // i.e. the same point the seed generator anchored the park's ~6km
  // boundary clip to) -- a Point geometry cannot itself be split across
  // an internal tile boundary the way a large polygon can, so this
  // source can never reproduce the duplicate-label bug no matter how
  // many tiles the underlying boundary polygon spans. Trade-off: if
  // that anchor point ends up outside the current viewport while the
  // park's edge is still on screen, no label draws there any more --
  // see loadPlacesViewport's comment on buildParkLabelPoints for why
  // that was accepted rather than worked around.
  map.addLayer({
    id: 'park-boundaries-labels',
    type: 'symbol',
    source: 'park-boundaries-label-points',
    minzoom: PLACE_LABEL_MIN_ZOOM,
    layout: {
      'text-field': ['get', 'name'],
      'text-font': ['Noto Sans Regular'],
      'text-size': 11,
      'text-anchor': 'top',
      'text-offset': [0, 0.9],
      'text-optional': true,
      'text-allow-overlap': false,
    },
  });
  map.setPaintProperty('park-boundaries-labels', 'text-color', textColor);
  map.setPaintProperty('park-boundaries-labels', 'text-halo-color', currentTheme() === 'neon' ? '#0C0B0A' : '#ffffff');
  map.setPaintProperty('park-boundaries-labels', 'text-halo-width', 1.2);

  const popup = new maplibregl.Popup({ closeButton: true, closeOnClick: true, offset: 12 });
  for (const type of PLACE_TYPES) {
    const layerId = `places-icons-${type}`;
    map.on('click', layerId, (e) => {
      const f = e.features[0];
      if (!f) return;
      showPlacePopup(map, popup, f.properties, f.geometry.coordinates);
    });
    map.on('mouseenter', layerId, () => { map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', layerId, () => { map.getCanvas().style.cursor = ''; });
  }

  // A park's NAME answering its own click -- the most obvious thing to
  // aim at on an outlined park, and previously the one thing that did
  // nothing (the edge ring answered, the label sitting right next to it
  // did not). Calls the exact same showPlacePopup() the marker click
  // above and the fill click below do, on purpose: a boundary is the
  // same place as its marker, at a different zoom, and none of the
  // three must be able to drift into showing different content for it.
  // Anchors on the label's own point (f.geometry.coordinates, same as
  // a marker click) rather than e.lngLat, since -- unlike the fill --
  // this feature IS a single point.
  //
  // Ranked immediately after a place marker, ahead of everything else:
  // bails only if a marker was also hit at this exact point (same
  // PLACE_LAYER_IDS check setupCellClickPopup's board-fill handler
  // below uses) since a marker is a more specific, independently-
  // placed reference than a label that just names the polygon under
  // it. A label and a board/park-fill click essentially never coincide
  // in practice (a label only draws where there is text to read, a
  // few screen pixels), but the board-fill and park-boundaries-fill
  // handlers below both bail on THIS layer regardless, so a label
  // click can never be shadowed by either.
  map.on('click', 'park-boundaries-labels', (e) => {
    if (map.queryRenderedFeatures(e.point, { layers: PLACE_LAYER_IDS }).length > 0) return;
    const f = e.features[0];
    if (!f) return;
    showPlacePopup(map, popup, f.properties, f.geometry.coordinates);
  });
  map.on('mouseenter', 'park-boundaries-labels', () => { map.getCanvas().style.cursor = 'pointer'; });
  map.on('mouseleave', 'park-boundaries-labels', () => { map.getCanvas().style.cursor = ''; });

  // A park's whole box answering its own click, EXCEPT wherever a
  // painted square already sits -- see setupCellClickPopup's
  // precedence comment for the full order and why the board wins
  // there instead. Same showPlacePopup() as the marker/label clicks
  // above, anchored at the click point itself (e.lngLat) since a
  // polygon has no single point of its own to anchor to the way a
  // marker or label does.
  //
  // Bound to BOTH park-boundaries-fill and park-boundaries-line, not
  // fill alone: a line's stroke is centred on the boundary and extends
  // PARK_BOUNDARY_LINE_WIDTH/2 outward past the fill's own rasterized
  // edge, so a click landing exactly on the drawn line can render a
  // park-boundaries-line feature at that pixel with no
  // park-boundaries-fill feature there at all (observed directly
  // during deploy verification -- a click on the exact boundary vertex
  // hit only the line). One shared handler, bound twice, so a click
  // answers identically whichever of the two the pixel happens to
  // rasterize; the two layers share one source and geometry, so
  // `f.properties` is identical either way.
  const parkAreaClick = (e) => {
    if (map.queryRenderedFeatures(e.point, { layers: PLACE_LAYER_IDS }).length > 0) return;
    if (map.queryRenderedFeatures(e.point, { layers: ['park-boundaries-labels'] }).length > 0) return;
    if (map.queryRenderedFeatures(e.point, { layers: ['board-fill'] }).length > 0) return;
    const f = e.features[0];
    if (!f) return;
    showPlacePopup(map, popup, f.properties, e.lngLat);
  };
  for (const layerId of ['park-boundaries-fill', 'park-boundaries-line']) {
    map.on('click', layerId, parkAreaClick);
    map.on('mouseenter', layerId, () => { map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', layerId, () => { map.getCanvas().style.cursor = ''; });
  }
}

// Shared by setupPlacesLayer's three click handlers -- a place
// marker's own click, a park's label click, and a park's boundary-fill
// click -- so a park never has two (or three) independent
// implementations of "what its popup says" that could quietly drift
// apart. `lngLat` is where the popup anchors: a marker or label click
// passes that feature's own point (f.geometry.coordinates), a fill
// click passes the click location itself (e.lngLat) since a polygon
// has no single point of its own to anchor to.
function showPlacePopup(map, popup, properties, lngLat) {
  const { name, type: t, points } = properties;
  popup.setLngLat(lngLat).setHTML(
    `<div class="mw-place-popup">`
    + `<div class="mw-place-popup-name">${escapeHtml(name)}</div>`
    + `<div class="mw-place-popup-meta">${escapeHtml(t)} &middot; ${points} pts</div>`
    + `</div>`
  ).addTo(map);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// ===== Cell click popup (ported from frontend/mc.js's bindCellPopup /
// buildCellPopupHtml / buildRepeaterSectionHtml) =====
//
// Content and markup are copied verbatim from mc.js -- same
// .mc-popup/.mc-popup-* classes (styled by mc.css, already loaded by
// map2.html for the territory panel), same fields, same "no section at
// all, not an empty-state message" rule for a cell with no recorded
// repeater/feeder observations. Only the delivery differs: mc.js binds
// a Leaflet popup per-rectangle at draw time and lazy-loads on
// popupopen; this page has one board-fill fill layer for the whole
// board (see main()'s map.addSource('board', ...)), so this fetches
// per click instead, against whichever board is active at the moment
// of the click (cfg() read fresh in the click handler, not captured at
// draw time -- there is nothing analogous to draw time here).

// See buildRepeaterSectionHtml's identical comment in mc.js: a cell
// with no recorded observations gets no section at all, never an
// invented or distance-guessed one.
function buildRepeaterSectionHtml(detail, c) {
  const repeaters = Array.isArray(detail.repeaters) ? detail.repeaters : [];
  if (repeaters.length === 0) return '';

  const rows = repeaters.map((r) => {
    const counts = [];
    if (r.direct_count) counts.push(`${r.direct_count} direct`);
    if (r.heard_count) counts.push(`${r.heard_count} heard`);
    const countText = counts.length ? counts.join(', ') : 'no hits';
    return `<div class="mc-popup-capture-row">
        ${escapeHtml(r.repeater_id)} &mdash; ${escapeHtml(countText)}, last heard ${escapeHtml(formatTs(r.last_seen))}
      </div>`;
  }).join('');

  return `
    <div class="mc-popup-section-title">${escapeHtml(c.repeaterLabel)}</div>
    ${rows}
  `;
}

function buildCellPopupHtml(cellId, detail, c) {
  const scoreRows = TEAM_ORDER.map((team) => {
    const score = detail.scores && detail.scores[team] !== undefined ? detail.scores[team] : 0;
    return `<div class="mc-popup-score-row">
        <span class="mc-dot" style="background:${TEAM_COLORS[team]}"></span>${escapeHtml(team)}: ${escapeHtml(score)}
      </div>`;
  }).join('');

  const captures = Array.isArray(detail.recent_captures) ? detail.recent_captures : [];
  const captureRows = captures.length
    ? captures.map((cap) => {
      // Logged out, the capturing player's name is withheld (see
      // app/mc_api.py's cell redaction) -- so show the team alone rather
      // than a placeholder standing in for a person. "ORANGE" is the whole
      // truth we have; "unknown player for ORANGE" implies we are hiding a
      // specific someone and reads as a defect rather than a boundary.
      const attribution = cap.by_display_name
        ? `${escapeHtml(cap.by_display_name)} for ${escapeHtml(cap.by_team)}`
        : escapeHtml(cap.by_team);
      const fromNote = cap.from_team ? ` (from ${escapeHtml(cap.from_team)})` : '';
      return `<div class="mc-popup-capture-row">
          ${escapeHtml(formatTs(cap.ts))} &mdash; ${attribution}${fromNote}
        </div>`;
    }).join('')
    : '<div class="mc-popup-capture-row mc-popup-empty">No capture history.</div>';

  // Additive -- see app/mc_api.py's _containing_park(): the cell popup
  // is now the only place a square's park membership shows once the
  // park's own fill click yields to a painted square sitting on top of
  // it (see setupCellClickPopup's precedence comment), so this is here
  // to make sure that information is never simply lost. `detail.park`
  // is the SAME >50%-of-cell relationship place_cell already encodes
  // for scoring, not a client-side re-guess -- see that function's
  // docstring for why (and for how it breaks a tie between two
  // designations that both cover this cell, e.g. a state park and a
  // coincident historic site). None, and no row, when this cell isn't
  // majority-inside any boundary-backed park.
  const parkRow = detail.park
    ? `<div class="mc-popup-row">Inside: ${escapeHtml(detail.park.name)} (${escapeHtml(detail.park.points)} pts)</div>`
    : '';

  return `
    <div class="mc-popup">
      <div class="mc-popup-header">
        <span class="mc-dot" style="background:${TEAM_COLORS[detail.owner_team] || '#888'}"></span>
        ${escapeHtml(cellId)}
      </div>
      <div class="mc-popup-row">Owner: ${escapeHtml(detail.owner_team || 'none')}</div>
      <div class="mc-popup-row">Captured: ${escapeHtml(formatTs(detail.captured_at))}</div>
      ${parkRow}
      <div class="mc-popup-section-title">Scores</div>
      ${scoreRows}
      <div class="mc-popup-section-title">Recent captures</div>
      ${captureRows}
      ${buildRepeaterSectionHtml(detail, c)}
    </div>
  `;
}

// Wired to the 'board-fill' layer, the same source both boards' squares
// draw from (see main()'s map.addSource('board', ...)) -- no second
// click handler needed when the board switches, cfg() is read fresh
// per click.
//
// Four things can now answer the same click, in this precedence:
//   1. a place marker (places-icons-*)            -- most specific
//   2. a park's label (park-boundaries-labels)     -- see setupPlacesLayer
//   3. a painted board square (this handler)        -- see below
//   4. a park's fill (park-boundaries-fill)          -- least specific
// MapLibre's per-layer click delegation queries each bound layer
// independently by pixel, not by on-screen stacking order, so a click
// landing on a marker, a park label, OR a park's fill sitting under a
// board square would otherwise fire more than one of these handlers at
// once. This bails out (no cell popup) whenever the same point also
// hits a places-icons-* or park-boundaries-labels feature, letting
// that handler's own popup win uncontested.
//
// Deliberately NOT bailing on park-boundaries-fill, and this is the
// crux of the whole precedence order: the board only ever has a
// feature where a square has actually been painted (mc_tile rows with
// owner_team set -- see app/mc_api.py's board_for) -- most of the
// ground inside a large boundary-backed park (Craters of the Moon,
// e.g.) is unpainted, so a click there never reaches this handler at
// all regardless of what it bails on. Letting the park's fill take
// precedence over a PAINTED square would make every owned cell inside
// a national park permanently unclickable, which is the regression
// the fill click in setupPlacesLayer explicitly bails on (it checks
// board-fill and yields to it) rather than risking here. The park a
// painted square sits inside is never lost, just relocated: see
// buildCellPopupHtml's `parkRow`, sourced from this fetch's own
// `detail.park` (app/mc_api.py's _containing_park). Verified by
// clicking well inside a large boundary-backed park, on both a painted
// square (cell popup, park named) and unpainted ground (park popup) --
// see the deploy verification notes for this change.
function setupCellClickPopup(map) {
  const cellPopup = new maplibregl.Popup({
    closeButton: true,
    closeOnClick: true,
    offset: 12,
    maxWidth: '320px',
    className: 'mc-tile-popup',
  });

  map.on('click', 'board-fill', (e) => {
    if (map.queryRenderedFeatures(e.point, { layers: PLACE_LAYER_IDS }).length > 0) return;
    if (map.queryRenderedFeatures(e.point, { layers: ['park-boundaries-labels'] }).length > 0) return;

    const f = e.features[0];
    if (!f) return;
    const cellId = f.properties.cell_id;
    const c = cfg();

    cellPopup.setLngLat(e.lngLat).setHTML('<div class="mc-popup-loading">Loading…</div>').addTo(map);

    fetch(c.cellEndpoint(cellId))
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((detail) => {
        cellPopup.setHTML(buildCellPopupHtml(cellId, detail, c));
      })
      .catch((err) => {
        console.warn('cell detail load failed:', err);
        cellPopup.setHTML('<div class="mc-popup-loading">Failed to load cell detail.</div>');
      });
  });
  map.on('mouseenter', 'board-fill', () => { map.getCanvas().style.cursor = 'pointer'; });
  map.on('mouseleave', 'board-fill', () => { map.getCanvas().style.cursor = ''; });
}

// One Point feature per boundary-backed park in `boundaryFeatures`, for
// the park-boundaries-label-points source (see setupPlacesLayer's
// park-boundaries-labels comment for why this exists at all -- in
// short, symbol-placement:'point' straight on the tiled polygon source
// prints a park's name once per internal geojson-vt tile its boundary
// crosses, not once per feature).
//
// The point used for each park is its OWN lat/lon from `places` --
// the same row app/places_api.py's `place` table already carries for
// this park's marker (suppressed on the map once its outline draws,
// see loadPlacesViewport's boundaryIds filter, but still present in
// the places array this function reads), which is also the exact
// point app/places_seed.py anchored that park's ~6km boundary clip to.
// Not a client-computed polygon centroid: that would need real
// polygon math (area-weighted centroid or a pole-of-inaccessibility
// pass, correctly handling multi-part geometry and holes) for a value
// this seed point already gives for free, at the cost of one lookup.
//
// Trade-off this makes explicit: the seed point sits within a few km
// of the park's own boundary by construction, but a huge park's edge
// can still be on screen while that point is not (the same
// off-screen-centroid case the old per-tile placement happened to
// paper over by accident). When that happens here, the park simply
// gets no label in that view -- accepted deliberately (see this
// change's commit message) rather than reintroducing a second
// placement path just to avoid it: one correct label beats a wrong
// count, and the park is still fully clickable (fill and, once close
// enough, its outline) with no label at all.
function buildParkLabelPoints(boundaryFeatures, places) {
  const byId = new Map(places.map((p) => [p.id, p]));
  const features = [];
  for (const f of boundaryFeatures) {
    const place = byId.get(f.properties.id);
    // This IS the off-screen-anchor case, not just a defensive
    // fallback: app/places_api.py fetches park_boundaries against the
    // viewport plus BOUNDARY_VIEWPORT_MARGIN_DEG of padding (a big
    // polygon can poke into view from just outside it) but fetches
    // `places` (markers) against the bare, unpadded viewport -- so a
    // huge park's boundary can come back here while its own point sits
    // just outside `places` and never arrives in this map at all. No
    // label point is created for it in that view; see this function's
    // header comment for why that is accepted rather than worked
    // around.
    if (!place) continue;
    features.push({
      type: 'Feature',
      properties: f.properties,
      geometry: { type: 'Point', coordinates: [place.lon, place.lat] },
    });
  }
  return { type: 'FeatureCollection', features };
}

async function loadPlacesViewport(map) {
  try {
    const bounds = map.getBounds();
    const data = await fetchPlacesInViewport(bounds, map.getZoom());
    // One park, one mark: a park's centroid marker is suppressed
    // exactly where its own outline is also coming back, so the two
    // can never both draw at once. Driven by the actual ids present in
    // data.park_boundaries -- not a re-derived zoom check -- so this
    // cannot drift out of sync with MIN_BOUNDARY_ZOOM (it IS whatever
    // that gate produced this response) and it degrades correctly when
    // app/places_api.py's MAX_BOUNDARY_RESULTS cap silently drops a
    // park's boundary from the response: that id is then simply absent
    // from boundaryIds, so its marker is not filtered and the park
    // stays visible as a dot rather than disappearing from the map.
    const boundaryIds = new Set(data.park_boundaries.features.map((f) => f.properties.id));
    map.getSource('places').setData({
      type: 'FeatureCollection',
      features: data.places.filter((p) => !boundaryIds.has(p.id)).map(placeToFeature),
    });
    // park_boundaries is always present (an empty FeatureCollection
    // below MIN_BOUNDARY_ZOOM, or with no boundary-backed park in
    // view) -- see app/places_api.py's places_in_viewport docstring --
    // so this can set it unconditionally rather than checking first.
    map.getSource('park-boundaries').setData(data.park_boundaries);
    // See buildParkLabelPoints -- one label point per park, derived
    // from data.places rather than left to the polygon source above,
    // so park-boundaries-labels can never print the same name twice.
    map.getSource('park-boundaries-label-points').setData(
      buildParkLabelPoints(data.park_boundaries.features, data.places)
    );
  } catch (err) {
    console.error('MeshWars map2: failed to load places', err);
  }
}

function renderPlacesPanel(data) {
  const list = document.getElementById('mw-places-list');
  if (!list) return;
  list.innerHTML = '';
  if (!data.places.length) {
    const li = document.createElement('li');
    li.className = 'mw-places-empty';
    li.textContent = 'No live places within range of this view.';
    list.appendChild(li);
    return;
  }
  for (const p of data.places) {
    const li = document.createElement('li');
    const miles = (p.distance_m / 1609.344).toFixed(1);
    li.innerHTML =
      `<span class="mw-place-icon" style="background:${PLACE_COLORS[currentTheme()]}"></span>`
      + `<span class="mw-place-name">${escapeHtml(p.name)}</span>`
      + `<span class="mw-place-meta">${p.points}pt &middot; ${miles}mi</span>`;
    li.addEventListener('click', () => {
      window.__mwMap && window.__mwMap.flyTo({ center: [p.lon, p.lat], zoom: Math.max(window.__mwMap.getZoom(), 14) });
    });
    list.appendChild(li);
  }
}

async function loadPlacesPanel(map) {
  try {
    const center = map.getCenter();
    const data = await fetchPlacesNear(center.lat, center.lng);
    renderPlacesPanel(data);
  } catch (err) {
    console.error('MeshWars map2: failed to load places panel', err);
  }
}

// ---- award highlight ---------------------------------------------------
//
// /results links an honor that happened somewhere ("Longest Road") to
// this page, carrying board+month+award. We fetch that award's geometry
// and draw it on top of the board, then frame it.
//
// Deliberately additive and failure-tolerant: no params, a bad award, a
// 404, or a fetch error all leave the map exactly as it would have been.
// A visitor arriving with a junk query string gets a normal map, not a
// broken one.

const AWARD_GEO_ENDPOINT = {
  meshcore: (month, award) => `/api/mc/results/${encodeURIComponent(month)}/${encodeURIComponent(award)}/geo`,
  meshtastic: (month, award) => `/api/results/${encodeURIComponent(month)}/${encodeURIComponent(award)}/geo`,
};

function featureCollectionBounds(fc) {
  let west = Infinity, south = Infinity, east = -Infinity, north = -Infinity;
  for (const f of (fc.features || [])) {
    const geom = f.geometry;
    if (!geom || !geom.coordinates) continue;
    // A Point is one coordinate pair; a Polygon is rings of them.
    const rings = geom.type === 'Point' ? [[geom.coordinates]] : geom.coordinates;
    for (const ring of rings) {
      for (const [lon, lat] of ring) {
        if (lon < west) west = lon;
        if (lon > east) east = lon;
        if (lat < south) south = lat;
        if (lat > north) north = lat;
      }
    }
  }
  return Number.isFinite(west) ? [[west, south], [east, north]] : null;
}

function showAwardBanner(map, geo) {
  const el = document.createElement('div');
  el.className = 'award-banner';
  el.innerHTML = `
    <span class="award-banner-label"></span>
    <span class="award-banner-who"></span>
    <span class="award-banner-detail"></span>
    <button type="button" class="award-banner-close" aria-label="Clear highlight">&times;</button>`;
  // The PLAYER is the subject of a player award -- naming the team
  // instead dropped the person who actually earned it. The team is
  // carried by the COLOUR of that name rather than spelled out beside
  // it, which said the same thing twice. A team award (Longest Road,
  // Largest Territory) has no player, so its team name is the subject
  // and is coloured the same way.
  // textContent, not innerHTML: a display name is player-supplied.
  el.querySelector('.award-banner-label').textContent = `${geo.label} —`;
  const who = el.querySelector('.award-banner-who');
  who.textContent = geo.player || geo.team || '';
  if (geo.team) {
    who.style.color = TEAM_COLORS[geo.team] || 'inherit';
    // Colour alone carries the team, so name it on hover for anyone who
    // cannot tell the seven apart.
    who.title = geo.team;
  }
  el.querySelector('.award-banner-detail').textContent =
    `${geo.value} ${geo.detail || ''}`.trim();
  el.querySelector('.award-banner-close').addEventListener('click', () => {
    const src = map.getSource('award-highlight');
    if (src) src.setData({ type: 'FeatureCollection', features: [] });
    el.remove();
  });
  document.body.appendChild(el);
}

function awardParams() {
  let params;
  try {
    params = new URLSearchParams(window.location.search);
  } catch {
    return null;
  }
  const award = params.get('award');
  const month = params.get('month');
  if (!award || !month) return null;
  return { award, month, board: boardParam() || 'meshcore' };
}

// ?lat=&lon=&zoom= -- go straight to a place on the map. There was no
// way to link someone to a spot at all before this; the only way to show
// somebody a square was to tell them to pan and hunt for it.
function viewParams() {
  let params;
  try {
    params = new URLSearchParams(window.location.search);
  } catch {
    return null;
  }
  const lat = parseFloat(params.get('lat'));
  const lon = parseFloat(params.get('lon'));
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return null;
  const zoom = parseFloat(params.get('zoom'));
  return { lat, lon, zoom: Number.isFinite(zoom) ? Math.min(Math.max(zoom, 1), 18) : 14 };
}

// ?board=meshcore|meshtastic, shared by the award links and any linked
// view. Anything else is ignored rather than guessed at.
function boardParam() {
  let params;
  try {
    params = new URLSearchParams(window.location.search);
  } catch {
    return null;
  }
  const b = params.get('board');
  return b === 'meshcore' || b === 'meshtastic' ? b : null;
}

function goToViewFromUrl(map) {
  const v = viewParams();
  if (!v) return;
  map.jumpTo({ center: [v.lon, v.lat], zoom: v.zoom });
}

// Whether this page load is someone arriving to look at one specific
// thing -- an award, or a place they were linked to. Read by
// loadNotice() too, so the first-visit modal does not land on top of
// whatever they came to see.
function hasAwardParams() {
  return awardParams() !== null || viewParams() !== null;
}

async function showAwardFromUrl(map) {
  const p = awardParams();
  if (!p) return;
  const { award, month, board } = p;
  const endpoint = AWARD_GEO_ENDPOINT[board];
  if (!endpoint) return;

  try {
    const res = await fetch(endpoint(month, award));
    if (!res.ok) return;   // 404 = that award has no geometry; leave the map alone
    const geo = await res.json();
    const fc = geo && geo.geojson;
    if (!fc || !(fc.features || []).length) return;

    const src = map.getSource('award-highlight');
    if (!src) return;
    src.setData(fc);

    const bounds = featureCollectionBounds(fc);
    if (bounds) {
      map.fitBounds(bounds, { padding: 60, maxZoom: 12, duration: 800 });
    }
    showAwardBanner(map, geo);
  } catch (err) {
    // Never let a highlight failure take the map down with it.
    console.warn('award highlight failed', err);
  }
}


function teamMatchExpression() {
  // ['match', ['get', 'team'], 'RED', '#ff4136', 'GREEN', '#2ecc40', ..., fallback]
  const expr = ['match', ['get', 'team']];
  for (const team of TEAM_ORDER) {
    expr.push(team, TEAM_COLORS[team]);
  }
  expr.push('#888888');
  return expr;
}

// Mirrors theme-toggle.js's currentTheme(): gold is the default and the
// only other value the toggle ever writes is 'neon'. (map2.html's own
// boot snippet defaults the FRONT PAGE to neon specifically when no
// choice has been stored yet -- see that file -- but once data-theme is
// set, reading it back here works exactly the same either way.)
function currentTheme() {
  return document.documentElement.getAttribute('data-theme') === 'neon' ? 'neon' : 'gold';
}

// No basemap layer left to flip visibility on -- both themes share the
// one dark BASEMAP_ID layer now (see its comment above). Never touches
// the board's team-colour expression -- that stays constant across
// themes on purpose (gameplay, not branding). The hillshade layer used
// to get a per-theme exaggeration re-tune here too; that was a
// raster-dem paint property computed in the browser, and the
// pre-rendered hillshade imagery has its exaggeration baked in at
// build time with no such property left to set. raster-opacity is a
// different paint property that survives the switch to baked imagery
// (see HILLSHADE_OPACITY), so it's still tuned here per theme.
function applyBasemapTheme(map) {
  const theme = currentTheme();
  map.setPaintProperty(HILLSHADE_ID, 'raster-opacity', HILLSHADE_OPACITY[theme]);
  map.setPaintProperty('board-fill', 'fill-opacity', BOARD_FILL_OPACITY[theme]);
  map.setPaintProperty('board-line', 'line-width', BOARD_LINE_WIDTH[theme]);
  map.setPaintProperty('park-boundaries-fill', 'fill-opacity', PARK_BOUNDARY_FILL_OPACITY[theme]);
  map.setPaintProperty('park-boundaries-fill', 'fill-color', PLACE_COLORS[theme]);
  map.setPaintProperty('park-boundaries-line', 'line-width', PARK_BOUNDARY_LINE_WIDTH[theme]);
  map.setPaintProperty('park-boundaries-line', 'line-opacity', PARK_BOUNDARY_LINE_OPACITY[theme]);
  map.setPaintProperty('park-boundaries-line', 'line-color', PLACE_COLORS[theme]);
  for (const type of PLACE_TYPES) {
    map.setLayoutProperty(`places-icons-${type}`, 'icon-image', placeIconExpression(type, theme));
  }
}

// theme-toggle.js sets data-theme on <html> directly; observing the
// attribute is more reliable than hooking the button itself, since it
// also catches the boot-time value and any future writer.
function watchTheme(map) {
  const observer = new MutationObserver(() => applyBasemapTheme(map));
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['data-theme'],
  });
}

// The self-hosted overlay sources + layers: public lands and USFS
// roads/trails. All defined up front (called once, at map load) so
// every checkbox toggle below only ever flips a layout property -- no
// addSource/addLayer after this point. Inserted beforeId 'board-fill'
// so they always sit under the team squares, no matter the draw order
// MapLibre would otherwise pick.
// Measured data ranges, from the tile headers (not a style choice):
//   usfs roads     z6  - z14
//   usfs trails    z6  - z14
//   public-lands   z4  - z12
// These floors are real limits of navi's archives (tippecanoe flags
// baked in at build time), not a style choice, so each layer below
// declares an explicit `minzoom` matching them, and LAYER_TOGGLES
// carries the same numbers so the switcher greys out an entry (with a
// "from zoom N" label) below its floor rather than leaving a ticked box
// that silently draws nothing; see setupLayerSwitcher.
// Neither gets a `maxzoom` -- MapLibre overzooms a vector layer fine,
// reusing the highest tile it has, so each should keep drawing all the
// way to the map's own maxZoom (17) rather than stopping early. The
// hillshade above now overzooms the same uncapped way: it used to be a
// raster-dem source that tore along tile seams past its data's z12
// ceiling (deliberately cut off at 13 to make that predictable), but
// plain raster imagery just reuses and stretches its last real tile
// like these vector layers do, so nothing tears and there is nothing
// to cap.
//
// Both route layers share this width ramp: the old flat 0.7-0.8px was
// measured to disappear under the public-lands wash even where the
// data was dense. Never below ~1px so a route has real ink at the low
// zooms this map opens at, growing toward 3px by street zoom.
const ROUTE_LINE_WIDTH = [
  'interpolate', ['linear'], ['zoom'],
  4, 1,
  9, 1.6,
  17, 3,
];

function setupOverlayLayers(map) {
  map.addSource('public-lands', {
    type: 'vector',
    url: `pmtiles://${PUBLIC_LANDS_URL}`,
  });
  map.addSource('usfs-trails-roads', {
    type: 'vector',
    url: `pmtiles://${USFS_TRAILS_ROADS_URL}`,
  });

  // PAD-US derived tiles carry no literal `Marine`/offshore flag -- the
  // properties actually present are access, acres, agency, designation,
  // gap_status, id, manager_type, name, owner_type (checked by decoding
  // a tile with python3 + the pmtiles module rather than assumed). One
  // pair stood out: agency 'BOEM' (Bureau of Ocean Energy Management)
  // paired 1:1 with designation 'OCS' (Outer Continental Shelf) at every
  // feature sampled world-wide at z4 -- these are offshore energy-lease
  // planning areas, and their footprint (Gulf of Mexico to Arctic Alaska
  // to American Samoa) is exactly what pushed this archive's bounds out
  // to lat -15..75 and put stripes over the Pacific and Canada once the
  // rebuild let the archive draw all the way down to z0. Filtering out
  // designation 'OCS' drops that regression; this is a land navigation
  // map and BOEM/OCS is the only agency+designation pair that is marine.
  const NOT_MARINE_FILTER = ['!=', ['get', 'designation'], 'OCS'];

  map.addLayer({
    id: 'public-lands-fill',
    type: 'fill',
    source: 'public-lands',
    'source-layer': 'public_lands',
    minzoom: 4,
    layout: { visibility: 'none' },
    filter: NOT_MARINE_FILTER,
    paint: {
      'fill-color': '#4f7a4a',
      // Public lands is context for the routes, not the subject -- at
      // low zoom the old flat 0.18 turned into 16,000+ polygon outlines
      // of green speckle (measured: 16,398 features in view at z4).
      // Fading toward transparent as the camera pulls out keeps the
      // boundary readable close in without it taking over the screen
      // zoomed out. No minzoom/maxzoom cutoff -- a gradual fade, per
      // spec.
      'fill-opacity': [
        'interpolate', ['linear'], ['zoom'],
        4, 0.02,
        6, 0.04,
        9, 0.08,
        13, 0.12,
      ],
    },
  }, 'board-fill');
  map.addLayer({
    id: 'public-lands-line',
    type: 'line',
    source: 'public-lands',
    'source-layer': 'public_lands',
    minzoom: 4,
    layout: { visibility: 'none' },
    filter: NOT_MARINE_FILTER,
    paint: {
      'line-color': '#4f7a4a',
      // Thinned from a flat 0.8 -- this is a boundary hint, not a route.
      'line-width': [
        'interpolate', ['linear'], ['zoom'],
        4, 0.3,
        9, 0.5,
        14, 0.6,
      ],
      // Same zoom fade as the fill above, and still capped below the
      // old flat 0.5 even fully zoomed in.
      'line-opacity': [
        'interpolate', ['linear'], ['zoom'],
        4, 0.05,
        6, 0.15,
        9, 0.3,
        13, 0.45,
      ],
    },
  }, 'board-fill');
  map.addLayer({
    id: 'usfs-roads-line',
    type: 'line',
    source: 'usfs-trails-roads',
    'source-layer': 'roads',
    minzoom: 6,
    layout: { visibility: 'none' },
    paint: {
      'line-color': '#b06a3a',
      'line-width': ROUTE_LINE_WIDTH,
    },
  }, 'board-fill');
  map.addLayer({
    id: 'usfs-trails-line',
    type: 'line',
    source: 'usfs-trails-roads',
    'source-layer': 'trails',
    minzoom: 6,
    layout: { visibility: 'none' },
    paint: {
      'line-color': '#7a5a2a',
      'line-width': ROUTE_LINE_WIDTH,
      'line-dasharray': [2, 2],
    },
  }, 'board-fill');
}

// A contour layer used to be set up here, generated client-side from
// the same DEM archive as the hillshade above via mlcontour. Dropped
// 2026-08-24: it cost 2.9 seconds of main-thread blocking in a single
// 5-second pan (33fps -> 9fps) and re-downloaded the DEM a second time
// through its own dem:// protocol even though the hillshade had already
// fetched it. A pre-rendered-tile alternative was also tried and
// abandoned -- baking contours at every interval across one state
// produced 12GB of intermediates with the finest pass still unfinished.
// Hillshade alone carries the terrain now. Do not re-add this without
// solving the cost, not just the symptom.

// A checked box over a layer with no data at the current zoom reads as
// broken -- there is nothing wrong, the tiles just do not exist below
// their minzoom (see LAYER_TOGGLES / setupOverlayLayers). So each
// switcher entry tracks the zoom it becomes available at, and this
// applies that on every 'zoom' event (and once at setup, for whatever
// zoom the map happened to load at):
//   - below minzoom: disable the checkbox, mark its row .unavailable,
//     and swap the label for one that names the reason.
//   - at/above minzoom: re-enable it and restore the plain label.
// What the user actually wants ticked is tracked separately (`wanted`)
// from the checkbox's own `checked` state, which this function may
// force off while unavailable -- so zooming back in restores the tick
// on its own rather than the user having to redo it.
function setupLayerSwitcher(map) {
  const entries = [];

  for (const [checkboxId, layerIds, minZoom] of LAYER_TOGGLES) {
    const checkbox = document.getElementById(checkboxId);
    if (!checkbox) continue;
    const row = checkbox.closest('li');
    const label = checkbox.closest('label');
    // The label markup is `<input> Some Text` -- pull out that trailing
    // text node once so it can be swapped for an availability note
    // later without touching the checkbox or rebuilding the label.
    let textNode = null;
    if (label) {
      for (const node of label.childNodes) {
        if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
          textNode = node;
          break;
        }
      }
    }
    const baseText = textNode ? textNode.textContent.trim() : '';

    const entry = { checkbox, layerIds, minZoom, row, textNode, baseText, wanted: checkbox.checked };
    entries.push(entry);

    checkbox.addEventListener('change', () => {
      entry.wanted = checkbox.checked;
      const visibility = checkbox.checked ? 'visible' : 'none';
      for (const layerId of layerIds) {
        map.setLayoutProperty(layerId, 'visibility', visibility);
      }
    });
  }

  function applyAvailability() {
    const zoom = map.getZoom();
    for (const entry of entries) {
      const available = zoom >= entry.minZoom;

      entry.checkbox.disabled = !available;
      if (entry.row) entry.row.classList.toggle('unavailable', !available);
      if (entry.textNode) {
        entry.textNode.textContent = available
          ? ` ${entry.baseText}`
          : ` ${entry.baseText} (from zoom ${entry.minZoom})`;
      }

      const shouldBeChecked = available && entry.wanted;
      if (entry.checkbox.checked !== shouldBeChecked) {
        entry.checkbox.checked = shouldBeChecked;
        const visibility = shouldBeChecked ? 'visible' : 'none';
        for (const layerId of entry.layerIds) {
          map.setLayoutProperty(layerId, 'visibility', visibility);
        }
      }
    }
  }

  map.on('zoom', applyAvailability);
  applyAvailability();
}

async function loadBoardData(map) {
  try {
    const board = await fetchBoard(cfg());
    map.getSource('board').setData(board);
  } catch (err) {
    console.error('MeshWars map2: failed to load board', err);
  }
}

// ===== Update notice (operator-authored, admin panel) =====
//
// One-time release notice, authored and toggled from the admin panel's
// Notice section (app/admin_ops.py's admin_notice_save) and read here
// from the tiny, unauthenticated GET /api/notice (app/notice_api.py).
// A player sees it once per version_key: dismissal is remembered in
// this browser's localStorage, never on the server, so bumping the
// version key server-side is what re-shows it to everyone, including
// anyone who already dismissed the previous one.
//
// SECURITY: title/body are operator-authored, not player-authored, but
// still written to the DOM with .textContent only, never innerHTML --
// see .mw-notice-modal-body's white-space: pre-wrap in map2.css, which
// is what turns the operator's plain line breaks back into visible
// ones without this ever parsing the body as markup.
const NOTICE_DISMISSED_KEY = 'mwNoticeDismissed';

function noticeAlreadyDismissed(versionKey) {
  try {
    return localStorage.getItem(NOTICE_DISMISSED_KEY) === versionKey;
  } catch {
    return false;
  }
}

function rememberNoticeDismissed(versionKey) {
  try {
    localStorage.setItem(NOTICE_DISMISSED_KEY, versionKey);
  } catch {
    // Storage unavailable (private browsing, quota) -- the notice just
    // shows again next load, which is the safe direction to fail in.
  }
}

let noticeModalEl = null;
let noticeReturnFocusEl = null;

function onNoticeKeydown(e) {
  if (e.key === 'Escape' && noticeModalEl) {
    closeNoticeModal(noticeModalEl.dataset.versionKey);
  }
}

// Always reachable: a close button, Escape, and a click on the dimmed
// backdrop all call this. Nothing about this modal can leave the map
// permanently covered -- there is no state where none of the three work.
function closeNoticeModal(versionKey) {
  if (versionKey) rememberNoticeDismissed(versionKey);
  if (noticeModalEl) {
    noticeModalEl.remove();
    noticeModalEl = null;
  }
  document.removeEventListener('keydown', onNoticeKeydown);
  // Returns focus to whatever had it before the modal opened (the body,
  // in practice, since this only ever opens once on boot) -- no focus
  // trap is installed in the first place, so Tab already never got
  // stuck; this just avoids dropping focus onto <body> instead of
  // wherever it reasonably belongs.
  if (noticeReturnFocusEl && typeof noticeReturnFocusEl.focus === 'function') {
    noticeReturnFocusEl.focus();
  }
  noticeReturnFocusEl = null;
}

function showNoticeModal(notice) {
  noticeReturnFocusEl = document.activeElement;

  const wrap = document.createElement('div');
  wrap.className = 'mw-notice-modal';
  wrap.dataset.versionKey = notice.version_key;

  const inner = document.createElement('div');
  inner.className = 'mw-notice-modal-inner';
  inner.setAttribute('role', 'dialog');
  inner.setAttribute('aria-modal', 'true');
  inner.setAttribute('aria-labelledby', 'mw-notice-title');

  const header = document.createElement('div');
  header.className = 'mw-notice-modal-header';
  const titleEl = document.createElement('span');
  titleEl.id = 'mw-notice-title';
  titleEl.textContent = notice.title;
  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'mw-notice-modal-close';
  closeBtn.setAttribute('aria-label', 'Close');
  closeBtn.textContent = '×';
  header.appendChild(titleEl);
  header.appendChild(closeBtn);

  const bodyEl = document.createElement('div');
  bodyEl.className = 'mw-notice-modal-body';
  bodyEl.textContent = notice.body;

  // The body stays plain text on purpose (see the block comment above),
  // so it can never itself carry a link -- this is the one fixed way
  // out to more detail, present on every notice regardless of what the
  // operator wrote. Kept in its own footer row, below the body and away
  // from .mw-notice-modal-close up in the header, so the two controls
  // read as distinct: one leaves you here to read on, the other leaves
  // the page.
  const footer = document.createElement('div');
  footer.className = 'mw-notice-modal-footer';
  const rulesLink = document.createElement('a');
  rulesLink.className = 'mw-notice-modal-rules-link';
  rulesLink.href = '/rules';
  rulesLink.textContent = 'Read the rules';
  footer.appendChild(rulesLink);

  inner.appendChild(header);
  inner.appendChild(bodyEl);
  inner.appendChild(footer);
  wrap.appendChild(inner);
  document.body.appendChild(wrap);
  noticeModalEl = wrap;

  closeBtn.addEventListener('click', () => closeNoticeModal(notice.version_key));
  wrap.addEventListener('click', (e) => {
    if (e.target === wrap) closeNoticeModal(notice.version_key);
  });
  // Clicking through to the rules is its own way of having seen the
  // notice -- remember it dismissed (same as the close button/Escape/
  // backdrop) so it does not pop again when the player comes back from
  // /rules, without pre-empting the real navigation the anchor already
  // performs.
  rulesLink.addEventListener('click', () => rememberNoticeDismissed(notice.version_key));
  document.addEventListener('keydown', onNoticeKeydown);

  closeBtn.focus();
}

// Fired off from main() without being awaited -- see that call site's
// own comment. Failure of any kind (network, bad JSON, no notice
// published) just means nothing renders; it never blocks or delays the
// map itself.
async function loadNotice() {
  try {
    const res = await fetch('/api/notice');
    if (!res.ok) return;
    const data = await res.json();
    const notice = data && data.notice;
    if (!notice || !notice.version_key || !notice.title || !notice.body) return;
    if (noticeAlreadyDismissed(notice.version_key)) return;
    // Someone who followed an award link from /results came here to
    // look at one specific thing on the map, and a modal over it is the
    // opposite of what they asked for. The notice is not dismissed --
    // just not shown this once, so it still greets them on a normal
    // visit.
    if (hasAwardParams()) return;
    showNoticeModal(notice);
  } catch (err) {
    console.error('MeshWars map2: failed to load notice', err);
  }
}

const finite = (v) => typeof v === 'number' && Number.isFinite(v);

// Opening view, in priority order: (1) the viewer's own location, if the
// browser will give it up quickly, (2) failing that -- denied, no
// permission prompt answered, no geolocation API at all, or just too
// slow -- this fixed box. It runs from Boise (Treasure Valley) at the
// northwest corner down to the southeast corner of Utah (Wasatch Front)
// at the southeast: [[west, south], [east, north]], same convention as
// playAreaBounds below. That span is where MeshWars players actually
// are today, unlike the empty mountains northeast of Boise the map used
// to open on by default. This box is what's on screen from first
// paint -- see the `bounds` passed to the constructor below -- and
// geolocation, when it resolves, is layered on top afterward rather
// than gating it.
const FALLBACK_VIEW_BOUNDS = [[-116.21, 37.00], [-109.05, 43.62]];
const GEOLOCATION_TIMEOUT_MS = 5000;
const GEOLOCATION_ZOOM = 11; // city-level -- close enough to orient, not so close it feels like a snap-to

// Read from /config rather than written down here, same reasoning as
// frontend/play-area-map.js: the play area is an operator setting that
// has moved before, and a hardcoded copy in this file would silently
// disagree with the server a month later. If /config is unreachable or
// the numbers are missing/non-finite, playAreaBounds comes back null so
// the map is built with no maxBounds rather than an invented box -- a
// wrong boundary is worse than none, and the server is the authority on
// where play happens.
//
// Also reads mc_default_view -- the same field mc.js's loadConfig()
// reads for its own default board -- so both pages agree on which board
// a fresh visitor sees first. One /config fetch serves both needs
// rather than two.
// CARTO now serves an "API KEY REQUIRED" watermark over keyless tiles.
// The key arrives from /config (Settings.carto_api_key) and is appended
// as ?key=... -- these URLs carry no query string of their own, so a
// plain '?' is always the right separator. {ratio} is MapLibre's own
// placeholder and it substitutes it wherever it sits in the string, so
// the suffix after it is untouched. No key -> the original URLs,
// unchanged: still a working basemap, just watermarked.
const CARTO_TILE_URLS = [
  'https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{ratio}.png',
  'https://b.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{ratio}.png',
  'https://c.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{ratio}.png',
];

function cartoTiles(key) {
  const k = String(key || '').trim();
  if (!k) return CARTO_TILE_URLS.slice();
  return CARTO_TILE_URLS.map((u) => `${u}?key=${encodeURIComponent(k)}`);
}

async function fetchBootConfig() {
  try {
    const res = await fetch('/config');
    if (!res.ok) return { playAreaBounds: null, defaultMode: 'meshcore', cartoKey: '' };
    const cfgData = await res.json();
    const pa = cfgData && cfgData.play_area;
    // MapLibre bounds are [lng, lat] pairs, southwest first.
    const playAreaBounds = (pa && finite(pa.north) && finite(pa.south) &&
      finite(pa.west) && finite(pa.east))
      ? [[pa.west, pa.south], [pa.east, pa.north]]
      : null;
    const raw = String(cfgData.mc_default_view || '').trim().toLowerCase();
    const defaultMode = raw === 'meshtastic' ? 'meshtastic' : 'meshcore';
    // Basemap key, supplied by the server from its environment (see
    // Settings.carto_api_key). Absent/blank is a supported state --
    // cartoTiles() below then requests tiles exactly as it always did.
    const cartoKey = String((cfgData && cfgData.carto_api_key) || '').trim();
    return { playAreaBounds, defaultMode, cartoKey };
  } catch {
    return { playAreaBounds: null, defaultMode: 'meshcore', cartoKey: '' };
  }
}

// Failure reporting + boot checkpoints (MAP_LOAD_TIMEOUT_MS,
// sendClientLog, showMapErrorBanner, bootCheckpoint, and the
// window/document listeners that use them) now live at the very top of
// this file, not here -- see that section's comment for why. This
// keeps only the DOM wiring that section didn't need to exist before
// main() runs.
const mapErrorReloadBtn = document.getElementById('mw-map-error-reload');
if (mapErrorReloadBtn) {
  mapErrorReloadBtn.addEventListener('click', () => location.reload());
}

// ===== Collapsible layer switcher =====
//
// The switcher sits bottom-left over the map and, on a phone, over a
// good share of the little screen there is. This slides it off the left
// edge and leaves its tab behind (see map2.html's #mw-layers and the
// .mw-layers rules in map2.css, which own the movement itself -- all
// this does is set the class and keep the button's label honest).
//
// Remembered per browser, same as the theme and the update notice: a
// phone visitor who collapses it should not have to collapse it again
// on every visit. Storage failures are swallowed in both directions --
// an unreadable or unwritable key just means the panel starts open,
// which is the state that hides nothing.
const LAYERS_COLLAPSED_KEY = 'mwLayersCollapsed';

function setupLayersCollapse() {
  const container = document.getElementById('mw-layers');
  const toggle = document.getElementById('mw-layers-toggle');
  if (!container || !toggle) return;

  function apply(collapsed) {
    container.classList.toggle('mw-layers-collapsed', collapsed);
    toggle.setAttribute('aria-expanded', String(!collapsed));
    const label = collapsed ? 'Show the layers panel' : 'Hide the layers panel';
    toggle.setAttribute('aria-label', label);
    toggle.title = label;
  }

  // A <button> already answers Enter and Space and already takes focus,
  // so click is the whole keyboard story -- no key handler to get wrong.
  toggle.addEventListener('click', () => {
    const collapsed = !container.classList.contains('mw-layers-collapsed');
    apply(collapsed);
    try {
      localStorage.setItem(LAYERS_COLLAPSED_KEY, collapsed ? '1' : '0');
    } catch {
      // Storage unavailable (private browsing, quota) -- the choice just
      // lasts for this page view rather than the next one.
    }
  });

  let stored = null;
  try {
    stored = localStorage.getItem(LAYERS_COLLAPSED_KEY);
  } catch {
    stored = null;
  }
  if (stored !== '1') return;

  // Restoring a remembered state must not look like the panel closing
  // itself, so the transitions are suppressed across this one write and
  // released a frame later (two frames -- one is not enough to
  // guarantee the class change has been through style resolution).
  container.classList.add('mw-layers-no-anim');
  apply(true);
  requestAnimationFrame(() => {
    requestAnimationFrame(() => container.classList.remove('mw-layers-no-anim'));
  });
}

// Wired here rather than from main()'s map 'load' handler: this is
// static markup with no dependency on the map, and it should still work
// if the map never finishes loading at all.
setupLayersCollapse();

async function main() {
  // ?board= wins over the configured default. Without this an award link
  // from the Meshtastic results page opened the map on whichever board
  // the config preferred, and drew a Meshtastic highlight over MeshCore
  // territory -- the square the link exists to show was simply not
  // there.
  //
  // Read up here, ahead of fetchBootConfig, purely so loadNotice() below
  // can see needsBoardChoice before it decides whether to fire -- the
  // priority order these feed into (?board= over a remembered choice
  // over /config's default) is unchanged and still resolved into `mode`
  // below, once defaultMode is in hand.
  const linked = boardParam();
  const storedMode = getStoredBoardMode();
  // Whether this load still owes the visitor the first-visit board
  // question below (see showBoardChoiceModal's own comment on why it
  // exists and has no dismiss-without-choosing path). Used twice: once
  // here, to decide whether loadNotice() can run now or has to wait,
  // and again at the showBoardChoiceModal call site itself.
  const needsBoardChoice = !linked && !storedMode;

  // Not awaited: a single small GET against a one-row table, kicked off
  // in parallel with the map's own boot rather than gating first paint
  // on it -- see loadNotice()'s own comment for why a failure here is
  // silent. Held back when needsBoardChoice, though: the board question
  // is the first thing an undecided visitor has to answer -- it is
  // required and has no way to dismiss it -- so the notice, which is
  // neither, cannot be allowed to race that fetch onto the screen. Left
  // to run here, both modals could end up open at once, stacked, with
  // the notice also sitting underneath and stealing clicks meant for
  // the board modal on top of it. showBoardChoiceModal's choose() fires
  // loadNotice() itself once the question is answered, so the notice
  // still appears this same visit, just after, never instead.
  if (!needsBoardChoice) {
    loadNotice();
  }

  const { playAreaBounds, defaultMode, cartoKey } = await fetchBootConfig();
  // Read before anything paints (see BOARD_MODE_KEY's own comment) so a
  // returning visitor's remembered board is baked into `mode` before the
  // scoreboard panel or map.on('load')'s setBoardMode call ever run --
  // there is no intermediate state where the wrong board's chrome is
  // built and then swapped. Below ?board= (a linked award/view should
  // never be overridden by a stale preference) but above /config's
  // default (a remembered choice always beats the operator's fallback).
  mode = linked || storedMode || defaultMode;
  if (!playAreaBounds) {
    console.warn('MeshWars map2: play area bounds unavailable from /config, map is unbounded');
  }

  let map;

  // Scheduled BEFORE `new maplibregl.Map(...)` below, not after: a
  // constructor call that hangs rather than throws never returns
  // control to this function, so anything scheduled only after it
  // returns -- including this watchdog, in the previous version --
  // never gets registered at all. Registering it first means a hanging
  // OR throwing constructor can still be caught, as long as the main
  // thread itself is not blocked. mapLoaded is module-scoped (declared
  // near sendClientLog above) and flipped true in map.on('load') below.
  setTimeout(() => {
    if (mapLoaded) return;
    console.error(`MeshWars map2: map did not fire 'load' within ${MAP_LOAD_TIMEOUT_MS}ms`);
    sendClientLog('map_load_timeout', `no load event after ${MAP_LOAD_TIMEOUT_MS}ms`);
    showMapErrorBanner();
  }, MAP_LOAD_TIMEOUT_MS);

  try {
    bootCheckpoint('t2_pre_ctor', mapGeomSnapshot('t2_pre_ctor', null));
  } catch {
    bootCheckpoint('t2_pre_ctor', 't2_pre_ctor');
  }
  try {
    map = new maplibregl.Map({
      container: 'map',
      bounds: FALLBACK_VIEW_BOUNDS, // opening view -- see its own comment above; geolocation (below, once loaded) can move off it
      fitBoundsOptions: { padding: 40 },
      minZoom: 4,   // roughly the whole play area in view
      maxZoom: 17,  // well past the 300 m grid; squares stay legible
      ...(playAreaBounds ? { maxBounds: playAreaBounds } : {}),
      // Pitching made the hillshade render with holes -- the DEM tiles are
      // not all there once the camera tilts, with nothing erroring to say
      // so -- and it took bandwidth from 8 MB to 32 MB for a worse picture.
      // This is a top-down territory game, so the camera is locked flat
      // rather than the rendering chased.
      maxPitch: 0,
      style: {
        version: 8,
        // Only needed once Places Worth Going's name labels
        // (places-labels, setupPlacesLayer) added the first `text-field`
        // layer this style has ever had -- MapLibre refuses to add ANY
        // symbol layer using text-field without a glyphs template in the
        // style, even one that never renders (board/overlay layers below
        // are all fill/line/hillshade, no text). Served from our own
        // /static mount (frontend/fonts/, see its README.md) rather than
        // MapLibre's public demo glyph server -- the OSM/CARTO basemap
        // tiles below are still fetched from their own public hosts, but
        // the place labels the game itself put on the map should not
        // depend on someone else's demo infrastructure staying up.
        glyphs: '/static/fonts/{fontstack}/{range}.pbf',
        sources: {
          [BASEMAP_ID]: {
            type: 'raster',
            tiles: cartoTiles(cartoKey),
            tileSize: 256,
            attribution: '© OpenStreetMap contributors © CARTO',
            maxzoom: 20,
          },
          // meshwars-hillshade-alpha.pmtiles is finished imagery (WEBP
          // tiles, z0-12, RGBA), not elevation data -- there is nothing
          // left for the browser to shade, so this is a plain raster
          // source, not raster-dem, and carries no `encoding`. maxzoom
          // marks the archive's real ceiling; MapLibre reuses and
          // stretches that z12 tile above it (see the HILLSHADE_ID layer
          // below and the overzoom comment near ROUTE_LINE_WIDTH).
          'hillshade-source': {
            type: 'raster',
            url: `pmtiles://${DEM_URL}`,
            tileSize: 256,
            maxzoom: 12,
          },
        },
        layers: [
          {
            id: BASEMAP_ID,
            type: 'raster',
            source: BASEMAP_ID,
          },
          {
            id: HILLSHADE_ID,
            type: 'raster',
            source: 'hillshade-source',
            // No `maxzoom` here (see the overzoom comment near
            // ROUTE_LINE_WIDTH) -- the map's own maxZoom is 17, and a
            // plain raster layer just keeps reusing the source's last
            // real z12 tile above that rather than vanishing. No
            // `raster-dem` exaggeration to set either: this archive's
            // exaggeration (0.85, the dark theme's former value) is baked
            // into the pixels at build time. raster-opacity is set by
            // applyBasemapTheme (HILLSHADE_OPACITY) instead, right after
            // the map loads, so the basemap underneath -- roads, water,
            // place labels -- still shows through everywhere the imagery's
            // own alpha channel already leaves transparent.
          },
        ],
      },
    });
  } catch (err) {
    console.error('MeshWars map2: map construction failed', err);
    sendClientLog('map_construct_failed', err && err.message);
    showMapErrorBanner();
    return;
  }
  try {
    bootCheckpoint('t3_post_ctor', mapGeomSnapshot('t3_post_ctor', map.getCanvas && map.getCanvas()));
  } catch {
    bootCheckpoint('t3_post_ctor', 't3_post_ctor');
  }

  // Standard corrective, independent of whatever mapGeomSnapshot above
  // finds: a WebGL canvas never resizes itself when its container does,
  // so if #map only gets its real height after layout settles --
  // common on real mobile browsers whose chrome (address bar, etc.)
  // resizes the viewport after first paint -- the canvas MapLibre sized
  // at construction time stays at that stale size until something
  // explicitly tells MapLibre to re-measure. Attached here (right after
  // construction) rather than waiting for 'load', so a container that
  // settles its size before the style finishes loading is still caught.
  // Guarded: ResizeObserver may not exist, and this must never throw.
  try {
    const mapEl = document.getElementById('map');
    if (mapEl && typeof ResizeObserver === 'function') {
      const mapResizeObserver = new ResizeObserver(() => {
        try {
          map.resize();
        } catch {
          // Never let the corrective itself become a page error.
        }
      });
      mapResizeObserver.observe(mapEl);
    }
  } catch {
    // ResizeObserver unavailable -- the map.resize() call inside
    // map.on('load') below still covers the common case.
  }

  // Failure paths for everything a successful constructor above can
  // still go wrong on later: a runtime error MapLibre reports through
  // the map itself (map.on('error') -- a failed tile/style/glyph
  // fetch, a WebGL init problem it catches internally, ...), a 'load'
  // that never arrives at all, and a WebGL context the browser or GPU
  // driver drops out from under an already-running map. Wired here,
  // immediately after construction succeeds, rather than inside the
  // map.on('load') block below -- that block is exactly what a hang
  // looks like, so its own contents can't be where a hang gets caught.
  // (mapLoaded itself is module-scoped now -- see near sendClientLog
  // above -- so the load-timeout watchdog can be scheduled before this
  // point too.)
  map.on('error', (e) => {
    const inner = e && e.error;
    const msg = inner && inner.message ? inner.message : String(inner || (e && e.type) || 'map error');
    console.error('MeshWars map2: map.on(error)', e);
    sendClientLog('map_error_event', msg);
    // MapLibre also fires 'error' for routine, non-fatal problems --
    // most commonly a single tile 404 or an aborted fetch -- which are
    // common and expected on a flaky mobile connection. Once the map
    // has already loaded, that's a hiccup on an otherwise-working map,
    // not the silent-hang failure this section exists to catch: only
    // show the banner (and cover a working map) if 'load' hasn't fired
    // yet. The report above still goes out either way.
    if (mapLoaded) return;
    showMapErrorBanner();
  });
  // Belt-and-braces for the loading overlay: registered here rather
  // than inside map.on('load') below, so it still fires even if 'load'
  // itself is somehow never reached (or its handler throws before
  // getting to the overlay removal). 'idle' fires once the map has
  // finished rendering everything currently queued, which in the
  // healthy case happens shortly after 'load' -- removeLoadingOverlay()
  // is idempotent, so this is a harmless no-op whenever the 'load' path
  // already handled it.
  map.on('idle', () => {
    removeLoadingOverlay();
  });

  const canvas = map.getCanvas && map.getCanvas();
  if (canvas) {
    canvas.addEventListener('webglcontextlost', (e) => {
      // Per spec this is cancelable and preventing default is what
      // requests a restore -- done here regardless of whether MapLibre
      // itself will ever act on it, since it costs nothing and is the
      // documented way to even ask for one.
      if (e && e.preventDefault) e.preventDefault();
      console.error('MeshWars map2: webglcontextlost', e);
      sendClientLog('webgl_context_lost', 'webglcontextlost');
      showMapErrorBanner();
    });
  }

  // Referenced by the places panel's click-to-fly-to handler
  // (renderPlacesPanel) and by the territory panel's player-lookup
  // fitBounds (doPlayerFind) -- both build their markup well after
  // `map` goes out of this function's own scope, so they read the map
  // back off window rather than main() threading it through another
  // layer of closures.
  window.__mwMap = map;

  // Camera is locked flat (see maxPitch above), so drop the compass/pitch
  // button from the nav control -- there is nothing left for it to do.
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-left');

  // Belt-and-suspenders for the flat camera: disable every interaction
  // that could still pitch or rotate the map. Each handler is guarded in
  // case a future MapLibre version renames one.
  if (map.dragRotate) map.dragRotate.disable();
  if (map.touchZoomRotate) map.touchZoomRotate.disableRotation();
  if (map.keyboard) map.keyboard.disableRotation();

  // Opening view, step 2: try to ease onto the viewer's own location,
  // asked for here (never gating the constructor above) so a slow or
  // unanswered permission prompt cannot delay first paint -- the map is
  // already sitting on FALLBACK_VIEW_BOUNDS by the time this fires.
  // movestart's originalEvent is only set when a real drag/scroll/pinch/
  // keyboard action caused it -- MapLibre leaves it undefined for its
  // own programmatic moves (the constructor's bounds fit, and the
  // easeTo below) -- so this is how a viewer who has already grabbed
  // the map is told apart from the map moving itself. Once set, the
  // easeTo below is skipped: don't yank the view out from under someone
  // who has already started looking around.
  let mapViewerInteracted = false;
  map.on('movestart', (e) => {
    if (e && e.originalEvent) mapViewerInteracted = true;
  });
  if (navigator.geolocation && typeof navigator.geolocation.getCurrentPosition === 'function') {
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        if (mapViewerInteracted) return;
        const lng = pos.coords.longitude;
        const lat = pos.coords.latitude;
        // A fix outside the configured play area would just get clamped
        // by maxBounds above -- fighting that clamp reads as a broken,
        // jittery fly-to rather than "no move happened", so detect it
        // up front and stay on the fallback box instead. No playAreaBounds
        // at all (server unreachable) means nothing to check against.
        if (playAreaBounds) {
          const [[west, south], [east, north]] = playAreaBounds;
          if (lng < west || lng > east || lat < south || lat > north) return;
        }
        map.easeTo({ center: [lng, lat], zoom: GEOLOCATION_ZOOM, duration: 1000 });
      },
      () => {
        // Denied, timed out, errored, or otherwise unavailable: this is
        // a nice-to-have, not something the viewer asked for, so stay
        // silent and stay on the fallback box -- no banner, no console
        // noise, no retry.
      },
      {
        enableHighAccuracy: false,
        timeout: GEOLOCATION_TIMEOUT_MS,
        maximumAge: 5 * 60 * 1000, // a fix from the last 5 minutes is close enough to skip a fresh GPS read
      }
    );
  }
  // Nothing above is sent to the server, logged, or persisted (no
  // fetch/sendClientLog/localStorage touches pos.coords) -- it only
  // ever reaches this map.easeTo() call, in this browser tab.

  // Territory panel + winner banner (ported from frontend/mc.js -- see
  // the "Territory panel" section above). Built here, alongside the
  // NavigationControl above, rather than inside map.on('load') below:
  // neither reads anything off the map style, so there is no reason to
  // wait for it, and doing it here means the panel and its seeded
  // all-zero rows are on screen the instant the page paints instead of
  // popping in once tiles start arriving.
  buildScoreboardControl(map);
  renderScoreboard(null); // seed all-zero rows immediately, before the first fetch

  // First-visit board choice: only when needsBoardChoice (computed
  // above, before loadNotice()'s own call) -- see showBoardChoiceModal's
  // own comment for why this one has no dismiss-without-choosing path.
  // Built here rather than inside map.on('load') below for the same
  // reason the panel above is: no dependency on the map style, so no
  // reason to make a first-time visitor wait for tiles before being
  // asked.
  if (needsBoardChoice) {
    showBoardChoiceModal(map);
  }

  // Territory panel starts collapsed on narrow screens only (phones) --
  // it otherwise eats a lot of a phone screen. Desktop always starts
  // (and stays) expanded; see setMcCollapsed / mc.css.
  if (window.matchMedia(`(max-width: ${NARROW_BREAKPOINT_PX}px)`).matches) {
    setMcCollapsed(true);
  }

  map.on('load', () => {
    try {
      bootCheckpoint('t4_load', mapGeomSnapshot('t4_load', map.getCanvas && map.getCanvas()));
    } catch {
      bootCheckpoint('t4_load', 't4_load');
    }
    mapLoaded = true; // read by the load-timeout setTimeout above
    // A 'load' arriving late, after the timeout above already swapped
    // in the error banner, means the map did in fact come up -- pull
    // the banner back down rather than leaving a stale "failed" message
    // over a map that is now working.
    if (mapFailureShown) {
      const banner = document.getElementById('map-error-banner');
      if (banner) banner.hidden = true;
      mapFailureShown = false;
    }

    // Overlay removal runs HERE, immediately once 'load' has fired and
    // before any of the board/places data calls below -- see
    // removeLoadingOverlay()'s own comment for why: the overlay exists
    // only to cover map startup, and 'load' means the map is up, so its
    // removal must not be gated behind data loading that can stall or
    // throw. (Belt-and-braces: also called from a one-shot
    // map.on('idle') handler above, in case this line is never reached.)
    removeLoadingOverlay();

    // t9_probe: fires 4s after 'load' regardless of what else happens
    // in this handler -- confirms whether #loading-overlay actually
    // left the DOM, and if it somehow didn't, captures enough (computed
    // opacity/display, rendered height) to tell a CSS/DOM problem apart
    // from removeLoadingOverlay() never having run at all.
    setTimeout(() => {
      try {
        const el = document.getElementById('loading-overlay');
        if (!el) {
          bootCheckpoint('t9_probe', 't9_probe present=0');
        } else {
          const cs = window.getComputedStyle ? window.getComputedStyle(el) : null;
          const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
          bootCheckpoint(
            't9_probe',
            `t9_probe present=1 opacity=${cs ? cs.opacity : '?'} display=${cs ? cs.display : '?'} rect_h=${rect ? Math.round(rect.height) : '?'}`
          );
        }
      } catch {
        bootCheckpoint('t9_probe', 't9_probe error');
      }
    }, 4000);

    // Board source/layers are created empty and filled in once the
    // fetch resolves, so 'board-fill' exists immediately as a stable
    // beforeId for the overlay layers below.
    // MapLibre tiles a GeoJSON source and simplifies it before drawing,
    // and at low zoom that simplification dropped every 300-metre square
    // entirely -- the board vanished rather than merely shrinking.
    // tolerance: 0 disables the simplification so the squares hold their
    // colour at every zoom, the way the Leaflet map does: Leaflet draws
    // rectangles straight to the canvas and never tiles them, so it never
    // had this problem.
    map.addSource('board', {
      type: 'geojson',
      data: { type: 'FeatureCollection', features: [] },
      tolerance: 0,
    });
    map.addLayer({
      id: 'board-fill',
      type: 'fill',
      source: 'board',
      paint: {
        'fill-color': teamMatchExpression(),
        'fill-opacity': 0.45,
      },
    });
    map.addLayer({
      id: 'board-line',
      type: 'line',
      source: 'board',
      paint: {
        'line-color': teamMatchExpression(),
        'line-width': 1,
      },
    });

    // An award highlight sits ON TOP of the board: same squares, drawn
    // again opaque with a bright outline so the shape reads through the
    // 0.45-opacity board fill underneath it. Empty until showAwardFromUrl
    // puts something in it, so it costs nothing on a normal visit.
    map.addSource('award-highlight', {
      type: 'geojson',
      data: { type: 'FeatureCollection', features: [] },
      tolerance: 0,
    });
    map.addLayer({
      id: 'award-highlight-fill',
      type: 'fill',
      source: 'award-highlight',
      paint: { 'fill-color': teamMatchExpression(), 'fill-opacity': 0.9 },
    });
    map.addLayer({
      id: 'award-highlight-line',
      type: 'line',
      source: 'award-highlight',
      paint: {
        'line-color': '#ffffff',
        // Frontier marks its furthest square -- the one the detail line
        // on /results is quoting -- with a heavier ring than the rest.
        'line-width': ['case', ['==', ['get', 'furthest'], true], 4, 2],
        'line-opacity': 0.85,
      },
    });
    // The place honors (Tourist / Park Hopper / Peak Tagger) are points,
    // not squares: the places themselves, not the ground around them.
    map.addLayer({
      id: 'award-highlight-point',
      type: 'circle',
      source: 'award-highlight',
      filter: ['==', ['geometry-type'], 'Point'],
      paint: {
        'circle-radius': 7,
        'circle-color': '#d9b45b',
        'circle-stroke-color': '#ffffff',
        'circle-stroke-width': 2,
        'circle-opacity': 0.95,
      },
    });

    setupOverlayLayers(map);
    // Wrapped on its own, separate from the try/catch already around
    // setBoardMode/loadPlacesViewport/loadPlacesPanel below: icon
    // registration draws/validates raster images against MapLibre's
    // own internals (see drawPlaceIcon's dim-rounding comment above
    // for the specific bug this guards against), which is a strictly
    // narrower failure surface than the board/places data flow, and
    // a thrown or future stall in here must not be able to take the
    // rest of this handler down with it -- setupPlacesLayer and
    // everything after it must still run even with no place icons
    // registered at all (places would then fall back to whatever
    // MapLibre does for a missing image, never a dead board).
    try {
      registerPlaceIcons(map);
    } catch (err) {
      sendClientLog('register_place_icons_failed', err && err.message);
    }
    setupPlacesLayer(map);
    setupCellClickPopup(map);
    setupLayerSwitcher(map);
    watchTheme(map);
    applyBasemapTheme(map);

    // Same call mc.js's boot() makes (setMode(defaultMode)) -- fills in
    // the panel's title/toggle state for the /config-derived `mode` set
    // in main() above, and does the same board/scoreboard/banner load
    // the periodic refresh below repeats every 30s.
    // Wrapped and re-thrown (not swallowed) rather than left bare: the
    // overlay removal above no longer depends on these three calls
    // completing, but a throw here would previously vanish silently
    // past this point -- reporting it keeps that failure mode visible
    // without changing what happens to the exception itself.
    try {
      setBoardMode(mode, map);
      loadPlacesViewport(map);
      loadPlacesPanel(map);
      // Deliberately OUTSIDE the three calls above in effect: it is
      // async and never awaited, so a slow or failing award fetch
      // cannot delay or break the board coming up. It only ever runs
      // when /results linked here with award params.
      // Before the award fetch: a linked view should be framed even if
      // the award geometry is slow or absent. showAwardFromUrl's own
      // fitBounds overrides it when there IS geometry to frame.
      goToViewFromUrl(map);
      showAwardFromUrl(map);
      // t5_post_dataload: proves whether the three calls above complete
      // (return control here) at all, independent of the overlay, which
      // by this point has already been handled above regardless.
      bootCheckpoint('t5_post_dataload', 't5_post_dataload');
    } catch (err) {
      sendClientLog('load_handler_threw', err && err.message);
      throw err;
    }

    // Same 30s cadence as frontend/mc.js's own REFRESH_INTERVAL_MS --
    // board squares, the scoreboard, and the winner banner all move on
    // their own (other players capturing cells, a season rolling over),
    // so this is a periodic re-fetch, never a re-fit of the camera: it
    // must not fight a visitor's own panning/zooming, same as the
    // Refresh map button (buildScoreboardControl) and unlike the
    // player-lookup Find (doPlayerFind), which fits deliberately.
    setInterval(() => {
      loadBoardData(map);
      loadScoreboard();
      refreshWinnerBanner();
    }, REFRESH_INTERVAL_MS);
    setInterval(tickCountdown, 1000);

    // Places refresh on moveend, debounced -- a drag or zoom fires many
    // intermediate move events, and only the settled position (bbox for
    // the map markers, centre for the "near here" panel) is worth a
    // request. 250ms is short enough that a reader does not notice the
    // lag, long enough that a multi-step pan/zoom gesture only fires
    // this once at the end of it.
    let placesRefreshTimer = null;
    map.on('moveend', () => {
      clearTimeout(placesRefreshTimer);
      placesRefreshTimer = setTimeout(() => {
        loadPlacesViewport(map);
        loadPlacesPanel(map);
      }, 250);
    });

    // Standard corrective, run once the style has actually finished
    // loading (rather than only at construction time, see the
    // ResizeObserver set up right after `new maplibregl.Map(...)`
    // above): harmless if the canvas was already sized correctly,
    // fixes it if #map's height only settled sometime between t3 and
    // t4.
    try {
      map.resize();
    } catch {
      // Never let this corrective become a page error.
    }
  });
}

main().catch((err) => {
  console.error('MeshWars map2: main() failed', err);
  sendClientLog('main_failed', err && err.message);
  showMapErrorBanner();
});
