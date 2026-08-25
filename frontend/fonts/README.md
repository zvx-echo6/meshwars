# Self-hosted map glyphs

`Noto Sans Regular/*.pbf` are pre-built MapLibre glyph-range PBFs (Signed
Distance Field font atlases) for the `text-font: ['Noto Sans Regular']`
place-name labels on `/map2` (see `frontend/map2.js`'s `setupPlacesLayer`
and the style's `glyphs` template). Serving them from our own `/static`
mount, the same way every other frontend asset is served, removes the
labels' only runtime dependency on `demotiles.maplibre.org`.

**Source:** built by [openmaptiles/fonts](https://github.com/openmaptiles/fonts)
(Noto Sans, "patched by Klokan Technologies" to widen its Unicode
coverage), vendored as pre-built PBFs by
[maplibre/demotiles](https://github.com/maplibre/demotiles) at
`font/Noto Sans Regular/`. Fetched from there directly rather than
rebuilt locally -- fontnik's build toolchain is a much heavier pipeline
than six range files justifies.

**License:** SIL Open Font License 1.1 (`Noto Sans Regular/LICENSE.txt`,
copied from openmaptiles/fonts' `noto-sans/LICENSE`). The OFL permits
redistribution, including as a converted glyph atlas, as long as the
license text travels with it -- which is why `LICENSE.txt` sits next to
the `.pbf` files rather than only being linked from here.

**Only 6 of the full 256 range files are vendored**, not the whole
~33 MB set: `0-255`, `256-511`, `512-767`, `768-1023` (Basic Latin,
Latin-1 Supplement, Latin Extended-A/B -- covers plain English and
accented European place names), `7680-7935` (Latin Extended Additional
-- Vietnamese diacritics), and `8192-8447` (General Punctuation -- smart
quotes, en/em dash, middle dot, all of which appear in
`app/reference/places_worth_going.csv` names). A handful of OSM
landmark names in that CSV use CJK characters outside these ranges;
their labels fall back to no glyph for those characters rather than
pulling in the dozens of additional range files (and the CJK glyph data
that makes the full set so much larger) that would be needed to cover
them. If that trade-off ever needs revisiting, the missing ranges can be
fetched the same way these were:
`https://raw.githubusercontent.com/maplibre/demotiles/gh-pages/font/Noto%20Sans%20Regular/<range>.pbf`.
