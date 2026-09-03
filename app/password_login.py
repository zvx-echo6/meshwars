"""Account password hashing: hashlib.scrypt only, no database, no
session, no route. Mirrors the split app/email_login.py already draws
for the magic-link door -- this module knows nothing about accounts,
sessions, or HTTP; app/account_api.py (set/change/remove) and
app/oauth_api.py (POST /auth/password/start) are the routers that own
all of that and call into this module only to hash a new password
(hash_password) and check one at sign-in (verify_password).

---- why scrypt, and NOT app/mc_ingest.py's hash_secret() -----------------

hash_secret() is a bare, single-round SHA-256 digest. That is exactly
right for api_key.key_hash and account_session.token_hash: a raw
secrets.token_urlsafe(32) token has 256 bits of real entropy, so
"stealing the database" is already the only way to guess one -- an
offline attacker gains nothing from SHA-256 being fast, because there
is nothing short of the full keyspace worth searching.

A human-chosen password is a completely different threat model: it has
far less real entropy than a random token, people reuse them across
sites, and a stolen database instantly hands an offline attacker
however many guesses their hardware can produce per second. A bare
SHA-256 digest can be tested at billions of guesses per second on
commodity GPU hardware -- for a password, that is not a safety margin,
it is barely a speed bump. scrypt (stdlib hashlib.scrypt, no new
dependency) is deliberately slow AND memory-hard: the memory
requirement (128*n*r*p bytes, see app/config.py's
account_password_scrypt_n/r/p comment for the actual numbers) is what
makes it expensive to parallelize on GPU/ASIC hardware the way a
plain-CPU-slow hash is not. This is the whole reason
app/db.py's account_password table exists as a SEPARATE table from
api_key/account_session rather than growing a second column on
either -- the two hash shapes must never be interchangeable, and never
sit behind the same function. If a future reader is tempted to
"simplify" this module down to hash_secret() because "it's just another
hash," this paragraph is why not to.

---- what is stored, and why -----------------------------------------------

hash_password() returns (salt, n, r, p, dklen, derived_key) -- every
one of those, not just the derived key -- for app/db.py's
account_password table to store verbatim. Storing the KDF parameters
ALONGSIDE each hash (not reading them fresh from settings at verify
time) means a future change to
settings.account_password_scrypt_n/r/p/dklen (raising the cost as
hardware gets faster) never invalidates a password hashed under the
old parameters -- verify_password() always uses whatever the STORED
row itself recorded, and a password only ever moves to the new
parameters the next time it is set or changed.

verify_password() compares with hmac.compare_digest, never `==` --
same reasoning every other secret comparison in this codebase already
follows (app/admin_api.py's token check, this app's key/session hash
lookups being indexed rather than compared at all): a naive `==` on
two strings short-circuits at the first differing byte, which leaks
timing information an attacker can use to recover a hash one byte at a
time. compare_digest runs in constant time for equal-length inputs.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

_SALT_BYTES = 16  # 128 bits -- generous for a per-password salt; only has to be unique, not secret.


@dataclass(frozen=True)
class PasswordHash:
    """Everything app/db.py's account_password table stores for one
    password. salt/derived_key are hex strings (same encoding
    app/mc_ingest.py's hash_secret() already uses for key_hash/
    token_hash, so every credential digest in this database has one
    consistent on-disk representation).
    """
    salt: str
    n: int
    r: int
    p: int
    dklen: int
    derived_key: str


def hash_password(
    raw_password: str, *, n: int, r: int, p: int, dklen: int
) -> PasswordHash:
    """Hash `raw_password` under a freshly generated random salt and
    the given scrypt cost parameters -- callers pass
    settings.account_password_scrypt_n/r/p/dklen (see that setting's
    own comment in app/config.py for the chosen values and why), never
    parameters recovered from an existing row: this is for SETTING a
    new password, not verifying one -- see verify_password() below for
    the read path, which uses whatever a stored row itself recorded.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        raw_password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen
    )
    return PasswordHash(
        salt=salt.hex(), n=n, r=r, p=p, dklen=dklen, derived_key=derived.hex()
    )


def verify_password(raw_password: str, stored: PasswordHash) -> bool:
    """True iff `raw_password`, hashed under `stored`'s OWN salt and
    cost parameters (never today's settings -- see this module's own
    docstring on why), matches `stored.derived_key`. Constant-time
    comparison (hmac.compare_digest) -- see this module's own docstring
    for why a plain `==` would leak timing information.
    """
    candidate = hashlib.scrypt(
        raw_password.encode("utf-8"),
        salt=bytes.fromhex(stored.salt),
        n=stored.n,
        r=stored.r,
        p=stored.p,
        dklen=stored.dklen,
    )
    return hmac.compare_digest(candidate.hex(), stored.derived_key)
