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

---- ACCOUNT COMMANDS: /link, /join, /radios, /setupcheck --------------

Everything above (/me, /standings, /honors, /nextnet, /player) is a
read-only public lookup that never requires a caller to have linked
anything. The commands below are different in kind: they act on the
CALLING Discord user's own MeshWars account, using the exact same
account/player model app/account_api.py, app/oauth_api.py,
app/join_api.py, and app/nodes_api.py already implement for the
website. THE PRINCIPLE that governs every one of them:

A Discord user whose snowflake is in account_identity (provider
='discord') is treated as SIGNED IN to that account -- the same
authority as clicking "Sign in with Discord" on the site. Because
OAuth sign-in already bypasses this app's own TOTP (see
app/totp_api.py's module docstring for exactly why), Discord must
NEVER be able to change anything that protects the account. The
following are WEBSITE-ONLY, and will never be added here: setting or
changing a password, enabling/disabling TOTP, setting or changing the
account's contact email, adding or removing a sign-in identity
(including Discord's own), issuing or displaying API keys (the one
exception -- and it is not an exception to this rule, just its literal
form -- is the one-time key POST /api/join itself mints on the
website; a Discord /join never mints or shows one, for either
protocol, see that command's own docstring below), rotating or
otherwise ever showing an existing API key, logging out or revoking
sessions, deleting an account, releasing a player link (operator-only,
see app/admin_api.py), and every operator/admin action. Every command
below is read-only or acts ONLY on the caller's own player/radios --
nothing here ever touches account_password, account_totp,
account.contact_email, or any admin-gated table.

REUSE: every command below calls the SAME service logic the website's
own routes call -- app/account_api.py's _claim_player() (POST
/api/account/link-key's own conflict checks and write),
app/oauth_api.py's _create_account_with_identity() (the same new-
account write case 4's "create a new account" choice performs),
app/join_api.py's _create_player() (POST /api/join's own dup-name/
node-conflict checks and inserts), and app/nodes_api.py's
add_node_for_player()/remove_node_for_player() (POST /api/nodes and
DELETE /api/nodes/{node_ref}'s own logic) -- never a second copy of
any of that SQL, and never an HTTP call to this app's own routes (see
each command's own docstring for exactly which shared function it
calls).

RESOLVING THE CALLER: _resolve_caller() below is the one place that
turns a Discord snowflake into (account_id, player_id, disabled) --
every command in this section calls it fresh, on EVERY interaction
(the initial command invocation AND every later component/modal
interaction it produces), never trusting a custom_id's own claim about
who is acting. A caller whose account.disabled_at is set is refused,
ephemerally, on every one of these commands -- note that
account.disabled_at is a column the WEBSITE currently never reads at
all (a known gap; every other disablement in this codebase is on
`player`, not `account`), and this module deliberately does not
repeat that gap for its own surface rather than wait for the website
to close it first.

CUSTOM_ID SCHEME: every button/select/modal this section opens uses a
custom_id of the form "<namespace>:<action>[:<arg>]" (Discord caps
this at 100 characters) -- see _custom_id_action() below for how that
is parsed for dispatch. An arg on a custom_id or a select option's
`value` is DATA (which radio, which node_ref) describing what was
clicked, NEVER identity or authority: every handler re-resolves the
CLICKING user's own snowflake (never anything the custom_id claims
about who owns what) and re-checks ownership against the database
before acting, on every single interaction including a confirm click
that follows an already-verified select -- see each handler's own
comment for exactly where that re-check happens.

EPHEMERAL, ALWAYS: every response in this section -- the initial
command's, and every later component/modal response it leads to --
carries the ephemeral flag and allowed_mentions.parse == [] (see this
module's own constants above). These commands only ever show a caller
their OWN radios, keys (never displayed, only ever minted once and
handed back the moment they're created), or setup diagnosis -- never
another player's.
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

from . import account_api, checkin_api, discord_bot, discord_notify, join_api, mc_api, nodes_api, oauth_api, results
from .auth import new_rate_limit_bucket
from .config import settings
from .db import WriteSession, connect
from .mc_ingest import PROTOCOL as MC_PROTOCOL
from .mc_scoring import team_totals
from .node_ref import normalize_node_ref
from .oauth import ProviderIdentity

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
_TYPE_MESSAGE_COMPONENT = 3
_TYPE_MODAL_SUBMIT = 5

# Discord's own documented response types this route ever sends.
_RESPONSE_PONG = 1
_RESPONSE_CHANNEL_MESSAGE = 4
_RESPONSE_DEFERRED_CHANNEL_MESSAGE = 5
# UPDATE_MESSAGE -- a component click's own reply that replaces the
# message the component lives on, rather than posting a new one (a
# select-then-confirm flow's own "here's what you chose" step, e.g.
# /radios' remove confirmation below).
_RESPONSE_UPDATE_MESSAGE = 7
# MODAL -- the only response type that can open a Discord modal. Only
# ever valid as the FIRST response to an APPLICATION_COMMAND or
# MESSAGE_COMPONENT interaction (Discord's own documented contract);
# never valid as a modal's own submit response, or as a deferred
# follow-up PATCH -- see _modal_response()'s own docstring below for
# how each command that opens one plans around that.
_RESPONSE_MODAL = 9

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

# /claimnode's own board option -- REQUIRED, unlike _BOARD_OPTION above:
# every other board-scoped command has a sensible default (MeshCore) to
# fall back to for a read-only lookup, but opening a confirmation window
# is a write with real consequences (see _cmd_claimnode's own docstring
# on cancel-and-restart), so a caller must say which radio type they
# mean rather than have one silently assumed.
_CLAIMNODE_BOARD_OPTION = {
    "type": _OPTION_TYPE_STRING,
    "name": "board",
    "description": "Which board -- MeshCore or Meshtastic",
    "required": True,
    "choices": [
        {"name": "MeshCore", "value": "mc"},
        {"name": "Meshtastic", "value": "mt"},
    ],
}

_CLAIMNODE_NAME_OPTION = {
    "type": _OPTION_TYPE_STRING,
    "name": "name",
    "description": "MeshCore only -- the name your radio currently shows on the mesh",
    "required": False,
}

# Discord's own component types (discord.dev's "Component Types") this
# module ever builds. Named here for the same reason _OPTION_TYPE_STRING
# is: so the account-command builders below read as what they mean.
_COMPONENT_ACTION_ROW = 1
_COMPONENT_BUTTON = 2
_COMPONENT_STRING_SELECT = 3
_COMPONENT_TEXT_INPUT = 4

# Discord's own button styles -- Danger (red) for a destructive confirm,
# Secondary (grey) for its paired cancel, Primary (blurple) for every
# other button this module opens.
_BUTTON_STYLE_PRIMARY = 1
_BUTTON_STYLE_SECONDARY = 2
_BUTTON_STYLE_DANGER = 4

# Discord's own text input style -- every field this module ever asks
# for (an API key, a display name, a team, a protocol) fits on one
# line.
_TEXT_INPUT_STYLE_SHORT = 1

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


# ---- account commands: shared plumbing (/link, /join, /radios, /setupcheck) --


def _modal_response(*, custom_id: str, title: str, components: list[dict]) -> dict:
    """A full MODAL (type 9) response ENVELOPE -- unlike _data()'s bare
    message-data dict, a Modal object (discord.dev) has no
    `allowed_mentions`/`flags` fields at all, so this builds the whole
    {"type": 9, "data": {...}} shape directly rather than going through
    _response()/_data(). A Command (or component) handler that returns
    this instead of a plain message-data dict signals _dispatch()/
    _dispatch_interactive() below to use ITS type verbatim instead of
    wrapping the result as the default type 4 -- a bare message-data
    dict never carries a top-level "type" key of its own, so the two
    are unambiguous. Only ever valid as the FIRST response to a command
    or component click, never as a modal's own submit response and
    never as a deferred follow-up PATCH (Discord's own documented
    contract) -- every handler that can return this (see COMMANDS'
    own /link and /join entries, and _cmd_radios_add_open below) does
    only a single indexed SELECT before answering, so this module never
    actually risks hitting the 2-second budget on one of these and
    having to defer it into an impossible PATCH.
    """
    return {
        "type": _RESPONSE_MODAL,
        "data": {"custom_id": custom_id, "title": title, "components": components},
    }


def _text_input_row(custom_id: str, label: str, *, required: bool = True, max_length: int | None = None) -> dict:
    """One single-field action row for a modal's own `components` list
    -- Discord nests every input inside its own action row (a modal
    cannot place two inputs in one row), so every _modal_response()
    call site below builds its `components` list out of one of these
    per field rather than repeating this two-level shape by hand.
    """
    field: dict = {
        "type": _COMPONENT_TEXT_INPUT,
        "custom_id": custom_id,
        "style": _TEXT_INPUT_STYLE_SHORT,
        "label": label,
        "required": required,
    }
    if max_length is not None:
        field["max_length"] = max_length
    return {"type": _COMPONENT_ACTION_ROW, "components": [field]}


def _modal_values(body: dict) -> dict[str, str]:
    """{custom_id: value} for every text-input field submitted with a
    MODAL_SUBMIT interaction. Discord nests each field inside its own
    single-item action row (data.components: [{type: 1, components:
    [{type: 4, custom_id, value}]}]), never flat -- this flattens it
    once here instead of every modal handler below re-walking the same
    two-level structure.
    """
    data = body.get("data") or {}
    out: dict[str, str] = {}
    for row in data.get("components") or []:
        for comp in row.get("components") or []:
            cid = comp.get("custom_id")
            if cid is not None:
                out[cid] = comp.get("value") or ""
    return out


def _custom_id_action(custom_id: str) -> str:
    """The dispatch key for a component/modal custom_id -- this
    module's own scheme (see this module's docstring's CUSTOM_ID
    SCHEME section) is "<namespace>:<action>[:<arg>]", and the
    namespace+action pair (never the arg) is what
    _COMPONENT_HANDLERS/_MODAL_HANDLERS below are keyed on. Example:
    "radios:remove_confirm:mc:aabbccdd" -> "radios:remove_confirm";
    "link:modal" -> "link:modal" (no arg at all).
    """
    parts = custom_id.split(":")
    return ":".join(parts[:2]) if len(parts) >= 2 else custom_id


def _disabled_account_message() -> dict:
    return _ephemeral(
        "This Discord account is linked to a MeshWars account that has been disabled."
    )


def _unlinked_pointer_message() -> dict:
    return _ephemeral(
        "You haven't linked a MeshWars account yet. Use /link if you already have a "
        "player, or /join to create one."
    )


class Caller(NamedTuple):
    """What _resolve_caller() below knows about the Discord user making
    ONE interaction -- see that function's own docstring.
    """
    account_id: int
    player_id: int | None
    disabled: bool


def _snowflake_from_body(body: dict) -> str | None:
    """The invoking Discord user's own snowflake id, from whichever of
    the two shapes discord.dev's Interaction Object carries it in --
    same member.user.id / user.id fallback _cmd_me() above already
    uses, factored out here since every account command and every
    component/modal interaction it produces needs this same lookup.
    """
    member = body.get("member") or {}
    user = member.get("user") or body.get("user") or {}
    snowflake = user.get("id")
    return str(snowflake) if snowflake else None


def _resolve_caller(conn, body: dict) -> Caller | None:
    """snowflake -> account (or None) -> player (or None) -- the ONE
    place every account command in this section uses to find out who
    is asking, called fresh on EVERY interaction (see this module's own
    docstring on why a later component/modal click never trusts its
    own custom_id for identity). Returns None only when this Discord
    snowflake has never linked a MeshWars account at all -- callers
    that see None point the caller at /link or /join (see
    _unlinked_pointer_message() above).
    """
    snowflake = _snowflake_from_body(body)
    if snowflake is None:
        return None
    row = conn.execute(
        "SELECT a.account_id AS account_id, a.disabled_at AS disabled_at, "
        "       p.player_id AS player_id "
        "  FROM account_identity ai "
        "  JOIN account a ON a.account_id = ai.account_id "
        "  LEFT JOIN player p ON p.account_id = a.account_id "
        " WHERE ai.provider = 'discord' AND ai.subject = ?",
        (snowflake,),
    ).fetchone()
    if row is None:
        return None
    return Caller(
        account_id=row["account_id"], player_id=row["player_id"],
        disabled=row["disabled_at"] is not None,
    )


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


# ---- account commands: /link, /join, /radios, /setupcheck -----------------
#
# See this module's own docstring's ACCOUNT COMMANDS section for the
# full contract (the signed-in-as-Discord principle, the website-only
# list, reuse, caller resolution, the custom_id scheme, ephemeral-
# always). Everything below implements that.

# Per-SNOWFLAKE rate limits -- every request here arrives from
# Discord's own edge IPs, never the caller's, so an address-keyed
# limiter (every other rate limit in this codebase) would do nothing.
# /link mirrors the website's own link-key budget exactly
# (settings.account_link_key_rate_limit_*) since it is the identical
# sensitive action (a key-guessing oracle without a limit -- see
# app/account_api.py's own module comment on _link_key_addr_limiter);
# /join and radio changes reuse the website's own equally-shaped
# budgets (settings.join_rate_limit_*, settings.
# account_rotate_key_rate_limit_*) for the same reason -- neither of
# those actions needs a tighter or looser cadence than the one already
# chosen for its website equivalent.
_link_rate_limiter = new_rate_limit_bucket()
_join_rate_limiter = new_rate_limit_bucket()
_radios_rate_limiter = new_rate_limit_bucket()
# /claimnode's own budget -- no website equivalent shaped quite like it
# (POST /api/checkin/confirm/start is key/session-authenticated, not
# snowflake-rate-limited at all), so this reuses
# settings.account_rotate_key_rate_limit_* -- the same "occasional,
# sensitive write action" cadence /radios' own add/submit already
# borrows for the identical reason.
_claimnode_rate_limiter = new_rate_limit_bucket()


def _cmd_link(conn, body: dict) -> dict:
    """Opens a MODAL for the caller's API key -- see this module's own
    docstring for why the key is NEVER accepted as a slash-command
    option (an option's value sits in Discord's own command-usage
    history/autocomplete for anyone who can see the interaction; a
    modal's own submission is not). The only thing checked before
    opening it is whether the caller's account (if one already exists)
    is disabled -- everything else (already has a player, key belongs
    to someone else, unknown key, ...) is exactly what
    _cmd_link_modal_submit() below already refuses, so re-deriving it
    here too would just be two places that have to agree instead of
    one.
    """
    caller = _resolve_caller(conn, body)
    if caller is not None and caller.disabled:
        return _disabled_account_message()
    return _modal_response(
        custom_id="link:modal",
        title="Link your MeshWars key",
        components=[_text_input_row("api_key", "Your MeshWars API key", max_length=200)],
    )


async def _cmd_link_modal_submit(conn, body: dict, ingestor) -> tuple[int, dict]:
    """/link's modal submit. Authenticates the pasted key the exact
    same way POST /api/account/link-key does
    (request.app.state.mc_ingestor.authenticate() -- threaded through
    here as `ingestor`, see _dispatch_interactive()'s own docstring for
    why this handler takes it as a parameter instead of reaching for a
    Request this interaction never carries), auto-creating an account
    for a snowflake with none yet via app/oauth_api.py's
    _create_account_with_identity() (the SAME write case 4's "create a
    new account" choice performs), then claims the player through
    app/account_api.py's _claim_player() -- the SAME two conflict
    checks and write POST /api/account/link-key already performs.
    Never echoes the raw key back, in this response or in a log line.
    """
    snowflake = _snowflake_from_body(body)
    if snowflake is None:
        return _RESPONSE_CHANNEL_MESSAGE, _ephemeral("Could not identify your Discord account.")

    if _link_rate_limiter.limited(
        snowflake,
        limit=settings.account_link_key_rate_limit_attempts,
        window=settings.account_link_key_rate_limit_window_seconds,
    ):
        return _RESPONSE_CHANNEL_MESSAGE, _ephemeral("Too many attempts -- try again in a minute.")

    caller = _resolve_caller(conn, body)
    if caller is not None and caller.disabled:
        return _RESPONSE_CHANNEL_MESSAGE, _disabled_account_message()

    raw_key = _modal_values(body).get("api_key", "").strip()
    if not raw_key:
        return _RESPONSE_CHANNEL_MESSAGE, _ephemeral("An API key is required.")

    if ingestor is None:
        log.error("discord interactions: /link submit with no mc_ingestor configured")
        return _RESPONSE_CHANNEL_MESSAGE, _ephemeral("Something went wrong.")

    auth = await ingestor.authenticate(raw_key)
    if auth.status in ("not_found", "revoked"):
        # Same generic wording every other key-authenticated route in
        # this app uses for both statuses -- see app/auth.py's own
        # comment on why not_found/revoked must stay indistinguishable
        # from the response alone.
        return _RESPONSE_CHANNEL_MESSAGE, _ephemeral("That key is not valid.")
    if auth.status == "disabled":
        return _RESPONSE_CHANNEL_MESSAGE, _ephemeral("That key's player is disabled.")

    player_id = auth.player_id
    now = int(time.time())

    async with WriteSession() as wconn:
        if caller is None:
            account_id = oauth_api._create_account_with_identity(
                wconn, provider_name="discord",
                identity=ProviderIdentity(subject=snowflake, email=None, email_verified=False),
                now=now, detail_suffix=" (via Discord)",
            )
        else:
            account_id = caller.account_id

        outcome = account_api._claim_player(
            wconn, account_id=account_id, player_id=player_id, now=now,
            detail_suffix=" (via Discord)",
        )
        if outcome.kind == "conflict":
            return _RESPONSE_CHANNEL_MESSAGE, _ephemeral(outcome.error["error"])

        player = account_api._player_out(wconn, player_id)
        contact_email = (
            account_api._verified_contact_email(wconn, account_id)
            if outcome.kind == "linked" else None
        )

    if outcome.kind == "linked":
        # Same fire-and-forget, never-break-the-response contract every
        # other call site of these two gives -- see
        # app/account_api.py's link_key() for the identical pairing.
        sync_conn = connect()
        try:
            await discord_bot.sync_member_safe(sync_conn, player_id)
        finally:
            sync_conn.close()

        when = account_api.format_notice_timestamp(now)
        await account_api._notify_security(
            account_id, contact_email,
            subject="A player was claimed on your MeshWars account",
            heading="A player was claimed",
            lines=(
                f"The player {player['display_name']} was claimed on your MeshWars "
                f"account on {when}.",
                "If that was you, there is nothing to do.",
                "If it wasn't, someone else has access. Sign in, rotate your API key, "
                "and check which sign-in methods are attached to the account.",
            ),
        )

    verb = "Linked" if outcome.kind == "linked" else "Already linked"
    return _RESPONSE_CHANNEL_MESSAGE, _ephemeral(
        f"{verb} -- you're playing as **{player['display_name']}** on team {player['team']}."
    )


def _cmd_join(conn, body: dict) -> dict:
    """Opens a MODAL asking for what the website's own signed-in join
    path (POST /api/join with a session -- app/join_api.py's join())
    asks for: display name, team, and radio type. This deployment lets
    a player CHOOSE their own team (settings.teams_list -- the SAME
    choices join()'s own team validation checks against, via
    app/join_api.py's _validate_team()), so the modal names those same
    choices rather than assigning one. No invite code -- a Discord
    caller is already as authenticated as a signed-in website caller,
    the same reasoning join()'s own docstring gives for skipping it
    there.
    """
    caller = _resolve_caller(conn, body)
    if caller is not None and caller.disabled:
        return _disabled_account_message()
    if caller is not None and caller.player_id is not None:
        return _ephemeral("You already have a player linked. Use /me to see your profile.")
    return _modal_response(
        custom_id="join:modal",
        title="Join MeshWars",
        components=[
            _text_input_row("display_name", "Display name (1-32 characters)", max_length=32),
            _text_input_row("team", f"Team: {', '.join(settings.teams_list)}", max_length=16),
            _text_input_row("protocol", "Radio: mc (MeshCore) or mt (Meshtastic)", max_length=2),
        ],
    )


async def _cmd_join_modal_submit(conn, body: dict, ingestor) -> tuple[int, dict]:
    """/join's modal submit. Mirrors app/join_api.py's own signed-in
    join path field for field: _validate_display_name()/_validate_team()
    are the SAME functions join() itself validates against (never a
    second copy), and _create_player() is the exact write join()
    performs (dup-name check, node conflict check, player/key/node
    inserts, account link) -- see that function's own docstring for
    exactly what changed to let this call it with mint_key=False.

    NEVER mints or shows an API key, for EITHER protocol -- see this
    module's own docstring's website-only list ("issuing or displaying
    API keys"). A MeshCore joiner still needs one to configure
    MeshMapper; this tells them plainly where to get it (the website's
    own /join page, the only place a key is ever minted) instead.
    """
    snowflake = _snowflake_from_body(body)
    if snowflake is None:
        return _RESPONSE_CHANNEL_MESSAGE, _ephemeral("Could not identify your Discord account.")

    if _join_rate_limiter.limited(
        snowflake,
        limit=settings.join_rate_limit_attempts,
        window=settings.join_rate_limit_window_seconds,
    ):
        return _RESPONSE_CHANNEL_MESSAGE, _ephemeral("Too many attempts -- try again later.")

    caller = _resolve_caller(conn, body)
    if caller is not None and caller.disabled:
        return _RESPONSE_CHANNEL_MESSAGE, _disabled_account_message()
    if caller is not None and caller.player_id is not None:
        return _RESPONSE_CHANNEL_MESSAGE, _ephemeral(
            "You already have a player linked. Use /me to see your profile."
        )

    values = _modal_values(body)
    display_name, err = join_api._validate_display_name(values.get("display_name"))
    if err:
        return _RESPONSE_CHANNEL_MESSAGE, _ephemeral(err)

    team, err = join_api._validate_team(values.get("team"))
    if err:
        return _RESPONSE_CHANNEL_MESSAGE, _ephemeral(
            f"{err} -- choose one of: {', '.join(settings.teams_list)}"
        )

    protocol = (values.get("protocol") or "").strip().lower()
    if protocol not in ("mc", "mt"):
        return _RESPONSE_CHANNEL_MESSAGE, _ephemeral(
            "Radio must be mc (MeshCore) or mt (Meshtastic)."
        )
    if protocol == "mt" and not settings.join_meshtastic_enabled:
        return _RESPONSE_CHANNEL_MESSAGE, _ephemeral("Meshtastic registration is not open yet.")

    now = int(time.time())
    if caller is None:
        async with WriteSession() as wconn:
            account_id = oauth_api._create_account_with_identity(
                wconn, provider_name="discord",
                identity=ProviderIdentity(subject=snowflake, email=None, email_verified=False),
                now=now, detail_suffix=" (via Discord)",
            )
    else:
        account_id = caller.account_id

    # Discord's /join NEVER mints or shows a key -- mint_key=False
    # unconditionally, for EVERY protocol (unlike join()'s own
    # skip_key, which only skips for an authenticated Meshtastic join
    # -- see _create_player()'s own docstring for exactly what this
    # parameter controls and why Discord always passes False).
    error, status, player_id, _raw_key = join_api._create_player(
        display_name=display_name, team=team, protocol=protocol, node_ref=None,
        account_id=account_id, mint_key=False, now=now,
    )
    if error is not None:
        return _RESPONSE_CHANNEL_MESSAGE, _ephemeral(error["error"])

    sync_conn = connect()
    try:
        await discord_bot.sync_member_safe(sync_conn, player_id)
    finally:
        sync_conn.close()

    base = (settings.oauth_public_base_url or "").rstrip("/")
    if protocol == "mc":
        # Website-only: issuing or displaying an API key (see this
        # module's own docstring). MeshMapper needs one to report a
        # position at all, so this points plainly at the one place a
        # key is ever minted -- never a Discord command, because there
        # isn't one and there will not be one.
        note = (
            f" MeshMapper needs an API key to report your position, and keys are only "
            f"issued through the website -- visit {base}/join to get one."
        )
    else:
        note = " Use /radios to add your Meshtastic node once you have its ID."
    return _RESPONSE_CHANNEL_MESSAGE, _ephemeral(
        f"Welcome to MeshWars! You're **{display_name}** on team {team}.{note}"
    )


def _cmd_radios(conn, body: dict) -> dict:
    """Lists the caller's own radios only (protocol + node_ref -- see
    this module's own PRIVACY section: these are shown ONLY to the
    caller they belong to), with an "Add radio" button and, when there
    is at least one to remove, a "Remove" select menu.
    """
    caller = _resolve_caller(conn, body)
    if caller is not None and caller.disabled:
        return _disabled_account_message()
    if caller is None or caller.player_id is None:
        return _unlinked_pointer_message()

    radios = nodes_api._radios_out(conn, caller.player_id)
    if not radios:
        lines = ["You have no radios registered yet."]
    else:
        lines = ["Your radios:"] + [
            f"- {_BOARD_LABELS.get(r['protocol'], r['protocol'])}: `{r['node_ref']}`"
            for r in radios
        ]

    components = [{
        "type": _COMPONENT_ACTION_ROW,
        "components": [{
            "type": _COMPONENT_BUTTON, "style": _BUTTON_STYLE_PRIMARY,
            "label": "Add radio", "custom_id": "radios:add_open",
        }],
    }]
    if radios:
        components.append({
            "type": _COMPONENT_ACTION_ROW,
            "components": [{
                "type": _COMPONENT_STRING_SELECT,
                "custom_id": "radios:remove_select",
                "placeholder": "Remove a radio...",
                "options": [
                    {
                        "label": f"{_BOARD_LABELS.get(r['protocol'], r['protocol'])} {r['node_ref']}",
                        "value": f"{r['protocol']}:{r['node_ref']}",
                    }
                    for r in radios
                ],
            }],
        })

    data = _ephemeral("\n".join(lines))
    data["components"] = components
    return data


async def _cmd_radios_add_open(conn, body: dict, ingestor) -> tuple[int, dict]:
    """The "Add radio" button -- opens a MODAL for protocol + node id.
    Re-resolves the CLICKING user fresh (see this module's own
    docstring) even though this button's own custom_id carries no
    claim about anyone at all -- the eventual add
    (_cmd_radios_add_submit below) always acts on THIS SAME re-resolved
    caller, never anything a custom_id could name.
    """
    caller = _resolve_caller(conn, body)
    if caller is not None and caller.disabled:
        return _RESPONSE_CHANNEL_MESSAGE, _disabled_account_message()
    if caller is None or caller.player_id is None:
        return _RESPONSE_CHANNEL_MESSAGE, _unlinked_pointer_message()
    return _RESPONSE_MODAL, {
        "custom_id": "radios:add_submit",
        "title": "Add a radio",
        "components": [
            _text_input_row("protocol", "Protocol: mc (MeshCore) or mt (Meshtastic)", max_length=2),
            _text_input_row("node_ref", "Node id (8 hex chars, with or without !)", max_length=16),
        ],
    }


async def _cmd_radios_add_submit(conn, body: dict, ingestor) -> tuple[int, dict]:
    """Modal submit for "Add radio" -- calls the exact same logic POST
    /api/nodes does (app/nodes_api.py's add_node_for_player(), which
    already includes app/node_ref.py's own normalization and the
    cross-player conflict check), scoped to the FRESHLY re-resolved
    caller's own player_id, never anything the modal's custom_id names.
    """
    caller = _resolve_caller(conn, body)
    if caller is not None and caller.disabled:
        return _RESPONSE_CHANNEL_MESSAGE, _disabled_account_message()
    if caller is None or caller.player_id is None:
        return _RESPONSE_CHANNEL_MESSAGE, _unlinked_pointer_message()

    snowflake = _snowflake_from_body(body)
    if snowflake and _radios_rate_limiter.limited(
        snowflake,
        limit=settings.account_rotate_key_rate_limit_attempts,
        window=settings.account_rotate_key_rate_limit_window_seconds,
    ):
        return _RESPONSE_CHANNEL_MESSAGE, _ephemeral("Too many attempts -- try again in a minute.")

    values = _modal_values(body)
    result, status = nodes_api.add_node_for_player(
        caller.player_id, values.get("protocol"), values.get("node_ref"), None,
    )
    if status >= 400:
        return _RESPONSE_CHANNEL_MESSAGE, _ephemeral(result["error"])

    verb = "Added" if result.get("added") else "Already registered"
    return _RESPONSE_CHANNEL_MESSAGE, _ephemeral(f"{verb}. Use /radios to see your full list.")


async def _cmd_radios_remove_select(conn, body: dict, ingestor) -> tuple[int, dict]:
    """The "Remove" select menu's own submit -- the chosen option's
    VALUE ("<protocol>:<node_ref>") names the radio, never who owns it.
    Ownership is checked here, fresh, against the CLICKING user's own
    re-resolved player_id, before ever showing a confirm step -- see
    this module's own docstring's CUSTOM_ID SCHEME section on why an
    arg is data, never authority.
    """
    caller = _resolve_caller(conn, body)
    if caller is not None and caller.disabled:
        return _RESPONSE_UPDATE_MESSAGE, _disabled_account_message()
    if caller is None or caller.player_id is None:
        return _RESPONSE_UPDATE_MESSAGE, _unlinked_pointer_message()

    data = body.get("data") or {}
    values = data.get("values") or []
    chosen = values[0] if values else ""
    protocol, _, node_ref = chosen.partition(":")

    owned = conn.execute(
        "SELECT 1 FROM player_node WHERE protocol = ? AND node_ref = ? AND player_id = ?",
        (protocol, node_ref, caller.player_id),
    ).fetchone()
    if owned is None:
        # Either a tampered value naming a radio this caller never
        # owned, or one that was removed by something else since the
        # list was rendered -- both refused the same way, never a
        # guess at which.
        return _RESPONSE_UPDATE_MESSAGE, _ephemeral(
            "That radio isn't yours (or it's already been removed)."
        )

    label = f"{_BOARD_LABELS.get(protocol, protocol)} `{node_ref}`"
    reply = _ephemeral(f"Remove {label}?")
    reply["components"] = [{
        "type": _COMPONENT_ACTION_ROW,
        "components": [
            {
                "type": _COMPONENT_BUTTON, "style": _BUTTON_STYLE_DANGER,
                "label": "Remove", "custom_id": f"radios:remove_confirm:{protocol}:{node_ref}",
            },
            {
                "type": _COMPONENT_BUTTON, "style": _BUTTON_STYLE_SECONDARY,
                "label": "Cancel", "custom_id": "radios:remove_cancel",
            },
        ],
    }]
    return _RESPONSE_UPDATE_MESSAGE, reply


async def _cmd_radios_remove_confirm(conn, body: dict, ingestor) -> tuple[int, dict]:
    """The confirm button -- re-authorizes ownership AGAIN, fresh,
    exactly as _cmd_radios_remove_select above already did (see this
    module's own docstring: every component interaction re-checks,
    never trusting an earlier step's own verification to still hold).
    Only THIS button actually removes anything; Cancel
    (_cmd_radios_remove_cancel below) never touches the database.
    """
    caller = _resolve_caller(conn, body)
    if caller is not None and caller.disabled:
        return _RESPONSE_UPDATE_MESSAGE, _disabled_account_message()
    if caller is None or caller.player_id is None:
        return _RESPONSE_UPDATE_MESSAGE, _unlinked_pointer_message()

    snowflake = _snowflake_from_body(body)
    if snowflake and _radios_rate_limiter.limited(
        snowflake,
        limit=settings.account_rotate_key_rate_limit_attempts,
        window=settings.account_rotate_key_rate_limit_window_seconds,
    ):
        return _RESPONSE_UPDATE_MESSAGE, _ephemeral("Too many attempts -- try again in a minute.")

    data = body.get("data") or {}
    parts = (data.get("custom_id") or "").split(":")
    protocol = parts[2] if len(parts) > 2 else ""
    node_ref = parts[3] if len(parts) > 3 else ""

    owned = conn.execute(
        "SELECT 1 FROM player_node WHERE protocol = ? AND node_ref = ? AND player_id = ?",
        (protocol, node_ref, caller.player_id),
    ).fetchone()
    if owned is None:
        return _RESPONSE_UPDATE_MESSAGE, _ephemeral(
            "That radio isn't yours (or it's already been removed)."
        )

    nodes_api.remove_node_for_player(caller.player_id, protocol, node_ref)
    return _RESPONSE_UPDATE_MESSAGE, _ephemeral("Removed.")


async def _cmd_radios_remove_cancel(conn, body: dict, ingestor) -> tuple[int, dict]:
    return _RESPONSE_UPDATE_MESSAGE, _ephemeral("Cancelled -- nothing was removed.")


def _cmd_setupcheck(conn, body: dict, ctx: CommandContext) -> dict:
    """Read-only: the caller's own setup diagnostics, from the exact
    same logic GET /api/account/checkin-health uses
    (app/account_api.py's _checkin_health_for_player()) -- never a
    second copy of that per-board diagnosis.

    Reads the check-in poller's own live directory snapshot off
    `ctx.app_state.checkin_poller` (see CommandContext's own docstring
    -- this is why this command declares needs_context=True), the
    SAME source and the SAME
    request.app.state.checkin_poller.directory_snapshot() call the
    website route makes, so this classifies an uncredited MeshCore
    contact exactly as precisely (resolving vs. not-in-directory vs.
    ambiguous -- see _checkin_contacts_status()'s own docstring) as
    that route does, rather than the empty directory a handler with no
    app.state at all would be stuck with. With nothing cached yet (no
    poller running, or ctx.app_state itself None -- e.g. a bare test
    app around just this router with no lifespan), this degrades to an
    empty directory, same as the website route does in that case: an
    honest "not_in_directory" rather than a 500. The credited/
    not-credited headline itself (mc_checkin_award, the actual thing a
    player cares about) does NOT depend on the directory at all, so
    this degrades gracefully rather than incorrectly either way.
    """
    caller = _resolve_caller(conn, body)
    if caller is not None and caller.disabled:
        return _disabled_account_message()
    if caller is None or caller.player_id is None:
        return _unlinked_pointer_message()

    app_state = ctx.app_state if ctx is not None else None
    poller = getattr(app_state, "checkin_poller", None) if app_state is not None else None
    directory = poller.directory_snapshot() if poller is not None else []

    result = account_api._checkin_health_for_player(conn, caller.player_id, directory)
    lines = []
    for protocol in (MC_PROTOCOL, MT_PROTOCOL):
        board = result["boards"].get(protocol)
        if board is None:
            continue
        lines.append(f"**{_BOARD_LABELS.get(protocol, protocol)}**: {board['summary']}")
    return _ephemeral("\n\n".join(lines))


# ---- /claimnode ---------------------------------------------------------
#
# Proves a specific radio is the caller's own and binds it, entirely
# through the SAME confirm logic GET/POST /api/checkin/confirm/* use
# (app/checkin_api.py's start_confirmation()/confirmation_status()/
# accept_confirmation()/cancel_confirmation(), each extracted from its
# own route for exactly this reuse -- see that module's own docstrings)
# -- never a second copy of the baseline scan, code issuance, or bind,
# and never an HTTP call to this app's own routes (see this module's
# own docstring's REUSE note).
#
# Unlike every command above, this one outlives its own interaction
# response: opening a window is answered immediately (or deferred, the
# same _HANDLER_BUDGET_SECONDS contract as any other command), but
# WATCHING for a candidate to appear runs in a background asyncio.Task
# (_claimnode_watch below) for up to the window's own five minutes,
# editing the ORIGINAL ephemeral message via PATCH
# .../webhooks/{app_id}/{token}/messages/@original -- this
# interaction's OWN token (embedded in the URL, reusing
# _patch_followup() verbatim), never app/discord_bot.py's bot token,
# for the exact same reason _patch_followup()'s own docstring already
# gives. Discord interaction tokens last 15 minutes, comfortably
# outliving the 5-minute window this ever needs to edit within.
#
# ONE ACTIVE CLAIM PER ACCOUNT, same as the website: _CLAIMNODE_TASKS
# below tracks at most one watcher per player_id, exactly mirroring
# start_confirmation()'s own "at most one open window per player,
# regardless of protocol" invariant (app/checkin_api.py: PRIMARY KEY
# (player_id) on both confirmation tables). A second /claimnode
# cancels this player's own still-running watcher and starts fresh --
# CANCEL-AND-RESTART, the same silent replacement
# start_confirmation()'s own docstring already documents ("opens (or
# REPLACES)") at the database layer -- rather than refusing outright;
# see _cmd_claimnode's own docstring for why that's the one this
# mirrors.
#
# PRIVACY: every message this section ever sends is ephemeral (see this
# module's own EPHEMERAL ALWAYS section) and shows candidates only for
# the name/code THIS caller supplied -- never another player's, and
# never a location: confirm_scan_all_connectors'/
# mt_confirm_scan_all_connectors' own output carries no lat/lon at all,
# so there is nothing here that could leak one even by accident.

# Matches frontend/account.js's own CHECKIN_CONFIRM_POLL_MS -- a poll
# cadence already proven safe against app/checkin_api.py's own 8-second
# upstream-scan throttle (_CONFIRM_SCAN_THROTTLE_SECONDS): every poll
# either lands inside the throttle (answered from the last scan, no new
# upstream request) or just outside it, never both stacking into a
# request storm.
_CLAIMNODE_POLL_SECONDS = 5.0

# player_id -> this player's own currently-running watcher task, if
# any -- at most one per player (see this section's own header comment
# above). Populated by _cmd_claimnode, read/cancelled by
# _cancel_claimnode_watch (a fresh /claimnode, an accept, an explicit
# Cancel click) and by cancel_all_claimnode_watches (app shutdown, see
# app/main.py's own lifespan). A task removes ITSELF once it finishes
# on its own (expiry, an unhandled exception) via the done-callback
# _register_claimnode_task attaches -- guarded by an identity check
# (`is t`) so a task that finishes just AFTER cancel-and-restart already
# overwrote this player's entry with a fresh task can never pop that
# fresh one out from under it.
_CLAIMNODE_TASKS: dict[int, asyncio.Task] = {}


def _register_claimnode_task(player_id: int, task: asyncio.Task) -> None:
    _CLAIMNODE_TASKS[player_id] = task

    def _cleanup(t: asyncio.Task, pid: int = player_id) -> None:
        if _CLAIMNODE_TASKS.get(pid) is t:
            _CLAIMNODE_TASKS.pop(pid, None)

    task.add_done_callback(_cleanup)


def _cancel_claimnode_watch(player_id: int) -> None:
    """Cancel this player's own /claimnode background watcher, if one is
    running -- safe to call with none running. Does not pop the dict
    entry itself: the cancelled task's own done-callback
    (_register_claimnode_task's `_cleanup`) does that, guarded by the
    identity check that keeps a cancel-and-restart race from evicting a
    freshly-registered replacement (see _CLAIMNODE_TASKS' own comment).
    """
    task = _CLAIMNODE_TASKS.get(player_id)
    if task is not None and not task.done():
        task.cancel()


async def cancel_all_claimnode_watches() -> None:
    """Cancel every in-flight /claimnode watcher -- called from
    app/main.py's own lifespan shutdown so a watcher never keeps
    running (and never keeps trying to PATCH a Discord message) past
    this process's own life. Safe to call with none running.
    """
    tasks = [t for t in _CLAIMNODE_TASKS.values() if not t.done()]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _claimnode_instructions_text(protocol: str, start_result: dict, expires_at: int) -> str:
    """The initial /claimnode reply text -- the SAME instructions
    frontend/account.js's own renderCheckinConfirmWaiting gives a
    website caller proving the same two protocols, plus when the watch
    expires as a Discord relative timestamp (<t:UNIX:R> -- Discord
    itself renders this in the reader's own local time zone, same as
    /nextnet's own timestamps above).
    """
    when = f"<t:{expires_at}:R>"
    if protocol == MT_PROTOCOL:
        code = start_result["code"]
        return (
            f"Send this exact code on any channel of your mesh now -- it can be "
            f"part of a longer sentence: `{code}`\n"
            f"Watching until {when}."
        )
    return (
        "Trigger an advert on that radio now -- most MeshCore devices send one "
        "from a long-press of the side button, or a \"Send Advert\" / "
        "\"Flood Advert\" menu item.\n"
        f"Watching until {when}."
    )


def _claimnode_expired_message() -> dict:
    return _ephemeral(
        "That confirmation window closed without hearing your radio. Try /claimnode again."
    )


def _claimnode_candidate_key(protocol: str, candidate: dict) -> str:
    """The identifier that tells two candidates apart, and lets
    _claimnode_watch below tell "the same candidate set as last poll"
    apart from "something changed" without re-sending an identical
    message every poll (see this command's own docstring: "update the
    message when candidates appear," not on every throttled repeat).
    """
    return candidate["public_key"] if protocol == MC_PROTOCOL else candidate["node_ref"]


def _claimnode_candidates_message(protocol: str, player_id: int, candidates: list[dict]) -> dict:
    """The "pick your radio" message once at least one live candidate
    has been heard. `candidates` is already filtered to drop anything
    already_claimed by SOMEONE ELSE (accepting one would just come back
    409) -- see _claimnode_watch below, the only caller.

    MeshCore: a string select listing each node (name plus a short key
    prefix, so two identically-named nodes stay distinguishable) --
    custom_id names the PENDING CONFIRMATION (this player_id), never
    trusted for identity on its own (see _cmd_claimnode_select below,
    which re-resolves the clicking user fresh); each option's `value`
    names the CANDIDATE (its public key) -- data, not authority, same
    as every other select in this module (see this module's own
    CUSTOM_ID SCHEME section).

    Meshtastic: one Confirm button per candidate node (normally just
    one -- the radio that actually sent the code) -- its own custom_id
    names BOTH the pending confirmation and the candidate node_ref,
    since a button (unlike a select) carries no separate `value` field
    of its own.
    """
    if protocol == MC_PROTOCOL:
        options = []
        for c in candidates[:25]:  # Discord's own cap on a select's option list
            key = c["public_key"]
            short_key = f"{key[:8]}…{key[-4:]}"
            options.append({"label": f"{c['name']} ({short_key})"[:100], "value": key})
        data = _ephemeral("We heard the following nodes advertising under that name. Pick yours:")
        data["components"] = [{
            "type": _COMPONENT_ACTION_ROW,
            "components": [{
                "type": _COMPONENT_STRING_SELECT,
                "custom_id": f"claimnode:select:{player_id}",
                "placeholder": "Which node is yours?",
                "options": options,
            }],
        }]
        return data

    data = _ephemeral("We heard that code from the following node. Confirm it's yours:")
    data["components"] = [
        {
            "type": _COMPONENT_ACTION_ROW,
            "components": [{
                "type": _COMPONENT_BUTTON, "style": _BUTTON_STYLE_PRIMARY,
                "label": f"Confirm {c.get('name') or c['node_ref']}"[:80],
                "custom_id": f"claimnode:confirm:{player_id}:{c['node_ref']}",
            }],
        }
        for c in candidates[:5]  # Discord's own cap: 5 action rows per message
    ]
    return data


async def _claimnode_watch(
    player_id: int, protocol: str, app_id: str, token: str, expires_at: int,
    *, http_client: httpx.AsyncClient | None = None,
) -> None:
    """Background watcher for one /claimnode confirmation window --
    polls the SAME status logic GET /api/checkin/confirm/status uses
    (app/checkin_api.py's confirmation_status()) every
    _CLAIMNODE_POLL_SECONDS, never longer than `expires_at` (the
    window's own five minutes), editing the original message only when
    the live candidate set actually CHANGES (_claimnode_candidate_key
    above) -- never on every poll, so a player watching the message
    doesn't see it flicker on an unchanged "still waiting."

    Ends one of three ways: candidates appear (message updated to the
    picker, this task's job is done -- the eventual select/button click
    is handled by _cmd_claimnode_select/_cmd_claimnode_confirm below,
    a SEPARATE interaction, and cancels this same task on success, see
    _cancel_claimnode_watch); the window closes with nothing heard
    (message updated to say so); or this task is cancelled from
    outside (a fresh /claimnode, an accept, an explicit Cancel click,
    or app shutdown -- cancel_all_claimnode_watches) -- in which case
    it exits without touching the message at all, since whichever
    caller cancelled it is the one responsible for the message's next
    state.

    An unhandled exception is logged and ends the watch with a short
    failure message, exactly like any other handler in this module
    never lets a stack trace reach Discord.
    """
    try:
        last_keys: frozenset[str] = frozenset()
        while True:
            now = int(time.time())
            remaining = expires_at - now
            if remaining <= 0:
                break
            await asyncio.sleep(min(_CLAIMNODE_POLL_SECONDS, remaining))

            status = await checkin_api.confirmation_status(player_id)
            if status.get("state") == "none":
                # Closed some other way that isn't this task's own
                # cancellation (which would have stopped this loop
                # already) -- the only way left is a natural expiry
                # confirmation_status() itself just noticed and cleared.
                break

            live = [c for c in (status.get("candidates") or []) if not c.get("already_claimed")]
            keys = frozenset(_claimnode_candidate_key(protocol, c) for c in live)
            if keys and keys != last_keys:
                last_keys = keys
                data = _claimnode_candidates_message(protocol, player_id, live)
                await _patch_followup(app_id, token, data, http_client=http_client)

        if not last_keys:
            await _patch_followup(app_id, token, _claimnode_expired_message(), http_client=http_client)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("discord interactions: /claimnode watcher failed for player %d", player_id)
        try:
            await _patch_followup(
                app_id, token,
                _ephemeral("Something went wrong watching for your radio. Try /claimnode again."),
                http_client=http_client,
            )
        except Exception:
            log.exception("discord interactions: /claimnode failure PATCH also failed")


async def _cmd_claimnode(conn, body: dict, ctx: CommandContext) -> dict:
    """/claimnode board:<MeshCore|Meshtastic> [name:<string>] -- proves a
    specific radio is the caller's own and binds it, entirely ephemeral
    (COMMANDS' own always_ephemeral=True for this command). Opens a
    confirmation window exactly as POST /api/checkin/confirm/start does
    (app/checkin_api.py's start_confirmation(), reused verbatim), then
    hands off to a background asyncio.Task (_claimnode_watch above)
    that watches for a candidate for up to the window's own five
    minutes, editing THIS interaction's own original message. Never
    calls this app's own HTTP routes over HTTP -- every call here is a
    plain Python function call into app/checkin_api.py.

    Caller must already be linked (_resolve_caller, the same fresh,
    never-trust-a-custom_id resolution every command in this section
    uses) -- unlinked points at /link or /join, same as every other
    account command; a disabled account is refused the same way too.

    `name` is required when `board` is MeshCore, and refused otherwise
    -- this is checked HERE (Discord's own option system has no
    "required if" between two options), before start_confirmation() is
    ever called, so a caller gets a plain, specific reason rather than
    that function's own generic "name is required" (worded for the
    website's JSON body, not a slash-command option).

    ONE ACTIVE CLAIM PER ACCOUNT, same as the website: see this
    section's own header comment for why a second /claimnode
    CANCELS-AND-RESTARTS rather than refusing -- start_confirmation()
    itself already silently replaces whatever window this player had
    open (any protocol), so this command's own watcher has to follow
    suit rather than leave two of them racing to edit the same old
    message.
    """
    caller = _resolve_caller(conn, body)
    if caller is not None and caller.disabled:
        return _disabled_account_message()
    if caller is None or caller.player_id is None:
        return _unlinked_pointer_message()

    snowflake = _snowflake_from_body(body)
    if snowflake and _claimnode_rate_limiter.limited(
        snowflake,
        limit=settings.account_rotate_key_rate_limit_attempts,
        window=settings.account_rotate_key_rate_limit_window_seconds,
    ):
        return _ephemeral("Too many attempts -- try again in a minute.")

    opts = _options_map(body)
    protocol = _BOARD_CHOICES.get(opts.get("board"))
    if protocol is None:
        return _ephemeral("Choose a board -- MeshCore or Meshtastic.")

    name = (opts.get("name") or "").strip() or None
    if protocol == MC_PROTOCOL and not name:
        return _ephemeral(
            "MeshCore needs a `name` -- the name your radio currently shows on the mesh."
        )
    if protocol == MT_PROTOCOL and name:
        return _ephemeral("Meshtastic doesn't take a `name` -- leave it out and run /claimnode again.")

    result, status = await checkin_api.start_confirmation(caller.player_id, protocol, name)
    if status >= 400:
        return _ephemeral(result.get("error", "Something went wrong."))

    # Cancel-and-restart -- see this function's own docstring.
    _cancel_claimnode_watch(caller.player_id)

    cfg = ctx.cfg if ctx is not None else None
    app_id = (cfg or {}).get("app_id") or ""
    token = body.get("token") or ""
    expires_at = result["expires_at"]
    if app_id and token:
        task = asyncio.create_task(
            _claimnode_watch(
                caller.player_id, protocol, app_id, token, expires_at,
                http_client=ctx.http_client if ctx is not None else None,
            )
        )
        _register_claimnode_task(caller.player_id, task)
    else:
        log.error("discord interactions: /claimnode with no app id or token -- cannot watch")

    data = _ephemeral(_claimnode_instructions_text(protocol, result, expires_at))
    data["components"] = [{
        "type": _COMPONENT_ACTION_ROW,
        "components": [{
            "type": _COMPONENT_BUTTON, "style": _BUTTON_STYLE_SECONDARY,
            "label": "Cancel", "custom_id": f"claimnode:cancel:{caller.player_id}",
        }],
    }]
    return data


def _claimnode_owner_from_custom_id(custom_id: str) -> int | None:
    """The player_id a claimnode custom_id names -- DATA, never
    authority (see this module's own CUSTOM_ID SCHEME section): every
    handler below re-resolves the CLICKING user fresh and compares
    against this, never trusting the custom_id's own claim about whose
    confirmation it is.
    """
    parts = custom_id.split(":")
    if len(parts) < 3:
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None


async def _cmd_claimnode_select(conn, body: dict, ingestor) -> tuple[int, dict]:
    """The MeshCore candidate select's own submit -- re-authorises the
    CLICKING user fresh, checks it against the custom_id's claimed
    player_id (never trusted on its own), then accepts through the SAME
    logic POST /api/checkin/confirm/accept uses
    (app/checkin_api.py's accept_confirmation(), which itself
    re-verifies the chosen key against a fresh scan -- see that
    function's own docstring).
    """
    caller = _resolve_caller(conn, body)
    if caller is not None and caller.disabled:
        return _RESPONSE_UPDATE_MESSAGE, _disabled_account_message()
    if caller is None or caller.player_id is None:
        return _RESPONSE_UPDATE_MESSAGE, _unlinked_pointer_message()

    data = body.get("data") or {}
    owner = _claimnode_owner_from_custom_id(data.get("custom_id") or "")
    if owner != caller.player_id:
        return _RESPONSE_UPDATE_MESSAGE, _ephemeral("That confirmation isn't yours.")

    values = data.get("values") or []
    public_key = values[0] if values else ""

    result, status = await checkin_api.accept_confirmation(caller.player_id, {"public_key": public_key})
    _cancel_claimnode_watch(caller.player_id)
    if status >= 400:
        return _RESPONSE_UPDATE_MESSAGE, _ephemeral(result.get("error", "Something went wrong."))
    return _RESPONSE_UPDATE_MESSAGE, _ephemeral(
        f"Bound to `{result['node_ref']}`. Check-ins from that node now count toward you."
    )


async def _cmd_claimnode_confirm(conn, body: dict, ingestor) -> tuple[int, dict]:
    """Meshtastic's own Confirm button -- same re-authorisation and
    shared accept_confirmation() call as _cmd_claimnode_select above,
    keyed on the node_ref the custom_id names (data, re-verified by
    accept_confirmation() itself against a fresh scan, never trusted on
    its own).
    """
    caller = _resolve_caller(conn, body)
    if caller is not None and caller.disabled:
        return _RESPONSE_UPDATE_MESSAGE, _disabled_account_message()
    if caller is None or caller.player_id is None:
        return _RESPONSE_UPDATE_MESSAGE, _unlinked_pointer_message()

    data = body.get("data") or {}
    custom_id = data.get("custom_id") or ""
    owner = _claimnode_owner_from_custom_id(custom_id)
    if owner != caller.player_id:
        return _RESPONSE_UPDATE_MESSAGE, _ephemeral("That confirmation isn't yours.")

    parts = custom_id.split(":")
    node_ref = parts[3] if len(parts) > 3 else ""

    result, status = await checkin_api.accept_confirmation(caller.player_id, {"node_ref": node_ref})
    _cancel_claimnode_watch(caller.player_id)
    if status >= 400:
        return _RESPONSE_UPDATE_MESSAGE, _ephemeral(result.get("error", "Something went wrong."))
    return _RESPONSE_UPDATE_MESSAGE, _ephemeral(
        f"Bound to `{result['node_ref']}`. Check-ins from that node now count toward you."
    )


async def _cmd_claimnode_cancel(conn, body: dict, ingestor) -> tuple[int, dict]:
    """Cancel button -- same logic DELETE /api/checkin/confirm uses
    (app/checkin_api.py's cancel_confirmation()), and stops this
    player's own background watcher (_cancel_claimnode_watch) so it
    never fires a stale edit after the player has already backed out.
    """
    caller = _resolve_caller(conn, body)
    if caller is not None and caller.disabled:
        return _RESPONSE_UPDATE_MESSAGE, _disabled_account_message()
    if caller is None or caller.player_id is None:
        return _RESPONSE_UPDATE_MESSAGE, _unlinked_pointer_message()

    data = body.get("data") or {}
    owner = _claimnode_owner_from_custom_id(data.get("custom_id") or "")
    if owner != caller.player_id:
        return _RESPONSE_UPDATE_MESSAGE, _ephemeral("That confirmation isn't yours.")

    checkin_api.cancel_confirmation(caller.player_id)
    _cancel_claimnode_watch(caller.player_id)
    return _RESPONSE_UPDATE_MESSAGE, _ephemeral("Cancelled.")


_COMPONENT_HANDLERS: dict[str, Callable] = {
    "radios:add_open": _cmd_radios_add_open,
    "radios:remove_select": _cmd_radios_remove_select,
    "radios:remove_confirm": _cmd_radios_remove_confirm,
    "radios:remove_cancel": _cmd_radios_remove_cancel,
    "claimnode:select": _cmd_claimnode_select,
    "claimnode:confirm": _cmd_claimnode_confirm,
    "claimnode:cancel": _cmd_claimnode_cancel,
}

_MODAL_HANDLERS: dict[str, Callable] = {
    "link:modal": _cmd_link_modal_submit,
    "join:modal": _cmd_join_modal_submit,
    "radios:add_submit": _cmd_radios_add_submit,
}


async def _run_interactive(handler: Callable, body: dict, ingestor) -> tuple[int, dict]:
    """Runs one component/modal handler against a fresh connection --
    unlike _run_command() below (which offloads a SYNC handler to a
    thread via asyncio.to_thread), every handler reached through this
    function is itself `async def` and does its own awaiting (Discord
    role sync, the key ingestor's own authenticate()) -- the same way
    every OTHER route in this app mixes plain sqlite3 calls directly
    into an async request handler with no to_thread at all
    (app/account_api.py, app/join_api.py, ...). These handlers simply
    follow that same, already-established convention instead of
    _run_command()'s.
    """
    conn = connect()
    try:
        return await handler(conn, body, ingestor)
    finally:
        conn.close()


async def _dispatch_interactive(
    body: dict, cfg: dict, handler: Callable | None, *,
    ingestor=None, http_client: httpx.AsyncClient | None = None,
) -> dict:
    """Shared budget/defer/exception wrapper for MESSAGE_COMPONENT and
    MODAL_SUBMIT interactions -- the same 2-second-budget/deferral
    contract _dispatch() below applies to application commands, adapted
    for a handler that picks its OWN success response TYPE (7,
    UPDATE_MESSAGE, for most of this section's component clicks; 4 for
    a modal's own follow-up message; 9 for a button that itself opens
    another modal) instead of always answering with type 4.

    Every entry point reached through here is always-ephemeral (this
    module's own docstring, EPHEMERAL ALWAYS), so a deferred ack always
    carries the ephemeral flag; it always uses the plain type-5
    "loading" ack, never Discord's component-only type 6
    (DEFERRED_UPDATE_MESSAGE, whose later PATCH must edit the ORIGINAL
    message) -- every handler in this section does no more than a
    couple of local sqlite3 queries before answering, so this should
    never actually trip in practice.
    """
    if handler is None:
        return _response(_RESPONSE_CHANNEL_MESSAGE, _ephemeral("Unknown interaction."))

    task = asyncio.create_task(_run_interactive(handler, body, ingestor))
    try:
        response_type, data = await asyncio.wait_for(asyncio.shield(task), timeout=_HANDLER_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        async def _finish_and_patch() -> None:
            try:
                _rt, result_data = await task
            except Exception:
                log.exception("discord interactions: deferred component/modal handler failed")
                result_data = _ephemeral("Something went wrong.")
            token = body.get("token") or ""
            app_id = cfg.get("app_id") or ""
            if not app_id or not token:
                log.error("discord interactions: cannot deliver deferred response -- missing app id or token")
                return
            await _patch_followup(app_id, token, result_data, http_client=http_client)

        followup = asyncio.create_task(_finish_and_patch())
        for t in (task, followup):
            _BACKGROUND_TASKS.add(t)
            t.add_done_callback(_BACKGROUND_TASKS.discard)
        return _response(
            _RESPONSE_DEFERRED_CHANNEL_MESSAGE,
            {"allowed_mentions": _ALLOWED_MENTIONS, "flags": _FLAG_EPHEMERAL},
        )
    except Exception:
        log.exception("discord interactions: component/modal handler failed")
        return _response(_RESPONSE_CHANNEL_MESSAGE, _ephemeral("Something went wrong."))

    return _response(response_type, data)


# ---- the one registry -----------------------------------------------------
#
# Every command's Discord definition AND handler, in one place, so
# app/discord_bot.py's register_commands() (what Discord is told exists,
# via each Command's own .definition()) and _dispatch() below (what
# actually runs, via .handler) read off the exact same list and can
# never disagree about what commands this bot has.


class CommandContext(NamedTuple):
    """What a command handler gets beyond (conn, body) when its own
    Command entry sets needs_context=True -- app_state (this process's
    live app.state, the same object every OTHER route in this app
    already reads via request.app.state, e.g. app/account_api.py's own
    request.app.state.checkin_poller) and http_client (the same
    injectable httpx.AsyncClient seam _patch_followup()/_dispatch()
    already thread through for tests). A command handler's own shape is
    deliberately just (conn, body) -- see this module's own docstring
    -- since a slash-command interaction carries no Request at all;
    this is the narrow, explicit substitute a handler declares it
    needs, rather than an open door to a Request this interaction was
    never given one of. Most commands need neither field at all --
    today only /setupcheck (app_state, for the live check-in directory)
    and /claimnode (both, to spawn its background watcher and edit the
    original message via the interaction token) do.
    """
    app_state: object | None
    http_client: httpx.AsyncClient | None
    # discord_config, the same dict _dispatch() itself already has on
    # hand (loaded once, up front, by the route -- see
    # discord_interactions_endpoint()) -- /claimnode's own watcher reads
    # cfg["app_id"] off this rather than the interaction body's own
    # `application_id` field, the same source _finish_deferred() above
    # already uses for its own follow-up PATCH, so app_id never has two
    # different sources of truth within this module.
    cfg: dict | None


class Command(NamedTuple):
    name: str
    description: str
    options: list[dict]
    handler: Callable
    always_ephemeral: bool
    # See CommandContext's own docstring. Defaulted so every existing
    # (conn, body) -> dict handler above is unaffected.
    needs_context: bool = False
    # True for a handler that is itself `async def` and does its own
    # awaiting (currently only /claimnode -- opening a confirmation
    # window awaits the same connector scan app/checkin_api.py's own
    # route already awaits, and spawning its background watcher needs a
    # running event loop under it, not a bare to_thread() call) --
    # every other command handler is a plain sync function run off the
    # loop via asyncio.to_thread (see _run_command below).
    is_async: bool = False

    def definition(self) -> dict:
        """The Discord-facing command definition -- name, description,
        options ONLY, never `handler`/`always_ephemeral`/`needs_context`/
        `is_async`, which mean nothing to Discord's own PUT
        .../commands body (see app/discord_bot.py's register_commands()).
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
    Command(
        name="link",
        description="Link an existing MeshWars API key to your Discord account",
        options=[],
        handler=_cmd_link,
        always_ephemeral=True,
    ),
    Command(
        name="join",
        description="Create a new MeshWars player linked to your Discord account",
        options=[],
        handler=_cmd_join,
        always_ephemeral=True,
    ),
    Command(
        name="radios",
        description="Manage your own MeshWars radios (only you can see this)",
        options=[],
        handler=_cmd_radios,
        always_ephemeral=True,
    ),
    Command(
        name="setupcheck",
        description="Check why your check-ins may not be counting (only you can see this)",
        options=[],
        handler=_cmd_setupcheck,
        always_ephemeral=True,
        needs_context=True,
    ),
    Command(
        name="claimnode",
        description="Prove a specific radio is yours and bind it to your account",
        options=[_CLAIMNODE_BOARD_OPTION, _CLAIMNODE_NAME_OPTION],
        handler=_cmd_claimnode,
        always_ephemeral=True,
        needs_context=True,
        is_async=True,
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


async def _run_command(entry: Command, body: dict, ctx: CommandContext) -> dict:
    """Run one command's handler against a fresh connection.

    Two shapes: a plain SYNC handler (every command except /claimnode)
    runs off the event loop via asyncio.to_thread, same as before this
    module had any async command handler at all -- every handler above
    does blocking sqlite3 work, same as the rest of this codebase's
    request handlers, and this is the one place that gives it a thread
    instead of stalling the loop for however long the query takes. An
    ASYNC one (entry.is_async -- see that field's own docstring) is
    awaited directly instead, the same "mix plain sqlite3 calls into an
    async handler with no to_thread" convention _run_interactive() above
    already uses for /link and /join's own modal submits.

    entry.needs_context (see CommandContext's own docstring) appends
    `ctx` as a third positional argument; every other handler keeps the
    plain (conn, body) shape untouched.
    """
    conn = connect()
    try:
        args = (conn, body, ctx) if entry.needs_context else (conn, body)
        if entry.is_async:
            return await entry.handler(*args)
        return await asyncio.to_thread(entry.handler, *args)
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


async def _dispatch(
    body: dict, cfg: dict, *, app_state=None, http_client: httpx.AsyncClient | None = None,
) -> dict:
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

    `app_state` is this process's request.app.state (see
    CommandContext's own docstring) -- the route below passes it
    through; a bare call (as most of this module's own tests make)
    leaves it None, which every handler that reads it already treats
    the same as "nothing cached yet."
    """
    data = body.get("data") or {}
    name = data.get("name")
    entry = _COMMANDS_BY_NAME.get(name)
    if entry is None:
        return _response(_RESPONSE_CHANNEL_MESSAGE, _ephemeral("Unknown command."))

    ctx = CommandContext(app_state=app_state, http_client=http_client, cfg=cfg)
    task = asyncio.create_task(_run_command(entry, body, ctx))
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

    if "type" in result:
        # A handler that answers with its own full response envelope
        # (a MODAL, type 9 -- see _modal_response()'s own docstring,
        # used by /link and /join above) rather than a plain
        # message-data dict -- used verbatim instead of being wrapped
        # as a type-4 message. A plain _data()/_ephemeral() dict never
        # carries a top-level "type" key of its own, so this is
        # unambiguous and every existing command above is unaffected.
        return result
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
        return JSONResponse(await _dispatch(body, cfg, app_state=request.app.state))
    if itype in (_TYPE_MESSAGE_COMPONENT, _TYPE_MODAL_SUBMIT):
        # Same key-authenticated surface /link's modal submit needs
        # (app/account_api.py's own POST /api/account/link-key
        # authenticates a pasted key the identical way) -- threaded
        # through here, never reached for via a second request this
        # module would have to make itself. Absent on a deployment that
        # never wires it up at all (app/main.py's lifespan always
        # constructs one -- see app/nodes_api.py's own module docstring
        # -- but a bare test app around just this router, as this
        # module's own tests build, may not).
        ingestor = getattr(request.app.state, "mc_ingestor", None)
        custom_id = ((body.get("data") or {}).get("custom_id")) or ""
        action = _custom_id_action(custom_id)
        registry = _COMPONENT_HANDLERS if itype == _TYPE_MESSAGE_COMPONENT else _MODAL_HANDLERS
        handler = registry.get(action)
        return JSONResponse(
            await _dispatch_interactive(body, cfg, handler, ingestor=ingestor)
        )
    return Response(status_code=400)
