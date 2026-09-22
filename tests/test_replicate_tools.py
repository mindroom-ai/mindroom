"""Replicate credential handoff and media contracts through the real SDK transport."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import httpx
import pytest
import replicate
import replicate.client as replicate_client
from agno.agent import Agent
from agno.tools.function import ToolResult

import mindroom.tools.replicate  # noqa: F401  # register the real tool factory
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import CredentialsManager
from mindroom.tool_system.metadata import get_tool_by_name

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from agno.tools.replicate import ReplicateTools

_MODEL = "test-owner/media"
_IMAGE = "https://media.example.org/result.png"
_VIDEO = "https://media.example.org/result.mp4"


@dataclass
class _ReplicateTransport:
    requests: list[httpx.Request] = field(default_factory=list)
    clients: list[httpx.Client] = field(default_factory=list)
    output: object = _IMAGE
    status_code: int = 201
    prediction_status: str = "succeeded"
    prediction_error: str | None = None

    def respond(self, request: httpx.Request) -> httpx.Response:
        """Provide complete prediction records while retaining real SDK request handling."""
        self.requests.append(request)
        if request.method == "GET":
            assert request.url.path == "/v1/models/test-owner/media/versions/test-version"
            return httpx.Response(
                200,
                json={
                    "id": "test-version",
                    "created_at": "2026-09-01T00:00:00Z",
                    "cog_version": "0.16.0",
                    "openapi_schema": {
                        "components": {"schemas": {"Output": {"type": "array", "x-cog-array-type": "iterator"}}},
                    },
                },
            )
        assert request.method == "POST"
        assert request.url.path in {"/v1/models/test-owner/media/predictions", "/v1/predictions"}
        assert json.loads(request.content)["input"] == {"prompt": "A red fox"}
        if self.status_code != 201:
            return httpx.Response(self.status_code, json={"detail": "Synthetic provider refusal"})
        return httpx.Response(
            201,
            json={
                "id": "test-prediction",
                "model": _MODEL,
                "version": "test-version",
                "status": self.prediction_status,
                "input": {"prompt": "A red fox"},
                "output": self.output,
                "logs": "",
                "error": self.prediction_error,
                "metrics": {"predict_time": 0.01},
                "created_at": "2026-09-01T00:00:00Z",
                "started_at": "2026-09-01T00:00:00Z",
                "completed_at": "2026-09-01T00:00:01Z",
                "urls": {
                    "get": "https://api.replicate.com/v1/predictions/test-prediction",
                    "cancel": "https://api.replicate.com/v1/predictions/test-prediction/cancel",
                },
            },
        )


@pytest.fixture
def replicate_transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[_ReplicateTransport]:
    """Replace only SDK transport construction and isolate its module-level client."""
    for name in ("REPLICATE_API_KEY", "REPLICATE_API_TOKEN", "REPLICATE_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    transport = _ReplicateTransport()
    build_httpx_client = replicate_client._build_httpx_client

    def build_client(
        client_type: type[httpx.Client | httpx.AsyncClient],
        api_token: str | None,
        base_url: str | None,
        timeout: httpx.Timeout | None,
        **kwargs: object,
    ) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(transport.respond)
        kwargs["trust_env"] = False
        client = build_httpx_client(client_type, api_token, base_url, timeout, **kwargs)
        assert isinstance(client, httpx.Client)
        transport.clients.append(client)
        return client

    monkeypatch.setattr(replicate_client, "_build_httpx_client", build_client)
    # Keep the real SDK run method, with no transport or credential state from another test.
    monkeypatch.setattr(replicate, "run", replicate.Client().run)
    try:
        yield transport
    finally:
        for client in transport.clients:
            client.close()


def _tool(tmp_path: Path, stored_key: str | None = None, *, model: str = _MODEL) -> ReplicateTools:
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_NO_AUTO_INSTALL_TOOLS": "1"},
    )
    credentials = CredentialsManager(tmp_path / "credentials")
    if stored_key is not None:
        credentials.save_credentials("replicate", {"api_key": stored_key})
    return cast(
        "ReplicateTools",
        get_tool_by_name(
            "replicate",
            runtime_paths,
            credentials_manager=credentials,
            tool_config_overrides={"model": model},
            worker_target=None,
            disable_sandbox_proxy=True,
        ),
    )


@pytest.mark.parametrize(
    ("stored_key", "env_key", "sdk_token", "expected_authorization"),
    [
        pytest.param("stored-key", None, None, "Bearer stored-key", id="stored-only"),
        pytest.param(None, "env-key", None, "Bearer env-key", id="documented-env-only"),
        pytest.param("stored-key", "env-key", "sdk-token", "Bearer stored-key", id="stored-wins-both-env-values"),
        pytest.param(None, "env-key", "sdk-token", "Bearer env-key", id="documented-env-wins-sdk-token"),
        pytest.param(None, "matching-key", "matching-key", "Bearer matching-key", id="matching-env-control"),
    ],
)
def test_replicate_request_uses_resolved_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replicate_transport: _ReplicateTransport,
    stored_key: str | None,
    env_key: str | None,
    sdk_token: str | None,
    expected_authorization: str,
) -> None:
    """The key selected by MindRoom and Agno must reach the SDK Authorization header."""
    if env_key is not None:
        monkeypatch.setenv("REPLICATE_API_KEY", env_key)
    if sdk_token is not None:
        monkeypatch.setenv("REPLICATE_API_TOKEN", sdk_token)
    tool = _tool(tmp_path, stored_key)

    result = tool.generate_media(agent=Agent(), prompt="A red fox")

    assert result.images is not None
    assert [image.url for image in result.images] == [_IMAGE]
    assert len(replicate_transport.requests) == 1
    assert replicate_transport.requests[0].headers.get("Authorization") == expected_authorization


@pytest.mark.parametrize("sdk_token", [None, "sdk-token"], ids=["no-key", "sdk-token-only"])
def test_missing_replicate_key_does_not_make_a_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replicate_transport: _ReplicateTransport,
    sdk_token: str | None,
) -> None:
    """An ambient SDK token must not bypass the toolkit's configured-key guard."""
    if sdk_token is not None:
        monkeypatch.setenv("REPLICATE_API_TOKEN", sdk_token)

    result = _tool(tmp_path).generate_media(agent=Agent(), prompt="A red fox")

    assert result.content == "API key is not set."
    assert result.images is None
    assert result.videos is None
    assert replicate_transport.requests == []
    assert replicate_transport.clients == []


@pytest.mark.parametrize(
    ("output", "expected_content", "image_urls", "video_urls"),
    [
        pytest.param(_IMAGE, f"Image generated successfully at {_IMAGE}", [_IMAGE], [], id="single-image"),
        pytest.param(_VIDEO, f"Video generated successfully at {_VIDEO}", [], [_VIDEO], id="single-video"),
        pytest.param(
            [_IMAGE, _VIDEO],
            f"Image generated successfully at {_IMAGE}\nVideo generated successfully at {_VIDEO}",
            [_IMAGE],
            [_VIDEO],
            id="mixed-media-list",
        ),
        pytest.param([], "", [], [], id="empty-list"),
        pytest.param("not-media", "Unexpected output type: <class 'str'>", [], [], id="unexpected-output"),
        pytest.param([_IMAGE, "not-media"], "Unexpected output type: <class 'str'>", [], [], id="unexpected-list-item"),
        pytest.param(
            "https://media.example.org/result.bin",
            "Error: Unsupported media type with extension '.bin'.",
            [],
            [],
            id="unsupported-extension",
        ),
    ],
)
def test_replicate_retains_upstream_media_output_contract(
    tmp_path: Path,
    replicate_transport: _ReplicateTransport,
    output: object,
    expected_content: str,
    image_urls: list[str],
    video_urls: list[str],
) -> None:
    """Credential routing must retain the established FileOutput and error result shapes."""
    replicate_transport.output = output

    result = _tool(tmp_path, "stored-key").generate_media(agent=Agent(), prompt="A red fox")

    assert result.content == expected_content
    assert [image.url for image in result.images or []] == image_urls
    assert [video.url for video in result.videos or []] == video_urls
    assert bool(result.images) == bool(image_urls)
    assert bool(result.videos) == bool(video_urls)


@pytest.mark.parametrize("failure", ["http-error", "model-error"])
def test_replicate_provider_failures_remain_tool_results(
    tmp_path: Path,
    replicate_transport: _ReplicateTransport,
    failure: str,
) -> None:
    """Both HTTP and model failures remain error ToolResults without attachments."""
    if failure == "http-error":
        replicate_transport.status_code = 401
    else:
        replicate_transport.prediction_status = "failed"
        replicate_transport.prediction_error = "Synthetic provider refusal"

    result = _tool(tmp_path, "stored-key").generate_media(agent=Agent(), prompt="A red fox")

    assert isinstance(result, ToolResult)
    assert result.content.startswith("Error:")
    assert "Synthetic provider refusal" in result.content
    assert result.images is None
    assert result.videos is None


@pytest.mark.parametrize("outcome", ["success", "http-error", "parse-error"])
def test_replicate_closes_owned_request_client(
    tmp_path: Path,
    replicate_transport: _ReplicateTransport,
    outcome: str,
) -> None:
    """The per-call HTTP client is closed after successful, failed, or unparseable output."""
    if outcome == "http-error":
        replicate_transport.status_code = 401
    elif outcome == "parse-error":
        replicate_transport.output = "https://media.example.org/result.bin"

    _tool(tmp_path, "stored-key").generate_media(agent=Agent(), prompt="A red fox")

    assert len(replicate_transport.clients) == 1
    assert replicate_transport.clients[0].is_closed


def test_separate_replicate_toolkits_keep_their_own_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replicate_transport: _ReplicateTransport,
) -> None:
    """One toolkit's resolved key must not be replaced by another toolkit or the SDK default."""
    monkeypatch.setenv("REPLICATE_API_TOKEN", "ambient-token")
    first = _tool(tmp_path / "first", "first-key")
    second = _tool(tmp_path / "second", "second-key")

    for tool in (first, second, first):
        tool.generate_media(agent=Agent(), prompt="A red fox")

    assert [request.headers.get("Authorization") for request in replicate_transport.requests] == [
        "Bearer first-key",
        "Bearer second-key",
        "Bearer first-key",
    ]


def test_replicate_materializes_sdk_iterator_outputs(
    tmp_path: Path,
    replicate_transport: _ReplicateTransport,
) -> None:
    """SDK iterator outputs remain usable until all image and video artifacts are parsed."""
    replicate_transport.output = [_IMAGE, _VIDEO]

    result = _tool(tmp_path, "stored-key", model=f"{_MODEL}:test-version").generate_media(
        agent=Agent(),
        prompt="A red fox",
    )

    assert result.images is not None
    assert result.videos is not None
    assert [image.url for image in result.images] == [_IMAGE]
    assert [video.url for video in result.videos] == [_VIDEO]
    assert [request.method for request in replicate_transport.requests] == ["POST", "GET"]


def test_registered_replicate_function_retains_framework_agent_parameter(
    tmp_path: Path,
    replicate_transport: _ReplicateTransport,
) -> None:
    """Agno can inspect and invoke the registered adapter without exposing its injected agent."""
    function = _tool(tmp_path, "stored-key").functions["generate_media"]
    function.process_entrypoint()
    assert set(function.parameters["properties"]) == {"prompt"}
    assert function.entrypoint is not None

    result = function.entrypoint(agent=Agent(), prompt="A red fox")

    assert isinstance(result, ToolResult)
    assert result.images is not None
    assert [image.url for image in result.images] == [_IMAGE]
    assert len(replicate_transport.requests) == 1
