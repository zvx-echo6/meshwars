'use strict';
// =====================================================================
// The operator panel. Split out of admin.html along with admin.css.
//
// The admin token lives in one variable for one page load. Never
// localStorage, never sessionStorage, never a cookie, never logged --
// this token can delete every player, and a browser that remembers it
// is a browser that hands it to whoever opens the laptop next.
//
// Everything below is plain DOM. No templating and no innerHTML with
// data in it: player names and labels are operator-supplied and
// third-party-supplied respectively, and textContent cannot be talked
// into running anything.
// =====================================================================

let adminToken = '';
let allPlayers = [];
let expanded = new Set();   // player ids left open across a refresh

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
  const opts = options || {};
  const headers = Object.assign({}, opts.headers || {}, { 'X-Admin-Token': adminToken });
  const resp = await fetch(path, Object.assign({}, opts, { headers: headers }));
  let body = null;
  try { body = await resp.json(); } catch (e) { /* no body */ }
  if (!resp.ok) throw new Error((body && body.error) || ('HTTP ' + resp.status));
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
        if (a.kind === 'checkin_unreachable') {
          const name = el('input', { placeholder: 'name their check-ins appear under' });
          row.appendChild(name);
          row.appendChild(btn('Register', '', async (b) => {
            b.disabled = true;
            try {
              await post('/api/admin/checkin/binding',
                { player_id: a.player_id, sender_name: name.value });
              setStatus('Registered check-in name for ' + a.player, false);
              await loadOverview();
            } catch (e) { setStatus('Failed: ' + e.message, true); b.disabled = false; }
          }));
        }
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

// ---- session and navigation -------------------------------------------
//
// Signing in is a real step, not a text box above the content: the token
// is checked against the server before anything is shown, so a wrong one
// says so here instead of leaving six panels silently empty.

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
  await Promise.all([loadPlayers(), loadOverview(), loadApiClients()]);
  badge('nav-players', allPlayers.length, false);
}

async function signIn(e) {
  if (e) e.preventDefault();
  const input = document.getElementById('token-input');
  const err = document.getElementById('login-err');
  const btnEl = document.getElementById('login-btn');
  err.textContent = '';
  if (!input.value) { err.textContent = 'Enter the admin token.'; return; }

  adminToken = input.value;
  btnEl.disabled = true;
  btnEl.textContent = 'Checking...';
  try {
    // Any authenticated route would do; players is the cheapest that
    // proves the token rather than merely proving the server is up.
    await api('/api/admin/players');
  } catch (ex) {
    adminToken = '';
    err.textContent = ex.message === 'unauthorized'
      ? 'That token was not accepted.'
      : ('Could not sign in: ' + ex.message);
    btnEl.disabled = false;
    btnEl.textContent = 'Sign in';
    return;
  }
  input.value = '';
  btnEl.disabled = false;
  btnEl.textContent = 'Sign in';

  document.getElementById('login').hidden = true;
  document.getElementById('app').hidden = false;
  const wanted = location.hash.slice(1);
  show(document.querySelector('.adm-section[data-section="' + wanted + '"]') ? wanted : 'overview');
  await refreshAll();
}

function signOut() {
  adminToken = '';
  allPlayers = [];
  expanded.clear();
  document.getElementById('app').hidden = true;
  document.getElementById('login').hidden = false;
  document.getElementById('token-input').focus();
}

document.getElementById('login-form').addEventListener('submit', signIn);
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
document.getElementById('mo-freeze').addEventListener('click', function () { freezeMonth(this); });
document.getElementById('apikey-create').addEventListener('click', function () { createApiClient(this); });
