/*
 * frontend/page-search.js -- shared client-side section search for
 * /docs, /rules and /account.
 *
 * Extracted from docs.js, which originally kept its own private copy
 * of this: index build, matching, scoring, snippet construction, the
 * results listbox and its keyboard handling. That is meaningfully more
 * code -- and more subtlety -- than the scroll-spy every page script
 * here still keeps its own copy of (see docs.js's/rules.js's/
 * account.js's own header comments, and each one's copied
 * setupScrollSpy()): a scroll-spy is fifty lines that can drift
 * harmlessly if a second copy ever diverges; a search index, scorer
 * and snippet builder is the kind of thing three separate copies
 * quietly grow apart until one page's search behaves differently from
 * the other two for no reason a reader could guess. So THIS piece is
 * shared, the same way frontend/signin-email.js already is for every
 * page offering sign-in; the scroll-spy stays copied, per this
 * codebase's existing convention.
 *
 * SEARCH
 * ------
 * The index is built from the calling page's own rendered DOM -- every
 * <h2 id> and <h3 id> under the given root, plus the text of whatever
 * sits between it and the next heading. There is no separate
 * hand-maintained list of "searchable sections" anywhere: the index IS
 * the page, read back out of it, so it is structurally impossible for
 * the index to name a section that doesn't exist or miss one that
 * does. A heading currently sitting under a hidden ancestor (anything
 * with the `hidden` attribute -- account.js gates whole groups of
 * panels this way while no player is linked, and gates the entire page
 * this way while signed out) is skipped, so search never offers a jump
 * to somewhere the reader can't actually see.
 *
 * Matching is deliberately simple: every word in the query has to
 * appear somewhere in a section's title+body (AND, not OR -- "switch
 * teams" should not surface every section that mentions either word
 * alone), scored so a title hit outranks a body hit and an exact-phrase
 * hit outranks a scattered one. No fuzzy matching, no external library
 * -- each of these pages is at most a few thousand words, a substring
 * scan over an array built once is instant, and stemming/fuzz would
 * risk surprising a reader more than it helps one.
 *
 * BREADCRUMB
 * ----------
 * An <h3> result is labeled with the nearest preceding <h2> in
 * document order, when that title differs from the <h3>'s own --
 * tracked with a running pointer as the heading list is walked, not by
 * looking for a wrapping <section>. docs.html happens to nest each
 * h2+h3 run inside one <section id="...">; account.html's h2 group
 * titles ("Radios & troubleshooting", "Play", "Security") are instead
 * plain siblings in front of a run of sibling
 * <section class="account-panel"> boxes, one per h3, with no shared
 * wrapping section at all. Document order is the one thing both shapes
 * share, so that -- not DOM nesting -- is what breadcrumbing is built
 * on, which is what let this move without also having to reshape
 * account.html's panel markup to match docs.html's.
 *
 * ANCHOR IDS
 * ----------
 * A result has to link somewhere real, but where the `id` actually
 * lives differs by page: every heading on /account carries its own id
 * (added for exactly this); every <h3> on /docs does too; but every
 * top-level <h2> on /docs and /rules is id-less -- the id instead sits
 * on the <section> that wraps that h2 and its prose (docs.html's own
 * <section id="start"><h2>Getting started</h2>...). Rather than move
 * or duplicate those ids to satisfy one selector, buildSearchIndex()
 * below falls back to a heading's nearest enclosing <section>'s id
 * when the heading has none of its own -- so a plain `<h2>` inside an
 * id'd `<section>` is exactly as indexable as one that carries the id
 * itself, and neither page's existing markup had to change for search
 * to reach it.
 *
 * SECURITY: every dynamic value below is set via textContent or built
 * as separate text nodes (see buildSnippetNode's <mark> wrapping)
 * rather than innerHTML -- consistent with join.js's rule, even though
 * each page's own DOM is the only source, because a search result is
 * still content assembled from strings and the rule costs nothing to
 * keep. The index is also built exclusively from the CALLING page's
 * own already-rendered DOM: on /account that is the signed-in reader's
 * own data (GET /api/account, scoped server-side to the session
 * cookie), never a separate fetch of anyone else's -- there is no code
 * path here that reads any account but the one already on screen.
 */

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

// headingsRoot: the element to search within -- each page passes its
// own prose/body container (see each page script's own call). Skips
// any heading currently sitting under a `hidden` ancestor so a
// gated-off section (account.js's applyPlayerGate()) is never offered
// as a result the reader can't actually reach, and any heading that
// resolves to no id at all (see this module's own "ANCHOR IDS" header
// comment) -- there is nowhere for a result like that to link to.
export function buildSearchIndex(headingsRoot) {
  const headings = Array.from(headingsRoot.querySelectorAll('h2, h3'))
    .filter((h) => !h.closest('[hidden]'));

  let currentH2 = null;
  const entries = [];
  for (const h of headings) {
    const isH2 = h.tagName === 'H2';
    if (isH2) currentH2 = h;

    const section = h.closest('section');
    const id = h.id || (section && section.id) || '';
    if (!id) continue;

    entries.push({
      id,
      title: (h.textContent || '').trim(),
      // Only meaningful for an <h3> whose nearest preceding <h2>
      // differs from its own title -- gives a search result like
      // "Security — Password" so two similarly-worded subsections
      // under different top-level headings read as clearly different
      // rows.
      breadcrumb: (!isH2 && currentH2 && currentH2 !== h) ? (currentH2.textContent || '').trim() : '',
      text: collectSectionText(h),
    });
  }
  return entries;
}

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

function runSearch(searchIndex, query) {
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
// <mark>, so the result row shows WHY it matched, not just that it
// did. Falls back to the plain start of the section's text (or, for a
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

// Wires up the search box + results listbox for one page.
// `headingsRoot` scopes the index (see buildSearchIndex above);
// `inputId`/`resultsId` default to the ids every page's search markup
// uses (docs.html's original #docs-search-input/#docs-search-results
// -- rules.html and account.html copy that same markup verbatim,
// class names included, the same way docs.html itself already borrows
// rules.css's .rules-toc/.rules-body class names for its own contents
// rail rather than inventing page-specific ones).
//
// `handleInitialHash` (default on) gives a direct #anchor page load
// (someone pastes a link to a specific subsection) the same brief
// landing highlight a search jump gets. docs.html and rules.html both
// want that handled right here, at setup time, because their content
// is already in the DOM and visible the instant this runs. account.js
// passes false and does its own version instead, later -- its content
// sits behind a `hidden` #account-content until GET /api/account
// resolves, so a highlight added here, now, would land on something
// invisible and have faded again long before there's anything to see;
// see that page's own refreshAccountNav() comment for where and why it
// retries this once its content is actually showing.
//
// Returns { rebuildIndex } so a page whose content arrives after load
// (account.js) can re-scan the DOM once real data has rendered, rather
// than searching whatever was there the instant this ran -- see that
// page's own boot()/applyPlayerGate()/refreshAccountNav() comments for
// exactly when it calls this back.
export function setupPageSearch({ headingsRoot, inputId = 'docs-search-input', resultsId = 'docs-search-results', handleInitialHash = true } = {}) {
  const input = document.getElementById(inputId);
  const resultsEl = document.getElementById(resultsId);
  if (!input || !resultsEl || !headingsRoot) return { rebuildIndex() {} };

  let searchIndex = buildSearchIndex(headingsRoot);

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
    currentResults = runSearch(searchIndex, query);
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
  // See handleInitialHash's own doc comment above for why account.js
  // opts out of this and does it later instead.
  if (handleInitialHash && location.hash.length > 1) {
    const id = decodeURIComponent(location.hash.slice(1));
    const target = document.getElementById(id);
    if (target) {
      requestAnimationFrame(() => {
        target.classList.add('docs-search-landed');
        setTimeout(() => target.classList.remove('docs-search-landed'), 1600);
      });
    }
  }

  return {
    rebuildIndex() {
      searchIndex = buildSearchIndex(headingsRoot);
    },
  };
}
