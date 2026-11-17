"""Reranking knowledge search hits."""

from __future__ import annotations

import asyncio
import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING

import google.oauth2.credentials
import httpx
import pytest

import mindroom.knowledge.rerank as rerank_module
import mindroom.knowledge.utils as knowledge_utils
from mindroom.config.knowledge import KnowledgeRerankerConfig
from mindroom.config.main import Config
from mindroom.knowledge.read_protocol import SearchHit
from mindroom.knowledge.rerank import create_reranker
from mindroom.knowledge.search import AgentKnowledge, KnowledgeSource, KnowledgeSourceDescription
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from mindroom.knowledge.rerank import Reranker


class _Index:
    def __init__(self, contents: list[str]) -> None:
        self.contents = contents
        self.limits: list[int] = []

    async def asearch(self, query: str, *, limit: int) -> list[SearchHit]:
        del query
        self.limits.append(limit)
        return [SearchHit(content=content) for content in self.contents[:limit]]


class _LengthReranker:
    """Scores longer documents higher; ``failing`` raises instead."""

    def __init__(self, *, failing: bool = False) -> None:
        self.failing = failing

    async def ascore(self, query: str, documents: Sequence[str]) -> list[float]:
        del query
        if self.failing:
            msg = "rerank service down"
            raise RuntimeError(msg)
        return [float(len(document)) for document in documents]


def _knowledge(reranker: _LengthReranker | None, *indexes: _Index) -> AgentKnowledge:
    return AgentKnowledge(
        sources=tuple(
            KnowledgeSource(description=KnowledgeSourceDescription(base_id=f"base{i}", description="d"), index=index)
            for i, index in enumerate(indexes)
        ),
        reranker=reranker,
    )


@pytest.mark.asyncio
async def test_reranker_orders_candidates_from_every_source() -> None:
    """Each source contributes reranking candidates, and the reranker's order decides the returned hits."""
    first = _Index(["a", "aaaa"])
    second = _Index(["bbb", "bb"])

    hits = await _knowledge(_LengthReranker(), first, second).asearch("q", limit=3)

    assert [hit.content for hit in hits] == ["aaaa", "bbb", "bb"]
    assert first.limits == second.limits == [20]


@pytest.mark.asyncio
async def test_failed_reranking_keeps_the_interleaved_order() -> None:
    """A reranker failure falls back to the sources' own interleaved order."""
    hits = await _knowledge(_LengthReranker(failing=True), _Index(["a", "aaaa"]), _Index(["bbb"])).asearch("q", limit=2)

    assert [hit.content for hit in hits] == ["a", "bbb"]


@pytest.mark.asyncio
async def test_without_reranker_sources_are_asked_for_the_limit_only() -> None:
    """Without a reranker each source is asked for the result limit, as before reranking existed."""
    index = _Index(["a", "b", "c"])

    hits = await _knowledge(None, index).asearch("q", limit=2)

    assert [hit.content for hit in hits] == ["a", "b"]
    assert index.limits == [2]


@pytest.mark.asyncio
async def test_cohere_reranker_sends_cohere_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Cohere-compatible reranker posts the query and documents and maps scores back by index."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.2}]},
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        rerank_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    reranker = create_reranker(
        KnowledgeRerankerConfig(
            provider="cohere",
            model="rerank-v3.5",
            url="https://rerank.test/v1/rerank",
            api_key="k",
        ),
        test_runtime_paths(tmp_path),
    )

    assert await reranker.ascore("query", ["first", "second"]) == [0.2, 0.9]
    [request] = requests
    assert str(request.url) == "https://rerank.test/v1/rerank"
    assert request.headers["Authorization"] == "Bearer k"
    assert json.loads(request.content) == {
        "model": "rerank-v3.5",
        "query": "query",
        "documents": ["first", "second"],
        "top_n": 2,
    }


@pytest.mark.asyncio
async def test_partial_cohere_response_reranks_the_returned_hits_and_puts_the_missing_one_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rerank response that omits a document still orders the others, raw negative logits included; it comes last."""

    def respond(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={"results": [{"index": 2, "relevance_score": -1.5}, {"index": 1, "relevance_score": -3.0}]},
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        rerank_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    reranker = create_reranker(
        KnowledgeRerankerConfig(provider="cohere", model="m", url="https://rerank.test/v1/rerank", api_key="k"),
        test_runtime_paths(tmp_path),
    )
    knowledge = AgentKnowledge(
        sources=(
            KnowledgeSource(
                description=KnowledgeSourceDescription(base_id="base", description="d"),
                index=_Index(["a", "b", "c"]),
            ),
        ),
        reranker=reranker,
    )

    hits = await knowledge.asearch("q", limit=3)

    assert [hit.content for hit in hits] == ["c", "b", "a"]


def test_cohere_reranker_reads_its_credential_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an explicit key, the reranker reads the configured credential service, defaulting to ``cohere``."""
    services: list[str] = []
    monkeypatch.setattr(
        rerank_module,
        "get_api_key_for_service",
        lambda service, _paths: services.append(service) or "s",
    )
    runtime_paths = test_runtime_paths(tmp_path)

    create_reranker(KnowledgeRerankerConfig(provider="cohere", model="m"), runtime_paths)
    create_reranker(KnowledgeRerankerConfig(provider="cohere", model="m", credentials_service="jina"), runtime_paths)

    assert services == ["cohere", "jina"]


class _GoogleCredentials(google.oauth2.credentials.Credentials):
    """User Application Default Credentials that hold token ``t`` once refreshed."""

    def refresh(self, request: object) -> None:
        del request
        self.token = "t"  # noqa: S105


@pytest.mark.asyncio
async def test_vertex_ai_reranker_splits_records_into_ranking_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Candidates go out in concurrent requests of 100 records, the unit Vertex AI bills, and are scored together.

    The credential file comes from the runtime's ``.env``, which MindRoom does not export, and names the project.
    """
    bodies: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "https://discoveryengine.googleapis.com/v1/projects/key-project/locations/global"
            "/rankingConfigs/default_ranking_config:rank"
        )
        assert request.headers["Authorization"] == "Bearer t"
        assert request.headers["x-goog-user-project"] == "quota"
        body = json.loads(request.content)
        bodies.append(body)
        # A zero score is left out of the JSON response, like other proto3 defaults.
        records = [
            {"id": record["id"], **({"score": int(record["id"]) / 1000} if record["id"] != "0" else {})}
            for record in body["records"]
        ]
        return httpx.Response(200, json={"records": records[::-1]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        rerank_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    credentials = _GoogleCredentials(token=None, quota_project_id="quota")
    credentials.project_id = "key-project"  # as a service account key carries it
    credential_files: list[str] = []
    monkeypatch.setattr(
        rerank_module,
        "load_google_application_credentials",
        lambda path: credential_files.append(path) or credentials,
    )
    (tmp_path / ".env").write_text("GOOGLE_APPLICATION_CREDENTIALS=adc.json\n")
    rerank_module._google_credentials.cache_clear()
    reranker = create_reranker(
        KnowledgeRerankerConfig(provider="vertex_ai", model="semantic-ranker-default@latest"),
        test_runtime_paths(tmp_path),
    )

    documents = [f"doc {index}" for index in range(250)]
    assert await reranker.ascore("query", documents) == [index / 1000 for index in range(250)]
    assert sorted(len(body["records"]) for body in bodies) == [50, 100, 100]
    assert {"id": "200", "content": "doc 200"} in [body["records"][0] for body in bodies]
    assert {body["model"] for body in bodies} == {"semantic-ranker-default@latest"}
    assert {body["query"] for body in bodies} == {"query"}
    assert credential_files == [str((tmp_path / "adc.json").resolve())]


def test_vertex_ai_reranker_reads_the_project_as_vertex_models_read_it(tmp_path: Path) -> None:
    """A blank exported ``GOOGLE_CLOUD_PROJECT`` is unset, so the ``.env`` project under it is the ranking project."""
    (tmp_path / ".env").write_text("GOOGLE_CLOUD_PROJECT=env-project\n", encoding="utf-8")
    runtime_paths = replace(test_runtime_paths(tmp_path), process_env={"GOOGLE_CLOUD_PROJECT": ""})

    reranker = create_reranker(KnowledgeRerankerConfig(provider="vertex_ai", model="m"), runtime_paths)

    assert isinstance(reranker, rerank_module._VertexAIReranker)
    assert reranker.project == "env-project"


@pytest.mark.asyncio
async def test_vertex_ai_reranker_needs_a_google_cloud_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without GOOGLE_CLOUD_PROJECT or a project in Application Default Credentials, ranking fails."""
    monkeypatch.setattr(
        rerank_module,
        "load_ambient_google_credentials",
        lambda: (_GoogleCredentials(token=None), None),
    )
    rerank_module._google_credentials.cache_clear()
    reranker = create_reranker(KnowledgeRerankerConfig(provider="vertex_ai", model="m"), test_runtime_paths(tmp_path))

    with pytest.raises(ValueError, match="GOOGLE_CLOUD_PROJECT"):
        await reranker.ascore("q", ["d"])


@pytest.mark.asyncio
async def test_cross_encoder_reranker_scores_query_document_pairs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local reranker scores each query and document pair with the cross-encoder."""

    class _Encoder:
        def predict(self, sentences: list[tuple[str, str]]) -> list[float]:
            return [float(len(document)) for _query, document in sentences]

    monkeypatch.setattr(rerank_module, "ensure_sentence_transformers_dependencies", lambda _paths: None)
    monkeypatch.setitem(rerank_module._cross_encoders, "local-model", _Encoder())
    reranker = create_reranker(
        KnowledgeRerankerConfig(provider="sentence_transformers", model="local-model"),
        test_runtime_paths(tmp_path),
    )

    assert await reranker.ascore("q", ["ab", "abc"]) == [2.0, 3.0]


@pytest.mark.asyncio
async def test_local_reranking_does_not_need_the_shared_default_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local scoring runs on its own thread, so a busy default executor does not hold up a rerank."""

    class _Encoder:
        def predict(self, sentences: list[tuple[str, str]]) -> list[float]:
            return [1.0 for _sentence in sentences]

    monkeypatch.setattr(rerank_module, "ensure_sentence_transformers_dependencies", lambda _paths: None)
    monkeypatch.setitem(rerank_module._cross_encoders, "local-model", _Encoder())
    reranker = create_reranker(
        KnowledgeRerankerConfig(provider="sentence_transformers", model="local-model"),
        test_runtime_paths(tmp_path),
    )
    loop = asyncio.get_running_loop()
    busy = ThreadPoolExecutor(1)
    loop.set_default_executor(busy)
    release = threading.Event()
    blocked = loop.run_in_executor(None, release.wait)
    try:
        assert await asyncio.wait_for(reranker.ascore("q", ["a", "b"]), timeout=5) == [1.0, 1.0]
    finally:
        release.set()
        await blocked
        busy.shutdown()


def _serve(monkeypatch: pytest.MonkeyPatch, respond: Callable[[httpx.Request], httpx.Response]) -> None:
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        rerank_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )


def _cohere_leaving_out_the_last(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Reranker:
    _serve(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            json={"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.2}]},
        ),
    )
    config = KnowledgeRerankerConfig(provider="cohere", model="m", url="https://rerank.test", api_key="k")
    return create_reranker(config, test_runtime_paths(tmp_path))


def _vertex_ai_leaving_out_the_last(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Reranker:
    _serve(
        monkeypatch,
        lambda _request: httpx.Response(200, json={"records": [{"id": "1", "score": 0.9}, {"id": "0", "score": 0.2}]}),
    )
    monkeypatch.setattr(
        rerank_module,
        "load_ambient_google_credentials",
        lambda: (_GoogleCredentials(token=None), "project"),
    )
    rerank_module._google_credentials.cache_clear()
    return create_reranker(KnowledgeRerankerConfig(provider="vertex_ai", model="m"), test_runtime_paths(tmp_path))


def _cross_encoder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Reranker:
    class _Encoder:
        """Answers as `CrossEncoder.predict` does for a one-label model: its default activation is a sigmoid."""

        def predict(self, sentences: list[tuple[str, str]]) -> list[float]:
            return [1 / (1 + math.exp(-logit)) for logit in (-1.4, 2.2, -9.0)[: len(sentences)]]

    monkeypatch.setattr(rerank_module, "ensure_sentence_transformers_dependencies", lambda _paths: None)
    monkeypatch.setitem(rerank_module._cross_encoders, "local-model", _Encoder())
    config = KnowledgeRerankerConfig(provider="sentence_transformers", model="local-model")
    return create_reranker(config, test_runtime_paths(tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize("build", [_cohere_leaving_out_the_last, _vertex_ai_leaving_out_the_last, _cross_encoder])
async def test_scores_are_in_unit_interval(
    build: Callable[[pytest.MonkeyPatch, Path], Reranker],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every provider scores each document in [0, 1], and a document missing from the response scores ``-inf``.

    ``-inf`` is the unjudged marker: it ranks below a raw negative logit and passes no recall threshold.
    """
    scores = await build(monkeypatch, tmp_path).ascore("q", ["first", "second", "third"])

    assert len(scores) == 3
    assert scores[1] > scores[0] > scores[2]
    if build is _cross_encoder:
        assert all(0.0 <= score <= 1.0 for score in scores)
    else:
        assert scores == [0.2, 0.9, -math.inf]


@pytest.mark.asyncio
async def test_a_reranker_that_cannot_be_built_keeps_the_interleaved_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured reranker is built when it scores, so a failed build only falls back to the search order."""

    def failing_build(_config: KnowledgeRerankerConfig, _runtime_paths: object) -> Reranker:
        msg = "sentence-transformers extra missing"
        raise ImportError(msg)

    monkeypatch.setattr(knowledge_utils, "create_reranker", failing_build)
    config = Config(reranker={"provider": "sentence_transformers", "model": "local-model"})
    reranker = knowledge_utils.configured_reranker(config, test_runtime_paths(tmp_path))
    assert reranker is not None
    knowledge = AgentKnowledge(
        sources=(
            KnowledgeSource(
                description=KnowledgeSourceDescription(base_id="base", description="d"),
                index=_Index(["a", "bbb", "cc"]),
            ),
        ),
        reranker=reranker,
    )

    assert [hit.content for hit in await knowledge.asearch("q", limit=2)] == ["a", "bbb"]
