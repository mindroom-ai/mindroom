"""Resumable, bounded semantic knowledge refresh behavior.

These tests drive the candidate lifecycle through the real vector store and a recording embedder so every assertion
about *which* files were embedded, how many provider requests were issued, and what survives an interruption is exact.
A candidate is a working collection: an owned collection the publication does not name, whose own ``files``,
``failures`` and ``meta`` tables are its whole state, so assertions about progress read those tables.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING, Any, BinaryIO, Protocol

import httpx
import httpx2
import pytest
from agno.knowledge.document.base import Document
from agno.knowledge.reader.markdown_reader import MarkdownReader
from openai import APIStatusError, InternalServerError, RateLimitError
from structlog.testing import capture_logs

import mindroom.embeddings.batching as embedding_batching_module
import mindroom.embeddings.openai_embedder as openai_embedder_module
import mindroom.knowledge.corpus_signature as knowledge_corpus_signature
import mindroom.knowledge.file_indexer as knowledge_file_indexer
import mindroom.knowledge.indexing_config as knowledge_indexing_config
import mindroom.knowledge.manager as knowledge_manager_module
import mindroom.knowledge.refresh_runner as knowledge_refresh_runner
import mindroom.knowledge.registry as knowledge_registry
import mindroom.knowledge.vector_store as knowledge_vector_store
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
from mindroom.embeddings.factory import create_configured_embedder
from mindroom.embeddings.signatures import effective_knowledge_embedder_signature, effective_mem0_embedder_signature
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.collection_lifetime import collection_lifetime_lock
from mindroom.knowledge.collections import (
    CollectionSpace,
    _collection_name_for_base,
    candidate_collection_name,
)
from mindroom.knowledge.file_indexer import FileIndexer, _PreparedFile, max_concurrent_file_indexes
from mindroom.knowledge.index_metadata import load_publication, publication_path
from mindroom.knowledge.index_retry import EmbeddingRetryPolicy, run_with_embedding_retry
from mindroom.knowledge.manager import KnowledgeManager
from mindroom.knowledge.refresh_outcome import RefreshOutcome
from mindroom.knowledge.refresh_runner import _refresh_knowledge_binding as refresh_knowledge_binding
from mindroom.knowledge.refresh_runner import refresh_knowledge_binding_in_subprocess
from mindroom.knowledge.registry import (
    get_published_index,
    published_index_storage_path,
    resolve_published_index_key,
)
from mindroom.knowledge.status import get_knowledge_index_status
from mindroom.knowledge.utils import resolve_agent_knowledge_access
from mindroom.knowledge.vector_store import KnowledgeCollection, StoredRow, list_collection_names
from mindroom.llm.errors import ProviderUnavailable
from mindroom.model_defaults import SENTENCE_TRANSFORMERS_DEFAULT
from mindroom.providers.errors import classify_provider_error
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.knowledge_test_support import (
    collection_meta,
    install_knowledge_embedder,
    run_refresh_child_in_process,
    seed_collection,
    stored_rows,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from mindroom.embeddings.base import Embedder
    from mindroom.knowledge.corpus_signature import FileSignature
    from mindroom.knowledge.index_metadata import Publication
    from mindroom.knowledge.refresh_runner import KnowledgeRefreshResult
    from mindroom.knowledge.vector_store import CollectionMeta


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
    install_knowledge_embedder(monkeypatch, lambda *_a, **_k: embedder)

    async def _no_sleep(_seconds: float) -> None:
        return

    monkeypatch.setattr(knowledge_file_indexer, "_EMBEDDING_RETRY_SLEEP", _no_sleep)
    yield
    knowledge_registry._published_indexes.clear()


class _RuntimeRefresh(Protocol):
    async def __call__(self, config: Config) -> KnowledgeRefreshResult: ...


@pytest.fixture
def runtime_refresh(monkeypatch: pytest.MonkeyPatch) -> _RuntimeRefresh:
    """Refresh the way the runtime does, which records the child's result, with the child run in this process."""
    monkeypatch.setattr(knowledge_refresh_runner, "_run_refresh_child", run_refresh_child_in_process)
    # The provider health probe a refresh schedules is not what these tests pin, and it would build a real embedder.
    monkeypatch.setattr(knowledge_refresh_runner, "_schedule_refresh_health_check", lambda *_a, **_k: None)

    async def _refresh(config: Config) -> KnowledgeRefreshResult:
        return await refresh_knowledge_binding_in_subprocess(
            "docs",
            config=config,
            runtime_paths=runtime_paths_for(config),
        )

    return _refresh


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


def _storage(config: Config) -> Path:
    return published_index_storage_path(
        resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths_for(config)),
    )


def _collection(config: Config, name: str) -> KnowledgeCollection:
    """Return one collection in the docs base's storage directory."""
    return KnowledgeCollection(path=_storage(config), name=name)


def _collection_names(config: Config) -> tuple[str, ...]:
    return list_collection_names(_storage(config))


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


def _publication(config: Config) -> Publication | None:
    return load_publication(_storage(config))


def _published_collection(config: Config) -> str:
    publication = _publication(config)
    assert publication is not None
    assert publication.collection is not None
    return publication.collection


def _working_collections(config: Config) -> list[KnowledgeCollection]:
    """Return the collections the publication does not name: the candidate, the kept one, and any leftover."""
    publication = _publication(config)
    published = None if publication is None else publication.collection
    return [_collection(config, name) for name in _collection_names(config) if name != published]


def _candidate(config: Config) -> KnowledgeCollection:
    """Return the base's one unfinished build."""
    (candidate,) = [collection for collection in _working_collections(config) if collection.meta().stage == "building"]
    return candidate


def _assert_claims_match_rows(collection: KnowledgeCollection) -> None:
    """Assert the collection claims exactly the paths it holds rows for (the corpora here have no empty files)."""
    claimed = set(collection.files())
    with_rows = {row["metadata"]["source_path"] for row in stored_rows(collection)}
    assert claimed == with_rows, f"{collection.name} claims {sorted(claimed)} but holds rows for {sorted(with_rows)}"


def _current_settings(config: Config) -> Mapping[str, str]:
    return _manager(config)._indexing_settings.to_metadata()


def _fail_files(monkeypatch: pytest.MonkeyPatch, failing: set[str]) -> set[str]:
    """Fail every file named in ``failing`` the way a rejected file fails; the returned set can be edited later."""
    original_index = FileIndexer._index_file

    async def _index(
        self: FileIndexer,
        prepared: _PreparedFile,
        *,
        batch_vectors: Mapping[str, list[float]],
    ) -> FileSignature | None:
        if prepared.path.name in failing:
            return None
        return await original_index(self, prepared, batch_vectors=batch_vectors)

    monkeypatch.setattr(FileIndexer, "_index_file", _index)
    return failing


def _cancel_after(monkeypatch: pytest.MonkeyPatch, count: int) -> list[str]:
    """Cancel the refresh, as a shutdown would, when it starts indexing file ``count + 1``; return the files indexed."""
    original_index = FileIndexer._index_file
    indexed: list[str] = []

    async def _index(
        self: FileIndexer,
        prepared: _PreparedFile,
        *,
        batch_vectors: Mapping[str, list[float]],
    ) -> FileSignature | None:
        if len(indexed) >= count:
            raise asyncio.CancelledError
        indexed.append(prepared.path.name)
        return await original_index(self, prepared, batch_vectors=batch_vectors)

    monkeypatch.setattr(FileIndexer, "_index_file", _index)
    return indexed


def _file_indexer(
    config: Config,
    docs_path: Path,
    collection: KnowledgeCollection,
    embedder: Embedder,
) -> FileIndexer:
    """Return the indexer one refresh of the docs base would use to write into ``collection``."""
    collection.create(collection_meta(_current_settings(config)))
    return FileIndexer(
        base_id="docs",
        config=config,
        runtime_paths=runtime_paths_for(config),
        knowledge_root=docs_path,
        collection=collection,
        build_embedder=lambda: RefreshEmbedder(embedder),
        max_concurrent_file_indexes=max_concurrent_file_indexes(runtime_paths_for(config)),
    )


_AUTH_FAILURE = EmbedderRequestError("embedder authentication failed (HTTP 401)", kind="credentials")


def _server_error() -> EmbedderRequestError:
    """Return the retryable error an embedder raises for an HTTP 503."""
    return EmbedderRequestError("embedder request failed (HTTP 503)", retryable=True)


def _api_error() -> Exception:
    """Return what the OpenAI embedder raises for a 503 with a ``Retry-After`` hint, classified at its boundary."""
    request = httpx.Request("POST", "http://embeddings.local/v1/embeddings")
    response = httpx.Response(503, request=request, headers={"retry-after": "7"})
    return openai_embedder_error(APIStatusError("overloaded", response=response, body=None))


# --------------------------------------------------------------------------
# 1. Cold-start interruption resumes the same candidate
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupted_cold_build_resumes_same_candidate_without_reembedding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    embedder: _RecordingEmbedder,
) -> None:
    """A killed build resumes its candidate and only embeds what it still owes."""
    (tmp_path / ".env").write_text("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY=1\n", encoding="utf-8")
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 6)
    config = _config(tmp_path, docs_path)
    assert max_concurrent_file_indexes(runtime_paths_for(config)) == 1

    interrupt_after = 3
    with monkeypatch.context() as patch:
        _cancel_after(patch, interrupt_after)
        with pytest.raises(asyncio.CancelledError):
            await _manager(config).reindex_all()

    interrupted = _candidate(config)
    completed = interrupted.files()
    assert len(completed) == interrupt_after
    _assert_claims_match_rows(interrupted)
    # Only durably completed files may be skipped on resume; vectors that were
    # merely batch-embedded in memory are legitimately re-embedded.
    completed_bodies = {f"content {names.index(name)}" for name in completed}
    embedded_after_interrupt = {body: embedder.embedded_count(body) for body in completed_bodies}

    # A brand new manager models a process restart: nothing survives in memory.
    resumed_manager = _manager(config)
    assert (await resumed_manager.reindex_all()).indexed_count == len(names) - interrupt_after

    publication = _publication(config)
    assert publication is not None
    assert publication.collection == interrupted.name, "resume must continue the same candidate"
    assert publication.indexed_count == len(names)
    assert _working_collections(config) == [], "publication leaves no unfinished build behind"
    for body, count in embedded_after_interrupt.items():
        assert embedder.embedded_count(body) == count, "completed files must not be embedded again"
    assert _stored_paths(config, interrupted.name) == sorted(names)


# --------------------------------------------------------------------------
# 2. Restart with a last-good index
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidate_resume_never_disturbs_the_last_good_published_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing candidate resumes privately while the published index stays queryable."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "a.md").write_text("first published", encoding="utf-8")
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    published_collection = _published_collection(config)

    (docs_path / "a.md").write_text("second revision", encoding="utf-8")
    (docs_path / "b.md").write_text("cannot index", encoding="utf-8")
    _fail_files(monkeypatch, {"b.md"})

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.published is False
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.reader is not None
    assert [document.content for document in await lookup.reader.asearch("published", limit=5)] == [
        "first published",
    ]
    assert _published_collection(config) == published_collection
    candidate = _candidate(config)
    assert candidate.name != published_collection
    assert set(candidate.files()) == {"a.md"}
    assert candidate.failed_paths() == {"b.md"}


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
    embedder.failures["content 4"] = [_server_error()]

    manager = _manager(config)
    assert await manager.reindex_all() == RefreshOutcome(indexed_count=5, published=True, error=None)

    publication = _publication(config)
    assert publication is not None
    assert publication.indexed_count == 5
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
        knowledge_file_indexer,
        "_EMBEDDING_RETRY_POLICY",
        EmbeddingRetryPolicy(max_attempts=2, initial_backoff_seconds=0.0),
    )
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    embedder.failures["content 3"] = [_server_error() for _ in range(20)]

    manager = _manager(config)
    outcome = await manager.reindex_all()
    assert outcome.indexed_count == 3
    assert not outcome.published
    assert outcome.error is not None
    assert "Indexed 3 of 4" in outcome.error
    assert _publication(config) is None

    candidate = _candidate(config)
    assert candidate.failed_paths() == {"doc0003.md"}
    assert candidate.claim_counts() == (3, 1)

    embedder.failures.pop("content 3")
    embedded_before = dict.fromkeys(embedder.embedded_texts)
    assert (await _manager(config).reindex_all()).indexed_count == 1
    for text in embedded_before:
        assert embedder.embedded_count(text) == 1, "resume must not re-embed resolved files"
    publication = _publication(config)
    assert publication is not None
    assert publication.collection == candidate.name
    assert publication.indexed_count == 4


@pytest.mark.asyncio
async def test_permanent_embedding_failure_never_publishes_and_reports_classified_error(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    runtime_refresh: _RuntimeRefresh,
) -> None:
    """Auth failures fail fast, keep last-good, and surface only classified text."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "a.md").write_text("published body", encoding="utf-8")
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    assert (await runtime_refresh(config)).index_published
    published_collection = _published_collection(config)

    (docs_path / "b.md").write_text("secret sk-should-never-appear", encoding="utf-8")
    embedder.fail_everything = EmbedderRequestError("embedder authentication failed (HTTP 401)", kind="credentials")

    result = await runtime_refresh(config)

    assert result.index_published is False
    assert result.availability is KnowledgeAvailability.REFRESH_FAILED
    assert result.last_error is not None
    assert "embedder authentication failed (HTTP 401)" in result.last_error
    assert "sk-should-never-appear" not in result.last_error
    # A permanent rejection must not cost one doomed request per remaining file.
    assert embedder.request_count <= 3
    assert _published_collection(config) == published_collection
    # Searching embeds the query with the same embedder, so let it answer again.
    embedder.fail_everything = None
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.reader is not None
    assert [document.content for document in await lookup.reader.asearch("body", limit=5)] == [
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
    published_collection = _published_collection(config)

    # Leave a partial candidate behind under the current settings.
    stale_candidate = candidate_collection_name(_manager(config)._collections)
    seed_collection(
        _collection(config, stale_candidate),
        [{"content": "content 0", "metadata": {"source_path": "doc0000.md"}}],
        settings=_current_settings(config),
    )

    changed_config = config.model_copy(deep=True)
    changed_config.memory.embedder.config.model = "text-embedding-3-large"
    changed_manager = KnowledgeManager("docs", config=changed_config, runtime_paths=runtime_paths)
    run = await changed_manager._open_candidate_run(force_reindex=False)

    assert run.resumed is False
    assert run.completed == {}, "vectors built under other settings were copied"
    assert run.collection.name not in {stale_candidate, published_collection}
    assert stale_candidate not in _collection_names(config), "incompatible candidate is discarded"
    assert published_collection in _collection_names(config), "published index is not touched"


@pytest.mark.asyncio
async def test_incompatible_candidate_delete_failure_does_not_block_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale candidate cannot make a base permanently unrefreshable; the sweep after publishing deletes it."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    manager = _manager(config)
    stale_candidate = candidate_collection_name(manager._collections)
    seed_collection(
        _collection(config, stale_candidate),
        [],
        settings=replace(manager._indexing_settings, embedder_model="old-model").to_metadata(),
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

    assert (await manager.reindex_all()).published

    assert attempts == 1
    assert _published_collection(config) != stale_candidate
    assert stale_candidate not in _collection_names(config), "candidate GC did not retry the transient failure"


# --------------------------------------------------------------------------
# 7-8. Source advancement and final-verification races
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_revision_advancement_reuses_unchanged_candidate_work(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Added, changed and deleted files are reconciled without redoing the rest."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "keep.md").write_text("keep me", encoding="utf-8")
    (docs_path / "change.md").write_text("old body", encoding="utf-8")
    (docs_path / "drop.md").write_text("delete me", encoding="utf-8")
    config = _config(tmp_path, docs_path)

    # Fail one file so the pass keeps its candidate instead of publishing.
    failing = _fail_files(monkeypatch, {"change.md"})
    await _manager(config).reindex_all()
    failing.clear()

    candidate = _candidate(config)
    assert set(candidate.files()) == {"keep.md", "drop.md"}

    (docs_path / "change.md").write_text("new body", encoding="utf-8")
    (docs_path / "drop.md").unlink()
    (docs_path / "added.md").write_text("added body", encoding="utf-8")
    embedder.embedded_texts.clear()

    assert (await _manager(config).reindex_all()).indexed_count == 2

    assert embedder.embedded_count("keep me") == 0, "unchanged work is reused"
    assert embedder.embedded_count("new body") == 1
    assert embedder.embedded_count("added body") == 1
    publication = _publication(config)
    assert publication is not None
    assert publication.collection == candidate.name
    assert publication.indexed_count == 3
    assert _stored_paths(config, candidate.name) == ["added.md", "change.md", "keep.md"], "deleted vectors are removed"


@pytest.mark.asyncio
async def test_source_change_during_final_verification_reconciles_without_losing_work(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A change racing the completeness check triggers reconciliation, not destruction."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    manager = _manager(config)
    original_scan = knowledge_manager_module.scan_corpus
    mutated = False

    def _mutate_during_verification(*args: Any, **kwargs: Any) -> dict[str, tuple[FileSignature, Path]]:  # noqa: ANN401
        nonlocal mutated
        if not mutated:
            mutated = True
            (docs_path / "late.md").write_text("late body", encoding="utf-8")
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(knowledge_manager_module, "scan_corpus", _mutate_during_verification)

    outcome = await manager.reindex_all()

    assert mutated
    assert outcome == RefreshOutcome(indexed_count=4, published=True, error=None)
    assert embedder.embedded_count("content 0") == 1, "the racing change costs no re-embedding"
    publication = _publication(config)
    assert publication is not None
    assert publication.indexed_count == 4


@pytest.mark.asyncio
async def test_a_local_refresh_reads_only_the_files_whose_stat_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file whose ctime and size still match its recorded signature is not read again, before or after indexing.

    The changed file is read once, by the scan that found it, and then copied for indexing; a refresh with nothing
    changed reads no file at all. A ctime-only change (``chmod``) is read once and then trusted again, because the
    restamped signature reaches the published collection's claims, which the next refresh compares against.
    """
    monkeypatch.setattr(knowledge_corpus_signature, "_RACY_WINDOW_NS", 0)
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    (docs_path / names[0]).write_text("changed body", encoding="utf-8")
    read: list[str] = []
    original_signature = knowledge_corpus_signature._descriptor_signature

    def _recording_signature(descriptor: int, file_path: Path, output: BinaryIO | None = None) -> FileSignature:
        if output is None:
            read.append(file_path.name)
        return original_signature(descriptor, file_path, output)

    monkeypatch.setattr(knowledge_corpus_signature, "_descriptor_signature", _recording_signature)

    assert (await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)).published
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
    await _manager(config).reindex_all()
    manager = _manager(config)
    await manager.source_signature()
    (docs_path / names[1]).write_text("late body", encoding="utf-8")

    assert (await manager.reindex_all()).published
    assert embedder.embedded_count("late body") == 1
    publication = _publication(config)
    assert publication is not None
    assert publication.source_signature == await _manager(config).source_signature()


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

    assert all(result.published for result in results)
    assert len(_candidate_collections(config)) == 1, "one candidate, not one per request"
    for index in range(4):
        assert embedder.embedded_count(f"content {index}") == 1, "no duplicate full rebuild"


# --------------------------------------------------------------------------
# 10. Cancellation boundaries
# --------------------------------------------------------------------------


@pytest.mark.parametrize("publication_save_fails", [True, False])
@pytest.mark.asyncio
async def test_a_failed_publication_write_never_produces_a_false_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publication_save_fails: bool,
) -> None:
    """Failing at the publication write leaves no publication; succeeding leaves a whole one."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    manager = _manager(config)
    original_save = knowledge_manager_module.save_publication

    def _save(storage: Path, publication: Publication) -> None:
        if publication_save_fails:
            msg = "publication save failed"
            raise OSError(msg)
        original_save(storage, publication)

    monkeypatch.setattr(knowledge_manager_module, "save_publication", _save)
    if publication_save_fails:
        with pytest.raises(OSError, match="publication save failed"):
            await manager.reindex_all()
        assert not publication_path(_storage(config)).exists(), "no publication without a completed write"
        (candidate,) = _working_collections(config)
        assert len(candidate.files()) == 2, "candidate progress survives the failure"
        _assert_claims_match_rows(candidate)
    else:
        assert (await manager.reindex_all()).published
        publication = _publication(config)
        assert publication is not None
        assert _stored_paths(config, _published_collection(config)) == ["doc0000.md", "doc0001.md"]


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
    published_collection = _published_collection(config)

    # A complete collection the publication does not keep: what an interrupted sweep leaves behind.
    abandoned = candidate_collection_name(_manager(config)._collections)
    seed_collection(_collection(config, abandoned), [], settings=_current_settings(config))
    _collection(config, abandoned).mark_complete()
    unknown = "some_other_service_collection"
    seed_collection(_collection(config, unknown), [])

    manager = _manager(config)
    run = await manager._open_candidate_run(force_reindex=False)

    assert abandoned not in _collection_names(config), "abandoned candidates are reclaimed"
    assert published_collection in _collection_names(config)
    assert unknown in _collection_names(config), "unknown collections are preserved"
    assert run.collection.name in _collection_names(config)


@pytest.mark.asyncio
async def test_repeated_interrupted_refreshes_keep_collection_count_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Many interrupted refreshes must not accumulate candidate collections."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    _fail_files(monkeypatch, set(names))

    for _ in range(6):
        await _manager(config).reindex_all()

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
        knowledge_file_indexer,
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

    assert result.published is True
    assert _stored_paths(config, _published_collection(config)) == names
    assert (len(embedder.batch_requests), sorted(embedder.single_requests)) == (
        1,
        [f"content {index}" for index in range(3)],
    )


def _input_too_long_error() -> EmbedderRequestError:
    """Return the embedder error of OpenAI's 400 for an input longer than the embedding model accepts."""
    message = (
        "This model's maximum context length is 8192 tokens, however you requested 9000 tokens (9000 in your prompt; "
        "0 for the completion). Please reduce your prompt; or completion length."
    )
    failure = classify_provider_error(message, provider="openai", status_code=400)
    return embedder_request_error(RuntimeError(message), failure)


@pytest.mark.asyncio
async def test_a_chunk_too_long_for_the_embedding_model_fails_only_its_own_file(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """The batch goes one chunk per request, so the refresh fails but keeps every other file's vectors for the next."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    embedder.batch_error = _input_too_long_error()
    embedder.failures["content 1"] = [_input_too_long_error() for _ in range(5)]

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.published is False
    candidate = _candidate(config)
    assert (sorted(candidate.files()), candidate.failed_paths()) == (["doc0000.md", "doc0002.md"], {"doc0001.md"})


def _physical_batch_too_small_error() -> Exception:
    """Return what the OpenAI embedder raises for llama.cpp's 500 for an input larger than its physical batch."""
    message = "input (9000 tokens) is too large to process. increase the physical batch size (current batch size: 512)"
    body = {"code": 500, "message": message, "type": "server_error"}
    response = httpx.Response(500, request=httpx.Request("POST", "http://embeddings.local/v1/embeddings"))
    return openai_embedder_error(InternalServerError(message, response=response, body=body))


@pytest.mark.asyncio
async def test_a_chunk_larger_than_a_llama_cpp_physical_batch_fails_only_its_own_file_at_once(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Only a smaller input fits, so the batch goes one chunk per request at once and the chunk's file is not retried."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    embedder.batch_error = _physical_batch_too_small_error()
    embedder.failures["content 1"] = [_physical_batch_too_small_error() for _ in range(10)]

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.published is False
    assert (len(embedder.batch_requests), embedder.single_requests.count("content 1")) == (1, 2)
    candidate = _candidate(config)
    assert (sorted(candidate.files()), candidate.failed_paths()) == (["doc0000.md", "doc0002.md"], {"doc0001.md"})


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_embedder_clients")
async def test_a_refresh_sends_a_request_the_account_s_billing_stops_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refresh retries a failed request itself, so the OpenAI SDK must not resend it underneath."""
    sent: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        error = {
            "message": "You exceeded your current quota.",
            "type": "insufficient_quota",
            "code": "insufficient_quota",
        }
        return httpx2.Response(429, headers={"retry-after-ms": "1"}, json={"error": error})

    sdk_client = openai_embedder_module.OpenAI
    monkeypatch.setattr(
        openai_embedder_module,
        "OpenAI",
        lambda **kwargs: sdk_client(**kwargs, http_client=httpx2.Client(transport=httpx2.MockTransport(respond))),
    )
    monkeypatch.setattr(knowledge_manager_module, "create_configured_embedder", create_configured_embedder)
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    embedder_config = {"model": "text-embedding-3-small", "api_key": "test-key", "host": "http://embeddings.local/v1"}
    config = _config(tmp_path, docs_path, memory={"embedder": {"provider": "openai", "config": embedder_config}})

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths_for(config))

    assert result.published is False
    assert len(sent) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_embedder_clients")
async def test_a_knowledge_search_keeps_the_sdk_s_retry_of_a_failed_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing retries a search's query embedding in place, so the OpenAI SDK's own retry of a 503 must stay."""
    sent: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        if len(sent) == 1:
            return httpx2.Response(503, headers={"retry-after-ms": "1"}, json={"error": {"message": "overloaded"}})
        return httpx2.Response(200, json={"object": "list", "data": [{"embedding": [4.0, 1.0], "index": 0}]})

    sdk_client = openai_embedder_module.AsyncOpenAI
    monkeypatch.setattr(
        openai_embedder_module,
        "AsyncOpenAI",
        lambda **kwargs: sdk_client(
            **{**kwargs, "http_client": httpx2.AsyncClient(transport=httpx2.MockTransport(respond))},
        ),
    )
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "a.md").write_text("body", encoding="utf-8")
    embedder_config = {"model": "text-embedding-3-small", "api_key": "test-key", "host": "http://embeddings.local/v1"}
    config = _config(tmp_path, docs_path, memory={"embedder": {"provider": "openai", "config": embedder_config}})
    runtime_paths = runtime_paths_for(config)
    # The refresh embeds with the recording embedder; the search builds its embedder the production way.
    monkeypatch.setattr(knowledge_registry, "create_configured_embedder", create_configured_embedder)
    assert (await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)).published
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.reader is not None

    hits = await lookup.reader.asearch("body", limit=5)

    assert ([hit.content for hit in hits], len(sent)) == (["body"], 2)


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
            if self.in_flight >= knowledge_file_indexer._EMBEDDING_REQUESTS_IN_FLIGHT:
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
    assert 2 <= tracked.peak_in_flight <= knowledge_file_indexer._EMBEDDING_REQUESTS_IN_FLIGHT


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
    assert tracked.started == knowledge_file_indexer._EMBEDDING_REQUESTS_IN_FLIGHT
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
        if len(entered) >= knowledge_file_indexer._EMBEDDING_REQUESTS_IN_FLIGHT - 1:
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
    assert entered_when_failed == knowledge_file_indexer._EMBEDDING_REQUESTS_IN_FLIGHT - 1
    # Each of those threads finishes the request it was already making, and starts no other.
    assert len(entered) == entered_when_failed


@pytest.mark.asyncio
async def test_embedding_requests_in_flight_never_exceed_the_file_index_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator who limits file indexing to one at a time also gets one embedding request at a time."""
    tracked = _use_in_flight_embedder(monkeypatch)
    (tmp_path / ".env").write_text("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY=1\n", encoding="utf-8")
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
        knowledge_file_indexer,
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
        with closing(sqlite3.connect(_collection(config, _published_collection(config)).file)) as connection:
            return connection.execute("SELECT id, document, metadata, embedding FROM chunks ORDER BY id").fetchall()

    overlapping = await _stored_rows(tmp_path / "overlapping")
    monkeypatch.setattr(knowledge_file_indexer, "_EMBEDDING_REQUESTS_IN_FLIGHT", 1)
    sequential = await _stored_rows(tmp_path / "sequential")

    assert len(overlapping) == 30
    assert overlapping == sequential


def test_refresh_embedder_degrades_for_providers_without_batching() -> None:
    """Providers without a batch surface keep working, one request per distinct chunk."""
    inner = _RecordingNonBatchEmbedder()
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


def test_a_batch_answered_with_the_wrong_vector_count_retires_batching() -> None:
    """A backend that answers an array with the wrong count does not batch, so the refresh embeds one text at a time."""
    inner = _RecordingEmbedder()
    inner.batch_error = EmbedderRequestError("embedder returned 1 embeddings for 2 inputs", kind="cardinality")
    embedder = RefreshEmbedder(inner)

    assert sorted(embedder.embed_texts(["one", "two"])) == ["one", "two"]

    assert embedder.batches() is False
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live indexing tasks stay bounded no matter how large the corpus is."""
    docs_path = tmp_path / "docs"
    file_count = 400
    _write_corpus(docs_path, file_count)
    (tmp_path / ".env").write_text("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY=4\n", encoding="utf-8")
    config = _config(tmp_path, docs_path)

    peak_tasks = 0
    original_index = FileIndexer._index_file

    async def _observe(
        self: FileIndexer,
        prepared: _PreparedFile,
        *,
        batch_vectors: Mapping[str, list[float]],
    ) -> FileSignature | None:
        nonlocal peak_tasks
        peak_tasks = max(peak_tasks, len(asyncio.all_tasks()))
        return await original_index(self, prepared, batch_vectors=batch_vectors)

    monkeypatch.setattr(FileIndexer, "_index_file", _observe)
    assert (await _manager(config).reindex_all()).indexed_count == file_count

    # One batch of tasks plus the driving task and pytest's own; nothing that
    # scales with the 400-file corpus.
    assert peak_tasks <= knowledge_file_indexer._INDEX_FILES_PER_BATCH + 8, peak_tasks


# --------------------------------------------------------------------------
# 14. Status and API compatibility
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_reports_candidate_progress_separately_from_published_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_refresh: _RuntimeRefresh,
) -> None:
    """Candidate progress is visible but is never mistaken for a queryable index."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    _fail_files(monkeypatch, {"doc0003.md"})

    assert (await runtime_refresh(config)).index_published is False

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
    seed_collection(
        _collection(config, candidate_collection_name(manager._collections)),
        [{"content": "content 0", "metadata": {"source_path": "doc0000.md"}}],
        settings=replace(manager._indexing_settings, embedder_model="different-model").to_metadata(),
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
# 15. Scale regression
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
    assert max_concurrent_file_indexes(runtime_paths_for(config)) == 1

    stop_after = int(file_count * 0.9)
    with monkeypatch.context() as patch:
        _cancel_after(patch, stop_after)
        with pytest.raises(asyncio.CancelledError):
            await _manager(config).reindex_all()

    candidate = _candidate(config)
    completed_after_interrupt = len(candidate.files())
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
    assert _collection_names(config) == (candidate.name,)
    publication = _publication(config)
    assert publication is not None
    assert publication.indexed_count == file_count
    assert publication.collection == candidate.name


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Candidate ``total_files`` is the target corpus, not a completed high-water mark.

    Persisting ``max(previous_total, completed)`` made ``pending_count`` always
    zero, so an operator watching a stalled build saw "nothing left to do".
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 5)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    _fail_files(monkeypatch, {"doc0003.md", "doc0004.md"})

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert _candidate(config).meta().total_files == 5
    status = get_knowledge_index_status("docs", config=config, runtime_paths=runtime_paths)
    assert status.candidate is not None
    assert status.candidate.total_files == 5
    assert status.candidate.completed_count == 3
    assert status.candidate.failed_count == 2
    assert status.candidate.pending_count == 2


@pytest.mark.asyncio
async def test_batch_failure_never_leaves_stragglers_writing_after_the_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One failing file must not strand its siblings past the batch that owns them.

    ``asyncio.gather`` propagates the first exception while leaving the other
    coroutines running, so a straggler could write after the refresh had
    already released the lock that admitted it.
    """
    (tmp_path / ".env").write_text("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY=4\n", encoding="utf-8")
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)

    slow_started = asyncio.Event()
    slow_finished = asyncio.Event()
    original_index = FileIndexer._index_file

    async def _fail_fast_and_stall_others(
        self: FileIndexer,
        prepared: _PreparedFile,
        *,
        batch_vectors: Mapping[str, list[float]],
    ) -> FileSignature | None:
        if prepared.path.name == "doc0000.md":
            await slow_started.wait()
            msg = "explodes while siblings are still running"
            raise RuntimeError(msg)
        slow_started.set()
        await asyncio.sleep(0)
        signature = await original_index(self, prepared, batch_vectors=batch_vectors)
        slow_finished.set()
        return signature

    monkeypatch.setattr(FileIndexer, "_index_file", _fail_fast_and_stall_others)
    with pytest.raises(RuntimeError, match="explodes while siblings"):
        await _manager(config).reindex_all()

    assert slow_finished.is_set(), "siblings must have settled before the batch returned"
    candidate = _candidate(config)
    # Every file whose vectors landed is claimed, and nothing else.
    _assert_claims_match_rows(candidate)
    assert set(candidate.files()) == {"doc0001.md", "doc0002.md", "doc0003.md"}


@pytest.mark.parametrize("force_reindex", [False, True])
@pytest.mark.asyncio
async def test_unreadable_publication_never_costs_the_live_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    force_reindex: bool,
) -> None:
    """Candidate GC must not delete any collection while the publication is unreadable, forced or not.

    A published collection is itself candidate-named, so proving which
    candidate-prefixed collections are superseded depends entirely on a readable
    publication. Without it, reclaiming storage could delete the last good index.
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    published_collection = _published_collection(config)
    assert "_candidate_" in published_collection
    # A build the last refresh left unfinished, and a complete collection no publication keeps.
    failing = _fail_files(monkeypatch, {"doc0001.md"})
    (docs_path / "doc0001.md").write_text("changed", encoding="utf-8")
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    failing.clear()
    leftover = candidate_collection_name(_manager(config)._collections)
    seed_collection(_collection(config, leftover), [], settings=_current_settings(config))
    _collection(config, leftover).mark_complete()
    before = set(_collection_names(config))
    assert len(before) == 3

    publication_path(_storage(config)).write_text("{ truncated", encoding="utf-8")

    run = await _manager(config)._open_candidate_run(force_reindex=force_reindex)

    assert before <= set(_collection_names(config)), "a collection was deleted while the live one was unprovable"
    assert run.collection.name not in before, "an unprovable state was resumed"


@pytest.mark.asyncio
async def test_unreadable_publication_never_reuses_the_live_collection(
    tmp_path: Path,
) -> None:
    """An unprovable publication must not resume writes against, or copy from, the live index."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    published_collection = _published_collection(config)
    published_rows = stored_rows(_collection(config, published_collection))
    publication_path(_storage(config)).write_text("{ truncated", encoding="utf-8")

    run = await _manager(config)._open_candidate_run(force_reindex=False)

    assert run.collection.name != published_collection
    assert run.resumed is False
    assert run.completed == {}
    assert stored_rows(_collection(config, published_collection)) == published_rows


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
    default_collection = manager._collections._default_collection
    # A collection under the default name that no settings of this release describe, as an old layout left it.
    seed_collection(_collection(config, default_collection), [])
    seed_collection(_collection(config, "some_unrelated_collection"), [])

    with capture_logs() as logs:
        await manager._open_candidate_run(force_reindex=False)

    reported = [
        entry for entry in logs if entry["event"] == "Preserved knowledge collections with unprovable ownership"
    ]
    assert len(reported) == 1
    assert reported[0]["collections"] == ["some_unrelated_collection"]
    assert default_collection in _collection_names(config)


@pytest.mark.asyncio
async def test_candidate_pending_count_is_visible_while_the_build_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mid-build readers must see real outstanding work, not completed == total."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 6)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    observed: list[tuple[int, int]] = []
    pending_samples: list[int] = []
    original_index = FileIndexer._index_file

    async def _observe_status(
        self: FileIndexer,
        prepared: _PreparedFile,
        *,
        batch_vectors: Mapping[str, list[float]],
    ) -> FileSignature | None:
        signature = await original_index(self, prepared, batch_vectors=batch_vectors)
        status = get_knowledge_index_status("docs", config=config, runtime_paths=runtime_paths)
        if status.candidate is not None:
            observed.append((status.candidate.total_files, status.candidate.completed_count))
            pending_samples.append(status.candidate.pending_count)
        return signature

    monkeypatch.setattr(FileIndexer, "_index_file", _observe_status)
    assert (await _manager(config).reindex_all()).indexed_count == 6

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successive interrupted refreshes converge instead of restarting.

    The corpus is mutated between every attempt, which is the situation a
    large Git-backed source is always in: each pass must keep what it has,
    absorb the delta, and eventually publish the latest snapshot.
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 9)
    config = _config(tmp_path, docs_path)
    blocked = _fail_files(monkeypatch, {"doc0004.md", "doc0007.md"})

    # Attempt 1: two files cannot be indexed, so nothing publishes.
    await _manager(config).reindex_all()
    candidate = _candidate(config)
    assert candidate.failed_paths() == blocked
    first = candidate.files()

    # Attempt 2: still blocked, and the source moves underneath it.
    (docs_path / "doc0000.md").write_text("rewritten body", encoding="utf-8")
    (docs_path / "doc0001.md").unlink()
    (docs_path / "extra.md").write_text("added between attempts", encoding="utf-8")
    await _manager(config).reindex_all()
    assert _candidate(config).name == candidate.name, "each attempt continues the same candidate"
    second = candidate.files()
    assert "doc0001.md" not in second
    assert second["doc0000.md"] != first["doc0000.md"], "changed file was re-indexed"
    assert "extra.md" in second

    # Attempt 3: the blocker clears and the accumulated candidate publishes.
    embedder.embedded_texts.clear()
    blocked.clear()
    await _manager(config).reindex_all()

    assert embedder.embedded_count("rewritten body") == 0, "work kept across attempts is not redone"
    assert embedder.embedded_count("added between attempts") == 0
    publication = _publication(config)
    assert publication is not None
    assert publication.collection == candidate.name
    assert publication.indexed_count == 9
    assert _stored_paths(config, candidate.name) == sorted(
        [f"doc{index:04d}.md" for index in range(9) if index != 1] + ["extra.md"],
    )
    assert _working_collections(config) == []


@pytest.mark.asyncio
async def test_a_failed_file_that_left_the_corpus_no_longer_counts_as_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed build drops the failures of files the source no longer has, so its status counts only real ones."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    _fail_files(monkeypatch, {"doc0002.md", "doc0003.md"})
    await _manager(config).reindex_all()
    assert _candidate(config).failed_paths() == {"doc0002.md", "doc0003.md"}

    (docs_path / "doc0002.md").unlink()
    await _manager(config).reindex_all()

    assert _candidate(config).failed_paths() == {"doc0003.md"}
    status = get_knowledge_index_status("docs", config=config, runtime_paths=runtime_paths)
    assert status.candidate is not None
    assert (status.candidate.total_files, status.candidate.completed_count, status.candidate.failed_count) == (3, 2, 1)


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
    embedder.short_batch = True

    assert (await _manager(config).reindex_all()).indexed_count == 5

    publication = _publication(config)
    assert publication is not None
    assert publication.indexed_count == 5
    for index in range(5):
        assert embedder.embedded_count(f"content {index}") >= 1


@pytest.mark.asyncio
async def test_batch_capability_failure_disables_batching_for_the_rest_of_the_run(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One failed capability probe must not be repeated for every later batch."""
    monkeypatch.setattr(knowledge_file_indexer, "_INDEX_FILES_PER_BATCH", 4)
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 16)
    config = _config(tmp_path, docs_path)
    embedder.short_batch = True

    assert (await _manager(config).reindex_all()).indexed_count == 16

    multi_input_requests = [batch for batch in embedder.batch_requests if len(batch) > 1]
    assert len(multi_input_requests) == 1, (
        f"batch support was probed {len(multi_input_requests)} times: {multi_input_requests}"
    )


@pytest.mark.parametrize("status", [400, 404], ids=["bad_request", "unknown_model"])
@pytest.mark.asyncio
async def test_ordinary_permanent_batch_error_fails_fast_without_a_request_storm(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    status: int,
) -> None:
    """Only a cardinality fault earns a fallback; a bad request or model must not be retried per chunk.

    A rejected model or malformed request fails identically one input at a
    time, so degrading to per-item would turn one clear error into one failed
    request per chunk.
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 40)
    config = _config(tmp_path, docs_path)
    error = f"embedder request failed (HTTP {status})"
    embedder.batch_error = EmbedderRequestError(error)

    outcome = await _manager(config).reindex_all()

    assert outcome.error is not None
    assert error in outcome.error
    assert _publication(config) is None
    assert embedder.request_count <= 4, f"degraded into a request storm: {embedder.request_count}"
    assert _candidate(config).exists()


@pytest.mark.asyncio
async def test_single_input_wrong_cardinality_still_fails_and_does_not_publish(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed single-input response is never excused as a batching quirk."""
    monkeypatch.setattr(knowledge_file_indexer, "_INDEX_FILES_PER_BATCH", 1)
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    embedder.failures["content 0"] = [
        EmbedderRequestError("embedder returned 0 embeddings for 1 inputs", kind="cardinality") for _ in range(10)
    ]

    outcome = await _manager(config).reindex_all()

    assert outcome.error is not None
    assert "Indexed 1 of 2" in outcome.error
    assert _publication(config) is None
    assert _candidate(config).failed_paths() == {"doc0000.md"}


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
    embedder.short_batch = True
    embedder.failures["content 2"] = [EmbedderRequestError("embedder request failed (HTTP 422)") for _ in range(10)]

    outcome = await _manager(config).reindex_all()

    assert outcome.error is not None
    assert "Indexed 3 of 4" in outcome.error
    assert "embedder request failed (HTTP 422)" in outcome.error
    assert _publication(config) is None, "an incomplete snapshot must not publish"
    candidate = _candidate(config)
    assert candidate.failed_paths() == {"doc0002.md"}
    assert set(candidate.files()) == {"doc0000.md", "doc0001.md", "doc0003.md"}


@pytest.mark.asyncio
async def test_credential_rejection_during_fallback_still_fails_fast(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A global credential failure aborts instead of issuing one doomed request per chunk."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 40)
    config = _config(tmp_path, docs_path)
    embedder.short_batch = True
    embedder.fail_everything = EmbedderRequestError("embedder authentication failed (HTTP 401)", kind="credentials")

    outcome = await _manager(config).reindex_all()

    assert outcome.error is not None
    assert "embedder authentication failed (HTTP 401)" in outcome.error
    assert _publication(config) is None
    assert embedder.request_count <= 4, f"kept probing a rejected credential: {embedder.request_count}"
    assert _candidate(config).exists()


@pytest.mark.asyncio
async def test_restart_during_batch_fallback_resumes_the_same_candidate(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback mode does not change resume semantics."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 6)
    config = _config(tmp_path, docs_path)
    embedder.short_batch = True
    failing = _fail_files(monkeypatch, {"doc0005.md"})
    await _manager(config).reindex_all()
    failing.clear()

    candidate = _candidate(config)
    assert len(candidate.files()) == 5
    embedder.embedded_texts.clear()

    assert (await _manager(config).reindex_all()).indexed_count == 1

    for index in range(5):
        assert embedder.embedded_count(f"content {index}") == 0, "claimed files were re-embedded"
    publication = _publication(config)
    assert publication is not None
    assert publication.collection == candidate.name
    assert publication.indexed_count == 6


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
    assert lookup.reader is None, "incompatible vectors must not be queryable"
    resolution = resolve_agent_knowledge_access("helper", changed_config, runtime_paths)
    assert resolution.knowledge is None

    embedder.short_batch = True
    result = await refresh_knowledge_binding("docs", config=changed_config, runtime_paths=runtime_paths)

    assert result.published is True
    reopened = get_published_index("docs", config=changed_config, runtime_paths=runtime_paths)
    assert reopened.availability is KnowledgeAvailability.READY
    assert reopened.reader is not None


@pytest.mark.asyncio
async def test_progress_and_resumable_state_stay_accurate_under_batch_fallback(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falling back to per-item must not distort progress or resumability.

    Request accounting, completed/remaining/failed counts and the resumable
    candidate all have to keep telling the truth once batching is retired.
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 8)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    embedder.short_batch = True
    failing = _fail_files(monkeypatch, {"doc0006.md", "doc0007.md"})
    with capture_logs() as logs:
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    failing.clear()

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
    assert result.published is True
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
    monkeypatch.setattr(knowledge_file_indexer, "_INDEX_FILES_PER_BATCH", 4)
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 12)
    config = _config(tmp_path, docs_path)
    original_batch = _RecordingEmbedder.batch_request
    calls = 0

    def _short_after_first_batch(self: _RecordingEmbedder, texts: list[str]) -> list[list[float]]:
        nonlocal calls
        calls += 1
        # First batch succeeds normally; the second reveals the broken capability.
        self.short_batch = calls > 1
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
    publication = _publication(config)
    assert publication is not None
    assert publication.indexed_count == 12


@pytest.mark.asyncio
async def test_mtime_only_change_keeps_completed_vectors(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkout that rewrites mtimes must not destroy completed work.

    The content digest, not the stamp, decides whether a file still counts as
    indexed. Comparing the full signature deleted and re-embedded every
    byte-identical file after any mtime-rewriting checkout, archive restore, or
    clone.
    """
    # Files written just now sit inside the racy window, whose stamps are never trusted; close it so the stamp
    # comparison this test is about runs at all.
    monkeypatch.setattr(knowledge_corpus_signature, "_RACY_WINDOW_NS", 0)
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 5)
    config = _config(tmp_path, docs_path)
    failing = _fail_files(monkeypatch, {"doc0004.md"})
    await _manager(config).reindex_all()
    failing.clear()

    candidate = _candidate(config)
    assert len(candidate.files()) == 4
    vectors_before = len(stored_rows(candidate))

    # Same bytes, new mtimes: exactly what git checkout does.
    for index in range(5):
        path = docs_path / f"doc{index:04d}.md"
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns + 10**9, stat.st_mtime_ns + 10**9))
    embedder.embedded_texts.clear()

    assert (await _manager(config).reindex_all()).indexed_count == 1, "only the previously failed file is indexed"

    for index in range(4):
        assert embedder.embedded_count(f"content {index}") == 0, "an mtime change re-embedded unchanged content"
    assert len(stored_rows(candidate)) == vectors_before + 1
    assert _published_collection(config) == candidate.name


# --------------------------------------------------------------------------
# 16. Global embedder failures
# --------------------------------------------------------------------------


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
    embedder = _use_non_batching_embedder(monkeypatch)
    embedder.fail_everything = EmbedderRequestError("embedder authentication failed (HTTP 401)", kind="credentials")

    outcome = await _manager(config).reindex_all()

    assert outcome.error is not None
    assert "embedder authentication failed (HTTP 401)" in outcome.error
    assert _publication(config) is None
    assert embedder.request_count <= 4, f"issued one doomed request per file: {embedder.request_count}"
    assert _candidate(config).exists()


@pytest.mark.asyncio
async def test_repeated_non_auth_rejections_stop_the_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid model rejects everything; it must not be retried per file."""
    monkeypatch.setattr(knowledge_file_indexer, "_GLOBAL_EMBEDDER_FAILURE_STREAK", 5)
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 60)
    config = _config(tmp_path, docs_path)
    embedder = _RecordingNonBatchEmbedder()
    embedder.fail_everything = EmbedderRequestError("embedder request failed (HTTP 404)")
    indexer = _file_indexer(config, docs_path, KnowledgeCollection(path=tmp_path / "store", name="candidate"), embedder)
    completed: dict[str, FileSignature] = {}

    await indexer.index_files(
        [docs_path / name for name in names],
        completed=completed,
        failed=set(),
        vanished=set(),
        on_file_done=lambda: None,
    )

    assert embedder.request_count <= 12, f"issued one doomed request per file: {embedder.request_count}"
    assert indexer.global_failure == "embedder request failed (HTTP 404)"
    assert completed == {}


def test_unrelated_failure_does_not_reset_embedder_rejection_streak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a successful embedding proves that repeated provider failures are not global."""
    monkeypatch.setattr(knowledge_file_indexer, "_GLOBAL_EMBEDDER_FAILURE_STREAK", 2)
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 1)
    config = _config(tmp_path, docs_path)
    indexer = _file_indexer(
        config,
        docs_path,
        KnowledgeCollection(path=tmp_path / "store", name="candidate"),
        _RecordingNonBatchEmbedder(),
    )
    rejection = EmbedderRequestError("embedder request failed (HTTP 404)")

    indexer._record_embedder_rejection(rejection)
    indexer._record_embedder_rejection(RuntimeError("unrelated"))
    indexer._record_embedder_rejection(rejection)

    assert indexer.global_failure == str(rejection)


@pytest.mark.asyncio
async def test_isolated_failures_between_successes_do_not_stop_the_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A success between two rejections resets the streak, so isolated rejections never stop a refresh."""
    monkeypatch.setattr(knowledge_file_indexer, "_GLOBAL_EMBEDDER_FAILURE_STREAK", 2)
    # Concurrent indexing could finish both rejections back to back, so index one file at a time.
    (tmp_path / ".env").write_text("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY=1\n", encoding="utf-8")
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 12)
    config = _config(tmp_path, docs_path)
    embedder = _RecordingNonBatchEmbedder()
    for index in (3, 7):
        embedder.failures[f"content {index}"] = [
            EmbedderRequestError("embedder request failed (HTTP 422)") for _ in range(5)
        ]
    collection = KnowledgeCollection(path=tmp_path / "store", name="candidate")
    indexer = _file_indexer(config, docs_path, collection, embedder)
    failed: set[str] = set()

    await indexer.index_files(
        [docs_path / name for name in names],
        completed={},
        failed=failed,
        vanished=set(),
        on_file_done=lambda: None,
    )

    assert indexer.global_failure is None
    assert failed == collection.failed_paths() == {"doc0003.md", "doc0007.md"}
    assert len(collection.files()) == 10


@pytest.mark.asyncio
async def test_candidate_path_removal_is_batched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Dropping many paths must not cost one vector-store round trip each."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 40)
    config = _config(tmp_path, docs_path)
    failing = _fail_files(monkeypatch, {"doc0039.md"})
    await _manager(config).reindex_all()
    failing.clear()

    # Rewrite every file's content so reconciliation must drop all 39 entries.
    for index in range(39):
        (docs_path / f"doc{index:04d}.md").write_text(f"rewritten {index}", encoding="utf-8")

    original_reconcile = KnowledgeCollection.reconcile
    drops: list[int] = []

    def _count_drops(
        self: KnowledgeCollection,
        *,
        drop: Sequence[str],
        restamp: Mapping[str, FileSignature],
        revision: str | None,
        total_files: int,
        rebuild: bool,
    ) -> None:
        drops.append(len(drop))
        original_reconcile(
            self,
            drop=drop,
            restamp=restamp,
            revision=revision,
            total_files=total_files,
            rebuild=rebuild,
        )

    monkeypatch.setattr(KnowledgeCollection, "reconcile", _count_drops)

    assert (await _manager(config).reindex_all()).indexed_count == 40

    # 39 dropped paths must not cost 39 round trips. Rewriting a file replaces
    # its rows in that file's own write, so only the batched drops count here.
    assert sum(drops) >= 39, f"the dropped paths never left the candidate: {drops}"
    assert len(drops) <= 4, f"one store call per dropped path instead of one per round: {drops}"
    assert _publication(config) is not None


@pytest.mark.asyncio
async def test_an_interrupted_forced_rebuild_is_finished_even_when_the_source_did_not_change(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unchanged refresh must not republish the old index over a forced rebuild the operator started.

    It finishes the rebuild instead, without redoing the files the rebuild already embedded, and keeps nothing older.
    """
    (tmp_path / ".env").write_text("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY=1\n", encoding="utf-8")
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    old_collection = _published_collection(config)
    with monkeypatch.context() as patch:
        _cancel_after(patch, 1)
        with pytest.raises(asyncio.CancelledError):
            await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths, force_reindex=True)
    rebuild = _candidate(config)
    assert rebuild.meta().rebuild is True
    (finished,) = rebuild.files()
    embedder.embedded_texts.clear()

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.published is True
    publication = _publication(config)
    assert publication is not None
    assert publication.collection == rebuild.name, "the unchanged shortcut republished the distrusted index"
    assert publication.retired is None, "a forced rebuild kept the collection it replaced"
    assert old_collection not in _collection_names(config)
    assert sorted(embedder.embedded_texts) == sorted(
        f"content {index}" for index in range(3) if f"doc{index:04d}.md" != finished
    ), "finishing the rebuild redid the file it had finished, or skipped one it had not"


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
    assert "first error: knowledge indexing failed (ValueError)" in outcome.error
    assert _candidate(config).failed_paths() == {"doc0001.md"}


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
    assert _candidate(config).failed_paths() == {"doc0001.md"}


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
    monkeypatch.setattr(knowledge_file_indexer, "_MAX_BATCH_TEXT_BYTES", 4_000)
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
    monkeypatch.setattr(knowledge_file_indexer, "_MAX_BATCH_TEXT_BYTES", 1_000)
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "huge.md").write_text("y" * 50_000, encoding="utf-8")
    for index in range(3):
        (docs_path / f"small{index}.md").write_text(f"small body {index}", encoding="utf-8")
    config = _config(tmp_path, docs_path, chunk_size=100_000)

    assert (await _manager(config).reindex_all()).indexed_count == 4

    # Files are indexed in path order, so the huge file fills the first batch alone.
    assert embedder.single_requests == ["y" * 50_000]
    assert embedder.batch_requests == [("small body 0", "small body 1", "small body 2")]
    publication = _publication(config)
    assert publication is not None
    assert publication.indexed_count == 4
    assert "huge.md" in _stored_paths(config, _published_collection(config))


@pytest.mark.asyncio
async def test_batch_budget_counts_overlap_expansion_not_size_on_disk(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overlapping chunks repeat text, so the budget counts the chunk text a file produced, not its size."""
    monkeypatch.setattr(knowledge_file_indexer, "_MAX_BATCH_TEXT_BYTES", 2_600)
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "expanding.md").write_text(_overlapping_body(250), encoding="utf-8")
    for index in range(3):
        (docs_path / f"small{index}.md").write_text(f"small body {index}", encoding="utf-8")
    config = _config(tmp_path, docs_path, chunk_size=1_000, chunk_overlap=100)

    assert (await _manager(config).reindex_all()).indexed_count == 4

    expanding_chunks, small_bodies = embedder.batch_requests
    assert (docs_path / "expanding.md").stat().st_size < 2_600
    assert sum(len(text.encode("utf-8")) for text in expanding_chunks) >= 2_600
    assert small_bodies == ("small body 0", "small body 1", "small body 2")
    publication = _publication(config)
    assert publication is not None
    assert publication.indexed_count == 4


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
    publication = _publication(config)
    assert publication is not None
    assert publication.indexed_count == 1
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.reader is not None
    hits = await lookup.reader.asearch("token0000", limit=5)
    assert hits, "the published overlapping index returned nothing"
    assert all("token" in document.content for document in hits)


# --------------------------------------------------------------------------
# 17. Reusing published vectors for files a refresh did not change
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_after_publish_reuses_vectors_for_unchanged_files(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Changing one file must cost one embedding, not one per file in the corpus."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)

    assert (await _manager(config).reindex_all()).indexed_count == 3
    first_collection = _published_collection(config)
    published_ids = {row["id"] for row in stored_rows(_collection(config, first_collection))}

    (docs_path / names[1]).write_text("rewritten body", encoding="utf-8")
    embedder.embedded_texts.clear()

    assert (await _manager(config).reindex_all()).indexed_count == 1, "the whole corpus was embedded again"

    assert embedder.embedded_count("content 0") == 0
    assert embedder.embedded_count("content 2") == 0
    assert embedder.embedded_count("rewritten body") == 1
    publication = _publication(config)
    assert publication is not None
    assert publication.indexed_count == 3
    published = _published_collection(config)
    assert published != first_collection, "publication must still swap in a separate collection"
    assert _stored_paths(config, published) == sorted(names)
    reused_ids = {
        row["id"] for row in stored_rows(_collection(config, published)) if row["metadata"]["source_path"] != names[1]
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

    await _manager(config).reindex_all()
    first_collection = _published_collection(config)
    before = stored_rows(_collection(config, first_collection))

    (docs_path / names[0]).write_text("rewritten body", encoding="utf-8")
    run = await _manager(config)._open_candidate_run(force_reindex=False)

    assert run.collection.name != first_collection
    assert sorted(run.completed) == names
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
    embedder: _RecordingEmbedder,
) -> None:
    """A published path that is no longer managed must never reach the next publication."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)

    await _manager(config).reindex_all()
    (docs_path / names[0]).unlink()
    embedder.embedded_texts.clear()

    assert (await _manager(config).reindex_all()).indexed_count == 0

    assert embedder.embedded_texts == []
    assert _stored_paths(config, _published_collection(config)) == [names[1], names[2]]
    publication = _publication(config)
    assert publication is not None
    assert publication.indexed_count == 2


@pytest.mark.asyncio
async def test_a_finished_build_that_never_published_is_resumed_without_embedding(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A first build that completed but whose publication was lost is published from its own claims.

    Before any publication there is no older collection to distrust, and the collection's claims are exact.
    """
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)

    await _manager(config).reindex_all()
    finished = _published_collection(config)
    publication_path(_storage(config)).unlink()
    embedder.embedded_texts.clear()

    outcome = await _manager(config).reindex_all()

    assert outcome.published
    assert embedder.embedded_texts == []
    assert _published_collection(config) == finished


@pytest.mark.asyncio
async def test_published_vector_reuse_falls_back_when_the_published_collection_is_gone(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A vanished published collection must cost a full rebuild, never a failed refresh."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)

    await _manager(config).reindex_all()
    _collection(config, _published_collection(config)).delete()
    embedder.embedded_texts.clear()

    assert (await _manager(config).reindex_all()).indexed_count == 3

    publication = _publication(config)
    assert publication is not None
    assert publication.indexed_count == 3
    assert _collection(config, _published_collection(config)).exists()


@pytest.mark.asyncio
async def test_a_missing_published_collection_does_not_discard_a_compatible_candidate(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The candidate's own claims are exact, so it is resumed whatever became of the published collection."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    await _manager(config).reindex_all()
    (docs_path / names[1]).write_text("rewritten body", encoding="utf-8")
    failing = _fail_files(monkeypatch, {names[1]})
    await _manager(config).reindex_all()
    failing.clear()
    candidate = _candidate(config)
    _collection(config, _published_collection(config)).delete()
    embedder.embedded_texts.clear()

    assert (await _manager(config).reindex_all()).indexed_count == 1

    assert embedder.embedded_texts == ["rewritten body"]
    assert _published_collection(config) == candidate.name
    assert _stored_paths(config, candidate.name) == sorted(names)


@pytest.mark.asyncio
async def test_a_failed_vector_copy_leaves_no_partial_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy that dies part-way must hand the rebuild an empty candidate, never part of the copy."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)

    await _manager(config).reindex_all()
    original_write_meta = knowledge_vector_store._write_meta
    refused: list[bool] = []

    def _refuse_the_copys_state(connection: sqlite3.Connection, meta: CollectionMeta) -> None:
        if not refused:
            refused.append(True)
            message = "vector store refused the write"
            raise RuntimeError(message)
        original_write_meta(connection, meta)

    monkeypatch.setattr(knowledge_vector_store, "_write_meta", _refuse_the_copys_state)

    run = await _manager(config)._open_candidate_run(force_reindex=False)

    assert refused, "the copy never got as far as its state, so this proves nothing"
    assert run.completed == {}, "a failed copy must claim nothing"
    assert stored_rows(run.collection) == [], "partially copied rows survived the failure"
    assert run.collection.meta().stage == "building"
    assert not list(_storage(config).glob("*.building")), "the failed copy's file was left behind"


@pytest.mark.asyncio
async def test_published_vector_reuse_survives_a_checkout_that_only_rewrites_mtimes(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copied claims carry the published stamp, which a checkout routinely invalidates.

    Reconciliation adopts the new stamp for byte-identical content, so the
    signature the copy carries must survive that comparison rather than send
    every reused file back through the embedder.
    """
    monkeypatch.setattr(knowledge_corpus_signature, "_RACY_WINDOW_NS", 0)
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)

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
    assert _stored_paths(config, _published_collection(config)) == sorted(names)


@pytest.mark.asyncio
async def test_published_vector_reuse_keeps_the_claim_of_an_empty_file(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A zero-length file is claimed with no rows, so a copy carries it forward instead of losing it."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 2)
    (docs_path / "empty.md").write_text("", encoding="utf-8")
    config = _config(tmp_path, docs_path)

    await _manager(config).reindex_all()
    assert "empty.md" in _collection(config, _published_collection(config)).files()
    (docs_path / names[0]).write_text("rewritten body", encoding="utf-8")
    embedder.embedded_texts.clear()

    assert (await _manager(config).reindex_all()).indexed_count == 1, "the empty file was lost or indexed again"

    assert embedder.embedded_texts == ["rewritten body"]
    publication = _publication(config)
    assert publication is not None
    assert publication.indexed_count == 3
    published = _collection(config, _published_collection(config))
    assert sorted(published.files()) == sorted([*names, "empty.md"])
    assert _stored_paths(config, published.name) == sorted(names)


@pytest.mark.asyncio
async def test_a_cancelled_vector_copy_never_publishes_rows_it_could_not_claim(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation waits for the copy worker before releasing the refresh lock, and the copy lands whole.

    ``asyncio.to_thread`` cannot cancel its worker, so the coroutine must drain
    that worker before propagating ``CancelledError``. The copy's claims come
    with its rows, so the next refresh resumes it and drops a departed path
    instead of copying again.
    """
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)

    await _manager(config).reindex_all()
    original_copy = KnowledgeCollection.create_copy
    copy_started = Event()
    release_copy = Event()

    def _blocked_copy(self: KnowledgeCollection, source: KnowledgeCollection, meta: CollectionMeta) -> None:
        copy_started.set()
        assert release_copy.wait(timeout=10)
        original_copy(self, source, meta)

    monkeypatch.setattr(KnowledgeCollection, "create_copy", _blocked_copy)
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
    monkeypatch.setattr(KnowledgeCollection, "create_copy", original_copy)

    interrupted = _candidate(config)
    assert sorted(interrupted.files()) == sorted(names), "the copy's claims did not land with its rows"
    _assert_claims_match_rows(interrupted)

    (docs_path / names[0]).unlink()
    embedder.embedded_texts.clear()

    indexed = (await _manager(config).reindex_all()).indexed_count

    assert _published_collection(config) == interrupted.name, "the interrupted copy was not resumed"
    assert _stored_paths(config, interrupted.name) == sorted(names[1:]), (
        "a departed path survived in the published index"
    )
    assert indexed == 0, "recovery re-embedded instead of reusing the copied vectors"


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
    manager = _manager(config)
    await manager.reindex_all()
    published_collection = _published_collection(config)
    published_rows = stored_rows(_collection(config, published_collection))

    unreadable_candidate = candidate_collection_name(manager._collections)
    seed_collection(_collection(config, unreadable_candidate), published_rows, settings=_current_settings(config))
    corrupt(_collection(config, unreadable_candidate).file)

    run = await manager._open_candidate_run(force_reindex=False)

    assert run.resumed is False
    assert run.collection.name != unreadable_candidate
    assert sorted(run.completed) == names
    assert unreadable_candidate not in _collection_names(config)
    assert stored_rows(_collection(config, published_collection)) == published_rows


@pytest.mark.asyncio
async def test_a_transient_store_error_reading_a_candidate_preserves_resumable_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A locked or failing read of a candidate is not evidence the candidate is unusable, so it is not deleted."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    failing = _fail_files(monkeypatch, {"doc0002.md"})
    await _manager(config).reindex_all()
    failing.clear()
    candidate = _candidate(config)
    original_meta = KnowledgeCollection.meta

    def _locked(self: KnowledgeCollection) -> CollectionMeta:
        if self.name == candidate.name:
            message = "database is locked"
            raise sqlite3.OperationalError(message)
        return original_meta(self)

    monkeypatch.setattr(KnowledgeCollection, "meta", _locked)

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        await _manager(config)._open_candidate_run(force_reindex=False)

    assert candidate.exists()
    assert len(candidate.files()) == 2


@pytest.mark.asyncio
async def test_opening_a_candidate_constructs_no_embedder(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Choosing what to resume reads the store without an embedder; only the run constructs one."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    seed_collection(
        _collection(config, candidate_collection_name(_manager(config)._collections)),
        [],
        settings=_current_settings(config),
    )
    created: list[Embedder] = []

    def _record_factory(*_args: object, **_kwargs: object) -> Embedder:
        created.append(embedder)
        return embedder

    monkeypatch.setattr(knowledge_manager_module, "create_configured_embedder", _record_factory)

    run = await _manager(config)._open_candidate_run(force_reindex=False)
    assert run.resumed is True
    assert created == [], "opening the candidate constructed a configured embedder"

    assert (await _manager(config).reindex_all()).published
    assert len(created) == 1


@pytest.mark.asyncio
async def test_an_interruption_after_the_copy_still_resumes_the_work_it_finished(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copied candidate that went on to index some files stays resumable with all of that work."""
    (tmp_path / ".env").write_text("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY=1\n", encoding="utf-8")
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    assert max_concurrent_file_indexes(runtime_paths_for(config)) == 1
    await _manager(config).reindex_all()

    for index, name in enumerate(names):
        (docs_path / name).write_text(f"revised {index}", encoding="utf-8")
    embedder.embedded_texts.clear()

    with monkeypatch.context() as patch:
        _cancel_after(patch, 2)
        with pytest.raises(asyncio.CancelledError):
            await _manager(config).reindex_all()

    completed = _candidate(config).files()
    assert len(completed) == 2
    already_embedded = {f"revised {names.index(name)}" for name in completed}

    assert (await _manager(config).reindex_all()).indexed_count == len(names) - 2

    for body in already_embedded:
        assert embedder.embedded_count(body) == 1, "an interrupted refresh re-embedded work it had finished"


@pytest.mark.asyncio
async def test_a_candidate_with_no_vectors_and_no_claims_is_still_resumed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claiming nothing is not itself a fault: a candidate whose every file failed is resumed, not rebuilt."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    _fail_files(monkeypatch, set(names))

    await _manager(config).reindex_all()
    first = _candidate(config)
    assert first.files() == {}
    assert first.failed_paths() == set(names)
    assert _stored_paths(config, first.name) == []

    await _manager(config).reindex_all()

    assert _candidate(config).name == first.name, "an empty candidate was needlessly rebuilt"
    assert first.failed_paths() == set(names)


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
    failing = _fail_files(monkeypatch, {names[-1]})
    failed = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert failed.published is False
    assert len(_candidate(config).files()) == 2, "the candidate should carry the work the failed refresh finished"

    failing.clear()
    embedder.embedded_texts.clear()

    result = await refresh_knowledge_binding(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
        force_reindex=True,
    )

    assert result.published is True
    assert result.indexed_count == 3, "a forced rebuild kept the candidate's claims"
    for index in range(3):
        assert embedder.embedded_count(f"revised {index}") == 1


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

    assert knowledge_registry.get_published_index("docs", config=config, runtime_paths=runtime_paths).reader is None
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.published is True
    assert result.indexed_count == 3, "the refresh reused vectors embedded without the model's prompts"
    for index in range(3):
        assert embedder.embedded_count(f"content {index}") == 1


# --------------------------------------------------------------------------
# Collection ownership
# --------------------------------------------------------------------------


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

    assert space._default_collection == _collection_name_for_base("docs", docs)
    assert "default_collection" not in CollectionSpace.__dataclass_fields__, (
        "default_collection is a constructor field again, so it can be supplied instead of derived"
    )

    other_base = replace(space, base_id="wiki")
    other_path = replace(space, knowledge_path=Path("/srv/knowledge/wiki"))
    assert other_base._default_collection != space._default_collection
    assert other_path._default_collection != space._default_collection


# --------------------------------------------------------------------------
# 18. Taking over the collection a publication retired instead of copying the index
# --------------------------------------------------------------------------


class _Crash(BaseException):
    """A process death at one chosen point of a refresh: no ``except Exception`` handler may absorb it."""


@dataclass
class _StoreWrites:
    """What refreshes wrote into the real store, counted by thin wrappers around its write methods."""

    #: Compact copies of another collection since the last ``clear``.
    copies: int = 0
    #: Rows written for indexed files since the last ``clear``.
    file_rows: int = 0
    lock: Lock = field(default_factory=Lock)

    def clear(self) -> None:
        with self.lock:
            self.copies = 0
            self.file_rows = 0

    def record_copy(self) -> None:
        with self.lock:
            self.copies += 1

    def record_file_rows(self, rows: int) -> None:
        with self.lock:
            self.file_rows += rows


@pytest.fixture
def store_writes(monkeypatch: pytest.MonkeyPatch) -> _StoreWrites:
    """Count what refreshes write into the real store, by how they write it."""
    writes = _StoreWrites()
    original_write = KnowledgeCollection.write_file
    original_copy = KnowledgeCollection.create_copy

    def _write(
        self: KnowledgeCollection,
        relative_path: str,
        signature: FileSignature,
        rows: Sequence[StoredRow],
    ) -> None:
        original_write(self, relative_path, signature, rows)
        writes.record_file_rows(len(rows))

    def _copy(self: KnowledgeCollection, source: KnowledgeCollection, meta: CollectionMeta) -> None:
        original_copy(self, source, meta)
        writes.record_copy()

    monkeypatch.setattr(KnowledgeCollection, "write_file", _write)
    monkeypatch.setattr(KnowledgeCollection, "create_copy", _copy)
    return writes


def _served(config: Config, collection: str) -> list[tuple[str, str]]:
    """Return what one collection serves, as sorted ``(source path, text)`` rows."""
    return sorted(
        (row["metadata"]["source_path"], row["content"]) for row in stored_rows(_collection(config, collection))
    )


def _corpus(docs_path: Path) -> list[tuple[str, str]]:
    """Return what an exact index of the files on disk serves: one row per file, holding its text."""
    return sorted((path.name, path.read_text(encoding="utf-8")) for path in docs_path.iterdir())


async def _refresh_and_check(config: Config, docs_path: Path) -> str:
    """Refresh, assert the new publication serves exactly the files on disk, and return it."""
    assert (await _manager(config).reindex_all()).published
    published = _published_collection(config)
    assert _served(config, published) == _corpus(docs_path), "the publication lost, duplicated or kept stale rows"
    return published


async def _build_steady_state(config: Config, docs_path: Path, names: Sequence[str]) -> None:
    """Build cold, then publish twice, so the next refresh has a retired collection to take over."""
    await _refresh_and_check(config, docs_path)
    for step in range(2):
        (docs_path / names[step]).write_text(f"warm-up change {step}", encoding="utf-8")
        await _refresh_and_check(config, docs_path)


def _rows_by_id(config: Config, collection: str) -> dict[str, tuple[str, str]]:
    return {
        row["id"]: (row["metadata"]["source_path"], row["content"])
        for row in stored_rows(_collection(config, collection))
    }


@pytest.mark.asyncio
async def test_a_refresh_writes_only_the_rows_that_changed_once_a_retired_collection_is_kept(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
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
    publication = _publication(config)
    assert publication is not None
    assert publication.retired == cold
    assert _rows_by_id(config, cold) == cold_rows, "the retired collection was dropped or written after it was retired"
    assert get_knowledge_index_status("docs", config=config, runtime_paths=runtime_paths).candidate is None, (
        "a kept collection must not look like an in-progress candidate"
    )

    (docs_path / names[1]).write_text("second change", encoding="utf-8")
    (docs_path / names[2]).unlink()
    (docs_path / "added.md").write_text("added body", encoding="utf-8")
    store_writes.clear()
    embedder.embedded_texts.clear()

    assert await _refresh_and_check(config, docs_path) == cold, "the retired collection was not taken over"

    assert store_writes.copies == 0, "the refresh copied the published index"
    # The taken-over collection is one publication behind, so the previous refresh's change is written again too.
    assert sorted(embedder.embedded_texts) == ["added body", "first change", "second change"]
    assert store_writes.file_rows == 3, "the refresh rewrote rows it did not change"
    untouched = set(names[3:])
    assert {i: r for i, r in _rows_by_id(config, cold).items() if r[0] in untouched} == {
        i: r for i, r in cold_rows.items() if r[0] in untouched
    }, "unchanged rows were rewritten"
    assert second in _collection_names(config), "the collection this publication retired was not kept in turn"
    assert sorted(_collection_names(config)) == sorted({cold, second})


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
            assert store_writes.copies == 0, f"refresh {step} copied the published index"
        assert len(_collection_names(config)) <= 2, "more than the published and the kept collection stayed on disk"
        for name in _collection_names(config):
            _assert_claims_match_rows(_collection(config, name))


def _crash_after(target: Callable[..., object]) -> Callable[..., object]:
    """Return ``target`` wrapped to raise ``_Crash`` once it has done its work."""

    def _wrapper(*args: object, **kwargs: object) -> object:
        target(*args, **kwargs)
        raise _Crash

    return _wrapper


def _inject_crash(patch: pytest.MonkeyPatch, crash_point: str) -> None:
    """Make the next refresh die at ``crash_point``: inside a store call, or just after one landed."""
    landed: dict[str, tuple[object, Callable[..., object]]] = {
        "create_copy": (KnowledgeCollection, KnowledgeCollection.create_copy),
        "reconcile": (KnowledgeCollection, KnowledgeCollection.reconcile),
        "mark_complete": (KnowledgeCollection, KnowledgeCollection.mark_complete),
        "save_publication": (knowledge_manager_module, knowledge_manager_module.save_publication),
    }
    if crash_point in landed:
        owner, target = landed[crash_point]
        patch.setattr(owner, crash_point, _crash_after(target))
    elif crash_point == "copy_midway":

        def _crash_writing_state(_connection: sqlite3.Connection, _meta: CollectionMeta) -> None:
            raise _Crash

        patch.setattr(knowledge_vector_store, "_write_meta", _crash_writing_state)
    elif crash_point == "write_file":
        original_write = KnowledgeCollection.write_file

        def _crash_after_added_rows(
            self: KnowledgeCollection,
            relative_path: str,
            signature: FileSignature,
            rows: Sequence[StoredRow],
        ) -> None:
            original_write(self, relative_path, signature, rows)
            if relative_path == "added.md":
                raise _Crash

        patch.setattr(KnowledgeCollection, "write_file", _crash_after_added_rows)
    else:
        assert crash_point == "sweep"
        original_cleanup = knowledge_manager_module.cleanup_superseded_collections

        def _crash_before_sweeping(
            space: CollectionSpace,
            *,
            preserved: frozenset[str],
            candidates_only: bool = False,
        ) -> None:
            if not candidates_only:
                raise _Crash
            original_cleanup(space, preserved=preserved, candidates_only=candidates_only)

        patch.setattr(knowledge_manager_module, "cleanup_superseded_collections", _crash_before_sweeping)


@pytest.mark.parametrize(
    "crash_point",
    ["copy_midway", "create_copy", "reconcile", "write_file", "mark_complete", "save_publication", "sweep"],
)
@pytest.mark.asyncio
async def test_a_crash_at_any_store_call_leaves_a_whole_index_and_costs_at_most_one_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_writes: _StoreWrites,
    crash_point: str,
) -> None:
    """A crash anywhere leaves readers the old or the new index and claims that match rows, and costs at most a copy.

    Later refreshes converge on an exact index without keeping more than the published and the kept collection.
    """
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 8)
    config = _config(tmp_path, docs_path)
    if crash_point in {"copy_midway", "create_copy"}:
        # Only a refresh with no kept collection copies the publication.
        await _refresh_and_check(config, docs_path)
    else:
        await _build_steady_state(config, docs_path, names)
    old_corpus = _corpus(docs_path)
    (docs_path / names[3]).write_text("changed before the crash", encoding="utf-8")
    (docs_path / "added.md").write_text("added before the crash", encoding="utf-8")
    new_corpus = _corpus(docs_path)

    with monkeypatch.context() as patch:
        _inject_crash(patch, crash_point)
        with pytest.raises(_Crash):
            await _manager(config).reindex_all()
    if crash_point == "copy_midway":
        assert list(_storage(config).glob("*.building")), "the copy died before its file could appear, as intended"

    served = _served(config, _published_collection(config))
    assert served in (old_corpus, new_corpus), "a crash left a published index that is neither old nor new"
    for name in _collection_names(config):
        _assert_claims_match_rows(_collection(config, name))

    copies = 0
    for step in range(4):
        (docs_path / names[step]).write_text(f"after the crash {step}", encoding="utf-8")
        store_writes.clear()
        await _refresh_and_check(config, docs_path)
        copies += store_writes.copies
    assert copies <= 1, f"the crash cost {copies} copies of the whole index"
    assert len(_collection_names(config)) <= 2
    assert not list(_storage(config).glob("*.building")), "a crashed copy's file outlived the refreshes after it"


@pytest.mark.asyncio
async def test_rows_a_crash_wrote_for_a_file_deleted_since_are_never_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_writes: _StoreWrites,
) -> None:
    """Rows land with their claim, so the refresh after a crash drops a file that left the corpus meanwhile."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 6)
    config = _config(tmp_path, docs_path)
    await _build_steady_state(config, docs_path, names)
    (docs_path / "transient.md").write_text("transient body", encoding="utf-8")
    original_write = KnowledgeCollection.write_file

    def _crash_after_transient_rows(
        self: KnowledgeCollection,
        relative_path: str,
        signature: FileSignature,
        rows: Sequence[StoredRow],
    ) -> None:
        original_write(self, relative_path, signature, rows)
        if relative_path == "transient.md":
            raise _Crash

    with monkeypatch.context() as patch:
        patch.setattr(KnowledgeCollection, "write_file", _crash_after_transient_rows)
        with pytest.raises(_Crash):
            await _manager(config).reindex_all()
    (docs_path / "transient.md").unlink()
    (docs_path / names[4]).write_text("change after the crash", encoding="utf-8")
    store_writes.clear()

    await _refresh_and_check(config, docs_path)

    for step in range(4):
        (docs_path / names[step]).write_text(f"later change {step}", encoding="utf-8")
        await _refresh_and_check(config, docs_path)
    assert store_writes.copies == 0, "the crash cost a copy of the whole index"


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

    outcome = await _manager(config).reindex_all(force_reindex=change == "forced")

    assert outcome.published
    assert outcome.indexed_count == len(names), "the rebuild reused vectors it was asked to replace"
    assert sorted(embedder.embedded_texts) == sorted(text for _name, text in _corpus(docs_path))
    assert not old_collections & set(_collection_names(config)), "a collection holding old vectors survived"
    publication = _publication(config)
    assert publication is not None
    assert publication.retired is None
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
    counted_copy = KnowledgeCollection.create_copy

    def _recording_copy(self: KnowledgeCollection, source: KnowledgeCollection, meta: CollectionMeta) -> None:
        collections_while_copying.append(len(_collection_names(config)))
        counted_copy(self, source, meta)

    monkeypatch.setattr(KnowledgeCollection, "create_copy", _recording_copy)
    store_writes.clear()

    # Two take-overs leave both collections mostly empty; the next two refreshes replace each with a fresh copy.
    for step in range(4):
        (docs_path / names[step]).write_text(f"edit {step}", encoding="utf-8")
        await _refresh_and_check(config, docs_path)

    assert store_writes.copies > 0, "no collection left mostly empty was copied afresh"
    # The worn collection is deleted before the copy starts, so the copy never shares the disk with it.
    assert max(collections_while_copying) == 1
    assert all(_collection(config, name).free_fraction() <= 0.25 for name in _collection_names(config))


@pytest.mark.asyncio
async def test_a_refresh_that_finds_nothing_changed_keeps_the_retired_collection(
    tmp_path: Path,
    store_writes: _StoreWrites,
) -> None:
    """The unchanged-source path must not discard the collection the next change takes over."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await _build_steady_state(config, docs_path, names)
    collections = set(_collection_names(config))

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.published is True
    assert set(_collection_names(config)) == collections
    assert get_knowledge_index_status("docs", config=config, runtime_paths=runtime_paths).candidate is None
    (docs_path / names[3]).write_text("change after an idle refresh", encoding="utf-8")
    store_writes.clear()
    await _refresh_and_check(config, docs_path)
    assert store_writes.copies == 0, "the idle refresh dropped the collection the next change could take over"


@pytest.mark.asyncio
async def test_resuming_the_retired_collection_waits_for_readers_still_holding_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reader that selected the retired collection before the swap must finish before it is written."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    storage = _storage(config)
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
    # Candidate cleanup waits on the same lock; without it, only the take-over itself stands between the two.
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


@pytest.mark.parametrize("store_call", ["reconcile", "mark_complete", "save_publication"])
@pytest.mark.asyncio
async def test_a_cancelled_refresh_finishes_its_store_call_before_it_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_call: str,
) -> None:
    """A store call still running after cancellation could undo the next refresh's, such as a forced rebuild's."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    await _build_steady_state(config, docs_path, names)
    (docs_path / names[1]).write_text("change before the cancellation", encoding="utf-8")
    started = Event()
    release = Event()
    finished = Event()

    def _blocking(target: Callable[..., object]) -> Callable[..., object]:
        def _wrapper(*args: object, **kwargs: object) -> object:
            started.set()
            assert release.wait(timeout=10)
            result = target(*args, **kwargs)
            finished.set()
            return result

        return _wrapper

    with monkeypatch.context() as patch:
        if store_call == "reconcile":
            patch.setattr(KnowledgeCollection, "reconcile", _blocking(KnowledgeCollection.reconcile))
        elif store_call == "mark_complete":
            patch.setattr(KnowledgeCollection, "mark_complete", _blocking(KnowledgeCollection.mark_complete))
        else:
            patch.setattr(
                knowledge_manager_module,
                "save_publication",
                _blocking(knowledge_manager_module.save_publication),
            )
        refresh = asyncio.create_task(_manager(config).reindex_all())
        assert await asyncio.to_thread(started.wait, 10)
        refresh.cancel()
        await asyncio.sleep(0.1)
        try:
            assert not refresh.done(), "the cancellation escaped while the store call was still running"
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await refresh
        assert finished.is_set()
    await _refresh_and_check(config, docs_path)


@pytest.mark.parametrize("damage", ["unreadable", "missing"])
@pytest.mark.asyncio
async def test_a_damaged_retired_collection_costs_one_copy_instead_of_failing(
    tmp_path: Path,
    store_writes: _StoreWrites,
    damage: str,
) -> None:
    """A kept collection the store cannot read, or that is gone, is dropped and the index copied once."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 6)
    config = _config(tmp_path, docs_path)
    await _build_steady_state(config, docs_path, names)
    (retired,) = set(_collection_names(config)) - {_published_collection(config)}
    retired_collection = _collection(config, retired)
    if damage == "unreadable":
        retired_collection.file.write_bytes(b"not a knowledge collection" * 100)
    else:
        retired_collection.delete()
    (docs_path / names[3]).write_text("change after the damage", encoding="utf-8")
    store_writes.clear()

    await _refresh_and_check(config, docs_path)

    assert store_writes.copies == 1
    assert retired not in _collection_names(config), "the damaged collection was kept"
    store_writes.clear()
    for step in range(2):
        (docs_path / names[step]).write_text(f"later change {step}", encoding="utf-8")
        await _refresh_and_check(config, docs_path)
    assert store_writes.copies == 0


@pytest.mark.asyncio
async def test_a_forced_rebuild_that_crashed_before_publishing_is_published_without_embedding(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forced rebuild that completed but crashed before its publication is published next, keeping nothing older."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    with monkeypatch.context() as patch:
        _inject_crash(patch, "mark_complete")
        with pytest.raises(_Crash):
            await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths, force_reindex=True)
    (rebuild,) = _working_collections(config)
    assert rebuild.meta().rebuild is True
    assert rebuild.meta().stage == "complete"
    embedder.embedded_texts.clear()
    assert (await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)).published
    publication = _publication(config)
    assert publication is not None
    assert publication.collection == rebuild.name
    assert publication.retired is None
    assert embedder.embedded_texts == []


@pytest.mark.asyncio
async def test_a_forced_rebuild_a_later_one_replaced_never_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replaced forced rebuild that an interrupted cleanup spared is a leftover, not a rebuild still to publish."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths, force_reindex=True)
    replaced = _published_collection(config)
    with monkeypatch.context() as patch:
        _inject_crash(patch, "save_publication")
        with pytest.raises(_Crash):
            await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths, force_reindex=True)
    replacement = _published_collection(config)
    assert replacement != replaced
    assert replaced in _collection_names(config)

    assert (await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)).published
    assert _published_collection(config) == replacement

    (docs_path / "doc0000.md").write_text("edited after the rebuild", encoding="utf-8")
    assert (await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)).published
    publication = _publication(config)
    assert publication is not None
    assert publication.retired == replacement
    assert replaced not in _collection_names(config)


@pytest.mark.asyncio
async def test_an_interrupted_forced_rebuild_never_leaves_more_than_two_collections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forced rebuild deletes the working collections before it starts, so an interruption leaves two."""
    (tmp_path / ".env").write_text("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY=1\n", encoding="utf-8")
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 3)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    (docs_path / names[0]).write_text("edit", encoding="utf-8")
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert len(_collection_names(config)) == 2
    _cancel_after(monkeypatch, 1)
    with pytest.raises(asyncio.CancelledError):
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths, force_reindex=True)
    assert len(_collection_names(config)) == 2


@pytest.mark.asyncio
async def test_an_unfinished_takeover_of_the_retired_collection_resumes_whatever_its_free_space(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A takeover interrupted midway resumes as an unfinished build, so the free-space rule never discards its work."""
    (tmp_path / ".env").write_text("MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY=1\n", encoding="utf-8")
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    (docs_path / names[0]).write_text("edit zero", encoding="utf-8")
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    publication = _publication(config)
    assert publication is not None
    assert publication.retired is not None
    (docs_path / names[1]).write_text("edit one", encoding="utf-8")
    (docs_path / names[2]).write_text("edit two", encoding="utf-8")
    with monkeypatch.context() as patch:
        finished = _cancel_after(patch, 2)
        with pytest.raises(asyncio.CancelledError):
            await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert len(finished) == 2
    monkeypatch.setattr(knowledge_manager_module, "_MAX_REUSED_FREE_FRACTION", -1.0)
    embedder.embedded_texts.clear()
    assert (await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)).published
    assert embedder.embedded_texts == ["edit two"]


@pytest.mark.asyncio
async def test_a_missing_published_file_lets_a_worn_retired_collection_resume(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no published file to copy, a retired collection is resumed whatever its free space."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 4)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    (docs_path / names[0]).write_text("edit zero", encoding="utf-8")
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    _collection(config, _published_collection(config)).delete()
    monkeypatch.setattr(knowledge_manager_module, "_MAX_REUSED_FREE_FRACTION", -1.0)
    embedder.embedded_texts.clear()
    assert (await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)).published
    assert embedder.embedded_texts == ["edit zero"]


@pytest.mark.asyncio
async def test_an_unchanged_source_rebuilds_a_published_collection_it_cannot_read(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """A published file of another schema is rebuilt rather than republished for every search to fail on."""
    docs_path = tmp_path / "docs"
    _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    unreadable = _published_collection(config)
    with closing(sqlite3.connect(_collection(config, unreadable).file)) as connection:
        connection.execute("PRAGMA user_version = 1")
    embedder.embedded_texts.clear()

    assert (await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)).published

    assert _published_collection(config) != unreadable
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.reader is not None
    assert await lookup.reader.asearch("content", limit=1)
    assert embedder.embedded_count("content 0") == 1


@pytest.mark.asyncio
async def test_a_mode_round_trip_through_files_resumes_the_semantic_index(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
) -> None:
    """Switching a semantic base to files mode and back rewrites only the files that changed meanwhile."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 5)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    files_config = bind_runtime_paths(
        config.model_copy(
            update={
                "knowledge_bases": {
                    "docs": config.knowledge_bases["docs"].model_copy(update={"mode": "files"}),
                },
            },
        ),
        runtime_paths,
    )
    await refresh_knowledge_binding("docs", config=files_config, runtime_paths=runtime_paths)
    (docs_path / names[0]).write_text("edited while in files mode", encoding="utf-8")
    embedder.embedded_texts.clear()

    assert (await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)).published

    assert embedder.embedded_texts == ["edited while in files mode"]


@pytest.mark.asyncio
@pytest.mark.parametrize("publications", [1, 2], ids=["copied_candidate", "taken_over_retired_collection"])
async def test_an_unchanged_republish_discards_the_build_a_reverted_change_left(
    tmp_path: Path,
    embedder: _RecordingEmbedder,
    monkeypatch: pytest.MonkeyPatch,
    publications: int,
) -> None:
    """A failed build for a file since deleted is obsolete once the source matches the publication again."""
    del embedder
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    for index in range(publications):
        (docs_path / names[0]).write_text(f"revision {index}", encoding="utf-8")
        assert (await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)).published
    retired_before = _publication(config)
    assert retired_before is not None
    (docs_path / "bad.md").write_text("cannot index", encoding="utf-8")
    failing = _fail_files(monkeypatch, {"bad.md"})
    assert not (await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)).published
    assert get_knowledge_index_status("docs", config=config, runtime_paths=runtime_paths).candidate is not None

    (docs_path / "bad.md").unlink()
    failing.clear()
    assert (await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)).published

    assert get_knowledge_index_status("docs", config=config, runtime_paths=runtime_paths).candidate is None
    assert [collection.meta().stage for collection in _working_collections(config)] in ([], ["complete"])


@pytest.mark.asyncio
async def test_an_unchanged_republish_keeps_the_retired_collection_no_build_took_over(tmp_path: Path) -> None:
    """Discarding a leftover build spares the retired collection, so the next change still rewrites only its files."""
    docs_path = tmp_path / "docs"
    names = _write_corpus(docs_path, 2)
    config = _config(tmp_path, docs_path)
    runtime_paths = runtime_paths_for(config)
    for index in range(2):
        (docs_path / names[0]).write_text(f"revision {index}", encoding="utf-8")
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    publication = _publication(config)
    assert publication is not None
    assert publication.retired is not None
    leftover = _collection(config, candidate_collection_name(_manager(config)._collections))
    leftover.create(collection_meta(_current_settings(config)))

    assert (await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)).published

    assert [collection.name for collection in _working_collections(config)] == [publication.retired]
