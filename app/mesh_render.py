"""Render a Content dict (see app/announce_content.py's Content contract)
into ONE plain-text line that fits a hard per-protocol UTF-8 byte
budget, for transmission as a single LoRa packet.

WHY always one string, never a list of fragments: this module feeds a
game bot's radio transmit path, and a game bot has no business
consuming shared airtime with multi-packet traffic for what is, at
most, a "team X moved up a rank" nicety. When a Content does not fit
the budget, detail is dropped -- lowest priority first, whole rows at
a time -- until what remains fits in a single packet. Nothing here
ever asks a caller to send more than one packet for one announcement.
"""
from __future__ import annotations

# Per-protocol single-packet payload budgets, taken from the sibling
# MeshWars radio-transport project's own protocol-aware chunker.
# MeshCore is the tighter of the two and is the one that matters here:
# a line that fits MESHCORE_BUDGET_BYTES fits MESHTASTIC_BUDGET_BYTES
# for free, so MeshCore is this module's default.
MESHCORE_BUDGET_BYTES = 150
MESHTASTIC_BUDGET_BYTES = 237


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Byte-safe truncation: cut `text` to at most `max_bytes` UTF-8
    bytes without ever splitting a multi-byte codepoint. Content is
    ASCII by construction today (app/announce_content.py's own HARD
    RULE), but this budgets in bytes and slices safely regardless, so a
    future non-ASCII name can never emit a broken byte sequence.
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


def _row_texts(content: dict) -> list[str]:
    """Every row's pre-rendered `text`, across all sections, in the
    order the builder wrote them. Section `heading` strings (e.g.
    "Placement", "Honors") are never emitted here -- per this module's
    own degradation ladder, they exist for JSON/API consumers and
    richer destinations, not for a packet with no room left for a
    label.
    """
    texts = []
    for section in content.get("sections") or []:
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


def render_mesh(content: dict, budget_bytes: int = MESHCORE_BUDGET_BYTES) -> str:
    """Render `content` into one line that fits `budget_bytes` of UTF-8,
    always. Deterministic: the same content and budget always produce
    byte-identical output, because the public API will serve this
    string from a cache while a bot renders the same Content locally --
    any drift between the two would be a bug, not a style choice.

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
