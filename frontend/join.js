/*
 * MeshWars: join page (/join).
 *
 * Self-contained -- no external libraries, no framework, nothing beyond
 * what the browser provides. Talks only to /config (to show the play
 * area) and POST /api/join.
 *
 * SECURITY: display names and every message the server returns are
 * untrusted. Every dynamic value rendered on this page is set via
 * textContent, an element's .value, or a CSS custom property with a
 * value this file validated itself (TEAM_COLORS lookups) -- never via
 * innerHTML/insertAdjacentHTML with anything other than a literal
 * string written in this file.
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

async function loadPlayArea() {
  const el = document.getElementById('play-area-text');
  try {
    const res = await fetch('/config');
    if (!res.ok) throw new Error('bad response');
    const cfg = await res.json();
    const pa = cfg && cfg.play_area;
    if (!pa) { el.textContent = 'Play area unavailable.'; return; }
    el.textContent =
      `Ontario, Oregon to Provo, Utah (${pa.north}°N, ${pa.west}°W ` +
      `to ${pa.south}°N, ${pa.east}°W)`;
  } catch (err) {
    el.textContent = 'Play area unavailable.';
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

function buildLabel(text) {
  const label = document.createElement('div');
  label.className = 'join-panel-subtitle';
  label.textContent = text;
  return label;
}

// config_link (built server-side by app/join_api.py's _config_link) is
// always "meshmapper://custom-api?url=<host>/api/mc/ingest&key=<key>".
// Method two (manual entry) needs just the "<host>/api/mc/ingest" part,
// pulled out of that known, server-generated shape rather than
// hardcoded here -- this stays correct if PUBLIC_HOST ever changes.
function extractEndpointFromConfigLink(configLink) {
  const match = /^meshmapper:\/\/custom-api\?url=([^&]+)&key=/.exec(configLink);
  return match ? match[1] : null;
}

function renderResult(data) {
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

    // ---- Method one: import the link ----------------------------------
    panel.appendChild(buildLabel('Method 1: import the link'));
    panel.appendChild(buildCopyRow(data.config_link));

    const linkWarning = document.createElement('p');
    linkWarning.className = 'join-key-warning';
    linkWarning.textContent =
      'This is a link for MeshMapper to import -- it is NOT a web address. ' +
      'Do not paste it into a URL or endpoint field; that will not work.';
    panel.appendChild(linkWarning);

    const steps = document.createElement('ol');
    steps.className = 'join-steps';
    [
      'Copy the link above.',
      'In MeshMapper, go to Settings, then API Endpoints.',
      'Choose Import from Clipboard.',
    ].forEach((text) => {
      const li = document.createElement('li');
      li.textContent = text;
      steps.appendChild(li);
    });
    panel.appendChild(steps);

    // ---- Method two: enter it by hand (more reliable) ------------------
    const endpoint = extractEndpointFromConfigLink(data.config_link);
    if (endpoint) {
      panel.appendChild(
        buildLabel('Method 2: enter it by hand (recommended -- more reliable)')
      );

      const endpointLabel = document.createElement('p');
      endpointLabel.className = 'hint';
      endpointLabel.textContent = 'Endpoint URL:';
      panel.appendChild(endpointLabel);
      panel.appendChild(buildCopyRow(endpoint));

      const keyLabel = document.createElement('p');
      keyLabel.className = 'hint';
      keyLabel.textContent = 'API key:';
      panel.appendChild(keyLabel);
      panel.appendChild(buildCopyRow(data.key));

      const schemeNote = document.createElement('p');
      schemeNote.className = 'hint';
      schemeNote.textContent =
        `If the app refuses the URL without a scheme, use https://${endpoint} ` +
        'instead. It must be HTTPS.';
      panel.appendChild(schemeNote);
    }
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

function boot() {
  buildTeamPicker();
  setupProtocolToggle();
  loadPlayArea();
  document.getElementById('join-form').addEventListener('submit', handleSubmit);
}

boot();
