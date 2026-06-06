"""Snake draft for balanced Red/Blue team assignment.

Sort eligible nodes by log-scaled packet count (so high-traffic routers
don't dwarf clients), then serpentine-assign 1,2,2,1,1,2,2,1...

Zero-activity nodes are excluded — they can't paint tiles and would just
take up roster space.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class DraftEntry:
    node_id: int
    activity_score: float  # already log-scaled
    team: str               # 'RED' | 'BLUE'


def snake_draft(node_packet_counts: dict[int, int]) -> list[DraftEntry]:
    """Return the draft result.

    `node_packet_counts` maps node_id -> raw packet count over the window.
    Nodes with zero counts are excluded from the draft.
    """
    eligible = [
        (nid, math.log1p(cnt))
        for nid, cnt in node_packet_counts.items()
        if cnt > 0
    ]
    if not eligible:
        return []

    # Sort by descending score, then by node_id ascending for a stable
    # deterministic tiebreak.
    eligible.sort(key=lambda x: (-x[1], x[0]))

    result: list[DraftEntry] = []
    teams = ["RED", "BLUE"]
    direction = 1
    idx = 0
    for node_id, score in eligible:
        team = teams[idx]
        result.append(DraftEntry(node_id=node_id, activity_score=score, team=team))
        if direction == 1:
            if idx == len(teams) - 1:
                direction = -1
                # Snake: same team picks again at the turn.
            else:
                idx += 1
        else:
            if idx == 0:
                direction = 1
            else:
                idx -= 1
        # Apply direction change on the *next* iteration so the snake bounces.
    return result


def snake_draft_clean(node_packet_counts: dict[int, int]) -> list[DraftEntry]:
    """Cleaner serpentine implementation.

    Order for 2 teams: R, B, B, R, R, B, B, R, R, B, ...
    """
    eligible = [
        (nid, math.log1p(cnt))
        for nid, cnt in node_packet_counts.items()
        if cnt > 0
    ]
    if not eligible:
        return []
    eligible.sort(key=lambda x: (-x[1], x[0]))

    result: list[DraftEntry] = []
    n_teams = 2
    teams = ["RED", "BLUE"]
    for i, (node_id, score) in enumerate(eligible):
        round_num = i // n_teams
        pos = i % n_teams
        # Reverse every other round.
        team_idx = pos if (round_num % 2 == 0) else (n_teams - 1 - pos)
        result.append(
            DraftEntry(node_id=node_id, activity_score=score, team=teams[team_idx])
        )
    return result


# Use the clean version.
snake_draft = snake_draft_clean


def assign_new_node(red_count: int, blue_count: int, node_id: int) -> str:
    """Balance-assign a previously-unknown node to a team.

    Picks whichever team has fewer members. On a tie, uses node_id parity
    so the result is deterministic (same node always gets the same team on
    a tie — no flapping if called twice before persistence).
    """
    if red_count < blue_count:
        return "RED"
    if blue_count < red_count:
        return "BLUE"
    return "RED" if (node_id & 1) == 0 else "BLUE"
