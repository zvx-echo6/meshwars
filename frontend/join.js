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
 * X-API-Key header of the /api/mc/status request itself -- it is never
 * put in a URL, never persisted (no localStorage/sessionStorage/cookie),
 * and never logged.
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

function setupProtocolToggle() {
  const radios = document.querySelectorAll('input[name="protocol"]');
  const mcBlock = document.getElementById('mc-instructions');
  const mtBlock = document.getElementById('mt-instructions');
  function apply() {
    const checked = document.querySelector('input[name="protocol"]:checked');
    const value = checked ? checked.value : 'mc';
    mcBlock.hidden = value !== 'mc';
    mtBlock.hidden = value !== 'mt';
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

function renderStatusResult(data) {
  const panel = document.getElementById('status-result');
  panel.replaceChildren();
  panel.hidden = false;

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

  const lastHeardLine = document.createElement('p');
  lastHeardLine.appendChild(document.createTextNode('Last heard from you: '));
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
  squaresLine.appendChild(document.createTextNode('Squares held: '));
  const squaresStrong = document.createElement('strong');
  squaresStrong.textContent = String(data.squares_held ?? 0);
  squaresLine.appendChild(squaresStrong);
  panel.appendChild(squaresLine);

  panel.appendChild(buildLabel('Today and last 7 days'));
  panel.appendChild(buildCountersTable(data.today || COUNTER_ZERO_ROW, data.last_7_days || COUNTER_ZERO_ROW));
}

async function handleStatusSubmit(e) {
  e.preventDefault();
  clearStatusError();
  document.getElementById('status-result').hidden = true;

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
}

boot();
