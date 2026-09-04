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

// ---- search -------------------------------------------------------------
// Built once, at load, straight from .rules-body -- this page's content
// never changes shape after load, so there is no need to ever call the
// returned rebuildIndex().
setupPageSearch({ headingsRoot: document.querySelector('.rules-body') });
