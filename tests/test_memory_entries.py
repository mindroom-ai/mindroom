"""Tests for splitting memory files into recall entries."""

from __future__ import annotations

import sys

import pytest

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


def test_auto_flush_entry_splits_into_balanced_fact_chunks_without_orphan_tail() -> None:
    """An over-cap auto-flush entry splits on ` | ` into balanced chunks instead of leaving an orphan tail."""
    prefix = "- [id=m_20261126090000_abcd1234] "
    facts = [f"Fact {index:02d} records a concrete deployment note." for index in range(26)]
    facts.append("approved.")
    entry = prefix + " | ".join(facts)
    # Trim the first fact so the entry sits at 1,210 chars where greedy chunking leaves "approved.".
    overflow = len(entry) - 1_210
    assert overflow > 0
    facts[0] = facts[0][:-overflow].rstrip() + "."
    entry = prefix + " | ".join(facts)
    assert 1_205 <= len(entry) <= 1_215

    pieces = split_memory_entries(entry, 1_200)

    assert len(pieces) == 2
    assert all(400 <= len(piece) <= 1_200 for piece in pieces)
    assert not pieces[0].endswith("|")
    assert not pieces[1].startswith("|")
    assert pieces[1].startswith("Fact ")
    assert pieces[1].endswith(" | approved.")
    assert " | ".join(pieces) == entry


def test_balanced_split_prefers_paragraph_then_newline_then_sentence() -> None:
    """Natural boundaries are preferred in order: paragraph, line break, sentence end."""
    paragraph_entry = "## Topic\n" + "\n\n".join(f"Paragraph {i} " + "text " * 40 for i in range(7))
    paragraph_pieces = split_memory_entries(paragraph_entry, 1_200)
    assert len(paragraph_pieces) == 2
    assert all(400 <= len(piece) <= 1_200 for piece in paragraph_pieces)
    assert paragraph_pieces[1].startswith("Paragraph ")

    line_entry = "## Checklist\n" + "\n".join(f"- Item {i:02d}: " + "detail " * 8 for i in range(20))
    line_pieces = split_memory_entries(line_entry, 1_200)
    assert len(line_pieces) == 2
    assert all(400 <= len(piece) <= 1_200 for piece in line_pieces)
    assert line_pieces[1].startswith("- Item ")

    sentence_entry = "- [id=m_20261126090000_s] " + " ".join(
        f"Sentence {i:02d} explains one clear choice{punct}"
        for i, punct in ((idx, ".?!"[idx % 3]) for idx in range(35))
    )
    assert len(sentence_entry) > 1_200
    sentence_pieces = split_memory_entries(sentence_entry, 1_200)
    assert len(sentence_pieces) == 2
    assert all(400 <= len(piece) <= 1_200 for piece in sentence_pieces)
    assert sentence_pieces[0][-1] in ".?!"
    assert sentence_pieces[1].startswith("Sentence ")
    assert " ".join(sentence_pieces) == sentence_entry


def test_unbreakable_token_and_edge_cases() -> None:
    """Unbreakable tokens split cleanly without stalling; edge separators do not make tiny shards."""
    solid = "a" * 2_500
    solid_pieces = split_memory_entries(solid, 1_200)
    assert [len(piece) for piece in solid_pieces] == [834, 833, 833]
    assert "".join(solid_pieces) == solid

    near_edges = "- [id=m_20261126090000_e] A | " + "word " * 240 + "| approved."
    edge_pieces = split_memory_entries(near_edges, 1_200)
    assert len(edge_pieces) == 2
    assert all(400 <= len(piece) <= 1_200 for piece in edge_pieces)

    long_url = "prefix " + "u" * 950 + " " + "tail " * 51
    url_pieces = split_memory_entries(long_url, 1_200)
    assert len(url_pieces) == 2
    assert "u" * 950 in url_pieces[0]
    assert all(len(piece) <= 1_200 for piece in url_pieces)

    with pytest.raises(ValueError, match="positive"):
        split_memory_entries("hello", 0)


def test_word_straddling_tight_cap_is_not_sliced() -> None:
    """When both spaces around a word exceed the tight right-side cap, whitespace fallback preserves the whole word."""
    entry = "note " * 239 + "Magnesium" + " done" * 239
    assert len(entry) == 2_399

    pieces = split_memory_entries(entry, 1_200)

    assert len(pieces) == 3
    assert any("Magnesium" in piece for piece in pieces)
    assert all(300 <= len(piece) <= 1_200 for piece in pieces)
    assert " ".join(pieces) == entry


def test_large_entry_splits_in_linear_time() -> None:
    """Splitting a very large entry avoids quadratic suffix copying and stays well-bounded."""
    entry = "- [id=m_20261126090000_big] " + "word " * 120_000
    pieces = split_memory_entries(entry, 1_200)

    assert len(pieces) == 501
    assert all(300 <= len(piece) <= 1_200 for piece in pieces)
    assert " ".join(pieces).split() == entry.split()
