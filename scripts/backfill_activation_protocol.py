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

Each old row is traced back to the capture that earned it. credit_places
is called from the ingest path with the capture's own timestamp, so the
activation's awarded_at equals the capture's ts exactly -- match on
(player, ts) plus "the capture's square maps to this place", then read
the protocol off the season that capture belongs to.

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

resolved, ambiguous, orphan = [], [], []
for a in rows:
    protos = {r["protocol"] for r in conn.execute(
        "SELECT DISTINCT s.protocol FROM mc_tile_capture_log l "
        "  JOIN mc_season s ON s.id = l.season_id "
        "  JOIN place_cell pc ON pc.cell_id = l.cell_id "
        " WHERE l.by_player_id = ? AND l.ts = ? AND pc.place_id = ?",
        (a["player_id"], a["awarded_at"], a["place_id"]))}
    if len(protos) == 1:
        resolved.append((a["id"], protos.pop()))
    elif not protos:
        orphan.append(a["id"])
    else:
        ambiguous.append(a["id"])

print(f"  resolved : {len(resolved)}")
print(f"  ambiguous: {len(ambiguous)} (capture on both boards at the same instant -- left as '')")
print(f"  orphan   : {len(orphan)} (no matching capture -- left as '')")
by = {}
for _id, proto in resolved:
    by[proto] = by.get(proto, 0) + 1
print(f"  breakdown: {by}")

if not DRY:
    conn.executemany("UPDATE place_activation SET protocol = ? WHERE id = ?",
                     [(proto, _id) for _id, proto in resolved])
    print(f"  APPLIED {len(resolved)} update(s)")
else:
    print("  DRY RUN -- pass --apply to write")
conn.close()
