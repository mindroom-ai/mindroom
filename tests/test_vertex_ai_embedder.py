"""Tests for the Vertex AI embedder."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
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


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Send a retried request again at once, unless Vertex AI names a wait."""
    monkeypatch.setattr(vertex_ai_embedder_module, "_RETRY_BASE_SECONDS", 0.0)


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
    *,
    build_seconds: float = 0.0,
    sign_in: bool = True,
) -> list[dict[str, object]]:
    """Point every client the embedder builds at a local server, keeping its own settings; return those settings.

    ``sign_in`` gives each client test credentials, and a project unless it has one; without it, google-genai finds
    its own.
    """
    built: list[dict[str, object]] = []
    real_client = genai.Client
    transport = _transport(replies, requests)

    def client_on_server(**kwargs: object) -> genai.Client:
        built.append(kwargs)
        time.sleep(build_seconds)
        http_options = {
            **kwargs.get("http_options", {}),  # type: ignore[dict-item]
            "httpx_client": httpx.Client(transport=transport),
            "httpx_async_client": httpx.AsyncClient(transport=transport),
        }
        if sign_in:
            test_credentials = Credentials(token="test-token")  # noqa: S106 - synthetic SDK credential
            kwargs = {**kwargs, "project": kwargs.get("project") or "test-project", "credentials": test_credentials}
        return real_client(**{**kwargs, "http_options": http_options})

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


def test_configured_vertex_ai_embedder_embeds_in_the_project_its_key_file_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``GOOGLE_CLOUD_PROJECT``, Vertex AI embeds in the project of the service account key file."""
    for variable in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(variable, raising=False)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_file = tmp_path / "key.json"
    key_file.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "key-project",
                "private_key": private_key.decode(),
                "client_email": "embedder@key-project.iam.gserviceaccount.com",
                "token_uri": "https://oauth2.googleapis.com/token",
            },
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        f"GOOGLE_APPLICATION_CREDENTIALS={key_file}\nGOOGLE_CLOUD_LOCATION=global\n",
        encoding="utf-8",
    )
    runtime_paths = test_runtime_paths(tmp_path)
    requests: list[httpx.Request] = []
    _serve_clients(monkeypatch, [[0.5] * 8], requests)
    embedder = create_configured_embedder(_vertex_config(runtime_paths), runtime_paths)

    assert embedder.embed(["text"], purpose="document") == [[0.5] * 8]
    assert requests[0].url.path.startswith("/v1beta1/projects/key-project/locations/global/")


def test_vertex_ai_embedder_signs_in_with_google_credentials_beside_a_gemini_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no project or location set, Application Default Credentials sign the request, not a Gemini API key.

    google-genai would otherwise send a ``GEMINI_API_KEY`` from the environment, kept for Gemini chat models, to Vertex
    AI's API-key mode.
    """
    for variable in (
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "ambient-gemini-key")
    adc = Credentials(token="adc-token")  # noqa: S106 - synthetic SDK credential
    monkeypatch.setattr("google.auth.default", lambda **_: (adc, "adc-project"))
    runtime_paths = test_runtime_paths(tmp_path)
    requests: list[httpx.Request] = []
    _serve_clients(monkeypatch, [[0.5] * 8], requests, sign_in=False)
    embedder = create_configured_embedder(_vertex_config(runtime_paths), runtime_paths)

    assert embedder.embed(["text"], purpose="query") == [[0.5] * 8]
    assert requests[0].url.path.startswith("/v1beta1/projects/adc-project/locations/global/")
    assert requests[0].headers["authorization"] == "Bearer adc-token"
    assert "x-goog-api-key" not in requests[0].headers


def test_vertex_ai_embedder_builds_one_client_for_concurrent_texts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A knowledge refresh embeds several files at once on worker threads, and they share one client.

    A client each thread built would replace the others, and google-genai closes a replaced client's connection while
    its request is still in flight.
    """
    runtime_paths = test_runtime_paths(tmp_path)
    built = _serve_clients(monkeypatch, [[0.5] * 8], [], build_seconds=0.05)
    embedder = create_configured_embedder(_vertex_config(runtime_paths), runtime_paths, caller_retries=True)

    with ThreadPoolExecutor(8) as threads:
        vectors = list(threads.map(lambda index: embedder.embed([f"file {index}"], purpose="document"), range(8)))

    assert vectors == [[[0.5] * 8]] * 8
    assert len(built) == 1


def test_vertex_ai_embedder_builds_its_client_off_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A search builds the client on a worker thread: without a project, google-genai looks up credentials, which blocks."""
    runtime_paths = test_runtime_paths(tmp_path)
    _serve_clients(monkeypatch, [[0.5] * 8], [])
    serve = vertex_ai_embedder_module.genai.Client
    built_on: list[int] = []

    def record_thread(**kwargs: object) -> genai.Client:
        built_on.append(threading.get_ident())
        return serve(**kwargs)

    monkeypatch.setattr(vertex_ai_embedder_module.genai, "Client", record_thread)
    embedder = create_configured_embedder(_vertex_config(runtime_paths), runtime_paths)

    async def search() -> int:
        assert await embedder.aembed(["text"], purpose="query") == [[0.5] * 8]
        return threading.get_ident()

    event_loop_thread = asyncio.run(search())
    assert len(built_on) == 1
    assert built_on[0] != event_loop_thread


@pytest.mark.parametrize("caller_retries", [False, True])
def test_vertex_ai_embedder_retries_each_text_through_a_brief_outage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    caller_retries: bool,
) -> None:
    """Each text's request outlives a brief outage, for a search and for a knowledge refresh alike.

    A refresh retries a whole file, and a file sends one request per text, so a refresh relying on its own retries would
    send every text of a large file again. No log line of the retry carries the provider's response text.
    """
    runtime_paths = test_runtime_paths(tmp_path)
    requests: list[httpx.Request] = []
    outage = httpx.Response(503, json={"error": {"code": 503, "message": "secret-bearing text"}})
    built = _serve_clients(monkeypatch, [outage, [0.5] * 8], requests)
    embedder = create_configured_embedder(_vertex_config(runtime_paths), runtime_paths, caller_retries=caller_retries)

    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(embedder.aembed(["text"], purpose="document")) == [[0.5] * 8]
    assert len(requests) == 2
    assert "secret-bearing" not in caplog.text
    # Requests are bounded even outside health probes.
    assert built[0]["http_options"]["timeout"] == 60_000  # type: ignore[index]


def test_vertex_ai_embedder_waits_as_long_as_vertex_ai_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A throttled request is sent again after the ``Retry-After`` wait Vertex AI names."""
    waits: list[float] = []
    monkeypatch.setattr(vertex_ai_embedder_module.time, "sleep", waits.append)
    throttled = httpx.Response(429, headers={"Retry-After": "3"}, json={"error": {"code": 429}})
    embedder = _embedder([throttled, [0.5] * 8], [])

    assert embedder.embed(["text"], purpose="query") == [[0.5] * 8]
    assert waits == [3.0]


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
    ("memory_backend", "agent_memory_backends"),
    [(None, [None]), ("mem0", [None]), ("file", ["mem0"]), ("file", [None, "none"])],
    ids=["default_backend", "mem0_backend", "agent_mem0_backend", "team_of_file_and_none"],
)
def test_vertex_ai_embedder_config_rejects_mem0_memory(
    memory_backend: str | None,
    agent_memory_backends: list[str | None],
) -> None:
    """Mem0 cannot embed with vertex_ai, so a config whose memory would use Mem0 fails at load instead of at runtime.

    A team whose members mix file and no memory remembers with Mem0, so agents that mix them are rejected too.
    """
    agents: dict[str, dict[str, str]] = {}
    for index, agent_memory_backend in enumerate(agent_memory_backends):
        agent = {"display_name": f"Helper {index}", "role": "Helps"}
        if agent_memory_backend is not None:
            agent["memory_backend"] = agent_memory_backend
        agents[f"helper_{index}"] = agent
    memory: dict[str, object] = {"embedder": {"provider": "vertex_ai", "config": {"model": "gemini-embedding-2"}}}
    if memory_backend is not None:
        memory["backend"] = memory_backend
    with pytest.raises(ValidationError, match=r"the vertex_ai embedder does not serve the mem0 memory backend"):
        Config.model_validate({"models": {}, "agents": agents, "memory": memory})


def test_vertex_ai_embedder_config_accepts_agents_that_all_use_file_memory() -> None:
    """Agents whose memory is all file-backed, or all of it off with ``none``, never reach Mem0."""
    for backend in ("file", "none"):
        Config.model_validate(
            {
                "models": {},
                "agents": {"a": {"display_name": "A", "role": "r"}, "b": {"display_name": "B", "role": "r"}},
                "memory": {
                    "backend": backend,
                    "embedder": {"provider": "vertex_ai", "config": {"model": "gemini-embedding-2"}},
                },
            },
        )


def test_vertex_ai_prefixes_name_new_vectors() -> None:
    """Prefixes change every Vertex AI vector, so indexes built without them are not reused."""
    plain = effective_knowledge_embedder_signature("vertex_ai", "gemini-embedding-2")
    prefixed = effective_knowledge_embedder_signature("vertex_ai", "gemini-embedding-2", query_prefix="q: ")

    assert plain == ("vertex_ai", "gemini-embedding-2", "", "")
    assert prefixed != plain
