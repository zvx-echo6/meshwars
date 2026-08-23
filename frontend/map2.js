/*
 * MeshWars: staging-only MapLibre GL map page (/map2). A proof-of-concept
 * renderer swap for the existing Leaflet page at / (frontend/mc.js) --
 * this file does NOT touch that page or its data model, it just asks the
 * same /api/mc/board and /api/mc/scores endpoints for the same data and
 * draws it with MapLibre GL + a self-hosted PMTiles DEM for hillshade,
 * which Leaflet has no equivalent for.
 *
 * Deliberately minimal: no history, roster, find, rankings, check-in or
 * theme switching. Just the board, a hillshaded terrain basemap, and a
 * plain team/score panel.
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

// pmtiles.js registers no protocol on its own -- this wires the
// pmtiles:// URL scheme into MapLibre's request pipeline so a
// raster-dem source can point straight at a single .pmtiles archive
// instead of a z/x/y tile template.
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

async function main() {
  const map = new maplibregl.Map({
    container: 'map',
    center: [-116.10, 43.76],
    zoom: 10,
    style: {
      version: 8,
      sources: {
        osm: {
          type: 'raster',
          tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
          tileSize: 256,
          attribution: '© OpenStreetMap contributors',
          maxzoom: 19,
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
          id: 'osm',
          type: 'raster',
          source: 'osm',
        },
        {
          id: 'hillshade',
          type: 'hillshade',
          source: 'dem',
          paint: {
            'hillshade-exaggeration': 0.6,
          },
        },
      ],
    },
  });

  map.addControl(new maplibregl.NavigationControl(), 'top-left');

  map.on('load', async () => {
    try {
      const board = await fetchBoard();
      map.addSource('board', {
        type: 'geojson',
        data: board,
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
    } catch (err) {
      console.error('MeshWars map2: failed to load board', err);
    }

    try {
      const scores = await fetchScores();
      renderScores(scores);
    } catch (err) {
      console.error('MeshWars map2: failed to load scores', err);
    }
  });
}

main();
