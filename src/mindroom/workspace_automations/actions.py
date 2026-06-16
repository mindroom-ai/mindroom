"""Workspace automation action execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from mindroom.hooks import (
    EVENT_AUTOMATION_TRIGGERED,
    AutomationTriggeredContext,
    HookRegistry,
    emit,
)
from mindroom.hooks.types import format_hook_source
from mindroom.logging_config import get_logger
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
    transient: bool = False
    hook_emitted: bool = False


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
        result = await _run_visible_action(target=target, automation=automation, message_sender=message_sender)
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
    room_id = resolve_action_room(
        action_room=automation.action.room,
        agent_configured_rooms=target.agent_configured_rooms,
    )
    context = _automation_context(
        config=config,
        runtime_paths=runtime_paths,
        automation=automation,
        check_result=check_result,
        room_id=room_id,
        trigger_payload=trigger_payload,
        message_sender=message_sender,
    )
    await emit(hook_registry or HookRegistry.empty(), EVENT_AUTOMATION_TRIGGERED, context)
    return _success(automation, hook_emitted=True)


async def _run_visible_action(
    *,
    target: WorkspaceAutomationTarget,
    automation: LoadedWorkspaceAutomation,
    message_sender: HookMessageSender | None,
) -> WorkspaceAutomationActionResult:
    action = automation.action
    room_id = resolve_action_room(
        action_room=action.room,
        agent_configured_rooms=target.agent_configured_rooms,
    )
    if room_id is None:
        return _failure(
            automation,
            "action.room is required unless the owning agent has exactly one configured room",
        )
    if action.message is None:
        return _failure(
            automation,
            "action.message is required for visible workspace automation actions",
        )
    if message_sender is None:
        return _failure(automation, "hook message sender is not available", transient=True)

    try:
        event_id = await message_sender(
            room_id,
            action.message,
            action.thread_id,
            format_hook_source(_HOOK_SOURCE_PLUGIN_NAME, EVENT_AUTOMATION_TRIGGERED),
            None,
            trigger_dispatch=action.type == "agent_message",
        )
    except Exception as exc:
        return _failure(automation, f"{type(exc).__name__}: {exc}", transient=True)
    if event_id is None:
        return _failure(automation, "hook message sender did not return an event id", transient=True)
    return _success(automation, event_id=event_id)


def _automation_context(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
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


def _success(
    automation: LoadedWorkspaceAutomation,
    *,
    event_id: str | None = None,
    hook_emitted: bool = False,
) -> WorkspaceAutomationActionResult:
    return WorkspaceAutomationActionResult(
        automation_id=automation.automation_id,
        action_type=automation.action.type,
        ok=True,
        event_id=event_id,
        hook_emitted=hook_emitted,
    )


def _failure(
    automation: LoadedWorkspaceAutomation,
    reason: str,
    *,
    transient: bool = False,
) -> WorkspaceAutomationActionResult:
    return WorkspaceAutomationActionResult(
        automation_id=automation.automation_id,
        action_type=automation.action.type,
        ok=False,
        failure_reason=reason,
        transient=transient,
    )


__all__ = ["WorkspaceAutomationActionResult", "run_automation_action"]
