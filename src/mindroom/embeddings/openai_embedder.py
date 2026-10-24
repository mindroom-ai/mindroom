"""OpenAI-compatible embedder used for semantic indexes.

Lives apart from the light helpers in ``mindroom.embeddings.signatures`` because
it imports the openai SDK; the embedding factory imports this module only when
the openai provider is configured (#1436).

Every request validates a complete non-empty response and applies MindRoom's
classified failure and health policy: a silent ``[]`` turns an auth failure
into fake-empty search results and unpublished indexes (ISSUE-237). Failures
raise ``EmbedderRequestError`` carrying only the classified detail (never the
raw provider exception, whose text can echo the rejected key), and each request
records process-wide embedder health so recovery is visible the moment a real
request succeeds again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from openai import AsyncOpenAI, OpenAI

from mindroom.embeddings.embedder_health import EmbedderHealthRecorder, capture_embedder_health_recorder
from mindroom.embeddings.errors import (
    EMBEDDER_EMPTY_VECTOR_DETAIL,
    EmbedderRequestError,
    describe_embedder_error,
    embedder_retry_after_seconds,
)
from mindroom.model_defaults import OPENAI_EMBEDDING_DIMENSIONS
from mindroom.providers.model_http_client import MODEL_HTTP_CLIENT

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from openai.types.create_embedding_response import CreateEmbeddingResponse

    from mindroom.embeddings.base import EmbeddingPurpose


#: The most texts one request carries; longer lists go in further requests.
_MAX_TEXTS_PER_REQUEST = 100


def _classified_request_error(exc: Exception, health_recorder: EmbedderHealthRecorder) -> EmbedderRequestError:
    """Record and return the classified failure for one provider exception."""
    detail = describe_embedder_error(exc)
    health_recorder.record(detail)
    # The classified error replaces the provider exception, so carry the
    # provider's own backoff hint across the boundary before it is discarded.
    return EmbedderRequestError(detail, retry_after_seconds=embedder_retry_after_seconds(exc))


def _validated_embeddings(
    response: CreateEmbeddingResponse,
    expected_count: int,
    health_recorder: EmbedderHealthRecorder,
) -> list[list[float]]:
    """Validate one non-empty vector per requested input and record health.

    OpenAI-compatible servers can return HTTP 200 with empty ``data``, empty
    vectors, or fewer items than inputs; accepting those silently recreates
    the fake-empty results this module exists to kill.
    """
    embeddings = [data.embedding for data in response.data]
    if len(embeddings) != expected_count:
        detail = f"embedder returned {len(embeddings)} embeddings for {expected_count} inputs"
        health_recorder.record(detail)
        raise EmbedderRequestError(detail)
    if any(not embedding for embedding in embeddings):
        health_recorder.record(EMBEDDER_EMPTY_VECTOR_DETAIL)
        raise EmbedderRequestError(EMBEDDER_EMPTY_VECTOR_DETAIL)
    health_recorder.record(None)
    return embeddings


@dataclass
class OpenAIEmbedder:
    """Embed through an OpenAI-compatible ``/embeddings`` endpoint without forcing OpenAI defaults onto other hosts.

    A request carrying one text sends it as a bare string, and one carrying several sends a list of at most
    ``_MAX_TEXTS_PER_REQUEST`` texts, exactly as the vectors in existing indexes were requested.
    """

    model: str
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    #: Explicitly configured width; OpenAI's own models fall back to ``OPENAI_EMBEDDING_DIMENSIONS``.
    dimensions: int | None = None
    query_prefix: str | None = None
    document_prefix: str | None = None
    #: SDK client limits; health probes shorten them so a stalled endpoint fails fast.
    timeout: float | None = None
    max_retries: int | None = None
    client: OpenAI | None = field(default=None, repr=False)
    async_client: AsyncOpenAI | None = field(default=None, repr=False)
    health_recorder: EmbedderHealthRecorder = field(default_factory=capture_embedder_health_recorder, repr=False)

    @property
    def sends_batches(self) -> bool:
        """Return true: one request carries every text of a batch."""
        return True

    def _client_kwargs(self) -> dict[str, Any]:
        settings = {
            "api_key": self.api_key,
            "base_url": self.base_url,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
        }
        return {name: value for name, value in settings.items() if value is not None}

    def _sdk_client(self) -> OpenAI:
        """Return the sync SDK client, building it on first use."""
        if self.client is None:
            self.client = OpenAI(**self._client_kwargs())
        return self.client

    def _async_sdk_client(self) -> AsyncOpenAI:
        """Return the async SDK client, building it on first use."""
        if self.async_client is None:
            # The shared model HTTP client keeps one connection pool for every async provider call.
            self.async_client = AsyncOpenAI(http_client=MODEL_HTTP_CLIENT, **self._client_kwargs())
        return self.async_client

    def _request_text(self, text: str, *, purpose: EmbeddingPurpose) -> str:
        prefix = self.query_prefix if purpose == "query" else self.document_prefix
        text = f"{prefix}{text}" if prefix else text
        # LiteLLM reserves files/... for Gemini file references; MindRoom inputs are text.
        return f" {text}" if text.startswith("files/") else text

    def _request_parameters(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> dict[str, Any]:
        """Build one text-only request with the configured prefix and dimensions."""
        inputs = [self._request_text(text, purpose=purpose) for text in texts]
        request: dict[str, Any] = {
            "input": inputs[0] if len(inputs) == 1 else inputs,
            "model": self.model,
            "encoding_format": "float",
        }
        dimensions = self.dimensions if self.dimensions is not None else OPENAI_EMBEDDING_DIMENSIONS.get(self.model)
        if dimensions is not None:
            request["dimensions"] = dimensions
        return request

    def _batches(self, texts: Sequence[str]) -> Iterator[Sequence[str]]:
        for start in range(0, len(texts), _MAX_TEXTS_PER_REQUEST):
            yield texts[start : start + _MAX_TEXTS_PER_REQUEST]

    def embed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        """Return one vector per text; raise a classified error on failure."""
        vectors: list[list[float]] = []
        for batch in self._batches(texts):
            try:
                response = self._sdk_client().embeddings.create(**self._request_parameters(batch, purpose=purpose))
            except Exception as exc:
                raise _classified_request_error(exc, self.health_recorder) from None
            vectors.extend(_validated_embeddings(response, len(batch), self.health_recorder))
        return vectors

    async def aembed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        """Return one vector per text asynchronously; raise a classified error on failure.

        A failing batch fails the whole call instead of retrying per item:
        after a batch-wide auth failure every retry repeats the same rejected
        credential and obscures the root cause.
        """
        vectors: list[list[float]] = []
        for batch in self._batches(texts):
            try:
                response = await self._async_sdk_client().embeddings.create(
                    **self._request_parameters(batch, purpose=purpose),
                )
            except Exception as exc:
                raise _classified_request_error(exc, self.health_recorder) from None
            vectors.extend(_validated_embeddings(response, len(batch), self.health_recorder))
        return vectors
