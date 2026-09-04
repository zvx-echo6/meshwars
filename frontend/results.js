// =====================================================================
// frontend/results.js -- the /results page.
//
// Renders what app/results.py computes: for each calendar month, the
// team standings and the honors. One board at a time, chosen by the
// toggle and remembered locally, the same MeshCore/Meshtastic split
// every other page uses.
//
// The endpoints differ in shape not at all -- /api/mc/results and
// /api/results both return the same object: finished months, newest
// first, plus when the month in progress closes. Everything below is
// written against that one shape, so neither board is special-cased.
//
// The open month is never rendered as a result. See app/results.py.
// =====================================================================

// Duplicated from mc.js rather than imported: these are gameplay
// constants, mc.js is a page script rather than a module anything
// imports, and a build step to share seven hex values would cost more
// than it saves. If a team colour ever changes, it changes in both.
const TEAM_COLORS = {
  RED: '#ff4136',
  GREEN: '#2ecc40',
  BLUE: '#3d8bfd',
  PURPLE: '#b10dc9',
  YELLOW: '#ffdc00',
  ORANGE: '#ff9020',
  PINK: '#f01ec0',
};

const BOARDS = {
  meshcore: { endpoint: '/api/mc/results', label: 'MeshCore' },
  meshtastic: { endpoint: '/api/results', label: 'Meshtastic' },
};

// Honors that happened SOMEWHERE, so the row can link onto the map.
// Mirrors app/results.GEOMETRIC_AWARDS -- if that grows, grow this too;
// a key listed here that the backend has no geometry for just 404s and
// the link is dropped rather than breaking the row.
const MAPPABLE_AWARDS = ['longest_road', 'frontier', 'tourist', 'park_hopper', 'peak_tagger'];

const MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
                     'July', 'August', 'September', 'October', 'November', 'December'];

const monthsEl = document.getElementById('rs-months');

function escapeHtml(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

function monthTitle(month) {
  const y = month.slice(0, 4);
  const m = parseInt(month.slice(5, 7), 10);
  return `${MONTH_NAMES[m - 1] || month} ${y}`;
}

// The month's name on its own, for prose that already sits under a
// heading carrying the year.
function monthName(month) {
  const m = parseInt(month.slice(5, 7), 10);
  return MONTH_NAMES[m - 1] || month;
}

function teamDot(team) {
  return `<span class="mc-dot" style="background:${TEAM_COLORS[team] || '#888'}"></span>`;
}

// Any name that belongs to a team is written in that team's colour --
// team names and player names alike, everywhere they appear. The colour
// IS the team, so it never has to be spelled out beside the name.
// `title` keeps it recoverable for anyone who cannot separate seven
// colours by eye.
function teamName(name, team) {
  const c = TEAM_COLORS[team];
  if (!c) return escapeHtml(name);
  return `<span class="rs-teamed" style="color:${c}" title="${escapeHtml(team)}">${escapeHtml(name)}</span>`;
}

// A number that is whole reads as whole. Check-in points are always
// whole today, but the column is REAL and a future half-point bonus
// should not make every other row grow a ".0".
function num(v) {
  const n = Number(v) || 0;
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

function renderStandings(standings) {
  // Teams that did nothing at all are dropped rather than listed on
  // zero. The map panel zero-fills because it reports a live season
  // where a team may yet appear; a finished month is a record of what
  // happened, and seven rows of nothing buries the three that matter.
  // A team is "active" if it did ANY of the three, so a team that only
  // ran the net still appears -- an honor can otherwise name a team the
  // standings above it never mentioned.
  const active = standings.filter(
    (s) => (s.squares || 0) > 0 || (s.checkin_points || 0) > 0 || (s.explorer_points || 0) > 0);
  if (!active.length) {
    return '<p class="rs-empty">No ground held, no check-ins and no places visited this month.</p>';
  }
  // Squares alone place a team -- see app/results.py. The other two
  // columns are work worth showing, not score, so nothing is totalled
  // here: a combined figure would put a team ahead on ground it does
  // not hold, which is exactly what this page used to do.
  const rows = active.map((s, i) => `<tr>
      <td class="rs-rank">${i + 1}</td>
      <td>${teamDot(s.team)}${teamName(s.team, s.team)}</td>
      <td class="rs-num rs-total">${num(s.squares)}</td>
      <td class="rs-num">${num(s.checkin_points)}</td>
      <td class="rs-num">${num(s.explorer_points)}</td>
    </tr>`).join('');
  return `<table class="rs-table">
    <thead><tr>
      <th>#</th><th>Team</th>
      <th class="rs-num">Squares</th>
      <th class="rs-num">Check-ins</th>
      <th class="rs-num">Exploration</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}


function renderHonors(awards, board, month) {
  if (!awards.length) {
    return '<p class="rs-empty">No honors awarded yet this month.</p>';
  }
  // Per-team awards carry a scope; they read as "Top Attacker, RED"
  // rather than as seven separate award names.
  // The value and what it counts are both shown -- the detail sits under
  // the award name rather than replacing the number, because "Top NetOp
  // 130" without a unit is the exact ambiguity the detail exists to fix.
  //
  // An honor nobody won still gets its row (app/results.with_placeholders):
  // player and team are both null, and it reads "not awarded" rather than
  // disappearing, which made Peak Tagger look like a missing feature.
  return `<ul class="rs-honors">${awards.map((a) => {
    const unawarded = !a.player && !a.team;
    if (unawarded) {
      return `<li class="rs-honor rs-honor-none">
        <span class="rs-honor-name">${escapeHtml(a.label)}</span>
        <span class="rs-honor-who">not awarded</span>
        <span class="rs-honor-value">&mdash;</span>
      </li>`;
    }
    const who = a.player || a.team;
    const scope = a.scope ? ` <span class="rs-scope">${teamDot(a.scope)}${teamName(a.scope, a.scope)}</span>` : '';
    const detail = a.detail ? `<span class="rs-detail">${escapeHtml(a.detail)}</span>` : '';
    // Only the award NAME is the link, not the whole row: the row also
    // carries a player and a team, and making all of it clickable would
    // suggest those go somewhere too.
    const mappable = MAPPABLE_AWARDS.indexOf(a.award) !== -1 && board && month;
    const href = mappable
      ? `/map2?board=${encodeURIComponent(board)}&month=${encodeURIComponent(month)}&award=${encodeURIComponent(a.award)}`
      : '';
    const name = mappable
      ? `<a class="rs-honor-link" href="${href}" title="Show this on the map">${escapeHtml(a.label)}</a>`
      : escapeHtml(a.label);
    return `<li class="rs-honor">
      <span class="rs-honor-name">${name}${scope}${detail}</span>
      <span class="rs-honor-who">${teamName(who, a.team)}</span>
      <span class="rs-honor-value">${num(a.value)}</span>
    </li>`;
  }).join('')}</ul>`;
}


// Every team gets its own Top Attacker and Top Defender, so as honor
// rows they are up to fourteen lines repeating two award names and two
// detail strings. The facts are a comparison between teams, so they are
// rendered as one row per team instead. Only the two team awards move:
// anything else scoped stays an honor, so a future scoped award is not
// silently swallowed by a table with no column for it.
const TEAM_AWARDS = ['team_builder', 'team_attacker', 'team_defender'];

function splitAwards(awards) {
  const league = [];
  const byTeam = new Map();
  awards.forEach((a) => {
    if (a.scope && TEAM_AWARDS.indexOf(a.award) !== -1) {
      const row = byTeam.get(a.scope) || {};
      row[a.award] = a;
      byTeam.set(a.scope, row);
    } else {
      league.push(a);
    }
  });
  return { league, byTeam };
}

// A team can hold one of the two and not the other -- a team that took
// ground but never took any back has an attacker and no defender. The
// gap is a fact about the month, so it is drawn as a dash rather than
// left blank, which would read as a rendering fault.
function awardCells(a) {
  if (!a) {
    return '<td class="rs-none">&mdash;</td><td class="rs-num rs-none">&mdash;</td>';
  }
  const who = a.player || a.team || '\u2014';
  // Plain, NOT team-coloured. Every name in a By team row belongs to the
  // same team, so colouring all three said it three more times and the
  // row became a block of one hue. The row's team is carried once, by
  // the first cell and the coloured edge on the row (results.css).
  return `<td>${escapeHtml(who)}</td><td class="rs-num">${num(a.value)}</td>`;
}

function renderTeamAwards(byTeam, standings) {
  if (!byTeam.size) return '';
  // Same order as the standings table directly above, so the eye tracks
  // between the two. Any team holding an award but absent from the
  // standings follows, rather than being dropped.
  const order = [];
  standings.forEach((s) => { if (byTeam.has(s.team)) order.push(s.team); });
  byTeam.forEach((_row, team) => { if (order.indexOf(team) === -1) order.push(team); });

  const rows = order.map((team) => {
    const row = byTeam.get(team) || {};
    const edge = TEAM_COLORS[team] || '#888';
    return `<tr class="rs-team-row" style="--rs-team-edge:${edge}">
      <td class="rs-team-cell">${teamDot(team)}${teamName(team, team)}</td>
      ${awardCells(row.team_builder)}
      ${awardCells(row.team_attacker)}
      ${awardCells(row.team_defender)}
    </tr>`;
  }).join('');

  return `<h3 class="rs-sub">By team</h3>
    <div class="rs-table-scroll">
      <table class="rs-table rs-team-table">
        <thead><tr>
          <th>Team</th>
          <th>Builder</th>
          <th class="rs-num">Held</th>
          <th>Attacker</th>
          <th class="rs-num">Taken</th>
          <th>Defender</th>
          <th class="rs-num">Retaken</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

// A month object carries preview:true only on a preview host, where
// app/config.py's results_preview_current_month is on and the open month
// is computed live rather than read back from a frozen row. Production
// never sets it, and a frozen month renders byte for byte as it always
// has -- the badge, the notice and the extra class are all empty
// strings unless the flag put them there.
// Turns a month string ("2026-08") into a stable id both the rail's
// own links and each month's <section> use -- letters/digits/hyphen
// only, so it's a safe id and URL fragment with no escaping needed.
function monthId(month) {
  return `month-${month}`;
}

function renderMonth(m, board) {
  const preview = m.preview === true;
  const cls = preview ? 'rs-month rs-month-preview' : 'rs-month';
  const badge = preview ? '<span class="rs-preview-badge">IN PROGRESS</span>' : '';
  const notice = preview
    ? `\n    <p class="rs-preview-note">Provisional &mdash; ${escapeHtml(monthName(m.month))} is still
      being played. These standings and honors are computed from data so far and will
      change before the month closes.</p>`
    : '';
  const standings = m.standings || [];
  const { league, byTeam } = splitAwards(m.awards || []);
  return `<section class="${cls}" id="${escapeHtml(monthId(m.month))}">
    <h2 class="rs-month-title">${escapeHtml(monthTitle(m.month))}${badge}</h2>${notice}
    <h3 class="rs-sub">Standings</h3>
    ${renderStandings(standings)}
    <h3 class="rs-sub">Honors</h3>
    ${renderHonors(league, board, m.month)}
    ${renderTeamAwards(byTeam, standings)}
  </section>`;
}

// ---- contents rail (built from the rendered months, not hand-typed) ---
//
// Unlike /docs and /rules, whose section list is fixed at publish time
// (a hand-typed <li> list, marked by their own copy of this scroll-spy),
// this page's months are rendered client-side and change every time the
// board toggle switches -- so the rail has to be built (and its
// scroll-spy rebuilt) AFTER load() below has actually rendered them,
// never at page load. Hidden entirely when there is nothing to list
// (no months yet, or the "couldn't load" error state) rather than
// showing an empty rail.
let rsTocObserver = null;
function buildResultsToc(months) {
  const nav = document.getElementById('rs-toc');
  const list = document.getElementById('rs-toc-list');
  if (!nav || !list) return;

  nav.hidden = !months.length;
  list.replaceChildren(...months.map((m) => {
    const li = document.createElement('li');
    const a = document.createElement('a');
    a.href = '#' + monthId(m.month);
    a.textContent = monthTitle(m.month);
    li.appendChild(a);
    return li;
  }));

  if (rsTocObserver) {
    rsTocObserver.disconnect();
    rsTocObserver = null;
  }
  const links = Array.from(list.querySelectorAll('a[href^="#"]'));
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
  rsTocObserver = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting) visible.add(e.target.id);
      else visible.delete(e.target.id);
    }
    mark();
  }, {
    rootMargin: '-80px 0px -55% 0px',
    threshold: 0,
  });
  for (const s of sections) rsTocObserver.observe(s);
}

// The month in progress carries no result -- see app/results.py for why.
// All this says is when it closes, so the page is not silent about the
// month everyone is currently playing. (A preview host additionally
// renders that month's live figures above; this banner stays either
// way, since the closing date is the part that is not provisional.)
function renderOpenMonth(data) {
  if (!data.open_month || !data.open_month_closes_at) return '';
  const left = data.open_month_closes_at * 1000 - Date.now();
  if (left <= 0) return '';
  const days = Math.ceil(left / 86400000);
  const when = days === 1 ? 'today' : `in ${days} days`;
  return `<p class="rs-open">${escapeHtml(monthTitle(data.open_month))} is still being played
    &mdash; it closes ${when}, and its result is judged then.</p>`;
}

async function load(board) {
  const spec = BOARDS[board] || BOARDS.meshcore;
  monthsEl.innerHTML = '<p class="rs-loading">Loading results&hellip;</p>';
  buildResultsToc([]);
  try {
    const res = await fetch(spec.endpoint);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const months = Array.isArray(data.months) ? data.months : [];
    const open = renderOpenMonth(data);
    monthsEl.innerHTML = months.length
      ? open + months.map((m) => renderMonth(m, board)).join('')
      : open + '<p class="rs-empty">No month has finished yet. The first result lands when this one does.</p>';
    buildResultsToc(months);
  } catch (err) {
    monthsEl.innerHTML = `<p class="rs-empty">Couldn't load results: ${escapeHtml(err.message)}</p>`;
  }
}

function setBoard(board) {
  document.querySelectorAll('.rs-board-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.board === board);
  });
  try { localStorage.setItem('resultsBoard', board); } catch (e) { /* private mode */ }
  load(board);
}

document.querySelectorAll('.rs-board-btn').forEach((btn) => {
  btn.addEventListener('click', () => setBoard(btn.dataset.board));
});

let initial = 'meshcore';
try {
  const saved = localStorage.getItem('resultsBoard');
  if (saved && BOARDS[saved]) initial = saved;
} catch (e) { /* private mode */ }
setBoard(initial);
