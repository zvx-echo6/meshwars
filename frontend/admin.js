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
// never the actual security boundary.
//
// Everything below is plain DOM. No templating and no innerHTML with
// data in it: player names and labels are operator-supplied and
// third-party-supplied respectively, and textContent cannot be talked
// into running anything.
// =====================================================================

let myRole = null;         // null | 'admin' | 'operator' -- from GET /api/account
let allPlayers = [];
let expanded = new Set();   // player ids left open across a refresh

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
    if (resp.status === 401 || resp.status === 404) {
      // The session expired, was revoked, or this account's role was
      // pulled out from under it mid-visit (an operator can revoke
      // their own role, or another operator's) -- reload straight into
      // the access screen rather than leaving stale panels on screen
      // that will now 401 on every action.
      showNoAccess();
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
        row.appendChild(el('strong', { text: a.player }));
        row.appendChild(el('span', { text: a.detail }));
        row.appendChild(btn('Open', 'adm-btn-quiet', () => {
          expanded.add(a.player_id);
          renderPlayers();
          const node = document.getElementById('player-' + a.player_id);
          if (node) node.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }));
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

async function loadOverview() {
  try {
    const d = await api('/api/admin/overview');
    renderHealth(d.health, d.boards);
    renderAttention(d.attention);
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
    row.appendChild(el('strong', { text: b.board === 'mc' ? 'MeshCore' : 'Meshtastic' }));
    row.appendChild(el('span', { text: 'season ' + b.season_id }));
    row.appendChild(el('span', { text: b.squares + ' squares held' }));
    row.appendChild(el('span', { text: 'ends ' + fmtTs(b.ends_at) }));

    const days = el('input', { type: 'number', value: '30' });
    days.style.width = '5rem';
    row.appendChild(el('span', { text: 'extend by' }));
    row.appendChild(days);
    row.appendChild(el('span', { text: 'days' }));
    row.appendChild(btn('Apply', 'adm-btn-quiet', async (bt) => {
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
    host.appendChild(row);
  });
}

// ---- players ----------------------------------------------------------

function renderRadio(p, r) {
  const row = el('div', { className: 'adm-row' });
  row.appendChild(el('span', { className: 'adm-mono', text: r.protocol + ':' + r.node_ref }));
  row.appendChild(el('span', { text: 'bound ' + fmtTs(r.bound_at) }));
  row.appendChild(btn('Remove', 'adm-btn-quiet', async (b) => {
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
  return row;
}

function renderKey(k) {
  const row = el('div', { className: 'adm-row' });
  row.appendChild(el('span', { className: 'adm-mono', text: k.key_hash_prefix }));
  row.appendChild(el('span', { text: 'issued ' + fmtTs(k.issued_at) }));
  row.appendChild(el('span', { text: 'last used ' + fmtTs(k.last_seen_at) }));
  row.appendChild(el('span', {
    className: 'adm-badge ' + (k.revoked ? 'adm-badge-bad' : 'adm-badge-ok'),
    text: k.revoked ? 'revoked' : 'active',
  }));
  if (!k.revoked) {
    row.appendChild(btn('Revoke', 'adm-btn-quiet', async (b) => {
      b.disabled = true;
      try {
        await post('/api/admin/revoke', { key_hash_prefix: k.key_hash_prefix });
        setStatus('Revoked ' + k.key_hash_prefix, false);
        await refreshAll();
      } catch (e) { setStatus('Failed: ' + e.message, true); b.disabled = false; }
    }));
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
    const typed = window.prompt('Deleting removes them and everything they earned.\n\nType ' + p.display_name + ' to confirm.');
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
    row.appendChild(el('span', {
      className: 'adm-player-meta',
      text: radios + (radios === 1 ? ' radio' : ' radios') + ' · joined ' + fmtTs(p.created_at),
    }));
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

// ---- check-ins --------------------------------------------------------

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

// ---- nets ---------------------------------------------------------------
//
// Two independent pieces on one screen: the config singleton (points,
// streak bonus, the poller's own timing knobs -- applies to every net
// at once) and the nets list itself (each net's own connector, window,
// and channel-or-hashtag). GET /api/admin/checkin/nets hands back both
// in one call, so one load feeds both halves of the section.

let allNets = [];
let editingNetId = null;   // null while the form is adding, a net id while editing
const NET_WEEKDAY_ABBR = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

// The four connector kinds an operator can pick (see app/checkin.py's
// KIND_CORESCOPE/KIND_BEACON/KIND_MESHVIEW/KIND_MQTT). `protocol`
// ('mc'/'mt') is derived from this on the backend and is never sent by
// this form -- see _validate_net_fields in app/admin_ops.py. Labels
// match the select options in admin.html exactly; the badge reuses the
// same short label so a row and the form agree on what to call a kind.
const NET_KIND_LABELS = {
  corescope: 'MC: CoreScope',
  beacon: 'MC: Beacon',
  meshview: 'MT: Meshview',
  mqtt: 'MT: MQTT',
};
// corescope and beacon are both channel-scoped connectors (a net picks
// one channel on the connector); meshview and mqtt are both
// hashtag-scoped (found by their hashtag on any channel) -- see
// app/checkin.py's module docstring.
function netKindHasChannel(kind) { return kind !== 'meshview' && kind !== 'mqtt'; }
function netKindIsMqtt(kind) { return kind === 'mqtt'; }

// mqtt's connector is a broker address, not an http(s) URL like the
// other three -- an https example there reads as a typo instruction
// rather than guidance. Keyed by kind so updateNetFormKind can swap
// the connector field's placeholder to match whatever's selected.
const NET_CONNECTOR_URL_EXAMPLES = {
  corescope: 'https://live.mwmesh.com',
  beacon: 'https://map.meshcore.coloradomesh.org',
  meshview: 'https://meshview.freq51.net',
  mqtt: 'mqtt://broker.example.org:1883',
};

function pad2(n) { return String(n).padStart(2, '0'); }

// end_hour is inclusive through :59:59 (see app/db.py's checkin_net
// comment), so displaying it as HH:59 rather than HH:00 is what
// actually matches the window a message gets judged against.
function netWindowText(n) {
  return NET_WEEKDAY_ABBR[n.weekday] + ' ' + pad2(n.start_hour) + ':00-' +
    pad2(n.end_hour) + ':59 ' + n.timezone;
}

function netHealthText(n) {
  if (n.last_poll_error) return 'poll error: ' + n.last_poll_error;
  return n.last_poll_at ? ('last polled ' + ago(n.last_poll_at)) : 'never polled yet';
}

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

function renderNetRow(n) {
  const wrap = el('div', { className: 'adm-net' });
  const row = el('div', { className: 'adm-net-row' });
  // Descriptive text lives in its own group so it can wrap on its own
  // line lengths without dragging the action buttons around -- see
  // .adm-net-row / .adm-net-info / .adm-net-actions in admin.css.
  const info = el('div', { className: 'adm-net-info' });
  info.appendChild(el('strong', { text: n.label }));
  // Shows the KIND, not the protocol -- corescope and beacon are both
  // 'mc' and otherwise indistinguishable in this row, so a protocol
  // badge would leave an operator unable to tell them apart.
  info.appendChild(el('span', { className: 'adm-badge', text: NET_KIND_LABELS[n.kind] || n.kind }));
  info.appendChild(el('span', { className: 'adm-mono', text: n.connector_url }));
  info.appendChild(el('span', { text: netKindHasChannel(n.kind) ? n.channel : n.hashtag }));
  info.appendChild(el('span', { text: netWindowText(n) }));
  info.appendChild(el('span', {
    className: 'adm-badge ' + (n.enabled ? 'adm-badge-ok' : 'adm-badge-bad'),
    text: n.enabled ? 'enabled' : 'disabled',
  }));
  row.appendChild(info);
  // Edit and Delete stay together as one unit pinned to the row's end,
  // so they land in the same place regardless of how long the info
  // group above happens to be.
  const actions = el('div', { className: 'adm-net-actions' });
  actions.appendChild(btn('Edit', 'adm-btn-quiet', () => startEditNet(n)));
  actions.appendChild(btn('Delete', 'adm-btn-danger', async (b) => {
    const typed = window.prompt('Deleting removes this net.\n\nType ' + n.label + ' to confirm.');
    if (!typed) return;
    b.disabled = true;
    try {
      await post('/api/admin/checkin/nets/delete', { id: n.id, label: typed });
      setStatus('Deleted net ' + n.label, false);
      if (editingNetId === n.id) resetNetForm();
      await loadNets();
    } catch (e) { setStatus('Failed: ' + e.message, true); b.disabled = false; }
  }));
  row.appendChild(actions);
  wrap.appendChild(row);

  // Poll status and unresolved-sender count share the same health line
  // -- both answer "is this net actually working," just for two
  // different silent failures (the connector being unreachable, versus
  // a message it fetched fine but could never credit to anyone -- see
  // app/checkin.py's module docstring). The count is its own span so
  // only it takes the warn tone; a poll error already colors the whole
  // line bad and should not be diluted by also drawing the eye to a
  // separate, less urgent warn-colored number.
  const healthP = el('p', {
    className: 'adm-net-health' + (n.last_poll_error ? ' adm-status-bad' : ''),
  });
  healthP.appendChild(el('span', { text: netHealthText(n) }));
  if (n.unresolved_count > 0) {
    healthP.appendChild(el('span', {
      className: 'adm-status-warn',
      text: '  ·  ' + n.unresolved_count +
        (n.unresolved_count === 1 ? ' unresolved sender' : ' unresolved senders') +
        ' (' + n.unresolved_net_date + ')',
    }));
  }
  wrap.appendChild(healthP);

  // The names themselves, only when there are any -- compact, one line,
  // built with textContent (never innerHTML) since a sender name is
  // whatever a MeshCore node owner typed into their own device, not
  // anything this app validated. Reuses .adm-net-health's own muted
  // tone rather than a new class; this is a detail line under an
  // already-established one, not a fresh kind of thing on the page.
  if (n.unresolved_senders && n.unresolved_senders.length) {
    wrap.appendChild(el('p', {
      className: 'adm-net-health',
      text: n.unresolved_senders
        .map((s) => s.sender_name + ' (' + s.message_count + ')')
        .join(', '),
    }));
  }
  return wrap;
}

function renderNets() {
  const host = document.getElementById('nets');
  const count = document.getElementById('nets-count');
  host.replaceChildren();
  count.textContent = allNets.length
    ? (allNets.length + (allNets.length === 1 ? ' net' : ' nets')) : '';
  if (!allNets.length) {
    host.appendChild(el('p', { className: 'adm-hint', text: 'No nets configured yet -- add one below.' }));
    return;
  }
  allNets.forEach((n) => host.appendChild(renderNetRow(n)));
}

async function loadNets() {
  try {
    const d = await api('/api/admin/checkin/nets');
    allNets = d.nets || [];
    renderNets();
    if (d.config) renderConfigForm(d.config);
  } catch (e) {
    setStatus('Nets load failed: ' + e.message, true);
  }
}

// ---- paint source: meshview vs FreqMapper ------------------------------
//
// One combined form (source + connector + scoring), one Save button --
// same "whole singleton, one POST" shape the Nets settings form above
// uses for checkin_config. The paint-source SELECT is the one field
// that gets its own confirmation before that POST goes out (see
// savePaint below): switching it changes where live Meshtastic
// territory comes from, the same kind of consequence the delete
// buttons elsewhere in this file already guard with a typed prompt.

// The mt_paint_source the form was last loaded/saved WITH -- compared
// against the select's current value at save time so the confirmation
// only fires on an actual switch, not on saving connector/scoring
// changes while leaving the source alone.
let loadedPaintSource = null;

function renderPaintForm(cfg) {
  document.getElementById('pt-source').value = cfg.mt_paint_source;
  document.getElementById('pt-enabled').checked = !!cfg.enabled;
  document.getElementById('pt-base-url').value = cfg.base_url || '';
  // Never pre-filled with the real key -- GET /api/admin/paint never
  // returns it (see app/admin_ops.py's _scrub_freqmapper_secrets); the
  // hint span is the only signal of whether one is set.
  document.getElementById('pt-api-key').value = '';
  document.getElementById('pt-clear-api-key').checked = false;
  document.getElementById('pt-api-key-hint').textContent = cfg.has_api_key ? 'currently set' : 'not set';
  document.getElementById('pt-poll-interval').value = cfg.poll_interval_seconds;
  document.getElementById('pt-page-limit').value = cfg.page_limit;
  document.getElementById('pt-paint-from').value = cfg.paint_from || '';
  document.getElementById('pt-points-per-event').value = cfg.points_per_event;
  document.getElementById('pt-unique-painter-bonus').value = cfg.unique_painter_bonus;
  loadedPaintSource = cfg.mt_paint_source;
}

function renderPaintStatus(cfg, cursor, verificationCount) {
  const host = document.getElementById('pt-status');
  host.replaceChildren();

  const pollP = el('p', {
    className: 'adm-net-health' + (cfg.last_poll_error ? ' adm-status-bad' : ''),
  });
  pollP.appendChild(el('span', {
    text: cfg.last_poll_error ? ('poll error: ' + cfg.last_poll_error)
      : (cfg.last_poll_at ? ('last polled ' + ago(cfg.last_poll_at)) : 'never polled yet'),
  }));
  host.appendChild(pollP);

  const countP = el('p', { className: 'adm-net-health' });
  countP.appendChild(el('span', {
    text: verificationCount + (verificationCount === 1 ? ' event consumed' : ' events consumed'),
  }));
  host.appendChild(countP);

  const cursorP = el('p', { className: 'adm-net-health' });
  cursorP.appendChild(el('span', { text: 'cursor:' }));
  host.appendChild(cursorP);

  // The cursor is an opaque base64 blob with no whitespace of its own,
  // so unlike the other .adm-mono values in this panel (a hash prefix,
  // a connector URL) it can't just sit inline -- there is nowhere for
  // it to wrap, and left alone it pushes the line out to whatever width
  // it needs, ignoring the section around it. Boxing it as its own
  // block with overflow-x scoped to that block keeps the value intact
  // and copyable (drag-select still grabs the whole string) without
  // ever letting it set the panel's width.
  host.appendChild(el('div', {
    className: 'adm-mono adm-mono-block',
    text: cursor || '(none yet)',
  }));
}

async function loadPaint() {
  try {
    const d = await api('/api/admin/paint');
    renderPaintForm(d.config);
    renderPaintStatus(d.config, d.cursor, d.verification_count);
  } catch (e) {
    setStatus('Paint config load failed: ' + e.message, true);
  }
}

async function savePaint(b) {
  const out = document.getElementById('pt-result');
  out.replaceChildren();

  const source = document.getElementById('pt-source').value;
  if (source !== loadedPaintSource) {
    // Switching which source paints the Meshtastic board -- named
    // confirmation, same "type it to confirm" shape every destructive
    // action in this file already uses, not a bare OK/Cancel a tired
    // operator could click through without reading.
    const label = source === 'freqmapper' ? 'FreqMapper' : 'Meshview';
    const typed = window.prompt(
      'This switches which source paints live Meshtastic territory.\n\n' +
      'Type ' + label + ' to confirm switching to it.'
    );
    if (typed !== label) {
      out.textContent = typed === null ? '' : 'Not confirmed -- no change made.';
      return;
    }
  }

  const payload = {
    mt_paint_source: source,
    enabled: document.getElementById('pt-enabled').checked,
    base_url: document.getElementById('pt-base-url').value.trim(),
    poll_interval_seconds: parseInt(document.getElementById('pt-poll-interval').value, 10),
    page_limit: parseInt(document.getElementById('pt-page-limit').value, 10),
    paint_from: document.getElementById('pt-paint-from').value,
    points_per_event: parseFloat(document.getElementById('pt-points-per-event').value),
    unique_painter_bonus: parseFloat(document.getElementById('pt-unique-painter-bonus').value),
  };
  // Blank means keep the existing key -- see app/admin_ops.py's
  // admin_paint_update, the same convention checkin_net's
  // broker_password/channel_key already use. clear_api_key is the
  // explicit way to actually blank it.
  if (document.getElementById('pt-clear-api-key').checked) {
    payload.clear_api_key = true;
  } else {
    const apiKey = document.getElementById('pt-api-key').value;
    if (apiKey) payload.api_key = apiKey;
  }

  b.disabled = true;
  try {
    const r = await post('/api/admin/paint', payload);
    renderPaintForm(r.config);
    out.textContent = 'Saved.';
    setStatus('Paint config saved', false);
  } catch (e) {
    out.textContent = 'Failed: ' + e.message;
  }
  b.disabled = false;
}

async function clearPaintCursor(b) {
  const out = document.getElementById('pt-clear-result');
  out.replaceChildren();
  const typed = window.prompt(
    'This makes the next FreqMapper poll re-walk its feed from the beginning.\n\n' +
    'Already-seen events are skipped (deduped by their own id), never double-scored.\n\n' +
    'Type CLEAR to confirm.'
  );
  if (typed !== 'CLEAR') {
    out.textContent = typed === null ? '' : 'Not confirmed -- cursor left alone.';
    return;
  }
  b.disabled = true;
  try {
    await post('/api/admin/paint/clear-cursor', {});
    out.textContent = 'Cursor cleared.';
    await loadPaint();
  } catch (e) {
    out.textContent = 'Failed: ' + e.message;
  }
  b.disabled = false;
}

function updateNetFormKind() {
  const kind = document.getElementById('nf-kind').value;
  document.getElementById('nf-channel-row').hidden = !netKindHasChannel(kind);
  document.getElementById('nf-hashtag-row').hidden = netKindHasChannel(kind);
  const isMqtt = netKindIsMqtt(kind);
  document.getElementById('nf-mqtt-row-1').hidden = !isMqtt;
  document.getElementById('nf-mqtt-row-2').hidden = !isMqtt;
  document.getElementById('nf-mqtt-row-3').hidden = !isMqtt;
  const example = NET_CONNECTOR_URL_EXAMPLES[kind] || NET_CONNECTOR_URL_EXAMPLES.corescope;
  document.getElementById('nf-connector').placeholder = 'connector URL, e.g. ' + example;
}

async function loadNetChannels(b) {
  const connector = document.getElementById('nf-connector').value.trim();
  const kind = document.getElementById('nf-kind').value;
  const out = document.getElementById('nf-result');
  out.replaceChildren();
  if (!connector) { out.textContent = 'Enter a connector URL first.'; return; }
  b.disabled = true;
  try {
    const r = await api('/api/admin/checkin/channels?connector=' + encodeURIComponent(connector) +
      '&kind=' + encodeURIComponent(kind));
    const select = document.getElementById('nf-channel-select');
    select.replaceChildren();
    // applicable is false only for a kind with no channel concept at
    // all (meshview) -- see GET /api/admin/checkin/channels. The
    // button that calls this is hidden for that kind, but this stays
    // defensive rather than assuming the caller never changes.
    if (!r.applicable) {
      select.hidden = true;
      out.textContent = 'This connector kind has no channel list -- type the channel name by hand.';
      b.disabled = false;
      return;
    }
    (r.channels || []).forEach((c) => {
      // Tolerant of shape, same reasoning the backend proxy applies to
      // CoreScope's own response: a channel might be a bare string or
      // an object carrying a name under one of a few likely keys.
      const name = typeof c === 'string' ? c : (c.name || c.channel || c.label || '');
      if (!name) return;
      select.appendChild(el('option', { value: name, text: name }));
    });
    if (select.children.length) {
      select.hidden = false;
      select.value = select.children[0].value;
      document.getElementById('nf-channel').value = select.value;
      out.textContent = 'Loaded ' + select.children.length + ' channels.';
    } else {
      select.hidden = true;
      out.textContent = 'Connector returned no channels -- type the channel name by hand.';
    }
  } catch (e) {
    // Never blocks the form -- a slow or unreachable connector still
    // leaves the plain text field usable.
    out.textContent = 'Could not load channels: ' + e.message + ' -- type the channel name by hand.';
  }
  b.disabled = false;
}

function resetNetForm() {
  editingNetId = null;
  document.getElementById('nf-kind').value = 'corescope';
  updateNetFormKind();
  document.getElementById('nf-label').value = '';
  document.getElementById('nf-connector').value = '';
  document.getElementById('nf-channel').value = '';
  document.getElementById('nf-hashtag').value = '';
  document.getElementById('nf-weekday').value = '2';
  document.getElementById('nf-start-hour').value = '17';
  document.getElementById('nf-end-hour').value = '23';
  document.getElementById('nf-timezone').value = 'America/Boise';
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
  const select = document.getElementById('nf-channel-select');
  select.hidden = true;
  select.replaceChildren();
  document.getElementById('net-form-title').textContent = 'Add a net';
  document.getElementById('nf-save').textContent = 'Add net';
  document.getElementById('nf-cancel').hidden = true;
  // Deliberately does not touch nf-result -- saveNet() calls this right
  // after a successful save specifically to clear the form back to a
  // blank "add" state, and clearing the result here would erase the
  // "Net added"/"Net updated" message in the same breath it appears.
}

function startEditNet(n) {
  editingNetId = n.id;
  document.getElementById('nf-kind').value = n.kind;
  updateNetFormKind();
  document.getElementById('nf-label').value = n.label;
  document.getElementById('nf-connector').value = n.connector_url;
  document.getElementById('nf-channel').value = n.channel || '';
  document.getElementById('nf-hashtag').value = n.hashtag || '';
  document.getElementById('nf-weekday').value = String(n.weekday);
  document.getElementById('nf-start-hour').value = String(n.start_hour);
  document.getElementById('nf-end-hour').value = String(n.end_hour);
  document.getElementById('nf-timezone').value = n.timezone;
  document.getElementById('nf-start-date').value = n.start_date || '';
  document.getElementById('nf-enabled').checked = !!n.enabled;
  document.getElementById('nf-topic-root').value = n.topic_root || '';
  document.getElementById('nf-broker-username').value = n.broker_username || '';
  // Secrets are NEVER echoed back (see app/admin_ops.py's
  // _scrub_secrets) -- these inputs start blank every edit, and a blank
  // submission means "keep the existing value," not "clear it"; the
  // has_* booleans GET /api/admin/checkin/nets does return are shown as
  // a hint next to the field instead, and "Clear" is the only way to
  // actually blank one out.
  document.getElementById('nf-broker-password').value = '';
  document.getElementById('nf-channel-key').value = '';
  document.getElementById('nf-clear-broker-password').checked = false;
  document.getElementById('nf-clear-channel-key').checked = false;
  document.getElementById('nf-broker-password-hint').textContent = n.has_broker_password ? 'currently set' : 'not set';
  document.getElementById('nf-channel-key-hint').textContent = n.has_channel_key ? 'currently set (blank = Meshtastic default)' : 'not set -- using Meshtastic default key';
  const select = document.getElementById('nf-channel-select');
  select.hidden = true;
  select.replaceChildren();
  document.getElementById('net-form-title').textContent = 'Edit net';
  document.getElementById('nf-save').textContent = 'Save net';
  document.getElementById('nf-cancel').hidden = false;
  document.getElementById('nf-result').replaceChildren();
  document.getElementById('net-form-title').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function saveNet(b) {
  const out = document.getElementById('nf-result');
  out.replaceChildren();
  const payload = {
    label: document.getElementById('nf-label').value.trim(),
    kind: document.getElementById('nf-kind').value,
    connector_url: document.getElementById('nf-connector').value.trim(),
    channel: document.getElementById('nf-channel').value.trim(),
    hashtag: document.getElementById('nf-hashtag').value.trim(),
    weekday: parseInt(document.getElementById('nf-weekday').value, 10),
    start_hour: parseInt(document.getElementById('nf-start-hour').value, 10),
    end_hour: parseInt(document.getElementById('nf-end-hour').value, 10),
    timezone: document.getElementById('nf-timezone').value.trim(),
    start_date: document.getElementById('nf-start-date').value,
    enabled: document.getElementById('nf-enabled').checked,
    topic_root: document.getElementById('nf-topic-root').value.trim(),
    broker_username: document.getElementById('nf-broker-username').value.trim(),
    // Blank means "keep the existing secret" on the backend (see
    // app/admin_ops.py's _validate_net_fields) -- so these are only
    // ever sent as a real new value, or left blank; clear_* is the
    // explicit override for actually blanking one out.
    broker_password: document.getElementById('nf-broker-password').value,
    channel_key: document.getElementById('nf-channel-key').value.trim(),
    clear_broker_password: document.getElementById('nf-clear-broker-password').checked,
    clear_channel_key: document.getElementById('nf-clear-channel-key').checked,
  };
  b.disabled = true;
  try {
    if (editingNetId === null) {
      await post('/api/admin/checkin/nets/create', payload);
      out.textContent = 'Net added.';
    } else {
      await post('/api/admin/checkin/nets/update', Object.assign({ id: editingNetId }, payload));
      out.textContent = 'Net updated.';
    }
    resetNetForm();
    await loadNets();
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
      row.appendChild(el('span', { className: 'adm-mono', text: c.key_hash_prefix }));
      row.appendChild(el('strong', { text: c.label }));
      row.appendChild(el('span', { text: 'issued ' + fmtTs(c.created_at) }));
      row.appendChild(el('span', { text: 'last used ' + fmtTs(c.last_seen_at) }));
      row.appendChild(el('span', {
        className: 'adm-badge ' + (c.revoked ? 'adm-badge-bad' : 'adm-badge-ok'),
        text: c.revoked ? 'revoked' : 'active',
      }));
      if (!c.revoked) {
        row.appendChild(btn('Revoke', 'adm-btn-quiet', async (b) => {
          if (!window.confirm('Revoke "' + c.label + '"? Anything using it stops within a minute.')) return;
          b.disabled = true;
          try {
            await post('/api/admin/api-clients/revoke', { key_hash_prefix: c.key_hash_prefix });
            await loadApiClients();
          } catch (e) { window.alert('Failed: ' + e.message); b.disabled = false; }
        }));
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

// ---- roles --------------------------------------------------------------
//
// Operator-only, both server-side (POST /api/admin/roles/* require the
// operator role -- app/admin_api.py's _role_guard(need="operator")) and
// here: the nav item and section are only ever shown when myRole ===
// 'operator' (see showApp() below), so an admin account never even sees
// a control that would just 401. Grant only ever mints 'admin' -- there
// is no role picker here, because becoming an operator has exactly one
// door (the claim flow, on the account page, token + two-factor), never
// a grant from this panel -- see POST /api/admin/roles/grant's own
// docstring for why that is deliberate, not a missing feature.

async function loadRoles() {
  if (myRole !== 'operator') return;
  const host = document.getElementById('roles-list');
  host.replaceChildren();
  try {
    const d = await api('/api/admin/roles');
    if (!d.roles.length) {
      host.appendChild(el('p', { className: 'adm-hint', text: 'No accounts hold a role.' }));
      return;
    }
    d.roles.forEach((r) => {
      const row = el('div', { className: 'adm-row' });
      row.appendChild(el('span', { className: 'adm-mono', text: 'account ' + r.account_id }));
      row.appendChild(el('span', {
        className: 'adm-badge' + (r.role === 'operator' ? ' adm-badge-ok' : ''),
        text: r.role,
      }));
      row.appendChild(btn('Revoke', 'adm-btn-danger', async (b) => {
        if (!window.confirm('Revoke the ' + r.role + ' role from account ' + r.account_id + '?')) return;
        b.disabled = true;
        try {
          await post('/api/admin/roles/revoke', { account_id: r.account_id });
          setStatus('Revoked ' + r.role + ' from account ' + r.account_id, false);
          await loadRoles();
        } catch (e) { setStatus('Failed: ' + e.message, true); b.disabled = false; }
      }));
      host.appendChild(row);
    });
  } catch (e) {
    host.appendChild(el('p', { className: 'adm-hint', text: 'Could not load: ' + e.message }));
  }
}

async function grantAdmin(b) {
  const input = document.getElementById('rl-account-id');
  const out = document.getElementById('rl-grant-result');
  out.replaceChildren();
  const accountId = parseInt(input.value, 10);
  if (!accountId || accountId < 1) { out.textContent = 'Enter a valid account id.'; return; }
  b.disabled = true;
  try {
    const r = await post('/api/admin/roles/grant', { account_id: accountId });
    out.textContent = r.changed
      ? ('Account ' + accountId + ' now holds the admin role.')
      : ('Account ' + accountId + ' already held the admin role.');
    input.value = '';
    await loadRoles();
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
    loadPlayers(), loadOverview(), loadApiClients(), loadNotice(), loadNets(), loadPaint(),
  ];
  if (myRole === 'operator') loads.push(loadRoles());
  await Promise.all(loads);
  badge('nav-players', allPlayers.length, false);
}

function showNoAccess(message) {
  myRole = null;
  document.getElementById('app').hidden = true;
  document.getElementById('login').hidden = false;
  document.getElementById('login-err').textContent = message || '';
}

async function showApp() {
  document.getElementById('login').hidden = true;
  document.getElementById('app').hidden = false;
  document.getElementById('nav-roles-item').hidden = myRole !== 'operator';
  document.getElementById('topbar-role').textContent = myRole;
  const wanted = location.hash.slice(1);
  show(document.querySelector('.adm-section[data-section="' + wanted + '"]') ? wanted : 'overview');
  await refreshAll();
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
async function checkAccess() {
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

async function signOut() {
  try {
    await fetch('/api/account/logout', { method: 'POST' });
  } catch (e) { /* falling through to the redirect either way */ }
  location.href = '/account';
}

document.getElementById('signout-btn').addEventListener('click', signOut);
document.getElementById('refresh-btn').addEventListener('click', function () {
  setStatus('Refreshing...', false);
  refreshAll().then(() => setStatus('Up to date', false));
});
document.querySelectorAll('.adm-nav-item').forEach((b) => {
  b.addEventListener('click', () => show(b.dataset.section));
});
document.getElementById('player-search').addEventListener('input', renderPlayers);
document.getElementById('ci-award').addEventListener('click', function () { awardCheckin(this); });
document.getElementById('nc-save').addEventListener('click', function () { saveConfig(this); });
document.getElementById('pt-save').addEventListener('click', function () { savePaint(this); });
document.getElementById('pt-clear-cursor').addEventListener('click', function () { clearPaintCursor(this); });
document.getElementById('nf-kind').addEventListener('change', updateNetFormKind);
document.getElementById('nf-load-channels').addEventListener('click', function () { loadNetChannels(this); });
document.getElementById('nf-channel-select').addEventListener('change', function () {
  document.getElementById('nf-channel').value = this.value;
});
document.getElementById('nf-save').addEventListener('click', function () { saveNet(this); });
document.getElementById('nf-cancel').addEventListener('click', function () {
  resetNetForm();
  document.getElementById('nf-result').replaceChildren();
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
document.getElementById('rl-grant').addEventListener('click', function () { grantAdmin(this); });

checkAccess();
