/*
 * MeshWars: staging-only MapLibre GL map page (/map2). A proof-of-concept
 * renderer swap for the existing Leaflet page at / (frontend/mc.js) --
 * this file does NOT touch that page or its data model, it just asks the
 * same /api/mc/board and /api/mc/scores endpoints for the same data and
 * draws it with MapLibre GL + a self-hosted PMTiles DEM for hillshade,
 * which Leaflet has no equivalent for.
 *
 * Extended (staging only) with: the site's theme system (theme.css +
 * theme-toggle.js, same as every other page), a basemap that follows
 * that theme (light raster under gold, dark under neon, both defined up
 * front and toggled by visibility rather than rebuilt -- rebuilding the
 * style loses the reader's pan/zoom), and four self-hosted PMTiles
 * overlays (public lands, USFS roads/trails, BLM routes) behind a small
 * layer-switcher panel, also visibility-toggled so flipping a checkbox
 * never refetches a source.
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

const DEM_URL = 'https://navi.echo6.co/tiles/planet-dem.pmtiles';
const PUBLIC_LANDS_URL = 'https://navi.echo6.co/tiles/public-lands.pmtiles';
const USFS_TRAILS_ROADS_URL = 'https://navi.echo6.co/tiles/usfs-trails-roads.pmtiles';
const BLM_TRAILS_ROADS_URL = 'https://navi.echo6.co/tiles/blm-trails-roads.pmtiles';

const BASEMAP_GOLD_ID = 'basemap-gold';
const BASEMAP_NEON_ID = 'basemap-neon';
const HILLSHADE_ID = 'hillshade';

// Hillshade reads weaker against the dark neon ground, so it gets a
// slightly higher exaggeration there. Keyed by theme name.
const HILLSHADE_EXAGGERATION = { gold: 0.6, neon: 0.85 };

// Each checkbox id -> the style layer id(s) it toggles. Layout
// visibility only -- no source is ever added or removed here, so
// toggling is instant and never refetches.
const LAYER_TOGGLES = [
  ['mw-layer-hillshade', [HILLSHADE_ID]],
  ['mw-layer-public-lands', ['public-lands-fill', 'public-lands-line']],
  ['mw-layer-usfs-roads', ['usfs-roads-line']],
  ['mw-layer-usfs-trails', ['usfs-trails-line']],
  ['mw-layer-blm-routes', ['blm-routes-line']],
];

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

async function fetchBoard() {
  const res = await fetch('/api/mc/board');
  if (!res.ok) throw new Error(`board fetch failed: ${res.status}`);
  const cells = await res.json();
  return {
    type: 'FeatureCollection',
    features: cells.map(boundsToPolygon),
  };
}

async function fetchScores() {
  const res = await fetch('/api/mc/scores');
  if (!res.ok) throw new Error(`scores fetch failed: ${res.status}`);
  return res.json();
}

function renderScores(data) {
  const list = document.getElementById('mw-scores-list');
  list.innerHTML = '';
  const byTeam = new Map((data.teams || []).map((t) => [t.team, t]));
  for (const team of TEAM_ORDER) {
    const entry = byTeam.get(team) || { total: 0 };
    const li = document.createElement('li');

    const name = document.createElement('span');
    name.className = 'mw-score-name';
    const dot = document.createElement('span');
    dot.className = 'mw-score-dot';
    dot.style.background = TEAM_COLORS[team];
    name.appendChild(dot);
    name.appendChild(document.createTextNode(team));

    const value = document.createElement('span');
    value.className = 'mw-score-value';
    value.textContent = entry.total ?? 0;

    li.appendChild(name);
    li.appendChild(value);
    list.appendChild(li);
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
// only other value the toggle ever writes is 'neon'.
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

// The four self-hosted overlay sources + layers. All defined up front
// (called once, at map load) so every checkbox toggle below only ever
// flips a layout property -- no addSource/addLayer after this point.
// Inserted beforeId 'board-fill' so they always sit under the team
// squares, no matter the draw order MapLibre would otherwise pick.
function setupOverlayLayers(map) {
  map.addSource('public-lands', {
    type: 'vector',
    url: `pmtiles://${PUBLIC_LANDS_URL}`,
  });
  map.addSource('usfs-trails-roads', {
    type: 'vector',
    url: `pmtiles://${USFS_TRAILS_ROADS_URL}`,
  });
  map.addSource('blm-trails-roads', {
    type: 'vector',
    url: `pmtiles://${BLM_TRAILS_ROADS_URL}`,
  });

  map.addLayer({
    id: 'public-lands-fill',
    type: 'fill',
    source: 'public-lands',
    'source-layer': 'public_lands',
    layout: { visibility: 'none' },
    paint: {
      'fill-color': '#4f7a4a',
      'fill-opacity': 0.18,
    },
  }, 'board-fill');
  map.addLayer({
    id: 'public-lands-line',
    type: 'line',
    source: 'public-lands',
    'source-layer': 'public_lands',
    layout: { visibility: 'none' },
    paint: {
      'line-color': '#4f7a4a',
      'line-width': 0.8,
      'line-opacity': 0.5,
    },
  }, 'board-fill');
  map.addLayer({
    id: 'usfs-roads-line',
    type: 'line',
    source: 'usfs-trails-roads',
    'source-layer': 'roads',
    layout: { visibility: 'none' },
    paint: {
      'line-color': '#b06a3a',
      'line-width': 0.8,
    },
  }, 'board-fill');
  map.addLayer({
    id: 'usfs-trails-line',
    type: 'line',
    source: 'usfs-trails-roads',
    'source-layer': 'trails',
    layout: { visibility: 'none' },
    paint: {
      'line-color': '#7a5a2a',
      'line-width': 0.8,
      'line-dasharray': [2, 2],
    },
  }, 'board-fill');
  map.addLayer({
    id: 'blm-routes-line',
    type: 'line',
    source: 'blm-trails-roads',
    'source-layer': 'blm_routes',
    layout: { visibility: 'none' },
    paint: {
      'line-color': '#8a6a4a',
      'line-width': 0.7,
    },
  }, 'board-fill');
}

function setupLayerSwitcher(map) {
  for (const [checkboxId, layerIds] of LAYER_TOGGLES) {
    const checkbox = document.getElementById(checkboxId);
    if (!checkbox) continue;
    checkbox.addEventListener('change', () => {
      const visibility = checkbox.checked ? 'visible' : 'none';
      for (const layerId of layerIds) {
        map.setLayoutProperty(layerId, 'visibility', visibility);
      }
    });
  }
}

async function loadBoardData(map) {
  try {
    const board = await fetchBoard();
    map.getSource('board').setData(board);
  } catch (err) {
    console.error('MeshWars map2: failed to load board', err);
  }
}

async function loadScores() {
  try {
    const scores = await fetchScores();
    renderScores(scores);
  } catch (err) {
    console.error('MeshWars map2: failed to load scores', err);
  }
}

async function main() {
  const bootTheme = currentTheme();

  const map = new maplibregl.Map({
    container: 'map',
    center: [-116.10, 43.76],
    zoom: 10,
    style: {
      version: 8,
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
          paint: {
            'hillshade-exaggeration': HILLSHADE_EXAGGERATION[bootTheme],
          },
        },
      ],
    },
  });

  map.addControl(new maplibregl.NavigationControl(), 'top-left');

  map.on('load', () => {
    // Board source/layers are created empty and filled in once the
    // fetch resolves, so 'board-fill' exists immediately as a stable
    // beforeId for the overlay layers below.
    map.addSource('board', {
      type: 'geojson',
      data: { type: 'FeatureCollection', features: [] },
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
    setupLayerSwitcher(map);
    watchTheme(map);
    applyBasemapTheme(map);

    loadBoardData(map);
    loadScores();
  });
}

main();
