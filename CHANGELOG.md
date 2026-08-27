# Changelog

Notable, player-facing changes. Plain language, newest first.

## 2026-08-27 — The Layers box gets out of the way

**You can put the Layers panel away.** The layer switcher in the bottom
left corner of the map now has a tab on its right-hand edge. Press it
and the panel slides off the left side of the screen, leaving just the
tab behind; press the tab again and it slides back. Worth the most on a
phone, where the panel was covering a real share of the map and could
not be dismissed at all.

Your choice is remembered on that device, so a collapsed panel stays
collapsed the next time you open the map rather than making you put it
away again on every visit.

## 2026-08-27 — Places no longer stack

**One square, one place, one credit.** A square that carries more than
one place — a landmark standing inside a large park is the usual case —
used to pay out for every live place on it from a single ping, most
valuable first. That was never intended: one trip to one square is one
errand, and it should pay once. Only the most valuable place on a
square scores now. The lesser one is dropped rather than paid alongside
it, and it is not a fallback either: if you already collected the
valuable place this week, the square pays nothing rather than handing
you the cheaper one. If the valuable place is worth more than what is
left of your weekly hundred, it still pays — just for the remainder,
capping you at the hundred instead of turning you away. Two places of
exactly equal value on one square always resolve to the same winner,
for every player and every week.

**The weekly cap caps, it doesn't refuse.** This applies whether or not
a square has a runner-up sitting on it: reach a place worth more than
what's left of your hundred for the week and it no longer turns you
away empty-handed — it pays out the rest of the budget and leaves you
at the ceiling. A player at 50 points who then reaches a 100-point peak
is credited the remaining 50, not zero. That place has used its one
credit for the week either way.

Nothing already earned changes. Past credits, past scores and frozen
months stay exactly as they are — this is forward behaviour only.

**Explorer, said properly.** Explorer is not a monthly honor and never
was resettable: it is the name of an activity and of a rankings tab
(alongside Wardrivers and NetOps under Top Operators), a season-long
points figure that counts toward your total score. The rules said so in
passing; they say so plainly now. The monthly honors built on places
count visits, not points — Tourist, Park Hopper and Peak Tagger.

## 2026-08-25 — Places Worth Going, a new map, and self-hosted terrain

**Scoring changed.** Reaching a summit, a park, or a landmark now earns
points on top of the usual square-claiming game — "Places Worth
Going." Points are effort-based, not category-based: anything inside a
town's limits (a city park, a summit you can drive to) is worth 5,
wherever it sits outside one a landmark is worth 10, a park 25, and a
summit 100. A weekly rotation cycles landmarks and small parks through
so there's always something new nearby (raised from a 5-place, 3-mile
quota to 15 places, 1-mile spacing, measured against real town
coverage rather than guessed). Trailheads and lookouts were dropped
from the landmark list — a trailhead is where a walk starts, not a
destination, and a lookout on a peak is the summit you already scored.

**Explorer and Frontier mean something different now.** Explorer used
to track squares nobody had ever claimed, which mostly duplicated
Frontier. Explorer now counts points earned from places; Frontier keeps
tracking ground taken out past the towns. Months already frozen keep
the numbers they were frozen with.

**The map changed.** The MapLibre map, previously a side-by-side
preview at `/map2`, is now the front page. The old Leaflet map is still
there for comparison at `/map-legacy`, marked so it doesn't compete
with the real page in search. Terrain, roads, and public-land overlays
are now served from the game's own archives instead of fetched live
from another project's tileset — that other project rebuilds its
archives often enough that the map was breaking (blank regions, stale
byte ranges) most times it did. Contour lines were tried and dropped:
they cost most of a map pan's frame budget to draw lines over terrain
the hillshade already shows.

## Older history

Everything before this file existed is in the commit log.
