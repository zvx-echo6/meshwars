"""Render a Content dict (see app/announce_content.py's Content contract)
into ONE plain-text packet payload that fits a hard per-protocol UTF-8
byte budget, for transmission as a single LoRa packet.

THE HARD RULE, confirmed by the operator: a mesh announcement NEVER
spans more than one packet. Not now, not configurably, not ever. This
module feeds a game bot's radio transmit path, and a game bot has no
business consuming shared airtime with multi-packet traffic for what
is, at most, a short recap. When a Content does not fit the budget,
DETAIL IS DROPPED -- lowest priority first, whole rows at a time --
until what remains fits in a single packet. Nothing here ever asks a
caller to send more than one packet for one announcement; render_mesh()
below always returns exactly one string.

BLOCK FORMAT: each of the four Content kinds this module knows about
(daily_recap, weekly_recap, month_honors, net_wrapup) renders as a
short title line followed by one line per row, newline-separated --
not the single run-on sentence the very first version of this module
used. A newline costs the same single UTF-8 byte the old ". " row
separator did, so this is not a byte-budget regression; it buys a
client-parseable, human-legible shape instead. Any OTHER kind -- or a
block that still does not fit even after every degradation stage below
-- falls back to _render_one_line(), the original one-line sentence
renderer, kept in full for exactly that reason.

URL PLACEMENT: only weekly_recap and month_honors ever carry a url in
their block (see _daily_weekly_lines()/_month_lines()) -- daily_recap
never does (it fires every day; a link every time is noise) and
net_wrapup does not yet either (the operator has not decided on one for
it -- flagged in this task's own report, and a one-line change here
whenever that changes). The url itself is only ever dropped WHOLE, and
only when it cannot fit even entirely on its own -- never truncated,
and never the reason a row gets dropped (it is not competing with rows
for space the way the old one-line renderer's url did; see
_weekly_tail_line()/_month_lines()).

DEGRADATION LADDERS -- each kind degrades through its OWN ladder (see
_DAILY_WEEKLY_LADDER_STAGES / _MONTH_LADDER_STAGES / _NET_LADDER_STAGES
below), tried in order by _best_fitting_block() until one fits
`budget_bytes`:

  daily_recap / weekly_recap:
    1. Drop the weekly-only new-places COUNT, keeping the url (weekly's
       trailing line goes from "<n> new places · <domain>" to just
       "<domain>").
    2. Drop rows from the bottom (rank 5, then 4, ... never below 3
       rows) -- a row is dropped WHOLE, never truncated mid-row.
    3. Drop the team emoji, keeping the plain team name.

  month_honors:
    1. Drop Honors rows from the bottom (never below 3 rows) -- the url
       and winner headline are never touched by this stage.

  net_wrapup:
    1. Drop streak rows from the bottom (rank 5, then 4, ... never
       below 3 rows).
    2. Drop the team emoji, keeping the plain player name.
    3. Drop the "Top streaks" heading line.
    4. Drop the short net name from the title ("MW <net_name> Net"
       becomes a bare "MW Net").

Every kind then shares the SAME final two stages once its own ladder is
exhausted:
  Fall back to _render_one_line()'s previous one-line sentence form.
  Last resort: truncate on a UTF-8 codepoint boundary -- never split a
  codepoint -- which is _render_one_line()'s own final safety net,
  reused rather than reimplemented, so there is exactly one place in
  this module that ever truncates mid-content.

Every stage keeps the same hard guarantees this module has always given:
ALWAYS <= budget_bytes in UTF-8 bytes, deterministic (the same Content
and budget always produce byte-identical output -- the public API
serves this string from a cache while a bot renders the same Content
locally, and any drift between the two would be a bug, not a style
choice), never splits a codepoint, never emits a truncated URL (whole
or omitted, never cut), and a row is dropped whole, never mid-row.
"""
from __future__ import annotations

# Per-protocol single-packet payload budgets, taken from the sibling
# MeshWars radio-transport project's own protocol-aware chunker.
# MeshCore is the tighter of the two and is the one that matters here:
# a line that fits MESHCORE_BUDGET_BYTES fits MESHTASTIC_BUDGET_BYTES
# for free, so MeshCore is this module's default.
MESHCORE_BUDGET_BYTES = 150
MESHTASTIC_BUDGET_BYTES = 237

# Team -> single-glyph mesh emoji. ONE editable module-level dict, per
# the operator's own instruction, so a deployment can restyle or extend
# the team roster (settings.teams, app/config.py) without touching any
# rendering logic below. A team absent from this map (e.g. the operator
# added an 8th team without adding an entry here yet) renders with its
# plain name alone -- see _team_line() below -- rather than breaking.
#
# RED (U+1F534) and BLUE (U+1F535) are the original "circle" emoji
# (Unicode 6.0 / Emoji 1.0, 2010) -- about as broadly supported as an
# emoji gets. GREEN/YELLOW/ORANGE/PURPLE (U+1F7E2/E1/E0/E3, "large
# coloured circle") are Unicode 12.0 / Emoji 12.0 (2019) -- any client
# built in the last several years renders these fine. PINK (U+1FA77
# "pink heart") is Unicode 15.0 / Emoji 15.0 (2022) and MAY box (render
# as a placeholder glyph) on an older client's font -- the six plain
# coloured circles above are the safe choice if that ever matters.
TEAM_EMOJI = {
    "GREEN": "🟢",
    "YELLOW": "🟡",
    "ORANGE": "🟠",
    "RED": "🔴",
    "BLUE": "🔵",
    "PURPLE": "🟣",
    "PINK": "🩷",
}

# Named constants, not bare literals in _team_line() below -- one edit
# from an ASCII fallback (e.g. "^"/"v") for a client whose font is
# missing these glyphs entirely. ARROW_UP/ARROW_DOWN are each 3 UTF-8
# bytes (U+25B2/U+25BC, outside the ASCII range); ARROW_SAME is a
# single ASCII byte -- so the common case, an unchanged rank, is
# deliberately the CHEAP row: a fully-unchanged Top 5 costs 10 fewer
# bytes than one where every team moved.
ARROW_UP = "▲"
ARROW_DOWN = "▼"
ARROW_SAME = "="

# Block title lines for the two kinds with a FIXED (never degraded)
# title -- "MW Daily Top 5"/"MW Weekly Top 5" are the operator's own
# exact target-format titles. month_honors and net_wrapup each build
# their own title dynamically instead (see _month_lines()/_net_title())
# -- month's carries the month name, net's carries the net's own short
# name and can itself be degraded away (see _NET_LADDER_STAGES).
_BLOCK_TITLES = {
    "daily_recap": "MW Daily Top 5",
    "weekly_recap": "MW Weekly Top 5",
}

# season_close's own fixed glyphs -- see _season_close_lines().
TROPHY = "🏆"
MEDALS = ("🥇", "🥈", "🥉")

# The row-count floor the degradation ladder never drops below -- see
# module docstring and the ladders below, all built from this tuple so
# the floor (3) only ever needs to change in one place.
_ROW_CAP_STAGES = (5, 4, 3)

# The degradation ladder for daily_recap/weekly_recap, IN ORDER: each
# stage is the kwargs _daily_weekly_lines() is called with.
# _best_fitting_block() below tries these in order and returns the
# first block that fits -- strictly sequential (never a full cross-
# product retry), so a row is never dropped before the weekly
# new-places COUNT, and emoji are never dropped before rows are. The
# url itself has no stage of its own -- see module docstring's URL
# PLACEMENT paragraph: it is either present at every stage (when it
# fits at all) or absent at every stage (when it does not fit even
# alone), never traded away for a row or an emoji.
_DAILY_WEEKLY_LADDER_STAGES = (
    dict(drop_count=False, row_cap=_ROW_CAP_STAGES[0], use_emoji=True),
    *(dict(drop_count=True, row_cap=cap, use_emoji=True) for cap in _ROW_CAP_STAGES),
    dict(drop_count=True, row_cap=_ROW_CAP_STAGES[-1], use_emoji=False),
)

# month_honors has only one dimension to degrade (its Honors rows) --
# the url is always attempted in full (see _month_lines()) and there is
# no emoji or count to drop.
_MONTH_LADDER_STAGES = tuple(dict(row_cap=cap) for cap in _ROW_CAP_STAGES)

# net_wrapup's own ladder, in the operator's own exact specified order:
# streak rows from the 5th upward (never below 3), THEN the emoji, THEN
# the "Top streaks" heading, THEN the short net name in the title
# (falling back to a bare "MW Net") -- see _net_lines() below.
_NET_LADDER_STAGES = (
    *(dict(row_cap=cap, use_emoji=True, show_heading=True, show_net_name=True)
      for cap in _ROW_CAP_STAGES),
    dict(row_cap=_ROW_CAP_STAGES[-1], use_emoji=False, show_heading=True, show_net_name=True),
    dict(row_cap=_ROW_CAP_STAGES[-1], use_emoji=False, show_heading=False, show_net_name=True),
    dict(row_cap=_ROW_CAP_STAGES[-1], use_emoji=False, show_heading=False, show_net_name=False),
)

# season_close's own ladder, in the operator's own exact specified
# order: the default render never shows a number beside a podium team
# at all (see _medal_line()/_season_close_lines() -- the podium is
# ordered by the combined total, not the raw squares figure this row
# used to print, and showing squares next to a total-ordered podium
# read as a contradiction/bug to anyone on a radio, so the number is
# gone at every stage, not just the degraded ones). What DOES degrade,
# in order: the medal emoji, THEN the trophy emoji in the title, THEN
# the third-place row entirely (first/second, the winning team, the
# congratulations line, and the url are the LAST things to go -- see
# _season_close_lines()). Codepoint-safe truncation, if even the
# 2-row block does not fit, is the shared final fallback every kind's
# ladder bottoms out to -- see _best_fitting_block()/_render_one_line().
_SEASON_CLOSE_LADDER_STAGES = (
    dict(show_medals=True, show_trophy=True, row_cap=3),
    dict(show_medals=False, show_trophy=True, row_cap=3),
    dict(show_medals=False, show_trophy=False, row_cap=3),
    dict(show_medals=False, show_trophy=False, row_cap=2),
)

# Section headings _row_texts() (the one-line fallback's own row
# gatherer, see _render_one_line()) must never flatten in: "Standings"
# (app/announce_content.py's full ALL-teams roster) restates, verbatim,
# the same row text the movers-only "Placement" section already
# contributes for any team that actually moved -- including it too
# would duplicate that row's text in the one-line fallback. The block
# renderer above reads "Standings" directly (see _daily_weekly_lines());
# only the one-line fallback needs to skip it.
_ONE_LINE_SKIP_HEADINGS = {"Standings"}


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Byte-safe truncation: cut `text` to at most `max_bytes` UTF-8
    bytes without ever splitting a multi-byte codepoint. Content is
    ASCII by construction today (app/announce_content.py's own HARD
    RULE) except for this module's own team emoji/arrows, but this
    budgets in bytes and slices safely regardless, so a non-ASCII
    character can never emit a broken byte sequence.
    Decoding the truncated byte slice with errors="ignore" drops only
    an incomplete trailing sequence -- every byte that belonged to a
    codepoint which decoded cleanly is kept.
    """
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()


def _section_rows(content: dict, heading: str) -> list[dict]:
    """The `rows` list of the first section in `content["sections"]`
    with this exact `heading`, or [] if no such section exists (a
    quieter window that never built it, or a synthetic/legacy Content
    with a different shape).
    """
    for section in content.get("sections") or []:
        if section.get("heading") == heading:
            return section.get("rows") or []
    return []


def _team_line(row: dict, use_emoji: bool) -> str:
    """One team's row: "EMOJI NAME ARROW PLACE" (or "NAME ARROW PLACE"
    with `use_emoji=False`, degradation stage 3, or when `row["team"]`
    has no entry in TEAM_EMOJI -- a team missing from the map renders
    with its name alone rather than breaking).

    Arrow: ARROW_UP when the team's rank IMPROVED (a smaller rank
    number is better -- 1st beats 2nd), ARROW_DOWN when it dropped,
    ARROW_SAME when unchanged OR when there is no prior rank to compare
    against at all (a team new to the board this window -- see
    app/announce_content.py's _standings_rows(), which already folds
    that case into "unchanged" for exactly this reason).
    """
    team = row.get("team") or ""
    rank = row.get("rank")
    rank_was = row.get("rank_was")
    if rank_was is None or rank_was == rank:
        arrow = ARROW_SAME
    elif rank_was > rank:
        arrow = ARROW_UP
    else:
        arrow = ARROW_DOWN

    name = team
    if use_emoji:
        emoji = TEAM_EMOJI.get(team)
        if emoji:
            name = f"{emoji} {team}"

    return f"{name} {arrow} {rank}"


def _strip_scheme(url: str) -> str:
    """"https://host/path" -> "host/path" -- the scheme buys nothing on
    a packet this tight (every mesh client capable of following a link
    already knows to prepend it), so month_honors and season_close both
    render their own url this compact way (see _month_lines()/
    _season_close_lines()). content["url"] itself keeps the full,
    scheme-qualified form (unaffected by this function) for the JSON API
    and the _render_one_line() fallback.
    """
    return url.split("://", 1)[-1]


def _domain_only(url: str) -> str:
    """Bare host, scheme and path both stripped -- "https://host/path"
    or "https://host" both become "host". weekly_recap's own trailing
    line (see _weekly_tail_line() below) only has room for the site's
    name, not a full link -- month_honors/season_close render a
    scheme-stripped FULL link instead (see _strip_scheme()) since they
    link to a SPECIFIC page, not just the site.
    """
    return _strip_scheme(url).split("/", 1)[0]


def _weekly_tail_line(content: dict, drop_count: bool) -> str | None:
    """weekly_recap's own trailing line: "<n> new places · <domain>" --
    the new-places COUNT (dropped first, degradation stage 1, via
    `drop_count`) and the site's bare domain (see _domain_only()), each
    included independently of the other. None (no trailing line at all)
    when neither piece is available -- content["new_places"] is always
    an int once weekly content exists at all (see build_weekly_content()
    own comment on that field) so in practice this only happens when
    `drop_count=True` AND no url is configured.
    """
    parts = []
    new_places = content.get("new_places")
    if not drop_count and new_places is not None:
        noun = "place" if new_places == 1 else "places"
        parts.append(f"{int(new_places)} new {noun}")
    url = content.get("url")
    if url:
        parts.append(_domain_only(url))
    return " · ".join(parts) if parts else None


def _daily_weekly_lines(content: dict, *, drop_count: bool, row_cap: int,
                         use_emoji: bool) -> list[str]:
    """Title line, then the Top 5 teams by current rank (never ranks 6
    or 7 -- filtered out here regardless of `row_cap`, not just capped
    by it), each as one _team_line(). weekly_recap only: a trailing
    "<n> new places · <domain>" line from _weekly_tail_line() -- see
    module docstring's URL PLACEMENT paragraph for why daily_recap never
    gets a url or a new-places line at all.
    """
    kind = content.get("kind")
    lines = [_BLOCK_TITLES[kind]]

    standings = [r for r in _section_rows(content, "Standings") if r.get("rank") is not None]
    standings = sorted((r for r in standings if r["rank"] <= 5), key=lambda r: r["rank"])
    for row in standings[:row_cap]:
        lines.append(_team_line(row, use_emoji))

    if kind == "weekly_recap":
        tail = _weekly_tail_line(content, drop_count)
        if tail:
            lines.append(tail)

    return lines


def _month_lines(content: dict, *, row_cap: int) -> list[str]:
    """Title line ("MW <Month name> Top 5" -- the month spelled out, no
    year), then up to `row_cap` teams by squares held (already ordered
    "most squares first," zero-square teams already excluded, by
    build_month_content()'s own "Standings" section), each "EMOJI NAME
    SQUARES" -- thousands-separated, NO arrows (a month has no "before"
    snapshot to compare against the way daily/weekly's own Standings
    does). No separate winner-sentence line -- SAME block shape as
    weekly/daily, so a reader learns one layout; the winner is simply
    whichever team leads row 1. The results URL is scheme-stripped but
    keeps its path (see _strip_scheme() -- "meshwars.com/results", NOT
    the weekly block's bare-DOMAIN-only form: a month links to a
    SPECIFIC page, not just the site, and that distinction is the whole
    point of the link) and included whenever present, at every
    degradation stage, since URL priority (never dropped for a row's
    sake, never truncated) is guaranteed by the _render_one_line()
    fallback if even the least-degraded block here does not fit; this
    function itself never has to choose between the url and a row.
    """
    period_label = content.get("period_label") or ""
    lines = [f"MW {period_label} Top 5"]

    for row in _section_rows(content, "Standings")[:row_cap]:
        team = row.get("team") or ""
        squares = row.get("value") or 0
        emoji = TEAM_EMOJI.get(team)
        name = f"{emoji} {team}" if emoji else team
        lines.append(f"{name} {squares:,}")

    url = content.get("url")
    if url:
        lines.append(_strip_scheme(url))
    return lines


def _streak_line(row: dict, use_emoji: bool) -> str:
    """One check-in's row: "EMOJI NAME STREAK" (or "NAME STREAK" with
    `use_emoji=False`, or when `row["team"]` has no entry in TEAM_EMOJI
    -- same missing-team fallback _team_line() uses for placement rows).
    `row["team"]` is the player's CURRENT team (see
    build_net_wrapup_content()'s own comment on that field); `row["value"]`
    is the stored streak, read back verbatim, never recomputed here.
    """
    team = row.get("team") or ""
    name = row.get("player") or ""
    streak = row.get("value")

    label = name
    if use_emoji:
        emoji = TEAM_EMOJI.get(team)
        if emoji:
            label = f"{emoji} {name}"

    return f"{label} {streak}"


def _net_title(content: dict, show_net_name: bool) -> str:
    """"MW <net_name> Net" -- or a bare "MW Net" once degradation stage
    4 drops the short net name (see _NET_LADDER_STAGES). Never a date:
    unlike period_label (kept on the Content for JSON/API consumers and
    for deciding which occurrence is due in app/announce.py), this
    module's title line carries no date at all, matching every other
    kind's own fixed title ("MW Weekly Top 5" etc.).
    """
    net_name = content.get("net_name") if show_net_name else None
    return f"MW {net_name} Net" if net_name else "MW Net"


def _net_lines(content: dict, *, row_cap: int, use_emoji: bool, show_heading: bool,
                show_net_name: bool) -> list[str]:
    """Title line ("MW <net_name> Net", see _net_title()), the wrap-up's
    own headline ("<n> checked in"), an optional "Top streaks" heading
    (dropped at degradation stage 3 -- it earns its bytes: without it a
    name and a bare number have no context on a radio), then up to
    `row_cap` streak rows (already ordered "top streak first" by
    build_net_wrapup_content()), each as one _streak_line(). No url --
    app/announce_content.py's build_net_wrapup_content() does not set
    one; the operator has not decided whether net wrap-ups should link
    anywhere, and this is a one-line addition here (mirroring
    _month_lines()'s own `if url:` line) whenever that changes.
    """
    lines = [_net_title(content, show_net_name)]
    headline = content.get("headline")
    if headline:
        lines.append(headline)
    if show_heading:
        lines.append("Top streaks")
    rows = _section_rows(content, "Check-ins")
    for row in rows[:row_cap]:
        lines.append(_streak_line(row, use_emoji))
    return lines


def _medal_line(row: dict, medal: str | None) -> str:
    """One season_close standings row: "MEDAL EMOJI NAME" -- no figure.
    The podium is ordered by the combined total (squares + check-in +
    place points, see build_season_close_content()'s own docstring),
    not the squares this row's `value` field still carries for JSON/API
    consumers -- printing that number beside a total-ordered podium
    could read as an out-of-order/contradictory result to anyone on a
    radio (e.g. a team with fewer squares placed above one with more),
    so the default render shows only rank, medal, and name; a richer
    destination can show the numbers from the structured row fields.
    `medal=None` drops the medal per the current degradation stage.
    Same missing-team-emoji fallback _team_line()/_streak_line() both
    use.
    """
    team = row.get("team") or ""
    emoji = TEAM_EMOJI.get(team)
    name = f"{emoji} {team}" if emoji else team
    prefix = f"{medal} " if medal else ""
    return f"{prefix}{name}"


def _season_close_lines(content: dict, *, show_medals: bool,
                          show_trophy: bool, row_cap: int) -> list[str]:
    """"MW SEASON OVER" title (trophy-framed until degradation stage 2
    drops it), a "Congratulations <winner>!" line (the winning team's
    name WITHOUT its emoji -- repeating it would cost bytes for no new
    information, since the winner already leads row 1), then up to
    `row_cap` (3, dropping to 2 at the final stage -- "third place" is
    the only row this ladder ever drops) standings rows, medal-ranked
    but numberless (see _medal_line() -- the podium's ORDER comes from
    the combined total; what it DISPLAYS is only the medal, team emoji,
    and name, never a figure, so an order that legitimately disagrees
    with squares never LOOKS like it disagrees with anything). The url
    last, whenever present -- scheme-stripped but keeping its path (see
    _strip_scheme(), same as _month_lines()'s own url line, for the
    same "distinguishes the click" reason). A block builder never has
    to choose between the url and a row here either way (the
    _render_one_line() fallback's own reserved-bytes priority covers
    that case if this block never fits at all).
    """
    lines = [f"{TROPHY} MW SEASON OVER {TROPHY}" if show_trophy else "MW SEASON OVER"]

    winner = content.get("winner")
    if winner:
        lines.append(f"Congratulations {winner}!")

    standings = _section_rows(content, "Standings")
    for i, row in enumerate(standings[:row_cap]):
        medal = MEDALS[i] if show_medals and i < len(MEDALS) else None
        lines.append(_medal_line(row, medal))

    url = content.get("url")
    if url:
        lines.append(_strip_scheme(url))
    return lines


_BLOCK_BUILDERS = {
    "daily_recap": _daily_weekly_lines,
    "weekly_recap": _daily_weekly_lines,
    "month_honors": _month_lines,
    "net_wrapup": _net_lines,
    "season_close": _season_close_lines,
}

# Which ladder (see the four _*_LADDER_STAGES tuples above) each block
# builder degrades through -- kept as a separate mapping, rather than
# folded into _BLOCK_BUILDERS, since two different kinds (daily/weekly)
# legitimately share one builder AND one ladder while month/net/season
# each need their own of both.
_LADDER_STAGES_BY_KIND = {
    "daily_recap": _DAILY_WEEKLY_LADDER_STAGES,
    "weekly_recap": _DAILY_WEEKLY_LADDER_STAGES,
    "month_honors": _MONTH_LADDER_STAGES,
    "net_wrapup": _NET_LADDER_STAGES,
    "season_close": _SEASON_CLOSE_LADDER_STAGES,
}


def _best_fitting_block(content: dict, budget_bytes: int, builder, stages) -> str | None:
    """Try `builder` at every stage of `stages`, in order, and return
    the first newline-joined block that fits `budget_bytes` of UTF-8 --
    or None if even the most degraded stage does not fit, telling the
    caller to fall back to _render_one_line().
    """
    for stage in stages:
        lines = builder(content, **stage)
        if not lines:
            continue
        block = "\n".join(lines)
        if len(block.encode("utf-8")) <= budget_bytes:
            return block
    return None


def render_mesh(content: dict, budget_bytes: int = MESHCORE_BUDGET_BYTES) -> str:
    """Render `content` into one packet payload that fits `budget_bytes`
    of UTF-8, always -- see this module's own docstring for the full
    degradation ladder and the guarantees every stage keeps.

    Dispatches on content["kind"]: a kind this module has a block
    builder AND a ladder for (_BLOCK_BUILDERS / _LADDER_STAGES_BY_KIND)
    is rendered as a newline-separated block, degrading through its own
    ladder until one fits; any other kind, or a block that never fits
    even fully degraded, is rendered by _render_one_line() -- the
    original one-line sentence form, which carries its own complete
    degradation/guarantee logic independently of everything above.
    """
    kind = content.get("kind")
    builder = _BLOCK_BUILDERS.get(kind)
    stages = _LADDER_STAGES_BY_KIND.get(kind)
    if builder is not None and stages is not None:
        block = _best_fitting_block(content, budget_bytes, builder, stages)
        if block is not None:
            return block
    return _render_one_line(content, budget_bytes)


# ---------------------------------------------------------------------
# The one-line sentence renderer -- this module's ORIGINAL shape, kept
# in full as degradation ladder stage 4/5 (see render_mesh() and this
# module's own docstring) and as the only renderer for a Content kind
# _BLOCK_BUILDERS does not know about.
# ---------------------------------------------------------------------


def _row_texts(content: dict) -> list[str]:
    """Every row's pre-rendered `text`, across all sections EXCEPT
    _ONE_LINE_SKIP_HEADINGS (see that set's own comment -- "Standings"
    would otherwise duplicate a "Placement" mover row's exact text), in
    the order the builder wrote them. Section `heading` strings (e.g.
    "Placement", "Honors") are never emitted here -- per this module's
    own degradation ladder, they exist for JSON/API consumers and the
    block renderer above, not for a packet with no room left for a
    label.
    """
    texts = []
    for section in content.get("sections") or []:
        if section.get("heading") in _ONE_LINE_SKIP_HEADINGS:
            continue
        for row in section.get("rows") or []:
            text = row.get("text")
            if text:
                texts.append(text)
    return texts


def _row_redundant_with_headline(row_text: str, headline: str) -> bool:
    """True when `row_text` restates a fact `headline` already states,
    so it should be SKIPPED AT RENDER TIME ONLY -- the Content's own
    `sections` are left untouched; JSON/API consumers still see the full
    row list. This is a distinct reason from the budget-driven dropping
    documented on render_mesh(): that drops whole rows because there is
    no room left; this drops a row because it carries no NEW information
    over the headline, no matter how much room is left.

    This exact shape of bug -- a headline and a detail line built
    independently ending up saying the same thing twice -- already bit
    this codebase once, in the Discord weekly recap (app/discord_notify.py),
    where a few wasted characters cost nothing. On a ~150 byte MeshCore
    packet the same duplication is not free: those bytes could have been
    a second team's rank change, so paying for the same fact twice here
    is unaffordable.

    "Redundant" is intentionally loose (contains-either-way, not an
    exact match): casefold both strings and strip trailing punctuation,
    then check whether either one contains the other. A row that adds
    something the headline doesn't have -- e.g. "BLUE: 1st (was 2nd)"
    against a headline of "BLUE climbed to 1st" -- is NOT a substring
    either direction, so it survives.
    """
    a = row_text.strip().rstrip(".!?").casefold()
    b = headline.strip().rstrip(".!?").casefold()
    if not a or not b:
        return False
    return a in b or b in a


def _render_one_line(content: dict, budget_bytes: int = MESHCORE_BUDGET_BYTES) -> str:
    """Render `content` into one line that fits `budget_bytes` of UTF-8,
    always. Deterministic: the same content and budget always produce
    byte-identical output.

    THE ORIGINAL renderer (before the block format above existed), now
    reached in exactly two cases: a Content `kind` render_mesh() has no
    block builder for, and degradation ladder stage 4 -- a block that
    still does not fit `budget_bytes` even fully degraded (row_cap=3,
    no emoji, new-places line dropped).

    Degrades in this exact priority order when the full line does not
    fit (see module docstring for why degrading beats ever splitting
    across packets):
      1. `{prefix} {period_label}: {headline}` -- always present, and
         the only piece ever truncated (a last resort, only reachable
         with an absurdly small budget_bytes).
      2. `url` -- when present, its bytes are RESERVED UP FRONT, before
         any row is even considered, so a row is never the reason a
         link gets dropped (Matt asked specifically that the monthly
         results link survive over an extra row). If the url cannot fit
         WHOLE inside `budget_bytes`, it is dropped entirely rather than
         truncated -- a half URL is a broken link that still costs
         airtime, which is strictly worse than no URL at all.
      3. rows, joined by ". ", stopping at the first row that does not
         fit -- a row is included whole or omitted entirely, never
         truncated mid-way (mirrors the Discord recap's existing rule
         of dropping whole sections rather than truncating them).
    """
    board = (content.get("board") or "").upper()
    prefix = f"MW {board}" if board else "MW"
    period_label = content.get("period_label") or ""
    headline = content.get("headline") or ""
    url = content.get("url") or None

    # Fix 4: a half URL is worse than no URL -- it is a broken link that
    # still costs airtime. If the url cannot fit WHOLE inside the hard
    # budget, drop it entirely here rather than let the final
    # safety-net truncation below cut it in half. The hard byte cap
    # itself never moves; this only decides whether the url is worth
    # attempting at all.
    if url and len(url.encode("utf-8")) > budget_bytes:
        url = None

    head_bits = [b for b in (prefix, period_label) if b]
    head = " ".join(head_bits)
    head = f"{head}: {headline}" if head and headline else (headline or head)

    # Priority 2: reserve the url's bytes before anything else gets a
    # budget. Only what is left over is available to priorities 1 and 3.
    url_suffix = f" {url}" if url else ""
    body_budget = budget_bytes - len(url_suffix.encode("utf-8"))
    if body_budget < 0:
        # Absurd-budget floor: even the url alone would blow the
        # budget. Nothing is left to reserve for -- fall through and
        # let the final safety-net truncation below produce an
        # in-budget string rather than ever exceeding budget_bytes.
        body_budget = 0

    # Priority 1, with its documented last-resort truncation.
    head = _truncate_utf8(head, body_budget)

    parts = [head] if head else []
    used = len(head.encode("utf-8"))
    for row_text in _row_texts(content):
        if _row_redundant_with_headline(row_text, headline):
            continue
        sep = ". " if parts else ""
        candidate_bytes = used + len(sep.encode("utf-8")) + len(row_text.encode("utf-8"))
        if candidate_bytes > body_budget:
            break  # stop at the first row that doesn't fit -- never skip ahead to a shorter later one
        parts.append(row_text)
        used = candidate_bytes

    body = ". ".join(parts).rstrip(" .")

    if url:
        result = f"{body} {url}" if body else url
    else:
        result = body

    # Final safety net for the hard guarantee (ALWAYS <= budget_bytes):
    # only ever bites in the body_budget == 0 absurd-budget floor above,
    # since body + url_suffix is already <= budget_bytes otherwise.
    return _truncate_utf8(result, budget_bytes)
