"""Tests for the Ollama embedder."""

from __future__ import annotations

import asyncio
import json
from functools import partial

import httpx
import ollama
import pytest

from mindroom.embeddings.errors import (
    EMBEDDER_EMPTY_VECTOR_DETAIL,
    EMBEDDER_UNREACHABLE_DETAIL,
    EmbedderRequestError,
    embedder_failure_is_transient,
    extract_classified_embedder_detail,
)
from mindroom.embeddings.ollama_embedder import OllamaEmbedder
from mindroom.knowledge.index_retry import EmbeddingRetryPolicy, run_with_embedding_retry

_Reply = list[list[float]] | Exception | httpx.Response


def _embedder(replies: list[_Reply], requests: list[dict[str, object]]) -> OllamaEmbedder:
    """Return an embedder whose server answers each request with the next reply, repeating the last one."""

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        reply = replies[min(len(requests), len(replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json={"model": "nomic-embed-text", "embeddings": reply})

    transport = httpx.MockTransport(handle)
    return OllamaEmbedder(
        model="nomic-embed-text",
        host="http://ollama.local:11434",
        client=ollama.Client(host="http://ollama.local:11434", transport=transport),
        async_client=ollama.AsyncClient(host="http://ollama.local:11434", transport=transport),
    )


def test_ollama_embedder_returns_vectors_of_the_models_own_width() -> None:
    """A 768-dimension model's vectors are used as they come, and no request asks for another width.

    Ollama refuses a ``dimensions`` wider than the model's vector, so asking every model for one width fails all
    narrower models.
    """
    requests: list[dict[str, object]] = []
    vector = [0.25] * 768
    embedder = _embedder([[vector]], requests)

    assert embedder.embed(["first", "second"], purpose="document") == [vector, vector]
    assert asyncio.run(embedder.aembed(["query"], purpose="query")) == [vector]
    # One text per request, queries and documents alike.
    assert [request["input"] for request in requests] == ["first", "second", "query"]
    assert all("dimensions" not in request for request in requests)


def test_ollama_embedder_rejects_an_empty_reply() -> None:
    """A reply without a vector raises instead of handing an empty vector to the index."""
    embedder = _embedder([[]], [])

    with pytest.raises(EmbedderRequestError, match=EMBEDDER_EMPTY_VECTOR_DETAIL):
        embedder.embed(["text"], purpose="document")


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("connection refused"), httpx.ReadTimeout("timed out")],
    ids=["refused", "read_timeout"],
)
def test_ollama_embedder_reports_an_unreachable_server_as_retryable(failure: Exception) -> None:
    """A refused or timed-out request reads as unreachable and retryable, keeping the transport error as its cause."""
    embedder = _embedder([failure], [])

    with pytest.raises(EmbedderRequestError) as excinfo:
        embedder.embed(["text"], purpose="query")
    assert str(excinfo.value) == EMBEDDER_UNREACHABLE_DETAIL
    assert embedder_failure_is_transient(excinfo.value)
    assert excinfo.value.__cause__ is not None
    # The memory index and the knowledge notice name the persisted cause.
    assert extract_classified_embedder_detail(f"refresh failed: {excinfo.value}") == EMBEDDER_UNREACHABLE_DETAIL


@pytest.mark.parametrize(
    ("status_code", "transient"),
    [(503, True), (429, True), (404, False), (400, False)],
)
def test_ollama_embedder_reports_an_http_error_by_its_status(status_code: int, transient: bool) -> None:
    """An HTTP error names only its status, and only throttling and server errors are retried."""
    reply = httpx.Response(status_code, json={"error": 'model "nomic-embed-text" not found, try pulling it first'})
    embedder = _embedder([reply], [])

    with pytest.raises(EmbedderRequestError) as excinfo:
        asyncio.run(embedder.aembed(["text"], purpose="query"))
    assert str(excinfo.value) == f"embedder request failed (HTTP {status_code})"
    assert embedder_failure_is_transient(excinfo.value) is transient


@pytest.mark.asyncio
async def test_knowledge_embedding_retries_a_brief_ollama_outage() -> None:
    """A knowledge write whose first request finds Ollama down is retried and succeeds once Ollama answers."""
    requests: list[dict[str, object]] = []
    vector = [0.5] * 768
    embedder = _embedder([httpx.ConnectError("connection refused"), [vector]], requests)
    retries = 0

    def record_retry() -> None:
        nonlocal retries
        retries += 1

    async def no_sleep(_seconds: float) -> None:
        return

    vectors = await run_with_embedding_retry(
        partial(asyncio.to_thread, embedder.embed, ["chunk"], purpose="document"),
        policy=EmbeddingRetryPolicy(),
        sleep=no_sleep,
        on_retry=record_retry,
    )

    assert vectors == [vector]
    assert retries == 1
    assert len(requests) == 2
