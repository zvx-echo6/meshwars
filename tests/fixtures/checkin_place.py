"""Recorded-shape fixtures for the check-in placement tests: small copies
of the responses CoreScope and Beacon give (see app/checkin_place.py)."""

CS_MESSAGES = {
    "messages": [
        {"packetId": 101, "packetHash": "HASH_FIRSTHOP", "sender": "Marsing ID", "timestamp": "2026-10-07T01:10:00Z"},
        {"packetId": 102, "packetHash": "HASH_DIRECT", "sender": "Direct Dan", "timestamp": "2026-10-07T01:11:00Z"},
        {"packetId": 103, "packetHash": "HASH_AMBIG", "sender": "Ambig Amy", "timestamp": "2026-10-07T01:12:00Z"},
        {"packetId": 104, "packetHash": "HASH_BESTHOPS", "sender": "Hops Hank", "timestamp": "2026-10-07T01:13:00Z"},
    ],
    "total": 4,
}

CS_TRACES = {
    "HASH_FIRSTHOP": {"traces": [
        {"observer": "obsA", "observer_name": "Obs A", "path_json": "[\"1FD3\",\"BA95\"]", "snr": 5, "rssi": -90, "time": "t"},
    ]},
    "HASH_DIRECT": {"traces": [
        {"observer": "obsA", "observer_name": "Obs A", "path_json": "[]", "snr": 9, "rssi": -70, "time": "t"},
    ]},
    "HASH_AMBIG": {"traces": [
        {"observer": "obsA", "observer_name": "Obs A", "path_json": "[\"AA11\"]", "snr": 5, "rssi": -90, "time": "t"},
        {"observer": "obsB", "observer_name": "Obs B", "path_json": "[\"AA11\",\"BB22\"]", "snr": 5, "rssi": -90, "time": "t"},
    ]},
    # two observations: the longer one listed first; the 1-hop one must win
    "HASH_BESTHOPS": {"traces": [
        {"observer": "obsB", "observer_name": "Obs B", "path_json": "[\"BA95\",\"1FD3\",\"CC33\"]", "snr": 1, "rssi": -100, "time": "t"},
        {"observer": "obsA", "observer_name": "Obs A", "path_json": "[\"1FD3\"]", "snr": 5, "rssi": -90, "time": "t"},
    ]},
}

CS_OBSERVERS = {"observers": [
    {"id": "obsA", "name": "Obs A", "iata": "BOI", "lat": 43.5, "lon": -116.2},
    {"id": "obsB", "name": "Obs B", "iata": "PIH", "lat": 0, "lon": 0},
]}

CS_RESOLVE = {
    "1FD3": {"name": "Highland 3", "pubkey": "1fd3aabbccdd", "candidates": [{"lat": 43.585, "lon": -116.795}],
             "conflicts": [], "confidence": "unique_prefix"},
    "BA95": {"name": "Other", "pubkey": "ba95eeff", "candidates": [{"lat": 44.0, "lon": -116.0}],
             "conflicts": [], "confidence": "unique_prefix"},
    # same prefix, two nodes: must never be guessed
    "AA11": {"name": "Twin", "pubkey": "aa11", "candidates": [{"lat": 43.0, "lon": -116.0}, {"lat": 44.0, "lon": -115.0}],
             "conflicts": [{"name": "Other twin"}], "confidence": "ambiguous"},
    "BB22": {"name": "NoPos", "pubkey": "bb22", "candidates": [{"lat": None, "lon": None}], "conflicts": [],
             "confidence": "unique_prefix"},
}

BEACON_CHANNELS = {"items": [{"id": 7, "name": "#central-oregon", "keyKnown": True}, {"id": 8, "keyKnown": False}]}

BEACON_PAGE_1 = {"items": [
    {"id": 5003, "packetHash": "BH_DIRECT", "senderName": "Direct Dee", "content": "x", "sentAt": 1_791_000_300_000},
    {"id": 5002, "packetHash": "BH_HIGH", "senderName": "High Hal", "content": "x", "sentAt": 1_791_000_200_000},
], "nextCursor": "c2", "hasMore": True}
BEACON_PAGE_2 = {"items": [
    {"id": 5001, "packetHash": "BH_AMBIG", "senderName": "Amb Al", "content": "x", "sentAt": 1_791_000_100_000},
    {"id": 4999, "packetHash": "BH_NONE", "senderName": "None Nan", "content": "x", "sentAt": 1_790_000_000_000},
], "nextCursor": None, "hasMore": False}

BEACON_PACKETS = {
    "BH_DIRECT": {"observations": [
        {"observerName": "Boise Obs", "iata": "boi", "pathLength": {"hopCount": 0}, "resolvedPath": []},
    ]},
    "BH_HIGH": {"observations": [
        {"observerName": "O1", "iata": "BOI", "pathLength": {"hopCount": 3},
         "resolvedPath": [{"confidence": "high", "nodes": [{"name": "Far", "publicKey": "ffff0000", "latitude": 1.0, "longitude": 1.0}]}]},
        {"observerName": "O2", "iata": "BOI", "pathLength": {"hopCount": 1},
         "resolvedPath": [{"confidence": "high", "nodes": [{"name": "Near", "publicKey": "abcd1234ef", "latitude": 43.6, "longitude": -116.3}]}]},
    ]},
    "BH_AMBIG": {"observations": [
        {"observerName": "O1", "iata": "BOI", "pathLength": {"hopCount": 1},
         "resolvedPath": [{"confidence": "ambiguous", "nodes": [
             {"name": "A", "publicKey": "a", "latitude": 1.0, "longitude": 1.0},
             {"name": "B", "publicKey": "b", "latitude": 2.0, "longitude": 2.0}]}]},
        {"observerName": "O2", "iata": "BOI", "pathLength": {"hopCount": 2},
         "resolvedPath": [{"confidence": "high", "nodes": [
             {"name": "Two", "publicKey": "t", "latitude": 1.0, "longitude": 1.0},
             {"name": "Dup", "publicKey": "d", "latitude": 2.0, "longitude": 2.0}]}]},
    ]},
}
