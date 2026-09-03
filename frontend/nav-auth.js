// =====================================================================
// frontend/nav-auth.js -- shows whether the visitor is signed in, in
// the nav bar's Account link.
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
// it to justify a copy per page.
//
// Talks to GET /api/account (app/account_api.py) -- no credentials:
// 'include' needed or set: this is always a same-origin fetch, and a
// browser attaches same-origin cookies (mw_session included) to those
// automatically.
// =====================================================================

const link = document.getElementById('mw-nav-account');

// 200 with a body means a live session; 401 (no cookie, or an
// expired/revoked one) and any network failure are both treated as
// "not signed in" -- this indicator is decoration, not a security
// check, so a failed or inconclusive lookup just leaves the nav in its
// default (signed-out-looking) state rather than surfacing an error
// anywhere a visitor would notice.
async function applySignedInState() {
  if (!link) return;
  try {
    const res = await fetch('/api/account');
    if (!res.ok) return;
    await res.json();
    link.classList.add('mw-nav-account-signed-in');
    link.title = 'Signed in — view your account';
  } catch (err) {
    // Offline, or the request otherwise never completed -- leave the
    // nav exactly as it rendered by default.
  }
}

applySignedInState();
