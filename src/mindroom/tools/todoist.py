"""Todoist tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.todoist import TodoistTools


@register_tool_with_metadata(
    name="todoist",
    display_name="Todoist",
    description="Task management with Todoist - create, update, delete, and organize tasks and projects",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiTodoist",
    icon_color="text-red-500",
    config_fields=[
        ConfigField(
            name="api_token",
            label="API Token",
            type="password",
            required=False,
            default=None,
        ),
    ],
    dependencies=["todoist-api-python"],
    docs_url="https://docs.agno.com/tools/toolkits/others/todoist",
)
def todoist_tools() -> type[TodoistTools]:
    """Return Todoist tools for task management."""
    from agno.tools.todoist import TodoistTools

    return TodoistTools
