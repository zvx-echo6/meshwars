// =====================================================================
// frontend/nav-auth.js -- shows whether the visitor is signed in, in
// the nav bar's Account link, and gives them a Sign out control right
// beside it.
//
// One shared module, included on every page right after theme-toggle.js
// (same include convention as that file -- see its own header comment
// for why a small, always-loaded module beats duplicating this per
// page). The nav bar's MARKUP is still duplicated across every page
// (three owning stylesheets: coverage.css / landing.css / join.css, per
// join.css's own comment on that), but there is only one way to answer
// "is this visitor signed in," and it is the same fetch regardless of
// which page asked -- so unlike TEAM_COLORS or displayNodeRef (which
// ARE duplicated per page script, on purpose, so each page stays
// loadable on its own), this one genuinely has nothing page-specific in
// it to justify a copy per page. The Sign out button belongs here for
// exactly the same reason: it only ever needs to exist once a signed-in
// session is confirmed, and this module is already the one place that
// makes that call.
//
// Talks to GET /api/account and POST /api/account/logout
// (app/account_api.py) -- no credentials: 'include' needed or set: this
// is always a same-origin fetch, and a browser attaches same-origin
// cookies (mw_session included) to those automatically. logout (not
// logout-all) ends the current session only, matching what a nav
// control next to Account should do -- signing out everywhere is a
// deliberate, separate action the account page's Sessions panel
// already offers.
// =====================================================================

const link = document.getElementById('mw-nav-account');

// 200 with a body means a live session; 401 (no cookie, or an
// expired/revoked one) and any network failure are both treated as
// "not signed in" -- this indicator is decoration, not a security
// check, so a failed or inconclusive lookup just leaves the nav in its
// default (signed-out-looking) state rather than surfacing an error
// anywhere a visitor would notice. The Sign out button is only ever
// created on the confirmed-signed-in path below, so "not signed in"
// and "inconclusive" both correctly leave it absent.
async function applySignedInState() {
  if (!link) return;
  try {
    const res = await fetch('/api/account');
    if (!res.ok) return;
    await res.json();
    link.classList.add('mw-nav-account-signed-in');
    link.title = 'Signed in — view your account';
    addSignOutButton();
  } catch (err) {
    // Offline, or the request otherwise never completed -- leave the
    // nav exactly as it rendered by default.
  }
}

// A <button>, not a link styled to look like one -- it performs an
// action (ends the session) rather than navigating. Placed right after
// the Account link in .mw-nav-links so it reads as one more nav item;
// its visual rules live alongside .mw-nav-links a in each of the three
// owning stylesheets (coverage.css / landing.css / join.css) so it
// looks the same wherever the nav appears.
function addSignOutButton() {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.id = 'mw-nav-signout';
  btn.className = 'mw-nav-signout';
  btn.textContent = 'Sign out';
  btn.addEventListener('click', handleSignOut);
  link.insertAdjacentElement('afterend', btn);
}

// Mirrors account.js's own handleLogout: disable the control while the
// request is in flight, and on success reload the current page rather
// than redirecting anywhere -- the visitor stays put, now signed out,
// and any signed-in content on the page (including this button, and
// /account's own signed-in view) clears itself the same way it would
// on a fresh signed-out load. Unlike account.js, this module has no
// dedicated error panel to write into (it is decoration on 13 pages,
// not a page with room for one) -- a failure alerts instead, the same
// fallback admin.js already uses for a control with nowhere else to
// report to, and re-enables the button so a visitor who did NOT sign
// out is never left thinking they did, and can just try again.
async function handleSignOut() {
  const btn = document.getElementById('mw-nav-signout');
  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/api/account/logout', { method: 'POST' });
    if (!res.ok) {
      window.alert('Could not sign out. Try again.');
      if (btn) btn.disabled = false;
      return;
    }
    window.location.reload();
  } catch (err) {
    window.alert('Could not reach the server. Check your connection and try again.');
    if (btn) btn.disabled = false;
  }
}

applySignedInState();
