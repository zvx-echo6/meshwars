/*
 * MeshWars: /about live-numbers band.
 *
 * Enhancement only -- the page (frontend/about.html) is fully readable
 * and correct with this script never loaded at all. Every value shown
 * here already has an em dash in the markup, so a fetch failure of any
 * kind just leaves the dash in place; nothing here can produce an
 * error state or a broken layout.
 *
 * Talks to GET /api/mc/scores and GET /api/mc/players -- both existing,
 * unauthenticated, read-only endpoints (see app/mc_api.py). Fetches
 * once on load, no polling.
 *
 * SECURITY: every value this script writes comes from the server and
 * is untrusted. All of it is written via .textContent -- never
 * innerHTML/insertAdjacentHTML -- so nothing served back to us can run
 * as markup.
 */

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
