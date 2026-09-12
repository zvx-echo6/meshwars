"""Tests for the published_at backfill-guard fix -- the 2026-09-08
production bug where the high-water-mark guard (app/freqmapper_ingest.py,
HIGH_WATER_MARK_KEY, _process_one_event) compared an event's occurred_at
against the stored mark instead of published_at, and silently stopped
passive RX painting going forward.

See app/freqmapper_ingest.py's module docstring ("occurred_at vs.
published_at") and _backfill_guard_time's own docstring for the full
mechanism and why. Short version: FreqMapper publishes every passive_rx
event at least rx_publication_delay_seconds (60s minimum) after
reception, and explicitly supports a phone uploading an OLDER OFFLINE
reception even later than that -- in both cases occurred_at (when the
radio actually heard the packet) is genuinely old while published_at
(the field the feed's own cursor is actually ordered by) is genuinely
new. verified_tx events don't have this gap, so an occurred_at-keyed
guard kept advancing the mark to "now" purely off TX traffic, and a
current, legitimate RX reception then looked like history against that
already-advanced mark and was refused forever. Measured live in
production: a fully-caught-up poll cycle (has_more=False) painted 2
verified_tx events and backfill-skipped all 5 passive_rx events on the
same page.

This file tests exactly the properties that distinguish the fix from
the bug it replaces:

  1. An event with an OLD occurred_at but a NEW published_at (the
     late-published / offline-upload case) now paints -- the whole
     point of this fix, and the one case the old occurred_at-keyed
     guard got wrong (test_offline_upload_*).
  2. An event with an OLD published_at is still backfill-skipped even
     when it looks recent by occurred_at or any other field -- proving
     the fix does not weaken what the guard protects against (a cursor
     reset or feed switch replaying old history) (test_old_published_at_*).
  3. A missing published_at falls back to occurred_at for the guard,
     rather than skipping the guard outright (test_published_at_missing_*).
  4. The paint timestamp (player_cell_ping.ts) and the paint_from date
     gate still key off occurred_at, NEVER published_at -- this is the
     regression that would silently misdate every paint if "the guard
     now uses published_at" ever got over-applied
     (test_paint_timestamp_and_paint_from_gate_*).
  5. The mark still only ever advances forward, using the guard's own
     time value (published_at, or its occurred_at fallback), never
     backward (test_mark_*).

Uses the shared in-memory `conn` fixture (tests/conftest.py) and calls
FreqMapperIngestor._process_one_event directly -- same shape
tests/test_freqmapper_backfill_guard.py and
tests/test_freqmapper_combined_feed.py already use for anything that
doesn't need the HTTP/poll-loop layer.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from app import mc_scoring
from app.db import get_cursor, set_cursor
from app.freqmapper_ingest import FreqMapperIngestor, HIGH_WATER_MARK_KEY

NOW = int(time.time())
PROTOCOL = "mt"
LAT, LON = 43.0, -116.0  # well within settings.play_area_* (see app/config.py)


# ---------------------------------------------------------------------
# fixtures / helpers -- same shapes as tests/test_freqmapper_backfill_guard.py
# ---------------------------------------------------------------------

def _seed_player_and_node(conn, player_id=1, node_ref="0a0a0a0a", team="RED"):
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (?, ?, ?, ?)",
        (player_id, f"player-{player_id}", team, NOW),
    )
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) VALUES (?, ?, ?, ?)",
        (PROTOCOL, node_ref, player_id, NOW),
    )


def _season_id(conn) -> int:
    conn.execute("BEGIN IMMEDIATE")
    mc_scoring.maybe_roll_season(conn, NOW, PROTOCOL)
    sid = mc_scoring.ensure_active_season(conn, NOW, PROTOCOL)
    conn.execute("COMMIT")
    return sid


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _tx_event(raw_id: str, occurred_at_ts: int, published_at_ts: int | None = None,
              node_ref: str = "0a0a0a0a") -> dict:
    event = {
        "event_id": f"verified_tx:{raw_id}",
        "event_type": "verified_tx",
        "verification_id": raw_id,
        "radio_node_id": "!" + node_ref,
        "latitude": LAT,
        "longitude": LON,
        "occurred_at": _iso(occurred_at_ts),
    }
    if published_at_ts is not None:
        event["published_at"] = _iso(published_at_ts)
    return event


def _rx_event(raw_id: str, occurred_at_ts: int, published_at_ts: int | None = None,
              node_ref: str = "0a0a0a0a") -> dict:
    event = {
        "event_id": f"passive_rx:{raw_id}",
        "reception_id": raw_id,
        "event_type": "passive_rx",
        "radio_node_id": "!" + node_ref,
        "latitude": LAT,
        "longitude": LON,
        "occurred_at": _iso(occurred_at_ts),
    }
    if published_at_ts is not None:
        event["published_at"] = _iso(published_at_ts)
    return event


def _process(conn, ingestor, event, season_id, registered, *, allow_backfill=False,
             paint_from="2020-01-01"):
    return ingestor._process_one_event(
        conn, event, season_id, registered, NOW,
        "both", 1.0, 0.5, paint_from,
        allow_backfill=allow_backfill,
    )


# ---------------------------------------------------------------------
# 1. Old occurred_at, new published_at (the offline-upload / RX-lag
#    case) -- the whole point of this fix.
# ---------------------------------------------------------------------

def test_offline_upload_old_occurred_at_new_published_at_paints(conn):
    """A passive_rx event whose radio heard the packet well before the
    stored mark (old occurred_at), but which FreqMapper only just
    published (new published_at, after the mark) -- e.g. a phone
    uploading an offline reception -- must paint. Under the old
    occurred_at-keyed guard this was exactly the case that got silently
    refused forever.
    """
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    mark = NOW - 3600
    set_cursor(conn, HIGH_WATER_MARK_KEY, str(mark))

    old_occurred = mark - 90 * 86400  # heard 90 days before the mark
    new_published = mark + 60          # but only just published, after the mark
    event = _rx_event("offline-upload-1", old_occurred, new_published, node_ref)

    outcome = _process(conn, ingestor, event, season_id, registered)

    assert outcome == "painted_rx"
    rows = conn.execute("SELECT count(*) FROM player_cell_ping").fetchone()[0]
    assert rows == 1

    # The mark advances to the event's published_at, not its occurred_at.
    assert get_cursor(conn, HIGH_WATER_MARK_KEY, "") == str(new_published)


def test_offline_upload_verified_tx_variant_also_paints(conn):
    """Same case, verified_tx side -- the guard is shared by both event
    types (ONE mark protects both), so a verified_tx event with the same
    old-occurred_at/new-published_at shape must behave identically.
    """
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    mark = NOW - 3600
    set_cursor(conn, HIGH_WATER_MARK_KEY, str(mark))

    old_occurred = mark - 86400
    new_published = mark + 30
    event = _tx_event("late-tx-1", old_occurred, new_published, node_ref)

    outcome = _process(conn, ingestor, event, season_id, registered)

    assert outcome == "painted"


# ---------------------------------------------------------------------
# 2. Old published_at is still backfill-skipped, even if the event
#    looks recent by other fields -- the cursor-reset case, unweakened.
# ---------------------------------------------------------------------

def test_old_published_at_is_backfill_skipped_even_if_occurred_at_looks_recent(conn):
    """A cursor reset or feed switch replays events with OLD published_at
    values -- that is precisely what "already processed, per the feed's
    own ordering" means, and the guard must still catch it, even for an
    event whose occurred_at (or any other field) makes it LOOK like
    current traffic.
    """
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    mark = NOW
    set_cursor(conn, HIGH_WATER_MARK_KEY, str(mark))

    # occurred_at is essentially "now" -- looks like a fresh, current
    # event by the RF-event clock -- but published_at is well before the
    # mark, exactly what a replayed/backfilled historical page looks
    # like on the feed's own ordering.
    recent_occurred = NOW
    old_published = mark - 7200
    event = _rx_event("stale-replay-1", recent_occurred, old_published, node_ref)

    outcome = _process(conn, ingestor, event, season_id, registered)

    assert outcome == "backfill_skipped_rx"
    rows = conn.execute("SELECT count(*) FROM player_cell_ping").fetchone()[0]
    assert rows == 0

    # Still recorded so it is never re-evaluated on a later poll.
    seen = conn.execute(
        "SELECT count(*) FROM freqmapper_verification WHERE verification_id = ?",
        ("stale-replay-1",),
    ).fetchone()[0]
    assert seen == 1

    # The mark must not have moved.
    assert get_cursor(conn, HIGH_WATER_MARK_KEY, "") == str(mark)


# ---------------------------------------------------------------------
# 3. published_at missing falls back to occurred_at for the guard.
# ---------------------------------------------------------------------

def test_published_at_missing_falls_back_to_occurred_at_for_guard_skips_old(conn):
    """An event with no published_at at all (an older type-specific
    feed shape, per this module's docstring) must not bypass the guard
    outright -- it falls back to occurred_at, reproducing the guard's
    pre-fix comparison for exactly this event rather than leaving it
    unprotected.
    """
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    mark = NOW
    set_cursor(conn, HIGH_WATER_MARK_KEY, str(mark))

    old_occurred = mark - 3600  # older than the mark, no published_at at all
    event = _tx_event("no-published-at-old-1", old_occurred, published_at_ts=None, node_ref=node_ref)
    assert "published_at" not in event

    outcome = _process(conn, ingestor, event, season_id, registered)

    assert outcome == "backfill_skipped"
    assert get_cursor(conn, HIGH_WATER_MARK_KEY, "") == str(mark)


def test_published_at_missing_falls_back_to_occurred_at_for_guard_paints_new(conn):
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    mark = NOW - 7200
    set_cursor(conn, HIGH_WATER_MARK_KEY, str(mark))

    new_occurred = mark + 60  # newer than the mark, no published_at at all
    event = _tx_event("no-published-at-new-1", new_occurred, published_at_ts=None, node_ref=node_ref)
    assert "published_at" not in event

    outcome = _process(conn, ingestor, event, season_id, registered)

    assert outcome == "painted"
    # The mark advances to occurred_at, the fallback value, since there
    # was no published_at to advance it to instead.
    assert get_cursor(conn, HIGH_WATER_MARK_KEY, "") == str(new_occurred)


# ---------------------------------------------------------------------
# 4. Paint timestamp and paint_from gate still use occurred_at, NEVER
#    published_at -- the regression this fix must not introduce.
# ---------------------------------------------------------------------

def test_paint_timestamp_uses_occurred_at_not_published_at(conn):
    """player_cell_ping.ts must record occurred_at (when the RF event
    actually happened), not published_at (when the feed happened to
    publish it) -- even for an event that only paints BECAUSE of the
    published_at-keyed guard fix. Getting this wrong would silently
    misdate every paint on the board.
    """
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    mark = NOW - 3600
    set_cursor(conn, HIGH_WATER_MARK_KEY, str(mark))

    old_occurred = mark - 90 * 86400
    new_published = mark + 60
    event = _rx_event("ts-check-1", old_occurred, new_published, node_ref)

    outcome = _process(conn, ingestor, event, season_id, registered)
    assert outcome == "painted_rx"

    row = conn.execute("SELECT ts FROM player_cell_ping").fetchone()
    assert row[0] == old_occurred  # occurred_at, NOT new_published
    assert row[0] != new_published


def test_paint_from_gate_still_uses_occurred_at_not_published_at(conn):
    """The paint_from date gate must still be evaluated against
    occurred_at -- an event whose occurred_at predates paint_from is
    skipped even though its published_at (and thus the backfill guard)
    would happily let it through.
    """
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    # occurred_at well before paint_from; published_at well after it --
    # if the gate ever used published_at this would wrongly paint.
    old_occurred = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp())
    new_published = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp())
    event = _tx_event("gate-check-1", old_occurred, new_published, node_ref)

    outcome = _process(conn, ingestor, event, season_id, registered, paint_from="2025-01-01")

    assert outcome == "skipped_before_paint_from"
    rows = conn.execute("SELECT count(*) FROM player_cell_ping").fetchone()[0]
    assert rows == 0
    # Date-skipped events stay out of the dedup table (recoverable) --
    # same contract every other paint_from skip gets.
    seen = conn.execute(
        "SELECT count(*) FROM freqmapper_verification WHERE verification_id = ?",
        ("gate-check-1",),
    ).fetchone()[0]
    assert seen == 0


# ---------------------------------------------------------------------
# 5. The mark still only ever advances forward.
# ---------------------------------------------------------------------

def test_mark_never_moves_backward_across_published_at_values(conn):
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    mark = NOW - 1000
    set_cursor(conn, HIGH_WATER_MARK_KEY, str(mark))

    # First, a normal forward event: advances the mark to its published_at.
    forward_published = mark + 500
    forward_event = _tx_event("forward-1", forward_published - 10, forward_published, node_ref)
    outcome1 = _process(conn, ingestor, forward_event, season_id, registered)
    assert outcome1 == "painted"
    assert get_cursor(conn, HIGH_WATER_MARK_KEY, "") == str(forward_published)

    # Then, an allow_backfill=True event with an OLDER published_at than
    # the mark just advanced to -- must paint (operator opt-in) but must
    # NOT drag the mark backwards.
    older_published = mark - 5000
    backfill_event = _tx_event("backfill-2", older_published - 10, older_published, node_ref)
    outcome2 = _process(conn, ingestor, backfill_event, season_id, registered, allow_backfill=True)
    assert outcome2 == "painted"
    assert get_cursor(conn, HIGH_WATER_MARK_KEY, "") == str(forward_published)  # unchanged

    # Finally, another forward event further ahead: advances again.
    further_published = forward_published + 200
    further_event = _tx_event("forward-2", further_published - 10, further_published, node_ref)
    outcome3 = _process(conn, ingestor, further_event, season_id, registered)
    assert outcome3 == "painted"
    assert get_cursor(conn, HIGH_WATER_MARK_KEY, "") == str(further_published)


def test_mark_seeds_from_first_event_using_guard_time_when_no_prior_mark(conn):
    """A fresh deployment with no mark yet seeds the mark from the first
    event it processes, using that event's guard time (published_at,
    falling back to occurred_at) -- not blindly occurred_at.
    """
    node_ref = "0a0a0a0a"
    _seed_player_and_node(conn, node_ref=node_ref)
    season_id = _season_id(conn)
    registered = {node_ref: (1, "RED")}
    ingestor = FreqMapperIngestor()

    assert get_cursor(conn, HIGH_WATER_MARK_KEY, "") == ""

    occurred = NOW - 999999
    published = NOW - 10  # very different from occurred_at
    event = _tx_event("first-ever-1", occurred, published, node_ref)
    outcome = _process(conn, ingestor, event, season_id, registered)

    assert outcome == "painted"
    assert get_cursor(conn, HIGH_WATER_MARK_KEY, "") == str(published)
