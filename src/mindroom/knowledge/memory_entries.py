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
_FACT_SEPARATOR = " | "
_WHITESPACE_RE = re.compile(r"[ \t\r\n]+")
# Priority-ordered split boundaries: (pattern, characters kept on the left piece).
_BOUNDARIES: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(re.escape(_FACT_SEPARATOR)), 0),
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
    text = body.strip()
    length = len(text)
    if not length or length <= max_chars:
        return [text] if text else []
    pieces: list[str] = []
    start = 0
    while length - start > max_chars:
        left_end, next_start = _split_once(text, start, max_chars)
        pieces.append(text[start:left_end])
        start = next_start
    if start < length:
        pieces.append(text[start:])
    return pieces


def _split_once(text: str, start: int, max_chars: int) -> tuple[int, int]:
    length = len(text)
    remaining_len = length - start
    parts_left = math.ceil(remaining_len / max_chars)
    target = math.ceil(remaining_len / parts_left)
    max_right = (parts_left - 1) * max_chars
    min_side = max(1, target // 2)
    for pattern, keep_left in _BOUNDARIES:
        if split := _best_boundary(text, start, pattern, keep_left, target, max_chars, max_right, min_side):
            return split
    if split := _best_boundary(text, start, _WHITESPACE_RE, 0, target, max_chars, remaining_len - 1, min_side):
        return split
    hard_end = start + target
    left_end = hard_end
    while left_end > start and text[left_end - 1].isspace():
        left_end -= 1
    next_start = hard_end
    while next_start < length and text[next_start].isspace():
        next_start += 1
    return left_end, next_start


def _best_boundary(
    text: str,
    start: int,
    pattern: re.Pattern[str],
    keep_left: int,
    target: int,
    max_chars: int,
    max_right: int,
    min_side: int,
) -> tuple[int, int] | None:
    length = len(text)
    scan_end = start + max_chars
    while scan_end < length and text[scan_end].isspace():
        scan_end += 1
    search_start = max(start, start + min_side - keep_left)
    # Allow _FACT_SEPARATOR whose leading space sits before scan_end while its trailing "| " extends past it.
    search_end = min(length, scan_end + len(_FACT_SEPARATOR) - 1)
    best: tuple[int, int, int, int] | None = None
    for match in pattern.finditer(text, search_start, search_end):
        left_end = match.start() + keep_left
        if left_end > scan_end:
            break
        right_start = match.end()
        if length - right_start < min_side:
            continue
        trimmed_left_end = left_end
        while trimmed_left_end > start and text[trimmed_left_end - 1].isspace():
            trimmed_left_end -= 1
        left_len = trimmed_left_end - start
        if not (min_side <= left_len <= max_chars):
            continue
        trimmed_right_start = right_start
        while trimmed_right_start < length and text[trimmed_right_start].isspace():
            trimmed_right_start += 1
        right_len = length - trimmed_right_start
        if not (min_side <= right_len <= max_right):
            continue
        distance = abs(left_len - target)
        if best is None or (distance, left_end) < (best[0], best[1]):
            best = (distance, left_end, trimmed_left_end, trimmed_right_start)
    return None if best is None else (best[2], best[3])
