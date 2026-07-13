"""Tests for the raising MindRoomOpenAIEmbedder request paths."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from openai import AuthenticationError

from mindroom import embedder_health
from mindroom.embedder_health import get_embedder_failure, record_embedder_health
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
    return AuthenticationError("Error code: 401", response=response, body=None)


def _success_response() -> SimpleNamespace:
    return SimpleNamespace(
        data=[SimpleNamespace(embedding=[1.0, 2.0])],
        usage=SimpleNamespace(model_dump=lambda: {"total_tokens": 1}),
    )


def _failing_sync_embedder() -> MindRoomOpenAIEmbedder:
    client = MagicMock()
    client.embeddings.create.side_effect = _auth_error()
    return MindRoomOpenAIEmbedder(id="gemini-embedding-001", api_key=SECRET, openai_client=client)


def _failing_async_embedder() -> MindRoomOpenAIEmbedder:
    async_client = MagicMock()
    async_client.embeddings.create = AsyncMock(side_effect=_auth_error())
    return MindRoomOpenAIEmbedder(id="gemini-embedding-001", api_key=SECRET, async_client=async_client)


def test_get_embedding_raises_and_records_failure() -> None:
    """Sync get_embedding raises and records the auth failure."""
    embedder = _failing_sync_embedder()

    with pytest.raises(AuthenticationError):
        embedder.get_embedding("hello")

    assert get_embedder_failure() == embedder_health._EMBEDDER_AUTH_FAILED_DETAIL


def test_get_embedding_and_usage_raises_instead_of_empty_tuple() -> None:
    """Sync usage variant raises instead of returning ([], None)."""
    embedder = _failing_sync_embedder()

    with pytest.raises(AuthenticationError):
        embedder.get_embedding_and_usage("hello")

    assert get_embedder_failure() == embedder_health._EMBEDDER_AUTH_FAILED_DETAIL


@pytest.mark.asyncio
async def test_async_get_embedding_raises_instead_of_empty_list() -> None:
    """Async get_embedding raises instead of returning []."""
    embedder = _failing_async_embedder()

    with pytest.raises(AuthenticationError):
        await embedder.async_get_embedding("hello")

    assert get_embedder_failure() == embedder_health._EMBEDDER_AUTH_FAILED_DETAIL


@pytest.mark.asyncio
async def test_async_get_embedding_and_usage_raises_instead_of_empty_tuple() -> None:
    """Async usage variant raises instead of returning ([], None)."""
    embedder = _failing_async_embedder()

    with pytest.raises(AuthenticationError):
        await embedder.async_get_embedding_and_usage("hello")

    assert get_embedder_failure() == embedder_health._EMBEDDER_AUTH_FAILED_DETAIL


@pytest.mark.asyncio
async def test_async_batch_raises_without_per_item_retry() -> None:
    """A failing batch raises once without per-item retries."""
    async_client = MagicMock()
    async_client.embeddings.create = AsyncMock(side_effect=_auth_error())
    embedder = MindRoomOpenAIEmbedder(id="gemini-embedding-001", api_key=SECRET, async_client=async_client)

    with pytest.raises(AuthenticationError):
        await embedder.async_get_embeddings_batch_and_usage(["hello", "world"])

    # One batch request only: no per-item retries against the same rejected key.
    assert async_client.embeddings.create.await_count == 1
    assert get_embedder_failure() == embedder_health._EMBEDDER_AUTH_FAILED_DETAIL


def test_recorded_failure_never_contains_the_key() -> None:
    """The recorded health detail never contains the API key."""
    embedder = _failing_sync_embedder()

    with pytest.raises(AuthenticationError):
        embedder.get_embedding("hello")

    failure = get_embedder_failure()
    assert failure is not None
    assert SECRET not in failure


def test_successful_embedding_clears_recorded_failure() -> None:
    """A non-empty vector clears an earlier recorded failure."""
    record_embedder_health(embedder_health._EMBEDDER_AUTH_FAILED_DETAIL)
    client = MagicMock()
    client.embeddings.create.return_value = _success_response()
    embedder = MindRoomOpenAIEmbedder(id="gemini-embedding-001", api_key=SECRET, openai_client=client)

    assert embedder.get_embedding("hello") == [1.0, 2.0]
    assert get_embedder_failure() is None


def test_get_embedding_and_usage_success_returns_vector_and_usage() -> None:
    """Success paths keep returning the vector and usage payload."""
    client = MagicMock()
    client.embeddings.create.return_value = _success_response()
    embedder = MindRoomOpenAIEmbedder(id="gemini-embedding-001", api_key=SECRET, openai_client=client)

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
