"""Agent-facing management tool for workspace automations."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    execution_identity_matches_tool_runtime_context,
    get_tool_runtime_context,
)
from mindroom.workspace_automations.loader import load_workspace_automations
from mindroom.workspace_automations.service import get_active_workspace_automation_service
from mindroom.workspace_automations.targets import WorkspaceAutomationTarget, iter_workspace_automation_targets

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.workspace_automations.models import LoadedWorkspaceAutomation, WorkspaceAutomationLoadError
    from mindroom.workspace_automations.service import WorkspaceAutomationLoadedStatus

_TOOL_NAME = "workspace_automation"
_UNAVAILABLE_CODE = "unavailable"
_SERVICE_UNAVAILABLE_MESSAGE = "Workspace automation service is unavailable in this runtime."
_CONTEXT_UNAVAILABLE_MESSAGE = "Workspace automation validation requires an active tool runtime context."


class WorkspaceAutomationTools(Toolkit):
    """Tools for validating and supervising workspace-authored automations."""

    def __init__(self) -> None:
        super().__init__(
            name=_TOOL_NAME,
            tools=[
                self.validate_automations,
                self.list_automations,
                self.reload_automations,
            ],
        )

    async def validate_automations(self) -> str:
        """Validate configured workspace automation files without starting automation tasks.

        Returns:
            A JSON payload with loaded automation summaries and structured load errors.

        """
        context = get_tool_runtime_context()
        if context is None:
            return _error_payload(_CONTEXT_UNAVAILABLE_MESSAGE, code=_UNAVAILABLE_CODE)

        automations: list[LoadedWorkspaceAutomation] = []
        errors: list[WorkspaceAutomationLoadError] = []
        for target in iter_workspace_automation_targets(context.config, context.runtime_paths):
            if not _target_matches_context(target, context):
                continue
            result = load_workspace_automations(
                agent_name=target.agent_name,
                workspace_root=target.workspace_root,
                agent_rooms=target.agent_configured_rooms,
                policy=target.policy,
            )
            automations.extend(result.automations)
            errors.extend(result.errors)

        return custom_tool_payload(
            _TOOL_NAME,
            "ok",
            loaded_count=len(automations),
            error_count=len(errors),
            automations=[_loaded_automation_payload(automation) for automation in automations],
            errors=[_load_error_payload(error) for error in errors],
        )

    async def list_automations(self) -> str:
        """List the currently loaded workspace automations from the live service.

        Returns:
            A JSON payload with loaded automation statuses, or an unavailable error.

        """
        context = get_tool_runtime_context()
        if context is None:
            return _error_payload(_CONTEXT_UNAVAILABLE_MESSAGE, code=_UNAVAILABLE_CODE)
        service = get_active_workspace_automation_service()
        if service is None or not service.is_started:
            return _service_unavailable_payload()

        target_filter = _target_filter_for_context(context)
        return custom_tool_payload(
            _TOOL_NAME,
            "ok",
            automations=[_loaded_status_payload(status) for status in service.list_loaded(target_filter=target_filter)],
        )

    async def reload_automations(self) -> str:
        """Reload workspace automation files through the live service.

        Returns:
            A JSON payload with scan counts, load errors, and loaded automation statuses.

        """
        context = get_tool_runtime_context()
        if context is None:
            return _error_payload(_CONTEXT_UNAVAILABLE_MESSAGE, code=_UNAVAILABLE_CODE)
        service = get_active_workspace_automation_service()
        if service is None or not service.is_started:
            return _service_unavailable_payload()

        target_filter = _target_filter_for_context(context)
        try:
            scan_result = await service.scan_now(target_filter=target_filter)
        except RuntimeError as exc:
            return _error_payload(f"{_SERVICE_UNAVAILABLE_MESSAGE} {exc}", code=_UNAVAILABLE_CODE)

        return custom_tool_payload(
            _TOOL_NAME,
            "ok",
            loaded_count=scan_result.loaded_count,
            error_count=scan_result.error_count,
            automations=[_loaded_status_payload(status) for status in service.list_loaded(target_filter=target_filter)],
            errors=[_load_error_payload(error) for error in scan_result.errors],
        )


def _service_unavailable_payload() -> str:
    return _error_payload(_SERVICE_UNAVAILABLE_MESSAGE, code=_UNAVAILABLE_CODE)


def _error_payload(message: str, *, code: str) -> str:
    return custom_tool_payload(_TOOL_NAME, "error", code=code, message=message)


def _target_filter_for_context(context: ToolRuntimeContext) -> Callable[[WorkspaceAutomationTarget], bool]:
    return lambda target: _target_matches_context(target, context)


def _target_matches_context(target: WorkspaceAutomationTarget, context: ToolRuntimeContext) -> bool:
    if target.agent_name != context.agent_name:
        return False
    execution_identity = target.agent_runtime.execution_identity
    if target.agent_runtime.is_private:
        return execution_identity is not None and execution_identity_matches_tool_runtime_context(
            execution_identity,
            context,
        )
    return execution_identity is None


def _loaded_status_payload(status: WorkspaceAutomationLoadedStatus) -> dict[str, object]:
    return asdict(status)


def _load_error_payload(error: WorkspaceAutomationLoadError) -> dict[str, object]:
    return {
        "automation_id": error.automation_id,
        "field_path": list(error.field_path),
        "file_path": str(error.file_path),
        "message": error.message,
    }


def _loaded_automation_payload(automation: LoadedWorkspaceAutomation) -> dict[str, object]:
    return {
        "action": automation.action.model_dump(exclude_none=True),
        "agent_name": automation.agent_name,
        "automation_id": automation.automation_id,
        "check": automation.check.model_dump(),
        "file_path": str(automation.file_path),
        "schedule": automation.schedule,
        "trigger": automation.trigger.model_dump(exclude_none=True) if automation.trigger is not None else None,
        "workspace_root": str(automation.workspace_root),
    }
