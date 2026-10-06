"""Background-role routing: which process owns which background loop.

settings.run_background_tasks (app/config.py) is the master switch: when
false a process serves HTTP only, whatever the roles say. When true,
settings.background_roles (env BACKGROUND_ROLES, comma-separated)
selects WHICH loops this process runs:

  all        every loop below (the default; a single all-in-one process)
  ingest     every loop except the board-cache publisher
  publisher  only the board-cache publisher (mc_api.run_forever)

The publisher is ~1 s of CPU every ~11 s (board build + json.dumps +
gzip), synchronous on the event loop. Run in the same process as ingest
it blocks ingest and check-ins; its own container (BACKGROUND_ROLES=
publisher) takes it off that event loop.

LOOP_ROLES is the ONE place a loop or gated startup write is assigned a
role. KNOWN_LOOPS is the explicit list of every such item; if the two
ever disagree this module fails to import, so a new loop cannot be
added without choosing its role. Asking about a name that is in neither
raises too.

This module deliberately imports nothing from app/ so app/config.py can
use parse_roles() in a validator without a cycle.
"""
from __future__ import annotations

ROLE_ALL = "all"
ROLE_INGEST = "ingest"
ROLE_PUBLISHER = "publisher"
VALID_ROLES = frozenset({ROLE_ALL, ROLE_INGEST, ROLE_PUBLISHER})

# Every background loop and every gated startup write, with its role.
LOOP_ROLES: dict[str, str] = {
    "ingest": ROLE_INGEST,
    "mc_ingest": ROLE_INGEST,
    "freqmapper_ingest": ROLE_INGEST,
    "checkin_poller": ROLE_INGEST,
    "mqtt_subscriber": ROLE_INGEST,
    "discord_outbox": ROLE_INGEST,
    # init_db()'s whole startup-writes block: places-seed thread and the
    # checkin/freqmapper/discord/tile-release config bootstraps.
    "startup_writes": ROLE_INGEST,
    "board_publisher": ROLE_PUBLISHER,
}

# Must list every loop explicitly; the check below keeps it in step
# with LOOP_ROLES.
KNOWN_LOOPS = (
    "ingest",
    "mc_ingest",
    "freqmapper_ingest",
    "checkin_poller",
    "mqtt_subscriber",
    "discord_outbox",
    "startup_writes",
    "board_publisher",
)

_missing = set(KNOWN_LOOPS) - set(LOOP_ROLES)
if _missing:
    raise RuntimeError(f"background loops with no role assigned in app/roles.py: {sorted(_missing)}")
_extra = set(LOOP_ROLES) - set(KNOWN_LOOPS)
if _extra:
    raise RuntimeError(f"app/roles.py LOOP_ROLES has entries not in KNOWN_LOOPS: {sorted(_extra)}")
_bad = {n: r for n, r in LOOP_ROLES.items() if r not in VALID_ROLES - {ROLE_ALL}}
if _bad:
    raise RuntimeError(f"app/roles.py assigns invalid roles: {_bad}")


def parse_roles(raw: str) -> frozenset[str]:
    """Parse a BACKGROUND_ROLES value. Raises ValueError on an unknown or empty role."""
    names = [p.strip().lower() for p in (raw or "").split(",") if p.strip()]
    if not names:
        raise ValueError(
            f"BACKGROUND_ROLES is empty; valid roles: {', '.join(sorted(VALID_ROLES))}"
        )
    unknown = [n for n in names if n not in VALID_ROLES]
    if unknown:
        raise ValueError(
            f"BACKGROUND_ROLES has unknown role(s) {unknown}; "
            f"valid roles: {', '.join(sorted(VALID_ROLES))}"
        )
    return frozenset(names)


def active_roles(background_roles: str) -> frozenset[str]:
    """Concrete roles (never 'all') for a BACKGROUND_ROLES value."""
    roles = parse_roles(background_roles)
    if ROLE_ALL in roles:
        return frozenset(VALID_ROLES - {ROLE_ALL})
    return roles


def loop_enabled(name: str, run_background_tasks: bool, background_roles: str) -> bool:
    """Should this process run loop/startup-write `name`?"""
    if name not in LOOP_ROLES:
        raise KeyError(f"background loop {name!r} has no role in app/roles.py LOOP_ROLES")
    roles = active_roles(background_roles)  # validates even when disabled
    if not run_background_tasks:
        return False
    return LOOP_ROLES[name] in roles
