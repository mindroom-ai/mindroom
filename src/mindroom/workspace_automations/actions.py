"""Workspace automation action execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.entity_resolution import entity_identity_registry
from mindroom.hooks import (
    EVENT_AUTOMATION_TRIGGERED,
    AutomationTriggeredContext,
    HookRegistry,
    emit,
)
from mindroom.hooks.types import format_hook_source
from mindroom.logging_config import get_logger
from mindroom.matrix.state import resolve_room_aliases
from mindroom.workspace_automations.targets import resolve_action_room

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import HookMessageSender
    from mindroom.workspace_automations.executor import ShellCheckResult
    from mindroom.workspace_automations.models import LoadedWorkspaceAutomation
    from mindroom.workspace_automations.targets import WorkspaceAutomationTarget

_LOGGER = get_logger(__name__)
_HOOK_SOURCE_PLUGIN_NAME = "workspace_automation"
_VISIBLE_ACTION_TYPES = {"agent_message", "matrix_message"}


@dataclass(frozen=True, slots=True)
class WorkspaceAutomationActionResult:
    """Outcome of one workspace automation action attempt."""

    automation_id: str
    action_type: str
    ok: bool
    event_id: str | None = None
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _VisibleActionRequest:
    room_id: str
    message: str
    thread_id: str | None
    message_sender: HookMessageSender


async def run_automation_action(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    target: WorkspaceAutomationTarget,
    automation: LoadedWorkspaceAutomation,
    check_result: ShellCheckResult,
    hook_registry: HookRegistry | None = None,
    message_sender: HookMessageSender | None = None,
    trigger_payload: Mapping[str, Any] | None = None,
) -> WorkspaceAutomationActionResult:
    """Run the configured action for one triggered workspace automation."""
    action = automation.action
    action_type = action.type

    if action_type != "none" and action_type not in target.policy.allowed_actions:
        result = _failure(
            automation,
            f"action.type '{action_type}' is not allowed by workspace automation policy",
        )
    elif action_type == "none":
        result = _success(automation)
    elif action_type == "hook":
        result = await _run_hook_action(
            config=config,
            runtime_paths=runtime_paths,
            target=target,
            automation=automation,
            check_result=check_result,
            trigger_payload=trigger_payload,
            message_sender=message_sender,
            hook_registry=hook_registry,
        )
    elif action_type in _VISIBLE_ACTION_TYPES:
        result = await _run_visible_action(
            config=config,
            runtime_paths=runtime_paths,
            target=target,
            automation=automation,
            message_sender=message_sender,
        )
    else:
        result = _failure(automation, f"Unsupported workspace automation action type: {action_type}")
    return result


async def _run_hook_action(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    target: WorkspaceAutomationTarget,
    automation: LoadedWorkspaceAutomation,
    check_result: ShellCheckResult,
    trigger_payload: Mapping[str, Any] | None,
    message_sender: HookMessageSender | None,
    hook_registry: HookRegistry | None,
) -> WorkspaceAutomationActionResult:
    room = resolve_action_room(
        action_room=automation.action.room,
        agent_configured_rooms=target.agent_configured_rooms,
    )
    room_id = None
    if room is not None:
        room_id = _resolve_matrix_room_id(room, runtime_paths)
        if room_id is None:
            return _failure(automation, f"action.room '{room}' did not resolve to a Matrix room id")
    context = _automation_context(
        config=config,
        runtime_paths=runtime_paths,
        target=target,
        automation=automation,
        check_result=check_result,
        room_id=room_id,
        trigger_payload=trigger_payload,
        message_sender=message_sender,
    )
    await emit(hook_registry or HookRegistry.empty(), EVENT_AUTOMATION_TRIGGERED, context)
    return _success(automation)


async def _run_visible_action(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    target: WorkspaceAutomationTarget,
    automation: LoadedWorkspaceAutomation,
    message_sender: HookMessageSender | None,
) -> WorkspaceAutomationActionResult:
    prepared = _prepare_visible_action(
        runtime_paths=runtime_paths,
        target=target,
        automation=automation,
        message_sender=message_sender,
    )
    if isinstance(prepared, str):
        return _failure(automation, prepared)

    trigger_dispatch = automation.action.type == "agent_message"
    try:
        event_id = await prepared.message_sender(
            prepared.room_id,
            prepared.message,
            prepared.thread_id,
            format_hook_source(_HOOK_SOURCE_PLUGIN_NAME, EVENT_AUTOMATION_TRIGGERED),
            _extra_content_for_visible_action(
                config=config,
                runtime_paths=runtime_paths,
                target=target,
                automation=automation,
            )
            if trigger_dispatch
            else None,
            trigger_dispatch=trigger_dispatch,
        )
    except Exception as exc:
        return _failure(automation, f"{type(exc).__name__}: {exc}")
    if event_id is None:
        return _failure(automation, "hook message sender did not return an event id")
    return _success(automation, event_id=event_id)


def _prepare_visible_action(
    *,
    runtime_paths: RuntimePaths,
    target: WorkspaceAutomationTarget,
    automation: LoadedWorkspaceAutomation,
    message_sender: HookMessageSender | None,
) -> _VisibleActionRequest | str:
    action = automation.action
    room_id = resolve_action_room(
        action_room=action.room,
        agent_configured_rooms=target.agent_configured_rooms,
    )
    if room_id is None:
        return "action.room is required unless the owning agent has exactly one configured room"
    if action.message is None:
        return "action.message is required for visible workspace automation actions"
    if message_sender is None:
        return "hook message sender is not available"
    resolved_room_id = _resolve_matrix_room_id(room_id, runtime_paths)
    if resolved_room_id is None:
        return f"action.room '{room_id}' did not resolve to a Matrix room id"
    return _VisibleActionRequest(
        room_id=resolved_room_id,
        message=action.message,
        thread_id=action.thread_id,
        message_sender=message_sender,
    )


def _resolve_matrix_room_id(room: str, runtime_paths: RuntimePaths) -> str | None:
    resolved_room = resolve_room_aliases([room], runtime_paths)[0]
    if resolved_room.startswith("!"):
        return resolved_room
    return None


def _extra_content_for_visible_action(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    target: WorkspaceAutomationTarget,
    automation: LoadedWorkspaceAutomation,
) -> dict[str, Any] | None:
    if automation.action.type != "agent_message":
        return None
    owner_user_id = entity_identity_registry(config, runtime_paths).current_id(automation.agent_name).full_id
    extra_content: dict[str, Any] = {"m.mentions": {"user_ids": [owner_user_id]}}
    requester_id = _automation_requester_id(target)
    if requester_id is not None:
        extra_content[ORIGINAL_SENDER_KEY] = requester_id
    return extra_content


def _automation_context(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    target: WorkspaceAutomationTarget,
    automation: LoadedWorkspaceAutomation,
    check_result: ShellCheckResult,
    room_id: str | None,
    trigger_payload: Mapping[str, Any] | None,
    message_sender: HookMessageSender | None,
) -> AutomationTriggeredContext:
    return AutomationTriggeredContext(
        event_name=EVENT_AUTOMATION_TRIGGERED,
        plugin_name="",
        settings={},
        config=config,
        runtime_paths=runtime_paths,
        logger=_LOGGER.bind(
            event_name=EVENT_AUTOMATION_TRIGGERED,
            agent_name=automation.agent_name,
            automation_id=automation.automation_id,
        ),
        correlation_id=f"{EVENT_AUTOMATION_TRIGGERED}:{automation.agent_name}:{automation.automation_id}",
        message_sender=message_sender,
        requester_id=_automation_requester_id(target),
        agent_name=automation.agent_name,
        automation_id=automation.automation_id,
        workspace_root=str(automation.workspace_root),
        room_id=room_id,
        thread_id=automation.action.thread_id,
        check_result=asdict(check_result),
        trigger_payload=_resolved_trigger_payload(automation, trigger_payload),
        action_payload=automation.action.model_dump(exclude_none=True),
    )


def _resolved_trigger_payload(
    automation: LoadedWorkspaceAutomation,
    trigger_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if trigger_payload is not None:
        return dict(trigger_payload)
    if automation.trigger is None:
        return {}
    return automation.trigger.model_dump(exclude_none=True)


def _automation_requester_id(target: WorkspaceAutomationTarget) -> str | None:
    if not target.agent_runtime.is_private:
        return None
    execution_identity = target.agent_runtime.execution_identity
    if execution_identity is None:
        return None
    return execution_identity.requester_id or None


def _success(
    automation: LoadedWorkspaceAutomation,
    *,
    event_id: str | None = None,
) -> WorkspaceAutomationActionResult:
    return WorkspaceAutomationActionResult(
        automation_id=automation.automation_id,
        action_type=automation.action.type,
        ok=True,
        event_id=event_id,
    )


def _failure(
    automation: LoadedWorkspaceAutomation,
    reason: str,
) -> WorkspaceAutomationActionResult:
    return WorkspaceAutomationActionResult(
        automation_id=automation.automation_id,
        action_type=automation.action.type,
        ok=False,
        failure_reason=reason,
    )


__all__ = ["WorkspaceAutomationActionResult", "run_automation_action"]
