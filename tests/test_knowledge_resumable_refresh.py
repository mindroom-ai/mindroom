"""Resumable, bounded semantic knowledge refresh behavior.

These tests drive the candidate-index lifecycle through a fake vector store and
a recording embedder so every assertion about *which* files were embedded, how
many provider requests were issued, and what survives an interruption is exact.

Against the pre-fix implementation the resume, retry and bounded-work tests
fail: the candidate collection was deleted on every non-publishing outcome and
all progress lived in process memory.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

import pytest
from agno.knowledge.document.base import Document
from agno.knowledge.embedder.base import Embedder
from chromadb.errors import InternalError, NotFoundError
from structlog.testing import capture_logs

import mindroom.knowledge.manager as knowledge_manager_module
import mindroom.knowledge.registry as knowledge_registry
from mindroom.config.agent import AgentConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.embedding_errors import (
    EmbedderRequestError,
    embedder_failure_is_transient,
    embedder_retry_after_seconds,
)
from mindroom.knowledge import resolve_agent_knowledge_access
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.candidate_checkpoint import (
    CandidateCheckpoint,
    CandidateFailure,
    _candidate_checkpoint_path,
    _candidate_journal_path,
    append_candidate_journal,
    delete_candidate_checkpoint,
    load_candidate_checkpoint,
    save_candidate_checkpoint,
)
from mindroom.knowledge.embedding_batch import BatchPrefetchEmbedder, plan_embedding_batches
from mindroom.knowledge.index_metadata import write_index_metadata_payload
from mindroom.knowledge.index_retry import EmbeddingRetryPolicy, run_with_embedding_retry
from mindroom.knowledge.manager import KnowledgeManager
from mindroom.knowledge.refresh_runner import refresh_knowledge_binding
from mindroom.knowledge.registry import (
    PublishedIndexState,
    get_published_index,
    load_published_index_state,
    published_index_metadata_path,
    published_index_storage_path,
    resolve_published_index_key,
)
from mindroom.knowledge.status import get_knowledge_index_status
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.knowledge_test_support import chroma_get_result, metadata_matches

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from mindroom.constants import RuntimePaths


# --------------------------------------------------------------------------
# Fake vector store + recording embedder
# --------------------------------------------------------------------------


class _SupportsRead(Protocol):
    """Reader surface the fake Knowledge needs to reproduce Agno's chunking."""

    def read(self, source: Path, name: str) -> list[Document]:
        """Return the documents Agno would embed for one file."""
        ...


@dataclass
class _Record:
    content: str
    embedding: list[float]
    metadata: dict[str, Any]


class _FakeCollection:
    def __init__(self, name: str) -> None:
        self._name = name

    def get(
        self,
        *,
        include: Sequence[str],
        limit: int | None = None,
        offset: int = 0,
        where: dict[str, object] | None = None,
    ) -> dict[str, object]:
        _FakeVectorDb.get_calls += 1
        if self._name in _FakeVectorDb.vanished_on_get:
            message = f"Collection {self._name!r} does not exist"
            raise NotFoundError(message)
        records = list(_FakeVectorDb.store.get(self._name, []))
        if where:
            key, condition = next(iter(where.items()))
            records = [record for record in records if metadata_matches(record.metadata, key, condition)]
        selected = records[offset:] if limit is None else records[offset : offset + limit]
        _FakeVectorDb.enforce_row_ceiling(len(selected))
        return chroma_get_result(
            ids=[str(index) for index in range(len(selected))],
            metadatas=[dict(record.metadata) for record in selected],
            include=include,
        )

    def delete(self, *, where: dict[str, object]) -> None:
        key, condition = next(iter(where.items()))
        _FakeVectorDb.store[self._name] = [
            record
            for record in _FakeVectorDb.store.get(self._name, [])
            if not metadata_matches(record.metadata, key, condition)
        ]


class _FakeClient:
    def get_collection(self, name: str) -> _FakeCollection:
        if name not in _FakeVectorDb.store:
            message = f"Collection {name!r} does not exist"
            raise NotFoundError(message)
        return _FakeCollection(name)

    def list_collections(self) -> list[str]:
        return sorted(_FakeVectorDb.store)


class _FakeVectorDb:
    store: ClassVar[dict[str, list[_Record]]] = {}
    #: Rows one ``get`` may return before the store rejects the whole query,
    #: mirroring SQLite's bind-variable ceiling: Chroma binds one variable per
    #: *returned row*, so the limit is a property of the result, not of the
    #: ``$in`` list. ``None`` leaves the store unbounded.
    max_rows_per_get: ClassVar[int | None] = None
    #: ``get`` calls issued, so a test can prove a query was not needlessly split.
    get_calls: ClassVar[int] = 0
    #: Collections that still resolve through ``get_collection`` but are gone by
    #: the time the query runs, as when a sweep deletes one mid-verification.
    vanished_on_get: ClassVar[set[str]] = set()

    @classmethod
    def enforce_row_ceiling(cls, rows: int) -> None:
        """Reject a query whose result would exceed the store's bind-variable ceiling."""
        if cls.max_rows_per_get is not None and rows > cls.max_rows_per_get:
            message = (
                "Error executing plan: Internal error: error returned from database: (code: 1) too many SQL variables"
            )
            raise InternalError(message)

    def __init__(self, *, collection: str, embedder: Embedder | None = None, **_: object) -> None:
        self.collection_name = collection
        self.embedder = embedder
        self.client = _FakeClient()

    def exists(self) -> bool:
        return self.collection_name in self.store

    def create(self) -> None:
        self.store.setdefault(self.collection_name, [])

    def delete(self) -> bool:
        if self.collection_name not in self.store:
            return False
        self.store.pop(self.collection_name)
        return True

    def search(self, *, query: str, limit: int, filters: object = None) -> list[Document]:
        _ = (query, filters)
        return [
            Document(content=record.content, meta_data=dict(record.metadata))
            for record in self.store.get(self.collection_name, [])[:limit]
        ]

    async def async_search(self, *, query: str, limit: int, filters: object = None) -> list[Document]:
        return self.search(query=query, limit=limit, filters=filters)


class _FakeKnowledge:
    """Knowledge stand-in that embeds every chunk exactly like Agno's write path."""

    def __init__(self, vector_db: _FakeVectorDb | None = None) -> None:
        self.vector_db = vector_db
        self.name: str | None = None
        self.description: str | None = None
        self.max_results = 5

    def insert(
        self,
        *,
        path: str,
        metadata: dict[str, Any],
        upsert: bool,
        reader: _SupportsRead | None = None,
    ) -> None:
        _ = upsert
        assert self.vector_db is not None
        source = Path(path)
        documents = (
            reader.read(source, name=source.name) if reader is not None else [Document(content=source.read_text())]
        )
        records = _FakeVectorDb.store.setdefault(self.vector_db.collection_name, [])
        embedded: list[_Record] = []
        for document in documents:
            embedder = self.vector_db.embedder
            assert embedder is not None
            embedding, _usage = embedder.get_embedding_and_usage(document.content)
            embedded.append(_Record(content=document.content, embedding=embedding, metadata=dict(metadata)))
        records.extend(embedded)

    def remove_vectors_by_metadata(self, metadata: dict[str, Any]) -> bool:
        assert self.vector_db is not None
        records = _FakeVectorDb.store.get(self.vector_db.collection_name, [])
        kept = [
            record
            for record in records
            if not all(record.metadata.get(key) == value for key, value in metadata.items())
        ]
        _FakeVectorDb.store[self.vector_db.collection_name] = kept
        return len(kept) != len(records)

    def search(self, query: str, max_results: int | None = None) -> list[Document]:
        assert self.vector_db is not None
        return self.vector_db.search(query=query, limit=max_results or self.max_results)


class _RecordingNonBatchEmbedder(Embedder):
    """Recording embedder with no batch surface at all, like Ollama.

    The absence of ``get_embeddings_batch`` is the point: a boolean flag cannot
    model it, because the capability check tests for the method's existence.
    """

    def __init__(self) -> None:
        super().__init__()
        self.batch_requests: list[tuple[str, ...]] = []
        self.single_requests: list[str] = []
        self.embedded_texts: list[str] = []
        self.failures: dict[str, list[BaseException]] = {}
        self.fail_everything: BaseException | None = None
        self.supports_batch = True
        #: Return one vector fewer than requested for any multi-input call,
        #: mimicking an OpenAI-compatible backend that accepts array input but
        #: does not really implement it.
        self.short_batch = False
        #: Raise this on any multi-input call (the classified error the real
        #: MindRoomOpenAIEmbedder raises for a short response).
        self.batch_error: BaseException | None = None

    @property
    def request_count(self) -> int:
        return len(self.batch_requests) + len(self.single_requests)

    def embedded_count(self, text: str) -> int:
        return self.embedded_texts.count(text)

    def _maybe_fail(self, texts: list[str]) -> None:
        if self.fail_everything is not None:
            raise self.fail_everything
        for text in texts:
            queued = self.failures.get(text)
            if queued:
                raise queued.pop(0)

    def get_embedding(self, text: str) -> list[float]:
        return self.get_embedding_and_usage(text)[0]

    def get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, Any] | None]:
        self.single_requests.append(text)
        self._maybe_fail([text])
        self.embedded_texts.append(text)
        return [float(len(text)), 1.0], None


class _RecordingEmbedder(_RecordingNonBatchEmbedder):
    """Recording embedder that also advertises multi-input support."""

    def get_embeddings_batch(self, texts: list[str]) -> list[list[float]]:
        if not self.supports_batch:
            msg = "batch embedding is disabled for this test"
            raise AssertionError(msg)
        self.batch_requests.append(tuple(texts))
        if len(texts) > 1 and self.batch_error is not None:
            raise self.batch_error
        embeddings = [[float(len(text)), 1.0] for text in texts]
        if len(texts) > 1 and self.short_batch:
            # A short response is a cardinality fault: it happens before the
            # backend would have reported anything about individual inputs.
            self.embedded_texts.extend(texts[:-1])
            return embeddings[:-1]
        self._maybe_fail(texts)
        self.embedded_texts.extend(texts)
        return embeddings


def _use_non_batching_embedder(monkeypatch: pytest.MonkeyPatch) -> _RecordingNonBatchEmbedder:
    """Point the manager at a provider that cannot batch at all."""
    plain = _RecordingNonBatchEmbedder()
    monkeypatch.setattr(knowledge_manager_module, "create_configured_embedder", lambda *_a, **_k: plain)
    return plain


class _NonBatchingEmbedder(Embedder):
    """Embedder without a batch surface, to prove the adapter degrades safely."""

    def __init__(self) -> None:
        super().__init__()
        self.single_requests: list[str] = []

    def get_embedding(self, text: str) -> list[float]:
        return self.get_embedding_and_usage(text)[0]

    def get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, Any] | None]:
        self.single_requests.append(text)
        return [float(len(text)), 1.0], None


@pytest.fixture
def embedder() -> _RecordingEmbedder:
    """Return the single embedder every manager in one test shares."""
    return _RecordingEmbedder()


@pytest.fixture(autouse=True)
def fake_vector_store(
    monkeypatch: pytest.MonkeyPatch,
    embedder: _RecordingEmbedder,
) -> Iterator[None]:
    """Install the in-memory vector store, fake Knowledge and recording embedder."""
    _FakeVectorDb.store = {}
    _FakeVectorDb.max_rows_per_get = None
    _FakeVectorDb.get_calls = 0
    _FakeVectorDb.vanished_on_get = set()
    monkeypatch.setattr(knowledge_manager_module, "ChromaDb", _FakeVectorDb)
    monkeypatch.setattr(knowledge_manager_module, "Knowledge", _FakeKnowledge)
    monkeypatch.setattr(knowledge_manager_module, "create_configured_embedder", lambda *_a, **_k: embedder)
    monkeypatch.setattr("mindroom.knowledge.indexing_config.ChromaDb", _FakeVectorDb)
    monkeypatch.setattr(knowledge_registry, "ChromaDb", _FakeVectorDb)
    monkeypatch.setattr(knowledge_registry, "StrictSearchKnowledge", _FakeKnowledge)
    monkeypatch.setattr(knowledge_registry, "create_configured_embedder", lambda *_a, **_k: embedder)

    async def _no_sleep(_seconds: float) -> None:
        return

    monkeypatch.setattr(knowledge_manager_module, "_EMBEDDING_RETRY_SLEEP", _no_sleep)
    knowledge_registry._published_indexes.clear()
    yield
    knowledge_registry._published_indexes.clear()
    _FakeVectorDb.store = {}


# --------------------------------------------------------------------------
# Config / manager helpers
# --------------------------------------------------------------------------


def _config(
    tmp_path: Path,
    docs_path: Path,
    *,
    chunk_size: int = 5000,
    chunk_overlap: int = 0,
) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
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


def _manager(config: Config) -> KnowledgeManager:
    return KnowledgeManager("docs", config=config, runtime_paths=runtime_paths_for(config))


def _storage_path(config: Config, runtime_paths: RuntimePaths) -> Path:
    return published_index_storage_path(
        resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths),
    )


def _write_corpus(docs_path: Path, count: int, *, body: str = "content") -> list[str]:
    docs_path.mkdir(parents=True, exist_ok=True)
    names = [f"doc{index:04d}.md" for index in range(count)]
    for index, name in enumerate(names):
        (docs_path / name).write_text(f"{body} {index}", encoding="utf-8")
    return names


def _overlapping_body(tokens: int) -> str:
    """Return text whose chunks all differ, so batched requests cannot dedupe them away."""
    return " ".join(f"token{index:04d}" for index in range(tokens))


def _candidate_collections() -> list[str]:
    return sorted(name for name in _FakeVectorDb.store if "_candidate_" in name)


def _published_state(config: Config, runtime_paths: RuntimePaths) -> PublishedIndexState | None:
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    return load_published_index_state(published_index_metadata_path(key))


_AUTH_FAILURE = EmbedderRequestError("embedder authentication failed (HTTP 401)")


def _api_error() -> Exception:
    import httpx  # noqa: PLC0415
    from openai import APIStatusError  # noqa: PLC0415

    request = httpx.Request("POST", "http://embeddings.local/v1/embeddings")
    response = httpx.Response(503, request=request, headers={"retry-after": "7"})
    return APIStatusError("overloaded", response=response, body=None)


# --------------------------------------------------------------------------
# 1. Cold-start interruption resumes the same candidate
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupted_cold_build_resumes_same_candidate_without_reembedding(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A killed build resumes its candidate and only embeds what it still owes."""
    monkeypatch.setenv("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY", "1")
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 6)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)

    interrupt_after = 3
    indexed_before_interrupt: list[str] = []
    manager = _manager(config)
    original_index = KnowledgeManager._index_file_locked

    async def _stop_midway(self: KnowledgeManager, resolved_path: Path, **kwargs: object) -> bool:
        if len(indexed_before_interrupt) >= interrupt_after:
            raise asyncio.CancelledError
        indexed_before_interrupt.append(resolved_path.name)
        return await original_index(self, resolved_path, **kwargs)

    KnowledgeManager._index_file_locked = _stop_midway  # type: ignore[method-assign]
    try:
        with pytest.raises(asyncio.CancelledError):
            await manager.reindex_all()
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    assert len(checkpoint.completed) == interrupt_after
    interrupted_collection = checkpoint.collection
    # Only durably completed files may be skipped on resume; vectors that were
    # merely prefetched into memory are legitimately re-embedded.
    completed_bodies = {f"content {names.index(name)}" for name in checkpoint.completed}
    embedded_after_interrupt = {body: embedder.embedded_count(body) for body in completed_bodies}

    # A brand new manager models a process restart: nothing survives in memory.
    resumed_manager = _manager(config)
    assert await resumed_manager.reindex_all() == len(names) - interrupt_after

    resumed_checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert resumed_checkpoint is None, "publication retires the checkpoint"
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.status == "complete"
    assert state.collection == interrupted_collection, "resume must continue the same candidate"
    assert state.indexed_count == len(names)
    for body, count in embedded_after_interrupt.items():
        assert embedder.embedded_count(body) == count, "completed files must not be embedded again"
    stored = sorted(record.metadata["source_path"] for record in _FakeVectorDb.store[interrupted_collection])
    assert stored == sorted(names)


# --------------------------------------------------------------------------
# 2. Restart with a last-good index
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidate_resume_never_disturbs_the_last_good_published_index(
    tmp_path: Path,
) -> None:
    """A failing candidate resumes privately while the published index stays queryable."""
    docs_path = tmp_path / "docs"
    (docs_path / "").parent.mkdir(parents=True, exist_ok=True)
    docs_path.mkdir()
    (docs_path / "a.md").write_text("first published", encoding="utf-8")
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    published_state = _published_state(config, runtime_paths)
    assert published_state is not None
    published_collection = published_state.collection

    (docs_path / "a.md").write_text("second revision", encoding="utf-8")
    (docs_path / "b.md").write_text("cannot index", encoding="utf-8")
    original_index = KnowledgeManager._index_file_locked

    async def _fail_b(self: KnowledgeManager, resolved_path: Path, **kwargs: object) -> bool:
        if resolved_path.name == "b.md":
            return False
        return await original_index(self, resolved_path, **kwargs)

    KnowledgeManager._index_file_locked = _fail_b  # type: ignore[method-assign]
    try:
        result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

    assert result.index_published is False
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.index is not None
    assert [document.content for document in lookup.index.knowledge.search("published", max_results=5)] == [
        "first published",
    ]
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.collection == published_collection
    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    assert checkpoint.collection != published_collection
    assert set(checkpoint.completed) == {"a.md"}
    assert set(checkpoint.failed) == {"b.md"}


# --------------------------------------------------------------------------
# 3-5. Embedding failure classification and retry
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_embedding_failure_retries_only_the_failed_work(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """One transient fault near the end costs a retry, not the whole build."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 5)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    embedder.failures["content 4"] = [EmbedderRequestError("embedder request failed (HTTP 503)")]

    manager = _manager(config)
    assert await manager.reindex_all() == 5
    assert manager._last_refresh_error is None

    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.status == "complete"
    assert state.indexed_count == 5
    # Only the faulted text was embedded twice; nothing else was redone.
    assert embedder.embedded_count("content 4") == 1
    assert [text for text in {*embedder.embedded_texts} if embedder.embedded_count(text) > 1] == []
    assert (
        embedder.single_requests.count("content 4")
        + sum(1 for batch in embedder.batch_requests if "content 4" in batch)
        >= 2
    )


@pytest.mark.asyncio
async def test_exhausted_transient_retries_keep_candidate_and_resume_only_unresolved_work(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecoverable-for-now file leaves the rest of the candidate intact."""
    monkeypatch.setattr(
        knowledge_manager_module,
        "_EMBEDDING_RETRY_POLICY",
        EmbeddingRetryPolicy(max_attempts=2, initial_backoff_seconds=0.0),
    )
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    embedder.failures["content 3"] = [EmbedderRequestError("embedder request failed (HTTP 503)") for _ in range(20)]

    manager = _manager(config)
    assert await manager.reindex_all() == 3
    assert manager._last_refresh_error is not None
    assert "Indexed 3 of 4" in manager._last_refresh_error
    assert _published_state(config, runtime_paths) is None

    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    assert checkpoint.status == "failed"
    assert set(checkpoint.failed) == {"doc0003.md"}
    assert checkpoint.failed["doc0003.md"].attempts == 1
    assert len(checkpoint.completed) == 3

    embedder.failures.pop("content 3")
    embedded_before = dict.fromkeys(embedder.embedded_texts)
    assert await _manager(config).reindex_all() == 1
    for text in embedded_before:
        assert embedder.embedded_count(text) == 1, "resume must not re-embed resolved files"
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.status == "complete"
    assert state.indexed_count == 4


@pytest.mark.asyncio
async def test_candidate_failures_record_each_files_actual_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent file failures must not all inherit the run's first error."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    embedder = _use_non_batching_embedder(monkeypatch)
    embedder.failures["content 0"] = [EmbedderRequestError("embedder request failed (HTTP 400)")]
    embedder.failures["content 2"] = [EmbedderRequestError("embedder returned an empty vector")]

    await _manager(config).reindex_all()

    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    assert checkpoint.failed["doc0000.md"].last_error == "embedder request failed (HTTP 400)"
    assert checkpoint.failed["doc0002.md"].last_error == "embedder returned an empty vector"


@pytest.mark.asyncio
async def test_permanent_embedding_failure_never_publishes_and_reports_classified_error(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Auth failures fail fast, keep last-good, and surface only classified text."""
    docs_path = tmp_path / "docs"
    (docs_path).mkdir()
    (docs_path / "a.md").write_text("published body", encoding="utf-8")
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    state = _published_state(config, runtime_paths)
    assert state is not None
    published_collection = state.collection

    (docs_path / "b.md").write_text("secret sk-should-never-appear", encoding="utf-8")
    embedder.fail_everything = EmbedderRequestError("embedder authentication failed (HTTP 401)")

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is False
    assert result.availability is KnowledgeAvailability.REFRESH_FAILED
    assert result.last_error is not None
    assert "embedder authentication failed (HTTP 401)" in result.last_error
    assert "sk-should-never-appear" not in result.last_error
    # A permanent rejection must not cost one doomed request per remaining file.
    assert embedder.request_count <= 3
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.collection == published_collection
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.index is not None
    assert [document.content for document in lookup.index.knowledge.search("body", max_results=5)] == [
        "published body",
    ]


def test_embedding_failure_classification_splits_transient_from_permanent() -> None:
    """Only retryable transport and throttling faults are classified transient."""
    assert embedder_failure_is_transient(EmbedderRequestError("embedder endpoint unreachable"))
    assert embedder_failure_is_transient(EmbedderRequestError("embedder request failed (HTTP 429)"))
    assert embedder_failure_is_transient(EmbedderRequestError("embedder request failed (HTTP 408)"))
    assert embedder_failure_is_transient(EmbedderRequestError("embedder request failed (HTTP 502)"))
    assert embedder_failure_is_transient(TimeoutError())
    assert embedder_failure_is_transient(ConnectionResetError())
    assert not embedder_failure_is_transient(EmbedderRequestError("embedder authentication failed (HTTP 401)"))
    assert not embedder_failure_is_transient(EmbedderRequestError("embedder permission denied (HTTP 403)"))
    assert not embedder_failure_is_transient(EmbedderRequestError("embedder request failed (HTTP 400)"))
    assert not embedder_failure_is_transient(EmbedderRequestError("embedder returned an empty vector"))
    assert not embedder_failure_is_transient(ValueError("nonsense"))


def test_provider_retry_after_header_survives_error_classification() -> None:
    """The provider backoff hint must cross the credential-redacting boundary."""
    assert embedder_retry_after_seconds(_api_error()) == 7.0
    assert embedder_retry_after_seconds(EmbedderRequestError("x", retry_after_seconds=3.5)) == 3.5
    assert embedder_retry_after_seconds(EmbedderRequestError("x")) is None


def test_retry_backoff_honors_retry_after_and_stays_bounded() -> None:
    """Backoff grows, jitters, respects Retry-After, and never exceeds the cap."""
    policy = EmbeddingRetryPolicy(initial_backoff_seconds=1.0, max_backoff_seconds=10.0, jitter_ratio=0.5)
    assert policy.backoff_seconds(1, retry_after_seconds=None, jitter_unit=0.5) == 1.0
    assert policy.backoff_seconds(3, retry_after_seconds=None, jitter_unit=0.5) == 4.0
    assert policy.backoff_seconds(9, retry_after_seconds=None, jitter_unit=0.5) == 10.0
    assert policy.backoff_seconds(1, retry_after_seconds=6.0, jitter_unit=0.5) == 6.0
    assert policy.backoff_seconds(1, retry_after_seconds=1000.0, jitter_unit=0.5) == 10.0
    assert policy.backoff_seconds(1, retry_after_seconds=None, jitter_unit=0.0) == 0.5
    assert policy.backoff_seconds(1, retry_after_seconds=None, jitter_unit=1.0) == 1.5
    assert policy.backoff_seconds(9, retry_after_seconds=None, jitter_unit=1.0) == 10.0


@pytest.mark.asyncio
async def test_retry_runner_stops_immediately_on_permanent_failures() -> None:
    """A permanent failure must not consume the retry budget."""
    attempts = 0

    async def _always_unauthorized() -> None:
        nonlocal attempts
        attempts += 1
        raise _AUTH_FAILURE

    async def _no_sleep(_seconds: float) -> None:
        return

    with pytest.raises(EmbedderRequestError):
        await run_with_embedding_retry(
            _always_unauthorized,
            policy=EmbeddingRetryPolicy(max_attempts=5),
            sleep=_no_sleep,
        )
    assert attempts == 1


# --------------------------------------------------------------------------
# 6. Settings changes
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_incompatible_settings_start_a_clean_candidate_and_keep_published_index(
    tmp_path: Path,
) -> None:
    """A settings change discards the incompatible candidate, not the last-good index."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    state = _published_state(config, runtime_paths)
    assert state is not None
    published_collection = state.collection

    # Leave a partial candidate behind under the current settings.
    storage_path = _storage_path(config, runtime_paths)
    stale_candidate = f"{published_collection}_candidate_stalecandidate"
    _FakeVectorDb.store[stale_candidate] = []
    save_candidate_checkpoint(
        storage_path,
        CandidateCheckpoint(
            collection=stale_candidate,
            settings=_manager(config)._indexing_settings,
            completed={"doc0000.md": (1, 1, "digest")},
        ),
    )

    changed_config = config.model_copy(deep=True)
    changed_config.memory.embedder.config.model = "text-embedding-3-large"
    changed_manager = KnowledgeManager("docs", config=changed_config, runtime_paths=runtime_paths)
    run = await changed_manager._open_candidate_run()

    assert run.resumed is False
    assert run.checkpoint.collection != stale_candidate
    assert stale_candidate not in _FakeVectorDb.store, "incompatible candidate is discarded"
    assert published_collection in _FakeVectorDb.store, "published index is not touched"


@pytest.mark.asyncio
async def test_incompatible_candidate_delete_failure_does_not_block_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale candidate cannot make a base permanently unrefreshable."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    manager = _manager(config)
    stale_candidate = f"{manager._default_collection_name()}_candidate_stale"
    _FakeVectorDb.store[stale_candidate] = []
    save_candidate_checkpoint(
        _storage_path(config, runtime_paths),
        CandidateCheckpoint(
            collection=stale_candidate,
            settings=replace(manager._indexing_settings, embedder_model="old-model"),
        ),
    )
    original_delete = _FakeVectorDb.delete
    attempts = 0

    def _fail_once(self: _FakeVectorDb) -> bool:
        nonlocal attempts
        if self.collection_name == stale_candidate and attempts == 0:
            attempts += 1
            return False
        return original_delete(self)

    monkeypatch.setattr(_FakeVectorDb, "delete", _fail_once)

    run = await manager._open_candidate_run()

    assert run.checkpoint.collection != stale_candidate
    assert stale_candidate not in _FakeVectorDb.store, "candidate GC did not retry the transient failure"


@pytest.mark.asyncio
async def test_incompatible_missing_candidate_is_already_deleted(tmp_path: Path) -> None:
    """A crash before candidate creation must not poison later settings changes."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    manager = _manager(config)
    missing_candidate = f"{manager._default_collection_name()}_candidate_missing"
    save_candidate_checkpoint(
        _storage_path(config, runtime_paths),
        CandidateCheckpoint(
            collection=missing_candidate,
            settings=replace(manager._indexing_settings, embedder_model="old-model"),
        ),
    )

    run = await manager._open_candidate_run()

    assert run.checkpoint.collection != missing_candidate
    assert run.resumed is False


# --------------------------------------------------------------------------
# 7-8. Source advancement and final-verification races
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_revision_advancement_reuses_unchanged_candidate_work(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Added, changed and deleted files are reconciled without redoing the rest."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "keep.md").write_text("keep me", encoding="utf-8")
    (docs_path / "change.md").write_text("old body", encoding="utf-8")
    (docs_path / "drop.md").write_text("delete me", encoding="utf-8")
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)

    # Fail one file so the pass keeps its candidate instead of publishing.
    original_index = KnowledgeManager._index_file_locked

    async def _fail_change(self: KnowledgeManager, resolved_path: Path, **kwargs: object) -> bool:
        if resolved_path.name == "change.md":
            return False
        return await original_index(self, resolved_path, **kwargs)

    KnowledgeManager._index_file_locked = _fail_change  # type: ignore[method-assign]
    try:
        await _manager(config).reindex_all()
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    candidate_collection = checkpoint.collection
    assert set(checkpoint.completed) == {"keep.md", "drop.md"}

    (docs_path / "change.md").write_text("new body", encoding="utf-8")
    (docs_path / "drop.md").unlink()
    (docs_path / "added.md").write_text("added body", encoding="utf-8")
    embedder.embedded_texts.clear()

    assert await _manager(config).reindex_all() == 2

    assert embedder.embedded_count("keep me") == 0, "unchanged work is reused"
    assert embedder.embedded_count("new body") == 1
    assert embedder.embedded_count("added body") == 1
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.collection == candidate_collection
    assert state.indexed_count == 3
    published_paths = sorted(record.metadata["source_path"] for record in _FakeVectorDb.store[candidate_collection])
    assert published_paths == ["added.md", "change.md", "keep.md"], "deleted vectors are removed"


@pytest.mark.asyncio
async def test_source_change_during_final_verification_reconciles_without_losing_work(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A change racing the completeness check triggers reconciliation, not destruction."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    manager = _manager(config)
    original_signature = knowledge_manager_module.knowledge_source_signature
    mutated = False

    def _mutate_during_verification(*args: object, **kwargs: object) -> str:
        nonlocal mutated
        if not mutated:
            mutated = True
            (docs_path / "late.md").write_text("late body", encoding="utf-8")
        return original_signature(*args, **kwargs)  # type: ignore[arg-type]

    knowledge_manager_module.knowledge_source_signature = _mutate_during_verification  # type: ignore[assignment]
    try:
        indexed = await manager.reindex_all()
    finally:
        knowledge_manager_module.knowledge_source_signature = original_signature  # type: ignore[assignment]

    assert indexed == 4
    assert manager._last_refresh_error is None
    assert embedder.embedded_count("content 0") == 1, "the racing change costs no re-embedding"
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.indexed_count == 4


# --------------------------------------------------------------------------
# 9. Concurrency
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_refreshes_share_one_candidate_and_do_not_rebuild_twice(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Overlapping refresh requests serialize onto a single candidate collection."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)

    results = await asyncio.gather(
        refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths),
        refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths),
        refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths),
    )

    assert all(result.index_published for result in results)
    assert len(_candidate_collections()) == 1, "one candidate, not one per request"
    for index in range(4):
        assert embedder.embedded_count(f"content {index}") == 1, "no duplicate full rebuild"


# --------------------------------------------------------------------------
# 10. Cancellation boundaries
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cancel_before_metadata_save", [True, False])
@pytest.mark.asyncio
async def test_cancellation_around_publication_never_produces_a_false_complete(
    tmp_path: Path,
    cancel_before_metadata_save: bool,
) -> None:
    """Cancelling at either side of the metadata write leaves consistent state."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    manager = _manager(config)
    original_save = KnowledgeManager._save_persisted_index_state

    def _cancelling_save(self: KnowledgeManager, *args: object, **kwargs: object) -> None:
        if cancel_before_metadata_save:
            msg = "metadata save failed"
            raise OSError(msg)
        original_save(self, *args, **kwargs)

    KnowledgeManager._save_persisted_index_state = _cancelling_save  # type: ignore[method-assign]
    try:
        if cancel_before_metadata_save:
            with pytest.raises(OSError, match="metadata save failed"):
                await manager.reindex_all()
        else:
            await manager.reindex_all()
    finally:
        KnowledgeManager._save_persisted_index_state = original_save  # type: ignore[method-assign]

    state = _published_state(config, runtime_paths)
    if cancel_before_metadata_save:
        assert state is None, "no published metadata without a completed write"
        checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
        assert checkpoint is not None, "candidate progress survives the failure"
        assert len(checkpoint.completed) == 2
    else:
        assert state is not None
        assert state.status == "complete"


@pytest.mark.asyncio
async def test_checkpoint_pointing_at_the_published_collection_is_never_reused(
    tmp_path: Path,
) -> None:
    """A crash between publication and cleanup must not reopen the live index."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    published_collection = _published_state(config, runtime_paths).collection
    storage_path = _storage_path(config, runtime_paths)

    manager = _manager(config)
    save_candidate_checkpoint(
        storage_path,
        CandidateCheckpoint(collection=published_collection, settings=manager._indexing_settings),
    )

    run = await manager._open_candidate_run()

    assert run.checkpoint.collection != published_collection
    assert published_collection in _FakeVectorDb.store, "the published index survives"


# --------------------------------------------------------------------------
# 11. Garbage collection
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_preserves_published_active_and_unknown_collections(
    tmp_path: Path,
) -> None:
    """GC removes proven superseded candidates and nothing whose owner is unknown."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    published_collection = _published_state(config, runtime_paths).collection
    default_collection = _manager(config)._default_collection_name()

    abandoned = f"{default_collection}_candidate_abandonedabandoned"
    unknown = "some_other_service_collection"
    _FakeVectorDb.store[abandoned] = []
    _FakeVectorDb.store[unknown] = []

    manager = _manager(config)
    run = await manager._open_candidate_run()

    assert abandoned not in _FakeVectorDb.store, "abandoned candidates are reclaimed"
    assert published_collection in _FakeVectorDb.store
    assert unknown in _FakeVectorDb.store, "unknown collections are preserved"
    assert run.checkpoint.collection in _FakeVectorDb.store


@pytest.mark.asyncio
async def test_repeated_interrupted_refreshes_keep_collection_count_bounded(
    tmp_path: Path,
) -> None:
    """Many interrupted refreshes must not accumulate candidate collections."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    original_index = KnowledgeManager._index_file_locked

    async def _always_fail(self: KnowledgeManager, resolved_path: Path, **kwargs: object) -> bool:
        _ = (self, resolved_path, kwargs)
        return False

    KnowledgeManager._index_file_locked = _always_fail  # type: ignore[method-assign]
    try:
        for _ in range(6):
            await _manager(config).reindex_all()
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

    assert len(_candidate_collections()) == 1


# --------------------------------------------------------------------------
# 12. Batching
# --------------------------------------------------------------------------


def test_embedding_batches_respect_item_and_payload_limits() -> None:
    """Batch planning bounds both request size and request payload."""
    assert plan_embedding_batches([], max_items=4, max_payload_bytes=100) == []
    assert plan_embedding_batches(["a"] * 9, max_items=4, max_payload_bytes=1000) == [
        ["a"] * 4,
        ["a"] * 4,
        ["a"],
    ]
    assert plan_embedding_batches(["aaaa", "bbbb", "cc"], max_items=10, max_payload_bytes=8) == [
        ["aaaa", "bbbb"],
        ["cc"],
    ]
    # A single oversized chunk still gets its own request rather than being split.
    assert plan_embedding_batches(["x" * 50, "y"], max_items=10, max_payload_bytes=8) == [["x" * 50], ["y"]]


@pytest.mark.asyncio
async def test_embedding_request_count_scales_with_batches_not_files(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A many-small-files corpus costs batched requests, not one request per file."""
    docs_path = tmp_path / "docs"
    file_count = 96
    _write_corpus(docs_path, file_count)
    config = _config(tmp_path, docs_path)

    assert await _manager(config).reindex_all() == file_count

    assert embedder.single_requests == [], "every chunk was served from a batch prefetch"
    assert len(embedder.batch_requests) <= 4, f"expected batched requests, got {len(embedder.batch_requests)}"
    assert sum(len(batch) for batch in embedder.batch_requests) == file_count
    assert all(len(batch) <= 64 for batch in embedder.batch_requests)


@pytest.mark.asyncio
async def test_batch_failure_falls_back_to_per_file_without_reembedding_successes(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exhausted batch retry degrades to per-file work, keeping cached vectors."""
    monkeypatch.setattr(
        knowledge_manager_module,
        "_EMBEDDING_RETRY_POLICY",
        EmbeddingRetryPolicy(max_attempts=2, initial_backoff_seconds=0.0),
    )
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    # "content 1" poisons the batch twice, exhausting the batch-level retry.
    embedder.failures["content 1"] = [EmbedderRequestError("embedder request failed (HTTP 503)") for _ in range(2)]

    assert await _manager(config).reindex_all() == 3

    assert embedder.single_requests, "the fallback path re-embedded per file"
    for index in range(3):
        assert embedder.embedded_count(f"content {index}") == 1


def test_batch_prefetch_embedder_degrades_for_providers_without_batching() -> None:
    """Providers without a batch surface keep working, one request per chunk."""
    inner = _NonBatchingEmbedder()
    adapter = BatchPrefetchEmbedder(inner=inner)

    assert adapter.supports_batching() is False
    assert adapter.embed_batch_into_cache(["one", "two"]) == 2
    assert inner.single_requests == ["one", "two"]
    # Prefetched texts are served from cache; misses still reach the provider.
    assert adapter.get_embedding_and_usage("one")[0] == [3.0, 1.0]
    assert inner.single_requests == ["one", "two"]
    adapter.clear_cache()
    assert adapter.get_embedding("one") == [3.0, 1.0]
    assert inner.single_requests == ["one", "two", "one"]


# --------------------------------------------------------------------------
# 13. Bounded scheduling
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_large_corpus_keeps_live_tasks_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live indexing tasks stay bounded no matter how large the corpus is."""
    docs_path = tmp_path / "docs"
    file_count = 400
    _write_corpus(docs_path, file_count)
    config = _config(tmp_path, docs_path)
    monkeypatch.setenv("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY", "4")

    manager = _manager(config)
    peak_tasks = 0
    original_index = KnowledgeManager._index_file_locked

    async def _observe(self: KnowledgeManager, resolved_path: Path, **kwargs: object) -> bool:
        nonlocal peak_tasks
        peak_tasks = max(peak_tasks, len(asyncio.all_tasks()))
        return await original_index(self, resolved_path, **kwargs)

    KnowledgeManager._index_file_locked = _observe  # type: ignore[method-assign]
    try:
        assert await manager.reindex_all() == file_count
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

    # One batch of tasks plus the driving task and pytest's own; nothing that
    # scales with the 400-file corpus.
    assert peak_tasks <= knowledge_manager_module._INDEX_FILES_PER_BATCH + 8, peak_tasks


# --------------------------------------------------------------------------
# 14. Status and API compatibility
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_reports_candidate_progress_separately_from_published_count(
    tmp_path: Path,
) -> None:
    """Candidate progress is visible but is never mistaken for a queryable index."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    original_index = KnowledgeManager._index_file_locked

    async def _fail_last(self: KnowledgeManager, resolved_path: Path, **kwargs: object) -> bool:
        if resolved_path.name == "doc0003.md":
            return False
        return await original_index(self, resolved_path, **kwargs)

    KnowledgeManager._index_file_locked = _fail_last  # type: ignore[method-assign]
    try:
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

    status = get_knowledge_index_status("docs", config=config, runtime_paths=runtime_paths)

    assert status.indexed_count == 0, "candidate work is not published work"
    assert status.availability is KnowledgeAvailability.REFRESH_FAILED
    assert status.candidate is not None
    assert status.candidate.completed_count == 3
    assert status.candidate.failed_count == 1
    assert status.candidate.status == "failed"


def test_status_omits_candidate_built_under_incompatible_settings(tmp_path: Path) -> None:
    """Progress from an old embedder configuration is not the current build."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    manager = _manager(config)
    save_candidate_checkpoint(
        _storage_path(config, runtime_paths),
        CandidateCheckpoint(
            collection="incompatible-candidate",
            settings=replace(manager._indexing_settings, embedder_model="different-model"),
            completed={"doc0000.md": (1, 1, "digest")},
        ),
    )

    status = get_knowledge_index_status("docs", config=config, runtime_paths=runtime_paths)

    assert status.candidate is None


@pytest.mark.asyncio
async def test_status_omits_candidate_once_the_index_is_published(tmp_path: Path) -> None:
    """A published base reports no candidate, keeping the payload backward compatible."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    status = get_knowledge_index_status("docs", config=config, runtime_paths=runtime_paths)

    assert status.candidate is None
    assert status.indexed_count == 2
    assert status.availability is KnowledgeAvailability.READY


@pytest.mark.asyncio
async def test_refresh_logs_aggregate_progress_instead_of_one_line_per_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large builds emit periodic summaries, not an INFO line per indexed file."""
    monkeypatch.setattr(knowledge_manager_module, "_PROGRESS_LOG_INTERVAL_FILES", 32)
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 128)
    config = _config(tmp_path, docs_path)

    with capture_logs() as logs:
        assert await _manager(config).reindex_all() == 128

    info_events = [entry["event"] for entry in logs if entry.get("log_level") == "info"]
    assert "Indexed knowledge file" not in info_events
    assert info_events.count("knowledge_candidate_finished") == 1
    assert 0 < info_events.count("knowledge_candidate_progress") <= 8
    summary = next(entry for entry in logs if entry["event"] == "knowledge_candidate_finished")
    assert summary["published"] is True
    assert summary["total"] == 128
    assert summary["pending"] == 0
    assert summary["resumed"] is False


# --------------------------------------------------------------------------
# 15. Vector visibility
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_checkpoint_entry_without_vectors_is_requeued(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A checkpoint claim is not trusted when the candidate cannot serve it."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "kept.md").write_text("kept body", encoding="utf-8")
    (docs_path / "lost.md").write_text("lost body", encoding="utf-8")
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    original_index = KnowledgeManager._index_file_locked

    async def _fail_third(self: KnowledgeManager, resolved_path: Path, **kwargs: object) -> bool:
        if resolved_path.name == "blocker.md":
            return False
        return await original_index(self, resolved_path, **kwargs)

    (docs_path / "blocker.md").write_text("blocker body", encoding="utf-8")
    KnowledgeManager._index_file_locked = _fail_third  # type: ignore[method-assign]
    try:
        await _manager(config).reindex_all()
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    assert {"kept.md", "lost.md"} <= set(checkpoint.completed)

    # Simulate vectors lost underneath a still-valid checkpoint claim.
    records = _FakeVectorDb.store[checkpoint.collection]
    _FakeVectorDb.store[checkpoint.collection] = [
        record for record in records if record.metadata["source_path"] != "lost.md"
    ]
    (docs_path / "blocker.md").unlink()
    embedder.embedded_texts.clear()

    assert await _manager(config).reindex_all() == 1
    assert embedder.embedded_count("lost body") == 1
    assert embedder.embedded_count("kept body") == 0
    stored = sorted(record.metadata["source_path"] for record in _FakeVectorDb.store[checkpoint.collection])
    assert stored == ["kept.md", "lost.md"]


@pytest.mark.asyncio
async def test_missing_candidate_collection_restarts_that_candidate_cleanly(
    tmp_path: Path,
) -> None:
    """A checkpoint pointing at a vanished collection rebuilds instead of publishing."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    manager = _manager(config)
    save_candidate_checkpoint(
        _storage_path(config, runtime_paths),
        CandidateCheckpoint(
            collection="mindroom_knowledge_docs_deadbeef_candidate_gone",
            settings=manager._indexing_settings,
            completed={"doc0000.md": (1, 1, "digest")},
        ),
    )

    run = await manager._open_candidate_run()

    assert run.resumed is False
    assert run.completed == {}
    assert run.vector_db.exists()


# --------------------------------------------------------------------------
# 16. Migration and checkpoint durability
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_published_metadata_without_candidate_fields_stays_queryable(
    tmp_path: Path,
) -> None:
    """A healthy pre-candidate index keeps serving and is not rebuilt from scratch."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    state = _published_state(config, runtime_paths)
    assert state is not None
    published_collection = state.collection

    # Strip every field this change introduced, leaving pre-candidate metadata.
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    write_index_metadata_payload(
        published_index_metadata_path(key),
        settings=state.settings.to_metadata(),
        status="complete",
        collection=published_collection,
        indexed_count=state.indexed_count,
        source_signature=state.source_signature,
        last_published_at=state.last_published_at,
    )
    delete_candidate_checkpoint(_storage_path(config, runtime_paths))

    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.availability is KnowledgeAvailability.READY
    assert lookup.index is not None

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert result.index_published is True
    assert _published_state(config, runtime_paths).collection == published_collection
    assert published_collection in _FakeVectorDb.store


def test_candidate_checkpoint_replays_journal_and_tolerates_a_torn_tail(tmp_path: Path) -> None:
    """Journal appends survive a crash mid-write without corrupting earlier entries."""
    storage_path = tmp_path / "state"
    settings = _manager(_config(tmp_path / "cfg", tmp_path / "cfg" / "docs"))._indexing_settings
    save_candidate_checkpoint(
        storage_path,
        CandidateCheckpoint(collection="candidate", settings=settings, completed={"a.md": (1, 2, "da")}),
    )
    append_candidate_journal(
        storage_path,
        completed=[("b.md", (3, 4, "db"))],
        failed=[("c.md", CandidateFailure(attempts=2, last_error="embedder endpoint unreachable"))],
    )
    append_candidate_journal(storage_path, removed=["a.md"])
    with _candidate_journal_path(storage_path).open("a", encoding="utf-8") as handle:
        handle.write('{"path": "torn.md", "signat')

    checkpoint = load_candidate_checkpoint(storage_path)

    assert checkpoint is not None
    assert set(checkpoint.completed) == {"b.md"}
    assert checkpoint.failed["c.md"].attempts == 2
    assert "torn.md" not in checkpoint.completed

    # Compaction folds the journal into the snapshot and removes it.
    compacted = save_candidate_checkpoint(storage_path, checkpoint)
    assert not _candidate_journal_path(storage_path).exists()
    reloaded = load_candidate_checkpoint(storage_path)
    assert reloaded is not None
    assert reloaded == compacted
    assert reloaded.completed == checkpoint.completed
    assert reloaded.failed == checkpoint.failed


def test_unknown_checkpoint_schema_version_is_ignored(tmp_path: Path) -> None:
    """Future or corrupt candidate state must never be resumed blindly."""
    storage_path = tmp_path / "state"
    storage_path.mkdir()
    _candidate_checkpoint_path(storage_path).write_text('{"schema_version": 9999}', encoding="utf-8")
    assert load_candidate_checkpoint(storage_path) is None

    _candidate_checkpoint_path(storage_path).write_text("not json", encoding="utf-8")
    assert load_candidate_checkpoint(storage_path) is None


# --------------------------------------------------------------------------
# 17. Scale regression
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scale_refresh_resumes_after_ninety_percent_and_stays_bounded(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large synthetic corpus interrupted at 90% resumes the remaining 10% only.

    This is the whole point of the change: before it, the second pass re-embedded
    every file and created a second candidate collection.
    """
    monkeypatch.setattr(knowledge_manager_module, "_PROGRESS_LOG_INTERVAL_FILES", 10_000)
    monkeypatch.setenv("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY", "1")
    docs_path = tmp_path / "docs"
    file_count = 500
    _write_corpus(docs_path, file_count)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)

    stop_after = int(file_count * 0.9)
    indexed = 0
    original_index = KnowledgeManager._index_file_locked

    async def _stop_at_ninety_percent(self: KnowledgeManager, resolved_path: Path, **kwargs: object) -> bool:
        nonlocal indexed
        if indexed >= stop_after:
            raise asyncio.CancelledError
        indexed += 1
        return await original_index(self, resolved_path, **kwargs)

    KnowledgeManager._index_file_locked = _stop_at_ninety_percent  # type: ignore[method-assign]
    try:
        with pytest.raises(asyncio.CancelledError):
            await _manager(config).reindex_all()
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    completed_after_interrupt = len(checkpoint.completed)
    assert completed_after_interrupt == stop_after
    first_pass_requests = embedder.request_count
    embedder.embedded_texts.clear()
    embedder.batch_requests.clear()
    embedder.single_requests.clear()

    assert await _manager(config).reindex_all() == file_count - completed_after_interrupt

    remaining = file_count - completed_after_interrupt
    assert len(embedder.embedded_texts) == remaining, "resume embeds only the outstanding files"
    assert embedder.request_count < first_pass_requests / 5, "resume is far cheaper than a rebuild"
    # Exactly one collection survives: the candidate that became the published
    # index. Interrupted refreshes must not accumulate collections.
    assert list(_FakeVectorDb.store) == [checkpoint.collection]
    assert load_candidate_checkpoint(_storage_path(config, runtime_paths)) is None
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.status == "complete"
    assert state.indexed_count == file_count
    assert state.collection == checkpoint.collection


@dataclass
class _BatchCounter:
    sizes: list[int] = field(default_factory=list)


@pytest.mark.asyncio
async def test_scale_refresh_issues_batched_requests_for_a_large_corpus(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Provider request count tracks chunk-count/batch-size, not file count."""
    docs_path = tmp_path / "docs"
    file_count = 512
    _write_corpus(docs_path, file_count)
    config = _config(tmp_path, docs_path)

    assert await _manager(config).reindex_all() == file_count

    assert embedder.single_requests == []
    assert embedder.request_count == pytest.approx(file_count / 64, abs=2)


@pytest.mark.asyncio
async def test_candidate_progress_reports_real_outstanding_work(
    tmp_path: Path,
) -> None:
    """Candidate ``total_files`` is the target corpus, not a completed high-water mark.

    Persisting ``max(previous_total, completed)`` made ``pending_count`` always
    zero, so an operator watching a stalled build saw "nothing left to do".
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 5)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    original_index = KnowledgeManager._index_file_locked

    async def _fail_two(self: KnowledgeManager, resolved_path: Path, **kwargs: object) -> bool:
        if resolved_path.name in {"doc0003.md", "doc0004.md"}:
            return False
        return await original_index(self, resolved_path, **kwargs)

    KnowledgeManager._index_file_locked = _fail_two  # type: ignore[method-assign]
    try:
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    assert checkpoint.total_files == 5
    status = get_knowledge_index_status("docs", config=config, runtime_paths=runtime_paths)
    assert status.candidate is not None
    assert status.candidate.total_files == 5
    assert status.candidate.completed_count == 3
    assert status.candidate.failed_count == 2
    assert status.candidate.pending_count == 2


@pytest.mark.asyncio
async def test_batch_failure_never_leaves_stragglers_writing_after_compaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One failing file must not strand its siblings past the batch that owns them.

    ``asyncio.gather`` propagates the first exception while leaving the other
    coroutines running, so a straggler could append journal entries after the
    refresh's ``finally`` had already compacted and unlinked the journal --
    silently losing files that had genuinely finished.
    """
    monkeypatch.setenv("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY", "4")
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)

    slow_started = asyncio.Event()
    slow_finished = asyncio.Event()
    original_index = KnowledgeManager._index_file_locked

    async def _fail_fast_and_stall_others(
        self: KnowledgeManager,
        resolved_path: Path,
        **kwargs: object,
    ) -> bool:
        if resolved_path.name == "doc0000.md":
            await slow_started.wait()
            msg = "explodes while siblings are still running"
            raise RuntimeError(msg)
        slow_started.set()
        await asyncio.sleep(0)
        indexed = await original_index(self, resolved_path, **kwargs)
        slow_finished.set()
        return indexed

    KnowledgeManager._index_file_locked = _fail_fast_and_stall_others  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="explodes while siblings"):
            await _manager(config).reindex_all()
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

    assert slow_finished.is_set(), "siblings must have settled before the batch returned"
    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    stored = sorted(record.metadata["source_path"] for record in _FakeVectorDb.store[checkpoint.collection])
    # Every file whose vectors landed is recorded; nothing was dropped by a
    # compaction racing an in-flight append.
    assert set(checkpoint.completed) == set(stored)
    assert set(checkpoint.completed) == {"doc0001.md", "doc0002.md", "doc0003.md"}


@pytest.mark.asyncio
async def test_compaction_decision_does_not_reread_the_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deciding whether to compact must not cost a full journal parse per batch."""
    monkeypatch.setattr(knowledge_manager_module, "_INDEX_FILES_PER_BATCH", 4)
    reads = 0
    original_read = Path.read_text

    def _counting_read_text(self: Path, *args: object, **kwargs: object) -> str:
        nonlocal reads
        if self.name.endswith(".jsonl"):
            reads += 1
        return original_read(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _counting_read_text)
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 40)
    config = _config(tmp_path, docs_path)

    assert await _manager(config).reindex_all() == 40

    # Ten batches, none of which may parse the journal just to decide.
    assert reads == 0, f"journal was re-read {reads} times while deciding whether to compact"


@pytest.mark.asyncio
async def test_unreadable_published_metadata_never_costs_the_live_collection(
    tmp_path: Path,
) -> None:
    """Candidate GC must not delete the published index when metadata is unreadable.

    A published collection is itself candidate-named, so proving which
    candidate-prefixed collections are superseded depends entirely on readable
    published metadata. Without it, reclaiming storage would delete the last
    good index.
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    published_collection = _published_state(config, runtime_paths).collection
    assert "_candidate_" in published_collection

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    published_index_metadata_path(key).write_text("{ truncated", encoding="utf-8")

    await _manager(config)._open_candidate_run()

    assert published_collection in _FakeVectorDb.store


@pytest.mark.asyncio
async def test_unreadable_published_metadata_never_reuses_live_collection_checkpoint(
    tmp_path: Path,
) -> None:
    """An unprovable checkpoint must not resume writes against the live index."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    state = _published_state(config, runtime_paths)
    assert state is not None
    published_collection = state.collection
    published_records = list(_FakeVectorDb.store[published_collection])
    storage = _storage_path(config, runtime_paths)
    save_candidate_checkpoint(
        storage,
        CandidateCheckpoint(
            collection=published_collection,
            settings=_manager(config)._indexing_settings,
        ),
    )
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    published_index_metadata_path(key).write_text("{ truncated", encoding="utf-8")

    run = await _manager(config)._open_candidate_run()

    assert run.vector_db.collection_name != published_collection
    assert _FakeVectorDb.store[published_collection] == published_records
    checkpoint = load_candidate_checkpoint(storage)
    assert checkpoint is not None
    assert checkpoint.collection == run.vector_db.collection_name


@pytest.mark.asyncio
async def test_incomplete_published_metadata_still_preserves_its_collection(
    tmp_path: Path,
) -> None:
    """A parseable-but-incomplete metadata file still names the live collection."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    state = _published_state(config, runtime_paths)
    assert state is not None
    published_collection = state.collection

    # Drop the fields the strict parser demands, keeping the collection name.
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    write_index_metadata_payload(
        published_index_metadata_path(key),
        settings=state.settings.to_metadata(),
        status="complete",
        collection=published_collection,
    )

    await _manager(config)._open_candidate_run()

    assert published_collection in _FakeVectorDb.store


@pytest.mark.asyncio
async def test_candidate_cleanup_does_not_report_the_bases_own_default_collection(
    tmp_path: Path,
) -> None:
    """Skipping the default collection is not an ownership failure.

    Candidate-only cleanup deliberately leaves the default collection alone.
    Reporting it as unprovably owned on every refresh tells operators their own
    base's collection is unrecognized.
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    manager = _manager(config)
    default_collection = manager._default_collection_name()
    _FakeVectorDb.store[default_collection] = []
    _FakeVectorDb.store["some_unrelated_collection"] = []

    with capture_logs() as logs:
        await manager._open_candidate_run()

    reported = [
        entry for entry in logs if entry["event"] == "Preserved knowledge collections with unprovable ownership"
    ]
    assert len(reported) == 1
    assert reported[0]["collections"] == ["some_unrelated_collection"]
    assert default_collection in _FakeVectorDb.store


@pytest.mark.asyncio
async def test_candidate_pending_count_is_visible_while_the_build_runs(
    tmp_path: Path,
) -> None:
    """Mid-build readers must see real outstanding work, not completed == total.

    The corpus size is only folded into the snapshot at compaction time, and
    status previously reported ``max(total_files, completed_count)``. Together
    those made ``pending_count`` read zero for the whole build.
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 6)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    observed: list[tuple[int, int]] = []
    pending_samples: list[int] = []
    original_index = KnowledgeManager._index_file_locked

    async def _observe_status(self: KnowledgeManager, resolved_path: Path, **kwargs: object) -> bool:
        indexed = await original_index(self, resolved_path, **kwargs)
        status = get_knowledge_index_status("docs", config=config, runtime_paths=runtime_paths)
        if status.candidate is not None:
            observed.append((status.candidate.total_files, status.candidate.completed_count))
            pending_samples.append(status.candidate.pending_count)
        return indexed

    KnowledgeManager._index_file_locked = _observe_status  # type: ignore[method-assign]
    try:
        assert await _manager(config).reindex_all() == 6
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

    assert observed, "status was never sampled during the build"
    assert all(total == 6 for total, _completed in observed), observed
    assert any(completed < total for total, completed in observed), (
        f"pending work was never visible mid-build: {observed}"
    )
    assert any(pending > 0 for pending in pending_samples), (
        f"pending_count never reported outstanding work: {pending_samples}"
    )
    assert pending_samples == [total - completed for total, completed in observed]


@pytest.mark.asyncio
async def test_candidate_converges_across_repeated_interruptions_and_source_changes(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Successive interrupted refreshes converge instead of restarting.

    The corpus is mutated between every attempt, which is the situation a
    large Git-backed source is always in: each pass must keep what it has,
    absorb the delta, and eventually publish the latest snapshot.
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 9)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    original_index = KnowledgeManager._index_file_locked
    blocked = {"doc0004.md", "doc0007.md"}

    async def _fail_blocked(self: KnowledgeManager, resolved_path: Path, **kwargs: object) -> bool:
        if resolved_path.name in blocked:
            return False
        return await original_index(self, resolved_path, **kwargs)

    KnowledgeManager._index_file_locked = _fail_blocked  # type: ignore[method-assign]
    try:
        # Attempt 1: two files cannot be indexed, so nothing publishes.
        await _manager(config).reindex_all()
        first = load_candidate_checkpoint(_storage_path(config, runtime_paths))
        assert first is not None
        assert set(first.failed) == blocked
        candidate_collection = first.collection

        # Attempt 2: still blocked, and the source moves underneath it.
        (docs_path / "doc0000.md").write_text("rewritten body", encoding="utf-8")
        (docs_path / "doc0001.md").unlink()
        (docs_path / "extra.md").write_text("added between attempts", encoding="utf-8")
        await _manager(config).reindex_all()
        second = load_candidate_checkpoint(_storage_path(config, runtime_paths))
        assert second is not None
        assert second.collection == candidate_collection, "each attempt continues the same candidate"
        assert "doc0001.md" not in second.completed
        assert second.completed["doc0000.md"] != first.completed["doc0000.md"], "changed file was re-indexed"
        assert "extra.md" in second.completed
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

    # Attempt 3: the blocker clears and the accumulated candidate publishes.
    embedder.embedded_texts.clear()
    blocked.clear()
    await _manager(config).reindex_all()

    assert embedder.embedded_count("rewritten body") == 0, "work kept across attempts is not redone"
    assert embedder.embedded_count("added between attempts") == 0
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.status == "complete"
    assert state.collection == candidate_collection
    assert state.indexed_count == 9
    stored = sorted(record.metadata["source_path"] for record in _FakeVectorDb.store[candidate_collection])
    assert stored == sorted(
        [f"doc{index:04d}.md" for index in range(9) if index != 1] + ["extra.md"],
    )
    assert load_candidate_checkpoint(_storage_path(config, runtime_paths)) is None


@pytest.mark.asyncio
async def test_short_batch_response_falls_back_to_per_item_and_publishes(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A backend that accepts array input but returns fewer vectors must not stall the build.

    Some OpenAI-compatible servers accept a multi-input embeddings request and
    answer with a single vector. Treating that as a permanent failure kills the
    candidate on its first batch, so the base can never index anything.
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 5)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    embedder.short_batch = True

    assert await _manager(config).reindex_all() == 5

    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.status == "complete"
    assert state.indexed_count == 5
    for index in range(5):
        assert embedder.embedded_count(f"content {index}") >= 1


@pytest.mark.asyncio
async def test_batch_capability_failure_disables_batching_for_the_rest_of_the_run(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One failed capability probe must not be repeated for every later batch."""
    monkeypatch.setattr(knowledge_manager_module, "_INDEX_FILES_PER_BATCH", 4)
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 16)
    config = _config(tmp_path, docs_path)
    embedder.short_batch = True

    assert await _manager(config).reindex_all() == 16

    multi_input_requests = [batch for batch in embedder.batch_requests if len(batch) > 1]
    assert len(multi_input_requests) == 1, (
        f"batch support was probed {len(multi_input_requests)} times: {multi_input_requests}"
    )


@pytest.mark.asyncio
async def test_ordinary_permanent_batch_error_fails_fast_without_a_request_storm(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Only a cardinality fault earns a fallback; a bad request must not be retried per chunk.

    A rejected model or malformed request fails identically one input at a
    time, so degrading to per-item would turn one clear error into one failed
    request per chunk.
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 40)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    embedder.batch_error = EmbedderRequestError("embedder request failed (HTTP 400)")

    manager = _manager(config)
    await manager.reindex_all()

    assert manager._last_refresh_error is not None
    assert "embedder request failed (HTTP 400)" in manager._last_refresh_error
    assert _published_state(config, runtime_paths) is None
    assert embedder.request_count <= 4, f"degraded into a request storm: {embedder.request_count}"


@pytest.mark.asyncio
async def test_unknown_model_batch_error_fails_fast(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A bad model is global: it must not be probed once per chunk."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 30)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    embedder.batch_error = EmbedderRequestError("embedder request failed (HTTP 404)")

    manager = _manager(config)
    await manager.reindex_all()

    assert _published_state(config, runtime_paths) is None
    assert embedder.request_count <= 4, f"degraded into a request storm: {embedder.request_count}"
    assert load_candidate_checkpoint(_storage_path(config, runtime_paths)) is not None


@pytest.mark.asyncio
async def test_single_input_wrong_cardinality_still_fails_and_does_not_publish(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed single-input response is never excused as a batching quirk."""
    monkeypatch.setattr(knowledge_manager_module, "_INDEX_FILES_PER_BATCH", 1)
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    embedder.failures["content 0"] = [
        EmbedderRequestError("embedder returned 0 embeddings for 1 inputs") for _ in range(10)
    ]

    manager = _manager(config)
    await manager.reindex_all()

    assert manager._last_refresh_error is not None
    assert "Indexed 1 of 2" in manager._last_refresh_error
    assert _published_state(config, runtime_paths) is None
    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    assert set(checkpoint.failed) == {"doc0000.md"}


def test_per_item_fallback_preserves_order_and_validates_dimensions() -> None:
    """Fallback keeps input order and refuses vectors of inconsistent width."""
    inner = _RecordingEmbedder()
    inner.short_batch = True
    adapter = BatchPrefetchEmbedder(inner=inner)

    assert adapter.embed_batch_into_cache(["alpha", "bb", "c"]) == 3
    assert adapter.supports_batching() is False, "a failed probe retires batching for the run"
    # Order preserved: each text maps to its own vector, keyed by content.
    assert adapter.get_embedding("alpha") == [5.0, 1.0]
    assert adapter.get_embedding("bb") == [2.0, 1.0]
    assert adapter.get_embedding("c") == [1.0, 1.0]

    widening = _RecordingEmbedder()
    widening.supports_batch = False
    adapter = BatchPrefetchEmbedder(inner=widening)
    assert adapter.embed_batch_into_cache(["one"]) == 1
    original_get = widening.get_embedding
    widening.get_embedding = lambda text: [*original_get(text), 9.0]  # type: ignore[method-assign]
    # Validation guards the path Agno's writer uses, so a widened vector cannot
    # reach the collection even though prefetch treats its own faults as best effort.
    with pytest.raises(EmbedderRequestError, match="3-dimension vector, expected 2"):
        adapter.get_embedding("two")


def test_empty_vector_is_rejected_by_the_batch_adapter() -> None:
    """An empty vector is never cached, whatever path produced it."""
    inner = _RecordingEmbedder()
    inner.supports_batch = False
    inner.get_embedding = lambda _text: []  # type: ignore[method-assign]
    adapter = BatchPrefetchEmbedder(inner=inner)

    assert adapter.embed_batch_into_cache(["anything"]) == 0, "prefetch leaves it to the per-file path"
    with pytest.raises(EmbedderRequestError, match="empty vector"):
        adapter.get_embedding("anything")


@pytest.mark.asyncio
async def test_permanent_per_item_failure_after_fallback_is_recorded_per_file(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A permanent fault affecting one chunk fails that file only, keeping the rest."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    embedder.short_batch = True
    embedder.failures["content 2"] = [EmbedderRequestError("embedder request failed (HTTP 422)") for _ in range(10)]

    manager = _manager(config)
    await manager.reindex_all()

    assert manager._last_refresh_error is not None
    assert "Indexed 3 of 4" in manager._last_refresh_error
    assert "embedder request failed (HTTP 422)" in manager._last_refresh_error
    assert _published_state(config, runtime_paths) is None, "an incomplete snapshot must not publish"
    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    assert set(checkpoint.failed) == {"doc0002.md"}
    assert set(checkpoint.completed) == {"doc0000.md", "doc0001.md", "doc0003.md"}


@pytest.mark.asyncio
async def test_credential_rejection_during_fallback_still_fails_fast(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A global credential failure aborts instead of issuing one doomed request per chunk."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 40)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    embedder.short_batch = True
    embedder.fail_everything = EmbedderRequestError("embedder authentication failed (HTTP 401)")

    manager = _manager(config)
    await manager.reindex_all()

    assert manager._last_refresh_error is not None
    assert "embedder authentication failed (HTTP 401)" in manager._last_refresh_error
    assert _published_state(config, runtime_paths) is None
    assert embedder.request_count <= 4, f"kept probing a rejected credential: {embedder.request_count}"
    assert load_candidate_checkpoint(_storage_path(config, runtime_paths)) is not None


@pytest.mark.asyncio
async def test_restart_during_batch_fallback_resumes_the_same_candidate(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Fallback mode does not change resume semantics."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 6)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    embedder.short_batch = True
    original_index = KnowledgeManager._index_file_locked

    async def _fail_one(self: KnowledgeManager, resolved_path: Path, **kwargs: object) -> bool:
        if resolved_path.name == "doc0005.md":
            return False
        return await original_index(self, resolved_path, **kwargs)

    KnowledgeManager._index_file_locked = _fail_one  # type: ignore[method-assign]
    try:
        await _manager(config).reindex_all()
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    candidate_collection = checkpoint.collection
    assert len(checkpoint.completed) == 5
    embedder.embedded_texts.clear()

    assert await _manager(config).reindex_all() == 1

    for index in range(5):
        assert embedder.embedded_count(f"content {index}") == 0, "checkpointed files were re-embedded"
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.collection == candidate_collection
    assert state.indexed_count == 6


@pytest.mark.asyncio
async def test_config_mismatched_index_stays_unavailable_until_the_candidate_publishes(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Search must not run against vectors built under incompatible settings."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    changed_config = config.model_copy(deep=True)
    changed_config.memory.embedder.config.model = "text-embedding-3-large"

    lookup = get_published_index("docs", config=changed_config, runtime_paths=runtime_paths)
    assert lookup.availability is KnowledgeAvailability.CONFIG_MISMATCH
    assert lookup.index is None, "incompatible vectors must not be queryable"
    resolution = resolve_agent_knowledge_access("helper", changed_config, runtime_paths)
    assert resolution.knowledge is None

    embedder.short_batch = True
    result = await refresh_knowledge_binding("docs", config=changed_config, runtime_paths=runtime_paths)

    assert result.index_published is True
    reopened = get_published_index("docs", config=changed_config, runtime_paths=runtime_paths)
    assert reopened.availability is KnowledgeAvailability.READY
    assert reopened.index is not None


@pytest.mark.asyncio
async def test_progress_and_resumable_state_stay_accurate_under_batch_fallback(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Falling back to per-item must not distort progress or resumability.

    Request accounting, completed/remaining/failed counts and the resumable
    checkpoint all have to keep telling the truth once batching is retired.
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 8)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    embedder.short_batch = True
    original_index = KnowledgeManager._index_file_locked

    async def _fail_two(self: KnowledgeManager, resolved_path: Path, **kwargs: object) -> bool:
        if resolved_path.name in {"doc0006.md", "doc0007.md"}:
            return False
        return await original_index(self, resolved_path, **kwargs)

    KnowledgeManager._index_file_locked = _fail_two  # type: ignore[method-assign]
    try:
        with capture_logs() as logs:
            await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

    status = get_knowledge_index_status("docs", config=config, runtime_paths=runtime_paths)
    assert status.indexed_count == 0, "nothing published, so nothing is queryable"
    assert status.candidate is not None
    assert status.candidate.total_files == 8
    assert status.candidate.completed_count == 6
    assert status.candidate.pending_count == 2
    assert status.candidate.failed_count == 2
    assert status.candidate.status == "failed"

    summary = next(entry for entry in logs if entry["event"] == "knowledge_candidate_finished")
    assert summary["published"] is False
    assert summary["total"] == 8
    assert summary["completed"] == 6
    assert summary["pending"] == 2
    assert summary["failed"] == 2
    # One multi-input probe, then one request per remaining chunk.
    assert [len(batch) for batch in embedder.batch_requests if len(batch) > 1] == [8]
    assert embedder.single_requests

    # The recorded state is genuinely resumable: clearing the fault publishes
    # without redoing any of the six files already completed.
    embedder.embedded_texts.clear()
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert result.index_published is True
    for index in range(6):
        assert embedder.embedded_count(f"content {index}") == 0
    assert get_knowledge_index_status("docs", config=config, runtime_paths=runtime_paths).indexed_count == 8


@pytest.mark.asyncio
async def test_vectors_prefetched_before_a_later_fallback_are_still_reused(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capability failure in a later batch must not discard earlier batched work."""
    monkeypatch.setattr(knowledge_manager_module, "_INDEX_FILES_PER_BATCH", 4)
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 12)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    original_batch = _RecordingEmbedder.get_embeddings_batch
    calls = {"count": 0}

    def _short_after_first_batch(self: _RecordingEmbedder, texts: list[str]) -> list[list[float]]:
        calls["count"] += 1
        # First batch succeeds normally; the second reveals the broken capability.
        self.short_batch = calls["count"] > 1
        return original_batch(self, texts)

    monkeypatch.setattr(_RecordingEmbedder, "get_embeddings_batch", _short_after_first_batch)

    assert await _manager(config).reindex_all() == 12

    multi_input = [batch for batch in embedder.batch_requests if len(batch) > 1]
    assert len(multi_input) == 2, "batching stopped after the batch that proved it broken"
    # The first batch succeeded, so its vectors are served from cache and are
    # never requested again.
    for index in range(4):
        assert embedder.embedded_count(f"content {index}") == 1, "an earlier successful batch was redone"
    # The short batch's own inputs are re-embedded once each, because a partial
    # response gives no safe mapping from vectors back to inputs.
    for index in range(4, 12):
        assert embedder.embedded_count(f"content {index}") <= 2
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.indexed_count == 12


@pytest.mark.asyncio
async def test_checkpoint_naming_a_published_collection_is_rejected_even_if_metadata_is_incomplete(
    tmp_path: Path,
) -> None:
    """The published collection must never be reopened as a candidate.

    The guard compared only the strictly parsed collection name. Metadata that
    is parseable but missing a required field makes that name null while the
    raw payload still names the live collection, so a surviving checkpoint
    could reopen the published collection and write candidate reconciliation
    into it.
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    state = _published_state(config, runtime_paths)
    assert state is not None
    published_collection = state.collection
    assert published_collection is not None

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    write_index_metadata_payload(
        published_index_metadata_path(key),
        settings=state.settings.to_metadata(),
        status="complete",
        collection=published_collection,
    )
    manager = _manager(config)
    save_candidate_checkpoint(
        _storage_path(config, runtime_paths),
        CandidateCheckpoint(collection=published_collection, settings=manager._indexing_settings),
    )

    run = await manager._open_candidate_run()

    assert run.checkpoint.collection != published_collection
    assert run.resumed is False
    assert published_collection in _FakeVectorDb.store


@pytest.mark.asyncio
async def test_incompatible_checkpoint_never_deletes_a_published_collection(
    tmp_path: Path,
) -> None:
    """Discarding an incompatible candidate must not take the live index with it."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    state = _published_state(config, runtime_paths)
    assert state is not None
    published_collection = state.collection
    assert published_collection is not None

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    write_index_metadata_payload(
        published_index_metadata_path(key),
        settings=state.settings.to_metadata(),
        status="complete",
        collection=published_collection,
    )
    # A checkpoint naming the published collection, recorded under settings the
    # current runtime no longer matches.
    stale_settings = replace(_manager(config)._indexing_settings, embedder_model="text-embedding-3-large")
    save_candidate_checkpoint(
        _storage_path(config, runtime_paths),
        CandidateCheckpoint(collection=published_collection, settings=stale_settings),
    )

    run = await _manager(config)._open_candidate_run()

    assert published_collection in _FakeVectorDb.store, "the last good collection was deleted"
    assert run.checkpoint.collection != published_collection


@pytest.mark.asyncio
async def test_mtime_only_change_keeps_completed_vectors(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A checkout that rewrites mtimes must not destroy completed work.

    The checkpoint's whole premise is that the content digest, not the mtime,
    decides whether a file still counts as indexed. Comparing the full
    signature deleted and re-embedded every byte-identical file after any
    mtime-rewriting checkout, archive restore, or clone.
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 5)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    original_index = KnowledgeManager._index_file_locked

    async def _fail_last(self: KnowledgeManager, resolved_path: Path, **kwargs: object) -> bool:
        if resolved_path.name == "doc0004.md":
            return False
        return await original_index(self, resolved_path, **kwargs)

    KnowledgeManager._index_file_locked = _fail_last  # type: ignore[method-assign]
    try:
        await _manager(config).reindex_all()
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    candidate_collection = checkpoint.collection
    assert len(checkpoint.completed) == 4
    vectors_before = len(_FakeVectorDb.store[candidate_collection])

    # Same bytes, new mtimes: exactly what git checkout does.
    for index in range(5):
        path = docs_path / f"doc{index:04d}.md"
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns + 10**9, stat.st_mtime_ns + 10**9))
    embedder.embedded_texts.clear()

    assert await _manager(config).reindex_all() == 1, "only the previously failed file is indexed"

    for index in range(4):
        assert embedder.embedded_count(f"content {index}") == 0, "an mtime change re-embedded unchanged content"
    assert len(_FakeVectorDb.store[candidate_collection]) == vectors_before + 1
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.status == "complete"
    assert state.collection == candidate_collection


@pytest.mark.asyncio
async def test_globally_failing_embedder_stops_a_non_batching_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected credential must stop the pass even with no batch surface.

    Providers without ``get_embeddings_batch`` skip prefetch entirely, so the
    batch-path stop never runs and every remaining file issued the same doomed
    request.
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 60)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    embedder = _use_non_batching_embedder(monkeypatch)
    embedder.fail_everything = EmbedderRequestError("embedder authentication failed (HTTP 401)")

    manager = _manager(config)
    await manager.reindex_all()

    assert manager._last_refresh_error is not None
    assert "embedder authentication failed (HTTP 401)" in manager._last_refresh_error
    assert _published_state(config, runtime_paths) is None
    assert embedder.request_count <= 4, f"issued one doomed request per file: {embedder.request_count}"
    assert load_candidate_checkpoint(_storage_path(config, runtime_paths)) is not None


@pytest.mark.asyncio
async def test_repeated_non_auth_rejections_stop_the_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid model rejects everything; it must not be retried per file."""
    monkeypatch.setattr(knowledge_manager_module, "_GLOBAL_EMBEDDER_FAILURE_STREAK", 5)
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 60)
    config = _config(tmp_path, docs_path)
    embedder = _use_non_batching_embedder(monkeypatch)
    embedder.fail_everything = EmbedderRequestError("embedder request failed (HTTP 404)")

    manager = _manager(config)
    await manager.reindex_all()

    assert embedder.request_count <= 12, f"issued one doomed request per file: {embedder.request_count}"
    assert manager._global_embedder_failure is not None


def test_unrelated_failure_does_not_reset_embedder_rejection_streak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a successful embedding proves that repeated provider failures are not global."""
    monkeypatch.setattr(knowledge_manager_module, "_GLOBAL_EMBEDDER_FAILURE_STREAK", 2)
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 1)
    manager = _manager(_config(tmp_path, docs_path))
    rejection = "embedder request failed (HTTP 404)"

    manager._record_embedder_rejection(rejection)
    manager._record_embedder_rejection(None)
    manager._record_embedder_rejection(rejection)

    assert manager._global_embedder_failure == rejection


@pytest.mark.asyncio
async def test_a_few_bad_files_do_not_stop_an_otherwise_healthy_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The global-failure stop must not fire for isolated per-file rejections."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 12)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    embedder = _use_non_batching_embedder(monkeypatch)
    for index in (3, 7):
        embedder.failures[f"content {index}"] = [
            EmbedderRequestError("embedder request failed (HTTP 422)") for _ in range(5)
        ]

    manager = _manager(config)
    await manager.reindex_all()

    assert manager._global_embedder_failure is None
    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    assert set(checkpoint.failed) == {"doc0003.md", "doc0007.md"}
    assert len(checkpoint.completed) == 10


@pytest.mark.asyncio
async def test_candidate_path_removal_is_batched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping many paths must not cost one vector-store round trip each."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 40)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    original_index = KnowledgeManager._index_file_locked

    async def _fail_last(self: KnowledgeManager, resolved_path: Path, **kwargs: object) -> bool:
        if resolved_path.name == "doc0039.md":
            return False
        return await original_index(self, resolved_path, **kwargs)

    KnowledgeManager._index_file_locked = _fail_last  # type: ignore[method-assign]
    try:
        await _manager(config).reindex_all()
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

    # Rewrite every file's content so reconciliation must drop all 39 entries.
    for index in range(39):
        (docs_path / f"doc{index:04d}.md").write_text(f"rewritten {index}", encoding="utf-8")

    # Count every vector-store deletion route, so a per-path loop through
    # remove_vectors_by_metadata is caught as readily as a per-path $in call.
    deletes = {"count": 0}
    original_delete = _FakeCollection.delete
    original_remove = _FakeKnowledge.remove_vectors_by_metadata

    def _counting_delete(self: _FakeCollection, *, where: dict[str, object]) -> None:
        deletes["count"] += 1
        original_delete(self, where=where)

    def _counting_remove(self: _FakeKnowledge, metadata: dict[str, Any]) -> bool:
        deletes["count"] += 1
        return original_remove(self, metadata)

    monkeypatch.setattr(_FakeCollection, "delete", _counting_delete)
    monkeypatch.setattr(_FakeKnowledge, "remove_vectors_by_metadata", _counting_remove)

    assert await _manager(config).reindex_all() == 40

    # 39 dropped paths must not cost 39 round trips; the upsert path still
    # clears each file it rewrites, so allow one per re-indexed file plus a
    # small number of batched deletes.
    assert deletes["count"] <= 45, f"one delete per dropped path instead of batched: {deletes['count']}"
    assert _published_state(config, runtime_paths) is not None


@pytest.mark.asyncio
async def test_journal_compaction_bound_survives_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed run inherits the journal it replayed, so it cannot grow forever."""
    monkeypatch.setattr(knowledge_manager_module, "_CANDIDATE_JOURNAL_COMPACT_ENTRIES", 10)
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 8)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    storage = _storage_path(config, runtime_paths)
    original_index = KnowledgeManager._index_file_locked

    async def _fail_last(self: KnowledgeManager, resolved_path: Path, **kwargs: object) -> bool:
        if resolved_path.name == "doc0007.md":
            return False
        return await original_index(self, resolved_path, **kwargs)

    KnowledgeManager._index_file_locked = _fail_last  # type: ignore[method-assign]
    try:
        await _manager(config).reindex_all()
        # Simulate a hard kill: journal entries exist with no compaction.
        ghost_entries = [(f"ghost{index}.md", (index, 1, f"digest-{index}")) for index in range(9)]
        append_candidate_journal(storage, completed=ghost_entries)

        checkpoint = load_candidate_checkpoint(storage)
        assert checkpoint is not None
        assert checkpoint.replayed_journal_entries == 9

        manager = _manager(config)
        run = await manager._open_candidate_run()
        assert run.journal_appends == 9, "a resumed run restarted the compaction count"
        final_path = docs_path / "doc0007.md"
        assert await original_index(
            manager,
            final_path,
            upsert=True,
            knowledge=run.knowledge,
            indexed_files=None,
            indexed_signatures=run.completed,
        )
        await manager._persist_candidate_batch(run, (final_path,))
        assert run.journal_appends == 10

        await manager._compact_candidate_checkpoint(run)

        assert not _candidate_journal_path(storage).exists(), "threshold-crossing write did not compact the journal"
        reloaded = load_candidate_checkpoint(storage)
        assert reloaded is not None
        assert "doc0007.md" in reloaded.completed
        assert {path for path, _signature in ghost_entries} <= set(reloaded.completed)
        assert reloaded.replayed_journal_entries == 0
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_unchanged_publish_discards_an_orphaned_candidate(
    tmp_path: Path,
) -> None:
    """An interrupted forced rebuild must not leave candidate state behind forever.

    When the next scheduled refresh finds the source unchanged it republishes
    the existing index and returns before the candidate is ever opened, so no
    cleanup path was reachable.
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    published_collection = _published_state(config, runtime_paths).collection

    # An interrupted forced rebuild: candidate state on disk, source unchanged.
    orphan = f"{published_collection}_orphan"
    _FakeVectorDb.store[orphan] = []
    save_candidate_checkpoint(
        _storage_path(config, runtime_paths),
        CandidateCheckpoint(
            collection=orphan,
            settings=_manager(config)._indexing_settings,
            completed={"doc0000.md": (1, 1, "digest")},
        ),
    )

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert load_candidate_checkpoint(_storage_path(config, runtime_paths)) is None, "orphan checkpoint survived"
    assert orphan not in _FakeVectorDb.store, "orphan collection survived"
    assert published_collection in _FakeVectorDb.store


@pytest.mark.asyncio
async def test_unchanged_publish_retries_orphan_cleanup_after_delete_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient collection-delete failure must retain the checkpoint for retry."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    published_collection = _published_state(config, runtime_paths).collection
    orphan = f"{published_collection}_orphan"
    _FakeVectorDb.store[orphan] = []
    storage = _storage_path(config, runtime_paths)
    save_candidate_checkpoint(
        storage,
        CandidateCheckpoint(
            collection=orphan,
            settings=_manager(config)._indexing_settings,
            completed={"doc0000.md": (1, 1, "digest")},
        ),
    )
    original_delete = _FakeVectorDb.delete
    attempts = 0

    def _fail_once(self: _FakeVectorDb) -> bool:
        nonlocal attempts
        if self.collection_name == orphan and attempts == 0:
            attempts += 1
            # How Agno actually reports a failed delete: it swallows the
            # provider error and returns False rather than raising.
            return False
        return original_delete(self)

    monkeypatch.setattr(_FakeVectorDb, "delete", _fail_once)

    first = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert first.index_published is True
    assert load_candidate_checkpoint(storage) is not None
    assert orphan in _FakeVectorDb.store

    second = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert second.index_published is True
    assert load_candidate_checkpoint(storage) is None
    assert orphan not in _FakeVectorDb.store


@pytest.mark.asyncio
async def test_unchanged_publish_stays_ready_when_checkpoint_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort candidate cleanup cannot undo an already-published refresh."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    state = _published_state(config, runtime_paths)
    assert state is not None
    orphan = f"{state.collection}_orphan"
    _FakeVectorDb.store[orphan] = []
    storage = _storage_path(config, runtime_paths)
    save_candidate_checkpoint(
        storage,
        CandidateCheckpoint(collection=orphan, settings=_manager(config)._indexing_settings),
    )

    def _fail_checkpoint_delete(_storage_path: Path) -> None:
        message = "checkpoint directory is read-only"
        raise OSError(message)

    monkeypatch.setattr(knowledge_manager_module, "delete_candidate_checkpoint", _fail_checkpoint_delete)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert result.availability is KnowledgeAvailability.READY
    assert result.last_error is None
    refreshed_state = _published_state(config, runtime_paths)
    assert refreshed_state is not None
    assert refreshed_state.refresh_job == "idle"
    assert refreshed_state.last_error is None
    assert load_candidate_checkpoint(storage) is not None, "failed cleanup unexpectedly removed the checkpoint"


def test_prefetch_text_is_bounded_by_bytes_not_only_file_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Peak prefetch memory must not scale with how large the batch's files are."""
    monkeypatch.setattr(knowledge_manager_module, "_MAX_PREFETCH_TEXT_BYTES", 4_000)
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    files = []
    for index in range(20):
        path = docs_path / f"big{index}.md"
        path.write_text("x" * 2_000, encoding="utf-8")
        files.append(path)
    config = _config(tmp_path, docs_path, chunk_size=100_000)

    texts = _manager(config)._chunk_texts_for_batch(files)

    total_bytes = sum(len(text.encode("utf-8")) for text in texts)
    assert texts, "prefetch produced nothing at all"
    assert total_bytes <= 4_000 + 2_000, f"prefetch held {total_bytes} bytes past its budget"
    assert len(texts) < len(files), "every file was read despite the byte budget"


def test_single_oversized_file_is_never_read_into_the_prefetch_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One huge file must not blow the prefetch bound on its own.

    Chunking materializes a file's whole content, so checking the budget after
    reading cannot bound anything: a single oversized document exceeds it by
    however large it happens to be.
    """
    monkeypatch.setattr(knowledge_manager_module, "_MAX_PREFETCH_TEXT_BYTES", 4_000)
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    huge = docs_path / "huge.md"
    huge.write_text("y" * 200_000, encoding="utf-8")
    small = [docs_path / f"small{index}.md" for index in range(3)]
    for path in small:
        path.write_text("z" * 500, encoding="utf-8")
    config = _config(tmp_path, docs_path, chunk_size=100_000)
    read_files: list[str] = []
    original_chunk = KnowledgeManager._chunk_texts_for_prefetch

    def _record_read(self: KnowledgeManager, resolved_path: Path) -> tuple[str, ...]:
        read_files.append(resolved_path.name)
        return original_chunk(self, resolved_path)

    monkeypatch.setattr(KnowledgeManager, "_chunk_texts_for_prefetch", _record_read)

    texts = _manager(config)._chunk_texts_for_batch([huge, *small])

    assert "huge.md" not in read_files, "the oversized file was read despite exceeding the budget"
    total_bytes = sum(len(text.encode("utf-8")) for text in texts)
    assert total_bytes <= 4_000, f"prefetch held {total_bytes} bytes past its budget"
    assert texts, "smaller files behind the oversized one were skipped too"


def test_prefetch_skips_overlap_that_can_expand_past_the_byte_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Near-total overlap must not amplify one small source into unbounded text."""
    monkeypatch.setattr(knowledge_manager_module, "_MAX_PREFETCH_TEXT_BYTES", 4_000)
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    source = docs_path / "overlap.md"
    source.write_text("x" * 4_000, encoding="utf-8")
    config = _config(tmp_path, docs_path, chunk_size=128, chunk_overlap=127)
    manager = _manager(config)
    read_files: list[str] = []
    original_chunk = KnowledgeManager._chunk_texts_for_prefetch

    def _record_read(self: KnowledgeManager, resolved_path: Path) -> tuple[str, ...]:
        read_files.append(resolved_path.name)
        return original_chunk(self, resolved_path)

    monkeypatch.setattr(KnowledgeManager, "_chunk_texts_for_prefetch", _record_read)

    texts = manager._chunk_texts_for_batch([source])

    assert texts == []
    assert read_files == [], "overlapping source was materialized before prefetch declined it"


@pytest.mark.asyncio
async def test_oversized_file_still_indexes_and_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipping a file for prefetch must never skip indexing it."""
    monkeypatch.setattr(knowledge_manager_module, "_MAX_PREFETCH_TEXT_BYTES", 1_000)
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "huge.md").write_text("y" * 50_000, encoding="utf-8")
    for index in range(3):
        (docs_path / f"small{index}.md").write_text(f"small body {index}", encoding="utf-8")
    config = _config(tmp_path, docs_path, chunk_size=100_000)
    runtime_paths = runtime_paths_for(config)

    assert await _manager(config).reindex_all() == 4

    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.status == "complete"
    assert state.indexed_count == 4
    stored = sorted(record.metadata["source_path"] for record in _FakeVectorDb.store[state.collection])
    assert "huge.md" in stored


def test_moderate_overlap_is_still_batch_prefetched(tmp_path: Path) -> None:
    """Ordinary overlap expands predictably, so it must not disable prefetch."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    source = docs_path / "overlapped.md"
    source.write_text(_overlapping_body(500), encoding="utf-8")
    config = _config(tmp_path, docs_path, chunk_size=1_000, chunk_overlap=100)

    texts = _manager(config)._chunk_texts_for_batch([source])

    assert len(texts) > 1, "overlapping chunks were not prefetched at all"


def test_prefetch_skips_overlap_expansion_that_outgrows_the_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admission is decided by worst-case expansion, not by the size on disk.

    The oversized file here is smaller than the whole budget; only its overlap
    expansion is not, so a check against the raw file size would read it.
    """
    monkeypatch.setattr(knowledge_manager_module, "_MAX_PREFETCH_TEXT_BYTES", 4_000)
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    expanding = docs_path / "expanding.md"
    expanding.write_text("x" * 2_500, encoding="utf-8")
    small = docs_path / "small.md"
    small.write_text(_overlapping_body(50), encoding="utf-8")
    config = _config(tmp_path, docs_path, chunk_size=1_000, chunk_overlap=100)
    read_files: list[str] = []
    original_chunk = KnowledgeManager._chunk_texts_for_prefetch

    def _record_read(self: KnowledgeManager, resolved_path: Path) -> tuple[str, ...]:
        read_files.append(resolved_path.name)
        return original_chunk(self, resolved_path)

    monkeypatch.setattr(KnowledgeManager, "_chunk_texts_for_prefetch", _record_read)

    texts = _manager(config)._chunk_texts_for_batch([expanding, small])

    assert expanding.stat().st_size < 4_000, "the oversized file no longer fits the budget by raw size"
    assert read_files == ["small.md"], "the expanding file was materialized before prefetch declined it"
    assert texts, "the smaller file behind the skipped one was never prefetched"
    assert sum(len(text.encode("utf-8")) for text in texts) <= 4_000


@pytest.mark.asyncio
async def test_overlapping_chunks_are_batch_prefetched_and_published(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Overlapping chunks embed in real multi-input requests and stay searchable."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "overlapped.md").write_text(_overlapping_body(500), encoding="utf-8")
    config = _config(tmp_path, docs_path, chunk_size=1_000, chunk_overlap=100)
    runtime_paths = runtime_paths_for(config)

    assert await _manager(config).reindex_all() == 1

    assert [batch for batch in embedder.batch_requests if len(batch) > 1], "no multi-input request was issued"
    assert embedder.single_requests == [], "every overlapping chunk was served from the batch prefetch"
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.status == "complete"
    assert state.indexed_count == 1
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.index is not None
    hits = lookup.index.knowledge.search("token0000", max_results=5)
    assert hits, "the published overlapping index returned nothing"
    assert all("token" in document.content for document in hits)


@pytest.mark.asyncio
async def test_file_skipped_by_overlap_expansion_still_indexes_and_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file prefetch declines must still be indexed by the per-file path."""
    monkeypatch.setattr(knowledge_manager_module, "_MAX_PREFETCH_TEXT_BYTES", 4_000)
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "expanding.md").write_text("x" * 2_500, encoding="utf-8")
    for index in range(3):
        (docs_path / f"small{index}.md").write_text(f"small body {index}", encoding="utf-8")
    config = _config(tmp_path, docs_path, chunk_size=1_000, chunk_overlap=100)
    runtime_paths = runtime_paths_for(config)

    assert await _manager(config).reindex_all() == 4

    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.status == "complete"
    assert state.indexed_count == 4
    stored = sorted(record.metadata["source_path"] for record in _FakeVectorDb.store[state.collection])
    assert "expanding.md" in stored


# --------------------------------------------------------------------------
# Vector verification against a store with a bind-variable ceiling
# --------------------------------------------------------------------------


def _seed_chunked_paths(collection: str, paths: Sequence[str], chunks_per_path: int) -> None:
    """Fill one collection with `chunks_per_path` vectors for each of `paths`."""
    _FakeVectorDb.store[collection] = [
        _Record(content=f"{relative_path} chunk {index}", embedding=[0.0], metadata={"source_path": relative_path})
        for relative_path in paths
        for index in range(chunks_per_path)
    ]


def _verification_manager(tmp_path: Path) -> tuple[KnowledgeManager, _FakeVectorDb]:
    """Return a manager and a created, empty candidate collection to verify against."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    manager = _manager(_config(tmp_path, docs_path))
    vector_db = _FakeVectorDb(collection="verification_target")
    vector_db.create()
    return manager, vector_db


def test_vector_verification_splits_a_batch_the_store_cannot_answer(tmp_path: Path) -> None:
    """A batch matching more rows than the store allows must still be verified.

    Chroma binds one SQL variable per matched chunk row, so a fixed batch of
    paths is not a bound at all: whether the query fits depends on how many
    chunks those files produced. A corpus of large files makes every
    verification query fail, which strands the candidate permanently.
    """
    manager, vector_db = _verification_manager(tmp_path)
    paths = [f"doc{index:02d}.md" for index in range(8)]
    _seed_chunked_paths(vector_db.collection_name, paths, chunks_per_path=100)
    _FakeVectorDb.max_rows_per_get = 250

    assert manager._candidate_paths_with_vectors(vector_db, paths) == set(paths)

    # Halving is the property that makes this affordable, and correctness alone
    # does not pin it: splitting one path off at a time also terminates and also
    # returns the right answer, while turning O(log n) queries into O(n).
    # 8 (800 rows, refused) -> 4 (400, refused) -> 2 + 2 (200 each, answered),
    # then the same on the right half: 7 queries.
    assert _FakeVectorDb.get_calls == 7


def test_vector_verification_confirms_a_file_larger_than_the_store_ceiling(tmp_path: Path) -> None:
    """One file with more chunks than the ceiling must still be confirmed.

    Splitting alone cannot rescue this: a single path is the smallest batch
    there is, so the query has to stop asking for every row it matches.
    """
    manager, vector_db = _verification_manager(tmp_path)
    _seed_chunked_paths(vector_db.collection_name, ["huge.md"], chunks_per_path=500)
    _FakeVectorDb.max_rows_per_get = 250

    assert manager._candidate_paths_with_vectors(vector_db, ["huge.md"]) == {"huge.md"}


def test_vector_verification_still_reports_paths_without_vectors_after_splitting(tmp_path: Path) -> None:
    """Splitting must not turn an unverifiable path into a verified one."""
    manager, vector_db = _verification_manager(tmp_path)
    present = [f"present{index:02d}.md" for index in range(6)]
    _seed_chunked_paths(vector_db.collection_name, present, chunks_per_path=100)
    _FakeVectorDb.max_rows_per_get = 250
    missing = ["missing0.md", "missing1.md"]

    found = manager._candidate_paths_with_vectors(vector_db, [*present, *missing])

    assert found == set(present)


def test_vector_verification_uses_one_query_when_the_store_answers(tmp_path: Path) -> None:
    """A store that can answer the whole batch must be asked exactly once.

    Splitting is a fallback, not the normal path: verifying per file would turn
    one query per 128 files into 128, which is the cost this batching exists to
    avoid.
    """
    manager, vector_db = _verification_manager(tmp_path)
    paths = [f"doc{index:02d}.md" for index in range(8)]
    _seed_chunked_paths(vector_db.collection_name, paths, chunks_per_path=100)

    assert manager._candidate_paths_with_vectors(vector_db, paths) == set(paths)
    assert _FakeVectorDb.get_calls == 1


def test_vector_verification_does_not_split_when_the_collection_is_gone(tmp_path: Path) -> None:
    """A missing collection must surface at once instead of being re-asked per path.

    Splitting exists to shrink a query the store found too large, and a missing
    collection is not that: it stays missing however small the query gets.
    Descending anyway costs ``log2(batch) + 1`` doomed queries before the
    leftmost leaf raises the identical error, and the caller does not catch, so
    verification aborts on this batch either way. Failing on the first query is
    the cheaper of two identical outcomes, which is what the count below pins.
    """
    manager, vector_db = _verification_manager(tmp_path)
    paths = [f"doc{index:02d}.md" for index in range(8)]
    # No rows are seeded: every ``get`` raises before reading the store, so
    # seeding would only suggest the contents mattered. ``create()`` in the
    # helper is what registers the collection for ``get_collection``.
    _FakeVectorDb.vanished_on_get = {vector_db.collection_name}

    with pytest.raises(NotFoundError):
        manager._candidate_paths_with_vectors(vector_db, paths)

    assert _FakeVectorDb.get_calls == 1


def test_vector_verification_records_that_it_had_to_split(tmp_path: Path) -> None:
    """A store refusing batches must leave a trace, not degrade silently.

    Splitting keeps verification correct but multiplies its queries, and how
    far it degrades depends on chunk counts nothing here can see. A base whose
    files grew past the ceiling would otherwise get quietly slower with no
    signal anywhere, which is the same invisibility that let the original
    failure go undiagnosed.
    """
    manager, vector_db = _verification_manager(tmp_path)
    paths = [f"doc{index:02d}.md" for index in range(8)]
    _seed_chunked_paths(vector_db.collection_name, paths, chunks_per_path=100)
    _FakeVectorDb.max_rows_per_get = 250

    with capture_logs() as logs:
        assert manager._candidate_paths_with_vectors(vector_db, paths) == set(paths)

    split_logs = [entry for entry in logs if entry["event"] == "Split a refused knowledge vector verification query"]
    assert [entry["paths"] for entry in split_logs] == [8, 4, 4]
