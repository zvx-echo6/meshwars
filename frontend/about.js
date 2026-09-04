/*
 * MeshWars: /about live-numbers band, plus the contents-rail scroll-spy
 * this page carries the same way /docs, /rules and /account each keep
 * their own copy (see rules.js's own header comment for why that one
 * piece is copied rather than shared). This page is read top to
 * bottom, not searched, so unlike those three it does not load
 * frontend/page-search.js or carry a search box.
 *
 * The live-numbers band is an enhancement only -- the page
 * (frontend/about.html) is fully readable and correct with this script
 * never loaded at all. Every value shown here already has an em dash
 * in the markup, so a fetch failure of any kind just leaves the dash
 * in place; nothing here can produce an error state or a broken
 * layout. Talks to GET /api/mc/scores and GET /api/mc/players -- both
 * existing, unauthenticated, read-only endpoints (see app/mc_api.py).
 * Fetches once on load, no polling.
 *
 * SECURITY: every value this script writes comes from the server and
 * is untrusted. All of it is written via .textContent -- never
 * innerHTML/insertAdjacentHTML -- so nothing served back to us can run
 * as markup.
 */

// ---- contents-rail scroll-spy (copied from rules.js/docs.js) ---------
(function setupScrollSpy() {
  const links = Array.from(document.querySelectorAll('.rules-toc a[href^="#"]'));
  const sections = links
    .map((a) => document.getElementById(decodeURIComponent(a.hash.slice(1))))
    .filter(Boolean);

  if (!sections.length || !('IntersectionObserver' in window)) return;

  const byId = new Map(links.map((a) => [decodeURIComponent(a.hash.slice(1)), a]));
  const visible = new Set();

  function mark() {
    if (!visible.size) return;
    const top = sections.find((s) => visible.has(s.id));
    if (!top) return;
    for (const a of links) a.classList.remove('current');
    const a = byId.get(top.id);
    if (a) a.classList.add('current');
  }

  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting) visible.add(e.target.id);
      else visible.delete(e.target.id);
    }
    mark();
  }, {
    rootMargin: '-80px 0px -55% 0px',
    threshold: 0,
  });

  for (const s of sections) io.observe(s);
})();

(function () {
  const DASH = '–';

  const els = {
    squares: document.getElementById('stat-squares'),
    players: document.getElementById('stat-players'),
    ends: document.getElementById('stat-ends'),
  };

  function setDash(...keys) {
    for (const key of keys) {
      const el = els[key];
      if (el) el.textContent = DASH;
    }
  }

  function formatEndsAt(ts) {
    if (typeof ts !== 'number' || !Number.isFinite(ts)) return null;
    const d = new Date(ts * 1000);
    if (Number.isNaN(d.getTime())) return null;
    return d.toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    });
  }

  async function loadScores() {
    try {
      const res = await fetch('/api/mc/scores');
      if (!res.ok) return setDash('squares', 'ends');
      const data = await res.json();

      const teams = Array.isArray(data && data.teams) ? data.teams : [];
      const total = teams.reduce((sum, t) => {
        const n = Number(t && t.tiles);
        return sum + (Number.isFinite(n) ? n : 0);
      }, 0);
      if (els.squares) els.squares.textContent = String(total);

      const ends = formatEndsAt(data && data.ends_at);
      if (els.ends) els.ends.textContent = ends !== null ? ends : DASH;
    } catch {
      setDash('squares', 'ends');
    }
  }

  async function loadPlayers() {
    try {
      const res = await fetch('/api/mc/players');
      if (!res.ok) return setDash('players');
      const data = await res.json();
      if (els.players) {
        els.players.textContent = Array.isArray(data) ? String(data.length) : DASH;
      }
    } catch {
      setDash('players');
    }
  }

  loadScores();
  loadPlayers();
})();
