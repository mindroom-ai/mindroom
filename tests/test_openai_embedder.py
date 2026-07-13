"""Tests for the raising MindRoomOpenAIEmbedder request paths."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from openai import AuthenticationError

from mindroom import embedder_health
from mindroom.embedder_health import (
    EMBEDDER_EMPTY_VECTOR_DETAIL,
    EmbedderRequestError,
    get_embedder_failure,
    record_embedder_health,
)
from mindroom.openai_embedder import MindRoomOpenAIEmbedder

if TYPE_CHECKING:
    from collections.abc import Iterator

SECRET = "sk-rotted-litellm-key"  # noqa: S105


@pytest.fixture(autouse=True)
def _reset_embedder_health() -> Iterator[None]:
    record_embedder_health(None)
    yield
    record_embedder_health(None)


def _auth_error() -> AuthenticationError:
    request = httpx.Request("POST", "http://embeddings.local/v1/embeddings")
    response = httpx.Response(
        401,
        request=request,
        json={"error": {"message": f"Incorrect API key provided: {SECRET}"}},
    )
    return AuthenticationError(f"Incorrect API key provided: {SECRET}", response=response, body=None)


def _success_response() -> SimpleNamespace:
    return SimpleNamespace(
        data=[SimpleNamespace(embedding=[1.0, 2.0])],
        usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 1}),
    )


def _sync_embedder_returning(response: SimpleNamespace) -> MindRoomOpenAIEmbedder:
    client = MagicMock()
    client.embeddings.create.return_value = response
    return MindRoomOpenAIEmbedder(id="gemini-embedding-001", api_key=SECRET, openai_client=client)


def _failing_sync_embedder() -> MindRoomOpenAIEmbedder:
    client = MagicMock()
    client.embeddings.create.side_effect = _auth_error()
    return MindRoomOpenAIEmbedder(id="gemini-embedding-001", api_key=SECRET, openai_client=client)


def _failing_async_embedder() -> MindRoomOpenAIEmbedder:
    async_client = MagicMock()
    async_client.embeddings.create = AsyncMock(side_effect=_auth_error())
    return MindRoomOpenAIEmbedder(id="gemini-embedding-001", api_key=SECRET, async_client=async_client)


def test_get_embedding_raises_and_records_failure() -> None:
    """Sync get_embedding raises the classified error and records the auth failure."""
    embedder = _failing_sync_embedder()

    with pytest.raises(EmbedderRequestError) as excinfo:
        embedder.get_embedding("hello")

    assert str(excinfo.value) == embedder_health._EMBEDDER_AUTH_FAILED_DETAIL
    assert get_embedder_failure() == embedder_health._EMBEDDER_AUTH_FAILED_DETAIL


def test_get_embedding_and_usage_raises_instead_of_empty_tuple() -> None:
    """Sync usage variant raises instead of returning ([], None)."""
    embedder = _failing_sync_embedder()

    with pytest.raises(EmbedderRequestError):
        embedder.get_embedding_and_usage("hello")

    assert get_embedder_failure() == embedder_health._EMBEDDER_AUTH_FAILED_DETAIL


@pytest.mark.asyncio
async def test_async_get_embedding_raises_instead_of_empty_list() -> None:
    """Async get_embedding raises instead of returning []."""
    embedder = _failing_async_embedder()

    with pytest.raises(EmbedderRequestError):
        await embedder.async_get_embedding("hello")

    assert get_embedder_failure() == embedder_health._EMBEDDER_AUTH_FAILED_DETAIL


@pytest.mark.asyncio
async def test_async_get_embedding_and_usage_raises_instead_of_empty_tuple() -> None:
    """Async usage variant raises instead of returning ([], None)."""
    embedder = _failing_async_embedder()

    with pytest.raises(EmbedderRequestError):
        await embedder.async_get_embedding_and_usage("hello")

    assert get_embedder_failure() == embedder_health._EMBEDDER_AUTH_FAILED_DETAIL


@pytest.mark.asyncio
async def test_async_batch_raises_without_per_item_retry() -> None:
    """A failing batch raises once without per-item retries."""
    async_client = MagicMock()
    async_client.embeddings.create = AsyncMock(side_effect=_auth_error())
    embedder = MindRoomOpenAIEmbedder(id="gemini-embedding-001", api_key=SECRET, async_client=async_client)

    with pytest.raises(EmbedderRequestError):
        await embedder.async_get_embeddings_batch_and_usage(["hello", "world"])

    # One batch request only: no per-item retries against the same rejected key.
    assert async_client.embeddings.create.await_count == 1
    assert get_embedder_failure() == embedder_health._EMBEDDER_AUTH_FAILED_DETAIL


def test_raised_error_never_carries_the_key() -> None:
    """Neither the raised error, its cause chain, nor recorded health carries the key."""
    embedder = _failing_sync_embedder()

    with pytest.raises(EmbedderRequestError) as excinfo:
        embedder.get_embedding("hello")

    assert SECRET not in str(excinfo.value)
    # The raw provider exception (whose body echoes the key) must not chain
    # into rendered tracebacks.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    failure = get_embedder_failure()
    assert failure is not None
    assert SECRET not in failure


def test_empty_data_raises_and_records_cardinality_failure() -> None:
    """An HTTP 200 with empty data raises instead of an unclassified IndexError."""
    response = SimpleNamespace(data=[], usage=None)
    embedder = _sync_embedder_returning(response)

    with pytest.raises(EmbedderRequestError) as excinfo:
        embedder.get_embedding("hello")

    assert str(excinfo.value) == "embedder returned 0 embeddings for 1 inputs"
    assert get_embedder_failure() == "embedder returned 0 embeddings for 1 inputs"


def test_empty_vector_raises_and_records_failure() -> None:
    """An HTTP 200 with an empty vector raises instead of returning []."""
    response = SimpleNamespace(data=[SimpleNamespace(embedding=[])], usage=None)
    embedder = _sync_embedder_returning(response)

    with pytest.raises(EmbedderRequestError) as excinfo:
        embedder.get_embedding("hello")

    assert str(excinfo.value) == EMBEDDER_EMPTY_VECTOR_DETAIL
    assert get_embedder_failure() == EMBEDDER_EMPTY_VECTOR_DETAIL


def test_empty_vector_in_usage_variant_raises() -> None:
    """The sync usage variant validates the vector before returning."""
    response = SimpleNamespace(
        data=[SimpleNamespace(embedding=[])],
        usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 1}),
    )
    embedder = _sync_embedder_returning(response)

    with pytest.raises(EmbedderRequestError):
        embedder.get_embedding_and_usage("hello")


@pytest.mark.asyncio
async def test_async_empty_data_raises() -> None:
    """Async single-embedding path rejects an empty data array."""
    async_client = MagicMock()
    async_client.embeddings.create = AsyncMock(return_value=SimpleNamespace(data=[], usage=None))
    embedder = MindRoomOpenAIEmbedder(id="gemini-embedding-001", api_key=SECRET, async_client=async_client)

    with pytest.raises(EmbedderRequestError):
        await embedder.async_get_embedding("hello")

    assert get_embedder_failure() == "embedder returned 0 embeddings for 1 inputs"


@pytest.mark.asyncio
async def test_async_batch_short_response_raises() -> None:
    """A batch response with fewer vectors than inputs raises loudly."""
    async_client = MagicMock()
    async_client.embeddings.create = AsyncMock(
        return_value=SimpleNamespace(data=[SimpleNamespace(embedding=[1.0])], usage=None),
    )
    embedder = MindRoomOpenAIEmbedder(id="gemini-embedding-001", api_key=SECRET, async_client=async_client)

    with pytest.raises(EmbedderRequestError) as excinfo:
        await embedder.async_get_embeddings_batch_and_usage(["hello", "world"])

    assert str(excinfo.value) == "embedder returned 1 embeddings for 2 inputs"
    assert get_embedder_failure() == "embedder returned 1 embeddings for 2 inputs"


@pytest.mark.asyncio
async def test_async_batch_empty_vector_raises() -> None:
    """A batch response containing an empty vector raises loudly."""
    async_client = MagicMock()
    async_client.embeddings.create = AsyncMock(
        return_value=SimpleNamespace(
            data=[SimpleNamespace(embedding=[1.0]), SimpleNamespace(embedding=[])],
            usage=None,
        ),
    )
    embedder = MindRoomOpenAIEmbedder(id="gemini-embedding-001", api_key=SECRET, async_client=async_client)

    with pytest.raises(EmbedderRequestError):
        await embedder.async_get_embeddings_batch_and_usage(["hello", "world"])

    assert get_embedder_failure() == EMBEDDER_EMPTY_VECTOR_DETAIL


def test_successful_embedding_clears_recorded_failure() -> None:
    """A validated response clears an earlier recorded failure."""
    record_embedder_health(embedder_health._EMBEDDER_AUTH_FAILED_DETAIL)
    embedder = _sync_embedder_returning(_success_response())

    assert embedder.get_embedding("hello") == [1.0, 2.0]
    assert get_embedder_failure() is None


def test_get_embedding_and_usage_success_returns_vector_and_usage() -> None:
    """Success paths keep returning the vector and usage payload."""
    embedder = _sync_embedder_returning(_success_response())

    embedding, usage = embedder.get_embedding_and_usage("hello")

    assert embedding == [1.0, 2.0]
    assert usage == {"total_tokens": 1}


@pytest.mark.asyncio
async def test_async_success_clears_recorded_failure() -> None:
    """An async success clears an earlier recorded failure."""
    record_embedder_health(embedder_health._EMBEDDER_AUTH_FAILED_DETAIL)
    async_client = MagicMock()
    async_client.embeddings.create = AsyncMock(return_value=_success_response())
    embedder = MindRoomOpenAIEmbedder(id="gemini-embedding-001", api_key=SECRET, async_client=async_client)

    assert await embedder.async_get_embedding("hello") == [1.0, 2.0]
    assert get_embedder_failure() is None
