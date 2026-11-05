"""Tests for the Vertex AI embedder."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import httpx
import pytest
from google import genai
from google.genai.types import HttpOptions, HttpRetryOptions
from google.oauth2.credentials import Credentials
from pydantic import ValidationError

import mindroom.embeddings.vertex_ai_embedder as vertex_ai_embedder_module
from mindroom.config.main import Config
from mindroom.embeddings.embedder_health import (
    capture_embedder_health_recorder,
    embedder_in_use,
    get_embedder_failure,
    probe_embedder,
)
from mindroom.embeddings.errors import (
    EMBEDDER_EMPTY_VECTOR_DETAIL,
    EmbedderRequestError,
    embedder_credentials_rejected,
    embedder_failure_is_transient,
)
from mindroom.embeddings.factory import create_configured_embedder
from mindroom.embeddings.signatures import effective_knowledge_embedder_signature
from mindroom.embeddings.vertex_ai_embedder import VertexAIEmbedder
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_MODEL_PATH = "/v1beta1/projects/test-project/locations/global/publishers/google/models/gemini-embedding-2:embedContent"

_Reply = list[float] | httpx.Response


@pytest.fixture(autouse=True)
def _reset_embedder_health() -> Iterator[None]:
    capture_embedder_health_recorder().record(None)
    yield
    capture_embedder_health_recorder().record(None)


def _transport(replies: list[_Reply], requests: list[httpx.Request]) -> httpx.MockTransport:
    """Return a Vertex AI server that answers each request with the next reply, repeating the last one."""

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        reply = replies[min(len(requests), len(replies)) - 1]
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json={"embedding": {"values": reply}})

    return httpx.MockTransport(handle)


def _embedder(replies: list[_Reply], requests: list[httpx.Request], **settings: object) -> VertexAIEmbedder:
    transport = _transport(replies, requests)
    client = genai.Client(
        vertexai=True,
        project="test-project",
        location="global",
        credentials=Credentials(token="test-token"),  # noqa: S106 - synthetic SDK credential
        http_options=HttpOptions(
            httpx_client=httpx.Client(transport=transport),
            httpx_async_client=httpx.AsyncClient(transport=transport),
            retry_options=HttpRetryOptions(attempts=1),
        ),
    )
    return VertexAIEmbedder(
        model="gemini-embedding-2",
        health_recorder=capture_embedder_health_recorder(),
        client=client,
        **settings,
    )


def _serve_clients(
    monkeypatch: pytest.MonkeyPatch,
    replies: list[_Reply],
    requests: list[httpx.Request],
) -> list[dict[str, object]]:
    """Point every client the embedder builds at a local server, keeping its own settings; return those settings."""
    built: list[dict[str, object]] = []
    real_client = genai.Client
    transport = _transport(replies, requests)

    def client_on_server(**kwargs: object) -> genai.Client:
        built.append(kwargs)
        http_options = {
            **kwargs.get("http_options", {}),  # type: ignore[dict-item]
            "httpx_client": httpx.Client(transport=transport),
            "httpx_async_client": httpx.AsyncClient(transport=transport),
        }
        test_credentials = Credentials(token="test-token")  # noqa: S106 - synthetic SDK credential
        project = kwargs.get("project") or "test-project"
        return real_client(
            **{**kwargs, "project": project, "credentials": test_credentials, "http_options": http_options},
        )

    monkeypatch.setattr(vertex_ai_embedder_module.genai, "Client", client_on_server)
    return built


def _vertex_config(runtime_paths: RuntimePaths, **embedder_config: object) -> Config:
    """Return a config whose semantic knowledge base embeds with Vertex AI, beside file-backed memory."""
    return bind_runtime_paths(
        Config.model_validate(
            {
                "models": {},
                "memory": {
                    "backend": "file",
                    "embedder": {"provider": "vertex_ai", "config": {"model": "gemini-embedding-2", **embedder_config}},
                },
                "knowledge_bases": {"docs": {"path": "docs", "mode": "semantic"}},
            },
        ),
        runtime_paths,
    )


def test_vertex_ai_embedder_sends_one_text_per_request_with_its_prefix_and_width() -> None:
    """Each text gets its own embedContent request, which Vertex AI requires, with its purpose's prefix and the width."""
    requests: list[httpx.Request] = []
    vector = [0.25] * 768
    embedder = _embedder(
        [vector],
        requests,
        dimensions=768,
        query_prefix="task: search result | query: ",
        document_prefix="title: none | text: ",
    )

    assert not embedder.sends_batches
    assert embedder.embed(["first", "second"], purpose="document") == [vector, vector]
    assert asyncio.run(embedder.aembed(["question"], purpose="query")) == [vector]
    assert [request.url.path for request in requests] == [_MODEL_PATH] * 3
    bodies = [json.loads(request.content) for request in requests]
    assert [body["content"]["parts"][0]["text"] for body in bodies] == [
        "title: none | text: first",
        "title: none | text: second",
        "task: search result | query: question",
    ]
    assert all(body["embedContentConfig"]["outputDimensionality"] == 768 for body in bodies)


def test_vertex_ai_embedder_leaves_the_width_and_text_alone_by_default() -> None:
    """Without settings, the text goes as written and the model returns its full-size vector."""
    requests: list[httpx.Request] = []
    embedder = _embedder([[0.5] * 3072], requests)

    assert embedder.embed(["text"], purpose="query") == [[0.5] * 3072]
    body = json.loads(requests[0].content)
    assert body["content"]["parts"][0]["text"] == "text"
    assert "outputDimensionality" not in body.get("embedContentConfig", {})


def test_vertex_ai_embedder_rejects_an_empty_reply() -> None:
    """A reply without a vector raises instead of handing an empty vector to the index, and is recorded."""
    embedder = _embedder([[]], [])

    with pytest.raises(EmbedderRequestError, match=EMBEDDER_EMPTY_VECTOR_DETAIL):
        embedder.embed(["text"], purpose="document")
    assert get_embedder_failure() == EMBEDDER_EMPTY_VECTOR_DETAIL


@pytest.mark.parametrize(
    ("status_code", "credentials", "transient"),
    [(401, True, False), (403, True, False), (429, False, True), (503, False, True), (404, False, False)],
)
def test_vertex_ai_embedder_reports_an_http_error_by_its_status(
    status_code: int,
    credentials: bool,
    transient: bool,
) -> None:
    """An HTTP error names only its status: rejected credentials stop indexing, throttling and outages are retried."""
    reply = httpx.Response(status_code, json={"error": {"code": status_code, "message": "secret-bearing text"}})
    embedder = _embedder([reply], [])

    with pytest.raises(EmbedderRequestError) as excinfo:
        asyncio.run(embedder.aembed(["text"], purpose="query"))
    assert str(status_code) in str(excinfo.value)
    assert embedder_credentials_rejected(excinfo.value) is credentials
    assert embedder_failure_is_transient(excinfo.value) is transient
    # A logged traceback shows neither the provider's text nor its exception.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__
    assert "secret-bearing" not in str(excinfo.value)


def test_vertex_ai_embedder_health_shows_a_rejection_until_a_request_succeeds() -> None:
    """A rejected credential shows in the runtime's embedder health, and the next successful request clears it."""
    denied = httpx.Response(403, json={"error": {"code": 403, "message": "Permission denied"}})
    embedder = _embedder([denied, [0.5] * 8], [])

    with pytest.raises(EmbedderRequestError):
        embedder.embed(["text"], purpose="query")
    assert get_embedder_failure() == "embedder permission denied (HTTP 403)"
    assert embedder.embed(["text"], purpose="query") == [[0.5] * 8]
    assert get_embedder_failure() is None


def test_vertex_ai_embedder_runs_the_runtime_embedder_health_checks(tmp_path: Path) -> None:
    """Startup, reload, and refresh health checks cover a Vertex AI embedder, whose credentials can be revoked."""
    assert embedder_in_use(_vertex_config(test_runtime_paths(tmp_path)))


def test_configured_vertex_ai_embedder_uses_the_runtime_project_and_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GOOGLE_CLOUD_PROJECT`` and ``GOOGLE_CLOUD_LOCATION`` in the runtime env choose where Vertex AI embeds."""
    for variable in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(variable, raising=False)
    (tmp_path / ".env").write_text(
        "GOOGLE_CLOUD_PROJECT=env-project\nGOOGLE_CLOUD_LOCATION=us-west1\n",
        encoding="utf-8",
    )
    runtime_paths = test_runtime_paths(tmp_path)
    requests: list[httpx.Request] = []
    built = _serve_clients(monkeypatch, [[0.5] * 768], requests)
    embedder = create_configured_embedder(_vertex_config(runtime_paths, dimensions=768), runtime_paths)

    assert isinstance(embedder, VertexAIEmbedder)
    assert embedder.embed(["text"], purpose="document") == [[0.5] * 768]
    assert len(built) == 1
    assert built[0]["vertexai"] is True
    assert requests[0].url.path.startswith("/v1beta1/projects/env-project/locations/us-west1/")


@pytest.mark.parametrize(("caller_retries", "sent"), [(False, 2), (True, 1)])
def test_vertex_ai_embedder_retries_a_brief_outage_unless_its_caller_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caller_retries: bool,
    sent: int,
) -> None:
    """A search's request outlives a brief outage, while a knowledge refresh, which retries itself, sends one."""
    runtime_paths = test_runtime_paths(tmp_path)
    requests: list[httpx.Request] = []
    _serve_clients(monkeypatch, [httpx.Response(503, json={"error": {"code": 503}}), [0.5] * 8], requests)
    embedder = create_configured_embedder(_vertex_config(runtime_paths), runtime_paths, caller_retries=caller_retries)

    if caller_retries:
        with pytest.raises(EmbedderRequestError, match=r"HTTP 503"):
            embedder.embed(["text"], purpose="query")
    else:
        assert embedder.embed(["text"], purpose="query") == [[0.5] * 8]
    assert len(requests) == sent


def test_probe_bounds_the_vertex_ai_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A health probe gives Vertex AI one attempt of 10 seconds, so a stalled endpoint fails in seconds, not minutes."""
    runtime_paths = test_runtime_paths(tmp_path)
    requests: list[httpx.Request] = []
    built = _serve_clients(monkeypatch, [httpx.Response(503, json={"error": {"code": 503}}), [0.5] * 8], requests)

    assert probe_embedder(_vertex_config(runtime_paths), runtime_paths) == "embedder request failed (HTTP 503)"
    assert len(requests) == 1
    assert built[0]["http_options"]["timeout"] == 10_000  # type: ignore[index]


def test_vertex_ai_embedder_config_accepts_only_the_fields_it_reads(tmp_path: Path) -> None:
    """Config load rejects a host for Vertex AI, which authenticates with Google Cloud credentials."""
    runtime_paths = test_runtime_paths(tmp_path)
    _vertex_config(runtime_paths, dimensions=768, query_prefix="q: ")
    with pytest.raises(ValidationError, match=r"memory\.embedder\.config\.host does not apply to the vertex_ai"):
        _vertex_config(runtime_paths, host="http://x")


@pytest.mark.parametrize(
    ("memory_backend", "agent_memory_backend"),
    [(None, None), ("mem0", None), ("file", "mem0")],
    ids=["default_backend", "mem0_backend", "agent_mem0_backend"],
)
def test_vertex_ai_embedder_config_rejects_mem0_memory(
    memory_backend: str | None,
    agent_memory_backend: str | None,
) -> None:
    """Mem0 cannot embed with vertex_ai, so a config whose memory would use Mem0 fails at load instead of at runtime."""
    agent: dict[str, str] = {"display_name": "Helper", "role": "Helps"}
    if agent_memory_backend is not None:
        agent["memory_backend"] = agent_memory_backend
    memory: dict[str, object] = {"embedder": {"provider": "vertex_ai", "config": {"model": "gemini-embedding-2"}}}
    if memory_backend is not None:
        memory["backend"] = memory_backend
    with pytest.raises(ValidationError, match=r"the vertex_ai embedder does not serve the mem0 memory backend"):
        Config.model_validate({"models": {}, "agents": {"helper": agent}, "memory": memory})


def test_vertex_ai_prefixes_name_new_vectors() -> None:
    """Prefixes change every Vertex AI vector, so indexes built without them are not reused."""
    plain = effective_knowledge_embedder_signature("vertex_ai", "gemini-embedding-2")
    prefixed = effective_knowledge_embedder_signature("vertex_ai", "gemini-embedding-2", query_prefix="q: ")

    assert plain == ("vertex_ai", "gemini-embedding-2", "", "")
    assert prefixed != plain
