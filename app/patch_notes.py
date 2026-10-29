"""The /patch-notes page: CHANGELOG.md rendered to HTML.

CHANGELOG.md in the repo root is the single source. The image ships no
markdown library and this adds none: the file only uses a small subset
(# / ## / ### headings, paragraphs, **bold**, `code`, [links](url) and
"-" lists), so this renders exactly that. Everything is HTML-escaped
first, and nothing in the file is ever passed through as raw HTML.

Rendered once per file version (keyed on mtime and size), never per
request.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

CHANGELOG_PATH = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
# Runs on already-escaped text, so the URL is escaped too. Only http(s)
# and site-relative links become anchors; anything else stays plain text.
_LINK = re.compile(r"\[([^\]]+)\]\(((?:https?://|/)[^\s)]*)\)")


def _inline(text: str) -> str:
    out = html.escape(text, quote=True)
    codes: list[str] = []

    def stash(m: re.Match) -> str:
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    out = _CODE.sub(stash, out)
    out = _LINK.sub(
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>'
        if not m.group(2).startswith("//") else m.group(0),
        out,
    )
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], out)


def render_markdown(md: str) -> str:
    parts: list[str] = []
    para: list[str] = []
    items: list[str] = []

    def flush_para() -> None:
        if para:
            parts.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()

    def flush_items() -> None:
        if items:
            parts.append("<ul>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for raw in md.splitlines():
        line = raw.rstrip()
        m = re.match(r"(#{1,3})\s+(.*)", line)
        if m:
            flush_para()
            flush_items()
            level = len(m.group(1))
            parts.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
        elif re.match(r"-\s+", line):
            flush_para()
            items.append(line[1:].strip())
        elif not line.strip():
            flush_para()
            flush_items()
        elif items and raw.startswith((" ", "\t")):
            items[-1] += " " + line.strip()
        else:
            flush_items()
            para.append(line.strip())
    flush_para()
    flush_items()
    return "\n".join(parts)


def _from_first_entry(md: str) -> str:
    """Drop the file's h1 and preamble: start at the first "## " heading
    (the page supplies its own title and subtitle)."""
    m = re.search(r"^## ", md, flags=re.M)
    return md[m.start():] if m else ""


_cache: dict[tuple, str] = {}


def rendered_changelog(path: Path = CHANGELOG_PATH) -> str:
    """HTML for the changelog; re-read only when the file changes."""
    try:
        st = path.stat()
    except OSError:
        return "<p>The patch notes are not available right now.</p>"
    key = (str(path), st.st_mtime_ns, st.st_size)
    hit = _cache.get(key)
    if hit is None:
        hit = render_markdown(_from_first_entry(path.read_text(encoding="utf-8")))
        _cache.clear()
        _cache[key] = hit
    return hit
