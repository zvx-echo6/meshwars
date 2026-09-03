/*
 * MeshWars: contact-email verification confirmation page
 * (/account/verify-email).
 *
 * Reached only by a redirect from GET /auth/contact-email/verify
 * (app/oauth_api.py's contact_email_verify()) -- never a destination
 * anyone links to directly. That route has already redeemed (or
 * failed to redeem) the mailed token by the time this page loads; the
 * outcome travels as a plain ?ok=1/?ok=0 on the query string, so this
 * script has nothing to fetch -- it only has to read that param and
 * show the matching message. Same reasoning frontend/join.js applies
 * to its own ?auth_error= query param.
 */

function boot() {
  const ok = new URLSearchParams(window.location.search).get('ok') === '1';
  const okEl = document.getElementById('verify-email-ok');
  const failEl = document.getElementById('verify-email-fail');
  if (ok) {
    okEl.hidden = false;
  } else {
    failEl.hidden = false;
  }
}

boot();
