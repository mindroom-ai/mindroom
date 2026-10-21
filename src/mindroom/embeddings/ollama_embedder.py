"""Embedder for a local or remote Ollama server.

Imported only when the ollama provider is configured, so the ollama SDK stays out of every other import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ollama import AsyncClient, Client

from mindroom.embeddings.errors import EMBEDDER_EMPTY_VECTOR_DETAIL, EmbedderRequestError, describe_embedder_error

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ollama import EmbedResponse

    from mindroom.embeddings.base import EmbeddingPurpose

#: The ``dimensions`` every request has asked Ollama for; it only shortens vectors wider than this.
_REQUESTED_DIMENSIONS = 4096


def _vector(response: EmbedResponse) -> list[float]:
    """Return the single vector of a one-text response."""
    vector = [float(value) for value in response.embeddings[0]] if response.embeddings else []
    if len(vector) != _REQUESTED_DIMENSIONS:
        raise EmbedderRequestError(EMBEDDER_EMPTY_VECTOR_DETAIL)
    return vector


@dataclass
class OllamaEmbedder:
    """Embed one text per ``/api/embed`` request; Ollama models embed queries and documents alike."""

    model: str
    host: str | None = None
    client: Client | None = field(default=None, repr=False)
    async_client: AsyncClient | None = field(default=None, repr=False)

    @property
    def sends_batches(self) -> bool:
        """Return false: each request carries one text."""
        return False

    def _sync_client(self) -> Client:
        if self.client is None:
            self.client = Client(host=self.host)
        return self.client

    def _async_client(self) -> AsyncClient:
        if self.async_client is None:
            self.async_client = AsyncClient(host=self.host)
        return self.async_client

    def embed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        """Return one vector per text; raise a classified error on failure."""
        del purpose
        vectors: list[list[float]] = []
        for text in texts:
            try:
                response = self._sync_client().embed(model=self.model, input=text, dimensions=_REQUESTED_DIMENSIONS)
            except Exception as exc:
                raise EmbedderRequestError(describe_embedder_error(exc)) from exc
            vectors.append(_vector(response))
        return vectors

    async def aembed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        """Return one vector per text asynchronously; raise a classified error on failure."""
        del purpose
        vectors: list[list[float]] = []
        for text in texts:
            try:
                response = await self._async_client().embed(
                    model=self.model,
                    input=text,
                    dimensions=_REQUESTED_DIMENSIONS,
                )
            except Exception as exc:
                raise EmbedderRequestError(describe_embedder_error(exc)) from exc
            vectors.append(_vector(response))
        return vectors
