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
setting's value, and every error it raises is a short, fixed message
naming the failure kind, never the request or its URL -- see that
function's own docstring.
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

# protocol -> the name a reader recognizes, for the embed's own text.
# 'mc'/'mt' are exactly the bare literals every other module in this
# codebase uses for the two boards (see app/results.py's own module
# docstring) -- this is purely a DISPLAY table, not a third copy of the
# protocol discriminator itself.
_PROTOCOL_NAMES = {"mc": "MeshCore", "mt": "Meshtastic"}


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

    Every message this carries is a short, fixed string naming the
    failure kind (a timeout, a transport error, a non-2xx status) --
    never the request itself, since httpx's own exception and request
    reprs include the URL, and a Discord webhook URL carries its own
    auth token in the path. See this module's own docstring.
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
            raise DiscordSendError("discord webhook post timed out") from e
        except httpx.HTTPError as e:
            # Deliberately not str(e) -- see this module's own
            # docstring and DiscordSendError's: httpx's own exception
            # text embeds the request URL, and that URL is this
            # deployment's webhook credential.
            raise DiscordSendError(f"discord webhook post failed ({type(e).__name__})") from e
        if resp.status_code < 200 or resp.status_code >= 300:
            raise DiscordSendError(f"discord webhook post returned HTTP {resp.status_code}")
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
            f"{s.get('team')}: {s.get('squares', 0)} squares held" for s in standings
        )
    else:
        standings_text = "No standings recorded."

    award_fields = []
    for a in result.get("awards") or []:
        if a.get("player_id") is None and a.get("team") is None:
            continue  # unwon placeholder -- see with_placeholders() -- nothing to announce
        label = a.get("label") or results.AWARD_LABELS.get(a.get("award"), a.get("award"))
        name = f"{label} ({a['scope']})" if a.get("scope") else label
        who = a.get("player") or a.get("team") or "Unknown"
        detail = a.get("detail")
        value = f"{who} -- {detail}" if detail else who
        award_fields.append({"name": name, "value": value, "inline": False})

    base_url = (settings.oauth_public_base_url or "").rstrip("/")
    results_url = f"{base_url}/results" if base_url else "/results"

    embed = {
        "title": f"{proto_label} results: {month}",
        "description": f"Standings:\n{standings_text}",
        "url": results_url,
        "fields": award_fields,
    }
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
