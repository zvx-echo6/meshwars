"""Place existing check-in awards on the map (see app/checkin_place.py).

Run inside the app container:

    python -m app.checkin_place_backfill [--dry-run] [--net-id N] [--season-id S] [--replace] [--limit N]

Polite: one request a second per feed host, MeshWars User-Agent, bounded
timeouts. Idempotent and resumable: awards that already have a placement
are skipped (--replace redoes them), awards whose placement is only
'unplaced' are retried, and a feed error leaves an award alone for the next
run. Writes are short transactions, retried when the database is busy.
--dry-run reads the feeds and the database and writes nothing.

Two phases, so estimates can lean on every award that CAN be traced: first
every award is traced; then the ones whose message or path is gone get the
estimate (the player's nearest traced check-in, else their latest claimed
square). Prints a per-net summary and, after a real run, how many placed
awards each region holds in the active seasons.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import logging
import sys
import time

from .checkin_place import (
    PlaceResult, Placer, Transient, _select_sync, _estimate_sync, estimate, write_placement,
)
from .db import connect

log = logging.getLogger("checkin_place_backfill")

COLUMNS = ("awards", "first_hop", "observer", "observer_iata", "est_recent_checkin", "est_recent_square",
           "unplaced", "errors")


def _net_labels() -> dict[int, str]:
    c = connect()
    try:
        return {r["id"]: f"{r['id']} {r['label']} ({r['kind']})" for r in c.execute("SELECT id, label, kind FROM checkin_net")}
    finally:
        c.close()


async def run(*, dry_run: bool = False, net_id: int | None = None, season_id: int | None = None,
              replace: bool = False, limit: int | None = None, placer: Placer | None = None,
              out=sys.stdout, log_every: int = 25) -> dict[int, collections.Counter]:
    own = placer is None
    placer = placer or Placer()
    rows = await asyncio.to_thread(
        lambda: _select_sync(net_id=net_id, season_id=season_id, replace=replace, limit=limit))
    stats: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    failed: list = []
    reasons: dict[tuple, str | None] = {}
    why: collections.Counter = collections.Counter()
    started = time.monotonic()
    try:
        # Phase 1: trace.
        for i, row in enumerate(rows, 1):
            st = stats[row["net_id"]]
            st["awards"] += 1
            try:
                result = await placer.trace(row)
            except Transient as exc:
                log.warning("feed error for %s/%s/%s: %s", row["season_id"], row["player_id"], row["net_date"], exc)
                st["errors"] += 1
                continue
            if result is None:
                failed.append(row)
                reasons[(row["season_id"], row["player_id"], row["net_date"])] = placer.last_miss
                why[(row["net_id"], placer.last_miss or "unknown")] += 1
            else:
                st[result.method] += 1
                if not dry_run:
                    await asyncio.to_thread(write_placement, row, result, attempts=(row["p_attempts"] or 0) + 1)
            if log_every and i % log_every == 0:
                print(f"  traced {i}/{len(rows)} ({placer.requests()} requests, {time.monotonic() - started:.0f}s)",
                      file=sys.stderr)
        # Phase 2: estimate what could not be traced.
        for row in failed:
            est = await asyncio.to_thread(_estimate_sync, row)
            miss = reasons.get((row["season_id"], row["player_id"], row["net_date"]))
            if miss:
                est = est._replace(detail=f"{est.detail} [no trace: {miss}]")
            stats[row["net_id"]][est.method] += 1
            if not dry_run:
                await asyncio.to_thread(write_placement, row, est, attempts=(row["p_attempts"] or 0) + 1)
    finally:
        if own:
            await placer.aclose()
    print_summary(stats, out, dry_run=dry_run, requests=placer.requests())
    if why:
        labels = _net_labels()
        print("\nwhy awards could not be traced:", file=out)
        for (nid, reason), n in sorted(why.items(), key=lambda kv: (kv[0][0], -kv[1])):
            print(f"  {labels.get(nid, nid)}: {n} x {reason}", file=out)
    return stats


def print_summary(stats, out, *, dry_run: bool, requests: int) -> None:
    labels = _net_labels()
    head = ["net"] + list(COLUMNS)
    rows = [[labels.get(nid, str(nid))] + [str(st[c]) for c in COLUMNS] for nid, st in sorted(stats.items())]
    total = collections.Counter()
    for st in stats.values():
        total.update(st)
    rows.append(["TOTAL"] + [str(total[c]) for c in COLUMNS])
    widths = [max(len(r[i]) for r in [head] + rows) for i in range(len(head))]
    print(("DRY RUN - nothing written\n" if dry_run else "") + f"{requests} feed request(s)", file=out)
    for r in [head] + rows:
        print("  ".join(v.ljust(widths[i]) if i == 0 else v.rjust(widths[i]) for i, v in enumerate(r)), file=out)


def region_counts() -> list[tuple[str, int]]:
    """Placed check-in awards per region in the ACTIVE seasons, most first.
    A group is not listed, only individual regions."""
    c = connect()
    try:
        rows = c.execute(
            "SELECT rc.region_code AS code, COUNT(*) AS n "
            "  FROM mc_checkin_award a JOIN mc_season s ON s.id = a.season_id AND s.status = 'active' "
            "  JOIN checkin_award_place p ON p.season_id = a.season_id AND p.player_id = a.player_id "
            "                             AND p.net_date = a.net_date "
            "  JOIN region_cell rc ON rc.cell_id = p.cell_id "
            " GROUP BY rc.region_code "
            "UNION ALL "
            "SELECT p.region_code AS code, COUNT(*) AS n "
            "  FROM mc_checkin_award a JOIN mc_season s ON s.id = a.season_id AND s.status = 'active' "
            "  JOIN checkin_award_place p ON p.season_id = a.season_id AND p.player_id = a.player_id "
            "                             AND p.net_date = a.net_date "
            " WHERE p.region_code IS NOT NULL AND p.cell_id IS NULL GROUP BY p.region_code").fetchall()
    finally:
        c.close()
    tally: collections.Counter = collections.Counter()
    for r in rows:
        tally[r["code"]] += r["n"]
    return sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.checkin_place_backfill", description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="read the feeds, write nothing")
    ap.add_argument("--net-id", type=int, help="only awards of this checkin_net id")
    ap.add_argument("--season-id", type=int, help="only awards of this season id")
    ap.add_argument("--replace", action="store_true", help="redo awards that already have a placement")
    ap.add_argument("--limit", type=int, help="stop after this many awards (for a trial run)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run(dry_run=args.dry_run, net_id=args.net_id, season_id=args.season_id,
                    replace=args.replace, limit=args.limit))
    if not args.dry_run:
        from . import regions
        mapped = regions.map_new_cells()
        print(f"\nmapped {mapped} new cell(s) to regions")
        counts = region_counts()
        print("placed check-in awards per region, active seasons:")
        for code, n in counts or [("(none)", 0)]:
            print(f"  {code:<8} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
