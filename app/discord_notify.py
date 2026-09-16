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
setting's value. A timeout or transport failure raises a short, fixed
message naming the failure kind, never the request or its URL, because
httpx's own exception text embeds the request (and so the URL) --
but a non-2xx HTTP response is a different case: the RESPONSE body is
Discord describing what was wrong with the payload it received, not a
credential, so a snippet of it is included to make a bad announcement
diagnosable. See _post()'s own docstring for the line between the two.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

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
    """Sum of title + description + each field's name/value, across
    every embed given -- the same text Discord counts toward its own
    6000-character total-embed budget (_MAX_TOTAL_EMBED_CHARS above).
    Author/footer/thumbnail text also count on Discord's side, but this
    module never sets any of those, so they would only ever contribute
    zero and are left out of the sum entirely.
    """
    total = 0
    for embed in embeds:
        total += len(embed.get("title") or "")
        total += len(embed.get("description") or "")
        for f in embed.get("fields") or []:
            total += len(f.get("name") or "")
            total += len(f.get("value") or "")
    return total


def announcements_enabled() -> bool:
    """True only when a webhook URL is configured -- mirrors
    app/oauth.py's provider_enabled(): empty means off, never open, so
    a fresh install with nothing configured never accumulates an outbox
    backlog (enqueue() below is a no-op while this is False) and
    run_forever()'s loop does nothing each cycle rather than trying to
    post to an empty string.
    """
    return bool(settings.discord_webhook_announcements)


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


async def _post(payload: dict, *, http_client: httpx.AsyncClient | None = None) -> None:
    """POST one already-built Discord message body to the configured
    webhook. Raises DiscordSendError on a non-2xx response or any
    transport failure; never returns anything on success.

    `http_client` is accepted purely so tests can hand this an
    httpx.AsyncClient wired to an httpx.MockTransport (the same
    injectable-client shape app/oauth.py's exchange_code() already uses
    for its own outbound call) -- every real caller leaves it None and
    a short-lived client is opened and closed around this one request,
    since a month freezes at most a handful of times a year and there
    is no benefit to keeping a pooled connection open between them.
    """
    url = settings.discord_webhook_announcements
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


def enqueue(conn, kind: str, key: str, payload: dict, now: int) -> None:
    """Queue one announcement -- SYNC, and takes the CALLER's own
    connection, so it runs inside whatever transaction the caller is
    already holding (app/results.py's freeze_month(), inside the same
    WriteSession/BEGIN IMMEDIATE block that just wrote month_result/
    month_standing/month_award). Nothing here opens its own transaction
    or connection.

    A no-op when announcements are disabled (announcements_enabled() is
    False) -- a fresh or webhook-less install must never accumulate a
    discord_outbox backlog it will never drain, only to dump all of it
    the moment an operator finally configures a webhook months later.

    INSERT OR IGNORE on discord_outbox's UNIQUE(kind, key) index is the
    exactly-once guarantee: a duplicate (kind, key) -- the admin
    re-freeze route calling freeze_month() again for an already-frozen
    month, most likely -- is silently dropped, never a second row and
    never a second post.
    """
    if not announcements_enabled():
        return
    conn.execute(
        "INSERT OR IGNORE INTO discord_outbox(kind, key, payload, created_at) VALUES (?, ?, ?, ?)",
        (kind, key, json.dumps(payload), now),
    )


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


def _value_detail_tail(value, detail) -> str:
    """Join a formatted `value` with its `detail`, for one award --
    shared by _award_line() and _team_award_line() so both render the
    same way.

    frontend/results.js's renderHonors() shows `value` and `detail` in
    two separate visual columns, so an award whose hand-written detail
    already restates its own number -- e.g. value=169.0,
    detail="169 s after the net opened" for 'quick_fingers' -- shows no
    visible duplication there; the number is simply repeated in two
    places on screen. A Discord field value is a single line of text
    though, so the same data reads as a stutter: "169 169 s after the
    net opened". This checks whether `detail` already begins with the
    number -- either _fmt_number()'s comma-formatted form or the plain
    integer string, since a hand-written detail will never carry a
    thousands separator -- and drops the duplicate numeric prefix when
    it does. The match only fires at the start of `detail` and only
    when followed by a space or end-of-string, so "9763 ft" matches
    value=9763 but "1690 squares past the towns" does NOT falsely match
    value=169 (169 is a prefix of "1690", but "169 " is not).

    Deliberately generic rather than keyed on the 'quick_fingers' award
    name: any future award whose detail embeds its own number would hit
    the exact same stutter here.
    """
    formatted = _fmt_number(value) if value is not None else None
    if formatted and detail:
        prefixes = {formatted}
        n = float(value)
        if n == int(n):
            prefixes.add(str(int(n)))
        if any(detail == p or detail.startswith(p + " ") for p in prefixes):
            return detail
    return " ".join(x for x in (formatted, detail) if x)


def _award_line(a: dict) -> str:
    """who -- value detail, for one non-placeholder award. Renders all
    three of who, the number, and its unit -- frontend/results.js's own
    renderHonors() shows all three for the same reason its comment
    gives: "Top NetOp 130" without a unit is the exact ambiguity the
    detail exists to fix, and a bare who with no number at all (this
    module's old bug) is that same ambiguity made worse. See
    _value_detail_tail() for why a detail that already restates the
    number (quick_fingers) is de-duplicated here even though the site
    shows both -- a Discord field is one line, not two columns.
    """
    who = a.get("player") or a.get("team") or "Unknown"
    tail = _value_detail_tail(a.get("value"), a.get("detail"))
    return f"{who} -- {tail}" if tail else who


def _team_award_line(a: dict, emoji: dict[str, str]) -> str:
    """One compact line inside a grouped per-team field: "TEAM: <rest>",
    prefixed with that team's coloured dot (_team_dot()) when
    configured. Most per-team awards (team_attacker, team_defender,
    ...) are a property of the team itself, so `who` (player() or
    team()) is just the scope team again -- "GREEN: GREEN -- 40 squares
    taken" says GREEN twice for nothing, so the leading "TEAM: " prefix
    stands in for `who` and _award_line's own who is dropped in that
    case. A per-team award that DOES name a player distinct from its
    scope (a team's own top scorer, say) keeps that player's name after
    the team prefix instead -- but the dot is always keyed on `scope`
    (the team the line is grouped under), never the player.
    """
    scope = a.get("scope") or ""
    who = a.get("player") or a.get("team") or "Unknown"
    dot = _team_dot(emoji, scope)
    if who == scope:
        tail = _value_detail_tail(a.get("value"), a.get("detail"))
        return f"{dot}{scope}: {tail}" if tail else f"{dot}{scope}"
    return f"{dot}{scope}: {_award_line(a)}"


def build_month_honors_embed(month: str, protocol: str, result: dict) -> dict:
    """The full Discord webhook JSON body (an `embeds` list of up to
    three embeds -- Standings, Honors, By team; see below for when the
    latter two are omitted) for one frozen month's result, as returned
    by app/results.py's compute_month()/freeze_month() -- standings and
    awards. Plain text throughout, with exactly one deliberate
    exception: a per-team coloured-dot custom emoji (settings.
    discord_team_emoji, parsed by _parse_team_emoji()) prefixed onto
    every line that names a team, when an operator has configured one
    for that team -- see _team_dot()'s own comment for the fallback
    that keeps a deployment without one rendering exactly as before.
    This is the ONLY emoji this module ever emits; nothing else here
    invents its own.

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

    proto_label = _PROTOCOL_NAMES.get(protocol, protocol)
    emoji = _parse_team_emoji(settings.discord_team_emoji)

    standings = sorted(
        result.get("standings") or [],
        key=lambda s: (-(s.get("squares") or 0), s.get("team") or ""),
    )
    if standings:
        standings_text = "\n".join(
            f"{_team_dot(emoji, s.get('team'))}{s.get('team')}: "
            f"{_fmt_number(s.get('squares', 0))} squares held"
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
        lines = "\n".join(_team_award_line(a, emoji) for a in group)
        # inline=False, deliberately unlike headline_fields above: each
        # of these values is a multi-line list (one line per team), and
        # squeezing a multi-line list into a third of the message width
        # would be unreadable rather than merely dense.
        team_fields.append({"name": label, "value": lines, "inline": False})

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
    # posted. Falls back to the literal "MeshWars" if the setting is
    # ever blanked out -- see settings.discord_webhook_username's own
    # comment for why this is the one field in this section that is
    # never simply omitted when unset.
    return {
        "username": settings.discord_webhook_username or "MeshWars",
        "embeds": embeds,
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
    broken webhook must eventually stop being retried). Both are plain
    WHERE clauses, not a Python-side filter, so a skipped row is never
    even fetched.
    """
    if not announcements_enabled():
        return

    now = int(time.time())
    cutoff = now - settings.discord_outbox_max_age_hours * 3600

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, payload, attempts FROM discord_outbox "
            " WHERE posted_at IS NULL AND created_at >= ? AND attempts < ? "
            " ORDER BY id",
            (cutoff, settings.discord_outbox_max_attempts),
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
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
            await _post(payload, http_client=http_client)
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


async def run_forever() -> None:
    """Background poll loop over discord_outbox -- started
    UNCONDITIONALLY by app/main.py's lifespan, the same "the loop must
    stay alive to notice a later config change" reasoning
    app/freqmapper_ingest.py's own run_forever() docstring gives for
    `enabled`: announcements_enabled() is a runtime setting (an
    operator can add DISCORD_WEBHOOK_ANNOUNCEMENTS and restart, or a
    future admin route could flip it live), so a fresh install with no
    webhook configured yet still starts this task, but it simply does
    nothing each cycle until one is set.

    Never raises out of the loop -- same fire-and-forget contract
    app/account_api.py's _notify_security() applies to a single mail
    send, extended here to a whole poll cycle: a bug handling one
    cycle's rows must never crash the process or stop later cycles
    (and later months' announcements) from ever running again.
    """
    log.info("discord outbox loop starting (announcements gated by discord_webhook_announcements)")
    while True:
        try:
            await _drain_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("discord outbox: drain cycle failed")
        await asyncio.sleep(max(settings.discord_outbox_poll_interval_seconds, 1))
