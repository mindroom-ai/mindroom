"""Contextual retrieval: model-written context prepended to each chunk before indexing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

import mindroom.knowledge.chunk_context as chunk_context_module
import mindroom.knowledge.file_indexer as file_indexer_module
from mindroom.knowledge.chunk_context import _document_window, chunk_contexts
from mindroom.knowledge.collections import _owned_collection_names, open_collection
from mindroom.knowledge.index_metadata import load_publication
from mindroom.knowledge.manager import KnowledgeManager
from tests.conftest import runtime_paths_for
from tests.knowledge_test_support import _config, patch_vector_store, stored_rows  # noqa: F401

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

pytestmark = pytest.mark.usefixtures("patch_vector_store")


@dataclass
class _Answer:
    content: str
    usage: object = None


@pytest.mark.asyncio
async def test_chunk_contexts_situate_each_chunk_in_its_document(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Each chunk gets its own answer, in order, from a prompt holding the document and the chunk; usage is recorded."""
    prompts: list[tuple[tuple[str, ...], str]] = []
    usage_kinds: list[str] = []

    async def complete(_provider: object, prompt: str, *, instructions: tuple[str, ...]) -> _Answer:
        prompts.append((instructions, prompt))
        return _Answer(content=f"  context {prompt.split('<chunk>', 1)[1].split(maxsplit=1)[0]}  ")

    async def record(_usage: object, *, runtime_paths: object, kind: str) -> None:
        del runtime_paths
        usage_kinds.append(kind)

    monkeypatch.setattr(chunk_context_module.provider_registry, "build_model_provider", lambda *_args: object())
    monkeypatch.setattr(chunk_context_module, "complete_prompt", complete)
    monkeypatch.setattr(chunk_context_module, "record_system_usage", record)
    config = _config(tmp_path, bases={}, agent_bases=[])

    contexts = await chunk_contexts(config, runtime_paths_for(config), "fast", ["alpha one", "beta two"])

    assert contexts == ["context alpha", "context beta"]
    # The shared document leads, in the system message, so a provider can cache it across the file's chunks.
    assert all(instructions == ("<document>\nalpha one\nbeta two\n</document>",) for instructions, _prompt in prompts)
    assert sorted(prompt.split("<chunk>\n", 1)[1].split("\n", 1)[0] for _instructions, prompt in prompts) == [
        "alpha one",
        "beta two",
    ]
    assert usage_kinds == ["knowledge_chunk_context", "knowledge_chunk_context"]


def test_a_long_document_sends_the_window_around_the_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    """A document over the limit is cut to the window centered on the chunk, clamped to the document's ends."""
    monkeypatch.setattr(chunk_context_module, "_MAX_DOCUMENT_CHARS", 10)
    document = "0123456789abcdefghij"

    assert _document_window(document, 10, 2) == "6789abcdef"
    assert _document_window(document, 0, 2) == "0123456789"
    assert _document_window(document, 19, 1) == "abcdefghij"
    assert _document_window("short", 0, 5) == "short"


def _contextual_config(tmp_path: Path) -> Config:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("first part " * 30 + "\n\n" + "second part " * 30, encoding="utf-8")
    (docs / "note.md").write_text("one chunk only", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs}, agent_bases=["docs"])
    config.knowledge_bases["docs"] = config.knowledge_bases["docs"].model_copy(
        update={"chunk_size": 400, "chunk_context_model": "fast"},
    )
    return config


@pytest.mark.asyncio
async def test_refresh_prefixes_multi_chunk_files_with_their_contexts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every chunk of a multi-chunk file is stored after its context; a single-chunk file is stored as it is."""
    requests: list[int] = []

    async def contexts(_config: Config, _paths: RuntimePaths, model_name: str, chunks: Sequence[str]) -> list[str]:
        assert model_name == "fast"
        requests.append(len(chunks))
        return [f"Context {index}." for index in range(len(chunks))]

    monkeypatch.setattr(file_indexer_module, "chunk_contexts", contexts)
    config = _contextual_config(tmp_path)
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths_for(config))

    outcome = await manager.reindex_all()

    assert outcome.published
    assert requests == [2]
    publication = load_publication(manager._base_storage_path)
    assert publication is not None
    assert publication.collection is not None
    documents: dict[str, list[str]] = {}
    for row in stored_rows(open_collection(manager._collections, publication.collection)):
        documents.setdefault(row["metadata"]["source_path"], []).append(row["content"])
    assert documents["note.md"] == ["one chunk only"]
    assert [document.split("\n\n", 1)[0] for document in documents["guide.md"]] == ["Context 0.", "Context 1."]
    assert "first part" in documents["guide.md"][0]


@pytest.mark.asyncio
async def test_a_failed_context_call_fails_only_that_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A context model failure records the file as failed and indexes the rest."""

    async def failing(*_args: object) -> list[str]:
        msg = "model unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(file_indexer_module, "chunk_contexts", failing)
    config = _contextual_config(tmp_path)
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths_for(config))

    outcome = await manager.reindex_all()

    assert not outcome.published
    [candidate_name] = _owned_collection_names(manager._collections)
    candidate = open_collection(manager._collections, candidate_name)
    assert set(candidate.files()) == {"note.md"}
    assert candidate.claim_counts() == (1, 1)


def test_neighbouring_chunks_of_a_long_document_share_a_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows snap to a quarter-window stride, so nearby chunks send the same text and a prompt cache can reuse it."""
    monkeypatch.setattr(chunk_context_module, "_MAX_DOCUMENT_CHARS", 40)
    document = "".join(chr(65 + index % 26) for index in range(200))

    assert _document_window(document, 100, 4) == _document_window(document, 104, 4)
    assert document.index(_document_window(document, 100, 4)) <= 100
