"""_radios_out (app/nodes_api.py) returns a stored display name per
radio: MeshCore from checkin_node_name, Meshtastic from mt_node_key.
"""
from __future__ import annotations

from app.nodes_api import _radios_out


def _setup(conn):
    conn.execute(
        "INSERT INTO player(player_id, display_name, team, created_at) VALUES (1, 'P', 'RED', 1)"
    )
    for proto, ref, at in (("mc", "db66a62f", 1), ("mc", "8031b0cf", 2), ("mc", "a5f0fbf1", 3), ("mt", "1234abcd", 4)):
        conn.execute(
            "INSERT INTO player_node(protocol, node_ref, player_id, bound_at) VALUES (?, ?, 1, ?)",
            (proto, ref, at),
        )


def test_radios_out_includes_names(conn):
    _setup(conn)
    conn.execute(
        "INSERT INTO checkin_node_name(connector, node_ref, player_id, name, first_seen, changed_at) "
        "VALUES ('c1', 'db66a62f', 1, 'Old', 10, NULL), ('c2', 'db66a62f', 1, 'Con', 20, NULL), "
        "('c1', '8031b0cf', 1, 'Con 🚗', 10, NULL)"
    )
    conn.execute(
        "INSERT INTO mt_node_key(node_ref, public_key, long_name, first_seen, last_seen) "
        "VALUES ('1234abcd', 'aa', 'Older', 1, 5), ('1234abcd', 'bb', 'Newer', 1, 9)"
    )
    by_ref = {r["node_ref"]: r for r in _radios_out(conn, 1)}
    assert by_ref["db66a62f"]["name"] == "Con"
    assert by_ref["8031b0cf"]["name"] == "Con 🚗"
    assert by_ref["a5f0fbf1"]["name"] is None
    assert by_ref["1234abcd"]["name"] == "Newer"


def test_changed_at_wins_and_other_players_names_ignored(conn):
    _setup(conn)
    conn.execute("INSERT INTO player(player_id, display_name, team, created_at) VALUES (2, 'Q', 'BLUE', 1)")
    conn.execute(
        "INSERT INTO checkin_node_name(connector, node_ref, player_id, name, first_seen, changed_at) "
        "VALUES ('c1', 'db66a62f', 1, 'Renamed', 10, 50), ('c2', 'db66a62f', 1, 'Later first', 30, NULL), "
        "('c3', 'a5f0fbf1', 2, 'Someone else', 99, NULL)"
    )
    by_ref = {r["node_ref"]: r for r in _radios_out(conn, 1)}
    assert by_ref["db66a62f"]["name"] == "Renamed"
    assert by_ref["a5f0fbf1"]["name"] is None
