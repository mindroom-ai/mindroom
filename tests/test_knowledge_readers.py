"""Which reader answers for a knowledge file, and where it puts the chunk boundaries.

Chunk boundaries decide what a knowledge base's vector store contains, and
almost nothing else asserts them: a reader carrying the wrong chunking still
indexes, still publishes, and still answers queries, only against different
vectors. These tests start from a base's authored config, assert every reader
field that can move a boundary by value, and read real text so the boundaries
are observed rather than inferred from attributes.

They exercise ``KnowledgeManager``'s reader construction directly rather than
through ``reindex_all``. That is deliberate, and the reason is the hazard in
:func:`test_two_bases_neither_share_a_reader_nor_poison_the_factory_cache`:
``ReaderFactory`` caches one reader per extension, and ``_build_reader``
reconfigures a copy. Delete that copy and the existing end-to-end suites still
pass, because within one refresh every ``_build_reader`` call re-applies that
manager's own chunking before the reader is used, so a sequential refresh stays
internally consistent. The damage needs two bases' refreshes to interleave --
a real possibility, since the source-root lock is per root and prefetch reads
run on worker threads, but not something a deterministic end-to-end test can
stage. Observing the constructed reader is the only way to hold the invariant.
"""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from agno.knowledge.chunking.fixed import FixedSizeChunking
from agno.knowledge.reader import ReaderFactory
from agno.knowledge.reader.markdown_reader import MarkdownReader
from agno.knowledge.reader.text_reader import TextReader

from mindroom.chunking import SafeFixedSizeChunking
from mindroom.config.agent import AgentConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.knowledge.manager import (
    KnowledgeManager,
    _InMemoryTextReader,
    _MalformedJSONSourceError,
)
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from agno.knowledge.reader.base import Reader

#: Chunk settings no factory default could be mistaken for (Agno ships 5000/0).
_CHUNK_SIZE = 137
_CHUNK_OVERLAP = 11

#: Suffixes whose reader MindRoom reconfigures with the base's chunking.
_CHUNKED_SUFFIXES = (".md", ".markdown", ".txt", ".py", ".yaml", ".html", ".xyz", "")
#: Suffixes whose reader owns its own splitting and must be left alone.
_UNTOUCHED_SUFFIXES = (".csv", ".xlsx", ".docx")


def _manager(root: Path, *, chunk_size: int = _CHUNK_SIZE, chunk_overlap: int = _CHUNK_OVERLAP) -> KnowledgeManager:
    """Build a manager for one knowledge base authoring these chunk settings."""
    docs_path = root / "docs"
    docs_path.mkdir(parents=True, exist_ok=True)
    runtime_paths = test_runtime_paths(root)
    config = bind_runtime_paths(
        Config(
            agents={"helper": AgentConfig(display_name="Helper", knowledge_bases=["docs"])},
            models={},
            memory={},
            knowledge_bases={
                "docs": KnowledgeBaseConfig(
                    path=str(docs_path),
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                ),
            },
        ),
        runtime_paths,
    )
    return KnowledgeManager("docs", config=config, runtime_paths=runtime_paths_for(config))


@pytest.fixture
def manager(tmp_path: Path) -> KnowledgeManager:
    """Return a manager whose base authors distinctive chunk settings."""
    return _manager(tmp_path)


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
    # None is how these readers spell UTF-8 (``self.encoding or "utf-8"``), which
    # is what lets `SafeFixedSizeChunking.max_chunk_text_bytes` bound decoded
    # text by a size on disk -- the precondition the prefetch budget rests on.
    assert reader.encoding is None


def test_two_bases_neither_share_a_reader_nor_poison_the_factory_cache(tmp_path: Path) -> None:
    """Configuring a reader must copy it first, because the factory's instance is shared.

    ``ReaderFactory`` hands out one cached reader per extension. Configuring
    that instance in place would give two bases the same object: whichever
    refreshed second would re-chunk the other's corpus at its own size, and the
    cache would keep serving that size for the rest of the process.
    """
    cached = ReaderFactory.get_reader_for_extension(".md")
    cached_strategy = cached.chunking_strategy
    cached_chunk_size = cached.chunk_size

    small = _manager(tmp_path / "small", chunk_size=137, chunk_overlap=11)._build_reader(Path("a.md"))
    large = _manager(tmp_path / "large", chunk_size=2000, chunk_overlap=0)._build_reader(Path("b.md"))

    assert small is not large
    assert (small.chunk_size, large.chunk_size) == (137, 2000)
    assert small.chunking_strategy is not large.chunking_strategy
    # The shared cache entry must be exactly as it was found.
    assert cached is not small
    assert cached is not large
    assert cached.chunking_strategy is cached_strategy
    assert cached.chunk_size == cached_chunk_size


def test_the_json_reader_does_not_borrow_the_cached_chunking_strategy(manager: KnowledgeManager) -> None:
    """The JSON subclass must copy the factory's chunker, not alias it.

    Nothing mutates a JSON reader's strategy today, so this pins the same
    copy-before-use rule one branch over rather than a present defect: an
    aliased strategy would put the shared cache one in-place edit away from
    every base that reads JSON.
    """
    cached = ReaderFactory.get_reader_for_extension(".json")

    reader = manager._build_reader(Path("source.json"))

    assert reader is not cached
    assert reader.chunking_strategy is not cached.chunking_strategy


@pytest.mark.parametrize("suffix", _CHUNKED_SUFFIXES)
def test_text_readers_carry_this_bases_chunking(suffix: str, manager: KnowledgeManager) -> None:
    """A text-like reader must chunk by the base's policy, not the factory's 5000/0."""
    reader = manager._build_reader(Path(f"source{suffix}"))

    assert isinstance(reader, (TextReader, MarkdownReader))
    _assert_carries_base_chunking(reader)


def test_text_reader_splits_a_file_on_the_bases_boundaries(tmp_path: Path, manager: KnowledgeManager) -> None:
    """Observe the boundaries a real read produces rather than trusting attributes.

    Both settings are visible in the output: chunk length is capped at
    ``chunk_size``, and each chunk after the first opens with the ``overlap``
    characters that closed its predecessor.
    """
    source = tmp_path / "notes.txt"
    source.write_text(_distinct_text(400), encoding="utf-8")
    reader = manager._build_reader(source)

    chunks = [document.content for document in reader.read(source, name="notes")]

    assert [len(chunk) for chunk in chunks] == [137, 137, 137, 22]
    for earlier, later in pairwise(chunks):
        assert later[:_CHUNK_OVERLAP] == earlier[-_CHUNK_OVERLAP:]


def test_a_second_base_moves_the_reader_and_its_boundaries(tmp_path: Path) -> None:
    """A different authored config must reach the reader and change where it splits.

    Every other case here uses one chunk size, which cannot tell a value read
    from config apart from a constant that happens to equal it.
    """
    source = tmp_path / "notes.txt"
    source.write_text(_distinct_text(1200), encoding="utf-8")
    reader = _manager(tmp_path / "base", chunk_size=512, chunk_overlap=64)._build_reader(source)

    assert reader.chunk_size == 512

    chunks = [document.content for document in reader.read(source, name="notes")]

    assert [len(chunk) for chunk in chunks] == [512, 512, 304]
    for earlier, later in pairwise(chunks):
        assert later[:64] == earlier[-64:]


@pytest.mark.parametrize("suffix", _UNTOUCHED_SUFFIXES)
def test_non_text_readers_keep_their_factory_chunking(suffix: str, manager: KnowledgeManager) -> None:
    """Row and document readers own their splitting; MindRoom must not overwrite it."""
    reader = manager._build_reader(Path(f"source{suffix}"))

    assert not isinstance(reader.chunking_strategy, SafeFixedSizeChunking)


def test_json_keeps_structured_chunking_and_tags_its_parse_failures(
    tmp_path: Path,
    manager: KnowledgeManager,
) -> None:
    """JSON must be read as JSON, and a parse failure must carry its source text.

    Taking MindRoom's text chunking here would split structured documents by
    size, and losing the tagging drops the malformed-source fallback entirely.
    """
    reader = manager._build_reader(Path("source.json"))

    assert type(reader.chunking_strategy) is FixedSizeChunking
    assert reader.chunking_strategy.chunk_size == 5000

    # The broken key is indented so line and column differ; equal values would
    # make the assertion below blind to the two being swapped.
    malformed = tmp_path / "claim.json"
    malformed.write_text('{\n  "claim": "kept",\n     “broken”: true\n}\n', encoding="utf-8")
    with pytest.raises(_MalformedJSONSourceError) as raised:
        reader.read(malformed)
    assert raised.value.source_text.startswith('{\n  "claim": "kept"')
    assert (raised.value.line, raised.value.column) == (3, 6)

    # The other direction: tagging must be reached only by a parse failure, or
    # every JSON file in the corpus would divert to the text fallback. The
    # caller's name has to reach every document too -- Chroma stores it as the
    # `name` metadata field and queries by it, so dropping it renames every
    # chunk of the file in search results.
    valid = tmp_path / "valid.json"
    valid.write_text('[{"claim": "one"}, {"claim": "two"}]', encoding="utf-8")
    documents = reader.read(valid, name="valid.json")
    assert [document.content for document in documents] == ['{"claim": "one"}', '{"claim": "two"}']
    assert {document.name for document in documents} == {"valid.json"}


def test_json_elements_above_the_factory_chunk_size_are_still_split(
    tmp_path: Path,
    manager: KnowledgeManager,
) -> None:
    """JSON keeps the factory's 5000-character chunking, so a larger value must split.

    Every other JSON case here uses a short document, which cannot tell
    chunking-enabled from chunking-disabled: both emit one document. Copying
    the factory reader with chunking off would put a single oversized element
    into one vector.
    """
    source = tmp_path / "big.json"
    source.write_text(json.dumps({"claim": "x" * 6013}), encoding="utf-8")
    reader = manager._build_reader(source)

    documents = reader.read(source)

    assert reader.chunk is True
    assert len(documents) > 1
    assert max(len(document.content) for document in documents) <= 5000


def test_malformed_json_fallback_reader_chunks_like_this_bases_text(manager: KnowledgeManager) -> None:
    """Text served from a failed parse is chunked like any other text of this base.

    Serving it unchunked would turn one long malformed file into a single
    oversized vector, which is the failure the fallback exists to avoid.
    """
    reader = manager._configure_text_reader(_InMemoryTextReader(_distinct_text(400)))
    _assert_carries_base_chunking(reader)

    chunks = [document.content for document in reader.read(Path("claim.json"), name="claim")]

    assert [len(chunk) for chunk in chunks] == [137, 137, 137, 22]


def test_fallback_documents_keep_the_callers_name(manager: KnowledgeManager) -> None:
    """The name the caller passed must reach every chunk of the fallback read.

    Chroma stores it as the ``name`` metadata field and looks documents up by
    it, so a fallback that substituted its own would make the file's chunks
    findable under the wrong identifier.

    Deliberately not asserted here: that two fallback documents carry distinct
    ids. Agno mixes the document id with a per-file content hash before it
    reaches the store, so a fixed id collides at this level and still yields
    distinct vector ids -- there is no overwrite to protect against, and an
    assertion implying otherwise would document a hazard that does not exist.
    """
    reader = manager._configure_text_reader(_InMemoryTextReader("retained source text"))

    documents = reader.read(Path("a.json"), name="a")

    assert {document.name for document in documents} == {"a"}
