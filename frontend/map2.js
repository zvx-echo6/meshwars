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
 * style loses the reader's pan/zoom), three self-hosted PMTiles
 * overlays (public lands, USFS roads/trails) and a contour overlay
 * generated client-side from the same DEM used for hillshade (via
 * mlcontour -- no tileset of its own), all behind a small
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

// The archives on navi get rebuilt in place, keeping the same filename,
// and a browser that already holds byte ranges of the previous file will
// happily keep serving them -- pointing into a file that has since
// changed shape. That shows up as a region silently missing rather than
// as an error. Bump TILE_REV whenever an archive on navi is replaced, so
// the URL changes and nothing stale can survive.
const TILE_REV = '20260824d';
const DEM_URL = `https://navi.echo6.co/tiles/planet-dem.pmtiles?r=${TILE_REV}`;
const PUBLIC_LANDS_URL = `https://navi.echo6.co/tiles/public-lands.pmtiles?r=${TILE_REV}`;
const USFS_TRAILS_ROADS_URL = `https://navi.echo6.co/tiles/usfs-trails-roads.pmtiles?r=${TILE_REV}`;

const BASEMAP_GOLD_ID = 'basemap-gold';
const BASEMAP_NEON_ID = 'basemap-neon';
const HILLSHADE_ID = 'hillshade';

// Hillshade reads weaker against the dark neon ground, so it gets a
// slightly higher exaggeration there. Keyed by theme name.
const HILLSHADE_EXAGGERATION = { gold: 0.6, neon: 0.85 };

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
  ['mw-layer-contours', ['contours-line'], 0],
  ['mw-layer-public-lands', ['public-lands-fill', 'public-lands-line'], 4],
  ['mw-layer-usfs-roads', ['usfs-roads-line'], 6],
  ['mw-layer-usfs-trails', ['usfs-trails-line'], 6],
];

// pmtiles.js registers no protocol on its own -- this wires the
// pmtiles:// URL scheme into MapLibre's request pipeline so a
// raster-dem source can point straight at a single .pmtiles archive
// instead of a z/x/y tile template. The same protocol also serves the
// vector overlay archives below.
const pmtilesProtocol = new pmtiles.Protocol();
maplibregl.addProtocol('pmtiles', pmtilesProtocol.tile);

// Contours are generated in the browser from the same DEM archive as
// the hillshade above -- no second tileset to build or serve. mlcontour
// (loaded from unpkg in map2.html, global `mlcontour`) reads DEM tiles
// through its own dem:// protocol and rasterizes contour lines into
// vector tiles on demand through a contour:// protocol; setupMaplibre
// registers both with MapLibre. This has to happen once, before the map
// style below is built, same as the pmtiles protocol above.
const demSourceInstance = new mlcontour.DemSource({
  url: `pmtiles://${DEM_URL}`,
  encoding: 'terrarium',
  maxzoom: 12,
  worker: true,
  cacheSize: 100,
});
demSourceInstance.setupMaplibre(maplibregl);

// zoom -> [minor, major] contour interval in feet. navi's table,
// verbatim -- the multiplier passed alongside it in setupContourLayer
// below converts the DEM's native metres to feet to match.
const CONTOUR_THRESHOLDS = {
  3: [5000, 25000],
  4: [2500, 10000],
  5: [1000, 5000],
  6: [1000, 5000],
  7: [500, 2500],
  8: [500, 2500],
  9: [250, 1000],
  10: [200, 1000],
  11: [200, 1000],
  12: [100, 500],
  13: [100, 500],
  14: [50, 200],
  15: [20, 100],
};

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

// Contour source: vector tiles generated in the browser from the DEM
// via mlcontour (demSourceInstance, set up above), not a second pmtiles
// archive -- contourProtocolUrl hands back a contour://{z}/{x}/{y}
// template already carrying the threshold table and the metres->feet
// multiplier, so this is otherwise a plain vector source.
// Contours are terrain reference, not a feature layer: thin, low
// opacity, and a muted tone picked to read on both the light OSM
// basemap and the dark CARTO one rather than being tuned to either.
// The library tags every generated line with a `level` property (1 for
// the heavier contour of a zoom's [minor, major] pair, 0 for the
// lighter one -- confirmed in the library's own tile output, not
// assumed), so major contours get a bit more width and opacity than
// minor ones for free via that property.
function setupContourLayer(map) {
  map.addSource('contours', {
    type: 'vector',
    tiles: [demSourceInstance.contourProtocolUrl({
      multiplier: 3.28084,
      thresholds: CONTOUR_THRESHOLDS,
    })],
    maxzoom: 16,
  });
  map.addLayer({
    id: 'contours-line',
    type: 'line',
    source: 'contours',
    'source-layer': 'contours',
    minzoom: 0,
    layout: { visibility: 'none' },
    paint: {
      'line-color': '#8f8168',
      'line-width': ['match', ['get', 'level'], 1, 1, 0.6],
      'line-opacity': ['match', ['get', 'level'], 1, 0.55, 0.3],
    },
  }, 'board-fill');
}

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

const finite = (v) => typeof v === 'number' && Number.isFinite(v);

// Read from /config rather than written down here, same reasoning as
// frontend/play-area-map.js: the play area is an operator setting that
// has moved before, and a hardcoded copy in this file would silently
// disagree with the server a month later. If /config is unreachable or
// the numbers are missing/non-finite, return null so the map is built
// with no maxBounds rather than an invented box -- a wrong boundary is
// worse than none, and the server is the authority on where play
// happens.
async function fetchPlayAreaBounds() {
  try {
    const res = await fetch('/config');
    if (!res.ok) return null;
    const cfg = await res.json();
    const pa = cfg && cfg.play_area;
    if (!pa || !finite(pa.north) || !finite(pa.south) ||
        !finite(pa.west) || !finite(pa.east)) {
      return null;
    }
    // MapLibre bounds are [lng, lat] pairs, southwest first.
    return [[pa.west, pa.south], [pa.east, pa.north]];
  } catch {
    return null;
  }
}

async function main() {
  const bootTheme = currentTheme();
  const playAreaBounds = await fetchPlayAreaBounds();
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

  // Camera is locked flat (see maxPitch above), so drop the compass/pitch
  // button from the nav control -- there is nothing left for it to do.
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-left');

  // Belt-and-suspenders for the flat camera: disable every interaction
  // that could still pitch or rotate the map. Each handler is guarded in
  // case a future MapLibre version renames one.
  if (map.dragRotate) map.dragRotate.disable();
  if (map.touchZoomRotate) map.touchZoomRotate.disableRotation();
  if (map.keyboard) map.keyboard.disableRotation();

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
    setupContourLayer(map);
    setupLayerSwitcher(map);
    watchTheme(map);
    applyBasemapTheme(map);

    loadBoardData(map);
    loadScores();
  });
}

main();
