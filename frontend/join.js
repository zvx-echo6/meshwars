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
 * X-API-Key header of a request itself (/api/mc/status, and now
 * /api/nodes for the radio list/add/remove calls below) -- it is never
 * put in a URL or request body, never persisted (no
 * localStorage/sessionStorage/cookie), and never logged. The radio
 * add/remove calls read it fresh from #f-status-key on every call
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
const COUNTER_LABELS = [
  ['batches', 'Batches received'],
  ['accepted', 'Accepted'],
  ['no_contact', 'No contact key'],
  ['wrong_owner', 'Wrong owner'],
  ['duplicate', 'Duplicate'],
  ['bad_coord', 'Bad coordinates'],
  ['out_of_area', 'Outside play area'],
  ['no_repeaters', 'No repeaters heard'],
];
const COUNTER_ZERO_ROW = COUNTER_LABELS.reduce((acc, [key]) => {
  acc[key] = 0;
  return acc;
}, {});

// Display labels for the "Your radios" list and the add-radio protocol
// select -- keys match exactly what the server uses everywhere else
// (player_node.protocol, app/nodes_api.py's _VALID_PROTOCOLS).
const PROTOCOL_LABELS = { mt: 'Meshtastic', mc: 'MeshCore' };

let selectedTeam = null;

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
  const stepsMc = document.querySelectorAll('.proto-step-mc');
  const stepsMt = document.querySelectorAll('.proto-step-mt');
  function apply() {
    const checked = document.querySelector('input[name="protocol"]:checked');
    const value = checked ? checked.value : 'mc';
    mcBlock.hidden = value !== 'mc';
    mtBlock.hidden = value !== 'mt';
    if (workingMc) workingMc.hidden = value !== 'mc';
    if (workingMt) workingMt.hidden = value !== 'mt';
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
  el.appendChild(nameStrong);
  el.appendChild(document.createTextNode(' on '));
  const dot = document.createElement('span');
  dot.className = 'mc-dot-inline';
  dot.style.background = TEAM_COLORS[data.team] || '#888';
  el.appendChild(dot);
  el.appendChild(document.createTextNode(data.team + '.'));
  el.hidden = false;
}

async function handleJoinClick() {
  clearError();

  const invite = document.getElementById('f-invite').value;
  const name = document.getElementById('f-name').value;
  const checkedProtocol = document.querySelector('input[name="protocol"]:checked');
  const protocol = checkedProtocol ? checkedProtocol.value : 'mc';
  const nodeRef = document.getElementById('f-node-ref').value;

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
  if (protocol === 'mt') {
    body.node_ref = nodeRef;
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

function buildCountersTable(today, week) {
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
  COUNTER_LABELS.forEach(([key, label]) => {
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

// /api/mc/status (app/mc_api.py) is, despite its now-generic-sounding
// name, entirely MeshCore-scoped under the hood: every counter, the
// diagnosis, last_batch_at, and squares_held all read player_ingest_stat/
// mc_tile filtered to protocol='mc' specifically -- there is no
// equivalent status endpoint for Meshtastic (Meshtastic's own ingest
// loop, app/ingest.py, writes real protocol='mt' rows to the same
// player_ingest_stat table, but nothing serves them back out yet). For
// a player with no MeshCore radio those fields are not just empty, they
// are actively misleading: batches stays 0 forever so the diagnosis
// always claims "MeshWars has never received anything from your app...
// check that Custom API Endpoint is switched on in MeshMapper" to
// someone who has never opened MeshMapper. This function decides what
// to show from data.radios (which protocols this player actually has
// registered) rather than rendering that response at face value --
// this is a real backend gap, not a styling choice; see the
// commit/report for the follow-up it needs (a protocol-aware status
// route) before real Meshtastic diagnostics can show here.
function renderStatusResult(data) {
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
    panel.appendChild(buildCountersTable(data.today || COUNTER_ZERO_ROW, data.last_7_days || COUNTER_ZERO_ROW));
  }

  if (hasMt) {
    const mtNote = document.createElement('p');
    mtNote.className = 'hint';
    mtNote.textContent = hasMc
      // Has both: the MeshCore section above is real and accurate for
      // that radio, but does not cover the Meshtastic one too.
      ? 'The MeshCore activity above does not cover your Meshtastic radio -- there is no live status check for Meshtastic yet. Use the map\'s player search (search by node ID) to see your current Meshtastic squares.'
      : 'There is no live status check for Meshtastic yet. MeshWars polls meshview every 45 seconds and picks up your node whenever it broadcasts a position a feeder hears -- use the map\'s player search (search by node ID) to see your current Meshtastic squares.';
    panel.appendChild(mtNote);
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
    label.textContent = `${PROTOCOL_LABELS[radio.protocol] || radio.protocol} ${radio.node_ref}`;
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
    renderRadiosList(data.radios);
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

  const nodeRefInput = document.getElementById('f-add-node-ref');
  const protocol = document.getElementById('f-add-protocol').value;
  const nodeRef = nodeRefInput.value;
  if (!nodeRef) {
    showRadiosError('Enter a node ID.');
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
    renderRadiosList(data.radios);
    nodeRefInput.value = '';
  } catch (err) {
    showRadiosError('Could not reach the server. Check your connection and try again.');
  } finally {
    submitBtn.disabled = false;
  }
}

async function handleStatusSubmit(e) {
  e.preventDefault();
  clearStatusError();
  document.getElementById('status-result').hidden = true;
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

function boot() {
  buildTeamPicker();
  setupProtocolToggle();
  applyMeshtasticAvailability();
  applyInviteCodeHint();
  setupStatusKeyToggle();
  setupEndpointCopy();
  document.getElementById('join-submit').addEventListener('click', handleJoinClick);
  document.getElementById('status-form').addEventListener('submit', handleStatusSubmit);
  document.getElementById('add-radio-form').addEventListener('submit', handleAddRadioSubmit);
}

boot();
