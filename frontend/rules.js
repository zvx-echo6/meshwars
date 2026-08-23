// =====================================================================
// frontend/rules.js -- the contents rail on /rules.
//
// One job: keep the rail marking where you are. In a document this long
// the rail answers "where am I" more often than "take me somewhere", and
// a list of twelve identical links answers neither.
//
// IntersectionObserver rather than a scroll handler: the browser does
// the work off the main thread and hands back only the crossings, so
// there is no listener firing on every frame of a long scroll.
// =====================================================================
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
