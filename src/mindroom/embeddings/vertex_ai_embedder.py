"""Embedder for Gemini embedding models on Vertex AI.

Imported only when the vertex_ai provider is configured, so google-genai stays out of every other import.
Like the OpenAI-compatible embedder, it raises only classified failures, never the provider exception, and records
each request's outcome in the process-wide embedder health.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from google import genai
from google.genai.errors import APIError

from mindroom.embeddings.errors import EMBEDDER_EMPTY_VECTOR_DETAIL, EmbedderRequestError, embedder_request_error
from mindroom.providers.errors import classify_provider_error, provider_failure

if TYPE_CHECKING:
    from collections.abc import Sequence

    from google.genai.types import EmbedContentResponse

    from mindroom.embeddings.base import EmbeddingPurpose
    from mindroom.embeddings.embedder_health import EmbedderHealthRecorder
    from mindroom.llm.errors import ProviderError

_PROVIDER = "vertex_ai"


def _request_error(error: Exception) -> EmbedderRequestError:
    """Return the classified error of one failed request; an unrecognized one is final."""
    failure: ProviderError | None = provider_failure(error, provider=_PROVIDER)
    if failure is None and isinstance(error, APIError):
        failure = classify_provider_error(str(error), provider=_PROVIDER, status_code=error.code)
    return embedder_request_error(error, failure)


@dataclass
class VertexAIEmbedder:
    """Embed one text per ``embedContent`` request, which is all Vertex AI's Gemini embedding models accept.

    Without ``client``, google-genai authenticates with ``credentials``, else Application Default Credentials.
    """

    model: str
    #: The recorder this embedder's request outcomes feed, captured by the factory like the OpenAI embedder's.
    health_recorder: EmbedderHealthRecorder = field(repr=False)
    project: str | None = None
    location: str | None = None
    credentials: Any = field(default=None, repr=False)
    dimensions: int | None = None
    query_prefix: str | None = None
    document_prefix: str | None = None
    #: One attempt per request, for a caller that retries failed requests itself; others get google-genai's
    #: retries of throttling, server errors, and timeouts, which it sends only when asked.
    single_attempt: bool = False
    #: Seconds a request may take; health probes set it so a stalled endpoint fails fast.
    timeout: float | None = None
    client: genai.Client | None = field(default=None, repr=False)

    @property
    def sends_batches(self) -> bool:
        """Return false: each request carries one text."""
        return False

    def _sdk_client(self) -> genai.Client:
        if self.client is None:
            http_options: dict[str, Any] = {"retry_options": {"attempts": 1} if self.single_attempt else {}}
            if self.timeout is not None:
                http_options["timeout"] = int(self.timeout * 1000)
            params: dict[str, Any] = {
                "vertexai": True,
                "project": self.project,
                "location": self.location,
                "http_options": http_options,
            }
            if self.credentials is not None:
                params["credentials"] = self.credentials
            self.client = genai.Client(**params)
        return self.client

    def _request(self, text: str, purpose: EmbeddingPurpose) -> dict[str, Any]:
        prefix = self.query_prefix if purpose == "query" else self.document_prefix
        config = {"output_dimensionality": self.dimensions} if self.dimensions is not None else None
        return {"model": self.model, "contents": f"{prefix}{text}" if prefix else text, "config": config}

    def _failed(self, error: EmbedderRequestError) -> EmbedderRequestError:
        self.health_recorder.record(str(error))
        return error

    def _vector(self, response: EmbedContentResponse) -> list[float]:
        """Return the single vector of a one-text response, recording the request healthy."""
        if not response.embeddings or not response.embeddings[0].values:
            raise self._failed(EmbedderRequestError(EMBEDDER_EMPTY_VECTOR_DETAIL))
        self.health_recorder.record(None)
        return [float(value) for value in response.embeddings[0].values]

    def embed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        """Return one vector per text; raise a classified error on failure."""
        vectors: list[list[float]] = []
        for text in texts:
            try:
                response = self._sdk_client().models.embed_content(**self._request(text, purpose))
            except Exception as exc:
                raise self._failed(_request_error(exc)) from None
            vectors.append(self._vector(response))
        return vectors

    async def aembed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        """Return one vector per text asynchronously; raise a classified error on failure."""
        vectors: list[list[float]] = []
        for text in texts:
            try:
                response = await self._sdk_client().aio.models.embed_content(**self._request(text, purpose))
            except Exception as exc:
                raise self._failed(_request_error(exc)) from None
            vectors.append(self._vector(response))
        return vectors
