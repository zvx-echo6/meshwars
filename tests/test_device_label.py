"""Tests for app/device_label.py's device_label_from_user_agent() --
the parser that turns a request's raw User-Agent header into a short
"<Browser> on <OS>" label for the Sessions panel, without ever storing
or leaking the raw header itself.

Real User-Agent strings below are copied verbatim from actual browser
releases (Chrome/Edge/Firefox/Safari on desktop, iOS, and Android) --
not hand-abbreviated -- so these tests exercise the exact token
ordering ambiguities (Chrome UAs containing "Safari/", Android UAs
containing "Linux", iPhone UAs containing "like Mac OS X") that make
detection order load-bearing in the first place. See that module's own
comments on _detect_browser/_detect_os for why each ordering matters.
"""
from __future__ import annotations

from app.device_label import device_label_from_user_agent

CHROME_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
SAFARI_MACOS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.1 Safari/605.1.15"
)
EDGE_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.2151.72"
)
FIREFOX_WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0"
FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0"
SAFARI_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1"
)
SAFARI_IPAD = (
    "Mozilla/5.0 (iPad; CPU OS 17_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1"
)
CHROME_ANDROID = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)
EDGE_ANDROID = (
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0 Mobile Safari/537.36 EdgA/119.0.2151.71"
)


def test_chrome_on_windows():
    assert device_label_from_user_agent(CHROME_WINDOWS) == "Chrome on Windows"


def test_safari_on_macos():
    assert device_label_from_user_agent(SAFARI_MACOS) == "Safari on macOS"


def test_edge_on_windows_not_mislabeled_as_chrome():
    """Edge's UA contains a literal "Chrome/" token (Chromium
    web-compat) -- if the Chrome check ran before the Edge check, this
    would come back "Chrome on Windows" instead.
    """
    assert device_label_from_user_agent(EDGE_WINDOWS) == "Edge on Windows"


def test_firefox_on_windows():
    assert device_label_from_user_agent(FIREFOX_WINDOWS) == "Firefox on Windows"


def test_firefox_on_linux():
    assert device_label_from_user_agent(FIREFOX_LINUX) == "Firefox on Linux"


def test_safari_on_iphone_not_mislabeled_as_macos():
    """An iPhone UA contains the literal substring "like Mac OS X" --
    if the macOS check ran before the iPhone check, this would come
    back "Safari on macOS" instead of the far more useful "on iPhone".
    """
    assert device_label_from_user_agent(SAFARI_IPHONE) == "Safari on iPhone"


def test_safari_on_ipad():
    assert device_label_from_user_agent(SAFARI_IPAD) == "Safari on iPad"


def test_chrome_on_android_not_mislabeled_as_linux():
    """An Android UA contains the literal substring "Linux" (Android's
    kernel) -- if the Linux check ran before the Android check, every
    Android device would read as a Linux desktop instead.
    """
    assert device_label_from_user_agent(CHROME_ANDROID) == "Chrome on Android"


def test_edge_on_android_not_mislabeled_as_chrome():
    """Both the Chrome-vs-Edge and Android-vs-Linux ordering traps at
    once -- EdgA/ (Edge for Android) UAs contain "Chrome/", "Safari/",
    AND "Linux".
    """
    assert device_label_from_user_agent(EDGE_ANDROID) == "Edge on Android"


def test_missing_user_agent_is_unknown_device():
    assert device_label_from_user_agent(None) == "Unknown device"


def test_empty_user_agent_is_unknown_device():
    assert device_label_from_user_agent("") == "Unknown device"


def test_whitespace_only_user_agent_is_unknown_device():
    assert device_label_from_user_agent("   ") == "Unknown device"


def test_unrecognised_user_agent_degrades_to_unknown_device_not_a_guess():
    """A non-browser client (a curl/script/bot UA) must never produce a
    fabricated label -- there is no browser or OS token here at all.
    """
    assert device_label_from_user_agent("curl/8.1.2") == "Unknown device"


def test_junk_user_agent_is_unknown_device():
    assert device_label_from_user_agent("asdf;;;###garbage\t\n") == "Unknown device"


def test_browser_recognised_but_os_unrecognised_still_degrades_to_unknown():
    """A half-match (known browser token, no recognisable OS token)
    must not invent a shape like "Chrome on unknown OS" -- the parser's
    contract is exactly two shapes: a real "<Browser> on <OS>" pair, or
    "Unknown device". Never a guess.
    """
    assert device_label_from_user_agent("Mozilla/5.0 Chrome/120.0.0.0 Safari/537.36") == "Unknown device"


def test_raw_user_agent_never_leaks_through_on_unrecognised_input():
    """The literal raw string must never appear anywhere in the output
    for input this module cannot parse -- the whole point of reducing
    to a fixed vocabulary of labels."""
    raw = "TotallyUnknownBrowser/9.9 ExoticOS/3.0 unique-marker-xyz"
    label = device_label_from_user_agent(raw)
    assert label == "Unknown device"
    assert "unique-marker-xyz" not in label


def test_overlong_user_agent_is_capped_and_still_handled():
    """An attacker-controlled header with no length limit upstream --
    padding a real Chrome/Windows UA with a huge prefix must not crash
    or hang the parser; detection still runs against the bounded,
    truncated string.
    """
    huge = ("A" * 100_000) + CHROME_WINDOWS
    label = device_label_from_user_agent(huge)
    # The real tokens are pushed past the truncation window by the
    # padding, so this degrades to Unknown device rather than finding
    # them -- the important assertions are that this returns promptly,
    # stays within the output cap, and never contains the padding.
    assert label == "Unknown device"
    assert len(label) <= 40
    assert "A" * 50 not in label


def test_control_characters_are_stripped():
    """A control-character-laced header (NUL, ESC, etc. spliced into an
    otherwise-real UA) must not break token matching or leak the raw
    bytes into the stored/rendered label.
    """
    laced = "Mozilla/5.0 (Windows NT 10.0)\x00\x1b Chrome/120.0.0.0 Safari/537.36"
    assert device_label_from_user_agent(laced) == "Chrome on Windows"


def test_output_length_is_always_within_the_cap():
    """Every real recognised combination stays well under the output
    cap -- this is a sanity check on the fixed vocabulary, not a
    behavioural requirement of any single input."""
    for ua in (CHROME_WINDOWS, SAFARI_MACOS, EDGE_WINDOWS, FIREFOX_WINDOWS, SAFARI_IPHONE, CHROME_ANDROID):
        assert len(device_label_from_user_agent(ua)) <= 40
