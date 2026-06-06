import {
  ageInDays,
  centerPos,
  geo,
  haversineMiles,
  initialZoom,
  loadConfig,
  maxDistanceMiles,
  posFromHash,
  pushMap,
  sigmoid,
  fromTruncatedTime,
} from './shared.js'

// Global Init - map will be initialized after config loads
let map = null;
let osm = null;

// Control state
let repeaterRenderMode = 'all';
let repeaterSearch = '';
let showSamples = false;
let colorPalette = 'cyber-green'; // 'cyber-green', 'red-yellow-green', 'blue', 'patterns'
let queryMode = 'coverage'; // 'coverage', 'last-updated', 'past-day', 'repeater-count'

// MESH-TERRITORY: track team-mode override
let territoryMode = true; // when true, team colors override the palette

// Data
let nodes = null; // Graph data from the last refresh
let idToRepeaters = null; // Index of id -> [repeater]
let hashToCoverage = null; // Index of geohash -> coverage
let edgeList = null; // List of connected repeater and coverage
let individualSamples = null; // Individual (non-aggregated) samples

// Map layers (will be initialized after map is created)
let coverageLayer = null;
let edgeLayer = null;
let sampleLayer = null;
let repeaterLayer = null;
let liveTrackLayer = null;

// Live track state
let showLiveTracks = true;
let liveTrackSSE = null;
let liveTrackData = {};
let liveTrackPruneTimer = null;

const TRACK_COLORS = [
  '#00e5ff', '#ff4081', '#ffea00', '#76ff03', '#ff6e40', '#e040fb',
  '#18ffff', '#ffd740', '#69f0ae', '#ff80ab', '#b388ff', '#84ffff',
];

const isMobile = () => window.innerWidth <= 600;

// MESH-TERRITORY: team colors
const TEAM_COLORS = {
  RED: '#d33636',
  BLUE: '#3673d3',
  GREEN: '#2d8a2d',
};

// ===== Main control =====
const mapControl = L.control({ position: 'topright' });
mapControl.onAdd = m => {
  const div = L.DomUtil.create('div', 'mesh-control leaflet-control');

  div.innerHTML = `
    <button type="button" id="mobile-toggle" class="mobile-toggle" title="Settings">⚙</button>
    <div id="control-body" class="control-body">
      <div class="mesh-control-row">
        <label>
          Query:
          <select id="query-mode-select">
            <option value="coverage" selected="true">Coverage</option>
            <option value="last-updated">Last Updated</option>
            <option value="past-day">Past Day</option>
            <option value="repeater-count">Feeder Count</option>
          </select>
        </label>
      </div>
      <div class="mesh-control-row">
        <label>
          Color Palette:
          <select id="color-palette-select">
            <option value="cyber-green" selected="true">Cyber Green</option>
            <option value="red-yellow-green">Red/Yellow/Green</option>
            <option value="blue">Blue</option>
            <option value="patterns">Patterns</option>
            <option value="simple-green">Simple Green</option>
          </select>
        </label>
      </div>
      <div class="mesh-control-row">
        <label>
          Feeders:
          <select id="repeater-filter-select">
            <option value="all" selected="true">All</option>
            <option value="hit">Hit</option>
            <option value="none">None</option>
          </select>
        </label>
      </div>
      <div class="mesh-control-row">
        <label>
          Search:
          <input type="text" id="repeater-search" placeholder="name or id" />
        </label>
      </div>
      <div class="mesh-control-row">
        <label>
          Show Live Wardriving:
          <input type="checkbox" id="show-live-tracks" />
        </label>
      </div>
      <div class="mesh-control-row">
        <label>
          Territory Mode:
          <input type="checkbox" id="territory-mode" checked />
        </label>
      </div>
      <div class="mesh-control-row">
        <button type="button" id="refresh-map-button">Refresh map</button>
      </div>
    </div>
  `;

  const toggle = div.querySelector('#mobile-toggle');
  const body = div.querySelector('#control-body');
  toggle.addEventListener('click', (e) => {
    e.stopPropagation();
    const collapsed = body.classList.toggle('collapsed');
    toggle.textContent = collapsed ? '⚙' : '✕';
    const fp = document.querySelector('.feeders-panel');
    if (fp) fp.style.display = collapsed ? 'none' : '';
  });
  if (isMobile()) body.classList.add('collapsed');

  div.querySelector('#query-mode-select').addEventListener('change', (e) => {
    queryMode = e.target.value;
    if (nodes) renderNodes(nodes);
  });
  div.querySelector('#color-palette-select').addEventListener('change', (e) => {
    colorPalette = e.target.value;
    if (nodes) renderNodes(nodes);
  });
  div.querySelector('#repeater-filter-select').addEventListener('change', (e) => {
    repeaterRenderMode = e.target.value;
    updateAllRepeaterMarkers();
  });
  div.querySelector('#repeater-search').addEventListener('input', (e) => {
    repeaterSearch = e.target.value.toLowerCase();
    updateAllRepeaterMarkers();
  });
  const _showSamplesEl = div.querySelector('#show-samples');
  if (_showSamplesEl) _showSamplesEl.addEventListener('change', async (e) => {
    showSamples = e.target.checked;
    if (showSamples) await loadIndividualSamples();
    else { clearIndividualSamples(); if (nodes) renderNodes(nodes); }
  });
  div.querySelector('#show-live-tracks').addEventListener('change', async (e) => {
    showLiveTracks = e.target.checked;
    if (showLiveTracks) await startLiveTracking();
    else stopLiveTracking();
  });
  // MESH-TERRITORY: territory mode toggle
  div.querySelector('#territory-mode').addEventListener('change', (e) => {
    territoryMode = e.target.checked;
    if (nodes) renderNodes(nodes);
  });
  div.querySelector('#refresh-map-button').addEventListener('click', () => refreshCoverage());

  L.DomEvent.disableClickPropagation(div);
  L.DomEvent.disableScrollPropagation(div);
  return div;
};

// ===== Top Feeders control =====
const repeatersControl = L.control({ position: 'topright' });
repeatersControl.onAdd = m => {
  const div = L.DomUtil.create('div', 'leaflet-control feeders-panel');
  div.innerHTML = `
    <button id="repeaters-button">Top MQTT Feeders</button>
    <div id="repeaters-list">
      <div class="repeaters-list-header">Feeders by Coverage</div>
      <div id="repeaters-list-content"></div>
    </div>
  `;
  const button = div.querySelector('#repeaters-button');
  const list = div.querySelector('#repeaters-list');
  const content = div.querySelector('#repeaters-list-content');

  button.addEventListener('click', (e) => {
    e.stopPropagation();
    if (list.style.display === 'none' || !list.style.display) {
      updateRepeatersList(content);
      list.style.display = 'block';
    } else {
      list.style.display = 'none';
    }
  });

  m.on('click', () => { list.style.display = 'none'; });
  list.addEventListener('click', e => e.stopPropagation());

  L.DomEvent.disableClickPropagation(div);
  L.DomEvent.disableScrollPropagation(div);
  return div;
};

// MESH-TERRITORY: Scoreboard control
const scoreboardControl = L.control({ position: 'topright' });
scoreboardControl.onAdd = function () {
  const div = L.DomUtil.create('div', 'leaflet-control meshwars-scoreboard');
  div.innerHTML = `
    <div class="mt-row mt-title">Territory</div>
    <div class="mt-row"><span class="mt-dot mt-red"></span> Red: <span id="mt-red-count">0</span></div>
    <div class="mt-row"><span class="mt-dot mt-blue"></span> Blue: <span id="mt-blue-count">0</span></div>
    <div class="mt-row"><span class="mt-dot mt-green"></span> Neutral: <span id="mt-green-count">0</span></div>
    <div class="mt-row mt-countdown">Ends in <span id="mt-countdown">--</span></div>
    <div class="mt-row mt-lookup-row">
      <input type="text" id="mt-lookup-input" placeholder="!abcd1234 or short name" />
      <button type="button" id="mt-lookup-btn">Find</button>
    </div>
    <div id="mt-lookup-result" class="mt-lookup-result"></div>
    <div class="mt-row"><a href="#" id="mt-history-link">History</a> &nbsp;|&nbsp; <a href="#" id="mt-roster-link">Roster</a></div>
    <div class="mt-row mt-actions">
      <button type="button" id="mt-refresh-btn">Refresh map</button>
    </div>
    <div class="mt-row mt-actions">
      <button type="button" id="mt-feeders-btn">Top MQTT Feeders</button>
    </div>
    <div id="mt-feeders-list" class="mt-feeders-list">
      <div class="repeaters-list-header">Feeders by Coverage</div>
      <div id="mt-feeders-list-content"></div>
    </div>
  `;
  L.DomEvent.disableClickPropagation(div);
  L.DomEvent.disableScrollPropagation(div);
  div.querySelector('#mt-history-link').addEventListener('click', (e) => {
    e.preventDefault();
    openHistoryModal();
  });
  div.querySelector('#mt-roster-link').addEventListener('click', (e) => {
    e.preventDefault();
    openRosterModal();
  });
  const lookupInput = div.querySelector('#mt-lookup-input');
  const lookupBtn = div.querySelector('#mt-lookup-btn');
  const doLookup = () => doTeamLookup(lookupInput.value);
  lookupBtn.addEventListener('click', doLookup);
  lookupInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') doLookup(); });
  // Stop map from stealing keystrokes while typing in the input
  L.DomEvent.on(lookupInput, 'keydown keypress keyup mousedown mouseup click dblclick',
                L.DomEvent.stopPropagation);

  // Refresh map button (moved from the old settings panel)
  div.querySelector('#mt-refresh-btn').addEventListener('click', (e) => {
    e.stopPropagation();
    refreshCoverage().catch(err => console.warn('refresh failed:', err));
  });

  // Top MQTT Feeders toggle (moved from the old repeatersControl)
  const feedersBtn = div.querySelector('#mt-feeders-btn');
  const feedersList = div.querySelector('#mt-feeders-list');
  const feedersContent = div.querySelector('#mt-feeders-list-content');
  feedersBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (feedersList.style.display === 'block') {
      feedersList.style.display = 'none';
    } else {
      updateRepeatersList(feedersContent);
      feedersList.style.display = 'block';
    }
  });

  return div;
};

// ===== Initialization =====
async function initMap() {
  await loadConfig();
  // Stash meshview URL for popup links
  try {
    const cfgResp = await fetch('/config');
    if (cfgResp.ok) {
      const cfg = await cfgResp.json();
      window.MT_MESHVIEW_URL = cfg.meshview_url || '';
    }
  } catch (e) { /* ignore */ }

  let savedView = null;
  try {
    const raw = localStorage.getItem('mapView');
    if (raw) savedView = JSON.parse(raw);
  } catch (e) { /* ignore */ }

  const startCenter = savedView ? [savedView.lat, savedView.lng] : centerPos;
  const startZoom = savedView ? savedView.zoom : initialZoom;

  map = L.map('map', {
    worldCopyJump: true,
    preferCanvas: true,
  }).setView(startCenter, startZoom);

  map.on('moveend', () => {
    const c = map.getCenter();
    localStorage.setItem('mapView', JSON.stringify({ lat: c.lat, lng: c.lng, zoom: map.getZoom() }));
  });

  window._svgRenderer = L.svg();

  osm = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/">CARTO</a>'
  }).addTo(map);

  coverageLayer = L.layerGroup().addTo(map);
  edgeLayer = L.layerGroup().addTo(map);
  sampleLayer = L.layerGroup().addTo(map);
  repeaterLayer = L.layerGroup();  // not added to map (territory-only view)
  liveTrackLayer = L.layerGroup().addTo(map);

  initSVGPatterns();

  scoreboardControl.addTo(map);  // unified panel (settings card removed)

  // (old feeders-panel removal no longer needed; unified into scoreboard)

  if (maxDistanceMiles > 0) {
    L.circle(centerPos, {
      radius: maxDistanceMiles * 1609.34,
      color: '#555', weight: 1, fill: false, dashArray: '6, 8', opacity: 0.3
    }).addTo(map);
  }

  const _ss = document.getElementById('show-samples'); if (_ss) _ss.checked = showSamples;
  const _slt = document.getElementById('show-live-tracks'); if (_slt) _slt.checked = showLiveTracks;
  const _tm = document.getElementById('territory-mode'); if (_tm) _tm.checked = territoryMode;

  map.on('zoomend', () => {
    if (showSamples && individualSamples) {
      sampleLayer.clearLayers();
      const direct = [];
      const indirect = [];
      individualSamples.keys.forEach(s => {
        const heard = s.metadata.path && s.metadata.path.length > 0;
        const isRelayed = s.metadata.observed === false;
        if (!isRelayed && heard) direct.push(s);
        else indirect.push(s);
      });
      indirect.forEach(s => sampleLayer.addLayer(individualSampleMarker(s)));
      direct.forEach(s => sampleLayer.addLayer(individualSampleMarker(s)));
    }
  });

  await refreshCoverage();

  const loadingOverlay = document.getElementById('loading-overlay');
  if (loadingOverlay) {
    loadingOverlay.classList.add('fade-out');
    setTimeout(() => loadingOverlay.remove(), 500);
  }

  if (showSamples) await loadIndividualSamples();
  if (showLiveTracks) await startLiveTracking();

  // MESH-TERRITORY: kick off scoreboard & banner refresh
  refreshScoreboard();
  refreshWinnerBanner();
  setInterval(refreshScoreboard, 30000);
  setInterval(refreshWinnerBanner, 60000);
  setInterval(tickCountdown, 1000);

  // Live tile updates - refresh every 30s
  setInterval(() => {
    refreshCoverage().catch(err => console.warn('auto-refresh failed:', err));
  }, 30000);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    initMap().catch(err => {
      console.error('Failed to initialize map:', err);
      const mapDiv = document.getElementById('map');
      if (mapDiv) mapDiv.innerHTML = `<div style="padding: 20px; color: red;">Failed to load map: ${err.message}</div>`;
    });
  });
} else {
  initMap().catch(err => {
    console.error('Failed to initialize map:', err);
    const mapDiv = document.getElementById('map');
    if (mapDiv) mapDiv.innerHTML = `<div style="padding: 20px; color: red;">Failed to load map: ${err.message}</div>`;
  });
}

// ===== SVG patterns (preserved from original) =====
function initSVGPatterns() {
  map.whenReady(() => {
    setTimeout(() => {
      const mapContainer = map.getContainer();
      let svg = mapContainer.querySelector('svg.leaflet-zoom-animated');
      if (!svg) { setTimeout(initSVGPatterns, 200); return; }
      let svgDefs = svg.querySelector('defs');
      if (!svgDefs) {
        svgDefs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
        svg.insertBefore(svgDefs, svg.firstChild);
      }
      if (svgDefs.querySelector('#pattern-sparse-lines')) return;

      const defs = [
        { id: 'pattern-sparse-lines',     w: 20, h: 20, lines: [[0,10,20,10]] },
        { id: 'pattern-medium-lines',     w: 12, h: 12, lines: [[0,4,12,4],[0,8,12,8]] },
        { id: 'pattern-dense-lines',      w: 8,  h: 8,  lines: [[0,2,8,2],[0,6,8,6]] },
        { id: 'pattern-very-dense-lines', w: 4,  h: 4,  lines: [[0,1,4,1],[0,3,4,3]] },
        { id: 'pattern-solid',            w: 2,  h: 2,  lines: [[0,1,2,1]] },
      ];
      defs.forEach(p => {
        const pat = document.createElementNS('http://www.w3.org/2000/svg', 'pattern');
        pat.setAttribute('id', p.id);
        pat.setAttribute('patternUnits', 'userSpaceOnUse');
        pat.setAttribute('width', p.w);
        pat.setAttribute('height', p.h);
        p.lines.forEach(([x1,y1,x2,y2]) => {
          const l = document.createElementNS('http://www.w3.org/2000/svg', 'line');
          l.setAttribute('x1', x1); l.setAttribute('y1', y1);
          l.setAttribute('x2', x2); l.setAttribute('y2', y2);
          l.setAttribute('stroke', '#000'); l.setAttribute('stroke-width', '1');
          pat.appendChild(l);
        });
        svgDefs.appendChild(pat);
      });
    }, 100);
  });
}

function escapeHtml(s) {
  return String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
}

function toHex(n) { const h = n.toString(16); return h.length === 1 ? '0' + h : h; }
function rgbToHex(r, g, b) { return `#${toHex(r)}${toHex(g)}${toHex(b)}`; }

function paletteRedYellowGreen(rate) {
  const r = Math.max(0, Math.min(1, rate));
  if (r === 0) return '#FF0000';
  if (r <= 0.25) return '#FF0000';
  if (r <= 0.40) return '#FFA500';
  if (r <= 0.70) return '#FFFF00';
  if (r <= 0.85) return '#90EE90';
  return '#006400';
}

function paletteBlue(rate) {
  const r = Math.max(0, Math.min(1, rate));
  if (r === 0) return '#E6F3FF';
  if (r <= 0.25) return '#B3D9FF';
  if (r <= 0.40) return '#87CEEB';
  if (r <= 0.70) return '#4169E1';
  if (r <= 0.85) return '#0000CD';
  return '#000033';
}

function palettePatterns(rate) {
  const r = Math.max(0, Math.min(1, rate));
  let patternId;
  if (r === 0) patternId = 'pattern-sparse-lines';
  else if (r <= 0.25) patternId = 'pattern-medium-lines';
  else if (r <= 0.40) patternId = 'pattern-dense-lines';
  else if (r <= 0.70) patternId = 'pattern-very-dense-lines';
  else patternId = 'pattern-solid';
  return { fillColor: '#E0E0E0', fillPattern: patternId, patternUrl: `url(#${patternId})` };
}

function paletteSimpleGreen(rate) {
  const r = Math.max(0, Math.min(1, rate));
  if (r < 0.25) return { fillColor: '#F5F5F5', borderColor: '#808080', fillOpacity: 0, hasBorder: true };
  if (r < 0.50) return { fillColor: '#90EE90', borderColor: '#90EE90', fillOpacity: 1, hasBorder: false };
  return { fillColor: '#006400', borderColor: '#006400', fillOpacity: 1, hasBorder: false };
}

function paletteCyberGreen(rate) {
  const r = Math.max(0, Math.min(1, rate));
  if (r === 0) return '#5c1a1a';
  if (r <= 0.25) return '#8b3a3a';
  if (r <= 0.40) return '#b8860b';
  if (r <= 0.70) return '#2d8a2d';
  if (r <= 0.85) return '#3aaa3a';
  return '#50c050';
}

function successRateToColor(rate) {
  const r = Math.max(0, Math.min(1, rate));
  if (colorPalette === 'cyber-green') return paletteCyberGreen(r);
  if (colorPalette === 'red-yellow-green') return paletteRedYellowGreen(r);
  if (colorPalette === 'blue') return paletteBlue(r);
  if (colorPalette === 'patterns') return palettePatterns(r).fillColor;
  if (colorPalette === 'simple-green') {
    const s = paletteSimpleGreen(r);
    return s.hasBorder ? '#808080' : s.fillColor;
  }
  return paletteRedYellowGreen(r);
}

function getQueryValue(coverage) {
  function recencyScore(ageDays) {
    const points = [
      { d: 0, v: 1.0 }, { d: 1, v: 1.0 }, { d: 2, v: 0.75 },
      { d: 3, v: 0.50 }, { d: 5, v: 0.25 }, { d: 7, v: 0.0 }, { d: 30, v: 0.0 },
    ];
    const a = Math.max(0, ageDays);
    for (let i = 1; i < points.length; i++) {
      const prev = points[i-1], curr = points[i];
      if (a <= curr.d) {
        const t = (a - prev.d) / (curr.d - prev.d);
        return prev.v + (curr.v - prev.v) * t;
      }
    }
    return 0.0;
  }

  switch (queryMode) {
    case 'coverage': {
      const total = coverage.rcv + coverage.lost;
      return total > 0 ? coverage.rcv / total : 0;
    }
    case 'last-updated': {
      const t = coverage.time || coverage.ut || coverage.lot || coverage.lht || 0;
      if (t === 0) return 0.0;
      const ageMs = Date.now() - fromTruncatedTime(t);
      return recencyScore(ageMs / (1000 * 86400));
    }
    case 'past-day': {
      const t = coverage.time || coverage.ut || coverage.lot || coverage.lht || 0;
      if (t === 0) return 0;
      const ageMs = Date.now() - fromTruncatedTime(t);
      const ageDays = ageMs / (1000 * 86400);
      if (ageDays > 1) return 0;
      return recencyScore(ageDays);
    }
    case 'repeater-count': {
      const c = (coverage.rptr && coverage.rptr.length) || 0;
      if (c === 0) return 0;
      if (c === 1) return 0.5;
      if (c === 2) return 0.75;
      return 1.0;
    }
    default: return 0;
  }
}

function successRateToStyle(rate) {
  const r = Math.max(0, Math.min(1, rate));
  if (colorPalette === 'patterns') {
    const p = palettePatterns(r);
    return { fillColor: p.fillColor, fillPattern: p.fillPattern, patternUrl: p.patternUrl };
  }
  if (colorPalette === 'simple-green') {
    const s = paletteSimpleGreen(r);
    return { fillColor: s.fillColor, borderColor: s.borderColor, fillOpacity: s.fillOpacity, hasBorder: s.hasBorder };
  }
  return { fillColor: successRateToColor(r) };
}

function coverageMarker(coverage) {
  const [minLat, minLon, maxLat, maxLon] = geo.decode_bbox(coverage.id);
  let queryValue = getQueryValue(coverage);
  if (queryMode === 'past-day' && queryValue === 0) return null;

  const totalSamples = coverage.rcv + coverage.lost;
  const heardRatio = totalSamples > 0 ? coverage.rcv / totalSamples : 0;
  const isOnlyRelayed = (coverage.obs === 0) && (totalSamples > 0);

  let styleInfo, color;
  if (isOnlyRelayed) {
    color = '#404040';
    styleInfo = { fillColor: color };
  } else {
    styleInfo = successRateToStyle(queryValue);
    color = successRateToColor(queryValue);
  }

  // MESH-TERRITORY: team color override - this is the entire game color decision
  if (territoryMode && coverage.owner_team && TEAM_COLORS[coverage.owner_team]) {
    color = TEAM_COLORS[coverage.owner_team];
    styleInfo = { fillColor: color };
  }

  const date = new Date(fromTruncatedTime(coverage.time || 0));
  const baseOpacity = 0.75 * sigmoid(totalSamples, 1.2, 2);
  const opacityValue = (queryMode === 'past-day' || queryMode === 'last-updated')
    ? queryValue
    : (heardRatio > 0 ? baseOpacity * heardRatio : Math.max(baseOpacity, 0.4));

  // MESH-TERRITORY: when territory mode is on, force opaque so team colors read cleanly
  let fillOpacity = styleInfo.fillOpacity !== undefined ? styleInfo.fillOpacity : Math.max(opacityValue, 0.2);
  if (territoryMode && coverage.owner_team) fillOpacity = 0.7;

  const style = {
    color: styleInfo.borderColor || color,
    weight: styleInfo.hasBorder ? 2 : 1,
    fillColor: styleInfo.fillColor || color,
    fillOpacity: fillOpacity,
  };

  if (colorPalette === 'patterns' && !territoryMode) style.renderer = window._svgRenderer;

  const rect = L.rectangle([[minLat, minLon], [maxLat, maxLon]], style);

  if (colorPalette === 'patterns' && !territoryMode && styleInfo.patternUrl) {
    rect.on('add', function() {
      setTimeout(() => {
        const path = rect.getElement();
        if (path) {
          path.setAttribute('fill', styleInfo.patternUrl);
          path.setAttribute('fill-opacity', style.fillOpacity || 0.6);
        }
      }, 10);
    });
  }

  const centerLat = ((minLat + maxLat) / 2).toFixed(4);
  const centerLon = ((minLon + maxLon) / 2).toFixed(4);

  // Lightweight placeholder shown immediately; rich detail loaded on popup open.
  const placeholderHtml = renderTilePopupSkeleton(coverage, centerLat, centerLon);

  rect.coverage = coverage;
  rect.bindPopup(placeholderHtml, { maxWidth: 360, className: 'mt-tile-popup' });
  rect.on('popupopen', async (e) => {
    updateAllEdgeVisibility(e.target.coverage);
    // Lazy-load rich detail
    try {
      const r = await fetch(`/tile/${encodeURIComponent(coverage.id)}`);
      if (!r.ok) return;
      const detail = await r.json();
      if (!detail.found) return;
      const html = renderTilePopupRich(coverage, detail, centerLat, centerLon);
      e.popup.setContent(html);
    } catch (err) { console.warn('tile detail load failed:', err); }
  });
  rect.on('popupclose', () => updateAllEdgeVisibility());

  if (window.matchMedia('(hover: hover)').matches) {
    rect.on('mouseover', e => updateAllEdgeVisibility(e.target.coverage));
    rect.on('mouseout', () => updateAllEdgeVisibility());
  }

  coverage.marker = rect;
  return rect;
}

function sampleRadiusForZoom(base, zoom) {
  if (zoom >= 13) return base;
  if (zoom >= 11) return Math.max(1, base - 1);
  return Math.max(1, base - 2);
}

function individualSampleMarker(sample) {
  const [lat, lon] = posFromHash(sample.name);
  const isRelayed = sample.metadata.observed === false;
  const heard = sample.metadata.path && sample.metadata.path.length > 0;
  const zoom = map.getZoom();

  // MESH-TERRITORY: sample dot color by sender's team if present
  let color, statusText, style;
  const team = sample.metadata.owner_team;
  if (territoryMode && team && TEAM_COLORS[team]) {
    color = TEAM_COLORS[team];
    statusText = `<span style="color: ${color};">${team}</span>`;
    style = { radius: sampleRadiusForZoom(4, zoom), weight: zoom >= 13 ? 1 : 0.5, color: color, fillColor: color, fillOpacity: 0.9 };
  } else if (isRelayed) {
    color = '#444';
    statusText = '<span style="color: grey;">Relayed</span>';
    style = { radius: sampleRadiusForZoom(3, zoom), weight: zoom >= 13 ? 1 : 0.5, color: color, fillColor: 'transparent', fillOpacity: 0 };
  } else if (heard) {
    color = '#50c050';
    statusText = '<span style="color: green;">Direct</span>';
    style = { radius: sampleRadiusForZoom(4, zoom), weight: zoom >= 13 ? 1 : 0.5, color: '#70e070', fillColor: color, fillOpacity: 0.9 };
  } else {
    color = '#444';
    statusText = '<span style="color: grey;">Indirect</span>';
    style = { radius: sampleRadiusForZoom(3, zoom), weight: zoom >= 13 ? 1 : 0.5, color: color, fillColor: 'transparent', fillOpacity: 0 };
  }

  const marker = L.circleMarker([lat, lon], style);
  const timeValue = sample.metadata.time;
  const date = timeValue ? new Date(typeof timeValue === 'string' ? parseInt(timeValue, 10) : timeValue) : null;
  const repeaters = sample.metadata.path || [];
  let details = `<strong>${sample.name}</strong><br/>${lat.toFixed(4)}, ${lon.toFixed(4)}<br/>Status: ${statusText}<br/>`;
  if (repeaters.length > 0) details += `<br/>Feeders: ${repeaters.join(', ')}`;
  if (sample.metadata.sender) details += `<br/>Sender: ${sample.metadata.sender}`;
  if (sample.metadata.snr !== null && sample.metadata.snr !== undefined) details += `<br/>SNR: ${sample.metadata.snr} dB`;
  if (sample.metadata.rssi !== null && sample.metadata.rssi !== undefined) details += `<br/>RSSI: ${sample.metadata.rssi} dBm`;
  if (date && !isNaN(date.getTime())) details += `<br/>Time: ${date.toLocaleString()}`;
  marker.bindPopup(details, { maxWidth: 320 });
  return marker;
}

function repeaterMarker(r) {
  const time = fromTruncatedTime(r.time);
  const stale = ageInDays(time) > 2;
  const dead = ageInDays(time) > 8;
  const ageClass = (dead ? 'dead' : (stale ? 'stale' : ''));
  // MESH-TERRITORY: team class on repeater dot
  const teamClass = r.team && r.team !== 'GREEN' ? `team-${r.team.toLowerCase()}` : '';
  const icon = L.divIcon({
    className: '',
    html: `<div class="repeater-dot ${ageClass} ${teamClass}"><span>${escapeHtml(r.name || r.id)}</span></div>`,
    iconSize: null,
    iconAnchor: [10, 10]
  });
  const details = [
    `<strong>${escapeHtml(r.name)} [${r.id}]</strong>`,
    `${r.lat.toFixed(4)}, ${r.lon.toFixed(4)} · <em>${(r.elev).toFixed(0)}m</em>`,
    r.team ? `Team: ${r.team}` : '',
    `${new Date(time).toLocaleString()}`
  ].filter(Boolean).join('<br/>');
  const marker = L.marker([r.lat, r.lon], { icon: icon });
  marker.repeater = r;
  marker.bindPopup(details, { maxWidth: 320 });
  marker.on('add', () => updateRepeaterMarkerVisibility(marker));
  marker.on('popupopen', e => updateAllEdgeVisibility(e.target.repeater));
  marker.on('popupclose', () => updateAllEdgeVisibility());
  if (window.matchMedia('(hover: hover)').matches) {
    marker.on('mouseover', e => updateAllEdgeVisibility(e.target.repeater));
    marker.on('mouseout', () => updateAllEdgeVisibility());
  }
  r.marker = marker;
  return marker;
}

function getBestRepeater(fromPos, repeaterList) {
  if (repeaterList.length === 1) return repeaterList[0];
  let minRepeater = null;
  let minDist = 30000;
  repeaterList.forEach(r => {
    const to = [r.lat, r.lon];
    const elev = r.elev ?? 0;
    const dist = haversineMiles(fromPos, to) - (0.5 * Math.sqrt(elev));
    if (dist < minDist) { minDist = dist; minRepeater = r; }
  });
  return minRepeater;
}

function shouldShowRepeater(r) {
  if (repeaterSearch !== '') {
    const nameMatch = (r.name || '').toLowerCase().includes(repeaterSearch);
    const idMatch = r.id.toLowerCase().includes(repeaterSearch);
    return nameMatch || idMatch;
  } else if (repeaterRenderMode === 'hit') {
    return r.hitBy.length > 0;
  } else if (repeaterRenderMode === 'none') {
    return false;
  }
  return true;
}

function updateRepeaterMarkerVisibility(m, forceVisible = false, highlight = false) {
  const el = m.getElement?.();
  if (!el) return;
  if (forceVisible || shouldShowRepeater(m.repeater)) {
    el.classList.remove('hidden');
    el.classList.add('leaflet-interactive');
  } else {
    el.classList.add('hidden');
    el.classList.remove('leaflet-interactive');
  }
  if (highlight) el.querySelector('.repeater-dot').classList.add('highlighted');
  else el.querySelector('.repeater-dot').classList.remove('highlighted');
}

function updateAllRepeaterMarkers() {
  repeaterLayer.eachLayer(m => updateRepeaterMarkerVisibility(m));
}

function updateCoverageMarkerHighlight(m, highlight = false) {
  const el = m.getElement?.();
  if (!el) return;
  if (highlight) el.classList.add('highlighted-path');
  else el.classList.remove('highlighted-path');
}

function updateAllCoverageMarkers() {
  coverageLayer.eachLayer(m => updateCoverageMarkerHighlight(m));
}

function updateAllEdgeVisibility(end) {
  const markersToOverride = [];
  const coverageToHighlight = [];
  updateAllRepeaterMarkers();
  updateAllCoverageMarkers();
  edgeLayer.eachLayer(e => {
    if (end !== undefined && e.ends.includes(end)) {
      markersToOverride.push(e.ends[0].marker);
      coverageToHighlight.push(e.ends[1].marker);
      e.setStyle({ opacity: 0.9, color: '#e8c860' });
    } else {
      e.setStyle({ opacity: 0 });
    }
  });
  markersToOverride.forEach(m => updateRepeaterMarkerVisibility(m, true, true));
  coverageToHighlight.forEach(m => updateCoverageMarkerHighlight(m, true));
}

function renderNodes(nodes) {
  coverageLayer.clearLayers();
  edgeLayer.clearLayers();
  sampleLayer.clearLayers();
  repeaterLayer.clearLayers();

  hashToCoverage.entries().forEach(([key, coverage]) => {
    const marker = coverageMarker(coverage);
    if (marker) coverageLayer.addLayer(marker);
  });

  if (showSamples && individualSamples) {
    const direct = [];
    const indirect = [];
    individualSamples.keys.forEach(s => {
      const heard = s.metadata.path && s.metadata.path.length > 0;
      const isRelayed = s.metadata.observed === false;
      if (!isRelayed && heard) direct.push(s);
      else indirect.push(s);
    });
    indirect.forEach(s => sampleLayer.addLayer(individualSampleMarker(s)));
    direct.forEach(s => sampleLayer.addLayer(individualSampleMarker(s)));
  }

  [...idToRepeaters.values()].flat().forEach(r => {
    repeaterLayer.addLayer(repeaterMarker(r));
  });

  // Edges (dashed lines from tiles to feeders) disabled — territory view
}

function buildIndexes(nodes) {
  hashToCoverage = new Map();
  idToRepeaters = new Map();
  edgeList = [];

  nodes.coverage.forEach(c => {
    const { latitude: lat, longitude: lon } = geo.decode(c.id);
    c.pos = [lat, lon];
    if (c.rptr === undefined) c.rptr = [];
    if (!c.time && c.ut) c.time = c.ut;
    else if (!c.time && c.lot) c.time = c.lot;
    else if (!c.time && c.lht) c.time = c.lht;
    if (c.rcv === undefined && c.heard !== undefined) c.rcv = c.heard;
    if (c.obs === undefined) c.obs = c.observed ?? 0;
    if (c.hrd === undefined) c.hrd = c.rcv ?? 0;
    hashToCoverage.set(c.id, c);
  });

  nodes.samples.forEach(s => {
    const key = s.id;
    let coverage = hashToCoverage.get(key);
    const sampleHeard = s.heard || 0;
    const sampleLost = s.lost || 0;
    if (!coverage) {
      const { latitude: lat, longitude: lon } = geo.decode(key);
      coverage = {
        id: key, pos: [lat, lon],
        rcv: sampleHeard, lost: sampleLost,
        time: s.time || 0,
        rptr: (s.path || s.rptr) ? [...(s.path || s.rptr)] : [],
        snr: (s.snr !== null && s.snr !== undefined) ? s.snr : undefined,
        rssi: (s.rssi !== null && s.rssi !== undefined) ? s.rssi : undefined,
        obs: (s.obs !== undefined) ? (s.obs ? 1 : 0) : 0,
        owner_team: s.owner_team,   // MESH-TERRITORY: propagate team from samples too
      };
      hashToCoverage.set(key, coverage);
    } else {
      coverage.rcv = sampleHeard;
      coverage.lost = sampleLost;
      if (s.obs !== undefined) coverage.obs = s.obs ? 1 : 0;
      if (s.time > (coverage.time || 0)) coverage.time = s.time;
      const samplePath = s.path || s.rptr;
      if (samplePath) {
        samplePath.forEach(r => {
          const rLower = r.toLowerCase();
          if (!coverage.rptr.includes(rLower)) coverage.rptr.push(rLower);
        });
      }
      if (s.snr !== null && s.snr !== undefined)
        coverage.snr = (coverage.snr === null || coverage.snr === undefined) ? s.snr : Math.max(coverage.snr, s.snr);
      if (s.rssi !== null && s.rssi !== undefined)
        coverage.rssi = (coverage.rssi === null || coverage.rssi === undefined) ? s.rssi : Math.max(coverage.rssi, s.rssi);
    }
  });

  nodes.repeaters.forEach(r => {
    r.hitBy = [];
    r.pos = [r.lat, r.lon];
    pushMap(idToRepeaters, r.id, r);
  });

  hashToCoverage.entries().forEach(([key, coverage]) => {
    coverage.rptr.forEach(r => {
      const candidateRepeaters = idToRepeaters.get(r);
      if (candidateRepeaters === undefined) return;
      const bestRepeater = getBestRepeater(coverage.pos, candidateRepeaters);
      bestRepeater.hitBy.push(coverage);
      edgeList.push({ repeater: bestRepeater, coverage: coverage });
    });
  });
}

function updateRepeatersList(contentDiv) {
  if (!nodes || !idToRepeaters) {
    contentDiv.innerHTML = '<div class="repeaters-list-empty">No feeder data available.<br/>Please refresh the map first.</div>';
    return;
  }
  const repeaterGeohashCount = new Map();
  let coverageWithRepeaters = 0;
  hashToCoverage.forEach((coverage) => {
    if (coverage.rptr && coverage.rptr.length > 0) {
      coverageWithRepeaters++;
      coverage.rptr.forEach(repeaterId => {
        const idLower = repeaterId.toLowerCase();
        repeaterGeohashCount.set(idLower, (repeaterGeohashCount.get(idLower) || 0) + 1);
      });
    }
  });
  const repeaterStats = [];
  idToRepeaters.forEach((repeaters, id) => {
    const count = repeaterGeohashCount.get(id.toLowerCase()) || 0;
    if (count > 0) repeaterStats.push({ id, name: repeaters[0]?.name || id, geohashCount: count });
  });
  repeaterStats.sort((a, b) => b.geohashCount - a.geohashCount);

  if (repeaterStats.length === 0) {
    contentDiv.innerHTML = `<div class="repeaters-list-empty">
      No feeders with coverage data found yet.<br/><br/>
      <div class="repeaters-list-stats">
        Total feeders: ${idToRepeaters.size}<br/>
        Total coverage areas: ${hashToCoverage.size}<br/>
        Coverage with feeders: ${coverageWithRepeaters}
      </div>
    </div>`;
    return;
  }

  let html = '<div class="repeaters-list-body">';
  repeaterStats.forEach((r) => {
    html += `<div class="repeaters-list-item">
      <span>${escapeHtml(r.name)}</span>
      <span class="repeaters-list-count">${r.geohashCount}</span>
    </div>`;
  });
  html += '</div>';
  contentDiv.innerHTML = html;
}

async function loadIndividualSamples() {
  try {
    const resp = await fetch('/get-samples', { headers: { 'Accept': 'application/json' } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
    individualSamples = await resp.json();
    sampleLayer.clearLayers();
    const direct = [], indirect = [];
    individualSamples.keys.forEach(s => {
      const heard = s.metadata.path && s.metadata.path.length > 0;
      const isRelayed = s.metadata.observed === false;
      if (!isRelayed && heard) direct.push(s);
      else indirect.push(s);
    });
    indirect.forEach(s => sampleLayer.addLayer(individualSampleMarker(s)));
    direct.forEach(s => sampleLayer.addLayer(individualSampleMarker(s)));
  } catch (error) {
    console.error('Error loading individual samples:', error);
  }
}

function clearIndividualSamples() { individualSamples = null; }

export async function refreshCoverage() {
  const resp = await fetch('/get-nodes', { headers: { 'Accept': 'application/json' } });
  if (!resp.ok) throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
  nodes = await resp.json();
  buildIndexes(nodes);
  renderNodes(nodes);
}

// ===== Live wardriving (preserved from original) =====
const THREE_HOURS_MS = 3 * 60 * 60 * 1000;

function trackColorForNode(nodeId) {
  let hash = 0;
  for (let i = 0; i < nodeId.length; i++) hash = ((hash << 5) - hash + nodeId.charCodeAt(i)) | 0;
  return TRACK_COLORS[Math.abs(hash) % TRACK_COLORS.length];
}

let sseReconnectTimer = null;
let sseReconnectDelay = 1000;
const SSE_MAX_DELAY = 60000;
let sseErrorCount = 0;

function connectSSE() {
  if (liveTrackSSE) { liveTrackSSE.close(); liveTrackSSE = null; }
  liveTrackSSE = new EventSource('/live-tracks/stream');
  liveTrackSSE.onopen = () => { sseReconnectDelay = 1000; sseErrorCount = 0; };
  liveTrackSSE.onmessage = (event) => {
    try {
      const point = JSON.parse(event.data);
      addTrackPoint(point, true);
    } catch (err) { /* ignore */ }
  };
  liveTrackSSE.onerror = () => {
    sseErrorCount++;
    if (liveTrackSSE) { liveTrackSSE.close(); liveTrackSSE = null; }
    if (showLiveTracks) {
      sseReconnectTimer = setTimeout(() => {
        sseReconnectTimer = null;
        if (showLiveTracks) connectSSE();
      }, sseReconnectDelay);
      sseReconnectDelay = Math.min(sseReconnectDelay * 2, SSE_MAX_DELAY);
    }
  };
}

async function startLiveTracking() {
  try {
    const resp = await fetch('/live-tracks', { headers: { 'Accept': 'application/json' } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    liveTrackData = {};
    (data.points || []).forEach(p => addTrackPoint(p, false));
    redrawAllTracks();
    connectSSE();
    liveTrackPruneTimer = setInterval(() => pruneOldTrackPoints(), 60000);
  } catch (err) { console.error('Error starting live tracking:', err); }
}

function stopLiveTracking() {
  if (liveTrackSSE) { liveTrackSSE.close(); liveTrackSSE = null; }
  if (sseReconnectTimer) { clearTimeout(sseReconnectTimer); sseReconnectTimer = null; }
  sseReconnectDelay = 1000; sseErrorCount = 0;
  if (liveTrackPruneTimer) { clearInterval(liveTrackPruneTimer); liveTrackPruneTimer = null; }
  liveTrackLayer.clearLayers();
  liveTrackData = {};
}

function addTrackPoint(point, render) {
  const nodeId = point.node_id;
  if (!nodeId) return;
  if (!liveTrackData[nodeId]) {
    liveTrackData[nodeId] = {
      name: point.node_name || nodeId,
      color: trackColorForNode(nodeId),
      points: [], polyline: null, headMarker: null, waypointMarkers: [],
    };
  }
  const track = liveTrackData[nodeId];
  if (point.node_name) track.name = point.node_name;
  track.points.push({ lat: parseFloat(point.lat), lon: parseFloat(point.lon), time: parseInt(point.time) });
  if (render) updateTrackOnMap(nodeId);
}

function redrawAllTracks() {
  liveTrackLayer.clearLayers();
  for (const nodeId of Object.keys(liveTrackData)) {
    const t = liveTrackData[nodeId];
    t.polyline = null; t.headMarker = null; t.waypointMarkers = [];
    updateTrackOnMap(nodeId);
  }
}

function updateTrackOnMap(nodeId) {
  const track = liveTrackData[nodeId];
  if (!track || track.points.length < 2) {
    if (track && track.polyline) { liveTrackLayer.removeLayer(track.polyline); track.polyline = null; }
    if (track && track.headMarker) { liveTrackLayer.removeLayer(track.headMarker); track.headMarker = null; }
    if (track && track.waypointMarkers) { track.waypointMarkers.forEach(d => liveTrackLayer.removeLayer(d)); track.waypointMarkers = []; }
    return;
  }
  const latlngs = track.points.map(p => [p.lat, p.lon]);
  const lastPoint = track.points[track.points.length - 1];
  if (track.polyline) track.polyline.setLatLngs(latlngs);
  else {
    track.polyline = L.polyline(latlngs, { color: track.color, weight: 3, opacity: 0.8, lineJoin: 'round', lineCap: 'round' });
    liveTrackLayer.addLayer(track.polyline);
  }
  while (track.waypointMarkers.length < track.points.length - 1) {
    const idx = track.waypointMarkers.length;
    const p = track.points[idx];
    const dot = L.circleMarker([p.lat, p.lon], { radius: 4, color: track.color, weight: 0, fillColor: track.color, fillOpacity: 0.9, interactive: false });
    track.waypointMarkers.push(dot);
    liveTrackLayer.addLayer(dot);
  }
  while (track.waypointMarkers.length >= track.points.length) {
    const dot = track.waypointMarkers.pop();
    liveTrackLayer.removeLayer(dot);
  }
  if (track.headMarker) track.headMarker.setLatLng([lastPoint.lat, lastPoint.lon]);
  else {
    track.headMarker = L.circleMarker([lastPoint.lat, lastPoint.lon], { radius: 6, color: '#fff', weight: 2, fillColor: track.color, fillOpacity: 1 });
    track.headMarker.bindTooltip(`${escapeHtml(track.name)}<br/><small>${new Date(lastPoint.time).toLocaleString()}</small>`, { permanent: false, direction: 'top', className: 'track-tooltip', offset: [0, -8] });
    track.headMarker.bindPopup(`<strong>${escapeHtml(track.name)}</strong><br/>Node: ${escapeHtml(nodeId)}<br/>Points: ${track.points.length}<br/>Last seen: ${new Date(lastPoint.time).toLocaleString()}`, { maxWidth: 280 });
    liveTrackLayer.addLayer(track.headMarker);
  }
}

function pruneOldTrackPoints() {
  const cutoff = Date.now() - THREE_HOURS_MS;
  for (const nodeId of Object.keys(liveTrackData)) {
    const track = liveTrackData[nodeId];
    const before = track.points.length;
    track.points = track.points.filter(p => p.time > cutoff);
    if (track.points.length !== before) {
      if (track.points.length === 0) {
        if (track.polyline) liveTrackLayer.removeLayer(track.polyline);
        if (track.headMarker) liveTrackLayer.removeLayer(track.headMarker);
        if (track.waypointMarkers) track.waypointMarkers.forEach(d => liveTrackLayer.removeLayer(d));
        delete liveTrackData[nodeId];
      } else updateTrackOnMap(nodeId);
    }
  }
}

// ============================================================================
// MESH-TERRITORY: scoreboard, winner banner, history modal
// ============================================================================

let scoreboardEndsAt = null;

async function refreshScoreboard() {
  try {
    const r = await fetch('/scores');
    if (!r.ok) return;
    const s = await r.json();
    const setText = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    setText('mt-red-count', s.red);
    setText('mt-blue-count', s.blue);
    setText('mt-green-count', s.green);
    scoreboardEndsAt = s.ends_at;
  } catch (e) { /* ignore */ }
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

function tickCountdown() {
  const el = document.getElementById('mt-countdown');
  if (!el || !scoreboardEndsAt) return;
  const now = Math.floor(Date.now() / 1000);
  el.textContent = formatCountdown(scoreboardEndsAt - now);
}

async function refreshWinnerBanner() {
  try {
    const r = await fetch('/config');
    if (!r.ok) return;
    const cfg = await r.json();
    const banner = cfg.winner_banner;
    const el = document.getElementById('mt-winner-banner');
    if (!el) return;
    if (banner) {
      const w = banner.winner;
      const wText = w === 'TIE' ? 'TIE' : `${w} WINS`;
      el.innerHTML = `
        <span class="mt-winner-tag ${w.toLowerCase()}">${wText}</span>
        <span class="mt-winner-counts">
          🔴 ${banner.red_tiles} &nbsp; 🔵 ${banner.blue_tiles} &nbsp; 🟢 ${banner.green_tiles}
        </span>
        <span class="mt-winner-dates">Season #${banner.season_id}</span>
      `;
      el.style.display = 'block';
    } else {
      el.style.display = 'none';
    }
  } catch (e) { /* ignore */ }
}

async function openHistoryModal() {
  const modal = document.getElementById('mt-history-modal');
  const body = document.getElementById('mt-history-body');
  body.innerHTML = '<div style="padding:1em;">Loading...</div>';
  modal.style.display = 'flex';
  try {
    const r = await fetch('/history');
    const h = await r.json();
    if (!h.seasons || !h.seasons.length) {
      body.innerHTML = '<div style="padding:1em;">No completed seasons yet.</div>';
      return;
    }
    const rows = h.seasons.map(s => {
      const started = new Date(s.started_at * 1000).toLocaleDateString();
      const ended = new Date(s.ends_at * 1000).toLocaleDateString();
      const wClass = (s.winner || '').toLowerCase();
      return `<tr>
        <td>#${s.id}</td>
        <td>${started} – ${ended}</td>
        <td class="mt-winner-cell ${wClass}">${s.winner || '-'}</td>
        <td class="mt-red-cell">${s.red_tiles ?? 0}</td>
        <td class="mt-blue-cell">${s.blue_tiles ?? 0}</td>
        <td class="mt-green-cell">${s.green_tiles ?? 0}</td>
      </tr>`;
    }).join('');
    body.innerHTML = `<table class="mt-history-table">
      <thead><tr><th>Season</th><th>Dates</th><th>Winner</th><th>🔴</th><th>🔵</th><th>🟢</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  } catch (e) {
    body.innerHTML = `<div style="padding:1em;color:#c66;">Failed to load: ${e.message}</div>`;
  }
}

function closeHistoryModal() {
  const m = document.getElementById('mt-history-modal');
  if (m) m.style.display = 'none';
}

// Expose for inline onclick handlers
window.closeHistoryModal = closeHistoryModal;



async function doTeamLookup(value) {
  const resultEl = document.getElementById('mt-lookup-result');
  if (!resultEl) return;
  const ref = (value || '').trim();
  if (!ref) { resultEl.innerHTML = ''; return; }
  resultEl.innerHTML = '<span style="color:#888;">Searching...</span>';
  try {
    const r = await fetch(`/team/${encodeURIComponent(ref)}`);
    const data = await r.json();
    if (!data.found) {
      resultEl.innerHTML = `<span style="color:#c66;">Not found: ${escapeHtml(ref)}</span>`;
      return;
    }
    const teamColor = TEAM_COLORS[data.team] || '#888';
    const name = data.name || data.short_name || data.node_hex;
    resultEl.innerHTML = `
      <div class="mt-lookup-card" style="border-left:3px solid ${teamColor};">
        <div class="mt-lookup-name">${escapeHtml(name)}</div>
        <div class="mt-lookup-meta">
          <span class="mt-team-pill" style="background:${teamColor};">${data.team}</span>
          <span>${escapeHtml(data.node_hex)}</span>
        </div>
        <div class="mt-lookup-meta">Tiles owned: <strong>${data.tiles_owned}</strong></div>
      </div>
    `;
  } catch (e) {
    resultEl.innerHTML = `<span style="color:#c66;">Error: ${escapeHtml(e.message)}</span>`;
  }
}

async function openRosterModal() {
  const modal = document.getElementById('mt-history-modal');
  const body = document.getElementById('mt-history-body');
  const header = modal.querySelector('.mt-modal-header span');
  if (header) header.textContent = 'Team Roster';
  body.innerHTML = '<div style="padding:1em;">Loading...</div>';
  modal.style.display = 'flex';
  try {
    const r = await fetch('/teams');
    const data = await r.json();
    const renderTeam = (label, color, list) => {
      if (!list.length) return `<div class="mt-roster-empty">No ${label} members</div>`;
      const rows = list.map(n => `
        <tr>
          <td>${escapeHtml(n.short_name || n.name || '')}</td>
          <td class="mt-roster-id">${escapeHtml(n.node_hex)}</td>
        </tr>`).join('');
      return `<div class="mt-roster-team">
        <h3 style="color:${color};">${label} — ${list.length}</h3>
        <table class="mt-roster-table"><tbody>${rows}</tbody></table>
      </div>`;
    };
    body.innerHTML = `<div class="mt-roster-grid">
      ${renderTeam('Red', TEAM_COLORS.RED, data.red || [])}
      ${renderTeam('Blue', TEAM_COLORS.BLUE, data.blue || [])}
    </div>`;
  } catch (e) {
    body.innerHTML = `<div style="padding:1em;color:#c66;">Failed to load: ${escapeHtml(e.message)}</div>`;
  }
}

// Reset modal header when opening history (so it doesn't keep saying "Team Roster")
const _originalOpenHistory = openHistoryModal;
openHistoryModal = async function() {
  const modal = document.getElementById('mt-history-modal');
  const header = modal?.querySelector('.mt-modal-header span');
  if (header) header.textContent = 'Past Seasons';
  return _originalOpenHistory();
};



function renderTilePopupSkeleton(coverage, lat, lon) {
  const team = coverage.owner_team || 'GREEN';
  const teamLabel = team === 'GREEN' ? 'NEUTRAL' : team;
  const badgeColor = TEAM_COLORS[team] || '#666';
  return `
    <div class="mt-pop">
      <div class="mt-pop-header">
        <span class="mt-pop-badge" style="background:${badgeColor};">${teamLabel}</span>
        <span class="mt-pop-coord">${lat}, ${lon}</span>
      </div>
      <div class="mt-pop-loading">Loading details…</div>
    </div>
  `;
}

function renderTilePopupRich(coverage, detail, lat, lon) {
  const team = detail.owner_team || 'GREEN';
  const teamLabel = team === 'GREEN' ? 'NEUTRAL' : team;
  const badgeColor = TEAM_COLORS[team] || '#666';
  const meshviewUrl = (window.MT_MESHVIEW_URL || '').replace(/\/$/, '');
  const nodeLink = (nid) => meshviewUrl ? `${meshviewUrl}/node/${nid}` : null;
  const packetLink = (pid) => meshviewUrl ? `${meshviewUrl}/packet/${pid}` : null;

  // Score bar
  const red = detail.scores.RED || 0;
  const blue = detail.scores.BLUE || 0;
  const total = Math.max(red + blue, 1);
  const redPct = (red / total) * 100;
  const bluePct = (blue / total) * 100;

  // Defense window
  let defenseHtml = '';
  if (detail.captures.current_captured_at) {
    const ageS = Math.floor(Date.now()/1000 - detail.captures.current_captured_at);
    const remaining = (15 * 60) - ageS;
    if (remaining > 0) {
      const mins = Math.floor(remaining / 60);
      const secs = remaining % 60;
      defenseHtml = `<div class="mt-pop-defense">🛡 Defense window: <strong>${mins}m ${secs}s</strong></div>`;
    }
  }

  // Last sender (linked if we have meshview URL)
  const ls = detail.last_sender;
  const senderName = ls.short_name || ls.name || ls.hex;
  const senderHtml = nodeLink(ls.node_id)
    ? `<a href="${nodeLink(ls.node_id)}" target="_blank" rel="noopener">${escapeHtml(senderName)}</a>`
    : escapeHtml(senderName);

  // Last packet link
  const packetHtml = (detail.last_packet_id && packetLink(detail.last_packet_id))
    ? `<a href="${packetLink(detail.last_packet_id)}" target="_blank" rel="noopener">packet</a>`
    : '';

  // Top contributors
  let contribHtml = '';
  if (detail.top_contributors && detail.top_contributors.length) {
    const rows = detail.top_contributors.map(c => {
      const name = c.short_name || c.name || c.node_hex;
      const link = nodeLink(c.node_id);
      const nameHtml = link
        ? `<a href="${link}" target="_blank" rel="noopener">${escapeHtml(name)}</a>`
        : escapeHtml(name);
      return `<div class="mt-pop-contrib-row">
        <span>${nameHtml}</span>
        <span class="mt-pop-contrib-count">${c.paint_count}</span>
      </div>`;
    }).join('');
    contribHtml = `
      <div class="mt-pop-section">
        <div class="mt-pop-section-title">Top Contributors</div>
        ${rows}
      </div>`;
  }

  // Capture history
  let capHtml = '';
  if (detail.captures.count > 0) {
    const recentRows = detail.captures.recent.map(c => {
      const when = new Date(c.ts * 1000).toLocaleString();
      const pktHtml = c.packet_id && packetLink(c.packet_id)
        ? ` · <a href="${packetLink(c.packet_id)}" target="_blank" rel="noopener">pkt</a>`
        : '';
      const nodeHtml = nodeLink(c.node_id)
        ? `<a href="${nodeLink(c.node_id)}" target="_blank" rel="noopener">${escapeHtml(c.node_hex)}</a>`
        : escapeHtml(c.node_hex);
      const fromHtml = c.from_team ? `from <span style="color:${TEAM_COLORS[c.from_team]};">${c.from_team}</span>` : 'new';
      return `<div class="mt-pop-cap-row">
        <span style="color:${TEAM_COLORS[c.team]};">${c.team}</span>
        captured ${fromHtml} by ${nodeHtml}${pktHtml}
        <div class="mt-pop-cap-when">${when}</div>
      </div>`;
    }).join('');
    capHtml = `
      <div class="mt-pop-section">
        <div class="mt-pop-section-title">Captures (${detail.captures.count})</div>
        ${recentRows}
      </div>`;
  }

  // Mesh details (collapsible)
  const updatedStr = new Date(detail.last_report_ts * 1000).toLocaleString();
  const feedersHtml = (coverage.rptr || []).map(f => escapeHtml(f)).join(', ') || '—';
  const meshHtml = `
    <details class="mt-pop-mesh-details">
      <summary>Mesh details</summary>
      <div class="mt-pop-mesh-body">
        <div>Packets: ${detail.rcv}</div>
        <div>Updated: ${updatedStr}</div>
        ${detail.last_snr != null ? `<div>SNR: ${detail.last_snr} dB</div>` : ''}
        ${detail.last_rssi != null ? `<div>RSSI: ${detail.last_rssi} dBm</div>` : ''}
        <div class="mt-pop-feeders">Feeders: ${feedersHtml}</div>
      </div>
    </details>
  `;

  return `
    <div class="mt-pop">
      <div class="mt-pop-header">
        <span class="mt-pop-badge" style="background:${badgeColor};">${teamLabel}</span>
        <span class="mt-pop-coord">${lat}, ${lon}</span>
      </div>
      ${defenseHtml}
      <div class="mt-pop-scorebar">
        <div class="mt-pop-scorebar-fill mt-pop-scorebar-red" style="width:${redPct.toFixed(1)}%"></div>
        <div class="mt-pop-scorebar-fill mt-pop-scorebar-blue" style="width:${bluePct.toFixed(1)}%"></div>
      </div>
      <div class="mt-pop-scorenums">
        <span style="color:${TEAM_COLORS.RED};">Red ${red.toFixed(2)}</span>
        <span style="color:${TEAM_COLORS.BLUE};">Blue ${blue.toFixed(2)}</span>
      </div>
      <div class="mt-pop-section">
        <div class="mt-pop-section-title">Last paint</div>
        <div>${senderHtml} ${packetHtml ? '· ' + packetHtml : ''}</div>
      </div>
      ${contribHtml}
      ${capHtml}
      ${meshHtml}
    </div>
  `;
}

/* Cache bust: 1780774709 */
