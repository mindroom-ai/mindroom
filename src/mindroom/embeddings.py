"""Embedding helpers for OpenAI-compatible providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agno.knowledge.embedder.openai import OpenAIEmbedder
from agno.utils.log import log_info, log_warning
from openai.types.create_embedding_response import CreateEmbeddingResponse

_OPENAI_EMBEDDING_DIMENSIONS = {
    "text-embedding-3-large": 3072,
    "text-embedding-3-small": 1536,
}


def _default_dimensions(model: str) -> int | None:
    """Return the default dimensions for models that support the parameter."""
    return _OPENAI_EMBEDDING_DIMENSIONS.get(model)


@dataclass
class MindRoomOpenAIEmbedder(OpenAIEmbedder):
    """Avoid forcing OpenAI defaults onto arbitrary OpenAI-compatible hosts."""

    _dimensions_explicit: bool = field(init=False, default=False, repr=False)

    def __post_init__(self) -> None:
        self._dimensions_explicit = self.dimensions is not None
        if self.dimensions is None:
            self.dimensions = _default_dimensions(self.id)

    def _should_send_dimensions(self) -> bool:
        return self.dimensions is not None and (
            self._dimensions_explicit or self.id in _OPENAI_EMBEDDING_DIMENSIONS
        )

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

    def response(self, text: str) -> CreateEmbeddingResponse:
        return self.client.embeddings.create(**self._request_params(text))

    async def async_get_embedding(self, text: str) -> list[float]:
        try:
            response: CreateEmbeddingResponse = await self.aclient.embeddings.create(**self._request_params(text))
            return response.data[0].embedding
        except Exception as e:
            log_warning(e)
            return []

    async def async_get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, Any] | None]:
        try:
            response = await self.aclient.embeddings.create(**self._request_params(text))
            embedding = response.data[0].embedding
            usage = response.usage
            return embedding, usage.model_dump() if usage else None
        except Exception as e:
            log_warning(f"Error getting embedding: {e}")
            return [], None

    async def async_get_embeddings_batch_and_usage(
        self,
        texts: list[str],
    ) -> tuple[list[list[float]], list[dict[str, Any] | None]]:
        all_embeddings: list[list[float]] = []
        all_usage: list[dict[str, Any] | None] = []
        log_info(f"Getting embeddings and usage for {len(texts)} texts in batches of {self.batch_size} (async)")

        for i in range(0, len(texts), self.batch_size):
            batch_texts = texts[i : i + self.batch_size]
            try:
                response: CreateEmbeddingResponse = await self.aclient.embeddings.create(
                    **self._request_params(batch_texts)
                )
                batch_embeddings = [data.embedding for data in response.data]
                all_embeddings.extend(batch_embeddings)

                usage_dict = response.usage.model_dump() if response.usage else None
                all_usage.extend([usage_dict] * len(batch_embeddings))
            except Exception as e:
                log_warning(f"Error in async batch embedding: {e}")
                for text in batch_texts:
                    try:
                        embedding, usage = await self.async_get_embedding_and_usage(text)
                        all_embeddings.append(embedding)
                        all_usage.append(usage)
                    except Exception as inner:
                        log_warning(f"Error in individual async embedding fallback: {inner}")
                        all_embeddings.append([])
                        all_usage.append(None)

        return all_embeddings, all_usage
