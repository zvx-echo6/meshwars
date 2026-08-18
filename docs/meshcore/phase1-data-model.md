---
title: Phase 1 Data Model — MeshCore Support
status: draft
---

# Phase 1 Data Model: the new database shape for MeshCore

This is a design proposal, not a build. Nothing here is implemented yet. It describes the database changes needed to bring MeshCore into MeshWars alongside Meshtastic, on top of the decisions already locked in Phase 0 (seven teams, explicit player registration, the new grid, no neutral tile state). It builds directly on that Phase 0 document (`docs/meshcore/phase0-discovery.md`) — read that first if any of the "why" here is unclear.

Every table below gets a one-sentence explanation of what it is *for*, in plain English, before any column list. The column lists are the actual proposed shape; the SQL types shown are illustrative of intent, not a finished schema file.

No code changes, no SQL files, and no other files were touched to produce this document. It is written against the real current schema in `app/db.py` and the real current code in `app/ingest.py` and `app/scoring.py`, so the "before" side of every comparison below is accurate as of `feature/meshcore` at commit `a21c01e`.

---

## The grid

Today, a tile is a geohash cell (roughly 1.2 km by 0.6 km). This design replaces that with a flat grid cell that matches MeshMapper's own grid, so our board and MeshMapper's board describe the same physical squares.

A cell's identity is a plain text string built from two numbers:

```
cell_id = "<latIdx>_<lonIdx>"

latIdx = floor(latitude  / 0.0027)
lonIdx = floor(longitude / 0.00384)
```

Both `latIdx` and `lonIdx` are signed integers — south of the equator or west of the prime meridian, they go negative. That's expected and fine; the cell_id text just carries a `-` in front of one or both numbers.

To go the other direction — turn a cell_id back into a rectangle for drawing on the map — the math is:

```
south = latIdx * 0.0027
north = south + 0.0027
west  = lonIdx * 0.00384
east  = west  + 0.00384
```

**An assumption we have not confirmed yet:** this math assumes MeshMapper's grid origin sits at latitude 0, longitude 0 — that is, cell `0_0` is the square whose southwest corner is exactly on the equator and the prime meridian. That's the natural, obvious choice for a grid origin, and it's what we're building against. But we have not checked it against a real MeshMapper payload yet. If MeshMapper's actual origin is offset from zero by even a fraction of a cell, our board will be shifted from theirs by that same fraction, and the two maps will stop lining up — a real position near a cell boundary could land in the cell next door on one map and not the other. This needs to be checked the first time a real batch of MeshCore data arrives (see Open Questions, item 3). If it turns out to be wrong, it's a one-line fix — an offset added to the two formulas above — not a redesign.

**Why this replaces the geohash-prefix trick.** Geohash IDs nest: a finer-precision geohash always starts with the exact characters of its coarser parent. The current per-node cooldown check in `app/ingest.py:415-422` leans on that directly — it does a `sample_hash LIKE tile_hash || '%'` text match to find recent samples inside the same tile, because an 8-character sample geohash is guaranteed to start with its 6-character tile geohash. Flat `latIdx_lonIdx` cell IDs don't nest that way at all — there's no shorter or longer version of a cell ID to prefix-match against. So the cooldown lookup has to change from "find rows whose hash starts with this tile's hash" to "find rows whose cell_id exactly equals this cell." That's actually simpler, not harder — an exact match is a plain equality check, not a text pattern match, and it doesn't depend on two different precisions being kept in sync.

---

## Protocol tag

Every scoring table gains a `protocol` column, holding either `'mt'` (Meshtastic) or `'mc'` (MeshCore). Meshtastic and MeshCore run completely separate scoreboards — separate seasons, separate tile ownership, separate everything downstream. `protocol` becomes part of the primary key on `season`, and because every other scoring table hangs off a `season_id`, the separation between the two games' data flows down from that one place without needing to be repeated on every table.

---

## New tables

### player

One row per registered person. This is the thing that owns everything else in the game — radios, keys, team, history all trace back to a player_id.

```
player_id     INTEGER PRIMARY KEY AUTOINCREMENT
display_name  TEXT NOT NULL
team          TEXT NOT NULL      -- RED GREEN BLUE PURPLE YELLOW ORANGE PINK
created_at    INTEGER NOT NULL
disabled_at   INTEGER            -- set to soft-ban a player without deleting history
```

Disabling a player sets `disabled_at` rather than deleting the row, so a banned player's captured tiles and stats stay intact and explainable rather than becoming orphaned data.

### player_node

Which radios belong to which person. One person can register more than one radio.

```
protocol   TEXT NOT NULL         -- 'mt' or 'mc'
node_ref   TEXT NOT NULL         -- Meshtastic: the '!xxxxxxxx' id. MeshCore: the 8-hex contact prefix.
player_id  INTEGER NOT NULL
bound_at   INTEGER NOT NULL

PRIMARY KEY (protocol, node_ref)
-- plus an index on player_id
```

The primary key being `(protocol, node_ref)` is what enforces that one specific radio belongs to exactly one person at a time — you can't bind the same node twice to two different players. Storing the Meshtastic node id as text (`node_ref`) rather than as the integer node number MeshWars uses internally today is deliberate: it lets both protocols' radio identifiers live in one column with one shape, instead of needing a numeric column for Meshtastic and a separate text column for MeshCore.

### api_key

The per-player secret that MeshMapper sends us on every batch, proving which player a batch of positions belongs to.

```
key_hash      TEXT PRIMARY KEY   -- SHA-256 of the key, hex
player_id     INTEGER NOT NULL
issued_at     INTEGER NOT NULL
revoked_at    INTEGER
last_seen_at  INTEGER
-- plus an index on player_id
```

We store only a hash of the key, never the key itself — the same principle as how a website stores a hash of your password, not the password. The player sees their real key exactly once, at the moment it's issued to them. If they lose it, we can't look the old one up and hand it back to them; we issue a brand new one instead. "Revoking" a key means setting `revoked_at`, not deleting the row, so there's a permanent record of every key a player has ever held, even ones that were later turned off. `last_seen_at` is updated every time the key is actually used, which is how we can tell a player whether their MeshMapper setup is actually working, rather than guessing.

### join_token

A short-lived, single-use ticket that turns a join request into an API key.

```
token_hash   TEXT PRIMARY KEY    -- SHA-256, hex
player_id    INTEGER NOT NULL
team         TEXT NOT NULL
created_at   INTEGER NOT NULL
expires_at   INTEGER NOT NULL    -- created_at + 900 seconds (15 minutes)
consumed_at  INTEGER
```

This is hashed for exactly the same reason as the API key: whoever holds the token can use it to claim the player's API key, so a join token is functionally a password, not just an id — anyone who intercepts it could claim someone else's registration. This is a deliberate strengthening of the original sketch of this feature, which stored the join token in the clear. The token expires 15 minutes after creation and can only be redeemed once, both of which limit how much damage a leaked or guessed token could do.

### player_last_fix

The single most recent position for a player, kept only at grid-cell resolution, used solely to catch physically impossible movement (someone appearing to travel 400 miles an hour, which usually means bad or spoofed GPS data rather than a real fast-moving person).

```
player_id  INTEGER NOT NULL
protocol   TEXT NOT NULL
cell_id    TEXT NOT NULL
ts         INTEGER NOT NULL

PRIMARY KEY (player_id, protocol)
```

This is one row per player-per-protocol that gets overwritten every time a new position comes in — it is not a history and never accumulates rows. It stores a grid cell, never an exact coordinate. A roughly 300-metre cell is plenty precise enough to catch someone who apparently teleported across the map between two reports; it doesn't need to know exactly where within that cell they were.

### player_cell_ping

Records that a given player pinged a given cell at a given moment. It answers two questions: "have we already processed this exact ping" (deduplication — a batch that MeshMapper accidentally sends to us twice, a retry after a dropped network response for example, can't be scored twice) and "did this player paint this cell recently" (the 5-minute cooldown).

```
player_id  INTEGER NOT NULL
protocol   TEXT NOT NULL
cell_id    TEXT NOT NULL
ts         INTEGER NOT NULL
seen_at    INTEGER NOT NULL

PRIMARY KEY (player_id, protocol, cell_id, ts)
```

**Why one table can do both jobs.** The cooldown is 5 minutes, hardcoded as `COOLDOWN_SECONDS = 300` at `app/ingest.py:413`. Today it's enforced by asking the `sample` table a question: what is the most recent moment this node painted anything inside this tile? Because geohash IDs nest, that question has to be answered with a text prefix match against `sample` — see the Grid section above.

With flat cell IDs, the same question becomes an exact-equality lookup instead: the most recent `ts` for this player, this protocol, and this exact `cell_id`. That's precisely the shape `player_cell_ping`'s primary key already gives you — `(player_id, protocol, cell_id, ts)` fixes the first three columns to known values and asks for the maximum of the fourth, which is exactly what the primary key index is built to walk straight to. No extra index, no table scan, no second table needed.

Dedup and cooldown turn out to be the same underlying fact restated twice — "this player pinged this cell at this moment" — so one row answers both questions instead of needing two separate tables that would otherwise have to agree with each other. That's simpler than what exists today: a text-pattern match against `sample` for the cooldown, plus a wholly separate dedup concept.

The 48-hour retention window (rows older than 48 hours get pruned, since a redelivery that old isn't a realistic concern) is far longer than the 5-minute cooldown window, so pruning can never delete a row the cooldown still needs to see.

**Why `protocol` is part of the key.** A player can hold both a Meshtastic radio and a MeshCore radio (see `player_node` above), and the two run completely separate scoreboards. Their cooldowns have to be independent too — without `protocol` in the key, a Meshtastic paint of a cell would suppress a MeshCore paint of that same physical square for the next 5 minutes, even though they're different games as far as scoring is concerned.

### player_ingest_stat

Per-player, per-day counters that let us tell a player exactly why their setup isn't scoring points, instead of guessing.

```
player_id           INTEGER NOT NULL
protocol             TEXT NOT NULL
day                  INTEGER NOT NULL   -- UTC date as YYYYMMDD
batches              INTEGER DEFAULT 0
pings_accepted       INTEGER DEFAULT 0
pings_no_contact     INTEGER DEFAULT 0  -- the "Include Contact Key" toggle is off
pings_wrong_owner    INTEGER DEFAULT 0  -- contact key maps to a different player than the API key
pings_duplicate      INTEGER DEFAULT 0
pings_bad_coord      INTEGER DEFAULT 0

PRIMARY KEY (player_id, protocol, day)
```

Kept for 30 days. The practical payoff: when someone says "MeshWars isn't seeing me," we can look at their counters and answer immediately — for example "you're sending batches but every ping is missing the contact key, turn on Include Contact Key in MeshMapper" — instead of digging through raw logs or guessing.

---

## Changed tables

### season

Gains `protocol`. Loses the three hardcoded colour tally columns.

The primary key stays `(id)`. `protocol` becomes a plain column, and there is one active season per protocol at a time (one active Meshtastic season, one active MeshCore season, running independently).

`red_tiles`, `blue_tiles`, and `green_tiles` are removed entirely, replaced by the new `season_team_tally` table below. Why: those three columns exist because the game used to have exactly two real teams (red and blue) plus green meaning "unclaimed." Green is now a real, playable team, and there are seven teams total — three hardcoded columns simply can't hold seven teams' worth of tallies. A per-team row is the only shape that scales to any number of teams.

### season_team_tally (new — replaces the three removed columns)

One row per team per season, holding that team's current tile count.

```
season_id  INTEGER NOT NULL
team       TEXT NOT NULL
tiles      INTEGER NOT NULL DEFAULT 0

PRIMARY KEY (season_id, team)
```

### tile

The geohash column becomes `cell_id`. The "who painted this most recently" column moves from a Meshtastic node number to a player.

```
PRIMARY KEY (season_id, cell_id)
```

- `last_sender_node_id` becomes `last_player_id` — it now records the person who last painted the tile, not the radio.
- `owner_team` is always a real team now, never a neutral placeholder. (Today, an unrecognized or excluded-role sender paints a tile "GREEN" to mean "nobody's claimed this." That placeholder use of green goes away because green is now a real team that can win tiles honestly — there is no neutral bucket left for unregistered traffic to land in. Unregistered traffic simply doesn't paint anything.)
- `last_packet_id` stays, but becomes nullable, since a packet id is a Meshtastic-only concept and MeshCore pings don't have one.
- `rcv`, `lost`, `last_report_ts`, `last_snr`, `last_rssi`, and `rptr_json` stay exactly as they are today.

### tile_score

`(season_id, cell_id, team)` primary key. Otherwise unchanged.

### tile_unique_painter

`(season_id, cell_id, team, player_id)` primary key. `node_id` becomes `player_id`.

This is a meaning change worth being explicit about, not just a rename. The one-time bonus this table protects (an extra point the first time a *different* radio paints a tile for a team) used to reward a different **radio**. It now rewards a different **person**. That matches what the bonus was always meant to reward — genuinely more people contesting a tile, not more hardware. It also closes an obvious exploit under the old model: one person with several radios could paint the same tile with each radio in turn and collect the "unique painter" bonus repeatedly, when really it was the same person the whole time. Keying the bonus on player_id instead of node_id makes that no longer possible.

### tile_capture_log

`(season_id, cell_id, ts)` primary key. `by_node_id` becomes `by_player_id`. `packet_id` becomes nullable (same Meshtastic-only reasoning as `tile.last_packet_id`).

### tile_capture

`(season_id, cell_id)` primary key. Otherwise unchanged.

---

## Tables being dropped

**team_assignment** — gone entirely. A player's team now lives directly on the `player` row and is set once, at registration. It does not get redealt every season the way node-to-team assignments used to.

**activity** — gone entirely. Its only job was feeding the snake draft that used to auto-balance new nodes onto the smaller team, and the draft is gone (Phase 0 already locked this — `app/draft.py` is being removed).

**sample** — recommended for removal, but flagged here as a decision the owner still needs to sign off on, not something already decided. Three reasons:

1. It is the only place in the database that stores a position finer than a whole grid cell (an 8-character geohash, versus the 6-character geohash tiles use today). That cuts directly against the minimum-retention principle this whole design is built on — see the Privacy section below.
2. The only UI control that displays sample data lives in a settings panel that is never actually connected to the map — no player can reach it today through normal use of the app.
3. Its extra precision has no meaning once tiles themselves are already flat, small grid cells. A sample geohash used to be meaningfully finer than a tile; a MeshMapper cell is already about as fine-grained as sample data was, so keeping a separate finer-grained table buys nothing.

`sample` also had a second consumer beyond the disconnected settings panel above — the cooldown lookup at `app/ingest.py:415-422`. That lookup now reads from `player_cell_ping` instead (see New Tables, above), so nothing in the codebase is left reading from `sample`.

If the owner wants `sample` kept anyway, it would have to be recorded at cell-level precision rather than its current finer precision (to respect the minimum-retention principle) — at which point it would hold exactly the same information as the `tile` table already holds, making it redundant. See Open Questions, item 2.

---

## Tables kept unchanged

**node_seen** — the Meshtastic roster snapshot pulled from meshview, still needed to draw node markers on the map. Meshtastic only; MeshCore has no equivalent because MeshCore data arrives as pushed batches, not a roster we poll.

**cursor** and **processed_packet** — Meshtastic poll bookkeeping (tracking which packets have already been fetched from meshview). Unaffected by any of this — MeshCore doesn't use meshview at all.

---

## Privacy

**No raw latitude or longitude from MeshCore is ever written to the database.** The conversion from a real coordinate to a cell id happens in the background worker, in memory, before anything touches storage. The only position-shaped thing that ever gets persisted is a cell id — a roughly 300-metre square someone was somewhere inside of, never their exact point.

The one exception is a raw-batch debug log, which the owner specifically asked for so real MeshMapper payloads can be inspected while tuning the ingest logic. That log must:

- be off by default,
- be controlled by a config flag (not a code change) to turn on,
- write to a rotating log file on disk, never to the database, and
- be clearly labelled, in the file itself and in any documentation of it, as containing real GPS tracks of real people.

The recommendation is that this flag only ever gets turned on briefly, for active tuning, and turned back off immediately afterward — not left on as a standing feature.

This is stricter than the original sketch of the MeshCore ingest pipeline, which allowed raw coordinates to be retained for a 24-hour rolling scoring window. That retention isn't needed: a cell id is sufficient for every scoring decision the game makes, and the implausible-speed check (`player_last_fix`, above) works fine comparing cell to cell rather than coordinate to coordinate.

---

## How the schema change actually gets applied

This is not a migration, and it's worth being direct about why. Tile identity (`cell_id` in place of `geohash`) is part of the primary key on five tables — `tile`, `tile_score`, `tile_unique_painter`, `tile_capture_log`, and `tile_capture`. There is no tooling in this codebase (or realistically, in SQLite generally) that changes a primary key on an existing table in place; a primary key change means the table is effectively a different table.

The owner has already decided the practical path around that: wipe and start a fresh season on the new grid rather than attempt to convert anything. So the actual change is: add the new tables to the startup schema script (`SCHEMA` in `app/db.py`), define the changed tables in their new shape, and start clean. Old season data on the old geohash grid is not converted to the new grid, because a geohash cell and a MeshMapper cell are different shapes covering different ground — there is no faithful way to convert one into the other, and doing so would mean inventing data that was never actually collected.

---

## Decisions

The owner has answered three of the four items below; those are recorded as settled. The fourth is not something to decide — it is a verification step — and stays open until it can actually be checked.

1. **A player's team is fixed at registration.** Decided: a player cannot change their own team. Switching sides mid-season would let someone hand over tiles they hold to whichever team they defect to, which undermines the whole point of team ownership. If this is ever wanted, it is an administrative action performed on a player's behalf, not a player-facing control.

2. **The `sample` table is dropped.** Decided: `sample` is removed, for the three reasons given above (minimum-retention principle, no reachable UI, redundant with tile-level precision once tiles are already grid cells). The technical blocker — the cooldown lookup depending on `sample` — is gone now that the cooldown reads from `player_cell_ping` instead (see Tables Being Dropped, above).

   Removing the table still has two knock-on effects in existing code that have to be handled at the same time the table is actually dropped: the `/get-samples` route in `app/api.py` currently serves from `sample`, and `app/ingest.py` currently writes to it. Both need to be removed alongside the table, not left behind pointing at a table that no longer exists.

3. **A player may bind more than one radio.** Decided: allowed. The schema as designed already permits it (nothing in `player_node` limits one player to one node_ref). A person can genuinely own both a Meshtastic node and a MeshCore radio, and because the two protocols run completely separate scoreboards, one person holding both doesn't give them any unfair advantage on either board.

4. **Confirm MeshMapper's grid origin against a real payload — still pending.** Not a decision; a verification step still to be done. This needs to happen once Phase 2 is actually receiving MeshCore data — the grid math above assumes an origin of latitude 0, longitude 0, and that assumption is unverified. See the Grid section above for what's at stake if it's wrong.

---

## Problems I found

Specific places where the existing code either contradicts this design or doesn't yet account for it. These are flagged rather than silently designed around, per the instructions for this document.

1. **RESOLVED — the cooldown's data source.** This used to be an open question: `app/ingest.py:415-422` reads `MAX(ts) FROM sample WHERE sample_hash LIKE tile_hash || '%' AND sender_node_id = ...` to enforce the cooldown, and neither `player_last_fix` nor the original `ingest_dedup` was an obvious drop-in replacement once `sample` is gone — and the `sample`-removal rationale didn't even mention that cooldown was a second, load-bearing consumer. Both are resolved now: the cooldown reads from `player_cell_ping` (see New Tables, above), and `sample` has no remaining consumer.

2. **Auto team-assignment code is still live and fully wired in, not just schema-dependent.** `app/ingest.py:351-398` currently auto-creates a `team_assignment` row and calls `assign_new_node()` (from `app/draft.py`) the moment an unrecognized node sends its first qualifying position. Since `team_assignment` is dropped and `app/draft.py` is being removed (per Phase 0, decision 2), this entire code block — not just the table it writes to — has to be deleted and replaced with "only registered, bound players can paint a tile; anyone else is dropped." This is a bigger change to `ingest.py` than a column rename; flagging it so it isn't underestimated during implementation.

3. **The GREEN-as-neutral-paint branch has to be deleted, not adapted.** `app/ingest.py:369-370` and `app/ingest.py:529-555` currently treat excluded-role or unrecognized senders by painting the tile `owner_team = 'GREEN'` as a neutral/unclaimed marker, while still bumping `rcv` and updating `rptr_json`. Under this design (and per Phase 0, decision 4, already locked), green is a real playable team and unregistered traffic must be dropped outright rather than painted at all. That whole `else` branch needs to be removed, not have "GREEN" swapped for something else.

4. **`season.winner`'s comment is stale under seven teams.** `app/db.py:25` documents `winner` as `'RED' | 'BLUE' | 'TIE' | NULL`. With seven teams and a real per-team tally (`season_team_tally`), the set of valid winner values is any one of the seven team names (or a tie, or null while active) — the two-team comment doesn't reflect that. Not a blocking issue, just a stale comment worth catching during implementation so it doesn't mislead whoever writes the season-close logic.

5. **The existing `MIGRATIONS` list becomes dead weight.** `app/db.py:164-167` has two `ALTER TABLE` statements (`tile.last_packet_id`, `tile_unique_painter.paint_count`) that exist to patch older, already-deployed databases. Since this design's implementation path is "wipe and start clean" rather than migrate, a fresh schema already includes those columns from the start, and these two ALTER statements have nothing left to do. Not a contradiction, just worth deleting during implementation rather than carrying forward as confusing leftover code.
