"""Retroactively credit summit visits that predate the summit cell map.

Until 2026-08-31 a summit was the single square containing its peak and
nobody had ever tagged one. Now that summits cover the ground you can
reach them from, captures already in the log land on summit squares --
but place credits are awarded at ingest time, so nothing would ever have
been recorded for them. This replays those captures through the same
rules live scoring uses, so the honors are populated when they launch
rather than starting empty.

Additive only. It never rewrites or removes an existing activation, and
it skips any (place, player, week) that already has one.
"""
import sqlite3, sys
from app.place_rotation import week_start_for_ts
from app.place_scoring import WEEKLY_CAP_POINTS

DRY = "--apply" not in sys.argv
conn = sqlite3.connect("/data/game.db", isolation_level=None)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA busy_timeout=15000")

rows = conn.execute("""
  SELECT l.by_player_id pid, l.ts, l.cell_id,
         (SELECT p.id FROM place_cell pc JOIN place p ON p.id=pc.place_id
           WHERE pc.cell_id=l.cell_id AND p.active=1
           ORDER BY p.points DESC, p.id ASC LIMIT 1) place_id
    FROM mc_tile_capture_log l
   WHERE l.by_air = 0 AND l.by_player_id IS NOT NULL
   ORDER BY l.ts
""").fetchall()

made = []
# A dry run has to simulate the writes it is previewing: the
# already-credited check and the weekly cap both read place_activation,
# so without this every repeat capture of the same summit reports again
# and the cap never moves.
sim_keys = set()
sim_spent = {}
for r in rows:
    if r["place_id"] is None:
        continue
    p = conn.execute("SELECT id, name, ref_type, points FROM place WHERE id=?",
                     (r["place_id"],)).fetchone()
    if p is None or p["ref_type"] != "summit":
        continue
    ws = week_start_for_ts(r["ts"])
    key = (p["id"], r["pid"], ws)
    if key in sim_keys:
        continue
    if conn.execute("SELECT 1 FROM place_activation WHERE place_id=? AND player_id=? AND week_start=?",
                    key).fetchone():
        continue
    already = conn.execute(
        "SELECT COALESCE(SUM(points),0) FROM place_activation WHERE player_id=? AND week_start=?",
        (r["pid"], ws)).fetchone()[0] + sim_spent.get((r["pid"], ws), 0)
    remaining = WEEKLY_CAP_POINTS - already
    if remaining <= 0:
        print(f"  SKIP cap: player {r['pid']} week {ws} already at {already}")
        continue
    awarded = min(p["points"], remaining)
    sim_keys.add(key)
    if DRY:
        # Only in a dry run. With --apply the row is really written, so
        # the next iteration's SUM already counts it -- adding it here
        # too would double-count the spend and under-award a place that
        # should still fit inside the remaining cap.
        sim_spent[(r["pid"], ws)] = sim_spent.get((r["pid"], ws), 0) + awarded
    made.append((p["id"], r["pid"], ws, awarded, r["ts"], p["name"]))
    if not DRY:
        conn.execute(
            "INSERT INTO place_activation(place_id, player_id, week_start, points, awarded_at) "
            "VALUES (?,?,?,?,?)", (p["id"], r["pid"], ws, awarded, r["ts"]))

names = {x["player_id"]: x["display_name"] for x in conn.execute("SELECT player_id,display_name FROM player")}
print(f"\n  {'DRY RUN -- would insert' if DRY else 'INSERTED'} {len(made)} summit activation(s):")
for pid_place, pid, ws, awarded, ts, nm in made:
    print(f"    {nm[:26]:28} -> {names.get(pid,'?'):14} week {ws}  {awarded} pts")
conn.close()
