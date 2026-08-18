---
title: Phase 0 Discovery — MeshCore Support
status: draft
---

# Phase 0 Discovery: bringing MeshCore into MeshWars

This document is a plain-English map of how MeshWars works today, written before any MeshCore work starts. MeshWars is currently built entirely around Meshtastic (a specific mesh-radio protocol). The goal of later phases is to let it also understand MeshCore (a different mesh-radio protocol) without throwing away what's here. This doc does not propose a design — it just explains what exists, where the Meshtastic-only assumptions are baked in, and what decisions are already locked in for later phases to build against.

Everything below reflects the code as of branch `feature/meshcore`, cut from `main` at commit `749cbaa`.

---

## a) How Meshtastic data gets into MeshWars today (it's a PULL, not a PUSH)

MeshWars never receives radio traffic directly and never gets anything pushed to it. It repeatedly **asks** a separate website called "meshview" — a community Meshtastic packet-logging service — "what's new?" on a timer. If meshview is down or slow, MeshWars just has stale data; nothing is lost or queued on the sending side because nothing is sent to MeshWars, it always goes and fetches.

The component that does this is `MeshviewClient` in `app/meshview_client.py`, driven by `Ingestor` in `app/ingest.py`.

**Startup sequence** (`Ingestor.run_forever`, `app/ingest.py:62`):
1. Make sure a "season" exists (see section c) — `ensure_initial_season()`.
2. Snapshot the current node roster from meshview into the local database (`_refresh_roster`, `app/ingest.py:92`) so there's something to draw on the map immediately.
3. Run a one-time "backfill" (`_backfill`, `app/ingest.py:143`): pull the last 24 hours of position history (configurable, `backfill_hours` in `app/config.py:35`) so the map isn't empty on a fresh restart. This walks backward through meshview's `/api/packets` endpoint, 100 packets at a time, using a packet ID as a paging cursor (`before_id`), for up to 50 pages, stopping once it walks past the 24-hour cutoff or the pagination stops advancing.
4. Then loop forever: check whether the current game season has expired and needs to roll over (see section c), then poll for new packets, then sleep for `poll_interval_seconds` (default 45 seconds, `app/config.py:15`) before doing it again.

**Endpoints called on meshview**, all via `MeshviewClient` in `app/meshview_client.py`:
- `/api/nodes` — the roster snapshot (names, short names, last known position, device role). Used to populate the local `node_seen` table.
- `/api/packets` — the actual position reports, filtered by `portnum=3`, which is Meshtastic's own numeric code for "this is a position packet" (`position_app_portnum` in `app/config.py:32`). The regular poll always asks for the newest 100 of these; the backfill pages backward through them.
- `/api/packets_seen/{packet_id}` — for one specific packet, who actually heard it over the air, with hop count and signal strength. This is what turns a "position was reported" event into "and here's proof someone nearby received it."
- `/api/stats` — a method exists for this (`MeshviewClient.stats()`, `app/meshview_client.py:64`) but nothing in the codebase actually calls it. It appears to be unused/dead code today.

**Deciding whether a packet "counts" (qualifies):** for each new position packet, MeshWars fetches its `packets_seen` rows (who received it) and checks each reception (`_classify_and_process`, `app/ingest.py:289`, filtering logic at `app/ingest.py:321`). Note that MeshWars's actual runtime settings come from `docker-compose.yml`, with `.env` able to override those — so a default written in `app/config.py` isn't always what's actually running, as is the case below:
- The reception's hop count (`hop_start` minus `hop_limit`, i.e. how many times the packet was relayed before this listener heard it) must be at or below `max_hops`. The code default in `app/config.py:25` is 99, but `docker-compose.yml` sets `MAX_HOPS: "${MAX_HOPS:-1}"` and the deployed `.env` doesn't set `MAX_HOPS` at all, so the live system actually runs with `max_hops=1`. In plain English: only receptions that were heard directly, or relayed at most once, count toward the game — this is real, meaningful filtering, not a no-op.
- The reception must not be MQTT-only. Meshview flags a reception as MQTT-relayed (`via_mqtt: true`) when it was learned about only through an internet gateway rather than actually heard over radio. MeshWars rejects those, because the game is about real RF coverage, not "someone posted it to the internet."

If at least one reception for a packet qualifies, the packet counts and gets processed into a tile paint (see section c). If none do, or the packet has no valid position at all, it's discarded — but still recorded.

**Avoiding reprocessing the same packet twice:** every packet ID that's been looked at — whether it counted or not — is written into a `processed_packet` table (`app/ingest.py:576`). Before doing any work on a packet, the code checks this table and skips it if already present. This is necessary because the regular poll doesn't use a true "give me only what changed since X" query against meshview — it just re-asks for the newest 100 packets every 45 seconds and relies on local dedup to ignore ones it's already handled. There is also a `cursor` table that stores a value called `last_position_import_us` (a timestamp bookmark), but reading the code closely, this value is written after each poll/backfill but is never actually read back into a request parameter for the next poll — the regular poll always requests "newest 100" regardless of what the cursor says. In practice the cursor looks like a health/debugging marker more than something the polling logic depends on.

---

## b) Where the map data comes from, end to end

The frontend is a static single page (`frontend/index.html`) that loads Leaflet (the mapping library) plus two of MeshWars's own JavaScript files, `frontend/shared.js` (small helper functions, including a bundled copy of a geohash encode/decode library) and `frontend/code.js` (everything else — map setup, drawing, UI controls, popups).

**Startup, in the browser:**
1. `initMap()` (`frontend/code.js:281`) runs on page load. It fetches `/config` first, which returns where to center the map and how zoomed in to start (computed server-side as the median position of known nodes — `_derive_map_center`, `app/api.py:92`), plus the current season and scoreboard numbers.
2. It then builds the Leaflet map, adds the dark base tile layer, adds a "scoreboard" control panel to the corner, and calls `refreshCoverage()` (`frontend/code.js:977`), which fetches `/get-nodes` and draws everything.
3. After that, it sets up recurring timers: refresh the scoreboard numbers every 30 seconds, refresh the winner banner every 60 seconds, and re-fetch and redraw the whole map every 30 seconds.

**The main data route is `/get-nodes`** (`app/api.py:121`). It returns three lists in one response:
- `coverage` — one entry per map tile (a geohash cell) that has ever received a qualifying position this season, including which team owns it, its packet counts, its red/blue fortress scores, and when it was last captured.
- `samples` — individual position pings, aggregated for display.
- `repeaters` — every node with a known position this season, used to draw the small node/repeater markers.

The frontend takes that response and builds two in-memory lookup structures (`buildIndexes`, `frontend/code.js:837`): a map from geohash string to its tile data, and a map from node ID to its repeater entries. Each tile's geohash is decoded back into a lat/lon bounding box using the bundled geohash library (`geo.decode_bbox`, from `frontend/shared.js`), and `renderNodes()` (`frontend/code.js:806`) draws a colored rectangle for each tile onto the map (`coverageMarker`, `frontend/code.js:565`). When "Territory Mode" is on (it defaults to on), the rectangle's fill color is simply the owning team's color (`TEAM_COLORS`, red/blue/green) rather than any of the original data-quality color palettes the frontend also supports.

**Tile detail popups** are lazy-loaded: clicking a tile shows a lightweight placeholder immediately, then fetches `/tile/{geohash}` (`app/api.py:461`) in the background for the rich detail — current red/blue score bar, whether the tile is inside its post-capture defense window, the top contributing nodes, and recent capture history — and swaps that into the popup once it arrives (`frontend/code.js:1313`).

**Other routes the frontend calls:**
- `/scores` — just the red/blue/green tile counts and season end time, polled every 30 seconds for the scoreboard.
- `/history` — list of past closed seasons, for the "Past Seasons" modal.
- `/teams` and `/team/{node_ref}` — full roster and single-node lookup, for the "Roster" modal and the search box.
- `/get-samples` — individual (unaggregated) position pings, at a finer geohash precision than tiles.
- `/live-tracks` and `/live-tracks/stream` — see below.

**Streaming:** `/live-tracks/stream` (`app/api.py:284`) is a Server-Sent-Events (SSE) endpoint — a way for the server to keep a connection open and push small messages to the browser as they happen, instead of the browser having to keep re-asking. The frontend has a fully working client for it (`connectSSE`, `frontend/code.js:999`, with automatic reconnect and backoff) meant to show live "wardriving" trails as a node moves around in real time. However, the server side of this is currently a stub: it only sends a heartbeat ping every 30 seconds and never actually pushes any track points (the code comment literally says "v1 doesn't push points," `app/api.py:290`). So the live-track feature is wired up front-to-back but not actually populated with real data yet.

**A note on unreachable frontend UI:** `frontend/code.js` defines a whole settings panel (`mapControl`, `frontend/code.js:64`) with dropdowns for query mode, color palette, a feeder search box, and checkboxes for "Show Live Wardriving" and "Territory Mode" — but that control is never actually attached to the map (there's no `mapControl.addTo(map)` call anywhere; only the separate `scoreboardControl` is added). So today, a visitor only sees the scoreboard/history/roster panel; the older settings panel and its "show individual samples" checkbox are dead code that never renders. Worth knowing before assuming any given control in the code is actually reachable by a user right now.

---

## c) Where scoring lives, and what the actual game rules are

The scoring system is nicknamed "fortress scoring" in the code comments. It lives mostly in `app/scoring.py` (the math and database primitives) and `app/ingest.py` (the decision logic that runs every time a qualifying position packet comes in). `app/seasons.py` handles the season-level wrapper around it (starting, ending, rolling over).

**How a tile gets captured:**
- The map is divided into geohash cells at precision 6 (roughly 1.2 km by 0.6 km per cell — `GEOHASH_TILE_PRECISION = 6`, `app/ingest.py:48`). Every qualifying position packet gets encoded to one of these cells.
- The very first qualifying paint on a brand-new (never-seen) tile captures it immediately for whichever team sent it.
- If the tile is already owned by that same team, painting it again doesn't change ownership — it's just "reinforcement," but the team's score for that tile still goes up.
- If the tile is owned by the *other* team, whether it flips depends on two things: first, whether the tile is inside its 15-minute "defense window" (`defense_window_seconds = 900`, `app/config.py:49`) — a tile cannot be flipped at all for 15 minutes after it was last captured, no matter the score. Second, once that window has passed, the tile flips to the attacking team only if the attacker's current score for that tile is greater than or equal to the defender's current score.
- On a flip: the old defending team's score for that tile resets to zero, the new capture timestamp restarts the 15-minute defense window, and an entry is written to a permanent capture log for history/audit purposes.
- Tiles never go back to "neutral" once a team has captured them. A node that isn't on a team (see below) can still paint a never-yet-captured tile green ("neutral," meaning nobody owns it) and bump its packet counters, but it can't take a tile away from a team.

**Scoring math** (`app/scoring.py`): every team has its own independent, decaying score *per tile*. Each qualifying paint adds a flat 0.5 points (`score_per_packet`) to the painting team's score for that tile. If this is the *first time this particular radio* has painted *this particular tile* for *this particular team* this season, an extra one-time 1.0-point bonus is added (`score_per_unique_node`) — this is the "unique painter" bonus, tracked in a dedicated table (`tile_unique_painter`) so it's only ever awarded once per (tile, team, node) combination. The intent is to reward a team having many different people contribute to holding a tile, not just one radio repeatedly pinging the same spot. All scores decay linearly at a fixed rate — 0.25 points per day (`score_decay_per_day`) — toward a floor of zero, calculated on the fly whenever a score is read rather than by a background job. This means an abandoned tile's defending score quietly erodes, so an attacking team can eventually take it over time even without a sudden burst of activity, once the defense window has expired.

**Team assignment (today):** `app/draft.py` implements a "snake draft." When a season ends, the previous season's per-node packet-count activity is sorted (log-scaled, so a high-traffic repeater doesn't dominate) and nodes are dealt out alternately, RED, BLUE, BLUE, RED, RED, BLUE, BLUE, RED... so both teams end up with a similar total activity level. A brand-new node that shows up mid-season and isn't yet assigned gets balance-assigned on the spot to whichever team currently has fewer members (`assign_new_node`, `app/draft.py:94`, called from `app/ingest.py:384` the moment a new node's first qualifying position comes in). **This entire mechanism — the draft, and this auto-assign-on-first-packet behavior — is what the owner has decided to remove** (see section g, decision 2).

**Season rollover** (`app/seasons.py`): seasons run on a fixed 30-day rolling window (`season_days`, `app/config.py:20`). The ingest loop checks on every cycle whether the active season's `ends_at` has passed (`maybe_close_and_roll`, `app/seasons.py:96`). When it has: the closing season's tiles are tallied by owning team to decide a winner (or a tie), the season row is marked closed with final counts, a brand-new season row is opened starting from zero tiles, the snake draft runs against the just-closed season's activity numbers to seed team assignments for the new season, old fine-grained samples are deleted, and history beyond the most recent 12 closed seasons is pruned (`history_max`, `app/config.py:22`) to keep the database from growing forever. A separate, purely cosmetic concept — the "winner banner" — just controls whether the most recently closed season's result is still shown on the map; it stays visible for 72 hours after the season ended (`winner_banner_hours`, `app/config.py:21`), independent of when the *next* season started.

**File responsibilities, summarized:** `app/scoring.py` holds the reusable scoring/decay/defense-window math and database read/write primitives. `app/ingest.py` is where those primitives actually get called, as part of deciding what happens when a new position packet arrives — it's the real "game engine" tick. `app/seasons.py` is the season lifecycle wrapper: creating, closing, tallying, rolling over, and applying the draft's output. `app/draft.py` is the team-balancing algorithm itself, which is being removed.

---

## d) The database as it stands today

MeshWars uses a single SQLite file (path configurable, defaults to `/data/game.db`). All twelve tables are created by one script in `app/db.py` (`init_db()`, run every time the app starts). There is no user/auth table and no migration framework (more on that below).

| Table | What it's for (one sentence) | Key columns |
|---|---|---|
| `season` | One row per game season: its time window and, once closed, the final tile tally and winner. | `id`, `started_at`, `ends_at`, `status` (active/closed), `red_tiles`/`blue_tiles`/`green_tiles`, `winner` |
| `team_assignment` | Which team each node belongs to, for a given season — the draft's output. | `(season_id, node_id)` primary key, `team`, `activity_score` |
| `tile` | Current state of one map cell: who owns it, how much traffic it's seen, when it was last painted. | `(season_id, geohash)` primary key, `rcv`/`lost`, `last_sender_node_id`, `last_report_ts`, `owner_team`, `rptr_json` (list of relaying nodes), `last_packet_id` |
| `sample` | Individual, unaggregated position pings at a finer grid resolution than tiles, current season only. | `(season_id, sample_hash, sender_node_id, ts)` primary key, `snr`, `rssi`, `path_json` |
| `node_seen` | Cached roster snapshot — every node meshview has told us about this season, with name and position. | `(season_id, node_id)` primary key, `name`, `short_name`, `lat`/`lon`/`elev`, `last_seen`, `role` |
| `tile_score` | A team's current stored fortress score for a tile (decay is calculated on read, not stored decayed). | `(season_id, geohash, team)` primary key, `score`, `last_update` |
| `tile_unique_painter` | Which nodes have painted a given tile for a given team, so the one-time bonus isn't paid twice. | `(season_id, geohash, team, node_id)` primary key, `first_ts`, `paint_count` |
| `tile_capture_log` | Full permanent history of every time a tile changed hands — the audit trail. | `(season_id, geohash, ts)` primary key, `by_node_id`, `by_team`, `from_team`, `packet_id` |
| `tile_capture` | Just the *most recent* capture of each tile, used to check the 15-minute defense window quickly. | `(season_id, geohash)` primary key, `captured_at`, `captured_by_team` |
| `cursor` | Generic key/value bookmark store for the poll loop. | `k` primary key, `v` |
| `activity` | Per-node packet counts for the current season window, feeding the next snake draft. | `(node_id, window_id)` primary key, `packet_count`, `last_seen` |
| `processed_packet` | Every packet ID already looked at (whether it counted or not), so the poll loop never double-processes. | `packet_id` primary key, `processed_at` |

**How they relate:** almost every table is scoped by `season_id` (there's no hard foreign-key enforcement between most of them and `season`, it's just a convention followed consistently). Within a season, `tile`, `tile_score`, `tile_unique_painter`, `tile_capture_log`, and `tile_capture` are all keyed off the same `geohash` string — that's the "which physical map cell" join key across the scoring system. Separately, `node_id` (Meshtastic's numeric node identifier) is the universal "who" key that threads through `team_assignment`, `tile.last_sender_node_id`, `activity`, `sample.sender_node_id`, `tile_unique_painter.node_id`, `tile_capture_log.by_node_id`, and `node_seen`.

**No migration framework, and what that implies:** the entire schema is one `CREATE TABLE IF NOT EXISTS` script (`SCHEMA` in `app/db.py`) that runs on every startup — safe to re-run, but it only ever *adds* tables that don't exist yet, it never changes ones that do. There is a small, separately maintained list of `ALTER TABLE ... ADD COLUMN` statements (`MIGRATIONS`, `app/db.py:164`) that also run on every startup, wrapped in a try/except that silently ignores the "column already exists" error on repeat runs — this is how the two existing schema tweaks (`tile.last_packet_id` and `tile_unique_painter.paint_count`) were added after the fact. **What this means for later phases:** adding a brand-new table is essentially free — one line in `SCHEMA`. Adding a new column to an existing table is also easy — one more line in `MIGRATIONS`. But there is nothing here that can rename a column, change a column's type, drop a column, change a primary key, or rewrite/backfill existing rows. Anything of that shape — for example, changing what a tile's identity is keyed on, or widening the `team` column's assumed range of values from 2 to 7 — has to be done by hand, either with a one-off script that copies data into a new table shape, or by accepting that existing seasons get wiped.

---

## e) Every place that assumes Meshtastic

| File : line | What it assumes | How hard to abstract |
|---|---|---|
| `app/config.py:32` | `position_app_portnum = 3` — the numeric code meshview uses to mean "this is a position packet" is Meshtastic's own protobuf port number. This is the filter used for every packet-fetching call. | Easy — becomes a per-protocol config value; the polling code needs to either ask for both protocols or take a protocol parameter. |
| `app/config.py:38` | `excluded_roles = "ROUTER,ROUTER_LATE,CLIENT_BASE"` — these are Meshtastic's own device-role names, used to exclude infrastructure nodes (repeaters/base stations) from being treated as players. | Moderate — MeshCore doesn't share this role vocabulary at all; needs its own exclusion list, not a shared string. |
| `app/meshview_client.py:106` (`parse_meshtastic_payload_text`) | Named for and built around Meshtastic: parses meshview's "key: value" text dump of a decoded Meshtastic position protobuf, specifically hunting for `latitude_i`/`longitude_i` keys. | Hard-ish — a MeshCore equivalent needs its own parser matching whatever format the MeshCore data source (meshview or something else) actually emits. |
| `app/meshview_client.py:136` (`extract_position`) | The "divide by 1e7 if it looks like a scaled integer" heuristic is specific to how Meshtastic encodes lat/lon as `latitude_i`/`longitude_i` integers. | Moderate — needs to know per-protocol which coordinate encoding applies, or a smarter format-agnostic check. |
| `app/meshview_client.py:231` (`hop_count`) | `hop_start - hop_limit` is Meshtastic's specific packet-header hop-accounting fields. | Moderate to hard — depends on whether a MeshCore data source exposes anything equivalent at all. |
| `app/meshview_client.py:240` (`is_via_mqtt`) | Relies on meshview's `via_mqtt` flag, which reflects Meshtastic's convention of repeaters bridging heard packets to an MQTT broker for internet-wide visibility. Used to decide if a reception represents real radio reach. | Depends entirely on whether a MeshCore data source can tell us anything equivalent about RF-vs-relay provenance. |
| `app/api.py:582` (`_node_hex`) | Formats every node ID as `!{node_id:08x}` — Meshtastic's canonical 8-hex-digit "!xxxxxxxx" ID string, used throughout node lookups, popups, roster display, and the search box. | Easy to moderate — needs a protocol-aware ID formatter; MeshCore identifiers may not be 32-bit integers at all. |
| `frontend/code.js:183`, `:231`, `:263` | The UI button/panel is literally labeled "Top MQTT Feeders." | Cosmetic, but user-visible — needs neutral wording once feeders that aren't MQTT-bridged Meshtastic repeaters show up. |
| `app/ingest.py:48` (`GEOHASH_TILE_PRECISION`) | The tile grid itself (geohash precision 6) is one global constant with no protocol awareness — chosen with Meshtastic's typical multi-kilometer RF reach in mind. Superseded regardless of protocol by decision 1 below, but worth noting there's currently no per-protocol tile-sizing concept at all. | N/A — being replaced outright (see section g). |
| `app/config.py` / `app/meshview_client.py` (whole client) | The entire ingest pipeline assumes exactly **one** upstream data source (`meshview_base_url`), which itself only knows about Meshtastic. | Hardest item on this list — `MeshviewClient` and `Ingestor` are both class-designed around a single upstream, not just parameterized by a constant. Adding MeshCore means either meshview needs to carry MeshCore data too, or a second, parallel client/ingest path needs to exist. |

I also want to flag one thing that is *not* a Meshtastic-protocol assumption but is closely related and just as load-bearing: `app/ingest.py:460` computes `opponent = "BLUE" if team == "RED" else "RED"` — the capture/flip decision is written assuming there are exactly two teams and they're always each other's opponent. That's a team-count assumption, not a protocol one, but it sits right in the core scoring logic and is directly relevant to decision 2 in section g below.

---

## f) What a protocol discriminator ('mt' vs 'mc') would need to touch — analysis only

This section only describes where a protocol tag would have to be threaded through, based on the assumptions in section e. It is not a design or a recommendation.

- **Packet fetching** (`app/config.py:32`, `position_app_portnum`): a discriminator would need to travel with every upstream fetch call, since the portnum filter (or its MeshCore equivalent) is protocol-specific.
- **Role exclusion** (`app/config.py:38`): the exclusion-list lookup would need to branch on protocol before comparing a node's role string against the list.
- **Position parsing** (`app/meshview_client.py:106`, `:136`): the payload parser and the coordinate-scaling heuristic would both need to know which protocol's packet they're looking at before choosing a parsing strategy.
- **Hop counting** (`app/meshview_client.py:231`): whatever computes "how many hops did this take" would need a protocol-specific code path, assuming a MeshCore equivalent even exists.
- **MQTT/relay detection** (`app/meshview_client.py:240`): the "was this real RF or just relayed" check would need a protocol-aware variant, or an entirely different check for MeshCore.
- **Node ID formatting** (`app/api.py:582`): every place that turns a raw node ID into a display string (popups, roster, lookup, search) would need to know which protocol's ID format to apply.
- **UI labeling** (`frontend/code.js:183` etc.): any place displaying protocol-specific terminology ("MQTT Feeders") would need to either stay protocol-specific in context, or generalize.
- **The upstream client/ingest pipeline itself** (`app/meshview_client.py`, `app/ingest.py`): the biggest one — a protocol discriminator here isn't a single flag to thread, it's a question of whether one client handles both protocols with branching, or two parallel clients/ingest loops feed the same downstream tables. Everything above depends on which shape this takes.

---

## g) Decisions already made by the owner (fixed context for later phases)

1. **The tile grid is changing.** Today, tiles are geohash cells at precision 6 (roughly 1.2 km by 0.6 km, `GEOHASH_TILE_PRECISION` in `app/ingest.py:48`). It is moving to MeshMapper's grid cell definition instead: 0.0027 degrees of latitude by 0.00384 degrees of longitude, roughly 300 meters. Meshtastic is being adapted onto this new grid — it is not staying on the old geohash grid while MeshCore gets the new one. Tables that key off `geohash` today and would therefore be directly affected by this change: `tile`, `tile_score`, `tile_unique_painter`, `tile_capture_log`, `tile_capture`, and `sample`. Also worth noting: the per-node-per-tile cooldown check in the ingest pipeline (`app/ingest.py:415`) currently relies on geohash's nested-prefix property — an 8-character sample hash always starts with the same 6 characters as its parent tile hash, so the cooldown query does a `LIKE` prefix match to find recent samples inside the same tile. A flat lat/lon-degree grid doesn't have that built-in nesting the same way, so this specific mechanism will need rethinking, not just a change of constants. Separately, the frontend's tile-drawing code (`geo.decode_bbox`, called from `frontend/code.js:566`) also assumes geohash-format tile IDs to turn a tile ID back into a map rectangle — a new grid format needs its own decode logic on the frontend too.

2. **Teams go from 2 to 7.** The seven teams are RED, GREEN, BLUE, PURPLE, YELLOW, ORANGE, and PINK. Automatic team assignment via the snake draft is being removed entirely — `app/draft.py` is being dropped — and replaced with explicit player registration. This also removes the "auto-assign a new node to whichever team is smaller" behavior currently triggered the moment an unrecognized node sends its first qualifying position (`assign_new_node`, called from `app/ingest.py:384`). As flagged in section e, the binary-opponent assumption baked into the capture/flip logic (`opponent = "BLUE" if team == "RED" else "RED"`, `app/ingest.py:460`) will need to become genuinely multi-team-aware, not just have more team names added to a list — with 7 teams, "the opponent" isn't a single well-defined team the way it is with 2.

3. **Players will be created by an explicit join action carrying a chosen color, not inferred from traffic.** Today, a node's team is inferred automatically the first time it sends a qualifying position packet. Going forward, a player will explicitly register/join and pick their color up front, and that registration — not radio traffic — is what puts them on a team. There are two entry points into that same registration: a join command that carries the chosen color creates the player and issues a single-use token, and a web page redeems that token for the player's API key. Either way, it's the same registration underneath, just two doors into it.

4. **There is no neutral or unclaimed tile state.** A tile is owned by a team, or it does not exist — there is no "nobody owns this yet" status for a tile to sit in. This has two direct consequences. First, the `season` table's three hardcoded tally columns (`red_tiles`/`blue_tiles`/`green_tiles`) have to be replaced with a per-team tally, because "green" previously meant "unclaimed" (see section c) and green is now a real, playable team — it can no longer double as the label for "nobody's." Second, traffic from any node or contact that isn't a registered player gets dropped outright rather than painting anything, since there's no neutral/green bucket left for it to land in. The practical effect: when the new season opens on the new grid, the Meshtastic board starts completely empty and stays empty until people actually register, because both Meshtastic node auto-discovery and the draft that used to seed teams automatically are gone.

5. **Capturing a tile from another team only requires out-scoring that team's current owner, not every other team.** With 7 teams, "the opponent" is no longer a single well-defined team the way it was with 2 (see decision 2) — an attacker only has to beat whichever team currently holds the tile. The 15-minute defense window (`defense_window_seconds = 900`, `app/config.py:49`) still applies unchanged; a tile still can't flip within 15 minutes of its last capture regardless of score. This replaces the hardcoded two-team opponent logic at `app/ingest.py:460`.

6. **There is no play area.** Coverage is global, identical to MeshMapper's — there are no regions and no geographic limit the game is scoped to. The consequence is that the previously planned "is this position outside the play area" sanity check has nothing left to check against, since there's no play-area boundary to be outside of. It reduces to just validating that a coordinate is real at all: latitude within plus or minus 90 degrees, longitude within plus or minus 180 degrees, and rejecting the exact 0,0 point that broken GPS hardware emits. The existing speed and clock sanity checks are unaffected by this.

7. **Meshtastic and MeshCore get separate scoreboards.** They are scored and tallied independently — different meshes, different boards — and are not merged into one combined ranking.

8. **There is no session slot cap on our side** — no limit on concurrent live-tracking sessions/slots imposed by MeshWars.

---

## h) What's still open

There are no open owner decisions left — everything above is locked. The one thing still genuinely unresolved is a known future problem, not a question, and it's being deliberately deferred rather than solved now:

- **The new grid produces roughly 8x more tiles for the same ground covered, and the map page as it exists today won't survive that.** A geohash-6 cell is about 0.74 square kilometers; a MeshMapper cell is about 0.093 square kilometers — roughly an eightfold increase in tile density for the same coverage area. The database side of this is fine, since only tiles that actually get painted become rows — an empty global board costs nothing extra. The problem is the map page: it currently fetches every tile in the season in one response and redraws all of it every 30 seconds. That approach does not survive an 8x increase in tile count on a board with global (not bounded) coverage. This is recorded here as a Phase 4 concern, to be solved when Phase 4 is reached — not something to design around now.
