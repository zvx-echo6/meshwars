// =====================================================================
// frontend/signin-email.js -- the sign-in-with-a-provider component
// shared by every page that offers it: frontend/join.js (registration
// flow's own sign-in panel), frontend/link.js (the pending-identity
// screen's "sign in with a method you already use" choice), and
// frontend/account.js (the signed-out /account page's welcome panel).
//
// One shared module, included alongside those pages' own scripts --
// same exception to the "self-contained, no shared import" convention
// frontend/nav-auth.js already carries, and for the same reason: there
// is only one correct way to render GET /auth/providers as a row of
// sign-in buttons plus the "email" entry's address-and-submit form, and
// unlike TEAM_COLORS (which IS duplicated per page script, on purpose,
// so each page stays loadable on its own), this piece has nothing
// page-specific in it to justify a third (now fourth) copy -- see
// nav-auth.js's own header comment. Provider display labels are never
// duplicated anywhere in the frontend either: app/oauth.py's
// PROVIDER_LABELS is the single source of truth, and every API
// response that names a provider (GET /auth/providers, GET
// /api/account, GET /api/account/pending) already carries the label
// alongside the raw name.
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
// join.js and account.js wire it in -- frontend/link.js's "already have
// an account" panel deliberately does NOT, because POST
// /auth/password/start only ever authenticates an existing account and
// never links a pending identity onto it (see that route's own
// docstring), which is the entire reason that panel exists.
// =====================================================================

// See this file's own header comment above for why password sign-in,
// unlike every provider GET /auth/providers can list, is never
// conditionally hidden.
export const PASSWORD_SIGNIN_AVAILABLE = true;

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

// One plain <a href="/auth/{name}/start"> per entry -- that route is
// itself a GET redirect, so no click handler is needed here at all,
// only the decision (made by each caller) of which providers to pass
// in. `providers` is expected to already exclude "email" (see this
// file's own header comment) and, on frontend/link.js, the provider
// that produced the pending identity being resolved.
export function renderProviderButtons(providers, wrap) {
  wrap.replaceChildren();
  providers.forEach((p) => {
    const link = document.createElement('a');
    link.className = 'signin-provider-btn';
    link.href = `/auth/${encodeURIComponent(p.name)}/start`;
    link.textContent = `Sign in with ${p.label || p.name}`;
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

      // Success: POST /auth/password/start already set the session
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
      passwordInput.value = '';
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
