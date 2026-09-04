// =====================================================================
// frontend/docs.js -- the /docs page: the contents-rail scroll-spy
// (identical job to rules.js, copied rather than shared -- each page
// here owns its own script) plus wiring up the client-side search box.
//
// The search itself (index build, matching/scoring, snippet
// construction, the results listbox and its keyboard handling) used to
// be a private copy right here -- it now lives in
// frontend/page-search.js, shared with /rules and /account, since a
// third copy of THAT (unlike the scroll-spy below) is how three
// implementations quietly drift apart. See that module's own header
// comment for the full design; this page just calls it with its own
// .docs-body as the root to index.
// =====================================================================
import { setupPageSearch } from './page-search.js?v=20260904-1';

// ---- contents-rail scroll-spy (copied from rules.js) ------------------
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

// ---- search -------------------------------------------------------------
// Built once, at load, straight from .docs-body -- this page's content
// never changes shape after load, unlike /account, so there is no need
// to ever call the returned rebuildIndex().
setupPageSearch({ headingsRoot: document.querySelector('.docs-body') });
