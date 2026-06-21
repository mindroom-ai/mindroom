"""Workspace automation tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import (
    SetupType,
    ToolCategory,
    ToolExecutionTarget,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from mindroom.custom_tools.workspace_automation import WorkspaceAutomationTools


@register_tool_with_metadata(
    name="workspace_automation",
    display_name="Workspace Automation",
    description="Validate, list, and reload workspace-authored automations",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    default_execution_target=ToolExecutionTarget.PRIMARY,
    icon="Workflow",
    icon_color="text-indigo-500",
    dependencies=["agno"],
    docs_url="https://docs.mindroom.chat/workspace-automations/",
    function_names=(
        "list_automations",
        "reload_automations",
        "validate_automations",
    ),
)
def workspace_automation_tools() -> type[WorkspaceAutomationTools]:
    """Return workspace automation tools for agent-facing management."""
    from mindroom.custom_tools.workspace_automation import WorkspaceAutomationTools

    return WorkspaceAutomationTools
