// =====================================================================
// frontend/theme-toggle.js -- the skin switch in the nav bar.
//
// Two halves, and the order matters. The first is an inline snippet each
// page runs in <head>, BEFORE any stylesheet paints -- see THEME_BOOT
// below and the copy of it in every page's <head>. Reading localStorage
// from a deferred module instead would paint gold first and repaint neon
// a frame later, which is the flash every theme switcher is judged on.
//
// The second half is this file: it binds the button, flips the
// attribute, and remembers the choice. Nothing here knows a colour --
// theme.css owns every value, and a theme is one block of tokens. That
// is the whole reason this file is twenty lines rather than a parallel
// stylesheet.
//
// Gold is the default and stays unnamed in storage: only an explicit
// choice of neon is written, so a reader who never touches the button is
// never carrying a preference they did not make.
// =====================================================================
const KEY = 'mwTheme';
const NEON = 'neon';

// Kept in sync with each page's <head> snippet by hand -- there are only
// four pages, and the alternative (fetching a script before first paint)
// is exactly the blocking request the inline copy exists to avoid.
export function currentTheme() {
  return document.documentElement.getAttribute('data-theme') === NEON ? NEON : 'gold';
}

function apply(theme) {
  const neon = theme === NEON;
  document.documentElement.setAttribute('data-theme', neon ? NEON : 'gold');
  try {
    if (neon) localStorage.setItem(KEY, NEON);
    else localStorage.removeItem(KEY);
  } catch (e) { /* private mode: the choice just does not persist */ }

  // The address bar and task switcher paint from this, not from the
  // stylesheet, so a themed page with an unthemed browser chrome looks
  // half-finished on a phone. Values match --mw-ink in each theme.
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', neon ? '#05080D' : '#0C0B0A');

  const btn = document.getElementById('mw-theme-btn');
  if (btn) {
    btn.setAttribute('aria-pressed', String(neon));
    btn.setAttribute('title', neon ? 'Switch to the gold theme' : 'Switch to the neon theme');
    btn.setAttribute('aria-label', btn.getAttribute('title'));
  }
}

const btn = document.getElementById('mw-theme-btn');
if (btn) {
  btn.addEventListener('click', () => {
    apply(currentTheme() === NEON ? 'gold' : NEON);
  });
}

// Run once on load so the button's pressed state and the theme-color
// meta agree with whatever the <head> snippet already applied.
apply(currentTheme());
