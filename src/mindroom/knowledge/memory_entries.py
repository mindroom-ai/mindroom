"""Split memory Markdown files into entries, the units recall indexes and returns.

Daily notes hold one entry per `- [id=m_YYYYMMDDhhmmss_…]` line; older entries start with `[auto_flush:…]` instead.
Topic files hold one entry per Markdown heading (`#` to `###`), with any text before the first heading as its own entry.
Both kinds of boundary are honoured in every file, so a topic file with id entries splits on those too.
A heading inside a fenced code block is code, such as a shell comment, not a boundary; an entry line still is, and it
closes the fence, so one entry's unclosed fence cannot swallow the rest of a daily note.
An entry longer than the size cap is split at whitespace into pieces.
"""

from __future__ import annotations

import re

from mindroom.knowledge.chunking import SafeFixedSizeChunking
from mindroom.knowledge.readers import Document

_ENTRY_START_RE = re.compile(r"^(?:- )?\[(?:id=m_\d{14}_|auto_flush:)")
_HEADING_RE = re.compile(r"^#{1,3}\s")
# A fence may be indented, as inside a list item; a backtick fence's info string holds no backtick.
_FENCE_RE = re.compile(r"^\s*(?P<fence>`{3,}(?=[^`]*$)|~{3,})(?P<info>.*)$")


def split_memory_entries(text: str, max_chars: int) -> list[str]:
    """Split one memory file's text into stripped entries of at most ``max_chars`` characters, in file order.

    Blank and heading-only entries are dropped.
    """
    entries = [body for section in _sections(text) if _has_content(body := section.strip())]
    return [piece for entry in entries for piece in _pieces(entry, max_chars)]


def _sections(text: str) -> list[str]:
    sections: list[list[str]] = [[]]
    open_fence: str | None = None
    for line in text.splitlines():
        if _ENTRY_START_RE.match(line) or (open_fence is None and _HEADING_RE.match(line)):
            sections.append([])
            open_fence = None
        elif fence := _FENCE_RE.match(line):
            open_fence = _fence_after(open_fence, fence["fence"], fence["info"])
        sections[-1].append(line)
    return ["\n".join(lines) for lines in sections]


def _fence_after(open_fence: str | None, fence: str, info: str) -> str | None:
    """Return the fence open after a fence line: it opens one, or closes the open one with as long a run of its kind."""
    if open_fence is None:
        return fence
    closes = fence[0] == open_fence[0] and len(fence) >= len(open_fence) and not info.strip()
    return None if closes else open_fence


def _has_content(body: str) -> bool:
    """Return whether ``body`` holds more than headings and blank lines."""
    return any(line.strip() and not _HEADING_RE.match(line) for line in body.splitlines())


def _pieces(body: str, max_chars: int) -> list[str]:
    if len(body) <= max_chars:
        return [body]
    chunks = SafeFixedSizeChunking(chunk_size=max_chars).chunk(Document(content=body))
    return [piece for chunk in chunks if (piece := chunk.content.strip())]
