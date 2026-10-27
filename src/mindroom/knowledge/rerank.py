"""Rerank knowledge search hits with a Cohere-compatible rerank API, Vertex AI ranking, or a local cross-encoder."""

from __future__ import annotations

import asyncio
import functools
import threading
from concurrent.futures import ThreadPoolExecutor
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
_VERTEX_AI_RANK_URL = (
    "https://discoveryengine.googleapis.com/v1/projects/{project}/locations/global"
    "/rankingConfigs/default_ranking_config:rank"
)
#: Most records one Vertex AI ranking request accepts.
_VERTEX_AI_MAX_RECORDS = 200


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


@dataclass(frozen=True)
class _VertexAIReranker:
    """Calls Google's Vertex AI ranking API with Application Default Credentials."""

    model: str
    credentials: Any
    project: str

    async def ascore(self, query: str, documents: Sequence[str]) -> list[float]:
        token = await asyncio.to_thread(_google_access_token, self.credentials)
        records = [{"id": str(index), "content": document} for index, document in enumerate(documents)]
        async with httpx.AsyncClient(timeout=_RERANK_TIMEOUT_SECONDS) as client, asyncio.TaskGroup() as group:
            requests = [
                group.create_task(
                    client.post(
                        _VERTEX_AI_RANK_URL.format(project=self.project),
                        headers={"Authorization": f"Bearer {token}"},
                        json={
                            "model": self.model,
                            "query": query,
                            "records": records[start : start + _VERTEX_AI_MAX_RECORDS],
                            "ignoreRecordDetailsInResponse": True,
                        },
                    ),
                )
                for start in range(0, len(records), _VERTEX_AI_MAX_RECORDS)
            ]
        scores = [float("-inf")] * len(documents)
        for request in requests:
            response = request.result()
            response.raise_for_status()
            for record in response.json()["records"]:
                # A zero score is left out of the response.
                scores[int(record["id"])] = float(record.get("score", 0.0))
        return scores


@functools.cache
def _google_default_credentials() -> tuple[Any, str | None]:
    import google.auth  # noqa: PLC0415

    return google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])


_google_credentials_lock = threading.Lock()


def _google_access_token(credentials: Any) -> str:  # noqa: ANN401
    with _google_credentials_lock:
        if not credentials.valid:
            import google.auth.transport.requests  # noqa: PLC0415

            credentials.refresh(google.auth.transport.requests.Request())
        return credentials.token


#: Loaded ``sentence_transformers.CrossEncoder`` models by name.
_cross_encoders: dict[str, Any] = {}
_cross_encoder_lock = threading.Lock()
_cross_encoder_thread = ThreadPoolExecutor(1, thread_name_prefix="knowledge-rerank")


@dataclass(frozen=True)
class _CrossEncoderReranker:
    """Scores query and document pairs with a sentence-transformers cross-encoder in process."""

    model: str

    async def ascore(self, query: str, documents: Sequence[str]) -> list[float]:
        # Scoring is serialized, so queued calls wait on their own thread, not in the shared default executor.
        return await asyncio.get_running_loop().run_in_executor(_cross_encoder_thread, self._score, query, documents)

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
    if config.provider == "vertex_ai":
        credentials, project = _google_default_credentials()
        if not project:
            msg = "The vertex_ai reranker needs a Google Cloud project in Application Default Credentials"
            raise ValueError(msg)
        return _VertexAIReranker(model=config.model, credentials=credentials, project=project)
    api_key = config.api_key or get_api_key_for_service(config.credentials_service or "cohere", runtime_paths)
    return _CohereReranker(model=config.model, url=config.url or _COHERE_RERANK_URL, api_key=api_key)
