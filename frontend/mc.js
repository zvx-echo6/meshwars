/*
 * MeshWars: MeshCore board rendering.
 *
 * Self-contained module -- no external libraries, nothing beyond what
 * Leaflet (already loaded by index.html) and the browser provide.
 *
 * This module does not create its own Leaflet map; it attaches to the
 * one code.js creates. Wiring the two together is a separate change
 * (this file is landed unwired, per the task that produced it). The
 * contract that follow-up change needs to satisfy, once it edits
 * frontend/code.js:
 *
 *   1. Load this file as a module after code.js, e.g. in index.html:
 *        <script src="/static/mc.js" type="module"></script>
 *
 *   2. In code.js's initMap(), right after the layer groups are
 *      created (coverageLayer, edgeLayer, sampleLayer, repeaterLayer,
 *      liveTrackLayer), expose the map and the layers that are
 *      normally shown by default -- NOT repeaterLayer, which the
 *      Meshtastic view already keeps off the map by default:
 *
 *        window.MESHWARS_MAP = map;
 *        window.MESHWARS_MESHTASTIC_LAYERS =
 *          [coverageLayer, edgeLayer, sampleLayer, liveTrackLayer];
 *        window.dispatchEvent(new Event('meshwars:map-ready'));
 *
 *      That's the whole integration -- this module discovers the map
 *      via that global/event and takes it from there. No other change
 *      to code.js is required.
 *
 * Until that wiring lands, this module loads, waits for the map, and
 * simply never gets it -- it has no effect on the existing Meshtastic
 * view.
 */

// One place to change team colors. Chosen to be saturated and
// distinguishable from each other on the existing dark basemap.
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

// Same cadence code.js uses for its own coverage/scoreboard refresh.
const REFRESH_INTERVAL_MS = 30000;

// Escapes text destined for an HTML string. display_name and team are
// attacker-controlled (a MeshCore XSS bug hit ~20 analyser sites this
// spring) -- every interpolated value that goes into an HTML string in
// this file must pass through this first.
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[c]));
}

function formatTs(ts) {
  if (!ts) return 'unknown';
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch (e) {
    return 'unknown';
  }
}

// ---- module state ----
let map = null;
let meshtasticLayers = [];
let mcLayerGroup = null;
let mode = 'meshcore'; // 'meshcore' | 'meshtastic'
let toggleControl = null;
let scoreboardControl = null;
let scoreboardBody = null;
let refreshTimer = null;

function waitForMap() {
  return new Promise((resolve) => {
    if (window.MESHWARS_MAP) {
      resolve(window.MESHWARS_MAP);
      return;
    }
    const onReady = () => {
      window.removeEventListener('meshwars:map-ready', onReady);
      clearInterval(poll);
      resolve(window.MESHWARS_MAP);
    };
    window.addEventListener('meshwars:map-ready', onReady);
    // In case the ready event fired before this module attached its
    // listener (module load order isn't guaranteed relative to the
    // event dispatch), also poll for the global directly.
    const poll = setInterval(() => {
      if (window.MESHWARS_MAP) {
        window.removeEventListener('meshwars:map-ready', onReady);
        clearInterval(poll);
        resolve(window.MESHWARS_MAP);
      }
    }, 100);
  });
}

async function loadDefaultMode() {
  try {
    const res = await fetch('/config');
    if (!res.ok) return 'meshcore';
    const cfg = await res.json();
    const raw = String(cfg.mc_default_view || '').trim().toLowerCase();
    if (raw === 'meshtastic') return 'meshtastic';
    if (raw === 'meshcore') return 'meshcore';
  } catch (e) {
    // fall through to default below
  }
  return 'meshcore';
}

// ===== Toggle control =====

function buildToggleControl() {
  const control = L.control({ position: 'topright' });
  control.onAdd = function () {
    const div = L.DomUtil.create('div', 'leaflet-control mc-toggle');

    const label = document.createElement('div');
    label.className = 'mc-toggle-label';
    label.textContent = 'View';
    div.appendChild(label);

    const row = document.createElement('div');
    row.className = 'mc-toggle-row';
    div.appendChild(row);

    const btnMeshtastic = document.createElement('button');
    btnMeshtastic.type = 'button';
    btnMeshtastic.id = 'mc-toggle-meshtastic';
    btnMeshtastic.className = 'mc-toggle-btn';
    btnMeshtastic.textContent = 'Meshtastic';
    row.appendChild(btnMeshtastic);

    const btnMeshcore = document.createElement('button');
    btnMeshcore.type = 'button';
    btnMeshcore.id = 'mc-toggle-meshcore';
    btnMeshcore.className = 'mc-toggle-btn';
    btnMeshcore.textContent = 'MeshCore';
    row.appendChild(btnMeshcore);

    btnMeshtastic.addEventListener('click', (e) => {
      e.stopPropagation();
      setMode('meshtastic');
    });
    btnMeshcore.addEventListener('click', (e) => {
      e.stopPropagation();
      setMode('meshcore');
    });

    L.DomEvent.disableClickPropagation(div);
    L.DomEvent.disableScrollPropagation(div);
    return div;
  };
  return control;
}

function updateToggleButtons() {
  const btnMeshtastic = document.getElementById('mc-toggle-meshtastic');
  const btnMeshcore = document.getElementById('mc-toggle-meshcore');
  if (btnMeshtastic) btnMeshtastic.classList.toggle('active', mode === 'meshtastic');
  if (btnMeshcore) btnMeshcore.classList.toggle('active', mode === 'meshcore');
}

// ===== Scoreboard control =====

function buildScoreboardControl() {
  const control = L.control({ position: 'topright' });
  control.onAdd = function () {
    const div = L.DomUtil.create('div', 'leaflet-control mc-scoreboard');

    const title = document.createElement('div');
    title.className = 'mc-row mc-title';
    title.textContent = 'MeshCore Territory';
    div.appendChild(title);

    const body = document.createElement('div');
    body.className = 'mc-scoreboard-body';
    div.appendChild(body);
    scoreboardBody = body;

    L.DomEvent.disableClickPropagation(div);
    L.DomEvent.disableScrollPropagation(div);
    return div;
  };
  return control;
}

function renderScores(data) {
  if (!scoreboardBody) return;
  // Rebuild from scratch each refresh -- textContent throughout, no
  // HTML string ever touches this panel.
  scoreboardBody.replaceChildren();

  const teams = (data && Array.isArray(data.teams) && data.teams.length)
    ? data.teams
    : TEAM_ORDER.map((t) => ({ team: t, tiles: 0 }));

  teams.forEach((entry) => {
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
    count.textContent = String(entry.tiles ?? 0);
    row.appendChild(count);

    scoreboardBody.appendChild(row);
  });
}

// ===== Board rendering =====

function buildCellPopupHtml(cellId, detail) {
  const scoreRows = TEAM_ORDER.map((team) => {
    const score = detail.scores && detail.scores[team] !== undefined ? detail.scores[team] : 0;
    return `<div class="mc-popup-score-row">
        <span class="mc-dot" style="background:${TEAM_COLORS[team]}"></span>${escapeHtml(team)}: ${escapeHtml(score)}
      </div>`;
  }).join('');

  const captures = Array.isArray(detail.recent_captures) ? detail.recent_captures : [];
  const captureRows = captures.length
    ? captures.map((c) => {
      const who = c.by_display_name ? escapeHtml(c.by_display_name) : 'unknown player';
      const fromNote = c.from_team ? ` (from ${escapeHtml(c.from_team)})` : '';
      return `<div class="mc-popup-capture-row">
          ${escapeHtml(formatTs(c.ts))} &mdash; ${who} for ${escapeHtml(c.by_team)}${fromNote}
        </div>`;
    }).join('')
    : '<div class="mc-popup-capture-row mc-popup-empty">No capture history.</div>';

  return `
    <div class="mc-popup">
      <div class="mc-popup-header">
        <span class="mc-dot" style="background:${TEAM_COLORS[detail.owner_team] || '#888'}"></span>
        ${escapeHtml(cellId)}
      </div>
      <div class="mc-popup-row">Owner: ${escapeHtml(detail.owner_team || 'none')}</div>
      <div class="mc-popup-row">Captured: ${escapeHtml(formatTs(detail.captured_at))}</div>
      <div class="mc-popup-section-title">Scores</div>
      ${scoreRows}
      <div class="mc-popup-section-title">Recent captures</div>
      ${captureRows}
    </div>
  `;
}

// Bound once per rectangle at creation time -- Leaflet opens a layer's
// bound popup on click by default, so no separate 'click' handler is
// needed. Detail is lazy-loaded on 'popupopen', same pattern code.js
// uses for its own tile popups.
function bindCellPopup(rect, cellId) {
  rect.bindPopup('<div class="mc-popup-loading">Loading…</div>', { maxWidth: 320, className: 'mc-tile-popup' });
  rect.on('popupopen', async (e) => {
    try {
      const res = await fetch(`/api/mc/cell/${encodeURIComponent(cellId)}`);
      if (!res.ok) {
        e.popup.setContent('<div class="mc-popup-loading">No data for this cell.</div>');
        return;
      }
      const detail = await res.json();
      e.popup.setContent(buildCellPopupHtml(cellId, detail));
    } catch (err) {
      console.warn('mc cell detail load failed:', err);
      e.popup.setContent('<div class="mc-popup-loading">Failed to load cell detail.</div>');
    }
  });
}

function drawBoard(cells) {
  if (!mcLayerGroup) return;
  mcLayerGroup.clearLayers();
  if (!Array.isArray(cells)) return;

  cells.forEach((cell) => {
    const color = TEAM_COLORS[cell.owner_team] || '#888';
    const rect = L.rectangle(
      [[cell.south, cell.west], [cell.north, cell.east]],
      { color, weight: 1, fillColor: color, fillOpacity: 0.55 },
    );
    bindCellPopup(rect, cell.cell_id);
    mcLayerGroup.addLayer(rect);
  });
}

async function refreshBoard() {
  try {
    const res = await fetch('/api/mc/board');
    if (!res.ok) return;
    const cells = await res.json();
    drawBoard(cells);
  } catch (err) {
    console.warn('mc board refresh failed:', err);
  }
}

async function refreshScores() {
  try {
    const res = await fetch('/api/mc/scores');
    if (!res.ok) return;
    const data = await res.json();
    renderScores(data);
  } catch (err) {
    console.warn('mc scores refresh failed:', err);
  }
}

// ===== Mode switching =====

function setMode(newMode) {
  mode = newMode === 'meshtastic' ? 'meshtastic' : 'meshcore';

  if (mode === 'meshcore') {
    meshtasticLayers.forEach((layer) => {
      if (layer && map.hasLayer(layer)) map.removeLayer(layer);
    });
    if (mcLayerGroup && !map.hasLayer(mcLayerGroup)) mcLayerGroup.addTo(map);
    const scoreboardDiv = document.querySelector('.mc-scoreboard');
    if (scoreboardDiv) scoreboardDiv.classList.remove('mc-hidden');
    refreshBoard();
    refreshScores();
  } else {
    if (mcLayerGroup && map.hasLayer(mcLayerGroup)) map.removeLayer(mcLayerGroup);
    const scoreboardDiv = document.querySelector('.mc-scoreboard');
    if (scoreboardDiv) scoreboardDiv.classList.add('mc-hidden');
    meshtasticLayers.forEach((layer) => {
      if (layer && !map.hasLayer(layer)) layer.addTo(map);
    });
  }

  updateToggleButtons();
}

// ===== Boot =====

async function boot() {
  map = await waitForMap();
  meshtasticLayers = Array.isArray(window.MESHWARS_MESHTASTIC_LAYERS)
    ? window.MESHWARS_MESHTASTIC_LAYERS
    : [];

  mcLayerGroup = L.layerGroup();

  toggleControl = buildToggleControl();
  toggleControl.addTo(map);

  scoreboardControl = buildScoreboardControl();
  scoreboardControl.addTo(map);
  renderScores(null); // seed all-zero rows immediately, before the first fetch

  const defaultMode = await loadDefaultMode();
  setMode(defaultMode);

  refreshTimer = setInterval(() => {
    if (mode === 'meshcore') {
      refreshBoard();
      refreshScores();
    }
  }, REFRESH_INTERVAL_MS);
}

boot().catch((err) => {
  console.error('MeshCore module failed to start:', err);
});
