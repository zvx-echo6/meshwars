"""Shared helper for normalizing a Meshtastic/MeshCore node reference.

Both the public join flow (app/join_api.py) and the key-authenticated
node-management routes (app/nodes_api.py) accept the same two input
shapes for a node id -- `!a1b2c3d4` or bare `a1b2c3d4`, in any case --
and both need to agree on exactly the same canonical form before it
touches player_node, since that table's primary key is a literal
string compare (protocol, node_ref). Keeping this in one place means
there is only ever one definition of "valid node reference" in the
whole app, instead of two copies drifting apart.

The canonical form is bare lowercase 8-hex, with NO leading "!" --
not because Meshtastic's own convention lacks one (it writes
`!a1b2c3d4`), but because app/mc_ingest.py's auto-bind path (a
MeshCore radio's first wardriving ping) has been writing player_node
rows in that bare form since before this module existed, and every
live production row is already in it. Making bare the canonical form
means zero data migration for MeshCore; Meshtastic gets migrated
instead, at the one point (this function) both protocols' writers and
readers already had to funnel through. Storage and lookup use this
form everywhere -- display is a separate concern, handled by whichever
UI renders a node_ref back out (see frontend/join.js's
displayNodeRef()), not by this function.
"""
from __future__ import annotations

import re

_NODE_REF_RE = re.compile(r"^[0-9a-fA-F]{8}$")


def normalize_node_ref(raw: object) -> str | None:
    """Accept `!a1b2c3d4` or `a1b2c3d4` (any case); return the canonical
    bare lowercase `xxxxxxxx` form, or None if it isn't 8 hex characters.
    """
    if not isinstance(raw, str):
        return None
    bare = raw[1:] if raw.startswith("!") else raw
    if not _NODE_REF_RE.match(bare):
        return None
    return bare.lower()


def normalize_sender_name(raw: object) -> str | None:
    """Canonical form of a MeshCore check-in sender/display name, for
    matching against both app/checkin.py's explicit mc_checkin_binding
    table and its public-key directory bridge. Lives here (rather than
    in app/checkin.py itself) for the same reason normalize_node_ref
    does: app/checkin_api.py (the binding endpoint) and app/checkin.py
    (the poller that matches incoming messages) both need this, and a
    table whose primary key is a literal string compare on the name
    must never have two independent ideas of what "the same name" means
    -- and this module has no imports of its own, so it is a safe place
    for both of those (and anything else that ever needs it) to share
    it without risking an import cycle.

    Strips leading/trailing whitespace and folds case -- nothing else.
    Real MeshCore hardware sends names with trailing spaces and with
    emoji in them; trimming and case-folding are only meant to absorb
    "the same person typed a trailing space" or "two apps disagree on
    capitalization," not to guess that two visually different names are
    the same person, so emoji and internal whitespace are left exactly
    as sent. Returns None for anything that isn't a non-empty string
    once trimmed.
    """
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not name:
        return None
    return name.casefold()


def format_node_ref(node_id: int) -> str:
    """Bare lowercase 8-hex form of a Meshtastic node id -- the exact
    form player_node.node_ref is canonically stored and looked up in
    (see this module's docstring). The formatting inverse of
    normalize_node_ref() above for the case where the input is already
    a known-good integer node id (e.g. meshview's from_node_id,
    node_seen.node_id) rather than untrusted text -- there is nothing
    to validate here, only to format, so this is deliberately a
    separate, simpler function rather than routing an int through
    normalize_node_ref()'s string-shaped validation.

    app/checkin_api.py's Meshtastic node picker uses this directly; it
    is the same one-line format app/ingest.py's own _bare_node_ref
    computes for the check-in award path (kept as its own private
    helper there rather than migrated to call this, to avoid touching
    that module's tested position-ingest/scoring code for a purely
    cosmetic dedup) -- both compute the identical value from the
    identical formatting rule, so the two can never drift in practice
    even though they are, today, two call sites.
    """
    return f"{node_id:08x}"
