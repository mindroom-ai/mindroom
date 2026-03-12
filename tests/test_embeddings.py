"""Tests for MindRoom embedding helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from mindroom.embeddings import MindRoomOpenAIEmbedder, create_sentence_transformers_embedder

if TYPE_CHECKING:
    import pytest


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

    def _ensure() -> None:
        captured["installed"] = True

    monkeypatch.setattr("mindroom.embeddings.ensure_sentence_transformers_dependencies", _ensure)
    monkeypatch.setattr(
        "mindroom.embeddings.importlib.import_module",
        lambda name: SimpleNamespace(SentenceTransformerEmbedder=DummyEmbedder) if name else None,
    )

    embedder = create_sentence_transformers_embedder(
        "sentence-transformers/all-MiniLM-L6-v2",
        dimensions=384,
    )

    assert captured["installed"] is True
    assert isinstance(embedder, DummyEmbedder)
    assert embedder.kwargs == {
        "id": "sentence-transformers/all-MiniLM-L6-v2",
        "dimensions": 384,
    }
