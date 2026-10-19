// =====================================================================
// frontend/rules.js -- the contents rail on /rules, plus wiring up the
// client-side search box.
//
// RAIL: one job -- keep it marking where you are. In a document this
// long the rail answers "where am I" more often than "take me
// somewhere", and a list of twelve identical links answers neither.
// IntersectionObserver rather than a scroll handler: the browser does
// the work off the main thread and hands back only the crossings, so
// there is no listener firing on every frame of a long scroll.
//
// SEARCH: the index build, matching/scoring, snippet construction and
// the results listbox live in frontend/page-search.js, shared with
// /docs and /account -- see that module's own header comment for the
// full design, including why /rules's own top-level <h2>s (which carry
// no id of their own -- the id lives on the wrapping <section> instead,
// e.g. <section id="basics"><h2>The basics</h2>) are still indexable
// without this page's existing markup having to change at all.
// =====================================================================
import { setupPageSearch } from './page-search.js?v=20260904-1';

const links = Array.from(document.querySelectorAll('.rules-toc a[href^="#"]'));
const sections = links
  .map((a) => document.getElementById(decodeURIComponent(a.hash.slice(1))))
  .filter(Boolean);

if (sections.length && 'IntersectionObserver' in window) {
  const byId = new Map(links.map((a) => [decodeURIComponent(a.hash.slice(1)), a]));
  const visible = new Set();

  function mark() {
    // The topmost section currently on screen wins. When none is (mid
    // scroll through a section taller than the viewport) the last one
    // marked simply stays marked, which is the correct answer anyway.
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
    // Discount the fixed nav bar at the top, and require a section to
    // reach the upper part of the viewport before it counts as "here" --
    // otherwise the next heading claims the rail the instant it peeks in
    // at the bottom of the screen.
    rootMargin: '-80px 0px -55% 0px',
    threshold: 0,
  });

  for (const s of sections) io.observe(s);
}

// ---- "Where it counts": net list ----------------------------------------
//
// GET /api/v1/nets fills the configured-nets table. Enhancement only,
// same contract as about.js: a failed fetch or a bad shape just shows
// the error note; nothing here throws.
//
// SECURITY: all of it comes from the server and is untrusted. Every
// element is built with document.createElement + .textContent, never
// innerHTML/insertAdjacentHTML.
(function () {
  const DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
  const BOARDS = { mc: 'MeshCore', mt: 'Meshtastic' };
  // Same mapping as app/checkin.py _TZ_DISPLAY_NAMES.
  const TZ_NAMES = {
    'America/Boise': 'Mountain',
    'America/Denver': 'Mountain',
    'America/Los_Angeles': 'Pacific',
    'America/Chicago': 'Central',
    'America/New_York': 'Eastern',
  };

  function pad2(n) {
    return String(n).padStart(2, '0');
  }

  // end_hour is inclusive through :59 (see app/checkin.py net_window_text).
  function formatHours(start, end) {
    if (start === 0 && end === 23) return 'all day';
    return pad2(start) + ':00\u2013' + pad2(end) + ':59';
  }

  function buildNetRow(net) {
    const tr = document.createElement('tr');
    const day = DAYS[net.weekday];
    const cells = [
      String(net.label),
      Object.prototype.hasOwnProperty.call(BOARDS, net.board) ? BOARDS[net.board] : String(net.board),
      day === undefined ? String(net.weekday) : day,
      formatHours(net.start_hour, net.end_hour),
      Object.prototype.hasOwnProperty.call(TZ_NAMES, net.timezone) ? TZ_NAMES[net.timezone] : String(net.timezone),
    ];
    for (const text of cells) {
      const td = document.createElement('td');
      td.textContent = text;
      tr.appendChild(td);
    }
    return tr;
  }

  async function loadNets() {
    const err = document.getElementById('area-nets-error');
    const body = document.getElementById('area-nets-body');
    try {
      const res = await fetch('/api/v1/nets');
      if (!res.ok) throw new Error('bad status');
      const data = await res.json();
      const nets = data && data.nets;
      if (!Array.isArray(nets) || !nets.length || !body) throw new Error('bad shape');
      const rows = nets.map(buildNetRow);
      rows.forEach((r) => body.appendChild(r));
    } catch {
      if (err) err.hidden = false;
    }
  }

  loadNets();
})();

// ---- search -------------------------------------------------------------
// Built once, at load, straight from .rules-body -- this page's content
// never changes shape after load, so there is no need to ever call the
// returned rebuildIndex().
setupPageSearch({ headingsRoot: document.querySelector('.rules-body') });
