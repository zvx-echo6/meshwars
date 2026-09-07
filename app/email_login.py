"""Passwordless email sign-in: address-shape validation and the actual
mail send. Deliberately the SMTP-and-shape-only half of this feature,
mirroring the split app/oauth.py/app/oauth_api.py already draws for
OAuth providers -- this module never touches a database, a session, or
a cookie, and knows nothing about tokens, rate limits, or the callback
decision tree. app/oauth_api.py is the router that owns all of that
(POST /auth/email/start, GET /auth/email/callback) and calls into this
module only to check whether the feature is configured at all
(email_login_enabled), validate/normalize an address
(looks_like_email/normalize_email), and send one mail
(send_magic_link_email). That split is what makes each half testable on
its own -- this module with nothing but a mocked smtplib, that router
with nothing but this module mocked out (same reasoning
tests/test_oauth.py and tests/test_oauth_api.py already split along for
OAuth).

---- not a Provider(...) table entry ------------------------------------

Email sign-in is NOT added to app/oauth.py's PROVIDERS table, even
though it ends up producing the exact same ProviderIdentity shape that
table's own providers do (see resolve_oauth_callback() in
app/oauth_api.py, which is genuinely provider-agnostic and does not
care whether "email" came from that table or not). There is no
authorize/token/userinfo round trip here, no client id/secret, no PKCE
-- the entire flow is "mail a single-use link, then redeem it," which
is a different shape from every entry that table's own Provider
dataclass was built to describe. What email sign-in DOES share with
every OAuth provider is the account model underneath it and the
callback decision tree that resolves an identity to an account --
that's the reuse this change is actually about, not a forced fit into
a table shape designed for something else.

---- SMTP: stdlib only, sent off the event loop -------------------------

smtplib is blocking, synchronous I/O -- calling it directly from an
async route would stall this process's ENTIRE shared event loop for
however long the SMTP round trip takes (connect, STARTTLS, auth, send),
not just the one request that triggered it: every other in-flight
request, and every background poller sharing this same process (see
app/checkin.py, app/mqtt_subscriber.py), would queue up behind it.
send_magic_link_email() below wraps the real, blocking send in
asyncio.to_thread() for exactly that reason -- the same pattern
app/sessions.py's verify_session() and app/mc_ingest.py's own
authenticate() already use to keep their own blocking sqlite3 calls off
this loop -- and _send_sync() itself must never be awaited or called
directly from async code.
"""
from __future__ import annotations

import asyncio
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

from .config import settings

log = logging.getLogger("email_login")

# Deliberately loose -- this is a SHAPE check ("does this look like an
# address at all"), not an RFC 5322 validator. The only thing that
# actually proves an address is real and controlled by whoever typed it
# is the magic link itself being clicked -- see this module's own
# docstring. Rejects anything with whitespace or more/fewer than one
# '@', and requires at least one '.' after the '@' (a bare "user@host"
# with no TLD-shaped suffix is almost always a typo, not a real
# deliverable address).
_EMAIL_SHAPE_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# RFC 5321's own limit on a complete address -- generous enough for any
# real address, tight enough to refuse an obviously-abusive payload
# before it goes anywhere near a database write or an outbound send.
_MAX_EMAIL_LENGTH = 254


def normalize_email(raw: str) -> str:
    """Trim + lowercase -- the exact normalization account_identity's
    (provider, subject) pair uses for provider='email' (see that
    table's own comment in app/db.py): two people typing
    "User@Example.com" and "user@example.com" must resolve to the same
    subject, the same way every other provider's own subject is already
    a single stable, case-consistent value.
    """
    return raw.strip().lower()


def looks_like_email(address: str) -> bool:
    """Shape check only -- see this module's own docstring and
    _EMAIL_SHAPE_RE's comment for why this is deliberately loose. Called
    AFTER normalize_email() by every caller in this codebase, so
    whitespace-trimming is already done by the time this runs.
    """
    if not address or len(address) > _MAX_EMAIL_LENGTH:
        return False
    return bool(_EMAIL_SHAPE_RE.match(address))


def email_login_enabled() -> bool:
    """Empty smtp_host means email sign-in is off, the same "empty means
    off, never open" contract every optional feature in app/config.py
    uses (join_invite_code, admin_token, app/oauth.py's own
    provider_enabled() for OAuth providers, ...). Also requires
    oauth_public_base_url: the magic link this feature mails out has to
    be an absolute URL a mail client can open from anywhere, and that
    setting already names this exact deployment's own public base
    address -- see its own comment in app/config.py for why this reuses
    it rather than adding a second setting for the same fact.
    """
    return bool(settings.smtp_host) and bool(settings.oauth_public_base_url)


def _mask_for_log(address: str) -> str:
    """Same masking app/account_api.py's _mask_email()/app/oauth_api.py's
    _mask_pending_email() already apply before showing an identity's
    address back through the API -- duplicated here (three lines, same
    reasoning both of those give for their own duplication) so a send
    failure's log line names roughly which address without putting a
    full, potentially-sensitive inbox address in a log file verbatim.
    """
    if not address or "@" not in address:
        return "***"
    local, _, domain = address.partition("@")
    masked_local = local[0] + "***" if local else "***"
    return f"{masked_local}@{domain}"


class EmailSendError(Exception):
    """Raised by send_magic_link_email() on any failure talking to the
    configured SMTP server (connection refused, auth failure, timeout,
    ...). Callers must treat this exactly like a successful send from
    the requester's point of view -- see app/oauth_api.py's
    POST /auth/email/start docstring for why a send failure must never
    produce a different response than success (the same "don't leak
    which part failed" -- here, "don't leak whether it worked at all" --
    posture app/oauth.py's OAuthError already applies to a provider's
    own outage).
    """


# The two things a mailed link can be for. They are NOT interchangeable
# wording on the same event: one hands over a session, the other confirms
# that an address reaches the person who typed it. A confirmation mail that
# says "click here to sign in" is both wrong and alarming -- the recipient
# is being told a link will log somebody in, when it will not.
PURPOSE_SIGN_IN = "sign_in"
PURPOSE_VERIFY_CONTACT = "verify_contact"

_MAIL_COPY = {
    PURPOSE_SIGN_IN: (
        "Your MeshWars sign-in link",
        "Click the link below to sign in to MeshWars:",
    ),
    PURPOSE_VERIFY_CONTACT: (
        "Confirm your MeshWars contact address",
        "Click the link below to confirm this address for MeshWars. "
        "It is where we can reach you -- it will not sign you in:",
    ),
}

# Which frontend/email/ fragment supplies the HTML heading/body/cta/footer
# for each purpose -- see _render_html()'s own comment for how that
# fragment is combined with _shell.html.
_MAIL_TEMPLATES = {
    PURPOSE_SIGN_IN: "sign_in.html",
    PURPOSE_VERIFY_CONTACT: "verify_contact.html",
}

# Same directory-derivation shape app/api.py's own top-level page routes
# use for frontend_dir (Path(__file__).resolve().parent.parent / "frontend")
# -- this module lives at app/email_login.py, so parent.parent is the repo
# root and frontend/email/ sits next to every other frontend/ page.
_EMAIL_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "frontend" / "email"

# Splits a fragment file (frontend/email/sign_in.html,
# frontend/email/verify_contact.html) into its named sections. Each
# section starts with a bare `<!--name-->` marker on its own line --
# deliberately NOT the same {placeholder} syntax _shell.html's own
# tokens use, so a fragment's OWN markers can never collide with a
# {placeholder} still waiting to be replaced (in particular {link_url},
# which lives inside the cta section's own text and must survive this
# split untouched). Requires \w+ with no spaces, so the free-text
# comment at the top of each fragment file (explaining what the file
# is) never accidentally parses as a section.
_FRAGMENT_MARKER_RE = re.compile(r"<!--(\w+)-->")


def _read_email_template(filename: str) -> str:
    """Reads one frontend/email/ file from disk. Called at send time,
    not cached at import -- the same per-call read app/api.py's own
    _templated_html_page() does for every other top-level page, and for
    the same reason: this is public AGPL software, and an operator who
    edits their own copy of these files should see the change on the
    next mail sent, not only after a process restart.
    """
    return (_EMAIL_TEMPLATE_DIR / filename).read_text(encoding="utf-8")


def _parse_fragments(text: str) -> dict[str, str]:
    """Splits a fragment file on its `<!--name-->` markers into
    {name: content} (see _FRAGMENT_MARKER_RE's own comment). re.split()
    with one capturing group returns [text-before-first-marker, name,
    content, name, content, ...] -- the leading element is the fragment
    file's own descriptive comment (never a section body) and is
    dropped here rather than exposed as a fragment.
    """
    parts = _FRAGMENT_MARKER_RE.split(text)
    return {name: content.strip() for name, content in zip(parts[1::2], parts[2::2])}


def _render_html(purpose: str, link_url: str, logo_cid: str) -> str:
    """Builds the HTML part by combining frontend/email/_shell.html (the
    table-based page frame: logo, gold heading, rule, meshwars.com line
    -- shared by every purpose) with the purpose's own fragment file
    (heading/body/cta/footer wording -- see _MAIL_TEMPLATES). Follows
    this codebase's one templating convention end to end (see this
    module's own docstring and app/api.py's _templated_html_page): plain
    HTML files on disk, substituted with str.replace, no templating
    dependency anywhere.

    {link_url} is replaced last, against the fully-assembled page,
    rather than against the fragment alone -- it appears twice inside
    the fragment's own cta section (the button's href and the plaintext
    fallback below it) and one replace after assembly covers both.
    """
    shell = _read_email_template("_shell.html")
    fragments = _parse_fragments(_read_email_template(_MAIL_TEMPLATES[purpose]))
    html = shell.replace("{logo_cid}", logo_cid)
    for name in ("heading", "body", "cta", "footer"):
        html = html.replace("{" + name + "}", fragments[name])
    return html.replace("{link_url}", link_url)


def _send_sync(to_address: str, link_url: str, purpose: str = PURPOSE_SIGN_IN) -> None:
    """The actual blocking SMTP conversation -- stdlib smtplib only, no
    third-party mail library. Never call this directly from async code;
    see send_magic_link_email() below and this module's own docstring.

    `purpose` selects the subject and body. It defaults to sign-in because
    that was this function's only behaviour before contact-address
    confirmation reused it, and a caller that forgets to say what it is
    sending should get the older, narrower wording rather than silence.

    Sends multipart/alternative( text/plain, multipart/related( text/html,
    image/png ) ) -- the standard nesting for an HTML mail with an inline
    image (the image belongs only under the html branch, never as a
    sibling of the plain-text one). The plain-text part is unchanged from
    before this HTML body existed (some mail clients and every spam
    filter still weigh it), the html part is the styled MeshWars page
    built by _render_html(), and the image/png next to it is the logo
    the html part's <img> references by Content-ID rather than a remote
    URL -- inlined so the mail renders with no network fetch and nothing
    for an image-blocking client to strip.
    """
    subject, lead = _MAIL_COPY[purpose]
    from_address = settings.smtp_from_address
    # The domain half of the configured sender address, used only to
    # make the Message-ID look like it belongs to this deployment (RFC
    # 5322 section 3.6.4 -- the right-hand side of a Message-ID is
    # conventionally the sending domain). Never hardcode
    # "meshwars.com" here: this is public AGPL software, and other
    # operators run it under their own domain.
    from_domain = from_address.rpartition("@")[2] or from_address

    msg = EmailMessage()
    msg["Subject"] = subject
    # Display name form ("Display Name <address>") for readability in
    # a mail client -- see settings.smtp_from_name's own comment in
    # app/config.py. This is cosmetic only: the SMTP *envelope* sender
    # (what the mail server actually signs DKIM against and what
    # delivery routes on) is passed explicitly to send_message() below
    # as the bare from_address, never this display-name form.
    msg["From"] = formataddr((settings.smtp_from_name, from_address))
    msg["To"] = to_address
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_domain)
    msg["X-Mailer"] = "MeshWars"
    msg.set_content(
        f"{lead}\n\n"
        f"{link_url}\n\n"
        "This link expires in a few minutes and can only be used once. "
        "If you didn't request it, you can safely ignore this message."
    )

    # make_msgid() already returns the angle-bracketed form
    # ("<unique@domain>") a Content-ID header is supposed to carry --
    # EmailMessage.add_related()'s own cid= kwarg sets the header
    # verbatim, with no bracket handling of its own (see
    # email.contentmanager._finalize_set), so that raw value is exactly
    # right for the header below. The HTML <img> tag's own `cid:` URL is
    # the opposite: RFC 2392 says a cid: URL is the bare content-id with
    # NO angle brackets, so logo_cid strips them before it ever reaches
    # _render_html() -- leaving them in is a common bug that silently
    # breaks the image in most mail clients.
    logo_content_id = make_msgid(domain=from_domain)
    logo_cid = logo_content_id.strip("<>")

    html_body = _render_html(purpose, link_url, logo_cid)
    # add_alternative() promotes this message to multipart/alternative
    # and appends the html part after the existing text/plain one --
    # order matters here, RFC 2046 says the LAST alternative is the
    # richest and mail clients render the last part they understand, so
    # plain text must be set_content()'d first and html added second.
    msg.add_alternative(html_body, subtype="html")
    logo_path = Path(__file__).resolve().parent.parent / "frontend" / "assets" / "logo" / "meshwars-email.png"
    # get_payload()[1] is the html part add_alternative() just appended
    # (index 0 is the plain-text part set_content() created) -- adding
    # the logo image AS related to that specific part, rather than to
    # the top-level message, is what nests it inside
    # multipart/alternative/<html part> as multipart/related instead of
    # sitting alongside text/plain as a top-level attachment (which is
    # what add_related() on `msg` itself would produce).
    msg.get_payload()[1].add_related(
        logo_path.read_bytes(), maintype="image", subtype="png", cid=logo_content_id
    )

    timeout = 10.0
    if settings.smtp_tls_mode == "implicit":
        # TLS from the first byte -- the common shape on port 465.
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=timeout) as smtp:
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            # from_addr/to_addrs passed explicitly (rather than left
            # for send_message() to derive from the headers) so the
            # SMTP envelope sender is always the bare from_address,
            # never the "Display Name <address>" form now in the From
            # header -- the mail server signs DKIM against the
            # envelope-from domain (rspamd use_domain = "envelope"),
            # and that must keep matching from_address exactly.
            smtp.send_message(msg, from_addr=from_address, to_addrs=[to_address])
    else:
        # STARTTLS -- connect plain, then upgrade before sending
        # anything sensitive (credentials, the message itself). The
        # common shape on port 587, and the default for any
        # smtp_tls_mode value other than "implicit" -- see that
        # setting's own comment in app/config.py.
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            # See the SMTP_SSL branch above for why from_addr/to_addrs
            # are explicit here too.
            smtp.send_message(msg, from_addr=from_address, to_addrs=[to_address])


async def send_magic_link_email(
    to_address: str, link_url: str, purpose: str = PURPOSE_SIGN_IN
) -> None:
    """Sends a mailed link, off the event loop (asyncio.to_thread -- see
    this module's own docstring for why that is not optional here).
    Raises EmailSendError on any failure, after logging it -- callers
    must catch this and respond to the ORIGINAL caller exactly as if it
    had succeeded (see EmailSendError's own docstring).

    `purpose` picks the wording: PURPOSE_SIGN_IN for a link that hands
    over a session, PURPOSE_VERIFY_CONTACT for one that only confirms an
    address is reachable. Both were once the same mail, so a contact
    confirmation arrived telling the recipient it would sign them in --
    untrue, and exactly the shape a phishing attempt takes.
    """
    try:
        await asyncio.to_thread(_send_sync, to_address, link_url, purpose)
    except Exception as e:
        log.exception("email_login: failed to send magic-link mail to %s", _mask_for_log(to_address))
        raise EmailSendError(str(e)) from e
