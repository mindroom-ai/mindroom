"""Tests for the raising OpenAI-compatible embedder."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from openai import AuthenticationError

from mindroom.embeddings.embedder_health import capture_embedder_health_recorder, get_embedder_failure
from mindroom.embeddings.errors import (
    EMBEDDER_EMPTY_VECTOR_DETAIL,
    EmbedderRequestError,
)
from mindroom.embeddings.openai_embedder import OpenAIEmbedder

if TYPE_CHECKING:
    from collections.abc import Iterator

SECRET = "sk-rotted-litellm-key"  # noqa: S105
EMBEDDER_AUTH_FAILED_DETAIL = "embedder authentication failed (HTTP 401)"


@pytest.fixture(autouse=True)
def _reset_embedder_health() -> Iterator[None]:
    capture_embedder_health_recorder().record(None)
    yield
    capture_embedder_health_recorder().record(None)


def _auth_error() -> AuthenticationError:
    request = httpx.Request("POST", "http://embeddings.local/v1/embeddings")
    response = httpx.Response(
        401,
        request=request,
        json={"error": {"message": f"Incorrect API key provided: {SECRET}"}},
    )
    return AuthenticationError(f"Incorrect API key provided: {SECRET}", response=response, body=None)


def _success_response(*, count: int = 1) -> SimpleNamespace:
    return SimpleNamespace(data=[SimpleNamespace(embedding=[1.0, 2.0]) for _ in range(count)])


def _sync_embedder_returning(response: SimpleNamespace) -> OpenAIEmbedder:
    client = MagicMock()
    client.embeddings.create.return_value = response
    return OpenAIEmbedder(model="gemini-embedding-2", api_key=SECRET, client=client)


def _failing_sync_embedder() -> OpenAIEmbedder:
    client = MagicMock()
    client.embeddings.create.side_effect = _auth_error()
    return OpenAIEmbedder(model="gemini-embedding-2", api_key=SECRET, client=client)


def _async_embedder(async_client: MagicMock) -> OpenAIEmbedder:
    return OpenAIEmbedder(model="gemini-embedding-2", api_key=SECRET, async_client=async_client)


def _failing_async_embedder() -> OpenAIEmbedder:
    async_client = MagicMock()
    async_client.embeddings.create = AsyncMock(side_effect=_auth_error())
    return _async_embedder(async_client)


@pytest.mark.parametrize("purpose", ["query", "document"])
def test_embed_raises_and_records_failure(purpose: str) -> None:
    """A sync request raises the classified error and records the auth failure."""
    embedder = _failing_sync_embedder()

    with pytest.raises(EmbedderRequestError) as excinfo:
        embedder.embed(["hello"], purpose=purpose)

    assert str(excinfo.value) == EMBEDDER_AUTH_FAILED_DETAIL
    assert get_embedder_failure() == EMBEDDER_AUTH_FAILED_DETAIL


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["query", "document"])
async def test_aembed_raises_instead_of_empty_list(purpose: str) -> None:
    """An async request raises instead of returning []."""
    embedder = _failing_async_embedder()

    with pytest.raises(EmbedderRequestError):
        await embedder.aembed(["hello"], purpose=purpose)

    assert get_embedder_failure() == EMBEDDER_AUTH_FAILED_DETAIL


@pytest.mark.asyncio
async def test_async_batch_raises_without_per_item_retry() -> None:
    """A failing batch raises once without per-item retries."""
    async_client = MagicMock()
    async_client.embeddings.create = AsyncMock(side_effect=_auth_error())
    embedder = _async_embedder(async_client)

    with pytest.raises(EmbedderRequestError):
        await embedder.aembed(["hello", "world"], purpose="document")

    # One batch request only: no per-item retries against the same rejected key.
    assert async_client.embeddings.create.await_count == 1
    assert get_embedder_failure() == EMBEDDER_AUTH_FAILED_DETAIL


def test_raised_error_never_carries_the_key() -> None:
    """Neither the raised error, its cause chain, nor recorded health carries the key."""
    embedder = _failing_sync_embedder()

    with pytest.raises(EmbedderRequestError) as excinfo:
        embedder.embed(["hello"], purpose="query")

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
    embedder = _sync_embedder_returning(SimpleNamespace(data=[]))

    with pytest.raises(EmbedderRequestError) as excinfo:
        embedder.embed(["hello"], purpose="query")

    assert str(excinfo.value) == "embedder returned 0 embeddings for 1 inputs"
    assert get_embedder_failure() == "embedder returned 0 embeddings for 1 inputs"


@pytest.mark.parametrize("purpose", ["query", "document"])
def test_empty_vector_raises_and_records_failure(purpose: str) -> None:
    """An HTTP 200 with an empty vector raises instead of returning []."""
    embedder = _sync_embedder_returning(SimpleNamespace(data=[SimpleNamespace(embedding=[])]))

    with pytest.raises(EmbedderRequestError) as excinfo:
        embedder.embed(["hello"], purpose=purpose)

    assert str(excinfo.value) == EMBEDDER_EMPTY_VECTOR_DETAIL
    assert get_embedder_failure() == EMBEDDER_EMPTY_VECTOR_DETAIL


@pytest.mark.asyncio
async def test_async_empty_data_raises() -> None:
    """The async path rejects an empty data array."""
    async_client = MagicMock()
    async_client.embeddings.create = AsyncMock(return_value=SimpleNamespace(data=[]))
    embedder = _async_embedder(async_client)

    with pytest.raises(EmbedderRequestError):
        await embedder.aembed(["hello"], purpose="query")

    assert get_embedder_failure() == "embedder returned 0 embeddings for 1 inputs"


@pytest.mark.asyncio
async def test_async_batch_short_response_raises() -> None:
    """A batch response with fewer vectors than inputs raises loudly."""
    async_client = MagicMock()
    async_client.embeddings.create = AsyncMock(return_value=SimpleNamespace(data=[SimpleNamespace(embedding=[1.0])]))
    embedder = _async_embedder(async_client)

    with pytest.raises(EmbedderRequestError) as excinfo:
        await embedder.aembed(["hello", "world"], purpose="document")

    assert str(excinfo.value) == "embedder returned 1 embeddings for 2 inputs"
    assert get_embedder_failure() == "embedder returned 1 embeddings for 2 inputs"


@pytest.mark.asyncio
async def test_async_batch_empty_vector_raises() -> None:
    """A batch response containing an empty vector raises loudly."""
    async_client = MagicMock()
    async_client.embeddings.create = AsyncMock(
        return_value=SimpleNamespace(data=[SimpleNamespace(embedding=[1.0]), SimpleNamespace(embedding=[])]),
    )
    embedder = _async_embedder(async_client)

    with pytest.raises(EmbedderRequestError):
        await embedder.aembed(["hello", "world"], purpose="document")

    assert get_embedder_failure() == EMBEDDER_EMPTY_VECTOR_DETAIL


def test_successful_embedding_clears_recorded_failure() -> None:
    """A validated response clears an earlier recorded failure."""
    capture_embedder_health_recorder().record(EMBEDDER_AUTH_FAILED_DETAIL)
    embedder = _sync_embedder_returning(_success_response())

    assert embedder.embed(["hello"], purpose="query") == [[1.0, 2.0]]
    assert get_embedder_failure() is None


def test_file_reference_like_inputs_are_sent_as_literal_text() -> None:
    """Text beginning with LiteLLM's files/ sentinel cannot become a file reference."""
    text = "files/bootsel.service#L9> If the embedded daemons do not start"
    client = MagicMock()
    client.embeddings.create.side_effect = [_success_response(), _success_response(count=2)]
    embedder = OpenAIEmbedder(model="gemini-embedding-2", api_key=SECRET, client=client)

    assert embedder.embed([text], purpose="query") == [[1.0, 2.0]]
    assert embedder.embed(["ordinary text", text], purpose="document") == [[1.0, 2.0], [1.0, 2.0]]

    first_request, second_request = client.embeddings.create.call_args_list
    assert first_request.kwargs["input"] == f" {text}"
    assert second_request.kwargs["input"] == ["ordinary text", f" {text}"]


def test_texts_beyond_the_batch_size_go_in_further_requests() -> None:
    """A long list is split into requests of at most ``batch_size`` texts, keeping input order."""
    client = MagicMock()
    client.embeddings.create.side_effect = [_success_response(count=2), _success_response(count=2), _success_response()]
    embedder = OpenAIEmbedder(model="gemini-embedding-2", api_key=SECRET, client=client, batch_size=2)

    assert len(embedder.embed(["a", "b", "c", "d", "e"], purpose="document")) == 5

    inputs = [call.kwargs["input"] for call in client.embeddings.create.call_args_list]
    assert inputs == [["a", "b"], ["c", "d"], "e"]


@pytest.mark.asyncio
async def test_async_success_clears_recorded_failure() -> None:
    """An async success clears an earlier recorded failure."""
    capture_embedder_health_recorder().record(EMBEDDER_AUTH_FAILED_DETAIL)
    async_client = MagicMock()
    async_client.embeddings.create = AsyncMock(return_value=_success_response())
    embedder = _async_embedder(async_client)

    assert await embedder.aembed(["hello"], purpose="query") == [[1.0, 2.0]]
    assert get_embedder_failure() is None


QUERY_PREFIX = "task: search result | query: "
DOCUMENT_PREFIX = "title: none | text: "


def _prefixed_embedder(
    client: MagicMock | None = None,
    async_client: MagicMock | None = None,
) -> OpenAIEmbedder:
    return OpenAIEmbedder(
        model="embeddinggemma-2",
        api_key=SECRET,
        base_url="http://llama.local/v1",
        client=client,
        async_client=async_client,
        query_prefix=QUERY_PREFIX,
        document_prefix=DOCUMENT_PREFIX,
    )


def test_prefixes_mark_sync_queries_and_documents() -> None:
    """The purpose picks the prefix for single texts and batches alike."""
    client = MagicMock()
    client.embeddings.create.side_effect = [_success_response(), _success_response(), _success_response(count=2)]
    embedder = _prefixed_embedder(client=client)

    embedder.embed(["who is harry"], purpose="query")
    embedder.embed(["files/notes.md"], purpose="document")
    embedder.embed(["one", "two"], purpose="document")

    query, document, batch = (call.kwargs["input"] for call in client.embeddings.create.call_args_list)
    assert query == f"{QUERY_PREFIX}who is harry"
    assert document == f"{DOCUMENT_PREFIX}files/notes.md"
    assert batch == [f"{DOCUMENT_PREFIX}one", f"{DOCUMENT_PREFIX}two"]


@pytest.mark.asyncio
async def test_prefixes_mark_async_queries_and_documents() -> None:
    """The async path follows the same rule, and a batch can be embedded as queries."""
    async_client = MagicMock()
    async_client.embeddings.create = AsyncMock(
        side_effect=[_success_response(), _success_response(), _success_response(count=2), _success_response(count=2)],
    )
    embedder = _prefixed_embedder(async_client=async_client)

    await embedder.aembed(["q"], purpose="query")
    await embedder.aembed(["d"], purpose="document")
    await embedder.aembed(["d1", "d2"], purpose="document")
    await embedder.aembed(["q1", "q2"], purpose="query")

    inputs = [call.kwargs["input"] for call in async_client.embeddings.create.await_args_list]
    assert inputs == [
        f"{QUERY_PREFIX}q",
        f"{DOCUMENT_PREFIX}d",
        [f"{DOCUMENT_PREFIX}d1", f"{DOCUMENT_PREFIX}d2"],
        [f"{QUERY_PREFIX}q1", f"{QUERY_PREFIX}q2"],
    ]
