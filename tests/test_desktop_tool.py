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

    result = await tool.desktop("screenshot", app="com.example.Editor")

    assert json.loads(result.content)["status"] == "ok"
    assert result.images is not None
    assert result.images[0].content == b"\xff\xd8\xffjpeg"
    command = request.await_args.args[1]
    assert command.requester_id == "@alice:example.org"
    assert command.agent_name == "computer"
    assert command.parameters == {"app": "com.example.Editor"}


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

    result = await tool.desktop("click", app="com.example.Editor", state_id="state-1", x=10)

    assert json.loads(result.content)["status"] == "error"
    request.assert_not_awaited()

    result = await tool.desktop(
        "click",
        app="com.example.Editor",
        state_id="state-1",
        x=1001,
        y=20,
    )

    assert json.loads(result.content)["status"] == "error"
    request.assert_not_awaited()

    result = await tool.desktop(
        "keypress",
        app="com.example.Editor",
        state_id="state-1",
        keys=["command", "tab"],
    )

    assert json.loads(result.content)["status"] == "error"
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_value_can_clear_semantic_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty semantic value is valid and avoids a select-all shortcut fallback."""
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
            ok=False,
            error="Expected test rejection.",
        ),
    )
    monkeypatch.setattr("mindroom.custom_tools.desktop.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.desktop_response_router",
        lambda _client: SimpleNamespace(request=request),
    )
    tool = DesktopTools("@desktop:example.org", "DESKTOP", "fingerprint")

    await tool.desktop(
        "set_value",
        app="com.example.Editor",
        state_id="state-1",
        element_index=4,
        value="",
    )

    command = request.await_args.args[1]
    assert command.parameters["value"] == ""


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

    result = await tool.desktop(
        "click",
        app="com.example.Editor",
        state_id="state-1",
        x=10,
        y=20,
    )

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

    result = await tool.desktop(
        "click",
        app="com.example.Editor",
        state_id="state-1",
        x=10,
        y=20,
    )

    payload = json.loads(result.content)
    assert payload["status"] == "partial"
    assert "do not repeat" in payload["message"]
    assert result.images is None


@pytest.mark.asyncio
async def test_state_with_undecryptable_screenshot_remains_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    """A returned semantic tree is preserved when only its encrypted screenshot fails."""
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
            result={"state": {"state_id": "state-1"}},
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

    result = await tool.desktop("get_app_state", app="com.example.Editor")

    payload = json.loads(result.content)
    assert payload["status"] == "partial"
    assert payload["result"]["state"] == {"state_id": "state-1"}


@pytest.mark.asyncio
async def test_semantic_action_carries_state_scoped_element_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """The preferred action path identifies an element only within its exact app state."""
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
            result={"action": "click_element", "action_completed": True},
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
        AsyncMock(return_value=b"jpeg"),
    )
    tool = DesktopTools("@desktop:example.org", "DESKTOP", "fingerprint")

    result = await tool.desktop(
        "click_element",
        app="com.example.Editor",
        state_id="state-7",
        element_index=12,
    )

    assert json.loads(result.content)["status"] == "ok"
    command = request.await_args.args[1]
    assert command.parameters == {
        "app": "com.example.Editor",
        "state_id": "state-7",
        "element_index": 12,
    }


@pytest.mark.asyncio
async def test_list_apps_needs_no_screenshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allowed application discovery is a valid non-visual response."""
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
            result={"apps": [{"id": "com.example.Editor", "name": "Editor", "running": True}]},
        ),
    )
    monkeypatch.setattr("mindroom.custom_tools.desktop.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.desktop_response_router",
        lambda _client: SimpleNamespace(request=request),
    )
    tool = DesktopTools("@desktop:example.org", "DESKTOP", "fingerprint")

    result = await tool.desktop("list_apps")

    assert json.loads(result.content)["status"] == "ok"
    assert result.images is None
