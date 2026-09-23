"""Turn a MeshCore ingest batch's User-Agent header into a short, coarse
client label -- "meshmapper-dart", "unrecognized-python" -- for
app/mc_ingest.py's record_ingest_identity() to store in
app/db.py's mc_ingest_request_log instead of the raw header.

Why this exists at all: an early draft of the ingest-identity-capture
feature (branch feat/ingest-identity-capture, commit 45eadb2) stored the
raw User-Agent string verbatim, which a privacy review rejected -- see
mc_ingest_request_log's own SCHEMA comment in app/db.py for the full
history, and app/device_label.py's own module docstring for the sibling
precedent this module deliberately follows: a raw UA is a fingerprint,
and nothing this feature exists to do (spot a batch that does NOT look
like it came from the genuine MeshMapper app) needs anything more
precise than a coarse family label. device_label.py answers "whose
browser is this" for a human's Sessions panel; this module answers "is
this the genuine ingest client" for an anti-spoofing log, but the shape
of the answer -- and the promise never to store or return the raw input
-- is the same.

Pure stdlib, deliberately, same reasoning device_label.py gives for its
own choice: this is a handful of substring checks against known,
self-identifying clients, not a general UA-parsing problem.
"""
from __future__ import annotations

import re

# Same defensive bound device_label.py's own _MAX_INPUT_LEN gives, for
# the same reason: User-Agent is attacker-controlled input from a
# public endpoint, and nothing upstream of this module limits its
# length. Every token this module looks for appears well within this
# bound in any real client's UA.
_MAX_INPUT_LEN = 512

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# The four coarse labels this module can return. "unrecognized-other" is
# the default for anything -- including a missing/empty header -- that
# does not match one of the other three; it is not a fifth, more
# specific bucket, and callers must not treat it as meaning anything
# beyond "not one of the known shapes below".
FAMILY_MESHMAPPER_DART = "meshmapper-dart"
FAMILY_FREQMAPPER = "freqmapper"
FAMILY_UNRECOGNIZED_PYTHON = "unrecognized-python"
FAMILY_UNRECOGNIZED_OTHER = "unrecognized-other"


def _sanitize(raw: str) -> str:
    return _CONTROL_CHARS.sub("", raw[:_MAX_INPUT_LEN])


def client_family_from_user_agent(raw_user_agent: str | None) -> str:
    """Classify `raw_user_agent` into one of the four FAMILY_* labels
    above. Never returns, logs, or otherwise surfaces the input itself
    -- the raw string is only ever used transiently, inside this
    function, to decide which fixed label to hand back.

    - MeshMapper is a Dart/Flutter app; Dart's own `dart:io` HTTP
      client sends a User-Agent containing "Dart" (e.g. "Dart/3.4
      (dart:io)") unless the app overrides it, which is the strongest
      available signal that a batch came from the real client rather
      than a hand-rolled script replaying a captured API key.
    - FreqMapper identifies itself with "FreqMapper" in its own
      User-Agent (its own separate ingest path, app/freqmapper_ingest.py,
      already expects this) -- included here too since nothing stops a
      shared build or a misconfigured client from posting to this
      endpoint instead, and a positive match here is strictly more
      useful than falling through to "unrecognized-other".
    - "python-requests", "python-urllib", or a bare "Python/" token
      marks a scripted client -- requests/urllib/aiohttp's own default
      User-Agent strings all self-identify this way. This is exactly
      the shape a hostile replay script tends to take (see this
      feature's own design brief: the incident that prompted it came
      from a datacenter address, not a real phone running MeshMapper),
      so it gets its own bucket rather than falling into the general
      "other" catch-all.
    - Everything else -- a genuinely unrecognized client, a missing or
      empty header, or a header this module cannot make sense of --
      falls to "unrecognized-other". Never a guess.
    """
    if not raw_user_agent:
        return FAMILY_UNRECOGNIZED_OTHER

    ua = _sanitize(raw_user_agent)
    if not ua:
        return FAMILY_UNRECOGNIZED_OTHER

    ua_lower = ua.lower()

    if "dart" in ua_lower:
        return FAMILY_MESHMAPPER_DART
    if "freqmapper" in ua_lower:
        return FAMILY_FREQMAPPER
    if "python" in ua_lower:
        return FAMILY_UNRECOGNIZED_PYTHON
    return FAMILY_UNRECOGNIZED_OTHER
