#!/usr/bin/env python3
"""Tag place credits written before place_activation had a protocol.

    python3 scripts/backfill_activation_protocol.py [--apply]

Run inside the app container with PYTHONPATH=/app; it opens /data/game.db.

Until 2026-08-31 a place credit recorded nothing about which board earned
it, so the place honors counted both boards at once: one Meshtastic drive
up Big Cottonwood put the same name on MeshCore's Peak Tagger, and
MeshCore activity decided the Meshtastic Tourist and Park Hopper. The
column exists now, but rows written before it do not have a value and
match neither board.

Each old row is traced back to the PING that earned it, via
player_cell_ping, which carries the protocol directly. credit_places is
called from the ingest path with the ping's own timestamp, so the
activation's awarded_at equals that ts exactly -- match on (player, ts)
plus "that square maps to this place".

player_cell_ping and not mc_tile_capture_log: a place is credited on any
scoring ping, including one that takes no square (a cooldown ping still
credits a place -- see credit_places' docstring), so the capture log
misses them. It left 41 of 98 rows untraceable on the preview clone.

player_cell_ping is pruned (it held only the last ~2 days on the preview
clone, against credits three weeks older), so a third and weaker signal
catches what the first two miss: a player whose registered radios are all
on ONE board cannot have earned a credit on the other. That is an
inference from player_node rather than a trace of the event, so it runs
last and is reported separately. It resolved 36 of the 38 remaining rows;
without it l3@n's nine park credits and TJ's landmarks would all have
stayed untagged and Park Hopper and Tourist would have vanished.

A row that cannot be placed unambiguously is LEFT ALONE rather than
guessed at. It keeps '' and stays invisible to both boards, which is the
safe direction: an honor that goes unawarded is recoverable, one awarded
to the wrong player on the wrong board is not.
"""
import sqlite3
import sys

DRY = "--apply" not in sys.argv
conn = sqlite3.connect("/data/game.db", isolation_level=None)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA busy_timeout=15000")

rows = conn.execute(
    "SELECT id, place_id, player_id, awarded_at FROM place_activation "
    " WHERE protocol = '' OR protocol IS NULL"
).fetchall()
print(f"  untagged activations: {len(rows)}")

radios = {}
for r in conn.execute("SELECT player_id, protocol FROM player_node"):
    radios.setdefault(r["player_id"], set()).add(r["protocol"])

resolved, inferred, ambiguous, orphan = [], [], [], []
for a in rows:
    protos = {r["protocol"] for r in conn.execute(
        "SELECT DISTINCT g.protocol FROM player_cell_ping g "
        "  JOIN place_cell pc ON pc.cell_id = g.cell_id "
        " WHERE g.player_id = ? AND g.ts = ? AND pc.place_id = ?",
        (a["player_id"], a["awarded_at"], a["place_id"]))}
    if not protos:
        # Fallback for a credit whose ping row is gone but whose square
        # change survived.
        protos = {r["protocol"] for r in conn.execute(
            "SELECT DISTINCT s.protocol FROM mc_tile_capture_log l "
            "  JOIN mc_season s ON s.id = l.season_id "
            "  JOIN place_cell pc ON pc.cell_id = l.cell_id "
            " WHERE l.by_player_id = ? AND l.ts = ? AND pc.place_id = ?",
            (a["player_id"], a["awarded_at"], a["place_id"]))}
    if len(protos) == 1:
        resolved.append((a["id"], protos.pop()))
        continue
    if protos:
        ambiguous.append(a["id"])
        continue
    # Weakest signal, last: a player with radios on only one board
    # cannot have scored on the other.
    owned = radios.get(a["player_id"]) or set()
    if len(owned) == 1:
        inferred.append((a["id"], next(iter(owned))))
    else:
        orphan.append(a["id"])

print(f"  traced   : {len(resolved)} (matched the ping or capture that earned it)")
print(f"  inferred : {len(inferred)} (player only owns radios on one board)")
print(f"  ambiguous: {len(ambiguous)} (capture on both boards at the same instant -- left as '')")
print(f"  orphan   : {len(orphan)} (no matching ping or capture -- left as '')")
by = {}
for _id, proto in resolved + inferred:
    by[proto] = by.get(proto, 0) + 1
print(f"  breakdown: {by}")

if not DRY:
    conn.executemany("UPDATE place_activation SET protocol = ? WHERE id = ?",
                     [(proto, _id) for _id, proto in resolved + inferred])
    print(f"  APPLIED {len(resolved) + len(inferred)} update(s)")
else:
    print("  DRY RUN -- pass --apply to write")
conn.close()
