"""Discord slash commands via HTTP Interactions -- no gateway connection.

Every other Discord integration in this codebase either posts outward
(app/discord_notify.py's webhook announcements) or authenticates as a
bot to call Discord's own REST API (app/discord_bot.py's role/channel
sync, "Herald"). This module is the one INBOUND surface: Discord POSTs
each slash-command invocation straight to POST /api/discord/interactions
and this app answers in the HTTP response itself -- there is no
persistent gateway websocket to keep alive, no bot process, nothing
running except this one route.

SECURITY comes first, in a fixed order, before the request body is ever
touched as JSON -- Discord's own documented contract for this endpoint,
and it deliberately sends REQUESTS WITH INVALID SIGNATURES when an
operator saves the endpoint URL in the developer portal, refusing to
save it unless every one of those is correctly rejected with 401. See
_verify_signature() and the route itself for the exact order.

GATING mirrors every other optional feature in this codebase ("empty
means off, never open" -- app/oauth.py's provider_enabled(),
app/discord_notify.py's announcements_enabled()): with
discord_config.slash_enabled off, or no public_key configured, this
route 404s -- indistinguishable from not existing at all, so an
operator can develop and deploy this endpoint privately before ever
turning it on in the developer portal.

THE 3-SECOND RULE: Discord fails a command outright if this app has not
answered within 3 seconds of the original POST. Every handler below
therefore runs under a fixed, shorter budget (_HANDLER_BUDGET_SECONDS)
via asyncio.wait_for -- if it finishes in time, the answer goes back
directly (type 4, "responded with a message"). If it does not, this
answers immediately with type 5 ("deferred, more to come") and finishes
the same handler in a background asyncio.Task, delivering the real
answer afterward with a PATCH to Discord's webhook-message-edit
endpoint. That PATCH authenticates with the INTERACTION'S OWN token
(embedded in the URL, exactly like a discord_notify.py webhook URL
carries its own auth) -- never app/discord_bot.py's bot token, which
has nothing to do with answering one specific interaction.

ONE REGISTRY: COMMANDS below is the single source of truth for
every command's Discord definition (name, description, options) AND its
handler function, so app/discord_bot.py's register_commands() (what
Discord is told exists) and _dispatch() below (what actually runs) can
never quietly disagree about what commands this bot has.

PRIVACY: every command here answers on a surface nobody has signed into
-- the same "identity can be public, location can be public, the link
between them requires a session" rule app/public_api.py's own module
docstring states, and the same rule app/discord_bot.py's module
docstring already applies to a Discord team role. /me and /player both
show a player's team, rank, and points -- all public, all things the
website itself already shows to any visitor -- and NEVER a location, a
cell, a place, a radio, or any node identifier. /me is additionally
ALWAYS ephemeral (ephemeral is Discord's own "only you can see this"
message flag), since it resolves a specific Discord user's own identity
even though nothing it shows is otherwise secret.

SCORING: every figure below reads through the exact same helpers the
site's own routes already use (app/mc_api.py's active_season()/
team_list(), app/mc_scoring.py's team_totals(), app/results.py's
month_results_for(), app/discord_notify.py's build_month_honors_embed()
and its rendering helpers) -- this module writes no new scoring SQL of
its own. The one deliberate exception is /player and /me's own player
lookup: app/mc_api.py's find_for() already exists and is exactly this
shape, but it also computes and returns a bounding box and a last-seen
timestamp -- exactly the location data this module's own privacy rule
above forbids ever putting in an unauthenticated Discord message. Rather
than call find_for() and discard fields a bug could someday forget to
strip, this module runs its own minimal, obviously location-free query
against `player` (display_name, team only) and gets every SCORING
figure (rank, points) from the same team_totals()/team_list()/
active_season() trio /standings already uses. See _team_standings()
below.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Callable, NamedTuple
from zoneinfo import ZoneInfo

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from . import discord_notify, mc_api, results
from .db import connect
from .mc_ingest import PROTOCOL as MC_PROTOCOL
from .mc_scoring import team_totals

log = logging.getLogger("discord_interactions")

router = APIRouter()

MT_PROTOCOL = "mt"
_BOARD_CHOICES = {"mc": MC_PROTOCOL, "mt": MT_PROTOCOL}
_BOARD_LABELS = {MC_PROTOCOL: "MeshCore", MT_PROTOCOL: "Meshtastic"}

# Discord's own documented replay-protection window for this endpoint:
# reject a request whose X-Signature-Timestamp is more than this many
# seconds from "now" in either direction, even if its signature is
# otherwise perfectly valid. This is a SEPARATE check from signature
# verification itself -- a captured, previously-valid request replayed
# later must still be rejected, which a signature check alone (which
# only proves Discord produced these exact bytes at SOME point) cannot
# catch on its own.
_MAX_TIMESTAMP_SKEW_SECONDS = 300

# Discord's own hard limit: this app must answer within 3 seconds of the
# original POST or the command fails outright on Discord's side. This
# budget is deliberately well under that -- it has to leave room for the
# network round trip itself (POST in, response out) on top of whatever
# this app spends computing the answer, and there is no way to know how
# much of the 3 seconds Discord's own delivery has already used by the
# time this process sees the request.
_HANDLER_BUDGET_SECONDS = 2.0

# Discord's own documented interaction types this route ever receives.
_TYPE_PING = 1
_TYPE_APPLICATION_COMMAND = 2

# Discord's own documented response types this route ever sends.
_RESPONSE_PONG = 1
_RESPONSE_CHANNEL_MESSAGE = 4
_RESPONSE_DEFERRED_CHANNEL_MESSAGE = 5

# Discord's own documented message flag for "only the invoking user can
# see this" -- used by every /me response (always) and by an error reply
# for any other command (never worth broadcasting a "no such player" to
# the whole channel).
_FLAG_EPHEMERAL = 1 << 6

# Every response this module ever sends carries this -- the bot must
# never be able to ping anyone, on any command, ever. See this module's
# own docstring.
_ALLOWED_MENTIONS = {"parse": []}

# Discord's own STRING option type (application command option types,
# discord.dev) -- named here so COMMANDS below reads as what it
# means rather than a bare "3" repeated five times.
_OPTION_TYPE_STRING = 3

_BOARD_OPTION = {
    "type": _OPTION_TYPE_STRING,
    "name": "board",
    "description": "Which board -- MeshCore or Meshtastic (default MeshCore)",
    "required": False,
    "choices": [
        {"name": "MeshCore", "value": "mc"},
        {"name": "Meshtastic", "value": "mt"},
    ],
}

# Fire-and-forget background tasks (one per deferred command -- see
# _dispatch() below) are kept here so nothing garbage-collects them
# mid-flight; asyncio only holds a weak reference to a task otherwise.
# Each task removes itself on completion via add_done_callback. Tests
# await every task still in this set to know a deferred command has
# actually finished (see tests/test_discord_interactions.py) rather than
# guessing at real-time sleeps.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _options_map(body: dict) -> dict:
    """{option name: value} for this interaction's own command options
    -- empty for a command with none given (every option in
    COMMANDS below is optional except /player's `name`, so a
    caller who omitted one simply gets it absent from this map, never a
    None entry to distinguish from "not given at all").
    """
    data = body.get("data") or {}
    return {o["name"]: o.get("value") for o in (data.get("options") or [])}


def _board_from_options(body: dict) -> str:
    """The 'board' option, resolved to the internal 'mc'/'mt' literal --
    every command that takes one defaults to MeshCore, same default
    every board-scoped route on the site itself uses (app/public_api.py,
    app/api.py's Meshtastic routes are the ones that need an explicit
    board instead).
    """
    raw = _options_map(body).get("board")
    return _BOARD_CHOICES.get(raw, MC_PROTOCOL)


def _response(response_type: int, data: dict | None = None) -> dict:
    return {"type": response_type, "data": data} if data is not None else {"type": response_type}


def _data(*, content: str | None = None, embeds: list[dict] | None = None, ephemeral: bool = False) -> dict:
    out: dict = {"allowed_mentions": _ALLOWED_MENTIONS}
    if content is not None:
        out["content"] = content
    if embeds is not None:
        out["embeds"] = embeds
    if ephemeral:
        out["flags"] = _FLAG_EPHEMERAL
    return out


def _ephemeral(text: str) -> dict:
    return _data(content=text, ephemeral=True)


def _connect_discord_message() -> dict:
    """The one reply /me gives a caller who has not linked Discord to a
    MeshWars account at all, or has no player yet -- both cases read
    identically to the caller (see COMMANDS's own /me entry):
    there is nothing here to show them. base_url mirrors
    build_month_honors_embed()'s own "absolute or omitted" rule for a
    Discord `url` field, but this is plain message content, not an
    embed link, so a missing OAUTH_PUBLIC_BASE_URL degrades to a relative
    path instead of omitting the mention entirely -- a person reading
    this in Discord still needs to be told where to go.
    """
    from .config import settings

    base = (settings.oauth_public_base_url or "").rstrip("/")
    return _ephemeral(f"Connect your Discord account at {base}/account to use this command.")


# ---- team standings (shared by /standings, /me, /player) ----------------


def _team_standings(conn, protocol: str) -> list[dict]:
    """Every team's rank and combined score for `protocol`'s ACTIVE
    season -- {"team", "total", "rank"} dicts, highest score first, ties
    broken by team name (same tie-break app/public_api.py's own
    _standings() uses). Empty list when there is no active season.

    Reads app/mc_scoring.py's team_totals() -- squares held PLUS
    check-in points PLUS Places Worth Going points -- rather than
    re-deriving a total from team_tile_counts()/team_checkin_points()
    separately the way app/public_api.py's own _standings() does: that
    helper's own docstring says it is "THE number for how a team is
    doing," the one every place that decides a season standing or
    winner already reads, so a Discord command showing "current rank
    and points" should mean the exact same thing app/mc_scoring.py's
    maybe_roll_season() means by it, not a second, narrower definition.
    app/mc_api.py's team_list() is the same team roster every other
    standings view on the site enumerates from, so a team with a total
    of zero (no tiles, no check-ins, no exploration yet) still gets a
    ranked row instead of silently not appearing.
    """
    season = mc_api.active_season(conn, protocol)
    if not season:
        return []
    totals = team_totals(conn, season["id"])
    rows = [{"team": t, "total": totals.get(t, 0.0)} for t in mc_api.team_list()]
    rows.sort(key=lambda r: (-r["total"], r["team"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def _team_line(cfg: dict, row: dict) -> str:
    """One ranked standings line: "**#<rank>** <dot>TEAM **<total>**" --
    same team-dot/bold-number building blocks
    build_month_honors_embed()'s own standings_text uses, so a team name
    reads identically here and in the monthly announcement.
    """
    emoji = discord_notify._parse_team_emoji(cfg["team_emoji"])
    dot = discord_notify._team_dot(emoji, row["team"])
    return f"**#{row['rank']}** {dot}{row['team']} **{discord_notify._fmt_number(row['total'])}**"


# ---- /me ------------------------------------------------------------------


def _cmd_me(conn, body: dict) -> dict:
    """Resolve the invoking Discord user's own MeshWars identity --
    ALWAYS ephemeral (see COMMANDS), and never anything about
    any OTHER player: this command only ever reads the row that
    account_identity(provider='discord', subject=<this caller's own
    snowflake>) itself points at.

    `interaction.member.user.id` is how Discord identifies the caller
    inside a GUILD (a "member" is a guild-scoped wrapper around a
    user); `interaction.user.id` is the same field for a DM/group
    context, which this bot supports too if it is ever installed that
    way. Falls back to the second only when the first is absent, never
    the reverse -- see discord.dev's own Interaction Object.
    """
    member = body.get("member") or {}
    user = member.get("user") or body.get("user") or {}
    snowflake = user.get("id")
    if not snowflake:
        return _ephemeral("Could not identify your Discord account.")

    identity = conn.execute(
        "SELECT account_id FROM account_identity WHERE provider = 'discord' AND subject = ?",
        (str(snowflake),),
    ).fetchone()
    if identity is None:
        return _connect_discord_message()

    player = conn.execute(
        "SELECT player_id, display_name, team FROM player "
        " WHERE account_id = ? AND disabled_at IS NULL",
        (identity["account_id"],),
    ).fetchone()
    if player is None:
        return _connect_discord_message()

    pid = player["player_id"]
    cfg = discord_notify.load_discord_config(conn)
    emoji = discord_notify._parse_team_emoji(cfg["team_emoji"])
    dot = discord_notify._team_dot(emoji, player["team"])

    boards = [
        r["protocol"] for r in conn.execute(
            "SELECT DISTINCT protocol FROM player_node WHERE player_id = ? ORDER BY protocol", (pid,)
        ).fetchall()
    ]
    radios = conn.execute(
        "SELECT count(*) FROM player_node WHERE player_id = ?", (pid,)
    ).fetchone()[0]

    lines = [f"{dot}**{player['display_name']}**"]
    if boards:
        lines.append("Board" + ("s" if len(boards) > 1 else "") + ": "
                      + ", ".join(_BOARD_LABELS.get(b, b) for b in boards))
    for protocol in boards:
        rows = _team_standings(conn, protocol)
        row = next((r for r in rows if r["team"] == player["team"]), None)
        if row:
            lines.append(f"{_BOARD_LABELS.get(protocol, protocol)}: {_team_line(cfg, row)}")
    lines.append(f"*{radios}* radio{'s' if radios != 1 else ''} registered")

    # Current net streak: the streak carried on this player's MOST
    # RECENT check-in award, across any board -- same persisted-column
    # read app/mc_api.py's top_checkin_for() and
    # app/public_api.py's _player_rows() already use (a fresh
    # checkin_streak() recompute is unnecessary here; the award row
    # already carries the number it was credited at). "if any" -- a
    # player who has never checked in shows no streak line at all,
    # never a fabricated 0.
    streak_row = conn.execute(
        "SELECT streak FROM mc_checkin_award WHERE player_id = ? "
        " ORDER BY net_date DESC LIMIT 1",
        (pid,),
    ).fetchone()
    if streak_row and streak_row["streak"]:
        lines.append(f"Current net streak: **{streak_row['streak']}**")

    embed = {"title": "Your MeshWars profile", "description": "\n".join(lines)}
    color = discord_notify._team_color(player["team"])
    if color is not None:
        embed["color"] = color
    return _data(embeds=[embed], ephemeral=True)


# ---- /standings -------------------------------------------------------


def _cmd_standings(conn, body: dict) -> dict:
    protocol = _board_from_options(body)
    cfg = discord_notify.load_discord_config(conn)
    rows = _team_standings(conn, protocol)
    label = _BOARD_LABELS[protocol]
    if not rows:
        desc = "No standings recorded."
    else:
        desc = "\n".join(_team_line(cfg, r) for r in rows)
    embed = {
        "title": f"{label} standings",
        "description": desc,
        "footer": {"text": discord_notify._SEASON_TOTAL_UNIT},
    }
    color = discord_notify._team_color(rows[0]["team"]) if rows else None
    if color is not None:
        embed["color"] = color
    return _data(embeds=[embed])


# ---- /honors ------------------------------------------------------------


def _valid_month(month: str) -> bool:
    """"YYYY-MM" shape only -- same plain check
    app/admin_ops.py's POST /api/admin/month/freeze already uses for the
    same string, rather than importing app/results.py's own private
    _MONTH_RE.
    """
    return len(month) == 7 and month[4] == "-" and month[:4].isdigit() and month[5:7].isdigit()


def _cmd_honors(conn, body: dict) -> dict:
    """Reuses app/discord_notify.py's build_month_honors_embed() verbatim
    -- the exact same function the monthly announcement itself calls --
    so this command's embeds and that announcement are never able to
    drift into looking like two different features for the same month.
    """
    protocol = _board_from_options(body)
    opts = _options_map(body)
    month = (opts.get("month") or "").strip()
    label = _BOARD_LABELS[protocol]

    if month and not _valid_month(month):
        return _ephemeral(f"'{month}' doesn't look like a month -- use YYYY-MM, e.g. 2026-08.")

    # A large limit, not results.month_results_for()'s own default of
    # 12: an explicit month request must be able to reach further back
    # than "the last year," and this is a read against already-frozen,
    # already-indexed rows -- cheap regardless of how many months back
    # it goes.
    now = int(time.time())
    data = results.month_results_for(conn, protocol, now, limit=9999)
    # The open month is never included by month_results_for() itself,
    # but a caller with results_preview_current_month on (see that
    # function's own docstring) gets it prepended and marked "preview" --
    # excluded here too, since an in-progress month is not a FROZEN one.
    frozen = [m for m in data["months"] if not m.get("preview")]

    if month:
        match = next((m for m in frozen if m["month"] == month), None)
        if match is None:
            return _ephemeral(f"No finished {label} results for {month}.")
    else:
        if not frozen:
            return _ephemeral(f"No {label} months have finished yet.")
        match = frozen[0]

    payload = discord_notify.build_month_honors_embed(conn, match["month"], protocol, match)
    return _data(embeds=payload["embeds"])


# ---- /nextnet -----------------------------------------------------------


def _next_net_start(net, now_ts: int) -> int:
    """The next unix timestamp `net` (a checkin_net row) opens at, local
    to net['timezone'] -- the SAME per-net "days ahead, roll a week if
    today's slot already passed" arithmetic app/public_api.py's
    _net_window() already uses to find the soonest of several nets (see
    that function's own comment); this is a date computation, not a
    scoring one, so duplicating five lines of it here rather than
    importing that module's private helper is the smaller footprint.
    """
    tz = ZoneInfo(net["timezone"])
    local = datetime.fromtimestamp(now_ts, tz=tz)
    days_ahead = (net["weekday"] - local.weekday()) % 7
    start = local.replace(hour=net["start_hour"], minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
    if start <= local:
        start += timedelta(days=7)
    return int(start.timestamp())


def _cmd_nextnet(conn, body: dict) -> dict:
    """Every ENABLED checkin_net row, read fresh at request time -- a
    net an operator adds through the admin panel appears here on the
    very next call, no code change, exactly the same "read fresh, never
    a fixed list" contract app/public_api.py's own _net_window() and
    app/checkin.py's most_recent_net_date() already apply to this same
    table. Each net's start renders as a Discord timestamp
    (<t:UNIX:F> for the absolute date/time, <t:UNIX:R> for "in 3 days") --
    Discord itself converts both to the READER's own local time zone, so
    a MeshCore net in America/Boise and a Meshtastic net in
    America/Denver both show correctly to every reader regardless of
    where they are.
    """
    now = int(time.time())
    nets = conn.execute(
        "SELECT label, weekday, start_hour, timezone FROM checkin_net "
        " WHERE enabled = 1 ORDER BY id"
    ).fetchall()
    if not nets:
        return _data(content="No check-in nets are configured right now.")
    lines = []
    for net in nets:
        start_ts = _next_net_start(net, now)
        label = net["label"] or "Net"
        lines.append(f"**{label}** -- <t:{start_ts}:F> (<t:{start_ts}:R>)")
    embed = {"title": "Next nets", "description": "\n".join(lines)}
    return _data(embeds=[embed])


# ---- /player --------------------------------------------------------------


def _cmd_player(conn, body: dict) -> dict:
    """Look up a player by display name. Exact, case-insensitive match
    first; failing that, up to 5 case-insensitive PREFIX matches listed
    by name only -- never a score, never a team, for a name this caller
    only partially guessed right.

    Deliberately queries `player` directly for display_name/team ONLY --
    see this module's own docstring for why app/mc_api.py's find_for()
    (which already does an exact-name lookup) is not reused here: it
    also computes and returns a bounding box and a last-position
    timestamp, and this command must never be able to leak either, no
    matter how the response is assembled downstream. Rank and points
    still come from the exact same _team_standings() helper /standings
    and /me use -- no second scoring path, just a narrower identity
    query.
    """
    name = (_options_map(body).get("name") or "").strip()
    if not name:
        return _ephemeral("Give a player name to look up.")

    exact = conn.execute(
        "SELECT display_name, team FROM player "
        " WHERE disabled_at IS NULL AND LOWER(display_name) = LOWER(?)",
        (name,),
    ).fetchone()

    if exact is None:
        escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        prefix_rows = conn.execute(
            "SELECT display_name FROM player "
            " WHERE disabled_at IS NULL AND LOWER(display_name) LIKE LOWER(?) ESCAPE '\\' "
            " ORDER BY display_name LIMIT 5",
            (escaped + "%",),
        ).fetchall()
        if not prefix_rows:
            return _ephemeral(f"No player found matching '{name}'.")
        listing = "\n".join(f"- {r['display_name']}" for r in prefix_rows)
        return _ephemeral(f"No exact match for '{name}'. Did you mean:\n{listing}")

    cfg = discord_notify.load_discord_config(conn)
    emoji = discord_notify._parse_team_emoji(cfg["team_emoji"])
    dot = discord_notify._team_dot(emoji, exact["team"])
    lines = [f"{dot}**{exact['display_name']}**"]
    for protocol in (MC_PROTOCOL, MT_PROTOCOL):
        rows = _team_standings(conn, protocol)
        row = next((r for r in rows if r["team"] == exact["team"]), None)
        if row:
            lines.append(f"{_BOARD_LABELS[protocol]}: {_team_line(cfg, row)}")

    embed = {"title": "Player", "description": "\n".join(lines)}
    color = discord_notify._team_color(exact["team"])
    if color is not None:
        embed["color"] = color
    return _data(embeds=[embed])


# ---- the one registry -----------------------------------------------------
#
# Every command's Discord definition AND handler, in one place, so
# app/discord_bot.py's register_commands() (what Discord is told exists,
# via each Command's own .definition()) and _dispatch() below (what
# actually runs, via .handler) read off the exact same list and can
# never disagree about what commands this bot has.


class Command(NamedTuple):
    name: str
    description: str
    options: list[dict]
    handler: Callable[[object, dict], dict]
    always_ephemeral: bool

    def definition(self) -> dict:
        """The Discord-facing command definition -- name, description,
        options ONLY, never `handler`/`always_ephemeral`, which mean
        nothing to Discord's own PUT .../commands body (see
        app/discord_bot.py's register_commands()).
        """
        return {"name": self.name, "description": self.description, "options": self.options}


COMMANDS: list[Command] = [
    Command(
        name="me",
        description="Show your own MeshWars profile (only you can see this)",
        options=[],
        handler=_cmd_me,
        always_ephemeral=True,
    ),
    Command(
        name="standings",
        description="Current team standings for the active season",
        options=[_BOARD_OPTION],
        handler=_cmd_standings,
        always_ephemeral=False,
    ),
    Command(
        name="honors",
        description="Monthly honors for a finished month (default: most recent)",
        options=[
            _BOARD_OPTION,
            {
                "type": _OPTION_TYPE_STRING,
                "name": "month",
                "description": "A finished month, e.g. 2026-08 (default: most recent)",
                "required": False,
            },
        ],
        handler=_cmd_honors,
        always_ephemeral=False,
    ),
    Command(
        name="nextnet",
        description="When each check-in net next opens",
        options=[],
        handler=_cmd_nextnet,
        always_ephemeral=False,
    ),
    Command(
        name="player",
        description="Look up a player by name",
        options=[
            {
                "type": _OPTION_TYPE_STRING,
                "name": "name",
                "description": "Player display name",
                "required": True,
            },
        ],
        handler=_cmd_player,
        always_ephemeral=False,
    ),
]

_COMMANDS_BY_NAME: dict[str, Command] = {c.name: c for c in COMMANDS}


# ---- signature verification ------------------------------------------


def _verify_signature(public_key_hex: str, signature_hex: str, timestamp: str, raw_body: bytes) -> bool:
    """True only if `signature_hex` is a valid Ed25519 signature, by the
    key `public_key_hex` names, over exactly `timestamp.encode() +
    raw_body` -- Discord's own documented signing scheme for this
    endpoint. ANY failure along the way (malformed hex in either
    argument, a key or signature of the wrong length, or a signature
    that simply does not verify) returns False rather than raising --
    the caller's contract is "reject with 401," not "crash," and this is
    the one place that distinction is made for every kind of failure at
    once.
    """
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        key.verify(bytes.fromhex(signature_hex), timestamp.encode("utf-8") + raw_body)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def _fresh_timestamp(raw: str) -> bool:
    """True only if `raw` (X-Signature-Timestamp, a decimal unix-seconds
    string) parses and is within _MAX_TIMESTAMP_SKEW_SECONDS of now, in
    either direction -- replay protection, entirely separate from
    signature validity: a captured request replayed later still carries
    a perfectly valid signature over its own (old) timestamp, and this
    is the only check that catches that.
    """
    try:
        ts = int(raw)
    except (TypeError, ValueError):
        return False
    return abs(int(time.time()) - ts) <= _MAX_TIMESTAMP_SKEW_SECONDS


# ---- dispatch, the 3-second budget, and the deferred follow-up --------


async def _run_command(entry: Command, body: dict) -> dict:
    """Run one command's SYNC handler against a fresh connection, off
    the event loop (asyncio.to_thread) -- every handler above does
    blocking sqlite3 work, same as the rest of this codebase's
    request handlers, and this is the one place that gives it a thread
    instead of stalling the loop for however long the query takes.
    """
    conn = connect()
    try:
        return await asyncio.to_thread(entry.handler, conn, body)
    finally:
        conn.close()


async def _patch_followup(app_id: str, token: str, data: dict, *, http_client: httpx.AsyncClient | None = None) -> None:
    """Deliver a deferred command's real answer via PATCH
    /webhooks/{app_id}/{token}/messages/@original -- Discord's own
    documented way to fill in a type-5 "more to come" acknowledgement.
    Authenticates with `token`, THIS INTERACTION'S OWN token, embedded in
    the URL -- there is no Authorization header at all, and specifically
    NEVER app/discord_bot.py's `Authorization: Bot ...` header: a bot
    token authenticates this app as a bot user in the guild for
    unrelated calls (role sync), while an interaction token authenticates
    only this one already-issued response and expires shortly after
    Discord considers the interaction done.

    `http_client` is accepted purely so tests can hand this an
    httpx.AsyncClient wired to an httpx.MockTransport -- the same
    injectable-client shape app/discord_notify.py's _post() and
    app/discord_bot.py's _request() already use for their own outbound
    calls. A failure here is logged and swallowed, never raised: by the
    time this runs, the interaction's own type-5 acknowledgement has
    already gone back to Discord, so there is no request left to fail
    outward to.
    """
    url = f"https://discord.com/api/v10/webhooks/{app_id}/{token}/messages/@original"
    client = http_client
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=10.0)
    try:
        try:
            await client.patch(url, json=data)
        except httpx.HTTPError:
            log.exception("discord interactions: deferred follow-up PATCH failed")
    finally:
        if owns_client:
            await client.aclose()


async def _finish_deferred(task: "asyncio.Task[dict]", body: dict, app_id: str,
                            *, http_client: httpx.AsyncClient | None = None) -> None:
    """The background half of a deferred command: await the SAME
    handler task _dispatch() below already started (shielded from its
    own asyncio.wait_for timeout, so the timeout tripping never cancels
    it -- see _dispatch()'s own comment), then deliver whatever it
    produces, or a generic sanitized error if it raised, via
    _patch_followup() above. Deliberately awaits the ORIGINAL task
    rather than re-invoking the handler a second time: the handler
    already owns one open sqlite3 connection (_run_command()'s own
    `finally: conn.close()`), and cancelling that task out from under a
    still-running thread would either race the close against an
    in-flight query or double the DB work for no reason -- awaiting the
    one already in flight avoids both. Never raises -- this runs as a
    fire-and-forget asyncio.Task with nothing awaiting it directly, so
    an unhandled exception here would only ever become a silent,
    unlogged failure.
    """
    try:
        data = await task
    except Exception:
        log.exception("discord interactions: deferred handler failed")
        data = _ephemeral("Something went wrong.")
    token = body.get("token") or ""
    if not app_id or not token:
        log.error("discord interactions: cannot deliver deferred response -- missing app id or token")
        return
    await _patch_followup(app_id, token, data, http_client=http_client)


async def _dispatch(body: dict, cfg: dict, *, http_client: httpx.AsyncClient | None = None) -> dict:
    """Run the named command under _HANDLER_BUDGET_SECONDS. Finishes in
    time -> its own answer, type 4. Times out -> type 5 immediately
    (carrying the ephemeral flag for a command that is always ephemeral,
    e.g. /me); the handler task itself is NOT cancelled when the budget
    trips (asyncio.shield below) -- it keeps running to completion in
    the background, and _finish_deferred() above awaits that SAME task
    and delivers its real result later via PATCH. An unknown command
    name or a handler that raises within the budget both produce a
    short, ephemeral, sanitized error -- never a stack trace back to
    Discord.
    """
    data = body.get("data") or {}
    name = data.get("name")
    entry = _COMMANDS_BY_NAME.get(name)
    if entry is None:
        return _response(_RESPONSE_CHANNEL_MESSAGE, _ephemeral("Unknown command."))

    task = asyncio.create_task(_run_command(entry, body))
    try:
        result = await asyncio.wait_for(asyncio.shield(task), timeout=_HANDLER_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        followup = asyncio.create_task(
            _finish_deferred(task, body, cfg.get("app_id") or "", http_client=http_client)
        )
        for t in (task, followup):
            _BACKGROUND_TASKS.add(t)
            t.add_done_callback(_BACKGROUND_TASKS.discard)
        deferred_data = {"allowed_mentions": _ALLOWED_MENTIONS}
        if entry.always_ephemeral:
            deferred_data["flags"] = _FLAG_EPHEMERAL
        return _response(_RESPONSE_DEFERRED_CHANNEL_MESSAGE, deferred_data)
    except Exception:
        log.exception("discord interactions: handler %r failed", name)
        return _response(_RESPONSE_CHANNEL_MESSAGE, _ephemeral("Something went wrong."))

    return _response(_RESPONSE_CHANNEL_MESSAGE, result)


# ---- the route --------------------------------------------------------


@router.post("/api/discord/interactions")
async def discord_interactions_endpoint(request: Request):
    """See this module's own docstring for the full security/gating/
    budget contract. Order matters: gate on config first (no signature
    to check against without a public key anyway), then verify the
    signature over the RAW body bytes, then check the timestamp's
    freshness, and only then parse the body as JSON -- a request that
    fails any earlier step never reaches the next one.
    """
    conn = connect()
    try:
        cfg = discord_notify.load_discord_config(conn)
    finally:
        conn.close()

    if not cfg.get("slash_enabled") or not cfg.get("public_key"):
        return Response(status_code=404)

    signature = request.headers.get("X-Signature-Ed25519", "")
    timestamp = request.headers.get("X-Signature-Timestamp", "")
    if not signature or not timestamp:
        return Response(status_code=401)

    raw_body = await request.body()
    if not _verify_signature(cfg["public_key"], signature, timestamp, raw_body):
        return Response(status_code=401)
    if not _fresh_timestamp(timestamp):
        return Response(status_code=401)

    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return Response(status_code=400)
    if not isinstance(body, dict):
        return Response(status_code=400)

    itype = body.get("type")
    if itype == _TYPE_PING:
        return JSONResponse({"type": _RESPONSE_PONG})
    if itype == _TYPE_APPLICATION_COMMAND:
        return JSONResponse(await _dispatch(body, cfg))
    return Response(status_code=400)
