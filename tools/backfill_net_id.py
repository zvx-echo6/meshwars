#!/usr/bin/env python3
"""One-time correction for mc_checkin_award rows written before net_id
existed.

    python3 tools/backfill_net_id.py                  # dry run (default) -- prints a report, writes NOTHING
    python3 tools/backfill_net_id.py --db-path X.db    # dry run against a specific database file
    python3 tools/backfill_net_id.py --apply --yes     # writes the corrections

Run inside the app container with PYTHONPATH=/app; it opens /data/game.db
by default (override with --db-path -- e.g. to dry-run against a copy).

Background
----------
Until 2026-09-10, app/checkin.py's checkin_streak() scoped a player's
streak by PROTOCOL alone (see that function's own docstring in
app/checkin.py for the full story, and app/db.py's mc_checkin_award.net_id
comment). Once a second MeshCore net (Coloradomesh, Thursdays) started
producing awards alongside the original one (Freq51, Wednesdays) on
2026-09-03, every MeshCore player's streak collapsed to 1 -- their own
attendance history was being checked against the OTHER net's dates too,
which they never had a chance to attend, so the very first comparison
always broke the "streak."

app/db.py's net_id column (added alongside this script) lets
checkin_streak() scope by the actual net going forward -- every award
written by the poller or the admin manual-credit endpoint from now on
carries the right net_id (or, rarely, NULL if genuinely ambiguous) at
the moment it is written. But every row written BEFORE that column
existed carries net_id = NULL and a streak/points value computed under
the old, broken, protocol-wide scoping. This script is the one-time fix
for that HISTORY. It does not change any code path, is not imported by
the running app, and changes nothing in the database at all unless run
with BOTH --apply AND --yes.

What it does, in order
-----------------------
1. Attribution: for every mc_checkin_award row with net_id IS NULL,
   match its (protocol, weekday-of-net_date) against checkin_net's own
   (protocol, weekday) -- against every configured net regardless of
   `enabled`, since what a night's own net was is a fact about the
   past, not about today's admin setting. If exactly one net matches,
   propose that net_id for the row. If zero or more than one net
   matches, the row is left NULL and reported separately -- never
   guessed at (same "leave it alone rather than guess" principle
   scripts/backfill_activation_protocol.py already uses for its own
   unresolvable rows).

2. Recompute: using the attribution from step 1 (held only in memory --
   nothing has been written to the database yet), replay
   checkin_streak()'s own algorithm for every row that now has a net_id
   (rows that already carried one are included too, purely as a
   consistency check -- their recomputed streak should always match
   what is already stored), in net_date order per (net_id, player_id).

3. Recompute what `points` WOULD be at the corrected streak, using
   checkin_config's current points/streak_bonus/streak_bonus_max -- the
   same formula app/checkin.py's streak_points() uses. Reimplemented
   here (not imported) so this script has no import-time dependency on
   the rest of the app and can run against a bare copy of the database
   file alone.

4. Report every row whose streak or points would change, grouped by
   player, with before/after values and a per-player point-total delta.

Safety
------
Dry run (the default -- no flags, or --apply given without --yes) opens
the database READ-ONLY via a sqlite3 `mode=ro` URI connection, so it is
not physically possible for this script to write anything in that mode
even if a bug tried to. --apply BY ITSELF still only reports -- an
explicit --yes is ALSO required to actually commit, so one typo'd flag
can never cause a write. Even under --apply --yes, every UPDATE runs
inside one transaction: either every proposed change lands, or (on any
error) none does.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date


def _weekday(net_date: str) -> int:
    y, m, d = (int(x) for x in net_date.split("-"))
    return date(y, m, d).weekday()


def streak_points(points: float, streak_bonus: float, streak_bonus_max: float, streak: int) -> float:
    """Mirrors app/checkin.py's streak_points() exactly: base points
    plus a per-attendance bonus, capped. Reimplemented rather than
    imported -- see module docstring on why this script has no
    dependency on the rest of the app.
    """
    bonus = min(streak_bonus * max(streak - 1, 0), streak_bonus_max)
    return points + bonus


def load_rows(conn):
    return conn.execute(
        "SELECT rowid, season_id, player_id, net_date, points, protocol, "
        "       message_id, streak, net_id "
        "  FROM mc_checkin_award ORDER BY net_date"
    ).fetchall()


def load_nets(conn):
    return conn.execute("SELECT id, label, protocol, weekday FROM checkin_net").fetchall()


def attribute(rows, nets):
    """Returns (attribution, ambiguous):
      attribution: {rowid: net_id or None} for every row.
      ambiguous:   [(row, matching_net_ids)] for every row left NULL
                   (rows that were NULL going in and stayed NULL).
    """
    by_proto_weekday: dict[tuple[str, int], list[int]] = {}
    for n in nets:
        by_proto_weekday.setdefault((n["protocol"], n["weekday"]), []).append(n["id"])

    attribution: dict[int, int | None] = {}
    ambiguous = []
    for r in rows:
        if r["net_id"] is not None:
            attribution[r["rowid"]] = r["net_id"]
            continue
        matches = by_proto_weekday.get((r["protocol"], _weekday(r["net_date"])), [])
        if len(matches) == 1:
            attribution[r["rowid"]] = matches[0]
        else:
            attribution[r["rowid"]] = None
            ambiguous.append((r, matches))
    return attribution, ambiguous


def recompute_streaks(rows, attribution):
    """Returns {rowid: new_streak}, computed exactly the way
    app/checkin.py's checkin_streak() computes it -- for every row whose
    attribution is not None. A row left NULL (see attribute() above)
    has no net to scope against and is skipped, same as
    checkin_streak() itself returning 1 for a None net_id without
    raising.
    """
    by_net: dict[int, list] = {}
    for r in rows:
        net_id = attribution[r["rowid"]]
        if net_id is None:
            continue
        by_net.setdefault(net_id, []).append(r)

    new_streak: dict[int, int] = {}
    for net_id, net_rows in by_net.items():
        dates = sorted({r["net_date"] for r in net_rows})
        attended_by_player: dict[int, set[str]] = {}
        for r in net_rows:
            attended_by_player.setdefault(r["player_id"], set()).add(r["net_date"])

        for r in net_rows:
            nd = r["net_date"]
            before = sorted((d for d in dates if d < nd), reverse=True)
            attended = attended_by_player.get(r["player_id"], set())
            streak = 1
            for d in before:
                if d not in attended:
                    break
                streak += 1
            new_streak[r["rowid"]] = streak
    return new_streak


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--db-path", default="/data/game.db",
                     help="sqlite file to read (and, with --apply --yes, write)")
    ap.add_argument("--apply", action="store_true",
                     help="write the corrections (still requires --yes)")
    ap.add_argument("--yes", action="store_true",
                     help="confirm --apply; required in addition to it to actually commit")
    args = ap.parse_args()

    do_write = args.apply and args.yes
    if args.apply and not args.yes:
        print("--apply given without --yes -- refusing to write. Running as a dry run instead.\n")

    if do_write:
        conn = sqlite3.connect(args.db_path, isolation_level=None)
    else:
        conn = sqlite3.connect(f"file:{args.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")

    try:
        rows = load_rows(conn)
        nets = load_nets(conn)
        cfg = conn.execute(
            "SELECT points, streak_bonus, streak_bonus_max FROM checkin_config WHERE id = 1"
        ).fetchone()
        if cfg is None:
            print("checkin_config has no row -- cannot compute points. Aborting.")
            sys.exit(1)

        net_summaries = [
            "{}={!r}(protocol={},weekday={})".format(n["id"], n["label"], n["protocol"], n["weekday"])
            for n in nets
        ]
        print(f"mode              : {'APPLY (writing)' if do_write else 'DRY RUN (read-only, writes nothing)'}")
        print(f"database          : {args.db_path}")
        print(f"mc_checkin_award rows : {len(rows)}")
        print(f"checkin_net rows      : {len(nets)}  ({', '.join(net_summaries)})")
        print(f"checkin_config        : points={cfg['points']} streak_bonus={cfg['streak_bonus']} "
              f"streak_bonus_max={cfg['streak_bonus_max']}")
        print()

        attribution, ambiguous = attribute(rows, nets)
        already_had_net_id = sum(1 for r in rows if r["net_id"] is not None)
        newly_attributed = sum(
            1 for r in rows if r["net_id"] is None and attribution[r["rowid"]] is not None
        )
        still_null = sum(1 for r in rows if attribution[r["rowid"]] is None)

        print("---- attribution ----")
        print(f"  already had net_id            : {already_had_net_id}")
        print(f"  newly attributed               : {newly_attributed}")
        print(f"  left NULL (ambiguous/no match) : {still_null}")
        if ambiguous:
            print("\n  rows left NULL:")
            for r, matches in ambiguous:
                reason = (
                    "no configured net shares this protocol+weekday" if not matches
                    else f"{len(matches)} nets share this protocol+weekday (ambiguous): {matches}"
                )
                print(
                    f"    season={r['season_id']} player={r['player_id']} net_date={r['net_date']} "
                    f"protocol={r['protocol']} -- {reason}"
                )
        print()

        new_streaks = recompute_streaks(rows, attribution)

        changes = []
        totals_old: dict[int, float] = {}
        totals_new: dict[int, float] = {}
        for r in rows:
            new_streak = new_streaks.get(r["rowid"])
            if new_streak is None:
                continue  # not attributed -- nothing to recompute against
            new_points = streak_points(cfg["points"], cfg["streak_bonus"], cfg["streak_bonus_max"], new_streak)
            old_points = r["points"]
            pid = r["player_id"]
            totals_old[pid] = totals_old.get(pid, 0.0) + old_points
            totals_new[pid] = totals_new.get(pid, 0.0) + new_points
            if r["streak"] != new_streak or abs(old_points - new_points) > 1e-9:
                changes.append((r, new_streak, new_points))

        print("---- streak/points changes ----")
        if not changes:
            print("  no rows would change.")
        for r, new_streak, new_points in sorted(
            changes, key=lambda c: (c[0]["player_id"], c[0]["net_date"])
        ):
            old_streak_disp = r["streak"] if r["streak"] is not None else "-"
            print(
                f"  player={r['player_id']:<6} net={attribution[r['rowid']]:<3} date={r['net_date']} "
                f"streak {old_streak_disp!s:>4} -> {new_streak:<4} "
                f"points {r['points']:>7.2f} -> {new_points:<7.2f}"
            )

        print()
        print("---- per-player point totals (players with at least one changed row) ----")
        changed_players = sorted({r["player_id"] for r, _, _ in changes})
        if not changed_players:
            print("  none.")
        for pid in changed_players:
            old_t = totals_old.get(pid, 0.0)
            new_t = totals_new.get(pid, 0.0)
            print(f"  player={pid:<6} old_total={old_t:>8.2f}  new_total={new_t:>8.2f}  delta={new_t - old_t:+.2f}")
        print()

        if do_write:
            print(f"APPLYING: {newly_attributed} net_id attribution(s), {len(changes)} streak/points correction(s)...")
            conn.execute("BEGIN IMMEDIATE")
            for r in rows:
                net_id = attribution[r["rowid"]]
                new_streak = new_streaks.get(r["rowid"], r["streak"])
                if new_streak is not None:
                    new_points = streak_points(
                        cfg["points"], cfg["streak_bonus"], cfg["streak_bonus_max"], new_streak
                    )
                else:
                    new_points = r["points"]
                conn.execute(
                    "UPDATE mc_checkin_award SET net_id = ?, streak = ?, points = ? WHERE rowid = ?",
                    (net_id, new_streak, new_points, r["rowid"]),
                )
            conn.execute("COMMIT")
            print("done.")
        else:
            print("DRY RUN -- nothing written. Pass --apply --yes to write these changes.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
