"""OpenAI-compatible embedder used for semantic indexes.

Lives apart from the light helpers in ``mindroom.embeddings`` because
subclassing agno's ``OpenAIEmbedder`` imports the openai SDK; the embedding
factory imports this module only when the openai provider is configured
(#1436).

Unlike agno's base embedder, every sync/async/batch method here raises on
provider failure or a malformed success response instead of returning empty
vectors: a silent ``[]`` turns an auth failure into fake-empty search results
and unpublished indexes (ISSUE-237). Failures raise ``EmbedderRequestError``
carrying only the classified detail (never the raw provider exception, whose
text can echo the rejected key), and each path records process-wide embedder
health so recovery is visible the moment a real request succeeds again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agno.knowledge.embedder.openai import OpenAIEmbedder
from agno.utils.log import log_info

from mindroom.embedder_health import (
    EMBEDDER_EMPTY_VECTOR_DETAIL,
    EmbedderRequestError,
    describe_embedder_error,
    record_embedder_health,
)
from mindroom.model_defaults import OPENAI_EMBEDDING_DIMENSIONS

if TYPE_CHECKING:
    from openai.types.create_embedding_response import CreateEmbeddingResponse


def _classified_request_error(exc: Exception) -> EmbedderRequestError:
    """Record and return the classified failure for one provider exception."""
    detail = describe_embedder_error(exc)
    record_embedder_health(detail)
    return EmbedderRequestError(detail)


def _validated_embeddings(response: CreateEmbeddingResponse, expected_count: int) -> list[list[float]]:
    """Validate one non-empty vector per requested input and record health.

    OpenAI-compatible servers can return HTTP 200 with empty ``data``, empty
    vectors, or fewer items than inputs; accepting those silently recreates
    the fake-empty results this module exists to kill.
    """
    embeddings = [data.embedding for data in response.data]
    if len(embeddings) != expected_count:
        detail = f"embedder returned {len(embeddings)} embeddings for {expected_count} inputs"
        record_embedder_health(detail)
        raise EmbedderRequestError(detail)
    if any(not embedding for embedding in embeddings):
        record_embedder_health(EMBEDDER_EMPTY_VECTOR_DETAIL)
        raise EmbedderRequestError(EMBEDDER_EMPTY_VECTOR_DETAIL)
    record_embedder_health(None)
    return embeddings


@dataclass
class MindRoomOpenAIEmbedder(OpenAIEmbedder):
    """Avoid forcing OpenAI defaults onto arbitrary OpenAI-compatible hosts."""

    _dimensions_explicit: bool = field(init=False, default=False, repr=False)

    def __post_init__(self) -> None:
        """Track whether dimensions came from explicit config."""
        self._dimensions_explicit = self.dimensions is not None
        if self.dimensions is None:
            self.dimensions = OPENAI_EMBEDDING_DIMENSIONS.get(self.id)

    def _should_send_dimensions(self) -> bool:
        return self.dimensions is not None and (self._dimensions_explicit or self.id in OPENAI_EMBEDDING_DIMENSIONS)

    def _request_params(self, input_value: str | list[str]) -> dict[str, Any]:
        request: dict[str, Any] = {
            "input": input_value,
            "model": self.id,
            "encoding_format": self.encoding_format,
        }
        if self.user is not None:
            request["user"] = self.user
        if self._should_send_dimensions():
            request["dimensions"] = self.dimensions
        if self.request_params:
            request.update(self.request_params)
        return request

    # NOTE: These overrides intentionally mirror agno's sync/async embedder
    # methods because upstream inlines request construction instead of calling
    # a shared helper. Keep them aligned with agno when upgrading that
    # dependency, but never reintroduce its swallow-and-return-[] behavior.
    def response(self, text: str) -> CreateEmbeddingResponse:
        """Request a single embedding synchronously."""
        return self.client.embeddings.create(**self._request_params(text))

    def get_embedding(self, text: str) -> list[float]:
        """Request one embedding; raise a classified error on failure."""
        try:
            response = self.response(text)
        except Exception as exc:
            raise _classified_request_error(exc) from None
        return _validated_embeddings(response, 1)[0]

    def get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, Any] | None]:
        """Request one embedding and its usage payload; raise a classified error on failure."""
        try:
            response = self.response(text)
        except Exception as exc:
            raise _classified_request_error(exc) from None
        embedding = _validated_embeddings(response, 1)[0]
        usage = response.usage
        return embedding, usage.model_dump() if usage else None

    async def async_get_embedding(self, text: str) -> list[float]:
        """Request a single embedding asynchronously; raise a classified error on failure."""
        try:
            response: CreateEmbeddingResponse = await self.aclient.embeddings.create(**self._request_params(text))
        except Exception as exc:
            raise _classified_request_error(exc) from None
        return _validated_embeddings(response, 1)[0]

    async def async_get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, Any] | None]:
        """Request one embedding and its usage payload asynchronously; raise a classified error on failure."""
        try:
            response = await self.aclient.embeddings.create(**self._request_params(text))
        except Exception as exc:
            raise _classified_request_error(exc) from None
        embedding = _validated_embeddings(response, 1)[0]
        usage = response.usage
        return embedding, usage.model_dump() if usage else None

    async def async_get_embeddings_batch_and_usage(
        self,
        texts: list[str],
    ) -> tuple[list[list[float]], list[dict[str, Any] | None]]:
        """Request embeddings for a batch of texts; raise a classified error on failure.

        A failing batch fails the whole call instead of retrying per item:
        after a batch-wide auth failure every retry repeats the same rejected
        credential and obscures the root cause.
        """
        all_embeddings: list[list[float]] = []
        all_usage: list[dict[str, Any] | None] = []
        log_info(f"Getting embeddings and usage for {len(texts)} texts in batches of {self.batch_size} (async)")

        for i in range(0, len(texts), self.batch_size):
            batch_texts = texts[i : i + self.batch_size]
            try:
                response: CreateEmbeddingResponse = await self.aclient.embeddings.create(
                    **self._request_params(batch_texts),
                )
            except Exception as exc:
                raise _classified_request_error(exc) from None
            batch_embeddings = _validated_embeddings(response, len(batch_texts))
            all_embeddings.extend(batch_embeddings)
            usage_dict = response.usage.model_dump() if response.usage else None
            all_usage.extend([usage_dict] * len(batch_embeddings))

        return all_embeddings, all_usage
