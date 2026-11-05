"""Embedder for Gemini embedding models on Vertex AI.

Imported only when the vertex_ai provider is configured, so google-genai stays out of every other import.
Like the OpenAI-compatible embedder, it raises only classified failures, never the provider exception, and records
each request's outcome in the process-wide embedder health.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from google import genai
from google.genai.errors import APIError

from mindroom.embeddings.errors import EMBEDDER_EMPTY_VECTOR_DETAIL, EmbedderRequestError, embedder_request_error
from mindroom.providers.errors import classify_provider_error, provider_failure, retry_after_seconds

if TYPE_CHECKING:
    from collections.abc import Sequence

    from google.genai.types import EmbedContentResponse

    from mindroom.embeddings.base import EmbeddingPurpose
    from mindroom.embeddings.embedder_health import EmbedderHealthRecorder
    from mindroom.llm.errors import ProviderError

_PROVIDER = "vertex_ai"
#: Attempts per text outside a caller that retries itself, as many as the OpenAI SDK's default.
_ATTEMPTS = 3
#: Wait before the first retry, doubled for each later one, unless the provider asks for its own.
_RETRY_BASE_SECONDS = 0.5
_MAX_RETRY_SECONDS = 10.0


def _request_error(error: Exception) -> EmbedderRequestError:
    """Return the classified error of one failed request; an unrecognized one is final."""
    failure: ProviderError | None = provider_failure(error, provider=_PROVIDER)
    if failure is None and isinstance(error, APIError):
        headers = getattr(error.response, "headers", None)
        failure = classify_provider_error(
            str(error),
            provider=_PROVIDER,
            status_code=error.code,
            retry_after_seconds=None if headers is None else retry_after_seconds(headers),
        )
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
    #: One attempt per text, for a health probe that must fail fast. Other callers get retries of throttling, outages,
    #: and timeouts here, not google-genai's, whose retry log line carries the response text.
    single_attempt: bool = False
    #: Seconds one request may take; google-genai sets none, and health probes shorten it so a stalled endpoint
    #: fails fast.
    timeout: float = 60.0
    client: genai.Client | None = field(default=None, repr=False)
    _client_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def sends_batches(self) -> bool:
        """Return false: each request carries one text."""
        return False

    def _sdk_client(self) -> genai.Client:
        """Return the one client, built on first use.

        A knowledge refresh embeds files on several threads at once. Each building its own client would replace the
        others, and google-genai closes a replaced client's connection while its request is still in flight.
        """
        with self._client_lock:
            if self.client is not None:
                return self.client
            params: dict[str, Any] = {
                "vertexai": True,
                "project": self.project,
                "location": self.location,
                "http_options": {"timeout": int(self.timeout * 1000), "retry_options": {"attempts": 1}},
            }
            if self.credentials is not None:
                params["credentials"] = self.credentials
            self.client = client = genai.Client(**params)
            return client

    def _request(self, text: str, purpose: EmbeddingPurpose) -> dict[str, Any]:
        prefix = self.query_prefix if purpose == "query" else self.document_prefix
        config = {"output_dimensionality": self.dimensions} if self.dimensions is not None else None
        return {"model": self.model, "contents": f"{prefix}{text}" if prefix else text, "config": config}

    def _failed(self, error: EmbedderRequestError) -> EmbedderRequestError:
        self.health_recorder.record(str(error))
        return error

    def _retry_delay(self, error: EmbedderRequestError, attempt: int) -> float | None:
        """Return the seconds to wait before another attempt, or None when ``error`` ends the request."""
        if self.single_attempt or not error.retryable or attempt >= _ATTEMPTS:
            return None
        delay = error.retry_after_seconds
        return min(_RETRY_BASE_SECONDS * 2 ** (attempt - 1) if delay is None else delay, _MAX_RETRY_SECONDS)

    def _embed_one(self, text: str, purpose: EmbeddingPurpose) -> list[float]:
        for attempt in range(1, _ATTEMPTS + 1):
            try:
                response = self._sdk_client().models.embed_content(**self._request(text, purpose))
            except Exception as exc:
                error = _request_error(exc)
                if (delay := self._retry_delay(error, attempt)) is None:
                    raise self._failed(error) from None
                time.sleep(delay)
            else:
                return self._vector(response)
        raise AssertionError  # pragma: no cover - the last attempt returns or raises

    async def _aembed_one(self, text: str, purpose: EmbeddingPurpose) -> list[float]:
        for attempt in range(1, _ATTEMPTS + 1):
            try:
                response = await self._sdk_client().aio.models.embed_content(**self._request(text, purpose))
            except Exception as exc:
                error = _request_error(exc)
                if (delay := self._retry_delay(error, attempt)) is None:
                    raise self._failed(error) from None
                await asyncio.sleep(delay)
            else:
                return self._vector(response)
        raise AssertionError  # pragma: no cover - the last attempt returns or raises

    def _vector(self, response: EmbedContentResponse) -> list[float]:
        """Return the single vector of a one-text response, recording the request healthy."""
        if not response.embeddings or not response.embeddings[0].values:
            raise self._failed(EmbedderRequestError(EMBEDDER_EMPTY_VECTOR_DETAIL))
        self.health_recorder.record(None)
        return [float(value) for value in response.embeddings[0].values]

    def embed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        """Return one vector per text; raise a classified error on failure."""
        return [self._embed_one(text, purpose) for text in texts]

    async def aembed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        """Return one vector per text asynchronously; raise a classified error on failure."""
        return [await self._aembed_one(text, purpose) for text in texts]
