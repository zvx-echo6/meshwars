/*
 * MeshWars: front page (/), also reachable at /map2. Boots a MapLibre GL
 * map -- the renderer swap for the original Leaflet page, now kept at
 * /map-legacy (frontend/mc.js) -- and draws it with a self-hosted
 * PMTiles DEM for hillshade, which Leaflet has no equivalent for.
 *
 * Also carries: the site's theme system (theme.css + theme-toggle.js,
 * same as every other page, but defaulting to the neon/dark theme here
 * specifically -- see the boot snippet in map2.html), a basemap that
 * follows that theme (light raster under gold, dark under neon, both
 * defined up front and toggled by visibility rather than rebuilt --
 * rebuilding the style loses the reader's pan/zoom), three self-hosted
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

// USFS roads/trails and public lands now ship with the game itself
// (same-origin /tiles/, see app/api.py's tiles_dir mount) rather than
// being fetched from navi at runtime -- navi's archives got rebuilt in
// place, keeping the same filename, and a browser that already held
// byte ranges of the previous file would happily keep serving them
// against a file that had since changed shape underneath it. That
// showed up as a region silently missing rather than as an error.
// Bump TILE_REV whenever a served archive changes, so the URL changes
// and nothing stale can survive.
//
// The DEM/hillshade source is the one archive still on navi: the
// pre-baked hillshade pmtiles (meshwars-hillshade.pmtiles) wasn't
// finished baking yet when the other two moved local. Point it local
// too once that bake lands and is copied over -- see docs/ for the
// same navi-rebuild caveat this comment used to describe for all three.
const TILE_REV = '20260824e';
const DEM_URL = `https://navi.echo6.co/tiles/planet-dem.pmtiles?r=${TILE_REV}`;
const PUBLIC_LANDS_URL = `/tiles/public-lands.pmtiles?r=${TILE_REV}`;
const USFS_TRAILS_ROADS_URL = `/tiles/usfs-trails-roads.pmtiles?r=${TILE_REV}`;

const BASEMAP_GOLD_ID = 'basemap-gold';
const BASEMAP_NEON_ID = 'basemap-neon';
const HILLSHADE_ID = 'hillshade';

// Hillshade reads weaker against the dark neon ground, so it gets a
// slightly higher exaggeration there. Keyed by theme name.
const HILLSHADE_EXAGGERATION = { gold: 0.6, neon: 0.85 };

// Team territory washes into the dark neon basemap/hillshade at the
// gold theme's weights, so it gets more fill opacity and a heavier
// outline there. Gold is untouched -- it already reads fine. Keyed by
// theme name, same pattern as HILLSHADE_EXAGGERATION above.
const BOARD_FILL_OPACITY = { gold: 0.45, neon: 0.65 };
const BOARD_LINE_WIDTH = { gold: 1, neon: 2 };

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
  ['mw-layer-places', ['places-icons-summit', 'places-icons-park', 'places-icons-landmark', 'places-labels'], 0],
];

// Places Worth Going (docs/features/places.md). Colours are
// deliberately NOT team colours (TEAM_COLORS above) -- a place is a
// separate scoring layer from square ownership, and reusing red/green/
// blue/etc. here would read as if a place belonged to a team. Shape is
// the primary signal per tier (summit=triangle, park=circle,
// landmark=diamond); colour is secondary and mostly there so the three
// still read apart from each other at a glance against the basemap.
// Landmark is deliberately NOT a square: the board's territory tiles
// are axis-aligned squares, and an axis-aligned square marker sitting
// among a run of captured tiles (e.g. along a highway) is
// indistinguishable from one at a glance. A diamond is a square
// rotated 45 degrees -- it keeps the tier's "squared-off" family
// relation but a rotated shape never reads as a grid tile, since the
// board itself is never drawn rotated.
const PLACE_COLORS = {
  summit: '#e8b84b',    // warm gold -- matches --mw-gold, the site's own "this matters most" accent
  park: '#2ec4b6',       // teal
  landmark: '#8892a0',   // slate
};

// Base pixel sizes (not degrees -- these must stay a constant SCREEN
// size across zoom, like any map marker, unlike the board squares
// which are drawn to true ground scale). Summits are sized to
// dominate, per the brief: a mountain worth 100 points should read as
// the biggest thing on the map, a park (25) in between, a landmark (5)
// smallest. Neon gets a larger baseline than gold -- a marker sized to
// read against gold's light basemap washes out against neon's dark
// hillshade and the orange territory squares underneath. Gold is left
// as it was; same reasoning as BOARD_FILL_OPACITY/BOARD_LINE_WIDTH
// below, it already reads fine. Zoom-interpolated on top of this via
// PLACE_ICON_SIZE_ZOOM -- see there for why.
const PLACE_ICON_PX = {
  gold: { summit: 22, park: 15, landmark: 9 },
  neon: { summit: 30, park: 20, landmark: 13 },
};

// Outline drawn around each marker's fill in drawPlaceIcon. This, not
// raw size, is what actually separates a filled shape from a dark
// hillshade and the orange territory squares underneath -- gold's
// existing soft dark outline is left untouched (it already reads fine
// against gold's light basemap), neon needs the opposite: a light
// halo, same problem BOARD_FILL_OPACITY/BOARD_LINE_WIDTH solved for
// the board layer, same per-theme pattern.
const PLACE_ICON_OUTLINE = {
  gold: { color: 'rgba(0,0,0,0.55)', width: 1 },
  neon: { color: 'rgba(244,241,232,0.95)', width: 1.5 },
};

// Park boundaries (app/places_api.py's `park_boundaries` -- a matched
// PAD-US polygon at or above one grid cell, docs/features/places.md).
// A big park still keeps its circle marker at every zoom (see
// setupPlacesLayer/PLACE_TYPE_MIN_ZOOM below) -- the outline is drawn
// ON TOP of that, only once zoomed in enough for the shape to mean
// anything, so the park is still findable by its marker at a whole-
// region zoom even though the outline itself has not appeared yet.
// MIN_BOUNDARY_ZOOM must match app/places_api.py's own constant of the
// same name -- it is both the client's own layer minzoom AND the zoom
// this page starts asking the server for boundary geometry at all
// (fetchPlacesInViewport passes map.getZoom() in the `zoom` param), so
// a mismatch here would either fetch boundary data that never draws or
// draw a layer that never has data.
const MIN_BOUNDARY_ZOOM = 11;

// Colour reuses PLACE_COLORS.park (the same teal the circle marker
// already draws in) rather than inventing a new one -- this is the
// same park, just drawn two ways at once. Fill stays close to nothing
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
// per-theme base pixel sizes above: with ~80,000 places now seeded
// (43,639 parks + 30,408 landmarks + 6,487 summits, plus rotating live
// ones), a size tuned to read at zoom 13 close-in tiles into a solid
// mass at zoom 9's region view if left constant. Same curve for both
// themes -- the per-theme PX values above already carry the theme
// difference. Summits alone carry PLACE_TYPE_MIN_ZOOM 0, so they are
// the only tier drawn below zoom 10 -- interpolate clamps to the
// lowest stop's value for any zoom below it, so the original curve
// (first stop at 9) rendered every summit at that same size all the
// way out to a whole-state view, where they read as chunky triangles
// blanketing the terrain. Stops now reach down to zoom 3 (global/
// regional) at a small fraction of the zoom-13 size -- just enough to
// say "a peak is here" -- and grow through the mid zooms up to the
// same zoom-11/13 values as before, so close-in legibility is
// unchanged. Since icon-size scales the whole rasterized icon
// (drawPlaceIcon bakes the outline into the same raster at
// PLACE_ICON_OUTLINE's width), the outline thins proportionally with
// the fill at low zoom for free -- no separate outline curve needed.
const PLACE_ICON_SIZE_ZOOM = ['interpolate', ['linear'], ['zoom'], 3, 0.1, 6, 0.2, 8, 0.32, 9, 0.6, 11, 0.85, 13, 1.1];

const PLACE_TYPES = ['summit', 'park', 'landmark'];

// Per-tier reveal zoom (see setupPlacesLayer's three-layer split).
// City parks alone run ~43,600 -- at a whole-region zoom (e.g. zoom 9
// over Boise) that many 25-point circles visually bury both the rare
// 100-point summits and the 5-point landmarks, backwards from what
// matters. Gating by value instead of shrinking further (shrinking
// undoes the whole point of this fix) makes the reveal match what is
// worth going to at that scale: summits from the map's own minzoom
// (rare and the highest tier -- the thing to see scanning a whole
// region), parks from zoom 10 (a sub-region has narrowed into view),
// landmarks from zoom 11 (already looking at one town, same zoom as
// the Twin Falls reference case). Chosen by screenshot at zoom 7/9/11/
// 13 -- see the deploy notes for what else was tried.
const PLACE_TYPE_MIN_ZOOM = { summit: 0, park: 10, landmark: 11 };

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

// pmtiles.js registers no protocol on its own -- this wires the
// pmtiles:// URL scheme into MapLibre's request pipeline so a
// raster-dem source can point straight at a single .pmtiles archive
// instead of a z/x/y tile template. The same protocol also serves the
// vector overlay archives below.
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
// Also not ported: per-cell click popups. Correction to an earlier note
// here -- mc.js's Leaflet page (bindCellPopup / buildCellPopupHtml)
// DOES have these, showing a square's owner/captures/repeaters on
// click; this page's board-fill layer still has no click handler at
// all. That is a real gap between the two pages, not a gap shared by
// both -- flagged in the deploy report, not implemented in this pass.
const PROTOCOLS = {
  meshcore: {
    protocol: 'mc',
    boardTitle: 'MeshCore Territory',
    topButtonLabel: 'Top Operators',
    topCaptureLabel: 'Wardrivers',
    topCheckinLabel: 'NetOps',
    lookupPlaceholder: 'player name',
    lookupHelp: 'Search by player name.',
    boardEndpoint: '/api/mc/board',
    scoresEndpoint: '/api/mc/scores',
    historyEndpoint: '/api/mc/history',
    rosterEndpoint: '/api/mc/players',
    findEndpoint: (q) => `/api/mc/find?name=${encodeURIComponent(q)}`,
    topEndpoint: '/api/mc/top',
    topCheckinEndpoint: '/api/mc/top-checkins',
    seasonEndpoint: '/api/mc/season',
  },
  meshtastic: {
    protocol: 'mt',
    boardTitle: 'Meshtastic Territory',
    topButtonLabel: 'Top Operators',
    topCaptureLabel: 'Wardrivers',
    topCheckinLabel: 'NetOps',
    lookupPlaceholder: 'player name',
    lookupHelp: 'Search by player name.',
    boardEndpoint: '/get-nodes',
    scoresEndpoint: '/scores',
    historyEndpoint: '/history',
    rosterEndpoint: '/teams',
    findEndpoint: (q) => `/find?name=${encodeURIComponent(q)}`,
    topEndpoint: '/top',
    topCheckinEndpoint: '/top-checkins',
    seasonEndpoint: '/season',
  },
};

const REFRESH_INTERVAL_MS = 30000;

// ---- module state (territory panel) ----
let mode = 'meshcore'; // key into PROTOCOLS -- set from /config's
                        // mc_default_view before the panel is built
                        // (see fetchBootConfig/main), same source of
                        // truth mc.js's loadConfig() reads. The board
                        // choice itself is never written to
                        // localStorage on either page -- mc.js persists
                        // only the map view itself (its 'mapView' key),
                        // never which board was showing, and this page
                        // does not persist the map view at all yet.
function cfg() {
  return PROTOCOLS[mode];
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
// points (app/mc_scoring.py's team_totals()) -- the combined figure
// that actually decides the season now, not squares alone. Falls back
// to tiles-only for the all-zero seed row renderScoreboard(null) hands
// out before the first real fetch, which has no checkin_points/total
// fields at all yet.
function teamTotal(t) {
  if (typeof t.total === 'number') return t.total;
  return t.tiles ?? 0;
}

function teamBreakdown(t) {
  const tiles = t.tiles ?? 0;
  const pts = t.checkin_points ?? 0;
  const squareWord = tiles === 1 ? 'square' : 'squares';
  const pointWord = pts === 1 ? 'point' : 'points';
  return `${tiles} ${squareWord} + ${pts} check-in ${pointWord}`;
}

function teamBreakdownCompact(t) {
  const tiles = t.tiles ?? 0;
  const pts = t.checkin_points ?? 0;
  return `${tiles}+${pts}`;
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
async function doPlayerFind(value) {
  const resultEl = document.getElementById('mc-lookup-result');
  if (!resultEl) return;
  const query = (value || '').trim();
  if (!query) { resultEl.textContent = ''; return; }
  resultEl.textContent = 'Searching...';

  try {
    const res = await fetch(cfg().findEndpoint(query));
    if (res.status === 404) {
      resultEl.textContent = `Not found: ${query}`;
      return;
    }
    if (!res.ok) {
      resultEl.textContent = 'Search failed.';
      return;
    }
    const data = await res.json();
    if (!data.bounds || !data.tiles_held) {
      resultEl.textContent = `${data.display_name} (${data.team}) holds no cells right now.`;
      return;
    }
    const b = data.bounds;
    // MapLibre bounds are [[west, south], [east, north]] -- Leaflet's
    // equivalent call (mc.js's doPlayerFind) uses [lat, lng] order
    // instead; only the coordinate order changes here, not the padding
    // or the MAX_FIT_ZOOM cap.
    if (window.__mwMap) {
      window.__mwMap.fitBounds([[b.west, b.south], [b.east, b.north]], { padding: 24, maxZoom: MAX_FIT_ZOOM });
    }
    const plural = data.tiles_held === 1 ? '' : 's';
    resultEl.textContent = `${data.display_name} (${data.team}) holds ${data.tiles_held} cell${plural}.`;
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
        .map((t) => `${escapeHtml(t.team)} <span class="mc-tally-count" data-total="${escapeHtml(teamTotal(t))}" data-compact="${escapeHtml(teamBreakdownCompact(t))}" title="${escapeHtml(teamBreakdown(t))}">${escapeHtml(teamTotal(t))}</span>`)
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
      const rows = list.map((p) => `<tr><td>${escapeHtml(p.display_name)}</td></tr>`).join('');
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
      <div class="mc-row"><a href="#" id="mc-history-link">History</a> &nbsp;|&nbsp; <a href="#" id="mc-roster-link">Roster</a></div>
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
  });
  div.querySelector('#mc-toggle-meshcore').addEventListener('click', (e) => {
    e.stopPropagation();
    setBoardMode('meshcore', map);
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
}

// ---- Places Worth Going (docs/features/places.md) ----------------------

function placeToFeature(p) {
  return {
    type: 'Feature',
    properties: { id: p.id, type: p.type, name: p.name, points: p.points },
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

// Draws a tiny raster icon for one place tier on an offscreen canvas
// and hands it to MapLibre via map.addImage -- no sprite sheet or
// external asset file, generated at runtime instead, same
// self-contained spirit as everything else this page ships with. A
// symbol layer (not a fill layer of ground polygons the way the board
// squares are drawn) is what keeps these a constant PIXEL size across
// zoom: a place marker is a marker, not a to-scale shape on the ground.
// outline is a {color, width} pair (PLACE_ICON_OUTLINE) -- width is in
// CSS px, doubled below for the same 2x-canvas reason dim is.
function drawPlaceIcon(shape, color, sizePx, outline) {
  const canvas = document.createElement('canvas');
  // 2x for a crisp icon on high-DPI screens; MapLibre reads pixelRatio
  // separately from the addImage options below.
  const dim = sizePx * 2;
  canvas.width = dim;
  canvas.height = dim;
  const ctx = canvas.getContext('2d');
  const cx = dim / 2, cy = dim / 2, r = dim / 2 - 2;

  ctx.fillStyle = color;
  ctx.strokeStyle = outline.color;
  ctx.lineWidth = outline.width * 2;

  if (shape === 'triangle') {
    ctx.beginPath();
    ctx.moveTo(cx, cy - r);
    ctx.lineTo(cx + r * 0.92, cy + r * 0.8);
    ctx.lineTo(cx - r * 0.92, cy + r * 0.8);
    ctx.closePath();
  } else if (shape === 'diamond') {
    const d = r * 0.95;
    ctx.beginPath();
    ctx.moveTo(cx, cy - d);
    ctx.lineTo(cx + d, cy);
    ctx.lineTo(cx, cy + d);
    ctx.lineTo(cx - d, cy);
    ctx.closePath();
  } else {
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
  }
  ctx.fill();
  ctx.stroke();

  return { data: ctx.getImageData(0, 0, dim, dim).data, width: dim, height: dim };
}

// Registers both themes' icon images up front (place-icon-<type>-gold
// and place-icon-<type>-neon) so applyBasemapTheme can flip which set
// each places-icons-<type> layer points at with a plain
// setLayoutProperty, the same visibility-swap-not-rebuild pattern the
// gold/neon basemap rasters use -- see applyBasemapTheme.
function registerPlaceIcons(map) {
  const shapes = { summit: 'triangle', park: 'circle', landmark: 'diamond' };
  for (const theme of Object.keys(PLACE_ICON_PX)) {
    for (const type of Object.keys(shapes)) {
      const icon = drawPlaceIcon(shapes[type], PLACE_COLORS[type], PLACE_ICON_PX[theme][type], PLACE_ICON_OUTLINE[theme]);
      map.addImage(`place-icon-${type}-${theme}`, icon, { pixelRatio: 2 });
    }
  }
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
  map.addLayer({
    id: 'park-boundaries-fill',
    type: 'fill',
    source: 'park-boundaries',
    minzoom: MIN_BOUNDARY_ZOOM,
    paint: {
      'fill-color': PLACE_COLORS.park,
      'fill-opacity': PARK_BOUNDARY_FILL_OPACITY.gold,
    },
  }, 'board-fill');
  map.addLayer({
    id: 'park-boundaries-line',
    type: 'line',
    source: 'park-boundaries',
    minzoom: MIN_BOUNDARY_ZOOM,
    paint: {
      'line-color': PLACE_COLORS.park,
      'line-width': PARK_BOUNDARY_LINE_WIDTH.gold,
      'line-opacity': PARK_BOUNDARY_LINE_OPACITY.gold,
    },
  }, 'board-fill');

  // One symbol layer per tier, not one shared layer, so each tier can
  // carry its own minzoom (PLACE_TYPE_MIN_ZOOM) -- a single "Places"
  // toggle still controls all three together (see LAYER_TOGGLES), this
  // split is purely about *when* each tier reveals, not a switcher
  // control of its own. Reveals progressively by value: summits (100
  // pts, rare) are visible from the map's own minzoom upward; parks
  // (25 pts, common) fade in at a mid zoom once the region has
  // narrowed to a town-sized area; landmarks (5 pts, most common)
  // reveal last, only once zoomed in on a town, so the most valuable
  // tier is what a reader sees first rather than being swamped by the
  // most numerous one -- see PLACE_TYPE_MIN_ZOOM's own comment.
  for (const type of PLACE_TYPES) {
    map.addLayer({
      id: `places-icons-${type}`,
      type: 'symbol',
      source: 'places',
      filter: ['==', ['get', 'type'], type],
      minzoom: PLACE_TYPE_MIN_ZOOM[type],
      layout: {
        // Theme suffix is filled in properly by applyBasemapTheme right
        // after this layer is added (see boot sequence); this initial
        // value is just a safe default before that first call.
        'icon-image': ['concat', 'place-icon-', ['get', 'type'], '-', currentTheme()],
        'icon-allow-overlap': true,
        'icon-size': PLACE_ICON_SIZE_ZOOM,
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

  const popup = new maplibregl.Popup({ closeButton: true, closeOnClick: true, offset: 12 });
  for (const type of PLACE_TYPES) {
    const layerId = `places-icons-${type}`;
    map.on('click', layerId, (e) => {
      const f = e.features[0];
      if (!f) return;
      const { name, type: t, points } = f.properties;
      popup.setLngLat(f.geometry.coordinates).setHTML(
        `<div class="mw-place-popup">`
        + `<div class="mw-place-popup-name">${escapeHtml(name)}</div>`
        + `<div class="mw-place-popup-meta">${escapeHtml(t)} &middot; ${points} pts</div>`
        + `</div>`
      ).addTo(map);
    });
    map.on('mouseenter', layerId, () => { map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', layerId, () => { map.getCanvas().style.cursor = ''; });
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

async function loadPlacesViewport(map) {
  try {
    const bounds = map.getBounds();
    const data = await fetchPlacesInViewport(bounds, map.getZoom());
    map.getSource('places').setData({
      type: 'FeatureCollection',
      features: data.places.map(placeToFeature),
    });
    // park_boundaries is always present (an empty FeatureCollection
    // below MIN_BOUNDARY_ZOOM, or with no boundary-backed park in
    // view) -- see app/places_api.py's places_in_viewport docstring --
    // so this can set it unconditionally rather than checking first.
    map.getSource('park-boundaries').setData(data.park_boundaries);
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
      `<span class="mw-place-icon" style="background:${PLACE_COLORS[p.type] || '#999'}"></span>`
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

// Flips which raster basemap is visible and re-tunes the hillshade
// exaggeration for it. Never rebuilds the style or touches the board's
// team-colour expression -- that stays constant across themes on
// purpose (gameplay, not branding).
function applyBasemapTheme(map) {
  const theme = currentTheme();
  const neon = theme === 'neon';
  map.setLayoutProperty(BASEMAP_GOLD_ID, 'visibility', neon ? 'none' : 'visible');
  map.setLayoutProperty(BASEMAP_NEON_ID, 'visibility', neon ? 'visible' : 'none');
  map.setPaintProperty(HILLSHADE_ID, 'hillshade-exaggeration', HILLSHADE_EXAGGERATION[theme]);
  map.setPaintProperty('board-fill', 'fill-opacity', BOARD_FILL_OPACITY[theme]);
  map.setPaintProperty('board-line', 'line-width', BOARD_LINE_WIDTH[theme]);
  map.setPaintProperty('park-boundaries-fill', 'fill-opacity', PARK_BOUNDARY_FILL_OPACITY[theme]);
  map.setPaintProperty('park-boundaries-line', 'line-width', PARK_BOUNDARY_LINE_WIDTH[theme]);
  map.setPaintProperty('park-boundaries-line', 'line-opacity', PARK_BOUNDARY_LINE_OPACITY[theme]);
  for (const type of PLACE_TYPES) {
    map.setLayoutProperty(`places-icons-${type}`, 'icon-image', ['concat', 'place-icon-', ['get', 'type'], '-', theme]);
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
// way to the map's own maxZoom (17) rather than stopping early.
// (Unlike the raster DEM hillshade above, which tore on overzoom and is
// deliberately cut off at 13 -- left alone.)
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

const finite = (v) => typeof v === 'number' && Number.isFinite(v);

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
async function fetchBootConfig() {
  try {
    const res = await fetch('/config');
    if (!res.ok) return { playAreaBounds: null, defaultMode: 'meshcore' };
    const cfgData = await res.json();
    const pa = cfgData && cfgData.play_area;
    // MapLibre bounds are [lng, lat] pairs, southwest first.
    const playAreaBounds = (pa && finite(pa.north) && finite(pa.south) &&
      finite(pa.west) && finite(pa.east))
      ? [[pa.west, pa.south], [pa.east, pa.north]]
      : null;
    const raw = String(cfgData.mc_default_view || '').trim().toLowerCase();
    const defaultMode = raw === 'meshtastic' ? 'meshtastic' : 'meshcore';
    return { playAreaBounds, defaultMode };
  } catch {
    return { playAreaBounds: null, defaultMode: 'meshcore' };
  }
}

async function main() {
  const bootTheme = currentTheme();
  const { playAreaBounds, defaultMode } = await fetchBootConfig();
  mode = defaultMode;
  if (!playAreaBounds) {
    console.warn('MeshWars map2: play area bounds unavailable from /config, map is unbounded');
  }

  const map = new maplibregl.Map({
    container: 'map',
    center: [-116.10, 43.76],
    zoom: 10,
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
        [BASEMAP_GOLD_ID]: {
          type: 'raster',
          tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
          tileSize: 256,
          attribution: '© OpenStreetMap contributors',
          maxzoom: 19,
        },
        [BASEMAP_NEON_ID]: {
          type: 'raster',
          tiles: [
            'https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{ratio}.png',
            'https://b.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{ratio}.png',
            'https://c.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{ratio}.png',
          ],
          tileSize: 256,
          attribution: '© OpenStreetMap contributors © CARTO',
          maxzoom: 20,
        },
        dem: {
          type: 'raster-dem',
          url: `pmtiles://${DEM_URL}`,
          encoding: 'terrarium',
          tileSize: 256,
          maxzoom: 12,
        },
      },
      layers: [
        {
          id: BASEMAP_GOLD_ID,
          type: 'raster',
          source: BASEMAP_GOLD_ID,
          layout: { visibility: bootTheme === 'neon' ? 'none' : 'visible' },
        },
        {
          id: BASEMAP_NEON_ID,
          type: 'raster',
          source: BASEMAP_NEON_ID,
          layout: { visibility: bootTheme === 'neon' ? 'visible' : 'none' },
        },
        {
          id: HILLSHADE_ID,
          type: 'hillshade',
          source: 'dem',
          // The DEM source itself only reaches z12 (that correctly
          // describes the data, left alone above). Above z12 MapLibre
          // was reusing whichever z12 parent tiles happened to already
          // be cached, stretched, and never fetching the rest -- so the
          // hillshade teared along tile seams with nothing erroring to
          // say so. Cutting the LAYER off at 13 makes that predictable
          // (uniformly gone instead of half-there), and hillshade is
          // meaningless at street zoom anyway: the whole view sits
          // inside one elevation sample by then.
          maxzoom: 13,
          paint: {
            'hillshade-exaggeration': HILLSHADE_EXAGGERATION[bootTheme],
          },
        },
      ],
    },
  });

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

  // Territory panel + winner banner (ported from frontend/mc.js -- see
  // the "Territory panel" section above). Built here, alongside the
  // NavigationControl above, rather than inside map.on('load') below:
  // neither reads anything off the map style, so there is no reason to
  // wait for it, and doing it here means the panel and its seeded
  // all-zero rows are on screen the instant the page paints instead of
  // popping in once tiles start arriving.
  buildScoreboardControl(map);
  renderScoreboard(null); // seed all-zero rows immediately, before the first fetch

  // Territory panel starts collapsed on narrow screens only (phones) --
  // it otherwise eats a lot of a phone screen. Desktop always starts
  // (and stays) expanded; see setMcCollapsed / mc.css.
  if (window.matchMedia(`(max-width: ${NARROW_BREAKPOINT_PX}px)`).matches) {
    setMcCollapsed(true);
  }

  map.on('load', () => {
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

    setupOverlayLayers(map);
    registerPlaceIcons(map);
    setupPlacesLayer(map);
    setupLayerSwitcher(map);
    watchTheme(map);
    applyBasemapTheme(map);

    // Same call mc.js's boot() makes (setMode(defaultMode)) -- fills in
    // the panel's title/toggle state for the /config-derived `mode` set
    // in main() above, and does the same board/scoreboard/banner load
    // the periodic refresh below repeats every 30s.
    setBoardMode(mode, map);
    loadPlacesViewport(map);
    loadPlacesPanel(map);

    const loadingOverlay = document.getElementById('loading-overlay');
    if (loadingOverlay) {
      loadingOverlay.classList.add('fade-out');
      setTimeout(() => loadingOverlay.remove(), 500);
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
  });
}

main();
