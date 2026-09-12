"""Tests for the per-net streak scoping fix (app/checkin.py's
checkin_streak()/_award_checkin()/_process_mt_packet()) and its one-time
history correction (tools/backfill_net_id.py).

Until 2026-09-10, checkin_streak() scoped a player's streak by protocol
alone, so two MeshCore nets on different weekdays shared one timeline
and broke each other's streaks the moment both had ever awarded anyone
-- see checkin_streak's own docstring in app/checkin.py for the full
story of the bug this fixes. These tests prove:

  (a) the net_id-scoped replacement keeps two such nets' streaks fully
      independent (checkin_streak() itself, the same call
      _award_checkin() makes -- no HTTP, no poller machinery needed to
      exercise the actual bug/fix).
  (b) the Meshtastic ingest path (_process_mt_packet) now logs an
      unresolved sender to checkin_unresolved_sender the same way the
      MeshCore path always has, closing the visibility gap described in
      this fix's own task.
  (c) tools/backfill_net_id.py's own weekday-matching attribution logic
      picks the one right net for a historical row that only carries
      protocol + net_date.

Uses tests/conftest.py's `conn` fixture (in-memory db, real SCHEMA +
MIGRATIONS) throughout -- these are all conn-in, conn-out calls, no
HTTP surface under test.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app.checkin import CheckinPoller, checkin_streak
from app.meshview_client import MeshviewClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import backfill_net_id as bfn  # noqa: E402

NOW = 1799999999


def _net(conn, *, weekday, protocol="mc", kind="corescope", label="Test Net",
         hashtag="", start_hour=0, end_hour=23, timezone="America/Boise") -> int:
    cur = conn.execute(
        "INSERT INTO checkin_net(label, protocol, kind, connector_url, channel, hashtag, "
        "weekday, start_hour, end_hour, timezone, start_date, enabled, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (label, protocol, kind, "http://example.test", "general", hashtag,
         weekday, start_hour, end_hour, timezone, "2000-01-01", 1, NOW),
    )
    return cur.lastrowid


def _player(conn, name="Player") -> int:
    cur = conn.execute(
        "INSERT INTO player(display_name, team, created_at) VALUES (?,?,?)",
        (name, "RED", NOW),
    )
    return cur.lastrowid


def _award(conn, *, player_id, net_id, net_date, streak, points=25.0, protocol="mc") -> None:
    conn.execute(
        "INSERT INTO mc_checkin_award"
        "(season_id, player_id, net_date, points, protocol, message_id, awarded_at, streak, net_id) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
        (player_id, net_date, points, protocol, f"msg-{player_id}-{net_date}", NOW, streak, net_id),
    )


# ---- (a) two MC nets on different weekdays produce independent streaks ----

def test_streak_independent_per_net_across_two_weekdays(conn):
    wed_net = _net(conn, weekday=2, label="Freq51 MC")
    thu_net = _net(conn, weekday=3, label="Coloradomesh MC")

    wed_player = _player(conn, "WedRegular")
    thu_player = _player(conn, "ThuOnly")

    # Wednesday activity by a DIFFERENT player, interleaved between the
    # three Thursdays below -- under the OLD protocol-only scoping this
    # is exactly what broke a Thursday-only player's streak the moment
    # both nets shared any history at all. Proves it no longer can.
    for d in ("2026-08-26", "2026-09-02", "2026-09-09"):
        _award(conn, player_id=wed_player, net_id=wed_net, net_date=d, streak=1)

    # thu_player attends three consecutive Thursdays and nothing else.
    # Computed and inserted one at a time, in date order, mirroring
    # exactly how _award_checkin computes a streak against already-
    # committed history (see checkin_streak's own docstring on why only
    # committed history strictly before net_date is ever consulted).
    thu_dates = ["2026-08-27", "2026-09-03", "2026-09-10"]
    expected = [1, 2, 3]
    for d, want in zip(thu_dates, expected):
        got = checkin_streak(conn, thu_player, thu_net, d)
        assert got == want, f"{d}: expected streak {want}, got {got}"
        _award(conn, player_id=thu_player, net_id=thu_net, net_date=d, streak=got)

    # The Wednesday-only player's own streak is likewise unaffected by
    # the Thursday activity sharing its protocol -- three Wednesdays in
    # a row (net_id=wed_net) means their next one is streak 4.
    assert checkin_streak(conn, wed_player, wed_net, "2026-09-16") == 4


def test_streak_with_null_net_id_finds_no_history_not_a_crash(conn):
    """A legacy row (net_id NULL) or an ambiguous admin credit
    (net_id=None passed in) must not crash checkin_streak() -- see that
    function's own docstring. It simply finds no scoped history and
    returns 1, same as a player's very first check-in.
    """
    net_id = _net(conn, weekday=2)
    player_id = _player(conn)
    # A legacy, unattributed row sharing this player's protocol+date
    # neighborhood -- must not be found by a net_id-scoped query.
    _award(conn, player_id=player_id, net_id=None, net_date="2026-08-19", streak=1)

    assert checkin_streak(conn, player_id, None, "2026-08-26") == 1
    assert checkin_streak(conn, player_id, net_id, "2026-08-26") == 1


# ---- (b) an unresolved MT sender is recorded ------------------------------

def test_unresolved_mt_sender_is_recorded(conn):
    """_process_mt_packet must log an unregistered Meshtastic sender to
    checkin_unresolved_sender, the same way _process_mc_message always
    has for MeshCore -- this path recorded nothing at all before this
    fix (the bug the task calls out).
    """
    poller = CheckinPoller(MeshviewClient(base_url="https://example.invalid"))

    # Full-day window so "now" always falls inside it regardless of
    # wall-clock time this test runs at, same convention
    # tests/test_account_player_data.py's own _net() helper documents.
    now_ts = int(time.time())
    weekday = datetime.now(ZoneInfo("America/Boise")).weekday()
    net_id = _net(conn, weekday=weekday, protocol="mt", kind="meshview",
                  label="Freq51 MT", hashtag="#freq51")
    net_row = conn.execute("SELECT * FROM checkin_net WHERE id = ?", (net_id,)).fetchone()

    pkt = {
        "id": 4242,
        "payload": "checking in #freq51",
        "import_time_us": now_ts * 1_000_000,
        "from_node_id": 0xDEADBEEF,
    }

    poller._process_mt_packet(
        conn, "https://meshview.example", [net_row], pkt,
        season_id=1, registered={},
        config={"points": 25.0, "streak_bonus": 5.0, "streak_bonus_max": 25.0},
        received_at=now_ts,
    )

    rows = conn.execute("SELECT * FROM checkin_unresolved_sender").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["net_id"] == net_id
    assert row["message_count"] == 1
    # No mt_node_key long_name on file and no name field on the packet
    # itself -- falls all the way back to the bare 8-hex node_ref, the
    # same canonical form player_node.node_ref uses.
    assert row["sender_name"] == "deadbeef"

    # Not settled -- an unresolved sender must stay eligible for a later
    # poll to retry once they register (see _mark_seen's own docstring
    # and _process_mc_message's 2026-08-19 incident comment, which
    # _process_mt_packet now follows too).
    seen = conn.execute(
        "SELECT 1 FROM checkin_seen_message WHERE connector = ? AND packet_id = ?",
        ("https://meshview.example", "4242"),
    ).fetchone()
    assert seen is None
    # And nothing was awarded.
    assert conn.execute("SELECT COUNT(*) FROM mc_checkin_award").fetchone()[0] == 0


def test_unresolved_mt_sender_prefers_mt_node_key_long_name(conn):
    """When a NodeInfo long_name is on file for this node_ref
    (mt_node_key, populated independently by app/ingest.py's own
    NodeInfo poll -- see app/db.py's own comment), it is used instead of
    the bare node_ref fallback.
    """
    poller = CheckinPoller(MeshviewClient(base_url="https://example.invalid"))
    now_ts = int(time.time())
    weekday = datetime.now(ZoneInfo("America/Boise")).weekday()
    net_id = _net(conn, weekday=weekday, protocol="mt", kind="meshview",
                  label="Freq51 MT", hashtag="#freq51")
    net_row = conn.execute("SELECT * FROM checkin_net WHERE id = ?", (net_id,)).fetchone()

    conn.execute(
        "INSERT INTO mt_node_key(node_ref, public_key, long_name, first_seen, last_seen) "
        "VALUES ('deadbeef', ?, 'Wandering Wardriver', ?, ?)",
        ("aa" * 32, now_ts - 100, now_ts - 10),
    )

    pkt = {
        "id": 4343,
        "payload": "checking in #freq51",
        "import_time_us": now_ts * 1_000_000,
        "from_node_id": 0xDEADBEEF,
    }
    poller._process_mt_packet(
        conn, "https://meshview.example", [net_row], pkt,
        season_id=1, registered={},
        config={"points": 25.0, "streak_bonus": 5.0, "streak_bonus_max": 25.0},
        received_at=now_ts,
    )

    row = conn.execute("SELECT sender_name FROM checkin_unresolved_sender").fetchone()
    assert row["sender_name"] == "Wandering Wardriver"


# ---- (c) backfill attribution: weekday -> net -----------------------------

def test_backfill_attribution_maps_weekday_to_net(conn):
    """tools/backfill_net_id.py's attribute() must map a Wednesday mc
    row to the Wednesday mc net and a Thursday mc row to the Thursday mc
    net -- the exact real-world shape (Freq51 MC id=1/weekday=2,
    Coloradomesh MC id=3/weekday=3) this fix's own history correction
    runs against.
    """
    freq51 = _net(conn, weekday=2, protocol="mc", label="Freq51 MC")
    coloradomesh = _net(conn, weekday=3, protocol="mc", label="Coloradomesh MC")
    player_id = _player(conn)

    _award(conn, player_id=player_id, net_id=None, net_date="2026-08-19", streak=None)  # Wednesday
    _award(conn, player_id=player_id, net_id=None, net_date="2026-09-03", streak=None)  # Thursday

    rows = bfn.load_rows(conn)
    nets = bfn.load_nets(conn)
    attribution, ambiguous = bfn.attribute(rows, nets)

    assert not ambiguous
    by_date = {r["net_date"]: attribution[r["rowid"]] for r in rows}
    assert by_date["2026-08-19"] == freq51
    assert by_date["2026-09-03"] == coloradomesh


def test_backfill_attribution_leaves_ambiguous_rows_null(conn):
    """Two nets sharing a (protocol, weekday) -- or none at all -- must
    be left NULL, never guessed at.
    """
    _net(conn, weekday=2, protocol="mc", label="Freq51 MC")
    _net(conn, weekday=2, protocol="mc", label="Second Wed Net")  # collides on purpose
    player_id = _player(conn)

    _award(conn, player_id=player_id, net_id=None, net_date="2026-08-19", streak=None)  # Wed: 2 matches
    _award(conn, player_id=player_id, net_id=None, net_date="2026-08-24", streak=None)  # Monday: 0 matches

    rows = bfn.load_rows(conn)
    nets = bfn.load_nets(conn)
    attribution, ambiguous = bfn.attribute(rows, nets)

    assert len(ambiguous) == 2
    assert all(attribution[r["rowid"]] is None for r in rows)
