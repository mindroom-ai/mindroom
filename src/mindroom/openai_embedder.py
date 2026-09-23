"""OpenAI-compatible embedder used for semantic indexes.

Lives apart from the light helpers in ``mindroom.embeddings`` because
subclassing agno's ``OpenAIEmbedder`` imports the openai SDK; the embedding
factory imports this module only when the openai provider is configured
(#1436).

Every sync/async/batch method validates complete non-empty responses and applies
MindRoom's classified failure and health policy: a silent ``[]`` turns an auth
failure into fake-empty search results and unpublished indexes (ISSUE-237). Failures raise ``EmbedderRequestError``
carrying only the classified detail (never the raw provider exception, whose
text can echo the rejected key), and each path records process-wide embedder
health so recovery is visible the moment a real request succeeds again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.agno_compat_openai_embedder import OpenAIEmbedderWithHooks
from mindroom.embedder_health import EmbedderHealthRecorder, capture_embedder_health_recorder
from mindroom.embedding_errors import (
    EMBEDDER_EMPTY_VECTOR_DETAIL,
    EmbedderRequestError,
    describe_embedder_error,
    embedder_retry_after_seconds,
)
from mindroom.model_defaults import OPENAI_EMBEDDING_DIMENSIONS

if TYPE_CHECKING:
    from openai.types.create_embedding_response import CreateEmbeddingResponse


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
class MindRoomOpenAIEmbedder(OpenAIEmbedderWithHooks):
    """Avoid forcing OpenAI defaults onto arbitrary OpenAI-compatible hosts."""

    _dimensions_explicit: bool = field(init=False, default=False, repr=False)
    health_recorder: EmbedderHealthRecorder = field(default_factory=capture_embedder_health_recorder, repr=False)

    def __post_init__(self) -> None:
        """Track whether dimensions came from explicit config."""
        self._dimensions_explicit = self.dimensions is not None
        if self.dimensions is None:
            self.dimensions = OPENAI_EMBEDDING_DIMENSIONS.get(self.id)

    def _should_send_dimensions(self) -> bool:
        return self.dimensions is not None and (self._dimensions_explicit or self.id in OPENAI_EMBEDDING_DIMENSIONS)

    def embedding_request_parameters(self, input_value: str | list[str]) -> dict[str, Any]:
        """Build a text-only request with the configured dimensions and overrides."""
        # LiteLLM reserves files/... for Gemini file references; MindRoom inputs are text.
        if isinstance(input_value, str):
            request_input = f" {input_value}" if input_value.startswith("files/") else input_value
        else:
            request_input = [f" {text}" if text.startswith("files/") else text for text in input_value]
        request: dict[str, Any] = {
            "input": request_input,
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

    def embedding_request_error(self, error: Exception) -> EmbedderRequestError:
        """Record a safe classified provider failure under this owner's health state."""
        return _classified_request_error(error, self.health_recorder)

    def validate_embedding_response(
        self,
        response: CreateEmbeddingResponse,
        expected_count: int,
    ) -> list[list[float]]:
        """Require exactly one non-empty vector per input and record health recovery."""
        return _validated_embeddings(response, expected_count, self.health_recorder)

    def get_embeddings_batch(self, texts: list[str]) -> list[list[float]]:
        """Request a synchronous batch for adapters that support batch embedding."""
        try:
            response = self.client.embeddings.create(**self.embedding_request_parameters(texts))
        except Exception as exc:
            raise self.embedding_request_error(exc) from None
        return self.validate_embedding_response(response, len(texts))
