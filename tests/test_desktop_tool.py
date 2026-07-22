"""Tests for the cloud-side desktop agent tool."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

import mindroom.tools  # noqa: F401
from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.credentials import CredentialsManager
from mindroom.custom_tools.desktop import DesktopTools
from mindroom.desktop.configuration import DesktopConfigurationStatus, desktop_configuration_state
from mindroom.desktop.media import DesktopMediaError
from mindroom.desktop.protocol import DesktopResponse, EncryptedDesktopMedia
from mindroom.tool_system.metadata import TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity, WorkerScope
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

MEDIA = EncryptedDesktopMedia(
    url="mxc://example.org/screenshot",
    key="key",
    iv="iv",
    sha256="hash",
    mime_type="image/jpeg",
    size=7,
)


def _desktop_target(
    *,
    worker_scope: WorkerScope | None = None,
    requester_id: str = "@alice:example.org",
) -> ResolvedWorkerTarget:
    return ResolvedWorkerTarget(
        worker_scope=worker_scope,
        routing_agent_name="computer",
        execution_identity=ToolExecutionIdentity(
            channel="matrix",
            agent_name="computer",
            requester_id=requester_id,
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id="session",
        ),
        tenant_id=None,
        account_id=None,
        worker_key=None,
    )


def _configured_tool(monkeypatch: pytest.MonkeyPatch) -> DesktopTools:
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.load_desktop_credentials",
        lambda *_args, **_kwargs: {
            "device_user_id": "@desktop:example.org",
            "device_id": "DESKTOP",
            "device_ed25519": "fingerprint",
        },
    )
    return DesktopTools(
        credentials_manager=MagicMock(spec=CredentialsManager),
        worker_target=_desktop_target(),
    )


def test_desktop_tool_is_registered_as_room_scoped_primary_tool() -> None:
    """Desktop commands use the live agent Matrix device, not a detached worker."""
    metadata = TOOL_METADATA["desktop"]

    assert metadata.requires_room_context
    assert metadata.default_execution_target.value == "primary"
    assert metadata.function_names == ("desktop",)
    assert [field.name for field in metadata.config_fields or ()] == ["timeout_seconds"]


@pytest.mark.parametrize("private", [None, {"per": "user_agent"}])
def test_desktop_supports_normal_and_private_agents(tmp_path: Path, private: dict[str, str] | None) -> None:
    """Desktop identity isolation does not require private agent execution."""
    agent: dict[str, object] = {
        "display_name": "Computer",
        "role": "Operate local apps",
        "tools": ["desktop"],
    }
    if private is not None:
        agent["private"] = private
    config = Config.validate_with_runtime(
        {
            "defaults": {"tools": []},
            "agents": {"computer": agent},
        },
        test_runtime_paths(tmp_path),
    )

    assert "desktop" in config.resolve_entity("computer").available_tools


def test_desktop_rejects_authored_device_identity(tmp_path: Path) -> None:
    """Desktop device identity comes only from requester-agent pairing."""
    with pytest.raises(ConfigRuntimeValidationError, match=r"desktop\.device_user_id"):
        Config.validate_with_runtime(
            {
                "defaults": {"tools": []},
                "agents": {
                    "computer": {
                        "display_name": "Computer",
                        "role": "Operate local apps",
                        "private": {"per": "user_agent"},
                        "tools": [{"desktop": {"device_user_id": "@desktop:example.org"}}],
                    },
                },
            },
            test_runtime_paths(tmp_path),
        )


@pytest.mark.asyncio
async def test_unconfigured_normal_desktop_tool_advertises_chat_pairing(tmp_path: Path) -> None:
    """A normal agent can pair a requester-owned Desktop from its direct chat."""
    tool = get_tool_by_name(
        "desktop",
        test_runtime_paths(tmp_path),
        disable_sandbox_proxy=True,
        worker_target=_desktop_target(),
    )

    result = await tool.desktop("status")  # type: ignore[attr-defined]

    payload = json.loads(result.content)
    assert payload["status"] == "setup_required"
    assert "!desktop setup" in payload["message"]


@pytest.mark.asyncio
async def test_unconfigured_private_user_agent_tool_advertises_chat_pairing() -> None:
    """Private requester-agent execution uses the same chat pairing flow."""
    tool = DesktopTools(worker_target=_desktop_target(worker_scope="user_agent"))

    result = await tool.desktop("status")

    assert "!desktop setup" in json.loads(result.content)["message"]


@pytest.mark.asyncio
async def test_live_requester_and_tool_owner_select_desktop_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reused or team-embedded toolkit follows the live requester and its owning agent."""
    loaded_scopes: list[tuple[str, str]] = []

    def load_credentials(
        _manager: CredentialsManager,
        *,
        requester_id: str,
        agent_name: str,
    ) -> dict[str, str]:
        loaded_scopes.append((requester_id, agent_name))
        return {
            "device_user_id": "@bob-desktop:example.org",
            "device_id": "BOB",
            "device_ed25519": "bob-fingerprint",
        }

    context = SimpleNamespace(
        requester_id="@bob:example.org",
        agent_name="configured_team",
        client=object(),
    )
    request = AsyncMock(
        return_value=DesktopResponse(
            request_id="request-1",
            session_id="channel",
            ok=True,
            result={"online": True},
        ),
    )
    monkeypatch.setattr("mindroom.custom_tools.desktop.load_desktop_credentials", load_credentials)
    monkeypatch.setattr("mindroom.custom_tools.desktop.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.desktop_response_router",
        lambda _client: SimpleNamespace(request=request),
    )
    tool = DesktopTools(
        credentials_manager=MagicMock(spec=CredentialsManager),
        worker_target=_desktop_target(requester_id="@alice:example.org"),
    )

    await tool.desktop("status")

    assert loaded_scopes == [("@bob:example.org", "computer")]
    assert request.await_args.args[0].user_id == "@bob-desktop:example.org"


def test_desktop_configuration_distinguishes_partial_and_invalid_state() -> None:
    """Partial and malformed scoped records fail closed with actionable state."""
    partial = desktop_configuration_state({"device_user_id": "@desktop:example.org"})
    invalid = desktop_configuration_state(
        {
            "device_user_id": "not-a-matrix-id",
            "device_id": "DEVICE",
            "device_ed25519": "fingerprint",
        },
    )

    assert partial.status is DesktopConfigurationStatus.SETUP_REQUIRED
    assert partial.missing_fields == ("device_ed25519", "device_id")
    assert invalid.status is DesktopConfigurationStatus.INVALID
    assert "@user:server" in (invalid.error or "")


def test_unconfigured_desktop_preserves_authored_timeout_for_scoped_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requester credentials inherit authored timeout even before identity exists."""
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.load_desktop_credentials",
        lambda *_args, **_kwargs: {
            "device_user_id": "@desktop:example.org",
            "device_id": "DEVICE",
            "device_ed25519": "fingerprint",
        },
    )
    tool = DesktopTools(
        timeout_seconds=90,
        credentials_manager=MagicMock(spec=CredentialsManager),
        worker_target=_desktop_target(),
    )

    assert tool._current_configuration().timeout_seconds == 90


def test_scoped_desktop_preserves_invalid_authored_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pairing cannot silently replace an invalid operator timeout."""
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.load_desktop_credentials",
        lambda *_args, **_kwargs: {
            "device_user_id": "@desktop:example.org",
            "device_id": "DEVICE",
            "device_ed25519": "fingerprint",
        },
    )
    tool = DesktopTools(
        timeout_seconds=500,
        credentials_manager=MagicMock(spec=CredentialsManager),
        worker_target=_desktop_target(),
    )

    state = tool._current_configuration()

    assert state.status is DesktopConfigurationStatus.INVALID
    assert "between 1 and 120" in (state.error or "")


@pytest.mark.asyncio
async def test_commands_use_one_process_channel_with_monotonic_sequences(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wall-clock changes cannot make later commands look reordered to the local bridge."""
    context = SimpleNamespace(
        session_id="matrix-conversation",
        requester_id="@alice:example.org",
        agent_name="computer",
        client=object(),
    )
    request = AsyncMock(
        return_value=DesktopResponse(
            request_id="request-1",
            session_id="channel",
            ok=True,
            result={"online": True},
        ),
    )
    monkeypatch.setattr("mindroom.custom_tools.desktop.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.desktop_response_router",
        lambda _client: SimpleNamespace(request=request),
    )
    tool = _configured_tool(monkeypatch)

    await tool.desktop("status")
    await tool.desktop("status")

    first_command = request.await_args_list[0].args[1]
    second_command = request.await_args_list[1].args[1]
    assert first_command.session_id == second_command.session_id
    assert first_command.session_id != context.session_id
    assert (first_command.sequence, second_command.sequence) == (0, 1)


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
    tool = _configured_tool(monkeypatch)

    result = await tool.desktop("screenshot", app="com.example.Editor")

    assert json.loads(result.content)["status"] == "ok"
    assert result.images is not None
    assert result.images[0].content == b"\xff\xd8\xffjpeg"
    command = request.await_args.args[1]
    assert command.requester_id == "@alice:example.org"
    assert command.agent_name == "computer"
    assert command.parameters == {"app": "com.example.Editor"}


@pytest.mark.asyncio
async def test_screenshot_can_return_turn_scoped_sendable_attachment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in screenshots reuse encrypted MXC media without creating a plaintext file."""
    context = SimpleNamespace(
        session_id="session-1",
        requester_id="@alice:example.org",
        agent_name="computer",
        client=object(),
        attachment_ids=(),
        runtime_attachment_ids=[],
        runtime_media_attachments={},
    )
    response = DesktopResponse(
        request_id="request-1",
        session_id="session-1",
        ok=True,
        result={"capture": {"width": 800, "height": 600}},
        screenshot=MEDIA,
    )
    monkeypatch.setattr("mindroom.custom_tools.desktop.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.desktop_response_router",
        lambda _client: SimpleNamespace(request=AsyncMock(return_value=response)),
    )
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.download_encrypted_screenshot",
        AsyncMock(return_value=b"\xff\xd8\xffjpeg"),
    )

    result = await _configured_tool(monkeypatch).desktop(
        "screenshot",
        app="com.example.Editor",
        return_attachment=True,
    )

    payload = json.loads(result.content)
    attachment_id = payload["attachment_id"]
    assert attachment_id.startswith("att_")
    assert payload["attachment_lifetime"] == "current_turn"
    assert context.runtime_attachment_ids == [attachment_id]
    attachment = context.runtime_media_attachments[attachment_id]
    assert attachment.url == MEDIA.url
    assert attachment.key == MEDIA.key
    assert attachment.filename.endswith(".jpg")


@pytest.mark.asyncio
async def test_return_attachment_is_rejected_for_non_screenshot_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only an explicit screenshot may mint a sendable image handle."""
    request = AsyncMock()
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.get_tool_runtime_context",
        lambda: SimpleNamespace(
            requester_id="@alice:example.org",
            agent_name="computer",
            client=object(),
        ),
    )
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.desktop_response_router",
        lambda _client: SimpleNamespace(request=request),
    )

    result = await _configured_tool(monkeypatch).desktop(
        "get_app_state",
        app="com.example.Editor",
        return_attachment=True,
    )

    assert json.loads(result.content)["status"] == "error"
    request.assert_not_awaited()


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
    tool = _configured_tool(monkeypatch)

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
async def test_launch_app_uses_allowlisted_app_without_a_state_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cloud can request an explicit launch before any accessibility state exists."""
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
    tool = _configured_tool(monkeypatch)

    await tool.desktop("launch_app", app="com.example.Editor")

    assert request.await_args.args[1].parameters == {"app": "com.example.Editor"}


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
    tool = _configured_tool(monkeypatch)

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
    tool = _configured_tool(monkeypatch)

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
    tool = _configured_tool(monkeypatch)

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
    tool = _configured_tool(monkeypatch)

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
    tool = _configured_tool(monkeypatch)

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
    tool = _configured_tool(monkeypatch)

    result = await tool.desktop("list_apps")

    assert json.loads(result.content)["status"] == "ok"
    assert result.images is None
