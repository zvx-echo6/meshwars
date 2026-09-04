// =====================================================================
// frontend/signin-email.js -- the sign-in-with-a-provider component
// shared by every page that offers it: frontend/account.js (the
// signed-out /account page's welcome panel -- also, now, the ONLY
// place sign-in is offered from the join side of the app; see that
// file's own module docstring) and frontend/link.js (the
// pending-identity screen's "sign in with a method you already use"
// choice). frontend/join.js used to be a third consumer (registration
// flow's own sign-in panel, above the anonymous invite-code flow) --
// it dropped sign-in entirely once an authenticated visitor could join
// straight from /account instead, so join.html no longer loads this
// module at all (see frontend/join.js's own module docstring for why).
//
// One shared module, included alongside those pages' own scripts --
// same exception to the "self-contained, no shared import" convention
// frontend/nav-auth.js already carries, and for the same reason: there
// is only one correct way to render GET /auth/providers as a row of
// sign-in buttons plus the "email" entry's address-and-submit form, and
// unlike TEAM_COLORS (which IS duplicated per page script, on purpose,
// so each page stays loadable on its own), this piece has nothing
// page-specific in it to justify a second copy -- see nav-auth.js's own
// header comment. Provider display labels are never duplicated anywhere
// in the frontend either: app/oauth.py's PROVIDER_LABELS is the single
// source of truth, and every API response that names a provider (GET
// /auth/providers, GET /api/account, GET /api/account/pending) already
// carries the label alongside the raw name.
//
// "email" is never rendered as a plain provider link -- unlike every
// OAuth provider, there is no GET /auth/email/start redirect to point
// one at (POST /auth/email/start is a JSON endpoint -- see
// app/oauth_api.py's own "email sign-in" section comment). It instead
// reveals the caller's own email form (address field + submit), which
// this module wires up once via setupEmailSignInForm().
//
// Password sign-in (POST /auth/password/start, app/oauth_api.py's
// "the fifth door" section) shares that same email field rather than
// asking for the address twice -- setupPasswordSignInForm() below adds
// a password input and a second submit button to the SAME <form>
// element setupEmailSignInForm() already owns, so both handlers listen
// on one 'submit' event and use `event.submitter` to tell which button
// was actually pressed. Unlike every entry GET /auth/providers can
// list, password sign-in is core application code -- there is no
// provider_enabled()-style gate for it in app/oauth.py and no SMTP
// dependency the way magic-link email has, so PASSWORD_SIGNIN_AVAILABLE
// below is a constant, not something derived from that endpoint. Only
// account.js wires it in -- frontend/link.js's "already have an
// account" panel deliberately does NOT, because POST
// /auth/password/start only ever authenticates an existing account and
// never links a pending identity onto it (see that route's own
// docstring), which is the entire reason that panel exists.
// =====================================================================

// See this file's own header comment above for why password sign-in,
// unlike every provider GET /auth/providers can list, is never
// conditionally hidden.
export const PASSWORD_SIGNIN_AVAILABLE = true;

// A failed sign-in attempt (GET /auth/{provider}/callback or GET
// /auth/email/callback -- see app/oauth_api.py's
// _callback_error_response()) redirects the browser back to /account
// with a short, non-sensitive reason code in the query string, never
// the raw provider error -- see that function's own docstring for why.
// Both callback routes land here now (previously /join, before it lost
// its own sign-in panel -- see frontend/join.js's module docstring), so
// this reads the query string once, in one place, instead of each
// signed-out panel (account.js's renderSignedOut(), formerly join.js's
// setupSignIn()) keeping its own copy of the same three-entry lookup
// table to drift out of sync with app/oauth_api.py's actual codes.
const AUTH_ERROR_MESSAGES = {
  provider_declined: 'Sign-in was cancelled.',
  invalid_session: 'That sign-in attempt expired or was already used. Try again.',
  provider_error: 'The sign-in provider had a problem. Try again in a moment.',
};

// Reads `?auth_error=` off the current page's URL and, if present,
// shows the matching message in `errEl` (a role="alert" element the
// caller owns and has already put somewhere visible in its signed-out
// panel). A no-op with no query param and with no `errEl` at all, so a
// caller can always call this unconditionally in its boot path rather
// than guarding on whether the element exists first.
export function showAuthErrorFromQuery(errEl) {
  if (!errEl) return;
  const errorCode = new URLSearchParams(window.location.search).get('auth_error');
  if (!errorCode) return;
  errEl.textContent = AUTH_ERROR_MESSAGES[errorCode] || 'Sign-in failed. Try again.';
  errEl.hidden = false;
}

// GET /auth/providers (app/oauth_api.py's list_providers()) -- only
// ever lists a provider that is actually configured
// (app/oauth.py's provider_enabled()), so an unconfigured one is never
// rendered by renderProviderButtons() below as a button that would
// 404 the moment someone clicked it.
export async function fetchProviders() {
  try {
    const res = await fetch('/auth/providers');
    if (!res.ok) return [];
    const data = await res.json();
    return Array.isArray(data && data.providers) ? data.providers : [];
  } catch (err) {
    return [];
  }
}

// Inline brand marks for the OAuth providers app/oauth.py's PROVIDERS
// can list ("github", "discord", "google" today), keyed by the
// provider's raw `name` -- never its `label`, which is free text (see
// app/oauth.py's PROVIDER_LABELS) and never guaranteed to match one of
// these keys. Every path below is copied straight from that provider's
// own published brand mark and drawn inline here as plain SVG markup --
// nothing is fetched from a CDN or any other host, so this card makes
// no request beyond GET /auth/providers itself and renders identically
// with no network reachable at all. Google's four colours are literal
// fill values on purpose (see .signin-provider-btn--google in
// account.css/join.css/link.css for why) -- the G is never recoloured
// to the page's own palette. GitHub's and Discord's marks use
// currentColor, so their colour comes from the button's own `color`
// (also set in those same three stylesheets, one brand colour each).
//
// A provider name with no entry here -- anything PROVIDERS grows later
// that nobody's added a mark for yet -- falls through the `icon`
// lookup below to the "generic" branch: still a full-width, full-height
// button with real hover/focus states and its "Sign in with {label}"
// text, just without an icon or a brand colour. It never renders an
// empty box.
const PROVIDER_ICONS = {
  github:
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path fill="currentColor" d="M8 0c-4.42 0-8 3.58-8 8a8.013 8.013 0 0 0 5.47 7.59c.4.08.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8z"/></svg>',
  google:
    '<svg viewBox="0 0 18 18" aria-hidden="true" focusable="false"><path fill="#4285F4" d="M17.64 9.2045c0-.6381-.0573-1.2518-.1636-1.8409H9v3.4814h4.8436c-.2086 1.125-.8427 2.0782-1.7959 2.7164v2.2582h2.9087c1.7018-1.5668 2.6836-3.8741 2.6836-6.6151z"/><path fill="#34A853" d="M9 18c2.43 0 4.4673-.8059 5.9564-2.1805l-2.9087-2.2582c-.8059.54-1.8368.8618-3.0477.8618-2.3436 0-4.3282-1.5818-5.0359-3.7104H.9573v2.3318C2.4382 15.9832 5.4818 18 9 18z"/><path fill="#FBBC05" d="M3.9641 10.71c-.18-.54-.2823-1.1168-.2823-1.71s.1023-1.17.2823-1.71V4.9582H.9573C.3477 6.1732 0 7.5477 0 9s.3477 2.8268.9573 4.0418L3.9641 10.71z"/><path fill="#EA4335" d="M9 3.5795c1.3214 0 2.5077.4541 3.4405 1.346l2.5814-2.5814C13.4632.8918 11.4259 0 9 0 5.4818 0 2.4382 2.0168.9573 4.9582L3.9641 7.29C4.6718 5.1614 6.6564 3.5795 9 3.5795z"/></svg>',
  discord:
    '<svg viewBox="0 0 256 199" aria-hidden="true" focusable="false"><path fill="currentColor" d="M216.856 16.597A208.502 208.502 0 0 0 164.042 0c-2.275 4.113-4.933 9.645-6.766 14.046-19.692-2.961-39.203-2.961-58.533 0-1.832-4.4-4.55-9.933-6.846-14.046a207.809 207.809 0 0 0-52.855 16.638C5.618 67.147-3.443 116.4 1.087 164.956c22.169 16.555 43.653 26.612 64.775 33.193A161.094 161.094 0 0 0 79.735 175.3a136.413 136.413 0 0 1-21.846-10.632 108.636 108.636 0 0 0 5.356-4.237c42.122 19.702 87.89 19.702 129.51 0a131.66 131.66 0 0 0 5.355 4.237 136.07 136.07 0 0 1-21.886 10.653c4.006 8.02 8.638 15.67 13.873 22.848 21.142-6.58 42.646-16.637 64.815-33.213 5.316-56.288-9.08-105.09-38.056-148.36ZM85.474 135.095c-12.645 0-23.015-11.805-23.015-26.18s10.149-26.2 23.015-26.2c12.867 0 23.236 11.804 23.015 26.2.02 14.375-10.148 26.18-23.015 26.18Zm85.051 0c-12.645 0-23.014-11.805-23.014-26.18s10.148-26.2 23.014-26.2c12.867 0 23.236 11.804 23.015 26.2 0 14.375-10.148 26.18-23.015 26.18Z"/></svg>',
};

// One <a href="/auth/{name}/start"> per entry -- that route is itself a
// GET redirect, so no click handler is needed here at all, only the
// decision (made by each caller) of which providers to pass in.
// `providers` is expected to already exclude "email" (see this file's
// own header comment) and, on frontend/link.js, the provider that
// produced the pending identity being resolved.
//
// Each button is an icon (PROVIDER_ICONS above, `aria-hidden` since
// it's purely decorative next to the visible label right beside it --
// see this file's own header comment on the "generic" fallback for
// when there is no icon) plus a `.signin-provider-label` span carrying
// the actual "{verb} {label}" text, which is also the button's whole
// accessible name -- no separate aria-label needed, and none of this
// changes what `link.textContent` reads back as. The brand colours
// themselves are CSS, not this file's concern -- see
// .signin-provider-btn--github/--google/--discord in
// account.css/join.css/link.css (each one, since this component's CSS
// is duplicated per page the same way its markup is -- see this file's
// own header comment).
//
// `verb` defaults to "Sign in with", the only wording every existing
// caller (this signed-out panel, frontend/link.js's own "sign in with
// a method you already use" choice) has ever needed. account.js's
// SIGNED-IN Sign-in methods panel is a second context for the exact
// same `/auth/{provider}/start` redirect -- app/oauth_api.py's case 2
// links the identity onto the already-logged-in account rather than
// starting a new session -- where "Sign in with GitHub" would read as
// though it logs the reader out first. That caller passes "Connect"
// instead, the same verb this page's own per-identity "Disconnect"
// button already uses for the opposite action, rather than this
// module growing a second near-identical button-builder.
export function renderProviderButtons(providers, wrap, verb = 'Sign in with') {
  wrap.replaceChildren();
  providers.forEach((p) => {
    const key = String(p.name || '').toLowerCase();
    const icon = PROVIDER_ICONS[key];
    const label = p.label || p.name;

    const link = document.createElement('a');
    link.className = icon
      ? `signin-provider-btn signin-provider-btn--${key}`
      : 'signin-provider-btn signin-provider-btn--generic';
    link.href = `/auth/${encodeURIComponent(p.name)}/start`;

    if (icon) {
      const iconWrap = document.createElement('span');
      iconWrap.className = 'signin-provider-icon';
      iconWrap.setAttribute('aria-hidden', 'true');
      iconWrap.innerHTML = icon;
      link.appendChild(iconWrap);
    }

    const text = document.createElement('span');
    text.className = 'signin-provider-label';
    text.textContent = `${verb} ${label}`;
    link.appendChild(text);

    wrap.appendChild(link);
  });
}

// Wires the submit handler for one email-address-and-submit form
// (join.html's #signin-email-form, link.html's #link-email-form,
// account.html's #account-signin-email-form). POST /auth/email/start
// is always answered with the SAME confirmation regardless of whether
// the address has an account or the mail actually went out -- see
// app/oauth_api.py's email_start() docstring for why: this response
// must never be an account-enumeration oracle. The only distinct
// outcomes handled here (rate limited, malformed address) are about
// the REQUEST, not about whether the address exists.
//
// `els` takes element references rather than relying on shared ids,
// since each page's form uses its own id prefix (f-signin-email vs.
// f-link-email vs. f-account-signin-email, etc.) -- same reasoning
// TEAM_COLORS' duplication comment gives for keeping each page
// independently correct, just applied to element wiring instead of a
// data table.
//
// On join.html and account.html this form also carries
// setupPasswordSignInForm()'s password field and submit button (see
// this file's own header comment) -- the `event.submitter !== submitBtn`
// guard below is what keeps a password-button click from also running
// the magic-link request. link.html never adds that second button, so
// there `event.submitter` is always this form's one submit button (or
// undefined, on a browser old enough to not populate it at all -- the
// guard is skipped entirely in that case, same as before this field
// existed).
export function setupEmailSignInForm(form, els) {
  if (!form) return;
  const { input, errorEl, sentEl, submitBtn } = els;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (e.submitter && submitBtn && e.submitter !== submitBtn) return;
    if (errorEl) errorEl.hidden = true;
    if (sentEl) sentEl.hidden = true;

    const email = input.value.trim();
    if (!email) {
      if (errorEl) {
        errorEl.textContent = 'Enter your email address.';
        errorEl.hidden = false;
      }
      return;
    }

    if (submitBtn) submitBtn.disabled = true;
    try {
      const res = await fetch('/auth/email/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email }),
      });

      if (res.status === 429) {
        if (errorEl) {
          errorEl.textContent = 'Too many attempts. Wait a moment and try again.';
          errorEl.hidden = false;
        }
        return;
      }
      if (res.status === 400) {
        if (errorEl) {
          errorEl.textContent = 'Enter a valid email address.';
          errorEl.hidden = false;
        }
        return;
      }
      if (!res.ok) {
        if (errorEl) {
          errorEl.textContent = 'Something went wrong. Try again in a moment.';
          errorEl.hidden = false;
        }
        return;
      }

      if (sentEl) sentEl.hidden = false;
      input.value = '';
    } catch (err) {
      if (errorEl) {
        errorEl.textContent = 'Could not reach the server. Check your connection and try again.';
        errorEl.hidden = false;
      }
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  });
}

// Wires the submit handler for password sign-in (POST
// /auth/password/start) -- the second action on the same <form>
// setupEmailSignInForm() already owns, see this file's own header
// comment for why the two share one email field and one 'submit'
// event instead of being two separate forms.
//
// `els.emailInput` is the SAME element passed to setupEmailSignInForm's
// `input` for this form -- read here, never written (only the password
// field is cleared on success), so a person who mistypes their password
// doesn't lose the address they already typed.
//
// A failed attempt (bad credentials, no password set on the account, or
// simply no such account) all come back as the exact same 401 body from
// the server -- see app/oauth_api.py's password_start() docstring for
// why that ambiguity is deliberate. The copy shown here preserves it:
// there is deliberately no separate "no account" / "no password set" /
// "wrong password" message.
export function setupPasswordSignInForm(form, els) {
  if (!form) return;
  const { emailInput, passwordInput, errorEl, submitBtn } = els;
  if (!passwordInput || !submitBtn) return;

  // Per the HTML spec, pressing Enter in ANY field submits a form via
  // its "default button" -- the FIRST submit button in tree order --
  // regardless of which field has focus. Without this, finishing a
  // password and hitting Enter would fire the magic-link button beside
  // it instead (the first button on this form), not this one. Calling
  // requestSubmit(submitBtn) dispatches a real 'submit' event with
  // event.submitter set to this button, so the guard in
  // setupEmailSignInForm's handler (and the one below) route it
  // correctly, the same as an actual click on this button would.
  passwordInput.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    if (typeof form.requestSubmit === 'function') {
      form.requestSubmit(submitBtn);
    } else {
      submitBtn.click();
    }
  });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (e.submitter && e.submitter !== submitBtn) return;
    if (errorEl) errorEl.hidden = true;

    const email = (emailInput.value || '').trim();
    if (!email) {
      if (errorEl) {
        errorEl.textContent = 'Enter your email address.';
        errorEl.hidden = false;
      }
      return;
    }
    const password = passwordInput.value;
    if (!password) {
      if (errorEl) {
        errorEl.textContent = 'Enter your password.';
        errorEl.hidden = false;
      }
      return;
    }

    submitBtn.disabled = true;
    try {
      const res = await fetch('/auth/password/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      });

      if (res.status === 429) {
        if (errorEl) {
          errorEl.textContent = 'Too many attempts. Wait a moment and try again.';
          errorEl.hidden = false;
        }
        return;
      }
      if (!res.ok) {
        // Covers the 401 "invalid email or password" (bad credentials,
        // no password set, or no such account -- all indistinguishable
        // by design, see this function's own header comment) and the
        // 400 shape-validation cases, with the SAME copy for all of
        // them: no case here may ever hint at which one happened.
        if (errorEl) {
          errorEl.textContent = 'Incorrect email or password.';
          errorEl.hidden = false;
        }
        return;
      }

      // Success is one of TWO shapes -- app/oauth_api.py's
      // password_start() either issues a session outright ("login") or,
      // when the account has TOTP two-factor active, hands off to a
      // second-factor challenge instead ("totp_required" -- see that
      // route's own docstring and app/totp_api.py's module docstring
      // for the full mechanism). The challenge cookie is already set on
      // THIS response either way this branches, so no token from the
      // body is needed here -- only where to send the browser next.
      let data = null;
      try { data = await res.json(); } catch (err) { data = null; }
      passwordInput.value = '';
      if (data && data.result === 'totp_required') {
        window.location.assign('/verify-totp');
        return;
      }
      // "login": POST /auth/password/start already set the session
      // cookie on this response (create_session + set_session_cookie --
      // see that route's own docstring), exactly like oauth_callback and
      // email_callback do for their own "login" case. Those two land a
      // completed sign-in on /account (RedirectResponse(_ACCOUNT_PAGE_PATH)
      // -- see _respond_to_callback_outcome's own docstring); a full
      // navigation there is the same landing spot for this door too,
      // rather than inventing a different one -- and it's correct
      // whether this form was reached from join.html, or was already on
      // /account itself (account.html), where it just re-loads the page
      // into its now-signed-in state.
      window.location.assign('/account');
    } catch (err) {
      if (errorEl) {
        errorEl.textContent = 'Could not reach the server. Check your connection and try again.';
        errorEl.hidden = false;
      }
    } finally {
      submitBtn.disabled = false;
    }
  });
}
