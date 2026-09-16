"""A logging.Filter that redacts Discord webhook tokens out of httpx's
own request-logging lines before they reach any handler.

Why this exists: app/discord_notify.py's _post() and
app/discord_interactions.py's deferred-followup POST both hand a full
Discord webhook/interaction URL to httpx.AsyncClient -- and httpx's own
"httpx" logger (not this app's code) logs every request it makes at
INFO as `HTTP Request: <METHOD> <full URL> "<status line>"`. Since a
Discord webhook URL embeds its own bearer token in the path
(/api/webhooks/<id>/<token>, or the equivalent
/api/v10/webhooks/<app_id>/<interaction_token>/... deferred-followup
form), that line puts a live credential straight into the container
log on every announcement -- independent of anything app/discord_notify.py
itself logs. See app/main.py, which attaches this filter to the
"httpx" logger at startup; app/discord_notify.py's own module
docstring describes the split.

The filter does NOT lower the httpx logger's level or drop any record
-- its other lines (connection errors, non-webhook requests, ...) stay
exactly as they are; only the token segment of a webhook path is
replaced, everywhere it appears in a record's `msg` or `args`.
"""
from __future__ import annotations

import logging
import re

# Matches the token segment of a Discord webhook path, across any API
# version httpx might see it through (bare /api/webhooks/... and the
# versioned /api/v10/webhooks/... form the interaction deferred-followup
# path also uses -- app/discord_interactions.py's own
# .../webhooks/{app_id}/{token}/messages/@original). Keeps:
#   - everything before the token (host, /api[/vN], /webhooks/<id>/)
#   - anything AFTER the token (/messages/@original, /github, ...)
# and replaces only the token itself, so the id and any trailing path
# stay readable.
_WEBHOOK_TOKEN_RE = re.compile(
    r"(/api(?:/v\d+)?/webhooks/\d+/)[^/\s\"]+"
)

_REDACTED = r"\1<redacted>"


def _redact_text(value: str) -> str:
    """Replace a webhook token in `value` if one is present; otherwise
    return `value` unchanged. Never raises."""
    try:
        return _WEBHOOK_TOKEN_RE.sub(_REDACTED, value)
    except Exception:
        return value


def _redact_any(value):
    """Redact `value` if it looks like text (str, or something whose
    str() might contain a URL -- e.g. httpx.URL), leaving every other
    type (ints, status codes, ...) untouched. Never raises."""
    if isinstance(value, str):
        return _redact_text(value)
    # httpx passes the request URL as an httpx.URL (or similar
    # object), not a str, in its log record args. str()-ing it is safe
    # for any of httpx's own arg types (URL, method name, status
    # line) -- but guard it anyway, since this filter must never raise
    # regardless of what shows up in args.
    try:
        text = str(value)
    except Exception:
        return value
    redacted = _redact_text(text)
    return redacted if redacted != text else value


class DiscordWebhookRedactionFilter(logging.Filter):
    """logging.Filter that rewrites a Discord webhook token out of a
    LogRecord's `msg` and `args` in place, then always lets the record
    through (a Filter's job is to decide pass/drop; this one always
    returns True -- redact, never suppress).

    Safe to attach to any logger, but exists to sit on the "httpx"
    logger specifically (see app/main.py) since that is the only
    logger in this codebase that ever sees a webhook URL as a
    request target rather than as an app-level secret already handled
    by app/discord_notify.py's own "never log the URL" discipline.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = _redact_text(record.msg)

            args = record.args
            if isinstance(args, tuple):
                record.args = tuple(_redact_any(a) for a in args)
            elif isinstance(args, dict):
                record.args = {k: _redact_any(v) for k, v in args.items()}
            # else: no args, or an unrecognized shape -- leave alone.
        except Exception:
            # Never let a redaction bug break logging or hide a record.
            pass
        return True
