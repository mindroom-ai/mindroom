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
from mindroom.embeddings.embedder_health import probe_embedder
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
    from pathlib import Path

_MODEL_PATH = "/v1beta1/projects/test-project/locations/global/publishers/google/models/gemini-embedding-2:embedContent"

_Reply = list[float] | httpx.Response


def _client(replies: list[_Reply], requests: list[httpx.Request]) -> genai.Client:
    """Return a Vertex AI client whose server answers each request with the next reply, repeating the last one."""

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        reply = replies[min(len(requests), len(replies)) - 1]
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json={"embedding": {"values": reply}})

    transport = httpx.MockTransport(handle)
    return genai.Client(
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


def _embedder(replies: list[_Reply], requests: list[httpx.Request], **settings: object) -> VertexAIEmbedder:
    return VertexAIEmbedder(model="gemini-embedding-2", client=_client(replies, requests), **settings)


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
    """A reply without a vector raises instead of handing an empty vector to the index."""
    embedder = _embedder([[]], [])

    with pytest.raises(EmbedderRequestError, match=EMBEDDER_EMPTY_VECTOR_DETAIL):
        embedder.embed(["text"], purpose="document")


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
    assert "secret-bearing" not in str(excinfo.value)
    assert str(status_code) in str(excinfo.value)
    assert embedder_credentials_rejected(excinfo.value) is credentials
    assert embedder_failure_is_transient(excinfo.value) is transient


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
    config = bind_runtime_paths(
        Config.model_validate(
            {
                "models": {},
                "memory": {
                    "embedder": {"provider": "vertex_ai", "config": {"model": "gemini-embedding-2", "dimensions": 768}},
                },
            },
        ),
        runtime_paths,
    )
    built: list[dict[str, object]] = []
    server = _client([[0.5] * 768], [])

    def record_client(**kwargs: object) -> genai.Client:
        built.append(kwargs)
        return server

    monkeypatch.setattr(vertex_ai_embedder_module.genai, "Client", record_client)
    embedder = create_configured_embedder(config, runtime_paths, caller_retries=True)

    assert isinstance(embedder, VertexAIEmbedder)
    assert embedder.embed(["text"], purpose="document") == [[0.5] * 768]
    assert len(built) == 1
    assert built[0]["vertexai"] is True
    assert built[0]["project"] == "env-project"
    assert built[0]["location"] == "us-west1"
    # A caller that retries itself gets one attempt per request, as with the OpenAI embedder.
    assert built[0]["http_options"] == {"retry_options": {"attempts": 1}}


def test_vertex_ai_embedder_config_accepts_only_the_fields_it_reads() -> None:
    """Config load rejects a host or key for Vertex AI, which authenticates with Google Cloud credentials."""
    Config.model_validate(
        {
            "models": {},
            "memory": {
                "embedder": {
                    "provider": "vertex_ai",
                    "config": {"model": "gemini-embedding-2", "dimensions": 768, "query_prefix": "q: "},
                },
            },
        },
    )
    with pytest.raises(ValidationError, match=r"memory\.embedder\.config\.host does not apply to the vertex_ai"):
        Config.model_validate(
            {
                "models": {},
                "memory": {"embedder": {"provider": "vertex_ai", "config": {"model": "m", "host": "http://x"}}},
            },
        )


def test_vertex_ai_prefixes_name_new_vectors() -> None:
    """Prefixes change every Vertex AI vector, so indexes built without them are not reused."""
    plain = effective_knowledge_embedder_signature("vertex_ai", "gemini-embedding-2")
    prefixed = effective_knowledge_embedder_signature("vertex_ai", "gemini-embedding-2", query_prefix="q: ")

    assert plain == ("vertex_ai", "gemini-embedding-2", "", "")
    assert prefixed != plain


def test_probe_bounds_the_vertex_ai_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A health probe gives Vertex AI one attempt of 10 seconds, so a stalled endpoint fails in seconds, not minutes."""
    built: list[dict[str, object]] = []
    server = _client([[0.5] * 8], [])

    def record_client(**kwargs: object) -> genai.Client:
        built.append(kwargs)
        return server

    monkeypatch.setattr(vertex_ai_embedder_module.genai, "Client", record_client)
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config.model_validate(
            {
                "models": {},
                "memory": {"embedder": {"provider": "vertex_ai", "config": {"model": "gemini-embedding-2"}}},
            },
        ),
        runtime_paths,
    )

    assert probe_embedder(config, runtime_paths) is None
    assert built[0]["http_options"] == {"timeout": 10_000, "retry_options": {"attempts": 1}}
