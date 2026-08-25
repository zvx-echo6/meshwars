---
title: Places Worth Going
status: built on feat/places, not deployed — schema, seed, rotation, scoring, API, map markers and panel, admin preview all land there; CT 119 (production) still runs without it
---

# Places Worth Going

What follows is the design conversation written down so it does not have to be had again, plus what the build actually does where that differs from the plan.

## The idea

Every square is currently worth the same; the map is graph paper with no features. Named places — summits, parks, landmarks — are destinations worth more than the cell they sit in. None of the lists need inventing: Summits on the Air and Parks on the Air are curated ham-radio programmes, and landmarks come from OpenStreetMap.

## Values and the cap

| Reference type | Source | Points |
|---|---|---|
| Landmark | OpenStreetMap | 5 |
| Park | POTA | 25 |
| Summit | SOTA | 100 |

A park is deliberately worth exactly one check-in — that is the anchor. Twenty landmarks, four parks, or one summit all reach the same weekly ceiling of 100 points, so the choice is how you would rather spend the week rather than which nets more.

## Rules

- One credit per reference, per person, per week.
- 100 points per person per week, whatever the mix.
- The week resets Wednesday just before the net, so the game has one clock — the gathering and the reset are the same moment.
- Activating requires a scoring ping; being there is not enough, exactly as it is not enough anywhere else.
- Points go to a personal Explorer Score AND to the team total, the same shape check-ins already have.
- Aircraft are excluded.

## The seed, as actually pulled

Sources ended up simpler than "a narrow OpenStreetMap tag list" alone implied: summits are SOTA's own list, parks are POTA's, and landmarks are OpenStreetMap filtered to the tag list below — all three pulled 2026-08-24 (`scripts/build_places_seed.py`, `app/reference/places_worth_going.csv`, 65,011 raw rows).

**Landmark tags kept:** town hall, courthouse, library, museum, viewpoint, attraction, visitor centre, memorial, monument, historic marker, trailhead. Fire station and post office were cut before this landed — nobody drives to one.

**The seed is not pre-filtered to the US.** SOTA and POTA were both pulled over one bounding box (49.29N/25.8S/-125W/-93.5E) that, being a rectangle, also swept in northern Mexico and southern Canada. The loader (`app/places_seed.py`) excludes those at load time, not by re-pulling the CSV:

- **Summits:** by SOTA association code (confirmed against SOTA's own `/api/associations/` endpoint) — `XE2` (Mexico - North, 7,246 summits) and `VE5`/`VE6`/`VE7` (Saskatchewan/Alberta/British Columbia, 703 summits) excluded. `K0M` looks like an odd one out next to the W-prefixed codes but is genuinely USA - Minnesota, not a typo.
- **Parks:** by POTA's own reference prefix — `CA-` (328) and `MX-` (70) excluded; `US-` kept.
- **Landmarks:** not filtered — every row's coordinates fall inside the real US-Mexico and US-Canada borders already (the OSM extract they came from is western-US-only), verified rather than assumed.

Kept after the country filter: **26,600 summits, 5,293 parks, 24,771 landmarks.**

**Park boundaries are not fully matched.** POTA publishes centre points only; a boundary is matched from PAD-US afterward (`scripts/build_places_seed.py`'s `match_parks()`). Of the 5,293 US parks, **3,465 matched a boundary and 1,828 did not (65.5%)**. An unmatched park is not treated as small — it scores like a summit, its point's own square, always active, never rotating. Of the matched parks, 3,184 are at or above one grid cell in area (score by the >50% rule, always active) and 281 are smaller than one cell (score their point's square, and rotate like a landmark — see below).

## Containment: which square scores

- **Landmark, summit:** the single square the place's own point falls in.
- **Park at or above one grid cell, boundary matched:** any square more than 50% inside the boundary.
- **Park below one grid cell (matched or not), or boundary unmatched:** the square containing its point, same as a landmark.

## Weekly rotation (decided 2026-08-24, retuned 2026-08-25)

Summits and boundary-backed parks — anything that does not rotate above — are **always active**. Landmarks and small parks **rotate weekly**, flipping on the same Wednesday reset as everything else, so a town's handful of live places changes from week to week rather than every landmark within reach being live all the time.

**The draw is deterministic, not random-per-player.** It is seeded from the week's own identifier (`app/place_rotation.py`, `hashlib.sha256("places-rotation:<week_start>")` — not Python's `hash()`, which is process-randomized and would not agree between two servers or two restarts). Every player sees the same live set for a given week, and the same week computed twice — on any machine, at any time — produces the identical draw. Computed once and cached in `place_week` the first time anything needs it that week (a scoring ping or a map request), not recomputed on every read.

**Two constraints on the draw:**

- **1-mile minimum spacing** (`MIN_SPACING_MILES`). No two live rotating places within 1 mile of each other, checked globally (not just within one region cell — two candidates a short walk apart but on opposite sides of a cell boundary must not both get picked).
- **Per-region quota.** The play area is divided into region cells sized **18 miles** on a side (`ROTATION_CELL_MILES`), each allowed up to **15 live rotating places** (`ROTATION_QUOTA_PER_CELL = 15`). A cell with fewer candidates than the quota gets all of them; a cell with many (a city downtown) fills more of its quota, up to what the 1-mile spacing floor still allows once that many are competing for room.

**Retuned 2026-08-25 against a real complaint** ("I shouldn't have to travel more than 10 minutes between spaces… entire towns have 4 available with the next closest 45 minutes away", and separately "even in larger cities I can't find the places" / "very very few landmarks and local parks"). The 2026-08-24 tuning (quota 1→5) had been measured against a preview whose play area was misconfigured to roughly Idaho and northern Utah — about a tenth of the real board — and against "does Twin Falls get more than one place" rather than against how a player actually experiences density.

Re-measured on the real play area (49.29N/25.8S/-125W/-93.5E, 61,563 active rotating candidates: 30,408 landmarks + ~31,155 small parks) against the yardstick that matches the complaint: **from any populated place, do the live ROTATING places (landmarks + small parks only — summits and boundary-backed parks don't count here) within a 40-mile radius (Twin Falls to Burley, a real answer to "how far would I actually drive") add up to at least 100 points (one week's per-person cap)?** Measured against the 12,136 real town anchors inside the play area in `app/reference/places.csv` (the file also carries a 1-degree margin strip with no seed data in it — those margin rows read as false zero-supply and are excluded from this measurement).

| | quota 5 / spacing 3mi (old) | quota 15 / spacing 1mi (current) |
|---|---|---|
| live rotating places nationwide | 8,303–8,457 | 16,267 |
| towns reaching 100 rotating-tier points within 40mi | 84.5% (10,250/12,136) | 91.3% (11,080/12,136) |
| … + boundary-backed big parks as fallback for the rest | — | +7.6% (927 more towns) |
| still short even with the big-park fallback | — | 1.06% (129 towns) — real empty country: Big Bend, the Dakota/Montana high plains, the Nebraska Sandhills |

Sweeping quota alone (spacing held at 3mi) moved the pass rate only 84.8%→84.9% before flattening completely at quota 25 — quota was not the real constraint. Sweeping spacing alone (quota held at 5) moved it 84.8%→93.3% at 0.5mi — spacing was. Of the candidate-slots the old 3-mile rule discarded, roughly 79% were in cells with 10 or fewer candidates to begin with: it was thinning already-thin rural clusters, not "crowded cities" as intended. `ROTATION_CELL_MILES` (18mi) was not changed — cell size was never the measured problem.

**Repeats are a fallback, not a preference.** Last week's picks are sorted to the back of their cell's candidate list; a place only repeats if nothing else in its cell clears the spacing check against everything already chosen elsewhere.

**Preview without committing:** the admin panel's Places section (`app/admin_ops.py`'s `/api/admin/places/preview`, `frontend/admin.html`/`admin.js`) runs the same draw for any week — including the current one, or a hypothetical future one — without writing to `place_week`, so an operator can sanity-check density (candidates per cell, densest cells, a sample of the draw) before or long after a week actually happens.

## Why per person rather than per team

It matches the existing rule that the first new person to paint a square earns extra, because more people beats one person going back and forth.

## Why the weekly cap does the work

- It keeps check-ins relevant: a full week of places is four check-ins, not ten.
- It stops landmark density deciding anything, because a city grinder and a mountaineer reach the same ceiling.
- It keeps the summit attractive, since one trip beats twenty town halls.

## Seeding is the real balance knob, not the point values

A narrow OpenStreetMap tag list, not every point of interest.

**In (as actually pulled — see "The seed, as actually pulled" above):** town hall, courthouse, library, museum, viewpoint, attraction, visitor centre, historic marker, monument, memorial, trailhead.

**Out:** fire station and post office (cut before the seed landed — nobody drives to one), schools, hospitals, churches, playgrounds, anything on private land, anything you would not tell a stranger to drive to at night.

The test is permanent, publicly accessible, distinctive. Narrow is the recoverable direction — adding tags later gives people new places, pruning later takes credits from people who already earned them.

## Seed first, submissions maybe never

The lesson from Ingress and Pokémon Go is that Niantic seeded from existing databases first and opened player submission years later, with players reviewing each other. User submission makes the operator the referee: someone submits their own driveway, or a real place on private land, and the game starts telling people to trespass.

## What it changes about the honors

Explorer becomes most Explorer points that month, instead of most squares nobody had ever claimed. Frontier keeps counting squares beyond city limits but drops its virgin-ground restriction, which only existed so Frontier would be a strict subset of Explorer. The two then measure different things: Frontier counts ground out past the towns, Explorer counts destinations reached.

**Built so far:** a place credit (`place_activation`) adds to the team total (`app/mc_scoring.team_place_points`, folded into `team_totals()`) and to a personal running figure (`explorer_points` on each player row in `/api/v1/players`), both scoped to the season by `awarded_at` falling inside it. **Not yet built:** `app/results.py`'s monthly Explorer/Frontier award definitions still read the old virgin-square logic — redefining Explorer as "most Explorer points that month" and dropping Frontier's virgin-ground restriction is a real change to two existing, currently-correct award computations, and was left alone rather than rewritten under the same pass that built the scoring path, per the brief's "do not change existing scoring behaviour" and "if genuinely ambiguous, stop." That redefinition is still open work.

## Considered and dropped

- An "infrastructure" award for distinct repeaters personally heard.
- Keeping Explorer points out of the team total entirely.
- Making the check-in a multiplier on the week's places — rejected because a bonus for showing up is a penalty for not, and the person out of signal on Wednesday is the one who played hardest.
- Scaling park credit by park size — rejected because SOTA and POTA themselves do not; a pocket park and a wilderness both count as one activation.
