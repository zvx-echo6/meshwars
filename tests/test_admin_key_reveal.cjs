// Run with: node --test tests/test_admin_key_reveal.cjs
// Exercise the actual admin handlers and DOM rebuilds without external services.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.events = {}; this.value = ''; this._text = ''; }
  get childElementCount() { return this.children.length; }
  get textContent() { return this._text + this.children.map(n => n.textContent).join(''); }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  appendChild(node) { node.remove(); this.children.push(node); node.parent = this; return node; }
  replaceChildren(...nodes) { this.children.forEach(n => { n.parent = null; }); this.children = []; this._text = ''; nodes.forEach(n => this.appendChild(n)); }
  remove() { if (this.parent) { this.parent.children = this.parent.children.filter(n => n !== this); this.parent = null; } }
  addEventListener(event, callback) { this.events[event] = callback; }
  focus() { this.focused = true; }
  select() { this.selected = true; }
}
function descendants(root) { return [root, ...root.children.flatMap(descendants)]; }
function setup() {
  const nodes = new Map();
  const document = {
    createElement: tag => new Element(tag),
    createTextNode: text => { const n = new Element('#text'); n.textContent = text; return n; },
    getElementById: id => { if (!nodes.has(id)) nodes.set(id, new Element('div')); return nodes.get(id); },
    querySelectorAll: () => [], addEventListener: () => {},
  };
  const context = vm.createContext({ document, clearInterval: () => {}, window: { prompt: () => 'Player One' }, fetch: () => new Promise(() => {}) });
  const source = process.env.ADMIN_JS_PATH || path.join(__dirname, '../frontend/admin.js');
  vm.runInContext(fs.readFileSync(source, 'utf8'), context);
  vm.runInContext(`
    myRole = 'operator';
    const player = { player_id: 1, display_name: 'Player One', team: 'RED', radios: [], keys: [] };
    const other = { player_id: 2, display_name: 'Player Two', team: 'BLUE', radios: [], keys: [] };
    let serial = 0;
    api = async () => [player, other];
    post = async (url, body) => {
      if (url.endsWith('/reissue')) player.keys.forEach(k => { k.revoked = true; });
      const key = 'test-key-' + (++serial);
      player.keys.push({ key_hash_prefix: 'hash-' + serial, revoked: false });
      return { key, revoked_count: serial - 1 };
    };
    loadAccounts = loadOverview = loadApiClients = loadNotice = loadNets = loadPaint =
      loadDiscord = loadTraffic = loadCheckinAwards = loadCommunities = loadSources = loadTileRelease = async () => {};
    allPlayers = [player, other]; expanded.add(1); expanded.add(2); renderPlayers();
  `, context);
  const run = code => vm.runInContext(code, context);
  const players = document.getElementById('players');
  const click = label => {
    const button = descendants(players).find(n => n.tagName === 'button' && n.textContent === label);
    assert.ok(button, `Button ${label} exists`); return button.events.click();
  };
  const keys = () => descendants(players).filter(n => n.tagName === 'input' && n.readOnly).map(n => n.value);
  return { run, click, keys, players, document };
}

test('extra key survives the immediate refresh, further refreshes, search and collapse', async () => {
  const ui = setup();
  await ui.click('Issue extra key');
  assert.deepEqual(ui.keys(), ['test-key-1']);
  await ui.run('refreshAll()');
  assert.deepEqual(ui.keys(), ['test-key-1']);
  ui.document.getElementById('player-search').value = 'no match'; ui.run('renderPlayers()');
  assert.deepEqual(ui.keys(), []);
  ui.document.getElementById('player-search').value = ''; ui.run('renderPlayers()');
  assert.deepEqual(ui.keys(), ['test-key-1']);
  ui.run('expanded.delete(1); renderPlayers()'); assert.deepEqual(ui.keys(), []);
  ui.run('expanded.add(1); renderPlayers()'); assert.deepEqual(ui.keys(), ['test-key-1']);
  assert.ok(ui.players.textContent.includes('hash-1'));
});

test('multiple extra keys remain available until individually dismissed', async () => {
  const ui = setup(); await ui.click('Issue extra key'); await ui.click('Issue extra key');
  assert.deepEqual(ui.keys(), ['test-key-1', 'test-key-2']);
  await ui.click('Dismiss key'); await ui.run('refreshAll()');
  assert.deepEqual(ui.keys(), ['test-key-2']);
  await ui.click('Dismiss key'); await ui.run('refreshAll()');
  assert.deepEqual(ui.keys(), []);
});

test('revoke and reissue keeps the replacement and removes revoked reveals', async () => {
  const ui = setup(); await ui.click('Issue extra key'); await ui.click('Revoke & reissue');
  assert.deepEqual(ui.keys(), ['test-key-2']);
  assert.ok(ui.players.textContent.includes('1 revoked'));
  await ui.run('refreshAll()'); assert.deepEqual(ui.keys(), ['test-key-2']);
});

test('refresh failure does not discard a successfully issued key', async () => {
  const ui = setup(); ui.run('api = async () => { throw new Error("offline"); }');
  await ui.click('Issue extra key'); assert.deepEqual(ui.keys(), ['test-key-1']);
});

test('loss of access clears keys, including displays detached by filtering', async () => {
  const ui = setup(); await ui.click('Issue extra key');
  ui.document.getElementById('player-search').value = 'no match'; ui.run('renderPlayers()');
  ui.run('showNoAccess("Session expired")');
  ui.document.getElementById('player-search').value = ''; ui.run('renderPlayers()');
  assert.deepEqual(ui.keys(), []);
});


test('each player keeps their own issued key', async () => {
  const ui = setup(); await ui.click('Issue extra key');
  ui.run('expanded.delete(1); renderPlayers()'); await ui.click('Issue extra key');
  ui.run('expanded.add(1); renderPlayers()');
  assert.deepEqual(ui.keys(), ['test-key-1', 'test-key-2']);
});

test('deleting a player removes their displayed key', async () => {
  const ui = setup(); await ui.click('Issue extra key'); await ui.click('Delete player');
  ui.run('expanded.add(1); renderPlayers()');
  assert.deepEqual(ui.keys(), []);
});
