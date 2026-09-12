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
from datetime import datetime, timezone
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

# A third, unrelated shape (Stage 2): "something changed on your account,
# here is what and when" -- no single-use link at all, just a plain,
# reusable page URL (see send_security_notice() below). Kept in the same
# _MAIL_TEMPLATES table as the two link-carrying purposes above only
# because _render_html() already dispatches on `purpose` to find its
# fragment file -- it is NOT in _MAIL_COPY, since a security notice's
# subject/heading/body are supplied by the CALLER (nine different account
# events -- see app/account_api.py, app/totp_api.py, app/admin_api.py),
# never selected from a fixed table the way PURPOSE_SIGN_IN/
# PURPOSE_VERIFY_CONTACT's copy is.
PURPOSE_SECURITY_NOTICE = "security_notice"

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
    PURPOSE_SECURITY_NOTICE: "security_notice.html",
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


def _render_html(purpose: str, logo_cid: str, **placeholders: str) -> str:
    """Builds the HTML part by combining frontend/email/_shell.html (the
    table-based page frame: logo, gold heading, rule, meshwars.com line
    -- shared by every purpose) with the purpose's own fragment file
    (heading/body/cta/footer wording -- see _MAIL_TEMPLATES). Follows
    this codebase's one templating convention end to end (see this
    module's own docstring and app/api.py's _templated_html_page): plain
    HTML files on disk, substituted with str.replace, no templating
    dependency anywhere.

    `**placeholders` are replaced last, against the fully-assembled
    page, rather than against the fragment alone -- for
    PURPOSE_SIGN_IN/PURPOSE_VERIFY_CONTACT that is just `link_url`,
    which appears twice inside the fragment's own cta section (the
    button's href and the plaintext fallback below it); for
    PURPOSE_SECURITY_NOTICE it is `heading`/`line1`/`line2`/`line3`/
    `cta_label`/`cta_url`/`footer` (see security_notice.html's own
    comment -- that fragment's heading/body/cta/footer SECTIONS are
    themselves nothing but placeholder tokens, since a security
    notice's wording comes from the caller, not from a fixed fragment
    file the way sign_in.html/verify_contact.html's is). Either way,
    one replace after the fragment is already sitting inside the shell
    covers every occurrence of a given token, wherever it landed.
    """
    shell = _read_email_template("_shell.html")
    fragments = _parse_fragments(_read_email_template(_MAIL_TEMPLATES[purpose]))
    html = shell.replace("{logo_cid}", logo_cid)
    for name in ("heading", "body", "cta", "footer"):
        html = html.replace("{" + name + "}", fragments[name])
    for key, value in placeholders.items():
        html = html.replace("{" + key + "}", value)
    return html


def _build_message(subject: str, to_address: str) -> tuple[EmailMessage, str, str]:
    """Headers shared by EVERY mail this module sends -- Subject/From/
    To/Date/Message-ID/X-Mailer, identical in shape whether the body
    carries a single-use link (_send_sync) or a security notice
    (_send_notice_sync). Pulled out once here rather than built twice:
    those two functions' only real difference is the body/html they
    attach afterward, not this header block.

    Returns (msg, from_address, from_domain) -- from_address/from_domain
    are handed back rather than re-derived by each caller, since both
    callers need from_address again for the envelope sender at delivery
    time, and from_domain again for the logo's own Content-ID.
    """
    from_address = settings.smtp_from_address
    # The domain half of the configured sender address, used only to
    # make the Message-ID (and the logo's own Content-ID) look like
    # they belong to this deployment (RFC 5322 section 3.6.4 -- the
    # right-hand side of a Message-ID is conventionally the sending
    # domain). Never hardcode "meshwars.com" here: this is public AGPL
    # software, and other operators run it under their own domain.
    from_domain = from_address.rpartition("@")[2] or from_address

    msg = EmailMessage()
    msg["Subject"] = subject
    # Display name form ("Display Name <address>") for readability in
    # a mail client -- see settings.smtp_from_name's own comment in
    # app/config.py. This is cosmetic only: the SMTP *envelope* sender
    # (what the mail server actually signs DKIM against and what
    # delivery routes on) is passed explicitly to _deliver() below as
    # the bare from_address, never this display-name form.
    msg["From"] = formataddr((settings.smtp_from_name, from_address))
    msg["To"] = to_address
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_domain)
    msg["X-Mailer"] = "MeshWars"
    return msg, from_address, from_domain


def _attach_html(msg: EmailMessage, purpose: str, from_domain: str, **placeholders: str) -> None:
    """Renders and attaches the html alternative plus its inline CID
    logo -- the shared tail end of _send_sync() and _send_notice_sync().

    Sends multipart/alternative( text/plain, multipart/related( text/html,
    image/png ) ) -- the standard nesting for an HTML mail with an inline
    image (the image belongs only under the html branch, never as a
    sibling of the plain-text one, which the caller must already have
    set_content()'d before calling this). add_alternative() promotes
    the message to multipart/alternative and appends the html part
    after the existing text/plain one -- order matters here, RFC 2046
    says the LAST alternative is the richest and mail clients render
    the last part they understand.

    make_msgid() already returns the angle-bracketed form
    ("<unique@domain>") a Content-ID header is supposed to carry --
    EmailMessage.add_related()'s own cid= kwarg sets the header
    verbatim, with no bracket handling of its own (see
    email.contentmanager._finalize_set), so that raw value is exactly
    right for the header. The HTML <img> tag's own `cid:` URL is the
    opposite: RFC 2392 says a cid: URL is the bare content-id with NO
    angle brackets, so logo_cid strips them before it ever reaches
    _render_html() -- leaving them in is a common bug that silently
    breaks the image in most mail clients.
    """
    logo_content_id = make_msgid(domain=from_domain)
    logo_cid = logo_content_id.strip("<>")
    html_body = _render_html(purpose, logo_cid, **placeholders)
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


def _deliver(msg: EmailMessage, from_address: str, to_address: str) -> None:
    """The actual blocking SMTP conversation -- stdlib smtplib only, no
    third-party mail library, shared by _send_sync() and
    _send_notice_sync(). Never call this directly from async code; see
    send_magic_link_email()/send_security_notice() below and this
    module's own docstring.
    """
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


def _send_sync(to_address: str, link_url: str, purpose: str = PURPOSE_SIGN_IN) -> None:
    """The single-use-link mail (sign-in / verify-contact) -- see
    _deliver()'s own docstring for why this must never be called
    directly from async code.

    `purpose` selects the subject and body. It defaults to sign-in because
    that was this function's only behaviour before contact-address
    confirmation reused it, and a caller that forgets to say what it is
    sending should get the older, narrower wording rather than silence.

    The plain-text part is unchanged from before this module's HTML
    body existed (some mail clients and every spam filter still weigh
    it); the html part is the styled MeshWars page built by
    _render_html() via _attach_html(), and the image/png next to it is
    the logo the html part's <img> references by Content-ID rather
    than a remote URL -- inlined so the mail renders with no network
    fetch and nothing for an image-blocking client to strip.
    """
    subject, lead = _MAIL_COPY[purpose]
    msg, from_address, from_domain = _build_message(subject, to_address)
    msg.set_content(
        f"{lead}\n\n"
        f"{link_url}\n\n"
        "This link expires in a few minutes and can only be used once. "
        "If you didn't request it, you can safely ignore this message."
    )
    _attach_html(msg, purpose, from_domain, link_url=link_url)
    _deliver(msg, from_address, to_address)


def _send_notice_sync(
    to_address: str,
    subject: str,
    heading: str,
    lines: tuple[str, str, str],
    cta_label: str,
    cta_url: str,
    footer: str,
) -> None:
    """The security-notice mail (Stage 2) -- see _deliver()'s own
    docstring for why this must never be called directly from async
    code; see send_security_notice() below for the full contract.

    Deliberately NOT folded into _send_sync() as another `purpose`
    branch: a security notice carries no single-use `link_url` at all
    (its CTA is an ordinary, reusable page URL) and its
    subject/heading/body come from the CALLER -- nine different
    account events (see app/account_api.py, app/totp_api.py,
    app/admin_api.py) -- never selected from _MAIL_COPY the way every
    _send_sync() purpose already is. One signature trying to serve
    both shapes would need optional arguments for almost everything.

    No List-Unsubscribe header -- see send_security_notice()'s own
    docstring for why: this is transactional security mail tied to an
    action the recipient's own account (or an operator acting on it)
    just took, not a subscription.
    """
    msg, from_address, from_domain = _build_message(subject, to_address)
    line1, line2, line3 = lines
    msg.set_content(
        f"{heading}\n\n"
        f"{line1}\n\n{line2}\n\n{line3}\n\n"
        f"{cta_label}: {cta_url}\n\n"
        f"{footer}"
    )
    _attach_html(
        msg, PURPOSE_SECURITY_NOTICE, from_domain,
        heading=heading, line1=line1, line2=line2, line3=line3,
        cta_label=cta_label, cta_url=cta_url, footer=footer,
    )
    _deliver(msg, from_address, to_address)


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


def format_notice_timestamp(when: int) -> str:
    """Renders a unix timestamp for security-notice copy -- always UTC,
    always this one shape ("7 September 2026 at 01:19 UTC"), regardless
    of the server's own local timezone. A security notice states WHEN
    something happened as plainly as possible to someone who may not
    know or share this deployment's timezone; UTC, spelled out, with no
    offset for the reader to do arithmetic on, is the least ambiguous
    choice. Lives here (not in whichever app/*_api.py module happens to
    call send_security_notice() first) because it is pure formatting
    with no database access, the same "shape-only, no DB" boundary this
    module's own docstring draws for looks_like_email()/normalize_email()
    -- every caller needs the exact same rendering, so there is exactly
    one place that decides what it looks like.
    """
    dt = datetime.fromtimestamp(when, tz=timezone.utc)
    return dt.strftime("%-d %B %Y at %H:%M UTC")


async def send_security_notice(
    to_address: str,
    *,
    subject: str,
    heading: str,
    lines: tuple[str, str, str] | list[str],
    cta_label: str,
    cta_url: str,
    footer: str,
) -> None:
    """Sends a security notice (Stage 2) -- "something changed on your
    account, here is what and when" -- off the event loop, the same
    asyncio.to_thread() shape send_magic_link_email() already uses and
    for the identical reason (see this module's own docstring).

    Unlike send_magic_link_email(), there is no `purpose` table to pick
    from: every one of `subject`/`heading`/`lines` (exactly three body
    lines -- see security_notice.html's own comment)/`cta_label`/
    `cta_url`/`footer` is supplied by the caller outright. There are
    nine distinct account-security events that call this (a password
    change, a role grant, an admin-initiated recovery action, ... --
    see app/account_api.py, app/totp_api.py, app/admin_api.py), each
    with its own wording, and this function has no opinion about any
    of it -- it only knows how to mail whatever it's handed, the same
    "SMTP-and-shape-only half" boundary this whole module already
    draws (see its own docstring).

    Raises EmailSendError on any failure, after logging it -- exactly
    like send_magic_link_email(), but the stakes on the CALLER side are
    higher here, not lower: a magic-link send failure means the
    recipient just doesn't get a link (annoying, but nothing already
    happened). A security-notice send failure sits downstream of an
    action that has ALREADY SUCCEEDED (the password was already
    changed, the key was already rotated, ...) by the time this is
    called -- a notice is a courtesy on top of that, never a
    precondition for it. Every call site MUST catch this (or any other
    exception) and let the underlying action's response stand exactly
    as if the notice had sent cleanly; see this module's own
    EmailSendError docstring for the same reasoning applied to the
    magic-link case, which carries over unchanged here, only more
    important.

    Deliberately sets no List-Unsubscribe header (see _send_notice_sync()'s
    own comment): this is transactional mail describing something that
    already happened to the recipient's own account, not marketing or a
    digest a recipient signed up for and might want to leave.

    Recipient policy (who this may ever be mailed to at all) is
    enforced entirely by the CALLER, not here -- this module has no
    database access (see its own docstring) and cannot itself check
    whether `to_address` is a confirmed contact address. Every call
    site resolves that first (account.contact_email, only when
    account.contact_email_verified_at is not NULL) and simply never
    calls this function at all when there is no confirmed address --
    see app/account_api.py's `_verified_contact_email()`.
    """
    try:
        await asyncio.to_thread(
            _send_notice_sync, to_address, subject, heading, tuple(lines), cta_label, cta_url, footer,
        )
    except Exception as e:
        log.exception("email_login: failed to send security notice to %s", _mask_for_log(to_address))
        raise EmailSendError(str(e)) from e
