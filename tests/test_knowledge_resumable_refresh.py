"""Resumable, bounded semantic knowledge refresh behavior.

These tests drive the candidate-index lifecycle through the real vector store
and a recording embedder so every assertion about *which* files were embedded,
how many provider requests were issued, and what survives an interruption is
exact.

Against the pre-fix implementation the resume, retry and bounded-work tests
fail: the candidate collection was deleted on every non-publishing outcome and
all progress lived in process memory.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING, Any, BinaryIO

import pytest
from agno.knowledge.document.base import Document
from agno.knowledge.reader.markdown_reader import MarkdownReader
from structlog.testing import capture_logs

import mindroom.embeddings.batching as embedding_batching_module
import mindroom.knowledge.candidate_checkpoint as knowledge_checkpoint_module
import mindroom.knowledge.indexing_config as knowledge_indexing_config
import mindroom.knowledge.manager as knowledge_manager_module
import mindroom.knowledge.registry as knowledge_registry
from mindroom.config.agent import AgentConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.embeddings.batching import RefreshEmbedder, plan_embedding_batches
from mindroom.embeddings.errors import (
    EMBEDDER_EMPTY_VECTOR_DETAIL,
    EmbedderRequestError,
    embedder_failure_is_transient,
    embedder_request_error,
    embedder_retry_after_seconds,
    openai_embedder_error,
)
from mindroom.embeddings.signatures import effective_knowledge_embedder_signature, effective_mem0_embedder_signature
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.candidate_checkpoint import (
    CandidateCheckpoint,
    CandidateFailure,
    FileSignature,
    _candidate_checkpoint_path,
    _candidate_journal_path,
    append_candidate_journal,
    delete_candidate_checkpoint,
    load_candidate_checkpoint,
    load_seals,
    save_candidate_checkpoint,
)
from mindroom.knowledge.collection_lifetime import collection_lifetime_lock
from mindroom.knowledge.collections import (
    CollectionSpace,
    _collection_name_for_base,
    candidate_collection_name,
)
from mindroom.knowledge.index_metadata import write_json_atomic
from mindroom.knowledge.index_retry import EmbeddingRetryPolicy, run_with_embedding_retry
from mindroom.knowledge.manager import KnowledgeManager, _PreparedFile
from mindroom.knowledge.refresh_outcome import RefreshOutcome
from mindroom.knowledge.refresh_runner import _refresh_knowledge_binding as refresh_knowledge_binding
from mindroom.knowledge.registry import (
    PublishedIndexState,
    get_published_index,
    load_published_index_state,
    published_index_metadata_path,
    published_index_storage_path,
    resolve_published_index_key,
    save_published_index_state,
)
from mindroom.knowledge.status import get_knowledge_index_status
from mindroom.knowledge.utils import resolve_agent_knowledge_access
from mindroom.knowledge.vector_store import KnowledgeCollection, list_collection_names
from mindroom.llm.errors import ProviderUnavailable
from mindroom.model_defaults import SENTENCE_TRANSFORMERS_DEFAULT
from mindroom.providers.errors import classify_provider_error
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.knowledge_test_support import seed_collection, stored_rows

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from mindroom.constants import RuntimePaths
    from mindroom.embeddings.base import Embedder
    from mindroom.knowledge.vector_store import StoredRow


# --------------------------------------------------------------------------
# Recording embedder
# --------------------------------------------------------------------------


class _RecordingNonBatchEmbedder:
    """Recording embedder that embeds one text per request, like Ollama."""

    def __init__(self) -> None:
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
        #: OpenAIEmbedder raises for a short response).
        self.batch_error: BaseException | None = None

    @property
    def sends_batches(self) -> bool:
        return False

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

    def single_request(self, text: str) -> list[float]:
        self.single_requests.append(text)
        self._maybe_fail([text])
        self.embedded_texts.append(text)
        return [float(len(text)), 1.0]

    def batch_request(self, texts: list[str]) -> list[list[float]]:
        if not self.supports_batch:
            msg = "batch embedding is disabled for this test"
            raise AssertionError(msg)
        self.batch_requests.append(tuple(texts))
        if self.batch_error is not None:
            raise self.batch_error
        embeddings = [[float(len(text)), 1.0] for text in texts]
        if self.short_batch:
            # A short response is a cardinality fault: it happens before the
            # backend would have reported anything about individual inputs.
            self.embedded_texts.extend(texts[:-1])
            return embeddings[:-1]
        self._maybe_fail(texts)
        self.embedded_texts.extend(texts)
        return embeddings

    def embed(self, texts: Sequence[str], *, purpose: str) -> list[list[float]]:
        del purpose
        if len(texts) == 1:
            return [self.single_request(texts[0])]
        assert self.sends_batches, "an embedder without batch requests was sent several texts"
        return self.batch_request(list(texts))

    async def aembed(self, texts: Sequence[str], *, purpose: str) -> list[list[float]]:
        return self.embed(texts, purpose=purpose)


class _RecordingEmbedder(_RecordingNonBatchEmbedder):
    """Recording embedder that also sends several texts in one request."""

    @property
    def sends_batches(self) -> bool:
        return True


def _use_non_batching_embedder(monkeypatch: pytest.MonkeyPatch) -> _RecordingNonBatchEmbedder:
    """Point the manager at a provider that cannot batch at all."""
    plain = _RecordingNonBatchEmbedder()
    monkeypatch.setattr(knowledge_manager_module, "create_configured_embedder", lambda *_a, **_k: plain)
    return plain


class _NonBatchingEmbedder:
    """Embedder without batch requests, to prove batch embedding degrades safely."""

    sends_batches = False

    def __init__(self) -> None:
        self.single_requests: list[str] = []

    def embed(self, texts: Sequence[str], *, purpose: str) -> list[list[float]]:
        del purpose
        (text,) = texts
        self.single_requests.append(text)
        return [[float(len(text)), 1.0]]

    async def aembed(self, texts: Sequence[str], *, purpose: str) -> list[list[float]]:
        return self.embed(texts, purpose=purpose)


@pytest.fixture
def embedder() -> _RecordingEmbedder:
    """Return the single embedder every manager in one test shares."""
    return _RecordingEmbedder()


@pytest.fixture(autouse=True)
def recording_embedder_everywhere(
    monkeypatch: pytest.MonkeyPatch,
    embedder: _RecordingEmbedder,
) -> Iterator[None]:
    """Index and search with the recording embedder; vectors go to the real store under each test's storage root."""
    monkeypatch.setattr(knowledge_manager_module, "create_configured_embedder", lambda *_a, **_k: embedder)
    monkeypatch.setattr(knowledge_registry, "create_configured_embedder", lambda *_a, **_k: embedder)

    async def _no_sleep(_seconds: float) -> None:
        return

    monkeypatch.setattr(knowledge_manager_module, "_EMBEDDING_RETRY_SLEEP", _no_sleep)
    knowledge_registry._published_indexes.clear()
    yield
    knowledge_registry._published_indexes.clear()


# --------------------------------------------------------------------------
# Config / manager helpers
# --------------------------------------------------------------------------


def _config(
    tmp_path: Path,
    docs_path: Path,
    *,
    chunk_size: int = 5000,
    chunk_overlap: int = 0,
    memory: dict[str, Any] | None = None,
) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={"helper": AgentConfig(display_name="Helper", knowledge_bases=["docs"])},
            models={},
            memory=memory or {},
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


def _collection(config: Config, name: str) -> KnowledgeCollection:
    """Return one collection in the docs base's storage directory."""
    return KnowledgeCollection(path=_storage_path(config, runtime_paths_for(config)), name=name)


def _collection_names(config: Config) -> tuple[str, ...]:
    return list_collection_names(_storage_path(config, runtime_paths_for(config)))


def _stored_paths(config: Config, collection: str) -> list[str]:
    return sorted(row["metadata"]["source_path"] for row in stored_rows(_collection(config, collection)))


def _write_corpus(docs_path: Path, count: int, *, body: str = "content") -> list[str]:
    docs_path.mkdir(parents=True, exist_ok=True)
    names = [f"doc{index:04d}.md" for index in range(count)]
    for index, name in enumerate(names):
        (docs_path / name).write_text(f"{body} {index}", encoding="utf-8")
    return names


def _overlapping_body(tokens: int) -> str:
    """Return text whose chunks all differ, so batched requests cannot dedupe them away."""
    return " ".join(f"token{index:04d}" for index in range(tokens))


def _candidate_collections(config: Config) -> list[str]:
    return [name for name in _collection_names(config) if "_candidate_" in name]


def _published_state(config: Config, runtime_paths: RuntimePaths) -> PublishedIndexState | None:
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    return load_published_index_state(published_index_metadata_path(key))


_AUTH_FAILURE = EmbedderRequestError("embedder authentication failed (HTTP 401)")


def _server_error() -> EmbedderRequestError:
    """Return the retryable error an embedder raises for an HTTP 503."""
    return EmbedderRequestError("embedder request failed (HTTP 503)", retryable=True)


def _api_error() -> Exception:
    """Return what the OpenAI embedder raises for a 503 with a ``Retry-After`` hint, classified at its boundary."""
    import httpx  # noqa: PLC0415
    from openai import APIStatusError  # noqa: PLC0415

    request = httpx.Request("POST", "http://embeddings.local/v1/embeddings")
    response = httpx.Response(503, request=request, headers={"retry-after": "7"})
    return openai_embedder_error(APIStatusError("overloaded", response=response, body=None))


# --------------------------------------------------------------------------
# 1. Cold-start interruption resumes the same candidate
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupted_cold_build_resumes_same_candidate_without_reembedding(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A killed build resumes its candidate and only embeds what it still owes."""
    (tmp_path / ".env").write_text("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY=1\n", encoding="utf-8")
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 6)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)

    interrupt_after = 3
    indexed_before_interrupt: list[str] = []
    manager = _manager(config)
    assert manager._max_concurrent_file_indexes == 1
    original_index = KnowledgeManager._index_file_locked

    async def _stop_midway(self: KnowledgeManager, prepared: _PreparedFile, **kwargs: object) -> bool:
        if len(indexed_before_interrupt) >= interrupt_after:
            raise asyncio.CancelledError
        indexed_before_interrupt.append(prepared.path.name)
        return await original_index(self, prepared, **kwargs)

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
    # merely batch-embedded in memory are legitimately re-embedded.
    completed_bodies = {f"content {names.index(name)}" for name in checkpoint.completed}
    embedded_after_interrupt = {body: embedder.embedded_count(body) for body in completed_bodies}

    # A brand new manager models a process restart: nothing survives in memory.
    resumed_manager = _manager(config)
    assert (await resumed_manager.reindex_all()).indexed_count == len(names) - interrupt_after

    resumed_checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert resumed_checkpoint is None, "publication retires the checkpoint"
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.status == "complete"
    assert state.collection == interrupted_collection, "resume must continue the same candidate"
    assert state.indexed_count == len(names)
    for body, count in embedded_after_interrupt.items():
        assert embedder.embedded_count(body) == count, "completed files must not be embedded again"
    assert _stored_paths(config, interrupted_collection) == sorted(names)


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

    async def _fail_b(self: KnowledgeManager, prepared: _PreparedFile, **kwargs: object) -> bool:
        if prepared.path.name == "b.md":
            return False
        return await original_index(self, prepared, **kwargs)

    KnowledgeManager._index_file_locked = _fail_b  # type: ignore[method-assign]
    try:
        result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

    assert result.index_published is False
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.index is not None
    assert [document.content for document in await lookup.index.reader.asearch("published", limit=5)] == [
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
    embedder.failures["content 4"] = [_server_error()]

    manager = _manager(config)
    assert await manager.reindex_all() == RefreshOutcome(indexed_count=5, published=True, error=None)

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
    embedder.failures["content 3"] = [_server_error() for _ in range(20)]

    manager = _manager(config)
    outcome = await manager.reindex_all()
    assert outcome.indexed_count == 3
    assert not outcome.published
    assert outcome.error is not None
    assert "Indexed 3 of 4" in outcome.error
    assert _published_state(config, runtime_paths) is None

    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    assert checkpoint.status == "failed"
    assert set(checkpoint.failed) == {"doc0003.md"}
    assert checkpoint.failed["doc0003.md"].attempts == 1
    assert len(checkpoint.completed) == 3

    embedder.failures.pop("content 3")
    embedded_before = dict.fromkeys(embedder.embedded_texts)
    assert (await _manager(config).reindex_all()).indexed_count == 1
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
    # Searching embeds the query with the same embedder, so let it answer again.
    embedder.fail_everything = None
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.index is not None
    assert [document.content for document in await lookup.index.reader.asearch("body", limit=5)] == [
        "published body",
    ]


def _classified(status_code: int | None = None, *, unreachable: bool = False) -> EmbedderRequestError:
    """Return the embedder error of a typed provider failure, as an embedder boundary builds it."""
    if unreachable:
        failure = ProviderUnavailable("connection refused", provider="ollama", code="connection_error")
    else:
        failure = classify_provider_error("rejected", provider="ollama", status_code=status_code)
    return embedder_request_error(RuntimeError("rejected"), failure)


def test_embedding_failure_classification_splits_transient_from_permanent() -> None:
    """An embedder failure is transient exactly when the rule model requests follow would retry it."""
    assert embedder_failure_is_transient(_classified(unreachable=True))
    assert embedder_failure_is_transient(_classified(429))
    assert embedder_failure_is_transient(_classified(408))
    assert embedder_failure_is_transient(_classified(502))
    assert embedder_failure_is_transient(_classified(409))
    assert embedder_failure_is_transient(TimeoutError())
    assert embedder_failure_is_transient(ConnectionResetError())
    assert not embedder_failure_is_transient(_classified(401))
    assert not embedder_failure_is_transient(_classified(403))
    assert not embedder_failure_is_transient(_classified(400))
    assert embedder_failure_is_transient(_classified(524))
    assert not embedder_failure_is_transient(_classified())
    assert not embedder_failure_is_transient(embedder_request_error(RuntimeError("out of memory"), None))
    assert not embedder_failure_is_transient(EmbedderRequestError(EMBEDDER_EMPTY_VECTOR_DETAIL))
    assert not embedder_failure_is_transient(ValueError("nonsense"))
    # The detail text alone never makes a failure retryable.
    assert not embedder_failure_is_transient(EmbedderRequestError("embedder endpoint unreachable"))


def test_provider_retry_after_header_survives_error_classification() -> None:
    """The provider backoff hint must cross the credential-redacting boundary."""
    assert embedder_retry_after_seconds(_api_error()) == 7.0
    assert embedder_retry_after_seconds(EmbedderRequestError("x", retry_after_seconds=3.5)) == 3.5
    assert embedder_retry_after_seconds(EmbedderRequestError("x")) is None


def test_retry_backoff_honors_retry_after_and_stays_bounded() -> None:
    """Backoff grows, jitters, respects Retry-After, and never exceeds the cap."""
    policy = EmbeddingRetryPolicy(initial_backoff_seconds=1.0, max_backoff_seconds=10.0, jitter_ratio=0.5)
    assert policy._backoff_seconds(1, retry_after_seconds=None, jitter_unit=0.5) == 1.0
    assert policy._backoff_seconds(3, retry_after_seconds=None, jitter_unit=0.5) == 4.0
    assert policy._backoff_seconds(9, retry_after_seconds=None, jitter_unit=0.5) == 10.0
    assert policy._backoff_seconds(1, retry_after_seconds=6.0, jitter_unit=0.5) == 6.0
    assert policy._backoff_seconds(1, retry_after_seconds=1000.0, jitter_unit=0.5) == 10.0
    assert policy._backoff_seconds(1, retry_after_seconds=None, jitter_unit=0.0) == 0.5
    assert policy._backoff_seconds(1, retry_after_seconds=None, jitter_unit=1.0) == 1.5
    assert policy._backoff_seconds(9, retry_after_seconds=None, jitter_unit=1.0) == 10.0


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
    _collection(config, stale_candidate).reset()
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
    assert stale_candidate not in _collection_names(config), "incompatible candidate is discarded"
    assert published_collection in _collection_names(config), "published index is not touched"


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
    stale_candidate = f"{manager._collections.default_collection}_candidate_stale"
    _collection(config, stale_candidate).reset()
    save_candidate_checkpoint(
        _storage_path(config, runtime_paths),
        CandidateCheckpoint(
            collection=stale_candidate,
            settings=replace(manager._indexing_settings, embedder_model="old-model"),
        ),
    )
    original_delete = KnowledgeCollection.delete
    attempts = 0

    def _fail_once(self: KnowledgeCollection) -> None:
        nonlocal attempts
        if self.name == stale_candidate and attempts == 0:
            attempts += 1
            message = "collection is locked"
            raise RuntimeError(message)
        original_delete(self)

    monkeypatch.setattr(KnowledgeCollection, "delete", _fail_once)

    run = await manager._open_candidate_run()

    assert run.checkpoint.collection != stale_candidate
    assert stale_candidate not in _collection_names(config), "candidate GC did not retry the transient failure"


@pytest.mark.asyncio
async def test_incompatible_missing_candidate_is_already_deleted(tmp_path: Path) -> None:
    """A crash before candidate creation must not poison later settings changes."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    manager = _manager(config)
    missing_candidate = f"{manager._collections.default_collection}_candidate_missing"
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

    async def _fail_change(self: KnowledgeManager, prepared: _PreparedFile, **kwargs: object) -> bool:
        if prepared.path.name == "change.md":
            return False
        return await original_index(self, prepared, **kwargs)

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

    assert (await _manager(config).reindex_all()).indexed_count == 2

    assert embedder.embedded_count("keep me") == 0, "unchanged work is reused"
    assert embedder.embedded_count("new body") == 1
    assert embedder.embedded_count("added body") == 1
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.collection == candidate_collection
    assert state.indexed_count == 3
    assert _stored_paths(config, candidate_collection) == ["added.md", "change.md", "keep.md"], (
        "deleted vectors are removed"
    )


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
    original_signature = knowledge_manager_module._knowledge_source_signature
    mutated = False

    def _mutate_during_verification(*args: object, **kwargs: object) -> str:
        nonlocal mutated
        if not mutated:
            mutated = True
            (docs_path / "late.md").write_text("late body", encoding="utf-8")
        return original_signature(*args, **kwargs)  # type: ignore[arg-type]

    knowledge_manager_module._knowledge_source_signature = _mutate_during_verification  # type: ignore[assignment]
    try:
        outcome = await manager.reindex_all()
    finally:
        knowledge_manager_module._knowledge_source_signature = original_signature  # type: ignore[assignment]

    assert outcome == RefreshOutcome(indexed_count=4, published=True, error=None)
    assert embedder.embedded_count("content 0") == 1, "the racing change costs no re-embedding"
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.indexed_count == 4


@pytest.mark.asyncio
async def test_a_local_refresh_reads_only_the_files_whose_stat_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file whose ctime and size still match its recorded signature is not read again, before or after indexing.

    The changed file is read once, by the scan that found it, and then copied for indexing; a refresh with nothing
    changed reads no file at all. A ctime-only change (``chmod``) is read once and then trusted again, because the
    restamped signature reaches the published seal the next refresh compares against.
    """
    monkeypatch.setattr(knowledge_manager_module, "_RACY_WINDOW_NS", 0)
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    (docs_path / names[0]).write_text("changed body", encoding="utf-8")
    read: list[str] = []
    original_signature = knowledge_manager_module._descriptor_signature

    def _recording_signature(descriptor: int, file_path: Path, output: BinaryIO | None = None) -> FileSignature:
        if output is None:
            read.append(file_path.name)
        return original_signature(descriptor, file_path, output)

    monkeypatch.setattr(knowledge_manager_module, "_descriptor_signature", _recording_signature)

    assert (await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)).index_published
    assert read == [names[0]]
    read.clear()
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert read == []
    (docs_path / names[1]).chmod(0o600)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert read == [names[1]]
    read.clear()
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert read == []


@pytest.mark.asyncio
async def test_a_file_changed_after_the_adopted_scan_is_still_indexed(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A change the adopted scan missed fails the final hash, so the next round indexes it before publishing."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await _manager(config).reindex_all()
    manager = _manager(config)
    await manager.source_signature()
    (docs_path / names[1]).write_text("late body", encoding="utf-8")

    assert (await manager.reindex_all()).published
    assert embedder.embedded_count("late body") == 1
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.source_signature == await _manager(config).source_signature()


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
    assert len(_candidate_collections(config)) == 1, "one candidate, not one per request"
    for index in range(4):
        assert embedder.embedded_count(f"content {index}") == 1, "no duplicate full rebuild"


# --------------------------------------------------------------------------
# 10. Cancellation boundaries
# --------------------------------------------------------------------------


@pytest.mark.parametrize("cancel_before_metadata_save", [True, False])
@pytest.mark.asyncio
async def test_cancellation_around_publication_never_produces_a_false_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_before_metadata_save: bool,
) -> None:
    """Cancelling at either side of the metadata write leaves consistent state."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    manager = _manager(config)

    def _cancelling_save(metadata_path: Path, state: PublishedIndexState) -> None:
        if cancel_before_metadata_save:
            msg = "metadata save failed"
            raise OSError(msg)
        save_published_index_state(metadata_path, state)

    monkeypatch.setattr("mindroom.knowledge.manager.save_published_index_state", _cancelling_save)
    if cancel_before_metadata_save:
        with pytest.raises(OSError, match="metadata save failed"):
            await manager.reindex_all()
    else:
        await manager.reindex_all()

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
    assert published_collection in _collection_names(config), "the published index survives"


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
    default_collection = _manager(config)._collections.default_collection

    abandoned = f"{default_collection}_candidate_abandonedabandoned"
    unknown = "some_other_service_collection"
    _collection(config, abandoned).reset()
    _collection(config, unknown).reset()

    manager = _manager(config)
    run = await manager._open_candidate_run()

    assert abandoned not in _collection_names(config), "abandoned candidates are reclaimed"
    assert published_collection in _collection_names(config)
    assert unknown in _collection_names(config), "unknown collections are preserved"
    assert run.checkpoint.collection in _collection_names(config)


@pytest.mark.asyncio
async def test_repeated_interrupted_refreshes_keep_collection_count_bounded(
    tmp_path: Path,
) -> None:
    """Many interrupted refreshes must not accumulate candidate collections."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    original_index = KnowledgeManager._index_file_locked

    async def _always_fail(self: KnowledgeManager, prepared: _PreparedFile, **kwargs: object) -> bool:
        _ = (self, prepared, kwargs)
        return False

    KnowledgeManager._index_file_locked = _always_fail  # type: ignore[method-assign]
    try:
        for _ in range(6):
            await _manager(config).reindex_all()
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

    assert len(_candidate_collections(config)) == 1


# --------------------------------------------------------------------------
# 12. Batching
# --------------------------------------------------------------------------


def test_embedding_batches_respect_item_and_payload_limits() -> None:
    """Batch planning bounds both request size and request payload."""
    assert plan_embedding_batches([]) == []
    assert plan_embedding_batches(["a"] * 129) == [["a"] * 64, ["a"] * 64, ["a"]]
    half = "a" * 256_000
    assert plan_embedding_batches([half, half, "c"]) == [[half, half], ["c"]]
    # A single oversized chunk still gets its own request rather than being split.
    oversized = "x" * 512_001
    assert plan_embedding_batches([oversized, "y"]) == [[oversized], ["y"]]


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

    assert (await _manager(config).reindex_all()).indexed_count == file_count

    assert embedder.single_requests == [], "every chunk was embedded in a batch request"
    assert len(embedder.batch_requests) <= 4, f"expected batched requests, got {len(embedder.batch_requests)}"
    assert sum(len(batch) for batch in embedder.batch_requests) == file_count
    assert all(len(batch) <= 64 for batch in embedder.batch_requests)


@pytest.mark.asyncio
async def test_batch_failure_falls_back_to_per_file_without_reembedding_successes(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exhausted batch retry degrades to per-file work without embedding anything twice."""
    monkeypatch.setattr(
        knowledge_manager_module,
        "_EMBEDDING_RETRY_POLICY",
        EmbeddingRetryPolicy(max_attempts=2, initial_backoff_seconds=0.0),
    )
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    # "content 1" poisons the batch twice, exhausting the batch-level retry.
    embedder.failures["content 1"] = [_server_error() for _ in range(2)]

    assert (await _manager(config).reindex_all()).indexed_count == 3

    assert embedder.single_requests, "the fallback path re-embedded per file"
    for index in range(3):
        assert embedder.embedded_count(f"content {index}") == 1


def _tokens_per_minute_error() -> Exception:
    """Return what the OpenAI embedder raises for a request larger than the account's tokens-per-minute limit."""
    import httpx  # noqa: PLC0415
    from openai import RateLimitError  # noqa: PLC0415

    message = (
        "Request too large for text-embedding-3-small in organization org-test on tokens per min (TPM): Limit 40000, "
        "Requested 80000. The input or output tokens must be reduced in order to run successfully."
    )
    body = {"message": message, "type": "tokens", "param": None, "code": "rate_limit_exceeded"}
    response = httpx.Response(429, request=httpx.Request("POST", "http://embeddings.local/v1/embeddings"))
    return openai_embedder_error(RateLimitError(message, response=response, body=body))


@pytest.mark.asyncio
async def test_a_batch_above_the_tokens_per_minute_limit_is_embedded_one_chunk_per_request(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Only a smaller request fits the account's per-minute token limit, so the batch's chunks go one at a time."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    embedder.batch_error = _tokens_per_minute_error()

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert _stored_paths(config, state.collection) == names
    assert (len(embedder.batch_requests), sorted(embedder.single_requests)) == (
        1,
        [f"content {index}" for index in range(3)],
    )


class _InFlightEmbedder(_RecordingEmbedder):
    """Recording embedder that counts how many batch requests run at once."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = Lock()
        self.started = 0
        self.in_flight = 0
        self.peak_in_flight = 0
        self.overlapped = Event()
        self.saturated = Event()

    def hold(self, texts: list[str]) -> None:
        """Keep a request open until another one overlaps it, then a little longer."""
        del texts
        self.overlapped.wait(timeout=5)
        time.sleep(0.02)

    def batch_request(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            self.started += 1
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            if self.in_flight >= 2:
                self.overlapped.set()
            if self.in_flight >= knowledge_manager_module._EMBEDDING_REQUESTS_IN_FLIGHT:
                self.saturated.set()
        try:
            self.hold(texts)
            return super().batch_request(texts)
        finally:
            with self._lock:
                self.in_flight -= 1


def _use_in_flight_embedder(monkeypatch: pytest.MonkeyPatch) -> _InFlightEmbedder:
    """Index with an in-flight counting embedder and four texts per request, so small corpora plan many requests."""
    tracked = _InFlightEmbedder()
    monkeypatch.setattr(knowledge_manager_module, "create_configured_embedder", lambda *_a, **_k: tracked)
    monkeypatch.setattr(embedding_batching_module, "_MAX_EMBEDDING_BATCH_ITEMS", 4)
    return tracked


@pytest.mark.asyncio
async def test_batch_overlaps_its_embedding_requests_up_to_the_in_flight_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch's planned requests overlap, but never more of them than the in-flight cap."""
    tracked = _use_in_flight_embedder(monkeypatch)
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 40)
    config = _config(tmp_path, docs_path)

    assert (await _manager(config).reindex_all()).indexed_count == 40

    assert len(tracked.batch_requests) == 10
    assert tracked.single_requests == []
    assert 2 <= tracked.peak_in_flight <= knowledge_manager_module._EMBEDDING_REQUESTS_IN_FLIGHT


@pytest.mark.asyncio
async def test_permanent_batch_error_cancels_the_other_request_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A permanent rejection cancels the batch's other requests; none is retried or started afterwards."""
    tracked = _use_in_flight_embedder(monkeypatch)
    release = Event()

    def _reject_one_and_stall_the_rest(texts: list[str]) -> None:
        if "content 0" in texts:
            tracked.saturated.wait(timeout=5)
            msg = "embedder request failed (HTTP 400)"
            raise EmbedderRequestError(msg)
        release.wait(timeout=5)
        # A request task that outlived the batch would retry this.
        raise _server_error()

    monkeypatch.setattr(tracked, "hold", _reject_one_and_stall_the_rest)
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 40)
    config = _config(tmp_path, docs_path)

    outcome = await _manager(config).reindex_all()

    assert outcome.error is not None
    assert "embedder request failed (HTTP 400)" in outcome.error
    assert asyncio.all_tasks() == {asyncio.current_task()}, "a request task outlived the batch"
    release.set()
    for _ in range(500):
        if tracked.in_flight == 0:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    assert tracked.started == knowledge_manager_module._EMBEDDING_REQUESTS_IN_FLIGHT
    assert tracked.single_requests == []


@pytest.mark.asyncio
async def test_a_failed_batch_starts_no_further_provider_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider threads still embedding one text at a time stop asking once a permanent rejection fails the batch."""
    tracked = _use_in_flight_embedder(monkeypatch)
    # Short responses retire batching, so the other requests go on one text per request on their threads.
    tracked.short_batch = True
    rejected = Event()
    others_on_single_texts = Event()
    release = Event()
    entered: list[str] = []
    original_single = tracked.single_request

    def _slow_single(text: str) -> list[float]:
        entered.append(text)
        if len(entered) >= knowledge_manager_module._EMBEDDING_REQUESTS_IN_FLIGHT - 1:
            others_on_single_texts.set()
        release.wait(timeout=5)
        return original_single(text)

    def _reject_one(texts: list[str]) -> None:
        # Every request is in flight before any returns, so each other worker's short response sends it on to one
        # text per request, and the rejection only comes once all of them are inside that loop.
        tracked.saturated.wait(timeout=5)
        if "content 0" in texts:
            others_on_single_texts.wait(timeout=5)
            rejected.set()
            msg = "embedder request failed (HTTP 400)"
            raise EmbedderRequestError(msg)

    monkeypatch.setattr(tracked, "single_request", _slow_single)
    monkeypatch.setattr(tracked, "hold", _reject_one)
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 40)

    outcome = await _manager(_config(tmp_path, docs_path)).reindex_all()
    entered_when_failed = len(entered)
    release.set()
    await asyncio.sleep(0.3)

    assert outcome.error is not None
    assert rejected.is_set()
    assert entered_when_failed == knowledge_manager_module._EMBEDDING_REQUESTS_IN_FLIGHT - 1
    # Each of those threads finishes the request it was already making, and starts no other.
    assert len(entered) == entered_when_failed


@pytest.mark.asyncio
async def test_embedding_requests_in_flight_never_exceed_the_file_index_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator who limits file indexing to one at a time also gets one embedding request at a time."""
    tracked = _use_in_flight_embedder(monkeypatch)
    monkeypatch.setattr(knowledge_manager_module, "_max_concurrent_knowledge_file_indexes", lambda _runtime_paths: 1)
    monkeypatch.setattr(tracked, "hold", lambda _texts: time.sleep(0.01))
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 20)

    assert (await _manager(_config(tmp_path, docs_path)).reindex_all()).published
    assert tracked.peak_in_flight == 1


@pytest.mark.asyncio
async def test_exhausted_request_falls_back_alone_and_other_requests_keep_their_vectors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the request that ran out of retries is embedded per file; its siblings' vectors are kept."""
    monkeypatch.setattr(
        knowledge_manager_module,
        "_EMBEDDING_RETRY_POLICY",
        EmbeddingRetryPolicy(max_attempts=2, initial_backoff_seconds=0.0),
    )
    tracked = _use_in_flight_embedder(monkeypatch)
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 12)
    config = _config(tmp_path, docs_path)
    tracked.failures["content 5"] = [_server_error() for _ in range(2)]

    assert (await _manager(config).reindex_all()).indexed_count == 12

    failed_request = next(batch for batch in tracked.batch_requests if "content 5" in batch)
    assert len(set(tracked.batch_requests)) == 3
    assert sorted(tracked.single_requests) == sorted(failed_request)
    for index in range(12):
        assert tracked.embedded_count(f"content {index}") == 1


@pytest.mark.asyncio
async def test_overlapping_embedding_requests_store_what_sequential_requests_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Embedding several requests at once stores the same rows and vectors as one request at a time."""
    tracked = _use_in_flight_embedder(monkeypatch)
    monkeypatch.setattr(tracked, "hold", lambda _texts: time.sleep(0.01))
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    for index in range(30):
        # Distinct lengths give every chunk its own vector, so a misattributed one shows.
        (docs_path / f"doc{index:04d}.md").write_text("word " * (index + 1), encoding="utf-8")

    async def _stored_rows(root: Path) -> list[tuple[Any, ...]]:
        config = _config(root, docs_path)
        assert (await _manager(config).reindex_all()).indexed_count == 30
        state = _published_state(config, runtime_paths_for(config))
        assert state is not None
        with closing(sqlite3.connect(_collection(config, state.collection).file)) as connection:
            return connection.execute("SELECT id, document, metadata, embedding FROM chunks ORDER BY id").fetchall()

    overlapping = await _stored_rows(tmp_path / "overlapping")
    monkeypatch.setattr(knowledge_manager_module, "_EMBEDDING_REQUESTS_IN_FLIGHT", 1)
    sequential = await _stored_rows(tmp_path / "sequential")

    assert len(overlapping) == 30
    assert overlapping == sequential


def test_refresh_embedder_degrades_for_providers_without_batching() -> None:
    """Providers without a batch surface keep working, one request per distinct chunk."""
    inner = _NonBatchingEmbedder()
    embedder = RefreshEmbedder(inner)

    assert embedder.batches() is False
    assert embedder.embed_texts(["one", "two"]) == {"one": [3.0, 1.0], "two": [3.0, 1.0]}
    assert inner.single_requests == ["one", "two"]
    assert embedder.embed_each(["one"]) == [[3.0, 1.0]]
    assert inner.single_requests == ["one", "two", "one"]


def test_a_too_large_batch_keeps_batching_for_the_next_one() -> None:
    """A smaller batch can pass the same limit, so one too-large batch does not retire batching for the refresh."""
    inner = _RecordingEmbedder()
    inner.batch_error = _tokens_per_minute_error()
    embedder = RefreshEmbedder(inner)

    assert sorted(embedder.embed_texts(["one", "two"])) == ["one", "two"]
    inner.batch_error = None
    embedder.embed_texts(["three", "four"])

    assert inner.batch_requests == [("one", "two"), ("three", "four")]
    assert inner.single_requests == ["one", "two"]


class _QueryPromptedEmbedder(_RecordingEmbedder):
    """Embedder whose query vectors differ from its document vectors, like a prompted local model."""

    def embed(self, texts: Sequence[str], *, purpose: str) -> list[list[float]]:
        if purpose == "query":
            return [[0.0, 0.0] for _text in texts]
        return super().embed(texts, purpose=purpose)


def test_refresh_embedder_embeds_chunks_as_documents() -> None:
    """Chunks must use the document path, or prompted models index them as queries."""
    embedder = RefreshEmbedder(_QueryPromptedEmbedder())

    assert embedder.embed_texts(["chunk", "bb"]) == {"chunk": [5.0, 1.0], "bb": [2.0, 1.0]}
    assert embedder.embed_texts(["chunk"]) == {"chunk": [5.0, 1.0]}
    assert embedder.embed_each(["chunk"]) == [[5.0, 1.0]]


# --------------------------------------------------------------------------
# 13. Bounded scheduling
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_large_corpus_keeps_live_tasks_bounded(
    tmp_path: Path,
) -> None:
    """Live indexing tasks stay bounded no matter how large the corpus is."""
    docs_path = tmp_path / "docs"
    file_count = 400
    _write_corpus(docs_path, file_count)
    (tmp_path / ".env").write_text("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY=4\n", encoding="utf-8")
    config = _config(tmp_path, docs_path)

    manager = _manager(config)
    peak_tasks = 0
    original_index = KnowledgeManager._index_file_locked

    async def _observe(self: KnowledgeManager, prepared: _PreparedFile, **kwargs: object) -> bool:
        nonlocal peak_tasks
        peak_tasks = max(peak_tasks, len(asyncio.all_tasks()))
        return await original_index(self, prepared, **kwargs)

    KnowledgeManager._index_file_locked = _observe  # type: ignore[method-assign]
    try:
        assert (await manager.reindex_all()).indexed_count == file_count
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

    async def _fail_last(self: KnowledgeManager, prepared: _PreparedFile, **kwargs: object) -> bool:
        if prepared.path.name == "doc0003.md":
            return False
        return await original_index(self, prepared, **kwargs)

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
        assert (await _manager(config).reindex_all()).indexed_count == 128

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

    async def _fail_third(self: KnowledgeManager, prepared: _PreparedFile, **kwargs: object) -> bool:
        if prepared.path.name == "blocker.md":
            return False
        return await original_index(self, prepared, **kwargs)

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
    _collection(config, checkpoint.collection).delete_source_paths(["lost.md"])
    (docs_path / "blocker.md").unlink()
    embedder.embedded_texts.clear()

    assert (await _manager(config).reindex_all()).indexed_count == 1
    assert embedder.embedded_count("lost body") == 1
    assert embedder.embedded_count("kept body") == 0
    assert _stored_paths(config, checkpoint.collection) == ["kept.md", "lost.md"]


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
    assert run.collection.exists()


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
    # Written as raw JSON on purpose: the current writer always emits the newer
    # keys, so only a hand-built payload can be the shape an old version left.
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    metadata_path = published_index_metadata_path(key)
    write_json_atomic(
        metadata_path,
        {
            "settings": state.settings.to_metadata(),
            "status": "complete",
            "collection": published_collection,
            "indexed_count": state.indexed_count,
            "source_signature": state.source_signature,
            "last_published_at": state.last_published_at,
        },
    )
    assert not {"refresh_job", "reason", "last_error", "updated_at", "last_refresh_at"} & set(
        json.loads(metadata_path.read_text(encoding="utf-8")),
    )
    delete_candidate_checkpoint(_storage_path(config, runtime_paths))

    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.availability is KnowledgeAvailability.READY
    assert lookup.index is not None

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert result.index_published is True
    assert _published_state(config, runtime_paths).collection == published_collection
    assert published_collection in _collection_names(config)


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


def test_candidate_checkpoint_retirement_deletes_the_authoritative_snapshot_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure on the second unlink must leave a resumable checkpoint."""
    storage_path = tmp_path / "state"
    settings = _manager(_config(tmp_path / "cfg", tmp_path / "cfg" / "docs"))._indexing_settings
    save_candidate_checkpoint(
        storage_path,
        CandidateCheckpoint(collection="candidate", settings=settings, completed={"a.md": (1, 2, "digest")}),
    )
    append_candidate_journal(storage_path, completed=[("b.md", (3, 4, "journal-digest"))])
    snapshot_path = _candidate_checkpoint_path(storage_path)
    journal_path = _candidate_journal_path(storage_path)
    original_unlink = Path.unlink
    unlinked: list[Path] = []

    def _fail_second_unlink(path: Path, *args: object, **kwargs: object) -> None:
        unlinked.append(path)
        if len(unlinked) == 2:
            message = "checkpoint directory stopped accepting unlinks"
            raise OSError(message)
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _fail_second_unlink)

    with pytest.raises(OSError, match="stopped accepting unlinks"):
        delete_candidate_checkpoint(storage_path)

    assert unlinked == [journal_path, snapshot_path]
    checkpoint = load_candidate_checkpoint(storage_path)
    assert checkpoint is not None
    assert checkpoint.collection == "candidate"
    assert checkpoint.completed == {"a.md": (1, 2, "digest")}


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
    (tmp_path / ".env").write_text("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY=1\n", encoding="utf-8")
    docs_path = tmp_path / "docs"
    file_count = 500
    _write_corpus(docs_path, file_count)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)

    stop_after = int(file_count * 0.9)
    indexed = 0
    original_index = KnowledgeManager._index_file_locked

    async def _stop_at_ninety_percent(self: KnowledgeManager, prepared: _PreparedFile, **kwargs: object) -> bool:
        nonlocal indexed
        if indexed >= stop_after:
            raise asyncio.CancelledError
        indexed += 1
        return await original_index(self, prepared, **kwargs)

    manager = _manager(config)
    assert manager._max_concurrent_file_indexes == 1
    KnowledgeManager._index_file_locked = _stop_at_ninety_percent  # type: ignore[method-assign]
    try:
        with pytest.raises(asyncio.CancelledError):
            await manager.reindex_all()
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

    assert (await _manager(config).reindex_all()).indexed_count == file_count - completed_after_interrupt

    remaining = file_count - completed_after_interrupt
    assert len(embedder.embedded_texts) == remaining, "resume embeds only the outstanding files"
    assert embedder.request_count < first_pass_requests / 5, "resume is far cheaper than a rebuild"
    # Exactly one collection survives: the candidate that became the published
    # index. Interrupted refreshes must not accumulate collections.
    assert _collection_names(config) == (checkpoint.collection,)
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

    assert (await _manager(config).reindex_all()).indexed_count == file_count

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

    async def _fail_two(self: KnowledgeManager, prepared: _PreparedFile, **kwargs: object) -> bool:
        if prepared.path.name in {"doc0003.md", "doc0004.md"}:
            return False
        return await original_index(self, prepared, **kwargs)

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
) -> None:
    """One failing file must not strand its siblings past the batch that owns them.

    ``asyncio.gather`` propagates the first exception while leaving the other
    coroutines running, so a straggler could append journal entries after the
    refresh's ``finally`` had already compacted and unlinked the journal --
    silently losing files that had genuinely finished.
    """
    (tmp_path / ".env").write_text("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY=4\n", encoding="utf-8")
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)

    slow_started = asyncio.Event()
    slow_finished = asyncio.Event()
    original_index = KnowledgeManager._index_file_locked

    async def _fail_fast_and_stall_others(
        self: KnowledgeManager,
        prepared: _PreparedFile,
        **kwargs: object,
    ) -> bool:
        if prepared.path.name == "doc0000.md":
            await slow_started.wait()
            msg = "explodes while siblings are still running"
            raise RuntimeError(msg)
        slow_started.set()
        await asyncio.sleep(0)
        indexed = await original_index(self, prepared, **kwargs)
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
    stored = _stored_paths(config, checkpoint.collection)
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

    assert (await _manager(config).reindex_all()).indexed_count == 40

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

    assert published_collection in _collection_names(config)


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
    published_rows = stored_rows(_collection(config, published_collection))
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

    assert run.collection.name != published_collection
    assert stored_rows(_collection(config, published_collection)) == published_rows
    checkpoint = load_candidate_checkpoint(storage)
    assert checkpoint is not None
    assert checkpoint.collection == run.collection.name


@pytest.mark.asyncio
async def test_an_unfinished_refresh_record_still_protects_the_collection_it_names(
    tmp_path: Path,
) -> None:
    """An unfinished record names the live collection, and cleanup keeps running.

    Skipping cleanup whenever a record is not a finished publication would be
    safe but would strand every abandoned candidate. The record still names the
    collection the last publication left live, so cleanup can tell the live
    index from the candidates it is there to reclaim.
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    state = _published_state(config, runtime_paths)
    assert state is not None
    published_collection = state.collection
    manager = _manager(config)
    abandoned_collection = f"{manager._collections.default_collection}_candidate_abandoned"
    _collection(config, abandoned_collection).reset()

    # A publication followed by a refresh that is running and has not published
    # again yet: the shape older versions wrote for every in-progress refresh.
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    save_published_index_state(
        published_index_metadata_path(key),
        PublishedIndexState(
            settings=state.settings,
            status="indexing",
            collection=published_collection,
            refresh_job="running",
            reason="refreshing",
        ),
    )
    assert load_published_index_state(published_index_metadata_path(key)) is not None

    await manager._open_candidate_run()

    assert published_collection in _collection_names(config)
    assert abandoned_collection not in _collection_names(config), (
        "cleanup was skipped instead of sparing the live index"
    )


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
    default_collection = manager._collections.default_collection
    _collection(config, default_collection).reset()
    _collection(config, "some_unrelated_collection").reset()

    with capture_logs() as logs:
        await manager._open_candidate_run()

    reported = [
        entry for entry in logs if entry["event"] == "Preserved knowledge collections with unprovable ownership"
    ]
    assert len(reported) == 1
    assert reported[0]["collections"] == ["some_unrelated_collection"]
    assert default_collection in _collection_names(config)


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

    async def _observe_status(self: KnowledgeManager, prepared: _PreparedFile, **kwargs: object) -> bool:
        indexed = await original_index(self, prepared, **kwargs)
        status = get_knowledge_index_status("docs", config=config, runtime_paths=runtime_paths)
        if status.candidate is not None:
            observed.append((status.candidate.total_files, status.candidate.completed_count))
            pending_samples.append(status.candidate.pending_count)
        return indexed

    KnowledgeManager._index_file_locked = _observe_status  # type: ignore[method-assign]
    try:
        assert (await _manager(config).reindex_all()).indexed_count == 6
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

    async def _fail_blocked(self: KnowledgeManager, prepared: _PreparedFile, **kwargs: object) -> bool:
        if prepared.path.name in blocked:
            return False
        return await original_index(self, prepared, **kwargs)

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
    assert _stored_paths(config, candidate_collection) == sorted(
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

    assert (await _manager(config).reindex_all()).indexed_count == 5

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

    assert (await _manager(config).reindex_all()).indexed_count == 16

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
    outcome = await manager.reindex_all()

    assert outcome.error is not None
    assert "embedder request failed (HTTP 400)" in outcome.error
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
    outcome = await manager.reindex_all()

    assert outcome.error is not None
    assert "Indexed 1 of 2" in outcome.error
    assert _published_state(config, runtime_paths) is None
    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    assert set(checkpoint.failed) == {"doc0000.md"}


def test_per_item_fallback_preserves_order_and_validates_dimensions() -> None:
    """Fallback maps each text to its own vector and refuses vectors of inconsistent width."""
    inner = _RecordingEmbedder()
    inner.short_batch = True
    embedder = RefreshEmbedder(inner)

    assert embedder.embed_texts(["alpha", "bb", "c"]) == {"alpha": [5.0, 1.0], "bb": [2.0, 1.0], "c": [1.0, 1.0]}
    assert embedder.batches() is False, "a failed probe retires batching for the run"
    assert inner.single_requests == ["alpha", "bb", "c"]

    widening = _RecordingEmbedder()
    widening.supports_batch = False
    embedder = RefreshEmbedder(widening)
    assert embedder.embed_each(["one"]) == [[3.0, 1.0]]
    original_single = widening.single_request
    widening.single_request = lambda text: [*original_single(text), 9.0]  # type: ignore[method-assign]
    # The width check guards the vectors a file writes, so a widened vector
    # cannot reach the collection, and batch embedding leaves it to that file.
    with pytest.raises(EmbedderRequestError, match="3-dimension vector, expected 2"):
        embedder.embed_each(["two"])
    assert embedder.embed_texts(["three"]) == {}


def test_empty_vector_is_rejected_on_every_path() -> None:
    """An empty vector never reaches a file's rows, whatever path produced it."""
    inner = _RecordingEmbedder()
    inner.supports_batch = False
    inner.single_request = lambda _text: []  # type: ignore[method-assign]
    embedder = RefreshEmbedder(inner)

    assert embedder.embed_texts(["anything"]) == {}, "batch embedding leaves it to the file's own write"
    with pytest.raises(EmbedderRequestError, match="empty vector"):
        embedder.embed_each(["anything"])


def test_batch_vectors_of_inconsistent_width_are_rejected() -> None:
    """A multi-input response whose vectors change width is refused rather than written."""
    inner = _RecordingEmbedder()
    inner.batch_request = lambda texts: [[1.0, 1.0], [1.0, 1.0, 1.0]][: len(texts)]  # type: ignore[method-assign]
    embedder = RefreshEmbedder(inner)

    with pytest.raises(EmbedderRequestError, match="3-dimension vector, expected 2"):
        embedder.embed_texts(["one", "two"])


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
    outcome = await manager.reindex_all()

    assert outcome.error is not None
    assert "Indexed 3 of 4" in outcome.error
    assert "embedder request failed (HTTP 422)" in outcome.error
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
    outcome = await manager.reindex_all()

    assert outcome.error is not None
    assert "embedder authentication failed (HTTP 401)" in outcome.error
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

    async def _fail_one(self: KnowledgeManager, prepared: _PreparedFile, **kwargs: object) -> bool:
        if prepared.path.name == "doc0005.md":
            return False
        return await original_index(self, prepared, **kwargs)

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

    assert (await _manager(config).reindex_all()).indexed_count == 1

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

    async def _fail_two(self: KnowledgeManager, prepared: _PreparedFile, **kwargs: object) -> bool:
        if prepared.path.name in {"doc0006.md", "doc0007.md"}:
            return False
        return await original_index(self, prepared, **kwargs)

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
async def test_vectors_batched_before_a_later_fallback_are_still_reused(
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
    original_batch = _RecordingEmbedder.batch_request
    calls = {"count": 0}

    def _short_after_first_batch(self: _RecordingEmbedder, texts: list[str]) -> list[list[float]]:
        calls["count"] += 1
        # First batch succeeds normally; the second reveals the broken capability.
        self.short_batch = calls["count"] > 1
        return original_batch(self, texts)

    monkeypatch.setattr(_RecordingEmbedder, "batch_request", _short_after_first_batch)

    assert (await _manager(config).reindex_all()).indexed_count == 12

    multi_input = [batch for batch in embedder.batch_requests if len(batch) > 1]
    assert len(multi_input) == 2, "batching stopped after the batch that proved it broken"
    # The first batch succeeded, so its files are written with its vectors and are
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
async def test_checkpoint_naming_a_published_collection_is_rejected_during_a_running_refresh(
    tmp_path: Path,
) -> None:
    """The published collection must never be reopened as a candidate.

    The guard used to compare only a name the strict parser dropped whenever
    the record was not a finished publication, while the live collection kept
    serving under it. A surviving checkpoint could then reopen the published
    collection and write candidate reconciliation into it.
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
    save_published_index_state(
        published_index_metadata_path(key),
        PublishedIndexState(
            settings=state.settings,
            status="indexing",
            collection=published_collection,
            refresh_job="running",
            reason="refreshing",
        ),
    )
    manager = _manager(config)
    save_candidate_checkpoint(
        _storage_path(config, runtime_paths),
        CandidateCheckpoint(collection=published_collection, settings=manager._indexing_settings),
    )

    run = await manager._open_candidate_run()

    assert run.checkpoint.collection != published_collection
    assert run.resumed is False
    assert published_collection in _collection_names(config)


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
    save_published_index_state(
        published_index_metadata_path(key),
        PublishedIndexState(
            settings=state.settings,
            status="indexing",
            collection=published_collection,
            refresh_job="running",
            reason="refreshing",
        ),
    )
    # A checkpoint naming the published collection, recorded under settings the
    # current runtime no longer matches.
    stale_settings = replace(_manager(config)._indexing_settings, embedder_model="text-embedding-3-large")
    save_candidate_checkpoint(
        _storage_path(config, runtime_paths),
        CandidateCheckpoint(collection=published_collection, settings=stale_settings),
    )

    run = await _manager(config)._open_candidate_run()

    assert published_collection in _collection_names(config), "the last good collection was deleted"
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

    async def _fail_last(self: KnowledgeManager, prepared: _PreparedFile, **kwargs: object) -> bool:
        if prepared.path.name == "doc0004.md":
            return False
        return await original_index(self, prepared, **kwargs)

    KnowledgeManager._index_file_locked = _fail_last  # type: ignore[method-assign]
    try:
        await _manager(config).reindex_all()
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    candidate_collection = checkpoint.collection
    assert len(checkpoint.completed) == 4
    vectors_before = len(stored_rows(_collection(config, candidate_collection)))

    # Same bytes, new mtimes: exactly what git checkout does.
    for index in range(5):
        path = docs_path / f"doc{index:04d}.md"
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns + 10**9, stat.st_mtime_ns + 10**9))
    embedder.embedded_texts.clear()

    assert (await _manager(config).reindex_all()).indexed_count == 1, "only the previously failed file is indexed"

    for index in range(4):
        assert embedder.embedded_count(f"content {index}") == 0, "an mtime change re-embedded unchanged content"
    assert len(stored_rows(_collection(config, candidate_collection))) == vectors_before + 1
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

    Providers without batch requests skip batch embedding entirely, so the
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
    outcome = await manager.reindex_all()

    assert outcome.error is not None
    assert "embedder authentication failed (HTTP 401)" in outcome.error
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
async def test_candidate_path_removal_is_batched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Dropping many paths must not cost one vector-store round trip each."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 40)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    original_index = KnowledgeManager._index_file_locked

    async def _fail_last(self: KnowledgeManager, prepared: _PreparedFile, **kwargs: object) -> bool:
        if prepared.path.name == "doc0039.md":
            return False
        return await original_index(self, prepared, **kwargs)

    KnowledgeManager._index_file_locked = _fail_last  # type: ignore[method-assign]
    try:
        await _manager(config).reindex_all()
    finally:
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

    # Rewrite every file's content so reconciliation must drop all 39 entries.
    for index in range(39):
        (docs_path / f"doc{index:04d}.md").write_text(f"rewritten {index}", encoding="utf-8")

    # Dropped paths leave the candidate through ``delete_source_paths``, so a
    # per-path loop shows up as one call per path.
    original_delete_paths = KnowledgeCollection.delete_source_paths
    deletes: list[int] = []

    def _count_deletes(self: KnowledgeCollection, relative_paths: Sequence[str]) -> None:
        deletes.append(len(relative_paths))
        original_delete_paths(self, relative_paths)

    monkeypatch.setattr(KnowledgeCollection, "delete_source_paths", _count_deletes)

    assert (await _manager(config).reindex_all()).indexed_count == 40

    # 39 dropped paths must not cost 39 round trips. Rewriting a file replaces
    # its rows in that file's own write, so only the batched drops count here.
    assert sum(deletes) >= 39, f"the dropped paths never left the candidate: {deletes}"
    assert len(deletes) <= 5, f"one delete per dropped path instead of batched: {deletes}"
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

    async def _fail_last(self: KnowledgeManager, prepared: _PreparedFile, **kwargs: object) -> bool:
        if prepared.path.name == "doc0007.md":
            return False
        return await original_index(self, prepared, **kwargs)

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
        KnowledgeManager._index_file_locked = original_index  # type: ignore[method-assign]

        async def _ignore_file(_file_path: Path) -> None:
            """Leave the journal to the explicit persist below, which is the append under test."""

        async def _ignore_batch() -> None:
            """Leave the compaction to the explicit call below."""

        assert (
            await manager._reindex_files_locked(
                [final_path],
                collection=run.collection,
                embedder=run.embedder,
                indexed_signatures=run.completed,
                vanished_files=run.vanished,
                on_file_result=_ignore_file,
                on_batch_complete=_ignore_batch,
            )
            == 1
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
    _collection(config, orphan).reset()
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
    assert orphan not in _collection_names(config), "orphan collection survived"
    assert published_collection in _collection_names(config)


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
    _collection(config, orphan).reset()
    storage = _storage_path(config, runtime_paths)
    save_candidate_checkpoint(
        storage,
        CandidateCheckpoint(
            collection=orphan,
            settings=_manager(config)._indexing_settings,
            completed={"doc0000.md": (1, 1, "digest")},
        ),
    )
    original_delete = KnowledgeCollection.delete
    attempts = 0

    def _fail_once(self: KnowledgeCollection) -> None:
        nonlocal attempts
        if self.name == orphan and attempts == 0:
            attempts += 1
            message = "collection is locked"
            raise RuntimeError(message)
        original_delete(self)

    monkeypatch.setattr(KnowledgeCollection, "delete", _fail_once)

    first = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert first.index_published is True
    assert load_candidate_checkpoint(storage) is not None
    assert orphan in _collection_names(config)

    second = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert second.index_published is True
    assert load_candidate_checkpoint(storage) is None
    assert orphan not in _collection_names(config)


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
    _collection(config, orphan).reset()
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


@pytest.mark.asyncio
async def test_each_file_is_read_once_per_refresh(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One read of each file feeds both its batch's embedding requests and its row write."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 5)
    config = _config(tmp_path, docs_path)
    reads: list[str | None] = []
    original_read = MarkdownReader.read

    def _record_read(self: MarkdownReader, file: Path, name: str | None = None) -> list[Document]:
        reads.append(name)
        return original_read(self, file, name=name)

    monkeypatch.setattr(MarkdownReader, "read", _record_read)

    assert (await _manager(config).reindex_all()).indexed_count == len(names)

    assert sorted(name or "" for name in reads) == names
    assert embedder.batch_requests == [tuple(f"content {index}" for index in range(len(names)))]
    assert embedder.single_requests == []


@pytest.mark.asyncio
async def test_reader_failure_fails_its_file_instead_of_passing_as_vanished(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reader raising ``ValueError`` fails its file; only the snapshot's errors mean a file vanished."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    original_read = MarkdownReader.read

    def _fail_one(self: MarkdownReader, file: Path, name: str | None = None) -> list[Document]:
        if name == "doc0001.md":
            message = "unparseable source"
            raise ValueError(message)
        return original_read(self, file, name=name)

    monkeypatch.setattr(MarkdownReader, "read", _fail_one)

    outcome = await _manager(config).reindex_all()

    assert not outcome.published
    assert outcome.error is not None
    assert "Indexed 2 of 3" in outcome.error
    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    assert set(checkpoint.failed) == {"doc0001.md"}
    assert checkpoint.failed["doc0001.md"].last_error == "knowledge indexing failed (ValueError)"


@pytest.mark.asyncio
async def test_an_empty_chunk_fails_only_its_own_file(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider rejecting empty input fails the file that produced it, not the batch it shares."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    embedder.failures[""] = [EmbedderRequestError("embedder request failed (HTTP 400)") for _ in range(10)]
    original_read = MarkdownReader.read

    def _add_empty_chunk(self: MarkdownReader, file: Path, name: str | None = None) -> list[Document]:
        documents = original_read(self, file, name=name)
        if name == "doc0001.md":
            documents.append(Document(name=name, content=""))
        return documents

    monkeypatch.setattr(MarkdownReader, "read", _add_empty_chunk)

    outcome = await _manager(config).reindex_all()

    assert outcome.error is not None
    assert "Indexed 2 of 3" in outcome.error
    assert embedder.batch_requests == [("content 0", "content 1", "content 2")]
    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    assert set(checkpoint.failed) == {"doc0001.md"}


@pytest.mark.asyncio
async def test_parsed_sources_share_batch_requests_with_text_sources(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Chunks from parsed formats are batched like text chunks instead of costing a request each."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "notes.md").write_text("prose worth batching", encoding="utf-8")
    (docs_path / "rows.csv").write_text("header,value\nalpha,1\nbeta,2\n", encoding="utf-8")
    config = _config(tmp_path, docs_path)

    assert (await _manager(config).reindex_all()).indexed_count == 2

    assert embedder.single_requests == []
    [request] = embedder.batch_requests
    assert "prose worth batching" in request
    assert any("alpha" in text for text in request)


@pytest.mark.asyncio
async def test_batch_text_is_bounded_by_bytes_not_only_file_count(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch closes once its chunk text reaches the budget, so peak memory does not scale with file size.

    Two 2,000-byte files exactly fill a 4,000-byte budget, so a comparison off
    by one at the boundary would put one or three files in a batch instead.
    """
    monkeypatch.setattr(knowledge_manager_module, "_MAX_BATCH_TEXT_BYTES", 4_000)
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    for index in range(20):
        (docs_path / f"big{index:02d}.md").write_text(f"{index:02d}" + "x" * 1_998, encoding="utf-8")
    config = _config(tmp_path, docs_path, chunk_size=100_000)

    assert (await _manager(config).reindex_all()).indexed_count == 20

    assert [len(request) for request in embedder.batch_requests] == [2] * 10
    assert embedder.single_requests == []


@pytest.mark.asyncio
async def test_oversized_file_closes_its_batch_and_still_indexes(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file larger than the whole budget is still indexed, without dragging later files into its batch."""
    monkeypatch.setattr(knowledge_manager_module, "_MAX_BATCH_TEXT_BYTES", 1_000)
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "huge.md").write_text("y" * 50_000, encoding="utf-8")
    for index in range(3):
        (docs_path / f"small{index}.md").write_text(f"small body {index}", encoding="utf-8")
    config = _config(tmp_path, docs_path, chunk_size=100_000)
    runtime_paths = runtime_paths_for(config)

    assert (await _manager(config).reindex_all()).indexed_count == 4

    # Files are indexed in path order, so the huge file fills the first batch alone.
    assert embedder.single_requests == ["y" * 50_000]
    assert embedder.batch_requests == [("small body 0", "small body 1", "small body 2")]
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.status == "complete"
    assert state.indexed_count == 4
    assert "huge.md" in _stored_paths(config, state.collection)


@pytest.mark.asyncio
async def test_batch_budget_counts_overlap_expansion_not_size_on_disk(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overlapping chunks repeat text, so the budget counts the chunk text a file produced, not its size."""
    monkeypatch.setattr(knowledge_manager_module, "_MAX_BATCH_TEXT_BYTES", 2_600)
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "expanding.md").write_text(_overlapping_body(250), encoding="utf-8")
    for index in range(3):
        (docs_path / f"small{index}.md").write_text(f"small body {index}", encoding="utf-8")
    config = _config(tmp_path, docs_path, chunk_size=1_000, chunk_overlap=100)
    runtime_paths = runtime_paths_for(config)

    assert (await _manager(config).reindex_all()).indexed_count == 4

    expanding_chunks, small_bodies = embedder.batch_requests
    assert (docs_path / "expanding.md").stat().st_size < 2_600
    assert sum(len(text.encode("utf-8")) for text in expanding_chunks) >= 2_600
    assert small_bodies == ("small body 0", "small body 1", "small body 2")
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.status == "complete"
    assert state.indexed_count == 4


@pytest.mark.asyncio
async def test_overlapping_chunks_are_batch_embedded_and_published(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Overlapping chunks embed in real multi-input requests and stay searchable."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "overlapped.md").write_text(_overlapping_body(500), encoding="utf-8")
    config = _config(tmp_path, docs_path, chunk_size=1_000, chunk_overlap=100)
    runtime_paths = runtime_paths_for(config)

    assert (await _manager(config).reindex_all()).indexed_count == 1

    assert [batch for batch in embedder.batch_requests if len(batch) > 1], "no multi-input request was issued"
    assert embedder.single_requests == [], "an overlapping chunk cost a request of its own"
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.status == "complete"
    assert state.indexed_count == 1
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.index is not None
    hits = await lookup.index.reader.asearch("token0000", limit=5)
    assert hits, "the published overlapping index returned nothing"
    assert all("token" in document.content for document in hits)


# --------------------------------------------------------------------------
# 14. Reusing published vectors for files a refresh did not change
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_mtime_ns", True),
        ("source_size", True),
        ("source_size", -1),
        ("source_digest", ""),
    ],
)
def test_reusable_row_signature_rejects_invalid_values(field: str, value: object) -> None:
    """Published metadata must satisfy the same signature contract as checkpoints."""
    metadata: dict[str, Any] = {
        "source_path": "doc.md",
        "source_mtime_ns": 1,
        "source_size": 1,
        "source_digest": "digest",
    }
    metadata[field] = value

    assert knowledge_manager_module._reusable_row_signature(metadata, frozenset({"doc.md"})) is None


@pytest.mark.asyncio
async def test_refresh_after_publish_reuses_vectors_for_unchanged_files(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Changing one file must cost one embedding, not one per file in the corpus."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)

    assert (await _manager(config).reindex_all()).indexed_count == 3
    first_state = _published_state(config, runtime_paths)
    assert first_state is not None
    first_collection = first_state.collection
    assert first_collection is not None
    published_ids = {row["id"] for row in stored_rows(_collection(config, first_collection))}

    (docs_path / names[1]).write_text("rewritten body", encoding="utf-8")
    embedder.embedded_texts.clear()

    assert (await _manager(config).reindex_all()).indexed_count == 1, "the whole corpus was embedded again"

    assert embedder.embedded_count("content 0") == 0
    assert embedder.embedded_count("content 2") == 0
    assert embedder.embedded_count("rewritten body") == 1
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.status == "complete"
    assert state.indexed_count == 3
    assert state.collection is not None
    assert state.collection != first_collection, "publication must still swap in a separate collection"
    assert _stored_paths(config, state.collection) == sorted(names)
    reused_ids = {
        row["id"]
        for row in stored_rows(_collection(config, state.collection))
        if row["metadata"]["source_path"] != names[1]
    }
    assert reused_ids <= published_ids, "copied chunks must keep their published ids"


@pytest.mark.asyncio
async def test_published_vector_reuse_leaves_the_published_collection_untouched(
    tmp_path: Path,
) -> None:
    """The published index serves live queries throughout; the copy may only read it."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)

    await _manager(config).reindex_all()
    first_state = _published_state(config, runtime_paths)
    assert first_state is not None
    first_collection = first_state.collection
    assert first_collection is not None
    before = stored_rows(_collection(config, first_collection))

    (docs_path / names[0]).write_text("rewritten body", encoding="utf-8")
    run = await _manager(config)._open_candidate_run()

    assert run.checkpoint.collection != first_collection
    assert stored_rows(_collection(config, first_collection)) == before, "the live published collection was mutated"


@pytest.mark.asyncio
async def test_published_vector_reuse_requires_matching_indexing_settings(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Settings pin the chunker and the embedder, so a change invalidates every vector."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)

    await _manager(config).reindex_all()

    rechunked = _config(tmp_path, docs_path, chunk_size=256, chunk_overlap=8)
    embedder.embedded_texts.clear()

    assert (await _manager(rechunked).reindex_all()).indexed_count == 3, (
        "vectors built under other settings were reused"
    )
    assert embedder.embedded_count("content 0") == 1


@pytest.mark.asyncio
async def test_published_vector_reuse_skips_paths_that_left_the_corpus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A published path that is no longer managed must never enter the candidate."""
    # One row per page, so the removed file's row is the only row of its page
    # and the copy has to cope with a page that contributes nothing.
    original_copy = KnowledgeCollection.copy_rows

    def _copy_one_row_per_page(
        self: KnowledgeCollection,
        source: KnowledgeCollection,
        *,
        select: Callable[[Mapping[str, Any]], bool],
        page_rows: int = 500,
    ) -> None:
        del page_rows
        original_copy(self, source, select=select, page_rows=1)

    monkeypatch.setattr(KnowledgeCollection, "copy_rows", _copy_one_row_per_page)
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)

    await _manager(config).reindex_all()
    (docs_path / names[0]).unlink()

    run = await _manager(config)._open_candidate_run()

    assert sorted(run.completed) == [names[1], names[2]]
    assert _stored_paths(config, run.checkpoint.collection) == [names[1], names[2]]


@pytest.mark.asyncio
async def test_published_vector_reuse_needs_a_completely_published_index(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Only a collection the metadata calls published is provably finished and stable.

    The rest of the candidate lifecycle treats any other collection as an
    abandoned candidate it may reclaim, so copying from one would read a
    collection nothing keeps alive.
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)

    await _manager(config).reindex_all()
    state = _published_state(config, runtime_paths)
    assert state is not None
    save_published_index_state(
        published_index_metadata_path(resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)),
        PublishedIndexState(
            settings=state.settings,
            status="indexing",
            collection=state.collection,
            indexed_count=state.indexed_count,
            source_signature=state.source_signature,
        ),
    )
    embedder.embedded_texts.clear()

    assert (await _manager(config).reindex_all()).indexed_count == 3, (
        "vectors from an unpublished collection were reused"
    )


@pytest.mark.asyncio
async def test_published_vector_reuse_never_claims_a_path_the_published_index_cannot_serve(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A path is claimed by the rows copied for it, never by the corpus listing.

    Copied paths skip the vector-existence probe because the copy already
    proved them, so a path claimed without rows would publish a file that
    semantic search cannot find.
    """
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)

    await _manager(config).reindex_all()
    first_state = _published_state(config, runtime_paths)
    assert first_state is not None
    assert first_state.collection is not None
    _collection(config, first_state.collection).delete_source_paths([names[2]])
    embedder.embedded_texts.clear()

    assert (await _manager(config).reindex_all()).indexed_count == 1

    assert embedder.embedded_count("content 2") == 1, "a path with no published rows was claimed as indexed"
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.collection is not None
    assert _stored_paths(config, state.collection) == sorted(names)


@pytest.mark.asyncio
async def test_copied_paths_skip_the_vector_existence_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The copy already reported which paths received rows, so none is probed for vectors again."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 12)
    config = _config(tmp_path, docs_path)

    await _manager(config).reindex_all()
    probed: list[str] = []
    original_probe = KnowledgeCollection.paths_with_vectors

    def _record_probe(self: KnowledgeCollection, relative_paths: Sequence[str]) -> set[str]:
        probed.extend(relative_paths)
        return original_probe(self, relative_paths)

    monkeypatch.setattr(KnowledgeCollection, "paths_with_vectors", _record_probe)
    (docs_path / names[0]).write_text("rewritten body", encoding="utf-8")

    assert (await _manager(config).reindex_all()).indexed_count == 1

    assert probed == [], "copied paths were sent back through the vector-existence probe"


@pytest.mark.asyncio
async def test_published_vector_reuse_falls_back_when_the_published_collection_is_gone(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A vanished published collection must cost a full rebuild, never a failed refresh."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)

    await _manager(config).reindex_all()
    first_state = _published_state(config, runtime_paths)
    assert first_state is not None
    assert first_state.collection is not None
    _collection(config, first_state.collection).delete()
    embedder.embedded_texts.clear()

    assert (await _manager(config).reindex_all()).indexed_count == 3

    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.status == "complete"
    assert state.indexed_count == 3


@pytest.mark.asyncio
async def test_a_failed_vector_copy_leaves_no_partial_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy that dies part-way must hand the rebuild an empty candidate.

    Rows copied for a path the candidate never records are invisible to
    reconciliation, so a later listing that drops that path would publish
    vectors for a file the corpus no longer contains.
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)

    await _manager(config).reindex_all()
    original_copy = KnowledgeCollection.copy_rows
    landed: list[dict[str, Any]] = []

    def _refuse_after_first_page(
        self: KnowledgeCollection,
        source: KnowledgeCollection,
        *,
        select: Callable[[Mapping[str, Any]], bool],
        page_rows: int = 500,
    ) -> None:
        del page_rows
        pages = 0

        def _select(metadata: Mapping[str, Any]) -> bool:
            nonlocal pages
            if pages:
                landed.extend(stored_rows(self))
                message = "vector store refused the write"
                raise RuntimeError(message)
            pages += 1
            return select(metadata)

        original_copy(self, source, select=_select, page_rows=1)

    monkeypatch.setattr(KnowledgeCollection, "copy_rows", _refuse_after_first_page)

    run = await _manager(config)._open_candidate_run()

    assert landed, "the copy failed before writing a row, so this proves nothing"
    assert run.completed == {}, "a failed copy must claim nothing"
    assert stored_rows(run.collection) == [], "partially copied rows survived the failure"


@pytest.mark.asyncio
async def test_published_vector_reuse_survives_a_checkout_that_only_rewrites_mtimes(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Copied rows carry the published mtime, which a checkout routinely invalidates.

    Reconciliation adopts the new stamp for byte-identical content, so the
    signature the copy records must survive that comparison rather than send
    every reused file back through the embedder.
    """
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)

    await _manager(config).reindex_all()
    (docs_path / names[1]).write_text("rewritten body", encoding="utf-8")
    for name in names:
        path = docs_path / name
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns + 10**9, stat.st_mtime_ns + 10**9))
    embedder.embedded_texts.clear()

    assert (await _manager(config).reindex_all()).indexed_count == 1, "an mtime rewrite re-embedded unchanged content"

    assert embedder.embedded_count("content 0") == 0
    assert embedder.embedded_count("content 2") == 0
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.collection is not None
    assert _stored_paths(config, state.collection) == sorted(names)


@pytest.mark.asyncio
async def test_published_vector_reuse_rescans_an_empty_file_it_cannot_claim(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A zero-length file legitimately has no vectors, so no copy can prove it indexed."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 2)
    (docs_path / "empty.md").write_text("", encoding="utf-8")
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)

    await _manager(config).reindex_all()
    (docs_path / names[0]).write_text("rewritten body", encoding="utf-8")
    embedder.embedded_texts.clear()

    assert (await _manager(config).reindex_all()).indexed_count == 2, "the empty file was claimed or lost"

    assert embedder.embedded_count("content 1") == 0
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.status == "complete"
    assert state.indexed_count == 3
    assert state.collection is not None
    assert _stored_paths(config, state.collection) == sorted(names)


@pytest.mark.asyncio
async def test_shielded_write_finishes_before_repeated_cancellation_escapes() -> None:
    """A second cancellation must not interrupt the drain of a durable write."""
    started = asyncio.Event()
    release = asyncio.Event()
    finished = False

    async def _write() -> None:
        nonlocal finished
        started.set()
        await release.wait()
        finished = True

    outer = asyncio.create_task(knowledge_manager_module._shielded_write(_write()))
    await started.wait()
    try:
        outer.cancel()
        await asyncio.sleep(0)
        outer.cancel()
        await asyncio.sleep(0)
        assert not outer.done(), "repeated cancellation escaped before the durable write finished"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert finished


@pytest.mark.asyncio
async def test_shielded_write_keeps_cancellation_authoritative_when_the_write_fails() -> None:
    """A failed recoverable write must not replace the caller's cancellation."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _write() -> None:
        started.set()
        await release.wait()
        msg = "recoverable write failed"
        raise RuntimeError(msg)

    outer = asyncio.create_task(knowledge_manager_module._shielded_write(_write()))
    await started.wait()
    outer.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await outer


@pytest.mark.asyncio
async def test_a_cancelled_vector_copy_never_publishes_rows_it_could_not_claim(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation must wait for the copy worker before releasing the refresh lock.

    ``asyncio.to_thread`` cannot cancel its worker, so the coroutine must drain
    that worker before propagating ``CancelledError``. Claims intentionally
    remain empty after cancellation; shape recovery then rebuilds the completed
    copy before a departed path can reach publication.
    """
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)

    await _manager(config).reindex_all()
    original_copy = KnowledgeManager._copy_published_vectors
    copy_started = Event()
    release_copy = Event()

    def _blocked_copy(
        self: KnowledgeManager,
        *,
        published_collection: str,
        candidate: KnowledgeCollection,
        managed_paths: frozenset[str],
    ) -> dict[str, FileSignature]:
        copy_started.set()
        release_copy.wait()
        return original_copy(
            self,
            published_collection=published_collection,
            candidate=candidate,
            managed_paths=managed_paths,
        )

    monkeypatch.setattr(KnowledgeManager, "_copy_published_vectors", _blocked_copy)
    refresh = asyncio.create_task(_manager(config).reindex_all())
    assert await asyncio.to_thread(copy_started.wait, 5), "the vector copy never started"
    try:
        refresh.cancel()
        await asyncio.sleep(0)
        assert not refresh.done(), "the refresh lock escaped while its copy worker was still running"
    finally:
        release_copy.set()
    with pytest.raises(asyncio.CancelledError):
        await refresh
    monkeypatch.setattr(KnowledgeManager, "_copy_published_vectors", original_copy)

    interrupted = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert interrupted is not None
    assert interrupted.completed == {}, "a cancelled copy must claim nothing"
    assert _stored_paths(config, interrupted.collection) == sorted(names), "the copy did write rows before cancelling"

    (docs_path / names[0]).unlink()
    embedder.embedded_texts.clear()

    indexed = (await _manager(config).reindex_all()).indexed_count

    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.status == "complete"
    assert state.collection is not None
    assert _stored_paths(config, state.collection) == sorted(names[1:]), (
        "a departed path survived in the published index"
    )
    # Recovery rebuilds the collection, so it must copy again rather than fall
    # back to embedding: losing a corpus copy is the bug's secondary cost.
    assert indexed == 0, "recovery re-embedded instead of copying the published vectors again"


@pytest.mark.asyncio
async def test_a_candidate_whose_collection_vanished_is_rebuilt_rather_than_resumed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resume probes that its collection still exists instead of trusting the checkpoint's claims."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    original_index = KnowledgeManager._index_file_locked

    async def _fail_last(self: KnowledgeManager, prepared: _PreparedFile, **kwargs: object) -> bool:
        if prepared.path.name == names[-1]:
            return False
        return await original_index(self, prepared, **kwargs)

    monkeypatch.setattr(KnowledgeManager, "_index_file_locked", _fail_last)
    await _manager(config).reindex_all()
    monkeypatch.setattr(KnowledgeManager, "_index_file_locked", original_index)

    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    assert len(checkpoint.completed) == 2, "the candidate should have kept the files it did index"
    _collection(config, checkpoint.collection).delete()

    run = await _manager(config)._open_candidate_run()

    assert run.resumed is False
    assert run.completed == {}, "claims survived a collection that no longer holds their vectors"


def _overwrite_with_garbage(file: Path) -> None:
    """Replace a collection file with bytes SQLite does not recognize as a database."""
    file.write_bytes(b"not a database" * 100)


def _corrupt_schema_page(file: Path) -> None:
    """Overwrite the first page after its header, so SQLite reports a malformed database image."""
    data = bytearray(file.read_bytes())
    page_size = int.from_bytes(data[16:18], "big")
    data[100:page_size] = b"\xff" * (page_size - 100)
    file.write_bytes(bytes(data))


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [_overwrite_with_garbage, _corrupt_schema_page], ids=["notadb", "corrupt"])
async def test_an_unreadable_candidate_is_discarded_without_touching_the_published_index(
    tmp_path: Path,
    corrupt: Callable[[Path], None],
) -> None:
    """A corrupt unpublished collection file must not permanently block refresh."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    manager = _manager(config)
    await manager.reindex_all()
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.collection is not None
    published_collection = state.collection
    published_rows = stored_rows(_collection(config, published_collection))

    unreadable_candidate = candidate_collection_name(manager._collections)
    seed_collection(_collection(config, unreadable_candidate), published_rows)
    save_candidate_checkpoint(
        _storage_path(config, runtime_paths),
        CandidateCheckpoint(
            collection=unreadable_candidate,
            settings=manager._indexing_settings,
            completed={names[0]: (1, 1, "digest")},
        ),
    )
    corrupt(_collection(config, unreadable_candidate).file)

    run = await manager._open_candidate_run()

    assert run.resumed is False
    assert run.checkpoint.collection != unreadable_candidate
    assert sorted(run.completed) == names
    assert unreadable_candidate not in _collection_names(config)
    assert stored_rows(_collection(config, published_collection)) == published_rows


@pytest.mark.asyncio
async def test_an_unreadable_candidate_is_not_deleted_when_its_checkpoint_cannot_be_retired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup failure must leave the candidate and published index recoverable."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 1)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    manager = _manager(config)
    await manager.reindex_all()
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.collection is not None
    published_collection = state.collection
    published_rows = stored_rows(_collection(config, published_collection))

    unreadable_candidate = candidate_collection_name(manager._collections)
    seed_collection(_collection(config, unreadable_candidate), published_rows)
    save_candidate_checkpoint(
        _storage_path(config, runtime_paths),
        CandidateCheckpoint(collection=unreadable_candidate, settings=manager._indexing_settings),
    )
    _overwrite_with_garbage(_collection(config, unreadable_candidate).file)

    def _fail_checkpoint_cleanup(_base_storage_path: Path) -> None:
        message = "checkpoint directory is read-only"
        raise PermissionError(message)

    monkeypatch.setattr(knowledge_manager_module, "delete_candidate_checkpoint", _fail_checkpoint_cleanup)

    with pytest.raises(RuntimeError, match="Cannot retire unreadable knowledge candidate checkpoint"):
        await manager._open_candidate_run()

    assert unreadable_candidate in _collection_names(config)
    assert stored_rows(_collection(config, published_collection)) == published_rows


@pytest.mark.asyncio
async def test_a_transient_candidate_probe_failure_preserves_resumable_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrelated vector-store failure is not evidence of index corruption."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 1)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    manager = _manager(config)
    await manager.reindex_all()
    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.collection is not None
    published_collection = state.collection
    published_rows = stored_rows(_collection(config, published_collection))

    candidate = candidate_collection_name(manager._collections)
    seed_collection(_collection(config, candidate), published_rows)
    storage = _storage_path(config, runtime_paths)
    save_candidate_checkpoint(
        storage,
        CandidateCheckpoint(collection=candidate, settings=manager._indexing_settings),
    )
    original_has_rows = KnowledgeCollection.has_rows

    def _fail_transient_probe(self: KnowledgeCollection) -> bool:
        if self.name == candidate:
            message = "database is locked"
            raise sqlite3.OperationalError(message)
        return original_has_rows(self)

    monkeypatch.setattr(KnowledgeCollection, "has_rows", _fail_transient_probe)

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        await manager._open_candidate_run()

    checkpoint = load_candidate_checkpoint(storage)
    assert checkpoint is not None
    assert checkpoint.collection == candidate
    assert candidate in _collection_names(config)
    assert stored_rows(_collection(config, published_collection)) == published_rows


@pytest.mark.asyncio
async def test_unclaimed_row_probe_constructs_no_embedder(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty-checkpoint probe reads rows without an embedder; only the run constructs one."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    manager = _manager(config)
    collection = candidate_collection_name(manager._collections)
    save_candidate_checkpoint(
        _storage_path(config, runtime_paths),
        CandidateCheckpoint(collection=collection, settings=manager._indexing_settings),
    )
    _collection(config, collection).reset()
    created: list[Embedder] = []

    def _record_factory(*_args: object, **_kwargs: object) -> Embedder:
        created.append(embedder)
        return embedder

    monkeypatch.setattr(knowledge_manager_module, "create_configured_embedder", _record_factory)

    run = await manager._open_candidate_run()

    assert run.resumed is True
    assert len(created) == 1, "the shape probe constructed another configured embedder"


@pytest.mark.asyncio
async def test_an_interruption_after_the_copy_still_resumes_the_work_it_finished(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A candidate that holds only rows it claims stays resumable.

    Rebuilding on sight of unclaimed rows is what keeps a cancelled copy from
    publishing them, but the test for that has to be narrow: a check that
    condemned any candidate holding vectors would throw away every file the
    refresh went on to embed.
    """
    (tmp_path / ".env").write_text("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY=1\n", encoding="utf-8")
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    manager = _manager(config)
    assert manager._max_concurrent_file_indexes == 1
    await manager.reindex_all()

    for index, name in enumerate(names):
        (docs_path / name).write_text(f"revised {index}", encoding="utf-8")
    embedder.embedded_texts.clear()

    indexed: list[str] = []
    original_index = KnowledgeManager._index_file_locked

    async def _stop_after_two(self: KnowledgeManager, prepared: _PreparedFile, **kwargs: object) -> bool:
        if len(indexed) >= 2:
            raise asyncio.CancelledError
        indexed.append(prepared.path.name)
        return await original_index(self, prepared, **kwargs)

    monkeypatch.setattr(KnowledgeManager, "_index_file_locked", _stop_after_two)
    with pytest.raises(asyncio.CancelledError):
        await _manager(config).reindex_all()
    monkeypatch.setattr(KnowledgeManager, "_index_file_locked", original_index)

    checkpoint = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert checkpoint is not None
    assert len(checkpoint.completed) == 2
    already_embedded = {f"revised {names.index(name)}" for name in checkpoint.completed}

    assert (await _manager(config).reindex_all()).indexed_count == len(names) - 2

    for body in already_embedded:
        assert embedder.embedded_count(body) == 1, "an interrupted refresh re-embedded work it had finished"


@pytest.mark.asyncio
async def test_a_candidate_with_no_vectors_and_no_claims_is_still_resumed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claiming nothing is not itself a violation; holding unclaimed rows is.

    A candidate whose every file failed to index claims nothing and holds no
    vectors, which satisfies the invariant. Condemning it on the empty claim
    map alone would rebuild it on every refresh and reset the retry ledger that
    records how many attempts each file has cost.
    """
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)

    async def _fail_everything(_self: KnowledgeManager, prepared: _PreparedFile, **kwargs: object) -> bool:
        _ = (prepared, kwargs)
        return False

    monkeypatch.setattr(KnowledgeManager, "_index_file_locked", _fail_everything)
    await _manager(config).reindex_all()
    first = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert first is not None
    assert first.completed == {}
    assert sorted(first.failed) == sorted(names)
    assert _stored_paths(config, first.collection) == []

    await _manager(config).reindex_all()

    second = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert second is not None
    assert second.collection == first.collection, "an empty candidate was needlessly rebuilt"
    assert [second.failed[name].attempts for name in sorted(names)] == [2, 2], "the retry ledger was reset"


@pytest.mark.asyncio
async def test_a_copy_records_its_claims_only_after_every_row_has_landed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery is sound only because a copy's claims land in one write, last.

    An interrupted copy is recoverable because it leaves exactly one shape: no
    claims and a non-empty collection. A copy that recorded claims as it went
    would instead leave a claim covering only part of a file, which
    ``_candidate_paths_missing_vectors`` confirms on sight -- one row is enough
    -- so the candidate would publish a silently truncated file.

    Nothing else in the design notices that, and every other test here would
    stay green, so this is the pin. Chunks are paged one row at a time to give
    a per-page write somewhere to happen.
    """
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    await _manager(config).reindex_all()

    events: list[str] = []
    original_save = knowledge_manager_module.save_candidate_checkpoint
    original_copy = KnowledgeCollection.copy_rows

    def _record_save(base_storage_path: Path, checkpoint: CandidateCheckpoint) -> CandidateCheckpoint:
        events.append(f"claims:{len(checkpoint.completed)}")
        return original_save(base_storage_path, checkpoint)

    def _record_copy(
        self: KnowledgeCollection,
        source: KnowledgeCollection,
        *,
        select: Callable[[Mapping[str, Any]], bool],
        page_rows: int = 500,
    ) -> None:
        del page_rows
        original_copy(self, source, select=select, page_rows=1)
        # Every row the copy wrote has landed by now; a claim saved from inside
        # the copy, or between copies, lands ahead of these.
        events.extend("rows" for _row in stored_rows(self))

    monkeypatch.setattr(knowledge_manager_module, "save_candidate_checkpoint", _record_save)
    monkeypatch.setattr(KnowledgeCollection, "copy_rows", _record_copy)
    (docs_path / names[0]).write_text("rewritten body", encoding="utf-8")

    await _manager(config).reindex_all()

    copied_rows = [index for index, event in enumerate(events) if event == "rows"]
    recorded_claims = [
        index for index, event in enumerate(events) if event.startswith("claims:") and event != "claims:0"
    ]
    assert copied_rows, "the copy wrote no rows, so this proves nothing"
    assert recorded_claims, "the copy never recorded its claims"
    assert min(recorded_claims) > max(copied_rows), "claims were recorded before every copied row had landed"


@pytest.mark.asyncio
async def test_a_rebuild_that_dies_before_emptying_the_collection_still_recovers(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dying part-way through a rebuild must not leave a resumable-looking candidate.

    A rebuild empties the collection and then records what replaced it, so a
    crash between the two leaves rows the checkpoint does not account for --
    the same shape a cancelled copy leaves, reached from the other direction.
    """
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await _manager(config).reindex_all()

    cancel_copy = True
    original_copy = KnowledgeManager._copy_published_vectors

    def _copy_then_maybe_cancel(
        self: KnowledgeManager,
        *,
        published_collection: str,
        candidate: KnowledgeCollection,
        managed_paths: frozenset[str],
    ) -> dict[str, FileSignature]:
        reused = original_copy(
            self,
            published_collection=published_collection,
            candidate=candidate,
            managed_paths=managed_paths,
        )
        if cancel_copy:
            raise asyncio.CancelledError
        return reused

    monkeypatch.setattr(KnowledgeManager, "_copy_published_vectors", _copy_then_maybe_cancel)
    with pytest.raises(asyncio.CancelledError):
        await _manager(config).reindex_all()
    cancel_copy = False

    (docs_path / names[0]).unlink()
    die_before_reset = True
    original_reset = KnowledgeCollection.reset

    def _maybe_die(self: KnowledgeCollection) -> None:
        if die_before_reset:
            message = "host lost power before the collection was emptied"
            raise RuntimeError(message)
        original_reset(self)

    monkeypatch.setattr(KnowledgeCollection, "reset", _maybe_die)
    with pytest.raises(RuntimeError):
        await _manager(config).reindex_all()
    die_before_reset = False

    interrupted = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert interrupted is not None
    assert interrupted.completed == {}
    assert _stored_paths(config, interrupted.collection) == sorted(names), "the crash left rows nothing claims"
    embedder.embedded_texts.clear()

    indexed = (await _manager(config).reindex_all()).indexed_count

    state = _published_state(config, runtime_paths)
    assert state is not None
    assert state.collection is not None
    assert _stored_paths(config, state.collection) == sorted(names[1:]), (
        "a departed path survived in the published index"
    )
    assert indexed == 0, "recovery re-embedded instead of copying the published vectors again"
    assert embedder.embedded_texts == []


@pytest.mark.asyncio
async def test_forced_reindex_discards_a_candidate_that_survived_a_failed_refresh(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh that never published leaves a candidate, and forcing must drop it too.

    Suppressing the copy is not enough: a surviving candidate carries its own
    claims, so the files it already embedded would be kept by the very rebuild
    the operator asked for because the index is not trusted.
    """
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    for index, name in enumerate(names):
        (docs_path / name).write_text(f"revised {index}", encoding="utf-8")
    fail_last = True
    original_index = KnowledgeManager._index_file_locked

    async def _maybe_fail_last(self: KnowledgeManager, prepared: _PreparedFile, **kwargs: object) -> bool:
        if fail_last and prepared.path.name == names[-1]:
            return False
        return await original_index(self, prepared, **kwargs)

    monkeypatch.setattr(KnowledgeManager, "_index_file_locked", _maybe_fail_last)
    failed = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert failed.index_published is False
    surviving = load_candidate_checkpoint(_storage_path(config, runtime_paths))
    assert surviving is not None
    assert len(surviving.completed) == 2, "the candidate should carry the work the failed refresh finished"

    fail_last = False
    embedder.embedded_texts.clear()

    result = await refresh_knowledge_binding(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
        force_reindex=True,
    )

    assert result.index_published is True
    assert result.indexed_count == 3, "a forced rebuild kept the candidate's claims"
    for index in range(3):
        assert embedder.embedded_count(f"revised {index}") == 1


@pytest.mark.asyncio
async def test_forced_reindex_rebuilds_every_vector_instead_of_copying_it(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """An operator forces a rebuild because the index is not trusted, so reuse must stop.

    Every other reason a rebuild is forced -- changed settings, a missing
    collection -- the reuse gates already reject. This one they cannot see.
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    embedder.embedded_texts.clear()

    result = await refresh_knowledge_binding(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
        force_reindex=True,
    )

    assert result.index_published is True
    assert result.indexed_count == 3, "a forced rebuild reused the vectors it was asked to replace"
    for index in range(3):
        assert embedder.embedded_count(f"content {index}") == 1


@pytest.mark.asyncio
async def test_local_index_built_before_prompted_embedding_is_rebuilt_not_reused(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local-model vectors from before query and document prompts are incompatible, so none survive a refresh."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    local_embedder = {"provider": "sentence_transformers", "config": {"model": SENTENCE_TRANSFORMERS_DEFAULT}}
    config = _config(tmp_path, docs_path, memory={"embedder": local_embedder})
    runtime_paths = runtime_paths_for(config)
    # Publish under the identity local vectors had before the prompts: the plain model, like Mem0's.
    monkeypatch.setattr(
        knowledge_indexing_config,
        "effective_knowledge_embedder_signature",
        effective_mem0_embedder_signature,
    )
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    monkeypatch.setattr(
        knowledge_indexing_config,
        "effective_knowledge_embedder_signature",
        effective_knowledge_embedder_signature,
    )
    embedder.embedded_texts.clear()

    assert knowledge_registry.get_published_index("docs", config=config, runtime_paths=runtime_paths).index is None
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert result.indexed_count == 3, "the refresh reused vectors embedded without the model's prompts"
    for index in range(3):
        assert embedder.embedded_count(f"content {index}") == 1


# --------------------------------------------------------------------------
# Vector verification and collection ownership
# --------------------------------------------------------------------------


def test_vector_verification_answers_an_empty_batch_without_opening_the_collection(tmp_path: Path) -> None:
    """No paths has an answer the store need not give, even for a collection that does not exist."""
    assert KnowledgeCollection(path=tmp_path, name="never_created").paths_with_vectors([]) == set()
    assert list_collection_names(tmp_path) == ()


def test_collection_ownership_is_derived_from_identity_not_supplied() -> None:
    """The name cleanup deletes by must be computed from the base's identity.

    ``default_collection`` is what proves a collection is this base's to delete,
    so it is derived from ``base_id`` and the resolved source path rather than
    accepted as a field. A space that could be handed the name could authorize
    deleting a collection the base does not own.
    """
    docs = Path("/srv/knowledge/docs")
    space = CollectionSpace(
        base_id="docs",
        knowledge_path=docs,
        storage_path=Path("/srv/state/docs"),
    )

    assert space.default_collection == _collection_name_for_base("docs", docs)
    assert "default_collection" not in CollectionSpace.__dataclass_fields__, (
        "default_collection is a constructor field again, so it can be supplied instead of derived"
    )

    other_base = replace(space, base_id="wiki")
    other_path = replace(space, knowledge_path=Path("/srv/knowledge/wiki"))
    assert other_base.default_collection != space.default_collection
    assert other_path.default_collection != space.default_collection


# --------------------------------------------------------------------------
# 18. Resuming the collection a publication retired instead of copying the index
# --------------------------------------------------------------------------


class _Crash(BaseException):
    """A process death at one chosen point of a refresh: no ``except Exception`` handler may absorb it."""


@dataclass
class _StoreWrites:
    """Rows written into the real store, counted by thin wrappers around its write methods."""

    #: Rows copied from another collection since the last ``clear``.
    copied: int = 0
    #: Rows written for indexed files since the last ``clear``.
    file_rows: int = 0
    lock: Lock = field(default_factory=Lock)

    def clear(self) -> None:
        with self.lock:
            self.copied = 0
            self.file_rows = 0

    def record(self, rows: int, *, copied: bool) -> None:
        with self.lock:
            if copied:
                self.copied += rows
            else:
                self.file_rows += rows


@pytest.fixture
def store_writes(monkeypatch: pytest.MonkeyPatch) -> _StoreWrites:
    """Count the rows refreshes write into the real store, by how they write them."""
    writes = _StoreWrites()
    original_write = KnowledgeCollection.write_source_rows
    original_copy = KnowledgeCollection.copy_rows

    def _write(self: KnowledgeCollection, relative_path: str, rows: Sequence[StoredRow]) -> None:
        original_write(self, relative_path, rows)
        writes.record(len(rows), copied=False)

    def _copy(
        self: KnowledgeCollection,
        source: KnowledgeCollection,
        *,
        select: Callable[[Mapping[str, Any]], bool],
        page_rows: int = 500,
    ) -> None:
        def _counting_select(metadata: Mapping[str, Any]) -> bool:
            selected = select(metadata)
            if selected:
                writes.record(1, copied=True)
            return selected

        original_copy(self, source, select=_counting_select, page_rows=page_rows)

    monkeypatch.setattr(KnowledgeCollection, "write_source_rows", _write)
    monkeypatch.setattr(KnowledgeCollection, "copy_rows", _copy)
    return writes


def _served(config: Config, collection: str) -> list[tuple[str, str]]:
    """Return what one collection serves, as sorted ``(source path, text)`` rows."""
    return sorted(
        (row["metadata"]["source_path"], row["content"]) for row in stored_rows(_collection(config, collection))
    )


def _corpus(docs_path: Path) -> list[tuple[str, str]]:
    """Return what an exact index of the files on disk serves: one row per file, holding its text."""
    return sorted((path.name, path.read_text(encoding="utf-8")) for path in docs_path.iterdir())


def _published_collection(config: Config) -> str:
    state = _published_state(config, runtime_paths_for(config))
    assert state is not None
    assert state.status == "complete"
    assert state.collection is not None
    return state.collection


async def _refresh_and_check(config: Config, docs_path: Path) -> str:
    """Refresh, assert the new publication serves exactly the files on disk, and return it."""
    assert (await _manager(config).reindex_all()).published
    published = _published_collection(config)
    assert _served(config, published) == _corpus(docs_path), "the publication lost, duplicated or kept stale rows"
    return published


async def _build_steady_state(config: Config, docs_path: Path, names: Sequence[str]) -> None:
    """Build cold, then publish twice, so the next refresh has a retired collection to resume."""
    await _refresh_and_check(config, docs_path)
    for step in range(2):
        (docs_path / names[step]).write_text(f"warm-up change {step}", encoding="utf-8")
        await _refresh_and_check(config, docs_path)


@pytest.mark.asyncio
async def test_a_refresh_writes_only_the_rows_that_changed_once_a_retired_collection_is_kept(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
    store_writes: _StoreWrites,
) -> None:
    """A small change must cost rows for that change, not a copy of every published row."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 40)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    cold = await _refresh_and_check(config, docs_path)
    cold_rows = _rows_by_id(config, cold)
    (docs_path / names[0]).write_text("first change", encoding="utf-8")
    second = await _refresh_and_check(config, docs_path)
    assert second != cold, "publication must still swap in a separate collection"
    assert _rows_by_id(config, cold) == cold_rows, "the retired collection was dropped or written after it was retired"
    assert load_candidate_checkpoint(_storage_path(config, runtime_paths)) is None, (
        "a kept collection must not look like an in-progress candidate"
    )

    (docs_path / names[1]).write_text("second change", encoding="utf-8")
    (docs_path / names[2]).unlink()
    (docs_path / "added.md").write_text("added body", encoding="utf-8")
    store_writes.clear()
    probed: list[str] = []
    original_probe = KnowledgeCollection.paths_with_vectors

    def _record_probe(self: KnowledgeCollection, relative_paths: Sequence[str]) -> set[str]:
        probed.extend(relative_paths)
        return original_probe(self, relative_paths)

    monkeypatch.setattr(KnowledgeCollection, "paths_with_vectors", _record_probe)
    embedder.embedded_texts.clear()

    assert await _refresh_and_check(config, docs_path) == cold, "the retired collection was not resumed"

    assert store_writes.copied == 0, "the refresh copied the published index"
    # The resumed collection is one publication behind, so the previous refresh's change is written again too.
    assert sorted(embedder.embedded_texts) == ["added body", "first change", "second change"]
    assert store_writes.file_rows == 3, "the refresh rewrote rows it did not change"
    assert probed == [], "a sealed collection's claims were probed again"
    untouched = set(names[3:])
    assert {i: r for i, r in _rows_by_id(config, cold).items() if r[0] in untouched} == {
        i: r for i, r in cold_rows.items() if r[0] in untouched
    }, "unchanged rows were rewritten"
    assert second in _collection_names(config), "the collection this publication retired was not kept in turn"
    assert sorted(_collection_names(config)) == sorted({cold, second})


def _rows_by_id(config: Config, collection: str) -> dict[str, tuple[str, str]]:
    return {
        row["id"]: (row["metadata"]["source_path"], row["content"])
        for row in stored_rows(_collection(config, collection))
    }


@pytest.mark.asyncio
async def test_every_publication_stays_exact_as_two_collections_take_turns(
    tmp_path: Path,
    store_writes: _StoreWrites,
) -> None:
    """Edits, additions, removals and mtime-only touches never lose, duplicate or keep stale rows."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 40)
    config = _config(tmp_path, docs_path)
    await _refresh_and_check(config, docs_path)
    for step in range(1, 15):
        (docs_path / names[step % len(names)]).write_text(f"edit {step}", encoding="utf-8")
        if step % 3 == 0:
            (docs_path / f"new{step}.md").write_text(f"new {step}", encoding="utf-8")
            (docs_path / f"new{step - 3}.md").unlink(missing_ok=True)
        touched = docs_path / names[(step + 5) % len(names)]
        os.utime(touched, ns=(touched.stat().st_atime_ns, touched.stat().st_mtime_ns + 1_000_000_000))
        store_writes.clear()
        await _refresh_and_check(config, docs_path)
        if step > 1:
            assert store_writes.copied == 0, f"refresh {step} copied the published index"
        assert len(_collection_names(config)) <= 2, "more than the published and the kept collection stayed on disk"


async def _disable_finalization(*_args: object, **_kwargs: object) -> None:
    """Stand in for the checkpoint compaction a killed process never runs."""


def _crash_after(target: Any) -> Any:  # noqa: ANN401
    """Return ``target`` wrapped to raise ``_Crash`` once it has done its work."""

    def _wrapper(*args: object, **kwargs: object) -> object:
        target(*args, **kwargs)
        raise _Crash

    return _wrapper


@pytest.mark.parametrize("crash_point", ["take_seal", "write_rows", "commit", "rotate_seals", "write_seal"])
@pytest.mark.asyncio
async def test_a_crash_at_any_step_leaves_a_whole_index_and_costs_at_most_one_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_writes: _StoreWrites,
    crash_point: str,
) -> None:
    """Readers see the old or the new index after any crash, and later refreshes converge."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 8)
    config = _config(tmp_path, docs_path)
    await _build_steady_state(config, docs_path, names)
    old_corpus = _corpus(docs_path)
    (docs_path / names[3]).write_text("changed before the crash", encoding="utf-8")
    (docs_path / "added.md").write_text("added before the crash", encoding="utf-8")
    new_corpus = _corpus(docs_path)

    with monkeypatch.context() as patch:
        patch.setattr(KnowledgeManager, "_finalize_candidate_checkpoint", _disable_finalization)
        if crash_point == "take_seal":
            patch.setattr(
                knowledge_manager_module,
                "take_sealed_candidate",
                _crash_after(knowledge_manager_module.take_sealed_candidate),
            )
        elif crash_point == "write_rows":
            original_write = knowledge_manager_module.write_source_rows

            def _crash_after_added_rows(collection: KnowledgeCollection, **kwargs: Any) -> None:  # noqa: ANN401
                original_write(collection, **kwargs)
                if kwargs["relative_path"] == "added.md":
                    raise _Crash

            patch.setattr(knowledge_manager_module, "write_source_rows", _crash_after_added_rows)
        elif crash_point == "commit":
            patch.setattr(
                knowledge_manager_module,
                "save_published_index_state",
                _crash_after(knowledge_manager_module.save_published_index_state),
            )
        elif crash_point == "rotate_seals":
            patch.setattr(knowledge_manager_module, "rotate_seals", _crash_after(lambda *_a, **_k: None))
        else:
            original_atomic = knowledge_checkpoint_module.write_json_atomic

            def _crash_writing_seal(path: Path, payload: Any) -> None:  # noqa: ANN401
                if "seal" in path.name:
                    raise _Crash
                original_atomic(path, payload)

            patch.setattr(knowledge_checkpoint_module, "write_json_atomic", _crash_writing_seal)
        with pytest.raises(_Crash):
            await _manager(config).reindex_all()

    served = _served(config, _published_collection(config))
    assert served in (old_corpus, new_corpus), "a crash left a published index that is neither old nor new"

    copies = 0
    for step in range(4):
        (docs_path / names[step]).write_text(f"after the crash {step}", encoding="utf-8")
        store_writes.clear()
        await _refresh_and_check(config, docs_path)
        copies += store_writes.copied > 0
    assert copies <= 1, f"the crash cost {copies} copies of the whole index"
    assert len(_collection_names(config)) <= 2


@pytest.mark.asyncio
async def test_rows_a_crash_wrote_before_their_claim_are_never_carried_into_a_kept_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed candidate may hold rows it never claimed, so it is never sealed and never resumed again."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 6)
    config = _config(tmp_path, docs_path)
    await _build_steady_state(config, docs_path, names)
    (docs_path / "transient.md").write_text("transient body", encoding="utf-8")
    original_write = knowledge_manager_module.write_source_rows

    def _crash_after_transient_rows(collection: KnowledgeCollection, **kwargs: Any) -> None:  # noqa: ANN401
        original_write(collection, **kwargs)
        if kwargs["relative_path"] == "transient.md":
            raise _Crash

    with monkeypatch.context() as patch:
        patch.setattr(KnowledgeManager, "_finalize_candidate_checkpoint", _disable_finalization)
        patch.setattr(knowledge_manager_module, "write_source_rows", _crash_after_transient_rows)
        with pytest.raises(_Crash):
            await _manager(config).reindex_all()
    (docs_path / "transient.md").unlink()
    (docs_path / names[4]).write_text("change after the crash", encoding="utf-8")
    # The resumed candidate publishes once with the rows it never claimed: the gap ordinary resume always had.
    assert (await _manager(config).reindex_all()).published
    tainted = _published_collection(config)

    for step in range(4):
        (docs_path / names[step]).write_text(f"later change {step}", encoding="utf-8")
        await _refresh_and_check(config, docs_path)
        assert tainted not in _collection_names(config), "the collection holding unclaimed rows was kept for reuse"


@pytest.mark.parametrize("change", ["chunking", "forced"])
@pytest.mark.asyncio
async def test_a_chunking_change_or_forced_rebuild_re_embeds_everything_and_keeps_nothing_old(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    change: str,
) -> None:
    """Old vectors are neither resumed nor kept for a later refresh once they are not to be trusted."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 6)
    config = _config(tmp_path, docs_path)
    await _build_steady_state(config, docs_path, names)
    old_collections = set(_collection_names(config))
    if change == "chunking":
        config = _config(tmp_path, docs_path, chunk_size=4000)
    embedder.embedded_texts.clear()

    assert (await _manager(config).reindex_all(force_reindex=change == "forced")).published

    assert sorted(embedder.embedded_texts) == sorted(text for _name, text in _corpus(docs_path))
    assert not old_collections & set(_collection_names(config)), "a collection holding old vectors survived"
    (docs_path / names[5]).write_text("next change", encoding="utf-8")
    embedder.embedded_texts.clear()
    await _refresh_and_check(config, docs_path)
    assert embedder.embedded_texts == ["next change"]


@pytest.mark.asyncio
async def test_a_kept_collection_a_shrinking_source_left_mostly_empty_is_copied_afresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_writes: _StoreWrites,
) -> None:
    """Deleted rows never shrink a file, so a kept collection left mostly free pages is replaced by a fresh copy."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 4)
    large = docs_path / "large.md"
    large.write_text(_overlapping_body(4000), encoding="utf-8")
    config = _config(tmp_path, docs_path, chunk_size=500)
    assert (await _manager(config).reindex_all()).published
    (docs_path / names[0]).write_text("first edit", encoding="utf-8")
    assert (await _manager(config).reindex_all()).published
    assert len(_collection_names(config)) == 2
    large.unlink()
    collections_while_copying: list[int] = []
    original_copy = KnowledgeManager._copy_published_vectors

    def _recording_copy(self: KnowledgeManager, **kwargs: Any) -> dict[str, FileSignature]:  # noqa: ANN401
        collections_while_copying.append(len(_collection_names(config)))
        return original_copy(self, **kwargs)

    monkeypatch.setattr(KnowledgeManager, "_copy_published_vectors", _recording_copy)
    store_writes.clear()

    # Two take-overs leave both collections mostly empty; the next two refreshes replace each with a fresh copy.
    for step in range(4):
        (docs_path / names[step]).write_text(f"edit {step}", encoding="utf-8")
        await _refresh_and_check(config, docs_path)

    assert store_writes.copied > 0, "no collection left mostly empty was copied afresh"
    # The worn collection is dropped before the copy, so only the published index and the copy share the disk.
    assert max(collections_while_copying) == 2
    assert all(_collection(config, name).free_fraction() <= 0.25 for name in _collection_names(config))


@pytest.mark.asyncio
async def test_a_refresh_that_finds_nothing_changed_keeps_the_retired_collection(
    tmp_path: Path,
    store_writes: _StoreWrites,
) -> None:
    """The unchanged-source path must not discard the collection the next change resumes."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await _build_steady_state(config, docs_path, names)
    collections = set(_collection_names(config))

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert set(_collection_names(config)) == collections
    assert get_knowledge_index_status("docs", config=config, runtime_paths=runtime_paths).candidate is None
    (docs_path / names[3]).write_text("change after an idle refresh", encoding="utf-8")
    store_writes.clear()
    await _refresh_and_check(config, docs_path)
    assert store_writes.copied == 0, "the idle refresh dropped the collection the next change could resume"


@pytest.mark.asyncio
async def test_resuming_the_retired_collection_waits_for_readers_still_holding_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reader that selected the retired collection before the swap must finish before it is written."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    storage = _storage_path(config, runtime_paths_for(config))
    await _build_steady_state(config, docs_path, names)
    retired_rows = {
        name: _served(config, name) for name in _collection_names(config) if name != _published_collection(config)
    }
    (docs_path / names[2]).write_text("change while a reader holds the old index", encoding="utf-8")
    reader_holds = Event()
    release_reader = Event()

    def _reader() -> None:
        with collection_lifetime_lock(storage, exclusive=False):
            reader_holds.set()
            assert release_reader.wait(timeout=10)

    reader = Thread(target=_reader)
    reader.start()
    assert reader_holds.wait(timeout=5)
    # Candidate cleanup waits on the same lock; without it, only the resume itself stands between the two.
    monkeypatch.setattr(knowledge_manager_module, "cleanup_superseded_collections", lambda *_a, **_k: None)
    refresh = asyncio.create_task(_manager(config).reindex_all())
    await asyncio.sleep(0.3)
    try:
        assert not refresh.done(), "the refresh did not wait for the reader"
        assert {name: _served(config, name) for name in retired_rows} == retired_rows, "the held collection was written"
    finally:
        release_reader.set()
        reader.join(timeout=5)
    assert (await refresh).published
    assert _served(config, _published_collection(config)) == _corpus(docs_path)


@pytest.mark.asyncio
async def test_an_unreadable_seal_costs_one_copy_and_nothing_else(
    tmp_path: Path,
    store_writes: _StoreWrites,
) -> None:
    """A seal that cannot be read proves nothing, so the refresh copies the published index as before."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    storage = _storage_path(config, runtime_paths_for(config))
    await _build_steady_state(config, docs_path, names)
    seals = load_seals(storage)
    assert seals, "a clean publication left no seal"
    for path in seals:
        path.write_text("not json", encoding="utf-8")
    (docs_path / names[3]).write_text("change after the seals broke", encoding="utf-8")
    store_writes.clear()

    await _refresh_and_check(config, docs_path)

    assert store_writes.copied > 0
    assert len(_collection_names(config)) <= 2


@pytest.mark.parametrize("seal_step", ["take_sealed_candidate", "rotate_seals"])
@pytest.mark.asyncio
async def test_a_cancelled_refresh_finishes_its_seal_change_before_it_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seal_step: str,
) -> None:
    """A seal change still running after cancellation could undo the next refresh's, such as a forced rebuild's."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    await _build_steady_state(config, docs_path, names)
    (docs_path / names[1]).write_text("change before the cancellation", encoding="utf-8")
    original = getattr(knowledge_manager_module, seal_step)
    started = Event()
    release = Event()
    finished = Event()

    def _blocking(*args: object, **kwargs: object) -> object:
        started.set()
        assert release.wait(timeout=10)
        result = original(*args, **kwargs)
        finished.set()
        return result

    monkeypatch.setattr(knowledge_manager_module, seal_step, _blocking)
    refresh = asyncio.create_task(_manager(config).reindex_all())
    assert await asyncio.to_thread(started.wait, 10)
    refresh.cancel()
    await asyncio.sleep(0.1)
    try:
        assert not refresh.done(), "the cancellation escaped while the seal change was still running"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await refresh
    assert finished.is_set()
    monkeypatch.setattr(knowledge_manager_module, seal_step, original)
    await _refresh_and_check(config, docs_path)


@pytest.mark.parametrize("damage", ["unreadable", "lost_row", "missing"])
@pytest.mark.asyncio
async def test_a_damaged_retired_collection_costs_one_copy_instead_of_failing_or_serving_lost_rows(
    tmp_path: Path,
    store_writes: _StoreWrites,
    damage: str,
) -> None:
    """A kept collection the store cannot read, or that lost rows at rest, is dropped and the index copied."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 6)
    config = _config(tmp_path, docs_path)
    await _build_steady_state(config, docs_path, names)
    (retired,) = set(_collection_names(config)) - {_published_collection(config)}
    retired_collection = _collection(config, retired)
    if damage == "unreadable":
        retired_collection.file.write_bytes(b"not a knowledge collection" * 100)
    elif damage == "lost_row":
        retired_collection.delete_source_paths([names[5]])
    else:
        retired_collection.delete()
    (docs_path / names[3]).write_text("change after the damage", encoding="utf-8")
    store_writes.clear()

    await _refresh_and_check(config, docs_path)

    assert store_writes.copied > 0
    assert retired not in _collection_names(config), "the damaged collection was kept"
    for step in range(2):
        (docs_path / names[step]).write_text(f"later change {step}", encoding="utf-8")
        await _refresh_and_check(config, docs_path)


@pytest.mark.asyncio
async def test_taking_a_seal_over_a_stranded_candidate_never_leaves_that_candidate_without_its_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_writes: _StoreWrites,
) -> None:
    """An interrupted take-over leaves the candidate it replaces whole or gone, never a snapshot missing its journal."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    storage = _storage_path(config, runtime_paths_for(config))
    await _build_steady_state(config, docs_path, names)
    # A candidate holding rows it never claimed is rebuilt, which is where a seal is taken over instead.
    stranded = f"{_manager(config)._collections.default_collection}_candidate_strandedstranded"
    seed_collection(_collection(config, stranded), [{"content": "stray", "metadata": {"source_path": "stray.md"}}])
    save_candidate_checkpoint(
        storage,
        CandidateCheckpoint(collection=stranded, settings=_manager(config)._indexing_settings),
    )
    append_candidate_journal(storage, removed=("stray.md",))
    snapshot = _candidate_checkpoint_path(storage)
    original_replace = Path.replace

    def _crash_before_the_seal_lands(self: Path, target: Path) -> Path:
        if Path(target) == snapshot:
            raise _Crash
        return original_replace(self, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", _crash_before_the_seal_lands)
        with pytest.raises(_Crash):
            await _manager(config).reindex_all()

    assert not snapshot.exists(), "the replaced candidate's snapshot outlived its journal"
    (docs_path / names[2]).write_text("change after the interrupted take-over", encoding="utf-8")
    store_writes.clear()
    await _refresh_and_check(config, docs_path)
    assert store_writes.copied == 0, "the seal was lost along with the stranded candidate"
