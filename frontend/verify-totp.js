/*
 * MeshWars: second-factor sign-in step (/verify-totp).
 *
 * Reached only by redirect, once a FIRST factor (password or a
 * magic-link click) has already verified on an account with TOTP
 * two-factor active -- app/oauth_api.py's password_start() and
 * email_callback() both hand off here instead of issuing a session
 * outright, via app/totp_api.py's account_totp_challenge mechanism
 * (see that module's own module docstring for the full design). The
 * raw challenge token itself is never visible to this file -- it
 * lives only in an HttpOnly cookie (app/totp_api.py's
 * _set_totp_challenge_cookie()) the browser attaches automatically;
 * this page only ever calls GET /auth/totp/challenge (to know whether
 * a live challenge exists at all) and POST /auth/totp/verify (to
 * redeem it), same "server reads the cookie, page never touches the
 * token" shape frontend/link.js's own module docstring describes for
 * the pending-identity cookie.
 *
 * Offers two ways to finish: a live 6-digit code from an authenticator
 * app, or one of the ten recovery codes shown once at enrollment
 * (app/account_api.py's Security panel -- frontend/account.js's own
 * TOTP section). Exactly one of the two forms is visible at a time;
 * "Use a recovery code instead" / "Use your authenticator app instead"
 * toggle between them without losing whatever the person already
 * typed into the other one.
 */

function showCodeError(message) {
  const el = document.getElementById('totp-code-error');
  if (!el) return;
  el.textContent = message;
  el.hidden = false;
}

function showRecoveryError(message) {
  const el = document.getElementById('totp-recovery-error');
  if (!el) return;
  el.textContent = message;
  el.hidden = false;
}

function clearErrors() {
  document.getElementById('totp-code-error').hidden = true;
  document.getElementById('totp-recovery-error').hidden = true;
}

// Both forms POST to the SAME route (POST /auth/totp/verify) with
// different body shapes ({code} or {recovery_code}) -- the challenge
// cookie carries which ACCOUNT this is for; the server accepts either
// credential against it, see that route's own docstring in
// app/totp_api.py. `errorFn` lets each form show its own error message
// in its own place rather than one shared banner.
async function submitVerify(body, errorFn) {
  let res;
  try {
    res = await fetch('/auth/totp/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (err) {
    errorFn('Could not reach the server. Check your connection and try again.');
    return false;
  }

  if (res.status === 429) {
    errorFn('Too many attempts. Wait a moment and try again.');
    return false;
  }
  if (res.status === 400) {
    // The challenge itself is gone (expired, already used, or this
    // page was opened with no sign-in attempt in progress at all) --
    // a wrong CODE is a 401, handled separately below, never this.
    document.getElementById('totp-content').hidden = true;
    document.getElementById('totp-expired').hidden = false;
    return false;
  }
  if (!res.ok) {
    errorFn('That code was not accepted. Check it and try again.');
    return false;
  }

  // Success: POST /auth/totp/verify already set the real session
  // cookie on this response (create_session + set_session_cookie --
  // see that route's own docstring), the same as every other door's
  // "login" case. Land on /account, the same completed-sign-in
  // destination every other door uses.
  window.location.assign('/account');
  return true;
}

function setupCodeForm() {
  const form = document.getElementById('totp-code-form');
  const input = document.getElementById('f-totp-code');
  const submitBtn = document.getElementById('totp-code-submit-btn');

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearErrors();
    const code = (input.value || '').trim();
    if (!/^[0-9]{6}$/.test(code)) {
      showCodeError('Enter the 6-digit code from your authenticator app.');
      return;
    }
    submitBtn.disabled = true;
    try {
      await submitVerify({ code }, showCodeError);
    } finally {
      submitBtn.disabled = false;
    }
  });
}

function setupRecoveryForm() {
  const form = document.getElementById('totp-recovery-form');
  const input = document.getElementById('f-totp-recovery-code');
  const submitBtn = document.getElementById('totp-recovery-submit-btn');

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearErrors();
    const recoveryCode = (input.value || '').trim();
    if (!recoveryCode) {
      showRecoveryError('Enter one of your recovery codes.');
      return;
    }
    submitBtn.disabled = true;
    try {
      await submitVerify({ recovery_code: recoveryCode }, showRecoveryError);
    } finally {
      submitBtn.disabled = false;
    }
  });
}

// Toggles which of the two forms is visible -- see this file's own
// header comment for why both exist. clearErrors() on every switch so
// a stale error from the form being hidden never lingers once it's
// out of view.
function setupModeToggle() {
  const codeForm = document.getElementById('totp-code-form');
  const recoveryForm = document.getElementById('totp-recovery-form');
  const showRecoveryBtn = document.getElementById('totp-show-recovery-btn');
  const showCodeBtn = document.getElementById('totp-show-code-btn');

  showRecoveryBtn.addEventListener('click', () => {
    clearErrors();
    codeForm.hidden = true;
    showRecoveryBtn.hidden = true;
    recoveryForm.hidden = false;
    showCodeBtn.hidden = false;
    document.getElementById('f-totp-recovery-code').focus();
  });

  showCodeBtn.addEventListener('click', () => {
    clearErrors();
    recoveryForm.hidden = true;
    showCodeBtn.hidden = true;
    codeForm.hidden = false;
    showRecoveryBtn.hidden = false;
    document.getElementById('f-totp-code').focus();
  });
}

// GET /auth/totp/challenge on load -- tells a live challenge apart
// from an expired/already-used/nonexistent one (someone who opened
// this page directly with no sign-in attempt in progress, or came
// back to it after already finishing) BEFORE showing either form, the
// same reasoning frontend/link.js's own loadPending() checks GET
// /api/account/pending first. Reveals nothing beyond "yes, keep
// going"/"no, start over" -- see app/totp_api.py's
// totp_challenge_status() docstring for why there is nothing more
// useful to show here.
async function loadChallenge() {
  const loadingEl = document.getElementById('totp-loading');
  const contentEl = document.getElementById('totp-content');
  const expiredEl = document.getElementById('totp-expired');

  let ok = false;
  try {
    const res = await fetch('/auth/totp/challenge');
    ok = res.ok;
  } catch (err) {
    ok = false;
  }

  loadingEl.hidden = true;
  if (!ok) {
    expiredEl.hidden = false;
    return;
  }
  contentEl.hidden = false;
  document.getElementById('f-totp-code').focus();
}

function boot() {
  setupCodeForm();
  setupRecoveryForm();
  setupModeToggle();
  loadChallenge();
}

boot();
