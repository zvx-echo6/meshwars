---
title: The Long Season
status: shipped (August 2026)
---

# The Long Season

## Seasons

Seasons were 30 days; they are now 180. The reason a longer season works is that score decay already does the job a wipe was doing: at 0.25 points per day, a square painted once and never revisited falls to zero within weeks, so held ground always means recently active. A wipe every 30 days landed as a punishment on whoever was doing best — the team playing well got reset along with everyone else. Both live seasons were extended in place rather than being allowed to expire, because letting them run out would have meant one more wipe before the new rule took effect.

## Monthly results

A six-month season leaves five months with nothing to show for it, so each calendar month closes with its own standings and honors on the `/results` page. Two rules matter here:

- A month is judged when it ENDS, not as it goes. Nothing is shown for the month in progress except when it closes, because an award you can watch changing hands daily is not an award.
- A month is scored ON the month — ground taken and points earned between its own start and end dates, never a snapshot of season standings — because a snapshot would name the same leader every month.

Months are calendar months in the net's timezone (America/Boise), not offsets from a season start, because the two boards began on different days and offsets would have drifted apart. Ties are refused rather than split: if two players share a lead, nobody gets that honor that month.

## Streaks

Checking in to the weekly net is worth 25 points. A bonus starts on the second consecutive net and caps after the sixth:

| Consecutive net | 1 | 2 | 3 | 4 | 5 | 6+ |
|---|---|---|---|---|---|---|
| Points | 25 | 30 | 35 | 40 | 45 | 50 |

Two rules make this fair. A Wednesday only counts as a net if at least one person checked in, so a week nobody ran the net cannot break everyone's run. And a streak survives a season boundary — it is a record of showing up, not territory — while the points it earns reset with the season. Streaks are counted per board.

## The honors

Each month awards:

- **Month Winner** — team with the most points gained
- **Top Attacker** — most squares taken off other teams
- **Top Defender** — most squares won back (a capture counts as a defence when the previous capture of that same square took it from the team now taking it back)
- **Team Attacker** and **Team Defender** — the same two, given within each team
- **Tourist** — most landmarks visited
- **Park Hopper** — most parks visited
- **Peak Tagger** — most summits visited
- **Frontier**
- **Quick Fingers** — fastest average check-in after the net opened

## Naming

The check-in activity was briefly called Netrunners, which is an active card-game trademark, then Phreaks, and is now NetOps. The rankings vocabulary is unified across both boards: the button says Top Operators, the two tabs are Wardrivers and NetOps. The award is Top NetOp, singular.

## Two guards, both deliberately narrow

Squares claimed while moving faster than about 100 mph are marked as airborne and excluded from the exploration honors — but they still count as territory, because the radio genuinely reached those repeaters and that is what territory measures. An implausibly large jump is treated as a bad GPS fix rather than a flight. Nothing catches a hovering aircraft; that is a known gap.

Separately, Quick Fingers is an award for speed on a scheduled event, which invites a cron job, so a player whose check-in timing barely varies across enough nets is quietly skipped for that one award — not penalised, and nothing else is affected.

## Decisions reversed during the build

Worth recording so they are not undone:

- `/results` showed the month in progress live, and that was removed.
- A "Team of the Month" award existed alongside Month Winner and was deleted, because the two named the same team almost every month.
- Frontier measured the single furthest square and became a count of squares instead.
- "Most Consistent" (longest run of nets) was dropped 2026-08-25. A month is about four nets, and nearly everyone who shows up hits all four, so it was a tie among most of the field and told you nothing — and streak points already reward showing up every week. Months frozen before it came down keep the winner they recorded.
- "Top NetOp" (most check-in points) was dropped 2026-08-25. The streak bonus pays 5 points per consecutive week, capped at 25, so whoever started their streak earliest pulls ahead by a gap a newcomer cannot close in a single month — a record of seniority, not a contest. Players still earn streak points; only the award for topping them is gone. Months frozen before it came down keep the winner they recorded.
- "Explorer" (most Places Worth Going points earned that month) was dropped 2026-08-25 and replaced with Tourist, Park Hopper, and Peak Tagger. Points are capped at 100 per person per week, so everyone who played seriously finished a month within the same narrow band — the award separated nobody, the same ceiling that retired Most Consistent. The three replacements count visits instead of points, one per place type, and ignore point values entirely; the weekly cap makes them mutually exclusive in practice, since one remote summit alone spends the whole week, so each describes a different way to play rather than a different view of the same grind. Months frozen before it came down keep the winner they recorded, and the Explorer label is kept on file so those months still display a real name.
