"""Tests for MindRoom embedding helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from mindroom.constants import resolve_primary_runtime_paths
from mindroom.embeddings import (
    MindRoomOpenAIEmbedder,
    create_sentence_transformers_embedder,
    effective_knowledge_embedder_signature,
    effective_mem0_embedder_signature,
)

if TYPE_CHECKING:
    import pytest


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
        "sentence-transformers/all-MiniLM-L6-v2",
        dimensions=384,
    )

    assert captured["installed"] == TEST_RUNTIME_PATHS
    assert isinstance(embedder, DummyEmbedder)
    assert embedder.kwargs == {
        "id": "sentence-transformers/all-MiniLM-L6-v2",
        "dimensions": 384,
    }


def test_mem0_and_knowledge_signatures_keep_their_own_openai_defaults() -> None:
    """Memory and knowledge signatures should not conflate different OpenAI defaults."""
    assert effective_mem0_embedder_signature("openai", "text-embedding-3-large") == (
        "openai",
        "text-embedding-3-large",
        "",
        "1536",
    )
    assert effective_knowledge_embedder_signature("openai", "text-embedding-3-large") == (
        "openai",
        "text-embedding-3-large",
        "",
        "3072",
    )
