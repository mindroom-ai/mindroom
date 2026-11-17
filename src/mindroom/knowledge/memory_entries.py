"""Split memory Markdown files into dated entries, the units recall indexes and returns.

Daily notes hold one entry per `- [id=m_YYYYMMDDhhmmss_…]` line; older entries start with `[auto_flush:…]` instead.
Topic files hold one entry per Markdown heading (`#` to `###`), with any text before the first heading as its own entry.
Both kinds of boundary are honoured in every file, so a topic file with id entries splits on those too.
An entry is dated by its id; without one, by a `YYYY-MM-DD` prefix of the file name; otherwise its date is unknown.
An entry longer than the size cap is split at whitespace into pieces that keep its date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import PurePath

from mindroom.knowledge.chunking import SafeFixedSizeChunking
from mindroom.knowledge.readers import Document

_ENTRY_START_RE = re.compile(r"^(?:- )?\[(?:id=m_(?P<id_date>\d{8})\d{6}_|auto_flush:)")
_HEADING_RE = re.compile(r"^#{1,3}\s")
_FILE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class _MemoryEntry:
    """One recall unit of a memory file and the day it was written, when known."""

    text: str
    date: date | None


def split_memory_entries(text: str, *, file_name: str, max_chars: int = 1200) -> list[_MemoryEntry]:
    """Split one memory file's text into entries of at most ``max_chars`` characters; blank entries are dropped."""
    file_date = _file_date(file_name)
    entries: list[_MemoryEntry] = []
    for section in _sections(text):
        body = section.strip()
        if not _has_content(body):
            continue
        entry_date = _id_date(body) or file_date
        entries.extend(_MemoryEntry(text=piece, date=entry_date) for piece in _pieces(body, max_chars))
    return entries


def _sections(text: str) -> list[str]:
    sections: list[list[str]] = [[]]
    for line in text.splitlines():
        if _ENTRY_START_RE.match(line) or _HEADING_RE.match(line):
            sections.append([])
        sections[-1].append(line)
    return ["\n".join(lines) for lines in sections]


def _has_content(body: str) -> bool:
    """Return whether ``body`` holds more than headings and blank lines."""
    return any(line.strip() and not _HEADING_RE.match(line) for line in body.splitlines())


def _id_date(body: str) -> date | None:
    match = _ENTRY_START_RE.match(body)
    if match is None or match["id_date"] is None:
        return None
    return _parse_date(match["id_date"], "%Y%m%d")


def _file_date(file_name: str) -> date | None:
    match = _FILE_DATE_RE.match(PurePath(file_name).name)
    return None if match is None else _parse_date(match[0], "%Y-%m-%d")


def _parse_date(value: str, date_format: str) -> date | None:
    try:
        return datetime.strptime(value, date_format).date()  # noqa: DTZ007 - a calendar day, not an instant
    except ValueError:
        return None


def _pieces(body: str, max_chars: int) -> list[str]:
    if len(body) <= max_chars:
        return [body]
    chunks = SafeFixedSizeChunking(chunk_size=max_chars).chunk(Document(content=body))
    return [piece for chunk in chunks if (piece := chunk.content.strip())]
