"""Which reader answers for a file, and where it puts the chunk boundaries.

Chunk boundaries decide what a knowledge base's vector store contains, and
almost nothing downstream asserts them: a reader carrying the wrong chunking
still indexes, still publishes, and still answers queries, only against
different vectors. So these tests start from a base's authored config rather
than from a hand-built strategy, assert every reader field that moves a
boundary, and read real text to observe the boundaries themselves instead of
inferring them from attributes.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agno.knowledge.chunking.fixed import FixedSizeChunking
from agno.knowledge.reader.markdown_reader import MarkdownReader
from agno.knowledge.reader.text_reader import TextReader

from mindroom.chunking import SafeFixedSizeChunking
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.knowledge.readers import (
    MalformedJSONSourceError,
    build_reader,
    chunking_strategy_for_base,
    reader_rereads_within_file_size,
    text_fallback_reader,
)

if TYPE_CHECKING:
    from agno.knowledge.reader.base import Reader

#: Chunk settings no factory default could be mistaken for (Agno ships 5000/0).
_CHUNK_SIZE = 137
_CHUNK_OVERLAP = 11

#: Suffixes whose reader MindRoom reconfigures with the base's chunking.
_CHUNKED_SUFFIXES = (".md", ".markdown", ".txt", ".py", ".yaml", ".html", ".xyz", "")
#: Suffixes whose reader owns its own splitting and must be left alone.
_UNTOUCHED_SUFFIXES = (".csv", ".xlsx", ".docx")


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Return a config whose knowledge base authors distinctive chunk settings."""
    return Config(
        knowledge_bases={
            "docs": KnowledgeBaseConfig(
                path=str(tmp_path / "docs"),
                chunk_size=_CHUNK_SIZE,
                chunk_overlap=_CHUNK_OVERLAP,
            ),
        },
    )


@pytest.fixture
def chunking(config: Config) -> SafeFixedSizeChunking:
    """Return the strategy the base's own config produces, not a hand-built one."""
    return chunking_strategy_for_base(config, "docs")


def _distinct_text(length: int) -> str:
    """Return whitespace-free text whose every position is identifiable.

    No whitespace means chunk boundaries land exactly on the size, so the
    emitted chunks pin ``chunk_size``, and the repeating alphabet makes the
    characters one chunk shares with the next pin ``overlap``.
    """
    return "".join(chr(ord("a") + index % 26) for index in range(length))


def _assert_carries_base_chunking(reader: Reader) -> None:
    """Assert every field of ``reader`` that can move a chunk boundary."""
    strategy = reader.chunking_strategy
    assert type(strategy) is SafeFixedSizeChunking
    assert strategy.chunk_size == _CHUNK_SIZE
    assert strategy.overlap == _CHUNK_OVERLAP
    assert strategy.min_chunk_fill_ratio == 0.5
    assert reader.chunk is True
    assert reader.chunk_size == _CHUNK_SIZE
    # None is how these readers spell UTF-8 (``self.encoding or "utf-8"``), and
    # the prefetch byte budget stays valid only while the decode is UTF-8.
    assert reader.encoding is None


def test_chunking_strategy_for_base_reads_the_authored_config(config: Config) -> None:
    """The strategy must come from this base's config, not from a default."""
    strategy = chunking_strategy_for_base(config, "docs")

    assert type(strategy) is SafeFixedSizeChunking
    assert strategy.chunk_size == _CHUNK_SIZE
    assert strategy.overlap == _CHUNK_OVERLAP


@pytest.mark.parametrize("suffix", _CHUNKED_SUFFIXES)
def test_text_readers_carry_this_bases_chunking(suffix: str, chunking: SafeFixedSizeChunking) -> None:
    """A text-like reader must chunk by the base's policy, not the factory's 5000/0."""
    reader = build_reader(Path(f"source{suffix}"), chunking=chunking)

    assert isinstance(reader, (TextReader, MarkdownReader))
    _assert_carries_base_chunking(reader)


def test_text_reader_splits_a_file_on_the_bases_boundaries(
    tmp_path: Path,
    chunking: SafeFixedSizeChunking,
) -> None:
    """Observe the boundaries a real read produces rather than trusting attributes.

    Both settings are visible in the output: chunk length is capped at
    ``chunk_size``, and each chunk after the first opens with the ``overlap``
    characters that closed its predecessor.
    """
    source = tmp_path / "notes.txt"
    source.write_text(_distinct_text(400), encoding="utf-8")
    reader = build_reader(source, chunking=chunking)

    chunks = [document.content for document in reader.read(source, name="notes")]

    assert [len(chunk) for chunk in chunks] == [137, 137, 137, 22]
    for earlier, later in pairwise(chunks):
        assert later[:_CHUNK_OVERLAP] == earlier[-_CHUNK_OVERLAP:]


def test_a_factory_reader_with_chunking_disabled_is_turned_back_on(
    monkeypatch: pytest.MonkeyPatch,
    chunking: SafeFixedSizeChunking,
) -> None:
    """Chunking must be forced on, not inherited from whatever the factory hands over.

    Every reader Agno currently ships defaults to chunking enabled, so this
    guards an assumption rather than today's behavior: a reader that arrived
    with it off would embed each file as one oversized vector, the largest
    boundary change there is.
    """
    monkeypatch.setattr(
        "mindroom.knowledge.readers.ReaderFactory.get_reader_for_extension",
        lambda _extension: TextReader(chunk=False),
    )

    reader = build_reader(Path("notes.txt"), chunking=chunking)

    assert reader.chunk is True


@pytest.mark.parametrize("suffix", _UNTOUCHED_SUFFIXES)
def test_non_text_readers_keep_their_factory_chunking(suffix: str, chunking: SafeFixedSizeChunking) -> None:
    """Row and document readers own their splitting; MindRoom must not overwrite it."""
    reader = build_reader(Path(f"source{suffix}"), chunking=chunking)

    assert reader.chunking_strategy is not chunking
    assert not isinstance(reader.chunking_strategy, SafeFixedSizeChunking)


def test_json_keeps_structured_chunking_and_tags_its_parse_failures(
    tmp_path: Path,
    chunking: SafeFixedSizeChunking,
) -> None:
    """JSON must be read as JSON, and a parse failure must carry its source text.

    Taking MindRoom's text chunking here would split structured documents by
    size, and losing the tagging drops the malformed-source fallback entirely.
    """
    reader = build_reader(Path("source.json"), chunking=chunking)

    assert type(reader.chunking_strategy) is FixedSizeChunking
    assert reader.chunking_strategy.chunk_size == 5000
    assert reader.chunking_strategy is not chunking

    malformed = tmp_path / "claim.json"
    malformed.write_text('{\n  "claim": "kept",\n  “broken”: true\n}\n', encoding="utf-8")
    with pytest.raises(MalformedJSONSourceError) as raised:
        reader.read(malformed)
    assert raised.value.source_text.startswith('{\n  "claim": "kept"')
    assert (raised.value.line, raised.value.column) == (3, 3)


def test_malformed_json_fallback_reader_chunks_like_this_bases_text(chunking: SafeFixedSizeChunking) -> None:
    """Text served from a failed parse is chunked like any other text of this base.

    Serving it unchunked would turn one long malformed file into a single
    oversized vector, which is the failure the fallback exists to avoid.
    """
    reader = text_fallback_reader(_distinct_text(400), chunking=chunking)
    _assert_carries_base_chunking(reader)

    chunks = [document.content for document in reader.read(Path("claim.json"), name="claim")]

    assert [len(chunk) for chunk in chunks] == [137, 137, 137, 22]


@pytest.mark.parametrize("suffix", _CHUNKED_SUFFIXES)
def test_text_sources_are_rereadable_within_their_file_size(suffix: str, chunking: SafeFixedSizeChunking) -> None:
    """Decoded-as-text files may be re-read: the embedding prefetch depends on it."""
    assert reader_rereads_within_file_size(build_reader(Path(f"source{suffix}"), chunking=chunking))


@pytest.mark.parametrize("suffix", [*_UNTOUCHED_SUFFIXES, ".json", ".pdf"])
def test_parsed_and_packed_sources_are_not_rereadable(suffix: str, chunking: SafeFixedSizeChunking) -> None:
    """Re-reading these costs a parse or an extraction, and size on disk bounds neither.

    Admitting them would make the embedding prefetch parse every such file a
    second time per refresh, and would apply a byte budget derived from a size
    on disk to text an archive can hold many times over.
    """
    assert not reader_rereads_within_file_size(build_reader(Path(f"source{suffix}"), chunking=chunking))


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32", "latin-1", "not-a-codec"])
def test_text_readers_that_expand_when_decoded_are_not_rereadable(encoding: str) -> None:
    """A reader whose decode outgrows its file invalidates the prefetch byte budget.

    ``max_chunk_text_bytes`` is documented against a size on disk, so it only
    budgets a decode that cannot expand. UTF-16 CJK is two bytes on disk and
    three in UTF-8; a Latin-1 accent is one and two. Being cheap to re-read is
    not sufficient on its own, which is why this is checked rather than
    inferred from the reader's type.
    """
    assert not reader_rereads_within_file_size(TextReader(encoding=encoding))
