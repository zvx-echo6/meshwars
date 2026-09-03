/*
 * MeshWars: account page (/account).
 *
 * Talks to GET /api/account, GET /api/account/pending, POST
 * /api/account/pending/link, POST /api/account/link-key, POST
 * /api/account/logout[-all], GET /auth/providers, and the full set of
 * session-authenticated player/security surfaces this page grew into:
 * GET/POST/DELETE /api/nodes, POST /api/mc/status, GET
 * /api/account/checkin-health, POST /api/checkin/confirm/start, GET
 * /api/checkin/confirm/status, POST /api/checkin/confirm/accept,
 * DELETE /api/checkin/confirm, GET/POST /api/team, GET
 * /api/account/stats, GET /api/account/checkins, GET
 * /api/account/honors, DELETE /api/account/identity/{provider},
 * POST/DELETE /api/account/password, POST /api/account/contact-email,
 * and POST /api/account/rotate-key -- all documented in
 * app/account_api.py, app/nodes_api.py, app/mc_api.py,
 * app/checkin_api.py, app/join_api.py, and app/oauth_api.py.
 * Self-contained, same as every other page script in this codebase: no
 * build step, no shared import from another page's script, with the
 * one exception every page offering sign-in shares --
 * frontend/signin-email.js, see that module's own header comment for
 * why (same exception frontend/nav-auth.js already is for the nav
 * bar's signed-in state).
 *
 * All of the routes above except the sign-in ones are session-cookie
 * authenticated with NO X-API-Key header -- see each router's own
 * "allow_session_fallback" note in app/auth.py. That is the whole
 * point of surfacing them here rather than sending someone back to
 * join.html's key-pasting panel: a signed-in visitor never has to
 * paste their key on this page for anything.
 *
 * Every player-scoped section below (radios, troubleshooting,
 * check-in health, confirm-my-node, team, stats, check-in history,
 * honors) is gated on session.player_id existing at all -- see
 * applyPlayerGate(). An account with no linked player sees a plain
 * explanation and the connect-by-key form instead of any of those
 * sections erroring out.
 *
 * SECURITY: every dynamic value rendered here (provider labels, masked
 * emails, player name/team, session user-agent/ip, server error text,
 * diagnosis/explanation copy, confirm-my-node candidate names) is set
 * via textContent or an element's .value, never innerHTML -- same rule
 * frontend/join.js's own module docstring states for the same reason.
 * The API key entered in the connect-by-key form, and the freshly
 * rotated key rotate-key returns, are each handled the same way
 * frontend/join.js's own module docstring describes for the identical
 * key on the join page: sent/shown exactly once, never stored or
 * logged, and GET /api/account never returns a key at all.
 */

import {
  fetchProviders,
  renderProviderButtons,
  setupEmailSignInForm,
  setupPasswordSignInForm,
  PASSWORD_SIGNIN_AVAILABLE,
} from './signin-email.js?v=20260903-3';

// No local PROVIDER_LABELS map here -- app/oauth.py's PROVIDER_LABELS
// is the single source of truth, and every API response this page
// reads that names a provider (GET /api/account's identities,
// GET /api/account/pending) already carries the label alongside the
// raw name (`label` / `provider_label`), so this page never has to
// guess a display capitalization itself.

// Same seven-team palette every other page script here carries its own
// copy of (join.js, mc.js, map2.js, results.js) -- see join.js's own
// comment on why this is duplicated rather than imported.
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

const PROTOCOL_LABELS = { mt: 'Meshtastic', mc: 'MeshCore' };

// Cached across sections so the page never has to re-fetch a response
// it already has just to swap one field after a mutation -- same
// pattern join.js's lastStatusData/teamStatusData follow.
let lastAccountData = null;
let lastTeamStatus = null;
let pendingSwitchTeam = null;

// Date-only, no time of day (year/month/day) -- the same convention
// frontend/about.js's formatEndsAt() uses. For a fact where only the
// day matters (an identity's Added date, a session's Signed-in date).
function formatDate(ts) {
  if (!ts) return 'unknown';
  try {
    return new Date(ts * 1000).toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    });
  } catch (e) {
    return 'unknown';
  }
}

// Same date, plus a minute-precision time -- for a fact where the time
// of day itself is useful (an identity's last-used moment, a session's
// last-seen moment), without the seconds a plain toLocaleString() would
// tack on.
function formatDateTime(ts) {
  if (!ts) return 'unknown';
  try {
    const d = new Date(ts * 1000);
    const date = d.toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    });
    const time = d.toLocaleTimeString(undefined, {
      hour: 'numeric',
      minute: '2-digit',
    });
    return `${date}, ${time}`;
  } catch (e) {
    return 'unknown';
  }
}

// Same rounding-to-a-phrase relative time join.js's own copy of this
// uses for the diagnosis sentence's "last heard from" fact.
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
// see GET /api/team's own docstring in app/join_api.py). Pinned to
// America/Boise rather than the viewer's local zone for the same
// reason join.js's own copy of this pins it: ts is midnight in that
// zone, and a viewer west of Mountain time would otherwise see that
// instant fall a day early.
function formatSwitchDate(ts) {
  if (!ts) return 'unknown';
  try {
    return new Date(ts * 1000).toLocaleDateString(undefined, {
      year: 'numeric', month: 'long', day: 'numeric', timeZone: 'America/Boise',
    });
  } catch (e) {
    return 'unknown';
  }
}

// "2026-08" -> "August 2026", for a month_award row's own month key
// (app/results.py's month_key()).
function formatMonth(monthKey) {
  if (!monthKey) return 'unknown';
  try {
    const [year, month] = monthKey.split('-').map(Number);
    return new Date(Date.UTC(year, month - 1, 1)).toLocaleDateString(undefined, {
      year: 'numeric', month: 'long', timeZone: 'UTC',
    });
  } catch (e) {
    return monthKey;
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
  row.className = 'account-copy-row';

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

function teamLine(team) {
  const span = document.createElement('span');
  span.textContent = team;
  span.style.color = TEAM_COLORS[team] || 'inherit';
  span.style.fontWeight = '700';
  return span;
}

// ---- Sign in (GET /auth/providers) -- signed-out state --------------------

async function renderSignedOut() {
  document.getElementById('account-signed-out').hidden = false;
  const wrap = document.getElementById('account-signin-providers');
  const noneEl = document.getElementById('account-signin-none');
  const emailForm = document.getElementById('account-signin-email-form');
  const magicLinkBtn = document.getElementById('account-signin-magic-link-btn');

  const providers = await fetchProviders();

  // "email" is rendered as its own address-field-and-submit form
  // (#account-signin-email-form below), not a plain provider link --
  // see frontend/signin-email.js's own header comment for why (there
  // is no GET /auth/email/start redirect to point one at). Same
  // component frontend/join.js's #signin-email-form and
  // frontend/link.js's #link-email-form use.
  const hasEmail = providers.some((p) => p.name === 'email');
  const linkableProviders = providers.filter((p) => p.name !== 'email');

  if (providers.length === 0 && !PASSWORD_SIGNIN_AVAILABLE) {
    // Truly nothing configured anywhere -- see this deployment's own
    // GET /auth/providers. Left as `providers`, not `linkableProviders`,
    // so an email-only deployment (nothing here to render, but email
    // sign-in IS reachable from /join or /link) never shows this
    // page's "no sign-in method enabled" hint when one genuinely is.
    // PASSWORD_SIGNIN_AVAILABLE is a constant `true` (see
    // frontend/signin-email.js's own header comment for why), so this
    // branch can no longer actually run -- left correct rather than
    // deleted, in case that ever stops being true.
    noneEl.hidden = false;
    if (emailForm) emailForm.hidden = true;
    return;
  }
  noneEl.hidden = true;
  renderProviderButtons(linkableProviders, wrap);

  // The email <form> now always has something to offer -- password
  // sign-in (#account-signin-password-group), which is never gated by
  // GET /auth/providers -- so unlike before, it is no longer hidden
  // when magic-link email itself isn't configured. Only the magic-link
  // button is gated on `hasEmail`, same as it always was.
  if (emailForm) emailForm.hidden = false;
  if (magicLinkBtn) magicLinkBtn.hidden = !hasEmail;
}

// ---- Sign-in methods (GET /api/account's own identities array) -----------

function showIdentitiesError(message) {
  const el = document.getElementById('account-identities-error');
  el.textContent = message;
  el.hidden = false;
}

async function handleUnlinkIdentity(provider, button) {
  button.disabled = true;
  try {
    const res = await fetch(`/api/account/identity/${encodeURIComponent(provider)}`, { method: 'DELETE' });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      const message = (data && typeof data.error === 'string')
        ? data.error
        : 'Something went wrong. Try again in a moment.';
      showIdentitiesError(message);
      button.disabled = false;
      return;
    }
    await refreshAccountCore();
  } catch (err) {
    showIdentitiesError('Could not reach the server. Check your connection and try again.');
    button.disabled = false;
  }
}

function renderIdentities(identities) {
  document.getElementById('account-identities-error').hidden = true;
  const list = document.getElementById('account-identities');
  list.replaceChildren();
  if (!identities || identities.length === 0) {
    const li = document.createElement('li');
    li.className = 'account-identities-empty';
    li.textContent = 'No sign-in methods connected.';
    list.appendChild(li);
    return;
  }
  identities.forEach((identity) => {
    const li = document.createElement('li');
    li.className = 'account-identity-item';

    const nameLine = document.createElement('div');
    nameLine.className = 'account-identity-name';
    const strong = document.createElement('strong');
    strong.textContent = identity.label || identity.provider;
    nameLine.appendChild(strong);
    li.appendChild(nameLine);

    if (identity.email) {
      const emailLine = document.createElement('div');
      emailLine.className = 'account-identity-detail';
      emailLine.textContent = identity.email;
      li.appendChild(emailLine);
    }

    const detailLine = document.createElement('div');
    detailLine.className = 'account-identity-detail';
    detailLine.textContent =
      `Added ${formatDate(identity.linked_at)} — last used ${formatDateTime(identity.last_login_at)}`;
    li.appendChild(detailLine);

    // can_remove already reflects the server's own last-door count
    // (see app/account_api.py's _door_counts()) -- a button is only
    // ever rendered when the backend would actually accept the
    // request, per this page's own hard rule never to offer one the
    // server would refuse.
    if (identity.can_remove) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'account-identity-disconnect-btn';
      btn.textContent = 'Disconnect';
      btn.addEventListener('click', () => handleUnlinkIdentity(identity.provider, btn));
      li.appendChild(btn);
    } else {
      const note = document.createElement('div');
      note.className = 'account-identity-detail account-identity-lastdoor';
      note.textContent = 'This is your only way to sign in — connect another method or set a password first.';
      li.appendChild(note);
    }

    list.appendChild(li);
  });
}

// ---- Player (GET /api/account's own player field, or null) ---------------

function renderPlayer(player) {
  const linkedEl = document.getElementById('account-player-linked');
  const unlinkedEl = document.getElementById('account-player-unlinked');
  if (player) {
    document.getElementById('account-player-name').replaceChildren(
      document.createTextNode('Name: '),
      (() => {
        const strong = document.createElement('strong');
        strong.textContent = player.display_name;
        strong.style.color = TEAM_COLORS[player.team] || 'inherit';
        return strong;
      })(),
    );
    document.getElementById('account-player-team').textContent = `Team: ${player.team}`;
    linkedEl.hidden = false;
    unlinkedEl.hidden = true;
  } else {
    linkedEl.hidden = true;
    unlinkedEl.hidden = false;
  }
}

function showConnectError(message) {
  const el = document.getElementById('account-connect-error');
  el.textContent = message;
  el.hidden = false;
}

function clearConnectError() {
  const el = document.getElementById('account-connect-error');
  el.textContent = '';
  el.hidden = true;
}

async function handleConnectSubmit(e) {
  e.preventDefault();
  clearConnectError();

  const input = document.getElementById('f-account-key');
  const rawKey = input.value.trim();
  if (!rawKey) {
    showConnectError('Enter your API key.');
    return;
  }

  const submitBtn = document.getElementById('account-connect-submit');
  submitBtn.disabled = true;
  try {
    const res = await fetch('/api/account/link-key', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: rawKey }),
    });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      const message = (data && typeof data.error === 'string')
        ? data.error
        : 'Something went wrong. Try again in a moment.';
      showConnectError(message);
      return;
    }
    input.value = '';
    renderPlayer(data.player);
    applyPlayerGate(!!data.player);
    if (data.player) loadPlayerSections();
  } catch (err) {
    showConnectError('Could not reach the server. Check your connection and try again.');
  } finally {
    submitBtn.disabled = false;
  }
}

// ---- Player-section gate ---------------------------------------------
//
// Everything scoped to a linked player (radios, troubleshooting,
// check-in health/name, team, stats, check-in history, honors) lives
// inside #account-player-groups, plus the API key panel down in
// Security (rotate-key 404s with no player -- see
// app/account_api.py's POST /api/account/rotate-key docstring). An
// account with no linked player never sees any of these error out --
// it sees the same one-line explanation #account-no-player-note
// already gives, pointing back up at the Player panel's connect form.
function applyPlayerGate(hasPlayer) {
  document.getElementById('account-player-groups').hidden = !hasPlayer;
  document.getElementById('account-no-player-note').hidden = hasPlayer;
  document.getElementById('account-key-panel').hidden = !hasPlayer;
}

// ---- Sessions (GET /api/account's own sessions array) ---------------------

function renderSessions(sessions) {
  const list = document.getElementById('account-sessions');
  list.replaceChildren();
  if (!sessions || sessions.length === 0) {
    const li = document.createElement('li');
    li.className = 'account-sessions-empty';
    li.textContent = 'No active sessions.';
    list.appendChild(li);
    return;
  }
  sessions.forEach((session) => {
    const li = document.createElement('li');
    li.className = 'account-session-item';

    const topLine = document.createElement('div');
    topLine.className = 'account-session-top';
    const labelSpan = document.createElement('span');
    // device_label is a short "<Browser> on <OS>" string produced
    // server-side by app/device_label.py -- never a raw User-Agent
    // (fingerprinting risk) and never an IP address (not stored at
    // all anymore; see account_session's own comment in app/db.py).
    // textContent, not innerHTML: this value is attacker-influenced
    // (derived from a request header) and must never be parsed as
    // markup, the same reasoning every other dynamic value on this
    // page already follows.
    labelSpan.textContent = session.device_label || 'Unknown device';
    topLine.appendChild(labelSpan);
    if (session.current) {
      const tag = document.createElement('span');
      tag.className = 'account-session-current-tag';
      tag.textContent = 'This device';
      topLine.appendChild(tag);
    }
    li.appendChild(topLine);

    const detailLine = document.createElement('div');
    detailLine.className = 'account-identity-detail';
    // No IP address to append anymore -- created/last-seen timestamps
    // plus the device label above are enough to tell one session from
    // another without keeping an address around.
    detailLine.textContent =
      `Signed in ${formatDate(session.created_at)} — last seen ${formatDateTime(session.last_seen_at)}`;
    li.appendChild(detailLine);

    list.appendChild(li);
  });
}

function showSessionError(message) {
  const el = document.getElementById('account-session-error');
  el.textContent = message;
  el.hidden = false;
}

async function handleLogout() {
  const btn = document.getElementById('account-logout-btn');
  btn.disabled = true;
  try {
    const res = await fetch('/api/account/logout', { method: 'POST' });
    if (!res.ok) {
      showSessionError('Something went wrong signing out. Try again.');
      btn.disabled = false;
      return;
    }
    // The session cookie this page was reading is now cleared --
    // reload lands back here and renders the signed-out state fresh,
    // same as loading /account with no session ever existed.
    window.location.reload();
  } catch (err) {
    showSessionError('Could not reach the server. Check your connection and try again.');
    btn.disabled = false;
  }
}

async function handleLogoutAll() {
  const btn = document.getElementById('account-logout-all-btn');
  btn.disabled = true;
  try {
    const res = await fetch('/api/account/logout-all', { method: 'POST' });
    if (!res.ok) {
      showSessionError('Something went wrong signing out. Try again.');
      btn.disabled = false;
      return;
    }
    window.location.reload();
  } catch (err) {
    showSessionError('Could not reach the server. Check your connection and try again.');
    btn.disabled = false;
  }
}

// ============================================================================
// RADIOS (GET/POST /api/nodes, DELETE /api/nodes/{node_ref})
// ============================================================================

function showRadiosError(message) {
  const el = document.getElementById('account-radios-error');
  el.textContent = message;
  el.hidden = false;
}

function clearRadiosError() {
  const el = document.getElementById('account-radios-error');
  el.textContent = '';
  el.hidden = true;
}

function displayNodeRef(protocol, nodeRef) {
  return protocol === 'mt' && nodeRef && !nodeRef.startsWith('!') ? `!${nodeRef}` : nodeRef;
}

function renderRadiosList(radios) {
  const list = document.getElementById('account-radios-list');
  list.replaceChildren();

  if (!radios || radios.length === 0) {
    const li = document.createElement('li');
    li.className = 'account-radios-empty';
    li.textContent = 'No radios registered yet.';
    list.appendChild(li);
    return;
  }

  radios.forEach((radio) => {
    const li = document.createElement('li');
    li.className = 'account-radios-item';

    const label = document.createElement('span');
    label.textContent = `${PROTOCOL_LABELS[radio.protocol] || radio.protocol} ${displayNodeRef(radio.protocol, radio.node_ref)}`;
    li.appendChild(label);

    const detail = document.createElement('span');
    detail.className = 'account-radios-item-detail';
    detail.textContent = `bound ${formatDate(radio.bound_at)}`;
    li.appendChild(detail);

    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.textContent = 'Remove';
    removeBtn.addEventListener('click', () => handleRemoveRadio(radio.protocol, radio.node_ref, removeBtn));
    li.appendChild(removeBtn);

    list.appendChild(li);
  });
}

// Shared response handling for the add/remove radio calls -- both
// routes sit behind the exact same session-cookie authentication.
// Returns true if the caller should stop (an error was shown already).
function handleRadiosApiError(res, data) {
  if (res.status === 409) {
    // The one conflict this form can hit: someone else already
    // registered this exact node_ref. Surfaced verbatim -- the server
    // message already says exactly this -- rather than folded into a
    // generic failure, per this page's own requirement to make that
    // case clear rather than a plain error.
    showRadiosError((data && data.error) || 'That radio is already registered to another player.');
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
  button.disabled = true;
  try {
    const url = `/api/nodes/${encodeURIComponent(nodeRef)}?protocol=${encodeURIComponent(protocol)}`;
    const res = await fetch(url, { method: 'DELETE' });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (handleRadiosApiError(res, data)) { button.disabled = false; return; }
    renderRadiosList(data.radios);
  } catch (err) {
    showRadiosError('Could not reach the server. Check your connection and try again.');
    button.disabled = false;
  }
}

function setupAddRadioProtocolToggle() {
  const select = document.getElementById('f-account-add-protocol');
  const publicKeyField = document.getElementById('account-add-public-key-field');
  const apply = () => { publicKeyField.hidden = select.value !== 'mt'; };
  select.addEventListener('change', apply);
  apply();
}

async function handleAddRadioSubmit(e) {
  e.preventDefault();
  clearRadiosError();

  const protocol = document.getElementById('f-account-add-protocol').value;
  const nodeRefInput = document.getElementById('f-account-add-node-ref');
  const nodeRef = nodeRefInput.value.trim();
  if (!nodeRef) {
    showRadiosError('Enter the radio’s node ID.');
    return;
  }
  const publicKeyInput = document.getElementById('f-account-add-public-key');
  const publicKey = protocol === 'mt' ? publicKeyInput.value.trim() : '';

  const body = { protocol, node_ref: nodeRef };
  if (publicKey) body.public_key = publicKey;

  const submitBtn = document.getElementById('account-add-radio-submit');
  submitBtn.disabled = true;
  try {
    const res = await fetch('/api/nodes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (handleRadiosApiError(res, data)) return;
    renderRadiosList(data.radios);
    nodeRefInput.value = '';
    publicKeyInput.value = '';
  } catch (err) {
    showRadiosError('Could not reach the server. Check your connection and try again.');
  } finally {
    submitBtn.disabled = false;
  }
}

async function loadRadios() {
  try {
    const res = await fetch('/api/nodes');
    if (!res.ok) return;
    const data = await res.json();
    renderRadiosList(data.radios);
  } catch (err) {
    // Quiet -- the add/remove form still works from an empty list; a
    // background load failure here is not worth its own error banner.
  }
}

// ============================================================================
// TROUBLESHOOTING / SETUP CHECK (POST /api/mc/status)
// ============================================================================

function buildCountersTable(today, week, labels) {
  const table = document.createElement('table');
  table.className = 'account-table';

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

const MC_COUNTER_LABELS = [
  ['accepted', 'Accepted'],
  ['duplicate', 'Duplicate'],
  ['out_of_area', 'Out of area'],
  ['no_repeaters', 'No repeaters heard'],
  ['bad_coord', 'Bad coordinates'],
  ['no_contact', 'No contact key'],
  ['wrong_owner', 'Wrong owner'],
  ['batches', 'Batches'],
];

const MT_COUNTER_LABELS = [
  ['accepted', 'Accepted'],
  ['duplicate', 'Duplicate'],
  ['out_of_area', 'Out of area'],
  ['no_repeaters', 'No feeder heard'],
  ['bad_coord', 'Bad coordinates'],
  ['low_precision', 'Low position precision'],
  ['implausible_speed', 'Implausible speed'],
];

function buildLabel(text) {
  const label = document.createElement('div');
  label.className = 'account-panel-subtitle';
  label.textContent = text;
  return label;
}

function renderStatusResult(data) {
  const panel = document.getElementById('account-status-result');
  panel.replaceChildren();
  panel.hidden = false;

  const radios = Array.isArray(data.radios) ? data.radios : [];
  const hasMc = radios.some((r) => r.protocol === 'mc');
  const hasMt = radios.some((r) => r.protocol === 'mt');

  if (hasMc) {
    const diagnosis = document.createElement('div');
    const code = data.diagnosis && data.diagnosis.code;
    diagnosis.className = 'account-diagnosis ' + (code === 'ok' ? 'account-diagnosis-ok' : 'account-diagnosis-attention');
    diagnosis.textContent = (data.diagnosis && data.diagnosis.message) || '';
    panel.appendChild(diagnosis);

    panel.appendChild(buildLabel('MeshCore'));
    const summary = document.createElement('p');
    summary.className = 'account-hint';
    summary.textContent = `Last batch ${relativeTimeFromEpoch(data.last_batch_at)}. Squares held: ${data.squares_held}.`;
    panel.appendChild(summary);
    panel.appendChild(buildCountersTable(data.today, data.last_7_days, MC_COUNTER_LABELS));
  }

  if (hasMt && data.mt) {
    if (hasMc) {
      const divider = document.createElement('hr');
      divider.className = 'account-divider';
      panel.appendChild(divider);
    }
    const mt = data.mt;
    const mtCode = mt.diagnosis && mt.diagnosis.code;
    const mtDiagnosis = document.createElement('div');
    mtDiagnosis.className = 'account-diagnosis ' + (mtCode === 'mt_ok' ? 'account-diagnosis-ok' : 'account-diagnosis-attention');
    mtDiagnosis.textContent = (mt.diagnosis && mt.diagnosis.message) || '';
    panel.appendChild(mtDiagnosis);

    panel.appendChild(buildLabel('Meshtastic'));
    const summary = document.createElement('p');
    summary.className = 'account-hint';
    summary.textContent = `Last heard ${relativeTimeFromEpoch(mt.last_heard_at)}. Squares held: ${mt.squares_held}.`;
    panel.appendChild(summary);
    panel.appendChild(buildCountersTable(mt.today, mt.last_7_days, MT_COUNTER_LABELS));
  }

  if (!hasMc && !hasMt) {
    const note = document.createElement('p');
    note.className = 'account-hint';
    note.textContent = 'No radios registered yet — add one above, then check again.';
    panel.appendChild(note);
  }
}

function showStatusError(message) {
  const el = document.getElementById('account-status-error');
  el.textContent = message;
  el.hidden = false;
}

async function handleStatusCheck() {
  const btn = document.getElementById('account-status-check-btn');
  const errEl = document.getElementById('account-status-error');
  errEl.hidden = true;
  btn.disabled = true;
  btn.textContent = 'Checking...';
  try {
    const res = await fetch('/api/mc/status', { method: 'POST' });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      const message = (data && typeof data.error === 'string')
        ? data.error
        : 'Something went wrong. Try again in a moment.';
      showStatusError(message);
      return;
    }
    renderStatusResult(data);
  } catch (err) {
    showStatusError('Could not reach the server. Check your connection and try again.');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Check now';
  }
}

// ============================================================================
// WHY MY CHECK-INS MAY NOT BE COUNTING (GET /api/account/checkin-health)
// ============================================================================

const CONTACT_STATUS_LABELS = {
  resolved: 'Resolved',
  not_in_directory: 'Not in directory',
  key_ambiguous: 'Key matches more than one entry',
  name_ambiguous: 'Name shared by more than one radio',
};

// data.state is the headline app/account_api.py's _diagnose_checkin_health()
// computed: 'credited' (an award actually exists for the most recent net --
// the ONLY state that's really "fine") plus five attention states, each
// with data.summary already written as one concrete next step. This
// function's job is just to lay that headline out and, underneath it,
// list this player's own bound contacts (data.contacts, unchanged shape)
// for anyone who wants the per-radio detail behind the headline -- it does
// not re-derive "is this okay," that decision already happened server-side
// against real award data, not against anything this function could infer
// from contacts alone (see this endpoint's own docstring for why that
// used to be the bug).
function renderCheckinHealth(data) {
  const panel = document.getElementById('account-checkin-health-result');
  panel.replaceChildren();
  panel.hidden = false;

  const summary = document.createElement('div');
  summary.className = 'account-diagnosis ' + (data.state === 'credited' ? 'account-diagnosis-ok' : 'account-diagnosis-attention');
  summary.textContent = data.summary || '';
  panel.appendChild(summary);

  if (data.contacts && data.contacts.length > 0) {
    panel.appendChild(buildLabel('Your bound MeshCore contacts'));
    const list = document.createElement('ul');
    list.className = 'account-contacts-list';
    data.contacts.forEach((c) => {
      const li = document.createElement('li');
      li.className = 'account-contacts-item';

      const top = document.createElement('div');
      top.className = 'account-contacts-item-top';
      const ref = document.createElement('span');
      ref.className = 'account-mono';
      ref.textContent = c.node_ref;
      top.appendChild(ref);
      const status = document.createElement('span');
      status.className = 'account-contact-status ' + (c.status === 'resolved' ? 'account-contact-status-ok' : 'account-contact-status-attention');
      status.textContent = CONTACT_STATUS_LABELS[c.status] || c.status;
      top.appendChild(status);
      li.appendChild(top);

      if (c.resolved_name) {
        const nameLine = document.createElement('div');
        nameLine.className = 'account-hint';
        nameLine.textContent = `Resolves as: ${c.resolved_name}`;
        li.appendChild(nameLine);
      }

      const explanation = document.createElement('p');
      explanation.className = 'account-hint';
      explanation.textContent = c.explanation;
      li.appendChild(explanation);

      list.appendChild(li);
    });
    panel.appendChild(list);
  }
}

function showCheckinHealthError(message) {
  const el = document.getElementById('account-checkin-health-error');
  el.textContent = message;
  el.hidden = false;
}

async function handleCheckinHealthCheck() {
  const btn = document.getElementById('account-checkin-health-btn');
  const errEl = document.getElementById('account-checkin-health-error');
  errEl.hidden = true;
  btn.disabled = true;
  btn.textContent = 'Checking...';
  try {
    const res = await fetch('/api/account/checkin-health');
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      const message = (data && typeof data.error === 'string')
        ? data.error
        : 'Something went wrong. Try again in a moment.';
      showCheckinHealthError(message);
      return;
    }
    renderCheckinHealth(data);
  } catch (err) {
    showCheckinHealthError('Could not reach the server. Check your connection and try again.');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Check now';
  }
}

// ============================================================================
// CONFIRM MY NODE (POST /api/checkin/confirm/start, GET .../status,
// POST .../accept, DELETE /api/checkin/confirm)
// ============================================================================
//
// Replaces the old typed fallback-name form (GET/POST/DELETE
// /api/checkin/name, retired -- nobody had one registered, so there
// was no migration to carry forward). That form let a player type
// whatever name they believed their radio posted under, which quietly
// went stale the moment a radio's on-mesh name drifted from it. This
// instead proves possession live, right now, on whichever protocol the
// player picks from the "Radio type" dropdown (see frontend/
// account.html's own comment on this section) -- ONE form, ONE
// button, the dropdown the only thing that changes what pressing it
// does:
//
//   - MeshCore: the player types the name their radio shows RIGHT NOW,
//     triggers an advert, and picks their node out of whatever adverts
//     arrive during a five-minute window -- proof of live possession,
//     not a typed guess. Full mechanics (the baseline snapshot, why a
//     bare name match isn't enough proof on its own) live in
//     app/checkin_api.py's node-confirmation section and app/db.py's
//     mc_node_confirmation comment.
//   - Meshtastic: no name to type at all -- the button issues a short,
//     one-time broadcast CODE (app/checkin.py's Meshtastic node-
//     confirmation section), the player sends that exact text on their
//     mesh, and MeshWars watches for whichever node id it arrived
//     from. The code itself is the proof; there's nothing to compare
//     it against.
//
// Both protocols share every state past submission -- idle -> waiting
// -> (candidates found) -> bound, with cancel/expire dropping back to
// idle from any point past start -- and every timer/poll/teardown
// mechanism below. Only the render functions branch on `data.protocol`
// (as reported by GET .../status, and by the start response) to draw
// the protocol-specific parts: MeshCore's advert instructions and
// public-key/role candidate details, versus Meshtastic's issued code
// and node-id candidate details.
// Server throttles its own upstream scan to one per 8s (see
// app/checkin_api.py's _scan_cache comment) -- polling every 5s here
// is safely inside that, so a poll never goes to waste but also never
// pushes on a scan that's about to be answered from cache anyway.
const CHECKIN_CONFIRM_POLL_MS = 5000;

// Two independent timers, both cleared together by
// stopCheckinConfirmPolling() -- the 5s one re-fetches status, the 1s
// one only redraws the on-screen countdown between fetches so it
// doesn't visibly stall between polls.
let checkinConfirmPollTimer = null;
let checkinConfirmCountdownTimer = null;
// epoch seconds of the currently open window, or null when none is
// open -- doubles as "are we actively watching" (tickCheckinConfirm
// Countdown and the 'none' branch of applyCheckinConfirmStatus both
// read it) so there's no separate boolean to keep in sync with it.
let checkinConfirmExpiresAt = null;

function showCheckinConfirmError(message) {
  const el = document.getElementById('account-checkin-confirm-error');
  el.textContent = message;
  el.hidden = false;
}

function clearCheckinConfirmError() {
  const el = document.getElementById('account-checkin-confirm-error');
  el.textContent = '';
  el.hidden = true;
}

function stopCheckinConfirmPolling() {
  if (checkinConfirmPollTimer) { clearInterval(checkinConfirmPollTimer); checkinConfirmPollTimer = null; }
  if (checkinConfirmCountdownTimer) { clearInterval(checkinConfirmCountdownTimer); checkinConfirmCountdownTimer = null; }
}

// Same pattern setupAddRadioProtocolToggle() already uses for the "My
// radios" form's own protocol select -- toggle a field's `hidden`
// property off the dropdown's current value, both on 'change' and once
// up front so the idle form starts consistent with whatever option is
// marked `selected` in the HTML. Meshtastic needs no typed name at all
// (see this section's header comment), so that's the one field this
// toggles; nothing else in the form depends on the protocol choice.
function setupCheckinConfirmProtocolToggle() {
  const select = document.getElementById('f-account-checkin-confirm-protocol');
  const nameField = document.getElementById('account-checkin-confirm-name-field');
  const apply = () => { nameField.hidden = select.value === 'mt'; };
  select.addEventListener('change', apply);
  apply();
}

// mm:ss, not the day/hour/minute granularity frontend/mc.js's own
// formatCountdown() uses -- that one's built for season-length
// windows; this window is five minutes, so seconds matter.
function formatCheckinConfirmCountdown(secondsRemaining) {
  const clamped = Math.max(0, secondsRemaining);
  const mins = Math.floor(clamped / 60);
  const secs = clamped % 60;
  return `${mins}:${String(secs).padStart(2, '0')}`;
}

function tickCheckinConfirmCountdown() {
  const el = document.getElementById('account-checkin-confirm-countdown');
  if (!el || !checkinConfirmExpiresAt) return;
  const remaining = checkinConfirmExpiresAt - Math.floor(Date.now() / 1000);
  if (remaining <= 0) {
    // Don't wait for the next 5s poll to say so -- the clock reaching
    // zero is itself proof the window closed.
    renderCheckinConfirmExpired();
    return;
  }
  el.textContent = `Window closes in ${formatCheckinConfirmCountdown(remaining)}`;
}

// Starts both timers together -- called once, right after start()
// succeeds or when loadCheckinConfirmStatus() finds a window already
// open from a previous page load. Guards against a double-start (e.g.
// a resumed page load racing a fresh submit) by clearing first.
function startCheckinConfirmWatching() {
  stopCheckinConfirmPolling();
  checkinConfirmPollTimer = setInterval(pollCheckinConfirmStatus, CHECKIN_CONFIRM_POLL_MS);
  checkinConfirmCountdownTimer = setInterval(tickCheckinConfirmCountdown, 1000);
}

function renderCheckinConfirmIdle() {
  stopCheckinConfirmPolling();
  checkinConfirmExpiresAt = null;
  // Re-showing the whole form (dropdown included) is what makes the
  // "Radio type" choice changeable again -- see frontend/account.html's
  // own comment on this section for why there's no separate disabled
  // state to manage: the dropdown is simply unreachable, hidden inside
  // this same form, for as long as a window is open past this point.
  document.getElementById('account-checkin-confirm-start-form').hidden = false;
  const result = document.getElementById('account-checkin-confirm-result');
  result.replaceChildren();
  result.hidden = true;
}

function renderCheckinConfirmWaiting(data) {
  document.getElementById('account-checkin-confirm-start-form').hidden = true;
  checkinConfirmExpiresAt = data.expires_at;

  const result = document.getElementById('account-checkin-confirm-result');
  result.replaceChildren();
  result.hidden = false;

  if (data.protocol === 'mt') {
    // GET .../status now echoes the issued code back (app/checkin_api.py's
    // confirm_status docstring: it's about to go out in the clear on an
    // open mesh anyway, and status is already scoped to the caller's own
    // window), so a page reload mid-window recovers it here exactly the
    // same way it recovers everything else about this window -- no
    // separate client-side cache to keep in sync, and no "we lost the
    // code" dead end to design around.
    //
    // Prominent and copyable, per this section's own design: sized
    // up (.account-checkin-confirm-code-row) from the same
    // input+Copy-button look buildCopyRow() already gives the
    // API key and join link elsewhere on this page, so a player can
    // read it at a glance or copy it without retyping by hand.
    const codeRow = buildCopyRow(data.code);
    codeRow.classList.add('account-checkin-confirm-code-row');
    result.appendChild(codeRow);

    const instructions = document.createElement('p');
    instructions.className = 'account-hint';
    instructions.textContent = 'Send that exact text as a message on your mesh now -- any channel, and it can be part of a longer sentence. We watch for it and show you which node it came from below.';
    result.appendChild(instructions);
  } else {
    const instructions = document.createElement('p');
    instructions.className = 'account-hint';
    instructions.textContent = 'Trigger an advert on that radio now -- most MeshCore devices send one from a long-press of the side button, or a "Send Advert" / "Flood Advert" menu item.';
    result.appendChild(instructions);

    if (data.baseline_count > 0) {
      // Other nodes were already posting under this exact name before
      // this window opened -- none of THEM count as proof (see this
      // section's own header comment), but the player should still
      // expect to see more than just their own node show up below.
      const baselineNote = document.createElement('p');
      baselineNote.className = 'account-hint';
      baselineNote.textContent = `${data.baseline_count} other node${data.baseline_count !== 1 ? 's' : ''} on the mesh already carr${data.baseline_count !== 1 ? 'y' : 'ies'} that name -- expect more than one candidate below once yours adverts.`;
      result.appendChild(baselineNote);
    }
  }

  const countdown = document.createElement('p');
  countdown.id = 'account-checkin-confirm-countdown';
  countdown.className = 'account-checkin-confirm-countdown';
  result.appendChild(countdown);
  tickCheckinConfirmCountdown();

  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.className = 'account-link-btn';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.addEventListener('click', handleCheckinConfirmCancel);
  result.appendChild(cancelBtn);
}

function renderCheckinConfirmCandidates(data) {
  document.getElementById('account-checkin-confirm-start-form').hidden = true;
  checkinConfirmExpiresAt = data.expires_at;

  const result = document.getElementById('account-checkin-confirm-result');
  result.replaceChildren();
  result.hidden = false;

  const isMt = data.protocol === 'mt';

  const prompt = document.createElement('p');
  prompt.className = 'account-hint';
  prompt.textContent = isMt
    ? 'We heard the following nodes send that code. Pick yours:'
    : 'We heard the following nodes adverting under that name. Pick yours:';
  result.appendChild(prompt);

  const countdown = document.createElement('p');
  countdown.id = 'account-checkin-confirm-countdown';
  countdown.className = 'account-checkin-confirm-countdown';
  result.appendChild(countdown);
  tickCheckinConfirmCountdown();

  const list = document.createElement('ul');
  list.className = 'account-checkin-candidates-list';
  (data.candidates || []).forEach((cand) => {
    const li = document.createElement('li');
    li.className = 'account-checkin-candidate-item';

    const top = document.createElement('div');
    top.className = 'account-checkin-candidate-top';
    const name = document.createElement('span');
    // Meshtastic candidates carry a name only when node_seen already
    // recognizes that node id (app/checkin.py's
    // mt_confirm_scan_all_connectors) -- fall back to the node_ref
    // itself rather than showing a blank label.
    name.textContent = isMt ? (cand.name || cand.node_ref) : cand.name;
    top.appendChild(name);
    if (!isMt) {
      // No role concept on the Meshtastic side (mt_confirm_scan_all_
      // connectors carries no such field) -- MeshCore-only, same as
      // the public-key detail below.
      const role = document.createElement('span');
      role.className = 'account-hint';
      role.textContent = cand.role || '';
      top.appendChild(role);
    }
    li.appendChild(top);

    const detail = document.createElement('p');
    detail.className = 'account-hint';
    if (isMt) {
      detail.textContent = `${cand.node_ref} · heard ${relativeTimeFromEpoch(cand.last_heard)}`;
    } else {
      const shortKey = cand.public_key ? `${cand.public_key.slice(0, 8)}…${cand.public_key.slice(-4)}` : 'unknown key';
      detail.textContent = `${cand.node_ref} · ${shortKey} · heard ${relativeTimeFromEpoch(cand.last_heard)}`;
    }
    li.appendChild(detail);

    const claimBtn = document.createElement('button');
    claimBtn.type = 'button';
    if (cand.already_claimed) {
      // Visibly disabled and labelled, not just left off the list --
      // the player still needs to see it was heard, and understand why
      // it's not clickable, rather than wonder if their node is
      // missing entirely.
      claimBtn.textContent = 'Already registered to someone else';
      claimBtn.disabled = true;
      claimBtn.className = 'account-checkin-candidate-claimed';
    } else {
      claimBtn.textContent = 'This is mine';
      claimBtn.className = 'account-checkin-confirm-btn';
      // Which identifier to send back on accept differs by protocol
      // (app/checkin_api.py's confirm_accept: `public_key` for mc,
      // `node_ref` for mt) -- handleCheckinConfirmAccept takes both
      // the protocol and the identifier so it never has to guess which
      // shape a candidate came in.
      const identifier = isMt ? cand.node_ref : cand.public_key;
      claimBtn.addEventListener('click', () => handleCheckinConfirmAccept(data.protocol, identifier, claimBtn));
    }
    li.appendChild(claimBtn);

    list.appendChild(li);
  });
  result.appendChild(list);

  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.className = 'account-link-btn';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.addEventListener('click', handleCheckinConfirmCancel);
  result.appendChild(cancelBtn);
}

function renderCheckinConfirmBound(nodeRef) {
  stopCheckinConfirmPolling();
  checkinConfirmExpiresAt = null;
  document.getElementById('account-checkin-confirm-start-form').hidden = true;

  const result = document.getElementById('account-checkin-confirm-result');
  result.replaceChildren();
  result.hidden = false;

  const done = document.createElement('div');
  done.className = 'account-diagnosis account-diagnosis-ok';
  done.textContent = `Bound to ${nodeRef}. Check-ins from that node now count toward you.`;
  result.appendChild(done);

  const again = document.createElement('button');
  again.type = 'button';
  again.className = 'account-link-btn';
  again.textContent = 'Confirm another node';
  again.addEventListener('click', renderCheckinConfirmIdle);
  result.appendChild(again);
}

function renderCheckinConfirmExpired() {
  stopCheckinConfirmPolling();
  checkinConfirmExpiresAt = null;
  document.getElementById('account-checkin-confirm-start-form').hidden = true;

  const result = document.getElementById('account-checkin-confirm-result');
  result.replaceChildren();
  result.hidden = false;

  const msg = document.createElement('p');
  msg.className = 'account-hint';
  msg.textContent = 'That confirmation window closed without a match. Start again below.';
  result.appendChild(msg);

  const again = document.createElement('button');
  again.type = 'button';
  again.className = 'account-checkin-confirm-btn';
  again.textContent = 'Start again';
  again.addEventListener('click', renderCheckinConfirmIdle);
  result.appendChild(again);
}

// Shared by the initial page-load resume (loadCheckinConfirmStatus)
// and every 5s poll (pollCheckinConfirmStatus) -- both just hand their
// fetched status here and let it pick the render function.
function applyCheckinConfirmStatus(data) {
  if (data.state === 'waiting') {
    renderCheckinConfirmWaiting(data);
  } else if (data.state === 'found') {
    renderCheckinConfirmCandidates(data);
  } else if (checkinConfirmExpiresAt) {
    // 'none', but we were watching a window a moment ago -- it closed
    // (expired server-side) between polls without an accept. A bare
    // 'none' with nothing previously open (the very first status check
    // on page load) is just "idle," not "expired," so that case is
    // handled separately by loadCheckinConfirmStatus rather than here.
    renderCheckinConfirmExpired();
  }
}

async function pollCheckinConfirmStatus() {
  try {
    const res = await fetch('/api/checkin/confirm/status');
    if (!res.ok) return; // transient failure -- next poll retries; don't tear down mid-advert over one bad response
    const data = await res.json();
    applyCheckinConfirmStatus(data);
  } catch (err) {
    // Quiet, same reasoning -- a single network hiccup should not
    // interrupt someone mid-advert.
  }
}

// Run once on page load so a reload mid-window resumes watching
// instead of silently dropping back to a blank idle form while the
// window is still open server-side.
async function loadCheckinConfirmStatus() {
  try {
    const res = await fetch('/api/checkin/confirm/status');
    if (!res.ok) return;
    const data = await res.json();
    if (data.state === 'waiting' || data.state === 'found') {
      applyCheckinConfirmStatus(data);
      startCheckinConfirmWatching();
    }
    // 'none' -- nothing to resume; the idle form is already what
    // account.html renders by default.
  } catch (err) {
    // Quiet -- same as loadRadios(): the idle form still works even if
    // this background check fails.
  }
}

async function handleCheckinConfirmStart(e) {
  e.preventDefault();
  clearCheckinConfirmError();

  const protocol = document.getElementById('f-account-checkin-confirm-protocol').value;
  const body = { protocol };

  // MeshCore needs the typed name; Meshtastic needs nothing from the
  // player at all -- the server issues the code (see this section's
  // header comment).
  if (protocol === 'mc') {
    const input = document.getElementById('f-account-checkin-confirm-name');
    const name = input.value.trim();
    if (!name) {
      showCheckinConfirmError('Enter the name your radio shows on the mesh.');
      return;
    }
    body.name = name;
  }

  const btn = document.getElementById('account-checkin-confirm-start-btn');
  btn.disabled = true;
  btn.textContent = 'Starting...';
  try {
    const res = await fetch('/api/checkin/confirm/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      showCheckinConfirmError((data && data.error) || 'Something went wrong. Try again in a moment.');
      return;
    }
    // The response shape already carries `protocol` (the server
    // default is 'mc' if omitted, but we always send it explicitly
    // above) and, for 'mt', `code` -- renderCheckinConfirmWaiting reads
    // both straight off `data`, so it stays in sync with whichever
    // window the server actually opened, the same as a resumed
    // status poll after a reload.
    renderCheckinConfirmWaiting(data);
    startCheckinConfirmWatching();
  } catch (err) {
    showCheckinConfirmError('Could not reach the server. Check your connection and try again.');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Confirm my node';
  }
}

async function handleCheckinConfirmCancel() {
  clearCheckinConfirmError();
  try {
    await fetch('/api/checkin/confirm', { method: 'DELETE' });
  } catch (err) {
    // Quiet -- the window expires server-side on its own timer even if
    // this DELETE never lands, so a failed cancel request here is not
    // worth surfacing; the UI already drops back to idle below.
  }
  renderCheckinConfirmIdle();
}

async function handleCheckinConfirmAccept(protocol, identifier, button) {
  clearCheckinConfirmError();
  button.disabled = true;
  const original = button.textContent;
  button.textContent = 'Binding...';
  // Which body field the server expects differs by protocol
  // (app/checkin_api.py's confirm_accept: `public_key` for mc,
  // `node_ref` for mt) -- `identifier` is whichever value
  // renderCheckinConfirmCandidates already picked off the matching
  // candidate for this protocol.
  const body = protocol === 'mt' ? { node_ref: identifier } : { public_key: identifier };
  try {
    const res = await fetch('/api/checkin/confirm/accept', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (res.status === 409) {
      // The one conflict this can hit: someone else already claimed
      // this exact node. Surfaced verbatim -- the server message
      // already says exactly this plainly -- rather than a generic
      // failure, so it reads as an expected outcome, not an error.
      showCheckinConfirmError((data && data.error) || 'That node is already registered to another player.');
      button.disabled = false;
      button.textContent = original;
      return;
    }
    if (!res.ok) {
      showCheckinConfirmError((data && data.error) || 'Something went wrong. Try again in a moment.');
      button.disabled = false;
      button.textContent = original;
      return;
    }
    renderCheckinConfirmBound(data.node_ref);
    loadRadios(); // refresh so the newly bound radio appears above without a page reload
  } catch (err) {
    showCheckinConfirmError('Could not reach the server. Check your connection and try again.');
    button.disabled = false;
    button.textContent = original;
  }
}

// ============================================================================
// MY STATS (GET /api/account/stats)
// ============================================================================

function renderBoardCard(protocol, board) {
  const card = document.createElement('div');
  card.className = 'account-stats-card';

  const title = document.createElement('div');
  title.className = 'account-panel-subtitle';
  title.textContent = PROTOCOL_LABELS[protocol] || protocol;
  card.appendChild(title);

  const rows = [
    ['Total points', board.total_points],
    ['Squares held', board.tiles_held],
    ['Check-in points', board.checkin_points],
    ['Explorer points', board.explorer_points],
    ['Nets checked in', board.nets_checked_in],
    ['Check-in streak', board.checkin_streak],
    ['Last check-in', board.last_checkin_net_date || 'never'],
    ['Last position heard', relativeTimeFromEpoch(board.last_position_ts)],
  ];

  const dl = document.createElement('dl');
  dl.className = 'account-stats-dl';
  rows.forEach(([label, value]) => {
    const dt = document.createElement('dt');
    dt.textContent = label;
    const dd = document.createElement('dd');
    dd.textContent = String(value);
    dl.appendChild(dt);
    dl.appendChild(dd);
  });
  card.appendChild(dl);
  return card;
}

async function loadStats() {
  const errEl = document.getElementById('account-stats-error');
  const resultEl = document.getElementById('account-stats-result');
  errEl.hidden = true;
  try {
    const res = await fetch('/api/account/stats');
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      errEl.textContent = (data && data.error) || 'Could not load your stats.';
      errEl.hidden = false;
      return;
    }
    resultEl.replaceChildren();
    const boards = data.boards || {};
    ['mc', 'mt'].forEach((protocol) => {
      if (boards[protocol]) resultEl.appendChild(renderBoardCard(protocol, boards[protocol]));
    });
    if (Object.keys(boards).length === 0) {
      const note = document.createElement('p');
      note.className = 'account-hint';
      note.textContent = 'No stats yet.';
      resultEl.appendChild(note);
    }
  } catch (err) {
    errEl.textContent = 'Could not reach the server. Check your connection and try again.';
    errEl.hidden = false;
  }
}

// ============================================================================
// MY TEAM (GET/POST /api/team)
// ============================================================================

function showTeamError(message) {
  const el = document.getElementById('account-team-error');
  el.textContent = message;
  el.hidden = false;
}

function clearTeamError() {
  const el = document.getElementById('account-team-error');
  el.textContent = '';
  el.hidden = true;
}

function closeTeamSwitchPicker() {
  pendingSwitchTeam = null;
  document.getElementById('account-team-picker-wrap').hidden = true;
  document.getElementById('account-team-confirm').hidden = true;
}

function renderTeamCurrent(team) {
  document.getElementById('account-team-current').replaceChildren(
    document.createTextNode('Current team: '),
    teamLine(team),
  );
}

function renderTeamSwitchControl() {
  const switchBtn = document.getElementById('account-team-switch-btn');
  const lockedHint = document.getElementById('account-team-locked-hint');
  if (!lastTeamStatus) return;
  switchBtn.hidden = false;
  if (lastTeamStatus.switch_available) {
    switchBtn.disabled = false;
    lockedHint.hidden = true;
  } else {
    switchBtn.disabled = true;
    lockedHint.textContent = `You already switched teams this month. You can switch again on ${formatSwitchDate(lastTeamStatus.next_switch_at)}.`;
    lockedHint.hidden = false;
  }
}

function showTeamSwitchConfirm(fromTeam, toTeam) {
  pendingSwitchTeam = toTeam;
  const box = document.getElementById('account-team-confirm');
  const title = document.getElementById('account-team-confirm-title');
  const body = document.getElementById('account-team-confirm-body');
  title.textContent = `Confirm switch to ${toTeam}?`;
  body.textContent = `You keep every point you have earned and your check-in streak. The ground you currently hold stays with ${fromTeam} — it does not come with you. You will not be able to switch teams again until ${formatSwitchDate(lastTeamStatus.next_switch_at)}.`;
  box.hidden = false;
}

function buildTeamSwitchPicker(currentTeam) {
  const wrap = document.getElementById('account-team-picker');
  wrap.replaceChildren();
  TEAM_ORDER.forEach((team) => {
    const swatch = document.createElement('button');
    swatch.type = 'button';
    swatch.className = 'account-team-swatch';
    swatch.style.setProperty('--swatch-color', TEAM_COLORS[team]);
    swatch.textContent = team;
    if (team === currentTeam) {
      swatch.disabled = true;
      swatch.title = 'Your current team';
    }
    swatch.addEventListener('click', () => {
      wrap.querySelectorAll('.account-team-swatch').forEach((b) => b.classList.remove('active'));
      swatch.classList.add('active');
      showTeamSwitchConfirm(currentTeam, team);
    });
    wrap.appendChild(swatch);
  });
}

function handleTeamSwitchBtnClick() {
  if (!lastTeamStatus || !lastTeamStatus.switch_available) return;
  clearTeamError();
  buildTeamSwitchPicker(lastTeamStatus.team);
  document.getElementById('account-team-confirm').hidden = true;
  document.getElementById('account-team-picker-wrap').hidden = false;
}

async function handleTeamSwitchConfirm() {
  clearTeamError();
  if (!pendingSwitchTeam) return;
  const toTeam = pendingSwitchTeam;
  const confirmBtn = document.getElementById('account-team-confirm-btn');
  confirmBtn.disabled = true;
  try {
    const res = await fetch('/api/team', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ team: toTeam }),
    });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      showTeamError((data && data.error) || 'Something went wrong. Try again in a moment.');
      if (res.status === 409 && data && typeof data.next_switch_at === 'number') {
        lastTeamStatus = Object.assign({}, lastTeamStatus, { switch_available: false, next_switch_at: data.next_switch_at });
        renderTeamSwitchControl();
      }
      closeTeamSwitchPicker();
      return;
    }
    renderTeamCurrent(data.team);
    if (lastAccountData && lastAccountData.player) {
      lastAccountData = Object.assign({}, lastAccountData, {
        player: Object.assign({}, lastAccountData.player, { team: data.team }),
      });
      renderPlayer(lastAccountData.player);
    }
    lastTeamStatus = { team: data.team, switch_available: false, next_switch_at: data.next_switch_at };
    closeTeamSwitchPicker();
    renderTeamSwitchControl();
    loadStats(); // team-scoped figures elsewhere on the page stay in sync
  } catch (err) {
    showTeamError('Could not reach the server. Check your connection and try again.');
  } finally {
    confirmBtn.disabled = false;
  }
}

async function loadTeamStatus() {
  try {
    const res = await fetch('/api/team');
    if (!res.ok) return;
    const data = await res.json();
    lastTeamStatus = data;
    renderTeamCurrent(data.team);
    renderTeamSwitchControl();
  } catch (err) {
    // Quiet -- the rest of the page still works without this section.
  }
}

// ============================================================================
// MY CHECK-IN HISTORY (GET /api/account/checkins)
// ============================================================================

async function loadCheckins() {
  const errEl = document.getElementById('account-checkins-error');
  const resultEl = document.getElementById('account-checkins-result');
  errEl.hidden = true;
  try {
    const res = await fetch('/api/account/checkins');
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      errEl.textContent = (data && data.error) || 'Could not load your check-in history.';
      errEl.hidden = false;
      return;
    }
    resultEl.replaceChildren();
    const checkins = data.checkins || [];
    if (checkins.length === 0) {
      const note = document.createElement('p');
      note.className = 'account-hint';
      note.textContent = 'No check-ins recorded yet.';
      resultEl.appendChild(note);
      return;
    }
    const table = document.createElement('table');
    table.className = 'account-table';
    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    ['Date', 'Board', 'Points', 'Streak'].forEach((h) => {
      const th = document.createElement('th');
      th.textContent = h;
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    checkins.forEach((c) => {
      const tr = document.createElement('tr');
      [c.net_date, PROTOCOL_LABELS[c.protocol] || c.protocol, c.points, c.streak ?? '—'].forEach((v) => {
        const td = document.createElement('td');
        td.textContent = String(v);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    resultEl.appendChild(table);
  } catch (err) {
    errEl.textContent = 'Could not reach the server. Check your connection and try again.';
    errEl.hidden = false;
  }
}

// ============================================================================
// MY HONORS (GET /api/account/honors)
// ============================================================================

async function loadHonors() {
  const errEl = document.getElementById('account-honors-error');
  const list = document.getElementById('account-honors-list');
  errEl.hidden = true;
  try {
    const res = await fetch('/api/account/honors');
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      errEl.textContent = (data && data.error) || 'Could not load your honors.';
      errEl.hidden = false;
      return;
    }
    list.replaceChildren();
    const honors = data.honors || [];
    if (honors.length === 0) {
      const li = document.createElement('li');
      li.className = 'account-honors-empty';
      li.textContent = 'No honors yet.';
      list.appendChild(li);
      return;
    }
    honors.forEach((h) => {
      const li = document.createElement('li');
      li.className = 'account-honors-item';
      const title = document.createElement('div');
      title.className = 'account-identity-name';
      const strong = document.createElement('strong');
      strong.textContent = h.label;
      title.appendChild(strong);
      li.appendChild(title);
      const detail = document.createElement('div');
      detail.className = 'account-identity-detail';
      detail.textContent = `${formatMonth(h.month)} — ${PROTOCOL_LABELS[h.protocol] || h.protocol}`
        + (h.detail ? ` — ${h.detail}` : '');
      li.appendChild(detail);
      list.appendChild(li);
    });
  } catch (err) {
    errEl.textContent = 'Could not reach the server. Check your connection and try again.';
    errEl.hidden = false;
  }
}

// ============================================================================
// SECURITY: password (POST/DELETE /api/account/password)
// ============================================================================

function showPasswordError(message) {
  const el = document.getElementById('account-password-error');
  el.textContent = message;
  el.hidden = false;
}

function clearPasswordMessages() {
  document.getElementById('account-password-error').hidden = true;
  document.getElementById('account-password-success').hidden = true;
}

// Reflects whether this account is even eligible to have a password
// (a verified email identity, per POST /api/account/password's own
// docstring in app/account_api.py -- exposed to this page as each
// identity's own email_verified field, GET /api/account) and, if so,
// whether one is already set (has_password) -- current-password field,
// button label, and the remove-password option all follow from that.
function renderPasswordSection(account) {
  const hint = document.getElementById('account-password-hint');
  const form = document.getElementById('account-password-form');
  const currentField = document.getElementById('account-password-current-field');
  const newLabel = document.getElementById('account-password-new-label');
  const removeBtn = document.getElementById('account-password-remove-btn');

  const hasVerifiedEmail = (account.identities || []).some((i) => i.email_verified);
  if (!hasVerifiedEmail) {
    hint.textContent = 'Link and verify an email sign-in method (from the join page) before setting a password.';
    form.hidden = true;
    return;
  }

  if (account.has_password) {
    hint.textContent = 'Change or remove your sign-in password.';
    currentField.hidden = false;
    newLabel.textContent = 'New password';
    // Removing the password is refused by the server if this account
    // has no OTHER door left -- see app/account_api.py's DELETE
    // /api/account/password docstring. "Other doors" for THIS account
    // is exactly identities.length, the same door-counting math
    // _door_counts() runs server-side (each identity row is one door;
    // has_password's own door is the one being removed here).
    removeBtn.hidden = (account.identities || []).length < 1;
  } else {
    hint.textContent = 'Set a password so you can sign in with an email address, without needing an OAuth provider.';
    currentField.hidden = true;
    newLabel.textContent = 'Password';
    removeBtn.hidden = true;
  }
  form.hidden = false;
}

// Shows/hides the non-dismissible top-of-page prompt (GET /api/account's
// owes_password -- see app/account_api.py's _owes_password() for the
// exact rule: a verified sign-in email on file, no password yet) and
// physically relocates the ONE #account-password-form between its two
// possible homes -- #account-owes-password-form-slot (the banner, while
// owed) and right after #account-password-form-anchor (its normal spot
// in the Security > Password panel, once it isn't). This is a DOM move
// (appendChild/insertAdjacentElement re-parent the existing node), not a
// clone: the form's fields, its submit handler (handlePasswordSubmit,
// wired up once in boot() below), and every other listener on it stay
// attached exactly as they were -- there is exactly one set-password
// form and one handler in this file, just two places it can visually
// sit. Idempotent: calling this again with the account in the same
// owed/not-owed state just re-parents the node to where it already is,
// which is harmless.
function renderOwesPasswordBanner(account) {
  const banner = document.getElementById('account-owes-password-banner');
  const slot = document.getElementById('account-owes-password-form-slot');
  const anchor = document.getElementById('account-password-form-anchor');
  const form = document.getElementById('account-password-form');

  banner.hidden = !account.owes_password;
  if (account.owes_password) {
    slot.appendChild(form);
  } else {
    anchor.insertAdjacentElement('afterend', form);
  }
}

async function handlePasswordSubmit(e) {
  e.preventDefault();
  clearPasswordMessages();

  const newPasswordInput = document.getElementById('f-account-new-password');
  const currentPasswordInput = document.getElementById('f-account-current-password');
  const newPassword = newPasswordInput.value;
  if (!newPassword) {
    showPasswordError('Enter a password.');
    return;
  }

  const body = { new_password: newPassword };
  const currentField = document.getElementById('account-password-current-field');
  if (!currentField.hidden) {
    if (!currentPasswordInput.value) {
      showPasswordError('Enter your current password.');
      return;
    }
    body.current_password = currentPasswordInput.value;
  }

  const submitBtn = document.getElementById('account-password-submit');
  submitBtn.disabled = true;
  try {
    const res = await fetch('/api/account/password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      showPasswordError((data && data.error) || 'Something went wrong. Try again in a moment.');
      return;
    }
    newPasswordInput.value = '';
    currentPasswordInput.value = '';
    const successEl = document.getElementById('account-password-success');
    successEl.textContent = 'Password saved.';
    successEl.hidden = false;
    await refreshAccountCore();
  } catch (err) {
    showPasswordError('Could not reach the server. Check your connection and try again.');
  } finally {
    submitBtn.disabled = false;
  }
}

async function handlePasswordRemove() {
  clearPasswordMessages();
  const btn = document.getElementById('account-password-remove-btn');
  btn.disabled = true;
  try {
    const res = await fetch('/api/account/password', { method: 'DELETE' });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      showPasswordError((data && data.error) || 'Something went wrong. Try again in a moment.');
      return;
    }
    const successEl = document.getElementById('account-password-success');
    successEl.textContent = data.warning_last_door
      ? 'Password removed. This account now has only one way to sign in.'
      : 'Password removed.';
    successEl.hidden = false;
    await refreshAccountCore();
  } catch (err) {
    showPasswordError('Could not reach the server. Check your connection and try again.');
  } finally {
    btn.disabled = false;
  }
}

// ============================================================================
// SECURITY: contact email (POST /api/account/contact-email)
// ============================================================================

function renderContactEmail(contactEmail) {
  const current = document.getElementById('account-contact-email-current');
  if (!contactEmail) {
    current.textContent = 'No contact email set.';
    return;
  }
  current.textContent = contactEmail.verified
    ? `${contactEmail.email} — verified.`
    : `${contactEmail.email} — not verified yet. Check your inbox for the verification link, or save again to resend it.`;
}

function showContactEmailError(message) {
  const el = document.getElementById('account-contact-email-error');
  el.textContent = message;
  el.hidden = false;
}

async function handleContactEmailSubmit(e) {
  e.preventDefault();
  const el = document.getElementById('account-contact-email-error');
  el.hidden = true;

  const input = document.getElementById('f-account-contact-email');
  const email = input.value.trim();
  if (!email) {
    showContactEmailError('Enter an email address.');
    return;
  }

  const submitBtn = document.getElementById('account-contact-email-submit');
  submitBtn.disabled = true;
  try {
    const res = await fetch('/api/account/contact-email', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email }),
    });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (res.status === 404) {
      showContactEmailError('Email isn’t configured on this deployment, so a contact address can’t be verified.');
      return;
    }
    if (!res.ok) {
      showContactEmailError((data && data.error) || 'Something went wrong. Try again in a moment.');
      return;
    }
    input.value = '';
    renderContactEmail({ email: data.email, verified: data.verified });
  } catch (err) {
    showContactEmailError('Could not reach the server. Check your connection and try again.');
  } finally {
    submitBtn.disabled = false;
  }
}

// ============================================================================
// SECURITY: API key rotation (POST /api/account/rotate-key)
// ============================================================================

function showRotateKeyError(message) {
  const el = document.getElementById('account-rotate-key-error');
  el.textContent = message;
  el.hidden = false;
}

function handleRotateKeyBtnClick() {
  document.getElementById('account-rotate-key-error').hidden = true;
  document.getElementById('account-rotate-key-confirm').hidden = false;
}

function handleRotateKeyCancel() {
  document.getElementById('account-rotate-key-confirm').hidden = true;
}

async function handleRotateKeyConfirm() {
  const errEl = document.getElementById('account-rotate-key-error');
  errEl.hidden = true;
  const confirmBtn = document.getElementById('account-rotate-key-confirm-btn');
  confirmBtn.disabled = true;
  try {
    const res = await fetch('/api/account/rotate-key', { method: 'POST' });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      showRotateKeyError((data && data.error) || 'Something went wrong. Try again in a moment.');
      return;
    }
    document.getElementById('account-rotate-key-confirm').hidden = true;
    const resultEl = document.getElementById('account-rotate-key-result');
    resultEl.replaceChildren();

    const warning = document.createElement('div');
    warning.className = 'account-warning-box account-warning-box-strong';
    const strong = document.createElement('strong');
    strong.textContent = 'Copy this key now — this is the only time it will ever be shown.';
    warning.appendChild(strong);
    warning.appendChild(document.createTextNode(
      ' Closing or reloading this page loses it for good. MeshWars stores only a one-way hash of your key, '
      + 'never the key itself, so there is no way for anyone, including an admin, to look it back up. The only '
      + 'fix for a lost key is another rotation, which retires this one too. Your old key stopped working the '
      + 'instant this one was issued — paste this into MeshMapper’s Settings, then API Endpoints, then API '
      + 'Key, before you do anything else.',
    ));
    resultEl.appendChild(warning);
    resultEl.appendChild(buildCopyRow(data.key));
    resultEl.hidden = false;
  } catch (err) {
    showRotateKeyError('Could not reach the server. Check your connection and try again.');
  } finally {
    confirmBtn.disabled = false;
  }
}

// ============================================================================
// Load every player-scoped section -- called once a linked player is
// confirmed (loadAccount() on boot, or right after a successful
// connect-by-key). Each of these is independent and fails quietly on
// its own (see each load* function above) so one slow or broken
// section never blocks the rest of the page from populating.
// ============================================================================

function loadPlayerSections() {
  loadRadios();
  loadCheckinConfirmStatus();
  loadStats();
  loadTeamStatus();
  loadCheckins();
  loadHonors();
}

// ---- Completing a pending link (GET /api/account/pending + POST
// /api/account/pending/link) ------------------------------------------
//
// The other half of frontend/link.js's "sign in with a method you
// already use" choice: that choice sends the browser through a FULL,
// separate OAuth round trip for a different provider, which -- if it
// resolves as a login (case 1) or auto-link (case 3) -- lands back
// here, signed in, with the ORIGINAL pending identity's cookie
// (mw_pending_token) still sitting untouched in the browser (an
// unrelated provider's callback never clears it -- see
// app/oauth_api.py's _clear_flow_cookies docstring, which only ever
// touches the flow cookies, not this one). So every load of this page,
// once a session is confirmed, checks for a leftover pending identity
// and -- if one is there -- redeems it onto the account that just
// signed in, then says so.
async function maybeCompletePendingLink() {
  let pending = null;
  try {
    const res = await fetch('/api/account/pending');
    if (res.ok) pending = await res.json();
  } catch (err) {
    pending = null;
  }
  if (!pending) return;

  try {
    const res = await fetch('/api/account/pending/link', { method: 'POST' });
    if (!res.ok) return; // e.g. already linked elsewhere -- say nothing, not an error the visitor caused
    const banner = document.getElementById('account-linked-banner');
    banner.textContent = `Connected: ${pending.provider_label || pending.provider} is now signed in to this account.`;
    banner.hidden = false;
  } catch (err) {
    // Offline -- leave it for the next page load to try again.
  }
}

// Re-fetches GET /api/account and re-renders every section it alone
// drives (identities, password eligibility, contact email, sessions) --
// called after any Security-panel mutation (identity unlink, password
// set/remove) so those panels can never drift from what the server
// actually holds. Deliberately does NOT touch player/team/stats/etc.:
// none of those can be affected by a Security-panel action.
async function refreshAccountCore() {
  try {
    const res = await fetch('/api/account');
    if (!res.ok) return;
    const data = await res.json();
    lastAccountData = data;
    renderIdentities(data.identities);
    renderSessions(data.sessions);
    renderPasswordSection(data);
    renderOwesPasswordBanner(data);
    renderContactEmail(data.contact_email);
  } catch (err) {
    // Leave whatever was already rendered in place.
  }
}

// ---- boot -------------------------------------------------------------

async function loadAccount() {
  const loadingEl = document.getElementById('account-loading');
  let res;
  try {
    res = await fetch('/api/account');
  } catch (err) {
    loadingEl.hidden = true;
    renderSignedOut();
    return;
  }

  loadingEl.hidden = true;

  if (!res.ok) {
    renderSignedOut();
    return;
  }

  const data = await res.json();
  document.getElementById('account-content').hidden = false;

  await maybeCompletePendingLink();

  // Re-fetch after a possible link above -- the identities list is the
  // one thing that can change out from under the response already in
  // hand. Player/sessions cannot be affected by linking a new sign-in
  // method, so this is not repeated for those.
  let finalData = data;
  try {
    const res2 = await fetch('/api/account');
    if (res2.ok) finalData = await res2.json();
  } catch (err) {
    // Keep the original response.
  }

  lastAccountData = finalData;
  renderIdentities(finalData.identities);
  renderPlayer(finalData.player);
  renderSessions(finalData.sessions);
  renderPasswordSection(finalData);
  renderOwesPasswordBanner(finalData);
  renderContactEmail(finalData.contact_email);

  const hasPlayer = !!finalData.player;
  applyPlayerGate(hasPlayer);
  if (hasPlayer) loadPlayerSections();
}

function boot() {
  document.getElementById('account-connect-form').addEventListener('submit', handleConnectSubmit);
  document.getElementById('account-logout-btn').addEventListener('click', handleLogout);
  document.getElementById('account-logout-all-btn').addEventListener('click', handleLogoutAll);
  setupEmailSignInForm(document.getElementById('account-signin-email-form'), {
    input: document.getElementById('f-account-signin-email'),
    errorEl: document.getElementById('account-signin-email-error'),
    sentEl: document.getElementById('account-signin-email-sent'),
    submitBtn: document.getElementById('account-signin-magic-link-btn'),
  });
  setupPasswordSignInForm(document.getElementById('account-signin-email-form'), {
    emailInput: document.getElementById('f-account-signin-email'),
    passwordInput: document.getElementById('f-account-signin-password'),
    errorEl: document.getElementById('account-signin-email-error'),
    submitBtn: document.getElementById('account-signin-password-btn'),
  });

  setupAddRadioProtocolToggle();
  document.getElementById('account-add-radio-form').addEventListener('submit', handleAddRadioSubmit);
  document.getElementById('account-status-check-btn').addEventListener('click', handleStatusCheck);
  document.getElementById('account-checkin-health-btn').addEventListener('click', handleCheckinHealthCheck);
  setupCheckinConfirmProtocolToggle();
  document.getElementById('account-checkin-confirm-start-form').addEventListener('submit', handleCheckinConfirmStart);

  document.getElementById('account-team-switch-btn').addEventListener('click', handleTeamSwitchBtnClick);
  document.getElementById('account-team-confirm-btn').addEventListener('click', handleTeamSwitchConfirm);
  document.getElementById('account-team-cancel-btn').addEventListener('click', closeTeamSwitchPicker);

  document.getElementById('account-password-form').addEventListener('submit', handlePasswordSubmit);
  document.getElementById('account-password-remove-btn').addEventListener('click', handlePasswordRemove);
  document.getElementById('account-contact-email-form').addEventListener('submit', handleContactEmailSubmit);

  document.getElementById('account-rotate-key-btn').addEventListener('click', handleRotateKeyBtnClick);
  document.getElementById('account-rotate-key-cancel-btn').addEventListener('click', handleRotateKeyCancel);
  document.getElementById('account-rotate-key-confirm-btn').addEventListener('click', handleRotateKeyConfirm);

  loadAccount();
}

boot();
