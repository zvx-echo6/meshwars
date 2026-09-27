/*
 * MeshWars: /about live-numbers band, plus the contents-rail scroll-spy
 * this page carries the same way /docs, /rules and /account each keep
 * their own copy (see rules.js's own header comment for why that one
 * piece is copied rather than shared). This page is read top to
 * bottom, not searched, so unlike those three it does not load
 * frontend/page-search.js or carry a search box.
 *
 * The live-numbers band is an enhancement only -- the page
 * (frontend/about.html) is fully readable and correct with this script
 * never loaded at all. Every value shown here already has an em dash
 * in the markup, so a fetch failure of any kind just leaves the dash
 * in place; nothing here can produce an error state or a broken
 * layout. Talks to GET /api/mc/scores and GET /api/mc/players -- both
 * existing, unauthenticated, read-only endpoints (see app/mc_api.py).
 * Fetches once on load, no polling.
 *
 * SECURITY: every value this script writes comes from the server and
 * is untrusted. All of it is written via .textContent -- never
 * innerHTML/insertAdjacentHTML -- so nothing served back to us can run
 * as markup.
 */

// ---- contents-rail scroll-spy (copied from rules.js/docs.js) ---------
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

(function () {
  const DASH = '–';

  const els = {
    squares: document.getElementById('stat-squares'),
    players: document.getElementById('stat-players'),
    ends: document.getElementById('stat-ends'),
  };

  function setDash(...keys) {
    for (const key of keys) {
      const el = els[key];
      if (el) el.textContent = DASH;
    }
  }

  function formatEndsAt(ts) {
    if (typeof ts !== 'number' || !Number.isFinite(ts)) return null;
    const d = new Date(ts * 1000);
    if (Number.isNaN(d.getTime())) return null;
    return d.toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    });
  }

  async function loadScores() {
    try {
      const res = await fetch('/api/mc/scores');
      if (!res.ok) return setDash('squares', 'ends');
      const data = await res.json();

      const teams = Array.isArray(data && data.teams) ? data.teams : [];
      const total = teams.reduce((sum, t) => {
        const n = Number(t && t.tiles);
        return sum + (Number.isFinite(n) ? n : 0);
      }, 0);
      if (els.squares) els.squares.textContent = String(total);

      const ends = formatEndsAt(data && data.ends_at);
      if (els.ends) els.ends.textContent = ends !== null ? ends : DASH;
    } catch {
      setDash('squares', 'ends');
    }
  }

  async function loadPlayers() {
    try {
      const res = await fetch('/api/mc/players');
      if (!res.ok) return setDash('players');
      const data = await res.json();
      if (els.players) {
        els.players.textContent = Array.isArray(data) ? String(data.length) : DASH;
      }
    } catch {
      setDash('players');
    }
  }

  loadScores();
  loadPlayers();
})();

// ---- "Where it's played" community list ------------------------------
//
// GET /api/about/communities (public, unauthenticated -- see
// app/admin_api.py's own docstring on that route) replaces what used
// to be seven hand-typed <li> entries under #where with a live-fetched
// list built the same way: one <li> per community, a link (or a plain
// name when the community has no url, matching Central Oregon's
// existing no-link style) followed by exactly ONE .landing-link-desc
// span combining the protocol prefix, the community's own blurb, and
// the net sentence -- matching production's own single-desc-line shape
// (verified against a live curl of this page's markup; see each
// template string below).
//
// Same "enhancement only" contract the stats band above documents --
// a fetch failure or an empty response just leaves #community-list
// empty; this never throws, and the rest of the page is unaffected.
//
// SECURITY: every field on a community (name, region, blurb, url,
// contact_url, and each net's channel/hashtag/window_text) comes from
// the server and is untrusted -- every element below is built with
// document.createElement + .textContent, never innerHTML/
// insertAdjacentHTML, same as the rest of this file. channel/hashtag
// specifically go in via .textContent on a real <strong> element (never
// interpolated into an HTML string), so the <strong> wrapping the
// public page shows around them is genuine DOM, not string-built markup.
(function () {
  // One prefix per protocol, used at most once per community (Part E's
  // own note: a community carrying both 'mc' and 'mt' doesn't exist in
  // today's data -- if it ever does, only the first protocol found is
  // used, deliberately not engineered further than that).
  const PROTOCOL_PREFIX = { mc: 'MeshCore: ', mt: 'Meshtastic: ' };

  // Builds the single .landing-link-desc span's contents (text nodes and
  // real <strong> elements) for one community, appending them straight
  // onto `desc`. Returns false if there is nothing to show at all
  // (defensive only -- real seed data always has at least a protocol).
  function appendDescContent(desc, community) {
    const protocols = Array.isArray(community.protocols) ? community.protocols : [];
    const protocol = protocols[0];
    const prefix = PROTOCOL_PREFIX[protocol];
    if (!prefix) return false;

    const nets = Array.isArray(community.nets) ? community.nets : [];
    const net = nets.find((n) => n.protocol === protocol);
    const blurb = community.blurb || '';

    let lead = prefix;
    if (blurb) lead += blurb + ' ';

    if (protocol === 'mc') {
      if (net && net.channel) {
        desc.appendChild(document.createTextNode(lead + 'Net check-in runs ' + net.window_text + ', in the '));
        const strong = document.createElement('strong');
        strong.textContent = net.channel;
        desc.appendChild(strong);
        desc.appendChild(document.createTextNode(' channel.'));
      } else {
        desc.appendChild(document.createTextNode(
          lead + "Net check-in isn't set up yet — time and channel to follow once they're decided."));
      }
      return true;
    }

    // protocol === 'mt'
    if (net && net.hashtag) {
      if (net.channel) {
        desc.appendChild(document.createTextNode(lead + 'Net check-in runs ' + net.window_text + ', in the '));
        const channelStrong = document.createElement('strong');
        channelStrong.textContent = net.channel;
        desc.appendChild(channelStrong);
        desc.appendChild(document.createTextNode(' channel — your message must include '));
      } else {
        desc.appendChild(document.createTextNode(lead + 'Net check-in runs ' + net.window_text + ' — your message must include '));
      }
      const hashtagStrong = document.createElement('strong');
      hashtagStrong.textContent = net.hashtag;
      desc.appendChild(hashtagStrong);
      desc.appendChild(document.createTextNode(' to count.'));
    } else {
      desc.appendChild(document.createTextNode(
        lead + "Net check-in isn't set up yet — time and hashtag to follow once they're decided."));
    }
    return true;
  }

  function buildCommunityItem(community) {
    const li = document.createElement('li');

    if (community.url) {
      const a = document.createElement('a');
      a.href = community.url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.textContent = community.name;
      li.appendChild(a);
    } else {
      const span = document.createElement('span');
      span.className = 'landing-link-name';
      span.textContent = community.name;
      li.appendChild(span);
    }

    const desc = document.createElement('span');
    desc.className = 'landing-link-desc';
    if (appendDescContent(desc, community)) li.appendChild(desc);

    return li;
  }

  async function loadCommunities() {
    const host = document.getElementById('community-list');
    if (!host) return;
    try {
      const res = await fetch('/api/about/communities');
      if (!res.ok) return;
      const communities = await res.json();
      if (!Array.isArray(communities)) return;
      communities.forEach((community) => host.appendChild(buildCommunityItem(community)));
    } catch {
      // Enhancement only -- leave the list empty rather than showing a
      // broken page, same contract as loadScores()/loadPlayers() above.
    }
  }

  loadCommunities();
})();
