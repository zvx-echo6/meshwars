/*
 * MeshWars: account page (/account).
 *
 * Talks to GET /api/account, GET /api/account/pending, POST
 * /api/account/pending/link, POST /api/account/link-key, POST
 * /api/account/logout[-all], and GET /auth/providers -- all documented
 * in app/account_api.py and app/oauth_api.py. Self-contained, same as
 * every other page script in this codebase: no build step, no shared
 * import from another page's script.
 *
 * SECURITY: every dynamic value rendered here (provider labels, masked
 * emails, player name/team, session user-agent/ip, server error text)
 * is set via textContent or an element's .value, never innerHTML --
 * same rule frontend/join.js's own module docstring states for the
 * same reason. The API key entered in the connect-by-key form is sent
 * exactly once, in the request body of POST /api/account/link-key, and
 * is never stored, logged, or echoed back -- same handling
 * frontend/join.js's own module docstring already describes for the
 * SAME key on the join page's status-check panel.
 *
 * This page never displays a secret and never implies one is
 * recoverable: GET /api/account does not return an API key (the server
 * only ever stores a one-way hash of one -- see join.html's own
 * key-warning copy), and nothing here renders one back.
 */

// Duplicated from app/oauth.py's PROVIDER_LABELS -- same reasoning as
// TEAM_COLORS' own duplication comment in join.js: this page has to
// stay correct and loadable entirely on its own, with no shared import
// from another page's script or from the backend beyond a plain
// provider NAME in each API response.
const PROVIDER_LABELS = { github: 'GitHub' };

function providerLabel(name) {
  return PROVIDER_LABELS[name] || name;
}

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

// ---- Sign in (GET /auth/providers) -- signed-out state --------------------

async function renderSignedOut() {
  document.getElementById('account-signed-out').hidden = false;
  const wrap = document.getElementById('account-signin-providers');
  const noneEl = document.getElementById('account-signin-none');

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
  if (providers.length === 0) {
    noneEl.hidden = false;
    return;
  }
  providers.forEach((p) => {
    const link = document.createElement('a');
    link.className = 'signin-provider-btn';
    link.href = `/auth/${encodeURIComponent(p.name)}/start`;
    link.textContent = `Sign in with ${p.label || providerLabel(p.name)}`;
    wrap.appendChild(link);
  });
}

// ---- Sign-in methods (GET /api/account's own identities array) -----------

function renderIdentities(identities) {
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
    strong.textContent = providerLabel(identity.provider);
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
  } catch (err) {
    showConnectError('Could not reach the server. Check your connection and try again.');
  } finally {
    submitBtn.disabled = false;
  }
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
    const uaSpan = document.createElement('span');
    uaSpan.textContent = session.user_agent || 'Unknown device';
    topLine.appendChild(uaSpan);
    if (session.current) {
      const tag = document.createElement('span');
      tag.className = 'account-session-current-tag';
      tag.textContent = 'This device';
      topLine.appendChild(tag);
    }
    li.appendChild(topLine);

    const detailLine = document.createElement('div');
    detailLine.className = 'account-identity-detail';
    detailLine.textContent =
      `Signed in ${formatDate(session.created_at)} — last seen ${formatDateTime(session.last_seen_at)}`
      + (session.ip ? ` — ${session.ip}` : '');
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
    banner.textContent = `Connected: ${pending.provider_label || providerLabel(pending.provider)} is now signed in to this account.`;
    banner.hidden = false;
  } catch (err) {
    // Offline -- leave it for the next page load to try again.
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

  renderIdentities(finalData.identities);
  renderPlayer(finalData.player);
  renderSessions(finalData.sessions);
}

function boot() {
  document.getElementById('account-connect-form').addEventListener('submit', handleConnectSubmit);
  document.getElementById('account-logout-btn').addEventListener('click', handleLogout);
  document.getElementById('account-logout-all-btn').addEventListener('click', handleLogoutAll);
  loadAccount();
}

boot();
