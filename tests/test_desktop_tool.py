"""Tests for the cloud-side desktop agent tool."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import mindroom.tools  # noqa: F401
from mindroom.custom_tools.desktop import DesktopTools
from mindroom.desktop.media import DesktopMediaError
from mindroom.desktop.protocol import DesktopResponse, EncryptedDesktopMedia
from mindroom.tool_system.metadata import TOOL_METADATA

MEDIA = EncryptedDesktopMedia(
    url="mxc://example.org/screenshot",
    key="key",
    iv="iv",
    sha256="hash",
    mime_type="image/jpeg",
    size=7,
)


def test_desktop_tool_is_registered_as_room_scoped_primary_tool() -> None:
    """Desktop commands use the live agent Matrix device, not a detached worker."""
    metadata = TOOL_METADATA["desktop"]

    assert metadata.requires_room_context
    assert metadata.default_execution_target.value == "primary"
    assert metadata.function_names == ("desktop",)


@pytest.mark.asyncio
async def test_screenshot_response_becomes_model_visible_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent receives decrypted bytes plus structured source-screen dimensions."""
    client = object()
    context = SimpleNamespace(
        session_id="session-1",
        requester_id="@alice:example.org",
        agent_name="computer",
        client=client,
    )
    request = AsyncMock(
        return_value=DesktopResponse(
            request_id="request-1",
            session_id="session-1",
            ok=True,
            result={"screen": {"width": 1920, "height": 1080}},
            screenshot=MEDIA,
        ),
    )
    monkeypatch.setattr("mindroom.custom_tools.desktop.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.desktop_response_router",
        lambda _client: SimpleNamespace(request=request),
    )
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.download_encrypted_screenshot",
        AsyncMock(return_value=b"\xff\xd8\xffjpeg"),
    )
    tool = DesktopTools("@desktop:example.org", "DESKTOP", "fingerprint")

    result = await tool.desktop("screenshot")

    assert json.loads(result.content)["status"] == "ok"
    assert result.images is not None
    assert result.images[0].content == b"\xff\xd8\xffjpeg"
    command = request.await_args.args[1]
    assert command.requester_id == "@alice:example.org"
    assert command.agent_name == "computer"


@pytest.mark.asyncio
async def test_invalid_control_parameters_fail_before_matrix_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed actions never leave the cloud controller device."""
    request = AsyncMock()
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.get_tool_runtime_context",
        lambda: SimpleNamespace(
            session_id="session-1",
            requester_id="@alice:example.org",
            agent_name="computer",
            client=object(),
        ),
    )
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.desktop_response_router",
        lambda _client: SimpleNamespace(request=request),
    )
    tool = DesktopTools("@desktop:example.org", "DESKTOP", "fingerprint")

    result = await tool.desktop("click", x=10)

    assert json.loads(result.content)["status"] == "error"
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_control_without_screenshot_is_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    """The model is warned not to retry an action whose follow-up capture failed."""
    context = SimpleNamespace(
        session_id="session-1",
        requester_id="@alice:example.org",
        agent_name="computer",
        client=object(),
    )
    request = AsyncMock(
        return_value=DesktopResponse(
            request_id="request-1",
            session_id="session-1",
            ok=True,
            result={
                "action": "click",
                "action_completed": True,
                "warning": "Action completed; do not repeat it automatically.",
            },
        ),
    )
    monkeypatch.setattr("mindroom.custom_tools.desktop.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.desktop_response_router",
        lambda _client: SimpleNamespace(request=request),
    )
    tool = DesktopTools("@desktop:example.org", "DESKTOP", "fingerprint")

    result = await tool.desktop("click", x=10, y=20)

    payload = json.loads(result.content)
    assert payload["status"] == "partial"
    assert "do not repeat" in payload["message"]
    assert result.images is None


@pytest.mark.asyncio
async def test_completed_control_with_undecryptable_screenshot_is_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cloud media failure cannot make completed input look safe to retry."""
    context = SimpleNamespace(
        session_id="session-1",
        requester_id="@alice:example.org",
        agent_name="computer",
        client=object(),
    )
    request = AsyncMock(
        return_value=DesktopResponse(
            request_id="request-1",
            session_id="session-1",
            ok=True,
            result={"action": "click", "action_completed": True},
            screenshot=MEDIA,
        ),
    )
    monkeypatch.setattr("mindroom.custom_tools.desktop.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.desktop_response_router",
        lambda _client: SimpleNamespace(request=request),
    )
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.download_encrypted_screenshot",
        AsyncMock(side_effect=DesktopMediaError("Decryption failed.")),
    )
    tool = DesktopTools("@desktop:example.org", "DESKTOP", "fingerprint")

    result = await tool.desktop("click", x=10, y=20)

    payload = json.loads(result.content)
    assert payload["status"] == "partial"
    assert "do not repeat" in payload["message"]
    assert result.images is None
