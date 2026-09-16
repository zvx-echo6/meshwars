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


def _team_award_line(a: dict) -> str:
    """One compact line inside a grouped per-team field: "TEAM: <rest>".
    Most per-team awards (team_attacker, team_defender, ...) are a
    property of the team itself, so `who` (player() or team()) is just
    the scope team again -- "GREEN: GREEN -- 40 squares taken" says
    GREEN twice for nothing, so the leading "TEAM: " prefix stands in
    for `who` and _award_line's own who is dropped in that case. A
    per-team award that DOES name a player distinct from its scope
    (a team's own top scorer, say) keeps that player's name after the
    team prefix instead.
    """
    scope = a.get("scope") or ""
    who = a.get("player") or a.get("team") or "Unknown"
    if who == scope:
        tail = _value_detail_tail(a.get("value"), a.get("detail"))
        return f"{scope}: {tail}" if tail else scope
    return f"{scope}: {_award_line(a)}"


def build_month_honors_embed(month: str, protocol: str, result: dict) -> dict:
    """The full Discord webhook JSON body (an `embeds` list, one embed)
    for one frozen month's result, as returned by
    app/results.py's compute_month()/freeze_month() -- standings and
    awards. Plain text only, no emoji anywhere, matching this module's
    own no-emoji rule.

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

    standings = sorted(
        result.get("standings") or [],
        key=lambda s: (-(s.get("squares") or 0), s.get("team") or ""),
    )
    if standings:
        standings_text = "\n".join(
            f"{s.get('team')}: {_fmt_number(s.get('squares', 0))} squares held" for s in standings
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
            headline_fields.append({"name": label, "value": _award_line(a), "inline": False})

    award_fields = headline_fields
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
        lines = "\n".join(_team_award_line(a) for a in group)
        award_fields.append({"name": label, "value": lines, "inline": False})

    # Belt-and-suspenders: whatever the grouping above produces, never
    # hand Discord more than its own hard limit -- see
    # _MAX_EMBED_FIELDS's own comment for why a 26th field is not a
    # partial failure but a 400 for the whole message.
    award_fields = award_fields[:_MAX_EMBED_FIELDS]

    base_url = (settings.oauth_public_base_url or "").rstrip("/")

    embed = {
        "title": f"{proto_label} results: {month}",
        "description": f"Standings:\n{standings_text}",
        "fields": award_fields,
    }
    # A Discord embed's "url" must be an ABSOLUTE url -- a relative one
    # (e.g. "/results") makes Discord reject the ENTIRE message with an
    # HTTP 400, not just drop the link. So when OAUTH_PUBLIC_BASE_URL
    # isn't configured, omit the "url" key entirely rather than falling
    # back to a relative path. This only bites a deployment that has
    # not set OAUTH_PUBLIC_BASE_URL, which is why it was invisible here.
    if base_url:
        embed["url"] = f"{base_url}/results"
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
