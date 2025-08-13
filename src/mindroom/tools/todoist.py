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
    icon="FaTasks",
    icon_color="text-red-500",
    config_fields=[
        # Authentication
        ConfigField(
            name="api_token",
            label="API Token",
            type="password",
            required=False,
            placeholder="your_api_token",
            description="Todoist API token (can also be set via TODOIST_API_TOKEN env var). Get from https://app.todoist.com/app/settings/integrations/developer",
        ),
        # Task operations
        ConfigField(
            name="create_task",
            label="Create Task",
            type="boolean",
            required=False,
            default=True,
            description="Enable creating new tasks in Todoist",
        ),
        ConfigField(
            name="get_task",
            label="Get Task",
            type="boolean",
            required=False,
            default=True,
            description="Enable fetching specific tasks by ID",
        ),
        ConfigField(
            name="update_task",
            label="Update Task",
            type="boolean",
            required=False,
            default=True,
            description="Enable updating existing tasks with new properties",
        ),
        ConfigField(
            name="close_task",
            label="Close Task",
            type="boolean",
            required=False,
            default=True,
            description="Enable marking tasks as completed",
        ),
        ConfigField(
            name="delete_task",
            label="Delete Task",
            type="boolean",
            required=False,
            default=True,
            description="Enable deleting tasks from Todoist",
        ),
        ConfigField(
            name="get_active_tasks",
            label="Get Active Tasks",
            type="boolean",
            required=False,
            default=True,
            description="Enable retrieving all active (non-completed) tasks",
        ),
        ConfigField(
            name="get_projects",
            label="Get Projects",
            type="boolean",
            required=False,
            default=True,
            description="Enable retrieving all projects in Todoist",
        ),
    ],
    dependencies=["todoist-api-python"],
    docs_url="https://docs.agno.com/tools/toolkits/others/todoist",
)
def todoist_tools() -> type[TodoistTools]:
    """Return Todoist tools for task management."""
    from agno.tools.todoist import TodoistTools

    return TodoistTools
