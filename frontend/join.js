/*
 * MeshWars: join page (/join).
 *
 * Self-contained -- no external libraries, no framework, nothing beyond
 * what the browser provides. Talks to POST /api/join, POST /api/mc/status,
 * and GET /config.
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

function buildLabel(text) {
  const label = document.createElement('div');
  label.className = 'join-panel-subtitle';
  label.textContent = text;
  return label;
}

// A numbered step whose content is one or more DOM nodes (plain text,
// a copy row, whatever) rather than a single string -- used to embed
// the live endpoint/key copy rows directly inside steps 5 and 6 of the
// MeshMapper setup list below.
function buildStepLi(nodes, { critical = false } = {}) {
  const li = document.createElement('li');
  if (critical) li.className = 'join-step-critical';
  nodes.forEach((n) => li.appendChild(typeof n === 'string' ? document.createTextNode(n) : n));
  return li;
}

function buildWarningBox() {
  const box = document.createElement('div');
  box.className = 'join-warning-box';

  const strong = document.createElement('strong');
  strong.textContent = 'Do not paste the meshmapper:// link into the URL field.';
  box.appendChild(strong);
  box.appendChild(document.createTextNode(
    ' It is not a web address. It is only for Import from Clipboard.'
  ));
  return box;
}

// config_link (built server-side by app/join_api.py's _config_link) is
// always "meshmapper://custom-api?url=<host>/api/mc/ingest&key=<key>".
// The manual-entry steps below need just the "<host>/api/mc/ingest"
// part, pulled out of that known, server-generated shape rather than
// hardcoded here -- this stays correct if PUBLIC_HOST ever changes.
function extractEndpointFromConfigLink(configLink) {
  const match = /^meshmapper:\/\/custom-api\?url=([^&]+)&key=/.exec(configLink);
  return match ? match[1] : null;
}

function renderResult(data) {
  document.getElementById('status-check-panel').hidden = true;
  document.getElementById('join-form-panel').hidden = true;

  const panel = document.getElementById('join-result-panel');
  panel.replaceChildren();
  panel.hidden = false;

  const title = document.createElement('div');
  title.className = 'join-panel-title';
  title.textContent = 'Registered';
  panel.appendChild(title);

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
  teamLine.appendChild(document.createTextNode(data.team));
  panel.appendChild(teamLine);

  const warn = document.createElement('p');
  warn.className = 'join-key-warning';
  warn.textContent = 'This key is shown once. It cannot be recovered -- store it somewhere safe.';
  panel.appendChild(warn);

  panel.appendChild(buildCopyRow(data.key));

  if (data.config_link) {
    const methodsTitle = document.createElement('div');
    methodsTitle.className = 'join-panel-title';
    methodsTitle.textContent = 'Set up MeshMapper';
    panel.appendChild(methodsTitle);

    const endpoint = extractEndpointFromConfigLink(data.config_link);
    const endpointUrl = endpoint ? `https://${endpoint}` : null;

    // ---- Recommended: explicit step-by-step, entered by hand --------
    // Step 4 (the Custom API Endpoint toggle) is the one people miss --
    // everything else can be perfect and nothing is sent while it's
    // off -- so it gets its own visual emphasis (join-step-critical).
    panel.appendChild(buildLabel('Follow these steps'));

    const steps = document.createElement('ol');
    steps.className = 'join-steps';
    steps.appendChild(buildStepLi(['Open MeshMapper']));
    steps.appendChild(buildStepLi(['Open Settings']));
    steps.appendChild(buildStepLi(['Scroll down to API Endpoints']));
    steps.appendChild(buildStepLi([(() => {
      const strong = document.createElement('strong');
      strong.textContent = 'Toggle Custom API Endpoint on';
      return strong;
    })()], { critical: true }));
    if (endpointUrl) {
      steps.appendChild(buildStepLi(['URL: ', buildCopyRow(endpointUrl)]));
    }
    steps.appendChild(buildStepLi(['API Key: ', buildCopyRow(data.key)]));
    steps.appendChild(buildStepLi(['Save']));
    steps.appendChild(buildStepLi([(() => {
      const frag = document.createDocumentFragment();
      const strong = document.createElement('strong');
      strong.textContent = 'Include Contact Key';
      frag.appendChild(document.createTextNode('Make sure '));
      frag.appendChild(strong);
      frag.appendChild(document.createTextNode(' is on'));
      return frag;
    })()]));
    steps.appendChild(buildStepLi(['Start a wardriving session — nothing is sent without one']));
    panel.appendChild(steps);

    panel.appendChild(buildWarningBox());

    // ---- Alternative: import the link ---------------------------------
    panel.appendChild(buildLabel('Alternative: import the link'));
    panel.appendChild(buildCopyRow(data.config_link));

    const altSteps = document.createElement('ol');
    altSteps.className = 'join-steps';
    [
      'Copy the link above.',
      'In MeshMapper, go to Settings, then API Endpoints.',
      'Choose Import from Clipboard.',
    ].forEach((text) => {
      const li = document.createElement('li');
      li.textContent = text;
      altSteps.appendChild(li);
    });
    panel.appendChild(altSteps);
  }
}

async function handleSubmit(e) {
  e.preventDefault();
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
      return;
    }
    renderResult(data);
  } catch (err) {
    showError('Could not reach the server. Check your connection and try again.');
  } finally {
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
      showStatusError('That key was not recognised. Double-check you copied it correctly.');
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
  setupStatusKeyToggle();
  document.getElementById('join-form').addEventListener('submit', handleSubmit);
  document.getElementById('status-form').addEventListener('submit', handleStatusSubmit);
}

boot();
