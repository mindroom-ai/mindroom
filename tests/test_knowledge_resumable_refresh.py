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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

import pytest
from agno.knowledge.document.base import Document
from agno.knowledge.embedder.base import Embedder
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

if TYPE_CHECKING:
    from collections.abc import Iterator

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


def _metadata_matches(metadata: dict[str, Any], key: str, condition: object) -> bool:
    if isinstance(condition, dict):
        if "$in" in condition:
            return metadata.get(key) in condition["$in"]
        if "$eq" in condition:
            return metadata.get(key) == condition["$eq"]
        msg = f"unsupported where condition: {condition!r}"
        raise AssertionError(msg)
    return metadata.get(key) == condition


class _FakeCollection:
    def __init__(self, name: str) -> None:
        self._name = name

    def get(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        include: list[str] | None = None,
        where: dict[str, object] | None = None,
    ) -> dict[str, object]:
        _ = include
        records = list(_FakeVectorDb.store.get(self._name, []))
        if where:
            key, condition = next(iter(where.items()))
            records = [record for record in records if _metadata_matches(record.metadata, key, condition)]
        selected = records[offset:] if limit is None else records[offset : offset + limit]
        return {
            "ids": [str(index) for index in range(len(selected))],
            "metadatas": [dict(record.metadata) for record in selected],
        }


class _FakeClient:
    def get_collection(self, name: str) -> _FakeCollection:
        return _FakeCollection(name)

    def list_collections(self) -> list[str]:
        return sorted(_FakeVectorDb.store)


class _FakeVectorDb:
    store: ClassVar[dict[str, list[_Record]]] = {}

    def __init__(self, *, collection: str, embedder: Embedder | None = None, **_: object) -> None:
        self.collection_name = collection
        self.embedder = embedder
        self.client = _FakeClient()

    def exists(self) -> bool:
        return self.collection_name in self.store

    def create(self) -> None:
        self.store.setdefault(self.collection_name, [])

    def delete(self) -> bool:
        self.store.pop(self.collection_name, None)
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


class _RecordingEmbedder(Embedder):
    """Embedder that records every provider request and can inject faults."""

    def __init__(self) -> None:
        super().__init__()
        self.batch_requests: list[tuple[str, ...]] = []
        self.single_requests: list[str] = []
        self.embedded_texts: list[str] = []
        self.failures: dict[str, list[BaseException]] = {}
        self.fail_everything: BaseException | None = None
        self.supports_batch = True

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

    def get_embeddings_batch(self, texts: list[str]) -> list[list[float]]:
        if not self.supports_batch:
            msg = "batch embedding is disabled for this test"
            raise AssertionError(msg)
        self.batch_requests.append(tuple(texts))
        self._maybe_fail(texts)
        self.embedded_texts.extend(texts)
        return [[float(len(text)), 1.0] for text in texts]

    def get_embedding(self, text: str) -> list[float]:
        return self.get_embedding_and_usage(text)[0]

    def get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, Any] | None]:
        self.single_requests.append(text)
        self._maybe_fail([text])
        self.embedded_texts.append(text)
        return [float(len(text)), 1.0], None


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


def _config(tmp_path: Path, docs_path: Path, *, chunk_size: int = 5000) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={"helper": AgentConfig(display_name="Helper", knowledge_bases=["docs"])},
            models={},
            memory={},
            knowledge_bases={"docs": KnowledgeBaseConfig(path=str(docs_path), chunk_size=chunk_size)},
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
    published_collection = _published_state(config, runtime_paths).collection

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
    published_collection = _published_state(config, runtime_paths).collection

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
    save_candidate_checkpoint(storage_path, checkpoint)
    assert not _candidate_journal_path(storage_path).exists()
    assert load_candidate_checkpoint(storage_path) == checkpoint or (
        load_candidate_checkpoint(storage_path).completed == checkpoint.completed
    )


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
