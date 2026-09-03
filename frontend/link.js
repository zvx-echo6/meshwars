/*
 * MeshWars: sign-in decision page (/link).
 *
 * Reached only by a redirect from GET /auth/{provider}/callback when a
 * provider identity has never been seen before AND no session was
 * already open to link it onto (case 4 of the callback decision tree --
 * see app/oauth_api.py's resolve_oauth_callback() docstring). The whole
 * reason this screen exists: signing in a new way must never silently
 * create a SECOND, separate account for someone who already has one.
 * It offers exactly two doors onto the one pending identity the
 * callback parked -- app/db.py's account_pending_identity, described
 * here via GET /api/account/pending and redeemed via POST
 * /api/account/pending/create or POST /api/account/pending/link:
 *
 *   - "Create a new account" -- POST /api/account/pending/create. No
 *     session required; this route makes one.
 *   - "Sign in with a method you already use" -- a plain link to
 *     GET /auth/{provider}/start for a DIFFERENT, already-enabled
 *     provider. That full sign-in resolves as case 1 (login) or case 3
 *     (auto_linked) if it recognizes an existing account, and redirects
 *     to /account with a session -- account.js then finds the SAME
 *     pending token still sitting in the mw_pending_token cookie (it is
 *     untouched by an unrelated provider's callback) and completes the
 *     link automatically. See account.js's own module comment for that
 *     other half.
 *
 * The raw pending token itself is never visible to this file -- it
 * lives only in an HttpOnly cookie (app/oauth_api.py's
 * _set_pending_cookie()) that the browser attaches automatically to
 * every request below. This page only ever sees the DESCRIPTION GET
 * /api/account/pending returns (provider, masked email).
 *
 * SECURITY: every dynamic value rendered here (provider label, masked
 * email) is set via textContent, never innerHTML -- same rule
 * frontend/join.js's own module docstring states for the same reason.
 */

// Same small "fetch the enabled provider list, render one link per
// entry" shape join.js's setupSignIn() carries -- duplicated rather
// than imported, same reasoning as TEAM_COLORS' own duplication comment
// in join.js: every page here has to stay loadable and correct on its
// own.
async function fetchEnabledProviders() {
  try {
    const res = await fetch('/auth/providers');
    if (!res.ok) return [];
    const data = await res.json();
    return Array.isArray(data && data.providers) ? data.providers : [];
  } catch (err) {
    return [];
  }
}

function showError(message) {
  const el = document.getElementById('link-error');
  if (!el) return;
  el.textContent = message;
  el.hidden = false;
}

async function handleCreateClick() {
  const btn = document.getElementById('link-create-btn');
  btn.disabled = true;
  try {
    const res = await fetch('/api/account/pending/create', { method: 'POST' });
    let data = null;
    try { data = await res.json(); } catch (err) { data = null; }
    if (!res.ok) {
      const message = (data && typeof data.error === 'string')
        ? data.error
        : 'Something went wrong creating your account. Try again.';
      showError(message);
      btn.disabled = false;
      return;
    }
    // The new session cookie is already set by this response -- land on
    // the account page that shows it worked, same destination a
    // successful GET /auth/{provider}/callback redirects to.
    window.location.href = '/account';
  } catch (err) {
    showError('Could not reach the server. Check your connection and try again.');
    btn.disabled = false;
  }
}

async function loadPending() {
  const loadingEl = document.getElementById('link-loading');
  const contentEl = document.getElementById('link-content');
  const expiredEl = document.getElementById('link-expired');
  const summaryEl = document.getElementById('link-summary');
  const existingWrap = document.getElementById('link-existing-providers');

  let pending = null;
  try {
    const res = await fetch('/api/account/pending');
    if (res.ok) pending = await res.json();
  } catch (err) {
    pending = null;
  }

  loadingEl.hidden = true;

  if (!pending) {
    expiredEl.hidden = false;
    return;
  }

  const providerLabel = pending.provider_label || pending.provider;
  summaryEl.replaceChildren();
  summaryEl.appendChild(document.createTextNode('You signed in with '));
  const providerStrong = document.createElement('strong');
  providerStrong.textContent = providerLabel;
  summaryEl.appendChild(providerStrong);
  if (pending.email) {
    summaryEl.appendChild(document.createTextNode(' ('));
    const emailSpan = document.createElement('span');
    emailSpan.textContent = pending.email;
    summaryEl.appendChild(emailSpan);
    summaryEl.appendChild(document.createTextNode(')'));
  }
  summaryEl.appendChild(document.createTextNode(', and MeshWars has not seen that sign-in before.'));

  contentEl.hidden = false;

  const allProviders = await fetchEnabledProviders();
  // Never offer the SAME provider that produced this pending identity
  // as an "existing method" choice -- retrying it just re-authenticates
  // the identical (provider, subject) pair, which cannot resolve any
  // differently (see this file's own module comment).
  const otherProviders = allProviders.filter((p) => p.name !== pending.provider);

  existingWrap.replaceChildren();
  if (otherProviders.length === 0) {
    const note = document.createElement('p');
    note.className = 'hint';
    note.textContent = 'No other sign-in method is enabled yet -- create a new account instead.';
    existingWrap.appendChild(note);
  } else {
    otherProviders.forEach((p) => {
      const link = document.createElement('a');
      link.className = 'signin-provider-btn';
      link.href = `/auth/${encodeURIComponent(p.name)}/start`;
      link.textContent = `Sign in with ${p.label}`;
      existingWrap.appendChild(link);
    });
  }
}

function boot() {
  document.getElementById('link-create-btn').addEventListener('click', handleCreateClick);
  loadPending();
}

boot();
