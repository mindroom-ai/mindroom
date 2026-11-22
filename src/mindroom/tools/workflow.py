"""Workflow tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolExecutionTarget,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.workflow import WorkflowTools


def _limit(name: str, label: str, default: float | None, minimum: float = 1) -> ConfigField:
    return ConfigField(
        name=name,
        label=label,
        type="number",
        required=False,
        default=default,
        validation={"min": minimum},
    )


@register_tool_with_metadata(
    name="workflow",
    display_name="Workflows",
    description="Run a Python script that calls your tools in the background, and read or cancel the run",
    docs_url="https://docs.mindroom.chat/tools/workflows/",
    category=ToolCategory.DEVELOPMENT,
    file_access=ToolFileAccess.NONE,
    executes_code=True,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    default_execution_target=ToolExecutionTarget.PRIMARY,
    requires_primary_runtime=True,
    icon="Workflow",
    icon_color="text-emerald-500",
    config_fields=[
        _limit("max_concurrent_runs", "Maximum Concurrent Runs", 8),
        _limit("max_runtime_hours", "Maximum Runtime Hours", 72, minimum=0),
        _limit("max_tool_calls_per_minute", "Maximum Tool Calls Per Minute", 600),
        _limit("max_concurrent_agents", "Maximum Concurrent Agents Per Run", None),
        _limit("large_run_notice_usd", "Large Run Notice (USD)", 10, minimum=0),
    ],
    managed_init_args=(ToolManagedInitArg.RUNTIME_CONFIG,),
    requires_room_context=True,
    function_names=("start_workflow", "get_workflow", "cancel_workflow"),
    workspace_writers=("start_workflow",),
    supports_toolkit_filters=False,
)
def workflow_tools() -> type[WorkflowTools]:
    """Return the primary-owned workflow controls."""
    from mindroom.custom_tools.workflow import WorkflowTools

    return WorkflowTools
