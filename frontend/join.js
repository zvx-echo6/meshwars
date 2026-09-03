/*
 * MeshWars: join page (/join).
 *
 * Self-contained -- no external libraries, no framework, nothing beyond
 * what the browser provides. Talks to POST /api/join, POST /api/mc/status,
 * and GET /config.
 *
 * The page is one continuous numbered flow (#join-flow-panel): each
 * instruction sits directly above the control it describes, from
 * entering an invite code through starting a wardriving session. The
 * steps after "Click Join" are visible before registering, showing a
 * placeholder in place of the key, and get filled in with the real key
 * in place once /api/join succeeds -- nothing is hidden or navigated
 * away.
 *
 * SECURITY: display names and every message the server returns are
 * untrusted. Every dynamic value rendered on this page is set via
 * textContent, an element's .value, or a CSS custom property with a
 * value this file validated itself (TEAM_COLORS lookups) -- never via
 * innerHTML/insertAdjacentHTML with anything other than a literal
 * string written in this file.
 *
 * The status-check API key never leaves this file except in the
 * X-API-Key header of a request itself (/api/mc/status, /api/nodes for
 * the radio list/add/remove calls, and /api/team for the switch-team
 * control, all below) -- it is never put in a URL or request body,
 * never persisted (no localStorage/sessionStorage/cookie), and never
 * logged. Every one of those calls reads it fresh from #f-status-key
 * rather than caching it in a variable, same reasoning: there is
 * already exactly one place this key lives on the page, and it should
 * stay that way.
 */

// Same roster/colors as frontend/mc.js -- duplicated rather than
// imported, since this page must stay self-contained and load
// independently of the board view.
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

// Labels for the /api/mc/status counters table -- key order here is
// display order, matching the field names _counters_out() in
// app/mc_api.py returns.
const MC_COUNTER_LABELS = [
  ['batches', 'Batches received'],
  ['accepted', 'Accepted'],
  ['no_contact', 'No contact key'],
  ['wrong_owner', 'Wrong owner'],
  ['duplicate', 'Duplicate'],
  ['bad_coord', 'Bad coordinates'],
  ['out_of_area', 'Outside play area'],
  ['no_repeaters', 'No repeaters heard'],
];
const MC_COUNTER_ZERO_ROW = MC_COUNTER_LABELS.reduce((acc, [key]) => {
  acc[key] = 0;
  return acc;
}, {});

// Same idea for data.mt.today/last_7_days, matching _counters_out_mt()
// in app/mc_api.py -- deliberately a shorter list than MC_COUNTER_LABELS
// above. There is no Meshtastic "batches received" (no batch concept at
// all), "no contact key" (nothing to attribute here to begin with), or
// "wrong owner" (player_node's (protocol, node_ref) key already makes
// ownership 1:1) -- the backend omits those three from data.mt for the
// same reason: rendering them would just be a column of counters that
// can never be anything but zero, which is the exact problem this whole
// panel update exists to remove. Do not add them back here without also
// adding them back on the backend.
const MT_COUNTER_LABELS = [
  ['accepted', 'Accepted'],
  ['duplicate', 'Duplicate'],
  ['bad_coord', 'Bad position'],
  ['out_of_area', 'Outside play area'],
  ['no_repeaters', 'No feeder heard'],
];
const MT_COUNTER_ZERO_ROW = MT_COUNTER_LABELS.reduce((acc, [key]) => {
  acc[key] = 0;
  return acc;
}, {});

// Display labels for the "Your radios" list and the add-radio protocol
// select -- keys match exactly what the server uses everywhere else
// (player_node.protocol, app/nodes_api.py's _VALID_PROTOCOLS).
const PROTOCOL_LABELS = { mt: 'Meshtastic', mc: 'MeshCore' };

// Every node_ref the server sends back (GET/POST/DELETE /api/nodes,
// POST /api/mc/status) is bare lowercase 8-hex -- that's player_node's
// one canonical storage/lookup form (see app/node_ref.py's module
// docstring), not a display form. Rendering it idiomatically per
// protocol is this file's job, not the server's: Meshtastic writes a
// node id as `!a1b2c3d4` everywhere in its own app/docs/ecosystem, so
// that's what shows here; MeshCore's MeshMapper shows a contact key
// bare, so that stays as-is. Display-only -- this value is never sent
// back to the server; every request still uses the raw node_ref field.
function displayNodeRef(protocol, nodeRef) {
  return protocol === 'mt' ? `!${nodeRef}` : nodeRef;
}

// ---- Node picker (protocol step of the join form) ------------------------
//
// Searchable pickers over GET /api/checkin/mc/nodes and
// GET /api/checkin/mt/nodes (app/checkin_api.py) -- both public, no key
// required, so a node picked here travels straight into the join form
// itself: for Meshtastic with no public key entered, it becomes
// /api/join's own optional node_ref field (unchanged); with one
// entered, or for MeshCore -- which /api/join has never accepted a
// node_ref for, since a MeshCore radio normally self-binds from a
// wardriving ping's contact key -- it becomes a follow-up POST
// /api/nodes call in handleJoinClick() below (bindPickedMtNode() /
// bindPickedMcNode()), using the key /api/join just returned. Either
// way the player never has to paste a key of their own just to make a
// pick.
//
// Replaces free-text node ID entry because the lists are genuinely long
// (800+ Meshtastic entries) and, on both protocols, can contain more
// than one node sharing the same name -- NODE_PICKER_MAX_RESULTS below
// caps how many of a broad search's matches render at once, but never
// collapses or hides a duplicate; distinguishing two same-named entries
// by their node_ref is left to the person looking at them, same as
// app/checkin.py's own directory bridge refuses to guess between them.
//
// Fetched at most ONCE per protocol per page load, on first focus of
// the search box -- not polled, not re-fetched per keystroke. Filtering
// after that is entirely client-side against the cached list, which is
// why there is no debounce here: there is never a request in flight to
// debounce against.
const NODE_PICKER_ENDPOINTS = { mc: '/api/checkin/mc/nodes', mt: '/api/checkin/mt/nodes' };
const NODE_PICKER_MAX_RESULTS = 40;

// Precomputed once per node when the list is fetched, not per
// keystroke: name, short name, the bare node_ref, and the
// protocol-appropriate display form, so typing the leading "!"
// Meshtastic shows still matches a Meshtastic entry.
function nodePickerHaystack(protocol, node) {
  return [
    node.name || '',
    node.short_name || '',
    node.node_ref || '',
    displayNodeRef(protocol, node.node_ref || ''),
  ].join(' ').toLowerCase();
}

// Owner-specified two-line entry format: "Node Long Name (Short Name)"
// over the node ID. short_name is already normalized to null (never
// empty string) by both app/checkin.py shaping functions, so a plain
// truthiness check is enough to decide whether to render the
// parenthetical at all -- never "Name ()" for a node with no short name.
function nodePickerEntryLabel(node) {
  return node.short_name ? `${node.name} (${node.short_name})` : node.name;
}

// onSelect is optional and fires only from a picker click (never manual
// entry, which has nothing to fire it with) -- today only the 'mt'
// picker passes one, to auto-fill the public key field from the
// picked entry's public_key (GET /api/checkin/mt/nodes now carries one
// when mt_node_key has exactly one distinct key on file for that node).
//
// `idPrefix` names the markup block this instance drives, defaulting to
// the protocol so the two join-form pickers keep the ids they always
// had. The add-a-radio picker at the bottom of the page is a SECOND
// instance of each protocol on the same document -- two blocks reading
// the same '/api/checkin/mt/nodes' list can't both answer to
// `mt-node-picker-*`, so they use 'add-mt' / 'add-mc' instead. Nothing
// else about them differs: same fetch, same filter, same manual-entry
// escape hatch, same markup. (Issue #3 -- the add-radio form used to be
// a bare text input, so a player who found their node by searching at
// signup had to go dig out a hex ID to add a second radio.)
function createNodePicker(protocol, onSelect, idPrefix = protocol) {
  const els = {
    searchWrap: document.getElementById(`${idPrefix}-node-picker-search`),
    searchInput: document.getElementById(`f-${idPrefix}-node-search`),
    manualLink: document.getElementById(`${idPrefix}-node-manual-link`),
    results: document.getElementById(`${idPrefix}-node-picker-results`),
    status: document.getElementById(`${idPrefix}-node-picker-status`),
    selectedWrap: document.getElementById(`${idPrefix}-node-picker-selected`),
    selectedName: document.getElementById(`${idPrefix}-node-picker-selected-name`),
    selectedRef: document.getElementById(`${idPrefix}-node-picker-selected-ref`),
    changeBtn: document.getElementById(`${idPrefix}-node-picker-change`),
    manualWrap: document.getElementById(`${idPrefix}-node-picker-manual`),
    manualInput: document.getElementById(`f-${idPrefix}-node-ref`),
    searchLink: document.getElementById(`${idPrefix}-node-search-link`),
  };
  // Defensive only -- both protocol blocks always carry this markup
  // today, but a picker instance for a block that isn't on the page
  // should degrade to "nothing picked" rather than throw.
  if (!els.searchWrap || !els.selectedWrap || !els.manualWrap) {
    return { getValue: () => null, reset: () => {} };
  }

  let nodes = null; // null = not fetched yet; [] = fetched empty, or fetch failed
  let fetching = null; // in-flight fetch promise, so a fast re-focus can't double-fire it
  let selectedNode = null;
  let mode = 'search'; // 'search' | 'selected' | 'manual'

  function setMode(next) {
    mode = next;
    els.searchWrap.hidden = mode !== 'search';
    els.selectedWrap.hidden = mode !== 'selected';
    els.manualWrap.hidden = mode !== 'manual';
  }

  function renderResults(query) {
    els.results.replaceChildren();
    const trimmed = query.trim().toLowerCase();
    if (!trimmed) {
      // Nothing typed yet -- stay quiet rather than dumping the entire
      // roster (800+ entries on Meshtastic) into the page unfiltered.
      els.results.hidden = true;
      els.status.hidden = true;
      return;
    }
    if (nodes === null) {
      els.results.hidden = true;
      els.status.textContent = 'Loading nodes…';
      els.status.hidden = false;
      return;
    }
    const matches = nodes.filter((n) => n._haystack.includes(trimmed));
    if (matches.length === 0) {
      els.results.hidden = true;
      els.status.textContent = nodes.length === 0
        ? "Couldn't load the node list right now."
        : 'No matches for that search.';
      els.status.hidden = false;
      return;
    }
    const shown = matches.slice(0, NODE_PICKER_MAX_RESULTS);
    shown.forEach((node) => {
      const li = document.createElement('li');
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'node-picker-result';
      const nameSpan = document.createElement('span');
      nameSpan.className = 'node-picker-result-name';
      nameSpan.textContent = nodePickerEntryLabel(node);
      const refSpan = document.createElement('span');
      refSpan.className = 'node-picker-result-ref';
      refSpan.textContent = displayNodeRef(protocol, node.node_ref);
      btn.appendChild(nameSpan);
      btn.appendChild(refSpan);
      btn.addEventListener('click', () => select(node));
      li.appendChild(btn);
      els.results.appendChild(li);
    });
    els.results.hidden = false;
    if (matches.length > NODE_PICKER_MAX_RESULTS) {
      els.status.textContent = `Showing ${NODE_PICKER_MAX_RESULTS} of ${matches.length} matches — keep typing to narrow it down.`;
      els.status.hidden = false;
    } else {
      els.status.hidden = true;
    }
  }

  function select(node) {
    selectedNode = node;
    els.selectedName.textContent = nodePickerEntryLabel(node);
    els.selectedRef.textContent = displayNodeRef(protocol, node.node_ref);
    setMode('selected');
    if (onSelect) onSelect(node);
  }

  function ensureFetched() {
    if (nodes !== null || fetching) return fetching;
    fetching = fetch(NODE_PICKER_ENDPOINTS[protocol])
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error(String(res.status)))))
      .then((data) => {
        const list = Array.isArray(data && data.nodes) ? data.nodes : [];
        list.forEach((n) => { n._haystack = nodePickerHaystack(protocol, n); });
        nodes = list;
      })
      .catch(() => {
        // Rate limited, offline, upstream down -- whatever it is, the
        // picker just comes up empty. Manual entry (always available,
        // see els.manualLink) is never blocked by this.
        nodes = [];
      })
      .finally(() => {
        fetching = null;
        renderResults(els.searchInput.value);
      });
    return fetching;
  }

  els.searchInput.addEventListener('focus', ensureFetched);
  els.searchInput.addEventListener('input', () => {
    ensureFetched();
    renderResults(els.searchInput.value);
  });
  els.manualLink.addEventListener('click', () => setMode('manual'));
  els.changeBtn.addEventListener('click', () => {
    selectedNode = null;
    els.searchInput.value = '';
    setMode('search');
    renderResults('');
    els.searchInput.focus();
  });
  if (els.searchLink) {
    els.searchLink.addEventListener('click', () => {
      els.manualInput.value = '';
      setMode('search');
      els.searchInput.focus();
    });
  }

  return {
    // The value handleJoinClick() actually submits: the picked node's
    // canonical node_ref in 'selected' mode, the raw typed value in
    // 'manual' mode (server-side normalize_node_ref validates it same
    // as always), or null in 'search' mode -- an in-progress, uncommitted
    // search is never treated as a value, so registering with no radio
    // at all still works exactly as before.
    getValue() {
      if (mode === 'selected' && selectedNode) return selectedNode.node_ref;
      if (mode === 'manual') {
        const v = els.manualInput.value.trim();
        return v ? v : null;
      }
      return null;
    },

    // Back to an untouched search box, keeping the fetched node list
    // (that's a page-load-lifetime cache, not part of the picked
    // value). Used by the add-a-radio form after a successful add, the
    // same way it used to clear its text input -- someone adding two
    // radios in a row must not find the first one still sitting there
    // looking like it is about to be submitted again. The join-form
    // pickers never call this: that form is submitted once.
    reset() {
      selectedNode = null;
      els.searchInput.value = '';
      els.manualInput.value = '';
      setMode('search');
      renderResults('');
    },
  };
}

let mcPicker = null;
let mtPicker = null;

// The same two pickers again, for the add-a-radio form in the
// setup-check panel at the bottom of the page. Separate instances
// rather than the join-form ones moved around: the two forms are on
// screen at the same time, submit independently, and a pick in one must
// never show up as a pick in the other.
let addMcPicker = null;
let addMtPicker = null;

// Which of the two add-form pickers the protocol <select> currently has
// showing -- the one handleAddRadioSubmit() reads a node_ref out of.
function activeAddPicker() {
  return document.getElementById('f-add-protocol').value === 'mc' ? addMcPicker : addMtPicker;
}

// The protocol <select> shows one picker block and hides the other.
// Whatever was picked in the block being hidden is cleared on the way
// out: leaving it set would mean flipping the protocol back and forth
// silently re-arms a node the player has visibly moved on from, and the
// submit handler only ever reads the visible one anyway.
function setupAddProtocolToggle() {
  const select = document.getElementById('f-add-protocol');
  const blocks = {
    mc: document.getElementById('add-mc-node-picker'),
    mt: document.getElementById('add-mt-node-picker'),
  };
  if (!select || !blocks.mc || !blocks.mt) return;
  select.addEventListener('change', () => {
    const chosen = select.value === 'mc' ? 'mc' : 'mt';
    const leaving = chosen === 'mc' ? addMtPicker : addMcPicker;
    if (leaving) leaving.reset();
    blocks.mc.hidden = chosen !== 'mc';
    blocks.mt.hidden = chosen !== 'mt';
  });
}

let selectedTeam = null;

// The full last-known /api/mc/status response for the currently
// checked-out key, kept around so the radios list and the status
// panel above it (name/team/MeshCore diagnosis/Meshtastic note/"no
// radios yet" line) can never drift apart -- see applyRadiosUpdate()
// below. Both panels render from this ONE object; there is no second,
// independent notion of "what radios does this player have" anywhere
// on the page.
let lastStatusData = null;

function showError(message) {
  const el = document.getElementById('join-error');
  el.textContent = message;
  el.hidden = false;
}

function clearError() {
  const el = document.getElementById('join-error');
  el.textContent = '';
  el.hidden = true;
}

function buildTeamPicker() {
  const wrap = document.getElementById('team-picker');
  wrap.replaceChildren();
  TEAM_ORDER.forEach((team) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'team-swatch';
    btn.style.setProperty('--swatch-color', TEAM_COLORS[team]);
    btn.textContent = team;
    btn.addEventListener('click', () => {
      selectedTeam = team;
      wrap.querySelectorAll('.team-swatch').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
    });
    wrap.appendChild(btn);
  });
}

// Every place on this page whose content depends on the selected
// protocol -- the registration-block pair, the join-flow's protocol-only
// steps (see join.html's "PROTOCOL-SPECIFIC JOIN STEPS" comment), and
// the "how do I know it's working?" panel -- toggles together off this
// one radio group, so a visitor never sees mismatched MeshCore/Meshtastic
// copy on the same view of the page.
function setupProtocolToggle() {
  const radios = document.querySelectorAll('input[name="protocol"]');
  const mcBlock = document.getElementById('mc-instructions');
  const mtBlock = document.getElementById('mt-instructions');
  const workingMc = document.getElementById('working-check-mc');
  const workingMt = document.getElementById('working-check-mt');
  const statusHintMc = document.getElementById('status-hint-mc');
  const statusHintMt = document.getElementById('status-hint-mt');
  const stepsMc = document.querySelectorAll('.proto-step-mc');
  const stepsMt = document.querySelectorAll('.proto-step-mt');
  function apply() {
    const checked = document.querySelector('input[name="protocol"]:checked');
    const value = checked ? checked.value : 'mc';
    mcBlock.hidden = value !== 'mc';
    mtBlock.hidden = value !== 'mt';
    if (workingMc) workingMc.hidden = value !== 'mc';
    if (workingMt) workingMt.hidden = value !== 'mt';
    if (statusHintMc) statusHintMc.hidden = value !== 'mc';
    if (statusHintMt) statusHintMt.hidden = value !== 'mt';
    stepsMc.forEach((li) => { li.hidden = value !== 'mc'; });
    stepsMt.forEach((li) => { li.hidden = value !== 'mt'; });
  }
  radios.forEach((r) => r.addEventListener('change', apply));
  apply();
}

// GET /config exposes join_meshtastic_enabled. The Meshtastic radio
// ships disabled in the HTML (fail-safe default matching the server's
// own default), and is only enabled here once the server confirms
// registration is actually open -- so a slow/failed config fetch never
// shows Meshtastic as available when it isn't.
async function applyMeshtasticAvailability() {
  let enabled = false;
  try {
    const res = await fetch('/config');
    if (res.ok) {
      const cfg = await res.json();
      enabled = cfg && cfg.join_meshtastic_enabled === true;
    }
  } catch (err) {
    enabled = false;
  }
  if (!enabled) return;

  const mtRadio = document.getElementById('f-protocol-mt');
  mtRadio.disabled = false;
  document.getElementById('mt-protocol-choice').classList.remove('protocol-choice-disabled');
  document.getElementById('mt-badge').hidden = true;
  document.getElementById('mt-coming-soon-hint').hidden = true;
}

// GET /config includes join_invite_code only when the owner has turned
// join_invite_code_public on AND a code is actually configured -- absent
// (not empty, not null) in every other case. When present, step 1 is
// extended to name the code so a person can read and type it in; the
// input itself is never prefilled and there is no copy button here, on
// purpose, so typing it stays a human action. Rendered with textContent,
// never innerHTML, same rule as the rest of this file.
async function applyInviteCodeHint() {
  const label = document.getElementById('invite-step-label');
  if (!label) return;
  try {
    const res = await fetch('/config');
    if (!res.ok) return;
    const cfg = await res.json();
    const code = cfg && typeof cfg.join_invite_code === 'string' ? cfg.join_invite_code : null;
    if (code) {
      label.textContent = `Enter ${code} as the invite code in the box below.`;
    }
  } catch (err) {
    // Leave the default label text in place.
  }
}

function copyToClipboard(text, button) {
  const original = button.textContent;
  const revert = () => { button.textContent = original; };
  navigator.clipboard.writeText(text).then(() => {
    button.textContent = 'Copied';
    setTimeout(revert, 1500);
  }).catch(() => {
    button.textContent = 'Copy failed';
    setTimeout(revert, 1500);
  });
}

function buildCopyRow(value) {
  const row = document.createElement('div');
  row.className = 'copy-row';

  const input = document.createElement('input');
  input.type = 'text';
  input.readOnly = true;
  input.value = value;

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.textContent = 'Copy';
  btn.addEventListener('click', () => copyToClipboard(value, btn));

  row.appendChild(input);
  row.appendChild(btn);
  return row;
}

// The endpoint URL step (step "Paste this as the URL") is the same for
// everybody and known before registering, so its copy row is plain
// markup in join.html rather than built here -- this just wires the
// existing input's value into the same copyToClipboard() helper every
// other copy button on this page uses.
function setupEndpointCopy() {
  const btn = document.getElementById('endpoint-copy-btn');
  const input = document.getElementById('f-endpoint-url');
  if (!btn || !input) return;
  btn.addEventListener('click', () => copyToClipboard(input.value, btn));
}

// Fills the "copy your key" and "paste your key" steps in place with
// the real key, replacing the "your key will appear here once you
// join" placeholder -- both slots show the same key, since both steps
// need the player to act on it (copy it once, paste it once).
function fillKeySlots(key) {
  ['key-slot', 'key-slot-2'].forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.replaceChildren(buildCopyRow(key));
  });
}

function showJoinSuccess(data) {
  const el = document.getElementById('join-success');
  el.replaceChildren();
  el.appendChild(document.createTextNode('Registered as '));
  const nameStrong = document.createElement('strong');
  nameStrong.textContent = data.display_name;
  // Your own name, in your team's colour -- the same rule the map and
  // the results page follow.
  nameStrong.style.color = TEAM_COLORS[data.team] || 'inherit';
  el.appendChild(nameStrong);
  el.appendChild(document.createTextNode(' on '));
  const dot = document.createElement('span');
  dot.className = 'mc-dot-inline';
  dot.style.background = TEAM_COLORS[data.team] || '#888';
  el.appendChild(dot);
  el.appendChild(document.createTextNode(data.team + '.'));
  el.hidden = false;
}

function showNodeWarning(message) {
  const el = document.getElementById('join-node-warning');
  if (!el) return;
  el.textContent = message;
  el.hidden = false;
}

function hideNodeWarning() {
  const el = document.getElementById('join-node-warning');
  if (!el) return;
  el.hidden = true;
  el.textContent = '';
}

// /api/join has never taken a node_ref for protocol 'mc' -- a MeshCore
// radio normally self-binds from its first wardriving ping's contact
// key, so there was never a field for one here before the node picker
// added one. Binding a pick/manual entry from that picker is therefore
// a second call, using the key /api/join's own response just returned
// -- the exact same POST /api/nodes app/nodes_api.py's key-authenticated
// radio management already exposes, called here automatically as part
// of the SAME "press Join" action, so registering and attaching the
// radio read as one thing to the person doing it, not a second errand
// with their own key. Registration itself has already succeeded by the
// time this runs, so a failure here is never reported as a failed
// registration -- see handleJoinClick()'s call site for how the message
// below is shown without touching join-error or the key display: the
// account exists and the key on screen is still exactly what fixes
// this, so nothing about that key's visibility or the success state
// above it may be hidden or thrown away over a bind failure.
async function bindPickedMcNode(key, nodeRef) {
  try {
    const res = await fetch('/api/nodes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-API-Key': key },
      body: JSON.stringify({ node_ref: nodeRef, protocol: 'mc' }),
    });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      const detail = (data && typeof data.error === 'string') ? data.error : `status ${res.status}`;
      return {
        ok: false,
        message: `Your account was created. The radio you picked was NOT attached to it (${detail}). Your key above still works -- paste it into the setup-check panel further down this page to add the radio there.`,
      };
    }
    return { ok: true };
  } catch (err) {
    return {
      ok: false,
      message: "Your account was created. The radio you picked was NOT attached to it -- the server couldn't be reached to do it. Your key above still works -- paste it into the setup-check panel further down this page to add the radio there.",
    };
  }
}

// Same shape and same reasoning as bindPickedMcNode() just above, but
// only ever called when a public key was entered for a Meshtastic
// registration. Without one, /api/join's own optional node_ref field
// still does the binding in the same request as before (unchanged) --
// there's nothing else worth a second call for. WITH one, /api/join is
// asked to register with no node_ref at all (see handleJoinClick()) and
// this follow-up POST /api/nodes call -- the one place public_key
// validation lives (app/nodes_api.py) -- does the binding instead, so
// the key travels in the exact same request as the node_ref it belongs
// to rather than requiring a schema change to /api/join just to carry
// one extra optional field.
async function bindPickedMtNode(key, nodeRef, publicKey) {
  try {
    const res = await fetch('/api/nodes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-API-Key': key },
      body: JSON.stringify({ node_ref: nodeRef, protocol: 'mt', public_key: publicKey }),
    });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      const detail = (data && typeof data.error === 'string') ? data.error : `status ${res.status}`;
      return {
        ok: false,
        message: `Your account was created. The radio you picked was NOT attached to it (${detail}). Your key above still works -- paste it into the setup-check panel further down this page to add the radio there.`,
      };
    }
    return { ok: true };
  } catch (err) {
    return {
      ok: false,
      message: "Your account was created. The radio you picked was NOT attached to it -- the server couldn't be reached to do it. Your key above still works -- paste it into the setup-check panel further down this page to add the radio there.",
    };
  }
}

async function handleJoinClick() {
  clearError();
  hideNodeWarning();

  const invite = document.getElementById('f-invite').value;
  const name = document.getElementById('f-name').value;
  const checkedProtocol = document.querySelector('input[name="protocol"]:checked');
  const protocol = checkedProtocol ? checkedProtocol.value : 'mc';
  const mtNodeRef = mtPicker ? mtPicker.getValue() : null;
  const mcNodeRef = mcPicker ? mcPicker.getValue() : null;
  const mtPublicKeyInput = document.getElementById('f-mt-public-key');
  const mtPublicKeyRaw = mtPublicKeyInput ? mtPublicKeyInput.value.trim() : '';
  // Only meaningful alongside a node -- a key with nothing to bind it
  // to is never sent.
  const mtPublicKey = (mtNodeRef && mtPublicKeyRaw) ? mtPublicKeyRaw : null;

  if (!selectedTeam) {
    showError('Choose a team.');
    return;
  }

  const body = {
    invite_code: invite,
    display_name: name,
    team: selectedTeam,
    protocol,
  };
  // With no public key, /api/join's own optional node_ref field still
  // does the binding in the same request, exactly as before. With one,
  // node_ref is left off here on purpose and the follow-up
  // bindPickedMtNode() call below does the binding instead -- see that
  // function's comment for why: public_key validation lives in
  // app/nodes_api.py's POST /api/nodes, not /api/join.
  if (protocol === 'mt' && mtNodeRef && !mtPublicKey) {
    body.node_ref = mtNodeRef;
  }

  const submitBtn = document.getElementById('join-submit');
  submitBtn.disabled = true;
  try {
    const res = await fetch('/api/join', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      const message = (data && typeof data.error === 'string')
        ? data.error
        : `Registration failed (status ${res.status}).`;
      showError(message);
      submitBtn.disabled = false;
      return;
    }
    showJoinSuccess(data);
    fillKeySlots(data.key);

    if (protocol === 'mc' && mcNodeRef) {
      const bindResult = await bindPickedMcNode(data.key, mcNodeRef);
      if (!bindResult.ok) {
        showNodeWarning(bindResult.message);
      }
    }
    if (protocol === 'mt' && mtNodeRef && mtPublicKey) {
      const bindResult = await bindPickedMtNode(data.key, mtNodeRef, mtPublicKey);
      if (!bindResult.ok) {
        showNodeWarning(bindResult.message);
      }
    }
    // Registration is one-time -- leave the button disabled rather than
    // re-enabling it, so a second click can't try to join again.
  } catch (err) {
    showError('Could not reach the server. Check your connection and try again.');
    submitBtn.disabled = false;
  }
}

// ---- Check my setup (POST /api/mc/status) --------------------------------

function showStatusError(message) {
  const el = document.getElementById('status-error');
  el.textContent = message;
  el.hidden = false;
}

function clearStatusError() {
  const el = document.getElementById('status-error');
  el.textContent = '';
  el.hidden = true;
}

// Mirrors the phrasing of _relative_time() in app/mc_api.py, which is
// only ever seen embedded inside the diagnosis sentence -- this is the
// same wording surfaced as its own "last heard from" field.
function relativeTimeFromEpoch(ts) {
  if (!ts) return 'never';
  const deltaSec = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (deltaSec < 60) return 'just now';
  const minutes = Math.floor(deltaSec / 60);
  if (minutes < 60) return `${minutes} minute${minutes !== 1 ? 's' : ''} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hour${hours !== 1 ? 's' : ''} ago`;
  const days = Math.floor(hours / 24);
  return `${days} day${days !== 1 ? 's' : ''} ago`;
}

// Server-provided next_switch_at (a real unix timestamp, always the
// end of the current month window in settings.checkin_net_timezone --
// see GET /api/team's docstring in app/join_api.py, which returns it
// in both the available and locked states now, so this file never has
// to guess at it). Rendered with an explicit timeZone: 'America/Boise'
// rather than the viewer's local zone -- ts is midnight in that zone,
// so a viewer west of Mountain time (Pacific, Alaska, Hawaii) would
// otherwise see that instant fall on the previous day and be told they
// can switch a day sooner than they actually can. Pinning the zone
// here is fine precisely because this is display-only: it renders a
// timestamp the server already computed, it does not recompute when a
// month begins the way the deleted estimateNextSwitchLabel() used to.
function formatSwitchDate(ts) {
  if (!ts) return 'unknown';
  try {
    return new Date(ts * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric', timeZone: 'America/Boise' });
  } catch (e) {
    return 'unknown';
  }
}

function buildCountersTable(today, week, labels) {
  const table = document.createElement('table');
  table.className = 'status-table';

  const thead = document.createElement('thead');
  const headRow = document.createElement('tr');
  ['', 'Today', 'Last 7 days'].forEach((h) => {
    const th = document.createElement('th');
    th.textContent = h;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  labels.forEach(([key, label]) => {
    const tr = document.createElement('tr');
    const rowHead = document.createElement('th');
    rowHead.scope = 'row';
    rowHead.textContent = label;
    tr.appendChild(rowHead);
    [today, week].forEach((row) => {
      const td = document.createElement('td');
      td.textContent = String((row && row[key]) ?? 0);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  return table;
}

function buildLabel(text) {
  const label = document.createElement('div');
  label.className = 'join-panel-subtitle';
  label.textContent = text;
  return label;
}

// /api/mc/status (app/mc_api.py) reports both protocols now: the
// top-level fields (last_batch_at, today, last_7_days, squares_held,
// diagnosis) stay MeshCore-scoped exactly as before, and everything
// Meshtastic-specific lives under the additive data.mt key, built from
// its own protocol='mt' queries and its own _diagnose_mt() -- see that
// function's docstring in app/mc_api.py for why the two boards can't
// share one diagnosis (pushed batches vs. polled packets, genuinely
// different failure modes). This function still decides what to show
// from data.radios (which protocols this player actually has
// registered), not from whether the fields are present -- the server
// always returns both blocks, so a MeshCore-only player would otherwise
// see a Meshtastic section reporting "never picked up a position packet"
// for a radio they don't own.
function renderStatusResult(data) {
  // Cache the full response -- applyRadiosUpdate() below reuses it (with
  // just .radios swapped in) so an add/remove never has to render the
  // radios list from a different, narrower object than the diagnostic
  // text sitting above it.
  lastStatusData = data;

  const panel = document.getElementById('status-result');
  panel.replaceChildren();
  panel.hidden = false;

  const radios = Array.isArray(data.radios) ? data.radios : [];
  const hasMc = radios.some((r) => r.protocol === 'mc');
  const hasMt = radios.some((r) => r.protocol === 'mt');

  const nameLine = document.createElement('p');
  nameLine.appendChild(document.createTextNode('Name: '));
  const nameStrong = document.createElement('strong');
  nameStrong.textContent = data.display_name;
  // Your own name, in your team's colour -- the same rule the map and
  // the results page follow.
  nameStrong.style.color = TEAM_COLORS[data.team] || 'inherit';
  nameLine.appendChild(nameStrong);
  panel.appendChild(nameLine);

  const teamLine = document.createElement('p');
  teamLine.appendChild(document.createTextNode('Team: '));
  const dot = document.createElement('span');
  dot.className = 'mc-dot-inline';
  dot.style.background = TEAM_COLORS[data.team] || '#888';
  teamLine.appendChild(dot);
  teamLine.appendChild(document.createTextNode(data.team || ''));
  panel.appendChild(teamLine);

  if (hasMc) {
    // Everything below this point is genuinely MeshCore-scoped data, so
    // only show it to a player who actually has a MeshCore radio.
    const lastHeardLine = document.createElement('p');
    lastHeardLine.appendChild(document.createTextNode('Last MeshCore batch: '));
    const lastHeardStrong = document.createElement('strong');
    lastHeardStrong.textContent = relativeTimeFromEpoch(data.last_batch_at);
    lastHeardLine.appendChild(lastHeardStrong);
    panel.appendChild(lastHeardLine);

    const code = data.diagnosis && data.diagnosis.code;
    const diagnosis = document.createElement('div');
    diagnosis.className = 'status-diagnosis ' + (code === 'ok' ? 'status-diagnosis-ok' : 'status-diagnosis-attention');
    diagnosis.textContent = (data.diagnosis && data.diagnosis.message) || '';
    panel.appendChild(diagnosis);

    const squaresLine = document.createElement('p');
    squaresLine.appendChild(document.createTextNode('MeshCore squares held: '));
    const squaresStrong = document.createElement('strong');
    squaresStrong.textContent = String(data.squares_held ?? 0);
    squaresLine.appendChild(squaresStrong);
    panel.appendChild(squaresLine);

    panel.appendChild(buildLabel('MeshCore batches: today and last 7 days'));
    panel.appendChild(buildCountersTable(data.today || MC_COUNTER_ZERO_ROW, data.last_7_days || MC_COUNTER_ZERO_ROW, MC_COUNTER_LABELS));
  }

  if (hasMt) {
    // Real per-protocol data now (see app/mc_api.py's mc_status()
    // docstring) -- data.mt, not a note apologizing that it doesn't
    // exist. Sits in its own block, clearly labeled, so a player who
    // holds both kinds of radio (the whole reason this branch exists)
    // can tell at a glance which numbers below belong to which radio,
    // the same way the MeshCore block above is already labeled.
    const mt = data.mt || {};

    if (hasMc) {
      // Both protocols on one player: separate the two sections instead
      // of letting the Meshtastic numbers read as a continuation of the
      // MeshCore ones just above them.
      const divider = document.createElement('hr');
      divider.className = 'status-protocol-divider';
      panel.appendChild(divider);
    }

    panel.appendChild(buildLabel('Meshtastic'));

    const lastHeardLine = document.createElement('p');
    lastHeardLine.appendChild(document.createTextNode('Last picked up from meshview: '));
    const lastHeardStrong = document.createElement('strong');
    lastHeardStrong.textContent = relativeTimeFromEpoch(mt.last_heard_at);
    lastHeardLine.appendChild(lastHeardStrong);
    panel.appendChild(lastHeardLine);

    const mtCode = mt.diagnosis && mt.diagnosis.code;
    const mtDiagnosis = document.createElement('div');
    mtDiagnosis.className = 'status-diagnosis ' + (mtCode === 'mt_ok' ? 'status-diagnosis-ok' : 'status-diagnosis-attention');
    mtDiagnosis.textContent = (mt.diagnosis && mt.diagnosis.message) || '';
    panel.appendChild(mtDiagnosis);

    const mtSquaresLine = document.createElement('p');
    mtSquaresLine.appendChild(document.createTextNode('Meshtastic squares held: '));
    const mtSquaresStrong = document.createElement('strong');
    mtSquaresStrong.textContent = String(mt.squares_held ?? 0);
    mtSquaresLine.appendChild(mtSquaresStrong);
    panel.appendChild(mtSquaresLine);

    panel.appendChild(buildLabel('Meshtastic pings: today and last 7 days'));
    panel.appendChild(buildCountersTable(mt.today || MT_COUNTER_ZERO_ROW, mt.last_7_days || MT_COUNTER_ZERO_ROW, MT_COUNTER_LABELS));
  }

  if (!hasMc && !hasMt) {
    const noRadiosNote = document.createElement('p');
    noRadiosNote.className = 'hint';
    noRadiosNote.textContent = 'You have no radios registered yet -- add one below.';
    panel.appendChild(noRadiosNote);
  }

  // /api/mc/status already returns the same radios array app/nodes_api.py
  // hands back from every add/remove call -- this reuses it directly
  // rather than making a second request just to populate the list.
  clearRadiosError();
  renderRadiosList(data.radios);
  document.getElementById('status-radios').hidden = false;

  // The check-in fallback-name control only ever means anything for a
  // player who actually has a MeshCore radio -- see
  // #checkin-name-section's comment in join.html for why this is an
  // exception path, gated on hasMc rather than shown to everyone.
  // Re-evaluated on every status check (including the ones
  // applyRadiosUpdate() re-runs after an add/remove), so adding a first
  // MeshCore radio and then re-checking status reveals it without a
  // page reload, and removing one hides it again.
  const checkinNameSection = document.getElementById('checkin-name-section');
  if (checkinNameSection) {
    checkinNameSection.hidden = !hasMc;
    if (hasMc) loadCheckinName();
  }
}

// Every add/remove call (POST/DELETE /api/nodes) returns only
// {radios, added} -- not the full name/team/diagnosis/counters shape
// /api/mc/status returns -- so it used to just re-render the radios
// list on its own, leaving whatever renderStatusResult() had already
// written above it (in particular "You have no radios registered yet")
// exactly as it was at the last full check. That text is only true at
// the moment it's written; the instant an add/remove call succeeds it
// can go stale while the radios list right below it shows otherwise --
// two lines on the same panel disagreeing about the same fact. Nothing
// else in data.diagnosis/data.today/etc. changes just because a
// binding was added or removed (that only moves on real ingest
// activity, which is what checking status again is for), so the fix
// is not a second network round trip -- it's re-running the exact same
// renderStatusResult() the initial check used, against the cached
// response with only .radios swapped for the fresher array this call
// just returned. hasMc/hasMt, the "no radios yet" line, and the
// MeshCore/Meshtastic blocks all get recomputed from that single
// object every time, so they can never point in different directions
// again.
function applyRadiosUpdate(radios) {
  if (!lastStatusData) {
    // Defensive only -- status-radios (and therefore this code path)
    // is never shown before a successful status check has already run
    // renderStatusResult() at least once.
    renderRadiosList(radios);
    return;
  }
  renderStatusResult(Object.assign({}, lastStatusData, { radios }));
}

// ---- MeshCore check-in fallback name (advanced, key-authenticated) -------
//
// GET/POST/DELETE /api/checkin/name (app/checkin_api.py) -- a
// last-resort path for a MeshCore player whose public key isn't in the
// mwmesh.com directory app/checkin.py's identity bridge checks first
// (roughly 4 in 10 of today's bound contacts, per that module's own
// docstring), so their net check-ins can't be resolved from their
// contact automatically. Only ever wired up/shown once renderStatusResult()
// has confirmed the player has a MeshCore radio -- see the hasMc check
// there and #checkin-name-section's own comment in join.html for why
// this is an exception path, not a normal one.
//
// Reads the key fresh from #f-status-key on every call, same reasoning
// as the radio management functions below: there is exactly one place
// this key lives on the page, and every authenticated call reads it
// from there rather than caching it anywhere else.

function showCheckinNameError(message) {
  const el = document.getElementById('checkin-name-error');
  if (!el) return;
  el.textContent = message;
  el.hidden = false;
}

function clearCheckinNameError() {
  const el = document.getElementById('checkin-name-error');
  if (!el) return;
  el.textContent = '';
  el.hidden = true;
}

// Toggles between the "nothing set" state (just the form) and the
// "something set" state (the current name + Remove button, form still
// there underneath for changing it) -- sender_name is null in the
// former case, a string in the latter, matching GET/POST/DELETE
// /api/checkin/name's own {sender_name: ...} response shape exactly.
function renderCheckinNameCurrent(senderName) {
  const current = document.getElementById('checkin-name-current');
  const currentText = document.getElementById('checkin-name-current-text');
  const input = document.getElementById('f-checkin-name');
  if (!current || !currentText) return;
  if (senderName) {
    currentText.textContent = `Currently set: ${senderName}`;
    current.hidden = false;
    if (input) input.value = '';
  } else {
    current.hidden = true;
  }
}

// Runs once, right after renderStatusResult() reveals the section for
// a MeshCore player -- fills in whatever is already set (or nothing)
// without waiting for the player to open the <details> first. Quiet on
// failure: this is an advanced, optional control, not worth a visible
// error for a background fetch nobody asked for directly.
async function loadCheckinName() {
  const key = statusKeyValue();
  if (!key) return;
  try {
    const res = await fetch('/api/checkin/name', { headers: { 'X-API-Key': key } });
    if (!res.ok) return;
    const data = await res.json();
    renderCheckinNameCurrent(data && data.sender_name);
  } catch (err) {
    // Leave whatever was last rendered -- see comment above.
  }
}

async function handleCheckinNameSubmit(e) {
  e.preventDefault();
  clearCheckinNameError();

  const key = statusKeyValue();
  if (!key) {
    showCheckinNameError('Enter your API key above first.');
    return;
  }

  const input = document.getElementById('f-checkin-name');
  const name = input.value.trim();
  if (!name) {
    showCheckinNameError('Enter the name your radio posts under in the weekly-net channel.');
    return;
  }

  const submitBtn = document.getElementById('checkin-name-submit');
  submitBtn.disabled = true;
  try {
    const res = await fetch('/api/checkin/name', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-API-Key': key },
      body: JSON.stringify({ sender_name: name }),
    });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      const message = (data && typeof data.error === 'string')
        ? data.error
        : 'Something went wrong. Try again in a moment.';
      showCheckinNameError(message);
      return;
    }
    renderCheckinNameCurrent(data.sender_name);
  } catch (err) {
    showCheckinNameError('Could not reach the server. Check your connection and try again.');
  } finally {
    submitBtn.disabled = false;
  }
}

async function handleCheckinNameRemove() {
  clearCheckinNameError();

  const key = statusKeyValue();
  if (!key) {
    showCheckinNameError('Enter your API key above first.');
    return;
  }

  const removeBtn = document.getElementById('checkin-name-remove');
  removeBtn.disabled = true;
  try {
    const res = await fetch('/api/checkin/name', {
      method: 'DELETE',
      headers: { 'X-API-Key': key },
    });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      const message = (data && typeof data.error === 'string')
        ? data.error
        : 'Something went wrong. Try again in a moment.';
      showCheckinNameError(message);
      return;
    }
    renderCheckinNameCurrent(null);
  } catch (err) {
    showCheckinNameError('Could not reach the server. Check your connection and try again.');
  } finally {
    removeBtn.disabled = false;
  }
}

// ---- Radio management (GET/POST/DELETE /api/nodes) -----------------------
//
// Lives inside the same setup-check box as the status result above,
// only shown once that check has succeeded -- that is the only point
// on this page where a live, verified key and a known player both
// exist together. Every call here reads the key fresh from
// #f-status-key rather than storing it anywhere else (see the module
// docstring at the top of this file).

function showRadiosError(message) {
  const el = document.getElementById('radios-error');
  el.textContent = message;
  el.hidden = false;
}

function clearRadiosError() {
  const el = document.getElementById('radios-error');
  el.textContent = '';
  el.hidden = true;
}

function statusKeyValue() {
  return document.getElementById('f-status-key').value;
}

// Renders the "Your radios" list and wires each row's Remove button.
// Called after the initial status check and again after every
// successful add/remove, always from the radios array the server just
// returned -- never an optimistic local guess, so the list can never
// drift from what player_node actually holds.
function renderRadiosList(radios) {
  const list = document.getElementById('radios-list');
  list.replaceChildren();

  if (!radios || radios.length === 0) {
    const li = document.createElement('li');
    li.className = 'radios-empty';
    li.textContent = 'No radios registered yet.';
    list.appendChild(li);
    return;
  }

  radios.forEach((radio) => {
    const li = document.createElement('li');
    li.className = 'radios-item';

    const label = document.createElement('span');
    label.textContent = `${PROTOCOL_LABELS[radio.protocol] || radio.protocol} ${displayNodeRef(radio.protocol, radio.node_ref)}`;
    li.appendChild(label);

    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.textContent = 'Remove';
    removeBtn.addEventListener('click', () => {
      handleRemoveRadio(radio.protocol, radio.node_ref, removeBtn);
    });
    li.appendChild(removeBtn);

    list.appendChild(li);
  });
}

// Shared response handling for both the add and remove calls below --
// the status codes and meanings are identical because both routes sit
// behind the exact same X-API-Key authentication in app/nodes_api.py.
// Returns true if the caller should stop (an error was shown already).
function handleRadiosApiError(res, data) {
  if (res.status === 401) {
    showRadiosError('That key was not recognized. Double-check you copied it correctly.');
    return true;
  }
  if (res.status === 403) {
    showRadiosError('This account has been disabled.');
    return true;
  }
  if (res.status === 429) {
    showRadiosError('Too many changes, too fast. Wait a moment and try again.');
    return true;
  }
  if (!res.ok) {
    const message = (data && typeof data.error === 'string')
      ? data.error
      : 'Something went wrong. Try again in a moment.';
    showRadiosError(message);
    return true;
  }
  return false;
}

async function handleRemoveRadio(protocol, nodeRef, button) {
  clearRadiosError();
  const key = statusKeyValue();
  if (!key) {
    showRadiosError('Enter your API key above first.');
    return;
  }

  button.disabled = true;
  try {
    const url = `/api/nodes/${encodeURIComponent(nodeRef)}?protocol=${encodeURIComponent(protocol)}`;
    const res = await fetch(url, {
      method: 'DELETE',
      headers: { 'X-API-Key': key },
    });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (handleRadiosApiError(res, data)) return;
    applyRadiosUpdate(data.radios);
  } catch (err) {
    showRadiosError('Could not reach the server. Check your connection and try again.');
  } finally {
    button.disabled = false;
  }
}

async function handleAddRadioSubmit(e) {
  e.preventDefault();
  clearRadiosError();

  const key = statusKeyValue();
  if (!key) {
    showRadiosError('Enter your API key above first.');
    return;
  }

  const protocol = document.getElementById('f-add-protocol').value;
  const picker = activeAddPicker();
  // null covers both "nothing picked yet" and "manual entry left
  // empty" -- same rule the join form applies, except a node_ref is
  // required here, so it's an error rather than a no-op.
  const nodeRef = picker ? picker.getValue() : null;
  if (!nodeRef) {
    showRadiosError('Search for your node above, or enter its ID by hand.');
    return;
  }

  const submitBtn = document.getElementById('add-radio-submit');
  submitBtn.disabled = true;
  try {
    const res = await fetch('/api/nodes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-API-Key': key },
      body: JSON.stringify({ node_ref: nodeRef, protocol }),
    });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (handleRadiosApiError(res, data)) return;
    applyRadiosUpdate(data.radios);
    if (picker) picker.reset();
  } catch (err) {
    showRadiosError('Could not reach the server. Check your connection and try again.');
  } finally {
    submitBtn.disabled = false;
  }
}

// ---- Switch team (GET/POST /api/team) -------------------------------------
//
// Lives in the same setup-check box as the radios list above, shown at
// the same point (once the key above has checked out) but rendered and
// reset independently of renderStatusResult()/applyRadiosUpdate() --
// those rebuild #status-result and the radios list from scratch on
// every check and every radio add/remove, and an in-progress team pick
// must not vanish just because the player edited a radio at the same
// time. Every call here reads the key fresh from #f-status-key, same
// reasoning as the radio management functions above.
//
// The player's own switch is capped at once per calendar month
// (app/join_api.py's switch_team()); an operator's override from the
// admin panel is unlimited and not exposed here. Ground the player is
// currently holding is never affected by a switch -- only points and
// streak travel -- and this control's confirmation copy says so
// in full before anything is submitted.

// The last GET /api/team response for the currently checked-out key --
// {team, switch_available, next_switch_at}. Read by
// renderTeamSwitchControl() and by the picker/confirm below to know
// which team is "current" (so it can't be picked again) without a
// second round trip.
let teamStatusData = null;

// The team picked in the switch-team picker, awaiting confirmation --
// null whenever the confirm box isn't showing.
let pendingSwitchTeam = null;

function showTeamError(message) {
  const el = document.getElementById('status-team-error');
  el.textContent = message;
  el.hidden = false;
}

function clearTeamError() {
  const el = document.getElementById('status-team-error');
  el.textContent = '';
  el.hidden = true;
}

// Back to "just the Switch team button" -- used on Cancel, after a
// successful switch, and before opening the picker fresh each time.
function closeTeamSwitchPicker() {
  pendingSwitchTeam = null;
  document.getElementById('status-team-picker-wrap').hidden = true;
  document.getElementById('status-team-confirm').hidden = true;
}

// Reflects teamStatusData.switch_available in the control itself: an
// enabled Switch team button, or a disabled one plus the date the
// player can next switch -- shown, never hidden outright, per the
// same "state the constraint rather than hide the control" rule the
// rest of this page's disabled states already follow.
function renderTeamSwitchControl() {
  const switchBtn = document.getElementById('status-team-switch-btn');
  const lockedHint = document.getElementById('status-team-locked-hint');
  if (!teamStatusData) return;

  if (teamStatusData.switch_available) {
    switchBtn.disabled = false;
    lockedHint.hidden = true;
  } else {
    switchBtn.disabled = true;
    lockedHint.textContent = `You already switched teams this month. You can switch again on ${formatSwitchDate(teamStatusData.next_switch_at)}.`;
    lockedHint.hidden = false;
  }
}

// Confirmation copy shown before a switch is submitted -- states, in
// order, the two things a player gives up and keeps by switching (the
// owner's explicit requirement): points and streak travel with them,
// the ground they currently hold does not, and when their next switch
// would be available if they go through with this one.
function showTeamSwitchConfirm(fromTeam, toTeam) {
  pendingSwitchTeam = toTeam;
  const box = document.getElementById('status-team-confirm');
  const title = document.getElementById('status-team-confirm-title');
  const body = document.getElementById('status-team-confirm-body');
  title.textContent = `Confirm switch to ${toTeam}?`;
  body.textContent = `You keep every point you have earned and your check-in streak. The ground you currently hold stays with ${fromTeam} -- it does not come with you. You will not be able to switch teams again until ${formatSwitchDate(teamStatusData.next_switch_at)}.`;
  box.hidden = false;
}

// Same seven swatches buildTeamPicker() draws for the join flow above,
// with the player's current team rendered disabled (still shown, per
// the "seven teams, always" rule the rest of the picker follows --
// never quietly dropped to six) rather than omitted, since picking it
// again is a 400 from the server, not a real choice.
function buildTeamSwitchPicker(currentTeam) {
  const wrap = document.getElementById('status-team-picker');
  wrap.replaceChildren();
  TEAM_ORDER.forEach((team) => {
    const swatch = document.createElement('button');
    swatch.type = 'button';
    swatch.className = 'team-swatch';
    swatch.style.setProperty('--swatch-color', TEAM_COLORS[team]);
    swatch.textContent = team;
    if (team === currentTeam) {
      swatch.disabled = true;
      swatch.title = 'Your current team';
    }
    swatch.addEventListener('click', () => {
      wrap.querySelectorAll('.team-swatch').forEach((b) => b.classList.remove('active'));
      swatch.classList.add('active');
      showTeamSwitchConfirm(currentTeam, team);
    });
    wrap.appendChild(swatch);
  });
}

function handleTeamSwitchBtnClick() {
  if (!teamStatusData || !teamStatusData.switch_available) return;
  clearTeamError();
  buildTeamSwitchPicker(teamStatusData.team);
  document.getElementById('status-team-confirm').hidden = true;
  document.getElementById('status-team-picker-wrap').hidden = false;
}

// Shared response handling for POST /api/team -- same status-code
// meanings as handleRadiosApiError() above, since /api/team sits
// behind the same X-API-Key authentication contract app/nodes_api.py's
// routes do. 400 (invalid team / already on that team) and 409
// (already switched this month) both carry a real, specific message
// from switch_team() itself, always shown verbatim rather than folded
// into a generic failure -- a 409 also carries a fresh next_switch_at,
// which updates the control into its locked state immediately instead
// of leaving a Switch team button up that can only fail again. Returns
// true if the caller should stop (an error was shown already).
function handleTeamApiError(res, data) {
  if (res.status === 401) {
    showTeamError('That key was not recognized. Double-check you copied it correctly.');
    return true;
  }
  if (res.status === 403) {
    showTeamError('This account has been disabled.');
    return true;
  }
  if (res.status === 429) {
    showTeamError('Too many changes, too fast. Wait a moment and try again.');
    return true;
  }
  if (!res.ok) {
    const message = (data && typeof data.error === 'string')
      ? data.error
      : 'Something went wrong. Try again in a moment.';
    showTeamError(message);
    if (res.status === 409 && data && typeof data.next_switch_at === 'number') {
      teamStatusData = Object.assign({}, teamStatusData, { switch_available: false, next_switch_at: data.next_switch_at });
      renderTeamSwitchControl();
    }
    return true;
  }
  return false;
}

async function handleTeamSwitchConfirm() {
  clearTeamError();
  if (!pendingSwitchTeam) return;

  const key = statusKeyValue();
  if (!key) {
    showTeamError('Enter your API key above first.');
    return;
  }

  const toTeam = pendingSwitchTeam;
  const confirmBtn = document.getElementById('status-team-confirm-btn');
  confirmBtn.disabled = true;
  try {
    const res = await fetch('/api/team', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-API-Key': key },
      body: JSON.stringify({ team: toTeam }),
    });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (handleTeamApiError(res, data)) {
      closeTeamSwitchPicker();
      return;
    }

    // Success -- reflect the new team everywhere this page already
    // shows it (name colour, the "Team:" line) by re-running
    // renderStatusResult() against the cached status response with
    // only .team swapped, the same pattern applyRadiosUpdate() above
    // uses for a fresher .radios array.
    if (lastStatusData) {
      renderStatusResult(Object.assign({}, lastStatusData, { team: data.team }));
    }
    teamStatusData = { team: data.team, switch_available: false, next_switch_at: data.next_switch_at };
    closeTeamSwitchPicker();
    renderTeamSwitchControl();
  } catch (err) {
    showTeamError('Could not reach the server. Check your connection and try again.');
  } finally {
    confirmBtn.disabled = false;
  }
}

// Runs once, right after a successful status check reveals #status-team
// -- every registered player has a team, so unlike the check-in-name
// section this is never gated behind hasMc/hasMt. Quiet-ish on
// failure: the section stays hidden rather than showing a broken
// control for a background fetch nobody asked for directly (the same
// key already just proved itself against /api/mc/status, so a failure
// here would be unexpected, not a normal error path worth its own
// message).
async function loadTeamStatus() {
  const section = document.getElementById('status-team');
  clearTeamError();
  closeTeamSwitchPicker();

  const key = statusKeyValue();
  if (!key) {
    section.hidden = true;
    return;
  }

  try {
    const res = await fetch('/api/team', { headers: { 'X-API-Key': key } });
    if (!res.ok) {
      section.hidden = true;
      return;
    }
    teamStatusData = await res.json();
    section.hidden = false;
    renderTeamSwitchControl();
  } catch (err) {
    section.hidden = true;
  }
}

async function handleStatusSubmit(e) {
  e.preventDefault();
  clearStatusError();
  document.getElementById('status-result').hidden = true;
  document.getElementById('status-team').hidden = true;
  document.getElementById('status-radios').hidden = true;

  const key = document.getElementById('f-status-key').value;
  if (!key) {
    showStatusError('Enter your API key.');
    return;
  }

  const submitBtn = document.getElementById('status-submit');
  submitBtn.disabled = true;
  try {
    const res = await fetch('/api/mc/status', {
      method: 'POST',
      headers: { 'X-API-Key': key },
    });

    if (res.status === 401) {
      showStatusError('That key was not recognized. Double-check you copied it correctly.');
      return;
    }
    if (res.status === 403) {
      showStatusError('This account has been disabled.');
      return;
    }
    if (res.status === 429) {
      showStatusError('Too many checks, too fast. Wait a moment and try again.');
      return;
    }
    if (!res.ok) {
      showStatusError('Something went wrong checking your status. Try again in a moment.');
      return;
    }

    const data = await res.json();
    renderStatusResult(data);
    loadTeamStatus();
  } catch (err) {
    showStatusError('Could not reach the server. Check your connection and try again.');
  } finally {
    submitBtn.disabled = false;
  }
}

function setupStatusKeyToggle() {
  const input = document.getElementById('f-status-key');
  const btn = document.getElementById('status-key-toggle');
  btn.addEventListener('click', () => {
    const showing = input.type === 'text';
    input.type = showing ? 'password' : 'text';
    btn.textContent = showing ? 'Show' : 'Hide';
    btn.setAttribute('aria-label', showing ? 'Show key' : 'Hide key');
  });
}

// ---- Sign in with an account provider (GET /auth/providers) --------------
//
// A SEPARATE thing from the invite-code registration flow above it on
// this page: that flow makes a PLAYER (app/join_api.py -- a radio, a
// team, an API key); this makes an ACCOUNT (app/account_api.py,
// app/oauth_api.py), the sign-in layer /account later lets someone
// point at that player via the connect-by-key flow. See #signin-panel's
// own comment in join.html for why the two are independent and can be
// done in either order.
//
// Duplicates the small "fetch the provider list, render a button per
// entry" shape frontend/link.js and frontend/account.js also carry --
// same reasoning as TEAM_COLORS' own duplication comment further up
// this file: every page here has to stay loadable and correct entirely
// on its own.
//
// Each enabled provider becomes a plain <a href="/auth/{name}/start">
// -- that route is itself a GET redirect (app/oauth_api.py), so no
// click handler is needed here at all, only the decision of which
// providers to render. GET /auth/providers only ever lists a provider
// that is actually configured (app/oauth.py's provider_enabled()), so
// an unconfigured one is never rendered as a button that would 404 the
// moment someone clicked it -- and the whole panel starts `hidden` in
// join.html and is only revealed once there is something worth
// showing (a real provider, or an auth_error to report), so an
// all-disabled deployment never flashes an empty "Sign in" box at all.
//
// "email" is the one entry in that list that is NOT rendered as a
// plain link -- there is no GET /auth/email/start redirect to point
// one at (POST /auth/email/start is a JSON endpoint -- see
// app/oauth_api.py's own "email sign-in" section comment). It instead
// just reveals #signin-email-form, the address-field-and-submit sibling
// already sitting in join.html, hidden until this function decides
// it's actually configured.
const AUTH_ERROR_MESSAGES = {
  provider_declined: 'Sign-in was cancelled.',
  invalid_session: 'That sign-in attempt expired or was already used. Try again.',
  provider_error: 'The sign-in provider had a problem. Try again in a moment.',
};

async function setupSignIn() {
  const panel = document.getElementById('signin-panel');
  const wrap = document.getElementById('signin-providers');
  const errEl = document.getElementById('signin-error');
  const emailForm = document.getElementById('signin-email-form');
  if (!panel || !wrap) return;

  // A failed sign-in attempt (GET /auth/{provider}/callback -- see
  // app/oauth_api.py's oauth_callback()) redirects back here with a
  // short, non-sensitive reason code in the query string, never the
  // raw provider error -- see that route's own docstring for why.
  const errorCode = new URLSearchParams(window.location.search).get('auth_error');
  if (errorCode && errEl) {
    errEl.textContent = AUTH_ERROR_MESSAGES[errorCode] || 'Sign-in failed. Try again.';
    errEl.hidden = false;
  }

  let providers = [];
  try {
    const res = await fetch('/auth/providers');
    if (res.ok) {
      const data = await res.json();
      providers = Array.isArray(data && data.providers) ? data.providers : [];
    }
  } catch (err) {
    providers = [];
  }

  wrap.replaceChildren();
  let hasEmail = false;
  providers.forEach((p) => {
    if (p.name === 'email') {
      hasEmail = true;
      return;
    }
    const link = document.createElement('a');
    link.className = 'signin-provider-btn';
    link.href = `/auth/${encodeURIComponent(p.name)}/start`;
    link.textContent = `Sign in with ${p.label}`;
    wrap.appendChild(link);
  });
  if (emailForm) emailForm.hidden = !hasEmail;

  if (providers.length > 0 || errorCode) panel.hidden = false;
}

// POST /auth/email/start, always answered with the SAME confirmation
// message regardless of whether the address has an account or the mail
// actually went out -- see app/oauth_api.py's email_start() docstring
// for why: this response must never be an account-enumeration oracle.
// The only distinct outcomes handled here (rate limited, malformed
// address) are about the REQUEST, not about whether the address exists.
async function handleSigninEmailSubmit(e) {
  e.preventDefault();
  const input = document.getElementById('f-signin-email');
  const errEl = document.getElementById('signin-email-error');
  const sentEl = document.getElementById('signin-email-sent');
  const btn = document.querySelector('#signin-email-form .signin-email-submit-btn');
  if (errEl) errEl.hidden = true;
  if (sentEl) sentEl.hidden = true;

  const email = input.value.trim();
  if (!email) {
    if (errEl) {
      errEl.textContent = 'Enter your email address.';
      errEl.hidden = false;
    }
    return;
  }

  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/auth/email/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email }),
    });

    if (res.status === 429) {
      if (errEl) {
        errEl.textContent = 'Too many attempts. Wait a moment and try again.';
        errEl.hidden = false;
      }
      return;
    }
    if (res.status === 400) {
      if (errEl) {
        errEl.textContent = 'Enter a valid email address.';
        errEl.hidden = false;
      }
      return;
    }
    if (!res.ok) {
      if (errEl) {
        errEl.textContent = 'Something went wrong. Try again in a moment.';
        errEl.hidden = false;
      }
      return;
    }

    if (sentEl) sentEl.hidden = false;
    input.value = '';
  } catch (err) {
    if (errEl) {
      errEl.textContent = 'Could not reach the server. Check your connection and try again.';
      errEl.hidden = false;
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function boot() {
  buildTeamPicker();
  mcPicker = createNodePicker('mc');
  mtPicker = createNodePicker('mt', (node) => {
    if (node.public_key) {
      const el = document.getElementById('f-mt-public-key');
      if (el) el.value = node.public_key;
    }
  });
  addMcPicker = createNodePicker('mc', null, 'add-mc');
  addMtPicker = createNodePicker('mt', null, 'add-mt');
  setupAddProtocolToggle();
  setupProtocolToggle();
  applyMeshtasticAvailability();
  applyInviteCodeHint();
  setupSignIn();
  setupStatusKeyToggle();
  setupEndpointCopy();
  document.getElementById('join-submit').addEventListener('click', handleJoinClick);
  document.getElementById('signin-email-form').addEventListener('submit', handleSigninEmailSubmit);
  document.getElementById('status-form').addEventListener('submit', handleStatusSubmit);
  document.getElementById('add-radio-form').addEventListener('submit', handleAddRadioSubmit);
  document.getElementById('checkin-name-form').addEventListener('submit', handleCheckinNameSubmit);
  document.getElementById('checkin-name-remove').addEventListener('click', handleCheckinNameRemove);
  document.getElementById('status-team-switch-btn').addEventListener('click', handleTeamSwitchBtnClick);
  document.getElementById('status-team-confirm-btn').addEventListener('click', handleTeamSwitchConfirm);
  document.getElementById('status-team-cancel-btn').addEventListener('click', closeTeamSwitchPicker);
}

boot();
