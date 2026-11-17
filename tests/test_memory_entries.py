"""Tests for splitting memory files into dated recall entries."""

from __future__ import annotations

from datetime import date

from mindroom.knowledge.memory_entries import _MemoryEntry, split_memory_entries


def test_splits_daily_note_on_id_entries() -> None:
    """Each `- [id=m_…]` line starts an entry dated by its id."""
    text = (
        "- [id=m_20261109111659_x] Bas prefers functional code.\n"
        "- [id=m_20261109111700_y] The lab service may be restarted.\n"
        "  It runs from /srv/mindroom.\n"
        "- [id=m_20261109111701_z] Use trash, not rm.\n"
    )

    entries = split_memory_entries(text, file_name="2026-11-10.md")

    assert entries == [
        _MemoryEntry(text="- [id=m_20261109111659_x] Bas prefers functional code.", date=date(2026, 11, 9)),
        _MemoryEntry(
            text="- [id=m_20261109111700_y] The lab service may be restarted.\n  It runs from /srv/mindroom.",
            date=date(2026, 11, 9),
        ),
        _MemoryEntry(text="- [id=m_20261109111701_z] Use trash, not rm.", date=date(2026, 11, 9)),
    ]


def test_auto_flush_entries_without_id_take_the_file_date() -> None:
    """Older `[auto_flush:…]` entries start entries too and are dated by the file name."""
    text = "- [auto_flush:!room:$a] First fact.\n[auto_flush:!room:$b] Second fact.\n"

    assert split_memory_entries(text, file_name="memory/2026-03-26.md") == [
        _MemoryEntry(text="- [auto_flush:!room:$a] First fact.", date=date(2026, 3, 26)),
        _MemoryEntry(text="[auto_flush:!room:$b] Second fact.", date=date(2026, 3, 26)),
    ]
    assert [entry.date for entry in split_memory_entries(text, file_name="notes.md")] == [None, None]


def test_splits_topic_file_on_headings() -> None:
    """Headings up to `###` start entries; the date comes from the file name or is unknown."""
    text = "## A\nFirst section.\n\n## B\nSecond section.\n#### not a split\nstill B.\n"

    dated = split_memory_entries(text, file_name="2026-02-19.md")
    undated = split_memory_entries(text, file_name="lessons.md")

    assert dated == [
        _MemoryEntry(text="## A\nFirst section.", date=date(2026, 2, 19)),
        _MemoryEntry(text="## B\nSecond section.\n#### not a split\nstill B.", date=date(2026, 2, 19)),
    ]
    assert [entry.text for entry in undated] == [entry.text for entry in dated]
    assert [entry.date for entry in undated] == [None, None]


def test_text_before_first_heading_is_its_own_entry() -> None:
    """A preamble before the first heading is kept; a heading with no body is dropped."""
    text = "Intro line.\n# Title\n## A\nBody.\n"

    assert [entry.text for entry in split_memory_entries(text, file_name="topic.md")] == ["Intro line.", "## A\nBody."]


def test_empty_text_gives_no_entries() -> None:
    """Blank files and blank sections produce nothing."""
    assert split_memory_entries("", file_name="topic.md") == []
    assert split_memory_entries("\n\n  \n", file_name="topic.md") == []


def test_invalid_id_date_falls_back_to_file_date() -> None:
    """An id whose digits are not a calendar date is dated like an entry without an id."""
    entries = split_memory_entries("- [id=m_20261399111659_x] Odd id.\n", file_name="2026-11-10.md")

    assert entries == [_MemoryEntry(text="- [id=m_20261399111659_x] Odd id.", date=date(2026, 11, 10))]


def test_long_entry_split_at_whitespace() -> None:
    """An entry over the size cap is split at whitespace into pieces that keep the parent's date."""
    text = "- [id=m_20261109111659_x] " + "word " * 595

    entries = split_memory_entries(text, file_name="topic.md")

    assert len(text.strip()) > 2_400
    assert len(entries) == 3
    assert all(len(entry.text) <= 1_200 for entry in entries)
    assert all(entry.date == date(2026, 11, 9) for entry in entries)
    assert all(not entry.text.startswith(" ") and not entry.text.endswith(" ") for entry in entries)
    assert " ".join(entry.text for entry in entries).split() == text.split()
