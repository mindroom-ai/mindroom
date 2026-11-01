"""Rerank knowledge search hits with a Cohere-compatible rerank API, Vertex AI ranking, or a local cross-encoder."""

from __future__ import annotations

import asyncio
import functools
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from mindroom.constants import runtime_env_path
from mindroom.credentials.environment import get_secret_from_env
from mindroom.credentials.sync import get_api_key_for_service
from mindroom.embeddings.signatures import ensure_sentence_transformers_dependencies
from mindroom.providers.google_adc import load_ambient_google_credentials, load_google_application_credentials
from mindroom.tool_system.dependencies import optional_dependency_environment

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.config.knowledge import KnowledgeRerankerConfig
    from mindroom.constants import RuntimePaths

_COHERE_RERANK_URL = "https://api.cohere.com/v2/rerank"
_RERANK_TIMEOUT_SECONDS = 30.0
_VERTEX_AI_RANK_URL = (
    "https://discoveryengine.googleapis.com/v1/projects/{project}/locations/global"
    "/rankingConfigs/default_ranking_config:rank"
)
#: Records per Vertex AI ranking request: Google bills each 100 as one query, and smaller requests answer sooner.
_VERTEX_AI_RECORDS_PER_REQUEST = 100


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
    """Calls Google's Vertex AI ranking API with Google Cloud credentials, loaded on the first search."""

    model: str
    credentials_path: Path | None
    project: str | None

    async def ascore(self, query: str, documents: Sequence[str]) -> list[float]:
        headers, project = await asyncio.to_thread(_google_request_auth, self.credentials_path, self.project)
        records = [{"id": str(index), "content": document} for index, document in enumerate(documents)]
        async with httpx.AsyncClient(timeout=_RERANK_TIMEOUT_SECONDS) as client, asyncio.TaskGroup() as group:
            requests = [
                group.create_task(
                    client.post(
                        _VERTEX_AI_RANK_URL.format(project=project),
                        headers=headers,
                        json={
                            "model": self.model,
                            "query": query,
                            "records": records[start : start + _VERTEX_AI_RECORDS_PER_REQUEST],
                            "ignoreRecordDetailsInResponse": True,
                        },
                    ),
                )
                for start in range(0, len(records), _VERTEX_AI_RECORDS_PER_REQUEST)
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
def _google_credentials(credentials_path: Path | None) -> tuple[Any, str | None]:
    """Load the runtime's Google credential file, or else Application Default Credentials, once per process.

    Returns the credentials with the project they name; a service account key names its own, user credentials none.
    """
    if credentials_path is None:
        return load_ambient_google_credentials()
    credentials = load_google_application_credentials(str(credentials_path))
    return credentials, getattr(credentials, "project_id", None)


_google_credentials_lock = threading.Lock()


def _google_request_auth(credentials_path: Path | None, project: str | None) -> tuple[dict[str, str], str]:
    """Return the headers a ranking request signs with, such as its quota project, and the project it ranks in."""
    credentials, credentials_project = _google_credentials(credentials_path)
    project = project or credentials_project
    if not project:
        msg = "The vertex_ai reranker needs GOOGLE_CLOUD_PROJECT or Google credentials that name a project"
        raise ValueError(msg)
    headers: dict[str, str] = {}
    with _google_credentials_lock:
        if not credentials.valid:
            import google.auth.transport.requests  # noqa: PLC0415

            credentials.refresh(google.auth.transport.requests.Request())
        credentials.apply(headers)
    return headers, project


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
        return _VertexAIReranker(
            model=config.model,
            credentials_path=runtime_env_path(runtime_paths, "GOOGLE_APPLICATION_CREDENTIALS"),
            project=get_secret_from_env("GOOGLE_CLOUD_PROJECT", runtime_paths=runtime_paths),
        )
    api_key = config.api_key or get_api_key_for_service(config.credentials_service or "cohere", runtime_paths)
    return _CohereReranker(model=config.model, url=config.url or _COHERE_RERANK_URL, api_key=api_key)
