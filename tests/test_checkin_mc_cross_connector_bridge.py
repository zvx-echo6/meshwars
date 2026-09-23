"""Tests for app/checkin.py's _resolve_mc_identities -- the fix for the
cross-mesh public-key prefix collision bug production logs constantly:

    WARNING checkin: mc contact 2bde9e1d matches 2 directory public
    keys, refusing to resolve (ambiguous)

Mechanism (see _resolve_mc_identities' own docstring for the full
story): _build_directory_bridge buckets directory entries by the first
8 hex of the public key. The OLD _resolve_mc_identities flattened every
OTHER connector's directory into one union before building a single
bridge over it -- so two unrelated nodes on two DIFFERENT meshes that
happen to share an 8-hex prefix (a real, if occasional, coincidence)
collided in that union index, even though each was perfectly
unambiguous within its own mesh. Widening the search this way turned a
CONFIDENT match into a refusal.

The fix builds one bridge PER other connector (never flattened) and
merges them with an explicit rule: one connector resolving a name, or
several agreeing on the same player, is fine; several resolving the
SAME name to DIFFERENT players is genuine ambiguity and is still
refused. The primary connector's own bridge still wins any remaining
overlap, unchanged.

Uses tests/conftest.py's `conn` fixture (in-memory db, real SCHEMA +
MIGRATIONS) -- plain conn-in, conn-out calls against
_resolve_mc_identities directly, no HTTP surface, no poller machinery.
"""
from __future__ import annotations

import logging
import time

from app.checkin import _resolve_mc_identities

NOW = int(time.time())


def _player(conn, name="Player") -> int:
    cur = conn.execute(
        "INSERT INTO player(display_name, team, created_at) VALUES (?,?,?)",
        (name, "RED", NOW),
    )
    return cur.lastrowid


def _bind(conn, *, player_id: int, node_ref: str) -> None:
    conn.execute(
        "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) VALUES ('mc', ?, ?, ?)",
        (node_ref, player_id, NOW),
    )


def _node(name: str, pubkey: str) -> dict:
    return {"name": name, "public_key": pubkey}


# ---- regression: a cross-mesh prefix collision no longer refuses ----------

def test_prefix_collision_across_connectors_still_resolves_confidently(conn):
    """The exact production scenario: player1's real node ("Alice") is
    only in connector A's directory. Connector B has no relation to
    player1 at all, but happens to carry an UNRELATED node ("Clutter")
    whose public key shares player1's contact's 8-hex prefix -- purely
    a cross-mesh coincidence. A second player (player2) has their own,
    non-colliding, unambiguous match entirely within connector B.

    Under the OLD flattened-union code, the union of A+B's directories
    put BOTH "Alice" and "Clutter" under the same by_prefix bucket,
    which _build_directory_bridge sees as 2 matches for player1's
    contact -- key_ambiguous, refused, "Alice" never resolves. Under
    the fix, connector A's own bridge alone resolves player1's contact
    unambiguously (only "Alice" is in A), and connector B's own bridge
    alone ALSO resolves it unambiguously (only "Clutter" shares that
    prefix within B) -- two different names, no collision between them,
    so nothing is refused. player2 is unaffected either way and serves
    as a control proving the merge didn't just start resolving
    everything.
    """
    player1 = _player(conn, "Player1")
    player2 = _player(conn, "Player2")
    _bind(conn, player_id=player1, node_ref="aaaa1111")
    _bind(conn, player_id=player2, node_ref="bbbb2222")

    dir_a = [_node("Alice", "aaaa1111" + "0" * 40)]
    dir_b = [
        _node("Clutter", "aaaa1111" + "f" * 40),  # unrelated node, same prefix as player1's contact
        _node("Bob", "bbbb2222" + "0" * 40),
    ]

    bridge = _resolve_mc_identities(conn, primary_directory=[], other_directories=[
        ("http://mesh-a.example", dir_a),
        ("http://mesh-b.example", dir_b),
    ])

    assert bridge.get("alice") == player1
    assert bridge.get("bob") == player2


def test_prefix_collision_never_logs_key_ambiguous_across_connectors(conn, caplog):
    """Same fixture as above, asserted from the log side: the
    production warning line ("matches N directory public keys") must
    NOT fire for player1's contact now that each connector is indexed
    on its own -- that ambiguity only ever existed in the old flattened
    union, never within either mesh alone.
    """
    player1 = _player(conn, "Player1")
    _bind(conn, player_id=player1, node_ref="aaaa1111")

    dir_a = [_node("Alice", "aaaa1111" + "0" * 40)]
    dir_b = [_node("Clutter", "aaaa1111" + "f" * 40)]

    with caplog.at_level(logging.WARNING, logger="checkin"):
        bridge = _resolve_mc_identities(conn, primary_directory=[], other_directories=[
            ("http://mesh-a.example", dir_a),
            ("http://mesh-b.example", dir_b),
        ])

    assert bridge.get("alice") == player1
    assert not any("matches" in r.message and "directory public keys" in r.message for r in caplog.records)


# ---- safety: a WITHIN-one-connector collision still refuses ---------------

def test_two_nodes_sharing_prefix_within_one_connector_still_refuses(conn, caplog):
    """Unlike the cross-connector case above, two directory entries
    sharing a prefix WITHIN the same connector's own directory are a
    genuine same-mesh collision -- _build_directory_bridge's existing
    key_ambiguous rule must still refuse this exactly as before. This
    proves the fix didn't weaken that safety property while fixing the
    cross-connector false positive.
    """
    player1 = _player(conn, "Player1")
    _bind(conn, player_id=player1, node_ref="cccc3333")

    dir_a = [
        _node("Charlie", "cccc3333" + "0" * 40),
        _node("Duplicate", "cccc3333" + "f" * 40),
    ]

    with caplog.at_level(logging.WARNING, logger="checkin"):
        bridge = _resolve_mc_identities(conn, primary_directory=[], other_directories=[
            ("http://mesh-a.example", dir_a),
        ])

    assert "charlie" not in bridge
    assert "duplicate" not in bridge
    assert any(
        "matches 2 directory public keys" in r.message and "cccc3333" in r.message
        for r in caplog.records
    )


# ---- several connectors agreeing on the same player is not ambiguous ------

def test_several_connectors_resolving_same_name_to_same_player_resolves(conn):
    """Two DIFFERENT connectors' own bridges both resolving the exact
    same name to the exact same player is corroboration, not ambiguity
    -- must resolve, not be refused.
    """
    player1 = _player(conn, "Player1")
    _bind(conn, player_id=player1, node_ref="dddd4444")

    # Two entirely separate directory entries (different full public
    # keys) that both happen to share player1's contact prefix AND
    # both happen to be named "Charlie" -- e.g. the same operator ran
    # the identical display name on two meshes.
    dir_a = [_node("Charlie", "dddd4444" + "0" * 40)]
    dir_b = [_node("Charlie", "dddd4444" + "f" * 40)]

    bridge = _resolve_mc_identities(conn, primary_directory=[], other_directories=[
        ("http://mesh-a.example", dir_a),
        ("http://mesh-b.example", dir_b),
    ])

    assert bridge.get("charlie") == player1


# ---- several connectors resolving to DIFFERENT players is ambiguous -------

def test_several_connectors_resolving_same_name_to_different_players_refuses(conn, caplog):
    """Two DIFFERENT connectors' own bridges resolving the SAME name to
    TWO DIFFERENT players is genuine ambiguity -- there is no safe way
    to tell which player that name actually belongs to -- and must
    still be refused, logged naming the connectors/players involved.
    """
    player1 = _player(conn, "Player1")
    player2 = _player(conn, "Player2")
    _bind(conn, player_id=player1, node_ref="dddd4444")
    _bind(conn, player_id=player2, node_ref="eeee5555")

    # player1's contact resolves to "Dave" within connector A alone.
    dir_a = [_node("Dave", "dddd4444" + "0" * 40)]
    # player2's contact ALSO resolves to "Dave" within connector B
    # alone -- an unrelated node, different player, same display name.
    dir_b = [_node("Dave", "eeee5555" + "0" * 40)]

    with caplog.at_level(logging.WARNING, logger="checkin"):
        bridge = _resolve_mc_identities(conn, primary_directory=[], other_directories=[
            ("http://mesh-a.example", dir_a),
            ("http://mesh-b.example", dir_b),
        ])

    assert "dave" not in bridge
    assert any(
        "resolves to different players across other connectors" in r.message and "dave" in r.message.lower()
        for r in caplog.records
    )


# ---- the primary connector still wins over any other connector's answer ---

def test_primary_connector_still_wins_over_other_connector(conn, caplog):
    """Unchanged behavior: when the primary connector's OWN bridge
    resolves a name to one player and the (post-merge) cross-connector
    bridge resolves the same name to a different player, the primary's
    answer wins -- see _resolve_mc_identities' docstring, this rule is
    untouched by the fix.
    """
    primary_player = _player(conn, "PrimaryPlayer")
    other_player = _player(conn, "OtherPlayer")
    _bind(conn, player_id=primary_player, node_ref="ffff6666")
    _bind(conn, player_id=other_player, node_ref="11112222")

    # Directory shaped so BOTH contacts resolve to the SAME name
    # "frank" -- on the primary connector's own directory for
    # primary_player's contact, and on one other connector's directory
    # for other_player's contact.
    primary_dir = [_node("Frank", "ffff6666" + "0" * 40)]
    other_dir = [_node("Frank", "11112222" + "0" * 40)]

    with caplog.at_level(logging.WARNING, logger="checkin"):
        bridge = _resolve_mc_identities(
            conn, primary_directory=primary_dir,
            other_directories=[("http://mesh-other.example", other_dir)],
            primary_connector="http://mesh-primary.example",
        )

    assert bridge.get("frank") == primary_player
    assert any(
        "keeping the primary connector's player" in r.message
        for r in caplog.records
    )
