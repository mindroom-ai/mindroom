"""Tests for executing workspace automation actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pytest

from mindroom.config.main import Config
from mindroom.config.models import WorkspaceAutomationPolicyConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import HOOK_MESSAGE_RECEIVED_DEPTH_KEY, ORIGINAL_SENDER_KEY, RuntimePaths, resolve_runtime_paths
from mindroom.hooks import (
    EVENT_AUTOMATION_TRIGGERED,
    AutomationTriggeredContext,
    HookRegistry,
    hook,
)
from mindroom.hooks.types import format_hook_source
from mindroom.matrix.state import MatrixState
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.workspace_automations.actions import run_automation_action
from mindroom.workspace_automations.executor import ShellCheckResult
from mindroom.workspace_automations.models import (
    LoadedWorkspaceAutomation,
    WorkspaceAutomationAction,
    WorkspaceAutomationCheck,
    WorkspaceAutomationTrigger,
)
from mindroom.workspace_automations.targets import WorkspaceAutomationTarget

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class _SenderCall:
    room_id: str
    body: str
    thread_id: str | None
    source_hook: str
    extra_content: dict[str, Any] | None
    trigger_dispatch: bool


class _FakeMessageSender:
    def __init__(self, event_id: str | None = "$sent") -> None:
        self.event_id = event_id
        self.calls: list[_SenderCall] = []

    async def __call__(
        self,
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, Any] | None,
        *,
        trigger_dispatch: bool = False,
    ) -> str | None:
        self.calls.append(
            _SenderCall(
                room_id=room_id,
                body=body,
                thread_id=thread_id,
                source_hook=source_hook,
                extra_content=extra_content,
                trigger_dispatch=trigger_dispatch,
            ),
        )
        return self.event_id


@dataclass(frozen=True)
class _FakeAgentRuntime:
    execution_identity: ToolExecutionIdentity | None = None
    is_private: bool = False


def _plugin(name: str, callbacks: list[object]) -> object:
    return type(
        "PluginStub",
        (),
        {
            "name": name,
            "discovered_hooks": tuple(callbacks),
            "entry_config": PluginEntryConfig(path=f"./plugins/{name}"),
            "plugin_order": 0,
        },
    )()


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Create isolated runtime paths for action execution."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


@pytest.fixture
def config(runtime_paths: RuntimePaths) -> Config:
    """Create a minimal config for hook contexts."""
    return Config.validate_with_runtime(
        {
            "memory": {"backend": "none"},
            "agents": {"ops": {"display_name": "Ops", "rooms": ["Lobby"]}},
        },
        runtime_paths,
    )


def _policy(*allowed_actions: str) -> WorkspaceAutomationPolicyConfig:
    return WorkspaceAutomationPolicyConfig(
        enabled=True,
        allowed_actions=list(allowed_actions),
    )


def _target(tmp_path: Path, *, rooms: tuple[str, ...] = ("Lobby",), policy: WorkspaceAutomationPolicyConfig) -> object:
    return _target_with_identity(tmp_path, rooms=rooms, policy=policy)


def _target_with_identity(
    tmp_path: Path,
    *,
    rooms: tuple[str, ...] = ("Lobby",),
    policy: WorkspaceAutomationPolicyConfig,
    execution_identity: ToolExecutionIdentity | None = None,
    is_private: bool = False,
) -> object:
    return WorkspaceAutomationTarget(
        agent_name="ops",
        agent_configured_rooms=rooms,
        policy=policy,
        agent_runtime=cast(
            "Any",
            _FakeAgentRuntime(
                execution_identity=execution_identity,
                is_private=is_private,
            ),
        ),
        workspace_root=tmp_path,
    )


def _add_room(runtime_paths: RuntimePaths, room_key: str, room_id: str = "!lobby:localhost") -> None:
    matrix_state = MatrixState.load(runtime_paths)
    matrix_state.add_room(room_key, room_id, f"#{room_key.lower()}:localhost", room_key)
    matrix_state.save(runtime_paths)


def _add_agent_accounts(runtime_paths: RuntimePaths) -> None:
    matrix_state = MatrixState.load(runtime_paths)
    matrix_state.add_account("agent_router", "mindroom_router", "test-password", domain="localhost")
    matrix_state.add_account("agent_ops", "mindroom_ops", "test-password", domain="localhost")
    matrix_state.save(runtime_paths)


def _automation(
    tmp_path: Path,
    *,
    action_type: str,
    room: str | None = "Lobby",
    thread_id: str | None = "$thread",
    message: str | None = "Urgent email condition matched. Investigate and summarize.",
    trigger: WorkspaceAutomationTrigger | None = None,
) -> LoadedWorkspaceAutomation:
    return LoadedWorkspaceAutomation(
        agent_name="ops",
        automation_id="urgent_email_poll",
        workspace_root=tmp_path,
        file_path=tmp_path / ".mindroom" / "automations.yaml",
        schedule="*/1 * * * *",
        check=WorkspaceAutomationCheck(
            type="shell",
            command="./scripts/check_urgent_email.sh",
            timeout_seconds=20,
            tail=100,
        ),
        trigger=trigger if trigger is not None else WorkspaceAutomationTrigger(exit_code=42),
        action=WorkspaceAutomationAction(
            type=cast("Any", action_type),
            room=room,
            thread_id=thread_id,
            message=message,
        ),
    )


def _check_result() -> ShellCheckResult:
    return ShellCheckResult(
        automation_id="urgent_email_poll",
        ok=False,
        exit_code=42,
        stdout="urgent email from ceo",
        stderr="",
        raw_output="urgent email from ceo",
        timed_out=False,
        error=None,
    )


def _private_identity() -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="ops",
        requester_id="@alice:example.org",
        room_id="!lobby:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )


@pytest.mark.asyncio
async def test_none_action_succeeds_without_visible_effects(
    config: Config,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """None actions should record success without sending or emitting anything."""
    sender = _FakeMessageSender()
    automation = _automation(tmp_path, action_type="none", room=None, thread_id=None, message=None, trigger=None)

    result = await run_automation_action(
        config=config,
        runtime_paths=runtime_paths,
        target=cast("WorkspaceAutomationTarget", _target(tmp_path, policy=_policy())),
        automation=automation,
        check_result=_check_result(),
        hook_registry=HookRegistry.empty(),
        message_sender=sender,
    )

    assert result.ok is True
    assert result.action_type == "none"
    assert result.event_id is None
    assert result.failure_reason is None
    assert sender.calls == []


@pytest.mark.asyncio
async def test_matrix_message_sends_visible_message_without_triggering_dispatch(
    config: Config,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Matrix-message actions should use hook sender semantics without dispatch metadata."""
    sender = _FakeMessageSender()
    _add_room(runtime_paths, "Lobby")
    automation = _automation(tmp_path, action_type="matrix_message")

    result = await run_automation_action(
        config=config,
        runtime_paths=runtime_paths,
        target=cast("WorkspaceAutomationTarget", _target(tmp_path, policy=_policy("matrix_message"))),
        automation=automation,
        check_result=_check_result(),
        hook_registry=HookRegistry.empty(),
        message_sender=sender,
    )

    assert result.ok is True
    assert result.action_type == "matrix_message"
    assert result.event_id == "$sent"
    assert sender.calls == [
        _SenderCall(
            room_id="!lobby:localhost",
            body="Urgent email condition matched. Investigate and summarize.",
            thread_id="$thread",
            source_hook=format_hook_source("workspace_automation", EVENT_AUTOMATION_TRIGGERED),
            extra_content=None,
            trigger_dispatch=False,
        ),
    ]


@pytest.mark.asyncio
async def test_agent_message_sends_visible_message_that_triggers_dispatch(
    config: Config,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Agent-message actions should differ from matrix-message only by dispatch metadata."""
    sender = _FakeMessageSender()
    _add_room(runtime_paths, "Lobby")
    _add_agent_accounts(runtime_paths)
    automation = _automation(tmp_path, action_type="agent_message")

    result = await run_automation_action(
        config=config,
        runtime_paths=runtime_paths,
        target=cast("WorkspaceAutomationTarget", _target(tmp_path, policy=_policy("agent_message"))),
        automation=automation,
        check_result=_check_result(),
        hook_registry=HookRegistry.empty(),
        message_sender=sender,
    )

    assert result.ok is True
    assert result.event_id == "$sent"
    assert sender.calls[0].room_id == "!lobby:localhost"
    assert sender.calls[0].trigger_dispatch is True
    assert sender.calls[0].source_hook == format_hook_source("workspace_automation", EVENT_AUTOMATION_TRIGGERED)
    assert sender.calls[0].extra_content == {"m.mentions": {"user_ids": ["@mindroom_ops:localhost"]}}


@pytest.mark.asyncio
async def test_private_agent_message_preserves_requester_original_sender(
    config: Config,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Private agent-message actions should dispatch under the persisted requester."""
    sender = _FakeMessageSender()
    _add_room(runtime_paths, "Lobby")
    _add_agent_accounts(runtime_paths)
    automation = _automation(tmp_path, action_type="agent_message")

    result = await run_automation_action(
        config=config,
        runtime_paths=runtime_paths,
        target=cast(
            "WorkspaceAutomationTarget",
            _target_with_identity(
                tmp_path,
                policy=_policy("agent_message"),
                execution_identity=_private_identity(),
                is_private=True,
            ),
        ),
        automation=automation,
        check_result=_check_result(),
        hook_registry=HookRegistry.empty(),
        message_sender=sender,
    )

    assert result.ok is True
    assert sender.calls[0].trigger_dispatch is True
    assert sender.calls[0].extra_content == {
        "m.mentions": {"user_ids": ["@mindroom_ops:localhost"]},
        ORIGINAL_SENDER_KEY: "@alice:example.org",
    }


@pytest.mark.asyncio
async def test_private_matrix_message_does_not_preserve_requester_original_sender(
    config: Config,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Private matrix-message actions should stay visible-only hook sends."""
    sender = _FakeMessageSender()
    _add_room(runtime_paths, "Lobby")
    automation = _automation(tmp_path, action_type="matrix_message")

    result = await run_automation_action(
        config=config,
        runtime_paths=runtime_paths,
        target=cast(
            "WorkspaceAutomationTarget",
            _target_with_identity(
                tmp_path,
                policy=_policy("matrix_message"),
                execution_identity=_private_identity(),
                is_private=True,
            ),
        ),
        automation=automation,
        check_result=_check_result(),
        hook_registry=HookRegistry.empty(),
        message_sender=sender,
    )

    assert result.ok is True
    assert sender.calls[0].trigger_dispatch is False
    assert sender.calls[0].extra_content is None


@pytest.mark.asyncio
async def test_hook_action_emits_automation_triggered_context_without_matrix_message(
    config: Config,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Hook actions should emit automation context and avoid visible Matrix sends."""
    captured: list[AutomationTriggeredContext] = []

    @hook(EVENT_AUTOMATION_TRIGGERED, agents=["ops"], rooms=["!lobby:localhost"])
    async def on_automation(ctx: AutomationTriggeredContext) -> None:
        captured.append(ctx)

    registry = HookRegistry.from_plugins([_plugin("automation-test", [on_automation])])
    sender = _FakeMessageSender()
    _add_room(runtime_paths, "Lobby")
    automation = _automation(tmp_path, action_type="hook", message=None)

    result = await run_automation_action(
        config=config,
        runtime_paths=runtime_paths,
        target=cast("WorkspaceAutomationTarget", _target(tmp_path, policy=_policy("hook"))),
        automation=automation,
        check_result=_check_result(),
        hook_registry=registry,
        message_sender=sender,
    )

    assert result.ok is True
    assert result.action_type == "hook"
    assert result.event_id is None
    assert sender.calls == []

    assert len(captured) == 1
    context = captured[0]
    assert context.event_name == EVENT_AUTOMATION_TRIGGERED
    assert context.agent_name == "ops"
    assert context.automation_id == "urgent_email_poll"
    assert context.workspace_root == str(tmp_path)
    assert context.room_id == "!lobby:localhost"
    assert context.thread_id == "$thread"
    assert context.check_result == {
        "automation_id": "urgent_email_poll",
        "ok": False,
        "exit_code": 42,
        "stdout": "urgent email from ceo",
        "stderr": "",
        "raw_output": "urgent email from ceo",
        "timed_out": False,
        "error": None,
    }
    assert context.trigger_payload == {"exit_code": 42}
    assert context.action_payload == {
        "type": "hook",
        "room": "Lobby",
        "thread_id": "$thread",
    }


@pytest.mark.asyncio
async def test_hook_action_refuses_unresolved_configured_room(
    config: Config,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Hook actions should not expose authored room names to room-scoped hooks."""
    captured: list[AutomationTriggeredContext] = []

    @hook(EVENT_AUTOMATION_TRIGGERED, agents=["ops"], rooms=["!lobby:localhost"])
    async def on_automation(ctx: AutomationTriggeredContext) -> None:
        captured.append(ctx)

    result = await run_automation_action(
        config=config,
        runtime_paths=runtime_paths,
        target=cast("WorkspaceAutomationTarget", _target(tmp_path, policy=_policy("hook"))),
        automation=_automation(tmp_path, action_type="hook", message=None),
        check_result=_check_result(),
        hook_registry=HookRegistry.from_plugins([_plugin("automation-test", [on_automation])]),
        message_sender=_FakeMessageSender(),
    )

    assert result.ok is False
    assert result.failure_reason == "action.room 'Lobby' did not resolve to a Matrix room id"
    assert captured == []


@pytest.mark.asyncio
async def test_private_hook_action_send_message_preserves_requester_original_sender(
    config: Config,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Automation hook sends should preserve the private requester identity."""
    captured: list[AutomationTriggeredContext] = []

    @hook(EVENT_AUTOMATION_TRIGGERED, agents=["ops"], rooms=["!lobby:localhost"])
    async def on_automation(ctx: AutomationTriggeredContext) -> None:
        captured.append(ctx)
        await ctx.send_message(
            "!lobby:localhost",
            "Investigate the private automation alert.",
            thread_id="$thread",
            trigger_dispatch=True,
        )

    registry = HookRegistry.from_plugins([_plugin("automation-test", [on_automation])])
    sender = _FakeMessageSender()
    _add_room(runtime_paths, "Lobby")
    automation = _automation(tmp_path, action_type="hook", message=None)

    result = await run_automation_action(
        config=config,
        runtime_paths=runtime_paths,
        target=cast(
            "WorkspaceAutomationTarget",
            _target_with_identity(
                tmp_path,
                policy=_policy("hook"),
                execution_identity=_private_identity(),
                is_private=True,
            ),
        ),
        automation=automation,
        check_result=_check_result(),
        hook_registry=registry,
        message_sender=sender,
    )

    assert result.ok is True
    assert len(captured) == 1
    assert captured[0].requester_id == "@alice:example.org"
    assert sender.calls == [
        _SenderCall(
            room_id="!lobby:localhost",
            body="Investigate the private automation alert.",
            thread_id="$thread",
            source_hook=format_hook_source("automation-test", EVENT_AUTOMATION_TRIGGERED),
            extra_content={
                HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 1,
                ORIGINAL_SENDER_KEY: "@alice:example.org",
            },
            trigger_dispatch=True,
        ),
    ]


@pytest.mark.asyncio
async def test_private_hook_action_plain_send_message_does_not_preserve_requester_original_sender(
    config: Config,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Automation hook sends should preserve requester metadata only for explicit dispatch."""
    captured: list[AutomationTriggeredContext] = []

    @hook(EVENT_AUTOMATION_TRIGGERED, agents=["ops"], rooms=["!lobby:localhost"])
    async def on_automation(ctx: AutomationTriggeredContext) -> None:
        captured.append(ctx)
        await ctx.send_message(
            "!lobby:localhost",
            "Visible private automation alert.",
            thread_id="$thread",
            trigger_dispatch=False,
        )

    registry = HookRegistry.from_plugins([_plugin("automation-test", [on_automation])])
    sender = _FakeMessageSender()
    _add_room(runtime_paths, "Lobby")
    automation = _automation(tmp_path, action_type="hook", message=None)

    result = await run_automation_action(
        config=config,
        runtime_paths=runtime_paths,
        target=cast(
            "WorkspaceAutomationTarget",
            _target_with_identity(
                tmp_path,
                policy=_policy("hook"),
                execution_identity=_private_identity(),
                is_private=True,
            ),
        ),
        automation=automation,
        check_result=_check_result(),
        hook_registry=registry,
        message_sender=sender,
    )

    assert result.ok is True
    assert len(captured) == 1
    assert captured[0].requester_id == "@alice:example.org"
    assert sender.calls[0].trigger_dispatch is False
    assert sender.calls[0].extra_content == {HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 1}


@pytest.mark.asyncio
async def test_action_execution_rechecks_policy_before_visible_effects(
    config: Config,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Action execution should defensively enforce policy before side effects."""
    sender = _FakeMessageSender()

    result = await run_automation_action(
        config=config,
        runtime_paths=runtime_paths,
        target=cast("WorkspaceAutomationTarget", _target(tmp_path, policy=_policy())),
        automation=_automation(tmp_path, action_type="agent_message"),
        check_result=_check_result(),
        hook_registry=HookRegistry.empty(),
        message_sender=sender,
    )

    assert result.ok is False
    assert result.failure_reason == "action.type 'agent_message' is not allowed by workspace automation policy"
    assert sender.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("rooms", [(), ("Lobby", "Ops")])
async def test_visible_action_room_resolution_failure_returns_non_transient_failure(
    config: Config,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
    rooms: tuple[str, ...],
) -> None:
    """Visible actions should fail when no Matrix room can be resolved."""
    sender = _FakeMessageSender()

    result = await run_automation_action(
        config=config,
        runtime_paths=runtime_paths,
        target=cast(
            "WorkspaceAutomationTarget",
            _target(tmp_path, rooms=rooms, policy=_policy("matrix_message")),
        ),
        automation=_automation(tmp_path, action_type="matrix_message", room=None),
        check_result=_check_result(),
        hook_registry=HookRegistry.empty(),
        message_sender=sender,
    )

    assert result.ok is False
    assert result.failure_reason == "action.room is required unless the owning agent has exactly one configured room"
    assert sender.calls == []


@pytest.mark.asyncio
async def test_visible_action_refuses_unresolved_configured_room(
    config: Config,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Visible actions should not send raw configured room names as Matrix room IDs."""
    sender = _FakeMessageSender()

    result = await run_automation_action(
        config=config,
        runtime_paths=runtime_paths,
        target=cast("WorkspaceAutomationTarget", _target(tmp_path, policy=_policy("matrix_message"))),
        automation=_automation(tmp_path, action_type="matrix_message"),
        check_result=_check_result(),
        hook_registry=HookRegistry.empty(),
        message_sender=sender,
    )

    assert result.ok is False
    assert result.failure_reason == "action.room 'Lobby' did not resolve to a Matrix room id"
    assert sender.calls == []


@pytest.mark.asyncio
async def test_visible_action_without_message_sender_returns_transient_failure(
    config: Config,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Visible actions should report missing sender as retryable runtime state."""
    result = await run_automation_action(
        config=config,
        runtime_paths=runtime_paths,
        target=cast("WorkspaceAutomationTarget", _target(tmp_path, policy=_policy("matrix_message"))),
        automation=_automation(tmp_path, action_type="matrix_message"),
        check_result=_check_result(),
        hook_registry=HookRegistry.empty(),
        message_sender=None,
    )

    assert result.ok is False
    assert result.failure_reason == "hook message sender is not available"


@pytest.mark.asyncio
async def test_visible_action_without_message_text_returns_non_transient_failure(
    config: Config,
    runtime_paths: RuntimePaths,
    tmp_path: Path,
) -> None:
    """Visible actions should reject empty authored message bodies."""
    sender = _FakeMessageSender()

    result = await run_automation_action(
        config=config,
        runtime_paths=runtime_paths,
        target=cast("WorkspaceAutomationTarget", _target(tmp_path, policy=_policy("agent_message"))),
        automation=_automation(tmp_path, action_type="agent_message", message=None),
        check_result=_check_result(),
        hook_registry=HookRegistry.empty(),
        message_sender=sender,
    )

    assert result.ok is False
    assert result.failure_reason == "action.message is required for visible workspace automation actions"
    assert sender.calls == []
