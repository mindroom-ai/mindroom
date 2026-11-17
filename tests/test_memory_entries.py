"""Tests for splitting memory files into recall entries."""

from __future__ import annotations

import sys

from mindroom.knowledge.memory_entries import split_memory_entries


def test_splits_daily_note_on_id_entries() -> None:
    """Each `- [id=m_…]` line starts an entry, which runs until the next one."""
    text = (
        "- [id=m_20261109111659_x] Bas prefers functional code.\n"
        "- [id=m_20261109111700_y] The lab service may be restarted.\n"
        "  It runs from /srv/mindroom.\n"
        "- [id=m_20261109111701_z] Use trash, not rm.\n"
    )

    assert split_memory_entries(text, 1_200) == [
        "- [id=m_20261109111659_x] Bas prefers functional code.",
        "- [id=m_20261109111700_y] The lab service may be restarted.\n  It runs from /srv/mindroom.",
        "- [id=m_20261109111701_z] Use trash, not rm.",
    ]


def test_auto_flush_lines_start_entries() -> None:
    """Older `[auto_flush:…]` entries start entries too, with or without a list marker."""
    text = "- [auto_flush:!room:$a] First fact.\n[auto_flush:!room:$b] Second fact.\n"

    assert split_memory_entries(text, 1_200) == [
        "- [auto_flush:!room:$a] First fact.",
        "[auto_flush:!room:$b] Second fact.",
    ]


def test_splits_topic_file_on_headings() -> None:
    """Headings up to `###` start entries; deeper ones stay inside their section."""
    text = "## A\nFirst section.\n\n## B\nSecond section.\n#### not a split\nstill B.\n"

    assert split_memory_entries(text, 1_200) == [
        "## A\nFirst section.",
        "## B\nSecond section.\n#### not a split\nstill B.",
    ]


def test_heading_inside_a_code_fence_does_not_start_an_entry() -> None:
    """A `#` line in a fenced code block is code, such as a shell comment, and stays in its entry."""
    text = (
        "## Deploy\n"
        "Run:\n"
        "```bash\n"
        "# build first\n"
        "just build\n"
        "```\n"
        "## Later\n"
        "~~~~\n"
        "# still code\n"
        "~~~\n"
        "## not a heading either, the fence needs four tildes to close\n"
        "~~~~\n"
        "## After\n"
        "Done.\n"
    )

    assert split_memory_entries(text, 1_200) == [
        "## Deploy\nRun:\n```bash\n# build first\njust build\n```",
        "## Later\n~~~~\n# still code\n~~~\n## not a heading either, the fence needs four tildes to close\n~~~~",
        "## After\nDone.",
    ]


def test_entry_line_closes_an_unclosed_fence() -> None:
    """An id line always starts an entry, so one entry's unclosed fence cannot swallow the rest of a daily note."""
    text = "- [id=m_20261109111659_x] Example:\n  ```\n  code\n- [id=m_20261109111700_y] Next.\n## Heading\nBody.\n"

    assert split_memory_entries(text, 1_200) == [
        "- [id=m_20261109111659_x] Example:\n  ```\n  code",
        "- [id=m_20261109111700_y] Next.",
        "## Heading\nBody.",
    ]


def test_text_before_first_heading_is_its_own_entry() -> None:
    """A preamble before the first heading is kept; a heading with no body is dropped."""
    text = "Intro line.\n# Title\n## A\nBody.\n"

    assert split_memory_entries(text, 1_200) == ["Intro line.", "## A\nBody."]


def test_empty_text_gives_no_entries() -> None:
    """Blank files and blank sections produce nothing."""
    assert split_memory_entries("", 1_200) == []
    assert split_memory_entries("\n\n  \n", 1_200) == []


def test_long_entry_split_at_whitespace() -> None:
    """An entry over the size cap is split at whitespace into pieces; with no cap it stays whole."""
    text = "- [id=m_20261109111659_x] " + "word " * 595

    pieces = split_memory_entries(text, 1_200)

    assert len(text.strip()) > 2_400
    assert len(pieces) == 3
    assert all(len(piece) <= 1_200 for piece in pieces)
    assert all(not piece.startswith(" ") and not piece.endswith(" ") for piece in pieces)
    assert " ".join(pieces).split() == text.split()
    assert split_memory_entries(text, sys.maxsize) == [text.strip()]
    assert all(len(piece) <= 300 for piece in split_memory_entries(text, 300))
