"""Tests for app/log_redact.py -- the logging.Filter that strips a
Discord webhook token out of httpx's own request-logging line before
it reaches a handler (see app/main.py, which attaches this filter to
the "httpx" logger, and app/log_redact.py's own module docstring for
why: httpx logs every request's full URL at INFO regardless of
anything app/discord_notify.py or app/discord_interactions.py do).

Tests 1-5 build a logging.LogRecord by hand, matching the exact shape
httpx's own logger.info(...) call uses (see
httpx._client.py: 'HTTP Request: %s %s "%s %d %s"' with args
(method, url, http_version, status_code, reason_phrase)) rather than
going through the logging module, so each case is a precise, isolated
check of the filter's own record-mutation logic. Test 6 is the one
end-to-end case: a real logger, this filter attached, and an actual
httpx request through httpx.MockTransport to a fake webhook URL,
asserting the fake token never appears in the captured output --
exactly the path a real deployment exercises.
"""
from __future__ import annotations

import logging

import httpx
import pytest

from app.log_redact import DiscordWebhookRedactionFilter


def _record(msg, args) -> logging.LogRecord:
    """Build a LogRecord with an arbitrary `args` shape.

    LogRecord.__init__ itself special-cases a single-dict-arg call
    (the '%(key)s' style) by probing args[0], which raises for some of
    the deliberately odd shapes these tests want to hand the FILTER
    (not the record's own constructor). So construct with an empty,
    always-safe args tuple, then set the real value directly -- this
    is exactly the object shape our filter must tolerate either way,
    since it reads `record.args` itself rather than reconstructing it.
    """
    rec = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    rec.args = args
    return rec


def test_redacts_webhook_token_keeps_id():
    """A record shaped exactly like httpx's own request-log line, with
    the URL as an httpx.URL object (as httpx itself passes it) --
    the token is replaced, the numeric webhook id is kept."""
    url = httpx.URL(
        "https://discord.com/api/webhooks/1549602148621488223/"
        "SUPERSECRETTOKENabc123"
    )
    rec = _record(
        'HTTP Request: %s %s "%s %d %s"',
        ("POST", url, "HTTP/1.1", 204, "No Content"),
    )

    assert DiscordWebhookRedactionFilter().filter(rec) is True

    rendered = rec.getMessage()
    assert "SUPERSECRETTOKENabc123" not in rendered
    assert "/webhooks/1549602148621488223/<redacted>" in rendered
    assert "204" in rendered and "No Content" in rendered


def test_redacts_versioned_interaction_followup_url_keeps_suffix():
    """The slash-command deferred-followup URL
    (.../api/v10/webhooks/<app_id>/<token>/messages/@original) keeps
    the trailing /messages/@original readable and redacts only the
    token."""
    url = httpx.URL(
        "https://discord.com/api/v10/webhooks/999888777/"
        "aVeryRealInteractionToken.xyz/messages/@original"
    )
    rec = _record('HTTP Request: %s %s "%s %d %s"', ("PATCH", url, "HTTP/1.1", 200, "OK"))

    DiscordWebhookRedactionFilter().filter(rec)

    rendered = rec.getMessage()
    assert "aVeryRealInteractionToken.xyz" not in rendered
    assert "/webhooks/999888777/<redacted>/messages/@original" in rendered


def test_redacts_github_suffixed_webhook_keeps_suffix():
    """A webhook URL configured as a GitHub-style integration
    (.../webhooks/<id>/<token>/github) keeps the /github suffix."""
    url = httpx.URL(
        "https://discord.com/api/webhooks/42/topsecrettoken/github"
    )
    rec = _record('HTTP Request: %s %s "%s %d %s"', ("POST", url, "HTTP/1.1", 204, "No Content"))

    DiscordWebhookRedactionFilter().filter(rec)

    rendered = rec.getMessage()
    assert "topsecrettoken" not in rendered
    assert "/webhooks/42/<redacted>/github" in rendered


def test_non_webhook_discord_url_unchanged():
    """A Discord API URL that isn't a webhook (e.g. the 404 guild-member
    lookup line quoted in the bug report) passes through byte-for-byte
    unchanged."""
    url = httpx.URL("https://discord.com/api/v10/guilds/111/members/222")
    rec = _record(
        'HTTP Request: %s %s "%s %d %s"',
        ("GET", url, "HTTP/1.1", 404, "Not Found"),
    )
    original = rec.getMessage()

    DiscordWebhookRedactionFilter().filter(rec)

    assert rec.getMessage() == original
    assert "/guilds/111/members/222" in rec.getMessage()


@pytest.mark.parametrize(
    "msg, args",
    [
        ("plain message, no args at all", None),
        ("plain message, empty args", ()),
        ("odd args shape: %s", "not-a-tuple-or-dict"),
        ("odd args shape: %s", 12345),
        ("dict-style: %(thing)s", {"thing": object()}),
        ("weird arg object: %s", (object(),)),
    ],
)
def test_no_args_or_odd_args_passes_through_without_raising(msg, args):
    """A record with no args, or an args shape this filter doesn't
    expect, must never raise and must still be let through."""
    rec = _record(msg, args)

    result = DiscordWebhookRedactionFilter().filter(rec)

    assert result is True


def test_end_to_end_fake_token_never_appears_in_captured_log(caplog):
    """Configure the "httpx" logger with this filter, make a real
    request through httpx.MockTransport to a webhook URL carrying a
    known fake token, and assert that token string never appears
    anywhere in the captured log output -- the actual path a live
    deployment exercises when app/discord_notify.py's _post() (or
    app/discord_interactions.py's deferred-followup POST) calls
    httpx.AsyncClient.post()."""
    import asyncio

    fake_token = "e2e-fake-token-should-never-be-logged-9f8e7d"
    webhook_url = f"https://discord.com/api/webhooks/123456789/{fake_token}"

    httpx_logger = logging.getLogger("httpx")
    redaction_filter = DiscordWebhookRedactionFilter()
    httpx_logger.addFilter(redaction_filter)
    try:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(204)

        async def _do_post():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await client.post(webhook_url, json={"content": "hi"})

        with caplog.at_level(logging.INFO, logger="httpx"):
            asyncio.run(_do_post())
    finally:
        httpx_logger.removeFilter(redaction_filter)

    full_output = "\n".join(rec.getMessage() for rec in caplog.records)
    assert fake_token not in full_output
    assert "123456789" in full_output
    assert "<redacted>" in full_output
