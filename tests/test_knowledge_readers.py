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

import codecs
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agno.knowledge.chunking.fixed import FixedSizeChunking
from agno.knowledge.reader import ReaderFactory
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

#: A CJK sample: three UTF-8 bytes per character but only two in UTF-16, so a
#: UTF-16 source is smaller on disk than the text it decodes to.
_CJK_TEXT = "写" * 100

#: Suffixes whose reader MindRoom reconfigures with the base's chunking.
_CHUNKED_SUFFIXES = (".md", ".markdown", ".txt", ".py", ".yaml", ".html", ".xyz", "")
#: Suffixes whose reader owns its own splitting and must be left alone.
_UNTOUCHED_SUFFIXES = (".csv", ".xlsx", ".docx")


def _config_for(tmp_path: Path, *, chunk_size: int, chunk_overlap: int) -> Config:
    """Return a config whose single knowledge base authors these chunk settings."""
    return Config(
        knowledge_bases={
            "docs": KnowledgeBaseConfig(
                path=str(tmp_path / "docs"),
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            ),
        },
    )


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Return a config whose knowledge base authors distinctive chunk settings."""
    return _config_for(tmp_path, chunk_size=_CHUNK_SIZE, chunk_overlap=_CHUNK_OVERLAP)


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


def _emitted_utf8_bytes(reader: Reader, source: Path) -> int:
    """Return the UTF-8 bytes one read of ``source`` puts in memory."""
    documents = reader.read(source, name=source.stem)
    assert len(documents) == 1, "keep the sample within one chunk so overlap cannot inflate the count"
    return len(documents[0].content.encode("utf-8"))


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


@pytest.mark.parametrize(("chunk_size", "chunk_overlap"), [(_CHUNK_SIZE, _CHUNK_OVERLAP), (512, 64)])
def test_chunking_strategy_for_base_reads_the_authored_config(
    tmp_path: Path,
    chunk_size: int,
    chunk_overlap: int,
) -> None:
    """The strategy must be read out of this base's config, not fixed in the code.

    Two different bases are checked because one would not distinguish reading
    the config from returning a constant that happens to match it.
    """
    strategy = chunking_strategy_for_base(
        _config_for(tmp_path, chunk_size=chunk_size, chunk_overlap=chunk_overlap),
        "docs",
    )

    assert type(strategy) is SafeFixedSizeChunking
    assert (strategy.chunk_size, strategy.overlap) == (chunk_size, chunk_overlap)


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


def test_a_second_base_moves_the_reader_and_its_boundaries(tmp_path: Path) -> None:
    """A different authored config must reach the reader and change where it splits.

    Every other case here uses one chunk size, which cannot tell a value read
    from config apart from a constant that happens to equal it.
    """
    chunking = chunking_strategy_for_base(_config_for(tmp_path, chunk_size=512, chunk_overlap=64), "docs")
    source = tmp_path / "notes.txt"
    source.write_text(_distinct_text(1200), encoding="utf-8")
    reader = build_reader(source, chunking=chunking)

    assert reader.chunk_size == 512
    assert reader.chunking_strategy is chunking

    chunks = [document.content for document in reader.read(source, name="notes")]

    assert [len(chunk) for chunk in chunks] == [512, 512, 304]
    for earlier, later in pairwise(chunks):
        assert later[:64] == earlier[-64:]


def test_two_bases_neither_share_a_reader_nor_poison_the_factory_cache(tmp_path: Path) -> None:
    """Configuring a reader must copy it first, because the factory's instance is shared.

    ``ReaderFactory`` hands out one cached reader per extension. Configuring
    that instance in place would give two bases the same object: whichever
    refreshed second would silently re-chunk the other's corpus at its own
    size, and the cache would keep serving that size for the rest of the
    process. The two refreshes are not even ordered -- the source-root lock is
    per root, so bases on different roots overlap, and prefetch reads run on
    worker threads.
    """
    cached = ReaderFactory.get_reader_for_extension(".md")
    cached_strategy = cached.chunking_strategy
    cached_chunk_size = cached.chunk_size

    small = build_reader(
        Path("a.md"),
        chunking=chunking_strategy_for_base(_config_for(tmp_path, chunk_size=137, chunk_overlap=11), "docs"),
    )
    large = build_reader(
        Path("b.md"),
        chunking=chunking_strategy_for_base(_config_for(tmp_path, chunk_size=2000, chunk_overlap=0), "docs"),
    )

    assert small is not large
    assert (small.chunk_size, large.chunk_size) == (137, 2000)
    assert small.chunking_strategy is not large.chunking_strategy
    # The shared cache entry must be exactly as it was found.
    assert cached is not small
    assert cached is not large
    assert cached.chunking_strategy is cached_strategy
    assert cached.chunk_size == cached_chunk_size


def test_the_json_reader_does_not_borrow_the_cached_chunking_strategy(chunking: SafeFixedSizeChunking) -> None:
    """The JSON subclass must copy the factory's chunker, not alias it.

    Nothing mutates a JSON reader's strategy today, so this pins the same
    copy-before-use rule one branch over rather than a present defect: an
    aliased strategy would put the shared cache one in-place edit away from
    every base that reads JSON.
    """
    cached = ReaderFactory.get_reader_for_extension(".json")

    reader = build_reader(Path("source.json"), chunking=chunking)

    assert reader is not cached
    assert reader.chunking_strategy is not cached.chunking_strategy


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

    # The broken key is indented so line and column differ; equal values would
    # make the assertion below blind to the two being swapped.
    malformed = tmp_path / "claim.json"
    malformed.write_text('{\n  "claim": "kept",\n     “broken”: true\n}\n', encoding="utf-8")
    with pytest.raises(MalformedJSONSourceError) as raised:
        reader.read(malformed)
    assert raised.value.source_text.startswith('{\n  "claim": "kept"')
    assert (raised.value.line, raised.value.column) == (3, 6)

    # The other direction: tagging must be reached only by a parse failure, or
    # every JSON file in the corpus would divert to the text fallback.
    valid = tmp_path / "valid.json"
    valid.write_text('[{"claim": "one"}, {"claim": "two"}]', encoding="utf-8")
    assert [document.content for document in reader.read(valid)] == ['{"claim": "one"}', '{"claim": "two"}']


def test_malformed_json_fallback_reader_chunks_like_this_bases_text(chunking: SafeFixedSizeChunking) -> None:
    """Text served from a failed parse is chunked like any other text of this base.

    Serving it unchunked would turn one long malformed file into a single
    oversized vector, which is the failure the fallback exists to avoid.
    """
    reader = text_fallback_reader(_distinct_text(400), chunking=chunking)
    _assert_carries_base_chunking(reader)

    chunks = [document.content for document in reader.read(Path("claim.json"), name="claim")]

    assert [len(chunk) for chunk in chunks] == [137, 137, 137, 22]


def test_fallback_documents_are_identified_per_file(chunking: SafeFixedSizeChunking) -> None:
    """Two malformed files in one base must not collide in the vector store.

    Agno derives a chunk's id from its document's id, so a fallback document
    carrying a fixed id would make the second malformed file's chunks overwrite
    the first's -- losing content that the fallback exists to keep searchable.
    The caller's name has to survive too, since that is what identifies the
    chunk when no id is set.
    """
    first = text_fallback_reader("first source", chunking=chunking).read(Path("a.json"), name="a")
    second = text_fallback_reader("second source", chunking=chunking).read(Path("b.json"), name="b")

    assert {document.name for document in first} == {"a"}
    assert {document.id for document in first}.isdisjoint({document.id for document in second})


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


@pytest.mark.parametrize("alias", ["utf-8", "UTF-8", "utf8", "U8", "utf_8", "cp65001"])
def test_every_spelling_of_utf8_is_admitted_and_stays_inside_its_file(alias: str, tmp_path: Path) -> None:
    """Admission must follow the codec, not the one spelling the constant happens to hold.

    ``codecs.lookup`` canonicalizes all of these to ``utf-8``. Matching raw
    strings instead would refuse most of them and silently switch the embedding
    prefetch off for those bases, costing throughput with nothing to show for
    it -- no failure, no log line.
    """
    source = tmp_path / "notes.txt"
    source.write_text(_CJK_TEXT, encoding="utf-8")
    reader = TextReader(encoding=alias)

    assert reader_rereads_within_file_size(reader)
    assert _emitted_utf8_bytes(reader, source) <= source.stat().st_size


def test_utf8_with_a_byte_order_mark_is_admitted_and_only_shrinks(tmp_path: Path) -> None:
    """A BOM is consumed rather than re-emitted, so the decode still fits the file."""
    source = tmp_path / "notes.txt"
    source.write_text(_CJK_TEXT, encoding="utf-8-sig")
    reader = TextReader(encoding="utf-8-sig")

    assert reader_rereads_within_file_size(reader)
    assert _emitted_utf8_bytes(reader, source) == source.stat().st_size - len(codecs.BOM_UTF8)


def test_a_refused_encoding_really_would_have_broken_the_budget(tmp_path: Path) -> None:
    """Show the refusal is warranted rather than merely conservative.

    This is the case the check exists for: the same characters occupy fewer
    bytes on disk as UTF-16 than they do once decoded and measured as UTF-8, so
    a budget derived from the file's size would under-count the text held in
    memory.
    """
    source = tmp_path / "notes.txt"
    source.write_text(_CJK_TEXT, encoding="utf-16")
    reader = TextReader(encoding="utf-16")

    assert not reader_rereads_within_file_size(reader)
    assert _emitted_utf8_bytes(reader, source) > source.stat().st_size


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
