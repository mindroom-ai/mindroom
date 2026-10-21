"""Tests for the Ollama embedder."""

from __future__ import annotations

import asyncio
import json

import httpx
import ollama
import pytest

from mindroom.embeddings.errors import EMBEDDER_EMPTY_VECTOR_DETAIL, EmbedderRequestError
from mindroom.embeddings.ollama_embedder import OllamaEmbedder


def _embedder(reply: list[list[float]] | Exception, requests: list[dict[str, object]]) -> OllamaEmbedder:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(200, json={"model": "nomic-embed-text", "embeddings": reply})

    transport = httpx.MockTransport(handle)
    return OllamaEmbedder(
        model="nomic-embed-text",
        host="http://ollama.local:11434",
        client=ollama.Client(host="http://ollama.local:11434", transport=transport),
        async_client=ollama.AsyncClient(host="http://ollama.local:11434", transport=transport),
    )


def test_ollama_embedder_returns_vectors_of_the_models_own_width() -> None:
    """A 768-dimension model's vectors are used as they come, not discarded for missing a 4096-dimension default."""
    requests: list[dict[str, object]] = []
    vector = [0.25] * 768
    embedder = _embedder([vector], requests)

    assert embedder.embed(["first", "second"], purpose="document") == [vector, vector]
    assert asyncio.run(embedder.aembed(["query"], purpose="query")) == [vector]
    # One text per request, queries and documents alike.
    assert [request["input"] for request in requests] == ["first", "second", "query"]


def test_ollama_embedder_rejects_an_empty_reply() -> None:
    """A reply without a vector raises instead of handing an empty vector to the index."""
    embedder = _embedder([], [])

    with pytest.raises(EmbedderRequestError, match=EMBEDDER_EMPTY_VECTOR_DETAIL):
        embedder.embed(["text"], purpose="document")


def test_ollama_embedder_classifies_transport_failures() -> None:
    """An unreachable server raises the classified embedder error, keeping the transport error as its cause."""
    embedder = _embedder(httpx.ConnectError("connection refused"), [])

    with pytest.raises(EmbedderRequestError, match=r"^embedder request failed \(ConnectionError\)$") as excinfo:
        embedder.embed(["text"], purpose="query")
    assert excinfo.value.__cause__ is not None
