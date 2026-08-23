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

function teamDot(team) {
  return `<span class="mc-dot" style="background:${TEAM_COLORS[team] || '#888'}"></span>`;
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
  const active = standings.filter((s) => s.points > 0);
  if (!active.length) {
    return '<p class="rs-empty">No ground taken and no check-ins this month.</p>';
  }
  const rows = active.map((s, i) => `<tr>
      <td class="rs-rank">${i + 1}</td>
      <td>${teamDot(s.team)}${escapeHtml(s.team)}</td>
      <td class="rs-num">${num(s.captures)}</td>
      <td class="rs-num">${num(s.checkin_points)}</td>
      <td class="rs-num rs-total">${num(s.points)}</td>
    </tr>`).join('');
  return `<table class="rs-table">
    <thead><tr>
      <th>#</th><th>Team</th>
      <th class="rs-num">Captures</th>
      <th class="rs-num">Check-ins</th>
      <th class="rs-num">Points</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function renderHonors(awards) {
  if (!awards.length) {
    return '<p class="rs-empty">No honors awarded yet this month.</p>';
  }
  // Per-team awards carry a scope; they read as "Top Attacker, RED"
  // rather than as seven separate award names.
  return `<ul class="rs-honors">${awards.map((a) => {
    const who = a.player || a.team || '—';
    const scope = a.scope ? ` <span class="rs-scope">${teamDot(a.scope)}${escapeHtml(a.scope)}</span>` : '';
    const team = a.player && a.team ? ` <span class="rs-scope">${teamDot(a.team)}${escapeHtml(a.team)}</span>` : '';
    const detail = a.detail ? `<span class="rs-detail">${escapeHtml(a.detail)}</span>`
                            : `<span class="rs-detail">${num(a.value)}</span>`;
    return `<li class="rs-honor">
      <span class="rs-honor-name">${escapeHtml(a.label)}${scope}</span>
      <span class="rs-honor-who">${escapeHtml(who)}${team}</span>
      ${detail}
    </li>`;
  }).join('')}</ul>`;
}

function renderMonth(m) {
  return `<section class="rs-month">
    <h2 class="rs-month-title">${escapeHtml(monthTitle(m.month))}</h2>
    <h3 class="rs-sub">Standings</h3>
    ${renderStandings(m.standings || [])}
    <h3 class="rs-sub">Honors</h3>
    ${renderHonors(m.awards || [])}
  </section>`;
}

// The month in progress is never rendered -- see app/results.py for why.
// All this says is when it closes, so the page is not silent about the
// month everyone is currently playing.
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
  try {
    const res = await fetch(spec.endpoint);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const months = Array.isArray(data.months) ? data.months : [];
    const open = renderOpenMonth(data);
    monthsEl.innerHTML = months.length
      ? open + months.map(renderMonth).join('')
      : open + '<p class="rs-empty">No month has finished yet. The first result lands when this one does.</p>';
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
