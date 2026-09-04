"""TOTP (RFC 6238) two-factor authentication: pure-stdlib code
generation/verification, secret-at-rest encryption, provisioning-URI
construction, and recovery-code generation. Mirrors the split
app/password_login.py and app/email_login.py already draw for their
own doors -- this module knows nothing about accounts, sessions, the
database, or HTTP; app/totp_api.py is the router that owns all of that
and calls into this module only to generate a secret (generate_secret),
build the otpauth:// URI an authenticator app scans (provisioning_uri),
check a submitted code against a decrypted secret (verify_totp_code),
encrypt/decrypt a secret for storage (encrypt_secret/decrypt_secret),
and mint fresh recovery codes (generate_recovery_codes -- hashed by the
CALLER with app/mc_ingest.py's hash_secret(), the same house
convention account_session/api_key/every other hashed-single-use-ticket
table in this app already uses for a random, server-generated,
compared-not-brute-forced secret; see that module's own reasoning for
why this is right for a recovery code and wrong for account_password's
human-chosen password, which is why this module never imports
hash_secret itself -- storage is app/totp_api.py's job, not this one's).

---- RFC 6238 in stdlib only -------------------------------------------

TOTP is HOTP (RFC 4226) -- an HMAC-SHA1 over a moving counter, truncated
to a short decimal code -- run against a counter derived from wall-clock
time instead of an incrementing counter: counter = floor(unix_time /
30). Every piece of that (hmac, hashlib.sha1, struct.pack for the
8-byte big-endian counter, base64 for the secret's text encoding) is
stdlib; there is no TOTP library in this codebase and none is added by
this change. _hotp() below is verified against RFC 6238 Appendix B's
own published test vectors in tests/test_totp.py (SHA1 mode, the
20-byte ASCII secret "12345678901234567890" that appendix uses) -- the
appendix's own vectors are 8-digit codes; this module always produces
6 (see _DIGITS below), so those tests compare against the last 6
digits of each published 8-digit vector, which is exactly what
RFC 4226's own truncation (`truncated_value MOD 10^Digits`) guarantees
will match: shrinking Digits from 8 to 6 only changes which power of
10 the same truncated integer is reduced by.

---- 6 digits, 30-second step -------------------------------------------

Every mainstream authenticator app (Google Authenticator, Authy, 1Password,
Aegis, ...) defaults to 6 digits / 30 seconds and many do not let a user
change either -- deviating here would make this feature simply not work
with the apps people already have installed, for no real security gain
(RFC 6238 itself treats both as tunable but recommends these defaults).

---- clock skew: one step either side, chosen deliberately -------------

A phone's clock and this server's clock are never perfectly in sync, and
a person also needs a few seconds to read a code off their screen and
type it in before it submits -- accepting ONLY the exact current step
would intermittently reject genuinely correct codes for no attacker-
facing reason. DEFAULT_SKEW_STEPS = 1 accepts the current step plus one
step immediately before and after it (a ~90-second window: the
in-flight step plus 30 seconds either side), which is the RFC's own
"acceptable" range (RFC 6238 SS6: "keys ... should be [checked] only ...
within a small compensation window") and the value most real-world TOTP
server implementations converge on. Widening it further would trade
security margin (each extra step is another 6-digit guess that
verifies) for a usability gain past the point most clock drift actually
needs.

---- replayed codes: rejected, via a caller-tracked "last used step" ----

RFC 6238 does not itself mandate rejecting an already-used code (HOTP's
own counter-advance is what a plain implementation relies on, but TOTP's
counter is time-derived, not caller-advanced, so nothing stops the SAME
still-valid code from verifying twice in a row with no extra state).
This app rejects replay anyway -- app/totp_api.py's verify_and_consume_
totp_code() records the counter step a code was accepted at
(account_totp.last_used_step) and refuses to accept ANY step at or
before that value, not just the identical one -- see that function's
own docstring for the full reasoning (in short: a stolen-in-transit code
is a real threat this narrows to zero, at the cost of nothing a
legitimate user would ever notice, since steps only move forward). This
module's own verify_totp_code() below is deliberately stateless and
knows nothing about replay at all -- it is the low-level "does this code
match, at or near this instant" check tests/test_totp.py's skew-window
tests exercise directly; the replay guard is a property of the
CALLER's bookkeeping, layered on top, tested separately.

---- secret at rest: Fernet, keyed from the environment -----------------

Unlike a password, a TOTP secret cannot be hashed -- verifying a
submitted code requires recomputing HOTP from the ORIGINAL secret, not
comparing to a one-way digest of it (see this module's own docstring
above: hashing loses exactly the information verification needs). So
the secret is stored ENCRYPTED, not hashed, with a symmetric key held
in the environment (settings.account_totp_encryption_key), never in the
database -- the point being that a stolen database file ALONE (a
backup, a leaked volume) yields no working second factors, since
decrypting account_totp.secret_encrypted also requires a value that was
never written to disk alongside it. `cryptography` is already a
dependency of this app (app/mqtt_subscriber.py's own AES-CTR decode) --
this reuses it (cryptography.fernet.Fernet: AES-128-CBC + HMAC-SHA256,
authenticated so a tampered ciphertext raises rather than silently
decrypting to garbage) rather than hand-rolling AES-GCM the way that
module has to (Meshtastic's own wire format dictates AES-CTR there;
nothing here is constrained by an external protocol, so the simpler,
already-reviewed Fernet recipe is the right choice).

FAILS CLOSED: every function below that touches encryption
(_fernet(), encrypt_secret(), decrypt_secret()) raises
TotpEncryptionUnavailable when settings.account_totp_encryption_key is
unset or malformed, rather than falling back to storing a plaintext
secret. This is the one place in this codebase where "empty means off"
(app/email_login.py's email_login_enabled(), app/oauth.py's
provider_enabled(), ...) is not enough on its own: those features
simply do nothing when unconfigured, but a TOTP secret has to be
encrypted THE MOMENT it is generated, in the same call that creates it
-- there is no safe "store it anyway, encrypt it later" fallback, so
app/totp_api.py's enrollment route must catch this exception and refuse
enrollment outright (see that route's own docstring) rather than ever
reaching a code path that could persist an unencrypted secret.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

# ---- code generation / verification -------------------------------------

_STEP_SECONDS = 30
_DIGITS = 6

# 160 bits -- RFC 4226 Appendix B's own recommended HOTP secret length
# (SHOULD be at least 128 bits, RECOMMENDED 160), and the width every
# mainstream authenticator app's own QR scanner expects without
# complaint. base32-encodes to 32 characters with no padding, which is
# what actually gets shown/scanned (see secret_to_base32 below).
_SECRET_BYTES = 20

# See this module's own docstring, "clock skew" section, for the full
# reasoning behind accepting one step either side of "now" by default.
DEFAULT_SKEW_STEPS = 1


def generate_secret() -> bytes:
    """A fresh, cryptographically random 160-bit TOTP secret --
    secrets.token_bytes(), the same "how many random bytes is enough"
    answer every other credential in this app already uses (see
    app/sessions.py's own _TOKEN_BYTES comment). Callers store the
    ENCRYPTED form (encrypt_secret() below), never these raw bytes.
    """
    return secrets.token_bytes(_SECRET_BYTES)


def secret_to_base32(secret: bytes) -> str:
    """RFC 4648 base32, no padding -- the text form an authenticator
    app's QR scanner and this module's own manual-entry text both use
    (RFC 6238's own otpauth:// URI format requires the `secret` query
    parameter to be base32, unpadded is conventional and what every
    real authenticator app emits/accepts).
    """
    return base64.b32encode(secret).decode("ascii").rstrip("=")


def _hotp(secret: bytes, counter: int) -> str:
    """RFC 4226's HOTP algorithm itself: HMAC-SHA1 over the 8-byte
    big-endian counter, dynamically truncated to a _DIGITS-digit
    decimal code. Verified against RFC 6238 Appendix B's own published
    test vectors in tests/test_totp.py -- see this module's own
    docstring for why those vectors (8-digit) still exercise this
    function's 6-digit output correctly.
    """
    msg = struct.pack(">Q", counter)
    digest = hmac.new(secret, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    code = truncated % (10 ** _DIGITS)
    return str(code).zfill(_DIGITS)


def totp_code_at(secret: bytes, *, when: int) -> str:
    """The code a real authenticator app would show at unix time
    `when` -- exists mainly for tests (constructing a known-good code
    to submit) and for _hotp's own RFC-vector tests above it, never
    called by a real verification path (verify_totp_code below checks
    a caller-submitted code against several candidate steps, it never
    needs "the" current code on its own).
    """
    return _hotp(secret, when // _STEP_SECONDS)


def verify_totp_code(
    secret: bytes, code: str, *, now: int | None = None, skew_steps: int = DEFAULT_SKEW_STEPS
) -> bool:
    """True iff `code` matches secret's own HOTP value at the current
    step, or at any step within `skew_steps` either side of it (see
    this module's own docstring for why one step is the chosen
    default). Constant-time comparison (hmac.compare_digest) against
    each candidate -- same "never a naive ==" rule every other secret
    comparison in this codebase already follows (app/password_login.py's
    verify_password(), app/admin_api.py's token check, ...): a 6-digit
    code has a small enough keyspace that this alone would not save an
    attacker much, but there is no reason to be the one comparison in
    this app that skips it.

    Deliberately stateless -- this function alone does NOT reject a
    replayed code (the same code submitted twice in a row would verify
    true both times); see this module's own docstring, "replayed
    codes" section, for why that guard lives one layer up, in
    app/totp_api.py, where a per-account "last accepted step" can
    actually be tracked. This function is the low-level primitive
    tests/test_totp.py's own skew-window tests exercise directly.

    Also rejects anything that doesn't even look like a code (empty,
    wrong length, non-digit characters) before doing any HMAC work at
    all -- a malformed submission is never a legitimate near-miss.
    """
    if now is None:
        now = int(time.time())
    if not code or len(code) != _DIGITS or not code.isdigit():
        return False

    counter = now // _STEP_SECONDS
    for delta in range(-skew_steps, skew_steps + 1):
        candidate = _hotp(secret, counter + delta)
        if hmac.compare_digest(candidate, code):
            return True
    return False


def step_for(now: int | None = None) -> int:
    """The counter step `now` (default: current wall clock) falls in --
    app/totp_api.py's replay guard needs this to know which step a
    successful verification actually landed on (verify_totp_code above
    returns only True/False, not which of the skew-window candidates
    matched), so it recomputes the same step->code check itself when
    recording last_used_step. Exposed here rather than duplicated so
    both call sites agree on exactly how a unix timestamp maps to a
    step.
    """
    if now is None:
        now = int(time.time())
    return now // _STEP_SECONDS


# ---- otpauth:// provisioning URI -----------------------------------------

def provisioning_uri(*, secret: bytes, account_label: str, issuer: str) -> str:
    """The otpauth://totp/... URI an authenticator app's QR scanner (or
    a manual "enter this URI" import, which some apps also offer)
    reads to add this account -- Google's own "Key URI Format" is the
    de facto spec every mainstream app implements
    (https://github.com/google/google-authenticator/wiki/Key-Uri-Format).

    `issuer` appears TWICE (in the label, as "{issuer}:{account_label}",
    and again as its own query parameter) -- the label form is what
    older/simpler scanners fall back to for display, the query
    parameter is what the spec calls authoritative and what most
    current apps actually group entries by; setting both is the
    documented belt-and-suspenders way to get a sensible-looking entry
    ("MeshWars (jdoe@example.com)", not a bare, unbranded secret) in
    every app this might be scanned into.

    algorithm/digits/period are included explicitly even though they
    match every app's own defaults (SHA1/6/30 -- see this module's own
    docstring for why this app never deviates from them) -- an app that
    reads them confirms they match what it would have assumed anyway,
    and one that ignores them is unaffected either way, so there is no
    downside to being explicit.
    """
    # The separator between issuer and account must be a LITERAL colon,
    # not %3A. Google's Key URI grammar permits either, but every
    # canonical example and every mainstream app (Google Authenticator,
    # Aegis, 1Password) emits the literal form, and a parser that splits
    # the label on ":" to recover the issuer sees a percent-encoded one
    # as part of one long account name instead -- Ente Auth rejected a
    # %3A-encoded label outright, which is what prompted this. `safe`
    # therefore keeps ":" and "@" unescaped (the spec's own example
    # carries a bare "john.doe@email.com"); anything genuinely needing
    # escaping, a space in an issuer above all, still is.
    label = urllib.parse.quote(f"{issuer}:{account_label}", safe=":@")
    params = {
        "secret": secret_to_base32(secret),
        "issuer": issuer,
        "algorithm": "SHA1",
        "digits": str(_DIGITS),
        "period": str(_STEP_SECONDS),
    }
    query = urllib.parse.urlencode(params)
    return f"otpauth://totp/{label}?{query}"


# ---- secret-at-rest encryption -------------------------------------------

class TotpEncryptionUnavailable(Exception):
    """Raised whenever settings.account_totp_encryption_key is unset or
    is not a valid Fernet key -- see this module's own docstring,
    "secret at rest" section, for why this is a LOUD failure (an
    exception every caller must handle) rather than the quiet "empty
    means off" every other optional feature in app/config.py uses:
    unlike those, there is no safe way to proceed here at all --
    storing an unencrypted secret would be actively unsafe, not merely
    "the feature does nothing." app/totp_api.py's enrollment route
    catches this and refuses enrollment (same "indistinguishable from
    not existing" 404 contract every other unconfigured optional
    feature in this app already uses) rather than ever reaching a
    write that could persist a plaintext secret.
    """


def _fernet() -> Fernet:
    key = settings.account_totp_encryption_key
    if not key:
        raise TotpEncryptionUnavailable("account_totp_encryption_key is not set")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as e:
        raise TotpEncryptionUnavailable(
            f"account_totp_encryption_key is not a valid Fernet key: {e}"
        ) from e


def totp_encryption_available() -> bool:
    """Whether encrypt_secret()/decrypt_secret() would actually work
    right now -- app/totp_api.py's enrollment route checks this BEFORE
    generating a secret at all (no point minting one just to fail to
    store it), and GET /api/account surfaces it to the frontend
    (`totp.available`) so the Security panel can explain an unavailable
    "Enable two-factor authentication" button instead of offering one
    that would 404.
    """
    try:
        _fernet()
        return True
    except TotpEncryptionUnavailable:
        return False


def encrypt_secret(secret: bytes) -> str:
    """Encrypts `secret` for storage in account_totp.secret_encrypted.
    Raises TotpEncryptionUnavailable (never silently proceeds) if no
    usable key is configured -- see this module's own docstring and
    TotpEncryptionUnavailable's own docstring for why this fails
    closed rather than falling back to plaintext.
    """
    return _fernet().encrypt(secret).decode("ascii")


def decrypt_secret(token: str) -> bytes:
    """The inverse of encrypt_secret() -- reads a stored
    account_totp.secret_encrypted value back to raw secret bytes for
    verify_totp_code() to check a submitted code against. Raises
    TotpEncryptionUnavailable if the configured key cannot decrypt the
    stored token at all (wrong/rotated key, or -- Fernet is
    authenticated -- a corrupted/tampered value): a row that cannot be
    decrypted can never verify a code, so callers should treat this
    exactly like "no working second factor for this account" rather
    than a 500.
    """
    try:
        return _fernet().decrypt(token.encode("ascii"))
    except InvalidToken as e:
        raise TotpEncryptionUnavailable(
            "stored TOTP secret could not be decrypted (wrong/rotated key, or corrupted value)"
        ) from e


# ---- recovery codes -------------------------------------------------------

# Crockford-style alphabet with the visually-ambiguous characters
# dropped (0/O, 1/I/L never appear) -- these codes are shown to a
# person exactly ONCE (app/totp_api.py's activation response) and are
# meant to be written down or printed, so a character that could be
# misread off a screen or a scrawled note is a real usability bug here
# in a way it never is for, say, secrets.token_urlsafe()'s own alphabet
# (which nobody ever transcribes by hand).
_RECOVERY_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_RECOVERY_CODE_LENGTH = 10


def generate_recovery_code() -> str:
    """One recovery code -- secrets.choice() per character (not
    secrets.token_urlsafe(), which draws from base64url's own
    alphabet, mixed case and ambiguous-on-a-screen characters included)
    so every character comes from _RECOVERY_CODE_ALPHABET above. 10
    characters from a 32-symbol alphabet is 50 bits of entropy -- far
    more than a 6-digit TOTP code, appropriate for a credential that
    (unlike a TOTP code) never expires until used, so it has to resist
    guessing indefinitely, not just within a 90-second window.
    """
    return "".join(secrets.choice(_RECOVERY_CODE_ALPHABET) for _ in range(_RECOVERY_CODE_LENGTH))


def generate_recovery_codes(count: int) -> list[str]:
    """`count` fresh, independently-random recovery codes -- callers
    (app/totp_api.py's activation route) hash each one with
    app/mc_ingest.py's hash_secret() before storing it (see this
    module's own docstring for why that hasher, not scrypt, is correct
    here) and show the plaintext list to the caller exactly once; this
    module never sees or stores a hash, only generates the plaintext.
    """
    return [generate_recovery_code() for _ in range(count)]
