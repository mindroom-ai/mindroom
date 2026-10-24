"""Tests for MindRoom embedding helpers."""

from __future__ import annotations

import asyncio
import gc
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

import mindroom.embeddings.sentence_transformers_embedder as local_embedder
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.embeddings.errors import EmbedderRequestError
from mindroom.embeddings.openai_embedder import OpenAIEmbedder
from mindroom.embeddings.sentence_transformers_embedder import create_sentence_transformers_embedder
from mindroom.embeddings.signatures import (
    effective_knowledge_embedder_signature,
    effective_mem0_embedder_signature,
)
from mindroom.model_defaults import OPENAI_EMBEDDING_LARGE, SENTENCE_TRANSFORMERS_DEFAULT

TEST_RUNTIME_PATHS = resolve_primary_runtime_paths(config_path=Path("config.yaml"))


@pytest.fixture(autouse=True)
def _isolate_sentence_transformer_client_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_embedder, "_SENTENCE_TRANSFORMER_CLIENT", None)
    monkeypatch.setattr(local_embedder, "ensure_sentence_transformers_dependencies", lambda _paths: None)


def _request_dimensions(model: str, *, base_url: str | None = None, dimensions: int | None = None) -> int | None:
    embedder = OpenAIEmbedder(model=model, api_key="sk-test", base_url=base_url, dimensions=dimensions)
    return embedder._request_parameters(["hello"], purpose="query").get("dimensions")


def test_custom_host_non_openai_model_omits_dimensions() -> None:
    """OpenAI-compatible custom models should not inherit OpenAI's 1536-d fallback."""
    assert _request_dimensions("gemini-embedding-2", base_url="http://example.com/v1") is None


def test_custom_host_official_openai_model_keeps_dimensions() -> None:
    """Known OpenAI embedding models should keep their explicit dimensionality."""
    assert _request_dimensions("text-embedding-3-small", base_url="http://example.com/v1") == 1536


def test_official_openai_ada_omits_dimensions() -> None:
    """Legacy OpenAI ada requests should not include the newer dimensions parameter."""
    assert _request_dimensions("text-embedding-ada-002") is None


def test_custom_host_explicit_dimensions_override_is_preserved() -> None:
    """Explicit dimensions should still be forwarded for custom-host models."""
    assert _request_dimensions("gemini-embedding-2", base_url="http://example.com/v1", dimensions=3072) == 3072


def test_batch_requests_use_the_single_request_dimension_rules() -> None:
    """A batch request uses the same custom-host dimension rules as a single request."""
    embedder = OpenAIEmbedder(model="gemini-embedding-2", api_key="sk-test", base_url="http://example.com/v1")

    request = embedder._request_parameters(["hello", "world"], purpose="document")

    assert request == {"input": ["hello", "world"], "model": "gemini-embedding-2", "encoding_format": "float"}


def test_create_sentence_transformers_embedder_auto_installs_optional_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local embedder creation should ensure the optional runtime and load the configured model."""
    captured: dict[str, object] = {}
    loaded = object()

    def _ensure(runtime_paths: object) -> None:
        captured["installed"] = runtime_paths

    def _load(model: str) -> object:
        captured["model"] = model
        return loaded

    monkeypatch.setattr(local_embedder, "ensure_sentence_transformers_dependencies", _ensure)
    monkeypatch.setattr(local_embedder, "_load_sentence_transformer", _load)

    embedder = create_sentence_transformers_embedder(TEST_RUNTIME_PATHS, SENTENCE_TRANSFORMERS_DEFAULT)

    assert captured == {"installed": TEST_RUNTIME_PATHS, "model": SENTENCE_TRANSFORMERS_DEFAULT}
    assert embedder.client is loaded
    assert embedder.sends_batches is False


def test_sentence_transformers_embedder_reuses_one_model_client_across_concurrent_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent index handles should share one expensive local model client."""
    construction_lock = threading.Lock()
    model_constructions = 0

    def _load(_model: str) -> object:
        nonlocal model_constructions
        with construction_lock:
            model_constructions += 1
        time.sleep(0.02)
        return object()

    monkeypatch.setattr(local_embedder, "_load_sentence_transformer", _load)

    with ThreadPoolExecutor(max_workers=8) as pool:
        embedders = list(
            pool.map(
                lambda _index: create_sentence_transformers_embedder(
                    TEST_RUNTIME_PATHS,
                    "sentence-transformers/cache-regression-test",
                ),
                range(8),
            ),
        )

    assert model_constructions == 1
    assert len({id(embedder) for embedder in embedders}) == 8
    assert len({id(embedder.client) for embedder in embedders}) == 1


def test_sentence_transformers_embedder_releases_retired_cached_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching the active model should not permanently retain the old client."""

    class ModelClient:
        pass

    monkeypatch.setattr(local_embedder, "_load_sentence_transformer", lambda _model: ModelClient())

    old_embedder = create_sentence_transformers_embedder(TEST_RUNTIME_PATHS, "sentence-transformers/old")
    old_client = old_embedder.client
    old_client_ref = weakref.ref(old_client)

    new_embedder = create_sentence_transformers_embedder(TEST_RUNTIME_PATHS, "sentence-transformers/new")

    assert new_embedder.client is not old_client
    del old_embedder, old_client
    gc.collect()
    assert old_client_ref() is None


def test_sentence_transformers_embedder_retries_failed_model_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed replacement should preserve the old client and allow retry."""
    reject_new_model = True

    def _load(model: str) -> object:
        if model == "sentence-transformers/new" and reject_new_model:
            error_message = "model load failed"
            raise RuntimeError(error_message)
        return object()

    monkeypatch.setattr(local_embedder, "_load_sentence_transformer", _load)

    old_embedder = create_sentence_transformers_embedder(TEST_RUNTIME_PATHS, "sentence-transformers/old")

    with pytest.raises(RuntimeError, match="model load failed"):
        create_sentence_transformers_embedder(TEST_RUNTIME_PATHS, "sentence-transformers/new")

    old_embedder_after_failure = create_sentence_transformers_embedder(
        TEST_RUNTIME_PATHS,
        "sentence-transformers/old",
    )
    assert old_embedder_after_failure.client is old_embedder.client

    reject_new_model = False
    new_embedder = create_sentence_transformers_embedder(TEST_RUNTIME_PATHS, "sentence-transformers/new")
    assert new_embedder.client is not old_embedder.client


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


def test_sentence_transformers_embedder_serializes_queries_and_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Query and document embedding share one process-wide serialization boundary."""
    probe = _ConcurrencyProbe()
    monkeypatch.setattr(local_embedder, "_load_sentence_transformer", lambda _model: _ProbedModelClient(probe))
    embedder = create_sentence_transformers_embedder(TEST_RUNTIME_PATHS, SENTENCE_TRANSFORMERS_DEFAULT)
    texts = ["one", "two", "three", "four", "five", "six", "seven", "eight"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(embedder.embed, [text], purpose="query" if index % 2 == 0 else "document")
            for index, text in enumerate(texts)
        ]
        results = [future.result() for future in futures]

    assert probe.max_active == 1
    assert results == [[[float(len(text))]] for text in texts]


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
    """Queries use the model's query prompt and documents its document prompt, one text per model call."""
    client = _PromptRecordingModelClient()
    monkeypatch.setattr(local_embedder, "_load_sentence_transformer", lambda _model: client)
    embedder = create_sentence_transformers_embedder(TEST_RUNTIME_PATHS, "google/embeddinggemma-2")

    assert embedder.embed(["search"], purpose="query") == [[1.0, 0.0]]
    assert await embedder.aembed(["async search"], purpose="query") == [[1.0, 0.0]]
    assert embedder.embed(["chunk", "other chunk"], purpose="document") == [[0.0, 1.0], [0.0, 1.0]]
    assert await embedder.aembed(["async chunk"], purpose="document") == [[0.0, 1.0]]
    assert client.calls == [
        ("query", "search", False),
        ("query", "async search", False),
        ("document", "chunk", False),
        ("document", "other chunk", False),
        ("document", "async chunk", False),
    ]


def test_sentence_transformers_embedder_reports_model_failures_as_classified_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local model failure surfaces as the classified embedder error, keeping the model's error as its cause."""

    class FailingModelClient:
        def encode_query(self, text: str, *, normalize_embeddings: bool) -> np.ndarray:
            del text, normalize_embeddings
            msg = "out of memory"
            raise RuntimeError(msg)

    monkeypatch.setattr(local_embedder, "_load_sentence_transformer", lambda _model: FailingModelClient())
    embedder = create_sentence_transformers_embedder(TEST_RUNTIME_PATHS, "google/embeddinggemma-2")

    with pytest.raises(EmbedderRequestError, match=r"^embedder request failed \(RuntimeError\)$") as excinfo:
        asyncio.run(embedder.aembed(["search"], purpose="query"))
    assert str(excinfo.value.__cause__) == "out of memory"


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
