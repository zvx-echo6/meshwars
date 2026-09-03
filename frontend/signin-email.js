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
// =====================================================================

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
export function setupEmailSignInForm(form, els) {
  if (!form) return;
  const { input, errorEl, sentEl, submitBtn } = els;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
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
