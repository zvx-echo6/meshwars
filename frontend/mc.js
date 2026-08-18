/*
 * MeshWars: MeshCore board rendering.
 *
 * Self-contained module -- no external libraries, nothing beyond what
 * Leaflet (already loaded by index.html) and the browser provide.
 *
 * This module attaches to the Leaflet map code.js creates via the
 * window.MESHWARS_MAP / window.MESHWARS_MESHTASTIC_LAYERS / 'meshwars:map-ready'
 * handoff and window.MESHWARS_MT_CONTROLS (see code.js's initMap for where
 * those are set). This module never reaches into code.js's internals
 * beyond that handoff, and code.js is never modified by this module.
 *
 * The MeshCore territory panel built below is a parity match for
 * code.js's Meshtastic scoreboard control (season countdown, player
 * search, History/Roster links, Refresh map, and a ranked-players
 * button) -- but every modal and popup here has its own markup/styles
 * (this file + mc.css), never code.js's #mt-history-modal or its
 * openHistoryModal/openRosterModal functions.
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

// Cap on how far map.fitBounds() is ever allowed to zoom in when
// framing the MeshCore board or a player search result. A board (or a
// single player) holding only one or two 300m cells has a tiny bounds
// box -- fitting to it with no cap zooms in far enough that a visitor
// sees one giant coloured rectangle filling the screen with no
// surrounding context. At zoom 13 a 300m square is small but clearly
// visible with several kilometres of context around it, which reads as
// a game board rather than a coloured blob. Keep this even once the
// board is full and every fit naturally lands well under this cap --
// it's the sparse early board (and any single-player search) this
// exists to protect against, and that stops being visible from the UI
// once the symptom is gone.
const MAX_FIT_ZOOM = 13;

// Matches the breakpoint used elsewhere in mc.css/coverage.css (the
// settings control, roster grid, About overlay) for "phone-width".
const NARROW_BREAKPOINT_PX = 600;

// Escapes text destined for an HTML string. display_name and team are
// attacker-controlled (a MeshCore XSS bug hit ~20 analyser sites this
// spring) -- every interpolated value that goes into an HTML string in
// this file must pass through this first (or use textContent instead
// of building HTML at all).
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

// Same wording/thresholds as code.js's formatCountdown -- duplicated
// rather than imported since code.js does not export it and this
// module must not reach into code.js's internals.
function formatCountdown(secondsRemaining) {
  if (secondsRemaining <= 0) return 'closing';
  const days = Math.floor(secondsRemaining / 86400);
  const hours = Math.floor((secondsRemaining % 86400) / 3600);
  const mins = Math.floor((secondsRemaining % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${mins}m`;
  return `${mins}m`;
}

// ---- module state ----
let map = null;
let meshtasticLayers = [];
let mcLayerGroup = null;
let mode = 'meshcore'; // 'meshcore' | 'meshtastic'
let toggleControl = null;
let scoreboardControl = null;
let scoreboardBody = null;
let scoreboardPanelEl = null;
let scoreboardHeaderBtn = null;
let scoreboardSummaryEl = null;
let refreshTimer = null;
let scoreboardEndsAt = null;
// Play-area box from /config, loaded once at boot -- used to frame the
// map when the MeshCore board has no owned cells yet (see refreshBoard).
let playAreaBounds = null;

// Show/hide the Meshtastic scoreboard control (Red/Blue/Neutral counts,
// season countdown, node search, History/Roster, Top MQTT Feeders) while
// the MeshCore view is active -- those are Meshtastic-only concepts.
// code.js hands us its container(s) via this global, the same handoff
// pattern used for window.MESHWARS_MAP / window.MESHWARS_MESHTASTIC_LAYERS,
// so this module never has to guess at code.js's DOM structure.
function setMeshtasticControlsVisible(visible) {
  const controls = Array.isArray(window.MESHWARS_MT_CONTROLS) ? window.MESHWARS_MT_CONTROLS : [];
  controls.forEach((el) => {
    if (el) el.style.display = visible ? '' : 'none';
  });
}

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

// code.js mounts its .meshwars-scoreboard control synchronously, a few
// lines after the map-ready handoff this module waits on -- in practice
// there's no yield point between the two, so it's already in the DOM by
// the time boot() gets here. Poll anyway (short timeout) rather than
// assume that ordering; if code.js's control never shows up at all, the
// MeshCore panel just keeps its own natural (min-width driven) size.
function waitForMtPanel(timeoutMs) {
  return new Promise((resolve) => {
    const existing = document.querySelector('.meshwars-scoreboard');
    if (existing) { resolve(existing); return; }
    const deadline = Date.now() + timeoutMs;
    const poll = setInterval(() => {
      const el = document.querySelector('.meshwars-scoreboard');
      if (el || Date.now() > deadline) {
        clearInterval(poll);
        resolve(el);
      }
    }, 50);
  });
}

// Cached at boot, once, while both panels are still in their initial
// un-hidden state. code.js's setMode()/setMeshtasticControlsVisible(false)
// sets the Meshtastic panel to display:none as soon as the MeshCore view
// becomes active -- after that its offsetWidth reads back as 0, so this
// measurement can never be safely retaken later. Not re-measured on
// window resize or on later view toggles; the two panels share one
// width for the life of the page load.
let cachedMtPanelWidth = null;

async function applyMatchedPanelWidth() {
  const mtPanel = await waitForMtPanel(3000);
  const mcPanel = document.querySelector('.mc-scoreboard');
  if (!mtPanel || !mcPanel) return;
  cachedMtPanelWidth = mtPanel.offsetWidth;
  if (cachedMtPanelWidth > 0) {
    // .mc-scoreboard is box-sizing: border-box (see mc.css) so this
    // sets the *total* rendered width to match, the same quantity
    // offsetWidth measures on the Meshtastic side -- regardless of any
    // padding/border differences between the two panels.
    mcPanel.style.width = `${cachedMtPanelWidth}px`;
  }
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

// Loaded once at boot, in parallel with loadDefaultMode(). If /config is
// unreachable or play_area is missing, playAreaBounds stays null and
// refreshBoard() simply leaves the map wherever it already is, same as
// before this existed.
async function loadPlayArea() {
  try {
    const res = await fetch('/config');
    if (!res.ok) return;
    const cfg = await res.json();
    const pa = cfg.play_area;
    if (
      pa && typeof pa.north === 'number' && typeof pa.south === 'number' &&
      typeof pa.west === 'number' && typeof pa.east === 'number'
    ) {
      playAreaBounds = L.latLngBounds([pa.south, pa.west], [pa.north, pa.east]);
    }
  } catch (e) {
    // leave playAreaBounds null
  }
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
//
// Parity match for code.js's .meshwars-scoreboard control: team counts,
// season countdown, player search + Find, History/Roster links, a
// Refresh map button, and a ranked-players button (Top Wardrivers --
// the MeshCore equivalent of Meshtastic's Top MQTT Feeders, since
// MeshCore has no MQTT feeders, only players).

function buildScoreboardControl() {
  const control = L.control({ position: 'topright' });
  control.onAdd = function () {
    const div = L.DomUtil.create('div', 'leaflet-control mc-scoreboard');
    div.innerHTML = `
      <button type="button" class="mc-row mc-title mc-header-btn" id="mc-header-btn" aria-expanded="true">
        <span class="mc-header-title-text">MeshCore Territory</span>
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
          <button type="button" id="mc-top-btn">Top Wardrivers</button>
        </div>
      </div>
    `;
    scoreboardBody = div.querySelector('.mc-scoreboard-body');
    scoreboardPanelEl = div;
    scoreboardHeaderBtn = div.querySelector('#mc-header-btn');
    scoreboardSummaryEl = div.querySelector('#mc-header-summary');

    L.DomEvent.disableClickPropagation(div);
    L.DomEvent.disableScrollPropagation(div);

    // Collapsible territory panel (phones only) -- see mc.css's
    // .mc-collapsed rule, which is itself gated to NARROW_BREAKPOINT_PX
    // so this toggle has no visual effect on desktop even though the
    // button and listener exist there too.
    scoreboardHeaderBtn.addEventListener('click', (e) => {
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

    const lookupInput = div.querySelector('#mc-lookup-input');
    const lookupBtn = div.querySelector('#mc-lookup-btn');
    const doLookup = () => doPlayerFind(lookupInput.value);
    lookupBtn.addEventListener('click', doLookup);
    lookupInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') doLookup(); });
    // Stop the map from stealing keystrokes while typing -- same fix
    // code.js applies to its own #mt-lookup-input.
    L.DomEvent.on(lookupInput, 'keydown keypress keyup mousedown mouseup click dblclick',
                  L.DomEvent.stopPropagation);

    div.querySelector('#mc-refresh-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      refreshBoard(false);
      refreshScores();
    });

    div.querySelector('#mc-top-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      openTopModal();
    });

    return div;
  };
  return control;
}

// Picks whichever team currently holds the most cells, for the
// collapsed-header summary ("TEAM count") on phones -- the /api/mc/scores
// teams array is in fixed TEAM_ORDER, not sorted by tile count, so this
// has to be computed client-side rather than just taking teams[0].
function leadingTeam(teams) {
  let best = null;
  teams.forEach((t) => {
    if (!best || (t.tiles ?? 0) > (best.tiles ?? 0)) best = t;
  });
  return best;
}

function setMcCollapsed(collapsed) {
  if (!scoreboardPanelEl || !scoreboardHeaderBtn) return;
  scoreboardPanelEl.classList.toggle('mc-collapsed', collapsed);
  scoreboardHeaderBtn.setAttribute('aria-expanded', String(!collapsed));
}

function renderScores(data) {
  if (!scoreboardBody) return;
  // Rebuild from scratch each refresh -- textContent throughout, no
  // HTML string ever touches this panel.
  scoreboardBody.replaceChildren();

  const teams = (data && Array.isArray(data.teams) && data.teams.length)
    ? data.teams
    : TEAM_ORDER.map((t) => ({ team: t, tiles: 0 }));

  if (scoreboardSummaryEl) {
    const lead = leadingTeam(teams);
    scoreboardSummaryEl.textContent = lead ? `${lead.team} ${lead.tiles ?? 0}` : '';
  }

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

function tickCountdown() {
  const el = document.getElementById('mc-countdown');
  if (!el || !scoreboardEndsAt) return;
  const now = Math.floor(Date.now() / 1000);
  el.textContent = formatCountdown(scoreboardEndsAt - now);
}

// ===== Player search (Find) =====
//
// Placeholder text and copy deliberately say "player" rather than
// "node"/"radio" -- MeshCore's /api/mc/find looks up a person by
// display name, not a piece of hardware by id. Every branch below sets
// textContent only, never innerHTML, so this path needs no separate
// escaping review: it structurally cannot execute a payload.
async function doPlayerFind(value) {
  const resultEl = document.getElementById('mc-lookup-result');
  if (!resultEl) return;
  const name = (value || '').trim();
  if (!name) { resultEl.textContent = ''; return; }
  resultEl.textContent = 'Searching...';
  try {
    const res = await fetch(`/api/mc/find?name=${encodeURIComponent(name)}`);
    if (res.status === 404) {
      resultEl.textContent = `Not found: ${name}`;
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
    if (map) map.fitBounds([[b.south, b.west], [b.north, b.east]], { padding: [24, 24], maxZoom: MAX_FIT_ZOOM });
    const plural = data.tiles_held === 1 ? '' : 's';
    resultEl.textContent = `${data.display_name} (${data.team}) holds ${data.tiles_held} cell${plural}.`;
  } catch (err) {
    resultEl.textContent = 'Search failed.';
  }
}

// ===== Modal (History / Roster / Top Wardrivers) =====
//
// Built and styled entirely in this module (markup here, styles in
// mc.css) -- deliberately not index.html's #mt-history-modal or
// code.js's openHistoryModal/openRosterModal, which operate on
// Meshtastic data and that module's own DOM.

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
  // Click on the dimmed backdrop (not the inner card) closes the modal.
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
    const res = await fetch('/api/mc/history');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const seasons = await res.json();
    if (!Array.isArray(seasons) || seasons.length === 0) {
      showModalMessage(body, 'mc-modal-empty', 'No completed seasons yet.');
      return;
    }
    const rows = seasons.map((s) => {
      const started = s.started_at ? new Date(s.started_at * 1000).toLocaleDateString() : '?';
      const ended = s.ends_at ? new Date(s.ends_at * 1000).toLocaleDateString() : '?';
      const teams = Array.isArray(s.teams) ? s.teams : [];
      const tallyText = teams
        .filter((t) => (t.tiles ?? 0) > 0)
        .map((t) => `${escapeHtml(t.team)} ${escapeHtml(t.tiles)}`)
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
  } catch (err) {
    showModalMessage(body, 'mc-modal-error', `Failed to load: ${err.message}`);
  }
}

async function openRosterModal() {
  const body = openMcModal('Player Roster');
  try {
    const res = await fetch('/api/mc/players');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const players = await res.json();
    if (!Array.isArray(players) || players.length === 0) {
      showModalMessage(body, 'mc-modal-empty', 'No players yet.');
      return;
    }
    const byTeam = new Map();
    players.forEach((p) => {
      const team = p.team || 'UNKNOWN';
      if (!byTeam.has(team)) byTeam.set(team, []);
      byTeam.get(team).push(p);
    });
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

async function openTopModal() {
  const body = openMcModal('Top Wardrivers');
  try {
    const res = await fetch('/api/mc/top');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const rows = await res.json();
    if (!Array.isArray(rows) || rows.length === 0) {
      showModalMessage(body, 'mc-modal-empty', 'No capture activity yet.');
      return;
    }
    const trs = rows.map((r, i) => {
      const color = TEAM_COLORS[r.team] || '#888';
      return `<tr>
        <td>${i + 1}</td>
        <td>${escapeHtml(r.display_name)}</td>
        <td><span class="mc-dot" style="background:${color}"></span>${escapeHtml(r.team)}</td>
        <td>${escapeHtml(r.captures)}</td>
      </tr>`;
    }).join('');
    body.innerHTML = `<table class="mc-history-table">
      <thead><tr><th>#</th><th>Player</th><th>Team</th><th>Captures</th></tr></thead>
      <tbody>${trs}</tbody>
    </table>`;
  } catch (err) {
    showModalMessage(body, 'mc-modal-error', `Failed to load: ${err.message}`);
  }
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

// Bounding box of every cell the board API returned, using the bounds
// it already computes server-side (app.grid.cell_bounds) -- this module
// never recomputes cell geometry itself.
function boardBounds(cells) {
  if (!Array.isArray(cells) || cells.length === 0) return null;
  let south = Infinity, west = Infinity, north = -Infinity, east = -Infinity;
  cells.forEach((c) => {
    if (c.south < south) south = c.south;
    if (c.west < west) west = c.west;
    if (c.north > north) north = c.north;
    if (c.east > east) east = c.east;
  });
  return L.latLngBounds([south, west], [north, east]);
}

// fitToBoard is true only when this refresh is the result of switching
// INTO the MeshCore view (or the initial load, if MeshCore is the
// default) -- never on the 30s auto-refresh timer or the Refresh map
// button, which must not fight the user's own panning/zooming.
async function refreshBoard(fitToBoard) {
  try {
    const res = await fetch('/api/mc/board');
    if (!res.ok) return;
    const cells = await res.json();
    drawBoard(cells);
    if (fitToBoard) {
      const bounds = boardBounds(cells);
      if (bounds && map) {
        map.fitBounds(bounds, { padding: [24, 24], maxZoom: MAX_FIT_ZOOM });
      } else if (map && playAreaBounds) {
        // No owned cells yet -- frame the configured play area instead
        // of leaving the map wherever it happened to be.
        map.fitBounds(playAreaBounds);
      }
    }
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
    scoreboardEndsAt = data.ends_at || null;
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
    setMeshtasticControlsVisible(false);
    refreshBoard(true);
    refreshScores();
  } else {
    if (mcLayerGroup && map.hasLayer(mcLayerGroup)) map.removeLayer(mcLayerGroup);
    const scoreboardDiv = document.querySelector('.mc-scoreboard');
    if (scoreboardDiv) scoreboardDiv.classList.add('mc-hidden');
    setMeshtasticControlsVisible(true);
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

  // Leaflet stacks controls in the same corner in add order, so without
  // this the VIEW toggle would end up wherever it happens to land relative
  // to the two territory panels -- and since only one territory panel is
  // visible at a time, hiding one lets the controls below it slide up,
  // making the toggle (and the visible panel) jump position when the user
  // switches boards. Force the toggle to be the first child of its corner
  // container so it always stays put, with whichever territory panel is
  // active directly beneath it. Do not remove this thinking it is a no-op.
  const toggleContainer = toggleControl.getContainer();
  if (toggleContainer && toggleContainer.parentNode) {
    toggleContainer.parentNode.insertBefore(toggleContainer, toggleContainer.parentNode.firstChild);
  }

  scoreboardControl = buildScoreboardControl();
  scoreboardControl.addTo(map);
  renderScores(null); // seed all-zero rows immediately, before the first fetch

  // Territory panel starts collapsed on narrow screens only (phones) --
  // it otherwise eats roughly the top half of a phone screen. Desktop
  // always starts (and stays) expanded; see setMcCollapsed / mc.css.
  if (window.matchMedia(`(max-width: ${NARROW_BREAKPOINT_PX}px)`).matches) {
    setMcCollapsed(true);
  }

  // Match panel widths (see cachedMtPanelWidth comment above) -- must
  // happen before setMode() below, which is what hides whichever panel
  // isn't the default view.
  await applyMatchedPanelWidth();

  const [defaultMode] = await Promise.all([loadDefaultMode(), loadPlayArea()]);
  setMode(defaultMode);

  refreshTimer = setInterval(() => {
    if (mode === 'meshcore') {
      refreshBoard(false);
      refreshScores();
    }
  }, REFRESH_INTERVAL_MS);

  setInterval(tickCountdown, 1000);
}

boot().catch((err) => {
  console.error('MeshCore module failed to start:', err);
});
