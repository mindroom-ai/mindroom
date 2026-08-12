"""Tests for MindRoom embedding helpers."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.constants import resolve_primary_runtime_paths
from mindroom.embeddings import (
    create_sentence_transformers_embedder,
    effective_knowledge_embedder_signature,
    effective_mem0_embedder_signature,
)
from mindroom.model_defaults import OPENAI_EMBEDDING_LARGE, SENTENCE_TRANSFORMERS_DEFAULT
from mindroom.openai_embedder import MindRoomOpenAIEmbedder

TEST_RUNTIME_PATHS = resolve_primary_runtime_paths(config_path=Path("config.yaml"))


def _mock_openai_client() -> MagicMock:
    client = MagicMock()
    client.embeddings.create.return_value = MagicMock()
    return client


def test_custom_host_non_openai_model_omits_dimensions() -> None:
    """OpenAI-compatible custom models should not inherit OpenAI's 1536-d fallback."""
    client = _mock_openai_client()
    embedder = MindRoomOpenAIEmbedder(
        id="gemini-embedding-001",
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
        id="gemini-embedding-001",
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
        id="gemini-embedding-001",
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

    def _ensure(runtime_paths: object) -> None:
        captured["installed"] = runtime_paths

    monkeypatch.setattr("mindroom.embeddings.ensure_sentence_transformers_dependencies", _ensure)
    monkeypatch.setattr(
        "mindroom.embeddings.importlib.import_module",
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


def _probed_local_embedder(monkeypatch: pytest.MonkeyPatch, probe: _ConcurrencyProbe) -> object:
    class DummyEmbedder:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def get_embedding(self, text: str) -> list[float]:
            probe.observe()
            return [float(len(text))]

        def get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, int]]:
            probe.observe()
            return [float(len(text))], {"tokens": len(text)}

    monkeypatch.setattr("mindroom.embeddings.ensure_sentence_transformers_dependencies", lambda _paths: None)
    monkeypatch.setattr(
        "mindroom.embeddings.importlib.import_module",
        lambda _name: SimpleNamespace(SentenceTransformerEmbedder=DummyEmbedder),
    )
    return create_sentence_transformers_embedder(TEST_RUNTIME_PATHS, SENTENCE_TRANSFORMERS_DEFAULT)


@pytest.mark.timeout(1)
def test_sentence_transformers_usage_delegation_does_not_deadlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agno's usage method may delegate back through the serialized embedding entry point."""

    class DelegatingEmbedder:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def get_embedding(self, text: str) -> list[float]:
            return [float(len(text))]

        def get_embedding_and_usage(self, text: str) -> tuple[list[float], None]:
            return self.get_embedding(text), None

    monkeypatch.setattr("mindroom.embeddings.ensure_sentence_transformers_dependencies", lambda _paths: None)
    monkeypatch.setattr(
        "mindroom.embeddings.importlib.import_module",
        lambda _name: SimpleNamespace(SentenceTransformerEmbedder=DelegatingEmbedder),
    )
    embedder = create_sentence_transformers_embedder(TEST_RUNTIME_PATHS, SENTENCE_TRANSFORMERS_DEFAULT)

    assert embedder.get_embedding_and_usage("chunk") == ([5.0], None)


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
        ([3.0], {"tokens": 3}),
        [5.0],
        ([4.0], {"tokens": 4}),
        [4.0],
        ([3.0], {"tokens": 3}),
        [5.0],
        ([5.0], {"tokens": 5}),
    ]


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
        "gemini-embedding-001",
        host="http://example.com/v1",
    ) == (
        "openai",
        "gemini-embedding-001",
        "http://example.com/v1",
        "",
    )
    assert effective_mem0_embedder_signature(
        "openai",
        "gemini-embedding-001",
        host="http://example.com/v1",
        dimensions=1536,
    ) == (
        "openai",
        "gemini-embedding-001",
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
