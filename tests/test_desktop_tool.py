"""Tests for the cloud-side desktop agent tool."""

from __future__ import annotations

import json
from dataclasses import replace as dc_replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

import mindroom.tools  # noqa: F401
from mindroom.agents import create_agent
from mindroom.approval.tool_owners import required_approval_tool_names
from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.constants import DEFAULT_TOOL_OUTPUT_AUTO_SAVE_THRESHOLD_BYTES
from mindroom.credentials.manager import (
    CredentialsManager,
    get_runtime_credentials_manager,
    scoped_credentials_path,
)
from mindroom.custom_tools.desktop import DesktopTools, desktop_configuration_for_scope
from mindroom.delegation.state import SubagentPersona
from mindroom.desktop.client import DesktopRequestError
from mindroom.desktop.configuration import DesktopConfigurationStatus, desktop_configuration_state
from mindroom.desktop.credentials import (
    delete_desktop_credentials,
    has_ready_self_service_desktop,
    save_desktop_credentials,
)
from mindroom.desktop.media import DesktopMediaError
from mindroom.desktop.protocol import MAX_SHELL_OUTPUT_BYTES, DesktopResponse, EncryptedDesktopMedia
from mindroom.event_journal import ApprovalCall
from mindroom.tool_jobs.authorization import locally_allowed
from mindroom.tool_system import agent_tools as agent_tools_module
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity, WorkerScope
from tests.agent_tool_helpers import toolkit_offers
from tests.conftest import persist_entity_accounts, test_runtime_paths
from tests.turn_model_helpers import turn_model

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.constants import RuntimePaths

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
    metadata = builtin_tool_registry().entries["desktop"]

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
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "agents": {"computer": agent},
        },
        test_runtime_paths(tmp_path),
    )

    assert "desktop" in config.resolve_agent("computer").tools.tool_names


def test_desktop_rejects_authored_device_identity(tmp_path: Path) -> None:
    """Desktop device identity comes only from requester-agent pairing."""
    with pytest.raises(ConfigRuntimeValidationError, match=r"desktop\.device_user_id"):
        Config.validate_with_runtime(
            {
                "defaults": {"tools": []},
                "models": {"default": {"provider": "openai", "id": "test-model"}},
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
        runs_in_worker=False,
        worker_target=_desktop_target(),
        tools=builtin_tool_registry(),
    )

    result = await tool.desktop("status")  # type: ignore[attr-defined]

    payload = json.loads(result.content)
    assert payload["status"] == "setup_required"
    assert "/desktop setup" in payload["message"]


@pytest.mark.asyncio
async def test_unconfigured_private_user_agent_tool_advertises_chat_pairing() -> None:
    """Private requester-agent execution uses the same chat pairing flow."""
    tool = DesktopTools(worker_target=_desktop_target(worker_scope="user_agent"))

    result = await tool.desktop("status")

    assert "/desktop setup" in json.loads(result.content)["message"]


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
    command = request.await_args.args[1]
    assert command.requester_id == "@bob:example.org"
    assert command.agent_name == "computer"


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
    assert result.images
    assert result.images[0].content == b"\xff\xd8\xffjpeg"
    command = request.await_args.args[1]
    assert command.requester_id == "@alice:example.org"
    assert command.agent_name == "computer"
    assert command.parameters == {"app": "com.example.Editor", "observation": "screenshot"}


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
    assert result.images == ()


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
    assert result.images == ()


@pytest.mark.asyncio
async def test_state_with_undecryptable_screenshot_remains_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    """A returned semantic tree is preserved when only its encrypted screenshot fails."""
    context = SimpleNamespace(
        session_id="session-1",
        requester_id="@alice:example.org",
        agent_name="computer",
        client=object(),
    )
    stable_state = {
        "state_id": "state-1",
        "elements": [{"index": 0, "role": "button", "title": "Save", "ref": "ref-0"}],
        "truncated": False,
        "stability": "stable",
    }
    request = AsyncMock(
        return_value=DesktopResponse(
            request_id="request-1",
            session_id="session-1",
            ok=True,
            result={"observation": {"mode": "both"}, "state": stable_state},
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
    assert payload["result"]["state"] == stable_state


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
    assert result.images == ()


@pytest.mark.asyncio
async def test_explicit_tree_result_is_success_without_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """Intentional semantic observations are usable results, not failed screenshots."""
    context = SimpleNamespace(
        session_id="matrix-conversation",
        requester_id="@alice:example.org",
        agent_name="computer",
        client=object(),
    )
    request = AsyncMock(
        return_value=DesktopResponse(
            "request-1",
            "session-1",
            True,
            result={"observation": {"mode": "tree"}, "state": {"state_id": "state-1", "elements": []}},
        ),
    )
    monkeypatch.setattr("mindroom.custom_tools.desktop.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.desktop_response_router",
        lambda _client: SimpleNamespace(request=request),
    )
    tool = _configured_tool(monkeypatch)
    result = await tool.desktop("get_app_state", app="com.example.Editor", observation="tree")
    assert json.loads(result.content)["status"] == "ok"
    assert not result.images
    assert request.await_args.args[1].parameters["observation"] == "tree"


@pytest.mark.asyncio
async def test_timeout_returns_identity_for_request_status_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent can recover a timed-out request instead of repeating input."""
    context = SimpleNamespace(requester_id="@alice:example.org", agent_name="computer", client=object())
    request = AsyncMock(side_effect=DesktopRequestError("Timed out", request_id="original-request"))
    monkeypatch.setattr("mindroom.custom_tools.desktop.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.desktop_response_router",
        lambda _client: SimpleNamespace(request=request),
    )
    tool = _configured_tool(monkeypatch)
    result = await tool.desktop("get_app_state", app="com.example.Editor")
    payload = json.loads(result.content)
    assert payload["request_id"] == "original-request"
    assert payload["action_outcome"] == "unknown"
    assert payload["recovery_action"] == "request_status"
    request.side_effect = None
    request.return_value = DesktopResponse("query", "session", True, result={"state": "completed"})
    result = await tool.desktop("request_status", request_id="original-request")
    assert json.loads(result.content)["status"] == "ok"
    assert request.await_args.args[1].parameters == {"request_id": "original-request"}


def _route_desktop_requests(monkeypatch: pytest.MonkeyPatch, *responses: DesktopResponse) -> AsyncMock:
    """Send every command to a recorded router returning the given responses in order."""
    request = AsyncMock(side_effect=list(responses))
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.get_tool_runtime_context",
        lambda: SimpleNamespace(requester_id="@alice:example.org", agent_name="computer", client=object()),
    )
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.desktop_response_router",
        lambda _client: SimpleNamespace(request=request),
    )
    return request


def _shell_result(**fields: object) -> dict[str, object]:
    return {
        "state": "completed",
        "handle": None,
        "exit_code": 0,
        "output": "",
        "output_bytes": 0,
        "output_truncated": False,
        "output_attachment": None,
        "metrics": {"elapsed_ms": 5},
        **fields,
    }


def _output_media(output: bytes) -> EncryptedDesktopMedia:
    return EncryptedDesktopMedia(
        url="mxc://example.org/shell-output",
        key="key",
        iv="iv",
        sha256="hash",
        mime_type="text/plain",
        size=len(output),
    )


def test_local_folder_and_shell_actions_are_discoverable_with_their_own_parameters() -> None:
    """The schema advertises read-only folder and handle-based shell actions with bounded arguments."""
    function = toolkit_offers(DesktopTools(), "desktop")
    schema = function.parameters
    properties = schema["properties"]

    assert {"list_folders", "list_directory", "read_file", "run_shell", "check_shell", "kill_shell"} <= set(
        properties["action"]["enum"],
    )
    assert properties["root_id"]["type"] == "string"
    assert properties["path"]["maxLength"] == 4096
    assert (properties["offset"]["type"], properties["offset"]["minimum"]) == ("integer", 0)
    assert properties["command"]["maxLength"] == 8192
    assert properties["cwd"]["maxLength"] == 4096
    assert (properties["timeout_seconds"]["minimum"], properties["timeout_seconds"]["maximum"]) == (1, 60)
    assert properties["handle"]["type"] == "string"
    assert properties["force"]["type"] == "boolean"
    description = function.description or ""
    assert "read-only" in description
    assert "full account access" in description
    assert "request_status" in description
    assert "check_shell" in description
    assert "kill_shell" in description
    assert "untrusted" in description
    assert "gui_mode (observe_only or control) covers only app control" in description
    assert "kill_shell finishes with state killed" in description


def test_shell_guidance_states_output_limits_and_the_shell_it_runs_in() -> None:
    """The agent learns the real size limits and that each command runs alone in a non-interactive /bin/sh."""
    function = toolkit_offers(DesktopTools(), "desktop")
    description = function.description or ""
    assert f"{DEFAULT_TOOL_OUTPUT_AUTO_SAVE_THRESHOLD_BYTES // 1024} KiB" in description
    assert f"{MAX_SHELL_OUTPUT_BYTES // (1024 * 1024)} MiB" in description
    for phrase in (
        "stderr is merged into output in order",
        "2>file",
        "$SHELL is the user's login shell",
        "fresh non-interactive /bin/sh",
        "stdin at EOF",
        "no TTY",
    ):
        assert phrase in description
    command = function.parameters["properties"]["command"]["description"]
    assert "/bin/sh" in command
    assert "2>file" in command
    handle = function.parameters["properties"]["handle"]["description"]
    assert handle == (
        "Shell handle from a run_shell or check_shell reply; a finished command keeps one only while output is "
        "still undelivered."
    )


@pytest.mark.asyncio
async def test_remote_folder_read_encodes_root_and_path_and_returns_text_without_screenshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A folder read goes to the paired device with its own arguments and yields text, not a screenshot error."""
    read = {"text": "notes", "offset": 4, "next_offset": 9, "eof": True, "truncated": False}
    request = _route_desktop_requests(monkeypatch, DesktopResponse("request-1", "session", True, result=read))
    tool = _configured_tool(monkeypatch)

    result = await tool.desktop("read_file", root_id="root-1", path="docs/notes.txt", offset=4)

    payload = json.loads(result.content)
    assert payload["status"] == "ok"
    assert payload["result"] == read
    assert not result.images
    target, command = request.await_args.args
    assert target.user_id == "@desktop:example.org"
    assert command.action == "read_file"
    assert command.parameters == {"root_id": "root-1", "path": "docs/notes.txt", "offset": 4}
    assert command.expires_at_ms - command.issued_at_ms == 30_000
    assert request.await_args.kwargs["timeout_seconds"] == 30.0


@pytest.mark.asyncio
async def test_folder_listing_omits_unspecified_optional_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Optional folder and handle arguments are sent only when the agent supplies them."""
    request = _route_desktop_requests(
        monkeypatch,
        DesktopResponse("r1", "s", True, result={"folders": []}),
        DesktopResponse("r2", "s", True, result={"entries": [], "truncated": False}),
        DesktopResponse("r3", "s", True, result={"state": "killed", "handle": "shell:1"}),
        DesktopResponse("r4", "s", True, result={"state": "killed", "handle": "shell:1"}),
    )
    tool = _configured_tool(monkeypatch)

    await tool.desktop("list_folders")
    await tool.desktop("list_directory", root_id="root-1")
    await tool.desktop("kill_shell", handle="shell:1")
    await tool.desktop("kill_shell", handle="shell:1", force=True)

    assert [call.args[1].parameters for call in request.await_args_list] == [
        {},
        {"root_id": "root-1"},
        {"handle": "shell:1"},
        {"handle": "shell:1", "force": True},
    ]


@pytest.mark.asyncio
async def test_check_shell_polls_from_an_offset_and_returns_where_to_continue(monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent passes the last next_offset to check_shell; other shell actions refuse an offset."""
    polled = _shell_result(state="running", handle="shell:1", exit_code=None, output="new", next_offset=9)
    request = _route_desktop_requests(monkeypatch, DesktopResponse("r1", "s", True, result=polled))
    tool = _configured_tool(monkeypatch)

    result = await tool.desktop("check_shell", handle="shell:1", offset=6)

    assert request.await_args.args[1].parameters == {"handle": "shell:1", "offset": 6}
    assert json.loads(result.content)["result"]["next_offset"] == 9
    for action, arguments, message in (
        ("check_shell", {"handle": "shell:1", "offset": -1}, "offset must be an integer of at least 0"),
        ("kill_shell", {"handle": "shell:1", "offset": 6}, "does not accept offset"),
        ("run_shell", {"command": "ls", "offset": 6}, "does not accept offset"),
    ):
        payload = json.loads((await tool.desktop(action, **arguments)).content)
        assert (payload["status"], message in payload["message"]) == ("error", True)
    assert request.await_count == 1
    properties = toolkit_offers(tool, "desktop").parameters["properties"]
    assert "check_shell" in properties["offset"]["description"]
    description = toolkit_offers(tool, "desktop").description or ""
    assert "next_offset" in description
    assert "an output_start above the offset you asked for" in description


@pytest.mark.asyncio
async def test_run_shell_waits_for_local_approval_while_other_actions_keep_configured_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shell starts may wait the full command lifetime; handle checks keep the authored timeout."""
    request = _route_desktop_requests(
        monkeypatch,
        DesktopResponse("r1", "s", True, result=_shell_result(output="done\n", output_bytes=5)),
        DesktopResponse("r2", "s", True, result=_shell_result(state="running", handle="shell:1", exit_code=None)),
    )
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.load_desktop_credentials",
        lambda *_args, **_kwargs: {
            "device_user_id": "@desktop:example.org",
            "device_id": "DESKTOP",
            "device_ed25519": "fingerprint",
        },
    )
    tool = DesktopTools(
        timeout_seconds=12,
        credentials_manager=MagicMock(spec=CredentialsManager),
        worker_target=_desktop_target(),
    )

    run = await tool.desktop("run_shell", command="make test", cwd="/work/project", timeout_seconds=45)
    check = await tool.desktop("check_shell", handle="shell:1")

    run_call, check_call = request.await_args_list
    assert run_call.args[1].parameters == {"command": "make test", "cwd": "/work/project", "timeout_seconds": 45}
    assert run_call.args[1].expires_at_ms - run_call.args[1].issued_at_ms == 120_000
    assert run_call.kwargs["timeout_seconds"] == 120
    assert check_call.args[1].parameters == {"handle": "shell:1"}
    assert check_call.args[1].expires_at_ms - check_call.args[1].issued_at_ms == 12_000
    assert check_call.kwargs["timeout_seconds"] == 12
    run_result = json.loads(run.content)["result"]
    assert run_result["output"] == "done\n"
    assert "output_attachment" not in run_result
    assert json.loads(check.content)["result"]["handle"] == "shell:1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "arguments", "message"),
    [
        ("read_file", {"root_id": "root-1", "path": "a.txt", "command": "ls"}, "command"),
        ("list_directory", {"path": "docs"}, "root_id"),
        ("read_file", {"root_id": "root-1"}, "path"),
        ("read_file", {"root_id": "root-1", "path": "a.txt", "offset": -1}, "offset"),
        ("run_shell", {"command": "ls", "app": "com.example.Editor"}, "app"),
        ("run_shell", {"command": "ls", "timeout_seconds": 61}, "timeout_seconds"),
        ("run_shell", {"command": "ls", "timeout_seconds": True}, "timeout_seconds"),
        ("run_shell", {"command": "ls", "handle": "shell:1"}, "handle"),
        ("run_shell", {}, "command"),
        ("run_shell", {"command": "x" * 8193}, "command"),
        ("check_shell", {"handle": "shell:1", "force": True}, "force"),
        ("kill_shell", {}, "handle"),
        ("status", {"root_id": "root-1"}, "root_id"),
        ("get_app_state", {"app": "com.example.Editor", "cwd": "/work"}, "cwd"),
    ],
)
async def test_local_actions_accept_only_their_own_arguments(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    arguments: dict[str, object],
    message: str,
) -> None:
    """Arguments meant for another action are refused before anything leaves the cloud."""
    request = _route_desktop_requests(monkeypatch)
    tool = _configured_tool(monkeypatch)

    result = await tool.desktop(action, **arguments)

    payload = json.loads(result.content)
    assert payload["status"] == "error"
    assert message in payload["message"]
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_shell_output_attachment_is_downloaded_verified_and_returned_as_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large output arrives as the full text; the transport descriptor and its key never reach the model."""
    output = "row\n" * 30_000
    media = _output_media(output.encode())
    _route_desktop_requests(
        monkeypatch,
        DesktopResponse(
            "r1",
            "s",
            True,
            result=_shell_result(output_bytes=len(output), output_attachment=media.to_content()),
        ),
    )
    download = AsyncMock(return_value=output.encode())
    monkeypatch.setattr("mindroom.custom_tools.desktop.download_encrypted_media", download)

    result = await _configured_tool(monkeypatch).desktop("run_shell", command="seq 30000")

    payload = json.loads(result.content)
    assert payload["status"] == "ok"
    assert payload["result"]["output"] == output
    assert "output_attachment" not in payload["result"]
    assert media.key not in result.content
    downloaded_media = download.await_args.args[1]
    assert downloaded_media == media
    assert download.await_args.kwargs["timeout_seconds"] == 30.0


@pytest.mark.asyncio
async def test_failed_output_download_is_partial_and_points_to_request_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """A command that ran is never presented as safe to run again when only its output transfer failed."""
    media = _output_media(b"x" * 50_000)
    request = _route_desktop_requests(
        monkeypatch,
        DesktopResponse(
            "r1",
            "s",
            True,
            result=_shell_result(output_bytes=50_000, output_attachment=media.to_content()),
        ),
    )
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.download_encrypted_media",
        AsyncMock(side_effect=DesktopMediaError("Matrix media authentication or decryption failed.")),
    )

    result = await _configured_tool(monkeypatch).desktop("check_shell", handle="shell:1")

    payload = json.loads(result.content)
    assert payload["status"] == "partial"
    assert payload["result"]["exit_code"] == 0
    assert "output_attachment" not in payload["result"]
    assert "request_status" in payload["message"]
    assert "check_shell again" in payload["message"]
    assert payload["request_id"] == request.await_args.args[1].request_id
    assert payload["recovery_action"] == "request_status"
    assert media.key not in result.content


@pytest.mark.asyncio
async def test_interrupted_shell_command_is_partial_with_the_bridge_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """An uncertain shell outcome keeps the do-not-repeat warning instead of looking like a failed screenshot."""
    warning = "The shell command outcome is unknown and it may have completed; do not repeat it automatically."
    _route_desktop_requests(
        monkeypatch,
        DesktopResponse(
            "r1",
            "s",
            True,
            result={"action": "run_shell", "action_outcome": "unknown", "warning": warning},
        ),
    )

    result = await _configured_tool(monkeypatch).desktop("run_shell", command="make deploy")

    payload = json.loads(result.content)
    assert payload["status"] == "partial"
    assert payload["message"] == warning


@pytest.mark.asyncio
async def test_request_status_recovers_a_lost_shell_reply_with_its_full_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lost check_shell reply is recovered from its receipt, including output sent as an attachment."""
    output = b"recovered\n" * 10_000
    media = _output_media(output)
    recorded = DesktopResponse(
        "original",
        "s",
        True,
        result=_shell_result(handle="shell:1", output_bytes=len(output), output_attachment=media.to_content()),
    )
    _route_desktop_requests(
        monkeypatch,
        DesktopResponse(
            "query",
            "s",
            True,
            result={"request_id": "original", "state": "completed", "response": recorded.to_content()},
        ),
    )
    monkeypatch.setattr("mindroom.custom_tools.desktop.download_encrypted_media", AsyncMock(return_value=output))

    result = await _configured_tool(monkeypatch).desktop("request_status", request_id="original")

    payload = json.loads(result.content)
    assert payload["status"] == "ok"
    recovered = payload["result"]["response"]["result"]
    assert recovered["output"] == output.decode()
    assert recovered["handle"] == "shell:1"
    assert "output_attachment" not in recovered
    assert media.key not in result.content


@pytest.mark.asyncio
async def test_large_shell_output_uses_the_workspace_output_file_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The registered toolkit is wrapped, so large desktop results auto-save and honor mindroom_output_path."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = "log line\n" * 12_000
    response = DesktopResponse(
        "r1",
        "s",
        True,
        result=_shell_result(output_bytes=len(output), output_attachment=_output_media(output.encode()).to_content()),
    )
    _route_desktop_requests(monkeypatch, response, response)
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.download_encrypted_media",
        AsyncMock(return_value=output.encode()),
    )
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.load_desktop_credentials",
        lambda *_args, **_kwargs: {
            "device_user_id": "@desktop:example.org",
            "device_id": "DESKTOP",
            "device_ed25519": "fingerprint",
        },
    )
    toolkit = get_tool_by_name(
        "desktop",
        test_runtime_paths(tmp_path),
        runs_in_worker=False,
        tool_output_workspace_root=workspace,
        worker_target=_desktop_target(),
        tools=builtin_tool_registry(),
    )
    function = toolkit_offers(toolkit, "desktop")
    assert "mindroom_output_path" in function.parameters["properties"]

    auto_saved = await function.entrypoint(action="run_shell", command="make logs")
    redirected = await function.entrypoint(
        action="run_shell",
        command="make logs",
        mindroom_output_path="logs/shell.json",
    )

    receipt = auto_saved["mindroom_tool_output"]
    assert receipt["auto_saved"] is True
    assert receipt["path"].startswith("mindroom_tool_outputs/desktop-")
    assert "log line" in receipt["preview"]
    saved = json.loads((workspace / receipt["path"]).read_text(encoding="utf-8"))
    assert saved["result"]["output"] == output
    assert redirected["mindroom_tool_output"]["status"] == "saved_to_file"
    written = json.loads((workspace / "logs" / "shell.json").read_text(encoding="utf-8"))
    assert written["result"]["output"] == output


def _build_assistant(
    tmp_path: Path,
    *,
    requester_id: str,
    desktop_pairing: str | None = None,
    tools: list[str] | None = None,
    room_id: str | None = "!room:example.org",
    agent_mode: str = "standard",
    persona: SubagentPersona | None = None,
    pair: bool = False,
) -> tuple[Config, RuntimePaths, AgentBuild, ToolExecutionIdentity]:
    runtime_paths = test_runtime_paths(tmp_path)
    defaults: dict[str, object] = {"tools": []}
    if desktop_pairing is not None:
        defaults["desktop_pairing"] = desktop_pairing
    config = Config.validate_with_runtime(
        {
            "defaults": defaults,
            "models": {"default": {"provider": "openai", "id": "gpt-4o-mini"}},
            "agents": {
                "assistant": {
                    "display_name": "Assistant",
                    "role": "Help",
                    "tools": tools if tools is not None else ["calculator"],
                },
            },
        },
        runtime_paths,
    )
    persist_entity_accounts(config, runtime_paths)
    if pair:
        save_desktop_credentials(
            get_runtime_credentials_manager(runtime_paths),
            {"device_user_id": "@mac:example.org", "device_id": "MAC1", "device_ed25519": "mac-key"},
            requester_id=requester_id,
            agent_name="assistant",
        )
    identity = ToolExecutionIdentity(
        channel="matrix" if room_id is not None else "openai_compat",
        agent_name="assistant",
        requester_id=requester_id,
        room_id=room_id,
        thread_id=None,
        resolved_thread_id=None,
        session_id="session",
    )
    build = create_agent(
        "assistant",
        config,
        runtime_paths,
        execution_identity=identity,
        knowledge=None,
        model=turn_model(config, runtime_paths, "assistant"),
        plugins=config_plugins(config, runtime_paths),
        agent_mode=agent_mode,  # type: ignore[arg-type]
        persona=persona,
    )
    return config, runtime_paths, build, identity


def test_dynamic_desktop_tool_absent_when_unpaired_and_injected_when_paired(tmp_path: Path) -> None:
    """Under self_service, desktop is absent for unpaired requesters and injected only for paired ones."""
    config, _, unpaired, _ = _build_assistant(tmp_path, requester_id="@alice:example.org")
    assert config.defaults.desktop_pairing == "self_service"
    assert "desktop" not in {t.name for t in unpaired.spec.toolkits}

    config, runtime_paths, paired, alice_id = _build_assistant(tmp_path, requester_id="@alice:example.org", pair=True)
    assert "desktop" in {t.name for t in paired.spec.toolkits}
    assert paired.tool_jobs is not None
    tools_registry = config_plugins(config, runtime_paths).tools
    desktop_authority = dict(paired.tool_jobs.tools["desktop"].authority)
    assert locally_allowed(
        config,
        alice_id,
        tools=tools_registry,
        tool_name="desktop",
        toolkit_name="desktop",
        origin={},
        depth=0,
        authority=desktop_authority,
        runtime_paths=runtime_paths,
    )
    assert not locally_allowed(
        config,
        alice_id,
        tools=tools_registry,
        tool_name="desktop",
        toolkit_name="desktop",
        origin={},
        depth=0,
        authority=desktop_authority,
        runtime_paths=None,
    )

    _, _, bob_build, _ = _build_assistant(tmp_path, requester_id="@bob:example.org")
    assert "desktop" not in {t.name for t in bob_build.spec.toolkits}

    _, _, roomless, _ = _build_assistant(tmp_path, requester_id="@alice:example.org", room_id=None)
    assert "desktop" not in {t.name for t in roomless.spec.toolkits}

    _, _, minimal, _ = _build_assistant(
        tmp_path / "minimal",
        requester_id="@alice:example.org",
        tools=["shell"],
        agent_mode="minimal",
        pair=True,
    )
    assert "desktop" in {t.name for t in minimal.spec.toolkits}
    assert "desktop" in minimal.surface.toolkit_names


@pytest.mark.parametrize("policy", ["explicit_only", "disabled"])
def test_explicit_only_and_disabled_policies_block_dynamic_injection(tmp_path: Path, policy: str) -> None:
    """explicit_only and disabled block dynamic injection, and narrowed personas are never bypassed."""
    blocked_cfg, blocked_paths, blocked, alice_id = _build_assistant(
        tmp_path / policy,
        requester_id="@alice:example.org",
        desktop_pairing=policy,
        pair=True,
    )
    assert "desktop" not in {t.name for t in blocked.spec.toolkits}

    persona = SubagentPersona(
        source_kind="inline",
        source_name="narrow",
        system_prompt="Only calculator.",
        tools=("calculator",),
    )
    _, _, narrowed, _ = _build_assistant(
        tmp_path / f"{policy}-ss",
        requester_id="@alice:example.org",
        persona=persona,
        pair=True,
    )
    assert "desktop" not in {t.name for t in narrowed.spec.toolkits}

    _, _, full, _ = _build_assistant(tmp_path / f"{policy}-ss", requester_id="@alice:example.org")
    assert full.tool_jobs is not None
    assert not locally_allowed(
        blocked_cfg,
        alice_id,
        tools=config_plugins(blocked_cfg, blocked_paths).tools,
        tool_name="desktop",
        toolkit_name="desktop",
        origin={},
        depth=0,
        authority=dict(full.tool_jobs.tools["desktop"].authority),
    )


@pytest.mark.asyncio
async def test_tool_owners_and_locally_allowed_enforce_self_service_ready_credentials_and_disabled_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """required_approval_tool_names and locally_allowed enforce self_service policy, ready credentials, and disabled tools."""
    config, runtime_paths, paired, alice_id = _build_assistant(
        tmp_path,
        requester_id="@alice:example.org",
        pair=True,
    )
    assert paired.tool_jobs is not None
    tools = config_plugins(config, runtime_paths).tools
    authority = dict(paired.tool_jobs.tools["desktop"].authority)
    calls = (ApprovalCall("call-1", "desktop", "assistant", 100, toolkit_name="desktop"),)

    # Paired requester in a room under self_service is permitted by both approval recovery and tool-job authorization.
    assert await required_approval_tool_names(
        "assistant",
        calls,
        config=config,
        runtime_paths=runtime_paths,
        tools=tools,
        execution_identity=alice_id,
        delegation_depth=0,
    ) == ("desktop",)
    assert locally_allowed(
        config,
        alice_id,
        tools=tools,
        tool_name="desktop",
        toolkit_name="desktop",
        origin={},
        depth=0,
        authority=authority,
        runtime_paths=runtime_paths,
    )

    # Roomless execution identity is refused by both.
    roomless_id = ToolExecutionIdentity(
        channel="matrix",
        agent_name="assistant",
        requester_id="@alice:example.org",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id="session",
    )
    with pytest.raises(RuntimeError, match="no longer permitted"):
        await required_approval_tool_names(
            "assistant",
            calls,
            config=config,
            runtime_paths=runtime_paths,
            tools=tools,
            execution_identity=roomless_id,
            delegation_depth=0,
        )
    assert not locally_allowed(
        config,
        roomless_id,
        tools=tools,
        tool_name="desktop",
        toolkit_name="desktop",
        origin={},
        depth=0,
        authority=authority,
        runtime_paths=runtime_paths,
    )

    # Disabling desktop_pairing (explicit_only or disabled) refuses both even while credentials exist.
    for policy in ("explicit_only", "disabled"):
        policy_cfg = config.model_copy(
            update={"defaults": config.defaults.model_copy(update={"desktop_pairing": policy})},
        )
        with pytest.raises(RuntimeError, match="no longer permitted"):
            await required_approval_tool_names(
                "assistant",
                calls,
                config=policy_cfg,
                runtime_paths=runtime_paths,
                tools=tools,
                execution_identity=alice_id,
                delegation_depth=0,
            )
        assert not locally_allowed(
            policy_cfg,
            alice_id,
            tools=tools,
            tool_name="desktop",
            toolkit_name="desktop",
            origin={},
            depth=0,
            authority=authority,
            runtime_paths=runtime_paths,
        )

    # If desktop appears in agent_tools.facts.disabled, both reject it.
    real_resolve = agent_tools_module.resolve_agent_tools

    def resolve_with_desktop_disabled(cfg: Config, name: str, registry: object) -> object:
        resolved = real_resolve(cfg, name, registry)  # type: ignore[arg-type]
        return dc_replace(resolved, facts=dc_replace(resolved.facts, disabled=resolved.facts.disabled | {"desktop"}))

    with monkeypatch.context() as mp:
        mp.setattr("mindroom.approval.tool_owners.resolve_agent_tools", resolve_with_desktop_disabled)
        mp.setattr("mindroom.tool_jobs.authorization.resolve_agent_tools", resolve_with_desktop_disabled)
        with pytest.raises(RuntimeError, match="no longer permitted"):
            await required_approval_tool_names(
                "assistant",
                calls,
                config=config,
                runtime_paths=runtime_paths,
                tools=tools,
                execution_identity=alice_id,
                delegation_depth=0,
            )
        assert not locally_allowed(
            config,
            alice_id,
            tools=tools,
            tool_name="desktop",
            toolkit_name="desktop",
            origin={},
            depth=0,
            authority=authority,
            runtime_paths=runtime_paths,
        )

    # Once the requester disconnects (credentials deleted), both refuse immediately.
    delete_desktop_credentials(
        get_runtime_credentials_manager(runtime_paths),
        requester_id="@alice:example.org",
        agent_name="assistant",
    )
    with pytest.raises(RuntimeError, match="no longer permitted"):
        await required_approval_tool_names(
            "assistant",
            calls,
            config=config,
            runtime_paths=runtime_paths,
            tools=tools,
            execution_identity=alice_id,
            delegation_depth=0,
        )
    assert not locally_allowed(
        config,
        alice_id,
        tools=tools,
        tool_name="desktop",
        toolkit_name="desktop",
        origin={},
        depth=0,
        authority=authority,
        runtime_paths=runtime_paths,
    )


@pytest.mark.asyncio
async def test_request_status_decrypts_recorded_screenshot_and_strips_media_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovering an app receipt decrypts its screenshot and never leaks media encryption fields."""
    secret_media = EncryptedDesktopMedia(
        url="mxc://example.org/secret-shot",
        key="secret-aes-key-material",
        iv="secret-iv-value",
        sha256="secret-sha256-hash",
        mime_type="image/jpeg",
        size=10,
    )
    recorded = DesktopResponse(
        "original",
        "s",
        True,
        result={"action": "get_app_state", "state": {"state_id": "state-1"}},
        screenshot=secret_media,
    )
    _route_desktop_requests(
        monkeypatch,
        DesktopResponse(
            "query",
            "s",
            True,
            result={"request_id": "original", "state": "completed", "response": recorded.to_content()},
        ),
    )
    download = AsyncMock(return_value=b"\xff\xd8\xffrecovered")
    monkeypatch.setattr("mindroom.custom_tools.desktop.download_encrypted_screenshot", download)

    result = await _configured_tool(monkeypatch).desktop("request_status", request_id="original")

    payload = json.loads(result.content)
    assert payload["status"] == "ok"
    assert "screenshot" not in payload["result"]["response"]
    for leaked in (secret_media.key, secret_media.url, secret_media.iv, secret_media.sha256):
        assert leaked not in result.content
    assert len(result.images) == 1
    assert result.images[0].content == b"\xff\xd8\xffrecovered"
    assert result.images[0].mime_type == "image/jpeg"
    download.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_status_screenshot_failure_is_partial_and_redacts_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport or media error during request_status screenshot download returns sanitized partial output."""
    secret_media = EncryptedDesktopMedia(
        url="mxc://example.org/secret-shot",
        key="secret-aes-key-material",
        iv="secret-iv-value",
        sha256="secret-sha256-hash",
        mime_type="image/jpeg",
        size=10,
    )
    recorded = DesktopResponse(
        "original",
        "s",
        True,
        result={"action": "click_element", "action_completed": True},
        screenshot=secret_media,
    )
    fake_client = SimpleNamespace(
        download=AsyncMock(
            side_effect=aiohttp.ClientPayloadError(
                "stream broke for mxc://example.org/secret-shot",
            ),
        ),
    )
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.get_tool_runtime_context",
        lambda: SimpleNamespace(requester_id="@alice:example.org", agent_name="computer", client=fake_client),
    )
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.desktop_response_router",
        lambda _client: SimpleNamespace(
            request=AsyncMock(
                return_value=DesktopResponse(
                    "query",
                    "s",
                    True,
                    result={"request_id": "original", "state": "completed", "response": recorded.to_content()},
                ),
            ),
        ),
    )

    result = await _configured_tool(monkeypatch).desktop("request_status", request_id="original")

    payload = json.loads(result.content)
    assert payload["status"] == "partial"
    assert payload["result"]["response"]["result"] == {"action": "click_element", "action_completed": True}
    assert "request_status" in payload["message"]
    assert "do not repeat" in payload["message"]
    assert "screenshot" not in payload["result"]["response"]
    for leaked in (secret_media.key, secret_media.url, secret_media.iv, secret_media.sha256, "stream broke"):
        assert leaked not in result.content
    assert not result.images


@pytest.mark.asyncio
async def test_screenshot_observation_defaults_to_screenshot_and_allows_explicit_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The screenshot action defaults to observation=screenshot while explicit both and other app actions default to both."""
    schema_prop = toolkit_offers(DesktopTools(), "desktop").parameters["properties"]["observation"]
    assert "default" not in schema_prop
    assert "action=screenshot" in schema_prop["description"]

    request = _route_desktop_requests(
        monkeypatch,
        DesktopResponse("r1", "s", True, result={"action": "screenshot"}, screenshot=MEDIA),
        DesktopResponse("r2", "s", True, result={"action": "screenshot"}, screenshot=MEDIA),
        DesktopResponse("r3", "s", True, result={"action": "get_app_state"}, screenshot=MEDIA),
    )
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.download_encrypted_screenshot",
        AsyncMock(return_value=b"\xff\xd8\xffjpeg"),
    )
    tool = _configured_tool(monkeypatch)

    await tool.desktop("screenshot", app="com.example.Editor")
    assert request.await_args_list[0].args[1].parameters == {
        "app": "com.example.Editor",
        "observation": "screenshot",
    }

    await tool.desktop("screenshot", app="com.example.Editor", observation="both")
    assert request.await_args_list[1].args[1].parameters == {"app": "com.example.Editor"}

    await tool.desktop("get_app_state", app="com.example.Editor")
    assert request.await_args_list[2].args[1].parameters == {"app": "com.example.Editor"}


@pytest.mark.asyncio
async def test_unreadable_scoped_desktop_credentials_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable/corrupt scoped pairing file maps to INVALID instead of SETUP_REQUIRED."""
    runtime_paths = test_runtime_paths(tmp_path)
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    worker_target = _desktop_target(requester_id="@alice:example.org")
    scoped_path = scoped_credentials_path(
        "desktop",
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    scoped_path.parent.mkdir(parents=True, exist_ok=True)
    scoped_path.write_text("{corrupt-json", encoding="utf-8")

    state = desktop_configuration_for_scope(
        credentials_manager,
        ("@alice:example.org", "computer"),
        default_timeout_seconds=30.0,
    )
    assert state.status is DesktopConfigurationStatus.INVALID
    assert "could not be read" in (state.error or "")

    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-4o"}},
            "defaults": {"desktop_pairing": "self_service"},
            "agents": {"computer": {"display_name": "Computer", "role": "Use desktop", "tools": ["desktop"]}},
        },
        runtime_paths,
    )
    assert has_ready_self_service_desktop(config, runtime_paths, "computer", "@alice:example.org") is False

    request = AsyncMock()
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.get_tool_runtime_context",
        lambda: SimpleNamespace(requester_id="@alice:example.org", agent_name="computer", client=object()),
    )
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.desktop_response_router",
        lambda _client: SimpleNamespace(request=request),
    )
    tool = DesktopTools(credentials_manager=credentials_manager, worker_target=worker_target)

    result = await tool.desktop("status")
    payload = json.loads(result.content)
    assert payload["status"] == "setup_required"
    assert "Desktop configuration is invalid" in payload["message"]
    assert "could not be read" in payload["message"]
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("observation", "state"),
    [
        (
            {"mode": "screenshot"},
            {
                "state_id": "state-shot",
                "app": {"id": "com.example.Editor", "name": "Editor"},
                "truncated": False,
                "stability": "stable",
            },
        ),
        (
            {"mode": "both"},
            {
                "state_id": "state-empty",
                "app": {"id": "com.example.Editor", "name": "Editor"},
                "elements": [],
                "truncated": False,
                "stability": "stable",
            },
        ),
        (
            {"mode": "both"},
            {
                "state_id": "state-truncated",
                "app": {"id": "com.example.Editor", "name": "Editor"},
                "elements": [{"index": 0, "role": "button", "title": "Save", "ref": "ref-0"}],
                "truncated": True,
                "stability": "stable",
            },
        ),
        (
            {"mode": "both"},
            {
                "state_id": "state-unstable",
                "app": {"id": "com.example.Editor", "name": "Editor"},
                "elements": [{"index": 0, "role": "button", "title": "Save", "ref": "ref-0"}],
                "truncated": False,
                "stability": "unstable",
            },
        ),
    ],
)
async def test_direct_screenshot_failure_withholds_state_id_when_fallback_needed(
    monkeypatch: pytest.MonkeyPatch,
    observation: dict[str, object],
    state: dict[str, object],
) -> None:
    """Direct partial receipts withhold state_id whenever missing screenshot pixels would make fallback blind."""
    _route_desktop_requests(
        monkeypatch,
        DesktopResponse(
            "r-get",
            "s",
            True,
            result={"observation": observation, "state": state},
            screenshot=MEDIA,
        ),
        DesktopResponse(
            "r-click",
            "s",
            True,
            result={"action": "click", "action_completed": True, "observation": observation, "state": state},
            screenshot=MEDIA,
        ),
    )
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.download_encrypted_screenshot",
        AsyncMock(side_effect=DesktopMediaError("Decryption failed.")),
    )
    tool = _configured_tool(monkeypatch)

    get_result = await tool.desktop("get_app_state", app="com.example.Editor")
    get_payload = json.loads(get_result.content)
    assert get_payload["status"] == "partial"
    assert "state_id" not in get_payload["result"]["state"]
    assert get_payload["result"]["state"]["app"] == {"id": "com.example.Editor", "name": "Editor"}
    assert "get_app_state" in get_payload["message"]
    assert not get_result.images

    click_result = await tool.desktop("click", app="com.example.Editor", state_id="prior-state", x=10, y=20)
    click_payload = json.loads(click_result.content)
    assert click_payload["status"] == "partial"
    assert click_payload["result"]["action_completed"] is True
    assert "state_id" not in click_payload["result"]["state"]
    assert click_payload["result"]["state"]["app"] == {"id": "com.example.Editor", "name": "Editor"}
    assert "do not repeat" in click_payload["message"]
    assert "get_app_state" in click_payload["message"]
    assert not click_result.images


@pytest.mark.asyncio
async def test_request_status_screenshot_failure_withholds_only_screenshot_dependent_state_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """request_status strips state_id when screenshot fallback was needed, but preserves complete stable trees."""
    secret_media = EncryptedDesktopMedia(
        url="mxc://example.org/secret-shot",
        key="secret-aes-key-material",
        iv="secret-iv-value",
        sha256="secret-sha256-hash",
        mime_type="image/jpeg",
        size=11,
    )
    screenshot_only_recorded = DesktopResponse(
        "original-1",
        "s",
        True,
        result={
            "action": "click",
            "action_completed": True,
            "observation": {"mode": "screenshot"},
            "state": {
                "state_id": "state-blind",
                "app": {"id": "com.example.Editor", "name": "Editor"},
                "truncated": False,
                "stability": "stable",
            },
        },
        screenshot=secret_media,
    )
    stable_tree_recorded = DesktopResponse(
        "original-2",
        "s",
        True,
        result={
            "action": "click",
            "action_completed": True,
            "observation": {"mode": "both"},
            "state": {
                "state_id": "state-usable",
                "app": {"id": "com.example.Editor", "name": "Editor"},
                "elements": [{"index": 0, "role": "button", "title": "Save", "ref": "ref-0"}],
                "truncated": False,
                "stability": "stable",
            },
        },
        screenshot=secret_media,
    )
    _route_desktop_requests(
        monkeypatch,
        DesktopResponse(
            "query-1",
            "s",
            True,
            result={
                "request_id": "original-1",
                "state": "completed",
                "response": screenshot_only_recorded.to_content(),
            },
        ),
        DesktopResponse(
            "query-2",
            "s",
            True,
            result={
                "request_id": "original-2",
                "state": "completed",
                "response": stable_tree_recorded.to_content(),
            },
        ),
    )
    monkeypatch.setattr(
        "mindroom.custom_tools.desktop.download_encrypted_screenshot",
        AsyncMock(side_effect=DesktopMediaError("Decryption failed.")),
    )
    tool = _configured_tool(monkeypatch)

    blind_result = await tool.desktop("request_status", request_id="original-1")
    blind_payload = json.loads(blind_result.content)
    assert blind_payload["status"] == "partial"
    recovered_blind = blind_payload["result"]["response"]["result"]
    assert recovered_blind["action_completed"] is True
    assert "state_id" not in recovered_blind["state"]
    assert recovered_blind["state"]["app"] == {"id": "com.example.Editor", "name": "Editor"}
    assert "screenshot" not in blind_payload["result"]["response"]
    assert secret_media.key not in blind_result.content
    assert "get_app_state" in blind_payload["message"]
    assert "do not repeat" in blind_payload["message"]
    assert not blind_result.images

    usable_result = await tool.desktop("request_status", request_id="original-2")
    usable_payload = json.loads(usable_result.content)
    assert usable_payload["status"] == "partial"
    recovered_usable = usable_payload["result"]["response"]["result"]
    assert recovered_usable["state"]["state_id"] == "state-usable"
    assert "screenshot" not in usable_payload["result"]["response"]
    assert secret_media.key not in usable_result.content
    assert not usable_result.images
