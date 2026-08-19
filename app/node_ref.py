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
