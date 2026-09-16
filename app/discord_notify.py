"""Outbound Discord announcements for end-of-month honors, via a
durable outbox table (app/db.py's discord_outbox -- see that table's
own comment for the exactly-once and enqueue-inside-the-freeze-
transaction reasoning this module builds on).

The shape mirrors app/account_api.py's own fire-and-forget security
notices (_notify_security there, send_security_notice/EmailSendError
in app/email_login.py): a send failure must never surface to, delay,
or undo whatever action triggered it. The difference here is that a
month freeze can happen while this deployment's webhook is down (or
before one is even configured), so the "send" step is decoupled from
the write entirely -- freeze_month() only ever enqueues a row, on the
SAME connection and inside the SAME transaction as the freeze itself
(app/results.py), and this module's own background loop
(run_forever(), started unconditionally by app/main.py) is what
actually walks the outbox and posts, on its own schedule, with its own
retry and give-up rules.

Nothing here ever logs or stores the webhook URL itself: it is a bearer
credential (a Discord webhook URL embeds its own auth token in the
path -- anyone holding it can post to the channel as this app, no
further authentication), the same "a secret, never returned or logged"
treatment app/config.py already gives freqmapper_api_key and
admin_token. _post() below is the one place that ever touches the
value. A timeout or transport failure raises a short, fixed message
naming the failure kind, never the request or its URL, because httpx's
own exception text embeds the request (and so the URL) -- but a non-2xx
HTTP response is a different case: the RESPONSE body is Discord
describing what was wrong with the payload it received, not a
credential, so a snippet of it is included to make a bad announcement
diagnosable. See _post()'s own docstring for the line between the two.

Configuration (enabled, webhook_url, username, team_emoji,
announce_month_honors, announce_season_close, announce_weekly_recap,
announce_net_wrapup) lives in the DB, not settings.py directly --
(announce_place_activation
is still a real column, kept per this codebase's "never drop a column"
rule, but is no longer read anywhere: the per-event place announcement
it gated was retired 2026-09-16 in favour of the weekly recap's
Exploration section -- see credit_places()'s own comment in
app/place_scoring.py and weekly_recap_provider()'s docstring below.)
app/db.py's discord_config singleton, read fresh by
load_discord_config() below every time it is needed, the same
DB-backed, admin-editable runtime config app/freqmapper_ingest.py's
load_freqmapper_config() already established for the FreqMapper
connector. settings.discord_webhook_announcements/
discord_webhook_username/discord_team_emoji remain the SEED
(seed_discord_config_from_env() below, called once from app/db.py's
init_db()) and the documented bootstrap path for a brand-new
deployment; once seeded, this module never reads them again.

Two more pieces of plumbing on top of the single default webhook above:

- PER-KIND ROUTING (app/db.py's discord_channel table): an announcement
  kind can be routed to its own webhook instead of the shared default.
  load_discord_channels() reads the routing table; resolve_discord_webhook()
  is the ONE place that decides, per kind, which URL wins (see its own
  docstring for the enabled=0-means-do-not-announce distinction).
  Resolved fresh at POST time in _drain_once(), never at enqueue() time
  and never stored on the outbox row -- a channel move after a row is
  already queued must still be honored.
- TIME-DRIVEN ANNOUNCEMENTS (TIME_DRIVEN_PROVIDERS, check_due_time_driven()):
  the "clock" for a kind with no triggering event to enqueue() from, since
  this app has no scheduler and must not grow one. See
  TIME_DRIVEN_PROVIDERS's own docstring for the shape (a list of PROVIDER
  FUNCTIONS, not fixed entries) and the worked example of why.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .config import settings
from .db import WriteSession, connect

log = logging.getLogger("discord_notify")

# Discord webhooks accept a JSON body with (at minimum) `embeds`; 10s is
# generous for a small JSON POST to Discord's own API and matches the
# connect+read budget every other outbound HTTP client in this codebase
# uses for a single external call (see app/freqmapper_ingest.py's own
# httpx.Timeout, and app/oauth.py's token-exchange client).
_POST_TIMEOUT_SECONDS = 10.0

# How much of Discord's own non-2xx response body to fold into
# DiscordSendError's message (see _post() below) -- enough to show the
# actual field/name Discord objected to without letting one row's
# stored last_error grow without bound.
_MAX_ERROR_BODY_CHARS = 200

# protocol -> the name a reader recognizes, for the embed's own text.
# 'mc'/'mt' are exactly the bare literals every other module in this
# codebase uses for the two boards (see app/results.py's own module
# docstring) -- this is purely a DISPLAY table, not a third copy of the
# protocol discriminator itself.
_PROTOCOL_NAMES = {"mc": "MeshCore", "mt": "Meshtastic"}

# Discord's own documented hard limit on the number of `fields` entries
# in one embed. It is not a soft truncation on Discord's end -- posting
# a 26th field fails the ENTIRE message with an HTTP 400, so this is a
# guard, not a style choice. build_month_honors_embed() stays well
# under it (headline awards are one field each, at most one per
# results.AWARD_LABELS entry with an empty scope; per-team awards are
# grouped into one field per award KEY rather than one per team, the
# same headline/per-team split frontend/results.js's renderHonors() and
# splitAwards() already draw for the same reason), but the slice below
# applies regardless of what a future award shape produces.
_MAX_EMBED_FIELDS = 25

# The unit for every standings/territory figure this module renders --
# named ONCE here rather than as an inline literal so the standings
# line (build_month_honors_embed()'s standings_text, no longer where
# this word appears now that Change 2 moved it) and the standings
# embed's own `footer` (the ONE place it now renders -- see that
# embed's construction below) can never drift apart or say two
# different things for the same figure.
_STANDINGS_UNIT = "squares held"

# The unit for build_season_close_embed()'s own standings field -- a
# SEASON's standing is decided on team_totals() (squares held PLUS
# check-in points PLUS Places Worth Going points, see that function's
# own docstring in app/mc_scoring.py), not squares alone, so it needs
# its own word rather than reusing _STANDINGS_UNIT above: printing
# "squares held" next to a number that is not a square count would be
# actively wrong, not just imprecise.
_SEASON_TOTAL_UNIT = "combined score"

# Discord's own documented hard limit on the TOTAL character count
# across every embed in one message -- title + description + each
# field's name + value, summed across ALL embeds, not per embed (see
# _total_embed_chars() below for exactly what is counted). This is
# separate from, and in addition to, _MAX_EMBED_FIELDS above: a message
# can have well under 25 fields in every embed and still be rejected on
# this budget alone. When the assembled three-embed payload
# (build_month_honors_embed() below) would exceed it, the "By team"
# embed is dropped first and entirely -- never truncated -- because it
# is the least important of the three: a reader who only sees standings
# and the headline Honors embed still gets the full story for the
# month, while per-team breakdowns are supplementary detail.
_MAX_TOTAL_EMBED_CHARS = 6000

# Discord's own documented hard limit on a single field's `value`
# string -- separate from, and much tighter than, the two budgets
# above. The "By team" fields are the longest values this module ever
# builds (one line per team, up to a whole season's roster) and now
# carry bold-number markup on every line on top of that, so they are
# the only fields this module actively guards against it: see
# _join_team_field() below, which drops trailing team lines and ends
# on a plain truncation marker rather than ever handing Discord an
# over-long field value that would 400 the ENTIRE message.
_MAX_FIELD_VALUE_CHARS = 1024

# Plain text, deliberately un-emphasised (no bold/italic) -- it is
# reporting a truncation, not a piece of the month's data, so it must
# never be mistaken for a real trailing line of standings.
_TRUNCATION_MARKER = "(truncated)"

# Team colours, mirrored from frontend/theme.css's own canonical
# definitions (~line 100) as INTEGERS -- Discord's embed `color` field
# takes a decimal int, not a hex string, so these are written as
# 0xff4136-style literals to stay visually comparable to the CSS hex
# values they come from, rather than pre-converted to decimal.
# theme.css's own comment: "If a team colour ever changes it changes in
# five files, and this is one of them" -- frontend/theme.css,
# frontend/mc.js, frontend/map2.js, frontend/join.js, and
# frontend/results.js are those five. This module is now the SIXTH: if
# a team colour ever changes, change it here too.
_TEAM_COLORS = {
    "RED": 0xff4136,
    "GREEN": 0x2ecc40,
    "BLUE": 0x3d8bfd,
    "PURPLE": 0xb10dc9,
    "YELLOW": 0xffdc00,
    "ORANGE": 0xff8a00,
    "PINK": 0xff8ac6,
}


def _parse_team_emoji(raw: str) -> dict[str, str]:
    """Parse settings.discord_team_emoji into a {TEAM: token} map.

    Format is comma-separated TEAM=token entries, where the token is
    exactly what Discord itself echoes back for a custom emoji (see
    that setting's own comment in app/config.py for how an operator
    gets one) -- it contains its own ":" and "<>" characters but never
    "=", so each entry is split on the FIRST "=" only. The team key is
    uppercased and both sides are stripped of surrounding whitespace,
    so "green = <:mw_green:222>" and "GREEN=<:mw_green:222>" parse
    identically.

    A malformed entry (no "=", or either side empty after stripping) is
    silently skipped rather than raising -- a typo in this setting must
    never break the whole announcement, only lose that one team's dot.
    Skipped entries are counted and logged once at WARNING with only
    the count, never the raw setting value: DISCORD_TEAM_EMOJI is not a
    secret, but there is no reason to echo an operator's possibly-messy
    input back into the log either.

    Empty/unset `raw` yields {}, same "empty means off" contract every
    other optional setting in this file's config section uses.
    """
    if not raw:
        return {}
    emoji: dict[str, str] = {}
    skipped = 0
    for entry in raw.split(","):
        if "=" not in entry:
            skipped += 1
            continue
        team, token = entry.split("=", 1)
        team = team.strip().upper()
        token = token.strip()
        if not team or not token:
            skipped += 1
            continue
        emoji[team] = token
    if skipped:
        log.warning(
            "discord team emoji: skipped %d malformed entr%s in DISCORD_TEAM_EMOJI",
            skipped, "y" if skipped == 1 else "ies",
        )
    return emoji


def _team_emoji_token(emoji: dict[str, str], team: str | None) -> str:
    """The configured custom-emoji token for `team`, out of an
    already-parsed {TEAM: token} map (_parse_team_emoji()'s return), or
    "" when `team` is blank/None or has no entry in the map -- never
    raises. See _team_dot() for how a caller turns this into a leading
    dot on a team name/value, with the mandatory plain-text fallback
    this function's own "" return makes trivial.
    """
    if not team:
        return ""
    return emoji.get(team, "")


def _team_dot(emoji: dict[str, str], team: str | None) -> str:
    """`team`'s emoji token plus exactly one trailing space, ready to
    prepend directly onto a team name or an award value -- or "" when
    the team has no emoji configured. This "" case is the mandatory
    fallback: a deployment with DISCORD_TEAM_EMOJI unset (or a team
    simply missing from it) must render with no leading space, no empty
    placeholder, and no stray "<:name:id>" text -- exactly as it did
    before this feature existed.
    """
    token = _team_emoji_token(emoji, team)
    return f"{token} " if token else ""


def _team_color(team: str | None) -> int | None:
    """The team's Discord embed colour, or None for a blank/unknown
    team name -- never raises. build_month_honors_embed() uses this to
    colour the month's announcement by whichever team is LEADING, and a
    corrupt or future team name this table doesn't know about must
    degrade to "no colour" there, never to a guessed default and never
    to a crashed announcement.
    """
    if not team:
        return None
    return _TEAM_COLORS.get(team)


# Mirrors MONTH_NAMES in frontend/results.js exactly (same order,
# same spelling) -- see _month_title() below.
_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _month_title(month: str) -> str:
    """"2026-08" -> "August 2026" -- mirrors monthTitle() in
    frontend/results.js so the Discord announcement and the website's
    own /results page always name a month the same way; a raw
    "YYYY-MM" key in the embed title read, verbatim, as "ugly." A month
    string that doesn't parse to a 1-12 calendar month (or isn't
    "YYYY-MM" shaped at all) is returned UNCHANGED rather than raising
    or -- the trap a naive `MONTH_NAMES[m - 1]` index falls into --
    rendering something like "None 2026".
    """
    try:
        year = month[:4]
        m = int(month[5:7])
    except (ValueError, IndexError):
        return month
    if not 1 <= m <= 12:
        return month
    return f"{_MONTH_NAMES[m - 1]} {year}"


def _total_embed_chars(embeds: list) -> int:
    """Sum of title + description + footer text + each field's
    name/value, across every embed given -- the same text Discord
    counts toward its own 6000-character total-embed budget
    (_MAX_TOTAL_EMBED_CHARS above). Author/thumbnail text also count on
    Discord's side, but this module never sets either, so they would
    only ever contribute zero and are left out of the sum entirely.
    footer IS counted now: the standings embed carries one (see its own
    construction in build_month_honors_embed()), and the field-count/
    total-char guards below must see the real, post-markup total, not
    an undercount that would let a message through that Discord itself
    then rejects.
    """
    total = 0
    for embed in embeds:
        total += len(embed.get("title") or "")
        total += len(embed.get("description") or "")
        total += len((embed.get("footer") or {}).get("text") or "")
        for f in embed.get("fields") or []:
            total += len(f.get("name") or "")
            total += len(f.get("value") or "")
    return total


def load_discord_config(conn) -> dict:
    """Fresh, uncached read of the discord_config singleton (app/db.py)
    -- enabled, webhook_url, username, team_emoji, announce_month_honors,
    announce_season_close, announce_weekly_recap, announce_net_wrapup,
    guild_id, roles_enabled, team_channels_enabled, team_category_name,
    team_category_id, updated_at. Read on every enqueue() call, every drain cycle
    (_drain_once()), by build_month_honors_embed()/
    build_season_close_embed()/build_weekly_recap_embed()/
    build_net_wrapup_embed(), and by every admin route that needs the
    current values (app/admin_ops.py) -- never cached anywhere in the
    process. Exactly the pattern app/freqmapper_ingest.py's
    load_freqmapper_config() uses for freqmapper_config, for the same
    reason: an admin edit through /api/admin/discord must take effect on
    the very next freeze/roll/activation or drain cycle, not after a
    restart.

    Deliberately does NOT select discord_config.announce_place_activation
    any more: that column still exists (this codebase never drops a
    column -- see the CREATE TABLE's own comment in app/db.py) but the
    kind it gated was retired 2026-09-16, so nothing reads it going
    forward; a caller that still wants to see its stored value can query
    the column directly.

    Falls back to config.py's original settings if the row is somehow
    missing (a database whose migrations have not run yet) rather than
    raising -- defensive, since app/db.py's MIGRATIONS seeds this row
    unconditionally and it should always be there in practice, but a
    freeze or drain cycle failing outright over a missing config row
    would be a worse failure mode than briefly falling back to the
    settings this row was itself seeded from. `enabled` in that
    fallback mirrors seed_discord_config_from_env()'s own reasoning: a
    webhook being configured at all WAS the on/off switch before this
    table existed, so the fallback reconstructs the same state a real
    column would hold. announce_month_honors/announce_season_close/
    announce_weekly_recap/announce_net_wrapup all default True in the
    fallback too -- the same CREATE TABLE default every one of them
    carries, so a missing row degrades to exactly the schema's own
    defaults rather than inventing a different answer.
    """
    row = conn.execute(
        "SELECT enabled, webhook_url, username, team_emoji, "
        "       announce_month_honors, announce_season_close, "
        "       announce_weekly_recap, announce_net_wrapup, "
        "       guild_id, roles_enabled, "
        "       team_channels_enabled, team_category_name, team_category_id, "
        "       slash_enabled, app_id, public_key, "
        "       updated_at "
        "  FROM discord_config WHERE id = 1"
    ).fetchone()
    if row is None:
        return {
            "enabled": bool(settings.discord_webhook_announcements),
            "webhook_url": settings.discord_webhook_announcements,
            "username": settings.discord_webhook_username,
            "team_emoji": settings.discord_team_emoji,
            "announce_month_honors": True,
            "announce_season_close": True,
            "announce_weekly_recap": True,
            "announce_net_wrapup": True,
            # Role sync (app/discord_bot.py) has no settings.py seed of
            # its own beyond guild_id -- see that module's own
            # _roles_ready() -- so this fallback reproduces the same
            # "not configured yet" state a real row defaults to.
            "guild_id": settings.discord_guild_id,
            "roles_enabled": False,
            # Private team channels (app/discord_bot.py's
            # ensure_team_channels()) has no settings.py seed at all --
            # same "not configured yet" fallback, matching the CREATE
            # TABLE's own defaults exactly (off, category name 'Teams',
            # no discovered category id yet).
            "team_channels_enabled": False,
            "team_category_name": "Teams",
            "team_category_id": None,
            # Slash commands (app/discord_interactions.py) -- same
            # "not configured yet" fallback as roles_enabled above:
            # off, app_id/public_key seeded from settings the same way
            # guild_id is.
            "slash_enabled": False,
            "app_id": settings.discord_app_id,
            "public_key": settings.discord_public_key,
            "updated_at": 0,
        }
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    d["announce_month_honors"] = bool(d["announce_month_honors"])
    d["announce_season_close"] = bool(d["announce_season_close"])
    d["announce_weekly_recap"] = bool(d["announce_weekly_recap"])
    d["announce_net_wrapup"] = bool(d["announce_net_wrapup"])
    d["roles_enabled"] = bool(d["roles_enabled"])
    d["team_channels_enabled"] = bool(d["team_channels_enabled"])
    d["slash_enabled"] = bool(d["slash_enabled"])
    return d


def load_discord_channels(conn) -> dict[str, dict]:
    """Every discord_channel row, keyed by `kind` -- a fresh, uncached
    read, same "never cached, read every time it is needed" contract
    load_discord_config() above documents for discord_config. Read once
    per enqueue() call and once per drain cycle (_drain_once(), which
    loads it alongside discord_config and resolves each row's channel
    from this already-loaded dict rather than re-querying per outbox
    row -- see that function's own docstring).

    A kind absent from the returned dict has no override at all -- the
    common case for every kind before an operator ever visits
    /api/admin/discord's channel table -- and resolve_discord_webhook()
    below treats a missing key exactly like a row it has never seen.
    """
    rows = conn.execute(
        "SELECT kind, webhook_url, enabled, updated_at FROM discord_channel"
    ).fetchall()
    return {row["kind"]: dict(row) for row in rows}


def _channel_kind_candidates(kind: str) -> list[str]:
    """Most-specific-first candidate list for resolving `kind` against
    discord_channel: `kind` itself, then -- only when it is colon-scoped
    ("<generic>:<instance>") -- the generic prefix before the FIRST
    colon. A plain, unscoped kind ("month_honors", "test") yields just
    itself, one element, so resolve_discord_webhook() below behaves
    identically to a simple single-kind lookup for every kind this task
    ships.

    This is the plumbing a future per-instance kind rides on without any
    further change here: a net wrap-up (see discord_notify's own
    TIME_DRIVEN_PROVIDERS docstring for the worked example) would use
    kind="net_wrapup:<net id>", which resolves against a per-net
    override FIRST ("net_wrapup:12"), falling back to a generic
    "net_wrapup" channel shared by every net that has no override of its
    own, before ever falling all the way back to discord_config's
    default -- so an operator CAN split one community's wrap-ups onto
    their own channel later without that ever being required.
    """
    if ":" in kind:
        return [kind, kind.split(":", 1)[0]]
    return [kind]


def resolve_discord_webhook(cfg: dict, channels: dict[str, dict], kind: str) -> str | None:
    """The webhook URL `kind` should post to right now, or None when it
    must not be announced at all -- the ONE place this whole module
    decides that, called fresh both by enqueue() (to decide whether to
    skip queuing in the first place) and by _drain_once() (to decide
    where each pending row actually posts). `cfg` and `channels` are
    already-loaded load_discord_config()/load_discord_channels() dicts
    for the CURRENT cycle -- this function makes no DB call of its own,
    so a caller iterating many outbox rows in one drain cycle resolves
    each row's kind from the same loaded snapshot rather than a fresh
    query per row.

    Tries _channel_kind_candidates(kind) in order, most specific first.
    The FIRST candidate that has ANY row in `channels` wins outright --
    resolution stops there, it never keeps searching for a "better"
    match once it has found a configured one:

    - enabled=0: explicit "do not announce this kind at all" -- returns
      None. NOT a fallback to a less-specific candidate or to
      discord_config's default; see discord_channel's own SCHEMA comment
      for why silently falling back here would be exactly wrong (an
      operator turning a kind off would see it keep posting to the main
      channel).
    - enabled=1 with a non-empty webhook_url: that row's own webhook.
    - enabled=1 with a BLANK webhook_url (turned on before a URL was
      ever pasted in): treated as not actually configured, so this
      falls through to discord_config's own default -- same as no row
      at all -- rather than trying to POST to an empty string.

    Only when NONE of the candidates has any row at all does this fall
    back to discord_config.webhook_url -- the original, single-channel
    behavior every deployment already has, unchanged for every kind an
    operator has never touched in the new channel table.
    """
    for candidate in _channel_kind_candidates(kind):
        row = channels.get(candidate)
        if row is None:
            continue
        if not row["enabled"]:
            return None
        if row["webhook_url"]:
            return row["webhook_url"]
        break
    return cfg.get("webhook_url") or None


def seed_discord_config_from_env(conn) -> None:
    """One-time bootstrap, called from app/db.py's init_db() on every
    startup: populates the discord_config singleton with exactly what
    settings.py already describes, the same guarded-by-updated_at
    pattern app/freqmapper_ingest.py's seed_freqmapper_config_from_env
    uses for freqmapper_config (see that function's own docstring for
    the full reasoning). Only fires while updated_at is still 0 --
    app/db.py's MIGRATIONS already guarantees the row exists (bare
    column defaults) by the time this ever runs, so this is an UPDATE,
    not an INSERT, and an operator's later edit through
    /api/admin/discord (which always sets updated_at to the current
    time) can never be silently overwritten by a later boot.

    `enabled` is seeded to whether a webhook is configured at all, not
    copied from any settings.py flag -- there never was one. A
    non-empty DISCORD_WEBHOOK_ANNOUNCEMENTS was itself the on/off switch
    before this table existed (see announcements_enabled() below), so
    this is what makes deploying this table change NO behavior for an
    already-live deployment: same webhook, same "is it actually on"
    answer, just moved from env-var-and-restart to database-and-admin-
    API. announce_month_honors is left untouched (the CREATE TABLE
    default of 1 already reproduces the always-on behavior that existed
    before this toggle did -- see discord_config's own comment in
    app/db.py).
    """
    row = conn.execute("SELECT updated_at FROM discord_config WHERE id = 1").fetchone()
    if row is None or row["updated_at"] != 0:
        return
    webhook_url = settings.discord_webhook_announcements
    conn.execute(
        "UPDATE discord_config SET enabled = ?, webhook_url = ?, username = ?, "
        " team_emoji = ?, guild_id = ?, app_id = ?, public_key = ?, updated_at = ? WHERE id = 1",
        (
            int(bool(webhook_url)),
            webhook_url,
            settings.discord_webhook_username,
            settings.discord_team_emoji,
            # guild_id (app/discord_bot.py's role sync): seeded here the
            # exact same one-time way as every other column in this
            # statement, but deliberately does NOT flip `roles_enabled`
            # -- that stays 0 (its own column default) until an operator
            # actually opts in through /api/admin/discord, even on a
            # deployment that already has DISCORD_GUILD_ID set.
            settings.discord_guild_id,
            # app_id / public_key (app/discord_interactions.py's slash
            # commands): same one-time seed, same deliberate omission of
            # `slash_enabled` from this statement -- that stays 0 (its
            # own column default) until an operator has actually
            # deployed the endpoint and opted in, even on a deployment
            # that already has both env vars set.
            settings.discord_app_id,
            settings.discord_public_key,
            int(time.time()),
        ),
    )
    log.info("discord: seeded config from settings (enabled=%s)", bool(webhook_url))


def announcements_enabled(cfg: dict) -> bool:
    """True only when the config's own `enabled` flag is set AND a
    webhook URL is configured -- both gates, not either alone: an
    operator can flip `enabled` off without touching the stored
    webhook (a quick "pause" that leaves the secret in place for later),
    and a blank webhook_url must never read as "on" no matter what
    `enabled` says. Mirrors app/oauth.py's provider_enabled(): empty
    means off, never open, so a fresh install with nothing configured
    never accumulates an outbox backlog (enqueue() below is a no-op
    while this is False) and run_forever()'s loop does nothing each
    cycle rather than trying to post to an empty string.

    Takes an already-loaded config dict (load_discord_config()'s own
    return shape) rather than a connection -- every caller here already
    has one loaded for the current cycle (enqueue(), _drain_once()), so
    this stays a pure, trivially-testable check rather than a second DB
    read every time it is asked.
    """
    return bool(cfg.get("enabled")) and bool(cfg.get("webhook_url"))


class DiscordSendError(Exception):
    """Raised by _post() on any failure posting to the configured
    webhook -- mirrors app/email_login.py's EmailSendError: callers
    (run_forever()'s drain loop) must treat this as "didn't send this
    time," never as a reason to crash the loop or lose the queued
    announcement, which stays in discord_outbox with posted_at still
    NULL for the next cycle to retry.

    A timeout or transport error message is a short, fixed string
    naming only the failure kind -- never the request itself, since
    httpx's own exception and request reprs include the URL, and a
    Discord webhook URL carries its own auth token in the path. A
    non-2xx status message additionally carries a truncated snippet of
    Discord's OWN response body, which is safe: it describes what was
    wrong with the payload this app sent, not the credential used to
    send it. See this module's own docstring and _post()'s.
    """


async def _post(url: str, payload: dict, *, http_client: httpx.AsyncClient | None = None) -> None:
    """POST one already-built Discord message body to `url` -- the
    configured webhook (discord_config.webhook_url, loaded fresh by the
    caller; see _drain_once() below, which loads it once per drain cycle
    rather than once per row). Raises DiscordSendError on a non-2xx
    response or any transport failure; never returns anything on
    success.

    `http_client` is accepted purely so tests can hand this an
    httpx.AsyncClient wired to an httpx.MockTransport (the same
    injectable-client shape app/oauth.py's exchange_code() already uses
    for its own outbound call) -- every real caller leaves it None and
    a short-lived client is opened and closed around this one request,
    since a month freezes at most a handful of times a year and there
    is no benefit to keeping a pooled connection open between them.
    """
    client = http_client
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=_POST_TIMEOUT_SECONDS)
    try:
        try:
            resp = await client.post(url, json=payload)
        except httpx.TimeoutException as e:
            # The REQUEST side: never str(e). See below.
            raise DiscordSendError("discord webhook post timed out") from e
        except httpx.HTTPError as e:
            # Deliberately not str(e) -- see this module's own
            # docstring and DiscordSendError's: httpx's own exception
            # text embeds the request URL, and that URL is this
            # deployment's webhook credential.
            raise DiscordSendError(f"discord webhook post failed ({type(e).__name__})") from e
        if resp.status_code < 200 or resp.status_code >= 300:
            # The RESPONSE side, not the request: Discord's own error
            # body names the exact problem with the payload (an invalid
            # field, a length limit, ...) and holds no credential --
            # unlike the request/exception text above, it is safe to
            # fold into the message. Capped to _MAX_ERROR_BODY_CHARS so
            # one row's stored last_error can never grow unbounded.
            snippet = (resp.text or "").strip()[:_MAX_ERROR_BODY_CHARS]
            detail = f": {snippet}" if snippet else ""
            raise DiscordSendError(f"discord webhook post returned HTTP {resp.status_code}{detail}")
    finally:
        if owns_client:
            await client.aclose()


def enqueue(conn, kind: str, key: str, payload: dict, now: int) -> bool:
    """Queue one announcement -- SYNC, and takes the CALLER's own
    connection, so it runs inside whatever transaction the caller is
    already holding (app/results.py's freeze_month(), inside the same
    WriteSession/BEGIN IMMEDIATE block that just wrote month_result/
    month_standing/month_award). Nothing here opens its own transaction
    or connection. Returns True when a new row was actually inserted,
    False for every no-op case below (disabled, gated off, or a
    duplicate (kind, key) the UNIQUE index silently dropped) -- used by
    check_due_time_driven() below to know how many of a due-check's
    candidate items actually turned into new rows, without a second
    query.

    A no-op when announcements are disabled (announcements_enabled() is
    False against the freshly loaded discord_config) -- a fresh or
    webhook-less install must never accumulate a discord_outbox backlog
    it will never drain, only to dump all of it the moment an operator
    finally configures a webhook months later.

    For kind="month_honors" specifically, also a no-op when
    discord_config.announce_month_honors is off -- a SEPARATE gate from
    `enabled`, so an operator can leave the webhook enabled (letting a
    manual kind="test" announcement from POST /api/admin/discord/test
    still go out) while turning off the automatic end-of-month post on
    its own. kind="season_close" and kind="weekly_recap" have the exact
    same per-kind shape, gated by announce_season_close and
    announce_weekly_recap respectively. Every colon-scoped
    kind="net_wrapup:<net id>" is gated the same way too, by
    announce_net_wrapup -- checked against the GENERIC prefix
    ("net_wrapup"), never the per-instance kind, since this is one
    on/off switch for every net's wrap-up, not a per-net one (see that
    column's own comment in app/db.py). Four independent on/off
    switches in total, each of which can be flipped without touching
    `enabled` or any of the others. No other kind is gated by any of
    them. (kind="place_activation" used to be a fifth -- see
    announce_place_activation's own comment in app/db.py's CREATE
    TABLE -- but nothing enqueues that kind any more, so this function no
    longer branches on it.)

    Also a no-op when discord_channel's per-kind routing
    (resolve_discord_webhook(), against _channel_kind_candidates(kind))
    resolves to None -- an operator explicitly switched this kind off.
    This is an ENQUEUE-TIME check only, deciding whether to queue at
    all; it never determines WHERE a queued row eventually posts -- that
    is resolved again, fresh, by _drain_once() at POST time, precisely
    so a channel move after a row is already queued still takes effect
    (see _drain_once()'s own docstring for why the URL itself is never
    stored on the outbox row).

    INSERT OR IGNORE on discord_outbox's UNIQUE(kind, key) index is the
    exactly-once guarantee: a duplicate (kind, key) -- the admin
    re-freeze route calling freeze_month() again for an already-frozen
    month, most likely, or check_due_time_driven() attempting the same
    period's item twice -- is silently dropped, never a second row and
    never a second post.
    """
    cfg = load_discord_config(conn)
    if not announcements_enabled(cfg):
        return False
    if kind == "month_honors" and not cfg["announce_month_honors"]:
        return False
    if kind == "season_close" and not cfg["announce_season_close"]:
        return False
    if kind == "weekly_recap" and not cfg["announce_weekly_recap"]:
        return False
    if kind.split(":", 1)[0] == "net_wrapup" and not cfg["announce_net_wrapup"]:
        return False
    channels = load_discord_channels(conn)
    if resolve_discord_webhook(cfg, channels, kind) is None:
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO discord_outbox(kind, key, payload, created_at) VALUES (?, ?, ?, ?)",
        (kind, key, json.dumps(payload), now),
    )
    return cur.rowcount == 1


def _fmt_number(value) -> str:
    """Render an award's numeric `value` the way frontend/results.js's
    own num() does -- a whole number reads as whole, never trailing
    ".0" (6005.0 -> "6005", not "6005.0") -- plus a thousands separator
    on top, since a Discord field is read at a glance in a chat
    scrollback rather than a lined-up table column, and a bare
    "6005" for a territory count is easy to misread by an order of
    magnitude in that context. 6005.0 -> "6,005".
    """
    n = float(value or 0)
    if n == int(n):
        return f"{int(n):,}"
    return f"{n:,.1f}"


def _detail_restates_value(value, detail) -> bool:
    """True when `detail` already begins with `value`'s own number --
    e.g. value=169.0, detail="169 s after the net opened" for
    'quick_fingers'. frontend/results.js's renderHonors() shows `value`
    and `detail` in two separate visual columns, so this shows no
    visible duplication there; a Discord field value is a single line
    (now two, see _value_unit_line() below), so printing both verbatim
    would read as a stutter: "**169** *169 s after the net opened*".

    The match only fires at the START of `detail` and only when
    followed by a space or end-of-string, checked against both
    _fmt_number()'s comma-formatted form and the plain integer string
    (a hand-written detail will never carry a thousands separator), so
    "9763 ft" matches value=9763 but "1690 squares past the towns" does
    NOT falsely match value=169 (169 is a STRING-prefix of "1690", but
    "169 " is not).

    Deliberately generic rather than keyed on the 'quick_fingers' award
    name: any future award whose detail embeds its own number would hit
    the exact same stutter here.
    """
    if value is None or not detail:
        return False
    formatted = _fmt_number(value)
    prefixes = {formatted}
    n = float(value)
    if n == int(n):
        prefixes.add(str(int(n)))
    return any(detail == p or detail.startswith(p + " ") for p in prefixes)


def _value_unit_line(value, detail) -> str:
    """The second line of a headline award's two-line rendering (owner
    feedback: "needs spacing and bold and italics ... to really drive
    it" -- a wrapping "Largest Territory -> GREEN -- 6,005 squares
    held" in a narrow inline column read as a wall of plain text):
    "**<number>** *<unit>*", bold number and italic unit, e.g.
    "**6,005** *squares held*".

    When `detail` already restates the number (_detail_restates_value()
    -- quick_fingers-shaped), the bold number is dropped entirely and
    this is just "*<detail>*", to avoid a doubled
    "**169** *169 s after the net opened*". Returns "" when there is
    neither a value nor a detail to show (an award line with only a
    `who`), so the caller never emits a bare "\\n" or an empty "****".
    """
    if _detail_restates_value(value, detail):
        return f"*{detail}*"
    parts = []
    if value is not None:
        parts.append(f"**{_fmt_number(value)}**")
    if detail:
        parts.append(f"*{detail}*")
    return " ".join(parts)


def _award_line(a: dict) -> str:
    """Two lines for one non-placeholder headline award:
    "<winner>\\n**<number>** *<unit>*" (see _value_unit_line() for the
    second line, including the quick_fingers de-duplication). Renders
    all three of who, the number, and its unit -- frontend/results.js's
    own renderHonors() shows all three for the same reason its comment
    gives: "Top NetOp 130" without a unit is the exact ambiguity the
    detail exists to fix, and a bare who with no number at all (this
    module's old bug) is that same ambiguity made worse. Falls back to
    just `who` when there is nothing else to show.
    """
    who = a.get("player") or a.get("team") or "Unknown"
    tail = _value_unit_line(a.get("value"), a.get("detail"))
    return f"{who}\n{tail}" if tail else who


# A spaced middle dot, written as an escape (not the literal multi-byte
# character) so this file stays plain ASCII on disk. Owner feedback on
# a by-team screenshot: team, player, and number ran together with
# nothing but plain spaces between them, making it hard to tell where
# the team name ended and a handle like "l3@n" or "KI7NOX" began --
# "still need a dileniator between the COLOR Player <points>". The
# "--" separator was removed from this exact line by the immediately
# preceding change (see _team_award_line()'s history) because it was
# heavy, repeated 30+ times across a by-team field, and read as noise
# -- but the line still needs SOME token boundary, and a spaced middle
# dot gives one at a fraction of the visual weight. Used by
# _team_award_line() below and by _weekly_exploration_section()'s
# per-player "new" count line -- every one of them a line with more than
# two tokens that would otherwise run together. NOT used by
# _weekly_placement_section(), whose "TEAM **rank** <arrow-or-italic-
# tail>" shape separates its own tokens with plain spaces and Discord's
# own italics instead (see that function's own comment). Deliberately
# NOT applied to the standings lines
# (build_month_honors_embed()'s standings_text, already just
# "<dot>TEAM **<number>**", two unambiguous tokens) or the headline
# honors field values (_award_line()/_value_unit_line(), already split
# across two separate lines), neither of which has this run-together
# problem.
_SEP = " \u00b7 "


def _team_award_line(a: dict, emoji: dict[str, str]) -> str:
    """One compact line inside a grouped per-team field: "TEAM
    <_SEP> <winner> <_SEP> **<number>**", prefixed with that team's
    coloured dot (_team_dot()) when configured, and no per-line unit
    (the owner's "wallish" complaint: the same unit phrase repeated on
    all 7 lines of a by-team block). The unit is appended ONCE for the
    whole field instead -- see _join_team_field() below, which this
    function's caller feeds these lines into. See _SEP's own comment
    for why a spaced middle dot separates the tokens here rather than
    the "--" this line used before, or nothing at all.

    Most per-team awards (team_attacker, team_defender, ...) are a
    property of the team itself, so `who` (player() or team()) is just
    the scope team again -- "GREEN . GREEN . **40**" says GREEN twice
    for nothing, so the leading "TEAM" stands in for `who` and is
    dropped in that case. A per-team award that DOES name a player
    distinct from its scope (a team's own top scorer, say) keeps that
    player's name after the team instead -- but the dot is always keyed
    on `scope` (the team the line is grouped under), never the player.

    `value` is None only for a shape this module has never actually
    produced (every real per-team award carries a number), but even
    then this must never leave a dangling trailing separator -- so the
    bold-number segment, `_SEP` included, is only appended when there
    is a value to put after it.
    """
    scope = a.get("scope") or ""
    who = a.get("player") or a.get("team") or "Unknown"
    dot = _team_dot(emoji, scope)
    value = a.get("value")
    bold = f"{_SEP}**{_fmt_number(value)}**" if value is not None else ""
    if who == scope:
        return f"{dot}{scope}{bold}"
    return f"{dot}{scope}{_SEP}{who}{bold}"


def _join_team_field(lines: list[str], unit: str | None) -> str:
    """Assemble one "By team" field's full value: every team's line
    (_team_award_line()'s own output, one per team, already in
    standings order), then -- when `unit` is given -- a blank line and
    the unit ONCE, italicised: "*squares held*". This is the other half
    of the de-densifying this module's Change 4 makes: six of the seven
    repetitions of the unit phrase are simply gone, replaced by this one
    trailing line.

    Guards Discord's own hard 1024-character-per-field-value limit
    (_MAX_FIELD_VALUE_CHARS) -- unlike the 25-field and 6000-char
    budgets elsewhere in this module, which drop a whole embed, this
    trims from the END of `lines` (dropping the lowest-ranked teams
    first, since `lines` arrives in standings order) one at a time,
    replacing the trailing unit line with a single plain
    _TRUNCATION_MARKER line, until the assembled value fits. A field
    that still doesn't fit with zero team lines left (pathological: the
    marker text itself would somehow exceed the limit) is hard-cut to
    the limit as a last resort -- this should never happen in practice
    but must never hand Discord an over-long value that 400s the whole
    message.
    """
    def render(ls: list[str], *, truncated: bool) -> str:
        parts = list(ls)
        if truncated:
            parts.append(_TRUNCATION_MARKER)
        elif unit:
            parts.append("")
            parts.append(f"*{unit}*")
        return "\n".join(parts)

    value = render(lines, truncated=False)
    if len(value) <= _MAX_FIELD_VALUE_CHARS:
        return value

    remaining = list(lines)
    while remaining:
        remaining.pop()
        value = render(remaining, truncated=True)
        if len(value) <= _MAX_FIELD_VALUE_CHARS:
            return value
    return _TRUNCATION_MARKER[:_MAX_FIELD_VALUE_CHARS]


def build_month_honors_embed(conn, month: str, protocol: str, result: dict) -> dict:
    """The full Discord webhook JSON body (an `embeds` list of up to
    three embeds -- Standings, Honors, By team; see below for when the
    latter two are omitted) for one frozen month's result, as returned
    by app/results.py's compute_month()/freeze_month() -- standings and
    awards. Plain text throughout, with exactly one deliberate
    exception: a per-team coloured-dot custom emoji (discord_config.
    team_emoji, loaded fresh via load_discord_config() and parsed by
    _parse_team_emoji()) prefixed onto every line that names a team,
    when an operator has configured one for that team -- see
    _team_dot()'s own comment for the fallback that keeps a deployment
    without one rendering exactly as before. This is the ONLY emoji this
    module ever emits; nothing else here invents its own.

    Awards use results.AWARD_LABELS (via each award dict's own `label`,
    already set by compute_month()) so this never invents its own
    wording for an award. Unwon placeholders (with_placeholders() in
    app/results.py -- player_id and team both None) are skipped: an
    award nobody won has nothing to announce. Imported locally, not at
    module level -- app/results.py imports THIS module (to call
    enqueue() from inside freeze_month()), so importing results back at
    module level here would be a circular import; see freeze_month()'s
    own comment on its side of this.
    """
    from . import results

    cfg = load_discord_config(conn)
    proto_label = _PROTOCOL_NAMES.get(protocol, protocol)
    emoji = _parse_team_emoji(cfg["team_emoji"])

    standings = sorted(
        result.get("standings") or [],
        key=lambda s: (-(s.get("squares") or 0), s.get("team") or ""),
    )
    if standings:
        # "<dot> TEAM **<number>**" -- team name plain (the coloured dot
        # already identifies it), number bold. The unit
        # (_STANDINGS_UNIT) is deliberately NOT repeated on every one of
        # these lines any more -- see standings_embed's own `footer`
        # below, where it is stated exactly once for the whole embed
        # instead.
        standings_text = "\n".join(
            f"{_team_dot(emoji, s.get('team'))}{s.get('team')} "
            f"**{_fmt_number(s.get('squares', 0))}**"
            for s in standings
        )
    else:
        standings_text = "No standings recorded."

    # A month has up to 10 headline awards (scope empty/None) plus one
    # per-team award per team per per-team award key -- for a 5-team
    # season that was 20 more rows, and one Discord field each blew
    # straight through _MAX_EMBED_FIELDS (a real August announcement:
    # 10 + 20 = 30 fields, rejected outright with an HTTP 400). Mirror
    # how frontend/results.js already solves this for the same data
    # (splitAwards()/renderTeamAwards(), and that function's own
    # comment on why): a headline award still gets its own field, but
    # per-team awards are grouped into ONE field per award KEY, with
    # every team's line inside that field's value instead of a field of
    # its own.
    #
    # The two groups are also now two SEPARATE embeds (Honors, By team
    # -- see below) rather than one combined `fields` list: the owner's
    # own feedback on the old single-embed shape was that 13 stacked
    # full-width fields read as "DENSE," and headline awards (short,
    # one-line values) and per-team awards (multi-line lists) don't
    # want the same field width either -- see the `inline` settings
    # below.
    team_rank = {s.get("team"): i for i, s in enumerate(standings)}
    headline_fields = []
    team_awards: dict[str, list[dict]] = {}
    team_award_order: list[str] = []
    for a in result.get("awards") or []:
        if a.get("player_id") is None and a.get("team") is None:
            continue  # unwon placeholder -- see with_placeholders() -- nothing to announce
        scope = a.get("scope")
        if scope:
            key = a.get("award")
            if key not in team_awards:
                team_awards[key] = []
                team_award_order.append(key)
            team_awards[key].append(a)
        else:
            label = a.get("label") or results.AWARD_LABELS.get(a.get("award"), a.get("award"))
            # inline=True: headline awards are short one-line values, so
            # Discord lays these out three-across instead of stacking
            # each one the full width of the message -- the owner's own
            # "DENSE" complaint about the old shape.
            # The award's own `team` field says which team the winner
            # belongs to (a player award carries both `player` and
            # `team`) -- that, not the award's scope (headline awards
            # have none), is what the dot is keyed on.
            dot = _team_dot(emoji, a.get("team"))
            headline_fields.append({"name": label, "value": f"{dot}{_award_line(a)}", "inline": True})

    team_fields = []
    for key in team_award_order:
        group = team_awards[key]
        # Same order the standings table above is drawn in, so a reader
        # scanning down one lines the two up -- same reasoning
        # frontend/results.js's renderTeamAwards() gives for its own
        # `order` list. A team absent from standings (nothing held, no
        # check-ins, no exploration) still gets a line, sorted after
        # every ranked team.
        group.sort(key=lambda a: (team_rank.get(a.get("scope"), len(team_rank)), a.get("scope") or ""))
        label = group[0].get("label") or results.AWARD_LABELS.get(key, key)
        lines = [_team_award_line(a, emoji) for a in group]
        # The unit is a property of the award KEY (every team in one
        # group is winning the same kind of award, e.g. "squares taken
        # from other teams"), not of any one team's line any more --
        # see _team_award_line()'s own comment. Taken from whichever
        # group member has a non-empty `detail`, first one found, since
        # in practice every member of a group shares the same wording.
        unit = next((a.get("detail") for a in group if a.get("detail")), None)
        # inline=False, deliberately unlike headline_fields above: each
        # of these values is a multi-line list (one line per team), and
        # squeezing a multi-line list into a third of the message width
        # would be unreadable rather than merely dense.
        team_fields.append({"name": label, "value": _join_team_field(lines, unit), "inline": False})

    # Belt-and-suspenders, PER EMBED: whatever the grouping above
    # produces, never hand Discord more fields in one embed than its
    # own hard limit -- see _MAX_EMBED_FIELDS's own comment for why a
    # 26th field is not a partial failure but a 400 for the whole
    # message. Honors and By team are now separate embeds, so each is
    # capped independently rather than against one shared 25-field
    # budget.
    headline_fields = headline_fields[:_MAX_EMBED_FIELDS]
    team_fields = team_fields[:_MAX_EMBED_FIELDS]

    base_url = (settings.oauth_public_base_url or "").rstrip("/")

    standings_embed = {
        "title": f"{proto_label} — {_month_title(month)}",
        "description": standings_text,
    }
    # The unit for every line above, stated ONCE for the whole embed --
    # see _STANDINGS_UNIT's own comment for why this is a named
    # constant rather than a second inline literal. Omitted when there
    # are no standings at all (standings_text is the plain
    # "No standings recorded." sentence, not a list of figures the
    # footer would be labelling).
    if standings:
        standings_embed["footer"] = {"text": _STANDINGS_UNIT}
    # A Discord embed's "url" must be an ABSOLUTE url -- a relative one
    # (e.g. "/results") makes Discord reject the ENTIRE message with an
    # HTTP 400, not just drop the link. So when OAUTH_PUBLIC_BASE_URL
    # isn't configured, omit the "url" key entirely rather than falling
    # back to a relative path. This only bites a deployment that has
    # not set OAUTH_PUBLIC_BASE_URL, which is why it was invisible here.
    if base_url:
        standings_embed["url"] = f"{base_url}/results"

    # Never emit an embed with an empty `fields` list -- Discord allows
    # that, but it renders as a bare, pointless title with nothing under
    # it, so a group with nothing to show is left out of the message
    # entirely rather than sent as an empty shell.
    honors_embed = {"title": "Honors", "fields": headline_fields} if headline_fields else None
    team_embed = {"title": "By team", "fields": team_fields} if team_fields else None

    # The month's colour: the LEADING team (first entry of `standings`,
    # already sorted by squares descending above) -- the post itself
    # reads as that month's winning team's colour, so the colour
    # carries information rather than being decoration. This is the
    # same reasoning frontend/theme.css gives for team colours not
    # being a skinnable/brand choice: they mean something in the game,
    # not just on screen. Omitted -- never guessed at a default --
    # when there are no standings at all, or the leading team's name
    # isn't one _TEAM_COLORS knows.
    leading_team = standings[0].get("team") if standings else None
    color = _team_color(leading_team)
    embeds = [standings_embed]
    if honors_embed is not None:
        embeds.append(honors_embed)
    if team_embed is not None:
        embeds.append(team_embed)
    if color is not None:
        for e in embeds:
            e["color"] = color

    # Discord's 6000-character TOTAL budget across every embed in the
    # message (_MAX_TOTAL_EMBED_CHARS's own comment) is separate from,
    # and in addition to, the per-embed field-count guard above. If the
    # assembled payload would exceed it, "By team" is dropped first and
    # entirely, never truncated: it is the least important of the three
    # embeds (a reader still gets the full month from Standings +
    # Honors alone), and a truncated field value is far more confusing
    # on screen than that field simply not being there.
    if team_embed is not None and _total_embed_chars(embeds) > _MAX_TOTAL_EMBED_CHARS:
        embeds = [e for e in embeds if e is not team_embed]

    # "username" overrides the webhook's own configured display name --
    # without it, Discord shows whatever the webhook happened to be
    # named when it was created in the channel's Integrations settings
    # (Discord's own unrenamed placeholder, "Captain Hook," if nobody
    # bothered to change it), which says nothing about what app actually
    # posted. Falls back to the literal "MeshWars" if the config value is
    # ever blank -- see settings.discord_webhook_username's own comment
    # for why this is the one field in this section that is never simply
    # omitted when unset.
    return {
        "username": cfg["username"] or "MeshWars",
        "embeds": embeds,
    }


def build_season_close_embed(conn, protocol: str, season_row, tallies: list[dict]) -> dict:
    """The full Discord webhook JSON body for one closed MeshCore/
    Meshtastic season (app/mc_scoring.py's maybe_roll_season(), called
    inside that function's own write transaction, AFTER the closing
    season's rows are written -- see that function's own comment for
    why the ordering matters).

    Mirrors build_month_honors_embed()'s structure and every hard-won
    rule its own docstring and inline comments already establish: team
    dots from discord_config.team_emoji with the same plain-text
    fallback (_team_dot()), bold numbers via _fmt_number(), an
    italicised unit, never a "--" separator, an absolute-or-omitted
    `url`, and Discord's own _MAX_EMBED_FIELDS/_MAX_FIELD_VALUE_CHARS/
    _MAX_TOTAL_EMBED_CHARS budgets. The standings field reuses
    _join_team_field() verbatim -- a season's final standings are
    exactly the same shape as a month's "By team" field (one line per
    team, trimmed from the bottom if it would ever overflow
    _MAX_FIELD_VALUE_CHARS) -- so this embed's two short fields
    (standings, winner) never come close to the 6000-character total
    budget in practice; there is nothing this function could drop and
    still say anything meaningful, unlike build_month_honors_embed()'s
    three-embed message, so no drop step is needed here.

    `season_row` is the CLOSED season's own row shape (at minimum `id`
    and `winner` -- either a team name or the literal 'TIE', see
    maybe_roll_season()'s own docstring). `tallies` is a list of
    {"team", "total"} dicts -- the SAME team_totals() figure
    maybe_roll_season() used to decide the winner in the first place
    (squares held + check-in points + Places Worth Going points), not
    mc_season_team_tally's own persisted columns (which hold only the
    squares/check-in split, for history -- see that table's own comment
    in app/db.py). Passing the exact number that decided the winner,
    rather than re-deriving a different one here, guarantees this
    announcement can never show a standings order that disagrees with
    the winner it names.

    winner == 'TIE' is handled explicitly: rendered as "It's a tie!",
    never as a bare "TIE" that would read as a team's name, and no
    colour is looked up for it -- _team_color('TIE') would already
    return None (it is simply absent from _TEAM_COLORS), but this is
    spelled out rather than relied on as a coincidence.

    Imported LOCALLY by maybe_roll_season() (app/mc_scoring.py), not the
    other way -- this module has no reason to import mc_scoring at all,
    so there is no circular-import concern here the way
    build_month_honors_embed() has with app/results.py.
    """
    cfg = load_discord_config(conn)
    proto_label = _PROTOCOL_NAMES.get(protocol, protocol)
    emoji = _parse_team_emoji(cfg["team_emoji"])

    standings = sorted(
        tallies or [],
        key=lambda t: (-(t.get("total") or 0), t.get("team") or ""),
    )
    lines = [
        f"{_team_dot(emoji, t.get('team'))}{t.get('team')} **{_fmt_number(t.get('total', 0))}**"
        for t in standings
    ]
    standings_value = _join_team_field(lines, _SEASON_TOTAL_UNIT) if lines else "No standings recorded."

    winner = season_row["winner"]
    if winner is None or winner == "TIE":
        winner_value = "It's a tie!"
        color = None
    else:
        winner_value = f"{_team_dot(emoji, winner)}**{winner}**"
        color = _team_color(winner)

    fields = [
        {"name": "Final standings", "value": standings_value, "inline": False},
        {"name": "Winner", "value": winner_value, "inline": False},
    ][:_MAX_EMBED_FIELDS]

    embed = {
        "title": f"{proto_label} — Season #{season_row['id']} closed",
        "fields": fields,
    }
    base_url = (settings.oauth_public_base_url or "").rstrip("/")
    # Same absolute-or-omitted rule as build_month_honors_embed()'s own
    # standings_embed url -- a relative path here would make Discord
    # reject the ENTIRE message with an HTTP 400, not just drop the link.
    if base_url:
        embed["url"] = f"{base_url}/results"
    if color is not None:
        embed["color"] = color

    return {
        "username": cfg["username"] or "MeshWars",
        "embeds": [embed],
    }


async def _drain_once(*, http_client: httpx.AsyncClient | None = None) -> None:
    """One pass over the pending rows in discord_outbox -- the unit
    run_forever() repeats on its own interval, pulled out on its own so
    tests can exercise a single cycle without the sleep loop around it.

    Reads the pending rows with a plain connect() (not WriteSession):
    the SELECT itself needs no write lock, and the actual HTTP POST for
    each row happens entirely outside any lock -- see this function's
    own body -- so a slow or hung webhook can never hold up every other
    write in the app the way it would if this ran inside WriteSession.
    The write lock is taken again, briefly, only to record each row's
    outcome (_mark_posted/_mark_failed below).

    A row is skipped -- left pending, never posted, never touched --
    when it is older than discord_outbox_max_age_hours (a long outage
    must never dump stale news the moment a webhook is fixed) or has
    already failed discord_outbox_max_attempts times (a permanently
    broken webhook must eventually stop being retried). This age cutoff
    is now the thing that actually gives the self-healing time-driven
    providers (weekly_recap_provider(), net_wrapup_provider() -- see
    TIME_DRIVEN_PROVIDERS's own docstring) their outer bound: those
    providers ask "has the most recent completed period been posted
    yet" with no upper limit of their own on how long ago that period
    was (see _weekly_recap_period()'s and _due_net_wrapups()'s own
    "CRITICAL" notes on why they still only ever look at the SINGLE most
    recent one, never a backlog) -- so a row that WAS enqueued for a
    period but never actually delivered inside discord_outbox_max_age_
    hours ages out right here and simply stops being retried, rather
    than eventually posting as stale news once whatever kept it from
    sending is fixed. Before this change this cutoff mattered only for
    an ordinary send failure retried past its window; now it is the
    thing standing between "briefly undeliverable" and "delivered days
    late," which is exactly what it is for. Both this and the attempts
    cutoff are plain WHERE clauses, not a Python-side filter, so a
    skipped row is never even fetched. discord_config AND discord_channel are each loaded
    ONCE per cycle here, not once per row -- an admin editing either
    mid-cycle takes effect on the NEXT cycle, the same granularity
    app/freqmapper_ingest.py's own poll loop already applies to its
    config -- and every row's own webhook is resolved
    (resolve_discord_webhook()) against that one already-loaded snapshot
    rather than a fresh discord_channel query per row.

    THIS is the POST-TIME resolution the outbox's own design depends on:
    a row never carries its own target URL, only `kind`, so a channel
    move an operator makes after a row was already queued is picked up
    the very next time this function runs, for every row still pending
    -- never the channel that happened to be configured back when
    enqueue() first wrote it.

    A row whose kind currently resolves to None (an operator switched it
    off via discord_channel since it was queued) is skipped exactly like
    an aged-out or attempts-exhausted row -- left pending, attempts and
    last_error untouched, not counted as a failure, because nothing was
    actually attempted. It simply waits: if the kind is re-enabled later
    it posts on the next cycle, and it still ages out via created_at like
    any other pending row.
    """
    now = int(time.time())
    cutoff = now - settings.discord_outbox_max_age_hours * 3600

    conn = connect()
    try:
        cfg = load_discord_config(conn)
        if not announcements_enabled(cfg):
            return
        channels = load_discord_channels(conn)
        rows = conn.execute(
            "SELECT id, kind, payload, attempts FROM discord_outbox "
            " WHERE posted_at IS NULL AND created_at >= ? AND attempts < ? "
            " ORDER BY id",
            (cutoff, settings.discord_outbox_max_attempts),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        webhook_url = resolve_discord_webhook(cfg, channels, row["kind"])
        if webhook_url is None:
            # Explicitly switched off since this row was queued -- see
            # this function's own docstring. Left pending, untouched.
            continue
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            # A row that can never be parsed can never be posted either
            # -- treated as an ordinary failure so it ages out through
            # the same attempts/max_age rules as a real send failure,
            # rather than being retried forever for a reason no retry
            # can fix.
            await _mark_failed(row["id"], row["attempts"], "stored payload is not valid JSON")
            continue
        try:
            await _post(webhook_url, payload, http_client=http_client)
        except DiscordSendError as e:
            await _mark_failed(row["id"], row["attempts"], str(e))
            continue
        except Exception:
            # Never let one row's unexpected failure stop the rest of
            # this cycle, or the loop itself -- same contract
            # app/account_api.py's _notify_security() applies to a
            # single mail send. type(e).__name__ only, never str(e):
            # an exception raised from inside httpx's own request path
            # can carry the request (and so the webhook URL) in its
            # text even when it isn't one of the httpx.HTTPError/
            # TimeoutException cases _post() already sanitizes.
            log.exception("discord outbox: unexpected error posting row %d", row["id"])
            await _mark_failed(row["id"], row["attempts"], "unexpected error")
            continue
        await _mark_posted(row["id"])


async def _mark_posted(row_id: int) -> None:
    now = int(time.time())
    async with WriteSession() as conn:
        conn.execute("UPDATE discord_outbox SET posted_at = ? WHERE id = ?", (now, row_id))


async def _mark_failed(row_id: int, prior_attempts: int, error: str) -> None:
    async with WriteSession() as conn:
        conn.execute(
            "UPDATE discord_outbox SET attempts = ?, last_error = ? WHERE id = ?",
            (prior_attempts + 1, error, row_id),
        )



# ---------------------------------------------------------------------
# Time-driven announcements -- the "clock" for kinds with no triggering
# event to enqueue() from.
#
# month_honors hangs off a real event: app/results.py's freeze_month()
# calls enqueue() itself, inside its own write transaction, the moment a
# month closes. Some kinds -- the Sunday weekly recap (weekly_recap_
# provider() below, this module's first real provider), a per-net
# wrap-up posted the day after a net (still a worked example only, see
# below) -- have no such event; nothing else in this app ever calls a
# function at "the day after Tuesday's net." This app
# has NO scheduler by design and must not grow one (no cron, no APScheduler,
# no extra background task per kind) -- so instead, the ALREADY-RUNNING
# drain loop (run_forever(), polling every discord_outbox_poll_interval_
# seconds, currently 30s) asks once per cycle, cheaply, "is anything
# time-driven due right now," via TIME_DRIVEN_PROVIDERS and
# check_due_time_driven() below, BEFORE that cycle's _drain_once() --
# so anything enqueued here still gets posted in the very same cycle.
#
# TIME_DRIVEN_PROVIDERS: list[Callable[[sqlite3.Connection, int], list[dict]]]
#
# Each entry is a PROVIDER FUNCTION -- not a static description of one
# announcement -- called as provider(conn, now), fresh, on every single
# due-check, returning a list of ZERO OR MORE items due RIGHT NOW. `conn`
# is check_due_time_driven()'s own caller's connection (the same one
# enqueue() itself takes), passed straight through rather than opened
# fresh here: a provider needs database access to decide what is due
# (see the checkin_net-derived worked example below, which SELECTs from
# it), and check_due_time_driven() already runs inside an open
# WriteSession (_check_due_time_driven_once() below) -- a provider
# opening a SECOND connection of its own would be a second writer
# competing for the same in-flight write lock, not a second reader.
# `now` is that same caller's clock (int(time.time()), read once for the
# whole cycle) so every provider in one due-check agrees on what instant
# "right now" means, rather than each one calling time.time() itself a
# few microseconds apart.
#
#   [{"kind": str, "key": str, "payload": dict}, ...]
#
# A provider decides for itself how many items that is. A fixed weekly
# thing returns at most one item PER PROTOCOL (usually zero: due only
# once a week) -- weekly_recap_provider() below is exactly this shape.
# THE REASON this is a list of PROVIDERS rather than a single static
# registry entry: a provider can instead be NET-DERIVED, returning one
# item per currently-due row of a table that itself changes over time --
# see net_wrapup_provider() below, which is exactly this shape.
#
# "kind" is discord_outbox's routing key -- resolve_discord_webhook()
# resolves it via _channel_kind_candidates() exactly like any other
# announcement, so a colon-scoped kind (e.g. "net_wrapup:12") is routed
# to a per-instance channel first, falling back to the generic prefix,
# then to discord_config's own default, with zero special-casing here.
# "key" is discord_outbox's DEDUPE key: passed straight to enqueue(),
# relying ENTIRELY on discord_outbox's existing UNIQUE(kind, key) index
# for "once per period." There is deliberately NO separate "last run"
# timestamp or table anywhere in this feature -- that would be a SECOND
# source of truth for the exact same fact discord_outbox already answers
# durably (a row for this (kind, key) exists, or it does not), and the
# two could drift out of step with each other. A provider proves an item
# is due by computing that period's own key; check_due_time_driven()
# below proves "not already sent" for free, via enqueue()'s own INSERT OR
# IGNORE, every single time it is called -- calling it twice inside the
# same period is always exactly as safe as calling it once.
#
# net_wrapup_provider() below is the worked example this shape was
# always meant for, now real: a per-net "wrap-up" announcement, one per
# checkin_net row (app/db.py: id, label, protocol, weekday, start_hour,
# end_hour, timezone, enabled -- live deployments span multiple IANA
# zones across multiple weekdays). Matt's own words on why this can't be
# a fixed entry: "there WILL be more communities this should not be
# hardcoded but adapted and computed directly from the net schedules."
# So net_wrapup_provider(), on every call (given its own (conn, now)):
#
#   1. SELECTs the ENABLED rows of checkin_net, on the `conn` it was
#      handed -- nothing about their count, weekdays, labels, or
#      timezones is ever written into code; a net added through the
#      admin UI starts getting wrap-ups on its own very next due net
#      with NO code change and NO redeploy, and a disabled/deleted net
#      simply stops appearing in this SELECT and so never produces one
#      again.
#   2. for EACH row, decides "is it due" against the passed-in `now`
#      (never time.time() called fresh inside the provider -- every
#      provider in one due-check must agree on the same instant) and
#      computes the local calendar date the wrap-up covers using THAT
#      ROW'S OWN `weekday` and `timezone` (zoneinfo.ZoneInfo(row
#      ["timezone"]), the same per-entry-timezone pattern
#      app/results.py's own _tz() already establishes for month
#      arithmetic) -- NEVER one single app-wide clock, because two nets
#      can be due on different calendar days, in different zones, at
#      the same instant. See _due_net_wrapups() below for the exact rule.
#   3. for each due net, yields one item shaped like:
#        kind = f"net_wrapup:{net['id']}"     -- per-net ROUTING, falls
#                                                 back to the generic
#                                                 "net_wrapup" channel
#        key  = f"{net['id']}:{local_date}"   -- per-net, per-date
#                                                 DEDUPE, so the SAME
#                                                 net's SAME date is
#                                                 never announced twice
#
# CHEAPNESS: this whole check runs every discord_outbox_poll_interval_
# seconds (30s by default) FOREVER, for the life of the process, ON THE
# SAME WriteSession CONNECTION the caller is already holding open (see
# _check_due_time_driven_once() below) -- every provider in this list
# must stay cheap, because a slow provider here holds the write lock
# just as long as a slow enqueue() would. checkin_net has a handful of
# rows (four, today) and reading all of it every cycle is fine -- even
# many more communities is still a tiny table -- but this is NOT a
# license for a provider to run anything heavier: a provider must NEVER
# query a large/growing table (mc_tile, mc_tile_capture_log,
# player_ingest_stat, ...) directly from here. A provider that needs a
# heavier computation to decide "am I due" must precompute or cache that
# decision elsewhere and read only the cheap, already-decided state in
# this function.
#
# weekly_recap_provider() below is a DELIBERATE, NARROW exception to the
# "never query a large table" rule just above -- see its own docstring
# for exactly why that is safe here: the period arithmetic itself
# (_weekly_recap_period()) is pure date math with no DB access at all,
# and the one discord_outbox lookup that follows it (per protocol) is
# cheap and indexed -- so the heavy build_weekly_recap_embed() path is
# only ever reached for a period that has not been posted yet, which in
# the ordinary case is at most once per protocol per ISO week (every
# later poll cycle that same week short-circuits on the outbox lookup
# before ever reaching it). No other provider gets this exception
# without the same reasoning holding for it.

# Cap on how many players' first-count lines the Exploration section of
# the weekly recap lists -- a busy week must never blow Discord's
# field-value budget. "Top 5": qualifying_place_firsts()'s own measured
# volume is ~82 qualifying firsts a MONTH (about 2 a day), so even the
# busiest realistic week touches only a handful of distinct players and
# this is headroom, not a truncation that fires in practice.
_MAX_WEEKLY_RECAP_PLAYERS = 5


def _season_id_for_ts(conn, protocol: str, ts: int) -> int | None:
    """The mc_season row covering `ts` for `protocol`, half-open
    [started_at, ends_at) -- the same time-based season resolution
    app/mc_scoring.py's team_place_points() already applies from a KNOWN
    season_id outward to its own boundary (place_activation has no
    season_id column -- see that function's docstring, and
    qualifying_place_firsts()'s in app/place_scoring.py, for why a place
    credit is scoped by TIME against mc_season instead). This is the
    same idea run in the other direction: given a timestamp with no
    season_id yet, find which season it falls in.

    Unlike app/results.py's ownership_at() -- which only checks
    started_at against a chain of seasons it assumes are CONTIGUOUS, and
    so never needs an ends_at bound at all -- this checks both ends of
    the interval explicitly, because a weekly recap's window could in
    principle land in a gap between seasons (or before the first one
    ever started). Returns None in that case rather than guessing at the
    nearest season.
    """
    row = conn.execute(
        "SELECT id FROM mc_season WHERE protocol = ? AND started_at <= ? AND ends_at > ? "
        "ORDER BY started_at DESC LIMIT 1",
        (protocol, ts, ts),
    ).fetchone()
    return row["id"] if row is not None else None


def _weekly_recap_period(now: int) -> tuple[str, int, int]:
    """(period_key, start_ts, end_ts) for the most recently COMPLETED
    week, ending at the most recent local Sunday 00:00 at or before
    `now`, in settings.checkin_net_timezone -- the same app-wide local
    clock a month (app/results.py's _tz()), a net date
    (app/checkin.py's net_date_for_net()), and a place activation's
    week_start (app/place_rotation.py's week_start_for_ts()) all already
    use.

    SELF-HEALING dueness, not "is it the trigger moment right now": the
    old version of this function returned None on every day but Sunday,
    so a service outage spanning a whole Sunday meant the week's own
    recap was never computed at all -- Monday is not Sunday either, so
    the very next poll cycle no longer recognised anything as due, and
    that week's news was gone for good. This function instead always
    answers "which week most recently finished," which is true on every
    day of the week, not just the one it happens to end on -- so
    weekly_recap_provider() below can ask this on a Tuesday and get back
    exactly the week that ended the preceding Sunday, still due for as
    long as it has not yet been posted (see that function's own
    docstring for the check that decides THAT). This mirrors
    app/results.py's maybe_roll_months(), which likewise asks "which
    months are finished and not yet frozen" rather than "is it midnight
    on the 1st" -- the same shift from a trigger-moment check to a
    completed-period query, for the same reason: a missed moment must
    not mean a lost period.

    CRITICAL: this returns ONLY the single most recently completed week,
    never a backlog of every unposted week since some earlier date --
    there is deliberately no loop here walking backwards over past
    weeks. Unlike maybe_roll_months() (which DOES walk every unfrozen
    month, because a missed month freeze would silently under-count
    every later month's standings), an old, unposted weekly recap has no
    such downstream correctness cost -- it is a point-in-time news post,
    not a running total -- so there is nothing to gain and real harm in
    ever posting it late among a flood of others. Turning this feature
    on for the first time, or recovering from a long outage, must never
    dump weeks of old recaps into the channel; it must simply resume
    with the most recent one. Whatever happened before that is gone,
    deliberately, not backfilled.

    The window is the SEVEN DAYS ENDING at that Sunday's local midnight
    -- built from local calendar boundaries the same way
    app/results.py's month_bounds() builds a month, not a fixed
    7 * 86400 offset, so a week that crosses a daylight-saving change is
    still exactly seven calendar days, never an hour short or long.

    The period key is the ISO week of that Sunday, e.g. "2026-W37" --
    the same key on EVERY call made before the next Sunday passes,
    whether that is 2,880 thirty-second polls on the Sunday itself or
    the same number of polls spread across the outage-recovery days that
    follow it. discord_outbox's own UNIQUE(kind, key) index (via
    enqueue()'s INSERT OR IGNORE, reached through
    check_due_time_driven()) is what actually makes repeated calls safe
    -- see weekly_recap_provider()'s own docstring for the pre-check
    that now does double duty as the dueness test itself.
    """
    tz = ZoneInfo(settings.checkin_net_timezone)
    local = datetime.fromtimestamp(now, tz=tz)
    today_midnight = datetime(local.year, local.month, local.day, tzinfo=tz)
    # datetime.weekday(): Monday=0 .. Sunday=6. Days back to the most
    # recent Sunday, INCLUSIVE of today (0 when today itself is Sunday):
    # Sunday(6) -> 0, Monday(0) -> 1, ..., Saturday(5) -> 6.
    days_since_sunday = (local.weekday() + 1) % 7
    end_local = today_midnight - timedelta(days=days_since_sunday)
    end_ts = int(end_local.timestamp())
    start_ts = end_ts - 7 * 86400
    iso_year, iso_week, _ = end_local.isocalendar()
    key = f"{iso_year}-W{iso_week:02d}"
    return key, start_ts, end_ts


def _ordinal(n: int) -> str:
    """1 -> "1st", 2 -> "2nd", 3 -> "3rd", 4 -> "4th", 11 -> "11th" --
    English ordinal suffix rules, where every "teens" value (10 through
    20, by the n % 100 test below) takes "th" regardless of its last
    digit -- 11th/12th/13th, not "11st"/"12nd"/"13rd" -- and every other
    value takes "st"/"nd"/"rd"/"th" off its last digit alone. Ranks in
    this module never exceed the number of teams (settings.teams_list is
    small), but the rule is written generally rather than hand-listing
    the few values that could ever actually appear.
    """
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _weekly_placement_section(conn, protocol: str, start_ts: int, end_ts: int,
                               emoji: dict[str, str]) -> str | None:
    """"Placement changes": each team's CURRENT rank (by squares held,
    descending, ties broken by team name ascending -- see _ranked()
    below), and how that rank moved since the window opened, one line
    per team that holds any ground right now, ordered by current rank
    ascending (1st first) -- the owner's own correction: the old version
    of this section showed squares GAINED, which is positive for nearly
    every team nearly every week and so never actually read as movement.
    A reader wants to know who is winning and who is climbing, and only
    a RANK answers that; the raw squares-held count sorts the very same
    order this section is already in and does not add "movement" the
    rank line does not already carry, so it is left out entirely: "the
    rank is the point."

    Line shape: "<dot> TEAM **<ordinal>** <movement>", e.g.
    "ORANGE **2nd** ▲ *from 5th*" / "GREEN **1st** *no change*" /
    "BLUE **6th** ▼ *from 4th*" -- arrows in place of the "up"/"down"
    words (the owner's own call: "use arrows instead of up and down
    words"), "from Nth" kept as the italic tail so which prior rank a
    team moved from/to is still stated, not just the direction.

    Reuses app/results.py's ownership_at() -- the exact function
    compute_month() itself calls to answer "who holds what right now"
    (see that function's own docstring) -- at the window's two
    endpoints, rather than writing a second standings query: squares
    held at start_ts - 1 (the instant BEFORE this window opened, i.e.
    last week's own close) versus at end_ts - 1 (this window's own
    close, the same "end is exclusive, the close is end - 1" convention
    compute_month() uses). Both are point-in-time snapshots every
    existing standings page already trusts; a team's rank is nothing
    more than its position in that snapshot, so there is no second
    scoring path here to ever fall out of sync with the first.

    A team absent from `after` (held nothing at the window's close --
    wiped out, or never held ground at all) has no CURRENT rank to
    report and is left out of this section entirely: unlike the old
    squares-delta version, which could at least show a negative number
    for a team that lost everything, "rank N (currently absent)" has no
    natural reading, and a wipe-out already shows up in every OTHER
    team's own rank moving up over it. A team absent from `before` (new
    to holding ground since the window opened) still gets a line -- its
    current rank, with "new to the board" in place of an up/down/no
    change verdict, since there is no PRIOR rank to compare against.

    Returns None when no team holds any ground at either end of the
    window (a brand-new or reset board) -- "no standings recorded" is
    not something a week's own Discord message needs to say.
    """
    from . import results

    def _by_team(rows) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["team"]] = counts.get(r["team"], 0) + 1
        return counts

    def _ranked(counts: dict[str, int]) -> list[str]:
        # Team names in rank order (index 0 = 1st) -- squares held
        # descending, then team name ascending as a stable, deterministic
        # tiebreak (the same tiebreak _join_team_field()'s own callers
        # already rely on elsewhere in this module for equal-value rows).
        return [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    before = _by_team(results.ownership_at(conn, protocol, start_ts - 1))
    after = _by_team(results.ownership_at(conn, protocol, end_ts - 1))
    if not before and not after:
        return None

    after_order = _ranked(after)
    before_rank = {t: i + 1 for i, t in enumerate(_ranked(before))}

    lines = []
    for i, team in enumerate(after_order):
        current_rank = i + 1
        prior_rank = before_rank.get(team)
        if prior_rank is None:
            # Not an up/down word -- nothing to turn into an arrow here,
            # since there is no PRIOR rank to have moved from. Left as-is
            # per the owner's own instruction.
            movement = "*new to the board*"
        elif prior_rank == current_rank:
            # No glyph expresses "unchanged" -- an up or down arrow would
            # actively claim movement that didn't happen -- so this stays
            # italic text rather than a symbol, same as "new to the
            # board".
            movement = "*no change*"
        elif current_rank < prior_rank:
            # Up: U+25B2, BLACK UP-POINTING TRIANGLE (a geometric symbol,
            # not emoji) in place of the word "up" -- the owner's own
            # call. Written as a \u escape, matching how _SEP above
            # encodes its own middle dot, rather than a literal
            # multi-byte character in the source. "from Nth" stays as
            # the italic tail so the prior rank is still legible, not
            # just the direction.
            movement = f"\u25B2 *from {_ordinal(prior_rank)}*"
        else:
            # Down: U+25BC, BLACK DOWN-POINTING TRIANGLE -- same
            # reasoning and same \u-escape convention as the up case
            # above.
            movement = f"\u25BC *from {_ordinal(prior_rank)}*"
        lines.append(f"{_team_dot(emoji, team)}{team} **{_ordinal(current_rank)}** {movement}")
    # No trailing unit line -- a rank/movement line is self-explanatory,
    # unlike a bare number that needs "squares held" stated once to mean
    # anything (see _join_team_field()'s own `unit` parameter).
    return _join_team_field(lines, None)


def _weekly_exploration_section(conn, protocol: str, season_id: int | None,
                                 start_ts: int, end_ts: int, emoji: dict[str, str]) -> str | None:
    """"Exploration": app/place_scoring.py's qualifying_place_firsts()
    own aggregate counts, one unattributed elevation figure, and a bare
    per-player COUNT of firsts -- see that function's own HARD PRIVACY
    WARNING for why a PLACE NAME never appears anywhere in this section,
    attributed or not: only ref_type/elevation_ft (aggregated, never
    tied to one player) and player_name/team (tied only to a COUNT) are
    ever read off of its rows.

    None when there is no active season for `protocol` covering this
    window (season_id is None -- see _season_id_for_ts()) or no
    qualifying activation at all inside it -- a quiet week's Exploration
    section is simply omitted, never rendered as "0 new."
    """
    if season_id is None:
        return None
    from . import place_scoring

    rows = place_scoring.qualifying_place_firsts(
        conn, protocol=protocol, season_id=season_id, start_ts=start_ts, end_ts=end_ts,
    )
    if not rows:
        return None

    n_summits = sum(1 for r in rows if r["ref_type"] == "summit")
    n_parks = sum(1 for r in rows if r["ref_type"] == "park")
    # Display word is "new", not "first" -- the owner's own call, purely
    # presentational (see the per-player line below for the fuller note).
    # "claimed" dropped too: it was padding the owner never asked for.
    lines = [
        f"**{_fmt_number(n_summits)}** new summit{'' if n_summits == 1 else 's'} "
        f"and **{_fmt_number(n_parks)}** new park{'' if n_parks == 1 else 's'}"
    ]

    # ONE unattributed elevation figure -- deliberately no player name
    # anywhere near it, even though every row it is drawn from has one.
    # See this function's own docstring and qualifying_place_firsts()'s
    # HARD PRIVACY WARNING.
    summit_elevations = [
        r["elevation_ft"] for r in rows
        if r["ref_type"] == "summit" and r["elevation_ft"] is not None
    ]
    if summit_elevations:
        lines.append(f"Highest new summit: **{_fmt_number(max(summit_elevations))}** *ft*")

    # Per-player COUNT of firsts -- never a place name -- capped to
    # _MAX_WEEKLY_RECAP_PLAYERS, sorted by count desc then name asc so a
    # tie has a stable, deterministic order.
    #
    # Rendered word is "new", not "first"/"firsts" -- the owner's own
    # call ("labelling firsts is awkward i dont like it. use new
    # instead"). This is PRESENTATION ONLY: the underlying rule is still
    # "that player's first activation of that place in this season" (see
    # qualifying_place_firsts()'s own docstring and HARD PRIVACY WARNING
    # in app/place_scoring.py), and that function's name -- along with
    # its variables and docstring -- is NOT renamed to match. Only the
    # rendered string changes.
    #
    # Incidental win: "new" has no separate plural, so the singular/
    # plural branch that used to pick between "first" and "firsts" has
    # nothing left to decide and is removed outright rather than kept
    # around computing a value nobody reads.
    counts: dict[int, int] = {}
    info: dict[int, tuple[str, str]] = {}
    for r in rows:
        pid = r["player_id"]
        counts[pid] = counts.get(pid, 0) + 1
        info[pid] = (r["player_name"], r["team"])
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], info[kv[0]][0]))
    for pid, count in ranked[:_MAX_WEEKLY_RECAP_PLAYERS]:
        name, team = info[pid]
        lines.append(
            f"{_team_dot(emoji, team)}{name}{_SEP}"
            f"**{_fmt_number(count)}** *new*"
        )

    value = "\n".join(lines)
    # Belt-and-suspenders hard cap -- the player cap above already keeps
    # this well short in every realistic week, but this must never hand
    # Discord an over-long field value regardless (see
    # _MAX_FIELD_VALUE_CHARS's own comment on why that would 400 the
    # whole message).
    return value if len(value) <= _MAX_FIELD_VALUE_CHARS else value[:_MAX_FIELD_VALUE_CHARS]


def build_weekly_recap_embed(conn, protocol: str, start_ts: int, end_ts: int) -> dict | None:
    """The full Discord webhook JSON body for one week's recap -- two
    sections (Placement changes, Exploration -- see
    weekly_recap_provider() for the window this covers and why), each
    its own field in a single embed, using every rendering rule already
    established elsewhere in this module: team dots from
    discord_config.team_emoji with the plain-text fallback (_team_dot()),
    bold numbers (_fmt_number()), italic units, _SEP between tokens,
    never a "--" separator, an absolute-or-omitted `url`, and Discord's
    own field-count/field-value/total-char budgets.

    A section with nothing to report for the window is OMITTED, not
    rendered empty (see each _weekly_*_section() helper's own docstring
    for what "nothing to report" means for it) -- and when both are
    empty, this returns None so weekly_recap_provider() enqueues
    nothing: an empty week gets NO message at all, never a Discord post
    that says so.

    Nets are deliberately NOT one of these sections any more: a weekly
    roll-up of check-in activity was rejected in favour of a per-net
    wrap-up posted the day after each net (net_wrapup_provider() below)
    -- see that provider's own docstring.
    """
    cfg = load_discord_config(conn)
    emoji = _parse_team_emoji(cfg["team_emoji"])
    season_id = _season_id_for_ts(conn, protocol, start_ts)

    fields = []
    placement = _weekly_placement_section(conn, protocol, start_ts, end_ts, emoji)
    if placement:
        fields.append({"name": "Placement changes", "value": placement, "inline": False})
    exploration = _weekly_exploration_section(conn, protocol, season_id, start_ts, end_ts, emoji)
    if exploration:
        fields.append({"name": "Exploration", "value": exploration, "inline": False})
    if not fields:
        return None
    fields = fields[:_MAX_EMBED_FIELDS]

    proto_label = _PROTOCOL_NAMES.get(protocol, protocol)
    tz = ZoneInfo(settings.checkin_net_timezone)
    start_date = datetime.fromtimestamp(start_ts, tz=tz).date().isoformat()
    end_date = datetime.fromtimestamp(end_ts - 1, tz=tz).date().isoformat()
    embed = {
        "title": f"{proto_label} — Weekly Recap ({start_date} to {end_date})",
        "fields": fields,
    }
    # Same absolute-or-omitted rule as every other embed's own `url` in
    # this module -- a relative path here would make Discord reject the
    # ENTIRE message with an HTTP 400, not just drop the link.
    base_url = (settings.oauth_public_base_url or "").rstrip("/")
    if base_url:
        embed["url"] = f"{base_url}/results"

    embeds = [embed]
    if _total_embed_chars(embeds) > _MAX_TOTAL_EMBED_CHARS:
        # Should not happen in practice -- each section's own value is
        # already capped well under _MAX_FIELD_VALUE_CHARS above -- but
        # never hand Discord a payload that would 400 the whole message.
        # Drop the least-critical section first (Exploration, keeping
        # Placement changes -- the standings movement -- as the one
        # thing this post must always be able to say), the same "drop a
        # whole piece rather than truncate it" philosophy
        # build_month_honors_embed() already applies to its own
        # "By team" embed.
        for name in ("Exploration",):
            fields = [f for f in fields if f["name"] != name]
            embed["fields"] = fields
            if _total_embed_chars(embeds) <= _MAX_TOTAL_EMBED_CHARS:
                break

    return {
        "username": cfg["username"] or "MeshWars",
        "embeds": embeds,
    }


def _active_recap_protocols(conn) -> list[str]:
    """Every protocol with a currently active mc_season row, in a fixed
    order (_PROTOCOL_NAMES's own key order: 'mc' then 'mt') so a test or
    a reader comparing two weeks' worth of keys sees a stable order
    rather than SQLite's unspecified one. Mirrors month honors' own
    per-protocol split -- app/results.py's freeze_month() is called once
    per protocol, enqueue()ing kind="month_honors" with a key of
    f"{month}:{protocol}" each time (see that function's own docstring)
    -- so the weekly recap follows the exact same "the two boards are
    separate games, always announced separately" rule every other part
    of this codebase already applies, rather than combining them into
    one post. A protocol with no active season (never started yet, or
    between seasons) gets no recap item: there is no season for a week's
    standing to be a week INSIDE of.

    A plain query, not app/mc_scoring.py's ensure_active_season() --
    this must never CREATE a season as a side effect of checking whether
    one exists, the way that function does for its own, very different,
    scoring-path callers.
    """
    return [
        p for p in _PROTOCOL_NAMES
        if conn.execute(
            "SELECT 1 FROM mc_season WHERE protocol = ? AND status = 'active' LIMIT 1", (p,),
        ).fetchone() is not None
    ]


def weekly_recap_provider(conn, now: int) -> list[dict]:
    """TIME_DRIVEN_PROVIDERS entry for the weekly recap -- see
    _weekly_recap_period() for exactly which week this covers (the most
    recently COMPLETED one, self-healing across an outage -- see that
    function's own docstring) and build_weekly_recap_embed() for the
    message itself.

    Emits ONE ITEM PER PROTOCOL with an active season (_active_recap_
    protocols() above) -- never a single combined post: a MeshCore-only
    board used to mean Meshtastic players never appeared here at all,
    which month honors already avoided by announcing each board
    separately, and this now follows the same rule. Each protocol's item
    carries its OWN key (f"{period_key}:{protocol}"), so the two boards'
    recaps dedupe entirely independently of each other -- MeshCore's
    post existing already (or being disabled, or empty) has no bearing
    on whether Meshtastic's goes out this week, and vice versa. `kind`
    stays the plain "weekly_recap" for both -- the same "kind carries no
    protocol, key does" shape month_honors' own kind never needed
    scoping either, since routing (resolve_discord_webhook()) has no
    reason to ever split the two boards onto different channels.

    This is the one provider in this list allowed to touch big, growing
    tables (mc_tile_capture_log via app/results.py's ownership_at(),
    place_activation via qualifying_place_firsts()) -- a deliberate,
    narrow exception to TIME_DRIVEN_PROVIDERS's own "never query a large
    table" rule (see that list's own docstring), safe here because this
    does NOT pay that cost on every ~30-second poll cycle:
    _weekly_recap_period() itself is pure date/timezone arithmetic with
    no DB access at all, so the heavy path below is only ever reached
    after it. And the discord_outbox lookup for THAT PROTOCOL'S key,
    right below, is the ACTUAL dueness test now, not merely an
    optimisation on top of one -- under the old Sunday-only trigger,
    "is it due" and "has it been posted" were two separate questions
    that happened to agree in the common case; under the self-healing
    model there is only one question ("is the most recent completed
    period posted yet"), and this lookup answers it directly. It still
    also does the SAME expense-avoiding job it always did: once a
    protocol's row has actually been enqueued, every later poll cycle --
    Sunday itself, or any outage-recovery day after it, for as long as
    this stays the most recent completed period -- skips straight past
    the heavy build below for that protocol. In the ordinary case the
    heavy computation runs at most ONCE per protocol per ISO week, not
    once per poll cycle. enqueue()'s own INSERT OR IGNORE on
    discord_outbox's UNIQUE(kind, key) remains the actual correctness
    guarantee against a double-post regardless -- this lookup is a
    dueness signal and a cost-avoidance shortcut, never the guard itself
    (see enqueue()'s own docstring).

    (A genuinely empty week for one protocol -- build_weekly_recap_embed()
    returning None for it -- never produces a row, so this early-exit
    cannot kick in for that protocol; every remaining poll cycle
    re-runs the heavy computation for it for as long as that protocol's
    week stays both empty and the most recently completed one,
    independently of whether the OTHER protocol's row has already been
    posted. Accepted: an empty week is not the normal state of an active
    season, and the alternative -- a second "we checked and it was
    empty" marker table -- is exactly the kind of second source of truth
    TIME_DRIVEN_PROVIDERS's own docstring already argues against.)
    """
    period_key, start_ts, end_ts = _weekly_recap_period(now)

    # Cheap pre-checks only -- enqueue() (via check_due_time_driven())
    # remains the real, authoritative gate for both `enabled` and
    # announce_weekly_recap; this is purely to avoid re-running the
    # heavy build below every cycle when the answer is already known.
    cfg = load_discord_config(conn)
    if not announcements_enabled(cfg) or not cfg.get("announce_weekly_recap"):
        return []

    items = []
    for protocol in _active_recap_protocols(conn):
        key = f"{period_key}:{protocol}"
        # THE dueness test, not just an expense-avoiding pre-check --
        # see this function's own docstring. A row already existing for
        # this (kind, key) -- posted or still pending -- means this
        # period is not due; nothing else here decides that question.
        already = conn.execute(
            "SELECT 1 FROM discord_outbox WHERE kind = 'weekly_recap' AND key = ?",
            (key,),
        ).fetchone()
        if already is not None:
            continue
        payload = build_weekly_recap_embed(conn, protocol, start_ts, end_ts)
        if payload is None:
            continue
        items.append({"kind": "weekly_recap", "key": key, "payload": payload})
    return items


# ---------------------------------------------------------------------
# Per-net wrap-up -- the checkin_net-derived TIME_DRIVEN_PROVIDERS
# worked example, now real. See that list's own docstring for the shape
# this implements.


def _due_net_wrapups(conn, now: int) -> list[dict]:
    """Every ENABLED checkin_net row's MOST RECENTLY COMPLETED occurrence
    -- each as {"net": <row>, "net_date": "YYYY-MM-DD"} -- "due" meaning
    08:00 or LATER has already passed, on the calendar day immediately
    after that occurrence's weekday, in THAT ROW'S OWN timezone
    (zoneinfo.ZoneInfo(net["timezone"])) -- never settings.
    checkin_net_timezone or any other single app-wide clock, because two
    nets in different zones (or on different weekdays) complete their
    own "day after at 08:00" trigger on different local days at the very
    same instant `now`.

    SELF-HEALING, like _weekly_recap_period() above (see that function's
    own docstring, including the maybe_roll_months() precedent it cites
    for this same shift): the old version of this function asked "is it
    the trigger moment right now" (local weekday == day-after AND hour
    >= 8) and returned nothing at all outside that narrow window -- so a
    service down for the entire day-after, or simply still down once
    that day had fully passed, meant a whole occurrence's wrap-up was
    lost, permanently, the moment the calendar turned over again. This
    version instead always computes the SINGLE most recently completed
    occurrence for each net -- true on every day of the week, not just
    the one the trigger happens to land on -- so net_wrapup_provider()
    below can ask this on a Saturday, two days after a Wednesday net's
    Thursday-08:00 trigger, and still get that occurrence back, due for
    as long as it has not yet been posted (checked there, not here --
    see that function's own docstring).

    CRITICAL: this returns only the ONE most recent completed occurrence
    per net, never a backlog of every unposted occurrence stretching
    back through a long outage -- there is no loop here walking
    backwards over past weeks for a net. See _weekly_recap_period()'s
    own docstring for why: turning this feature on, or recovering from a
    multi-week outage, must never dump a run of old wrap-ups into the
    channel. If an occurrence was never posted and a newer one has since
    completed, the old one is gone from this function's output for good;
    it does not become two due items on the next call.

    Nothing about a net's count, weekday, hours, or timezone is read
    from anywhere but this SELECT: an operator adding a net through
    admin starts getting wrap-ups on its very next occurrence with no
    code change, and disabling one (the `WHERE enabled = 1` below) stops
    them immediately -- see checkin_net's own comment in app/db.py and
    TIME_DRIVEN_PROVIDERS's own docstring for why this must stay true.

    `net_date` is computed as a CALENDAR date (the occurrence's own date
    minus one day), not a fixed second offset -- date arithmetic, not
    `now` minus 86400 seconds, so a day that crosses a daylight-saving
    change still lands on the correct previous calendar date. This is
    the exact date app/checkin.py's net_date_for_net() would itself have
    stamped onto that night's mc_checkin_award rows (net['weekday']
    matching, hour inside [start_hour, end_hour]), so
    build_net_wrapup_embed() below can look check-ins up by that same
    net_date directly rather than recomputing a timestamp window.
    """
    nets = conn.execute(
        "SELECT id, label, protocol, weekday, start_hour, end_hour, timezone "
        "  FROM checkin_net WHERE enabled = 1"
    ).fetchall()
    due = []
    for net in nets:
        local_now = datetime.fromtimestamp(now, tz=ZoneInfo(net["timezone"]))
        trigger_weekday = (net["weekday"] + 1) % 7
        # Days back, from today, to the most recent date matching the
        # trigger weekday -- 0 when today itself is that weekday.
        days_back = (local_now.weekday() - trigger_weekday) % 7
        if days_back == 0 and local_now.hour < 8:
            # Today IS the trigger day, but 08:00 hasn't happened yet --
            # the most recently COMPLETED trigger is therefore last
            # week's, not today's still-pending one.
            days_back = 7
        trigger_date = local_now.date() - timedelta(days=days_back)
        net_date = (trigger_date - timedelta(days=1)).isoformat()
        due.append({"net": net, "net_date": net_date})
    return due


# Cap on how many named players build_net_wrapup_embed() lists out of a
# single night's check-ins -- this is a PRESENTATION decision, not a
# safety net: a real wrap-up once rendered all 12 (and, on a busier
# night, would render 25+) check-ins with no cap at all, and
# _join_team_field()'s own _MAX_FIELD_VALUE_CHARS trim (still applied
# underneath, see below) is a last-resort guard against Discord's hard
# 1024-character field limit that ends on a bare "(truncated)" marker
# with no count of what was cut -- a reader has no way to tell how many
# names are missing. This cap fires far earlier, on a much smaller
# number, specifically so the "and N more" line below can always say
# exactly how many were left out.
_MAX_NET_WRAPUP_NAMED_PLAYERS = 12


def build_net_wrapup_embed(conn, net, net_date: str) -> dict | None:
    """The full Discord webhook JSON body for one net's wrap-up -- the
    net's own label and date, how many players checked in, who they
    were (with their team dot), and any notable streak, all pulled from
    mc_checkin_award rows for THIS net's THIS occurrence: `net_id = ?`
    and `net_date = ?`, the exact (net_id, net_date) pair app/checkin.py
    already stamps onto a check-in at award time (see
    net_date_for_net()) -- so this is a plain, already-indexed lookup
    (idx_mc_checkin_award_net), never a timestamp-range scan.

    Named players and counts only -- a check-in event names no
    location, so unlike the weekly recap's own Exploration section this
    carries none of qualifying_place_firsts()'s privacy concern (see
    this module's own HARD PRIVACY RULE at the top of the file): pairing
    a named player with a named PLACE is what is forbidden, and nothing
    here ever reads a place.

    A "notable" streak is 2 or more consecutive checked-in nets -- a
    single check-in has a streak of 1 by definition (app/checkin.py's
    checkin_streak()) and is not itself news. Streaks are READ BACK from
    mc_checkin_award.streak, already computed and stored at award time --
    never recomputed here.

    The named-player list is CAPPED at _MAX_NET_WRAPUP_NAMED_PLAYERS,
    sorted first so the players worth naming survive the cut: those with
    a notable (>=2) streak first, by streak descending, then everyone
    else, stable by name -- a busy night's chronological roster (the
    order these rows actually arrive in, ORDER BY awarded_at) is not
    itself a meaningful order to cut at, but "who's on a streak" is
    exactly the kind of thing a reader wants to see even when the full
    list doesn't fit. When the true count exceeds the cap, one italic
    "*and N more*" line is appended -- N is the exact remainder, so a
    capped list never leaves the reader guessing how many names were
    left out (unlike _join_team_field()'s own bare "(truncated)"
    marker). The headline "**<N>** checked in" count above the list is
    always the TRUE total from `rows`, never the capped count -- the cap
    only shortens which names are SHOWN, it must never make the night
    look smaller than it was.

    Reuses _join_team_field() for the line list -- the exact same
    per-field 1024-character trim (dropping the LAST lines first, a
    truncation marker in their place) build_month_honors_embed()'s "By
    team" fields and the weekly recap's own sections already rely on.
    With the cap above in place this is now the BACKSTOP it was always
    meant to be (see _MAX_NET_WRAPUP_NAMED_PLAYERS's own comment) rather
    than the only guard: at most 13 lines (12 names plus the count line,
    plus one more for "and N more") reach it, well under
    _MAX_FIELD_VALUE_CHARS in every realistic case, but it stays in
    place regardless.

    Returns None when nobody checked in for this net on this date -- a
    quiet night posts NOTHING, never an empty "0 checked in" message
    (net_wrapup_provider() below relies on this to decide whether there
    is anything to enqueue at all).
    """
    cfg = load_discord_config(conn)
    emoji = _parse_team_emoji(cfg["team_emoji"])

    rows = conn.execute(
        "SELECT a.player_id, a.streak, p.display_name AS player_name, p.team AS team "
        "  FROM mc_checkin_award a "
        "  JOIN player p ON p.player_id = a.player_id "
        " WHERE a.net_id = ? AND a.net_date = ? "
        " ORDER BY a.awarded_at",
        (net["id"], net_date),
    ).fetchall()
    if not rows:
        return None

    # Notable-streak rows first (streak descending), then everyone else,
    # stable by name -- see this function's own docstring for why. A
    # plain `-streak` DESC key on the WHOLE list would put a streak of 1
    # ahead of a streak of 0 for no reason a reader would recognize as
    # "notable"; splitting on the same >=2 threshold the streak-suffix
    # rendering below already uses keeps the two in agreement.
    def _sort_key(r):
        streak = r["streak"] or 0
        if streak >= 2:
            return (0, -streak, r["player_name"])
        return (1, 0, r["player_name"])
    ranked_rows = sorted(rows, key=_sort_key)

    # The count line is the field's own first line, not the field NAME --
    # a field name renders as a plain header on Discord's side (see every
    # other embed in this module: "Placement changes", "Exploration",
    # "By team", ...), never markdown-formatted text, so the bold count
    # belongs in the value like every other bold number this module ever
    # renders. `len(rows)`, the TRUE total -- never `len(shown)` -- see
    # this function's own docstring on why the headline must never
    # shrink to match a capped list.
    lines = [f"**{_fmt_number(len(rows))}** checked in"]
    shown = ranked_rows[:_MAX_NET_WRAPUP_NAMED_PLAYERS]
    for r in shown:
        streak = r["streak"] or 0
        line = f"{_team_dot(emoji, r['team'])}{r['player_name']}"
        if streak >= 2:
            line += f"{_SEP}**{_fmt_number(streak)}**-net streak"
        lines.append(line)
    remaining = len(ranked_rows) - len(shown)
    if remaining > 0:
        # Unconditional whenever the roster overflows the cap -- never
        # leave the reader unable to tell the list was cut. Italic,
        # matching _TRUNCATION_MARKER's own "reporting a truncation, not
        # a piece of the night's data" treatment, but naming the exact
        # count where that marker cannot.
        lines.append(f"*and {remaining} more*")
    checkins_value = _join_team_field(lines, None)

    embed = {
        "title": f"{net['label']} — {net_date}",
        "fields": [{"name": "Check-ins", "value": checkins_value, "inline": False}][:_MAX_EMBED_FIELDS],
    }
    # Same absolute-or-omitted rule as every other embed's own `url` in
    # this module -- a relative path here would make Discord reject the
    # ENTIRE message with an HTTP 400, not just drop the link.
    base_url = (settings.oauth_public_base_url or "").rstrip("/")
    if base_url:
        embed["url"] = f"{base_url}/results"

    return {
        "username": cfg["username"] or "MeshWars",
        "embeds": [embed],
    }


def net_wrapup_provider(conn, now: int) -> list[dict]:
    """TIME_DRIVEN_PROVIDERS entry for the per-net wrap-up -- see
    _due_net_wrapups() for exactly which occurrence is due (the most
    recently COMPLETED one, self-healing across an outage) and
    build_net_wrapup_embed() for the message itself.

    Unlike weekly_recap_provider() above, the discord_outbox check below
    is not needed to avoid a genuinely EXPENSIVE rebuild -- checkin_net
    has a handful of rows (TIME_DRIVEN_PROVIDERS's own "CHEAPNESS"
    comment already requires this to stay small), and
    build_net_wrapup_embed() only ever runs one indexed (net_id,
    net_date) lookup against mc_checkin_award per due net, never a scan
    of a large or growing table. It is needed for a different reason
    now: under the self-healing model, _due_net_wrapups() returns the
    SAME occurrence on every poll cycle for as long as it remains the
    net's most recently completed one -- which, if it stays unposted, is
    potentially forever, not just for one calendar day the way the old
    trigger-moment version bounded it. "Due until posted" means this
    function must itself know whether it has been posted, the same
    dueness role the check plays in weekly_recap_provider(), so it gets
    the same treatment here.

    kind is colon-scoped per net (f"net_wrapup:{net id}") -- resolved by
    resolve_discord_webhook() via _channel_kind_candidates() against a
    per-net route first, falling back to the generic "net_wrapup"
    channel, then discord_config's own default, with zero special-casing
    here (see that function's own docstring). key is
    f"{net id}:{net_date}" -- per-net, per-occurrence DEDUPE, entirely
    independent of every other net's own key, so two nets due on the
    same poll cycle (or the same net, called twice for the one
    occurrence) each get exactly one outbox row. enqueue()'s own INSERT
    OR IGNORE on discord_outbox's UNIQUE(kind, key) remains the actual
    correctness guarantee against a double-post regardless of this
    lookup -- see weekly_recap_provider()'s own docstring for why this
    check is a dueness signal and a cost-avoidance shortcut, never the
    guard itself.
    """
    items = []
    for due in _due_net_wrapups(conn, now):
        net, net_date = due["net"], due["net_date"]
        kind = f"net_wrapup:{net['id']}"
        key = f"{net['id']}:{net_date}"
        already = conn.execute(
            "SELECT 1 FROM discord_outbox WHERE kind = ? AND key = ?", (kind, key),
        ).fetchone()
        if already is not None:
            continue
        payload = build_net_wrapup_embed(conn, net, net_date)
        if payload is None:
            continue
        items.append({"kind": kind, "key": key, "payload": payload})
    return items


TIME_DRIVEN_PROVIDERS: list = [weekly_recap_provider, net_wrapup_provider]


def check_due_time_driven(conn, now: int) -> int:
    """Call every provider in TIME_DRIVEN_PROVIDERS once, and enqueue()
    whatever items each one says is due right now. Returns how many
    items actually turned into NEW outbox rows (enqueue()'s own bool
    return, summed) -- 0 for an empty registry, and 0 again for a second
    call inside the same period once every item's (kind, key) is already
    in discord_outbox, since dueness here is decided ENTIRELY by that
    table's own UNIQUE(kind, key) index (via enqueue()'s INSERT OR
    IGNORE) -- see TIME_DRIVEN_PROVIDERS's own docstring for why there is
    no separate "last run" column to get out of sync with it. This makes
    calling this function twice in the same period always exactly as
    safe as calling it once, by construction, not by a check written
    here.

    SYNC, and takes the CALLER's own connection -- same shape as
    enqueue() itself, since this is nothing but a loop that calls it.
    Each provider is called as provider(conn, now), passed straight
    through: a provider that needs its own DB access (any real one
    will) uses THIS connection, inside THIS already-open transaction,
    rather than opening a second one of its own -- see
    TIME_DRIVEN_PROVIDERS's own docstring for why. Never raises: a
    single misbehaving provider is logged and skipped, never allowed to
    stop a later provider in the same list, or a later call to this
    function on the next cycle.
    """
    inserted = 0
    for provider in TIME_DRIVEN_PROVIDERS:
        try:
            items = provider(conn, now)
        except Exception:
            log.exception("discord: time-driven provider failed, skipping it this cycle")
            continue
        for item in items:
            if enqueue(conn, kind=item["kind"], key=item["key"], payload=item["payload"], now=now):
                inserted += 1
    return inserted


async def _check_due_time_driven_once() -> None:
    """WriteSession wrapper around check_due_time_driven() for
    run_forever()'s own use. Unlike _drain_once() (which reads and posts
    outside the write lock because posting is a slow network call), a
    due-check is pure DB work end to end -- the cheap reads
    TIME_DRIVEN_PROVIDERS's own docstring requires, plus a handful of
    enqueue()'s INSERT OR IGNOREs -- so holding the write lock for the
    whole check is fine and simpler than juggling two connections.
    """
    now = int(time.time())
    async with WriteSession() as conn:
        check_due_time_driven(conn, now)


async def run_forever() -> None:
    """Background poll loop over discord_outbox -- started
    UNCONDITIONALLY by app/main.py's lifespan, the same "the loop must
    stay alive to notice a later config change" reasoning
    app/freqmapper_ingest.py's own run_forever() docstring gives for
    `enabled`: discord_config is a runtime, DB-backed setting (an
    operator can flip it live through /api/admin/discord, no restart),
    so a fresh install with no webhook configured yet still starts this
    task, but it simply does nothing each cycle until one is set.

    Runs the time-driven due-check (_check_due_time_driven_once())
    BEFORE each cycle's _drain_once() -- see TIME_DRIVEN_PROVIDERS's own
    docstring for why this loop, rather than a second scheduler, is what
    this app uses for a kind with no triggering event -- so anything a
    provider enqueues this cycle is picked up by the SAME cycle's drain
    pass rather than waiting a full poll interval.

    Also calls app/discord_bot.py's maybe_reconcile_roles() once per
    cycle -- a THIRD, unrelated Discord feature (role sync, never a
    webhook post) riding this same already-alive loop rather than
    starting a second asyncio.create_task of its own, per Matt's own
    call: one background loop, its own interval gate. maybe_reconcile_
    roles() itself no-ops on every cycle except the one every
    _RECONCILE_INTERVAL_SECONDS (15 minutes) that is actually due, so
    this adds no real per-cycle cost. Imported locally, not at module
    level: app/discord_bot.py imports FROM this module (load_discord_
    config, _TEAM_COLORS), so importing it back here at module load
    time would be a circular import -- same reasoning
    build_month_honors_embed()'s own local `from . import results`
    already gives.

    Never raises out of the loop -- same fire-and-forget contract
    app/account_api.py's _notify_security() applies to a single mail
    send, extended here to a whole poll cycle: a bug handling one
    cycle's rows (or one cycle's due-check, or one cycle's role
    reconcile) must never crash the process or stop later cycles (and
    later months' announcements) from ever running again. Each of the
    three is wrapped separately so one's failure never skips the other
    two in the same cycle.
    """
    log.info("discord outbox loop starting (announcements gated by discord_config)")
    while True:
        try:
            await _check_due_time_driven_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("discord: time-driven due-check cycle failed")
        try:
            await _drain_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("discord outbox: drain cycle failed")
        try:
            from . import discord_bot
            await discord_bot.maybe_reconcile_roles()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("discord roles: reconcile cycle failed")
        await asyncio.sleep(max(settings.discord_outbox_poll_interval_seconds, 1))
