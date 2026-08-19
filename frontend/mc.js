/*
 * MeshWars: map page (/). Boots the Leaflet map and renders BOTH boards
 * -- MeshCore (protocol='mc') and Meshtastic (protocol='mt') -- through
 * one renderer and one top-right panel. Self-contained module -- no
 * external libraries, nothing beyond what Leaflet (loaded by index.html)
 * and the browser provide.
 *
 * History: this file used to attach a MeshCore-only board on top of a
 * separate legacy module (frontend/code.js) that drew the retired
 * Meshtastic geohash-tile fortress game -- team colors, palette, and
 * grid all specific to that retired game. The backend has since moved
 * Meshtastic onto the exact same player/grid-cell model MeshCore already
 * runs on (see app/ingest.py's module docstring), so code.js's
 * geohash-drawing/color-palette/Territory-Mode/sample-dot code had
 * nothing left to draw and was deleted outright rather than restyled --
 * this module now owns map bootstrapping too and renders both boards by
 * asking each protocol-parameterized endpoint for the same shape of
 * data (see PROTOCOLS below).
 *
 * The two boards use different endpoint families because the Meshtastic
 * routes were grandfathered in at the API root (/get-nodes, /scores,
 * /history, /cell/{id}, /teams, /team/{node_ref} -- see app/api.py)
 * while MeshCore's are namespaced under /api/mc/*, but both sides read
 * the exact same mc_season/mc_tile* tables underneath (app/mc_api.py's
 * protocol-parameterized helpers) -- see the PROTOCOLS table below for
 * the one-to-one mapping and the honest gaps where no Meshtastic
 * equivalent of an /api/mc/* route exists yet.
 */

// One place to change team colors. Chosen to be saturated and
// distinguishable from each other on the existing dark basemap. The
// ONLY team-color map in the frontend for the map page -- both boards
// read this same object, never a second copy (frontend/join.js keeps
// its own, deliberately, since that page must load independently of
// this one -- see that file's header comment).
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

// Same cadence the retired code.js used for its own coverage/scoreboard
// refresh.
const REFRESH_INTERVAL_MS = 30000;

// Cap on how far map.fitBounds() is ever allowed to zoom in when
// framing a board or a single player search result. A board (or a
// single player) holding only one or two 300m cells has a tiny bounds
// box -- fitting to it with no cap zooms in far enough that a visitor
// sees one giant colored rectangle filling the screen with no
// surrounding context. At zoom 13 a 300m square is small but clearly
// visible with several kilometers of context around it, which reads as
// a game board rather than a colored blob. Keep this even once a board
// is full and every fit naturally lands well under this cap -- it's the
// sparse early board (and any single-player search) this exists to
// protect against, and that stops being visible from the UI once the
// symptom is gone.
const MAX_FIT_ZOOM = 13;

// Matches the breakpoint used elsewhere in mc.css (the collapsible
// header, roster grid) for "phone-width".
const NARROW_BREAKPOINT_PX = 600;

// Fallback map center/zoom if /config is unreachable, and the starting
// values before it resolves.
let centerPos = [37.3382, -121.8863];
let initialZoom = 10;
let maxDistanceMiles = 0;

// Escapes text destined for an HTML string. display_name and team are
// attacker-controlled (a MeshCore XSS bug hit ~20 analyzer sites this
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

function formatCountdown(secondsRemaining) {
  if (secondsRemaining <= 0) return 'closing';
  const days = Math.floor(secondsRemaining / 86400);
  const hours = Math.floor((secondsRemaining % 86400) / 3600);
  const mins = Math.floor((secondsRemaining % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${mins}m`;
  return `${mins}m`;
}

// =====================================================================
// PROTOCOLS -- the one-to-one mapping between the two boards, and the
// vocabulary that differs between them. Everything in this module that
// varies by board reads from here, so the swap is one lookup, never a
// scattered set of `if (protocol === 'mt')` branches.
//
// Board switch is data-only, on purpose (the owner's explicit
// requirement): only the values below and the fetched data ever change
// between boards. The panel's structure, control order, and position
// are identical regardless of which entry is active.
//
// Endpoint parity, and the two honest gaps this file does NOT paper
// over with invented data:
//
//  - Board cells:   MeshCore /api/mc/board (array) vs Meshtastic
//                    /get-nodes (.coverage) -- both cell shapes match,
//                    just fetched/unwrapped differently. See fetchBoardCells.
//  - Scores/season: /api/mc/scores vs /scores -- identical shape
//                    (app/api.py's /scores calls the exact same
//                    scores_for() helper mc_api.py's own route does).
//  - Cell popup:    /api/mc/cell/{id} vs /cell/{id} -- identical shape
//                    (same cell_detail_for() helper on both sides).
//  - History modal: /api/mc/history (array) vs /history (.seasons) --
//                    same per-season shape once unwrapped.
//  - Roster modal:  /api/mc/players (flat [{display_name,team}]) vs
//                    /teams ({teams:{TEAM:[{display_name,node_hex}]}})
//                    -- different shape, both grouped into a common
//                    Map<team, [{display_name}]> by fetchRoster below.
//  - GAP -- player Find: /api/mc/find?name= looks a player up BY NAME
//                    and returns a bounds box to zoom to. There is no
//                    Meshtastic equivalent of that route -- the closest
//                    is /team/{node_ref}, which looks a player up by
//                    NODE REFERENCE instead of name and returns no
//                    bounds at all. So on the Meshtastic board this
//                    control searches by node ID instead of name, and
//                    a successful search cannot zoom the map -- both
//                    intentional, not bugs. See doPlayerFind.
//  - GAP -- top-ranked players: /api/mc/top ranks players by capture
//                    count in the active MeshCore season. No Meshtastic
//                    route computes anything equivalent (nor could this
//                    module derive it client-side: neither /get-nodes'
//                    coverage cells nor /cell/{id} expose which player
//                    owns a cell, only which TEAM does). The Meshtastic
//                    "Top Operators" modal says so plainly rather than
//                    showing invented or zeroed numbers. See openTopModal.
//  - GAP -- cell popup feeders/repeaters: neither /cell/{id} nor
//                    /api/mc/cell/{id} return anything about which
//                    repeaters/feeders can hear a given cell -- the old
//                    per-tile `rptr` list doesn't exist in the new
//                    mc_tile-backed model. app/db.py's own comment on
//                    the repeater_observation/repeater_identity tables
//                    confirms this is deliberate for now ("nothing here
//                    reads from these tables yet"). This module does
//                    not reintroduce the old distance-guessed
//                    repeater-per-tile heuristic to fake the gap --
//                    that guess is exactly what those tables exist to
//                    eventually replace with real observation data. See
//                    the module docstring / final report for the
//                    follow-up this needs on the backend.
// =====================================================================
const PROTOCOLS = {
  meshcore: {
    protocol: 'mc',
    boardTitle: 'MeshCore Territory',
    topButtonLabel: 'Top Wardrivers',
    topModalTitle: 'Top Wardrivers',
    lookupPlaceholder: 'player name',
    lookupHelp: 'Search by player name.',
    boardEndpoint: '/api/mc/board',
    scoresEndpoint: '/api/mc/scores',
    cellEndpoint: (id) => `/api/mc/cell/${encodeURIComponent(id)}`,
    historyEndpoint: '/api/mc/history',
    rosterEndpoint: '/api/mc/players',
    findEndpoint: (q) => `/api/mc/find?name=${encodeURIComponent(q)}`,
    topEndpoint: '/api/mc/top',
  },
  meshtastic: {
    protocol: 'mt',
    boardTitle: 'Meshtastic Territory',
    // "Wardrivers" is MeshCore-specific vocabulary (MeshMapper is a
    // deliberate wardriving app); a Meshtastic node just broadcasts its
    // own position, so the community term for the person behind a node
    // -- "operator" -- fits better than reusing "wardriver" here.
    topButtonLabel: 'Top Operators',
    topModalTitle: 'Top Operators',
    lookupPlaceholder: 'node ID (!a1b2c3d4)',
    lookupHelp: 'Search by node ID -- Meshtastic players are looked up by radio, not by name.',
    boardEndpoint: '/get-nodes',
    scoresEndpoint: '/scores',
    cellEndpoint: (id) => `/cell/${encodeURIComponent(id)}`,
    historyEndpoint: '/history',
    rosterEndpoint: '/teams',
    findEndpoint: null, // no by-name lookup route -- see doPlayerFind
    topEndpoint: null,  // no ranking route -- see openTopModal
  },
};

// ---- module state ----
let map = null;
let cellLayerGroup = null;
let mode = 'meshcore'; // key into PROTOCOLS
let scoreboardControl = null;
let scoreboardBody = null;
let scoreboardPanelEl = null;
let scoreboardHeaderBtn = null;
let scoreboardSummaryEl = null;
let scoreboardTitleEl = null;
let scoreboardTopBtn = null;
let scoreboardLookupInput = null;
let refreshTimer = null;
let scoreboardEndsAt = null;
// Play-area box from /config, loaded once at boot -- used to frame the
// map when a board has no owned cells yet (see refreshBoard).
let playAreaBounds = null;

function cfg() {
  return PROTOCOLS[mode];
}

// ===== Board switch (the territory card's own top row) =====

function updateToggleButtons() {
  const btnMeshtastic = document.getElementById('mc-toggle-meshtastic');
  const btnMeshcore = document.getElementById('mc-toggle-meshcore');
  if (btnMeshtastic) btnMeshtastic.classList.toggle('active', mode === 'meshtastic');
  if (btnMeshcore) btnMeshcore.classList.toggle('active', mode === 'meshcore');
}

// ===== Scoreboard control =====
//
// One panel for both boards: board switch, collapsible header with live
// summary, seven-team scoreboard with color dots, season countdown,
// player lookup, History/Roster, Refresh, and a ranked-players button.
// Every element below exists in the DOM at every width and for both
// boards -- only its text/data content changes with mode (see setMode
// and the per-board fetch functions) -- so the panel never gains or
// loses a control when the board switches.

function buildScoreboardControl() {
  const control = L.control({ position: 'topright' });
  control.onAdd = function () {
    const div = L.DomUtil.create('div', 'leaflet-control mc-scoreboard');
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
      </div>
    `;
    scoreboardBody = div.querySelector('.mc-scoreboard-body');
    scoreboardPanelEl = div;
    scoreboardHeaderBtn = div.querySelector('#mc-header-btn');
    scoreboardSummaryEl = div.querySelector('#mc-header-summary');
    scoreboardTitleEl = div.querySelector('#mc-header-title-text');
    scoreboardTopBtn = div.querySelector('#mc-top-btn');
    scoreboardLookupInput = div.querySelector('#mc-lookup-input');

    L.DomEvent.disableClickPropagation(div);
    L.DomEvent.disableScrollPropagation(div);

    // Board switch -- row one of the card, always visible, never inside
    // .mc-panel-content, so it survives the phone collapse below.
    div.querySelector('#mc-toggle-meshtastic').addEventListener('click', (e) => {
      e.stopPropagation();
      setMode('meshtastic');
    });
    div.querySelector('#mc-toggle-meshcore').addEventListener('click', (e) => {
      e.stopPropagation();
      setMode('meshcore');
    });

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

    const lookupBtn = div.querySelector('#mc-lookup-btn');
    const doLookup = () => doPlayerFind(scoreboardLookupInput.value);
    lookupBtn.addEventListener('click', doLookup);
    scoreboardLookupInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') doLookup(); });
    // Stop the map from stealing keystrokes while typing.
    L.DomEvent.on(scoreboardLookupInput, 'keydown keypress keyup mousedown mouseup click dblclick',
                  L.DomEvent.stopPropagation);

    div.querySelector('#mc-refresh-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      refreshBoard(false);
      refreshScores();
    });

    scoreboardTopBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      openTopModal();
    });

    return div;
  };
  return control;
}

// Picks whichever team currently holds the most cells, for the
// collapsed-header summary ("TEAM count") on phones -- the scores
// endpoint's teams array is in fixed TEAM_ORDER, not sorted by tile
// count, so this has to be computed client-side rather than just
// taking teams[0].
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

// ===== Board cell fetch (protocol-parameterized) =====

// The two boards' "give me every owned cell" routes return different
// envelopes (see PROTOCOLS' comment above) -- this is the one place
// that difference is unwrapped, so every caller below just gets an
// array of cells either way.
async function fetchBoardCells(c) {
  const res = await fetch(c.boardEndpoint);
  if (!res.ok) return [];
  const data = await res.json();
  if (Array.isArray(data)) return data; // MeshCore: array directly
  return Array.isArray(data.coverage) ? data.coverage : []; // Meshtastic: {coverage, repeaters}
}

async function fetchHistorySeasons(c) {
  const res = await fetch(c.historyEndpoint);
  if (!res.ok) return [];
  const data = await res.json();
  if (Array.isArray(data)) return data; // MeshCore: array directly
  return Array.isArray(data.seasons) ? data.seasons : []; // Meshtastic: {seasons}
}

// Normalizes both roster shapes into a common Map<team, [{display_name}]>
// -- MeshCore's /api/mc/players is a flat list this groups itself;
// Meshtastic's /teams is already grouped, just keyed differently
// (node_hex instead of nothing extra). Only display_name is used either
// way, since that's all the roster modal shows.
async function fetchRosterByTeam(c) {
  const byTeam = new Map();
  if (c.protocol === 'mc') {
    const res = await fetch(c.rosterEndpoint);
    if (!res.ok) return byTeam;
    const players = await res.json();
    if (!Array.isArray(players)) return byTeam;
    players.forEach((p) => {
      const team = p.team || 'UNKNOWN';
      if (!byTeam.has(team)) byTeam.set(team, []);
      byTeam.get(team).push({ display_name: p.display_name });
    });
  } else {
    const res = await fetch(c.rosterEndpoint);
    if (!res.ok) return byTeam;
    const data = await res.json();
    const teams = (data && data.teams) || {};
    Object.keys(teams).forEach((team) => {
      byTeam.set(team, (teams[team] || []).map((p) => ({ display_name: p.display_name })));
    });
  }
  return byTeam;
}

// ===== Player search (Find) =====
//
// MeshCore looks a player up by display name and gets back a bounds box
// to zoom to (/api/mc/find). Meshtastic has no by-name route -- the
// closest available is /team/{node_ref}, a by-NODE-REFERENCE lookup
// that returns no bounds at all -- so on that board this searches by
// node ID instead, and a hit reports team/tiles-owned as text without
// moving the map. Both are real, working lookups against real routes;
// neither fakes the other board's behavior. See PROTOCOLS' comment for
// why. Every branch below sets textContent only, never innerHTML.
async function doPlayerFind(value) {
  const c = cfg();
  const resultEl = document.getElementById('mc-lookup-result');
  if (!resultEl) return;
  const query = (value || '').trim();
  if (!query) { resultEl.textContent = ''; return; }
  resultEl.textContent = 'Searching...';

  if (c.protocol === 'mc') {
    try {
      const res = await fetch(c.findEndpoint(query));
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
      if (map) map.fitBounds([[b.south, b.west], [b.north, b.east]], { padding: [24, 24], maxZoom: MAX_FIT_ZOOM });
      const plural = data.tiles_held === 1 ? '' : 's';
      resultEl.textContent = `${data.display_name} (${data.team}) holds ${data.tiles_held} cell${plural}.`;
    } catch (err) {
      resultEl.textContent = 'Search failed.';
    }
    return;
  }

  // Meshtastic: /team/{node_ref} -- by node reference, no bounds.
  try {
    const res = await fetch(`/team/${encodeURIComponent(query)}`);
    if (!res.ok) {
      resultEl.textContent = 'Search failed.';
      return;
    }
    const data = await res.json();
    if (!data.found) {
      resultEl.textContent = `Not found: ${query}`;
      return;
    }
    const plural = data.tiles_owned === 1 ? '' : 's';
    resultEl.textContent = `${data.display_name} (${data.team}) holds ${data.tiles_owned} cell${plural}.`;
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
  const c = cfg();
  const body = openMcModal('Past Seasons');
  try {
    const seasons = await fetchHistorySeasons(c);
    if (!seasons.length) {
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
  const c = cfg();
  const body = openMcModal('Player Roster');
  try {
    const byTeam = await fetchRosterByTeam(c);
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

// GAP: no Meshtastic route ranks players by squares held or captures
// (see PROTOCOLS' comment) -- this says so honestly rather than
// inventing or zeroing numbers. Real backend follow-up needed before
// this can show real Meshtastic rankings.
async function openTopModal() {
  const c = cfg();
  const body = openMcModal(c.topModalTitle);

  if (!c.topEndpoint) {
    showModalMessage(body, 'mc-modal-empty',
      'Player rankings aren\'t available for the Meshtastic board yet -- check Roster for the full player list.');
    return;
  }

  try {
    const res = await fetch(c.topEndpoint);
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

// GAP: no field on either /cell/{id} or /api/mc/cell/{id} names which
// repeaters/feeders can hear this cell -- see PROTOCOLS' comment above
// for why this section deliberately does not exist here. Everything
// below is otherwise identical for both boards.
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
// needed. Detail is lazy-loaded on 'popupopen'. `c` is captured at
// draw time (not read from `cfg()` again on open) so a popup a visitor
// already has open keeps working even if they've since flipped boards.
function bindCellPopup(rect, cellId, c) {
  rect.bindPopup('<div class="mc-popup-loading">Loading…</div>', { maxWidth: 320, className: 'mc-tile-popup' });
  rect.on('popupopen', async (e) => {
    try {
      const res = await fetch(c.cellEndpoint(cellId));
      if (!res.ok) {
        e.popup.setContent('<div class="mc-popup-loading">No data for this cell.</div>');
        return;
      }
      const detail = await res.json();
      e.popup.setContent(buildCellPopupHtml(cellId, detail));
    } catch (err) {
      console.warn('cell detail load failed:', err);
      e.popup.setContent('<div class="mc-popup-loading">Failed to load cell detail.</div>');
    }
  });
}

function drawBoard(cells, c) {
  if (!cellLayerGroup) return;
  cellLayerGroup.clearLayers();
  if (!Array.isArray(cells)) return;

  cells.forEach((cell) => {
    const color = TEAM_COLORS[cell.owner_team] || '#888';
    const rect = L.rectangle(
      [[cell.south, cell.west], [cell.north, cell.east]],
      { color, weight: 1, fillColor: color, fillOpacity: 0.55 },
    );
    bindCellPopup(rect, cell.cell_id, c);
    cellLayerGroup.addLayer(rect);
  });
}

// Bounding box of every cell the board API returned, using the bounds
// it already computes server-side (app.grid.cell_bounds) -- this module
// never recomputes cell geometry itself, and never decodes a geohash on
// the client for either board any more.
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
// INTO this board (or the initial load) -- never on the 30s auto-refresh
// timer or the Refresh map button, which must not fight the user's own
// panning/zooming.
async function refreshBoard(fitToBoard) {
  const c = cfg();
  try {
    const cells = await fetchBoardCells(c);
    drawBoard(cells, c);
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
    console.warn('board refresh failed:', err);
  }
}

async function refreshScores() {
  const c = cfg();
  try {
    const res = await fetch(c.scoresEndpoint);
    if (!res.ok) return;
    const data = await res.json();
    renderScores(data);
    scoreboardEndsAt = data.ends_at || null;
  } catch (err) {
    console.warn('scores refresh failed:', err);
  }
}

// ===== Mode switching =====

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

function setMode(newMode) {
  mode = newMode === 'meshtastic' ? 'meshtastic' : 'meshcore';
  applyProtocolChrome();
  updateToggleButtons();
  refreshBoard(true);
  refreshScores();
}

// ===== Map bootstrap =====

async function loadConfig() {
  try {
    const res = await fetch('/config');
    if (!res.ok) return null;
    const cfgData = await res.json();
    if (cfgData.centerPos) centerPos = cfgData.centerPos;
    if (typeof cfgData.initialZoom === 'number') initialZoom = cfgData.initialZoom;
    if (typeof cfgData.maxDistanceMiles === 'number') maxDistanceMiles = cfgData.maxDistanceMiles;
    const pa = cfgData.play_area;
    if (
      pa && typeof pa.north === 'number' && typeof pa.south === 'number' &&
      typeof pa.west === 'number' && typeof pa.east === 'number'
    ) {
      playAreaBounds = L.latLngBounds([pa.south, pa.west], [pa.north, pa.east]);
    }
    const raw = String(cfgData.mc_default_view || '').trim().toLowerCase();
    return raw === 'meshtastic' ? 'meshtastic' : 'meshcore';
  } catch (e) {
    return null;
  }
}

async function boot() {
  const defaultMode = (await loadConfig()) || 'meshcore';

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

  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/">CARTO</a>'
  }).addTo(map);

  // #map is sized by CSS (top: var(--mw-nav-h), below the fixed site nav
  // bar) rather than filling the whole viewport, so it's already the
  // right size before L.map() reads it above. Nudge Leaflet to
  // re-measure anyway once this layout pass has settled, so a container
  // size a browser reports late (e.g. a slow web-font swap shifting the
  // bar's height) can never leave the map under- or over-sized.
  requestAnimationFrame(() => map.invalidateSize());

  cellLayerGroup = L.layerGroup().addTo(map);

  scoreboardControl = buildScoreboardControl();
  scoreboardControl.addTo(map);

  renderScores(null); // seed all-zero rows immediately, before the first fetch

  // Territory panel starts collapsed on narrow screens only (phones) --
  // it otherwise eats roughly the top half of a phone screen. Desktop
  // always starts (and stays) expanded; see setMcCollapsed / mc.css.
  if (window.matchMedia(`(max-width: ${NARROW_BREAKPOINT_PX}px)`).matches) {
    setMcCollapsed(true);
  }

  if (maxDistanceMiles > 0) {
    L.circle(centerPos, {
      radius: maxDistanceMiles * 1609.34,
      color: '#555', weight: 1, fill: false, dashArray: '6, 8', opacity: 0.3
    }).addTo(map);
  }

  setMode(defaultMode);

  const loadingOverlay = document.getElementById('loading-overlay');
  if (loadingOverlay) {
    loadingOverlay.classList.add('fade-out');
    setTimeout(() => loadingOverlay.remove(), 500);
  }

  refreshTimer = setInterval(() => {
    refreshBoard(false);
    refreshScores();
  }, REFRESH_INTERVAL_MS);

  setInterval(tickCountdown, 1000);
}

boot().catch((err) => {
  console.error('MeshWars map failed to start:', err);
  const mapDiv = document.getElementById('map');
  if (mapDiv) mapDiv.innerHTML = `<div style="padding: 20px; color: red;">Failed to load map: ${err.message}</div>`;
});
