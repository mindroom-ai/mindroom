"""Split memory Markdown files into entries, the units recall indexes and returns.

Daily notes hold one entry per `- [id=m_YYYYMMDDhhmmss_…]` line; older entries start with `[auto_flush:…]` instead.
Topic files hold one entry per Markdown heading (`#` to `###`), with any text before the first heading as its own entry.
Both kinds of boundary are honoured in every file, so a topic file with id entries splits on those too.
A heading inside a fenced code block is code, such as a shell comment, not a boundary; an entry line still is, and it
closes the fence, so one entry's unclosed fence cannot swallow the rest of a daily note.
An entry longer than the size cap is split into balanced pieces along natural boundaries so no tiny orphan tail forms.
"""

from __future__ import annotations

import math
import re

MEMORY_ENTRIES_STRATEGY_ID = "memory_entries:v2"

_ENTRY_START_RE = re.compile(r"^(?:- )?\[(?:id=m_\d{14}_|auto_flush:)")
_HEADING_RE = re.compile(r"^#{1,3}\s")
# A fence may be indented, as inside a list item; a backtick fence's info string holds no backtick.
_FENCE_RE = re.compile(r"^\s*(?P<fence>`{3,}(?=[^`]*$)|~{3,})(?P<info>.*)$")
_WHITESPACE_RE = re.compile(r"[ \t\r\n]+")
# Priority-ordered split boundaries: (pattern, characters kept on the left piece).
_BOUNDARIES: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r" \| "), 0),
    (re.compile(r"\n\n+"), 0),
    (re.compile(r"\n+"), 0),
    (re.compile(r"[.!?] +"), 1),
    (_WHITESPACE_RE, 0),
)


def split_memory_entries(text: str, max_chars: int) -> list[str]:
    """Split one memory file's text into stripped entries of at most ``max_chars`` characters, in file order.

    Blank and heading-only entries are dropped.
    """
    if max_chars <= 0:
        msg = "max_chars must be positive"
        raise ValueError(msg)
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
    remaining = body.strip()
    if not remaining or len(remaining) <= max_chars:
        return [remaining] if remaining else []
    pieces: list[str] = []
    while len(remaining) > max_chars:
        left, remaining = _split_once(remaining, max_chars)
        pieces.append(left)
    return [*pieces, remaining] if remaining else pieces


def _split_once(remaining: str, max_chars: int) -> tuple[str, str]:
    length = len(remaining)
    parts_left = math.ceil(length / max_chars)
    target = math.ceil(length / parts_left)
    max_right = (parts_left - 1) * max_chars
    min_side = max(1, target // 2)
    for pattern, keep_left in _BOUNDARIES:
        if split := _best_boundary(remaining, pattern, keep_left, target, max_chars, max_right, min_side):
            return split
    if min_side > 1 and (split := _best_boundary(remaining, _WHITESPACE_RE, 0, target, max_chars, max_right, 1)):
        return split
    return remaining[:target].strip(), remaining[target:].strip()


def _best_boundary(
    remaining: str,
    pattern: re.Pattern[str],
    keep_left: int,
    target: int,
    max_chars: int,
    max_right: int,
    min_side: int,
) -> tuple[str, str] | None:
    scan_end = max_chars
    while scan_end < len(remaining) and remaining[scan_end].isspace():
        scan_end += 1
    best: tuple[int, int, str, str] | None = None
    for match in pattern.finditer(remaining, max(0, min_side - keep_left)):
        left_end = match.start() + keep_left
        if left_end > scan_end:
            break
        right_start = match.end()
        if len(remaining) - right_start < min_side:
            continue
        left = remaining[:left_end].strip()
        right = remaining[right_start:].strip()
        if not (min_side <= len(left) <= max_chars and min_side <= len(right) <= max_right):
            continue
        distance = abs(len(left) - target)
        if best is None or (distance, left_end) < (best[0], best[1]):
            best = (distance, left_end, left, right)
    return None if best is None else (best[2], best[3])
