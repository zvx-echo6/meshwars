# Changelog

Notable, player-facing changes. Plain language, newest first.

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
