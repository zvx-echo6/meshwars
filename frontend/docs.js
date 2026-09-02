// =====================================================================
// frontend/docs.js -- the /docs page: the contents-rail scroll-spy
// (identical job to rules.js, copied rather than shared -- each page
// here owns its own script) plus the client-side search box.
//
// SEARCH
// ------
// The index is built once, at load, straight from this page's own
// rendered DOM -- every <h2 id> and <h3 id> under .docs-body, plus the
// text of whatever sits between it and the next heading. There is no
// separate hand-maintained list of "searchable sections" anywhere: the
// index IS the page, read back out of it, so it is structurally
// impossible for the index to name a section that doesn't exist or
// miss one that does. Add a new <h3 id="..."> with a heading and some
// prose and it is searchable on the next load, with no other change
// required anywhere.
//
// Matching is deliberately simple: every word in the query has to
// appear somewhere in a section's title+body (AND, not OR -- "switch
// teams" should not surface every section that mentions either word
// alone), scored so a title hit outranks a body hit and an exact-phrase
// hit outranks a scattered one. No fuzzy matching, no external library
// -- the page is a few thousand words, a substring scan over an array
// built once is instant, and stemming/fuzz would risk surprising a
// reader more than it helps one.
//
// SECURITY: every dynamic value below is set via textContent or built
// as separate text nodes (see buildSnippet's <mark> wrapping) rather
// than innerHTML -- consistent with join.js's rule, even though this
// page's own DOM is the only source, because a search result is still
// content assembled from strings and the rule costs nothing to keep.
// =====================================================================

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

// ---- search index, built from the DOM ----------------------------------

const MAX_RESULTS = 8;
const SNIPPET_RADIUS = 90; // characters shown either side of a body-text match

function collectSectionText(heading) {
  let text = '';
  let node = heading.nextElementSibling;
  while (node && !/^H[23]$/.test(node.tagName)) {
    text += ' ' + (node.textContent || '');
    node = node.nextElementSibling;
  }
  return text.replace(/\s+/g, ' ').trim();
}

function buildSearchIndex() {
  const headings = Array.from(document.querySelectorAll('.docs-body h2[id], .docs-body h3[id]'));
  return headings.map((h) => {
    const section = h.closest('section');
    const parentH2 = section ? section.querySelector(':scope > h2') : null;
    const isSub = h.tagName === 'H3';
    return {
      id: h.id,
      title: (h.textContent || '').trim(),
      // Only meaningful for an <h3> whose own section title differs --
      // gives a search result like "Your account" above "Switching
      // teams" so two similarly-worded subsections under different
      // top-level sections read as clearly different rows.
      breadcrumb: (isSub && parentH2 && parentH2 !== h) ? (parentH2.textContent || '').trim() : '',
      text: collectSectionText(h),
    };
  });
}

let searchIndex = [];

function scoreEntry(entry, words, phrase) {
  const titleLower = entry.title.toLowerCase();
  const bodyLower = entry.text.toLowerCase();
  const hay = titleLower + ' ' + bodyLower;
  if (!words.every((w) => hay.includes(w))) return null;

  let score = 0;
  if (titleLower.includes(phrase)) score += 100;
  else if (bodyLower.includes(phrase)) score += 20;
  for (const w of words) {
    if (titleLower.includes(w)) score += 10;
  }
  return score;
}

function runSearch(query) {
  const phrase = query.trim().toLowerCase();
  if (!phrase) return [];
  const words = phrase.split(/\s+/).filter(Boolean);
  const results = [];
  for (const entry of searchIndex) {
    const score = scoreEntry(entry, words, phrase);
    if (score !== null) results.push({ entry, score });
  }
  results.sort((a, b) => b.score - a.score);
  return results.slice(0, MAX_RESULTS).map((r) => r.entry);
}

// Builds a snippet <span> with the first matching word wrapped in
// <mark>, so the result row shows WHY it matched, not just that it did.
// Falls back to the plain start of the section's text (or, for a
// section with no body copy of its own -- a heading immediately
// followed by another heading -- the title again) when the query only
// matched the title.
function buildSnippetNode(entry, words) {
  const span = document.createElement('span');
  span.className = 'docs-search-result-snippet';

  const text = entry.text;
  if (!text) {
    span.textContent = entry.title;
    return span;
  }

  const lower = text.toLowerCase();
  let hitAt = -1;
  let hitLen = 0;
  for (const w of words) {
    const idx = lower.indexOf(w);
    if (idx !== -1 && (hitAt === -1 || idx < hitAt)) {
      hitAt = idx;
      hitLen = w.length;
    }
  }

  if (hitAt === -1) {
    // Matched only in the title (or the breadcrumb) -- lead with the
    // section's own opening text instead of an unrelated mid-sentence
    // fragment.
    const lead = text.slice(0, SNIPPET_RADIUS * 2);
    span.textContent = lead + (text.length > lead.length ? '…' : '');
    return span;
  }

  const start = Math.max(0, hitAt - SNIPPET_RADIUS);
  const end = Math.min(text.length, hitAt + hitLen + SNIPPET_RADIUS);

  if (start > 0) span.appendChild(document.createTextNode('…'));
  span.appendChild(document.createTextNode(text.slice(start, hitAt)));
  const mark = document.createElement('mark');
  mark.textContent = text.slice(hitAt, hitAt + hitLen);
  span.appendChild(mark);
  span.appendChild(document.createTextNode(text.slice(hitAt + hitLen, end)));
  if (end < text.length) span.appendChild(document.createTextNode('…'));
  return span;
}

function setupSearch() {
  const input = document.getElementById('docs-search-input');
  const resultsEl = document.getElementById('docs-search-results');
  if (!input || !resultsEl) return;

  searchIndex = buildSearchIndex();

  let activeIndex = -1;
  let currentResults = [];
  let landedTimer = null;

  function closeResults() {
    resultsEl.hidden = true;
    resultsEl.replaceChildren();
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
    activeIndex = -1;
    currentResults = [];
  }

  function applyActive() {
    const rows = resultsEl.querySelectorAll('.docs-search-result');
    rows.forEach((row, i) => {
      const isActive = i === activeIndex;
      row.classList.toggle('docs-search-active', isActive);
      if (isActive) {
        input.setAttribute('aria-activedescendant', row.id);
        row.scrollIntoView({ block: 'nearest' });
      }
    });
    if (activeIndex === -1) input.removeAttribute('aria-activedescendant');
  }

  function jumpTo(id) {
    const target = document.getElementById(id);
    if (!target) return;
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    // Focus for keyboard/screen-reader users landing mid-page, then a
    // brief highlight for sighted ones -- tabindex is removed after
    // blur so the heading doesn't become a permanent, confusing stop
    // in normal tab order.
    target.setAttribute('tabindex', '-1');
    target.focus({ preventScroll: true });
    target.addEventListener('blur', () => target.removeAttribute('tabindex'), { once: true });

    if (landedTimer) clearTimeout(landedTimer);
    target.classList.add('docs-search-landed');
    landedTimer = setTimeout(() => target.classList.remove('docs-search-landed'), 1600);

    if (history.replaceState) history.replaceState(null, '', '#' + id);
    closeResults();
    input.blur();
  }

  function renderResults(query) {
    const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    currentResults = runSearch(query);
    activeIndex = -1;
    resultsEl.replaceChildren();

    if (!words.length) {
      closeResults();
      return;
    }

    if (!currentResults.length) {
      const li = document.createElement('li');
      li.className = 'docs-search-empty';
      li.textContent = `No matches for "${query.trim()}".`;
      resultsEl.appendChild(li);
      resultsEl.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      return;
    }

    currentResults.forEach((entry, i) => {
      const li = document.createElement('li');
      li.className = 'docs-search-result';
      li.id = `docs-search-option-${i}`;
      li.setAttribute('role', 'option');

      const a = document.createElement('a');
      a.href = '#' + entry.id;
      a.addEventListener('click', (e) => {
        e.preventDefault();
        jumpTo(entry.id);
      });

      const title = document.createElement('span');
      title.className = 'docs-search-result-title';
      title.textContent = entry.breadcrumb ? `${entry.breadcrumb} — ${entry.title}` : entry.title;
      a.appendChild(title);
      a.appendChild(buildSnippetNode(entry, words));

      li.appendChild(a);
      resultsEl.appendChild(li);
    });

    resultsEl.hidden = false;
    input.setAttribute('aria-expanded', 'true');
  }

  input.addEventListener('input', () => renderResults(input.value));

  input.addEventListener('keydown', (e) => {
    if (resultsEl.hidden && e.key !== 'Escape') return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (!currentResults.length) return;
      activeIndex = (activeIndex + 1) % currentResults.length;
      applyActive();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (!currentResults.length) return;
      activeIndex = (activeIndex - 1 + currentResults.length) % currentResults.length;
      applyActive();
    } else if (e.key === 'Enter') {
      if (!currentResults.length) return;
      e.preventDefault();
      const pick = currentResults[activeIndex === -1 ? 0 : activeIndex];
      jumpTo(pick.id);
    } else if (e.key === 'Escape') {
      closeResults();
      input.blur();
    }
  });

  input.addEventListener('focus', () => {
    if (input.value.trim()) renderResults(input.value);
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.docs-search-wrap')) closeResults();
  });

  // A direct #anchor load (someone pastes a link to a specific
  // subsection) gets the same brief landing highlight a search jump
  // does, for the same reason -- confirming arrival on a long page.
  if (location.hash.length > 1) {
    const id = decodeURIComponent(location.hash.slice(1));
    const target = document.getElementById(id);
    if (target) {
      requestAnimationFrame(() => {
        target.classList.add('docs-search-landed');
        setTimeout(() => target.classList.remove('docs-search-landed'), 1600);
      });
    }
  }
}

setupSearch();
