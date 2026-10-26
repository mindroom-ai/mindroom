"""Rerank knowledge search hits with a Cohere-compatible rerank API or a local cross-encoder."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from mindroom.credentials.sync import get_api_key_for_service
from mindroom.embeddings.signatures import ensure_sentence_transformers_dependencies
from mindroom.tool_system.dependencies import optional_dependency_environment

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.knowledge import KnowledgeRerankerConfig
    from mindroom.constants import RuntimePaths

_COHERE_RERANK_URL = "https://api.cohere.com/v2/rerank"
_RERANK_TIMEOUT_SECONDS = 30.0


class Reranker(Protocol):
    """Scores how well each document answers a query; higher is better."""

    async def ascore(self, query: str, documents: Sequence[str]) -> list[float]:
        """Return one relevance score per document, in the documents' order."""
        ...


@dataclass(frozen=True)
class _CohereReranker:
    """Calls a rerank endpoint speaking Cohere's request and response shape."""

    model: str
    url: str
    api_key: str | None

    async def ascore(self, query: str, documents: Sequence[str]) -> list[float]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        async with httpx.AsyncClient(timeout=_RERANK_TIMEOUT_SECONDS) as client:
            response = await client.post(
                self.url,
                headers=headers,
                json={"model": self.model, "query": query, "documents": list(documents), "top_n": len(documents)},
            )
        response.raise_for_status()
        scores = [float("-inf")] * len(documents)
        for result in response.json()["results"]:
            scores[int(result["index"])] = float(result["relevance_score"])
        return scores


#: Loaded ``sentence_transformers.CrossEncoder`` models by name.
_cross_encoders: dict[str, Any] = {}
_cross_encoder_lock = threading.Lock()


@dataclass(frozen=True)
class _CrossEncoderReranker:
    """Scores query and document pairs with a sentence-transformers cross-encoder in process."""

    model: str

    async def ascore(self, query: str, documents: Sequence[str]) -> list[float]:
        return await asyncio.to_thread(self._score, query, documents)

    def _score(self, query: str, documents: Sequence[str]) -> list[float]:
        with _cross_encoder_lock:
            encoder = _cross_encoders.get(self.model)
            if encoder is None:
                with optional_dependency_environment():
                    from sentence_transformers import CrossEncoder  # noqa: PLC0415

                encoder = _cross_encoders[self.model] = CrossEncoder(self.model)
            return [float(score) for score in encoder.predict([(query, document) for document in documents])]


def create_reranker(config: KnowledgeRerankerConfig, runtime_paths: RuntimePaths) -> Reranker:
    """Build the configured reranker."""
    if config.provider == "sentence_transformers":
        ensure_sentence_transformers_dependencies(runtime_paths)
        return _CrossEncoderReranker(model=config.model)
    api_key = config.api_key or get_api_key_for_service(config.credentials_service or "cohere", runtime_paths)
    return _CohereReranker(model=config.model, url=config.url or _COHERE_RERANK_URL, api_key=api_key)
