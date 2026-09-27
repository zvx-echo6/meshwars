'use strict';
// =====================================================================
// The operator/admin panel. Split out of admin.html along with
// admin.css.
//
// Session-authenticated, not a token typed into this page -- every
// /api/admin/* call below rides the same mw_session cookie the rest of
// the site already uses (app/sessions.py), sent automatically by the
// browser on every same-origin fetch(), so there is nothing here to
// hold in a variable or protect from a shared laptop the way the old
// admin-token box had to. Whether this account can see anything at all
// is decided server-side, on every single request, by app/admin_api.py's
// _role_guard() -- this file's own gating (checkAccess() below) is a
// UX nicety (don't show empty panels to someone who will just get 401s),
// never the actual security boundary. The one exception is
// settings.admin_require_auth being off (see checkAccess()'s own
// comment) -- there, the server-side boundary IS "none," and this
// file's job shifts to making that state impossible to miss rather
// than hiding a panel nothing is protecting anyway.
//
// Everything below is plain DOM. No templating and no innerHTML with
// data in it: player names and labels are operator-supplied and
// third-party-supplied respectively, and textContent cannot be talked
// into running anything.
// =====================================================================

let myRole = null;         // null | 'admin' | 'operator' -- from GET /api/account, or synthetic 'operator' when admin_require_auth is off (see checkAccess())
let allPlayers = [];
let expanded = new Set();   // player ids left open across a refresh
let allAccounts = [];       // GET /api/admin/accounts -- every account, not just linked ones
let allCommunities = [];    // GET /api/admin/communities
let expandedConnections = new Set(); // 'net-'+id / 'source-'+id left open across a refresh, Connections list
let expandedCommunities = new Set(); // community ids left open across a refresh

// Flips true the moment any /api/admin/* call first succeeds, and back
// to false whenever showNoAccess() runs. This is the one piece of
// state that can tell apart the two things a 404 means below -- see
// api()'s own comment for why the response body cannot.
let panelLoaded = false;

// True for as long as the pointer is inside the traffic chart -- set by
// trafficChart()'s own mouseenter/mouseleave. pollTraffic() (below
// loadTraffic()) checks this before every tick: the chart's crosshair
// and tooltip live only in that SVG's in-memory state, not CSS, so
// re-running renderTraffic() out from under a mid-hover pointer would
// silently drop them even though the pointer never moved. Skipping the
// tick is enough -- the next one, 30s later, picks it up once the
// operator has moved on.
let trafficHovering = false;

// Same seven teams settings.teams_list serves and the join page's own
// team-picker offers (frontend/join.js's TEAM_ORDER) -- duplicated
// rather than imported, same reasoning as everywhere else on this site
// two frontend pages don't share a module: this page has to keep
// loading on its own. No colour mapping here (unlike join.js/mc.js) --
// the admin panel has never colour-coded teams, just the plain
// .adm-badge text already shown on each player row.
const TEAM_LIST = ['RED', 'GREEN', 'BLUE', 'PURPLE', 'YELLOW', 'ORANGE', 'PINK'];

// ---- tiny DOM helpers -------------------------------------------------

function el(tag, opts) {
  const n = document.createElement(tag);
  const o = opts || {};
  if (o.className) n.className = o.className;
  if (o.text !== undefined) n.textContent = o.text;
  if (o.type) n.type = o.type;
  if (o.placeholder) n.placeholder = o.placeholder;
  if (o.value !== undefined) n.value = o.value;
  if (o.title) n.title = o.title;
  return n;
}

function btn(text, cls, onClick) {
  const b = el('button', { className: 'adm-btn ' + (cls || ''), text: text });
  b.type = 'button';
  b.addEventListener('click', () => onClick(b));
  return b;
}

function fmtTs(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString();
}

function ago(ts) {
  if (!ts) return 'never';
  const s = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (s < 90) return s + 's ago';
  if (s < 5400) return Math.round(s / 60) + 'm ago';
  if (s < 172800) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
}

function bytes(n) {
  if (!n) return '—';
  if (n > 1e9) return (n / 1e9).toFixed(1) + ' GB';
  return (n / 1e6).toFixed(0) + ' MB';
}

// Joins several "facts" onto one line with the same "  ·  " separator
// already used throughout this file (the health line, a net's poll
// status) -- but each fact is its own node, so one of them can be an
// .adm-badge instead of plain text when it is a state worth noticing
// (never signed in, zero of something) rather than an ordinary detail.
// A badge only ever stands in for the one fact it replaces, never for
// the line -- a chip encodes state, it does not replace a sentence.
function appendFacts(container, nodes) {
  nodes.forEach((node, i) => {
    if (i > 0) container.appendChild(document.createTextNode('  ·  '));
    container.appendChild(node);
  });
}

function setStatus(msg, bad) {
  const s = document.getElementById('status');
  s.textContent = msg;
  s.className = 'adm-status' + (bad ? ' adm-status-bad' : '');
}

async function api(path, options) {
  // No X-Admin-Token header anymore -- that door is retired (see
  // app/admin_api.py's own module docstring). The session cookie rides
  // along automatically on every same-origin fetch(); nothing here has
  // to attach it.
  const resp = await fetch(path, options || {});
  let body = null;
  try { body = await resp.json(); } catch (e) { /* no body */ }
  if (!resp.ok) {
    if (resp.status === 401) {
      // The session expired or was revoked, or this account's role was
      // pulled out from under it mid-visit (an operator can revoke
      // their own role, or another operator's) -- reload straight into
      // the access screen rather than leaving stale panels on screen
      // that will now 401 on every action.
      showNoAccess();
    } else if (resp.status === 404) {
      // 404 is ambiguous by design, and deliberately so: app/admin_api.py's
      // _role_guard() returns the exact same status and the same generic
      // {"error": "not found"} body whether this deployment has no admin
      // surface at all (no token set, no account holds a role) or the
      // route below the guard simply did not find the thing it was asked
      // for -- a player, a key, a net. Making those distinguishable would
      // mean the server marking "this route exists but the guard failed"
      // in the body or the status, which is precisely the information the
      // 404 is designed to withhold from a probe (see _role_guard's own
      // docstring). So the two cases have to be told apart here, from
      // something the server never has to say: whether an admin call has
      // ever actually succeeded in this visit. Before that (panelLoaded
      // is still false, e.g. the very first load), a 404 can only mean
      // the surface itself was never there, so it blanks to the access
      // screen exactly as before. After that (the panel is already up),
      // a 404 is an ordinary lookup miss on a route that already passed
      // the guard -- leave the panel up and let the caller's own
      // try/catch show body.error, same as any other failed lookup.
      //
      // The one case this does not resolve cleanly: an operator revoking
      // the very last role-holding account (their own, or the only other
      // one) while someone else is mid-visit. From that instant the
      // surface really is disabled, but panelLoaded is already true, so
      // the next action here reads as an ordinary "not found" rather than
      // a sign-out. That is judged acceptable rather than worth chasing:
      // it is a narrow race, it never claims success (every route 404s
      // the same way, so nothing looks like it silently worked), and the
      // Refresh button (below) re-runs checkAccess() rather than just
      // refreshAll(), so the next manual refresh resolves it to the
      // correct access-revoked screen instead of leaving stale
      // "not found" text on screen indefinitely.
      if (!panelLoaded) {
        showNoAccess();
      }
    } else if (resp.status === 403) {
      // Same idea, for the one guard failure that carries its own
      // message: TOTP was disabled mid-visit (app/admin_api.py's
      // _role_guard() requires it be active on every call, not just at
      // sign-in). body.error here is _role_guard's own explanation,
      // not a generic one -- surface it rather than the fallback
      // "HTTP 403" this would otherwise throw as.
      showNoAccess((body && body.error) || 'Two-factor authentication is required to use this panel.');
    }
    throw new Error((body && body.error) || ('HTTP ' + resp.status));
  }
  panelLoaded = true;
  return body;
}

function post(path, payload) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

// ---- overview ---------------------------------------------------------

function tile(value, label, tone) {
  const t = el('div', { className: 'adm-tile' + (tone ? ' adm-tile-' + tone : '') });
  t.appendChild(el('div', { className: 'adm-tile-value', text: String(value) }));
  t.appendChild(el('div', { className: 'adm-tile-label', text: label }));
  return t;
}

function renderHealth(h, boards) {
  const wrap = document.getElementById('tiles');
  wrap.replaceChildren();

  // Four tiles, not nine. The question a tile answers has to be one
  // somebody actually asks on opening the page -- is data arriving, is
  // the poller alive, how much is broken, how long is left. Database
  // size and town-data counts are diagnostics; they live in the quiet
  // line underneath, where they cost nothing and interrupt nobody.
  const now = Math.floor(Date.now() / 1000);
  const pingAge = h.last_ping_at ? now - h.last_ping_at : null;
  wrap.appendChild(tile(h.pings_last_hour, 'pings, last hour',
    pingAge === null || pingAge > 21600 ? 'bad' : (pingAge > 3600 ? 'warn' : 'ok')));

  const poll = h.checkin_poller || {};
  const pollAge = poll.last_poll_at ? now - poll.last_poll_at : null;
  wrap.appendChild(tile(
    poll.running ? (poll.last_poll_at ? ago(poll.last_poll_at) : 'starting') : 'stopped',
    'check-in poller',
    !poll.running || (pollAge !== null && pollAge > 300) ? 'bad' : 'ok'));

  wrap.appendChild(tile(h.players_active_today, 'players active, 24h'));

  const mc = boards.find((b) => b.board === 'mc');
  const days = mc && mc.ends_at ? Math.round((mc.ends_at - now) / 86400) : null;
  wrap.appendChild(tile(days === null ? '—' : days + 'd', 'season left'));

  const bits = [
    h.pings_last_day + ' pings in 24h',
    bytes(h.database_bytes) + ' database',
    bytes(h.disk_free_bytes) + ' disk free',
    h.places_loaded ? 'town data loaded' : 'TOWN DATA MISSING',
  ];
  if (poll.last_error) bits.push('poller error: ' + poll.last_error);
  const line = document.getElementById('health-line');
  line.textContent = bits.join('  ·  ');
  line.className = 'adm-hint' +
    ((!h.places_loaded || poll.last_error) ? ' adm-status-bad' : '');
}

// One row per PROBLEM, not per player. Fourteen people with the same
// unreachable radio was fourteen identical rows carrying the same
// sentence and the same fix, which is most of why the page read as a
// wall. Grouped, that is one line saying fourteen, opening to the names.
const KIND_TITLES = {
  no_radio: 'never connected a radio',
  no_key: 'has no working key',
  no_contact_key: 'sending without a contact key',
  out_of_area: 'playing outside the map',
  no_repeaters: 'reaching nothing',
  never_accepted: 'nothing they send is counting',
  never_sent: 'connected a radio, never sent',
  wrong_owner: 'using someone else\'s radio',
  checkin_unreachable: 'cannot earn net check-ins',
  checkin_name_changed: 'MeshCore name changed recently',
  stale: 'stopped playing',
};

const openGroups = new Set();

function renderAttention(list) {
  const host = document.getElementById('attention');
  const count = document.getElementById('attention-count');
  host.replaceChildren();

  if (!list.length) {
    count.textContent = 'nothing to do';
    badge('nav-attention', '', false);
    host.appendChild(el('p', { className: 'adm-hint', text: 'Everyone is set up and sending.' }));
    return;
  }

  const groups = new Map();
  list.forEach((a) => {
    if (!groups.has(a.kind)) groups.set(a.kind, { kind: a.kind, fix: a.fix, severity: a.severity, items: [] });
    groups.get(a.kind).items.push(a);
  });
  count.textContent = list.length + ' across ' + groups.size +
    (groups.size === 1 ? ' issue' : ' issues');
  badge('nav-attention', list.length, list.some((a) => a.severity === 'bad'));

  groups.forEach((g) => {
    const open = openGroups.has(g.kind);
    const wrap = el('div', { className: 'adm-group' });

    const head = el('div', { className: 'adm-group-head' });
    head.appendChild(el('span', { className: 'adm-caret', text: open ? '▾' : '▸' }));
    head.appendChild(el('span', { className: 'adm-dot adm-dot-' + g.severity }));
    head.appendChild(el('span', { className: 'adm-group-n', text: String(g.items.length) }));
    head.appendChild(el('span', { className: 'adm-group-title', text: KIND_TITLES[g.kind] || g.kind }));
    head.appendChild(el('span', {
      className: 'adm-group-who',
      text: g.items.map((i) => i.player).join(', '),
    }));
    head.addEventListener('click', () => {
      if (open) openGroups.delete(g.kind); else openGroups.add(g.kind);
      renderAttention(list);
    });
    wrap.appendChild(head);

    if (open) {
      const body = el('div', { className: 'adm-group-body' });
      body.appendChild(el('p', { className: 'adm-hint', text: g.fix }));
      g.items.forEach((a) => {
        const row = el('div', { className: 'adm-row' });
        const info = el('div', { className: 'adm-row-info' });
        info.appendChild(el('strong', { text: a.player }));
        info.appendChild(el('span', { text: a.detail }));
        row.appendChild(info);
        const actions = el('div', { className: 'adm-row-actions' });
        actions.appendChild(btn('Open', 'adm-btn-quiet', () => {
          expanded.add(a.player_id);
          renderPlayers();
          const node = document.getElementById('player-' + a.player_id);
          if (node) node.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }));
        row.appendChild(actions);
        // checkin_unreachable used to offer an inline "Register" box here
        // that called POST /api/admin/checkin/binding to hand-set a
        // fallback check-in name. That route is gone -- the fix now is
        // node confirmation on the player's own account page (see the
        // group's remediation text above), or the operator adding/
        // removing the radio directly via "Add radio" in the player
        // panel, reached with the "Open" button just above.
        body.appendChild(row);
      });
      wrap.appendChild(body);
    }
    host.appendChild(wrap);
  });
}

// "Worth a look" -- DELIBERATELY separate from renderAttention() above,
// not another `kind` folded into that list: see app/admin_ops.py's own
// _worth_a_look() docstring for the full reasoning. No severity here at
// all (a hollow dot, never adm-dot-bad/warn/info) and its own count,
// which nav-attention's own badge (computed from d.attention alone,
// see renderAttention()'s `list.some((a) => a.severity === 'bad')`
// above) never sees -- this function does not touch that badge in any
// way. Each item's `detail` string is the whole observation, innocent
// reading, and consequence in one paragraph already (see the server's
// own copy) -- rendered in full, never behind an expand/collapse the
// way renderAttention()'s own groups are, since hiding the innocent
// reading behind a click is exactly the friction this block exists to
// avoid.
function renderWorthALook(list) {
  const host = document.getElementById('worth-a-look');
  const count = document.getElementById('worth-a-look-count');
  host.replaceChildren();

  if (!list.length) {
    count.textContent = '';
    host.appendChild(el('p', { className: 'adm-hint', text: 'Nothing worth a second look right now.' }));
    return;
  }

  count.textContent = String(list.length);

  list.forEach((w) => {
    const wrap = el('div', { className: 'adm-row' });
    const info = el('div', { className: 'adm-row-info' });
    info.appendChild(el('span', { className: 'adm-dot adm-dot-hollow' }));
    info.appendChild(el('strong', { text: w.player }));
    info.appendChild(el('span', { text: w.title }));
    wrap.appendChild(info);

    const actions = el('div', { className: 'adm-row-actions' });
    // No confirmation dialog, no typed-name gate -- see POST
    // /api/admin/worth-a-look/dismiss's own docstring: every other
    // confirmation in this admin surface guards an action that takes
    // something away from someone; this is the safe direction, an
    // operator saying "I looked, this is fine".
    actions.appendChild(btn('Looks fine', 'adm-btn-quiet', () => dismissWorthALook(w.player_id, w.signal)));
    wrap.appendChild(actions);
    host.appendChild(wrap);

    host.appendChild(el('p', { className: 'adm-hint', text: w.detail }));
  });
}

async function dismissWorthALook(playerId, signal) {
  try {
    await post('/api/admin/worth-a-look/dismiss', { player_id: playerId, signal: signal });
    await loadOverview();
  } catch (e) {
    setStatus('Dismiss failed: ' + e.message, true);
  }
}

async function loadOverview() {
  try {
    const d = await api('/api/admin/overview');
    renderHealth(d.health, d.boards);
    renderAttention(d.attention);
    renderWorthALook(d.worth_a_look);
    renderSeasons(d.boards);
  } catch (e) {
    setStatus('Overview failed: ' + e.message, true);
  }
}

// ---- seasons ----------------------------------------------------------

function renderSeasons(boards) {
  const host = document.getElementById('seasons');
  host.replaceChildren();
  boards.forEach((b) => {
    if (!b.season_id) return;
    const row = el('div', { className: 'adm-row' });
    const info = el('div', { className: 'adm-row-info' });
    info.appendChild(el('strong', { text: b.board === 'mc' ? 'MeshCore' : 'Meshtastic' }));
    info.appendChild(el('span', { text: 'season ' + b.season_id }));
    info.appendChild(el('span', { text: b.squares + ' squares held' }));
    info.appendChild(el('span', { text: 'ends ' + fmtTs(b.ends_at) }));
    row.appendChild(info);

    const actions = el('div', { className: 'adm-row-actions' });
    const days = el('input', { type: 'number', value: '30' });
    days.style.width = '5rem';
    actions.appendChild(el('span', { text: 'extend by' }));
    actions.appendChild(days);
    actions.appendChild(el('span', { text: 'days' }));
    actions.appendChild(btn('Apply', 'adm-btn-quiet', async (bt) => {
      const n = parseInt(days.value, 10);
      if (!n || n < 1) { setStatus('Enter a number of days', true); return; }
      bt.disabled = true;
      try {
        await post('/api/admin/season/extend',
          { season_id: b.season_id, ends_at: b.ends_at + n * 86400 });
        setStatus('Season ' + b.season_id + ' extended by ' + n + ' days', false);
        await loadOverview();
      } catch (e) {
        setStatus('Extend failed: ' + e.message, true);
        bt.disabled = false;
      }
    }));
    row.appendChild(actions);
    host.appendChild(row);
  });
}

// ---- traffic (sidebar) -------------------------------------------------
// A small persistent stat block under the section nav, not a section of
// its own -- Matt was explicit: no new nav entry, visible regardless of
// which section is open. Loaded once alongside everything else in
// refreshAll() below, same as loadOverview()/loadPlayers()/etc.
//
// See app/traffic.py's own module docstring for what GET
// /api/admin/traffic counts (salted-hash identity, the content-type
// rule for what is a page view, bot hits walled off from every human
// figure) -- this file only renders the aggregate that route already
// computed server-side.

async function loadTraffic() {
  const host = document.getElementById('traffic-body');
  try {
    const d = await api('/api/admin/traffic?days=30');
    renderTraffic(host, d);
  } catch (e) {
    // A stat panel with nothing to show is not the kind of failure
    // that should steal the status line from whatever the operator is
    // actually doing, or take the rest of the panel down with it --
    // see api()'s own comment on why 401/403 already routed elsewhere
    // above this catch ever runs. One quiet line, nothing thrown.
    host.replaceChildren(el('p', { className: 'adm-hint', text: 'Traffic unavailable' }));
  }
}

function renderTraffic(host, d) {
  host.replaceChildren();

  const daily = d.daily || [];
  // build_traffic_report() always zero-fills one entry per day in the
  // window (app/traffic.py), so this is never actually an empty array
  // for the fixed ?days=30 above -- it is here as a floor under a
  // response shape this file does not control, not a case expected to
  // fire in practice.
  if (!daily.length) {
    host.appendChild(el('p', { className: 'adm-hint', text: 'No data yet' }));
    return;
  }

  // The case that WILL fire, right after this ships: every zero-filled
  // day is genuinely zero because there is no recorded traffic at all
  // yet. Distinct from a flat but nonzero trend (a steady few visitors
  // a day for a month), which is real data and gets the chart, just
  // drawn flat -- see trafficChart()'s own `range === 0` handling.
  const totalRecorded = daily.reduce((sum, r) => sum + r.views + r.bot_views, 0);
  if (totalRecorded === 0) {
    host.appendChild(el('p', { className: 'adm-hint', text: 'No data yet' }));
    return;
  }

  const today = d.today || { uniques: 0, new_visitors: 0, views: 0 };
  host.appendChild(el('div', { className: 'adm-traffic-hero', text: String(today.uniques) }));
  host.appendChild(el('div', {
    className: 'adm-traffic-sub',
    text: today.new_visitors + ' new · ' + today.views + ' views',
  }));

  // A reserved-height row for the hover readout, ABOVE the chart and
  // BELOW the resting "N new / M views" line -- its own space, not a
  // box floated over either one. An earlier version anchored the
  // tooltip to the hovered point's x-position, directly above the
  // chart, which put it exactly on top of adm-traffic-sub (losing that
  // stat while reading this one) and risked spilling outside this
  // 180px column at the first/last day where the anchor point sits
  // right at the column's own edge. A fixed row can't do either: it
  // never overlaps another stat because a full row is reserved for it
  // whether or not anything is hovered (see .adm-traffic-tip's
  // visibility:hidden in admin.css -- hidden, not display:none, so it
  // keeps its box and nothing else shifts when it appears), and its
  // text sits inside the row's own width instead of being anchored to
  // a point that can sit anywhere from one edge of the chart to the
  // other.
  const tip = el('div', { className: 'adm-traffic-tip' });
  host.appendChild(tip);

  host.appendChild(trafficChart(daily, tip));

  const note = el('p', { className: 'adm-traffic-note' });
  note.appendChild(document.createTextNode('Bots excluded · since '));
  // A separate nowrap span for just the date -- "Bots excluded · since"
  // is free to wrap onto its own line at this column's width, but the
  // date itself (2026-08-09) must never split across the wrap point,
  // which is exactly what happened when the whole line was one plain
  // text node: the browser wrapped wherever there was space, including
  // mid-date, and rendered "2026-08-" / "09" on two lines.
  note.appendChild(el('span', { className: 'adm-traffic-date', text: d.since }));
  host.appendChild(note);
}

// Hand-written inline SVG, no chart library -- one series (unique
// visitors/day) so no legend either; the heading above already names
// it. Coordinates are drawn in a fixed viewBox and stretched to the
// sidebar's actual width by preserveAspectRatio="none" on the <svg>
// itself (set in CSS via width:100%), so the hit-test math below works
// entirely in viewBox units and never has to read the element's
// rendered size except once, in getBoundingClientRect(), to convert a
// mouse position back into that same space.
//
// `tipEl`, if given, is the reserved-row element (built in
// renderTraffic() above) this chart's hover state writes its readout
// into -- kept outside this function rather than built in here so it
// can live in the DOM between adm-traffic-sub and this chart, never on
// top of either.
//
// Density note: at 30 points across a ~160-180px column the line is
// inherently a lot of short zigzagging segments -- that is real data,
// not rendering noise, and neither the data window nor the values
// themselves are touched here to soften it (Matt was explicit: don't
// smooth the data). What IS deliberate: stroke-linejoin/linecap: round
// (admin.css) already takes the harshest edge off each angle, and the
// straight point-to-point segments below are kept straight rather than
// curve-fit through a spline -- a curve would overshoot between real
// samples and read as data between days that was never recorded, which
// is the same misrepresentation smoothing the values would cause, just
// moved into the rendering step instead of the data step. So: no
// further change beyond the two real defects (stroke width, hover
// placement) fixed elsewhere in this function.
function trafficChart(daily, tipEl) {
  const W = 300, H = 44, PAD_X = 4, PAD_Y = 5;
  const NS = 'http://www.w3.org/2000/svg';

  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('class', 'adm-traffic-chart');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label',
    'Daily unique visitors, last ' + daily.length + ' days');

  const values = daily.map((r) => r.uniques);
  const min = Math.min.apply(null, values);
  const max = Math.max.apply(null, values);
  // A flat series -- every day the same count -- has no range to
  // normalise a y-position against; dividing by (max - min) would
  // divide by zero. Drawn as a flat line at mid-height instead of
  // collapsing onto the baseline, which would otherwise misread a
  // healthy, steady site as "zero all month".
  const range = max - min;

  const points = daily.map((r, i) => ({
    x: daily.length === 1 ? W / 2 : PAD_X + (i / (daily.length - 1)) * (W - PAD_X * 2),
    y: range === 0 ? H / 2 : PAD_Y + (1 - (r.uniques - min) / range) * (H - PAD_Y * 2),
    row: r,
  }));

  // Baseline -- a single recessive rule, nothing else drawn behind the
  // line (no gridlines, no axes, per the project's chart standard).
  const base = document.createElementNS(NS, 'line');
  base.setAttribute('x1', '0');
  base.setAttribute('x2', String(W));
  base.setAttribute('y1', String(H - 1));
  base.setAttribute('y2', String(H - 1));
  base.setAttribute('class', 'adm-traffic-baseline');
  // See the path's own vector-effect comment below -- the same
  // non-uniform-scale hairline problem applies to every stroked line
  // in this chart, this one included.
  base.setAttribute('vector-effect', 'non-scaling-stroke');
  svg.appendChild(base);

  if (points.length > 1) {
    const path = document.createElementNS(NS, 'path');
    const dAttr = points
      .map((p, i) => (i === 0 ? 'M' : 'L') + p.x.toFixed(2) + ',' + p.y.toFixed(2))
      .join(' ');
    path.setAttribute('d', dAttr);
    path.setAttribute('class', 'adm-traffic-line');
    // The viewBox is 300x44 but CSS stretches the <svg> to roughly
    // 160-180px wide with preserveAspectRatio="none" (~0.55-0.6x
    // horizontally, 1x vertically, since the element's CSS height
    // matches the viewBox height exactly). A plain stroke-width is
    // drawn IN that coordinate space and then scaled along with
    // everything else, so a "2px" stroke comes out thinner on
    // near-vertical segments than near-horizontal ones -- squashed
    // by the larger of the two scale factors -- which is what made
    // the whole line read as a wispy hairline rather than a clean,
    // consistent 2px one. vector-effect="non-scaling-stroke" draws the
    // stroke AFTER the coordinate transform instead of before it, so
    // it stays a true 2 device pixels everywhere along the path
    // regardless of how the viewBox itself is stretched.
    path.setAttribute('vector-effect', 'non-scaling-stroke');
    svg.appendChild(path);
  }

  // The crosshair -- built once here and toggled in showAt()/hide()
  // below, rather than created and torn down on every move.
  const hoverLine = document.createElementNS(NS, 'line');
  hoverLine.setAttribute('y1', '0');
  hoverLine.setAttribute('y2', String(H));
  hoverLine.setAttribute('class', 'adm-traffic-hoverline');
  hoverLine.setAttribute('vector-effect', 'non-scaling-stroke');
  hoverLine.style.display = 'none';
  svg.appendChild(hoverLine);

  // The hit target is the full SVG height, not the 2px line -- a
  // transparent rect covering the whole viewBox carries the listener so
  // the pointer never has to land on the line itself.
  const hit = document.createElementNS(NS, 'rect');
  hit.setAttribute('x', '0');
  hit.setAttribute('y', '0');
  hit.setAttribute('width', String(W));
  hit.setAttribute('height', String(H));
  hit.setAttribute('fill', 'transparent');
  svg.appendChild(hit);

  const wrap = el('div', { className: 'adm-traffic-chart-wrap' });
  wrap.appendChild(svg);

  // The point marker (both the single-day static dot and the hover
  // dot) is a plain CSS-positioned <div>, not an SVG <circle> -- an SVG
  // circle lives in the same non-uniformly-scaled coordinate space the
  // path's vector-effect comment above describes, and vector-effect
  // only fixes STROKE width under that scale, not fill geometry: a
  // `<circle r="3.5">` would still render as a narrow ellipse, not a
  // dot. A CSS div sized in real pixels sidesteps the scale entirely --
  // vertical position maps 1:1 (the viewBox height and the rendered
  // height are equal by construction, see H/.adm-traffic-chart's own
  // height in admin.css), and horizontal position is a percentage of
  // the wrap's own width -- so it is a true circle at every width the
  // sidebar ever renders at, not just the one this was eyeballed in.
  const dot = el('div', { className: 'adm-traffic-dot' });
  dot.style.display = 'none';
  wrap.appendChild(dot);

  function placeDot(i) {
    const p = points[i];
    dot.style.left = ((p.x / W) * 100) + '%';
    dot.style.top = p.y + 'px';
    dot.style.display = '';
  }

  if (points.length === 1) {
    // One day of data -- a line needs two points, so this is a single
    // dot, shown permanently, rather than a degenerate/invisible path.
    // Nothing to hover into that a static dot does not already show.
    placeDot(0);
    return wrap;
  }

  function showAt(i) {
    const p = points[i];
    hoverLine.setAttribute('x1', String(p.x));
    hoverLine.setAttribute('x2', String(p.x));
    hoverLine.style.display = '';
    placeDot(i);
    if (tipEl) {
      // MM-DD, not the full YYYY-MM-DD -- the year is redundant this
      // close to today (the footnote below already anchors the window
      // with a full "since YYYY-MM-DD"), and dropping it is what keeps
      // "unique"/"new" as whole words instead of ellipsis-truncated in
      // this 180px column, at every day in the range including the
      // widest values (11 unique, 8 new -- checked against this exact
      // stub data during the fix for this defect).
      tipEl.textContent = p.row.day.slice(5) + ' — ' + p.row.uniques + ' unique, ' + p.row.new_visitors + ' new';
      tipEl.classList.add('is-active');
    }
  }

  function hide() {
    hoverLine.style.display = 'none';
    dot.style.display = 'none';
    if (tipEl) tipEl.classList.remove('is-active');
  }

  svg.addEventListener('mousemove', (ev) => {
    trafficHovering = true;
    const rect = svg.getBoundingClientRect();
    const frac = rect.width ? (ev.clientX - rect.left) / rect.width : 0;
    const idx = Math.round(frac * (points.length - 1));
    showAt(Math.max(0, Math.min(points.length - 1, idx)));
  });
  svg.addEventListener('mouseleave', () => {
    trafficHovering = false;
    hide();
  });

  return wrap;
}

// ---- traffic auto-refresh ----------------------------------------------
// The panel above loads once in refreshAll() like everything else; this
// keeps it current after that without an operator-triggered reload, by
// polling the exact same route and re-running the exact same
// renderTraffic() on a timer -- no separate live-update path (no SSE, no
// websocket) for one stat block.
const TRAFFIC_POLL_MS = 30000;
let trafficTimer = null;

async function pollTraffic() {
  // Nothing to poll for before sign-in, and no point -- api() would
  // just 401 through its own showNoAccess() handling for every tick
  // until the operator signs in, which is wasted traffic for a panel
  // nobody can see yet. showApp()/showNoAccess() start and stop this
  // timer, so myRole tracks the panel's own visibility already; this
  // check only guards the one tick that can land in between (the
  // immediate visibilitychange refresh, below).
  if (!myRole || trafficHovering) return;
  try {
    const d = await api('/api/admin/traffic?days=30');
    renderTraffic(document.getElementById('traffic-body'), d);
  } catch (e) {
    // Unlike loadTraffic()'s own catch above (the first paint, with
    // nothing on screen yet to protect), a failed background refresh
    // leaves whatever numbers are already showing exactly as they are.
    // A transient network blip is not a reason to blank a panel that
    // was working a moment ago, and the timer keeps ticking regardless
    // -- the next poll gets its own try, nothing here stops it.
  }
}

function startTrafficPolling() {
  if (trafficTimer) return; // already running -- e.g. a second showApp()
  trafficTimer = setInterval(pollTraffic, TRAFFIC_POLL_MS);
}

function stopTrafficPolling() {
  clearInterval(trafficTimer);
  trafficTimer = null;
}

// Page Visibility API: a panel left open in a background tab has no
// operator watching it, so there is no reason to hit the server every
// 30s -- paused on hidden, and given one immediate refresh on becoming
// visible again (rather than waiting up to another 30s for the next
// tick) so the operator never comes back to numbers that are already
// stale by the time they look.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    stopTrafficPolling();
  } else {
    pollTraffic();
    startTrafficPolling();
  }
});

// ---- players ----------------------------------------------------------

function renderRadio(p, r) {
  const row = el('div', { className: 'adm-row' });
  const info = el('div', { className: 'adm-row-info' });
  info.appendChild(el('span', { className: 'adm-mono', text: r.protocol + ':' + r.node_ref }));
  info.appendChild(el('span', { text: 'bound ' + fmtTs(r.bound_at) }));
  row.appendChild(info);
  const actions = el('div', { className: 'adm-row-actions' });
  actions.appendChild(btn('Remove', 'adm-btn-quiet', async (b) => {
    const typed = window.prompt('Type ' + p.display_name + ' to confirm removing ' + r.node_ref);
    if (!typed) return;
    b.disabled = true;
    try {
      await post('/api/admin/node/remove', {
        player_id: p.player_id, display_name: typed,
        protocol: r.protocol, node_ref: r.node_ref,
      });
      setStatus('Removed ' + r.node_ref, false);
      await refreshAll();
    } catch (e) { setStatus('Failed: ' + e.message, true); b.disabled = false; }
  }));
  row.appendChild(actions);
  return row;
}

function renderKey(k) {
  const row = el('div', { className: 'adm-row' });
  const info = el('div', { className: 'adm-row-info' });
  info.appendChild(el('span', { className: 'adm-mono', text: k.key_hash_prefix }));
  info.appendChild(el('span', { text: 'issued ' + fmtTs(k.issued_at) }));
  info.appendChild(el('span', { text: 'last used ' + fmtTs(k.last_seen_at) }));
  info.appendChild(el('span', {
    className: 'adm-badge ' + (k.revoked ? 'adm-badge-bad' : 'adm-badge-ok'),
    text: k.revoked ? 'revoked' : 'active',
  }));
  row.appendChild(info);
  if (!k.revoked) {
    const actions = el('div', { className: 'adm-row-actions' });
    actions.appendChild(btn('Revoke', 'adm-btn-quiet', async (b) => {
      b.disabled = true;
      try {
        await post('/api/admin/revoke', { key_hash_prefix: k.key_hash_prefix });
        setStatus('Revoked ' + k.key_hash_prefix, false);
        await refreshAll();
      } catch (e) { setStatus('Failed: ' + e.message, true); b.disabled = false; }
    }));
    row.appendChild(actions);
  }
  return row;
}

function revealKey(host, label, key) {
  host.replaceChildren();
  host.appendChild(el('div', { text: label + ' — copy it now, it is not shown again:' }));
  const box = el('input', { className: 'adm-reveal', value: key });
  box.readOnly = true;
  host.appendChild(box);
  box.focus();
  box.select();
}

function renderPlayerDetail(p) {
  const d = el('div', { className: 'adm-player-detail', });

  // Team change (POST /api/admin/player/team) -- an operator override,
  // unlimited unlike the player's own once-a-month self-switch (that
  // one lives on the Join page's setup-check panel, not here). Sits
  // with the ordinary player-management controls, right at the top
  // next to where the row above already shows this player's team
  // badge -- not in .adm-danger-zone below, and not behind a
  // window.prompt typed-name gate: a team change is fully reversible
  // by switching back, so a plain window.confirm() is enough, the same
  // light-guard weight "Add radio" below carries.
  d.appendChild(el('div', { className: 'adm-sub-title', text: 'Team' }));
  const teamRow = el('div', { className: 'adm-form' });
  const teamSelect = el('select');
  TEAM_LIST.forEach((t) => {
    const opt = el('option', { text: t, value: t });
    if (t === p.team) opt.selected = true;
    teamSelect.appendChild(opt);
  });
  teamRow.appendChild(teamSelect);
  teamRow.appendChild(btn('Change team', 'adm-btn-quiet', async (b) => {
    const newTeam = teamSelect.value;
    if (newTeam === p.team) { setStatus(p.display_name + ' is already on ' + newTeam, true); return; }
    const ok = window.confirm(
      'Move ' + p.display_name + ' from ' + p.team + ' to ' + newTeam + '?\n\n' +
      'Ground they currently hold stays with ' + p.team + '. Their points and check-in streak move with them.'
    );
    if (!ok) return;
    b.disabled = true;
    try {
      await post('/api/admin/player/team', { player_id: p.player_id, team: newTeam });
      setStatus('Moved ' + p.display_name + ' from ' + p.team + ' to ' + newTeam, false);
      await refreshAll();
    } catch (e) { setStatus('Failed: ' + e.message, true); b.disabled = false; }
  }));
  d.appendChild(teamRow);

  // Account link (player.account_id, app/db.py) -- read-only status
  // here; the action that clears it lives in the danger zone below,
  // next to the other operator-only, someone-else-loses-something
  // actions, not here next to the reversible team change above.
  d.appendChild(el('div', { className: 'adm-sub-title', text: 'Account' }));
  d.appendChild(el('p', {
    className: 'adm-hint',
    text: p.account_id ? ('Linked to account ' + p.account_id + '.') : 'Not linked to an account.',
  }));

  d.appendChild(el('div', { className: 'adm-sub-title', text: 'Radios' }));
  if (!p.radios || !p.radios.length) {
    d.appendChild(el('p', { className: 'adm-hint', text: 'None bound.' }));
  } else {
    p.radios.forEach((r) => d.appendChild(renderRadio(p, r)));
  }

  const add = el('div', { className: 'adm-form' });
  const ref = el('input', { placeholder: '!a1b2c3d4 or a1b2c3d4' });
  const proto = el('select');
  [['mt', 'Meshtastic'], ['mc', 'MeshCore']].forEach((o) => {
    const opt = el('option', { text: o[1], value: o[0] });
    proto.appendChild(opt);
  });
  add.appendChild(ref);
  add.appendChild(proto);
  add.appendChild(btn('Add radio', 'adm-btn-quiet', async (b) => {
    if (!ref.value.trim()) { setStatus('Enter a node reference', true); return; }
    b.disabled = true;
    try {
      await post('/api/admin/node/add',
        { player_id: p.player_id, protocol: proto.value, node_ref: ref.value.trim() });
      setStatus('Added radio for ' + p.display_name, false);
      await refreshAll();
    } catch (e) { setStatus('Failed: ' + e.message, true); b.disabled = false; }
  }));
  d.appendChild(add);

  d.appendChild(el('div', { className: 'adm-sub-title', text: 'Keys' }));
  if (!p.keys || !p.keys.length) {
    d.appendChild(el('p', { className: 'adm-hint', text: 'No keys.' }));
  } else {
    p.keys.forEach((k) => d.appendChild(renderKey(k)));
  }

  const out = el('div', { className: 'adm-result' });

  d.appendChild(el('div', { className: 'adm-sub-title', text: 'Diagnostics' }));
  const diag = el('div', { className: 'adm-result' });
  d.appendChild(btn('Why is nothing happening for them?', 'adm-btn-quiet', async (b) => {
    b.disabled = true;
    try {
      const r = await api('/api/admin/player/' + p.player_id + '/diagnostics');
      diag.replaceChildren();
      if (!r.days.length) {
        diag.appendChild(el('div', { text: 'No pings have ever arrived for this player.' }));
      } else {
        r.days.slice(0, 7).forEach((day) => {
          const parts = Object.keys(day)
            .filter((k) => k.startsWith('pings_') && day[k])
            .map((k) => k.replace('pings_', '') + ' ' + day[k]);
          diag.appendChild(el('div', {
            text: day.day + '  ' + (parts.length ? parts.join(', ') : 'nothing'),
          }));
        });
      }
    } catch (e) { diag.textContent = 'Failed: ' + e.message; }
    b.disabled = false;
  }));
  d.appendChild(diag);

  const zone = el('div', { className: 'adm-danger-zone' });
  zone.appendChild(btn(p.disabled ? 'Enable player' : 'Disable player', 'adm-btn-quiet', async (b) => {
    b.disabled = true;
    try {
      await post('/api/admin/player/' + (p.disabled ? 'enable' : 'disable'), { player_id: p.player_id });
      setStatus((p.disabled ? 'Enabled ' : 'Disabled ') + p.display_name, false);
      await refreshAll();
    } catch (e) { setStatus('Failed: ' + e.message, true); b.disabled = false; }
  }));
  zone.appendChild(btn('Issue extra key', '', async (b) => {
    b.disabled = true;
    try {
      const r = await post('/api/admin/player/issue_key', { player_id: p.player_id });
      revealKey(out, 'Extra key for ' + p.display_name, r.key);
      await refreshAll();
    } catch (e) { setStatus('Failed: ' + e.message, true); }
    b.disabled = false;
  }));
  zone.appendChild(btn('Revoke & reissue', 'adm-btn-danger', async (b) => {
    const typed = window.prompt('This breaks their current setup until they reconfigure.\n\nType ' + p.display_name + ' to confirm.');
    if (!typed) return;
    b.disabled = true;
    try {
      const r = await post('/api/admin/player/reissue',
        { player_id: p.player_id, display_name: typed });
      revealKey(out, 'New key for ' + p.display_name + ' (' + r.revoked_count + ' revoked)', r.key);
      await refreshAll();
    } catch (e) { setStatus('Failed: ' + e.message, true); }
    b.disabled = false;
  }));
  if (p.account_id) {
    // Player-facing account release does not exist anywhere in this
    // app on purpose (see app/account_api.py's module docstring) -- a
    // player can claim a key-only player onto their account via
    // link-key, but can never let go of one themselves. This is the
    // only door that clears player.account_id, which is why it only
    // appears at all when there is a link to release. Same typed-name
    // confirmation this page already uses for node removal above
    // (line ~291) and for reissue/delete below -- one consistent
    // interaction for "this takes something away from someone", not a
    // second style borrowed from the account page's own confirm step.
    zone.appendChild(btn('Release account link', 'adm-btn-danger', async (b) => {
      const typed = window.prompt(
        p.display_name + ' keeps every radio, key, check-in, and point they have earned.\n' +
        'This only disconnects account ' + p.account_id + ' from them -- afterward, ' +
        'whoever holds their API key can link a fresh account onto this player.\n\n' +
        'Type ' + p.display_name + ' to confirm.'
      );
      if (!typed) return;
      b.disabled = true;
      try {
        await post('/api/admin/player/unlink-account',
          { player_id: p.player_id, display_name: typed });
        setStatus('Released ' + p.display_name + ' from its account', false);
        await refreshAll();
      } catch (e) { setStatus('Failed: ' + e.message, true); b.disabled = false; }
    }));
  }
  zone.appendChild(btn('Delete player', 'adm-btn-danger', async (b) => {
    const typed = window.prompt('Deleting removes the player, not what they earned — their squares, capture history, month awards, and check-in awards all stay.\n\nType ' + p.display_name + ' to confirm.');
    if (!typed) return;
    b.disabled = true;
    try {
      await post('/api/admin/player/delete', { player_id: p.player_id, display_name: typed });
      setStatus('Deleted ' + p.display_name, false);
      expanded.delete(p.player_id);
      await refreshAll();
    } catch (e) { setStatus('Failed: ' + e.message, true); b.disabled = false; }
  }));
  d.appendChild(zone);
  d.appendChild(out);
  return d;
}

function renderPlayers() {
  const host = document.getElementById('players');
  const q = (document.getElementById('player-search').value || '').trim().toLowerCase();
  host.replaceChildren();

  const shown = allPlayers.filter((p) =>
    !q || p.display_name.toLowerCase().includes(q) || (p.team || '').toLowerCase().includes(q));
  document.getElementById('players-count').textContent =
    shown.length === allPlayers.length
      ? allPlayers.length + ' players'
      : shown.length + ' of ' + allPlayers.length;

  shown.forEach((p) => {
    const wrap = el('div', { className: 'adm-player' });
    wrap.id = 'player-' + p.player_id;

    const row = el('div', { className: 'adm-player-row' });
    const open = expanded.has(p.player_id);
    row.appendChild(el('span', { className: 'adm-caret', text: open ? '▾' : '▸' }));
    row.appendChild(el('span', { className: 'adm-player-name', text: p.display_name }));
    row.appendChild(el('span', { className: 'adm-badge', text: p.team }));
    if (p.disabled) row.appendChild(el('span', { className: 'adm-badge adm-badge-bad', text: 'disabled' }));
    const radios = (p.radios || []).length;
    const meta = el('span', { className: 'adm-player-meta' });
    appendFacts(meta, [
      // No radio at all is why "never connected a radio" is one of the
      // Overview attention groups -- worth the same weight here, not
      // just buried as "0 radios" in a run of grey text.
      radios === 0
        ? el('span', { className: 'adm-badge adm-badge-attn', text: 'no radios' })
        : document.createTextNode(radios + (radios === 1 ? ' radio' : ' radios')),
      document.createTextNode('joined ' + fmtTs(p.created_at)),
    ]);
    row.appendChild(meta);
    row.addEventListener('click', () => {
      if (expanded.has(p.player_id)) expanded.delete(p.player_id);
      else expanded.add(p.player_id);
      renderPlayers();
    });
    wrap.appendChild(row);
    if (open) wrap.appendChild(renderPlayerDetail(p));
    host.appendChild(wrap);
  });
}

async function loadPlayers() {
  try {
    allPlayers = await api('/api/admin/players');
    renderPlayers();
    setStatus('Loaded ' + allPlayers.length + ' players', false);
  } catch (e) {
    setStatus('Load failed: ' + e.message, true);
  }
}

// ---- accounts -----------------------------------------------------------
//
// The account-shaped counterpart to Players above. GET
// /api/admin/players lists every PLAYER and only ever reaches an
// account by way of the player it happens to be linked to -- an
// account with no linked player at all (released by "Release account
// link" above, or never claimed after signing in) is invisible there.
// GET /api/admin/accounts (app/admin_api.py) fixes that by listing
// `account` directly; this section is its only consumer. Flat rows,
// same .adm-row/.adm-mono/.adm-badge shape the Roles section below
// already uses for one-row-per-account, rather than the expandable
// .adm-player treatment Players uses -- there is exactly one action
// here (delete), not a growing list of per-row controls, so a second
// nested-detail affordance would add a click for no reason.

function accountConfirmPhrase(a) {
  // Mirrors app/admin_api.py's _admin_account_no_player_confirm()
  // exactly. See that function's own docstring for why an orphan's
  // confirmation is "DELETE ACCOUNT <id>" rather than a fixed literal:
  // this page can show several orphans at once, and a fixed phrase
  // would let one get pasted onto the wrong row without the text
  // itself ever forcing a look at which row is actually being
  // confirmed.
  return a.player ? a.player.display_name : ('DELETE ACCOUNT ' + a.account_id);
}

function recoveryConfirmPhrase(a, actionPhrase) {
  // Same purpose accountConfirmPhrase() above serves for delete,
  // generalized to whichever recovery action is asking -- mirrors
  // app/admin_api.py's own _admin_account_recovery_confirm() exactly.
  // Each recovery action gets its OWN phrase (not delete's fixed
  // "DELETE ACCOUNT") so an orphan-account confirmation never reads
  // like the wrong action.
  return a.player ? a.player.display_name : (actionPhrase + ' ' + a.account_id);
}

// The three recovery actions below all share one rule: an operator can
// only take a credential AWAY, never hand back a working one. There is
// no "set a password for this account" action anywhere on this page,
// and never will be -- see app/admin_api.py's own "account recovery:
// clear a credential, never set one" section comment for the full
// reasoning. Clearing restores the account to whoever can still prove
// they hold ITS OTHER credentials; it never lets the operator sign in
// as them.
function appendAccountRecoveryActions(actions, a) {
  if (a.totp_active) {
    actions.appendChild(btn('Disable two-factor', 'adm-btn-danger', async (b) => {
      const who = a.player ? a.player.display_name : ('account ' + a.account_id);
      const expected = recoveryConfirmPhrase(a, 'DISABLE TWO-FACTOR');
      const typed = window.prompt(
        'This turns off two-factor authentication on ' + who + "'s account and clears " +
        'every recovery code — the fix for a lost phone with no recovery codes left. ' +
        who + ' will be able to sign back in with just their other credentials, with no ' +
        'second factor until they set one up again.\n\n' +
        'Type ' + expected + ' to confirm.'
      );
      if (!typed) return;
      b.disabled = true;
      try {
        await post('/api/admin/account/disable-totp', { account_id: a.account_id, display_name: typed });
        setStatus('Disabled two-factor authentication on account ' + a.account_id, false);
        await refreshAll();
      } catch (e) { setStatus('Failed: ' + e.message, true); b.disabled = false; }
    }));
  }

  // Offered only when a door would REMAIN. The server refuses a clear or a
  // removal that would leave an account with none -- this surface can only
  // ever clear a credential, never set one, so stripping the last door
  // produces an account nobody can reach and nobody can hand back. Deciding
  // that here as well, from identity_providers + has_password, is what stops
  // the row offering a button that is guaranteed to fail: applicability is
  // "would this help", not merely "does this credential exist".
  const doorCount = (a.identity_providers || []).length + (a.has_password ? 1 : 0);

  if (a.has_password && doorCount > 1) {
    actions.appendChild(btn('Clear password', 'adm-btn-danger', async (b) => {
      const who = a.player ? a.player.display_name : ('account ' + a.account_id);
      const expected = recoveryConfirmPhrase(a, 'CLEAR PASSWORD');
      const typed = window.prompt(
        'This removes the password on ' + who + "'s account — nothing is set in its " +
        'place. ' + who + ' can only get back in through another sign-in method they ' +
        'still hold, and can set a fresh password once they do.\n\n' +
        'Type ' + expected + ' to confirm.'
      );
      if (!typed) return;
      b.disabled = true;
      try {
        await post('/api/admin/account/password/clear', { account_id: a.account_id, display_name: typed });
        setStatus('Cleared the password on account ' + a.account_id, false);
        await refreshAll();
      } catch (e) { setStatus('Failed: ' + e.message, true); b.disabled = false; }
    }));
  }

  (doorCount > 1 ? (a.identity_providers || []) : []).forEach((provider) => {
    actions.appendChild(btn('Remove ' + provider, 'adm-btn-danger', async (b) => {
      const who = a.player ? a.player.display_name : ('account ' + a.account_id);
      const expected = recoveryConfirmPhrase(a, 'REMOVE SIGN-IN');
      const typed = window.prompt(
        'This disconnects ' + provider + ' from ' + who + "'s account — for a provider " +
        'they have lost access to, or one linked in error. Only offered when another ' +
        'way to sign in would remain.\n\n' +
        'Type ' + expected + ' to confirm.'
      );
      if (!typed) return;
      b.disabled = true;
      try {
        await post('/api/admin/account/identity/remove',
          { account_id: a.account_id, provider: provider, display_name: typed });
        setStatus('Removed ' + provider + ' from account ' + a.account_id, false);
        await refreshAll();
      } catch (e) { setStatus('Failed: ' + e.message, true); b.disabled = false; }
    }));
  });
}

// Grant and revoke, folded in from the old standalone Roles section --
// every role holder is an account, so Roles was always a strict subset
// of this list; there was never a reason for a role change to live
// anywhere other than the account it changes. Operator-only in both
// directions: POST /api/admin/roles/grant and .../revoke both guard
// with _role_guard(need="operator") server-side, and this function
// only ever runs when myRole === 'operator' -- an admin's own session
// would just 401 on either route, so an admin never even sees a
// control that would fail.
function appendAccountRoleActions(actions, a) {
  if (myRole !== 'operator') return;
  const who = a.player ? a.player.display_name : ('account ' + a.account_id);

  if (!a.role) {
    actions.appendChild(btn('Grant admin', 'adm-btn', async (b) => {
      b.disabled = true;
      try {
        await post('/api/admin/roles/grant', { account_id: a.account_id });
        setStatus('Granted admin to ' + who, false);
        await refreshAll();
      } catch (e) { setStatus('Failed: ' + e.message, true); b.disabled = false; }
    }));
    return;
  }

  // Revoke only, never Grant, on a row that already holds a role --
  // POST /api/admin/roles/grant refuses (409) onto an account already
  // holding operator, because granting 'admin' onto it would silently
  // demote them, and granting onto an existing admin is a no-op this
  // row has no need to offer. Revoke has no such restriction in either
  // direction (see that route's own docstring).
  actions.appendChild(btn('Revoke', 'adm-btn-danger', async (b) => {
    // A plain confirm, not the typed confirmation the recovery actions
    // below use -- revoke only strips an elevated role, leaving the
    // account, its data, and its ordinary sign-in untouched (unlike
    // those actions, which can leave someone with no way in at all, or
    // delete, which tombstones a player). The old Roles section used
    // the same plain confirm for the same reason.
    if (!window.confirm('Revoke the ' + a.role + ' role from ' + who + '?')) return;
    b.disabled = true;
    try {
      await post('/api/admin/roles/revoke', { account_id: a.account_id });
      setStatus('Revoked ' + a.role + ' from ' + who, false);
      await refreshAll();
    } catch (e) { setStatus('Failed: ' + e.message, true); b.disabled = false; }
  }));
}

function renderAccounts() {
  const host = document.getElementById('accounts');
  const q = (document.getElementById('account-search').value || '').trim().toLowerCase();
  const orphansOnly = document.getElementById('account-orphans-only').checked;
  const rolesOnly = document.getElementById('account-roles-only').checked;
  host.replaceChildren();

  const shown = allAccounts.filter((a) => {
    if (orphansOnly && a.player) return false;
    if (rolesOnly && !a.role) return false;
    if (!q) return true;
    if (String(a.account_id).includes(q)) return true;
    return !!(a.player && a.player.display_name.toLowerCase().includes(q));
  });

  const orphanTotal = allAccounts.filter((a) => !a.player).length;
  document.getElementById('accounts-count').textContent =
    (shown.length === allAccounts.length
      ? allAccounts.length + ' accounts'
      : shown.length + ' of ' + allAccounts.length) +
    (orphanTotal ? ', ' + orphanTotal + ' with no linked player' : '');

  if (!shown.length) {
    host.appendChild(el('div', { className: 'adm-row', text: 'No accounts match.' }));
    return;
  }

  shown.forEach((a) => {
    const row = el('div', { className: 'adm-row' });
    const info = el('div', { className: 'adm-row-info' });
    // No linked player is the one state Players can never surface at
    // all (see this section's own docstring) -- a badge here, not a
    // plain unstyled name-shaped blank, so it reads as a state rather
    // than an absence of one.
    info.appendChild(a.player
      ? el('span', { className: 'adm-player-name', text: a.player.display_name })
      : el('span', { className: 'adm-badge adm-badge-attn', text: 'no linked player' }));
    info.appendChild(el('span', { className: 'adm-mono', text: 'id ' + a.account_id }));
    if (a.role) {
      info.appendChild(el('span', {
        className: 'adm-badge' + (a.role === 'operator' ? ' adm-badge-ok' : ''),
        text: a.role,
      }));
    }
    const meta = el('span', { className: 'adm-player-meta' });
    appendFacts(meta, [
      // Zero doors in is an account nobody can sign into -- a real
      // problem, not a routine fact, so it gets -bad rather than
      // sitting in the same grey run every other count here does.
      a.sign_in_methods === 0
        ? el('span', { className: 'adm-badge adm-badge-bad', text: '0 sign-in methods' })
        : document.createTextNode(a.sign_in_methods +
            (a.sign_in_methods === 1 ? ' sign-in method' : ' sign-in methods')),
      document.createTextNode('created ' + fmtTs(a.created_at)),
      // Never signed in is a notice, not an error -- a fresh grant or a
      // just-claimed account looks exactly like this on its first
      // visit, so -attn rather than -bad.
      a.last_login_at
        ? document.createTextNode('last signed in ' + ago(a.last_login_at))
        : el('span', { className: 'adm-badge adm-badge-attn', text: 'never signed in' }),
    ]);
    info.appendChild(meta);
    row.appendChild(info);

    // All of a row's buttons -- role, recovery, delete -- land in one
    // actions group so they wrap onto a second line as a unit, never
    // individually (see .adm-row-actions in admin.css).
    const actions = el('div', { className: 'adm-row-actions' });
    appendAccountRoleActions(actions, a);
    appendAccountRecoveryActions(actions, a);
    actions.appendChild(btn('Delete account', 'adm-btn-danger', async (b) => {
      const who = a.player ? a.player.display_name : ('account ' + a.account_id);
      const expected = accountConfirmPhrase(a);
      const typed = window.prompt(
        (a.player
          ? ('Deleting removes the account, not what ' + who + ' earned — their squares, ' +
             'capture history, month awards, and check-in awards all stay. ' + who +
             ' is tombstoned the same way "Delete player" leaves them.\n\n')
          : ('This account has no linked player — there is nothing to tombstone, only the ' +
             'account itself and its sign-in identities and sessions go away.\n\n')) +
        'Type ' + expected + ' to confirm.'
      );
      if (!typed) return;
      b.disabled = true;
      try {
        await post('/api/admin/account/delete', { account_id: a.account_id, display_name: typed });
        setStatus('Deleted account ' + a.account_id, false);
        await refreshAll();
      } catch (e) { setStatus('Failed: ' + e.message, true); b.disabled = false; }
    }));
    row.appendChild(actions);
    host.appendChild(row);
  });
}

async function loadAccounts() {
  try {
    allAccounts = await api('/api/admin/accounts');
    renderAccounts();
    setStatus('Loaded ' + allAccounts.length + ' accounts', false);
  } catch (e) {
    setStatus('Load failed: ' + e.message, true);
  }
}

// ---- check-ins --------------------------------------------------------

let allCheckinAwards = []; // GET /api/admin/checkin/awards -- most recent net dates only

// net_label already carries the protocol-label fallback for a legacy
// net_id-NULL row (see app/admin_ops.py's admin_checkin_awards) -- this
// only turns a flat award list into the "grouped by net date, newest
// first" table the checkins section asks for, same "one <table>, build
// it with el()" shape as everything else in this file builds its rows.
function renderCheckinAwards() {
  const host = document.getElementById('ci-awards');
  host.replaceChildren();
  if (!allCheckinAwards.length) {
    host.appendChild(el('p', { className: 'adm-hint', text: 'No check-ins recorded yet.' }));
    return;
  }
  const table = el('table', { className: 'adm-table' });
  const thead = el('thead');
  const headRow = el('tr');
  ['Date', 'Net', 'Player', 'Points', 'Streak', 'Source'].forEach((h) => {
    headRow.appendChild(el('th', { text: h }));
  });
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = el('tbody');
  allCheckinAwards.forEach((a) => {
    const tr = el('tr');
    tr.appendChild(el('td', { text: a.net_date }));
    tr.appendChild(el('td', { text: a.net_label }));
    tr.appendChild(el('td', { text: a.player_name }));
    tr.appendChild(el('td', { text: String(a.points) }));
    tr.appendChild(el('td', { text: a.streak === null || a.streak === undefined ? '—' : String(a.streak) }));
    tr.appendChild(el('td', { text: a.source === 'admin' ? 'admin' : 'poller' }));
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  const wrap = el('div', { className: 'adm-table-wrap' });
  wrap.appendChild(table);
  host.appendChild(wrap);
}

async function loadCheckinAwards() {
  try {
    const d = await api('/api/admin/checkin/awards');
    allCheckinAwards = d.awards || [];
    renderCheckinAwards();
  } catch (e) {
    setStatus('Check-in history load failed: ' + e.message, true);
  }
}

async function awardCheckin(b) {
  const name = document.getElementById('ci-player').value.trim().toLowerCase();
  const date = document.getElementById('ci-date').value.trim();
  const proto = document.getElementById('ci-proto').value;
  const out = document.getElementById('ci-result');
  out.replaceChildren();
  const p = allPlayers.find((x) => x.display_name.toLowerCase() === name);
  if (!p) { out.textContent = 'No player by that exact name. Load players first.'; return; }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) { out.textContent = 'Date must look like 2026-08-19.'; return; }
  b.disabled = true;
  try {
    const r = await post('/api/admin/checkin/award',
      { player_id: p.player_id, net_date: date, protocol: proto });
    out.textContent = 'Credited ' + p.display_name + ' ' + r.points +
      ' points for ' + r.net_date + ' (streak ' + r.streak + ').';
    await loadCheckinAwards();
  } catch (e) {
    out.textContent = 'Failed: ' + e.message;
  }
  b.disabled = false;
}

async function freezeMonth(b) {
  const month = document.getElementById('mo-month').value.trim();
  const proto = document.getElementById('mo-proto').value;
  const out = document.getElementById('mo-result');
  out.replaceChildren();
  if (!/^\d{4}-\d{2}$/.test(month)) { out.textContent = 'Month must look like 2026-08.'; return; }
  b.disabled = true;
  try {
    await post('/api/admin/month/freeze', { month: month, protocol: proto });
    out.textContent = month + ' recomputed and frozen.';
  } catch (e) {
    out.textContent = 'Failed: ' + e.message;
  }
  b.disabled = false;
}

// ---- checkin config (points, streak bonus, poller timing knobs) --------
//
// The config singleton -- applies to every net at once. GET
// /api/admin/checkin/nets hands this back alongside the nets list
// itself (see loadNets() further down, in the connections section).

function renderConfigForm(c) {
  document.getElementById('nc-enabled').checked = !!c.enabled;
  document.getElementById('nc-points').value = c.points;
  document.getElementById('nc-streak-bonus').value = c.streak_bonus;
  document.getElementById('nc-streak-bonus-max').value = c.streak_bonus_max;
  document.getElementById('nc-poll-interval').value = c.poll_interval_seconds;
  document.getElementById('nc-directory-limit').value = c.directory_limit;
  document.getElementById('nc-directory-refresh').value = c.directory_refresh_seconds;
}

async function saveConfig(b) {
  const out = document.getElementById('nc-result');
  out.replaceChildren();
  const payload = {
    enabled: document.getElementById('nc-enabled').checked,
    points: parseFloat(document.getElementById('nc-points').value),
    streak_bonus: parseFloat(document.getElementById('nc-streak-bonus').value),
    streak_bonus_max: parseFloat(document.getElementById('nc-streak-bonus-max').value),
    poll_interval_seconds: parseInt(document.getElementById('nc-poll-interval').value, 10),
    directory_limit: parseInt(document.getElementById('nc-directory-limit').value, 10),
    directory_refresh_seconds: parseInt(document.getElementById('nc-directory-refresh').value, 10),
  };
  b.disabled = true;
  try {
    const c = await post('/api/admin/checkin/config', payload);
    renderConfigForm(c);
    out.textContent = 'Settings saved.';
  } catch (e) {
    out.textContent = 'Failed: ' + e.message;
  }
  b.disabled = false;
}

// ---- connections (nets + observation sources, merged) -------------------
//
// Nets (app/db.py's checkin_net) and observation sources (app/db.py's
// observation_source) share a connector-kind vocabulary and the same
// secret handling, and now the same optional community_id -- an
// operator managing "the FREQ51 connection" thinks of one thing, not
// two separate tables, so this section renders them as one merged
// list (renderConnections below), with a shared connector-kind
// vocabulary (NET_KIND_LABELS, netKindHasChannel/Hashtag/IsMeshCore/
// IsMqtt, NET_CONNECTOR_URL_EXAMPLES) and one shared show/hide
// function (applyConnectorVisibility) for the kind-dependent fields,
// used by both the bottom add-a-connection form (nf-* ids) and each
// row's own inline edit panel (dynamically built elements, same
// caret-expand idiom Players uses -- see renderPlayers/
// renderPlayerDetail). The backend still exposes two tables and four
// create/update endpoints (checkin/nets/* and observation/sources/*)
// plus two conversion endpoints (nets/convert-to-source,
// sources/convert-to-net) for flipping a row from one to the other --
// this file never merges the tables, only the screen.

// The five connector kinds an operator can pick (see app/checkin.py's
// KIND_CORESCOPE/KIND_BEACON/KIND_MESHVIEW/KIND_MQTT/
// KIND_MQTT_MESHTASTIC). `protocol` ('mc'/'mt') is derived from this
// on the backend and is never sent by this form -- see
// _validate_net_fields in app/admin_ops.py.
const NET_KIND_LABELS = {
  corescope: 'MC: CoreScope',
  beacon: 'MC: Beacon',
  meshview: 'MT: Meshview',
  mqtt: 'MT: MQTT',
  mqtt_meshtastic: 'MT: Official Meshtastic broker',
};
// corescope and beacon are channel-scoped connectors (a net picks one
// channel on the connector); meshview, mqtt and mqtt_meshtastic are
// all hashtag-scoped nets (found by their hashtag on any channel) --
// see app/checkin.py's module docstring. mqtt and mqtt_meshtastic
// ALSO show the channel field even though they're hashtag-scoped:
// channel narrows which Meshtastic channel the broker subscription
// itself watches, a separate concern from the hashtag that identifies
// this net's own messages once they arrive. An observation source
// never has a hashtag at all regardless of kind (the
// observation_source table has no such column) -- see
// applyScoresNetHashtagVisibility below for where that second
// condition is layered on top of this one.
function netKindHasChannel(kind) { return kind !== 'meshview'; }
function netKindHasHashtag(kind) { return kind !== 'corescope' && kind !== 'beacon'; }
// corescope/beacon poll an upstream channel-list API (see
// loadConnectorChannels below); an MQTT broker has no such API -- GET
// /api/admin/checkin/channels returns applicable: false for both mqtt
// kinds -- so "Load channels" only ever makes sense for the MeshCore
// two.
function netKindIsMeshCore(kind) { return kind === 'corescope' || kind === 'beacon'; }
function netKindIsMqtt(kind) { return kind === 'mqtt' || kind === 'mqtt_meshtastic'; }

// mqtt's connector is a broker address, not an http(s) URL like the
// other kinds -- an https example there reads as a typo instruction
// rather than guidance. mqtt_meshtastic has no entry here: its
// connector row is hidden outright (see applyConnectorVisibility
// below) since there is only one official broker and nothing for an
// operator to type -- app/admin_ops.py's _validate_connector_url
// forces it server-side regardless of what this form would send.
const NET_CONNECTOR_URL_EXAMPLES = {
  corescope: 'https://live.mwmesh.com',
  beacon: 'https://map.meshcore.coloradomesh.org',
  meshview: 'https://meshview.freq51.net',
  mqtt: 'mqtt://broker.example.org:1883',
};

// Same curated list nf-timezone's <select> in admin.html carries --
// duplicated here so a row's own dynamically-built timezone <select>
// (renderConnectionDetail below) offers the identical choices without
// depending on the static form markup.
const NET_TIMEZONE_OPTIONS = [
  ['America/Los_Angeles', 'Pacific - America/Los_Angeles'],
  ['America/Denver', 'Mountain - America/Denver'],
  ['America/Phoenix', 'Arizona, no DST - America/Phoenix'],
  ['America/Boise', 'Mountain (Idaho) - America/Boise'],
  ['America/Chicago', 'Central - America/Chicago'],
  ['America/New_York', 'Eastern - America/New_York'],
  ['America/Anchorage', 'Alaska - America/Anchorage'],
  ['Pacific/Honolulu', 'Hawaii - Pacific/Honolulu'],
  ['UTC', 'UTC'],
];

function pad2(n) { return String(n).padStart(2, '0'); }

// A curated <select> can't just be assigned a value outside its
// options (e.g. a stored timezone posted straight to the API before
// this picker existed) -- the browser silently leaves an unmatched
// <select>.value at whatever was already selected. So if the value
// isn't one of the curated options, inject it as an extra option and
// select that instead of falling back -- otherwise merely opening a
// row for edit, without touching the field, would rewrite its
// timezone to the default on next save. Takes the <select> itself
// (not an id) so both the one static form and every dynamically-built
// row editor can share this.
function setTimezoneSelectValue(select, tz) {
  Array.from(select.querySelectorAll('option[data-custom-tz]')).forEach((o) => o.remove());
  select.value = tz;
  if (select.value !== tz) {
    const opt = document.createElement('option');
    opt.dataset.customTz = '1';
    opt.value = tz;
    opt.textContent = tz + ' (current value, not in list)';
    select.appendChild(opt);
    select.value = tz;
  }
}

function netHealthText(n) {
  if (n.last_poll_error) return 'poll error: ' + n.last_poll_error;
  return n.last_poll_at ? ('last polled ' + ago(n.last_poll_at)) : 'never polled yet';
}

// Small builders shared by the connection-detail panel and the
// community-detail panel below -- both build several label+input
// pairs the same shape the static admin.html markup already uses
// (.adm-field-label / .adm-check-label), just assembled with el()
// instead of written out as HTML.
function fieldLabel(text, input, wide) {
  const label = el('label', { className: 'adm-field-label' + (wide ? ' adm-field-label-wide' : ''), text: text });
  label.appendChild(input);
  return label;
}
function checkLabel(text, input) {
  const label = el('label', { className: 'adm-check-label' });
  label.appendChild(input);
  label.appendChild(document.createTextNode(' ' + text));
  return label;
}

// One shared show/hide pass for the connector-kind-dependent fields --
// used by both the bottom add-a-connection form (via
// updateConnectionFormKind, reading nf-* ids) and every row's own
// inline edit panel (via renderConnectionDetail, passing its own
// dynamically-built elements). `refs` carries the same seven pieces
// either caller has: channelRow, loadChannelsBtn, channelSelect,
// connectorRow, officialHint, mqttRow1, brokerUsername, mqttRow2,
// mqttRow3, mqttHint, connectorInput, hashtagRow. This never decides
// whether hashtag makes sense for an OBSERVATION SOURCE (which has no
// hashtag column regardless of kind) -- that's layered on top by each
// caller after this runs, since it depends on the "Scores a net"
// checkbox state, not the connector kind.
function applyConnectorVisibility(kind, refs) {
  refs.channelRow.hidden = !netKindHasChannel(kind);
  refs.hashtagRow.hidden = !netKindHasHashtag(kind);
  const isMeshCore = netKindIsMeshCore(kind);
  refs.loadChannelsBtn.hidden = !isMeshCore;
  if (!isMeshCore) {
    refs.channelSelect.hidden = true;
    refs.channelSelect.replaceChildren();
  }
  const isMqtt = netKindIsMqtt(kind);
  const isOfficialMqtt = kind === 'mqtt_meshtastic';
  refs.connectorRow.hidden = isOfficialMqtt;
  refs.officialHint.hidden = !isOfficialMqtt;
  refs.mqttRow1.hidden = !isMqtt;
  refs.brokerUsername.hidden = isOfficialMqtt;
  refs.mqttRow2.hidden = !isMqtt || isOfficialMqtt;
  refs.mqttRow3.hidden = !isMqtt;
  refs.mqttHint.hidden = !isOfficialMqtt;
  const example = NET_CONNECTOR_URL_EXAMPLES[kind] || NET_CONNECTOR_URL_EXAMPLES.corescope;
  refs.connectorInput.placeholder = 'connector URL, e.g. ' + example;
}

// GET /api/admin/checkin/channels proxies whichever connector a kind
// actually has a channel-list API for (see that route's own
// docstring) -- shared by the add form and every row's own "Load
// channels" button so the fetch/populate logic exists exactly once.
async function loadConnectorChannels(kind, connector, channelSelect, channelInput, out) {
  out.textContent = '';
  if (!connector) { out.textContent = 'Enter a connector URL first.'; return; }
  try {
    const r = await api('/api/admin/checkin/channels?connector=' + encodeURIComponent(connector) +
      '&kind=' + encodeURIComponent(kind));
    channelSelect.replaceChildren();
    if (!r.applicable) {
      channelSelect.hidden = true;
      out.textContent = 'This connector kind has no channel list -- type the channel name by hand.';
      return;
    }
    (r.channels || []).forEach((c) => {
      const name = typeof c === 'string' ? c : (c.name || c.channel || c.label || '');
      if (!name) return;
      channelSelect.appendChild(el('option', { value: name, text: name }));
    });
    if (channelSelect.children.length) {
      channelSelect.hidden = false;
      channelSelect.value = channelSelect.children[0].value;
      channelInput.value = channelSelect.value;
      out.textContent = 'Loaded ' + channelSelect.children.length + ' channels.';
    } else {
      channelSelect.hidden = true;
      out.textContent = 'Connector returned no channels -- type the channel name by hand.';
    }
  } catch (e) {
    // Never blocks the form -- a slow or unreachable connector still
    // leaves the plain text field usable.
    out.textContent = 'Could not load channels: ' + e.message + ' -- type the channel name by hand.';
  }
}

// Fills a community <select> (the add form's nf-community, or a row's
// own dynamically-built one) from allCommunities -- "No community" is
// always the first option, value ''.
function populateCommunitySelect(select, selectedId) {
  const prior = select.value;
  select.replaceChildren();
  select.appendChild(el('option', { value: '', text: 'No community' }));
  allCommunities.forEach((c) => {
    select.appendChild(el('option', { value: String(c.id), text: c.name }));
  });
  const want = selectedId != null ? String(selectedId) : prior;
  select.value = want;
  if (select.value !== want) select.value = '';
}

function communityName(communityId) {
  if (communityId == null) return null;
  const c = allCommunities.find((x) => x.id === communityId);
  return c ? c.name : null;
}

// ---- communities (app/db.py's community) -------------------------------

function renderCommunities() {
  const host = document.getElementById('communities');
  const count = document.getElementById('communities-count');
  host.replaceChildren();
  count.textContent = allCommunities.length
    ? (allCommunities.length + (allCommunities.length === 1 ? ' community' : ' communities')) : '';
  if (!allCommunities.length) {
    host.appendChild(el('p', { className: 'adm-hint', text: 'No communities configured yet -- add one below.' }));
    return;
  }
  allCommunities.forEach((c) => host.appendChild(renderCommunityRow(c)));
}

function renderCommunityRow(c) {
  const wrap = el('div', { className: 'adm-net' });
  const row = el('div', { className: 'adm-net-row' });
  const open = expandedCommunities.has(c.id);
  row.appendChild(el('span', { className: 'adm-caret', text: open ? '▾' : '▸' }));
  const info = el('div', { className: 'adm-net-info' });
  info.appendChild(el('strong', { text: c.name }));
  if (c.region) info.appendChild(el('span', { className: 'adm-badge', text: c.region }));
  info.appendChild(el('span', {
    className: 'adm-badge ' + (c.shown_on_about ? 'adm-badge-ok' : 'adm-badge-bad'),
    text: c.shown_on_about ? 'shown on about' : 'hidden from about',
  }));
  row.appendChild(info);
  const actions = el('div', { className: 'adm-net-actions' });
  actions.appendChild(btn('Edit', 'adm-btn-quiet', () => startEditCommunity(c)));
  actions.appendChild(btn('Delete', 'adm-btn-danger', async (b) => {
    const typed = window.prompt(
      'Deleting unlinks any net or observation source pointing at this community -- it does not delete them.\n\nType ' + c.name + ' to confirm.');
    if (!typed) return;
    b.disabled = true;
    try {
      await post('/api/admin/communities/delete', { id: c.id, name: typed });
      setStatus('Deleted community ' + c.name, false);
      expandedCommunities.delete(c.id);
      if (editingCommunityId === c.id) resetCommunityForm();
      await Promise.all([loadCommunities(), loadNets(), loadSources()]);
    } catch (e) { setStatus('Failed: ' + e.message, true); b.disabled = false; }
  }));
  row.appendChild(actions);
  // Only the caret/name/badges toggle the expand -- clicking Edit or
  // Delete above must not also flip this row open/closed underneath
  // the action it just ran.
  row.addEventListener('click', (e) => {
    if (e.target.closest('.adm-net-actions')) return;
    if (expandedCommunities.has(c.id)) expandedCommunities.delete(c.id);
    else expandedCommunities.add(c.id);
    renderCommunities();
  });
  wrap.appendChild(row);
  if (open) wrap.appendChild(renderCommunityDetail(c));
  return wrap;
}

// Read-only detail -- actual editing happens in the one shared cf-*
// form below the list (see startEditCommunity), the same "click Edit,
// the shared form scrolls into view and fills in" pattern this file
// used for nets/sources before this section existed, kept here since
// a community has no kind-dependent fields to justify a fully inline
// per-row form the way a connection now needs.
function renderCommunityDetail(c) {
  const d = el('div', { className: 'adm-player-detail' });
  if (c.blurb) d.appendChild(el('p', { className: 'adm-hint', text: c.blurb }));
  if (c.url) d.appendChild(el('p', { className: 'adm-hint', text: 'URL: ' + c.url }));
  if (c.contact_url) d.appendChild(el('p', { className: 'adm-hint', text: 'Contact: ' + c.contact_url }));
  d.appendChild(el('p', { className: 'adm-hint', text: 'Display order: ' + c.display_order }));
  return d;
}

let editingCommunityId = null; // null while the cf-* form is adding, a community id while editing

function resetCommunityForm() {
  editingCommunityId = null;
  document.getElementById('cf-name').value = '';
  document.getElementById('cf-region').value = '';
  document.getElementById('cf-blurb').value = '';
  document.getElementById('cf-url').value = '';
  document.getElementById('cf-contact-url').value = '';
  document.getElementById('cf-display-order').value = '0';
  document.getElementById('cf-shown-on-about').checked = true;
  document.getElementById('community-form-title').textContent = 'Add a community';
  document.getElementById('cf-save').textContent = 'Add community';
  // Deliberately does not touch cf-result -- saveCommunity() calls
  // this right after a successful save specifically to clear the form
  // back to a blank "add" state, same reasoning the old resetNetForm's
  // own comment gave for nf-result.
}

function startEditCommunity(c) {
  editingCommunityId = c.id;
  document.getElementById('cf-name').value = c.name;
  document.getElementById('cf-region').value = c.region || '';
  document.getElementById('cf-blurb').value = c.blurb || '';
  document.getElementById('cf-url').value = c.url || '';
  document.getElementById('cf-contact-url').value = c.contact_url || '';
  document.getElementById('cf-display-order').value = c.display_order != null ? c.display_order : 0;
  document.getElementById('cf-shown-on-about').checked = !!c.shown_on_about;
  document.getElementById('community-form-title').textContent = 'Edit community';
  document.getElementById('cf-save').textContent = 'Save community';
  document.getElementById('cf-result').replaceChildren();
  document.getElementById('community-form-title').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function saveCommunity(b) {
  const out = document.getElementById('cf-result');
  out.replaceChildren();
  const name = document.getElementById('cf-name').value.trim();
  if (!name) { out.textContent = 'Name is required.'; return; }
  const payload = {
    name: name,
    region: document.getElementById('cf-region').value.trim(),
    blurb: document.getElementById('cf-blurb').value.trim(),
    url: document.getElementById('cf-url').value.trim(),
    contact_url: document.getElementById('cf-contact-url').value.trim(),
    display_order: parseInt(document.getElementById('cf-display-order').value, 10) || 0,
    shown_on_about: document.getElementById('cf-shown-on-about').checked,
  };
  b.disabled = true;
  try {
    if (editingCommunityId === null) {
      await post('/api/admin/communities/create', payload);
      out.textContent = 'Community added.';
    } else {
      await post('/api/admin/communities/update', Object.assign({ id: editingCommunityId }, payload));
      out.textContent = 'Community updated.';
    }
    resetCommunityForm();
    await loadCommunities();
  } catch (e) {
    out.textContent = 'Failed: ' + e.message;
  }
  b.disabled = false;
}

async function loadCommunities() {
  try {
    const d = await api('/api/admin/communities');
    allCommunities = d.communities || [];
    renderCommunities();
    populateCommunitySelect(document.getElementById('nf-community'), null);
  } catch (e) {
    setStatus('Communities load failed: ' + e.message, true);
  }
  // Community names feed into every connection row's info line, so a
  // communities reload always re-renders the connections list too,
  // the same way loadNets()/loadSources() below do for each other.
  renderConnections();
}

// ---- connections (app/db.py's checkin_net + observation_source) --------

let allNets = [];
let allSources = [];

function connectionKey(row) { return (row.__isNet ? 'net-' : 'source-') + row.id; }

function renderConnections() {
  const host = document.getElementById('connections');
  const count = document.getElementById('connections-count');
  host.replaceChildren();
  const combined = allNets.map((n) => Object.assign({}, n, { __isNet: true }))
    .concat(allSources.map((s) => Object.assign({}, s, { __isNet: false })));
  count.textContent = combined.length
    ? (combined.length + (combined.length === 1 ? ' connection' : ' connections')) : '';
  if (!combined.length) {
    host.appendChild(el('p', { className: 'adm-hint', text: 'No connections configured yet -- add one below.' }));
    return;
  }
  combined.forEach((row) => host.appendChild(renderConnectionRow(row)));
}

function renderConnectionRow(row) {
  const key = connectionKey(row);
  const wrap = el('div', { className: 'adm-net' });
  const rowEl = el('div', { className: 'adm-net-row' });
  const open = expandedConnections.has(key);
  rowEl.appendChild(el('span', { className: 'adm-caret', text: open ? '▾' : '▸' }));

  const info = el('div', { className: 'adm-net-info' });
  info.appendChild(el('strong', { text: row.label }));
  // Shows the KIND, not the protocol -- corescope and beacon are both
  // 'mc' and otherwise indistinguishable in this row, so a protocol
  // badge would leave an operator unable to tell them apart.
  info.appendChild(el('span', { className: 'adm-badge', text: NET_KIND_LABELS[row.kind] || row.kind }));
  const cName = communityName(row.community_id);
  info.appendChild(el('span', { className: 'adm-hint', text: cName || 'no community' }));
  if (row.__isNet) {
    // window_text comes straight off the server (GET
    // /api/admin/checkin/nets) -- a human-readable public-style
    // schedule string, e.g. "Wednesdays, 5:00pm to midnight Mountain
    // time".
    info.appendChild(el('span', { text: row.window_text || '' }));
  } else {
    info.appendChild(el('span', { className: 'adm-hint', text: 'observation only' }));
  }
  info.appendChild(el('span', {
    className: 'adm-badge ' + (row.enabled ? 'adm-badge-ok' : 'adm-badge-bad'),
    text: row.enabled ? 'enabled' : 'disabled',
  }));
  rowEl.appendChild(info);
  rowEl.addEventListener('click', () => {
    if (expandedConnections.has(key)) expandedConnections.delete(key);
    else expandedConnections.add(key);
    renderConnections();
  });
  wrap.appendChild(rowEl);

  // Poll status and unresolved-sender count share the same health line
  // -- both answer "is this connection actually working," just for
  // two different silent failures (the connector being unreachable,
  // versus a message it fetched fine but could never credit to anyone
  // -- the latter only ever applies to a net, never an observation
  // source, which never credits anyone at all).
  const healthP = el('p', {
    className: 'adm-net-health' + (row.last_poll_error ? ' adm-status-bad' : ''),
  });
  healthP.appendChild(el('span', { text: netHealthText(row) }));
  if (row.__isNet) {
    healthP.appendChild(el('span', {
      text: '  ·  ' + row.last_checkin_count +
        (row.last_checkin_count === 1 ? ' check-in' : ' check-ins') +
        (row.last_checkin_net_date ? ' (' + row.last_checkin_net_date + ')' : ''),
    }));
    if (row.unresolved_count > 0) {
      healthP.appendChild(el('span', {
        className: 'adm-status-warn',
        text: '  ·  ' + row.unresolved_count +
          (row.unresolved_count === 1 ? ' unresolved sender' : ' unresolved senders') +
          ' (' + row.unresolved_net_date + ')',
      }));
    }
  }
  wrap.appendChild(healthP);

  if (row.__isNet && row.unresolved_senders && row.unresolved_senders.length) {
    wrap.appendChild(el('p', {
      className: 'adm-net-health',
      text: row.unresolved_senders
        .map((s) => s.sender_name + ' (' + s.message_count + ')')
        .join(', '),
    }));
  }

  if (open) wrap.appendChild(renderConnectionDetail(row));
  return wrap;
}

// The inline edit panel a row expands into -- same caret-expand idiom
// Players uses (renderPlayerDetail), not a second shared bottom form:
// every field here is a freshly built element read back by closure,
// not a fixed nf-* id, so any number of rows can be open (and edited)
// at once without colliding with each other or with the add-a-
// connection form below the list.
function renderConnectionDetail(row) {
  const originalIsNet = row.__isNet;
  const d = el('div', { className: 'adm-player-detail' });

  // -- Identity --
  d.appendChild(el('div', { className: 'adm-sub-title', text: 'Identity' }));
  const labelInput = el('input', { value: row.label });
  labelInput.placeholder = 'label';
  const communitySelect = el('select');
  populateCommunitySelect(communitySelect, row.community_id);
  const enabledCheck = el('input', { type: 'checkbox' });
  enabledCheck.checked = !!row.enabled;
  const identityRow = el('div', { className: 'adm-form' });
  identityRow.appendChild(labelInput);
  identityRow.appendChild(fieldLabel('Community', communitySelect, true));
  identityRow.appendChild(checkLabel('Enabled', enabledCheck));
  d.appendChild(identityRow);

  // -- Connector --
  d.appendChild(el('div', { className: 'adm-sub-title', text: 'Connector' }));
  const kindSelect = el('select');
  Object.keys(NET_KIND_LABELS).forEach((k) => {
    kindSelect.appendChild(el('option', { value: k, text: NET_KIND_LABELS[k] }));
  });
  kindSelect.value = row.kind;
  const kindRow = el('div', { className: 'adm-form' });
  kindRow.appendChild(fieldLabel('Connector kind', kindSelect));
  d.appendChild(kindRow);

  const connectorInput = el('input', { value: row.connector_url || '' });
  const connectorRow = el('div', { className: 'adm-form' });
  connectorRow.appendChild(connectorInput);
  d.appendChild(connectorRow);

  const officialHint = el('p', { className: 'adm-hint', text: 'Uses the official Meshtastic broker at mqtt.meshtastic.org.' });
  d.appendChild(officialHint);

  const channelInput = el('input', { value: row.channel || '' });
  channelInput.placeholder = 'channel';
  const channelSelect = el('select');
  channelSelect.hidden = true;
  const channelOut = el('span', { className: 'adm-hint' });
  const loadChannelsBtn = btn('Load channels', 'adm-btn-quiet', async (b) => {
    b.disabled = true;
    await loadConnectorChannels(kindSelect.value, connectorInput.value.trim(), channelSelect, channelInput, channelOut);
    b.disabled = false;
  });
  channelSelect.addEventListener('change', () => { channelInput.value = channelSelect.value; });
  const channelRow = el('div', { className: 'adm-form' });
  channelRow.appendChild(channelInput);
  channelRow.appendChild(loadChannelsBtn);
  channelRow.appendChild(channelSelect);
  d.appendChild(channelRow);
  d.appendChild(channelOut);

  const hashtagInput = el('input', { value: row.hashtag || '' });
  hashtagInput.placeholder = 'hashtag, e.g. #freq51';
  const hashtagRow = el('div', { className: 'adm-form' });
  hashtagRow.appendChild(hashtagInput);
  d.appendChild(hashtagRow);

  const mqttHint = el('p', {
    className: 'adm-hint',
    text: 'Topic root and channel are both REQUIRED for the official Meshtastic broker -- e.g. topic root msh/US, channel LongFast.',
  });
  d.appendChild(mqttHint);

  const topicRootInput = el('input', { value: row.topic_root || '' });
  topicRootInput.placeholder = 'topic root, e.g. msh/US (blank = subscribe broadly)';
  const brokerUsernameInput = el('input', { value: row.broker_username || '' });
  brokerUsernameInput.placeholder = 'broker username (optional)';
  const mqttRow1 = el('div', { className: 'adm-form' });
  mqttRow1.appendChild(topicRootInput);
  mqttRow1.appendChild(brokerUsernameInput);
  d.appendChild(mqttRow1);

  // Secrets are NEVER echoed back (see app/admin_ops.py's
  // _scrub_secrets) -- these inputs start blank every time a row
  // opens, and a blank submission means "keep the existing value,"
  // not "clear it"; the has_* booleans the row already carries are
  // shown as a hint instead, and "Clear" is the only way to actually
  // blank one out.
  const brokerPasswordInput = el('input', { type: 'password' });
  brokerPasswordInput.placeholder = 'leave blank to keep current';
  const clearBrokerPassword = el('input', { type: 'checkbox' });
  const brokerPasswordHint = el('span', { className: 'adm-hint', text: row.has_broker_password ? 'currently set' : 'not set' });
  const mqttRow2 = el('div', { className: 'adm-form' });
  mqttRow2.appendChild(fieldLabel('Broker password', brokerPasswordInput, true));
  mqttRow2.appendChild(checkLabel('Clear', clearBrokerPassword));
  mqttRow2.appendChild(brokerPasswordHint);
  d.appendChild(mqttRow2);

  const channelKeyInput = el('input', { type: 'password' });
  channelKeyInput.placeholder = 'base64 PSK, blank = Meshtastic default';
  const clearChannelKey = el('input', { type: 'checkbox' });
  const channelKeyHint = el('span', {
    className: 'adm-hint',
    text: row.has_channel_key ? 'currently set (blank = Meshtastic default)' : 'not set -- using Meshtastic default key',
  });
  const mqttRow3 = el('div', { className: 'adm-form' });
  mqttRow3.appendChild(fieldLabel('Channel key', channelKeyInput, true));
  mqttRow3.appendChild(checkLabel('Clear', clearChannelKey));
  mqttRow3.appendChild(channelKeyHint);
  d.appendChild(mqttRow3);

  // -- Net check-in schedule --
  d.appendChild(el('div', { className: 'adm-sub-title', text: 'Net check-in schedule' }));
  const scoresNetCheck = el('input', { type: 'checkbox' });
  scoresNetCheck.checked = originalIsNet;
  const scoresNetRow = el('div', { className: 'adm-form' });
  scoresNetRow.appendChild(checkLabel('Scores a net', scoresNetCheck));
  d.appendChild(scoresNetRow);

  const scheduleGroup = el('div');
  const weekdaySelect = el('select');
  ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'].forEach((name, i) => {
    weekdaySelect.appendChild(el('option', { value: String(i), text: name }));
  });
  weekdaySelect.value = String(row.weekday != null ? row.weekday : 2);
  const startHourInput = el('input', { type: 'number' });
  startHourInput.min = '0'; startHourInput.max = '23';
  startHourInput.value = row.start_hour != null ? row.start_hour : 17;
  const endHourInput = el('input', { type: 'number' });
  endHourInput.min = '0'; endHourInput.max = '23';
  endHourInput.value = row.end_hour != null ? row.end_hour : 23;
  const timezoneSelect = el('select');
  NET_TIMEZONE_OPTIONS.forEach(([value, text]) => timezoneSelect.appendChild(el('option', { value: value, text: text })));
  setTimezoneSelectValue(timezoneSelect, row.timezone || 'America/Boise');
  const scheduleRow1 = el('div', { className: 'adm-form' });
  scheduleRow1.appendChild(fieldLabel('Weekday', weekdaySelect));
  scheduleRow1.appendChild(fieldLabel('Start hour', startHourInput));
  scheduleRow1.appendChild(fieldLabel('End hour', endHourInput));
  scheduleRow1.appendChild(fieldLabel('Timezone', timezoneSelect, true));
  scheduleGroup.appendChild(scheduleRow1);

  const startDateInput = el('input', { type: 'date' });
  startDateInput.value = row.start_date || '';
  const scheduleRow2 = el('div', { className: 'adm-form' });
  scheduleRow2.appendChild(fieldLabel('Start date', startDateInput));
  scheduleGroup.appendChild(scheduleRow2);
  scheduleGroup.appendChild(el('p', {
    className: 'adm-hint',
    text: 'An empty start date blocks every award for this net.',
  }));
  d.appendChild(scheduleGroup);

  function refreshVisibility() {
    applyConnectorVisibility(kindSelect.value, {
      channelRow, loadChannelsBtn, channelSelect, connectorRow, officialHint,
      mqttRow1, brokerUsername: brokerUsernameInput, mqttRow2, mqttRow3, mqttHint,
      connectorInput, hashtagRow,
    });
    const scoresNet = scoresNetCheck.checked;
    scheduleGroup.hidden = !scoresNet;
    // An observation source has no hashtag column at all, regardless
    // of connector kind -- layered on top of applyConnectorVisibility's
    // own kind-based hashtag decision, never in place of it.
    hashtagRow.hidden = hashtagRow.hidden || !scoresNet;
  }
  kindSelect.addEventListener('change', refreshVisibility);
  scoresNetCheck.addEventListener('change', refreshVisibility);
  refreshVisibility();

  // -- Status --
  d.appendChild(el('div', { className: 'adm-sub-title', text: 'Status' }));
  const statusP = el('p', { className: 'adm-net-health' + (row.last_poll_error ? ' adm-status-bad' : '') });
  statusP.appendChild(el('span', { text: netHealthText(row) }));
  d.appendChild(statusP);

  const out = el('div', { className: 'adm-result' });
  const actionsRow = el('div', { className: 'adm-form' });
  actionsRow.appendChild(btn('Save', 'adm-btn', async (b) => {
    out.textContent = '';
    const communityVal = communitySelect.value;
    const basePayload = {
      id: row.id,
      label: labelInput.value.trim(),
      kind: kindSelect.value,
      connector_url: connectorInput.value.trim(),
      channel: channelInput.value.trim(),
      enabled: enabledCheck.checked,
      topic_root: topicRootInput.value.trim(),
      broker_username: brokerUsernameInput.value.trim(),
      broker_password: brokerPasswordInput.value,
      channel_key: channelKeyInput.value.trim(),
      clear_broker_password: clearBrokerPassword.checked,
      clear_channel_key: clearChannelKey.checked,
      community_id: communityVal ? parseInt(communityVal, 10) : null,
    };
    const scoresNet = scoresNetCheck.checked;
    const scheduleFields = {
      hashtag: hashtagInput.value.trim(),
      weekday: parseInt(weekdaySelect.value, 10),
      start_hour: parseInt(startHourInput.value, 10),
      end_hour: parseInt(endHourInput.value, 10),
      timezone: timezoneSelect.value.trim(),
      start_date: startDateInput.value,
    };
    b.disabled = true;
    try {
      if (originalIsNet && scoresNet) {
        await post('/api/admin/checkin/nets/update', Object.assign({}, basePayload, scheduleFields));
        setStatus('Updated net ' + basePayload.label, false);
      } else if (!originalIsNet && !scoresNet) {
        await post('/api/admin/observation/sources/update', basePayload);
        setStatus('Updated source ' + basePayload.label, false);
      } else if (originalIsNet && !scoresNet) {
        // Toggled off -- the conversion endpoint carries over label/
        // kind/connector/credentials/enabled/community_id from the
        // existing net itself; it does not accept edits to those
        // fields in the same request, so any edits made to them here
        // are not applied when a toggle is also being saved.
        await post('/api/admin/checkin/nets/convert-to-source', { id: row.id });
        setStatus('Converted ' + basePayload.label + ' to an observation source', false);
      } else {
        // !originalIsNet && scoresNet -- toggled on; same "existing
        // fields carry over, only the schedule is new" contract as
        // above, in the other direction.
        await post('/api/admin/observation/sources/convert-to-net', Object.assign({ id: row.id }, scheduleFields));
        setStatus('Converted ' + basePayload.label + ' to a net', false);
      }
      expandedConnections.delete(connectionKey(row));
      await Promise.all([loadNets(), loadSources()]);
    } catch (e) {
      out.textContent = 'Failed: ' + e.message;
      b.disabled = false;
    }
  }));
  actionsRow.appendChild(btn('Delete', 'adm-btn-danger', async (b) => {
    const typed = window.prompt(
      'Deleting removes this ' + (originalIsNet ? 'net' : 'observation source') + '.\n\nType ' + row.label + ' to confirm.');
    if (!typed) return;
    b.disabled = true;
    try {
      if (originalIsNet) {
        await post('/api/admin/checkin/nets/delete', { id: row.id, label: typed });
      } else {
        await post('/api/admin/observation/sources/delete', { id: row.id, label: typed });
      }
      setStatus('Deleted ' + row.label, false);
      expandedConnections.delete(connectionKey(row));
      if (originalIsNet) await loadNets(); else await loadSources();
    } catch (e) { setStatus('Failed: ' + e.message, true); b.disabled = false; }
  }));
  d.appendChild(actionsRow);
  d.appendChild(out);
  return d;
}

async function loadNets() {
  try {
    const d = await api('/api/admin/checkin/nets');
    allNets = d.nets || [];
    if (d.config) renderConfigForm(d.config);
  } catch (e) {
    setStatus('Nets load failed: ' + e.message, true);
  }
  renderConnections();
}

async function loadSources() {
  try {
    const d = await api('/api/admin/observation/sources');
    allSources = d.sources || [];
  } catch (e) {
    setStatus('Sources load failed: ' + e.message, true);
  }
  renderConnections();
}

// ---- add-a-connection form (nf-* ids) -----------------------------------
//
// Add-only -- editing an existing connection happens inline in its own
// row (renderConnectionDetail above), not here. "Scores a net" decides
// which of the two create endpoints this form posts to.

function updateConnectionFormKind() {
  const kind = document.getElementById('nf-kind').value;
  applyConnectorVisibility(kind, {
    channelRow: document.getElementById('nf-channel-row'),
    loadChannelsBtn: document.getElementById('nf-load-channels'),
    channelSelect: document.getElementById('nf-channel-select'),
    connectorRow: document.getElementById('nf-connector-row'),
    officialHint: document.getElementById('nf-official-broker-hint'),
    mqttRow1: document.getElementById('nf-mqtt-row-1'),
    brokerUsername: document.getElementById('nf-broker-username'),
    mqttRow2: document.getElementById('nf-mqtt-row-2'),
    mqttRow3: document.getElementById('nf-mqtt-row-3'),
    mqttHint: document.getElementById('nf-mqtt-meshtastic-hint'),
    connectorInput: document.getElementById('nf-connector'),
    hashtagRow: document.getElementById('nf-hashtag-row'),
  });
  const scoresNet = document.getElementById('nf-scores-net').checked;
  document.getElementById('nf-schedule-group').hidden = !scoresNet;
  const hashtagRow = document.getElementById('nf-hashtag-row');
  hashtagRow.hidden = hashtagRow.hidden || !scoresNet;
}

async function loadConnectionChannelsAdd(b) {
  const connector = document.getElementById('nf-connector').value.trim();
  const kind = document.getElementById('nf-kind').value;
  const out = document.getElementById('nf-result');
  b.disabled = true;
  await loadConnectorChannels(kind, connector, document.getElementById('nf-channel-select'), document.getElementById('nf-channel'), out);
  b.disabled = false;
}

function resetConnectionForm() {
  document.getElementById('nf-kind').value = 'corescope';
  document.getElementById('nf-label').value = '';
  document.getElementById('nf-connector').value = '';
  document.getElementById('nf-channel').value = '';
  document.getElementById('nf-hashtag').value = '';
  document.getElementById('nf-weekday').value = '2';
  document.getElementById('nf-start-hour').value = '17';
  document.getElementById('nf-end-hour').value = '23';
  setTimezoneSelectValue(document.getElementById('nf-timezone'), 'America/Boise');
  document.getElementById('nf-start-date').value = '';
  document.getElementById('nf-enabled').checked = true;
  document.getElementById('nf-topic-root').value = '';
  document.getElementById('nf-broker-username').value = '';
  document.getElementById('nf-broker-password').value = '';
  document.getElementById('nf-channel-key').value = '';
  document.getElementById('nf-clear-broker-password').checked = false;
  document.getElementById('nf-clear-channel-key').checked = false;
  document.getElementById('nf-broker-password-hint').textContent = '';
  document.getElementById('nf-channel-key-hint').textContent = '';
  document.getElementById('nf-scores-net').checked = true;
  const select = document.getElementById('nf-channel-select');
  select.hidden = true;
  select.replaceChildren();
  populateCommunitySelect(document.getElementById('nf-community'), null);
  updateConnectionFormKind();
  // Deliberately does not touch nf-result -- saveConnection() calls
  // this right after a successful save specifically to clear the form
  // back to blank, and clearing the result here would erase the
  // "Connection added" message in the same breath it appears.
}

async function saveConnection(b) {
  const out = document.getElementById('nf-result');
  out.replaceChildren();
  const scoresNet = document.getElementById('nf-scores-net').checked;
  const communityVal = document.getElementById('nf-community').value;
  const basePayload = {
    label: document.getElementById('nf-label').value.trim(),
    kind: document.getElementById('nf-kind').value,
    connector_url: document.getElementById('nf-connector').value.trim(),
    channel: document.getElementById('nf-channel').value.trim(),
    enabled: document.getElementById('nf-enabled').checked,
    topic_root: document.getElementById('nf-topic-root').value.trim(),
    broker_username: document.getElementById('nf-broker-username').value.trim(),
    broker_password: document.getElementById('nf-broker-password').value,
    channel_key: document.getElementById('nf-channel-key').value.trim(),
    clear_broker_password: document.getElementById('nf-clear-broker-password').checked,
    clear_channel_key: document.getElementById('nf-clear-channel-key').checked,
    community_id: communityVal ? parseInt(communityVal, 10) : null,
  };
  b.disabled = true;
  try {
    if (scoresNet) {
      const payload = Object.assign({}, basePayload, {
        hashtag: document.getElementById('nf-hashtag').value.trim(),
        weekday: parseInt(document.getElementById('nf-weekday').value, 10),
        start_hour: parseInt(document.getElementById('nf-start-hour').value, 10),
        end_hour: parseInt(document.getElementById('nf-end-hour').value, 10),
        timezone: document.getElementById('nf-timezone').value.trim(),
        start_date: document.getElementById('nf-start-date').value,
      });
      await post('/api/admin/checkin/nets/create', payload);
      out.textContent = 'Net added.';
    } else {
      await post('/api/admin/observation/sources/create', basePayload);
      out.textContent = 'Source added.';
    }
    resetConnectionForm();
    await Promise.all([loadNets(), loadSources()]);
  } catch (e) {
    out.textContent = 'Failed: ' + e.message;
  }
  b.disabled = false;
}


// ---- places rotation preview -------------------------------------------

async function previewPlaces(b) {
  const week = document.getElementById('pl-week').value.trim();
  const out = document.getElementById('pl-result');
  out.replaceChildren();
  if (week && !/^\d{4}-\d{2}-\d{2}$/.test(week)) {
    out.textContent = 'Week must look like 2026-08-19, or be left blank.';
    return;
  }
  b.disabled = true;
  try {
    const qs = week ? ('?week_start=' + encodeURIComponent(week)) : '';
    const r = await api('/api/admin/places/preview' + qs);

    out.appendChild(el('p', {
      text: r.live_rotating_count + ' live rotating places for the week of ' + r.week_start +
        ' -- ' + r.region_cells_with_a_live_pick + ' of ' + r.region_cells_with_candidates +
        ' candidate-bearing region cells filled.',
    }));
    out.appendChild(el('p', {
      className: 'adm-hint',
      text: 'Candidates per cell: min ' + r.candidates_per_cell.min +
        ', max ' + r.candidates_per_cell.max + ', mean ' + r.candidates_per_cell.mean + '.',
    }));

    if (r.by_type && Object.keys(r.by_type).length) {
      const parts = Object.keys(r.by_type).sort().map((t) => t + ': ' + r.by_type[t]);
      out.appendChild(el('p', { className: 'adm-hint', text: 'By type -- ' + parts.join(', ') + '.' }));
    }

    if (r.densest_cells && r.densest_cells.length) {
      out.appendChild(el('h3', { className: 'adm-h3', text: 'Densest region cells' }));
      const list = el('ul');
      for (const c of r.densest_cells) {
        list.appendChild(el('li', {
          text: c.cell + ': ' + c.candidates + ' candidates, ' + c.chosen + ' chosen',
        }));
      }
      out.appendChild(list);
    }

    if (r.sample && r.sample.length) {
      out.appendChild(el('h3', { className: 'adm-h3', text: 'Sample of the draw' }));
      const list = el('ul');
      for (const p of r.sample) {
        list.appendChild(el('li', { text: p.name + ' (' + p.ref_type + ', ' + p.points + ' pts)' }));
      }
      out.appendChild(list);
    }
  } catch (e) {
    out.textContent = 'Failed: ' + e.message;
  }
  b.disabled = false;
}

// ---- notice -------------------------------------------------------------

function renderNoticeCurrent(n) {
  const line = document.getElementById('nt-current');
  if (!n.active || !n.title) {
    line.textContent = 'Nothing is currently shown to players.';
    return;
  }
  line.textContent = 'Currently shown to players: "' + n.title + '" (version ' + n.version_key + ').';
}

async function loadNotice() {
  try {
    const n = await api('/api/admin/notice');
    document.getElementById('nt-version').value = n.version_key || '';
    document.getElementById('nt-title').value = n.title || '';
    document.getElementById('nt-body').value = n.body || '';
    document.getElementById('nt-active').checked = !!n.active;
    renderNoticeCurrent(n);
  } catch (e) {
    document.getElementById('nt-current').textContent = 'Could not load: ' + e.message;
  }
}

async function saveNotice(b, overrideActive) {
  const out = document.getElementById('nt-result');
  out.replaceChildren();
  const version = document.getElementById('nt-version').value.trim();
  const title = document.getElementById('nt-title').value.trim();
  const bodyText = document.getElementById('nt-body').value.trim();
  const active = overrideActive !== undefined ? overrideActive : document.getElementById('nt-active').checked;
  if (!version || !title || !bodyText) {
    out.textContent = 'Version key, title and body are all required.';
    return;
  }
  b.disabled = true;
  try {
    const n = await post('/api/admin/notice',
      { version_key: version, title: title, body: bodyText, active: active });
    document.getElementById('nt-active').checked = n.active;
    renderNoticeCurrent(n);
    out.textContent = 'Saved.';
  } catch (e) {
    out.textContent = 'Failed: ' + e.message;
  }
  b.disabled = false;
}

// ---- discord announcements ----------------------------------------------
//
// Same "whole singleton, one POST" shape savePaint()/saveNotice() above
// already use for their own DB-backed config. The webhook URL never
// comes back from GET /api/admin/discord (app/admin_ops.py's
// _scrub_discord_secrets) -- only webhook_set/webhook_hint, the same
// has_api_key-shaped hint the Paint section's API key field already
// uses -- so the input is always rendered blank and a blank submit
// leaves the stored value alone (clear_webhook is the explicit way to
// actually blank it).

function renderDiscordForm(cfg) {
  document.getElementById('dc-enabled').checked = !!cfg.enabled;
  document.getElementById('dc-month-honors').checked = !!cfg.announce_month_honors;
  document.getElementById('dc-season-close').checked = !!cfg.announce_season_close;
  document.getElementById('dc-weekly-recap').checked = !!cfg.announce_weekly_recap;
  document.getElementById('dc-net-wrapup').checked = !!cfg.announce_net_wrapup;
  document.getElementById('dc-webhook-url').value = '';
  document.getElementById('dc-clear-webhook').checked = false;
  document.getElementById('dc-webhook-hint').textContent = cfg.webhook_set
    ? ('currently set, ending in ' + cfg.webhook_hint)
    : 'not set';
  document.getElementById('dc-username').value = cfg.username || '';
  document.getElementById('dc-team-emoji').value = cfg.team_emoji || '';

  // Team roles (app/discord_bot.py) -- an entirely separate feature
  // from every field above, but roles_enabled/guild_id are plain,
  // non-secret columns saved together with the rest of this same form
  // (see saveDiscord() below). bot_token_set never carries the token
  // itself -- only whether DISCORD_BOT_TOKEN is configured at all, the
  // same has_api_key-shaped hint every other secret field on this page
  // already uses.
  document.getElementById('dc-roles-enabled').checked = !!cfg.roles_enabled;
  document.getElementById('dc-guild-id').value = cfg.guild_id || '';
  document.getElementById('dc-bot-token-hint').textContent = cfg.bot_token_set
    ? 'Bot token: configured'
    : 'Bot token: not set (DISCORD_BOT_TOKEN environment variable)';

  // Team channels (app/discord_bot.py's ensure_team_channels()) -- a
  // second feature layered on the roles above, saved together with the
  // rest of this same form. team_category_id (this app's own
  // discovered/created id) is never shown or edited here -- it's not
  // admin-editable, see saveDiscord() below.
  document.getElementById('dc-team-channels-enabled').checked = !!cfg.team_channels_enabled;
  document.getElementById('dc-team-category-name').value = cfg.team_category_name || '';

  // Slash commands (app/discord_interactions.py) -- a fourth, separate
  // Discord integration, saved together with the rest of this same
  // form. Neither app_id nor public_key is a secret (see
  // discord_config's own comment in app/db.py), so both come back and
  // go out as plain values -- no webhook_set-style hint needed.
  document.getElementById('dc-slash-enabled').checked = !!cfg.slash_enabled;
  document.getElementById('dc-app-id').value = cfg.app_id || '';
  document.getElementById('dc-public-key').value = cfg.public_key || '';

  // Leaderboard (app/discord_leaderboard.py) -- a fifth, separate
  // feature, saved together with the rest of this same form. Neither
  // the interval nor top-N is a secret, so both come back and go out as
  // plain values -- no webhook_set-style hint needed.
  document.getElementById('dc-leaderboard-enabled').checked = !!cfg.leaderboard_enabled;
  document.getElementById('dc-leaderboard-interval').value = cfg.leaderboard_interval_seconds || '';
  document.getElementById('dc-leaderboard-topn').value = cfg.leaderboard_top_n || '';
}

// Leaderboard status (GET /api/admin/discord's own `leaderboard` block --
// app/discord_leaderboard.py's leaderboard_admin_status()): whether a
// message has ever been posted, a jump link, pinned yes/no, and when its
// content last actually changed -- rendered separately from the plain
// config fields above since this is READ-ONLY status, not a form field.
function renderLeaderboardStatus(status) {
  const jumpEl = document.getElementById('dc-leaderboard-jump');
  if (status && status.posted && status.jump_url) {
    jumpEl.replaceChildren(el('a', { href: status.jump_url, target: '_blank', rel: 'noopener', text: status.jump_url }));
  } else if (status && status.posted) {
    jumpEl.textContent = 'posted (set a Guild ID above for a jump link)';
  } else {
    jumpEl.textContent = 'not posted yet';
  }
  document.getElementById('dc-leaderboard-pinned').textContent = status && status.posted
    ? (status.pinned ? 'yes' : 'no')
    : '--';
  document.getElementById('dc-leaderboard-updated').textContent = status && status.updated_at
    ? fmtTs(status.updated_at)
    : '--';
}

// Team roles (app/discord_bot.py) -- discord_team_role rows. role_id
// stays informational (repaired only by the buttons below); channel_id
// is now editable per row, POSTing to /api/admin/discord/team-channel --
// this is the operator's own way to resolve one of
// ensure_team_channels()'s `ambiguous` entries (see
// renderAmbiguousChannels() below) by hand-picking the right channel id,
// or to clear a bad pick back to empty (which lets the next "Create /
// repair" run adopt or create one on its own again).
function renderTeamRoles(teamRoles) {
  const host = document.getElementById('dc-team-roles');
  host.replaceChildren();
  if (!teamRoles.length) {
    host.appendChild(el('p', { className: 'adm-hint', text: 'No team roles discovered yet -- use "Create / repair team roles and channels" below.' }));
    return;
  }
  teamRoles.forEach((r) => {
    const row = el('div', { className: 'adm-row' });
    const info = el('div', { className: 'adm-row-info' });
    info.appendChild(el('strong', { className: 'adm-mono', text: r.team }));
    info.appendChild(el('span', { className: 'adm-mono', text: r.role_id }));
    row.appendChild(info);

    const form = el('div', { className: 'adm-row-actions' });
    const channelInput = el('input', { type: 'text', placeholder: 'channel id (blank = none)' });
    channelInput.className = 'adm-mono';
    channelInput.autocomplete = 'off';
    channelInput.value = r.channel_id || '';
    form.appendChild(channelInput);
    const rowOut = el('span', { className: 'adm-hint' });
    form.appendChild(btn('Save', 'adm-btn-quiet', async (b) => {
      b.disabled = true;
      rowOut.textContent = '';
      try {
        const value = channelInput.value.trim();
        await post('/api/admin/discord/team-channel', { team: r.team, channel_id: value ? value : null });
        await loadDiscord();
      } catch (e) {
        rowOut.textContent = 'Failed: ' + e.message;
      }
      b.disabled = false;
    }));
    form.appendChild(rowOut);
    row.appendChild(form);

    host.appendChild(row);
  });
}

// ensure_team_channels()'s `ambiguous` bucket (app/discord_bot.py): a
// team where more than one channel in the configured category
// normalizes to its name -- that function refuses to guess, so this
// just lists the candidate (real, un-normalized) channel names and
// points at the per-row channel id field above, the only way to
// actually resolve one. Rendered fresh after every "Create / repair"
// click (see ensureDiscordRoles() below) -- purely informational,
// cleared to empty (nothing shown) once ensure reports no ambiguous
// teams at all, rather than left showing a stale prior result.
function renderAmbiguousChannels(ambiguous) {
  const host = document.getElementById('dc-roles-ambiguous');
  host.replaceChildren();
  if (!ambiguous || !ambiguous.length) return;
  host.appendChild(el('p', { className: 'adm-hint adm-status-bad', text: 'Could not tell which channel is which for these teams -- pick one by hand in the channel id field above, then Save:' }));
  ambiguous.forEach((a) => {
    host.appendChild(el('p', { className: 'adm-hint', text: a.team + ': ' + a.candidates.join(', ') }));
  });
}

function renderReconcileStatus(lastReconcile) {
  const out = document.getElementById('dc-roles-reconcile-status');
  if (!lastReconcile || !lastReconcile.at) {
    out.textContent = 'No reconcile has run yet.';
    return;
  }
  out.textContent = 'Last reconcile: ' + fmtTs(lastReconcile.at)
    + ' -- checked ' + lastReconcile.checked + ', changed ' + lastReconcile.changed + '.';
}

function renderDiscordOutbox(outbox) {
  const summary = document.getElementById('dc-outbox-summary');
  summary.replaceChildren();
  const p = el('p', { className: 'adm-net-health' + (outbox.failed > 0 ? ' adm-status-bad' : '') });
  p.appendChild(el('span', {
    text: outbox.pending + ' pending, ' + outbox.posted + ' posted, ' + outbox.failed + ' failed',
  }));
  summary.appendChild(p);

  const host = document.getElementById('dc-outbox');
  host.replaceChildren();
  if (!outbox.recent.length) {
    host.appendChild(el('p', { className: 'adm-hint', text: 'No announcements queued yet.' }));
    return;
  }
  outbox.recent.forEach((row) => {
    const rowEl = el('div', { className: 'adm-row' });
    const info = el('div', { className: 'adm-row-info' });
    info.appendChild(el('span', { className: 'adm-mono', text: row.kind + ':' + row.key }));
    info.appendChild(el('span', {
      text: row.posted_at ? ('posted ' + fmtTs(row.posted_at))
        : (row.attempts + (row.attempts === 1 ? ' attempt' : ' attempts')),
    }));
    if (row.last_error) {
      info.appendChild(el('span', { text: row.last_error }));
    }
    rowEl.appendChild(info);
    if (!row.posted_at && row.attempts > 0) {
      rowEl.appendChild(btn('Retry', 'adm-btn-quiet', (b) => retryDiscordOutboxRow(b, row.id)));
    }
    host.appendChild(rowEl);
  });
}

// Per-kind channel routing (app/db.py's discord_channel,
// app/admin_ops.py's POST /api/admin/discord/channel) -- same
// never-show-the-real-webhook rule as the default webhook field above:
// GET /api/admin/discord's `channels` never carries a real URL, only
// webhook_set/webhook_hint, so each row's webhook input always renders
// blank and a blank Save leaves that row's stored value alone (its own
// "Clear" checkbox is the explicit way to blank it).
function renderDiscordChannels(channels) {
  const host = document.getElementById('dc-channels');
  host.replaceChildren();
  if (!channels.length) {
    host.appendChild(el('p', { className: 'adm-hint', text: 'No per-kind routes configured -- every kind uses the default webhook above.' }));
    return;
  }
  channels.forEach((c) => {
    const row = el('div', { className: 'adm-row' });

    const info = el('div', { className: 'adm-row-info' });
    info.appendChild(el('strong', { className: 'adm-mono', text: c.kind }));
    info.appendChild(el('span', {
      text: c.webhook_set ? ('webhook set, ending in ' + c.webhook_hint) : 'webhook not set',
    }));
    row.appendChild(info);

    const form = el('div', { className: 'adm-row-actions' });
    const enabledLabel = el('label', { className: 'adm-check-label' });
    const enabledBox = el('input', { type: 'checkbox' });
    enabledBox.className = 'adm-check';
    enabledBox.checked = !!c.enabled;
    enabledLabel.appendChild(enabledBox);
    enabledLabel.appendChild(document.createTextNode(' On'));
    form.appendChild(enabledLabel);

    const webhookInput = el('input', { type: 'password', placeholder: 'leave blank to keep current' });
    webhookInput.autocomplete = 'off';
    form.appendChild(webhookInput);

    const clearLabel = el('label', { className: 'adm-check-label' });
    const clearBox = el('input', { type: 'checkbox' });
    clearBox.className = 'adm-check';
    clearLabel.appendChild(clearBox);
    clearLabel.appendChild(document.createTextNode(' Clear'));
    form.appendChild(clearLabel);

    form.appendChild(btn('Save', 'adm-btn-quiet', async (b) => {
      b.disabled = true;
      try {
        const payload = { kind: c.kind, enabled: enabledBox.checked };
        if (clearBox.checked) {
          payload.clear_webhook = true;
        } else if (webhookInput.value) {
          payload.webhook_url = webhookInput.value;
        }
        await post('/api/admin/discord/channel', payload);
        await loadDiscord();
      } catch (e) {
        setStatus('Channel route save failed: ' + e.message, true);
      }
      b.disabled = false;
    }));
    row.appendChild(form);

    host.appendChild(row);
  });
}

async function addDiscordChannel(b) {
  const input = document.getElementById('dc-channel-new-kind');
  const out = document.getElementById('dc-channel-result');
  out.replaceChildren();
  const kind = input.value.trim();
  if (!kind) { out.textContent = 'Give it a kind first.'; return; }
  b.disabled = true;
  try {
    await post('/api/admin/discord/channel', { kind: kind, enabled: true });
    input.value = '';
    await loadDiscord();
  } catch (e) {
    out.textContent = 'Failed: ' + e.message;
  }
  b.disabled = false;
}

async function loadDiscord() {
  try {
    const d = await api('/api/admin/discord');
    renderDiscordForm(d.config);
    renderDiscordChannels(d.channels || []);
    renderDiscordOutbox(d.outbox);
    renderTeamRoles(d.team_roles || []);
    renderReconcileStatus(d.last_reconcile);
    renderLeaderboardStatus(d.leaderboard);
    // Interactions Endpoint URL -- app/admin_ops.py's GET
    // /api/admin/discord only computes this when OAUTH_PUBLIC_BASE_URL
    // is configured; blank otherwise, same as every absolute-or-omitted
    // URL app/discord_notify.py builds.
    document.getElementById('dc-slash-endpoint-url').textContent =
      d.interactions_endpoint_url || '(set OAUTH_PUBLIC_BASE_URL first)';
  } catch (e) {
    setStatus('Discord config load failed: ' + e.message, true);
  }
}

async function saveDiscord(b) {
  const out = document.getElementById('dc-result');
  out.replaceChildren();

  const payload = {
    enabled: document.getElementById('dc-enabled').checked,
    announce_month_honors: document.getElementById('dc-month-honors').checked,
    announce_season_close: document.getElementById('dc-season-close').checked,
    announce_weekly_recap: document.getElementById('dc-weekly-recap').checked,
    announce_net_wrapup: document.getElementById('dc-net-wrapup').checked,
    username: document.getElementById('dc-username').value.trim(),
    team_emoji: document.getElementById('dc-team-emoji').value.trim(),
    roles_enabled: document.getElementById('dc-roles-enabled').checked,
    guild_id: document.getElementById('dc-guild-id').value.trim(),
    team_channels_enabled: document.getElementById('dc-team-channels-enabled').checked,
    team_category_name: document.getElementById('dc-team-category-name').value.trim(),
    slash_enabled: document.getElementById('dc-slash-enabled').checked,
    app_id: document.getElementById('dc-app-id').value.trim(),
    public_key: document.getElementById('dc-public-key').value.trim(),
    leaderboard_enabled: document.getElementById('dc-leaderboard-enabled').checked,
    leaderboard_interval_seconds: parseInt(document.getElementById('dc-leaderboard-interval').value, 10) || 600,
    leaderboard_top_n: parseInt(document.getElementById('dc-leaderboard-topn').value, 10) || 5,
  };
  // Blank means keep the existing webhook -- see app/admin_ops.py's
  // admin_discord_update, the same convention the Paint section's own
  // api_key field already uses. clear_webhook is the explicit way to
  // actually blank it.
  if (document.getElementById('dc-clear-webhook').checked) {
    payload.clear_webhook = true;
  } else {
    const webhookUrl = document.getElementById('dc-webhook-url').value;
    if (webhookUrl) payload.webhook_url = webhookUrl;
  }

  b.disabled = true;
  try {
    const r = await post('/api/admin/discord', payload);
    renderDiscordForm(r.config);
    out.textContent = 'Saved.';
    setStatus('Discord config saved', false);
  } catch (e) {
    out.textContent = 'Failed: ' + e.message;
  }
  b.disabled = false;
}

async function sendDiscordTest(b) {
  const out = document.getElementById('dc-result');
  out.replaceChildren();
  b.disabled = true;
  try {
    await post('/api/admin/discord/test', {});
    out.textContent = 'Test announcement queued -- check the channel shortly.';
    await loadDiscord();
  } catch (e) {
    out.textContent = 'Failed: ' + e.message;
  }
  b.disabled = false;
}

async function retryDiscordOutboxRow(b, id) {
  b.disabled = true;
  try {
    await post('/api/admin/discord/outbox/retry', { id: id });
    await loadDiscord();
  } catch (e) {
    setStatus('Retry failed: ' + e.message, true);
  }
  b.disabled = false;
}

async function ensureDiscordRoles(b) {
  const out = document.getElementById('dc-roles-result');
  out.replaceChildren();
  document.getElementById('dc-roles-ambiguous').replaceChildren();
  b.disabled = true;
  try {
    const r = await post('/api/admin/discord/roles/ensure', {});
    let text = 'Roles -- created: ' + (r.created.join(', ') || 'none')
      + '. Recreated: ' + (r.recreated.join(', ') || 'none')
      + '. Reused: ' + (r.reused.join(', ') || 'none') + '.';
    // Channels (app/discord_bot.py's ensure_team_channels(), run right
    // after roles above): {"ok": false} here just means the feature
    // isn't turned on, or a Discord permission was missing -- not a
    // failure of the roles step above, which already succeeded by the
    // time this ran, so it renders as its own line rather than an error.
    const c = r.channels;
    if (c && c.ok) {
      text += ' Channels -- created: ' + (c.created.join(', ') || 'none')
        + '. Recreated: ' + (c.recreated.join(', ') || 'none')
        + '. Adopted: ' + (c.adopted.join(', ') || 'none') + '.';
      // "ambiguous" gets its own block below rather than folded into
      // this one-line summary -- each entry carries candidate channel
      // names an operator needs to actually read, not just a team list.
      renderAmbiguousChannels(c.ambiguous);
    } else if (c) {
      text += ' Channels: ' + (c.reason || 'not configured.');
    }
    out.textContent = text;
    await loadDiscord();
  } catch (e) {
    out.textContent = 'Failed: ' + e.message;
  }
  b.disabled = false;
}

async function reconcileDiscordRoles(b) {
  const out = document.getElementById('dc-roles-result');
  out.replaceChildren();
  b.disabled = true;
  try {
    const r = await post('/api/admin/discord/roles/reconcile', {});
    out.textContent = 'Checked ' + r.checked + ' player(s), changed ' + r.changed + '.';
    await loadDiscord();
  } catch (e) {
    out.textContent = 'Failed: ' + e.message;
  }
  b.disabled = false;
}

// Slash commands (app/discord_interactions.py) -- bulk-overwrites this
// guild's command list with app/discord_bot.py's register_commands().
// Save the Application ID/Public key fields (and turn Enabled on) with
// the Save button above FIRST -- this button reads whatever was last
// saved, not the form's current unsaved values.
async function registerDiscordSlashCommands(b) {
  const out = document.getElementById('dc-slash-result');
  out.replaceChildren();
  b.disabled = true;
  try {
    const r = await post('/api/admin/discord/slash/register', {});
    out.textContent = 'Registered: ' + r.commands.join(', ');
  } catch (e) {
    out.textContent = 'Failed: ' + e.message;
  }
  b.disabled = false;
}

// "Post / repair now" (app/discord_leaderboard.py's run_leaderboard_pass(
// force=True)) -- runs one pass immediately, ignoring the interval, and
// always re-asserts the pin even when the content itself didn't change
// (an operator may have unpinned it by hand). Save the Enabled/Interval/
// Top N fields above with the Save button FIRST -- this button reads
// whatever was last saved, not the form's current unsaved values, same
// caveat registerDiscordSlashCommands() already carries for its own
// fields.
async function runDiscordLeaderboard(b) {
  const out = document.getElementById('dc-leaderboard-result');
  out.replaceChildren();
  b.disabled = true;
  try {
    const r = await post('/api/admin/discord/leaderboard/run', {});
    out.textContent = r.ok ? ('Done: ' + r.reason) : ('Not run: ' + r.reason);
    await loadDiscord();
  } catch (e) {
    out.textContent = 'Failed: ' + e.message;
  }
  b.disabled = false;
}

// ---- read-API keys ----------------------------------------------------

async function loadApiClients() {
  const host = document.getElementById('apikeys');
  host.replaceChildren();
  try {
    const list = await api('/api/admin/api-clients');
    if (!list.length) {
      host.appendChild(el('p', { className: 'adm-hint', text: 'No keys issued yet.' }));
      return;
    }
    list.forEach((c) => {
      const row = el('div', { className: 'adm-row' });
      const info = el('div', { className: 'adm-row-info' });
      info.appendChild(el('span', { className: 'adm-mono', text: c.key_hash_prefix }));
      info.appendChild(el('strong', { text: c.label }));
      info.appendChild(el('span', { text: 'issued ' + fmtTs(c.created_at) }));
      info.appendChild(el('span', { text: 'last used ' + fmtTs(c.last_seen_at) }));
      info.appendChild(el('span', {
        className: 'adm-badge ' + (c.revoked ? 'adm-badge-bad' : 'adm-badge-ok'),
        text: c.revoked ? 'revoked' : 'active',
      }));
      row.appendChild(info);
      if (!c.revoked) {
        const actions = el('div', { className: 'adm-row-actions' });
        actions.appendChild(btn('Revoke', 'adm-btn-quiet', async (b) => {
          if (!window.confirm('Revoke "' + c.label + '"? Anything using it stops within a minute.')) return;
          b.disabled = true;
          try {
            await post('/api/admin/api-clients/revoke', { key_hash_prefix: c.key_hash_prefix });
            await loadApiClients();
          } catch (e) { window.alert('Failed: ' + e.message); b.disabled = false; }
        }));
        row.appendChild(actions);
      }
      host.appendChild(row);
    });
  } catch (e) {
    host.appendChild(el('p', { className: 'adm-hint', text: 'Could not load: ' + e.message }));
  }
}

async function createApiClient(b) {
  const input = document.getElementById('apikey-label');
  const out = document.getElementById('apikey-result');
  out.replaceChildren();
  if (!input.value.trim()) { out.textContent = 'Give it a label first.'; return; }
  b.disabled = true;
  try {
    const r = await post('/api/admin/api-clients/create', { label: input.value.trim() });
    revealKey(out, 'Key for "' + r.label + '"', r.key);
    input.value = '';
    await loadApiClients();
  } catch (e) {
    out.textContent = 'Failed: ' + e.message;
  }
  b.disabled = false;
}

// ---- session and navigation -------------------------------------------
//
// There is no sign-in FORM on this page any more -- the account page's
// own session cookie is what authenticates every /api/admin/* call
// (see api() above and app/admin_api.py's _role_guard()). checkAccess()
// below just asks GET /api/account whether the signed-in session (if
// any) holds a role, and shows either the panel or a plain "go sign in"
// message -- a UX convenience, never the actual security boundary.

function show(sectionName) {
  document.querySelectorAll('.adm-section').forEach((s) => {
    s.hidden = s.dataset.section !== sectionName;
  });
  document.querySelectorAll('.adm-nav-item').forEach((b) => {
    b.classList.toggle('active', b.dataset.section === sectionName);
  });
  // Deep-linkable, and survives a reload -- an operator who bookmarks
  // the players list should land on the players list.
  if (location.hash.slice(1) !== sectionName) {
    history.replaceState(null, '', '#' + sectionName);
  }
}

function badge(id, value, bad) {
  const b = document.getElementById(id);
  if (!b) return;
  b.textContent = value === 0 || value ? String(value) : '';
  b.className = 'adm-nav-badge' + (bad ? ' adm-nav-badge-bad' : '');
}

async function refreshAll() {
  const loads = [
    loadPlayers(), loadAccounts(), loadOverview(), loadApiClients(), loadNotice(),
    loadCommunities(), loadNets(), loadSources(),
    loadPaint(), loadTileRelease(), loadDiscord(), loadTraffic(), loadCheckinAwards(),
  ];
  await Promise.all(loads);
  badge('nav-players', allPlayers.length, false);
  // Orphan count, not total account count -- see this badge's own
  // comment in admin.html for why: the nav should surface a problem
  // (accounts nothing else can reach), not restate a size Players'
  // own badge already gives a close approximation of.
  const orphanCount = allAccounts.filter((a) => !a.player).length;
  badge('nav-accounts', orphanCount, orphanCount > 0);
}

function showNoAccess(message) {
  myRole = null;
  panelLoaded = false;
  stopTrafficPolling(); // no panel on screen for it to update any more
  document.getElementById('app').hidden = true;
  document.getElementById('login').hidden = false;
  document.getElementById('login-err').textContent = message || '';
}

async function showApp() {
  document.getElementById('login').hidden = true;
  document.getElementById('app').hidden = false;
  document.getElementById('topbar-role').textContent = myRole;
  const wanted = location.hash.slice(1);
  show(document.querySelector('.adm-section[data-section="' + wanted + '"]') ? wanted : 'overview');
  await refreshAll();
  // refreshAll() above just did the equivalent of one poll tick (its
  // own loadTraffic() call) -- start the recurring timer from here
  // rather than double-fetching immediately.
  startTrafficPolling();
}

// Asks whether the CURRENT session (if any) can use this panel at all --
// GET /api/account is the same session-shaped read the account page
// itself uses, never the admin token. A missing/expired session, or a
// real signed-in account that simply holds no role, both land on the
// same "go sign in" message: telling the two apart would only help
// someone probing for which accounts exist, the same reasoning
// app/admin_api.py's _role_guard() gives its own 401 for both cases.
//
// The one case that DOES get its own message: a role held, but no
// active two-factor authentication. _role_guard() now requires TOTP to
// USE admin/operator, not just to hold it (see that function's own
// docstring for the full reasoning on why this is enforced at every
// route below, and why the account-side 403 it returns is safe to
// surface here too) -- every /api/admin/* call this page makes from
// here on would otherwise 403 one at a time with no explanation, which
// reads as "this panel is broken" rather than "turn on two-factor".
// GET /api/account already carries `totp.enabled` (the same field the
// account page's own TOTP panel reads), so this is known before a
// single admin route is ever called.
//
// ---- settings.admin_require_auth: the one case this whole gate is
// skipped -------------------------------------------------------------
//
// GET /config carries that flag (app/api.py) -- checked FIRST, before
// GET /api/account. When it is false, app/admin_api.py's _role_guard()
// itself already lets every /api/admin/* call through with no session,
// role, or TOTP at all (see that flag's own comment in app/config.py),
// so gating the PANEL on a session here would be a UI lie: someone with
// no account whatsoever can already reach every route this page calls.
// The panel is shown directly -- no sign-in check -- with a persistent
// banner (showAuthOpenBanner() below) so nobody mistakes an open admin
// surface for a signed-in one. This never applies to a real deployment:
// the flag defaults true and must stay true anywhere reachable from the
// internet.
async function checkAccess() {
  let authRequired = true;
  try {
    const cfgRes = await fetch('/config');
    if (cfgRes.ok) {
      const cfg = await cfgRes.json();
      authRequired = cfg.admin_require_auth !== false;
    }
    // A failed/unreachable /config falls through with authRequired still
    // true -- the normal, session-gated path below -- rather than ever
    // guessing the surface is open because a request happened to fail.
  } catch (e) {
    // Same reasoning as above: leave authRequired true and let the
    // ordinary GET /api/account attempt below report the real problem.
  }

  if (!authRequired) {
    myRole = 'operator'; // matches the synthetic principal _role_guard() hands the backend, see its own comment
    showAuthOpenBanner();
    await showApp();
    return;
  }
  hideAuthOpenBanner();

  let res;
  try {
    res = await fetch('/api/account');
  } catch (e) {
    showNoAccess('Could not reach the server. Check your connection and try again.');
    return;
  }
  if (!res.ok) {
    showNoAccess('Sign in on the account page, then come back here.');
    return;
  }
  const data = await res.json();
  if (data.role !== 'admin' && data.role !== 'operator') {
    showNoAccess('This account does not hold the admin or operator role.');
    return;
  }
  if (!data.totp || !data.totp.enabled) {
    showNoAccess('This account holds a role, but needs two-factor authentication enabled before it can be used here. Enable it on the account page.');
    return;
  }
  myRole = data.role;
  await showApp();
}

// Persistent banner, shown for the entire visit whenever
// settings.admin_require_auth is false (see checkAccess() above) --
// never auto-hidden by anything except a re-run of checkAccess() that
// finds auth required again (a flag flip, or simply /config answering
// correctly on a retry after a transient failure).
function showAuthOpenBanner() {
  const b = document.getElementById('auth-open-banner');
  if (b) b.hidden = false;
}
function hideAuthOpenBanner() {
  const b = document.getElementById('auth-open-banner');
  if (b) b.hidden = true;
}

document.getElementById('refresh-btn').addEventListener('click', function () {
  setStatus('Refreshing...', false);
  // Goes through checkAccess(), not straight to refreshAll() -- this
  // doubles as the recovery path for the one gap api()'s panelLoaded
  // gate (above) leaves open: the admin surface being disabled
  // entirely mid-visit. checkAccess() re-asks GET /api/account, which
  // reflects a pulled role immediately, so a manual refresh always
  // lands on the correct screen (the real access-revoked message, or
  // the panel with fresh data) instead of ever leaving stale "not
  // found" text on screen from a guess this file cannot make on its
  // own. When access still holds, checkAccess() runs the exact same
  // showApp() -> refreshAll() path this used to call directly.
  checkAccess().then(() => {
    if (myRole) setStatus('Up to date', false);
  });
});
document.querySelectorAll('.adm-nav-item').forEach((b) => {
  b.addEventListener('click', () => show(b.dataset.section));
});
document.getElementById('player-search').addEventListener('input', renderPlayers);
document.getElementById('account-search').addEventListener('input', renderAccounts);
document.getElementById('account-orphans-only').addEventListener('change', renderAccounts);
document.getElementById('account-roles-only').addEventListener('change', renderAccounts);
document.getElementById('ci-award').addEventListener('click', function () { awardCheckin(this); });
document.getElementById('nc-save').addEventListener('click', function () { saveConfig(this); });
document.getElementById('pt-save').addEventListener('click', function () { savePaint(this); });
document.getElementById('pt-clear-cursor').addEventListener('click', function () { clearPaintCursor(this); });
document.getElementById('tr-preview').addEventListener('click', function () { previewTileRelease(this); });
document.getElementById('tr-save').addEventListener('click', function () { saveTileRelease(this); });
document.getElementById('tr-dry-run').addEventListener('change', function () {
  document.getElementById('tr-dry-run-hint').hidden = this.checked;
});
document.getElementById('nf-kind').addEventListener('change', updateConnectionFormKind);
document.getElementById('nf-scores-net').addEventListener('change', updateConnectionFormKind);
document.getElementById('nf-load-channels').addEventListener('click', function () { loadConnectionChannelsAdd(this); });
document.getElementById('nf-channel-select').addEventListener('change', function () {
  document.getElementById('nf-channel').value = this.value;
});
document.getElementById('nf-save').addEventListener('click', function () { saveConnection(this); });
document.getElementById('nf-cancel').addEventListener('click', function () {
  resetConnectionForm();
  document.getElementById('nf-result').replaceChildren();
});
document.getElementById('cf-save').addEventListener('click', function () { saveCommunity(this); });
document.getElementById('cf-cancel').addEventListener('click', function () {
  resetCommunityForm();
  document.getElementById('cf-result').replaceChildren();
});
document.getElementById('mo-freeze').addEventListener('click', function () { freezeMonth(this); });
document.getElementById('pl-preview').addEventListener('click', function () { previewPlaces(this); });
document.getElementById('apikey-create').addEventListener('click', function () { createApiClient(this); });
document.getElementById('nt-save').addEventListener('click', function () { saveNotice(this); });
// Resends whatever is currently in the form with active forced off --
// the "make it easy to clear" path: retiring a notice never requires
// first retyping title/body/version just to satisfy the required-field
// check saveNotice() otherwise runs.
document.getElementById('nt-clear').addEventListener('click', function () { saveNotice(this, false); });
document.getElementById('dc-save').addEventListener('click', function () { saveDiscord(this); });
document.getElementById('dc-test').addEventListener('click', function () { sendDiscordTest(this); });
document.getElementById('dc-channel-add').addEventListener('click', function () { addDiscordChannel(this); });
document.getElementById('dc-roles-ensure').addEventListener('click', function () { ensureDiscordRoles(this); });
document.getElementById('dc-roles-reconcile').addEventListener('click', function () { reconcileDiscordRoles(this); });
document.getElementById('dc-slash-register').addEventListener('click', function () { registerDiscordSlashCommands(this); });
document.getElementById('dc-leaderboard-run').addEventListener('click', function () { runDiscordLeaderboard(this); });

checkAccess();
