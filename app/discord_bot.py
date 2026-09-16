"""Discord role sync for MeshWars teams -- "Herald," the bot this app
talks to Discord's own REST API with (app/discord_notify.py, by
contrast, only ever POSTs to a webhook and never authenticates as a
bot at all -- these are two entirely separate Discord integrations
that happen to share one server).

WHAT THIS DOES: a MeshWars player who has (a) linked a Discord account
(app/oauth_api.py's Discord provider -- account_identity.subject is
that Discord user's own snowflake id) and (b) is a member of the
configured guild gets the Discord role that names their MeshWars team,
and no other team's role. This bot is not a chat bot -- it never reads
or sends a message a human wrote. Its role in the guild holds Manage
Roles, Manage Channels, View Channels, Send Messages, Read Message
History, and Pin Messages. Manage Roles is what the role sync above
actually uses; Manage Channels is what ensure_team_channels() (below)
uses to create/repair the private team channels and their permission
overwrites; View Channels, Send Messages, Read Message History, and
Pin Messages are Discord requirements for a bot to exist in a server,
see its own channel list, and post into a channel it manages at all --
this module never posts a message of its own into any of them.

PRIVACY: a team role reveals a Discord user's MeshWars TEAM -- never
their in-game player name, never a location, never anything about
where they've been. Linking Discord at all is opt-in, entirely the
player's own choice (app/oauth_api.py's case 2/3, or POST
/api/account/pending/link), and this module only ever acts on an
identity that link already produced. This is compatible with the rule
app/public_api.py:38 states for the read API's own two-tier privacy
model ("identity can be public, location can be public, the link
between them requires a session") -- a team role is the "identity"
tier (which team, publicly visible to anyone in the Discord server,
same as a player's own choice to show a coloured dot next to their
name), never the "location" tier, and the account-linking session that
produced the underlying account_identity row is exactly the "requires
a session" gate that rule already describes.

TEAM CHANNELS: ensure_team_channels() (below, run right after
ensure_team_roles() succeeds) is a second, later feature layered on top
of the same role: a private text channel per team, visible only to
players holding that team's role. This is still identity-tier, same as
the role itself -- a channel's membership list is exactly "everyone
holding this team's role," nothing about location.

ADOPTION, NOT JUST CREATION: the first version of this feature only
ever matched a category named EXACTLY discord_config.team_category_name
and a channel named EXACTLY the team name lowercased -- fine for a
guild this bot set up from nothing, wrong for the common case of an
owner who already ran their server by hand. One real guild had
`[Team Chat]` (not "Teams") holding `red🟥`, `orange🟧`, `yellow🟨`,
`blue🟦`, `purple🟪`, `pink🩷` (no green at all, and every name carried
an emoji this bot's exact-string match could never see past) -- the old
code found none of that, decided nothing existed yet, and created a
second, empty, parallel "Teams" category with all seven channels
duplicated. _normalize_channel_name() below (lowercase, strip
everything but [a-z0-9]) is the fix: `red🟥` and `Team-Red!` both
normalize to a name this bot CAN match against a team's own name or
discord_config.team_category_name, so adopting what an operator already
built is the normal path through _ensure_category()/
_ensure_team_channel() below, and creating a brand new channel is only
the last-resort fallback when nothing in the category matches at all.
An operator's own naming (emoji included) is never touched -- see
_ensure_team_channel()'s own docstring for why an adopted channel is
never renamed, and for the `ambiguous` bucket that refuses to guess (and
touches nothing) when two channels in the category normalize to the
same team name.

CONFIG: DISCORD_BOT_TOKEN (app/config.py's discord_bot_token) is a
SECRET, held in the environment only -- never the database, never
returned by any route, never logged -- the exact same treatment
app/config.py's account_totp_encryption_key already gets (see that
setting's own comment): a stolen database file alone must never be
enough to act as this bot. Everything else -- guild_id, roles_enabled,
and the discovered team->role id mapping (discord_team_role) -- is
non-secret, DB-backed, and admin-editable through
app/admin_ops.py's /api/admin/discord, the exact same "runtime config
lives in the DB, read fresh every time, never cached" shape
app/discord_notify.py's own load_discord_config() already established
(this module imports that same function rather than inventing a
second reader for the same row).

GATING: every entry point below (sync_member, ensure_team_roles,
reconcile_all) starts with _roles_ready(), which is False unless ALL
THREE of roles_enabled=1 (discord_config), a non-empty
DISCORD_BOT_TOKEN, and a non-empty guild_id are true. A fresh install,
or one that has only ever configured the separate webhook
announcements feed, does nothing here at all -- no outbound calls, no
discord_team_role writes, nothing to disable that doesn't already
default to off.

FIRE-AND-FORGET: every call site that triggers a sync from an HTTP
route (POST /api/account/link-key, the Discord OAuth callback cases,
POST /api/account/pending/link, admin_set_team, switch_team) does so
through sync_member_safe(), which never raises -- the exact same
contract app/account_api.py's _notify_security() already applies to a
security-notice email send: a Discord outage must never break, delay,
or roll back the account/team action that triggered it. Every one of
those call sites invokes sync_member_safe() AFTER its own write
transaction has already committed (WriteSession's __aexit__, or a
route's own manual COMMIT), never from inside one -- the same "HTTP
work happens outside any WriteSession" rule app/discord_notify.py's
outbox drain loop already follows, for the same reason: a slow or
hung Discord call must never hold this process's single global write
lock.

RECONCILE: reconcile_all() is a slow full sweep over every player with
a linked Discord identity, meant to catch drift the event-driven paths
above can miss (someone joins the Discord server after linking; an
operator hand-edits roles in Discord itself). maybe_reconcile_roles()
is the interval-gated wrapper app/discord_notify.py's run_forever()
calls once per its own poll cycle (every
discord_outbox_poll_interval_seconds, 30s by default) -- see that
function's own docstring for why this rides the EXISTING background
loop with its own, much longer (_RECONCILE_INTERVAL_SECONDS, 15
minutes) gate, rather than this module starting a second
asyncio.create_task of its own.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

from .config import settings
from .db import WriteSession, connect
from .discord_notify import _TEAM_COLORS, load_discord_config

log = logging.getLogger("discord_bot")

# Discord's own bot API, v10 -- entirely separate from the webhook URLs
# app/discord_notify.py posts to (those carry their own auth in the
# URL path; this base is authenticated per-request via the
# "Authorization: Bot <token>" header _request() below sets).
_API_BASE = "https://discord.com/api/v10"

# Same per-call budget app/discord_notify.py's _POST_TIMEOUT_SECONDS
# uses for its own single outbound webhook POST -- generous for a
# small JSON request/response to Discord's own API.
_REQUEST_TIMEOUT_SECONDS = 10.0

# Discord's edge rejects a default/missing User-Agent on some routes
# with a bare 403 that gives no other clue what was wrong -- this is
# the documented fix (discord.dev's own API reference asks for a
# descriptive UA naming the application and a contact URL).
_USER_AGENT = "DiscordBot (https://meshwars.com, 1.0)"

# Same truncation budget app/discord_notify.py's _MAX_ERROR_BODY_CHARS
# uses for the identical reason: Discord's own error body names the
# exact field/permission it objected to and holds no credential, so a
# snippet of it is safe (and useful) to fold into a raised message --
# but only a bounded snippet, never the whole thing.
_MAX_ERROR_BODY_CHARS = 200

# Team roles grant nothing -- see ensure_team_roles()'s own docstring.
# Discord's own API takes permissions as a string-encoded bitfield;
# "0" is the documented way to say "no permissions at all."
_TEAM_ROLE_PERMISSIONS = "0"

# Discord permission bits (API v10, discord.dev's own "Permissions"
# reference) that ensure_team_channels() below actually sets on a
# channel or category's permission overwrites. Named constants rather
# than the raw literals so the overwrite-building code below reads as
# what it means, not as three magic numbers OR'd together.
_PERM_MANAGE_CHANNELS = 1 << 4
_PERM_VIEW_CHANNEL = 1 << 10
_PERM_SEND_MESSAGES = 1 << 11
_PERM_READ_MESSAGE_HISTORY = 1 << 16

# What a team's OWN role is allowed inside its channel -- see enough to
# read and write there, nothing else (no Manage Channels, no touching
# permissions).
_TEAM_CHANNEL_MEMBER_PERMS = _PERM_VIEW_CHANNEL | _PERM_SEND_MESSAGES | _PERM_READ_MESSAGE_HISTORY

# What THIS BOT'S OWN user overwrite grants it in every team channel and
# the category, on top of the member perms above: Manage Channels, so it
# can keep editing the channel's overwrites on every later run. Without
# this, the very first @everyone-deny overwrite this bot writes would
# lock itself out of the channel it just created, with no way back in
# except a human re-inviting it in Discord's own UI -- see
# _bot_allow_overwrite()'s own docstring below.
_BOT_CHANNEL_PERMS = _TEAM_CHANNEL_MEMBER_PERMS | _PERM_MANAGE_CHANNELS

# Discord's own permission-overwrite `type`: 0 for a role, 1 for a
# guild member (discord.dev's "Overwrite Object"). Named here so the
# overwrite-building helpers below never repeat a bare 0/1.
_OVERWRITE_TYPE_ROLE = 0
_OVERWRITE_TYPE_MEMBER = 1

# Discord's own channel `type`: 4 is a category, 0 is a plain text
# channel (discord.dev's "Channel Types"). ensure_team_channels() only
# ever creates these two kinds.
_CHANNEL_TYPE_CATEGORY = 4
_CHANNEL_TYPE_TEXT = 0

# How often maybe_reconcile_roles() actually runs reconcile_all(), out
# of every call app/discord_notify.py's run_forever() makes to it (once
# per its own 30s poll cycle). 15 minutes, NOT that 30s interval: a
# full reconcile walks every player with a linked Discord identity, one
# guild-member GET plus up to a handful of role PUT/DELETEs each --
# real work against Discord's own rate limits, not a cheap local table
# scan the way the outbox drain's own due-check is. Nothing about role
# drift needs sub-minute latency the way a freshly queued announcement
# does; the event-driven paths (sync_member_safe, called on link and on
# every team change) already handle the common case immediately, and
# this sweep exists only to catch what those miss.
_RECONCILE_INTERVAL_SECONDS = 15 * 60


class DiscordAPIError(Exception):
    """Raised by _request()/_check_ok() below on any failure talking to
    Discord's bot API -- mirrors app/discord_notify.py's
    DiscordSendError exactly, including the same never-str(e)-on-a-
    transport-failure rule: httpx's own exception text embeds the
    request, and every request here carries this deployment's bot
    token in its Authorization header. A non-2xx HTTP response is
    different -- Discord's OWN response body describes what was wrong
    with the request this app sent, not a credential, so a truncated
    snippet of it is safe to include (see _check_ok() below).

    Every caller here treats this as "this one sync attempt failed,"
    never as a reason to crash a background loop or a request handler
    -- see sync_member_safe()'s own docstring for the fire-and-forget
    boundary that stops one of these from ever reaching an HTTP route.
    """


def _roles_ready(cfg: dict) -> bool:
    """True only when role sync is actually configured to run:
    discord_config.roles_enabled=1 AND a bot token AND a guild id are
    all present. Every public entry point below (sync_member,
    ensure_team_roles, reconcile_all) checks this FIRST and no-ops
    (returns a small {"ok": False, ...} dict, makes no outbound call,
    writes nothing) when it is False -- so a deployment that has never
    touched this feature, or has deliberately turned it off, behaves
    exactly as if this module did not exist. `cfg` is an
    already-loaded load_discord_config() dict, same "caller already
    has one loaded this cycle" shape load_discord_channels()'s own
    callers use in app/discord_notify.py.
    """
    return bool(cfg.get("roles_enabled")) and bool(settings.discord_bot_token) and bool(cfg.get("guild_id"))


def _channels_ready(cfg: dict) -> bool:
    """True only when private team channels are actually configured to
    run: discord_config.team_channels_enabled=1 ON TOP OF every
    _roles_ready() gate (a bot token, a guild id, roles_enabled) --
    channels are layered on team roles (a channel's own permission
    overwrite names a team's role id), so this feature can never be
    "on" while role sync itself is off or unconfigured. Checked FIRST by
    ensure_team_channels(), same no-outbound-call, no-op-dict contract
    _roles_ready() itself documents.
    """
    return bool(cfg.get("team_channels_enabled")) and _roles_ready(cfg)


def _parse_retry_after(resp: httpx.Response) -> float:
    """Discord's documented 429 shape carries `retry_after` (seconds,
    a float) in the JSON body -- the header of the same name exists
    too, but the body is Discord's own bot-API-specific value and is
    preferred here. Falls back to the header, then to a flat 1.0s, if
    the body isn't the shape expected -- this must never raise, since
    it runs inside a rate-limit path that is already the "something
    went wrong" branch.
    """
    try:
        body = resp.json()
        retry_after = body.get("retry_after")
        if isinstance(retry_after, (int, float)):
            return float(retry_after)
    except Exception:
        pass
    header = resp.headers.get("Retry-After")
    try:
        return float(header)
    except (TypeError, ValueError):
        return 1.0


async def _request(
    method: str,
    path: str,
    *,
    json_body: dict | list | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> httpx.Response:
    """One authenticated call to Discord's bot API, returning the raw
    response for the caller to interpret (status codes carry meaning
    here that a single "raise on anything but 2xx" helper would lose --
    a 404 on GET .../members/{id} means "not in this server," not a
    failure -- see sync_member() below). Raises DiscordAPIError only
    for a transport-level failure (timeout, connection error) or a 429
    that is still a 429 after honouring `retry_after` once (Discord's
    own doc: a well-behaved client backs off once and tries again;
    hitting it twice in a row means something is generating far more
    traffic than one player's sync ever should, and this gives up for
    THIS call rather than looping against a live rate limit).

    `http_client` is accepted purely so tests can hand this an
    httpx.AsyncClient wired to an httpx.MockTransport, the same
    injectable-client shape app/discord_notify.py's _post() and
    app/oauth.py's exchange_code() already use for their own outbound
    calls -- every real caller leaves it None and a short-lived client
    is opened and closed around this one request.
    """
    headers = {
        "Authorization": f"Bot {settings.discord_bot_token}",
        "User-Agent": _USER_AGENT,
    }
    client = http_client
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS)
    try:
        async def _one_call() -> httpx.Response:
            try:
                return await client.request(
                    method, f"{_API_BASE}{path}", json=json_body, headers=headers
                )
            except httpx.TimeoutException as e:
                raise DiscordAPIError("discord api request timed out") from e
            except httpx.HTTPError as e:
                # Deliberately not str(e) -- see this module's own
                # docstring and DiscordAPIError's: httpx's own
                # exception text embeds the request, headers included,
                # and the Authorization header carries the bot token.
                raise DiscordAPIError(f"discord api request failed ({type(e).__name__})") from e

        resp = await _one_call()
        if resp.status_code == 429:
            # Honour Discord's own back-off exactly once, then retry --
            # see this function's own docstring for why a second 429
            # gives up rather than looping.
            await asyncio.sleep(_parse_retry_after(resp))
            resp = await _one_call()
            if resp.status_code == 429:
                raise DiscordAPIError("discord api rate limited twice in a row, giving up this cycle")
        return resp
    finally:
        if owns_client:
            await client.aclose()


def _check_ok(resp: httpx.Response, action: str) -> None:
    """Raises DiscordAPIError on any non-2xx response, with a truncated
    snippet of Discord's own response body (safe -- see this module's
    own docstring and DiscordAPIError's). Callers that treat a specific
    status specially (sync_member()'s 404-means-not-a-member) check
    that status BEFORE calling this, so it never fires for a case that
    isn't actually a failure.
    """
    if 200 <= resp.status_code < 300:
        return
    snippet = (resp.text or "").strip()[:_MAX_ERROR_BODY_CHARS]
    detail = f": {snippet}" if snippet else ""
    raise DiscordAPIError(f"discord api {action} returned HTTP {resp.status_code}{detail}")


def _check_channel_ok(resp: httpx.Response, action: str, likely_missing_permission: str) -> None:
    """Same non-2xx contract as _check_ok() above, except a 403
    specifically is raised with `likely_missing_permission` named in
    the message rather than whatever (often unhelpfully generic)
    "Missing Permissions" text Discord's own body carries -- used only
    by ensure_team_channels() below and its helpers, where a 403 has
    exactly two likely causes (Manage Channels missing, for a create;
    Manage Roles missing, for a permission-overwrite edit) and naming
    the right one saves an operator a guessing game in Discord's own
    role list. Every other status is unchanged, delegated straight to
    _check_ok().
    """
    if resp.status_code == 403:
        raise DiscordAPIError(
            f'discord api {action} returned HTTP 403 -- Herald is likely missing the '
            f'"{likely_missing_permission}" permission in this guild'
        )
    _check_ok(resp, action)


async def _list_guild_roles(guild_id: str, *, http_client: httpx.AsyncClient | None = None) -> list[dict]:
    resp = await _request("GET", f"/guilds/{guild_id}/roles", http_client=http_client)
    _check_ok(resp, "list roles")
    return resp.json()


async def _create_guild_role(
    guild_id: str,
    *,
    name: str,
    color: int,
    hoist: bool,
    mentionable: bool,
    permissions: str,
    http_client: httpx.AsyncClient | None = None,
) -> dict:
    resp = await _request(
        "POST",
        f"/guilds/{guild_id}/roles",
        json_body={
            "name": name,
            "color": color,
            "hoist": hoist,
            "mentionable": mentionable,
            "permissions": permissions,
        },
        http_client=http_client,
    )
    _check_ok(resp, "create role")
    return resp.json()


# ---- ensure_team_channels()'s own REST wrappers ---------------------------
#
# Same shape as _list_guild_roles()/_create_guild_role() above (a thin
# wrapper over _request()+_check_ok()/_check_channel_ok()), kept
# separate from the role ones above rather than generalized into one
# shared helper: channels and roles are different Discord resources with
# different failure-permission mappings, and the extra indirection a
# shared helper would need buys nothing here.


async def _get_bot_user(*, http_client: httpx.AsyncClient | None = None) -> dict:
    """GET /users/@me -- this bot's own user object, id included. Never
    permission-gated (a bot can always read its own identity), so this
    goes through the plain _check_ok(), not _check_channel_ok().
    """
    resp = await _request("GET", "/users/@me", http_client=http_client)
    _check_ok(resp, "get bot user")
    return resp.json()


# This bot's own Discord user id, cached process-local once fetched --
# see _cached_bot_user_id() below for why (a bot's snowflake never
# changes for a given token, so re-fetching it on every
# ensure_team_channels() run would be a wasted call every single time).
_bot_user_id: str | None = None


async def _cached_bot_user_id(*, http_client: httpx.AsyncClient | None = None) -> str:
    """The cached _bot_user_id above, fetching it via _get_bot_user()
    exactly once per process lifetime (or per test, which monkeypatches
    this module's _bot_user_id back to None between runs -- see
    tests/test_discord_bot.py's own fixture). Every overwrite
    ensure_team_channels() writes needs this id (see
    _bot_allow_overwrite() below for why the bot must always hold its
    own explicit allow), so this is called once per ensure_team_channels()
    run and the result threaded through, never re-fetched per channel.
    """
    global _bot_user_id
    if _bot_user_id is None:
        me = await _get_bot_user(http_client=http_client)
        _bot_user_id = me["id"]
    return _bot_user_id


async def _list_guild_channels(guild_id: str, *, http_client: httpx.AsyncClient | None = None) -> list[dict]:
    resp = await _request("GET", f"/guilds/{guild_id}/channels", http_client=http_client)
    _check_ok(resp, "list channels")
    return resp.json()


async def _create_guild_channel(
    guild_id: str,
    *,
    name: str,
    channel_type: int,
    parent_id: str | None,
    permission_overwrites: list[dict],
    http_client: httpx.AsyncClient | None = None,
) -> dict:
    """POST /guilds/{guild_id}/channels -- `permission_overwrites` is set
    RIGHT HERE, at creation, so a brand-new category or channel is never
    even briefly public between being created and a follow-up PATCH (see
    ensure_team_channels()'s own docstring for why the overwrite list is
    otherwise reasserted with a separate PATCH only for a channel this
    function FOUND already existing, not one it just made).
    """
    body: dict = {"name": name, "type": channel_type, "permission_overwrites": permission_overwrites}
    if parent_id is not None:
        body["parent_id"] = parent_id
    resp = await _request("POST", f"/guilds/{guild_id}/channels", json_body=body, http_client=http_client)
    _check_channel_ok(resp, f"create channel {name!r}", "Manage Channels")
    return resp.json()


async def _set_channel_overwrites(
    channel_id: str, permission_overwrites: list[dict], *, http_client: httpx.AsyncClient | None = None
) -> None:
    """PATCH /channels/{channel_id} with a full `permission_overwrites`
    array -- Discord replaces the channel's ENTIRE overwrite list with
    exactly what's given here, which is exactly what "re-assert the full
    list on every run, so a hand edit is repaired" (ensure_team_channels()'s
    own docstring) needs: a single call that cannot leave a stray
    overwrite an operator added by hand still in place.
    """
    resp = await _request(
        "PATCH", f"/channels/{channel_id}",
        json_body={"permission_overwrites": permission_overwrites},
        http_client=http_client,
    )
    _check_channel_ok(resp, f"set permission overwrites on channel {channel_id}", "Manage Roles")


def _everyone_deny_overwrite(guild_id: str) -> dict:
    """@everyone -- Discord's documented convention is that the
    @everyone role's overwrite id IS the guild's own id -- denied
    VIEW_CHANNEL. Present on the category AND every team channel (never
    just the category): see ensure_team_channels()'s own docstring for
    why each channel repeats this rather than relying only on the
    category's copy (a channel dragged out of its category must stay
    private on its own).
    """
    return {"id": guild_id, "type": _OVERWRITE_TYPE_ROLE, "allow": "0", "deny": str(_PERM_VIEW_CHANNEL)}


def _team_role_allow_overwrite(role_id: str) -> dict:
    """The team's own role -- allowed to view, post, and read history in
    its channel, nothing more (no Manage anything -- a team channel is
    theirs to talk in, not to administer).
    """
    return {
        "id": role_id, "type": _OVERWRITE_TYPE_ROLE,
        "allow": str(_TEAM_CHANNEL_MEMBER_PERMS), "deny": "0",
    }


def _bot_allow_overwrite(bot_user_id: str) -> dict:
    """This bot's own user -- the member perms above PLUS Manage
    Channels. See _BOT_CHANNEL_PERMS's own comment for why Manage
    Channels specifically: without an explicit allow of its own, the
    very @everyone-deny overwrite this function writes would lock the
    bot itself out of the channel it just created (or is repairing),
    with no way back in short of a human re-granting it access by hand
    in Discord's own UI.
    """
    return {
        "id": bot_user_id, "type": _OVERWRITE_TYPE_MEMBER,
        "allow": str(_BOT_CHANNEL_PERMS), "deny": "0",
    }


def _category_overwrites(guild_id: str, bot_user_id: str) -> list[dict]:
    """The category's own overwrite list -- @everyone denied, the bot
    allowed. Deliberately does NOT include any team role: the category
    itself is never a place a team needs its own allow, since each
    child channel carries its own complete list (see
    _team_channel_overwrites() below) that a viewer's permissions
    resolve against directly.
    """
    return [_everyone_deny_overwrite(guild_id), _bot_allow_overwrite(bot_user_id)]


def _team_channel_overwrites(guild_id: str, role_id: str, bot_user_id: str) -> list[dict]:
    """One team channel's full, authoritative overwrite list: @everyone
    denied, that team's role allowed, the bot allowed -- see
    ensure_team_channels()'s own docstring for why this complete list is
    written to every team channel rather than left to inherit the
    category's (a channel dragged out of its category must stay
    private, and a channel's own overwrites are exactly what makes that
    true regardless of where it lives).
    """
    return [
        _everyone_deny_overwrite(guild_id),
        _team_role_allow_overwrite(role_id),
        _bot_allow_overwrite(bot_user_id),
    ]


def _normalize_channel_name(name: str) -> str:
    """Lowercase, then strip everything that isn't `[a-z0-9]` -- so
    `red🟥` and `Team-Red!` both become `red`/`teamred`, matchable
    against a plain team name or discord_config.team_category_name
    regardless of an operator's own emoji, punctuation, or capitalization
    choices in Discord itself. See this module's own docstring's
    ADOPTION section for why this exists: the previous exact-string
    match could never see past a single emoji, and treated every
    hand-decorated channel as not existing at all.

    Deliberately ASCII-only (`[a-z0-9]`, not a unicode-aware `\\w`) --
    this only ever needs to compare against team names and
    team_category_name, both of which are plain ASCII in this codebase,
    so anything outside that range (emoji, accented letters, whatever)
    is exactly the kind of decoration this should strip, never a
    character this needs to preserve or fold case-insensitively.
    """
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


async def _ensure_category(
    guild_id: str,
    category_name: str,
    stored_category_id: str | None,
    bot_user_id: str,
    channels: list[dict],
    *,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Ensure the one shared team-channel category exists, returning its
    id. Same three-tier "id, then name, then create" order
    ensure_team_roles() above already uses for a team role: the stored
    id is tried first (still present in `channels`?), then a NORMALIZED
    name match (see _normalize_channel_name() above -- an operator's
    `[Team Chat]` matches a configured "Team Chat" the same as it would
    match "team chat", and a past run before the id was recorded may
    already have one too), and only then is a new category created.
    Either of the first two branches re-asserts this category's full
    overwrite list with a PATCH (see _set_channel_overwrites()'s own
    docstring for why that's a full replace, not a diff) -- a category
    this function just created already got the same list at creation
    time and needs no follow-up call.
    """
    overwrites = _category_overwrites(guild_id, bot_user_id)
    categories_by_id = {c["id"]: c for c in channels if c.get("type") == _CHANNEL_TYPE_CATEGORY}
    if stored_category_id and stored_category_id in categories_by_id:
        await _set_channel_overwrites(stored_category_id, overwrites, http_client=http_client)
        return stored_category_id
    target = _normalize_channel_name(category_name)
    for c in channels:
        if c.get("type") == _CHANNEL_TYPE_CATEGORY and _normalize_channel_name(c.get("name") or "") == target:
            await _set_channel_overwrites(c["id"], overwrites, http_client=http_client)
            return c["id"]
    created = await _create_guild_channel(
        guild_id, name=category_name, channel_type=_CHANNEL_TYPE_CATEGORY,
        parent_id=None, permission_overwrites=overwrites, http_client=http_client,
    )
    return created["id"]


async def _ensure_team_channel(
    guild_id: str,
    team: str,
    role_id: str,
    category_id: str,
    stored_channel_id: str | None,
    bot_user_id: str,
    channels: list[dict],
    *,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[str | None, str, list[str] | None]:
    """Ensure `team`'s private text channel exists inside `category_id`,
    returning (channel_id, bucket, candidate_names). `channel_id` is
    None only for bucket "ambiguous" (see below); `candidate_names` is
    non-None only for that same bucket. Bucket is one of "unchanged",
    "adopted", "created", "recreated", or "ambiguous" -- see this
    module's own docstring's ADOPTION section for why adopting an
    existing channel is the normal path here, creating a new one the
    last resort.

    Adoption order, checked in this exact sequence:

      1. `stored_channel_id` (discord_team_role.channel_id, from a past
         run or an admin's own POST /api/admin/discord/team-channel) is
         still a real channel in `channels` -> use it AS-IS. Its own
         overwrites are still reasserted (see _set_channel_overwrites()'s
         own docstring for why that's an ongoing repair, not a one-time
         set), but its NAME is never touched -- an operator's own
         `red🟥` must survive forever once this bot has adopted it.
         Bucket "unchanged".
      2. No usable stored id -- look at every text channel inside
         `category_id` whose _normalize_channel_name() equals `team`'s
         own name lowercased.
           - Exactly one match -> adopt it (same "use as-is, reassert
             overwrites, never rename" treatment as step 1) and record
             its id. Bucket "adopted".
           - More than one match -> this function CANNOT guess which one
             is `team`'s -- no create, no overwrite PATCH, channel_id
             untouched in the database. Bucket "ambiguous", with every
             matching channel's own (real, un-normalized) name returned
             so an admin can pick one by hand.
      3. No match at all -> create a brand new text channel named
         `team`'s name lowercased (plain, no emoji -- there is nothing
         to adopt the styling of). Bucket "recreated" when
         `stored_channel_id` was set (a previously tracked channel is
         gone and no same-named replacement was found either), else
         "created".
    """
    name = team.lower()
    overwrites = _team_channel_overwrites(guild_id, role_id, bot_user_id)
    channels_by_id = {c["id"]: c for c in channels if c.get("type") == _CHANNEL_TYPE_TEXT}

    if stored_channel_id and stored_channel_id in channels_by_id:
        await _set_channel_overwrites(stored_channel_id, overwrites, http_client=http_client)
        return stored_channel_id, "unchanged", None

    candidates = [
        c for c in channels
        if c.get("type") == _CHANNEL_TYPE_TEXT
        and c.get("parent_id") == category_id
        and _normalize_channel_name(c.get("name") or "") == name
    ]
    if len(candidates) == 1:
        chan = candidates[0]
        await _set_channel_overwrites(chan["id"], overwrites, http_client=http_client)
        return chan["id"], "adopted", None
    if len(candidates) > 1:
        return None, "ambiguous", sorted(c.get("name") or "" for c in candidates)

    created = await _create_guild_channel(
        guild_id, name=name, channel_type=_CHANNEL_TYPE_TEXT, parent_id=category_id,
        permission_overwrites=overwrites, http_client=http_client,
    )
    return created["id"], ("recreated" if stored_channel_id else "created"), None


async def _upsert_team_role(team: str, role_id: str, now: int) -> None:
    """Record (or update) discord_team_role's one row for `team` --
    the ONLY table this module ever writes to, and a small enough write
    that a short-lived WriteSession per call (the same pattern
    app/discord_notify.py's _mark_posted()/_mark_failed() already use
    for their own one-row updates) is simpler than threading a
    caller-owned connection through ensure_team_roles()'s async/await
    HTTP calls.
    """
    async with WriteSession() as conn:
        conn.execute(
            "INSERT INTO discord_team_role(team, role_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(team) DO UPDATE SET role_id = excluded.role_id, updated_at = excluded.updated_at",
            (team, role_id, now),
        )


async def ensure_team_roles(*, http_client: httpx.AsyncClient | None = None) -> dict:
    """Make sure every team in _TEAM_COLORS (app/discord_notify.py's
    own palette -- reused verbatim, never a second copy, so a team
    colour change there is automatically the colour the next call here
    creates/repairs a role with too) has exactly one Discord role, and
    that discord_team_role remembers its id. Safe to call repeatedly --
    an operator's "Create / repair team roles and channels" button in
    app/admin_ops.py, and nothing else, since this never runs on its
    own schedule.

    Per team:
      - discord_team_role already has a row AND that role id is still
        a real role in the guild -> left alone entirely (no API call
        for that team beyond the one shared GET below).
      - a row exists but its role id is gone from the guild (deleted by
        hand) -> a NEW role is created and the row is updated to the
        new id. The old, deleted role is not "restored" -- Discord has
        no such operation -- this is a fresh role that happens to have
        the same name and colour.
      - no row, but the guild already has a role with this exact name
        -> that existing role is adopted (its id is written to a new
        row) rather than creating a duplicate -- an operator who
        created team roles by hand before this feature existed must
        never end up with two "GREEN" roles.
      - no row and no matching name -> a new role is created.

    Returns {"ok": True, "created": [...], "recreated": [...],
    "reused": [...], "unchanged": [...]} (team names in each bucket),
    or {"ok": False, "reason": ...} when role sync isn't configured
    (_roles_ready() False) -- no API call is made in that case at all.
    """
    conn = connect()
    try:
        cfg = load_discord_config(conn)
        if not _roles_ready(cfg):
            return {"ok": False, "reason": "roles sync disabled or not fully configured"}
        guild_id = cfg["guild_id"]
        existing = {
            r["team"]: dict(r)
            for r in conn.execute("SELECT team, role_id, updated_at FROM discord_team_role").fetchall()
        }
    finally:
        conn.close()

    guild_roles = await _list_guild_roles(guild_id, http_client=http_client)
    roles_by_id = {r["id"]: r for r in guild_roles}
    roles_by_name = {r["name"]: r for r in guild_roles}

    now = int(time.time())
    created: list[str] = []
    recreated: list[str] = []
    reused: list[str] = []
    unchanged: list[str] = []

    for team, color in _TEAM_COLORS.items():
        row = existing.get(team)
        if row is not None and row["role_id"] in roles_by_id:
            unchanged.append(team)
            continue
        if row is None:
            found = roles_by_name.get(team)
            if found is not None:
                await _upsert_team_role(team, found["id"], now)
                reused.append(team)
                continue
        new_role = await _create_guild_role(
            guild_id,
            name=team,
            color=color,
            hoist=True,
            mentionable=False,
            permissions=_TEAM_ROLE_PERMISSIONS,
            http_client=http_client,
        )
        await _upsert_team_role(team, new_role["id"], now)
        if row is None:
            created.append(team)
        else:
            recreated.append(team)

    return {"ok": True, "created": created, "recreated": recreated, "reused": reused, "unchanged": unchanged}


async def _upsert_team_channel(team: str, channel_id: str, now: int) -> None:
    """Record (or update) discord_team_role.channel_id for `team` -- the
    matching per-team write for ensure_team_channels() below, same
    per-row upsert shape _upsert_team_role() above uses for role_id, on
    the SAME row (see that column's own comment in app/db.py for why
    role id and channel id live together). Never touches role_id itself.
    In practice this is always an UPDATE against a row ensure_team_roles()
    already created (ensure_team_channels() only ever processes a team
    that already has a role_id -- see that function's own docstring),
    but ON CONFLICT keeps this safe regardless.
    """
    async with WriteSession() as conn:
        conn.execute(
            "INSERT INTO discord_team_role(team, role_id, channel_id, updated_at) VALUES (?, '', ?, ?) "
            "ON CONFLICT(team) DO UPDATE SET channel_id = excluded.channel_id, updated_at = excluded.updated_at",
            (team, channel_id, now),
        )


async def _save_team_category_id(category_id: str, now: int) -> None:
    """Record the discovered/created team-category's id onto
    discord_config.team_category_id -- the only discord_config write
    ensure_team_channels() makes (every other field there is
    admin-edited only, through app/admin_ops.py). Short-lived
    WriteSession, same one-write-one-transaction shape
    _upsert_team_role() above already uses.
    """
    async with WriteSession() as conn:
        conn.execute(
            "UPDATE discord_config SET team_category_id = ?, updated_at = ? WHERE id = 1",
            (category_id, now),
        )


async def ensure_team_channels(*, http_client: httpx.AsyncClient | None = None) -> dict:
    """Make sure every team in _TEAM_COLORS that already has a Discord
    role (discord_team_role.role_id -- from ensure_team_roles() above)
    has a private text channel, visible only to that team's own role,
    inside one shared category. Called by app/admin_ops.py's "Create /
    repair team roles and channels" button RIGHT AFTER ensure_team_roles()
    itself succeeds, never before and never on its own schedule -- a
    channel's own permission overwrite names a team's role id, so there
    is nothing to gate a channel on until that role exists.

    ONLY EVER touches: the one category named
    discord_config.team_category_name (found by its stored id, else by a
    NORMALIZED name match, else created -- see _ensure_category()), and
    the channels already recorded in discord_team_role.channel_id, or,
    for a team with no usable recorded channel id, a channel found by a
    normalized name match (see _normalize_channel_name()) SCOPED to that
    one category (see _ensure_team_channel()). No other channel or
    category in the guild is ever read for a match, modified, or -- see
    below -- deleted. A team whose normalized match is AMBIGUOUS (more
    than one channel in the category normalizes to its name) is skipped
    entirely for the rest of this function's work -- no create, no
    overwrite PATCH, its stored channel_id (if any) left exactly as it
    was -- see _ensure_team_channel()'s own docstring.

    Permission overwrites are the full, authoritative list on every
    single call, both on create and reasserted with a whole-array PATCH
    on a channel/category this function finds already existing (see
    _set_channel_overwrites()'s own docstring) -- deliberate, ongoing
    repair, not a one-time set: a hand edit to a channel's permissions in
    Discord itself (an operator removing the @everyone deny, say) is
    corrected on the very next run, the same "state lives in Discord,
    this app is just the enforcer" philosophy sync_member() above already
    applies to role membership. This applies EQUALLY to an adopted
    channel as to one this bot created itself -- adopting an operator's
    own, previously public `red🟥` necessarily makes it team-only from
    that point on, the same as any other channel this function manages;
    that is the intended behaviour of turning a channel into one of
    Herald's team channels at all, not a side effect to work around.

    NEVER deletes a channel, and NEVER renames one -- not a channel this
    function creates fresh (always named the team's own plain lowercase
    name, see _ensure_team_channel()), and especially not one it adopts:
    an operator's own `red🟥`/`Team-Red!` naming survives forever once
    adopted, exactly as it was. A team that has disappeared from
    _TEAM_COLORS entirely is simply not iterated below; its channel and
    stored channel_id are left exactly as they are, forever, until a
    human deletes the channel by hand.

    Returns {"ok": True, "created": [...], "recreated": [...],
    "reused": [...], "adopted": [...], "unchanged": [...],
    "ambiguous": [...]} (team names in every bucket except "ambiguous",
    which holds {"team": ..., "candidates": [channel name, ...]} dicts --
    see _ensure_team_channel()'s own docstring for what puts a team in
    each bucket; "reused" is never populated by this function -- kept
    here only for the same bucket-name shape ensure_team_roles() returns
    -- since a normalized name match is now always reported as
    "adopted" regardless of whether a stale stored id preceded it), or
    {"ok": False, "reason": ...} -- with NO API call made at all when
    this feature isn't configured (_channels_ready() False), or with the
    specific Discord call's own failure message (see _check_channel_ok()
    for the 403-names-a-permission case) when one does fail partway
    through. Every failure is caught and returned as a reason here
    rather than left to raise DiscordAPIError out of this function: an
    operator clicking the ensure button must always get an answer, never
    a crashed request, even when Herald's own permissions in the guild
    are wrong.
    """
    conn = connect()
    try:
        cfg = load_discord_config(conn)
        if not _channels_ready(cfg):
            return {"ok": False, "reason": "team channels disabled, or role sync not fully configured"}
        guild_id = cfg["guild_id"]
        category_name = cfg["team_category_name"] or "Teams"
        stored_category_id = cfg.get("team_category_id") or None
        team_role_rows = {
            r["team"]: dict(r)
            for r in conn.execute("SELECT team, role_id, channel_id FROM discord_team_role").fetchall()
        }
    finally:
        conn.close()

    now = int(time.time())
    created: list[str] = []
    recreated: list[str] = []
    reused: list[str] = []
    adopted: list[str] = []
    unchanged: list[str] = []
    ambiguous: list[dict] = []

    try:
        bot_user_id = await _cached_bot_user_id(http_client=http_client)
        channels = await _list_guild_channels(guild_id, http_client=http_client)

        category_id = await _ensure_category(
            guild_id, category_name, stored_category_id, bot_user_id, channels, http_client=http_client,
        )
        if category_id != stored_category_id:
            await _save_team_category_id(category_id, now)

        for team in _TEAM_COLORS:
            row = team_role_rows.get(team)
            if row is None or not row.get("role_id"):
                continue  # no team role yet -- ensure_team_roles() hasn't created/adopted one
            stored_channel_id = row.get("channel_id") or None

            channel_id, bucket, candidates = await _ensure_team_channel(
                guild_id, team, row["role_id"], category_id, stored_channel_id, bot_user_id, channels,
                http_client=http_client,
            )
            if bucket == "ambiguous":
                ambiguous.append({"team": team, "candidates": candidates})
                continue
            if channel_id != stored_channel_id:
                await _upsert_team_channel(team, channel_id, now)
            {
                "created": created, "recreated": recreated, "reused": reused,
                "adopted": adopted, "unchanged": unchanged,
            }[bucket].append(team)
    except DiscordAPIError as e:
        return {"ok": False, "reason": str(e)}

    return {
        "ok": True, "created": created, "recreated": recreated, "reused": reused,
        "adopted": adopted, "unchanged": unchanged, "ambiguous": ambiguous,
    }


# ---- slash-command registration (app/discord_interactions.py) -------------


async def register_commands(*, http_client: httpx.AsyncClient | None = None) -> dict:
    """Bulk-overwrite this guild's slash commands with the FULL registry
    (app/discord_interactions.py's COMMANDS): PUT
    /applications/{app_id}/guilds/{guild_id}/commands. A guild-scoped
    bulk overwrite replaces the ENTIRE command set in one call and takes
    effect immediately (unlike a GLOBAL command registration, which
    Discord can take up to an hour to propagate) -- a command this app
    no longer defines disappears from the guild the moment this call
    returns, and one newly added here appears just as fast, with zero
    special-casing for either direction.

    Admin-triggered only (POST /api/admin/discord/slash/register) --
    never run at startup and never on any schedule, unlike
    ensure_team_roles()/ensure_team_channels() above, which are at least
    SAFE to re-run unprompted (idempotent adopt-or-create). Registration
    has no such "nothing changes if nothing changed" property working in
    an operator's favor here -- it is still safe to call repeatedly (the
    SAME registry produces the SAME PUT body every time), but there is
    no reason to run it before an operator has actually finished setting
    up the interactions endpoint and asked for it.

    Uses the BOT token (settings.discord_bot_token, via this module's
    own _request()) -- registering commands is a guild-management
    action taken as the bot user, an entirely different credential from
    the INTERACTION token app/discord_interactions.py's own deferred
    follow-ups use to answer one single command invocation (see that
    module's own _patch_followup() docstring for why those two must
    never be confused).

    Returns {"ok": True, "commands": [<name>, ...]} (registry order,
    exactly what was just registered) on success, or {"ok": False,
    "reason": ...} when this isn't fully configured yet (no bot token,
    no app_id, or no guild_id -- discord_config.app_id specifically,
    since the guild the commands register into and the application
    registering them must both be known) or the PUT itself fails -- same
    "always answer, never raise" contract every other admin-triggered
    action in this module already follows.

    Local import of app/discord_interactions.py's COMMANDS: that module
    imports app/discord_notify.py at module level (build_month_honors_embed,
    load_discord_config, ...), which THIS module already imports from at
    its own module level too -- there is no cycle either way, but the
    import is kept local anyway so a future change to either module's
    own import graph can never surprise this one's load order, the same
    caution build_month_honors_embed()'s own local `from . import
    results` already takes in app/discord_notify.py.
    """
    from .discord_interactions import COMMANDS

    conn = connect()
    try:
        cfg = load_discord_config(conn)
    finally:
        conn.close()

    app_id = cfg.get("app_id")
    guild_id = cfg.get("guild_id")
    if not settings.discord_bot_token or not app_id or not guild_id:
        return {
            "ok": False,
            "reason": "slash commands are not fully configured (need a bot token, app id, and guild id)",
        }

    body = [c.definition() for c in COMMANDS]
    resp = await _request(
        "PUT", f"/applications/{app_id}/guilds/{guild_id}/commands",
        json_body=body, http_client=http_client,
    )
    try:
        _check_ok(resp, "register slash commands")
    except DiscordAPIError as e:
        return {"ok": False, "reason": str(e)}

    return {"ok": True, "commands": [c.name for c in COMMANDS]}


async def sync_member(
    conn, player_id: int, *, http_client: httpx.AsyncClient | None = None
) -> dict:
    """Make one player's Discord roles match their current MeshWars
    team, and only that team -- called directly (and awaited to
    completion) by reconcile_all() below, and via the never-raises
    sync_member_safe() wrapper from every event-driven call site (see
    this module's own docstring's FIRE-AND-FORGET section).

    `conn` is used for READS ONLY (player, account_identity,
    discord_team_role, discord_config) -- this function never writes to
    the database, so `conn` needs no write lock and callers are free to
    hand it a plain connect() connection, including one opened AFTER an
    unrelated WriteSession has already committed (see sync_member_safe's
    call sites) or the caller's own already-open connection.

    No-ops (returns {"ok": True, "reason": ...}, makes NO outbound
    call) for every case where there is nothing to do:
      - role sync not configured (_roles_ready() False)
      - the player has no linked account, or the account has no
        provider='discord' account_identity row
      - GET .../members/{snowflake} returns 404 -- not a member of the
        guild right now, not an error
    A disabled player (player.disabled_at set) or one with no team
    (should not happen in practice, but handled explicitly rather than
    assumed) is treated as "desired role: none" -- every team role is
    removed, nothing is added.

    Computes the diff between the member's CURRENT roles (from the GET)
    and the desired set, and issues only the PUT/DELETE calls actually
    needed -- a member already correctly holding just their team's role
    causes zero role-mutating calls, only the one GET. Never touches a
    role that isn't one of discord_team_role's own tracked ids: a
    member's other server roles (moderator, booster, whatever else this
    guild has) are none of this bot's business.
    """
    cfg = load_discord_config(conn)
    if not _roles_ready(cfg):
        return {"ok": False, "reason": "roles sync disabled or not fully configured"}
    guild_id = cfg["guild_id"]

    player = conn.execute(
        "SELECT account_id, team, disabled_at FROM player WHERE player_id = ?", (player_id,)
    ).fetchone()
    if player is None or player["account_id"] is None:
        return {"ok": True, "reason": "player has no linked account"}

    identity = conn.execute(
        "SELECT subject FROM account_identity WHERE account_id = ? AND provider = 'discord'",
        (player["account_id"],),
    ).fetchone()
    if identity is None:
        return {"ok": True, "reason": "account has no linked discord identity"}
    snowflake = identity["subject"]

    role_id_by_team = {
        r["team"]: r["role_id"]
        for r in conn.execute("SELECT team, role_id FROM discord_team_role").fetchall()
    }
    all_team_role_ids = set(role_id_by_team.values())

    resp = await _request("GET", f"/guilds/{guild_id}/members/{snowflake}", http_client=http_client)
    if resp.status_code == 404:
        return {"ok": True, "reason": "not a member of the guild"}
    _check_ok(resp, "get member")
    member = resp.json()
    current_role_ids = set(member.get("roles") or [])

    is_disabled = player["disabled_at"] is not None
    team = player["team"] if not is_disabled else None
    desired_role_id = role_id_by_team.get(team) if team else None

    to_add = {desired_role_id} if desired_role_id and desired_role_id not in current_role_ids else set()
    to_remove = {rid for rid in (current_role_ids & all_team_role_ids) if rid != desired_role_id}

    for rid in to_add:
        r = await _request(
            "PUT", f"/guilds/{guild_id}/members/{snowflake}/roles/{rid}", http_client=http_client
        )
        _check_ok(r, "add member role")
    for rid in to_remove:
        r = await _request(
            "DELETE", f"/guilds/{guild_id}/members/{snowflake}/roles/{rid}", http_client=http_client
        )
        _check_ok(r, "remove member role")

    return {"ok": True, "added": sorted(to_add), "removed": sorted(to_remove)}


async def sync_member_safe(conn, player_id: int) -> None:
    """Fire-and-forget wrapper around sync_member() for every
    event-driven call site (link-key, the Discord OAuth callback cases,
    pending/link, a team change) -- never raises. Same contract
    app/account_api.py's _notify_security() applies to a security-notice
    send: a Discord outage, a misconfigured guild, a role permission
    problem, anything -- must never surface to the caller or undo the
    account/team action that already committed. Logs and swallows.
    """
    try:
        await sync_member(conn, player_id)
    except Exception:
        log.exception("discord roles: sync failed for player %d", player_id)


# Monotonic timestamp of the last time reconcile_all() actually ran
# (whether it did real work or no-op'd on _roles_ready()), managed
# entirely by reconcile_all() itself -- see maybe_reconcile_roles()'s
# own docstring for why a manual admin-triggered run and the periodic
# background one share this one gate rather than each keeping their own.
_last_reconcile_gate_at = 0.0

# The last reconcile_all() result, for GET /api/admin/discord to show
# an operator ("last reconcile time," "count of members changed") --
# process-local only, not persisted to the database: this is
# operational visibility into a pass that just ran, not the underlying
# state itself (role membership always lives in Discord and is always
# fully re-derivable by the very next reconcile), so losing it across a
# restart costs nothing worth a migration.
_last_reconcile_result: dict = {"ok": False, "at": 0, "checked": 0, "changed": 0}


async def reconcile_all(*, http_client: httpx.AsyncClient | None = None) -> dict:
    """One full pass over every player with a linked Discord identity,
    calling sync_member() for each. Exists to catch what the
    event-driven paths (sync_member_safe on link/team-change) can miss:
    someone who links Discord and only joins the guild later, or a role
    an operator edited by hand in Discord itself, drifting away from
    what this app thinks is true. Called directly by
    POST /api/admin/discord/roles/reconcile (bypassing
    maybe_reconcile_roles()'s interval gate -- an operator clicking
    "Reconcile all now" means now) and, on its own schedule, by
    maybe_reconcile_roles() below.

    Updates _last_reconcile_gate_at UNCONDITIONALLY, at the very start,
    before any await -- so a manual run and the periodic loop can never
    race each other into overlapping work, and the periodic loop's next
    tick (maybe_reconcile_roles()) correctly waits out a full
    _RECONCILE_INTERVAL_SECONDS from whichever call happened most
    recently, manual or scheduled.

    No-ops immediately (an {"ok": False, ...} result, no player list
    ever read, no outbound calls) when role sync isn't configured
    (_roles_ready() False).

    A single player's sync_member() failure is logged and skipped, same
    "one bad row must never stop the rest of the cycle" rule
    app/discord_notify.py's check_due_time_driven() and _drain_once()
    already apply to their own per-item loops -- one broken identity
    (a snowflake Discord no longer recognizes, say) must never prevent
    every other player in the same pass from being reconciled.
    """
    global _last_reconcile_gate_at, _last_reconcile_result
    _last_reconcile_gate_at = time.monotonic()

    conn = connect()
    try:
        cfg = load_discord_config(conn)
        if not _roles_ready(cfg):
            result = {"ok": False, "reason": "roles sync disabled or not fully configured",
                       "at": int(time.time()), "checked": 0, "changed": 0}
            _last_reconcile_result = result
            return result
        player_ids = [
            r["player_id"]
            for r in conn.execute(
                "SELECT DISTINCT p.player_id FROM player p "
                "JOIN account_identity ai ON ai.account_id = p.account_id AND ai.provider = 'discord' "
                "WHERE p.account_id IS NOT NULL"
            ).fetchall()
        ]
    finally:
        conn.close()

    changed = 0
    for player_id in player_ids:
        read_conn = connect()
        try:
            outcome = await sync_member(read_conn, player_id, http_client=http_client)
        except Exception:
            log.exception("discord roles: reconcile failed for player %d", player_id)
            continue
        finally:
            read_conn.close()
        if outcome.get("added") or outcome.get("removed"):
            changed += 1

    result = {"ok": True, "at": int(time.time()), "checked": len(player_ids), "changed": changed}
    _last_reconcile_result = result
    return result


async def maybe_reconcile_roles(*, http_client: httpx.AsyncClient | None = None) -> dict | None:
    """Interval-gated wrapper app/discord_notify.py's run_forever() calls
    once per its own poll cycle -- see this module's own docstring's
    RECONCILE section, and _RECONCILE_INTERVAL_SECONDS's own comment,
    for why this is 15 minutes rather than that loop's native 30s.
    Returns reconcile_all()'s own result dict on a cycle that actually
    ran it, or None when the interval hasn't elapsed since the last run
    (manual or scheduled) -- the gate is skipped this tick, nothing is
    read or called.
    """
    if time.monotonic() - _last_reconcile_gate_at < _RECONCILE_INTERVAL_SECONDS:
        return None
    return await reconcile_all(http_client=http_client)


def get_last_reconcile() -> dict:
    """The last reconcile_all() result (manual or scheduled), for GET
    /api/admin/discord -- see _last_reconcile_result's own comment for
    why this is process-local rather than a database row.
    """
    return dict(_last_reconcile_result)
