---
title: Operator Handbook
status: reference (repo only, not published to the public site)
---

# Operator Handbook

This documents every control in the admin panel (`/admin`, `frontend/admin.html` and `frontend/admin.js`, backed by `app/admin_api.py` and `app/admin_ops.py`): what it does, which endpoint it calls, whether it can be undone, what confirmation stands between an operator and running it, and when you'd actually reach for it. It is deliberately not on the public site — it documents what an operator can do to a player's account, and that stays in the repo.

Read the code before trusting anything not cited here; every claim below is checked against the source, not guessed at.

## Signing in

There is no other authentication anywhere in this application — `/admin` and its token are the whole of it (`app/admin_api.py:1-8`). An empty `admin_token` disables the admin surface outright: `/admin` itself 404s (`app/admin_api.py:82-83`) and every `/api/admin/*` route 404s rather than 401s, so a disabled admin door is indistinguishable from one that was never built (`app/admin_api.py:69-70`). With a token set, every route checks it against the `X-Admin-Token` header with `secrets.compare_digest` so a wrong guess can't be timed (`app/admin_api.py:63-74`). The token lives in one JS variable for one page load — never localStorage, never a cookie — because it can delete every player and a browser that remembers it hands that power to whoever opens the laptop next (`frontend/admin.js:5-8`).

## Overview

`GET /api/admin/overview` (`app/admin_ops.py:294-324`) drives this page. It exists because the data that answers "why is nothing happening for me" — `player_ingest_stat` — was previously reachable only through the player's own key-authenticated `/api/mc/status`, and keys can't be recovered, so the person supporting a stuck player was the one person who couldn't look (`app/admin_ops.py:1-19`).

**Needs attention** (`_attention`, `app/admin_ops.py:64-218`) computes a list of problems rather than listing everything that exists, ordered by how stuck a player is (never-accepted-anything before merely-stale). Every entry carries player, team, kind, a plain-English detail and fix, and a severity (`bad`/`warn`/`info`). Read-only — nothing here writes anything except two of its rows, which carry their own action buttons:

- **checkin_unreachable** rows carry a "Register" control that calls `POST /api/admin/checkin/binding` inline (`frontend/admin.js:216-228`) — see Check-ins below.
- Every other kind (`no_radio`, `no_key`, `no_contact_key`, `out_of_area`, `no_repeaters`, `never_accepted`, `never_sent`, `wrong_owner`, `checkin_name_changed`, `stale`) is diagnostic text plus an "Open" button that jumps to that player's row in Players — it changes nothing.

**Health** (`_health`, `app/admin_ops.py:221-273`) and **poller liveness** (`_poller_health`, `app/admin_ops.py:276-291`) are pure read-outs: pings in the last hour/day, database and free-disk bytes, whether the check-in poller is running and when it last polled, whether the town-data file that Places Worth Going depends on is loaded. None of it is a control.

**Seasons** on this page is the "extend by N days" widget (`renderSeasons`, `frontend/admin.js:250-282`) — covered under Seasons below, since it's the same endpoint whether you reach it from Overview or the Seasons tab.

## Players

`GET /api/admin/players` (`app/admin_api.py:93-144`) lists every player with their radios and keys (only an 8-character key-hash prefix, never the hash or the raw key). Expanding a player (`renderPlayerDetail`, `frontend/admin.js:338-481`) exposes these controls:

**Change team** — `POST /api/admin/player/team` (`app/admin_api.py:963-1039`). Unlimited, unlike a player's own once-a-month self-service switch on the join page. Ground a player currently holds stays with their old team (`mc_tile.owner_team` is frozen at paint time and never re-derived from `player.team`); check-in points, exploration points and streaks travel to the new team for free, because those are computed live off `player.team`. Fully reversible by switching back, so it needs only a plain `window.confirm()` (`frontend/admin.js:362-366`), not a typed name. Reach for it when a player asks to switch teams outside their own monthly window, or to fix a team picked by mistake at join.

**Add radio** — `POST /api/admin/node/add` (`app/admin_api.py:264-376`). Binds a radio to a player with no key involved — the reason it exists: a MeshCore player's key already lives in their MeshMapper config, a Meshtastic player has no such fallback, and either way the fix for "this radio isn't registered" shouldn't require touching keys. Not destructive: it only ever creates a binding nobody held, or confirms one this player already has (`app/admin_api.py:276-283`). A wrong `player_id` binds a real radio to the wrong but real player — visible immediately and reversible with Remove — so this gets no confirmation dialog at all beyond being logged in. Reach for it when a player's radio never auto-bound (MeshCore, "Include Contact Key" off) or when adding a Meshtastic node ID by hand.

**Remove radio** — `POST /api/admin/node/remove` (`app/admin_api.py:379-461`). Destructive: it silently takes away MeshWars' ability to recognize that radio as this player's, breaking attribution for it (`app/admin_api.py:383-388`). Requires typing the player's exact current display name (`frontend/admin.js:290-302`, `window.prompt`). Use it when a radio changed hands, or a binding is simply wrong.

**Revoke key** — `POST /api/admin/revoke` (`app/admin_api.py:147-211`), one row per key under Keys. Matches on a hash prefix of at least 4 characters (`_MIN_PREFIX_LEN`, `app/admin_api.py:38-41`); an ambiguous prefix refuses with 409 rather than guessing. Sets `revoked_at`, never deletes the row, so the record of every key a player has ever held survives (`app/admin_api.py:186-190`). The auth cache is invalidated immediately so the revocation takes effect on the very next ingest attempt, not after the cache TTL (`app/admin_api.py:198-202`). **No confirmation dialog at all** — the button posts on click (`frontend/admin.js:306-326`). It is reversible only in the sense that the player can be issued a new key; the revoked one itself never works again.

**Issue extra key** — `POST /api/admin/player/issue_key` (`app/admin_api.py:648-730`). Mints an *additional* key without touching any key the player already holds — `api_key` has never enforced one-key-per-player. This is the fix for "I lost my key," as distinct from "someone else has my key." The worst case for a wrong `player_id` is a real player getting an extra working key they didn't ask for — nobody loses access — so it carries the same light guard as Add radio: no confirmation beyond being signed in (`app/admin_api.py:669-675`, `frontend/admin.js:446-454`). The admin UI deliberately styles this button lighter than Revoke & reissue so a tired operator reaching for the wrong one doesn't cause an outage (`app/admin_api.py:664-667`).

**Revoke & reissue** — `POST /api/admin/player/reissue` (`app/admin_api.py:733-837`). Mints one new key and revokes *every* key the player currently holds, in one operation. "I lost my key" and "someone else has my key" look identical from the admin side, so the safe default in both cases is that whatever the player had before stops working the moment a new one is issued — like a password reset that invalidates the old password (`app/admin_api.py:743-750`). This breaks the player's current setup (MeshMapper config, anything else holding the old key) the instant it runs, so it requires typing the player's exact display name (`frontend/admin.js:456-466`, `window.prompt`, with the warning text built in). **Irreversible for the old key** — `api_key` stores only a SHA-256 hash, so a revoked raw key cannot be recovered by any route, ever (`app/admin_api.py:738-742`).

**Disable / enable player** — `POST /api/admin/player/disable` / `.../enable` (`app/admin_api.py:507-514`, sharing `_set_player_disabled` at `464-505`). Flips `disabled_at`. Fully reversible — enabling clears it back to `NULL`. Invalidates the auth cache immediately so it takes effect right away. **No confirmation dialog** (`frontend/admin.js:438-445`). This is the right tool for "make someone stop playing" — see Delete player below for why that's a different, much heavier operation.

**Delete player — the one to be careful with.** `POST /api/admin/player/delete` (`app/admin_api.py:517-645`). This does not just remove the player row. In one transaction it deletes, in order (`app/admin_api.py:564-625`):

- every square where this player is the *last painter* (`mc_tile` rows keyed to them), and that square's `mc_tile_score`, `mc_tile_capture`, and `mc_tile_capture_log` rows — so nothing is left pointing at a square that no longer exists
- their `mc_tile_unique_painter` credit (who first painted a cell, for the unique-painter bonus)
- their `player_ingest_stat` diagnostics history and `player_cell_ping` MeshCore ping history
- every `player_node` radio binding and every `api_key` they hold
- the `player` row itself

This **rewrites territory history**: squares that player last held simply cease to have a score, a capture window, or a capture log, as if the capture never happened. It is not a soft delete and there is no undo — the transaction either completes fully or rolls back entirely (`app/admin_api.py:551,627-630`). It requires typing the player's exact display name (`frontend/admin.js:468-477`), and the UI's own warning is blunt: "Deleting removes them and everything they earned."

**If the goal is just to stop someone playing, use Disable, not Delete.** Delete is for a genuine data-hygiene case — a duplicate registration, a test account, a request to be forgotten — not for "I want this person off my team" or "they're being disruptive." Disable stops them earning or painting anything new while leaving their history, and everyone else's captures against their squares, intact.

## Check-ins

**Credit a check-in** — `POST /api/admin/checkin/award` (`app/admin_ops.py:464-512`). Backfills a check-in the poller missed. Streak and points are computed exactly as the poller would have computed them on the night (`checkin_streak`/`streak_points`), so a hand-added award is worth what it should be worth and affects everyone else's streak the same way a real one would — the monthly honors read these rows and cannot tell it was added by hand (`app/admin_ops.py:466-474`). The insert is `INSERT OR IGNORE` against the table's own `(season_id, player_id, net_date)` primary key, so awarding the same net twice for the same player is a no-op that returns 409 "already credited" rather than double-paying (`app/admin_ops.py:500-509`). **No confirmation dialog.** There is no corresponding "un-award" route — reversing a mistaken award means editing the database directly. Use it only when the feed genuinely dropped somebody, never to hand out extra points; nothing distinguishes an honest correction from an inflated one once it's in the table.

**Register check-in name** — `POST /api/admin/checkin/binding` (`app/admin_ops.py:515-558`). Needed only for a MeshCore player whose radio has never appeared in the mwmesh directory — there's no public key to match them on, so a hand-registered sender name is the only way their net messages can ever resolve to them (`app/admin_ops.py:517-524`). Sending an empty name removes the binding. Adding one for the wrong player achieves nothing (the poller still matches by name), rather than stealing someone else's check-ins — so this needs no confirmation. Reached most directly from the Overview page's `checkin_unreachable` rows (`frontend/admin.js:216-228`), which pre-fill the player.

## Nets

Everything here lives under `checkin_net` (one row per weekly net, any connector) plus a single `checkin_config` row that applies to every net at once.

**Global settings** — `POST /api/admin/checkin/config` (`app/admin_ops.py:972-1044`): whether the poller runs at all, points per check-in, streak bonus and its cap, and the poller's own polling/directory-refresh intervals. Takes effect on the poller's very next cycle — it reads this table fresh every cycle, never `settings.py` (`app/admin_ops.py:974-978`). No confirmation; changing `points`/`streak_bonus` never rewrites an award already on the books, since those values are copied onto each `mc_checkin_award` row at the moment it's earned.

**Add / edit a net** — `POST /api/admin/checkin/nets/create` / `.../update` (`app/admin_ops.py:826-924`), sharing validation in `_validate_net_fields` (`app/admin_ops.py:564-727`). A net picks a connector kind — CoreScope or Beacon (MeshCore, channel-scoped) or Meshview or MQTT (Meshtastic, hashtag-scoped) — and the scoring `protocol` is *derived* from that kind, never accepted independently, so a net's connector and the board it feeds can never disagree (`app/admin_ops.py:585-609`). MQTT's `broker_password` and `channel_key` are secrets: a blank submission on edit means "keep the current value," never "clear it," because the GET route never echoes a real secret back into the form — a "Clear" checkbox is the only way to actually blank one out (`app/admin_ops.py:676-716`, `frontend/admin.js:1029-1040`). No confirmation on either save.

**An empty start date on a net blocks every award for it — not "no lower bound."** Both feeds hand back their own history on every poll, so a freshly added net with a blank start date would otherwise retroactively award every past message still visible upstream, for whoever happens to be registered today (`app/checkin.py:247-256`). Set it to today (or the date the net should start counting from) the moment you add a connector — the admin form says as much directly under the field (`frontend/admin.html:183`).

**Delete a net** — `POST /api/admin/checkin/nets/delete` (`app/admin_ops.py:927-969`). Requires typing the net's exact label. Deletes only the `checkin_net` row — `mc_checkin_award` carries no `net_id` at all, so a net's historical awards (keyed on season/player/net_date/protocol) are untouched by removing the net that produced them (`app/admin_ops.py:935-938`).

## Paint

Controls which upstream source paints live Meshtastic territory, plus FreqMapper's own connector and scoring knobs. Meshview keeps supplying node names and roster regardless of which source is painting.

**Switch paint source** — `POST /api/admin/paint` (`app/admin_ops.py:1211-1334`) with `mt_paint_source` set to `meshview` or `freqmapper`. The backend itself has no way to tell an intentional switch from a typo — it enforces shape only — so the admin UI itself gates the switch with a typed confirmation: type "FreqMapper" or "Meshview" (whichever you're switching *to*) before the save fires (`app/admin_ops.py:1216-1222`, `frontend/admin.js:843-862`). Changing any other field on this form (connector URL, poll interval, scoring) with the source left alone needs no such prompt. Takes effect on the next poll cycle for both pollers — no restart.

`api_key` follows the same blank-means-keep-current convention as the net secrets above; `clear_api_key` blanks it explicitly (`app/admin_ops.py:1293-1307`).

**Clear FreqMapper cursor** — `POST /api/admin/paint/clear-cursor` (`app/admin_ops.py:1337-1366`). Deletes the stored cursor so the next poll **re-walks FreqMapper's entire verified-coverage feed from the beginning**. A real operational need, not a reset button for its own sake: when the upstream moves hosts, a cursor issued by the old backend may not resolve against the new one at all, and polling would then either error or silently never advance (`app/admin_ops.py:1339-1346`). It is safe because deduplication runs on `verification_id` *before* anything else touches an event — every event this deployment has ever looked at, painted or not, is already recorded in `freqmapper_verification`, so an already-seen event coming back around after a clear is a no-op, never a replay or a double-score (`app/admin_ops.py:1347-1354`). Safe, but slow: re-walking the entire feed from zero is not instant, and it's the reason this needs its own explicit action rather than happening automatically. Requires typing "CLEAR" (`frontend/admin.js:897-908`).

## Seasons

**Extend a season** — `POST /api/admin/season/extend` (`app/admin_ops.py:421-461`). Moves a running season's `ends_at`. Changing `settings.season_days` in config only affects the *next* season, since `ends_at` is written onto the row when it opens — this route is what actually changes the season people are currently playing, and it's how the current six-month seasons were set (`app/admin_ops.py:423-429`). Refuses to set an end date in the past, since shortening one that way would end the season on the very next poll and crown a winner (`app/admin_ops.py:431,444-447`). No confirmation dialog (`frontend/admin.js:266-279`) — just an "extend by N days" field and Apply.

## Months

**Recompute and freeze** — `POST /api/admin/month/freeze` (`app/admin_ops.py:1472-1509`), calling `results.freeze_month` (`app/results.py:745-766`). Months close on their own from ordinary traffic; this route is for the two cases that don't: a month that closed while history was wrong and needs recomputing, or a month nothing has closed for because the service was down across the boundary (`app/admin_ops.py:1477-1479`). Refuses the month currently in progress — "a result that can still change is not a result" (`app/admin_ops.py:1481-1482,1497-1498`).

**This overwrites an already-frozen result, silently, with no warning in the API or the UI.** `freeze_month` does `INSERT OR REPLACE INTO month_result`, deletes and rewrites every `month_standing` row for that month/protocol, and deletes and rewrites every `month_award` row the same way (`app/results.py:748-765`). Running it a second time on a month that's already frozen doesn't ask "are you sure you want to recompute this" — it just recomputes and replaces, which is exactly the intended behavior for the "history was wrong" case, but means there's no version history and no confirmation standing between an operator and quietly changing a published result. No confirmation dialog in the UI at all (`frontend/admin.js:554-568`).

## Places

**Preview rotation draw** — `GET /api/admin/places/preview` (`app/admin_ops.py:327-394`). Shows what Places Worth Going's weekly rotation would draw for a given week (or the current week if left blank), without persisting anything. The real draw for the current week is still resolved lazily, the first time a scoring ping or the places API actually needs it (`app/admin_ops.py:329-334`). Purely read-only — there is nothing here to break.

## Notice

**Save / clear the one-time notice** — `GET`/`POST /api/admin/notice` (`app/admin_ops.py:1369-1469`). A singleton row shown to players once on their first map load after it changes. Re-saving the same `version_key` updates what's already published (fixing a typo) without re-showing it to anyone who already dismissed it — only a *new* `version_key` does that, because the key is the exact string the player-facing localStorage dismissal check compares against (`app/admin_ops.py:1406-1410`). "Stop showing to players" resends whatever is currently saved with `active` forced to `false`, so retiring a notice never requires retyping title and body first (`frontend/admin.js:1359-1363`). No confirmation on either action; saving always replaces whatever was there before.

## API keys

Read-API keys for the public `/api/v1` surface — a completely separate table (`api_client`) from a player's `api_key`, deliberately, so a key that can read the public feed can never be used to post wardriving data (`app/admin_api.py:840-846`).

**Issue** — `POST /api/admin/api-clients/create` (`app/admin_api.py:880-921`). Requires a label (what it's for, e.g. "freq51 discord bot") — the raw key is shown exactly once, in this response only, and can never be retrieved again since only its hash is stored. No confirmation needed to create one.

**Revoke** — `POST /api/admin/api-clients/revoke` (`app/admin_api.py:924-960`), matched by a hash prefix of at least 8 characters. Sets `revoked_at`; the row is kept (never deleted) so the label, issue date, and usage stay visible afterward — a revoked key that vanishes leaves an operator unable to tell what it was or whether it's already been dealt with (`app/admin_api.py:930-933`). Takes effect within about a minute, since `app/public_api.py` caches authentication for that long. Guarded by a plain `window.confirm()` (`frontend/admin.js:1220-1227`), not a typed name — lower stakes than a player key, since revoking a read-only integration key breaks nothing but that integration's own reads.

---

## Key recovery

The single most common operator request, and the one where reaching for the wrong tool breaks a working radio for no reason. Three paths, in the order to actually try them:

1. **Have the player read it back from MeshMapper.** MeshCore players usually can read their own key back out of MeshMapper's own settings screen — it's stored there in plain form for the app's own use. If they can, this needs no admin action at all. Try this first before touching anything.
2. **Issue an extra key** (`POST /api/admin/player/issue_key`) — the right tool when the player simply mislaid their key (lost the paper it was written on, wiped a device) and their existing MeshMapper config or anything else using the old key is still fine and should keep working. This is additive: nothing about their current setup changes.
3. **Revoke & reissue** (`POST /api/admin/player/reissue`) — reach for this only when the old key must actually stop working, i.e. "someone else has my key," not just "I lost my key." It breaks the player's current MeshMapper config (or anything else holding the old key) the instant it runs, and they must reconfigure with the new one before anything works again.

The failure mode to avoid: a player says "I lost my key" and an operator reaches for Revoke & reissue out of habit, breaking a MeshMapper setup that was working fine and didn't need to change. Issue extra key is almost always the right answer to "I lost my key"; Revoke & reissue is for when the old key is a liability, not merely misplaced.

## Deployment traps

Configuration cases that fail silently — no error anywhere, just a deployment that quietly doesn't do what a reader of the code alone would expect.

- **`checkin_enabled: true` with an empty `checkin_net_start_date` awards nothing, with no error anywhere.** Empty is deliberately treated as "block every net," never "no lower bound" — the same contract `mc_checkin_base_url` and `join_invite_code` use for "empty means off, never open" (`app/config.py:478,483-489`; enforced per-net in `app/checkin.py:249-256`). Turning check-ins on is not enough by itself; the start date has to be set too, or the poller runs, finds messages, and silently awards none of them.
- **`join_meshtastic_enabled` defaults to `false`**, so a fresh deployment's join page offers Meshtastic registration as "Coming soon" (`frontend/join.html:139,143`) — while `about.html` unconditionally states Meshtastic is "Live" and "Playable now" (`frontend/about.html:180-181`). The two pages disagree on a default install: a new deployment tells players Meshtastic works before the operator has actually turned registration on for it (`app/config.py:324-332`, gated in `app/join_api.py:192-197`).
- **`MAX_HOPS` is documented as `99` in `app/config.py:51`** ("accept positions where hop_start - hop_limit <= max_hops") **but ships as `0` in `docker-compose.yml:24`** (`MAX_HOPS: "${MAX_HOPS:-0}"`). A value of `0` disables the hop filter entirely rather than restricting to zero-hop positions — anyone reading `config.py` alone, without also checking the compose file's own default, will get this backwards.
- **An empty `admin_token` disables the entire admin surface**, not just authentication on it — `/admin` 404s and every `/api/admin/*` route 404s rather than 401ing, indistinguishable from the admin door never having been built (`app/config.py:334-340`, `app/admin_api.py:69-70,82-83`). A deployment with no token set has no admin panel at all, not an admin panel anyone could stumble into.
