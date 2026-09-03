"""Turn a browser's User-Agent header into a short, human-readable
device label -- "Chrome on Windows", "Safari on iPhone" -- for
app/account_api.py's Sessions panel (GET /api/account) to render.

Why this exists at all: account_session used to store the raw
User-Agent string verbatim (see that table's MIGRATIONS entry in
app/db.py for the full history). A raw UA is a fingerprint -- it
carries exact browser/engine/OS build numbers that combine with other
signals to narrow a device down to a small set of people, sometimes
one. Nobody using the Sessions panel needs that precision; they need
enough to tell "the Firefox session on my laptop" from "the Safari
session on my phone" so they can recognise their own and revoke a
stranger's. A two-word label is exactly that much information and no
more.

Pure stdlib, deliberately: this app has no HTTP-parsing dependency
today and a device label is not worth adding one for. A real
UA-parsing library (e.g. ua-parser) chases hundreds of browser/engine
combinations; this module chases the handful that actually show up in
this game's session table and degrades honestly -- never a guess, and
never the raw string -- for everything else.
"""
from __future__ import annotations

import re

# Hard cap on the RAW input this module will even look at. Real
# browsers send UA strings well under this (Chrome's is ~120
# characters, the most verbose common ones top out around 250) -- this
# exists purely as a defensive bound against a pathological or
# malicious header, since User-Agent is attacker-controlled input and
# nothing upstream of this module limits its length. Truncating first
# means every regex/substring check below runs against a small,
# bounded string no matter what arrives, and since every browser/OS
# token this module looks for appears within the first ~100
# characters of a real UA, truncation never costs a real detection.
_MAX_INPUT_LEN = 512

# Hard cap on the OUTPUT label. In practice this is unreachable -- every
# return value below is built from a fixed, hardcoded vocabulary of
# short literals ("Chrome", "on", "Windows", ...), never a slice of the
# input string, so the output can never carry attacker content or grow
# unbounded on its own. The cap is kept anyway as defense in depth: if
# this module ever grows a code path that echoes part of the input
# (e.g. an unrecognised-but-short browser token), that path inherits
# this bound for free instead of silently becoming a new fingerprinting
# or injection surface.
_MAX_LABEL_LEN = 40

_UNKNOWN = "Unknown device"

# Strips C0/C1 control characters (including the ones .strip() does not
# touch, like embedded NUL or ESC) before this string is matched or
# logged. A User-Agent header is attacker-controlled free text; nothing
# forces it to be the printable ASCII everyone's actual browser sends.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _sanitize(raw: str) -> str:
    """Bound and clean the raw header before any detection runs."""
    return _CONTROL_CHARS.sub("", raw[:_MAX_INPUT_LEN])


# ---- browser detection -----------------------------------------------
#
# Order is load-bearing, not stylistic. Every Chromium-derived browser
# (Edge, Opera, and Chrome itself) ships a UA string that ALSO contains
# literal "Chrome/" and "Safari/" tokens, for web-compat reasons dating
# back to when sites sniffed UAs to decide what to serve -- and Safari
# itself only ever appears alongside "Safari/", never alone. So the
# checks below have to run from MOST specific to LEAST specific:
# Edge's own "Edg/" token first (an Edge UA would otherwise match the
# Chrome check first and mislabel every Edge user as Chrome), then
# Opera's "OPR/" token, then Firefox (which carries none of the above),
# then Chrome, and only once none of those matched does a bare
# "Safari/"+"Version/" pair -- the actual Safari signature -- get
# checked last. Checking Safari first, or Chrome before Edge, would
# silently mislabel the majority of real-world sessions.
def _detect_browser(ua: str) -> str | None:
    if "Edg/" in ua or "EdgA/" in ua or "EdgiOS/" in ua:
        return "Edge"
    if "OPR/" in ua or "Opera" in ua:
        return "Opera"
    if "Firefox/" in ua and "Seamonkey" not in ua:
        return "Firefox"
    if ("Chrome/" in ua or "CriOS/" in ua) and "Chromium" not in ua:
        return "Chrome"
    # A bare "Safari/" token also shows up in Chromium UAs (see the
    # comment above), so it is only trusted once every Chromium-family
    # browser has already had its chance to match above. "Version/" is
    # Safari's own browser-version marker (distinct from the WebKit
    # build number that follows "Safari/") and Chromium browsers never
    # include it, so requiring both together is what actually
    # distinguishes real Safari from a Chrome/Edge/Opera UA that merely
    # mentions Safari for compatibility.
    if "Safari/" in ua and "Version/" in ua:
        return "Safari"
    return None


# ---- OS detection -------------------------------------------------------
#
# Same ordering principle as browsers: iOS devices identify themselves
# as "iPhone"/"iPad" but their UA ALSO contains the literal substring
# "like Mac OS X" (WebKit's own compatibility shim), and Android UAs
# always contain "Linux" (Android's kernel) alongside "Android". Check
# the more specific token first in each case or an iPhone reads as
# "macOS" and every Android device reads as "Linux".
def _detect_os(ua: str) -> str | None:
    if "iPhone" in ua:
        return "iPhone"
    if "iPad" in ua:
        return "iPad"
    if "Android" in ua:
        return "Android"
    if "Windows" in ua:
        return "Windows"
    if "Macintosh" in ua or "Mac OS X" in ua:
        return "macOS"
    if "CrOS" in ua:
        return "ChromeOS"
    if "Linux" in ua:
        return "Linux"
    return None


def device_label_from_user_agent(raw_user_agent: str | None) -> str:
    """"<Browser> on <OS>" (e.g. "Chrome on Windows"), or "Unknown
    device" for anything missing, empty, or not recognised.

    Deliberately requires BOTH a recognised browser AND a recognised OS
    before returning the two-part label -- a half-match (a known
    browser on an unidentifiable OS, or vice versa) still falls back to
    "Unknown device" rather than inventing a shape like "Chrome on
    unknown OS". That keeps the output format fixed to exactly two
    shapes callers (app/account_api.py, frontend/account.js) ever have
    to handle, and keeps the promise this module exists to make: never
    a guess.
    """
    if not raw_user_agent:
        return _UNKNOWN

    ua = _sanitize(raw_user_agent)
    if not ua:
        return _UNKNOWN

    browser = _detect_browser(ua)
    os_name = _detect_os(ua)
    if browser is None or os_name is None:
        return _UNKNOWN

    label = f"{browser} on {os_name}"
    return label[:_MAX_LABEL_LEN]
