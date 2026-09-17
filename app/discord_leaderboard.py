"""The pinned, self-editing Discord leaderboard: one webhook message per
deployment for kind="leaderboard" (app/discord_notify.py's own per-kind
routing -- resolve_discord_webhook(), falling back to discord_config's
default webhook exactly like every other kind), posted ONCE and then
EDITED IN PLACE on a schedule of its own, never reposted while it still
exists. The bot (app/discord_bot.py's "Herald") pins it.

WHY A SEPARATE MODULE: every other Discord feature in this codebase
already has its own file split by WHAT IT TALKS TO, not by what it is
for -- app/discord_notify.py posts one-shot announcements through a
webhook (an outbox, drained on its own schedule); app/discord_bot.py
authenticates as a bot against Discord's REST API (roles, channels,
slash-command registration); app/discord_interactions.py answers
INBOUND slash commands. This feature is a genuine third thing: it posts
and edits through a WEBHOOK (like discord_notify.py) but also pins with
the BOT (like discord_bot.py), and needs its own persistent state
(discord_pinned_message, app/db.py) that belongs to neither existing
table. Splitting it out keeps each of those three modules answering to
one API surface, and this one composing exactly two of them.

CONTENT: one embed per board (MeshCore, Meshtastic) that currently has
an active season -- a board with no active season gets no embed at all,
not an empty one. Each embed mirrors the site's own "Season Rankings"
modal (frontend/mc.js / frontend/map2.js) field for field:

  - Standings: every team's rank, coloured dot, and combined score --
    the EXACT SAME app/discord_interactions.py._team_standings()/
    _team_line() helpers /standings and /me already read, so this
    embed, /standings, and a season-close announcement can never
    disagree about what a team's total is.
  - Wardrivers / NetOps / Explorer: the top `leaderboard_top_n` players
    from app/mc_api.py's top_for()/top_checkin_for()/top_explorer_for()
    -- the SAME three helpers that already back the site's own three
    "Top Operators" tabs (see those functions' own docstrings), never a
    fourth, combined per-player score invented for this feature alone
    (Matt's own call: there is no single "points" figure for a player
    anywhere else in this codebase, only these three independent
    activities, so the leaderboard mirrors the site's own three tabs
    rather than inventing a combined one). Labels ("Wardrivers",
    "NetOps", "Explorer" -- see _WARDRIVER_LABEL et al below) are taken
    verbatim from frontend/map2.js's own topCaptureLabel/topCheckinLabel/
    topExplorerLabel, not retyped. A list with no entries is left out of
    the embed entirely, same "never an empty shell" rule
    app/discord_notify.py's build_month_honors_embed() already applies
    to its own optional embeds.

PRIVACY: this is an UNAUTHENTICATED surface, same as every command in
app/discord_interactions.py -- team, display name, and a score are all
already public on the site itself; no location, cell, place, radio, or
node identifier is ever read here, let alone rendered. Every rendering
helper reused from app/discord_notify.py/app/discord_interactions.py
already carries that same restriction; this module adds no query of its
own that could leak one.

CHANGE DETECTION: the message body is hashed EXCLUDING its own "as of"
line (_content_hash() hashes _build_base_payload()'s return -- username
+ embeds only), so a pass that finds nothing changed does not edit the
message just to bump a clock. The "as of" line therefore means "last
CHANGED," not "last checked" -- the honest meaning, since nothing about
the content actually changed on every other pass. See
discord_pinned_message's own comment in app/db.py for the same point
made about content_hash there.

THE UPDATE PASS (run_leaderboard_pass()) -- gated by
discord_config.leaderboard_enabled and, via maybe_run_leaderboard()
below, its OWN interval (discord_config.leaderboard_interval_seconds),
never the outbox's 30s poll:

  1. Resolve the current webhook for kind="leaderboard"
     (resolve_discord_webhook(), same per-kind routing/fallback every
     other kind uses). Disabled, or no webhook resolved -> do nothing.
  2. No stored discord_pinned_message row -> POST with `?wait=true`
     (Discord's response body carries the new message's own id and
     channel_id), store the row, pin it.
  3. A stored row whose webhook_id still matches the resolved webhook's
     own parsed id -> PATCH the stored message only if the hash changed
     (never just to refresh the clock). A PATCH 404 (a person deleted
     the message) reposts and re-pins fresh, same as case 2. Any other
     failure is logged (sanitised) and left for the next interval to
     retry -- this pass never raises out to run_forever().
  4. A stored row whose webhook_id does NOT match (an operator moved the
     leaderboard to a different webhook) -> the OLD message is unpinned
     with the BOT via its stored channel_id/message_id (404 ignored --
     see app/discord_bot.py's unpin_message() for why it can never
     instead be DELETED: this bot holds no Manage Messages grant, and
     the OLD webhook's own bearer token was never stored in the first
     place -- discord_pinned_message.webhook_id is the numeric id only,
     see that column's own comment in app/db.py), then a fresh message
     is posted and pinned through the NEW webhook and the row is
     replaced wholesale.
  5. Pinning always goes through app/discord_bot.py's pin_message() --
     the bot, never the webhook -- and a failure there (no bot token, no
     "Pin Messages" permission) never fails the pass: the message keeps
     posting/editing, discord_pinned_message.pinned simply stays/becomes
     0, and the admin panel says "not pinned" honestly.

Webhook POST/PATCH never uses the bot token (only the webhook URL's own
embedded auth); pin/unpin always uses the bot token, never the webhook.
Every webhook URL this module builds is covered by the existing
app/log_redact.py filter (same /api[/vN]/webhooks/<id>/<token> shape
app/discord_notify.py's own _post() already produces).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time

import httpx

from . import discord_bot, discord_interactions, discord_notify, mc_api
from .db import WriteSession, connect

log = logging.getLogger("discord_leaderboard")

# The one discord_outbox-style "kind" this feature routes under --
# resolve_discord_webhook() (app/discord_notify.py) treats it exactly
# like any announcement kind: a discord_channel row for "leaderboard"
# wins if present and enabled, otherwise discord_config's own default
# webhook, otherwise nothing to post to at all.
_KIND = "leaderboard"

# Section labels, taken VERBATIM from frontend/map2.js's own
# topCaptureLabel/topCheckinLabel/topExplorerLabel (the same three tabs
# app/mc_api.py's top_for()/top_checkin_for()/top_explorer_for() already
# back on the site) -- never retyped, so an operator who already knows
# the site's own "Season Rankings" modal recognizes this embed
# immediately as the same three lists, not a fourth invented ranking.
_WARDRIVER_LABEL = "Wardrivers"
_NETOPS_LABEL = "NetOps"
_EXPLORER_LABEL = "Explorer"

# Units for each list's bold figure, in the site's OWN words --
# "captures" mirrors frontend/map2.js's own valueHeader for that tab
# ("Captures"); "check-in points" / "exploration points" mirror the
# exact phrasing frontend/mc.js's/map2.js's own team-total tooltip uses
# ("... check-in point(s) + ... exploration point(s)") to keep the two
# distinct even though both tabs' own column header is the bare generic
# "Points" -- a Discord field has no per-row tooltip to disambiguate
# them the way the site's own hover text does, so the unit line has to
# do that job instead.
_WARDRIVER_UNIT = "captures"
_NETOPS_UNIT = "check-in points"
_EXPLORER_UNIT = "exploration points"

# Discord's own default when nothing pins yet (see load_discord_config()'s
# own fallback in app/discord_notify.py, mirrored here so a caller that
# somehow gets a config dict with the key missing -- should not happen,
# but this is cheap insurance -- still gets a sane top-N rather than an
# unbounded list).
_DEFAULT_TOP_N = 5

# Discord's own webhook-URL shape, id-only (never the token) --
# .../webhooks/<id>/<token>[?query] -- across either the bare or
# versioned /api[/vN]/ prefix app/log_redact.py's own regex already
# expects. Used ONLY to pull the numeric id for
# discord_pinned_message.webhook_id (see that column's own comment in
# app/db.py for why the id, never the token, is what gets stored).
_WEBHOOK_ID_RE = re.compile(r"/webhooks/(\d+)/")

# allowed_mentions.parse == [] on every message this module ever posts
# or edits -- same "the bot must never be able to ping anyone" rule
# app/discord_interactions.py's own _ALLOWED_MENTIONS applies to every
# response it sends.
_ALLOWED_MENTIONS = {"parse": []}


def _webhook_id_from_url(url: str) -> str:
    """The numeric webhook id out of `url`, or "" if it doesn't look like
    a webhook URL at all (should never happen -- `url` is always
    resolve_discord_webhook()'s own return -- but this must never raise
    over a malformed value). Compared against discord_pinned_message.
    webhook_id on every pass to detect an operator moving the
    leaderboard to a different webhook (see this module's own docstring,
    case 4).
    """
    m = _WEBHOOK_ID_RE.search(url or "")
    return m.group(1) if m else ""


def _error_detail(resp: httpx.Response) -> str:
    """Same truncated-snippet-of-Discord's-own-response-body shape
    app/discord_notify.py's _post() and app/discord_bot.py's _check_ok()
    already use -- Discord's OWN text describing what was wrong with the
    payload this app sent, never a credential, so it is safe to fold
    into a raised message.
    """
    snippet = (resp.text or "").strip()[: discord_notify._MAX_ERROR_BODY_CHARS]
    return f": {snippet}" if snippet else ""


async def _webhook_request(
    method: str, url: str, json_body: dict | None = None, *, http_client: httpx.AsyncClient | None = None
) -> httpx.Response:
    """One call to a Discord WEBHOOK endpoint (POST .../webhooks/<id>/
    <token>?wait=true to create, PATCH .../webhooks/<id>/<token>/messages/
    <id> to edit) -- returns the RAW response so callers can interpret
    status codes themselves, same "return raw, let the caller decide"
    shape app/discord_bot.py's own _request() uses (a PATCH 404 here
    means "a person deleted the message," meaningful data, not a bug to
    raise over). Raises discord_notify.DiscordSendError only for a
    transport-level failure (timeout, connection error) -- same
    never-str(e) sanitisation _post() applies for the identical reason: a
    webhook URL carries its own bearer token in the path, and httpx's own
    exception text embeds the request.

    Deliberately does NOT go through app/discord_notify.py's own _post()
    -- that helper discards the response body entirely (an ordinary
    outbox post never needs its message's own id back) and raises on any
    non-2xx, which would turn this module's own 404-means-repost case
    into an exception to catch rather than a status to read.
    `http_client` is accepted purely so tests can inject an
    httpx.MockTransport-backed client, same as every other outbound
    call in this codebase.
    """
    client = http_client
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=discord_notify._POST_TIMEOUT_SECONDS)
    try:
        try:
            return await client.request(method, url, json=json_body)
        except httpx.TimeoutException as e:
            raise discord_notify.DiscordSendError("discord leaderboard webhook request timed out") from e
        except httpx.HTTPError as e:
            raise discord_notify.DiscordSendError(
                f"discord leaderboard webhook request failed ({type(e).__name__})"
            ) from e
    finally:
        if owns_client:
            await client.aclose()


# ---------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------


def _player_field(name: str, rows: list[dict], value_key: str, unit: str, emoji: dict[str, str], top_n: int) -> dict | None:
    """One "Top Operators" field (Wardrivers/NetOps/Explorer): rank, team
    dot, display name, bold figure -- one line per row, up to `top_n`,
    built with the exact same bold-number/team-dot building blocks
    app/discord_notify.py's own standings/award lines use. `rows` is
    already sorted best-first by whichever of app/mc_api.py's top_for()/
    top_checkin_for()/top_explorer_for() the caller passed in -- rank
    here is simply this list's own position, since none of those three
    helpers returns a rank field of its own. None when `rows` (after the
    top_n slice) is empty -- see build_leaderboard() below for why an
    empty list is left out of the embed entirely rather than sent as a
    field with nothing in it.
    """
    rows = rows[:top_n]
    if not rows:
        return None
    lines = [
        f"**#{i}** {discord_notify._team_dot(emoji, r['team'])}{r['display_name']} "
        f"**{discord_notify._fmt_number(r[value_key])}**"
        for i, r in enumerate(rows, 1)
    ]
    # _join_team_field() also appends the unit once, italicised, and
    # guards Discord's own 1024-char-per-field-value limit by dropping
    # trailing lines -- exactly the truncation guard a top-N list needs
    # too, not just a by-team award group, so it is reused as-is rather
    # than re-implemented here.
    return {"name": name, "value": discord_notify._join_team_field(lines, unit), "inline": True}


def _build_board_embed(conn, cfg: dict, protocol: str, top_n: int) -> dict | None:
    """One board's embed (Standings + Wardrivers + NetOps + Explorer), or
    None when `protocol` has no active season at all -- a board between
    seasons gets no embed, not an empty one (see build_leaderboard()'s
    own docstring).
    """
    season = mc_api.active_season(conn, protocol)
    if not season:
        return None

    proto_label = discord_notify._PROTOCOL_NAMES.get(protocol, protocol)
    emoji = discord_notify._parse_team_emoji(cfg["team_emoji"])
    standings = discord_interactions._team_standings(conn, protocol)

    fields = []
    if standings:
        # discord_interactions._team_line() is the EXACT line /standings
        # and /me already render for a team -- reused verbatim so this
        # embed can never disagree with either about a team's own total.
        lines = [discord_interactions._team_line(cfg, r) for r in standings]
        fields.append({
            "name": "Standings",
            "value": discord_notify._join_team_field(lines, discord_notify._SEASON_TOTAL_UNIT),
            "inline": False,
        })

    for f in (
        _player_field(_WARDRIVER_LABEL, mc_api.top_for(protocol), "captures", _WARDRIVER_UNIT, emoji, top_n),
        _player_field(_NETOPS_LABEL, mc_api.top_checkin_for(protocol), "points", _NETOPS_UNIT, emoji, top_n),
        _player_field(_EXPLORER_LABEL, mc_api.top_explorer_for(protocol), "points", _EXPLORER_UNIT, emoji, top_n),
    ):
        if f is not None:
            fields.append(f)

    if not fields:
        return None

    # Belt-and-suspenders, same as build_month_honors_embed()'s own use
    # of this guard: at most 4 fields per board here in practice
    # (Standings + 3 Top Operators lists), nowhere near Discord's real
    # 25-field cap, but applied anyway so a future fifth section can
    # never silently blow past it unnoticed.
    fields = fields[: discord_notify._MAX_EMBED_FIELDS]

    embed = {"title": proto_label, "fields": fields}
    color = discord_notify._team_color(standings[0]["team"]) if standings else None
    if color is not None:
        embed["color"] = color
    return embed


def build_leaderboard(conn, cfg: dict, top_n: int) -> dict | None:
    """The full webhook message body EXCLUDING the "as of" line and
    `allowed_mentions` (see _finalize_payload() below for where those are
    added, and this module's own docstring's CHANGE DETECTION section
    for why they must stay out of what gets hashed) -- one embed per
    board with an active season. None when NEITHER board has one right
    now (nothing at all to post).
    """
    embeds = []
    for protocol in discord_notify._PROTOCOL_NAMES:
        embed = _build_board_embed(conn, cfg, protocol, top_n)
        if embed is not None:
            embeds.append(embed)
    if not embeds:
        return None

    # Discord's own 6000-character TOTAL budget across every embed in the
    # message (app/discord_notify.py's _MAX_TOTAL_EMBED_CHARS) -- same
    # guard build_month_honors_embed() applies to its own multi-embed
    # payload. With top_n defaulting to 5 and each field already
    # length-guarded by _join_team_field() above, two boards' worth of
    # embeds should never realistically reach this, but the guard is
    # applied anyway: the second board's embed (Meshtastic, the later
    # entry in discord_notify._PROTOCOL_NAMES) is dropped entirely rather
    # than truncated, same "drop the least essential whole embed, never
    # hand Discord a value it will itself reject" rule.
    while len(embeds) > 1 and discord_notify._total_embed_chars(embeds) > discord_notify._MAX_TOTAL_EMBED_CHARS:
        embeds.pop()

    return {"username": cfg["username"] or "MeshWars", "embeds": embeds}


def _content_hash(base_payload: dict) -> str:
    """SHA-256 over `base_payload` (build_leaderboard()'s own return --
    username + embeds, nothing else) -- see this module's own docstring
    for why the "as of" line and allowed_mentions must never be part of
    what this hashes: either would make the hash change on every single
    pass regardless of whether the actual standings did, defeating the
    entire point of an edit-only-when-changed message.
    """
    return hashlib.sha256(json.dumps(base_payload, sort_keys=True).encode("utf-8")).hexdigest()


def _finalize_payload(base_payload: dict, changed_at: int) -> dict:
    """`base_payload` plus the two things deliberately excluded from
    hashing: the "as of" line (a Discord relative timestamp,
    <t:UNIX:R>, meaning "last changed," not "last checked" -- see this
    module's own docstring) as the message's own `content`, and
    `allowed_mentions.parse == []` (this bot must never be able to ping
    anyone, same rule every app/discord_interactions.py response
    follows). `changed_at` is the caller's own now -- passed in rather
    than read here so a re-PATCH of unchanged content (force=True with no
    real edit) never has to fabricate a new "changed" time it isn't
    actually reporting.
    """
    payload = dict(base_payload)
    payload["content"] = f"*Last changed <t:{changed_at}:R>*"
    payload["allowed_mentions"] = _ALLOWED_MENTIONS
    return payload


# ---------------------------------------------------------------------
# discord_pinned_message state
# ---------------------------------------------------------------------


def _load_pinned(conn, kind: str) -> dict | None:
    row = conn.execute(
        "SELECT webhook_id, channel_id, message_id, content_hash, pinned, updated_at "
        "  FROM discord_pinned_message WHERE kind = ?",
        (kind,),
    ).fetchone()
    return dict(row) if row is not None else None


async def _save_pinned(
    kind: str, webhook_id: str, channel_id: str, message_id: str, content_hash: str, pinned: bool, now: int
) -> None:
    """Full upsert -- used whenever the message ITSELF is new (a first
    post, a repost after a 404, or a fresh post through a moved webhook):
    every column is replaced, never merged, since none of the old row's
    values describe the new message.
    """
    async with WriteSession() as conn:
        conn.execute(
            "INSERT INTO discord_pinned_message"
            "   (kind, webhook_id, channel_id, message_id, content_hash, pinned, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(kind) DO UPDATE SET "
            "  webhook_id = excluded.webhook_id, channel_id = excluded.channel_id, "
            "  message_id = excluded.message_id, content_hash = excluded.content_hash, "
            "  pinned = excluded.pinned, updated_at = excluded.updated_at",
            (kind, webhook_id, channel_id, message_id, content_hash, int(pinned), now),
        )


async def _update_hash(kind: str, content_hash: str, now: int) -> None:
    """Record a successful in-place edit -- webhook_id/channel_id/
    message_id/pinned are all untouched, since editing a message changes
    none of them."""
    async with WriteSession() as conn:
        conn.execute(
            "UPDATE discord_pinned_message SET content_hash = ?, updated_at = ? WHERE kind = ?",
            (content_hash, now, kind),
        )


async def _update_pinned_flag(kind: str, pinned: bool) -> None:
    async with WriteSession() as conn:
        conn.execute(
            "UPDATE discord_pinned_message SET pinned = ? WHERE kind = ?", (int(pinned), kind)
        )


# ---------------------------------------------------------------------
# The update pass
# ---------------------------------------------------------------------


async def _post_new(
    webhook_url: str, webhook_id: str, base_payload: dict, new_hash: str, now: int,
    *, http_client: httpx.AsyncClient | None = None,
) -> dict:
    """POST a brand-new leaderboard message with `?wait=true` (Discord's
    response body carries the new message's own id and channel_id -- the
    whole reason this module never uses app/discord_notify.py's own
    _post(), which discards the body), pin it with the bot, and store the
    row wholesale. Used for a first-ever post, a repost after a person
    deletes the message (a PATCH 404), and a fresh post through a moved
    webhook -- see run_leaderboard_pass() for all three call sites.
    """
    payload = _finalize_payload(base_payload, now)
    resp = await _webhook_request("POST", f"{webhook_url}?wait=true", payload, http_client=http_client)
    if not (200 <= resp.status_code < 300):
        raise discord_notify.DiscordSendError(
            f"discord leaderboard post returned HTTP {resp.status_code}{_error_detail(resp)}"
        )
    body = resp.json()
    channel_id = str(body["channel_id"])
    message_id = str(body["id"])
    pinned = await discord_bot.pin_message(channel_id, message_id, http_client=http_client)
    await _save_pinned(_KIND, webhook_id, channel_id, message_id, new_hash, pinned, now)
    return {
        "ok": True, "reason": "posted",
        "channel_id": channel_id, "message_id": message_id, "pinned": pinned,
    }


async def _handle_same_webhook(
    webhook_url: str, stored: dict, base_payload: dict, new_hash: str, now: int,
    *, force: bool, http_client: httpx.AsyncClient | None = None,
) -> dict:
    """The stored message's webhook still matches the one resolved this
    pass -- edit it in place if the content actually changed, and
    (whenever it was edited, OR `force` asked for a repair regardless)
    re-assert the pin. Never issues a POST unless the stored message
    itself turns out to be gone (a PATCH 404, handled by falling through
    to _post_new()).
    """
    hash_changed = stored["content_hash"] != new_hash
    if not hash_changed and not force:
        return {"ok": True, "reason": "unchanged"}

    if hash_changed:
        payload = _finalize_payload(base_payload, now)
        resp = await _webhook_request(
            "PATCH", f"{webhook_url}/messages/{stored['message_id']}", payload, http_client=http_client
        )
        if resp.status_code == 404:
            # A person deleted the message -- nothing left to edit.
            # Same webhook as before (only the MESSAGE is gone, not the
            # webhook), so this is a fresh _post_new(), not the
            # webhook-moved path.
            return await _post_new(
                webhook_url, stored["webhook_id"], base_payload, new_hash, now, http_client=http_client
            )
        if not (200 <= resp.status_code < 300):
            raise discord_notify.DiscordSendError(
                f"discord leaderboard edit returned HTTP {resp.status_code}{_error_detail(resp)}"
            )
        await _update_hash(_KIND, new_hash, now)

    # Reached for a real edit (hash_changed), or a forced repair with
    # nothing to edit at all (force and not hash_changed) -- either way
    # the pin is reasserted here; see run_leaderboard_pass()'s own
    # docstring for why "Post / repair now" always does this even when
    # the content itself is unchanged (an operator may have unpinned it
    # by hand).
    pinned = await discord_bot.pin_message(stored["channel_id"], stored["message_id"], http_client=http_client)
    await _update_pinned_flag(_KIND, pinned)
    return {"ok": True, "reason": "edited" if hash_changed else "repinned", "pinned": pinned}


async def _handle_webhook_moved(
    webhook_url: str, webhook_id: str, stored: dict, base_payload: dict, new_hash: str, now: int,
    *, http_client: httpx.AsyncClient | None = None,
) -> dict:
    """An operator repointed kind="leaderboard" at a different webhook
    since the stored row was written. The OLD message is unpinned with
    the bot, best-effort (a 404 -- already unpinned, or gone -- is
    ignored, see discord_bot.unpin_message()'s own docstring) -- it
    cannot be DELETED: this bot holds no Manage Messages grant, and the
    OLD webhook's own bearer token was never something this table stored
    in the first place (discord_pinned_message.webhook_id is the numeric
    id only -- see that column's own comment in app/db.py). A fresh
    message is then posted and pinned through the NEW webhook, and the
    row is replaced wholesale -- nothing about the old row is worth
    carrying over once its webhook_id no longer matches.
    """
    await discord_bot.unpin_message(stored["channel_id"], stored["message_id"], http_client=http_client)
    return await _post_new(webhook_url, webhook_id, base_payload, new_hash, now, http_client=http_client)


async def run_leaderboard_pass(*, force: bool = False, http_client: httpx.AsyncClient | None = None) -> dict:
    """One leaderboard update pass -- see this module's own docstring for
    the full five-case contract. `force=True` is app/admin_ops.py's own
    "Post / repair now" button: bypasses maybe_run_leaderboard()'s
    interval gate (this function has none of its own to bypass) and, when
    the content hasn't actually changed, still re-asserts the pin without
    issuing a PATCH nobody asked for -- see _handle_same_webhook().

    Updates _last_leaderboard_run_at UNCONDITIONALLY at the very start,
    before any await -- same reasoning app/discord_bot.py's
    reconcile_all() gives for its own gate: a manual run and the
    periodic loop must never race into overlapping passes, and the
    periodic loop's next tick correctly waits out a full interval from
    whichever call happened most recently, manual or scheduled.

    Never raises: a DiscordSendError from anywhere in this pass (a
    transport failure, a non-2xx/non-404 response) is caught, logged with
    only its own already-sanitised message, and reported as a failed
    pass for the next interval to retry -- same "one bad cycle must never
    crash the loop" contract every other background pass in this
    codebase already follows.
    """
    global _last_leaderboard_run_at
    _last_leaderboard_run_at = time.monotonic()

    conn = connect()
    try:
        cfg = discord_notify.load_discord_config(conn)
        if not cfg.get("leaderboard_enabled"):
            return {"ok": False, "reason": "leaderboard disabled"}
        channels = discord_notify.load_discord_channels(conn)
        webhook_url = discord_notify.resolve_discord_webhook(cfg, channels, _KIND)
        if webhook_url is None:
            return {"ok": False, "reason": "no webhook routed for kind=leaderboard"}
        top_n = cfg.get("leaderboard_top_n") or _DEFAULT_TOP_N
        base_payload = build_leaderboard(conn, cfg, top_n)
        stored = _load_pinned(conn, _KIND)
    finally:
        conn.close()

    if base_payload is None:
        return {"ok": True, "reason": "nothing to show (no active seasons)"}

    new_hash = _content_hash(base_payload)
    webhook_id = _webhook_id_from_url(webhook_url)
    now = int(time.time())

    try:
        if stored is None:
            return await _post_new(webhook_url, webhook_id, base_payload, new_hash, now, http_client=http_client)
        if stored["webhook_id"] != webhook_id:
            return await _handle_webhook_moved(
                webhook_url, webhook_id, stored, base_payload, new_hash, now, http_client=http_client
            )
        return await _handle_same_webhook(
            webhook_url, stored, base_payload, new_hash, now, force=force, http_client=http_client
        )
    except discord_notify.DiscordSendError as e:
        log.warning("discord leaderboard: pass failed, will retry next interval: %s", e)
        return {"ok": False, "reason": str(e)}


# Monotonic timestamp of the last time run_leaderboard_pass() actually
# ran (whether it did real work or no-op'd on leaderboard_enabled),
# managed entirely by run_leaderboard_pass() itself -- same
# "manual and periodic runs share one gate" shape
# app/discord_bot.py's _last_reconcile_gate_at uses for reconcile_all().
_last_leaderboard_run_at = 0.0


async def maybe_run_leaderboard(*, http_client: httpx.AsyncClient | None = None) -> dict | None:
    """Interval-gated wrapper app/discord_notify.py's run_forever() calls
    once per its own poll cycle -- same shape as app/discord_bot.py's
    maybe_reconcile_roles() for reconcile_all(), except the interval
    itself is a DB-backed, admin-editable setting
    (discord_config.leaderboard_interval_seconds), not a fixed module
    constant, since a leaderboard pass does real work (every board's live
    standings plus three Top Operators rankings) that an operator may
    reasonably want tighter or looser than the 10-minute default.
    Returns run_leaderboard_pass()'s own result on a cycle that actually
    runs it, or None when the interval hasn't elapsed since the last run
    (manual or scheduled) -- the gate is skipped this tick, nothing else
    is read or called.
    """
    conn = connect()
    try:
        cfg = discord_notify.load_discord_config(conn)
    finally:
        conn.close()
    interval = cfg.get("leaderboard_interval_seconds") or 600
    if time.monotonic() - _last_leaderboard_run_at < interval:
        return None
    return await run_leaderboard_pass(http_client=http_client)


def leaderboard_admin_status(conn, cfg: dict) -> dict:
    """The leaderboard's current state for GET /api/admin/discord's own
    response: whether a message has ever been posted, a jump link built
    from discord_config.guild_id (the SAME guild id app/discord_bot.py's
    role sync already uses -- discord_pinned_message has no guild id of
    its own, since a Discord message only ever lives in the one guild its
    channel belongs to), whether it is currently pinned, and when its
    content last changed. Never raises -- a never-posted leaderboard or a
    blank guild_id (no jump link possible yet) both degrade to empty
    values rather than an error.
    """
    row = _load_pinned(conn, _KIND)
    if row is None:
        return {"posted": False, "jump_url": "", "pinned": False, "updated_at": 0}
    guild_id = cfg.get("guild_id") or ""
    jump_url = (
        f"https://discord.com/channels/{guild_id}/{row['channel_id']}/{row['message_id']}"
        if guild_id else ""
    )
    return {
        "posted": True,
        "jump_url": jump_url,
        "pinned": bool(row["pinned"]),
        "updated_at": row["updated_at"],
    }
