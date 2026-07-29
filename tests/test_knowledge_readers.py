"""Which reader answers for a file, and what chunking it carries.

Chunk boundaries decide what a knowledge base's vector store contains, and
almost nothing downstream asserts them: a reader handed the wrong chunking
still indexes, still publishes, and still answers queries, only against
different chunks. These tests pin the two decisions that set them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agno.knowledge.chunking.fixed import FixedSizeChunking
from agno.knowledge.reader.markdown_reader import MarkdownReader
from agno.knowledge.reader.text_reader import TextReader

from mindroom.chunking import SafeFixedSizeChunking
from mindroom.knowledge.readers import (
    build_reader,
    reader_decodes_plain_text,
    text_fallback_reader,
)

#: Suffixes whose reader MindRoom reconfigures with the base's chunking.
_CHUNKED_SUFFIXES = (".md", ".markdown", ".txt", ".py", ".yaml", ".html", ".xyz", "")
#: Suffixes whose reader keeps whatever chunking the Agno factory gave it.
_UNTOUCHED_SUFFIXES = (".csv", ".xlsx", ".docx")


@pytest.fixture
def chunking() -> SafeFixedSizeChunking:
    """Return a chunking policy no factory default could be mistaken for."""
    return SafeFixedSizeChunking(chunk_size=137, overlap=11)


@pytest.mark.parametrize("suffix", _CHUNKED_SUFFIXES)
def test_text_readers_carry_this_bases_chunking(suffix: str, chunking: SafeFixedSizeChunking) -> None:
    """A text-like reader must chunk by the base's policy, not the factory's 5000/0."""
    reader = build_reader(Path(f"source{suffix}"), chunking=chunking)

    assert isinstance(reader, (TextReader, MarkdownReader))
    assert reader.chunking_strategy is chunking
    assert reader.chunk_size == 137
    assert reader.chunk is True


@pytest.mark.parametrize("suffix", _UNTOUCHED_SUFFIXES)
def test_non_text_readers_keep_their_factory_chunking(suffix: str, chunking: SafeFixedSizeChunking) -> None:
    """Row and document readers own their splitting; MindRoom must not overwrite it."""
    reader = build_reader(Path(f"source{suffix}"), chunking=chunking)

    assert reader.chunking_strategy is not chunking
    assert not isinstance(reader.chunking_strategy, SafeFixedSizeChunking)


def test_json_keeps_structured_chunking_under_the_fallback_aware_reader(
    chunking: SafeFixedSizeChunking,
) -> None:
    """The decode-tagging JSON subclass must not inherit text chunking.

    Its whole purpose is to read JSON as JSON and tag a parse failure; taking
    MindRoom's text chunking here would split structured documents by size.
    """
    reader = build_reader(Path("source.json"), chunking=chunking)

    assert type(reader.chunking_strategy) is FixedSizeChunking
    assert reader.chunking_strategy is not chunking


def test_malformed_json_fallback_reader_carries_this_bases_chunking(chunking: SafeFixedSizeChunking) -> None:
    """Text served from a failed parse is chunked like any other text of this base."""
    reader = text_fallback_reader("retained source text", chunking=chunking)

    assert reader.chunking_strategy is chunking
    assert reader.chunk_size == 137


@pytest.mark.parametrize("suffix", _CHUNKED_SUFFIXES)
def test_text_sources_are_cheap_to_reread(suffix: str, chunking: SafeFixedSizeChunking) -> None:
    """Decoded-as-text files may be re-read: the embedding prefetch depends on it."""
    assert reader_decodes_plain_text(build_reader(Path(f"source{suffix}"), chunking=chunking))


@pytest.mark.parametrize("suffix", [*_UNTOUCHED_SUFFIXES, ".json", ".pdf"])
def test_parsed_and_packed_sources_are_not_cheap_to_reread(suffix: str, chunking: SafeFixedSizeChunking) -> None:
    """Re-reading these costs a parse or an extraction, and size on disk bounds neither.

    Answering ``True`` here would make the embedding prefetch parse every such
    file a second time per refresh, and would apply a byte budget derived from
    a size on disk to text that an archive can hold many times over.
    """
    assert not reader_decodes_plain_text(build_reader(Path(f"source{suffix}"), chunking=chunking))
