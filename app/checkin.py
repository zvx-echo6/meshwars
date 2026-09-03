"""Net check-ins: a second way to earn points, alongside squares held.

Checking in during a net's window earns a registered, non-disabled
player checkin_config.points, once per player per net -- on top of,
never instead of, the squares their team holds (see
app/mc_scoring.py's team_totals()). Theme only: square-holders are
"Wardrivers," check-in earners are "NetOps," but these are two
ACTIVITIES on the same player model, not two kinds of player -- the
same person can do both and shows up in both rankings. There is no
class or mode anywhere in this schema.

Nets are DB-backed (app/db.py's checkin_net/checkin_config), not the
single settings.checkin_* pair this feature originally shipped with --
an admin can add, edit, enable/disable, or delete a net at any time
through app/admin_ops.py's /api/admin/checkin/* routes, and it takes
effect on CheckinPoller's very next cycle, no restart, because that
poller reads checkin_net/checkin_config fresh from the database EVERY
cycle (see load_checkin_config and CheckinPoller._poll_once below) --
never from settings, and never cached in the process. settings.py's
original checkin_* fields still exist (app/config.py) purely as the
one-time seed source for a fresh database (seed_nets_from_env below)
and as the fallback net_date_for_net/load_checkin_config fall back to
if their tables are somehow unreadable; nothing scoring-relevant reads
them once a database has rows.

A "net" is one checkin_net row carrying a connector, a window, and a
channel-or-hashtag together -- see that table's own comment in
app/db.py for why those three belong on one row rather than three
tables. Multiple nets can share one connector instance (two nets, one
CoreScope, different channels; two nets, one meshview, different
hashtags) -- see CheckinPoller below for how client pooling and
identity resolution both handle that without either duplicating
upstream requests or letting one net's verdict on a shared message
block a different net sharing that connector from ever seeing it.
Points are GLOBAL, not per-net -- there is one player_id -> points
relationship, same as before this table existed; a player who attends
two different nets in the same protocol simply earns twice, keyed
apart by their different net_dates (see mc_checkin_award's PRIMARY
KEY). Two nets on the SAME weekday in the same protocol produce the
SAME net_date and collide under that key -- a known, accepted limit of
this design, not a bug: nets were never meant to double-pay the same
day.

The two protocols are polled the same way app/ingest.py polls meshview
for position packets -- a background task, its own interval, tolerant
of the upstream being down or slow, idempotent under repeated polling
-- but are deliberately asymmetric in what identifies a net, and that
asymmetry is intentional and correct, not something to unify:

- MeshCore: GET {connector_url}/api/channels/{channel}/messages on a
  CoreScope instance (e.g. live.mwmesh.com) returns the newest 100
  messages in the named channel, oldest first, no pagination. The only
  sender identifier ON A MESSAGE is a free-text display name (`sender`)
  -- MeshCore channel messages are encrypted to a shared channel key,
  not signed per sender, so there is no public key to read off a
  message itself. Identity resolution for a MeshCore check-in is
  therefore KEY-BASED, full stop -- one resolution mechanism, the
  public-key directory bridge, fed by two different ways a radio can
  arrive in player_node:

    The public-key directory bridge (_build_directory_bridge below,
    used by _resolve_mc_identities): resolves a player's ALREADY-BOUND
    MeshCore radio contact -- player_node, protocol='mc', the first 8
    hex characters of that radio's public key -- through a CoreScope
    instance's node directory (/api/nodes), which carries both a
    display name and the full public key for every node it has seen.
    If that contact's 8-hex prefix matches exactly one directory entry,
    that entry's name is trusted as the player's check-in identity.
    This needs no separate check-in registration step: it works
    identically no matter how the contact got into player_node --

      - MeshMapper's wardriving auto-bind (app/mc_ingest.py) or a
        player typing it into POST /api/nodes by hand
        (app/nodes_api.py), the two ways every OTHER radio type on this
        site gets bound too; or
      - node confirmation (app/checkin_api.py's POST
        /api/checkin/confirm/accept), MeshCore-only and strictly
        stronger than either: it makes the SPECIFIC radio advertise
        live during a short window and binds whichever public key
        showed a FRESH advert under the typed name, proving present
        possession rather than merely asserting a name or a node_ref
        (see app/db.py's mc_node_confirmation comment for the full
        mechanics). It exists because a MeshCore channel message
        carries no per-sender key, so nothing else in this identity
        model can prove who is really holding a given radio right now.

    All three write the exact same player_node row shape, and this
    bridge reads player_node, not whichever path wrote it -- so however
    a contact got bound, it resolves through this bridge the same way,
    with no second, weaker identity path standing behind it. (An
    earlier version of this feature had one: a last-resort,
    self-declared mc_checkin_binding row a player could type in for a
    contact the bridge found nothing for. Retired -- it carried none of
    the impersonation resistance described below, and node confirmation
    above is strictly stronger proof for exactly the players who needed
    it. mc_checkin_binding itself is left in place per this codebase's
    no-drop convention -- see its own comment in app/db.py -- but
    nothing reads it anymore.)

    Which directory: THIS net's own connector's cached directory
    first, and only if the contact is absent from it, the UNION of
    every OTHER connector currently being polled (CheckinPoller's
    _resolve_mc_identities below) -- a player's radio is most likely
    to show up in the directory of the net they actually attend, but
    a second connector having already seen the same public key
    should still resolve them. This cross-connector pass only WIDENS
    where the bridge looks; it applies the exact same ambiguity
    refusals described below to whatever set it is consulting, so
    widening the search can never turn an ambiguous match into a
    confident one.

    Why this is trusted automatically, when a bare name match would
    not be: the join is anchored on a public key MeshWars already
    knows independently of anything a check-in message claims.
    Someone who renames their OWN node to impersonate a player's
    display name shows up in the directory as a second, DIFFERENT
    public key under that name -- their contact was never bound to
    the real player, so their messages resolve to nobody. An
    attacker cannot make their own node's contact resolve to someone
    else's public key; they can only ever make their own node's NAME
    collide with someone else's, and resolution here never starts
    from the name. Starting from the name instead of the key is
    exactly what would make that attack work; starting from the key
    is why it doesn't.

    Refuses rather than guesses on ambiguity -- see
    _build_directory_bridge and _resolve_mc_identities below for the
    specific cases (a contact matching more than one public key; a name
    shared by more than one public key anywhere in the directory) and
    why every one of them is a skip-and-log, never a pick-one.

- Meshtastic: GET {connector_url}/api/packets?portnum=1 on a meshview
  instance -- app/ingest.py's shared client already polls the DEFAULT
  one (settings.meshview_url) for position packets (portnum=3); this is
  the same host, just text messages instead, for the default net's
  connector, and a separately-pooled MeshviewClient for any OTHER
  connector_url an admin has configured (see CheckinPoller._mt_client_for
  below). A check-in is any message whose payload contains the net's
  hashtag, case-insensitively, ON ANY CHANNEL -- unlike MeshCore's
  channel-scoped feed, this is deliberately not narrowed by channel,
  since meshview's packet feed has no per-channel query to narrow it
  with in the first place. Identity is `from_node_id`, resolved through
  player_node exactly the way app/ingest.py's position poller already
  does -- see _bare_node_ref (reused from there, not reimplemented) and
  _load_mt_registered_players below.

Dedup: the natural key for an award is (season, player, local net
date) -- neither feed has a session concept, and a MeshCore sender
posting several times in one net must still be credited once. That key
is the table's PRIMARY KEY (see app/db.py's mc_checkin_award), so
_award_checkin's INSERT OR IGNORE is what actually enforces "at most
one per player per net," not any in-memory bookkeeping here. Separately,
each message/packet's own id is deduplicated too, against
checkin_seen_message keyed on (connector_url, packet_id) -- see that
table's comment in app/db.py for why the connector has to be part of
the key now that more than one connector instance can exist per
protocol -- so a message already looked at on an earlier poll is never
re-examined at all, whether or not it ended up earning anything. When
two nets share one connector (see CheckinPoller._poll_mc_feed and
_process_mt_packet below), a message is deduped ONCE per connector, not
once per net sharing it: both are evaluated against the SAME fetched
message/packet in one pass, and it is marked seen only once every net
that could possibly have wanted it has had its say -- see those
functions' own comments for why settling it after only the first net
looked would silently starve the second.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from . import mc_scoring, results
from .config import settings
from .db import WriteSession, connect
from .mc_api import active_season
from .meshview_client import MeshviewClient
from .node_ref import format_node_ref, normalize_public_key, normalize_sender_name

log = logging.getLogger("checkin")

MC_PROTOCOL = "mc"
MT_PROTOCOL = "mt"

# How long a checkin_unresolved_sender row survives before
# CheckinPoller._maybe_prune_unresolved_senders removes it -- mirrors
# settings.mc_stat_retention_days' role for player_ingest_stat (app/
# mc_ingest.py): long enough for an operator to notice and act on a
# rename or a missed registration across several real nets, short
# enough that the table cannot grow without bound on a channel with
# persistent unregistered chatter during net windows.
UNRESOLVED_SENDER_RETENTION_DAYS = 60
_UNRESOLVED_PRUNE_INTERVAL_S = 3600.0

# Connector KIND is the admin-chosen field (checkin_net.kind) -- which
# upstream API a net's connector_url actually speaks. `protocol` above
# is the scoring-board discriminator every award/season/streak query
# keys on and stays exactly {mc, mt} forever; `kind` is free to grow a
# fourth option (as it just did, adding 'mqtt' alongside 'beacon')
# without either board gaining a third identity. KIND_PROTOCOL is the
# ONE place that mapping is expressed -- app/admin_ops.py's
# _validate_net_fields derives and validates `protocol` through this
# same dict on every write, so the two columns can never independently
# drift out of agreement.
#
# 'mqtt' is architecturally different from the other three: it is a
# persistent broker SUBSCRIPTION (app/mqtt_subscriber.py's
# MqttSubscriber), not a 30-second HTTP poll, so it has no client class
# alongside CoreScopeClient/BeaconClient/MeshviewClient below -- the
# subscriber writes decoded messages into mqtt_message_buffer
# (app/db.py) as they arrive, and CheckinPoller reads that table for an
# 'mqtt' net exactly the way it reads an HTTP response for the other
# three (see _fetch_mqtt_messages/_process_mqtt_message below), which is
# what keeps everything downstream of "a normalized message dict"
# completely unaware this kind is push-driven rather than pulled.
# Identity for 'mqtt' resolves the SAME way 'meshview' already does --
# directly off the sender's Meshtastic node id via
# _load_mt_registered_players, no directory, no name-ambiguity path --
# since both are protocol='mt' and MQTT carries the real numeric sender
# node id on every packet, unlike MeshCore's channel messages.
KIND_CORESCOPE = "corescope"
KIND_BEACON = "beacon"
KIND_MESHVIEW = "meshview"
KIND_MQTT = "mqtt"

KIND_PROTOCOL = {
    KIND_CORESCOPE: MC_PROTOCOL,
    KIND_BEACON: MC_PROTOCOL,
    KIND_MESHVIEW: MT_PROTOCOL,
    KIND_MQTT: MT_PROTOCOL,
}


def net_date_for_net(net, ts: int) -> str | None:
    """Local net date (YYYY-MM-DD) for `ts` if it falls inside THIS net's
    window -- net['weekday'], from net['start_hour'] through
    net['end_hour']:59:59, local to net['timezone'] -- and on or after
    net['start_date'], otherwise None. `net` is a checkin_net row (or any
    mapping with those keys) -- this used to read settings.checkin_net_*
    globals directly, back when there was exactly one net; now every net
    carries its own window, so the caller supplies which one.

    A real IANA zone via zoneinfo, not a fixed UTC offset: America/Boise
    is UTC-7 in winter and UTC-6 in summer, and a message near either
    boundary of the window must land in the same LOCAL hour either way,
    which only a real zone gets right across the transition. A message
    that spills past local midnight into Thursday has a different
    LOCAL weekday, not just a different hour -- the weekday check above
    is what rejects it, not the hour check.

    The start-date gate compares against the NET date computed here, not
    against `ts` -- a message posted at 23:30 local on a Wednesday
    belongs to that Wednesday's net date, which is what a start date of
    that same Wednesday must include. Both are YYYY-MM-DD strings, so a
    plain string comparison sorts correctly with no extra parsing.
    net['start_date'] empty means block every net, never "no lower
    bound" -- the same contract settings.checkin_net_start_date always
    used (see its comment in config.py for why), carried over unchanged
    so a net seeded from settings with no start date configured stays
    exactly as blocked as it always was. This runs on every message on
    every poll forever (both feeds hand back history on every request),
    so it has to stay a cheap comparison with no log line -- a poll
    finding the same handful of too-old messages, week after week, is
    the expected steady state, not something worth a log line each
    time.
    """
    local = datetime.fromtimestamp(ts, tz=ZoneInfo(net["timezone"]))
    if local.weekday() != net["weekday"]:
        return None
    if not (net["start_hour"] <= local.hour <= net["end_hour"]):
        return None
    net_date = local.date().isoformat()
    start_date = net["start_date"]
    if not start_date or net_date < start_date:
        return None
    return net_date


def most_recent_mc_net_date(conn, now: int | None = None) -> str | None:
    """The most recent local net date (YYYY-MM-DD) that any currently
    enabled MeshCore-family net (checkin_net, protocol='mc' -- both
    KIND_CORESCOPE and KIND_BEACON) has already reached the end of, as
    of `now` (real current time if omitted).

    Deliberately computed from checkin_net's own schedule columns
    (weekday/start_hour/end_hour/timezone/start_date) rather than from
    mc_checkin_award's own MAX(net_date). That distinction is the whole
    point of app/account_api.py's checkin-health endpoint calling this
    instead: award data answers "when did someone last get credited,"
    which is exactly the question that goes silently wrong on the one
    night that matters -- a net that ran and produced zero credited
    check-ins (a directory outage, every attending player's contact
    going stale at once) would make mc_checkin_award's own idea of
    "most recent" quietly point at an OLDER night that did succeed,
    hiding the very failure this function exists to help surface.
    Schedule truth doesn't have that failure mode: a net's weekday and
    hours say when it ran regardless of whether anyone was credited on
    it.

    For each enabled net, walks backward from `now`, local to that
    net's own timezone, to the most recent date matching its weekday
    whose window has already opened (today counts once the window has
    started, even if still in progress -- same "currently inside the
    window" moment net_date_for_net itself credits a message against).
    Across every enabled net, the LATEST such date wins (plain string
    comparison, since YYYY-MM-DD sorts correctly) -- a deployment with
    more than one MeshCore net on different weekdays reports on
    whichever one most recently closed its window, not a fixed one of
    them. A net whose start_date blocks `now`'s local date entirely
    (see net_date_for_net's own docstring for what an empty start_date
    means) is skipped for that comparison the same way it would refuse
    to award on it.

    None if no enabled MeshCore-family net exists at all, or every one
    of them is start_date-blocked as of `now`.
    """
    if now is None:
        now = int(time.time())
    rows = conn.execute(
        "SELECT weekday, start_hour, end_hour, timezone, start_date FROM checkin_net "
        " WHERE enabled = 1 AND protocol = ?",
        (MC_PROTOCOL,),
    ).fetchall()

    best: str | None = None
    for net in rows:
        start_date = net["start_date"]
        if not start_date:
            continue  # blocks all, same convention net_date_for_net uses
        local_now = datetime.fromtimestamp(now, tz=ZoneInfo(net["timezone"]))
        days_back = (local_now.weekday() - net["weekday"]) % 7
        if days_back == 0 and local_now.hour < net["start_hour"]:
            # Today IS the right weekday, but the window has not opened
            # yet -- the most recently COMPLETED occurrence is a full
            # week earlier, not today (today hasn't happened yet).
            days_back = 7
        candidate = (local_now - timedelta(days=days_back)).date().isoformat()
        if candidate < start_date:
            continue
        if best is None or candidate > best:
            best = candidate
    return best


def load_checkin_config(conn) -> dict:
    """Fresh, uncached read of the checkin_config singleton -- points,
    streak bonus, whether the poller runs at all, and the poller's own
    timing knobs. Read on every poll cycle (CheckinPoller._poll_once)
    and by every admin route that needs the current numbers
    (app/admin_ops.py), never cached anywhere in the process, the same
    pattern app/notice_api.py's GET /api/notice uses for its own
    singleton row -- this table is smaller and read far less often than
    a scoring query, so there is no performance case for caching it, and
    caching it would silently reintroduce the "an admin edit needs a
    restart to take effect" problem this table exists to remove.

    Falls back to config.py's original settings if the row is somehow
    missing (a database whose migrations have not run yet) rather than
    raising -- defensive, since app/db.py's MIGRATIONS seeds this row
    unconditionally and it should always be there in practice, but a
    scoring path failing outright over a missing config row would be a
    worse failure mode than briefly falling back to the settings this
    row was itself seeded from.
    """
    row = conn.execute(
        "SELECT enabled, points, streak_bonus, streak_bonus_max, "
        "       poll_interval_seconds, directory_limit, directory_refresh_seconds "
        "  FROM checkin_config WHERE id = 1"
    ).fetchone()
    if row is None:
        return {
            "enabled": settings.checkin_enabled,
            "points": settings.checkin_points,
            "streak_bonus": settings.checkin_streak_bonus,
            "streak_bonus_max": settings.checkin_streak_bonus_max,
            "poll_interval_seconds": settings.checkin_poll_interval_seconds,
            "directory_limit": settings.mc_checkin_directory_limit,
            "directory_refresh_seconds": settings.mc_checkin_directory_refresh_seconds,
        }
    return dict(row)


def seed_nets_from_env(conn) -> None:
    """One-time bootstrap, called from app/db.py's init_db() on every
    startup: if checkin_net has never been populated, create rows that
    reproduce exactly what settings.* already describes -- the single
    MeshCore net and the single Meshtastic net this feature has run as
    since before nets lived in the database. A no-op the moment a row
    exists, whether it was seeded by an earlier boot or hand-created by
    an operator through the admin API -- this only ever fires once per
    database, by design: the whole point of moving nets into the
    database is that an operator, not this function, owns them from
    here on. (If an operator deletes every net, this fires again on the
    next boot and reproduces the settings-derived defaults, same as a
    truly fresh database -- there is no separate "already ran once"
    flag, only "checkin_net is empty right now.")

    Deliberately reads settings rather than anything already in the
    database: those env vars are the only place today's production
    values exist before this function ever runs, and after it runs once
    they are never consulted again for net configuration -- see
    CheckinPoller below, which reads checkin_net/checkin_config fresh
    from the database on every cycle, never settings. The net effect is
    that deploying this changes NO behavior: same feeds, same window,
    same points, just moved from env-var-and-restart to database-and-
    admin-API.

    The MeshCore net is only created if settings.mc_checkin_base_url is
    set -- empty means that half of check-ins was never configured (see
    config.py's own comment on that field), and seeding a net anyway
    would turn "never configured" into "configured against an empty
    connector_url," which is not the same thing and would fail every
    poll. The Meshtastic net is always created: settings.meshview_url
    has no empty-means-off convention (app/ingest.py already requires
    it to run at all), so there has always been a Meshtastic check-in
    net in practice, gated only by checkin_enabled.
    """
    existing = conn.execute("SELECT count(*) FROM checkin_net").fetchone()[0]
    if existing:
        return

    now = int(time.time())
    if settings.mc_checkin_base_url:
        # settings.mc_checkin_base_url has only ever pointed at a
        # CoreScope instance (live.mwmesh.com in production) -- 'beacon'
        # was never an option when this env var was the only way to
        # configure a MeshCore net, so 'corescope' is not a guess, it is
        # what this seed has always meant.
        conn.execute(
            "INSERT INTO checkin_net(label, kind, protocol, connector_url, channel, hashtag, "
            " weekday, start_hour, end_hour, timezone, start_date, enabled, created_at) "
            "VALUES (?, 'corescope', 'mc', ?, ?, '', ?, ?, ?, ?, ?, 1, ?)",
            (
                "Weekly Net",
                settings.mc_checkin_base_url.rstrip("/"),
                settings.mc_checkin_channel,
                settings.checkin_net_weekday,
                settings.checkin_net_start_hour,
                settings.checkin_net_end_hour,
                settings.checkin_net_timezone,
                settings.checkin_net_start_date,
                now,
            ),
        )
    conn.execute(
        "INSERT INTO checkin_net(label, kind, protocol, connector_url, channel, hashtag, "
        " weekday, start_hour, end_hour, timezone, start_date, enabled, created_at) "
        "VALUES (?, 'meshview', 'mt', ?, '', ?, ?, ?, ?, ?, ?, 1, ?)",
        (
            "Weekly Net (Meshtastic)",
            settings.meshview_url.rstrip("/"),
            settings.mt_checkin_hashtag,
            settings.checkin_net_weekday,
            settings.checkin_net_start_hour,
            settings.checkin_net_end_hour,
            settings.checkin_net_timezone,
            settings.checkin_net_start_date,
            now,
        ),
    )

    # checkin_config's row already exists (app/db.py's MIGRATIONS seeds
    # it, unconditionally, with the bare column defaults) by the time
    # this ever runs -- so this is an UPDATE, not an INSERT, and only
    # fires while updated_at is still 0, i.e. nothing has touched this
    # row since that migration created it. That guard is what stops an
    # operator's already-saved config edit from being silently
    # overwritten if, say, checkin_net were ever emptied and this
    # function ran again on a later boot: updated_at != 0 the moment
    # anyone (this function, or /api/admin/checkin/config) has ever
    # written real values here.
    row = conn.execute("SELECT updated_at FROM checkin_config WHERE id = 1").fetchone()
    if row is not None and row["updated_at"] == 0:
        conn.execute(
            "UPDATE checkin_config SET enabled = ?, points = ?, streak_bonus = ?, "
            " streak_bonus_max = ?, poll_interval_seconds = ?, directory_limit = ?, "
            " directory_refresh_seconds = ?, updated_at = ? WHERE id = 1",
            (
                int(settings.checkin_enabled),
                settings.checkin_points,
                settings.checkin_streak_bonus,
                settings.checkin_streak_bonus_max,
                settings.checkin_poll_interval_seconds,
                settings.mc_checkin_directory_limit,
                settings.mc_checkin_directory_refresh_seconds,
                now,
            ),
        )
    log.info("checkin: seeded nets from settings (mc=%s, mt=%s)",
              "on" if settings.mc_checkin_base_url else "off", settings.meshview_url)


def checkin_streak(conn, player_id: int, protocol: str, net_date: str) -> int:
    """How many consecutive nets this player has now attended, counting
    the one on `net_date` as the most recent.

    Scoped by PROTOCOL, not by season. A streak is a record of turning up
    rather than a score, so it survives a season boundary the way the
    points it earned deliberately do not -- but it stays per board,
    because a player who only ever wardrives MeshCore should not inherit
    a streak from the Meshtastic net.

    The subtle part is what counts as a MISSED net. There is no table of
    nets that happened -- a net is simply a Wednesday, and if nobody
    posted on one, nothing anywhere records that it took place. So the
    set of real nets is derived: a date is a net if ANY player earned an
    award on it. A Wednesday nobody attended (a holiday, a week the net
    was skipped) never appears in that set, so it cannot silently break
    everybody's streak for a week they could not have shown up to.

    Deterministic, and computed from committed history rather than
    carried forward on the previous award row: only dates strictly
    BEFORE net_date are consulted, so the order in which a poll happens
    to award several players for the same net cannot change any of their
    streaks. Re-running it for an award that already exists produces the
    same number.
    """
    nets = [
        r["net_date"] for r in conn.execute(
            "SELECT DISTINCT net_date FROM mc_checkin_award "
            " WHERE protocol = ? AND net_date < ? ORDER BY net_date DESC",
            (protocol, net_date),
        )
    ]
    if not nets:
        return 1

    attended = {
        r["net_date"] for r in conn.execute(
            "SELECT net_date FROM mc_checkin_award "
            " WHERE protocol = ? AND player_id = ? AND net_date < ?",
            (protocol, player_id, net_date),
        )
    }

    streak = 1
    for nd in nets:
        if nd not in attended:
            break
        streak += 1
    return streak


def streak_points(config: dict, streak: int) -> float:
    """Total points a check-in is worth at this streak length -- the base
    plus the bonus described in checkin_config.streak_bonus. `config` is
    a load_checkin_config() dict (or app/admin_ops.py's
    /api/admin/checkin/award, which loads one for the same reason) --
    read once per caller, not re-queried per streak point, since a
    single admin action or poll cycle must score every check-in it
    touches against the SAME numbers, not whatever the config happens to
    say by the time the last one in a batch is computed.
    """
    bonus = min(
        config["streak_bonus"] * max(streak - 1, 0),
        config["streak_bonus_max"],
    )
    return config["points"] + bonus


def _award_checkin(
    conn, config: dict, season_id: int, player_id: int, net_date: str, protocol: str,
    message_id: str, awarded_at: int, message_ts: int | None = None,
) -> None:
    """Credit one check-in, if this (season, player, net_date) hasn't
    already been credited -- see the module docstring for why that
    triple, enforced by mc_checkin_award's PRIMARY KEY, is what actually
    makes this idempotent no matter how many qualifying messages a
    player posted in the net or how many times a poll re-examines them.
    `points` is the base plus whatever the player's streak is worth
    (checkin_streak/streak_points above), computed and copied onto the
    row now rather than read live later, so a config change afterward
    can never rewrite this award. The streak itself is stored alongside
    it, so the number is auditable rather than something that has to be
    re-derived to be explained.

    `message_ts` is when the player actually POSTED, taken from the
    message itself -- distinct from `awarded_at`, which is only when a
    poll happened to look at it and is therefore up to a full poll
    interval late and quantised to when the poller ran. Recorded because
    nothing else can recover it: both feeds hand back a fixed window of
    recent messages, so a net that passes without this stored has its
    posting times gone for good. Nothing reads it yet -- it exists so
    that when something does, there is history to read.
    """
    streak = checkin_streak(conn, player_id, protocol, net_date)
    points = streak_points(config, streak)
    cur = conn.execute(
        "INSERT OR IGNORE INTO mc_checkin_award"
        "(season_id, player_id, net_date, points, protocol, message_id, awarded_at, "
        " message_ts, streak) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (season_id, player_id, net_date, points, protocol, message_id,
         awarded_at, message_ts, streak),
    )
    if cur.rowcount:
        log.info(
            "checkin: awarded %.2f points to player %d for %s net %s "
            "(season %d, message %s, streak %d)",
            points, player_id, protocol, net_date, season_id, message_id, streak,
        )


def _seen(conn, connector: str, packet_id: str) -> bool:
    """True if this connector has already SETTLED this message/packet id
    on an earlier poll -- see _mark_seen for what settled means. Shared
    by both protocols against checkin_seen_message (app/db.py), keyed on
    (connector, packet_id) rather than packet_id alone specifically so
    two different connector instances -- of either protocol -- numbering
    their own ids from their own sequences can never collide and hide a
    real check-in from the poller that fetched it. See app/db.py's
    checkin_seen_message comment for the production incident this
    replaces mc_checkin_seen_message/processed_packet reuse to avoid.
    """
    return conn.execute(
        "SELECT 1 FROM checkin_seen_message WHERE connector = ? AND packet_id = ?",
        (connector, packet_id),
    ).fetchone() is not None


def _record_unresolved_sender(
    conn, net_id: int, net_date: str, sender_name: str, seen_at: int,
) -> None:
    """Note that an in-window MeshCore message from `sender_name` could
    not be matched to a registered player, for this net's `net_date` --
    see checkin_unresolved_sender's own comment in app/db.py for the
    table shape and why (net_id, net_date) is the scope rather than
    every unresolved message ever seen.

    Upsert, not INSERT OR IGNORE: a repeat offender in the same net
    bumps message_count and last_seen rather than being silently
    ignored after the first sighting, the same "once per offender, not
    once per message" idea mc_checkin_award's own PRIMARY KEY applies
    to a successful check-in.

    Deliberately does NOT touch checkin_seen_message -- see this
    function's one caller (_process_mc_message) for why marking the
    message seen is exactly the mistake that cost two players their
    2026-08-19 award, and must not be reintroduced here.
    """
    conn.execute(
        "INSERT INTO checkin_unresolved_sender"
        "(net_id, net_date, sender_name, first_seen, last_seen, message_count) "
        "VALUES (?, ?, ?, ?, ?, 1) "
        "ON CONFLICT(net_id, net_date, sender_name) DO UPDATE SET "
        "  last_seen = excluded.last_seen, "
        "  message_count = message_count + 1",
        (net_id, net_date, sender_name, seen_at, seen_at),
    )


def _mark_seen(conn, connector: str, packet_id: str, seen_at: int) -> None:
    """Record that this connector's message/packet id has been SETTLED
    -- a decision was reached, so no later poll needs to look at it
    again.

    Settled is not the same as "looked at," and the difference is the
    whole point of this function existing separately from the _seen()
    read that guards _process_mc_message/_process_mt_packet. A message
    is settled once EVERY net sharing this connector that could plausibly
    have wanted it has had its say -- awarded, or found to be outside
    that net's window, or (MeshCore) carrying no channel hashtag/no net
    at all whose window it could fall in, or (Meshtastic) carrying none
    of those nets' hashtags. An unparseable timestamp or an unresolvable
    sender STRING (MeshCore's `sender` normalizing to nothing, or a
    Meshtastic packet with no from_node_id) can never become awardable
    for any net on this connector either, so those are settled too.

    A message whose sender simply did not resolve to a REGISTERED PLAYER
    is the one outcome that is NOT settled and must not be passed here
    -- that depends on the directory bridge and player_node, both of
    which change independently of the message itself, so it is left
    for a later poll to retry instead. See
    _process_mc_message and _process_mt_packet for exactly where each of
    these branches lands.
    """
    conn.execute(
        "INSERT OR IGNORE INTO checkin_seen_message(connector, packet_id, seen_at) "
        "VALUES (?, ?, ?)",
        (connector, packet_id, seen_at),
    )


# ---- MeshCore-family connector clients ---------------------------------
#
# Two kinds (KIND_CORESCOPE, KIND_BEACON) share one identity-resolution
# model -- a channel-scoped message feed plus a public-key node
# directory (see the module docstring's MeshCore section) -- even though
# their upstream APIs agree on almost nothing else: field names,
# timestamp encoding, how a channel is even addressed. Both clients
# below implement the same small two-method shape:
#
#   fetch_messages(channel) -> list of NORMALIZED message dicts, each
#     carrying at minimum packet_id (str), sender_name (raw, un-
#     normalized display name or None), text, and ts (epoch seconds,
#     or None if the upstream timestamp was missing/unparseable).
#   fetch_directory(limit)  -> list of directory entries carrying at
#     minimum name/public_key (see each class for what else it carries).
#
# Normalizing HERE, once, at the one place each upstream's raw shape
# enters this module, is what lets every downstream reader --
# _process_mc_message, _build_directory_bridge, _resolve_mc_identities --
# stay completely kind-agnostic: none of them know or care whether a
# given message/directory entry came from a CoreScope instance or a
# Beacon instance, and a Beacon directory + a CoreScope directory can be
# unioned together (CheckinPoller's cross-connector identity fallback)
# with no special-casing at all, because both already agree on shape by
# the time either function sees them.
#
# packet_id is filtered to a valid one at THIS boundary, not downstream:
# an upstream message with no usable id can never be dedup-tracked or
# awarded no matter what else it carries, so dropping it here (rather
# than handing it to _process_mc_message to discover that) is a pure
# no-op for behavior -- exactly the same messages end up silently
# skipped either way, see below for why a mark-seen skip and a drop-at-
# the-client skip are equivalent when nothing ever reads or writes a
# checkin_seen_message row for either case.


class CoreScopeClient:
    """HTTP client for one CoreScope instance's two check-in feeds: a
    channel's messages, and the public-key node directory used by the
    identity bridge (see the module docstring). Not
    app/meshview_client.py's MeshviewClient -- that is built around a
    different host's /api/nodes, /api/packets, /api/packets_seen
    shapes -- but it follows the same conventions that client does: a
    real User-Agent (this host rejects requests without one, confirmed
    against production) and a bounded timeout. Tolerant of the upstream
    being slow or down by design, not by accident: a failed request logs
    and returns an empty result rather than raising into the poll loop,
    since the poll loop already retries on its own short interval --
    unlike MeshviewClient's own in-request retry/backoff, a second retry
    layer here would just be redundant.

    One instance per DISTINCT connector_url, not per net -- see
    CheckinPoller._mc_client_for below, which caches these by URL so two
    nets on the same CoreScope instance (different channels) share one
    connection pool instead of opening a second one to the same host.
    `base_url` is always net-supplied now (there is no longer a single
    settings.mc_checkin_base_url a bare CoreScopeClient() could default
    to -- that setting is only ever read once, by seed_nets_from_env,
    to create the FIRST such net).
    """

    def __init__(self, base_url: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={"Accept": "application/json", "User-Agent": "meshwars/1.0"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> dict | None:
        try:
            r = await self._client.get(path, params=params)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            log.warning("checkin: corescope request %s failed: %s", path, e)
            return None

    async def fetch_messages(self, channel: str, directory_refresh_seconds: int = 900) -> list[dict]:
        """Newest 100 messages in `channel`, oldest first, normalized to
        packet_id/sender_name/text/ts. `channel` is percent-encoded here
        (not by the caller) -- it contains a literal "#", which is a URL
        fragment separator and must never reach httpx unescaped or
        everything after it would be silently dropped from the request.

        `directory_refresh_seconds` is accepted and ignored -- CoreScope
        has no channel-name-to-id cache to keep fresh (a channel IS its
        name here, see BeaconClient for the kind that needs this), but
        both clients take the same call signature so CheckinPoller never
        has to branch on kind just to make the call.
        """
        data = await self._get(f"/api/channels/{quote(channel, safe='')}/messages")
        if not isinstance(data, dict):
            return []
        raw = data.get("messages")
        if not isinstance(raw, list):
            return []
        out: list[dict] = []
        for m in raw:
            if not isinstance(m, dict):
                continue
            packet_id = m.get("packetId")
            if not isinstance(packet_id, int):
                # No usable id -- see this section's header comment for
                # why dropping it here is behaviorally identical to the
                # old per-message `return` inside _process_mc_message.
                continue
            out.append({
                "packet_id": str(packet_id),
                "sender_name": m.get("sender"),
                "text": m.get("text"),
                "ts": _parse_iso_ts(m.get("timestamp")),
            })
        return out

    async def fetch_directory(self, limit: int) -> list[dict]:
        """Up to `limit` directory entries, passed through as-is --
        CoreScope's /api/nodes shape already carries every field this
        module's readers need (name, public_key for the identity bridge;
        role, last_seen, lat, lon for companion_directory_entries' node
        picker), so there is nothing to translate here, unlike
        BeaconClient's directory below which only ever carries
        name/public_key. `limit` is the one paging parameter this
        endpoint actually honors -- per-page/page/size and similar are
        silently ignored, so passing anything else would look like it
        worked while quietly returning the default page.
        """
        data = await self._get("/api/nodes", {"limit": limit})
        if not isinstance(data, dict):
            return []
        nodes = data.get("nodes")
        return nodes if isinstance(nodes, list) else []

    async def fetch_directory_search(self, name: str, limit: int) -> list[dict]:
        """Single on-demand, name-filtered directory fetch: GET
        /api/nodes?search=<name>&limit=<limit>. A deliberately separate
        method from fetch_directory() above rather than an optional
        argument on it, because the two back completely different
        callers with different freshness needs -- fetch_directory()
        feeds CheckinPoller's directory cache
        (config['directory_refresh_seconds'], 15 minutes by default);
        this feeds app/checkin.py's confirm_scan_connector(), which
        app/checkin_api.py's node-confirmation flow calls on-demand,
        every few seconds, for up to a 5-minute window. That flow must
        NEVER go through CheckinPoller's cache -- a fresh advert could
        not be seen until long after the confirmation window already
        closed. `search` is confirmed to be a SUBSTRING match upstream
        (querying "abc" also returns "abcdef"), so this only handles
        the HTTP round trip -- callers re-filter to an exact
        normalize_sender_name() match themselves (see
        confirm_scan_connector below).
        """
        data = await self._get("/api/nodes", {"search": name, "limit": limit})
        if not isinstance(data, dict):
            return []
        nodes = data.get("nodes")
        return nodes if isinstance(nodes, list) else []


class BeaconClient:
    """HTTP client for one MeshCore-Beacon (beacon-server) instance --
    the second KIND_BEACON option for a MeshCore-family net, alongside
    CoreScopeClient above. Same tolerant-of-slow-or-down style and same
    two-method shape (fetch_messages/fetch_directory), but talks a
    materially different API in four specific, non-obvious ways:

    1. Channels are addressed by a NUMERIC, INSTANCE-LOCAL id, never a
       name -- checkin_net.channel still stores the human channel NAME
       (the same column corescope nets use; see app/db.py), so every
       message fetch has to resolve name -> id first. That id means
       nothing on a different Beacon instance, or even the same
       instance after a channel is re-created, so it is never persisted
       to the database -- only cached in memory, per client instance
       (i.e. per connector_url, the same scope the HTTP connection pool
       already has), and re-resolved on a miss or once the cache goes
       stale. See _resolve_channel_id below.
    2. Only a channel with keyKnown: true carries a `name` at all -- the
       rest have no name and can therefore never be matched by one, so
       they can never be configured as a net's `channel` in the first
       place; _refresh_channel_cache filters to exactly those.
    3. Message timestamps (`sentAt`) are epoch MILLISECONDS, not the
       RFC3339 strings CoreScope uses -- divided down to seconds at
       normalization time so nothing downstream ever has to know which
       kind produced a given message.
    4. Node-directory pagination is CURSOR-based (`nextCursor`/
       `hasMore`), not a single limit-capped page like CoreScope's --
       see fetch_directory.

    If the configured channel name cannot be resolved to a keyKnown
    channel, fetch_messages raises a plain RuntimeError with a clean
    message rather than returning silently -- CheckinPoller's existing
    per-feed try/except (the same one that already catches a connector
    being down) turns that into exactly the per-net last_poll_error this
    needs, with no separate error-plumbing path required.
    """

    def __init__(self, base_url: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={"Accept": "application/json", "User-Agent": "meshwars/1.0"},
        )
        # channel NAME -> instance-local numeric id, and when that cache
        # was last (re)populated (monotonic clock, same convention
        # CheckinPoller._mc_directory_fetched_at uses) -- see
        # _resolve_channel_id. Deliberately scoped to this client (one
        # Beacon instance), not per-net: two nets on the same Beacon
        # connector share this cache exactly the way they already share
        # the underlying HTTP connection pool.
        self._channel_cache: dict[str, int] = {}
        self._channel_cache_fetched_at: float = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> dict | None:
        try:
            r = await self._client.get(path, params=params)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            log.warning("checkin: beacon request %s failed: %s", path, e)
            return None

    async def _refresh_channel_cache(self) -> None:
        data = await self._get("/api/v1/channels", {"limit": 1000})
        if not isinstance(data, dict):
            return  # request failed -- leave the previous cache (if any) in place
        items = data.get("items")
        if not isinstance(items, list):
            return
        cache: dict[str, int] = {}
        for item in items:
            if not isinstance(item, dict) or item.get("keyKnown") is not True:
                continue  # no `name` at all on these -- see class docstring, point 2
            name = item.get("name")
            cid = item.get("id")
            if isinstance(name, str) and name and isinstance(cid, int):
                cache[name] = cid
        self._channel_cache = cache
        self._channel_cache_fetched_at = time.monotonic()

    async def _resolve_channel_id(self, channel_name: str, refresh_seconds: int) -> int | None:
        """channel NAME -> this instance's current numeric id for it, or
        None if no keyKnown channel by that name exists. Refreshes the
        cache if it's past `refresh_seconds` old (reusing
        checkin_config.directory_refresh_seconds -- the same knob that
        already governs how often the MeshCore directory itself is
        re-fetched, rather than inventing a second timing knob for a
        second kind of cache) -- and, separately, re-resolves on a
        cache MISS even when the cache is still fresh, since a channel
        an admin just created should not have to wait out a full
        refresh interval before its net can ever poll successfully.
        """
        stale = (time.monotonic() - self._channel_cache_fetched_at) >= refresh_seconds
        if stale or not self._channel_cache:
            await self._refresh_channel_cache()
        cid = self._channel_cache.get(channel_name)
        if cid is None and self._channel_cache_fetched_at:
            await self._refresh_channel_cache()  # miss on an otherwise-fresh cache -- try once more
            cid = self._channel_cache.get(channel_name)
        return cid

    async def fetch_messages(self, channel: str, directory_refresh_seconds: int = 900) -> list[dict]:
        """Newest 100 messages in the channel named `channel`, normalized
        to packet_id/sender_name/text/ts. See class docstring points 1
        and 3 for the id-resolution and millisecond-timestamp handling.
        """
        channel_id = await self._resolve_channel_id(channel, directory_refresh_seconds)
        if channel_id is None:
            # Clean, per-net error -- NOT a bug in this client -- see
            # class docstring for why raising here (rather than
            # swallowing to an empty list) is exactly what turns into a
            # useful last_poll_error on the calling net.
            raise RuntimeError(
                "channel not found or key unknown on this Beacon instance: %r" % channel
            )
        data = await self._get(f"/api/v1/channels/{channel_id}/messages", {"limit": 100})
        if not isinstance(data, dict):
            return []
        raw = data.get("items")
        if not isinstance(raw, list):
            return []
        out: list[dict] = []
        for m in raw:
            if not isinstance(m, dict):
                continue
            packet_id = m.get("id")
            if not isinstance(packet_id, int):
                continue  # see the client-abstraction header comment above
            sent_at = m.get("sentAt")
            ts = int(sent_at // 1000) if isinstance(sent_at, (int, float)) else None
            out.append({
                "packet_id": str(packet_id),
                "sender_name": m.get("senderName"),
                "text": m.get("content"),
                "ts": ts,
            })
        return out

    async def fetch_directory(self, limit: int) -> list[dict]:
        """Up to `limit` directory entries, normalized to {name,
        public_key} -- the minimum the identity bridge needs (see
        _build_directory_bridge). Unlike CoreScopeClient's directory,
        Beacon's node objects carry no role/last_seen/lat/lon this
        module currently reads, so there is nothing else to carry
        through; a null `name` is skipped outright (the field is
        nullable on this endpoint) rather than producing an entry
        nothing could ever match by name.

        Pagination is CURSOR-based, not a single capped page: follow
        `nextCursor` while `hasMore` is true, up to a fixed page cap so
        a huge or misbehaving instance can never spin this loop
        forever -- 50 pages is far more than any real deployment needs
        today, a safety rail rather than an expected ceiling.
        """
        out: list[dict] = []
        cursor: str | None = None
        for _ in range(50):
            params: dict = {"limit": limit}
            if cursor is not None:
                params["cursor"] = cursor
            data = await self._get("/api/v1/nodes", params)
            if not isinstance(data, dict):
                break
            items = data.get("items")
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("name")
                    pubkey = item.get("publicKey")
                    if not isinstance(name, str) or not name:
                        continue  # nullable on this endpoint -- see docstring
                    if not isinstance(pubkey, str) or not pubkey:
                        continue
                    out.append({"name": name, "public_key": pubkey})
            if len(out) >= limit or data.get("hasMore") is not True:
                break
            cursor = data.get("nextCursor")
            if cursor is None:
                break
        return out[:limit]

    async def fetch_directory_search(self, name: str, limit: int) -> list[dict]:
        """Single on-demand, name-filtered directory fetch: GET
        /api/v1/nodes?name=<name>&limit=<limit> -- Beacon's OWN name
        filter (confirmed working; `search` is silently ignored on
        this API, unlike CoreScope). Same role as
        CoreScopeClient.fetch_directory_search above -- see that
        method's docstring for why this must never back
        fetch_directory()'s cache-fed caller -- but deliberately a
        single page, no cursor-follow: unlike fetch_directory()'s full-
        directory sync, a typed node name is never going to have more
        matches than one page can hold, and iatas/lastHeard are read
        straight off each raw item here (unlike fetch_directory()'s
        name/public_key-only shape), since that is exactly the
        freshness signal app/checkin.py's confirm_scan_connector needs
        and fetch_directory() has never had a reason to keep.
        """
        data = await self._get("/api/v1/nodes", {"name": name, "limit": limit})
        if not isinstance(data, dict):
            return []
        items = data.get("items")
        return items if isinstance(items, list) else []


# ---- MeshCore: node confirmation (app/checkin_api.py's ------------------
# POST/GET/DELETE /api/checkin/confirm/*) -----------------------------
#
# Support code for proving possession of a specific radio, as opposed to
# merely asserting a name -- see app/db.py's mc_node_confirmation comment
# for the full motivation and the shape of what gets stored. The one
# thing every function below shares: a fetch through here is ALWAYS an
# on-demand, name-filtered, uncached round trip -- never
# CheckinPoller's own directory_snapshot/_refresh_mc_directory_if_stale,
# which is fine (good, even) for identity resolution on a message that
# already arrived, but useless for "is this radio transmitting RIGHT
# NOW" inside a five-minute window that cache's own refresh interval
# comfortably outlives.

_CONFIRM_SEARCH_LIMIT = 50


def _mc_node_last_heard_epoch(kind: str, node: dict) -> int | None:
    """Epoch seconds a single raw directory node (CoreScope or Beacon
    shape, whichever `kind` says) was last heard, or None if neither
    shape yields one -- confirm_scan_connector() below treats None as
    "never heard," not "just now," so a node with no usable timestamp
    can never look like a fresh advert by accident.

    CoreScope: `last_heard`, falling back to `last_seen`, both ISO-8601
    UTC strings parsed by _parse_iso_ts (same parser
    companion_directory_entries' upstream data already goes through
    elsewhere in this module).

    Beacon: max(iatas[].lastHeard), epoch MILLISECONDS -- Beacon's node
    objects carry no top-level timestamp at all (see BeaconClient's own
    class docstring); `iatas` absent or empty means None, not zero.
    Beacon's top-level `stale` boolean is deliberately never read here
    -- it is a ~24-hour staleness threshold, useless for telling a
    30-second-old advert apart from a 20-hour-old one, which is exactly
    the distinction a confirmation window needs.
    """
    if kind == KIND_BEACON:
        iatas = node.get("iatas")
        if not isinstance(iatas, list) or not iatas:
            return None
        values = [
            i.get("lastHeard") for i in iatas
            if isinstance(i, dict) and isinstance(i.get("lastHeard"), (int, float))
        ]
        if not values:
            return None
        return int(max(values) // 1000)
    ts = _parse_iso_ts(node.get("last_heard"))
    if ts is None:
        ts = _parse_iso_ts(node.get("last_seen"))
    return ts


async def confirm_scan_connector(kind: str, connector_url: str, name: str) -> list[dict]:
    """One on-demand, name-filtered directory fetch against a single
    MeshCore-family connector, normalized to {public_key, name, role,
    last_heard_epoch} -- the shape app/checkin_api.py's confirmation
    endpoints and mc_node_confirmation.baseline both key off. See this
    section's header comment for why this is never CheckinPoller's
    cached directory.

    A single, short-lived client, unlike CheckinPoller's pooled
    _mc_client_for -- confirmation is a low-frequency, human-driven
    flow (open a window, poll status every few seconds, for at most
    five minutes), never CheckinPoller's steady 30-second cycle, so
    there is no steady-state connection worth keeping open between
    calls the way CheckinPoller's own pooling is.  Tolerant of a down
    connector the same way CheckinPoller already is: a failed request
    logs and returns an empty list rather than raising, so one bad
    connector can never take out every OTHER connector's chance to find
    the node (see confirm_scan_all_connectors below, which unions this
    across every configured connector).

    Filtering is substring on the upstream side (see
    CoreScopeClient.fetch_directory_search /
    BeaconClient.fetch_directory_search) -- re-filtered here to an
    EXACT normalize_sender_name() match, so a search for "bob" can
    never surface "bobby" as a candidate. A node whose public key
    doesn't validate as a real 64-hex key (node_ref.py's
    normalize_public_key) is dropped rather than passed through --
    nothing downstream of this function can do anything useful with a
    key it can't derive a node_ref from.
    """
    target = normalize_sender_name(name)
    if target is None:
        return []

    client: CoreScopeClient | BeaconClient
    client = BeaconClient(connector_url) if kind == KIND_BEACON else CoreScopeClient(connector_url)
    try:
        raw = await client.fetch_directory_search(name, _CONFIRM_SEARCH_LIMIT)
    except Exception:
        log.warning("checkin: confirm scan failed for connector %s (%s)", connector_url, kind)
        raw = []
    finally:
        await client.aclose()

    out: list[dict] = []
    for node in raw:
        if not isinstance(node, dict):
            continue
        node_name = node.get("name")
        if not isinstance(node_name, str) or normalize_sender_name(node_name) != target:
            continue  # substring match upstream -- see fetch_directory_search docstrings
        raw_pubkey = node.get("publicKey") if kind == KIND_BEACON else node.get("public_key")
        public_key = normalize_public_key(raw_pubkey)
        if public_key is None:
            continue
        role = node.get("nodeTypeName") if kind == KIND_BEACON else node.get("role")
        out.append({
            "public_key": public_key,
            "name": node_name,
            "role": role,
            "last_heard_epoch": _mc_node_last_heard_epoch(kind, node),
        })
    return out


async def confirm_scan_all_connectors(conn, name: str) -> list[dict]:
    """confirm_scan_connector() above, unioned across every distinct
    (kind, connector_url) among this deployment's MeshCore-family
    checkin_net rows -- regardless of whether that net's OWN weekly
    window is open right now. Confirmation has to work any day, not
    just net night: it is proving who owns a radio, not earning a
    check-in, and app/checkin_api.py's endpoints never look at a net's
    weekday/start_hour/end_hour at all.

    Distinct on (kind, connector_url), not on net id -- the same
    "share by connector, not by net" idea CheckinPoller's own
    _mc_client_for pooling already relies on: two nets configured
    against the same CoreScope or Beacon instance (different channels)
    would otherwise scan the identical directory twice for nothing.
    Every connector is scanned concurrently (asyncio.gather) so a
    deployment with several configured connectors doesn't pay for them
    one at a time inside a status poll a browser is waiting on.
    """
    rows = conn.execute(
        "SELECT DISTINCT kind, connector_url FROM checkin_net WHERE kind IN (?, ?)",
        (KIND_CORESCOPE, KIND_BEACON),
    ).fetchall()
    if not rows:
        return []
    results = await asyncio.gather(
        *[confirm_scan_connector(r["kind"], r["connector_url"], name) for r in rows]
    )
    out: list[dict] = []
    for r in results:
        out.extend(r)
    return out


# ---- Meshtastic: node confirmation (app/checkin_api.py's ----------------
# POST/GET/DELETE /api/checkin/confirm/*, protocol='mt') -------------------
#
# The Meshtastic counterpart to the MeshCore section above -- same job
# (prove a player is really holding a specific radio before binding it),
# same five-minute window, but the OPPOSITE identity problem, and a
# correspondingly simpler proof. MeshCore channel messages carry no
# per-sender key, only a free-text name that could already be shared or
# stale, so that flow has to broadcast the player's OWN chosen name and
# tell a genuinely fresh advert apart from one that was already on the
# mesh (the baseline/_fresh_candidates machinery above). Meshtastic
# packets carry a real sender node id on every message -- the thing
# that's missing here is not identity, it's PROOF that a specific
# person, right now, controls a specific radio. That proof is a short,
# high-entropy, freshly-generated CODE (never a name the player already
# had) the player is asked to broadcast: nothing on the mesh could have
# posted that exact text before this window opened, so any message
# containing it is unambiguous proof the sender radio is under this
# player's control -- no baseline snapshot, no "was it already
# advertising" comparison, needed at all. See app/db.py's
# mt_node_confirmation comment for the full contrast.

# No 0/O or 1/l/I -- both pairs are visually near-identical in most UI
# fonts and on a phone screen a player is squinting at while standing
# next to a radio; dropping them costs a small, irrelevant amount of
# entropy (32 candidates per character instead of 36) in exchange for a
# code nobody mistypes because they can't tell two characters apart.
_MT_CONFIRM_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_MT_CONFIRM_CODE_LENGTH = 7
_MT_CONFIRM_CODE_PREFIX = "mw-"
_MT_CONFIRM_CODE_GEN_ATTEMPTS = 20  # see issue_unique_mt_confirm_code -- a safety rail, not an expected ceiling


def _generate_mt_confirm_code() -> str:
    """One candidate code, e.g. "mw-3h7fpk4" -- `secrets.choice`, never
    `random`, because this is a capability token (whoever broadcasts it
    proves control of a radio and gets it bound to their account), not
    a cosmetic id; `random`'s Mersenne Twister is not safe for anything
    that has to resist being guessed.
    """
    body = "".join(secrets.choice(_MT_CONFIRM_CODE_ALPHABET) for _ in range(_MT_CONFIRM_CODE_LENGTH))
    return f"{_MT_CONFIRM_CODE_PREFIX}{body}"


def issue_unique_mt_confirm_code(conn) -> str:
    """A fresh code guaranteed unused by any OTHER row currently in
    mt_node_confirmation -- open window or an expired one this player's
    own confirm_status/confirm_accept hasn't gotten around to cleaning
    up yet (see app/checkin_api.py: both delete a row the moment they
    notice it's expired, but nothing sweeps the table proactively, so a
    stale row can sit there for a while). Collision odds at this
    alphabet/length (32^7, ~34 billion) are already astronomically low
    -- this loop exists as a correctness backstop, not because a
    collision is expected in practice, the same reasoning
    mc_node_confirmation.code's own UNIQUE constraint gives for being a
    backstop rather than the primary mechanism.

    Callers are expected to hold this connection's write lock (BEGIN
    IMMEDIATE) across both this call and the INSERT that consumes its
    result, the same way app/checkin_api.py's confirm_start already
    wraps its MeshCore delete-then-insert -- SQLite serializes writers
    on one file, so nothing else can steal a code between the check
    here and that INSERT as long as both happen inside one transaction.
    """
    for _ in range(_MT_CONFIRM_CODE_GEN_ATTEMPTS):
        code = _generate_mt_confirm_code()
        exists = conn.execute("SELECT 1 FROM mt_node_confirmation WHERE code = ?", (code,)).fetchone()
        if exists is None:
            return code
    # Should be unreachable at this alphabet/length -- see docstring.
    raise RuntimeError("could not generate a unique Meshtastic confirmation code")


async def mt_confirm_scan_connector(kind: str, connector_url: str, code: str, conn) -> list[dict]:
    """One on-demand scan of a single Meshtastic-family connector
    (KIND_MESHVIEW or KIND_MQTT) for any message containing `code`,
    normalized to {node_ref, node_id, last_heard_epoch} -- `name` is
    filled in afterwards by mt_confirm_scan_all_connectors, which is
    the one place that can look it up once, across every connector's
    matches together, rather than once per connector here.

    Deliberately NOT CheckinPoller's steady 30-second poll cycle, for
    the same reason confirm_scan_connector (MeshCore, above) is not
    CheckinPoller's cached directory: that cycle applies a net's own
    window and hashtag before a message is ever kept, and neither
    constraint has anything to do with node confirmation, which has to
    work at any moment regardless of whether any net's window happens
    to be open. For KIND_MESHVIEW this means a fresh, short-lived
    MeshviewClient (mirroring CoreScopeClient/BeaconClient's own
    single-request-then-close style above, not CheckinPoller's pooled,
    reused one) calling packets(portnum=1) -- the same upstream call
    _poll_mt_connector makes, just with no window/hashtag filtering
    applied to what comes back. For KIND_MQTT there is no upstream call
    at all: mqtt_message_buffer already holds every currently-buffered
    message for this connector regardless of any net's window (see
    _fetch_mqtt_messages's own docstring for why), so this is a plain
    read of the same rows through the connection the caller already
    holds.

    Tolerant of a down connector the same way every other client in
    this module is: a failed request logs and returns an empty list
    rather than raising, so one bad connector can never take out every
    OTHER connector's chance to find the code (see
    mt_confirm_scan_all_connectors, which unions this across every
    configured connector).

    Matching is case-insensitive substring, on the code INCLUDING its
    "mw-" prefix, against the raw message text -- deliberately not
    anchored to the whole message, since a player may type it in a
    sentence ("here's my code mw-3h7fpk4 fkey") rather than send it
    bare. Multiple matching messages from the same node_ref keep only
    the one with the LATEST timestamp -- a player retrying after a
    typo, or a mesh redelivering the same text, must never look like
    two different candidates.
    """
    target = code.strip().lower()
    if not target:
        return []

    raw_msgs: list[tuple[object, object, object]] = []  # (sender_id, text, ts)
    if kind == KIND_MQTT:
        rows = conn.execute(
            "SELECT from_node, text, ts FROM mqtt_message_buffer WHERE connector = ?",
            (connector_url,),
        ).fetchall()
        raw_msgs = [(r["from_node"], r["text"], r["ts"]) for r in rows]
    else:
        client = MeshviewClient(base_url=connector_url)
        try:
            packets = await client.packets(portnum=1, limit=100)
        except Exception:
            log.warning("checkin: mt confirm scan failed for connector %s", connector_url)
            packets = []
        finally:
            await client.aclose()
        for pkt in packets:
            if not isinstance(pkt, dict):
                continue
            import_us = pkt.get("import_time_us")
            ts = int(import_us / 1_000_000) if isinstance(import_us, (int, float)) else None
            raw_msgs.append((pkt.get("from_node_id"), pkt.get("payload"), ts))

    # Local import: see _process_mt_packet's own comment below for why
    # this has to be deferred rather than a top-level import (app/api.py
    # -> app/checkin_api.py -> app/checkin.py -> app/ingest.py ->
    # app/api.py would otherwise close a cycle) -- reused as-is, per
    # this module's own docstring, rather than reimplemented.
    from .ingest import _bare_node_ref

    matches: dict[str, dict] = {}
    for sender_id, text, ts in raw_msgs:
        if not isinstance(sender_id, int) or not isinstance(text, str):
            continue
        if target not in text.lower():
            continue
        node_ref = _bare_node_ref(sender_id)
        heard = ts if isinstance(ts, int) else None
        current = matches.get(node_ref)
        if current is None or (heard is not None and (current["last_heard_epoch"] is None or heard > current["last_heard_epoch"])):
            matches[node_ref] = {"node_ref": node_ref, "node_id": sender_id, "last_heard_epoch": heard}
    return list(matches.values())


async def mt_confirm_scan_all_connectors(conn, code: str) -> list[dict]:
    """mt_confirm_scan_connector() above, unioned across every distinct
    (kind, connector_url) among this deployment's Meshtastic-family
    checkin_net rows (KIND_MESHVIEW, KIND_MQTT) -- regardless of
    whether any of those nets' own weekly windows are open right now,
    for the same reason confirm_scan_all_connectors (MeshCore) scans
    regardless of net window: this is proving who holds a radio, not
    earning a check-in.

    Distinct on (kind, connector_url), not on net id -- same "share by
    connector, not by net" reasoning confirm_scan_all_connectors and
    CheckinPoller's own client pooling already rely on. Every connector
    is scanned concurrently (asyncio.gather), same as the MeshCore
    version.

    Merges connector-level results by node_ref, keeping whichever match
    has the latest last_heard_epoch -- the same node could plausibly
    show up via more than one connector (e.g. a meshview instance and
    an MQTT broker both hearing the same broadcast), and only one
    candidate per physical radio should ever reach a player.

    `name`, absent from mt_confirm_scan_connector's own output, is
    filled in here as a single bulk node_seen lookup (season_id,
    node_id) against the currently active Meshtastic season -- the same
    roster app/checkin.py's mt_roster_entries() and
    _load_mt_registered_players() already read, just narrowed to the
    handful of node ids this scan actually matched, rather than the
    whole roster. None (not '', not omitted) when no active season
    exists or a matched node_id has never appeared in node_seen at
    all -- a player's radio genuinely can be unrecognized here (it has
    never sent a position/telemetry packet meshview logged a name
    for), and the confirmation flow does not need a name to work, only
    to display one when it has one.
    """
    rows = conn.execute(
        "SELECT DISTINCT kind, connector_url FROM checkin_net WHERE kind IN (?, ?)",
        (KIND_MESHVIEW, KIND_MQTT),
    ).fetchall()
    if not rows:
        return []
    scan_results = await asyncio.gather(
        *[mt_confirm_scan_connector(r["kind"], r["connector_url"], code, conn) for r in rows]
    )

    merged: dict[str, dict] = {}
    for r in scan_results:
        for m in r:
            current = merged.get(m["node_ref"])
            if current is None or (m["last_heard_epoch"] is not None and (current["last_heard_epoch"] is None or m["last_heard_epoch"] > current["last_heard_epoch"])):
                merged[m["node_ref"]] = m

    if merged:
        season = active_season(conn, MT_PROTOCOL)
        if season:
            node_id_by_ref = {m["node_ref"]: m["node_id"] for m in merged.values()}
            placeholders = ",".join("?" for _ in node_id_by_ref)
            name_rows = conn.execute(
                f"SELECT node_id, name FROM node_seen WHERE season_id = ? AND node_id IN ({placeholders})",
                (season["id"], *node_id_by_ref.values()),
            ).fetchall()
            name_by_node_id = {r["node_id"]: r["name"] for r in name_rows}
            for ref, node_id in node_id_by_ref.items():
                merged[ref]["name"] = name_by_node_id.get(node_id)
        else:
            for m in merged.values():
                m["name"] = None
    return list(merged.values())


# ---- MeshCore: identity resolution -------------------------------------


def _record_node_name(conn, connector: str, node_ref: str, player_id: int, name: str, now: int) -> None:
    """Persist that MeshCore contact `node_ref` (bound to `player_id`)
    currently resolves to `name` on `connector` (checkin_node_name,
    app/db.py), and log the moment THAT NODE's name changes.

    Why this matters more than an ordinary bookkeeping table: a rename
    is the EXACT moment check-ins matched against the old name would
    start silently going uncredited (see the module docstring --
    resolution is name-matched against the directory, so the old
    binding stops matching the instant the display name changes), and
    nothing else in this schema records what a resolved name used to
    be. Making that moment a log line (and a row app/admin_ops.py's
    _attention can surface) is what turns an invisible failure into a
    visible one, without changing resolution or awarding behavior at
    all -- this function only ever observes and records; it plays no
    part in deciding who gets credited.

    Keyed on (connector, node_ref), NOT (connector, player_id) -- see
    checkin_node_name's own comment in app/db.py for why: a player can
    hold more than one bound MeshCore contact, each with its own
    display name, and that is normal, not a rename. player_id is
    carried through only so a row is self-describing to a reader.

    First sighting of a (connector, node_ref) pair is an INSERT, not a
    "change" -- there is no previous name to have drifted from, so
    changed_at/previous_name are left at their column defaults (NULL,
    '') rather than manufactured. Only a genuinely DIFFERENT name on a
    later call updates the row and fires the log line; the common case
    -- the same name resolving again this cycle -- is a no-op UPDATE-of-
    nothing, not skipped by a separate read first, since the WHERE
    clause below already makes that the cheap path.
    """
    cur = conn.execute(
        "UPDATE checkin_node_name SET previous_name = name, name = ?, changed_at = ? "
        " WHERE connector = ? AND node_ref = ? AND name != ?",
        (name, now, connector, node_ref, name),
    )
    if cur.rowcount:
        row = conn.execute(
            "SELECT previous_name FROM checkin_node_name WHERE connector = ? AND node_ref = ?",
            (connector, node_ref),
        ).fetchone()
        log.info(
            "checkin: player %d's MeshCore node %s on %s changed name from %r to %r",
            player_id, node_ref, connector, row["previous_name"] if row else None, name,
        )
        return
    conn.execute(
        "INSERT OR IGNORE INTO checkin_node_name(connector, node_ref, player_id, name, first_seen) "
        "VALUES (?, ?, ?, ?, ?)",
        (connector, node_ref, player_id, name, now),
    )


def _index_mc_directory(
    directory_nodes: list[dict],
) -> tuple[dict[str, list[dict]], set[str]]:
    """Index one MeshCore directory snapshot for key/name lookups:
    by_prefix (8-hex public-key prefix -> matching directory entries)
    and ambiguous_names (display names shared by more than one public
    key anywhere in the directory). Both of _build_directory_bridge's
    ambiguity rules below read directly off these two structures, so
    this is the one place the indexing happens -- anything else that
    needs to know WHY a particular contact did or did not resolve
    (mc_contact_status below, and app/account_api.py's checkin-health
    endpoint that calls it) builds on these same indices rather than
    re-deriving the ambiguity rules a second time.
    """
    by_name: dict[str, set[str]] = {}
    by_prefix: dict[str, list[dict]] = {}
    for node in directory_nodes:
        name = node.get("name")
        pubkey = node.get("public_key")
        if not isinstance(name, str) or not name or not isinstance(pubkey, str) or len(pubkey) < 8:
            continue
        pubkey = pubkey.lower()
        by_name.setdefault(name, set()).add(pubkey)
        by_prefix.setdefault(pubkey[:8], []).append(node)

    ambiguous_names = {name for name, keys in by_name.items() if len(keys) > 1}
    return by_prefix, ambiguous_names


def mc_contact_status(
    by_prefix: dict[str, list[dict]], ambiguous_names: set[str], contact: str,
) -> dict:
    """Classify ONE bound MeshCore contact (an 8-hex node_ref) against
    an already-indexed directory (_index_mc_directory), applying the
    exact same two ambiguity rules _build_directory_bridge's own loop
    applies -- this function only tells you what it would decide, not a
    second opinion. Unlike the bridge, which discards everything but a
    clean resolution, every outcome is reported here:

    - "resolved": exactly one directory entry matches, name is unique --
      `name` is what it resolves to.
    - "not_in_directory": no directory entry's key matches this contact
      at all.
    - "key_ambiguous": more than one directory entry shares this
      contact's 8-hex prefix -- `match_count` says how many.
    - "name_ambiguous": the key matched exactly one entry, but that
      entry's display name is also used by a different public key
      elsewhere in the directory -- `name` is the shared name.

    Built for app/account_api.py's checkin-health endpoint, which needs
    this per-contact detail to explain a resolution failure to a
    player; _build_directory_bridge itself only needs the final
    bridge-wide name -> player_id map, so it calls this the same way but
    only keeps the "resolved" case.
    """
    matches = by_prefix.get(contact, [])
    if not matches:
        return {"status": "not_in_directory", "name": None, "match_count": 0}
    if len(matches) > 1:
        return {"status": "key_ambiguous", "name": None, "match_count": len(matches)}
    name = matches[0].get("name")
    if name in ambiguous_names:
        return {"status": "name_ambiguous", "name": name, "match_count": 1}
    return {"status": "resolved", "name": name, "match_count": 1}


def _build_directory_bridge(
    conn, directory_nodes: list[dict], connector: str | None = None, now: int | None = None,
) -> dict[str, int]:
    """normalized display name -> player_id, resolved by walking each
    non-disabled player's bound MeshCore radio contact (player_node,
    protocol='mc') through to `directory_nodes` (a live.mwmesh.com
    /api/nodes snapshot). See the module docstring for why this join,
    anchored on a public key rather than a name, is safe to trust
    automatically with no separate registration step.

    Refuses rather than guesses on either kind of ambiguity, logging
    and skipping instead of picking one:

    - A bound contact whose 8-hex prefix matches more than one (or
      zero) directory public keys is skipped entirely -- if two
      different real nodes share a prefix, or the radio the player
      bound has never shown up in the directory at all, there is no
      way to tell which (if any) entry is really theirs.
    - A display name shared by more than one public key ANYWHERE in the
      directory -- not just among players who happen to have a bound
      contact -- is refused for every player it would otherwise resolve
      to. A repeater or a room server sharing a player's chosen display
      name makes that name unsafe to attribute to anyone, exactly the
      same as two different companion nodes sharing it would; the
      directory carries 3 such duplicate names in production today.

    A wrong attribution here is worse than a missed point, so every one
    of the skips above is a no-op for that player (they simply don't
    resolve through the bridge this cycle -- a later unambiguous
    directory state, or a node-confirmation redo, still works), never a
    best-effort pick.

    `connector`/`now`: when given, every contact this bridge resolves is
    also passed to _record_node_name against `connector` -- see that
    function for why. Only _resolve_mc_identities' PRIMARY pass (this
    net's own connector, see CheckinPoller._poll_mc_feed) supplies
    these; the cross-connector fallback pass below leaves them None,
    since `directory_nodes` there is already a union across every OTHER
    connector currently polled and no single connector identity applies
    to a name resolved from it. Recording nothing for that pass is not a
    gap: the SAME contact, if it resolves at all, resolves through its
    own net's primary pass on some cycle too (that is the common case
    this whole bridge is built around), which is what actually gets
    recorded.
    """
    by_prefix, ambiguous_names = _index_mc_directory(directory_nodes)

    rows = conn.execute(
        "SELECT pn.node_ref, pn.player_id FROM player_node pn "
        "JOIN player p ON p.player_id = pn.player_id "
        "WHERE pn.protocol = 'mc' AND p.disabled_at IS NULL"
    ).fetchall()

    bridge: dict[str, int] = {}
    for r in rows:
        contact = r["node_ref"]  # already bare lowercase 8-hex (app/node_ref.py)
        status = mc_contact_status(by_prefix, ambiguous_names, contact)
        if status["status"] == "key_ambiguous":
            log.warning(
                "checkin: mc contact %s matches %d directory public keys, "
                "refusing to resolve (ambiguous)", contact, status["match_count"],
            )
            continue
        if status["status"] == "not_in_directory":
            continue
        if status["status"] == "name_ambiguous":
            log.warning(
                "checkin: directory name %r is shared by multiple public keys, "
                "refusing to attribute it to player %d", status["name"], r["player_id"],
            )
            continue
        name = status["name"]
        normalized = normalize_sender_name(name)
        if normalized is None:
            continue
        bridge[normalized] = r["player_id"]
        if connector is not None:
            # See this function's own docstring for why only the
            # PRIMARY pass (connector supplied) records here.
            _record_node_name(conn, connector, contact, r["player_id"], name, now)
    return bridge


def _resolve_mc_identities(
    conn, primary_directory: list[dict], other_directories: list[list[dict]],
    primary_connector: str | None = None,
) -> dict[str, int]:
    """normalized sender name -> player_id, the single map
    _process_mc_message actually looks a check-in sender up in -- the
    key-based directory bridge (_build_directory_bridge), widened
    across every currently-polled connector. There is no second,
    weaker identity source layered underneath this one anymore -- see
    the module docstring's MeshCore section for why a bare typed name
    was retired rather than kept as a fallback.

    `primary_directory` is the feed's OWN connector's cached directory
    (see CheckinPoller._poll_mc_feed); `other_directories` is every
    OTHER connector's cached directory currently being polled this
    cycle, consulted only to WIDEN the search -- see the module
    docstring's "which directory" note under the MeshCore identity
    section. Both passes run through the exact same
    _build_directory_bridge, so both apply its ambiguity refusals
    identically; the only difference between them is which directory
    entries are on the table, and the primary pass's answers win over
    the cross-connector pass's on any remaining overlap (a player whose
    contact resolves on their own net's connector is never second-
    guessed by a match found elsewhere). A single connector (the common
    case today) makes other_directories empty and this collapses to
    exactly the original one-directory behavior.

    `primary_connector`, when given, is passed through to
    _build_directory_bridge's PRIMARY-pass call only (never the cross-
    connector pass) so it can record each resolved contact's current
    directory name (checkin_node_name, app/db.py) against a single,
    unambiguous connector -- see that function's own docstring for why
    the cross-connector pass, built from a union across other
    connectors, never does this.
    """
    now = int(time.time())
    primary_bridge = _build_directory_bridge(conn, primary_directory, connector=primary_connector, now=now)
    if other_directories:
        other_nodes = [node for nodes in other_directories for node in nodes]
        other_bridge = _build_directory_bridge(conn, other_nodes)
    else:
        other_bridge = {}
    # Primary wins any remaining overlap -- see the docstring above.
    for name, other_player_id in other_bridge.items():
        primary_player_id = primary_bridge.get(name)
        if primary_player_id is not None and primary_player_id != other_player_id:
            log.warning(
                "checkin: mc name %r resolves to player %d on the primary "
                "connector's bridge and player %d on another connector's "
                "bridge -- keeping the primary connector's player %d, "
                "discarding the other's player %d", name, primary_player_id,
                other_player_id, primary_player_id, other_player_id,
            )
    bridge = dict(other_bridge)
    bridge.update(primary_bridge)
    return bridge


def companion_directory_entries(directory_nodes: list[dict]) -> list[dict]:
    """Shape a cached CoreScope directory snapshot (possibly a union
    across every configured MeshCore connector -- see
    CheckinPoller.directory_snapshot below) into the "people only"
    picker list app/checkin_api.py's MeshCore node-picker endpoint
    serves -- filters to role == "companion" (repeaters and room
    servers are infrastructure and must never be offered as a person to
    pick), and reduces each entry to exactly what a player needs to
    recognise their own node and what the UI needs to disambiguate two
    companions sharing a name.

    Same entry shape as mt_roster_entries() below (name, short_name,
    node_ref, last_seen, lat, lon) so a shared picker UI can render both
    protocols identically -- short_name is always None here, not
    invented and not derived by truncating the name: the MeshCore
    directory has no short-name concept at all, unlike node_seen's real
    (if nullable) short_name column. node_ref is the bare lowercase
    8-hex public-key prefix that would actually be bound (the SAME
    value MeshMapper auto-binds and app/nodes_api.py accepts typed in by
    hand -- picking from this list is a third way to arrive at the exact
    same contact, never a different kind of identity) -- deliberately
    NOT "!"-prefixed the way a Meshtastic reference displays; that
    convention is protocol-specific display formatting the UI already
    owns elsewhere (the join page's radio list, the admin portal), not
    something this endpoint bakes in, and MeshCore contacts are never
    shown with a leading "!" in any of those existing places.

    Deliberately does NOT deduplicate by name -- the directory carries
    real duplicate names (3 in production today), and collapsing them
    would let the UI silently offer the wrong node. Every companion
    entry is returned as its own row, distinguished by its prefix;
    picking between duplicates is left entirely to the person looking at
    them. This function has no database access and does not know who is
    asking -- whether a given entry is already bound is deliberately NOT
    part of this shape at all: app/checkin_api.py's picker endpoint is
    public, and exposing that would leak which nodes belong to
    registered players to anyone who asks. POST /api/nodes' existing
    conflict check is where that enforcement belongs.
    """
    out = []
    for node in directory_nodes:
        if node.get("role") != "companion":
            continue
        name = node.get("name")
        pubkey = node.get("public_key")
        if not isinstance(name, str) or not name or not isinstance(pubkey, str) or len(pubkey) < 8:
            continue
        out.append({
            "name": name,
            "short_name": None,
            "node_ref": pubkey[:8].lower(),
            "last_seen": node.get("last_seen"),
            "lat": node.get("lat"),
            "lon": node.get("lon"),
        })
    return out


def mt_roster_entries(conn) -> list[dict]:
    """Shape node_seen into the same "people to pick from" list
    companion_directory_entries() builds for MeshCore -- same entry
    shape (name, short_name, node_ref, last_seen, lat, lon), different
    source and different exclusion rule. node_seen is populated every
    poll cycle by app/ingest.py's Ingestor._refresh_roster() from
    meshview's /api/nodes -- this is a read against data the app
    already keeps on hand for the live board's own node markers, not a
    new upstream call, scoped to the currently active Meshtastic season
    the same way that roster is scoped everywhere else it's read.

    Filtered by settings.excluded_roles_set -- the SAME roles the live
    Meshtastic board already treats as infrastructure rather than
    players (ROUTER, ROUTER_LATE, CLIENT_BASE by default), reused
    rather than a second list so "what counts as a person" never has
    two different answers in this codebase. A row with no role at all
    is not excluded by this filter -- an unknown role is not
    infrastructure by default.

    Ordered most-recently-seen first: node_seen runs to roughly a
    thousand rows, and the radio someone is actually holding right now
    was almost certainly heard recently -- this just keeps that node
    from being buried at the bottom of an otherwise unordered list.

    short_name is normalized to None when absent -- node_seen's column
    is nullable in the schema, but real rows from
    Ingestor._refresh_roster() commonly carry an empty string instead
    of a true NULL (meshview's own "no short name" signal isn't
    consistently absent-vs-empty either), so a plain pass-through would
    leave the UI rendering an empty "()" for most nodes instead of
    dropping the parenthetical entirely as intended -- both falsy forms
    are folded to None here, once, so nothing downstream has to
    special-case "" separately from null. node_ref is the bare
    lowercase 8-hex form (app/node_ref.py's format_node_ref) -- the
    same canonical value player_node stores and POST /api/nodes
    accepts, deliberately not "!"-prefixed here for the same reason
    companion_directory_entries() gives above: protocol-specific
    display formatting belongs to the UI that already owns it
    elsewhere (the join page's radio list, the admin portal), not this
    endpoint.
    """
    season = active_season(conn, "mt")
    if not season:
        return []
    rows = conn.execute(
        "SELECT node_id, name, short_name, last_seen, lat, lon, role "
        "  FROM node_seen WHERE season_id = ? ORDER BY last_seen DESC",
        (season["id"],),
    ).fetchall()

    # One bulk query rather than one per row: node_seen runs to roughly
    # a thousand rows, so this stays a single query instead of a
    # thousand. GROUP BY ... HAVING COUNT(*) = 1 picks out exactly the
    # node_refs with a single distinct public_key on record -- the same
    # "zero or many means NULL" rule POST /api/nodes uses when
    # auto-filling a binding, applied here to a whole roster at once.
    key_rows = conn.execute(
        "SELECT node_ref, public_key FROM mt_node_key "
        " GROUP BY node_ref HAVING COUNT(*) = 1"
    ).fetchall()
    key_by_ref = {r["node_ref"]: r["public_key"] for r in key_rows}

    excluded = settings.excluded_roles_set
    out = []
    for r in rows:
        role = r["role"]
        if isinstance(role, str) and role.strip().upper() in excluded:
            continue
        name = r["name"]
        if not isinstance(name, str) or not name:
            continue
        node_ref = format_node_ref(r["node_id"])
        out.append({
            "name": name,
            "short_name": r["short_name"] or None,
            "node_ref": node_ref,
            "last_seen": r["last_seen"],
            "lat": r["lat"],
            "lon": r["lon"],
            "public_key": key_by_ref.get(node_ref),
        })
    return out


def _parse_iso_ts(raw: object) -> int | None:
    """Parse a "...Z" UTC ISO-8601 timestamp (the shape both weekly-net
    messages and directory entries use) to epoch seconds. None on
    anything malformed -- this is upstream data, never raised on.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


# ---- Meshtastic: identity resolution -------------------------------------


def _load_mt_registered_players(conn) -> dict[str, int]:
    """node_ref (bare lowercase 8-hex) -> player_id, for every active
    Meshtastic player. Same shape and filter as app/ingest.py's
    _load_registered_players (disabled players excluded by the JOIN),
    loaded once per poll rather than once per packet for the same
    reason that function gives: the overwhelming majority of packets in
    a poll are not registered players, so a per-packet query would be a
    query for nearly nothing on every poll.
    """
    rows = conn.execute(
        "SELECT pn.node_ref, pn.player_id "
        "  FROM player_node pn JOIN player p ON p.player_id = pn.player_id "
        " WHERE pn.protocol = 'mt' AND p.disabled_at IS NULL"
    ).fetchall()
    return {r["node_ref"]: r["player_id"] for r in rows}


# ---- Poller ---------------------------------------------------------------


class CheckinPoller:
    """Background task: reloads checkin_net/checkin_config fresh from
    the database every cycle (never cached, never read from settings --
    see load_checkin_config and the module docstring for why) and awards
    points for qualifying, in-window, registered senders on every
    enabled net. Follows the same shape app/ingest.py's Ingestor and
    app/mc_ingest.py's McIngestor already use -- a loop task started
    from app/main.py's lifespan, exceptions caught and logged per cycle
    so one bad poll never kills the loop.

    ALWAYS started and stopped unconditionally by app/main.py, unlike
    before this table existed (that gated construction AND the loop
    itself on settings.checkin_enabled at process startup). The loop
    now has to always be running for checkin_config.enabled to be a
    true runtime toggle -- see run_forever/_poll_once below, which
    check the database's `enabled` flag on every cycle and simply do
    nothing when it is off, rather than the flag deciding whether the
    task exists at all.

    HTTP clients are pooled by connector_url, not held one-per-protocol
    like the original single-feed version -- see _mc_client_for and
    _mt_client_for. The Meshtastic client for settings.meshview_url
    specifically is the SAME MeshviewClient app/ingest.py's Ingestor
    already holds (passed in as `mt_client`), reused rather than a
    second connection pool to the same host, so all traffic to that
    instance stays under its own rate limiter
    (settings.upstream_rate_per_sec) -- any OTHER Meshtastic
    connector_url an admin configures gets its own MeshviewClient,
    built lazily and owned (and closed) by this poller. Every MeshCore-
    family connector (KIND_CORESCOPE or KIND_BEACON) gets its own
    CoreScopeClient/BeaconClient the same way; there is no shared
    MeshCore client to inherit, since app/mc_ingest.py's wardriving
    ingest is a completely separate feature from these check-in feeds.

    Dispatches on `kind`, not `protocol`, when deciding which client
    class a connector_url gets (_mc_client_for) -- protocol only ever
    tells you the SCORING BOARD (mc vs mt), which is no longer enough
    to tell you which upstream API to actually call now that two kinds
    (corescope, beacon) both drive protocol='mc'. Once a client exists
    for a connector_url, every function downstream of it
    (_process_mc_message, _resolve_mc_identities, the directory bridge)
    talks to it purely through the normalized shape CoreScopeClient/
    BeaconClient both produce -- see that section's header comment
    above -- and never needs to know which kind it actually is.
    """

    def __init__(self, mt_client: MeshviewClient) -> None:
        # The one Meshtastic client this poller does not own -- see the
        # class docstring. Kept separate from _mt_clients (which this
        # poller DOES own and must close) so stop() can never accidentally
        # close a connection pool app/main.py's own shutdown still needs.
        self._shared_mt_url = settings.meshview_url
        self._shared_mt_client = mt_client
        self._mt_clients: dict[str, MeshviewClient] = {}
        # One entry per connector_url regardless of which MeshCore-
        # family kind it is -- a CoreScopeClient or a BeaconClient,
        # whichever _mc_client_for built for it the first time this
        # connector_url was seen (see that method: a connector_url is
        # assumed to always mean the same upstream kind, same as this
        # module already assumes it always means the same upstream
        # instance).
        self._mc_clients: dict[str, CoreScopeClient | BeaconClient] = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        # MeshCore directory cache, one entry per connector_url -- see
        # _refresh_mc_directory_if_stale. A single connector (today's
        # common case) makes this a dict of one, behaviorally identical
        # to the single `self._directory` list this used to be.
        self._mc_directory: dict[str, list[dict]] = {}
        self._mc_directory_fetched_at: dict[str, float] = {}
        # Wall-clock of the last completed cycle, for the admin health
        # panel (app/admin_ops.py's _poller_health) -- a whole-poller
        # heartbeat, distinct from checkin_net.last_poll_at/
        # last_poll_error (app/db.py), which is the PER-NET counterpart
        # an admin nets list reads to see which specific net is failing.
        # In memory rather than a table because it is a liveness signal,
        # and a liveness signal that survives the process dying is not
        # one. Zero until the first cycle finishes.
        self.last_poll_at: int = 0
        self.last_poll_error: str | None = None
        # Gates _maybe_prune_unresolved_senders to at most once an hour --
        # same interval and monotonic-clock idiom
        # app/mqtt_subscriber.py's _HOUSEKEEPING_INTERVAL_S/
        # _maybe_housekeeping and app/mc_ingest.py's McIngestor use for
        # their own retention pruning, mirrored here rather than shared
        # code since the tables involved are unrelated. There is nothing
        # to prune on every 30-second poll cycle -- checkin_unresolved_
        # sender only grows during net windows, a few hours a week -- so
        # gating this the same way those two already do avoids a mostly-
        # pointless DELETE on every cycle.
        self._last_unresolved_prune: float = 0.0

    async def start(self) -> None:
        self._task = asyncio.create_task(self.run_forever(), name="checkin-poller")
        conn = connect()
        try:
            net_count = conn.execute(
                "SELECT count(*) FROM checkin_net WHERE enabled = 1"
            ).fetchone()[0]
        finally:
            conn.close()
        log.info("checkin poller started; %d net(s) enabled", net_count)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        # Only clients THIS poller opened -- the shared Meshtastic
        # client at self._shared_mt_url belongs to app/main.py, which
        # closes it itself after this stop() returns.
        for client in self._mc_clients.values():
            await client.aclose()
        for client in self._mt_clients.values():
            await client.aclose()
        log.info("checkin poller stopped")

    async def run_forever(self) -> None:
        try:
            while not self._stop.is_set():
                interval = settings.checkin_poll_interval_seconds
                try:
                    interval = await self._poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("checkin: poll cycle failed")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=max(interval, 1))
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise

    async def _poll_once(self) -> int:
        """One cycle: reload config+nets fresh (never cached -- see the
        module docstring), poll every enabled net grouped by KIND
        FAMILY, and return the poll interval to sleep for next -- also
        read fresh, so tightening or loosening it from the admin panel
        takes effect on the very next sleep, not after a restart.
        """
        conn = connect()
        try:
            config = load_checkin_config(conn)
            nets = [dict(r) for r in conn.execute(
                "SELECT * FROM checkin_net WHERE enabled = 1 ORDER BY id"
            ).fetchall()]
        finally:
            conn.close()

        if not config["enabled"]:
            return config["poll_interval_seconds"]

        # Grouped by KIND FAMILY, not by protocol: corescope and beacon
        # are two different kinds but share one polling/identity model
        # (channel-scoped feed + public-key directory), so both land in
        # mc_nets and are handled by _poll_mc below, which dispatches to
        # the right client per net's own `kind` (see _mc_client_for).
        # meshview and mqtt are the two Meshtastic-family kinds -- both
        # protocol='mt', both resolve identity directly off the sender
        # node id via _load_mt_registered_players -- so both land in
        # mt_nets and are handled by _poll_mt below, which dispatches
        # per net's own `kind` to either MeshviewClient's HTTP fetch or
        # mqtt_message_buffer's local read (see _poll_mt_connector).
        mc_nets = [n for n in nets if n["kind"] in (KIND_CORESCOPE, KIND_BEACON)]
        mt_nets = [n for n in nets if n["kind"] in (KIND_MESHVIEW, KIND_MQTT)]

        # The two protocols are independent and one going down must
        # never block the other -- each gets its own try/except, same
        # as before nets moved into the database. Failure WITHIN one
        # protocol's nets is isolated further still, per net -- see
        # _poll_mc/_poll_mt, which record a failing net's error onto its
        # own checkin_net row rather than letting it take out every net
        # sharing that protocol.
        errors = []
        if mc_nets:
            try:
                await self._poll_mc(mc_nets, config)
            except Exception as e:
                log.exception("checkin: mc poll failed")
                errors.append("mc: %s" % e)
        if mt_nets:
            try:
                await self._poll_mt(mt_nets, config)
            except Exception as e:
                log.exception("checkin: mt poll failed")
                errors.append("mt: %s" % e)

        await self._maybe_prune_unresolved_senders()

        self.last_poll_at = int(time.time())
        self.last_poll_error = "; ".join(errors) if errors else None
        return config["poll_interval_seconds"]

    async def _maybe_prune_unresolved_senders(self) -> None:
        """Delete checkin_unresolved_sender rows older than
        UNRESOLVED_SENDER_RETENTION_DAYS, at most once an hour -- see
        that constant and _UNRESOLVED_PRUNE_INTERVAL_S for why. Pruned
        on `last_seen` (the most recent sighting of that sender in that
        net), not `first_seen`, so a name that keeps posting unresolved
        week after week stays visible the whole time it remains a live
        problem, and only drops off once it has genuinely gone quiet.
        """
        now = time.monotonic()
        if now - self._last_unresolved_prune < _UNRESOLVED_PRUNE_INTERVAL_S:
            return
        self._last_unresolved_prune = now
        cutoff = int(time.time()) - UNRESOLVED_SENDER_RETENTION_DAYS * 86400
        async with WriteSession() as conn:
            cur = conn.execute(
                "DELETE FROM checkin_unresolved_sender WHERE last_seen < ?", (cutoff,)
            )
            removed = cur.rowcount
        if removed:
            log.info("checkin: pruned %d stale checkin_unresolved_sender row(s)", removed)

    # ---- client pooling ----------------------------------------------

    def _mc_client_for(self, kind: str, connector_url: str) -> CoreScopeClient | BeaconClient:
        """One HTTP client per distinct connector_url, shared by every
        net on that connector regardless of which of the two MeshCore-
        family kinds it is. `kind` only matters the FIRST time a given
        connector_url is seen -- it decides which class gets
        constructed; after that, this is assumed to always mean the
        same upstream kind for that URL, the same assumption the rest
        of this module already makes about a connector_url always
        meaning the same upstream instance.
        """
        client = self._mc_clients.get(connector_url)
        if client is None:
            client = BeaconClient(connector_url) if kind == KIND_BEACON else CoreScopeClient(connector_url)
            self._mc_clients[connector_url] = client
        return client

    def _mt_client_for(self, connector_url: str) -> MeshviewClient:
        if connector_url == self._shared_mt_url:
            return self._shared_mt_client
        client = self._mt_clients.get(connector_url)
        if client is None:
            client = MeshviewClient(base_url=connector_url)
            self._mt_clients[connector_url] = client
        return client

    # ---- MeshCore directory cache --------------------------------------

    async def _refresh_mc_directory_if_stale(self, kind: str, connector_url: str, config: dict) -> None:
        now = time.monotonic()
        fetched_at = self._mc_directory_fetched_at.get(connector_url, 0.0)
        if connector_url in self._mc_directory and now - fetched_at < config["directory_refresh_seconds"]:
            return
        client = self._mc_client_for(kind, connector_url)
        nodes = await client.fetch_directory(config["directory_limit"])
        if nodes:
            self._mc_directory[connector_url] = nodes
            self._mc_directory_fetched_at[connector_url] = now
        elif connector_url not in self._mc_directory:
            # No cache to fall back on yet for THIS connector -- its
            # directory bridge is simply unavailable this cycle. Only
            # nets on this one connector are affected (see
            # _resolve_mc_identities' cross-connector widening for how
            # other connectors' caches can still cover a player this
            # one can't).
            log.warning(
                "checkin: mc directory fetch failed for %s and no cached copy yet",
                connector_url,
            )

    def directory_snapshot(self, connector_url: str | None = None) -> list[dict]:
        """Read-only copy of a cached MeshCore-family directory (a
        CoreScope connector's or a Beacon connector's -- both normalize
        to the same shape, see the client-abstraction header comment
        above), for app/checkin_api.py's node-picker endpoint. With
        `connector_url`, just that connector's cache; without one, the
        union across every connector currently cached -- with today's
        common case of a single configured connector these are the same
        list, so this is a pure widening, never a narrowing, of what
        that endpoint used to return. Reads the SAME cache
        _refresh_mc_directory_if_stale maintains on its own per-
        connector interval -- that endpoint is a person clicking around
        a form, not something to hit any upstream for on every request.
        """
        if connector_url is not None:
            return list(self._mc_directory.get(connector_url, []))
        out: list[dict] = []
        for nodes in self._mc_directory.values():
            out.extend(nodes)
        return out

    # ---- MeshCore polling ------------------------------------------------

    async def _poll_mc(self, nets: list[dict], config: dict) -> None:
        connectors = sorted({n["connector_url"] for n in nets})
        # kind_by_connector: a connector_url is assumed to always mean
        # one upstream kind (see _mc_client_for) -- picking whichever
        # net currently on it happens to be first is just how that
        # kind gets discovered the first time this connector is touched.
        kind_by_connector = {n["connector_url"]: n["kind"] for n in nets}
        for url in connectors:
            await self._refresh_mc_directory_if_stale(kind_by_connector[url], url, config)

        now = int(time.time())
        # Season bookkeeping, same call app/mc_ingest.py's batch worker
        # makes -- check-ins must be able to roll the MeshCore season
        # forward on their own; a quiet week of wardriving traffic must
        # never be the only thing keeping mc_season current. Protocol-
        # wide, not per-net, so it runs once per cycle regardless of how
        # many mc nets are configured.
        async with WriteSession() as conn:
            mc_scoring.maybe_roll_season(conn, now, MC_PROTOCOL)
            season_id = mc_scoring.ensure_active_season(conn, now, MC_PROTOCOL)
            results.maybe_roll_months(conn, now, MC_PROTOCOL)

        # Grouped by the EXACT feed a request would fetch (connector,
        # channel), not just connector -- CoreScope's messages endpoint
        # is channel-scoped, so two nets on different channels already
        # trigger two separate requests no matter how they're grouped;
        # this grouping only matters for the (rare, but not impossible)
        # case of two nets sharing one exact channel on one connector,
        # so that pair is evaluated against one fetched message list in
        # a single pass -- see _process_mc_message for why that matters
        # for dedup, same reasoning _poll_mt applies to a shared
        # Meshtastic connector below.
        feeds: dict[tuple[str, str], list[dict]] = {}
        for n in nets:
            feeds.setdefault((n["connector_url"], n["channel"]), []).append(n)

        for (connector_url, channel), feed_nets in feeds.items():
            # Every net in one feed group shares (connector_url,
            # channel) by construction, but not necessarily `kind` in
            # some hand-edited/misconfigured database -- take the
            # first's, same as kind_by_connector above; there is no
            # meaningful way for two nets pointed at the identical
            # connector+channel to disagree about which upstream API
            # actually serves it.
            kind = feed_nets[0]["kind"]
            try:
                await self._poll_mc_feed(kind, connector_url, channel, feed_nets, connectors, season_id, now, config)
            except Exception as e:
                log.exception("checkin: mc feed %s#%s poll failed", connector_url, channel)
                for n in feed_nets:
                    await self._record_net_error(n["id"], str(e))
            else:
                for n in feed_nets:
                    await self._record_net_ok(n["id"], now)

    async def _poll_mc_feed(
        self, kind: str, connector_url: str, channel: str, feed_nets: list[dict],
        all_connectors: list[str], season_id: int, received_at: int, config: dict,
    ) -> None:
        client = self._mc_client_for(kind, connector_url)
        messages = await client.fetch_messages(channel, config["directory_refresh_seconds"])
        if not messages:
            return

        primary_dir = self._mc_directory.get(connector_url, [])
        other_dirs = [self._mc_directory.get(u, []) for u in all_connectors if u != connector_url]

        async with WriteSession() as conn:
            resolved = _resolve_mc_identities(conn, primary_dir, other_dirs, primary_connector=connector_url)
            for m in messages:
                self._process_mc_message(conn, connector_url, feed_nets, m, season_id, resolved, config, received_at)

    def _process_mc_message(
        self, conn, connector_url: str, nets: list[dict], m: dict, season_id: int,
        resolved: dict, config: dict, received_at: int,
    ) -> None:
        """Evaluate one fetched, ALREADY-NORMALIZED message (see the
        client-abstraction header comment above CoreScopeClient) against
        every net sharing this EXACT (connector, channel) feed (usually
        just one). Settled (marked seen) only once none of them is left
        wanting a retry -- see _mark_seen's docstring for what settled
        means and why a message a shared feed's OTHER net might still
        need must not be buried by this one's verdict.
        """
        pid_str = m["packet_id"]  # always present and a str -- the client dropped anything without one

        # Settled on an earlier poll -- READ first, rather than letting
        # an INSERT OR IGNORE claim the id up front. Marking a message
        # seen before trying to attribute it is what silently cost two
        # players their 2026-08-19 award: their radios were bound
        # correctly, but the directory had not yet seen those public
        # keys when the first poll swallowed the whole 100-message
        # backlog, so the bridge resolved them to nobody -- and the
        # message was already marked seen, so no later poll ever looked
        # at it again. See _mark_seen for what now counts as settled and
        # what is deliberately left for a retry.
        if _seen(conn, connector_url, pid_str):
            return

        ts = m.get("ts")
        if ts is None:
            _mark_seen(conn, connector_url, pid_str, received_at)
            return

        normalized = normalize_sender_name(m.get("sender_name"))
        if normalized is None:
            _mark_seen(conn, connector_url, pid_str, received_at)
            return

        player_id = resolved.get(normalized)

        # net_date_for_net is net-specific (each net carries its own
        # weekday/hours/timezone/start_date), so this is checked per net
        # even though every net here shares one connector and channel.
        unresolved = False
        for net in nets:
            net_date = net_date_for_net(net, ts)
            if net_date is None:
                continue  # outside THIS net's window -- its business with the message is over
            if player_id is None:
                # Deliberately NOT settled: unlike the branches above,
                # this one can change without the message changing. The
                # sender may be someone who simply is not playing -- or
                # a real player whose bound contact has not reached the
                # directory yet, or whose radio is posting under a name
                # that does not match what it confirmed/wardrove under.
                #
                # Recorded here, not settled -- this is purely a
                # visibility log for an operator (checkin_unresolved_sender,
                # app/db.py) and must never be confused with the
                # checkin_seen_message dedupe below. It is written INSIDE
                # this per-net loop, keyed on THIS net's own net_date, so
                # a message inside two different nets' windows (a
                # theoretical edge case today) is recorded once against
                # each net it actually fell inside, exactly the same
                # scoping net_date_for_net already applies to awarding.
                unresolved = True
                _record_unresolved_sender(conn, net["id"], net_date, normalized, received_at)
                continue
            _award_checkin(conn, config, season_id, player_id, net_date, MC_PROTOCOL,
                           pid_str, received_at, ts)

        if not unresolved:
            # Only now: an awarded (or window-rejected-by-every-net)
            # message must never be re-examined, or a season roll would
            # let the same message earn again under the new season_id
            # (mc_checkin_award's key is per season).
            _mark_seen(conn, connector_url, pid_str, received_at)

    # ---- Meshtastic polling -----------------------------------------------

    async def _poll_mt(self, nets: list[dict], config: dict) -> None:
        now = int(time.time())
        async with WriteSession() as conn:
            mc_scoring.maybe_roll_season(conn, now, MT_PROTOCOL)
            season_id = mc_scoring.ensure_active_season(conn, now, MT_PROTOCOL)
            results.maybe_roll_months(conn, now, MT_PROTOCOL)
            registered = _load_mt_registered_players(conn)

        # Grouped by connector, not by net: meshview's /api/packets feed
        # (and mqtt_message_buffer, read the same way -- see
        # _poll_mt_connector/_poll_mqtt_connector below) is not channel-
        # or hashtag-scoped the way CoreScope's is, so two nets on ONE
        # connector with two different hashtags are a genuinely plausible
        # setup (unlike mc's identical-channel edge case above), and
        # fetching once per net here would risk a real one: whichever
        # net's hashtag check ran first could settle a message before the
        # other net's hashtag was ever checked against it. Fetching once
        # per connector and evaluating every net sharing it in one pass
        # (_process_mt_packet/_process_mqtt_message) avoids that.
        connector_nets: dict[str, list[dict]] = {}
        for n in nets:
            connector_nets.setdefault(n["connector_url"], []).append(n)

        for connector_url, group in connector_nets.items():
            # A connector_url is assumed to always mean one upstream kind
            # -- same assumption _mc_client_for/kind_by_connector already
            # make for the mc family (see _poll_mc) -- take the first
            # net's `kind`, since two nets sharing one connector_url have
            # no meaningful way to disagree about which kind actually
            # serves it.
            kind = group[0]["kind"]
            if kind == KIND_MQTT:
                # mqtt_message_buffer is a local table written by
                # app/mqtt_subscriber.py's MqttSubscriber, not an
                # upstream HTTP API -- a failure reading it would be a
                # real bug (a locked or corrupt db), not "upstream is
                # down," so this deliberately does NOT call
                # _record_net_ok/_record_net_error the way the HTTP kinds
                # below do. last_poll_at/last_poll_error for an mqtt net
                # belongs to MqttSubscriber -- it reflects BROKER
                # connectivity, which this cycle's mere ability to read a
                # local sqlite table says nothing about -- see that
                # module's docstring. A genuine failure here is still
                # logged and folds into _poll_once's whole-poller
                # last_poll_error; it just never overwrites a specific
                # net's own row the way the subscriber's connection-state
                # writes do.
                try:
                    await self._poll_mqtt_connector(connector_url, group, season_id, registered, now, config)
                except Exception:
                    log.exception("checkin: mqtt buffer read failed for %s", connector_url)
                continue
            try:
                await self._poll_mt_connector(connector_url, group, season_id, registered, now, config)
            except Exception as e:
                log.exception("checkin: mt connector %s poll failed", connector_url)
                for n in group:
                    await self._record_net_error(n["id"], str(e))
            else:
                for n in group:
                    await self._record_net_ok(n["id"], now)

    async def _poll_mt_connector(
        self, connector_url: str, nets: list[dict], season_id: int,
        registered: dict, received_at: int, config: dict,
    ) -> None:
        client = self._mt_client_for(connector_url)
        packets = await client.packets(portnum=1, limit=100)
        if not packets:
            return
        async with WriteSession() as conn:
            for pkt in packets:
                self._process_mt_packet(conn, connector_url, nets, pkt, season_id, registered, config, received_at)

    # ---- mqtt buffer polling ---------------------------------------------
    #
    # See KIND_MQTT's own header comment above this class for the
    # architecture: app/mqtt_subscriber.py's MqttSubscriber holds the
    # actual broker connections and writes decoded messages into
    # mqtt_message_buffer as they arrive; everything below reads that
    # table exactly the way _poll_mt_connector/_process_mt_packet read an
    # HTTP response, so it plugs into the exact same window/dedupe/
    # identity/award machinery with no changes to any of it.

    async def _poll_mqtt_connector(
        self, connector_url: str, nets: list[dict], season_id: int,
        registered: dict, received_at: int, config: dict,
    ) -> None:
        messages = await self._fetch_mqtt_messages(connector_url)
        if not messages:
            return
        async with WriteSession() as conn:
            for m in messages:
                self._process_mqtt_message(conn, connector_url, nets, m, season_id, registered, config, received_at)

    async def _fetch_mqtt_messages(self, connector_url: str) -> list[dict]:
        """Every currently-buffered message for one mqtt connector.
        Unlike CoreScopeClient/BeaconClient/MeshviewClient this is a
        plain local sqlite read, not an HTTP call -- there is nothing to
        retry or swallow here (see _poll_mt's kind dispatch above for why
        a failure here is treated as a real bug, not "upstream down").

        Reads the WHOLE current buffer for this connector, not just rows
        newer than the last cycle -- cheap, since the buffer is pruned to
        settings.mqtt_buffer_retention_hours (app/mqtt_subscriber.py) and
        not an unbounded history, and correct: _process_mqtt_message's
        _seen() check is what actually keeps re-reading an
        already-settled row from doing anything, exactly the way
        re-fetching the same 100 messages from CoreScope/Beacon on every
        cycle already does.
        """
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT packet_id, from_node, text, ts FROM mqtt_message_buffer "
                " WHERE connector = ? ORDER BY ts",
                (connector_url,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def _process_mqtt_message(
        self, conn, connector_url: str, nets: list[dict], m: dict, season_id: int,
        registered: dict, config: dict, received_at: int,
    ) -> None:
        """Evaluate one buffered, already-decoded message against every
        mqtt net sharing this connector -- the mqtt-buffer counterpart of
        _process_mt_packet just below, with the same settled-once-
        nobody-needs-a-retry rule (see _mark_seen) and the same
        hashtag-substring matching meshview uses. The one real
        difference: identity is read straight off `from_node` (the
        Meshtastic sender node id app/mqtt_subscriber.py already decoded
        off the packet -- from a JSON message's `from` field, or a
        decrypted protobuf MeshPacket's `from` field) rather than parsed
        out of an HTTP packet dict, since there is no HTTP packet here.
        `registered` is the SAME node_ref -> player_id map _poll_mt loads
        once per cycle for every mt-protocol net, meshview and mqtt
        alike -- a node_ref means the same player under either kind.
        """
        pid_str = m["packet_id"]  # already a str -- mqtt_message_buffer.packet_id is TEXT

        # READ first, not INSERT OR IGNORE -- same reasoning
        # _process_mt_packet/_process_mc_message give (see _mark_seen):
        # a message this poll cannot yet attribute to a player must stay
        # eligible for a later retry, not be claimed as settled here.
        if _seen(conn, connector_url, pid_str):
            return

        text = m.get("text")
        if not isinstance(text, str):
            _mark_seen(conn, connector_url, pid_str, received_at)
            return
        text_lower = text.lower()

        matching_nets = [n for n in nets if n["hashtag"].lower() in text_lower]
        if not matching_nets:
            # No net sharing this connector cares about this message --
            # settled the same way a hashtag-less meshview packet is.
            _mark_seen(conn, connector_url, pid_str, received_at)
            return

        ts = m.get("ts")
        if not isinstance(ts, int):
            _mark_seen(conn, connector_url, pid_str, received_at)
            return

        from_node = m.get("from_node")
        if not isinstance(from_node, int):
            _mark_seen(conn, connector_url, pid_str, received_at)
            return
        # Local import: see _process_mt_packet's own comment below for
        # why this has to be deferred rather than a top-level import
        # (app/api.py -> app/checkin_api.py -> app/checkin.py ->
        # app/ingest.py -> app/api.py would otherwise close a cycle).
        from .ingest import _bare_node_ref

        node_ref = _bare_node_ref(from_node)
        player_id = registered.get(node_ref)  # same for every net -- protocol-wide, not net-specific

        unresolved = False
        for net in matching_nets:
            net_date = net_date_for_net(net, ts)
            if net_date is None:
                continue  # outside THIS net's window -- its business with the message is over
            if player_id is None:
                unresolved = True  # not settled -- see _mark_seen
                continue
            _award_checkin(conn, config, season_id, player_id, net_date, MT_PROTOCOL,
                           pid_str, received_at, ts)

        if not unresolved:
            _mark_seen(conn, connector_url, pid_str, received_at)

    def _process_mt_packet(
        self, conn, connector_url: str, nets: list[dict], pkt: dict, season_id: int,
        registered: dict, config: dict, received_at: int,
    ) -> None:
        """Evaluate one fetched packet against every net sharing this
        connector. Same settled-once-nobody-needs-a-retry rule
        _process_mc_message applies, adapted for the one genuine
        per-net variable here being the hashtag match (sender
        registration is protocol-wide, not net-specific -- a node_ref
        means the same player under every mt net) and the window.
        """
        pid = pkt.get("id")
        if not isinstance(pid, int):
            return
        pid_str = str(pid)

        # Dedup against checkin_seen_message, not the old shared
        # processed_packet table -- see app/db.py's checkin_seen_message
        # comment for why a Meshtastic id now has to be scoped by
        # connector too, the same reasoning that already applied to
        # MeshCore: a SECOND meshview connector numbering its own
        # packet ids from its own sequence could otherwise collide with
        # the first one's ids and hide a real check-in.
        #
        # READ first rather than claiming the id with an INSERT OR
        # IGNORE, for the same reason _process_mc_message does -- see
        # _mark_seen.
        if _seen(conn, connector_url, pid_str):
            return

        payload = pkt.get("payload")
        if not isinstance(payload, str):
            _mark_seen(conn, connector_url, pid_str, received_at)
            return
        payload_lower = payload.lower()

        matching_nets = [n for n in nets if n["hashtag"].lower() in payload_lower]
        if not matching_nets:
            # No net sharing this connector cares about this message at
            # all -- settled the same way a hashtag-less message always
            # was, before more than one net could ever share a connector.
            _mark_seen(conn, connector_url, pid_str, received_at)
            return

        import_us = pkt.get("import_time_us")
        if not isinstance(import_us, (int, float)):
            _mark_seen(conn, connector_url, pid_str, received_at)
            return
        message_ts = int(import_us / 1_000_000)

        sender_id = pkt.get("from_node_id")
        if not isinstance(sender_id, int):
            _mark_seen(conn, connector_url, pid_str, received_at)
            return
        # Local import: see app/ingest.py's _bare_node_ref docstring for
        # the exact form (bare lowercase 8-hex, matching
        # player_node.node_ref's canonical form) -- reused as-is rather
        # than reimplemented, per the module docstring. Deferred to call
        # time, not imported at module load, to avoid a circular import:
        # app/api.py -> app/checkin_api.py -> app/checkin.py would close
        # a cycle back to app/api.py if this were a top-level import,
        # since app/ingest.py itself imports from app/api.py.
        from .ingest import _bare_node_ref

        node_ref = _bare_node_ref(sender_id)
        player_id = registered.get(node_ref)  # same for every net -- see the docstring above

        unresolved = False
        for net in matching_nets:
            net_date = net_date_for_net(net, message_ts)
            if net_date is None:
                continue  # outside THIS net's window -- its business with the message is over
            if player_id is None:
                unresolved = True  # not settled -- see _mark_seen
                continue
            _award_checkin(conn, config, season_id, player_id, net_date, MT_PROTOCOL,
                           pid_str, received_at, message_ts)

        if not unresolved:
            _mark_seen(conn, connector_url, pid_str, received_at)

    # ---- per-net status (app/admin_ops.py's nets list) ------------------

    async def _record_net_ok(self, net_id: int, at: int) -> None:
        async with WriteSession() as conn:
            conn.execute(
                "UPDATE checkin_net SET last_poll_at = ?, last_poll_error = NULL WHERE id = ?",
                (at, net_id),
            )

    async def _record_net_error(self, net_id: int, error: str) -> None:
        # Truncated: an upstream client library's exception text can run
        # arbitrarily long (a full URL, a stack of chained causes), and
        # this only has to be enough for an operator to recognize what
        # broke, not a full traceback -- that already went to the log.
        async with WriteSession() as conn:
            conn.execute(
                "UPDATE checkin_net SET last_poll_at = ?, last_poll_error = ? WHERE id = ?",
                (int(time.time()), error[:500], net_id),
            )
