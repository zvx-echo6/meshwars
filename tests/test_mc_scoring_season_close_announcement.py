"""Integration tests for the Discord "season closed" announcement
app/mc_scoring.py's maybe_roll_season() enqueues (app/discord_notify.py's
enqueue()/build_season_close_embed()) -- on the SAME connection, inside
the SAME transaction, right after the closing season's own rows
(mc_season_team_tally, mc_season.status/winner) are written.

Uses the in-memory `conn` fixture (tests/conftest.py), same as
tests/test_discord_notify.py's own enqueue() tests: maybe_roll_season()
takes an already-open connection exactly like app/results.py's
freeze_month() does, so this needs no real file-backed database or
WriteSession the way the drain-loop tests do.
"""
from __future__ import annotations

import time

from app import mc_scoring
from app.config import settings

PROTOCOL = "mc"


def _enable_discord(conn) -> None:
    """Same DB-backed config as tests/test_discord_notify.py's own
    _enable_discord() -- enqueue() is a no-op while announcements are
    disabled, and the whole point of these tests is to prove they are
    NOT a no-op here.
    """
    conn.execute(
        "UPDATE discord_config SET enabled = 1, "
        " webhook_url = 'https://discord.test/api/webhooks/1/x' WHERE id = 1"
    )


def _expired_season(conn, now: int) -> int:
    """An active season whose ends_at is already in the past -- exactly
    what maybe_roll_season() looks for to decide a roll is due."""
    conn.execute(
        "INSERT INTO mc_season(protocol, started_at, ends_at, status) "
        "VALUES (?, ?, ?, 'active')",
        (PROTOCOL, now - settings.mc_season_days * 86400, now - 1),
    )
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def test_maybe_roll_season_enqueues_one_row_keyed_on_the_closed_season_id(conn):
    _enable_discord(conn)
    now = int(time.time())
    season_id = _expired_season(conn, now)

    rolled = mc_scoring.maybe_roll_season(conn, now, PROTOCOL)

    assert rolled is True
    rows = conn.execute(
        "SELECT kind, key FROM discord_outbox WHERE kind = 'season_close'"
    ).fetchall()
    assert [(r["kind"], r["key"]) for r in rows] == [("season_close", str(season_id))]


def test_maybe_roll_season_never_enqueues_when_no_roll_is_due(conn):
    _enable_discord(conn)
    now = int(time.time())
    # Active, NOT expired -- ends_at in the future.
    conn.execute(
        "INSERT INTO mc_season(protocol, started_at, ends_at, status) "
        "VALUES (?, ?, ?, 'active')",
        (PROTOCOL, now, now + settings.mc_season_days * 86400),
    )

    rolled = mc_scoring.maybe_roll_season(conn, now, PROTOCOL)

    assert rolled is False
    assert conn.execute("SELECT * FROM discord_outbox").fetchall() == []


def test_rolling_the_same_season_twice_does_not_enqueue_twice(conn):
    """discord_outbox's own UNIQUE(kind, key) index (via enqueue()'s
    INSERT OR IGNORE), exercised through the real call site rather than
    a hand-rolled replay: after the first roll closes `season_id`, that
    exact row is forced back to 'active' and expired again (standing in
    for whatever operator action might re-trigger a roll of an
    already-closed season) and rolled a second time. The second call
    must not produce a second discord_outbox row for the same season.
    """
    _enable_discord(conn)
    now = int(time.time())
    season_id = _expired_season(conn, now)

    mc_scoring.maybe_roll_season(conn, now, PROTOCOL)
    conn.execute(
        "UPDATE mc_season SET status = 'active', ends_at = ? WHERE id = ?",
        (now - 1, season_id),
    )
    mc_scoring.maybe_roll_season(conn, now, PROTOCOL)

    rows = conn.execute(
        "SELECT * FROM discord_outbox WHERE kind = 'season_close' AND key = ?",
        (str(season_id),),
    ).fetchall()
    assert len(rows) == 1


def test_maybe_roll_season_no_op_when_discord_disabled(conn):
    """discord_config defaults to disabled (enabled=0) -- a deployment
    that has never configured a webhook must not accumulate a
    discord_outbox backlog it will never drain, same contract every
    other enqueue() call site already has."""
    now = int(time.time())
    _expired_season(conn, now)

    rolled = mc_scoring.maybe_roll_season(conn, now, PROTOCOL)

    assert rolled is True
    assert conn.execute("SELECT * FROM discord_outbox").fetchall() == []
