"""Exercise the registered Luma toolkit through the installed SDK HTTP boundary."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import agno.tools.lumalab as agno_lumalab
import httpx
import pytest
from agno.agent import Agent

import mindroom.tools  # noqa: F401
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials import CredentialsManager
from mindroom.tool_system.metadata import get_tool_by_name

if TYPE_CHECKING:
    from pathlib import Path

    from agno.tools.function import ToolResult


_VIDEO_URL = "https://example.com/generated.mp4"
_KEYFRAMES = {
    "frame0": {"type": "image", "url": "https://example.com/start.png"},
    "frame1": {"type": "image", "url": "https://example.com/end.png"},
}
_METHODS = ["generate_video", "image_to_video"]


@dataclass
class _LumaHTTP:
    """Queued HTTP replies and observed requests for a single test."""

    replies: deque[tuple[int, dict[str, object]]] = field(default_factory=deque)
    requests: list[httpx.Request] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)


@pytest.fixture
def luma_http(monkeypatch: pytest.MonkeyPatch) -> _LumaHTTP:
    """Intercept transport only, retaining the real SDK signature and serialization."""
    mocked = _LumaHTTP()
    monkeypatch.delenv("LUMAAI_BASE_URL", raising=False)
    monkeypatch.delenv("LUMAAI_API_KEY", raising=False)
    monkeypatch.setattr(agno_lumalab, "time", SimpleNamespace(sleep=mocked.sleeps.append))

    def handle_request(_transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        mocked.requests.append(request)
        assert mocked.replies, f"Unexpected request: {request.method} {request.url.path}"
        status, payload = mocked.replies.popleft()
        return httpx.Response(status, json=payload, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    return mocked


def _tool(tmp_path: Path, **overrides: object) -> agno_lumalab.LumaLabTools:
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "state",
        process_env={"MINDROOM_NO_AUTO_INSTALL_TOOLS": "1"},
    )
    return cast(
        "agno_lumalab.LumaLabTools",
        get_tool_by_name(
            "lumalabs",
            paths,
            worker_target=None,
            disable_sandbox_proxy=True,
            credentials_manager=CredentialsManager(tmp_path / "credentials"),
            credential_overrides={"api_key": "test-luma-key"},
            tool_config_overrides=overrides,
        ),
    )


def _invoke(tool: agno_lumalab.LumaLabTools, method: str) -> ToolResult:
    if method == "generate_video":
        return tool.generate_video(
            agent=Agent(),
            prompt="A calm lake.",
            loop=True,
            aspect_ratio="9:16",
            keyframes=_KEYFRAMES,
        )
    return tool.image_to_video(
        agent=Agent(),
        prompt="A calm lake.",
        start_image_url=_KEYFRAMES["frame0"]["url"],
        end_image_url=_KEYFRAMES["frame1"]["url"],
        loop=True,
        aspect_ratio="9:16",
    )


@pytest.mark.parametrize("method", _METHODS)
@pytest.mark.parametrize(
    ("overrides", "expected_model"),
    [pytest.param({}, "ray-2", id="default"), pytest.param({"model": "ray-flash-2"}, "ray-flash-2", id="configured")],
)
def test_lumalabs_generation_sends_model_and_returns_video(
    tmp_path: Path,
    luma_http: _LumaHTTP,
    method: str,
    overrides: dict[str, object],
    expected_model: str,
) -> None:
    """Both public methods must reach the real SDK and retain returned media."""
    luma_http.replies.extend(
        [
            (201, {"id": "generation-id", "state": "queued"}),
            (200, {"id": "generation-id", "state": "completed", "assets": {"video": _VIDEO_URL}}),
        ],
    )
    tool = _tool(tmp_path, **overrides)

    result = _invoke(tool, method)

    assert result.content == f"Video generated successfully: {_VIDEO_URL}"
    assert result.videos is not None
    assert len(result.videos) == 1
    assert result.videos[0].url == _VIDEO_URL
    assert result.videos[0].id
    assert set(tool.functions) == set(_METHODS)
    assert [(request.method, request.url.path) for request in luma_http.requests] == [
        ("POST", "/dream-machine/v1/generations/video"),
        ("GET", "/dream-machine/v1/generations/generation-id"),
    ]
    assert json.loads(luma_http.requests[0].content) == {
        "model": expected_model,
        "prompt": "A calm lake.",
        "loop": True,
        "aspect_ratio": "9:16",
        "keyframes": _KEYFRAMES,
    }
    assert not luma_http.replies
    assert not luma_http.sleeps


@pytest.mark.parametrize("method", _METHODS)
@pytest.mark.parametrize(
    ("create_body", "poll_bodies", "overrides", "expected_content", "expected_sleeps"),
    [
        pytest.param(
            {"id": "generation-id"},
            [],
            {"wait_for_completion": False},
            "Async generation unsupported",
            [],
            id="no-wait",
        ),
        pytest.param(
            {"id": "generation-id"},
            [{"state": "failed", "failure_reason": "provider rejected request"}],
            {},
            "Generation failed: provider rejected request",
            [],
            id="failed",
        ),
        pytest.param(
            {"id": "generation-id"},
            [{"id": "generation-id", "state": "dreaming"}, {"id": "generation-id", "state": "dreaming"}],
            {"max_wait_time": 2, "poll_interval": 1},
            "Video generation timed out after 2 seconds",
            [1, 1],
            id="timeout",
        ),
        pytest.param({}, [], {}, "Failed to get generation ID", [], id="missing-id"),
    ],
)
def test_lumalabs_preserves_completion_controls(
    tmp_path: Path,
    luma_http: _LumaHTTP,
    method: str,
    create_body: dict[str, object],
    poll_bodies: list[dict[str, object]],
    overrides: dict[str, object],
    expected_content: str,
    expected_sleeps: list[int],
) -> None:
    """Completion settings and provider failure outcomes must remain observable."""
    luma_http.replies.append((201, create_body))
    luma_http.replies.extend((200, body) for body in poll_bodies)
    tool = _tool(tmp_path, **overrides)

    result = _invoke(tool, method)

    assert result.content == expected_content
    assert not result.videos
    assert [request.method for request in luma_http.requests] == ["POST", *(["GET"] * len(poll_bodies))]
    assert json.loads(luma_http.requests[0].content)["model"] == "ray-2"
    assert not luma_http.replies
    assert luma_http.sleeps == expected_sleeps


@pytest.mark.parametrize("method", _METHODS)
def test_lumalabs_preserves_sdk_http_errors(
    tmp_path: Path,
    luma_http: _LumaHTTP,
    method: str,
) -> None:
    """SDK HTTP failures must still return an error without an attachment or polling."""
    luma_http.replies.append((400, {"detail": "synthetic generation rejection"}))

    result = _invoke(_tool(tmp_path), method)

    assert result.content.startswith("Error:")
    assert "synthetic generation rejection" in result.content
    assert not result.videos
    assert [request.method for request in luma_http.requests] == ["POST"]
    assert not luma_http.replies


@pytest.mark.parametrize(
    ("overrides", "expected_functions"),
    [
        ({"enable_generate_video": False}, {"image_to_video"}),
        ({"enable_image_to_video": False}, {"generate_video"}),
        ({"enable_generate_video": False, "enable_image_to_video": False}, set()),
        ({"enable_generate_video": False, "enable_image_to_video": False, "all": True}, set(_METHODS)),
    ],
)
def test_lumalabs_preserves_registration_flags(
    tmp_path: Path,
    luma_http: _LumaHTTP,
    overrides: dict[str, object],
    expected_functions: set[str],
) -> None:
    """Existing enable flags and all must retain their registration behavior."""
    tool = _tool(tmp_path, **overrides)

    assert set(tool.functions) == expected_functions
    assert not luma_http.requests
