// =====================================================================
// frontend/play-area-map.js -- the "Where it's played" illustration.
//
// Draws the CONFIGURED play area (app/config.py's play_area_*, served by
// /config alongside the map's own centre/zoom) as a rectangle on a small
// Leaflet map. Used by the About and Join pages, which are otherwise
// plain text; the main map page does not use this -- it already draws
// the real board.
//
// Read from /config rather than written into the HTML on purpose. The
// bounds are an operator setting that moves: widening them is a one-line
// .env change plus a restart, and a hardcoded copy in two static pages
// is exactly the kind of thing that silently disagrees with the server
// a month later. Nothing here knows which states are in the box.
//
// Deliberately not an interactive map. Dragging, scroll-zoom and
// keyboard panning are all off, so a reader scrolling the page past this
// panel on a phone never gets their scroll captured by it. It is a
// picture of a rectangle, and the link under it goes to the real map.
//
// Fails silently and completely: if /config is unreachable, malformed,
// or Leaflet did not load, the container is removed from the layout
// rather than left as an empty dark box, so the panel degrades to the
// sentence above it.
// =====================================================================
(() => {
  const el = document.getElementById('play-area-map');
  if (!el) return;

  const hide = () => { el.style.display = 'none'; };

  if (typeof L === 'undefined') {
    hide();
    return;
  }

  const finite = (v) => typeof v === 'number' && Number.isFinite(v);

  (async () => {
    let pa;
    try {
      const res = await fetch('/config');
      if (!res.ok) return hide();
      const cfg = await res.json();
      pa = cfg && cfg.play_area;
    } catch {
      return hide();
    }

    if (!pa || !finite(pa.north) || !finite(pa.south) ||
        !finite(pa.west) || !finite(pa.east)) {
      return hide();
    }

    // north == south is app/grid.py's in_play_area() disable sentinel:
    // the check is off and there is no area to draw.
    if (pa.north === pa.south) return hide();

    const bounds = L.latLngBounds([pa.south, pa.west], [pa.north, pa.east]);

    const map = L.map(el, {
      zoomControl: false,
      attributionControl: true,
      dragging: false,
      scrollWheelZoom: false,
      doubleClickZoom: false,
      boxZoom: false,
      keyboard: false,
      touchZoom: false,
      tap: false,
    });

    // Same basemap as the board (frontend/mc.js) so the two read as one
    // site rather than two different maps that happen to share a nav bar.
    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/">CARTO</a>',
    }).addTo(map);

    L.rectangle(bounds, {
      color: '#A9885B',        // --mw-gold; Leaflet paints to canvas, not
      weight: 2,               // CSS, so the token cannot be used directly
      fillColor: '#A9885B',
      fillOpacity: 0.12,
      interactive: false,
    }).addTo(map);

    map.fitBounds(bounds, { padding: [12, 12] });

    // The container is sized by CSS, and on these pages it can still be
    // settling (web font swap shifting the panel) when L.map() measures
    // it. Re-measure once the layout pass is done, then re-fit, so the
    // rectangle is never drawn against a stale container size.
    setTimeout(() => {
      map.invalidateSize();
      map.fitBounds(bounds, { padding: [12, 12] });
    }, 120);
  })();
})();
