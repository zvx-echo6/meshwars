---
title: The Long Season
status: shipped (August 2026)
---

# The Long Season

## Seasons

Seasons were 30 days; they are now 180. The reason a longer season works is that score decay already does the job a wipe was doing: at 0.25 points per day, a square painted once and never revisited falls to zero within weeks, so held ground always means recently active. A wipe every 30 days landed as a punishment on whoever was doing best — the team playing well got reset along with everyone else. Both live seasons were extended in place rather than being allowed to expire, because letting them run out would have meant one more wipe before the new rule took effect.

## Monthly results

A six-month season leaves five months with nothing to show for it, so each calendar month closes with its own standings and honors on the `/results` page. Two rules matter here:

- A month is judged when it ENDS, not as it goes. Nothing is shown for the month in progress except when it closes, because an award you can watch changing hands daily is not an award. A preview host can render the open month anyway, behind `results_preview_current_month` (default off, `app/config.py`), for looking at a month's awards before it closes; it computes read-only and never freezes anything.
- A month is scored on the ground a team HOLDS when it closes, plus the check-in points earned inside it. Squares are counted the way the scoreboard counts them (`mc_scoring.team_tile_counts`), so the two pages agree.

  This replaced counting capture events on 2026-08-31. Events were the original choice, on the reasoning that "a capture is worth one point, the same as a held square is worth one in the season total, so the two numbers stay in the same units". That reasoning was wrong twice over: a square that changed hands five times scored five, and ground a team had taken and then lost still counted for them. In August 2026 RED read 4570 against a scoreboard figure of 2898, 58% high. The original concern — that scoring on held ground makes a month a snapshot of the season, naming the same leader every time — is real and accepted; matching the scoreboard was judged worth it. `results._held_at()` reconstructs ownership at the closing instant, which is exact because a cell has no neutral state, so a capture landing after the month ended cannot change that month's result.

Months are calendar months in the net's timezone (America/Boise), not offsets from a season start, because the two boards began on different days and offsets would have drifted apart. Ties are refused rather than split: if two players share a lead, nobody gets that honor that month. Peak Tagger is the one exception — a tie there goes to whoever reached the taller summit, because two people who each tagged one peak have not done the same thing if one of them climbed 3,000 feet higher. Landmarks and parks have no comparable "harder" axis, so Tourist and Park Hopper still refuse. A tie on both count and height is refused like any other.

## Streaks

Checking in to the weekly net is worth 25 points. A bonus starts on the second consecutive net and caps after the sixth:

| Consecutive net | 1 | 2 | 3 | 4 | 5 | 6+ |
|---|---|---|---|---|---|---|
| Points | 25 | 30 | 35 | 40 | 45 | 50 |

Two rules make this fair. A Wednesday only counts as a net if at least one person checked in, so a week nobody ran the net cannot break everyone's run. And a streak survives a season boundary — it is a record of showing up, not territory — while the points it earns reset with the season. Streaks are counted per board.

## The honors

An honor that happened somewhere links to the map, which draws it on top of the board and frames it: Longest Road draws the chain itself, Frontier draws every square out past the towns with the furthest one ringed heavier, and Tourist / Park Hopper / Peak Tagger drop a point on each place visited. Largest Territory, Empire Builder and the attack honors are deliberately not linked — thousands of squares scattered across the board, where a link would either redraw the team colour the map already shows or scatter pins with no shape to them. The geometry is recomputed on request (`results.award_geometry`) and the winner is read from `compute_month()` rather than re-derived, so the map can never disagree with the page.

Every honor below is listed every month, won or not. One nobody earned shows as "not awarded" rather than vanishing — Peak Tagger was absent for all of August 2026 and read as a missing feature when in fact nobody had reached a summit. Placeholders are never stored; a frozen month records what was won, and the empty rows are added on the way out so a frozen month and a live one render alike.

Each month awards:

- **Largest Territory** — team holding the most ground when the month closes (was "Month Winner", most points gained, until 2026-08-31)
- **Longest Road** — team holding the longest unbroken chain of squares, linked on sides *or corners*, and only if it reaches `longest_road_min_squares` (300). The floor is what makes it the one honor a rival can take off you without out-scoring you anywhere: a chain that long crosses most of a valley, and a single enemy square landing in the middle of it splits the run in two. Cut a 400-square road at its midpoint and the holder still owns 399 squares but has two runs of 200 and 199 — neither clears the floor, and nobody wins it that month. Measured as the longest shortest-path across each connected patch (`results._longest_road`): exact for a chain, and on a patch with loops it can only understate, never credit a road that is not there. Deliberately not scaled by team size — it rewards a shape, so a small team that drove a highway beats a big one that filled in a city. In August 2026 RED led on 330 while holding half of GREEN's ground.
- **Empire Builder** — player holding the most ground they painted themselves, counted from the same ownership rows as the standings so a player's figure is a readable share of their team's own total
- **Top Attacker** — most squares taken off other teams
- **Top Defender** — most squares won back (a capture counts as a defence when the previous capture of that same square took it from the team now taking it back)
- **Team Builder**, **Team Attacker** and **Team Defender** — the same three, given within each team
- **Tourist** — most landmarks visited
- **Park Hopper** — most parks visited
- **Peak Tagger** — most summits visited. A square credits a summit when it is within 1.5 km horizontally **and** within 200 m of that summit's own elevation, and each square is assigned to its single nearest qualifying summit. A summit always gets the square its own peak stands in regardless of the vertical test: a 300 m square is wide enough that on a sharp peak its centre sits more than 100 m below the apex, which would otherwise leave summits untaggable by someone standing on top of them. Both halves are load-bearing. Horizontal distance alone is the wrong axis: at 8 km, 11 of the 17 Wasatch summits become claimable from a valley street 6,000 ft below, while the same 8 km still reaches only 40 of 4,851 summits statewide. And without the nearest-summit rule one hike would credit up to 18 peaks at once, since summits cluster — worse still, the runtime non-stacking rule resolves a square to its *dearest* place, which for summits means the taller neighbour. Before this (until 2026-08-31) a summit was the single square containing its peak, and in the game's whole history nobody had ever tagged one. The mapping is precomputed by `scripts/build_summit_cells.py` against the planet DEM and ships as `app/reference/summit_cells.csv`, because the app hosts have no elevation data
- **Frontier** — most squares claimed more than 20 miles past the edge of any town (`frontier_miles`); any capture out there counts, attack, retake or first claim alike
- **Quick Fingers** — fastest average check-in after the net opened, over however many timed check-ins the player has that month, down to one. It needed two until 2026-08-31, which quietly made it unwinnable for August: `message_ts` shipped between the month's two nets, so only one of them carried timings and nobody could reach two.

## Naming

The check-in activity was briefly called Netrunners, which is an active card-game trademark, then Phreaks, and is now NetOps. The rankings vocabulary is unified across both boards: the button says Top Operators, and the tabs are Wardrivers, NetOps and Explorer. The naming outlived the award it was coined for — a "Top NetOp" monthly honor existed under that name and was dropped 2026-08-25 (see "Decisions reversed" below). NetOps survives as the name of the activity and of the rankings tab, not as an award.

Explorer is the same shape. The monthly "Explorer" honor was dropped on the same date (see "Decisions reversed"), but Explorer remains the name of the Places Worth Going activity and of its rankings tab — a season-long personal points figure (`explorer_points`, `app/public_api._player_rows`) that is folded into a player's total score (`app/mc_api`'s `total_points`). Because it feeds the season total it is scoped to the season by `awarded_at` and cannot be reset or judged month by month the way an honor is; the month-scoped honors built on places count visits instead — Tourist, Park Hopper, Peak Tagger.

## Two guards, both deliberately narrow

On MeshCore, squares claimed while moving faster than about 100 mph are marked as airborne and excluded from Places Worth Going and the exploration honors — but they still count as territory, because the radio genuinely reached those repeaters and that is what territory measures. Above about 900 mph (`_GLITCH_SPEED_MPS`) the jump is treated as a bad GPS fix rather than a flight, and is not labelled at all. Nothing catches a hovering aircraft; that is a known gap. The Meshtastic path has no airborne label at all (`app/ingest.py` passes `by_air=False` unconditionally): it rejects a fix implying more than `mt_max_speed_mps` (90 m/s, ~200 mph) outright instead, and separately rejects any position carrying less than `mt_min_precision_bits` (18) of GPS precision.

Separately, Quick Fingers is an award for speed on a scheduled event, which invites a cron job, so a player whose check-in timing barely varies across enough nets is quietly skipped for that one award — not penalised, and nothing else is affected.

## Decisions reversed during the build

Worth recording so they are not undone:

- `/results` showed the month in progress live, and that was removed.
- A "Team of the Month" award existed alongside Month Winner and was deleted, because the two named the same team almost every month.
- Frontier measured the single furthest square and became a count of squares instead.
- "Most Consistent" (longest run of nets) was dropped 2026-08-25. A month is about four nets, and nearly everyone who shows up hits all four, so it was a tie among most of the field and told you nothing — and streak points already reward showing up every week. Months frozen before it came down keep the winner they recorded.
- "Top NetOp" (most check-in points) was dropped 2026-08-25. The streak bonus pays 5 points per consecutive week, capped at 25, so whoever started their streak earliest pulls ahead by a gap a newcomer cannot close in a single month — a record of seniority, not a contest. Players still earn streak points; only the award for topping them is gone. Months frozen before it came down keep the winner they recorded.
- "Explorer" (most Places Worth Going points earned that month) was dropped 2026-08-25 and replaced with Tourist, Park Hopper, and Peak Tagger. Points are capped at 100 per person per week, so everyone who played seriously finished a month within the same narrow band — the award separated nobody, the same ceiling that retired Most Consistent. The three replacements count visits instead of points, one per place type, and ignore point values entirely; the weekly cap makes them mutually exclusive in practice, since one remote summit alone spends the whole week, so each describes a different way to play rather than a different view of the same grind. Months frozen before it came down keep the winner they recorded, and the Explorer label is kept on file so those months still display a real name.
