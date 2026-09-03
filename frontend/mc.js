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
 * /history, /cell/{id}, /teams, /team/{node_ref}, /find, /top, /season
 * -- see app/api.py) while MeshCore's are namespaced under /api/mc/*,
 * but both sides read the exact same mc_season/mc_tile* tables
 * underneath, through app/mc_api.py's protocol-parameterized helpers
 * (board_for, scores_for, history_for, cell_detail_for, find_for,
 * top_for, winner_banner_for, ...) -- see the PROTOCOLS table below for
 * the one-to-one mapping. Every one of those endpoint pairs now returns
 * the identical shape; there is no longer a Meshtastic-only gap this
 * module has to paper over or explain away.
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

// Any name that belongs to a team is written in that team's colour --
// team names and player names alike. The colour IS the team, so it is
// never spelled out beside the name; `title` keeps it recoverable for
// anyone who cannot separate seven colours by eye.
function teamName(name, team) {
  const c = TEAM_COLORS[team];
  if (!c) return escapeHtml(name);
  return `<span class="mc-teamed" style="color:${c}" title="${escapeHtml(team)}">${escapeHtml(name)}</span>`;
}


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

// Basemap key, supplied by the server from its environment (see
// Settings.carto_api_key) and appended to the CARTO tile URL below via
// cartoUrl(). Absent/blank is a fully supported state -- the layer then
// requests tiles exactly as it always did: watermarked but working.
// Same source and same treatment as frontend/map2.js's cartoTiles() and
// frontend/play-area-map.js's cartoUrl().
let cartoApiKey = '';

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
// Endpoint parity -- the full mapping between the two boards' routes,
// all of which now return identical shapes (only the vocabulary and
// the underlying data differ):
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
//                    /teams ({teams:{TEAM:[{display_name}]}}) -- one
//                    entry per player on both, different shape, both
//                    grouped into a common Map<team, [{display_name}]>
//                    by fetchRoster below.
//  - Player Find:   /api/mc/find?name= vs /find?name= -- identical
//                    shape (same find_for() helper on both sides).
//                    Looks a player up BY NAME and returns a bounds box
//                    to zoom to on both boards. See doPlayerFind.
//                    (/team/{node_ref} still exists as a by-NODE-
//                    REFERENCE lookup for anyone who wants it directly,
//                    but the Find control on this panel uses /find on
//                    both boards now, same as MeshCore always has.)
//  - Top-ranked players: /api/mc/top vs /top -- identical shape (same
//                    top_for() helper). Ranks players by capture count
//                    in the active season for whichever board is
//                    showing. See openTopModal.
//  - Top check-in earners: /api/mc/top-checkins vs /top-checkins --
//                    identical shape (same top_checkin_for() helper).
//                    Ranks players by check-in points ("NetOps",
//                    see app/checkin.py) in the active season. This is
//                    a SECOND ranking behind the SAME top-players
//                    button/modal as captures above, not a second
//                    button in a new spot on the panel -- see
//                    openTopModal's own comment for why.
//  - Winner banner: /api/mc/season vs /season -- identical shape (same
//                    winner_banner_for() helper), each `.winner_banner`
//                    seven-team shaped. See refreshWinnerBanner.
//  - Cell popup repeaters/feeders: /api/mc/cell/{id} and /cell/{id}
//                    both now carry a `.repeaters` list (additive to
//                    the existing cell_detail_for() shape) -- real rows
//                    from app/db.py's repeater_observation table,
//                    written by both boards' ingest paths. This module
//                    does not reintroduce the old distance-guessed
//                    repeater-per-tile heuristic: a cell with no
//                    recorded observations gets no section in the
//                    popup at all, never an invented one. See
//                    buildCellPopupHtml.
// =====================================================================
const PROTOCOLS = {
  meshcore: {
    protocol: 'mc',
    // "Score" not "Territory" -- this number is squares held PLUS
    // check-in points PLUS exploration points (see scores_for() in
    // app/mc_api.py), never squares alone, so the heading must not
    // imply it is a territory/square count. See the Breakdown modal
    // (openBreakdownModal) for where that split is actually shown.
    boardTitle: 'MeshCore Score',
    // "Operators" is the umbrella and the tabs are the two activities
    // under it -- which is exactly the model the schema already has: a
    // player is one account with one team, and wardriving and checking
    // in are two things they can do, not two kinds of person (see
    // app/checkin.py). Identical on both boards, so the button never
    // renames itself when you flip protocol.
    topButtonLabel: 'Top Operators',
    topCaptureLabel: 'Wardrivers',
    topCheckinLabel: 'NetOps',
    lookupPlaceholder: 'player name',
    lookupHelp: 'Search by player name.',
    // "Repeaters" is MeshCore's own term for the mesh nodes a wardrive
    // can hear -- see app/db.py's repeater_observation table.
    repeaterLabel: 'Repeaters heard',
    boardEndpoint: '/api/mc/board',
    scoresEndpoint: '/api/mc/scores',
    cellEndpoint: (id) => `/api/mc/cell/${encodeURIComponent(id)}`,
    historyEndpoint: '/api/mc/history',
    rosterEndpoint: '/api/mc/players',
    findEndpoint: (q) => `/api/mc/find?name=${encodeURIComponent(q)}`,
    topEndpoint: '/api/mc/top',
    topCheckinEndpoint: '/api/mc/top-checkins',
    seasonEndpoint: '/api/mc/season',
  },
  meshtastic: {
    protocol: 'mt',
    boardTitle: 'Meshtastic Score',
    // Same three labels as MeshCore, deliberately. "Wardriving" started
    // as MeshCore vocabulary -- MeshMapper is a wardriving app, while a
    // Meshtastic node just broadcasts its own position -- and this board
    // used to say "Operators" for the capture ranking on that reasoning.
    // One vocabulary across both boards is worth more than that
    // distinction: a reader flipping protocol should see the same two
    // activities, not have to learn a second set of words for them.
    topButtonLabel: 'Top Operators',
    topCaptureLabel: 'Wardrivers',
    topCheckinLabel: 'NetOps',
    // Same by-name search as MeshCore now that /find exists for this
    // board too (see app/api.py) -- no more of a node-ID-only Find box.
    lookupPlaceholder: 'player name',
    lookupHelp: 'Search by player name.',
    // Meshtastic's own vocabulary for the same evidence -- packets are
    // relayed onto MQTT by gateway nodes ("feeders"), not repeaters in
    // the MeshCore sense, even though both write the same
    // repeater_observation rows underneath (see app/ingest.py).
    repeaterLabel: 'MQTT feeders heard',
    boardEndpoint: '/get-nodes',
    scoresEndpoint: '/scores',
    cellEndpoint: (id) => `/cell/${encodeURIComponent(id)}`,
    historyEndpoint: '/history',
    rosterEndpoint: '/teams',
    findEndpoint: (q) => `/find?name=${encodeURIComponent(q)}`,
    topEndpoint: '/top',
    topCheckinEndpoint: '/top-checkins',
    seasonEndpoint: '/season',
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
        <div class="mc-row"><a href="#" id="mc-history-link">History</a> &nbsp;|&nbsp; <a href="#" id="mc-roster-link">Roster</a> &nbsp;|&nbsp; <a href="#" id="mc-breakdown-link">Breakdown</a></div>
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
    div.querySelector('#mc-breakdown-link').addEventListener('click', (e) => {
      e.preventDefault();
      openBreakdownModal();
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

// A team's number, wherever it's shown: squares held PLUS check-in
// points PLUS exploration (Places Worth Going) points (app/mc_scoring.py's
// team_totals()) -- the combined figure that actually decides the season
// now, not squares alone. Every display below reads this instead of
// entry.tiles directly, so a team's number means the same thing on the
// scoreboard, the collapsed summary, the history modal, and the winner
// banner. Falls back to tiles-only for the all-zero seed row
// renderScores(null) hands out before the first real fetch (see boot()),
// which has no checkin_points/explorer_points/total fields at all yet.
function teamTotal(t) {
  if (typeof t.total === 'number') return t.total;
  return t.tiles ?? 0;
}

// Human-readable breakdown of teamTotal() above, for the "on demand"
// disclosure (a title/tooltip, not a permanent extra line -- see
// renderScores) of where a team's combined number came from. Always
// states all three components, even when a component is zero, so
// "mysterious" never means "silently rounds to squares" -- the owner's
// explicit ask is that the split stays legible, not merely available
// when non-zero. See also the Breakdown modal (openBreakdownModal) for
// the same three numbers laid out per team rather than one-team-at-a-time.
function teamBreakdown(t) {
  const tiles = t.tiles ?? 0;
  const pts = t.checkin_points ?? 0;
  const explorer = t.explorer_points ?? 0;
  const squareWord = tiles === 1 ? 'square' : 'squares';
  const pointWord = pts === 1 ? 'point' : 'points';
  const explorerWord = explorer === 1 ? 'point' : 'points';
  return `${tiles} ${squareWord} + ${pts} check-in ${pointWord} + ${explorer} exploration ${explorerWord}`;
}

// Short form of teamBreakdown() above, for the actual on-tap/on-click
// swap (see attachBreakdownToggle) rather than the `title` tooltip --
// this has to fit inside one cell of the scoreboard's two-column grid
// alongside a team's dot and label, where the full sentence would
// overflow into the neighboring column. "squares+checkin+exploration",
// no words, in that order to match teamTotal()'s own composition.
function teamBreakdownCompact(t) {
  const tiles = t.tiles ?? 0;
  const pts = t.checkin_points ?? 0;
  const explorer = t.explorer_points ?? 0;
  return `${tiles}+${pts}+${explorer}`;
}

// Wires the click-to-reveal half of the "on demand" breakdown -- `title`
// alone (a native browser tooltip) is not something a person can get to
// on a touch screen, and isn't something a screenshot can ever prove
// shows the right numbers either, so the actual disclosure mechanism is
// a tap/click that swaps the element's own text between the total and
// teamBreakdownCompact()'s split, toggling back on a second tap. `title`
// is kept alongside this as a same-information hover bonus on desktop,
// never the only way to reach the split.
function bindBreakdownToggle(el) {
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    const showingBreakdown = el.classList.toggle('mc-showing-breakdown');
    el.textContent = showingBreakdown ? el.dataset.compact : el.dataset.total;
  });
}

// For DOM-built elements (scoreboard rows, winner banner) that don't
// already carry data-total/data-compact from an HTML string -- sets
// them, seeds the visible text, and wires the same click handler
// bindBreakdownToggle() gives an already-marked-up element (see the
// history modal's .mc-tally-count spans, built from a template string
// instead of appendChild).
function attachBreakdownToggle(el, totalText, compactText) {
  el.dataset.total = totalText;
  el.dataset.compact = compactText;
  el.textContent = totalText;
  bindBreakdownToggle(el);
}

// Picks whichever team currently has the highest combined total, for
// the collapsed-header summary ("TEAM count") on phones -- the scores
// endpoint's teams array is in fixed TEAM_ORDER, not sorted by total,
// so this has to be computed client-side rather than just taking
// teams[0]. Reads teamTotal(), not tiles alone, for the same reason
// every other display on this page does -- see that function's
// docstring.
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
    scoreboardSummaryEl.textContent = lead ? `${lead.team} ${teamTotal(lead)}` : '';
  }

  // Leader first. The API zero-fills all seven teams so a team appearing
  // on the board never shifts the panel (see scores_for()) -- ordering by
  // score deliberately reintroduces movement, because a scoreboard that
  // never reorders makes you read all seven rows to find who is winning.
  // TEAM_ORDER breaks ties so the teams sitting on the same score (often
  // several on zero early in a season) hold a stable position instead of
  // swapping places on every 30s refresh.
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

    // The combined total is the number shown -- squares alone would be
    // misleading now that check-in points can decide a season (see
    // teamTotal()'s docstring). Where that number came from is still
    // one tap away (attachBreakdownToggle), rather than a permanent
    // second line -- with seven teams already in this panel, printing
    // both components on every row every time was the "cluttered"
    // option the owner explicitly asked to avoid.
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
// Both boards look a player up by display name and get back a bounds
// box to zoom to (/api/mc/find vs /find -- identical shape, same
// find_for() helper on the backend). One path for both boards now;
// sets textContent only, never innerHTML.
async function doPlayerFind(value) {
  const c = cfg();
  const resultEl = document.getElementById('mc-lookup-result');
  if (!resultEl) return;
  const query = (value || '').trim();
  if (!query) { resultEl.textContent = ''; return; }
  resultEl.textContent = 'Searching...';

  try {
    const res = await fetch(c.findEndpoint(query));
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
      // Same combined figure as the live scoreboard (teamTotal()) -- a
      // closed season's final standings have to mean the same thing as
      // its live ones did, or "who was actually ahead" would read
      // differently here than it did while the season was still open.
      // Each tally's count is a .mc-tally-count span carrying
      // data-total/data-compact (teamBreakdownCompact()) plus a `title`
      // -- bound to the same click-to-reveal handler as the scoreboard
      // (bindBreakdownToggle, wired up below once this HTML is in the
      // DOM), same on-demand disclosure as the live scoreboard rows.
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
// team. Exists because the scoreboard itself only ever shows the combined
// total (teamTotal()), with the three components one tap/click away one
// team at a time (bindBreakdownToggle) -- this modal is the "all seven
// teams, all three components, at once" view of the exact same numbers,
// reusing the same c.scoresEndpoint fetch and the same
// teamTotal/teamBreakdownCompact helpers so it can never disagree with
// the scoreboard it explains. Same modal chrome as History/Roster/Top
// (openMcModal/closeMcModal) -- no separate open/close logic here.
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

    // Same leader-first ordering as the scoreboard itself (renderScores)
    // -- the row order in this modal should match what a visitor just saw
    // on the panel, not restart from TEAM_ORDER.
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

// Two rankings, one button. The owner's explicit requirement is that
// the panel stays identical between boards -- same controls, same
// positions -- so adding a second ranking (check-in points, alongside
// the existing capture count) cannot mean a second button in a new
// spot. Instead both rankings live behind the SAME mc-top-btn, as two
// tabs inside the one modal it already opened: clicking the button
// never moves or resizes anything on the panel itself, only the modal
// content changes. See PROTOCOLS' topCaptureLabel/topCheckinLabel for
// the tab wording, and top_for()/top_checkin_for() in app/mc_api.py for
// the two backend queries these tabs read.
let topModalTab = 'captures';

function topTabSpecs(c) {
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
      // Only this tab has a second number worth showing. Declared per
      // tab rather than always rendered, so the capture ranking keeps
      // its three columns instead of growing an empty fourth.
      extraKey: 'streak',
      extraHeader: 'Streak',
    },
  };
}

async function renderTopModalTab(c) {
  const tabBody = mcModalBodyEl.querySelector('#mc-top-tab-body');
  if (!tabBody) return;
  const spec = topTabSpecs(c)[topModalTab];
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
      // A null streak is an award written before streaks existed -- show
      // a dash, never a fabricated 1.
      const extra = spec.extraKey
        ? `<td>${escapeHtml(r[spec.extraKey] == null ? '\u2014' : `${r[spec.extraKey]}x`)}</td>`
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
  const c = cfg();
  const specs = topTabSpecs(c);
  topModalTab = 'captures'; // always reopen on the capture ranking
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
      renderTopModalTab(c);
    });
  });
  await renderTopModalTab(c);
}

// ===== Board rendering =====

// Renders detail.repeaters (see PROTOCOLS' comment above, and
// app/mc_api.py's _repeater_observations()) as its own popup section,
// labeled per board (c.repeaterLabel: "Repeaters heard" on MeshCore,
// "MQTT feeders heard" on Meshtastic). Returns '' -- no section at all,
// not an empty-state message -- when the cell has no recorded
// observations, per the owner's explicit call: this must never invent
// or distance-guess a repeater/feeder that ingest never actually
// logged hearing this square.
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
      ${buildRepeaterSectionHtml(detail, c)}
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
      e.popup.setContent(buildCellPopupHtml(cellId, detail, c));
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

// ===== Winner banner =====
//
// A separate top-center overlay on the map itself (#mc-winner-banner in
// index.html), not part of the top-right territory panel -- same
// element, same position, same behavior on both boards; only which
// board's season it reports changes with mode. Reads
// cfg().seasonEndpoint (/api/mc/season vs /season -- identical shape,
// both built by app/mc_api.py's winner_banner_for()) and shows nothing
// at all once the season's display window elapses, same as the backend
// already decides via winner_banner_active/settings.winner_banner_hours
// -- this never has its own separate opinion about when to hide it.
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
    // Each team's dot+count as one flex item (not two separately-gapped
    // ones) so `gap` on .mc-winner-counts spaces teams evenly instead of
    // also prying a dot apart from its own number. The count itself is
    // the combined total (teamTotal()) -- the same figure that decided
    // this winner (see app/mc_scoring.py's maybe_roll_season) -- with
    // its split one tap away (attachBreakdownToggle on the count span
    // specifically, not the whole entry, so tapping the dot doesn't
    // also trigger it), same on-demand disclosure as the scoreboard and
    // history modal.
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
  const c = cfg();
  try {
    const res = await fetch(c.seasonEndpoint);
    if (!res.ok) { renderWinnerBanner(null); return; }
    const data = await res.json();
    renderWinnerBanner(data.winner_banner || null);
  } catch (err) {
    console.warn('winner banner refresh failed:', err);
    renderWinnerBanner(null);
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
  refreshWinnerBanner();
}

// ===== Map bootstrap =====

// The ?key= lands AFTER the .png, so it never collides with the
// {s}/{z}/{x}/{y}/{r} placeholders Leaflet expands in the path -- and it
// introduces no braces of its own (encodeURIComponent cannot emit them),
// which matters because L.Util.template throws on any {placeholder} it
// has no value for. {r} still expands to '@2x' or '' in place, giving
// .../{y}@2x.png?key=... on a retina screen. No key -> the original
// string, untouched: still a working (watermarked) basemap.
function cartoUrl(key) {
  const k = String(key || '').trim();
  const base = 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png';
  return k ? `${base}?key=${encodeURIComponent(k)}` : base;
}

async function loadConfig() {
  try {
    const res = await fetch('/config');
    if (!res.ok) return null;
    const cfgData = await res.json();
    if (cfgData.centerPos) centerPos = cfgData.centerPos;
    if (typeof cfgData.initialZoom === 'number') initialZoom = cfgData.initialZoom;
    if (typeof cfgData.maxDistanceMiles === 'number') maxDistanceMiles = cfgData.maxDistanceMiles;
    cartoApiKey = String(cfgData.carto_api_key || '').trim();
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

  L.tileLayer(cartoUrl(cartoApiKey), {
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
    refreshWinnerBanner();
  }, REFRESH_INTERVAL_MS);

  setInterval(tickCountdown, 1000);
}

boot().catch((err) => {
  console.error('MeshWars map failed to start:', err);
  const mapDiv = document.getElementById('map');
  if (mapDiv) mapDiv.innerHTML = `<div style="padding: 20px; color: red;">Failed to load map: ${err.message}</div>`;
});
