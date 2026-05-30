"""Tests for MindRoom embedding helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.constants import resolve_primary_runtime_paths
from mindroom.embeddings import (
    MindRoomOpenAIEmbedder,
    create_sentence_transformers_embedder,
    effective_knowledge_embedder_signature,
    effective_mem0_embedder_signature,
)
from mindroom.model_defaults import OPENAI_EMBEDDING_LARGE, SENTENCE_TRANSFORMERS_DEFAULT

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
