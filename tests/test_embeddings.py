"""Tests for MindRoom embedding helpers."""

from __future__ import annotations

import gc
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest
from agno.exceptions import EmbeddingError

import mindroom.embeddings.signatures as embedding_helpers
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.embeddings.openai_embedder import MindRoomOpenAIEmbedder
from mindroom.embeddings.signatures import (
    create_sentence_transformers_embedder,
    effective_knowledge_embedder_signature,
    effective_mem0_embedder_signature,
)
from mindroom.model_defaults import OPENAI_EMBEDDING_LARGE, SENTENCE_TRANSFORMERS_DEFAULT

TEST_RUNTIME_PATHS = resolve_primary_runtime_paths(config_path=Path("config.yaml"))


@pytest.fixture(autouse=True)
def _isolate_sentence_transformer_client_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embedding_helpers, "_SENTENCE_TRANSFORMER_CLIENT", None)


def _mock_openai_client() -> MagicMock:
    client = MagicMock()
    client.embeddings.create.return_value = MagicMock()
    return client


def test_custom_host_non_openai_model_omits_dimensions() -> None:
    """OpenAI-compatible custom models should not inherit OpenAI's 1536-d fallback."""
    client = _mock_openai_client()
    embedder = MindRoomOpenAIEmbedder(
        id="gemini-embedding-2",
        api_key="sk-test",
        base_url="http://example.com/v1",
        openai_client=client,
    )

    embedder.response("hello")

    _, kwargs = client.embeddings.create.call_args
    assert "dimensions" not in kwargs


def test_custom_host_official_openai_model_keeps_dimensions() -> None:
    """Known OpenAI embedding models should keep their explicit dimensionality."""
    client = _mock_openai_client()
    embedder = MindRoomOpenAIEmbedder(
        id="text-embedding-3-small",
        api_key="sk-test",
        base_url="http://example.com/v1",
        openai_client=client,
    )

    embedder.response("hello")

    _, kwargs = client.embeddings.create.call_args
    assert kwargs["dimensions"] == 1536


def test_official_openai_ada_omits_dimensions() -> None:
    """Legacy OpenAI ada requests should not include the newer dimensions parameter."""
    client = _mock_openai_client()
    embedder = MindRoomOpenAIEmbedder(
        id="text-embedding-ada-002",
        api_key="sk-test",
        openai_client=client,
    )

    embedder.response("hello")

    _, kwargs = client.embeddings.create.call_args
    assert "dimensions" not in kwargs


def test_custom_host_explicit_dimensions_override_is_preserved() -> None:
    """Explicit dimensions should still be forwarded for custom-host models."""
    client = _mock_openai_client()
    embedder = MindRoomOpenAIEmbedder(
        id="gemini-embedding-2",
        api_key="sk-test",
        base_url="http://example.com/v1",
        dimensions=3072,
        openai_client=client,
    )

    embedder.response("hello")

    _, kwargs = client.embeddings.create.call_args
    assert kwargs["dimensions"] == 3072


@pytest.mark.asyncio
async def test_custom_host_batch_embedding_omits_dimensions() -> None:
    """Async batch requests should use the same custom-host dimension rules as single requests."""
    async_client = MagicMock()
    async_client.embeddings.create = AsyncMock(
        return_value=SimpleNamespace(
            data=[
                SimpleNamespace(embedding=[1.0, 2.0]),
                SimpleNamespace(embedding=[3.0, 4.0]),
            ],
            usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 2}),
        ),
    )
    embedder = MindRoomOpenAIEmbedder(
        id="gemini-embedding-2",
        api_key="sk-test",
        base_url="http://example.com/v1",
        async_client=async_client,
    )

    embeddings, usage = await embedder.async_get_embeddings_batch_and_usage(["hello", "world"])

    assert embeddings == [[1.0, 2.0], [3.0, 4.0]]
    assert usage == [{"total_tokens": 2}, {"total_tokens": 2}]
    _, kwargs = async_client.embeddings.create.call_args
    assert kwargs["input"] == ["hello", "world"]
    assert "dimensions" not in kwargs


def test_create_sentence_transformers_embedder_auto_installs_optional_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local embedder creation should ensure the optional runtime and pass through config."""
    captured: dict[str, object] = {}

    class DummyEmbedder:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.sentence_transformer_client = kwargs.get("sentence_transformer_client", object())

    def _ensure(runtime_paths: object) -> None:
        captured["installed"] = runtime_paths

    monkeypatch.setattr("mindroom.embeddings.signatures.ensure_sentence_transformers_dependencies", _ensure)
    monkeypatch.setattr(
        "mindroom.embeddings.signatures.importlib.import_module",
        lambda name: SimpleNamespace(SentenceTransformerEmbedder=DummyEmbedder) if name else None,
    )

    embedder = create_sentence_transformers_embedder(
        TEST_RUNTIME_PATHS,
        SENTENCE_TRANSFORMERS_DEFAULT,
        dimensions=384,
    )

    assert captured["installed"] == TEST_RUNTIME_PATHS
    assert isinstance(embedder, DummyEmbedder)
    assert embedder.kwargs == {
        "id": SENTENCE_TRANSFORMERS_DEFAULT,
        "dimensions": 384,
    }


def test_sentence_transformers_embedder_reuses_one_model_client_across_concurrent_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent index handles should share one expensive local model client."""
    construction_lock = threading.Lock()
    model_constructions = 0

    def _ensure(_runtime_paths: object) -> None:
        pass

    class DummyEmbedder:
        def __init__(
            self,
            *,
            sentence_transformer_client: object | None = None,
            **kwargs: object,
        ) -> None:
            nonlocal model_constructions
            self.id = kwargs["id"]
            self.dimensions = kwargs.get("dimensions")
            if sentence_transformer_client is None:
                with construction_lock:
                    model_constructions += 1
                time.sleep(0.02)
                sentence_transformer_client = object()
            self.sentence_transformer_client = sentence_transformer_client

    monkeypatch.setattr("mindroom.embeddings.signatures.ensure_sentence_transformers_dependencies", _ensure)
    monkeypatch.setattr(
        "mindroom.embeddings.signatures.importlib.import_module",
        lambda _name: SimpleNamespace(SentenceTransformerEmbedder=DummyEmbedder),
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        embedders = list(
            pool.map(
                lambda _index: create_sentence_transformers_embedder(
                    TEST_RUNTIME_PATHS,
                    "sentence-transformers/cache-regression-test",
                    dimensions=384 if _index % 2 == 0 else 768,
                ),
                range(8),
            ),
        )

    assert model_constructions == 1
    assert len({id(embedder) for embedder in embedders}) == 8
    assert len({id(embedder.sentence_transformer_client) for embedder in embedders}) == 1
    assert {embedder.dimensions for embedder in embedders} == {384, 768}


def test_sentence_transformers_embedder_releases_retired_cached_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching the active model should not permanently retain the old client."""

    class ModelClient:
        pass

    class DummyEmbedder:
        def __init__(
            self,
            *,
            sentence_transformer_client: object | None = None,
            **_kwargs: object,
        ) -> None:
            self.sentence_transformer_client = sentence_transformer_client or ModelClient()

    monkeypatch.setattr("mindroom.embeddings.signatures.ensure_sentence_transformers_dependencies", lambda _paths: None)
    monkeypatch.setattr(
        "mindroom.embeddings.signatures.importlib.import_module",
        lambda _name: SimpleNamespace(SentenceTransformerEmbedder=DummyEmbedder),
    )

    old_embedder = create_sentence_transformers_embedder(TEST_RUNTIME_PATHS, "sentence-transformers/old")
    old_client = old_embedder.sentence_transformer_client
    old_client_ref = weakref.ref(old_client)

    new_embedder = create_sentence_transformers_embedder(TEST_RUNTIME_PATHS, "sentence-transformers/new")

    assert new_embedder.sentence_transformer_client is not old_client
    del old_embedder, old_client
    gc.collect()
    assert old_client_ref() is None


def test_sentence_transformers_embedder_retries_failed_model_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed replacement should preserve the old client and allow retry."""
    reject_new_model = True

    class DummyEmbedder:
        def __init__(
            self,
            *,
            sentence_transformer_client: object | None = None,
            **kwargs: object,
        ) -> None:
            if kwargs["id"] == "sentence-transformers/new" and reject_new_model:
                error_message = "model load failed"
                raise RuntimeError(error_message)
            self.sentence_transformer_client = sentence_transformer_client or object()

    monkeypatch.setattr("mindroom.embeddings.signatures.ensure_sentence_transformers_dependencies", lambda _paths: None)
    monkeypatch.setattr(
        "mindroom.embeddings.signatures.importlib.import_module",
        lambda _name: SimpleNamespace(SentenceTransformerEmbedder=DummyEmbedder),
    )

    old_embedder = create_sentence_transformers_embedder(TEST_RUNTIME_PATHS, "sentence-transformers/old")

    with pytest.raises(RuntimeError, match="model load failed"):
        create_sentence_transformers_embedder(TEST_RUNTIME_PATHS, "sentence-transformers/new")

    old_embedder_after_failure = create_sentence_transformers_embedder(
        TEST_RUNTIME_PATHS,
        "sentence-transformers/old",
    )
    assert old_embedder_after_failure.sentence_transformer_client is old_embedder.sentence_transformer_client

    reject_new_model = False
    new_embedder = create_sentence_transformers_embedder(TEST_RUNTIME_PATHS, "sentence-transformers/new")
    assert new_embedder.sentence_transformer_client is not old_embedder.sentence_transformer_client


class _ConcurrencyProbe:
    """Record the highest number of callers inside the embedder at once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def observe(self) -> None:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        # Long enough that unserialized callers overlap on any machine.
        time.sleep(0.02)
        with self._lock:
            self.active -= 1


class _ProbedModelClient:
    """Stand in for a SentenceTransformer whose encode calls report concurrency."""

    def __init__(self, probe: _ConcurrencyProbe) -> None:
        self.probe = probe

    def encode_query(self, text: str, **_kwargs: object) -> np.ndarray:
        self.probe.observe()
        return np.array([float(len(text))])

    def encode_document(self, text: str, **_kwargs: object) -> np.ndarray:
        self.probe.observe()
        return np.array([float(len(text))])


def _probed_local_embedder(monkeypatch: pytest.MonkeyPatch, probe: _ConcurrencyProbe) -> object:
    class DummyEmbedder:
        def __init__(self, **kwargs: object) -> None:
            self.id = kwargs["id"]
            self.normalize_embeddings = False
            self.sentence_transformer_client = kwargs.get("sentence_transformer_client", _ProbedModelClient(probe))

    monkeypatch.setattr("mindroom.embeddings.signatures.ensure_sentence_transformers_dependencies", lambda _paths: None)
    monkeypatch.setattr(
        "mindroom.embeddings.signatures.importlib.import_module",
        lambda _name: SimpleNamespace(SentenceTransformerEmbedder=DummyEmbedder),
    )
    return create_sentence_transformers_embedder(TEST_RUNTIME_PATHS, SENTENCE_TRANSFORMERS_DEFAULT)


def test_sentence_transformers_embedder_serializes_mixed_entry_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both local embedding entry points share one process-wide serialization boundary."""
    probe = _ConcurrencyProbe()
    embedder = _probed_local_embedder(monkeypatch, probe)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(embedder.get_embedding, "one"),
            pool.submit(embedder.get_embedding_and_usage, "two"),
            pool.submit(embedder.get_embedding, "three"),
            pool.submit(embedder.get_embedding_and_usage, "four"),
            pool.submit(embedder.get_embedding, "five"),
            pool.submit(embedder.get_embedding_and_usage, "six"),
            pool.submit(embedder.get_embedding, "seven"),
            pool.submit(embedder.get_embedding_and_usage, "eight"),
        ]
        results = [future.result() for future in futures]

    assert probe.max_active == 1
    assert results == [
        [3.0],
        ([3.0], None),
        [5.0],
        ([4.0], None),
        [4.0],
        ([3.0], None),
        [5.0],
        ([5.0], None),
    ]


class _PromptRecordingModelClient:
    """Stand in for a SentenceTransformer that records which prompt family each call used."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    def encode_query(self, text: str, *, normalize_embeddings: bool) -> np.ndarray:
        self.calls.append(("query", text, normalize_embeddings))
        return np.array([1.0, 0.0])

    def encode_document(self, text: str, *, normalize_embeddings: bool) -> np.ndarray:
        self.calls.append(("document", text, normalize_embeddings))
        return np.array([0.0, 1.0])


@pytest.mark.asyncio
async def test_sentence_transformers_embedder_embeds_queries_and_documents_with_their_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agno's query path uses the model's query prompt, and its document path the document prompt."""
    model = "google/embeddinggemma-2"
    client = _PromptRecordingModelClient()
    monkeypatch.setattr("mindroom.embeddings.signatures.ensure_sentence_transformers_dependencies", lambda _paths: None)
    monkeypatch.setattr(embedding_helpers, "_SENTENCE_TRANSFORMER_CLIENT", (model, client))
    embedder = create_sentence_transformers_embedder(TEST_RUNTIME_PATHS, model)

    assert embedder.get_embedding("search") == [1.0, 0.0]
    assert await embedder.async_get_embedding("async search") == [1.0, 0.0]
    assert embedder.get_embedding_and_usage("chunk") == ([0.0, 1.0], None)
    assert await embedder.async_get_embedding_and_usage("async chunk") == ([0.0, 1.0], None)
    assert client.calls == [
        ("query", "search", False),
        ("query", "async search", False),
        ("document", "chunk", False),
        ("document", "async chunk", False),
    ]


def test_sentence_transformers_embedder_reports_model_failures_as_embedding_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local model failure surfaces as Agno's typed embedding error, like other providers."""

    class FailingModelClient:
        def encode_query(self, text: str, *, normalize_embeddings: bool) -> np.ndarray:
            del text, normalize_embeddings
            msg = "out of memory"
            raise RuntimeError(msg)

    model = "google/embeddinggemma-2"
    monkeypatch.setattr("mindroom.embeddings.signatures.ensure_sentence_transformers_dependencies", lambda _paths: None)
    monkeypatch.setattr(embedding_helpers, "_SENTENCE_TRANSFORMER_CLIENT", (model, FailingModelClient()))
    embedder = create_sentence_transformers_embedder(TEST_RUNTIME_PATHS, model)

    with pytest.raises(EmbeddingError, match="out of memory"):
        embedder.get_embedding("search")


def test_mem0_and_knowledge_signatures_use_openai_model_defaults() -> None:
    """Memory and knowledge signatures should match known OpenAI model defaults."""
    assert effective_mem0_embedder_signature("openai", OPENAI_EMBEDDING_LARGE) == (
        "openai",
        OPENAI_EMBEDDING_LARGE,
        "",
        "3072",
    )
    assert effective_knowledge_embedder_signature("openai", OPENAI_EMBEDDING_LARGE) == (
        "openai",
        OPENAI_EMBEDDING_LARGE,
        "",
        "3072",
    )


def test_local_knowledge_signature_marks_prompted_vectors_and_leaves_mem0_unchanged() -> None:
    """MindRoom's local vectors use the model's prompts; older indexes and Mem0's own client do not."""
    mem0 = effective_mem0_embedder_signature("sentence_transformers", SENTENCE_TRANSFORMERS_DEFAULT)
    knowledge = effective_knowledge_embedder_signature("sentence_transformers", SENTENCE_TRANSFORMERS_DEFAULT)

    assert mem0 == ("sentence_transformers", SENTENCE_TRANSFORMERS_DEFAULT, "", "")
    assert knowledge == ("sentence_transformers", f"{SENTENCE_TRANSFORMERS_DEFAULT}#query-document-prompts", "", "")


def test_openai_prefixes_name_new_vectors_and_leave_unprefixed_signatures_unchanged() -> None:
    """Prefixed vectors cannot be queried or reused as plain ones; configs without prefixes keep their indexes."""
    plain = effective_knowledge_embedder_signature("openai", "embeddinggemma-2", host="http://local/v1", dimensions=768)
    prefixed = effective_knowledge_embedder_signature(
        "openai",
        "embeddinggemma-2",
        host="http://local/v1",
        dimensions=768,
        query_prefix="q: ",
        document_prefix="d: ",
    )
    other_query = effective_knowledge_embedder_signature(
        "openai",
        "embeddinggemma-2",
        host="http://local/v1",
        dimensions=768,
        query_prefix="other: ",
        document_prefix="d: ",
    )

    assert plain == ("openai", "embeddinggemma-2", "http://local/v1", "768")
    assert prefixed[2:] == plain[2:]
    assert len({plain, prefixed, other_query}) == 3
    # Mem0 embeds through the same prefixed client, so its collections follow the prefixes too.
    assert (
        effective_mem0_embedder_signature(
            "openai",
            "embeddinggemma-2",
            host="http://local/v1",
            dimensions=768,
            query_prefix="q: ",
            document_prefix="d: ",
        )
        == prefixed
    )


def test_mem0_openai_signature_separates_implicit_and_explicit_dimensions() -> None:
    """Implicit OpenAI dimensions and explicit shortened dimensions should not share collections."""
    assert effective_mem0_embedder_signature("openai", OPENAI_EMBEDDING_LARGE) != effective_mem0_embedder_signature(
        "openai",
        OPENAI_EMBEDDING_LARGE,
        dimensions=1536,
    )


def test_mem0_custom_openai_compatible_signature_keeps_implicit_dimensions_unset() -> None:
    """Custom OpenAI-compatible models should not be keyed as explicit 1536-d vectors."""
    assert effective_mem0_embedder_signature(
        "openai",
        "gemini-embedding-2",
        host="http://example.com/v1",
    ) == (
        "openai",
        "gemini-embedding-2",
        "http://example.com/v1",
        "",
    )
    assert effective_mem0_embedder_signature(
        "openai",
        "gemini-embedding-2",
        host="http://example.com/v1",
        dimensions=1536,
    ) == (
        "openai",
        "gemini-embedding-2",
        "http://example.com/v1",
        "1536",
    )


def test_mem0_openai_signature_does_not_guess_unknown_model_dimensions() -> None:
    """Unknown OpenAI-compatible models should only include dimensions when configured."""
    assert effective_mem0_embedder_signature("openai", "custom-embedding-model") == (
        "openai",
        "custom-embedding-model",
        "",
        "",
    )
    assert effective_mem0_embedder_signature("openai", "custom-embedding-model", dimensions=1024) == (
        "openai",
        "custom-embedding-model",
        "",
        "1024",
    )
