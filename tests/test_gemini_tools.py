"""Tests for the native Gemini media toolkit."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import httpx
import pytest
from google.genai import types

import mindroom.tools  # noqa: F401
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.credentials.manager import CredentialsManager
from mindroom.custom_tools import gemini_media
from mindroom.custom_tools.gemini_media import GeminiTools
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.tool_function import ToolValue
from tests.agent_tool_helpers import toolkit_offers

if TYPE_CHECKING:
    from pathlib import Path


def test_generate_image_uses_gemini_content_generation(monkeypatch) -> None:  # noqa: ANN001
    """Nano Banana 2 should use generate_content while preserving generate_image."""
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model",
                    parts=[types.Part(inline_data=types.Blob(data=b"image-bytes", mime_type="image/png"))],
                ),
            ),
        ],
    )
    generate_content = Mock(return_value=response)
    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    monkeypatch.setattr(gemini_media, "Client", lambda **_kwargs: client)
    toolkit = GeminiTools(
        api_key="test-key",
        image_generation_model="gemini-3.1-flash-image",
        enable_generate_video=False,
    )

    result = toolkit.generate_image("A geometric Matrix room")

    assert isinstance(result, ToolValue)
    assert len(result.images) == 1
    assert result.images[0].content == b"image-bytes"
    kwargs = generate_content.call_args.kwargs
    assert kwargs["model"] == "gemini-3.1-flash-image"
    assert kwargs["contents"] == "A geometric Matrix room"
    assert kwargs["config"].response_modalities == ["IMAGE"]


def test_generate_image_offers_the_model_its_prompt(monkeypatch) -> None:  # noqa: ANN001
    """The schema asks only for the prompt with no injected framework parameter."""
    monkeypatch.setattr(gemini_media, "Client", lambda **_kwargs: SimpleNamespace())
    toolkit = GeminiTools(api_key="test-key", enable_generate_video=False)

    function = toolkit_offers(toolkit, "generate_image")

    assert function.description == "Generate an image through the Gemini content-generation API."
    assert set(function.parameters["properties"]) == {"prompt"}
    assert function.parameters["required"] == ["prompt"]


def test_generate_image_keeps_the_gemini_api_on_v1beta(monkeypatch) -> None:  # noqa: ANN001
    """google-genai 3 defaults the Gemini API to v1, which lacks preview image models, so image requests stay on v1beta."""
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        part = {"inlineData": {"data": base64.b64encode(b"image-bytes").decode(), "mimeType": "image/png"}}
        return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [part]}}]})

    real_client = gemini_media.Client
    http_client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(
        gemini_media,
        "Client",
        lambda **kwargs: real_client(**kwargs, http_options=types.HttpOptions(httpx_client=http_client)),
    )
    toolkit = GeminiTools(api_key="test-key", enable_generate_video=False)

    result = toolkit.generate_image("A geometric Matrix room")

    assert len(result.images) == 1
    assert result.images[0].content == b"image-bytes"
    assert [request.url.path for request in sent] == [f"/v1beta/models/{toolkit.image_model}:generateContent"]


@pytest.mark.parametrize("env_value", ["true", "1"])
def test_gemini_vertexai_env_only_without_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_value: str,
) -> None:
    """GOOGLE_GENAI_USE_VERTEXAI in runtime env enables Vertex AI mode without an API key."""
    captured: list[dict[str, object]] = []

    def fake_client(**kwargs: object) -> SimpleNamespace:
        captured.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(gemini_media, "Client", fake_client)
    (tmp_path / "config.yaml").write_text("agents: {}\n")
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_NO_AUTO_INSTALL_TOOLS": "1",
            "GOOGLE_GENAI_USE_VERTEXAI": env_value,
            "GOOGLE_CLOUD_PROJECT": "demo-project",
            "GOOGLE_CLOUD_LOCATION": "global",
        },
    )

    toolkit = get_tool_by_name(
        "gemini",
        runtime_paths,
        tools=builtin_tool_registry(),
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        runs_in_worker=False,
        worker_target=None,
    )

    assert isinstance(toolkit, GeminiTools)
    assert toolkit.vertexai is True
    assert toolkit.api_key is None
    assert captured == [{"vertexai": True, "project": "demo-project", "location": "global"}]


def test_gemini_explicit_vertexai_false_overrides_env_and_requires_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit vertexai=False setting overrides GOOGLE_GENAI_USE_VERTEXAI and fails closed without an API key."""
    monkeypatch.setattr(gemini_media, "Client", Mock(side_effect=AssertionError("Client should not be built")))
    (tmp_path / "config.yaml").write_text("agents: {}\n")
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_NO_AUTO_INSTALL_TOOLS": "1",
            "GOOGLE_GENAI_USE_VERTEXAI": "true",
        },
    )

    with pytest.raises(ValueError, match="GOOGLE_API_KEY not set"):
        get_tool_by_name(
            "gemini",
            runtime_paths,
            tools=builtin_tool_registry(),
            credentials_manager=CredentialsManager(tmp_path / "credentials"),
            tool_config_overrides={"vertexai": False},
            runs_in_worker=False,
            worker_target=None,
        )


def test_gemini_explicit_vertexai_false_routes_to_gemini_api_despite_process_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit vertexai=False must pass vertexai=False to genai.Client so requests hit Gemini API, not Vertex."""
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        part = {"inlineData": {"data": base64.b64encode(b"image-bytes").decode(), "mimeType": "image/png"}}
        return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [part]}}]})

    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    real_client = gemini_media.Client
    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        monkeypatch.setattr(
            gemini_media,
            "Client",
            lambda **kwargs: real_client(**kwargs, http_options=types.HttpOptions(httpx_client=http_client)),
        )
        (tmp_path / "config.yaml").write_text("agents: {}\n")
        runtime_paths = resolve_primary_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "storage",
            process_env={
                "MINDROOM_NO_AUTO_INSTALL_TOOLS": "1",
                "GOOGLE_GENAI_USE_VERTEXAI": "true",
                "GOOGLE_CLOUD_PROJECT": "demo-project",
                "GOOGLE_CLOUD_LOCATION": "us-central1",
                "GOOGLE_API_KEY": "test-key",
            },
        )

        toolkit = get_tool_by_name(
            "gemini",
            runtime_paths,
            tools=builtin_tool_registry(),
            credentials_manager=CredentialsManager(tmp_path / "credentials"),
            tool_config_overrides={"vertexai": False},
            runs_in_worker=False,
            worker_target=None,
        )

        assert isinstance(toolkit, GeminiTools)
        assert toolkit.vertexai is False
        assert toolkit.client._api_client.vertexai is False
        result = toolkit.generate_image("A geometric Matrix room")

    assert len(result.images) == 1
    assert result.images[0].content == b"image-bytes"
    assert [request.url.host for request in sent] == ["generativelanguage.googleapis.com"]
    assert [request.url.path for request in sent] == [f"/v1beta/models/{toolkit.image_model}:generateContent"]
